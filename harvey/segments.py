"""The website segments Harvey writes to, and when an old site may be pitched.

Four segments, from what a company's site runs on (``SITE_PLATFORM``, see
harvey/collectors/site_age.py):

- ``shopify``: Shopify stores.
- ``wordpress``: WordPress and WooCommerce sites.
- ``custom``: legacy shop software (osCommerce, Zen Cart, Magento 1,
  ShopFactory, SellerDeck...), hand-built PHP sites, and any other site whose
  ``LEGACY_SITE_SCORE`` is 40 or more (an old builder or unknown site).
- ``other``: modern builders and frameworks, and anything not classified.

``custom`` and ``wordpress`` are the old-site segments: their email may say
what is old about the site only when ``eligible_for_old_site_pitch`` is true,
and otherwise they are written as generic. ``custom`` is written only to
verified addresses (state.count_writable_prospects and the Writer agree).
"""

import json
import re

SEGMENTS = ("shopify", "wordpress", "custom", "other")
SEGMENT_LABELS = {"shopify": "Shopify", "wordpress": "WordPress", "custom": "Custom", "other": "Other"}
OLD_SITE_SEGMENTS = frozenset({"custom", "wordpress"})

# Always custom: legacy carts and hand-built PHP.
CUSTOM_PLATFORMS = frozenset({
    "magento1", "oscommerce", "zencart", "opencart", "prestashop", "virtuemart",
    "cubecart", "xcart", "shopfactory", "sellerdeck", "custom_php",
})
# Custom only when the site scores as old (an old builder site, or one
# nothing recognised); otherwise other.
CUSTOM_WHEN_OLD = frozenset({
    "unknown", "joomla", "magento2", "wix", "squarespace", "webflow", "ecwid", "bigcommerce",
})
WORDPRESS_PLATFORMS = frozenset({"wordpress", "woocommerce"})
OLD_SITE_SCORE = 40

# A platform fact is only named in an email at this confidence or above.
PLATFORM_CLAIM_FLOOR = 0.7


def segment_of(platform_id: str | None, tech_stack=None, industry: str = "",
               legacy_score: float | None = None) -> str:
    """shopify | wordpress | custom | other.

    ``platform_id`` is the company's SITE_PLATFORM; without one, the tech
    stack Scout saw (``TECH_STACK`` names) and then the industry label are
    the fallback. A fallback never says ``custom``: only a profile that read
    the site does.
    """
    platform = (platform_id or "").strip().lower()
    if platform == "shopify":
        return "shopify"
    if platform in WORDPRESS_PLATFORMS:
        return "wordpress"
    if platform in CUSTOM_PLATFORMS:
        return "custom"
    old = legacy_score is not None and float(legacy_score) >= OLD_SITE_SCORE
    if platform in CUSTOM_WHEN_OLD and old:
        return "custom"
    if platform and platform != "unknown":
        return "other"
    tools = {str(t).strip().lower() for t in (tech_stack or [])}
    if "shopify" in tools:
        return "shopify"
    if tools & {"wordpress", "woocommerce"}:
        return "wordpress"
    label = (industry or "").lower()
    if re.search(r"\bshopify\b", label):
        return "shopify"
    if re.search(r"\b(?:wordpress|woocommerce)\b", label):
        return "wordpress"
    return "other"


def eligible_for_old_site_pitch(company_facts: dict) -> bool:
    """May the email say what is old about this site?

    Only with something to say (a claimable age fact, or a legacy score of
    40+) and signs of life: the home page answered 200 at the last profile
    (``site_ok``), the domain has an MX record (``has_mx``), and the company
    isn't marked dissolved.
    """
    facts = company_facts or {}
    claimable = [f for f in facts.get("age_facts") or [] if isinstance(f, dict) and f.get("sentence")]
    score = facts.get("legacy_score")
    something = bool(claimable) or (score is not None and float(score) >= OLD_SITE_SCORE)
    alive = bool(facts.get("site_ok")) and bool(facts.get("has_mx")) and not facts.get("dissolved")
    return something and alive


def _dissolved(company) -> bool:
    """Marked dissolved by the operator (a note on the company); Harvey reads no registry."""
    return bool(re.search(r"\bdissolved\b", str(getattr(company, "notes", "") or ""), re.I))


def parse_age_facts(text: str) -> list[dict]:
    """SITE_AGE_FACTS' JSON, as [{code, sentence}], ignoring anything malformed."""
    try:
        rows = json.loads(text or "[]")
    except (TypeError, ValueError):
        return []
    return [{"code": str(r.get("code", "")), "sentence": str(r["sentence"]).strip()}
            for r in rows if isinstance(r, dict) and str(r.get("sentence") or "").strip()]


async def company_site_facts(state, company_id: str, company=None, check_mx: bool = True) -> dict:
    """What the latest profile says about a company's site, plus its segment.

    {"platform", "platform_conf", "legacy_score", "age_facts" (claimable
    [{code, sentence}]), "site_ok", "has_mx", "dissolved", "segment",
    "classified" (a SITE_PLATFORM row exists), "source", "industry"}. The MX
    lookup (free, cached) only runs when there is an old-site pitch to make.
    """
    out = {"platform": "", "platform_conf": 0.0, "legacy_score": None, "age_facts": [],
           "site_ok": False, "has_mx": False, "mx_checked": False, "dissolved": False,
           "segment": "other", "classified": False, "source": "", "industry": "", "domain": ""}
    if not company_id:
        return out
    if company is None:
        company = await state.get_company(company_id)
    rows = await state.get_observations(company_id=company_id, latest_only=True)
    latest = {r.get("signal_code"): r for r in rows}
    platform_row = latest.get("SITE_PLATFORM")
    if platform_row:
        out["platform"] = (platform_row.get("value_text") or "").strip().lower()
        out["platform_conf"] = float(platform_row.get("confidence") or 0.0)
        out["classified"] = True
        out["site_ok"] = True       # written only when the home page answered 200
    score_row = latest.get("LEGACY_SITE_SCORE")
    if score_row and score_row.get("value_num") is not None:
        out["legacy_score"] = float(score_row["value_num"])
    facts_row = latest.get("SITE_AGE_FACTS")
    if facts_row:
        out["age_facts"] = parse_age_facts(facts_row.get("value_text") or "")
    tech = list(getattr(company, "tech_stack", None) or [])
    out["source"] = str(getattr(company, "source", "") or "")
    out["industry"] = str(getattr(company, "industry", "") or "")
    out["dissolved"] = _dissolved(company)
    out["segment"] = segment_of(out["platform"] or None, tech, out["industry"], out["legacy_score"])
    out["domain"] = str(getattr(company, "domain", "") or "")
    return await add_mx(out) if check_mx else out


async def add_mx(facts: dict) -> dict:
    """``facts`` with ``has_mx`` filled in, asked only when there is an
    old-site pitch to make (a free DNS lookup, cached per domain)."""
    if facts.get("mx_checked"):
        return facts
    worth_asking = facts.get("age_facts") or (facts.get("legacy_score") or 0) >= OLD_SITE_SCORE
    if worth_asking and facts.get("site_ok") and facts.get("domain"):
        from harvey.integrations.email_finder import get_mx_host
        facts["has_mx"] = bool(await get_mx_host(facts["domain"]))
    facts["mx_checked"] = True
    return facts
