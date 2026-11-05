"""Connecting LinkedIn or WhatsApp with no terminal: the sign-in as its own process.

`harvey linkedin login` and `harvey whatsapp login` open a visible Chrome on the
channel's dedicated profile and wait for the owner, who signs in or scans there.
Harvey still never touches a password or a code. This module lets the dashboard
start that same command in the background and follow it.

Three things keep it honest, because the dashboard, the heartbeat and the sign-in
are separate processes:

- The settings row ``<channel>:login`` says how the latest try went. The sign-in
  writes it itself, so one started from a terminal shows up on the tab too.
- An OS lock next to the database says whether that process is still alive. The OS
  drops it however the process dies, so a crash can't leave a channel "waiting".
  It's how data/harvey.lock works for the heartbeat, and for the same reason: a PID
  alone can name whatever later got that number, so only a process holding the lock
  is ever signalled.
- A channel's Chrome profile can only be open once. A sign-in refuses to start
  during a run, and a run refuses to start during a sign-in (``in_progress``).

Cancelling asks first, so Chrome closes properly and the profile is flushed. A
process that doesn't answer has its whole tree ended, Chrome included.

No harvey.state import here: harvey.state imports the channels' stores.
"""

import asyncio
import json
import logging
import os
import signal
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from harvey.paths import PROJECT_ROOT
from harvey.pidfile import hold_loop_lock, loop_lock_held, pid_alive, release_loop_lock

logger = logging.getLogger("harvey.channel_login")

LABELS = {"linkedin": "LinkedIn", "whatsapp": "WhatsApp"}

STARTING, WAITING, CONNECTED = "starting", "waiting", "connected"
TIMED_OUT, CANCELLED, FAILED, ENDED, BUSY = "timed_out", "cancelled", "failed", "ended", "busy"

# A process just started has this long to take its lock before it counts as gone.
START_GRACE_SECONDS = 60
# The sign-in reads the cancel request this often...
CANCEL_POLL_SECONDS = 2.0
# ...and gets this long to close Chrome before its process tree is ended.
CANCEL_WAIT_SECONDS = 10.0
KILL_WAIT_SECONDS = 5.0
LOCK_POLL_SECONDS = 0.25
# login_interactive keeps its own deadline. This catches a browser that has hung.
WATCHDOG_EXTRA_SECONDS = 180
STOP_WAIT_SECONDS = 30.0


@dataclass
class Outcome:
    status: str
    detail: str = ""

    @property
    def connected(self) -> bool:
        return self.status == CONNECTED


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _iso(at: datetime) -> str:
    return at.isoformat(timespec="seconds")


def _parse(value) -> datetime | None:
    try:
        return datetime.fromisoformat(str(value)) if value else None
    except ValueError:
        return None


def login_key(channel: str) -> str:
    return f"{channel}:login"


def cancel_key(channel: str) -> str:
    return f"{channel}:login_cancel"


def lock_path(state, channel: str) -> Path:
    """Next to the database, so a scratch or test database has its own."""
    return Path(state.db_path).parent / f"{channel}-login.lock"


def log_path(state, channel: str) -> Path:
    return Path(state.db_path).parent / f"{channel}-login.log"


async def _read(state, channel: str) -> dict:
    try:
        record = json.loads(await state.get_setting(login_key(channel)) or "{}")
    except (TypeError, ValueError):
        return {}
    return record if isinstance(record, dict) else {}


async def _write(state, channel: str, **fields) -> dict:
    record = {**await _read(state, channel), **fields}
    await state.set_setting(login_key(channel), json.dumps(record))
    return record


async def read_attempt(state, channel: str) -> dict:
    """The latest try, with ``live`` true while its process is there. A try whose
    process has gone without saying how it ended is settled here as ``ended``."""
    record = await _read(state, channel)
    status = record.get("status")
    if status not in (STARTING, WAITING):
        return {**record, "live": False}
    if loop_lock_held(lock_path(state, channel)):
        return {**record, "live": True}
    if status == STARTING:
        started = _parse(record.get("started"))
        fresh = started is not None and _now() - started < timedelta(seconds=START_GRACE_SECONDS)
        if fresh and pid_alive(int(record.get("pid") or 0)):
            return {**record, "live": True}
    return {**await _write(state, channel, status=ENDED, finished=_iso(_now())), "live": False}


def attempt_note(attempt: dict, minutes: float) -> str:
    """How the latest try ended, when that's news: one line for the card."""
    status = attempt.get("status")
    if status == TIMED_OUT:
        return f"The last try timed out: Harvey waited {int(minutes)} minutes. Press the button to try again."
    if status == CANCELLED:
        return "You cancelled the last try."
    if status == FAILED:
        return "The last try didn't finish: " + (attempt.get("detail") or "something went wrong") + "."
    if status == ENDED:
        return "The last try stopped before it finished, so the Chrome window or its process closed."
    return ""


async def recent_pauses(state, action_type: str, limit: int = 8) -> list[dict]:
    """A channel's latest warnings and other stops, newest first, from the action
    log: a 48-hour pause ends, and what caused it shouldn't vanish with it. The
    pauses you set yourself aren't warnings."""
    rows = await state._li_query(
        "SELECT details_json, created_at FROM actions WHERE action_type = ? "
        "ORDER BY created_at DESC, rowid DESC LIMIT ?", (action_type, limit * 4))
    out = []
    for row in rows:
        try:
            pause = json.loads(row["details_json"] or "{}")
        except ValueError:
            continue
        if isinstance(pause, dict) and pause.get("kind") != "manual":
            out.append({"at": pause.get("since") or row["created_at"], "kind": pause.get("kind") or "",
                        "reason": pause.get("reason") or "", "until": pause.get("until")})
    return out[:limit]


async def in_progress(state, channel: str) -> bool:
    """A sign-in holds this channel's Chrome profile, so a run must not start."""
    return bool((await read_attempt(state, channel)).get("live"))


async def session_going(state, channel: str, cfg) -> bool:
    """A run holds this channel's Chrome profile. A stamp older than a run can
    last was left by a crash."""
    started = _parse(await state.get_setting(f"{channel}:running"))
    return bool(started and _now() - started < timedelta(minutes=cfg.max_run_minutes + 15))


# ── The sign-in's own side: `harvey <channel> login` ──

async def run_attended(state, channel: str, login, *, minutes: float, on_connected=None) -> Outcome:
    """Run ``login`` (a coroutine function returning (linked, why not)), holding
    the channel's lock and keeping the record current. One at a time: a second
    gets ``busy`` and changes nothing."""
    handle = hold_loop_lock(lock_path(state, channel))
    if handle is None:
        return Outcome(BUSY, f"A {LABELS[channel]} sign-in is already waiting. Finish it in its "
                             "Chrome window, or cancel it on the dashboard.")
    try:
        started = _now()
        await _write(state, channel, status=WAITING, pid=os.getpid(), started=_iso(started),
                     deadline=_iso(started + timedelta(minutes=minutes)), finished=None, detail="")
        watchdog = started + timedelta(minutes=minutes, seconds=WATCHDOG_EXTRA_SECONDS)
        outcome = await _watch(state, channel, login, watchdog)
        if outcome.connected and on_connected is not None:
            try:
                await on_connected()
            except Exception as e:
                logger.exception("Recording the sign-in failed")
                outcome = Outcome(FAILED, _first_line(e))
        await _write(state, channel, status=outcome.status, detail=outcome.detail,
                     finished=_iso(_now()))
        return outcome
    finally:
        release_loop_lock(handle)


async def _watch(state, channel: str, login, watchdog: datetime) -> Outcome:
    """Wait for ``login``, ending it if the dashboard asks or it hangs. Ending it
    runs its session's cleanup, which is what closes Chrome properly. The request
    names this process, so one left over from an earlier try can't end this one."""
    me = str(os.getpid())
    task = asyncio.create_task(login())
    try:
        while True:
            done, _ = await asyncio.wait({task}, timeout=CANCEL_POLL_SECONDS)
            if done:
                break
            if await state.get_setting(cancel_key(channel)) == me:
                await _end(task)
                return Outcome(CANCELLED)
            if _now() >= watchdog:
                await _end(task)
                return Outcome(TIMED_OUT)
    finally:
        if not task.done():
            task.cancel()
    try:
        linked, why = task.result()
    except Exception as e:
        logger.warning(f"{LABELS[channel]} sign-in failed: {e}")
        return Outcome(FAILED, _first_line(e))
    if linked:
        return Outcome(CONNECTED)
    return Outcome(FAILED, why) if why else Outcome(TIMED_OUT)


async def _end(task: asyncio.Task) -> None:
    task.cancel()
    await asyncio.wait({task}, timeout=STOP_WAIT_SECONDS)


def _first_line(error) -> str:
    """Playwright's errors run to a page of call log; the reason is the first line.
    Its "has been closed" is also what a second Chrome on a profile in use looks like."""
    text = str(error).strip()
    line = (text.splitlines()[0] if text else type(error).__name__)[:300]
    if "has been closed" in line.lower():
        return ("The Chrome window closed before the sign-in finished. If another Chrome window "
                "is open on this profile, close it and try again")
    return line


# ── The dashboard's side ──

_children: dict[str, subprocess.Popen] = {}
_starting: set[str] = set()


def spawn(channel: str, log_file: Path) -> subprocess.Popen:
    """`harvey <channel> login` in the background: no console of its own, while
    the Chrome it opens is an ordinary window on the owner's desktop. No window
    flags that would hide that: CREATE_NO_WINDOW only covers this process's console."""
    env = {**os.environ, "PYTHONUTF8": "1"}
    flags = 0
    if sys.platform == "win32":
        flags = subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP
    log_file.parent.mkdir(parents=True, exist_ok=True)
    with open(log_file, "w", encoding="utf-8") as log:
        return subprocess.Popen(
            [sys.executable, "-m", "harvey", channel, "login"],
            cwd=str(PROJECT_ROOT), stdin=subprocess.DEVNULL, stdout=log, stderr=log,
            start_new_session=True, creationflags=flags, env=env)


def kill_tree(pid: int) -> None:
    """End a process and everything it started. On Windows Chrome is a grandchild,
    which ending the process alone would leave open with the profile held."""
    if pid <= 0:
        return
    if sys.platform == "win32":
        subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True, timeout=20,
                       creationflags=subprocess.CREATE_NO_WINDOW)
        return
    try:
        # Only a group this process leads, or we'd take a terminal's shell with it.
        if os.getpgid(pid) == pid:
            os.killpg(pid, signal.SIGKILL)
        else:
            os.kill(pid, signal.SIGKILL)
    except OSError:
        pass


async def start(state, channel: str, *, minutes: float, session_running: bool) -> tuple[bool, str]:
    """Start the sign-in process: (started, why not)."""
    label = LABELS[channel]
    if channel in _starting:
        return False, f"A {label} sign-in is already starting."
    _starting.add(channel)
    try:
        if await in_progress(state, channel):
            return False, (f"A {label} sign-in is already waiting. Finish it in its Chrome window, "
                           "or press Cancel.")
        if session_running:
            return False, (f"A {label} session is running in that Chrome profile. Wait for it to "
                           f"finish, or turn {label} off so it stops, then connect.")
        try:
            child = spawn(channel, log_path(state, channel))
        except OSError as e:
            logger.warning(f"Couldn't start the {label} sign-in: {e}")
            return False, f"Couldn't start the {label} sign-in: {e}"
        _children[channel] = child
        started = _now()
        await _write(state, channel, status=STARTING, pid=child.pid, started=_iso(started),
                     deadline=_iso(started + timedelta(minutes=minutes)), finished=None, detail="")
        return True, ""
    finally:
        _starting.discard(channel)


async def _lock_released(state, channel: str, seconds: float) -> bool:
    path = lock_path(state, channel)
    end = asyncio.get_running_loop().time() + seconds
    while loop_lock_held(path):
        if asyncio.get_running_loop().time() >= end:
            return False
        await asyncio.sleep(LOCK_POLL_SECONDS)
    return True


async def cancel(state, channel: str) -> tuple[bool, str]:
    """End a waiting sign-in: (ended, why not)."""
    label = LABELS[channel]
    attempt = await read_attempt(state, channel)
    if not attempt.get("live"):
        return False, f"No {label} sign-in is waiting."
    path = lock_path(state, channel)
    pid = int(attempt.get("pid") or 0)
    if loop_lock_held(path) and pid:
        await state.set_setting(cancel_key(channel), str(pid))
        if await _lock_released(state, channel, CANCEL_WAIT_SECONDS):
            await _settle_cancelled(state, channel)
            return True, ""
    # It didn't answer, or hasn't got as far as listening. Ours to end: the lock's
    # holder, a process that has only just started, or the child this process made.
    targets = set()
    if pid and (loop_lock_held(path) or attempt.get("status") == STARTING):
        targets.add(pid)
    child = _children.get(channel)
    if child is not None and child.poll() is None:
        targets.add(child.pid)
    for target in targets:
        try:
            await asyncio.to_thread(kill_tree, target)
        except (OSError, subprocess.SubprocessError) as e:
            logger.warning(f"Couldn't end the {label} sign-in: {e}")
    if not await _lock_released(state, channel, KILL_WAIT_SECONDS):
        return False, f"Couldn't end the {label} sign-in. Close its Chrome window yourself."
    await _settle_cancelled(state, channel)
    return True, ""


async def _settle_cancelled(state, channel: str) -> None:
    """The process wrote this itself if it got the chance; if not, it's written here."""
    record = await _read(state, channel)
    if record.get("status") in (STARTING, WAITING, ENDED, CANCELLED):
        await _write(state, channel, status=CANCELLED, detail="", finished=_iso(_now()))
