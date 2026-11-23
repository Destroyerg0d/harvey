"""The dashboard's Meetings tab (harvey/meetings.py): every meeting, its time
in their zone and yours, the conversation, and a prep brief."""

from __future__ import annotations

import logging

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from harvey import meetings

logger = logging.getLogger("harvey.dashboard")
router = APIRouter()


def _state():
    from harvey import dashboard
    return dashboard._state()


def _config():
    from harvey.config import load_config
    from harvey import dashboard
    return load_config(str(dashboard._resolve_config_file()))


def _refuse(message: str, code: int = 400):
    return JSONResponse({"success": False, "error": message}, status_code=code)


def _channel_ok(channel: str) -> bool:
    return channel in meetings.CHANNELS


@router.get("/api/meetings")
async def meetings_overview():
    try:
        state = _state()
        await state.init_db()
        return await meetings.collect(state, _config())
    except Exception as e:
        logger.exception("meetings load failed")
        return {"error": str(e)}


@router.post("/api/meetings/{channel}/{item_id}/brief")
async def meetings_brief(channel: str, item_id: str, request: Request):
    if not _channel_ok(channel):
        return _refuse("Unknown channel.")
    try:
        body = await request.json()
    except Exception:
        body = {}
    try:
        from harvey.brain import Brain
        state = _state()
        await state.init_db()
        config = _config()
        result = await meetings.brief(state, Brain(state, config.brain), config, channel, item_id,
                                      again=bool((body or {}).get("again")))
    except Exception as e:
        logger.exception("meeting brief failed")
        return _refuse(f"Couldn't write the brief: {e}", 500)
    if result is None:
        return _refuse("Harvey couldn't write the brief just now (the brain may be busy). Try again shortly.", 503)
    return {"success": True, "brief": result}


@router.post("/api/meetings/{channel}/{item_id}/time")
async def meetings_time(channel: str, item_id: str, request: Request):
    if not _channel_ok(channel):
        return _refuse("Unknown channel.")
    try:
        body = await request.json()
    except Exception:
        return _refuse("Send the date and time.")
    date, clock = str(body.get("date") or ""), str(body.get("time") or "")
    try:
        minutes = int(body.get("minutes") or meetings.DEFAULT_MINUTES)
    except (TypeError, ValueError):
        minutes = meetings.DEFAULT_MINUTES
    state = _state()
    await state.init_db()
    config = _config()
    if await meetings.item(state, config, channel, item_id) is None:
        return _refuse("That person isn't here any more.", 404)
    try:
        await meetings.set_time(state, config, channel, item_id, date, clock, minutes)
    except ValueError:
        return _refuse("Write the date as 2026-11-23 and the time as 10:30.")
    return {"success": True, "item": await meetings.item(state, config, channel, item_id)}


@router.post("/api/meetings/{channel}/{item_id}/booked")
async def meetings_booked(channel: str, item_id: str):
    if not _channel_ok(channel):
        return _refuse("Unknown channel.")
    state = _state()
    await state.init_db()
    if not await meetings.mark_booked(state, channel, item_id):
        return _refuse("That person isn't here any more.", 404)
    await state.log_action("meeting_marked", "dashboard", {"channel": channel, "id": item_id})
    return {"success": True}


# ── Find meetings in every conversation (harvey/meeting_finder.py) ──

_scan_process = None


def _scan_running(status: dict) -> bool:
    global _scan_process
    if _scan_process is not None and _scan_process.poll() is None:
        return True
    return False


@router.post("/api/meetings/scan")
async def meetings_scan(request: Request):
    """Start `harvey meetings scan` in the background; its progress shows on
    the tab (meeting_finder.SCAN_KEY)."""
    global _scan_process
    import json as _json
    import os
    import subprocess
    import sys
    from harvey import meeting_finder
    from harvey.paths import PROJECT_ROOT
    try:
        body = await request.json()
    except Exception:
        body = {}
    state = _state()
    await state.init_db()
    try:
        status = _json.loads(await state.get_setting(meeting_finder.SCAN_KEY) or "{}")
    except ValueError:
        status = {}
    if _scan_running(status):
        return _refuse("A scan is already running.", 409)
    args = [sys.executable, "-m", "harvey", "meetings", "scan"]
    if (body or {}).get("linkedin_inbox"):
        args.append("--linkedin-inbox")
    if (body or {}).get("force"):
        args.append("--force")
    log_file = PROJECT_ROOT / "data" / "logs" / "meetings-scan.log"
    log_file.parent.mkdir(parents=True, exist_ok=True)
    flags = 0
    if sys.platform == "win32":
        flags = subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP
    with open(log_file, "w", encoding="utf-8") as log:
        _scan_process = subprocess.Popen(args, cwd=str(PROJECT_ROOT), stdin=subprocess.DEVNULL, stdout=log,
                                         stderr=log, start_new_session=True, creationflags=flags,
                                         env={**os.environ, "PYTHONUTF8": "1"})
    await state.set_setting(meeting_finder.SCAN_KEY, _json.dumps(
        {"started": meeting_finder._utcnow().isoformat(), "running": True, "step": "starting", "report": {}}))
    return {"success": True}
