"""Cold outreach only to the countries and decision makers the owner chose
(owner, 2026-11-19: Western countries, never cold to India, founders, CEOs,
CTOs and the like). Replies and anyone who wrote first aren't filtered. Every
name, address and number is fictional."""

import pytest
from pydantic import ValidationError

from harvey import targeting
from harvey.agents.linkedin_agent import AUTOPILOT_KEY
from harvey.config import TargetingConfig
from tests.linkedin_fakes import Person
from tests.test_linkedin_agent import World, _candidate, run
from tests.test_outbox_native import FakeProvider, make_sender, state  # noqa: F401  (fixture)
from tests.test_send_window import approved, outbox_row, seed_at

WEST = list(targeting.WESTERN)


@pytest.mark.parametrize("title, yes", [
    ("Founder & CEO", True), ("Co-Founder, Example Goods", True), ("Owner", True), ("CTO", True),
    ("Chief Marketing Officer", True), ("Managing Director", True), ("President at Example", True),
    ("Executive Assistant to the CEO", False), ("Marketing Manager", False), ("VP Sales", False),
    ("Former founder", False), ("Head of Ecommerce", False), ("Principal Designer", False), ("", False),
])
def test_decision_makers(title, yes):
    assert targeting.is_decision_maker(title) is yes


@pytest.mark.parametrize("location, code", [
    ("Austin, Texas, United States", "US"), ("San Francisco Bay Area", "US"),
    ("New York City Metropolitan Area", "US"), ("London Area, United Kingdom", "GB"),
    ("Greater Sydney Area", "AU"), ("Mumbai, Maharashtra, India", "IN"), ("Greater Delhi Area", "IN"),
    ("", ""),
])
def test_countries_from_locations(location, code):
    assert targeting.location_country(location) == code


def test_the_rules_and_the_config():
    config = type("C", (), {"targeting": TargetingConfig(countries=WEST, decision_makers_only=True)})()
    assert targeting.cold_reason(config, country="IN") == "outside your target countries (IN)"
    assert targeting.cold_reason(config, country="US", title="Founder") == ""
    assert targeting.cold_reason(config, title="Marketing Manager").startswith("not a decision maker")
    assert targeting.cold_reason(config) == ""                              # unknown: cold email goes
    assert targeting.cold_reason(config, country_required=True, title_required=True) != ""
    assert targeting.active(type("C", (), {"targeting": TargetingConfig()})()) is False
    assert TargetingConfig(countries="us, uk").countries == ["US", "GB"]
    with pytest.raises(ValidationError):
        TargetingConfig(countries=["United States"])


# ── Cold email ──

def sender_with_targets(state, provider, decision_makers_only=False):
    sender = make_sender(state, provider, require_approval=False)
    sender.config.targeting = TargetingConfig(countries=WEST, decision_makers_only=decision_makers_only)
    return sender


@pytest.mark.asyncio
async def test_cold_email_to_someone_named_who_isn_t_a_decision_maker_isn_t_sent(state):
    vp = await seed_at(state, "", "vp@shop.example")          # seed_at's prospects are a "VP"
    held = await approved(state, vp, "vp@shop.example")
    provider = FakeProvider()
    await sender_with_targets(state, provider, decision_makers_only=True)._drain_due()
    assert provider.sent == []
    assert (await outbox_row(state, held))["error"] == "targeting: not a decision maker (VP)"


@pytest.mark.asyncio
async def test_cold_email_to_india_isn_t_sent_and_a_com_address_still_is(state):
    india = await seed_at(state, "", "owner@shop.co.in")
    usa = await seed_at(state, "", "owner@shop.example")
    held = await approved(state, india, "owner@shop.co.in")
    await approved(state, usa, "owner@shop.example")
    provider = FakeProvider()
    await sender_with_targets(state, provider)._drain_due()
    assert [m["to"] for m in provider.sent] == ["owner@shop.example"]
    row = await outbox_row(state, held)
    assert row["status"] == "failed" and row["error"] == "targeting: outside your target countries (IN)"


@pytest.mark.asyncio
async def test_a_website_chat_follow_up_to_india_still_goes(state):
    india = await seed_at(state, "", "visitor@mail.co.in")
    item = await approved(state, india, "visitor@mail.co.in")
    await state.update_outbox_item(item, angle="inbound")
    provider = FakeProvider()
    await sender_with_targets(state, provider)._drain_due()
    assert [m["to"] for m in provider.sent] == ["visitor@mail.co.in"]


# ── LinkedIn connects ──

@pytest.mark.parametrize("location, headline, invited", [
    ("Austin, Texas, United States", "Founder at Ray Goods", True),
    ("Mumbai, Maharashtra, India", "Founder at Ray Goods", False),
    ("Austin, Texas, United States", "Marketing Manager at Ray Goods", False),
    ("", "Founder at Ray Goods", False),                                 # location unread
])
def test_a_connect_needs_a_target_country_and_a_decision_maker_on_the_profile(tmp_path, location,
                                                                               headline, invited):
    w = World(tmp_path, [Person(slug="kim-ray", name="Kim Ray", location=location, headline=headline)])
    w.config.targeting = TargetingConfig(countries=WEST, decision_makers_only=True)
    run(w.state.set_setting(AUTOPILOT_KEY, "on"))
    _candidate(w)
    report = w.go()
    lead = run(w.state.li_get_lead_by_slug("kim-ray"))
    assert (w.driver.invites != []) is invited
    if not invited:
        assert lead["no_message_reason"].startswith("not invited: ") and report["off_target"] == 1
        note = w.msgs(lead["id"], kinds=("invite_note",))[0]
        assert note["status"] == "skipped"
