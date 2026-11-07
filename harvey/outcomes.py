"""The outcome table: how each kind of cold email did, read-only.

One row per (channel, segment, angle, step, mailbox, country) for the
sequence emails sent in the last ``days`` days: how many went out, and what
came back to them. A reply is tied to the email it answers (``email_answered``:
its In-Reply-To, else the last sequence email sent to that person before it),
so a reply to an email sent before the window is not counted against one
inside it. Automatic replies (out-of-office, helpdesk acknowledgements) are
never human replies; a "no" an auto-reply gave (a company that takes no sales
email) still counts as not interested.

The Analyst writes it to data/outcomes.json on each run. Nothing here changes
the database.
"""

import json
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import aiosqlite

from harvey.models.conversation import Conversation
from harvey.segment_stamp import segment_for_company
from harvey.send_window import country_for
from harvey.state import AUTO_CONVERSATION, AUTOMATIC_INTENTS

logger = logging.getLogger("harvey.outcomes")

OUTCOMES_FILE = "outcomes.json"
POSITIVE_INTENTS = frozenset({"interested", "question"})
POSITIVE_STAGES = frozenset({"closing", "closed_won"})
COUNTS = ("sent", "human_replies", "positive", "not_interested", "opt_outs", "bounces")
KEYS = ("channel", "segment", "angle", "step", "mailbox", "country")


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _id_forms(message_id: str) -> list[str]:
    """A Message-ID as stored and as headers carry it: with and without <>."""
    raw = (message_id or "").strip()
    if not raw:
        return []
    bare = raw.strip("<>").strip()
    return list(dict.fromkeys(x for x in (raw, f"<{bare}>", bare) if x))


def _their_messages(conversation: Conversation) -> list:
    """What they wrote, oldest first: a person's messages when there are any
    (a thread an auto-reply opened is the person's once they write), else
    the automatic ones."""
    people = [m for m in conversation.thread if m.sender == "prospect"]
    return people or [m for m in conversation.thread if m.sender not in ("harvey", "you")]


async def _one(db, sql: str, params) -> dict | None:
    async with db.execute(sql, params) as cursor:
        row = await cursor.fetchone()
        return dict(row) if row else None


async def email_answered(state, conversation: Conversation) -> dict | None:
    """The outbox row a conversation's first reply answers: the email its
    In-Reply-To names, else the last sequence email sent to that prospect
    before the reply. None when neither is known."""
    theirs = _their_messages(conversation)
    for message in theirs:
        for message_id in _id_forms(getattr(message, "in_reply_to", "")):
            row = await state.find_outbox_by_message_id(message_id)
            if row is not None:
                return row
    when = theirs[0].timestamp if theirs else conversation.created_at
    async with aiosqlite.connect(state.db_path, timeout=30) as db:
        db.row_factory = aiosqlite.Row
        return await _one(db, (
            "SELECT * FROM outbox WHERE prospect_id = ? AND kind = 'sequence' "
            "AND status = 'sent' AND sent_at <= ? ORDER BY sent_at DESC LIMIT 1"),
            (conversation.prospect_id, when.isoformat()))


def _answered_id(conversation: Conversation, by_message_id: dict, sequence_rows: dict) -> str:
    """email_answered over preloaded rows, for a whole table at once: the
    sequence row's id, or "". A reply to one of Harvey's answers is tied to
    the last sequence email before it instead."""
    theirs = _their_messages(conversation)
    for message in theirs:
        for message_id in _id_forms(getattr(message, "in_reply_to", "")):
            hit = by_message_id.get(message_id)
            if hit is not None and hit[1] == "sequence":
                return hit[0]
    when = (theirs[0].timestamp if theirs else conversation.created_at).isoformat()
    best = ""
    best_at = ""
    for row_id, sent_at in sequence_rows.get(conversation.prospect_id, ()):
        if sent_at and sent_at <= when and sent_at >= best_at:
            best, best_at = row_id, sent_at
    return best


async def build_outcomes(state, days: int = 30, now: datetime | None = None) -> list[dict]:
    """The outcome rows for sequence emails sent in the last ``days`` days."""
    now = now or _utcnow()
    since = (now - timedelta(days=max(1, int(days)))).isoformat()
    async with aiosqlite.connect(state.db_path, timeout=30) as db:
        db.row_factory = aiosqlite.Row

        async def rows(sql, params=()):
            async with db.execute(sql, params) as cursor:
                return [dict(r) for r in await cursor.fetchall()]

        sent = await rows(
            "SELECT o.id, o.prospect_id, o.step, o.mailbox, o.segment, o.angle, o.to_email, "
            "o.sent_at, p.company_id, p.industry, p.status AS prospect_status, co.location "
            "FROM outbox o LEFT JOIN prospects p ON p.id = o.prospect_id "
            "LEFT JOIN companies co ON co.id = p.company_id "
            "WHERE o.kind = 'sequence' AND o.status = 'sent' AND o.sent_at >= ?", (since,))
        ids = await rows("SELECT id, message_id, kind FROM outbox WHERE COALESCE(message_id, '') != ''")
        history = await rows(
            "SELECT id, prospect_id, sent_at FROM outbox WHERE kind = 'sequence' AND status = 'sent'")
        conversations = await rows(
            "SELECT c.*, p.status AS prospect_status FROM conversations c "
            "LEFT JOIN prospects p ON p.id = c.prospect_id "
            "WHERE COALESCE(c.channel, 'email') = 'email' AND c.updated_at >= ?", (since,))
        bounces = await rows(
            "SELECT details_json FROM actions WHERE action_type = 'bounce' AND created_at >= ?",
            (since,))

    by_message_id: dict[str, tuple[str, str]] = {}
    for r in ids:
        for form in _id_forms(r["message_id"]):
            by_message_id.setdefault(form, (r["id"], r["kind"] or ""))
    sequence_rows: dict[str, list] = {}
    for r in history:
        sequence_rows.setdefault(r["prospect_id"], []).append((r["id"], str(r["sent_at"] or "")))

    segments: dict[tuple, str] = {}
    countries: dict[str, str] = {}
    table: dict[tuple, dict] = {}
    key_of: dict[str, tuple] = {}
    for r in sent:
        segment = (r.get("segment") or "").strip()
        if not segment:
            cache = (r.get("company_id") or "", r.get("industry") or "")
            if cache not in segments:
                segments[cache] = await segment_for_company(
                    state, cache[0], SimpleNamespace(industry=cache[1]))
            segment = segments[cache]
        location = r.get("location") or ""
        if location not in countries:
            code, certain = country_for(location) if location else ("", False)
            countries[location] = code if (code and certain) else "unknown"
        key = ("email", segment, (r.get("angle") or "").strip(), int(r.get("step") or 1),
               (r.get("mailbox") or "").strip().lower() or "primary", countries[location])
        key_of[r["id"]] = key
        row = table.setdefault(key, {**dict(zip(KEYS, key)), **dict.fromkeys(COUNTS, 0)})
        row["sent"] += 1

    for c in conversations:
        thread = Conversation.thread_from_json(c.get("thread_json"))
        try:
            created = datetime.fromisoformat(str(c.get("created_at")))
        except (TypeError, ValueError):
            created = now
        convo = Conversation(id=c["id"], prospect_id=c.get("prospect_id") or "",
                             thread=thread, created_at=created)
        key = key_of.get(_answered_id(convo, by_message_id, sequence_rows))
        if key is None:
            continue
        row = table[key]
        intent = c.get("intent") or ""
        automatic = c.get("status") == AUTO_CONVERSATION or intent in AUTOMATIC_INTENTS
        if not automatic:
            row["human_replies"] += 1
            if (intent in POSITIVE_INTENTS or c.get("stage") in POSITIVE_STAGES
                    or c.get("prospect_status") == "meeting"):
                row["positive"] += 1
        row["not_interested"] += intent == "not_interested"
        row["opt_outs"] += intent == "unsubscribe"

    # A bounce names the email it bounced (outbox_id) since outcomes were
    # recorded; an older one, only the address: its latest email in the window.
    latest_to: dict[str, tuple[str, str]] = {}
    for r in sent:
        address = (r.get("to_email") or "").lower()
        if address and str(r["sent_at"]) >= latest_to.get(address, ("", ""))[1]:
            latest_to[address] = (r["id"], str(r["sent_at"]))
    bounced: set[str] = set()
    for b in bounces:
        try:
            details = json.loads(b.get("details_json") or "{}")
        except (json.JSONDecodeError, TypeError):
            continue
        if not isinstance(details, dict):
            continue
        row_id = details.get("outbox_id") or latest_to.get(
            str(details.get("prospect") or "").lower(), ("", ""))[0]
        if row_id in key_of:
            bounced.add(row_id)
    for row_id in bounced:
        table[key_of[row_id]]["bounces"] += 1

    return [table[k] for k in sorted(table, key=lambda k: tuple(str(x) for x in k))]


async def write_outcomes(state, days: int = 30, path: Path | None = None) -> Path:
    """Build the table and write it next to the database, atomically (a temp
    file renamed over the old one), as analytics.json is."""
    rows = await build_outcomes(state, days=days)
    path = path or Path(state.db_path).parent / OUTCOMES_FILE
    report = {"generated_at": _utcnow().isoformat(), "days": days, "rows": rows}
    tmp = path.with_suffix(".json.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2)
    tmp.replace(path)
    return path
