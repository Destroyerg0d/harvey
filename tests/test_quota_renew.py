"""An expired CLI token is renewed by `claude auth status` (no model call), and
the usage gauge read again (2026-11-08: a 401 kept Harvey blind all evening)."""

import httpx
import pytest

from harvey.integrations import quota as Q

WINDOWS = {"five_hour": {"utilization": 12, "resets_at": ""},
           "seven_day": {"utilization": 40, "resets_at": ""}}


@pytest.mark.asyncio
async def test_a_401_renews_the_token_once_and_reads_again(monkeypatch):
    answers = [401, 200]
    ran = []

    def handler(request):
        code = answers.pop(0)
        return httpx.Response(code, json=WINDOWS if code == 200 else {})

    real_client = httpx.AsyncClient
    monkeypatch.setattr(Q.httpx, "AsyncClient",
                        lambda **kw: real_client(transport=httpx.MockTransport(handler)))
    monkeypatch.setattr(Q, "get_oauth_token", lambda: "token")

    class Proc:
        async def wait(self):
            return 0

    async def exec_(*cmd, **kw):
        ran.append(cmd[1:])
        return Proc()
    monkeypatch.setattr(Q.asyncio, "create_subprocess_exec", exec_)
    import harvey.claude_runtime as rt
    monkeypatch.setattr(rt, "check_cli", lambda *a, **k: "claude")
    client = Q.QuotaClient()
    result = await client._fetch()
    assert result and result["seven_day"]["utilization"] == 40
    assert ran == [("auth", "status")]
    # Not again within RENEW_EVERY: another 401 is just a failed reading.
    answers[:] = [401]
    assert await client._fetch() is None and ran == [("auth", "status")]
