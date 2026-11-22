"""The meeting time Harvey offers when someone asks to meet.

Owner, 2026-11-14: "When a client asks like this [Tell me when we can meet?],
schedule the meeting for me the next working day, 10 AM to 12 PM IST, convert
it into the client's time zone if you know it and tell them in their time zone
... instead of telling them to pick two or three time slots." Asked about a
client for whom that window is the night (London 4:30-6:30 AM), the owner chose
the same window for everyone.

``product.offer.meeting_window`` ("10:00-12:00") in ``meeting_timezone``
("Asia/Kolkata") on the owner's next working day (Monday to Friday there),
written in the client's own time when Harvey knows their zone, with the
owner's time beside it. Every channel's reply prompt gets ``prompt_block`` when
the person asks to meet (``asks_to_meet``).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
import pytz

ASKS_TO_MEET_RE = re.compile(
    r"\b(?:meet|meeting|call|zoom|teams|google meet|g ?meet|talk|speak|schedule|book|"
    r"available|availability|when can|what time|which time|time works|free (?:on|at|for|this|next))\b",
    re.IGNORECASE)
_WINDOW_RE = re.compile(r"^\s*(\d{1,2}):(\d{2})\s*-\s*(\d{1,2}):(\d{2})\s*$")
# What a zone is called in a message.
ZONE_NAMES = {"Asia/Kolkata": "IST", "Asia/Calcutta": "IST", "UTC": "UTC"}


def asks_to_meet(text: str) -> bool:
    return bool(ASKS_TO_MEET_RE.search(text or ""))


def zone_name(zone: str) -> str:
    """"London time", "New York time", "IST"."""
    if zone in ZONE_NAMES:
        return ZONE_NAMES[zone]
    return zone.rsplit("/", 1)[-1].replace("_", " ") + " time"


def _clock(moment: datetime) -> str:
    hour = moment.hour % 12 or 12
    return f"{hour}" + (f":{moment.minute:02d}" if moment.minute else "")


def _ampm(moment: datetime) -> str:
    return "AM" if moment.hour < 12 else "PM"


def _day(moment: datetime) -> str:
    return f"{moment:%A} {moment.day} {moment:%B}"


def span(start: datetime, end: datetime, label: str, with_day: bool = True) -> str:
    """"Monday 16 November, 4:30 to 6:30 AM London time"."""
    day = f"{_day(start)}, " if with_day else ""
    if start.date() != end.date():
        return (f"{day}{_clock(start)} {_ampm(start)} to {_day(end)}, {_clock(end)} {_ampm(end)} "
                f"{label}").strip()
    if _ampm(start) == _ampm(end):
        return f"{day}{_clock(start)} to {_clock(end)} {_ampm(end)} {label}"
    return f"{day}{_clock(start)} {_ampm(start)} to {_clock(end)} {_ampm(end)} {label}"


@dataclass
class Slot:
    start: datetime          # aware, in the owner's zone
    end: datetime
    owner_zone: str
    their_zone: str = ""

    @property
    def theirs(self) -> tuple[datetime, datetime] | None:
        if not self.their_zone or self.their_zone == self.owner_zone:
            return None
        zone = pytz.timezone(self.their_zone)
        return self.start.astimezone(zone), self.end.astimezone(zone)

    def text(self) -> str:
        """In their time with the owner's beside it, or the owner's alone."""
        ours = span(self.start, self.end, zone_name(self.owner_zone))
        theirs = self.theirs
        if theirs is None:
            return ours
        same_day = theirs[0].date() == self.start.date()
        return (f"{span(theirs[0], theirs[1], zone_name(self.their_zone))} "
                f"({span(self.start, self.end, zone_name(self.owner_zone), with_day=not same_day)})")

    def their_start(self) -> str:
        """The start as they will read it ("4:30"), to check a draft states it."""
        theirs = self.theirs
        return _clock(theirs[0] if theirs else self.start)


def _window(config) -> tuple[time, time, str] | None:
    offer = getattr(getattr(config, "product", None), "offer", None)
    raw = (getattr(offer, "meeting_window", "") or "").strip()
    zone = (getattr(offer, "meeting_timezone", "") or "").strip()
    match = _WINDOW_RE.match(raw)
    if not match or not zone:
        return None
    try:
        pytz.timezone(zone)
        start = time(int(match.group(1)), int(match.group(2)))
        end = time(int(match.group(3)), int(match.group(4)))
    except (ValueError, KeyError, Exception):
        return None
    return (start, end, zone) if end > start else None


def next_working_day(today: date) -> date:
    """The next Monday-to-Friday after ``today``."""
    day = today + timedelta(days=1)
    while day.weekday() >= 5:
        day += timedelta(days=1)
    return day


def offer(config, now: datetime | None = None, their_zone: str = "") -> Slot | None:
    """The slot to offer, or None when no meeting window is configured."""
    window = _window(config)
    if window is None:
        return None
    start_t, end_t, owner_zone = window
    zone = pytz.timezone(owner_zone)
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    day = next_working_day(now.astimezone(zone).date())
    try:
        if their_zone:
            pytz.timezone(their_zone)
    except Exception:
        their_zone = ""
    return Slot(start=zone.localize(datetime.combine(day, start_t)),
                end=zone.localize(datetime.combine(day, end_t)), owner_zone=owner_zone,
                their_zone=their_zone)


def _offer_field(config, name: str) -> str:
    offer = getattr(getattr(config, "product", None), "offer", None)
    return (getattr(offer, name, "") or "").strip()


# Why the time is offered: they asked, or they showed interest and Harvey asks
# for the call directly (owner, 2026-11-20: "optimized to book meeting ... on
# this link and allot time asking directly").
_LEAD_INS = {
    "asked": "They asked to meet. Offer exactly this time",
    "interested": ("They're interested, so move this to a short call now and ask for it directly, "
                   "in a natural way after answering what they said. Offer exactly this time"),
    "catch_up": ("They were interested before. Pick it up by proposing a short call, directly. "
                 "Offer exactly this time"),
}


def prompt_block(slot: Slot, config, why: str = "asked") -> str:
    """What a channel's prompt gets to offer the owner's slot: when they ask to
    meet, when they're interested (``why`` "interested"), or in a catch-up to
    someone who was ("catch_up")."""
    duration = _offer_field(config, "meeting_duration") or "short"
    who = _offer_field(config, "meeting_owner") or "our team"
    link = _offer_field(config, "meeting_link")
    invite = (f"Give them this video call link for that time, written exactly: {link}. It is the "
              "invite: no separate calendar invite is needed. "
              if link else "Say the calendar invite follows once they confirm. ")
    return (
        f"\n\nTHE MEETING TIME. {_LEAD_INS.get(why, _LEAD_INS['asked'])}, written as it is here, "
        f"for a call ({duration}) with {who}: {slot.text()}. {invite}Ask them to reply to confirm "
        "it suits. Do not ask them to suggest times or windows, and do not send a booking link. "
        "If it doesn't suit them, they can say so and we'll find another time. This overrides "
        "any other instruction about timing or booking.")


def meeting_link(config) -> str:
    return _offer_field(config, "meeting_link")


# Someone interested in the work itself, not just polite: the price, the next
# step, how to start. With intent "interested" this is when the call is offered.
NEXT_STEP_RE = re.compile(
    r"\b(?:price|pricing|prices|cost|costs|quote|how much|rates?|budget|next steps?|get started|"
    r"how (?:do|would|can) (?:we|you|i) (?:start|begin|proceed|work)|work(?:ing)? together|"
    r"tell me more|more (?:details|info|information)|let'?s do (?:it|this)|proposal|"
    r"interested|sounds (?:good|great|interesting)|keen)\b", re.IGNORECASE)


def wants_next_step(text: str) -> bool:
    return bool(NEXT_STEP_RE.search(text or ""))


# A yes to a time we offered: short, and no "but", "instead" or another day.
CONFIRMS_RE = re.compile(
    r"\b(?:yes|yeah|yep|yup|sure|ok(?:ay)?|perfect|great|works|suits|sounds good|see you|"
    r"confirm(?:ed)?|booked|fine|good for me|looking forward|talk (?:then|soon)|done)\b", re.IGNORECASE)
NOT_THAT_TIME_RE = re.compile(
    r"\b(?:not|no|can'?t|cannot|won'?t|instead|another|different|other|later|earlier|reschedule|"
    r"busy|unavailable|but|next week|\d{1,2}(?::\d{2})?\s*(?:am|pm))\b", re.IGNORECASE)
_FRIENDLY_NO_RE = re.compile(r"\b(?:no problem|not a problem|no worries|no rush)\b", re.IGNORECASE)
_WEEKDAY_RE = re.compile(r"\b(monday|tuesday|wednesday|thursday|friday|saturday|sunday)\b", re.IGNORECASE)


def confirms_slot(text: str, offered_text: str = "") -> bool:
    """Their message says yes to the time we offered, and nothing else about
    it (another day, a different hour, a "but"). A weekday they name must be
    the one offered."""
    t = " ".join((text or "").replace("’", "'").split())
    if not t or len(t) > 220:
        return False
    t = _FRIENDLY_NO_RE.sub("", t)
    days = {d.lower() for d in _WEEKDAY_RE.findall(t)}
    if days and not days <= {d.lower() for d in _WEEKDAY_RE.findall(offered_text or "")}:
        return False
    return bool(CONFIRMS_RE.search(t)) and not NOT_THAT_TIME_RE.search(t) and not declines_call(t)


OFFERED_KEY = "meeting:offered:"     # + channel:id -> {"text", "start"}


async def remember_offer(state, key: str, slot: Slot) -> None:
    import json
    await state.set_setting(OFFERED_KEY + key, json.dumps(
        {"text": slot.text(), "start": slot.start.astimezone(timezone.utc).isoformat(),
         "end": slot.end.astimezone(timezone.utc).isoformat(), "their_zone": slot.their_zone or ""}))


async def offered(state, key: str) -> dict:
    """The slot last offered to them ({"text", "start"}), or {}."""
    import json
    try:
        data = json.loads(await state.get_setting(OFFERED_KEY + key) or "{}")
    except ValueError:
        data = {}
    return data if isinstance(data, dict) else {}


def still_ahead(offer_: dict, now: datetime | None = None) -> bool:
    try:
        start = datetime.fromisoformat(str(offer_.get("start") or ""))
    except ValueError:
        return False
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    if start.tzinfo is None:
        start = start.replace(tzinfo=timezone.utc)
    return start > now


def confirm_block(time_text: str, config) -> str:
    """What a reply prompt gets once they've said yes to the offered time."""
    link = meeting_link(config)
    return (
        "\n\nTHE MEETING IS BOOKED. They said yes to the call at " + (time_text or "the time offered")
        + ". Thank them in a few words, confirm that time exactly as written here"
        + (f" and give the link again, written exactly: {link}" if link else "")
        + ". Nothing else: no pitch, no new question. This overrides any other instruction.")


def offered_link_in(texts, config) -> bool:
    """One of these messages (ours, sent) carried the meeting link."""
    link = meeting_link(config)
    if not link:
        return False
    bare = link.split("://", 1)[-1].rstrip("/").lower()
    return any(bare in (t or "").lower() for t in texts)


# They can't, or would rather not, do a call (owner, 2026-11-15, on a client
# "often on the road": "follow up in a playful friendly manner, we can chat on
# WhatsApp ... create a link to text me", and "do this if a client disagrees
# to the call for similar reasons").
DECLINES_CALL_RE = re.compile(
    r"\b(?:can'?t|cannot|can not|unable to|not able to|won'?t be able to|couldn'?t|"
    r"don'?t have (?:the )?time|no time|not (?:keen|interested|sure about))\b[^.?!\n]{0,40}?"
    r"\b(?:call|calls|meeting|meet|talk|zoom|date|time|phone)\b"
    r"|\bon the road\b|\btravel(?:l?ing)?\s+(?:a lot|constantly|all the time|so much)\b"
    r"|\bprefer(?:red)? (?:to )?(?:email|e-mail|text|texting|chat|whatsapp|writing)\b"
    r"|\bno calls?\b|\bnot (?:a )?(?:call|phone) person\b"
    r"|\brather not (?:do a |have a |jump on a )?(?:call|meet)",
    re.IGNORECASE)


def declines_call(text: str) -> bool:
    """They say they can't or would rather not do a call."""
    return bool(DECLINES_CALL_RE.search((text or "").replace("’", "'")))


def chat_link(config) -> str:
    return _offer_field(config, "chat_link")


def chat_instead_block(config) -> str:
    """What a reply prompt gets when they can't do a call: chat instead."""
    link = chat_link(config)
    if not link:
        return ""
    return (
        "\n\nTHEY CAN'T DO A CALL. Don't push the call and don't offer a time. In a warm, light, "
        "friendly way (a little playfulness is welcome, never pushy), say no call is needed and "
        "offer to keep it going on WhatsApp instead, with this link to message us there, written "
        f"exactly: {link}. Email stays open too. Then answer anything they asked. This overrides "
        "any other instruction about calls, timing or booking.")

