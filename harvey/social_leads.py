"""New companies from social profiles, read with Scout (owner, 2026-11-27).

Scout (github.com/kiryano/Scout, MIT) reads a public profile when it is given
a handle; it can't search. So each prospecting cycle, when
``social_leads.enabled``:

1. A few web searches from a rotating list (your terms x places x platforms,
   ``site:instagram.com "skincare brand" "London"``) find profile handles in the
   results, through the Scout agent's own ``_web_search``.
2. Scout reads those profiles, in its own Python and folder
   (``harvey/collectors/scout_bridge.py``), at its own pace, with its proxy
   settings cleared and its SMTP step unused (port 25 is usually blocked, and
   Harvey's verifiers do that job).
3. A brand with its own website (the profile's link, a link on its link-in-bio
   page, or the domain of a business email in the bio) becomes a company.
   Instagram shows its link only to signed-in visitors, so for a profile with
   none, a search for the brand's name finds candidate sites, and one counts
   only when its own page links back to that very profile (``find_site``).
   Each company gets source ``social:<platform>`` and the bio as its
   description. Link pages, marketplaces, social hosts, private accounts and
   companies Harvey already has are skipped.

That's all: prospecting then finds the people at each company from its site
(``_prospect_via_known_companies``), and verification, targeting, the writer
and the outbox treat them like any other.

LinkedIn is never read this way: Scout's LinkedIn scraper needs the owner's
own session cookie, and that account runs Harvey's LinkedIn channel. Three
failed reads in a row (a platform turning us away) stop this source for a day.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import subprocess
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

logger = logging.getLogger("harvey.social_leads")

SOURCE_PREFIX = "social:"
CURSOR_KEY = "social_leads:cursor"
SEEN_KEY = "social_leads:seen"            # handles already read, so none is read twice
PAUSED_KEY = "social_leads:paused_until"
ACTION = "social_leads"
MAX_SEEN = 5000
FAILS_TO_PAUSE = 3
PAUSE_HOURS = 24
BIO_CHARS = 400
PROFILE_SECONDS = 25                      # time allowed per profile: Scout's pause plus the page
DELAY = (1.5, 4.5)                        # seconds between profiles, as Scout paces itself
SEARCH_GAP = 2.1                          # seconds between this source's searches (TinyFish: 30 a minute)
LOOKUP_PAGES = 3                          # candidate sites opened per brand-name search
# Platforms whose logged-out profile hides the link: the site is looked up instead.
LOOKUP_PLATFORMS = ("instagram", "tiktok", "youtube")
BRIDGE = Path(__file__).resolve().parent / "collectors" / "scout_bridge.py"

ALLOWED = ("instagram", "tiktok", "youtube", "pinterest", "linktree")   # never linkedin
SITES = {"instagram": "instagram.com", "tiktok": "tiktok.com", "youtube": "youtube.com",
         "pinterest": "pinterest.com", "linktree": "linktr.ee"}

# Path words that aren't somebody's handle.
_RESERVED = {
    "instagram": {"p", "reel", "reels", "explore", "stories", "tv", "accounts", "about", "legal",
                  "directory", "developer", "web", "challenge", "popular", "privacy"},
    "pinterest": {"pin", "search", "ideas", "today", "categories", "explore", "business", "login",
                  "settings", "about", "topics", "videos", "_", "shopping", "news_hub"},
    "linktree": {"s", "marketplace", "blog", "login", "register", "discover", "help", "features",
                 "pricing", "templates", "terms", "privacy", "admin", "creators"},
}
_HANDLE_RE = {
    "instagram": re.compile(r"^[A-Za-z0-9._]{2,30}$"),
    "tiktok": re.compile(r"^[A-Za-z0-9._]{2,30}$"),
    "youtube": re.compile(r"^[A-Za-z0-9._-]{2,40}$"),
    "pinterest": re.compile(r"^[A-Za-z0-9_]{3,30}$"),
    "linktree": re.compile(r"^[A-Za-z0-9._-]{2,40}$"),
}

# Not a brand's own site: link pages, marketplaces, social, payment and link hosts.
NOT_A_SITE = (
    "linktr.ee", "beacons.ai", "bio.link", "linkr.bio", "stan.store", "taplink.cc", "lnk.bio",
    "campsite.bio", "solo.to", "shor.by", "bit.ly", "tinyurl.com", "linkin.bio", "msha.ke",
    "withkoji.com", "koji.to", "hoo.be", "snipfeed.co", "carrd.co", "linkpop.com", "allmylinks.com",
    "instagram.com", "facebook.com", "fb.me", "fb.com", "tiktok.com", "youtube.com", "youtu.be",
    "twitter.com", "x.com", "pinterest.com", "pin.it", "threads.net", "snapchat.com", "linkedin.com",
    "wa.me", "whatsapp.com", "t.me", "telegram.me", "discord.gg", "discord.com", "spotify.com",
    "apple.com", "soundcloud.com", "twitch.tv", "patreon.com", "onlyfans.com", "ko-fi.com",
    "buymeacoffee.com", "gofundme.com", "paypal.me", "paypal.com", "venmo.com", "cash.app",
    "etsy.com", "amzn.to", "ebay.com", "depop.com", "poshmark.com", "shopmy.us", "liketoknow.it",
    "shopltk.com", "mavely.app", "substack.com", "medium.com", "google.com", "goo.gl", "forms.gle",
    "calendly.com", "canva.com", "canva.site", "gumroad.com", "eepurl.com", "wixsite.com",
    "square.site", "bigcartel.com", "faire.com", "temu.com", "shein.com", "aliexpress.com",
    # Registries and directories that name a brand without being it.
    "service.gov.uk", "opencorporates.com", "companieshouse.gov.uk", "yelp.com", "yelp.co.uk",
    "yell.com", "tripadvisor.com", "tripadvisor.co.uk", "trustpilot.com", "crunchbase.com",
    "bloomberg.com", "dnb.com", "zoominfo.com", "glassdoor.com", "indeed.com", "wikipedia.org",
)
_MARKET_PREFIXES = ("amazon.", "ebay.", "etsy.")
FREE_MAIL = ("gmail.com", "googlemail.com", "yahoo.com", "yahoo.co.uk", "hotmail.com", "hotmail.co.uk",
             "outlook.com", "live.com", "msn.com", "icloud.com", "me.com", "mac.com", "aol.com",
             "proton.me", "protonmail.com", "gmx.com", "gmx.de", "mail.com", "yandex.com", "zoho.com")
_URL_RE = re.compile(r"(?:https?://|www\.)[^\s<>\"')]+", re.IGNORECASE)
_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@([A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+)")


@dataclass(frozen=True)
class SocialQuery:
    platform: str
    term: str
    place: str = ""

    def text(self) -> str:
        q = f'site:{SITES[self.platform]} "{self.term}"'
        return q + (f' "{self.place}"' if self.place else "")


@dataclass
class Found:
    platform: str
    handle: str
    name: str
    domain: str
    website: str
    bio: str
    profile_url: str


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _clean(text) -> str:
    return " ".join(str(text or "").split())


# ── What to search for ──

def default_terms(industries) -> list[str]:
    """Short search terms from icp.industries ("Luxury Fragrance & Jewelry"
    gives "Luxury Fragrance" and "Jewelry")."""
    out: list[str] = []
    for label in industries or []:
        label = re.sub(r"\([^)]*\)", " ", str(label))
        for part in re.split(r"[/,&]|\band\b", label):
            term = _clean(part)
            if 2 < len(term) <= 30 and term.lower() not in {t.lower() for t in out}:
                out.append(term)
    return out


def default_places(geography) -> list[str]:
    """Cities and countries from icp.geography ("New York, New York, United
    States" gives "New York"); blocs like "European Union" are left out."""
    out: list[str] = []
    for entry in geography or []:
        first = _clean(str(entry).split(",")[0])
        if not first or "union" in first.lower():
            continue
        if first.lower() not in {p.lower() for p in out}:
            out.append(first)
    return out


def platforms_of(cfg) -> list[str]:
    """The configured platforms Harvey may read. LinkedIn (and anything
    unknown) is dropped, and said so once in the log."""
    wanted = [str(p).strip().lower() for p in (cfg.platforms or []) if str(p).strip()]
    refused = [p for p in wanted if p not in ALLOWED]
    if refused:
        logger.warning(f"Social leads: not reading {', '.join(refused)} (only {', '.join(ALLOWED)}).")
    return [p for p in wanted if p in ALLOWED]


def bank(config) -> list[SocialQuery]:
    """Every search in rotation order: platforms alternate, then terms, then places."""
    cfg = config.social_leads
    terms = [t for t in (cfg.terms or default_terms(config.icp.industries)) if _clean(t)]
    places = [p for p in (cfg.places or default_places(config.icp.geography)) if _clean(p)] or [""]
    platforms = platforms_of(cfg)
    return [SocialQuery(platform, _clean(term), _clean(place))
            for place in places for term in terms for platform in platforms]


async def next_queries(state, queries: list[SocialQuery], count: int) -> list[SocialQuery]:
    if not queries or count <= 0:
        return []
    try:
        cursor = int(await state.get_setting(CURSOR_KEY) or 0)
    except (TypeError, ValueError):
        cursor = 0
    start = cursor % len(queries)
    return [queries[(start + i) % len(queries)] for i in range(min(count, len(queries)))]


async def advance(state, queries: list[SocialQuery], used: int) -> None:
    if not queries or used <= 0:
        return
    try:
        cursor = int(await state.get_setting(CURSOR_KEY) or 0)
    except (TypeError, ValueError):
        cursor = 0
    await state.set_setting(CURSOR_KEY, str((cursor % len(queries) + used) % len(queries)))


# ── Handles, websites, companies ──

def handle_from_url(platform: str, url: str) -> str:
    """The profile handle a search result points at, or "" (a post, a reel,
    a search page or another site)."""
    try:
        parsed = urlparse(url if "://" in url else "https://" + url)
    except ValueError:
        return ""
    host = (parsed.hostname or "").lower()
    parts = [p for p in parsed.path.split("/") if p]
    if not parts or not (host == SITES[platform] or host.endswith("." + SITES[platform])):
        return ""
    first = unquote(parts[0])
    if platform in ("tiktok", "youtube"):
        if first.startswith("@"):
            handle = first[1:]
        elif platform == "youtube" and first == "channel" and len(parts) > 1:
            return parts[1] if re.match(r"^UC[A-Za-z0-9_-]{22}$", parts[1]) else ""
        else:
            return ""
    else:
        if first.lower() in _RESERVED.get(platform, set()):
            return ""
        handle = first
    return handle if _HANDLE_RE[platform].match(handle) else ""


def _host(url: str) -> str:
    try:
        host = (urlparse(url if "://" in url else "https://" + url).hostname or "").lower()
    except ValueError:
        return ""
    return host[4:] if host.startswith("www.") else host


def not_a_site(domain: str) -> bool:
    d = (domain or "").lower()
    if not d or "." not in d:
        return True
    if d.startswith(_MARKET_PREFIXES):
        return True
    return any(d == h or d.endswith("." + h) for h in NOT_A_SITE)


def _unwrap(url: str) -> str:
    """Instagram and others wrap outbound links ("l.instagram.com/?u=...")."""
    host = _host(url)
    if host.startswith("l.") or host in ("lm.facebook.com", "l.facebook.com"):
        target = parse_qs(urlparse(url).query).get("u", [""])[0]
        if target:
            return unquote(target)
    return url


def website_of(profile: dict) -> str:
    """The brand's own site from a profile: its link, a link on its link-in-bio
    page, a link in the bio, or the domain of a business email; "" if none."""
    candidates = [str(profile.get("website") or "")]
    for link in profile.get("links") or []:
        candidates.append(str(link.get("url") if isinstance(link, dict) else link or ""))
    candidates += _URL_RE.findall(str(profile.get("bio") or ""))
    for raw in candidates:
        url = _unwrap(raw.strip().rstrip(".,;"))
        if not url or url.lower().startswith("mailto:"):
            continue
        if not url.lower().startswith("http"):
            url = "https://" + url
        if _host(url) and not not_a_site(_host(url)):
            return url
    emails = [str(profile.get("email") or "")] + [m.group(0) for m in _EMAIL_RE.finditer(str(profile.get("bio") or ""))]
    for email in emails:
        domain = email.rsplit("@", 1)[-1].lower().strip(".") if "@" in email else ""
        if domain and domain not in FREE_MAIL and not not_a_site(domain):
            return f"https://{domain}"
    return ""


def links_back(html: str, platform: str, handle: str) -> bool:
    """Whether a page links to this profile ("instagram.com/glow.candles")."""
    text, h = (html or "").lower(), handle.lower().lstrip("@")
    if not h:
        return False
    host = SITES[platform]
    return bool(re.search(rf"{re.escape(host)}/@?{re.escape(h)}(?![a-z0-9._-])", text))


def lookup_name(name: str, handle: str) -> str:
    """What to search for: the profile's name, else its handle as words
    ("glow.candles" -> "glow candles")."""
    name = _clean(name)
    if len(name) >= 3:
        return name
    return _clean(re.sub(r"[._]+", " ", handle.lstrip("@")))


async def find_site(agent, platform: str, handle: str, name: str) -> str:
    """The brand's own site when its profile doesn't show one: a search for its
    name (or handle), and the first candidate whose home page links back to
    the profile. "" when none does (never a guess)."""
    from harvey.collectors.discover import is_junk
    name = lookup_name(name, handle)
    if len(name) < 3:
        return ""
    await asyncio.sleep(SEARCH_GAP)
    results = await agent._web_search(f'"{name}"', count=False)
    opened, tried = 0, set()
    for url, _snippet in results:
        domain = _host(url)
        if not domain or domain in tried or not_a_site(domain) or is_junk(domain):
            continue
        tried.add(domain)
        opened += 1
        resp = await agent._fetch(f"https://{domain}", timeout=15, retries=1)
        if resp is not None and links_back(getattr(resp, "text", "") or "", platform, handle):
            return f"https://{domain}"
        if opened >= LOOKUP_PAGES:
            break
    return ""


def from_profile(platform: str, handle: str, profile) -> tuple[Found | None, str]:
    """A company from one profile, or None and why not."""
    if not isinstance(profile, dict):
        return None, "no profile"
    if profile.get("is_private"):
        return None, "private account"
    url = website_of(profile)
    if not url:
        return None, "no website"
    name = _clean(profile.get("full_name") or profile.get("title") or handle)[:120]
    return Found(platform=platform, handle=handle, name=name or handle, domain=_host(url),
                 website=url.split("?")[0], bio=_clean(profile.get("bio"))[:BIO_CHARS],
                 profile_url=str(profile.get("profile_url") or "")), ""


# ── Scout ──

def scout_python(scout_path: str) -> Path:
    folder = Path(scout_path)
    return folder / ".venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")


def _run_bridge(scout_path: str, platform: str, handles: list[str]) -> list[dict]:
    python = scout_python(scout_path)
    if not python.exists():
        raise RuntimeError(f"Scout isn't installed at {scout_path} (no {python.name} in its .venv)")
    # No proxies, and no LinkedIn cookie: Scout reads the profiles directly.
    env = {k: v for k, v in os.environ.items()
           if not k.upper().startswith(("SCOUT_PROXY", "SCOUT_FREE_PROXY")) and k.upper() != "LINKEDIN_COOKIE"}
    env["PYTHONUTF8"] = "1"
    job = json.dumps({"platform": platform, "handles": handles, "delay": list(DELAY),
                      "stop_after_failures": FAILS_TO_PAUSE})
    done = subprocess.run(
        [str(python), str(BRIDGE)], input=job, capture_output=True, text=True, encoding="utf-8",
        errors="replace", cwd=scout_path, env=env, timeout=PROFILE_SECONDS * len(handles) + 30,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    rows = []
    for line in (done.stdout or "").splitlines():
        try:
            rows.append(json.loads(line))
        except ValueError:
            continue
    if not rows and done.returncode:
        raise RuntimeError(f"Scout stopped ({done.returncode}): {(done.stderr or '').strip()[-300:]}")
    return rows


async def read_profiles(cfg, platform: str, handles: list[str], runner=None) -> list[dict]:
    """Scout's reading of each handle: [{"handle", "ok", "profile" | "error"}]."""
    if platform not in ALLOWED:
        raise ValueError(f"{platform} profiles aren't read (only {', '.join(ALLOWED)})")
    if runner is not None:
        return await runner(platform, handles)
    return await asyncio.to_thread(_run_bridge, cfg.scout_path, platform, handles)


# ── One pass ──

def prepare(agent) -> None:
    """What the Scout agent's run() sets up, for a pass started on its own (CLI)."""
    for name, value in (("_seen_domains", set()), ("_queries_this_cycle", 0), ("_site_reads", {}),
                        ("_site_evidence", {}), ("_own_domains", {}), ("_challenged", set()),
                        ("_seen_prospect_keys", set())):
        if not hasattr(agent, name):
            setattr(agent, name, value)


async def run_cycle(agent, *, dry_run: bool = False, limit: int | None = None, runner=None,
                    now: datetime | None = None) -> dict:
    """One pass (see the module's note). ``agent`` is the Scout agent, for its
    web search, domain checks and company records. ``dry_run`` adds nothing
    and remembers nothing. Returns counts, and with ``dry_run`` the domains."""
    from harvey.collectors.discover import is_junk
    state, config = agent.state, agent.config
    cfg = config.social_leads
    now = now or _utcnow()
    report = {"searches": 0, "handles": 0, "read": 0, "failed": 0, "looked_up": 0, "sites_found": 0,
              "added": 0, "skipped": {}, "stopped": "", "found": []}

    def skip(why: str):
        report["skipped"][why] = report["skipped"].get(why, 0) + 1

    if not cfg.scout_path:
        report["stopped"] = "social_leads.scout_path isn't set"
        return report
    paused = await state.get_setting(PAUSED_KEY) or ""
    if paused and not dry_run:
        try:
            until = datetime.fromisoformat(paused)
        except ValueError:
            until = None
        if until and now < until:
            report["stopped"] = f"paused until {paused[:16]} UTC: a platform refused several reads"
            return report
    queries = bank(config)
    if not queries:
        report["stopped"] = "nothing to search for (social_leads.terms and icp.industries are empty)"
        return report
    try:
        seen = set(json.loads(await state.get_setting(SEEN_KEY) or "[]"))
    except ValueError:
        seen = set()
    cap = max(0, int(limit if limit is not None else cfg.profiles_per_cycle))

    picked: dict[str, list[tuple[str, SocialQuery]]] = {}
    keys: set[str] = set()
    ran = 0
    for i, q in enumerate(await next_queries(state, queries, max(1, int(cfg.searches_per_cycle)))):
        if len(keys) >= cap:
            break
        if i:
            await asyncio.sleep(SEARCH_GAP)
        results = await agent._web_search(q.text(), count=False)
        ran += 1
        for url, _snippet in results:
            handle = handle_from_url(q.platform, url)
            key = f"{q.platform}:{handle.lower()}"
            if not handle or key in seen or key in keys:
                continue
            keys.add(key)
            picked.setdefault(q.platform, []).append((handle, q))
            if len(keys) >= cap:
                break
    report["searches"], report["handles"] = ran, len(keys)

    in_a_row = 0
    for platform, items in picked.items():
        if report["stopped"]:
            break
        try:
            rows = await read_profiles(cfg, platform, [h for h, _ in items], runner=runner)
        except Exception as e:
            logger.warning(f"Social leads: Scout couldn't read {platform} profiles: {e}")
            report["failed"] += len(items)
            report["stopped"] = f"Scout couldn't run: {e}"[:200]
            break
        by_handle = {str(r.get("handle") or "").lstrip("@").lower(): r for r in rows}
        for handle, q in items:
            seen.add(f"{platform}:{handle.lower()}")
            row = by_handle.get(handle.lstrip("@").lower())
            if not row or not row.get("ok"):
                report["failed"] += 1
                in_a_row += 1
                if in_a_row >= FAILS_TO_PAUSE:
                    report["stopped"] = f"{platform} refused {in_a_row} reads in a row: paused for a day"
                    if not dry_run:
                        await state.set_setting(PAUSED_KEY, (now + timedelta(hours=PAUSE_HOURS)).isoformat())
                    break
                continue
            in_a_row = 0
            report["read"] += 1
            profile = row.get("profile")
            found, why = from_profile(platform, handle, profile)
            if (found is None and why == "no website" and platform in LOOKUP_PLATFORMS
                    and report["looked_up"] < cfg.site_lookups_per_cycle):
                report["looked_up"] += 1
                name = (profile or {}).get("full_name") or (profile or {}).get("title") or ""
                site = await find_site(agent, platform, handle, name)
                if site:
                    report["sites_found"] += 1
                    found, why = from_profile(platform, handle, {**profile, "website": site})
                else:
                    why = "no website (none links back)"
            if found is None:
                skip(why)
                continue
            if not agent._is_candidate_domain(found.domain) or is_junk(found.domain):
                skip("not a business site")
                continue
            if await state.company_exists(found.domain):
                skip("already known")
                continue
            if report["added"] >= cfg.companies_per_cycle:
                skip("cycle full")
                continue
            agent._seen_domains.add(found.domain)
            report["added"] += 1
            if dry_run:
                report["found"].append({"platform": platform, "handle": handle, "domain": found.domain})
                continue
            await agent._ensure_company(
                name=found.name, domain=found.domain, website=found.website, description=found.bio,
                industry=q.term, location=q.place, source=SOURCE_PREFIX + platform,
                source_url=found.profile_url)

    if not dry_run:
        await advance(state, queries, ran)
        await state.set_setting(SEEN_KEY, json.dumps(sorted(seen)[-MAX_SEEN:]))
        details = {k: v for k, v in report.items() if k != "found"}
        await state.log_action(action_type=ACTION, agent="scout", details=details)
        logger.info(f"Social leads: {report['searches']} searches, {report['handles']} handles, "
                    f"{report['read']} read, {report['added']} companies added"
                    + (f"; {report['stopped']}" if report["stopped"] else ""))
    return report


# ── CLI: harvey social run [--dry-run] [--limit N] ──

def add_cli_parser(subparsers):
    sub = subparsers.add_parser("social", help="New companies from social profiles, read with Scout")
    sub.add_argument("social_action", choices=["run", "status"],
                     help="run: one pass now; status: the settings and the last pass")
    sub.add_argument("--dry-run", action="store_true", help="run: show what it finds, add and remember nothing")
    sub.add_argument("--limit", type=int, default=None, help="run: profiles to read (default profiles_per_cycle)")
    sub.set_defaults(func=cmd_social)


def cmd_social(args):
    from harvey.agents.scout import Scout
    from harvey.config import load_config, load_env
    from harvey.state import StateManager

    async def _run():
        config = load_config()
        state = StateManager()
        await state.init_db()
        cfg = config.social_leads
        if args.social_action == "status":
            last = await state.get_setting(PAUSED_KEY) or ""
            print(f"\n  Social leads: {'on' if cfg.enabled else 'off'} | Scout: {cfg.scout_path or 'not set'}"
                  f" ({'found' if cfg.scout_path and scout_python(cfg.scout_path).exists() else 'missing'})")
            print(f"  Platforms: {', '.join(platforms_of(cfg)) or 'none'} | searches in rotation: {len(bank(config))}")
            if last:
                print(f"  Paused until: {last[:16]} UTC")
            print()
            return
        agent = Scout(brain=None, state=state, config=config, env=load_env())
        prepare(agent)
        report = await run_cycle(agent, dry_run=args.dry_run, limit=args.limit)
        print(f"\n  {'Dry run: ' if args.dry_run else ''}{report['searches']} searches, {report['handles']} handles, "
              f"{report['read']} profiles read, {report['failed']} not read, "
              f"{report['added']} compan{'y' if report['added'] == 1 else 'ies'} "
              f"{'would be ' if args.dry_run else ''}added.")
        if report["skipped"]:
            print("  Skipped: " + ", ".join(f"{n} {why}" for why, n in sorted(report["skipped"].items())))
        if report["stopped"]:
            print(f"  Stopped: {report['stopped']}")
        for row in report["found"]:
            print(f"    {row['platform']:9} {row['domain']}")
        print()

    asyncio.run(_run())
