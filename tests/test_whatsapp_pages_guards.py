"""Parsing WhatsApp Web's page, and the breakers. Hand-written markup in the shape
pages.py reads: nothing here has seen the live site, so what matters most is that
markup it doesn't recognise means "not sure", never a send."""

import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from harvey.state import StateManager
from harvey.whatsapp import guards
from harvey.whatsapp.pages import (
    app_ready, chat_is_for, chat_open, chat_title, invalid_number, parse_chat_list, parse_messages,
    qr_visible, send_url, title_digits,
)
from tests.whatsapp_fakes import QR_PAGE, make_config

JANE = "+447700900123"
APP = '<div id="app"><div id="side"><div id="pane-side">{rows}</div></div>{main}</div>'


def chat(title="+44 7700 900123", rows="", footer="", extra=""):
    return APP.format(rows="", main=(
        f'<div id="main"><header><span title="{title}">{title}</span></header>{rows}'
        f'<footer>{footer}<div contenteditable="true" role="textbox"></div></footer></div>{extra}'))


def bubble(direction, text, data_id="x_1", icon=""):
    return (f'<div data-id="{data_id}"><div class="message-{direction}"><span class="selectable-text">'
            f'<span>{text}</span></span>{icon}</div></div>')


def test_the_url_is_by_number_and_never_carries_text():
    assert send_url("+44 7700 900123") == "https://web.whatsapp.com/send?phone=447700900123"
    assert "text" not in send_url(JANE)


# ── Which screen ──

def test_the_screens():
    assert qr_visible(QR_PAGE) and not app_ready(QR_PAGE)
    assert app_ready(APP.format(rows="", main=""))
    assert not app_ready("<html><body>Loading your chats</body></html>")
    assert invalid_number('<div role="dialog">Phone number shared via url is invalid.</div>')
    assert not invalid_number("<p>Phone number shared via url is invalid.</p>")   # not WhatsApp's notice
    assert not invalid_number(chat(rows=bubble("in", "Phone number shared via url is invalid.")))


def test_a_chat_is_open_only_with_a_composer_and_nothing_on_top_of_it():
    assert chat_open(chat())
    assert not chat_open(chat(extra='<div role="dialog">Starting chat</div>'))
    assert not chat_open(APP.format(rows="", main='<div id="main"><header></header></div>'))
    assert not chat_open("")


# ── Which chat: fails closed ──

@pytest.mark.parametrize("title,digits", [
    ("+44 7700 900123", "447700900123"),
    ("\u202a+44 7700 900123\u202c", "447700900123"),
    ("+1 (415) 555-0100", "14155550100"),
    ("Suite 415 Dental", ""),
    ("Jane Doe", ""),
    ("12", ""),
])
def test_title_digits(title, digits):
    assert title_digits(title) == digits


def test_the_chat_is_theirs_by_number_or_failing_that_their_name():
    assert chat_is_for(chat(), JANE)
    assert not chat_is_for(chat(title="+44 7700 900999"), JANE)
    assert chat_is_for(chat(title="Jane Doe"), JANE, "Jane Doe")
    assert not chat_is_for(chat(title="Jane Doe"), JANE)            # no name of theirs to compare
    assert not chat_is_for(chat(title="Riley Other"), JANE, "Jane Doe")
    assert not chat_is_for(APP.format(rows="", main=""), JANE, "Jane Doe")
    assert not chat_is_for("<html></html>", JANE, "Jane Doe")
    assert chat_title("<html></html>") == ""


# ── Messages ──

def test_messages_come_oldest_first_with_direction_id_and_clock():
    html = chat(rows=bubble("out", "Hello there", "true_44@c.us_1", '<span data-icon="msg-dblcheck"></span>')
                + bubble("in", "Who is this?", "false_44@c.us_2")
                + bubble("out", "Sorry", "true_44@c.us_3", '<span data-icon="msg-time"></span>'))
    got = parse_messages(html)
    assert [(m.direction, m.body, m.pending) for m in got] == [
        ("out", "Hello there", False), ("in", "Who is this?", False), ("out", "Sorry", True)]
    assert got[1].wa_id == "false_44@c.us_2"


def test_a_row_whose_direction_cant_be_told_is_left_out():
    both = '<div data-id="a"><div class="message-in"></div><div class="message-out"></div></div>'
    neither = '<div data-id="b"><span class="selectable-text">Hello</span></div>'
    assert parse_messages(chat(rows=both + neither)) == []


def test_a_quoted_message_isnt_taken_for_their_words_and_emoji_survive():
    row = ('<div data-id="a"><div class="message-in"><div data-testid="quoted-message">'
           '<span class="selectable-text">What you wrote</span></div>'
           '<span class="selectable-text">Sure <img alt="\U0001F44D"></span></div></div>')
    assert parse_messages(chat(rows=row))[0].body == "Sure \U0001F44D"


def test_a_message_with_no_text_is_marked_so():
    row = '<div data-id="a"><div class="message-in"><img src="x"></div></div>'
    got = parse_messages(chat(rows=row))
    assert got[0].text is False and got[0].body == ""


def test_markup_it_doesnt_know_gives_nothing():
    html = '<div class="totally-new"><p>Hello</p></div>'
    assert parse_messages(html) == [] and parse_chat_list(html) == []
    assert not app_ready(html) and not chat_open(html) and not chat_is_for(html, JANE, "Jane")


# ── The chat list ──

def test_the_chat_list_keeps_only_title_digits_and_unread():
    rows = ('<div role="listitem"><span title="+44 7700 900123"></span><span title="preview text"></span>'
            '<span aria-label="2 unread messages">2</span></div>'
            '<div role="listitem"><span title="Family Group"></span></div>'
            '<div role="row"><span title="+1 (415) 555-0100"></span></div>')
    got = parse_chat_list(APP.format(rows=rows, main=""))
    assert [(r.title, r.digits, r.unread) for r in got] == [
        ("+44 7700 900123", "447700900123", True), ("Family Group", "", False),
        ("+1 (415) 555-0100", "14155550100", False)]
    assert not hasattr(got[0], "snippet")


# ── Breakers ──

BAN = '<div id="app"><div>{}</div></div>'


@pytest.mark.parametrize("text,kind", [
    ("Your phone number is banned from using WhatsApp.", "banned"),
    ("This account is not allowed to use WhatsApp", "banned"),
    ("This account can no longer use WhatsApp due to spam.", "banned"),
    ("Your account has been temporarily banned", "banned"),
    ("This account is temporarily restricted", "restricted"),
    ("You can’t send messages for a while", "restricted"),
    ("Your account may be sending spam", "spam"),
    ("We detected bulk messaging", "spam"),
    ("We noticed unusual activity on your account", "unusual_activity"),
    ("Suspicious activity", "unusual_activity"),
    ("WhatsApp is open in another window", "session_conflict"),
    ("Phone not connected", "phone_offline"),
    ("Make sure your phone has an active internet connection", "phone_offline"),
])
def test_breaker_notices(text, kind):
    for html in (BAN.format(text),
                 APP.format(rows="", main=f'<div role="dialog">{text}</div>'),
                 chat(extra=f'<div role="alertdialog">{text}</div>')):
        breaker = guards.detect_breaker("https://web.whatsapp.com/", html)
        assert breaker is not None and breaker.kind == kind and breaker.reason


def test_a_notice_in_the_chats_footer_counts_too():
    html = chat(footer="You can't send messages for a while")
    assert guards.detect_breaker("u", html).kind == "restricted"


def test_a_notice_above_the_chat_list_counts_too():
    html = APP.format(rows="", main="").replace('<div id="pane-side">', 'Phone not connected<div id="pane-side">')
    assert guards.detect_breaker("u", html).kind == "phone_offline"


def test_the_offline_icon_counts():
    html = APP.format(rows="", main='<span data-icon="alert-phone"></span>')
    assert guards.detect_breaker("u", html).kind == "phone_offline"


def test_the_qr_code_is_signed_out():
    assert guards.detect_breaker("u", QR_PAGE).kind == "signed_out"
    assert guards.detect_breaker("u", "<div>Scan to log in</div>").kind == "signed_out"


@pytest.mark.parametrize("words", [
    "My account is banned from Instagram, can you help?",
    "We noticed unusual activity on our store",
    "Your phone number is banned, says my friend",
    "Phone not connected to the wifi again",
    "This account is not allowed to use WhatsApp, ha",
    "stop sending spam",
])
def test_what_people_write_is_never_read_for_this(words):
    assert guards.detect_breaker("u", chat(rows=bubble("in", words))) is None
    # ...nor what they call themselves, in the list or at the top of the chat...
    row = f'<div role="listitem"><span title="{words}"></span></div>'
    assert guards.detect_breaker("u", APP.format(rows=row, main="")) is None
    assert guards.detect_breaker("u", chat(title=words)) is None
    # ...nor what's in the composer.
    typed = chat().replace("</div></footer>", f"{words}</div></footer>")
    assert guards.detect_breaker("u", typed) is None


def test_the_pages_own_code_is_never_read_for_this():
    html = ("<html><head><title>account is banned</title><style>/* unusual activity */</style></head><body>"
            "<script>var s = 'This account is not allowed to use WhatsApp';</script>"
            + chat() + "</body></html>")
    assert guards.detect_breaker("u", html) is None


def test_an_ordinary_app_page_is_no_breaker():
    assert guards.detect_breaker("u", chat()) is None
    assert guards.detect_breaker("u", APP.format(rows="", main="")) is None


# ── Pauses ──

def _state(tmp_path):
    state = StateManager(str(tmp_path / "h.db"))
    asyncio.run(state.init_db())
    return state


def _utcnow():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def test_each_kind_of_trip_pauses_for_its_time(tmp_path):
    state = _state(tmp_path)
    cfg = make_config().channels.whatsapp
    now = _utcnow()

    async def go(kind):
        await guards.trip(state, guards.Breaker(kind, guards.REASONS[kind]), cfg, now=now)
        return await guards.active_pause(state, now)
    for kind in ("banned", "restricted", "spam", "unusual_activity"):
        pause = asyncio.run(go(kind))
        assert pause["kind"] == kind
        assert datetime.fromisoformat(pause["until"]) == now.replace(microsecond=0) + timedelta(hours=48)
    pause = asyncio.run(go("phone_offline"))
    assert datetime.fromisoformat(pause["until"]) == now.replace(microsecond=0) + timedelta(minutes=30)
    pause = asyncio.run(go("signed_out"))
    assert pause["until"] is None and "harvey whatsapp login" in pause["reason"]


def test_a_pause_expires_and_clearing_starts_the_failure_count_again(tmp_path):
    state = _state(tmp_path)
    now = _utcnow()
    asyncio.run(guards.set_pause(state, "x", hours=48, now=now))
    assert asyncio.run(guards.active_pause(state, now + timedelta(hours=47)))
    assert asyncio.run(guards.active_pause(state, now + timedelta(hours=49))) is None
    asyncio.run(guards.set_pause(state, "x", kind="manual", hours=None, now=now))
    asyncio.run(state.set_setting(guards.FAILED_KEY, "3"))
    assert not asyncio.run(guards.clear_pause(state, kinds=("signed_out",)))
    assert asyncio.run(guards.clear_pause(state))
    assert asyncio.run(state.get_setting(guards.FAILED_KEY)) == "0"
    assert asyncio.run(guards.active_pause(state)) is None
