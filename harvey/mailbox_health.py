"""Sending health and results, per mailbox and per sending domain.

Everything the dashboard's Mailboxes tab shows comes from here. It only
reads: the database (outbox, actions, prospects, conversations, settings),
the config, each mailbox's stored sign-in, and public DNS. Nothing here
writes a row or changes a setting.

Where each number comes from:

- Sends: outbox rows with status 'sent', by the mailbox they went out from
  (a row's mailbox "" is the primary's, as everywhere else).
- Bounces: the Handler's 'bounce' actions. With several mailboxes each one
  names the mailbox it counted against; with one mailbox none does, and they
  are all the primary's. "Since last resume" is the counter the bounce-rate
  switch reads (per mailbox with several, the global one with one), which a
  resume resets to 0.
- Replies and opt-outs: the people a mailbox emailed (a cold email sent from
  it) and where each of them stands now, by prospect status. "Replied" is
  anyone who wrote back, a no and an opt-out included.
- The results funnel: the same prospect statuses and conversation buckets
  the Today tab uses (dashboard._pipeline_bucket).

The health score is Harvey's own estimate from bounces, DNS and blocklists
(``health_score``). It is not an inbox-placement score: only a warmup or
seed-test tool can measure that, and Harvey can't read it.
"""

import asyncio
import time
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

import aiosqlite

from harvey import deliverability as d
from harvey import mailboxes as mb
from harvey.state import AUTO_CONVERSATION

# The global bounce counter (agents/handler.BOUNCE_COUNT_KEY): with one
# mailbox it is that mailbox's "since last resume".
GLOBAL_BOUNCE_KEY = "bounce_count"

# Prospect statuses, read the way the Today tab reads them. Contacted is
# dashboard.SENT_STATUSES; wrote back is everyone a reply moved on from
# "contacted" (dashboard.REPLY_STATUSES plus a no and an opt-out).
CONTACTED_STATUSES = ("contacted", "replied", "meeting", "closed", "lost", "opted_out")
WROTE_BACK_STATUSES = ("replied", "meeting", "closed", "lost", "opted_out")
# Addresses the Writer may write to (state.WRITABLE_EMAIL_STATUSES).
SENDABLE_EMAIL_STATUSES = ("verified", "risky")
# Waiting to go out: to approve, approved, going out now.
QUEUED_STATUSES = ("pending_review", "approved", "sending")

CHART_DAYS = 14
# DNS answers are cached this long per domain; a re-check sooner than
# MIN_RECHECK_SECONDS after the last one returns the last one, so a held-down
# button can't hammer the blocklists (which rate-limit).
DNS_TTL_SECONDS = 3600
MIN_RECHECK_SECONDS = 60

EVER = "0001-01-01T00:00:00"


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _rate(part: int, whole: int) -> float | None:
    return round(part / whole, 4) if whole else None


def _box(alias: str) -> str:
    """SQL: row ``alias``'s mailbox as an address, "" being the primary.
    Takes one parameter, the primary's address."""
    return (f"(CASE WHEN trim(COALESCE({alias}.mailbox, '')) = '' THEN ? "
            f"ELSE lower(trim({alias}.mailbox)) END)")


# A bounce action's mailbox, "" or missing (one mailbox) being the primary.
# Two parameters, both the primary's address. Nested so json_extract never
# reads a row whose details aren't JSON.
_BOUNCE_BOX = ("(CASE WHEN json_valid(details_json) THEN COALESCE(NULLIF(lower(trim("
               "json_extract(details_json, '$.mailbox'))), ''), ?) ELSE ? END)")
# Older Handlers logged a bounce they couldn't match to any email Harvey had
# sent (a warmup network's notice, say) with total_sent 0. Those weren't
# Harvey's bounces and no longer count anywhere else, so not here either.
_MATCHED_BOUNCE = ("NOT (json_valid(details_json) AND COALESCE("
                   "CAST(json_extract(details_json, '$.total_sent') AS INTEGER), -1) = 0)")


# ── The health score ─────────────────────────────────────────────────────

# Points off, in the order the card lists them. Start at 100; never below 0.
BOUNCE_TIERS = ((0.05, 40), (0.03, 25), (0.02, 10))   # (over this rate, points)
AUTH_POINTS = {d.FAIL: 15, d.WARN: 5}                  # SPF, DKIM, DMARC each
MX_POINTS = 10                                         # no MX record
BLOCKLIST_POINTS = 30                                  # listed anywhere
AGE_POINTS = {d.FAIL: 10, d.WARN: 5}                   # under 30 / 90 days
PAUSED_POINTS = 10                                     # each paused mailbox
GOOD_SCORE, WATCH_SCORE = 85, 60

SCORE_FORMULA = [
    "Start at 100.",
    "Bounce rate over 5%: -40, over 3%: -25, over 2%: -10. The last 7 days "
    f"count once the domain sent {d.MIN_SENDS_FOR_RATES} in them, else all time; "
    f"under {d.MIN_SENDS_FOR_RATES} sends it isn't judged.",
    "SPF, DKIM and DMARC, each: missing or broken -15, weak -5.",
    f"No MX record: -{MX_POINTS}.",
    f"Listed on a domain blocklist: -{BLOCKLIST_POINTS}.",
    "Domain younger than 30 days: -10, younger than 90 days: -5.",
    f"Each paused mailbox on the domain: -{PAUSED_POINTS}.",
    "A check that couldn't run takes nothing off and says so. The score never goes below 0.",
]
SCORE_NOTE = (
    "Harvey's own estimate from bounces, DNS and blocklists. It is not an "
    "inbox-placement score: the one a warmup tool shows (TrulyInbox and the "
    "like) is a different measure, and Harvey can't read it here."
)


def _item(rule: str, result: str, points: int = 0, status: str = d.OK) -> dict:
    return {"rule": rule, "result": result, "points": -abs(points), "status": status}


def _bounce_item(sent_7d: int, bounces_7d: int, sent_all: int, bounces_all: int) -> dict:
    floor = d.MIN_SENDS_FOR_RATES
    if sent_7d >= floor:
        sent, bounces, window = sent_7d, bounces_7d, "last 7 days"
    elif sent_all >= floor:
        sent, bounces, window = sent_all, bounces_all, "all time"
    else:
        return _item("Bounce rate", f"{bounces_all} bounced of {sent_all} sent: too few to judge",
                     0, d.INFO)
    rate = bounces / sent
    points = next((p for limit, p in BOUNCE_TIERS if rate > limit), 0)
    status = d.OK if not points else d.WARN if points <= 10 else d.FAIL
    return _item("Bounce rate", f"{rate:.1%} ({bounces} of {sent}, {window})", points, status)


def _check_item(rule: str, check: dict | None, points_by_status: dict) -> dict:
    if not check:
        return _item(rule, "not checked yet", 0, d.INFO)
    status = check.get("status", d.INFO)
    if check.get("unknown") or status == d.INFO:
        return _item(rule, "couldn't check: " + str(check.get("detail") or ""), 0, d.INFO)
    word = {d.OK: "ok", d.WARN: "weak", d.FAIL: "missing or broken"}.get(status, status)
    if rule == "Domain age":
        word = str(check.get("detail") or "").split(" (")[0] or word
    return _item(rule, word, points_by_status.get(status, 0), status)


def health_score(checks: dict | None, *, sent_7d: int = 0, bounces_7d: int = 0,
                 sent_all: int = 0, bounces_all: int = 0, paused: int = 0) -> dict:
    """A sending domain's health, 0-100, and every rule's share of it.

    ``checks``: run_domain_checks' result, or None while DNS hasn't been
    read (then only bounces and pauses count, and ``partial`` says so).
    Deterministic: the same numbers and checks always give the same score.
    """
    items = [_bounce_item(sent_7d, bounces_7d, sent_all, bounces_all)]
    if checks is not None:
        for key, rule in (("spf", "SPF"), ("dkim", "DKIM"), ("dmarc", "DMARC")):
            items.append(_check_item(rule, checks.get(key), AUTH_POINTS))
        items.append(_check_item("MX", checks.get("mx"), {d.FAIL: MX_POINTS}))
        lists = checks.get("blocklists") or []
        listed = [c for c in lists if c.get("status") == d.FAIL]
        unchecked = [c for c in lists if c.get("status") != d.FAIL and c.get("status") != d.OK]
        if listed:
            names = ", ".join(c["name"].replace("Blocklist ", "") for c in listed)
            items.append(_item("Blocklists", f"listed on {names}", BLOCKLIST_POINTS, d.FAIL))
        elif lists and len(unchecked) == len(lists):
            items.append(_item("Blocklists", "couldn't check any list", 0, d.INFO))
        elif lists:
            note = f" ({len(unchecked)} couldn't be checked)" if unchecked else ""
            items.append(_item("Blocklists", f"not listed{note}", 0, d.OK))
        else:
            items.append(_item("Blocklists", "not checked", 0, d.INFO))
        items.append(_check_item("Domain age", checks.get("age"), AGE_POINTS))
    items.append(_item("Paused mailboxes", f"{paused} paused" if paused else "none",
                       PAUSED_POINTS * paused, d.FAIL if paused else d.OK))
    score = max(0, 100 + sum(i["points"] for i in items))
    grade = "good" if score >= GOOD_SCORE else "watch" if score >= WATCH_SCORE else "risk"
    return {
        "score": score,
        "grade": grade,
        "label": {"good": "Healthy", "watch": "Watch", "risk": "At risk"}[grade],
        "items": items,
        "partial": checks is None,
    }


# ── DNS checks, cached per domain ────────────────────────────────────────

def _safe(name: str, check, *args):
    try:
        return check(*args)
    except Exception as e:   # a check that blew up is unknown, never a verdict
        return d.Check(name, d.INFO, f"Couldn't run this check ({type(e).__name__}).", unknown=True)


def _as_dict(check) -> dict:
    return asdict(check) if not isinstance(check, dict) else check


async def run_domain_checks(domain: str, selector: str = "") -> dict:
    """SPF, DKIM, DMARC, MX, domain age and the domain blocklists for
    ``domain``, each in its own thread (they are blocking lookups)."""
    if not d.DOMAIN_RE.match(domain or ""):
        bad = d.Check("Domain", d.INFO, f"'{domain}' isn't a domain Harvey can look up.", unknown=True)
        return {"mx": asdict(bad), "spf": asdict(bad), "dkim": asdict(bad), "dmarc": asdict(bad),
                "age": asdict(bad), "blocklists": [], "checked_at": _utcnow().isoformat() + "Z"}
    mx, spf, dkim, dmarc, age, lists = await asyncio.gather(
        asyncio.to_thread(_safe, "MX", d.check_mx, domain),
        asyncio.to_thread(_safe, "SPF", d.check_spf, domain),
        asyncio.to_thread(_safe, "DKIM", d.check_dkim, domain, selector),
        asyncio.to_thread(_safe, "DMARC", d.check_dmarc, domain),
        asyncio.to_thread(_safe, "Domain age", d.check_domain_age, domain),
        asyncio.to_thread(_safe, "Blocklists", d.check_domain_blocklists, domain),
    )
    if not isinstance(lists, list):
        lists = [lists]
    return {
        "mx": _as_dict(mx), "spf": _as_dict(spf), "dkim": _as_dict(dkim),
        "dmarc": _as_dict(dmarc), "age": _as_dict(age),
        "blocklists": [_as_dict(c) for c in lists],
        "checked_at": _utcnow().isoformat() + "Z",
    }


class DomainChecks:
    """run_domain_checks, remembered for ``ttl`` seconds per (domain,
    selector). Two requests for the same domain at once share one lookup."""

    def __init__(self, ttl: float = DNS_TTL_SECONDS, run=None, clock=time.monotonic):
        self.ttl = ttl
        self.run = run or run_domain_checks
        self.clock = clock
        self._done: dict[tuple, tuple[float, dict]] = {}
        self._running: dict[tuple, asyncio.Task] = {}

    def clear(self):
        self._done.clear()
        self._running.clear()

    def cached(self, domain: str, selector: str = "") -> dict | None:
        """The remembered result while it is fresh, else None. No lookups."""
        hit = self._done.get((domain, selector))
        if hit and self.clock() - hit[0] < self.ttl:
            return hit[1]
        return None

    async def get(self, domain: str, selector: str = "", refresh: bool = False) -> dict:
        key = (domain, selector)
        hit = self._done.get(key)
        age = self.clock() - hit[0] if hit else None
        if hit and (age < MIN_RECHECK_SECONDS or (not refresh and age < self.ttl)):
            return hit[1]
        task = self._running.get(key)
        loop = asyncio.get_running_loop()
        if task is None or task.done() or task.get_loop() is not loop:
            task = loop.create_task(self.run(domain, selector))
            self._running[key] = task
        try:
            result = await task
        finally:
            if self._running.get(key) is task and task.done():
                self._running.pop(key, None)
        self._done[key] = (self.clock(), result)
        return result


# The dashboard's: one per process.
CHECKS = DomainChecks()


# ── Connection ───────────────────────────────────────────────────────────

def connection_of(config, mailbox: mb.Mailbox, env=None) -> tuple[bool | None, str]:
    """(connected, why not) for ``mailbox`` without any network call:
    None when Harvey doesn't send from it itself (Instantly)."""
    provider = (getattr(config.channels.email, "provider", "") or "").strip().lower()
    if provider == "gmail":
        if mb.is_connected(mailbox):
            return True, ""
        command = "harvey gmail auth" + (f" --mailbox {mailbox.email}" if mb.is_multi(config) else "")
        return False, f"Not signed in. Run '{command}' in your terminal."
    if provider in ("smtp", "microsoft"):
        try:
            from harvey.config import load_env
            from harvey.integrations.mail_provider import get_mail_provider
            built = get_mail_provider(config, env if env is not None else load_env())
            if built is not None and built.is_configured():
                return True, ""
        except Exception:
            return None, "Couldn't tell whether it is connected."
        how = "harvey microsoft auth" if provider == "microsoft" else "the SMTP settings"
        return False, f"Not set up yet: see Settings ({how})."
    return None, "Instantly sends from its own servers, so Harvey doesn't track this mailbox."


# ── Reading the database ─────────────────────────────────────────────────

async def _rows(db, sql: str, params=()) -> list[dict]:
    async with db.execute(sql, tuple(params)) as cursor:
        return [dict(r) for r in await cursor.fetchall()]


async def _settings(db, keys: list[str]) -> dict[str, str]:
    if not keys:
        return {}
    marks = ", ".join("?" for _ in keys)
    rows = await _rows(db, f"SELECT key, value FROM settings WHERE key IN ({marks})", keys)
    return {r["key"]: r["value"] or "" for r in rows}


def _day_labels(now: datetime, tz_minutes: int, days: int) -> list[str]:
    today = (now + timedelta(minutes=tz_minutes)).date()
    return [(today - timedelta(days=n)).isoformat() for n in range(days - 1, -1, -1)]


def _local_midnight_utc(now: datetime, tz_minutes: int) -> str:
    local = now + timedelta(minutes=tz_minutes)
    midnight = local.replace(hour=0, minute=0, second=0, microsecond=0)
    return (midnight - timedelta(minutes=tz_minutes)).isoformat()


def _empty_box(address: str) -> dict:
    return {
        "email": address, "sent_today": 0, "sent_24h": 0, "sent_7d": 0, "sent_total": 0,
        "cold_today": 0, "last_sent": None, "bounces_total": 0, "bounces_7d": 0,
        "contacted": 0, "replied": 0, "positive": 0, "opted_out": 0,
        "to_approve": 0, "approved": 0,
    }


async def collect(db_path, config, *, tz_minutes: int = 0, now: datetime | None = None,
                  connection=None, days: int = CHART_DAYS) -> dict:
    """Every number the Mailboxes tab shows except the DNS checks.

    ``tz_minutes``: the viewer's offset east of UTC, so "today" and the chart's
    days are theirs (stored times are UTC). ``connection(mailbox)`` returns
    (connected, why not); connection_of by default."""
    from harvey.dashboard import _pipeline_bucket

    now = now or _utcnow()
    tz_minutes = max(-840, min(840, int(tz_minutes or 0)))
    shift = f"{tz_minutes:+d} minutes"
    labels = _day_labels(now, tz_minutes, days)
    midnight = _local_midnight_utc(now, tz_minutes)
    day_ago = (now - timedelta(hours=24)).isoformat()
    week_ago = (now - timedelta(days=7)).isoformat()
    connection = connection or (lambda m: connection_of(config, m))

    email = config.channels.email
    provider = (getattr(email, "provider", "") or "").strip().lower()
    multi = mb.is_multi(config)
    configured = mb.configured_mailboxes(config)
    primary = mb.primary_mailbox(config).email

    boxes: dict[str, dict] = {m.email: _empty_box(m.email) for m in configured}
    daily_sent: dict[str, dict[str, int]] = {}
    daily_bounced: dict[str, dict[str, int]] = {}
    funnel_rows: list[dict] = []
    companies = 0
    settings: dict[str, str] = {}

    path = Path(db_path)
    if path.exists():
        async with aiosqlite.connect(str(path)) as db:
            db.row_factory = aiosqlite.Row
            for r in await _rows(db, (
                    f"SELECT {_box('o')} AS box, COUNT(*) AS sent_total, "
                    "SUM(julianday(o.sent_at) >= julianday(?)) AS sent_today, "
                    "SUM(julianday(o.sent_at) >= julianday(?)) AS sent_24h, "
                    "SUM(julianday(o.sent_at) >= julianday(?)) AS sent_7d, "
                    "SUM(o.kind = 'sequence' AND julianday(o.sent_at) >= julianday(?)) AS cold_today, "
                    "MAX(o.sent_at) AS last_sent "
                    "FROM outbox o WHERE o.status = 'sent' GROUP BY box"),
                    (primary, midnight, day_ago, week_ago, midnight)):
                box = boxes.setdefault(r["box"], _empty_box(r["box"]))
                for k in ("sent_total", "sent_today", "sent_24h", "sent_7d", "cold_today"):
                    box[k] = int(r[k] or 0)
                box["last_sent"] = r["last_sent"]

            marks = ", ".join("?" for _ in QUEUED_STATUSES)
            for r in await _rows(db, (
                    f"SELECT {_box('o')} AS box, SUM(o.status = 'pending_review') AS to_approve, "
                    "SUM(o.status IN ('approved', 'sending')) AS approved "
                    f"FROM outbox o WHERE o.status IN ({marks}) GROUP BY box"),
                    (primary, *QUEUED_STATUSES)):
                box = boxes.setdefault(r["box"], _empty_box(r["box"]))
                box["to_approve"] = int(r["to_approve"] or 0)
                box["approved"] = int(r["approved"] or 0)

            for r in await _rows(db, (
                    f"SELECT {_BOUNCE_BOX} AS box, COUNT(*) AS total, "
                    "SUM(julianday(created_at) >= julianday(?)) AS week "
                    f"FROM actions WHERE action_type = 'bounce' AND {_MATCHED_BOUNCE} "
                    "GROUP BY box"),
                    (primary, primary, week_ago)):
                box = boxes.setdefault(r["box"], _empty_box(r["box"]))
                box["bounces_total"] = int(r["total"] or 0)
                box["bounces_7d"] = int(r["week"] or 0)

            # The latest conversation of each prospect: how a reply went. One
            # only an auto-reply is in (status auto) is no reply.
            latest = ("LEFT JOIN (SELECT prospect_id, stage, intent, status, ROW_NUMBER() OVER ("
                      "PARTITION BY prospect_id ORDER BY updated_at DESC, created_at DESC) AS rn "
                      "FROM conversations) l ON l.prospect_id = p.id AND l.rn = 1")
            for r in await _rows(db, (
                    "SELECT x.box, p.status, p.email_status, l.stage, l.intent, "
                    "l.status AS convo_status FROM ("
                    f"SELECT DISTINCT {_box('o')} AS box, o.prospect_id AS pid FROM outbox o "
                    "WHERE o.status = 'sent' AND o.kind = 'sequence' "
                    "AND COALESCE(o.prospect_id, '') != '') x "
                    f"JOIN prospects p ON p.id = x.pid {latest}"), (primary,)):
                box = boxes.setdefault(r["box"], _empty_box(r["box"]))
                status = r["status"] or "new"
                box["contacted"] += 1
                box["replied"] += _wrote_back(status, r["convo_status"])
                box["opted_out"] += status == "opted_out"
                bucket = _pipeline_bucket(status, r["stage"], r["intent"], r["email_status"])
                box["positive"] += bucket in ("interested", "ready_to_meet")

            for r in await _rows(db, (
                    f"SELECT {_box('o')} AS box, date(o.sent_at, ?) AS day, COUNT(*) AS n "
                    "FROM outbox o WHERE o.status = 'sent' AND date(o.sent_at, ?) >= ? "
                    "GROUP BY box, day"), (primary, shift, shift, labels[0])):
                daily_sent.setdefault(r["box"], {})[r["day"]] = int(r["n"] or 0)
            for r in await _rows(db, (
                    f"SELECT {_BOUNCE_BOX} AS box, date(created_at, ?) AS day, COUNT(*) AS n "
                    f"FROM actions WHERE action_type = 'bounce' AND {_MATCHED_BOUNCE} "
                    "AND date(created_at, ?) >= ? "
                    "GROUP BY box, day"), (primary, primary, shift, shift, labels[0])):
                daily_bounced.setdefault(r["box"], {})[r["day"]] = int(r["n"] or 0)

            companies = int((await _rows(db, "SELECT COUNT(*) AS n FROM companies"))[0]["n"] or 0)
            funnel_rows = await _rows(db, (
                "SELECT p.status, p.email_status, COALESCE(p.email, '') != '' AS has_email, "
                f"l.stage, l.intent, l.status AS convo_status, COUNT(*) AS n FROM prospects p "
                f"{latest} GROUP BY p.status, p.email_status, has_email, l.stage, l.intent, "
                "l.status"))

            keys = [mb.GLOBAL_PAUSE_KEY, GLOBAL_BOUNCE_KEY]
            for m in configured:
                keys += [mb.paused_key(m.email), mb.bounce_key(m.email),
                         mb.read_ok_key(m.email), mb.down_since_key(m.email)]
            settings = await _settings(db, keys)

    global_pause = settings.get(mb.GLOBAL_PAUSE_KEY, "")
    warmup = getattr(email, "warmup", None)
    mailboxes = []
    for m in configured:
        box = boxes[m.email]
        connected, why = connection(m)
        own_pause = settings.get(mb.paused_key(m.email), "") if multi else ""
        down = settings.get(mb.down_since_key(m.email), "")
        if global_pause:
            pause, scope = global_pause, "all"
        elif own_pause:
            pause, scope = own_pause, "mailbox"
        else:
            pause, scope = "", ""
        if connected is None:
            status, reason = "untracked", why
        elif not connected:
            status, reason = "not_connected", why
        elif pause:
            status, reason = "paused", ("All sending is paused: " if scope == "all" else "") + pause
        elif down:
            status, reason = "failing", f"Failing since {down[:16].replace('T', ' ')} UTC."
        else:
            status, reason = "sending", ""
        since_resume = settings.get(mb.bounce_key(m.email) if multi else GLOBAL_BOUNCE_KEY, "0")
        try:
            since_resume = int(since_resume or 0)
        except ValueError:
            since_resume = 0
        cold_cap = None
        if warmup is not None and getattr(warmup, "start_date", ""):
            try:
                cold_cap = warmup.daily_cap(m.daily_limit)
            except Exception:
                cold_cap = None
        mailboxes.append({
            **box,
            "name": m.name, "domain": m.domain, "is_primary": m.is_primary, "configured": True,
            "daily_limit": m.daily_limit, "cold_cap_today": cold_cap,
            "status": status, "reason": reason, "connected": connected,
            "paused": pause, "pause_scope": scope, "down_since": down or None,
            "last_inbox_read": settings.get(mb.read_ok_key(m.email)) or None,
            "bounces_since_resume": since_resume,
            "bounce_rate": _rate(box["bounces_total"], box["sent_total"]),
            "bounce_rate_7d": _rate(box["bounces_7d"], box["sent_7d"]),
            "reply_rate": _rate(box["replied"], box["contacted"]),
            "queued": box["to_approve"] + box["approved"],
        })
    # Mail from an address that isn't in the config any more still counts in
    # the totals, so it gets a row of its own rather than vanishing.
    for address, box in boxes.items():
        if address in {m.email for m in configured}:
            continue
        if not (box["sent_total"] or box["bounces_total"] or box["to_approve"] or box["approved"]):
            continue
        mailboxes.append({
            **box, "name": "", "domain": address.rpartition("@")[2], "is_primary": False,
            "configured": False, "daily_limit": 0, "cold_cap_today": None,
            "status": "not_configured",
            "reason": "Not in channels.email.mailboxes any more; its history still counts.",
            "connected": None, "paused": "", "pause_scope": "", "down_since": None,
            "last_inbox_read": None, "bounces_since_resume": 0,
            "bounce_rate": _rate(box["bounces_total"], box["sent_total"]),
            "bounce_rate_7d": _rate(box["bounces_7d"], box["sent_7d"]),
            "reply_rate": _rate(box["replied"], box["contacted"]),
            "queued": box["to_approve"] + box["approved"],
        })

    def total(key: str, rows=mailboxes) -> int:
        return sum(int(r.get(key) or 0) for r in rows)

    configured_rows = [r for r in mailboxes if r["configured"]]
    by_status: dict[str, int] = {}
    for r in configured_rows:
        by_status[r["status"]] = by_status.get(r["status"], 0) + 1
    summary = {
        "sent_today": total("sent_today"),
        "capacity": sum(m.daily_limit for m in configured),
        "sent_7d": total("sent_7d"),
        "sent_total": total("sent_total"),
        "bounces_7d": total("bounces_7d"),
        "bounces_total": total("bounces_total"),
        "bounce_rate_7d": _rate(total("bounces_7d"), total("sent_7d")),
        "bounce_rate": _rate(total("bounces_total"), total("sent_total")),
        "contacted": total("contacted"),
        "replied": total("replied"),
        "positive": total("positive"),
        "opted_out": total("opted_out"),
        "reply_rate": _rate(total("replied"), total("contacted")),
        "mailboxes": len(configured_rows),
        "sending": by_status.get("sending", 0),
        "paused": by_status.get("paused", 0),
        "not_connected": by_status.get("not_connected", 0),
        "failing": by_status.get("failing", 0),
        "queued": total("queued"),
        "to_approve": total("to_approve"),
        "approved": total("approved"),
        "global_pause": global_pause,
    }

    domains: dict[str, dict] = {}
    for r in configured_rows:
        dom = domains.setdefault(r["domain"], {
            "domain": r["domain"], "mailboxes": [], "paused": 0, "capacity": 0,
            **{k: 0 for k in ("sent_today", "sent_7d", "sent_total", "bounces_7d",
                              "bounces_total", "contacted", "replied", "positive", "opted_out")},
        })
        dom["mailboxes"].append(r["email"])
        dom["paused"] += r["status"] == "paused"
        dom["capacity"] += r["daily_limit"]
        for k in ("sent_today", "sent_7d", "sent_total", "bounces_7d", "bounces_total",
                  "contacted", "replied", "positive", "opted_out"):
            dom[k] += int(r.get(k) or 0)
    for dom in domains.values():
        dom["bounce_rate"] = _rate(dom["bounces_total"], dom["sent_total"])
        dom["bounce_rate_7d"] = _rate(dom["bounces_7d"], dom["sent_7d"])
        dom["reply_rate"] = _rate(dom["replied"], dom["contacted"])

    series = []
    for r in mailboxes:
        sent = daily_sent.get(r["email"], {})
        bounced = daily_bounced.get(r["email"], {})
        series.append({"email": r["email"], "configured": r["configured"],
                       "sent": [sent.get(day, 0) for day in labels],
                       "bounces": [bounced.get(day, 0) for day in labels]})
    daily = {
        "days": labels,
        "series": series,
        "sent": [sum(s.get(day, 0) for s in daily_sent.values()) for day in labels],
        "bounces": [sum(b.get(day, 0) for b in daily_bounced.values()) for day in labels],
    }

    return {
        "provider": provider,
        "tracks_sends": provider != "instantly",
        "multi": multi,
        "primary": primary,
        "generated_at": now.isoformat() + "Z",
        "summary": summary,
        "mailboxes": mailboxes,
        "domains": list(domains.values()),
        "daily": daily,
        "funnel": _funnel(companies, funnel_rows, _pipeline_bucket),
        "score_formula": SCORE_FORMULA,
        "score_note": SCORE_NOTE,
    }


def _wrote_back(status: str, convo_status) -> bool:
    """A person answered: their status says so, and their latest conversation
    isn't one only an auto-reply is in (a company's "we take no sales email"
    makes them 'lost' without anyone replying)."""
    return status in WROTE_BACK_STATUSES and convo_status != AUTO_CONVERSATION


def _funnel(companies: int, rows: list[dict], bucket) -> dict:
    """The pipeline from businesses found to meetings, counted the way the
    Today tab counts it (prospect status, then the latest conversation)."""
    out = {"companies": companies, "contacts": 0, "sendable": 0, "contacted": 0,
           "replied": 0, "positive": 0, "ready_to_meet": 0, "not_interested": 0,
           "opted_out": 0}
    for r in rows:
        n = int(r.get("n") or 0)
        status = r.get("status") or "new"
        out["contacts"] += n
        contacted = status in CONTACTED_STATUSES
        # Anyone already emailed had a sendable address then, even if it has
        # bounced since (a bounce marks it invalid): the funnel never narrows
        # backwards.
        if r.get("has_email") and ((r.get("email_status") or "") in SENDABLE_EMAIL_STATUSES
                                   or contacted):
            out["sendable"] += n
        out["contacted"] += n if contacted else 0
        out["replied"] += n if _wrote_back(status, r.get("convo_status")) else 0
        out["not_interested"] += n if status == "lost" else 0
        out["opted_out"] += n if status == "opted_out" else 0
        stage = bucket(status, r.get("stage"), r.get("intent"), r.get("email_status"))
        out["positive"] += n if stage in ("interested", "ready_to_meet") else 0
        out["ready_to_meet"] += n if stage == "ready_to_meet" else 0
    out["reply_rate"] = _rate(out["replied"], out["contacted"])
    out["positive_rate"] = _rate(out["positive"], out["contacted"])
    out["opt_out_rate"] = _rate(out["opted_out"], out["contacted"])
    return out


# ── Putting DNS and numbers together ─────────────────────────────────────

def dkim_selector(config) -> str:
    """channels.email.warmup.dkim_selector, or "" to try the common ones
    (Google Workspace's, 'google', first)."""
    warmup = getattr(config.channels.email, "warmup", None)
    return (getattr(warmup, "dkim_selector", "") or "").strip()


async def add_domain_checks(report: dict, config, *, wait: bool = False, refresh: bool = False,
                            checks: DomainChecks | None = None) -> dict:
    """Each domain card in ``report`` gets its DNS checks and health score.

    ``wait``: look up what isn't cached (all domains at once, off the event
    loop). Without it only cached answers are used and the rest say
    ``checking``, so the page never waits on DNS. ``refresh``: look again
    even if cached (at most once a minute)."""
    checks = checks or CHECKS
    selector = dkim_selector(config)
    domains = [dom for dom in report.get("domains", []) if dom.get("domain")]

    async def look_up(domain: str) -> dict | None:
        try:
            return await checks.get(domain, selector, refresh=refresh)
        except Exception:   # numbers still show; the card says it isn't checked
            return None

    if wait:
        found = await asyncio.gather(*(look_up(dom["domain"]) for dom in domains))
    else:
        found = [checks.cached(dom["domain"], selector) for dom in domains]
    for dom, result in zip(domains, found):
        dom["checks"] = result
        # Not looked up yet (the page then asks again with wait), or the
        # lookup itself failed (it doesn't: the card says so instead).
        dom["checking"] = result is None and not wait
        dom["check_failed"] = result is None and wait
        dom["checked_at"] = (result or {}).get("checked_at")
        dom["health"] = health_score(
            result, sent_7d=dom["sent_7d"], bounces_7d=dom["bounces_7d"],
            sent_all=dom["sent_total"], bounces_all=dom["bounces_total"], paused=dom["paused"])
    report["dkim_selector"] = selector
    return report
