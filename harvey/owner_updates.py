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

import aiosqlite

logger = logging.getLogger("harvey.owner_updates")

LAST_KEY = "owner:updates_through"
# Where the update now waiting in the queue starts: a newer one taking it back
# starts there instead.
WAITING_SINCE_KEY = "owner:update_waiting_since"
HEADER = "Harvey update"
MERGED_NOTE = "merged into a newer update"
MAX_LINES = 12
MAX_CHARS = 1200
# What a line may name: an address, never more than this.
_WHO = 60


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
            what = str(d.get("site") or d.get("name") or "")[:_WHO]
            line = (f"Site review for {what} written, waiting for you."
                    if d.get("ok") else f"Couldn't write a site review for {what}"
                    + (f" ({str(d.get('why'))[:80]})." if d.get("why") else "."))
        elif kind == "linkedin_daily":
            line = (f"LinkedIn today: {int(d.get('sent') or 0)} connects (target "
                    f"{int(d.get('target') or 0)}), {int(d.get('accepted') or 0)} accepted. Last 7 days: "
                    f"{int(d.get('week_sent') or 0)} sent, {int(d.get('week_accepted') or 0)} accepted."
                    + (f" Short because {str(d.get('why'))[:120]}." if d.get("why") else ""))
        elif kind == "linkedin_invite_limit":
            line = ("LinkedIn's weekly invite limit was reached: connects wait 2 days, "
                    "messages and replies carry on.")
        elif kind == "needs_you":
            line = f"Needs you: {str(d.get('title') or '')[:160]}"
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
    """The message: a plain line per item, no list markers. WhatsApp makes a
    list of "- " or a bullet at a line's start, and then neither the box nor
    the sent bubble holds the text word for word (harvey/whatsapp/pages.py
    ``composer_safe``)."""
    shown = lines[:MAX_LINES]
    if len(lines) > MAX_LINES:
        shown.append(f"+{len(lines) - MAX_LINES} more on the dashboard.")
    return (HEADER + "\n" + "\n".join(shown))[:MAX_CHARS]


async def _withdraw_unsent(state) -> int:
    """Take back updates still waiting in the WhatsApp queue. Only ones not yet
    claimed for sending (status approved), so nothing half-sent is touched;
    meeting alerts and questions to you are never taken back."""
    async with aiosqlite.connect(state.db_path, timeout=30) as db:
        cur = await db.execute(
            "UPDATE whatsapp_messages SET status = 'skipped', error = ? WHERE direction = 'out' "
            "AND kind = 'alert' AND status = 'approved' AND body LIKE ?",
            (MERGED_NOTE, HEADER + "\n%"))
        await db.commit()
        return cur.rowcount or 0


async def _mark(state, value: str) -> int | None:
    """A bookmark as the actions table's rowid. Actions are stamped to the
    second, so a time would skip what was logged in the same second just after
    an update; the rowid only grows. A time (what earlier versions kept, or a
    test's starting point) means the last action logged by then."""
    value = (value or "").strip()
    if not value:
        return None
    if value.isdigit():
        return int(value)
    async with aiosqlite.connect(state.db_path, timeout=30) as db:
        async with db.execute("SELECT COALESCE(MAX(rowid), 0) FROM actions WHERE created_at <= ?",
                              (value,)) as cursor:
            return int((await cursor.fetchone())[0])


async def _latest(state) -> int:
    async with aiosqlite.connect(state.db_path, timeout=30) as db:
        async with db.execute("SELECT COALESCE(MAX(rowid), 0) FROM actions") as cursor:
            return int((await cursor.fetchone())[0])


async def _rows_since(state, since: int) -> list[tuple[int, str, dict]]:
    async with aiosqlite.connect(state.db_path, timeout=30) as db:
        async with db.execute(
                "SELECT rowid, action_type, details_json FROM actions WHERE rowid > ? "
                "ORDER BY rowid", (since,)) as cursor:
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
        since = await _mark(state, await state.get_setting(LAST_KEY))
        if since is None:
            # The first time: from now on, not the whole history.
            await state.set_setting(LAST_KEY, str(await _latest(state)))
            return False
        rows = await _rows_since(state, since)
        if not rows:
            return False
        lines = lines_for([(kind, d) for _, kind, d in rows])
        through = str(rows[-1][0])
        if not lines:
            await state.set_setting(LAST_KEY, through)
            return False
        # Something new, and an update still waiting to go (WhatsApp paused, or
        # behind): that one is taken back and this one covers its time too, so
        # you get one message, not a pile.
        if await _withdraw_unsent(state):
            waiting = await _mark(state, await state.get_setting(WAITING_SINCE_KEY))
            if waiting is not None and waiting < since:
                since = waiting
                lines = lines_for([(kind, d) for _, kind, d in await _rows_since(state, since)])
        if not await queue_to_owner(state, config, compose(lines)):
            await state.set_setting(LAST_KEY, str(since))   # whatever was taken back goes next time
            return False
        await state.set_setting(WAITING_SINCE_KEY, str(since))
        await state.set_setting(LAST_KEY, through)
        return True
    except Exception as e:
        logger.warning(f"Owner update failed: {e}")
        return False
