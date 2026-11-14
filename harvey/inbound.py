"""Inbound leads: someone gave their address in the website's live chat, and the
chat stopped. They get one personal email that picks up where it stopped.

Where it comes from: the ERP's chat tables, read-only
(harvey/collectors/erp_chat.py), or a sheet the owner keeps (`harvey inbound
import`, whose notes stand in for the chat). Each lead goes through:

- ``prefilter``, deterministic, no calls: our own domains and do-not-contact,
  job seekers, too little written to go on, already a customer, or a person on
  our side active in the chat lately. Anyone already contacted, replied, opted
  out, or whose company answered is left out here too.
- ``triage``, one fenced model call: what is this? Only ``project`` goes on;
  ``client`` and ``support`` are logged as an escalation for the owner.
- Priority: a directory lead, then a business address that is large, then
  large, medium, small, newest first within each.
- ``verify`` and ``draft_email`` (``prepare_lead`` calls them and stores
  nothing), then ``stage`` (writes and calls nothing). A draft with a money
  figure, over the word limit, or linking outside your domains is refused.

The chat promises serious visitors a written estimate, so when triage says it
said enough to scope one (``estimate_ready``), ``draft_estimate`` writes that
instead. Every money figure in it must be in ``inbound.price_list``
(harvey/money.py). Any other figure, an empty list, or no price at all refuses
it, and they get ``draft_email``'s email asking for the one missing detail, with
why recorded in ``inbound_leads``. An estimate always waits for a person, on
autopilot too, and "Approve all" doesn't release it: it is the one email here
that quotes a price.

What it writes: a company row for a business domain only (free mail never
becomes a company), a prospect, and one outbox row (``kind=sequence``,
``step=1``, ``angle=inbound`` or ``inbound_fresh``), held for your review unless
``channels.email.inbound.autopilot``. They aren't cold email (see
segment_stamp.INBOUND_ANGLES for what the Sender does differently). The footer,
opt-out, caps, pauses, gate and stop-on-reply all still apply.

The ``inbound_leads`` table is what makes runs resume: one row per address with
the visitor's last message as the fingerprint, so a lead isn't triaged again
until they write something new. It holds no message text beyond the one-line
summary, and nothing here logs or stores a session token.

``InboundRunner`` polls in the background on its own timer, quiet hours included,
so a new lead is reached within the hour of its chat ending. A chat is finished
once its last message is ``idle_minutes`` old. If they write again, or a person
on our side does, the unsent draft is cancelled; for new messages it is redone
once the chat is idle again. The poll starts from the last day: the older
backlog is only ever taken on by `harvey inbound run`.
"""

import asyncio
import csv
import json
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

import aiosqlite

from harvey.brain import SKILLS_DIR, BrainUnavailable
from harvey.collectors.erp_chat import (
    ASSISTANT, NOTES, REP, VISITOR, ChatLine, ERPChatClient, ERPChatError, InboundLead,
    directory_in, parse_time,
)
from harvey.config import inbound_of, live_do_not_contact, operator_timezone
from harvey.gate import (
    GIVEN, MAX_BODY_WORDS, MAX_ESTIMATE_BODY_WORDS, allowed_link_domains, autopilot_review,
    do_not_contact_match, pre_send_check,
)
from harvey.integrations.email_finder import verify_published_address
from harvey.integrations.mail_provider import NATIVE_PROVIDERS
from harvey.mailboxes import (
    MailboxProviders, configured_mailboxes, is_multi, least_loaded, normalize, render_sender,
    takes_new_prospects, primary_mailbox,
)
from harvey.models.company import Company
from harvey.models.prospect import Prospect
from harvey.money import find_figures, price_problem
from harvey.referrals import _ask_json, _ours, clean_name, registrable_domain
from harvey.segment_stamp import (
    ESTIMATE_HELD_REASON, INBOUND_ANGLE, INBOUND_ANGLES, INBOUND_AUTOPILOT, INBOUND_ESTIMATE_ANGLE,
    INBOUND_FRESH_ANGLE,
)
from harvey.send_window import zone_for
from harvey.state import COMPANY_STOP_KEY, FREE_MAIL_DOMAINS, HELD_FOR_REVIEW, MANUAL_SEND_KEY
from harvey.untrusted import UNTRUSTED_RULE, fence

logger = logging.getLogger("harvey.inbound")

CHAT_SOURCE = "inbound:website_chat"
TITLE = "Website enquiry"
AGENT = "inbound"
PRIVATE_SKILL = "inbound_followup"
ESTIMATE_SKILL = "estimate_rules"
CANCEL_REP = "a person on our side took this chat"
CANCEL_WROTE_AGAIN = "they wrote again in the chat"

# Who triage says wrote to us.
CATEGORIES = ("project", "client", "hiring", "vendor", "support", "spam", "other")
ESCALATED_CATEGORIES = ("client", "support")
SIZES = ("large", "medium", "small", "unknown")
SERVICE_TYPES = ("shopify", "wordpress", "custom", "mobile", "other")
PRIORITY_LABELS = ("directory lead", "large, business address", "large", "medium", "small",
                   "size unknown")

# What an inbound_leads row can say.
SKIPPED = "skipped"              # the prefilter, or already dealt with
NOT_PROJECT = "not_project"      # triage: hiring, vendor, spam, other
ESCALATED = "escalated"          # triage: client or support, logged for the owner
TRIAGE_FAILED = "triage_failed"  # no usable answer; tried again, MAX_ATTEMPTS at most
TRIAGED = "triaged"              # a project, waiting for its turn at a draft
INVALID = "invalid_address"      # the verifier says it doesn't exist: never sent
UNVERIFIED = "unverified"        # no verdict, or a catch-all without send_to_risky
NO_DRAFT = "no_draft"            # the drafts were refused; tried again, MAX_ATTEMPTS at most
DRAFTED = "drafted"              # in the Outbox
SENT = "sent"
REDRAFT = "redraft"              # they wrote again: its draft was cancelled

MAX_ATTEMPTS = 3
MAX_WORDS = 110
MAX_ESTIMATE_WORDS = 300
MIN_VISITOR_WORDS = 4
MAX_PER_POLL = 10
DRAFT_TRIES = 2
DRY_RUN_LIMIT = 5
FRESH_FOR = timedelta(hours=24)
FIRST_POLL_LOOKBACK = timedelta(hours=24)
CURSOR_OVERLAP = timedelta(minutes=2)
POLL_TIMEOUT_SECONDS = 600
RUNNING_STALE = timedelta(minutes=30)

CURSOR_KEY = "inbound:cursor"
RUNNING_KEY = "inbound:running"
LAST_RUN_KEY = "inbound:last_run"
LAST_ERROR_KEY = "inbound:last_error"   # the last poll that failed, for the dashboard

# Prospect statuses that mean nobody writes to them from here again.
_NOT_AGAIN = frozenset({"contacted", "replied", "opted_out", "lost", "meeting", "closed"})
_JOB_BOARDS = ("naukri", "internshala", "indeed", "glassdoor", "wellfound", "foundit", "monster",
               "ziprecruiter", "linkedin.com/jobs")
_JOB_INTENT_RE = re.compile(r"\bcareers?\b|\bget\s+hired\b|\bjob\s*seeker\b|\bjobs\b", re.IGNORECASE)

_ATTACHED_RE = re.compile(
    r"\b(?:estimate|quote|proposal)\b[^.\n]{0,40}\battach"
    r"|\battach\w*\b[^.\n]{0,40}\b(?:estimate|quote|proposal)\b", re.IGNORECASE)

INBOUND_SCHEMA = """
    CREATE TABLE IF NOT EXISTS inbound_leads (
        email TEXT PRIMARY KEY,
        -- The visitor's last message when this row was decided: the fingerprint.
        last_message_at TEXT DEFAULT '',
        category TEXT DEFAULT '',
        size TEXT DEFAULT '',
        summary TEXT DEFAULT '',
        language TEXT DEFAULT '',
        missing TEXT DEFAULT '',
        estimate_ready INTEGER DEFAULT 0,
        service_type TEXT DEFAULT '',
        status TEXT DEFAULT '',
        why TEXT DEFAULT '',
        outbox_id TEXT DEFAULT '',
        attempts INTEGER DEFAULT 0,
        processed_at TEXT DEFAULT ''
    );
    CREATE INDEX IF NOT EXISTS idx_inbound_leads_status ON inbound_leads(status);
"""
_RECORD_FIELDS = ("last_message_at", "category", "size", "summary", "language", "missing",
                  "estimate_ready", "service_type", "status", "why", "outbox_id", "attempts")

# Used only when prompts/inbound.md can't be read. The code still refuses a
# price, a long draft or a link elsewhere.
FALLBACK_PROMPT = (
    "You write as {who}. Tone: {tone}. We offer {product}.\n"
    "Someone reached us through {how} and gave us their email address, then it stopped. Write "
    "one short personal email that says so honestly and picks up where it stopped: one or two "
    "specifics in your own words, in their language, inventing nothing, one clear next step. "
    "No prices or price ranges, no promised dates, nothing said to be attached. Under 110 "
    "words, plain text. Subject: lowercase, 2-5 words.")
# The same for prompts/inbound_estimate.md. The price guard doesn't depend on it.
FALLBACK_ESTIMATE_PROMPT = (
    "You write as {who}. Tone: {tone}. We offer {product}.\n"
    "Someone reached us through {how} and was promised a written estimate. Write it, in their "
    "language: what we understood in two lines, scope as a short section-by-section list, plans "
    "with one recommended, timeline, what's not included, one next step. Use only the allowed "
    "amounts, exactly as written, and no other money figure. Invent nothing, say nothing is "
    "attached. Under 300 words, plain text. Subject: lowercase, 2-5 words.")


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _after(a: str, b: str) -> bool:
    """Time ``a`` is later than time ``b`` (or they differ, if one isn't a time)."""
    ta, tb = parse_time(a), parse_time(b)
    if ta and tb:
        return ta > tb
    return bool(a) and a != b


def _email_domain(email: str) -> str:
    return (email or "").rpartition("@")[2].strip().lower()


def is_business_email(email: str) -> bool:
    domain = _email_domain(email)
    return bool(domain) and domain not in FREE_MAIL_DOMAINS


def _when(value) -> float:
    at = parse_time(value)
    return (at - datetime(1970, 1, 1)).total_seconds() if at else 0.0


# ── The prefilter ──

def prefilter(lead: InboundLead, own_domains=(), do_not_contact=(), rep_active_days: int = 14,
              now: datetime | None = None, everyone: bool = False) -> str:
    """Why this lead gets nothing, or "". Deterministic, and it costs nothing,
    so it runs every time a lead is read. Reasons name no message text.
    ``everyone`` (inbound.everyone): a short chat and one a person on our side
    handled are no longer reasons."""
    now = now or _utcnow()
    if _ours(_email_domain(lead.email), own_domains):
        return "our own domain"
    if do_not_contact_match(lead.email, do_not_contact):
        return "on the do-not-contact list"
    if lead.ai_stage.strip().lower() == "hiring":
        return "looking for a job (chat stage)"
    if _JOB_INTENT_RE.search(lead.intent or ""):
        return "looking for a job (intent)"
    referrer = (lead.referrer or "").lower()
    if any(board in referrer for board in _JOB_BOARDS):
        return "looking for a job (came from a job board)"
    if lead.visitor_words < MIN_VISITOR_WORDS and not everyone:
        return f"wrote under {MIN_VISITOR_WORDS} words"
    if lead.converted:
        return "already converted"
    last = parse_time(lead.last_human_at)
    if last and now - last < timedelta(days=rep_active_days) and not everyone:
        return f"a person on our side was active in the last {rep_active_days} days"
    return ""


# Triage categories that get the follow-up: a project, and with inbound.everyone
# one triage couldn't place.
def _emailed_categories(everyone: bool) -> tuple[str, ...]:
    return ("project", "other") if everyone else ("project",)


async def already_dealt_with(state, lead: InboundLead) -> str:
    """Why nobody writes to them from here (a contact, a hand send, an answer
    from their company), or ""."""
    existing = await state.get_prospect_by_email(lead.email)
    if existing is not None:
        if existing.status in _NOT_AGAIN:
            return f"already {existing.status}"
        if await state.get_setting(MANUAL_SEND_KEY + existing.id):
            return "you already emailed them by hand"
        if await state.colleague_who_replied(existing.id):
            return "someone at their company already answered"
        return ""
    if is_business_email(lead.email) and await _company_answered(state, lead.email):
        return "someone at their company already answered"
    return ""


async def _company_answered(state, email: str) -> bool:
    """Someone at this address's domain has replied. Someone who isn't a
    prospect yet has no row for colleague_who_replied to start from."""
    domain = _email_domain(email)
    registrable = registrable_domain(domain)
    async with aiosqlite.connect(state.db_path, timeout=30) as db:
        async with db.execute(
                "SELECT 1 FROM settings s JOIN prospects c "
                f"ON s.key = '{COMPANY_STOP_KEY}' || c.id "
                "WHERE lower(substr(c.email, instr(c.email, '@') + 1)) IN (?, ?) "
                "OR c.company_id IN (SELECT id FROM companies WHERE lower(domain) IN (?, ?)) "
                "LIMIT 1", (domain, registrable, domain, registrable)) as cursor:
            return await cursor.fetchone() is not None


# ── Triage ──

@dataclass
class Triage:
    category: str = "other"
    size: str = "unknown"
    summary: str = ""
    language: str = ""
    missing: str = ""
    estimate_ready: bool = False   # the chat says enough to scope it
    service_type: str = "other"

    @classmethod
    def from_answer(cls, answer: dict) -> "Triage":
        category = str(answer.get("category") or "").strip().lower()
        size = str(answer.get("size") or "").strip().lower()
        service = str(answer.get("service_type") or "").strip().lower()
        missing = answer.get("missing")
        if isinstance(missing, (list, tuple)):
            missing = ", ".join(str(m) for m in missing)
        # Anything but a plain yes leads to an ordinary email, which is safe.
        ready = answer.get("estimate_ready")
        ready = ready is True or str(ready).strip().lower() in ("true", "yes")
        return cls(category=category if category in CATEGORIES else "other",
                   size=size if size in SIZES else "unknown",
                   summary=" ".join(str(answer.get("summary") or "").split())[:240],
                   language=" ".join(str(answer.get("language") or "").split())[:40],
                   missing=" ".join(str(missing or "").split())[:240],
                   estimate_ready=ready,
                   service_type=service if service in SERVICE_TYPES else "other")

    @classmethod
    def from_record(cls, record: dict) -> "Triage":
        return cls(category=record.get("category") or "project",
                   size=record.get("size") or "unknown", summary=record.get("summary") or "",
                   language=record.get("language") or "", missing=record.get("missing") or "",
                   estimate_ready=bool(record.get("estimate_ready")),
                   service_type=record.get("service_type") or "other")

    def fields(self) -> dict:
        """As inbound_leads stores it."""
        return dict(category=self.category, size=self.size, summary=self.summary,
                    language=self.language, missing=self.missing,
                    estimate_ready=int(self.estimate_ready), service_type=self.service_type)


_LABELS = {VISITOR: "Visitor", ASSISTANT: "Our assistant", REP: "Our team",
           NOTES: "Our notes on what they asked"}


def transcript_text(lead: InboundLead, max_chars: int = 6000) -> str:
    """The conversation for a prompt, the end of it when it is long."""
    lines = [f"{_LABELS.get(line.who, line.who)}: {line.text[:1000]}" for line in lead.transcript]
    return "\n".join(lines)[-max_chars:]


def how_they_reached_us(lead: InboundLead) -> str:
    if lead.origin == "website_chat":
        return "the live chat on our website"
    if lead.origin == "pasted" and lead.source_label:
        return lead.source_label       # where you said the chat happened
    via = lead.directory or lead.source_label
    return f"an enquiry through {via}" if via else "an enquiry they sent us"


async def triage(brain, lead: InboundLead, offer: str = "") -> Triage | None:
    """What this lead is: one fenced JSON call. None when no usable answer
    came back; raises BrainUnavailable when the provider is down."""
    context = []
    if lead.directory:
        context.append(f"They came from the {lead.directory} directory.")
    if lead.intent:
        context.append(f"Button they chose: {lead.intent}")
    prompt = f"""Someone reached {offer or 'a company that sells services'} through {how_they_reached_us(lead)}
and gave their email address. Read it and sort it.

{UNTRUSTED_RULE}

{fence(transcript_text(lead), "the conversation")}
{fence(" ".join(context), "where they came from") if context else ""}

Return ONLY JSON: {{"category": "...", "size": "...", "summary": "...", "language": "...", "missing": "...", "estimate_ready": false, "service_type": "..."}}
- category, exactly one of:
  project: they may want to buy something from us (a build, a service, an estimate);
  client: they already work with us;
  hiring: they want a job or an internship with us;
  vendor: they are selling something to us, or offering a partnership or backlinks;
  support: they need help with something we already delivered;
  spam: nonsense, a test, or an automated message;
  other: anything else.
- size of what they describe: large, medium, small, or unknown when they don't say enough.
- summary: one line under 20 words saying what they want, in your own words. No names,
  addresses or quotes.
- language: the language they write in, in English (for example "German").
- missing: in a few words, what we would need to know from them next, or "" if nothing.
- estimate_ready: true only if what they wrote is enough to scope the work and pick a plan
  without guessing; false when in doubt.
- service_type: shopify, wordpress, custom (a site or software built from scratch), mobile
  (an app), or other."""
    answer = await _ask_json(brain, prompt, "inbound_triage", agent=AGENT)
    if not isinstance(answer, dict) or not str(answer.get("category") or "").strip():
        return None
    return Triage.from_answer(answer)


# ── Priority ──

def priority_rank(lead: InboundLead, triaged: Triage) -> int:
    """Lower goes first: an index into PRIORITY_LABELS."""
    if lead.directory:
        return 0
    if triaged.size == "large":
        return 1 if is_business_email(lead.email) else 2
    return {"medium": 3, "small": 4}.get(triaged.size, 5)


def priority_label(lead: InboundLead, triaged: Triage) -> str:
    label = PRIORITY_LABELS[priority_rank(lead, triaged)]
    return f"{label} ({lead.directory})" if lead.directory else label


def priority_key(lead: InboundLead, triaged: Triage) -> tuple:
    return (priority_rank(lead, triaged), -_when(lead.last_message_at))


def _triage_order(lead: InboundLead) -> tuple:
    """Who to triage first when not all of them will be: size isn't known yet."""
    return (0 if lead.directory else 1 if is_business_email(lead.email) else 2,
            -_when(lead.last_message_at))


# ── The draft ──

class DraftRejected(Exception):
    """No draft that passes the checks. The message says why, with no draft text."""


class EstimateRejected(DraftRejected):
    """No estimate that can go to a reviewer: the lead gets an ordinary email."""


def _shape_problem(subject: str, body: str, config, recipient: str, limit: int,
                   gate_limit: int) -> str:
    """Why this text can't go out as written, or "": its length, a false claim of
    an attachment, the send gate's checks, and links outside your domains."""
    words = len(body.split())
    if words > limit:
        return f"it runs to {words} words; the limit is {limit}"
    if _ATTACHED_RE.search(f"{subject}\n{body}"):
        return "it says an estimate is attached"
    # The sender tag is filled in when it goes out; the gate reads it as a stray tag.
    plain = render_sender(body, primary_mailbox(config)) if is_multi(config) else body
    gate = pre_send_check(recipient, subject, plain, kind="reply", max_words=gate_limit)
    if not gate:
        return "; ".join(gate.reasons)[:200]
    # Your domains and booking page only, and none of Harvey's own instructions.
    review = autopilot_review(subject, body, config, recipient=recipient)
    return "; ".join(review)[:200]


def draft_problem(subject: str, body: str, config, recipient: str) -> str:
    """Why this email can't be used, or "". It names no money at all."""
    if find_figures(f"{subject}\n{body}"):
        return "it names a money figure"
    return _shape_problem(subject, body, config, recipient, MAX_WORDS, MAX_BODY_WORDS)


def estimate_problem(subject: str, body: str, config, recipient: str) -> str:
    """Why this estimate can't be used, leaving its prices to price_problem."""
    return _shape_problem(subject, body, config, recipient, MAX_ESTIMATE_WORDS,
                          MAX_ESTIMATE_BODY_WORDS)


def _facts(lead: InboundLead, triaged: Triage) -> str:
    facts = [f"- Name: {lead.name or 'unknown (greet them with Hi there)'}",
             f"- Write in: {triaged.language or 'the language they wrote in'}",
             f"- What they want: {triaged.summary or 'see the conversation'}"]
    if lead.company:
        facts.append(f"- Company: {lead.company}")
    if lead.city or lead.region or lead.country:
        facts.append(f"- Where they are: {', '.join(x for x in (lead.city, lead.region, lead.country) if x)}")
    facts += journey_facts(lead)
    if triaged.missing:
        facts.append(f"- What we still need to know: {triaged.missing}")
    if lead.entry_path:
        facts.append(f"- The page they started from: {lead.entry_path}")
    if lead.has_whatsapp:
        facts.append("- We offered them WhatsApp in the chat")
    if lead.human_replied:
        facts.append("- A person on our team has answered them before")
    if triaged.service_type != "other":
        facts.append(f"- Kind of work: {triaged.service_type}")
    if lead.site_facts:
        facts.append(f"- What their own website shows (Harvey read it): {lead.site_facts}")
    if lead.owner_note:
        # Yours, from the dashboard: trusted, unlike the conversation.
        facts.append(f"- The owner's note for this email (follow it): {lead.owner_note}")
    return "\n".join(facts)


def journey_facts(lead: InboundLead) -> list[str]:
    """Their visit, for the draft: how they found us and what they read, in
    order. Pages are ours, so naming the one they read is fair; say it plainly
    ("you were reading about ..."), never as surveillance."""
    out = []
    found = [x for x in (lead.directory and f"the {lead.directory} directory",
                         lead.referrer and f"a link on {_host(lead.referrer)}",
                         lead.utm and f"a campaign tagged {lead.utm}") if x]
    if found:
        out.append("- How they found us: " + "; ".join(found))
    if lead.trail:
        out.append("- Pages of our site they read, in order: " + " -> ".join(lead.trail))
    if lead.opened_on:
        out.append(f"- The page they were on when they opened the chat: {lead.opened_on}")
    return out


def journey_note(lead: InboundLead) -> str:
    """One short line of the same, for the prospect's notes and WhatsApp."""
    parts = []
    if lead.trail:
        pages = [p.split(" (")[0] for p in lead.trail]
        parts.append(f"read {len(pages)} page{'s' if len(pages) != 1 else ''}: " + ", ".join(pages[:6]))
    if lead.opened_on:
        parts.append("opened the chat on " + lead.opened_on.split(" (")[0])
    place = ", ".join(x for x in (lead.city, lead.region, lead.country) if x)
    if place:
        parts.append("in " + place)
    return "; ".join(parts)[:300]


def _host(url: str) -> str:
    host = re.sub(r"^[a-z]+://", "", str(url or "").strip().lower()).split("/")[0]
    return host.removeprefix("www.") or str(url)[:60]


def _whatsapp_context(lead: InboundLead, triaged: Triage) -> str:
    """What the WhatsApp draft knows about them: what they want, and their visit."""
    visit = journey_note(lead)
    return (triaged.summary or "") + (f" ({visit})" if visit else "")


def _skills_with_private(brain) -> str:
    """The writer's skills, and the private follow-up skill when it exists."""
    skills = ""
    if hasattr(brain, "load_skills_for_agent"):
        skills = brain.load_skills_for_agent("writer") or ""
    if hasattr(brain, "load_skill") and (Path(SKILLS_DIR) / f"{PRIVATE_SKILL}.md").is_file():
        private = brain.load_skill(PRIVATE_SKILL) or ""
        if private:
            skills = f"{skills}\n\nHOW WE FOLLOW UP ON THESE\n{private}"
    return skills


def _rules(brain, config, lead: InboundLead, prompt_name: str, fallback: str) -> list[str]:
    """The opening of a draft's prompt: its rules, the footer and the sign-off."""
    from harvey.agents.writer import BUILT_IN_FOOTER, sign_off_rule
    persona, product = config.persona, config.product
    multi = is_multi(config)
    who = (f"a member of the {persona.company} team" if multi
           else f"{persona.name}, {persona.role} at {persona.company}")
    how = how_they_reached_us(lead)
    rules = ""
    if hasattr(brain, "load_prompt"):
        rules = brain.load_prompt(prompt_name, who=who, tone=persona.tone, product_name=product.name,
                                  product_description=product.description,
                                  how_they_reached_us=how) or ""
    rules = rules or fallback.format(
        who=who, tone=persona.tone, product=f"{product.name}: {product.description}", how=how)
    parts = [rules, BUILT_IN_FOOTER]
    if multi:
        parts.append(sign_off_rule("the email"))
    return parts


def _conversation(lead: InboundLead, triaged: Triage, skills: str) -> list[str]:
    """The close of a draft's prompt: what is known, and the format."""
    parts = [f"\nFACTS (everything you know; invent nothing else):\n{fence(_facts(lead, triaged), 'their details')}",
             f"\nThe conversation so far:\n{fence(transcript_text(lead), 'the conversation')}",
             f"\n{UNTRUSTED_RULE}"]
    if skills:
        parts.append(skills)
    parts.append('\nReturn ONLY JSON: {"subject": "...", "body": "..."}')
    return parts


def _draft_prompt(brain, config, lead: InboundLead, triaged: Triage, skills: str,
                  ask_one: bool = False) -> str:
    parts = _rules(brain, config, lead, AGENT, FALLBACK_PROMPT)
    if lead.promised_estimate and not lead.human_replied:
        parts.append(
            "- Our assistant told them in the chat that a written estimate would follow, and no "
            "person has replied since. Own that plainly in one line, without making a new "
            "promise, and ask only for what we still need in order to write it.")
    if ask_one:
        parts.append(
            "- We can't send them a written estimate yet. Ask for the one detail that would help "
            "most (see FACTS), and nothing else. Don't say why, and name no price.")
    return "\n".join(parts + _conversation(lead, triaged, skills))


async def draft_email(brain, config, lead: InboundLead, triaged: Triage, skills: str = "",
                      ask_one: bool = False) -> dict:
    """Their email: {"subject", "body"}. ``ask_one``: an estimate couldn't be
    sent, so it asks for the one missing detail. Asked again once when a draft
    fails the checks; raises DraftRejected when none passes, and
    BrainUnavailable when the provider is down."""
    from harvey.humanize import humanize, humanizer_enabled
    prompt = _draft_prompt(brain, config, lead, triaged, skills, ask_one)
    why = ""
    for _ in range(DRAFT_TRIES):
        asked = prompt + (f"\n\nYour last draft was refused: {why}. Write it again without that."
                          if why else "")
        answer = await _ask_json(brain, asked, "inbound_email", agent=AGENT)
        if not isinstance(answer, dict):
            why = "no usable draft came back"
            continue
        subject = " ".join(str(answer.get("subject") or "").split())[:120]
        body = str(answer.get("body") or "").strip()[:4000]
        if not subject or not body:
            why = "the draft came back empty"
            continue
        [polished] = await humanize(brain, [{"subject": subject, "body": body}], agent=AGENT,
                                    enabled=humanizer_enabled(config))
        subject = polished.get("subject") or subject
        body = polished.get("body") or body
        why = draft_problem(subject, body, config, lead.email)
        if not why:
            return {"subject": subject, "body": body}
    raise DraftRejected(why)


def _estimate_prompt(brain, config, lead: InboundLead, triaged: Triage, skills: str,
                     price_list: list[str]) -> str:
    parts = _rules(brain, config, lead, "inbound_estimate", FALLBACK_ESTIMATE_PROMPT)
    parts.append("\nALLOWED AMOUNTS (the only money figures this estimate may name):\n"
                 + "\n".join(f"- {amount}" for amount in price_list))
    if hasattr(brain, "load_skill") and (Path(SKILLS_DIR) / f"{ESTIMATE_SKILL}.md").is_file():
        private = brain.load_skill(ESTIMATE_SKILL) or ""
        if private:
            parts.append(f"\nHOW WE SCOPE AND PRICE THIS WORK\n{private}")
    return "\n".join(parts + _conversation(lead, triaged, skills))


async def draft_estimate(brain, config, lead: InboundLead, triaged: Triage, skills: str = "") -> dict:
    """Their written estimate: {"subject", "body"}. Fails closed with
    EstimateRejected: an empty price list costs no model call, and a figure off
    the list is never asked about again, so the model can't be coaxed into a
    price. Too long or otherwise unusable is asked again once."""
    from harvey.humanize import humanize, humanizer_enabled
    price_list = list(inbound_of(config).price_list)
    if not price_list:
        raise EstimateRejected("channels.email.inbound.price_list is empty")
    prompt = _estimate_prompt(brain, config, lead, triaged, skills, price_list)
    why = ""
    for _ in range(DRAFT_TRIES):
        asked = prompt + (f"\n\nYour last draft was refused: {why}. Write it again without that."
                          if why else "")
        answer = await _ask_json(brain, asked, "inbound_estimate", agent=AGENT)
        if not isinstance(answer, dict):
            why = "no usable draft came back"
            continue
        subject = " ".join(str(answer.get("subject") or "").split())[:120]
        body = str(answer.get("body") or "").strip()[:6000]
        if not subject or not body:
            why = "the draft came back empty"
            continue
        [polished] = await humanize(brain, [{"subject": subject, "body": body}], agent=AGENT,
                                    enabled=humanizer_enabled(config))
        subject = polished.get("subject") or subject
        body = polished.get("body") or body
        priced = price_problem(f"{subject}\n{body}", price_list)
        if priced:
            raise EstimateRejected(priced)
        why = estimate_problem(subject, body, config, lead.email)
        if not why:
            return {"subject": subject, "body": body}
    raise EstimateRejected(why)


# ── Preparing and storing ──

async def verify(lead: InboundLead, env, state) -> str:
    """verified / risky / guess / invalid, as for any address a company publishes."""
    return await verify_published_address(lead.email, env=env, state=state)


@dataclass
class Plan:
    """What store writes for one project lead."""
    lead: InboundLead
    triaged: Triage
    grade: str = ""
    existing: object | None = None
    draft: dict | None = None
    is_estimate: bool = False      # the draft is a written estimate: always held
    status: str = ""               # INVALID, UNVERIFIED or NO_DRAFT when nothing goes out
    why: str = ""                  # why nothing goes out, or why an estimate fell back


async def prepare_lead(state, brain, config, env, lead: InboundLead, triaged: Triage,
                       skills: str = "", check_address: bool = True) -> Plan:
    """Grade the address and write the email. Stores nothing (the free tiers'
    counters aside); raises BrainUnavailable when the provider is down. With
    inbound.everyone, an address they typed themselves that no verifier could
    check is graded "given" and written to; one a verifier says doesn't exist
    never is."""
    plan = Plan(lead=lead, triaged=triaged)
    plan.existing = await state.get_prospect_by_email(lead.email)
    allow_risky = bool(getattr(config.channels.email, "send_to_risky", False))
    sendable = ("verified", "risky") if allow_risky else ("verified",)
    if inbound_of(config).everyone:
        sendable += (GIVEN,)
    if plan.existing is not None and plan.existing.email_status in sendable:
        plan.grade = plan.existing.email_status
    elif check_address:
        plan.grade = await verify(lead, env, state)
        if plan.grade == "guess" and GIVEN in sendable:
            plan.grade = GIVEN
    if check_address and plan.grade not in sendable:
        if plan.grade == "invalid":
            plan.status, plan.why = INVALID, "the verifier says this address doesn't exist"
        elif plan.grade == "risky":
            plan.status, plan.why = UNVERIFIED, "a catch-all address, and send_to_risky is off"
        else:
            plan.status, plan.why = UNVERIFIED, "no verifier could say whether this address exists"
        return plan
    if triaged.estimate_ready:
        try:
            plan.draft = await draft_estimate(brain, config, lead, triaged, skills)
            plan.is_estimate = True
            return plan
        except EstimateRejected as e:
            plan.why = f"estimate refused, asked for a detail instead: {e}"[:300]
    try:
        plan.draft = await draft_email(brain, config, lead, triaged, skills,
                                       ask_one=triaged.estimate_ready)
    except DraftRejected as e:
        plan.status = NO_DRAFT
        plan.why = "; ".join(x for x in (plan.why, str(e)) if x)[:300]
    return plan


def _company_name(domain: str) -> str:
    label = domain.split(".")[0] if domain else ""
    return label.replace("-", " ").title()


def _notes(lead: InboundLead, triaged: Triage) -> str:
    at = parse_time(lead.fingerprint)
    how = how_they_reached_us(lead)
    start = how[0].upper() + how[1:]
    visit = journey_note(lead)
    return (f"{start}" + (f", {at:%Y-%m-%d}" if at else "") + ": "
            + (triaged.summary or "see the Outbox draft") + (f" ({visit})" if visit else ""))[:500]


async def _set_notes(state, prospect_id: str, notes: str):
    async with aiosqlite.connect(state.db_path, timeout=30) as db:
        await db.execute("UPDATE prospects SET personalization_notes = ?, updated_at = ? WHERE id = ?",
                         (notes, _utcnow().isoformat(), prospect_id))
        await db.commit()


async def _choose_mailbox(state, config, providers, prospect_id: str) -> str:
    """Several mailboxes: the mailbox a company already dealt with stays with
    it; otherwise the least busy one that can take new prospects, or of all of
    them when none can (the email waits for it). "" with one mailbox."""
    if not is_multi(config):
        return ""
    stored = await state.company_mailbox([prospect_id])
    if stored is not None:
        return normalize(config, stored)
    mailboxes = configured_mailboxes(config)
    open_ = [m for m in mailboxes
             if not await takes_new_prospects(state, m, providers.get(m))]
    chosen = await least_loaded(state, config, open_ or mailboxes)
    return chosen.email if chosen else ""


async def stage(state, config, plan: Plan, *, now: datetime, providers=None,
                autopilot: bool = False, force: bool = False) -> str | None:
    """Write what ``plan`` says: the company (a business domain only), the
    prospect and their email in the Outbox. Returns its id, or None when
    someone beat us to them. Makes no model or verifier calls. ``force``: you
    said to write to them anyway (a chat you pasted about someone Harvey
    already dealt with)."""
    lead, triaged = plan.lead, plan.triaged
    if not force and await already_dealt_with(state, lead):
        return None
    place = ", ".join(x for x in (lead.city, lead.country) if x)
    source = CHAT_SOURCE if lead.origin == "website_chat" else "inbound:pasted" if lead.origin == "pasted" else (
        "inbound:" + re.sub(r"[^a-z0-9]+", "_", (lead.directory or lead.source_label or "sheet").lower()).strip("_"))
    company_id = ""
    company_name = lead.company.strip()
    if is_business_email(lead.email):
        domain = registrable_domain(_email_domain(lead.email))
        company_id = await state.add_company(Company(
            name=company_name or _company_name(domain), domain=domain,
            website=lead.website.strip() or f"https://{domain}", location=place, source=source))
        company = await state.get_company(company_id)
        company_name = (getattr(company, "name", "") or company_name).strip()
    notes = _notes(lead, triaged)
    first, last = clean_name(lead.name)
    existing = plan.existing
    if existing is not None:
        prospect_id = existing.id
        # Anything cold still queued for them would be a second, stranger email.
        await state.cancel_pending_outbox_for_prospect(
            prospect_id, reason="they wrote to us themselves")
        await _set_notes(state, prospect_id, notes)
        if plan.grade and plan.grade != existing.email_status:
            await state.update_prospect_email(prospect_id, lead.email, plan.grade)
    else:
        prospect_id = await state.add_prospect(Prospect(
            company_id=company_id, company=company_name, first_name=first, last_name=last,
            title=TITLE, email=lead.email, email_status=plan.grade or "guess",
            email_verified=plan.grade == "verified", phone=lead.phone[:40],
            source=source, status="new", personalization_notes=notes))
    if not (existing is not None and (existing.mailbox or "").strip()):
        mailbox = await _choose_mailbox(state, config, providers, prospect_id)
        if mailbox:
            await state.set_prospect_mailbox(prospect_id, mailbox)
    try:
        zone = zone_for(lead.city, lead.country)
    except Exception:
        zone = ""
    if zone:
        await state.set_prospect_timezone(prospect_id, zone)
    last_at = parse_time(lead.last_message_at)
    fresh = lead.origin == "website_chat" and last_at is not None and now - last_at < FRESH_FOR
    angle = INBOUND_FRESH_ANGLE if fresh else INBOUND_ANGLE
    # A written estimate quotes a price: a person approves it, whatever autopilot says.
    goes_alone = autopilot and not plan.is_estimate
    email = config.channels.email
    outbox_id = await state.add_outbox_item(
        prospect_id=prospect_id, to_email=lead.email, subject=plan.draft["subject"],
        body=plan.draft["body"], send_at=now.isoformat(),
        status="approved" if goes_alone else "pending_review", step=1, kind="sequence",
        provider=getattr(email, "provider", "") or "",
        approved_by=INBOUND_AUTOPILOT if goes_alone else "",
        angle=INBOUND_ESTIMATE_ANGLE if plan.is_estimate else angle)
    if outbox_id and not goes_alone:
        what = ESTIMATE_HELD_REASON if plan.is_estimate else f"a follow-up to {how_they_reached_us(lead)}"
        reason = f"{what}, priority: {priority_label(lead, triaged)}"
        await state.update_outbox_item(outbox_id, error=(HELD_FOR_REVIEW + reason)[:300])
    # Off the Writer's list: this is the email they get.
    await state.queue_prospect_if_unsent(prospect_id)
    return outbox_id


# ── The table ──

async def get_record(state, email: str) -> dict | None:
    async with aiosqlite.connect(state.db_path, timeout=30) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM inbound_leads WHERE email = ?", (email,)) as cursor:
            row = await cursor.fetchone()
    return dict(row) if row else None


async def save_record(state, email: str, now: datetime | None = None, **fields) -> None:
    """Upsert one lead's row; only the fields named change."""
    unknown = set(fields) - set(_RECORD_FIELDS)
    if unknown:
        raise ValueError(f"not inbound_leads columns: {sorted(unknown)}")
    stamp = (now or _utcnow()).isoformat(timespec="seconds")
    columns = list(fields) + ["processed_at"]
    values = [*fields.values(), stamp]
    marks = ", ".join("?" for _ in columns)
    updates = ", ".join(f"{c} = excluded.{c}" for c in columns)
    async with aiosqlite.connect(state.db_path, timeout=30) as db:
        await db.execute(
            f"INSERT INTO inbound_leads (email, {', '.join(columns)}) VALUES (?, {marks}) "
            f"ON CONFLICT(email) DO UPDATE SET {updates}", (email, *values))
        await db.commit()


async def _cancel_unsent(state, outbox_id: str, reason: str) -> bool:
    """Cancel a draft that hasn't gone out. False once it is on its way."""
    async with aiosqlite.connect(state.db_path, timeout=30) as db:
        cursor = await db.execute(
            "UPDATE outbox SET status = 'cancelled', error = ?, updated_at = ? "
            "WHERE id = ? AND status IN ('pending_review', 'approved')",
            (reason, _utcnow().isoformat(), outbox_id))
        await db.commit()
        return cursor.rowcount > 0


async def status_counts(state) -> dict:
    """Counts only: per status and category, and the Outbox rows they made."""
    angles = ", ".join(f"'{a}'" for a in INBOUND_ANGLES)
    async with aiosqlite.connect(state.db_path, timeout=30) as db:
        async def grouped(sql):
            async with db.execute(sql) as cursor:
                return {str(k or "none"): int(n) for k, n in await cursor.fetchall()}
        return {
            "status": await grouped("SELECT status, COUNT(*) FROM inbound_leads GROUP BY 1"),
            "category": await grouped("SELECT category, COUNT(*) FROM inbound_leads "
                                      "WHERE category != '' GROUP BY 1"),
            "outbox": await grouped("SELECT status, COUNT(*) FROM outbox "
                                    f"WHERE angle IN ({angles}) GROUP BY 1"),
            "kind": await grouped("SELECT angle, COUNT(*) FROM outbox "
                                  f"WHERE angle IN ({angles}) GROUP BY 1"),
        }


# ── A run ──

def _new_report(read: int = 0) -> dict:
    return {"status": "ok", "note": "", "read": read, "waiting": 0, "skipped": 0, "not_project": 0,
            "escalated": 0, "triaged": 0, "drafted": 0, "estimates": 0, "estimate_fallbacks": 0,
            "invalid": 0, "unverified": 0, "no_draft": 0, "cancelled": 0, "whatsapp": 0, "reasons": {},
            "previews": []}


class InboundPipeline:
    """One pass over leads. ``poll`` reads what changed, ``run`` everything,
    ``run_leads`` leads it is handed (a sheet)."""

    def __init__(self, brain, state, config, env, reader=None, now=None):
        self.brain, self.state, self.config, self.env = brain, state, config, env
        self.settings = inbound_of(config)
        self.reader = reader or ERPChatClient(getattr(env, "erp_supabase_url", ""),
                                              getattr(env, "erp_supabase_key", ""))
        self._now = now
        self.providers = MailboxProviders(config, env) if is_multi(config) else None
        self._skills: str | None = None
        self._known_people: set[str] = set()

    def now(self) -> datetime:
        value = self._now() if self._now else _utcnow()
        return value.astimezone(timezone.utc).replace(tzinfo=None) if value.tzinfo else value

    def finished(self, lead: InboundLead) -> bool:
        last = parse_time(lead.last_message_at)
        return last is None or self.now() - last >= timedelta(minutes=self.settings.idle_minutes)

    async def _read_known_people(self) -> None:
        """Our staff and existing clients, read once per run. On a failure the
        last list stands."""
        reader = getattr(self.reader, "known_people", None)
        if reader is None:
            return
        try:
            self._known_people = await reader()
        except Exception as e:
            logger.warning(f"Inbound: couldn't read our staff and clients: {e}")

    def _prefilter(self, lead: InboundLead) -> str:
        if lead.email.strip().lower() in self._known_people:
            return "one of our staff or an existing client"
        return prefilter(lead, own_domains=allowed_link_domains(self.config),
                         do_not_contact=live_do_not_contact(self.config),
                         rep_active_days=self.settings.rep_active_days, now=self.now(),
                         everyone=self.settings.everyone)

    @property
    def skills(self) -> str:
        if self._skills is None:
            self._skills = _skills_with_private(self.brain)
        return self._skills

    async def _unavailable(self) -> str:
        """Why the Brain can't be used now ("" when it can)."""
        paused = getattr(self.brain, "paused_reason", "") or ""
        if paused:
            return paused
        preflight = getattr(self.brain, "preflight", None)
        if preflight is not None:
            problem = await preflight()
            if problem:
                return problem
        usage = getattr(self.config, "usage", None)
        check = getattr(self.brain, "is_within_budget", None)
        if usage is not None and check is not None:
            pct = usage.max_daily_claude_percent
            if not await check(max(int(200 * (pct / 100)), 1), max_percent=pct):
                return "over the usage budget"
        return ""

    async def _claim(self) -> bool:
        """Take the run lock; False while another run holds a fresh one."""
        now = self.now()
        async with aiosqlite.connect(self.state.db_path, timeout=30) as db:
            cursor = await db.execute(
                "INSERT INTO settings (key, value, updated_at) VALUES (?, ?, CURRENT_TIMESTAMP) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value, "
                "updated_at = CURRENT_TIMESTAMP WHERE settings.value = '' OR settings.value < ?",
                (RUNNING_KEY, now.isoformat(timespec="seconds"),
                 (now - RUNNING_STALE).isoformat(timespec="seconds")))
            await db.commit()
            return cursor.rowcount > 0

    async def _keep_claim(self):
        await self.state.set_setting(RUNNING_KEY, self.now().isoformat(timespec="seconds"))

    async def _release(self):
        await self.state.set_setting(RUNNING_KEY, "")

    # What each lead needs, deciding nothing that costs a call.

    async def _reconcile(self, lead: InboundLead, record: dict | None, report: dict,
                         dry_run: bool) -> dict | None:
        """Bring the row up to date with its Outbox email, and cancel a draft the
        chat has overtaken. Returns the row as it now is."""
        if not record or not record.get("outbox_id"):
            return record
        row = await self.state.get_outbox_item(record["outbox_id"])
        if row is None:
            return record
        if row["status"] in ("sent", "sending"):
            if record["status"] != SENT and not dry_run:
                await save_record(self.state, lead.email, self.now(), status=SENT)
                record = {**record, "status": SENT}
            return record
        if row["status"] not in ("pending_review", "approved"):
            return record
        fingerprint = record.get("last_message_at") or ""
        if lead.last_human_at and _after(lead.last_human_at, fingerprint):
            reason, status = CANCEL_REP, SKIPPED
        elif _after(lead.fingerprint, fingerprint):
            reason, status = CANCEL_WROTE_AGAIN, REDRAFT
        else:
            return record
        report["cancelled"] += 1
        if dry_run:
            return {**record, "status": status, "outbox_id": ""}
        if not await _cancel_unsent(self.state, record["outbox_id"], reason):
            return record
        await save_record(self.state, lead.email, self.now(), status=status, why=reason, outbox_id="")
        logger.debug("Inbound: cancelled an unsent draft (%s).", reason)
        return {**record, "status": status, "why": reason, "outbox_id": ""}

    async def _skip(self, lead: InboundLead, record: dict | None, reason: str, report: dict,
                    dry_run: bool):
        report["skipped"] += 1
        report["reasons"][reason] = report["reasons"].get(reason, 0) + 1
        if dry_run:
            return
        if record and record.get("outbox_id"):
            row = await self.state.get_outbox_item(record["outbox_id"])
            if row and row["status"] in ("pending_review", "approved"):
                await _cancel_unsent(self.state, record["outbox_id"], reason)
                report["cancelled"] += 1
        await save_record(self.state, lead.email, self.now(), last_message_at=lead.fingerprint,
                          status=SKIPPED, why=reason, outbox_id="")

    async def _sort(self, lead: InboundLead, record: dict | None, report: dict, dry_run: bool,
                    retry_addresses: bool) -> tuple[str, Triage | None]:
        """Where a lead goes this pass: ("wait"|"done"|"triage"|"draft", its triage)."""
        record = await self._reconcile(lead, record, report, dry_run)
        if not self.finished(lead):
            report["waiting"] += 1
            return "wait", None
        status = (record or {}).get("status") or ""
        changed = (record is None or status == REDRAFT
                   or _after(lead.fingerprint, record.get("last_message_at") or ""))
        if status == SENT:
            if changed and not dry_run:
                await save_record(self.state, lead.email, self.now(),
                                  last_message_at=lead.fingerprint,
                                  why="they wrote again after our email: it stays with the team")
            return "done", None
        reason = self._prefilter(lead) or await already_dealt_with(self.state, lead)
        if reason:
            await self._skip(lead, record, reason, report, dry_run)
            return "done", None
        if changed:
            return "triage", None
        if status == TRIAGED:
            return "draft", Triage.from_record(record)
        attempts = int((record or {}).get("attempts") or 0)
        if status == TRIAGE_FAILED:
            return ("triage", None) if attempts < MAX_ATTEMPTS else ("done", None)
        if status == NO_DRAFT:
            return ("draft", Triage.from_record(record)) if attempts < MAX_ATTEMPTS else ("done", None)
        if status == UNVERIFIED and (retry_addresses or self.settings.everyone):
            return "draft", Triage.from_record(record)
        if status == SKIPPED:     # whatever skipped them has passed
            return "triage", None
        if (status == NOT_PROJECT and (record or {}).get("category") in
                _emailed_categories(self.settings.everyone)):
            return "draft", Triage.from_record(record)    # inbound.everyone was turned on since
        return "done", None

    async def _triage_one(self, lead: InboundLead, record: dict | None, report: dict,
                          dry_run: bool) -> tuple[Triage | None, bool]:
        """(the triage when it is a project, whether this lead is decided)."""
        offer = getattr(getattr(self.config, "persona", None), "company", "") or ""
        result = await triage(self.brain, lead, offer)
        now = self.now()
        if result is None:
            # New messages start the count again.
            same = record and not _after(lead.fingerprint, record.get("last_message_at") or "")
            attempts = int(record.get("attempts") or 0) + 1 if same else 1
            if not dry_run:
                await save_record(self.state, lead.email, now, last_message_at=lead.fingerprint,
                                  status=TRIAGE_FAILED, attempts=attempts, outbox_id="",
                                  why="the model gave no usable answer")
            return None, attempts >= MAX_ATTEMPTS
        common = dict(last_message_at=lead.fingerprint, outbox_id="", attempts=0, **result.fields())
        if result.category in _emailed_categories(self.settings.everyone):
            report["triaged"] += 1
            if not dry_run:
                await save_record(self.state, lead.email, now, status=TRIAGED, why="", **common)
                await self._meeting_alert(lead, result)
            return result, False
        escalated = result.category in ESCALATED_CATEGORIES
        report["escalated" if escalated else "not_project"] += 1
        if not dry_run:
            await save_record(self.state, lead.email, now, status=ESCALATED if escalated else NOT_PROJECT,
                              why=f"triage: {result.category}", **common)
            if escalated:
                await self.state.log_action(
                    action_type="escalation", agent=AGENT,
                    details={"prospect_email": lead.email,
                             "reason": f"{how_they_reached_us(lead)}: looks like a "
                                       f"{result.category} matter, not a new project: {result.summary}"})
        return None, True

    async def _meeting_alert(self, lead: InboundLead, triaged: Triage) -> None:
        """A visitor who asked for a call or a time in the chat: a WhatsApp
        message to your phone (harvey/meeting_alert.py). Never raises."""
        from harvey.meeting_alert import maybe_alert
        theirs = [line.text for line in lead.transcript if line.who == VISITOR]
        if not theirs:
            return
        name = lead.name or lead.email
        await maybe_alert(
            self.state, self.brain, self.config, channel=how_they_reached_us(lead).capitalize(),
            key=f"inbound:{lead.email.lower()}", name=name, company=lead.company,
            latest="\n".join(theirs[-3:]), thread=transcript_text(lead),
            contacts=[lead.email, lead.phone], where=f"Harvey dashboard, Website leads tab, {lead.email}",
            notes=" ".join(filter(None, [journey_note(lead), triaged.summary])),
            service_hint=triaged.service_type if triaged.service_type != "other" else "")

    async def _queue_whatsapp(self, lead: InboundLead, triaged: Triage, report: dict):
        """A project lead who left a number joins the WhatsApp queue once a
        number is linked (channels.whatsapp.number), with that channel's own
        approval and limits. It runs even when their email address is bad:
        WhatsApp is then the only way left. Never stops the email."""
        whatsapp = getattr(getattr(self.config, "channels", None), "whatsapp", None)
        if not (lead.phone and getattr(whatsapp, "number", "")):
            return
        from harvey.whatsapp.contacts import queue_contact
        try:
            result = await queue_contact(
                self.state, phone=lead.phone, name=lead.name, email=lead.email,
                context=_whatsapp_context(lead, triaged), source=how_they_reached_us(lead),
                country=lead.country, config=self.config)
        except Exception as e:
            logger.warning(f"Inbound: couldn't queue {lead.email} for WhatsApp: {e}")
            return
        report["whatsapp"] += result.queued

    async def _store(self, plan: Plan, report: dict):
        lead, triaged = plan.lead, plan.triaged
        now = self.now()
        await self._queue_whatsapp(lead, triaged, report)
        if plan.status:
            # Triage just reset the count when they wrote something new.
            current = await get_record(self.state, lead.email) or {}
            attempts = int(current.get("attempts") or 0) + 1 if plan.status == NO_DRAFT else 0
            report[{INVALID: "invalid", UNVERIFIED: "unverified", NO_DRAFT: "no_draft"}[plan.status]] += 1
            await save_record(self.state, lead.email, now, last_message_at=lead.fingerprint,
                              status=plan.status, why=plan.why, attempts=attempts, outbox_id="",
                              **triaged.fields())
            return
        outbox_id = await stage(self.state, self.config, plan, now=now, providers=self.providers,
                                autopilot=self.settings.autopilot)
        if not outbox_id:
            report["skipped"] += 1
            await save_record(self.state, lead.email, now, last_message_at=lead.fingerprint,
                              status=SKIPPED, why="already dealt with by the time it was drafted")
            return
        report["drafted"] += 1
        report["estimates"] += plan.is_estimate
        report["estimate_fallbacks"] += bool(plan.why)
        await save_record(self.state, lead.email, now, last_message_at=lead.fingerprint,
                          status=DRAFTED, why=plan.why, outbox_id=outbox_id, attempts=0,
                          **triaged.fields())
        await self.state.log_action(action_type="inbound_draft", agent=AGENT, details={
            "outbox_id": outbox_id, "prospect_email": lead.email,
            "priority": priority_label(lead, triaged), "estimate": plan.is_estimate,
            "status": ("approved" if self.settings.autopilot and not plan.is_estimate
                       else "pending_review")})

    async def process(self, leads: list[InboundLead], *, limit: int | None = None,
                      max_triage: int | None = None, dry_run: bool = False,
                      retry_addresses: bool = False) -> dict:
        """Take ``leads`` as far as they can go now. ``limit``: how many reach a
        draft. ``max_triage``: how many model calls triage may make. Keeps
        ``_carried`` (leads not decided yet) and ``_waiting`` (chats still going)
        for the cursor. A stop part-way (the provider going down) loses nothing:
        what was decided is stored, and the rest is read again next time."""
        report = _new_report(len(leads))
        await self._read_known_people()
        decided: set[str] = set()
        waiting: list[InboundLead] = []
        records: dict[str, dict | None] = {}
        to_triage: list[InboundLead] = []
        ready: list[tuple[InboundLead, Triage]] = []
        for lead in leads:
            records[lead.email] = await get_record(self.state, lead.email)
            step, triaged = await self._sort(lead, records[lead.email], report, dry_run,
                                             retry_addresses)
            if step == "wait":
                waiting.append(lead)
            elif step == "done":
                decided.add(lead.email)
            elif step == "triage":
                to_triage.append(lead)
            else:
                ready.append((lead, triaged))
        report["_waiting"] = waiting
        calls = 0
        try:
            if to_triage or ready:
                problem = await self._unavailable()
                if problem:
                    raise BrainUnavailable(problem)
            for lead in sorted(to_triage, key=_triage_order):
                if ((limit is not None and len(ready) >= limit)
                        or (max_triage is not None and calls >= max_triage)):
                    break
                problem = await self._unavailable()
                if problem:
                    raise BrainUnavailable(problem)
                calls += 1
                triaged, done = await self._triage_one(lead, records[lead.email], report, dry_run)
                if not dry_run:
                    await self._keep_claim()
                if done:
                    decided.add(lead.email)
                if triaged is not None:
                    ready.append((lead, triaged))
            ready.sort(key=lambda pair: priority_key(*pair))
            for lead, triaged in (ready if limit is None else ready[:limit]):
                problem = await self._unavailable()
                if problem:
                    raise BrainUnavailable(problem)
                plan = await prepare_lead(self.state, self.brain, self.config, self.env, lead,
                                          triaged, self.skills, check_address=not dry_run)
                if dry_run:
                    report["previews"].append(self._preview(plan))
                else:
                    await self._store(plan, report)
                    await self._keep_claim()
                decided.add(lead.email)
        except BrainUnavailable as e:
            report["status"] = "paused"
            report["note"] = f"the brain can't be used now: {e}"
            logger.info(f"Inbound: stopped for now ({e}); what's left is read again next time.")
        report["_carried"] = [lead for lead in leads if lead.email not in decided]
        return report

    @staticmethod
    def _preview(plan: Plan) -> dict:
        return {"priority": priority_label(plan.lead, plan.triaged), "email": plan.lead.email,
                "summary": plan.triaged.summary, "why": plan.why, "estimate": plan.is_estimate,
                "subject": (plan.draft or {}).get("subject", ""),
                "body": (plan.draft or {}).get("body", "")}

    async def _move_cursor(self, leads: list[InboundLead], hold: list[InboundLead]):
        """Past everything read, but never past a lead still owed a look."""
        seen = [t for t in (parse_time(lead.last_message_at) for lead in leads) if t]
        held = [t for t in (parse_time(lead.last_message_at) for lead in hold) if t]
        if held:
            target = min(held)
        elif seen:
            target = max(seen)
        else:
            return
        await self.state.set_setting(CURSOR_KEY, target.isoformat())

    async def _record(self, report: dict) -> dict:
        public = {k: v for k, v in report.items() if not k.startswith("_") and k != "previews"}
        public["at"] = self.now().isoformat(timespec="seconds")
        await self.state.set_setting(LAST_RUN_KEY, json.dumps(public, sort_keys=True))
        return report

    async def poll(self) -> dict:
        """One poll: the chats that changed since the last one. Starts from the
        last day when it has never run: the older backlog is `harvey inbound run`'s."""
        if not await self._claim():
            return {**_new_report(), "status": "busy", "note": "another inbound run is going"}
        try:
            raw = await self.state.get_setting(CURSOR_KEY)
            cursor = parse_time(raw)
            if cursor is None:
                cursor = self.now() - FIRST_POLL_LOOKBACK
                await self.state.set_setting(CURSOR_KEY, cursor.isoformat())
            leads = await self.reader.read_leads(since=(cursor - CURSOR_OVERLAP).isoformat())
            report = await self.process(leads, limit=MAX_PER_POLL, max_triage=MAX_PER_POLL)
            await self._move_cursor(leads, report["_carried"])
            return await self._record(report)
        finally:
            await self._release()

    async def run(self, *, limit: int | None = None, dry_run: bool = False) -> dict:
        """The backlog: everyone with an address, as far as ``limit`` allows. Safe
        to run again: what is decided stays decided until they write again."""
        if dry_run:
            leads = await self.reader.read_leads()
            return await self.process(leads, limit=DRY_RUN_LIMIT if limit is None else limit,
                                      dry_run=True)
        if not await self._claim():
            return {**_new_report(), "status": "busy", "note": "another inbound run is going"}
        try:
            leads = await self.reader.read_leads()
            report = await self.process(leads, limit=limit, retry_addresses=True)
            # The poll picks up from here; a chat still going stays in its view.
            await self._move_cursor(leads, report["_waiting"])
            return await self._record(report)
        finally:
            await self._release()

    async def run_leads(self, leads: list[InboundLead], *, limit: int | None = None,
                        dry_run: bool = False) -> dict:
        """Leads from a sheet."""
        if dry_run:
            return await self.process(leads, limit=DRY_RUN_LIMIT if limit is None else limit,
                                      dry_run=True)
        if not await self._claim():
            return {**_new_report(), "status": "busy", "note": "another inbound run is going"}
        try:
            return await self._record(await self.process(leads, limit=limit, retry_addresses=True))
        finally:
            await self._release()


class InboundRunner:
    """The background poll: its own timer, so a long cycle, quiet hours or a
    paused brain never hold it up, and it never holds up a send. One at a time:
    each poll finishes before the next wait begins."""

    def __init__(self, brain, state, config, env, pipeline_factory=None):
        self.brain, self.state, self.config, self.env = brain, state, config, env
        self.settings = inbound_of(config)
        self.pipeline_factory = pipeline_factory or (lambda: InboundPipeline(brain, state, config, env))
        self.poll_seconds = self.settings.poll_minutes * 60
        self.task: asyncio.Task | None = None

    def problem(self) -> str:
        """Why it can't poll, or "" (it is switched on and has what it needs)."""
        if not self.settings.enabled:
            return "channels.email.inbound.enabled is off"
        email = getattr(getattr(self.config, "channels", None), "email", None)
        if not getattr(email, "enabled", False) or getattr(email, "provider", "") not in NATIVE_PROVIDERS:
            return "it needs the email channel on, with Gmail, Microsoft 365 or SMTP"
        if not (getattr(self.env, "erp_supabase_url", "") and getattr(self.env, "erp_supabase_key", "")):
            return "ERP_SUPABASE_URL and ERP_SUPABASE_KEY aren't set in .env"
        return ""

    @property
    def running(self) -> bool:
        return self.task is not None and not self.task.done()

    def start(self) -> bool:
        if self.running:
            return True
        problem = self.problem()
        if problem:
            if self.settings.enabled:
                logger.warning(f"Inbound: not polling: {problem}.")
            return False
        logger.info(f"Inbound: polling the website chat every {self.settings.poll_minutes} minutes.")
        self.task = asyncio.create_task(self._loop())
        return True

    async def poll_once(self) -> dict | None:
        """One poll, bounded; a failure is logged and the next one tries again.
        A failure is also kept (LAST_ERROR_KEY) for the Website leads tab, and
        cleared by the next poll that works."""
        problem = ""
        try:
            report = await asyncio.wait_for(self.pipeline_factory().poll(), timeout=POLL_TIMEOUT_SECONDS)
            await self._note_error("")
            return report
        except asyncio.TimeoutError:
            problem = f"a read ran past {POLL_TIMEOUT_SECONDS}s and was stopped"
            logger.warning(f"Inbound: a poll ran past {POLL_TIMEOUT_SECONDS}s and was stopped.")
        except asyncio.CancelledError:
            raise
        except ERPChatError as e:
            problem = f"couldn't read the chat: {e}"
            logger.warning(f"Inbound: couldn't read the chat: {e}")
        except Exception as e:
            problem = f"the poll failed: {e}"
            logger.error(f"Inbound: a poll failed: {e}", exc_info=e)
        await self._note_error(problem)
        return None

    async def _note_error(self, problem: str) -> None:
        try:
            if problem:
                await self.state.set_setting(LAST_ERROR_KEY, json.dumps(
                    {"at": _utcnow().isoformat(timespec="seconds"), "error": problem[:300]}))
            elif await self.state.get_setting(LAST_ERROR_KEY):
                await self.state.delete_setting(LAST_ERROR_KEY)
        except Exception:
            pass

    async def _loop(self):
        while True:
            await self.poll_once()
            await asyncio.sleep(self.poll_seconds)

    async def shutdown(self):
        if self.running:
            self.task.cancel()
            try:
                await self.task
            except (asyncio.CancelledError, Exception):
                pass


# ── A sheet: `harvey inbound import FILE` ──

_HEADERS = {
    "name": ("name", "full name", "contact", "contact name", "client", "client name", "lead",
             "lead name", "person"),
    "first": ("first name", "firstname", "given name"),
    "last": ("last name", "lastname", "surname", "family name"),
    "email": ("email", "e mail", "email address", "e mail address", "mail", "work email",
              "contact email", "business email"),
    "phone": ("phone", "phone number", "mobile", "mobile number", "contact number", "whatsapp",
              "telephone", "tel", "cell"),
    "company": ("company", "company name", "business", "business name", "organisation",
                "organization", "brand"),
    "website": ("website", "site", "url", "web", "domain", "company website", "website url"),
    "message": ("message", "messages", "notes", "note", "requirements", "requirement", "brief",
                "details", "description", "project", "project details", "comments", "comment",
                "enquiry", "inquiry", "summary"),
    "source": ("source", "lead source", "channel", "platform", "directory", "from", "via"),
    "date": ("date", "created", "created at", "date added", "received", "received at",
             "enquiry date", "inquiry date", "submitted", "submitted at", "timestamp"),
}
_ALIASES = {alias: field_name for field_name, names in _HEADERS.items() for alias in names}
_ADDRESS_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")


def _header_key(value) -> str:
    return " ".join(re.sub(r"[^a-z0-9]+", " ", str(value or "").lower()).split())


def _rows_of(path: Path) -> list[list[list]]:
    """The file as sheets of rows."""
    if path.suffix.lower() in (".xlsx", ".xlsm"):
        from harvey.linkedin.importer import read_xlsx_sheet, xlsx_sheet_names
        return [read_xlsx_sheet(path, name) for name in xlsx_sheet_names(path)]
    with open(path, newline="", encoding="utf-8-sig", errors="replace") as f:
        return [list(csv.reader(f))]


def _columns(header: list) -> dict[str, list[int]]:
    """{field: its column numbers}. Several message columns are all kept."""
    columns: dict[str, list[int]] = {}
    for index, value in enumerate(header):
        name = _ALIASES.get(_header_key(value))
        if name:
            columns.setdefault(name, []).append(index)
    return columns


def read_sheet(path, source: str = "", zone: str = "UTC") -> list[InboundLead]:
    """Leads from a CSV or XLSX with flexible headers (name, email, phone, company,
    website, message or notes or requirements, source, date), one per address.
    The owner's notes stand in for the chat. A source naming a directory sets
    ``directory``. Raises ValueError when nothing in the file has an email column."""
    from harvey.linkedin.importer import parse_when
    path = Path(path)
    leads: dict[str, InboundLead] = {}
    found_header = False
    for rows in _rows_of(path):
        at = next((i for i, row in enumerate(rows[:30]) if "email" in _columns(row)), None)
        if at is None:
            continue
        found_header = True
        columns = _columns(rows[at])

        def cell(row, name):
            values = (row[i] if i < len(row) else "" for i in columns.get(name, ()))
            return [str(v).strip() for v in values if str(v).strip()]

        for row in rows[at + 1:]:
            found = _ADDRESS_RE.search(" ".join(cell(row, "email")))
            if not found:
                continue
            email = found.group(0).lower()
            name = " ".join(cell(row, "name")[:1] or cell(row, "first") + cell(row, "last"))
            notes = "\n".join(cell(row, "message"))
            raw_date = next((row[i] for i in columns.get("date", ()) if i < len(row) and row[i] != ""), "")
            when = parse_when(raw_date, zone=zone)
            label = (cell(row, "source") or [source.strip()])[0]
            lead = leads.setdefault(email, InboundLead(email=email, origin="sheet"))
            lead.name = lead.name or name
            lead.phone = lead.phone or " ".join(cell(row, "phone")[:1])
            lead.company = lead.company or " ".join(cell(row, "company")[:1])
            lead.website = lead.website or " ".join(cell(row, "website")[:1])
            lead.source_label = lead.source_label or label
            lead.referrer = lead.referrer or label     # a job board named here counts
            lead.directory = lead.directory or directory_in(label)
            stamp = when.isoformat() if when else ""
            if notes:
                lead.transcript.append(ChatLine(NOTES, notes, stamp))
            if stamp and _after(stamp, lead.last_message_at):
                lead.last_message_at = lead.last_visitor_at = stamp
    if not found_header:
        raise ValueError(f"{path.name}: no header row with an email column")
    return list(leads.values())


# ── `harvey inbound status|run|import` ──

def add_cli_parser(subparsers):
    sub = subparsers.add_parser(
        "inbound", help="Follow up with people who gave their email in the website chat")
    sub.add_argument("inbound_action", choices=["status", "run", "import"],
                     help="status: counts only; run: the backlog, resumable; import: a CSV or XLSX")
    sub.add_argument("file", nargs="?", default="", help="import: the CSV or XLSX")
    sub.add_argument("--limit", type=int, default=None, metavar="N",
                     help="run/import: draft at most N this time")
    sub.add_argument("--dry-run", action="store_true",
                     help="run/import: triage and draft a few and print them; nothing is stored, "
                          "verified or queued")
    sub.add_argument("--source", default="", metavar="NAME",
                     help="import: where these came from, for rows with no source column")
    sub.set_defaults(func=cmd_inbound)
    return sub


def _print_report(report: dict):
    print(f"\n  Read {report['read']} lead(s). {report['drafted']} drafted ({report['estimates']} "
          f"written estimates, {report['estimate_fallbacks']} fell back to an email), "
          f"{report['triaged']} new projects, {report['waiting']} chats still going.")
    print(f"  Skipped {report['skipped']}, not a project {report['not_project']}, escalated "
          f"{report['escalated']}, invalid address {report['invalid']}, unverified "
          f"{report['unverified']}, no usable draft {report['no_draft']}, drafts cancelled "
          f"{report['cancelled']}, queued for WhatsApp {report['whatsapp']}.")
    for reason, n in sorted(report["reasons"].items(), key=lambda x: -x[1]):
        print(f"    {n:>5}  {reason}")
    if report["status"] != "ok" or report["note"]:
        print(f"  {report['status']}: {report['note']}")
    for preview in report["previews"]:
        kind = "written estimate" if preview["estimate"] else "email"
        print(f"\n  --- {preview['email']} [{preview['priority']}, {kind}]\n  {preview['summary']}")
        if preview["why"]:
            print(f"  Note: {preview['why']}")
        if preview["subject"]:
            print(f"  Subject: {preview['subject']}\n\n{preview['body']}\n")


def cmd_inbound(args):
    from harvey.brain import Brain
    from harvey.config import load_config, load_env
    from harvey.state import StateManager

    async def _status():
        state = StateManager()
        await state.init_db()
        config = load_config()
        counts = await status_counts(state)
        print(f"\n  Polling: {'on' if inbound_of(config).enabled else 'off'}. "
              f"Autopilot: {'on' if inbound_of(config).autopilot else 'off'}.")
        for label, key in (("Leads", "status"), ("Triage", "category"), ("Outbox", "outbox"),
                           ("Kinds of email", "kind")):
            print(f"\n  {label}:")
            if not counts[key]:
                print("    none yet")
            for name, n in sorted(counts[key].items(), key=lambda x: -x[1]):
                print(f"    {n:>5}  {name}")
        print()

    async def _work():
        config, env, state = load_config(), load_env(), StateManager()
        await state.init_db()
        email = config.channels.email
        if not email.enabled or email.provider not in NATIVE_PROVIDERS:
            print("\n  Inbound follow-ups go through the Outbox, so they need the email channel "
                  "on with gmail, microsoft or smtp.\n")
            return
        pipeline = InboundPipeline(Brain(state, config.brain), state, config, env)
        if args.inbound_action == "run":
            report = await pipeline.run(limit=args.limit, dry_run=args.dry_run)
        else:
            if not args.file:
                print("\n  Usage: harvey inbound import FILE [--source NAME]\n")
                return
            leads = read_sheet(args.file, source=args.source, zone=operator_timezone(config))
            report = await pipeline.run_leads(leads, limit=args.limit, dry_run=args.dry_run)
        _print_report(report)
        if report["drafted"] and not inbound_of(config).autopilot:
            print("\n  Each draft waits for you: `harvey outbox` or the dashboard's Outbox tab.\n")

    asyncio.run(_status() if args.inbound_action == "status" else _work())
