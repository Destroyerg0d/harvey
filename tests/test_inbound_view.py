"""The Website leads tab: whether the ERP is being read, and what Harvey did."""

import json
from datetime import datetime, timedelta
from types import SimpleNamespace

import httpx
import pytest

from harvey import inbound as I
from harvey import inbound_view as V
from harvey.collectors.erp_chat import ERPChatClient, ERPChatError
from tests.test_inbound import Brain, config, lead, pipeline, state, verified  # noqa: F401

NOW = datetime(2026, 11, 8, 12, 0)
ENV = SimpleNamespace(erp_supabase_url="https://erp.example", erp_supabase_key="key")


@pytest.mark.asyncio
async def test_a_recent_read_is_ok(state):
    await state.set_setting(V.LAST_RUN_KEY, json.dumps({"at": (NOW - timedelta(minutes=4)).isoformat(),
                                                       "read": 3, "drafted": 1}))
    c = await V.connection(state, config(enabled=True, poll_minutes=5), ENV, now=NOW)
    assert c["ok"] and c["why"] == ""


@pytest.mark.asyncio
async def test_a_late_read_a_failed_read_and_no_keys_are_not(state):
    cfg = config(enabled=True, poll_minutes=5)
    await state.set_setting(V.LAST_RUN_KEY, json.dumps({"at": (NOW - timedelta(minutes=40)).isoformat()}))
    late = await V.connection(state, cfg, ENV, now=NOW)
    assert not late["ok"] and "Is Harvey running" in late["why"]
    await state.set_setting(V.LAST_ERROR_KEY, json.dumps({"at": (NOW - timedelta(minutes=1)).isoformat(),
                                                         "error": "couldn't read the chat: HTTP 401"}))
    failed = await V.connection(state, cfg, ENV, now=NOW)
    assert not failed["ok"] and "HTTP 401" in failed["why"]
    none = await V.connection(state, cfg, SimpleNamespace(erp_supabase_url="", erp_supabase_key=""), now=NOW)
    assert not none["ok"] and "ERP_SUPABASE_URL" in none["why"]
    off = await V.connection(state, config(enabled=False), ENV, now=NOW)
    assert not off["ok"] and "switched off" in off["why"]


@pytest.mark.asyncio
async def test_every_lead_read_is_listed_with_what_harvey_did(state):
    await pipeline(state, [lead()]).run()
    rows = await V.leads(state)
    assert len(rows) == 1
    row = rows[0]
    assert row["status"] == I.DRAFTED and row["status_words"] == "Email written"
    assert row["outbox_status"] == "pending_review" and row["subject"]


@pytest.mark.asyncio
async def test_a_failed_poll_is_kept_and_a_good_one_clears_it(state):
    class Failing:
        async def poll(self):
            raise ERPChatError("crm_chat_threads answered HTTP 503")

    runner = I.InboundRunner(Brain(), state, config(enabled=True), ENV, pipeline_factory=lambda: Failing())
    assert await runner.poll_once() is None
    assert "HTTP 503" in json.loads(await state.get_setting(I.LAST_ERROR_KEY))["error"]

    class Working:
        async def poll(self):
            return {"status": "ok"}

    runner.pipeline_factory = lambda: Working()
    assert await runner.poll_once() == {"status": "ok"}
    assert not await state.get_setting(I.LAST_ERROR_KEY)


@pytest.mark.asyncio
async def test_viewing_a_chat_only_ever_sends_get_requests():
    methods = []

    def answer(request: httpx.Request):
        methods.append(request.method)
        table = request.url.path.rsplit("/", 1)[-1]
        rows = {
            "crm_guest_sessions": [{"session_token": "s1", "name": "Ann Sample", "email": "ann@acme-shop.example",
                                    "phone": "", "source": "{}", "created_at": "2026-11-07T10:00:00"}],
            "crm_chat_threads": [{"id": "t1", "guest_session_id": "s1", "is_guest": True, "has_client_info": True,
                                  "status": "open", "last_message_at": "2026-11-07T10:05:00"}],
            "crm_chat_messages": [{"thread_id": "t1", "sender_type": "guest", "is_ai": False,
                                   "content": "We need a new shop", "created_at": "2026-11-07T10:01:00",
                                   "meta": {}, "message_type": "text"}],
        }.get(table, [])
        return httpx.Response(200, json=rows)

    reader = ERPChatClient("https://erp.example", "key", transport=httpx.MockTransport(answer))
    found = await reader.lead_for("ann@acme-shop.example")
    assert found is not None and found.email == "ann@acme-shop.example"
    assert any("new shop" in line.text for line in found.transcript)
    assert methods and set(methods) == {"GET"}
