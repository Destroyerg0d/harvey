"""30 connects a day to the right people, found by web search, and tracked daily
(owner, 2026-11-19). No network: TinyFish is an httpx.MockTransport. Every
person, company and profile here is invented."""

import json
from datetime import datetime, timezone

import httpx
import pytest

from harvey import owner_updates, targeting
from harvey.agents.linkedin_agent import AUTOPILOT_KEY
from harvey.config import TargetingConfig
from harvey.linkedin import daily, finder
from tests.linkedin_fakes import Person
from tests.test_linkedin_agent import World, run

WEST = list(targeting.WESTERN)


def result(slug, title, snippet=""):
    return {"url": f"https://www.linkedin.com/in/{slug}/", "title": title, "snippet": snippet}


def test_a_search_result_reads_as_a_profile():
    p = finder.parse_result(result("jane-doe-7", "Jane Doe - Founder - Lumen Candles | LinkedIn",
                                   "Austin, Texas, United States · Founder at Lumen Candles"))
    assert {k: p[k] for k in ("url", "slug", "name", "headline", "company", "location")} == {
        "url": "https://www.linkedin.com/in/jane-doe-7/", "slug": "jane-doe-7", "name": "Jane Doe",
        "headline": "Founder - Lumen Candles", "company": "Lumen Candles",
        "location": "Austin, Texas, United States"}
    assert finder.parse_result({"url": "https://www.linkedin.com/company/lumen/", "title": "Lumen"}) is None
    # TinyFish's own shape: the title is the name, the snippet has the rest (live, 2026-11-19).
    q = finder.parse_result({"url": "https://www.linkedin.com/in/fred-h/", "title": "Fred H",
                             "snippet": "# Fred H Co-Founder & CEO | Example Goods | DTC skincare - **Location:** "
                                        "Stockholm, Stockholm County, Sweden # Experience"})
    assert q["headline"] == "Co-Founder & CEO | Example Goods | DTC skincare"
    assert q["location"] == "Stockholm, Stockholm County, Sweden"


def test_searches_cover_roles_niches_and_countries_the_us_most():
    config = World.__new__(World)
    from tests.linkedin_fakes import make_config
    config = make_config()
    config.targeting = TargetingConfig(countries=["GB", "SE", "US"])       # the US still goes first
    config.icp.industries = ["DTC Ecommerce (Shopify / Shopify Plus brands)"]
    qs = finder.queries(config)
    assert qs[0] == 'site:linkedin.com/in "founder" "DTC Ecommerce" "United States"'
    assert any('"CEO" "DTC Ecommerce" "United Kingdom"' in q for q in qs)


def test_finding_people_adds_only_decision_makers_in_the_targets(tmp_path):
    w = World(tmp_path)
    w.config.targeting = TargetingConfig(countries=WEST, decision_makers_only=True)
    run(_finding(w))


async def _finding(w):
    run_lead = await w.state.li_upsert_lead("known-one", stage="invited", name="Known One")
    results = [
        result("ann-lee-1", "Ann Lee - Founder & CEO - Shop Example | LinkedIn", "London Area, United Kingdom"),
        result("raj-k-2", "Raj K - Founder - Shop Example | LinkedIn", "Mumbai, Maharashtra, India"),
        result("bo-ma-3", "Bo Ma - Marketing Manager - Shop Example | LinkedIn", "Boston, Massachusetts, United States"),
        result("cy-no-4", "Cy No - Owner - Shop Example | LinkedIn", ""),          # unknown: checked at invite
        result("known-one", "Known One - Founder | LinkedIn", ""),
        {"url": "https://www.linkedin.com/company/shop-example/", "title": "Shop Example"},
    ]
    asked = []

    def handler(request):
        asked.append(str(request.url))
        return httpx.Response(200, json={"results": results})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        report = await finder.find_people(w.state, w.config, "k", wanted=2, client=client, max_searches=3)
    assert report["added"] == 2 and report["skipped"] == 2 and report["searches"] == 1
    assert (await w.state.li_get_lead_by_slug("ann-lee-1"))["source"] == finder.SOURCE
    assert await w.state.li_get_lead_by_slug("raj-k-2") is None
    assert await w.state.li_get_lead_by_slug("cy-no-4") is not None
    assert await w.state.get_setting(finder.CURSOR_KEY) == "1" and run_lead


def test_found_people_are_invited_after_their_profile_passes(tmp_path):
    w = World(tmp_path, [Person(slug="ann-lee-1", name="Ann Lee", location="London Area, United Kingdom",
                                headline="Founder at Shop Example"),
                         Person(slug="cy-no-4", name="Cy No", location="Bengaluru, Karnataka, India",
                                headline="Owner at Shop Example")])
    w.config.targeting = TargetingConfig(countries=WEST, decision_makers_only=True)
    run(w.state.set_setting(AUTOPILOT_KEY, "on"))
    for slug, name in (("ann-lee-1", "Ann Lee"), ("cy-no-4", "Cy No")):
        run(w.state.li_upsert_lead(slug, stage="found", name=name, first_name=name.split()[0],
                                   headline="Founder", profile_url=f"https://www.linkedin.com/in/{slug}/",
                                   source=finder.SOURCE))
    report = w.go()
    assert [slug for slug, _ in w.driver.invites] == ["ann-lee-1"]
    assert report["sent_invites"] == 1 and report["off_target"] == 1
    assert run(w.state.li_get_lead_by_slug("cy-no-4"))["no_message_reason"].startswith(
        "not invited: outside your target countries (IN)")


# ── Every day, tracked ──

AT_2130 = datetime(2026, 11, 19, 16, 0, tzinfo=timezone.utc)      # 21:30 IST
AT_2230 = datetime(2026, 11, 19, 17, 0, tzinfo=timezone.utc)      # 22:30 IST


def test_a_day_under_target_says_why_and_the_day_is_reported_once(tmp_path):
    w = World(tmp_path)
    run(_a_day(w))


async def _a_day(w):
    await w.state.set_setting("linkedin:enabled", "on")
    lead, _ = await w.state.li_upsert_lead("ann-lee-1", stage="accepted", name="Ann Lee")
    await w.state.li_update_lead(lead, accepted_at=datetime(2026, 11, 19))
    item = await daily.under_target_item(w.state, w.config, AT_2130)
    assert item["title"] == f"LinkedIn connects today: 0 of {w.config.channels.linkedin.max_daily_connections}"
    assert "no new people to invite were found" in item["detail"] and "1 accepted today" in item["detail"]
    assert await daily.under_target_item(w.state, w.config, datetime(2026, 11, 19, 10, tzinfo=timezone.utc)) is None
    assert await daily.report_day(w.state, w.config, AT_2230) is True
    assert await daily.report_day(w.state, w.config, AT_2230) is False         # once a day
    import aiosqlite
    async with aiosqlite.connect(w.state.db_path) as db:
        row = await (await db.execute(
            "SELECT details_json FROM actions WHERE action_type = 'linkedin_daily'")).fetchone()
    line = owner_updates.lines_for([("linkedin_daily", json.loads(row[0]))])[0]
    assert line.startswith("LinkedIn today: 0 connects (target") and "1 accepted" in line
    assert "Short because no new people" in line
