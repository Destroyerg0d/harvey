"""What a home page says about the software behind it, and how old it is.

Pure functions over HTML Harvey already fetched (plus the facts of that
fetch: where it ended up, whether https answered, what a phone was served).
No network, no model. ``ProfileCollector.profile_company`` calls them and
records ``SITE_PLATFORM``, ``LEGACY_SITE_SCORE`` and ``SITE_AGE_FACTS``.

Every age fact carries a plain-English ``sentence`` the Writer may repeat,
and a ``claimable`` flag. A fact is claimable only when what was seen proves
it: "no mobile viewport" needs a phone's fetch to lack one too, and "only
works over http" needs https to have failed to connect, not merely to have
been slow. An email that tells an owner something false about their own
site is worse than one that says nothing about it, so a fact that is
merely likely is kept (it still counts toward the score) but never claimed.
"""

import re
from dataclasses import dataclass
from datetime import date
from html import unescape
from urllib.parse import urlparse

# The ids SITE_PLATFORM records. Lowercase and stable: other modules map
# them to segments (harvey/segments.py).
PLATFORMS = (
    "shopify", "wordpress", "woocommerce", "magento1", "magento2", "oscommerce",
    "zencart", "opencart", "prestashop", "virtuemart", "joomla", "cubecart", "xcart",
    "ecwid", "bigcommerce", "shopfactory", "sellerdeck", "wix", "squarespace",
    "webflow", "custom_php", "unknown",
)

# How a platform is named in an email's FACTS.
PLATFORM_LABELS = {
    "shopify": "Shopify",
    "wordpress": "WordPress",
    "woocommerce": "WooCommerce (WordPress)",
    "magento1": "Magento 1",
    "magento2": "Magento 2",
    "oscommerce": "osCommerce",
    "zencart": "Zen Cart",
    "opencart": "OpenCart",
    "prestashop": "PrestaShop",
    "virtuemart": "VirtueMart (Joomla)",
    "joomla": "Joomla",
    "cubecart": "CubeCart",
    "xcart": "X-Cart",
    "ecwid": "Ecwid",
    "bigcommerce": "BigCommerce",
    "shopfactory": "ShopFactory",
    "sellerdeck": "SellerDeck (Actinic)",
    "wix": "Wix",
    "squarespace": "Squarespace",
    "webflow": "Webflow",
    "custom_php": "a custom PHP site (no off-the-shelf shop software detected)",
    "unknown": "",
}

# A platform whose best marker reaches this is decided by precedence (below);
# weaker markers only count when nothing reaches it.
STRONG = 0.7
WEAK = 0.5

# When two platforms both match strongly, the more specific wins: a
# WooCommerce shop is also a WordPress site, a VirtueMart shop a Joomla one,
# and an Ecwid store embedded in WordPress is a WordPress site.
PRECEDENCE = (
    "shopify", "magento2", "magento1", "woocommerce", "prestashop", "opencart",
    "zencart", "oscommerce", "cubecart", "xcart", "virtuemart", "sellerdeck",
    "shopfactory", "bigcommerce", "wordpress", "ecwid", "joomla", "wix",
    "squarespace", "webflow",
)

# (platform, regex over the raw HTML, confidence, what was seen)
_HTML_MARKERS: tuple[tuple[str, str, float, str], ...] = (
    ("shopify", r"cdn\.shopify\.com/s/", 0.95, "cdn.shopify.com assets"),
    ("shopify", r"\bShopify\.(?:theme|shop|routes)\b", 0.95, "the Shopify theme script"),
    ("shopify", r"[\w-]+\.myshopify\.com", 0.85, "a myshopify.com address"),
    ("magento2", r"text/x-magento-init", 0.95, "Magento 2 init scripts"),
    ("magento2", r"\bdata-mage-init\b", 0.9, "Magento 2 widgets"),
    ("magento2", r"/static/(?:version\d+/)?frontend/[\w-]+/[\w-]+/[a-z]{2}_[A-Z]{2}/", 0.9,
     "Magento 2 static paths"),
    ("magento1", r"/skin/frontend/", 0.9, "'/skin/frontend/' paths (Magento 1)"),
    ("magento1", r"/js/(?:mage|varien)/", 0.85, "'/js/varien/' scripts (Magento 1)"),
    ("magento1", r"\bMage\.Cookies\b", 0.9, "Magento 1 cookie script"),
    ("woocommerce", r"/wp-content/plugins/woocommerce/", 0.95, "the WooCommerce plugin"),
    ("woocommerce", r"""class=["'][^"']*\bwoocommerce\b""", 0.85, "WooCommerce page classes"),
    ("woocommerce", r"\bwc_add_to_cart_params\b|\bwoocommerce_params\b|[?&]wc-ajax=", 0.9,
     "WooCommerce scripts"),
    ("prestashop", r"\bvar\s+prestashop\s*=|\bprestashop\s*=\s*\{", 0.9, "the PrestaShop script"),
    ("prestashop", r"/modules/ps_\w+/", 0.85, "PrestaShop modules"),
    ("opencart", r"index\.php\?route=(?:common|product|checkout|account|information)/", 0.9,
     "'index.php?route=' links (OpenCart)"),
    ("opencart", r"catalog/view/(?:theme|javascript)/", 0.9, "OpenCart theme paths"),
    ("zencart", r"index\.php\?main_page=", 0.9, "'index.php?main_page=' links (Zen Cart)"),
    ("zencart", r"\bzenid=", 0.9, "Zen Cart session links"),
    ("zencart", r"includes/templates/[\w-]+/(?:css|images|jscript)/", 0.7, "Zen Cart template paths"),
    ("oscommerce", r"\bosCsid=", 0.95, "osCommerce session links"),
    ("oscommerce", r"\bproduct_info\.php\?(?:[^\"'>\s]*&(?:amp;)?)?products_id=", 0.75,
     "'product_info.php' links (osCommerce)"),
    ("oscommerce", r"\bindex\.php\?cPath=\d", 0.6, "'index.php?cPath=' links"),
    ("cubecart", r"index\.php\?_a=(?:viewprod|viewcat|category|product|basket|login|register|account)\b",
     0.9, "'index.php?_a=' links (CubeCart)"),
    ("xcart", r"/skin/common_files/", 0.9, "X-Cart skin paths"),
    ("xcart", r"\bxcart_web_dir\b", 0.9, "the X-Cart script"),
    ("xcart", r"\b(?:home|product|cart)\.php\?(?:cat|productid|mode)=", 0.7, "X-Cart style links"),
    ("virtuemart", r"option=com_virtuemart|/components/com_virtuemart/", 0.95, "VirtueMart links"),
    ("joomla", r"/media/jui/|/media/system/js/(?:core|mootools)", 0.8, "Joomla system scripts"),
    ("joomla", r"option=com_content\b", 0.7, "Joomla content links"),
    ("sellerdeck", r"/acatalog/", 0.9, "'/acatalog/' pages (SellerDeck/Actinic)"),
    ("sellerdeck", r"\bactinic[\w-]*\.js\b|\bACTINIC_\w+", 0.85, "Actinic scripts"),
    ("shopfactory", r"/contents/[a-z]{2}-[a-z]{2}/", 0.8, "ShopFactory page paths"),
    ("ecwid", r"app\.ecwid\.com|\becwid_product", 0.9, "the Ecwid store script"),
    ("bigcommerce", r"cdn\d*\.bigcommerce\.com|\bstencil-utils\b", 0.9, "BigCommerce assets"),
    ("wordpress", r"/wp-content/(?:themes|plugins|uploads)/", 0.9, "wp-content paths"),
    ("wordpress", r"/wp-includes/", 0.9, "wp-includes paths"),
    ("wix", r"static\.parastorage\.com|static\.wixstatic\.com", 0.95, "Wix assets"),
    ("squarespace", r"static1\.squarespace\.com|<!-- This is Squarespace\. -->", 0.95,
     "Squarespace assets"),
    ("webflow", r"assets\.website-files\.com|cdn\.prod\.website-files\.com|\bdata-wf-(?:page|site|domain)\b",
     0.95, "Webflow assets"),
)

# Footer credits in the visible text: what old shop software prints itself.
_TEXT_MARKERS: tuple[tuple[str, str, float], ...] = (
    ("oscommerce", r"\bpowered by oscommerce\b", 0.95),
    ("zencart", r"\bpowered by zen ?cart\b", 0.95),
    ("cubecart", r"\b(?:powered by|e-?commerce by|shopping cart software by) cubecart\b", 0.95),
    ("shopfactory", r"\b(?:created|made|built|powered) (?:with|by) shopfactory\b", 0.95),
    ("sellerdeck", r"\b(?:powered by|created with|e-?commerce by|built with) (?:sellerdeck|actinic)\b", 0.95),
    ("xcart", r"\bpowered by x-?cart\b", 0.95),
    ("opencart", r"\bpowered by opencart\b", 0.95),
    ("prestashop", r"\b(?:powered by|e-?commerce software by) prestashop\b", 0.9),
    ("virtuemart", r"\bpowered by virtuemart\b", 0.9),
    ("woocommerce", r"\bpowered by woocommerce\b", 0.9),
    ("shopify", r"\bpowered by shopify\b", 0.9),
    ("bigcommerce", r"\bpowered by bigcommerce\b", 0.9),
    ("ecwid", r"\bpowered by ecwid\b", 0.9),
    ("wordpress", r"\bproudly powered by wordpress\b", 0.9),
)

# <meta name="generator" content="..."> → platform. A generator naming
# something else (Drupal, TYPO3...) means a CMS, so never custom_php.
_GENERATORS: tuple[tuple[str, str], ...] = (
    ("woocommerce", r"^woocommerce\b"),
    ("wordpress", r"^wordpress\b"),
    ("joomla", r"^joomla\b"),
    ("prestashop", r"^prestashop\b"),
    ("oscommerce", r"^oscommerce\b"),
    ("zencart", r"^zen ?cart\b"),
    ("cubecart", r"^cubecart\b"),
    ("shopfactory", r"^shopfactory\b"),
    ("sellerdeck", r"^(?:sellerdeck|actinic)\b"),
    ("xcart", r"^x-?cart\b"),
    ("opencart", r"^opencart\b"),
    ("wix", r"^wix\.com\b"),
    ("squarespace", r"^squarespace\b"),
    ("webflow", r"^webflow\b"),
)
# Generators that are HTML editors, not a CMS: a hand-built site.
_EDITOR_GENERATORS = re.compile(
    r"^(?:microsoft frontpage|adobe dreamweaver|dreamweaver|netobjects|microsoft word|"
    r"mozilla/|notepad|coffeecup|namo webeditor|homesite|adobe golive|serif webplus|"
    r"web page maker|xara|rapidweaver|sitegrinder|kompozer|bluefish|mshtml)", re.I)

# Response headers: lowercase name → (regex over the value, platform, confidence).
_HEADER_MARKERS: tuple[tuple[str, str, str, float], ...] = (
    ("x-shopify-stage", r".", "shopify", 0.95),
    ("x-shopid", r".", "shopify", 0.95),
    ("powered-by", r"shopify", "shopify", 0.95),
    ("x-magento-cache-debug", r".", "magento2", 0.85),
    ("x-magento-tags", r".", "magento2", 0.85),
    ("x-powered-by", r"prestashop", "prestashop", 0.9),
    ("x-wix-request-id", r".", "wix", 0.95),
    ("set-cookie", r"\bosCsid=", "oscommerce", 0.95),
    ("set-cookie", r"\bzenid=", "zencart", 0.95),
    ("set-cookie", r"\bOCSESSID=", "opencart", 0.9),
    ("set-cookie", r"\bPrestaShop-", "prestashop", 0.9),
    ("set-cookie", r"(?:^|[\s,;])frontend=", "magento1", 0.75),
    ("set-cookie", r"\bxid_[0-9a-f]+=", "xcart", 0.85),
)

_COMMENT = re.compile(r"(?s)<!--.*?-->")
_SCRIPT_STYLE = re.compile(r"(?is)<(script|style|noscript)\b.*?</\1\s*>")
_TAG = re.compile(r"(?s)<[^>]+>")
_META_TAG = re.compile(r"(?is)<meta\b[^>]*>")
_LINK_TAG = re.compile(r"(?is)<link\b[^>]*>")


def _attr(tag: str, name: str) -> str:
    m = re.search(rf"""(?is)\b{name}\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s>]+))""", tag)
    return unescape(next(g for g in m.groups() if g is not None)).strip() if m else ""


def _without_comments(html: str) -> str:
    return _COMMENT.sub(" ", html or "")


def visible_text(html: str) -> str:
    """The words a visitor reads: no scripts, styles or comments, entities decoded."""
    text = _SCRIPT_STYLE.sub(" ", _without_comments(html))
    text = re.sub(r"(?i)<(?:br|/p|/div|/li|/tr|/td|/h[1-6]|/footer)\b[^>]*>", "\n", text)
    return re.sub(r"[ \t\r\f\v\xa0]+", " ", unescape(_TAG.sub(" ", text)))


def generators(html: str) -> list[str]:
    """Every <meta name="generator"> value on the page."""
    out = []
    for tag in _META_TAG.findall(_without_comments(html)):
        if _attr(tag, "name").lower() == "generator":
            content = _attr(tag, "content")
            if content:
                out.append(content)
    return out


def _host(url: str) -> str:
    return (urlparse(url or "").hostname or "").lower().removeprefix("www.")


def _php_links(html: str, host: str = "") -> set[str]:
    """Distinct same-site paths ending in .php that links and forms point to."""
    paths = set()
    for link in re.findall(r"""(?i)\b(?:href|action|src)\s*=\s*["']([^"'#\s]+)["']""",
                           _SCRIPT_STYLE.sub(" ", _without_comments(html))):
        parsed = urlparse(unescape(link))
        if parsed.scheme and parsed.scheme not in ("http", "https"):
            continue
        if parsed.netloc and (not host or _host(link) != host):
            continue
        if re.search(r"\.(?:php\d?|phtml)$", parsed.path, re.I):
            paths.add(parsed.path.lower())
    return paths


def detect_platform(html: str, headers: dict | None = None, url: str = "") -> tuple[str, float, str]:
    """(platform id, confidence 0-1, what was seen) for a home page.

    ``headers`` are the response headers (any case); ``url`` is where the
    page was served from, so absolute links to the site itself count as its
    own. ``custom_php`` only when the site's own links and forms go to
    ``.php`` pages and no CMS or shop software matched; ``unknown`` when
    nothing says either way.
    """
    html = html or ""
    headers = {str(k).lower(): str(v) for k, v in (headers or {}).items()}
    best: dict[str, tuple[float, str]] = {}

    def seen(platform: str, conf: float, evidence: str):
        if conf > best.get(platform, (0.0, ""))[0]:
            best[platform] = (conf, evidence)

    for platform, pattern, conf, evidence in _HTML_MARKERS:
        if re.search(pattern, html, re.I if platform != "magento2" else 0):
            seen(platform, conf, evidence)
    text = visible_text(html).lower()
    for platform, pattern, conf in _TEXT_MARKERS:
        m = re.search(pattern, text)
        if m:
            seen(platform, conf, f'"{m.group(0)}" in the page text')
    cms_generator = False
    for gen in generators(html):
        low = gen.lower()
        for platform, pattern in _GENERATORS:
            if re.search(pattern, low):
                seen(platform, 0.95, f'generator tag "{gen[:60]}"')
                break
        else:
            if not _EDITOR_GENERATORS.search(gen):
                cms_generator = True
    for name, value in headers.items():
        for header, pattern, platform, conf in _HEADER_MARKERS:
            if name == header and re.search(pattern, value, re.I if header != "set-cookie" else 0):
                seen(platform, conf, f"the {header} response header")

    for platform in PRECEDENCE:
        conf, evidence = best.get(platform, (0.0, ""))
        if conf >= STRONG:
            return platform, conf, evidence
    if best:
        platform, (conf, evidence) = max(best.items(), key=lambda kv: kv[1][0])
        if conf >= WEAK:
            return platform, conf, evidence
    if not cms_generator:
        php = _php_links(html, _host(url))
        on_php = bool(re.search(r"\bphp\b", headers.get("x-powered-by", ""), re.I))
        if len(php) >= 2 or (php and on_php):
            conf = 0.8 if on_php else 0.7
            sample = ", ".join(sorted(php)[:3])
            return "custom_php", conf, f"its own links go to .php pages ({sample})"
    return "unknown", 0.5, ""


# ── Age facts ──

@dataclass(frozen=True)
class Fact:
    """One thing a home page shows about its age.

    ``sentence`` is plain English the Writer may repeat when ``claimable``;
    ``evidence`` is what was seen, for the owner and the logs.
    """

    code: str
    sentence: str
    confidence: float
    claimable: bool
    evidence: str = ""

    def as_dict(self) -> dict:
        return {"code": self.code, "sentence": self.sentence}


FACT_CODES = (
    "NO_MOBILE_VIEWPORT", "HTTP_ONLY", "OLD_COPYRIGHT", "OLD_JQUERY", "OLD_WORDPRESS",
    "MAGENTO1", "TABLE_LAYOUT", "FLASH", "OLD_DOCTYPE", "OUTDATED_NOTICE",
)

# A copyright year this many years behind the current one is "old".
OLD_COPYRIGHT_YEARS = 4

_VIEWPORT = re.compile(r"""(?is)<meta\b[^>]*\bname\s*=\s*["']?viewport\b""")
# Hosts and paths a phone is sent to when a site keeps a separate mobile site.
_MOBILE_HOST = re.compile(r"^(?:m|mobile|mobi|touch|wap)\.", re.I)
_MOBILE_PATH = re.compile(r"^/(?:m|mobile|mobi)(?:/|$|\.)", re.I)
# What an https failure was: "connect" is a refused connection or a failed
# TLS handshake (the proof HTTP_ONLY needs); "timeout" and "dns" are not.
HTTPS_PROOF = "connect"


def has_viewport(html: str) -> bool:
    """True when the page declares a mobile viewport (outside comments)."""
    return bool(_VIEWPORT.search(_without_comments(html)))


def _mobile_alternate(html: str) -> bool:
    """<link rel="alternate" media="handheld"> (or a max-width media query):
    the site points phones at a separate mobile version."""
    for tag in _LINK_TAG.findall(_without_comments(html)):
        if "alternate" in _attr(tag, "rel").lower() and re.search(
                r"handheld|max-width", _attr(tag, "media"), re.I):
            return True
    return False


def _sent_to_mobile_site(desktop_url: str, mobile_url: str) -> bool:
    if not mobile_url:
        return False
    parsed = urlparse(mobile_url)
    host = (parsed.hostname or "").lower()
    if _MOBILE_HOST.match(host) or _MOBILE_PATH.match(parsed.path or ""):
        return True
    return bool(desktop_url) and _host(mobile_url) != _host(desktop_url) and "mobile" in host


def _viewport_fact(html: str, fetch: dict) -> Fact | None:
    if has_viewport(html):
        return None
    desktop_url = fetch.get("final_url") or fetch.get("url") or ""
    if _mobile_alternate(html):
        return None         # phones are sent to a mobile version
    sentence = "the home page has no mobile viewport tag, so phones show the desktop layout shrunk down"
    if not fetch.get("mobile_checked"):
        return Fact("NO_MOBILE_VIEWPORT", sentence, 0.5, False,
                    "no viewport tag on the desktop page; not checked with a phone")
    if _sent_to_mobile_site(desktop_url, fetch.get("mobile_final_url", "")):
        return None         # a phone is redirected to an m. site
    mobile_html = fetch.get("mobile_html") or ""
    if not mobile_html:
        return Fact("NO_MOBILE_VIEWPORT", sentence, 0.5, False,
                    "no viewport tag on the desktop page; the phone fetch got no page")
    if has_viewport(mobile_html) or _mobile_alternate(mobile_html):
        return None         # phones are served a different, mobile page
    return Fact("NO_MOBILE_VIEWPORT", sentence, 0.9, True,
                "no viewport tag, on the desktop page or the page served to a phone")


def _http_only_fact(fetch: dict) -> Fact | None:
    final = fetch.get("final_url") or ""
    if urlparse(final).scheme != "http":
        return None
    sentence = ("the site only loads over plain http (a secure https connection fails), "
                "so browsers mark it Not secure")
    error = fetch.get("https_error") or ""
    if error == HTTPS_PROOF and not fetch.get("https_answered"):
        return Fact("HTTP_ONLY", sentence, 0.9, True,
                    f"https failed to connect; {final} answered over http without redirecting")
    return Fact("HTTP_ONLY", sentence, 0.3, False,
                f"served over http; https was not proven broken ({error or 'not tried'})")


_COPYRIGHT = re.compile(r"(?i)(?:©|\(c\)|\bcopyright\b)")
_YEAR = re.compile(r"\b(19[89]\d|20\d{2})\b")
# "© Acme Ltd, trading since 1996": a year that dates the business, not the notice.
_NOT_COPYRIGHT_YEAR = re.compile(
    r"(?i)\b(?:since|est\.?|established|founded|trading|registered|reg\.?|company no\.?|in business)\s*$")


def _copyright_fact(html: str, today: date) -> Fact | None:
    text = visible_text(html)
    years, snippet = [], ""
    for m in _COPYRIGHT.finditer(text):
        # The notice runs to the end of its line or sentence ("| Privacy" starts the next).
        window = re.split(r"\n|\||(?<=\.)\s+(?=[A-Z])|\s[·•]\s", text[m.start():m.start() + 70])[0]
        found = [int(y.group(1)) for y in _YEAR.finditer(window)
                 if int(y.group(1)) <= today.year
                 and not _NOT_COPYRIGHT_YEAR.search(window[:y.start()][-24:])]
        if found:
            years.extend(found)
            snippet = re.sub(r"\s+", " ", window).strip()
    if not years:
        return None
    year = max(years)
    if year > today.year - OLD_COPYRIGHT_YEARS:
        return None
    # A script that writes the current year over the static one: visitors
    # see this year, so the old year in the HTML is not what they read.
    dynamic = bool(re.search(r"getFullYear\s*\(|\bdate\s*\(\s*['\"]Y['\"]", html or ""))
    return Fact("OLD_COPYRIGHT", f"the copyright notice on the home page still says {year}",
                0.4 if dynamic else 0.85, not dynamic, snippet[:80])


_JQUERY = (
    re.compile(r"(?i)jquery[-.](1\.\d{1,2}(?:\.\d{1,2})?)(?:[-.](?:min|pack|slim))*\.js"),
    re.compile(r"(?i)/jquery/(1\.\d{1,2}(?:\.\d{1,2})?)/jquery"),
    re.compile(r"(?i)/jquery(?:\.min)?\.js\?ver=(1\.\d{1,2}(?:\.\d{1,2})?)"),
)


def _version_key(version: str) -> tuple:
    return tuple(int(p) for p in re.findall(r"\d+", version))


def _jquery_fact(html: str) -> Fact | None:
    raw = _without_comments(html)
    versions = {m.group(1) for r in _JQUERY for m in r.finditer(raw)}
    if not versions:
        return None
    oldest = min(versions, key=_version_key)
    return Fact("OLD_JQUERY", f"the site still loads jQuery {oldest}, from the old 1.x line",
                0.8, True, f"jQuery {oldest} script")


def _wordpress_fact(html: str) -> Fact | None:
    version, conf, where = "", 0.0, ""
    for gen in generators(html):
        m = re.match(r"(?i)wordpress\s+(\d+(?:\.\d+){0,2})", gen)
        if m:
            version, conf, where = m.group(1), 0.9, f'generator tag "{gen[:40]}"'
            break
    if not version:
        m = re.search(r"(?i)wp-emoji-release\.min\.js\?ver=(\d+(?:\.\d+){0,2})", _without_comments(html))
        if m:
            version, conf, where = m.group(1), 0.8, "the WordPress emoji script's version"
    if not version or _version_key(version)[0] >= 5:
        return None
    return Fact("OLD_WORDPRESS",
                f"the site runs WordPress {version}, older than the 5.0 release of December 2018",
                conf, True, where)


def _table_fact(html: str) -> Fact | None:
    raw = _SCRIPT_STYLE.sub(" ", _without_comments(html))
    tables = len(re.findall(r"(?i)<table\b[^>]*\b(?:width|cellpadding|cellspacing|bgcolor|border)\s*=", raw))
    fonts = len(re.findall(r"(?i)<font\b", raw))
    centers = len(re.findall(r"(?i)<center\b", raw))
    old_tags = fonts + centers
    if tables >= 3 and old_tags >= 2:
        kinds = " and ".join(k for k, n in (("font", fonts), ("center", centers)) if n)
        return Fact("TABLE_LAYOUT",
                    f"the home page is laid out with {tables} HTML tables and old {kinds} tags "
                    "instead of modern CSS",
                    0.8, True, f"{tables} layout tables, {fonts} <font>, {centers} <center>")
    if tables >= 5 or old_tags >= 5:
        return Fact("TABLE_LAYOUT", "the home page leans on HTML tables or old font tags for layout",
                    0.5, False, f"{tables} layout tables, {fonts} <font>, {centers} <center>")
    return None


def _flash_fact(html: str) -> Fact | None:
    raw = _without_comments(html)
    if re.search(r"""(?is)<(?:embed|param|object)\b[^>]*(?:\.swf\b|application/x-shockwave-flash)""", raw):
        return Fact("FLASH", "the home page still embeds a Flash file, which browsers stopped playing "
                    "at the start of 2021", 0.9, True, "a .swf embed")
    if re.search(r"(?i)\bswfobject\b", raw):
        return Fact("FLASH", "the home page loads a Flash embedding script",
                    0.5, False, "swfobject script, no .swf embed seen")
    return None


def _doctype_fact(html: str) -> Fact | None:
    m = re.search(r'(?i)<!DOCTYPE\s+html\s+PUBLIC\s+"-//W3C//DTD\s+(X?HTML)\s+(\d\.\d+)', html or "")
    if not m:
        return None
    kind = "XHTML" if m.group(1).upper() == "XHTML" else "HTML"
    return Fact("OLD_DOCTYPE", f"the page is written to the {kind} {m.group(2)} standard (its doctype), "
                "which came before HTML5", 0.7, True, m.group(0)[:70])


# A COVID-19 notice is a notice (deliveries delayed, open as normal), not a
# sentence of company history ("during COVID we moved online").
_COVID = re.compile(r"(?i)\b(?:covid(?:-?19)?|coronavirus)\b")
_COVID_NOTICE = re.compile(
    r"(?i)\b(?:lockdown|restrictions?|guidance|delays?|delayed|social distancing|government advice|"
    r"remain open|still open|open as normal|safety measures|precautions|until further notice|"
    r"temporarily|currently|deliveries|dispatch(?:ed|ing)?)\b")
# A browser or screen notice: "best viewed in Internet Explorer at 1024x768".
_BROWSER_ADVICE = re.compile(
    r"(?i)\b(?:best viewed|optimi[sz]ed for|works best|designed for|recommended|please use|requires|upgrade)\b")
_OLD_BROWSER = re.compile(
    r"(?i)\binternet explorer\b|\bnetscape\b|\bIE ?[4-9]\b|\b\d{3,4}\s?x\s?\d{3,4}\b|\bscreen resolution\b")


def _notice_fact(html: str) -> Fact | None:
    for line in re.split(r"(?<=[.!?])\s+|\n", visible_text(html)):
        line = re.sub(r"\s+", " ", line).strip()
        if not line or len(line) > 300:
            continue
        covid = _COVID.search(line) and _COVID_NOTICE.search(line)
        browser = _BROWSER_ADVICE.search(line) and _OLD_BROWSER.search(line)
        if covid or browser:
            quote = line[:100].replace('"', "'")
            return Fact("OUTDATED_NOTICE", f'the home page still shows an outdated notice: "{quote}"',
                        0.75, True, quote)
    return None


def age_facts(html: str, fetch: dict | None = None, platform: str | None = None,
              today: date | None = None) -> list[Fact]:
    """What the home page shows about its age.

    ``fetch`` describes how the page was fetched (every key optional):
    ``final_url`` (where it ended up), ``https_error`` ("connect", "timeout",
    "dns", "status", "other" or "" for the https attempt on the host that
    answered), ``https_answered`` (any https attempt got a response),
    ``mobile_checked`` / ``mobile_html`` / ``mobile_final_url`` (a fetch
    with a phone's user agent). ``platform`` is detect_platform's id, found
    here when not given.
    """
    fetch = fetch or {}
    today = today or date.today()
    if platform is None:
        platform = detect_platform(html, fetch.get("headers"), fetch.get("final_url", ""))[0]
    facts = [
        _viewport_fact(html, fetch),
        _http_only_fact(fetch),
        _copyright_fact(html, today),
        _jquery_fact(html),
        _wordpress_fact(html),
        _table_fact(html),
        _flash_fact(html),
        _doctype_fact(html),
        _notice_fact(html),
    ]
    if platform == "magento1":
        facts.append(Fact("MAGENTO1", "the shop runs on Magento 1, which Adobe stopped supporting in June 2020",
                          0.85, True, "Magento 1 paths"))
    return [f for f in facts if f is not None]


# ── Legacy score ──
#
# 0-100: how old the site looks. Points for the platform itself, plus each
# fact's weight times its confidence (a likely-but-unproven fact counts for
# less), capped at 100. 40 or more reads as an old site (segments.py).
PLATFORM_WEIGHTS = {
    "oscommerce": 35, "zencart": 30, "magento1": 30, "shopfactory": 30,
    "cubecart": 25, "xcart": 25, "sellerdeck": 25, "virtuemart": 25,
    "custom_php": 20, "opencart": 15, "joomla": 15, "prestashop": 10,
    "wordpress": 5, "woocommerce": 5, "magento2": 5, "unknown": 5,
    "shopify": 0, "bigcommerce": 0, "ecwid": 0, "wix": 0, "squarespace": 0, "webflow": 0,
}
FACT_WEIGHTS = {
    "NO_MOBILE_VIEWPORT": 25, "FLASH": 20, "HTTP_ONLY": 15, "OLD_COPYRIGHT": 15,
    "TABLE_LAYOUT": 15, "OLD_WORDPRESS": 15, "OLD_JQUERY": 10, "OUTDATED_NOTICE": 10,
    "MAGENTO1": 5, "OLD_DOCTYPE": 5,
}


def legacy_score(platform: str, facts: list[Fact]) -> int:
    """0-100, from the platform's weight and each fact's weight x confidence."""
    score = PLATFORM_WEIGHTS.get(platform or "unknown", 5)
    seen = set()
    for fact in facts:
        if fact.code in seen:
            continue
        seen.add(fact.code)
        score += FACT_WEIGHTS.get(fact.code, 0) * max(0.0, min(1.0, fact.confidence))
    return int(round(min(100.0, score)))
