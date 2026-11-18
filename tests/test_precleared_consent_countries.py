"""compliance.precleared.consent_countries (owner, 2026-11-18): where a cold
email needs the recipient's consent whatever a check finds (Australia's Spam
Act 2003), the recipient is held without spending one of the day's checks.
Sure from the company's location or the address's country domain; a country
domain only ever holds, it never skips a check. Scout stops looking for shops
there. No network: Precleared is an httpx.MockTransport that records every
call, and every name and address is fictional."""

from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from harvey.agents import scout as S
from harvey.config import PreclearedConfig
from harvey.integrations.precleared import SETTINGS_PREFIX, PreclearedClient, _domain_country
from tests.test_outbox_native import FakeProvider
from tests.test_precleared_skip_countries import CONSENT, OK_US, checked_sender, client, state  # noqa: F401
from tests.test_send_window import approved, outbox_row, seed_at


# ── config ──

def test_consent_countries_are_validated_like_skip_countries():
    assert PreclearedConfig(consent_countries=["au", "UK"]).consent_countries == ["AU", "GB"]
    assert PreclearedConfig().consent_countries == []
    with pytest.raises(ValidationError, match="consent_countries"):
        PreclearedConfig(consent_countries=["Australia"])
    with pytest.raises(ValidationError, match="in both"):
        PreclearedConfig(skip_countries=["US", "AU"], consent_countries=["AU"])


def test_from_config_passes_consent_countries():
    settings = PreclearedConfig(enabled=True, sender_country="US", consent_countries=["AU"])
    config = SimpleNamespace(compliance=SimpleNamespace(precleared=settings),
                             product=SimpleNamespace(description="d"))
    built = PreclearedClient.from_config(config, SimpleNamespace(precleared_api_key=""))
    assert built.consent_countries == {"AU"}


@pytest.mark.parametrize("email, code", [
    ("hello@shop.com.au", "AU"), ("hi@shop.au", "AU"), ("a@shop.co.uk", "GB"), ("a@shop.uk", "GB"),
    ("a@shop.com", ""), ("a@shop.store", ""), ("a@localhost", ""), ("broken", ""),
])
def test_an_address_s_country_domain(email, code):
    assert _domain_country(email) == code


# ── the client ──

@pytest.mark.asyncio
@pytest.mark.parametrize("email, country", [
    ("hello@shop.com.au", ""),          # the address's country domain
    ("hello@shop.example", "AU"),       # the company's location
])
async def test_a_recipient_in_a_consent_country_is_held_without_a_check(state, email, country):
    calls = []
    result = await client(calls, consent_countries=["AU"]).clear(email, state, country=country)
    assert not result.allowed and result.skipped and not result.held
    assert result.verdict == "consent_needed" and result.country == "AU"
    assert "consent" in result.reason and calls == []
    assert await state.get_setting(SETTINGS_PREFIX + email) == ""      # no verdict stored


@pytest.mark.asyncio
async def test_anyone_else_is_still_checked(state):
    calls = []
    result = await client(calls, consent_countries=["AU"]).clear("jane@shop.example", state)
    assert result.allowed and not result.skipped and len(calls) == 1


@pytest.mark.asyncio
async def test_a_country_domain_never_skips_a_check(state):
    calls = []
    result = await client(calls, skip_countries=["GB"]).clear("jane@shop.co.uk", state)
    assert not result.skipped and len(calls) == 1


@pytest.mark.asyncio
async def test_a_stored_verdict_still_comes_first(state):
    calls = []
    await client(calls, OK_US).clear("sam@shop.com.au", state)
    result = await client(calls, consent_countries=["AU"]).clear("sam@shop.com.au", state)
    assert result.cached and result.allowed and len(calls) == 1


# ── the sender ──

@pytest.mark.asyncio
async def test_the_sender_holds_an_australian_first_email_and_spends_no_check(state):
    au = await seed_at(state, "", "hello@shop.com.au")
    us = await seed_at(state, "", "jane@shop.example")
    held = await approved(state, au, "hello@shop.com.au")
    await approved(state, us, "jane@shop.example")
    calls, provider = [], FakeProvider()
    await checked_sender(state, provider, calls, consent_countries=["AU"])._drain_due()
    assert [m["to"] for m in provider.sent] == ["jane@shop.example"]
    assert len(calls) == 1 and "jane@shop.example" in str(calls[0])
    row = await outbox_row(state, held)
    assert row["status"] == "failed" and row["error"].startswith("precleared: consent_needed: not checked")


# ── Scout ──

def test_scout_doesn_t_look_for_shops_in_a_consent_country():
    def config(consent):
        return SimpleNamespace(
            icp=SimpleNamespace(geography=["United States", "Australia", "Austin, TX"]),
            compliance=SimpleNamespace(precleared=SimpleNamespace(consent_countries=consent)))
    held = S.city_rotation(config(["AU"]))
    assert not set(held) & set(S.MAP_CITIES["australia"])
    assert "Austin, TX" in held and S.MAP_CITIES["united states"][0] in held
    assert set(S.MAP_CITIES["australia"]) <= set(S.city_rotation(config([])))
