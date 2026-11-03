"""The counted free-tier verifiers (Bounceless, EmailVerify.io, GetProspect,
Abstract, MailboxValidator, Sniffmail, emailvalidation.io, BounceZero,
MyEmailVerifier): each provider's answer is read into the same verdict shape
as Reoon's and ZeroBounce's, anything odd is inconclusive (None) and never
raises, they run after the other verifiers in both orders, and each one stops
once this month's (or, when set, today's) allowance is used. No network:
every request goes to an httpx.MockTransport, and the data is fictional."""

import json
import logging
import os
import tempfile
import time
from datetime import datetime, timezone

import httpx
import pytest
import pytest_asyncio

import harvey.dashboard as dash
import harvey.main  # noqa: F401  (installs the httpx log redaction filter)
from harvey.config import load_env
from harvey.integrations import email_finder as ef
from harvey.state import StateManager
from tests.test_dashboard_live_status import client, env  # noqa: F401  (fixtures)

REAL_CLIENT = httpx.AsyncClient
ADDRESS = "jane.doe@acme.example.test"
COUNTED = ("bounceless", "emailverify", "getprospect", "abstract",
           "mailboxvalidator", "sniffmail", "emailvalidationio", "bouncezero", "myemailverifier")
ALLOWANCE_SETTINGS = tuple(f"{s.upper()}_MONTHLY_CHECKS" for s in COUNTED)
DAILY_SETTINGS = tuple(f"{s.upper()}_DAILY_CHECKS" for s in COUNTED)
# The pacing as shipped, before the fixture below zeroes it for speed.
SHIPPED_INTERVALS = dict(ef.VERIFIER_MIN_INTERVALS)


@pytest_asyncio.fixture
async def state():
    with tempfile.TemporaryDirectory() as tmpdir:
        sm = StateManager(os.path.join(tmpdir, "test.db"))
        await sm.init_db()
        yield sm


@pytest.fixture(autouse=True)
def _defaults(monkeypatch):
    # Free-tier allowances and no daily caps unless a test sets one; no pause
    # between calls unless a test is about it.
    for name in ALLOWANCE_SETTINGS + DAILY_SETTINGS:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(ef, "ABSTRACT_MIN_INTERVAL", 0.0)
    monkeypatch.setattr(ef, "VERIFIER_MIN_INTERVALS", dict.fromkeys(ef.VERIFIER_MIN_INTERVALS, 0.0))
    monkeypatch.setattr(ef, "_verifier_last_calls", {})


def mock_http(monkeypatch, handler):
    """Route every httpx client in email_finder to ``handler``; returns the
    requests it saw."""
    seen = []

    def record(request):
        seen.append(request)
        return handler(request)

    transport = httpx.MockTransport(record)
    monkeypatch.setattr(ef.httpx, "AsyncClient",
                        lambda **kw: REAL_CLIENT(transport=transport, **kw))
    return seen


def answer(body=None, status=200, raw=None):
    def handler(request):
        if raw is not None:
            return httpx.Response(status, content=raw)
        return httpx.Response(status, json=body)
    return handler


async def graded(monkeypatch, verify, handler):
    """What _verify_candidate would make of this verifier's answer."""
    mock_http(monkeypatch, handler)
    verdict = await verify(ADDRESS, "test-key")
    return None if verdict is None else ef._status_from_verdict(verdict)


class Keys:
    """An env with the given verifier keys and nothing else."""

    def __init__(self, **keys):
        for attr in ("reoon", "zerobounce") + COUNTED:
            setattr(self, f"{attr}_api_key", keys.get(attr, ""))


# ── Bounceless ──

@pytest.mark.parametrize("body, expected", [
    ({"status": "completed", "presend": {"verdict": "DELIVERABLE", "decision": "send"}}, "verified"),
    ({"presend": {"verdict": "UNDELIVERABLE_NO_MAILBOX", "decision": "suppress"}}, "invalid"),
    ({"presend": {"verdict": "CATCH_ALL", "decision": "send_with_caution"}}, "risky"),
    ({"presend": {"verdict": "DISPOSABLE", "decision": "suppress"}}, "invalid"),
    ({"presend": {"verdict": "UNKNOWN", "decision": "recheck_later"}}, None),
    ({"status": "queued", "presend": {"verdict": None, "decision": None}}, None),
    ({"presend": "DELIVERABLE"}, None),
    ({"verdict": "DELIVERABLE"}, None),
    ([{"presend": {"verdict": "DELIVERABLE"}}], None),
])
@pytest.mark.asyncio
async def test_bounceless_verdicts(monkeypatch, body, expected):
    assert await graded(monkeypatch, ef.verify_bounceless, answer(body)) == expected


@pytest.mark.asyncio
async def test_bounceless_request_shape(monkeypatch):
    seen = mock_http(monkeypatch, answer({"presend": {"verdict": "DELIVERABLE"}}))
    await ef.verify_bounceless(ADDRESS, "blc.test-key")
    await ef.verify_bounceless(ADDRESS, "blc.test-key")
    first = seen[0]
    assert first.method == "POST" and str(first.url) == "https://api.bounceless.io/v1/verify"
    assert first.headers["X-Api-Key"] == "blc.test-key"
    assert json.loads(first.content)["email"] == ADDRESS
    # A fresh idempotency key per check, so a second check is a new one.
    assert first.headers["Idempotency-Key"] and \
        first.headers["Idempotency-Key"] != seen[1].headers["Idempotency-Key"]


# ── EmailVerify.io ──

@pytest.mark.parametrize("body, expected", [
    ({"email": ADDRESS, "status": "valid", "sub_status": "permitted"}, "verified"),
    ({"status": "Valid"}, "verified"),
    ({"status": "invalid", "sub_status": "mailbox_not_found"}, "invalid"),
    ({"status": "catch-all"}, "risky"),
    ({"status": "Catch-All", "sub_status": "None"}, "risky"),
    ({"status": "disposable"}, "invalid"),
    ({"status": "unknown", "sub_status": "greylisted"}, None),
    ({"status": "do_not_mail", "sub_status": "role_based"}, None),
    ({"status": "Do Not Mail", "sub_status": "possible_trap"}, "invalid"),
    ({"status": "skipped"}, None),
    ({"status": 42}, None),
    ({"message": "Invalid API key"}, None),
    ("valid", None),
])
@pytest.mark.asyncio
async def test_emailverify_verdicts(monkeypatch, body, expected):
    assert await graded(monkeypatch, ef.verify_emailverify, answer(body)) == expected


@pytest.mark.asyncio
async def test_emailverify_request_shape(monkeypatch):
    seen = mock_http(monkeypatch, answer({"status": "valid"}))
    await ef.verify_emailverify(ADDRESS, "ev-test-key")
    url = seen[0].url
    assert seen[0].method == "GET"
    assert (url.host, url.path) == ("app.emailverify.io", "/api/v1/validate")
    assert url.params["key"] == "ev-test-key" and url.params["email"] == ADDRESS


# ── GetProspect ──

@pytest.mark.parametrize("body, expected", [
    ({"success": True, "data": {"email": ADDRESS, "status": "valid", "domain_status": "valid"}},
     "verified"),
    # GetProspect counts a valid mailbox on an accept-all domain as confirmed.
    ({"success": True, "data": {"status": "valid", "domain_status": "accept_all"}}, "verified"),
    ({"success": True, "data": {"status": "invalid", "domain_status": "valid"}}, "invalid"),
    ({"success": True, "data": {"status": "accept_all", "domain_status": "accept_all"}}, "risky"),
    ({"success": True, "data": {"status": "unknown", "domain_status": "disposable"}}, "invalid"),
    ({"success": True, "data": {"status": "unknown", "domain_status": "unknown"}}, None),
    ({"success": False, "data": {"status": "unknown"},
      "errors": [{"name": "VERIFICATION_ERROR"}]}, None),
    ({"status": "valid"}, "verified"),          # a flat answer is read too
    ({"success": True, "data": None}, None),
    ({"success": True, "data": ["valid"]}, None),
])
@pytest.mark.asyncio
async def test_getprospect_verdicts(monkeypatch, body, expected):
    assert await graded(monkeypatch, ef.verify_getprospect, answer(body)) == expected


@pytest.mark.asyncio
async def test_getprospect_request_shape(monkeypatch):
    seen = mock_http(monkeypatch, answer({"success": True, "data": {"status": "valid"}}))
    await ef.verify_getprospect(ADDRESS, "gp-test-key")
    url = seen[0].url
    assert seen[0].method == "GET"
    assert (url.host, url.path) == ("api.getprospect.com", "/v2/email-verifier")
    assert url.params["email"] == ADDRESS and "api_key" not in url.params
    assert seen[0].headers["x-api-key"] == "gp-test-key"


# ── Abstract ──

def abstract_body(deliverability, catch_all=False, disposable=False):
    return {
        "email": ADDRESS, "deliverability": deliverability, "quality_score": "0.90",
        "is_catchall_email": {"value": catch_all, "text": str(catch_all).upper()},
        "is_disposable_email": {"value": disposable, "text": str(disposable).upper()},
    }


@pytest.mark.parametrize("body, expected", [
    (abstract_body("DELIVERABLE"), "verified"),
    (abstract_body("UNDELIVERABLE"), "invalid"),
    (abstract_body("DELIVERABLE", catch_all=True), "risky"),
    (abstract_body("UNKNOWN", catch_all=True), "risky"),
    (abstract_body("DELIVERABLE", disposable=True), "invalid"),
    (abstract_body("UNKNOWN"), None),
    # Only a real true counts: the text "TRUE" alone is not a flag.
    ({"deliverability": "UNKNOWN", "is_catchall_email": "TRUE"}, None),
    ({"deliverability": ["DELIVERABLE"]}, None),
    ({"error": {"message": "Invalid API key"}}, None),
])
@pytest.mark.asyncio
async def test_abstract_verdicts_from_an_older_validation_key(monkeypatch, body, expected):
    # An older key: Reputation rejects it, Validation answers.
    assert await graded(monkeypatch, ef.verify_abstract, abstract_by_host(validation=body)) == expected


def reputation_body(status, catch_all=False, disposable=False):
    return {
        "email_address": ADDRESS,
        "email_deliverability": {"status": status, "status_detail": "x", "is_smtp_valid": status == "deliverable"},
        "email_quality": {"score": 0.5, "is_catchall": catch_all, "is_disposable": disposable},
    }


def abstract_by_host(reputation=None, validation=None):
    """Abstract's two APIs: each answers its body, or 401 when given None."""
    def handler(request):
        body = reputation if request.url.host.startswith("emailreputation") else validation
        if body is None:
            return httpx.Response(401, json={"error": {"code": "unauthorized"}})
        return httpx.Response(200, json=body)
    return handler


@pytest.mark.parametrize("body, expected", [
    (reputation_body("deliverable"), "verified"),
    (reputation_body("undeliverable"), "invalid"),
    (reputation_body("deliverable", catch_all=True), "risky"),
    (reputation_body("unknown", catch_all=True), "risky"),
    (reputation_body("deliverable", disposable=True), "invalid"),
    (reputation_body("unknown"), None),
    ({"email_deliverability": "deliverable"}, None),
    ({"email_deliverability": {"status": "deliverable"}, "email_quality": {"is_catchall": "true"}}, "verified"),
])
@pytest.mark.asyncio
async def test_abstract_verdicts_from_a_reputation_key(monkeypatch, body, expected):
    assert await graded(monkeypatch, ef.verify_abstract, abstract_by_host(reputation=body)) == expected


@pytest.mark.asyncio
async def test_abstract_asks_reputation_first_and_stops_when_it_answers(monkeypatch):
    seen = mock_http(monkeypatch, abstract_by_host(reputation=reputation_body("unknown"),
                                                  validation=abstract_body("DELIVERABLE")))
    assert await ef.verify_abstract(ADDRESS, "ab-test-key") is None   # inconclusive, not retried
    assert [r.url.host for r in seen] == ["emailreputation.abstractapi.com"]
    url = seen[0].url
    assert url.path == "/v1/"
    assert url.params["api_key"] == "ab-test-key" and url.params["email"] == ADDRESS


@pytest.mark.asyncio
async def test_abstract_falls_back_to_validation_for_an_older_key(monkeypatch):
    seen = mock_http(monkeypatch, abstract_by_host(validation=abstract_body("DELIVERABLE")))
    assert await ef.verify_abstract(ADDRESS, "ab-test-key") == {"status": "valid", "catch_all": False}
    assert [r.url.host for r in seen] == ["emailreputation.abstractapi.com",
                                         "emailvalidation.abstractapi.com"]
    assert seen[1].url.params["api_key"] == "ab-test-key"


@pytest.mark.asyncio
async def test_abstract_calls_are_spaced_for_its_free_plan(monkeypatch):
    mock_http(monkeypatch, answer(abstract_body("DELIVERABLE")))
    monkeypatch.setattr(ef, "ABSTRACT_MIN_INTERVAL", 0.3)
    monkeypatch.setattr(ef, "_abstract_last_call", time.monotonic())
    started = time.monotonic()
    await ef.verify_abstract(ADDRESS, "ab-test-key")
    assert time.monotonic() - started >= 0.25


# ── MailboxValidator ──

def mbv_body(**changes):
    body = {
        "email_address": ADDRESS, "base_email_address": ADDRESS, "domain": "acme.example.test",
        "is_free": False, "is_syntax": True, "is_domain": True, "is_smtp": True,
        "is_verified": True, "is_server_down": False, "is_greylisted": False,
        "is_disposable": False, "is_suppressed": False, "is_role": False,
        "is_high_risk": False, "is_catchall": False, "mailboxvalidator_score": 0.9,
        "time_taken": 0.4, "status": True, "credits_available": 299,
    }
    body.update(changes)
    return body


@pytest.mark.parametrize("body, expected", [
    (mbv_body(), "verified"),
    (mbv_body(is_verified=False, status=False), "invalid"),
    (mbv_body(is_catchall=True, status=False), "risky"),
    # A catch-all server confirms any mailbox: never valid.
    (mbv_body(is_catchall=True), "risky"),
    # Catch-all not ruled out (null): the confirmation means nothing.
    (mbv_body(is_catchall=None), None),
    (mbv_body(is_disposable=True), "invalid"),
    (mbv_body(is_syntax=False, is_domain=None, is_smtp=None, is_verified=None, status=False), "invalid"),
    (mbv_body(is_domain=False, is_smtp=None, is_verified=None, status=False), "invalid"),
    (mbv_body(is_server_down=True, is_verified=None, status=False), None),
    (mbv_body(is_greylisted=True, is_verified=False, status=False), None),
    (mbv_body(is_smtp=False, is_verified=None, is_catchall=None, status=False), None),
    # Syntax and MX alone are never valid.
    (mbv_body(is_smtp=None, is_verified=None, is_catchall=None), None),
    # Its own verdict says no (a role or high-risk address): not valid.
    (mbv_body(status=False, is_role=True), None),
    (mbv_body(is_verified=False, status=True), None),
    # Only JSON booleans count: v1-style text is not read.
    (mbv_body(is_smtp="True", is_verified="True", is_catchall="False", status="True"), None),
    ({"error": {"error_code": 10004, "error_message": "Insufficient credits."}}, None),
])
@pytest.mark.asyncio
async def test_mailboxvalidator_verdicts(monkeypatch, body, expected):
    assert await graded(monkeypatch, ef.verify_mailboxvalidator, answer(body)) == expected


@pytest.mark.asyncio
async def test_mailboxvalidator_request_shape(monkeypatch):
    seen = mock_http(monkeypatch, answer(mbv_body()))
    await ef.verify_mailboxvalidator(ADDRESS, "mbv-test-key")
    url = seen[0].url
    assert seen[0].method == "GET"
    assert (url.scheme, url.host, url.path) == ("https", "api.mailboxvalidator.com", "/v2/validation/single")
    assert url.params["key"] == "mbv-test-key" and url.params["email"] == ADDRESS
    assert url.params["format"] == "json"


@pytest.mark.parametrize("code, message", [(10001, "API key not found."), (10004, "Insufficient credits.")])
@pytest.mark.asyncio
async def test_mailboxvalidator_bad_key_or_no_credits_is_inconclusive(monkeypatch, code, message):
    body = {"error": {"error_code": code, "error_message": message}}
    assert await graded(monkeypatch, ef.verify_mailboxvalidator, answer(body, status=401)) is None


# ── Sniffmail ──

def sniff_body(reachable, deliverable=True, **changes):
    body = {"email": ADDRESS, "is_valid": reachable == "safe", "is_reachable": reachable,
            "is_disposable": False, "is_role_account": False, "is_deliverable": deliverable,
            "mx_valid": True, "reason": "x"}
    body.update(changes)
    return body


@pytest.mark.parametrize("body, expected", [
    (sniff_body("safe"), "verified"),
    (sniff_body("invalid", deliverable=False), "invalid"),
    # "risky" is a catch-all, a role address or a full inbox: can't tell which.
    (sniff_body("risky"), None),
    (sniff_body("risky", is_role_account=True), None),
    # ...unless a catch-all is flagged in so many words.
    (sniff_body("risky", smtp={"is_catch_all": True, "can_connect_smtp": True}), "risky"),
    (sniff_body("risky", is_catch_all=True), "risky"),
    (sniff_body("risky", smtp={"is_catch_all": False}), None),
    (sniff_body("safe", is_disposable=True), "invalid"),
    (sniff_body("unknown", deliverable=False), None),
    (sniff_body("unverified", deliverable=False), None),
    # Safe needs the mailbox itself confirmed.
    (sniff_body("safe", deliverable=False), None),
    ({"is_reachable": "safe"}, None),
    ({"is_reachable": "unverified", "mx_valid": True}, None),
    ({"error": "Unauthorized", "message": "Invalid API key"}, None),
])
@pytest.mark.asyncio
async def test_sniffmail_verdicts(monkeypatch, body, expected):
    assert await graded(monkeypatch, ef.verify_sniffmail, answer(body)) == expected


@pytest.mark.asyncio
async def test_sniffmail_request_shape(monkeypatch):
    seen = mock_http(monkeypatch, answer(sniff_body("safe")))
    await ef.verify_sniffmail(ADDRESS, "sniff_test-key")
    request = seen[0]
    assert request.method == "POST" and str(request.url) == "https://api.sniffmail.io/verify"
    assert request.headers["Authorization"] == "Bearer sniff_test-key"
    assert json.loads(request.content) == {"email": ADDRESS}


@pytest.mark.parametrize("status, body", [
    (401, {"error": "Unauthorized", "message": "Invalid API key"}),
    (403, {"error": "Forbidden", "message": "Usage limit exceeded"}),
    (429, {"error": "Too Many Requests", "message": "Rate limit exceeded"}),
])
@pytest.mark.asyncio
async def test_sniffmail_bad_key_or_used_up_is_inconclusive(monkeypatch, status, body):
    body = {**sniff_body("safe"), **body}
    assert await graded(monkeypatch, ef.verify_sniffmail, answer(body, status=status)) is None


# ── emailvalidation.io ──

def evio_body(state, reason, status, sub_status=None, smtp=True, **changes):
    body = {"email": ADDRESS, "user": "jane.doe", "tag": "", "domain": "acme.example.test",
            "smtp_check": smtp, "mx_found": True, "did_you_mean": "", "role": False,
            "disposable": False, "score": 0.8, "state": state, "reason": reason, "free": False,
            "format_valid": True, "catch_all": None, "catch_all_resolution": None,
            "bounce_probability": None, "status": status, "sub_status": sub_status,
            "recommended_action": "send"}
    body.update(changes)
    return body


@pytest.mark.parametrize("body, expected", [
    (evio_body("deliverable", "valid_mailbox", "valid"), "verified"),
    # A role mailbox exists too.
    (evio_body("deliverable", "low_quality", "valid", "role_based", role=True), "verified"),
    (evio_body("undeliverable", "invalid_mailbox", "invalid", "mailbox_not_found", smtp=False), "invalid"),
    (evio_body("undeliverable", "invalid_mx", "invalid", "no_dns_entries", smtp=False), "invalid"),
    # A catch-all comes back "deliverable": that is not valid.
    (evio_body("deliverable", "low_deliverability", "catch_all", "accept_all",
               catch_all_resolution={"verdict": "unresolved"}), "risky"),
    (evio_body("deliverable", "valid_mailbox", "valid", catch_all=True), "risky"),
    (evio_body("deliverable", "low_deliverability", "do_not_mail", "disposable", disposable=True), "invalid"),
    (evio_body("deliverable", "low_deliverability", "risky", "mailbox_full"), None),
    (evio_body("unknown", "timeout", "unknown", "timeout_exceeded", smtp=False), None),
    (evio_body("unknown", "no_connect", "unknown", "failed_smtp_connection", smtp=False), None),
    # Valid needs the SMTP check to have passed.
    (evio_body("deliverable", "valid_mailbox", "valid", smtp=False), None),
    ({"status": "valid"}, None),
    ({"state": "deliverable", "reason": "valid_mailbox", "smtp_check": True}, None),
    ({"message": "Invalid API key", "error": {"code": "invalid_api_key"}}, None),
])
@pytest.mark.asyncio
async def test_emailvalidationio_verdicts(monkeypatch, body, expected):
    assert await graded(monkeypatch, ef.verify_emailvalidationio, answer(body)) == expected


@pytest.mark.asyncio
async def test_emailvalidationio_request_shape(monkeypatch):
    seen = mock_http(monkeypatch, answer(evio_body("deliverable", "valid_mailbox", "valid")))
    await ef.verify_emailvalidationio(ADDRESS, "evio-test-key")
    request, url = seen[0], seen[0].url
    assert request.method == "GET"
    assert (url.scheme, url.host, url.path) == ("https", "api.emailvalidation.io", "/v1/info")
    assert url.params["email"] == ADDRESS
    # Its own SMTP timeout stays under Harvey's read timeout.
    assert int(url.params["timeout"]) < ef.API_TIMEOUT.read
    # The key goes in the header; catch_all=1 needs a paid plan, so it isn't asked.
    assert request.headers["apikey"] == "evio-test-key"
    assert "evio-test-key" not in str(url) and "catch_all" not in url.params


@pytest.mark.parametrize("status, code", [(401, "invalid_api_key"), (429, "quota_exceeded"),
                                          (429, "rate_limit_exceeded"), (403, "plan_upgrade_required")])
@pytest.mark.asyncio
async def test_emailvalidationio_bad_key_or_used_up_is_inconclusive(monkeypatch, status, code):
    body = {**evio_body("deliverable", "valid_mailbox", "valid"),
            "message": "no", "error": {"code": code}}
    assert await graded(monkeypatch, ef.verify_emailvalidationio, answer(body, status=status)) is None


# ── BounceZero ──

def bz_body(classification, internal, mailbox_verified=True, not_catch_all=True,
            not_disposable=True, catch_all_unresolved=None, refunded=None):
    return {
        "email": ADDRESS, "score": 98, "classification": classification,
        "is_deliverable": classification == "valid", "risk_level": "low",
        "checks": {"syntax": True, "domain_valid": True, "not_disposable": not_disposable,
                   "not_catch_all": not_catch_all, "mailbox_verified": mailbox_verified},
        "metadata": {"internal_classification": internal, "refunded": refunded,
                     "catch_all_unresolved": catch_all_unresolved, "response_time_ms": 412},
    }


@pytest.mark.parametrize("body, expected", [
    (bz_body("valid", "verified"), "verified"),
    # The trap: BounceZero reports a catch-all as "valid". Never valid here.
    (bz_body("valid", "catch_all", mailbox_verified=False, not_catch_all=False,
             catch_all_unresolved=True), "risky"),
    ({"classification": "valid", "is_deliverable": True,
      "metadata": {"internal_classification": "catch_all"}}, "risky"),
    (bz_body("valid", "verified", catch_all_unresolved=True), "risky"),
    (bz_body("valid", "verified", not_catch_all=False), "risky"),
    # Signals without a mailbox confirmation are not enough.
    (bz_body("valid", "likely_valid", mailbox_verified=False), None),
    (bz_body("valid", "verified", mailbox_verified=False), None),
    ({"classification": "valid", "is_deliverable": True, "score": 98}, None),
    (bz_body("invalid", "invalid", mailbox_verified=False), "invalid"),
    (bz_body("invalid", "disposable", not_disposable=False), "invalid"),
    (bz_body("invalid", "spamtrap"), "invalid"),
    (bz_body("invalid", "risky"), None),
    # Couldn't verify: reported as invalid with the credit refunded.
    (bz_body("invalid", "unknown", mailbox_verified=False, refunded=True), None),
    (bz_body("invalid", "invalid", mailbox_verified=False, refunded=True), None),
    (bz_body("invalid", "error"), None),
    ({"classification": "invalid"}, None),
])
@pytest.mark.asyncio
async def test_bouncezero_verdicts(monkeypatch, body, expected):
    assert await graded(monkeypatch, ef.verify_bouncezero, answer(body)) == expected


@pytest.mark.asyncio
async def test_bouncezero_request_shape(monkeypatch):
    seen = mock_http(monkeypatch, answer(bz_body("valid", "verified")))
    await ef.verify_bouncezero(ADDRESS, "bz_live_test-key")
    request = seen[0]
    assert request.method == "POST" and str(request.url) == "https://api.bouncezero.io/api/v1/verify"
    assert request.headers["X-API-Key"] == "bz_live_test-key"
    assert json.loads(request.content) == {"email": ADDRESS}


@pytest.mark.parametrize("status, message", [(401, "Invalid API key"), (402, "Insufficient credits"),
                                             (403, "Key disabled"), (429, "Rate limited")])
@pytest.mark.asyncio
async def test_bouncezero_bad_key_or_no_credits_is_inconclusive(monkeypatch, status, message):
    body = {**bz_body("valid", "verified"), "error": message, "code": status}
    assert await graded(monkeypatch, ef.verify_bouncezero, answer(body, status=status)) is None


# ── MyEmailVerifier ──

def mev_body(status, catch_all="false", disposable="false", greylisted="false"):
    return {"Address": ADDRESS, "catch_all": catch_all, "Status": status,
            "Disposable_Domain": disposable, "Role_Based": "false", "Free_Domain": "false",
            "Greylisted": greylisted, "Diagnosis": "x"}


@pytest.mark.parametrize("body, expected", [
    (mev_body("Valid"), "verified"),
    (mev_body("Invalid"), "invalid"),
    (mev_body("Catch All", catch_all="true"), "risky"),
    (mev_body("Catch All"), "risky"),
    (mev_body("Valid", catch_all="true"), "risky"),
    (mev_body("Valid", catch_all=False), "verified"),   # a JSON boolean reads the same
    (mev_body("Valid", disposable="true"), "invalid"),
    (mev_body("Unknown"), None),
    (mev_body("Grey-listed", greylisted="true"), None),
    (mev_body("Valid", greylisted="true"), None),
    # Catch-all not ruled out: not valid.
    ({"Address": ADDRESS, "Status": "Valid"}, None),
    (mev_body("Valid", catch_all="maybe"), None),
    ({"status": False, "msg": "Invalid API Key"}, None),
    ({"status": False, "msg": "You do not have enough credits."}, None),
    ({"status": False, "msg": "Unauthorized Request"}, None),
])
@pytest.mark.asyncio
async def test_myemailverifier_verdicts(monkeypatch, body, expected):
    assert await graded(monkeypatch, ef.verify_myemailverifier, answer(body)) == expected


@pytest.mark.asyncio
async def test_myemailverifier_request_shape(monkeypatch):
    seen = mock_http(monkeypatch, answer(mev_body("Valid")))
    await ef.verify_myemailverifier("jane.doe+news@acme.example.test", "mev-test-key")
    request, url = seen[0], seen[0].url
    assert request.method == "GET"
    assert (url.scheme, url.host) == ("https", "client.myemailverifier.com")
    assert url.raw_path == b"/verifier/validate_single/jane.doe%2Bnews@acme.example.test/mev-test-key"
    assert not url.query


@pytest.mark.asyncio
async def test_myemailverifier_key_cannot_escape_its_path_segment(monkeypatch):
    seen = mock_http(monkeypatch, answer(mev_body("Valid")))
    await ef.verify_myemailverifier(ADDRESS, "k/../e?y#1")
    url = seen[0].url
    assert url.raw_path == f"/verifier/validate_single/{ADDRESS}/k%2F..%2Fe%3Fy%231".encode()
    assert not url.query and not url.fragment


@pytest.mark.parametrize("status, body", [
    (200, {"status": False, "msg": "You do not have enough credits."}),
    (200, {"status": False, "msg": "Invalid API Key"}),
    (429, {"status": False, "message": "Too many requests. Please try again later.", "retryAfter": 60}),
])
@pytest.mark.asyncio
async def test_myemailverifier_bad_key_or_no_credits_is_inconclusive(monkeypatch, status, body):
    assert await graded(monkeypatch, ef.verify_myemailverifier, answer(body, status=status)) is None


# ── Keys that travel in the URL never reach the log ──

@pytest.mark.parametrize("verify, body", [
    (ef.verify_myemailverifier, mev_body("Valid")),       # key in the path
    (ef.verify_mailboxvalidator, mbv_body()),             # key in the query
])
@pytest.mark.parametrize("outcome", ["ok", "http_error", "network_error"])
@pytest.mark.asyncio
async def test_a_key_in_the_url_never_shows_in_the_log(monkeypatch, caplog, verify, body, outcome):
    key = "s3cr3t-test-key-in-url"

    def handler(request):
        if outcome == "network_error":
            raise httpx.ConnectError(f"cannot reach {request.url}")
        return httpx.Response(200 if outcome == "ok" else 500, json=body)

    caplog.set_level(logging.DEBUG)
    seen = mock_http(monkeypatch, handler)
    await verify(ADDRESS, key)
    assert key in str(seen[0].url)          # it really was in the URL...
    assert key not in caplog.text           # ...and never in the log
    if outcome != "network_error":
        assert "<redacted>" in caplog.text  # httpx logged the request, redacted


# ── Paced for their free plans' per-minute limits ──

@pytest.mark.parametrize("service, verify, body", [
    ("sniffmail", ef.verify_sniffmail, sniff_body("safe")),
    ("emailvalidationio", ef.verify_emailvalidationio, evio_body("deliverable", "valid_mailbox", "valid")),
    ("bouncezero", ef.verify_bouncezero, bz_body("valid", "verified")),
    ("myemailverifier", ef.verify_myemailverifier, mev_body("Valid")),
])
@pytest.mark.asyncio
async def test_rate_limited_free_plans_are_spaced(monkeypatch, service, verify, body):
    mock_http(monkeypatch, answer(body))
    monkeypatch.setitem(ef.VERIFIER_MIN_INTERVALS, service, 0.3)
    monkeypatch.setitem(ef._verifier_last_calls, service, time.monotonic())
    started = time.monotonic()
    await verify(ADDRESS, "test-key")
    assert time.monotonic() - started >= 0.25


def test_the_shipped_intervals_respect_the_per_minute_limits():
    shipped = SHIPPED_INTERVALS
    assert shipped["sniffmail"] >= 6 and shipped["emailvalidationio"] >= 6     # 10 a minute
    assert shipped["bouncezero"] >= 2 and shipped["myemailverifier"] >= 2      # 30 a minute


# ── Failures are inconclusive, never raised ──

VERIFIERS = [ef.verify_bounceless, ef.verify_emailverify, ef.verify_getprospect, ef.verify_abstract,
             ef.verify_mailboxvalidator, ef.verify_sniffmail, ef.verify_emailvalidationio,
             ef.verify_bouncezero, ef.verify_myemailverifier]


@pytest.mark.parametrize("verify", VERIFIERS)
@pytest.mark.parametrize("status", [202, 400, 401, 402, 408, 422, 429, 500, 503])
@pytest.mark.asyncio
async def test_http_errors_are_inconclusive(monkeypatch, verify, status):
    body = {"error": {"code": "x", "message": "no"}, "status": "valid",
            "presend": {"verdict": "DELIVERABLE"}, "deliverability": "DELIVERABLE"}
    assert await graded(monkeypatch, verify, answer(body, status=status)) is None


@pytest.mark.parametrize("verify", VERIFIERS)
@pytest.mark.parametrize("raw", [b"", b"not json", b"<html>busy</html>", b"[1, 2", b"null", b"7"])
@pytest.mark.asyncio
async def test_garbage_bodies_are_inconclusive(monkeypatch, verify, raw):
    assert await graded(monkeypatch, verify, answer(raw=raw)) is None


@pytest.mark.parametrize("verify", VERIFIERS)
@pytest.mark.parametrize("error", [httpx.ConnectError("refused"), httpx.ReadTimeout("slow"),
                                   ValueError("odd header")])
@pytest.mark.asyncio
async def test_transport_failures_are_inconclusive(monkeypatch, verify, error):
    def boom(request):
        raise error
    assert await graded(monkeypatch, verify, boom) is None


# ── Monthly allowances ──

@pytest.mark.asyncio
async def test_allowances_default_to_the_free_tiers(state):
    expected = {"bounceless": 1000, "emailverify": 100, "getprospect": 100, "abstract": 100,
                "mailboxvalidator": 300, "sniffmail": 500, "emailvalidationio": 100,
                "bouncezero": 100, "myemailverifier": 100}
    assert {s: free for s, (_, free) in ef.VERIFIER_ALLOWANCES.items()} == expected
    for service, free in expected.items():
        assert await ef.verifier_checks_remaining(service, state) == free
    assert await ef.record_verifier_check("bounceless", state)
    assert await ef.verifier_checks_remaining("bounceless", state) == 999
    assert await ef.verifier_checks_remaining("abstract", state) == 100   # counted apart


@pytest.mark.parametrize("raw, cap", [("250", 250), (" 5 ", 5), ("0", 0), ("-3", 0), ("lots", 100)])
@pytest.mark.asyncio
async def test_allowance_follows_the_env_setting(state, monkeypatch, raw, cap):
    monkeypatch.setenv("GETPROSPECT_MONTHLY_CHECKS", raw)
    assert await ef.verifier_checks_remaining("getprospect", state) == cap


@pytest.mark.asyncio
async def test_without_a_state_store_nothing_is_spent():
    assert await ef.verifier_checks_remaining("bounceless", None) == 0
    assert await ef.record_verifier_check("bounceless", None) is False


def test_the_allowance_settings_are_named_after_the_service():
    for service, (setting, _) in ef.VERIFIER_ALLOWANCES.items():
        assert setting == f"{service.upper()}_MONTHLY_CHECKS"


# ── Optional daily allowances ──

def frozen_utc(year, month, day, hour=12, minute=0):
    class Frozen(datetime):
        @classmethod
        def now(cls, tz=None):
            assert tz is timezone.utc   # counted per UTC day, never local time
            return cls(year, month, day, hour, minute, tzinfo=timezone.utc)
    return Frozen


@pytest.mark.parametrize("raw", [None, "", "0", "lots", "-4"])
@pytest.mark.asyncio
async def test_no_daily_setting_means_no_daily_cap(state, monkeypatch, raw):
    if raw is not None:
        monkeypatch.setenv("MYEMAILVERIFIER_DAILY_CHECKS", raw)
    monkeypatch.setattr(ef, "datetime", frozen_utc(2026, 11, 3))
    for _ in range(5):
        assert await ef.record_verifier_check("myemailverifier", state)
    assert await ef.verifier_checks_remaining("myemailverifier", state) == 95
    # Nothing counted per day without a daily allowance.
    assert await state.get_setting("myemailverifier_check_count:2026-11-03") == ""
    assert await state.get_setting("myemailverifier_check_count:2026-11") == "5"


@pytest.mark.asyncio
async def test_a_daily_allowance_is_counted_per_utc_day(state, monkeypatch):
    monkeypatch.setenv("MYEMAILVERIFIER_DAILY_CHECKS", "2")
    monkeypatch.setattr(ef, "datetime", frozen_utc(2026, 11, 3, 23, 59))
    assert await ef.verifier_checks_remaining("myemailverifier", state) == 2
    assert await ef.record_verifier_check("myemailverifier", state)
    assert await ef.record_verifier_check("myemailverifier", state)
    assert await ef.verifier_checks_remaining("myemailverifier", state) == 0
    assert await state.get_setting("myemailverifier_check_count:2026-11-03") == "2"
    # A minute later it is another UTC day: today's allowance is back, and
    # the month still remembers both checks.
    monkeypatch.setattr(ef, "datetime", frozen_utc(2026, 11, 4, 0, 0))
    assert await ef.verifier_checks_remaining("myemailverifier", state) == 2
    assert await state.get_setting("myemailverifier_check_count:2026-11") == "2"


@pytest.mark.asyncio
async def test_the_daily_and_monthly_allowances_both_hold(state, monkeypatch):
    monkeypatch.setenv("SNIFFMAIL_DAILY_CHECKS", "2")
    monkeypatch.setenv("SNIFFMAIL_MONTHLY_CHECKS", "3")
    monkeypatch.setattr(ef, "datetime", frozen_utc(2026, 11, 3))
    await ef.record_verifier_check("sniffmail", state)
    await ef.record_verifier_check("sniffmail", state)
    assert await ef.verifier_checks_remaining("sniffmail", state) == 0      # the day is used
    monkeypatch.setattr(ef, "datetime", frozen_utc(2026, 11, 4))
    assert await ef.verifier_checks_remaining("sniffmail", state) == 1      # the month has one left
    await ef.record_verifier_check("sniffmail", state)
    assert await ef.verifier_checks_remaining("sniffmail", state) == 0      # the month is used
    monkeypatch.setattr(ef, "datetime", frozen_utc(2026, 12, 1))
    assert await ef.verifier_checks_remaining("sniffmail", state) == 2      # new month, new day


@pytest.mark.parametrize("service", COUNTED)
@pytest.mark.asyncio
async def test_every_counted_verifier_takes_a_daily_allowance(state, monkeypatch, service):
    monkeypatch.setenv(f"{service.upper()}_DAILY_CHECKS", "1")
    assert await ef.verifier_checks_remaining(service, state) == 1
    await ef.record_verifier_check(service, state)
    assert await ef.verifier_checks_remaining(service, state) == 0


class FakeCalls:
    """Stand-ins for every verifier, recording the order they're asked in."""

    def __init__(self, monkeypatch, answers=None):
        self.calls = []
        answers = answers or {}
        for name in ("zerobounce", "reoon", "hunter") + COUNTED:
            monkeypatch.setattr(ef, f"verify_{name}", self._fake(name, answers.get(name)))

        async def smtp(email):
            self.calls.append("smtp")
            return answers.get("smtp")

        monkeypatch.setattr(ef, "verify_email_smtp", smtp)
        monkeypatch.setattr(ef, "_hunter_api_key", lambda: "hunter-test-key")

    def _fake(self, name, verdict):
        async def verify(email, key):
            self.calls.append(name)
            return verdict
        return verify


ALL_KEYS = Keys(reoon="r", zerobounce="z", **{s: s[0] for s in COUNTED})


def test_counted_channels_run_in_this_order():
    channels = ef._counted_channels(ALL_KEYS)
    assert [service for service, _, _ in channels] == [
        "bounceless", "emailverify", "getprospect", "abstract",
        "mailboxvalidator", "sniffmail", "emailvalidationio", "bouncezero", "myemailverifier"]
    assert [verify.__name__ for _, verify, _ in channels] == [f"verify_{s}" for s in COUNTED]
    assert list(ef.VERIFIER_ALLOWANCES) == list(COUNTED)


@pytest.mark.asyncio
async def test_big_providers_try_the_counted_tiers_last(state, monkeypatch):
    fake = FakeCalls(monkeypatch)
    assert await ef._verify_candidate(ADDRESS, "google", ALL_KEYS, state=state) == (None, False)
    assert fake.calls == ["zerobounce", "reoon", "hunter", *COUNTED]


@pytest.mark.asyncio
async def test_small_hosts_try_the_counted_tiers_after_smtp_and_hunter(state, monkeypatch):
    fake = FakeCalls(monkeypatch)
    assert await ef._verify_candidate(ADDRESS, "other", ALL_KEYS, state=state) == (None, False)
    assert fake.calls == ["reoon", "smtp", "hunter", *COUNTED]


@pytest.mark.asyncio
async def test_the_first_conclusive_verdict_wins_and_later_ones_are_not_spent(state, monkeypatch):
    fake = FakeCalls(monkeypatch, {"emailverify": {"status": "valid", "catch_all": False}})
    assert await ef._verify_candidate(ADDRESS, "microsoft", ALL_KEYS, state=state) == ("verified", False)
    assert fake.calls[-1] == "emailverify"
    assert await ef.verifier_checks_remaining("bounceless", state) == 999
    assert await ef.verifier_checks_remaining("emailverify", state) == 99
    # Never asked, never counted.
    for service in COUNTED[2:]:
        assert await ef.verifier_checks_remaining(service, state) == ef.VERIFIER_ALLOWANCES[service][1]


@pytest.mark.asyncio
async def test_a_counted_catch_all_is_risky_and_seen(state, monkeypatch):
    FakeCalls(monkeypatch, {"abstract": {"status": "catch_all", "catch_all": True}})
    assert await ef._verify_candidate(ADDRESS, "other", ALL_KEYS, state=state) == ("risky", True)


@pytest.mark.asyncio
async def test_an_earlier_answer_spends_none_of_the_counted_tiers(state, monkeypatch):
    fake = FakeCalls(monkeypatch, {"reoon": {"status": "safe", "catch_all": False}})
    assert await ef._verify_candidate(ADDRESS, "google", ALL_KEYS, state=state) == ("verified", False)
    assert fake.calls == ["zerobounce", "reoon"]
    for service in COUNTED:
        assert await ef.verifier_checks_remaining(service, state) == ef.VERIFIER_ALLOWANCES[service][1]


@pytest.mark.asyncio
async def test_only_verifiers_with_a_key_are_asked_or_counted(state, monkeypatch):
    fake = FakeCalls(monkeypatch)
    monkeypatch.setattr(ef, "_hunter_api_key", lambda: "")
    await ef._verify_candidate(ADDRESS, "google", Keys(getprospect="g"), state=state)
    assert fake.calls == ["getprospect"]
    assert await ef.verifier_checks_remaining("getprospect", state) == 99
    assert await ef.verifier_checks_remaining("bounceless", state) == 1000


@pytest.mark.asyncio
async def test_a_used_up_allowance_skips_that_verifier(state, monkeypatch):
    monkeypatch.setenv("BOUNCELESS_MONTHLY_CHECKS", "1")
    fake = FakeCalls(monkeypatch)
    keys = Keys(bounceless="b", abstract="a")
    await ef._verify_candidate(ADDRESS, "google", keys, state=state)
    await ef._verify_candidate(ADDRESS, "google", keys, state=state)
    hunter_calls = fake.calls.count("hunter")
    assert [c for c in fake.calls if c != "hunter"] == ["bounceless", "abstract", "abstract"]
    assert hunter_calls == 2
    assert await ef.verifier_checks_remaining("bounceless", state) == 0
    assert await ef.verifier_checks_remaining("abstract", state) == 98


@pytest.mark.asyncio
async def test_a_zero_allowance_never_calls(state, monkeypatch):
    monkeypatch.setenv("ABSTRACT_MONTHLY_CHECKS", "0")
    fake = FakeCalls(monkeypatch)
    monkeypatch.setattr(ef, "_hunter_api_key", lambda: "")
    await ef._verify_candidate(ADDRESS, "other", Keys(abstract="a"), state=state)
    assert fake.calls == ["smtp"]


@pytest.mark.asyncio
async def test_without_state_the_counted_tiers_are_not_used(monkeypatch):
    fake = FakeCalls(monkeypatch)
    monkeypatch.setattr(ef, "_hunter_api_key", lambda: "")
    await ef._verify_candidate(ADDRESS, "google", Keys(bounceless="b", reoon="r"))
    assert fake.calls == ["reoon"]


def frozen_at(year, month, day=15):
    class Frozen(datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(year, month, day, 12, 0, tzinfo=timezone.utc)
    return Frozen


@pytest.mark.asyncio
async def test_the_allowance_resets_when_the_month_turns(state, monkeypatch):
    monkeypatch.setenv("EMAILVERIFY_MONTHLY_CHECKS", "1")
    fake = FakeCalls(monkeypatch)
    monkeypatch.setattr(ef, "_hunter_api_key", lambda: "")
    keys = Keys(emailverify="e")
    monkeypatch.setattr(ef, "datetime", frozen_at(2026, 10, 31))
    await ef._verify_candidate(ADDRESS, "google", keys, state=state)
    await ef._verify_candidate(ADDRESS, "google", keys, state=state)
    assert fake.calls == ["emailverify"]
    assert await ef.verifier_checks_remaining("emailverify", state) == 0
    monkeypatch.setattr(ef, "datetime", frozen_at(2026, 11, 1))
    assert await ef.verifier_checks_remaining("emailverify", state) == 1
    await ef._verify_candidate(ADDRESS, "google", keys, state=state)
    assert fake.calls == ["emailverify", "emailverify"]


@pytest.mark.asyncio
async def test_a_used_up_day_skips_that_verifier_until_the_next_utc_day(state, monkeypatch):
    monkeypatch.setenv("MYEMAILVERIFIER_DAILY_CHECKS", "1")
    fake = FakeCalls(monkeypatch)
    monkeypatch.setattr(ef, "_hunter_api_key", lambda: "")
    keys = Keys(myemailverifier="m", bouncezero="b")
    monkeypatch.setattr(ef, "datetime", frozen_utc(2026, 11, 3))
    await ef._verify_candidate(ADDRESS, "google", keys, state=state)
    await ef._verify_candidate(ADDRESS, "google", keys, state=state)
    assert fake.calls == ["bouncezero", "myemailverifier", "bouncezero"]
    monkeypatch.setattr(ef, "datetime", frozen_utc(2026, 11, 4))
    await ef._verify_candidate(ADDRESS, "google", keys, state=state)
    assert fake.calls[-2:] == ["bouncezero", "myemailverifier"]
    assert await ef.verifier_checks_remaining("myemailverifier", state) == 0   # today's one is used
    assert await state.get_setting("myemailverifier_check_count:2026-11") == "2"


# ── End to end through find_email and a published address ──

@pytest.mark.asyncio
async def test_find_email_verifies_with_a_counted_tier_alone(state, monkeypatch):
    async def google_mx(domain):
        return "aspmx.l.google.com"

    monkeypatch.setattr(ef, "get_mx_host", google_mx)
    monkeypatch.setattr(ef, "_hunter_api_key", lambda: "")
    seen = mock_http(monkeypatch, answer({"presend": {"verdict": "DELIVERABLE"}}))
    result = await ef.find_email("Jane", "Doe", "acme.example.test",
                                 env=Keys(bounceless="b"), state=state)
    assert result.status == "verified" and result.email == ADDRESS
    assert len(seen) == 1
    assert await ef.verifier_checks_remaining("bounceless", state) == 999


@pytest.mark.asyncio
async def test_published_address_counts_against_the_allowance(state, monkeypatch):
    async def mx(domain):
        return "mx.acme.example.test"

    async def no_smtp(email):
        return None

    monkeypatch.setattr(ef, "get_mx_host", mx)
    monkeypatch.setattr(ef, "verify_email_smtp", no_smtp)
    monkeypatch.setattr(ef, "_hunter_api_key", lambda: "")
    mock_http(monkeypatch, answer({"success": True, "data": {"status": "invalid"}}))
    status = await ef.verify_published_address("office@acme.example.test",
                                               env=Keys(getprospect="g"), state=state)
    assert status == "invalid"
    assert await ef.verifier_checks_remaining("getprospect", state) == 99


@pytest.mark.asyncio
async def test_find_email_never_verifies_a_bouncezero_catch_all(state, monkeypatch):
    # BounceZero calls a catch-all "valid"; Harvey files it as risky.
    async def google_mx(domain):
        return "aspmx.l.google.com"

    monkeypatch.setattr(ef, "get_mx_host", google_mx)
    monkeypatch.setattr(ef, "_hunter_api_key", lambda: "")
    mock_http(monkeypatch, answer(bz_body("valid", "catch_all", mailbox_verified=False,
                                          not_catch_all=False, catch_all_unresolved=True)))
    keys = Keys(bouncezero="b")
    assert await ef._verify_candidate(ADDRESS, "google", keys, state=state) == ("risky", True)
    # On a guessed pattern a catch-all is only a guess; never verified either way.
    result = await ef.find_email("Jane", "Doe", "acme.example.test", env=keys, state=state)
    assert result.status in ("risky", "guess")
    assert await ef.verifier_checks_remaining("bouncezero", state) == 98


# ── A new key alone counts as having a verifier ──

def test_load_env_reads_the_new_keys(monkeypatch):
    monkeypatch.setattr("harvey.config.load_dotenv", lambda *a, **k: False)
    monkeypatch.setenv("BOUNCELESS_API_KEY", "  blc.test  ")
    monkeypatch.setenv("EMAILVERIFY_API_KEY", "ev-test")
    monkeypatch.setenv("GETPROSPECT_API_KEY", "gp-test")
    monkeypatch.setenv("ABSTRACT_API_KEY", "ab-test")
    monkeypatch.setenv("MAILBOXVALIDATOR_API_KEY", " mbv-test ")
    monkeypatch.setenv("SNIFFMAIL_API_KEY", "sniff_test")
    monkeypatch.setenv("EMAILVALIDATIONIO_API_KEY", "evio-test")
    monkeypatch.setenv("BOUNCEZERO_API_KEY", "bz_live_test")
    monkeypatch.setenv("MYEMAILVERIFIER_API_KEY", "mev-test")
    env = load_env()
    assert (env.bounceless_api_key, env.emailverify_api_key,
            env.getprospect_api_key, env.abstract_api_key) == ("blc.test", "ev-test", "gp-test", "ab-test")
    assert (env.mailboxvalidator_api_key, env.sniffmail_api_key, env.emailvalidationio_api_key,
            env.bouncezero_api_key, env.myemailverifier_api_key) == (
        "mbv-test", "sniff_test", "evio-test", "bz_live_test", "mev-test")
    # Every counted verifier reads its key from the env attribute _counted_channels uses.
    assert [s for s, _, _ in ef._counted_channels(env)] == list(COUNTED)


@pytest.fixture
def no_verifier_env(monkeypatch):
    for key in dash.VERIFIER_KEYS:
        monkeypatch.delenv(key, raising=False)


def _verifier_step(client):
    checks = client.get("/api/setup-status").json()["checks"]
    return next(c for c in checks if c["id"] == "verifier")


def test_setup_status_needs_a_verifier_key(client, no_verifier_env):
    assert not _verifier_step(client)["done"]


@pytest.mark.parametrize("key", [f"{s.upper()}_API_KEY" for s in COUNTED])
def test_one_new_key_alone_completes_the_verifier_step(client, no_verifier_env, key):
    r = client.post("/api/settings/env", json={key: "test-key-123"})
    assert r.json()["success"]
    assert _verifier_step(client)["done"]
    data = client.get("/api/settings").json()
    assert data[f"{key.lower()}_set"] is True
    assert "test-key-123" not in json.dumps(data)
    assert f"{key}=test-key-123" in dash.ENV_FILE.read_text()


def test_the_backlog_notice_names_the_new_keys():
    from harvey.main import PROSPECT_BACKLOG, unusable_backlog_notice
    notice = unusable_backlog_notice({"prospects": {"new": 0}, "writable_prospects": 0,
                                      "recent_unusable_prospects": PROSPECT_BACKLOG})
    for key in (f"{s.upper()}_API_KEY" for s in COUNTED):
        assert key in notice


def test_every_counted_key_is_a_dashboard_verifier_key():
    assert {f"{s.upper()}_API_KEY" for s in COUNTED} <= set(dash.VERIFIER_KEYS)


def test_the_settings_card_has_a_field_for_every_counted_key():
    web = os.path.join(os.path.dirname(dash.__file__), "web")
    html = open(os.path.join(web, "index.html"), encoding="utf-8").read()
    js = open(os.path.join(web, "app.js"), encoding="utf-8").read()
    for service in COUNTED:
        assert f'id="{service}-key"' in html and f'id="{service}-status"' in html
        assert f"payload.{service.upper()}_API_KEY" in js
        assert f"data.{service}_api_key_set" in js
