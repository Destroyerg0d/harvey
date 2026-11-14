"""State methods for the WhatsApp tables, mixed into StateManager.

Kept out of harvey/state.py so that file only creates the tables. Nothing here
imports harvey.state (state imports this).

Two rules live here so nothing above can get round them:

- ``wa_claim`` moves an approved message to maybe_sent BEFORE Enter is pressed.
  A crash after that leaves it maybe_sent, not approved, so it can't go twice.
- ``wa_retry`` only takes a failed message back. A maybe_sent one never returns
  to the queue: the owner says whether it went (``wa_resolve_maybe_sent``).
"""

import uuid

import aiosqlite

from harvey.linkedin.store import _iso, _now
from harvey.whatsapp.schema import MESSAGE_KINDS, MESSAGE_STATUSES, OWNER_STAGE, STAGES

STAGE_RANK = {"queued": 0, "messaged": 1, "replied": 2, "warm": 3, "meeting": 4}
# Error prefix on a draft the review sent back for a human: approve-all skips these.
HELD_FOR_REVIEW = "held for your review: "
AUTOPILOT_OFF = "waiting for you: WhatsApp autopilot was turned off before it went out"
MAYBE_SENT_NOTE = ("Enter was pressed but the chat didn't show the message. Look at the chat on "
                   "your phone: Harvey won't send this again.")
# What still stands between a contact and a new draft.
HOLDING_STATUSES = ("draft", "approved", "failed", "maybe_sent")

_CONTACT_COLUMNS = frozenset({
    "phone", "raw_phone", "name", "email", "prospect_id", "country", "source", "context",
    "review_reason", "no_message_reason", "last_outbound_at", "last_inbound_at", "next_action_at",
})
_MESSAGE_COLUMNS = frozenset({"body", "status", "error", "intent", "wa_id", "sent_at", "approved_by"})


def advance_stage(current: str, target: str) -> str:
    """The stage after moving ``current`` towards ``target``: forward moves
    only, declined is final, and no number on WhatsApp stops only a contact
    who hasn't answered."""
    current = current if current in STAGES else "queued"
    if target not in STAGES or current in ("declined", OWNER_STAGE) or target == OWNER_STAGE:
        return current
    if target == "declined":
        return "declined"
    if target == "no_whatsapp":
        return "no_whatsapp" if current in ("queued", "messaged", "needs_review") else current
    if current == "no_whatsapp":
        return current
    if target == "needs_review":
        return current
    if current == "needs_review":
        return target
    return target if STAGE_RANK[target] > STAGE_RANK[current] else current


def _fields(fields: dict, allowed: frozenset) -> dict:
    return {k: _iso(v) if k.endswith("_at") else v for k, v in fields.items() if k in allowed}


class WhatsAppStateMixin:
    """WhatsApp contacts and messages. Needs ``self._connect`` (StateManager)."""

    # ── Contacts ──

    async def wa_add_contact(self, *, phone: str, source: str, stage: str = "queued",
                             **fields) -> tuple[str, bool]:
        """The contact for this number, made if new: (id, created). One that
        exists keeps what it has; only blank fields are filled."""
        fields = {k: v for k, v in _fields(fields, _CONTACT_COLUMNS).items() if v not in (None, "")}
        existing = await self.wa_get_contact_by_phone(phone) if phone else None
        if existing is None and not phone and fields.get("raw_phone"):
            existing = await self.wa_get_review_by_raw(fields["raw_phone"])
        if existing is None:
            contact_id = uuid.uuid4().hex[:12]
            now = _now()
            cols = ["id", "phone", "source", "stage", "created_at", "updated_at", *fields]
            vals = [contact_id, phone, source, stage if stage in STAGES else "queued", now, now,
                    *fields.values()]
            async with self._connect() as db:
                await db.execute(
                    f"INSERT INTO whatsapp_contacts ({', '.join(cols)}) "
                    f"VALUES ({', '.join('?' for _ in cols)})", vals)
                await db.commit()
            return contact_id, True
        updates = {k: v for k, v in fields.items() if not existing.get(k)}
        if updates:
            await self.wa_update_contact(existing["id"], **updates)
        return existing["id"], False

    async def wa_get_contact(self, contact_id: str) -> dict | None:
        rows = await self._wa_query("SELECT * FROM whatsapp_contacts WHERE id = ?", (contact_id,))
        return rows[0] if rows else None

    async def wa_get_contact_by_phone(self, phone: str) -> dict | None:
        rows = await self._wa_query("SELECT * FROM whatsapp_contacts WHERE phone = ?", (phone,))
        return rows[0] if rows else None

    async def wa_get_review_by_raw(self, raw_phone: str) -> dict | None:
        rows = await self._wa_query(
            "SELECT * FROM whatsapp_contacts WHERE stage = 'needs_review' AND raw_phone = ?",
            (raw_phone,))
        return rows[0] if rows else None

    async def wa_contacts(self, stages=None, limit: int = 5000, oldest_first: bool = False) -> list[dict]:
        order = "created_at, rowid" if oldest_first else "updated_at DESC, rowid DESC"
        if stages:
            stages = list(stages)
            return await self._wa_query(
                f"SELECT * FROM whatsapp_contacts WHERE stage IN ({', '.join('?' for _ in stages)}) "
                f"ORDER BY {order} LIMIT ?", (*stages, limit))
        return await self._wa_query(f"SELECT * FROM whatsapp_contacts ORDER BY {order} LIMIT ?", (limit,))

    async def wa_update_contact(self, contact_id: str, **fields) -> bool:
        fields = _fields(fields, _CONTACT_COLUMNS)
        if not fields:
            return False
        fields["updated_at"] = _now()
        async with self._connect() as db:
            cur = await db.execute(
                f"UPDATE whatsapp_contacts SET {', '.join(f'{k} = ?' for k in fields)} WHERE id = ?",
                (*fields.values(), contact_id))
            await db.commit()
            return cur.rowcount > 0

    async def wa_set_stage(self, contact_id: str, stage: str, **fields) -> str:
        """Move a contact forward to ``stage`` (and set ``fields``). Returns
        the stage it is at afterwards, unchanged for a move that isn't allowed."""
        contact = await self.wa_get_contact(contact_id)
        if contact is None:
            return ""
        new = advance_stage(contact.get("stage") or "queued", stage)
        updates = _fields(fields, _CONTACT_COLUMNS)
        updates["updated_at"] = _now()
        updates["stage"] = new
        async with self._connect() as db:
            await db.execute(
                f"UPDATE whatsapp_contacts SET {', '.join(f'{k} = ?' for k in updates)} WHERE id = ?",
                (*updates.values(), contact_id))
            await db.commit()
        return new

    # ── Messages ──

    async def wa_add_message(self, contact_id: str, *, direction: str = "out", kind: str = "first",
                             body: str = "", status: str = "draft", error: str = "",
                             intent: str = "", wa_id: str = "", sent_at=None, approved_by: str = "",
                             created_at=None) -> str | None:
        """Record a message. None when WhatsApp's id for it is already on file."""
        if direction not in ("in", "out") or kind not in MESSAGE_KINDS:
            raise ValueError(f"bad WhatsApp message: {direction}/{kind}")
        if status not in MESSAGE_STATUSES:
            raise ValueError(f"bad WhatsApp message status: {status}")
        message_id = uuid.uuid4().hex[:12]
        async with self._connect() as db:
            cur = await db.execute(
                "INSERT OR IGNORE INTO whatsapp_messages (id, contact_id, direction, kind, body, status, "
                "error, intent, wa_id, created_at, sent_at, approved_by) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (message_id, contact_id, direction, kind, (body or "").strip(), status, error, intent,
                 wa_id, _iso(created_at) or _now(), _iso(sent_at), approved_by))
            await db.commit()
            return message_id if cur.rowcount else None

    async def wa_get_message(self, message_id: str) -> dict | None:
        rows = await self._wa_query("SELECT * FROM whatsapp_messages WHERE id = ?", (message_id,))
        return rows[0] if rows else None

    async def wa_messages(self, contact_id: str | None = None, statuses=None, kinds=None,
                          direction: str | None = None, limit: int = 1000) -> list[dict]:
        where, params = [], []
        if contact_id:
            where.append("contact_id = ?")
            params.append(contact_id)
        for column, values in (("status", statuses), ("kind", kinds)):
            if values:
                values = list(values)
                where.append(f"{column} IN ({', '.join('?' for _ in values)})")
                params.extend(values)
        if direction:
            where.append("direction = ?")
            params.append(direction)
        sql = "SELECT * FROM whatsapp_messages"
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY COALESCE(sent_at, created_at), created_at, rowid LIMIT ?"
        return await self._wa_query(sql, (*params, limit))

    async def wa_update_message(self, message_id: str, **fields) -> bool:
        fields = _fields(fields, _MESSAGE_COLUMNS)
        if not fields:
            return False
        async with self._connect() as db:
            cur = await db.execute(
                f"UPDATE whatsapp_messages SET {', '.join(f'{k} = ?' for k in fields)} WHERE id = ?",
                (*fields.values(), message_id))
            await db.commit()
            return cur.rowcount > 0

    async def wa_approve(self, message_id: str | None = None, by: str = "you") -> int:
        """Approve one draft, or every draft when ``message_id`` is None (not
        the ones held for a look: those are approved one at a time)."""
        async with self._connect() as db:
            if message_id:
                cur = await db.execute(
                    "UPDATE whatsapp_messages SET status = 'approved', approved_by = ?, error = '' "
                    "WHERE id = ? AND status = 'draft' AND direction = 'out'", (by, message_id))
            else:
                cur = await db.execute(
                    "UPDATE whatsapp_messages SET status = 'approved', approved_by = ?, error = '' "
                    "WHERE status = 'draft' AND direction = 'out' "
                    "AND COALESCE(error, '') NOT LIKE ?", (by, HELD_FOR_REVIEW + "%"))
            await db.commit()
            return cur.rowcount

    async def wa_retry(self, message_id: str) -> int:
        """A failed send goes back to the queue, approved by you. Never a
        maybe_sent one: that may have reached them."""
        async with self._connect() as db:
            cur = await db.execute(
                "UPDATE whatsapp_messages SET status = 'approved', approved_by = 'you', error = '' "
                "WHERE id = ? AND status = 'failed' AND direction = 'out'", (message_id,))
            await db.commit()
            return cur.rowcount

    async def wa_skip(self, message_id: str, error: str = "") -> int:
        async with self._connect() as db:
            cur = await db.execute(
                "UPDATE whatsapp_messages SET status = 'skipped', "
                "error = CASE WHEN ? != '' THEN ? ELSE error END "
                "WHERE id = ? AND status IN ('draft', 'approved', 'failed') AND direction = 'out'",
                (error, error, message_id))
            await db.commit()
            return cur.rowcount

    async def wa_edit_draft(self, message_id: str, body: str) -> int:
        """Rewrite a draft. Never approves it: that stays a separate step."""
        async with self._connect() as db:
            cur = await db.execute(
                "UPDATE whatsapp_messages SET body = ? WHERE id = ? AND status = 'draft' "
                "AND direction = 'out'", ((body or "").strip(), message_id))
            await db.commit()
            return cur.rowcount

    async def wa_requeue_autopilot(self) -> int:
        """Autopilot was turned off: what it approved and hasn't sent waits for you."""
        async with self._connect() as db:
            cur = await db.execute(
                "UPDATE whatsapp_messages SET status = 'draft', approved_by = '', error = ? "
                "WHERE status = 'approved' AND approved_by = 'autopilot'", (AUTOPILOT_OFF,))
            await db.commit()
            return cur.rowcount

    async def wa_claim(self, message_id: str) -> bool:
        """Just before Enter: approved becomes maybe_sent. False when it isn't
        approved any more (you skipped it, autopilot went off) and must not go."""
        async with self._connect() as db:
            cur = await db.execute(
                "UPDATE whatsapp_messages SET status = 'maybe_sent', error = ? "
                "WHERE id = ? AND status = 'approved'", (MAYBE_SENT_NOTE, message_id))
            await db.commit()
            return cur.rowcount > 0

    async def wa_mark_sent(self, message_id: str, at=None) -> None:
        """A send went out: the message, and the contact's stage and times."""
        message = await self.wa_get_message(message_id)
        if message is None:
            return
        at = _iso(at) or _now()
        await self.wa_update_message(message_id, status="sent", sent_at=at, error="")
        await self.wa_set_stage(message["contact_id"], "messaged", last_outbound_at=at)

    async def wa_resolve_maybe_sent(self, message_id: str, went: bool) -> int:
        """The owner looked at the chat. Went: it counts as sent. Didn't: it is
        closed, and still isn't re-sent; a new message is a new decision."""
        message = await self.wa_get_message(message_id)
        if message is None or message["status"] != "maybe_sent":
            return 0
        if went:
            await self.wa_mark_sent(message_id)
        else:
            await self.wa_update_message(message_id, status="skipped",
                                         error="you checked: it didn't go out")
        return 1

    async def wa_unanswered(self, contact_id: str) -> list[dict]:
        """Outbound messages after their last inbound one. One that may have
        gone counts: being careful with it must not mean one more message."""
        out: list[dict] = []
        for row in await self.wa_messages(contact_id=contact_id):
            if row["direction"] == "in":
                out = []
            elif row["status"] in ("sent", "maybe_sent"):
                out.append(row)
        return out

    async def wa_pending_for(self, contact_id: str, kinds=None) -> list[dict]:
        """Drafts and approved messages still waiting to go to this contact."""
        return await self.wa_messages(contact_id=contact_id, statuses=("draft", "approved"),
                                      kinds=kinds, direction="out")

    # ── Reporting ──

    async def wa_funnel(self) -> dict:
        talking = "('replied', 'warm', 'meeting')"
        reached = {
            "contacts": "1",
            "queued": "stage = 'queued'",
            "messaged": ("last_outbound_at IS NOT NULL OR stage IN "
                         "('messaged', 'replied', 'warm', 'meeting')"),
            "replied": f"last_inbound_at IS NOT NULL OR stage IN {talking}",
            "positive": "stage IN ('warm', 'meeting')",
            "meetings": "stage = 'meeting'",
            "declined": "stage = 'declined'",
            "no_whatsapp": "stage = 'no_whatsapp'",
            "needs_review": "stage = 'needs_review'",
        }
        sql = "SELECT " + ", ".join(
            f"COALESCE(SUM(CASE WHEN {cond} THEN 1 ELSE 0 END), 0) AS {key}"
            for key, cond in reached.items()) + " FROM whatsapp_contacts"
        rows = await self._wa_query(sql)
        f = {k: int(v or 0) for k, v in (rows[0] if rows else {}).items()}
        for k in reached:
            f.setdefault(k, 0)

        def rate(a, b):
            return round(f[a] / f[b], 4) if f.get(b) else None
        f["reply_rate"] = rate("replied", "messaged")
        f["positive_rate"] = rate("positive", "replied")
        f["decline_rate"] = rate("declined", "messaged")
        return f

    async def _wa_query(self, sql: str, params: tuple = ()) -> list[dict]:
        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(sql, params) as cur:
                return [dict(r) for r in await cur.fetchall()]
