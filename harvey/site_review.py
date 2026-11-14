"""The site review: what someone gets when they say yes to "want me to write up
the three changes I'd make first?".

Owner, 2026-11-14: "you are telling them that I have found several issues in
their website, how are you planning to address that if they send yes, give me
the issues". The answer they chose: real fixes plus a call. Every review waits
for the owner's approval until they turn on reviews autopilot on the dashboard
("I will check quality every time, and if you are doing good I will give you a
free hand").

1. ``measure``: their home page, measured, never guessed.
   - Headless system Chrome (Playwright) as a mid-range phone on 4G (412x823,
     Chrome's Fast 4G preset, CPU 4x slower): when the largest element showed (LCP), how much the layout
     moved (CLS), what the page downloaded, the heaviest images, scripts that
     hold up the first paint, images without alt text or a size, and the
     page's title, description, headings and structured data.
   - Google PageSpeed Insights (mobile) as well when PAGESPEED_API_KEY is set
     (free; the keyless quota is shared by everyone and usually used up):
     Lighthouse's scores and Google's real-visitor field data.
2. ``write_review``: the brain picks the two or three fixes that matter most,
   each from a measured fact, and asks for a 15-minute call with the booking link.
3. ``review_problem``: every number in it must be one the facts hold, the only
   links are the booking page and their own site, and it stays short.
4. It is queued held for approval (``HELD_REASON``) unless reviews autopilot
   (``REVIEW_AUTOPILOT_KEY``) is on; then it is approved as ``REVIEW_AUTOPILOT``,
   still under the autopilot review for planted links.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone

logger = logging.getLogger("harvey.site_review")

REVIEW_AUTOPILOT_KEY = "reviews:autopilot"
REVIEW_AUTOPILOT = "reviews autopilot"     # approved_by on a review autopilot let go
REVIEW_ANGLE = "site_review"
HELD_REASON = ("a site review: read it before it goes. Turn on reviews autopilot on the "
               "dashboard to let them go on their own")
NO_SITE = "no website to measure"

# Our earlier message offered the write-up (house playbook: "Want me to write up
# the three changes I'd make first?"), or said we'd found things on their site.
OFFER_RE = re.compile(
    r"write (?:it |them |those |these )?up|written (?:review|list)|"
    r"(?:two or )?three (?:changes|fixes|things)|changes I'?d make|fixes (?:I|we)'?d make|"
    r"first fixes|short list of fixes|list of (?:fixes|changes|issues)|"
    r"found (?:a few|several|some|a couple of|\d+) (?:issues|problems|things|fixes)|"
    r"send (?:you |over )?(?:the |a |my )?(?:list|fixes|changes|issues|review|notes)",
    re.IGNORECASE)
# Harvey's own earlier reply promised the write-up was coming.
PROMISED_RE = re.compile(
    r"two working days|within (?:a day|two days|48 hours)|I'?ll (?:write|send|put) (?:it|them|this|that|"
    r"the (?:list|changes|fixes|review|write-?up))", re.IGNORECASE)
REVIEW_INTENTS = ("interested", "question")

MOBILE_UA = ("Mozilla/5.0 (Linux; Android 11; moto g power (2022)) AppleWebKit/537.36 "
             "(KHTML, like Gecko) Chrome/129.0.0.0 Mobile Safari/537.36")
VIEWPORT = {"width": 412, "height": 823}
# Chrome's "Fast 4G" preset with a mid-range phone's CPU. (Lighthouse's
# "slow 4G" figures come from a simulation; applied to a real page load they
# overstate a heavy page several times over, which a prospect who checks
# would read as exaggeration.)
LATENCY_MS = 60
DOWN_BPS = 9 * 1024 * 1024 / 8
UP_BPS = 1.5 * 1024 * 1024 / 8
CPU_SLOWDOWN = 4
CONDITIONS = "a mid-range phone over 4G"
PSI_URL = "https://www.googleapis.com/pagespeedonline/v5/runPagespeed"

MAX_WORDS = {"email": 200, "linkedin": 160}
BANNED = ("free", "no cost", "no-cost", "guarantee", "no obligation", "no strings")
_NUMBER_RE = re.compile(r"\d+(?:[.,]\d+)?")
_LINK_RE = re.compile(r"https?://[^\s<>()\"']+", re.IGNORECASE)

_OBSERVE_JS = """
window.__hv = {lcp: 0, lcpWhat: '', cls: 0};
try {
  new PerformanceObserver(list => {
    for (const e of list.getEntries()) {
      window.__hv.lcp = e.startTime;
      const el = e.element;
      window.__hv.lcpWhat = e.url ? 'image' : (el ? el.tagName.toLowerCase() : '');
    }
  }).observe({type: 'largest-contentful-paint', buffered: true});
  new PerformanceObserver(list => {
    for (const e of list.getEntries()) if (!e.hadRecentInput) window.__hv.cls += e.value;
  }).observe({type: 'layout-shift', buffered: true});
} catch (e) {}
"""

_COLLECT_JS = """
() => {
  const nav = performance.getEntriesByType('navigation')[0] || {};
  const fcp = (performance.getEntriesByName('first-contentful-paint')[0] || {}).startTime || 0;
  const imgs = Array.from(document.images);
  const visible = imgs.filter(i => i.naturalWidth > 1 && i.naturalHeight > 1);
  const meta = n => { const m = document.querySelector(`meta[name="${n}"]`); return m ? (m.content || '').trim() : ''; };
  const headBlocking = Array.from(document.head ? document.head.querySelectorAll('script[src]') : [])
    .filter(s => !s.async && !s.defer && (s.type || '').toLowerCase() !== 'module').length;
  let theme = '';
  try { theme = (window.Shopify && Shopify.theme && Shopify.theme.name) || ''; } catch (e) {}
  return {
    title: document.title || '',
    description: meta('description'),
    viewport: !!document.querySelector('meta[name="viewport"]'),
    h1: document.querySelectorAll('h1').length,
    ld: document.querySelectorAll('script[type="application/ld+json"]').length,
    lang: !!(document.documentElement.getAttribute('lang') || '').trim(),
    images: visible.length,
    noAlt: visible.filter(i => !i.hasAttribute('alt')).length,
    noSize: visible.filter(i => !i.getAttribute('width') || !i.getAttribute('height')).length,
    headBlocking: headBlocking,
    shopify: !!window.Shopify,
    theme: theme,
    ttfb: nav.responseStart || 0,
    load: nav.loadEventEnd || 0,
    fcp: fcp,
    lcp: window.__hv ? window.__hv.lcp : 0,
    lcpWhat: window.__hv ? window.__hv.lcpWhat : '',
    cls: window.__hv ? window.__hv.cls : 0,
    challenge: /just a moment|attention required|access denied|verify you are human/i.test(document.title || ''),
  };
}
"""


@dataclass
class Measured:
    url: str
    ok: bool = False
    why: str = ""
    final_url: str = ""
    measured_on: str = ""
    lcp_s: float = 0.0
    lcp_what: str = ""
    cls: float = 0.0
    fcp_s: float = 0.0
    ttfb_s: float = 0.0
    load_s: float = 0.0
    bytes_total: int = 0
    requests: int = 0
    image_bytes: int = 0
    script_bytes: int = 0
    heaviest_images: list = field(default_factory=list)   # [(file name, KB)]
    other_hosts: int = 0
    images: int = 0
    no_alt: int = 0
    no_size: int = 0
    head_blocking: int = 0
    title: str = ""
    description: bool = True
    viewport: bool = True
    h1: int = 1
    structured_data: bool = True
    shopify: bool = False
    theme: str = ""
    psi: dict = field(default_factory=dict)

    def as_json(self) -> str:
        return json.dumps(asdict(self), default=str)[:20000]


def _registrable(host: str) -> str:
    parts = [p for p in (host or "").lower().split(".") if p]
    if len(parts) >= 3 and len(parts[-1]) == 2 and parts[-2] in ("co", "com", "org", "net", "ac", "gov"):
        return ".".join(parts[-3:])
    return ".".join(parts[-2:])


def _file_name(url: str) -> str:
    path = urllib.parse.urlparse(url).path.rstrip("/")
    name = urllib.parse.unquote(path.rsplit("/", 1)[-1]) if path else ""
    name = re.sub(r"[?#].*", "", name)
    if len(name) > 40:
        stem, dot, ext = name.rpartition(".")
        name = (stem[:32] + "..." + dot + ext) if dot and len(ext) <= 5 else name[:40] + "..."
    return name or "an image"


def site_url(domain_or_url: str) -> str:
    text = (domain_or_url or "").strip()
    if not text:
        return ""
    if not re.match(r"https?://", text, re.I):
        text = "https://" + text.lstrip("/")
    parsed = urllib.parse.urlparse(text)
    return f"{parsed.scheme}://{parsed.netloc}/" if parsed.netloc else ""


async def measure_in_chrome(url: str, chrome_path: str = "", timeout: float = 75) -> Measured:
    """The home page in headless system Chrome, as a phone on slow 4G."""
    from playwright.async_api import async_playwright

    from harvey.linkedin.session import find_chrome

    m = Measured(url=url, measured_on=datetime.now(timezone.utc).date().isoformat())
    chrome = find_chrome(chrome_path)
    seen: dict[str, dict] = {}

    def response(event):
        r = event.get("response") or {}
        seen.setdefault(event["requestId"], {}).update(
            url=r.get("url", ""), type=event.get("type", ""), mime=r.get("mimeType", ""))

    def finished(event):
        seen.setdefault(event["requestId"], {})["bytes"] = int(event.get("encodedDataLength") or 0)

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(executable_path=chrome, headless=True,
                                           args=["--no-first-run", "--no-default-browser-check"])
        try:
            context = await browser.new_context(viewport=VIEWPORT, device_scale_factor=1.75,
                                                is_mobile=True, has_touch=True, user_agent=MOBILE_UA)
            page = await context.new_page()
            cdp = await context.new_cdp_session(page)
            await cdp.send("Network.enable")
            await cdp.send("Network.setCacheDisabled", {"cacheDisabled": True})
            await cdp.send("Network.emulateNetworkConditions", {
                "offline": False, "latency": LATENCY_MS, "downloadThroughput": DOWN_BPS,
                "uploadThroughput": UP_BPS})
            await cdp.send("Emulation.setCPUThrottlingRate", {"rate": CPU_SLOWDOWN})
            cdp.on("Network.responseReceived", response)
            cdp.on("Network.loadingFinished", finished)
            await page.add_init_script(_OBSERVE_JS)
            answer = await page.goto(url, wait_until="load", timeout=int(timeout * 1000))
            if answer is not None and answer.status >= 400:
                m.why = f"the home page answered {answer.status}"
                return m
            try:
                await page.wait_for_load_state("networkidle", timeout=10000)
            except Exception:
                pass
            await page.wait_for_timeout(1500)
            data = await page.evaluate(_COLLECT_JS)
            m.final_url = page.url
        finally:
            await browser.close()
    if data.get("challenge"):
        m.why = "the site showed a bot check instead of the page"
        return m
    own = _registrable(urllib.parse.urlparse(m.final_url or url).hostname or "")
    hosts, images = set(), []
    for item in seen.values():
        size = int(item.get("bytes") or 0)
        if not item.get("url", "").startswith("http"):
            continue
        m.requests += 1
        m.bytes_total += size
        kind = (item.get("type") or "").lower()
        if kind == "image" or (item.get("mime") or "").startswith("image/"):
            m.image_bytes += size
            images.append((size, item["url"]))
        elif kind == "script":
            m.script_bytes += size
        host = urllib.parse.urlparse(item["url"]).hostname or ""
        if host and _registrable(host) != own:
            hosts.add(_registrable(host))
    m.other_hosts = len(hosts)
    m.heaviest_images = [(_file_name(u), round(s / 1024)) for s, u in sorted(images, reverse=True)[:3]]
    m.lcp_s = round(float(data.get("lcp") or 0) / 1000, 1)
    m.lcp_what = data.get("lcpWhat") or ""
    m.cls = round(float(data.get("cls") or 0), 2)
    m.fcp_s = round(float(data.get("fcp") or 0) / 1000, 1)
    m.ttfb_s = round(float(data.get("ttfb") or 0) / 1000, 1)
    m.load_s = round(float(data.get("load") or 0) / 1000, 1)
    m.images, m.no_alt, m.no_size = int(data["images"]), int(data["noAlt"]), int(data["noSize"])
    m.head_blocking = int(data["headBlocking"])
    m.title = (data.get("title") or "").strip()
    m.description = bool(data.get("description"))
    m.viewport, m.h1 = bool(data.get("viewport")), int(data.get("h1") or 0)
    m.structured_data = int(data.get("ld") or 0) > 0
    m.shopify, m.theme = bool(data.get("shopify")), (data.get("theme") or "").strip()[:40]
    m.ok = m.requests > 0
    if not m.ok:
        m.why = "the page loaded nothing"
    return m


def _psi_get(url: str, key: str, timeout: float) -> dict:
    query = urllib.parse.urlencode([("url", url), ("strategy", "mobile"), ("key", key),
                                    ("category", "performance"), ("category", "seo"),
                                    ("category", "accessibility")])
    with urllib.request.urlopen(f"{PSI_URL}?{query}", timeout=timeout) as answer:
        return json.loads(answer.read().decode("utf-8"))


async def pagespeed(url: str, key: str, timeout: float = 90) -> dict:
    """Google PageSpeed Insights, mobile: {"scores": {...}, "field": {...}}, or {}."""
    if not key:
        return {}
    try:
        data = await asyncio.to_thread(_psi_get, url, key, timeout)
    except Exception as e:
        logger.info(f"Site review: PageSpeed Insights didn't answer for {url}: {e}")
        return {}
    lighthouse = data.get("lighthouseResult") or {}
    scores = {name: round(float(c.get("score") or 0) * 100)
              for name, c in (lighthouse.get("categories") or {}).items() if c.get("score") is not None}
    metrics = ((data.get("loadingExperience") or {}).get("metrics") or {})
    field_data = {}
    if "LARGEST_CONTENTFUL_PAINT_MS" in metrics:
        field_data["lcp_s"] = round(metrics["LARGEST_CONTENTFUL_PAINT_MS"]["percentile"] / 1000, 1)
    if "INTERACTION_TO_NEXT_PAINT" in metrics:
        field_data["inp_ms"] = int(metrics["INTERACTION_TO_NEXT_PAINT"]["percentile"])
    if "CUMULATIVE_LAYOUT_SHIFT_SCORE" in metrics:
        field_data["cls"] = round(metrics["CUMULATIVE_LAYOUT_SHIFT_SCORE"]["percentile"] / 100, 2)
    return {"scores": scores, "field": field_data}


async def measure(url: str, config=None, env=None) -> Measured:
    """Chrome first, PageSpeed alongside when a key is set. Never raises."""
    settings = getattr(config, "site_review", None)
    timeout = float(getattr(settings, "timeout_seconds", 90) or 90)
    chrome_path = getattr(getattr(getattr(config, "channels", None), "linkedin", None), "chrome_path", "") or ""
    key = (getattr(env, "pagespeed_api_key", "") or "") if getattr(settings, "pagespeed", True) else ""
    try:
        m, psi = await asyncio.gather(
            asyncio.wait_for(measure_in_chrome(url, chrome_path, timeout=timeout * 0.8), timeout),
            pagespeed(url, key, timeout))
    except Exception as e:
        logger.info(f"Site review: couldn't measure {url}: {type(e).__name__}: {e}")
        return Measured(url=url, why=f"the page didn't load in the test ({type(e).__name__})",
                        measured_on=datetime.now(timezone.utc).date().isoformat())
    m.psi = psi
    return m


def _mb(n: int) -> str:
    return f"{n / (1024 * 1024):.1f}"


def facts(m: Measured) -> list[str]:
    """What the test found, as plain sentences with their numbers: the problems
    first, then the plain measurements. Only these may be claimed."""
    out = []
    if m.lcp_s > 2.5:
        what = " (an image)" if m.lcp_what == "image" else ""
        out.append(f"The largest element on the home page{what} appeared after {m.lcp_s} seconds. "
                   "Google counts 2.5 seconds or less as good.")
    if m.cls > 0.1:
        out.append(f"The layout moved while the page loaded: a layout shift score of {m.cls}, where "
                   "Google counts 0.1 or less as good.")
    if m.bytes_total > 2.5 * 1024 * 1024:
        out.append(f"The home page downloaded {_mb(m.bytes_total)} MB in {m.requests} requests, "
                   f"{_mb(m.image_bytes)} MB of it images.")
    big = [(name, kb) for name, kb in m.heaviest_images if kb >= 250]
    if big:
        out.append("The heaviest images: " + "; ".join(f"{name} at {kb} KB" for name, kb in big) + ".")
    if m.head_blocking >= 3:
        out.append(f"{m.head_blocking} scripts in the page head load before the page can draw "
                   "anything (no async or defer).")
    if m.other_hosts >= 12:
        out.append(f"Files from {m.other_hosts} other domains load on the home page (apps, trackers, widgets).")
    if m.no_alt:
        out.append(f"{m.no_alt} of the {m.images} images have no alt text, so screen readers and "
                   "Google can't tell what they show.")
    if m.no_size >= 3:
        out.append(f"{m.no_size} images have no width and height set, one cause of the page jumping as it loads.")
    if not m.description:
        out.append("The home page has no meta description, so Google writes its own snippet for it.")
    if m.title and len(m.title) > 65:
        out.append(f"The page title is {len(m.title)} characters; Google shows about 60.")
    if not m.title:
        out.append("The home page has no title.")
    if m.h1 == 0:
        out.append("The home page has no H1 heading.")
    elif m.h1 > 2:
        out.append(f"The home page has {m.h1} H1 headings; one is usual.")
    if not m.viewport:
        out.append("The page has no mobile viewport tag, so phones may show it zoomed out.")
    if not m.structured_data:
        out.append("There is no structured data (schema.org) on the home page, so Google gets no "
                   "business or product details from it.")
    if m.ttfb_s > 1.0:
        out.append(f"The server took {m.ttfb_s} seconds to start answering.")
    scores = (m.psi or {}).get("scores") or {}
    if scores:
        named = {"performance": "performance", "seo": "SEO", "accessibility": "accessibility"}
        out.append("Google PageSpeed Insights (mobile) scores out of 100: " + ", ".join(
            f"{named.get(k, k)} {v}" for k, v in scores.items() if k in named) + ".")
    real = (m.psi or {}).get("field") or {}
    if real.get("lcp_s"):
        out.append(f"Google's data from real visitors over the last 28 days: the main content shows "
                   f"after {real['lcp_s']} seconds for 1 in 4 visits or worse.")
    if real.get("inp_ms") and real["inp_ms"] > 200:
        out.append(f"Real visitors wait {real['inp_ms']} ms for the page to respond to a tap "
                   "(Google's good line is 200 ms).")
    measured = (f"Measured on {m.measured_on} on {CONDITIONS}: the largest element at "
                f"{m.lcp_s} seconds, first paint at {m.fcp_s} seconds, layout shift {m.cls}, "
                f"{_mb(m.bytes_total)} MB in {m.requests} requests.")
    out.append(measured)
    if m.shopify:
        out.append(f"The site runs on Shopify" + (f", theme {m.theme}." if m.theme else "."))
    return out


def problem_count(m: Measured) -> int:
    return max(0, len(facts(m)) - 1 - int(m.shopify))


# ── Writing it ──

def _who(config, mailbox_name: str = "") -> str:
    persona = getattr(config, "persona", None)
    company = getattr(persona, "company", "") or "our team"
    name = mailbox_name or getattr(persona, "name", "") or ""
    return f"{name} at {company}" if name else company


def booking_url(config) -> str:
    offer = getattr(getattr(config, "product", None), "offer", None)
    return (getattr(offer, "booking_url", "") or "").strip()


def review_prompt(config, *, name: str, site: str, fact_lines: list[str], their_words: str,
                  channel: str, playbook: str = "", mailbox_name: str = "") -> str:
    from harvey.untrusted import UNTRUSTED_RULE, fence
    booking = booking_url(config)
    words = MAX_WORDS.get(channel, 180)
    greeting = (f'Start with "Hi {name}," on its own line.' if name and channel == "email"
                else (f'Start with "{name}," ' if name else "Start with a short greeting.")
                + " then the review.")
    ask = (f"Close by asking for a 15-minute call to walk through them, with this booking link: {booking}"
           if booking else "Close by asking whether they'd like a 15-minute call to walk through them.")
    rules = f"\n\nHOUSE PLAYBOOK (voice and what we may claim)\n{playbook}" if playbook else ""
    return f"""You are {_who(config, mailbox_name)}. Earlier we offered to write up the changes we would
make first on {site}, and they said yes. Write that review now: it IS the reply, not a promise of one.{rules}

THE MEASUREMENTS (the only facts you may use; every number you write must appear here exactly as written):
{fence(chr(10).join("- " + f for f in fact_lines), "what the test measured")}

{UNTRUSTED_RULE}

WHAT THEY SAID:
{fence(their_words[:1500], "their reply")}

Write:
- {greeting}
- One line saying these come from a test of their home page on a phone.
- Two or three numbered fixes, the ones that would matter most for their sales or leads. Each says
  what we measured (with its number), what it costs them, and what we would change. One or two
  sentences each. Concrete, about their site, nothing generic.
- {ask}
- If the measurements show a site in good shape, say so honestly and give the smaller wins.
- Under {words} words. Plain text: no markdown, no bold, no headings, no dashes, no emojis.
  Never write "free", "no cost" or "guarantee". No other links.
Output ONLY the message."""


def _numbers(text: str) -> set[str]:
    return {n.replace(",", "") for n in _NUMBER_RE.findall(text or "")}


def review_problem(text: str, fact_lines: list[str], config, site_host: str, channel: str) -> str:
    """Why this review can't go out, or ""."""
    body = (text or "").strip()
    if not body:
        return "it was empty"
    lowered = body.lower()
    if lowered.startswith(("i can't", "i cannot", "as an ai", "sorry,")):
        return "it wasn't a review"
    hits = [b for b in BANNED if re.search(rf"\b{re.escape(b)}\b", lowered)]
    if hits:
        return f"it says {hits[0]!r}"
    words = len(body.split())
    if words > MAX_WORDS.get(channel, 180) + 20:
        return f"it is {words} words; keep it under {MAX_WORDS.get(channel, 180)}"
    booking = booking_url(config)
    allowed = _numbers("\n".join(fact_lines)) | _numbers(booking) | {"1", "2", "3", "15"}
    extra = sorted(_numbers(body) - allowed)
    if extra:
        return f"it uses numbers the measurements don't have ({', '.join(extra[:4])})"
    for link in _LINK_RE.findall(body):
        link = link.rstrip(".,;:)")
        host = (urllib.parse.urlparse(link).hostname or "").lower()
        if booking and link.rstrip("/") == booking.rstrip("/"):
            continue
        if host and _registrable(host) == _registrable(site_host):
            continue
        return f"it links to {host or link}, which is neither their site nor the booking page"
    if booking and booking.rstrip("/") not in body:
        return "it doesn't include the booking link"
    if not re.search(r"(?m)^\s*(?:1[.)]|\(1\))", body):
        return "it doesn't number the fixes"
    return ""


def tidy(text: str) -> str:
    text = (text or "").strip().strip('"').strip()
    text = re.sub(r"\*\*(.+?)\*\*", r"\1", text)
    text = re.sub(r"(?m)^#+\s*", "", text)
    text = re.sub(r"\s*[—–]\s*", ", ", text)
    text = re.sub(r"[\U0001F300-\U0001FAFF☀-➿]", "", text)
    return re.sub(r"[ \t]+", " ", text).strip()


async def write_review(brain, config, *, name: str, site: str, fact_lines: list[str],
                       their_words: str, channel: str = "email", playbook: str = "",
                       mailbox_name: str = "", tries: int = 3) -> tuple[str, str]:
    """(the review, "") or ("", why it couldn't be written). Raises
    BrainUnavailable when the provider is down."""
    from harvey.brain import BrainUnavailable
    prompt = review_prompt(config, name=name, site=site, fact_lines=fact_lines,
                           their_words=their_words, channel=channel, playbook=playbook,
                           mailbox_name=mailbox_name)
    host = urllib.parse.urlparse(site_url(site)).hostname or site
    problem = ""
    for _ in range(tries):
        again = f"\n\nYour last draft was refused: {problem}. Write it again." if problem else ""
        text = tidy(await brain.think(prompt + again, session_id="harvey-review",
                                      agent="site_review", task="site_review"))
        if not text and getattr(brain, "paused_reason", ""):
            raise BrainUnavailable(brain.paused_reason)
        problem = review_problem(text, fact_lines, config, host, channel)
        if not problem:
            return text, ""
        logger.info(f"Site review: a draft for {site} was refused: {problem}.")
    return "", f"the review drafts were refused ({problem})"


# ── Who said yes, and where their site is ──

def offered_review(texts) -> bool:
    return any(OFFER_RE.search(t or "") for t in texts)


async def reviews_autopilot(state) -> bool:
    return (await state.get_setting(REVIEW_AUTOPILOT_KEY) or "") == "on"


def domain_of_email(email: str) -> str:
    from harvey.state import FREE_MAIL_DOMAINS
    domain = (email or "").rsplit("@", 1)[-1].strip().lower() if "@" in (email or "") else ""
    return "" if not domain or domain in FREE_MAIL_DOMAINS else domain


async def site_for(state, *, prospect=None, company_id: str = "", website: str = "") -> str:
    """Their home page: the company's domain, a website on record, or their
    business email's domain. "" when none is known."""
    if website:
        return site_url(website)
    company_id = company_id or getattr(prospect, "company_id", "") or ""
    if company_id:
        company = await state.get_company(company_id)
        if company is not None and getattr(company, "domain", ""):
            return site_url(company.domain)
    return site_url(domain_of_email(getattr(prospect, "email", "") or ""))


@dataclass
class Review:
    body: str = ""
    why: str = ""          # why there is no review ("" when there is one)
    site: str = ""
    facts: list = field(default_factory=list)
    measured: Measured | None = None


async def make_review(state, brain, config, env, *, site: str, name: str, their_words: str,
                      channel: str = "email", mailbox_name: str = "") -> Review:
    """Measure their site and write the review. Records what was measured
    (action site_review) either way. Raises BrainUnavailable only."""
    if not site:
        return Review(why=NO_SITE)
    if env is None:
        try:
            from harvey.config import load_env
            env = load_env()
        except Exception:
            env = None
    measured = await measure(site, config, env)
    if not measured.ok:
        await state.log_action("site_review", "site_review", {
            "site": site, "channel": channel, "ok": False, "why": measured.why})
        return Review(why=f"couldn't measure {site}: {measured.why}", site=site, measured=measured)
    fact_lines = facts(measured)
    playbook = ""
    if hasattr(brain, "load_skill"):
        try:
            playbook = brain.load_skill("house_playbook") or ""
        except Exception:
            playbook = ""
    body, why = await write_review(brain, config, name=name, site=site, fact_lines=fact_lines,
                                   their_words=their_words, channel=channel, playbook=playbook,
                                   mailbox_name=mailbox_name)
    await state.log_action("site_review", "site_review", {
        "site": site, "channel": channel, "ok": bool(body), "why": why, "facts": fact_lines,
        "measured": json.loads(measured.as_json())})
    return Review(body=body, why=why, site=site, facts=fact_lines, measured=measured)


# ── The people already promised one, and the CLI ──

async def promised(state) -> list[dict]:
    """Everyone a sent reply promised the write-up to who hasn't had one:
    [{"channel", "name", "site", "their_words", ...}]. Opted out, lost and
    declined people are left out."""
    import aiosqlite

    from harvey.agents.linkedin_agent import REVIEWED_KEY
    out = []
    async with aiosqlite.connect(state.db_path, timeout=30) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
                "SELECT prospect_id, conversation_id, body, sent_at FROM outbox "
                "WHERE kind = 'reply' AND status = 'sent' ORDER BY sent_at") as cursor:
            emails = [dict(r) for r in await cursor.fetchall()]
        async with db.execute(
                "SELECT m.lead_id, m.body, m.sent_at, l.stage FROM linkedin_messages m "
                "JOIN linkedin_leads l ON l.id = m.lead_id WHERE m.direction = 'out' "
                "AND m.status = 'sent' AND m.kind = 'reply' ORDER BY m.sent_at") as cursor:
            linkedin = [dict(r) for r in await cursor.fetchall()]
    seen = set()
    for row in emails:
        pid = row["prospect_id"]
        if pid in seen or not PROMISED_RE.search(row["body"] or ""):
            continue
        seen.add(pid)
        prospect = await state.get_prospect(pid)
        if prospect is None or prospect.status in ("opted_out", "lost"):
            continue
        if await state.has_outbox_angle(pid, REVIEW_ANGLE):
            continue
        convo = await state.get_conversation(row["conversation_id"]) if row["conversation_id"] else None
        words = next((m.content for m in reversed(convo.thread) if m.sender == "prospect"), "") if convo else ""
        out.append({"channel": "email", "prospect_id": pid, "conversation_id": row["conversation_id"],
                    "name": prospect.first_name or "", "who": prospect.email,
                    "site": await site_for(state, prospect=prospect), "their_words": words,
                    "promised_at": row["sent_at"] or ""})
    seen = set()
    for row in linkedin:
        lead_id = row["lead_id"]
        if lead_id in seen or row["stage"] == "declined" or not PROMISED_RE.search(row["body"] or ""):
            continue
        seen.add(lead_id)
        if await state.get_setting(REVIEWED_KEY + lead_id):
            continue
        lead = await state.li_get_lead(lead_id)
        prospect = await state.get_prospect(lead["prospect_id"]) if lead.get("prospect_id") else None
        if prospect is not None and prospect.status in ("opted_out", "lost"):
            continue
        inbound = [m for m in await state.li_messages(lead_id=lead_id) if m["direction"] == "in"]
        out.append({"channel": "linkedin", "lead_id": lead_id,
                    "name": lead.get("first_name") or (lead.get("name") or "").split(" ")[0],
                    "who": lead.get("name") or lead.get("slug") or "",
                    "site": await site_for(state, prospect=prospect,
                                           company_id=lead.get("company_id") or ""),
                    "their_words": inbound[-1]["body"] if inbound else "",
                    "promised_at": row["sent_at"] or ""})
    return out


async def write_promised(state, brain, config, env, only: str = "", site: str = "") -> list[dict]:
    """Write the review for everyone ``promised`` lists, each held for you
    (or approved under reviews autopilot). Returns what happened to each.
    ``site``: their website when none is on record (with ``only`` naming one
    person); nothing about their company is stored from it."""
    from harvey.agents.linkedin_agent import LinkedInAgent
    from harvey.owner_reply import thread_headers
    from harvey.state import HELD_FOR_REVIEW
    results = []
    chosen = [p for p in await promised(state)
              if not only or only.lower() in (p["who"] or "").lower()]
    if site:
        if len(chosen) != 1:
            raise ValueError(f"--site needs a name that matches exactly one person ({len(chosen)} match)")
        chosen[0]["site"] = site_url(site)
    for p in chosen:
        if not p["site"]:
            results.append({**p, "done": False, "why": NO_SITE})
            continue
        review = await make_review(state, brain, config, env, site=p["site"], name=p["name"],
                                   their_words=p["their_words"], channel=p["channel"])
        if not review.body:
            results.append({**p, "done": False, "why": review.why})
            continue
        if p["channel"] == "linkedin":
            agent = LinkedInAgent(brain, state, config, driver_factory=lambda: None, env=env)
            lead = await state.li_get_lead(p["lead_id"])
            await agent.add_review(lead, review.body, thread_url=lead.get("thread_url") or "")
            results.append({**p, "done": True, "body": review.body})
            continue
        prospect = await state.get_prospect(p["prospect_id"])
        convo = await state.get_conversation(p["conversation_id"]) if p["conversation_id"] else None
        headers = await thread_headers(state, prospect.id, convo, fallback_mailbox=prospect.mailbox or "")
        auto = await reviews_autopilot(state)
        item_id = await state.add_outbox_item(
            prospect_id=prospect.id, conversation_id=p["conversation_id"] or "", kind="reply",
            to_email=prospect.email,
            subject=headers.subject if headers else "Re: the changes I'd make",
            body=review.body, send_at=datetime.now(timezone.utc).replace(tzinfo=None).isoformat(),
            status="approved" if auto else "pending_review",
            thread_ref=headers.thread_ref if headers else "",
            in_reply_to=headers.in_reply_to if headers else "",
            approved_by=REVIEW_AUTOPILOT if auto else "",
            mailbox=(headers.mailbox if headers else None) or None, angle=REVIEW_ANGLE)
        if item_id and not auto:
            await state.update_outbox_item(item_id, error=(HELD_FOR_REVIEW + HELD_REASON)[:300])
        results.append({**p, "done": bool(item_id), "body": review.body,
                        "why": "" if item_id else "an identical email is already queued"})
    return results


def add_cli_parser(subparsers):
    sub = subparsers.add_parser(
        "reviews", help="Site reviews: measure a site, list or write the ones already promised")
    sub.add_argument("reviews_action", choices=["promised", "write", "measure"],
                     help="promised: who was promised a write-up and hasn't had it; write: write "
                          "those (each waits for your approval); measure URL: print what the test finds")
    sub.add_argument("target", nargs="?", default="",
                     help="measure: the site; write: only people whose address or name contains this")
    sub.add_argument("--site", default="", metavar="URL",
                     help="write: their website, when Harvey has none on record (one person only)")
    sub.set_defaults(func=cmd_reviews)
    return sub


def cmd_reviews(args):
    from harvey.brain import Brain
    from harvey.config import load_config, load_env
    from harvey.state import StateManager

    async def _run():
        config, env, state = load_config(), load_env(), StateManager()
        await state.init_db()
        if args.reviews_action == "measure":
            m = await measure(site_url(args.target), config, env)
            if not m.ok:
                print(f"\n  Couldn't measure {args.target}: {m.why}")
                return
            print()
            for line in facts(m):
                print(f"  - {line}")
            return
        if args.reviews_action == "promised":
            rows = await promised(state)
            print(f"\n  {len(rows)} promised a write-up and haven't had it:")
            for p in rows:
                print(f"  {p['channel']:<9} {p['who']:<40} {p['site'] or '(no site known)'}")
            return
        try:
            results = await write_promised(state, Brain(state, config.brain), config, env,
                                           only=args.target, site=args.site)
        except ValueError as e:
            print(f"\n  {e}")
            return
        for r in results:
            mark = "held for you" if r["done"] else f"not written: {r['why']}"
            print(f"\n  --- {r['channel']} {r['who']} ({r['site'] or 'no site'}): {mark}")
            if r.get("body"):
                print("\n" + r["body"] + "\n")
        print(f"\n  {sum(r['done'] for r in results)} of {len(results)} written.")
    asyncio.run(_run())
