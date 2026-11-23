"""LinkedIn's whole inbox, read once into Harvey's records (read-only on LinkedIn).

Owner, 2026-11-23: "compile all the conversations across all the platforms
... accurately". Harvey only records a LinkedIn thread when it opens one with
a new message, so the chats from before Harvey ran LinkedIn, and your own on
LinkedIn, were missing, and meetings agreed in them could never be found
(harvey/meeting_finder.py).

``backfill`` borrows Harvey's LinkedIn window between runs (it waits for a
running one to finish, and marks LinkedIn busy meanwhile), scrolls the whole
conversation list (pressing "Load more conversations"), and opens each thread
at a reading pace by clicking it, as LinkedIn's list has no thread links now.
An unread conversation is left unread, for Harvey's own inbox read and for
you. What it finds is stored with each message's date from the page
(``pages.parse_thread``):

- a lead Harvey knows gets the messages it was missing;
- someone who isn't a lead becomes one only when the thread speaks of a
  meeting, set aside ("from your inbox history") so Harvey never writes to
  them on its own.

Every message stored here carries intent ``history``, so the reply step never
answers an old message. Nothing is clicked but the threads themselves, and
the usual guards stop it on any LinkedIn warning. A finished thread is noted
(``DONE_KEY``) so a second run picks up where the first stopped.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from datetime import datetime, timedelta, timezone

logger = logging.getLogger("harvey.linkedin")

DONE_KEY = "linkedin:history:done"       # thread URLs already read
HISTORY = "history"                      # the intent of every message stored here
INBOX_LEAD_REASON = "from your inbox history: Harvey doesn't write to them on its own"
MAX_THREADS = 500
MAX_LISTED = 1000
MAX_SCROLLS = 120
WAIT_FOR_RUN_MINUTES = 120


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _parse(value) -> datetime | None:
    if not value:
        return None
    try:
        moment = datetime.fromisoformat(str(value).replace("Z", ""))
    except ValueError:
        return None
    return moment.replace(tzinfo=None) if moment.tzinfo is None else moment.astimezone(timezone.utc).replace(tzinfo=None)


async def _all_cards(agent):
    """Every conversation in the list, in its order: scroll until it stops
    growing, pressing "Load more conversations" when it shows."""
    from harvey.linkedin.pages import CONVERSATION_LIST, parse_conversation_list
    seen, still, cards = 0, 0, []
    for _ in range(MAX_SCROLLS):
        html = await agent.driver.html()
        await agent._guard(html)
        cards = parse_conversation_list(html)
        if len(cards) >= MAX_LISTED:
            break
        if len(cards) > seen:
            still, seen = 0, len(cards)
        else:
            still += 1
            if still >= 2 and await agent.driver.load_more_conversations():
                still = 0
                await agent.driver.wait(2.0)
                continue
            if still >= 3:
                break
        await agent.driver.scroll(CONVERSATION_LIST, 4)
        await agent.driver.wait(1.2)
    return cards


def card_key(card) -> str:
    """A conversation as the list shows it, before it is opened: a new
    message changes the snippet, so it is read again."""
    return f"card:{card.name}|{card.snippet[:80]}"


def member_id(slug: str) -> bool:
    """A LinkedIn member id ("ACoAA..."), as thread pages link people, rather
    than a profile name ("jane-doe-1")."""
    return (slug or "").lower().startswith("aco") and len(slug) >= 30 and "-" not in slug


def thread_profile_slug(html: str, name: str = "") -> str:
    """Their profile slug from an open thread: the first /in/ link that
    names them (the thread header links to their profile)."""
    from harvey.linkedin.pages import names_match, slug_from_url, soup_of
    s = soup_of(html)
    for a in s.select("a[href*='/in/']"):
        if a.find_parent("aside"):
            continue
        slug = slug_from_url(a.get("href") or "")
        text = " ".join(a.get_text(" ").split())
        if slug and (not name or names_match(text, name) or names_match(a.get("aria-label") or "", name)):
            return slug
    return ""


async def record_history(state, lead: dict, thread, url: str, now: datetime | None = None) -> int:
    """Store the thread's messages Harvey doesn't have. Returns how many."""
    from harvey.chat_style import parts as chat_parts, squash
    from harvey.linkedin.pages import clean_text, names_match
    now = now or _utcnow()
    known: dict[str, datetime | None] = {}
    for m in await state.li_messages(lead_id=lead["id"], limit=10000):
        when = _parse(m.get("sent_at") or m.get("created_at"))
        known[squash(m["body"] or "")] = when
        for p in chat_parts(m["body"] or ""):
            known[squash(p)] = when
    added = 0
    n = len(thread.messages)
    # With no date on the page, a missing message goes just after the one
    # before it that Harvey has, so the order stays the thread's.
    last_at: datetime | None = None
    for i, message in enumerate(thread.messages):
        body = clean_text(message.body)
        if not body:
            continue
        if squash(body) in known:
            last_at = known[squash(body)] or last_at
            continue
        theirs = names_match(message.sender, lead.get("name") or "")
        at = getattr(message, "at", None) or (last_at + timedelta(seconds=1) if last_at
                                              else now - timedelta(seconds=n - i))
        last_at = at
        known[squash(body)] = at
        if await state.li_add_message(
                lead["id"], direction="in" if theirs else "out", kind="message" if theirs else "reply",
                body=message.body, status="sent", sent_at=at, created_at=at, thread_url=url,
                intent=HISTORY if theirs else "", approved_by="" if theirs else "you on LinkedIn", dedupe=True):
            added += 1
    return added


async def _open(agent, card) -> str:
    """Open one conversation at a reading pace: by its link when the list
    has one, else by clicking it in the list (LinkedIn's list today).
    Returns the page, or "" when it didn't open."""
    if card.thread_url:
        return await agent._visit(card.thread_url)
    await agent.pacing.between_pages()
    await agent._still_on()
    if not await agent.driver.open_conversation(card.index):
        return ""
    html = await agent.driver.html()
    await agent._guard(html)
    return html


async def backfill(state, config, progress=None, max_threads: int = MAX_THREADS, *, driver_factory=None,
                   agent_options=None) -> dict:
    """Read LinkedIn's inbox into Harvey's records (see the module's note).
    Returns {"listed", "threads", "new_messages", "new_people", "skipped", "unread", "stopped"}."""
    from harvey.agents.linkedin_agent import RUNNING_KEY, LinkedInAgent, SwitchedOff
    from harvey.kept_browser import own_window
    from harvey.linkedin.guards import BreakerTripped
    from harvey.linkedin.pages import MESSAGING_URL, names_match, parse_thread
    from harvey.linkedin.session import PageNotLoading, open_session
    from harvey.meeting_alert import MEETING_RE
    from harvey.meeting_finder import LINK_RE

    report = {"listed": 0, "threads": 0, "new_messages": 0, "new_people": 0, "skipped": 0, "unread": 0,
              "stopped": ""}
    cfg = config.channels.linkedin
    for _ in range(WAIT_FOR_RUN_MINUTES * 3):
        if not (await state.get_setting(RUNNING_KEY) or ""):
            break
        await asyncio.sleep(20)
    else:
        report["stopped"] = "a LinkedIn run kept the window busy; try again later"
        return report
    await state.set_setting(RUNNING_KEY, _utcnow().isoformat())
    try:
        done = set(json.loads(await state.get_setting(DONE_KEY) or "[]"))
    except ValueError:
        done = set()
    agent = LinkedInAgent(None, state, config, **(agent_options or {}), driver_factory=driver_factory or (
        lambda: own_window(state, "linkedin", lambda: open_session(cfg))))
    agent.dry_run, agent.deadline, agent.replies_only = False, None, False
    agent.report = {"would_send": []}
    try:
        async with agent.driver_factory() as driver:
            agent.driver = driver
            await agent._check_signed_in()
            await agent._visit(MESSAGING_URL)
            cards = await _all_cards(agent)
            report["listed"] = len(cards)
            leads = await state.li_leads()
            for card in cards:
                if report["threads"] >= max_threads:
                    break
                key = card_key(card)
                if card.unread:
                    report["unread"] += 1      # left unread: Harvey's own inbox read answers it
                    continue
                if key in done or (card.thread_url and card.thread_url in done):
                    continue
                if await agent._held():
                    report["stopped"] = "LinkedIn was switched off or paused"
                    break
                try:
                    html = await _open(agent, card)
                except PageNotLoading:
                    html = ""
                url = (agent.driver.url or "").split("?")[0]
                if not html or "/messaging/thread/" not in url:
                    report["skipped"] += 1
                    continue
                thread = parse_thread(html)
                if thread.header and not names_match(thread.header, card.name):
                    # The pane still shows the conversation before: read it once more.
                    await agent.driver.wait(2.0)
                    html = await agent.driver.html()
                    thread = parse_thread(html)
                    if thread.header and not names_match(thread.header, card.name):
                        report["skipped"] += 1
                        continue
                report["threads"] += 1
                if url in done:
                    done.add(key)
                    continue
                slug = thread_profile_slug(html, card.name)
                lead = None
                if slug:
                    lead = await state.li_get_lead_by_slug(slug)
                if lead is None:
                    # A thread links them by LinkedIn's member id ("ACoAA..."),
                    # not their profile name, so a lead is known by name: one
                    # lead only, and not one whose profile name differs.
                    matches = [x for x in leads if names_match(card.name, x.get("name") or "")]
                    if len(matches) == 1 and (not slug or member_id(slug) or member_id(matches[0]["slug"])):
                        lead = matches[0]
                if lead is None:
                    text = " ".join(m.body for m in thread.messages)
                    if not slug or not (MEETING_RE.search(text) or LINK_RE.search(text)):
                        report["skipped"] += 1
                        done.update((url, key))
                        continue
                    theirs = any(names_match(m.sender, card.name) for m in thread.messages)
                    lead_id, _ = await state.li_upsert_lead(
                        slug, stage="replied" if theirs else "messaged", name=card.name,
                        first_name=card.name.split(" ")[0], source="inbox", thread_url=url,
                        no_message_reason=INBOX_LEAD_REASON)
                    lead = await state.li_get_lead(lead_id)
                    leads.append(lead)
                    report["new_people"] += 1
                elif not lead.get("thread_url"):
                    await state.li_update_lead(lead["id"], thread_url=url)
                report["new_messages"] += await record_history(state, lead, thread, url)
                done.update((url, key))
                if report["threads"] % 5 == 0:
                    # Still busy: an old stamp reads as a stale run, and the
                    # heartbeat's run then fails on the profile this holds
                    # (live, 2026-11-23: "LinkedIn error" on the owner's phone).
                    await state.set_setting(RUNNING_KEY, _utcnow().isoformat())
                    await state.set_setting(DONE_KEY, json.dumps(sorted(done)))
                    if progress:
                        await progress(dict(report))
    except BreakerTripped as e:
        report["stopped"] = "LinkedIn warning: " + e.breaker.reason
    except SwitchedOff:
        report["stopped"] = "LinkedIn was switched off or paused"
    finally:
        await state.set_setting(DONE_KEY, json.dumps(sorted(done)))
        await state.set_setting(RUNNING_KEY, "")
    if progress:
        await progress(dict(report))
    logger.info(f"LinkedIn inbox read: {report}")
    return report
