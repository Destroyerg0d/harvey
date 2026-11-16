"""Free discovery (owner, 2026-11-16): shops from Overture Maps' open data, and
stores asking for help on the Shopify Community. No network here: the data
sources are stood in for. Every name and domain is fictional."""

import asyncio
import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace as NS

import httpx
import pytest

from harvey.agents import scout as S
from harvey.collectors import intent
from harvey.collectors.discover import DiscoveryQuery, DiscoveryReport, OvertureMaps
from harvey.models.company import Company
from tests.test_outbox_native import state  # noqa: F401  (fixture)


def place(pid, name, site, category="jewelry_store", brand=None, shop=True):
    return (pid, {"primary": name}, [site] if site else None, ["+15125550100"],
            [{"freeform": "1 Main St", "locality": "Austin", "region": "TX", "country": "US"}],
            {"primary": category, "hierarchy": (["shopping"] if shop else ["food"]) + [category]}, brand)


def test_overture_keeps_independent_shops_with_their_own_site(monkeypatch):
    rows = [place("1", "Lumen Candles", "https://lumen-candles.example/", "gift_shop"),
            place("2", "Gas Plus", "https://gasplus.example", "gas_station"),
            place("3", "Big Chain", "https://bigchain.example", brand={"names": {"primary": "Big"}}),
            place("4", "Cafe", "https://cafe.example", "coffee_shop", shop=False),
            place("5", "On Facebook", "https://www.facebook.com/someshop"),
            place("6", "Chain A", "https://samechain.example/a"),
            place("7", "Chain B", "https://samechain.example/b"),
            place("8", "No Site", None)]
    p = OvertureMaps()
    calls = []
    monkeypatch.setattr(p, "_query", lambda bbox, limit, offset: calls.append((limit, offset)) or rows)
    out = asyncio.run(p.fetch(None, None, DiscoveryQuery(term="shops", location="Austin, TX",
                                                         coordinate="30.27,-97.74,25", limit=1500,
                                                         offset=3000)))
    assert [b.domain for b in out] == ["lumen-candles.example"]
    assert out[0].location == "Austin, TX, US" and out[0].category == "gift_shop"
    assert calls == [(1500, 3000)]


def test_the_city_box_is_about_the_radius_wide():
    west, south, east, north = OvertureMaps.bbox("30.0,-97.0,25")
    assert round(north - south, 2) == round(50 / 111.0, 2) and west < -97.0 < east


def test_cities_rotate_with_three_us_cities_a_round():
    config = NS(icp=NS(geography=["United Kingdom", "United States"]))
    cities = S.city_rotation(config)
    us = S.MAP_CITIES["united states"]
    assert cities[:4] == ["London", us[0], us[1], us[2]]
    assert len(cities) == len(set(cities))


@pytest.mark.asyncio
async def test_the_overture_step_pages_through_a_city_and_starts_over_when_it_runs_out(state, monkeypatch):
    import harvey.collectors.discover as D
    asked = []
    found = {"n": 12}

    async def fake_run(st, config, provider, queries=None, max_spend=0.0):
        asked.append((provider, queries[0].location, queries[0].offset))
        return DiscoveryReport(provider=provider, found=found["n"], new_companies=found["n"])
    monkeypatch.setattr(D, "run_discovery", fake_run)
    sc = S.Scout.__new__(S.Scout)
    sc.state, sc.config = state, NS(icp=NS(geography=["Austin, TX"]))
    await sc._prospect_via_overture()
    await sc._prospect_via_overture()
    found["n"] = 0
    await sc._prospect_via_overture()
    await sc._prospect_via_overture()
    assert asked == [("overture", "Austin, TX", 0), ("overture", "Austin, TX", S.OVERTURE_BATCH),
                     ("overture", "Austin, TX", 2 * S.OVERTURE_BATCH), ("overture", "Austin, TX", 0)]


# ── The Shopify Community ──

def test_links_that_are_never_the_posters_store_are_dropped():
    html = ('<a href="https://help.shopify.com/en/manual">help</a> <a href="https://my-store.example/'
            'collections/all">store</a> <a href="https://i.imgur.com/x.png">pic</a> '
            '<a href="https://my-store.example/products/a">again</a>')
    assert intent.store_links(html) == ["https://my-store.example/collections/all",
                                        "https://my-store.example/products/a"]


def community(now, topics, posts, pages):
    def handler(request):
        url = str(request.url)
        if url.endswith("/latest.json") or "/c/" in url:
            return httpx.Response(200, json={"topic_list": {"topics": topics}})
        for tid, cooked in posts.items():
            if url.endswith(f"/{tid}.json"):
                return httpx.Response(200, json={"post_stream": {"posts": [{"cooked": cooked}]}})
        for host, html in pages.items():
            if host in url:
                return httpx.Response(200, text=html)
        return httpx.Response(404)
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


@pytest.mark.asyncio
async def test_new_questions_linking_a_shopify_store_become_leads_once(state):
    now = datetime(2026, 11, 16, tzinfo=timezone.utc)
    topics = [{"id": 1, "slug": "slow-store", "title": "My store is slow", "created_at": "2026-11-15T10:00:00Z"},
              {"id": 2, "slug": "app-question", "title": "Which app?", "created_at": "2026-11-15T09:00:00Z"},
              {"id": 3, "slug": "old", "title": "Old one", "created_at": "2026-10-01T09:00:00Z"}]
    posts = {1: '<a href="https://slow-shop.example/">my store</a>',
             2: '<a href="https://some-app.example/">their app</a>',
             3: '<a href="https://old-shop.example/">mine</a>'}
    pages = {"slow-shop.example": '<script src="https://cdn.shopify.com/s/x.js"></script>',
             "some-app.example": "<html>An app for Shopify merchants</html>",
             "old-shop.example": '<script src="https://cdn.shopify.com/s/x.js"></script>'}
    async with community(now, topics, posts, pages) as client:
        leads = await intent.shopify_community_leads(state, now=now, client=client)
        again = await intent.shopify_community_leads(state, now=now, client=client)
    assert [(l.domain, l.asked_on) for l in leads] == [("slow-shop.example", "2026-11-15")]
    assert "My store is slow" in leads[0].note() and again == []


@pytest.mark.asyncio
async def test_the_intent_step_adds_the_store_and_waits_before_asking_again(state, monkeypatch):
    lead = intent.IntentLead(domain="slow-shop.example", store_url="https://slow-shop.example/",
                             title="My store is slow", topic_url="https://community.example/t/1",
                             asked_on="2026-11-15")
    calls = []

    async def fake_leads(st):
        calls.append(1)
        return [lead]
    monkeypatch.setattr(intent, "shopify_community_leads", fake_leads)
    sc = S.Scout.__new__(S.Scout)
    sc.state = state
    await sc._prospect_via_intent()
    await sc._prospect_via_intent()                     # within INTENT_EVERY: not asked
    company = await state.get_company_by_domain("slow-shop.example")
    assert company.source == S.INTENT_SOURCE and "My store is slow" in company.notes
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_the_backfill_takes_intent_companies_first(state):
    await state.add_company(Company(name="Plain", domain="plain-shop.example", source="overture"))
    await state.add_company(Company(name="Warm", domain="warm-shop.example", source=S.INTENT_SOURCE))
    first = (await state.companies_needing_prospects(limit=1))[0]
    assert first["domain"] == "warm-shop.example"
