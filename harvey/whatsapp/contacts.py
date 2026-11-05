"""Who may be messaged on WhatsApp. This is the core rule of the channel.

Only people who gave us their number themselves. ``queue_contact`` takes one
person at a time and requires a ``source``, and there is deliberately no way to
bulk-import a list. A number is normalised to E.164 or, when its country code
can't be established, stored as ``needs_review`` and never guessed: a wrong
guess messages a stranger.

Anyone opted out or declined on any channel, on the do-not-contact list, or
already in a live email conversation is refused, here and again before every
draft and every send (``stop_reason``), since all of that can change after they
were queued.

No harvey.state import here: harvey.state imports this package's store.
"""

import logging
import re
from dataclasses import dataclass

from harvey.whatsapp.numbers import country_of_number, digits_of, normalize_phone

logger = logging.getLogger("harvey.whatsapp")

# Sources that say the number wasn't given to us by the person. An honest source
# is all this can check, but a tool shouldn't accept these when asked politely.
_NOT_THEIR_OWN_RE = re.compile(
    r"scrap|bought|purchas|bulk|\bcsv\b|\bimport|\b(?:cold|lead|email|contact|call|phone)\s*list\b|"
    r"\bdirectory\b|yellow pages|google maps|\bdiscover|\bprospect",
    re.IGNORECASE)
# Prospect statuses that mean they said no by email.
SAID_NO = frozenset({"lost", "closed_lost"})
# ...and that they're talking to us by email, or a deal is under way.
LIVE_EMAIL = frozenset({"replied", "meeting", "closed", "closed_won"})
LIVE_CONVERSATION = ("open", "replied", "meeting_booked", "needs_human")


@dataclass
class QueueResult:
    status: str             # queued | needs_review | duplicate | refused
    contact_id: str = ""
    reason: str = ""

    @property
    def queued(self) -> bool:
        return self.status == "queued"


def country_code_of(value: str) -> str:
    """A two-letter code from a code or a country's name, or ""."""
    from harvey.send_window import country_code, country_for
    text = str(value or "").strip()
    if not text:
        return ""
    return country_code(text) or country_for(text)[0]


def blocked_numbers(config) -> set[str]:
    """channels.whatsapp.do_not_contact as the config file says it is now, so
    adding a number while Harvey runs holds at once (compliance.do_not_contact
    only takes addresses and domains). A file that can't be read keeps the list
    Harvey started with."""
    numbers = set(getattr(config.channels.whatsapp, "do_not_contact", None) or [])
    path = getattr(config, "_source_path", None)
    if not path:
        return numbers
    try:
        import yaml
        from harvey.config import WhatsAppChannelConfig
        from harvey.textio import read_user_text
        data = yaml.safe_load(read_user_text(path)) or {}
        section = ((data.get("channels") or {}).get("whatsapp") or {}) if isinstance(data, dict) else {}
        numbers |= set(WhatsAppChannelConfig(
            do_not_contact=section.get("do_not_contact") or []).do_not_contact)
    except Exception as e:
        logger.warning(f"channels.whatsapp.do_not_contact couldn't be read again ({e}).")
    return numbers


async def find_prospect(state, email: str = "", phone: str = ""):
    """The Harvey contact this person is, by email or by number. Their stored
    number may be written any way, so digits are compared."""
    if email:
        prospect = await state.get_prospect_by_email(email)
        if prospect is not None:
            return prospect
    digits = digits_of(phone)
    if digits:
        rows = await state._wa_query(
            "SELECT id, phone FROM prospects WHERE phone IS NOT NULL AND phone != ''")
        for row in rows:
            # "07700 900123" is the end of +447700900123, its trunk 0 aside.
            theirs = digits_of(row["phone"]).lstrip("0")
            if theirs == digits or (len(theirs) >= 9 and digits.endswith(theirs)):
                return await state.get_prospect(row["id"])
    return None


async def stop_reason(state, config, contact: dict, *, replying: bool = False, numbers=None,
                      scan_phones: bool = True) -> str:
    """Why this contact must not get a WhatsApp message from us now, or "".

    ``replying``: they wrote to us on WhatsApp, so a live email conversation
    doesn't stop the answer (a WhatsApp reply itself marks their email replied).
    A no, on any channel, always does. ``numbers`` and ``scan_phones`` are for
    drawing a whole tab: the blocked list already read, and no walk through every
    stored phone per contact. Before a draft or a send, both stay at their defaults."""
    from harvey.config import live_do_not_contact
    from harvey.gate import do_not_contact_domain_match, do_not_contact_match

    phone, email = contact.get("phone") or "", (contact.get("email") or "").strip().lower()
    if phone and phone in (blocked_numbers(config) if numbers is None else numbers):
        return "their number is on channels.whatsapp.do_not_contact"
    entries = live_do_not_contact(config)
    if email and do_not_contact_match(email, entries):
        return "their address is on your do-not-contact list"
    prospect = None
    if contact.get("prospect_id"):
        prospect = await state.get_prospect(contact["prospect_id"])
    if prospect is None:
        prospect = await find_prospect(state, email, phone if scan_phones else "")
    if prospect is None:
        return ""
    if prospect.email and do_not_contact_match(prospect.email, entries):
        return "their address is on your do-not-contact list"
    if prospect.status == "opted_out":
        return "they opted out"
    if prospect.status in SAID_NO:
        return "they said no by email"
    if await state._wa_query("SELECT 1 FROM linkedin_leads WHERE prospect_id = ? AND stage = 'declined' "
                             "LIMIT 1", (prospect.id,)):
        return "they said no on LinkedIn"
    if prospect.company_id:
        company = await state.get_company(prospect.company_id)
        if company and company.domain and do_not_contact_domain_match(company.domain, entries):
            return "their company is on your do-not-contact list"
    if replying:
        return ""
    if prospect.status in LIVE_EMAIL:
        return "they're in a live email conversation"
    marks = ", ".join("?" for _ in LIVE_CONVERSATION)
    if await state._wa_query(
            f"SELECT 1 FROM conversations WHERE prospect_id = ? AND status IN ({marks}) "
            "AND COALESCE(channel, 'email') = 'email' LIMIT 1", (prospect.id, *LIVE_CONVERSATION)):
        return "they're in a live email conversation"
    if await state.colleague_who_replied(prospect.id):
        return "someone at their company answered an email"
    return ""


async def queue_contact(state, *, phone, name="", email="", context="", source, country="",
                        config=None) -> QueueResult:
    """Queue one person who gave us their number themselves.

    ``source``: where they gave it ("website chat", "contact form"). Required,
    and said back to them in the first message. Returns what became of it:
    queued, needs_review (the number can't be established), duplicate (already
    here), or refused. Raises ValueError with no source or no number at all."""
    source = " ".join(str(source or "").split())
    if not source:
        raise ValueError("a WhatsApp contact needs a source: where they gave us this number")
    raw = str(phone or "").strip()
    if not raw:
        raise ValueError("a WhatsApp contact needs a number")
    if _NOT_THEIR_OWN_RE.search(source):
        return QueueResult("refused", reason=f"\"{source}\" doesn't sound like they gave us this "
                                             "number themselves")
    if config is None:
        try:
            from harvey.config import load_config
            config = load_config()
        except Exception as e:
            return QueueResult("refused", reason=f"couldn't read the do-not-contact list ({e})")

    code = country_code_of(country)
    number = normalize_phone(raw, code)
    email = str(email or "").strip().lower()
    name, context = " ".join(str(name or "").split()), str(context or "").strip()
    fields = {"phone": number.e164, "email": email}
    why = await stop_reason(state, config, fields)
    if why:
        await state.log_action("whatsapp_refused", "whatsapp", {"why": why, "source": source})
        return QueueResult("refused", reason=why)

    existing = await state.wa_get_contact_by_phone(number.e164) if number.ok else None
    if existing and existing["stage"] == "declined":
        return QueueResult("refused", existing["id"], "they said no on WhatsApp")

    prospect = await find_prospect(state, email, number.e164)
    common = {"raw_phone": raw, "name": name, "email": email, "context": context,
              "prospect_id": prospect.id if prospect else ""}
    if not number.ok:
        contact_id, created = await state.wa_add_contact(
            phone="", source=source, stage="needs_review", country=code,
            review_reason=number.problem, **common)
        status = "needs_review" if created else "duplicate"
        await state.log_action("whatsapp_contact", "whatsapp", {"contact_id": contact_id, "status": status,
                                                               "source": source})
        return QueueResult(status, contact_id, number.problem)

    contact_id, created = await state.wa_add_contact(
        phone=number.e164, source=source, stage="queued",
        country=code or country_of_number(number.e164), **common)
    status = "queued" if created else "duplicate"
    await state.log_action("whatsapp_contact", "whatsapp", {"contact_id": contact_id, "status": status,
                                                           "source": source})
    return QueueResult(status, contact_id, "" if created else "this number is already here")


async def fix_number(state, config, contact_id: str, phone: str, country: str = "") -> tuple[bool, str]:
    """The owner corrected a needs_review number: (done, why not). The same
    rules as queueing them; nothing is guessed here either."""
    contact = await state.wa_get_contact(contact_id)
    if contact is None or contact["stage"] != "needs_review":
        return False, "That contact isn't waiting for a number."
    code = country_code_of(country) or contact.get("country") or ""
    number = normalize_phone(phone, code)
    if not number.ok:
        return False, f"That number won't do: {number.problem}."
    other = await state.wa_get_contact_by_phone(number.e164)
    if other and other["id"] != contact_id:
        return False, "Another contact already has this number."
    why = await stop_reason(state, config, {**contact, "phone": number.e164})
    if why:
        return False, f"Harvey won't message them: {why}."
    await state.wa_set_stage(contact_id, "queued", phone=number.e164, review_reason="",
                             country=code or country_of_number(number.e164))
    return True, ""
