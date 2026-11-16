"""Instructions from your own phone, on WhatsApp.

Owner, 2026-11-16: "if you have any doubts let me know, I will solve on
WhatsApp only, and I might give you some instruction from WhatsApp only, so
you can implement that also directly". Chose: safe actions, and ask when unsure.

Each WhatsApp run opens your chat with the linked number (the one meeting
alerts and updates go to, channels.whatsapp.alert_number) when it shows
unread, and records every new message you wrote there (``record``). Harvey's
brain reads each one beside a snapshot of how things stand and the last
messages it sent you, and decides (``handle``):

- a question: answered from the snapshot;
- a safe action: pause or resume email, LinkedIn or WhatsApp; guide one
  conversation (drafted, then held for you on the dashboard); add an address
  or a domain to do-not-contact; change the meeting window, its time zone,
  the meeting link or the chat link;
- unclear: Harvey asks you, on WhatsApp;
- anything that needs a change to Harvey's code, and your answers to a
  question asked from a coding session (``ask``): kept as notes for the next
  build (``harvey owner notes``).

Every message gets a short reply. Messages already in the chat when this
first runs are kept as seen, never acted on.
"""

from __future__ import annotations

import json
import logging
import re
import uuid
from datetime import datetime, timezone

import aiosqlite

from harvey.untrusted import fence

logger = logging.getLogger("harvey.owner_inbox")

STARTED_KEY = "owner:inbox_started"
OVERRIDE_KEY = "owner:override:"
KILL_SWITCH_KEY = "sending_paused"          # the Sender's and Handler's
CHANNEL_KEYS = {"linkedin": "linkedin:enabled", "whatsapp": "whatsapp:enabled"}
# Settings a message may change, and what each must look like.
SETTABLE = {
    "meeting_window": re.compile(r"^\d{1,2}:\d{2}-\d{1,2}:\d{2}$"),
    "meeting_timezone": re.compile(r"^[A-Za-z_]+/[A-Za-z_/+-]+$"),
    "meeting_link": re.compile(r"^https://\S+$"),
    "chat_link": re.compile(r"^https://\S+$"),
}
ACTIONS = ("none", "pause", "resume", "guide_conversation", "do_not_contact", "set")
NEW, DONE, FOR_DEVELOPER, ASKED, SEEN, FAILED, QUESTION_OUT = (
    "new", "done", "for_developer", "asked", "seen", "failed", "question_out")


def _now() -> str:
    return datetime.now(timezone.utc).replace(tzinfo=None).isoformat(timespec="seconds")


async def _ensure(state) -> None:
    async with aiosqlite.connect(state.db_path, timeout=30) as db:
        await db.execute(
            "CREATE TABLE IF NOT EXISTS owner_messages (id TEXT PRIMARY KEY, wa_id TEXT DEFAULT '', "
            "body TEXT DEFAULT '', direction TEXT DEFAULT 'in', status TEXT DEFAULT 'new', "
            "kind TEXT DEFAULT '', action TEXT DEFAULT '', reply TEXT DEFAULT '', "
            "error TEXT DEFAULT '', created_at TEXT DEFAULT '', updated_at TEXT DEFAULT '')")
        await db.commit()


async def _rows(state, sql: str, args=()) -> list[dict]:
    await _ensure(state)
    async with aiosqlite.connect(state.db_path, timeout=30) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(sql, args) as cursor:
            return [dict(r) for r in await cursor.fetchall()]


async def _insert(state, body: str, *, wa_id: str = "", direction: str = "in",
                  status: str = NEW, kind: str = "") -> str:
    await _ensure(state)
    row_id = uuid.uuid4().hex[:12]
    async with aiosqlite.connect(state.db_path, timeout=30) as db:
        await db.execute(
            "INSERT INTO owner_messages (id, wa_id, body, direction, status, kind, created_at, "
            "updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (row_id, wa_id, body, direction, status, kind, _now(), _now()))
        await db.commit()
    return row_id


async def _update(state, row_id: str, **fields) -> None:
    fields["updated_at"] = _now()
    cols = ", ".join(f"{k} = ?" for k in fields)
    async with aiosqlite.connect(state.db_path, timeout=30) as db:
        await db.execute(f"UPDATE owner_messages SET {cols} WHERE id = ?", (*fields.values(), row_id))
        await db.commit()


async def record(state, messages) -> int:
    """Your messages from the open chat (objects with direction, body, text,
    wa_id): the ones not seen before are stored. The first time, everything
    already there is kept as seen. Returns how many are new to act on."""
    await _ensure(state)
    first = not await state.get_setting(STARTED_KEY)
    messages = list(messages)
    # The first read: what you wrote after Harvey's latest message to you is
    # new (a reply to it); everything before that is history, kept as seen.
    last_ours = max((i for i, m in enumerate(messages) if getattr(m, "direction", "") == "out"
                     and (m.body or "").strip().startswith("Harvey")), default=-1)
    known = await _rows(state, "SELECT wa_id, body FROM owner_messages WHERE direction = 'in' "
                               "ORDER BY created_at DESC LIMIT 200")
    ids = {r["wa_id"] for r in known if r["wa_id"]}
    bodies = {r["body"] for r in known}
    new = 0
    for i, m in enumerate(messages):
        if getattr(m, "direction", "") != "in" or not getattr(m, "text", True):
            continue
        body = (m.body or "").strip()
        if not body or (m.wa_id and m.wa_id in ids) or (not m.wa_id and body in bodies):
            continue
        old = first and (last_ours < 0 or i < last_ours)
        await _insert(state, body, wa_id=m.wa_id or "", status=SEEN if old else NEW)
        ids.add(m.wa_id)
        bodies.add(body)
        new += 0 if old else 1
    if first:
        await state.set_setting(STARTED_KEY, _now())
    return new


async def snapshot(state, config) -> str:
    """How things stand, in a few lines: what a question is answered from."""
    from harvey.agents.linkedin_agent import channel_on as li_on
    from harvey.agents.whatsapp_agent import channel_on as wa_on

    async def one(sql, args=()):
        rows = await _rows(state, sql, args)
        return list(rows[0].values())[0] if rows else 0
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    lines = [
        f"Emails sent today (UTC): {await one('SELECT COUNT(*) FROM outbox WHERE status = ? AND sent_at >= ?', ('sent', today))}",
        f"Emails waiting for approval: {await one('SELECT COUNT(*) FROM outbox WHERE status = ?', ('pending_review',))}",
        f"Emails approved, due to go: {await one('SELECT COUNT(*) FROM outbox WHERE status = ?', ('approved',))}",
        f"Conversations on Needs you: {await one('SELECT COUNT(*) FROM conversations WHERE status = ?', ('needs_human',))}",
        f"Conversations you're handling: {await one('SELECT COUNT(*) FROM conversations WHERE status = ?', ('with_you',))}",
        f"Email sending paused: {(await state.get_setting(KILL_SWITCH_KEY)) or 'no'}",
    ]
    try:
        lines.append(f"LinkedIn on: {await li_on(state, config)}; WhatsApp on: {await wa_on(state, config)}")
    except Exception:
        pass
    offer = getattr(getattr(config, "product", None), "offer", None)
    lines.append(f"Meeting window: {getattr(offer, 'meeting_window', '')} "
                 f"{getattr(offer, 'meeting_timezone', '')}; link {getattr(offer, 'meeting_link', '')}")
    for row in await _rows(state, "SELECT action_type, details_json, created_at FROM actions "
                                  "WHERE action_type IN ('linkedin_run', 'whatsapp_run') "
                                  "ORDER BY created_at DESC LIMIT 2"):
        lines.append(f"Last {row['action_type'].replace('_', ' ')} at {row['created_at']} UTC: "
                     f"{row['details_json'][:200]}")
    return "\n".join(lines)


def _prompt(message: str, snap: str, recent: str, company: str) -> str:
    return f"""You are Harvey, the sales assistant of {company or "a small agency"}. The OWNER of the
business just sent you this message on WhatsApp. It is a real instruction or question from them.

{fence(message[:2000], "the owner's message")}

How things stand now:
{fence(snap, "status")}

The last messages you sent the owner (alerts, updates, questions you asked):
{fence(recent or "(none)", "your recent messages")}

Decide what it is and return ONLY JSON:
{{"kind": "question|action|unclear|for_developer|answer", "action": "none|pause|resume|guide_conversation|do_not_contact|set",
 "channel": "email|linkedin|whatsapp|all", "who": "", "guidance": "", "entry": "", "setting": "", "value": "",
 "reply": ""}}
- question: they asked how things stand. Answer in "reply" from the status only; say so if it isn't there.
- action, one of:
  pause / resume: stop or restart "channel" (email, linkedin, whatsapp or all).
  guide_conversation: they told you what to say or do in one client's conversation. "who" is the
    client's email address or name as they wrote it, "guidance" their instruction in their words.
  do_not_contact: never contact an address or domain again; "entry" is it.
  set: change "setting" (meeting_window as "10:00-12:00", meeting_timezone like "Asia/Kolkata",
    meeting_link or chat_link as a full https:// link) to "value".
- unclear: you aren't sure what they want, or it's risky to guess. "reply" asks them one short question.
- for_developer: anything else, which needs a change to how Harvey works (new features, new rules
  you can't apply with the actions above). "reply" says it's noted for the next update.
- answer: it answers a question you asked them earlier. "reply" thanks them briefly.
"reply" is always one or two short plain sentences, no dashes, no emojis, no markdown."""


async def _find_conversation(state, who: str):
    who = (who or "").strip().lower()
    if not who:
        return None
    prospect = await state.get_prospect_by_email(who) if "@" in who else None
    if prospect is None:
        words = [w for w in re.findall(r"[a-z]+", who) if len(w) > 1][:3]
        if not words:
            return None
        like = " AND ".join("lower(first_name || ' ' || last_name || ' ' || company) LIKE ?" for _ in words)
        rows = await _rows(state, f"SELECT id FROM prospects WHERE {like} ORDER BY updated_at DESC LIMIT 2",
                           tuple(f"%{w}%" for w in words))
        if len(rows) != 1:
            return None
        prospect = await state.get_prospect(rows[0]["id"])
    rows = await _rows(state, "SELECT id FROM conversations WHERE prospect_id = ? ORDER BY updated_at DESC "
                              "LIMIT 1", (prospect.id,))
    return await state.get_conversation(rows[0]["id"]) if rows else None


async def apply_overrides(state, config) -> None:
    """Settings you changed from WhatsApp, onto the running config (startup,
    and right after a change)."""
    offer = getattr(getattr(config, "product", None), "offer", None)
    if offer is None:
        return
    for field in SETTABLE:
        value = await state.get_setting(OVERRIDE_KEY + field)
        if value:
            setattr(offer, field, value)


async def _act(state, brain, config, d: dict) -> str:
    """Carry out one decided action. Returns what was done, in a few words."""
    action = d.get("action") or "none"
    channel = (d.get("channel") or "").lower()
    if action in ("pause", "resume"):
        targets = ["email", "linkedin", "whatsapp"] if channel == "all" else [channel]
        done = []
        for ch in targets:
            if ch == "email":
                if action == "pause":
                    await state.set_setting(KILL_SWITCH_KEY, "paused from your WhatsApp")
                else:
                    await state.delete_setting(KILL_SWITCH_KEY)
            elif ch in CHANNEL_KEYS:
                await state.set_setting(CHANNEL_KEYS[ch], "off" if action == "pause" else "on")
            else:
                raise ValueError(f"no channel called {ch!r}")
            done.append(ch)
        return f"{action}d {', '.join(done)}"
    if action == "do_not_contact":
        from harvey.config import add_extra_do_not_contact
        entry = (d.get("entry") or "").strip().lower()
        add_extra_do_not_contact(entry)
        return f"added {entry} to do-not-contact"
    if action == "set":
        field, value = (d.get("setting") or "").strip(), (d.get("value") or "").strip()
        if field not in SETTABLE or not SETTABLE[field].match(value):
            raise ValueError(f"can't set {field!r} to {value!r}")
        await state.set_setting(OVERRIDE_KEY + field, value)
        await apply_overrides(state, config)
        return f"set {field} to {value}"
    if action == "guide_conversation":
        from harvey import guidance
        from harvey.agents.handler import Handler
        from harvey.config import load_env
        convo = await _find_conversation(state, d.get("who") or "")
        if convo is None:
            raise ValueError(f"couldn't find one conversation for {d.get('who')!r}")
        started = await guidance.start(state, convo.id, d.get("guidance") or "")
        handler = Handler(brain=brain, state=state, config=config, env=load_env())
        row = await guidance.draft_from(state, handler, started["id"])
        if row.get("status") != "drafted":
            raise ValueError(row.get("error") or "the draft didn't work")
        return f"drafted a reply to {started['prospect'].email}, held for you"
    return ""


async def handle(state, brain, config) -> int:
    """Act on every new message from you, and queue a reply to each.
    Returns how many were handled. Never raises."""
    from harvey.meeting_alert import queue_to_owner
    from harvey.referrals import _ask_json
    handled = 0
    try:
        pending = await _rows(state, "SELECT * FROM owner_messages WHERE direction = 'in' AND status = ? "
                                     "ORDER BY created_at", (NEW,))
    except Exception as e:
        logger.warning(f"Owner inbox couldn't be read: {e}")
        return 0
    if not pending:
        return 0
    company = getattr(getattr(config, "persona", None), "company", "") or ""
    for row in pending:
        try:
            recent = await _rows(state, "SELECT m.body FROM whatsapp_messages m JOIN whatsapp_contacts c "
                                        "ON c.id = m.contact_id WHERE c.stage = 'owner' AND m.direction = 'out' "
                                        "ORDER BY m.created_at DESC LIMIT 4")
            d = await _ask_json(brain, _prompt(row["body"], await snapshot(state, config),
                                               "\n---\n".join(r["body"][:400] for r in recent), company),
                                "owner_message", agent="handler")
            if not isinstance(d, dict):
                raise ValueError("the model gave no usable answer")
            kind = str(d.get("kind") or "unclear")
            reply = " ".join(str(d.get("reply") or "").split())[:600]
            status = {"for_developer": FOR_DEVELOPER, "answer": FOR_DEVELOPER,
                      "unclear": ASKED}.get(kind, DONE)
            done = ""
            if kind == "action":
                try:
                    done = await _act(state, brain, config, d)
                except Exception as e:
                    status, reply = FAILED, f"I couldn't do that: {e}. Can you say it another way?"
            text = reply or (f"Done: {done}." if done else "Noted.")
            await _update(state, row["id"], status=status, kind=kind, action=done or d.get("action", ""),
                          reply=text)
            await queue_to_owner(state, config, f"Harvey: {text}")
            await state.log_action("owner_message", "owner", {"kind": kind, "did": done,
                                                              "message": row["body"][:200]})
            handled += 1
        except Exception as e:
            logger.warning(f"Owner message couldn't be handled: {e}")
            await _update(state, row["id"], status=FAILED, error=str(e)[:300])
    return handled


async def ask(state, config, question: str) -> str | None:
    """A question to you on WhatsApp from a coding session; your answer is
    kept as a note (``notes``)."""
    from harvey.meeting_alert import queue_to_owner
    text = " ".join((question or "").split())
    if not text:
        return None
    message_id = await queue_to_owner(state, config, f"Harvey (a question from your developer): {text}")
    if message_id:
        await _insert(state, text, direction="out", status=QUESTION_OUT, kind="question")
    return message_id


async def notes(state, include_done: bool = False) -> list[dict]:
    """Your messages kept for the next build, and the questions asked you."""
    statuses = (FOR_DEVELOPER, QUESTION_OUT, ASKED, FAILED) + ((DONE,) if include_done else ())
    marks = ",".join("?" for _ in statuses)
    return await _rows(state, f"SELECT * FROM owner_messages WHERE status IN ({marks}) "
                              "ORDER BY created_at", statuses)


def add_cli_parser(subparsers):
    sub = subparsers.add_parser("owner", help="Your WhatsApp instructions to Harvey, and questions to you")
    sub.add_argument("owner_action", choices=["notes", "ask", "inbox"],
                     help="notes: what you asked for that needs a code change, and answers; ask TEXT: "
                          "a question to your phone; inbox: your recent messages and what Harvey did")
    sub.add_argument("text", nargs="?", default="")
    sub.set_defaults(func=cmd_owner)
    return sub


def cmd_owner(args):
    import asyncio

    from harvey.config import load_config
    from harvey.state import StateManager

    async def _run():
        state = StateManager()
        await state.init_db()
        if args.owner_action == "ask":
            ok = await ask(state, load_config(), args.text)
            print("\n  Queued to your phone." if ok else "\n  Not sent: set channels.whatsapp.alert_number.")
            return
        rows = (await notes(state) if args.owner_action == "notes" else
                await _rows(state, "SELECT * FROM owner_messages ORDER BY created_at DESC LIMIT 20"))
        if not rows:
            print("\n  Nothing yet.")
        for r in rows:
            print(f"\n  [{r['created_at']}] {r['direction']} {r['status']}"
                  + (f" ({r['action']})" if r.get("action") else "") + f"\n    {r['body'][:300]}"
                  + (f"\n    -> {r['reply'][:200]}" if r.get("reply") else ""))
    asyncio.run(_run())
