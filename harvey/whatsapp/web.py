"""The dashboard's WhatsApp tab: routes under /api/whatsapp.

Included by harvey/dashboard.py, so the dashboard's token guard covers every
route here like any other.
"""

import json
import logging
import re
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from harvey import channel_login
from harvey.whatsapp import guards
from harvey.whatsapp.pacing import LINKED_AT_KEY, LINKED_NUMBER_KEY
from harvey.whatsapp.schema import OWNER_STAGE
from harvey.whatsapp.store import HELD_FOR_REVIEW

logger = logging.getLogger("harvey.dashboard")
router = APIRouter()

MANUAL_STAGES = ("warm", "meeting", "declined")
TYPE_TO_TURN_ON = "whatsapp"
RISK = ("Automating WhatsApp breaks its terms and can get the number banned, with no appeal worth "
        "counting on. Harvey keeps its limits low and stops at any warning, but the risk stays with "
        "you, so use a separate number you can afford to lose, never your main one.")
# What only a pasted list has in its number box.
_ONE_NUMBER_ONLY_RE = re.compile(r"[,;\n\r\t|]")


def _state():
    from harvey import dashboard
    return dashboard._state()


def _config():
    from harvey.config import load_config
    from harvey import dashboard
    return load_config(str(dashboard._resolve_config_file()))


def _refuse(message: str, code: int = 409):
    return JSONResponse({"success": False, "message": message}, status_code=code)


async def _body(request: Request) -> dict:
    try:
        data = await request.json()
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def _contact_view(contact: dict | None) -> dict:
    if not contact:
        return {}
    keep = ("id", "phone", "raw_phone", "name", "email", "country", "source", "context", "stage",
            "review_reason", "no_message_reason", "last_outbound_at", "last_inbound_at", "next_action_at",
            "prospect_id")
    return {k: contact.get(k) for k in keep}


async def _connection(state, config) -> dict:
    """The connection card: where things stand, in plain words, and what to do next."""
    from harvey.agents.whatsapp_agent import ENABLED_KEY, channel_on
    from harvey.pidfile import loop_lock_held
    from harvey.whatsapp.pacing import WhatsAppPacing
    cfg = config.channels.whatsapp
    attempt = await channel_login.read_attempt(state, "whatsapp")
    pause = await guards.active_pause(state)
    on = await channel_on(state, config)
    running = await channel_login.session_going(state, "whatsapp", cfg)
    linked_at = await state.get_setting(LINKED_AT_KEY)
    # The ramp counts from the first link; "connected since" from the latest one.
    since = attempt.get("finished") if attempt.get("status") == channel_login.CONNECTED else ""
    number = await state.get_setting(LINKED_NUMBER_KEY) or cfg.number
    if attempt.get("live"):
        kind = "waiting"
        step = ("A Chrome window has opened on this PC with a QR code. On the phone with the separate "
                "number: WhatsApp, Linked devices, Link a device, then scan it. Harvey never sees a "
                "code. This page updates by itself.")
    elif pause and pause.get("kind") == "signed_out":
        kind = "signed_out"
        step = ("WhatsApp unlinked this number, so nothing is read or sent. Press Reconnect, then scan "
                "the QR code in the Chrome window that opens on this PC.")
    elif linked_at:
        kind = "connected"
        hours = WhatsAppPacing(state, cfg).hours_text()
        step = (f"Harvey runs WhatsApp by each person's own clock, or {hours} when that isn't known." if on
                else "Linked and ready. Turn it on below when you want Harvey to run it.")
    else:
        kind = "not_connected"
        step = ("Press Connect. A Chrome window opens on this PC with a QR code to scan from the phone "
                "with the separate number.")
    override = (await state.get_setting(ENABLED_KEY) or "").strip().lower()
    return {
        "state": kind, "since": since or linked_at or None, "number": number, "step": step, "enabled": on,
        "enabled_source": "dashboard" if override in ("on", "off") else "config",
        "session_running": running, "deadline": attempt.get("deadline") if attempt.get("live") else None,
        "note": channel_login.attempt_note(attempt, cfg.login_timeout_minutes),
        "can_connect": not attempt.get("live") and not running,
        "harvey_running": loop_lock_held(), "risk": RISK, "confirm_word": TYPE_TO_TURN_ON,
    }


async def today_items(state) -> list[dict]:
    """Today cards: a pause, on but not connected, drafts waiting, and sends that
    may have gone out."""
    from harvey.agents.whatsapp_agent import channel_on
    items = []
    try:
        pause = await guards.active_pause(state)
        signed_out = bool(pause and pause.get("kind") == "signed_out")
        try:
            config = _config()
            on = await channel_on(state, config)
            connection = (await _connection(state, config))["state"] if on else ""
        except Exception as e:
            logger.warning(f"WhatsApp today card couldn't read the config: {e}")
            on, connection = signed_out, ""
        if signed_out and on:
            items.append({
                "key": "whatsapp-signed-out", "tone": "bad", "title": "WhatsApp is on but its number is unlinked",
                "detail": "Nothing is read or sent until it's linked again. Press Reconnect on the WhatsApp "
                          "tab and scan the QR code in the Chrome window that opens on this PC.",
                "action": "Open WhatsApp", "tab": "whatsapp"})
        elif pause and not signed_out:
            items.append({
                "key": "whatsapp-paused", "tone": "bad", "title": "WhatsApp is paused",
                "detail": str(pause.get("reason") or "Paused").rstrip(".") + "." + (
                    f" Until {str(pause['until'])[:16].replace('T', ' ')} UTC, unless you resume it."
                    if pause.get("until") else " Until you resume it."),
                "action": "Open WhatsApp", "tab": "whatsapp"})
        elif connection == "not_connected":
            items.append({
                "key": "whatsapp-not-connected", "tone": "bad", "title": "WhatsApp is on but no phone is linked",
                "detail": "Press Connect on the WhatsApp tab and scan the QR code in the Chrome window that "
                          "opens on this PC.",
                "action": "Open WhatsApp", "tab": "whatsapp"})
        maybe = await state.wa_messages(statuses=("maybe_sent",), direction="out")
        if maybe:
            n = len(maybe)
            items.append({
                "key": "whatsapp-maybe-sent", "tone": "bad",
                "title": f"{n} WhatsApp message{'' if n == 1 else 's'} may have gone out",
                "detail": "Enter was pressed but the chat didn't show it. Harvey won't send it again. "
                          "Check the chat on your phone and say whether it went.",
                "action": "Open WhatsApp", "tab": "whatsapp"})
        drafts = await state.wa_messages(statuses=("draft",), direction="out")
        if drafts:
            n = len(drafts)
            items.append({
                "key": "whatsapp-drafts", "tone": "warn",
                "title": f"{n} WhatsApp draft{'' if n == 1 else 's'} waiting for approval",
                "detail": "Nothing goes out on WhatsApp until you approve it (unless WhatsApp "
                          "autopilot is on). Read them on the WhatsApp tab.",
                "action": "Open WhatsApp", "tab": "whatsapp"})
    except Exception as e:
        logger.warning(f"WhatsApp today cards failed: {e}")
    return items


def _history(rows: list[dict]) -> list[dict]:
    """Everything that has passed between you, oldest first: what was sent, what
    may have gone, and what you approved that was then stopped, with why."""
    out = []
    for m in rows:
        stopped = m["status"] == "skipped" and bool(m["approved_by"] and m["error"])
        if m["status"] not in ("sent", "maybe_sent") and not stopped:
            continue
        out.append({"direction": m["direction"], "kind": m["kind"], "body": m["body"],
                    "at": m["sent_at"] or m["created_at"], "by": m["approved_by"],
                    "status": "not_sent" if stopped else m["status"],
                    "note": (m["error"] or "") if stopped or m["status"] == "maybe_sent" else ""})
    return out


async def _reply_block(state, config, contact: dict, rows: list[dict], **stop_options) -> str:
    """Why you can't write to this contact from the conversation, or "". The send
    checks all of this again, since any of it can change after you've written."""
    from harvey.whatsapp.contacts import stop_reason
    stage = contact.get("stage")
    if stage == "declined":
        why = contact.get("no_message_reason") or "they said no"
        return f"You can't write to them here: {why}, so Harvey never messages them again."
    if stage == "no_whatsapp":
        return "You can't write to them here: this number isn't on WhatsApp."
    if stage == "needs_review" or not contact.get("phone"):
        return "You can't write to them here until their number is fixed, under Numbers to check."
    why = await stop_reason(state, config, contact, replying=True, **stop_options)
    if why:
        return f"You can't write to them here: {why}."
    if not any(m["direction"] == "in" for m in rows):
        return ("They haven't written back yet. Until they do, only the first message and the one "
                "follow-up go, as the limits say.")
    if any(m["status"] == "maybe_sent" for m in rows):
        return ("A message to them may have gone out. Say whether it did in the queue above, then "
                "you can write again.")
    cap = config.channels.whatsapp.max_unanswered
    waiting = [m for m in rows if m["direction"] == "out" and m["status"] == "approved"
               and m["approved_by"] == "you"]
    unanswered = 0
    for m in rows:
        unanswered = 0 if m["direction"] == "in" else unanswered + (m["status"] == "sent")
    if unanswered + len(waiting) >= cap:
        return f"{cap} messages without a reply, counting what's waiting to go. Wait for them to answer."
    return ""


def _due_messages(contacts: list[dict], messages: dict[str, list[dict]], cfg, now: datetime) -> list[dict]:
    """Contacts whose next message is due, and which one it is."""
    from harvey.agents.whatsapp_agent import next_due
    out = []
    for contact in contacts:
        if contact["stage"] not in ("queued", "messaged") or contact.get("no_message_reason"):
            continue
        which, due = next_due(contact, messages.get(contact["id"], []), cfg, now)
        if which:
            out.append({"contact": _contact_view(contact), "due_at": due.isoformat() if due else None,
                        "which": "first message" if which == "first" else "follow-up"})
    return out[:50]


@router.get("/api/whatsapp")
async def whatsapp_overview():
    try:
        config = _config()
    except Exception as e:
        return JSONResponse({"error": f"Couldn't read the config: {e}"}, status_code=500)
    from harvey.agents.whatsapp_agent import AUTOPILOT_KEY, LAST_RUN_KEY, autopilot_on
    from harvey.whatsapp.contacts import blocked_numbers
    from harvey.whatsapp.pacing import WhatsAppPacing
    try:
        state = _state()
        await state.init_db()
        cfg = config.channels.whatsapp
        pacing = WhatsAppPacing(state, cfg)
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        contacts = await state.wa_contacts()
        by_id = {c["id"]: c for c in contacts}
        messages: dict[str, list[dict]] = {}
        for m in await state.wa_messages(limit=100000):
            messages.setdefault(m["contact_id"], []).append(m)

        queue = []
        for m in await state.wa_messages(statuses=("draft", "approved", "failed", "maybe_sent"),
                                         direction="out", limit=500):
            # Failed sends older than two weeks drop off; one that may have gone never does.
            stale = (m.get("created_at") or "") < (now - timedelta(days=14)).isoformat()
            if m["status"] == "failed" and stale:
                continue
            queue.append({**{k: m[k] for k in ("id", "kind", "body", "status", "error", "created_at",
                                               "approved_by")},
                          "held": (m["error"] or "").startswith(HELD_FOR_REVIEW),
                          "contact": _contact_view(by_id.get(m["contact_id"]))})

        # Everyone a message has actually passed between: all of them, each with
        # the whole history.
        conversations = []
        numbers = blocked_numbers(config)
        for contact in contacts:
            if contact["stage"] == OWNER_STAGE:
                continue        # your own phone's meeting alerts: in the queue, not a conversation
            rows = messages.get(contact["id"], [])
            thread = _history(rows)
            if not thread and not contact.get("last_inbound_at"):
                continue
            inbound = [m for m in rows if m["direction"] == "in"]
            draft = next((m for m in reversed(rows) if m["direction"] == "out" and m["kind"] == "reply"
                          and m["status"] in ("draft", "approved")), None)
            conversations.append({
                "contact": _contact_view(contact),
                "latest_in": ({"body": inbound[-1]["body"], "intent": inbound[-1]["intent"],
                               "at": inbound[-1]["sent_at"] or inbound[-1]["created_at"]}
                              if inbound else None),
                "draft": ({"id": draft["id"], "body": draft["body"], "status": draft["status"],
                           "error": draft["error"]} if draft else None),
                "thread": thread,
                "last_at": max([m["at"] or "" for m in thread] + [contact.get("last_inbound_at") or ""]),
                "reply_blocked": await _reply_block(state, config, contact, rows, numbers=numbers,
                                                    scan_phones=False),
            })
        conversations.sort(key=lambda c: c["last_at"], reverse=True)

        try:
            last_run = json.loads(await state.get_setting(LAST_RUN_KEY) or "{}")
        except ValueError:
            last_run = {}
        override = (await state.get_setting(AUTOPILOT_KEY) or "").strip().lower()
        pause = await guards.active_pause(state)
        connection = await _connection(state, config)
        return {
            "enabled": connection["enabled"],
            "connection": connection,
            "warnings": await channel_login.recent_pauses(state, "whatsapp_paused"),
            "number": connection["number"],
            "linked_at": await state.get_setting(LINKED_AT_KEY),
            "linked": bool(await pacing.linked_at()) and not (pause and pause.get("kind") == "signed_out"),
            "new_number": await pacing.is_new_number(),
            "new_number_days": cfg.new_number_days,
            "autopilot": await autopilot_on(state, config),
            "autopilot_source": "dashboard" if override in ("on", "off") else "config",
            "hours": pacing.hours_text(),
            "in_hours": pacing.in_hours(),
            "pause": pause,
            "running": connection["session_running"],
            "last_run": last_run if isinstance(last_run, dict) else {},
            "used": await pacing.used(),
            "caps": await pacing.caps(),
            "funnel": await state.wa_funnel(),
            "queue": queue,
            "drafts_waiting": sum(1 for q in queue if q["status"] == "draft"),
            "maybe_sent": sum(1 for q in queue if q["status"] == "maybe_sent"),
            "conversations": conversations,
            "follow_ups_due": _due_messages(contacts, messages, cfg, now),
            "review": [_contact_view(c) for c in contacts if c["stage"] == "needs_review"][:50],
            "stopped": [_contact_view(c) for c in contacts
                        if c.get("no_message_reason") and c["stage"] in ("queued", "messaged")][:50],
        }
    except Exception as e:
        logger.exception("whatsapp load failed")
        return {"error": str(e)}


@router.post("/api/whatsapp/messages/approve-all")
async def whatsapp_approve_all():
    state = _state()
    await state.init_db()
    n = await state.wa_approve(None)
    held = len([m for m in await state.wa_messages(statuses=("draft",), direction="out")
                if (m["error"] or "").startswith(HELD_FOR_REVIEW)])
    return {"success": True, "approved": n, "held": held}


@router.post("/api/whatsapp/messages/{message_id}/{action}")
async def whatsapp_message_action(message_id: str, action: str, request: Request):
    state = _state()
    await state.init_db()
    message = await state.wa_get_message(message_id)
    if message is None or message["direction"] != "out":
        return _refuse("No such WhatsApp draft.", 404)
    if action == "approve":
        ok = await state.wa_approve(message_id)
    elif action == "skip":
        ok = await state.wa_skip(message_id)
    elif action == "retry":
        ok = await state.wa_retry(message_id)
        if not ok and message["status"] == "maybe_sent":
            return _refuse("This one may have gone out, so Harvey won't send it again. Check the chat "
                           "on your phone and say whether it went.")
    elif action in ("went", "not_sent"):
        ok = await state.wa_resolve_maybe_sent(message_id, went=action == "went")
    elif action == "edit":
        from harvey.agents.whatsapp_agent import draft_problem
        body = str((await _body(request)).get("body") or "").strip()
        try:
            booking = _config().product.offer.booking_url or ""
        except Exception:
            booking = ""
        problem = draft_problem(message["kind"], body, booking)
        if problem:
            return _refuse(f"That won't do: {problem}.", 400)
        ok = await state.wa_edit_draft(message_id, body)
        if ok:
            await state.log_action("whatsapp_edited", "dashboard",
                                   {"message_id": message_id, "old_body": message["body"]})
    else:
        return _refuse("Unknown action.", 404)
    if not ok:
        return _refuse("That message has changed (sent, skipped or approved already). Refresh.")
    return {"success": True}


@router.post("/api/whatsapp/autopilot")
async def whatsapp_autopilot(request: Request):
    """{"mode": "autopilot", "confirm": "autopilot"} turns it on; {"mode":
    "approval"} turns it off at once and sends back what it approved."""
    from harvey.agents.whatsapp_agent import AUTOPILOT_KEY
    body = await _body(request)
    mode = body.get("mode")
    if mode not in ("approval", "autopilot"):
        return _refuse('expected {"mode": "approval"} or {"mode": "autopilot", "confirm": "autopilot"}', 400)
    if mode == "autopilot" and str(body.get("confirm") or "").strip().lower() != "autopilot":
        return _refuse('Type "autopilot" to confirm: Harvey will send on WhatsApp without asking you.', 400)
    state = _state()
    await state.init_db()
    await state.set_setting(AUTOPILOT_KEY, "on" if mode == "autopilot" else "off")
    if mode == "autopilot":
        # Autopilot is Harvey running WhatsApp by itself: it switches the channel on too.
        from harvey.agents.whatsapp_agent import ENABLED_KEY
        await state.set_setting(ENABLED_KEY, "on")
    requeued = await state.wa_requeue_autopilot() if mode == "approval" else 0
    await state.log_action("whatsapp_mode", "dashboard", {"mode": mode, "requeued": requeued})
    return {"success": True, "mode": mode, "requeued": requeued}


# Declared before /api/whatsapp/{action} below, which would otherwise take these.

@router.get("/api/whatsapp/login")
async def whatsapp_login_status():
    """The connection card on its own: what the tab polls during a sign-in."""
    state = _state()
    await state.init_db()
    try:
        return await _connection(state, _config())
    except Exception as e:
        return _refuse(f"Couldn't read the config: {e}", 500)


@router.post("/api/whatsapp/login")
async def whatsapp_login():
    """Open the QR code window on this PC. Harvey still never handles a code."""
    state = _state()
    await state.init_db()
    try:
        cfg = _config().channels.whatsapp
    except Exception as e:
        return _refuse(f"Couldn't read the config: {e}", 500)
    started, why = await channel_login.start(
        state, "whatsapp", minutes=cfg.login_timeout_minutes,
        session_running=await channel_login.session_going(state, "whatsapp", cfg))
    return {"success": True} if started else _refuse(why)


@router.post("/api/whatsapp/login/cancel")
async def whatsapp_login_cancel():
    state = _state()
    await state.init_db()
    ended, why = await channel_login.cancel(state, "whatsapp")
    return {"success": True} if ended else _refuse(why)


@router.post("/api/whatsapp/enabled")
async def whatsapp_enabled(request: Request):
    """{"mode": "on", "confirm": "whatsapp"} turns it on; {"mode": "off"} turns it
    off at once. Read by the heartbeat every cycle, so neither needs a restart."""
    from harvey.agents.whatsapp_agent import ENABLED_KEY
    body = await _body(request)
    mode = body.get("mode")
    if mode not in ("on", "off"):
        return _refuse('expected {"mode": "on", "confirm": "whatsapp"} or {"mode": "off"}', 400)
    state = _state()
    await state.init_db()
    if mode == "on":
        try:
            connection = await _connection(state, _config())
        except Exception as e:
            return _refuse(f"Couldn't read the config: {e}", 500)
        if connection["state"] != "connected":
            why = {"waiting": "Finish linking first: scan the QR code in the Chrome window on this PC.",
                   "signed_out": "WhatsApp unlinked this number. Press Reconnect and scan the QR code first."}
            return _refuse(why.get(connection["state"], "Connect WhatsApp first: no phone is linked yet."))
        if str(body.get("confirm") or "").strip().lower() != TYPE_TO_TURN_ON:
            return _refuse(f'Type "{TYPE_TO_TURN_ON}" to confirm. {RISK}', 400)
    await state.set_setting(ENABLED_KEY, mode)
    await state.log_action("whatsapp_switch", "dashboard", {"mode": mode})
    return {"success": True, "mode": mode}


@router.post("/api/whatsapp/contacts")
async def whatsapp_add_contact(request: Request):
    """Queue one person who gave you their number themselves. One at a time and
    with a source, on purpose: there is no way to upload a list."""
    from harvey.whatsapp.contacts import queue_contact
    body = await _body(request)
    phone, name, source = body.get("phone"), body.get("name"), body.get("source")
    if not all(isinstance(x, str) or x is None for x in (phone, name, source)):
        return _refuse("One person at a time: a number, a name and where they gave it.", 400)
    if _ONE_NUMBER_ONLY_RE.search(phone or "") or (phone or "").count("+") > 1:
        return _refuse("One number at a time. There is no list upload, on purpose: each person has "
                       "to have given you their number themselves.", 400)
    state = _state()
    await state.init_db()
    try:
        config = _config()
    except Exception as e:
        return _refuse(f"Couldn't read the config: {e}", 500)
    try:
        result = await queue_contact(state, phone=phone, name=name or "", source=source, config=config)
    except ValueError as e:
        why = str(e)
        return _refuse(why[:1].upper() + why[1:] + ".", 400)
    if result.status == "refused":
        return JSONResponse({"success": False, "status": "refused", "contact_id": result.contact_id,
                             "message": f"Refused: {result.reason}."}, status_code=409)
    message = {
        "queued": "Queued. Harvey drafts their first message on its next run, and it waits here for "
                  "your approval.",
        "needs_review": f"Held, not queued: {result.reason}. Harvey never guesses a country code. Write "
                        "the number with its + and country code, like +14155550100: fix it under "
                        "Numbers to check on this tab.",
        "duplicate": "Already here: nothing changed but any blank details.",
    }[result.status]
    return {"success": True, "status": result.status, "contact_id": result.contact_id, "message": message}


@router.post("/api/whatsapp/contacts/{contact_id}/reply")
async def whatsapp_own_reply(contact_id: str, request: Request):
    """Your own words, in the same queue: approved by you, then the same caps,
    hours and stop rules as anything else that goes out."""
    from harvey.agents.whatsapp_agent import draft_problem
    state = _state()
    await state.init_db()
    contact = await state.wa_get_contact(contact_id)
    if contact is None:
        return _refuse("No such WhatsApp contact.", 404)
    try:
        config = _config()
    except Exception as e:
        return _refuse(f"Couldn't read the config: {e}", 500)
    why = await _reply_block(state, config, contact, await state.wa_messages(contact_id=contact_id))
    if why:
        return _refuse(why)
    body = str((await _body(request)).get("body") or "").strip()
    booking = getattr(getattr(config.product, "offer", None), "booking_url", "") or ""
    problem = draft_problem("reply", body, booking)
    if problem:
        return _refuse(f"That won't do: {problem}.", 400)
    # Yours replaces what Harvey had drafted for this conversation: both must not go.
    replaced = 0
    for waiting in await state.wa_pending_for(contact_id, kinds=("reply",)):
        if waiting["approved_by"] != "you":
            replaced += await state.wa_skip(waiting["id"], "you wrote your own")
    message_id = await state.wa_add_message(contact_id, direction="out", kind="reply", body=body,
                                            status="approved", approved_by="you")
    await state.log_action("whatsapp_own_reply", "dashboard",
                           {"whatsapp_contact_id": contact_id, "message_id": message_id, "replaced": replaced})
    return {"success": True, "id": message_id, "replaced": replaced}


@router.post("/api/whatsapp/contacts/{contact_id}/stage")
async def whatsapp_contact_stage(contact_id: str, request: Request):
    """Mark a contact warm, a meeting booked, or declined (which also stops
    their email, as a no on WhatsApp does). A number waiting for review that
    you drop just stops here: it says nothing about their email."""
    stage = str((await _body(request)).get("stage") or "")
    if stage not in MANUAL_STAGES:
        return _refuse(f"stage must be one of {', '.join(MANUAL_STAGES)}", 400)
    state = _state()
    await state.init_db()
    contact = await state.wa_get_contact(contact_id)
    if contact is None:
        return _refuse("No such WhatsApp contact.", 404)
    if stage == "declined":
        if contact["stage"] == "needs_review":
            await state.wa_set_stage(contact_id, "declined", no_message_reason="you dropped this number")
            return {"success": True, "stage": "declined"}
        from harvey.agents.whatsapp_agent import WhatsAppAgent
        try:
            config = _config()
        except Exception as e:
            return _refuse(f"Couldn't read the config: {e}", 500)
        agent = WhatsAppAgent(None, state, config, driver_factory=lambda: None)
        await agent.decline(contact, "", "marked by you")
        return {"success": True, "stage": "declined"}
    new = await state.wa_set_stage(contact_id, stage)
    if new != stage:
        return _refuse(f"They're {new}; a contact only moves forward.")
    return {"success": True, "stage": new}


@router.post("/api/whatsapp/contacts/{contact_id}/number")
async def whatsapp_contact_number(contact_id: str, request: Request):
    """The right number for a contact waiting for review. Nothing is guessed
    here either: it needs a + or a country."""
    from harvey.whatsapp.contacts import fix_number
    body = await _body(request)
    state = _state()
    await state.init_db()
    try:
        config = _config()
    except Exception as e:
        return _refuse(f"Couldn't read the config: {e}", 500)
    ok, why = await fix_number(state, config, contact_id, str(body.get("phone") or ""),
                               str(body.get("country") or ""))
    if not ok:
        return _refuse(why, 400)
    return {"success": True}


@router.post("/api/whatsapp/{action}")
async def whatsapp_pause_toggle(action: str):
    """The kill switch: pause holds at the next step of a run already going."""
    if action not in ("pause", "resume"):
        return _refuse("Unknown action.", 404)
    state = _state()
    await state.init_db()
    if action == "pause":
        await guards.set_pause(state, "Paused by you from the dashboard", kind="manual", hours=None)
    else:
        await guards.clear_pause(state)
    return {"success": True}
