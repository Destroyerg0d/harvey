"""WhatsApp and LinkedIn talk like a person texting (owner, 2026-11-19): a few
short messages at once, their name once in a conversation, and a catch-up
when a chat goes quiet. No browser: the pretend WhatsApp and LinkedIn from the
fakes. Every person and number is invented."""

from datetime import timedelta, timezone

import pytest

from harvey.agents.linkedin_agent import AUTOPILOT_KEY as LI_AUTOPILOT
from harvey.agents.whatsapp_agent import AUTOPILOT_KEY as WA_AUTOPILOT, catch_up_due
from harvey.chat_style import parts, strip_repeat_name
from tests.whatsapp_fakes import JANE, OPEN, FakeBrain, Person, World, run

THREE = "Ah, that makes sense.\n\nMost stores we see lose people at checkout.\n\nIs yours on Shopify?"


def naive(dt):
    return dt.astimezone(timezone.utc).replace(tzinfo=None)


def test_a_draft_is_a_few_messages_and_the_name_is_used_once():
    assert parts(THREE) == ["Ah, that makes sense.", "Most stores we see lose people at checkout.",
                            "Is yours on Shopify?"]
    assert parts("a\n\nb\n\nc\n\nd") == ["a", "b", "c\nd"]                # three at most
    assert strip_repeat_name("Jane, got it.\n\nWant the list?", "Jane", ["Hi Jane, you left..."]) == \
        "Got it.\n\nWant the list?"
    assert strip_repeat_name("Hi Jane, welcome.\n\nJane, how is it going?", "Jane", []) == \
        "Hi Jane, welcome.\n\nHow is it going?"
    assert strip_repeat_name("Janet, hi", "Jane", ["Jane"]) == "Janet, hi"


# ── WhatsApp ──

def known_in(w, contact_id, body, index=0):
    """Their message already read, with the id the pretend chat gives it."""
    run(w.state.wa_add_message(contact_id, direction="in", kind="reply", body=body, status="sent",
                               sent_at=naive(OPEN) - timedelta(hours=1),
                               wa_id=f"false_{JANE[1:]}@c.us_{index}", intent="question"))

def test_a_reply_goes_as_three_messages_in_order_and_counts_once(tmp_path):
    w = World(tmp_path, [Person(phone=JANE, thread=[("in", "How much for a store?")])])
    c = w.contact(stage="replied")
    known_in(w, c, "How much for a store?")
    m = w.approved(c, kind="reply", body=THREE)
    report = w.go()
    assert [body for _, body in w.driver.sent] == parts(THREE)
    assert w.get(m)["status"] == "sent" and report["sent"] == 1
    assert run(w.agent().pacing.used())["messages_today"] == 1
    # Reading the chat again doesn't store the three as messages you sent by hand.
    w.go()
    assert not [x for x in w.msgs(c, direction="out") if x["approved_by"] == "you on WhatsApp"]


def test_a_part_that_doesn_t_go_after_others_did_is_never_sent_again(tmp_path):
    def typing(s):
        return s + " x" if s.startswith("Most stores") else s       # the second part won't match
    w = World(tmp_path, [Person(phone=JANE, thread=[("in", "How much?")])], driver_kwargs={"typing": typing})
    c = w.contact(stage="replied")
    known_in(w, c, "How much?")
    m = w.approved(c, kind="reply", body=THREE)
    w.go()
    row = w.get(m)
    assert [body for _, body in w.driver.sent] == ["Ah, that makes sense."]
    assert row["status"] == "maybe_sent" and row["error"].startswith("sent 1 of 3 messages")


def test_a_reply_draft_drops_their_name_once_used(tmp_path):
    w = World(tmp_path, [Person(phone=JANE, unread=True, thread=[
        ("out", "Hi Jane, you left this number on our website."), ("in", "Yes, what do you do?")])],
        brain=FakeBrain(drafts={"draft_reply": "Jane, we build and fix online stores.\n\nWhat do you sell?"}))
    c = w.contact(stage="messaged")
    a_day_ago = naive(OPEN) - timedelta(days=1)
    run(w.state.wa_add_message(c, direction="out", kind="first", status="sent", sent_at=a_day_ago,
                               created_at=a_day_ago, body="Hi Jane, you left this number on our website."))
    w.go()
    [draft] = [x for x in w.msgs(c, direction="out") if x["kind"] == "reply"]
    assert draft["body"] == "We build and fix online stores.\n\nWhat do you sell?"


def test_a_quiet_chat_gets_at_most_two_catch_ups():
    from types import SimpleNamespace as NS
    now = naive(OPEN)
    cfg = NS(max_catch_ups=2, catch_up_after_days=2)

    def row(direction, kind, days_ago):
        return {"direction": direction, "kind": kind, "status": "sent",
                "sent_at": (now - timedelta(days=days_ago)).isoformat()}
    talk = [row("out", "first", 9), row("in", "reply", 8), row("out", "reply", 8)]
    assert catch_up_due(talk, cfg, now)
    assert not catch_up_due(talk[:-1] + [row("out", "reply", 1)], cfg, now)            # too soon
    assert not catch_up_due(talk + [row("in", "reply", 3)], cfg, now)                  # they wrote last
    assert catch_up_due(talk + [row("out", "catch_up", 5)], cfg, now)                  # one so far
    assert not catch_up_due(talk + [row("out", "catch_up", 5), row("out", "catch_up", 3)], cfg, now)
    assert not catch_up_due([row("out", "first", 9)], cfg, now)                        # they never wrote


def test_the_catch_up_is_drafted_for_a_quiet_chat(tmp_path):
    w = World(tmp_path, [Person(phone=JANE)],
              brain=FakeBrain(drafts={"draft_catch_up": "Still thinking about the checkout?"}))
    run(w.state.set_setting(WA_AUTOPILOT, "on"))
    c = w.contact(stage="replied")
    for direction, kind, days in (("out", "first", 9), ("in", "reply", 8), ("out", "reply", 8)):
        run(w.state.wa_add_message(c, direction=direction, kind=kind, status="sent", body=f"{kind} {days}",
                                   sent_at=naive(OPEN) - timedelta(days=days)))
    run(w.agent().draft_messages())
    [catch] = [x for x in w.msgs(c, direction="out") if x["kind"] == "catch_up"]
    assert catch["body"] == "Still thinking about the checkout?" and catch["status"] == "approved"


# ── LinkedIn ──

def test_a_linkedin_message_goes_as_its_parts(tmp_path):
    from tests.linkedin_fakes import Person as LiPerson
    from tests.test_linkedin_agent import World as LiWorld, run as li_run
    w = LiWorld(tmp_path, [LiPerson(slug="jane-doe-1", name="Jane Doe", connected=True,
                                    connected_on="Oct 28, 2026")])
    lead = w.lead("jane-doe-1", stage="accepted", name="Jane Doe",
                  profile_url="https://www.linkedin.com/in/jane-doe-1/")
    m = li_run(w.state.li_add_message(lead, body=THREE, status="approved", approved_by="you"))
    report = w.go()
    assert [body for _, body in w.driver.sent] == parts(THREE)
    assert li_run(w.state.li_get_message(m))["status"] == "sent" and report["sent_messages"] == 1


def test_a_quiet_linkedin_conversation_gets_a_catch_up(tmp_path):
    from tests.linkedin_fakes import FakeBrain as LiBrain, Person as LiPerson
    from tests.test_linkedin_agent import World as LiWorld, run as li_run
    brain = LiBrain()
    w = LiWorld(tmp_path, [LiPerson(slug="jane-doe-1", name="Jane Doe", connected=True)], brain=brain)
    li_run(w.state.set_setting(LI_AUTOPILOT, "on"))
    lead = w.lead("jane-doe-1", stage="replied", name="Jane Doe", first_name="Jane",
                  profile_url="https://www.linkedin.com/in/jane-doe-1/")
    at = naive(w.at)
    for direction, kind, days in (("out", "message", 9), ("in", "message", 8), ("out", "reply", 8)):
        li_run(w.state.li_add_message(lead, direction=direction, kind=kind, status="sent",
                                      body=f"{kind} {days}", sent_at=at - timedelta(days=days)))
    li_run(w.agent().draft_messages())
    assert [x["kind"] for x in w.msgs(lead) if x["status"] == "approved"] == ["catch_up"]


# ── Proving a message went (live, 2026-11-18) ──

def test_a_message_naming_an_address_is_confirmed_despite_link_spacing(tmp_path):
    # WhatsApp draws the address as a link; reading it back spaces it apart.
    def shown(s):
        return s.replace("someone@shop.example:", "someone@shop.example :")
    w = World(tmp_path, [Person(phone=JANE, thread=[("in", "Who was it?")])], driver_kwargs={"typing": lambda s: s})
    c = w.contact(stage="replied")
    known_in(w, c, "Who was it?")
    body = "It was someone@shop.example: they asked for the list."
    m = w.approved(c, kind="reply", body=body)
    real_press = w.driver.press_enter

    async def press_enter():
        w.driver.composer = shown(w.driver.composer)
        await real_press()
    w.driver.press_enter = press_enter
    w.go()
    assert w.get(m)["status"] == "sent"


def test_the_same_words_as_an_earlier_message_are_confirmed_by_a_new_bubble(tmp_path):
    w = World(tmp_path, [Person(phone=JANE, thread=[("out", "Got it, thanks."), ("in", "Ok")])])
    c = w.contact(stage="replied")
    run(w.state.wa_add_message(c, kind="reply", body="Got it, thanks.", status="sent",
                               sent_at=naive(OPEN) - timedelta(days=1)))
    known_in(w, c, "Ok", index=1)
    m = w.approved(c, kind="reply", body="Got it, thanks.")
    w.go()
    assert w.get(m)["status"] == "sent"


def test_the_owner_s_own_update_failing_never_pauses_whatsapp(tmp_path):
    from harvey import meeting_alert as ma
    from harvey.whatsapp import guards
    w = World(tmp_path, [Person(phone="+15555550199")], alert_number="+15555550199",
              driver_kwargs={"show_after_send": False})
    for n in range(3):
        run(ma.queue_to_owner(w.state, w.config, f"Harvey update\nSent {n + 1} follow-ups."))
    w.go()
    assert run(guards.active_pause(w.state)) is None
    assert run(w.state.get_setting(guards.FAILED_KEY, "0")) in ("0", "")
