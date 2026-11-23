"""Every meeting in every conversation (harvey/meeting_finder.py; owner,
2026-11-23). The brain is a fake that answers with what a careful reader would;
the code's own checks are what's tested. People, numbers and dates are invented."""

import asyncio
import json
from datetime import datetime, timedelta, timezone

import pytest

from harvey import meeting_finder, meetings
from harvey.models.company import Company
from harvey.models.conversation import Conversation, Message
from harvey.models.prospect import Prospect
from harvey.state import StateManager
from tests.linkedin_fakes import make_config

NOW = datetime(2026, 11, 20, 6, 0, tzinfo=timezone.utc)      # a Friday


def run(coro):
    return asyncio.run(coro)


def _config():
    config = make_config()
    offer = config.product.offer
    offer.meeting_window, offer.meeting_timezone, offer.meeting_link = "10:00-12:00", "Asia/Kolkata", "https://meet.example.com/x"
    return config


@pytest.fixture
def state(tmp_path):
    s = StateManager(str(tmp_path / "h.db"))
    run(s.init_db())
    return s


class Brain:
    """Answers each conversation from a table: its name -> the meetings."""

    def __init__(self, answers):
        self.answers, self.calls = answers, []
        self.paused_reason = ""

    async def think_json(self, prompt, **kw):
        self.calls.append(prompt)
        for name, meetings_ in self.answers.items():
            if name in prompt:
                return {"meetings": meetings_}
        return {"meetings": []}


def _linkedin(state, slug, name, thread):
    lead, _ = run(state.li_upsert_lead(slug, name=name, stage="replied"))
    for i, (who, body) in enumerate(thread):
        run(state.li_add_message(lead, direction="in" if who == "them" else "out", body=body, status="sent",
                                 sent_at=(NOW - timedelta(days=3) + timedelta(hours=i)).replace(tzinfo=None)))
    return lead


def test_an_agreed_time_is_found_at_the_right_moment(state):
    lead = _linkedin(state, "ann-lee-1", "Ann Lee", [("us", "Could we do a short call?"),
                                                    ("them", "Sure, Tuesday at 3pm EST works")])
    brain = Brain({"Ann Lee": [{"status": "agreed", "start": "2026-11-24T15:00-05:00", "minutes": 30,
                                "proposed_by": "them", "quote": "Tuesday at 3pm EST works"}]})
    report = run(meeting_finder.scan(state, brain, _config(), now=NOW))
    assert report["read"] == 1 and report["found"]["agreed"] == 1
    [m] = run(meeting_finder.stored(state))
    assert m["id"] == lead and m["start"] == "2026-11-24T20:00:00+00:00"
    assert "[2026-11-17" in brain.calls[0] and "Tuesday at 3pm EST" in brain.calls[0]   # dated messages


def test_offers_cancellations_and_made_up_evidence(state):
    _linkedin(state, "bo-ma-2", "Bo Ma", [("us", "Would Monday 10 AM IST suit?"), ("them", "Let me check my diary")])
    _linkedin(state, "cy-no-3", "Cy No", [("us", "Thursday 11 AM?"), ("them", "Yes, Thursday works"),
                                          ("them", "Sorry, can't make it any more")])
    _linkedin(state, "dee-4", "Dee Example", [("us", "Free for a call?"), ("them", "Maybe")])
    brain = Brain({
        "Bo Ma": [{"status": "proposed", "start": "2026-11-23T10:00+05:30", "proposed_by": "us",
                   "quote": "Would Monday 10 AM IST suit?"}],
        "Cy No": [{"status": "cancelled", "start": "2026-11-19T11:00+05:30", "quote": "can't make it any more"}],
        # Evidence that isn't in the conversation, and a time that doesn't parse.
        "Dee Example": [{"status": "agreed", "start": "2026-11-25T10:00+05:30", "quote": "See you Wednesday!"},
                        {"status": "proposed", "start": "next week sometime", "quote": "Free for a call?"}],
    })
    run(meeting_finder.scan(state, brain, _config(), now=NOW))
    found = {(m["status"], m["start"]) for m in run(meeting_finder.stored(state))}
    assert ("proposed", "2026-11-23T04:30:00+00:00") in found
    assert ("cancelled", "2026-11-19T05:30:00+00:00") in found
    assert ("proposed", "") in found                         # kept, without a guessed time
    assert not any(s == "agreed" for s, _ in found)         # the made-up quote is dropped


def test_a_conversation_is_read_again_only_when_it_changes(state):
    lead = _linkedin(state, "ann-lee-1", "Ann Lee", [("us", "A call?"), ("them", "Sure, Tuesday at 3pm")])
    brain = Brain({})
    run(meeting_finder.scan(state, brain, _config(), now=NOW))
    run(meeting_finder.scan(state, brain, _config(), now=NOW))
    assert len(brain.calls) == 1
    run(state.li_add_message(lead, direction="in", body="Actually, can we do 4pm?", status="sent",
                             sent_at=NOW.replace(tzinfo=None)))
    run(meeting_finder.scan(state, brain, _config(), now=NOW))
    assert len(brain.calls) == 2


def test_conversations_without_a_reply_or_a_meeting_word_arent_read(state):
    _linkedin(state, "eve-5", "Eve Example", [("us", "Nice store you have.")])
    _linkedin(state, "fay-6", "Fay Example", [("us", "Nice store."), ("them", "Thanks, we like it")])
    brain = Brain({})
    report = run(meeting_finder.scan(state, brain, _config(), now=NOW))
    assert report["with_signs"] == 0 and brain.calls == []


def test_whatsapp_and_email_conversations_are_read_too(state):
    contact, _ = run(state.wa_add_contact(phone="+447700900123", stage="replied", name="Sam Example", source="website chat"))
    run(state.wa_add_message(contact, direction="in", kind="reply", body="Can we talk tomorrow at 11?", status="sent",
                             sent_at=(NOW - timedelta(hours=2)).replace(tzinfo=None).isoformat()))
    company_id = run(state.add_company(Company(name="Example Shop", domain="example-shop.test")))
    prospect_id = run(state.add_prospect(Prospect(first_name="Ray", last_name="Example", email="ray@example-shop.test",
                                                  company_id=company_id, status="replied")))
    run(state.add_conversation(Conversation(id="conv-1", prospect_id=prospect_id, thread=[
        Message(sender="harvey", content="Would a call next week help?", timestamp=NOW - timedelta(days=2)),
        Message(sender="prospect", content="Yes, Wednesday 2pm works for me", timestamp=NOW - timedelta(days=1))])))
    brain = Brain({"Sam Example": [{"status": "proposed", "start": "2026-11-21T11:00+00:00", "quote": "talk tomorrow at 11"}],
                   "Ray Example": [{"status": "agreed", "start": "2026-11-25T14:00-06:00", "quote": "Wednesday 2pm works for me"}]})
    report = run(meeting_finder.scan(state, brain, _config(), now=NOW))
    assert report["by_channel"]["whatsapp"]["proposed"] == 1 and report["by_channel"]["email"]["agreed"] == 1


def test_the_calendar_places_what_was_found(state):
    config = _config()
    _linkedin(state, "ann-lee-1", "Ann Lee", [("us", "A call?"), ("them", "Sure, Tuesday at 3pm EST works")])
    _linkedin(state, "bo-ma-2", "Bo Ma", [("us", "Would Monday 10 AM IST suit?"), ("them", "Let me check")])
    _linkedin(state, "cy-no-3", "Cy No", [("us", "Thursday?"), ("them", "Sorry, can't make it")])
    _linkedin(state, "gus-7", "Gus Example", [("us", "Call on the 10th?"), ("them", "Yes that works, see you then")])
    brain = Brain({
        "Ann Lee": [{"status": "agreed", "start": "2026-11-24T15:00-05:00", "quote": "Tuesday at 3pm EST works"}],
        "Bo Ma": [{"status": "proposed", "start": "2026-11-23T10:00+05:30", "quote": "Would Monday 10 AM IST suit?"}],
        "Cy No": [{"status": "cancelled", "start": "2026-11-19T11:00+05:30", "quote": "can't make it"}],
        "Gus Example": [{"status": "agreed", "start": "2026-11-10T10:00+05:30", "quote": "see you then"}],
    })
    run(meeting_finder.scan(state, brain, config, now=NOW))
    data = run(meetings.collect(state, config, now=NOW))
    assert [m["name"] for m in data["upcoming"]] == ["Ann Lee"]
    assert data["upcoming"][0]["when"]["ours"].startswith("Wednesday 25 November, 1:30")   # 3pm EST in IST
    assert [m["name"] for m in data["close"]] == ["Bo Ma"] and data["close"][0]["status"] == "proposed"
    assert {m["name"]: m["status"] for m in data["earlier"]} == {"Cy No": "cancelled", "Gus Example": "agreed"}
    assert data["stats"]["upcoming"] == 1 and data["stats"]["held"] == 1 and data["stats"]["cancelled"] == 1
    assert data["upcoming"][0]["found"]["quote"] == "Tuesday at 3pm EST works"
    assert json.dumps(data)


def test_an_offer_that_passed_unanswered_isnt_counted_as_held(state):
    # Live, 2026-11-23: "Held: 1 · 20 LinkedIn" counted lapsed offers in the split.
    config = _config()
    _linkedin(state, "di-ex-4", "Di Example", [("us", "Would Monday 10 AM IST suit?"), ("them", "Busy week")])
    _linkedin(state, "gus-7", "Gus Example", [("us", "Call on the 10th?"), ("them", "Yes that works, see you then")])
    brain = Brain({
        "Di Example": [{"status": "proposed", "start": "2026-11-02T10:00+05:30", "quote": "Would Monday 10 AM IST suit?"}],
        "Gus Example": [{"status": "agreed", "start": "2026-11-03T10:00+05:30", "quote": "see you then"}],
    })
    run(meeting_finder.scan(state, brain, config, now=NOW))
    st = run(meetings.collect(state, config, now=NOW))["stats"]
    assert st["held"] == 1 and st["lapsed"] == 1 and st["proposed"] == 0
    assert st["by_channel"]["linkedin"]["held"] == 1 and st["by_channel"]["linkedin"]["lapsed"] == 1
