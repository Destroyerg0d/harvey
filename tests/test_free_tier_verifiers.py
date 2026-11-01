"""The four counted free-tier verifiers (Bounceless, EmailVerify.io,
GetProspect, Abstract): each provider's answer is read into the same verdict
shape as Reoon's and ZeroBounce's, anything odd is inconclusive (None) and
never raises, they run after the other verifiers in both orders, and each one
stops once this month's allowance is used. No network: every request goes to
an httpx.MockTransport, and the data is fictional."""

import json
import os
import tempfile
import time
from datetime import datetime, timezone

import httpx
import pytest
import pytest_asyncio

import harvey.dashboard as dash
from harvey.config import load_env
from harvey.integrations import email_finder as ef
from harvey.state import StateManager
from tests.test_dashboard_live_status import client, env  # noqa: F401  (fixtures)

REAL_CLIENT = httpx.AsyncClient
ADDRESS = "jane.doe@acme.example.test"
COUNTED = ("bounceless", "emailverify", "getprospect", "abstract")
ALLOWANCE_SETTINGS = ("BOUNCELESS_MONTHLY_CHECKS", "EMAILVERIFY_MONTHLY_CHECKS",
                      "GETPROSPECT_MONTHLY_CHECKS", "ABSTRACT_MONTHLY_CHECKS")


@pytest_asyncio.fixture
async def state():
    with tempfile.TemporaryDirectory() as tmpdir:
        sm = StateManager(os.path.join(tmpdir, "test.db"))
        await sm.init_db()
        yield sm


@pytest.fixture(autouse=True)
def _defaults(monkeypatch):
    # Free-tier allowances unless a test sets one; no pause between Abstract
    # calls unless a test is about it.
    for name in ALLOWANCE_SETTINGS:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(ef, "ABSTRACT_MIN_INTERVAL", 0.0)


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
        for attr in ("reoon", "zerobounce", "bounceless", "emailverify", "getprospect", "abstract"):
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
async def test_abstract_verdicts(monkeypatch, body, expected):
    assert await graded(monkeypatch, ef.verify_abstract, answer(body)) == expected


@pytest.mark.asyncio
async def test_abstract_request_shape(monkeypatch):
    seen = mock_http(monkeypatch, answer(abstract_body("DELIVERABLE")))
    await ef.verify_abstract(ADDRESS, "ab-test-key")
    url = seen[0].url
    assert (url.host, url.path) == ("emailvalidation.abstractapi.com", "/v1/")
    assert url.params["api_key"] == "ab-test-key" and url.params["email"] == ADDRESS


@pytest.mark.asyncio
async def test_abstract_calls_are_spaced_for_its_free_plan(monkeypatch):
    mock_http(monkeypatch, answer(abstract_body("DELIVERABLE")))
    monkeypatch.setattr(ef, "ABSTRACT_MIN_INTERVAL", 0.3)
    monkeypatch.setattr(ef, "_abstract_last_call", time.monotonic())
    started = time.monotonic()
    await ef.verify_abstract(ADDRESS, "ab-test-key")
    assert time.monotonic() - started >= 0.25


# ── Failures are inconclusive, never raised ──

VERIFIERS = [ef.verify_bounceless, ef.verify_emailverify, ef.verify_getprospect, ef.verify_abstract]


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
    expected = {"bounceless": 1000, "emailverify": 100, "getprospect": 100, "abstract": 100}
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


class FakeCalls:
    """Stand-ins for every verifier, recording the order they're asked in."""

    def __init__(self, monkeypatch, answers=None):
        self.calls = []
        answers = answers or {}
        for name in ("zerobounce", "reoon", "hunter", "bounceless", "emailverify",
                     "getprospect", "abstract"):
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


ALL_KEYS = Keys(reoon="r", zerobounce="z", bounceless="b", emailverify="e",
                getprospect="g", abstract="a")


@pytest.mark.asyncio
async def test_big_providers_try_the_counted_tiers_last(state, monkeypatch):
    fake = FakeCalls(monkeypatch)
    assert await ef._verify_candidate(ADDRESS, "google", ALL_KEYS, state=state) == (None, False)
    assert fake.calls == ["zerobounce", "reoon", "hunter",
                          "bounceless", "emailverify", "getprospect", "abstract"]


@pytest.mark.asyncio
async def test_small_hosts_try_the_counted_tiers_after_smtp_and_hunter(state, monkeypatch):
    fake = FakeCalls(monkeypatch)
    assert await ef._verify_candidate(ADDRESS, "other", ALL_KEYS, state=state) == (None, False)
    assert fake.calls == ["reoon", "smtp", "hunter",
                          "bounceless", "emailverify", "getprospect", "abstract"]


@pytest.mark.asyncio
async def test_the_first_conclusive_verdict_wins_and_later_ones_are_not_spent(state, monkeypatch):
    fake = FakeCalls(monkeypatch, {"emailverify": {"status": "valid", "catch_all": False}})
    assert await ef._verify_candidate(ADDRESS, "microsoft", ALL_KEYS, state=state) == ("verified", False)
    assert fake.calls[-1] == "emailverify"
    assert await ef.verifier_checks_remaining("bounceless", state) == 999
    assert await ef.verifier_checks_remaining("emailverify", state) == 99
    # Never asked, never counted.
    assert await ef.verifier_checks_remaining("getprospect", state) == 100
    assert await ef.verifier_checks_remaining("abstract", state) == 100


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


# ── A new key alone counts as having a verifier ──

def test_load_env_reads_the_new_keys(monkeypatch):
    monkeypatch.setattr("harvey.config.load_dotenv", lambda *a, **k: False)
    monkeypatch.setenv("BOUNCELESS_API_KEY", "  blc.test  ")
    monkeypatch.setenv("EMAILVERIFY_API_KEY", "ev-test")
    monkeypatch.setenv("GETPROSPECT_API_KEY", "gp-test")
    monkeypatch.setenv("ABSTRACT_API_KEY", "ab-test")
    env = load_env()
    assert (env.bounceless_api_key, env.emailverify_api_key,
            env.getprospect_api_key, env.abstract_api_key) == ("blc.test", "ev-test", "gp-test", "ab-test")


@pytest.fixture
def no_verifier_env(monkeypatch):
    for key in dash.VERIFIER_KEYS:
        monkeypatch.delenv(key, raising=False)


def _verifier_step(client):
    checks = client.get("/api/setup-status").json()["checks"]
    return next(c for c in checks if c["id"] == "verifier")


def test_setup_status_needs_a_verifier_key(client, no_verifier_env):
    assert not _verifier_step(client)["done"]


@pytest.mark.parametrize("key", ["BOUNCELESS_API_KEY", "EMAILVERIFY_API_KEY",
                                 "GETPROSPECT_API_KEY", "ABSTRACT_API_KEY"])
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
    for key in ("BOUNCELESS_API_KEY", "EMAILVERIFY_API_KEY", "GETPROSPECT_API_KEY",
                "ABSTRACT_API_KEY"):
        assert key in notice
