"""Gmail with more than one mailbox: each sends and reads as itself, on its
own token, and never as the wrong account. Without a mailbox the provider
is the single persona mailbox, exactly as before."""

import base64
import email
import http.client
import json
import socket
import threading
import urllib.parse
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

import harvey.integrations.gmail as gmail
import harvey.mailboxes as mb
from tests.test_gmail_inbox import message

PRIMARY = "primary@sender-one.test"
SECOND = "second@sender-one.test"
THIRD = "third@sender-two.test"
ENV = SimpleNamespace(gmail_client_id="i", gmail_client_secret="s")
# The real clients, before any test swaps them for a scripted transport.
REAL_ASYNC_CLIENT = httpx.AsyncClient
REAL_CLIENT = httpx.Client


def persona_config(address=PRIMARY):
    return SimpleNamespace(persona=SimpleNamespace(name="Alex Example", email=address))


def box(address=SECOND, name="Sam Sample", token_path=None):
    return mb.Mailbox(email=address, name=name, daily_limit=10,
                      token_path=token_path or Path("no-such-token.json"),
                      is_primary=address == PRIMARY)


def scripted(monkeypatch, *, profile=SECOND, send_status=200, mailbox=None,
             config=None, real_token=False):
    """A provider whose Gmail API answers from a script; ``calls`` records
    every request it made, as (method, path)."""
    calls = []
    profiles = list(profile) if isinstance(profile, list) else None

    def respond(request):
        path = request.url.path.removeprefix("/gmail/v1/users/me")
        calls.append((request.method, path))
        if path == "/profile":
            answer = profiles.pop(0) if profiles is not None else profile
            if isinstance(answer, int):
                return httpx.Response(answer, json={})
            return httpx.Response(200, json={"emailAddress": answer, "historyId": "7"})
        if path == "/messages/send":
            calls[-1] = (request.method, path, request.content)
            return httpx.Response(send_status, json={"id": "gmail-id", "threadId": "thread-id"})
        if path == "/messages" and request.method == "GET":
            return httpx.Response(200, json={"messages": [{"id": "m1"}]})
        if path == "/messages/m1":
            return httpx.Response(200, json=message("m1"))
        return httpx.Response(200, json={"payload": {"headers": [
            {"name": "Message-ID", "value": "<rfc@sender-one.test>"}]}})

    transport = httpx.MockTransport(respond)
    monkeypatch.setattr(gmail.httpx, "AsyncClient",
                        lambda **kw: REAL_ASYNC_CLIENT(transport=transport, **kw))
    p = gmail.GmailProvider(config or persona_config(), ENV, mailbox=mailbox)
    if not real_token:
        p._token = AsyncMock(return_value="synthetic-token")
    return p, calls


def sent_header(calls, name):
    [content] = [c[2] for c in calls if c[1] == "/messages/send"]
    raw = base64.urlsafe_b64decode(json.loads(content)["raw"])
    return email.message_from_bytes(raw)[name]


def sends(calls):
    return [c for c in calls if c[1] == "/messages/send"]


# ── From, token, identity ──

@pytest.mark.asyncio
async def test_a_mailbox_sends_from_its_own_name_and_address(monkeypatch):
    p, calls = scripted(monkeypatch, mailbox=box())
    result = await p.send_email("lead@prospect.test", "Hi", "Hello there")
    assert result.ok and result.status == 200 and result.attempted
    assert sent_header(calls, "From") == f"Sam Sample <{SECOND}>"
    assert calls[0] == ("GET", "/profile")      # checked before the send


@pytest.mark.asyncio
async def test_every_email_offers_an_unsubscribe_to_its_own_mailbox(monkeypatch):
    # Mail clients show an Unsubscribe link for the header, so a recipient
    # who is done can say so rather than marking the email as spam.
    p, calls = scripted(monkeypatch, mailbox=box())
    await p.send_email("lead@prospect.test", "Hi", "Hello there")
    assert sent_header(calls, "List-Unsubscribe") == f"<mailto:{SECOND}?subject=unsubscribe>"


@pytest.mark.asyncio
async def test_without_a_mailbox_the_persona_sends_as_before(monkeypatch):
    p, calls = scripted(monkeypatch, profile=THIRD)
    result = await p.send_email("lead@prospect.test", "Hi", "Hello there")
    assert result.ok
    assert sent_header(calls, "From") == f"Alex Example <{PRIMARY}>"
    assert ("GET", "/profile") not in calls     # no identity check in single mode
    assert p.unavailable_reason == ""


@pytest.mark.asyncio
async def test_a_mailbox_signed_in_as_another_account_never_sends_or_reads(monkeypatch):
    p, calls = scripted(monkeypatch, profile=THIRD, mailbox=box())
    for _ in range(2):
        result = await p.send_email("lead@prospect.test", "Hi", "Hello there")
        assert not result.ok and not result.attempted
        assert THIRD in result.error and SECOND in result.error
    assert sends(calls) == []
    assert calls == [("GET", "/profile")]       # asked once, remembered
    assert SECOND in p.unavailable_reason and THIRD in p.unavailable_reason
    assert f"harvey gmail auth --mailbox {SECOND}" in p.unavailable_reason
    with pytest.raises(gmail.GmailError) as e:
        await p.get_replies()
    assert THIRD in str(e.value)
    assert calls == [("GET", "/profile")]       # no inbox read either


@pytest.mark.asyncio
async def test_the_identity_is_checked_once_per_provider(monkeypatch):
    p, calls = scripted(monkeypatch, profile=SECOND.upper(), mailbox=box())
    assert (await p.send_email("a@prospect.test", "Hi", "One")).ok
    assert (await p.send_email("b@prospect.test", "Hi", "Two")).ok
    assert calls.count(("GET", "/profile")) == 1
    assert len(sends(calls)) == 2


@pytest.mark.asyncio
async def test_an_unanswered_identity_check_is_asked_again(monkeypatch):
    p, calls = scripted(monkeypatch, profile=[503, SECOND], mailbox=box())
    first = await p.send_email("lead@prospect.test", "Hi", "Hello")
    assert not first.ok and not first.attempted and first.status == 503
    assert p.unavailable_reason == ""           # not known to be wrong
    assert sends(calls) == []
    assert (await p.send_email("lead@prospect.test", "Hi", "Hello")).ok
    assert calls.count(("GET", "/profile")) == 2


@pytest.mark.asyncio
async def test_a_mailbox_without_a_token_makes_no_call(monkeypatch, tmp_path):
    p, calls = scripted(monkeypatch, mailbox=box(token_path=tmp_path / "none" / "t.json"),
                        real_token=True)
    assert not p.is_configured()
    result = await p.send_email("lead@prospect.test", "Hi", "Hello")
    assert (result.ok, result.attempted, result.status) == (False, False, 0)
    assert f"harvey gmail auth --mailbox {SECOND}" in result.error
    assert calls == []
    with pytest.raises(gmail.GmailError) as e:
        await p.get_replies()
    assert f"--mailbox {SECOND}" in str(e.value)


@pytest.mark.asyncio
async def test_a_mailbox_refreshes_with_its_own_token(monkeypatch, tmp_path):
    token_path = tmp_path / f"{SECOND}.json"
    token_path.write_text(json.dumps({"refresh_token": "second-refresh"}), encoding="utf-8")
    # The primary's token must not be read for this mailbox.
    monkeypatch.setattr(gmail, "TOKEN_FILE", tmp_path / "primary.json")
    seen = []

    def respond(request):
        seen.append(urllib.parse.parse_qs(request.content.decode())["refresh_token"])
        return httpx.Response(200, json={"access_token": "a", "expires_in": 3600})
    monkeypatch.setattr(gmail.httpx, "AsyncClient", lambda **kw: REAL_ASYNC_CLIENT(
        transport=httpx.MockTransport(respond), **kw))
    p = gmail.GmailProvider(persona_config(), ENV, mailbox=box(token_path=token_path))
    assert p.is_configured()
    assert await p._token() == "a"
    assert seen == [["second-refresh"]]


@pytest.mark.asyncio
async def test_single_mode_without_a_token_is_not_attempted(monkeypatch):
    # The zero-argument stand-in other tests use must keep working.
    monkeypatch.setattr(gmail, "_load_token", lambda: None)
    p, calls = scripted(monkeypatch, real_token=True)
    result = await p.send_email("lead@prospect.test", "Hi", "Hello")
    assert (result.ok, result.attempted, result.status, result.error) == (
        False, False, 0, "gmail send failed")
    assert calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [400, 401, 403, 429])
async def test_a_refused_send_carries_its_http_status(monkeypatch, status):
    p, calls = scripted(monkeypatch, send_status=status, mailbox=box())
    result = await p.send_email("lead@prospect.test", "Hi", "Hello")
    assert (result.ok, result.attempted, result.status) == (False, True, status)
    assert p.last_status == status


@pytest.mark.asyncio
async def test_replies_are_marked_with_the_mailbox_they_arrived_in(monkeypatch):
    p, calls = scripted(monkeypatch, mailbox=box())
    [reply] = await p.get_replies()
    assert reply.mailbox == SECOND and reply.from_email == "person@example.test"
    single, _ = scripted(monkeypatch)
    [reply] = await single.get_replies()
    assert reply.mailbox == ""


# ── test_connection ──

@pytest.mark.asyncio
async def test_connection_names_the_wrong_account(monkeypatch, tmp_path):
    token_path = tmp_path / "t.json"
    token_path.write_text(json.dumps({"refresh_token": "r"}), encoding="utf-8")
    p, _ = scripted(monkeypatch, profile=THIRD, mailbox=box(token_path=token_path))
    assert await p.test_connection() == (False, (
        f"Signed in as {THIRD}, but this mailbox is {SECOND}. "
        f"Run harvey gmail auth --mailbox {SECOND} again."))
    p, _ = scripted(monkeypatch, profile=SECOND, mailbox=box(token_path=token_path))
    assert await p.test_connection() == (True, f"Connected as {SECOND}")


@pytest.mark.asyncio
async def test_single_mode_connection_compares_persona_email(monkeypatch):
    monkeypatch.setattr(gmail, "_load_token", lambda: {"refresh_token": "r"})
    p, _ = scripted(monkeypatch, profile=THIRD)
    ok, detail = await p.test_connection()
    assert not ok and detail.startswith(f"Signed in as {THIRD}, but this mailbox is {PRIMARY}.")
    p, _ = scripted(monkeypatch, profile=THIRD, config=persona_config(""))
    assert await p.test_connection() == (True, f"Connected as {THIRD}")


@pytest.mark.asyncio
async def test_connection_without_a_mailbox_token_says_how_to_sign_in(monkeypatch, tmp_path):
    p, calls = scripted(monkeypatch, mailbox=box(token_path=tmp_path / "missing.json"))
    ok, detail = await p.test_connection()
    assert not ok and f"harvey gmail auth --mailbox {SECOND}" in detail
    assert calls == []


# ── Token files ──

@pytest.fixture
def token_dirs(monkeypatch, tmp_path):
    restricted = []
    monkeypatch.setattr(gmail, "TOKEN_FILE", tmp_path / "data" / "gmail_token.json")
    monkeypatch.setattr(gmail, "TOKENS_DIR", tmp_path / "data" / "gmail_tokens")
    monkeypatch.setattr(gmail, "_restrict_folder", lambda folder: restricted.append(folder))
    return restricted


def leftovers(folder: Path):
    return [p.name for p in folder.iterdir() if p.name.startswith(".gmail-token-")]


def test_save_and_load_default_to_the_token_file_now(token_dirs):
    gmail._save_token({"refresh_token": "one"})
    assert json.loads(gmail.TOKEN_FILE.read_text(encoding="utf-8")) == {"refresh_token": "one"}
    assert gmail._load_token() == {"refresh_token": "one"}
    assert token_dirs == []                     # data/ itself is never locked down
    assert leftovers(gmail.TOKEN_FILE.parent) == []


def test_a_mailbox_token_goes_in_the_locked_folder(token_dirs):
    path = mb.token_path_for(SECOND, PRIMARY)
    assert path == gmail.TOKENS_DIR / f"{SECOND}.json"
    assert not path.parent.exists()
    gmail._save_token({"refresh_token": "two"}, path)
    assert gmail._load_token(path) == {"refresh_token": "two"}
    assert gmail._load_token() is None          # the primary's file is untouched
    assert token_dirs == [gmail.TOKENS_DIR]
    gmail._save_token({"refresh_token": "three"}, path)
    assert gmail._load_token(path) == {"refresh_token": "three"}
    assert leftovers(path.parent) == []


def test_a_failed_save_keeps_the_old_token(token_dirs, monkeypatch):
    path = gmail.TOKENS_DIR / f"{SECOND}.json"
    gmail._save_token({"refresh_token": "good"}, path)

    def broken(*args):
        raise OSError("disk full")
    monkeypatch.setattr(gmail.os, "replace", broken)
    with pytest.raises(OSError):
        gmail._save_token({"refresh_token": "new"}, path)
    assert gmail._load_token(path) == {"refresh_token": "good"}
    assert leftovers(path.parent) == []


@pytest.mark.skipif(gmail.os.name == "nt", reason="POSIX file modes")
def test_a_saved_token_is_private(token_dirs):
    path = gmail.TOKENS_DIR / f"{SECOND}.json"
    gmail._save_token({"refresh_token": "r"}, path)
    assert path.stat().st_mode & 0o777 == 0o600


# ── Sign-in ──

def test_the_consent_page_asks_for_the_expected_account():
    query = urllib.parse.parse_qs(urllib.parse.urlparse(
        gmail._auth_url("cid", "http://localhost:1", "st", " Second@Sender-One.test")).query)
    assert query["login_hint"] == [SECOND]
    assert query["hd"] == ["sender-one.test"]
    assert query["prompt"] == ["consent select_account"]
    assert query["access_type"] == ["offline"] and query["state"] == ["st"]
    plain = urllib.parse.parse_qs(urllib.parse.urlparse(
        gmail._auth_url("cid", "http://localhost:1", "st")).query)
    assert "login_hint" not in plain and "hd" not in plain
    assert plain["prompt"] == ["consent select_account"]
    consumer = urllib.parse.parse_qs(urllib.parse.urlparse(
        gmail._auth_url("cid", "http://localhost:1", "st", "example.user@gmail.com")).query)
    assert consumer["login_hint"] == ["example.user@gmail.com"] and "hd" not in consumer


def google(monkeypatch, *, signed_in=SECOND, profile_status=200):
    """Google's token, profile and revoke endpoints, for the sign-in."""
    calls = []

    def respond(request):
        body = urllib.parse.parse_qs(request.content.decode()) if request.content else {}
        calls.append((request.method, request.url.host, request.url.path, body))
        if request.url.path == "/token":
            return httpx.Response(200, json={"access_token": "new-access",
                                             "refresh_token": "new-refresh", "scope": gmail.SCOPE})
        if request.url.path.endswith("/profile"):
            assert request.headers["Authorization"] == "Bearer new-access"
            return httpx.Response(profile_status, json={"emailAddress": signed_in})
        if request.url.path == "/revoke":
            return httpx.Response(200)
        return httpx.Response(404)
    monkeypatch.setattr(gmail.httpx, "Client", lambda **kw: REAL_CLIENT(
        transport=httpx.MockTransport(respond), **kw))
    return calls


def revoked(calls):
    return [c[3].get("token") for c in calls if c[2] == "/revoke"]


def test_a_sign_in_as_the_wrong_account_is_revoked_and_not_saved(monkeypatch, token_dirs, capsys):
    calls = google(monkeypatch, signed_in=THIRD)
    path = gmail.TOKENS_DIR / f"{SECOND}.json"
    assert not gmail._finish_auth("cid", "secret", "code", "http://localhost:1",
                                  token_path=path, expected_email=SECOND)
    assert not path.exists() and not gmail.TOKEN_FILE.exists()
    assert revoked(calls) == [["new-refresh"]]
    out = capsys.readouterr().out
    assert THIRD in out and SECOND in out and "new-refresh" not in out


def test_an_unconfirmed_sign_in_is_not_saved_either(monkeypatch, token_dirs):
    calls = google(monkeypatch, profile_status=500)
    path = gmail.TOKENS_DIR / f"{SECOND}.json"
    assert not gmail._finish_auth("cid", "secret", "code", "http://localhost:1",
                                  token_path=path, expected_email=SECOND)
    assert not path.exists()
    assert revoked(calls) == [["new-refresh"]]


def test_the_right_account_is_saved_where_asked(monkeypatch, token_dirs):
    calls = google(monkeypatch, signed_in=SECOND.upper())
    path = gmail.TOKENS_DIR / f"{SECOND}.json"
    assert gmail._finish_auth("cid", "secret", "code", "http://localhost:1",
                              token_path=path, expected_email=SECOND)
    assert gmail._load_token(path)["refresh_token"] == "new-refresh"
    assert not gmail.TOKEN_FILE.exists() and revoked(calls) == []


def test_with_no_expected_account_the_sign_in_is_saved_as_before(monkeypatch, token_dirs):
    google(monkeypatch, profile_status=500)
    assert gmail._finish_auth("cid", "secret", "code", "http://localhost:1")
    assert gmail._load_token()["refresh_token"] == "new-refresh"


def test_the_whole_sign_in_round_trip_refuses_the_wrong_account(monkeypatch, token_dirs):
    """Drives run_auth_flow's loopback server the way the browser would."""
    calls = google(monkeypatch, signed_in=THIRD)
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    monkeypatch.setattr(gmail, "LOOPBACK_PORT", port)
    opened = []

    def browser(url):
        opened.append(url)
        state = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)["state"][0]

        def callback():
            conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
            conn.request("GET", f"/?state={state}&code=the-code")
            conn.getresponse().read()
            conn.close()
        threading.Thread(target=callback, daemon=True).start()
        return True
    monkeypatch.setattr(gmail.webbrowser, "open", browser)
    path = gmail.TOKENS_DIR / f"{SECOND}.json"
    assert not gmail.run_auth_flow("cid", "secret", token_path=path, expected_email=SECOND)
    assert "login_hint=second%40sender-one.test" in opened[0]
    assert not path.exists()
    exchange = [c for c in calls if c[2] == "/token"]
    assert exchange[0][3]["code"] == ["the-code"]
    assert revoked(calls) == [["new-refresh"]]
