"""Owner 2026-11-08: autopilot means Harvey runs WhatsApp and LinkedIn by itself.
With autopilot on a channel is on, unless you switched it off; turning autopilot
on from the dashboard switches the channel on too."""

import asyncio
import tempfile
from pathlib import Path

import pytest

from harvey.agents import linkedin_agent, whatsapp_agent
from harvey.state import StateManager
from tests.whatsapp_fakes import make_config


def _state(tmp):
    state = StateManager(str(Path(tmp) / "h.db"))
    asyncio.run(state.init_db())
    return state


@pytest.mark.parametrize("agent", [whatsapp_agent, linkedin_agent])
def test_autopilot_on_means_the_channel_runs(agent):
    with tempfile.TemporaryDirectory() as tmp:
        state = _state(tmp)
        config = make_config(enabled=False)
        assert not asyncio.run(agent.channel_on(state, config))
        asyncio.run(state.set_setting(agent.AUTOPILOT_KEY, "on"))
        assert asyncio.run(agent.channel_on(state, config))
        # Your off switch still wins.
        asyncio.run(state.set_setting(agent.ENABLED_KEY, "off"))
        assert not asyncio.run(agent.channel_on(state, config))


@pytest.mark.parametrize("channel", ["whatsapp", "linkedin"])
def test_turning_autopilot_on_from_the_dashboard_switches_the_channel_on(channel, monkeypatch):
    from tests.test_dashboard_signals import client as client_fixture  # noqa: F401
    import harvey.dashboard as dash
    from fastapi.testclient import TestClient
    agent = whatsapp_agent if channel == "whatsapp" else linkedin_agent
    with tempfile.TemporaryDirectory() as tmp:
        db = Path(tmp) / "harvey.db"
        monkeypatch.setattr(dash, "DB_PATH", db)
        web = __import__(f"harvey.{channel}.web", fromlist=["_state"])
        monkeypatch.setattr(web, "_state", lambda: StateManager(str(db)))
        dash.token_store.configure(Path(tmp) / "token")
        token = dash.token_store.get()
        with TestClient(dash.app, base_url="http://127.0.0.1:5555",
                        headers={"Authorization": f"Bearer {token}"}) as c:
            r = c.post(f"/api/{channel}/autopilot", json={"mode": "autopilot", "confirm": "autopilot"})
            assert r.status_code == 200, r.text
        state = StateManager(str(db))
        assert asyncio.run(state.get_setting(agent.ENABLED_KEY)) == "on"
        assert asyncio.run(state.get_setting(agent.AUTOPILOT_KEY)) == "on"
