"""LinkedIn hours, delays, breaks and caps."""

import asyncio
import random
from datetime import datetime, timezone

from harvey.linkedin.pacing import Pacing
from harvey.state import StateManager
from tests.linkedin_fakes import make_config


def _pacing(tmp_path, at, **cfg):
    state = StateManager(str(tmp_path / "h.db"))
    asyncio.run(state.init_db())
    slept = []

    async def sleep(s):
        slept.append(s)
    pacing = Pacing(state, make_config(**cfg).channels.linkedin, now=lambda: at,
                    rng=random.Random(1), sleep=sleep)
    return pacing, slept


def test_hours_are_weekdays_10_to_19_india_time(tmp_path):
    utc = lambda *a: datetime(*a, tzinfo=timezone.utc)  # noqa: E731
    p, _ = _pacing(tmp_path, utc(2026, 11, 4, 6, 30))
    assert p.in_hours(utc(2026, 11, 4, 4, 30))        # Wed 10:00 IST
    assert not p.in_hours(utc(2026, 11, 4, 4, 29))    # 09:59
    assert p.in_hours(utc(2026, 11, 4, 13, 29))       # 18:59
    assert not p.in_hours(utc(2026, 11, 4, 13, 30))   # 19:00
    assert not p.in_hours(utc(2026, 11, 7, 6, 30))    # Saturday noon
    p2, _ = _pacing(tmp_path, utc(2026, 11, 7, 6, 30), hours={"weekdays_only": False})
    assert p2.in_hours()


def test_delays_are_60_to_240_seconds_with_a_break_every_few_actions(tmp_path):
    p, slept = _pacing(tmp_path, datetime(2026, 11, 4, 6, 30, tzinfo=timezone.utc),
                       min_delay_seconds=60, max_delay_seconds=240, break_every_actions=5,
                       break_min_seconds=300, break_max_seconds=900)
    for _ in range(10):
        asyncio.run(p.between_actions())
    plain = [s for i, s in enumerate(slept) if (i + 1) % 5]
    breaks = [slept[4], slept[9]]
    assert all(60 <= s <= 240 for s in plain)
    assert all(360 <= s <= 1140 for s in breaks)


def test_daily_weekly_and_hourly_caps(tmp_path):
    at = datetime(2026, 11, 4, 6, 30, tzinfo=timezone.utc)
    p, _ = _pacing(tmp_path, at, max_daily_connections=2, max_weekly_invites=3,
                   max_daily_messages=1, max_actions_per_hour=12)
    assert asyncio.run(p.blocked("invite")) == ""
    for _ in range(2):
        asyncio.run(p.record("invite"))
    assert "invites" in asyncio.run(p.blocked("invite"))
    assert asyncio.run(p.blocked("message")) == ""
    asyncio.run(p.record("message"))
    assert "messages" in asyncio.run(p.blocked("message"))
    used = asyncio.run(p.used())
    assert used == {"invites_today": 2, "messages_today": 1, "withdrawals_today": 0,
                    "invites_week": 2, "actions_hour": 3}

    # Next day (same week): the daily cap is fresh, the weekly one is not.
    p._now = lambda: datetime(2026, 11, 5, 6, 30, tzinfo=timezone.utc)
    assert asyncio.run(p.blocked("invite")) == ""
    asyncio.run(p.record("invite"))
    assert "week" in asyncio.run(p.blocked("invite"))


def test_the_hourly_cap_stops_everything(tmp_path):
    at = datetime(2026, 11, 4, 6, 30, tzinfo=timezone.utc)
    p, _ = _pacing(tmp_path, at, max_actions_per_hour=2)
    asyncio.run(p.record("message"))
    asyncio.run(p.record("withdraw"))
    assert "this hour" in asyncio.run(p.blocked("invite"))
    p._now = lambda: datetime(2026, 11, 4, 7, 31, tzinfo=timezone.utc)
    assert asyncio.run(p.blocked("invite")) == ""


def test_caps_come_from_the_existing_config_names(tmp_path):
    cfg = make_config(max_daily_connections=9, max_daily_messages=7).channels.linkedin
    p = Pacing(None, cfg)
    assert p.caps()["invites_today"] == 9 and p.caps()["messages_today"] == 7
    defaults = make_config().channels.linkedin
    assert (defaults.max_daily_connections, defaults.max_daily_messages,
            defaults.max_weekly_invites) == (15, 25, 75)
