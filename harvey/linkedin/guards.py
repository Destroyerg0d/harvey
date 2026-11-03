"""Breakers: the pages that must stop Harvey on LinkedIn at once.

A captcha or security check, an "unusual activity" or restriction notice,
the weekly invitation limit, a "Something went wrong" toast, or a login
wall. Any of them pauses LinkedIn for ``pause_hours_on_warning`` (48 by
default) with the reason, which the dashboard shows, and ends the run.
Signed out pauses until the owner signs in again (`harvey linkedin login`).

Message text is never read for this: a lead writing "something went wrong
with our checkout" is not LinkedIn warning the account.
"""

import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from harvey.linkedin.pages import SIGNED_OUT_PATHS, clean_text, path_segments, soup_of

PAUSE_KEY = "linkedin:pause"

# kind -> what the dashboard says
REASONS = {
    "challenge": "LinkedIn asked for a security check (captcha or sign-in challenge)",
    "restricted": "LinkedIn says the account is restricted",
    "unusual_activity": "LinkedIn warned about unusual activity on the account",
    "invite_limit": "LinkedIn says the weekly invitation limit is reached",
    "error_toast": "LinkedIn showed \"Something went wrong\"",
    "signed_out": "The LinkedIn profile is signed out",
}

_TEXT_PATTERNS = (
    ("challenge", ("quick security check", "security verification", "verify you're a human",
                   "verify you are a human", "verify that you're not a robot",
                   "i'm not a robot", "complete this puzzle", "prove you're a person")),
    ("restricted", ("temporarily restricted", "account has been restricted",
                    "account is restricted", "restricted your account",
                    "account has been temporarily limited", "your account is temporarily limited")),
    ("unusual_activity", ("unusual activity", "suspicious activity",
                          "use of automation tools", "automated activity")),
    ("invite_limit", ("weekly invitation limit", "reached the weekly limit",
                      "invitation limit", "limit for invitations", "too many invitations",
                      "out of invitations for the week")),
)
# Only inside a toast or alert: as page text it is too ordinary.
_TOAST_PATTERNS = ("something went wrong", "try again later")
_TOAST_SELECTOR = (".artdeco-toast-item, [role='alert'], .artdeco-inline-feedback--error, "
                   "[data-test-artdeco-toast-item-type='error'], .artdeco-global-alert")
_OVERLAY_SELECTOR = ("[role='dialog'], [role='alertdialog'], .artdeco-modal, .global-alert-banner, "
                     ".artdeco-global-alert, " + _TOAST_SELECTOR)
# What people wrote, and what Harvey typed: never read for warnings.
_USER_TEXT = (".msg-s-event-listitem__body, .msg-conversation-card__message-snippet, "
              ".msg-form, textarea, [contenteditable], .feed-shared-update-v2, "
              ".update-components-text, .msg-s-message-list")


@dataclass
class Breaker:
    kind: str
    reason: str
    url: str = ""


class BreakerTripped(Exception):
    """A breaker page was seen: stop the run (the pause is already set)."""

    def __init__(self, breaker: Breaker):
        super().__init__(breaker.reason)
        self.breaker = breaker


def _breaker(kind: str, url: str, detail: str = "") -> Breaker:
    reason = REASONS[kind] + (f": \"{detail}\"" if detail else "")
    return Breaker(kind=kind, reason=reason, url=url)


def detect_breaker(url: str, html: str) -> Breaker | None:
    """The breaker this page is, or None."""
    first, segments = path_segments(url)
    if first in ("checkpoint", "challenge") or any(x.startswith("captcha") for x in segments):
        return _breaker("challenge", url)
    if first in SIGNED_OUT_PATHS:
        return _breaker("signed_out", url)

    s = soup_of(html)
    if s.select_one("iframe[src*='captcha'], #captcha-internal, [id*='captcha'], "
                    "[class*='captcha'], iframe[src*='challenge']"):
        return _breaker("challenge", url)
    for el in s.select(_USER_TEXT):
        el.decompose()
    has_nav = bool(s.select_one("#global-nav, .global-nav, .global-nav__me"))
    if not has_nav and s.select_one("form.login__form, input#username, input[name='session_key']"):
        return _breaker("signed_out", url)

    for el in s.select(_TOAST_SELECTOR):
        text = clean_text(el.get_text(" ")).lower()
        if any(p in text for p in _TOAST_PATTERNS):
            return _breaker("error_toast", url)
    # Warnings come as a modal, a banner or a whole page of their own (no
    # navigation); a feed or a profile's own text is not scanned.
    scopes = s.select(_OVERLAY_SELECTOR)
    if not has_nav:
        scopes.append(s)
    for scope in scopes:
        text = clean_text(scope.get_text(" ")).lower().replace("’", "'")
        for kind, patterns in _TEXT_PATTERNS:
            for p in patterns:
                if p in text:
                    return _breaker(kind, url)
    return None


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


async def set_pause(state, reason: str, kind: str = "breaker", hours: float | None = 48,
                    now: datetime | None = None, url: str = "") -> dict:
    """Pause LinkedIn: for ``hours``, or until resumed when hours is None."""
    now = now or _now()
    pause = {"reason": reason, "kind": kind, "since": now.isoformat(timespec="seconds"),
             "until": (now + timedelta(hours=hours)).isoformat(timespec="seconds")
             if hours else None, "url": url}
    await state.set_setting(PAUSE_KEY, json.dumps(pause))
    try:
        await state.log_action("linkedin_paused", "linkedin", pause)
    except Exception:
        pass
    return pause


async def trip(state, breaker: Breaker, hours: float = 48) -> dict:
    """Record a breaker: signed out waits for a sign-in, the rest ``hours``."""
    if breaker.kind == "signed_out":
        return await set_pause(state, breaker.reason + ". Run `harvey linkedin login` to sign in.",
                               kind="signed_out", hours=None, url=breaker.url)
    return await set_pause(state, breaker.reason, kind=breaker.kind, hours=hours, url=breaker.url)


async def active_pause(state, now: datetime | None = None) -> dict | None:
    """The pause in force, or None (an expired one counts as none)."""
    raw = await state.get_setting(PAUSE_KEY)
    if not raw:
        return None
    try:
        pause = json.loads(raw)
    except (TypeError, ValueError):
        return {"reason": str(raw), "kind": "manual", "since": "", "until": None}
    if not isinstance(pause, dict):
        return None
    until = pause.get("until")
    if until:
        try:
            if datetime.fromisoformat(until) <= (now or _now()):
                return None
        except ValueError:
            pass
    return pause


async def clear_pause(state, kinds=None) -> bool:
    """Lift the pause (only one of ``kinds``, when given). True if lifted."""
    pause = await active_pause(state)
    raw = await state.get_setting(PAUSE_KEY)
    if not raw:
        return False
    if kinds and pause and pause.get("kind") not in kinds:
        return False
    await state.set_setting(PAUSE_KEY, "")
    return True
