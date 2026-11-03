"""LinkedIn hours, human pacing and the daily and weekly caps.

Counts live in settings, one key per kind per day (the LinkedIn hours'
time zone), per ISO week for invites, and per hour for actions, so a
restart never resets a cap. An action is a send: an invite, a message or
a withdrawn invite. Reading a page is not an action, but is paced too.
"""

import asyncio
import random
from datetime import datetime, time, timedelta, timezone

import pytz

COUNT_PREFIX = "linkedin:count:"
KINDS = ("invite", "message", "withdraw")


class Pacing:
    def __init__(self, state, cfg, *, now=None, rng=None, sleep=None):
        """``cfg`` is channels.linkedin. ``now`` returns an aware datetime
        (tests pass a fixed one); ``sleep`` is awaited for every wait."""
        self.state = state
        self.cfg = cfg
        self._now = now or (lambda: datetime.now(timezone.utc))
        self.rng = rng or random.Random()
        self._sleep = sleep or asyncio.sleep
        self.actions_since_break = 0
        self.slept = 0.0

    # ── Hours ──

    @property
    def zone(self):
        return pytz.timezone(self.cfg.hours.timezone)

    def local_now(self) -> datetime:
        return self._now().astimezone(self.zone)

    def in_hours(self, at: datetime | None = None) -> bool:
        local = (at or self._now()).astimezone(self.zone)
        hours = self.cfg.hours
        if hours.weekdays_only and local.weekday() >= 5:
            return False
        start, end = time.fromisoformat(hours.start), time.fromisoformat(hours.end)
        now = local.time()
        if start <= end:
            return start <= now < end
        return now >= start or now < end

    def hours_text(self) -> str:
        h = self.cfg.hours
        days = "weekdays" if h.weekdays_only else "every day"
        return f"{days} {h.start}-{h.end} {h.timezone}"

    # ── Caps ──

    def _keys(self, kind: str) -> list[str]:
        local = self.local_now()
        keys = [f"{COUNT_PREFIX}{kind}:{local.date().isoformat()}",
                f"{COUNT_PREFIX}action-hour:{local.strftime('%Y-%m-%dT%H')}"]
        if kind == "invite":
            year, week, _ = local.isocalendar()
            keys.append(f"{COUNT_PREFIX}invite-week:{year}-W{week:02d}")
        return keys

    async def _count(self, key: str) -> int:
        try:
            return int(await self.state.get_setting(key, "0") or 0)
        except ValueError:
            return 0

    async def used(self) -> dict:
        local = self.local_now()
        day = local.date().isoformat()
        year, week, _ = local.isocalendar()
        return {
            "invites_today": await self._count(f"{COUNT_PREFIX}invite:{day}"),
            "messages_today": await self._count(f"{COUNT_PREFIX}message:{day}"),
            "withdrawals_today": await self._count(f"{COUNT_PREFIX}withdraw:{day}"),
            "invites_week": await self._count(f"{COUNT_PREFIX}invite-week:{year}-W{week:02d}"),
            "actions_hour": await self._count(
                f"{COUNT_PREFIX}action-hour:{local.strftime('%Y-%m-%dT%H')}"),
        }

    def caps(self) -> dict:
        c = self.cfg
        return {"invites_today": c.max_daily_connections, "messages_today": c.max_daily_messages,
                "withdrawals_today": c.max_daily_withdrawals,
                "invites_week": c.max_weekly_invites, "actions_hour": c.max_actions_per_hour}

    async def blocked(self, kind: str) -> str:
        """Why one more ``kind`` action can't happen now, or ""."""
        used, caps = await self.used(), self.caps()
        if used["actions_hour"] >= caps["actions_hour"]:
            return f"{caps['actions_hour']} actions this hour already"
        if kind == "invite":
            if used["invites_today"] >= caps["invites_today"]:
                return f"today's {caps['invites_today']} invites are used"
            if used["invites_week"] >= caps["invites_week"]:
                return f"this week's {caps['invites_week']} invites are used"
        elif kind == "message" and used["messages_today"] >= caps["messages_today"]:
            return f"today's {caps['messages_today']} messages are used"
        elif kind == "withdraw" and used["withdrawals_today"] >= caps["withdrawals_today"]:
            return f"today's {caps['withdrawals_today']} withdrawals are used"
        return ""

    async def record(self, kind: str) -> None:
        if kind not in KINDS:
            raise ValueError(kind)
        for key in self._keys(kind):
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

    # ── Waiting like a person ──

    def delay_seconds(self) -> float:
        """The next wait between actions: 60-240 s, and a break of 5-15
        minutes every ``break_every_actions`` actions."""
        c = self.cfg
        self.actions_since_break += 1
        wait = self.rng.uniform(c.min_delay_seconds, c.max_delay_seconds)
        if c.break_every_actions and self.actions_since_break >= c.break_every_actions:
            self.actions_since_break = 0
            wait += self.rng.uniform(c.break_min_seconds, c.break_max_seconds)
        return wait

    async def between_actions(self) -> float:
        wait = self.delay_seconds()
        await self.sleep(wait)
        return wait

    async def between_pages(self, low: float = 4, high: float = 14) -> float:
        """A reading pause between page views (not an action)."""
        wait = self.rng.uniform(low, high)
        await self.sleep(wait)
        return wait

    async def sleep(self, seconds: float) -> None:
        self.slept += seconds
        await self._sleep(seconds)
