"""Read-only reader for the website chat leads kept in an ERP's Supabase.

People who gave their address in the website's live chat are in three tables,
read over Supabase REST (PostgREST) at ``{ERP_SUPABASE_URL}/rest/v1/``:

- ``crm_chat_threads``: one chat. ``guest_session_id`` holds the session's
  ``session_token``, NOT its id: that is the join.
- ``crm_guest_sessions``: who: name, email, phone, ``source`` (usually JSON,
  but a bare string like "marketing-site" happens) and ``is_converted``.
- ``crm_chat_messages``: what was said. A human rep is ``sender_type`` employee
  and not ``is_ai``.

Two rules this file exists to keep:

- It only ever issues GET. ``_only_get`` refuses anything else before it leaves
  (and nothing here tries).
- A session token is never logged or stored. It lives in memory for the join
  and goes nowhere else: no InboundLead field holds it. It does travel in a
  query string (``session_token=in.(...)``), so ``_HideChatQueries`` keeps those
  URLs out of httpx's INFO log and errors here never quote a URL.
"""

import json
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone

import httpx

logger = logging.getLogger("harvey.collectors.erp_chat")

PAGE_SIZE = 1000
IN_CHUNK = 80
TIMEOUT_SECONDS = 30.0

THREAD_COLUMNS = ("id,guest_session_id,is_guest,has_client_info,status,ai_stage,handling_rep_id,"
                  "handling_rep_name,last_message_at,preferred_language")
SESSION_COLUMNS = "session_token,name,email,phone,source,is_converted,created_at"
MESSAGE_COLUMNS = "thread_id,sender_type,is_ai,content,created_at,meta,message_type"

# Not part of the conversation: greetings and buttons the widget draws, the
# contact card, and the transcript-by-email note.
# Whose address isn't a lead: our staff and the clients we already have.
KNOWN_PEOPLE_TABLES = ("profiles", "clients", "client_accounts")
DROPPED_KINDS = frozenset({"greeting", "selector", "contact_channels", "email_transcript"})
DIRECTORIES = ("clutch", "themanifest", "designrush", "goodfirms", "sortlist", "selectedfirms",
               "upcity", "topdevelopers", "softwareworld", "techbehemoths", "itfirms", "appfutura")

VISITOR, ASSISTANT, REP, NOTES = "visitor", "assistant", "rep", "notes"
# Our assistant told them a written estimate is coming, or to expect us in 24 hours.
_ESTIMATE_RE = re.compile(
    r"\bwritten\s+(?:estimate|quote|proposal)\b|\bestimate\b[^.\n]{0,60}\bin\s+writing\b"
    r"|\b24\s*-?\s*(?:hours?|hrs?|h|stunden|heures|horas|ore|uur)\b|24\s*時間",
    re.IGNORECASE)
_EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}$")
_PLAIN_VALUE_RE = re.compile(r"^[\w-]+$")


class ERPChatError(Exception):
    """The ERP couldn't be read. The message never carries a URL or a token."""


@dataclass
class ChatLine:
    who: str     # visitor / assistant / rep / notes (an owner's notes from a sheet)
    text: str
    at: str = ""


@dataclass
class InboundLead:
    """One person who gave us their address, across all their chats."""
    email: str
    name: str = ""
    phone: str = ""
    has_whatsapp: bool = False
    country: str = ""
    city: str = ""
    directory: str = ""          # clutch, themanifest, ... when they came from one
    referrer: str = ""
    intent: str = ""
    transcript: list[ChatLine] = field(default_factory=list)
    last_message_at: str = ""    # the latest message from anyone (naive UTC ISO)
    thread_ids: list[str] = field(default_factory=list)
    rep_name: str = ""
    ai_stage: str = ""
    converted: bool = False
    promised_estimate: bool = False
    human_replied: bool = False  # a person on our side answered them
    last_human_at: str = ""      # the last time a person on our side was on it
    last_visitor_at: str = ""    # the visitor's own last message: the fingerprint
    entry_path: str = ""
    # From a sheet (harvey inbound import).
    company: str = ""
    website: str = ""
    origin: str = "website_chat"
    source_label: str = ""

    @property
    def visitor_words(self) -> int:
        """How many words they wrote themselves (or the owner noted for them)."""
        return sum(len(line.text.split()) for line in self.transcript
                   if line.who in (VISITOR, NOTES))

    @property
    def fingerprint(self) -> str:
        """Changes only when the visitor writes something new."""
        return self.last_visitor_at or self.last_message_at


# ── Parsing what the tables hold ──

def parse_time(value) -> datetime | None:
    """A stored time as naive UTC, or None."""
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt.astimezone(timezone.utc).replace(tzinfo=None) if dt.tzinfo else dt


def _iso(dt: datetime | None) -> str:
    return dt.isoformat() if dt else ""


def directory_in(*texts) -> str:
    """The directory a referrer, utm or source names ("the-manifest" too), or ""."""
    flat = re.sub(r"[^a-z0-9]", "", " ".join(str(t or "") for t in texts).lower())
    return next((d for d in DIRECTORIES if d in flat), "")


def parse_source(raw) -> dict:
    """``crm_guest_sessions.source`` as {referrer, intent, directory, entry_path}.
    Usually JSON (or already a dict); a bare string like "marketing-site" is
    kept as what it is. Anything unreadable gives empty fields."""
    out = {"referrer": "", "intent": "", "directory": "", "entry_path": ""}
    data = raw
    if isinstance(raw, str):
        text = raw.strip()
        if text.startswith("{"):
            try:
                data = json.loads(text)
            except ValueError:
                data = None
        else:
            out["directory"] = directory_in(text)
            return out
    if not isinstance(data, dict):
        return out
    utm = data.get("utm")
    utm_text = json.dumps(utm) if isinstance(utm, (dict, list)) else str(utm or "")
    out["referrer"] = str(data.get("referrer") or "").strip()
    out["intent"] = str(data.get("intent") or data.get("openedIntent") or "").strip()
    out["directory"] = directory_in(out["referrer"], utm_text)
    for key, value in data.items():
        lowered = str(key).lower()
        if ("entry" in lowered or "landing" in lowered) and isinstance(value, str) and value.strip():
            out["entry_path"] = value.strip()[:200]
            break
    return out


def parse_place(value) -> tuple[str, str]:
    """``preferred_language`` ("CC|Region|City", or a bare country code) as
    (country, city)."""
    parts = [p.strip() for p in str(value or "").split("|")]
    code = parts[0].upper() if parts else ""
    country = code if len(code) == 2 and code.isascii() and code.isalpha() else ""
    return country, (parts[2] if len(parts) >= 3 else "")


def _meta(raw) -> dict:
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str) and raw.strip().startswith("{"):
        try:
            data = json.loads(raw)
        except ValueError:
            return {}
        return data if isinstance(data, dict) else {}
    return {}


def _clean_email(raw) -> str:
    email = str(raw or "").strip().lower()
    return email if _EMAIL_RE.match(email) else ""


def _in_list(values) -> str:
    """A PostgREST ``in.(...)``; anything but a plain word is double-quoted
    (an address holds reserved characters)."""
    def one(value):
        text = str(value)
        if _PLAIN_VALUE_RE.match(text):
            return text
        return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'
    return "in.(" + ",".join(one(v) for v in values) + ")"


def _chunks(values):
    values = list(values)
    for i in range(0, len(values), IN_CHUNK):
        yield values[i:i + IN_CHUNK]


# ── Turning rows into leads ──

def _read_messages(rows: list[dict], lead: InboundLead) -> None:
    """Fill the lead from its messages: the transcript (no system messages,
    widget furniture or team notes) and what they say about who did what."""
    def when(m):
        return parse_time(m.get("created_at")) or datetime.min

    last_human = last_visitor = None
    for m in sorted(rows, key=when):
        meta = _meta(m.get("meta"))
        kind = str(meta.get("kind") or "").strip().lower()
        audience = str(meta.get("audience") or "").strip().lower()
        sender = str(m.get("sender_type") or "").strip().lower()
        is_ai = bool(m.get("is_ai"))
        at = parse_time(m.get("created_at"))
        if kind == "contact_channels" and "whatsapp" in json.dumps(meta.get("channels") or "").lower():
            lead.has_whatsapp = True
            # The number they typed into the card, when the session has none.
            for channel in meta.get("channels") if isinstance(meta.get("channels"), list) else ():
                if (isinstance(channel, dict) and "whatsapp" in json.dumps(channel).lower()
                        and sum(c.isdigit() for c in str(channel.get("value") or "")) >= 7):
                    lead.phone = lead.phone or str(channel["value"]).strip()
        human = sender == "employee" and not is_ai and kind not in DROPPED_KINDS
        if human and at and (last_human is None or at > last_human):
            last_human = at
        if sender == "system" or kind in DROPPED_KINDS or audience == "team":
            continue
        text = str(m.get("content") or "").strip()
        if not text:
            continue
        if sender == "guest":
            who = VISITOR
            if at and (last_visitor is None or at > last_visitor):
                last_visitor = at
        elif sender == "employee":
            who = ASSISTANT if is_ai else REP
            lead.human_replied = lead.human_replied or human
            if is_ai and _ESTIMATE_RE.search(text):
                lead.promised_estimate = True
        else:
            continue
        lead.transcript.append(ChatLine(who, text, _iso(at)))
    lead.last_human_at = _iso(last_human)
    lead.last_visitor_at = _iso(last_visitor)


def build_leads(threads: list[dict], sessions: list[dict], messages: list[dict]) -> list[InboundLead]:
    """Leads deduped by lowercased email, each person's threads merged. A
    thread whose session has no usable address leads nowhere."""
    by_token = {s.get("session_token"): s for s in sessions if s.get("session_token")}
    by_thread: dict[str, list[dict]] = {}
    for m in messages:
        by_thread.setdefault(str(m.get("thread_id") or ""), []).append(m)

    people: dict[str, list[tuple[dict, dict]]] = {}
    for thread in threads:
        session = by_token.get(thread.get("guest_session_id"))
        email = _clean_email((session or {}).get("email"))
        if email:
            people.setdefault(email, []).append((thread, session))

    leads = []
    for email, pairs in people.items():
        # Newest thread first: its stage, rep and place are the current ones.
        pairs.sort(key=lambda p: parse_time(p[0].get("last_message_at")) or datetime.min, reverse=True)
        latest = pairs[0][0]
        country, city = parse_place(latest.get("preferred_language"))
        lead = InboundLead(email=email, country=country, city=city,
                           ai_stage=str(latest.get("ai_stage") or "").strip(),
                           rep_name=str(latest.get("handling_rep_name") or "").strip(),
                           thread_ids=[str(t.get("id")) for t, _ in pairs if t.get("id")])
        rows: list[dict] = []
        newest = None
        for thread, session in pairs:
            lead.name = lead.name or str(session.get("name") or "").strip()
            lead.phone = lead.phone or str(session.get("phone") or "").strip()
            lead.converted = lead.converted or bool(session.get("is_converted"))
            source = parse_source(session.get("source"))
            for name in ("referrer", "intent", "directory", "entry_path"):
                setattr(lead, name, getattr(lead, name) or source[name])
            rows += by_thread.get(str(thread.get("id") or ""), [])
            at = parse_time(thread.get("last_message_at"))
            if at and (newest is None or at > newest):
                newest = at
        _read_messages(rows, lead)
        for m in rows:
            at = parse_time(m.get("created_at"))
            if at and (newest is None or at > newest):
                newest = at
        lead.last_message_at = _iso(newest)
        leads.append(lead)
    return leads


# ── Reading ──

async def _only_get(request: httpx.Request):
    """This reader never changes the ERP, whatever calls it."""
    if request.method != "GET":
        raise ERPChatError(f"the ERP reader only issues GET, not {request.method}")


class _HideChatQueries(logging.Filter):
    """httpx logs every request URL at INFO, and these carry session tokens and
    addresses: "session_token=" also slips past main.RedactKeysFilter. Only the
    path is kept."""

    _QUERY = re.compile(r"(/rest/v1/crm_\w+)\?[^\s\"']*")

    def filter(self, record: logging.LogRecord) -> bool:
        message = record.getMessage()
        hidden = self._QUERY.sub(r"\1?<query hidden>", message)
        if hidden != message:
            record.msg, record.args = hidden, ()
        return True


logging.getLogger("httpx").addFilter(_HideChatQueries())


class ERPChatClient:
    def __init__(self, url: str = "", key: str = "", transport=None,
                 timeout: float = TIMEOUT_SECONDS):
        self.url = (url or "").strip().rstrip("/").removesuffix("/rest/v1")
        self.key = (key or "").strip()
        self.transport = transport
        self.timeout = timeout

    @property
    def configured(self) -> bool:
        return bool(self.url and self.key)

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            base_url=f"{self.url}/rest/v1/", transport=self.transport, timeout=self.timeout,
            follow_redirects=False, event_hooks={"request": [_only_get]},
            headers={"apikey": self.key, "Authorization": f"Bearer {self.key}",
                     "Accept": "application/json"})

    async def _rows(self, client: httpx.AsyncClient, table: str, params: dict) -> list[dict]:
        """Every row of one query, a Range header's page at a time."""
        out: list[dict] = []
        start = 0
        while True:
            headers = {"Range-Unit": "items", "Range": f"{start}-{start + PAGE_SIZE - 1}"}
            try:
                response = await client.get(table, params=params, headers=headers)
            except httpx.HTTPError as e:
                raise ERPChatError(f"{table} couldn't be read ({type(e).__name__})") from None
            if response.status_code == 416:      # past the last row
                return out
            if response.status_code not in (200, 206):
                raise ERPChatError(f"{table} answered HTTP {response.status_code}")
            try:
                rows = response.json()
            except ValueError:
                raise ERPChatError(f"{table} didn't answer with JSON") from None
            if not isinstance(rows, list):
                raise ERPChatError(f"{table} didn't answer with a list")
            out += [r for r in rows if isinstance(r, dict)]
            if len(rows) < PAGE_SIZE:
                return out
            start += PAGE_SIZE

    async def _threads(self, client, **where) -> list[dict]:
        return await self._rows(client, "crm_chat_threads", {
            "select": THREAD_COLUMNS, "has_client_info": "eq.true", "is_guest": "eq.true",
            "order": "id.asc", **where})

    async def _threads_of(self, client, tokens) -> list[dict]:
        out: list[dict] = []
        for chunk in _chunks(sorted(tokens)):
            out += await self._threads(client, guest_session_id=_in_list(chunk))
        return out

    async def _sessions(self, client, column: str, values) -> list[dict]:
        out: list[dict] = []
        for chunk in _chunks(sorted(values)):
            out += await self._rows(client, "crm_guest_sessions", {
                "select": SESSION_COLUMNS, column: _in_list(chunk), "order": "session_token.asc"})
        return out

    async def _messages(self, client, thread_ids) -> list[dict]:
        out: list[dict] = []
        for chunk in _chunks(sorted(thread_ids)):
            out += await self._rows(client, "crm_chat_messages", {
                "select": MESSAGE_COLUMNS, "thread_id": _in_list(chunk),
                "order": "thread_id.asc,created_at.asc"})
        return out

    async def known_people(self) -> set[str]:
        """Lowercased addresses of our staff and existing clients: a chat from
        one of them is not a new lead. A table that can't be read is skipped."""
        if not self.configured:
            return set()
        people: set[str] = set()
        async with self._client() as client:
            for table in KNOWN_PEOPLE_TABLES:
                try:
                    rows = await self._rows(client, table, {"select": "email", "email": "not.is.null"})
                except (ERPChatError, httpx.HTTPError) as e:
                    logger.warning(f"ERP: couldn't read {table}: {e}")
                    continue
                people.update(str(r.get("email") or "").strip().lower() for r in rows)
        people.discard("")
        return people

    async def read_leads(self, since: str = "") -> list[InboundLead]:
        """Everyone with an address, deduped by lowercased email. ``since``
        ("" for everything): only people with a thread whose last message is
        at or after it, each with all their threads, so a transcript and a
        rep's activity are never half read."""
        if not self.configured:
            raise ERPChatError("ERP_SUPABASE_URL and ERP_SUPABASE_KEY aren't set in .env")
        where = {}
        if since:
            at = parse_time(since)
            if at is None:
                raise ValueError("since must be an ISO time")
            where["last_message_at"] = f"gte.{at.isoformat()}+00:00"
        async with self._client() as client:
            threads = await self._threads(client, **where)
            tokens = {t.get("guest_session_id") for t in threads} - {None, ""}
            if not tokens:
                return []
            sessions = await self._sessions(client, "session_token", tokens)
            if since:
                # This person's other sessions (another device) and threads.
                emails = {str(s.get("email")).strip() for s in sessions if _clean_email(s.get("email"))}
                emails |= {e.lower() for e in emails}
                if not emails:
                    return []
                known = {s.get("session_token") for s in sessions}
                sessions += [s for s in await self._sessions(client, "email", emails)
                             if s.get("session_token") not in known]
                tokens = {s.get("session_token") for s in sessions} - {None, ""}
                threads = await self._threads_of(client, tokens)
            thread_ids = {str(t.get("id")) for t in threads if t.get("id")}
            messages = await self._messages(client, thread_ids)
        return build_leads(threads, sessions, messages)
