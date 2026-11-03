"""A pretend LinkedIn for the LinkedIn agent's tests: no network, no browser.

FakeLinkedIn renders hand-written pages in the shape of LinkedIn's markup
(the selectors harvey/linkedin/pages.py reads), and FakeDriver plays the
browser over it: goto, click, type, scroll. Every person here is invented.
"""

import html as htmlmod
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone

from harvey.config import HarveyConfig
from harvey.linkedin.pages import (
    BASE, COMPOSER, CONNECTIONS_URL, FEED_URL, INVITE_NOTE_BOX, MESSAGING_URL, SEND_BUTTON,
    SENT_INVITES_URL,
)

OWNER = "Sam Owner"

NAV = '<header id="global-nav" class="global-nav"><a class="global-nav__me">Me</a></header>'


def page(body: str, nav: bool = True) -> str:
    return f"<html><body>{NAV if nav else ''}<main>{body}</main></body></html>"


def e(text: str) -> str:
    return htmlmod.escape(text or "")


def thread_html(header: str, messages, composer: str = "", recipient_pill: str = "") -> str:
    """A conversation as LinkedIn draws it: grouped senders, then bodies."""
    groups, last = [], None
    for sender, body in messages:
        if sender != last:
            groups.append(f'<li class="msg-s-message-list__event"><span class="msg-s-message-group__name">'
                          f'{e(sender)}</span></li>')
            last = sender
        groups.append(f'<li class="msg-s-message-list__event"><p class="msg-s-event-listitem__body">'
                      f'{e(body)}</p></li>')
    pill = (f'<div class="msg-compose__recipient-pill"><span>{e(recipient_pill)}</span></div>'
            if recipient_pill else "")
    return page(
        f'{pill}<h2 class="msg-entity-lockup__entity-title">{e(header)}</h2>'
        f'<ul class="msg-s-message-list">{"".join(groups)}</ul>'
        f'<form class="msg-form"><div class="msg-form__contenteditable" contenteditable="true">'
        f'<p>{e(composer)}</p></div><button class="msg-form__send-button">Send</button></form>')


@dataclass
class Person:
    slug: str
    name: str
    connected: bool = False
    connected_on: str = ""            # "Oct 28, 2026"
    pending: bool = False
    pending_age: str = "Sent 3 days ago"
    thread: list = field(default_factory=list)   # [(sender, body)]
    urn: str = ""

    @property
    def thread_url(self) -> str:
        return f"{BASE}/messaging/thread/t-{self.slug}/"


class FakeLinkedIn:
    def __init__(self, people=(), *, note_limit=False, weekly_limit=False, breaker_on=""):
        self.people = {p.slug: p for p in people}
        self.note_limit = note_limit
        self.weekly_limit = weekly_limit
        self.breaker_on = breaker_on      # a URL that shows "unusual activity"
        self.signed_in = True
        self.withdrawn: list[str] = []

    def person_by_urn(self, urn: str):
        return next((p for p in self.people.values() if (p.urn or p.slug) == urn), None)

    def render(self, url: str, state: dict) -> str:
        if not self.signed_in:
            return page('<form class="login__form"><input id="username"></form>', nav=False)
        if self.breaker_on and url.startswith(self.breaker_on):
            return page('<div role="dialog" class="artdeco-modal"><h2>We noticed unusual activity '
                        'on your account</h2></div>')
        if url.startswith(FEED_URL):
            return page('<div class="feed-shared-update-v2">A post about a temporarily restricted '
                        'road, nothing to do with the account.</div>')
        if url.startswith(MESSAGING_URL) and "/thread/" not in url and "/compose/" not in url:
            items = []
            for p in self.people.values():
                if not p.thread:
                    continue
                sender, body = p.thread[-1]
                snippet = ("You: " if sender == OWNER else "") + body
                items.append(f'<li class="msg-conversation-listitem"><a href="{p.thread_url}">'
                             f'<h3>{e(p.name)}</h3><p class="msg-conversation-card__message-snippet">'
                             f'{e(snippet)}</p><time>2:14 PM</time></a></li>')
            return page(f'<ul class="msg-conversations-container__conversations-list">{"".join(items)}</ul>')
        if "/messaging/thread/" in url:
            slug = url.rstrip("/").rsplit("/t-", 1)[-1]
            p = self.people[slug]
            return thread_html(p.name, p.thread, state.get("composer", ""))
        if "/messaging/compose/" in url:
            m = re.search(r"recipient=([^&]+)", url)
            p = self.person_by_urn(m.group(1)) if m else None
            if p is None:
                return thread_html("New message", [], state.get("composer", ""))
            shown = state.get("pill_override") or p.name
            return thread_html(shown, p.thread, state.get("composer", ""), recipient_pill=shown)
        if url.startswith(CONNECTIONS_URL):
            cards = "".join(
                f'<li class="mn-connection-card"><a href="/in/{p.slug}/"><span>{e(p.name)}</span></a>'
                f'<time>Connected on {p.connected_on}</time></li>'
                for p in self.people.values() if p.connected and p.connected_on)
            return page(f'<ul>{cards}</ul>')
        if url.startswith(SENT_INVITES_URL):
            cards = "".join(
                f'<li class="invitation-card"><a href="/in/{p.slug}/">{e(p.name)}</a>'
                f'<span class="time-badge">{p.pending_age}</span><button>Withdraw</button></li>'
                for p in self.people.values() if p.pending)
            return page(f'<ul>{cards}</ul>')
        m = re.search(r"/in/([^/?#]+)", url)
        if m:
            p = self.people[m.group(1)]
            return self.profile_html(p, state)
        return page("<p>Page not found</p>")

    def profile_html(self, p: Person, state: dict) -> str:
        urn = p.urn or p.slug
        if p.connected:
            buttons = (f'<a href="/messaging/compose/?profileUrn=urn%3Ali%3Afsd_profile%3A{urn}'
                       f'&recipient={urn}&screenContext=NON_SELF_PROFILE_VIEW" '
                       f'aria-label="Message {e(p.name)}">Message</a><span>1st</span>')
        elif p.pending:
            buttons = '<button aria-label="Pending, click to withdraw invitation">Pending</button>'
        else:
            buttons = f'<button aria-label="Invite {e(p.name)} to connect">Connect</button>'
        modal = state.get("modal", "")
        toast = state.get("toast", "")
        sidebar = ('<aside><a href="/messaging/compose/?profileUrn=urn%3Ali%3Afsd_profile%3AOTHER'
                   '&recipient=OTHER" aria-label="Message Riley Other">Message</a></aside>')
        return (f"<html><body>{NAV}{sidebar}<main><section class=\"artdeco-card pv-top-card\">"
                f"<h1>{e(p.name)}</h1>{buttons}</section>{modal}"
                f"{toast}</main></body></html>")


class FakeDriver:
    """The browser over a FakeLinkedIn."""

    def __init__(self, site: FakeLinkedIn, *, typing=lambda s: s, show_after_send=True):
        self.site = site
        self.url = ""
        self.state: dict = {}
        self.typing = typing
        self.show_after_send = show_after_send
        self.visited: list[str] = []
        self.clicked: list[str] = []
        self.sent: list[tuple[str, str]] = []
        self.invites: list[tuple[str, str]] = []

    def _person(self):
        m = re.search(r"/in/([^/?#]+)", self.url)
        if m:
            return self.site.people.get(m.group(1))
        m = re.search(r"recipient=([^&]+)", self.url)
        return self.site.person_by_urn(m.group(1)) if m else None

    async def goto(self, url: str):
        self.url = url if url.startswith("http") else BASE + url
        self.visited.append(self.url)
        self.state = {k: v for k, v in self.state.items() if k == "pill_override"}

    async def html(self) -> str:
        return self.site.render(self.url, self.state)

    async def wait(self, seconds: float):
        pass

    async def scroll(self, selector: str, steps: int = 3):
        pass

    async def click(self, selector: str) -> bool:
        self.clicked.append(selector)
        if selector == SEND_BUTTON and self.state.get("composer"):
            p = self._person()
            body = self.state.pop("composer")
            if p is not None:
                self.sent.append((p.slug, body))
                if self.show_after_send:
                    p.thread.append((OWNER, body))
            return True
        return selector == SEND_BUTTON

    async def click_button(self, name_pattern: str, within: str = "") -> bool:
        self.clicked.append(name_pattern)
        rx = re.compile(name_pattern, re.IGNORECASE)
        p = self._person()
        if p is None:
            return False
        if rx.search(f"Invite {p.name} to connect") and not p.connected and not p.pending:
            if self.site.weekly_limit:
                self.state["modal"] = ('<div role="dialog" class="artdeco-modal">You\'ve reached the '
                                       'weekly invitation limit</div>')
            else:
                self.state["modal"] = ('<div role="dialog" class="artdeco-modal"><button>Add a note</button>'
                                       '<button>Send without a note</button></div>')
            return True
        if rx.search("Add a note") and "Add a note" in self.state.get("modal", ""):
            if self.site.note_limit:
                self.state["modal"] = ('<div role="dialog" class="artdeco-modal">You\'ve used all your '
                                       'free personalized invitations this month.<button>Dismiss</button></div>')
            else:
                self.state["modal"] = ('<div role="dialog" class="artdeco-modal"><textarea name="message">'
                                       '</textarea><button>Send</button></div>')
            return True
        if rx.search("Dismiss"):
            self.state["modal"] = ""
            return True
        if (rx.search("Send without a note") and "Send without a note" in self.state.get("modal", "")) or (
                rx.search("Send") and "<textarea" in self.state.get("modal", "")):
            note = self.state.get("note", "") if "<textarea" in self.state.get("modal", "") else ""
            self.invites.append((p.slug, note))
            p.pending = True
            self.state["modal"] = ""
            self.state["toast"] = '<div class="artdeco-toast-item">Invitation sent</div>'
            return True
        return False

    async def click_button_near(self, anchor_selector: str, name_pattern: str) -> bool:
        m = re.search(r"/in/([^/']+)", anchor_selector)
        p = self.site.people.get(m.group(1)) if m else None
        if p is None or not p.pending:
            return False
        p.pending = False
        self.site.withdrawn.append(p.slug)
        return True

    async def type_text(self, selector: str, text: str) -> bool:
        if selector == COMPOSER:
            self.state["composer"] = self.typing(text)
            return True
        if selector == INVITE_NOTE_BOX and "<textarea" in self.state.get("modal", ""):
            self.state["note"] = self.typing(text)
            return True
        return False

    async def clear(self, selector: str):
        self.state.pop("composer" if selector == COMPOSER else "note", None)

    async def text_of(self, selector: str) -> str:
        return self.state.get("composer", "") if selector == COMPOSER else ""

    async def value_of(self, selector: str) -> str:
        return self.state.get("note", "")


class DriverFactory:
    """agent.driver_factory: hands out the same FakeDriver each run."""

    def __init__(self, driver):
        self.driver = driver
        self.opened = 0

    def __call__(self):
        factory = self

        class _Session:
            async def __aenter__(self_inner):
                factory.opened += 1
                return factory.driver

            async def __aexit__(self_inner, *exc):
                return False
        return _Session()


class FakeBrain:
    def __init__(self, intent: str = "question", text: str = ""):
        self.intent = intent
        self.text = text or "Your checkout page asks for an account first. Want a short list of fixes?"
        self.calls: list[tuple[str, str]] = []
        self.paused_reason = ""

    async def think(self, prompt, session_id=None, agent="", task="", **kw):
        self.calls.append((task, prompt))
        if task == "classify_intent":
            return self.intent
        if task == "draft_invite":
            return "Fellow store owner here, would be good to connect."
        return self.text

    def load_prompt(self, name, **kw):
        return f"PROMPT {name} for {kw.get('persona_name', '')}"

    def load_skill(self, name):
        return ""


MINIMAL = {
    "persona": {"name": OWNER, "company": "Example Studio", "role": "Founder",
                "email": "sam@example.com", "linkedin": "", "tone": "plain"},
    "product": {"name": "Rebuilds", "description": "Store rebuilds", "pricing": "$1",
                "key_benefits": ["x"], "objection_responses": {}},
    "icp": {"industries": ["Retail"], "company_size": "1-10", "titles": ["Founder"],
            "geography": ["US"]},
}


def make_config(**linkedin) -> HarveyConfig:
    base = {"enabled": True, "min_delay_seconds": 0, "max_delay_seconds": 0,
            "break_min_seconds": 0, "break_max_seconds": 0}
    base.update(linkedin)
    return HarveyConfig(**{**MINIMAL, "channels": {"linkedin": base}})


# A weekday inside 10:00-19:00 IST: Wednesday 2026-11-04 12:00 IST.
IN_HOURS = datetime(2026, 11, 4, 6, 30, tzinfo=timezone.utc)
