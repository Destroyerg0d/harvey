"""The mailbox columns: added to an existing database without a numbered
migration, inherited by outbox rows, and counted with "" as the primary."""

import sqlite3
from datetime import datetime, timedelta, timezone

import pytest
import pytest_asyncio

import harvey.state as state_mod
from harvey.models.conversation import Conversation
from harvey.models.prospect import Prospect
from harvey.state import MIGRATIONS, StateManager

PRIMARY = "primary@sender-one.test"
SECOND = "second@sender-one.test"
THIRD = "third@sender-two.test"


def _now(minutes: int = 0) -> str:
    return (datetime.now(timezone.utc).replace(tzinfo=None)
            + timedelta(minutes=minutes)).isoformat()


@pytest_asyncio.fixture
async def state(tmp_path):
    sm = StateManager(str(tmp_path / "harvey.db"))
    await sm.init_db()
    return sm


async def prospect(state, email="lead@prospect.test", mailbox=""):
    return await state.add_prospect(Prospect(
        first_name="Lee", last_name="Lead", title="Owner", email=email,
        email_status="verified", mailbox=mailbox))


async def row(state, pid, *, mailbox=None, status="approved", kind="sequence", step=1,
              send_at=None, campaign_id="", sent_at=None):
    item_id = await state.add_outbox_item(
        prospect_id=pid, to_email="lead@prospect.test", subject="Hi", body="Hello?",
        send_at=send_at or _now(-5), status=status, kind=kind, step=step,
        campaign_id=campaign_id, mailbox=mailbox)
    if sent_at:
        await state.update_outbox_item(item_id, status="sent", sent_at=sent_at)
    return item_id


def columns(path, table):
    con = sqlite3.connect(path)
    try:
        return {r[1]: r for r in con.execute(f"PRAGMA table_info({table})")}
    finally:
        con.close()


# ── Schema ──

def test_the_mailbox_columns_are_not_a_numbered_migration():
    # A numbered one would shift upstream's numbering and skip its next migration.
    assert not any("add column mailbox" in " ".join(script.lower().split())
                   for script in MIGRATIONS)


@pytest.mark.asyncio
async def test_init_db_adds_the_columns_to_an_existing_database(tmp_path):
    path = str(tmp_path / "old.db")
    con = sqlite3.connect(path)
    for target, script in enumerate(MIGRATIONS, start=1):
        con.executescript(script)
        con.execute(f"PRAGMA user_version = {target}")
    con.execute("INSERT INTO prospects (id, email, status) VALUES ('p1', 'lead@prospect.test', 'queued')")
    con.execute("INSERT INTO outbox (id, prospect_id, to_email, status) "
                "VALUES ('o1', 'p1', 'lead@prospect.test', 'approved')")
    con.execute("INSERT INTO conversations (id, prospect_id) VALUES ('c1', 'p1')")
    con.commit()
    con.close()
    for table in ("prospects", "outbox", "conversations"):
        assert "mailbox" not in columns(path, table)

    state = StateManager(path)
    await state.init_db()
    await state.init_db()                       # idempotent
    for table in ("prospects", "outbox", "conversations"):
        assert columns(path, table)["mailbox"][4] == "''"     # DEFAULT ''
    con = sqlite3.connect(path)
    assert con.execute("PRAGMA user_version").fetchone()[0] == len(MIGRATIONS)
    con.close()
    # What was there reads as the primary's ("").
    assert (await state.get_prospect("p1")).mailbox == ""
    assert (await state.get_outbox_item("o1"))["mailbox"] == ""
    assert (await state.get_conversation("c1")).mailbox == ""


@pytest.mark.asyncio
async def test_another_process_adding_the_column_first_is_fine(tmp_path, monkeypatch):
    state = StateManager(str(tmp_path / "race.db"))
    await state.init_db()
    # As if the table_info read happened before the other process's ALTER.
    real_execute = state_mod.aiosqlite.Connection.execute

    def stale_table_info(self, sql, *args, **kwargs):
        if sql.startswith("PRAGMA table_info("):
            sql = "SELECT 0, 'not-mailbox' WHERE 0"
        return real_execute(self, sql, *args, **kwargs)
    monkeypatch.setattr(state_mod.aiosqlite.Connection, "execute", stale_table_info)
    await state.init_db()


# ── Prospects and conversations ──

@pytest.mark.asyncio
async def test_a_prospect_keeps_its_mailbox(state):
    pid = await prospect(state, mailbox=" Second@Sender-One.TEST ")
    assert (await state.get_prospect(pid)).mailbox == SECOND
    other = await prospect(state, email="other@prospect.test")
    assert (await state.get_prospect(other)).mailbox == ""
    assert await state.set_prospect_mailbox(other, THIRD.upper())
    assert (await state.get_prospect(other)).mailbox == THIRD
    assert (await state.get_prospect_by_email("other@prospect.test")).mailbox == THIRD
    assert not await state.set_prospect_mailbox("no-such-id", THIRD)


@pytest.mark.asyncio
async def test_a_conversation_records_its_mailbox(state):
    pid = await prospect(state)
    cid = await state.add_conversation(Conversation(id="", prospect_id=pid, mailbox=SECOND.upper()))
    assert (await state.get_conversation(cid)).mailbox == SECOND
    plain = await state.add_conversation(Conversation(id="", prospect_id=pid))
    assert (await state.get_conversation(plain)).mailbox == ""
    await state.update_conversation(plain, mailbox=" Third@Sender-Two.test")
    assert (await state.get_conversation(plain)).mailbox == THIRD
    assert {c.mailbox for c in await state.get_conversations_by_status("open")} == {SECOND, THIRD}


# ── Outbox ──

@pytest.mark.asyncio
async def test_an_outbox_row_takes_its_prospects_mailbox(state):
    pid = await prospect(state, mailbox=SECOND)
    inherited = await row(state, pid)
    assert (await state.get_outbox_item(inherited))["mailbox"] == SECOND
    primary = await row(state, pid, mailbox="", kind="reply")
    assert (await state.get_outbox_item(primary))["mailbox"] == ""
    chosen = await row(state, pid, mailbox="Third@Sender-Two.TEST", kind="reply")
    assert (await state.get_outbox_item(chosen))["mailbox"] == THIRD
    orphan = await row(state, "no-such-prospect", kind="reply")
    assert (await state.get_outbox_item(orphan))["mailbox"] == ""


@pytest.mark.asyncio
async def test_the_double_send_guard_still_holds(state):
    pid = await prospect(state, mailbox=SECOND)
    assert await row(state, pid, campaign_id="c1")
    assert await row(state, pid, campaign_id="c1") is None


@pytest.mark.asyncio
async def test_update_outbox_item_can_move_a_row(state):
    pid = await prospect(state)
    item = await row(state, pid)
    await state.update_outbox_item(item, mailbox=" Second@Sender-One.test ")
    assert (await state.get_outbox_item(item))["mailbox"] == SECOND


@pytest.mark.asyncio
async def test_release_hands_back_only_a_row_being_sent(state):
    pid = await prospect(state)
    item = await row(state, pid)
    assert not await state.release_outbox_item(item)          # approved, not claimed
    assert await state.claim_outbox_item(item)
    await state.update_outbox_item(item, error="mailbox down")
    assert await state.release_outbox_item(item)
    released = await state.get_outbox_item(item)
    assert (released["status"], released["error"]) == ("approved", "")
    assert not await state.release_outbox_item(item)          # already released
    assert await state.claim_outbox_item(item)
    await state.update_outbox_item(item, status="sent", sent_at=_now())
    assert not await state.release_outbox_item(item)
    assert (await state.get_outbox_item(item))["status"] == "sent"
    assert not await state.release_outbox_item("no-such-item")


@pytest.mark.asyncio
async def test_sent_counts_treat_blank_as_the_primary(state):
    legacy = await prospect(state, email="a@prospect.test")
    second = await prospect(state, email="b@prospect.test", mailbox=SECOND)
    await row(state, legacy, sent_at=_now(-60))                         # "" = primary
    await row(state, legacy, mailbox=PRIMARY, kind="reply", sent_at=_now(-30))
    await row(state, second, sent_at=_now(-10))
    await row(state, second, kind="reply", sent_at=_now(-26 * 60))       # outside 24h
    await row(state, second)                                            # not sent
    since = _now(-24 * 60)
    assert await state.count_outbox_sent_since(since) == 3
    assert await state.count_outbox_sent_since(since, mailbox=PRIMARY, primary=PRIMARY) == 2
    assert await state.count_outbox_sent_since(since, mailbox="", primary=PRIMARY) == 2
    assert await state.count_outbox_sent_since(
        since, mailbox=PRIMARY.upper(), primary=" " + PRIMARY.upper()) == 2
    assert await state.count_outbox_sent_since(since, mailbox=SECOND, primary=PRIMARY) == 1
    assert await state.count_outbox_sent_since(
        since, mailbox=PRIMARY, primary=PRIMARY, kind="sequence") == 1
    assert await state.count_outbox_sent_since(_now(-27 * 60), mailbox=SECOND,
                                               primary=PRIMARY) == 2
    assert await state.count_outbox_sent_since(since, mailbox=THIRD, primary=PRIMARY) == 0


@pytest.mark.asyncio
async def test_get_outbox_filters_by_mailbox(state):
    legacy = await prospect(state, email="a@prospect.test")
    second = await prospect(state, email="b@prospect.test", mailbox=SECOND)
    third = await prospect(state, email="c@prospect.test", mailbox=THIRD)
    blank = await row(state, legacy)
    named = await row(state, legacy, mailbox=PRIMARY, kind="reply")
    to_second = await row(state, second)
    to_third = await row(state, third, status="pending_review")

    def ids(rows):
        return {r["id"] for r in rows}
    assert ids(await state.get_outbox()) == {blank, named, to_second, to_third}
    assert ids(await state.get_outbox(mailboxes=[PRIMARY], primary=PRIMARY)) == {blank, named}
    assert ids(await state.get_outbox(mailboxes=[""], primary=PRIMARY)) == {blank, named}
    assert ids(await state.get_outbox(mailboxes=[SECOND.upper(), THIRD],
                                      primary=PRIMARY)) == {to_second, to_third}
    assert ids(await state.get_outbox(status="approved", mailboxes=[SECOND, THIRD],
                                      primary=PRIMARY)) == {to_second}
    assert ids(await state.get_outbox(kind="sequence", mailboxes=[PRIMARY],
                                      primary=PRIMARY)) == {blank}
    assert await state.get_outbox(mailboxes=[], primary=PRIMARY) == []
    # Single mailbox, no persona.email: "" rows are the only mailbox.
    assert ids(await state.get_outbox(mailboxes=[""], primary="")) == {blank}


@pytest.mark.asyncio
async def test_due_cold_counts_filter_by_mailbox(state):
    legacy = await prospect(state, email="a@prospect.test")
    second = await prospect(state, email="b@prospect.test", mailbox=SECOND)
    await row(state, legacy)
    await row(state, second)
    await row(state, second, send_at=_now(60))                          # not due
    await row(state, second, kind="reply")                              # not cold
    now = _now()
    assert await state.count_due_cold_outbox(now) == 2
    assert await state.count_due_cold_outbox(now, mailboxes=[PRIMARY], primary=PRIMARY) == 1
    assert await state.count_due_cold_outbox(now, mailboxes=[SECOND], primary=PRIMARY) == 1
    assert await state.count_due_cold_outbox(now, mailboxes=[THIRD], primary=PRIMARY) == 0
    assert await state.count_due_cold_outbox(now, mailboxes=[], primary=PRIMARY) == 0
    seen = []

    async def no_verdict(item):
        seen.append(item["prospect_id"])
        return False
    assert await state.count_due_cold_outbox(
        now, has_cached_verdict=no_verdict, mailboxes=[SECOND], primary=PRIMARY) == 1
    assert seen == [second]
