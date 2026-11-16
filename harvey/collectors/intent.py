"""Stores asking for help in public, right now: the Shopify Community.

Owner, 2026-11-16: "find more discover ways I can use for free". The Shopify
Community runs on Discourse, whose pages answer as JSON with no key. A merchant
stuck on their theme, their collections or their speed often links their store
in the question. Each Scout run reads the newest topics of a few categories,
takes the first post's links, and keeps a domain only when its home page is
really a Shopify store (an app's site or a help page drops out). The company
is added with a note of what they asked, and the backfill finds who to write to
on their own site. Nobody is ever messaged on the forum: its rules ban
unsolicited private messages.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import httpx

logger = logging.getLogger("harvey.intent")

COMMUNITY = "https://community.shopify.com"
FEEDS = (f"{COMMUNITY}/c/store-design/133.json", f"{COMMUNITY}/c/technical-qa/211.json",
         f"{COMMUNITY}/latest.json")
SEEN_KEY = "intent:shopify_seen"
MAX_SEEN = 3000
MAX_AGE_DAYS = 14
UA = {"User-Agent": "Harvey/0.1 (open-source sales agent)", "Accept": "application/json"}
TIMEOUT = httpx.Timeout(connect=10.0, read=30.0, write=10.0, pool=10.0)
_LINK_RE = re.compile(r'href="(https?://[^"#]+)"', re.IGNORECASE)
# Hosts in a post that are never the poster's store.
_NOT_A_STORE_RE = re.compile(
    r"(^|\.)(shopify\.com|shopify\.dev|shopifycdn\.com|shopifysvc\.com|myshopify\.io|youtube\.com|"
    r"youtu\.be|imgur\.com|google\.com|github\.com|gstatic\.com|loom\.com|wikipedia\.org|"
    r"apple\.com|facebook\.com|instagram\.com|tiktok\.com|x\.com|twitter\.com|w3\.org|"
    r"mozilla\.org|stackoverflow\.com|amazonaws\.com|cloudfront\.net)$", re.IGNORECASE)
# What a Shopify storefront's home page always carries.
_SHOPIFY_RE = re.compile(r"cdn\.shopify\.com|Shopify\.theme|shopify-section|myshopify\.com", re.IGNORECASE)


@dataclass
class IntentLead:
    domain: str
    store_url: str
    title: str
    topic_url: str
    asked_on: str          # YYYY-MM-DD

    def note(self) -> str:
        return f"Asked on the Shopify Community on {self.asked_on}: {self.title[:160]}"


def store_links(cooked_html: str) -> list[str]:
    """Links in a post that could be the poster's store, in order."""
    from harvey.collectors.discover import normalize_domain
    out = []
    for url in _LINK_RE.findall(cooked_html or ""):
        domain = normalize_domain(url)
        if not domain or "." not in domain or _NOT_A_STORE_RE.search(domain):
            continue
        if re.search(r"\.(png|jpe?g|gif|webp|svg|mp4|pdf)(\?|$)", url, re.IGNORECASE):
            continue
        if url not in out:
            out.append(url)
    return out


async def _get_json(client, url):
    r = await client.get(url)
    if r.status_code != 200:
        raise RuntimeError(f"{url} answered {r.status_code}")
    return r.json()


async def shopify_store_at(client, url: str) -> str:
    """The store's own domain when ``url`` is a Shopify storefront (after
    redirects, so a *.myshopify.com link gives the store's real domain), or ""."""
    from harvey.collectors.discover import normalize_domain
    try:
        r = await client.get(url, headers={"Accept": "text/html"})
    except httpx.HTTPError:
        return ""
    if r.status_code >= 400 or not _SHOPIFY_RE.search(r.text[:300000]):
        return ""
    return normalize_domain(str(r.url)) or normalize_domain(url)


async def shopify_community_leads(state, limit: int = 15, now: datetime | None = None,
                                  client: httpx.AsyncClient | None = None) -> list[IntentLead]:
    """Stores linked in new Shopify Community questions (each topic read
    once, MAX_AGE_DAYS at most). Raises only when no feed answers at all."""
    from harvey.collectors.discover import normalize_domain
    now = now or datetime.now(timezone.utc)
    cutoff = now - timedelta(days=MAX_AGE_DAYS)
    try:
        seen = set(json.loads(await state.get_setting(SEEN_KEY) or "[]"))
    except ValueError:
        seen = set()
    owned = client is None
    client = client or httpx.AsyncClient(timeout=TIMEOUT, headers=UA, follow_redirects=True)
    leads: list[IntentLead] = []
    try:
        topics, errors = {}, []
        for feed in FEEDS:
            try:
                data = await _get_json(client, feed)
            except Exception as e:
                errors.append(str(e))
                continue
            for t in (data.get("topic_list") or {}).get("topics") or []:
                if t.get("id") is not None:
                    topics[t["id"]] = t
        if not topics and errors:
            raise RuntimeError("; ".join(errors)[:300])
        fresh = []
        for t in topics.values():
            try:
                created = datetime.fromisoformat(str(t.get("created_at", "")).replace("Z", "+00:00"))
            except ValueError:
                continue
            if created >= cutoff and str(t["id"]) not in seen and not t.get("pinned"):
                fresh.append((created, t))
        for created, t in sorted(fresh, key=lambda x: x[0], reverse=True)[:limit]:
            seen.add(str(t["id"]))
            topic_url = f"{COMMUNITY}/t/{t.get('slug', 'topic')}/{t['id']}"
            try:
                data = await _get_json(client, topic_url + ".json")
                cooked = ((data.get("post_stream") or {}).get("posts") or [{}])[0].get("cooked", "")
            except Exception as e:
                logger.debug(f"intent: couldn't read {topic_url}: {e}")
                continue
            for url in store_links(cooked)[:3]:
                domain = await shopify_store_at(client, url)
                if domain and not _NOT_A_STORE_RE.search(domain):
                    leads.append(IntentLead(domain=domain, store_url=f"https://{domain}/",
                                            title=" ".join(str(t.get("title") or "").split()),
                                            topic_url=topic_url, asked_on=created.date().isoformat()))
                    break
    finally:
        await state.set_setting(SEEN_KEY, json.dumps(sorted(seen)[-MAX_SEEN:]))
        if owned:
            await client.aclose()
    return leads
