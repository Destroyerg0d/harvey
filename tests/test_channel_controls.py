"""The on/off switch with no restart, writing your own reply, adding a contact,
everything visible on the tabs, and the Today cards. All with fakes: nothing here
opens a browser or sends anything."""

import asyncio
import random
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import yaml

import harvey.dashboard as dash
from harvey import channel_login as cl
from harvey.agents.linkedin_agent import ENABLED_KEY as LI_KEY
from harvey.agents.linkedin_agent import LinkedInAgent, LinkedInRunner, channel_on as li_on
from harvey.agents.scout import Scout
from harvey.agents.whatsapp_agent import ENABLED_KEY as WA_KEY
from harvey.agents.whatsapp_agent import WhatsAppRunner, channel_on as wa_on
from harvey.linkedin import guards as li_guards
from harvey.linkedin import pacing as li_pacing
from harvey.linkedin.cli import SIGNED_IN_KEY
from harvey.models.prospect import Prospect
from harvey.state import StateManager
from harvey.whatsapp import guards as wa_guards
from harvey.whatsapp.pacing import LINKED_AT_KEY
from tests import linkedin_fakes as lf
from tests import whatsapp_fakes as wf
from tests.test_dashboard_signals import client  # noqa: F401  (fixture)

JANE = wf.JANE


def run(coro):
    return asyncio.run(coro)


@pytest.fixture
def web(client, tmp_path, monkeypatch):  # noqa: F811
    path = tmp_path / "harvey.yaml"
    path.write_text(yaml.safe_dump({**wf.MINIMAL, "channels": {
        "linkedin": {"max_unanswered": 3}, "whatsapp": {"number": "+15555550100", "max_unanswered": 3}}}))
    monkeypatch.setattr(dash, "_resolve_config_file", lambda: path)
    state = StateManager(str(client.db_path))
    run(state.init_db())
    client.state = state
    return client


def connected(web):
    run(web.state.set_setting(SIGNED_IN_KEY, "2026-11-01T00:00:00+00:00"))
    run(web.state.set_setting(LINKED_AT_KEY, "2026-11-01T00:00:00"))


class LiWorld:
    def __init__(self, tmp_path, people=(), **cfg):
        self.state = StateManager(str(tmp_path / "li.db"))
        run(self.state.init_db())
        self.config = lf.make_config(**cfg)
        self.site = lf.FakeLinkedIn(people)
        self.driver = lf.FakeDriver(self.site)
        self.brain = lf.FakeBrain()

    def agent(self, sleep=None):
        async def no_sleep(_):
            pass
        return LinkedInAgent(self.brain, self.state, self.config, driver_factory=lf.DriverFactory(self.driver),
                             now=lambda: lf.IN_HOURS, sleep=sleep or no_sleep, rng=random.Random(3))

    def go(self, **kw):
        return run(self.agent(**kw).run(ignore_interval=True))


def li_jane():
    return lf.Person(slug="jane-doe-1", name="Jane Doe", connected=True, connected_on="Oct 28, 2026")


def wa_jane(**kw):
    return wf.Person(phone=JANE, title="Jane Doe", **kw)


# ── The override, read fresh every time ──

@pytest.mark.parametrize("override,config_on,expected", [
    ("", True, True), ("", False, False), ("on", False, True), ("off", True, False),
    ("ON ", False, True), ("nonsense", True, True), ("nonsense", False, False),
])
def test_the_setting_overrides_the_config_either_way(tmp_path, override, config_on, expected):
    w = wf.World(tmp_path, enabled=config_on)
    run(w.state.set_setting(WA_KEY, override))
    assert run(wa_on(w.state, w.config)) is expected
    li = LiWorld(tmp_path, enabled=config_on)
    run(li.state.set_setting(LI_KEY, override))
    assert run(li_on(li.state, li.config)) is expected


def test_whatsapp_turned_on_with_the_config_off_starts_with_no_restart(tmp_path):
    """The same runner, as the heartbeat made it at startup with the config saying off."""
    w = wf.World(tmp_path, [wa_jane()], enabled=False)
    w.approved(w.contact())

    async def go():
        runner = WhatsAppRunner(w.brain, w.state, w.config, agent_factory=w.agent)
        assert await runner.maybe_start() == "off" and runner.task is None
        await w.state.set_setting(WA_KEY, "on")
        assert await runner.maybe_start() == "started"
        await runner.task
    run(go())
    assert w.driver.enter_presses == 1


def test_whatsapp_turned_off_with_the_config_on_stops_at_the_next_cycle(tmp_path):
    w = wf.World(tmp_path, [wa_jane()])
    w.approved(w.contact())

    async def go():
        runner = WhatsAppRunner(w.brain, w.state, w.config, agent_factory=w.agent)
        await w.state.set_setting(WA_KEY, "off")
        assert await runner.maybe_start() == "off" and runner.task is None
    run(go())
    assert w.driver.enter_presses == 0


def test_linkedin_runner_reads_the_switch_each_cycle(tmp_path):
    li = LiWorld(tmp_path, [li_jane()], enabled=False)

    async def go():
        runner = LinkedInRunner(li.brain, li.state, li.config, agent_factory=li.agent)
        assert await runner.maybe_start() == "off"
        await li.state.set_setting(LI_KEY, "on")
        assert await runner.maybe_start() == "started"
        await runner.task
        await li.state.set_setting(LI_KEY, "off")
        assert await runner.maybe_start() == "off"
    run(go())


def test_the_heartbeat_makes_its_runners_whatever_the_config_says():
    """A config that's off at startup must not leave a channel out for good."""
    from pathlib import Path
    from harvey import main
    source = Path(main.__file__).read_text(encoding="utf-8")
    assert "linkedin = LinkedInRunner(brain, state, config)" in source
    assert "whatsapp = WhatsAppRunner(brain, state, config)" in source
    assert "channels.linkedin.enabled and" not in source and "channels.whatsapp.enabled and" not in source


def test_a_run_refuses_while_off_and_says_where_the_switch_is(tmp_path):
    w = wf.World(tmp_path)
    run(w.state.set_setting(WA_KEY, "off"))
    report = run(w.agent().run(ignore_interval=True))
    assert report["ran"] is False and "WhatsApp is off" in report["reason"] and "dashboard" in report["reason"]
    li = LiWorld(tmp_path)
    run(li.state.set_setting(LI_KEY, "off"))
    report = run(li.agent().run(ignore_interval=True))
    assert report["ran"] is False and "LinkedIn is off" in report["reason"]


def test_status_on_the_terminal_tells_the_same_story(tmp_path, capsys):
    import argparse
    from harvey.whatsapp import cli as wa_cli
    w = wf.World(tmp_path, enabled=False)
    run(w.state.set_setting(WA_KEY, "on"))
    parser = argparse.ArgumentParser()
    wa_cli.add_parser(parser.add_subparsers(dest="command"))
    run(wa_cli._status(parser.parse_args(["whatsapp", "status"]), w.config, w.state))
    assert "WhatsApp: on" in capsys.readouterr().out


# ── Off takes effect at once, in the middle of a run ──

def test_off_during_a_whatsapp_run_stops_before_the_next_send(tmp_path):
    w = wf.World(tmp_path, [wa_jane(), wf.Person(phone=wf.SAM, title="Sam Lane")])
    first = w.approved(w.contact())
    second = w.approved(w.contact(phone=wf.SAM, name="Sam Lane"))
    sent = []

    async def after_first_send(*a):
        if w.driver.enter_presses and not sent:
            sent.append(True)
            await w.state.set_setting(WA_KEY, "off")
    agent = w.agent()
    agent.pacing._sleep = after_first_send
    report = run(agent.run(ignore_interval=True))
    assert report["outcome"] == "stopped: switched off" and report["sent"] == 1
    assert w.driver.enter_presses == 1
    statuses = {w.get(first)["status"], w.get(second)["status"]}
    assert statuses == {"sent", "approved"}       # what's left stays approved, unsent
    assert run(w.state.get_setting("whatsapp:running")) == ""


def test_off_while_reading_whatsapp_stops_before_the_next_chat_opens(tmp_path):
    w = wf.World(tmp_path, [wa_jane(unread=True, thread=[("in", "Hello")])])
    w.contact()

    async def off(*a):
        await w.state.set_setting(WA_KEY, "off")
    agent = w.agent()
    agent.pacing._sleep = off
    report = run(agent.run(ignore_interval=True))
    assert report["outcome"] == "stopped: switched off"
    assert not any("phone=" in url for url in w.driver.visited)


def test_off_just_before_a_chat_opens_to_send_is_a_stop_not_a_failed_send(tmp_path):
    """Nothing was typed, so nothing counts against the failed-send breaker."""
    w = wf.World(tmp_path, [wa_jane()])
    m = w.approved(w.contact())
    agent = w.agent()
    original = agent.send_message

    async def send(contact, item):
        await w.state.set_setting(WA_KEY, "off")
        return await original(contact, item)
    agent.send_message = send
    report = run(agent.run(ignore_interval=True))
    assert report["outcome"] == "stopped: switched off" and report["failed"] == 0
    assert w.get(m)["status"] == "approved" and w.driver.enter_presses == 0
    assert run(w.state.get_setting(wa_guards.FAILED_KEY)) in ("", "0")


def test_off_while_reading_linkedin_stops_before_the_next_page(tmp_path):
    li = LiWorld(tmp_path, [li_jane()])

    async def off(*a):
        await li.state.set_setting(LI_KEY, "off")
    report = li.go(sleep=off)
    assert report["outcome"] == "stopped: switched off"
    assert li.driver.visited == [lf.FEED_URL]         # the check it starts with, and nothing after


def test_off_while_typing_a_whatsapp_message_means_enter_is_never_pressed(tmp_path):
    w = wf.World(tmp_path, [wa_jane()])

    async def switched_off():
        await w.state.set_setting(WA_KEY, "off")
    w.driver.on_type = switched_off
    m = w.approved(w.contact())
    report = run(w.agent().run(ignore_interval=True))
    assert w.driver.enter_presses == 0 and w.driver.typed and w.driver.composer == ""
    assert w.get(m)["status"] == "approved"       # never claimed: nothing to wonder about
    assert report["outcome"] == "stopped: switched off" and report["maybe_sent"] == 0


def test_a_pause_while_typing_a_whatsapp_message_stops_it_too(tmp_path):
    w = wf.World(tmp_path, [wa_jane()])

    async def paused():
        await wa_guards.set_pause(w.state, "you", kind="manual", hours=None, now=wf.OPEN.replace(tzinfo=None))
    w.driver.on_type = paused
    w.approved(w.contact())
    run(w.agent().run(ignore_interval=True))
    assert w.driver.enter_presses == 0


def _li_approved(li, slug="jane-doe-1", kind="message", body="Jane, your product pages load slowly on phones."):
    lead = run(li.state.li_upsert_lead(slug, stage="accepted", name="Jane Doe",
                                       profile_url=f"https://www.linkedin.com/in/{slug}/"))[0]
    return lead, run(li.state.li_add_message(lead, kind=kind, body=body, status="approved", approved_by="you"))


def test_off_during_a_linkedin_run_stops_before_the_send(tmp_path):
    li = LiWorld(tmp_path, [li_jane()])
    _, m = _li_approved(li)

    async def off(*a):
        await li.state.set_setting(LI_KEY, "off")
    report = li.go(sleep=off)
    assert li.driver.sent == [] and report["outcome"] == "stopped: switched off"
    assert run(li.state.li_get_message(m))["status"] == "approved"
    assert run(li.state.get_setting("linkedin:running")) == ""


def test_off_while_typing_on_linkedin_means_send_is_never_pressed(tmp_path):
    li = LiWorld(tmp_path, [li_jane()])
    _, m = _li_approved(li)
    original = li.driver.type_text

    async def type_then_off(selector, text):
        ok = await original(selector, text)
        await li.state.set_setting(LI_KEY, "off")
        return ok
    li.driver.type_text = type_then_off
    report = li.go()
    assert li.driver.sent == [] and lf.SEND_BUTTON not in li.driver.clicked
    assert run(li.state.li_get_message(m))["status"] == "approved"
    assert report["outcome"] == "stopped: switched off"


def test_pausing_linkedin_mid_run_now_holds_the_next_send_too(tmp_path):
    li = LiWorld(tmp_path, [li_jane()])
    _, m = _li_approved(li)

    async def paused(*a):
        await li_guards.set_pause(li.state, "you", kind="manual", hours=None)
    li.go(sleep=paused)
    assert li.driver.sent == [] and run(li.state.li_get_message(m))["status"] == "approved"


def test_off_stops_old_invites_being_withdrawn_mid_run(tmp_path):
    person = lf.Person(slug="jane-doe-1", name="Jane Doe", pending=True, pending_age="Sent 4 weeks ago")
    li = LiWorld(tmp_path, [person])
    run(li.state.li_upsert_lead("jane-doe-1", stage="invited", name="Jane Doe"))

    async def off(*a):
        await li.state.set_setting(LI_KEY, "off")
    li.go(sleep=off)
    assert li.site.withdrawn == []


def test_a_long_break_ends_early_when_the_channel_stops(monkeypatch):
    monkeypatch.setattr(li_pacing, "STOP_CHECK_SECONDS", 0.01)
    ended = []

    async def long_break():
        try:
            await asyncio.sleep(60)
        finally:
            ended.append(True)

    async def stop():
        return True
    assert run(li_pacing.wait_unless(long_break(), stop)) is False
    assert ended == [True]


def test_a_wait_that_isnt_stopped_runs_its_course(monkeypatch):
    monkeypatch.setattr(li_pacing, "STOP_CHECK_SECONDS", 0.01)
    done = []

    async def short():
        await asyncio.sleep(0.03)
        done.append(True)

    async def stop():
        return False
    assert run(li_pacing.wait_unless(short(), stop)) is True and done == [True]


@pytest.mark.asyncio
async def test_off_stops_scouts_linkedin_search_too_and_on_doesnt_start_it():
    """Off means off everywhere. The dashboard's on is a yes to the channel's own
    signed-in profile, not to a password-based search besides."""
    def scout(config_on, setting):
        state = SimpleNamespace(log_action=AsyncMock(), get_setting=AsyncMock(return_value=setting))
        config = SimpleNamespace(channels=SimpleNamespace(linkedin=SimpleNamespace(enabled=config_on)))
        s = Scout(SimpleNamespace(load_skills_for_agent=lambda name: ""), state, config,
                  SimpleNamespace(linkedin_email="me@example.com"))
        for name in ("hunter", "known_companies", "linkedin", "hiring_boards", "profile_search",
                     "footprints", "company_discovery"):
            setattr(s, f"_prospect_via_{name}", AsyncMock(return_value=0))
        return s

    off = scout(True, "off")
    await off.run()
    off._prospect_via_linkedin.assert_not_awaited()
    off._prospect_via_profile_search.assert_not_awaited()
    on = scout(False, "on")
    await on.run()
    on._prospect_via_linkedin.assert_not_awaited()
    still = scout(True, "")
    await still.run()
    still._prospect_via_linkedin.assert_awaited_once()


# ── The switch on the dashboard ──

@pytest.mark.parametrize("channel,key", [("linkedin", LI_KEY), ("whatsapp", WA_KEY)])
def test_turning_on_while_not_connected_is_refused_with_the_reason(web, channel, key):
    res = web.post(f"/api/{channel}/enabled", json={"mode": "on", "confirm": channel})
    assert res.status_code == 409 and "Connect" in res.json()["message"]
    assert run(web.state.get_setting(key)) == ""


@pytest.mark.parametrize("channel,guards,key", [("linkedin", li_guards, LI_KEY), ("whatsapp", wa_guards, WA_KEY)])
def test_turning_on_while_signed_out_is_refused(web, channel, guards, key):
    connected(web)
    run(guards.set_pause(web.state, "gone", kind="signed_out", hours=None))
    res = web.post(f"/api/{channel}/enabled", json={"mode": "on", "confirm": channel})
    assert res.status_code == 409 and "Reconnect" in res.json()["message"]
    assert run(web.state.get_setting(key)) == ""


@pytest.mark.parametrize("channel,guards,key", [("linkedin", li_guards, LI_KEY), ("whatsapp", wa_guards, WA_KEY)])
def test_turning_on_during_a_sign_in_is_refused(web, channel, guards, key):
    from harvey.pidfile import hold_loop_lock, release_loop_lock
    connected(web)
    handle = hold_loop_lock(cl.lock_path(web.state, channel))
    try:
        run(web.state.set_setting(cl.login_key(channel), '{"status": "waiting", "pid": 1}'))
        res = web.post(f"/api/{channel}/enabled", json={"mode": "on", "confirm": channel})
        assert res.status_code == 409 and "first" in res.json()["message"]
    finally:
        release_loop_lock(handle)


@pytest.mark.parametrize("channel,key,warning", [
    ("linkedin", LI_KEY, "LinkedIn's terms"), ("whatsapp", WA_KEY, "breaks its terms")])
def test_turning_on_needs_the_typed_word_and_the_refusal_states_the_risk(web, channel, key, warning):
    connected(web)
    for body in ({"mode": "on"}, {"mode": "on", "confirm": "yes"}, {"mode": "on", "confirm": "autopilot"}):
        res = web.post(f"/api/{channel}/enabled", json=body)
        assert res.status_code == 400 and warning in res.json()["message"]
        assert run(web.state.get_setting(key)) == ""
    assert web.post(f"/api/{channel}/enabled", json={"mode": "on", "confirm": f" {channel.upper()} "}).json()["success"]
    assert run(web.state.get_setting(key)) == "on"
    now = web.get(f"/api/{channel}").json()
    assert now["enabled"] is True and now["connection"]["enabled_source"] == "dashboard"


def test_the_risk_is_on_the_card_for_the_ui_to_say_plainly(web):
    assert "restricted" in web.get("/api/linkedin/login").json()["risk"]
    assert "banned" in web.get("/api/whatsapp/login").json()["risk"]


@pytest.mark.parametrize("channel,key", [("linkedin", LI_KEY), ("whatsapp", WA_KEY)])
def test_turning_off_is_one_click_and_needs_no_connection(web, channel, key):
    assert web.post(f"/api/{channel}/enabled", json={"mode": "off"}).json()["success"]
    assert run(web.state.get_setting(key)) == "off"
    assert web.get(f"/api/{channel}").json()["enabled"] is False


@pytest.mark.parametrize("channel", ["linkedin", "whatsapp"])
def test_the_switch_takes_nothing_else(web, channel):
    assert web.post(f"/api/{channel}/enabled", json={"mode": "maybe"}).status_code == 400
    assert web.post(f"/api/{channel}/enabled", json={}).status_code == 400


def test_a_warning_pause_stands_when_the_channel_is_turned_on(web):
    connected(web)
    run(wa_guards.set_pause(web.state, "spam notice", kind="spam", hours=48))
    assert web.post("/api/whatsapp/enabled", json={"mode": "on", "confirm": "whatsapp"}).json()["success"]
    assert run(wa_guards.active_pause(web.state))["kind"] == "spam"


def test_the_pause_routes_still_work_beside_the_new_ones(web):
    """/api/<channel>/{action} comes last in each router, so these aren't its."""
    assert web.post("/api/whatsapp/pause").json()["success"]
    assert web.post("/api/whatsapp/resume").json()["success"]
    assert web.post("/api/linkedin/pause").json()["success"]
    assert web.post("/api/linkedin/resume").json()["success"]
    assert web.post("/api/whatsapp/nonsense").status_code == 404


def test_the_config_stays_a_config_nothing_rewrites_it(web, tmp_path):
    before = (tmp_path / "harvey.yaml").read_text()
    connected(web)
    web.post("/api/whatsapp/enabled", json={"mode": "on", "confirm": "whatsapp"})
    assert (tmp_path / "harvey.yaml").read_text() == before


# ── Your own reply ──

def _wa_talking(web, phone=JANE, **kw):
    state = web.state
    kw.setdefault("name", "Jane Doe")
    contact, _ = run(state.wa_add_contact(phone=phone, source="website chat", stage="replied",
                                          last_inbound_at="2026-11-04T05:00:00", **kw))
    run(state.wa_add_message(contact, direction="in", kind="reply", body="What would it involve?", status="sent",
                             sent_at="2026-11-04T05:00:00", intent="question"))
    return contact


def _li_talking(web, slug="jane-doe-1", **kw):
    state = web.state
    lead, _ = run(state.li_upsert_lead(slug, name="Jane Doe", stage="replied",
                                       last_inbound_at="2026-11-04T05:00:00", **kw))
    run(state.li_add_message(lead, direction="in", body="What would it involve?", status="sent",
                             sent_at="2026-11-04T05:00:00", intent="question"))
    return lead


def test_a_whatsapp_reply_is_queued_approved_by_you(web):
    c = _wa_talking(web)
    res = web.post(f"/api/whatsapp/contacts/{c}/reply", json={"body": "A short chat would tell, Jane."})
    assert res.status_code == 200 and res.json()["success"]
    m = run(web.state.wa_get_message(res.json()["id"]))
    assert m["status"] == "approved" and m["approved_by"] == "you" and m["kind"] == "reply"
    assert m["direction"] == "out" and m["body"] == "A short chat would tell, Jane."
    assert m["sent_at"] is None        # nothing has gone: it waits for a run
    data = web.get("/api/whatsapp").json()
    assert data["queue"][0]["id"] == m["id"] and data["queue"][0]["approved_by"] == "you"
    assert data["conversations"][0]["draft"]["status"] == "approved"


def test_a_linkedin_reply_is_queued_approved_by_you(web):
    lead = _li_talking(web)
    res = web.post(f"/api/linkedin/leads/{lead}/reply", json={"body": "A short chat would tell, Jane."})
    assert res.status_code == 200 and res.json()["success"]
    m = run(web.state.li_get_message(res.json()["id"]))
    assert m["status"] == "approved" and m["approved_by"] == "you" and m["kind"] == "reply" and m["sent_at"] is None


def test_yours_replaces_harveys_draft_so_both_dont_go(web):
    c = _wa_talking(web)
    drafted = run(web.state.wa_add_message(c, kind="reply", body="Harvey's draft"))
    res = web.post(f"/api/whatsapp/contacts/{c}/reply", json={"body": "Mine instead."}).json()
    assert res["replaced"] == 1
    assert run(web.state.wa_get_message(drafted))["status"] == "skipped"
    lead = _li_talking(web)
    drafted = run(web.state.li_add_message(lead, kind="reply", body="Harvey's draft"))
    res = web.post(f"/api/linkedin/leads/{lead}/reply", json={"body": "Mine instead."}).json()
    assert res["replaced"] == 1
    assert run(web.state.li_get_message(drafted))["status"] == "skipped"


def test_a_second_reply_of_yours_doesnt_replace_your_first(web):
    c = _wa_talking(web)
    one = web.post(f"/api/whatsapp/contacts/{c}/reply", json={"body": "One."}).json()
    two = web.post(f"/api/whatsapp/contacts/{c}/reply", json={"body": "Two."}).json()
    assert two["replaced"] == 0 and run(web.state.wa_get_message(one["id"]))["status"] == "approved"


def test_after_a_no_the_box_is_closed_and_says_why(web):
    state = web.state
    c = _wa_talking(web)
    run(state.wa_set_stage(c, "declined", no_message_reason="they said no on WhatsApp"))
    convo = web.get("/api/whatsapp").json()["conversations"][0]
    assert "they said no on WhatsApp" in convo["reply_blocked"]
    res = web.post(f"/api/whatsapp/contacts/{c}/reply", json={"body": "Please reconsider."})
    assert res.status_code == 409 and "they said no" in res.json()["message"]
    assert [m["kind"] for m in run(state.wa_messages(contact_id=c, direction="out"))] == []

    lead = _li_talking(web)
    run(state.li_set_stage(lead, "declined"))
    convo = web.get("/api/linkedin").json()["conversations"][0]
    assert "they said no" in convo["reply_blocked"]
    res = web.post(f"/api/linkedin/leads/{lead}/reply", json={"body": "Please reconsider."})
    assert res.status_code == 409 and "never contacts them again" in res.json()["message"]
    assert run(state.li_messages(lead_id=lead, direction="out")) == []


@pytest.mark.parametrize("status,word", [("opted_out", "opted out"), ("closed_lost", "by email")])
def test_a_no_by_email_closes_the_box_on_both_channels(web, status, word):
    pid = run(web.state.add_prospect(Prospect(first_name="Jane", last_name="Doe", title="Founder",
                                              email="jane@example.org", status=status)))
    c = _wa_talking(web, prospect_id=pid)
    assert word in web.get("/api/whatsapp").json()["conversations"][0]["reply_blocked"]
    assert web.post(f"/api/whatsapp/contacts/{c}/reply", json={"body": "Hi."}).status_code == 409
    lead = _li_talking(web, prospect_id=pid)
    assert word in web.get("/api/linkedin").json()["conversations"][0]["reply_blocked"]
    assert web.post(f"/api/linkedin/leads/{lead}/reply", json={"body": "Hi."}).status_code == 409


def test_the_do_not_contact_list_closes_the_box(web, tmp_path):
    path = tmp_path / "harvey.yaml"
    data = yaml.safe_load(path.read_text())
    data["compliance"] = {"do_not_contact": ["jane@example.org"]}
    path.write_text(yaml.safe_dump(data))
    c = _wa_talking(web, email="jane@example.org")
    assert "do-not-contact" in web.get("/api/whatsapp").json()["conversations"][0]["reply_blocked"]
    assert web.post(f"/api/whatsapp/contacts/{c}/reply", json={"body": "Hi."}).status_code == 409


def test_a_number_not_on_whatsapp_closes_the_box(web):
    other, _ = run(web.state.wa_add_contact(phone=wf.SAM, source="website chat", stage="no_whatsapp"))
    run(web.state.wa_add_message(other, kind="first", body=wf.FIRST, status="sent", sent_at="2026-11-04T05:00:00"))
    res = web.post(f"/api/whatsapp/contacts/{other}/reply", json={"body": "Hi."})
    assert res.status_code == 409 and "isn't on WhatsApp" in res.json()["message"]


def test_nobody_gets_a_reply_before_they_have_written(web):
    """The box isn't a way round the first message and the one follow-up."""
    state = web.state
    c, _ = run(state.wa_add_contact(phone=JANE, source="website chat", stage="messaged"))
    run(state.wa_add_message(c, kind="first", body=wf.FIRST, status="sent", sent_at="2026-11-04T05:00:00"))
    convo = web.get("/api/whatsapp").json()["conversations"][0]
    assert "haven't written back" in convo["reply_blocked"]
    assert web.post(f"/api/whatsapp/contacts/{c}/reply", json={"body": "Hi again."}).status_code == 409

    lead, _ = run(state.li_upsert_lead("jane-doe-1", name="Jane Doe", stage="messaged"))
    run(state.li_add_message(lead, kind="message", body="Hello", status="sent", sent_at="2026-11-04T05:00:00"))
    assert "haven't written back" in web.get("/api/linkedin").json()["conversations"][0]["reply_blocked"]
    assert web.post(f"/api/linkedin/leads/{lead}/reply", json={"body": "Hi again."}).status_code == 409


def test_the_box_closes_at_the_unanswered_limit_counting_whats_queued(web):
    c = _wa_talking(web)
    for i in range(2):
        run(web.state.wa_add_message(c, kind="reply", body=f"Sent {i}", status="sent",
                                     sent_at=f"2026-11-04T06:0{i}:00"))
    assert web.post(f"/api/whatsapp/contacts/{c}/reply", json={"body": "The third."}).status_code == 200
    res = web.post(f"/api/whatsapp/contacts/{c}/reply", json={"body": "A fourth."})
    assert res.status_code == 409 and "without a reply" in res.json()["message"]
    assert "without a reply" in web.get("/api/whatsapp").json()["conversations"][0]["reply_blocked"]


def test_the_linkedin_box_closes_at_the_unanswered_limit_too(web):
    lead = _li_talking(web)
    for i in range(3):
        run(web.state.li_add_message(lead, kind="reply", body=f"Sent {i}", status="sent",
                                     sent_at=f"2026-11-04T06:0{i}:00", dedupe=False))
    res = web.post(f"/api/linkedin/leads/{lead}/reply", json={"body": "A fourth."})
    assert res.status_code == 409 and "without a reply" in res.json()["message"]


def test_a_message_that_may_have_gone_closes_the_box_until_you_say(web):
    c = _wa_talking(web)
    m = run(web.state.wa_add_message(c, kind="reply", body="Maybe", status="maybe_sent", error="may have"))
    res = web.post(f"/api/whatsapp/contacts/{c}/reply", json={"body": "Hi."})
    assert res.status_code == 409 and "may have gone out" in res.json()["message"]
    assert web.post(f"/api/whatsapp/messages/{m}/not_sent").json()["success"]
    assert web.post(f"/api/whatsapp/contacts/{c}/reply", json={"body": "Hi."}).status_code == 200


@pytest.mark.parametrize("body,word", [
    ("", "empty"),
    ("See https://evil.example/x for more.", "booking"),
    ("It costs $500.", "price"),
    ("word " * 80, "under 80"),
])
def test_a_whatsapp_reply_is_held_to_the_same_rules_as_an_edit(web, body, word):
    c = _wa_talking(web)
    res = web.post(f"/api/whatsapp/contacts/{c}/reply", json={"body": body})
    assert res.status_code == 400 and word in res.json()["message"]
    assert run(web.state.wa_messages(contact_id=c, direction="out")) == []


def test_a_linkedin_reply_must_be_something_and_not_a_book(web):
    lead = _li_talking(web)
    assert web.post(f"/api/linkedin/leads/{lead}/reply", json={"body": "  "}).status_code == 400
    assert web.post(f"/api/linkedin/leads/{lead}/reply", json={"body": "x" * 2001}).status_code == 400
    assert run(web.state.li_messages(lead_id=lead, direction="out")) == []


def test_replying_to_nobody_is_a_404(web):
    assert web.post("/api/linkedin/leads/nope/reply", json={"body": "Hi"}).status_code == 404
    assert web.post("/api/whatsapp/contacts/nope/reply", json={"body": "Hi"}).status_code == 404


# ── ...and it really is the same queue, with the same limits ──

def _wa_world_with_reply(tmp_path, **cfg):
    w = wf.World(tmp_path, [wa_jane(thread=[("in", "What would it involve?")])], **cfg)
    c = w.contact(stage="replied", last_inbound_at="2026-11-04T05:00:00")
    run(w.state.wa_add_message(c, direction="in", kind="reply", body="What would it involve?", status="sent",
                               sent_at="2026-11-04T05:00:00", intent="question",
                               wa_id=f"false_{JANE[1:]}@c.us_0"))
    m = run(w.state.wa_add_message(c, kind="reply", body="A short chat would tell.", status="approved",
                                   approved_by="you"))
    return w, c, m


def test_a_whatsapp_reply_of_yours_goes_through_the_whole_recipe(tmp_path):
    w, _, m = _wa_world_with_reply(tmp_path)
    w.go()
    assert w.get(m)["status"] == "sent" and w.driver.enter_presses == 1


def test_a_whatsapp_reply_of_yours_waits_at_the_cap(tmp_path):
    w, _, m = _wa_world_with_reply(tmp_path, max_daily_messages=0)
    w.go()
    assert w.get(m)["status"] == "approved" and w.driver.enter_presses == 0


def test_a_whatsapp_reply_of_yours_waits_for_their_hours(tmp_path):
    from datetime import datetime, timezone
    w, _, m = _wa_world_with_reply(tmp_path)
    w.at = datetime(2026, 11, 4, 3, 0, tzinfo=timezone.utc)       # 03:00 in London
    w.go()
    assert w.get(m)["status"] == "approved" and w.driver.enter_presses == 0


def test_a_whatsapp_reply_of_yours_stops_if_they_said_no_meanwhile(tmp_path):
    w, c, m = _wa_world_with_reply(tmp_path)
    run(w.state.wa_set_stage(c, "declined", no_message_reason="they said no on WhatsApp"))
    w.go()
    assert w.get(m)["status"] == "skipped" and w.driver.enter_presses == 0


def _li_world_with_reply(tmp_path, **cfg):
    li = LiWorld(tmp_path, [li_jane()], **cfg)
    lead = run(li.state.li_upsert_lead("jane-doe-1", stage="replied", name="Jane Doe",
                                       profile_url="https://www.linkedin.com/in/jane-doe-1/",
                                       last_inbound_at="2026-11-04T05:00:00"))[0]
    run(li.state.li_add_message(lead, direction="in", body="What would it involve?", status="sent",
                                sent_at="2026-11-04T05:00:00", intent="question"))
    m = run(li.state.li_add_message(lead, kind="reply", body="A short chat would tell.", status="approved",
                                    approved_by="you"))
    return li, lead, m


def test_a_linkedin_reply_of_yours_goes_through_the_whole_recipe(tmp_path):
    li, _, m = _li_world_with_reply(tmp_path)
    li.go()
    assert run(li.state.li_get_message(m))["status"] == "sent" and len(li.driver.sent) == 1


def test_a_linkedin_reply_of_yours_waits_at_the_cap(tmp_path):
    li, _, m = _li_world_with_reply(tmp_path, max_daily_messages=0)
    li.go()
    assert run(li.state.li_get_message(m))["status"] == "approved" and li.driver.sent == []


def test_a_linkedin_reply_of_yours_stops_if_they_opted_out_by_email_meanwhile(tmp_path):
    li, lead, m = _li_world_with_reply(tmp_path)
    pid = run(li.state.add_prospect(Prospect(first_name="Jane", last_name="Doe", title="Founder",
                                             email="jane@example.org", status="opted_out")))
    run(li.state.li_update_lead(lead, prospect_id=pid))
    li.go()
    got = run(li.state.li_get_message(m))
    assert got["status"] == "skipped" and "opted out" in got["error"] and li.driver.sent == []


def test_a_linkedin_reply_of_yours_stops_at_the_unanswered_limit(tmp_path):
    li, lead, m = _li_world_with_reply(tmp_path)
    for i in range(3):
        run(li.state.li_add_message(lead, kind="reply", body=f"Sent {i}", status="sent",
                                    sent_at=f"2026-11-04T06:0{i}:00", dedupe=False))
    li.go()
    got = run(li.state.li_get_message(m))
    assert got["status"] == "skipped" and "without a reply" in got["error"] and li.driver.sent == []


# ── Add a contact ──

def test_adding_a_contact_queues_them_and_they_show_as_due(web):
    res = web.post("/api/whatsapp/contacts", json={"phone": JANE, "name": "Jane Doe",
                                                   "source": "website contact form"})
    assert res.status_code == 200
    data = res.json()
    assert data["success"] and data["status"] == "queued" and "approval" in data["message"]
    got = run(web.state.wa_get_contact(data["contact_id"]))
    assert got["stage"] == "queued" and got["phone"] == JANE and got["source"] == "website contact form"
    overview = web.get("/api/whatsapp").json()
    assert overview["funnel"]["queued"] == 1
    assert overview["follow_ups_due"][0]["which"] == "first message"
    assert overview["follow_ups_due"][0]["contact"]["id"] == got["id"]


@pytest.mark.parametrize("body", [
    {"phone": JANE, "name": "Jane Doe"},
    {"phone": JANE, "name": "Jane Doe", "source": "   "},
])
def test_a_source_is_required(web, body):
    res = web.post("/api/whatsapp/contacts", json=body)
    assert res.status_code == 400 and "source" in res.json()["message"]
    assert run(web.state.wa_contacts()) == []


def test_a_number_is_required(web):
    res = web.post("/api/whatsapp/contacts", json={"name": "Jane Doe", "source": "website chat"})
    assert res.status_code == 400 and "number" in res.json()["message"]


@pytest.mark.parametrize("phone", [
    "+447700900123, +447700900124", "+447700900123;+447700900124", "+447700900123\n+447700900124",
    "+447700900123 +447700900124", "+447700900123\t+447700900124",
])
def test_a_pasted_list_is_refused_outright(web, phone):
    res = web.post("/api/whatsapp/contacts", json={"phone": phone, "source": "website chat"})
    assert res.status_code == 400 and "One number at a time" in res.json()["message"]
    assert run(web.state.wa_contacts()) == []


@pytest.mark.parametrize("phone", [["+447700900123", "+447700900124"], {"a": 1}, 447700900123])
def test_only_one_number_as_text_is_taken(web, phone):
    res = web.post("/api/whatsapp/contacts", json={"phone": phone, "source": "website chat"})
    assert res.status_code == 400 and run(web.state.wa_contacts()) == []


def test_there_is_no_upload_route(web):
    assert web.post("/api/whatsapp/contacts/import", json={}).status_code in (404, 405)
    assert web.post("/api/whatsapp/contacts/upload", json={}).status_code in (404, 405)


def test_a_number_with_no_country_is_held_and_says_what_to_fix(web):
    res = web.post("/api/whatsapp/contacts", json={"phone": "07700 900123", "name": "Jane Doe",
                                                   "source": "website chat"})
    assert res.status_code == 200
    data = res.json()
    assert data["status"] == "needs_review"
    assert "no + and no country" in data["message"]      # what's wrong
    assert "+" in data["message"] and "country code" in data["message"]      # what to do
    assert "Numbers to check" in data["message"]         # where to do it
    got = run(web.state.wa_get_contact(data["contact_id"]))
    assert got["stage"] == "needs_review" and got["phone"] == ""
    assert web.get("/api/whatsapp").json()["review"][0]["raw_phone"] == "07700 900123"


@pytest.mark.parametrize("source", ["scraped from their site", "bought list", "cold list", "import"])
def test_a_source_that_says_they_didnt_give_it_is_refused(web, source):
    res = web.post("/api/whatsapp/contacts", json={"phone": JANE, "source": source})
    assert res.status_code == 409 and res.json()["status"] == "refused" and "Refused" in res.json()["message"]
    assert run(web.state.wa_contacts()) == []


def test_someone_who_said_no_cant_be_added_again(web):
    c = _wa_talking(web)
    run(web.state.wa_set_stage(c, "declined"))
    res = web.post("/api/whatsapp/contacts", json={"phone": JANE, "source": "website chat"})
    assert res.status_code == 409 and "said no" in res.json()["message"]


def test_adding_twice_changes_nothing(web):
    assert web.post("/api/whatsapp/contacts", json={"phone": JANE, "source": "website chat"}).json()["status"] == "queued"
    res = web.post("/api/whatsapp/contacts", json={"phone": JANE, "source": "website form"}).json()
    assert res["status"] == "duplicate" and "Already here" in res["message"]
    assert len(run(web.state.wa_contacts())) == 1
    assert run(web.state.wa_contacts())[0]["source"] == "website chat"


def test_your_own_second_phone_goes_in_like_anyone_else(web):
    """The first live test: it waits for approval like any first message."""
    res = web.post("/api/whatsapp/contacts", json={"phone": "+15555550111", "name": "Sam Owner",
                                                   "source": "my own second phone, for a test"})
    assert res.json()["status"] == "queued"
    assert run(web.state.wa_messages(contact_id=res.json()["contact_id"])) == []    # nothing drafted, let alone sent


def test_the_form_is_on_the_page(web):
    html = web.get("/").text
    for element in ('id="wa-add-phone"', 'id="wa-add-name"', 'id="wa-add-source"', 'id="wa-add-result"'):
        assert element in html
    assert 'type="file"' not in html.split('id="whatsapp"')[1].split('id="learning"')[0]


# ── Everything visible ──

def test_every_conversation_shows_past_the_old_forty_with_its_whole_history(web):
    state = web.state
    for i in range(45):
        c, _ = run(state.wa_add_contact(phone=f"+1415555{i:04d}", source="website chat", stage="replied",
                                        last_inbound_at=f"2026-11-04T05:{i:02d}:00", name=f"Person {i}"))
        run(state.wa_add_message(c, direction="in", kind="reply", body="Hello", status="sent",
                                 sent_at=f"2026-11-04T05:{i:02d}:00"))
    for j in range(9):
        run(state.wa_add_message(c, kind="reply", body=f"Line {j}", status="sent", sent_at=f"2026-11-04T06:{j:02d}:00"))
    convos = web.get("/api/whatsapp").json()["conversations"]
    assert len(convos) == 45
    assert convos[0]["contact"]["name"] == "Person 44"            # latest first
    assert [m["body"] for m in convos[0]["thread"]][:2] == ["Hello", "Line 0"]
    assert len(convos[0]["thread"]) == 10                         # not the old last six


def test_someone_who_never_answered_is_a_conversation_too(web):
    state = web.state
    c, _ = run(state.wa_add_contact(phone=JANE, source="website chat", stage="messaged", name="Jane Doe"))
    run(state.wa_add_message(c, kind="first", body=wf.FIRST, status="sent", sent_at="2026-11-04T05:00:00"))
    lead, _ = run(state.li_upsert_lead("jane-doe-1", name="Jane Doe", stage="messaged"))
    run(state.li_add_message(lead, kind="message", body="Hello Jane", status="sent", sent_at="2026-11-04T05:00:00"))
    assert web.get("/api/whatsapp").json()["conversations"][0]["thread"][0]["body"] == wf.FIRST
    assert web.get("/api/linkedin").json()["conversations"][0]["thread"][0]["body"] == "Hello Jane"


def test_an_invite_alone_isnt_a_conversation_but_shows_in_the_history_of_one(web):
    state = web.state
    only, _ = run(state.li_upsert_lead("sam-lane-2", name="Sam Lane", stage="invited"))
    run(state.li_add_message(only, kind="invite_note", body="", status="sent", sent_at="2026-11-03T05:00:00"))
    assert web.get("/api/linkedin").json()["conversations"] == []
    lead = _li_talking(web)
    run(state.li_add_message(lead, kind="invite_note", body="Hi", status="sent", sent_at="2026-11-01T05:00:00"))
    thread = web.get("/api/linkedin").json()["conversations"][0]["thread"]
    assert [m["kind"] for m in thread] == ["invite_note", "message"]


def test_a_message_that_may_have_gone_or_was_stopped_stays_in_the_history(web):
    state = web.state
    c = _wa_talking(web)
    run(state.wa_add_message(c, kind="reply", body="Might have", status="maybe_sent", error="Enter was pressed"))
    stopped = run(state.wa_add_message(c, kind="reply", body="Too late", status="approved", approved_by="you"))
    run(state.wa_skip(stopped, "they wrote after this was drafted"))
    plain = run(state.wa_add_message(c, kind="reply", body="Harvey's, skipped by you", status="draft"))
    run(state.wa_skip(plain))
    thread = web.get("/api/whatsapp").json()["conversations"][0]["thread"]
    by_body = {m["body"]: m for m in thread}
    assert by_body["Might have"]["status"] == "maybe_sent"
    assert by_body["Too late"]["status"] == "not_sent" and "they wrote" in by_body["Too late"]["note"]
    assert "Harvey's, skipped by you" not in by_body


def test_whatsapp_shows_follow_ups_due(web):
    state = web.state
    c, _ = run(state.wa_add_contact(phone=JANE, source="website chat", stage="messaged", name="Jane Doe",
                                    next_action_at="2026-01-01T00:00:00"))
    run(state.wa_add_message(c, kind="first", body=wf.FIRST, status="sent", sent_at="2026-01-01T00:00:00"))
    due = web.get("/api/whatsapp").json()["follow_ups_due"]
    assert due[0]["which"] == "follow-up" and due[0]["contact"]["id"] == c and due[0]["due_at"]


def test_a_follow_up_not_due_yet_or_held_isnt_listed(web):
    from datetime import datetime, timezone
    state = web.state
    now = datetime.now(timezone.utc).replace(tzinfo=None).isoformat(timespec="seconds")
    c, _ = run(state.wa_add_contact(phone=JANE, source="website chat", stage="messaged"))
    run(state.wa_add_message(c, kind="first", body=wf.FIRST, status="sent", sent_at=now))
    assert web.get("/api/whatsapp").json()["follow_ups_due"] == []
    held, _ = run(state.wa_add_contact(phone=wf.SAM, source="website chat", stage="queued",
                                       no_message_reason="they're in a live email conversation"))
    assert held and web.get("/api/whatsapp").json()["follow_ups_due"] == []


@pytest.mark.parametrize("channel,guards,kw", [("linkedin", li_guards, {}), ("whatsapp", wa_guards, {})])
def test_a_warning_outlives_its_pause_and_your_own_pause_isnt_one(web, channel, guards, kw):
    from datetime import datetime
    long_ago = datetime(2026, 1, 1)
    run(guards.set_pause(web.state, "They warned about unusual activity", kind="unusual_activity", hours=48, now=long_ago))
    run(guards.set_pause(web.state, "Paused by you", kind="manual", hours=None, now=long_ago))
    run(guards.clear_pause(web.state))
    data = web.get(f"/api/{channel}").json()
    assert data["pause"] is None
    assert [w["kind"] for w in data["warnings"]] == ["unusual_activity"]
    assert "unusual activity" in data["warnings"][0]["reason"]


def test_a_finished_run_shows_with_caps_and_funnel_on_both_tabs(web):
    for channel in ("linkedin", "whatsapp"):
        data = web.get(f"/api/{channel}").json()
        for key in ("funnel", "queue", "conversations", "follow_ups_due", "used", "caps", "last_run", "warnings",
                    "pause", "connection"):
            assert key in data, (channel, key)


def test_the_page_has_every_new_place(web):
    html = web.get("/").text
    for element in ("li-connection", "wa-connection", "li-warnings", "wa-warnings", "wa-due"):
        assert f'id="{element}"' in html


# ── Today ──

def _today_keys(web):
    return [i["key"] for i in web.get("/api/today").json()["items"]]


@pytest.mark.parametrize("channel,guards,key", [("linkedin", li_guards, LI_KEY), ("whatsapp", wa_guards, WA_KEY)])
def test_on_but_signed_out_gets_a_card_pointing_at_reconnect(web, channel, guards, key):
    connected(web)
    run(web.state.set_setting(key, "on"))
    run(guards.set_pause(web.state, "gone", kind="signed_out", hours=None))
    items = web.get("/api/today").json()["items"]
    card = next(i for i in items if i["key"] == f"{channel}-signed-out")
    assert card["tab"] == channel and "Reconnect" in card["detail"] and card["tone"] == "bad"
    assert f"{channel}-paused" not in [i["key"] for i in items]       # one card, not two


@pytest.mark.parametrize("channel,guards", [("linkedin", li_guards), ("whatsapp", wa_guards)])
def test_off_and_signed_out_says_nothing(web, channel, guards):
    connected(web)
    run(guards.set_pause(web.state, "gone", kind="signed_out", hours=None))
    keys = _today_keys(web)
    assert f"{channel}-signed-out" not in keys and f"{channel}-paused" not in keys


@pytest.mark.parametrize("channel,guards,key", [("linkedin", li_guards, LI_KEY), ("whatsapp", wa_guards, WA_KEY)])
def test_paused_by_a_warning_gets_a_card_with_the_reason(web, channel, guards, key):
    connected(web)
    run(web.state.set_setting(key, "on"))
    run(guards.set_pause(web.state, "They warned about unusual activity", kind="unusual_activity", hours=48))
    card = next(i for i in web.get("/api/today").json()["items"] if i["key"] == f"{channel}-paused")
    assert "unusual activity" in card["detail"] and card["tab"] == channel and card["tone"] == "bad"


@pytest.mark.parametrize("channel", ["linkedin", "whatsapp"])
def test_on_in_the_config_but_never_connected_gets_a_card(web, tmp_path, channel):
    path = tmp_path / "harvey.yaml"
    data = yaml.safe_load(path.read_text())
    data["channels"][channel]["enabled"] = True
    path.write_text(yaml.safe_dump(data))
    assert f"{channel}-not-connected" in _today_keys(web)


def test_a_channel_off_and_never_connected_says_nothing(web):
    keys = _today_keys(web)
    assert not any(k.startswith(("linkedin-", "whatsapp-")) for k in keys)
