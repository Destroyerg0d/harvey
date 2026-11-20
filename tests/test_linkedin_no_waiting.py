"""LinkedIn connects that go, replies that don't wait, autopilot that sends, and
the call booked directly (owner, 2026-11-20). Every person, company, profile and
link here is invented."""

import json
from datetime import timedelta

from harvey import targeting
from harvey.agents.linkedin_agent import (
    AUTOPILOT_KEY, FOUND_KEY, INBOX_CHECK_KEY, PROFILE_KEY, RETRY_UNREAD_KEY, WAITING,
    answers_automation_honestly,
)
from harvey.config import TargetingConfig
from harvey.linkedin.pages import summary_headline, summary_location
from harvey.linkedin.store import HELD_FOR_REVIEW
from harvey.meeting_slot import confirms_slot
from harvey.models.company import Company
from tests.linkedin_fakes import IN_HOURS, OWNER, FakeBrain, Person
from tests.test_linkedin_agent import World, jane, naive, run

WEST = list(targeting.WESTERN)
MEET = "https://meet.example.com/abc-defg-hij"


def _meeting_config(w):
    offer = w.config.product.offer
    offer.meeting_window, offer.meeting_timezone, offer.meeting_link = "10:00-12:00", "Asia/Kolkata", MEET


# ── A. The profile as LinkedIn draws it today ──

SUMMARY = "\n".join(["Ann Lee", "· 1st", "He/Him", "Founder & CEO at Example Goods", "Example Goods Ltd",
                     "· San Diego State University", "Paris, Île-de-France, France", "About", "We make soap."])


def test_the_top_of_a_profile_gives_its_headline_and_location():
    assert summary_headline(SUMMARY) == "Founder & CEO at Example Goods"
    assert summary_location(SUMMARY) == "Paris, Île-de-France, France"     # not the school
    assert targeting.location_country("Greater Madrid Metropolitan Area") == "ES"
    assert targeting.location_country("Royal Leamington Spa, England") == "GB"


def test_an_invite_goes_to_a_profile_with_no_h1(tmp_path):
    w = World(tmp_path, [Person(slug="ann-lee-1", name="Ann Lee", headline="Founder & CEO at Example Goods",
                                location="Paris, Île-de-France, France", plain_card=True)])
    w.config.targeting = TargetingConfig(countries=WEST, decision_makers_only=True)
    lead = w.lead("ann-lee-1", stage="found", name="Ann Lee")
    run(w.state.li_add_message(lead, kind="invite_note", body="", status="approved", approved_by="you"))
    report = w.go()
    assert report["sent_invites"] == 1 and [s for s, _ in w.driver.invites] == ["ann-lee-1"]
    cached = json.loads(run(w.state.get_setting(PROFILE_KEY + lead)))
    assert cached["location"].startswith("Paris")         # kept for their first message


def test_connect_under_more_goes_to_them_and_never_to_the_side_list(tmp_path):
    # Live, 2026-11-20: a page-wide "Invite ... to connect" click hit the
    # "More profiles for you" list first and invited 20 strangers.
    strangers = ("Riley Stranger", "Sam Other")
    w = World(tmp_path, [Person(slug="ann-lee-1", name="Ann Lee", headline="Founder & CEO at Example Goods",
                                location="Paris, Île-de-France, France", plain_card=True,
                                connect_in_more=True, side_list=strangers)])
    w.config.targeting = TargetingConfig(countries=WEST, decision_makers_only=True)
    lead = w.lead("ann-lee-1", stage="found", name="Ann Lee")
    run(w.state.li_add_message(lead, kind="invite_note", body="", status="approved", approved_by="you"))
    report = w.go()
    assert w.site.stray_invites == []
    assert report["sent_invites"] == 1 and [s for s, _ in w.driver.invites] == ["ann-lee-1"]


def test_a_card_connect_with_a_side_list_goes_to_them_only(tmp_path):
    w = World(tmp_path, [Person(slug="ann-lee-1", name="Ann Lee", headline="Founder at Example Goods",
                                location="London, England, United Kingdom", side_list=("Riley Stranger",))])
    lead = w.lead("ann-lee-1", stage="found", name="Ann Lee")
    run(w.state.li_add_message(lead, kind="invite_note", body="", status="approved", approved_by="you"))
    assert w.go()["sent_invites"] == 1 and w.site.stray_invites == []


def test_no_note_box_means_the_invite_goes_without_a_note(tmp_path):
    from harvey.agents.linkedin_agent import NOTES_OFF_KEY
    w = World(tmp_path, [Person(slug="ann-lee-1", name="Ann Lee", headline="Founder at Example Goods",
                                location="London, England, United Kingdom")], site_kwargs={"no_note_box": True})
    lead = w.lead("ann-lee-1", stage="found", name="Ann Lee")
    run(w.state.li_add_message(lead, kind="invite_note", body="Fellow store owner here.", status="approved",
                               approved_by="you"))
    assert w.go()["sent_invites"] == 1
    assert w.driver.invites == [("ann-lee-1", "")] and run(w.state.get_setting(NOTES_OFF_KEY))


def test_a_side_lists_connect_isnt_theirs():
    from harvey.linkedin.pages import profile_connect_state
    html = ("<main><section class='artdeco-card'><div><p>Ann Lee</p></div><button>Follow</button>"
            "<button>More</button></section><aside><button aria-label='Invite Riley Stranger to connect'>"
            "Connect</button></aside></main>")
    assert profile_connect_state(html, "Ann Lee") == "more"
    theirs = html.replace("<button>More</button>", "<button aria-label='Invite Ann Lee to connect'>Connect</button>")
    assert profile_connect_state(theirs, "Ann Lee") == "connect"
    assert profile_connect_state(theirs, "Bo Ma") == "more" or profile_connect_state(theirs, "Bo Ma") == "unknown"


def test_the_web_searchs_location_counts_when_the_page_shows_none(tmp_path):
    w = World(tmp_path, [Person(slug="bo-ma-2", name="Bo Ma", headline="Founder at Example Goods",
                                plain_card=True)])
    w.config.targeting = TargetingConfig(countries=WEST, decision_makers_only=True)
    lead = w.lead("bo-ma-2", stage="found", name="Bo Ma")
    run(w.state.set_setting(FOUND_KEY + lead, json.dumps({"location": "Austin, Texas, United States",
                                                          "headline": "Founder at Example Goods"})))
    run(w.state.li_add_message(lead, kind="invite_note", body="", status="approved", approved_by="you"))
    assert w.go()["sent_invites"] == 1


def test_someone_outside_the_targets_is_skipped_without_the_wait(tmp_path):
    w = World(tmp_path, [Person(slug="cy-no-3", name="Cy No", headline="Founder at Example Goods",
                                location="Pune, Maharashtra, India", plain_card=True)])
    w.config.targeting = TargetingConfig(countries=WEST, decision_makers_only=True)
    lead = w.lead("cy-no-3", stage="found", name="Cy No")
    run(w.state.li_add_message(lead, kind="invite_note", body="", status="approved", approved_by="you"))
    agent = w.agent()
    waits = []

    async def counted():
        waits.append(1)
    agent._wait_between_actions = counted
    report = run(agent.run(ignore_interval=True))
    assert report["sent_invites"] == 0 and report["off_target"] == 1 and waits == []


def test_people_whose_profile_couldnt_be_read_get_another_go_once(tmp_path):
    w = World(tmp_path, [Person(slug="dee-4", name="Dee Example", headline="Owner at Example Goods",
                                location="Dublin, Ireland", plain_card=True)])
    w.config.targeting = TargetingConfig(countries=WEST, decision_makers_only=True)
    run(w.state.set_setting(AUTOPILOT_KEY, "on"))
    lead = w.lead("dee-4", stage="found", name="Dee Example", first_name="Dee",
                  no_message_reason="not invited: their country couldn't be read")
    run(w.state.li_add_message(lead, kind="invite_note", body="", status="skipped",
                               error="not invited: their country couldn't be read"))
    report = w.go()
    assert report["sent_invites"] == 1
    assert json.loads(run(w.state.get_setting(RETRY_UNREAD_KEY)))["leads"] == 1


# ── B. Nothing waits ──

def test_an_approved_reply_goes_before_any_drafting(tmp_path):
    w = World(tmp_path, [jane(), Person(slug="kim-5", name="Kim Example", connected=True,
                                        connected_on="Oct 28, 2026")])
    run(w.state.set_setting(AUTOPILOT_KEY, "on"))
    jane_id = w.lead("jane-doe-1", stage="replied", name="Jane Doe")
    run(w.state.li_add_message(jane_id, kind="reply", body="Thanks, here it is.", status="approved",
                               approved_by="you"))
    w.lead("kim-5", stage="accepted", name="Kim Example", first_name="Kim",
           accepted_at=naive(IN_HOURS) - timedelta(days=3))
    w.go()
    visited = w.driver.visited
    reply_at = next(i for i, u in enumerate(visited) if "/messaging/compose/" in u and "jane-doe-1" in u)
    kim_at = next(i for i, u in enumerate(visited) if "/in/kim-5" in u)
    assert reply_at < kim_at


def test_a_reply_that_comes_in_during_a_long_run_is_answered_in_it(tmp_path):
    w = World(tmp_path, [jane(thread=[(OWNER, "Jane, a first message."), ("Jane Doe", "How would this work?")])],
              brain=FakeBrain(intent="question", text="We'd start with your product pages."))
    run(w.state.set_setting(AUTOPILOT_KEY, "on"))
    lead = w.lead("jane-doe-1", stage="messaged", name="Jane Doe")
    run(w.state.li_add_message(lead, body="Jane, a first message.", status="sent",
                               sent_at=naive(IN_HOURS) - timedelta(days=1)))
    run(_break(w))
    assert w.driver.sent == [("jane-doe-1", "We'd start with your product pages.")]


async def _break(w):
    agent = w.agent()
    agent.dry_run, agent.replies_only, agent.deadline = False, False, None
    agent._failed_leads, agent._waiting, agent._in_break = set(), {}, False
    agent.report = {"inbound": 0, "drafted": 0, "sent_messages": 0, "sent_invites": 0, "failed": 0,
                    "declined": 0, "would_send": []}
    await w.state.set_setting(INBOX_CHECK_KEY, (naive(IN_HOURS) - timedelta(minutes=20)).isoformat())
    async with agent.driver_factory() as driver:
        agent.driver = driver
        await agent._inbox_break()
    assert agent.report["inbox_breaks"] == 1


def test_an_approved_message_that_cant_go_says_why(tmp_path):
    w = World(tmp_path, [jane()], max_actions_per_hour=1)
    lead = w.lead("jane-doe-1", stage="accepted", name="Jane Doe")
    m = run(w.state.li_add_message(lead, body="A first message.", status="approved", approved_by="you"))
    run(w.agent().pacing.record("message"))          # this hour's one action is used
    report = w.go()
    item = run(w.state.li_get_message(m))
    assert item["status"] == "approved" and item["error"].startswith(WAITING + "1 actions this hour")
    assert report["waiting"]


# ── C. Autopilot sends ──

def test_a_draft_held_for_naming_their_own_site_goes_next_run(tmp_path):
    w = World(tmp_path, [jane()])
    run(w.state.set_setting(AUTOPILOT_KEY, "on"))
    company_id = run(w.state.add_company(Company(name="Example Shop", domain="example-shop.test",
                                                 website="https://example-shop.test/")))
    lead = w.lead("jane-doe-1", stage="accepted", name="Jane Doe", company_id=company_id)
    m = run(w.state.li_add_message(lead, body="Your example-shop.test product pages load slowly.",
                                   status="draft", error=HELD_FOR_REVIEW + "links to example-shop.test, "
                                   "not one of your domains (add it to channels.email.link_domains if it's yours)"))
    w.go()
    assert run(w.state.li_get_message(m))["status"] == "sent"


def test_a_site_their_own_profile_shows_counts_as_theirs(tmp_path):
    w = World(tmp_path, [jane()])
    run(w.state.set_setting(AUTOPILOT_KEY, "on"))
    lead = w.lead("jane-doe-1", stage="accepted", name="Jane Doe")
    run(w.state.set_setting(PROFILE_KEY + lead, json.dumps(
        {"at": naive(IN_HOURS).isoformat(), "text": "Jane Doe\nFounder at Example\nwww.example-shop.test",
         "location": "", "headline": ""})))
    m = run(w.state.li_add_message(lead, body="Your example-shop.test product pages load slowly.",
                                   status="draft", error=HELD_FOR_REVIEW + "links to example-shop.test"))
    w.go()
    assert run(w.state.li_get_message(m))["status"] == "sent"


def test_a_released_cold_draft_still_keeps_to_the_targets(tmp_path):
    w = World(tmp_path, [jane()])
    w.config.targeting = TargetingConfig(countries=WEST, decision_makers_only=False)
    run(w.state.set_setting(AUTOPILOT_KEY, "on"))
    lead = w.lead("jane-doe-1", stage="accepted", name="Jane Doe")
    run(w.state.set_setting(PROFILE_KEY + lead, json.dumps(
        {"at": naive(IN_HOURS).isoformat(), "text": "Jane Doe\nFounder at Example\nPune, Maharashtra, India",
         "location": "", "headline": ""})))
    m = run(w.state.li_add_message(lead, body="See example-elsewhere.test.", status="draft",
                                   error=HELD_FOR_REVIEW + "links to example-elsewhere.test"))
    run(w.state.li_update_message(m, body="A plain first message."))
    w.go()
    item = run(w.state.li_get_message(m))
    assert item["status"] == "skipped" and "outside your target countries (IN)" in item["error"]
    assert w.driver.sent == []


def test_an_honest_answer_about_automation():
    assert answers_automation_honestly("Yes, I use an AI assistant to draft these, and I read every reply.")
    assert not answers_automation_honestly("Ha, what makes you ask?")
    assert not answers_automation_honestly("Yes it's me, not a bot.")


# ── D. Booking the call ──

def test_an_interested_reply_offers_the_time_and_the_link_in_their_zone(tmp_path):
    w = World(tmp_path, [jane(thread=[(OWNER, "Jane, a first message."), ("Jane Doe", "Sounds interesting!")])],
              brain=FakeBrain(intent="interested", text="Glad to hear it."))
    _meeting_config(w)
    lead = w.lead("jane-doe-1", stage="messaged", name="Jane Doe")
    run(w.state.li_add_message(lead, body="Jane, a first message.", status="sent",
                               sent_at=naive(IN_HOURS) - timedelta(days=1)))
    run(w.state.set_setting(PROFILE_KEY + lead, json.dumps(
        {"at": naive(IN_HOURS).isoformat(), "text": "Jane Doe\nFounder at Example\nLondon, England, United Kingdom",
         "location": "London, England, United Kingdom", "headline": "Founder at Example"})))
    w.go()
    prompt = next(p for task, p in w.brain.calls if task == "draft_reply")
    assert "THE MEETING TIME. They're interested" in prompt and MEET in prompt
    assert "London time" in prompt and "IST" in prompt


def test_a_yes_to_the_offered_time_books_the_meeting_once(tmp_path):
    offer = f"Thursday 5 November, 4:30 to 6:30 AM London time (10 AM to 12 PM IST). The link: {MEET}"
    w = World(tmp_path, [jane(thread=[(OWNER, offer), ("Jane Doe", "Yes, that works for me")])],
              brain=FakeBrain(intent="interested", text="Great, see you then."))
    _meeting_config(w)
    lead = w.lead("jane-doe-1", stage="warm", name="Jane Doe", company="Example Goods")
    run(w.state.li_add_message(lead, kind="reply", body=offer, status="sent",
                               sent_at=naive(IN_HOURS) - timedelta(hours=2)))
    run(w.state.set_setting("meeting:offered:linkedin:" + lead, json.dumps(
        {"text": "Thursday 5 November, 4:30 to 6:30 AM London time",
         "start": (IN_HOURS + timedelta(days=1)).isoformat()})))
    w.go()
    assert run(w.state.li_get_lead(lead))["stage"] == "meeting"
    prompt = next(p for task, p in w.brain.calls if task == "draft_reply")
    assert "THE MEETING IS BOOKED" in prompt and MEET in prompt
    import aiosqlite

    async def booked():
        async with aiosqlite.connect(w.state.db_path) as db:
            return await (await db.execute(
                "SELECT count(*) FROM actions WHERE action_type = 'meeting_booked'")).fetchone()
    assert run(booked())[0] == 1


def test_what_counts_as_a_yes_to_the_time():
    assert confirms_slot("Yes, that works, no problem")
    assert confirms_slot("Thursday works", "Thursday 5 November, 4:30 to 6:30 AM")
    assert not confirms_slot("Monday works better", "Thursday 5 November")
    assert not confirms_slot("Sure, but can we do 3pm instead?")
    assert not confirms_slot("Can't make that, sorry")
