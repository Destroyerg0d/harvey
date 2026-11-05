"""`harvey whatsapp ...`, linking the phone, and the store (no browser is started)."""

import argparse
import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest
import yaml

from harvey.state import StateManager
from harvey.whatsapp import cli as wa_cli
from harvey.whatsapp import guards, session as wa_session
from harvey.whatsapp.pacing import LINKED_AT_KEY
from harvey.whatsapp.session import login_interactive, open_session, profile_dir
from harvey.whatsapp.store import advance_stage
from tests.whatsapp_fakes import FIRST, JANE, MINIMAL, QR_PAGE, FakeBrain, make_config


def run(coro):
    return asyncio.run(coro)


def _parse(*argv):
    parser = argparse.ArgumentParser()
    wa_cli.add_parser(parser.add_subparsers(dest="command"))
    return parser.parse_args(["whatsapp", *argv])


@pytest.fixture
def state(tmp_path):
    return StateManager(str(tmp_path / "h.db"))


def test_the_subcommands_parse():
    assert _parse("login").whatsapp_action == "login"
    args = _parse("run", "--once", "--dry-run")
    assert args.once and args.dry_run
    args = _parse("add", "+14155550100", "--source", "website chat", "--name", "Jane Doe",
                  "--email", "jane@example.org", "--context", "Asked", "--country", "US")
    assert args.target == "+14155550100" and args.source == "website chat" and args.country == "US"
    assert _parse("approve", "--all").approve_all
    with pytest.raises(SystemExit):
        _parse("explode")
    with pytest.raises(SystemExit):
        _parse("import")


def test_add_needs_a_source_and_says_what_happened(state, capsys):
    config = make_config()
    run(state.init_db())
    run(wa_cli._add(_parse("add", "+447700900123"), config, state))
    assert "source" in capsys.readouterr().out
    assert run(state.wa_contacts()) == []
    run(wa_cli._add(_parse("add", "+447700900123", "--source", "website chat"), config, state))
    assert "Queued" in capsys.readouterr().out
    run(wa_cli._add(_parse("add", "+447700900123", "--source", "website chat"), config, state))
    assert "Already here" in capsys.readouterr().out
    run(wa_cli._add(_parse("add", "07700 900124", "--source", "website chat"), config, state))
    assert "Held for you" in capsys.readouterr().out
    run(wa_cli._add(_parse("add", "+447700900125", "--source", "bought list"), config, state))
    assert "Refused" in capsys.readouterr().out


def test_status_pause_resume_approve_skip(state, capsys):
    config = make_config()
    run(state.init_db())
    contact, _ = run(state.wa_add_contact(phone=JANE, source="website chat", name="Jane Doe"))
    a = run(state.wa_add_message(contact, kind="first", body=FIRST))
    b = run(state.wa_add_message(contact, kind="reply", body="Thanks"))
    run(wa_cli._pause(_parse("pause", "--reason", "holiday"), config, state))
    assert run(guards.active_pause(state))["reason"] == "holiday"
    run(wa_cli._status(_parse("status"), config, state))
    out = capsys.readouterr().out
    assert "PAUSED: holiday" in out and "2 drafts waiting" in out and a in out and "+15555550100" in out
    assert "not linked" in out and "0/5 first messages" in out and "0/30 in total" in out
    run(wa_cli._resume(_parse("resume"), config, state))
    assert run(guards.active_pause(state)) is None
    run(wa_cli._approve(_parse("approve", a), config, state))
    assert run(state.wa_get_message(a))["status"] == "approved"
    run(wa_cli._skip(_parse("skip", b), config, state))
    assert run(state.wa_get_message(b))["status"] == "skipped"
    capsys.readouterr()
    run(wa_cli._approve(_parse("approve", "nope"), config, state))
    assert "No draft" in capsys.readouterr().out
    run(wa_cli._approve(_parse("approve"), config, state))
    assert "--all" in capsys.readouterr().out


def test_run_when_off_says_why(state, capsys, monkeypatch):
    monkeypatch.setattr("harvey.brain.Brain", lambda *a, **k: FakeBrain())
    run(wa_cli._run(_parse("run", "--once"), make_config(enabled=False), state))
    assert "Not run: WhatsApp is off" in capsys.readouterr().out


def test_the_login_command_records_when_it_was_linked(state, capsys, monkeypatch):
    async def fine(cfg, **hooks):
        return True
    monkeypatch.setattr(wa_session, "login_interactive", fine)
    run(state.init_db())
    run(guards.set_pause(state, "signed out", kind="signed_out", hours=None))
    run(wa_cli._login(_parse("login"), make_config(), state))
    assert run(state.get_setting(LINKED_AT_KEY))
    assert run(guards.active_pause(state)) is None
    assert "Linked" in capsys.readouterr().out


def test_a_login_that_times_out_records_nothing(state, capsys, monkeypatch):
    async def timed_out(cfg, **hooks):
        return False
    monkeypatch.setattr(wa_session, "login_interactive", timed_out)
    run(state.init_db())
    run(wa_cli._login(_parse("login"), make_config(), state))
    assert run(state.get_setting(LINKED_AT_KEY)) == ""
    assert "Not linked" in capsys.readouterr().out


# ── The session ──

def test_the_profile_is_its_own_under_data():
    assert profile_dir(type(make_config().channels.whatsapp)()).parts[-2:] == ("data", "whatsapp-profile")


def test_it_is_never_headless_and_the_ui_is_english(monkeypatch):
    seen = {}

    @asynccontextmanager
    async def fake_open(cfg, **kw):
        seen.update(kw)
        yield SimpleNamespace(page=object(), rng=None)
    monkeypatch.setattr(wa_session, "_open_chrome", fake_open)

    async def go():
        async with open_session(make_config().channels.whatsapp) as driver:
            assert isinstance(driver, wa_session.WhatsAppDriver)
    run(go())
    assert seen == {"headless": False, "locale": "en-US"}


def test_chrome_not_found_names_the_right_setting(monkeypatch):
    @asynccontextmanager
    async def fake_open(cfg, **kw):
        raise wa_session.ChromeNotFound("set channels.linkedin.chrome_path")
        yield
    monkeypatch.setattr(wa_session, "_open_chrome", fake_open)

    async def go():
        async with open_session(make_config().channels.whatsapp):
            pass
    with pytest.raises(wa_session.ChromeNotFound, match="channels.whatsapp.chrome_path"):
        run(go())


def test_the_driver_types_at_a_persons_pace_and_enter_is_the_send_key():
    log = []

    class Keyboard:
        async def type(self, text, delay=0):
            log.append(("type", text, delay))

        async def press(self, key):
            log.append(("press", key))

    class Locator:
        first = None

        async def count(self):
            return 1

        async def click(self, timeout=0):
            log.append(("click",))

    class FakePage:
        keyboard = Keyboard()

        def locator(self, selector):
            loc = Locator()
            loc.first = loc
            return loc

        async def wait_for_timeout(self, ms):
            pass

    driver = wa_session.WhatsAppDriver(FakePage())

    async def go():
        assert await driver.type_text("#x", "Line one\nLine two")
        await driver.press_enter()
    run(go())
    typed = [e for e in log if e[0] == "type"]
    assert [e[1] for e in typed] == ["Line one", "Line two"]
    assert all(wa_session.KEY_DELAY_MS[0] <= e[2] <= wa_session.KEY_DELAY_MS[1] for e in typed)
    # A new line isn't Enter, or half a message would go; Enter comes once, at the end.
    assert [e[1] for e in log if e[0] == "press"] == ["Shift+Enter", "Enter"]


class _Page:
    """Shows a QR code until the owner has scanned it, or a page of its own."""

    def __init__(self, pages):
        self.pages = list(pages)
        self.url = ""
        self.closed = False

    async def goto(self, url):
        self.url = url

    async def html(self):
        return self.pages.pop(0) if len(self.pages) > 1 else self.pages[0]

    async def wait(self, seconds):
        pass


def _session(page):
    @asynccontextmanager
    async def opener(cfg):
        yield page
    return opener


READY = '<div id="pane-side"></div>'


def test_login_waits_while_the_qr_code_shows(capsys):
    page = _Page([QR_PAGE, QR_PAGE, READY, READY])
    cfg = make_config().channels.whatsapp
    assert run(login_interactive(cfg, poll_seconds=0, session=_session(page))) is True
    out = capsys.readouterr().out
    assert "QR code" in out and "never sees" in out and "30 minutes" in out
    assert page.url == "https://web.whatsapp.com/"


def test_already_linked_says_so(capsys):
    page = _Page([READY])
    assert run(login_interactive(make_config().channels.whatsapp, session=_session(page))) is True
    assert "already linked" in capsys.readouterr().out


def test_login_gives_up_at_the_deadline(capsys):
    cfg = make_config(login_timeout_minutes=0).channels.whatsapp
    assert run(login_interactive(cfg, poll_seconds=0, session=_session(_Page([QR_PAGE])))) is False


def test_the_default_wait_is_generous():
    assert type(make_config().channels.whatsapp)().login_timeout_minutes == 30


def test_a_closed_window_ends_the_wait(capsys):
    page = _Page([QR_PAGE])
    page.closed = True
    assert run(login_interactive(make_config().channels.whatsapp, poll_seconds=0, session=_session(page))) is False
    assert "closed" in capsys.readouterr().out


def test_a_ban_on_the_linking_page_ends_the_wait(capsys):
    page = _Page([QR_PAGE, "<div>Your phone number is banned from using WhatsApp</div>"])
    assert run(login_interactive(make_config().channels.whatsapp, poll_seconds=0, session=_session(page))) is False
    assert "banned" in capsys.readouterr().out


# ── The store ──

@pytest.mark.parametrize("current,target,after", [
    ("queued", "messaged", "messaged"), ("messaged", "replied", "replied"), ("replied", "warm", "warm"),
    ("warm", "meeting", "meeting"), ("meeting", "warm", "meeting"), ("replied", "messaged", "replied"),
    ("declined", "replied", "declined"), ("declined", "queued", "declined"), ("meeting", "declined", "declined"),
    ("queued", "no_whatsapp", "no_whatsapp"), ("replied", "no_whatsapp", "replied"),
    ("no_whatsapp", "messaged", "no_whatsapp"), ("needs_review", "queued", "queued"),
    ("queued", "needs_review", "queued"), ("queued", "nonsense", "queued"),
])
def test_stages_only_move_forward(current, target, after):
    assert advance_stage(current, target) == after


def test_tables_are_made_and_the_number_is_unique(state):
    run(state.init_db())
    run(state.init_db())
    a, created = run(state.wa_add_contact(phone=JANE, source="website chat"))
    b, again = run(state.wa_add_contact(phone=JANE, source="website form", name="Jane Doe"))
    assert created and not again and a == b
    got = run(state.wa_get_contact(a))
    assert got["source"] == "website chat" and got["name"] == "Jane Doe"
    # Any number of contacts can wait for review with no number yet.
    run(state.wa_add_contact(phone="", source="s", stage="needs_review", raw_phone="1"))
    run(state.wa_add_contact(phone="", source="s", stage="needs_review", raw_phone="2"))
    assert run(state.wa_funnel())["needs_review"] == 2


def test_bad_rows_are_refused(state):
    run(state.init_db())
    a, _ = run(state.wa_add_contact(phone=JANE, source="s"))
    with pytest.raises(ValueError):
        run(state.wa_add_message(a, kind="broadcast"))
    with pytest.raises(ValueError):
        run(state.wa_add_message(a, status="whatever"))
    with pytest.raises(ValueError):
        run(state.wa_add_message(a, direction="sideways"))


def test_the_same_whatsapp_id_is_stored_once(state):
    run(state.init_db())
    a, _ = run(state.wa_add_contact(phone=JANE, source="s"))
    assert run(state.wa_add_message(a, direction="in", kind="reply", body="x", status="sent", wa_id="abc"))
    assert run(state.wa_add_message(a, direction="in", kind="reply", body="x", status="sent", wa_id="abc")) is None


def test_a_claim_only_takes_an_approved_message(state):
    run(state.init_db())
    a, _ = run(state.wa_add_contact(phone=JANE, source="s"))
    m = run(state.wa_add_message(a, kind="first", body="x"))
    assert run(state.wa_claim(m)) is False
    run(state.wa_approve(m))
    assert run(state.wa_claim(m)) is True
    assert run(state.wa_get_message(m))["status"] == "maybe_sent"
    assert run(state.wa_claim(m)) is False


def test_the_harvey_yaml_example_is_commented_out_with_no_real_number():
    from pathlib import Path
    root = Path(__file__).resolve().parent.parent
    text = (root / "harvey.yaml").read_text(encoding="utf-8")
    assert "# whatsapp:" in text and "+15555550100" in text
    assert "whatsapp" not in (yaml.safe_load(text)["channels"])
    assert yaml.safe_load(text)["channels"]["linkedin"]["enabled"] is False


def test_a_config_with_whatsapp_loads(tmp_path):
    from harvey.config import load_config
    path = tmp_path / "harvey.yaml"
    path.write_text(yaml.safe_dump({**MINIMAL, "channels": {"whatsapp": {
        "enabled": True, "number": "+15555550100", "do_not_contact": ["+1 415 555 0101"],
        "hours": {"start": "09:00", "end": "17:00", "timezone": "America/Chicago"}}}}))
    cfg = load_config(str(path)).channels.whatsapp
    assert cfg.enabled and cfg.do_not_contact == ["+14155550101"] and cfg.hours.timezone == "America/Chicago"
    assert cfg.max_daily_messages == 30


def test_the_channel_is_off_by_default():
    from harvey.config import ChannelsConfig
    channels = ChannelsConfig()
    assert channels.whatsapp.enabled is False and channels.whatsapp.autopilot is False
