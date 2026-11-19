"""LinkedIn's day: connects sent against the target, and who accepted.

Owner, 2026-11-19: "send daily 30 connects ... and track their acceptance
without missing any day make sure to do this daily". Once LinkedIn's hours end
(``REPORT_HOUR``, in channels.linkedin.hours' zone) the heartbeat logs one
``linkedin_daily`` action a day, which your WhatsApp update turns into a line:
connects today against max_daily_connections, accepted today, and the last 7
days. From ``UNDER_HOUR`` on, a day under target is a Needs you item saying why
(harvey/needs_you.py), so a missed day is never silent.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

logger = logging.getLogger("harvey.linkedin.daily")

REPORTED_KEY = "linkedin:daily_reported"
UNDER_HOUR = 21
REPORT_HOUR = 22


def _local(cfg, now=None):
    from harvey.linkedin.pacing import Pacing
    pacing = Pacing(None, cfg, now=(lambda: now) if now else None)
    return pacing.local_now()


async def numbers(state, config, now=None) -> dict:
    """{"date", "sent", "target", "accepted", "week_sent", "week_accepted"} for
    the local day (channels.linkedin.hours.timezone)."""
    from harvey.linkedin.pacing import Pacing
    cfg = config.channels.linkedin
    pacing = Pacing(state, cfg, now=(lambda: now) if now else None)
    local = pacing.local_now()
    today = local.date()
    week_from = (today - timedelta(days=6)).isoformat()
    used = await pacing.used()
    rows = await state._li_query(
        "SELECT SUM(CASE WHEN date(accepted_at) = ? THEN 1 ELSE 0 END) AS today, "
        "SUM(CASE WHEN date(accepted_at) >= ? THEN 1 ELSE 0 END) AS week "
        "FROM linkedin_leads WHERE accepted_at IS NOT NULL AND accepted_at != ''",
        (today.isoformat(), week_from))
    sent_week = await state._li_query(
        "SELECT COUNT(*) AS n FROM linkedin_messages WHERE kind = 'invite_note' AND status = 'sent' "
        "AND date(sent_at) >= ?", (week_from,))
    row = dict(rows[0]) if rows else {}
    return {"date": today.isoformat(), "sent": used["invites_today"],
            "target": cfg.max_daily_connections, "accepted": int(row.get("today") or 0),
            "week_sent": int(dict(sent_week[0]).get("n") or 0) if sent_week else 0,
            "week_accepted": int(row.get("week") or 0)}


async def why_short(state, config, now=None) -> str:
    """Why today's connects are under target, in plain words."""
    from harvey.agents.linkedin_agent import channel_on
    from harvey.linkedin.guards import active_pause
    from harvey.linkedin.pacing import Pacing
    cfg = config.channels.linkedin
    if not await channel_on(state, config):
        return "LinkedIn is switched off"
    at = (now or datetime.now(timezone.utc)).astimezone(timezone.utc).replace(tzinfo=None)
    pause = await active_pause(state, at)
    if pause:
        return "LinkedIn is paused: " + str(pause.get("reason") or "paused").rstrip(".")
    until = await Pacing(state, cfg, now=(lambda: now) if now else None).invites_paused_until()
    if until:
        return f"LinkedIn's weekly invite limit was reached; invites start again {until:%d %b %H:%M} UTC"
    waiting = await state._li_query(
        "SELECT COUNT(*) AS n FROM linkedin_leads l WHERE l.stage = 'found' "
        "AND COALESCE(l.no_message_reason, '') = '' AND NOT EXISTS (SELECT 1 FROM linkedin_messages m "
        "WHERE m.lead_id = l.id AND m.kind = 'invite_note')", ())
    if not (dict(waiting[0]).get("n") if waiting else 0):
        return "no new people to invite were found (web search needs the TinyFish key)"
    return "Harvey ran out of LinkedIn's hours before sending them all"


async def under_target_item(state, config, now=None) -> dict | None:
    """The Needs you item for a day under target, from UNDER_HOUR on."""
    from harvey.agents.linkedin_agent import channel_on
    cfg = config.channels.linkedin
    if _local(cfg, now).hour < UNDER_HOUR or not await channel_on(state, config):
        return None
    n = await numbers(state, config, now)
    if n["sent"] >= n["target"]:
        return None
    why = await why_short(state, config, now)
    return {"key": f"li-under-{n['date']}", "tone": "bad",
            "title": f"LinkedIn connects today: {n['sent']} of {n['target']}",
            "detail": f"Why: {why}. {n['accepted']} accepted today.",
            "action": "Open LinkedIn", "tab": "linkedin"}


async def report_day(state, config, now=None) -> bool:
    """Log today's numbers once, after LinkedIn's hours. True when logged."""
    from harvey.agents.linkedin_agent import channel_on
    cfg = config.channels.linkedin
    if _local(cfg, now).hour < REPORT_HOUR or not await channel_on(state, config):
        return False
    n = await numbers(state, config, now)
    if await state.get_setting(REPORTED_KEY) == n["date"]:
        return False
    if n["sent"] < n["target"]:
        n["why"] = await why_short(state, config, now)
    await state.log_action("linkedin_daily", "linkedin", n)
    await state.set_setting(REPORTED_KEY, n["date"])
    return True
