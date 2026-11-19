"""Everything waiting on you, one item each, on Today and on your phone.

Owner, 2026-11-19: "sometime you promise to give them improvement list to
prospects on linkedin I don't know you won't be giving to them and you are also
not telling me so ... make sure to tell me if any situation like that occurs ...
give me in needs you". Today used to show LinkedIn and WhatsApp drafts as bare
counts, never named a site review waiting for approval, and nothing at all
showed a promised write-up that hadn't been written (``site_review.promised``
was a CLI command only). Now each of these is its own item:

- a site review written and waiting for you (LinkedIn, email);
- a write-up Harvey promised that hasn't been written, and why (no site known);
- a LinkedIn or WhatsApp message held for you, and why;
- a WhatsApp number to check.

``items`` builds them for Today (harvey/dashboard.py get_today). ``announce``
runs every heartbeat cycle and logs a ``needs_you`` action the first time an
item is seen, so your next WhatsApp update says it once (harvey/owner_updates.py).
"""

from __future__ import annotations

import json
import logging

logger = logging.getLogger("harvey.needs_you")

# Items listed one by one per kind; the rest are counted in one more item.
MAX_LISTED = 10
SEEN_KEY = "needs_you:seen"
MAX_SEEN = 2000
HELD = "held for your review: "


def _first(name: str) -> str:
    return (name or "").strip() or "someone"


def _why(error: str) -> str:
    return (error or "")[len(HELD):].strip().rstrip(".") if (error or "").startswith(HELD) else ""


def _is_review(why: str) -> bool:
    from harvey.site_review import HELD_REASON
    return why.startswith(HELD_REASON[:24]) or "said yes to the site review" in why


def _capped(rows: list[dict], kind: str, tab: str, action: str, more_title: str) -> list[dict]:
    if len(rows) <= MAX_LISTED:
        return rows
    extra = len(rows) - MAX_LISTED
    return rows[:MAX_LISTED] + [{
        "key": f"{kind}-more", "tone": "warn", "title": more_title.format(n=extra),
        "detail": "They are listed in full on the tab.", "action": action, "tab": tab}]


async def _linkedin_held(state) -> list[dict]:
    out = []
    for m in await state.li_messages(statuses=("draft",), direction="out"):
        why = _why(m.get("error") or "")
        if not why:
            continue
        lead = await state.li_get_lead(m["lead_id"]) or {}
        name = _first(lead.get("name") or lead.get("first_name") or "")
        if _is_review(why):
            title = f"Site review for {name} (LinkedIn) is waiting for you"
            detail = ("They said yes to the improvement list. Harvey wrote it and is holding it "
                      "until you read it. Send, edit or skip it on the LinkedIn tab.")
            if "couldn't write it" in why:
                title = f"Promised {name} an improvement list on LinkedIn: Harvey couldn't write it"
                detail = (f"{why[0].upper() + why[1:]}. Write to them yourself, or tell Harvey "
                          "their website, on the LinkedIn tab.")
        else:
            title = f"LinkedIn message to {name} is held for you"
            detail = f"Why: {why}. Send, edit or skip it on the LinkedIn tab."
        out.append({"key": f"li-held-{m['id']}", "tone": "warn", "title": title, "detail": detail,
                    "action": "Open LinkedIn", "tab": "linkedin", "_review": _is_review(why)})
    # Site reviews first: someone is waiting for what they were promised.
    out.sort(key=lambda it: not it.pop("_review"))
    return _capped(out, "li-held", "linkedin", "Open LinkedIn",
                   "{n} more LinkedIn messages are held for you")


async def _whatsapp_held(state) -> list[dict]:
    out = []
    for m in await state.wa_messages(statuses=("draft",), direction="out"):
        why = _why(m.get("error") or "")
        if not why:
            continue
        contact = await state.wa_get_contact(m["contact_id"]) or {}
        name = _first(contact.get("name") or contact.get("phone") or "")
        out.append({"key": f"wa-held-{m['id']}", "tone": "warn",
                    "title": f"WhatsApp message to {name} is held for you",
                    "detail": f"Why: {why}. Send, edit or skip it on the WhatsApp tab.",
                    "action": "Open WhatsApp", "tab": "whatsapp"})
    for c in await state.wa_contacts(stages=("needs_review",)):
        name = _first(c.get("name") or c.get("raw_phone") or "")
        reason = (c.get("review_reason") or "the number needs checking").rstrip(".")
        out.append({"key": f"wa-number-{c['id']}", "tone": "warn",
                    "title": f"WhatsApp number to check: {name}",
                    "detail": f"{reason[0].upper() + reason[1:]}. Fix or skip it on the WhatsApp tab; "
                              "nothing is sent to them until then.",
                    "action": "Open WhatsApp", "tab": "whatsapp"})
    return _capped(out, "wa-held", "whatsapp", "Open WhatsApp",
                   "{n} more WhatsApp messages or numbers need you")


async def _email_reviews(state) -> list[dict]:
    from harvey.site_review import REVIEW_ANGLE
    out = []
    for row in await state.get_outbox(status="pending_review", angles=(REVIEW_ANGLE,), limit=50):
        who = row.get("to_email") or "someone"
        out.append({"key": f"email-review-{row['id']}", "tone": "warn",
                    "title": f"Site review for {who} (email) is waiting for you",
                    "detail": ("They said yes to the improvement list. Harvey wrote it and is holding "
                               "it until you read it on the Outbox tab."),
                    "action": "Open the Outbox", "tab": "outbox"})
    return out


async def _promised(state) -> list[dict]:
    from harvey.site_review import promised
    out = []
    for p in await promised(state):
        channel = "LinkedIn" if p.get("channel") == "linkedin" else "email"
        name = _first(p.get("who") or p.get("name") or "")
        if p.get("site"):
            why = "Harvey hasn't written it yet; it will on its next run"
        else:
            why = "No website is known for them, so nothing can be measured"
        key_id = p.get("lead_id") or p.get("prospect_id") or name
        out.append({"key": f"promised-{p.get('channel')}-{key_id}", "tone": "bad",
                    "title": f"Promised {name} an improvement list on {channel}: not sent yet",
                    "detail": f"{why}. Write to them yourself, or tell Harvey their website.",
                    "action": "Open LinkedIn" if channel == "LinkedIn" else "Read conversations",
                    "tab": "linkedin" if channel == "LinkedIn" else "conversations"})
    return out


async def items(state) -> list[dict]:
    """Today's items for everything above. Never raises: a part that fails is
    logged and left out."""
    out: list[dict] = []
    for part in (_promised, _linkedin_held, _email_reviews, _whatsapp_held):
        try:
            out += await part(state)
        except Exception as e:
            logger.warning(f"Needs you: {part.__name__} failed: {e}")
    return out


async def announce(state) -> int:
    """Log a ``needs_you`` action for each item not announced before, so the
    owner's WhatsApp update names it once. Returns how many were new."""
    current = await items(state)
    try:
        seen = list(json.loads(await state.get_setting(SEEN_KEY) or "[]"))
    except ValueError:
        seen = []
    known = set(seen)
    new = [it for it in current if it["key"] not in known and not it["key"].endswith("-more")]
    for it in new:
        await state.log_action("needs_you", "harvey", {"key": it["key"], "title": it["title"]})
        seen.append(it["key"])
    if new:
        await state.set_setting(SEEN_KEY, json.dumps(seen[-MAX_SEEN:]))
    return len(new)
