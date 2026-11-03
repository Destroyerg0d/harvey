"""Pure parsing of LinkedIn page HTML.

Nothing here touches a browser: each function takes the page's HTML (as
``page.content()`` returns it) and returns plain data, so all of it is
tested on hand-written fixtures. The selectors are the ones LinkedIn's
messaging, profile and My Network pages used when this was written; when
LinkedIn changes its markup these return nothing rather than guessing, and
the agent treats "nothing" as "can't confirm", never as success.
"""

import re
import unicodedata
from dataclasses import dataclass, field
from datetime import date, datetime
from urllib.parse import parse_qs, quote, unquote, urlencode, urljoin, urlparse

from bs4 import BeautifulSoup

BASE = "https://www.linkedin.com"
LOGIN_URL = BASE + "/login"
FEED_URL = BASE + "/feed/"
MESSAGING_URL = BASE + "/messaging/"
CONNECTIONS_URL = BASE + "/mynetwork/invite-connect/connections/"
SENT_INVITES_URL = BASE + "/mynetwork/invitation-manager/sent/"

# Live-page selectors the driver clicks, types into and scrolls.
COMPOSER = ".msg-form__contenteditable"
SEND_BUTTON = ".msg-form__send-button"
THREAD_HEADER = ".msg-entity-lockup__entity-title"
CONVERSATION_LIST = ".msg-conversations-container__conversations-list"
CONNECTIONS_LIST = "main"
INVITE_NOTE_BOX = "textarea[name='message'], textarea#custom-message"

_SLUG_RE = re.compile(r"/in/([^/?#]+)", re.IGNORECASE)
_CONNECTED_ON_RE = re.compile(r"Connected\s+on\s+([A-Z][a-z]+\.?\s+\d{1,2},\s+\d{4})")
_SENT_AGO_RE = re.compile(
    r"\bSent\s+(?:(today|yesterday)|(\d+|an?|one)\s+(minute|hour|day|week|month|year)s?\s+ago)",
    re.IGNORECASE)


def soup_of(html: str) -> BeautifulSoup:
    return BeautifulSoup(html or "", "html.parser")


def clean_text(text: str) -> str:
    """Whitespace collapsed, ends trimmed: how two texts are compared."""
    return " ".join(unicodedata.normalize("NFKC", text or "").split())


def _name_tokens(name: str) -> list[str]:
    name = unicodedata.normalize("NFKD", name or "")
    name = "".join(c for c in name if not unicodedata.combining(c)).casefold()
    # Credentials and pronouns after a comma or in brackets aren't the name.
    name = re.split(r",|\(|\|", name)[0]
    return [t for t in re.findall(r"[^\W\d_]+", name) if t]


def names_match(a: str, b: str) -> bool:
    """Two display names are the same person's: same first and last word
    (case, accents and "Jane Doe, MBA" style suffixes aside)."""
    ta, tb = _name_tokens(a), _name_tokens(b)
    if not ta or not tb:
        return False
    if ta == tb:
        return True
    return ta[0] == tb[0] and ta[-1] == tb[-1] and (len(ta) > 1 or len(tb) == 1)


def slug_from_url(url: str) -> str:
    """'jane-doe-1a2b' from any /in/ URL ('' when it isn't a profile)."""
    m = _SLUG_RE.search(url or "")
    return unquote(m.group(1)).strip().lower() if m else ""


def profile_url(slug: str) -> str:
    return f"{BASE}/in/{quote(slug, safe='-_.~%')}/"


def _abs(href: str) -> str:
    return urljoin(BASE + "/", href or "")


# ── Signed in? ──

# First path segments of LinkedIn's sign-in, sign-up and auth-wall pages.
SIGNED_OUT_PATHS = ("login", "uas", "authwall", "signup", "signin")


def path_segments(url: str) -> tuple[str, list[str]]:
    """(first path segment, all of them), lower-cased: '/in/loginov/' is a
    profile, not the login page."""
    segments = [x for x in urlparse(url or "").path.lower().split("/") if x]
    return (segments[0] if segments else ""), segments


def signed_in_from(url: str, html: str) -> bool:
    """True when the page is LinkedIn's own, signed-in app: not a login,
    sign-up, challenge or auth wall, and the global navigation is there."""
    first, _ = path_segments(url)
    if first in SIGNED_OUT_PATHS or first == "checkpoint":
        return False
    s = soup_of(html)
    return bool(s.select_one("#global-nav, .global-nav, .global-nav__me, "
                             "[data-test-global-nav-link], header.global-nav__content"))


# ── Messaging ──

@dataclass
class ConversationCard:
    name: str
    snippet: str
    we_sent_last: bool
    time: str = ""
    thread_url: str = ""
    unread: bool = False


def parse_conversation_list(html: str) -> list[ConversationCard]:
    cards = []
    for li in soup_of(html).select("li.msg-conversation-listitem"):
        name_el = li.select_one("h3")
        snippet_el = li.select_one(".msg-conversation-card__message-snippet")
        name = clean_text(name_el.get_text(" ")) if name_el else ""
        snippet = clean_text(snippet_el.get_text(" ")) if snippet_el else ""
        if not name:
            continue
        we_last = snippet.lower().startswith("you:")
        if we_last:
            snippet = snippet[4:].strip()
        time_el = li.select_one("time")
        link = li.select_one("a[href*='/messaging/thread/']")
        classes = " ".join(" ".join(el.get("class") or []) for el in [li, *li.find_all(True)])
        cards.append(ConversationCard(
            name=name, snippet=snippet, we_sent_last=we_last,
            time=clean_text(time_el.get_text(" ")) if time_el else "",
            thread_url=_abs(link.get("href")).split("?")[0] if link else "",
            unread="unread" in classes))
    return cards


@dataclass
class ThreadMessage:
    sender: str
    body: str


@dataclass
class Thread:
    header: str
    messages: list[ThreadMessage] = field(default_factory=list)

    def last(self) -> ThreadMessage | None:
        return self.messages[-1] if self.messages else None


def parse_thread(html: str) -> Thread:
    """A conversation's messages in order. LinkedIn names the sender once
    per group of consecutive messages, so a body takes the last name seen."""
    s = soup_of(html)
    header_el = s.select_one(THREAD_HEADER)
    thread = Thread(header=clean_text(header_el.get_text(" ")) if header_el else "")
    sender = ""
    for el in s.select(".msg-s-message-group__name, .msg-s-event-listitem__body"):
        classes = el.get("class") or []
        if "msg-s-message-group__name" in classes:
            sender = clean_text(el.get_text(" "))
            continue
        body = el.get_text("\n").strip()
        body = "\n".join(clean_text(line) for line in body.split("\n")).strip()
        body = re.sub(r"\n{3,}", "\n\n", body)
        if body:
            thread.messages.append(ThreadMessage(sender=sender, body=body))
    return thread


def composer_text(html: str) -> str:
    el = soup_of(html).select_one(COMPOSER)
    return clean_text(el.get_text(" ")) if el else ""


def compose_link_from_profile(html: str, lead_name: str) -> str:
    """The compose URL behind the profile's own Message button, with only
    its profileUrn and recipient parameters: the first compose link that
    isn't labelled "Message <someone else>" (the sidebar's people)."""
    for a in soup_of(html).select("a[href*='/messaging/compose/']"):
        label = clean_text(a.get("aria-label") or "")
        if label.lower().startswith("message ") and label[8:].strip():
            if not names_match(label[8:], lead_name):
                continue
        parsed = urlparse(_abs(a.get("href")))
        query = parse_qs(parsed.query)
        keep = {k: query[k][0] for k in ("profileUrn", "recipient") if query.get(k)}
        if not keep:
            continue
        return f"{BASE}{parsed.path}?{urlencode(keep)}"
    return ""


def compose_recipient_names(html: str) -> list[str]:
    """Names shown as the compose page's recipient: elements whose class
    has "recipient" or "pill", plus the thread header when it opened one."""
    s = soup_of(html)
    names: list[str] = []
    for el in s.find_all(class_=lambda c: bool(c) and ("recipient" in c or "pill" in c)):
        text = clean_text(el.get_text(" "))
        if text and text not in names and len(text) <= 120:
            names.append(text)
    header = s.select_one(THREAD_HEADER)
    if header and clean_text(header.get_text(" ")) not in names:
        names.append(clean_text(header.get_text(" ")))
    return [n for n in names if n]


def recipient_confirmed(html: str, lead_name: str) -> bool:
    return any(names_match(n, lead_name) or _contains_name(n, lead_name)
               for n in compose_recipient_names(html))


def _contains_name(text: str, name: str) -> bool:
    """'Remove Jane Doe' or 'Jane Doe Founder at X' still names Jane Doe."""
    tokens, want = _name_tokens(text), _name_tokens(name)
    if not want:
        return False
    for i in range(len(tokens) - len(want) + 1):
        if tokens[i:i + len(want)] == want:
            return True
    return False


# ── My Network ──

@dataclass
class Connection:
    slug: str
    name: str
    connected_on: date | None
    profile_url: str = ""


def _card_with_profile(node):
    """The nearest ancestor of ``node`` holding a /in/ link."""
    el = node.parent
    while el is not None and getattr(el, "name", None) not in (None, "[document]"):
        link = el.select_one("a[href*='/in/']") if hasattr(el, "select_one") else None
        if link is not None:
            return el, link
        el = el.parent
    return None, None


def _link_name(card, link) -> str:
    for el in (link, *card.select("a[href*='/in/']")):
        text = clean_text(el.get_text(" "))
        if text:
            return text
    return clean_text(link.get("aria-label") or "")


def parse_connections(html: str) -> list[Connection]:
    """Connection cards: who, and the date in "Connected on Oct 28, 2026"."""
    out, seen = [], set()
    s = soup_of(html)
    for node in s.find_all(string=_CONNECTED_ON_RE):
        card, link = _card_with_profile(node)
        if card is None:
            continue
        slug = slug_from_url(link.get("href"))
        if not slug or slug in seen:
            continue
        seen.add(slug)
        m = _CONNECTED_ON_RE.search(card.get_text(" "))
        when = None
        if m:
            raw = m.group(1).replace(".", "")
            for fmt in ("%b %d, %Y", "%B %d, %Y"):
                try:
                    when = datetime.strptime(raw, fmt).date()
                    break
                except ValueError:
                    pass
        out.append(Connection(slug=slug, name=_link_name(card, link), connected_on=when,
                              profile_url=profile_url(slug)))
    return out


@dataclass
class SentInvite:
    slug: str
    name: str
    age_days: int | None


def _age_days(match) -> int | None:
    if match.group(1):
        return 0 if match.group(1).lower() == "today" else 1
    n = match.group(2).lower()
    n = 1 if n in ("a", "an", "one") else int(n)
    unit = match.group(3).lower()
    return {"minute": 0, "hour": 0, "day": n, "week": 7 * n, "month": 30 * n,
            "year": 365 * n}[unit]


def parse_sent_invitations(html: str) -> list[SentInvite]:
    """Pending invites on the Sent page, with how long ago each went."""
    out, seen = [], set()
    for node in soup_of(html).find_all(string=_SENT_AGO_RE):
        card, link = _card_with_profile(node)
        if card is None:
            continue
        slug = slug_from_url(link.get("href"))
        if not slug or slug in seen:
            continue
        seen.add(slug)
        out.append(SentInvite(slug=slug, name=_link_name(card, link),
                              age_days=_age_days(_SENT_AGO_RE.search(str(node)))))
    return out


# ── Profile: connect flow ──

def _top_card(s: BeautifulSoup):
    """The profile's own header, not the "people also viewed" sidebar."""
    for selector in ("main .pv-top-card", "main section.artdeco-card", "main"):
        el = s.select_one(selector)
        if el is not None:
            return el
    return s


def _buttons(scope) -> list:
    return scope.select("button, [role='button'], a[aria-label], div[role='menuitem']")


def _label(el) -> str:
    return clean_text(el.get("aria-label") or el.get_text(" "))


def profile_connect_state(html: str) -> str:
    """'pending' (an invite is out), 'connected' (1st degree: a Message
    button and no Connect), 'connect' (a Connect button on the card),
    'more' (Connect may be under More), or 'unknown'."""
    s = soup_of(html)
    card = _top_card(s)
    labels = [_label(b) for b in _buttons(card)]
    low = [x.lower() for x in labels]
    if any(x == "pending" or x.startswith("pending,") or "withdraw invitation" in x
           or x.startswith("pending ") for x in low):
        return "pending"
    if any(x == "connect" or (x.startswith("invite ") and x.endswith(" to connect")) for x in low):
        return "connect"
    if card.find(string=re.compile(r"\b1st\b")) and any(x.startswith("message") for x in low):
        return "connected"
    if any(x == "more" or x.startswith("more actions") for x in low):
        return "more"
    return "unknown"


_NOTE_LIMIT_PATTERNS = (
    "free personalized invitation", "personalized invitations left",
    "used all your free personalized", "out of personalized invitations",
    "personalize all your invitations with premium", "unlimited personalized invitations",
    "you've used your", "reached your monthly limit of personalized",
)
_INVITE_SENT_PATTERNS = ("invitation sent", "your invitation to", "invitation was sent")


def note_limit_reached(html: str) -> bool:
    """LinkedIn said this (free) account can't add a note right now."""
    text = clean_text(soup_of(html).get_text(" ")).lower().replace("’", "'")
    return any(p in text for p in _NOTE_LIMIT_PATTERNS)


def invite_modal_open(html: str) -> bool:
    s = soup_of(html)
    for b in _buttons(s):
        x = _label(b).lower()
        if x in ("add a note", "send without a note", "send now", "send", "send invitation"):
            return True
    return False


def invite_confirmed(html: str) -> bool:
    """The invite went: LinkedIn's toast says so, or the card shows Pending."""
    text = clean_text(soup_of(html).get_text(" ")).lower()
    return any(p in text for p in _INVITE_SENT_PATTERNS) or profile_connect_state(html) == "pending"
