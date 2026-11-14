"""A WhatsApp message to your own phone when a client gets close to a meeting.

Owner, 2026-11-14: "If you get close to setting up a meeting with any client,
from any channel (ERP, WhatsApp, LinkedIn or email), message my personal number
about that meeting with all the details: which channel, where I can read the
conversation in the dashboard, the type of service the client wants and all
the basic details."

Every channel calls ``maybe_alert`` with the newest message of a conversation
that reads positive (interested, a question, an objection; on the website chat,
a project). A cheap word check comes first (a call, a meeting, a time, a day,
"book", "schedule"...); only then does one model call read the conversation and
say whether a meeting really is close, and pull out when, what service and a
short summary. Then one message goes in the WhatsApp queue to
``channels.whatsapp.alert_number`` from the linked number (kind "alert", which
none of a lead's rules hold back: harvey/agents/whatsapp_agent.py).

One alert per conversation every ALERT_EVERY (a new time proposed days later
alerts again). Nothing is sent when no alert number is set, and the brain being
down only means no alert this time.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta, timezone

from harvey.untrusted import UNTRUSTED_RULE, fence

logger = logging.getLogger("harvey.meeting_alert")

ALERT_KEY = "meeting_alert:"
ALERT_EVERY = timedelta(days=3)
ALERT_APPROVED = "harvey alert"
POSITIVE_INTENTS = frozenset({"interested", "question", "objection"})
MAX_ALERT_CHARS = 1500

# Words that can mean a meeting is being arranged. Only a gate before the model
# call: the model decides.
MEETING_RE = re.compile(
    r"\b(?:call|calls|meeting|meet|zoom|google meet|g ?meet|teams|skype|calendar|calendly|invite|"
    r"schedule|scheduled|book|booked|booking|slot|slots|appointment|demo|catch up|hop on|jump on|"
    r"talk|chat|speak|discuss|connect|available|availability|free (?:on|at|this|next|tomorrow)|"
    r"what time|which time|time works|works for me|tomorrow|today|tonight|"
    r"monday|tuesday|wednesday|thursday|friday|saturday|sunday|next week|this week|"
    r"\d{1,2}(?::\d{2})?\s?(?:am|pm)|ist|est|edt|pst|pdt|cst|gmt|bst|utc|aest)\b",
    re.IGNORECASE)


def alert_number(config) -> str:
    whatsapp = getattr(getattr(config, "channels", None), "whatsapp", None)
    return (getattr(whatsapp, "alert_number", "") or "").strip()


def might_be_meeting(text: str) -> bool:
    return bool(MEETING_RE.search(text or ""))


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _prompt(company: str, channel: str, name: str, thread: str, notes: str) -> str:
    return f"""You help the owner of {company or "a services company"} keep track of sales conversations.
Read this {channel} conversation with {name or "a prospect"} and say whether it is close to a meeting
or call with us: they asked for one, agreed to one, proposed or accepted a time, asked how to book,
or a time is being worked out. Plain interest, a question about price or scope, or "send me more
info" is NOT close to a meeting.

{UNTRUSTED_RULE}

{fence(thread[-8000:], "the conversation")}
{fence(notes[:1500], "what we knew about them") if notes else ""}

Return ONLY JSON: {{"close_to_meeting": false, "when": "", "service": "", "summary": "", "next_step": ""}}
- close_to_meeting: true or false, as above. When unsure, false.
- when: the day and time they proposed or agreed, with its time zone if given, or "" if none yet.
- service: what they want from us, in a few words (for example "Shopify store redesign"), or "".
- summary: one or two plain sentences on where the conversation stands. No quotes.
- next_step: what the owner should do next, in one short sentence."""


def _line(label: str, value: str) -> str:
    value = " ".join((value or "").split())
    return f"{label}: {value}" if value else ""


def compose(*, name: str, company: str, channel: str, where: str, contacts, read: dict,
            latest: str, service_hint: str = "") -> str:
    """The WhatsApp message: plain text, one fact a line."""
    who = name or "A client"
    if company:
        who += f" ({company})"
    quote = " ".join((latest or "").split())
    if len(quote) > 280:
        quote = quote[:277].rstrip() + "..."
    lines = [
        f"Harvey: a meeting is close with {who}",
        _line("Channel", channel),
        _line("Read it", where),
        _line("They want", read.get("service") or service_hint),
        _line("When", read.get("when") or "no time agreed yet"),
        _line("Contact", " | ".join(c for c in contacts if c)),
        _line("They said", f'"{quote}"' if quote else ""),
        _line("Where it stands", read.get("summary", "")),
        _line("Next", read.get("next_step", "")),
    ]
    return "\n".join(l for l in lines if l)[:MAX_ALERT_CHARS]


async def queue_to_owner(state, config, text: str) -> str | None:
    """One message to your phone in the WhatsApp queue, approved. Returns its
    id, or None when no alert number is set (or it is the linked number)."""
    from harvey.whatsapp.numbers import digits_of, normalize_phone
    from harvey.whatsapp.schema import OWNER_STAGE
    raw = alert_number(config)
    if not raw:
        return None
    number = normalize_phone(raw)
    phone = getattr(number, "e164", "") or ""
    if not phone:
        logger.warning("Meeting alert: channels.whatsapp.alert_number isn't a full number "
                       "(with its country code); no alert sent.")
        return None
    linked = getattr(getattr(config.channels, "whatsapp", None), "number", "") or ""
    if linked and digits_of(linked) == digits_of(phone):
        logger.warning("Meeting alert: the alert number is the linked number itself; no alert sent.")
        return None
    contact_id, _ = await state.wa_add_contact(phone=phone, source="meeting alerts", stage=OWNER_STAGE)
    contact = await state.wa_get_contact(contact_id)
    if contact and contact.get("stage") != OWNER_STAGE:
        # Your number was on file as a lead: from now on it's yours.
        async with state._connect() as db:
            await db.execute("UPDATE whatsapp_contacts SET stage = ? WHERE id = ?", (OWNER_STAGE, contact_id))
            await db.commit()
    return await state.wa_add_message(contact_id, direction="out", kind="alert", body=text,
                                      status="approved", approved_by=ALERT_APPROVED)


async def maybe_alert(state, brain, config, *, channel: str, key: str, name: str, latest: str,
                      thread: str, where: str, company: str = "", contacts=(), notes: str = "",
                      service_hint: str = "") -> bool:
    """Alert you when this conversation is close to a meeting. ``key`` names
    the conversation (one alert each per ALERT_EVERY). Never raises."""
    try:
        if not alert_number(config) or not might_be_meeting(latest):
            return False
        setting = ALERT_KEY + key
        last = await state.get_setting(setting)
        if last:
            try:
                if _now() - datetime.fromisoformat(last) < ALERT_EVERY:
                    return False
            except ValueError:
                pass
        from harvey.referrals import _ask_json
        our_company = getattr(getattr(config, "persona", None), "company", "") or ""
        read = await _ask_json(brain, _prompt(our_company, channel, name, thread, notes),
                               "meeting_alert", agent="handler")
        if not isinstance(read, dict):
            return False
        close = read.get("close_to_meeting")
        if not (close is True or str(close).strip().lower() in ("true", "yes")):
            return False
        read = {k: str(read.get(k) or "")[:300] for k in ("when", "service", "summary", "next_step")}
        text = compose(name=name, company=company, channel=channel, where=where, contacts=contacts,
                       read=read, latest=latest, service_hint=service_hint)
        message_id = await queue_to_owner(state, config, text)
        if not message_id:
            return False
        await state.set_setting(setting, _now().isoformat())
        await state.log_action("meeting_alert", "alerts", {
            "channel": channel, "key": key, "name": name, "company": company,
            "when": read["when"], "service": read["service"], "whatsapp_message_id": message_id})
        logger.info(f"Meeting alert: {name or key} on {channel}; your phone gets a WhatsApp message.")
        return True
    except Exception as e:
        logger.warning(f"Meeting alert for {name or key} on {channel} failed: {e}")
        return False
