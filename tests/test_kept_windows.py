"""WhatsApp and LinkedIn windows kept open between runs, and messages typed so
WhatsApp's message box keeps them (owner, 2026-11-18). No browser here: the
windows are stand-ins. Every number, name and address is fictional."""

import asyncio
import json
import os
import re

import pytest

from harvey import channel_login, kept_browser, owner_updates
from harvey.agents.whatsapp_agent import WhatsAppRunner
from harvey.kept_browser import KeptBrowser, browser_key, holder, release_key, request_release
from harvey.meeting_alert import queue_to_owner
from harvey.whatsapp.pages import BULLET, composer_safe
from tests.test_outbox_native import state  # noqa: F401  (fixture)
from tests.test_owner_whatsapp import cfg, to_owner
from tests.whatsapp_fakes import JANE, Person, World, run


# ── What WhatsApp's message box keeps ──

def test_list_and_quote_markers_at_a_line_start_become_plain_characters():
    text = "Update\n- one\n* two\n  - nested\n1. first\n> quoted\n*bold* stays\n-5 stays\nmid - dash stays"
    assert composer_safe(text) == (f"Update\n{BULLET}one\n{BULLET}two\n  {BULLET}nested\n1) first\n"
                                   "quoted\n*bold* stays\n-5 stays\nmid - dash stays")
    assert composer_safe(composer_safe(text)) == composer_safe(text)


def whatsapp_lists(typed: str) -> str:
    """What WhatsApp's box holds: a typed "- " at a line's start became a list."""
    return re.sub(r"(?m)^- ", "", typed)


def test_a_message_with_dashes_is_typed_so_the_box_holds_it_and_goes(tmp_path):
    w = World(tmp_path, [Person(phone=JANE)], driver_kwargs={"typing": whatsapp_lists})
    c = w.contact()
    m = w.approved(c, body="Hi Jane, two ideas:\n- a faster home page\n- one checkout step less")
    report = w.go()
    assert report["sent"] == 1 and report["failed"] == 0
    sent = w.get(m)
    assert sent["status"] == "sent" and "- " not in sent["body"] and f"{BULLET}a faster home page" in sent["body"]


def test_an_update_never_starts_a_line_with_a_dash():
    message = owner_updates.compose(["Sent 2 follow-ups.", "ann@shop.example opted out."])
    assert message == f"Harvey update\n{BULLET}Sent 2 follow-ups.\n{BULLET}ann@shop.example opted out."
    assert composer_safe(message) == message


# ── Updates that couldn't go merge into one ──

@pytest.mark.asyncio
async def test_updates_waiting_to_go_merge_into_one_and_a_sent_one_stays(state):
    await state.set_setting(owner_updates.LAST_KEY, "2000-01-01 00:00:00")
    await queue_to_owner(state, cfg(), "Harvey: a meeting is close with Jo (Shop Example)")
    await state.log_action("email_sent", "sender", {"kind": "sequence", "step": 1})
    assert await owner_updates.send(state, cfg())
    await state.log_action("email_sent", "sender", {"kind": "sequence", "step": 2})
    assert await owner_updates.send(state, cfg())
    contact = await state.wa_get_contact_by_phone("+15555550199")
    out = await state.wa_messages(contact_id=contact["id"])
    waiting = [m for m in out if m["status"] == "approved"]
    merged = [m for m in out if m["status"] == "skipped"]
    assert len(merged) == 1 and merged[0]["error"] == owner_updates.MERGED_NOTE
    assert sorted(m["body"].splitlines()[0] for m in waiting) == ["Harvey update",
                                                                 "Harvey: a meeting is close with Jo (Shop Example)"]
    [update] = [m for m in waiting if m["body"].startswith("Harvey update")]
    assert f"{BULLET}Sent 1 first email, 1 follow-up." in update["body"]
    # Once it went, the next update is its own message and covers only what's new.
    await state.wa_update_message(update["id"], status="sent")
    await state.log_action("email_sent", "sender", {"kind": "reply"})
    assert await owner_updates.send(state, cfg())
    newest = (await to_owner(state))[-1]
    assert f"{BULLET}Sent 1 reply." in newest and "follow-up" not in newest
    assert (await state.wa_get_message(update["id"]))["status"] == "sent"


# ── The window kept open ──

class Window:
    def __init__(self):
        self.closed = False
        self.visited: list[str] = []

    async def goto(self, url):
        if self.closed:
            raise RuntimeError("the window is gone")
        self.visited.append(url)


class Opener:
    def __init__(self):
        self.windows: list[Window] = []
        self.exits = 0

    def __call__(self):
        opener = self

        class _Session:
            async def __aenter__(self_inner):
                opener.windows.append(Window())
                return opener.windows[-1]

            async def __aexit__(self_inner, *exc):
                opener.exits += 1
                opener.windows[-1].closed = True
                return False
        return _Session()


@pytest.mark.asyncio
async def test_runs_share_one_window_and_each_leaves_it_on_the_chat_list(state):
    opener = Opener()
    kept = KeptBrowser(state, "whatsapp", opener, park_url="https://chat.example/", watch_seconds=60)
    async with kept.lease() as first:
        first.visited.append("a chat")
    async with kept.lease() as second:
        assert second is first
    assert len(opener.windows) == 1 and opener.exits == 0 and kept.is_open
    assert first.visited == ["a chat", "https://chat.example/", "https://chat.example/"]
    assert await holder(state, "whatsapp") == os.getpid()
    await kept.close()
    assert opener.exits == 1 and await holder(state, "whatsapp") == 0


@pytest.mark.asyncio
async def test_a_window_closed_by_hand_is_opened_again_by_the_next_run(state):
    opener = Opener()
    kept = KeptBrowser(state, "linkedin", opener, watch_seconds=60)
    async with kept.lease() as window:
        pass
    window.closed = True
    async with kept.lease() as again:
        assert again is not window
    assert len(opener.windows) == 2
    await kept.close()


@pytest.mark.asyncio
async def test_a_failed_run_still_leaves_no_chat_open(state):
    opener = Opener()
    kept = KeptBrowser(state, "whatsapp", opener, park_url="https://chat.example/", watch_seconds=60)
    with pytest.raises(RuntimeError):
        async with kept.lease() as window:
            raise RuntimeError("the run broke")
    assert window.visited == ["https://chat.example/"] and kept.is_open
    await kept.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("why", ["switched off", "a sign-in asked"])
async def test_an_idle_window_closes_when_switched_off_or_a_sign_in_asks(state, why):
    on = {"v": True}

    async def wanted():
        return on["v"]
    opener = Opener()
    kept = KeptBrowser(state, "whatsapp", opener, wanted=wanted, watch_seconds=0.01)
    async with kept.lease():
        pass
    if why == "switched off":
        on["v"] = False
    else:
        await state.set_setting(release_key("whatsapp"), "12345")
    for _ in range(200):
        if not kept.is_open:
            break
        await asyncio.sleep(0.01)
    await kept.close()                      # the watcher's own close has finished
    assert not kept.is_open and opener.exits == 1
    assert await state.get_setting(browser_key("whatsapp")) == ""
    assert await state.get_setting(release_key("whatsapp")) == ""


@pytest.mark.asyncio
async def test_a_window_in_use_is_not_closed_under_a_run(state):
    async def wanted():
        return False
    opener = Opener()
    kept = KeptBrowser(state, "whatsapp", opener, wanted=wanted, watch_seconds=0.01)
    async with kept.lease() as window:
        await asyncio.sleep(0.1)            # the watcher looks several times meanwhile
        assert not window.closed
    for _ in range(200):
        if not kept.is_open:
            break
        await asyncio.sleep(0.01)
    await kept.close()
    assert not kept.is_open and opener.exits == 1


@pytest.mark.asyncio
async def test_a_sign_in_waits_for_the_heartbeat_to_let_the_profile_go(state):
    assert await request_release(state, "whatsapp", seconds=0.2, poll=0.01)      # nobody holds it
    other = os.getppid()                    # a live process that isn't this one
    await state.set_setting(browser_key("whatsapp"), json.dumps({"pid": other}))

    async def heartbeat_lets_go():
        while not await state.get_setting(release_key("whatsapp")):
            await asyncio.sleep(0.01)
        await state.set_setting(browser_key("whatsapp"), "")
    helper = asyncio.create_task(heartbeat_lets_go())
    assert await request_release(state, "whatsapp", seconds=2, poll=0.01)
    await helper
    await state.set_setting(browser_key("whatsapp"), json.dumps({"pid": other}))
    assert not await request_release(state, "whatsapp", seconds=0.05, poll=0.01)  # a run keeps it
    # A stamp left by a process that's gone counts as free.
    await state.set_setting(browser_key("whatsapp"), json.dumps({"pid": 999999999}))
    assert await holder(state, "whatsapp") == 0


@pytest.mark.asyncio
async def test_a_sign_in_that_cant_get_the_profile_says_so_and_opens_nothing(state, monkeypatch):
    async def busy(*a, **k):
        return False
    monkeypatch.setattr(kept_browser, "request_release", busy)
    opened = []

    async def login():
        opened.append(1)
        return True, ""
    outcome = await channel_login.run_attended(state, "whatsapp", login, minutes=1)
    assert outcome.status == channel_login.FAILED and "busy with a run" in outcome.detail
    assert opened == []


def test_the_heartbeat_s_whatsapp_runs_borrow_its_kept_window(tmp_path):
    w = World(tmp_path)
    runner = WhatsAppRunner(w.brain, w.state, w.config)
    agent = runner.agent_factory()
    assert agent.driver_factory == runner.kept.lease
    run(runner.shutdown())
    assert not runner.kept.is_open
