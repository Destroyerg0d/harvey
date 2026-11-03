"""The dashboard's Learning tab: /api/lessons, /api/lessons/status,
/api/learning and the Today card. All data is fictional."""

import asyncio
import json
import tempfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import harvey.dashboard as dash
from harvey import lessons
from harvey.state import StateManager

WEB = Path(__file__).resolve().parent.parent / "harvey" / "web"


@pytest.fixture(autouse=True)
def _fresh_registry():
    saved = dict(lessons._registry)
    yield
    lessons._registry.clear()
    lessons._registry.update(saved)


@pytest.fixture
def client(monkeypatch):
    with tempfile.TemporaryDirectory() as tmp:
        db = Path(tmp) / "harvey.db"
        monkeypatch.setattr(dash, "DB_PATH", db)
        monkeypatch.setattr(dash, "WEB_DIR", WEB)
        dash.token_store.configure(Path(tmp) / "data" / "dashboard-token")
        token = dash.token_store.get()
        with TestClient(dash.app, base_url="http://127.0.0.1:5555",
                        headers={"Authorization": f"Bearer {token}"}) as c:
            c.db_path = db
            yield c


def _run(coro):
    return asyncio.run(coro)


def _seed(db_path, *items):
    async def go():
        state = StateManager(str(db_path))
        await state.init_db()
        ids = []
        for kind, text, evidence in items:
            lesson_id, _ = await lessons.propose(state, kind, text, evidence)
            ids.append(lesson_id)
        return ids
    return _run(go())


def test_lessons_route_lists_proposals_active_lessons_and_the_minimums(client):
    a, b = _seed(client.db_path,
                 ("writing", "Open with the tool you saw on their site.", {"sent": 140}),
                 ("linkedin", "On LinkedIn, ask about their shop first.", {"sent": 90}))
    _run(lessons.set_status(StateManager(str(client.db_path)), [b], "active"))
    data = client.get("/api/lessons").json()
    assert [l["id"] for l in data["proposed"]] == [a]
    assert [l["id"] for l in data["active"]] == [b]
    assert data["proposed"][0]["evidence"] == {"sent": 140}
    assert data["summary"]["proposed"] == 1 and data["summary"]["active"] == 1
    assert data["minimums"]["sends_per_side"] == 40 and data["minimums"]["segment_sends"] == 100
    assert data["last_run"] is None


def test_status_route_approves_rejects_and_retires_along_the_lifecycle(client):
    a, b, c = _seed(client.db_path,
                    ("writing", "Open with the tool you saw on their site.", {}),
                    ("writing", "Keep the follow-up to a couple of lines.", {}),
                    ("subject", "Keep subject lines to a few plain lowercase words.", {}))
    r = client.post("/api/lessons/status", json={"ids": [a], "status": "active"}).json()
    assert r["success"] and r["changed"] == [a]
    # Approve all: only the undecided ones change.
    r = client.post("/api/lessons/status", json={"ids": [a, b], "status": "active"}).json()
    assert r["changed"] == [b] and a in r["refused"]
    r = client.post("/api/lessons/status", json={"ids": [c], "status": "rejected"}).json()
    assert r["changed"] == [c]
    r = client.post("/api/lessons/status", json={"ids": [a], "status": "retired"}).json()
    assert r["changed"] == [a]
    summary = client.get("/api/lessons").json()["summary"]
    assert summary == {"proposed": 0, "active": 1, "rejected": 1, "retired": 1}


def test_status_route_refuses_bad_requests(client):
    assert client.post("/api/lessons/status", json={"ids": ["x"], "status": "proposed"}).status_code == 400
    assert client.post("/api/lessons/status", json={"ids": [], "status": "active"}).status_code == 400
    assert client.post("/api/lessons/status", json={"ids": [1], "status": "active"}).status_code == 400


def test_approving_a_segment_mix_lesson_from_the_dashboard_sets_the_weights(client):
    [mix] = _seed(client.db_path, ("segment_mix", "Send more first emails to old custom sites.",
                                   {"weights": {"custom": 0.4, "wordpress": 0.35, "shopify": 0.25}}))
    r = client.post("/api/lessons/status", json={"ids": [mix], "status": "active"}).json()
    assert r["segment_weights"] == {"custom": 0.4, "wordpress": 0.35, "shopify": 0.25}
    assert client.get("/api/lessons").json()["segment_weights"] == r["segment_weights"]


def test_learning_route_reads_the_analysts_table_and_filters_it(client):
    _seed(client.db_path)
    rows = [
        {"channel": "email", "segment": "shopify", "angle": "tools", "step": 1, "mailbox": "primary",
         "country": "US", "sent": 50, "human_replies": 2, "positive": 1, "not_interested": 0,
         "opt_outs": 0, "bounces": 1},
        {"channel": "email", "segment": "custom", "angle": "tools", "step": 2, "mailbox": "primary",
         "country": "GB", "sent": 30, "human_replies": 1, "positive": 0, "not_interested": 1,
         "opt_outs": 0, "bounces": 0},
    ]
    (client.db_path.parent / "outcomes.json").write_text(json.dumps(
        {"generated_at": "2026-11-04T06:00:00", "days": 30, "rows": rows}), encoding="utf-8")
    data = client.get("/api/learning").json()
    assert data["source"] == "analyst" and len(data["rows"]) == 2
    assert data["options"]["segments"] == ["custom", "shopify"] and data["options"]["steps"] == ["1", "2"]
    assert data["totals"]["sent"] == 80 and data["segment_sends"] == {"shopify": 50, "custom": 30}
    one = client.get("/api/learning?segment=custom&step=2").json()
    assert [r["segment"] for r in one["rows"]] == ["custom"] and one["totals"]["sent"] == 30
    assert client.get("/api/learning?channel=linkedin").json()["rows"] == []
    assert data["subject_variants"] == [] and data["minimums"]["human_replies"] == 3


def test_learning_route_builds_the_table_when_the_analyst_hasnt(client):
    _seed(client.db_path)
    data = client.get("/api/learning").json()
    assert data["source"] == "live" and data["rows"] == [] and "error" not in data


def test_today_has_a_learning_card_while_proposals_wait(client):
    _seed(client.db_path, ("writing", "Open with the tool you saw on their site.", {}))
    data = client.get("/api/today").json()
    card = next(i for i in data["items"] if i["key"] == "lessons")
    assert card["tab"] == "learning" and data["stats"]["lessons_proposed"] == 1


def test_the_learning_tab_is_wired_into_the_page():
    html = (WEB / "index.html").read_text(encoding="utf-8")
    js = (WEB / "app.js").read_text(encoding="utf-8")
    assert "showTab('learning', this)" in html and 'id="learning" class="section"' in html
    for el in ("lrn-proposals", "lrn-active", "lrn-outcomes", "lrn-variants", "lrn-mix", "lrn-run"):
        assert f'id="{el}"' in html
    assert "case 'learning': loadLearning(); break;" in js
    assert "/api/lessons/status" in js and "currentTab === 'learning') loadLearning()" in js
