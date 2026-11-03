"""The LinkedIn tables: stage machine, dedupe, the approval ladder, the funnel."""

import asyncio
import sqlite3

import pytest

from harvey.linkedin.store import HELD_FOR_REVIEW, MAYBE_SENT, advance_stage
from harvey.state import StateManager


def run(coro):
    return asyncio.run(coro)


@pytest.fixture
def state(tmp_path):
    s = StateManager(str(tmp_path / "h.db"))
    run(s.init_db())
    return s


def test_tables_are_created_and_init_is_repeatable(tmp_path):
    s = StateManager(str(tmp_path / "h.db"))
    run(s.init_db())
    run(s.init_db())
    with sqlite3.connect(tmp_path / "h.db") as db:
        names = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"linkedin_leads", "linkedin_messages"} <= names


@pytest.mark.parametrize("current,target,expected", [
    ("found", "invited", "invited"), ("invited", "accepted", "accepted"),
    ("accepted", "messaged", "messaged"), ("messaged", "replied", "replied"),
    ("replied", "warm", "warm"), ("warm", "meeting", "meeting"),
    ("messaged", "accepted", "messaged"),          # never backwards
    ("replied", "messaged", "replied"),
    ("warm", "declined", "declined"), ("declined", "warm", "declined"),   # a no is final
    ("declined", "invited", "declined"),
    ("invited", "withdrawn", "withdrawn"), ("accepted", "withdrawn", "accepted"),
    ("withdrawn", "invited", "invited"), ("withdrawn", "accepted", "accepted"),
    ("found", "bogus", "found"),
])
def test_stage_machine(current, target, expected):
    assert advance_stage(current, target) == expected


def test_upsert_fills_blanks_and_only_moves_forward(state):
    lead_id, created = run(state.li_upsert_lead("Jane-Doe-1", stage="accepted", name="Jane Doe"))
    assert created
    again, created = run(state.li_upsert_lead("jane-doe-1", stage="invited", name="Other",
                                              company="Example Goods"))
    assert again == lead_id and not created
    lead = run(state.li_get_lead(lead_id))
    assert lead["stage"] == "accepted" and lead["name"] == "Jane Doe" and lead["company"] == "Example Goods"
    with pytest.raises(ValueError):
        run(state.li_upsert_lead(""))


def test_messages_dedupe_and_the_approval_ladder(state):
    lead_id, _ = run(state.li_upsert_lead("ada-lane", name="Ada Lane", stage="accepted"))
    m1 = run(state.li_add_message(lead_id, body="Hello Ada"))
    assert run(state.li_add_message(lead_id, body="Hello Ada")) is None      # same draft twice
    held = run(state.li_add_message(lead_id, kind="reply", body="Yes, automated",
                                    error=HELD_FOR_REVIEW + "they asked", dedupe=False))
    assert run(state.li_edit_draft(m1, "Hello Ada, edited")) == 1
    assert run(state.li_approve(None)) == 1                 # held one waits for a one-by-one approve
    assert run(state.li_get_message(held))["status"] == "draft"
    assert run(state.li_approve(held)) == 1
    assert run(state.li_edit_draft(m1, "too late")) == 0    # approved: no longer editable
    assert run(state.li_get_message(m1))["approved_by"] == "you"


def test_mark_sent_moves_the_lead_and_autopilot_off_requeues(state):
    lead_id, _ = run(state.li_upsert_lead("bo-chen", name="Bo Chen"))
    invite = run(state.li_add_message(lead_id, kind="invite_note", body="", status="approved",
                                      approved_by="autopilot"))
    msg = run(state.li_add_message(lead_id, body="Hi Bo", status="approved", approved_by="autopilot"))
    mine = run(state.li_add_message(lead_id, body="Mine", status="approved", approved_by="you"))
    run(state.li_mark_sent(invite))
    lead = run(state.li_get_lead(lead_id))
    assert lead["stage"] == "invited" and lead["invited_at"]
    assert run(state.li_requeue_autopilot()) == 1
    assert run(state.li_get_message(msg))["status"] == "draft"
    assert run(state.li_get_message(mine))["status"] == "approved"


def test_retry_never_resends_one_that_may_have_gone(state):
    lead_id, _ = run(state.li_upsert_lead("cy-park", name="Cy Park"))
    a = run(state.li_add_message(lead_id, body="A", status="failed", error="no Message button"))
    b = run(state.li_add_message(lead_id, body="B", status="failed", error=MAYBE_SENT + "check"))
    assert run(state.li_retry(a)) == 1 and run(state.li_get_message(a))["status"] == "approved"
    assert run(state.li_retry(b)) == 0


def test_unanswered_counts_since_their_last_message(state):
    lead_id, _ = run(state.li_upsert_lead("di-ross", name="Di Ross"))
    run(state.li_add_message(lead_id, kind="invite_note", body="n", status="sent", sent_at="2026-10-01T10:00:00"))
    run(state.li_add_message(lead_id, body="m1", status="sent", sent_at="2026-10-03T10:00:00"))
    assert len(run(state.li_unanswered(lead_id))) == 2
    run(state.li_add_message(lead_id, direction="in", body="hi", status="sent", sent_at="2026-10-04T10:00:00"))
    assert run(state.li_unanswered(lead_id)) == []
    run(state.li_add_message(lead_id, kind="reply", body="r1", status="sent", sent_at="2026-10-05T10:00:00"))
    assert [m["body"] for m in run(state.li_unanswered(lead_id))] == ["r1"]


def test_funnel_and_rates(state):
    for slug, stage in (("a", "invited"), ("b", "accepted"), ("c", "messaged"), ("d", "replied"),
                        ("e", "warm"), ("f", "meeting"), ("g", "declined"), ("h", "found")):
        lead_id, _ = run(state.li_upsert_lead(slug, stage=stage, name=slug.upper()))
        if stage in ("replied", "warm", "meeting"):
            run(state.li_update_lead(lead_id, last_inbound_at="2026-11-01T10:00:00"))
    f = run(state.li_funnel())
    assert (f["found"], f["invited"], f["accepted"], f["messaged"], f["replied"],
            f["positive"], f["meetings"], f["declined"]) == (8, 6, 5, 4, 3, 2, 1, 1)
    assert f["acceptance_rate"] == round(5 / 6, 4) and f["reply_rate"] == 0.75
    assert f["positive_rate"] == round(2 / 3, 4)


def test_daily_activity_counts_sent_items_per_day(state):
    from datetime import datetime, timedelta, timezone
    today = datetime.now(timezone.utc).replace(tzinfo=None, microsecond=0)
    lead_id, _ = run(state.li_upsert_lead("e", name="E"))
    run(state.li_add_message(lead_id, kind="invite_note", status="sent", sent_at=today, dedupe=False))
    run(state.li_add_message(lead_id, body="m", status="sent", sent_at=today - timedelta(days=1)))
    run(state.li_add_message(lead_id, direction="in", body="r", status="sent", sent_at=today))
    run(state.li_add_message(lead_id, body="draft", status="draft"))
    chart = run(state.li_daily_activity(7))
    assert len(chart["days"]) == 7
    assert chart["invites"][-1] == 1 and chart["replies"][-1] == 1
    assert chart["messages"][-2] == 1 and sum(chart["messages"]) == 1
