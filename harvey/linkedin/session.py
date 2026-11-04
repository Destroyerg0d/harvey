"""The dedicated LinkedIn Chrome profile, and the page driver.

Harvey drives the installed Google Chrome (not Playwright's bundled
Chromium) with a persistent profile of its own in data/linkedin-profile,
visible on screen by default. The owner signs that profile in once with
`harvey linkedin login`: Harvey opens the login page and waits. It never
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

from harvey.linkedin.pages import LOGIN_URL, signed_in_from
from harvey.paths import PROJECT_ROOT

logger = logging.getLogger("harvey.linkedin")


class ChromeNotFound(RuntimeError):
    pass


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

    @property
    def url(self) -> str:
        return self.page.url

    async def goto(self, url: str) -> None:
        await self.page.goto(url, wait_until="domcontentloaded", timeout=45000)
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


async def is_signed_in(driver) -> bool:
    """Whether the profile is signed in, judged from the page it is on."""
    try:
        return signed_in_from(driver.url, await driver.html())
    except Exception:
        return False


async def login_interactive(cfg, timeout_minutes: float = 10, poll_seconds: float = 3,
                            say=print) -> bool:
    """Open linkedin.com/login in the dedicated profile, visible, and wait
    for the owner to sign in themself. True once signed in."""
    async with open_session(cfg, headless=False) as driver:
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
            if await is_signed_in(driver):
                await driver.wait(2)
                return True
        return False
