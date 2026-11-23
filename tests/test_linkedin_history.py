"""LinkedIn's whole inbox read into Harvey's records (harvey/linkedin/history.py;
owner, 2026-11-23). The fake LinkedIn from tests/linkedin_fakes.py; every
person here is invented."""

import random

from harvey.agents.linkedin_agent import AUTOPILOT_KEY
from harvey.linkedin import history
from harvey.linkedin.pages import parse_thread
from tests.linkedin_fakes import OWNER, DriverFactory, Person
from tests.test_linkedin_agent import World, run


def _quiet():
    async def no_sleep(_):
        pass
    return {"sleep": no_sleep, "rng": random.Random(3)}


def test_the_inbox_fills_in_what_harvey_missed_and_never_answers_it(tmp_path):
    jane = Person(slug="jane-doe-1", name="Jane Doe", connected=True,
                  thread=[(OWNER, "Jane, a first message."), ("Jane Doe", "Happy to talk, Tuesday at 3pm EST?"),
                          (OWNER, "Tuesday 3pm EST works, see you then")])
    sam = Person(slug="sam-example-2", name="Sam Example", connected=True,
                 thread=[("Sam Example", "Can we schedule a call next week?"), (OWNER, "Sure, Thursday 11am IST?")])
    kim = Person(slug="kim-example-3", name="Kim Example", connected=True,
                 thread=[("Kim Example", "Thanks for connecting"), (OWNER, "Likewise")])
    w = World(tmp_path, [jane, sam, kim])
    run(w.state.set_setting(AUTOPILOT_KEY, "on"))
    lead = w.lead("jane-doe-1", stage="messaged", name="Jane Doe")
    run(w.state.li_add_message(lead, body="Jane, a first message.", status="sent"))
    report = run(history.backfill(w.state, w.config, driver_factory=DriverFactory(w.driver),
                                  agent_options=_quiet()))
    assert report["listed"] == 3 and report["threads"] == 3
    assert report["new_people"] == 1 and report["skipped"] == 1          # Kim: no meeting in it
    bodies = [(m["direction"], m["intent"]) for m in w.msgs(lead)]
    assert bodies == [("out", ""), ("in", "history"), ("out", "")]
    sam_lead = run(w.state.li_get_lead_by_slug("sam-example-2"))
    assert sam_lead["source"] == "inbox" and sam_lead["no_message_reason"] == history.INBOX_LEAD_REASON
    assert run(w.state.li_get_lead_by_slug("kim-example-3")) is None
    # A run afterwards answers nothing old and writes to nobody from the inbox.
    w.brain.calls.clear()
    w.go()
    assert not any(task == "draft_reply" for task, _ in w.brain.calls)
    assert w.driver.sent == []


def test_a_second_read_starts_where_the_first_stopped(tmp_path):
    people = [Person(slug=f"p-{i}", name=f"Pat Example{i}", connected=True,
                     thread=[(f"Pat Example{i}", "A call on Monday?")]) for i in range(4)]
    w = World(tmp_path, people)
    first = run(history.backfill(w.state, w.config, max_threads=2, driver_factory=DriverFactory(w.driver),
                                 agent_options=_quiet()))
    second = run(history.backfill(w.state, w.config, driver_factory=DriverFactory(w.driver), agent_options=_quiet()))
    assert first["threads"] == 2 and second["threads"] == 2


def test_a_thread_names_its_person_by_their_profile_link():
    html = ("<main><h2 class='msg-entity-lockup__entity-title'>Ann Lee</h2>"
            "<a href='https://www.linkedin.com/in/ann-lee-1/' aria-label='Ann Lee'>View profile</a>"
            "<aside><a href='https://www.linkedin.com/in/someone-else/'>Someone Else</a></aside></main>")
    assert history.thread_profile_slug(html, "Ann Lee") == "ann-lee-1"
    assert parse_thread(html).header == "Ann Lee"
    assert history.member_id("ACoAAB1234567890abcdefghijklmnopqrstu") and not history.member_id("ann-lee-1")


def test_the_live_list_is_read_by_clicking_and_unread_ones_are_left_alone(tmp_path):
    # Live, 2026-11-23: the list has no thread links, 20 more per scroll,
    # then a "Load more conversations" button; opening one marks it read.
    names = ["Pat Alpha", "Kim Bravo", "Lou Charlie", "Ray Delta", "Max Echo"]
    people = [Person(slug=f"p-{i}", name=name, connected=True,
                     thread=[(name, "A call on Monday at 10am?"), (OWNER, "Monday 10am works")])
              for i, name in enumerate(names)]
    people.append(Person(slug="new-1", name="Lee Example", connected=True,
                         thread=[("Lee Example", "Free for a quick call this week?")]))
    w = World(tmp_path, people, site_kwargs={"live_list": True, "unread": {"new-1"}, "list_page": 2})
    report = run(history.backfill(w.state, w.config, driver_factory=DriverFactory(w.driver),
                                  agent_options=_quiet()))
    assert report["listed"] == 6 and report["unread"] == 1
    assert report["threads"] == 5 and report["new_people"] == 5
    assert "new-1" not in w.driver.opened and "new-1" in w.site.unread       # still unread
    lead = run(w.state.li_get_lead_by_slug("p-0"))
    assert lead["thread_url"].endswith("/messaging/thread/t-p-0/")
    again = run(history.backfill(w.state, w.config, driver_factory=DriverFactory(w.driver),
                                 agent_options=_quiet()))
    assert again["threads"] == 0                                             # nothing new to read


def test_a_thread_gives_each_message_its_date_and_time():
    from datetime import date, datetime, timezone

    def group(name, clock, *bodies):
        return (f"<li class='msg-s-message-list__event'><span class='msg-s-message-group__profile-link "
                f"msg-s-message-group__name'>{name}</span><time class='msg-s-message-group__timestamp'>"
                f"{clock}</time>" + "".join(f"<p class='msg-s-event-listitem__body'>{b}</p>" for b in bodies)
                + "</li>")

    def heading(text):
        return f"<li class='msg-s-message-list__event'><time class='msg-s-message-list__time-heading'>{text}</time></li>"

    html = ("<main><h2 class='msg-entity-lockup__entity-title'>Ann Lee</h2><ul class='msg-s-message-list'>"
            + heading("OCT 22") + group("Sam Owner", "11:07 AM", "Hello Ann")
            + heading("NOV 1, 2025") + group("Ann Lee", "8:04 PM", "Hi")
            + heading("FRIDAY") + group("Ann Lee", "2:17 AM", "Monday 10am?", "Or Tuesday")
            + heading("TODAY") + group("Sam Owner", "1:37 PM", "Monday works")
            + "</ul></main>")
    t = parse_thread(html, today=date(2026, 11, 23), tz=timezone.utc)       # a Monday
    assert [m.at for m in t.messages] == [
        datetime(2026, 10, 22, 11, 7), datetime(2025, 11, 1, 20, 4),
        datetime(2026, 11, 20, 2, 17), datetime(2026, 11, 20, 2, 17, 1), datetime(2026, 11, 23, 13, 37)]
    assert [m.sender for m in t.messages] == ["Sam Owner", "Ann Lee", "Ann Lee", "Ann Lee", "Sam Owner"]
    from harvey.linkedin.pages import thread_day
    assert thread_day("Dec 30", date(2026, 1, 2)) == date(2025, 12, 30)          # last year's
    assert thread_day("Monday", date(2026, 11, 23)) == date(2026, 11, 16)        # a week back, not today
    assert thread_day("Yesterday", date(2026, 11, 23)) == date(2026, 11, 22)
    assert thread_day("Someday", date(2026, 11, 23)) is None


def test_a_long_read_keeps_linkedin_marked_busy(tmp_path):
    # Live, 2026-11-23: a 2.5-hour read let its busy mark go stale; the
    # heartbeat's run then failed on the profile and told the owner, every 10 minutes.
    from harvey.agents.linkedin_agent import RUNNING_KEY
    people = [Person(slug=f"q-{i}", name=f"Quinn {chr(65 + i)}lpha", connected=True,
                     thread=[(f"Quinn {chr(65 + i)}lpha", "A call on Monday?")]) for i in range(6)]
    w = World(tmp_path, people)
    stamps = []
    real = w.state.set_setting

    async def spy(key, value):
        if key == RUNNING_KEY:
            stamps.append(value)
        return await real(key, value)
    w.state.set_setting = spy
    run(history.backfill(w.state, w.config, driver_factory=DriverFactory(w.driver), agent_options=_quiet()))
    assert len([s for s in stamps if s]) >= 2 and stamps[-1] == ""        # refreshed, then let go
