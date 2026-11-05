"""Breakers: what must stop Harvey on WhatsApp at once, and the pauses.

- A ban, "this account is not allowed", unusual activity, spam or a temporary
  restriction: a hard stop, paused 48 hours (``pause_hours_on_warning``).
- Signed out, or the QR code showing: paused until the phone is linked again
  (Connect on the dashboard's WhatsApp tab, or `harvey whatsapp login`).
- "Phone not connected" or WhatsApp open in another window: a short pause.
- Three failed sends in a row (``max_failed_sends_in_a_row``): paused until you
  resume, because something is wrong that retrying won't fix.

Only WhatsApp's own interface is read for this, never what anyone wrote: a
contact writing "my account got banned" or naming themselves "Unusual Activity"
is not WhatsApp warning the account. The chat list, the open chat (except its
footer, where WhatsApp says you can't send) and the composer are removed first.
"""

import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from harvey.linkedin.pages import soup_of
from harvey.whatsapp.pages import (
    CHAT_FOOTER, CHAT_LIST, CHAT_PANE, DIALOG, EDITABLE, NOT_INTERFACE, OFFLINE_ICON, QR_CODE, fold,
)

PAUSE_KEY = "whatsapp:pause"
FAILED_KEY = "whatsapp:failed_in_a_row"

# kind -> what the dashboard says
REASONS = {
    "signed_out": "This number isn't linked in Harvey's Chrome profile (WhatsApp is showing its QR code)",
    "banned": "WhatsApp says this account is banned or not allowed to use WhatsApp",
    "unusual_activity": "WhatsApp warned about unusual activity on this account",
    "spam": "WhatsApp warned about spam or bulk messaging on this account",
    "restricted": "WhatsApp says this account is restricted for now",
    "phone_offline": "WhatsApp says the phone isn't connected",
    "session_conflict": "WhatsApp is open in another window",
}
HARD_STOPS = frozenset({"banned", "unusual_activity", "spam", "restricted"})

_TEXT_PATTERNS = (
    ("banned", ("account is not allowed", "not allowed to use whatsapp",
                "can no longer use whatsapp", "phone number is banned", "number is banned",
                "account has been banned", "account is banned", "banned from using whatsapp",
                "temporarily banned", "account was banned")),
    ("restricted", ("temporarily restricted", "account is restricted", "account has been restricted",
                    "account was restricted", "restricted your account", "account is limited",
                    "account has been limited", "you can't send messages", "can't send messages for",
                    "cannot send messages", "you can't message", "you can't start new chats",
                    "can't start new chats", "temporary restriction", "temporarily limited")),
    ("spam", ("sending spam", "sent spam", "spam activity", "reported for spam", "reported as spam",
              "suspected spam", "because of spam", "considered spam", "bulk messaging",
              "automated or bulk", "too many messages", "sending messages too quickly")),
    ("unusual_activity", ("unusual activity", "suspicious activity", "automated activity",
                          "unofficial app", "unofficial whatsapp", "violates our terms",
                          "violating our terms", "violated our terms")),
    ("session_conflict", ("whatsapp is open in another window", "open in another window")),
    ("phone_offline", ("phone not connected", "phone is not connected", "computer not connected",
                       "make sure your phone has an active internet connection",
                       "trying to reach phone")),
)
# Only when the app itself isn't on screen: the linking page's own words.
_LINKING_PATTERNS = ("scan this qr code", "scan to log in", "steps to log in",
                     "log into whatsapp web", "log in with phone number", "link with phone number",
                     "use whatsapp on your computer", "to use whatsapp on your computer")


@dataclass
class Breaker:
    kind: str
    reason: str
    url: str = ""


class BreakerTripped(Exception):
    """A breaker was seen: stop the run (the pause is already set)."""

    def __init__(self, breaker: Breaker):
        super().__init__(breaker.reason)
        self.breaker = breaker


def own_interface_text(html: str) -> str:
    """The page's text with everything people wrote or named taken out."""
    s = soup_of(html)
    for el in s.select(NOT_INTERFACE):
        el.decompose()
    kept = []
    main = s.select_one(CHAT_PANE)
    if main is not None:
        # Dialogs and the footer can sit inside the chat pane; their text is
        # WhatsApp's. The footer's composer is what Harvey typed.
        for el in main.select(DIALOG):
            kept.append(el.get_text(" "))
        footer = s.select_one(CHAT_FOOTER)
        if footer is not None:
            for el in footer.select(EDITABLE):
                el.decompose()
            kept.append(footer.get_text(" "))
        main.decompose()
    pane = s.select_one(CHAT_LIST)
    if pane is not None:
        pane.decompose()
    kept.append(s.get_text(" "))
    return fold(" ".join(kept))


def detect_breaker(url: str, html: str) -> Breaker | None:
    """The breaker this page is, or None."""
    text = own_interface_text(html)
    for kind, patterns in _TEXT_PATTERNS:
        for p in patterns:
            if p in text:
                return Breaker(kind=kind, reason=REASONS[kind], url=url)
    s = soup_of(html)
    if s.select_one(OFFLINE_ICON):
        return Breaker(kind="phone_offline", reason=REASONS["phone_offline"], url=url)
    in_app = bool(s.select_one(CHAT_LIST)) or bool(s.select_one(CHAT_PANE))
    if s.select_one(QR_CODE) or (not in_app and any(p in text for p in _LINKING_PATTERNS)):
        return Breaker(kind="signed_out", reason=REASONS["signed_out"], url=url)
    return None


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


async def set_pause(state, reason: str, kind: str = "breaker", hours: float | None = 48,
                    now: datetime | None = None, url: str = "") -> dict:
    """Pause WhatsApp: for ``hours``, or until resumed when hours is None."""
    now = now or _now()
    pause = {"reason": reason, "kind": kind, "since": now.isoformat(timespec="seconds"),
             "until": (now + timedelta(hours=hours)).isoformat(timespec="seconds")
             if hours else None, "url": url}
    await state.set_setting(PAUSE_KEY, json.dumps(pause))
    try:
        await state.log_action("whatsapp_paused", "whatsapp", pause)
    except Exception:
        pass
    return pause


async def trip(state, breaker: Breaker, cfg=None, now: datetime | None = None) -> dict:
    """Record a breaker: signed out waits for the phone, a warning is 48 hours,
    a phone that dropped off or a second window a short while."""
    if breaker.kind == "signed_out":
        return await set_pause(state, breaker.reason + ". Press Connect on the dashboard's WhatsApp tab "
                                                       "(or run `harvey whatsapp login`) to link it.",
                               kind="signed_out", hours=None, url=breaker.url, now=now)
    if breaker.kind in ("phone_offline", "session_conflict"):
        minutes = getattr(cfg, "pause_minutes_phone_offline", 30)
        return await set_pause(state, breaker.reason, kind=breaker.kind, hours=minutes / 60,
                               url=breaker.url, now=now)
    hours = getattr(cfg, "pause_hours_on_warning", 48)
    return await set_pause(state, breaker.reason, kind=breaker.kind, hours=hours, url=breaker.url,
                           now=now)


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
    """Lift the pause (only one of ``kinds``, when given). True if lifted. The
    failed-send count starts again with it, or one more failure re-trips."""
    pause = await active_pause(state)
    raw = await state.get_setting(PAUSE_KEY)
    if not raw:
        return False
    if kinds and pause and pause.get("kind") not in kinds:
        return False
    await state.set_setting(PAUSE_KEY, "")
    await state.set_setting(FAILED_KEY, "0")
    return True
