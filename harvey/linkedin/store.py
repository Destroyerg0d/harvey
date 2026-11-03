"""State methods for the LinkedIn tables, mixed into StateManager.

Kept out of harvey/state.py so that file only creates the tables. Nothing
here imports harvey.state (state imports this).

A lead's stage only moves forward (``advance_stage``); declined is final:
someone who said no is never contacted again on LinkedIn, and their email
side is stopped too (the agent does that, with the existing state helpers).
"""

import uuid
from datetime import datetime, timedelta, timezone

import aiosqlite

from harvey.linkedin.schema import STAGES, MESSAGE_KINDS, MESSAGE_STATUSES

# Forward order. withdrawn sits with invited: the invite can be sent again.
STAGE_RANK = {"found": 0, "invited": 1, "withdrawn": 1, "accepted": 2, "messaged": 3,
              "replied": 4, "warm": 5, "meeting": 6}
# Error prefix on a draft the autopilot review sent back for a human (as
# state.HELD_FOR_REVIEW is for email): approve-all skips these.
HELD_FOR_REVIEW = "held for your review: "
# Error on drafts autopilot had approved when it was turned off.
AUTOPILOT_OFF = "waiting for you: LinkedIn autopilot was turned off before it went out"
# Error prefix for a send that may have reached them: never retried.
MAYBE_SENT = "may have gone out: "

_LEAD_COLUMNS = frozenset({
    "name", "first_name", "headline", "company", "profile_url", "urn", "prospect_id",
    "company_id", "invited_at", "accepted_at", "last_outbound_at", "last_inbound_at",
    "next_action_at", "note", "source", "no_message_reason", "thread_url",
})
_MESSAGE_COLUMNS = frozenset({
    "body", "status", "error", "thread_url", "intent", "sent_at", "approved_by",
})


def _now() -> str:
    return datetime.now(timezone.utc).replace(tzinfo=None).isoformat(timespec="seconds")


def _iso(value) -> str | None:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        if value.tzinfo is not None:
            value = value.astimezone(timezone.utc).replace(tzinfo=None)
        return value.isoformat(timespec="seconds")
    return str(value)


def normalize_slug(slug: str) -> str:
    return (slug or "").strip().strip("/").lower()


def advance_stage(current: str, target: str) -> str:
    """The stage after moving ``current`` towards ``target``: forward moves
    only, declined is final, and withdrawn applies only to an open invite."""
    current = current if current in STAGES else "found"
    if target not in STAGES or current == "declined":
        return current
    if target == "declined":
        return "declined"
    if target == "withdrawn":
        return "withdrawn" if current in ("found", "invited") else current
    if current == "withdrawn" and target == "invited":
        return "invited"
    return target if STAGE_RANK[target] > STAGE_RANK[current] else current


class LinkedInStateMixin:
    """LinkedIn leads and messages. Needs ``self._connect`` (StateManager)."""

    # ── Leads ──

    async def li_upsert_lead(self, slug: str, stage: str = "found", **fields) -> tuple[str, bool]:
        """The lead for ``slug``, made if new: (id, created). An existing
        lead keeps what it has; only blank fields are filled, and its stage
        moves forward to ``stage`` if that is further on."""
        slug = normalize_slug(slug)
        if not slug:
            raise ValueError("a LinkedIn lead needs a profile slug")
        fields = {k: _iso(v) if k.endswith("_at") else v for k, v in fields.items()
                  if k in _LEAD_COLUMNS and v not in (None, "")}
        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            async with db.execute("SELECT * FROM linkedin_leads WHERE slug = ?", (slug,)) as cur:
                row = await cur.fetchone()
            if row is None:
                lead_id = uuid.uuid4().hex[:12]
                cols = ["id", "slug", "stage", "created_at", "updated_at", *fields]
                now = _now()
                vals = [lead_id, slug, stage if stage in STAGES else "found", now, now,
                        *fields.values()]
                await db.execute(
                    f"INSERT INTO linkedin_leads ({', '.join(cols)}) "
                    f"VALUES ({', '.join('?' for _ in cols)})", vals)
                await db.commit()
                return lead_id, True
            have = dict(row)
            updates = {k: v for k, v in fields.items() if not have.get(k)}
            new_stage = advance_stage(have.get("stage") or "found", stage)
            if new_stage != have.get("stage"):
                updates["stage"] = new_stage
            if updates:
                updates["updated_at"] = _now()
                sets = ", ".join(f"{k} = ?" for k in updates)
                await db.execute(f"UPDATE linkedin_leads SET {sets} WHERE id = ?",
                                 (*updates.values(), have["id"]))
                await db.commit()
            return have["id"], False

    async def li_get_lead(self, lead_id: str) -> dict | None:
        rows = await self._li_query("SELECT * FROM linkedin_leads WHERE id = ?", (lead_id,))
        return rows[0] if rows else None

    async def li_get_lead_by_slug(self, slug: str) -> dict | None:
        rows = await self._li_query("SELECT * FROM linkedin_leads WHERE slug = ?",
                                    (normalize_slug(slug),))
        return rows[0] if rows else None

    async def li_leads(self, stages=None, limit: int = 5000) -> list[dict]:
        if stages:
            stages = list(stages)
            return await self._li_query(
                f"SELECT * FROM linkedin_leads WHERE stage IN ({', '.join('?' for _ in stages)}) "
                "ORDER BY updated_at DESC LIMIT ?", (*stages, limit))
        return await self._li_query(
            "SELECT * FROM linkedin_leads ORDER BY updated_at DESC LIMIT ?", (limit,))

    async def li_update_lead(self, lead_id: str, **fields) -> bool:
        fields = {k: _iso(v) if k.endswith("_at") else v for k, v in fields.items()
                  if k in _LEAD_COLUMNS}
        if not fields:
            return False
        fields["updated_at"] = _now()
        async with self._connect() as db:
            cur = await db.execute(
                f"UPDATE linkedin_leads SET {', '.join(f'{k} = ?' for k in fields)} WHERE id = ?",
                (*fields.values(), lead_id))
            await db.commit()
            return cur.rowcount > 0

    async def li_set_stage(self, lead_id: str, stage: str, **fields) -> str:
        """Move a lead forward to ``stage`` (and set ``fields``). Returns the
        stage it is at afterwards, which is unchanged for a backward move."""
        lead = await self.li_get_lead(lead_id)
        if lead is None:
            return ""
        new = advance_stage(lead.get("stage") or "found", stage)
        updates = {k: _iso(v) if k.endswith("_at") else v for k, v in fields.items()
                   if k in _LEAD_COLUMNS}
        updates["updated_at"] = _now()
        updates["stage"] = new
        async with self._connect() as db:
            await db.execute(
                f"UPDATE linkedin_leads SET {', '.join(f'{k} = ?' for k in updates)} WHERE id = ?",
                (*updates.values(), lead_id))
            await db.commit()
        return new

    # ── Messages ──

    async def li_add_message(self, lead_id: str, *, direction: str = "out", kind: str = "message",
                             body: str = "", status: str = "draft", error: str = "",
                             thread_url: str = "", sent_at=None, approved_by: str = "",
                             intent: str = "", created_at=None, dedupe: bool = True) -> str | None:
        """Record a message. With ``dedupe`` (the default) the same text in
        the same direction and kind for this lead is recorded once: None
        when it was already there (sync and the import are re-run safely)."""
        if direction not in ("in", "out") or kind not in MESSAGE_KINDS:
            raise ValueError(f"bad LinkedIn message: {direction}/{kind}")
        if status not in MESSAGE_STATUSES:
            raise ValueError(f"bad LinkedIn message status: {status}")
        body = (body or "").strip()
        async with self._connect() as db:
            if dedupe:
                async with db.execute(
                        "SELECT id FROM linkedin_messages WHERE lead_id = ? AND direction = ? "
                        "AND kind = ? AND body = ? AND status != 'skipped' LIMIT 1",
                        (lead_id, direction, kind, body)) as cur:
                    if await cur.fetchone():
                        return None
            message_id = uuid.uuid4().hex[:12]
            await db.execute(
                "INSERT INTO linkedin_messages (id, lead_id, direction, kind, body, status, error, "
                "thread_url, intent, created_at, sent_at, approved_by) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (message_id, lead_id, direction, kind, body, status, error, thread_url, intent,
                 _iso(created_at) or _now(), _iso(sent_at), approved_by))
            await db.commit()
            return message_id

    async def li_get_message(self, message_id: str) -> dict | None:
        rows = await self._li_query("SELECT * FROM linkedin_messages WHERE id = ?", (message_id,))
        return rows[0] if rows else None

    async def li_messages(self, lead_id: str | None = None, statuses=None, kinds=None,
                          direction: str | None = None, limit: int = 1000) -> list[dict]:
        where, params = [], []
        if lead_id:
            where.append("lead_id = ?")
            params.append(lead_id)
        for column, values in (("status", statuses), ("kind", kinds)):
            if values:
                values = list(values)
                where.append(f"{column} IN ({', '.join('?' for _ in values)})")
                params.extend(values)
        if direction:
            where.append("direction = ?")
            params.append(direction)
        sql = "SELECT * FROM linkedin_messages"
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY COALESCE(sent_at, created_at), created_at LIMIT ?"
        return await self._li_query(sql, (*params, limit))

    async def li_update_message(self, message_id: str, **fields) -> bool:
        fields = {k: _iso(v) if k.endswith("_at") else v for k, v in fields.items()
                  if k in _MESSAGE_COLUMNS}
        if not fields:
            return False
        async with self._connect() as db:
            cur = await db.execute(
                f"UPDATE linkedin_messages SET {', '.join(f'{k} = ?' for k in fields)} WHERE id = ?",
                (*fields.values(), message_id))
            await db.commit()
            return cur.rowcount > 0

    async def li_approve(self, message_id: str | None = None, by: str = "you") -> int:
        """Approve one draft, or every draft when ``message_id`` is None
        (not the ones held for a look: those are approved one at a time)."""
        async with self._connect() as db:
            if message_id:
                cur = await db.execute(
                    "UPDATE linkedin_messages SET status = 'approved', approved_by = ?, error = '' "
                    "WHERE id = ? AND status = 'draft' AND direction = 'out'", (by, message_id))
            else:
                cur = await db.execute(
                    "UPDATE linkedin_messages SET status = 'approved', approved_by = ?, error = '' "
                    "WHERE status = 'draft' AND direction = 'out' "
                    "AND COALESCE(error, '') NOT LIKE ?", (by, HELD_FOR_REVIEW + "%"))
            await db.commit()
            return cur.rowcount

    async def li_retry(self, message_id: str) -> int:
        """A failed send goes back to the queue, approved by you. Not one that
        may have gone out: check the thread for those."""
        async with self._connect() as db:
            cur = await db.execute(
                "UPDATE linkedin_messages SET status = 'approved', approved_by = 'you', error = '' "
                "WHERE id = ? AND status = 'failed' AND COALESCE(error, '') NOT LIKE ?",
                (message_id, MAYBE_SENT + "%"))
            await db.commit()
            return cur.rowcount

    async def li_skip(self, message_id: str) -> int:
        async with self._connect() as db:
            cur = await db.execute(
                "UPDATE linkedin_messages SET status = 'skipped' "
                "WHERE id = ? AND status IN ('draft', 'approved', 'failed') AND direction = 'out'",
                (message_id,))
            await db.commit()
            return cur.rowcount

    async def li_edit_draft(self, message_id: str, body: str) -> int:
        """Rewrite a draft. Never approves it: that stays a separate step."""
        async with self._connect() as db:
            cur = await db.execute(
                "UPDATE linkedin_messages SET body = ? WHERE id = ? AND status = 'draft' "
                "AND direction = 'out'", ((body or "").strip(), message_id))
            await db.commit()
            return cur.rowcount

    async def li_requeue_autopilot(self) -> int:
        """Autopilot was turned off: what it approved and hasn't sent waits for you."""
        async with self._connect() as db:
            cur = await db.execute(
                "UPDATE linkedin_messages SET status = 'draft', approved_by = '', error = ? "
                "WHERE status = 'approved' AND approved_by = 'autopilot'", (AUTOPILOT_OFF,))
            await db.commit()
            return cur.rowcount

    async def li_mark_sent(self, message_id: str, thread_url: str = "", at=None) -> None:
        """A send went out: the message, and the lead's stage and times."""
        message = await self.li_get_message(message_id)
        if message is None:
            return
        at = _iso(at) or _now()
        await self.li_update_message(message_id, status="sent", sent_at=at, error="",
                                     **({"thread_url": thread_url} if thread_url else {}))
        extra = {"last_outbound_at": at}
        if thread_url:
            extra["thread_url"] = thread_url
        if message["kind"] == "invite_note":
            await self.li_set_stage(message["lead_id"], "invited", invited_at=at, **extra)
        else:
            await self.li_set_stage(message["lead_id"], "messaged", **extra)

    async def li_unanswered(self, lead_id: str) -> list[dict]:
        """Outbound messages that went out after their last inbound one."""
        rows = await self.li_messages(lead_id=lead_id)
        out: list[dict] = []
        for row in rows:
            if row["direction"] == "in":
                out = []
            elif row["status"] == "sent":
                out.append(row)
        return out

    async def li_pending_for(self, lead_id: str, kinds=None) -> list[dict]:
        """Drafts and approved messages still waiting to go to this lead."""
        return await self.li_messages(lead_id=lead_id, statuses=("draft", "approved"),
                                      kinds=kinds, direction="out")

    # ── Reporting ──

    async def li_funnel(self) -> dict:
        reached = {
            "found": "1",
            "invited": ("invited_at IS NOT NULL OR accepted_at IS NOT NULL OR stage IN "
                        "('invited', 'withdrawn', 'accepted', 'messaged', 'replied', 'warm', 'meeting')"),
            "accepted": ("accepted_at IS NOT NULL OR stage IN "
                         "('accepted', 'messaged', 'replied', 'warm', 'meeting')"),
            "messaged": ("stage IN ('messaged', 'replied', 'warm', 'meeting') OR EXISTS ("
                         "SELECT 1 FROM linkedin_messages m WHERE m.lead_id = l.id AND "
                         "m.direction = 'out' AND m.kind IN ('message', 'reply') AND m.status = 'sent')"),
            "replied": "last_inbound_at IS NOT NULL OR stage IN ('replied', 'warm', 'meeting')",
            "positive": "stage IN ('warm', 'meeting')",
            "meetings": "stage = 'meeting'",
            "declined": "stage = 'declined'",
            "withdrawn": "stage = 'withdrawn'",
        }
        sql = "SELECT " + ", ".join(
            f"COALESCE(SUM(CASE WHEN {cond} THEN 1 ELSE 0 END), 0) AS {key}"
            for key, cond in reached.items()) + " FROM linkedin_leads l"
        rows = await self._li_query(sql)
        f = {k: int(v or 0) for k, v in (rows[0] if rows else {}).items()}
        for k in reached:
            f.setdefault(k, 0)

        def rate(a, b):
            return round(f[a] / f[b], 4) if f.get(b) else None
        f["acceptance_rate"] = rate("accepted", "invited")
        f["reply_rate"] = rate("replied", "messaged")
        f["positive_rate"] = rate("positive", "replied")
        return f

    async def li_daily_activity(self, days: int = 14, tz_minutes: int = 0) -> dict:
        """Invites and messages sent, and replies received, per day for the
        last ``days`` days in the viewer's time zone (minutes east of UTC)."""
        days = max(1, min(90, int(days)))
        shift = f"{int(tz_minutes):+d} minutes"
        today = (datetime.now(timezone.utc) + timedelta(minutes=int(tz_minutes))).date()
        labels = [(today - timedelta(days=n)).isoformat() for n in range(days - 1, -1, -1)]
        rows = await self._li_query(
            "SELECT date(COALESCE(sent_at, created_at), ?) AS d, direction, kind, COUNT(*) AS n "
            "FROM linkedin_messages WHERE status = 'sent' "
            "AND date(COALESCE(sent_at, created_at), ?) >= ? GROUP BY d, direction, kind",
            (shift, shift, labels[0]))
        series = {"invites": {}, "messages": {}, "replies": {}}
        for r in rows:
            key = ("replies" if r["direction"] == "in" else
                   "invites" if r["kind"] == "invite_note" else "messages")
            series[key][r["d"]] = series[key].get(r["d"], 0) + int(r["n"])
        return {"days": labels, **{k: [v.get(d, 0) for d in labels] for k, v in series.items()}}

    async def _li_query(self, sql: str, params: tuple = ()) -> list[dict]:
        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(sql, params) as cur:
                return [dict(r) for r in await cur.fetchall()]
