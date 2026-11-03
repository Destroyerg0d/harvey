"""Bing results: its click-tracking links are read back to the real URL, and
pages that never mention the query (what Bing serves a search it distrusts)
are dropped rather than becoming companies."""

import base64
from types import SimpleNamespace

import pytest

import harvey.agents.scout as scout_mod
from harvey.agents.scout import Scout, about_the_query, bing_target, query_terms


def wrapped(url: str) -> str:
    code = base64.urlsafe_b64encode(url.encode()).decode().rstrip("=")
    return f"https://www.bing.com/ck/a?!&&p=abc123&ptn=3&ver=2&u=a1{code}&ntb=1"


def page(*results) -> str:
    items = "".join(
        f'<li class="b_algo"><h2><a href="{href}">{title}</a></h2>'
        f'<div class="b_caption"><p>{snippet}</p></div></li>'
        for href, title, snippet in results)
    return f"<html><body><ol id='b_results'>{items}</ol></body></html>"


def make_scout(html: str):
    env = SimpleNamespace(serper_api_key="", tinyfish_api_key="", linkedin_email="")
    config = SimpleNamespace(icp=SimpleNamespace(industries=[], geography=[], titles=[]),
                             channels=SimpleNamespace(linkedin=SimpleNamespace(enabled=False)))
    scout = Scout(SimpleNamespace(load_skills_for_agent=lambda n: ""), None, config, env)

    async def fetch(url, **kwargs):
        return SimpleNamespace(text=html, status_code=200)
    scout._fetch = fetch
    return scout


@pytest.fixture(autouse=True)
def no_delay(monkeypatch):
    async def instant(_seconds):
        return None
    monkeypatch.setattr(scout_mod.asyncio, "sleep", instant)


def test_a_tracking_link_reads_back_to_the_real_url():
    assert bing_target(wrapped("https://oldtools.example/index.php?cPath=2")) == \
        "https://oldtools.example/index.php?cPath=2"
    assert bing_target("https://acmegarden.example/") == "https://acmegarden.example/"
    # Unreadable or not a web address: nothing, never bing.com itself.
    assert bing_target("https://www.bing.com/ck/a?!&&p=abc") == ""
    assert bing_target("https://www.bing.com/ck/a?u=a1!!!") == ""
    assert bing_target(wrapped("javascript:alert(1)")) == ""


def test_query_terms_skip_operators_and_generic_words():
    assert query_terms('"Powered by osCommerce" "United Kingdom"') == {"oscommerce"}
    assert query_terms('site:example.com "Zen Cart" workwear -jobs') == {"cart", "workwear"}
    # Nothing distinctive: every word counts.
    assert query_terms('"online shop" company') == {"online", "shop", "company"}
    assert about_the_query('"Powered by osCommerce"', "Acme Tools", "Powered by osCommerce")
    assert not about_the_query('"Powered by osCommerce"', "Free video chat", "Watch now")


@pytest.mark.asyncio
async def test_bing_keeps_real_results_and_drops_unrelated_ones():
    scout = make_scout(page(
        (wrapped("https://oldtools.example/"), "Old Tools Ltd", "Copyright 2009. Powered by osCommerce"),
        (wrapped("https://unrelated-videos.example/"), "Videos", "Watch the latest clips"),
        (wrapped("https://acmegarden.example/shop"), "Acme Garden oscommerce shop", ""),
        ("https://www.bing.com/ck/a?!&&p=broken", "Broken", "osCommerce"),
    ))
    results = await scout._search_bing('"Powered by osCommerce" "United Kingdom"')
    assert [url for url, _ in results] == ["https://oldtools.example/",
                                           "https://acmegarden.example/shop"]


@pytest.mark.asyncio
async def test_an_all_unrelated_bing_page_hands_over_to_the_next_backend():
    scout = make_scout(page(
        (wrapped("https://unrelated-videos.example/"), "Videos", "Watch the latest clips"),
        (wrapped("https://chat-tools.example/"), "Chat tools", "Free AI chat"),
    ))
    assert await scout._search_bing('"Powered by Zen Cart" workwear') == []

    async def nothing(query):
        return []

    async def google(query):
        return [("https://oldtools.example/", "Powered by Zen Cart workwear")]
    scout._search_duckduckgo = nothing
    scout._search_google = google
    assert await scout._web_search('"Powered by Zen Cart" workwear') == [
        ("https://oldtools.example/", "Powered by Zen Cart workwear")]
