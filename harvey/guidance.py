"""Guiding Harvey on a conversation, and learning from it.

On the Conversations tab you tell Harvey what to do with a reply ("ask for their
current site, offer a call on Tuesday"). Harvey drafts the reply from that, using
the Handler's own prompt, playbook, lessons and humanizer, and holds it for you:
an escalated thread is your call, so a guided draft never goes out by itself.

Every piece of guidance also teaches: right after the draft, one model call asks
what general rule it shows, and proposes that as a lesson (kind ``replies``)
through the Learner's usual filter (no names, numbers, links, quotes or claims).
It waits for your Keep or Skip, on the card or the Learning tab; once kept, the
Handler uses it on every reply it writes.
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime, timezone

import aiosqlite

from harvey.brain import BrainUnavailable
from harvey.lessons import lesson_problem, propose
from harvey.owner_reply import ReplyRefused, thread_headers
from harvey.state import HELD_FOR_REVIEW
from harvey.untrusted import UNTRUSTED_RULE, fence

logger = logging.getLogger("harvey.guidance")

GUIDANCE_SCHEMA = """
CREATE TABLE IF NOT EXISTS guidance (
    id TEXT PRIMARY KEY,
    conversation_id TEXT NOT NULL DEFAULT '',
    prospect_id TEXT DEFAULT '',
    intent TEXT DEFAULT '',
    guidance TEXT NOT NULL DEFAULT '',
    status TEXT DEFAULT 'drafting',
    outbox_id TEXT DEFAULT '',
    lesson_id TEXT DEFAULT '',
    lesson_note TEXT DEFAULT '',
    error TEXT DEFAULT '',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_guidance_conversation ON guidance(conversation_id, created_at);
"""

DRAFTING, DRAFTED, FAILED = "drafting", "drafted", "failed"
LESSON_KIND = "replies"
MAX_GUIDANCE_CHARS = 2000
GUIDED_NOTE = "drafted from your guidance"
NO_RULE = "NONE"


def _now() -> str:
    return datetime.now(timezone.utc).replace(tzinfo=None).isoformat()


async def _ensure(state) -> None:
    async with aiosqlite.connect(state.db_path, timeout=30) as db:
        await db.executescript(GUIDANCE_SCHEMA)
        await db.commit()


async def _update(state, guidance_id: str, **fields) -> None:
    sets = ", ".join(f"{k} = ?" for k in fields)
    async with aiosqlite.connect(state.db_path, timeout=30) as db:
        await db.execute(f"UPDATE guidance SET {sets}, updated_at = ? WHERE id = ?",
                         (*fields.values(), _now(), guidance_id))
        await db.commit()


async def latest_for(state, conversation_ids) -> dict:
    """{conversation id: its latest guidance row}, with the lesson it proposed."""
    ids = [i for i in conversation_ids if i]
    if not ids:
        return {}
    await _ensure(state)
    marks = ",".join("?" * len(ids))
    async with aiosqlite.connect(state.db_path, timeout=30) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
                f"SELECT g.*, l.text AS lesson_text, l.status AS lesson_status FROM guidance g "
                f"LEFT JOIN lessons l ON l.id = g.lesson_id WHERE g.conversation_id IN ({marks}) "
                "ORDER BY g.created_at", ids) as cursor:
            rows = [dict(r) for r in await cursor.fetchall()]
    return {r["conversation_id"]: r for r in rows}


async def start(state, convo_id: str, guidance: str) -> dict:
    """Check the guidance can be used and record it: {id, conversation, prospect}.
    The draft itself is ``draft_from`` (a model call: run it in the background)."""
    text = (guidance or "").strip()
    if not text:
        raise ReplyRefused("Tell Harvey what to do first.")
    if len(text) > MAX_GUIDANCE_CHARS:
        raise ReplyRefused(f"Keep it under {MAX_GUIDANCE_CHARS} characters.")
    convo = await state.get_conversation(convo_id)
    if convo is None:
        raise ReplyRefused("That conversation no longer exists.")
    prospect = await state.get_prospect(convo.prospect_id)
    if prospect is None:
        raise ReplyRefused("Harvey has no record of this person.")
    if prospect.status == "opted_out":
        raise ReplyRefused("They opted out: Harvey won't write to them.")
    if (prospect.email_status or "") == "invalid":
        raise ReplyRefused("Their address bounced, so a reply can't be delivered.")
    if not any(m.sender == "prospect" for m in convo.thread):
        raise ReplyRefused("They haven't written anything yet for Harvey to answer.")
    await _ensure(state)
    async with aiosqlite.connect(state.db_path, timeout=30) as db:
        async with db.execute("SELECT 1 FROM guidance WHERE conversation_id = ? AND status = ?",
                              (convo_id, DRAFTING)) as cursor:
            if await cursor.fetchone():
                raise ReplyRefused("Harvey is still drafting from your last guidance.")
        guidance_id = uuid.uuid4().hex[:12]
        await db.execute(
            "INSERT INTO guidance (id, conversation_id, prospect_id, intent, guidance, status, "
            "created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (guidance_id, convo_id, prospect.id, convo.intent or "", text, DRAFTING, _now(), _now()))
        await db.commit()
    await state.log_action("guidance", "dashboard", {"conversation_id": convo_id,
                                                     "guidance_id": guidance_id})
    return {"id": guidance_id, "conversation": convo, "prospect": prospect}


async def _replace_waiting_draft(state, convo_id: str) -> None:
    async with aiosqlite.connect(state.db_path, timeout=30) as db:
        await db.execute(
            "UPDATE outbox SET status = 'cancelled', error = ?, updated_at = ? "
            "WHERE kind = 'reply' AND status = 'pending_review' AND conversation_id = ?",
            ("replaced by a draft from your guidance", _now(), convo_id))
        await db.commit()


async def draft_from(state, handler, guidance_id: str) -> dict:
    """Write the reply the guidance asks for and hold it in the Outbox, then
    propose the lesson it teaches. Never raises: the row records what happened."""
    async with aiosqlite.connect(state.db_path, timeout=30) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM guidance WHERE id = ?", (guidance_id,)) as cursor:
            row = await cursor.fetchone()
    if row is None:
        return {}
    row = dict(row)
    try:
        convo = await state.get_conversation(row["conversation_id"])
        prospect = await state.get_prospect(row["prospect_id"])
        if convo is None or prospect is None:
            raise ReplyRefused("the conversation or the person is gone")
        theirs = [m for m in convo.thread if m.sender == "prospect"]
        handler.skills = handler.brain.load_skills_for_agent("handler")
        body = await handler._generate_response(
            row["intent"] or convo.intent or "question", theirs[-1].content if theirs else "",
            prospect, convo, guidance=row["guidance"])
        if not body:
            raise ReplyRefused(getattr(handler.brain, "paused_reason", "") or
                               "the model gave no usable draft; try again")
        headers = await thread_headers(state, prospect.id, convo,
                                       fallback_mailbox=getattr(prospect, "mailbox", "") or "")
        if headers is None:
            raise ReplyRefused("there's no email thread to reply on")
        await _replace_waiting_draft(state, convo.id)
        outbox_id = await state.add_outbox_item(
            prospect_id=prospect.id, conversation_id=convo.id, kind="reply",
            to_email=prospect.email, subject=headers.subject, body=body,
            send_at=_now(), status="pending_review",
            provider=getattr(handler.config.channels.email, "provider", "") or "",
            thread_ref=headers.thread_ref, in_reply_to=headers.in_reply_to,
            mailbox=headers.mailbox or None)
        # Held: "Approve all" passes it by; you send it from the card or the Outbox.
        await state.update_outbox_item(outbox_id, error=HELD_FOR_REVIEW + GUIDED_NOTE)
        await _update(state, guidance_id, status=DRAFTED, outbox_id=outbox_id or "", error="")
        row.update(status=DRAFTED, outbox_id=outbox_id)
    except (ReplyRefused, BrainUnavailable) as e:
        await _update(state, guidance_id, status=FAILED, error=str(e)[:300])
        return {**row, "status": FAILED, "error": str(e)}
    except Exception as e:
        logger.warning(f"Guidance: the draft failed: {e}")
        await _update(state, guidance_id, status=FAILED, error=f"the draft failed: {e}"[:300])
        return {**row, "status": FAILED, "error": str(e)}
    company = getattr(getattr(handler.config, "persona", None), "company", "") or ""
    lesson_id, note = await propose_lesson(state, handler.brain, row, convo, company=company)
    await _update(state, guidance_id, lesson_id=lesson_id, lesson_note=note[:300])
    return {**row, "lesson_id": lesson_id, "lesson_note": note}


def _rule_prompt(guidance: str, intent: str, their_message: str, company: str) -> str:
    return f"""You help a sales assistant learn from its owner. The assistant answers emails from
people {company or 'the business'} reached out to. On one reply the owner told it what to do.

{UNTRUSTED_RULE}

{fence(their_message[:1500], "the reply the owner was guiding")}

The reply was classed as: {intent or 'unknown'}

The owner's instruction (trusted, from the owner):
--- owner's instruction ---
{guidance.strip()[:MAX_GUIDANCE_CHARS]}
--- end ---

What general rule does this instruction teach for similar replies in future? Write ONE rule
of at most two sentences, in the imperative ("When someone ..., ..."), that would help with
other people too. Leave out every name, company, number, price, date, link, email address
and quotation, and claim nothing about our company. If the instruction only fits this one
person (a one-off fact, a specific time, a personal favour), answer exactly {NO_RULE}.

Answer with the rule only, or {NO_RULE}."""


async def propose_lesson(state, brain, row: dict, convo=None, company: str = "") -> tuple[str, str]:
    """(lesson id or "", what happened): one model call, then the lesson filter."""
    try:
        their = ""
        if convo is not None:
            theirs = [m for m in convo.thread if m.sender == "prospect"]
            their = theirs[-1].content if theirs else ""
        rule = await brain.think(_rule_prompt(row["guidance"], row.get("intent", ""), their, company),
                                 session_id="harvey-learner", agent="learner",
                                 task="guidance_lesson")
        rule = (rule or "").strip().strip('"').strip()
        if not rule:
            return "", "no lesson: the model gave no answer"
        if rule.upper().startswith(NO_RULE):
            return "", "no lesson: this guidance fits only this conversation"
        problem = lesson_problem(rule)
        if problem:
            return "", f"no lesson: the rule it drew {problem}"
        lesson_id, _ = await propose(state, LESSON_KIND, rule, evidence={
            "source": "your guidance", "guidance_id": row["id"],
            "conversation_id": row["conversation_id"], "intent": row.get("intent", "")},
            source_run="guidance")
        return lesson_id, "proposed"
    except ValueError as e:
        return "", f"no lesson: {e}"
    except Exception as e:
        logger.warning(f"Guidance: couldn't propose a lesson: {e}")
        return "", f"no lesson: {e}"


def summary(row: dict | None) -> dict | None:
    """What the card shows about the latest guidance."""
    if not row:
        return None
    return {"id": row["id"], "status": row["status"], "guidance": row["guidance"],
            "error": row.get("error") or "", "outbox_id": row.get("outbox_id") or "",
            "lesson_id": row.get("lesson_id") or "", "lesson_text": row.get("lesson_text") or "",
            "lesson_status": row.get("lesson_status") or "", "lesson_note": row.get("lesson_note") or "",
            "created_at": row.get("created_at") or ""}
