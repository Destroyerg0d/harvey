"""SITE_PLATFORM, LEGACY_SITE_SCORE and SITE_AGE_FACTS through a real profile pass.

HTTP is a mocked transport: the fictional shops below answer (or fail) the
way real ones do, including a TLS failure on https and a phone being sent
to an m. site. Plus the two state queries that use the result: which
companies still need classifying, and who the Writer will write to.
"""

import json
import os
import tempfile

import httpx
import pytest
import pytest_asyncio

import harvey.collectors.profile as profile_mod
from harvey.collectors.profile import ProfileCollector
from harvey.models.company import Company
from harvey.models.prospect import Prospect
from harvey.signals import seed_signal_catalog
from harvey.state import StateManager

SITE_CODES = {"SITE_PLATFORM", "LEGACY_SITE_SCORE", "SITE_AGE_FACTS"}
VIEWPORT = '<meta name="viewport" content="width=device-width, initial-scale=1">'

OLD_SHOP = (
    "<html><head><title>Acme Garden Supplies</title></head><body>"
    '<a href="product_info.php?products_id=12">Spade</a>'
    '<a href="shopping_cart.php">Basket</a>'
    "<p>Copyright &copy; 2011 Acme Garden Supplies. Powered by osCommerce</p></body></html>"
)


@pytest_asyncio.fixture
async def state():
    with tempfile.TemporaryDirectory() as tmp:
        sm = StateManager(os.path.join(tmp, "t.db"))
        await sm.init_db()
        await seed_signal_catalog(sm)
        yield sm


def mock_http(monkeypatch, handler):
    """ProfileCollector's client, on a mocked transport."""
    def client(*, timeout=15.0, headers=None):
        return httpx.AsyncClient(transport=httpx.MockTransport(handler), timeout=timeout,
                                 headers=headers, follow_redirects=True)

    async def no_sleep(_seconds):
        return None

    monkeypatch.setattr(profile_mod, "public_http_client", client)
    monkeypatch.setattr(profile_mod.asyncio, "sleep", no_sleep)


def is_phone(request) -> bool:
    return "iPhone" in request.headers.get("user-agent", "")


async def profile(state, domain, confirmed=SITE_CODES):
    cid = await state.add_company(Company(name="Acme", domain=domain))
    async with ProfileCollector(state, set(confirmed)) as c:
        await c.run([{"id": cid, "name": "Acme", "domain": domain}])
    rows = await state.get_observations(company_id=cid, latest_only=True)
    return cid, {r["signal_code"]: r for r in rows}


def claimable(rows) -> dict:
    return {f["code"]: f["sentence"] for f in json.loads(rows["SITE_AGE_FACTS"]["value_text"])}


@pytest.mark.asyncio
async def test_an_http_only_legacy_shop_is_proven_old(state, monkeypatch):
    seen = []

    def handler(request):
        seen.append((str(request.url), is_phone(request)))
        if request.url.scheme == "https":
            raise httpx.ConnectError("[SSL: WRONG_VERSION_NUMBER] wrong version number", request=request)
        if request.url.path in ("", "/"):
            return httpx.Response(200, html=OLD_SHOP, headers={"X-Powered-By": "PHP/5.4.45"})
        return httpx.Response(404, text="not found")

    mock_http(monkeypatch, handler)
    _, rows = await profile(state, "oldshop.example")
    assert rows["SITE_PLATFORM"]["value_text"] == "oscommerce"
    facts = claimable(rows)
    assert {"HTTP_ONLY", "NO_MOBILE_VIEWPORT", "OLD_COPYRIGHT"} <= set(facts)
    assert facts["OLD_COPYRIGHT"].endswith("2011")
    assert rows["SITE_AGE_FACTS"]["value_num"] == len(facts)
    assert rows["LEGACY_SITE_SCORE"]["value_num"] >= 40
    # One extra fetch, with a phone's user agent, because the page had no viewport.
    assert ("http://oldshop.example", True) in seen


@pytest.mark.asyncio
async def test_https_that_times_out_is_not_proof(state, monkeypatch):
    def handler(request):
        if request.url.scheme == "https":
            raise httpx.ConnectTimeout("timed out", request=request)
        return httpx.Response(200, html=OLD_SHOP)

    mock_http(monkeypatch, handler)
    _, rows = await profile(state, "slowshop.example")
    assert "HTTP_ONLY" not in claimable(rows)
    assert "NO_MOBILE_VIEWPORT" in claimable(rows)


@pytest.mark.asyncio
async def test_https_that_redirects_away_from_http_is_not_http_only(state, monkeypatch):
    www_tries = []

    def handler(request):
        if request.url.scheme == "https" and request.url.host == "acme.example":
            raise httpx.ConnectError("connection refused", request=request)
        if request.url.scheme == "https" and not www_tries:
            www_tries.append(1)         # the first try on www is slow, later ones answer
            raise httpx.ConnectTimeout("timed out", request=request)
        if request.url.scheme == "http":
            return httpx.Response(301, headers={"Location": "https://www.acme.example/"})
        return httpx.Response(200, html=OLD_SHOP)

    mock_http(monkeypatch, handler)
    _, rows = await profile(state, "acme.example")
    # The page was reached through http, which sent it on to https: not HTTP only.
    assert www_tries and "HTTP_ONLY" not in claimable(rows)
    assert rows["SITE_PLATFORM"]["evidence_url"].startswith("https://www.acme.example")


@pytest.mark.asyncio
async def test_a_phone_sent_to_an_m_site_is_not_told_it_has_no_mobile_view(state, monkeypatch):
    desktop = "<html><head><title>Acme</title></head><body><p>&copy; 2026 Acme</p></body></html>"
    mobile = f"<html><head>{VIEWPORT}</head><body><p>Acme mobile</p></body></html>"

    def handler(request):
        if request.url.host == "m.mshop.example":
            return httpx.Response(200, html=mobile)
        if is_phone(request):
            return httpx.Response(302, headers={"Location": "https://m.mshop.example/"})
        if request.url.path in ("", "/"):
            return httpx.Response(200, html=desktop)
        return httpx.Response(404)

    mock_http(monkeypatch, handler)
    _, rows = await profile(state, "mshop.example")
    assert "NO_MOBILE_VIEWPORT" not in claimable(rows)


@pytest.mark.asyncio
async def test_a_modern_shop_scores_low_and_skips_the_phone_fetch(state, monkeypatch):
    phone_fetches = []
    home = (f"<html><head>{VIEWPORT}</head><body>"
            '<link href="//cdn.shopify.com/s/files/1/0001/t/1/assets/theme.css">'
            "<footer>&copy; 2026 Acme</footer></body></html>")

    def handler(request):
        if is_phone(request):
            phone_fetches.append(str(request.url))
        return httpx.Response(200, html=home) if request.url.path in ("", "/") else httpx.Response(404)

    mock_http(monkeypatch, handler)
    _, rows = await profile(state, "modern.example")
    assert rows["SITE_PLATFORM"]["value_text"] == "shopify"
    assert rows["LEGACY_SITE_SCORE"]["value_num"] == 0
    assert claimable(rows) == {} and rows["SITE_AGE_FACTS"]["value_num"] == 0
    assert phone_fetches == []


@pytest.mark.asyncio
async def test_nothing_is_collected_unless_confirmed(state, monkeypatch):
    mock_http(monkeypatch, lambda request: httpx.Response(200, html=OLD_SHOP))
    _, rows = await profile(state, "quiet.example", confirmed={"TECH_STACK"})
    assert not SITE_CODES & set(rows)


# ── who still needs classifying ──

async def confirm_platform(state):
    await state.set_signal_status("SITE_PLATFORM", "confirmed")


@pytest.mark.asyncio
async def test_a_company_profiled_before_site_platform_comes_back_once(state):
    await confirm_platform(state)
    cid = await state.add_company(Company(name="Acme", domain="acme.example"))
    await state.add_observation("TECH_STACK", company_id=cid, collector="profile", value_text="WordPress")
    pending = [c["id"] for c in await state.companies_needing_profile()]
    assert pending == [cid] and await state.count_companies_needing_profile() == 1

    await state.add_observation("SITE_PLATFORM", company_id=cid, collector="profile", value_text="wordpress")
    assert await state.count_companies_needing_profile() == 0


@pytest.mark.asyncio
async def test_an_unreachable_site_is_not_retried_every_cycle(state):
    await confirm_platform(state)
    cid = await state.add_company(Company(name="Acme", domain="acme.example"))
    await state.add_observation("TECH_STACK", company_id=cid, collector="profile", value_text="WordPress",
                                observed_at="2026-01-01 00:00:00")
    # The next attempt found nothing (the stamp is newer than every row).
    await state.mark_company_profiled(cid)
    assert await state.count_companies_needing_profile() == 0


@pytest.mark.asyncio
async def test_classification_waits_for_site_platform_to_be_confirmed(state):
    cid = await state.add_company(Company(name="Acme", domain="acme.example"))
    await state.add_observation("TECH_STACK", company_id=cid, collector="profile", value_text="WordPress")
    assert await state.count_companies_needing_profile() == 0


# ── who the Writer will write to ──

async def company_with(state, domain, platform=None, score=None, **kw):
    cid = await state.add_company(Company(name=domain, domain=domain, **kw))
    if platform:
        await state.add_observation("SITE_PLATFORM", company_id=cid, collector="profile", value_text=platform)
    if score is not None:
        await state.add_observation("LEGACY_SITE_SCORE", company_id=cid, collector="profile", value_num=score)
    return cid


async def prospect_at(state, cid, n, status="verified"):
    return await state.add_prospect(Prospect(
        company_id=cid, first_name=f"Pat{n}", last_name="Doe", title="Owner", company="Acme",
        email=f"pat{n}@shop{n}.example", email_status=status))


@pytest.mark.asyncio
async def test_custom_segment_counts_only_verified_addresses(state):
    legacy = await company_with(state, "legacy.example", "oscommerce")
    await prospect_at(state, legacy, 1, "risky")
    assert await state.count_writable_prospects() == 0
    await prospect_at(state, legacy, 2, "verified")
    assert await state.count_writable_prospects() == 1
    # An unknown site that scores old is custom too; a modern one isn't.
    old = await company_with(state, "old.example", "unknown", score=55)
    await prospect_at(state, old, 3, "risky")
    shop = await company_with(state, "shop.example", "shopify")
    await prospect_at(state, shop, 4, "risky")
    nothing = await company_with(state, "plain.example")
    await prospect_at(state, nothing, 5, "risky")
    assert await state.count_writable_prospects() == 3
