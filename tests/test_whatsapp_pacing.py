"""Caps, the 14-day ramp for a new number, and hours by the recipient's clock."""

import asyncio
import random
from datetime import datetime, timedelta, timezone

import pytest

from harvey.models.company import Company
from harvey.models.prospect import Prospect
from harvey.state import StateManager
from harvey.whatsapp.pacing import (
    LINKED_AT_KEY, WhatsAppPacing, mark_linked, recipient_zones,
)
from tests.whatsapp_fakes import JANE, OPEN, RAVI, SAM, make_config


def run(coro):
    return asyncio.run(coro)


@pytest.fixture
def state(tmp_path):
    s = StateManager(str(tmp_path / "h.db"))
    run(s.init_db())
    return s


def pacing(state, at=OPEN, **cfg):
    cfg.setdefault("min_delay_seconds", 45)
    cfg.setdefault("max_delay_seconds", 180)
    return WhatsAppPacing(state, make_config(**cfg).channels.whatsapp, now=lambda: at, rng=random.Random(1))


def link(state, days_ago, at=OPEN):
    when = at.astimezone(timezone.utc).replace(tzinfo=None) - timedelta(days=days_ago)
    run(state.set_setting(LINKED_AT_KEY, when.isoformat()))


# ── Defaults ──

def test_the_defaults_are_low():
    cfg = make_config().channels.whatsapp
    fresh = type(cfg)()
    assert fresh.enabled is False and fresh.autopilot is False and fresh.number == ""
    assert (fresh.max_daily_first_messages_new, fresh.max_daily_first_messages, fresh.new_number_days) == (5, 10, 14)
    assert (fresh.max_daily_messages, fresh.max_messages_per_hour) == (30, 8)
    assert (fresh.min_delay_seconds, fresh.max_delay_seconds) == (45, 180)
    assert (fresh.hours.start, fresh.hours.end, fresh.hours.weekdays_only) == ("10:00", "19:00", True)
    assert (fresh.follow_up_min_days, fresh.follow_up_max_days, fresh.max_unanswered) == (3, 5, 3)
    assert fresh.login_timeout_minutes == 30 and fresh.profile_dir == "data/whatsapp-profile"
    assert not hasattr(fresh, "headless")


def test_an_overnight_window_is_refused():
    with pytest.raises(Exception, match="before end"):
        make_config(hours={"start": "19:00", "end": "10:00", "timezone": "Europe/London"})


# ── The ramp ──

def test_first_messages_are_5_a_day_for_14_days_then_10(state):
    p = pacing(state)
    for days, cap in ((0, 5), (13, 5), (15, 10)):
        link(state, days)
        assert run(p.caps())["first_today"] == cap


def test_exactly_14_days_after_linking_the_ramp_is_over(state):
    link(state, 14)
    assert run(pacing(state).caps())["first_today"] == 10


def test_never_linked_counts_as_new(state):
    assert run(pacing(state).is_new_number()) is True
    assert run(pacing(state).caps())["first_today"] == 5


def test_linking_again_doesnt_restart_the_ramp_but_a_new_number_does(state):
    cfg = make_config(number="+15555550100").channels.whatsapp
    first = datetime(2026, 10, 1, 9, 0)
    run(mark_linked(state, cfg, at=first))
    run(mark_linked(state, cfg, at=first + timedelta(days=20)))
    assert run(state.get_setting(LINKED_AT_KEY)) == first.isoformat(timespec="seconds")
    other = make_config(number="+15555550101").channels.whatsapp
    run(mark_linked(state, other, at=first + timedelta(days=21)))
    assert run(state.get_setting(LINKED_AT_KEY)) == (first + timedelta(days=21)).isoformat(timespec="seconds")


# ── Caps ──

def test_the_first_message_cap(state):
    p = pacing(state)
    for _ in range(5):
        assert run(p.blocked("first")) == ""
        run(p.record("first"))
    assert "first messages" in run(p.blocked("first"))
    assert run(p.blocked("reply")) == ""       # a reply isn't a first message
    assert run(p.used())["first_today"] == 5 and run(p.used())["messages_today"] == 5


def test_the_daily_total_covers_every_kind(state):
    p = pacing(state, max_daily_messages=3, max_messages_per_hour=99)
    for kind in ("first", "follow_up", "reply"):
        run(p.record(kind))
    for kind in ("first", "follow_up", "reply"):
        assert "today's 3 messages" in run(p.blocked(kind))


def test_the_hourly_cap(state):
    p = pacing(state, max_messages_per_hour=2)
    run(p.record("reply"))
    run(p.record("reply"))
    assert "this hour" in run(p.blocked("reply"))
    later = pacing(state, at=OPEN + timedelta(hours=1), max_messages_per_hour=2)
    assert run(later.blocked("reply")) == ""


def test_a_restart_doesnt_reset_a_cap(state):
    run(pacing(state).record("first"))
    assert run(pacing(state).used())["first_today"] == 1


def test_a_new_day_starts_the_counts(state):
    run(pacing(state).record("first"))
    assert run(pacing(state, at=OPEN + timedelta(days=1)).used())["first_today"] == 0


def test_unknown_kind_is_refused(state):
    with pytest.raises(ValueError):
        run(pacing(state).record("broadcast"))


def test_delays_are_45_to_180_seconds_with_longer_breaks(state):
    p = pacing(state, break_min_seconds=300, break_max_seconds=900)
    waits = [p.delay_seconds() for _ in range(20)]
    plain = [w for i, w in enumerate(waits, 1) if i % 5]
    breaks = [w for i, w in enumerate(waits, 1) if not i % 5]
    assert all(45 <= w <= 180 for w in plain)
    assert all(w >= 45 + 300 for w in breaks)


# ── Hours, by the recipient's clock ──

def contact(phone=JANE, country="", prospect_id=""):
    return {"phone": phone, "country": country, "prospect_id": prospect_id}


def test_a_known_zone_goes_by_their_clock(state):
    zones = run(recipient_zones(state, contact(country="GB")))
    assert zones == ["Europe/London"]
    p = pacing(state, at=datetime(2026, 11, 4, 12, 0, tzinfo=timezone.utc))
    assert p.recipient_open(zones)
    assert not pacing(state, at=datetime(2026, 11, 4, 9, 59, tzinfo=timezone.utc)).recipient_open(zones)
    assert not pacing(state, at=datetime(2026, 11, 4, 19, 0, tzinfo=timezone.utc)).recipient_open(zones)


def test_india_is_shut_when_london_is_open(state):
    zones = run(recipient_zones(state, contact(RAVI, country="IN")))
    assert zones == ["Asia/Kolkata"]
    assert not pacing(state).recipient_open(zones)                     # 00:00 in India
    morning = datetime(2026, 11, 4, 6, 0, tzinfo=timezone.utc)         # 11:30 in India
    assert pacing(state, at=morning).recipient_open(zones)


def test_weekdays_only_by_their_calendar(state):
    zones = ["Asia/Kolkata"]
    saturday_there = datetime(2026, 11, 6, 20, 0, tzinfo=timezone.utc)   # already Saturday in India
    assert not pacing(state, at=saturday_there).recipient_open(zones)
    p = pacing(state, at=datetime(2026, 11, 7, 6, 0, tzinfo=timezone.utc),
               hours={"start": "10:00", "end": "19:00", "timezone": "Europe/London", "weekdays_only": False})
    assert p.recipient_open(zones)


def test_a_company_location_is_better_than_a_country(state):
    company_id = run(state.add_company(Company(name="Example Goods", domain="goods.example",
                                               location="Denver, CO")))
    pid = run(state.add_prospect(Prospect(first_name="Jane", last_name="Doe", title="Founder",
                                          email="jane@goods.example", company_id=company_id)))
    assert run(recipient_zones(state, contact(SAM, country="US", prospect_id=pid))) == ["America/Denver"]


def test_a_plus_1_number_with_no_zone_must_be_daytime_on_both_coasts(state):
    zones = run(recipient_zones(state, contact(SAM, country="US")))
    assert zones == ["America/New_York", "America/Los_Angeles"]
    assert run(recipient_zones(state, contact(SAM))) == zones
    assert pacing(state).recipient_open(zones)                         # 13:30 NY, 10:30 LA
    too_early_west = datetime(2026, 11, 4, 16, 0, tzinfo=timezone.utc)  # 11:00 NY, 08:00 LA
    assert not pacing(state, at=too_early_west).recipient_open(zones)
    too_late_east = datetime(2026, 11, 5, 0, 30, tzinfo=timezone.utc)   # 19:30 NY, 16:30 LA
    assert not pacing(state, at=too_late_east).recipient_open(zones)


def test_nobody_knows_where_they_are_so_the_channels_hours_apply(state):
    unknown = contact("+6512345678")
    assert run(recipient_zones(state, unknown)) == []
    assert pacing(state).recipient_open([])                            # 18:30 in London, the channel's zone
    shut = datetime(2026, 11, 4, 20, 0, tzinfo=timezone.utc)
    assert not pacing(state, at=shut).recipient_open([])
    ist = {"start": "10:00", "end": "19:00", "timezone": "Asia/Kolkata"}
    assert not pacing(state, hours=ist).recipient_open([])
