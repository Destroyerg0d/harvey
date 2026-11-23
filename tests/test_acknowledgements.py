"""A short "okay, thanks" is never a no (live, 2026-11-23: one after a confirmed
meeting was read as not interested and the lead was declined). Every person,
number and link here is invented."""

import json
from datetime import timedelta

from harvey.agents.linkedin_agent import ACK, acknowledged, is_acknowledgement
from tests.linkedin_fakes import IN_HOURS, OWNER, FakeBrain
from tests.test_linkedin_agent import World, jane, naive, run
from tests import whatsapp_fakes as wa

MEET = "https://meet.example.com/abc-defg-hij"
CONFIRMED = f"Great, thank you. You're confirmed for Thursday 5 November, 10 AM to 12 PM IST. The link: {MEET}"


def test_what_counts_as_a_thanks():
    for text in ("Okay, thanks", "ok thanks!", "Noted, thank you", "Sounds good", "Thank you so much",
                 "Perfect, see you then", "Great, looking forward to it", "👍", "Cheers"):
        assert is_acknowledgement(text), text
    for text in ("No thanks", "Not interested, thanks", "Okay but not now", "Thanks, can we do Friday?",
                 "Ok thanks, please remove me", "Thanks, we already have an agency", "",
                 "Okay thanks, we'll pass for now", "Okay?"):
        assert not is_acknowledgement(text), text
    assert acknowledged("Okay, thanks", "not_interested", "warm") == ACK
    assert acknowledged("Okay, thanks", "interested", "meeting") == ACK
    assert acknowledged("Okay, thanks", "interested", "warm") == "interested"
    assert acknowledged("No thanks", "not_interested", "meeting") == "not_interested"


PITCH = "Your checkout asks for an account first. Happy to share the three fixes I'd make."


def _linkedin(tmp_path, last, stage, intent="not_interested", ours=CONFIRMED):
    w = World(tmp_path, [jane(thread=[(OWNER, ours), ("Jane Doe", last)])],
              brain=FakeBrain(intent=intent, text="Anything else I can help with?"))
    offer = w.config.product.offer
    offer.meeting_window, offer.meeting_timezone, offer.meeting_link = "10:00-12:00", "Asia/Kolkata", MEET
    lead = w.lead("jane-doe-1", stage=stage, name="Jane Doe", company="Example Goods")
    run(w.state.li_add_message(lead, kind="reply", body=ours, status="sent",
                               sent_at=naive(IN_HOURS) - timedelta(hours=2)))
    return w, lead


def test_a_thanks_after_the_meeting_is_booked_keeps_the_meeting(tmp_path):
    w, lead = _linkedin(tmp_path, "Okay, thanks", stage="meeting")
    w.go()
    assert run(w.state.li_get_lead(lead))["stage"] == "meeting"
    theirs = [m for m in w.msgs(lead) if m["direction"] == "in"]
    assert theirs and theirs[-1]["intent"] == ACK
    assert not [t for t, _ in w.brain.calls if t == "draft_reply"]       # nothing to answer


def test_a_thanks_read_as_a_no_doesnt_decline_them(tmp_path):
    w, lead = _linkedin(tmp_path, "Ok thanks", stage="warm", ours=PITCH)
    w.go()
    assert run(w.state.li_get_lead(lead))["stage"] == "warm"


def test_a_real_no_still_declines(tmp_path):
    w, lead = _linkedin(tmp_path, "No thanks, not for us", stage="warm", ours=PITCH)
    w.go()
    assert run(w.state.li_get_lead(lead))["stage"] == "declined"


def test_an_okay_to_the_offered_time_books_it(tmp_path):
    offer = f"Would Thursday 5 November, 4:30 to 6:30 AM London time suit you? The link: {MEET}"
    w = World(tmp_path, [jane(thread=[(OWNER, offer), ("Jane Doe", "Okay, thanks")])],
              brain=FakeBrain(intent="not_interested", text="Great, see you then."))
    o = w.config.product.offer
    o.meeting_window, o.meeting_timezone, o.meeting_link = "10:00-12:00", "Asia/Kolkata", MEET
    lead = w.lead("jane-doe-1", stage="warm", name="Jane Doe", company="Example Goods")
    run(w.state.li_add_message(lead, kind="reply", body=offer, status="sent",
                               sent_at=naive(IN_HOURS) - timedelta(hours=2)))
    run(w.state.set_setting("meeting:offered:linkedin:" + lead, json.dumps(
        {"text": "Thursday 5 November, 4:30 to 6:30 AM London time",
         "start": (IN_HOURS + timedelta(days=1)).isoformat()})))
    w.go()
    assert run(w.state.li_get_lead(lead))["stage"] == "meeting"


def test_whatsapp_keeps_the_meeting_too(tmp_path):
    w = wa.World(tmp_path, [wa.Person(phone=wa.JANE, unread=True,
                                      thread=[("out", CONFIRMED), ("in", "Okay, thanks")])],
                 brain=wa.FakeBrain(intent="not_interested"))
    c = w.contact(stage="meeting")
    run(w.state.wa_add_message(c, kind="reply", body=CONFIRMED, status="sent",
                               sent_at=naive(wa.OPEN) - timedelta(hours=1)))
    w.go()
    assert w.stage(c) == "meeting"
    assert "draft_reply" not in dict(w.brain.calls)
