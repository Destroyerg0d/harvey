"""A client chat you paste into the dashboard: Harvey reads it, looks at their
website, and emails them (and messages them on WhatsApp when you give a number).

It is the website-chat follow-up (harvey/inbound.py) for a chat Harvey didn't
read itself: the same triage, draft rules, price list and Outbox, with your note
for Harvey added to what the draft is told. The text can be a chat copied from
the ERP's client inbox, a "Name: message" chat (WhatsApp, email), or plain notes.

Your choice (Nov 2026): a pasted lead's email sends on its own, like the website
chat on autopilot; a written estimate still waits for you. The checks that always
apply still do: opt-outs, bounces, the do-not-contact list. Someone Harvey already
wrote to, or one of our staff or clients, asks you first.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone

from harvey.collectors.erp_chat import (
    ASSISTANT, NOTES, VISITOR, ChatLine, ERPChatClient, InboundLead,
)

logger = logging.getLogger("harvey.pasted")

ORIGIN = "pasted"
SOURCE = "inbound:pasted"
MAX_CHAT_CHARS = 20000
# Where the chat happened, as the email and the WhatsApp message say it.
WHERE = {
    "website": "the live chat on our website",
    "whatsapp": "a WhatsApp chat with us",
    "email": "an email to us",
    "call": "a call with us",
    "other": "a conversation with us",
}

_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
_PHONE_RE = re.compile(r"^\+?\d[\d\s().\-]{6,}\d$")
_TIME_RE = re.compile(r"^\d{1,2}:\d{2}(?:\s?[ap]m)?$", re.IGNORECASE)
_SAID_RE = re.compile(r"^(?:\[[^\]]{1,40}\]\s*)?([^:\[\]]{1,40}?):\s+(.+)$")
# The inbox's own notices (a line starting so), and its one-word labels (the
# whole line), never anyone's words. "Open to a quick chat?" is someone's.
_NOTICE_RE = re.compile(
    r"^(?:visitor (?:shared|asked|left|opened|joined)|translated from|ai reply translated|"
    r"faster ways to reach them|shared by the visitor|reach them on|transcript sent|"
    r"translate: |ai: (?:on|off)|came from$|· )", re.IGNORECASE)
_LABELS = frozenset({"open", "whatsapp", "read", "today", "yesterday", "saved", "resolve",
                     "take over", "show original", "›", "email", "contact", "live chat"})
MARK_OURS, MARK_THEIRS = "you", "?"


@dataclass
class Pasted:
    lines: list[ChatLine] = field(default_factory=list)
    name: str = ""
    email: str = ""
    phone: str = ""
    city: str = ""
    country: str = ""
    came_from: str = ""
    via: str = ""
    shape: str = ""          # inbox / named / notes


def _strip_country(name: str, country: str) -> str:
    """The inbox runs the country into the name: "Ann LeeUnited States"."""
    if country and name.endswith(country) and len(name) > len(country):
        return name[: -len(country)].strip()
    return name


def _header(lines: list[str], out: Pasted) -> None:
    for i, raw in enumerate(lines):
        line = raw.strip()
        if not line:
            continue
        found = _EMAIL_RE.search(line)
        if found and not out.email:
            out.email = found.group(0)
            continue
        if _PHONE_RE.match(line) and not out.phone:
            out.phone = line
            continue
        if line.lower() == "came from" and i + 1 < len(lines):
            out.came_from = lines[i + 1].strip()
            continue
        if line.lower().startswith("· via "):
            out.via = line[6:].strip()
            continue
        if line.count(",") >= 1 and not out.country and "@" not in line and len(line) < 120:
            parts = [p.strip() for p in line.split(",") if p.strip()]
            if len(parts) >= 2:
                out.city, out.country = parts[0], parts[-1]
    first = next((l.strip() for l in lines if l.strip()), "")
    if first and "@" not in first and not _PHONE_RE.match(first):
        out.name = _strip_country(first, out.country)


def _inbox(lines: list[str], visitor: str, out: Pasted) -> bool:
    """The ERP inbox's copy: "You" or "?" (then their name) opens a message, a
    time closes it, and the rep's name follows the time on ours."""
    starts = [i for i, l in enumerate(lines) if l.strip().lower() in (MARK_OURS, MARK_THEIRS)]
    if not starts:
        return False
    _header(lines[: starts[0]], out)
    who, text, skip_name, after_time = None, [], False, False

    def flush():
        nonlocal text
        words = " ".join(" ".join(text).split())
        if who and words:
            out.lines.append(ChatLine(who, words))
        text = []

    for raw in lines[starts[0]:]:
        line = raw.strip()
        low = line.lower()
        if not line:
            continue
        if low == MARK_OURS:
            flush()
            who, skip_name, after_time = ASSISTANT, False, False
            continue
        if low == MARK_THEIRS:
            flush()
            who, skip_name, after_time = VISITOR, True, False
            continue
        if skip_name:          # their name, under the "?"
            skip_name = False
            continue
        if _TIME_RE.match(line):
            flush()
            after_time = who == ASSISTANT
            continue
        if after_time:         # the rep's name, under our message's time
            after_time = False
            if len(line.split()) <= 4 and not _NOTICE_RE.match(line):
                continue
        if _PHONE_RE.match(line):      # a number they shared ("Faster ways to reach them")
            out.phone = out.phone or line
            continue
        if _NOTICE_RE.match(line) or low in _LABELS:
            continue
        text.append(line)
    flush()
    out.shape = "inbox"
    return bool(out.lines)


def _named(lines: list[str], visitor: str, out: Pasted) -> bool:
    """"Name: message" lines (a WhatsApp export, an email thread copied out)."""
    said = [(i, _SAID_RE.match(l.strip())) for i, l in enumerate(lines)]
    named = [(i, m) for i, m in said if m]
    speakers = [m.group(1).strip().lower() for _, m in named]
    # A conversation goes back and forth between a few people; "Budget: ...",
    # "Timeline: ..." notes name each label once.
    if (len(named) < 2 or len(set(speakers)) > 3
            or max(speakers.count(s) for s in set(speakers)) < 2):
        return False
    ours = {"you", "me", "us", "digital heroes", "team"}
    them = (visitor or "").strip().lower()
    if not them:
        others = [m.group(1).strip().lower() for _, m in named if m.group(1).strip().lower() not in ours]
        them = others[0] if others else ""
    first_token = them.split()[0] if them else ""
    for raw in lines:
        line = raw.strip()
        if not line:
            continue
        m = _SAID_RE.match(line)
        if m:
            speaker = m.group(1).strip().lower()
            theirs = bool(them) and (speaker == them or (first_token and speaker.split()[0] == first_token))
            out.lines.append(ChatLine(VISITOR if theirs else ASSISTANT, m.group(2).strip()))
        elif out.lines:
            out.lines[-1].text += " " + line
    out.shape = "named"
    return bool(out.lines)


def parse_pasted_chat(text: str, visitor: str = "") -> Pasted:
    """The conversation in what you pasted, whatever its shape. Nothing is
    dropped: text with no speakers at all is kept whole as your notes."""
    out = Pasted()
    lines = (text or "")[:MAX_CHAT_CHARS].replace("\r\n", "\n").split("\n")
    if _inbox(lines, visitor, out) or _named(lines, visitor, out):
        return out
    words = " ".join((text or "").split())
    if words:
        out.lines = [ChatLine(NOTES, words[:MAX_CHAT_CHARS])]
    out.shape = "notes"
    return out


# ── Doing it ──

def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


async def site_facts(state, config, company_id: str, domain: str) -> str:
    """What their own website shows, from Harvey's profiler (no model call):
    a few lines for the draft, or ""."""
    from harvey.collectors.profile import profile_companies
    try:
        await profile_companies(state, [{"id": company_id, "domain": domain, "name": ""}], config)
    except Exception as e:
        logger.info(f"Pasted chat: couldn't read {domain}: {e}")
    try:
        obs = await state.get_observations(company_id=company_id, latest_only=True, limit=40)
    except Exception:
        return ""
    keep = []
    for o in obs:
        value = str(o.get("value_text") or "").strip()
        if not value and o.get("value_num") not in (None, ""):
            value = str(o.get("value_num"))
        if value and o.get("signal_code") not in ("EMAIL_STATUS",):
            keep.append(f"{o['signal_code']}: {value[:160]}")
    return "; ".join(keep[:12])[:900]


async def checks(state, config, env, lead: InboundLead) -> tuple[str, list[str]]:
    """(why it can't go at all, what to ask you first). No model calls."""
    from harvey.config import live_do_not_contact
    from harvey.gate import do_not_contact_match
    from harvey.inbound import already_dealt_with
    blocked = do_not_contact_match(lead.email, live_do_not_contact(config))
    if blocked:
        return f"{lead.email} is on your do-not-contact list", []
    existing = await state.get_prospect_by_email(lead.email)
    if existing is not None and existing.status == "opted_out":
        return "they opted out: Harvey won't write to them", []
    if existing is not None and (existing.email_status or "") == "invalid":
        return "their address bounced before", []
    ask = []
    dealt = await already_dealt_with(state, lead)
    if dealt:
        ask.append(f"Harvey has dealt with them before ({dealt})")
    reader = ERPChatClient(getattr(env, "erp_supabase_url", ""), getattr(env, "erp_supabase_key", ""))
    if reader.configured:
        try:
            if lead.email.lower() in await reader.known_people():
                ask.append("they're one of our staff or an existing client in the ERP")
        except Exception as e:
            logger.info(f"Pasted chat: couldn't check the ERP's staff and clients: {e}")
    return "", ask


def build_lead(pasted: Pasted, *, email: str, phone: str = "", name: str = "", website: str = "",
               company: str = "", note: str = "", where: str = "website",
               now: datetime | None = None) -> InboundLead:
    at = (now or _now()).isoformat()
    lines = list(pasted.lines)
    path = pasted.came_from.split("/", 1)[1] if "/" in pasted.came_from else ""
    return InboundLead(
        email=email.strip(), name=(name or pasted.name).strip(), phone=(phone or pasted.phone).strip(),
        country=pasted.country, city=pasted.city, referrer=pasted.via,
        entry_path=("/" + path) if path else "", transcript=lines,
        last_message_at=at, last_visitor_at=at, company=company.strip(), website=website.strip(),
        origin=ORIGIN, source_label=WHERE.get(where, WHERE["other"]),
        owner_note=" ".join((note or "").split())[:1000])


async def run_pasted(state, brain, config, env, lead: InboundLead, *, send_anyway: bool = False,
                     now: datetime | None = None, providers=None) -> dict:
    """Research, triage, draft and stage one pasted lead. Returns what happened:
    {status, why, outbox_id, estimate, subject, body, summary, whatsapp}."""
    from harvey import inbound as I
    from harvey.mailboxes import MailboxProviders, is_multi
    now = now or _now()
    refused, ask = await checks(state, config, env, lead)
    if refused:
        return {"status": "refused", "why": refused}
    if ask and not send_anyway:
        return {"status": "confirm", "why": "; ".join(ask)}
    domain = ""
    if I.is_business_email(lead.email):
        domain = I.registrable_domain(I._email_domain(lead.email))
    elif lead.website:
        domain = I.registrable_domain(re.sub(r"^https?://", "", lead.website.strip()).split("/")[0].lower())
    if domain:
        from harvey.models.company import Company
        company_id = await state.add_company(Company(
            name=lead.company or I._company_name(domain), domain=domain,
            website=lead.website or f"https://{domain}", source=SOURCE))
        lead.site_facts = await site_facts(state, config, company_id, domain)
    offer = getattr(getattr(config, "persona", None), "company", "") or ""
    triaged = await I.triage(brain, lead, offer)
    if triaged is None:
        return {"status": "failed", "why": "the model gave no usable reading of the chat; try again"}
    if triaged.category != "project":
        if not send_anyway:
            return {"status": "not_project", "why": f"Harvey reads this as {triaged.category}, not a lead",
                    "summary": triaged.summary, "category": triaged.category}
        triaged.category = "project"
    plan = await I.prepare_lead(state, brain, config, env, lead, triaged,
                                I._skills_with_private(brain))
    whatsapp = await _whatsapp(state, config, lead, triaged)
    if plan.status:
        await I.save_record(state, lead.email, now, last_message_at=lead.fingerprint,
                            status=plan.status, why=plan.why, outbox_id="", **triaged.fields())
        return {"status": plan.status, "why": plan.why, "summary": triaged.summary, "whatsapp": whatsapp}
    if providers is None and is_multi(config):
        providers = MailboxProviders(config, env)
    outbox_id = await I.stage(state, config, plan, now=now, providers=providers, autopilot=True,
                              force=send_anyway)
    if not outbox_id:
        return {"status": "skipped", "why": "someone dealt with them while Harvey was writing",
                "summary": triaged.summary, "whatsapp": whatsapp}
    await I.save_record(state, lead.email, now, last_message_at=lead.fingerprint, status=I.DRAFTED,
                        why="a chat you pasted" + (f"; {plan.why}" if plan.why else ""),
                        outbox_id=outbox_id, **triaged.fields())
    await state.log_action("pasted_chat", "dashboard", {
        "outbox_id": outbox_id, "estimate": plan.is_estimate, "shape": "",
        "researched": bool(lead.site_facts), "whatsapp": whatsapp})
    return {"status": "drafted", "why": plan.why, "outbox_id": outbox_id, "estimate": plan.is_estimate,
            "subject": plan.draft["subject"], "body": plan.draft["body"], "summary": triaged.summary,
            "whatsapp": whatsapp, "researched": bool(lead.site_facts)}


async def _whatsapp(state, config, lead: InboundLead, triaged) -> str:
    """Queue them for WhatsApp when you gave a number: "" without one."""
    if not lead.phone:
        return ""
    whatsapp = getattr(getattr(config, "channels", None), "whatsapp", None)
    if not getattr(whatsapp, "number", ""):
        return "not queued: no WhatsApp number is set up for Harvey"
    from harvey.inbound import how_they_reached_us
    from harvey.whatsapp.contacts import queue_contact
    try:
        result = await queue_contact(state, phone=lead.phone, name=lead.name, email=lead.email,
                                     context=triaged.summary, source=how_they_reached_us(lead),
                                     country=lead.country, config=config)
    except ValueError as e:
        return f"not queued: {e}"
    return result.status + (f": {result.reason}" if getattr(result, "reason", "") else "")
