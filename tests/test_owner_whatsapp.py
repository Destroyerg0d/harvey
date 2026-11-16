"""Updates to the owner's phone, and the owner's instructions from it (owner,
2026-11-16). Every number, name and address here is fictional."""

import json
from types import SimpleNamespace as NS

import pytest

from harvey import owner_inbox, owner_updates
from harvey.meeting_alert import queue_to_owner
from tests.test_outbox_native import state  # noqa: F401  (fixture)

OWNER = "+15555550199"


def cfg():
    return NS(channels=NS(whatsapp=NS(alert_number=OWNER, number="+15555550100", enabled=True,
                                      autopilot=False),
                          linkedin=NS(enabled=True, autopilot=False)),
              persona=NS(company="Studio Example"),
              product=NS(offer=NS(meeting_window="10:00-12:00", meeting_timezone="Asia/Kolkata",
                                  meeting_link="", chat_link="")))


async def to_owner(state):
    contact = await state.wa_get_contact_by_phone(OWNER)
    return [] if contact is None else [m["body"] for m in await state.wa_messages(contact_id=contact["id"])]


# ── Updates ──

def test_an_update_reads_as_a_few_short_lines():
    rows = [("email_sent", {"kind": "sequence", "step": 1}), ("email_sent", {"kind": "sequence", "step": 2}),
            ("email_sent", {"kind": "sequence", "step": 3}), ("email_sent", {"kind": "reply"}),
            ("reply_queued", {"prospect_email": "ann@shop.example", "intent": "interested"}),
            ("escalation", {"prospect_email": "bo@shop.example", "reason": "a complaint"}),
            ("linkedin_run", {"outcome": "done", "sent_invites": 3, "inbound": 1}),
            ("whatsapp_run", {"outcome": "done", "sent": 0, "inbound": 0}),
            ("linkedin_inbox_check", {"outcome": "stopped: signed out"}),
            ("idle", {})]
    lines = owner_updates.lines_for(rows)
    assert lines[0] == "Sent 1 first email, 2 follow-ups, 1 reply."
    assert "ann@shop.example replied (interested); Harvey answered." in lines
    assert "Needs you: bo@shop.example (a complaint)" in lines
    assert "LinkedIn: 3 invites, 1 reply in." in lines
    assert "LinkedIn stopped: signed out." in lines
    assert not any("WhatsApp" in l for l in lines)        # a run that did nothing says nothing


@pytest.mark.asyncio
async def test_updates_start_from_now_then_report_what_happened_once(state):
    assert not await owner_updates.send(state, cfg())        # sets the starting point
    await state.log_action("email_sent", "sender", {"kind": "sequence", "step": 1})
    await state.set_setting(owner_updates.LAST_KEY, "2000-01-01 00:00:00")
    assert await owner_updates.send(state, cfg())
    [message] = await to_owner(state)
    assert message.startswith("Harvey update") and "Sent 1 first email." in message
    assert not await owner_updates.send(state, cfg())        # nothing new since


@pytest.mark.asyncio
async def test_no_update_in_quiet_hours_or_without_an_alert_number(state):
    await state.set_setting(owner_updates.LAST_KEY, "2000-01-01 00:00:00")
    await state.log_action("email_sent", "sender", {"kind": "reply"})
    assert not await owner_updates.send(state, cfg(), quiet=True)
    no_number = cfg()
    no_number.channels.whatsapp.alert_number = ""
    assert not await owner_updates.send(state, no_number)
    assert await owner_updates.send(state, cfg())            # it all goes once allowed


# ── Instructions ──

class Msg:
    def __init__(self, body, wa_id="", direction="in"):
        self.body, self.wa_id, self.direction, self.text = body, wa_id, direction, True


class Brain:
    def __init__(self, answer):
        self.answer, self.prompts = answer, []
        self.paused_reason, self.failed_calls = "", 0

    async def think(self, prompt, session_id=None, agent="", task=""):
        self.prompts.append(prompt)
        return json.dumps(self.answer)


async def started(state):
    """The chat as it was before: kept as seen, never acted on."""
    assert await owner_inbox.record(state, [Msg("got it", "w0")]) == 0


@pytest.mark.asyncio
async def test_old_messages_are_seen_and_new_ones_acted_on_once(state):
    await started(state)
    assert await owner_inbox.record(state, [Msg("got it", "w0"), Msg("pause linkedin", "w1")]) == 1
    assert await owner_inbox.record(state, [Msg("pause linkedin", "w1")]) == 0


@pytest.mark.asyncio
async def test_a_pause_is_carried_out_and_answered(state):
    await started(state)
    await owner_inbox.record(state, [Msg("pause linkedin for today", "w1")])
    brain = Brain({"kind": "action", "action": "pause", "channel": "linkedin", "reply": "LinkedIn is paused."})
    assert await owner_inbox.handle(state, brain, cfg()) == 1
    assert await state.get_setting("linkedin:enabled") == "off"
    assert "Harvey: LinkedIn is paused." in await to_owner(state)
    assert "pause linkedin for today" in brain.prompts[0]


@pytest.mark.asyncio
async def test_pause_all_stops_email_too_and_resume_restarts(state):
    await started(state)
    await owner_inbox.record(state, [Msg("stop everything", "w1")])
    await owner_inbox.handle(state, Brain({"kind": "action", "action": "pause", "channel": "all"}), cfg())
    assert await state.get_setting("sending_paused") and await state.get_setting("whatsapp:enabled") == "off"
    await owner_inbox.record(state, [Msg("start again", "w2")])
    await owner_inbox.handle(state, Brain({"kind": "action", "action": "resume", "channel": "all"}), cfg())
    assert not await state.get_setting("sending_paused") and await state.get_setting("whatsapp:enabled") == "on"


@pytest.mark.asyncio
async def test_a_setting_is_changed_on_the_running_config_and_kept(state):
    await started(state)
    config = cfg()
    await owner_inbox.record(state, [Msg("make meetings 11 to 1", "w1")])
    await owner_inbox.handle(state, Brain({"kind": "action", "action": "set", "setting": "meeting_window",
                                           "value": "11:00-13:00"}), config)
    assert config.product.offer.meeting_window == "11:00-13:00"
    fresh = cfg()
    await owner_inbox.apply_overrides(state, fresh)          # after a restart
    assert fresh.product.offer.meeting_window == "11:00-13:00"


@pytest.mark.asyncio
async def test_a_setting_harvey_cant_take_is_refused_and_said(state):
    await started(state)
    await owner_inbox.record(state, [Msg("set the api key to x", "w1")])
    await owner_inbox.handle(state, Brain({"kind": "action", "action": "set", "setting": "api_key",
                                           "value": "x"}), cfg())
    [reply] = await to_owner(state)
    assert "couldn't do that" in reply
    [row] = [r for r in await owner_inbox.notes(state) if r["status"] == "failed"]
    assert row["body"] == "set the api key to x"


@pytest.mark.asyncio
async def test_do_not_contact_goes_in_the_side_list(state, tmp_path, monkeypatch):
    import harvey.config as config_mod
    monkeypatch.setattr(config_mod, "PROJECT_ROOT", tmp_path)
    await started(state)
    await owner_inbox.record(state, [Msg("never email rival.example", "w1")])
    await owner_inbox.handle(state, Brain({"kind": "action", "action": "do_not_contact",
                                           "entry": "rival.example"}), cfg())
    assert config_mod.extra_do_not_contact() == ["rival.example"]
    assert "rival.example" in config_mod.live_do_not_contact(NS(compliance=NS(do_not_contact=[])))


@pytest.mark.parametrize("kind, status", [("for_developer", "for_developer"), ("answer", "for_developer"),
                                          ("unclear", "asked"), ("question", "done")])
@pytest.mark.asyncio
async def test_what_isnt_an_action_is_answered_and_kept_where_it_belongs(state, kind, status):
    await started(state)
    await owner_inbox.record(state, [Msg("something", "w1")])
    await owner_inbox.handle(state, Brain({"kind": kind, "action": "none", "reply": "Okay."}), cfg())
    rows = await owner_inbox._rows(state, "SELECT status FROM owner_messages WHERE wa_id = 'w1'")
    assert rows[0]["status"] == status
    assert "Harvey: Okay." in await to_owner(state)


@pytest.mark.asyncio
async def test_a_question_from_a_coding_session_reaches_the_phone(state):
    assert await owner_inbox.ask(state, cfg(), "Should reviews mention prices?")
    assert any("a question from your developer" in m for m in await to_owner(state))
    assert [r["kind"] for r in await owner_inbox.notes(state)] == ["question"]


# ── The WhatsApp agent reads your chat ──

def test_whatsapp_reads_your_unread_chat_and_answers_in_the_same_run(tmp_path):
    from tests.whatsapp_fakes import FakeBrain, Person, World, run

    class Owners(FakeBrain):
        async def think(self, prompt, session_id=None, agent="", task="", **kw):
            if task == "owner_message":
                return json.dumps({"kind": "action", "action": "pause", "channel": "linkedin",
                                   "reply": "Paused LinkedIn."})
            return await super().think(prompt, session_id, agent, task, **kw)

    w = World(tmp_path, [Person(phone=OWNER, unread=True,
                                thread=[("out", "Harvey: test alert"), ("in", "pause linkedin please")])],
              brain=Owners(), alert_number=OWNER)
    run(queue_to_owner(w.state, w.config, "Harvey: test alert"))
    run(w.state.set_setting(owner_inbox.STARTED_KEY, "2026-11-01T00:00:00"))
    w.go()
    assert run(w.state.get_setting("linkedin:enabled")) == "off"
    contact = run(w.state.wa_get_contact_by_phone(OWNER))
    bodies = [m["body"] for m in w.msgs(contact["id"], direction="out")]
    assert "Harvey: Paused LinkedIn." in bodies
