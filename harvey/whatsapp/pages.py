"""Everything Harvey knows about WhatsApp Web's page: every selector, in one
place, and pure parsing of its HTML.

NONE OF THESE SELECTORS HAS BEEN CHECKED AGAINST THE LIVE SITE. They are written
from what WhatsApp Web is known to use, preferring ARIA roles and stable
attributes over its generated class names, and it changes them without notice.
So this file fails closed: when markup doesn't match, a function returns
nothing, and the agent treats nothing as "not sure", which never sends. When
WhatsApp changes its page, this is the only file to edit.

Nothing here touches a browser: each function takes ``page.content()``'s HTML
and returns plain data, so all of it runs on hand-written fixtures.
"""

import re
import unicodedata
from dataclasses import dataclass

from harvey.linkedin.pages import clean_text, names_match, soup_of
from harvey.whatsapp.numbers import digits_of

BASE = "https://web.whatsapp.com"
HOME_URL = BASE + "/"

# ── Selectors. The driver uses COMPOSER; parsing uses the rest. ──

# The signed-in app: the chat list's own pane.
CHAT_LIST = "#pane-side"
# Linking screen: the QR code.
QR_CODE = "canvas[aria-label*='QR' i], [data-testid='qrcode'], [data-ref] canvas, div[data-ref]"
# A modal on top of everything ("Starting chat...", the invalid-number notice).
DIALOG = "[role='dialog'], [role='alertdialog'], [data-animate-modal-popup='true']"
# WhatsApp's tips and new-feature notes ("Now you can make voice and video calls
# on WhatsApp Web") float in their own layer, anchored to a button. They are
# role=dialog too, but cover nothing: seen live 2026-11-08, it made every chat
# look blocked. They have a Close button, which Harvey presses before typing.
POPOVERS_ID = "wa-popovers-bucket"
POPOVER_CLOSE = (f"#{POPOVERS_ID} [role='dialog'] button[aria-label='Close'], "
                 f"#{POPOVERS_ID} [role='dialog'] [data-testid$='-close']")
# The open chat.
CHAT_PANE = "#main"
CHAT_FOOTER = "#main footer"
CHAT_TITLE = ("#main header [data-testid='conversation-info-header-chat-title'], "
              "#main header span[title], #main header span[dir='auto']")
# Anything typed: Harvey's draft, never WhatsApp's words.
EDITABLE = "[contenteditable]"
# Inside the open chat only, so the search box can never be typed into.
COMPOSER = ("#main footer [contenteditable='true'][role='textbox'], "
            "#main footer [contenteditable='true']")
# A message: the element carrying WhatsApp's id ("false_<number>@c.us_<id>",
# true_ for ones we sent), and inside it the direction, text and clock icon.
MESSAGE = "#main [data-id]"
MESSAGE_OUT = ".message-out"
MESSAGE_IN = ".message-in"
MESSAGE_TEXT = "[data-testid='selectable-text'], span.selectable-text"
MESSAGE_QUOTE = "[data-testid='quoted-message'], .quoted-mention"
MESSAGE_PENDING = "[data-icon='msg-time']"
# A row of the chat list, its title, and its unread marker.
CHAT_ROW = "#pane-side [role='listitem'], #pane-side [role='row']"
ROW_TITLE = "[data-testid='cell-frame-title'] span[title], span[title]"
ROW_UNREAD = "[aria-label*='unread' i], [data-testid='icon-unread-count']"
# Icons of WhatsApp's own "phone isn't connected" banner.
OFFLINE_ICON = "[data-icon='alert-phone'], [data-icon='alert-computer']"
# Where WhatsApp Web's localStorage keeps its own number ("15555550100:12@c.us").
OWN_NUMBER_KEYS = ("last-wid-md", "last-wid")
# Never read for words: the page's code and metadata aren't its interface.
NOT_INTERFACE = "script, style, noscript, template, head"

# The notice send?phone= shows for a number that isn't on WhatsApp.
_INVALID_NUMBER = ("phone number shared via url is invalid", "isn't on whatsapp",
                   "is not on whatsapp", "not registered on whatsapp")

_BIDI_RE = re.compile("[\u200b-\u200f\u202a-\u202e\u2060-\u2069\ufeff]")
_PHONE_TITLE_RE = re.compile(r"\+?[\d\s().\-\u2010-\u2015]{7,}")


def send_url(phone: str) -> str:
    """The chat for this number. Never with text=: a prefilled message can't be
    checked against the approved draft, and is one click from going out wrong."""
    return f"{BASE}/send?phone={digits_of(phone)}"


def fold(text: str) -> str:
    """Lower case, curly quotes straight: how interface text is compared."""
    return clean_text(text).lower().replace("’", "'")


# ── Which screen is this? ──

def qr_visible(html: str) -> bool:
    return bool(soup_of(html).select_one(QR_CODE))


def app_ready(html: str) -> bool:
    """The signed-in app has drawn its chat list, and no QR code is up."""
    s = soup_of(html)
    return bool(s.select_one(CHAT_LIST)) and not s.select_one(QR_CODE)


def invalid_number(html: str) -> bool:
    """WhatsApp's own notice that this number has no WhatsApp."""
    for el in soup_of(html).select(DIALOG):
        text = fold(el.get_text(" "))
        if any(p in text for p in _INVALID_NUMBER):
            return True
    return False


def blocking_dialogs(soup) -> list:
    """Dialogs that can be on top of the chat: every one but a tip in the popover layer."""
    return [d for d in soup.select(DIALOG) if not d.find_parent(id=POPOVERS_ID)]


def chat_open(html: str) -> bool:
    """A chat is open and nothing is on top of it: no dialog ("Starting
    chat..." or anything else but a tip), and the composer is there."""
    s = soup_of(html)
    return not blocking_dialogs(s) and bool(s.select_one(COMPOSER))


def tip_showing(html: str) -> bool:
    """A tip with a Close button is up (it's closed before anything is typed)."""
    return bool(soup_of(html).select_one(POPOVER_CLOSE))


# ── Which chat is this? ──

def title_digits(title: str) -> str:
    """The digits of a title that is nothing but a phone number, or "". A name
    with digits in it ("Suite 415 Dental") is not a number."""
    text = _BIDI_RE.sub("", unicodedata.normalize("NFKC", title or "")).strip()
    return digits_of(text) if _PHONE_TITLE_RE.fullmatch(text) else ""


def chat_title(html: str) -> str:
    el = soup_of(html).select_one(CHAT_TITLE)
    if el is None:
        return ""
    return clean_text(el.get("title") or el.get_text(" "))


def chat_is_for(html: str, phone: str, name: str = "") -> bool:
    """The open chat is this contact's: its title is their number, or (a number
    saved on the phone, a business account) their name. Anything else, and
    certainly no title at all, is "not sure"."""
    title = chat_title(html)
    if not title:
        return False
    digits = title_digits(title)
    if digits:
        return digits == digits_of(phone)
    return bool(name) and names_match(title, name)


# ── The messages of the open chat ──

@dataclass
class ChatMessage:
    direction: str        # "in" or "out"
    body: str
    wa_id: str = ""
    pending: bool = False  # the clock icon: not gone yet
    text: bool = True      # False: a photo, voice note or file with no text


def _body_of(element) -> str:
    for quote in element.select(MESSAGE_QUOTE):
        quote.decompose()
    el = element.select_one(MESSAGE_TEXT)
    if el is None:
        return ""
    for img in el.find_all("img"):
        img.replace_with(img.get("alt") or "")
    return clean_text(el.get_text(" "))


def parse_messages(html: str) -> list[ChatMessage]:
    """The open chat's messages, oldest first. A row whose direction can't be
    told is left out, never guessed: it could be read as their words."""
    s = soup_of(html)
    out = []
    for row in s.select(MESSAGE):
        if row.find_parent(attrs={"data-id": True}) is not None:
            continue
        wa_id = row.get("data-id") or ""
        is_out = bool(row.select_one(MESSAGE_OUT)) or "message-out" in (row.get("class") or [])
        is_in = bool(row.select_one(MESSAGE_IN)) or "message-in" in (row.get("class") or [])
        if is_out == is_in:
            continue
        pending = bool(row.select_one(MESSAGE_PENDING))
        body = _body_of(row)
        out.append(ChatMessage(direction="out" if is_out else "in", body=body, wa_id=wa_id,
                               pending=pending, text=bool(body)))
    return out


# ── The chat list ──

@dataclass
class ChatRow:
    """Only what deciding whether to open it takes. The preview of the last
    message is deliberately not read: most rows aren't ours to read at all."""
    title: str
    digits: str
    unread: bool


def parse_chat_list(html: str) -> list[ChatRow]:
    rows, seen = [], set()
    for row in soup_of(html).select(CHAT_ROW):
        el = row.select_one(ROW_TITLE)
        title = clean_text((el.get("title") or el.get_text(" ")) if el else "")
        if not title or title in seen:
            continue
        seen.add(title)
        rows.append(ChatRow(title=title, digits=title_digits(title),
                            unread=bool(row.select_one(ROW_UNREAD))))
    return rows
