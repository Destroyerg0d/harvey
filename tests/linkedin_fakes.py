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


def thread_html(header: str, messages, composer: str = "", recipient_pill: str = "", slug: str = "") -> str:
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
        + (f'<a href="https://www.linkedin.com/in/{slug}/" aria-label="{e(header)}">View profile</a>' if slug else '')
        + f'<ul class="msg-s-message-list">{"".join(groups)}</ul>'
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
    headline: str = ""                # the top card's line under the name
    location: str = ""                # the top card's location line
    plain_card: bool = False          # LinkedIn's page today: no h1, degree, company and school lines
    connect_in_more: bool = False     # Follow on the card, Connect under More (3rd degree)
    side_list: tuple = ()             # names in "More profiles for you", each with its own Connect

    @property
    def thread_url(self) -> str:
        return f"{BASE}/messaging/thread/t-{self.slug}/"


class FakeLinkedIn:
    def __init__(self, people=(), *, note_limit=False, weekly_limit=False, breaker_on="", no_note_box=False,
                 live_list=False, unread=(), list_page=0):
        self.no_note_box = no_note_box    # "Add a note" opens a form Harvey can't find a box in
        self.live_list = live_list        # the list as LinkedIn draws it now: no thread links, click to open
        self.unread = set(unread)         # slugs whose conversation is unread
        self.list_page = list_page        # conversations shown before "Load more conversations" (0: all)
        self.loaded_pages = 1
        self.people = {p.slug: p for p in people}
        self.note_limit = note_limit
        self.weekly_limit = weekly_limit
        self.breaker_on = breaker_on      # a URL that shows "unusual activity"
        self.stray_invites: list[str] = []  # invites that went to someone in a side list
        self.signed_in = True
        self.withdrawn: list[str] = []

    def listed(self) -> list:
        """The conversations the list shows, in order (Load more adds a page)."""
        people = [p for p in self.people.values() if p.thread]
        return people[:self.list_page * self.loaded_pages] if self.list_page else people

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
            listed = self.listed()
            for p in listed:
                sender, body = p.thread[-1]
                snippet = ("You: " if sender == OWNER else "") + body
                unread = " msg-conversation-card--unread" if p.slug in self.unread else ""
                opener = (f'<div class="msg-conversation-listitem__link{unread}">' if self.live_list
                          else f'<a class="{unread.strip()}" href="{p.thread_url}">')
                closer = "</div>" if self.live_list else "</a>"
                items.append(f'<li class="msg-conversation-listitem">{opener}'
                             f'<h3>{e(p.name)}</h3><p class="msg-conversation-card__message-snippet">'
                             f'{e(snippet)}</p><time>2:14 PM</time>{closer}</li>')
            more = ("<button>Load more conversations</button>"
                    if len(listed) < len([p for p in self.people.values() if p.thread]) else "")
            return page(f'<ul class="msg-conversations-container__conversations-list">{"".join(items)}</ul>{more}')
        if "/messaging/thread/" in url:
            slug = url.rstrip("/").rsplit("/t-", 1)[-1]
            p = self.people[slug]
            return thread_html(p.name, p.thread, state.get("composer", ""), slug=p.slug)
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
        side = ""
        if p.side_list:
            side = "<aside><h2>More profiles for you</h2>" + "".join(
                f'<button aria-label="Invite {e(other)} to connect">Connect</button>' for other in p.side_list) + "</aside>"
        if p.connect_in_more and not p.connected and not p.pending:
            menu = (f'<div class="artdeco-dropdown__content--is-open"><a aria-label="Invite {e(p.name)} to connect" '
                    f'href="/preload/custom-invite/?vanityName={p.slug}">Connect</a></div>'
                    if state.get("menu_open") else "")
            buttons = f'<button aria-label="Follow {e(p.name)}">Follow</button><button>More</button>{menu}'
        elif p.connected:
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
        if p.plain_card:
            # Live, 2026-11-20: the name isn't an h1, and the card carries the
            # degree, the current company and a school before the location.
            return (f"<html><body>{NAV}{sidebar}<main><section class=\"artdeco-card\">"
                    f"<div><p>{e(p.name)}</p><span>· 2nd</span></div>"
                    + (f'<p>{e(p.headline)}</p>' if p.headline else "")
                    + "<p>Example Goods Ltd</p><p>· Example State University</p>"
                    + (f'<p>{e(p.location)}</p>' if p.location else "")
                    + f"{buttons}</section>{side}{modal}{toast}</main></body></html>")
        return (f"<html><body>{NAV}{sidebar}<main><section class=\"artdeco-card pv-top-card\">"
                f"<h1>{e(p.name)}</h1>"
                + (f'<div class="text-body-medium">{e(p.headline)}</div>' if p.headline else "")
                + (f'<span class="text-body-small">{e(p.location)}</span>' if p.location else "")
                + f"{buttons}</section>{side}{modal}"
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
        self.not_loading: list[str] = []     # URL parts whose page never loads
        self.opened: list[str] = []          # conversations opened by a click in the list

    def _person(self):
        m = re.search(r"/in/([^/?#]+)", self.url)
        if m:
            return self.site.people.get(m.group(1))
        m = re.search(r"recipient=([^&]+)", self.url)
        return self.site.person_by_urn(m.group(1)) if m else None

    async def goto(self, url: str):
        target = url if url.startswith("http") else BASE + url
        if any(bit in target for bit in self.not_loading):
            from harvey.linkedin.session import PageNotLoading
            raise PageNotLoading(f"LinkedIn didn't load {target.split('?')[0]}")
        self.url = target
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

    def _open_invite(self, p):
        if self.site.weekly_limit:
            self.state["modal"] = ('<div role="dialog" class="artdeco-modal">You\'ve reached the '
                                   'weekly invitation limit</div>')
        else:
            self.state["modal"] = ('<dialog open><h2>Add a note to your invitation?</h2><button>Add a note</button>'
                                   '<button>Send without a note</button><button>Dismiss</button></dialog>')

    async def open_conversation(self, index: int) -> bool:
        if "/messaging/" not in self.url:
            return False
        listed = self.site.listed()
        if not 0 <= index < len(listed):
            return False
        p = listed[index]
        self.url = p.thread_url
        self.visited.append(self.url)
        self.opened.append(p.slug)
        self.site.unread.discard(p.slug)        # opening it marks it read, as LinkedIn does
        return True

    async def load_more_conversations(self) -> bool:
        if not self.site.list_page or len(self.site.listed()) >= len(
                [p for p in self.site.people.values() if p.thread]):
            return False
        self.site.loaded_pages += 1
        return True

    async def click_connect_for(self, name: str) -> bool:
        from harvey.linkedin.pages import names_match
        self.clicked.append(f"connect-for:{name}")
        p = self._person()
        if p is None or p.connected or p.pending or not names_match(p.name, name):
            return False
        if p.connect_in_more and not self.state.get("menu_open"):
            return False                    # under More, and More isn't open
        self._open_invite(p)
        return True

    async def click_button(self, name_pattern: str, within: str = "") -> bool:
        self.clicked.append(name_pattern)
        rx = re.compile(name_pattern, re.IGNORECASE)
        p = self._person()
        if p is None:
            return False
        for other in p.side_list:
            # A page-wide connect click hits the side list first, and that
            # invite goes at once, to a stranger (live, 2026-11-20).
            if rx.search(f"Invite {other} to connect"):
                self.site.stray_invites.append(other)
                return True
        if rx.search("More") and p.connect_in_more:
            self.state["menu_open"] = True
            return True
        if p.connect_in_more:
            pass                            # their connect is in the menu, not a button
        elif rx.search(f"Invite {p.name} to connect") and not p.connected and not p.pending:
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
            elif self.site.no_note_box:
                self.state["modal"] = ('<dialog open><div contenteditable="true"></div><button>Send</button>'
                                       '<button>Dismiss</button></dialog>')
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
