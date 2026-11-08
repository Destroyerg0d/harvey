"""Scout's map step and its plain search queries (2026-11-08: every search engine
refused this PC, and the old queries quoted whole ICP labels)."""

import asyncio
import os
import tempfile
from types import SimpleNamespace

import pytest

from harvey.agents import scout as S
from harvey.collectors.discover import DiscoveryReport, OpenStreetMap


def cfg(industries, geography):
    return SimpleNamespace(icp=SimpleNamespace(industries=industries, geography=geography,
                                               keyword_variants={}, company_size="5-200 employees"))


def test_search_phrases_are_plain_words():
    assert S.search_phrase("DTC Ecommerce (Shopify / Shopify Plus brands)") == "DTC Ecommerce"
    assert S.search_phrase("Home Decor & Furniture") == "Home Decor Furniture"
    s = S.Scout.__new__(S.Scout)
    s.config = cfg(["DTC Ecommerce (Shopify / Shopify Plus brands)", "Home Decor & Furniture"],
                   ["United States", "United Kingdom"])
    queries = s._build_company_search_queries()
    assert queries and all('"' not in q and "employees" not in q for q in queries)


def test_the_map_rotates_shop_kinds_and_cities_and_skips_what_it_cant_map():
    pairs = S.map_rotation(cfg(["Luxury Fragrance & Jewelry", "B2B SaaS", "Home Decor & Furniture"],
                               ["United Kingdom", "Australia"]), OpenStreetMap())
    terms = {t for t, _ in pairs}
    assert terms == {"Luxury Fragrance & Jewelry", "Home Decor & Furniture"}
    assert pairs[0][1] == "London" and pairs[2][1] == "Sydney"      # round robin over countries
    assert len(pairs) == 2 * (len(S.MAP_CITIES["united kingdom"]) + len(S.MAP_CITIES["australia"]))


def test_a_city_named_in_the_geography_is_searched_as_it_is():
    pairs = S.map_rotation(cfg(["Home Decor & Furniture"], ["Austin, TX"]), OpenStreetMap())
    assert pairs == [("Home Decor & Furniture", "Austin, TX")]


@pytest.mark.asyncio
async def test_each_cycle_searches_the_next_pair(monkeypatch):
    from harvey.state import StateManager
    calls = []

    async def fake_run(state, config, provider_key, queries=None, max_spend=1.0, dry_run=False):
        calls.append((provider_key, queries[0].term, queries[0].location, max_spend))
        report = DiscoveryReport(provider=provider_key)
        report.new_companies = 3
        return report

    import harvey.collectors.discover as D
    monkeypatch.setattr(D, "run_discovery", fake_run)
    with tempfile.TemporaryDirectory() as tmp:
        state = StateManager(os.path.join(tmp, "h.db"))
        await state.init_db()
        s = S.Scout.__new__(S.Scout)
        s.state, s.config = state, cfg(["Home Decor & Furniture"], ["United Kingdom"])
        assert await s._prospect_via_map() == 0
        await s._prospect_via_map()
    assert calls == [("osm", "Home Decor & Furniture", "London", 0.0),
                     ("osm", "Home Decor & Furniture", "Manchester", 0.0)]


@pytest.mark.asyncio
async def test_web_discovery_drops_directories_before_looking_for_contacts(monkeypatch):
    s = S.Scout.__new__(S.Scout)
    s.config = cfg(["Home Decor & Furniture"], ["United Kingdom"])
    looked = []

    async def found():
        return [{"domain": "f6s.com", "name": "F6S"}, {"domain": "sample-shop.example", "name": "Sample"}]

    async def none():
        return []

    async def ensure(**kw):
        looked.append(kw["domain"])
        return "c1"

    async def contacts(**kw):
        return []
    s._find_companies_via_search, s._find_companies_via_directories = found, none
    s._ensure_company = ensure
    s._contacts_from_company = contacts
    monkeypatch.setattr(S.Scout, "_domain_to_name", lambda self, d: d, raising=False)
    try:
        await s._prospect_via_company_discovery()
    except Exception:
        pass
    assert "f6s.com" not in looked and "sample-shop.example" in looked
