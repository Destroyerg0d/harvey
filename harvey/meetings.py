"""Every meeting in one place, to prepare for it (the dashboard's Meetings tab).

Owner, 2026-11-22: "make a tab for all the meeting time showing in their and
when meeting is scheduled and option to view the convo of that prospect and
where it is so I can prepare for the meeting with some context".

A meeting is a LinkedIn lead or WhatsApp contact at stage ``meeting``, or an
email prospect at status ``meeting``. Its time is the one you set on the tab
(``TIME_KEY``), else the slot Harvey offered and they said yes to
(``meeting_slot.OFFERED_KEY``), written in their time zone and the owner's.
"Getting close" lists the recent meeting alerts (harvey/meeting_alert.py) of
conversations not booked yet. ``brief`` asks the brain for a short prep note,
kept until the conversation changes.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timedelta, timezone

import pytz

from harvey.meeting_slot import OFFERED_KEY, Slot, _window, span, zone_name

logger = logging.getLogger("harvey.meetings")

TIME_KEY = "meeting:time:"       # + channel:id -> {"start": UTC ISO, "minutes", "at"}
BRIEF_KEY = "meeting:brief:"     # + channel:id -> {"text", "messages", "at"}
BOOKED_KEY = "meeting:booked:"   # + channel:id -> an ISO time (Harvey) or {"at", "by"}
CHANNELS = ("linkedin", "whatsapp", "email")
CLOSE_DAYS = 21
DEFAULT_MINUTES = 30
OWNER_ZONE = "Asia/Kolkata"
TAB = {"linkedin": "linkedin", "whatsapp": "whatsapp", "email": "conversations"}
LABEL = {"linkedin": "LinkedIn", "whatsapp": "WhatsApp", "email": "Email"}


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _aware(value) -> datetime | None:
    if not value:
        return None
    try:
        moment = value if isinstance(value, datetime) else datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


def owner_zone(config) -> str:
    offer = getattr(getattr(config, "product", None), "offer", None)
    zone = (getattr(offer, "meeting_timezone", "") or "").strip() or OWNER_ZONE
    try:
        pytz.timezone(zone)
        return zone
    except Exception:
        return OWNER_ZONE


def _window_minutes(config) -> int:
    window = _window(config)
    if not window:
        return DEFAULT_MINUTES
    start, end, _ = window
    return max(15, (end.hour * 60 + end.minute) - (start.hour * 60 + start.minute))


def when(start: datetime, end: datetime, config, their_zone: str = "") -> dict:
    """The meeting's time, as the offer reads: theirs with ours beside it."""
    ours_zone = owner_zone(config)
    zone = pytz.timezone(ours_zone)
    slot = Slot(start=start.astimezone(zone), end=end.astimezone(zone), owner_zone=ours_zone,
                their_zone=their_zone if their_zone and their_zone != ours_zone else "")
    theirs = slot.theirs
    return {
        "start": start.astimezone(timezone.utc).isoformat(),
        "end": end.astimezone(timezone.utc).isoformat(),
        "ours": span(slot.start, slot.end, zone_name(ours_zone)),
        "theirs": span(theirs[0], theirs[1], zone_name(their_zone)) if theirs else "",
        "text": slot.text(),
    }


async def _json_setting(state, key: str) -> dict:
    raw = await state.get_setting(key) or ""
    try:
        data = json.loads(raw) if raw.strip().startswith("{") else ({"at": raw} if raw else {})
    except ValueError:
        data = {}
    return data if isinstance(data, dict) else {}


async def meeting_time(state, config, channel: str, item_id: str, their_zone: str = "") -> tuple[dict | None, str]:
    """(the meeting's time or None, "you" / "offer")."""
    key = f"{channel}:{item_id}"
    mine = await _json_setting(state, TIME_KEY + key)
    start = _aware(mine.get("start"))
    if start:
        minutes = int(mine.get("minutes") or DEFAULT_MINUTES)
        return when(start, start + timedelta(minutes=minutes), config, their_zone), "you"
    offered = await _json_setting(state, OFFERED_KEY + key)
    start = _aware(offered.get("start"))
    if start:
        end = _aware(offered.get("end")) or start + timedelta(minutes=_window_minutes(config))
        return when(start, end, config, their_zone or offered.get("their_zone") or ""), "offer"
    return None, ""


async def set_time(state, config, channel: str, item_id: str, date: str, clock: str,
                   minutes: int = DEFAULT_MINUTES) -> datetime:
    """Your time for a meeting: ``date`` ("2026-11-23") and ``clock`` ("10:30")
    in the owner's zone. Returns the start in UTC."""
    zone = pytz.timezone(owner_zone(config))
    local = datetime.fromisoformat(f"{date.strip()}T{clock.strip()}")
    start = zone.localize(local.replace(tzinfo=None)).astimezone(timezone.utc)
    minutes = max(10, min(int(minutes or DEFAULT_MINUTES), 480))
    await state.set_setting(TIME_KEY + f"{channel}:{item_id}", json.dumps(
        {"start": start.isoformat(), "minutes": minutes, "at": _utcnow().isoformat()}))
    return start


# ── Who and where ──

async def _profile(state, lead: dict) -> dict:
    from harvey.agents.linkedin_agent import FOUND_KEY, PROFILE_KEY
    from harvey.linkedin.pages import summary_headline, summary_location
    data = await _json_setting(state, PROFILE_KEY + lead["id"])
    text = data.get("text") or ""
    found = await _json_setting(state, FOUND_KEY + lead["id"])
    return {"text": text,
            "location": data.get("location") or summary_location(text) or found.get("location") or "",
            "headline": data.get("headline") or summary_headline(text) or found.get("headline") or ""}


async def _company(state, company_id: str):
    try:
        return await state.get_company(company_id) if company_id else None
    except Exception:
        return None


async def _prospect(state, prospect_id: str):
    try:
        return await state.get_prospect(prospect_id) if prospect_id else None
    except Exception:
        return None


async def _booked(state, key: str, fallback_at: str = "") -> tuple[str, str]:
    data = await _json_setting(state, BOOKED_KEY + key)
    if data.get("at"):
        return str(data["at"]), data.get("by") or "harvey"
    return fallback_at or "", "you"


async def _linkedin_item(state, config, lead: dict) -> dict:
    from harvey.linkedin.web import _history
    from harvey.send_window import recipient_zone, zone_for
    profile = await _profile(state, lead)
    prospect = await _prospect(state, lead.get("prospect_id") or "")
    company = await _company(state, lead.get("company_id") or (prospect.company_id if prospect else ""))
    zone = ""
    try:
        zone = await recipient_zone(state, prospect, None) if prospect else ""
    except Exception:
        zone = ""
    if not zone and profile["location"]:
        try:
            zone = zone_for(location=profile["location"])
        except Exception:
            zone = ""
    rows = await state.li_messages(lead_id=lead["id"], limit=5000)
    thread = [m for m in _history(rows) if m["kind"] != "invite_note" or m["body"]]
    key = f"linkedin:{lead['id']}"
    the_time, set_by = await meeting_time(state, config, "linkedin", lead["id"], zone)
    booked_at, booked_by = await _booked(state, key, lead.get("updated_at") or "")
    return {
        "key": key, "channel": "linkedin", "id": lead["id"], "stage": lead.get("stage") or "",
        "name": lead.get("name") or lead.get("first_name") or "", "role": profile["headline"] or lead.get("headline") or "",
        "company": lead.get("company") or (company.name if company else ""),
        "location": profile["location"] or (company.location if company else ""),
        "website": (company.website or company.domain) if company else "",
        "their_zone": zone, "their_zone_name": zone_name(zone) if zone else "",
        "when": the_time, "time_from": set_by, "booked_at": booked_at, "booked_by": booked_by,
        "where": {"tab": "linkedin", "profile_url": lead.get("profile_url") or "",
                  "thread_url": lead.get("thread_url") or "", "email": prospect.email if prospect else ""},
        "notes": (prospect.personalization_notes if prospect else "") or "",
        "profile_text": profile["text"][:1500],
        "thread": [{"direction": m["direction"], "body": m["body"], "at": m["at"], "kind": m["kind"]}
                   for m in thread],
    }


async def _whatsapp_item(state, config, contact: dict) -> dict:
    from harvey.whatsapp.pacing import recipient_zones
    from harvey.whatsapp.web import _history
    prospect = await _prospect(state, contact.get("prospect_id") or "")
    company = await _company(state, prospect.company_id if prospect else "")
    try:
        zones = await recipient_zones(state, contact, None)
    except Exception:
        zones = []
    zone = zones[0] if len(zones) == 1 else ""
    rows = await state.wa_messages(contact_id=contact["id"], limit=5000)
    key = f"whatsapp:{contact['id']}"
    the_time, set_by = await meeting_time(state, config, "whatsapp", contact["id"], zone)
    booked_at, booked_by = await _booked(state, key, contact.get("updated_at") or "")
    return {
        "key": key, "channel": "whatsapp", "id": contact["id"], "stage": contact.get("stage") or "",
        "name": contact.get("name") or contact.get("phone") or "", "role": prospect.title if prospect else "",
        "company": (company.name if company else "") or (prospect.company if prospect else ""),
        "location": (company.location if company else "") or contact.get("country") or "",
        "website": (company.website or company.domain) if company else "",
        "their_zone": zone, "their_zone_name": zone_name(zone) if zone else "",
        "when": the_time, "time_from": set_by, "booked_at": booked_at, "booked_by": booked_by,
        "where": {"tab": "whatsapp", "phone": contact.get("phone") or "", "email": contact.get("email") or "",
                  "source": contact.get("source") or ""},
        "notes": "\n".join(x for x in ((contact.get("context") or ""),
                                       (prospect.personalization_notes if prospect else "")) if x),
        "profile_text": "",
        "thread": [{"direction": m["direction"], "body": m["body"], "at": m["at"], "kind": m["kind"]}
                   for m in _history(rows) if m["kind"] != "alert"],
    }


async def _email_item(state, config, prospect, conversation=None) -> dict:
    from harvey.models.conversation import Conversation
    from harvey.send_window import recipient_zone
    company = await _company(state, prospect.company_id)
    try:
        zone = await recipient_zone(state, prospect, None)
    except Exception:
        zone = ""
    if conversation is None:
        rows = await state._li_query(
            "SELECT * FROM conversations WHERE prospect_id = ? ORDER BY updated_at DESC LIMIT 1", (prospect.id,))
        conversation = rows[0] if rows else None
    thread = []
    if conversation:
        for m in Conversation.thread_from_json(conversation.get("thread_json")):
            if m.sender == "auto":
                continue
            thread.append({"direction": "in" if m.sender == "prospect" else "out", "body": m.content,
                           "at": m.timestamp.isoformat() if m.timestamp else "", "kind": "email"})
    key = f"email:{prospect.id}"
    the_time, set_by = await meeting_time(state, config, "email", prospect.id, zone)
    updated = prospect.updated_at.isoformat() if getattr(prospect, "updated_at", None) else ""
    booked_at, booked_by = await _booked(state, key, updated)
    name = " ".join(x for x in (prospect.first_name, prospect.last_name) if x) or prospect.email
    return {
        "key": key, "channel": "email", "id": prospect.id, "stage": prospect.status,
        "name": name, "role": prospect.title or "", "company": (company.name if company else "") or prospect.company,
        "location": company.location if company else "",
        "website": (company.website or company.domain) if company else "",
        "their_zone": zone, "their_zone_name": zone_name(zone) if zone else "",
        "when": the_time, "time_from": set_by, "booked_at": booked_at, "booked_by": booked_by,
        "where": {"tab": "conversations", "email": prospect.email,
                  "conversation_id": (conversation or {}).get("id") or ""},
        "notes": prospect.personalization_notes or "", "profile_text": "", "thread": thread,
    }


async def item(state, config, channel: str, item_id: str) -> dict | None:
    """One person's card, whatever their stage."""
    if channel == "linkedin":
        lead = await state.li_get_lead(item_id)
        return await _linkedin_item(state, config, lead) if lead else None
    if channel == "whatsapp":
        contact = await state.wa_get_contact(item_id)
        return await _whatsapp_item(state, config, contact) if contact else None
    if channel == "email":
        prospect = await _prospect(state, item_id)
        return await _email_item(state, config, prospect) if prospect else None
    return None


async def _alert_target(state, key: str):
    """A meeting alert's key as (channel, id), or None: "linkedin:<lead>",
    "whatsapp:<contact>", "email:<conversation>" (its prospect)."""
    channel, _, rest = (key or "").partition(":")
    if channel in ("linkedin", "whatsapp") and rest:
        return channel, rest
    if channel == "email" and rest:
        rows = await state._li_query("SELECT prospect_id FROM conversations WHERE id = ?", (rest,))
        if rows and rows[0].get("prospect_id"):
            return "email", rows[0]["prospect_id"]
    return None


async def collect(state, config, now: datetime | None = None) -> dict:
    """{"upcoming", "earlier", "close", "owner_zone", "meet_link"} for the tab."""
    now = _aware(now) or _utcnow()
    items: list[dict] = []
    for lead in await state.li_leads(stages=("meeting",)):
        items.append(await _linkedin_item(state, config, lead))
    for contact in await state.wa_contacts(stages=("meeting",)):
        items.append(await _whatsapp_item(state, config, contact))
    for row in await state._li_query("SELECT id FROM prospects WHERE status = 'meeting'"):
        prospect = await _prospect(state, row["id"])
        if prospect is not None:
            items.append(await _email_item(state, config, prospect))
    for it in items:
        it["brief"] = await _cached_brief(state, it)
    far = datetime.max.replace(tzinfo=timezone.utc)
    upcoming = [i for i in items if not i["when"] or _aware(i["when"]["end"]) >= now]
    upcoming.sort(key=lambda i: _aware(i["when"]["start"]) if i["when"] else far)
    earlier = [i for i in items if i["when"] and _aware(i["when"]["end"]) < now]
    earlier.sort(key=lambda i: i["when"]["start"], reverse=True)

    booked = {i["key"] for i in items}
    close, seen = [], set()
    since = (now - timedelta(days=CLOSE_DAYS)).astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    rows = await state._li_query(
        "SELECT created_at, details_json FROM actions WHERE action_type = 'meeting_alert' AND created_at >= ? "
        "ORDER BY created_at DESC", (since,))
    for row in rows:
        try:
            alert = json.loads(row["details_json"] or "{}")
        except ValueError:
            continue
        target = await _alert_target(state, alert.get("key") or "")
        if target is None:
            continue
        key = f"{target[0]}:{target[1]}"
        if key in booked or key in seen:
            continue
        seen.add(key)
        card = await item(state, config, *target)
        if card is None or card["stage"] in ("declined", "opted_out", "lost", "closed", "closed_lost"):
            continue
        card["alert"] = {"at": row["created_at"], "when": alert.get("when") or "",
                         "service": alert.get("service") or "", "channel": alert.get("channel") or ""}
        card["brief"] = await _cached_brief(state, card)
        close.append(card)
    offer = getattr(getattr(config, "product", None), "offer", None)
    return {"upcoming": upcoming, "earlier": earlier, "close": close,
            "owner_zone": zone_name(owner_zone(config)), "owner_tz": owner_zone(config),
            "meet_link": (getattr(offer, "meeting_link", "") or "").strip(),
            "now": now.isoformat()}


async def mark_booked(state, channel: str, item_id: str) -> bool:
    """You say it's booked: the stage (or email status) moves to meeting."""
    if channel == "linkedin" and await state.li_get_lead(item_id):
        await state.li_set_stage(item_id, "meeting")
    elif channel == "whatsapp" and await state.wa_get_contact(item_id):
        await state.wa_set_stage(item_id, "meeting")
    elif channel == "email" and await _prospect(state, item_id):
        await state.update_prospect_status(item_id, "meeting")
    else:
        return False
    key = BOOKED_KEY + f"{channel}:{item_id}"
    if not await state.get_setting(key):
        await state.set_setting(key, json.dumps({"at": _utcnow().isoformat(), "by": "you"}))
    return True


# ── The prep brief ──

async def _cached_brief(state, card: dict) -> dict | None:
    data = await _json_setting(state, BRIEF_KEY + card["key"])
    if not data.get("text"):
        return None
    return {"text": data["text"], "at": data.get("at") or "",
            "stale": int(data.get("messages") or 0) != len(card["thread"])}


_MARKDOWN = re.compile(r"(\*\*|__|^#+\s*|`)", re.MULTILINE)


async def brief(state, brain, config, channel: str, item_id: str, again: bool = False) -> dict | None:
    """A short note to prepare for the meeting, by the brain, kept until the
    conversation changes (``again``: write it anew)."""
    from harvey.untrusted import UNTRUSTED_RULE, fence
    card = await item(state, config, channel, item_id)
    if card is None:
        return None
    cached = await _cached_brief(state, card)
    if cached and not cached["stale"] and not again:
        return cached
    persona, product = config.persona, config.product
    facts = "\n".join(f"{label}: {value}" for label, value in (
        ("Name", card["name"]), ("Role", card["role"]), ("Company", card["company"]),
        ("Location", card["location"]), ("Website", card["website"]),
        ("Channel", LABEL.get(channel, channel)),
        ("Meeting time", (card["when"] or {}).get("text") or "not set yet"),
        ("Research notes", card["notes"]), ("Their profile", card["profile_text"][:1200])) if value)
    thread = "\n\n".join(f"{'Them' if m['direction'] == 'in' else 'Us'} ({(m['at'] or '')[:10]}): {m['body']}"
                         for m in card["thread"][-40:])
    playbook = ""
    if hasattr(brain, "load_skill"):
        try:
            playbook = brain.load_skill("house_playbook") or ""
        except Exception:
            playbook = ""
    prompt = (
        f"You help {persona.name} ({persona.role} at {persona.company}) prepare for a sales call. "
        f"{persona.company} sells {product.name}: {product.description}\n"
        + (f"\nWHAT WE MAY CLAIM (house playbook):\n{playbook[:3000]}\n" if playbook else "")
        + f"\n{UNTRUSTED_RULE}\n\nABOUT THEM:\n{fence(facts, 'what we know about them')}\n"
        f"\nTHE CONVERSATION SO FAR:\n{fence(thread or '(no messages yet)', 'the conversation')}\n"
        "\nWrite a short prep brief for the call, in plain text, short lines, no markdown, no bullets "
        "symbols other than a plain hyphen, under 220 words, with these parts, each on its own line "
        "starting with its label:\n"
        "Who: who they are and their business, in a sentence.\n"
        "What they want: what they asked for or care about, and why now, from their own words.\n"
        "What we've said: anything we offered, promised or sent (a review, a time, a link, a price).\n"
        "Ask them: three questions to ask.\n"
        "Watch for: likely objections or sensitivities.\n"
        "Agenda: three short steps for the call.\n"
        "Use only what is above; if something isn't known, say so briefly. Output only the brief.")
    text = await brain.think(prompt, session_id="harvey-meetings", agent="meetings", task="meeting_brief")
    text = _MARKDOWN.sub("", (text or "").strip())
    if not text:
        return cached
    await state.set_setting(BRIEF_KEY + card["key"], json.dumps(
        {"text": text[:4000], "messages": len(card["thread"]), "at": _utcnow().isoformat()}))
    return {"text": text[:4000], "at": _utcnow().isoformat(), "stale": False}
