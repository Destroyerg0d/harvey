"""Over the Claude limit: writing and prospecting wait, but replies are read and
what's approved still goes. Only when the quota is nearly out does it all stop."""

import pytest

from harvey.integrations.quota import QuotaClient
from tests.test_codex_e2e import Harness, _approved_item
from tests.test_gmail_inbox import message, provider


def at_percent(monkeypatch, percent):
    async def reading(_self):
        return {"seven_day": {"utilization": percent, "resets_at": "2099-01-01T00:00:00+00:00"}}
    monkeypatch.setattr(QuotaClient, "get_utilization", reading)




@pytest.mark.asyncio
async def test_over_the_limit_what_is_approved_still_goes_and_scout_waits(monkeypatch, tmp_path):
    h = Harness(monkeypatch, tmp_path, provider="claude", model="")
    await h.state.init_db()
    await _approved_item(h.state)
    at_percent(monkeypatch, 92)
    await h.run_cycles(1)
    assert [m["to"] for m in h.mail.sent] == ["bob@beta.com"]
    assert h.scout_runs == 0


@pytest.mark.asyncio
async def test_over_the_limit_replies_are_still_read(monkeypatch, tmp_path):
    h = Harness(monkeypatch, tmp_path, provider="claude", model="")
    await h.state.init_db()
    await _approved_item(h.state)
    gmail = provider([
        {"messages": [{"id": "stop"}]}, message("stop", "bob@beta.com", "unsubscribe"),
    ])
    h.mail.get_replies = gmail.get_replies
    at_percent(monkeypatch, 92)
    await h.run_cycles(1)
    assert await h.state.is_reply_processed("stop")
    assert h.mail.sent == []     # they opted out: the approved email was cancelled, not sent


@pytest.mark.asyncio
async def test_nearly_out_everything_sleeps(monkeypatch, tmp_path):
    h = Harness(monkeypatch, tmp_path, provider="claude", model="")
    await h.state.init_db()
    await _approved_item(h.state)
    at_percent(monkeypatch, 99)
    await h.run_cycles(1)
    assert h.mail.sent == [] and h.scout_runs == 0


@pytest.mark.asyncio
async def test_under_the_limit_is_unchanged(monkeypatch, tmp_path):
    h = Harness(monkeypatch, tmp_path, provider="claude", model="")
    await h.state.init_db()
    await _approved_item(h.state)
    at_percent(monkeypatch, 40)
    await h.run_cycles(1)
    assert [m["to"] for m in h.mail.sent] == ["bob@beta.com"]
