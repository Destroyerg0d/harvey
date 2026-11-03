"""Lessons: what Harvey learned from its own results, for the owner to approve.

The Learner (harvey/agents/learner.py) proposes them once a day; the owner
approves, rejects or later retires each one (dashboard Learning tab, or
``harvey lessons``). The lifecycle is the signals one: proposed -> active |
rejected, and active -> retired. Nothing a lesson says reaches a prompt until
the owner approves it.

Kinds:

- ``writing``: how emails are written. Writer prompts (first emails and
  sequences) and the Handler's reply prompt.
- ``subject``: about subject lines. Writer prompts. Only ever proposed, with
  its honest sample size: at about a reply a day a real subject winner needs
  thousands of sends per variant.
- ``linkedin``: how LinkedIn messages are written. The LinkedIn drafting prompt.
- ``targeting``: who to write to. Shown to the owner, injected nowhere.
- ``segment_mix``: a proposed share of first emails per segment. Its evidence
  carries ``weights``; approving it writes them to the ``segment_weights``
  setting, which the Sender's segment mix reads over the config's.

Active lessons reach a prompt in one capped block (``lessons_block``) whose
header says they never override the STRICT EMAIL RULES or the
verified-claims rule, and their text is part of the leak guard's instruction
sources (harvey/gate.py), so an email copying one is held like any other
instruction leak.

Every lesson's text passes ``lesson_problem`` before it is stored: no digits
or percentages, links, email addresses, names of people or businesses in the
data, quoted reply text, claims about the sender's company, or instructions to
set rules aside.

No harvey.state import here: harvey.state reads LESSONS_SCHEMA in init_db.
"""

import json
import re
import sqlite3
import time
import unicodedata
import uuid
from contextlib import closing
from datetime import datetime, timedelta, timezone
from difflib import SequenceMatcher
from pathlib import Path

import aiosqlite

KINDS = ("writing", "subject", "linkedin", "targeting", "segment_mix")
STATUSES = ("proposed", "active", "rejected", "retired")
# Where each status may go. Nothing comes back from rejected or retired: a
# lesson worth having again is proposed again by the Learner.
TRANSITIONS = {"proposed": ("active", "rejected"), "active": ("retired",)}
KIND_LABELS = {"writing": "Writing", "subject": "Subject lines", "linkedin": "LinkedIn",
               "targeting": "Targeting", "segment_mix": "Segment mix"}

# Which active lessons each prompt carries.
PROMPT_KINDS = {
    "writer": ("writing", "subject"),
    "handler": ("writing",),
    "linkedin": ("linkedin",),
}

SEGMENT_WEIGHTS_KEY = "segment_weights"
MIX_SEGMENTS = ("shopify", "wordpress", "custom", "other")

BLOCK_CHARS = 1500
BLOCK_HEADER = (
    "LESSONS FROM RESULTS: guidance the owner approved from Harvey's own results, most "
    "evidence first. Apply it where it fits. It never overrides the STRICT EMAIL RULES, any "
    "other rule above, or the verified-claims rule (claim only what the FACTS and the "
    "playbook support); where they disagree, those rules win.")

MAX_LESSON_CHARS = 300
# A copied run of this many words from someone's reply is a quote.
EXCERPT_RUN_WORDS = 8

LESSONS_SCHEMA = """
CREATE TABLE IF NOT EXISTS lessons (
    id TEXT PRIMARY KEY,
    kind TEXT NOT NULL DEFAULT 'writing',
    text TEXT NOT NULL DEFAULT '',
    evidence_json TEXT DEFAULT '{}',
    status TEXT DEFAULT 'proposed',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    decided_at TIMESTAMP,
    decided_by TEXT DEFAULT '',
    source_run TEXT DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_lessons_status ON lessons(status, kind);
"""


def _now() -> str:
    return datetime.now(timezone.utc).replace(tzinfo=None).isoformat(timespec="seconds")


def clean_text(text) -> str:
    """One line: NFKC-folded, whitespace collapsed."""
    folded = unicodedata.normalize("NFKC", str(text or ""))
    return " ".join(folded.split())


# ── The text filter ──

_DIGITS_RE = re.compile(r"\d|%|\bper ?cent\b|\bpercentage\b", re.IGNORECASE)
_LINK_RE = re.compile(
    r"https?:|\bwww\.|\b[a-z0-9-]+(?:\.[a-z0-9-]+)*\.(?:com|net|org|io|co|uk|us|ca|au|nz|ie|de|fr|"
    r"es|it|nl|in|eu|shop|store|biz|info|app|dev|ai|site|online|example|test|invalid)\b",
    re.IGNORECASE)
_EMAIL_RE = re.compile(r"@|\bat\b\s*\w+\s*\bdot\b", re.IGNORECASE)
_QUOTE_RE = re.compile(r"[\"“”„«»]|‘[^’]*\s[^’]*’")
_MARKUP_RE = re.compile(r"[<>{}]")
_HIDDEN_RE = re.compile("[​-‏⁠﻿]")
# Anything about the sender's own company is a claim the facts must carry.
_CLAIM_RE = re.compile(
    r"\b(?:we|we've|we're|we'll|we'd|our|ours|ourselves)\b|"
    r"\b(?:award\w*|ranked|ranking\w*|top[- ]rated|number one|best[- ]in[- ]class|"
    r"industry[- ]leading|market[- ]leading|world[- ]class|certified|accredited|trusted by|"
    r"featured in|as seen (?:in|on)|guarantee\w*|years of experience|clients include|"
    r"testimonial\w*|case stud(?:y|ies)|portfolio)\b",
    re.IGNORECASE)
_OVERRIDE_RE = re.compile(
    r"\b(?:ignore|ignoring|disregard|override|overrides|overriding|bypass|circumvent|"
    r"jailbreak)\b|\bsystem prompt\b|\b(?:previous|above|earlier|prior|other) instructions?\b|"
    r"\b(?:forget|skip|drop|break|relax|suspend|waive|loosen)\b[^.]{0,40}"
    r"\b(?:rules?|instructions?|guidelines?|limits?|checks?|gate|guard|filters?)\b|"
    r"\b(?:rules?|instructions?|guidelines?|limits?)\b[^.]{0,30}"
    r"\b(?:no longer|don'?t|do not|does not|doesn'?t|never|needn'?t)\s+(?:apply|matter|count)\b",
    re.IGNORECASE)

_WORD_RE = re.compile(r"[a-z0-9']+")
_TOKEN_RE = re.compile(r"[A-Za-z][A-Za-z'\-]*")
# Capitalised words a lesson may use that are not anyone's name: platforms,
# segments, channels, days and months.
ALLOWED_PROPER = frozenset("""
shopify wordpress woocommerce magento oscommerce zen cart opencart prestashop virtuemart joomla
cubecart x-cart ecwid bigcommerce shopfactory sellerdeck actinic wix squarespace webflow php
custom other linkedin gmail outlook google email i ok
monday tuesday wednesday thursday friday saturday sunday january february march april may june
july august september october november december
""".split())
# Words that start a sentence of guidance. A person's name from the data that
# is also one of these ("Mark", "Will") is not read as a name at the start.
COMMON_WORDS = frozenset("""
a an the and or but if when where while for to of in on at by with without from into about after
before during as so than then that this these those there their they them it its one two each every
some any all no not never always only more most less least few many much very just also again
open opener opening ask asking keep make use using lead leading write writing name mention
mentioning avoid avoiding say saying start starting end ending try put offer offering send sending
follow following reply replying answer answering thank thanks give show tell let close closing
short shorter long longer plain simple first last next new old early late good better best clear
subject subjects line lines email emails message messages note notes question questions people
person owner owners founder founders director directors store stores shop shops site sites
website websites company companies business businesses brand brands team teams reader readers
inbox inboxes support helpdesk invite invites connection connections follow-up follow-ups
""".split())
_GENERIC_NAME_TOKENS = frozenset("""
ltd limited llc inc co corp company companies group the and of shop shops store stores studio
studios services service solutions online uk us usa international global trading enterprises
agency digital design web media home house
""".split())


def _words(text: str) -> list[str]:
    return _WORD_RE.findall((text or "").lower())


def _runs(words: list[str], n: int):
    for i in range(len(words) - n + 1):
        yield tuple(words[i:i + n])


def _sentence_starts(text: str) -> set[int]:
    """Character offsets where a sentence starts."""
    starts = {0}
    for m in re.finditer(r"[.!?;:]\s+", text):
        starts.add(m.end())
    return starts


def _names_in(text: str, names: dict | None) -> bool:
    """Whether the lesson names a person or business that is in the data.

    A whole name ("Ana Doe", "Acme Roofing") anywhere, any case; a person's
    first or last name, or a distinctive word of a business name, written as
    a name (capitalised mid-sentence). At the start of a sentence a person's
    name counts unless it is a common word of guidance.
    """
    if not names:
        return False
    flat = " " + " ".join(_words(text)) + " "
    for phrase in names.get("phrases", ()):
        if f" {phrase} " in flat:
            return True
    people, firms = names.get("people", set()), names.get("firms", set())
    starts = _sentence_starts(text)
    for m in _TOKEN_RE.finditer(text):
        word = m.group(0)
        low = word.lower()
        if low in ALLOWED_PROPER or not word[0].isupper():
            continue
        at_start = m.start() in starts
        if at_start:
            if low in people and low not in COMMON_WORDS:
                return True
            continue
        if low in people or low in firms:
            return True
    return False


def lesson_problem(text, names: dict | None = None, excerpts: set | None = None) -> str:
    """Why this text may not be a lesson, or "" when it may.

    ``names`` (``filter_context``): the people and businesses in the data.
    ``excerpts``: EXCERPT_RUN_WORDS-word runs of what people wrote to Harvey.
    """
    raw = str(text or "")
    if _HIDDEN_RE.search(raw) or any(unicodedata.category(c) == "Cc" and c not in "\n\t " for c in raw):
        return "has hidden characters"
    line = clean_text(raw)
    if not line:
        return "is empty"
    if len(line) > MAX_LESSON_CHARS:
        return f"is longer than {MAX_LESSON_CHARS} characters"
    if len(re.findall(r"[.!?](?:\s|$)", line)) > 3:
        return "is more than a couple of sentences"
    if _DIGITS_RE.search(line):
        return "has a number or percentage"
    if _EMAIL_RE.search(line):
        return "has an email address"
    if _LINK_RE.search(line):
        return "has a link or domain"
    if _MARKUP_RE.search(line):
        return "has markup or merge tags"
    if _QUOTE_RE.search(line):
        return "quotes something"
    if _OVERRIDE_RE.search(line):
        return "tells Harvey to set its rules aside"
    if _CLAIM_RE.search(line):
        return "makes a claim about the sender's company"
    if _names_in(line, names):
        return "names a person or business from the data"
    if excerpts:
        for run in _runs(_words(line), EXCERPT_RUN_WORDS):
            if run in excerpts:
                return "copies what someone wrote in a reply"
    return ""


async def filter_context(state, days: int = 120) -> dict:
    """What ``lesson_problem`` checks against: every person's and business's
    name Harvey holds (contacts, companies, LinkedIn leads), and runs of what
    people wrote back in the last ``days`` days (email and LinkedIn)."""
    since = (datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=days)).isoformat()
    phrases: set[str] = set()
    people: set[str] = set()
    firms: set[str] = set()
    excerpts: set[tuple] = set()

    def person(*parts):
        words = [w for p in parts for w in _words(p or "")]
        if len(words) >= 2:
            phrases.add(" ".join(words))
        for w in words:
            if len(w) >= 3:
                people.add(w)

    def firm(name):
        words = _words(name or "")
        if not words:
            return
        if len(" ".join(words)) >= 4:
            phrases.add(" ".join(words))
        for w in words:
            if len(w) >= 3 and w not in _GENERIC_NAME_TOKENS:
                firms.add(w)

    async with aiosqlite.connect(state.db_path, timeout=30) as db:
        async def rows(sql, params=()):
            try:
                async with db.execute(sql, params) as cur:
                    return await cur.fetchall()
            except aiosqlite.OperationalError:
                return []

        for first, last, company in await rows("SELECT first_name, last_name, company FROM prospects"):
            person(first, last)
            firm(company)
        for (name,) in await rows("SELECT name FROM companies"):
            firm(name)
        for name, first, company in await rows(
                "SELECT name, first_name, company FROM linkedin_leads"):
            person(name)
            person(first)
            firm(company)
        for (thread,) in await rows(
                "SELECT thread_json FROM conversations WHERE updated_at >= ? "
                "ORDER BY updated_at DESC LIMIT 2000", (since,)):
            try:
                messages = json.loads(thread or "[]")
            except (TypeError, ValueError):
                continue
            for m in messages if isinstance(messages, list) else []:
                if isinstance(m, dict) and m.get("sender") != "harvey":
                    excerpts.update(_runs(_words(str(m.get("content") or "")), EXCERPT_RUN_WORDS))
        for (body,) in await rows(
                "SELECT body FROM linkedin_messages WHERE direction = 'in' "
                "AND COALESCE(sent_at, created_at) >= ? LIMIT 4000", (since,)):
            excerpts.update(_runs(_words(body or ""), EXCERPT_RUN_WORDS))
    # A one-word phrase is matched as a token above; only multi-word ones here.
    phrases = {p for p in phrases if " " in p}
    return {"names": {"phrases": phrases, "people": people, "firms": firms}, "excerpts": excerpts}


# ── Near-identical text ──

def near_identical(a: str, b: str) -> bool:
    wa, wb = _words(a), _words(b)
    if not wa or not wb:
        return False
    if wa == wb:
        return True
    sa, sb = set(wa), set(wb)
    jaccard = len(sa & sb) / len(sa | sb)
    return jaccard >= 0.8 or SequenceMatcher(None, " ".join(wa), " ".join(wb)).ratio() >= 0.88


# ── Store ──

def _view(row) -> dict:
    out = dict(row)
    try:
        evidence = json.loads(out.get("evidence_json") or "{}")
    except (TypeError, ValueError):
        evidence = {}
    out["evidence"] = evidence if isinstance(evidence, dict) else {}
    out.pop("evidence_json", None)
    return out


async def _query(state, sql: str, params=()) -> list[dict]:
    async with aiosqlite.connect(state.db_path, timeout=30) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(sql, params) as cur:
            return [_view(r) for r in await cur.fetchall()]


async def list_lessons(state, statuses=None, kinds=None, limit: int = 500) -> list[dict]:
    """Lessons, newest first, with ``evidence`` parsed."""
    where, params = [], []
    if statuses:
        statuses = [statuses] if isinstance(statuses, str) else list(statuses)
        where.append(f"status IN ({','.join('?' * len(statuses))})")
        params += statuses
    if kinds:
        kinds = [kinds] if isinstance(kinds, str) else list(kinds)
        where.append(f"kind IN ({','.join('?' * len(kinds))})")
        params += kinds
    sql = "SELECT * FROM lessons" + (" WHERE " + " AND ".join(where) if where else "")
    sql += " ORDER BY created_at DESC, rowid DESC LIMIT ?"
    return await _query(state, sql, (*params, int(limit)))


async def get_lesson(state, lesson_id: str) -> dict | None:
    rows = await _query(state, "SELECT * FROM lessons WHERE id = ?", (lesson_id,))
    return rows[0] if rows else None


async def count_lessons(state, status: str = "proposed") -> int:
    async with aiosqlite.connect(state.db_path, timeout=30) as db:
        async with db.execute("SELECT COUNT(*) FROM lessons WHERE status = ?", (status,)) as cur:
            (n,) = await cur.fetchone()
    return int(n or 0)


async def propose(state, kind: str, text: str, evidence: dict | None = None,
                  source_run: str = "", context: dict | None = None) -> tuple[str, bool]:
    """Propose a lesson: (id, created).

    A near-identical lesson of the same kind already on file is not proposed
    again: a waiting proposal takes the new evidence; an active, rejected or
    retired one is left as it is. Raises ValueError on an unknown kind or on
    text the filter refuses (``context``: ``filter_context``, read here when
    not given)."""
    if kind not in KINDS:
        raise ValueError(f"unknown lesson kind: {kind!r}")
    line = clean_text(text)
    if context is None:
        context = await filter_context(state)
    problem = lesson_problem(line, context.get("names"), context.get("excerpts"))
    if problem:
        raise ValueError(f"lesson refused: it {problem}")
    evidence_json = json.dumps(evidence or {}, sort_keys=True)
    for have in await list_lessons(state, kinds=(kind,), limit=2000):
        if near_identical(have["text"], line):
            if have["status"] == "proposed":
                async with aiosqlite.connect(state.db_path, timeout=30) as db:
                    await db.execute(
                        "UPDATE lessons SET evidence_json = ?, source_run = ? WHERE id = ?",
                        (evidence_json, source_run, have["id"]))
                    await db.commit()
            return have["id"], False
    lesson_id = uuid.uuid4().hex[:12]
    async with aiosqlite.connect(state.db_path, timeout=30) as db:
        await db.execute(
            "INSERT INTO lessons (id, kind, text, evidence_json, status, created_at, source_run) "
            "VALUES (?, ?, ?, ?, 'proposed', ?, ?)",
            (lesson_id, kind, line, evidence_json, _now(), source_run))
        await db.commit()
    return lesson_id, True


def valid_weights(raw) -> dict | None:
    """A segment_mix lesson's proposed weights as shares summing to 1, or
    None when they aren't usable: at least two known segments, each a number
    from 0 to 1, not all zero."""
    if not isinstance(raw, dict):
        return None
    out = {}
    for key, value in raw.items():
        seg = str(key).strip().lower()
        if seg not in MIX_SEGMENTS or isinstance(value, bool):
            return None
        try:
            share = float(value)
        except (TypeError, ValueError):
            return None
        if not (0 <= share <= 1) or share != share:
            return None
        out[seg] = share
    total = sum(out.values())
    if len(out) < 2 or total <= 0:
        return None
    return {k: round(v / total, 2) for k, v in out.items()}


async def current_weights(state) -> dict | None:
    try:
        raw = json.loads(await state.get_setting(SEGMENT_WEIGHTS_KEY) or "null")
    except (TypeError, ValueError):
        return None
    return raw if isinstance(raw, dict) else None


async def set_status(state, ids, status: str, by: str = "you") -> dict:
    """Move lessons along their lifecycle. Returns
    {"changed": [ids], "refused": {id: why}, "segment_weights": dict | None}.

    Approving a segment_mix lesson writes its weights to the
    ``segment_weights`` setting and retires any other active one (one mix
    applies); retiring the lesson whose weights are in force removes the
    setting, so the config's mix applies again."""
    if status not in STATUSES:
        raise ValueError(f"unknown lesson status: {status!r}")
    ids = [ids] if isinstance(ids, str) else [i for i in (ids or []) if isinstance(i, str)]
    changed, refused = [], {}
    weights_now = None
    for lesson_id in dict.fromkeys(i.strip() for i in ids if i.strip()):
        lesson = await get_lesson(state, lesson_id)
        if lesson is None:
            refused[lesson_id] = "no such lesson"
            continue
        if status not in TRANSITIONS.get(lesson["status"], ()):
            refused[lesson_id] = f"a {lesson['status']} lesson can't become {status}"
            continue
        weights = None
        if lesson["kind"] == "segment_mix" and status == "active":
            weights = valid_weights((lesson.get("evidence") or {}).get("weights"))
            if weights is None:
                refused[lesson_id] = "its proposed segment weights aren't usable"
                continue
        async with aiosqlite.connect(state.db_path, timeout=30) as db:
            cur = await db.execute(
                "UPDATE lessons SET status = ?, decided_at = ?, decided_by = ? "
                "WHERE id = ? AND status = ?",
                (status, _now(), by, lesson_id, lesson["status"]))
            await db.commit()
            if cur.rowcount == 0:
                refused[lesson_id] = "it changed meanwhile"
                continue
        changed.append(lesson_id)
        if lesson["kind"] != "segment_mix":
            continue
        if status == "active":
            for other in await list_lessons(state, statuses=("active",), kinds=("segment_mix",)):
                if other["id"] != lesson_id:
                    async with aiosqlite.connect(state.db_path, timeout=30) as db:
                        await db.execute(
                            "UPDATE lessons SET status = 'retired', decided_at = ?, "
                            "decided_by = 'replaced' WHERE id = ? AND status = 'active'",
                            (_now(), other["id"]))
                        await db.commit()
            await state.set_setting(SEGMENT_WEIGHTS_KEY, json.dumps(weights, sort_keys=True))
            weights_now = weights
        elif status == "retired":
            mine = valid_weights((lesson.get("evidence") or {}).get("weights"))
            if mine is not None and await current_weights(state) == mine:
                await state.delete_setting(SEGMENT_WEIGHTS_KEY)
    await refresh_active(state)
    return {"changed": changed, "refused": refused,
            "segment_weights": weights_now if weights_now is not None else await current_weights(state)}


# ── Prompt block ──

def evidence_weight(lesson: dict) -> int:
    evidence = lesson.get("evidence") or {}
    try:
        return int(evidence.get("sent") or 0) + int(evidence.get("edits") or 0)
    except (TypeError, ValueError):
        return 0


def render_block(lessons: list[dict], cap: int = BLOCK_CHARS) -> str:
    """The prompt block for these lessons: most evidence first, newest
    breaking a tie, whole lessons only, the whole block at most ``cap``
    characters. "" when none fits."""
    ordered = sorted(lessons, key=lambda l: (evidence_weight(l), str(l.get("decided_at") or ""),
                                             str(l.get("created_at") or "")), reverse=True)
    block = BLOCK_HEADER
    added = 0
    for lesson in ordered:
        text = clean_text(lesson.get("text"))
        if not text:
            continue
        line = f"\n- {text}"
        if len(block) + len(line) > cap:
            continue
        block += line
        added += 1
    return "\n\n" + block if added else ""


async def lessons_block(state, which: str) -> str:
    """The active lessons for one prompt ("writer", "handler", "linkedin") as
    prompt text, or "". Never stops a draft: a failed read is ""."""
    kinds = PROMPT_KINDS.get(which, ())
    if not kinds:
        return ""
    try:
        active = await list_lessons(state, statuses=("active",))
    except Exception:
        return ""
    _remember(state, tuple(clean_text(l["text"]) for l in active))
    return render_block([l for l in active if l["kind"] in kinds])


# ── The leak guard's view of active lessons (harvey/gate.py) ──
#
# The gate is synchronous and doesn't know the database, so the process keeps
# the active lessons' text here: refreshed whenever a prompt block is built,
# a status changes or the heartbeat's learner hook runs, and re-read straight
# from the database it came from at most every REGISTRY_TTL seconds.

REGISTRY_TTL = 60.0
_registry: dict = {"db_path": "", "texts": (), "read_at": 0.0}


def _remember(state, texts: tuple):
    _registry.update(db_path=str(getattr(state, "db_path", "") or ""), texts=tuple(texts),
                     read_at=time.monotonic())


async def refresh_active(state) -> tuple:
    try:
        rows = await list_lessons(state, statuses=("active",))
    except Exception:
        return _registry["texts"]
    _remember(state, tuple(clean_text(r["text"]) for r in rows))
    return _registry["texts"]


def active_texts() -> tuple:
    """The active lessons' text, for the leak guard."""
    path = _registry["db_path"]
    if path and time.monotonic() - _registry["read_at"] > REGISTRY_TTL:
        try:
            # Read-only, so a database that has gone is never re-created.
            uri = "file:" + Path(path).resolve().as_posix() + "?mode=ro"
            with closing(sqlite3.connect(uri, uri=True, timeout=5)) as db:
                rows = db.execute("SELECT text FROM lessons WHERE status = 'active'").fetchall()
            _registry.update(texts=tuple(clean_text(r[0]) for r in rows), read_at=time.monotonic())
        except (sqlite3.Error, OSError):
            _registry["read_at"] = time.monotonic()
    return _registry["texts"]


# ── Today ──

async def today_items(state) -> list[dict]:
    """A Today card while proposals wait."""
    try:
        n = await count_lessons(state, "proposed")
    except Exception:
        return []
    if not n:
        return []
    return [{
        "key": "lessons", "tone": "warn",
        "title": f"{n} lesson{'' if n == 1 else 's'} from results to review",
        "detail": "Harvey read its own results and proposes what to change. Nothing applies "
                  "until you approve it on the Learning tab.",
        "action": "Open Learning", "tab": "learning"}]


# ── CLI: harvey lessons ──

def add_cli_parser(subparsers):
    sub = subparsers.add_parser(
        "lessons", help="Lessons from results: review, approve, reject, retire, or run the learner")
    sub.add_argument("--approve", default="", metavar="ID", help="Approve lesson(s), comma-separated")
    sub.add_argument("--reject", default="", metavar="ID", help="Reject proposed lesson(s)")
    sub.add_argument("--retire", default="", metavar="ID", help="Retire active lesson(s)")
    sub.add_argument("--run", action="store_true",
                     help="Run the learner once now (its data minimums still apply)")
    sub.set_defaults(func=cmd_lessons)
    return sub


def _evidence_line(lesson: dict) -> str:
    e = lesson.get("evidence") or {}
    parts = []
    if e.get("sent"):
        parts.append(f"{e['sent']} sent, {e.get('human_replies', 0)} replies from people")
    if e.get("edits"):
        parts.append(f"{e['edits']} of your edits")
    if e.get("weights"):
        parts.append("mix " + ", ".join(f"{k} {round(float(v) * 100)}%" for k, v in e["weights"].items()))
    if e.get("note"):
        parts.append(str(e["note"]))
    return "; ".join(parts)


def cmd_lessons(args):
    import asyncio

    async def _run():
        from harvey.state import StateManager
        state = StateManager()
        await state.init_db()
        did = False
        for flag, status in (("approve", "active"), ("reject", "rejected"), ("retire", "retired")):
            ids = [i.strip() for i in (getattr(args, flag, "") or "").split(",") if i.strip()]
            if not ids:
                continue
            did = True
            result = await set_status(state, ids, status, by="you")
            for lesson_id in result["changed"]:
                print(f"\n  {lesson_id}: {status}.")
            for lesson_id, why in result["refused"].items():
                print(f"\n  {lesson_id}: not changed ({why}).")
            if status == "active" and result.get("segment_weights"):
                print("  Segment mix now: " + json.dumps(result["segment_weights"], sort_keys=True))
        if getattr(args, "run", False):
            did = True
            from harvey.agents.learner import run_once
            report = await run_once(state)
            print(f"\n  Learner: {report.get('status')}. {report.get('note', '')}".rstrip())
            if report.get("proposed"):
                print(f"  {report['proposed']} lesson(s) proposed.")
        if did:
            print("")
            return
        from harvey.agents.learner import last_run
        run = await last_run(state)
        proposed = await list_lessons(state, statuses=("proposed",))
        active = await list_lessons(state, statuses=("active",))
        print(f"\n  Lessons: {len(proposed)} waiting for you, {len(active)} active")
        print("  " + "=" * 66)
        for title, rows in (("WAITING FOR YOU", proposed), ("ACTIVE", active)):
            if not rows:
                continue
            print(f"\n  {title}")
            for lesson in rows:
                print(f"    {lesson['id']}  [{KIND_LABELS.get(lesson['kind'], lesson['kind'])}] "
                      f"{lesson['text']}")
                line = _evidence_line(lesson)
                if line:
                    print(f"                  {line}")
        if run:
            print(f"\n  Last learner run {str(run.get('at', ''))[:16].replace('T', ' ')} UTC: "
                  f"{run.get('note') or run.get('status', '')}")
        if proposed:
            print("\n  Approve with: harvey lessons --approve ID   (reject: --reject ID)")
        print("")

    asyncio.run(_run())
