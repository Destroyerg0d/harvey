"""Who Harvey reaches out to, cold: the countries and the people you chose.

Owner, 2026-11-19: "filter prospects which are in 1st world country only ... do
not message to indian prospects or send connects to them also make sure you are
sending to decision makers of that company like founder, CEO, CTO or similar
roles". Their choices: Western countries only, and people in India who reached
out first are still answered.

``targeting.countries`` (two-letter codes) and ``targeting.decision_makers_only``
in the config. Both empty/false: no rule, as before. The rules cover cold
outreach only: cold email (every step of a sequence), LinkedIn connects, and
LinkedIn's first message and follow-up to a new connection. Replies, website
chat follow-ups and anyone who wrote first are never filtered.

- A country comes from a location ("Austin, Texas, United States", a LinkedIn
  metro area such as "San Francisco Bay Area") or an address's country domain
  (".in", ".co.uk"). Unknown is unknown: cold email still goes (most .com
  addresses say nothing), a LinkedIn connect doesn't (its profile always shows
  a location, so unknown means unread).
- A decision maker is a founder, owner, chief officer (CEO, CTO, COO, CMO,
  CFO, "Chief ... Officer"), president, managing director or partner, or
  principal; never an assistant, intern, someone aspiring to it or a former one.
"""

from __future__ import annotations

import re

# The owner's "1st world" (2026-11-19, "Western only"), for harvey.local.yaml.
WESTERN = ("US", "CA", "GB", "IE", "AU", "NZ", "DE", "FR", "NL", "BE", "LU", "AT", "CH", "LI",
           "SE", "NO", "DK", "FI", "IS", "IT", "ES", "PT", "MT", "MC")

# LinkedIn writes big metros without a country ("New York City Metropolitan
# Area", "San Francisco Bay Area", "Greater Sydney Area"): their cities.
_METRO_CITIES = {
    "US": ("new york", "san francisco", "los angeles", "chicago", "boston", "seattle", "dallas",
           "houston", "atlanta", "miami", "denver", "philadelphia", "washington dc", "phoenix",
           "austin", "san diego", "detroit", "minneapolis", "nashville", "portland", "salt lake city",
           "las vegas", "charlotte", "orlando", "tampa", "raleigh", "baltimore", "pittsburgh",
           "cincinnati", "cleveland", "columbus", "indianapolis", "kansas city", "st. louis",
           "san antonio", "sacramento", "san jose", "new orleans", "hartford", "richmond"),
    "GB": ("london", "manchester", "birmingham", "leeds", "glasgow", "edinburgh", "bristol",
           "liverpool", "england", "scotland", "wales", "northern ireland"),
    # LinkedIn's own areas ("Greater Madrid Metropolitan Area") name a city, not a
    # country (live, 2026-11-20).
    "ES": ("madrid", "barcelona", "valencia", "seville", "malaga"),
    "FR": ("paris", "lyon", "marseille", "toulouse", "bordeaux", "lille", "nice"),
    "DE": ("berlin", "munich", "hamburg", "frankfurt", "cologne", "stuttgart", "dusseldorf",
           "düsseldorf"),
    "NL": ("amsterdam", "rotterdam", "the hague", "utrecht", "eindhoven"),
    "BE": ("brussels", "antwerp", "ghent"),
    "AT": ("vienna",),
    "CH": ("zurich", "zürich", "geneva", "basel", "lausanne"),
    "SE": ("stockholm", "gothenburg", "malmö", "malmo"),
    "DK": ("copenhagen", "aarhus"),
    "NO": ("oslo", "bergen"),
    "FI": ("helsinki",),
    "IT": ("milan", "rome", "turin", "florence", "naples", "bologna"),
    "PT": ("lisbon", "porto"),
    "LU": ("luxembourg",),
    "AU": ("sydney", "melbourne", "brisbane", "perth", "adelaide"),
    "CA": ("toronto", "vancouver", "montreal", "calgary", "ottawa"),
    "IE": ("dublin",),
    "NZ": ("auckland", "wellington"),
    "IN": ("mumbai", "delhi", "new delhi", "bengaluru", "bangalore", "hyderabad", "pune", "chennai",
           "kolkata", "ahmedabad", "noida", "gurgaon", "gurugram", "jaipur", "chandigarh",
           "lucknow", "indore", "kochi", "surat"),
}
_METRO_RE = {code: re.compile(r"\b(" + "|".join(re.escape(c) for c in cities) + r")\b", re.IGNORECASE)
             for code, cities in _METRO_CITIES.items()}

_DECISION_RE = re.compile(
    r"\b(co[- ]?founder|founder|founding partner|owner|co[- ]?owner|proprietor|"
    r"ceo|cto|coo|cmo|cfo|cpo|cro|chief\s+\w+(?:\s+\w+)?\s+officer|president|"
    r"managing director|managing partner|general partner|"
    # "Principal" alone or "Principal at X" (an owner); never "Principal Designer".
    r"principal(?=\s*(?:$|[,|&@/]|at\b|of\b))|"
    r"(?:^|\W)md(?:\W|$)|director general|chairman|chairwoman|chairperson)\b",
    re.IGNORECASE)
# Words that make a title someone else's, or not now.
_NOT_THEM_RE = re.compile(
    r"\b(assistant|intern|internship|aspiring|former|ex[- ]|executive assistant|ea to|"
    r"student|seeking|looking for|vice president|vp\b|deputy)\b", re.IGNORECASE)


def location_country(location: str) -> str:
    """The country a free-text location names, or "": "Austin, Texas, United
    States" -> "US", "San Francisco Bay Area" -> "US", "Mumbai, India" -> "IN"."""
    text = (location or "").strip()
    if not text:
        return ""
    from harvey.send_window import country_for
    try:
        code, sure = country_for(text)
    except Exception:
        code, sure = "", False
    if code and sure:
        return code
    for code, pattern in _METRO_RE.items():
        if pattern.search(text):
            return code
    return ""


def email_country(email: str) -> str:
    """The country an address's own country domain names ("shop.co.in" -> "IN"),
    or "" for .com and the like."""
    from harvey.integrations.precleared import _domain_country
    return _domain_country(email)


def is_decision_maker(title: str) -> bool:
    """A founder, owner, chief officer, president, managing director or the
    like, by their title or LinkedIn headline."""
    text = " ".join((title or "").split())
    if not text or not _DECISION_RE.search(text):
        return False
    # "Assistant to the CEO", "Former founder", "VP, Office of the CEO": not them.
    first = _DECISION_RE.search(text)
    return not _NOT_THEM_RE.search(text[:first.start()] if first else text)


def settings(config) -> tuple[frozenset, bool]:
    """(the countries cold outreach may go to, decision makers only)."""
    t = getattr(config, "targeting", None)
    countries = frozenset(c.strip().upper() for c in (getattr(t, "countries", None) or []) if c.strip())
    return countries, bool(getattr(t, "decision_makers_only", False))


def active(config) -> bool:
    countries, decision = settings(config)
    return bool(countries) or decision


def cold_reason(config, *, country: str = "", title: str = "", country_required: bool = False,
                title_required: bool = False) -> str:
    """Why this person isn't someone to reach out to cold, or "" when they are.
    ``country_required`` / ``title_required``: an unknown one is a reason too
    (a LinkedIn connect, where the profile shows both)."""
    countries, decision = settings(config)
    code = (country or "").strip().upper()
    if countries:
        if code and code not in countries:
            return f"outside your target countries ({code})"
        if not code and country_required:
            return "their country couldn't be read"
    if decision:
        if title and not is_decision_maker(title):
            return f"not a decision maker ({' '.join(title.split())[:60]})"
        if not title and title_required:
            return "their role couldn't be read"
    return ""
