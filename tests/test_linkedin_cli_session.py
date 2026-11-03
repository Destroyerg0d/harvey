"""`harvey linkedin ...` and the session helpers (no browser is started)."""

import argparse
import asyncio
import json

import pytest

from harvey.linkedin import cli as li_cli
from harvey.linkedin import guards
from harvey.linkedin.session import ChromeNotFound, Driver, find_chrome, is_signed_in, profile_dir
from harvey.state import StateManager
from tests.linkedin_fakes import make_config


def run(coro):
    return asyncio.run(coro)


def _parse(*argv):
    parser = argparse.ArgumentParser()
    li_cli.add_parser(parser.add_subparsers(dest="command"))
    return parser.parse_args(["linkedin", *argv])


def test_the_subcommands_parse():
    assert _parse("login").linkedin_action == "login"
    args = _parse("run", "--once", "--dry-run")
    assert args.once and args.dry_run
    args = _parse("import", "--pipeline", "p.json", "--sends", "s.xlsx")
    assert (args.pipeline, args.sends) == ("p.json", "s.xlsx")
    with pytest.raises(SystemExit):
        _parse("explode")


def test_import_status_pause_resume(tmp_path, capsys):
    state = StateManager(str(tmp_path / "h.db"))
    config = make_config()
    pipeline = tmp_path / "p.json"
    pipeline.write_text(json.dumps([{"name": "Jane Doe", "first": "Jane", "brand": "Example Goods",
                                     "slug": "jane-doe-1", "stage": "accepted_no_reply",
                                     "connected": "Oct28", "draft": "", "sent": ""}]))
    run(li_cli._import(_parse("import", "--pipeline", str(pipeline)), config, state))
    assert "1 leads added" in capsys.readouterr().out
    run(li_cli._pause(_parse("pause", "--reason", "holiday"), config, state))
    assert run(guards.active_pause(state))["reason"] == "holiday"
    run(li_cli._status(_parse("status"), config, state))
    out = capsys.readouterr().out
    assert "PAUSED: holiday" in out and "1 accepted" in out
    run(li_cli._resume(_parse("resume"), config, state))
    assert run(guards.active_pause(state)) is None


def test_run_outside_hours_says_why(tmp_path, capsys, monkeypatch):
    state = StateManager(str(tmp_path / "h.db"))
    config = make_config(enabled=False)
    run(li_cli._run(_parse("run", "--once"), config, state))
    assert "Not run: LinkedIn is off" in capsys.readouterr().out


def test_find_chrome(tmp_path, monkeypatch):
    exe = tmp_path / "chrome.exe"
    exe.write_bytes(b"")
    assert find_chrome(str(exe)) == str(exe)
    with pytest.raises(ChromeNotFound, match="chrome_path"):
        find_chrome(str(tmp_path / "missing.exe"))
    monkeypatch.setattr("harvey.linkedin.session.chrome_candidates", lambda: [str(exe)])
    assert find_chrome() == str(exe)


def test_the_profile_lives_under_data_by_default():
    path = profile_dir(make_config().channels.linkedin)
    assert path.parts[-2:] == ("data", "linkedin-profile")


class _Keyboard:
    def __init__(self, log):
        self.log = log

    async def type(self, text, delay=0):
        self.log.append(("type", text))

    async def press(self, key):
        self.log.append(("press", key))


class _Locator:
    def __init__(self, log, present=True):
        self.log, self.present, self.first = log, present, self

    async def count(self):
        return 1 if self.present else 0

    async def click(self, **kw):
        self.log.append(("click",))

    async def inner_text(self, **kw):
        return "typed"


class _Page:
    def __init__(self):
        self.log = []
        self.keyboard = _Keyboard(self.log)
        self.url = "https://www.linkedin.com/feed/"

    def locator(self, selector):
        return _Locator(self.log, present=selector != ".missing")

    async def wait_for_timeout(self, ms):
        pass

    async def content(self):
        return '<header id="global-nav"></header>'


def test_a_new_line_is_shift_enter_never_a_bare_enter():
    page = _Page()
    driver = Driver(page)
    assert run(driver.type_text(".msg-form__contenteditable", "Line one\nLine two"))
    keys = [x for x in page.log if x[0] != "click"]
    assert keys == [("type", "Line one"), ("press", "Shift+Enter"), ("type", "Line two")]
    assert not any(x == ("press", "Enter") for x in page.log)
    assert not run(driver.type_text(".missing", "x"))
    assert run(is_signed_in(driver))
