"""Which segment's first email goes next, and the custom segment's ramp.

``channels.email.segment_weights`` gives each segment a target share of the
day's first emails (step 1 of a sequence), e.g. custom 0.40, wordpress 0.35,
shopify 0.25. Empty, nothing here runs and the Sender drains in queue order,
as before. With weights, the Sender only REORDERS what it was going to read
anyway (every per-mailbox cap, pause, window and check still applies):

- Follow-ups (step 2 on), replies and anything else that isn't a first email
  go first, in their usual order.
- Then first emails, each time from the weighted segment furthest below its
  share of today's first emails (sends already made today count, from local
  midnight, as the cold cap does). A segment with nothing due leaves its
  share to the others, so capacity never idles waiting for it. Segments
  without a weight (other, unknown) go after every weighted one.

The ramp (domain health). The custom segment (old shop software and
hand-built sites, the likeliest to bounce) starts at 15% of first emails,
not its target. After a 7-day window in which custom first emails and
follow-ups bounced under 2% (with at least 40 of them sent in the window) it
steps to 25%, then after another such window to its target. Over 3% (once
10 have gone out since the last change) it steps down one level. While it is
ramping its share is a ceiling: custom email over it waits for a later cycle
rather than taking share another segment left unused. The level and each
change are kept in the ``segment_ramp`` setting and shown on the Mailboxes tab.

A queued row's segment is its own ``segment`` column when set, else what
the company's profile says now (segment_stamp.segment_from_facts, the rule
the Sender stamps at send time).
"""

import json
import logging
from datetime import datetime, timedelta, timezone

import aiosqlite

from harvey.segment_stamp import segment_from_facts

logger = logging.getLogger("harvey.segment_mix")

# The segments a weight may name (segment_stamp.SEGMENTS).
MIX_SEGMENTS = ("shopify", "wordpress", "custom", "other", "unknown")
LABELS = {"shopify": "Shopify", "wordpress": "WordPress", "custom": "Custom",
          "other": "Other", "unknown": "Not classified"}

RAMP_KEY = "segment_ramp"
# Written when you approve a segment-mix lesson (harvey/lessons.py), deleted
# when it is retired: JSON {segment: share}. While set, it is the mix.
LEARNED_KEY = "segment_weights"
RAMP_SEGMENT = "custom"
RAMP_STEPS = (0.15, 0.25)
RAMP_WINDOW = timedelta(days=7)
RAMP_MIN_SENDS = 40          # sent in the window before a step up
RAMP_UP_BELOW = 0.02         # bounce rate under this steps up
RAMP_DOWN_ABOVE = 0.03       # bounce rate over this steps down
RAMP_MIN_SENDS_DOWN = 10     # sent since the last change before a step down
RAMP_HISTORY = 30            # changes kept
RAMP_RULE = (
    "Custom starts at 15% of the day's first emails. After 7 days with custom bounces under 2% "
    f"({RAMP_MIN_SENDS}+ custom emails sent in those days) it steps to 25%, then to its target. "
    f"Over 3% (once {RAMP_MIN_SENDS_DOWN} have gone out since the last change) it steps down one "
    "level. While ramping, its share is a ceiling: custom email over it waits."
)

_IN_CHUNK = 400


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def clean_weights(raw) -> dict[str, float]:
    """``raw`` as {segment: share}, shares adding up to 1; unknown names,
    zero and anything that isn't a number are dropped. {} when nothing is
    left (no mix)."""
    out: dict[str, float] = {}
    for key, value in (raw or {}).items() if isinstance(raw, dict) else ():
        name = str(key).strip().lower()
        try:
            weight = float(value)
        except (TypeError, ValueError):
            continue
        if name in MIX_SEGMENTS and weight > 0 and weight == weight:
            out[name] = weight
    total = sum(out.values())
    return {k: v / total for k, v in out.items()} if total > 0 else {}


def weights_of(config, learned_raw: str = "") -> dict[str, float]:
    """The mix in force: an approved segment-mix lesson's (``learned_raw``,
    the LEARNED_KEY setting's JSON) when there is one, else
    channels.email.segment_weights."""
    if learned_raw:
        try:
            learned = clean_weights(json.loads(learned_raw))
        except (TypeError, ValueError):
            learned = {}
        if learned:
            return learned
    email = getattr(getattr(config, "channels", None), "email", None)
    return clean_weights(getattr(email, "segment_weights", None) or {})


def ramp_levels(target: float) -> list[float]:
    """The shares the ramped segment steps through, ending at its target."""
    return [s for s in RAMP_STEPS if s < target - 1e-9] + [target]


def effective_shares(weights: dict[str, float], level: int) -> dict[str, float]:
    """Each weighted segment's share today: the ramped segment at its ramp
    level, the others scaled up to fill what it doesn't use yet."""
    if RAMP_SEGMENT not in weights:
        return dict(weights)
    target = weights[RAMP_SEGMENT]
    levels = ramp_levels(target)
    share = levels[max(0, min(int(level or 0), len(levels) - 1))]
    rest = 1.0 - target
    out = {}
    for segment, weight in weights.items():
        if segment == RAMP_SEGMENT:
            out[segment] = share
        else:
            out[segment] = weight * (1.0 - share) / rest if rest > 1e-9 else 0.0
    return out


def read_ramp(raw) -> dict:
    """The stored ramp: {"level", "since", "history"} (level 0 when unset)."""
    try:
        data = json.loads(raw) if raw else {}
    except (TypeError, ValueError):
        data = {}
    if not isinstance(data, dict):
        data = {}
    try:
        level = max(0, int(data.get("level") or 0))
    except (TypeError, ValueError):
        level = 0
    history = [h for h in data.get("history") or [] if isinstance(h, dict)]
    return {"level": level, "since": str(data.get("since") or ""), "history": history[-RAMP_HISTORY:]}


def ramp_view(weights: dict[str, float], ramp: dict) -> dict | None:
    """The ramp as the Mailboxes tab shows it, or None without a custom weight."""
    if RAMP_SEGMENT not in weights:
        return None
    levels = ramp_levels(weights[RAMP_SEGMENT])
    level = max(0, min(int(ramp.get("level") or 0), len(levels) - 1))
    return {
        "segment": RAMP_SEGMENT, "level": level, "levels": levels,
        "share": levels[level], "target": weights[RAMP_SEGMENT],
        "ramping": level < len(levels) - 1, "since": ramp.get("since") or None,
        "history": list(reversed(ramp.get("history") or []))[:10], "rule": RAMP_RULE,
    }


def bounced_row_sql(alias: str = "a") -> str:
    """SQL: the outbox row a bounce action names (outbox_id), read only from
    details that are JSON."""
    return (f"(CASE WHEN json_valid({alias}.details_json) "
            f"THEN json_extract({alias}.details_json, '$.outbox_id') END)")


async def segment_window(db_path: str, segment: str, since: str) -> tuple[int, int]:
    """(sent, bounced) for ``segment``'s cold email sent since ``since``:
    the sequence emails stamped with it, and the bounces naming one of them."""
    async with aiosqlite.connect(db_path, timeout=30) as db:
        async with db.execute(
                "SELECT COUNT(*) FROM outbox WHERE kind = 'sequence' AND status = 'sent' "
                "AND segment = ? AND sent_at >= ?", (segment, since)) as cursor:
            sent = (await cursor.fetchone())[0] or 0
        async with db.execute(
                "SELECT COUNT(DISTINCT o.id) FROM actions a JOIN outbox o "
                f"ON o.id = {bounced_row_sql()} WHERE a.action_type = 'bounce' "
                "AND o.kind = 'sequence' AND o.segment = ? AND o.sent_at >= ?",
                (segment, since)) as cursor:
            bounced = (await cursor.fetchone())[0] or 0
    return int(sent), int(bounced)


async def update_ramp(state, weights: dict[str, float], now: datetime | None = None) -> dict:
    """Step the ramp up or down if its rule says so, store it, and return it
    ({"level", "since", "history"}). Nothing to do without a custom weight."""
    raw = await state.get_setting(RAMP_KEY)
    ramp = read_ramp(raw)
    if RAMP_SEGMENT not in weights:
        return ramp
    now = now or _utcnow()
    levels = ramp_levels(weights[RAMP_SEGMENT])
    top = len(levels) - 1
    level = min(ramp["level"], top)
    try:
        since = datetime.fromisoformat(ramp["since"]) if ramp["since"] else None
    except ValueError:
        since = None
    if since is None or since > now:
        ramp.update(level=level, since=now.isoformat())
        await state.set_setting(RAMP_KEY, json.dumps(ramp))
        return ramp

    window_start = max(since, now - RAMP_WINDOW)
    sent, bounced = await segment_window(state.db_path, RAMP_SEGMENT, window_start.isoformat())
    rate = bounced / sent if sent else 0.0
    move, why = 0, ""
    if sent >= RAMP_MIN_SENDS_DOWN and rate > RAMP_DOWN_ABOVE:
        move, why = -1, f"bounces {bounced}/{sent} ({rate:.1%}) over {RAMP_DOWN_ABOVE:.0%}"
    elif (level < top and now - since >= RAMP_WINDOW and sent >= RAMP_MIN_SENDS
          and rate < RAMP_UP_BELOW):
        move, why = 1, f"bounces {bounced}/{sent} ({rate:.1%}) under {RAMP_UP_BELOW:.0%} for 7 days"
    if move == 0 and level == ramp["level"]:
        return ramp
    new = max(0, min(top, level + move))
    ramp["history"] = (ramp["history"] + [{
        "at": now.isoformat(), "from": levels[level], "to": levels[new],
        "sent": sent, "bounced": bounced, "why": why or "the target changed",
    }])[-RAMP_HISTORY:]
    ramp.update(level=new, since=now.isoformat())
    await state.set_setting(RAMP_KEY, json.dumps(ramp))
    logger.info(f"Segment ramp: custom {levels[level]:.0%} -> {levels[new]:.0%} ({ramp['history'][-1]['why']}).")
    return ramp


async def first_emails_since(db_path: str, since: str) -> dict[str, int]:
    """First emails (sequence step 1) sent since ``since``, by stamped segment."""
    async with aiosqlite.connect(db_path, timeout=30) as db:
        async with db.execute(
                "SELECT COALESCE(NULLIF(segment, ''), 'unknown'), COUNT(*) FROM outbox "
                "WHERE kind = 'sequence' AND step = 1 AND status = 'sent' "
                "AND sent_at >= ? GROUP BY 1", (since,)) as cursor:
            return {str(seg): int(n) for seg, n in await cursor.fetchall()}


def is_first_email(row: dict) -> bool:
    return row.get("kind") == "sequence" and int(row.get("step") or 0) == 1


async def queued_segments(db_path: str, rows: list[dict]) -> dict[str, str]:
    """Each first email's segment, by outbox id: its own column, else what
    its company's latest profile says (read for all of them at once)."""
    out: dict[str, str] = {}
    need: list[dict] = []
    for row in rows:
        if not is_first_email(row):
            continue
        if (row.get("segment") or "").strip():
            out[row["id"]] = row["segment"].strip()
        else:
            need.append(row)
    if not need:
        return out
    pids = sorted({r.get("prospect_id") or "" for r in need} - {""})
    prospects: dict[str, tuple[str, str]] = {}
    companies: dict[str, tuple[list, str]] = {}
    facts: dict[str, dict[str, tuple]] = {}
    async with aiosqlite.connect(db_path, timeout=30) as db:
        for i in range(0, len(pids), _IN_CHUNK):
            chunk = pids[i:i + _IN_CHUNK]
            marks = ", ".join("?" for _ in chunk)
            async with db.execute(
                    f"SELECT id, COALESCE(company_id, ''), COALESCE(industry, '') FROM prospects "
                    f"WHERE id IN ({marks})", chunk) as cursor:
                for pid, cid, industry in await cursor.fetchall():
                    prospects[pid] = (cid, industry)
        cids = sorted({cid for cid, _ in prospects.values()} - {""})
        for i in range(0, len(cids), _IN_CHUNK):
            chunk = cids[i:i + _IN_CHUNK]
            marks = ", ".join("?" for _ in chunk)
            async with db.execute(
                    f"SELECT id, COALESCE(tech_stack_json, '[]'), COALESCE(industry, '') "
                    f"FROM companies WHERE id IN ({marks})", chunk) as cursor:
                for cid, stack_json, industry in await cursor.fetchall():
                    try:
                        stack = json.loads(stack_json or "[]")
                    except (TypeError, ValueError):
                        stack = []
                    companies[cid] = (stack if isinstance(stack, list) else [], industry)
            # Oldest first, so the newest reading of each signal is kept.
            async with db.execute(
                    "SELECT company_id, signal_code, value_text, value_num FROM observations "
                    "WHERE signal_code IN ('SITE_PLATFORM', 'LEGACY_SITE_SCORE') "
                    f"AND company_id IN ({marks}) ORDER BY observed_at ASC, rowid ASC",
                    chunk) as cursor:
                for cid, code, text, num in await cursor.fetchall():
                    facts.setdefault(cid, {})[code] = (text, num)
    for row in need:
        cid, p_industry = prospects.get(row.get("prospect_id") or "", ("", ""))
        stack, c_industry = companies.get(cid, ([], ""))
        seen = facts.get(cid, {})
        platform = str((seen.get("SITE_PLATFORM") or ("", None))[0] or "")
        score = (seen.get("LEGACY_SITE_SCORE") or ("", None))[1]
        industry = " ".join(i for i in (c_industry, p_industry) if i)
        try:
            out[row["id"]] = segment_from_facts(platform, score, stack, industry)
        except Exception:
            out[row["id"]] = "unknown"
    return out


class SegmentMix:
    """Today's mix: the shares to aim for, what has gone out, and the
    ceiling a ramping segment may not pass."""

    def __init__(self, shares: dict[str, float], counts: dict[str, int] | None = None,
                 ceilings: dict[str, float] | None = None):
        self.shares = dict(shares)
        self.counts = {k: int(v) for k, v in (counts or {}).items()}
        self.ceilings = dict(ceilings or {})
        self.segments: dict[str, str] = {}   # outbox id -> segment
        self.held = 0                        # first emails the ceiling held this cycle

    def segment(self, row: dict) -> str:
        return self.segments.get(row.get("id") or "", "unknown")

    def _fits(self, segment: str, counts: dict[str, int], total: int) -> bool:
        cap = self.ceilings.get(segment)
        return cap is None or counts.get(segment, 0) + 1 <= max(1.0, cap * (total + 1)) + 1e-9

    def _pick(self, available: list[str], counts: dict[str, int], total: int) -> str | None:
        weighted = [s for s in available if self.shares.get(s, 0) > 0 and self._fits(s, counts, total)]
        if not weighted:
            return None
        # The furthest below its share once this email is counted; a tie
        # goes to the larger share, then to the order the queue gave.
        return max(weighted, key=lambda s: (self.shares[s] * (total + 1) - counts.get(s, 0),
                                            self.shares[s], -available.index(s)))

    def order(self, queues: list[list[dict]], limits: list[int] | None = None) -> list[list[dict]]:
        """Each queue (a mailbox's due rows, oldest first) reordered: its
        rows that aren't first emails first, then its first emails by the
        mix, chosen in the turn order _in_turn takes them in so the mailboxes
        share one count. ``limits``: how many each mailbox can send this
        cycle; a pick past it doesn't count towards the mix. Rows are only
        moved, never added or dropped."""
        priority = [[r for r in q if not is_first_email(r)] for q in queues]
        places = [{id(r): n for n, r in enumerate(q)} for q in queues]
        firsts: list[dict[str, list[dict]]] = []
        for q in queues:
            by_segment: dict[str, list[dict]] = {}
            for r in q:
                if is_first_email(r):
                    by_segment.setdefault(self.segment(r), []).append(r)
            firsts.append(by_segment)
        out = [list(p) for p in priority]
        tails: list[list[dict]] = [[] for _ in queues]
        counts = dict(self.counts)
        total = sum(counts.values())
        position = 0
        while any(any(rows for rows in lane.values()) for lane in firsts):
            for i, lane in enumerate(firsts):
                if position < len(priority[i]):
                    continue        # this turn of the mailbox is a follow-up or reply
                available = [s for s, rows in lane.items() if rows]
                if not available:
                    continue
                segment = self._pick(available, counts, total)
                if segment is None:
                    # No weighted segment left that fits: the unweighted
                    # ones in queue order, then whatever the ceiling holds.
                    loose = [r for s in available if self.shares.get(s, 0) <= 0 for r in lane[s]]
                    if loose:
                        segment = self.segment(min(loose, key=lambda r: places[i][id(r)]))
                    else:
                        for s in available:
                            tails[i] += lane[s]
                            lane[s] = []
                        continue
                row = lane[segment].pop(0)
                out[i].append(row)
                if limits is None or len(out[i]) <= limits[i]:
                    counts[segment] = counts.get(segment, 0) + 1
                    total += 1
            position += 1
        return [o + t for o, t in zip(out, tails)]

    def allows(self, row: dict) -> bool:
        """Whether this first email may go now under its segment's ceiling,
        counting what this cycle has sent. Anything else always may."""
        if not is_first_email(row):
            return True
        return self._fits(self.segment(row), self.counts, sum(self.counts.values()))

    def record(self, row: dict) -> None:
        if is_first_email(row):
            segment = self.segment(row)
            self.counts[segment] = self.counts.get(segment, 0) + 1


async def build_mix(state, weights: dict[str, float], level: int, since: str) -> SegmentMix:
    """Today's SegmentMix: ``weights`` at ramp ``level``, with the first
    emails sent since ``since`` (local midnight) already counted."""
    shares = effective_shares(weights, level)
    ceilings = {}
    if RAMP_SEGMENT in weights:
        levels = ramp_levels(weights[RAMP_SEGMENT])
        if min(int(level or 0), len(levels) - 1) < len(levels) - 1:
            ceilings[RAMP_SEGMENT] = shares[RAMP_SEGMENT]
    return SegmentMix(shares, await first_emails_since(state.db_path, since), ceilings)
