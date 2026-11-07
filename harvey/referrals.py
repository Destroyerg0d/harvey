"""Referrals: a "wrong person" reply that names who to write to instead.

"Not me, write to sam@acme.example, she runs the shop" used to get a polite
"who would be the right person?" and nothing else. Now the person it names
becomes a prospect, by these rules:

- Read only the replier's own words: no quoted email, no signature, and no
  "for urgent matters contact ..." (an out-of-office's line, not a referral).
  Our own addresses and domains, the replier's own address and the address
  Harvey emailed are never a referral. A generic mailbox (info@, support@)
  counts only when the reply offers it as the one to write to.
- The address must be at the company's own domain (the same registrable
  domain as the company's site or the address Harvey emailed). Anyone else
  (an agency, a freelancer, a gmail address) goes to you: nothing is sent.
- It is graded like any address (verify_published_address, counted free
  tiers included). Only verified, or a catch-all on the company's confirmed
  address pattern, is written to; a guess or invalid one is kept, never sent.
- Its first email says who pointed us there and always waits for you in the
  Outbox (held for review, even on autopilot). One in Canada or Australia
  says why too: being referred isn't consent under CASL or the Spam Act.
- The company stop that follows leaves the referral out (stop_company's
  ``keep``), so the person they named isn't blocked by their own colleague.

Nothing here sends anything. ``prepare_referral`` makes the network and
model calls and stores nothing; ``store_referral`` writes and calls nothing,
so a provider outage half-way never leaves half a referral behind.
"""

import json
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone

from harvey.brain import BrainUnavailable
from harvey.integrations.email_finder import (
    ROLE_LOCALS, WEAK_SOURCES, infer_pattern_from_email, verify_published_address,
)
from harvey.state import FREE_MAIL_DOMAINS, HELD_FOR_REVIEW
from harvey.untrusted import UNTRUSTED_RULE, fence

logger = logging.getLogger("harvey.referrals")

REFERRAL_SOURCE = "referral"
REFERRED_TITLE = "Referred contact"
# What a referral's first email is told to open with, after the greeting.
NAMED_OPENING = "{first} suggested I get in touch"
UNNAMED_OPENING = "I was told you're the right person to talk to"
# Grades a referral's first email is staged for (the gate still checks
# send_to_risky when it goes out).
SENDABLE_GRADES = ("verified", "risky")
# Where a referral isn't consent: a first email there says so.
CONSENT_COUNTRIES = {"CA": "Canada (CASL)", "AU": "Australia (the Spam Act)"}
MAX_REFERRAL_WORDS = 110

# Kinds of referral find_referral returns.
ADDRESS = "address"        # an address at the company's own domain
OUTSIDE = "outside"        # an address anywhere else: yours to decide
SEVERAL = "several"        # more than one, and no telling which
NAME = "name"              # a person named, no address
LINKEDIN = "linkedin"      # a LinkedIn profile
NONE = "none"

_ADDRESS_RE = re.compile(r"(?<![\w.%+-])[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
_LINKEDIN_RE = re.compile(r"(?:https?://)?(?:[a-z]{2,3}\.)?linkedin\.com/in/([A-Za-z0-9\-_%.]+)",
                          re.IGNORECASE)
_SENTENCE_RE = re.compile(r"(?<=[.!?;])\s+|\n+")
# "For urgent matters contact ...": an out-of-office's line, not a referral.
_URGENT_RE = re.compile(
    r"\b(?:urgent|urgently|emergenc\w*|in\s+my\s+absence|while\s+i(?:'m|\s+am)\s+(?:away|out|off)"
    r"|until\s+i\s+(?:return|am\s+back|get\s+back|'m\s+back)|out\s+of\s+(?:the\s+)?office"
    r"|on\s+(?:annual\s+|parental\s+|maternity\s+|paternity\s+|sick\s+)?leave"
    r"|on\s+(?:vacation|holiday|holidays))\b", re.IGNORECASE)
# A generic mailbox (info@, support@) is a referral only when the sentence
# offers it as the one to write to (and the model picked it too): "the
# right person is ...", "send it to ...", "please email ...". "Our support
# email is support@..." offers nothing.
_OFFERED_RE = re.compile(
    r"\b(?:right|best|correct|better|appropriate|proper)\s+(?:person|people|contact|address"
    r"|e-?mail|team|one|place|department|inbox)\b"
    r"|\b(?:in\s+charge\s+of|responsible\s+for|looks?\s+after|takes?\s+care\s+of)\b"
    r"|\b(?:send|forward|direct|pass)\s+(?:it|this|that|these|them|your\s+\w+)\s+(?:on\s+)?to\b",
    re.IGNORECASE)
_OFFERED_BEFORE_RE = re.compile(
    r"\b(?:reach\s+out\s+to|get\s+in\s+touch\s+with|talk\s+to|speak\s+(?:to|with)|contact"
    r"|e-?mail|write\s+to|try|message|cc|copy\s+in)\s*[:,]?\s*(?:(?:them|him|her|us)\s+)?(?:at\s+|on\s+)?"
    r"[(\[<\"']?$", re.IGNORECASE)
# Where their own words end: a signature delimiter, a sign-off line, a phone's.
_SIGNATURE_START_RE = re.compile(
    r"^[ \t]*(?:--|__+|-{2,}\s*$|(?:thanks|thank\s+you|many\s+thanks|best|best\s+regards|regards"
    r"|kind\s+regards|warm\s+regards|cheers|sincerely|all\s+the\s+best|take\s+care)[ \t]*[,.!]?[ \t]*$"
    r"|sent\s+from\s+my\s+\w|get\s+outlook\s+for\s+\w)", re.IGNORECASE)
_SECOND_LEVEL = {"co", "com", "org", "net", "ac", "gov", "edu", "ltd", "plc", "or", "ne", "go", "nom"}
_NAME_RE = re.compile(r"^[^\W\d_][^\W\d_'.\- ]*(?:[ '.\-][^\W\d_][^\W\d_'.\-]*){0,4}$")
_HONORIFIC_RE = re.compile(r"^(?:mr|mrs|ms|miss|mx|dr|prof)\.?\s+", re.IGNORECASE)


@dataclass
class Referral:
    kind: str = NONE
    email: str = ""
    first_name: str = ""
    last_name: str = ""
    role: str = ""
    linkedin_slug: str = ""
    why: str = ""

    @property
    def name(self) -> str:
        return f"{self.first_name} {self.last_name}".strip()

    @property
    def who(self) -> str:
        """How the replies and notes say who they named."""
        return self.name or self.email or "the person they named"


@dataclass
class Plan:
    """What store_referral writes for an ADDRESS referral."""
    referral: Referral
    grade: str = ""                       # verified / risky / guess / invalid
    existing: object | None = None        # a prospect already on file at that address
    fresh: bool = True                    # nothing has been written for them yet
    draft: dict | None = None             # their first email, when one was written
    hold: list[str] = field(default_factory=list)
    country: str = ""

    @property
    def staged(self) -> bool:
        """Their first email goes to the Outbox (held for you)."""
        return self.fresh and self.draft is not None and self.grade in SENDABLE_GRADES


# ── Reading the reply ──

def registrable_domain(domain: str) -> str:
    """mail.acme.example -> acme.example; shop.acme.co.uk -> acme.co.uk."""
    domain = (domain or "").strip().lower().rstrip(".")
    domain = re.sub(r"^https?://", "", domain).split("/")[0].split(":")[0]
    if domain.startswith("www."):
        domain = domain[4:]
    labels = [x for x in domain.split(".") if x]
    if len(labels) >= 3 and labels[-2] in _SECOND_LEVEL and len(labels[-1]) == 2:
        return ".".join(labels[-3:])
    return ".".join(labels[-2:])


def company_domains(prospect_email: str = "", company_domain: str = "") -> set[str]:
    """The registrable domains an address at the company may be at: its
    site's and the one Harvey emailed (never a free-mail domain)."""
    out = set()
    for raw in (company_domain, (prospect_email or "").rpartition("@")[2]):
        domain = registrable_domain(raw)
        if domain and "." in domain and domain not in FREE_MAIL_DOMAINS:
            out.add(domain)
    return out


def _ours(domain: str, own_domains) -> bool:
    domain = (domain or "").lower()
    return any(domain == d or domain.endswith("." + d) for d in own_domains if d)


def their_own_words(text: str) -> str:
    """The reply without a signature: everything above the first signature
    delimiter, sign-off line or phone signature that follows something they
    wrote (a "Thanks!" opening the reply isn't one). The caller has already
    taken the quoted email and our own text out."""
    kept = []
    for line in (text or "").replace("\r\n", "\n").split("\n"):
        if line.lstrip().startswith(">"):
            continue
        if _SIGNATURE_START_RE.match(line):
            if any(k.strip() for k in kept):
                break
            continue
        kept.append(line)
    return "\n".join(kept).strip()


def is_generic(address: str) -> bool:
    """info@, support@, sales@: a function's mailbox, not a person's."""
    return address.partition("@")[0].lower() in ROLE_LOCALS


def addresses_in(text: str, exclude=(), own_domains=()) -> list[str]:
    """The addresses their words offer, in order: none from a sentence about
    urgent matters or being away, none of ``exclude`` or at ``own_domains``,
    and a generic mailbox only when the sentence offers it."""
    exclude = {(e or "").strip().lower() for e in exclude if e}
    found = []
    for sentence in _SENTENCE_RE.split(text or ""):
        if _URGENT_RE.search(sentence):
            continue
        for m in _ADDRESS_RE.finditer(sentence):
            address = m.group(0).strip(".").lower()
            domain = address.partition("@")[2]
            if address in exclude or _ours(domain, own_domains) or address in found:
                continue
            if is_generic(address) and not (
                    _OFFERED_BEFORE_RE.search(sentence[:m.start()])
                    or _OFFERED_RE.search(sentence[:m.start()] + " " + sentence[m.end():])):
                continue
            found.append(address)
    return found


def linkedin_profiles_in(text: str, exclude=()) -> list[str]:
    """LinkedIn profile slugs their words name (outside urgent-matter lines)."""
    exclude = {(e or "").strip().lower() for e in exclude if e}
    found = []
    for sentence in _SENTENCE_RE.split(text or ""):
        if _URGENT_RE.search(sentence):
            continue
        for slug in _LINKEDIN_RE.findall(sentence):
            slug = slug.strip("./").lower()
            if slug and slug not in exclude and slug not in found:
                found.append(slug)
    return found


def clean_name(value) -> tuple[str, str]:
    """(first, last) from what the model read as their name, or ("", "")."""
    if not isinstance(value, str):
        return "", ""
    name = _HONORIFIC_RE.sub("", " ".join(value.split())).strip(" ,.")
    if not name or len(name) > 60 or not _NAME_RE.match(name):
        return "", ""
    parts = name.split(" ")
    return parts[0], " ".join(parts[1:])


def clean_role(value) -> str:
    if not isinstance(value, str):
        return ""
    role = re.sub(r"[^\w &/,.'()+-]", "", " ".join(value.split())).strip(" ,.")
    return role[:80]


async def _ask_json(brain, prompt: str, task: str, agent: str = "handler"):
    """One JSON answer from the brain (think_json when it has one)."""
    session = f"harvey-{agent}"
    failed_before = _failed_calls(brain)
    if hasattr(brain, "think_json"):
        result = await brain.think_json(prompt, session_id=session, agent=agent, task=task)
    else:
        raw = await brain.think(prompt, session_id=session, agent=agent, task=task)
        try:
            result = json.loads(raw) if raw else None
        except (TypeError, ValueError):
            result = None
    if result is None and getattr(brain, "paused_reason", ""):
        raise BrainUnavailable(brain.paused_reason)
    if result is None and _failed_calls(brain) > failed_before:
        # No answer came back at all (every retry failed): that says nothing
        # about what was asked, so it mustn't count against it.
        raise BrainUnavailable("the model call failed; trying again later")
    return result


def _failed_calls(brain) -> int:
    count = getattr(brain, "failed_calls", 0)
    return count if isinstance(count, int) else 0


async def ask_who(brain, words: str, company: str = "") -> dict:
    """The model's reading of who they named: {name, role, email}."""
    prompt = f"""Someone at {company or 'a company'} answered a sales email to say they are not
the right person to talk to. Read their reply and say who, if anyone, they
tell us to contact instead.

{UNTRUSTED_RULE}

{fence(words, "their reply")}

Return ONLY JSON: {{"name": "...", "role": "...", "email": "..."}}
- name: the person they point us to, as they wrote it, else null.
- role: that person's job or what they look after, as they wrote it, else null.
- email: that person's address exactly as written in the reply, else null.
  Never make one up. An address given only for urgent matters, while
  someone is away, or as a general support line is not who they point us to.
Use null for all three when they point us to nobody."""
    result = await _ask_json(brain, prompt, "find_referral")
    return result if isinstance(result, dict) else {}


async def find_referral(brain, text: str, *, domains: set[str], exclude=(), own_domains=(),
                        company: str = "", linkedin_exclude=(), exclude_names=()) -> Referral:
    """Who a "wrong person" reply tells us to write to (see the module doc).
    ``domains``: the company's own registrable domains (company_domains).
    ``exclude``: addresses never taken (the replier's, the one Harvey
    emailed, our own); ``exclude_names``: the replier's own name."""
    words = their_own_words(text)
    if not words:
        return Referral()
    addresses = addresses_in(words, exclude, own_domains)
    profiles = linkedin_profiles_in(words, linkedin_exclude)
    answer = await ask_who(brain, words, company)
    first, last = clean_name(answer.get("name"))
    if f"{first} {last}".strip().lower() in {(n or "").strip().lower() for n in exclude_names if n}:
        first, last = "", ""
    role = clean_role(answer.get("role"))
    picked = str(answer.get("email") or "").strip().lower()
    # A generic mailbox needs the model's pick as well as the sentence's offer.
    addresses = [a for a in addresses if not is_generic(a) or a == picked]
    if picked not in addresses:
        picked = addresses[0] if len(addresses) == 1 else ""
    referral = Referral(first_name=first, last_name=last, role=role)
    if picked:
        referral.email = picked
        own = registrable_domain(picked.partition("@")[2]) in domains
        free = picked.partition("@")[2] in FREE_MAIL_DOMAINS
        referral.kind = ADDRESS if own and not free else OUTSIDE
        if referral.kind == OUTSIDE:
            referral.why = (f"they named {picked}, which isn't at the company's own domain "
                            f"({', '.join(sorted(domains)) or 'none known'})")
    elif addresses:
        referral.kind = SEVERAL
        referral.why = f"they named several addresses ({', '.join(addresses[:4])})"
    elif profiles:
        referral.kind, referral.linkedin_slug = LINKEDIN, profiles[0]
    elif first:
        referral.kind = NAME
    return referral


# ── Grading and the first email ──

async def grade_address(state, env, referral: Referral) -> str:
    """verify_published_address's grade, with a catch-all ("risky") kept
    only on the domain's confirmed address pattern (the person's name fits
    it); otherwise it is a guess, as find_email grades one."""
    grade = await verify_published_address(referral.email, env=env, state=state)
    if grade != "risky":
        return grade
    domain = referral.email.partition("@")[2]
    try:
        cached = await state.get_email_pattern(domain) or {}
    except Exception:
        cached = {}
    pattern = cached.get("pattern") or ""
    confirmed = bool(pattern) and (cached.get("source") or "") not in WEAK_SOURCES
    fits = bool(referral.first_name and referral.last_name) and infer_pattern_from_email(
        referral.email, referral.first_name, referral.last_name) == pattern
    return "risky" if confirmed and fits else "guess"


def consent_country(company) -> str:
    """CA or AU when the company is in Canada or Australia (its location,
    else its domain's country code), else ""."""
    from harvey.send_window import country_for
    location = (getattr(company, "location", "") or "").strip()
    if location:
        try:
            code, _ = country_for(location)
        except Exception:
            code = ""
        if code in CONSENT_COUNTRIES:
            return code
    domain = registrable_domain(getattr(company, "domain", "") or "")
    for code in CONSENT_COUNTRIES:
        if domain.endswith("." + code.lower()):
            return code
    return ""


def _opening(referrer_first: str) -> str:
    return NAMED_OPENING.format(first=referrer_first) if referrer_first else UNNAMED_OPENING


def ensure_opening(body: str, referrer_first: str) -> str:
    """The body with its referral line: kept when the draft has it,
    otherwise put after the greeting (or first)."""
    opening = _opening(referrer_first)
    if opening.lower() in " ".join((body or "").split()).lower():
        return body
    lines = (body or "").strip().split("\n")
    line = opening + "."
    if lines and re.match(r"^\s*(?:hi|hello|hey|dear|good\s+\w+)\b[^\n]{0,40}[,!]?\s*$",
                          lines[0], re.IGNORECASE):
        return "\n".join([lines[0], "", line, *lines[1:]]).strip()
    return "\n".join([line, "", *lines]).strip()


async def draft_referral_email(brain, config, *, referral: Referral, company, referrer_first: str,
                               their_words: str, skills: str = "", multi: bool = False) -> dict | None:
    """Their first email: who pointed us here, one reason to talk, one
    question. {"subject", "body"} or None when no usable draft came back."""
    from harvey.agents.writer import BUILT_IN_FOOTER, sign_off_rule
    from harvey.humanize import humanize, humanizer_enabled
    persona, product = config.persona, config.product
    company_name = (getattr(company, "name", "") or "").strip() or "their company"
    facts = [f"- Name: {referral.name or 'unknown (greet them with Hi there)'}",
             f"- Role: {referral.role or 'unknown'}",
             f"- Company: {company_name}"]
    for label, value in (("What the company says about itself", getattr(company, "description", "")),
                         ("Industry", getattr(company, "industry", ""))):
        if value:
            facts.append(f"- {label}: {value}")
    pointed = (f"{referrer_first}, a colleague at {company_name}" if referrer_first
               else f"someone at {company_name}")
    who = (f"a member of the {persona.company} team" if multi
           else f"{persona.name}, {persona.role} at {persona.company}")
    opening = _opening(referrer_first)
    prompt = f"""You write as {who}. Tone: {persona.tone}.
Product: {product.name}: {product.description}
{skills}

Write ONE short first email to a person {pointed} told us to contact.

FACTS (everything you know; invent nothing else):
{fence(chr(10).join(facts), "their company")}

What the colleague wrote when they pointed us here:
{fence(their_words, "their colleague's reply")}

{UNTRUSTED_RULE}

Requirements:
- Right after the greeting, the first sentence starts exactly with "{opening}"
  and finishes in a few words with what you'd like to talk about.
- Then one specific reason it's worth their time and one easy question.
- Under 90 words. No quotes from the colleague and nothing about what they think.
- Subject: lowercase, 2-5 words.
{BUILT_IN_FOOTER}{chr(10) + sign_off_rule("the email") if multi else ""}

Return ONLY JSON: {{"subject": "...", "body": "..."}}"""
    result = await _ask_json(brain, prompt, "referral_email")
    if not isinstance(result, dict):
        return None
    subject = str(result.get("subject") or "").strip()[:120]
    body = str(result.get("body") or "").strip()[:2000]
    if not subject or not body:
        return None
    [polished] = await humanize(brain, [{"subject": subject, "body": body}], agent="handler",
                                enabled=humanizer_enabled(config))
    body = ensure_opening(polished.get("body") or body, referrer_first)
    if len(body.split()) > MAX_REFERRAL_WORDS:
        return None
    return {"subject": polished.get("subject") or subject, "body": body}


def hold_reasons(referrer_label: str, company_name: str, country: str) -> list[str]:
    reasons = [f"a referral: {referrer_label} at {company_name or 'their company'} named them, "
               "so their first email waits for you"]
    if country in CONSENT_COUNTRIES:
        reasons.append(f"they're in {CONSENT_COUNTRIES[country]}, where being referred is not "
                       "consent: check you may email them before approving")
    return reasons


async def prepare_referral(state, brain, config, env, *, referral: Referral, company,
                           referrer_first: str, referrer_label: str, their_words: str,
                           skills: str = "", multi: bool = False) -> Plan:
    """Grade the address and write their first email. Stores nothing;
    raises BrainUnavailable when the provider is down."""
    plan = Plan(referral=referral)
    existing = await state.get_prospect_by_email(referral.email)
    if existing is not None:
        plan.existing = existing
        plan.fresh = (existing.status == "new"
                      and not await state.get_texts_for_prospect(existing.id, limit=1))
        if existing.status == "opted_out":
            plan.fresh = False
    if plan.fresh and existing is not None and existing.email_status in SENDABLE_GRADES:
        plan.grade = existing.email_status
    elif plan.fresh or existing is None:
        plan.grade = await grade_address(state, env, referral)
    else:
        plan.grade = existing.email_status or ""
    plan.country = consent_country(company)
    plan.hold = hold_reasons(referrer_label, getattr(company, "name", "") or "", plan.country)
    if plan.fresh and plan.grade in SENDABLE_GRADES:
        plan.draft = await draft_referral_email(
            brain, config, referral=referral, company=company, referrer_first=referrer_first,
            their_words=their_words, skills=skills, multi=multi)
    return plan


async def store_referral(state, plan: Plan, *, referrer, company, mailbox: str = "",
                         provider: str = "", agent: str = "handler",
                         referrer_name: str = "", source_note: str = "") -> dict:
    """Write what ``plan`` says: the referral prospect (or the one on file),
    their first email held in the Outbox, and the company stop (the
    referrer's) that keeps them. Returns what happened, for the log."""
    from harvey.models.prospect import Prospect
    from harvey.segment_stamp import variant_for
    referral = plan.referral
    referrer_id = getattr(referrer, "id", "") or ""
    referrer_name = referrer_name or (referrer.full_name() if referrer is not None else "")
    today = datetime.now(timezone.utc).date().isoformat()
    note = (f"Referred on {today} by {referrer_name or 'a colleague'}"
            + (f" (prospect {referrer_id})" if referrer_id else "")
            + (f" {source_note}" if source_note else "") + ".")
    grade = plan.grade or "guess"
    if plan.existing is not None:
        prospect_id = plan.existing.id
        if plan.fresh and grade != (plan.existing.email_status or ""):
            await state.update_prospect_email(prospect_id, referral.email, grade)
    else:
        prospect_id = await state.add_prospect(Prospect(
            company_id=getattr(company, "id", "") or getattr(referrer, "company_id", "") or "",
            company=(getattr(company, "name", "") or getattr(referrer, "company", "") or ""),
            industry=getattr(company, "industry", "") or getattr(referrer, "industry", "") or "",
            first_name=referral.first_name, last_name=referral.last_name,
            title=referral.role or REFERRED_TITLE, email=referral.email,
            email_status=grade, email_verified=grade == "verified",
            source=REFERRAL_SOURCE, status="new", personalization_notes=note,
            mailbox=mailbox or ""))
    outbox_id = None
    if plan.staged:
        if plan.existing is not None and mailbox:
            await state.set_prospect_mailbox(prospect_id, mailbox)
        outbox_id = await state.add_outbox_item(
            prospect_id=prospect_id, to_email=referral.email, subject=plan.draft["subject"],
            body=plan.draft["body"], send_at=datetime.now(timezone.utc).replace(tzinfo=None).isoformat(),
            status="pending_review", step=1, kind="sequence", provider=provider,
            angle=REFERRAL_SOURCE, variant=variant_for(prospect_id))
        if outbox_id:
            await state.update_outbox_item(
                outbox_id, error=(HELD_FOR_REVIEW + "; ".join(plan.hold))[:300])
    if plan.fresh and grade in SENDABLE_GRADES:
        # Off the Writer's list: the first email they get is the referral's
        # (the one above, or yours when none could be written).
        await state.queue_prospect_if_unsent(prospect_id)
    stopped = 0
    if referrer_id:
        stopped = await state.stop_company(referrer_id, keep={prospect_id})
    result = {"referral_prospect_id": prospect_id, "referred_by": referrer_id,
              "referred_by_name": referrer_name, "email": referral.email, "grade": grade,
              "already_on_file": plan.existing is not None, "first_email": outbox_id or "",
              "held": plan.hold if outbox_id else [], "colleagues_stopped": stopped}
    await state.log_action(action_type="referral", agent=agent, details=result)
    logger.info(f"Referral: {referrer_name or 'a reply'} named {referral.email} [{grade}]"
                + ("; their first email waits for you." if outbox_id else "; nothing queued."))
    return result


def reply_guidance(referral: Referral | None, plan: Plan | None = None) -> str:
    """What the thank-you to the replier should say (a prompt fragment), or
    "" for the usual "who would be the right person?"."""
    if referral is None:
        return ""
    who = referral.who
    if referral.kind == ADDRESS:
        if plan is not None and plan.staged:
            return ("- Thanks them in one line\n"
                    f"- Says you'll get in touch with {who} directly\n"
                    "- Asks for nothing else\n- Under 40 words")
        return ("- Thanks them for pointing you to " + who + "\n"
                "- Promises nothing about when or whether you'll write to them\n"
                "- Asks for nothing else\n- Under 40 words")
    if referral.kind == NAME:
        return ("- Thanks them\n"
                f"- Asks for the best email address to reach {who}\n"
                "- One line, under 40 words")
    if referral.kind == LINKEDIN:
        return ("- Thanks them\n"
                f"- Says you'll reach out to {who} on LinkedIn\n"
                "- Under 40 words")
    return ""
