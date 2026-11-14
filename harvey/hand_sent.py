"""Email you or a coworker sent by hand from one of Harvey's mailboxes.

Owner, 2026-11-14: "I might manually send emails from the inbox on your reply
thread, so make sure you notice that and show it in Harvey ... if a thread is
on Needs you I can reply on it ... I might hand the conversation to you after I
replied once, so you have to take the context."

Each cycle Harvey lists the last few days of every Gmail mailbox's Sent mail.
What Harvey sent itself (its Message-ID or Gmail id is in the Outbox) is
skipped. An email a person wrote that belongs to someone Harvey knows (it
answers a message of their thread, sits in their Gmail thread, or is addressed
to them) is:

- added to their conversation as yours (sender "you", from the mailbox it
  went out from), so the dashboard shows it and every later draft reads it as
  the owner's words;
- the conversation becomes WITH_YOU ("you're handling it"): Harvey doesn't
  answer and it leaves Needs you. Their next reply puts it back on Needs you.
  "Hand back to Harvey" opens it again, and Harvey answers their next reply
  with the whole thread, yours included;
- anything Harvey had queued for them is cancelled (a draft reply, the rest of
  the cold sequence): it was written before your email. What you queued from
  the dashboard stays.

Email to anyone Harvey doesn't know is left alone, and only its Gmail id is
kept (so it isn't fetched again).
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from email.utils import getaddresses, parsedate_to_datetime

import aiosqlite

from harvey.integrations.mail_provider import strip_quoted_reply
from harvey.models.conversation import Conversation, Message
from harvey.state import AUTO_CONVERSATION, HAND_SENT_REASON, MANUAL_SEND_KEY, WITH_YOU

logger = logging.getLogger("harvey.hand_sent")

SCAN_DAYS = 3
# A sent message this new may be Harvey's own, its Message-ID not yet stored.
SETTLE = timedelta(minutes=5)
KEEP_SEEN = timedelta(days=30)
HARVEY, HAND, OTHER = "harvey", "hand", "other"
CANCEL_REASON = "you wrote to them by hand"
# Where a hand-written email joins: the thread being worked first.
_JOIN_ORDER = ("needs_human", WITH_YOU, "open", AUTO_CONVERSATION, "awaiting_reply", "closed")


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


async def _ensure(state) -> None:
    async with aiosqlite.connect(state.db_path, timeout=30) as db:
        await db.execute(
            "CREATE TABLE IF NOT EXISTS sent_seen (mailbox TEXT NOT NULL, gmail_id TEXT NOT NULL, "
            "kind TEXT DEFAULT '', prospect_id TEXT DEFAULT '', seen_at TEXT DEFAULT '', "
            "PRIMARY KEY (mailbox, gmail_id))")
        await db.commit()


async def seen_ids(state, mailbox: str) -> set[str]:
    await _ensure(state)
    async with aiosqlite.connect(state.db_path, timeout=30) as db:
        async with db.execute("SELECT gmail_id FROM sent_seen WHERE mailbox = ?",
                              (mailbox.lower(),)) as cursor:
            return {r[0] for r in await cursor.fetchall()}


async def _mark_seen(state, mailbox: str, gmail_id: str, kind: str, prospect_id: str = "") -> None:
    async with aiosqlite.connect(state.db_path, timeout=30) as db:
        await db.execute(
            "INSERT OR REPLACE INTO sent_seen (mailbox, gmail_id, kind, prospect_id, seen_at) "
            "VALUES (?, ?, ?, ?, ?)", (mailbox.lower(), gmail_id, kind, prospect_id, _now().isoformat()))
        await db.commit()


async def _prune(state) -> None:
    async with aiosqlite.connect(state.db_path, timeout=30) as db:
        await db.execute("DELETE FROM sent_seen WHERE seen_at < ?",
                         ((_now() - KEEP_SEEN).isoformat(),))
        await db.commit()


def _sent_at(msg) -> datetime | None:
    try:
        when = parsedate_to_datetime(msg.date or "")
    except (TypeError, ValueError, IndexError):
        return None
    if when is None:
        return None
    if when.tzinfo is not None:
        when = when.astimezone(timezone.utc).replace(tzinfo=None)
    return when


async def is_harveys(state, msg) -> bool:
    """Harvey sent it: its Message-ID (or, when Gmail gave none back, its
    Gmail id) is an Outbox row's."""
    for key in (msg.message_id, msg.provider_id):
        if key and await state.find_outbox_by_message_id(key):
            return True
    return False


async def _prospect_in_threads(state, needle: str) -> str:
    """The prospect whose conversation thread holds this Message-ID or
    Gmail thread id, or ""."""
    if not needle:
        return ""
    async with aiosqlite.connect(state.db_path, timeout=30) as db:
        async with db.execute(
                "SELECT prospect_id FROM conversations WHERE channel = 'email' AND thread_json LIKE ? "
                "ORDER BY updated_at DESC LIMIT 1", (f'%"{needle}"%',)) as cursor:
            row = await cursor.fetchone()
    return row[0] if row else ""


async def _prospect_on_gmail_thread(state, thread_ref: str, mailbox: str) -> str:
    if not thread_ref:
        return ""
    async with aiosqlite.connect(state.db_path, timeout=30) as db:
        async with db.execute(
                "SELECT prospect_id FROM outbox WHERE thread_ref = ? AND prospect_id != '' "
                "AND (mailbox = ? OR mailbox = '' OR mailbox IS NULL) ORDER BY created_at DESC LIMIT 1",
                (thread_ref, mailbox.lower())) as cursor:
            row = await cursor.fetchone()
    return row[0] if row else await _prospect_in_threads(state, thread_ref)


async def whose(state, msg, own_addresses=()) -> object | None:
    """The prospect a hand-written email belongs to, or None: the thread it
    answers (In-Reply-To, References), then its Gmail thread, then whom it
    is addressed to."""
    from harvey.agents.handler import referenced_ids
    headers = msg.headers or {}
    for ref in referenced_ids(msg.in_reply_to, headers.get("references", "")):
        row = await state.find_outbox_by_message_id(ref)
        pid = (row or {}).get("prospect_id") or await _prospect_in_threads(state, ref)
        if pid:
            prospect = await state.get_prospect(pid)
            if prospect is not None:
                return prospect
    pid = await _prospect_on_gmail_thread(state, msg.thread_ref, msg.mailbox or "")
    if pid:
        prospect = await state.get_prospect(pid)
        if prospect is not None:
            return prospect
    own = {a.lower() for a in own_addresses}
    recipients = getaddresses([headers.get("to", ""), headers.get("cc", "")])
    for _, address in recipients:
        address = (address or "").strip().lower()
        if address and address not in own:
            prospect = await state.get_prospect_by_email(address)
            if prospect is not None:
                return prospect
    return None


async def _conversation_for(state, prospect_id: str) -> Conversation | None:
    found = []
    async with aiosqlite.connect(state.db_path, timeout=30) as db:
        async with db.execute(
                "SELECT id, status FROM conversations WHERE prospect_id = ? AND "
                "COALESCE(channel, 'email') = 'email' ORDER BY updated_at DESC", (prospect_id,)) as cursor:
            found = await cursor.fetchall()
    if not found:
        return None
    rank = {s: i for i, s in enumerate(_JOIN_ORDER)}
    convo_id = min(found, key=lambda r: rank.get(r[1], len(rank)))[0]
    return await state.get_conversation(convo_id)


async def _cancel_queued(state, prospect_id: str, before: datetime) -> int:
    """What Harvey queued for them before your email: cancelled. What you
    queued from the dashboard (approved_by "you") stays."""
    async with aiosqlite.connect(state.db_path, timeout=30) as db:
        cursor = await db.execute(
            "UPDATE outbox SET status = 'cancelled', error = ?, updated_at = ? WHERE prospect_id = ? "
            "AND status IN ('pending_review', 'approved') AND COALESCE(approved_by, '') != 'you' "
            "AND datetime(created_at) <= datetime(?)",
            (CANCEL_REASON, _now().isoformat(), prospect_id, before.isoformat(sep=" ", timespec="seconds")))
        await db.commit()
        return cursor.rowcount


def _naive(when: datetime) -> datetime:
    return when.astimezone(timezone.utc).replace(tzinfo=None) if when.tzinfo else when


async def record(state, prospect, msg, mailbox_address: str) -> dict:
    """Put a hand-written email in their conversation and step Harvey back."""
    text = (strip_quoted_reply(msg.body or "") or msg.body or "").strip()
    message = Message(sender="you", content=text, from_email=(mailbox_address or "").lower(),
                      in_reply_to=msg.in_reply_to or "", message_id=msg.message_id or "",
                      thread_ref=msg.thread_ref or "", subject=(msg.subject or "")[:300])
    when = _sent_at(msg) or _now()
    message.timestamp = when
    stays_closed = prospect.status == "opted_out"
    convo = await _conversation_for(state, prospect.id)
    cancelled = 0
    if convo is None:
        convo_id = await state.add_conversation(Conversation(
            id="", prospect_id=prospect.id, channel="email", stage="engaged",
            status="closed" if stays_closed else WITH_YOU, mailbox=(mailbox_address or "").lower(),
            thread=[message]))
        cancelled = await _cancel_queued(state, prospect.id, when)
    else:
        convo_id = convo.id
        if msg.message_id and any(m.message_id == msg.message_id for m in convo.thread):
            return {"conversation_id": convo_id, "added": False, "cancelled": 0}
        # Read late (the first scan looks back days): if the thread moved on
        # after your email, it is only added, for the record and the context.
        moved_on = any(_naive(m.timestamp) > when for m in convo.thread)
        convo.thread.append(message)
        convo.thread.sort(key=lambda m: _naive(m.timestamp))
        status = convo.status if stays_closed or moved_on else WITH_YOU
        await state.update_conversation(convo_id, thread_json=convo.thread_json(), status=status)
        if not moved_on:
            cancelled = await _cancel_queued(state, prospect.id, when)
    await state.set_setting(MANUAL_SEND_KEY + prospect.id, _now().isoformat())
    await state.log_action("hand_sent", "handler", {
        "prospect_id": prospect.id, "prospect_email": prospect.email, "conversation_id": convo_id,
        "mailbox": mailbox_address, "subject": (msg.subject or "")[:120], "cancelled": cancelled})
    logger.info(f"Hand-sent: an email to {prospect.email} from {mailbox_address} is on their "
                f"conversation; Harvey steps back" + (f" ({cancelled} queued cancelled)." if cancelled else "."))
    return {"conversation_id": convo_id, "added": True, "cancelled": cancelled}


async def scan(state, providers, own_addresses=(), now: datetime | None = None) -> dict:
    """Every mailbox's recent Sent mail: ``providers`` is [(address, provider)].
    Returns counts; a mailbox that can't be read is skipped (next cycle)."""
    await _ensure(state)
    now = now or _now()
    counts = {"harvey": 0, "hand": 0, "other": 0, "unreadable": 0}
    for address, provider in providers:
        if not hasattr(provider, "get_sent"):
            continue
        address = (address or "").lower()
        try:
            messages = await provider.get_sent(SCAN_DAYS, known=await seen_ids(state, address))
        except Exception as e:
            logger.info(f"Hand-sent: couldn't read {address}'s sent mail: {e}")
            counts["unreadable"] += 1
            continue
        for msg in messages:
            when = _sent_at(msg)
            if when is not None and now - when < SETTLE:
                continue                     # looked at next cycle
            msg.mailbox = msg.mailbox or address
            if await is_harveys(state, msg):
                await _mark_seen(state, address, msg.provider_id, HARVEY)
                counts["harvey"] += 1
                continue
            prospect = await whose(state, msg, own_addresses)
            if prospect is None:
                await _mark_seen(state, address, msg.provider_id, OTHER)
                counts["other"] += 1
                continue
            await record(state, prospect, msg, address)
            await _mark_seen(state, address, msg.provider_id, HAND, prospect.id)
            counts["hand"] += 1
    await _prune(state)
    return counts
