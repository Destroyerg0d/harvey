"""New people to connect with on LinkedIn, found by web search.

Owner, 2026-11-19: "send daily 30 connects to connections who are right
prospects for us ... without missing any day". Every imported lead had been
invited, and the only other source (prospects whose own website links a
LinkedIn profile) had 49 left, so Harvey had nobody new to invite. The owner
chose web search with the free TinyFish key plus shop websites; LinkedIn's own
search is never used.

Each search is ``site:linkedin.com/in "<role>" "<niche>" "<country>"``: a
decision maker's title, a phrase from icp.industries, a target country
(targeting.countries, the US weighted). A rotating cursor in settings moves
through them. A result becomes a lead (source ``web_search``, stage
``found``) with the name, headline and company from its title, when:

- it is a profile (linkedin.com/in/...) not already a lead;
- its headline is a decision maker's (harvey/targeting.py);
- its snippet's location, when it shows one, is a target country.

An unknown location is checked on the profile itself before the invite
(LinkedInAgent.send_invite), so nobody outside the targets gets a connect.
"""

from __future__ import annotations

import logging
import re
from urllib.parse import quote_plus

import httpx

logger = logging.getLogger("harvey.linkedin.finder")

CURSOR_KEY = "linkedin:finder:cursor"
SOURCE = "web_search"
API = "https://api.search.tinyfish.ai"
TIMEOUT = httpx.Timeout(connect=10.0, read=30.0, write=10.0, pool=10.0)
MAX_SEARCHES_PER_RUN = 8
ROLES = ("founder", "co-founder", "CEO", "owner")
COUNTRY_NAMES = {
    "US": "United States", "GB": "United Kingdom", "CA": "Canada", "AU": "Australia",
    "IE": "Ireland", "NZ": "New Zealand", "DE": "Germany", "FR": "France", "NL": "Netherlands",
    "BE": "Belgium", "AT": "Austria", "CH": "Switzerland", "SE": "Sweden", "NO": "Norway",
    "DK": "Denmark", "FI": "Finland", "IT": "Italy", "ES": "Spain", "PT": "Portugal",
}
# Searches a country gets per round: where most clients are.
COUNTRY_WEIGHT = {"US": 4, "GB": 2, "CA": 2, "AU": 1}
_PROFILE_RE = re.compile(r"https?://(?:[a-z]{2,3}\.)?linkedin\.com/in/[^/?#\s]+", re.IGNORECASE)


def search_terms(config) -> list[str]:
    """The niches to search for, from icp.industries ("DTC Ecommerce (Shopify /
    Shopify Plus brands)" -> "DTC Ecommerce"), each once."""
    from harvey.agents.scout import search_phrase
    out = []
    for label in getattr(config.icp, "industries", None) or []:
        phrase = search_phrase(label)
        if phrase and phrase.lower() not in (p.lower() for p in out):
            out.append(phrase)
    return out or ["ecommerce"]


def queries(config) -> list[str]:
    """Every search, in the order the cursor walks them: each niche, for each
    role, across the target countries round-robin by weight."""
    from harvey import targeting
    countries, _ = targeting.settings(config)
    places = [c for c in (countries or ("US",)) if c in COUNTRY_NAMES]
    ordered: list[str] = []
    for code in places:
        ordered += [code] * COUNTRY_WEIGHT.get(code, 1)
    out = []
    for niche in search_terms(config):
        for role in ROLES:
            for code in ordered:
                q = f'site:linkedin.com/in "{role}" "{niche}" "{COUNTRY_NAMES[code]}"'
                if q not in out:
                    out.append(q)
    return out


def parse_result(item: dict) -> dict | None:
    """A search result as a profile: {"url", "slug", "name", "headline",
    "company", "location"}; None when it isn't a profile."""
    from harvey import targeting
    from harvey.linkedin.pages import profile_url, slug_from_url
    url = str(item.get("url") or "")
    if not _PROFILE_RE.match(url):
        return None
    slug = slug_from_url(url)
    if not slug:
        return None
    title = " ".join(str(item.get("title") or "").split())
    title = re.sub(r"\s*\|\s*LinkedIn.*$", "", title, flags=re.IGNORECASE)
    parts = [p.strip() for p in re.split(r"\s+[-–—]\s+", title) if p.strip()]
    name = parts[0] if parts else ""
    headline = " - ".join(parts[1:]) if len(parts) > 1 else ""
    company = parts[2] if len(parts) > 2 else ""
    snippet = " ".join(str(item.get("snippet") or item.get("description") or "").split())
    location = ""
    for piece in re.split(r"[·|]|Location:\s*", snippet):
        piece = piece.strip(" .")
        if 3 <= len(piece) <= 80 and targeting.location_country(piece):
            location = piece
            break
    return {"url": profile_url(slug), "slug": slug, "name": name, "headline": headline,
            "company": company, "location": location}


async def search(query: str, key: str, client: httpx.AsyncClient | None = None) -> list[dict]:
    """One TinyFish search: its results, or [] when it fails."""
    owned = client is None
    client = client or httpx.AsyncClient(timeout=TIMEOUT)
    try:
        r = await client.get(f"{API}?query={quote_plus(query)}",
                             headers={"X-API-Key": key, "Accept": "application/json"})
        if r.status_code != 200:
            logger.info(f"LinkedIn finder: search answered {r.status_code}")
            return []
        data = r.json()
        return [x for x in (data.get("results") or []) if isinstance(x, dict)] if isinstance(data, dict) else []
    except Exception as e:
        logger.info(f"LinkedIn finder: search failed: {e}")
        return []
    finally:
        if owned:
            await client.aclose()


async def find_people(state, config, key: str, wanted: int, client: httpx.AsyncClient | None = None,
                      max_searches: int = MAX_SEARCHES_PER_RUN) -> dict:
    """Search until ``wanted`` new leads are added or ``max_searches`` ran.
    Returns {"searches", "found", "added", "skipped"}."""
    from harvey import targeting
    report = {"searches": 0, "found": 0, "added": 0, "skipped": 0}
    if not key or wanted <= 0:
        return report
    plan = queries(config)
    if not plan:
        return report
    try:
        cursor = int(await state.get_setting(CURSOR_KEY) or 0)
    except ValueError:
        cursor = 0
    while report["searches"] < max_searches and report["added"] < wanted:
        query = plan[cursor % len(plan)]
        cursor += 1
        report["searches"] += 1
        for item in await search(query, key, client):
            person = parse_result(item)
            if person is None:
                continue
            report["found"] += 1
            if await state.li_get_lead_by_slug(person["slug"]):
                continue
            why = targeting.cold_reason(
                config, country=targeting.location_country(person["location"]),
                title=person["headline"], title_required=True)
            if why:
                report["skipped"] += 1
                continue
            first = person["name"].split(" ")[0] if person["name"] else ""
            await state.li_upsert_lead(
                person["slug"], stage="found", name=person["name"][:120], first_name=first[:60],
                headline=person["headline"][:220], company=person["company"][:120],
                profile_url=person["url"], source=SOURCE)
            report["added"] += 1
    await state.set_setting(CURSOR_KEY, str(cursor % max(1, len(plan))))
    return report
