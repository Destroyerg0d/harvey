"""A pretend WhatsApp Web for the WhatsApp tests: no network, no browser.

FakeWhatsApp renders hand-written pages in the shape harvey/whatsapp/pages.py
reads, and FakeDriver plays the browser over it. Every person here is invented,
and every number is one nobody can have: US 555-01xx and UK 07700 900xxx are
reserved for fiction, and +91 12345 is an obvious placeholder.
"""

import asyncio
import html as htmlmod
import random
from dataclasses import dataclass, field
from datetime import datetime, timezone
from urllib.parse import parse_qs, urlparse

from harvey.agents.whatsapp_agent import WhatsAppAgent
from harvey.config import HarveyConfig
from harvey.state import StateManager
from harvey.whatsapp.numbers import digits_of
from harvey.whatsapp.pacing import mark_linked
from harvey.whatsapp.pages import COMPOSER

JANE = "+447700900123"
RAVI = "+911234567890"
SAM = "+14155550100"
STRANGER = "+447700900999"

QR_PAGE = ('<html><body><div id="app"><div><h1>Log into WhatsApp Web</h1>'
           '<canvas aria-label="Scan this QR code to link a device!"></canvas></div></div></body></html>')


def e(text: str) -> str:
    return htmlmod.escape(text or "")


@dataclass
class Person:
    phone: str
    title: str = ""                     # what the chat is titled ("" = the number)
    on_whatsapp: bool = True
    thread: list = field(default_factory=list)   # [(direction, body)]
    unread: bool = False

    @property
    def digits(self) -> str:
        return digits_of(self.phone)

    @property
    def shown(self) -> str:
        if self.title:
            return self.title
        d = self.digits
        return f"+{d[:2]} {d[2:6]} {d[6:]}"


class FakeWhatsApp:
    def __init__(self, people=(), *, linked=True, banner="", dialog="", whole_page="", footer_note=""):
        self.people = {p.digits: p for p in people}
        self.linked = linked
        self.banner = banner            # WhatsApp's own notice above the chat list
        self.dialog = dialog            # a modal over everything
        self.whole_page = whole_page    # a page of its own, no app
        self.footer_note = footer_note  # WhatsApp's note in the chat's footer
        self.next_message_id = 0

    def _row(self, p: Person) -> str:
        badge = '<span aria-label="1 unread message">1</span>' if p.unread else ""
        return (f'<div role="listitem"><span dir="auto" title="{e(p.shown)}">{e(p.shown)}</span>'
                f'<span title="the latest message preview">the latest message preview</span>{badge}</div>')

    def _bubble(self, p: Person, i: int, direction: str, body: str, pending: bool = False) -> str:
        prefix = "true" if direction == "out" else "false"
        icon = ('<span data-icon="msg-time"></span>' if pending else '<span data-icon="msg-dblcheck"></span>') \
            if direction == "out" else ""
        return (f'<div role="row"><div data-id="{prefix}_{p.digits}@c.us_{i}">'
                f'<div class="message-{direction}"><div class="copyable-text">'
                f'<span class="selectable-text copyable-text"><span>{e(body)}</span></span></div>{icon}'
                f'</div></div></div>')

    def _chat(self, p: Person, composer: str, title: str, pending: bool) -> str:
        last = len(p.thread) - 1
        rows = "".join(self._bubble(p, i, d, b, pending and i == last and d == "out")
                       for i, (d, b) in enumerate(p.thread))
        box = f'<div contenteditable="true" role="textbox">{e(composer)}</div>'
        return (f'<div id="main"><header><span dir="auto" title="{e(title)}">{e(title)}</span></header>'
                f'<div class="msgs">{rows}</div><footer>{e(self.footer_note)}{box}</footer></div>')

    def render(self, url: str, driver) -> str:
        if self.whole_page:
            return f'<html><body><div id="app"><div>{e(self.whole_page)}</div></div></body></html>'
        if not self.linked:
            return QR_PAGE
        modal = f'<div role="dialog">{e(self.dialog)}</div>' if self.dialog else ""
        rows = "".join(self._row(p) for p in self.people.values() if p.on_whatsapp)
        side = (f'<div id="side"><header>Chats</header>{e(self.banner)}'
                f'<div id="pane-side" aria-label="Chat list">{rows}</div></div>')
        phone = (parse_qs(urlparse(url).query).get("phone") or [""])[0]
        chat = ""
        if phone:
            p = self.people.get(phone) or Person(phone=phone, on_whatsapp=False)
            if not p.on_whatsapp:
                modal = '<div role="dialog" data-animate-modal-popup="true">Phone number shared via url is invalid.</div>'
            elif not driver.no_chat:
                chat = self._chat(p, driver.composer, driver.title_override or p.shown, driver.pending_after_send)
        return f'<html><body><div id="app">{side}{chat}{modal}</div></body></html>'


class FakeDriver:
    """The browser over a FakeWhatsApp."""

    def __init__(self, site: FakeWhatsApp, *, typing=lambda s: s, show_after_send=True,
                 pending_after_send=False, no_composer=False, no_chat=False, title_override="",
                 on_type=None, crash_on_enter=False):
        self.site = site
        self.url = ""
        self.composer = ""
        self.typing = typing
        self.show_after_send = show_after_send
        self.pending_after_send = pending_after_send
        self.no_composer = no_composer
        self.no_chat = no_chat
        self.title_override = title_override
        self.on_type = on_type
        self.crash_on_enter = crash_on_enter
        self.visited: list[str] = []
        self.typed: list[str] = []
        self.sent: list[tuple[str, str]] = []
        self.enter_presses = 0

    def _person(self):
        phone = (parse_qs(urlparse(self.url).query).get("phone") or [""])[0]
        return self.site.people.get(phone)

    async def goto(self, url: str):
        self.url = url
        self.visited.append(url)
        self.composer = ""

    async def html(self) -> str:
        return self.site.render(self.url, self)

    async def wait(self, seconds: float):
        pass

    async def scroll(self, selector: str, steps: int = 3):
        pass

    async def type_text(self, selector: str, text: str) -> bool:
        if selector != COMPOSER or self.no_composer or self._person() is None:
            return False
        self.typed.append(text)
        self.composer = self.typing(text)
        if self.on_type:
            await self.on_type()
        return True

    async def text_of(self, selector: str) -> str:
        return self.composer if selector == COMPOSER and not self.no_composer else ""

    async def clear(self, selector: str):
        self.composer = ""

    async def press_enter(self):
        self.enter_presses += 1
        if self.crash_on_enter:
            raise RuntimeError("the browser went away")
        p = self._person()
        if p is not None and self.composer:
            self.sent.append((p.phone, self.composer))
            if self.show_after_send:
                p.thread.append(("out", self.composer))
            self.composer = ""


class DriverFactory:
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


FIRST = "Hi Jane, you left this number on our website about a store rebuild. Happy to share two ideas here. Reply no and I won't message again."
FOLLOW_UP = "Jane, still happy to send those two ideas if useful. Should I?"
REPLY = "Thanks Jane. It depends on the work, so a short chat is the easiest way to tell."


class FakeBrain:
    def __init__(self, intent: str = "question", drafts=None):
        self.intent = intent
        self.drafts = drafts or {}      # task -> text, or a list used up in order
        self.calls: list[tuple[str, str]] = []
        self.paused_reason = ""

    async def think(self, prompt, session_id=None, agent="", task="", **kw):
        self.calls.append((task, prompt))
        if task == "classify_intent":
            return self.intent
        if task in self.drafts:
            value = self.drafts[task]
            return value.pop(0) if isinstance(value, list) else value
        return {"draft_first": FIRST, "draft_follow_up": FOLLOW_UP, "draft_reply": REPLY}[task]

    def load_prompt(self, name, **kw):
        return f"PROMPT {name} for {kw.get('persona_name', '')}"

    def load_skill(self, name):
        return ""


MINIMAL = {
    "persona": {"name": "Sam Owner", "company": "Example Studio", "role": "Founder",
                "email": "sam@example.com", "linkedin": "", "tone": "plain"},
    "product": {"name": "Rebuilds", "description": "Store rebuilds", "pricing": "$1",
                "key_benefits": ["x"], "objection_responses": {}},
    "icp": {"industries": ["Retail"], "company_size": "1-10", "titles": ["Founder"],
            "geography": ["US"]},
}


def make_config(**whatsapp) -> HarveyConfig:
    base = {"enabled": True, "number": "+15555550100", "min_delay_seconds": 0, "max_delay_seconds": 0,
            "break_min_seconds": 0, "break_max_seconds": 0,
            "hours": {"start": "10:00", "end": "19:00", "timezone": "Europe/London"}}
    base.update(whatsapp)
    return HarveyConfig(**{**MINIMAL, "channels": {"whatsapp": base}})


# A Wednesday when the window is open in London (18:30), New York (13:30) and
# Los Angeles (10:30), and shut in India (00:00 on Thursday).
OPEN = datetime(2026, 11, 4, 18, 30, tzinfo=timezone.utc)


def run(coro):
    return asyncio.run(coro)


class World:
    def __init__(self, tmp_path, people=(), brain=None, at=OPEN, driver_kwargs=None, site_kwargs=None,
                 linked=True, **cfg):
        self.state = StateManager(str(tmp_path / "h.db"))
        run(self.state.init_db())
        self.config = make_config(**cfg)
        self.site = FakeWhatsApp(people, **(site_kwargs or {}))
        self.driver = FakeDriver(self.site, **(driver_kwargs or {}))
        self.brain = brain or FakeBrain()
        self.at = at
        if linked:
            run(mark_linked(self.state, self.config.channels.whatsapp,
                            at=at.astimezone(timezone.utc).replace(tzinfo=None)))

    def agent(self):
        async def no_sleep(_):
            pass
        return WhatsAppAgent(self.brain, self.state, self.config, driver_factory=DriverFactory(self.driver),
                             now=lambda: self.at, sleep=no_sleep, rng=random.Random(3))

    def go(self, **kw):
        return run(self.agent().run(ignore_interval=True, **kw))

    def contact(self, phone=JANE, stage="queued", **fields):
        fields.setdefault("source", "website contact form")
        fields.setdefault("name", "Jane Doe")
        contact_id, _ = run(self.state.wa_add_contact(phone=phone, stage=stage, **fields))
        return contact_id

    def approved(self, contact_id, kind="first", body=FIRST):
        return run(self.state.wa_add_message(contact_id, kind=kind, body=body, status="approved",
                                             approved_by="you"))

    def msgs(self, contact_id, **kw):
        return run(self.state.wa_messages(contact_id=contact_id, **kw))

    def get(self, message_id):
        return run(self.state.wa_get_message(message_id))

    def stage(self, contact_id):
        return run(self.state.wa_get_contact(contact_id))["stage"]
