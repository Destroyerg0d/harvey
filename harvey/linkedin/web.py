"""The dashboard's LinkedIn tab: routes under /api/linkedin.

Included by harvey/dashboard.py, so the dashboard's token guard covers
every route here like any other.
"""

import logging
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from harvey.linkedin import guards
from harvey.linkedin.store import HELD_FOR_REVIEW, MAYBE_SENT

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


def _lead_view(lead: dict | None) -> dict:
    if not lead:
        return {}
    keep = ("id", "name", "first_name", "headline", "company", "profile_url", "stage",
            "invited_at", "accepted_at", "last_outbound_at", "last_inbound_at",
            "next_action_at", "no_message_reason", "prospect_id", "note")
    return {k: lead.get(k) for k in keep}


async def today_items(state) -> list[dict]:
    """Today cards: LinkedIn drafts waiting, and a LinkedIn pause."""
    items = []
    try:
        pause = await guards.active_pause(state)
        if pause:
            items.append({
                "key": "linkedin-paused", "tone": "bad", "title": "LinkedIn is paused",
                "detail": str(pause.get("reason") or "Paused.") + (
                    f" Until {str(pause['until'])[:16].replace('T', ' ')} UTC, unless you resume it."
                    if pause.get("until") else " Until you resume it."),
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


def _running(stamp: str, cfg, now: datetime) -> bool:
    """A run is going (a lock older than a run can last was left by a crash)."""
    try:
        started = datetime.fromisoformat(stamp) if stamp else None
    except ValueError:
        return False
    return bool(started and now - started < timedelta(minutes=cfg.max_run_minutes + 15))


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
    from harvey.agents.linkedin_agent import AUTOPILOT_KEY, LAST_RUN_KEY, RUNNING_KEY, autopilot_on
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

        conversations = []
        talking = [lead for lead in leads if lead.get("last_inbound_at")]
        talking.sort(key=lambda lead: lead.get("last_inbound_at") or "", reverse=True)
        for lead in talking[:40]:
            rows = messages.get(lead["id"], [])
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
                "thread": [{"direction": m["direction"], "kind": m["kind"], "body": m["body"],
                            "at": m["sent_at"] or m["created_at"]}
                           for m in rows if m["status"] == "sent"][-6:],
            })

        import json
        try:
            last_run = json.loads(await state.get_setting(LAST_RUN_KEY) or "{}")
        except ValueError:
            last_run = {}
        override = (await state.get_setting(AUTOPILOT_KEY) or "").strip().lower()
        return {
            "enabled": cfg.enabled,
            "autopilot": await autopilot_on(state, config),
            "autopilot_source": "dashboard" if override in ("on", "off") else "config",
            "hours": pacing.hours_text(),
            "in_hours": pacing.in_hours(),
            "pause": await guards.active_pause(state),
            "running": _running(await state.get_setting(RUNNING_KEY), cfg, now),
            "last_run": last_run if isinstance(last_run, dict) else {},
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
