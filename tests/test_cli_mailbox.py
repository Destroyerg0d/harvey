"""The per-mailbox CLI: harvey gmail auth|test --mailbox and harvey sending
pause|resume --mailbox. Without --mailbox they are what they always were."""

import asyncio
from types import SimpleNamespace

import pytest

import harvey.config as config_mod
import harvey.integrations.gmail as gmail
import harvey.mailboxes as mb
import harvey.state as state_mod
from tests.test_mailboxes import MAILBOXES, PRIMARY, SECOND, THIRD, make_config


# ── CLI: sending pause|resume --mailbox ──

@pytest.fixture
def cli_db(tmp_path, monkeypatch):
    db = tmp_path / "data" / "harvey.db"
    monkeypatch.setattr(state_mod, "DB_PATH", db)
    state = state_mod.StateManager(str(db))
    asyncio.run(state.init_db())
    return state


def run_cli(monkeypatch, argv):
    from harvey import cli
    monkeypatch.setattr("sys.argv", ["harvey", *argv])
    try:
        cli.main()
    except SystemExit as e:
        return e.code
    return 0


async def settings(state, *keys):
    return [await state.get_setting(k) for k in keys]


def test_pause_and_resume_one_mailbox(monkeypatch, cli_db, capsys):
    config = make_config(MAILBOXES)
    monkeypatch.setattr(config_mod, "load_config", lambda *a, **k: config)
    keys = (mb.paused_key(SECOND), mb.bounce_key(SECOND), "sending_paused", "bounce_count",
            mb.paused_key(PRIMARY))
    assert run_cli(monkeypatch, ["sending", "pause", "--mailbox", "Second@Sender-One.test"]) == 0
    assert asyncio.run(settings(cli_db, *keys)) == ["paused manually", "", "", "", ""]
    asyncio.run(cli_db.set_setting(mb.bounce_key(SECOND), "4"))
    asyncio.run(cli_db.set_setting("bounce_count", "7"))
    assert run_cli(monkeypatch, ["sending", "status", "--mailbox", SECOND]) == 0
    assert "PAUSED" in capsys.readouterr().out
    assert run_cli(monkeypatch, ["sending", "resume", "--mailbox", SECOND]) == 0
    assert asyncio.run(settings(cli_db, *keys)) == ["", "0", "", "7", ""]


def test_pause_without_a_mailbox_is_the_global_switch(monkeypatch, cli_db):
    config = make_config(MAILBOXES)
    monkeypatch.setattr(config_mod, "load_config", lambda *a, **k: config)
    assert run_cli(monkeypatch, ["sending", "pause"]) == 0
    assert asyncio.run(settings(cli_db, "sending_paused", mb.paused_key(SECOND))) == [
        "paused manually", ""]
    asyncio.run(cli_db.set_setting("bounce_count", "3"))
    assert run_cli(monkeypatch, ["sending", "resume"]) == 0
    assert asyncio.run(settings(cli_db, "sending_paused", "bounce_count")) == ["", "0"]


def test_an_unknown_mailbox_lists_the_configured_ones(monkeypatch, cli_db, capsys):
    config = make_config(MAILBOXES)
    monkeypatch.setattr(config_mod, "load_config", lambda *a, **k: config)
    assert run_cli(monkeypatch, ["sending", "pause", "--mailbox", "nobody@sender-one.test"]) == 1
    out = capsys.readouterr().out
    assert PRIMARY in out and SECOND in out and THIRD in out
    assert asyncio.run(settings(cli_db, mb.paused_key("nobody@sender-one.test"))) == [""]


def test_a_mailbox_pause_needs_mailboxes_configured(monkeypatch, cli_db, capsys):
    config = make_config()
    monkeypatch.setattr(config_mod, "load_config", lambda *a, **k: config)
    assert run_cli(monkeypatch, ["sending", "pause", "--mailbox", PRIMARY]) == 1
    assert "No mailboxes are configured" in capsys.readouterr().out
    assert asyncio.run(settings(cli_db, "sending_paused", mb.paused_key(PRIMARY))) == ["", ""]


# ── CLI: gmail auth|test --mailbox ──

@pytest.fixture
def auth_calls(monkeypatch):
    calls = []
    monkeypatch.setattr(config_mod, "load_env", lambda: SimpleNamespace(
        gmail_client_id="id", gmail_client_secret="secret"))
    monkeypatch.setattr(gmail, "run_auth_flow",
                        lambda *a, **k: calls.append((a, k)) or True)
    return calls


@pytest.mark.parametrize("flag,token_path,expected", [
    ([], lambda: gmail.TOKEN_FILE, PRIMARY),
    (["--mailbox", "SECOND@sender-one.test"],
     lambda: gmail.TOKENS_DIR / f"{SECOND}.json", SECOND),
])
def test_gmail_auth_signs_in_the_chosen_mailbox(monkeypatch, auth_calls, flag, token_path,
                                                expected):
    config = make_config(MAILBOXES)
    monkeypatch.setattr(config_mod, "load_config", lambda *a, **k: config)
    assert run_cli(monkeypatch, ["gmail", "auth", *flag]) == 0
    assert auth_calls == [(("id", "secret"),
                           {"token_path": token_path(), "expected_email": expected})]


def test_gmail_auth_refuses_a_mailbox_that_isnt_configured(monkeypatch, auth_calls, capsys):
    config = make_config(MAILBOXES)
    monkeypatch.setattr(config_mod, "load_config", lambda *a, **k: config)
    assert run_cli(monkeypatch, ["gmail", "auth", "--mailbox", "nobody@sender-one.test"]) == 1
    assert auth_calls == []
    assert SECOND in capsys.readouterr().out


def test_single_mode_gmail_auth_checks_persona_email(monkeypatch, auth_calls):
    config = make_config()
    monkeypatch.setattr(config_mod, "load_config", lambda *a, **k: config)
    assert run_cli(monkeypatch, ["gmail", "auth"]) == 0
    assert auth_calls == [(("id", "secret"), {"token_path": None, "expected_email": PRIMARY})]


def test_gmail_auth_before_any_config_signs_in_as_before(monkeypatch, auth_calls):
    def missing(*a, **k):
        raise config_mod.ConfigFileNotFoundError("no config")
    monkeypatch.setattr(config_mod, "load_config", missing)
    assert run_cli(monkeypatch, ["gmail", "auth"]) == 0
    assert auth_calls == [(("id", "secret"), {})]


def test_gmail_test_checks_every_mailbox(monkeypatch, auth_calls, capsys):
    config = make_config(MAILBOXES)
    monkeypatch.setattr(config_mod, "load_config", lambda *a, **k: config)

    async def fake_test(self):
        if self.mailbox.email == SECOND:
            return False, "Not authorized yet"
        return True, f"Connected as {self.mailbox.email}"
    monkeypatch.setattr(gmail.GmailProvider, "test_connection", fake_test)
    assert run_cli(monkeypatch, ["gmail", "test"]) == 1
    lines = [l.strip() for l in capsys.readouterr().out.splitlines() if l.strip()]
    assert lines == [f"✓ {PRIMARY}: OK", f"✗ {SECOND}: Not authorized yet", f"✓ {THIRD}: OK"]
    assert run_cli(monkeypatch, ["gmail", "test", "--mailbox", THIRD]) == 0
    assert [l.strip() for l in capsys.readouterr().out.splitlines() if l.strip()] == [
        f"✓ {THIRD}: OK"]
