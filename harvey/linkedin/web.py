"""The dashboard's LinkedIn tab: routes under /api/linkedin.

Included by harvey/dashboard.py, so the dashboard's token guard covers
every route here like any other.
"""

import json
import logging
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from harvey import channel_login
from harvey.linkedin import guards
from harvey.linkedin.cli import SIGNED_IN_KEY
from harvey.linkedin.store import HELD_FOR_REVIEW, MAYBE_SENT

logger = logging.getLogger("harvey.dashboard")
router = APIRouter()

MANUAL_STAGES = ("warm", "meeting", "declined")
TYPE_TO_TURN_ON = "linkedin"
RISK = ("Automating a LinkedIn account breaks LinkedIn's terms and can get the account restricted. "
        "Harvey keeps its limits low and stops at any warning, but the risk stays with you, so use "
        "an account you can afford to lose.")
# Long enough for any real reply, short enough that typing it can't take all run.
MAX_REPLY_CHARS = 2000


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


def _lead_view(lead: dict | None) -> dict:
    if not lead:
        return {}
    keep = ("id", "name", "first_name", "headline", "company", "profile_url", "stage",
            "invited_at", "accepted_at", "last_outbound_at", "last_inbound_at",
            "next_action_at", "no_message_reason", "prospect_id", "note")
    return {k: lead.get(k) for k in keep}


async def _last_run(state) -> dict:
    from harvey.agents.linkedin_agent import LAST_RUN_KEY
    try:
        last = json.loads(await state.get_setting(LAST_RUN_KEY) or "{}")
    except ValueError:
        return {}
    return last if isinstance(last, dict) else {}


async def _connection(state, config) -> dict:
    """The connection card: where things stand, in plain words, and what to do next."""
    from harvey.agents.linkedin_agent import ENABLED_KEY, channel_on
    from harvey.linkedin.pacing import Pacing
    from harvey.pidfile import loop_lock_held
    cfg = config.channels.linkedin
    attempt = await channel_login.read_attempt(state, "linkedin")
    pause = await guards.active_pause(state)
    on = await channel_on(state, config)
    running = await channel_login.session_going(state, "linkedin", cfg)
    last = await _last_run(state)
    # A run that finished got past its own sign-in check: an account signed in
    # before Harvey kept this date is connected all the same.
    since = await state.get_setting(SIGNED_IN_KEY) or (
        last.get("started") if last.get("outcome") == "done" else "")
    minutes = cfg.login_timeout_minutes
    if attempt.get("live"):
        kind = "waiting"
        step = ("A Chrome window has opened on this PC. Sign in to LinkedIn there yourself; Harvey never "
                "sees your password or codes. This page updates by itself.")
    elif pause and pause.get("kind") == "signed_out":
        kind = "signed_out"
        step = ("LinkedIn signed this profile out, so nothing is read or sent. Press Reconnect, then sign "
                "in again in the Chrome window that opens on this PC.")
    elif since:
        kind = "connected"
        hours = Pacing(state, cfg).hours_text()
        step = f"Harvey runs LinkedIn {hours}." if on else "Signed in and ready. Turn it on below when you want Harvey to run it."
    else:
        kind = "not_connected"
        step = "Press Connect. A Chrome window opens on this PC, and you sign in to LinkedIn there yourself."
    override = (await state.get_setting(ENABLED_KEY) or "").strip().lower()
    return {
        "state": kind, "since": since or None, "step": step, "enabled": on,
        "enabled_source": "dashboard" if override in ("on", "off") else "config",
        "session_running": running, "deadline": attempt.get("deadline") if attempt.get("live") else None,
        "note": channel_login.attempt_note(attempt, minutes),
        "can_connect": not attempt.get("live") and not running,
        "harvey_running": loop_lock_held(), "risk": RISK, "confirm_word": TYPE_TO_TURN_ON,
    }


async def today_items(state) -> list[dict]:
    """Today cards: LinkedIn drafts waiting, a pause, and on but not connected."""
    from harvey.agents.linkedin_agent import channel_on
    items = []
    try:
        pause = await guards.active_pause(state)
        signed_out = bool(pause and pause.get("kind") == "signed_out")
        try:
            config = _config()
            on = await channel_on(state, config)
            connection = (await _connection(state, config))["state"] if on else ""
        except Exception as e:
            logger.warning(f"LinkedIn today card couldn't read the config: {e}")
            on, connection = signed_out, ""
        if signed_out and on:
            items.append({
                "key": "linkedin-signed-out", "tone": "bad", "title": "LinkedIn is on but signed out",
                "detail": "Nothing is read or sent until it's connected again. Press Reconnect on the "
                          "LinkedIn tab and sign in in the Chrome window that opens on this PC.",
                "action": "Open LinkedIn", "tab": "linkedin"})
        elif pause and not signed_out:
            items.append({
                "key": "linkedin-paused", "tone": "bad", "title": "LinkedIn is paused",
                "detail": str(pause.get("reason") or "Paused").rstrip(".") + "." + (
                    f" Until {str(pause['until'])[:16].replace('T', ' ')} UTC, unless you resume it."
                    if pause.get("until") else " Until you resume it."),
                "action": "Open LinkedIn", "tab": "linkedin"})
        elif connection == "not_connected":
            items.append({
                "key": "linkedin-not-connected", "tone": "bad", "title": "LinkedIn is on but not connected",
                "detail": "Press Connect on the LinkedIn tab and sign in in the Chrome window that opens "
                          "on this PC.",
                "action": "Open LinkedIn", "tab": "linkedin"})
        drafts = await state.li_messages(statuses=("draft",), direction="out")
        if drafts:
            n = len(drafts)
            items.append({
                "key": "linkedin-drafts", "tone": "warn",
                "title": f"{n} LinkedIn draft{'' if n == 1 else 's'} waiting for approval",
                "detail": "Nothing goes out on LinkedIn until you approve it (unless LinkedIn "
                          "autopilot is on). Read them on the LinkedIn tab.",
                "action": "Open LinkedIn", "tab": "linkedin"})
    except Exception as e:
        logger.warning(f"LinkedIn today cards failed: {e}")
    return items


def _history(rows: list[dict]) -> list[dict]:
    """Everything that has passed between you, oldest first: what was sent, what
    may have gone, and what you approved that was then stopped, with why."""
    out = []
    for m in rows:
        maybe = m["status"] == "failed" and (m["error"] or "").startswith(MAYBE_SENT)
        stopped = m["status"] == "skipped" and bool(m["approved_by"] and m["error"])
        if m["status"] != "sent" and not maybe and not stopped:
            continue
        out.append({"direction": m["direction"], "kind": m["kind"], "body": m["body"],
                    "at": m["sent_at"] or m["created_at"], "by": m["approved_by"],
                    "status": "maybe_sent" if maybe else "not_sent" if stopped else "sent",
                    "note": (m["error"] or "") if maybe or stopped else ""})
    return out


async def _reply_block(state, config, lead: dict, rows: list[dict], entries=None) -> str:
    """Why you can't write to this lead from the conversation, or "". The send
    checks the first two again, since either can change after you've written."""
    from harvey.agents.linkedin_agent import said_no
    why = await said_no(state, config, lead, entries)
    if why:
        return f"You can't write to them here: {why}, so Harvey never contacts them again."
    if not lead.get("last_inbound_at") and not any(m["direction"] == "in" for m in rows):
        return ("They haven't written back yet. Until they do, only the first message and the one "
                "follow-up go, as the limits say.")
    cap = config.channels.linkedin.max_unanswered
    waiting = [m for m in rows if m["direction"] == "out" and m["status"] == "approved"
               and m["approved_by"] == "you" and m["kind"] != "invite_note"]
    unanswered = 0
    for m in rows:
        unanswered = 0 if m["direction"] == "in" else unanswered + (m["status"] == "sent")
    if unanswered + len(waiting) >= cap:
        return f"{cap} messages without a reply, counting what's waiting to go. Wait for them to answer."
    return ""


def _due_follow_ups(leads: list[dict], messages: dict[str, list[dict]], cfg, now: datetime) -> list[dict]:
    """Leads whose next LinkedIn message is due, and which one it is."""
    out = []
    for lead in leads:
        if lead["stage"] not in ("accepted", "messaged") or lead.get("no_message_reason"):
            continue
        rows = messages.get(lead["id"], [])
        if any(m["status"] in ("draft", "approved") and m["direction"] == "out" for m in rows):
            continue
        sent = [m for m in rows if m["direction"] == "out" and m["status"] == "sent"
                and m["kind"] in ("message", "reply")]
        due_raw = lead.get("next_action_at")
        if lead["stage"] == "accepted" and not sent:
            which = "first message"
            due_raw = due_raw or lead.get("accepted_at")
        elif lead["stage"] == "messaged" and len(sent) == 1:
            which = "follow-up"
            if not due_raw and sent[-1].get("sent_at"):
                due_raw = (datetime.fromisoformat(sent[-1]["sent_at"])
                           + timedelta(days=cfg.follow_up_min_days)).isoformat(timespec="seconds")
        else:
            continue
        if due_raw and str(due_raw) > now.isoformat():
            continue
        out.append({"lead": _lead_view(lead), "which": which, "due_at": due_raw})
    return out[:50]


@router.get("/api/linkedin")
async def linkedin_overview(tz: int = 0):
    try:
        config = _config()
    except Exception as e:
        return JSONResponse({"error": f"Couldn't read the config: {e}"}, status_code=500)
    from harvey.agents.linkedin_agent import AUTOPILOT_KEY, autopilot_on
    from harvey.config import live_do_not_contact
    from harvey.linkedin.pacing import Pacing
    tz = max(-840, min(840, int(tz)))
    try:
        state = _state()
        await state.init_db()
        cfg = config.channels.linkedin
        pacing = Pacing(state, cfg)
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        leads = await state.li_leads()
        by_id = {lead["id"]: lead for lead in leads}
        messages: dict[str, list[dict]] = {}
        for m in await state.li_messages(limit=100000):
            messages.setdefault(m["lead_id"], []).append(m)

        queue = []
        for m in await state.li_messages(statuses=("draft", "approved", "failed"), direction="out",
                                         limit=500):
            # Failed sends older than two weeks drop off, unless one may have gone out.
            stale = (m.get("created_at") or "") < (now - timedelta(days=14)).isoformat()
            if m["status"] == "failed" and stale and not (m["error"] or "").startswith(MAYBE_SENT):
                continue
            queue.append({**{k: m[k] for k in ("id", "kind", "body", "status", "error", "created_at",
                                               "approved_by")},
                          "held": (m["error"] or "").startswith(HELD_FOR_REVIEW),
                          "maybe_sent": (m["error"] or "").startswith(MAYBE_SENT),
                          "lead": _lead_view(by_id.get(m["lead_id"]))})

        # Everyone a message has actually passed between: all of them, each with
        # the whole history. An invite on its own isn't a conversation yet.
        conversations = []
        entries = live_do_not_contact(config)
        for lead in leads:
            rows = messages.get(lead["id"], [])
            thread = _history(rows)
            spoken = [m for m in thread if m["kind"] != "invite_note"]
            if not spoken and not lead.get("last_inbound_at"):
                continue
            inbound = [m for m in rows if m["direction"] == "in"]
            draft = next((m for m in reversed(rows) if m["direction"] == "out"
                          and m["kind"] == "reply" and m["status"] in ("draft", "approved")), None)
            conversations.append({
                "lead": _lead_view(lead),
                "latest_in": ({"body": inbound[-1]["body"], "intent": inbound[-1]["intent"],
                               "at": inbound[-1]["sent_at"] or inbound[-1]["created_at"]}
                              if inbound else None),
                "draft": ({"id": draft["id"], "body": draft["body"], "status": draft["status"],
                           "error": draft["error"]} if draft else None),
                "thread": thread,
                "last_at": max([m["at"] or "" for m in thread] + [lead.get("last_inbound_at") or ""]),
                "reply_blocked": await _reply_block(state, config, lead, rows, entries),
            })
        conversations.sort(key=lambda c: c["last_at"], reverse=True)

        override = (await state.get_setting(AUTOPILOT_KEY) or "").strip().lower()
        connection = await _connection(state, config)
        return {
            "enabled": connection["enabled"],
            "connection": connection,
            "warnings": await channel_login.recent_pauses(state, "linkedin_paused"),
            "autopilot": await autopilot_on(state, config),
            "autopilot_source": "dashboard" if override in ("on", "off") else "config",
            "hours": pacing.hours_text(),
            "in_hours": pacing.in_hours(),
            "pause": await guards.active_pause(state),
            "running": connection["session_running"],
            "last_run": await _last_run(state),
            "used": await pacing.used(),
            "caps": pacing.caps(),
            "funnel": await state.li_funnel(),
            "queue": queue,
            "drafts_waiting": sum(1 for q in queue if q["status"] == "draft"),
            "conversations": conversations,
            "follow_ups_due": _due_follow_ups(leads, messages, cfg, now),
            "chart": await state.li_daily_activity(14, tz),
        }
    except Exception as e:
        logger.exception("linkedin load failed")
        return {"error": str(e)}


@router.post("/api/linkedin/messages/approve-all")
async def linkedin_approve_all():
    state = _state()
    await state.init_db()
    n = await state.li_approve(None)
    held = len([m for m in await state.li_messages(statuses=("draft",), direction="out")
                if (m["error"] or "").startswith(HELD_FOR_REVIEW)])
    return {"success": True, "approved": n, "held": held}


@router.post("/api/linkedin/messages/{message_id}/{action}")
async def linkedin_message_action(message_id: str, action: str, request: Request):
    state = _state()
    await state.init_db()
    message = await state.li_get_message(message_id)
    if message is None or message["direction"] != "out":
        return _refuse("No such LinkedIn draft.", 404)
    if action == "approve":
        ok = await state.li_approve(message_id)
    elif action == "skip":
        ok = await state.li_skip(message_id)
    elif action == "retry":
        ok = await state.li_retry(message_id)
        if not ok and (message["error"] or "").startswith(MAYBE_SENT):
            return _refuse("This one may have gone out. Check the conversation on LinkedIn; "
                           "skip it if it's there.")
    elif action == "edit":
        body = str((await _body(request)).get("body") or "").strip()
        if not body and message["kind"] != "invite_note":
            return _refuse("A message can't be empty.", 400)
        try:
            limit = _config().channels.linkedin.invite_note_max_chars
        except Exception:
            limit = 200
        if message["kind"] == "invite_note" and len(" ".join(body.split())) > limit:
            return _refuse(f"A connection note is at most {limit} characters.", 400)
        ok = await state.li_edit_draft(message_id, body)
        if ok:
            await state.log_action("linkedin_edited", "dashboard",
                                   {"message_id": message_id, "old_body": message["body"]})
    else:
        return _refuse("Unknown action.", 404)
    if not ok:
        return _refuse("That draft has changed (sent, skipped or approved already). Refresh.")
    return {"success": True}


@router.post("/api/linkedin/autopilot")
async def linkedin_autopilot(request: Request):
    """{"mode": "autopilot", "confirm": "autopilot"} turns it on; {"mode":
    "approval"} turns it off at once and sends back what it approved."""
    from harvey.agents.linkedin_agent import AUTOPILOT_KEY
    body = await _body(request)
    mode = body.get("mode")
    if mode not in ("approval", "autopilot"):
        return _refuse('expected {"mode": "approval"} or {"mode": "autopilot", "confirm": "autopilot"}', 400)
    if mode == "autopilot" and str(body.get("confirm") or "").strip().lower() != "autopilot":
        return _refuse('Type "autopilot" to confirm: Harvey will send on LinkedIn without asking you.', 400)
    state = _state()
    await state.init_db()
    await state.set_setting(AUTOPILOT_KEY, "on" if mode == "autopilot" else "off")
    requeued = await state.li_requeue_autopilot() if mode == "approval" else 0
    await state.log_action("linkedin_mode", "dashboard", {"mode": mode, "requeued": requeued})
    return {"success": True, "mode": mode, "requeued": requeued}


# Declared before /api/linkedin/{action} below, which would otherwise take these.

@router.get("/api/linkedin/login")
async def linkedin_login_status():
    """The connection card on its own: what the tab polls during a sign-in."""
    state = _state()
    await state.init_db()
    try:
        return await _connection(state, _config())
    except Exception as e:
        return _refuse(f"Couldn't read the config: {e}", 500)


@router.post("/api/linkedin/login")
async def linkedin_login():
    """Open the sign-in window on this PC. Harvey still never sees a password."""
    state = _state()
    await state.init_db()
    try:
        cfg = _config().channels.linkedin
    except Exception as e:
        return _refuse(f"Couldn't read the config: {e}", 500)
    started, why = await channel_login.start(
        state, "linkedin", minutes=cfg.login_timeout_minutes,
        session_running=await channel_login.session_going(state, "linkedin", cfg))
    return {"success": True} if started else _refuse(why)


@router.post("/api/linkedin/login/cancel")
async def linkedin_login_cancel():
    state = _state()
    await state.init_db()
    ended, why = await channel_login.cancel(state, "linkedin")
    return {"success": True} if ended else _refuse(why)


@router.post("/api/linkedin/enabled")
async def linkedin_enabled(request: Request):
    """{"mode": "on", "confirm": "linkedin"} turns it on; {"mode": "off"} turns it
    off at once. Read by the heartbeat every cycle, so neither needs a restart."""
    from harvey.agents.linkedin_agent import ENABLED_KEY
    body = await _body(request)
    mode = body.get("mode")
    if mode not in ("on", "off"):
        return _refuse('expected {"mode": "on", "confirm": "linkedin"} or {"mode": "off"}', 400)
    state = _state()
    await state.init_db()
    if mode == "on":
        try:
            connection = await _connection(state, _config())
        except Exception as e:
            return _refuse(f"Couldn't read the config: {e}", 500)
        if connection["state"] != "connected":
            why = {"waiting": "Finish signing in first, in the Chrome window on this PC.",
                   "signed_out": "LinkedIn signed this profile out. Press Reconnect and sign in first."}
            return _refuse(why.get(connection["state"], "Connect LinkedIn first: nothing has signed in yet."))
        if str(body.get("confirm") or "").strip().lower() != TYPE_TO_TURN_ON:
            return _refuse(f'Type "{TYPE_TO_TURN_ON}" to confirm. {RISK}', 400)
    await state.set_setting(ENABLED_KEY, mode)
    await state.log_action("linkedin_switch", "dashboard", {"mode": mode})
    return {"success": True, "mode": mode}


@router.post("/api/linkedin/leads/{lead_id}/reply")
async def linkedin_own_reply(lead_id: str, request: Request):
    """Your own words, in the same queue: approved by you, then the same caps,
    hours and stop rules as anything else that goes out."""
    state = _state()
    await state.init_db()
    lead = await state.li_get_lead(lead_id)
    if lead is None:
        return _refuse("No such LinkedIn lead.", 404)
    try:
        config = _config()
    except Exception as e:
        return _refuse(f"Couldn't read the config: {e}", 500)
    why = await _reply_block(state, config, lead, await state.li_messages(lead_id=lead_id))
    if why:
        return _refuse(why)
    body = str((await _body(request)).get("body") or "").strip()
    if not body:
        return _refuse("Write something first.", 400)
    if len(body) > MAX_REPLY_CHARS:
        return _refuse(f"That's {len(body)} characters; a message here is at most {MAX_REPLY_CHARS}.", 400)
    # Yours replaces what Harvey had drafted for this conversation: both must not go.
    replaced = 0
    for waiting in await state.li_pending_for(lead_id, kinds=("reply",)):
        if waiting["approved_by"] != "you":
            replaced += await state.li_skip(waiting["id"])
    message_id = await state.li_add_message(
        lead_id, direction="out", kind="reply", body=body, status="approved", approved_by="you",
        thread_url=lead.get("thread_url") or "", dedupe=False)
    await state.log_action("linkedin_own_reply", "dashboard",
                           {"linkedin_lead_id": lead_id, "message_id": message_id, "replaced": replaced})
    return {"success": True, "id": message_id, "replaced": replaced}


@router.post("/api/linkedin/{action}")
async def linkedin_pause_toggle(action: str):
    if action not in ("pause", "resume"):
        return _refuse("Unknown action.", 404)
    state = _state()
    await state.init_db()
    if action == "pause":
        await guards.set_pause(state, "Paused by you from the dashboard", kind="manual", hours=None)
    else:
        await guards.clear_pause(state)
    return {"success": True}


@router.post("/api/linkedin/leads/{lead_id}/stage")
async def linkedin_lead_stage(lead_id: str, request: Request):
    """Mark a lead warm, a meeting booked, or declined (which also stops
    their email, as a no on LinkedIn does)."""
    stage = str((await _body(request)).get("stage") or "")
    if stage not in MANUAL_STAGES:
        return _refuse(f"stage must be one of {', '.join(MANUAL_STAGES)}", 400)
    state = _state()
    await state.init_db()
    lead = await state.li_get_lead(lead_id)
    if lead is None:
        return _refuse("No such LinkedIn lead.", 404)
    if stage == "declined":
        from harvey.agents.linkedin_agent import LinkedInAgent
        try:
            config = _config()
        except Exception as e:
            return _refuse(f"Couldn't read the config: {e}", 500)
        agent = LinkedInAgent(None, state, config, driver_factory=lambda: None)
        await agent.decline(lead, "", "marked by you")
        return {"success": True, "stage": "declined"}
    new = await state.li_set_stage(lead_id, stage)
    if new != stage:
        return _refuse(f"They're {new}; a lead only moves forward.")
    return {"success": True, "stage": new}
