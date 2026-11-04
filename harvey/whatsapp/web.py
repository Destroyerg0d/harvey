"""The dashboard's WhatsApp tab: routes under /api/whatsapp.

Included by harvey/dashboard.py, so the dashboard's token guard covers every
route here like any other.
"""

import json
import logging
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from harvey.whatsapp import guards
from harvey.whatsapp.pacing import LINKED_AT_KEY
from harvey.whatsapp.store import HELD_FOR_REVIEW

logger = logging.getLogger("harvey.dashboard")
router = APIRouter()

MANUAL_STAGES = ("warm", "meeting", "declined")


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


async def today_items(state) -> list[dict]:
    """Today cards: a pause, drafts waiting, and sends that may have gone out."""
    items = []
    try:
        pause = await guards.active_pause(state)
        if pause:
            items.append({
                "key": "whatsapp-paused", "tone": "bad", "title": "WhatsApp is paused",
                "detail": str(pause.get("reason") or "Paused.") + (
                    f" Until {str(pause['until'])[:16].replace('T', ' ')} UTC, unless you resume it."
                    if pause.get("until") else " Until you resume it."),
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


def _running(stamp: str, cfg, now: datetime) -> bool:
    """A run is going (a lock older than a run can last was left by a crash)."""
    try:
        started = datetime.fromisoformat(stamp) if stamp else None
    except ValueError:
        return False
    return bool(started and now - started < timedelta(minutes=cfg.max_run_minutes + 15))


@router.get("/api/whatsapp")
async def whatsapp_overview():
    try:
        config = _config()
    except Exception as e:
        return JSONResponse({"error": f"Couldn't read the config: {e}"}, status_code=500)
    from harvey.agents.whatsapp_agent import AUTOPILOT_KEY, LAST_RUN_KEY, RUNNING_KEY, autopilot_on
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

        conversations = []
        talking = [c for c in contacts if c.get("last_inbound_at")]
        talking.sort(key=lambda c: c.get("last_inbound_at") or "", reverse=True)
        for contact in talking[:40]:
            rows = messages.get(contact["id"], [])
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
                "thread": [{"direction": m["direction"], "kind": m["kind"], "body": m["body"],
                            "at": m["sent_at"] or m["created_at"]}
                           for m in rows if m["status"] == "sent"][-6:],
            })

        try:
            last_run = json.loads(await state.get_setting(LAST_RUN_KEY) or "{}")
        except ValueError:
            last_run = {}
        override = (await state.get_setting(AUTOPILOT_KEY) or "").strip().lower()
        pause = await guards.active_pause(state)
        return {
            "enabled": cfg.enabled,
            "number": cfg.number,
            "linked_at": await state.get_setting(LINKED_AT_KEY),
            "linked": bool(await pacing.linked_at()) and not (pause and pause.get("kind") == "signed_out"),
            "new_number": await pacing.is_new_number(),
            "new_number_days": cfg.new_number_days,
            "autopilot": await autopilot_on(state, config),
            "autopilot_source": "dashboard" if override in ("on", "off") else "config",
            "hours": pacing.hours_text(),
            "in_hours": pacing.in_hours(),
            "pause": pause,
            "running": _running(await state.get_setting(RUNNING_KEY), cfg, now),
            "last_run": last_run if isinstance(last_run, dict) else {},
            "used": await pacing.used(),
            "caps": await pacing.caps(),
            "funnel": await state.wa_funnel(),
            "queue": queue,
            "drafts_waiting": sum(1 for q in queue if q["status"] == "draft"),
            "maybe_sent": sum(1 for q in queue if q["status"] == "maybe_sent"),
            "conversations": conversations,
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
    requeued = await state.wa_requeue_autopilot() if mode == "approval" else 0
    await state.log_action("whatsapp_mode", "dashboard", {"mode": mode, "requeued": requeued})
    return {"success": True, "mode": mode, "requeued": requeued}


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
