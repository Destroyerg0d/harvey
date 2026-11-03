"""Site platform, site age facts, the legacy score, segments and the old-site gate.

Every fixture is hand-written with fictional shops: what matters is the
markup shape each kind of software leaves behind, not anyone's real page.
"""

from datetime import date

import pytest

from harvey.collectors.site_age import (
    FACT_WEIGHTS, PLATFORM_WEIGHTS, Fact, age_facts, detect_platform, has_viewport, legacy_score,
)
from harvey.segments import eligible_for_old_site_pitch, segment_of

TODAY = date(2026, 11, 4)
VIEWPORT = '<meta name="viewport" content="width=device-width, initial-scale=1">'


def page(body: str, head: str = "", doctype: str = "<!DOCTYPE html>") -> str:
    return f"{doctype}<html><head><title>Acme Garden Supplies</title>{head}</head><body>{body}</body></html>"


# ── platform ──

PLATFORM_PAGES = {
    "oscommerce": page('<a href="product_info.php?products_id=12&amp;osCsid=ab12cd">Spade</a>'
                       "<p>Copyright 2009 Acme Garden. Powered by osCommerce</p>"),
    "zencart": page('<a href="index.php?main_page=product_info&amp;cPath=2&amp;products_id=3">Rake</a>'
                    '<link href="includes/templates/acme/css/style.css" rel="stylesheet">'),
    "opencart": page('<link href="catalog/view/theme/default/stylesheet/stylesheet.css" rel="stylesheet">'
                     '<a href="index.php?route=product/product&amp;product_id=40">Hoe</a>'),
    "magento1": page('<link rel="stylesheet" href="/skin/frontend/default/acme/css/styles.css">'
                     '<script src="/js/varien/js.js"></script>'),
    "magento2": page('<script type="text/x-magento-init">{"*": {}}</script>'
                     '<link href="/static/version1612345678/frontend/Acme/garden/en_GB/css/styles-m.css">'),
    "cubecart": page('<a href="index.php?_a=viewProd&amp;productId=5">Trowel</a>'),
    "xcart": page('<link href="/skin/common_files/css/main.css"><a href="home.php?cat=248">Tools</a>'),
    "prestashop": page("<script>var prestashop = {\"currency\": {\"iso_code\": \"GBP\"}};</script>"),
    "virtuemart": page('<a href="index.php?option=com_virtuemart&amp;view=productdetails">Shears</a>'),
    "shopfactory": page('<a href="contents/en-uk/d12_Garden_Tools.html">Tools</a>'
                        "<p>Created with ShopFactory</p>"),
    "sellerdeck": page('<a href="/acatalog/Garden_Tools.html">Garden tools</a>'),
    "woocommerce": page('<link href="/wp-content/plugins/woocommerce/assets/css/woocommerce.css">'
                        '<link href="/wp-content/themes/acme/style.css"><body class="home woocommerce-page">'),
    "wordpress": page('<link href="/wp-content/themes/acme/style.css">',
                      '<meta name="generator" content="WordPress 6.4.2">'),
    "shopify": page('<link href="//cdn.shopify.com/s/files/1/0000/0001/t/2/assets/theme.css">'
                    '<script>Shopify.theme = {"name": "Dawn"};</script>', VIEWPORT),
    "wix": page('<img src="https://static.wixstatic.com/media/acme.jpg">'),
    "squarespace": page('<link href="https://static1.squarespace.com/static/acme/site.css">'),
    "webflow": page('<html data-wf-page="abc" data-wf-site="def"><p>Acme</p>'),
    "ecwid": page('<script src="https://app.ecwid.com/script.js?1234"></script>'),
    "bigcommerce": page('<link href="https://cdn11.bigcommerce.com/s-abc/stencil/theme.css">'),
}


@pytest.mark.parametrize("expected", sorted(PLATFORM_PAGES))
def test_each_platform_is_recognised(expected):
    platform, conf, evidence = detect_platform(PLATFORM_PAGES[expected])
    assert platform == expected
    assert conf >= 0.7 and evidence


def test_magento_1_and_2_are_told_apart():
    assert detect_platform(PLATFORM_PAGES["magento1"])[0] == "magento1"
    assert detect_platform(PLATFORM_PAGES["magento2"])[0] == "magento2"


def test_woocommerce_beats_wordpress_and_wordpress_beats_an_embedded_ecwid():
    both = PLATFORM_PAGES["woocommerce"]
    assert detect_platform(both)[0] == "woocommerce"
    embedded = page('<link href="/wp-content/themes/acme/style.css">'
                    '<script src="https://app.ecwid.com/script.js?99"></script>')
    assert detect_platform(embedded)[0] == "wordpress"


def test_a_footer_credit_split_by_a_link_still_reads():
    html = page('<p>Powered by&nbsp;<a href="https://cart.example/">CubeCart</a></p>')
    platform, _, evidence = detect_platform(html)
    assert platform == "cubecart" and "powered by cubecart" in evidence


def test_headers_and_generators_count():
    assert detect_platform(page("<p>Shop</p>"), {"Set-Cookie": "osCsid=abc; path=/"})[0] == "oscommerce"
    assert detect_platform(page("<p>Shop</p>"), {"X-ShopId": "123"})[0] == "shopify"
    gen = page("<p>Shop</p>", '<meta content="Zen Cart, http://www.zen-cart.com" name="generator">')
    assert detect_platform(gen)[0] == "zencart"


def test_a_hand_built_php_site_is_custom_php():
    html = page('<a href="about.php">About</a><a href="products.php?cat=2">Products</a>'
                '<form action="contact.php" method="post"><input name="email"></form>')
    platform, conf, evidence = detect_platform(html, url="http://acme.example/")
    assert platform == "custom_php" and ".php" in evidence


def test_custom_php_needs_the_sites_own_links_and_no_cms():
    # One .php link to someone else's site, one of its own: not enough.
    html = page('<a href="https://partner.example/shop.php">Partner</a><a href="index.php">Home</a>')
    assert detect_platform(html, url="https://acme.example/")[0] == "unknown"
    # The same with a PHP server header is.
    assert detect_platform(html, {"X-Powered-By": "PHP/5.6.40"},
                           url="https://acme.example/")[0] == "custom_php"
    # A CMS that names itself is never custom_php.
    drupal = page('<a href="a.php">A</a><a href="b.php">B</a>', '<meta name="generator" content="Drupal 7">')
    assert detect_platform(drupal)[0] == "unknown"
    # An HTML editor's generator is a hand-built site, still custom_php.
    frontpage = page('<a href="a.php">A</a><a href="b.php">B</a>',
                     '<meta name="GENERATOR" content="Microsoft FrontPage 4.0">')
    assert detect_platform(frontpage)[0] == "custom_php"


def test_mentioning_a_platform_is_not_running_it():
    html = page("<p>We moved our shop off osCommerce and Zen Cart years ago, and now sell on Shopify.</p>"
                '<a href="/about.html">About</a>', VIEWPORT)
    assert detect_platform(html)[0] == "unknown"


# ── age facts ──

def codes(facts, claimable=True):
    return {f.code for f in facts if f.claimable == claimable}


def test_a_modern_responsive_shop_has_no_age_facts():
    html = page('<link href="//cdn.shopify.com/s/files/1/a.css"><footer>&copy; 2026 Acme Garden</footer>',
                VIEWPORT)
    facts = age_facts(html, {"final_url": "https://acme.example/"}, today=TODAY)
    assert facts == []


def test_no_viewport_is_claimable_only_when_a_phone_gets_none_either():
    html = page("<p>Welcome to Acme Garden</p>")
    checked = {"final_url": "https://acme.example/", "mobile_checked": True,
               "mobile_html": html, "mobile_final_url": "https://acme.example/"}
    [fact] = [f for f in age_facts(html, checked, today=TODAY) if f.code == "NO_MOBILE_VIEWPORT"]
    assert fact.claimable and "phones show the desktop layout" in fact.sentence

    unchecked = [f for f in age_facts(html, {"final_url": "https://acme.example/"}, today=TODAY)
                 if f.code == "NO_MOBILE_VIEWPORT"]
    assert unchecked and not unchecked[0].claimable


def test_an_m_site_or_a_mobile_page_means_no_viewport_fact():
    html = page("<p>Welcome</p>")
    to_m = {"final_url": "https://acme.example/", "mobile_checked": True,
            "mobile_html": page("<p>Mobile</p>", VIEWPORT), "mobile_final_url": "https://m.acme.example/"}
    assert "NO_MOBILE_VIEWPORT" not in {f.code for f in age_facts(html, to_m, today=TODAY)}
    served = {"final_url": "https://acme.example/", "mobile_checked": True,
              "mobile_html": page("<p>Mobile</p>", VIEWPORT), "mobile_final_url": "https://acme.example/"}
    assert "NO_MOBILE_VIEWPORT" not in {f.code for f in age_facts(html, served, today=TODAY)}
    alternate = page("<p>Welcome</p>", '<link rel="alternate" media="only screen and (max-width: 640px)" '
                                       'href="https://m.acme.example/">')
    assert "NO_MOBILE_VIEWPORT" not in {f.code for f in age_facts(alternate, {}, today=TODAY)}
    # A viewport tag that is commented out is no viewport.
    assert not has_viewport(page("<p>x</p>", "<!-- " + VIEWPORT + " -->"))


def test_http_only_needs_proof():
    html = page("<p>Shop</p>", VIEWPORT)
    proven = {"final_url": "http://acme.example/", "https_error": "connect", "https_answered": False}
    [fact] = [f for f in age_facts(html, proven, today=TODAY) if f.code == "HTTP_ONLY"]
    assert fact.claimable and "Not secure" in fact.sentence

    timed_out = {"final_url": "http://acme.example/", "https_error": "timeout", "https_answered": False}
    [slow] = [f for f in age_facts(html, timed_out, today=TODAY) if f.code == "HTTP_ONLY"]
    assert not slow.claimable

    answered = {"final_url": "http://acme.example/", "https_error": "status", "https_answered": True}
    assert not [f for f in age_facts(html, answered, today=TODAY) if f.code == "HTTP_ONLY"][0].claimable
    secure = {"final_url": "https://acme.example/"}
    assert "HTTP_ONLY" not in {f.code for f in age_facts(html, secure, today=TODAY)}


@pytest.mark.parametrize("footer, expected", [
    ("<footer>Copyright &copy; 2015 Acme Garden Ltd. All rights reserved.</footer>", 2015),
    ("<footer>&copy; Acme Garden Ltd. 2012 | Privacy</footer>", 2012),
    ("<footer>Copyright 2004 - 2018 Acme Garden</footer>", 2018),
    ("<footer>&copy; 2022 Acme Garden</footer>", 2022),
    ("<footer>&copy; 2023 Acme Garden</footer>", None),          # three years: not old
    ("<footer>&copy; 2010-2026 Acme Garden</footer>", None),     # kept current
    ("<footer>&copy; Acme Garden Ltd, trading since 1996</footer>", None),
    ("<p>Established 1996.</p><footer>&copy; 2026 Acme</footer>", None),
])
def test_copyright_year(footer, expected):
    facts = [f for f in age_facts(page(footer, VIEWPORT), {}, today=TODAY) if f.code == "OLD_COPYRIGHT"]
    if expected is None:
        assert facts == []
    else:
        assert facts[0].claimable and facts[0].sentence.endswith(str(expected))


def test_a_script_that_writes_the_year_makes_the_old_one_unclaimable():
    html = page('<footer>&copy; <span id="y">2014</span> Acme<script>'
                'document.getElementById("y").textContent = new Date().getFullYear();</script></footer>', VIEWPORT)
    [fact] = [f for f in age_facts(html, {}, today=TODAY) if f.code == "OLD_COPYRIGHT"]
    assert not fact.claimable


def test_old_jquery_and_old_wordpress():
    old = page('<script src="/wp-includes/js/jquery/jquery.js?ver=1.12.4"></script>'
               '<script src="https://code.jquery.com/jquery-1.8.3.min.js"></script>'
               '<script src="/js/jquery-ui-1.8.custom.min.js"></script>',
               VIEWPORT + '<meta name="generator" content="WordPress 4.9.8">')
    facts = {f.code: f for f in age_facts(old, {}, today=TODAY)}
    assert "jQuery 1.8.3" in facts["OLD_JQUERY"].sentence
    assert "WordPress 4.9.8" in facts["OLD_WORDPRESS"].sentence and facts["OLD_WORDPRESS"].claimable

    new = page('<script src="/wp-includes/js/jquery/jquery.min.js?ver=3.7.1"></script>',
               VIEWPORT + '<meta name="generator" content="WordPress 6.4.2">')
    assert codes(age_facts(new, {}, today=TODAY)) == set()


def test_magento_1_tables_flash_doctype_and_notices():
    html = page(
        '<link href="/skin/frontend/default/acme/css/styles.css">'
        '<table width="780" cellpadding="0"><tr><td><table border="0" width="100%"><tr><td>'
        '<table bgcolor="#ffffff"><tr><td><font face="Arial">Welcome</font> <center>Offers</center>'
        "</td></tr></table></td></tr></table></td></tr></table>"
        '<object type="application/x-shockwave-flash" data="intro.swf"></object>'
        "<p>Due to COVID-19, deliveries may be delayed.</p>"
        "<p>This site is best viewed in Internet Explorer at 1024x768.</p>",
        doctype='<!DOCTYPE HTML PUBLIC "-//W3C//DTD HTML 4.01 Transitional//EN">')
    facts = age_facts(html, {}, today=TODAY)
    assert {"MAGENTO1", "TABLE_LAYOUT", "FLASH", "OLD_DOCTYPE", "OUTDATED_NOTICE"} <= codes(facts)
    by = {f.code: f for f in facts}
    assert "HTML 4.01" in by["OLD_DOCTYPE"].sentence
    assert "3 HTML tables" in by["TABLE_LAYOUT"].sentence
    assert "COVID-19" in by["OUTDATED_NOTICE"].sentence


def test_ordinary_sentences_are_not_outdated_notices():
    html = page("<p>During COVID we moved our whole shop online.</p>"
                "<p>Our roses are best viewed in person at the nursery.</p>", VIEWPORT)
    assert "OUTDATED_NOTICE" not in {f.code for f in age_facts(html, {}, today=TODAY)}


def test_a_flash_script_alone_is_not_claimable():
    html = page('<script src="/js/swfobject.js"></script>', VIEWPORT)
    [fact] = [f for f in age_facts(html, {}, today=TODAY) if f.code == "FLASH"]
    assert not fact.claimable


# ── legacy score ──

def test_legacy_score_weights():
    facts = [Fact("NO_MOBILE_VIEWPORT", "s", 0.9, True), Fact("OLD_COPYRIGHT", "s", 0.85, True)]
    expected = PLATFORM_WEIGHTS["oscommerce"] + 25 * 0.9 + 15 * 0.85
    assert legacy_score("oscommerce", facts) == round(expected) == 70
    assert legacy_score("shopify", []) == 0
    every = [Fact(code, "s", 1.0, True) for code in FACT_WEIGHTS]
    assert legacy_score("oscommerce", every) == 100          # capped
    # The same fact twice counts once.
    assert legacy_score("unknown", [Fact("FLASH", "s", 1.0, True)] * 2) == 25


# ── segments ──

@pytest.mark.parametrize("platform, tech, industry, score, expected", [
    ("shopify", [], "", None, "shopify"),
    ("woocommerce", [], "", None, "wordpress"),
    ("wordpress", [], "", 80, "wordpress"),
    ("oscommerce", [], "", 10, "custom"),
    ("custom_php", [], "", None, "custom"),
    ("unknown", [], "", 55, "custom"),          # an old site nothing recognised
    ("wix", [], "", 20, "other"),
    ("webflow", [], "", None, "other"),
    ("unknown", ["WordPress"], "", 10, "wordpress"),
    (None, ["Shopify"], "", None, "shopify"),
    (None, [], "WordPress store", None, "wordpress"),
    (None, [], "Legacy e-commerce", None, "other"),   # a search label is never proof
    (None, [], "Roofing", None, "other"),
])
def test_segment_of(platform, tech, industry, score, expected):
    assert segment_of(platform, tech, industry, score) == expected


def test_the_old_site_gate():
    alive = {"site_ok": True, "has_mx": True, "dissolved": False}
    fact = [{"code": "OLD_COPYRIGHT", "sentence": "the copyright notice on the home page still says 2015"}]
    assert eligible_for_old_site_pitch({**alive, "age_facts": fact})
    assert eligible_for_old_site_pitch({**alive, "legacy_score": 45})
    assert not eligible_for_old_site_pitch({**alive, "legacy_score": 39})        # nothing to say
    assert not eligible_for_old_site_pitch({**alive, "age_facts": fact, "has_mx": False})
    assert not eligible_for_old_site_pitch({**alive, "age_facts": fact, "site_ok": False})
    assert not eligible_for_old_site_pitch({**alive, "age_facts": fact, "dissolved": True})
    assert not eligible_for_old_site_pitch({})
