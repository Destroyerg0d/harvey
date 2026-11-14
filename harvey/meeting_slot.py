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


def prompt_block(slot: Slot, config) -> str:
    """What every channel's reply prompt gets when they ask to meet."""
    offered = getattr(getattr(config, "product", None), "offer", None)
    duration = getattr(offered, "meeting_duration", "") or "short"
    who = getattr(offered, "meeting_owner", "") or "our team"
    return (
        "\n\nTHE MEETING TIME. They asked to meet. Offer exactly this time, written as it is here, "
        f"for a call ({duration}) with {who}: {slot.text()}. Ask them to reply to confirm it suits, and "
        "say the calendar invite follows once they do. Do not ask them to suggest times or windows, "
        "and do not send a booking link. If it doesn't suit them, they can say so and we'll find "
        "another time. This overrides any other instruction about timing or booking.")
