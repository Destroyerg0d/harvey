"""WhatsApp books the call directly, and autopilot sends what no longer needs
you (owner, 2026-11-20). Every person, number and link here is invented."""

import json
from datetime import timedelta

import aiosqlite

from harvey.agents.whatsapp_agent import AUTOPILOT_KEY
from harvey.whatsapp.store import HELD_FOR_REVIEW
from tests.whatsapp_fakes import FIRST, JANE, OPEN, FakeBrain, Person, World, run

MEET = "https://meet.example.com/abc-defg-hij"


def _meeting(w):
    offer = w.config.product.offer
    offer.meeting_window, offer.meeting_timezone, offer.meeting_link = "10:00-12:00", "Asia/Kolkata", MEET


def naive(dt):
    return dt.replace(tzinfo=None)


def test_an_interested_reply_offers_the_time_and_the_link(tmp_path):
    w = World(tmp_path, [Person(phone=JANE, unread=True, thread=[("in", "Sounds interesting, tell me more")])],
              brain=FakeBrain(intent="interested"))
    _meeting(w)
    w.contact(stage="messaged")
    w.go()
    prompt = dict(w.brain.calls)["draft_reply"]
    assert "THE MEETING TIME. They're interested" in prompt and MEET in prompt


def test_a_yes_to_the_offered_time_books_the_meeting(tmp_path):
    offer = f"Would Thursday 5 November, 4:30 to 6:30 AM London time suit you? The link: {MEET}"
    w = World(tmp_path, [Person(phone=JANE, unread=True, thread=[("out", offer), ("in", "Yes, that works")])],
              brain=FakeBrain(intent="interested", drafts={"draft_reply": "Great, see you then."}))
    _meeting(w)
    c = w.contact(stage="warm")
    run(w.state.wa_add_message(c, kind="reply", body=offer, status="sent",
                               sent_at=naive(OPEN) - timedelta(hours=1)))
    run(w.state.set_setting("meeting:offered:whatsapp:" + c, json.dumps(
        {"text": "Thursday 5 November, 4:30 to 6:30 AM London time",
         "start": (OPEN + timedelta(days=1)).isoformat()})))
    w.go()
    assert w.stage(c) == "meeting"
    assert "THE MEETING IS BOOKED" in dict(w.brain.calls)["draft_reply"]

    async def booked():
        async with aiosqlite.connect(w.state.db_path) as db:
            return (await (await db.execute(
                "SELECT count(*) FROM actions WHERE action_type = 'meeting_booked'")).fetchone())[0]
    assert run(booked()) == 1


def test_a_held_draft_with_a_link_that_passes_now_goes(tmp_path):
    w = World(tmp_path, [Person(phone=JANE)])
    run(w.state.set_setting(AUTOPILOT_KEY, "on"))
    c = w.contact()
    m = run(w.state.wa_add_message(c, kind="first", body=FIRST, status="draft",
                                   error=HELD_FOR_REVIEW + "links to example.org"))
    w.go()
    assert w.get(m)["status"] == "sent"
