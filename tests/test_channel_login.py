"""Connecting a channel from the dashboard: the sign-in as a process of its own.

No browser and no real sign-in process: the process start and the login are
fakes. The one real process here is a throwaway Python, for ending a process tree.
"""

import argparse
import asyncio
import json
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone

import pytest
import yaml
from fastapi.testclient import TestClient

import harvey.dashboard as dash
from harvey import channel_login as cl
from harvey.agents.linkedin_agent import LinkedInAgent
from harvey.linkedin import cli as li_cli
from harvey.linkedin import guards as li_guards
from harvey.linkedin import session as li_session
from harvey.pidfile import hold_loop_lock, loop_lock_held, release_loop_lock
from harvey.state import StateManager
from harvey.whatsapp import cli as wa_cli
from harvey.whatsapp import guards as wa_guards
from harvey.whatsapp import session as wa_session
from harvey.whatsapp.pacing import LINKED_AT_KEY, LINKED_NUMBER_KEY
from tests.test_dashboard_signals import client  # noqa: F401  (fixture)
from tests.whatsapp_fakes import MINIMAL

CHANNELS = ("linkedin", "whatsapp")
GUARDS = {"linkedin": li_guards, "whatsapp": wa_guards}
CLIS = {"linkedin": li_cli, "whatsapp": wa_cli}
SESSIONS = {"linkedin": li_session, "whatsapp": wa_session}
CONNECTED_KEY = {"linkedin": li_cli.SIGNED_IN_KEY, "whatsapp": LINKED_AT_KEY}


def run(coro):
    return asyncio.run(coro)


class FakeChild:
    """What subprocess.Popen returns: nothing is ever started."""

    def __init__(self, pid=4321, alive=True):
        self.pid, self.alive = pid, alive

    def poll(self):
        return None if self.alive else 1


@pytest.fixture(autouse=True)
def fakes(monkeypatch):
    """Nothing here starts a real process or signals one, and no wait is real."""
    seen = {"spawned": [], "killed": [], "alive": set()}

    def fake_spawn(channel, log_file):
        seen["spawned"].append(channel)
        seen["alive"].add(4321)
        return FakeChild()

    monkeypatch.setattr(cl, "spawn", fake_spawn)
    monkeypatch.setattr(cl, "kill_tree", lambda pid: seen["killed"].append(pid))
    monkeypatch.setattr(cl, "pid_alive", lambda pid: pid in seen["alive"])
    monkeypatch.setattr(cl, "CANCEL_WAIT_SECONDS", 0.05)
    monkeypatch.setattr(cl, "KILL_WAIT_SECONDS", 0.05)
    monkeypatch.setattr(cl, "LOCK_POLL_SECONDS", 0.01)
    monkeypatch.setattr(cl, "CANCEL_POLL_SECONDS", 0.01)
    cl._children.clear()
    cl._starting.clear()
    return seen


@pytest.fixture
def web(client, tmp_path, monkeypatch):  # noqa: F811
    path = tmp_path / "harvey.yaml"
    path.write_text(yaml.safe_dump({**MINIMAL, "channels": {
        "linkedin": {"login_timeout_minutes": 30},
        "whatsapp": {"number": "+15555550100", "login_timeout_minutes": 30}}}))
    monkeypatch.setattr(dash, "_resolve_config_file", lambda: path)
    state = StateManager(str(client.db_path))
    run(state.init_db())
    client.state = state
    return client


@pytest.fixture
def state(tmp_path):
    s = StateManager(str(tmp_path / "h.db"))
    run(s.init_db())
    return s


def config():
    from harvey.config import HarveyConfig
    return HarveyConfig(**{**MINIMAL, "channels": {
        "linkedin": {"enabled": True, "login_timeout_minutes": 30},
        "whatsapp": {"enabled": True, "number": "+15555550100", "login_timeout_minutes": 30}}})


def record(state, channel):
    return json.loads(run(state.get_setting(cl.login_key(channel))) or "{}")


def card(web, channel):
    res = web.get(f"/api/{channel}/login")
    assert res.status_code == 200
    return res.json()


@pytest.fixture
def child_holds_the_lock(web):
    """A live sign-in, as the process itself leaves things: lock held, record written."""
    handles = []

    def hold(channel, pid=7777):
        handles.append(hold_loop_lock(cl.lock_path(web.state, channel)))
        run(web.state.set_setting(cl.login_key(channel), json.dumps({
            "status": cl.WAITING, "pid": pid, "started": "2026-11-05T10:00:00",
            "deadline": "2026-11-05T10:30:00"})))
    yield hold
    for handle in handles:
        release_loop_lock(handle)


# ── Starting one ──

@pytest.mark.parametrize("channel", CHANNELS)
def test_connect_starts_one_background_process_and_the_card_says_to_go_and_look(web, fakes, channel):
    assert card(web, channel)["state"] == "not_connected"
    res = web.post(f"/api/{channel}/login")
    assert res.status_code == 200 and res.json()["success"]
    assert fakes["spawned"] == [channel]
    got = record(web.state, channel)
    assert got["status"] == cl.STARTING and got["pid"] == 4321 and got["deadline"]
    now = card(web, channel)
    assert now["state"] == "waiting" and now["can_connect"] is False
    assert "Chrome window has opened on this PC" in now["step"] and now["deadline"]


def test_each_channel_says_what_to_do_in_the_window(web, child_holds_the_lock):
    child_holds_the_lock("linkedin")
    child_holds_the_lock("whatsapp")
    linkedin = card(web, "linkedin")["step"]
    assert "Sign in to LinkedIn there yourself" in linkedin and "never" in linkedin
    whatsapp = card(web, "whatsapp")["step"]
    assert "Linked devices" in whatsapp and "Link a device" in whatsapp and "scan" in whatsapp
    assert "never sees a code" in whatsapp


@pytest.mark.parametrize("channel", CHANNELS)
def test_a_second_connect_is_refused_while_one_waits(web, fakes, channel):
    assert web.post(f"/api/{channel}/login").status_code == 200
    res = web.post(f"/api/{channel}/login")
    assert res.status_code == 409 and "already waiting" in res.json()["message"]
    assert fakes["spawned"] == [channel]


def test_one_channel_waiting_doesnt_hold_the_other(web, fakes):
    assert web.post("/api/linkedin/login").status_code == 200
    assert web.post("/api/whatsapp/login").status_code == 200
    assert fakes["spawned"] == ["linkedin", "whatsapp"]


@pytest.mark.parametrize("channel", CHANNELS)
def test_a_sign_in_started_from_a_terminal_counts_too(web, fakes, child_holds_the_lock, channel):
    child_holds_the_lock(channel)
    assert web.post(f"/api/{channel}/login").status_code == 409
    assert fakes["spawned"] == []


@pytest.mark.parametrize("channel", CHANNELS)
def test_connect_is_refused_during_a_session_in_that_profile(web, fakes, channel):
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    run(web.state.set_setting(f"{channel}:running", now.isoformat(timespec="seconds")))
    res = web.post(f"/api/{channel}/login")
    assert res.status_code == 409 and "session is running" in res.json()["message"]
    assert fakes["spawned"] == []
    assert card(web, channel)["can_connect"] is False and card(web, channel)["session_running"] is True


@pytest.mark.parametrize("channel", CHANNELS)
def test_a_lock_left_by_a_crashed_session_doesnt_block_connect(web, fakes, channel):
    old = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=1)
    run(web.state.set_setting(f"{channel}:running", old.isoformat(timespec="seconds")))
    assert web.post(f"/api/{channel}/login").status_code == 200


def test_a_process_that_wont_start_says_why(web, monkeypatch):
    def broken(channel, log_file):
        raise OSError("no such file")
    monkeypatch.setattr(cl, "spawn", broken)
    res = web.post("/api/linkedin/login")
    assert res.status_code == 409 and "no such file" in res.json()["message"]
    assert card(web, "linkedin")["state"] == "not_connected"


def test_the_command_is_the_existing_login_with_no_console_and_no_hidden_window(tmp_path, monkeypatch):
    """Only the background process goes without a console. The flags that would
    hide Chrome (STARTUPINFO's show-window) must not be there."""
    seen = {}

    class Popen:
        def __init__(self, args, **kw):
            seen.update(kw, args=args)
    monkeypatch.undo()          # the autouse fixture's spawn is what this one tests
    monkeypatch.setattr(subprocess, "Popen", Popen)
    cl.spawn("whatsapp", tmp_path / "logs" / "whatsapp-login.log")
    assert seen["args"] == [sys.executable, "-m", "harvey", "whatsapp", "login"]
    assert seen["stdin"] == subprocess.DEVNULL and "startupinfo" not in seen
    assert (tmp_path / "logs" / "whatsapp-login.log").exists()
    if sys.platform == "win32":
        assert seen["creationflags"] & subprocess.CREATE_NO_WINDOW
        assert not seen["creationflags"] & subprocess.DETACHED_PROCESS


# ── The sign-in process's own side, through the real CLI code ──

def _parse(channel):
    parser = argparse.ArgumentParser()
    CLIS[channel].add_parser(parser.add_subparsers(dest="command"))
    return parser.parse_args([channel, "login"])


def _fake_login(monkeypatch, channel, behave):
    monkeypatch.setattr(SESSIONS[channel], "login_interactive", behave)


@pytest.mark.parametrize("channel", CHANNELS)
def test_signing_in_moves_the_card_to_connected_and_clears_signed_out(state, monkeypatch, capsys, channel):
    async def fine(cfg, **hooks):
        return True
    _fake_login(monkeypatch, channel, fine)
    run(GUARDS[channel].set_pause(state, "signed out", kind="signed_out", hours=None))
    run(CLIS[channel]._login(_parse(channel), config(), state))
    got = record(state, channel)
    assert got["status"] == cl.CONNECTED and got["finished"] and got["pid"]
    assert run(GUARDS[channel].active_pause(state)) is None
    assert run(state.get_setting(CONNECTED_KEY[channel]))
    assert not loop_lock_held(cl.lock_path(state, channel))         # let go when done


@pytest.mark.parametrize("channel", CHANNELS)
def test_a_warning_pause_survives_signing_in(state, monkeypatch, channel):
    """Connecting clears signed out and nothing else: a 48-hour warning stands."""
    async def fine(cfg, **hooks):
        return True
    _fake_login(monkeypatch, channel, fine)
    run(GUARDS[channel].set_pause(state, "unusual activity", kind="unusual_activity", hours=48))
    run(CLIS[channel]._login(_parse(channel), config(), state))
    assert run(GUARDS[channel].active_pause(state))["kind"] == "unusual_activity"


@pytest.mark.parametrize("channel", CHANNELS)
def test_the_wait_timing_out_is_recorded_and_the_card_says_so(web, monkeypatch, channel):
    async def timed_out(cfg, **hooks):
        return False
    _fake_login(monkeypatch, channel, timed_out)
    run(CLIS[channel]._login(_parse(channel), config(), web.state))
    assert record(web.state, channel)["status"] == cl.TIMED_OUT
    now = card(web, channel)
    assert now["state"] == "not_connected" and "timed out" in now["note"] and "30 minutes" in now["note"]
    assert now["can_connect"] is True
    assert not run(web.state.get_setting(CONNECTED_KEY[channel]))


@pytest.mark.parametrize("channel", CHANNELS)
def test_closing_the_window_or_a_ban_says_why_not_timed_out(web, monkeypatch, channel):
    async def stopped(cfg, stopped=None, **hooks):
        stopped("The Chrome window was closed")
        return False
    _fake_login(monkeypatch, channel, stopped)
    run(CLIS[channel]._login(_parse(channel), config(), web.state))
    assert record(web.state, channel)["status"] == cl.FAILED
    assert "The Chrome window was closed" in card(web, channel)["note"]


@pytest.mark.parametrize("channel", CHANNELS)
def test_chrome_missing_says_so_on_the_card_and_on_the_terminal(web, monkeypatch, capsys, channel):
    async def no_chrome(cfg, **hooks):
        raise SESSIONS[channel].ChromeNotFound("Google Chrome wasn't found")
    _fake_login(monkeypatch, channel, no_chrome)
    run(CLIS[channel]._login(_parse(channel), config(), web.state))
    assert "Google Chrome wasn't found" in card(web, channel)["note"]
    assert "Google Chrome wasn't found" in capsys.readouterr().out


def test_a_page_of_browser_error_comes_back_as_one_plain_line():
    long = "BrowserType.launch: Target page, context or browser has been closed\nCall log:\n" + "x\n" * 200
    assert "\n" not in cl._first_line(RuntimeError(long)) and "close it and try again" in cl._first_line(RuntimeError(long))
    assert cl._first_line(RuntimeError("boom\nmore")) == "boom"
    assert cl._first_line(RuntimeError("")) == "RuntimeError"


@pytest.mark.parametrize("channel", CHANNELS)
def test_a_second_login_process_changes_nothing(web, monkeypatch, capsys, child_holds_the_lock, channel):
    called = []

    async def login(cfg, **hooks):
        called.append(1)
        return True
    _fake_login(monkeypatch, channel, login)
    child_holds_the_lock(channel)
    before = record(web.state, channel)
    run(CLIS[channel]._login(_parse(channel), config(), web.state))
    assert called == [] and record(web.state, channel) == before
    assert "already waiting" in capsys.readouterr().out


@pytest.mark.parametrize("channel", CHANNELS)
def test_the_terminal_command_refuses_during_a_session(state, monkeypatch, capsys, channel):
    called = []

    async def login(cfg, **hooks):
        called.append(1)
        return True
    _fake_login(monkeypatch, channel, login)
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    run(state.set_setting(f"{channel}:running", now.isoformat(timespec="seconds")))
    run(CLIS[channel]._login(_parse(channel), config(), state))
    assert called == [] and "session is running" in capsys.readouterr().out


def test_a_browser_that_hangs_past_the_deadline_is_ended(state, monkeypatch):
    closed = []

    async def hang():
        try:
            await asyncio.sleep(60)
        finally:
            closed.append(True)
        return True, ""
    monkeypatch.setattr(cl, "WATCHDOG_EXTRA_SECONDS", 0)
    outcome = run(cl.run_attended(state, "linkedin", hang, minutes=0))
    assert outcome.status == cl.TIMED_OUT and closed == [True]
    assert record(state, "linkedin")["status"] == cl.TIMED_OUT


# ── Going away by itself ──

@pytest.mark.parametrize("channel", CHANNELS)
def test_a_process_that_dies_without_a_word_is_noticed(web, child_holds_the_lock, channel):
    run(web.state.set_setting(cl.login_key(channel), json.dumps({
        "status": cl.WAITING, "pid": 7777, "started": "2026-11-05T10:00:00"})))
    # Nobody holds the lock: the OS dropped it when the process went.
    now = card(web, channel)
    assert now["state"] == "not_connected" and now["can_connect"] and "stopped before it finished" in now["note"]
    assert record(web.state, channel)["status"] == cl.ENDED


def test_a_process_that_never_got_going_is_noticed(web, fakes):
    assert web.post("/api/whatsapp/login").status_code == 200
    fakes["alive"].clear()          # gone before it took the lock
    assert card(web, "whatsapp")["state"] == "not_connected"
    assert record(web.state, "whatsapp")["status"] == cl.ENDED


def test_a_start_that_stalls_past_the_grace_is_noticed_even_if_the_pid_lives(web, fakes):
    """A PID can name whatever got that number after a reboot: it isn't proof."""
    old = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(minutes=5)
    fakes["alive"].add(4321)
    run(web.state.set_setting(cl.login_key("linkedin"), json.dumps({
        "status": cl.STARTING, "pid": 4321, "started": old.isoformat(timespec="seconds")})))
    assert card(web, "linkedin")["state"] == "not_connected"


def test_a_bad_row_reads_as_no_attempt(web):
    run(web.state.set_setting(cl.login_key("linkedin"), "not json"))
    assert card(web, "linkedin")["state"] == "not_connected"


# ── Cancel ──

@pytest.mark.parametrize("channel", CHANNELS)
def test_cancel_with_nothing_waiting_is_refused(web, fakes, channel):
    res = web.post(f"/api/{channel}/login/cancel")
    assert res.status_code == 409 and "waiting" in res.json()["message"]
    assert fakes["killed"] == []


@pytest.mark.parametrize("channel", CHANNELS)
def test_cancel_just_after_starting_ends_the_process_tree(web, fakes, channel):
    assert web.post(f"/api/{channel}/login").status_code == 200
    assert web.post(f"/api/{channel}/login/cancel").status_code == 200
    assert fakes["killed"] == [4321]
    assert record(web.state, channel)["status"] == cl.CANCELLED
    now = card(web, channel)
    assert now["state"] == "not_connected" and "cancelled" in now["note"] and now["can_connect"]


def test_cancel_asks_the_process_first_and_it_closes_chrome_itself(state, monkeypatch):
    """The real wait loop and a real request: the login is ended from inside, which
    is what runs its cleanup, and nothing is killed."""
    closed = []

    async def go():
        started = asyncio.Event()

        async def login():
            started.set()
            try:
                await asyncio.sleep(60)
            finally:
                closed.append(True)
            return True, ""

        child = asyncio.create_task(cl.run_attended(state, "linkedin", login, minutes=30))
        await started.wait()
        await asyncio.sleep(0.05)
        assert await cl.in_progress(state, "linkedin")
        monkeypatch.setattr(cl, "CANCEL_WAIT_SECONDS", 5.0)
        ended, why = await cl.cancel(state, "linkedin")
        assert ended, why
        return await child

    killed = []
    monkeypatch.setattr(cl, "kill_tree", lambda pid: killed.append(pid))
    outcome = run(go())
    assert outcome.status == cl.CANCELLED and closed == [True] and killed == []
    assert record(state, "linkedin")["status"] == cl.CANCELLED


def test_a_request_left_from_an_earlier_process_is_ignored(state):
    """The request names the process it's for, so a leftover can't end a new try."""
    run(state.set_setting(cl.cancel_key("linkedin"), "1"))

    async def login():
        await asyncio.sleep(0.05)
        return True, ""
    assert run(cl.run_attended(state, "linkedin", login, minutes=30)).connected


@pytest.mark.parametrize("channel", CHANNELS)
def test_a_process_that_wont_answer_is_ended_by_its_lock_holders_pid(web, fakes, monkeypatch, channel):
    """The lock is what says this PID is ours to signal."""
    handle = hold_loop_lock(cl.lock_path(web.state, channel))
    run(web.state.set_setting(cl.login_key(channel), json.dumps({
        "status": cl.WAITING, "pid": 7777, "started": "2026-11-05T10:00:00"})))

    def kill(pid):
        fakes["killed"].append(pid)
        release_loop_lock(handle)
    monkeypatch.setattr(cl, "kill_tree", kill)
    assert web.post(f"/api/{channel}/login/cancel").status_code == 200
    assert fakes["killed"] == [7777]
    assert run(web.state.get_setting(cl.cancel_key(channel))) == "7777"
    assert record(web.state, channel)["status"] == cl.CANCELLED


def test_a_pid_nobody_holds_the_lock_for_is_never_signalled(web, fakes):
    """A stale record names a PID that may be anyone's by now."""
    old = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(minutes=5)
    fakes["alive"].add(4321)
    run(web.state.set_setting(cl.login_key("linkedin"), json.dumps({
        "status": cl.WAITING, "pid": 4321, "started": old.isoformat(timespec="seconds")})))
    assert web.post("/api/linkedin/login/cancel").status_code == 409
    assert fakes["killed"] == []


def test_cancel_says_so_when_the_process_wont_end(web, child_holds_the_lock):
    child_holds_the_lock("whatsapp")
    res = web.post("/api/whatsapp/login/cancel")
    assert res.status_code == 409 and "Close its Chrome window" in res.json()["message"]


def test_ending_a_process_tree_takes_its_children(tmp_path, monkeypatch):
    """Real processes, no browser: a Python that starts a Python, as the sign-in
    starts Chrome. Ending the parent alone would leave the child with the profile."""
    monkeypatch.undo()
    marker = tmp_path / "child.pid"
    child = ("import os,time,pathlib;"
             f"pathlib.Path({str(marker)!r}).write_text(str(os.getpid()));time.sleep(60)")
    parent = f"import subprocess,sys,time;subprocess.Popen([sys.executable,'-c',{child!r}]);time.sleep(60)"
    process = subprocess.Popen([sys.executable, "-c", parent], start_new_session=True)
    try:
        for _ in range(100):
            if marker.exists() and marker.read_text():
                break
            time.sleep(0.1)
        grandchild = int(marker.read_text())
        from harvey.pidfile import pid_alive
        assert pid_alive(grandchild)
        cl.kill_tree(process.pid)
        process.wait(timeout=20)
        for _ in range(100):
            if not pid_alive(grandchild):
                break
            time.sleep(0.1)
        assert not pid_alive(grandchild)
    finally:
        if process.poll() is None:
            process.kill()


def test_a_bad_pid_is_left_alone(monkeypatch):
    monkeypatch.undo()
    assert cl.kill_tree(0) is None and cl.kill_tree(-5) is None


# ── A sign-in and a run share one Chrome profile ──

def test_a_run_wont_start_while_a_sign_in_waits(tmp_path):
    from tests.whatsapp_fakes import World as WaWorld
    w = WaWorld(tmp_path)
    assert run(w.agent().why_not_now(ignore_interval=True)) == ""
    handle = hold_loop_lock(cl.lock_path(w.state, "whatsapp"))
    try:
        run(w.state.set_setting(cl.login_key("whatsapp"), json.dumps({"status": cl.WAITING, "pid": 1})))
        why = run(w.agent().why_not_now(ignore_interval=True))
        assert "sign-in is waiting" in why
    finally:
        release_loop_lock(handle)
    assert run(w.agent().why_not_now(ignore_interval=True)) == ""


def test_a_linkedin_run_wont_start_while_a_sign_in_waits(tmp_path):
    from tests.linkedin_fakes import IN_HOURS, make_config
    state = StateManager(str(tmp_path / "h.db"))
    run(state.init_db())
    agent = LinkedInAgent(None, state, make_config(), driver_factory=lambda: None, now=lambda: IN_HOURS)
    assert run(agent.why_not_now()) == ""
    handle = hold_loop_lock(cl.lock_path(state, "linkedin"))
    try:
        run(state.set_setting(cl.login_key("linkedin"), json.dumps({"status": cl.WAITING, "pid": 1})))
        assert "sign-in is waiting" in run(agent.why_not_now())
    finally:
        release_loop_lock(handle)
    assert run(agent.why_not_now()) == ""


# ── What the session code gained ──

class _Page:
    def __init__(self, signed_in_after=99, closed=False, storage=None):
        self.url = "https://www.linkedin.com/login"
        self.left = signed_in_after
        self.closed = closed
        self.page = _Storage(storage or {})

    async def goto(self, url):
        self.url = url

    async def html(self):
        self.left -= 1
        if self.left < 0:
            self.url = "https://www.linkedin.com/feed/"
            return '<header id="global-nav"></header>'
        return '<form class="login__form"><input id="username"></form>'

    async def wait(self, seconds):
        pass


class _Storage:
    def __init__(self, items):
        self.items = items

    async def evaluate(self, script, key):
        if self.items == "broken":
            raise RuntimeError("the page went away")
        return self.items.get(key)


def _session(page):
    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def opener(cfg):
        yield page
    return opener


def test_linkedin_notices_its_window_closing_instead_of_waiting_half_an_hour(capsys):
    why = []
    page = _Page(closed=True)
    ok = run(li_session.login_interactive(config().channels.linkedin, timeout_minutes=0.1, poll_seconds=0,
                                          session=_session(page), stopped=why.append))
    assert ok is False and why == ["The Chrome window was closed"]
    assert "closed" in capsys.readouterr().out


def test_linkedin_still_waits_for_the_sign_in():
    page = _Page(signed_in_after=3)
    assert run(li_session.login_interactive(config().channels.linkedin, timeout_minutes=0.1, poll_seconds=0,
                                            session=_session(page))) is True


@pytest.mark.parametrize("stored,expected", [
    ({"last-wid-md": '"15555550100:12@c.us"'}, "+15555550100"),
    ({"last-wid": "15555550100@c.us"}, "+15555550100"),
    ({}, ""),
    ({"last-wid-md": "nonsense"}, ""),
    ({"last-wid-md": '"123@c.us"'}, ""),
    ("broken", ""),
])
def test_the_linked_number_is_only_read_when_it_is_plainly_a_number(stored, expected):
    assert run(wa_session.own_number(_Page(storage=stored))) == expected


def test_a_driver_with_no_page_has_no_number():
    assert run(wa_session.own_number(object())) == ""


def test_linking_records_the_number_whatsapp_says_it_is(state, monkeypatch):
    async def fine(cfg, number_seen=None, **hooks):
        number_seen("+15555550111")
        return True
    _fake_login(monkeypatch, "whatsapp", fine)
    run(wa_cli._login(_parse("whatsapp"), config(), state))
    assert run(state.get_setting(LINKED_NUMBER_KEY)) == "+15555550111"


def test_a_number_written_another_way_doesnt_restart_the_ramp(state):
    from harvey.whatsapp.pacing import mark_linked
    cfg = config().channels.whatsapp
    first = run(mark_linked(state, cfg, at=datetime(2026, 11, 1), number="+1 555 555 0100"))
    again = run(mark_linked(state, cfg, at=datetime(2026, 11, 3), number="+15555550100"))
    assert first == again == "2026-11-01T00:00:00"
    other = run(mark_linked(state, cfg, at=datetime(2026, 11, 4), number="+15555550111"))
    assert other == "2026-11-04T00:00:00"


# ── The connection card ──

def test_the_card_shows_connected_since_and_the_number(web):
    run(web.state.set_setting(LINKED_AT_KEY, "2026-11-01T00:00:00"))
    now = card(web, "whatsapp")
    assert now["state"] == "connected" and now["since"] == "2026-11-01T00:00:00"
    assert now["number"] == "+15555550100"
    run(web.state.set_setting(LINKED_NUMBER_KEY, "+15555550111"))
    assert card(web, "whatsapp")["number"] == "+15555550111"       # what was linked wins


def test_reconnecting_moves_since_while_the_ramp_keeps_its_first_date(web):
    run(web.state.set_setting(LINKED_AT_KEY, "2026-11-01T00:00:00"))
    run(web.state.set_setting(cl.login_key("whatsapp"), json.dumps({
        "status": cl.CONNECTED, "finished": "2026-11-05T09:00:00"})))
    assert card(web, "whatsapp")["since"] == "2026-11-05T09:00:00"
    assert run(web.state.get_setting(LINKED_AT_KEY)) == "2026-11-01T00:00:00"


@pytest.mark.parametrize("channel", CHANNELS)
def test_signed_out_says_reconnect(web, channel):
    run(web.state.set_setting(CONNECTED_KEY[channel], "2026-11-01T00:00:00"))
    run(GUARDS[channel].set_pause(web.state, "gone", kind="signed_out", hours=None))
    now = card(web, channel)
    assert now["state"] == "signed_out" and "Reconnect" in now["step"] and now["can_connect"]


def test_linkedin_signed_in_before_the_date_was_kept_counts_as_connected(web):
    """A run that finished got past its own sign-in check."""
    run(web.state.set_setting("linkedin:last_run", json.dumps(
        {"started": "2026-11-01T09:00:00", "outcome": "done"})))
    assert card(web, "linkedin")["state"] == "connected"
    run(web.state.set_setting("linkedin:last_run", json.dumps(
        {"started": "2026-11-01T09:00:00", "outcome": "error: boom"})))
    assert card(web, "linkedin")["state"] == "not_connected"


def test_the_terminal_wording_is_gone_from_the_tabs():
    from pathlib import Path
    js = (Path(dash.__file__).parent / "web" / "app.js").read_text(encoding="utf-8")
    assert "harvey linkedin login" not in js and "harvey whatsapp login" not in js
    assert "channels.linkedin.enabled" not in js and "channels.whatsapp.enabled" not in js


# ── Every route is behind the token ──

@pytest.mark.parametrize("method,path", [
    ("get", "/api/linkedin/login"), ("post", "/api/linkedin/login"), ("post", "/api/linkedin/login/cancel"),
    ("post", "/api/linkedin/enabled"), ("post", "/api/linkedin/leads/x/reply"),
    ("get", "/api/whatsapp/login"), ("post", "/api/whatsapp/login"), ("post", "/api/whatsapp/login/cancel"),
    ("post", "/api/whatsapp/enabled"), ("post", "/api/whatsapp/contacts"),
    ("post", "/api/whatsapp/contacts/x/reply"),
])
def test_every_new_route_needs_the_token(web, fakes, method, path):
    bare = TestClient(dash.app, base_url="http://127.0.0.1:5555")
    kw = {"json": {"mode": "off", "phone": "+14155550100", "source": "website chat", "body": "x"}} \
        if method == "post" else {}
    assert getattr(bare, method)(path, **kw).status_code == 401
    assert fakes["spawned"] == [] and fakes["killed"] == []
    assert run(web.state.get_setting("linkedin:enabled")) == ""
    assert run(web.state.get_setting("whatsapp:enabled")) == ""
    assert run(web.state.wa_contacts()) == []


@pytest.mark.parametrize("path", [
    "/api/linkedin/login", "/api/linkedin/login/cancel", "/api/linkedin/enabled",
    "/api/whatsapp/login", "/api/whatsapp/login/cancel", "/api/whatsapp/enabled", "/api/whatsapp/contacts",
])
def test_another_site_cant_press_them(web, fakes, path):
    """The token isn't enough from elsewhere: a request from another origin is refused."""
    res = web.post(path, json={"mode": "off"}, headers={"Origin": "https://evil.example"})
    assert res.status_code == 403 and fakes["spawned"] == []
