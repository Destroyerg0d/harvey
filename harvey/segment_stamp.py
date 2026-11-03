"""What an outbox row is stamped with, so outcomes can be read per kind of email.

segment: which kind of business the email went to (shopify, wordpress, custom,
other or unknown), worked out at send time from what Harvey knows about the
company then. variant: "a" or "b" for a first email, fixed per prospect, so a
later subject test has two stable halves. angle: the kind of fact the first
email's opening line used, when the Writer says so.

Read-only and never raises: a stamp that can't be worked out is "unknown" or
"", and the email goes out regardless.
"""

import hashlib
import re

SEGMENTS = ("shopify", "wordpress", "custom", "other", "unknown")

# SITE_PLATFORM values (normalised: lower case, no spaces, dashes or
# underscores) that are an older PHP cart or a hand-built site.
_LEGACY = frozenset({
    "magento1", "oscommerce", "zencart", "opencart", "cubecart", "xcart",
    "shopfactory", "sellerdeck", "actinic", "prestashop", "virtuemart", "joomla",
    "customphp", "custom", "legacy", "legacycart",
})
_WORDPRESS = frozenset({"wordpress", "woocommerce"})
# Platform values that say nothing: read the next source instead.
_UNKNOWN = frozenset({"", "unknown", "none", "notfound"})

_ANGLE_RE = re.compile(r"[^a-z0-9]+")
ANGLE_MAX = 40


def _platform_segment(value: str) -> str:
    key = re.sub(r"[\s_\-./]+", "", (value or "").strip().lower())
    if key in _UNKNOWN:
        return ""
    if key == "shopify":
        return "shopify"
    if key in _WORDPRESS:
        return "wordpress"
    if key in _LEGACY:
        return "custom"
    return "other"


def _stack_segment(tech_stack) -> str:
    tools = " ".join(str(t) for t in (tech_stack or [])).lower()
    if "shopify" in tools:
        return "shopify"
    if "woocommerce" in tools or "wordpress" in tools:
        return "wordpress"
    return ""


async def segment_for_company(state, company_id: str, prospect=None) -> str:
    """The company's segment, from, in order: its SITE_PLATFORM observation
    (when that signal exists and was recorded), the tools detected on its
    site, then the company's or the prospect's industry naming Shopify; else
    "unknown"."""
    # One rule for the Writer's campaigns and these stamps: harvey.segments.
    company, platform, score = None, "", None
    if company_id:
        try:
            rows = await state.get_observations(company_id=company_id, signal_code="SITE_PLATFORM",
                                                latest_only=True, limit=1)
            platform = str(rows[0].get("value_text") or "") if rows else ""
            rows = await state.get_observations(company_id=company_id, signal_code="LEGACY_SITE_SCORE",
                                                latest_only=True, limit=1)
            score = rows[0].get("value_num") if rows else None
        except Exception:
            pass
        try:
            company = await state.get_company(company_id)
        except Exception:
            company = None
    stack = getattr(company, "tech_stack", None) if company is not None else None
    industry = " ".join(i for i in (getattr(company, "industry", "") if company is not None else "",
                                    getattr(prospect, "industry", "") if prospect is not None else "") if i)
    return segment_from_facts(platform, score, stack, industry)


def segment_from_facts(platform: str, score, stack, industry: str) -> str:
    """segment_for_company's rule on facts already read: the SITE_PLATFORM
    text, the LEGACY_SITE_SCORE number (or None), the tech stack and the
    industry label(s)."""
    from harvey.segments import segment_of

    platform = str(platform or "")
    # "Zen Cart" / "magento 1" -> the profile's ids ("zencart", "magento1");
    # underscores stay ("custom_php").
    platform_id = re.sub(r"[\s\-./]+", "", platform.strip().lower())
    segment = segment_of(platform_id or None, stack, industry or "", score)
    if segment == "other" and not _platform_segment(platform) and not _stack_segment(stack):
        return "unknown"   # nothing read the site yet: not a real "other"
    return segment


def variant_for(prospect_id: str) -> str:
    """"a" or "b" for this prospect, the same every time (a stable hash, not
    Python's per-process one)."""
    digest = hashlib.sha256((prospect_id or "").encode("utf-8")).digest()
    return "a" if digest[0] % 2 == 0 else "b"


def clean_angle(value) -> str:
    """The Writer's angle as a short label ("tools", "hiring_signal"), or ""."""
    if not isinstance(value, str):
        return ""
    return _ANGLE_RE.sub("_", value.strip().lower()).strip("_")[:ANGLE_MAX]
