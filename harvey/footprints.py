"""Footprint searches: the text old shop software prints on every page.

"Powered by osCommerce" in a footer is a better way to find an old online
shop than any directory. This is a bank of such footprints per segment,
combined with the niches (``icp.legacy_niches``) and countries
(``icp.geography``) you sell to, plus any searches of your own
(``icp.footprint_queries``, run as written). Scout walks the bank a few
queries a cycle (the position is kept in settings), and the discover preset
``legacy_cart`` uses the same footprints.

A search hit is only a lead. Companies found this way are recorded with a
plain industry label (never the query), and nobody there is written to until
the profile has read the site and recorded its platform (SITE_PLATFORM).
"""

from dataclasses import dataclass

# Visible-text footprints, in search syntax, by segment.
FOOTPRINTS: dict[str, tuple[str, ...]] = {
    "custom": (
        '"Powered by osCommerce"',
        '"Powered by Zen Cart"',
        '"Powered by CubeCart"',
        '"Created with ShopFactory"',
        '"SellerDeck"',
        '"X-Cart"',
        '"Powered by OpenCart"',
        '"Copyright 2012" "online shop"',
        '"add to basket" "Ltd" "Copyright 2015"',
    ),
    "wordpress": (
        '"WooCommerce" "Copyright 2016"',
        '"Proudly powered by WordPress" "add to basket"',
    ),
}
# The industry recorded for a company a footprint found.
SEGMENT_INDUSTRY = {"custom": "Legacy e-commerce", "wordpress": "WordPress store"}
FOOTPRINT_LABELS = frozenset(SEGMENT_INDUSTRY.values())
# Company.source for a company Scout found by a footprint search.
FOOTPRINT_SOURCE = "search:footprint"
# Settings key holding the position in the bank.
CURSOR_KEY = "footprints:cursor"

# The software makers' own sites, forums, add-on stores and demos match
# their own footprints; they are never a prospect.
VENDOR_DOMAINS = (
    "oscommerce.com", "zen-cart.com", "cubecart.com", "shopfactory.com", "shopfactory.co.uk",
    "sellerdeck.com", "sellerdeck.co.uk", "actinic.co.uk", "x-cart.com", "opencart.com",
    "prestashop.com", "virtuemart.net", "magento.com", "woocommerce.com", "wordpress.org",
    "wordpress.com", "joomla.org", "ecwid.com", "bigcommerce.com", "templatemonster.com",
    "themeforest.net", "envato.com", "builtwith.com", "wappalyzer.com", "w3techs.com",
    "storeleads.app", "github.com", "sourceforge.net", "stackoverflow.com", "reddit.com",
)


@dataclass(frozen=True)
class FootprintQuery:
    query: str
    segment: str      # custom | wordpress, the segment the footprint points at
    label: str        # the industry a company it finds is recorded under


def is_vendor(domain: str) -> bool:
    domain = (domain or "").lower().removeprefix("www.")
    return any(domain == v or domain.endswith("." + v) for v in VENDOR_DOMAINS)


def _label_for(query: str) -> tuple[str, str]:
    low = query.lower()
    segment = "wordpress" if ("wordpress" in low or "woocommerce" in low) else "custom"
    return segment, SEGMENT_INDUSTRY[segment]


def _quoted(term: str) -> str:
    term = " ".join(str(term or "").split()).replace('"', "")
    return f'"{term}"' if term else ""


def query_bank(footprint_queries=None, legacy_niches=None, geography=None) -> list[FootprintQuery]:
    """Every search in the rotation, in a fixed order.

    The operator's own ``footprint_queries`` first, as written. Then, when
    ``legacy_niches`` is set, each built-in footprint with each niche and
    country, ordered so consecutive searches use different footprints (a
    run of the same footprint mostly finds the same shops). Empty when
    neither is configured: footprint searching is opt-in.
    """
    bank: list[FootprintQuery] = []
    seen: set[str] = set()

    def add(query: str, segment: str, label: str):
        query = " ".join(query.split())
        if query and query.lower() not in seen:
            seen.add(query.lower())
            bank.append(FootprintQuery(query, segment, label))

    for raw in footprint_queries or []:
        segment, label = _label_for(str(raw))
        add(str(raw), segment, label)
    niches = [_quoted(n) for n in legacy_niches or [] if _quoted(n)]
    if niches:
        prints = [(seg, fp) for seg, fps in FOOTPRINTS.items() for fp in fps]
        countries = [str(g).strip() for g in geography or [] if str(g).strip()] or [""]
        total = len(prints) * len(niches) * len(countries)
        for i in range(total):
            seg, fp = prints[i % len(prints)]
            niche = niches[(i // len(prints)) % len(niches)]
            country = countries[(i // (len(prints) * len(niches))) % len(countries)]
            add(f"{fp} {niche} {_quoted(country)}", seg, SEGMENT_INDUSTRY[seg])
    return bank


def preset_footprints() -> tuple[str, ...]:
    """The footprints the discover preset ``legacy_cart`` searches for."""
    return FOOTPRINTS["custom"]


async def next_queries(state, bank: list[FootprintQuery], count: int) -> list[FootprintQuery]:
    """The next ``count`` searches from the bank, wrapping around."""
    if not bank or count <= 0:
        return []
    try:
        cursor = int(await state.get_setting(CURSOR_KEY) or 0)
    except (TypeError, ValueError):
        cursor = 0
    start = cursor % len(bank)
    return [bank[(start + i) % len(bank)] for i in range(min(count, len(bank)))]


async def advance(state, bank: list[FootprintQuery], used: int) -> None:
    """Move past the ``used`` searches, so the next cycle starts after them."""
    if not bank or used <= 0:
        return
    try:
        cursor = int(await state.get_setting(CURSOR_KEY) or 0)
    except (TypeError, ValueError):
        cursor = 0
    await state.set_setting(CURSOR_KEY, str((cursor % len(bank) + used) % len(bank)))
