"""WhatsApp's caps, the ramp for a new number, human pacing, and recipients' hours.

A new number is the riskiest pattern there is, so the defaults are low: 5 first
messages a day for the first 14 days after linking, then 10; 30 messages a day
in total; 8 an hour; 45-180 seconds between actions with longer breaks.

Counts live in settings, one key per kind per day (in ``channels.whatsapp.hours``'
time zone) and per hour, so a restart never resets a cap.

A message goes out only in 10:00-19:00 of the recipient's own time zone when
known, through harvey.send_window. A +1 number with no state or zone has no one
zone, and 10:00 in New York is 07:00 in Los Angeles, so it needs the window open
on both coasts at once. Anyone else Harvey can't place uses ``channels.whatsapp.hours``.
"""

from datetime import datetime, timedelta, timezone

from harvey.linkedin.pacing import Pacing
from harvey import send_window

COUNT_PREFIX = "whatsapp:count:"
LINKED_AT_KEY = "whatsapp:linked_at"
LINKED_NUMBER_KEY = "whatsapp:linked_number"
KINDS = ("first", "follow_up", "reply")
NANP_SPAN = ("America/New_York", "America/Los_Angeles")


def _parse(value) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.astimezone(timezone.utc).replace(tzinfo=None) if parsed.tzinfo else parsed


async def mark_linked(state, cfg, at: datetime | None = None) -> str:
    """Record when the phone was linked. The ramp counts from here, so linking
    the same number again doesn't start it over; a different number does."""
    at = at or datetime.now(timezone.utc).replace(tzinfo=None)
    linked = await state.get_setting(LINKED_AT_KEY)
    number = (getattr(cfg, "number", "") or "").strip()
    if not linked or (number and number != await state.get_setting(LINKED_NUMBER_KEY)):
        linked = at.isoformat(timespec="seconds")
        await state.set_setting(LINKED_AT_KEY, linked)
    if number:
        await state.set_setting(LINKED_NUMBER_KEY, number)
    return linked


async def recipient_zones(state, contact: dict, config=None) -> list[str]:
    """The zones the window must be open in for this contact. Empty: nothing is
    known about them, so channels.whatsapp.hours applies."""
    country = contact.get("country") or ""
    prospect_id = contact.get("prospect_id") or ""
    if prospect_id:
        prospect = await state.get_prospect(prospect_id)
        if prospect is not None:
            zone = await send_window.recipient_zone(state, prospect, config, country)
            if zone:
                return [zone]
    zone = send_window.zone_for(country=country) if country else ""
    if zone:
        return [zone]
    if (contact.get("phone") or "").startswith("+1"):
        return list(NANP_SPAN)
    return []


class WhatsAppPacing(Pacing):
    """``cfg`` is channels.whatsapp. Hours, delays and sleeping are LinkedIn's."""

    # ── Caps ──

    def _day_key(self, name: str) -> str:
        return f"{COUNT_PREFIX}{name}:{self.local_now().date().isoformat()}"

    def _hour_key(self) -> str:
        return f"{COUNT_PREFIX}hour:{self.local_now().strftime('%Y-%m-%dT%H')}"

    async def used(self) -> dict:
        return {"first_today": await self._count(self._day_key("first")),
                "messages_today": await self._count(self._day_key("message")),
                "messages_hour": await self._count(self._hour_key())}

    async def linked_at(self) -> datetime | None:
        return _parse(await self.state.get_setting(LINKED_AT_KEY))

    async def is_new_number(self) -> bool:
        """Still in the ramp. Never linked counts as new: the lower cap is the
        safe answer to not knowing."""
        linked = await self.linked_at()
        if linked is None:
            return True
        now = self._now().astimezone(timezone.utc).replace(tzinfo=None)
        return now - linked < timedelta(days=self.cfg.new_number_days)

    async def caps(self) -> dict:
        c = self.cfg
        first = c.max_daily_first_messages_new if await self.is_new_number() else c.max_daily_first_messages
        return {"first_today": first, "messages_today": c.max_daily_messages,
                "messages_hour": c.max_messages_per_hour}

    async def blocked(self, kind: str) -> str:
        """Why one more message of this kind can't go now, or ""."""
        used, caps = await self.used(), await self.caps()
        if used["messages_hour"] >= caps["messages_hour"]:
            return f"{caps['messages_hour']} messages this hour already"
        if used["messages_today"] >= caps["messages_today"]:
            return f"today's {caps['messages_today']} messages are used"
        if kind == "first" and used["first_today"] >= caps["first_today"]:
            return f"today's {caps['first_today']} first messages are used"
        return ""

    async def record(self, kind: str) -> None:
        if kind not in KINDS:
            raise ValueError(kind)
        keys = [self._day_key("message"), self._hour_key()]
        if kind == "first":
            keys.append(self._day_key("first"))
        for key in keys:
            await self.state.increment_setting(key)

    async def prune(self, keep_days: int = 40) -> None:
        """Forget counters older than ``keep_days``."""
        cutoff = (datetime.now(timezone.utc) - timedelta(days=keep_days)).strftime("%Y-%m-%d %H:%M:%S")
        try:
            async with self.state._connect() as db:
                await db.execute("DELETE FROM settings WHERE key LIKE ? AND updated_at < ?",
                                 (COUNT_PREFIX + "%", cutoff))
                await db.commit()
        except Exception:
            pass

    # ── Recipients' hours ──

    def recipient_open(self, zones: list[str]) -> bool:
        """The window is open now in every one of ``zones``. None known: the
        channel's own hours and zone."""
        if not zones:
            return self.in_hours()
        at = self._now().astimezone(timezone.utc).replace(tzinfo=None)
        return all(send_window.in_window(at, zone, self.cfg.hours) for zone in zones)
