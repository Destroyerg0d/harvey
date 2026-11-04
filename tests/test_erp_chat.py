"""The read-only reader for the website chat's tables. Everyone and everything
here is invented (.example domains only), the ERP is an httpx.MockTransport, and
nothing touches the network."""

import json
import logging
import re
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest

from harvey.collectors import erp_chat
from harvey.collectors.erp_chat import (
    ASSISTANT, REP, VISITOR, ERPChatClient, ERPChatError, build_leads, directory_in, parse_place,
    parse_source,
)

TOKEN_A = "guest-token-aaaa-1111"
TOKEN_B = "guest-token-bbbb-2222"
TOKEN_C = "guest-token-cccc-3333"


def thread(tid, token, last, **extra):
    return {"id": tid, "guest_session_id": token, "is_guest": True, "has_client_info": True,
            "status": "active", "ai_stage": "qualifying", "handling_rep_id": None,
            "handling_rep_name": None, "last_message_at": last, "preferred_language": "DE|Bavaria|Munich",
            **extra}


def session(token, email, name="Ann Lee", source=None, converted=False, phone=""):
    return {"session_token": token, "name": name, "email": email, "phone": phone,
            "source": source, "is_converted": converted, "created_at": "2026-10-01T09:00:00+00:00"}


def message(tid, sender, content, at, is_ai=False, meta=None, message_type="text"):
    return {"thread_id": tid, "sender_type": sender, "is_ai": is_ai, "content": content,
            "created_at": at, "meta": meta, "message_type": message_type}


class FakeERP:
    """Enough of PostgREST for the reader: eq, gte and in filters, and Range."""

    def __init__(self, threads=(), sessions=(), messages=()):
        self.tables = {"crm_chat_threads": list(threads), "crm_guest_sessions": list(sessions),
                       "crm_chat_messages": list(messages)}
        self.requests: list[httpx.Request] = []

    @staticmethod
    def _values(raw: str) -> list[str]:
        inner = raw[len("in.("):-1]
        return [a or b for a, b in re.findall(r'"((?:[^"\\]|\\.)*)"|([^,]+)', inner)]

    def _matches(self, row: dict, column: str, condition: str) -> bool:
        if condition.startswith("in."):
            return str(row.get(column)) in self._values(condition)
        if condition.startswith("eq."):
            want = condition[3:]
            return str(row.get(column)).lower() == want.lower()
        if condition.startswith("gte."):
            return erp_chat.parse_time(row.get(column)) >= erp_chat.parse_time(condition[4:])
        raise AssertionError(f"unexpected filter {condition}")

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        parts = urlsplit(str(request.url))
        table = parts.path.rsplit("/", 1)[-1]
        assert parts.path == f"/rest/v1/{table}"
        assert request.headers["apikey"] == "the-key"
        assert request.headers["authorization"] == "Bearer the-key"
        query = {k: v[0] for k, v in parse_qs(parts.query).items()}
        rows = self.tables[table]
        for column, condition in query.items():
            if column in ("select", "order"):
                continue
            rows = [r for r in rows if self._matches(r, column, condition)]
        start, end = (int(x) for x in request.headers["range"].split("-"))
        return httpx.Response(206, json=rows[start:end + 1])


def client_for(erp) -> ERPChatClient:
    return ERPChatClient("https://erp.example/", "the-key", transport=httpx.MockTransport(erp))


# ── The token join ──

@pytest.mark.asyncio
async def test_a_thread_joins_its_session_on_the_session_token():
    erp = FakeERP(
        threads=[thread("t1", TOKEN_A, "2026-10-02T10:00:00+00:00")],
        sessions=[session(TOKEN_A, "Ann@Acme-Shop.example", phone="+1 555 0100")],
        messages=[message("t1", "guest", "We need a new shop built soon", "2026-10-02T09:58:00+00:00")])
    [lead] = await client_for(erp).read_leads()
    assert lead.email == "ann@acme-shop.example"      # lower-cased
    assert lead.name == "Ann Lee" and lead.phone == "+1 555 0100"
    assert lead.thread_ids == ["t1"]
    assert (lead.country, lead.city) == ("DE", "Munich")


def test_a_session_found_only_by_its_id_doesnt_join():
    """guest_session_id holds the token: a row keyed by anything else is no match."""
    leads = build_leads([thread("t1", "the-session-id", "2026-10-02T10:00:00+00:00")],
                        [{**session(TOKEN_A, "ann@acme-shop.example"), "id": "the-session-id"}], [])
    assert leads == []


@pytest.mark.asyncio
async def test_threads_are_merged_by_lowercased_email():
    erp = FakeERP(
        threads=[thread("t1", TOKEN_A, "2026-10-02T10:00:00+00:00"),
                 thread("t2", TOKEN_B, "2026-10-03T10:00:00+00:00", ai_stage="proposal")],
        sessions=[session(TOKEN_A, "ann@acme-shop.example"),
                  session(TOKEN_B, " ANN@ACME-SHOP.EXAMPLE ", converted=True)],
        messages=[message("t2", "guest", "second chat", "2026-10-03T09:59:00+00:00"),
                  message("t1", "guest", "first chat", "2026-10-02T09:59:00+00:00")])
    [lead] = await client_for(erp).read_leads()
    assert lead.email == "ann@acme-shop.example"
    assert sorted(lead.thread_ids) == ["t1", "t2"]
    assert [line.text for line in lead.transcript] == ["first chat", "second chat"]
    assert lead.ai_stage == "proposal" and lead.converted  # the newest thread's stage


def test_no_usable_address_means_no_lead():
    threads = [thread("t1", TOKEN_A, "2026-10-02T10:00:00+00:00")]
    assert build_leads(threads, [session(TOKEN_A, None)], []) == []
    assert build_leads(threads, [session(TOKEN_A, "not an address")], []) == []


@pytest.mark.asyncio
async def test_since_reads_the_changed_threads_and_that_persons_other_chats():
    erp = FakeERP(
        threads=[thread("old", TOKEN_A, "2026-09-01T10:00:00+00:00"),
                 thread("new", TOKEN_B, "2026-10-05T10:00:00+00:00"),
                 thread("other", TOKEN_C, "2026-09-02T10:00:00+00:00")],
        sessions=[session(TOKEN_A, "ann@acme-shop.example"), session(TOKEN_B, "ann@acme-shop.example"),
                  session(TOKEN_C, "bo@other.example", name="Bo Ray")],
        messages=[message("old", "guest", "the earlier chat", "2026-09-01T09:00:00+00:00"),
                  message("new", "guest", "the later chat", "2026-10-05T09:00:00+00:00"),
                  message("other", "guest", "unrelated", "2026-09-02T09:00:00+00:00")])
    leads = await client_for(erp).read_leads(since="2026-10-01T00:00:00")
    assert [lead.email for lead in leads] == ["ann@acme-shop.example"]
    assert [line.text for line in leads[0].transcript] == ["the earlier chat", "the later chat"]
    first = parse_qs(urlsplit(str(erp.requests[0].url)).query)
    assert first["last_message_at"] == ["gte.2026-10-01T00:00:00+00:00"]
    assert first["has_client_info"] == ["eq.true"] and first["is_guest"] == ["eq.true"]


@pytest.mark.asyncio
async def test_nothing_changed_reads_no_further():
    erp = FakeERP()
    assert await client_for(erp).read_leads(since="2026-10-01T00:00:00") == []
    assert len(erp.requests) == 1


@pytest.mark.asyncio
async def test_pages_with_a_range_header_and_chunks_in_lists(monkeypatch):
    monkeypatch.setattr(erp_chat, "PAGE_SIZE", 2)
    monkeypatch.setattr(erp_chat, "IN_CHUNK", 3)
    ids = [f"t{n}" for n in range(5)]
    erp = FakeERP(
        threads=[thread(t, f"tok-{t}", "2026-10-02T10:00:00+00:00") for t in ids],
        sessions=[session(f"tok-{t}", f"p{t}@acme-shop.example") for t in ids],
        messages=[message(t, "guest", f"hello from {t}", "2026-10-02T09:00:00+00:00") for t in ids])
    leads = await client_for(erp).read_leads()
    assert len(leads) == 5 and all(lead.transcript for lead in leads)
    thread_pages = [r for r in erp.requests if "crm_chat_threads" in r.url.path]
    assert [r.headers["range"] for r in thread_pages] == ["0-1", "2-3", "4-5"]
    message_calls = [r for r in erp.requests if "crm_chat_messages" in r.url.path]
    lists = [parse_qs(urlsplit(str(r.url)).query)["thread_id"][0] for r in message_calls]
    # Two chunks of up to three, whatever pages each one takes.
    assert list(dict.fromkeys(lists)) == ["in.(t0,t1,t2)", "in.(t3,t4)"]


# ── source and place ──

def test_source_as_json_text_a_dict_or_a_bare_string():
    text = json.dumps({"referrer": "https://clutch.co/profile/x", "utm": "utm_source=clutch",
                       "intent": "I have a project", "entryPath": "/services/x/"})
    from_text = parse_source(text)
    assert from_text["directory"] == "clutch" and from_text["intent"] == "I have a project"
    assert from_text["referrer"] == "https://clutch.co/profile/x"
    assert from_text["entry_path"] == "/services/x/"
    assert parse_source({"utm": {"source": "the-manifest"}, "openedIntent": "Careers"}) == {
        "referrer": "", "intent": "Careers", "directory": "themanifest", "entry_path": ""}
    assert parse_source("marketing-site") == {"referrer": "", "intent": "", "directory": "",
                                              "entry_path": ""}
    assert parse_source("designrush")["directory"] == "designrush"
    for junk in (None, 7, "{not json", [], ""):
        assert parse_source(junk)["directory"] == ""


def test_directory_names_are_found_however_they_are_written():
    assert directory_in("https://www.goodfirms.co/x") == "goodfirms"
    assert directory_in("Selected-Firms") == "selectedfirms"
    assert directory_in("https://example.com/") == ""


def test_preferred_language_is_a_place_or_a_bare_country():
    assert parse_place("IN|Maharashtra|Pune") == ("IN", "Pune")
    assert parse_place("us") == ("US", "")
    assert parse_place("") == ("", "")
    assert parse_place("not a code|x") == ("", "")


# ── The transcript ──

def _lead_from(*messages_):
    [lead] = build_leads([thread("t1", TOKEN_A, "2026-10-02T10:00:00+00:00")],
                         [session(TOKEN_A, "ann@acme-shop.example")], list(messages_))
    return lead


def test_the_transcript_keeps_only_the_conversation():
    at = "2026-10-02T09:00:00+00:00"
    lead = _lead_from(
        message("t1", "system", "Ann joined the chat", at),
        message("t1", "employee", "Hi, how can I help?", at, meta={"kind": "greeting"}),
        message("t1", "guest", "I have a project", at, meta={"kind": "selector"}),
        message("t1", "employee", "Reach us here", at, meta={"kind": "contact_channels"}),
        message("t1", "employee", "Transcript sent", at, meta={"kind": "email_transcript"}),
        message("t1", "employee", "Internal: looks big", "2026-10-02T09:01:00+00:00",
                meta={"audience": "team"}),
        message("t1", "guest", "We need a new online shop", "2026-10-02T09:02:00+00:00"),
        message("t1", "employee", "Tell me more", "2026-10-02T09:03:00+00:00", is_ai=True),
        message("t1", "employee", "Happy to help", "2026-10-02T09:04:00+00:00"),
        message("t1", "guest", "   ", "2026-10-02T09:05:00+00:00"))
    assert [(line.who, line.text) for line in lead.transcript] == [
        (VISITOR, "We need a new online shop"), (ASSISTANT, "Tell me more"), (REP, "Happy to help")]
    # A clicked button isn't the visitor writing, and the fingerprint is their words.
    assert lead.visitor_words == 6
    assert lead.last_visitor_at == "2026-10-02T09:02:00"


def test_meta_as_json_text_is_read_too():
    lead = _lead_from(message("t1", "employee", "Hi", "2026-10-02T09:00:00+00:00",
                              meta=json.dumps({"kind": "greeting"})))
    assert lead.transcript == []


def test_a_team_note_is_rep_activity_but_not_a_reply_to_them():
    lead = _lead_from(message("t1", "employee", "Internal: call them", "2026-10-02T09:00:00+00:00",
                              meta={"audience": "team"}))
    assert lead.last_human_at == "2026-10-02T09:00:00" and not lead.human_replied


def test_the_assistant_and_its_greeting_are_not_a_human_rep():
    lead = _lead_from(message("t1", "employee", "Hello", "2026-10-02T09:00:00+00:00", is_ai=True),
                      message("t1", "employee", "Hi", "2026-10-02T09:01:00+00:00",
                              meta={"kind": "greeting"}))
    assert lead.last_human_at == "" and not lead.human_replied


def test_a_rep_who_wrote_is_a_human_reply():
    lead = _lead_from(message("t1", "employee", "Happy to help", "2026-10-02T09:00:00+00:00"))
    assert lead.human_replied and lead.last_human_at == "2026-10-02T09:00:00"


@pytest.mark.parametrize("meta,has", [
    ({"kind": "contact_channels", "channels": [{"type": "whatsapp", "value": "x"}]}, True),
    ({"kind": "contact_channels", "channels": {"WhatsApp": "x"}}, True),
    ({"kind": "contact_channels", "channels": [{"type": "email"}]}, False),
    ({"kind": "greeting", "channels": ["whatsapp"]}, False),
])
def test_whatsapp_comes_from_a_contact_channels_card(meta, has):
    lead = _lead_from(message("t1", "employee", "Reach us", "2026-10-02T09:00:00+00:00", meta=meta))
    assert lead.has_whatsapp is has


@pytest.mark.parametrize("text,promised", [
    ("You'll get a written estimate from our team", True),
    ("We reply within 24 hours", True),
    ("Expect it in 24-hours", True),
    ("Tell me more about your shop", False),
    ("It takes 2 weeks", False),
])
def test_a_promised_estimate_is_read_from_the_assistant(text, promised):
    lead = _lead_from(message("t1", "employee", text, "2026-10-02T09:00:00+00:00", is_ai=True))
    assert lead.promised_estimate is promised


def test_a_visitor_saying_it_promises_nothing():
    lead = _lead_from(message("t1", "guest", "Can I have a written estimate in 24 hours?",
                              "2026-10-02T09:00:00+00:00"))
    assert not lead.promised_estimate


# ── GET only, and no token anywhere it shouldn't be ──

@pytest.mark.asyncio
async def test_only_get_is_ever_issued():
    erp = FakeERP(threads=[thread("t1", TOKEN_A, "2026-10-02T10:00:00+00:00")],
                  sessions=[session(TOKEN_A, "ann@acme-shop.example")],
                  messages=[message("t1", "guest", "hello there friend", "2026-10-02T09:00:00+00:00")])
    await client_for(erp).read_leads()
    await client_for(erp).read_leads(since="2026-10-01T00:00:00")
    assert erp.requests and {r.method for r in erp.requests} == {"GET"}


@pytest.mark.asyncio
async def test_anything_but_get_is_refused_before_it_leaves():
    erp = FakeERP()
    async with client_for(erp)._client() as client:
        for method in ("POST", "PATCH", "PUT", "DELETE"):
            with pytest.raises(ERPChatError, match="only issues GET"):
                await client.request(method, "crm_chat_threads")
    assert erp.requests == []


def test_the_source_calls_no_writing_verb():
    source = Path(erp_chat.__file__).read_text(encoding="utf-8")
    assert not re.search(r"\.(post|put|patch|delete)\(", source)


@pytest.mark.asyncio
async def test_a_session_token_reaches_no_log_and_no_lead(caplog):
    caplog.set_level(logging.DEBUG)
    erp = FakeERP(threads=[thread("t1", TOKEN_A, "2026-10-02T10:00:00+00:00")],
                  sessions=[session(TOKEN_A, "ann@acme-shop.example")],
                  messages=[message("t1", "guest", "hello there friend", "2026-10-02T09:00:00+00:00")])
    [lead] = await client_for(erp).read_leads(since="2026-10-01T00:00:00")
    # It does travel in the query, which is why httpx's line must not show it.
    assert any(TOKEN_A in str(r.url) for r in erp.requests)
    assert "crm_guest_sessions" in caplog.text            # the request was logged...
    assert TOKEN_A not in caplog.text                     # ...without its query
    assert "ann@acme-shop.example" not in caplog.text
    assert TOKEN_A not in repr(lead)


@pytest.mark.asyncio
async def test_an_error_quotes_no_url_or_token():
    def down(request):
        return httpx.Response(500, json={"message": f"failed on {request.url}"})
    with pytest.raises(ERPChatError) as e:
        await ERPChatClient("https://erp.example", "the-key",
                            transport=httpx.MockTransport(down)).read_leads()
    assert str(e.value) == "crm_chat_threads answered HTTP 500"

    def refuses(request):
        raise httpx.ConnectError(f"no route to {request.url}")
    with pytest.raises(ERPChatError) as e:
        await ERPChatClient("https://erp.example", "the-key",
                            transport=httpx.MockTransport(refuses)).read_leads()
    assert "erp.example" not in str(e.value) and "ConnectError" in str(e.value)


@pytest.mark.asyncio
async def test_no_keys_says_so():
    with pytest.raises(ERPChatError, match="ERP_SUPABASE_URL"):
        await ERPChatClient("", "").read_leads()


def test_the_url_is_taken_with_or_without_the_rest_path():
    assert ERPChatClient("https://erp.example/rest/v1/", "k").url == "https://erp.example"
    assert ERPChatClient("https://erp.example/", "k").url == "https://erp.example"
