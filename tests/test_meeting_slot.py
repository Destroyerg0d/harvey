"""When someone asks to meet, Harvey offers the owner's fixed window on the
next working day, in their time when it knows their zone (owner, 2026-11-14:
"instead of telling them to pick two or three time slots"). Fictional data."""

import json
from datetime import datetime, timezone
from types import SimpleNamespace as NS

import pytest

from harvey import meeting_alert as ma
from harvey.meeting_slot import asks_to_meet, next_working_day, offer, prompt_block

SUNDAY_NIGHT = datetime(2026, 11, 14, 18, 30, tzinfo=timezone.utc)    # Sunday 00:00 in India
FRIDAY_NOON = datetime(2026, 11, 20, 6, 30, tzinfo=timezone.utc)      # Friday 12:00 in India


def cfg(window="10:00-12:00", zone="Asia/Kolkata"):
    return NS(product=NS(offer=NS(meeting_window=window, meeting_timezone=zone,
                                  meeting_duration="15-minute", meeting_owner="Robin",
                                  booking_url="https://book.studio.example/")))


def test_the_next_working_day_skips_the_weekend():
    assert next_working_day(SUNDAY_NIGHT.date()).isoformat() == "2026-11-16"     # Saturday -> Monday
    assert next_working_day(FRIDAY_NOON.date()).isoformat() == "2026-11-23"      # Friday -> Monday


@pytest.mark.parametrize("zone, text", [
    ("Europe/London", "Monday 16 November, 4:30 to 6:30 AM London time (10 AM to 12 PM IST)"),
    ("Asia/Kolkata", "Monday 16 November, 10 AM to 12 PM IST"),
    ("", "Monday 16 November, 10 AM to 12 PM IST"),
    ("Australia/Sydney", "Monday 16 November, 3:30 to 5:30 PM Sydney time (10 AM to 12 PM IST)"),
    ("America/New_York", "Sunday 15 November, 11:30 PM to Monday 16 November, 1:30 AM New York time "
                         "(Monday 16 November, 10 AM to 12 PM IST)"),
])
def test_the_slot_reads_in_their_time_with_ours_beside_it(zone, text):
    assert offer(cfg(), SUNDAY_NIGHT, zone).text() == text


def test_no_window_set_means_no_slot():
    assert offer(cfg(window=""), SUNDAY_NIGHT) is None
    assert offer(cfg(window="12:00-10:00"), SUNDAY_NIGHT) is None
    assert offer(cfg(zone="Not/AZone"), SUNDAY_NIGHT) is None
    assert offer(cfg(), SUNDAY_NIGHT, "Not/AZone").their_zone == ""


def test_what_counts_as_asking_to_meet():
    for text in ("Tell me when we can meet?", "Can we do a call?", "Happy to talk next week",
                 "What time works for you?"):
        assert asks_to_meet(text), text
    for text in ("Sure I'd like to get your opinion", "How much is it?"):
        assert not asks_to_meet(text), text


def test_the_prompt_offers_the_slot_and_never_asks_for_windows():
    block = prompt_block(offer(cfg(), SUNDAY_NIGHT, "Europe/London"), cfg())
    assert "4:30 to 6:30 AM London time" in block and "Robin" in block
    assert "Do not ask them to suggest times" in block and "do not send a booking link" in block


# ── Email ──

@pytest.mark.asyncio
async def test_an_email_asking_to_meet_gets_the_slot_and_a_draft_without_it_is_redone(monkeypatch):
    from harvey.agents.handler import Handler
    from harvey.models.conversation import Conversation, Message
    from harvey.models.prospect import Prospect
    import harvey.meeting_slot as ms

    slot = offer(cfg(), SUNDAY_NIGHT, "Europe/London")
    prompts = []

    class Brain:
        paused_reason = ""

        def load_prompt(self, *a, **k):
            return ""

        async def think(self, prompt, session_id=None, agent="", task=""):
            prompts.append(prompt)
            return ("Happy to. Could you send two or three windows?" if len(prompts) == 1 else
                    f"Happy to. How about {slot.text()}? I'll send the invite once you confirm.")

    h = Handler.__new__(Handler)
    h.brain, h.state, h.skills = Brain(), None, ""
    h.config = NS(persona=NS(name="Mary", role="Sales", company="Studio Example", tone="plain"),
                  product=NS(name="P", description="d", objection_responses={},
                             offer=cfg().product.offer),
                  channels=NS(email=NS(mailboxes=[])), writer=NS(humanize=False))

    async def mine(prospect):
        return slot
    monkeypatch.setattr(h, "_meeting_slot", mine)
    monkeypatch.setattr("harvey.agents.handler.startup_facts", _none)
    monkeypatch.setattr("harvey.agents.handler.lessons_block", _empty)
    monkeypatch.setattr("harvey.agents.handler.humanize", _same)
    monkeypatch.setattr("harvey.agents.handler.is_multi", lambda c: False)
    prospect = Prospect(first_name="Ann", last_name="Lee", email="ann@shop.example")
    convo = Conversation(id="c1", prospect_id="p1", thread=[Message(sender="prospect",
                                                                     content="Tell me when we can meet?")])
    reply = await h._generate_response("interested", "Tell me when we can meet?", prospect, convo)
    assert "4:30 to 6:30 AM London time" in reply and len(prompts) == 2
    assert "THE MEETING TIME" in prompts[0] and "left out what it must say" in prompts[1]
    assert ms.prompt_block                     # the module the handler uses


async def _none(*a, **k):
    return None


async def _empty(*a, **k):
    return ""


async def _same(brain, items, **k):
    return items


# ── The alert says what was offered, and again when a time is agreed ──

class ReadBrain:
    def __init__(self, when):
        self.when, self.calls = when, 0
        self.paused_reason, self.failed_calls = "", 0

    async def think(self, prompt, session_id=None, agent="", task=""):
        self.calls += 1
        return json.dumps({"close_to_meeting": True, "when": self.when, "service": "Shopify fixes",
                           "summary": "Setting up a call.", "next_step": "Send the invite."})


@pytest.mark.asyncio
async def test_the_alert_names_the_slot_and_a_later_agreed_time_alerts_again():
    import os
    import tempfile

    from harvey.state import StateManager
    with tempfile.TemporaryDirectory() as tmp:
        state = StateManager(os.path.join(tmp, "h.db"))
        await state.init_db()
        config = NS(channels=NS(whatsapp=NS(alert_number="+15555550199", number="+15555550100")),
                    persona=NS(company="Studio Example"))

        async def alert(brain, latest):
            return await ma.maybe_alert(state, brain, config, channel="Email", key="email:c1",
                                        name="Ann", latest=latest, thread=latest, where="Conversations",
                                        offered="Monday 16 November, 4:30 to 6:30 AM London time")

        assert await alert(ReadBrain(""), "Tell me when we can meet?")
        assert not await alert(ReadBrain(""), "Which call app do you use?")    # nothing new
        assert await alert(ReadBrain("Monday 16 Nov, 5 AM London"), "5 AM Monday works")
        assert not await alert(ReadBrain("Monday 16 Nov, 5 AM London"), "See you then")
        contact = await state.wa_get_contact_by_phone("+15555550199")
        bodies = [m["body"] for m in await state.wa_messages(contact_id=contact["id"])]
        assert len(bodies) == 2
        assert "Harvey offered: Monday 16 November, 4:30 to 6:30 AM London time" in bodies[0]
        assert bodies[1].startswith("Harvey: time agreed with Ann") and "5 AM London" in bodies[1]


# ── The meeting room link, and chat instead of a call ──

ROOM = "https://meet.example.test/abc-defg-hij"
CHAT = "https://wa.me/15555550100"


def linked_cfg():
    c = cfg()
    c.product.offer.meeting_link, c.product.offer.chat_link = ROOM, CHAT
    return c


def test_the_slot_carries_the_meeting_room_as_the_invite():
    block = prompt_block(offer(linked_cfg(), SUNDAY_NIGHT, "Europe/London"), linked_cfg())
    assert ROOM in block and "no separate calendar invite" in block
    assert "calendar invite follows" in prompt_block(offer(cfg(), SUNDAY_NIGHT), cfg())


def test_someone_who_cant_do_a_call_is_offered_chat_instead():
    from harvey.meeting_slot import chat_instead_block, declines_call
    for text in ("I can’t pick a date for a call right now, I’m often on the road.",
                 "No calls please, I travel a lot", "Prefer email if that's ok"):
        assert declines_call(text), text
    for text in ("Can we do a call Thursday?", "Tell me when we can meet?"):
        assert not declines_call(text), text
    assert CHAT in chat_instead_block(linked_cfg()) and "playful" in chat_instead_block(linked_cfg())
    assert chat_instead_block(cfg()) == ""                 # no chat link set: no offer


def test_the_gate_lets_your_room_and_chat_links_through_and_nothing_else_there():
    from harvey.gate import autopilot_review
    c = linked_cfg()
    c.persona = NS(email="sam@studio.example")
    c.channels = NS(email=NS(link_domains=[], mailboxes=[]))
    assert autopilot_review("", f"Here's the room: {ROOM}", c) == []
    assert autopilot_review("", f"Message us: {CHAT}", c) == []
    assert autopilot_review("", "Join https://meet.example.test/someone-else", c)
    assert autopilot_review("", "Message https://wa.me/15555550999", c)
