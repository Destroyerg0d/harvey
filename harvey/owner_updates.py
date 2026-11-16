"""Short updates to your own phone on WhatsApp, as things happen.

Owner, 2026-11-16: "give me all the updates in my WhatsApp in short, always",
and asked for every event as it happens. Each heartbeat cycle reads what was
logged since the last update (the actions table) and, when something worth
knowing happened, queues one short message to channels.whatsapp.alert_number
(harvey/meeting_alert.py's lane): emails sent, replies drafted, what needs you,
opt-outs and bounces, new senders, website leads, LinkedIn and WhatsApp runs,
site reviews. One message a cycle, not one per event, so a batch of sends is a
line, not twenty messages. In quiet hours (usage.quiet_hours) they gather and
go as one message when the quiet hours end. Meeting alerts go on their own.
"""

from __future__ import annotations

import json
import logging
from collections import Counter
from datetime import datetime, timezone

import aiosqlite

logger = logging.getLogger("harvey.owner_updates")

LAST_KEY = "owner:updates_through"
MAX_LINES = 12
MAX_CHARS = 1200
# What a line may name: an address, never more than this.
_WHO = 60


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _who(d: dict) -> str:
    return str(d.get("prospect_email") or d.get("prospect") or d.get("to") or d.get("from")
               or d.get("name") or "someone")[:_WHO]


def _plural(n: int, one: str, many: str) -> str:
    return f"{n} {one if n == 1 else many}"


def _channel_line(channel: str, d: dict, fields) -> str:
    """"LinkedIn: 2 invites, 1 reply in." from a run's report; a run that
    stopped says why; a run that did nothing says nothing."""
    if str(d.get("outcome", "")).startswith(("stopped", "error")):
        return f"{channel} {str(d['outcome'])[:120]}."
    bits = [_plural(int(d.get(key) or 0), one, many) for key, one, many in fields
            if int(d.get(key) or 0)]
    return f"{channel}: " + ", ".join(bits) + "." if bits else ""


def lines_for(rows: list[tuple[str, dict]]) -> list[str]:
    """The update's lines for these (action_type, details), in plain words."""
    out: list[str] = []
    sends = Counter()
    for kind, d in rows:
        if kind == "email_sent":
            if d.get("kind") == "reply":
                sends["reply"] += 1
            elif int(d.get("step") or 1) <= 1:
                sends["first"] += 1
            else:
                sends["follow"] += 1
    if sends:
        parts = [p for p in (
            _plural(sends["first"], "first email", "first emails") if sends["first"] else "",
            _plural(sends["follow"], "follow-up", "follow-ups") if sends["follow"] else "",
            _plural(sends["reply"], "reply", "replies") if sends["reply"] else "") if p]
        out.append("Sent " + ", ".join(parts) + ".")
    for kind, d in rows:
        line = ""
        if kind == "reply_queued":
            intent = str(d.get("intent") or "").replace("_", " ")
            line = f"{_who(d)} replied ({intent}); Harvey answered."
        elif kind == "escalation":
            line = f"Needs you: {_who(d)}" + (f" ({str(d.get('reason'))[:90]})" if d.get("reason") else "")
        elif kind == "opt_out":
            line = f"{_who(d)} opted out."
        elif kind == "bounce":
            line = f"Bounced: {_who(d)}."
        elif kind == "new_sender":
            line = f"New email from {_who(d)} ({d.get('sorted_as') or 'unsorted'})."
        elif kind == "not_a_client":
            line = (f"{_who(d)}: job seeker, pointed to HR." if d.get("intent") == "job_seeker"
                    else f"{_who(d)}: selling to us, no reply.")
        elif kind == "hand_sent":
            line = f"Saw your hand-written email to {_who(d)}; Harvey stepped back."
        elif kind == "inbound_draft":
            line = f"Website lead {_who(d)}: follow-up written."
        elif kind == "site_review":
            line = (f"Site review for {str(d.get('site') or '')[:_WHO]} written, waiting for you."
                    if d.get("ok") else f"Couldn't write a site review for {str(d.get('site') or '')[:_WHO]}.")
        elif kind == "whatsapp_run":
            line = _channel_line("WhatsApp", d, (("sent", "sent", "sent"),
                                                 ("inbound", "reply in", "replies in")))
        elif kind in ("linkedin_run", "linkedin_inbox_check"):
            line = _channel_line("LinkedIn", d, (("sent_invites", "invite", "invites"),
                                                 ("sent_messages", "message sent", "messages sent"),
                                                 ("inbound", "reply in", "replies in")))
        if line:
            out.append(line)
    return out


def compose(lines: list[str]) -> str:
    shown = lines[:MAX_LINES]
    if len(lines) > MAX_LINES:
        shown.append(f"+{len(lines) - MAX_LINES} more on the dashboard.")
    return ("Harvey update\n" + "\n".join(f"- {l}" for l in shown))[:MAX_CHARS]


async def _rows_since(state, since: str) -> list[tuple[str, str, dict]]:
    async with aiosqlite.connect(state.db_path, timeout=30) as db:
        async with db.execute(
                "SELECT created_at, action_type, details_json FROM actions WHERE created_at > ? "
                "ORDER BY created_at", (since,)) as cursor:
            raw = await cursor.fetchall()
    rows = []
    for at, kind, details in raw:
        try:
            d = json.loads(details or "{}")
        except ValueError:
            d = {}
        rows.append((at, kind, d if isinstance(d, dict) else {}))
    return rows


async def send(state, config, quiet: bool = False) -> bool:
    """Queue one update to your phone about what happened since the last
    one. ``quiet``: in quiet hours nothing goes; it all goes after. Never raises."""
    from harvey.meeting_alert import alert_number, queue_to_owner
    try:
        if not alert_number(config) or quiet:
            return False
        since = await state.get_setting(LAST_KEY)
        if not since:
            # The first time: from now on, not the whole history.
            await state.set_setting(LAST_KEY, _now().strftime("%Y-%m-%d %H:%M:%S"))
            return False
        rows = await _rows_since(state, since)
        if not rows:
            return False
        lines = lines_for([(kind, d) for _, kind, d in rows])
        through = rows[-1][0]
        if not lines:
            await state.set_setting(LAST_KEY, through)
            return False
        if not await queue_to_owner(state, config, compose(lines)):
            return False
        await state.set_setting(LAST_KEY, through)
        return True
    except Exception as e:
        logger.warning(f"Owner update failed: {e}")
        return False
