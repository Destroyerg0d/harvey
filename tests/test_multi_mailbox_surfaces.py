"""Several mailboxes in `harvey status`, `harvey warmup` and the dashboard:
each mailbox shown and resumable on its own, its sign-off filled in where an
email is shown, and hand sends opened in the prospect's own mailbox. With one
mailbox, none of it appears."""

import asyncio
import json
import sqlite3

import pytest
from fastapi.testclient import TestClient

import harvey.config as config_mod
import harvey.dashboard as dash
import harvey.integrations.gmail as gmail
import harvey.mailboxes as mb
import harvey.state as state_mod
from harvey.state import StateManager
from tests.test_cli_mailbox import run_cli
from tests.test_mailboxes import PRIMARY, SECOND, THIRD, make_config
from tests.test_multi_mailbox_send import BOXES, connect

ORIGIN = "http://127.0.0.1:5555"


@pytest.fixture
def tokens(tmp_path, monkeypatch):
    monkeypatch.setattr(gmail, "TOKEN_FILE", tmp_path / "gmail_token.json")
    monkeypatch.setattr(gmail, "TOKENS_DIR", tmp_path / "gmail_tokens")
    return tmp_path


@pytest.fixture
def multi(monkeypatch, tokens):
    cfg = make_config(BOXES)
    monkeypatch.setattr(config_mod, "load_config", lambda *a, **k: cfg)
    return cfg


@pytest.fixture
def db(tmp_path, monkeypatch):
    path = tmp_path / "data" / "harvey.db"
    path.parent.mkdir(exist_ok=True)
    monkeypatch.setattr(state_mod, "DB_PATH", path)
    asyncio.run(StateManager(str(path)).init_db())
    return path


def sql(path, query, params=()):
    with sqlite3.connect(path) as con:
        con.execute(query, params)


def outbox_row(path, oid, mailbox, status="pending_review", body="Hi,\n\nQuestion?\n\n{{sender_first_name}}",
               pid="p1", campaign="c1"):
    sql(path, "INSERT INTO outbox (id, campaign_id, prospect_id, step, kind, to_email, subject, "
              "body, status, send_at, mailbox) VALUES (?, ?, ?, 1, 'sequence', 'jo@roofco.test', "
              "'booking jobs', ?, ?, '2026-01-01T00:00:00', ?)",
        (oid, campaign, pid, body, status, mailbox))


# ── harvey status ──

def test_status_lists_each_mailbox(monkeypatch, multi, db, capsys):
    connect(multi, PRIMARY, SECOND)
    state = StateManager(str(db))
    asyncio.run(state.set_setting(mb.paused_key(SECOND), "bounce rate 2/10 exceeded 5%"))
    outbox_row(db, "o1", SECOND, status="approved")
    outbox_row(db, "o2", "", status="pending_review", pid="p2")
    assert run_cli(monkeypatch, ["status"]) == 0
    out = capsys.readouterr().out
    assert "3 configured, 2 signed in" in out
    assert f"{SECOND}, signs as Sam" in out and f"{PRIMARY} (primary), signs as Alex" in out
    assert f"NOT SIGNED IN (harvey gmail auth --mailbox {THIRD})" in out
    assert "queue 1; 1 due and waiting" in out           # SECOND's approved row
    assert f"PAUSED: bounce rate 2/10 exceeded 5% (harvey sending resume --mailbox {SECOND})" in out
    assert "RESTART NEEDED" not in out


def test_status_says_when_a_running_harvey_needs_a_restart(monkeypatch, multi, db, capsys):
    import harvey.pidfile as pidfile
    monkeypatch.setattr(pidfile, "loop_lock_held", lambda *a: True)
    state = StateManager(str(db))
    asyncio.run(state.set_setting(mb.IN_USE_KEY, PRIMARY))       # started with one mailbox
    assert run_cli(monkeypatch, ["status"]) == 0
    assert "RESTART NEEDED" in capsys.readouterr().out
    asyncio.run(state.set_setting(mb.IN_USE_KEY, mb.in_use_value(multi)))
    assert run_cli(monkeypatch, ["status"]) == 0
    assert "RESTART NEEDED" not in capsys.readouterr().out


def test_status_with_one_mailbox_has_no_mailbox_section(monkeypatch, tokens, db, capsys):
    monkeypatch.setattr(config_mod, "load_config", lambda *a, **k: make_config())
    assert run_cli(monkeypatch, ["status"]) == 0
    assert "Mailboxes:" not in capsys.readouterr().out


# ── harvey warmup ──

@pytest.fixture
def offline_checks(monkeypatch):
    from harvey import cli
    from harvey import deliverability as d
    checked = []

    def ok(name):
        def check(domain=None, *a, **k):
            checked.append((name, domain))
            return d.Check(name, d.OK, "fine")
        return check
    for name in ("check_mx", "check_spf", "check_dkim", "check_dmarc", "check_domain_age"):
        monkeypatch.setattr(d, name, ok(name))
    monkeypatch.setattr(d, "check_domain_blocklists", lambda domain: [d.Check("Blocklists", d.OK, "none")])
    monkeypatch.setattr(cli, "_warmup_service_check",
                        lambda address, warmup: checked.append(("service", address))
                        or d.Check("Warmup service", d.INFO, address))
    return checked


def test_warmup_checks_every_mailbox_domain(monkeypatch, multi, db, offline_checks, capsys):
    assert run_cli(monkeypatch, ["warmup"]) == 0
    out = capsys.readouterr().out
    assert "Deliverability check - sender-one.test" in out
    assert "Deliverability check - sender-two.test" in out
    assert ("check_mx", "sender-one.test") in offline_checks
    assert ("check_mx", "sender-two.test") in offline_checks
    assert [c for c in offline_checks if c[0] == "service"] == [
        ("service", PRIMARY), ("service", SECOND), ("service", THIRD)]


def test_warmup_with_a_domain_or_one_mailbox_checks_one(monkeypatch, tokens, db, offline_checks, capsys):
    monkeypatch.setattr(config_mod, "load_config", lambda *a, **k: make_config(BOXES))
    assert run_cli(monkeypatch, ["warmup", "sender-two.test"]) == 0
    assert capsys.readouterr().out.count("Deliverability check - ") == 1
    monkeypatch.setattr(config_mod, "load_config", lambda *a, **k: make_config())
    assert run_cli(monkeypatch, ["warmup"]) == 0
    out = capsys.readouterr().out
    assert out.count("Deliverability check - ") == 1 and "sender-one.test" in out


# ── Dashboard ──

@pytest.fixture
def client(tmp_path, monkeypatch, db):
    monkeypatch.setattr(dash, "ENV_FILE", tmp_path / ".env")
    monkeypatch.setattr(dash, "DB_PATH", db)
    monkeypatch.setattr(dash, "PID_FILE", tmp_path / "data" / "harvey.pid")
    monkeypatch.setattr(dash, "LOG_FILE", tmp_path / "data" / "harvey.log")
    dash.token_store.configure(tmp_path / "data" / "dashboard-token")
    monkeypatch.setattr(dash, "_harvey_can_send", lambda: False)
    with TestClient(dash.app, base_url=ORIGIN,
                    headers={"Authorization": f"Bearer {dash.token_store.get()}"}) as c:
        yield c


def test_outbox_rows_say_their_mailbox_and_sign_off(client, multi, db):
    outbox_row(db, "o1", SECOND)
    outbox_row(db, "o2", "", pid="p2")
    state = StateManager(str(db))
    asyncio.run(state.set_setting(mb.paused_key(THIRD), "paused manually"))
    data = client.get("/api/outbox").json()
    rows = {r["id"]: r for r in data["pending"]}
    assert (rows["o1"]["mailbox_address"], rows["o1"]["sender_first_name"]) == (SECOND, "Sam")
    assert (rows["o2"]["mailbox_address"], rows["o2"]["sender_first_name"]) == (PRIMARY, "Alex")
    assert data["paused_mailboxes"] == [{"email": THIRD, "reason": "paused manually"}]


def test_with_one_mailbox_the_outbox_is_as_before(client, tokens, db, monkeypatch):
    monkeypatch.setattr(config_mod, "load_config", lambda *a, **k: make_config())
    outbox_row(db, "o1", "")
    data = client.get("/api/outbox").json()
    assert "paused_mailboxes" not in data
    assert "mailbox_address" not in data["pending"][0]


def test_an_edit_keeping_the_sign_off_tag_is_accepted(client, multi, db):
    outbox_row(db, "o1", SECOND)
    ok = client.post("/api/outbox/o1/edit", json={
        "subject": "booking jobs", "body": "Hi,\n\nHow do you book jobs?\n\n{{sender_first_name}}"})
    assert ok.json()["success"] is True
    bad = client.post("/api/outbox/o1/edit", json={
        "subject": "booking jobs", "body": "Hi {{nickname}},\n\nHow?\n\n{{sender_first_name}}"})
    assert bad.status_code == 400 and "unrendered merge tags" in bad.json()["message"]


def test_one_mailbox_can_be_resumed_on_its_own(client, multi, db):
    state = StateManager(str(db))
    asyncio.run(state.set_setting(mb.paused_key(SECOND), "bounce rate 2/10"))
    asyncio.run(state.set_setting(mb.bounce_key(SECOND), "2"))
    asyncio.run(state.set_setting("sending_paused", "paused from dashboard"))
    assert client.post(f"/api/sending/resume?mailbox={SECOND}").json()["success"] is True
    assert asyncio.run(state.get_setting(mb.paused_key(SECOND))) == ""
    assert asyncio.run(state.get_setting(mb.bounce_key(SECOND))) == "0"
    assert asyncio.run(state.get_setting("sending_paused")) == "paused from dashboard"
    assert client.post("/api/sending/pause?mailbox=nobody@else.test").status_code == 404


def test_a_hand_send_opens_in_and_signs_as_the_prospects_mailbox(client, multi, db):
    sql(db, "INSERT INTO prospects (id, first_name, last_name, email, email_status, status, "
            "company, mailbox) VALUES ('p1', 'Jo', '', 'jo@roofco.test', 'verified', 'new', "
            "'Roof Co', ?)", (SECOND,))
    sql(db, "INSERT INTO campaigns (id, name, channel, sequence_json, prospect_ids_json, status) "
            "VALUES ('c1', 'c1', 'email', ?, ?, 'draft')",
        (json.dumps([{"step": 1, "subject": "hi", "body": "Hi\n\n{{sender_first_name}}",
                      "delay_days": 0}]), json.dumps(["p1"])))
    [campaign] = client.get("/api/campaigns").json()
    [r] = campaign["recipients"]
    assert (r["send_as"], r["sender_first_name"]) == (SECOND, "Sam")


def test_settings_count_the_connected_mailboxes(client, multi):
    connect(multi, PRIMARY, THIRD)
    data = client.get("/api/settings").json()
    assert (data["mailboxes_connected"], data["mailboxes_configured"]) == (2, 3)


def test_harvey_can_send_with_any_mailbox_signed_in(tokens, monkeypatch):
    from types import SimpleNamespace
    cfg = make_config(BOXES)
    monkeypatch.setattr(config_mod, "load_config", lambda *a, **k: cfg)
    monkeypatch.setattr(config_mod, "load_env", lambda *a, **k: SimpleNamespace(
        gmail_client_id="id", gmail_client_secret="secret", instantly_api_key=""))
    assert dash._harvey_can_send() is False
    connect(cfg, THIRD)                                  # not the primary
    assert dash._harvey_can_send() is True
