"""Breakers: what must stop Harvey on LinkedIn, and the 48-hour pause."""

import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from harvey.linkedin import guards
from harvey.state import StateManager

NAV = '<header id="global-nav"></header>'


def _utcnow():
    return datetime.now(timezone.utc).replace(tzinfo=None)


@pytest.mark.parametrize("url,html,kind", [
    ("https://www.linkedin.com/checkpoint/challenge/abc", "", "challenge"),
    ("https://www.linkedin.com/feed/", NAV + '<iframe src="https://x.example/captcha"></iframe>', "challenge"),
    ("https://www.linkedin.com/feed/", NAV + "<div role='dialog'>Let's do a quick security check</div>",
     "challenge"),
    ("https://www.linkedin.com/feed/", NAV + "<div class='artdeco-modal'>We noticed unusual activity</div>",
     "unusual_activity"),
    ("https://www.linkedin.com/in/x/", "<main>Your account has been temporarily restricted</main>",
     "restricted"),
    ("https://www.linkedin.com/in/x/", NAV + "<div role='dialog'>You’ve reached the weekly "
     "invitation limit</div>", "invite_limit"),
    ("https://www.linkedin.com/messaging/", NAV + "<div class='artdeco-toast-item'>Something went "
     "wrong. Try again.</div>", "error_toast"),
    ("https://www.linkedin.com/authwall?trk=x", "", "signed_out"),
    ("https://www.linkedin.com/login", "", "signed_out"),
    ("https://www.linkedin.com/feed/", "<form class='login__form'><input id='username'></form>",
     "signed_out"),
])
def test_breaker_pages(url, html, kind):
    breaker = guards.detect_breaker(url, html)
    assert breaker is not None and breaker.kind == kind
    assert breaker.reason


@pytest.mark.parametrize("html", [
    # What people write is never a warning about the account.
    NAV + "<ul class='msg-s-message-list'><li><p class='msg-s-event-listitem__body'>Something went "
    "wrong with our checkout and we saw unusual activity in orders</p></li></ul>",
    NAV + "<p class='msg-conversation-card__message-snippet'>temporarily restricted shipping</p>",
    NAV + "<div class='feed-shared-update-v2'>Our road is temporarily restricted</div>",
    NAV + "<main><p>Something went wrong? We fix stores.</p></main>",
    NAV + "<form class='msg-form'><div contenteditable>captcha unusual activity</div></form>",
])
def test_user_text_never_trips_a_breaker(html):
    assert guards.detect_breaker("https://www.linkedin.com/messaging/thread/1/", html) is None


def test_a_profile_slug_with_login_in_it_is_not_the_login_page():
    assert guards.detect_breaker("https://www.linkedin.com/in/loginov/", NAV) is None


def _state(tmp_path):
    state = StateManager(str(tmp_path / "h.db"))
    asyncio.run(state.init_db())
    return state


def test_a_breaker_pauses_for_48_hours_then_expires(tmp_path):
    state = _state(tmp_path)
    now = _utcnow()
    breaker = guards.Breaker("unusual_activity", guards.REASONS["unusual_activity"])
    asyncio.run(guards.trip(state, breaker, hours=48))
    pause = asyncio.run(guards.active_pause(state, now))
    assert pause and pause["kind"] == "unusual_activity" and "unusual activity" in pause["reason"]
    assert asyncio.run(guards.active_pause(state, _utcnow() + timedelta(hours=47)))
    assert asyncio.run(guards.active_pause(state, _utcnow() + timedelta(hours=49))) is None


def test_signed_out_waits_for_a_sign_in_and_login_clears_only_that(tmp_path):
    state = _state(tmp_path)
    asyncio.run(guards.trip(state, guards.Breaker("signed_out", guards.REASONS["signed_out"])))
    pause = asyncio.run(guards.active_pause(state, _utcnow() + timedelta(days=30)))
    assert pause["kind"] == "signed_out" and pause["until"] is None
    assert asyncio.run(guards.clear_pause(state, kinds=("signed_out",)))
    assert asyncio.run(guards.active_pause(state)) is None
    asyncio.run(guards.trip(state, guards.Breaker("challenge", guards.REASONS["challenge"])))
    assert not asyncio.run(guards.clear_pause(state, kinds=("signed_out",)))
    assert asyncio.run(guards.active_pause(state))["kind"] == "challenge"
    assert asyncio.run(guards.clear_pause(state))


# Seen live 2026-11-08: LinkedIn's messaging pages carry Google's invisible reCAPTCHA,
# which paused LinkedIn for 48 hours on an ordinary thread.
INVISIBLE_RECAPTCHA = (
    "<iframe title='reCAPTCHA' src='https://www.google.com/recaptcha/enterprise/anchor?ar=1&k=x&size=invisible'>"
    "</iframe><div class='grecaptcha-badge' style='display: none;'><div class='grecaptcha-logo'></div>"
    "<div class='grecaptcha-error'></div><textarea id='g-recaptcha-response-100000' "
    "class='g-recaptcha-response'></textarea></div>")


def test_the_invisible_recaptcha_is_not_a_security_check():
    from harvey.linkedin.guards import detect_breaker
    assert detect_breaker("https://www.linkedin.com/messaging/thread/x/", INVISIBLE_RECAPTCHA) is None


def test_a_check_someone_has_to_pass_still_stops_it():
    from harvey.linkedin.guards import detect_breaker
    thread = "https://www.linkedin.com/messaging/thread/x/"
    for page in (INVISIBLE_RECAPTCHA + "<iframe src='https://www.google.com/recaptcha/enterprise/bframe?k=x'></iframe>",
                 "<iframe src='https://www.google.com/recaptcha/api2/anchor?k=x&size=normal'></iframe>",
                 "<div id='captcha-internal'></div>",
                 "<iframe src='https://client-api.arkoselabs.com/fc/x'></iframe>"):
        breaker = detect_breaker(thread, page)
        assert breaker is not None and breaker.kind == "challenge", page[:60]
