"""compliance.precleared.skip_countries: a recipient Harvey is SURE is in a
skipped country (from the company's location) gets no Precleared check and
is clear to send. An unknown or unsure country, or one that only the
prospect_country default supplies, is still checked; an empty list is
today's behaviour. No network: Precleared is an httpx.MockTransport, and
every name and address is fictional."""

import json
import os
import tempfile
from types import SimpleNamespace

import httpx
import pytest
import pytest_asyncio
import yaml
from pydantic import ValidationError

from harvey.config import PreclearedConfig, load_config
from harvey.integrations.precleared import SETTINGS_PREFIX, PreclearedClient
from harvey.state import StateManager
from tests.test_outbox_native import FakeProvider, make_sender
from tests.test_send_window import approved, seed_at

OK_US = {
    "verdict": "ok_to_email", "confidence": "high", "reasons": [],
    "facts": {"recipient_country": "US"}, "receipt": {"id": "cc_rcpt_US0000000001"},
}
CONSENT = {
    "verdict": "consent_needed", "confidence": "high",
    "reasons": [{"text": "Consent is needed.", "source_title": "Rule"}],
    "next_step": "Get consent first.", "receipt": {"id": "cc_rcpt_NO0000000001"},
}


@pytest_asyncio.fixture
async def state():
    with tempfile.TemporaryDirectory() as tmpdir:
        sm = StateManager(os.path.join(tmpdir, "test.db"))
        await sm.init_db()
        yield sm


def mock_precleared(calls, body=OK_US):
    def handler(request):
        calls.append(json.loads(request.content))
        return httpx.Response(200, json=body)
    return httpx.MockTransport(handler)


def client(calls, body=OK_US, **kw):
    return PreclearedClient(sender_country="GB", transport=mock_precleared(calls, body), **kw)


# ── config ──

def test_skip_countries_defaults_to_none():
    assert PreclearedConfig().skip_countries == []
    assert PreclearedConfig(skip_countries=None).skip_countries == []


@pytest.mark.parametrize("given, expected", [
    (["us"], ["US"]),
    ([" ca ", "UK", "gb", "US"], ["CA", "GB", "US"]),   # upper-cased, UK is GB, each once
    ("US, CA", ["US", "CA"]),
    (["", "us"], ["US"]),
])
def test_skip_countries_are_normalised(given, expected):
    assert PreclearedConfig(skip_countries=given).skip_countries == expected


@pytest.mark.parametrize("bad", [[False], ["USA1"], ["U"], [1], ["U5"], {"US": True}, 7])
def test_skip_countries_reject_anything_but_country_codes(bad):
    with pytest.raises(ValidationError):
        PreclearedConfig(skip_countries=bad)


def test_unquoted_yaml_no_is_an_error_not_ignored():
    with pytest.raises(ValidationError, match="quote"):
        PreclearedConfig(skip_countries=yaml.safe_load("[NO]"))


def test_skip_countries_load_from_the_config_file(tmp_path):
    path = tmp_path / "harvey.yaml"
    path.write_text(yaml.safe_dump({
        "persona": {"name": "H", "company": "A", "role": "B",
                    "email": "h@a.example.test", "linkedin": "", "tone": "casual"},
        "product": {"name": "P", "description": "D", "pricing": "$1",
                    "key_benefits": ["x"], "objection_responses": {}},
        "icp": {"industries": ["SaaS"], "company_size": "10-50",
                "titles": ["CEO"], "geography": ["US"]},
        "compliance": {"precleared": {
            "enabled": True, "sender_country": "US", "skip_countries": ["us"],
        }},
    }))
    assert load_config(str(path)).compliance.precleared.skip_countries == ["US"]


def test_from_config_passes_skip_countries():
    settings = PreclearedConfig(enabled=True, sender_country="GB", skip_countries=["US", "CA"])
    config = SimpleNamespace(compliance=SimpleNamespace(precleared=settings),
                             product=SimpleNamespace(description="d"))
    built = PreclearedClient.from_config(config, SimpleNamespace(precleared_api_key=""))
    assert built.skip_countries == {"US", "CA"}
    plain = PreclearedConfig(enabled=True, sender_country="GB")
    config.compliance.precleared = plain
    assert PreclearedClient.from_config(config, SimpleNamespace()).skip_countries == frozenset()


# ── the client ──

@pytest.mark.asyncio
async def test_a_sure_country_in_the_list_is_not_checked(state):
    calls = []
    result = await client(calls, skip_countries=["US"]).clear(
        "jane@tx-acme.example.test", state, country="US")
    assert result.allowed and result.skipped and not result.cached
    assert result.receipt_id == "" and calls == []
    # Nothing stored: no verdict was given.
    assert await state.get_setting(SETTINGS_PREFIX + "jane@tx-acme.example.test") == ""


@pytest.mark.asyncio
async def test_unknown_country_is_still_checked(state):
    calls = []
    result = await client(calls, skip_countries=["US"]).clear("jane@acme.example.test", state)
    assert result.allowed and not result.skipped and len(calls) == 1


@pytest.mark.asyncio
async def test_the_prospect_country_default_never_skips(state):
    calls = []
    check = client(calls, skip_countries=["US"], prospect_country="US")
    result = await check.clear("jane@acme.example.test", state)
    assert not result.skipped and len(calls) == 1
    assert calls[0]["default_recipient_country"] == "US"
    assert "recipient_country" not in calls[0]


@pytest.mark.asyncio
async def test_another_sure_country_is_still_checked(state):
    calls = []
    result = await client(calls, skip_countries=["US"]).clear(
        "kate@acme.example.test", state, country="GB")
    assert not result.skipped and calls[0]["recipient_country"] == "GB"


@pytest.mark.asyncio
async def test_an_empty_list_checks_as_before(state):
    calls = []
    result = await client(calls, skip_countries=[]).clear(
        "jane@tx-acme.example.test", state, country="US")
    assert not result.skipped and calls[0]["recipient_country"] == "US"
    assert result.receipt_id == "cc_rcpt_US0000000001"


@pytest.mark.asyncio
async def test_a_stored_verdict_still_applies(state):
    calls = []
    await client(calls, CONSENT).clear("sam@tx-acme.example.test", state, country="US")
    result = await client(calls, skip_countries=["US"]).clear(
        "sam@tx-acme.example.test", state, country="US")
    assert result.cached and not result.skipped and not result.allowed
    assert len(calls) == 1


# ── the sender ──

def checked_sender(state, provider, calls, transport=None, **kw):
    sender = make_sender(state, provider, require_approval=False)
    sender.precleared = PreclearedClient(
        sender_country="GB", transport=transport or mock_precleared(calls), **kw)
    return sender


async def receipts(state):
    import aiosqlite
    async with aiosqlite.connect(state.db_path) as db:
        async with db.execute(
            "SELECT details_json FROM actions WHERE action_type = 'email_sent'"
        ) as cursor:
            rows = await cursor.fetchall()
    return [json.loads(r[0]).get("precleared_receipt", "") for r in rows]


@pytest.mark.asyncio
async def test_sender_skips_the_check_for_a_known_us_recipient(state):
    pid = await seed_at(state, "Austin, TX", "jane@tx-acme.example.test")
    await approved(state, pid, "jane@tx-acme.example.test")
    calls, provider = [], FakeProvider()
    await checked_sender(state, provider, calls, skip_countries=["US"])._drain_due()
    assert [m["to"] for m in provider.sent] == ["jane@tx-acme.example.test"]
    assert calls == []
    assert await receipts(state) == [""]


@pytest.mark.asyncio
async def test_sender_still_checks_an_unknown_country(state):
    pid = await seed_at(state, "", "jane@acme.example.test")
    await approved(state, pid, "jane@acme.example.test")
    calls, provider = [], FakeProvider()
    await checked_sender(state, provider, calls, skip_countries=["US"])._drain_due()
    assert len(calls) == 1 and "recipient_country" not in calls[0]
    assert [m["to"] for m in provider.sent] == ["jane@acme.example.test"]


@pytest.mark.asyncio
async def test_sender_still_checks_when_only_prospect_country_says_us(state):
    pid = await seed_at(state, "Houston", "kim@acme.example.test")   # a city alone: not sure
    await approved(state, pid, "kim@acme.example.test")
    calls, provider = [], FakeProvider()
    await checked_sender(state, provider, calls, skip_countries=["US"],
                         prospect_country="US")._drain_due()
    assert len(calls) == 1 and calls[0]["default_recipient_country"] == "US"
    assert await receipts(state) == ["cc_rcpt_US0000000001"]


@pytest.mark.asyncio
async def test_sender_with_an_empty_list_checks_as_before(state):
    pid = await seed_at(state, "Austin, TX", "jane@tx-acme.example.test")
    await approved(state, pid, "jane@tx-acme.example.test")
    calls, provider = [], FakeProvider()
    await checked_sender(state, provider, calls, skip_countries=[])._drain_due()
    assert len(calls) == 1 and calls[0]["recipient_country"] == "US"
    assert await receipts(state) == ["cc_rcpt_US0000000001"]


@pytest.mark.asyncio
async def test_a_skipped_recipient_goes_out_while_precleared_is_down(state):
    def down(request):
        raise httpx.ConnectError("no route")

    us = await seed_at(state, "Austin, TX", "jane@tx-acme.example.test")
    elsewhere = await seed_at(state, "", "kate@acme.example.test")
    await approved(state, elsewhere, "kate@acme.example.test")
    await approved(state, us, "jane@tx-acme.example.test")
    provider = FakeProvider()
    sender = checked_sender(state, provider, [], transport=httpx.MockTransport(down),
                            skip_countries=["US"])
    await sender._drain_due()
    assert [m["to"] for m in provider.sent] == ["jane@tx-acme.example.test"]
    # The unknown-country email waits on the check, and a skip is not an answer.
    assert sender.compliance_held
