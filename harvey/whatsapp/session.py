"""The dedicated WhatsApp Chrome profile, and linking the phone.

The installed Google Chrome with a persistent profile of its own in
data/whatsapp-profile, always visible on screen: there is no headless setting.
LinkedIn's session code does the work; this adds pressing Enter.

`harvey whatsapp login`, or the Connect button on the dashboard's WhatsApp tab,
which starts the same command, opens web.whatsapp.com and waits while the owner
links the phone by QR code. Harvey never handles a code. The UI language is forced
to English because the breakers read WhatsApp's own words: in another language a
ban notice wouldn't match anything.
"""

import asyncio
import logging
import re
from contextlib import asynccontextmanager

from harvey.linkedin.session import ChromeNotFound, Driver, open_session as _open_chrome, profile_dir
from harvey.whatsapp.guards import HARD_STOPS, detect_breaker
from harvey.whatsapp.pages import HOME_URL, OWN_NUMBER_KEYS, app_ready

logger = logging.getLogger("harvey.whatsapp")

__all__ = ["ChromeNotFound", "WhatsAppDriver", "is_linked", "login_interactive", "open_session",
           "profile_dir"]

# After the chat list appears WhatsApp is still writing the session to the
# profile; closing at once can lose the link.
LINK_SETTLE_SECONDS = 20


# Milliseconds between keys, and the pause after a line: a person's pace, slower
# than LinkedIn's driver, since typing bursts are part of what a new number is
# judged on. A 50-word message takes about half a minute.
KEY_DELAY_MS = (60, 170)


class WhatsAppDriver(Driver):
    """LinkedIn's driver, typing at a person's pace, plus the key WhatsApp sends with."""

    async def type_text(self, selector: str, text: str) -> bool:
        """A new line is Shift+Enter: in WhatsApp a plain Enter sends the message."""
        loc = await self._first(selector)
        if loc is None:
            return False
        await loc.click(timeout=8000)
        lines = (text or "").split("\n")
        for i, line in enumerate(lines):
            if line:
                await self.page.keyboard.type(line, delay=self.rng.randint(*KEY_DELAY_MS))
            if i < len(lines) - 1:
                await self.page.keyboard.press("Shift+Enter")
        await self.wait(self.rng.uniform(1.0, 3.0))
        return True

    async def press_enter(self) -> None:
        await self.page.keyboard.press("Enter")


@asynccontextmanager
async def open_session(cfg):
    """The dedicated profile in system Chrome, visible: yields a WhatsAppDriver."""
    try:
        session = _open_chrome(cfg, headless=False, locale="en-US")
        async with session as driver:
            yield WhatsAppDriver(driver.page, driver.rng)
    except ChromeNotFound as e:
        raise ChromeNotFound(str(e).replace("channels.linkedin", "channels.whatsapp")) from None


async def is_linked(driver) -> bool:
    """Whether the profile's number is linked, judged from the page it is on."""
    try:
        return app_ready(await driver.html())
    except Exception:
        return False


async def own_number(driver) -> str:
    """The linked number, from what WhatsApp Web keeps in its own storage, so the
    dashboard can say which phone it is without a config edit. Only ever a label:
    this hasn't been checked against the live site, and anything unexpected comes
    back as "" (channels.whatsapp.number is then what's shown)."""
    page = getattr(driver, "page", None)
    if page is None:
        return ""
    for key in OWN_NUMBER_KEYS:
        try:
            raw = await page.evaluate("key => localStorage.getItem(key)", key)
        except Exception:
            return ""
        found = re.search(r"(\d{7,15})(?::\d+)?@", str(raw or ""))
        if found:
            return "+" + found.group(1)
    return ""


async def login_interactive(cfg, poll_seconds: float = 3, say=print, session=None, stopped=None,
                            number_seen=None) -> bool:
    """Open WhatsApp Web in the dedicated profile, visible, and wait for the
    owner to link the phone themself. True once linked. ``stopped`` hears why,
    when the wait ends before its time; ``number_seen``, the linked number."""
    minutes = cfg.login_timeout_minutes

    def stop(reason: str) -> bool:
        say(f"  {reason}.")
        if stopped:
            stopped(reason)
        return False

    async def saw_number(driver) -> None:
        number = await own_number(driver)
        if number and number_seen:
            number_seen(number)

    async with (session or open_session)(cfg) as driver:
        await driver.goto(HOME_URL)
        if await is_linked(driver):
            say("  This profile is already linked to WhatsApp.")
            await saw_number(driver)
            return True
        say("  A Chrome window is open on WhatsApp Web.")
        say("  On the phone with the separate number: WhatsApp > Linked devices > Link a device,")
        say("  then scan the QR code. Harvey never sees or handles a code.")
        say(f"  Waiting up to {int(minutes)} minutes...")
        deadline = asyncio.get_running_loop().time() + minutes * 60
        while asyncio.get_running_loop().time() < deadline:
            await asyncio.sleep(poll_seconds)
            if getattr(driver, "closed", False):
                return stop("The Chrome window was closed")
            if await is_linked(driver):
                await driver.wait(LINK_SETTLE_SECONDS)
                await saw_number(driver)
                return True
            try:
                breaker = detect_breaker(driver.url, await driver.html())
            except Exception:
                continue
            if breaker and breaker.kind in HARD_STOPS:
                return stop(breaker.reason)
        return False
