"""Every meeting in every conversation, read from the conversations themselves.

Owner, 2026-11-23: "calculate all the meetings done or messaged in past and
all the coming up meetings, compile all the conversations across all the
platforms and figure this out accurately, and fill the calendar".

The Meetings tab knew a meeting only by a stage (``meeting``) or Harvey's own
booked offer. A time agreed any other way (you on LinkedIn, their suggestion,
"see you Tuesday" in an email) never reached it. Here each conversation on
LinkedIn, WhatsApp and email that shows any sign of a meeting is read once by
the brain, with every message's date, and every meeting in it comes back:
agreed, proposed (not confirmed), held, or cancelled, with its time, who
proposed it and the words that show it.

Nothing is taken on trust: a meeting whose quote isn't in the conversation is
dropped, and a time that doesn't parse leaves the meeting without a time
(never a guess). Results are kept per conversation (``FOUND_KEY``) with the
number of messages read, so a conversation is read again only when it changes.
The finder never changes a stage or sends anything.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone

from harvey.meeting_alert import MEETING_RE
from harvey.meeting_slot import OFFERED_KEY
from harvey.untrusted import UNTRUSTED_RULE, fence

logger = logging.getLogger("harvey.meeting_finder")

FOUND_KEY = "meeting:found:"          # + channel:id -> {"messages", "at", "meetings"}
SCAN_KEY = "meeting:scan"             # the last scan: {"started", "finished", "report", "running"}
STATUSES = ("agreed", "proposed", "held", "cancelled")
MAX_MESSAGES = 60                     # the newest, per conversation
LINK_RE = re.compile(r"meet\.google\.com|zoom\.us/|calendly\.com|teams\.microsoft\.com|cal\.com/", re.IGNORECASE)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _norm(text: str) -> str:
    return " ".join(re.sub(r"[^\w\s:]", " ", (text or "").lower().replace("’", "'")).split())


# ── The conversations ──

async def conversations(state, config) -> list[dict]:
    """Every conversation with at least one message from them or about a
    meeting: {"channel", "id", "name", "company", "thread": [{"who", "body", "at"}]}."""
    out: list[dict] = []
    by_lead: dict[str, list[dict]] = {}
    for m in await state.li_messages(statuses=("sent",), limit=200000):
        if m["kind"] == "invite_note":
            continue
        by_lead.setdefault(m["lead_id"], []).append(m)
    for lead_id, rows in by_lead.items():
        lead = await state.li_get_lead(lead_id)
        if not lead:
            continue
        out.append({"channel": "linkedin", "id": lead_id, "name": lead.get("name") or "",
                    "company": lead.get("company") or "",
                    "thread": [{"who": "them" if r["direction"] == "in" else "us", "body": r["body"] or "",
                                "at": r.get("sent_at") or r.get("created_at") or ""} for r in rows]})
    by_contact: dict[str, list[dict]] = {}
    for m in await state.wa_messages(statuses=("sent",), limit=200000):
        if m["kind"] == "alert":
            continue
        by_contact.setdefault(m["contact_id"], []).append(m)
    for contact_id, rows in by_contact.items():
        contact = await state.wa_get_contact(contact_id)
        if not contact or contact.get("stage") == "owner":
            continue
        out.append({"channel": "whatsapp", "id": contact_id, "name": contact.get("name") or contact.get("phone") or "",
                    "company": "",
                    "thread": [{"who": "them" if r["direction"] == "in" else "us", "body": r["body"] or "",
                                "at": r.get("sent_at") or r.get("created_at") or ""} for r in rows]})
    from harvey.models.conversation import Conversation
    by_prospect: dict[str, list[dict]] = {}
    for row in await state._li_query("SELECT prospect_id, thread_json FROM conversations ORDER BY created_at"):
        for msg in Conversation.thread_from_json(row.get("thread_json")):
            if msg.sender == "auto":
                continue
            by_prospect.setdefault(row["prospect_id"], []).append(
                {"who": "them" if msg.sender == "prospect" else "us", "body": msg.content or "",
                 "at": msg.timestamp.isoformat() if msg.timestamp else ""})
    for prospect_id, thread in by_prospect.items():
        prospect = await state.get_prospect(prospect_id) if prospect_id else None
        if prospect is None:
            continue
        name = " ".join(x for x in (prospect.first_name, prospect.last_name) if x) or prospect.email
        out.append({"channel": "email", "id": prospect_id, "name": name, "company": prospect.company or "",
                    "thread": sorted(thread, key=lambda m: m["at"] or "")})
    for conv in out:
        conv["thread"].sort(key=lambda m: m["at"] or "")
    return out


async def _offered(state, conv: dict) -> dict:
    try:
        data = json.loads(await state.get_setting(OFFERED_KEY + f"{conv['channel']}:{conv['id']}") or "{}")
    except ValueError:
        data = {}
    return data if isinstance(data, dict) else {}


def has_sign(conv: dict, offered: dict | None = None) -> bool:
    """Worth a read: a reply from them, and some word of a meeting, a meeting
    link, or a time Harvey offered."""
    thread = conv["thread"]
    if not any(m["who"] == "them" for m in thread):
        return False
    if offered:
        return True
    return any(MEETING_RE.search(m["body"]) or LINK_RE.search(m["body"]) for m in thread)


# ── Reading one conversation ──

def prompt_for(conv: dict, config, now: datetime, offered: dict, their_zone: str = "") -> str:
    from harvey.meetings import owner_zone
    persona = config.persona
    lines = []
    for m in conv["thread"][-MAX_MESSAGES:]:
        when = (m["at"] or "")[:16].replace("T", " ")
        lines.append(f"[{when} UTC] {'Us' if m['who'] == 'us' else 'Them'}: {m['body']}")
    channel = {"linkedin": "LinkedIn messages", "whatsapp": "WhatsApp", "email": "email"}[conv["channel"]]
    offer = ""
    if offered.get("start"):
        offer = (f"\nA time Harvey offered them: {offered.get('text') or ''} (starts {offered['start']} UTC).")
    return (
        f"You read a sales conversation on {channel} between {persona.name} of {persona.company} (Us) and "
        f"{conv['name'] or 'a prospect'}{' of ' + conv['company'] if conv['company'] else ''} (Them), and find "
        "every meeting or call arranged in it.\n"
        f"Today is {now:%A %d %B %Y, %H:%M} UTC. Our time zone is {owner_zone(config)}."
        + (f" Their time zone is {their_zone}." if their_zone else " Their time zone isn't known.")
        + offer
        + f"\n\n{UNTRUSTED_RULE}\n\nTHE CONVERSATION (oldest first, each message with when it was sent):\n"
        + fence("\n".join(lines), "the conversation")
        + "\n\nReturn JSON: {\"meetings\": [{\"status\": \"agreed|proposed|held|cancelled\", "
          "\"start\": \"YYYY-MM-DDTHH:MM+HH:MM or empty\", \"minutes\": 30, \"proposed_by\": \"us|them\", "
          "\"quote\": \"the exact words from the conversation that show it, copied, at most 160 characters\", "
          "\"note\": \"at most 100 characters\"}]}\n"
          "Rules:\n"
          "- agreed: a specific day and time both sides accepted (they said yes to our time, or we to theirs).\n"
          "- proposed: a day or time was suggested and not accepted or refused yet.\n"
          "- held: the conversation says the call took place.\n"
          "- cancelled: called off, refused, can't make it with no new time, or they said no after it was agreed.\n"
          "- An offer of \"a call\" with no answer, or general interest, is not a meeting: leave it out.\n"
          "- Work out relative days (\"tomorrow\", \"Monday\", \"next week\") from the date of the message they are in.\n"
          "- start carries the zone of the time as written; if none is written, use the zone of whoever proposed it "
          "when known (ours for us), else leave start empty. Never invent a time.\n"
          "- A meeting moved to a new time is two entries: the old one cancelled, the new one with its own status.\n"
          "- No meetings: {\"meetings\": []}.")


def _parse_start(value: str) -> datetime | None:
    value = (value or "").strip()
    if not value:
        return None
    try:
        moment = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return moment.astimezone(timezone.utc) if moment.tzinfo else None


def check(raw, conv: dict) -> list[dict]:
    """The model's meetings, kept only when they hold up: a known status, a
    quote that is really in the conversation, and a time that parses (else no
    time). Each: {"status", "start", "minutes", "proposed_by", "quote", "note"}."""
    items = raw.get("meetings") if isinstance(raw, dict) else raw
    if not isinstance(items, list):
        return []
    text = _norm(" ".join(m["body"] for m in conv["thread"]))
    out = []
    for item in items:
        if not isinstance(item, dict):
            continue
        status = str(item.get("status") or "").strip().lower()
        quote = " ".join(str(item.get("quote") or "").split())[:200]
        if status not in STATUSES or not quote:
            continue
        probe = _norm(quote)
        if not probe or (probe not in text and probe[:60] not in text):
            continue                      # words that aren't in the conversation: not evidence
        start = _parse_start(str(item.get("start") or ""))
        try:
            minutes = max(10, min(int(item.get("minutes") or 30), 240))
        except (TypeError, ValueError):
            minutes = 30
        out.append({"status": status, "start": start.isoformat() if start else "", "minutes": minutes,
                    "proposed_by": "them" if str(item.get("proposed_by") or "").lower() == "them" else "us",
                    "quote": quote, "note": " ".join(str(item.get("note") or "").split())[:120]})
    return out


async def read_one(state, brain, config, conv: dict, now: datetime | None = None) -> list[dict] | None:
    """One conversation's meetings, or None when the brain gave nothing."""
    from harvey import meetings
    now = now or _utcnow()
    offered = await _offered(state, conv)
    their_zone = ""
    try:
        card = await meetings.item(state, config, conv["channel"], conv["id"])
        their_zone = (card or {}).get("their_zone") or ""
    except Exception:
        their_zone = ""
    prompt = prompt_for(conv, config, now, offered, their_zone)
    if hasattr(brain, "think_json"):
        raw = await brain.think_json(prompt, session_id="harvey-meetings", agent="meetings", task="meeting_find")
    else:
        text = await brain.think(prompt, session_id="harvey-meetings", agent="meetings", task="meeting_find")
        raw = _json(text)
    if raw is None:
        return None
    return check(raw, conv)


def _json(text: str):
    text = (text or "").strip()
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        return json.loads(text[start:end + 1])
    except ValueError:
        return None


async def scan(state, brain, config, *, force: bool = False, limit: int | None = None,
               channels=("linkedin", "whatsapp", "email"), progress=None, now: datetime | None = None) -> dict:
    """Read every conversation with a sign of a meeting that changed since its
    last read (``force``: all of them). Returns a report of what was read and
    found. Stops when the brain is paused."""
    now = now or _utcnow()
    report = {"conversations": 0, "with_signs": 0, "read": 0, "unchanged": 0, "failed": 0,
              "found": {s: 0 for s in STATUSES}, "by_channel": {}}
    convs = [c for c in await conversations(state, config) if c["channel"] in channels]
    report["conversations"] = len(convs)
    for conv in convs:
        offered = await _offered(state, conv)
        if not has_sign(conv, offered):
            continue
        report["with_signs"] += 1
        key = FOUND_KEY + f"{conv['channel']}:{conv['id']}"
        try:
            cached = json.loads(await state.get_setting(key) or "{}")
        except ValueError:
            cached = {}
        if not force and cached.get("messages") == len(conv["thread"]):
            report["unchanged"] += 1
            _count(report, conv["channel"], cached.get("meetings") or [])
            continue
        if limit is not None and report["read"] >= limit:
            break
        if getattr(brain, "paused_reason", ""):
            report["stopped"] = brain.paused_reason
            break
        try:
            found = await read_one(state, brain, config, conv, now)
        except Exception as e:
            logger.warning(f"Meetings: couldn't read a {conv['channel']} conversation: {e}")
            found = None
        if found is None:
            report["failed"] += 1
            continue
        await state.set_setting(key, json.dumps({"messages": len(conv["thread"]), "at": now.isoformat(),
                                                 "meetings": found}))
        report["read"] += 1
        _count(report, conv["channel"], found)
        if progress:
            await progress(report)
    return report


def _count(report: dict, channel: str, found: list[dict]):
    per = report["by_channel"].setdefault(channel, {s: 0 for s in STATUSES})
    for m in found:
        if m.get("status") in STATUSES:
            report["found"][m["status"]] += 1
            per[m["status"]] += 1


async def stored(state) -> list[dict]:
    """Every meeting found so far: {"channel", "id", "index", ...meeting}."""
    rows = await state._li_query("SELECT key, value FROM settings WHERE key LIKE ?", (FOUND_KEY + "%",))
    out = []
    for row in rows:
        channel, _, item_id = row["key"][len(FOUND_KEY):].partition(":")
        try:
            data = json.loads(row["value"] or "{}")
        except ValueError:
            continue
        for i, meeting in enumerate(data.get("meetings") or []):
            out.append({"channel": channel, "id": item_id, "index": i, "read_at": data.get("at") or "", **meeting})
    return out


# ── `harvey meetings scan` (also started by the Meetings tab's button) ──

async def run_scan(state, brain, config, *, linkedin_inbox: bool = False, force: bool = False,
                   limit: int | None = None) -> dict:
    """The whole job, its progress kept in SCAN_KEY for the tab: first (when
    asked) LinkedIn's inbox read into Harvey's records, then every conversation."""
    status = {"started": _utcnow().isoformat(), "finished": "", "running": True, "step": "", "report": {}}

    async def save(**changes):
        status.update(changes)
        await state.set_setting(SCAN_KEY, json.dumps(status))

    await save(step="starting")
    try:
        if linkedin_inbox:
            from harvey.linkedin.history import backfill
            await save(step="reading LinkedIn's inbox")

            async def inbox_progress(done: dict):
                await save(step="reading LinkedIn's inbox", inbox=done)
            status["inbox"] = await backfill(state, config, progress=inbox_progress)
        await save(step="reading the conversations")

        async def progress(report: dict):
            await save(report=dict(report))
        report = await scan(state, brain, config, force=force, limit=limit, progress=progress)
        await save(step="done", report=report)
        return status
    except Exception as e:
        logger.exception("meeting scan failed")
        await save(step=f"stopped: {e}")
        return status
    finally:
        await save(running=False, finished=_utcnow().isoformat())


def add_cli_parser(subparsers):
    sub = subparsers.add_parser(
        "meetings", help="Find every meeting in every conversation (LinkedIn, WhatsApp, email)")
    sub.add_argument("meetings_action", choices=["scan", "list"],
                     help="scan: read the conversations for meetings; list: print what was found")
    sub.add_argument("--linkedin-inbox", action="store_true",
                     help="scan: first read LinkedIn's whole inbox into Harvey's records (in Harvey's window)")
    sub.add_argument("--force", action="store_true", help="scan: read every conversation again")
    sub.add_argument("--limit", type=int, default=None, help="scan: read at most this many conversations")
    sub.set_defaults(func=cmd_meetings)
    return sub


def cmd_meetings(args):
    import asyncio
    from harvey.brain import Brain
    from harvey.config import load_config
    from harvey.state import StateManager

    async def _run():
        config, state = load_config(), StateManager()
        await state.init_db()
        if args.meetings_action == "list":
            rows = await stored(state)
            print(f"\n  {len(rows)} meetings found in conversations:")
            for m in sorted(rows, key=lambda r: r.get("start") or ""):
                print(f"  {m['channel']:<9} {m['status']:<10} {m.get('start') or '(no time)':<26} {m.get('note') or ''}")
            return
        status = await run_scan(state, Brain(state, config.brain), config, linkedin_inbox=args.linkedin_inbox,
                                force=args.force, limit=args.limit)
        report = status.get("report") or {}
        print(f"\n  {status.get('step')}: {report.get('conversations', 0)} conversations, "
              f"{report.get('with_signs', 0)} with signs of a meeting, {report.get('read', 0)} read now, "
              f"{report.get('unchanged', 0)} unchanged.")
        print("  Found: " + ", ".join(f"{n} {s}" for s, n in (report.get("found") or {}).items()))
        if status.get("inbox"):
            print(f"  LinkedIn inbox: {status['inbox']}")
    asyncio.run(_run())


DAILY_KEY = "meeting:scan:daily"
DAILY_EVERY_HOURS = 20
_daily_task = None


async def maybe_daily_scan(state, brain, config) -> bool:
    """Start the day's scan of conversations that changed, in the background,
    at most once every DAILY_EVERY_HOURS and never beside a running scan.
    Reads Harvey's own records only: LinkedIn's inbox is read on request."""
    import asyncio
    global _daily_task
    if _daily_task is not None and not _daily_task.done():
        return False
    try:
        last = datetime.fromisoformat(await state.get_setting(DAILY_KEY) or "")
    except ValueError:
        last = None
    now = _utcnow()
    if last is not None and (now - (last if last.tzinfo else last.replace(tzinfo=timezone.utc))).total_seconds() < DAILY_EVERY_HOURS * 3600:
        return False
    try:
        status = json.loads(await state.get_setting(SCAN_KEY) or "{}")
    except ValueError:
        status = {}
    started = status.get("started") or ""
    if status.get("running") and started and (now - datetime.fromisoformat(started)).total_seconds() < 4 * 3600:
        return False
    await state.set_setting(DAILY_KEY, now.isoformat())
    _daily_task = asyncio.create_task(run_scan(state, brain, config))
    return True
