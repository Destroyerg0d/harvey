"""The Meetings tab (harvey/meetings.py, harvey/meetings_web.py; owner,
2026-11-22): every meeting, its time in their zone and yours, when it was
booked, the conversation, and a prep brief. People, numbers and links here are
invented."""

import asyncio
import json
from datetime import datetime, timedelta, timezone

import pytest
import yaml
from fastapi.testclient import TestClient

import harvey.dashboard as dash
from harvey import meetings
from harvey.agents.linkedin_agent import PROFILE_KEY
from harvey.meeting_slot import OFFERED_KEY
from harvey.models.company import Company
from harvey.models.conversation import Conversation, Message
from harvey.models.prospect import Prospect
from harvey.state import StateManager
from tests.linkedin_fakes import MINIMAL, make_config
from tests.test_dashboard_signals import client  # noqa: F401  (fixture)

MEET = "https://meet.example.com/abc-defg-hij"
# Wednesday 4 November 2026, 12:00 IST; the meeting is Thursday 10:00-12:00 IST.
NOW = datetime(2026, 11, 4, 6, 30, tzinfo=timezone.utc)
START = datetime(2026, 11, 5, 4, 30, tzinfo=timezone.utc)


def run(coro):
    return asyncio.run(coro)


def _config():
    config = make_config()
    offer = config.product.offer
    offer.meeting_window, offer.meeting_timezone, offer.meeting_link = "10:00-12:00", "Asia/Kolkata", MEET
    return config


@pytest.fixture
def state(tmp_path):
    s = StateManager(str(tmp_path / "h.db"))
    run(s.init_db())
    return s


def _linkedin_meeting(state, booked_by_harvey=True, offered=True):
    lead, _ = run(state.li_upsert_lead("jane-doe-1", name="Jane Doe", first_name="Jane", stage="meeting",
                                       headline="Founder at Example Goods", company="Example Goods",
                                       profile_url="https://www.linkedin.com/in/jane-doe-1/"))
    run(state.li_add_message(lead, body="Jane, your product pages load slowly.", status="sent",
                             sent_at=NOW - timedelta(days=2)))
    run(state.li_add_message(lead, direction="in", body="Interesting, can we talk?", status="sent",
                             sent_at=NOW - timedelta(days=1)))
    run(state.set_setting(PROFILE_KEY + lead, json.dumps(
        {"at": NOW.isoformat(), "text": "Jane Doe\nFounder at Example Goods\nLondon, England, United Kingdom",
         "location": "London, England, United Kingdom", "headline": "Founder at Example Goods"})))
    if offered:
        run(state.set_setting(OFFERED_KEY + f"linkedin:{lead}", json.dumps(
            {"text": "x", "start": START.isoformat(), "end": (START + timedelta(hours=2)).isoformat(),
             "their_zone": "Europe/London"})))
    if booked_by_harvey:
        run(state.set_setting(meetings.BOOKED_KEY + f"linkedin:{lead}", (NOW - timedelta(hours=3)).isoformat()))
    return lead


def test_a_booked_linkedin_meeting_reads_in_their_time_and_yours(state):
    _linkedin_meeting(state)
    data = run(meetings.collect(state, _config(), now=NOW))
    [m] = data["upcoming"]
    assert m["when"]["theirs"] == "Thursday 5 November, 4:30 to 6:30 AM London time"
    assert m["when"]["ours"] == "Thursday 5 November, 10 AM to 12 PM IST"
    assert m["booked_by"] == "harvey" and m["their_zone_name"] == "London time"
    assert [x["direction"] for x in m["thread"]] == ["out", "in"]
    assert m["where"]["profile_url"].endswith("/in/jane-doe-1/") and data["meet_link"] == MEET


def test_a_meeting_you_marked_has_no_time_until_you_set_one(state):
    lead = _linkedin_meeting(state, booked_by_harvey=False, offered=False)
    config = _config()
    [m] = run(meetings.collect(state, config, now=NOW))["upcoming"]
    assert m["when"] is None and m["booked_by"] == "you"
    run(meetings.set_time(state, config, "linkedin", lead, "2026-11-05", "16:30", 30))
    [m] = run(meetings.collect(state, config, now=NOW))["upcoming"]
    assert m["when"]["ours"] == "Thursday 5 November, 4:30 to 5 PM IST"
    assert m["when"]["theirs"] == "Thursday 5 November, 11 to 11:30 AM London time"
    assert m["time_from"] == "you"


def test_whatsapp_and_email_meetings_are_listed_and_past_ones_move_down(state):
    config = _config()
    contact, _ = run(state.wa_add_contact(phone="+447700900123", stage="meeting", name="Sam Example",
                                          source="website chat"))
    run(state.wa_add_message(contact, direction="in", kind="reply", body="Yes, Thursday works", status="sent",
                             sent_at=(NOW - timedelta(hours=5)).replace(tzinfo=None).isoformat()))
    company_id = run(state.add_company(Company(name="Example Shop", domain="example-shop.test",
                                               location="Austin, Texas")))
    prospect_id = run(state.add_prospect(Prospect(first_name="Ray", last_name="Example", email="ray@example-shop.test",
                                                  title="Owner", company_id=company_id, status="meeting")))
    run(state.add_conversation(Conversation(
        id="conv-1", prospect_id=prospect_id,
        thread=[Message(sender="harvey", content="Would Thursday suit?"),
                Message(sender="prospect", content="Yes please")])))
    run(state.set_setting(meetings.TIME_KEY + f"email:{prospect_id}", json.dumps(
        {"start": (NOW - timedelta(days=3)).isoformat(), "minutes": 30})))
    data = run(meetings.collect(state, config, now=NOW))
    assert [m["channel"] for m in data["upcoming"]] == ["whatsapp"]
    assert [m["channel"] for m in data["earlier"]] == ["email"]
    email = data["earlier"][0]
    assert email["name"] == "Ray Example" and email["company"] == "Example Shop"
    assert [x["body"] for x in email["thread"]] == ["Would Thursday suit?", "Yes please"]


def test_a_meeting_being_arranged_shows_under_getting_close_until_booked(state):
    config = _config()
    lead, _ = run(state.li_upsert_lead("bo-ma-2", name="Bo Ma", stage="warm"))
    run(state.log_action("meeting_alert", "alerts", {"channel": "LinkedIn", "key": f"linkedin:{lead}",
                                                     "name": "Bo Ma", "when": "next week", "service": "Shopify"}))
    data = run(meetings.collect(state, config))
    assert [m["key"] for m in data["close"]] == [f"linkedin:{lead}"]
    assert data["close"][0]["alert"]["when"] == "next week"
    assert run(meetings.mark_booked(state, "linkedin", lead))
    data = run(meetings.collect(state, config))
    assert data["close"] == [] and data["upcoming"][0]["booked_by"] == "you"


class Brain:
    def __init__(self):
        self.prompts = []

    async def think(self, prompt, **kw):
        self.prompts.append(prompt)
        return "Who: **a founder**.\nAgenda: one, two, three."

    def load_skill(self, name):
        return ""


def test_the_brief_reads_the_conversation_and_is_kept_until_it_changes(state):
    config = _config()
    lead = _linkedin_meeting(state)
    brain = Brain()
    first = run(meetings.brief(state, brain, config, "linkedin", lead))
    assert first["text"] == "Who: a founder.\nAgenda: one, two, three."       # no markdown
    assert "<untrusted" in brain.prompts[0] and "Interesting, can we talk?" in brain.prompts[0]
    run(meetings.brief(state, brain, config, "linkedin", lead))
    assert len(brain.prompts) == 1                                           # kept
    run(state.li_add_message(lead, direction="in", body="Can we move it to 11?", status="sent",
                             sent_at=NOW.replace(tzinfo=None)))
    [m] = run(meetings.collect(state, config, now=NOW))["upcoming"]
    assert m["brief"]["stale"] is True
    run(meetings.brief(state, brain, config, "linkedin", lead))
    assert len(brain.prompts) == 2


# ── The routes ──

@pytest.fixture
def web(client, tmp_path, monkeypatch):  # noqa: F811
    path = tmp_path / "harvey.yaml"
    cfg = {**MINIMAL, "product": {**MINIMAL["product"], "offer": {
        "meeting_window": "10:00-12:00", "meeting_timezone": "Asia/Kolkata", "meeting_link": MEET}}}
    path.write_text(yaml.safe_dump(cfg))
    monkeypatch.setattr(dash, "_resolve_config_file", lambda: path)
    state = StateManager(str(client.db_path))
    run(state.init_db())
    client.state = state
    return client


def test_the_tab_lists_meetings_and_sets_a_time(web):
    lead = _linkedin_meeting(web.state, booked_by_harvey=False, offered=False)
    data = web.get("/api/meetings").json()
    assert len(data["upcoming"]) == 1 and data["owner_tz"] == "Asia/Kolkata"
    res = web.post(f"/api/meetings/linkedin/{lead}/time", json={"date": "2030-01-07", "time": "10:00", "minutes": 45}).json()
    assert res["success"] and res["item"]["when"]["ours"] == "Monday 7 January, 10 to 10:45 AM IST"
    assert web.post(f"/api/meetings/linkedin/{lead}/time", json={"date": "soon", "time": "x"}).status_code == 400
    assert web.post("/api/meetings/fax/1/booked").status_code == 400


def test_the_tab_needs_the_token(web):
    with TestClient(dash.app, base_url="http://127.0.0.1:5555") as anonymous:
        assert anonymous.get("/api/meetings").status_code == 401


def test_the_tab_is_on_the_page(web):
    html = web.get("/").text
    assert "showTab('meetings'" in html
    for element in ('id="mt-calendar"', 'id="mt-agenda"', 'id="mt-close"', 'id="mt-unset"',
                    '<dialog class="mt-dialog" id="mt-dialog"', 'data-view="month"', 'data-view="list"'):
        assert element in html
