"""An email you write yourself, sent by Harvey on the conversation's own thread.

From the Conversations tab: you type the email, and it goes out from the mailbox
the conversation is with, in the same Gmail thread (its threadId, and In-Reply-To
the latest message in it), on Harvey's next cycle. It's an ordinary outbox reply,
approved by you, so the Sender's checks still run: the pre-send gate, opt-outs,
bounces and the do-not-contact list. Replies don't count against a mailbox's daily
limit; they do wait through quiet hours, like every reply.

A reply Harvey drafted and that is waiting for you is replaced by yours, which the
Learner then reads as one of your edits. Nothing here makes a model call.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone

import aiosqlite

from harvey.config import live_do_not_contact
from harvey.gate import do_not_contact_match, pre_send_check
from harvey.models.conversation import Conversation

logger = logging.getLogger("harvey.owner_reply")

# The outbox angle of an email you wrote: the Sender files it in the thread as
# yours, not Harvey's. approved_by stays "you", which the Sender's own checks key on.
OWNER_ANGLE = "owner_wrote"
OWNER_SENDER = "you"
MAX_BODY_CHARS = 10000
AWAITING_PREFIX = "awaiting-"


class ReplyRefused(Exception):
    """Why your email can't be queued, said plainly."""


@dataclass
class ThreadHeaders:
    subject: str
    thread_ref: str
    in_reply_to: str
    mailbox: str


def _now() -> str:
    return datetime.now(timezone.utc).replace(tzinfo=None).isoformat()


def _re(subject: str) -> str:
    subject = (subject or "").strip()
    if not subject:
        return "Re: our conversation"
    return subject if subject.lower().startswith("re:") else f"Re: {subject}"


def _same_box(a: str, b: str) -> bool:
    return not a or not b or a.strip().lower() == b.strip().lower()


async def _outbox_rows(state, prospect_id: str) -> list[dict]:
    async with aiosqlite.connect(state.db_path, timeout=30) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
                "SELECT kind, status, subject, mailbox, thread_ref, in_reply_to, message_id, "
                "created_at, sent_at FROM outbox WHERE prospect_id = ? AND kind IN ('sequence', 'reply')",
                (prospect_id,)) as cursor:
            return [dict(r) for r in await cursor.fetchall()]


async def thread_headers(state, prospect_id: str, convo: Conversation | None = None,
                         fallback_mailbox: str = "") -> ThreadHeaders | None:
    """Where an email to this prospect belongs: the latest message in their
    thread with a known Message-ID, and that thread's Gmail id. None when Harvey
    never emailed them and they never wrote: there is no thread to answer."""
    mailbox = (convo.mailbox if convo else "") or fallback_mailbox or ""
    seen = []   # (when, message_id, thread_ref, subject, mailbox)
    for m in (convo.thread if convo else []):
        if m.sender == "prospect" and getattr(m, "message_id", ""):
            seen.append((m.timestamp.isoformat(), m.message_id, getattr(m, "thread_ref", ""),
                         getattr(m, "subject", ""), mailbox))
    for row in await _outbox_rows(state, prospect_id):
        if row["kind"] == "reply" and row["in_reply_to"]:
            # The message Harvey's reply answered: theirs, in the thread they wrote in.
            seen.append((row["created_at"] or "", row["in_reply_to"], row["thread_ref"] or "",
                         row["subject"] or "", row["mailbox"] or ""))
        if row["status"] == "sent" and row["message_id"]:
            seen.append((row["sent_at"] or row["created_at"] or "", row["message_id"],
                         row["thread_ref"] or "", row["subject"] or "", row["mailbox"] or ""))
    seen = [s for s in seen if _same_box(s[4], mailbox)]
    if not seen:
        return None
    seen.sort(key=lambda s: str(s[0]))
    when, message_id, thread_ref, subject, box = seen[-1]
    if not thread_ref:
        thread_ref = next((s[2] for s in reversed(seen) if s[2]), "")
    if not subject:
        subject = next((s[3] for s in reversed(seen) if s[3]), "")
    return ThreadHeaders(subject=_re(subject), thread_ref=thread_ref, in_reply_to=message_id,
                         mailbox=mailbox or box or "")


async def _waiting_draft(state, convo_id: str) -> dict | None:
    if not convo_id:
        return None
    async with aiosqlite.connect(state.db_path, timeout=30) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
                "SELECT * FROM outbox WHERE kind = 'reply' AND status = 'pending_review' "
                "AND conversation_id = ? ORDER BY created_at DESC LIMIT 1", (convo_id,)) as cursor:
            row = await cursor.fetchone()
    return dict(row) if row else None


async def _latest_campaign(state, prospect_id: str) -> str:
    async with aiosqlite.connect(state.db_path, timeout=30) as db:
        async with db.execute(
                "SELECT campaign_id FROM outbox WHERE prospect_id = ? AND kind = 'sequence' "
                "AND status = 'sent' ORDER BY sent_at DESC LIMIT 1", (prospect_id,)) as cursor:
            row = await cursor.fetchone()
    return (row[0] if row else "") or ""


async def queue_your_reply(state, config, *, body: str, convo_id: str = "", prospect_id: str = "",
                           take_over: bool = False) -> dict:
    """Queue the email you wrote on the conversation's thread. ``convo_id`` is a
    conversation, or ``awaiting-<prospect id>`` for someone who hasn't replied yet
    (a conversation is started for them, so their reply lands in it). Returns
    {outbox_id, conversation_id, mailbox, replaced_draft}; raises ReplyRefused."""
    text = (body or "").strip()
    if not text:
        raise ReplyRefused("Write the email first.")
    if len(text) > MAX_BODY_CHARS:
        raise ReplyRefused(f"That's longer than {MAX_BODY_CHARS} characters.")
    convo = None
    if convo_id and not convo_id.startswith(AWAITING_PREFIX):
        convo = await state.get_conversation(convo_id)
        if convo is None:
            raise ReplyRefused("That conversation no longer exists.")
        prospect_id = convo.prospect_id
    elif convo_id:
        prospect_id = convo_id[len(AWAITING_PREFIX):]
    prospect = await state.get_prospect(prospect_id) if prospect_id else None
    if prospect is None:
        raise ReplyRefused("Harvey has no record of this person.")
    if prospect.status == "opted_out":
        raise ReplyRefused("They opted out: Harvey won't write to them, and neither should this.")
    if (prospect.email_status or "") == "invalid":
        raise ReplyRefused("Their address bounced, so the email can't be delivered.")
    blocked = do_not_contact_match(prospect.email, live_do_not_contact(config))
    if blocked:
        raise ReplyRefused(f"{prospect.email} is on your do-not-contact list.")
    headers = await thread_headers(state, prospect.id, convo,
                                   fallback_mailbox=getattr(prospect, "mailbox", "") or "")
    if headers is None:
        raise ReplyRefused("Harvey hasn't emailed them yet, so there's no thread to reply on.")
    gate = pre_send_check(prospect.email, headers.subject, text, kind="reply",
                          do_not_contact=live_do_not_contact(config))
    if not gate:
        raise ReplyRefused("The sender would block this: " + "; ".join(gate.reasons))

    if convo is None:
        # Their next reply finds this conversation (the Handler reuses an open one).
        new_id = await state.add_conversation(Conversation(
            id="", prospect_id=prospect.id, campaign_id=await _latest_campaign(state, prospect.id),
            channel="email", thread=[], status="needs_human" if take_over else "open",
            mailbox=headers.mailbox))
        convo = await state.get_conversation(new_id)

    replaced = await _waiting_draft(state, convo.id)
    now = _now()
    if replaced is not None:
        # Your version of Harvey's draft: an edit (the Learner reads those), then approved.
        await state.edit_outbox_draft(replaced["id"], replaced["subject"], text)
        await state.log_action("outbox_edited", "dashboard", {
            "outbox_id": replaced["id"], "to_email": replaced.get("to_email", ""),
            "old_subject": replaced.get("subject", ""), "old_body": replaced.get("body", ""),
        })
        await state.update_outbox_item(replaced["id"], angle=OWNER_ANGLE, send_at=now)
        await state.approve_outbox(replaced["id"])     # approved_by "you", any hold cleared
        outbox_id = replaced["id"]
    else:
        outbox_id = await state.add_outbox_item(
            prospect_id=prospect.id, conversation_id=convo.id, kind="reply",
            to_email=prospect.email, subject=headers.subject, body=text, send_at=now,
            status="approved", provider=getattr(config.channels.email, "provider", "") or "",
            thread_ref=headers.thread_ref, in_reply_to=headers.in_reply_to,
            approved_by="you", angle=OWNER_ANGLE, mailbox=headers.mailbox or None)
    if take_over and convo.status != "needs_human":
        await state.update_conversation(convo.id, status="needs_human")
    await state.log_action("owner_reply", "dashboard", {
        "conversation_id": convo.id, "outbox_id": outbox_id, "to_email": prospect.email,
        "replaced_draft": replaced is not None, "take_over": bool(take_over)})
    logger.info(f"Your reply to {prospect.email} is queued on its thread.")
    return {"outbox_id": outbox_id, "conversation_id": convo.id, "mailbox": headers.mailbox,
            "replaced_draft": replaced is not None}
