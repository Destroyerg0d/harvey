"""The LinkedIn agent against a pretend LinkedIn: no network, no browser.

Covers the send recipe (and each way it refuses), approval vs autopilot,
the cross-channel stop, sync, invites, breakers and when a run may start.
"""

import asyncio
import random
from datetime import datetime, timedelta, timezone

import pytest

from harvey.agents.linkedin_agent import (
    AUTOPILOT_KEY, NOTES_OFF_KEY, RUNNING_KEY, LinkedInAgent, LinkedInRunner, asks_if_automated,
    fit_note,
)
from harvey.linkedin import guards
from harvey.linkedin.pages import MESSAGING_URL
from harvey.linkedin.store import HELD_FOR_REVIEW, MAYBE_SENT
from harvey.models.company import Company
from harvey.models.prospect import Prospect
from harvey.state import StateManager
from tests.linkedin_fakes import (
    IN_HOURS, OWNER, DriverFactory, FakeBrain, FakeDriver, FakeLinkedIn, Person, make_config,
)


def run(coro):
    return asyncio.run(coro)


def naive(dt):
    return dt.astimezone(timezone.utc).replace(tzinfo=None)


class World:
    def __init__(self, tmp_path, people=(), brain=None, at=IN_HOURS, driver_kwargs=None,
                 site_kwargs=None, **cfg):
        self.state = StateManager(str(tmp_path / "h.db"))
        run(self.state.init_db())
        self.config = make_config(**cfg)
        self.site = FakeLinkedIn(people, **(site_kwargs or {}))
        self.driver = FakeDriver(self.site, **(driver_kwargs or {}))
        self.brain = brain or FakeBrain()
        self.at = at

    def agent(self):
        async def no_sleep(_):
            pass
        return LinkedInAgent(self.brain, self.state, self.config,
                             driver_factory=DriverFactory(self.driver), now=lambda: self.at,
                             sleep=no_sleep, rng=random.Random(3))

    def go(self, **kw):
        return run(self.agent().run(ignore_interval=True, **kw))

    def lead(self, slug, **fields):
        lead_id, _ = run(self.state.li_upsert_lead(slug, **fields))
        return lead_id

    def msgs(self, lead_id, **kw):
        return run(self.state.li_messages(lead_id=lead_id, **kw))


def jane(**kw):
    return Person(slug="jane-doe-1", name="Jane Doe", connected=True, connected_on="Oct 28, 2026", **kw)


# ── The send recipe ──

def test_an_approved_message_goes_through_the_whole_recipe(tmp_path):
    w = World(tmp_path, [jane()])
    lead = w.lead("jane-doe-1", stage="accepted", name="Jane Doe",
                  profile_url="https://www.linkedin.com/in/jane-doe-1/")
    body = "Jane, your product pages load slowly on phones.\nWant a short list of fixes?"
    m = run(w.state.li_add_message(lead, body=body, status="approved", approved_by="you"))
    report = w.go()
    assert report["outcome"] == "done" and report["sent_messages"] == 1
    assert w.driver.sent == [("jane-doe-1", body)]
    # profile first, then its own compose link (not the sidebar's other person)
    compose = [u for u in w.driver.visited if "/messaging/compose/" in u]
    assert compose and "recipient=jane-doe-1" in compose[0] and "screenContext" not in compose[0]
    sent = run(w.state.li_get_message(m))
    assert sent["status"] == "sent" and sent["sent_at"]
    l = run(w.state.li_get_lead(lead))
    assert l["stage"] == "messaged" and l["next_action_at"] > naive(IN_HOURS).isoformat()
    assert run(w.agent().pacing.used())["messages_today"] == 1


def test_wrong_recipient_fails_and_nothing_is_typed(tmp_path):
    w = World(tmp_path, [jane()])
    w.driver.state["pill_override"] = "Riley Other"
    lead = w.lead("jane-doe-1", stage="accepted", name="Jane Doe")
    m = run(w.state.li_add_message(lead, body="Hi Jane", status="approved", approved_by="you"))
    report = w.go()
    item = run(w.state.li_get_message(m))
    assert item["status"] == "failed" and "recipient" in item["error"]
    assert w.driver.sent == [] and report["failed"] == 1
    assert run(w.state.li_retry(m)) == 1          # it never went: you may retry it


def test_composer_text_that_differs_is_not_sent(tmp_path):
    w = World(tmp_path, [jane()], driver_kwargs={"typing": lambda s: s[:-1]})
    lead = w.lead("jane-doe-1", stage="accepted", name="Jane Doe")
    m = run(w.state.li_add_message(lead, body="Hi Jane, a draft.", status="approved", approved_by="you"))
    w.go()
    item = run(w.state.li_get_message(m))
    assert item["status"] == "failed" and "exactly" in item["error"]
    assert w.driver.sent == [] and "composer" not in w.driver.state


def test_a_send_the_thread_doesnt_show_may_have_gone_and_is_never_retried(tmp_path):
    w = World(tmp_path, [jane()], driver_kwargs={"show_after_send": False})
    lead = w.lead("jane-doe-1", stage="accepted", name="Jane Doe")
    m = run(w.state.li_add_message(lead, body="Hi Jane", status="approved", approved_by="you"))
    w.go()
    item = run(w.state.li_get_message(m))
    assert item["status"] == "failed" and item["error"].startswith(MAYBE_SENT)
    assert run(w.state.li_retry(m)) == 0
    w.go()
    assert len(w.driver.sent) == 1                 # not sent a second time


def test_no_message_button_means_not_connected(tmp_path):
    w = World(tmp_path, [Person(slug="ada-lane", name="Ada Lane")])
    lead = w.lead("ada-lane", stage="accepted", name="Ada Lane")
    m = run(w.state.li_add_message(lead, body="Hi Ada", status="approved", approved_by="you"))
    w.go()
    assert "Message button" in run(w.state.li_get_message(m))["error"]


def test_they_wrote_first_so_the_pitch_waits(tmp_path):
    w = World(tmp_path, [jane(thread=[("Jane Doe", "Hi Sam, saw your profile, what do you do?")])])
    lead = w.lead("jane-doe-1", stage="accepted", name="Jane Doe")
    m = run(w.state.li_add_message(lead, body="Jane, a pitch.", status="approved", approved_by="you"))
    agent = w.agent()
    async def go():
        async with agent.driver_factory() as d:
            agent.driver = d
            agent.report = {"would_send": [], "inbound": 0}
            return await agent.send_message(await w.state.li_get_lead(lead), await w.state.li_get_message(m))
    ok, why = run(go())
    assert not ok and "written to you" in why and w.driver.sent == []
    assert [x["direction"] for x in w.msgs(lead)].count("in") == 1


# ── Approval and autopilot ──

def _accepted_two_days_ago(w):
    return w.lead("jane-doe-1", stage="accepted", name="Jane Doe", first_name="Jane",
                  company="Example Goods", accepted_at=naive(IN_HOURS) - timedelta(days=2))


def test_approval_mode_drafts_and_waits(tmp_path):
    w = World(tmp_path, [jane()])
    lead = _accepted_two_days_ago(w)
    report = w.go()
    drafts = w.msgs(lead, statuses=("draft",))
    assert len(drafts) == 1 and drafts[0]["kind"] == "message" and report["drafted"] == 1
    assert w.driver.sent == []
    task, prompt = [c for c in w.brain.calls if c[0] == "draft_first"][0]
    assert "Example Goods" in prompt and "<untrusted" in prompt


def test_autopilot_approves_and_sends_in_the_same_run(tmp_path):
    w = World(tmp_path, [jane()])
    run(w.state.set_setting(AUTOPILOT_KEY, "on"))
    lead = _accepted_two_days_ago(w)
    w.go()
    sent = w.msgs(lead, statuses=("sent",))
    assert len(sent) == 1 and sent[0]["approved_by"] == "autopilot"
    assert w.driver.sent and w.driver.sent[0][1] == w.brain.text


def test_config_autopilot_is_overridden_by_the_dashboard_switch(tmp_path):
    w = World(tmp_path, [jane()], autopilot=True)
    run(w.state.set_setting(AUTOPILOT_KEY, "off"))
    lead = _accepted_two_days_ago(w)
    w.go()
    assert w.driver.sent == [] and len(w.msgs(lead, statuses=("draft",))) == 1


def test_autopilot_holds_a_reply_to_are_you_a_bot(tmp_path):
    w = World(tmp_path, [jane(thread=[(OWNER, "Jane, a first message."),
                                      ("Jane Doe", "Is this automated? Are you a bot?")])],
              brain=FakeBrain(intent="question", text="Yes, I use an AI assistant to draft these."))
    run(w.state.set_setting(AUTOPILOT_KEY, "on"))
    lead = w.lead("jane-doe-1", stage="messaged", name="Jane Doe")
    run(w.state.li_add_message(lead, body="Jane, a first message.", status="sent",
                               sent_at=naive(IN_HOURS) - timedelta(days=1)))
    w.go()
    replies = w.msgs(lead, kinds=("reply",), direction="out")
    assert len(replies) == 1 and replies[0]["status"] == "draft"
    assert replies[0]["error"].startswith(HELD_FOR_REVIEW) and "automated" in replies[0]["error"]
    assert w.driver.sent == []
    assert run(w.state.li_get_lead(lead))["stage"] == "replied"


def test_asks_if_automated():
    assert asks_if_automated("Quick one, is this automated?")
    assert asks_if_automated("are you a real person")
    assert asks_if_automated("This reads like ChatGPT")
    assert not asks_if_automated("We automated our shipping last year")


# ── Cross-channel ──

def _prospect(w, status="contacted"):
    company_id = run(w.state.add_company(Company(name="Example Goods", domain="examplegoods.test")))
    pid = run(w.state.add_prospect(Prospect(first_name="Jane", last_name="Doe", title="Founder",
                                            email="jane@examplegoods.test", company_id=company_id,
                                            status=status)))
    return company_id, pid


def test_a_no_on_linkedin_stops_email_too(tmp_path):
    w = World(tmp_path, [jane(thread=[(OWNER, "Jane, a first message."),
                                      ("Jane Doe", "No thanks, not interested.")])])
    company_id, pid = _prospect(w)
    outbox = run(w.state.add_outbox_item(prospect_id=pid, to_email="jane@examplegoods.test",
                                         subject="s", body="b", send_at="2026-11-05T10:00:00",
                                         status="approved", campaign_id="c1", step=2))
    lead = w.lead("jane-doe-1", stage="messaged", name="Jane Doe", prospect_id=pid, company_id=company_id)
    run(w.state.li_add_message(lead, body="Jane, a first message.", status="sent",
                               sent_at=naive(IN_HOURS) - timedelta(days=3)))
    pending = run(w.state.li_add_message(lead, body="A follow-up", status="approved", approved_by="you"))
    report = w.go()
    assert run(w.state.li_get_lead(lead))["stage"] == "declined" and report["declined"] == 1
    assert run(w.state.get_prospect(pid)).status == "opted_out"
    assert run(w.state.get_outbox_item(outbox))["status"] == "cancelled"
    assert run(w.state.li_get_message(pending))["status"] == "skipped"
    assert w.driver.sent == []
    assert not w.msgs(lead, kinds=("reply",), direction="out")       # a no gets no answer


def test_an_email_reply_blocks_the_linkedin_pitch(tmp_path):
    w = World(tmp_path, [jane()])
    company_id, pid = _prospect(w, status="replied")
    lead = w.lead("jane-doe-1", stage="accepted", name="Jane Doe", prospect_id=pid,
                  company_id=company_id, accepted_at=naive(IN_HOURS) - timedelta(days=2))
    m = run(w.state.li_add_message(lead, body="Approved earlier", status="approved", approved_by="you"))
    w.go()
    assert not w.msgs(lead, statuses=("draft",))                     # nothing new drafted
    item = run(w.state.li_get_message(m))
    assert item["status"] == "skipped" and "replied" in item["error"]
    assert w.driver.sent == []


def test_a_positive_linkedin_reply_stops_the_cold_email(tmp_path):
    w = World(tmp_path, [jane(thread=[(OWNER, "Jane, a first message."),
                                      ("Jane Doe", "Yes, tell me more about this")])],
              brain=FakeBrain(intent="interested"))
    company_id, pid = _prospect(w, status="contacted")
    outbox = run(w.state.add_outbox_item(prospect_id=pid, to_email="jane@examplegoods.test",
                                         subject="s", body="b", send_at="2026-11-05T10:00:00",
                                         campaign_id="c1", step=2))
    lead = w.lead("jane-doe-1", stage="messaged", name="Jane Doe", prospect_id=pid, company_id=company_id)
    run(w.state.li_add_message(lead, body="Jane, a first message.", status="sent",
                               sent_at=naive(IN_HOURS) - timedelta(days=1)))
    w.go()
    assert run(w.state.li_get_lead(lead))["stage"] == "warm"
    assert run(w.state.get_prospect(pid)).status == "replied"
    assert run(w.state.get_outbox_item(outbox))["status"] == "cancelled"
    assert len(w.msgs(lead, kinds=("reply",), statuses=("draft",))) == 1


def test_declined_leads_are_never_messaged(tmp_path):
    w = World(tmp_path, [jane()])
    lead = w.lead("jane-doe-1", stage="declined", name="Jane Doe")
    m = run(w.state.li_add_message(lead, body="Hi", status="approved", approved_by="you"))
    w.go()
    assert run(w.state.li_get_message(m))["status"] == "skipped" and w.driver.sent == []


# ── Follow-ups ──

def test_one_follow_up_after_five_to_seven_days_then_nothing(tmp_path):
    w = World(tmp_path, [jane()])
    lead = w.lead("jane-doe-1", stage="messaged", name="Jane Doe")
    run(w.state.li_add_message(lead, kind="invite_note", body="", status="sent",
                               sent_at=naive(IN_HOURS) - timedelta(days=12)))
    run(w.state.li_add_message(lead, body="First", status="sent",
                               sent_at=naive(IN_HOURS) - timedelta(days=4)))
    w.go()
    assert not w.msgs(lead, statuses=("draft",))                    # 4 days: too soon
    w.at = IN_HOURS + timedelta(days=2)                              # 6 days, a Friday
    w.go()
    drafts = w.msgs(lead, statuses=("draft",))
    assert len(drafts) == 1 and any(c[0] == "draft_follow_up" for c in w.brain.calls)
    run(w.state.li_update_message(drafts[0]["id"], status="sent",
                                  sent_at=naive(w.at)))              # invite + 2 = 3 unanswered
    w.at = IN_HOURS + timedelta(days=14)
    w.go()
    assert not w.msgs(lead, statuses=("draft",))


def test_same_company_leads_are_never_messaged(tmp_path):
    w = World(tmp_path, [jane()])
    lead = _accepted_two_days_ago(w)
    run(w.state.li_update_lead(lead, no_message_reason="a colleague is the one to message"))
    w.go()
    assert not w.msgs(lead)


# ── Sync ──

def test_sync_records_replies_and_acceptances_once(tmp_path):
    people = [jane(thread=[(OWNER, "Jane, a first message."), ("Jane Doe", "What would it cost?")]),
              Person(slug="omar-field", name="Omar Field", connected=True, connected_on="Nov 2, 2026")]
    w = World(tmp_path, people)
    jane_id = w.lead("jane-doe-1", stage="messaged", name="Jane Doe")
    run(w.state.li_add_message(jane_id, body="Jane, a first message.", status="sent",
                               sent_at=naive(IN_HOURS) - timedelta(days=1)))
    omar_id = w.lead("omar-field", stage="invited", name="Omar Field")
    w.go()
    inbound = w.msgs(jane_id, direction="in")
    assert [m["body"] for m in inbound] == ["What would it cost?"] and inbound[0]["intent"] == "question"
    assert run(w.state.li_get_lead(jane_id))["stage"] == "replied"
    omar = run(w.state.li_get_lead(omar_id))
    assert omar["stage"] == "accepted" and omar["accepted_at"].startswith("2026-11-02")
    assert len(w.msgs(jane_id, kinds=("reply",), statuses=("draft",))) == 1
    w.go()
    assert len(w.msgs(jane_id, direction="in")) == 1
    assert len(w.msgs(jane_id, kinds=("reply",), statuses=("draft",))) == 1


def test_a_reply_you_sent_by_hand_is_recorded(tmp_path):
    w = World(tmp_path, [jane(thread=[("Jane Doe", "Who are you?"), (OWNER, "Sam here, I build stores.")])])
    lead = w.lead("jane-doe-1", stage="accepted", name="Jane Doe")
    w.site.people["jane-doe-1"].thread.append(("Jane Doe", "Ok"))   # their last word shows in the inbox
    w.go()
    out = w.msgs(lead, direction="out")
    assert any(m["body"] == "Sam here, I build stores." and m["status"] == "sent" for m in out)


def test_old_invites_are_withdrawn(tmp_path):
    w = World(tmp_path, [Person(slug="ada-lane", name="Ada Lane", pending=True, pending_age="Sent 4 weeks ago"),
                         Person(slug="bo-chen", name="Bo Chen", pending=True, pending_age="Sent 2 weeks ago")])
    ada = w.lead("ada-lane", stage="invited", name="Ada Lane")
    bo = w.lead("bo-chen", stage="invited", name="Bo Chen")
    report = w.go()
    assert w.site.withdrawn == ["ada-lane"] and report["withdrawn"] == 1
    assert run(w.state.li_get_lead(ada))["stage"] == "withdrawn"
    assert run(w.state.li_get_lead(bo))["stage"] == "invited"


# ── Invites ──

def _candidate(w, slug="kim-ray", title="Founder"):
    company_id = run(w.state.add_company(Company(name="Ray Goods", domain="raygoods.test")))
    return run(w.state.add_prospect(Prospect(first_name="Kim", last_name="Ray", title=title,
                                             email="kim@raygoods.test", company_id=company_id,
                                             linkedin_url=f"https://www.linkedin.com/in/{slug}/",
                                             status="contacted")))


def test_invites_go_to_founders_with_a_short_note(tmp_path):
    w = World(tmp_path, [Person(slug="kim-ray", name="Kim Ray"), Person(slug="lee-wu", name="Lee Wu")])
    run(w.state.set_setting(AUTOPILOT_KEY, "on"))
    pid = _candidate(w)
    _candidate(w, slug="lee-wu", title="Marketing Intern")
    report = w.go()
    lead = run(w.state.li_get_lead_by_slug("kim-ray"))
    assert lead and lead["prospect_id"] == pid and run(w.state.li_get_lead_by_slug("lee-wu")) is None
    assert w.driver.invites == [("kim-ray", "Fellow store owner here, would be good to connect.")]
    assert run(w.state.li_get_lead(lead["id"]))["stage"] == "invited" and report["sent_invites"] == 1
    assert run(w.agent().pacing.used())["invites_today"] == 1


def test_the_note_limit_sends_the_invite_without_a_note(tmp_path):
    w = World(tmp_path, [Person(slug="kim-ray", name="Kim Ray")], site_kwargs={"note_limit": True})
    run(w.state.set_setting(AUTOPILOT_KEY, "on"))
    _candidate(w)
    w.go()
    assert w.driver.invites == [("kim-ray", "")]
    assert run(w.state.get_setting(NOTES_OFF_KEY))
    lead = run(w.state.li_get_lead_by_slug("kim-ray"))
    note = w.msgs(lead["id"], kinds=("invite_note",))[0]
    assert note["status"] == "sent" and note["body"] == ""


def test_the_weekly_invite_limit_pauses_linkedin(tmp_path):
    w = World(tmp_path, [Person(slug="kim-ray", name="Kim Ray")], site_kwargs={"weekly_limit": True})
    run(w.state.set_setting(AUTOPILOT_KEY, "on"))
    _candidate(w)
    report = w.go()
    assert report["outcome"].startswith("stopped") and w.driver.invites == []
    pause = run(guards.active_pause(w.state, naive(IN_HOURS)))
    assert pause["kind"] == "invite_limit" and pause["until"]
    lead = run(w.state.li_get_lead_by_slug("kim-ray"))
    assert w.msgs(lead["id"], kinds=("invite_note",))[0]["status"] == "approved"   # not lost


def test_invite_daily_cap_limits_drafts(tmp_path):
    w = World(tmp_path, [], max_daily_connections=1)
    _candidate(w)
    _candidate(w, slug="lee-wu")
    w.go()
    assert len(run(w.state.li_messages(kinds=("invite_note",)))) == 1


def test_fit_note():
    assert fit_note("Short note.", 200) == "Short note."
    long = "One sentence here. " + "word " * 60
    assert fit_note(long, 34) == "One sentence here."
    assert len(fit_note("x" * 10 + " " + "y" * 300, 50)) <= 50


# ── Breakers and when a run may start ──

def test_a_warning_stops_the_run_and_pauses_for_48_hours(tmp_path):
    w = World(tmp_path, [jane()], site_kwargs={"breaker_on": MESSAGING_URL})
    lead = w.lead("jane-doe-1", stage="accepted", name="Jane Doe")
    m = run(w.state.li_add_message(lead, body="Hi", status="approved", approved_by="you"))
    report = w.go()
    assert report["outcome"].startswith("stopped") and "unusual activity" in report["outcome"]
    pause = run(guards.active_pause(w.state, naive(IN_HOURS)))
    assert pause["kind"] == "unusual_activity"
    assert run(w.state.li_get_message(m))["status"] == "approved" and w.driver.sent == []
    assert "paused" in run(w.agent().why_not_now(ignore_interval=True))


def test_signed_out_pauses_until_sign_in(tmp_path):
    w = World(tmp_path, [jane()])
    w.site.signed_in = False
    report = w.go()
    assert report["outcome"].startswith("stopped")
    pause = run(guards.active_pause(w.state, naive(IN_HOURS) + timedelta(days=30)))
    assert pause["kind"] == "signed_out" and "harvey linkedin login" in pause["reason"]


def test_when_a_run_may_start(tmp_path):
    w = World(tmp_path, [jane()])
    agent = w.agent()
    assert run(agent.why_not_now()) == ""
    w.config.channels.linkedin.enabled = False
    assert "off" in run(w.agent().why_not_now())
    w.config.channels.linkedin.enabled = True
    w.at = IN_HOURS.replace(hour=15)                                # 20:30 IST
    assert "outside LinkedIn hours" in run(w.agent().why_not_now())
    w.at = IN_HOURS
    w.go()
    assert "last LinkedIn run" in run(w.agent().why_not_now())       # ~2 hours apart
    w.at = IN_HOURS + timedelta(minutes=121)
    assert run(w.agent().why_not_now()) == ""
    run(w.state.set_setting(RUNNING_KEY, naive(w.at).isoformat()))
    assert "already going" in run(w.agent().why_not_now())


def test_dry_run_reads_and_drafts_but_sends_nothing(tmp_path):
    w = World(tmp_path, [jane()])
    run(w.state.set_setting(AUTOPILOT_KEY, "on"))
    lead = w.lead("jane-doe-1", stage="accepted", name="Jane Doe")
    m = run(w.state.li_add_message(lead, body="Hi Jane", status="approved", approved_by="you"))
    report = w.go(dry_run=True)
    assert w.driver.sent == [] and report["would_send"][0]["body"] == "Hi Jane"
    assert run(w.state.li_get_message(m))["status"] == "approved"


def test_outside_hours_nothing_opens(tmp_path):
    w = World(tmp_path, [jane()], at=IN_HOURS.replace(hour=1))      # 06:30 IST
    report = run(w.agent().run())
    assert not report["ran"] and w.driver.visited == []


def test_the_runner_starts_one_run_at_a_time(tmp_path):
    w = World(tmp_path, [jane()])
    release = asyncio.Event()
    started = []

    class SlowAgent:
        async def why_not_now(self):
            return ""

        async def run(self):
            started.append(1)
            await release.wait()

    async def go():
        runner = LinkedInRunner(w.brain, w.state, w.config, agent_factory=SlowAgent)
        assert await runner.maybe_start() == "started"
        await asyncio.sleep(0)
        assert await runner.maybe_start() == "a run is going"
        release.set()
        await runner.task
        assert await runner.maybe_start() == "started"
        await asyncio.sleep(0)
        await runner.shutdown()
    run(go())
    assert len(started) == 2


def test_the_runner_does_nothing_when_linkedin_is_off(tmp_path):
    w = World(tmp_path, [jane()], enabled=False)
    runner = LinkedInRunner(w.brain, w.state, w.config)
    assert run(runner.maybe_start()) == "off" and runner.task is None


@pytest.mark.parametrize("cfg", [{"min_delay_seconds": 300, "max_delay_seconds": 60},
                                 {"hours": {"timezone": "Mars/Base"}},
                                 {"hours": {"start": "25:00"}},
                                 {"max_daily_messages": -1}])
def test_bad_linkedin_settings_are_refused(cfg):
    with pytest.raises(Exception):
        make_config(**cfg)
