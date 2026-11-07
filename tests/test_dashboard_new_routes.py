"""The dashboard routes for writing your own reply, guiding Harvey, the Website
leads tab and the channels strip."""

import asyncio
import json
import sqlite3

import pytest

import harvey.config as harvey_config
from tests.test_dashboard_signals import client  # noqa: F401  (fixture)
from tests.test_inbound import config
from harvey.state import StateManager


def _sql(db, query, params=()):
    with sqlite3.connect(db) as conn:
        conn.execute(query, params)
    conn.close()


def _rows(db, query, params=()):
    with sqlite3.connect(db) as conn:
        conn.row_factory = sqlite3.Row
        rows = [dict(r) for r in conn.execute(query, params)]
    conn.close()
    return rows


@pytest.fixture(autouse=True)
def test_config(monkeypatch):
    monkeypatch.setattr(harvey_config, "load_config", lambda *a, **k: config())


def _seed(db, status="open"):
    asyncio.run(StateManager(str(db)).init_db())
    _sql(db, "INSERT INTO prospects (id, first_name, email, email_status, status) "
             "VALUES ('p1', 'Iris', 'iris@acme-shop.example', 'verified', 'replied')")
    thread = [{"sender": "prospect", "content": "What does it cost?", "timestamp": "2026-10-18T10:00:00",
               "message_id": "<theirs@acme-shop.example>", "thread_ref": "t-9", "subject": "Re: hello"}]
    _sql(db, "INSERT INTO conversations (id, prospect_id, thread_json, intent, status, updated_at) "
             "VALUES ('c1', 'p1', ?, 'question', ?, '2026-10-18T10:00:00')", (json.dumps(thread), status))
    _sql(db, "INSERT INTO outbox (id, prospect_id, campaign_id, kind, step, to_email, subject, body, status, "
             "sent_at, send_at, message_id, thread_ref) VALUES ('o1', 'p1', 'k1', 'sequence', 1, "
             "'iris@acme-shop.example', 'hello', 'Hi Iris', 'sent', '2026-10-10T09:00:00', "
             "'2026-10-10T09:00:00', '<ours@ours.example>', 't-9')")


def test_your_reply_is_queued_on_the_thread_and_shows_as_on_its_way(client):
    _seed(client.db_path)
    r = client.post("/api/conversations/c1/reply", json={"body": "About two weeks. Call Tuesday?"})
    assert r.status_code == 200 and r.json()["success"]
    [row] = _rows(client.db_path, "SELECT * FROM outbox WHERE kind = 'reply'")
    assert row["status"] == "approved" and row["approved_by"] == "you"
    assert row["in_reply_to"] == "<theirs@acme-shop.example>" and row["thread_ref"] == "t-9"
    [convo] = client.get("/api/conversations").json()
    assert convo["queued"]["body"] == "About two weeks. Call Tuesday?"


def test_a_refused_reply_says_why(client):
    _seed(client.db_path)
    _sql(client.db_path, "UPDATE prospects SET status = 'opted_out' WHERE id = 'p1'")
    r = client.post("/api/conversations/c1/reply", json={"body": "Hello"})
    assert r.status_code == 400 and "opted out" in r.json()["message"]


def test_guidance_that_cant_start_says_why(client):
    _seed(client.db_path, status="needs_human")
    r = client.post("/api/conversations/c1/guide", json={"guidance": "  "})
    assert r.status_code == 400 and "first" in r.json()["message"]
    [convo] = client.get("/api/conversations").json()
    assert convo["guidance"] is None


def test_the_website_leads_tab_and_the_channels_strip_answer(client):
    asyncio.run(StateManager(str(client.db_path)).init_db())
    inbound = client.get("/api/inbound").json()
    assert set(inbound) >= {"connection", "counts", "leads", "jobs"}
    channels = client.get("/api/channels").json()
    assert set(channels) >= {"email", "whatsapp", "linkedin", "erp"}


def test_a_chat_is_never_looked_up_by_a_url_and_a_paste_needs_text_and_an_address(client):
    asyncio.run(StateManager(str(client.db_path)).init_db())
    assert client.get("/api/inbound/chat?email=iris@acme-shop.example").status_code == 405
    assert client.post("/api/inbound/chat", json={"email": "nope"}).status_code == 400
    assert client.post("/api/inbound/paste", json={"chat": " "}).status_code == 400
    r = client.post("/api/inbound/paste", json={"chat": "We need a shop built"})
    assert r.status_code == 400 and "email" in r.json()["message"]
