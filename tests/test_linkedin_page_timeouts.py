"""A LinkedIn page that won't load no longer stops a run (owner, 2026-11-20:
"make sure this error doesn't come again"). Pages and people here are invented."""

import asyncio
import json

from harvey import owner_updates
from harvey.agents.linkedin_agent import (
    AUTOPILOT_KEY, NOT_LOADING, NOT_LOADING_KEY, WAITING,
)
from harvey.linkedin.session import Driver, PageNotLoading
from tests.linkedin_fakes import Person
from tests.test_linkedin_agent import World, jane, run


# ── The browser: a slow page is checked, retried, and "Leave site?" is accepted ──

class Timeout(Exception):
    pass


Timeout.__name__ = "TimeoutError"


class SlowPage:
    """Enough of a Playwright page: goto times out ``fails`` times; after a
    timeout the page is still where it was unless ``lands`` is set."""

    def __init__(self, fails=1, lands=False):
        self.url = "https://www.linkedin.com/feed/"
        self.fails, self.lands, self.gotos, self.handlers = fails, lands, [], {}

    def on(self, event, handler):
        self.handlers[event] = handler

    async def goto(self, url, wait_until="", timeout=0):
        self.gotos.append(wait_until)
        if self.fails:
            self.fails -= 1
            if self.lands:
                self.url = url
            raise Timeout("Timeout 45000ms exceeded.")
        self.url = url

    async def wait_for_load_state(self, *a, **k):
        pass

    async def evaluate(self, script):
        return "interactive" if script == "document.readyState" else None

    async def wait_for_timeout(self, ms):
        pass


def test_a_page_that_timed_out_but_has_its_content_is_used():
    page = SlowPage(fails=1, lands=True)
    asyncio.run(Driver(page).goto("https://www.linkedin.com/in/example-1/"))
    assert page.gotos == ["domcontentloaded"] and page.url.endswith("/in/example-1/")


def test_a_page_that_never_came_is_tried_once_more():
    page = SlowPage(fails=1)
    asyncio.run(Driver(page).goto("https://www.linkedin.com/messaging/"))
    assert page.gotos == ["domcontentloaded", "commit"] and page.url.endswith("/messaging/")


def test_a_page_that_never_loads_says_so_without_its_query():
    page = SlowPage(fails=2)
    try:
        asyncio.run(Driver(page).goto("https://www.linkedin.com/messaging/compose/?profileUrn=x"))
    except PageNotLoading as e:
        assert str(e) == "LinkedIn didn't load www.linkedin.com/messaging/compose/"
    else:
        raise AssertionError("expected PageNotLoading")


def test_leave_site_is_accepted_other_dialogs_dismissed():
    page = SlowPage(fails=0)
    Driver(page)
    seen = []

    class Dialog:
        def __init__(self, kind):
            self.type = kind

        async def accept(self):
            seen.append((self.type, "accept"))

        async def dismiss(self):
            seen.append((self.type, "dismiss"))
    asyncio.run(page.handlers["dialog"](Dialog("beforeunload")))
    asyncio.run(page.handlers["dialog"](Dialog("alert")))
    assert seen == [("beforeunload", "accept"), ("alert", "dismiss")]


# ── The run carries on ──

def test_an_inbox_that_wont_load_doesnt_stop_the_run(tmp_path):
    w = World(tmp_path, [jane()])
    run(w.state.set_setting(AUTOPILOT_KEY, "on"))
    lead = w.lead("jane-doe-1", stage="accepted", name="Jane Doe")
    m = run(w.state.li_add_message(lead, body="A first message.", status="approved", approved_by="you"))
    w.driver.not_loading = ["linkedin.com/messaging/"]   # the inbox and the compose link
    report = w.go()
    assert report["outcome"] == "done" and report["pages_not_loading"] >= 1


def test_a_send_whose_page_wont_load_waits_and_the_next_goes(tmp_path):
    w = World(tmp_path, [jane(), Person(slug="kim-5", name="Kim Example", connected=True,
                                        connected_on="Oct 28, 2026")])
    first = w.lead("jane-doe-1", stage="accepted", name="Jane Doe", profile_url="https://www.linkedin.com/in/jane-doe-1/")
    second = w.lead("kim-5", stage="accepted", name="Kim Example", profile_url="https://www.linkedin.com/in/kim-5/")
    a = run(w.state.li_add_message(first, body="First one.", status="approved", approved_by="you"))
    b = run(w.state.li_add_message(second, body="Second one.", status="approved", approved_by="you"))
    w.driver.not_loading = ["/in/jane-doe-1"]
    report = w.go()
    assert report["outcome"] == "done"
    assert run(w.state.li_get_message(a))["status"] == "approved"
    assert run(w.state.li_get_message(a))["error"].startswith(WAITING + "LinkedIn didn't load")
    assert run(w.state.li_get_message(b))["status"] == "sent"


def test_pages_that_keep_failing_stop_the_run_and_you_hear_after_three_runs(tmp_path):
    w = World(tmp_path, [jane()])
    w.driver.not_loading = ["linkedin.com/"]        # nothing loads
    for n in (1, 2, 3):
        report = w.go()
        assert report["outcome"].startswith(NOT_LOADING) and report["runs_not_loading"] == n
        line = owner_updates.lines_for([("linkedin_run", report)])
        assert (line == []) if n < 3 else ("hasn't been loading for 3 runs in a row" in line[0])
    w.driver.not_loading = []
    assert w.go()["outcome"] == "done"
    assert run(w.state.get_setting(NOT_LOADING_KEY)) == "0"
    assert json.dumps(report)        # the report stays plain data
