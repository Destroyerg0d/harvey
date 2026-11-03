"""The dashboard's LinkedIn tab routes, and its Today cards."""

import asyncio

import pytest
import yaml

import harvey.dashboard as dash
from harvey.agents.linkedin_agent import AUTOPILOT_KEY
from harvey.linkedin import guards
from harvey.models.prospect import Prospect
from harvey.state import StateManager
from tests.linkedin_fakes import MINIMAL
from tests.test_dashboard_signals import client  # noqa: F401  (fixture)


def run(coro):
    return asyncio.run(coro)


@pytest.fixture
def li(client, tmp_path, monkeypatch):  # noqa: F811
    path = tmp_path / "harvey.yaml"
    path.write_text(yaml.safe_dump({**MINIMAL, "channels": {"linkedin": {"enabled": True}}}))
    monkeypatch.setattr(dash, "_resolve_config_file", lambda: path)
    state = StateManager(str(client.db_path))
    run(state.init_db())
    client.state = state
    return client


def _lead(state, slug="jane-doe-1", **kw):
    lead_id, _ = run(state.li_upsert_lead(slug, name="Jane Doe", **kw))
    return lead_id


def test_overview_has_the_funnel_queue_caps_and_chart(li):
    state = li.state
    lead = _lead(state, stage="replied", last_inbound_at="2026-11-04T05:00:00")
    run(state.li_add_message(lead, direction="in", body="What would it cost?", status="sent",
                             sent_at="2026-11-04T05:00:00", intent="question"))
    run(state.li_add_message(lead, kind="reply", body="About a week of work.", status="draft"))
    data = li.get("/api/linkedin?tz=330").json()
    assert data["enabled"] is True and data["autopilot"] is False
    assert data["funnel"]["replied"] == 1 and data["drafts_waiting"] == 1
    assert data["queue"][0]["body"] == "About a week of work." and data["queue"][0]["lead"]["name"] == "Jane Doe"
    convo = data["conversations"][0]
    assert convo["latest_in"]["body"] == "What would it cost?" and convo["draft"]["status"] == "draft"
    assert data["caps"]["invites_today"] == 15 and data["caps"]["messages_today"] == 25
    assert len(data["chart"]["days"]) == 14 and data["pause"] is None
    assert "Asia/Kolkata" in data["hours"]


def test_approve_edit_skip_and_approve_all(li):
    state = li.state
    lead = _lead(state, stage="accepted")
    a = run(state.li_add_message(lead, body="Draft A"))
    b = run(state.li_add_message(lead, body="Draft B"))
    held = run(state.li_add_message(lead, kind="reply", body="Held", error="held for your review: x",
                                    dedupe=False))
    assert li.post(f"/api/linkedin/messages/{a}/edit", json={"body": "Draft A, better"}).json()["success"]
    assert run(state.li_get_message(a))["body"] == "Draft A, better"
    assert run(state.li_get_message(a))["status"] == "draft"          # editing never approves
    assert li.post(f"/api/linkedin/messages/{a}/edit", json={"body": " "}).status_code == 400
    assert li.post(f"/api/linkedin/messages/{a}/approve").json()["success"]
    assert li.post(f"/api/linkedin/messages/{a}/approve").status_code == 409   # already approved
    assert li.post(f"/api/linkedin/messages/{b}/skip").json()["success"]
    res = li.post("/api/linkedin/messages/approve-all").json()
    assert res["approved"] == 0 and res["held"] == 1                   # held ones one by one
    assert run(state.li_get_message(held))["status"] == "draft"
    assert li.post("/api/linkedin/messages/nope/approve").status_code == 404


def test_invite_note_edit_respects_the_limit(li):
    lead = _lead(li.state)
    note = run(li.state.li_add_message(lead, kind="invite_note", body="Hi"))
    assert li.post(f"/api/linkedin/messages/{note}/edit", json={"body": "x" * 201}).status_code == 400
    assert li.post(f"/api/linkedin/messages/{note}/edit", json={"body": ""}).json()["success"]


def test_autopilot_needs_the_word_and_turning_it_off_requeues(li):
    state = li.state
    lead = _lead(state)
    m = run(state.li_add_message(lead, body="Hi", status="approved", approved_by="autopilot"))
    assert li.post("/api/linkedin/autopilot", json={"mode": "autopilot"}).status_code == 400
    assert li.post("/api/linkedin/autopilot", json={"mode": "autopilot", "confirm": "auto"}).status_code == 400
    assert li.post("/api/linkedin/autopilot", json={"mode": "autopilot", "confirm": "autopilot"}).json()["success"]
    assert run(state.get_setting(AUTOPILOT_KEY)) == "on"
    assert li.get("/api/linkedin").json()["autopilot"] is True
    res = li.post("/api/linkedin/autopilot", json={"mode": "approval"}).json()
    assert res["success"] and res["requeued"] == 1
    assert run(state.li_get_message(m))["status"] == "draft"


def test_pause_resume_and_the_today_cards(li):
    state = li.state
    assert li.post("/api/linkedin/pause").json()["success"]
    pause = run(guards.active_pause(state))
    assert pause and pause["kind"] == "manual" and pause["until"] is None
    lead = _lead(state)
    run(state.li_add_message(lead, body="Draft"))
    keys = [i["key"] for i in li.get("/api/today").json()["items"]]
    assert "linkedin-paused" in keys and "linkedin-drafts" in keys
    assert li.post("/api/linkedin/resume").json()["success"]
    assert run(guards.active_pause(state)) is None
    assert li.post("/api/linkedin/nonsense").status_code == 404


def test_marking_a_lead_declined_stops_their_email(li):
    state = li.state
    pid = run(state.add_prospect(Prospect(first_name="Jane", last_name="Doe", title="Founder",
                                          email="jane@example.org", status="contacted")))
    lead = _lead(state, stage="replied", prospect_id=pid)
    draft = run(state.li_add_message(lead, kind="reply", body="Draft"))
    assert li.post(f"/api/linkedin/leads/{lead}/stage", json={"stage": "meeting"}).json()["stage"] == "meeting"
    assert li.post(f"/api/linkedin/leads/{lead}/stage", json={"stage": "warm"}).status_code == 409
    assert li.post(f"/api/linkedin/leads/{lead}/stage", json={"stage": "declined"}).json()["success"]
    assert run(state.li_get_lead(lead))["stage"] == "declined"
    assert run(state.get_prospect(pid)).status == "opted_out"
    assert run(state.li_get_message(draft))["status"] == "skipped"
    assert li.post(f"/api/linkedin/leads/{lead}/stage", json={"stage": "found"}).status_code == 400


def test_the_tab_is_in_the_page(li):
    html = li.get("/").text
    assert "showTab('linkedin'" in html and 'id="li-queue"' in html
