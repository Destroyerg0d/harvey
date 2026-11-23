"""The dedicated LinkedIn Chrome profile, and the page driver.

Harvey drives the installed Google Chrome (not Playwright's bundled
Chromium) with a persistent profile of its own in data/linkedin-profile,
visible on screen by default. The owner signs that profile in once with
`harvey linkedin login`, or the Connect button on the dashboard's LinkedIn tab,
which starts the same command: Harvey opens the login page and waits. It never
types a password or a 2FA code, and has no way to.

``Driver`` is the small set of things the agent does to a page (go to a
URL, read its HTML, click, type, scroll). The agent only talks to a
driver, so tests run the whole send recipe against a fake one.
"""

import asyncio
import logging
import os
import random
import re
import shutil
import sys
from contextlib import asynccontextmanager
from pathlib import Path

from harvey.linkedin.pages import LOGIN_URL, SIGNED_OUT_PATHS, path_segments, signed_in_from
from harvey.paths import PROJECT_ROOT

logger = logging.getLogger("harvey.linkedin")


class ChromeNotFound(RuntimeError):
    pass


class PageNotLoading(RuntimeError):
    """A LinkedIn page didn't load, after a second try. The run skips that
    item or step and carries on (LinkedInAgent), instead of stopping."""


GOTO_TIMEOUT_MS = 45000


def _is_timeout(error: Exception) -> bool:
    try:
        from playwright.async_api import TimeoutError as PlaywrightTimeout
        if isinstance(error, PlaywrightTimeout):
            return True
    except Exception:
        pass
    return "timeout" in type(error).__name__.lower() or "Timeout" in str(error)[:80]


def _short(url: str) -> str:
    """A page as the owner would name it: its path, no query (profile ids)."""
    return (url or "").split("://", 1)[-1].split("?", 1)[0][:80]


def chrome_candidates() -> list[str]:
    """Where Google Chrome is normally installed on this platform."""
    if sys.platform.startswith("win"):
        roots = [os.environ.get(k, "") for k in ("PROGRAMFILES", "PROGRAMFILES(X86)", "LOCALAPPDATA")]
        roots += [r"C:\Program Files", r"C:\Program Files (x86)"]
        return [str(Path(r) / "Google" / "Chrome" / "Application" / "chrome.exe") for r in roots if r]
    if sys.platform == "darwin":
        return ["/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
                str(Path.home() / "Applications/Google Chrome.app/Contents/MacOS/Google Chrome")]
    found = [shutil.which(n) for n in ("google-chrome", "google-chrome-stable", "chrome")]
    return [f for f in found if f] + ["/usr/bin/google-chrome", "/opt/google/chrome/chrome"]


def find_chrome(override: str = "") -> str:
    """The Chrome executable to drive. ``override`` (channels.linkedin.
    chrome_path) wins; raises ChromeNotFound with what was tried."""
    tried = [override] if override else chrome_candidates()
    for path in tried:
        if path and Path(path).is_file():
            return str(path)
    raise ChromeNotFound(
        "Google Chrome wasn't found (tried: " + ", ".join(p for p in tried if p) + "). "
        "Install it, or set channels.linkedin.chrome_path to chrome's full path.")


def profile_dir(cfg) -> Path:
    path = Path(cfg.profile_dir or "data/linkedin-profile")
    return path if path.is_absolute() else PROJECT_ROOT / path


class Driver:
    """A Playwright page, reduced to what the LinkedIn agent needs."""

    def __init__(self, page, rng: random.Random | None = None):
        self.page = page
        self.rng = rng or random.Random()
        # "Leave site?" (a typed message, an open invite form): accepted, or
        # Playwright's default dismiss cancels the navigation and goto hangs
        # until its timeout (2026-11-11 to 11-20: 11 runs stopped that way).
        try:
            page.on("dialog", self._on_dialog)
        except Exception:
            pass

    async def _on_dialog(self, dialog) -> None:
        try:
            if dialog.type == "beforeunload":
                await dialog.accept()
            else:
                await dialog.dismiss()
        except Exception as e:
            logger.debug(f"LinkedIn: a page dialog couldn't be closed: {e}")

    @property
    def url(self) -> str:
        return self.page.url

    @property
    def closed(self) -> bool:
        return self.page.is_closed()

    async def _arrived(self, before: str) -> bool:
        """The page moved on from ``before`` and has its content (a slow
        LinkedIn page keeps loading long after it is usable)."""
        try:
            state = await self.page.evaluate("document.readyState")
        except Exception:
            return False
        if self.page.url == before or state not in ("interactive", "complete"):
            return False
        try:
            await self.page.evaluate("window.stop()")
        except Exception:
            pass
        return True

    async def goto(self, url: str) -> None:
        """Open a page. One that doesn't load in time is checked (often it
        has its content and is only still loading), then tried once more;
        PageNotLoading when it still won't."""
        before = self.page.url
        try:
            await self.page.goto(url, wait_until="domcontentloaded", timeout=GOTO_TIMEOUT_MS)
        except Exception as e:
            if not _is_timeout(e):
                raise
            if not await self._arrived(before):
                logger.info(f"LinkedIn: {_short(url)} didn't load in time; trying once more.")
                try:
                    await self.page.goto(url, wait_until="commit", timeout=30000)
                    await self.page.wait_for_load_state("domcontentloaded", timeout=20000)
                except Exception as again:
                    if not _is_timeout(again):
                        raise
                    if not await self._arrived(before):
                        raise PageNotLoading(f"LinkedIn didn't load {_short(url)}") from None
        try:
            await self.page.wait_for_load_state("networkidle", timeout=8000)
        except Exception:
            pass  # LinkedIn pages rarely go fully idle
        await self.wait(self.rng.uniform(1.5, 3.5))

    async def html(self) -> str:
        return await self.page.content()

    async def wait(self, seconds: float) -> None:
        await self.page.wait_for_timeout(int(seconds * 1000))

    async def _first(self, selector: str):
        loc = self.page.locator(selector).first
        try:
            if await loc.count() == 0:
                return None
        except Exception:
            return None
        return loc

    async def click(self, selector: str) -> bool:
        loc = await self._first(selector)
        if loc is None:
            return False
        try:
            await loc.scroll_into_view_if_needed(timeout=5000)
            await loc.click(timeout=8000, delay=self.rng.randint(40, 140))
        except Exception as e:
            logger.debug(f"LinkedIn: click {selector} failed: {e}")
            return False
        await self.wait(self.rng.uniform(0.8, 2.0))
        return True

    async def open_conversation(self, index: int) -> bool:
        """Open the ``index``-th conversation in the messaging list by
        clicking it, as a person does: LinkedIn's list carries no thread
        links now (live, 2026-11-23). The thread opens beside the list and
        the address becomes its /messaging/thread/ URL."""
        from harvey.linkedin.pages import CONVERSATION_ITEM, CONVERSATION_ITEM_LINK
        item = self.page.locator(CONVERSATION_ITEM).nth(index)
        try:
            if await item.count() == 0:
                return False
            target = item.locator(CONVERSATION_ITEM_LINK).first
            if await target.count() == 0:
                target = item
            await target.scroll_into_view_if_needed(timeout=5000)
            await target.click(timeout=8000, delay=self.rng.randint(40, 140))
        except Exception as e:
            logger.debug(f"LinkedIn: opening conversation {index} failed: {e}")
            return False
        await self.wait(self.rng.uniform(1.8, 3.0))
        return True

    async def load_more_conversations(self) -> bool:
        """Press the list's "Load more conversations" button, if it shows."""
        return await self.click_button(r"^\s*load more conversations\s*$")

    async def click_button(self, name_pattern: str, within: str = "") -> bool:
        """Click the first button (or menu item) whose accessible name
        matches ``name_pattern`` (a regex, case-insensitive)."""
        scope = self.page.locator(within).first if within else self.page
        name = re.compile(name_pattern, re.IGNORECASE)
        for role in ("button", "menuitem", "link"):
            loc = scope.get_by_role(role, name=name).first
            try:
                if await loc.count() == 0 or not await loc.is_visible():
                    continue
                await loc.click(timeout=8000, delay=self.rng.randint(40, 140))
            except Exception as e:
                logger.debug(f"LinkedIn: click {role} {name_pattern} failed: {e}")
                continue
            await self.wait(self.rng.uniform(0.8, 2.0))
            return True
        return False

    async def click_connect_for(self, name: str) -> bool:
        """Click "Invite <name> to connect" for this person only: on their
        card, or in their More menu once it is open. Never one inside a side
        list of other people ("More profiles for you"), and never one naming
        someone else (2026-11-20: a page-wide click invited 20 strangers)."""
        from harvey.linkedin.pages import names_match
        controls = self.page.locator("button, a, [role=menuitem], [role=button]")
        try:
            found = await controls.evaluate_all(
                "(els) => els.map((e, i) => ({i: i, label: (e.getAttribute('aria-label') || e.innerText || '')"
                ".replace(/\\s+/g, ' ').trim(), aside: !!e.closest('aside'), "
                "visible: !!(e.offsetWidth || e.offsetHeight || e.getClientRects().length)}))")
        except Exception as e:
            logger.debug(f"LinkedIn: reading the connect controls failed: {e}")
            return False
        # The page repeats their connect (the card, a sticky header copy);
        # one copy can refuse the click (covered, off screen), so each is
        # tried in turn (live, 2026-11-20).
        for item in found:
            m = re.match(r"^invite (.+) to connect$", item["label"], re.IGNORECASE)
            if not m or item["aside"] or not item["visible"] or not names_match(m.group(1), name):
                continue
            target = controls.nth(item["i"])
            try:
                await target.scroll_into_view_if_needed(timeout=4000)
                await target.click(timeout=5000, delay=self.rng.randint(40, 140))
            except Exception as e:
                logger.debug(f"LinkedIn: clicking a copy of their connect failed: {e}")
                continue
            await self.wait(self.rng.uniform(0.8, 2.0))
            return True
        return False

    async def click_button_near(self, anchor_selector: str, name_pattern: str) -> bool:
        """Click the button named ``name_pattern`` in the list item that holds
        ``anchor_selector`` (a card's own Withdraw, say)."""
        card = self.page.locator("li", has=self.page.locator(anchor_selector)).first
        try:
            if await card.count() == 0:
                return False
        except Exception:
            return False
        loc = card.get_by_role("button", name=re.compile(name_pattern, re.IGNORECASE)).first
        try:
            if await loc.count() == 0:
                return False
            await loc.click(timeout=8000, delay=self.rng.randint(40, 140))
        except Exception:
            return False
        await self.wait(self.rng.uniform(0.8, 2.0))
        return True

    async def type_text(self, selector: str, text: str) -> bool:
        """Type like a person. A new line is Shift+Enter: in LinkedIn's
        composer a plain Enter can send the message half-written."""
        loc = await self._first(selector)
        if loc is None:
            return False
        await loc.click(timeout=8000)
        lines = (text or "").split("\n")
        for i, line in enumerate(lines):
            if line:
                await self.page.keyboard.type(line, delay=self.rng.randint(25, 80))
            if i < len(lines) - 1:
                await self.page.keyboard.press("Shift+Enter")
        await self.wait(self.rng.uniform(0.6, 1.5))
        return True

    async def clear(self, selector: str) -> None:
        loc = await self._first(selector)
        if loc is None:
            return
        await loc.click(timeout=8000)
        await self.page.keyboard.press("Meta+A" if sys.platform == "darwin" else "Control+A")
        await self.page.keyboard.press("Backspace")

    async def text_of(self, selector: str) -> str:
        loc = await self._first(selector)
        if loc is None:
            return ""
        try:
            return await loc.inner_text(timeout=5000)
        except Exception:
            return ""

    async def value_of(self, selector: str) -> str:
        loc = await self._first(selector)
        if loc is None:
            return ""
        try:
            return await loc.input_value(timeout=5000)
        except Exception:
            return ""

    async def scroll(self, selector: str, steps: int = 3) -> None:
        """Scroll a list that lazy-loads inside its own container: the mouse
        wheel over that container, not the window."""
        loc = await self._first(selector)
        box = None
        if loc is not None:
            try:
                box = await loc.bounding_box()
            except Exception:
                box = None
        if box:
            await self.page.mouse.move(box["x"] + box["width"] / 2,
                                       box["y"] + min(box["height"] / 2, 300))
        for _ in range(max(0, steps)):
            await self.page.mouse.wheel(0, self.rng.randint(400, 900))
            await self.wait(self.rng.uniform(0.7, 1.8))


@asynccontextmanager
async def open_session(cfg, headless: bool | None = None, **context_options):
    """The dedicated profile in system Chrome: yields a Driver. ``context_options``
    go to Playwright's persistent context as they are."""
    from playwright.async_api import async_playwright

    chrome = find_chrome(cfg.chrome_path)
    folder = profile_dir(cfg)
    folder.mkdir(parents=True, exist_ok=True)
    pw = await async_playwright().start()
    context = None
    try:
        context = await pw.chromium.launch_persistent_context(
            str(folder), executable_path=chrome,
            headless=cfg.headless if headless is None else headless,
            viewport={"width": 1280, "height": 860},
            args=["--no-first-run", "--no-default-browser-check"], **context_options)
        page = context.pages[0] if context.pages else await context.new_page()
        yield Driver(page)
    finally:
        if context is not None:
            try:
                await context.close()   # flushes the profile's cookies to disk
            except Exception as e:
                logger.debug(f"LinkedIn: closing Chrome failed: {e}")
        await pw.stop()


async def has_session_cookie(driver) -> bool:
    """LinkedIn's own session cookie (li_at) is in the profile: it exists only
    while signed in, whatever the page looks like."""
    page = getattr(driver, "page", None)
    if page is None:
        return False
    try:
        cookies = await page.context.cookies("https://www.linkedin.com")
    except Exception:
        return False
    return any(c.get("name") == "li_at" and c.get("value") for c in cookies)


async def is_signed_in(driver) -> bool:
    """Whether the profile is signed in: the page is LinkedIn's signed-in app,
    or (a layout Harvey doesn't know) the session cookie is there and the page
    isn't a sign-in, sign-up or challenge page."""
    try:
        if signed_in_from(driver.url, await driver.html()):
            return True
        first, _ = path_segments(driver.url)
        if first in SIGNED_OUT_PATHS or first in ("checkpoint", "challenge"):
            return False
        return await has_session_cookie(driver)
    except Exception:
        return False


async def login_interactive(cfg, timeout_minutes: float = 10, poll_seconds: float = 3,
                            say=print, stopped=None, session=None) -> bool:
    """Open linkedin.com/login in the dedicated profile, visible, and wait
    for the owner to sign in themself. True once signed in. ``stopped`` hears
    why, when the wait ends before its time."""
    opener = session or (lambda c: open_session(c, headless=False))
    async with opener(cfg) as driver:
        await driver.goto(LOGIN_URL)
        if await is_signed_in(driver):
            say("  This profile is already signed in to LinkedIn.")
            return True
        say("  A Chrome window is open on LinkedIn's sign-in page.")
        say("  Sign in there yourself (Harvey never sees your password or codes).")
        say(f"  Waiting up to {int(timeout_minutes)} minutes...")
        deadline = asyncio.get_running_loop().time() + timeout_minutes * 60
        while asyncio.get_running_loop().time() < deadline:
            await asyncio.sleep(poll_seconds)
            if getattr(driver, "closed", False):
                say("  The Chrome window was closed.")
                if stopped:
                    stopped("The Chrome window was closed")
                return False
            if await is_signed_in(driver):
                await driver.wait(2)
                return True
        return False
