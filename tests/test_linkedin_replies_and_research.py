"""LinkedIn (owner, 2026-11-16): a shortened name no longer fails every send,
a send that keeps failing stops being redrafted, messages are written from
the person's own profile, and replies go out at any hour. WhatsApp replies
don't wait for the person's daytime. All data is fictional."""

import asyncio
import json
from datetime import datetime, timedelta, timezone

import pytest

from harvey.agents.linkedin_agent import (
    INBOX_CHECK_KEY, MAX_FAILED_SENDS, PROFILE_KEY, LinkedInAgent, LinkedInRunner,
)
from harvey.linkedin.pages import names_match, profile_headline, profile_summary
from harvey.state import StateManager
from tests.linkedin_fakes import IN_HOURS, DriverFactory, FakeBrain, FakeDriver, FakeLinkedIn, make_config

NIGHT = IN_HOURS + timedelta(hours=14)        # 02:00 IST


def run(coro):
    return asyncio.run(coro)


def test_a_shortened_last_name_matches_the_whole_one():
    assert names_match("Toby G.", "Toby Green")
    assert names_match("Toby Green", "Toby G")
    assert not names_match("Toby G.", "Toby Brown")
    assert not names_match("Toby G.", "Tom Green")


PROFILE = """<html><body><nav>Home My Network Jobs</nav><main>
<section class="artdeco-card"><h1>Ann Sample</h1><div class="text-body-medium">Founder at Lumen Candles,
hand-poured soy candles</div><span>London, England</span><button>Message</button><span>500+ connections</span></section>
<section><h2>About</h2><p>We make small-batch candles and sell them through our own Shopify store.</p></section>
</main><aside>People also viewed: Somebody Else</aside></body></html>"""


def test_the_profile_is_read_into_plain_lines_without_buttons_or_side_panels():
    text = profile_summary(PROFILE)
    assert "Founder at Lumen Candles" in text and "small-batch candles" in text
    assert "Message" not in text and "500+ connections" not in text and "Somebody Else" not in text
    assert profile_headline(PROFILE).startswith("Founder at Lumen Candles")


def world(tmp_path, at=IN_HOURS, **cfg):
    state = StateManager(str(tmp_path / "h.db"))
    run(state.init_db())
    agent = LinkedInAgent(FakeBrain(), state, make_config(**cfg), driver_factory=DriverFactory(
        FakeDriver(FakeLinkedIn([]))), now=lambda: at)
    return state, agent


def test_a_send_that_keeps_failing_stops_being_redrafted(tmp_path):
    state, agent = world(tmp_path)

    async def go():
        lead, _ = await state.li_upsert_lead("toby-g-1", stage="accepted", name="Toby G.",
                                             accepted_at=IN_HOURS - timedelta(days=3))
        for _ in range(MAX_FAILED_SENDS):
            await state.li_add_message(lead, kind="message", body="Hi", status="failed",
                                       error="the message window didn't show Toby G. as the recipient",
                                       dedupe=False)
        agent.deadline = None
        await agent.draft_messages()
        return await state.li_get_lead(lead), await state.li_messages(lead_id=lead, statuses=("draft",))
    lead, drafts = run(go())
    assert drafts == [] and "failed" in lead["no_message_reason"]


def test_the_profile_goes_into_the_draft_and_is_kept(tmp_path):
    state, agent = world(tmp_path)

    class Page:
        url = "https://www.linkedin.com/in/ann-sample-1/"

        async def goto(self, url):
            self.url = url

        async def html(self):
            return PROFILE

    async def go():
        lead, _ = await state.li_upsert_lead("ann-sample-1", stage="accepted", name="Ann Sample",
                                             first_name="Ann", company="Lumen Candles",
                                             profile_url="https://www.linkedin.com/in/ann-sample-1/")
        agent.driver = Page()

        async def no_wait():
            pass
        agent.pacing.between_pages = no_wait
        lead_row = await state.li_get_lead(lead)
        await agent.draft(lead_row, "first")
        cached = json.loads(await state.get_setting(PROFILE_KEY + lead))
        return lead_row, cached, await state.li_get_lead(lead)
    lead_row, cached, stored = run(go())
    prompt = agent.brain.calls[-1][1] if isinstance(agent.brain.calls[-1], tuple) else agent.brain.calls[-1]
    assert "small-batch candles" in prompt and "Never fall back on a question" in prompt
    assert "small-batch candles" in cached["text"]
    assert stored["headline"].startswith("Founder at Lumen Candles")


def test_replies_can_be_checked_at_night_but_a_full_run_waits_for_hours(tmp_path):
    state, agent = world(tmp_path, at=NIGHT, replies_any_time=True, reply_check_minutes=10)
    assert "outside LinkedIn hours" in run(agent.why_not_now())
    assert run(agent.why_not_now(replies_only=True)) == ""
    run(state.set_setting(INBOX_CHECK_KEY, (NIGHT - timedelta(minutes=5)).replace(tzinfo=None).isoformat()))
    assert "next check waits 10 minutes" in run(agent.why_not_now(replies_only=True))


def test_without_the_switch_there_is_no_night_check(tmp_path):
    state, agent = world(tmp_path, at=NIGHT)
    assert "replies at any hour are off" in run(agent.why_not_now(replies_only=True))


def test_the_runner_starts_an_inbox_check_when_no_full_run_is_due(tmp_path):
    state, agent = world(tmp_path, at=NIGHT, replies_any_time=True)
    started = []

    class Agent:
        async def why_not_now(self, ignore_interval=False, replies_only=False):
            return "" if replies_only else "outside LinkedIn hours"

        async def run(self, replies_only=False):
            started.append(replies_only)
            return {}
    runner = LinkedInRunner(None, state, agent.config, agent_factory=Agent)

    async def go():
        result = await runner.maybe_start()
        await runner.task
        return result
    assert run(go()) == "started (inbox check)" and started == [True]


def test_whatsapp_replies_go_whatever_the_hour_with_the_switch(tmp_path):
    from tests.whatsapp_fakes import World
    w = World(tmp_path, replies_any_time=True)
    agent = w.agent()
    contact = {"id": "c1", "phone": "+447700900123", "country": "GB", "prospect_id": ""}
    assert run(agent._may_send(contact, "reply"))
    w2 = World(tmp_path / "b")
    assert run(w2.agent()._may_send(contact, "reply")) == run(w2.agent()._recipient_open(contact))
