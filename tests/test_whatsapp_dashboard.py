"""The dashboard's WhatsApp tab routes, and its Today cards."""

import asyncio

import pytest
import yaml
from fastapi.testclient import TestClient

import harvey.dashboard as dash
from harvey.agents.whatsapp_agent import AUTOPILOT_KEY
from harvey.models.prospect import Prospect
from harvey.state import StateManager
from harvey.whatsapp import guards
from tests.test_dashboard_signals import client  # noqa: F401  (fixture)
from tests.whatsapp_fakes import FIRST, FOLLOW_UP, JANE, MINIMAL


def run(coro):
    return asyncio.run(coro)


@pytest.fixture
def wa(client, tmp_path, monkeypatch):  # noqa: F811
    path = tmp_path / "harvey.yaml"
    path.write_text(yaml.safe_dump({**MINIMAL, "channels": {"whatsapp": {
        "enabled": True, "number": "+15555550100"}}}))
    monkeypatch.setattr(dash, "_resolve_config_file", lambda: path)
    state = StateManager(str(client.db_path))
    run(state.init_db())
    client.state = state
    return client


def _contact(state, phone=JANE, **kw):
    kw.setdefault("name", "Jane Doe")
    contact_id, _ = run(state.wa_add_contact(phone=phone, source="website chat", **kw))
    return contact_id


def test_overview_has_the_linked_number_funnel_queue_caps_and_conversations(wa):
    state = wa.state
    c = _contact(state, stage="replied", last_inbound_at="2026-11-04T05:00:00",
                 last_outbound_at="2026-11-03T05:00:00")
    run(state.wa_add_message(c, direction="in", kind="reply", body="What would it involve?", status="sent",
                             sent_at="2026-11-04T05:00:00", intent="question"))
    run(state.wa_add_message(c, kind="reply", body="A short chat would tell."))
    run(state.set_setting("whatsapp:linked_at", "2026-11-01T00:00:00"))
    data = wa.get("/api/whatsapp").json()
    assert data["enabled"] is True and data["autopilot"] is False and data["number"] == "+15555550100"
    assert data["linked"] is True and data["linked_at"] == "2026-11-01T00:00:00"
    assert data["funnel"]["replied"] == 1 and data["funnel"]["messaged"] == 1 and data["funnel"]["reply_rate"] == 1.0
    assert data["drafts_waiting"] == 1
    assert data["queue"][0]["body"] == "A short chat would tell." and data["queue"][0]["contact"]["phone"] == JANE
    convo = data["conversations"][0]
    assert convo["latest_in"]["body"] == "What would it involve?" and convo["draft"]["status"] == "draft"
    assert data["caps"]["first_today"] == 5 and data["caps"]["messages_today"] == 30
    assert data["caps"]["messages_hour"] == 8 and data["pause"] is None
    assert data["last_run"] == {}


def test_a_number_never_linked_is_shown_so(wa):
    data = wa.get("/api/whatsapp").json()
    assert data["linked"] is False and data["new_number"] is True


def test_approve_edit_skip_and_approve_all(wa):
    state = wa.state
    c = _contact(state)
    a = run(state.wa_add_message(c, kind="first", body=FIRST))
    b = run(state.wa_add_message(c, kind="first", body=FIRST + " "))
    held = run(state.wa_add_message(c, kind="reply", body="Held", error="held for your review: x"))
    edited = "Hi Jane, you left this number on our site. Reply no and I won't message again."
    assert wa.post(f"/api/whatsapp/messages/{a}/edit", json={"body": edited}).json()["success"]
    assert run(state.wa_get_message(a))["body"] == edited
    assert run(state.wa_get_message(a))["status"] == "draft"          # editing never approves
    assert wa.post(f"/api/whatsapp/messages/{a}/approve").json()["success"]
    assert wa.post(f"/api/whatsapp/messages/{a}/approve").status_code == 409   # already approved
    assert wa.post(f"/api/whatsapp/messages/{b}/skip").json()["success"]
    res = wa.post("/api/whatsapp/messages/approve-all").json()
    assert res["approved"] == 0 and res["held"] == 1                  # held ones one by one
    assert run(state.wa_get_message(held))["status"] == "draft"
    assert wa.post("/api/whatsapp/messages/nope/approve").status_code == 404
    assert wa.post(f"/api/whatsapp/messages/{a}/nonsense").status_code == 404


@pytest.mark.parametrize("body,word", [
    (" ", "empty"),
    ("Hi Jane, see https://example.com. Reply no and I won't message again.", "link"),
    ("Hi Jane, it costs $500. Reply no and I won't message again.", "price"),
    ("Hi Jane, you left this on our website.", "way out"),
])
def test_an_edit_is_held_to_the_same_rules(wa, body, word):
    c = _contact(wa.state)
    m = run(wa.state.wa_add_message(c, kind="first", body=FIRST))
    res = wa.post(f"/api/whatsapp/messages/{m}/edit", json={"body": body})
    assert res.status_code == 400 and word in res.json()["message"]
    assert run(wa.state.wa_get_message(m))["body"] == FIRST


def test_a_send_that_may_have_gone_cant_be_retried_only_answered(wa):
    state = wa.state
    c = _contact(state)
    m = run(state.wa_add_message(c, kind="first", body=FIRST, status="maybe_sent", error="may have"))
    data = wa.get("/api/whatsapp").json()
    assert data["maybe_sent"] == 1 and data["queue"][0]["status"] == "maybe_sent"
    res = wa.post(f"/api/whatsapp/messages/{m}/retry")
    assert res.status_code == 409 and "won't send it again" in res.json()["message"]
    assert wa.post(f"/api/whatsapp/messages/{m}/skip").status_code == 409
    assert wa.post(f"/api/whatsapp/messages/{m}/approve").status_code == 409
    assert run(state.wa_get_message(m))["status"] == "maybe_sent"
    keys = [i["key"] for i in wa.get("/api/today").json()["items"]]
    assert "whatsapp-maybe-sent" in keys
    assert wa.post(f"/api/whatsapp/messages/{m}/went").json()["success"]
    assert run(state.wa_get_message(m))["status"] == "sent"
    assert run(state.wa_get_contact(c))["stage"] == "messaged"
    other = run(state.wa_add_message(c, kind="follow_up", body=FOLLOW_UP, status="maybe_sent"))
    assert wa.post(f"/api/whatsapp/messages/{other}/not_sent").json()["success"]
    assert run(state.wa_get_message(other))["status"] == "skipped"
    assert wa.post(f"/api/whatsapp/messages/{other}/went").status_code == 409


def test_a_failed_send_can_be_retried(wa):
    c = _contact(wa.state)
    m = run(wa.state.wa_add_message(c, kind="first", body=FIRST, status="failed", error="no composer"))
    assert wa.post(f"/api/whatsapp/messages/{m}/retry").json()["success"]
    assert run(wa.state.wa_get_message(m))["status"] == "approved"


def test_autopilot_needs_the_word_and_turning_it_off_requeues(wa):
    state = wa.state
    m = run(state.wa_add_message(_contact(state), kind="first", body=FIRST, status="approved",
                                 approved_by="autopilot"))
    assert wa.post("/api/whatsapp/autopilot", json={"mode": "autopilot"}).status_code == 400
    assert wa.post("/api/whatsapp/autopilot", json={"mode": "autopilot", "confirm": "auto"}).status_code == 400
    assert wa.post("/api/whatsapp/autopilot", json={"mode": "autopilot", "confirm": "autopilot"}).json()["success"]
    assert run(state.get_setting(AUTOPILOT_KEY)) == "on"
    assert wa.get("/api/whatsapp").json()["autopilot"] is True
    res = wa.post("/api/whatsapp/autopilot", json={"mode": "approval"}).json()
    assert res["success"] and res["requeued"] == 1
    assert run(state.wa_get_message(m))["status"] == "draft"
    assert wa.post("/api/whatsapp/autopilot", json={"mode": "x"}).status_code == 400


def test_the_kill_switch_and_the_today_cards(wa):
    state = wa.state
    assert wa.post("/api/whatsapp/pause").json()["success"]
    pause = run(guards.active_pause(state))
    assert pause and pause["kind"] == "manual" and pause["until"] is None
    run(state.wa_add_message(_contact(state), kind="first", body=FIRST))
    keys = [i["key"] for i in wa.get("/api/today").json()["items"]]
    assert "whatsapp-paused" in keys and "whatsapp-drafts" in keys
    assert wa.get("/api/whatsapp").json()["pause"]["kind"] == "manual"
    assert wa.post("/api/whatsapp/resume").json()["success"]
    assert run(guards.active_pause(state)) is None
    assert wa.post("/api/whatsapp/nonsense").status_code == 404


def test_marking_a_contact_declined_stops_their_email(wa):
    state = wa.state
    pid = run(state.add_prospect(Prospect(first_name="Jane", last_name="Doe", title="Founder",
                                          email="jane@example.org", status="contacted")))
    c = _contact(state, stage="replied", prospect_id=pid)
    draft = run(state.wa_add_message(c, kind="reply", body="Draft"))
    assert wa.post(f"/api/whatsapp/contacts/{c}/stage", json={"stage": "meeting"}).json()["stage"] == "meeting"
    assert wa.post(f"/api/whatsapp/contacts/{c}/stage", json={"stage": "warm"}).status_code == 409
    assert wa.post(f"/api/whatsapp/contacts/{c}/stage", json={"stage": "declined"}).json()["success"]
    assert run(state.wa_get_contact(c))["stage"] == "declined"
    assert run(state.get_prospect(pid)).status == "opted_out"
    assert run(state.wa_get_message(draft))["status"] == "skipped"
    assert wa.post(f"/api/whatsapp/contacts/{c}/stage", json={"stage": "queued"}).status_code == 400
    assert wa.post("/api/whatsapp/contacts/nope/stage", json={"stage": "warm"}).status_code == 404


def test_fixing_or_dropping_a_number_waiting_for_review(wa):
    state = wa.state
    c = _contact(state, phone="", stage="needs_review", raw_phone="07700 900123",
                 review_reason="it has no + and no country")
    data = wa.get("/api/whatsapp").json()
    assert data["review"][0]["raw_phone"] == "07700 900123" and data["funnel"]["needs_review"] == 1
    res = wa.post(f"/api/whatsapp/contacts/{c}/number", json={"phone": "07700 900123"})
    assert res.status_code == 400 and "can't be established" in res.json()["message"]
    assert wa.post(f"/api/whatsapp/contacts/{c}/number", json={"phone": "+447700900123"}).json()["success"]
    got = run(state.wa_get_contact(c))
    assert got["stage"] == "queued" and got["phone"] == JANE

    pid = run(state.add_prospect(Prospect(first_name="Ada", last_name="Lane", title="Owner",
                                          email="ada@example.org", status="contacted")))
    other = _contact(state, phone="", stage="needs_review", raw_phone="555", prospect_id=pid)
    assert wa.post(f"/api/whatsapp/contacts/{other}/stage", json={"stage": "declined"}).json()["success"]
    assert run(state.wa_get_contact(other))["stage"] == "declined"
    assert run(state.get_prospect(pid)).status == "contacted"     # a dropped number isn't their no


def test_the_tab_is_in_the_page(wa):
    html = wa.get("/").text
    assert "showTab('whatsapp'" in html and 'id="wa-queue"' in html and 'id="wa-mode"' in html


def test_the_routes_are_behind_the_token(wa):
    bare = TestClient(dash.app, base_url="http://127.0.0.1:5555")
    assert bare.get("/api/whatsapp").status_code == 401
    assert bare.post("/api/whatsapp/pause").status_code in (401, 403)
    assert run(guards.active_pause(wa.state)) is None
