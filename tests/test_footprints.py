"""Footprint searches for old shops: the bank, its rotation, Scout's strategy
and the discover preset. Search results are mocked; every domain is fictional."""

import os
import tempfile
from types import SimpleNamespace

import pytest
import pytest_asyncio

import harvey.agents.scout as scout_mod
from harvey import footprints
from harvey.agents.scout import MAX_QUERIES_PER_CYCLE, Scout
from harvey.agents.writer import Writer
from harvey.collectors import discover as D
from harvey.models.company import Company
from harvey.models.prospect import Prospect
from harvey.signals import seed_signal_catalog
from harvey.state import StateManager


@pytest_asyncio.fixture
async def state():
    with tempfile.TemporaryDirectory() as tmp:
        sm = StateManager(os.path.join(tmp, "f.db"))
        await sm.init_db()
        await seed_signal_catalog(sm)
        await sm.set_signal_status("SITE_PLATFORM", "confirmed")
        yield sm


# ── the bank ──

def test_footprinting_is_opt_in():
    assert footprints.query_bank([], [], ["United Kingdom"]) == []


def test_the_bank_combines_footprints_niches_and_countries():
    bank = footprints.query_bank([], ["garden furniture", "model railways"], ["United Kingdom", "Ireland"])
    per_niche = sum(len(v) for v in footprints.FOOTPRINTS.values())
    assert len(bank) == per_niche * 2 * 2
    first = bank[0]
    assert first.query == '"Powered by osCommerce" "garden furniture" "United Kingdom"'
    assert first.label == "Legacy e-commerce" and first.segment == "custom"
    # Consecutive searches use different footprints.
    assert bank[0].query.split('" "')[0] != bank[1].query.split('" "')[0]
    assert {q.label for q in bank} == {"Legacy e-commerce", "WordPress store"}
    assert all('"{' not in q.query for q in bank)


def test_operator_queries_run_as_written_and_get_a_plain_label():
    bank = footprints.query_bank(['"Powered by Actinic" "tools"', '"WooCommerce" "since 2014"'], [], [])
    assert [q.query for q in bank] == ['"Powered by Actinic" "tools"', '"WooCommerce" "since 2014"']
    assert [q.label for q in bank] == ["Legacy e-commerce", "WordPress store"]


@pytest.mark.asyncio
async def test_the_rotation_moves_on_and_wraps(state):
    bank = footprints.query_bank(["a", "b", "c"], [], [])
    assert [q.query for q in await footprints.next_queries(state, bank, 2)] == ["a", "b"]
    await footprints.advance(state, bank, 2)
    assert [q.query for q in await footprints.next_queries(state, bank, 2)] == ["c", "a"]
    await footprints.advance(state, bank, 2)
    assert [q.query for q in await footprints.next_queries(state, bank, 2)] == ["b", "c"]


def test_vendor_sites_are_never_prospects():
    assert footprints.is_vendor("forums.oscommerce.com")
    assert footprints.is_vendor("www.zen-cart.com")
    assert not footprints.is_vendor("acmegarden.example")
    assert D.is_junk("addons.oscommerce.com") and D.is_junk("cubecart.com")
    assert not D.is_junk("acmegarden.example")


# ── Scout ──

def make_scout(state, *, niches=("garden furniture",), queries=(), results=None):
    brain = SimpleNamespace(load_skills_for_agent=lambda name: "")
    icp = SimpleNamespace(industries=["Retail"], geography=["United Kingdom"], titles=["Owner"],
                          company_size="", legacy_niches=list(niches), footprint_queries=list(queries))
    config = SimpleNamespace(icp=icp, channels=SimpleNamespace(linkedin=SimpleNamespace(enabled=False)))
    env = SimpleNamespace(serper_api_key="", tinyfish_api_key="", linkedin_email="")
    scout = Scout(brain, state, config, env)
    scout.searched = []

    async def ddg(query):
        scout.searched.append(query)
        return (results or {}).get(len(scout.searched), [])
    scout._search_duckduckgo = ddg
    return scout


@pytest.fixture(autouse=True)
def no_delay(monkeypatch):
    async def instant(_seconds):
        return None
    monkeypatch.setattr(scout_mod.asyncio, "sleep", instant)


@pytest.mark.asyncio
async def test_footprint_hits_become_companies_with_a_clean_label(state):
    scout = make_scout(state, results={
        1: [("https://acmegarden.example/index.php?cPath=3", "Powered by osCommerce"),
            ("https://forums.oscommerce.com/topic/1", "osCommerce forum"),
            ("https://www.facebook.com/acmegarden", "Acme on Facebook"),
            ("https://www.oldtools.example/", "Powered by Zen Cart")],
        2: [("https://acmegarden.example/", "again")],
    })
    assert await scout._prospect_via_footprints() == 0          # companies only
    assert len(scout.searched) == scout_mod.FOOTPRINT_QUERIES_PER_CYCLE
    acme = await state.get_company_by_domain("acmegarden.example")
    tools = await state.get_company_by_domain("oldtools.example")
    assert acme and tools
    assert acme.industry == "Legacy e-commerce" and acme.source == "search:footprint"
    # Named from the domain until the profile reads the site's own name.
    assert acme.name == D.name_from_domain("acmegarden.example")
    assert not await state.company_exists("forums.oscommerce.com")
    assert not await state.company_exists("facebook.com")
    # The next cycle starts after the searches this one ran.
    assert await state.get_setting(footprints.CURSOR_KEY) == str(scout_mod.FOOTPRINT_QUERIES_PER_CYCLE)


@pytest.mark.asyncio
async def test_footprints_never_exceed_the_cycle_query_cap(state):
    scout = make_scout(state)
    scout._queries_this_cycle = MAX_QUERIES_PER_CYCLE - 1
    await scout._prospect_via_footprints()
    assert len(scout.searched) == 1
    assert scout._queries_this_cycle == MAX_QUERIES_PER_CYCLE
    assert await state.get_setting(footprints.CURSOR_KEY) == "1"
    await scout._prospect_via_footprints()
    assert len(scout.searched) == 1


@pytest.mark.asyncio
async def test_footprints_wait_for_site_platform_and_need_configuring(state):
    unconfigured = make_scout(state, niches=())
    await unconfigured._prospect_via_footprints()
    assert unconfigured.searched == []

    await state.set_signal_status("SITE_PLATFORM", "proposed")
    scout = make_scout(state)
    await scout._prospect_via_footprints()
    assert scout.searched == []


# ── the discover preset ──

def test_the_legacy_cart_preset_uses_the_bank_and_the_niches():
    config = SimpleNamespace(
        icp=SimpleNamespace(industries=["Retail"], geography=[], legacy_niches=["garden furniture"],
                            keyword_variants={}, geo_coordinates={}),
        product=SimpleNamespace(name="P"))
    assert D.preset_problem("legacy_cart", "serper", config) is None
    queries = D.build_queries(config, preset="legacy_cart")
    assert len(queries) == len(footprints.preset_footprints())
    assert all('"garden furniture"' not in q.term and "garden furniture" in q.term for q in queries)
    assert any('"Powered by osCommerce"' in q.term for q in queries)
    assert all("{" not in q.term and q.preset == "legacy_cart" for q in queries)


@pytest.mark.asyncio
async def test_a_preset_hit_is_recorded_under_its_label_not_the_query(state):
    biz = D.Business(name="Acme Garden", domain="acmegarden.example", provider="serper")
    query = D.DiscoveryQuery(term='"Powered by osCommerce" garden furniture', preset="legacy_cart")
    cid, new = await D._upsert(state, biz, query)
    assert new and (await state.get_company(cid)).industry == "Legacy e-commerce"


# ── nobody at a footprint find is written to before its profile ──

SEQ = [{"step": n, "subject": f"s{n}", "body": f"b{n} {{{{company}}}}", "delay_days": 0 if n == 1 else 3}
       for n in (1, 2, 3)]


class SequenceBrain:
    def load_skills_for_agent(self, name):
        return ""

    def load_prompt(self, name, **kw):
        return "prompt"

    async def think_json(self, prompt, **kw):
        return SEQ


async def footprint_find(state, industry="Legacy e-commerce", source="search:footprint"):
    cid = await state.add_company(Company(name="Acme Garden", domain="acmegarden.example",
                                          industry=industry, source=source))
    pid = await state.add_prospect(Prospect(
        company_id=cid, first_name="Pat", last_name="Doe", title="Owner", company="Acme Garden",
        email="pat@acmegarden.example", email_status="verified"))
    return cid, pid


@pytest.mark.asyncio
@pytest.mark.parametrize("industry, source", [
    ("Legacy e-commerce", "search:footprint"),      # Scout's footprint strategy
    ("Legacy e-commerce", "discover:serper"),       # the legacy_cart preset
])
async def test_a_footprint_find_waits_for_its_profile(state, industry, source):
    cid, pid = await footprint_find(state, industry, source)
    assert await state.count_writable_prospects() == 0
    config = SimpleNamespace(
        channels=SimpleNamespace(email=SimpleNamespace(provider="instantly", require_approval=True)),
        product=SimpleNamespace(name="P", description="D", key_benefits=[], pricing=""),
        persona=SimpleNamespace(name="S", company="C", role="R", tone="t"))
    await Writer(SequenceBrain(), state, config).run()
    assert (await state.get_prospect(pid)).status == "new"
    assert await state.get_campaigns_by_status("draft") == []

    await state.add_observation("SITE_PLATFORM", company_id=cid, collector="profile", value_text="zencart",
                                confidence=0.9)
    assert await state.count_writable_prospects() == 1
    await Writer(SequenceBrain(), state, config).run()
    assert [c.name for c in await state.get_campaigns_by_status("draft")] == ["Custom-outreach"]
