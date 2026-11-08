"""The dashboard's Website leads tab: what Harvey read from the ERP's client
inbox, what it did with each person, and whether the connection is working.

Read-only everywhere: Harvey's own tables here, and GET requests to the ERP for
one person's chat when you open it (harvey/collectors/erp_chat.py refuses any
other method). Nothing is written to the ERP.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import aiosqlite

from harvey.collectors.erp_chat import ERPChatClient, ERPChatError, parse_time

LAST_RUN_KEY = "inbound:last_run"
LAST_ERROR_KEY = "inbound:last_error"
MAX_ROWS = 500
# Late by this many poll intervals and the dot turns red.
LATE_AFTER_POLLS = 3

# What each record status means, in words, for the tab.
STATUS_WORDS = {
    "drafted": "Email written",
    "sent": "Emailed",
    "skipped": "Skipped",
    "not_project": "Not a lead",
    "escalated": "Passed to you",
    "triage_failed": "Couldn't be read",
    "triaged": "Waiting for a draft",
    "invalid_address": "Address doesn't exist",
    "unverified": "Address unconfirmed",
    "no_draft": "No usable draft",
    "redraft": "Wrote again: redrafting",
}


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _json(raw) -> dict:
    try:
        value = json.loads(raw or "{}")
    except (TypeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


async def connection(state, config, env, now: datetime | None = None) -> dict:
    """Is the ERP being read? {configured, enabled, poll_minutes, last_run,
    last_error, ok, why}."""
    from harvey.config import inbound_of
    now = now or _utcnow()
    settings = inbound_of(config)
    configured = bool(getattr(env, "erp_supabase_url", "") and getattr(env, "erp_supabase_key", ""))
    last_run = _json(await state.get_setting(LAST_RUN_KEY))
    last_error = _json(await state.get_setting(LAST_ERROR_KEY))
    ran_at = parse_time(last_run.get("at"))
    failed_at = parse_time(last_error.get("at"))
    poll = max(1, int(getattr(settings, "poll_minutes", 5) or 5))
    late = ran_at is None or now - ran_at > timedelta(minutes=poll * LATE_AFTER_POLLS)
    failing = failed_at is not None and (ran_at is None or failed_at > ran_at)
    if not settings.enabled:
        why = "Reading the client inbox is switched off (channels.email.inbound.enabled)."
    elif not configured:
        why = "ERP_SUPABASE_URL and ERP_SUPABASE_KEY aren't set in .env."
    elif failing:
        why = "The last read failed: " + str(last_error.get("error") or "unknown error")
    elif late:
        why = ("Harvey hasn't read the client inbox yet." if ran_at is None else
               f"The last read was over {poll * LATE_AFTER_POLLS} minutes ago. Is Harvey running?")
    else:
        why = ""
    return {"configured": configured, "enabled": bool(settings.enabled), "poll_minutes": poll,
            "autopilot": bool(getattr(settings, "autopilot", False)),
            "last_run": last_run, "last_error": last_error, "ok": not why, "why": why}


async def leads(state, limit: int = MAX_ROWS) -> list[dict]:
    """Every person Harvey read, newest first, with what it did: the record,
    its Outbox email, and the conversation if they replied."""
    async with aiosqlite.connect(state.db_path, timeout=30) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
                "SELECT l.email, l.last_message_at, l.category, l.size, l.summary, l.missing, "
                "l.estimate_ready, l.service_type, l.status, l.why, l.processed_at, "
                "o.id AS outbox_id, o.status AS outbox_status, o.angle, o.subject, o.sent_at, "
                "o.error AS outbox_note, p.id AS prospect_id, p.first_name, p.last_name, p.phone, "
                "p.company, p.status AS prospect_status, "
                "(SELECT c.id FROM conversations c WHERE c.prospect_id = p.id "
                " ORDER BY c.updated_at DESC LIMIT 1) AS conversation_id, "
                "(SELECT c.status FROM conversations c WHERE c.prospect_id = p.id "
                " ORDER BY c.updated_at DESC LIMIT 1) AS conversation_status "
                "FROM inbound_leads l "
                "LEFT JOIN outbox o ON o.id = l.outbox_id "
                "LEFT JOIN prospects p ON lower(p.email) = lower(l.email) "
                "ORDER BY COALESCE(l.last_message_at, l.processed_at) DESC LIMIT ?",
                (int(limit),)) as cursor:
            rows = [dict(r) for r in await cursor.fetchall()]
        async with db.execute("SELECT lower(email), stage FROM whatsapp_contacts "
                              "WHERE COALESCE(email, '') != ''") as cursor:
            whatsapp = {e: s for e, s in await cursor.fetchall()}
    for row in rows:
        row["status_words"] = STATUS_WORDS.get(row["status"] or "", row["status"] or "")
        row["estimate_ready"] = str(row.get("estimate_ready") or "0") in ("1", "True", "true")
        row["whatsapp_stage"] = whatsapp.get((row["email"] or "").lower(), "")
    return rows


async def overview(state, config, env) -> dict:
    from harvey.inbound import status_counts
    return {"connection": await connection(state, config, env),
            "counts": await status_counts(state),
            "leads": await leads(state)}


async def chat(env, email: str) -> dict:
    """One person's chats, read from the ERP now (GET only)."""
    reader = ERPChatClient(getattr(env, "erp_supabase_url", ""), getattr(env, "erp_supabase_key", ""))
    if not reader.configured:
        raise ERPChatError("ERP_SUPABASE_URL and ERP_SUPABASE_KEY aren't set in .env")
    lead = await reader.lead_for(email)
    if lead is None:
        return {"found": False, "lines": []}
    return {"found": True, "name": lead.name, "phone": lead.phone, "country": lead.country,
            "city": lead.city, "region": lead.region, "trail": lead.trail, "opened_on": lead.opened_on,
            "utm": lead.utm, "directory": lead.directory, "referrer": lead.referrer,
            "entry_path": lead.entry_path, "rep_name": lead.rep_name,
            "last_message_at": lead.last_message_at,
            "lines": [{"who": line.who, "text": line.text, "at": line.at} for line in lead.transcript]}
