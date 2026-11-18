"""One Chrome window per channel, kept open between runs.

Owner, 2026-11-18: "why are you closing linkedin and whatsapp and opening it
again and closing it when done ... just keep them open passively". Each LinkedIn
and WhatsApp run used to open the channel's Chrome profile and close it at the
end, so the windows came and went all day, and WhatsApp Web loaded and synced
with the phone from scratch every time. The heartbeat now opens a channel's
window at its first run and keeps it: each run borrows it (``lease``).

The window closes only when:

- the channel is switched off;
- a sign-in needs the profile: Chrome lets one process open a profile at a time,
  so `harvey <channel> login` (also what the dashboard's Connect starts) and
  `harvey <channel> run --once` ask for it with ``request_release`` and wait;
- it was closed by hand, or Chrome crashed: the next run opens a new one;
- Harvey stops: Chrome goes with the process, however it ends.

Between runs Harvey reads and clicks nothing. A run that ends parks the page
(``park_url``): WhatsApp on its chat list, so no chat stays open between runs,
where a new message would be marked read and never show as unread to the next.

``<channel>:browser`` names the process holding the window, so the dashboard and
a sign-in can tell; a stamp whose process is gone counts as closed.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from contextlib import AsyncExitStack, asynccontextmanager
from datetime import datetime, timezone

from harvey.pidfile import pid_alive

logger = logging.getLogger("harvey.kept_browser")

# How often an idle window checks whether it should close.
WATCH_SECONDS = 3.0
# How long a sign-in waits for the heartbeat to let the profile go.
RELEASE_WAIT_SECONDS = 45.0


def browser_key(channel: str) -> str:
    return f"{channel}:browser"


def release_key(channel: str) -> str:
    return f"{channel}:release"


async def holder(state, channel: str) -> int:
    """The PID of the process keeping this channel's window open, or 0."""
    try:
        data = json.loads(await state.get_setting(browser_key(channel)) or "{}")
        pid = int(data.get("pid") or 0) if isinstance(data, dict) else 0
    except (ValueError, TypeError):
        return 0
    return pid if pid > 0 and pid_alive(pid) else 0


async def request_release(state, channel: str, seconds: float = RELEASE_WAIT_SECONDS,
                          poll: float = 0.5) -> bool:
    """Ask the process keeping the window to close it, and wait. True once the
    profile is free; False when the window stayed open (a run is using it)."""
    pid = await holder(state, channel)
    if not pid:
        return True
    if pid == os.getpid():
        return False        # this process holds it: waiting on ourselves would hang
    await state.set_setting(release_key(channel), str(os.getpid()))
    loop = asyncio.get_running_loop()
    end = loop.time() + seconds
    while loop.time() < end:
        await asyncio.sleep(poll)
        if not await holder(state, channel):
            return True
    return False


@asynccontextmanager
async def own_window(state, channel: str, opener):
    """The profile for a run from a terminal (`harvey <channel> run --once`): the
    heartbeat lets go of its window first. The run has already stamped
    ``<channel>:running``, so the heartbeat won't open it again meanwhile."""
    if not await request_release(state, channel):
        raise RuntimeError(f"Harvey's heartbeat is using the {channel} window for a run; "
                           "try again when it has finished")
    async with opener() as driver:
        yield driver


class KeptBrowser:
    """A channel's Chrome window, opened by the first run and kept.

    ``opener()`` returns the async context manager that opens the profile and
    yields a driver (the channel's ``open_session``). ``wanted()`` says whether
    the window should stay open (the channel is on and no sign-in waits)."""

    def __init__(self, state, channel: str, opener, *, park_url: str = "", wanted=None,
                 watch_seconds: float = WATCH_SECONDS):
        self.state = state
        self.channel = channel
        self.opener = opener
        self.park_url = park_url
        self.wanted = wanted
        self.watch_seconds = watch_seconds
        self._lock = asyncio.Lock()
        self._stack: AsyncExitStack | None = None
        self._driver = None
        self._watcher: asyncio.Task | None = None

    @property
    def is_open(self) -> bool:
        if self._driver is None:
            return False
        try:
            return not self._driver.closed
        except Exception:
            return False

    @property
    def busy(self) -> bool:
        """A run has the window right now."""
        return self._lock.locked()

    @asynccontextmanager
    async def lease(self):
        """The window for one run: opened if it isn't, left open after."""
        async with self._lock:
            if not self.is_open:
                await self._close()
                await self._open()
            try:
                yield self._driver
            finally:
                await self._park()

    async def close(self) -> None:
        """Close the window now, waiting for a run that has it to finish."""
        async with self._lock:
            await self._close()

    async def close_if_idle(self) -> bool:
        """Close the window unless a run has it. True when it's closed."""
        if self.busy:
            return not self.is_open and self._stack is None
        await self.close()
        return True

    # ── Inside ──

    async def _open(self) -> None:
        stack = AsyncExitStack()
        try:
            self._driver = await stack.enter_async_context(self.opener())
        except BaseException:
            self._driver = None
            await stack.aclose()
            raise
        self._stack = stack
        # A release asked for before this window existed was for the one before.
        await self.state.set_setting(release_key(self.channel), "")
        await self.state.set_setting(browser_key(self.channel), json.dumps(
            {"pid": os.getpid(), "since": datetime.now(timezone.utc).replace(tzinfo=None).isoformat()}))
        self._watcher = asyncio.create_task(self._watch())
        logger.info(f"{self.channel}: Chrome window opened; it stays open between runs.")

    async def _park(self) -> None:
        if not self.is_open:
            await self._close()
            return
        if not self.park_url:
            return
        try:
            await self._driver.goto(self.park_url)
        except Exception as e:
            # A page Harvey can't steer is a page it shouldn't keep.
            logger.info(f"{self.channel}: couldn't park the window ({e}); closing it.")
            await self._close()

    async def _close(self) -> None:
        stack, self._stack, self._driver = self._stack, None, None
        watcher, self._watcher = self._watcher, None
        if watcher is not None and watcher is not asyncio.current_task():
            watcher.cancel()
        if stack is not None:
            try:
                await stack.aclose()
            except Exception as e:
                logger.debug(f"{self.channel}: closing Chrome failed: {e}")
            logger.info(f"{self.channel}: Chrome window closed.")
        try:
            if await holder(self.state, self.channel) in (0, os.getpid()):
                await self.state.set_setting(browser_key(self.channel), "")
            await self.state.set_setting(release_key(self.channel), "")
        except Exception as e:
            logger.debug(f"{self.channel}: clearing the window's stamp failed: {e}")

    async def _should_close(self) -> bool:
        if await self.state.get_setting(release_key(self.channel)):
            return True
        if self.wanted is not None and not await self.wanted():
            return True
        return False

    async def _watch(self) -> None:
        """While idle: close when the window is gone, the channel is off, or a
        sign-in asked for the profile."""
        while True:
            await asyncio.sleep(self.watch_seconds)
            if self.busy:
                continue
            try:
                gone = not self.is_open
                if gone or await self._should_close():
                    async with self._lock:
                        await self._close()
                    return
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.debug(f"{self.channel}: window check failed: {e}")
