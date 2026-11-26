"""New companies from social profiles, read with Scout (harvey/social_leads.py;
owner, 2026-11-27). Scout itself is faked: a runner stands in for its bridge,
and one test runs the real bridge against a tiny fake Scout package. Every
handle, name and domain here is invented."""

import asyncio
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from harvey import social_leads as sl
from harvey.agents.scout import Scout
from harvey.collectors import scout_bridge
from harvey.config import EnvConfig, HarveyConfig
from harvey.state import StateManager
from tests.linkedin_fakes import MINIMAL

NOW = datetime(2026, 11, 27, 6, 0, tzinfo=timezone.utc)


def run(coro):
    return asyncio.run(coro)


@pytest.fixture
def state(tmp_path):
    s = StateManager(str(tmp_path / "h.db"))
    run(s.init_db())
    return s


def config(**social) -> HarveyConfig:
    base = {"enabled": True, "scout_path": "C:/fake/Scout", "platforms": ["instagram"],
            "terms": ["candle brand"], "places": ["Leeds"], "searches_per_cycle": 1,
            "profiles_per_cycle": 10, "companies_per_cycle": 10}
    base.update(social)
    return HarveyConfig(**{**MINIMAL, "social_leads": base})


@pytest.fixture(autouse=True)
def no_waits(monkeypatch):
    monkeypatch.setattr(sl, "SEARCH_GAP", 0)


def agent(state, cfg, results=None, by_query=None, pages=None):
    """The Scout agent with its web search and page fetch faked: ``results``
    for every search (or ``by_query``: a search's text -> results), and
    ``pages``: domain -> home page HTML."""
    a = Scout(brain=None, state=state, config=cfg, env=EnvConfig())
    sl.prepare(a)
    a.searched, a.fetched = [], []

    async def web_search(query, count=True):
        a.searched.append((query, count))
        if by_query is not None:
            return list(by_query.get(query, []))
        return list(results or [])

    async def fetch(url, **kw):
        a.fetched.append(url)
        html = (pages or {}).get(url.split("//", 1)[-1].strip("/"))
        return None if html is None else type("R", (), {"text": html, "status_code": 200})()
    a._web_search, a._fetch = web_search, fetch
    return a


def runner_for(profiles: dict, calls=None):
    async def runner(platform, handles):
        if calls is not None:
            calls.append((platform, list(handles)))
        out = []
        for h in handles:
            p = profiles.get(h)
            out.append({"handle": h, "ok": p is not None, **({"profile": p} if p else {"error": "no profile"})})
        return out
    return runner


IG = "https://www.instagram.com/{}/"


def test_handles_come_from_profile_links_only():
    assert sl.handle_from_url("instagram", "https://www.instagram.com/glow.candles/") == "glow.candles"
    assert sl.handle_from_url("instagram", "https://www.instagram.com/p/AbC123/") == ""
    assert sl.handle_from_url("instagram", "https://www.instagram.com/reel/XyZ/") == ""
    assert sl.handle_from_url("instagram", "https://example.com/glow.candles/") == ""
    assert sl.handle_from_url("tiktok", "https://www.tiktok.com/@glowcandles/video/123") == "glowcandles"
    assert sl.handle_from_url("tiktok", "https://www.tiktok.com/discover/candles") == ""
    assert sl.handle_from_url("youtube", "https://www.youtube.com/@GlowCandles") == "GlowCandles"
    assert sl.handle_from_url("youtube", "https://www.youtube.com/watch?v=abc") == ""
    assert sl.handle_from_url("pinterest", "https://uk.pinterest.com/glowcandles/") == "glowcandles"
    assert sl.handle_from_url("pinterest", "https://www.pinterest.com/pin/12345/") == ""
    assert sl.handle_from_url("linktree", "https://linktr.ee/glowcandles") == "glowcandles"
    assert sl.handle_from_url("linktree", "https://linktr.ee/s/about") == ""


def test_linkedin_is_never_read():
    cfg = config(platforms=["linkedin", "instagram", "LinkedIn"]).social_leads
    assert sl.platforms_of(cfg) == ["instagram"]
    assert "linkedin" not in scout_bridge.SCRAPERS
    with pytest.raises(ValueError):
        run(sl.read_profiles(cfg, "linkedin", ["someone"]))


def test_it_is_off_by_default(state):
    assert HarveyConfig(**MINIMAL).social_leads.enabled is False
    a = agent(state, HarveyConfig(**MINIMAL), results=[(IG.format("glow.candles"), "")])
    assert run(a._prospect_via_social()) == 0
    assert a.searched == []


def test_the_website_is_the_brands_own():
    assert sl.website_of({"website": "https://l.instagram.com/?u=https%3A%2F%2Fglowcandles.example%2F&e=x"}) \
        == "https://glowcandles.example/"
    assert sl.website_of({"website": "https://linktr.ee/glow",
                          "links": [{"url": "https://www.instagram.com/glow"}, {"url": "https://shop.glow.example/"}]}) \
        == "https://shop.glow.example/"
    assert sl.website_of({"bio": "Hand-poured candles. Shop www.glowcandles.example"}) == "https://www.glowcandles.example"
    assert sl.website_of({"email": "hello@glowcandles.example"}) == "https://glowcandles.example"
    assert sl.website_of({"email": "x@gmail.com", "website": "https://www.etsy.com/shop/glow"}) == ""
    assert sl.website_of({"website": "https://www.amazon.co.uk/stores/glow"}) == ""


def test_a_brand_with_a_site_becomes_a_company_and_the_rest_are_skipped(state):
    cfg = config()
    run(state.add_company(__import__("harvey.models.company", fromlist=["Company"]).Company(
        name="Known Wax", domain="knownwax.example", website="https://knownwax.example")))
    results = [(IG.format(h), "") for h in ("glow.candles", "private.one", "no.site", "etsy.only", "known.wax")]
    profiles = {
        "glow.candles": {"full_name": "Glow Candles", "bio": "Small-batch soy candles from Leeds.",
                         "website": "https://glowcandles.example/", "is_private": False,
                         "profile_url": IG.format("glow.candles")},
        "private.one": {"full_name": "Private", "is_private": True, "website": "https://private.example"},
        "no.site": {"full_name": "No Site", "bio": "DM to order"},
        "etsy.only": {"full_name": "Etsy Only", "website": "https://www.etsy.com/shop/etsyonly"},
        "known.wax": {"full_name": "Known Wax", "website": "https://knownwax.example"},
    }
    a = agent(state, cfg, results)
    report = run(sl.run_cycle(a, runner=runner_for(profiles), now=NOW))
    assert (report["searches"], report["handles"], report["read"], report["added"]) == (1, 5, 5, 1)
    assert report["skipped"] == {"private account": 1, "no website (none links back)": 2, "already known": 1}
    assert report["looked_up"] == 2 and report["sites_found"] == 0
    assert a.searched == [('site:instagram.com "candle brand" "Leeds"', False),     # its own allowance
                          ('"No Site"', False), ('"Etsy Only"', False)]             # the two site lookups
    company = run(state.get_company_by_domain("glowcandles.example"))
    assert company.source == "social:instagram" and company.industry == "candle brand"
    assert company.location == "Leeds" and "soy candles" in company.description
    # The backfill finds its people next.
    assert "glowcandles.example" in {c["domain"] for c in run(state.companies_needing_prospects(limit=50))}


def test_a_handle_is_read_once_and_the_searches_rotate(state):
    cfg = config(terms=["candle brand", "soap brand"])
    calls = []
    profiles = {"glow.candles": {"full_name": "Glow", "website": "https://glowcandles.example"}}
    a = agent(state, cfg, [(IG.format("glow.candles"), "")])
    run(sl.run_cycle(a, runner=runner_for(profiles, calls), now=NOW))
    assert run(state.get_setting(sl.CURSOR_KEY)) == "1"
    second = run(sl.run_cycle(agent(state, cfg, [(IG.format("glow.candles"), "")]),
                              runner=runner_for(profiles, calls), now=NOW))
    assert second["handles"] == 0 and len(calls) == 1                  # not read again
    assert run(state.get_setting(sl.CURSOR_KEY)) == "0"                  # wrapped round


def test_three_refused_reads_stop_it_for_a_day(state):
    cfg = config()
    results = [(IG.format(h), "") for h in ("a.one", "b.two", "c.three", "d.four")]
    report = run(sl.run_cycle(agent(state, cfg, results), runner=runner_for({}), now=NOW))
    assert report["failed"] == 3 and "paused for a day" in report["stopped"]
    later = run(sl.run_cycle(agent(state, cfg, results), runner=runner_for({}), now=NOW + timedelta(hours=2)))
    assert later["stopped"].startswith("paused until") and later["searches"] == 0
    after = run(sl.run_cycle(agent(state, cfg, []), runner=runner_for({}), now=NOW + timedelta(hours=25)))
    assert not after["stopped"]


def test_a_dry_run_adds_and_remembers_nothing(state):
    cfg = config()
    profiles = {"glow.candles": {"full_name": "Glow", "website": "https://glowcandles.example"}}
    report = run(sl.run_cycle(agent(state, cfg, [(IG.format("glow.candles"), "")]), dry_run=True,
                              runner=runner_for(profiles), now=NOW))
    assert report["found"] == [{"platform": "instagram", "handle": "glow.candles", "domain": "glowcandles.example"}]
    assert run(state.get_company_by_domain("glowcandles.example")) is None
    assert not run(state.get_setting(sl.SEEN_KEY)) and not run(state.get_setting(sl.CURSOR_KEY))


def test_terms_and_places_default_from_the_target_profile():
    assert sl.default_terms(["Luxury Fragrance & Jewelry", "DTC Ecommerce (Shopify / Shopify Plus brands)"]) \
        == ["Luxury Fragrance", "Jewelry", "DTC Ecommerce"]
    assert sl.default_places(["United States", "European Union", "New York, New York, United States"]) \
        == ["United States", "New York"]


def test_the_bridge_reads_with_scouts_own_scrapers(tmp_path, monkeypatch):
    # A tiny stand-in for Scout's package, run through the real bridge.
    pkg = tmp_path / "app" / "scrapers"
    pkg.mkdir(parents=True)
    (tmp_path / "app" / "__init__.py").write_text("")
    (pkg / "__init__.py").write_text("")
    (pkg / "instagram.py").write_text(
        "def scrape_profile_no_login(handle):\n"
        "    return None if handle.startswith('gone') else {'username': handle, 'website': 'https://' + handle + '.example'}\n")
    monkeypatch.setattr(sl, "scout_python", lambda path: Path(sys.executable))
    monkeypatch.setattr(sl, "DELAY", (0, 0))
    rows = sl._run_bridge(str(tmp_path), "instagram", ["glow", "gone1", "gone2", "gone3", "late"])
    assert [r["ok"] for r in rows] == [True, False, False, False, False]
    assert rows[0]["profile"]["website"] == "https://glow.example"
    assert "refused the ones before" in rows[4]["error"]               # stopped after 3 in a row
    refused = sl._run_bridge(str(tmp_path), "linkedin", ["someone"])    # the bridge says no too
    assert refused == [{"handle": "", "ok": False, "error": "platform not allowed: linkedin"}]


def test_an_instagram_brand_with_no_link_is_found_by_a_site_that_links_back(state):
    cfg = config()
    search = 'site:instagram.com "candle brand" "Leeds"'
    by_query = {
        search: [(IG.format("glow.candles"), ""), (IG.format("lone.maker"), "")],
        '"Glow Candles"': [("https://www.etsy.com/shop/glow", ""), ("https://glowcandles-reviews.example/", ""),
                           ("https://glowcandles.example/about", "")],
        '"Lone Maker"': [("https://lonemaker.example/", "")],
    }
    pages = {
        "glowcandles-reviews.example": "<a href='https://www.instagram.com/other.brand/'>us</a>",
        "glowcandles.example": "<footer><a href='https://www.instagram.com/glow.candles/'>Instagram</a></footer>",
        "lonemaker.example": "<a href='https://www.instagram.com/lone.maker.official/'>ig</a>",   # not them
    }
    profiles = {"glow.candles": {"full_name": "Glow Candles", "bio": "Soy candles"},
                "lone.maker": {"full_name": "Lone Maker", "bio": "Handmade"}}
    a = agent(state, cfg, by_query=by_query, pages=pages)
    report = run(sl.run_cycle(a, runner=runner_for(profiles), now=NOW))
    assert (report["looked_up"], report["sites_found"], report["added"]) == (2, 1, 1)
    assert report["skipped"] == {"no website (none links back)": 1}
    assert run(state.get_company_by_domain("glowcandles.example")).source == "social:instagram"
    assert "https://www.etsy.com" not in a.fetched                         # a marketplace isn't opened


def test_a_link_back_means_that_profile_and_no_other():
    page = "<a href='https://instagram.com/glow.candles'>x</a> <a href='https://www.instagram.com/glow.candles.shop/'>y</a>"
    assert sl.links_back(page, "instagram", "glow.candles")
    assert not sl.links_back("<a href='https://instagram.com/glow.candles.shop/'>y</a>", "instagram", "glow.candles")
    assert sl.links_back("<a href='https://www.tiktok.com/@glowcandles'>t</a>", "tiktok", "glowcandles")


def test_a_profile_with_no_name_is_looked_up_by_its_handle():
    assert sl.lookup_name("", "glow.candles") == "glow candles"
    assert sl.lookup_name("  ", "@lone_maker") == "lone maker"
    assert sl.lookup_name("Glow Candles Ltd", "glow.candles") == "Glow Candles Ltd"
    assert sl.not_a_site("find-and-update.company-information.service.gov.uk")


def test_the_backfill_takes_social_brands_before_bulk_imports(state):
    from harvey.models.company import Company
    for i in range(3):
        run(state.add_company(Company(name=f"Plain {i}", domain=f"plain{i}.example", source="discover:overture")))
    run(state.add_company(Company(name="Glow", domain="glowcandles.example", source="social:pinterest")))
    run(state.add_company(Company(name="Warm", domain="warm.example", source="intent:shopify_community")))
    order = [c["domain"] for c in run(state.companies_needing_prospects(limit=2))]
    assert order == ["warm.example", "glowcandles.example"]          # intent first, then social
