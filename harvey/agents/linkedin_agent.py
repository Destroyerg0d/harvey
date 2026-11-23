"""LinkedIn agent: one paced session in the dedicated Chrome profile.

A run, inside LinkedIn hours and at most one every ~2 hours:

1. Sync: the messaging inbox (new replies recorded against their lead),
   recent connections (accepted) and the sent-invites page (withdrawn, and
   invites pending over 21 days withdrawn).
2. Replies: classified with the Handler's own rules (its opt-out and
   escalation checks, then its model classifier); a no declines the lead
   and stops their email too; questions and interest get a draft reply.
3. Messages: a first message 1-3 days after they accept, one follow-up
   5-7 days later if silent, never more than 3 messages without a reply.
4. Invites: founders and owners among Harvey's contacts with a LinkedIn
   URL, with a short note while the account allows notes.
5. Send what is approved (everything, on LinkedIn autopilot, except what
   the review holds): open the profile, take its compose link, open it,
   check the recipient is the lead, type, check the text, press Send,
   check the thread shows it. Any mismatch is a failure, not a retry.

Any LinkedIn warning, captcha, sign-in challenge or limit notice stops the
run and pauses LinkedIn for 48 hours (harvey/linkedin/guards.py). The
dashboard's on/off switch and pause are read again before every send, so
either one stops a run already going.
"""

import asyncio
import json
import logging
import random
import re
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from harvey import channel_login
from harvey.careers import CAREERS_APPROVED, JOB_SEEKER, NOT_A_CLIENT, careers_reply
from harvey.kept_browser import KeptBrowser
from harvey.chat_style import parts as chat_parts, squash, strip_repeat_name
from harvey.meeting_alert import POSITIVE_INTENTS
from harvey.meeting_slot import (
    asks_to_meet, chat_instead_block, confirm_block, confirms_slot, declines_call,
    offer as meeting_offer, offered as offered_slot, offered_link_in, prompt_block, remember_offer,
    still_ahead, wants_next_step,
)
from harvey.site_review import HELD_REASON as REVIEW_HELD_REASON, REVIEW_AUTOPILOT, reviews_autopilot
from harvey.linkedin import guards
from harvey.linkedin.guards import BreakerTripped, active_pause, detect_breaker
from harvey.linkedin.pacing import INVITES_PAUSE_KEY, Pacing, wait_unless
from harvey.linkedin.pages import (
    COMPOSER, CONNECTIONS_URL, CONVERSATION_LIST, FEED_URL, INVITE_DIALOG, INVITE_NOTE_BOX, MESSAGING_URL,
    SEND_BUTTON, SENT_INVITES_URL, clean_text, compose_link_from_profile, invite_confirmed,
    names_match, note_limit_reached, parse_connections, parse_conversation_list,
    parse_sent_invitations, parse_thread, profile_connect_state, profile_headline, profile_location, profile_summary,
    profile_url,
    recipient_confirmed, signed_in_from, summary_headline, summary_location,
)
from harvey.linkedin.session import PageNotLoading
from harvey.linkedin.store import HELD_FOR_REVIEW, MAYBE_SENT, _iso
from harvey.untrusted import UNTRUSTED_RULE, fence

logger = logging.getLogger("harvey.linkedin")

LAST_RUN_KEY = "linkedin:last_run"
# When the last replies-only inbox check started (channels.linkedin.replies_any_time).
INBOX_CHECK_KEY = "linkedin:last_inbox_check"
INBOX_CHECK_MINUTES = 15     # the most a replies-only check runs
RUNNING_KEY = "linkedin:running"
AUTOPILOT_KEY = "linkedin:autopilot"
# A site review was written for this lead (harvey/site_review.py): one each.
REVIEWED_KEY = "linkedin:reviewed:"
# Failed sends to one person before Harvey stops drafting for them.
MAX_FAILED_SENDS = 2
# What their profile page says, read once and kept this long (drafting).
PROFILE_KEY = "linkedin:profile:"
PROFILE_FRESH = timedelta(days=14)
ENABLED_KEY = "linkedin:enabled"
NOTES_OFF_KEY = "linkedin:notes_unavailable_until"
# The model sees this many of the conversation's sent messages.
HISTORY_MESSAGES = 20
# What the web search said about someone it found (harvey/linkedin/finder.py):
# their location and headline, for when their profile page shows neither.
FOUND_KEY = "linkedin:found:"
# Once: people set aside only because their profile couldn't be read get
# another go, now that it can be (2026-11-20).
RETRY_UNREAD_KEY = "linkedin:retry_unread:2026-11-20"
UNREAD_REASONS = ("not invited: their country couldn't be read", "not invited: their role couldn't be read")
# Why an approved item didn't go this run, written on it (never silent).
WAITING = "waiting: "
# Pages that wouldn't load (session.PageNotLoading) before a run stops; and how
# many runs in a row stopped so before your phone hears of it.
MAX_PAGES_NOT_LOADING = 3
NOT_LOADING_KEY = "linkedin:runs_not_loading"
NOT_LOADING_RUNS_TO_TELL = 3
NOT_LOADING = "stopped: LinkedIn pages weren't loading"


def li_catch_up_due(rows: list[dict], cfg, now) -> bool:
    """A conversation that went quiet after Harvey's last message, where they
    had written before: due after catch_up_after_days, at most max_catch_ups
    times since their last message (owner, 2026-11-19)."""
    sent = [m for m in rows if m["status"] == "sent"]
    if not sent or sent[-1]["direction"] != "out":
        return False
    last_in = max((i for i, m in enumerate(sent) if m["direction"] == "in"), default=-1)
    if last_in < 0:
        return False
    since = sent[last_in + 1:]
    if sum(m["kind"] == "catch_up" for m in since) >= getattr(cfg, "max_catch_ups", 2):
        return False
    last_at = _parse(sent[-1].get("sent_at"))
    return bool(last_at) and now - last_at >= timedelta(days=getattr(cfg, "catch_up_after_days", 3))

DECLINE_INTENTS = {"not_interested", "unsubscribe"}
REPLY_INTENTS = {"interested", "question", "objection", "wrong_person"}

# A short "okay, thanks": nothing to answer, and never a no. Live, 2026-11-23:
# "Okay, thanks" to a confirmed meeting was read as not interested and the
# lead was declined. Recorded as ACK: no reply, no decline, the stage stays.
ACK = "ack"
_ACK_WORDS = frozenset("""
    ok okay okey k kk alright allright thanks thank thankyou thx ty tq you so very much many a lot
    again great perfect noted cool got it sounds good will do see then talk soon looking forward to
    appreciate appreciated cheers awesome nice wonderful excellent brilliant lovely fine received
    understood sir madam for the info information update confirmation confirming done super
""".split())
ACK_MAX_WORDS = 8


def is_acknowledgement(text: str) -> bool:
    """Only thanks and okays, at most ACK_MAX_WORDS words, no question: "Okay,
    thanks", "Noted, thank you", "Sounds good". Anything with another word
    ("no", "not", "but", a day, a time) isn't one."""
    t = (text or "").replace("’", "'").lower().strip()
    if not t or "?" in t:
        return False
    words = re.findall(r"[a-z]+", t)
    if not words:
        # Only emoji or punctuation ("👍"), never letters in another script.
        return all(not ch.isalnum() for ch in t)
    return len(words) <= ACK_MAX_WORDS and all(w in _ACK_WORDS for w in words)


def acknowledged(text: str, intent: str, stage: str) -> str:
    """ACK in place of the classifier's intent when the message only says
    thanks and the classifier read it as a no, or the meeting is booked
    already (nothing left to answer). Otherwise the intent as it was."""
    if is_acknowledgement(text) and (intent in DECLINE_INTENTS or stage == "meeting"):
        return ACK
    return intent
# A contact whose email side has answered or stopped: no LinkedIn pitch.
EMAIL_STOPPED = {"replied", "opted_out", "lost", "meeting", "closed", "closed_won", "closed_lost"}
# Email statuses that a LinkedIn conversation stops (they're talking here now).
EMAIL_PITCHABLE = {"new", "queued", "contacted"}
MAX_THREADS_PER_RUN = 12
MAX_DRAFT_ATTEMPTS = 2

_AUTOMATION_RE = re.compile(
    r"\b(?:is this (?:message )?(?:automated|a bot|ai|an ai|real)|are you (?:a bot|an? ai|real|"
    r"a real person|human)|automated (?:message|messages|outreach)|bot\b\??|ai[- ]generated|"
    r"written by (?:ai|a bot|chatgpt)|chat ?gpt|sent by a bot)", re.IGNORECASE)


def asks_if_automated(text: str) -> bool:
    return bool(_AUTOMATION_RE.search(text or ""))


_HONEST_YES_RE = re.compile(r"\b(?:yes|yeah|yep|yup|it is|i do|correct|that's right|true)\b", re.IGNORECASE)
_AI_WORD_RE = re.compile(r"\b(?:ai|a\.i\.|assistant|automat\w*|bot)\b", re.IGNORECASE)
_DENIES_RE = re.compile(
    r"\b(?:not (?:a )?(?:bot|an? ai|automated)|i'?m (?:a )?(?:real|human)(?! person who)|"
    r"100% (?:human|me)|no ai|written by me alone)\b", re.IGNORECASE)


def answers_automation_honestly(body: str) -> bool:
    """A reply to "is this automated?" that says yes, plainly: a yes and the
    AI or the assistant named, and no denial. Such a reply goes on its own
    (owner, 2026-11-20: "not waiting for approval"); any other waits for you."""
    text = (body or "").replace("’", "'")
    return bool(_HONEST_YES_RE.search(text) and _AI_WORD_RE.search(text)) and not _DENIES_RE.search(text)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _parse(value) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "")).replace(tzinfo=None)
    except ValueError:
        return None


async def autopilot_on(state, config) -> bool:
    """LinkedIn autopilot: the dashboard's switch once used, else the config."""
    value = (await state.get_setting(AUTOPILOT_KEY) or "").strip().lower()
    if value in ("on", "off"):
        return value == "on"
    return bool(config.channels.linkedin.autopilot)


async def channel_on(state, config) -> bool:
    """LinkedIn on or off: the dashboard's switch once used, else the config.
    Read every cycle, since the heartbeat only reads its config at startup.
    Autopilot means Harvey runs LinkedIn by itself, day after day (owner,
    2026-11-08): with autopilot on it is on, unless you switched it off."""
    value = (await state.get_setting(ENABLED_KEY) or "").strip().lower()
    if value in ("on", "off"):
        return value == "on"
    return bool(config.channels.linkedin.enabled) or await autopilot_on(state, config)


class InviteLimitReached(Exception):
    """LinkedIn's weekly invite limit, seen while inviting: invites stop for now."""


class SwitchedOff(Exception):
    """The channel was switched off during a run: stop it where it is."""


async def said_no(state, config, lead: dict, entries=None) -> str:
    """Why this lead must not get another message from anyone here, you included,
    or "": a no on any channel, or the do-not-contact list. ``entries``: that list,
    when the caller already has it."""
    from harvey.config import live_do_not_contact
    from harvey.gate import do_not_contact_domain_match, do_not_contact_match
    from harvey.whatsapp.contacts import SAID_NO
    if lead.get("stage") == "declined":
        return "they said no on LinkedIn"
    entries = live_do_not_contact(config) if entries is None else entries
    prospect = await state.get_prospect(lead["prospect_id"]) if lead.get("prospect_id") else None
    if prospect is not None:
        if prospect.status == "opted_out":
            return "they opted out"
        if prospect.status in SAID_NO:
            return "they said no by email"
        if prospect.email and do_not_contact_match(prospect.email, entries):
            return "their address is on your do-not-contact list"
    company_id = lead.get("company_id") or (prospect.company_id if prospect else "")
    company = await state.get_company(company_id) if company_id else None
    if company and company.domain and do_not_contact_domain_match(company.domain, entries):
        return "their company is on your do-not-contact list"
    return ""


def _clean_draft(text: str) -> str:
    text = (text or "").strip().strip('"').strip()
    text = re.sub(r"^(?:message|note|reply)\s*:\s*", "", text, flags=re.IGNORECASE)
    text = re.sub(r"\s*[—–]\s*", ", ", text)       # no dashes
    text = re.sub(r"[\U0001F300-\U0001FAFF☀-➿]", "", text)  # no emojis
    text = re.sub(r"[ \t]+", " ", text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def fit_note(text: str, limit: int) -> str:
    """A connection note within LinkedIn's limit: whole sentences, else whole words."""
    text = " ".join((text or "").split())
    if len(text) <= limit:
        return text
    cut = text[:limit]
    end = max(cut.rfind(". "), cut.rfind("? "), cut.rfind("! "))
    if end >= limit // 2:
        return cut[:end + 1]
    if cut.endswith((".", "?")):
        return cut
    return cut.rsplit(" ", 1)[0].rstrip(",;:") if " " in cut else ""


class LinkedInAgent:
    def __init__(self, brain, state, config, *, driver_factory=None, pacing=None,
                 now=None, sleep=None, rng=None, env=None):
        self.brain = brain
        self.state = state
        self.config = config
        self.env = env        # verifier keys for a referral's address (loaded when needed)
        self.cfg = config.channels.linkedin
        self.rng = rng or random.Random()
        self._now = now or (lambda: datetime.now(timezone.utc))
        self.pacing = pacing or Pacing(state, self.cfg, now=self._now, rng=self.rng, sleep=sleep)
        if driver_factory is None:
            from harvey.linkedin.session import open_session
            driver_factory = lambda: open_session(self.cfg)  # noqa: E731
        self.driver_factory = driver_factory
        self.driver = None
        self.dry_run = False
        self.deadline = None
        self.report: dict = {}
        self._failed_leads: set[str] = set()
        self._waiting: dict[str, int] = {}
        self._in_break = False

    def now(self) -> datetime:
        """Naive UTC, as stored."""
        return self._now().astimezone(timezone.utc).replace(tzinfo=None)

    # ── When it may run ──

    async def why_not_now(self, ignore_interval: bool = False, replies_only: bool = False) -> str:
        """Why a run can't start now, or "". ``replies_only``: the short inbox
        check channels.linkedin.replies_any_time allows at any hour."""
        if not await channel_on(self.state, self.config):
            return "LinkedIn is off (the switch on the dashboard's LinkedIn tab, or channels.linkedin.enabled)"
        pause = await active_pause(self.state, self.now())
        if pause:
            return "LinkedIn is paused: " + str(pause.get("reason") or "paused")
        if await channel_login.in_progress(self.state, "linkedin"):
            return "a LinkedIn sign-in is waiting, and its Chrome window holds the profile"
        if replies_only and not getattr(self.cfg, "replies_any_time", False):
            return "replies at any hour are off (channels.linkedin.replies_any_time)"
        if not replies_only and not self.pacing.in_hours():
            return f"outside LinkedIn hours ({self.pacing.hours_text()})"
        running = _parse(await self.state.get_setting(RUNNING_KEY))
        if running and self.now() - running < timedelta(minutes=self.cfg.max_run_minutes + 15):
            return "a LinkedIn run is already going"
        if replies_only and not ignore_interval:
            checked = _parse(await self.state.get_setting(INBOX_CHECK_KEY))
            every = max(1, int(getattr(self.cfg, "reply_check_minutes", 10) or 10))
            if checked and self.now() - checked < timedelta(minutes=every):
                return (f"the inbox was checked at {checked:%H:%M} UTC; the next check "
                        f"waits {every} minutes")
        elif not ignore_interval:
            last = await self.last_run()
            started = _parse(last.get("started"))
            if started and self.now() - started < timedelta(minutes=self.cfg.run_every_minutes):
                return (f"the last LinkedIn run started at {started:%H:%M} UTC; the next one "
                        f"waits {self.cfg.run_every_minutes} minutes")
        return ""

    async def last_run(self) -> dict:
        try:
            data = json.loads(await self.state.get_setting(LAST_RUN_KEY) or "{}")
        except ValueError:
            data = {}
        return data if isinstance(data, dict) else {}

    # ── A run ──

    async def run(self, dry_run: bool = False, ignore_interval: bool = False,
                  replies_only: bool = False) -> dict:
        reason = await self.why_not_now(ignore_interval, replies_only=replies_only)
        if reason:
            return {"ran": False, "reason": reason}
        self.dry_run = dry_run
        self.replies_only = replies_only
        self._failed_leads = set()
        self._waiting = {}
        self._not_loading = 0
        started = self.now()
        self.deadline = started + timedelta(
            minutes=INBOX_CHECK_MINUTES if replies_only else self.cfg.max_run_minutes)
        self.report = {"ran": True, "dry_run": dry_run, "started": _iso(started), "outcome": "",
                       "inbound": 0, "accepted": 0, "withdrawn": 0, "declined": 0, "drafted": 0,
                       "invite_drafts": 0, "sent_messages": 0, "sent_invites": 0, "failed": 0,
                       "would_send": []}
        await self.state.set_setting(RUNNING_KEY, _iso(started))
        self._in_break = False
        # Every run reads the inbox first; a replies-only check doesn't push the
        # next full run back.
        await self.state.set_setting(INBOX_CHECK_KEY, _iso(started))
        if not replies_only:
            await self.state.set_setting(LAST_RUN_KEY, json.dumps(
                {"started": _iso(started), "finished": None, "outcome": "running"}))
        try:
            async with self.driver_factory() as driver:
                self.driver = driver
                await self._steps()
            self.report["outcome"] = "done"
        except BreakerTripped as e:
            self.report["outcome"] = "stopped: " + e.breaker.reason
            logger.warning(f"LinkedIn: stopped and paused: {e.breaker.reason}")
        except SwitchedOff:
            self.report["outcome"] = "stopped: switched off"
            logger.info("LinkedIn: switched off, so the run stopped.")
        except PageNotLoading as e:
            pages = max(1, getattr(self, "_not_loading", 0))
            self.report["outcome"] = f"{NOT_LOADING} ({pages} timed out); the next run tries again"
            logger.warning(f"LinkedIn: {e}; the run stopped and the next one tries again.")
        except Exception as e:
            self.report["outcome"] = f"error: {e}"
            logger.error(f"LinkedIn run failed: {e}", exc_info=True)
        finally:
            self.driver = None
            self.report["finished"] = _iso(self.now())
            # Pages that didn't load once in a while are LinkedIn being slow:
            # your phone hears only when it lasts several runs (owner, 2026-11-20).
            try:
                if str(self.report.get("outcome", "")).startswith(NOT_LOADING):
                    runs = int(await self.state.get_setting(NOT_LOADING_KEY) or 0) + 1
                    await self.state.set_setting(NOT_LOADING_KEY, str(runs))
                    self.report["runs_not_loading"] = runs
                elif str(self.report.get("outcome", "")) == "done":
                    await self.state.set_setting(NOT_LOADING_KEY, "0")
            except Exception:
                pass
            if self._waiting:
                self.report["waiting"] = dict(self._waiting)
                logger.info("LinkedIn: approved but not sent this run: " + "; ".join(
                    f"{n} ({why})" for why, n in self._waiting.items()))
            await self.state.set_setting(RUNNING_KEY, "")
            if not replies_only:
                await self.state.set_setting(LAST_RUN_KEY, json.dumps(
                    {k: v for k, v in self.report.items() if k != "would_send"}))
            busy = (self.report.get("inbound") or self.report.get("sent_messages")
                    or not str(self.report.get("outcome", "")).startswith("done"))
            if not replies_only or busy:
                try:
                    await self.state.log_action(
                        "linkedin_inbox_check" if replies_only else "linkedin_run", "linkedin",
                        {k: v for k, v in self.report.items()})
                except Exception:
                    pass
        return self.report

    async def _steps(self):
        await self.pacing.prune()
        await self._check_signed_in()
        # Replies first (owner, 2026-11-20: "without waiting hours to send
        # messages"): what's ready to answer goes before any drafting or invites,
        # and the inbox is read again every reply_check_minutes during the run.
        steps = ((self.sync_inbox, self.handle_inbound, self.release_held, self.send_approved)
                 if getattr(self, "replies_only", False) else
                 (self.retry_unread, self.sync_inbox, self.handle_inbound, self.release_held,
                  self.send_replies, self.sync_connections, self.sync_sent_invites,
                  self.draft_messages, self.find_people, self.pick_invites, self.send_approved))
        for step in steps:
            await self._still_on()
            try:
                await step()
            except PageNotLoading as e:
                self._page_not_loading(e, f"the rest of {step.__name__.strip('_').replace('_', ' ')}")
        await self._still_on()      # stopped during the last step: say so, not "done"

    async def _inbox_break(self):
        """During a long full run: when reply_check_minutes have passed since the
        inbox was last read, read it, answer what came in and send the replies
        that are ready, then carry on."""
        if (getattr(self, "replies_only", False) or getattr(self, "_in_break", False)
                or self.dry_run or self.driver is None):
            return
        every = max(1, int(getattr(self.cfg, "reply_check_minutes", 10) or 10))
        checked = _parse(await self.state.get_setting(INBOX_CHECK_KEY))
        if checked and self.now() - checked < timedelta(minutes=every):
            return
        self._in_break = True
        try:
            await self.state.set_setting(INBOX_CHECK_KEY, _iso(self.now()))
            await self.sync_inbox()
            await self.handle_inbound()
            await self.release_held()
            await self.send_approved(only=("reply",))
            self.report["inbox_breaks"] = self.report.get("inbox_breaks", 0) + 1
        finally:
            self._in_break = False

    async def send_replies(self):
        await self.send_approved(only=("reply",))

    async def retry_unread(self):
        """Once: people set aside only because their profile couldn't be read
        (LinkedIn's page had changed) are invited again, now that it can be."""
        if self.dry_run or await self.state.get_setting(RETRY_UNREAD_KEY):
            return
        rows = await self.state._li_query(
            "SELECT id FROM linkedin_leads WHERE stage = 'found' AND ("
            + " OR ".join("no_message_reason LIKE ?" for _ in UNREAD_REASONS) + ")",
            tuple(r + "%" for r in UNREAD_REASONS))
        for row in rows:
            await self.state.li_update_lead(row["id"], no_message_reason="")
        await self.state.set_setting(RETRY_UNREAD_KEY, json.dumps(
            {"at": self.now().isoformat(), "leads": len(rows)}))
        if rows:
            logger.info(f"LinkedIn: {len(rows)} people whose profile couldn't be read get another go.")

    def _time_left(self) -> bool:
        return self.deadline is None or self.now() < self.deadline

    def _page_not_loading(self, error: Exception, skipped: str):
        """A page that wouldn't load: that bit waits for the next run, and the
        run carries on, unless pages keep failing (then it stops)."""
        self._not_loading = getattr(self, "_not_loading", 0) + 1
        self.report["pages_not_loading"] = self._not_loading
        logger.info(f"LinkedIn: {error}; {skipped} waits for the next run.")
        if self._not_loading >= MAX_PAGES_NOT_LOADING:
            raise PageNotLoading(str(error))

    async def _note_waiting(self, item: dict, why: str):
        """Why an approved item didn't go this time, on the item (the tab shows
        it). Written only when it changes."""
        note = (WAITING + why)[:300]
        if item.get("status") == "approved" and (item.get("error") or "") != note:
            # Only while it is still approved: a reply sent meanwhile keeps no note.
            async with self.state._connect() as db:
                await db.execute("UPDATE linkedin_messages SET error = ? WHERE id = ? AND status = 'approved'",
                                 (note, item["id"]))
                await db.commit()
            item["error"] = note
        self._waiting[why] = self._waiting.get(why, 0) + 1

    async def _still_on(self):
        """The switch works mid-run, not just at the start of one."""
        if not await channel_on(self.state, self.config):
            raise SwitchedOff()

    async def _held(self) -> bool:
        """Switched off or paused, this moment."""
        return not await channel_on(self.state, self.config) or bool(
            await active_pause(self.state, self.now()))

    async def _wait_between_actions(self):
        """The wait between actions, which ends early if the channel is stopped."""
        await wait_unless(self.pacing.between_actions(), self._held)

    async def _guard(self, html: str):
        breaker = detect_breaker(self.driver.url, html)
        if breaker and breaker.kind == "invite_limit":
            # LinkedIn's weekly invite limit: invites wait, everything else goes
            # on (owner, 2026-11-19: replies must never stop for it).
            if not await self.pacing.invites_paused_until():
                until = self.now() + timedelta(hours=self.cfg.pause_hours_on_warning)
                await self.state.set_setting(INVITES_PAUSE_KEY, _iso(until))
                logger.warning(f"LinkedIn: weekly invite limit reached; invites wait until "
                               f"{until:%Y-%m-%d %H:%M} UTC, messages and replies carry on.")
                try:
                    await self.state.log_action("linkedin_invite_limit", "linkedin",
                                                {"until": _iso(until)})
                except Exception:
                    pass
            if getattr(self, "_inviting", False):
                raise InviteLimitReached(breaker.reason)
            return
        if breaker:
            await guards.trip(self.state, breaker, hours=self.cfg.pause_hours_on_warning)
            raise BreakerTripped(breaker)

    async def _visit(self, url: str, pause: bool = True) -> str:
        if pause:
            await self.pacing.between_pages()
        await self._still_on()
        await self.driver.goto(url)
        html = await self.driver.html()
        await self._guard(html)
        return html

    async def _check_signed_in(self):
        html = await self._visit(FEED_URL, pause=False)
        from harvey.linkedin.session import has_session_cookie
        if not signed_in_from(self.driver.url, html) and not await has_session_cookie(self.driver):
            breaker = guards.Breaker("signed_out", guards.REASONS["signed_out"], self.driver.url)
            await guards.trip(self.state, breaker)
            raise BreakerTripped(breaker)

    # ── 1. Sync ──

    async def sync_inbox(self):
        html = await self._visit(MESSAGING_URL)
        await self.driver.scroll(CONVERSATION_LIST, 2)
        html = await self.driver.html()
        await self._guard(html)
        leads = await self.state.li_leads()
        opened = 0
        for card in parse_conversation_list(html):
            if card.we_sent_last or not self._time_left():
                continue
            matches = [lead for lead in leads if names_match(card.name, lead.get("name") or "")]
            if len(matches) != 1:
                continue          # not one of ours, or two leads share the name
            lead = matches[0]
            known = [clean_text(m["body"]) for m in
                     await self.state.li_messages(lead_id=lead["id"], direction="in")]
            snippet = clean_text(card.snippet)
            if snippet and any(k.startswith(snippet[:60]) for k in known):
                continue
            url = card.thread_url or lead.get("thread_url") or ""
            if not url or opened >= MAX_THREADS_PER_RUN:
                continue
            opened += 1
            thread_html = await self._visit(url)
            await self.record_thread(lead, parse_thread(thread_html), self.driver.url or url)

    async def record_thread(self, lead: dict, thread, url: str) -> int:
        """Store what the thread shows that Harvey hasn't recorded: their
        messages (the lead moves to replied) and any you sent by hand."""
        mine = await self.state.li_messages(lead_id=lead["id"])
        known_in = {clean_text(m["body"]) for m in mine if m["direction"] == "in"}
        known_out = set()
        for m in mine:
            if m["direction"] == "out":
                known_out.add(clean_text(m["body"]))
                known_out.update(clean_text(p) for p in chat_parts(m["body"]))
                known_out.add(squash(m["body"]))
                known_out.update(squash(p) for p in chat_parts(m["body"]))
        base = self.now() - timedelta(seconds=len(thread.messages) + 1)
        new_in = 0
        for i, message in enumerate(thread.messages):
            body = clean_text(message.body)
            at = base + timedelta(seconds=i + 1)
            if names_match(message.sender, lead.get("name") or ""):
                if body in known_in:
                    continue
                known_in.add(body)
                if await self.state.li_add_message(lead["id"], direction="in", kind="message",
                                                   body=message.body, status="sent",
                                                   sent_at=at, thread_url=url):
                    new_in += 1
            elif message.sender and body not in known_out and squash(body) not in known_out and not any(
                    k.startswith(body[:60]) or body.startswith(k[:60]) for k in known_out if k):
                known_out.add(body)
                await self.state.li_add_message(lead["id"], direction="out", kind="reply",
                                                body=message.body, status="sent", sent_at=at,
                                                thread_url=url, approved_by="you on LinkedIn")
        if new_in:
            self.report["inbound"] = self.report.get("inbound", 0) + new_in
            await self.state.li_set_stage(lead["id"], "replied", last_inbound_at=self.now(),
                                          thread_url=url)
        return new_in

    async def sync_connections(self):
        html = await self._visit(CONNECTIONS_URL)
        await self.driver.scroll("main", 3)
        html = await self.driver.html()
        await self._guard(html)
        for conn in parse_connections(html):
            lead = await self.state.li_get_lead_by_slug(conn.slug)
            if not lead or lead["stage"] not in ("found", "invited", "withdrawn"):
                continue
            at = (datetime.combine(conn.connected_on, datetime.min.time())
                  if conn.connected_on else self.now())
            wait = self.rng.uniform(self.cfg.first_message_min_days, self.cfg.first_message_max_days)
            await self.state.li_set_stage(lead["id"], "accepted", accepted_at=at,
                                          next_action_at=max(at, self.now() - timedelta(days=30))
                                          + timedelta(days=wait))
            self.report["accepted"] += 1

    async def sync_sent_invites(self):
        html = await self._visit(SENT_INVITES_URL)
        await self.driver.scroll("main", 3)
        html = await self.driver.html()
        await self._guard(html)
        old = []
        for invite in parse_sent_invitations(html):
            lead = await self.state.li_get_lead_by_slug(invite.slug)
            if not lead:
                continue
            if lead["stage"] == "found":
                await self.state.li_set_stage(lead["id"], "invited")
            if (invite.age_days or 0) >= self.cfg.withdraw_after_days and lead["stage"] in (
                    "found", "invited"):
                old.append((invite, lead))
        for invite, lead in old:
            if self.dry_run:
                self.report["would_send"].append({"kind": "withdraw", "lead": lead.get("name")})
                continue
            if not self._time_left() or not self.pacing.in_hours() or \
                    await self.pacing.blocked("withdraw"):
                break
            await self._wait_between_actions()
            await self._still_on()
            if await active_pause(self.state, self.now()):
                break
            anchor = f"a[href*='/in/{invite.slug}']"
            if not await self.driver.click_button_near(anchor, r"^withdraw"):
                continue
            await self.driver.click_button(r"^withdraw$", within="[role='dialog']")
            await self._guard(await self.driver.html())
            await self.pacing.record("withdraw")
            await self.state.li_set_stage(lead["id"], "withdrawn")
            self.report["withdrawn"] += 1

    # ── 2. Replies ──

    def _as_prospect(self, lead: dict):
        name = lead.get("name") or lead.get("first_name") or "them"
        return SimpleNamespace(full_name=lambda: name, title=lead.get("headline") or "",
                               company=lead.get("company") or "",
                               company_id=lead.get("company_id") or "")

    async def classify(self, text: str, lead: dict) -> str:
        """The Handler's rules: opt-out phrases and escalation words first
        (they override the model), then its model classifier."""
        from harvey.agents.handler import ESCALATION_PATTERNS, Handler, _flat, _opt_out_reason
        if _opt_out_reason(text, text):
            return "unsubscribe"
        if any(p in _flat(text) for p in ESCALATION_PATTERNS):
            return "escalate"
        intent = await Handler._classify_intent(self, text, self._as_prospect(lead))
        return acknowledged(text, intent, lead.get("stage") or "")

    async def handle_inbound(self):
        if self._brain_paused():
            return
        unread = [m for m in await self.state.li_messages(direction="in") if not m["intent"]]
        latest: dict[str, dict] = {}
        for message in unread:
            lead = await self.state.li_get_lead(message["lead_id"])
            if lead is None:
                continue
            try:
                intent = await self.classify(message["body"], lead)
            except Exception as e:
                if self._brain_paused():
                    return
                logger.warning(f"LinkedIn: couldn't classify a reply ({e}); it waits for you.")
                intent = "escalate"
            if intent == ACK and lead["stage"] != "meeting":
                # An "okay, thanks" to the time we offered books it.
                history = await self.state.li_messages(lead_id=lead["id"])
                if await self.booking_confirmed(lead, message["body"], history) is not None:
                    intent = "interested"
            # A "wrong person" that names someone: the referral (before the
            # message counts as read, so an outage leaves it for next time).
            referred = {"note": "", "for_you": False, "keep": set()}
            if intent == "wrong_person" and lead["stage"] != "declined" and not self.dry_run:
                try:
                    referred = await self.referral_from(lead, message["body"])
                except Exception as e:
                    if self._brain_paused():
                        return
                    logger.warning(f"LinkedIn: couldn't read who they pointed to ({e}).")
            await self.state.li_update_message(message["id"], intent=intent)
            if intent in DECLINE_INTENTS:
                await self.decline(lead, message["body"], intent)
                latest.pop(lead["id"], None)
                continue
            if lead["stage"] == "declined":
                continue
            if intent in NOT_A_CLIENT:
                await self.not_a_client(lead, intent, thread_url=message.get("thread_url") or "")
                latest.pop(lead["id"], None)
                continue
            if intent == "interested" and lead["stage"] != "meeting":
                await self.state.li_set_stage(lead["id"], "warm")
            if intent in REPLY_INTENTS or intent == "escalate":
                await self.stop_email_pitch(lead, keep=referred["keep"])
            if intent in POSITIVE_INTENTS and not self.dry_run:
                await self.meeting_alert(lead, message["body"])
            latest[lead["id"]] = {**message, "intent": intent, "referral_note": referred["note"],
                                  "for_you": referred["for_you"]}
        for lead_id, message in latest.items():
            if message["intent"] not in REPLY_INTENTS or not self._time_left():
                continue
            if message.get("for_you"):
                continue          # they pointed outside the company: yours to answer
            lead = await self.state.li_get_lead(lead_id)
            history = await self.state.li_messages(lead_id=lead_id)
            if not history or history[-1]["direction"] != "in":
                continue          # answered already (by you, on LinkedIn)
            if await self.state.li_pending_for(lead_id, kinds=("reply",)):
                continue
            hold = []
            # A yes to the time we offered: the meeting is booked (stage, your phone).
            booked = await self.booking_confirmed(lead, message["body"], history)
            if booked is None and await self.wants_site_review(message["intent"], lead, history):
                # A yes to the write-up we offered: the reply is the review.
                review = await self.site_review(lead, message["body"])
                if review.body:
                    await self.add_review(lead, review.body, thread_url=message.get("thread_url") or "")
                    continue
                hold.append(f"they said yes to the site review, and Harvey couldn't write it: "
                            f"{review.why}")
                # Said once on Today and on your phone (harvey/needs_you.py, owner_updates).
                try:
                    await self.state.log_action("site_review", "linkedin", {
                        "ok": False, "why": review.why, "channel": "linkedin",
                        "name": lead.get("name") or lead.get("first_name") or ""})
                except Exception:
                    pass
            if booked is not None and not self.dry_run:
                await self.book_meeting(lead, booked)
            body = await self.draft(lead, "reply", intent=message["intent"], history=history,
                                    note=message.get("referral_note") or "", booked=booked)
            if body:
                # An honest yes goes on its own; anything else waits for you.
                if asks_if_automated(message["body"]) and not answers_automation_honestly(body):
                    hold.append("they asked whether this is automated")
                await self.add_draft(lead, "reply", body, hold=hold,
                                     thread_url=message.get("thread_url") or "")

    async def booking_confirmed(self, lead: dict, latest: str, history) -> str | None:
        """The time they said yes to, when our last message offered the
        meeting link and their answer confirms it (meeting_slot.confirms_slot);
        None otherwise. An offer whose time has passed isn't confirmed."""
        ours = [m["body"] for m in history or [] if m["direction"] == "out" and m["status"] == "sent"]
        if not ours or not offered_link_in(ours[-1:], self.config):
            return None
        offer = await offered_slot(self.state, "linkedin:" + lead["id"])
        if offer and not still_ahead(offer, self._now()):
            return None
        if not confirms_slot(latest, offer.get("text", "")):
            return None
        return offer.get("text") or ""

    async def book_meeting(self, lead: dict, time_text: str):
        """They confirmed: the lead moves to Meeting, and your phone hears it once."""
        await self.state.li_set_stage(lead["id"], "meeting")
        key = "meeting:booked:linkedin:" + lead["id"]
        if await self.state.get_setting(key):
            return
        await self.state.set_setting(key, self.now().isoformat())
        self.report["meetings_booked"] = self.report.get("meetings_booked", 0) + 1
        try:
            await self.state.log_action("meeting_booked", "linkedin", {
                "name": lead.get("name") or lead.get("first_name") or "", "company": lead.get("company") or "",
                "time": time_text, "channel": "LinkedIn", "linkedin_lead_id": lead["id"]})
        except Exception:
            pass
        logger.info(f"LinkedIn: meeting booked with {lead.get('name') or 'a lead'} ({time_text or 'the time offered'}).")

    async def decline(self, lead: dict, text: str, intent: str):
        """A no: the lead is declined for good, nothing queued goes, and their
        email side stops too (opted out, company stopped)."""
        await self.state.li_set_stage(lead["id"], "declined")
        for m in await self.state.li_pending_for(lead["id"]):
            await self.state.li_skip(m["id"])
        self.report["declined"] = self.report.get("declined", 0) + 1
        prospect_id = lead.get("prospect_id") or ""
        if prospect_id:
            await self.state.update_prospect_status(prospect_id, "opted_out")
            await self.state.cancel_pending_outbox_for_prospect(prospect_id, "said no on LinkedIn")
            await self.state.stop_company(prospect_id)
            await self.state.log_action("opt_out", "linkedin", {
                "prospect_id": prospect_id, "rule": "linkedin", "matched": intent,
                "why": "They said no on LinkedIn.", "message": (text or "")[:1000],
                "linkedin_lead_id": lead["id"]})

    async def meeting_slot(self, lead: dict):
        """The owner's next meeting window, in their time when their email
        contact says where they are (never a guess), or None."""
        from harvey.send_window import recipient_zone, zone_for
        zone = ""
        try:
            if lead.get("prospect_id"):
                prospect = await self.state.get_prospect(lead["prospect_id"])
                zone = await recipient_zone(self.state, prospect, None) if prospect else ""
        except Exception:
            zone = ""
        if not zone:
            # Their LinkedIn location ("London Area, United Kingdom"), when read.
            try:
                location = (await self._profile_cache(lead)).get("location") or ""
                zone = zone_for(location=location) if location else ""
            except Exception:
                zone = ""
        return meeting_offer(self.config, now=self._now(), their_zone=zone or "")

    async def meeting_alert(self, lead: dict, latest: str) -> None:
        """Close to a meeting: a WhatsApp message to your phone
        (harvey/meeting_alert.py). Never raises."""
        from harvey.meeting_alert import maybe_alert
        name = lead.get("name") or lead.get("first_name") or lead.get("slug") or ""
        history = await self.state.li_messages(lead_id=lead["id"])
        thread = "\n\n".join(f"{'Them' if m['direction'] == 'in' else 'Us'}: {m['body']}"
                             for m in history[-14:] if m["status"] == "sent" and m["body"])
        prospect = await self.state.get_prospect(lead["prospect_id"]) if lead.get("prospect_id") else None
        slot = await self.meeting_slot(lead) if asks_to_meet(latest) else None
        await maybe_alert(
            self.state, self.brain, self.config, channel="LinkedIn", key=f"linkedin:{lead['id']}",
            name=name, company=lead.get("company") or "", latest=latest, thread=thread,
            contacts=[lead.get("profile_url") or "", getattr(prospect, "email", "") or ""],
            where=f"Harvey dashboard, LinkedIn tab, {name}", notes=lead.get("headline") or "",
            offered=slot.text() if slot is not None else "")

    async def wants_site_review(self, intent: str, lead: dict, history) -> bool:
        """A yes to the write-up one of our messages offered, and no review
        written for them yet (harvey/site_review.py)."""
        from harvey.site_review import REVIEW_INTENTS, offered_review
        if (intent not in REVIEW_INTENTS or self.dry_run
                or not getattr(getattr(self.config, "site_review", None), "enabled", False)):
            return False
        ours = [m["body"] for m in history or [] if m["direction"] == "out" and m["status"] == "sent"]
        # A yes answers what we said last (2026-11-20: "yes, that works" to the
        # meeting time was read as a yes to a review offered days before, and
        # the confirmation waited a day for you). The offer must be in our last
        # two messages, and the last one mustn't be the meeting time.
        if offered_link_in(ours[-1:], self.config) or not offered_review(ours[-2:]):
            return False
        return not await self.state.get_setting(REVIEWED_KEY + lead["id"])

    async def site_review(self, lead: dict, their_words: str):
        from harvey.site_review import make_review, site_for
        prospect = await self.state.get_prospect(lead["prospect_id"]) if lead.get("prospect_id") else None
        site = await site_for(self.state, prospect=prospect, company_id=lead.get("company_id") or "")
        name = lead.get("first_name") or (lead.get("name") or "").split(" ")[0]
        return await make_review(self.state, self.brain, self.config, getattr(self, "env", None),
                                 site=site, name=name, their_words=their_words, channel="linkedin")

    async def _own_sites(self, lead: dict) -> tuple[str, ...]:
        """The lead's own website, when known: a message may name it to them.
        A site their own LinkedIn profile shows counts too (2026-11-20: drafts
        naming the site from their profile were held as "not one of your
        domains")."""
        sites: list[str] = []
        try:
            from harvey.site_review import site_for
            prospect = (await self.state.get_prospect(lead["prospect_id"])
                        if lead.get("prospect_id") else None)
            site = await site_for(self.state, prospect=prospect, company_id=lead.get("company_id") or "")
            if site:
                sites.append(site)
        except Exception:
            pass
        try:
            from harvey.gate import find_links
            text = (await self._profile_cache(lead)).get("text") or ""
            sites += [host for host, _ in find_links(text)]
        except Exception:
            pass
        return tuple(dict.fromkeys(s for s in sites if s))

    async def add_review(self, lead: dict, body: str, thread_url: str = "") -> str | None:
        """A review waits for you, unless reviews autopilot is on (then the
        autopilot review for planted links still applies)."""
        from harvey.gate import autopilot_review
        from harvey.site_review import HELD_REASON, REVIEW_AUTOPILOT, reviews_autopilot, tidy
        body = _clean_draft(tidy(body)) or body
        status, approved_by, error = "draft", "", HELD_FOR_REVIEW + HELD_REASON
        if await reviews_autopilot(self.state):
            reasons = autopilot_review("", body, self.config, own_sites=await self._own_sites(lead))
            if reasons:
                error = HELD_FOR_REVIEW + "; ".join(reasons)
            else:
                status, approved_by, error = "approved", REVIEW_AUTOPILOT, ""
        message_id = await self.state.li_add_message(
            lead["id"], direction="out", kind="reply", body=body, status=status, error=error[:300],
            approved_by=approved_by, thread_url=thread_url, dedupe=False)
        await self.state.set_setting(REVIEWED_KEY + lead["id"], self.now().isoformat())
        self.report["drafted"] = self.report.get("drafted", 0) + 1
        return message_id

    async def not_a_client(self, lead: dict, intent: str, thread_url: str = ""):
        """A job seeker or someone selling to us (harvey/careers.py). A job
        seeker gets the one fixed reply pointing to HR, approved at once (our
        text, not a model's); LinkedIn stops for them once it has gone
        (send_approved). Someone selling gets nothing and stops now."""
        for m in await self.state.li_pending_for(lead["id"]):
            await self.state.li_skip(m["id"])
        await self.stop_email_pitch(lead)
        body = careers_reply(self.config, lead.get("first_name") or (lead.get("name") or "").split(" ")[0]) \
            if intent == JOB_SEEKER else ""
        if not body:
            await self.stop_for_good(lead)
            return
        if any(m["approved_by"] == CAREERS_APPROVED and m["status"] in ("approved", "sent")
               for m in await self.state.li_messages(lead_id=lead["id"])):
            return
        await self.state.li_add_message(
            lead["id"], direction="out", kind="reply", body=body,
            status="draft" if self.dry_run else "approved",
            approved_by="" if self.dry_run else CAREERS_APPROVED,
            thread_url=thread_url, dedupe=False)
        self.report["drafted"] = self.report.get("drafted", 0) + 1

    async def stop_for_good(self, lead: dict):
        """Not a client: declined on LinkedIn, and their email side lost."""
        await self.state.li_set_stage(lead["id"], "declined")
        for m in await self.state.li_pending_for(lead["id"]):
            await self.state.li_skip(m["id"])
        self.report["declined"] = self.report.get("declined", 0) + 1
        prospect_id = lead.get("prospect_id") or ""
        prospect = await self.state.get_prospect(prospect_id) if prospect_id else None
        if prospect is not None and prospect.status in (*EMAIL_PITCHABLE, "replied"):
            await self.state.update_prospect_status(prospect_id, "lost")

    async def stop_email_pitch(self, lead: dict, keep=()):
        """They're talking on LinkedIn: no cold email pitch on top of it.
        ``keep``: the colleague they named (a referral), whom the company
        stop leaves alone."""
        prospect_id = lead.get("prospect_id") or ""
        if not prospect_id:
            return
        prospect = await self.state.get_prospect(prospect_id)
        if prospect is None or prospect.status not in EMAIL_PITCHABLE:
            return
        await self.state.update_prospect_status(prospect_id, "replied")
        await self.state.cancel_pending_outbox_for_prospect(prospect_id, "talking on LinkedIn")
        await self.state.stop_company(prospect_id, keep=keep)

    async def referral_from(self, lead: dict, text: str) -> dict:
        """A "wrong person" reply on LinkedIn that names someone
        (harvey/referrals.py). An address at the company's own domain becomes
        the same email referral as one by email (first email held for you); a
        LinkedIn profile becomes a lead at stage found whose invite waits for
        your approval; anyone outside the company is yours to answer.
        Returns {"note": what the thank-you says, "for_you": no reply drafted,
        "keep": the referral's prospect id, for the company stop}."""
        from harvey.config import live_do_not_contact
        from harvey.gate import do_not_contact_match
        from harvey.integrations.mail_provider import NATIVE_PROVIDERS
        from harvey.linkedin.pages import profile_url
        from harvey.mailboxes import configured_mailboxes, is_multi
        from harvey import referrals as ref

        out = {"note": "", "for_you": False, "keep": set()}
        prospect = (await self.state.get_prospect(lead["prospect_id"])
                    if lead.get("prospect_id") else None)
        company_id = lead.get("company_id") or (prospect.company_id if prospect else "")
        company = await self.state.get_company(company_id) if company_id else None
        if company is None:
            company = SimpleNamespace(id=company_id or "", name=lead.get("company") or "",
                                      domain="", location="", description="", industry="")
        mine = {m.email for m in configured_mailboxes(self.config) if m.email}
        if getattr(self.config.persona, "email", ""):
            mine.add(self.config.persona.email.strip().lower())
        own = {a.rsplit("@", 1)[-1] for a in mine if "@" in a}
        own |= set(getattr(self.config.channels.email, "link_domains", None) or [])
        own |= {ref.registrable_domain(d) for d in own}
        referrer_name = lead.get("name") or ""
        referral = await ref.find_referral(
            self.brain, text,
            domains=ref.company_domains(prospect.email if prospect else "", company.domain or ""),
            exclude={*mine, prospect.email if prospect else ""}, own_domains=own,
            company=company.name or "", linkedin_exclude={lead.get("slug") or ""},
            exclude_names={referrer_name})
        email = self.config.channels.email
        email_native = bool(getattr(email, "enabled", True)) and email.provider in NATIVE_PROVIDERS
        first = (lead.get("first_name") or referrer_name.split(" ")[0]).strip()

        if referral.kind == ref.ADDRESS and email_native:
            if do_not_contact_match(referral.email, live_do_not_contact(self.config)):
                referral.kind, referral.why = ref.OUTSIDE, f"{referral.email} is on your do-not-contact list"
            else:
                skills = (self.brain.load_skills_for_agent("writer") or ""
                          if hasattr(self.brain, "load_skills_for_agent") else "")
                plan = await ref.prepare_referral(
                    self.state, self.brain, self.config, self.env or self._load_env(),
                    referral=referral, company=company, referrer_first=first,
                    referrer_label=referrer_name or "their contact", skills=skills,
                    their_words=ref.their_own_words(text), multi=is_multi(self.config))
                result = await ref.store_referral(
                    self.state, plan, referrer=prospect, company=company,
                    mailbox=prospect.mailbox if prospect else "", provider=email.provider,
                    agent="linkedin", referrer_name=referrer_name,
                    source_note=f"on LinkedIn (lead {lead['id']})")
                out["keep"] = {result["referral_prospect_id"]}
                out["note"] = ref.reply_guidance(referral, plan)
                return out
        if referral.kind in (ref.OUTSIDE, ref.SEVERAL) or referral.kind == ref.ADDRESS:
            why = referral.why or f"they named {referral.email}, and email isn't set up to write to them"
            out["for_you"] = True
            await self.state.li_update_lead(lead["id"], note=("Pointed elsewhere: " + why)[:300])
            await self.state.log_action("escalation", "linkedin", {
                "linkedin_lead_id": lead["id"], "reason": why, "reply_preview": (text or "")[:200]})
            return out
        if referral.kind == ref.LINKEDIN:
            slug = referral.linkedin_slug
            name = referral.name or " ".join(p.title() for p in slug.split("-") if p.isalpha())
            today = self.now().date().isoformat()
            lead_id, created = await self.state.li_upsert_lead(
                slug, stage="found", name=name, first_name=referral.first_name or name.split(" ")[0],
                headline=referral.role, company=company.name or lead.get("company") or "",
                profile_url=profile_url(slug), company_id=company.id or "", source=ref.REFERRAL_SOURCE,
                note=f"Referred on {today} by {referrer_name or 'a contact'} on LinkedIn")
            new_lead = await self.state.li_get_lead(lead_id)
            if new_lead and new_lead["stage"] == "found" and not await self.state.li_pending_for(lead_id):
                note = ""
                if first and await self.notes_allowed():
                    note = fit_note(ref.NAMED_OPENING.format(first=first) + ".",
                                    self.cfg.invite_note_max_chars)
                await self.state.li_add_message(
                    lead_id, direction="out", kind="invite_note", body=note, status="draft",
                    error=(HELD_FOR_REVIEW + f"a referral: {referrer_name or 'a contact'} named them; "
                           "invite them only if you want to"))
                self.report["invite_drafts"] = self.report.get("invite_drafts", 0) + 1
            await self.state.log_action("referral", "linkedin", {
                "linkedin_lead_id": lead_id, "referred_by_lead": lead["id"], "created": created})
        out["note"] = ref.reply_guidance(referral)
        return out

    @staticmethod
    def _load_env():
        try:
            from harvey.config import load_env
            return load_env()
        except Exception:
            return None

    async def email_block(self, lead: dict) -> str:
        """Why no LinkedIn pitch may go to this lead because of email, or ""."""
        prospect_id = lead.get("prospect_id") or ""
        if prospect_id:
            prospect = await self.state.get_prospect(prospect_id)
            if prospect is not None and prospect.status in EMAIL_STOPPED:
                return f"their email status is {prospect.status}"
            if await self.state.colleague_who_replied(prospect_id):
                return "someone at their company answered an email"
        company_id = lead.get("company_id") or ""
        if company_id:
            from harvey.config import live_do_not_contact
            from harvey.gate import do_not_contact_domain_match
            company = await self.state.get_company(company_id)
            if company and company.domain and do_not_contact_domain_match(
                    company.domain, live_do_not_contact(self.config)):
                return "their company is on the do-not-contact list"
        return ""

    # ── 3. First messages and follow-ups ──

    async def draft_messages(self):
        if self._brain_paused():
            return
        used = await self.pacing.used()
        room = max(0, self.cfg.max_daily_messages - used["messages_today"])
        room -= len(await self.state.li_messages(statuses=("draft", "approved"),
                                                 kinds=("message",), direction="out"))
        now = self.now()
        for lead in await self.state.li_leads(stages=("accepted", "messaged")):
            if room <= 0 or not self._time_left():
                break
            await self._inbox_break()
            if lead.get("no_message_reason") or await self.state.li_pending_for(
                    lead["id"], kinds=("message", "reply")):
                continue
            failed = await self.state.li_messages(lead_id=lead["id"], kinds=("message",),
                                                  statuses=("failed",))
            if len(failed) >= MAX_FAILED_SENDS:
                # The same send failing again and again: set them aside for you
                # instead of drafting (and spending a model call) each run.
                await self.state.li_update_lead(lead["id"], no_message_reason=(
                    f"{len(failed)} messages to them failed ({failed[-1]['error'] or 'no reason'}): "
                    "look at their profile")[:300])
                continue
            if await self.email_block(lead):
                continue
            unanswered = await self.state.li_unanswered(lead["id"])
            if len(unanswered) >= self.cfg.max_unanswered:
                continue
            sent_messages = [m for m in unanswered if m["kind"] in ("message", "reply")]
            ever_sent = await self.state.li_messages(lead_id=lead["id"], kinds=("message",),
                                                     statuses=("sent",))
            if lead["stage"] == "accepted" and not ever_sent:
                accepted = _parse(lead.get("accepted_at"))
                due = _parse(lead.get("next_action_at")) or (
                    accepted + timedelta(days=self.cfg.first_message_min_days) if accepted else now)
                which = "first"
            elif lead["stage"] == "messaged" and len(sent_messages) == 1:
                last = _parse(sent_messages[-1].get("sent_at")) or now
                due = _parse(lead.get("next_action_at")) or (
                    last + timedelta(days=self.cfg.follow_up_min_days))
                due = max(due, last + timedelta(days=self.cfg.follow_up_min_days))
                which = "follow_up"
            else:
                continue
            if due > now:
                continue
            from harvey import targeting
            if targeting.active(self.config):
                await self.research(lead)         # the profile, once (kept 14 days)
                profile = await self._profile_cache(lead)
                why = self.off_target(lead, location=profile.get("location") or "",
                                      headline=profile.get("headline") or "")
                if why:
                    await self.state.li_update_lead(
                        lead["id"], no_message_reason=f"not messaged: {why}"[:300])
                    self.report["off_target"] = self.report.get("off_target", 0) + 1
                    continue
            history = await self.state.li_messages(lead_id=lead["id"])
            body = await self.draft(lead, which, history=history)
            if body and await self.add_draft(lead, "message", body):
                room -= 1
        # Catch-ups: a conversation that went quiet after Harvey's last message.
        for lead in await self.state.li_leads(stages=("replied", "warm")):
            if room <= 0 or not self._time_left() or self._brain_paused():
                break
            if await self.state.li_pending_for(lead["id"]):
                continue
            # Set aside (by you, or after failed sends): no catch-up. Targeting's
            # own marks don't count: someone who wrote to you is still answered.
            reason = lead.get("no_message_reason") or ""
            if reason and not reason.startswith(("not messaged:", "not invited:")):
                continue
            history = await self.state.li_messages(lead_id=lead["id"])
            if not li_catch_up_due(history, self.cfg, now):
                continue
            if await self.email_block(lead) or await said_no(self.state, self.config, lead):
                continue
            body = await self.draft(lead, "catch_up", history=history)
            if body and await self.add_draft(lead, "catch_up", body):
                room -= 1

    # ── 4. Invites ──

    async def notes_allowed(self) -> bool:
        if not self.cfg.invite_notes:
            return False
        until = _parse(await self.state.get_setting(NOTES_OFF_KEY))
        return not (until and until > self.now())

    async def found_to_invite(self, limit: int) -> list[dict]:
        """Leads already found (web search, referrals, shop sites) and never
        invited: no invite drafted for them yet and nothing setting them aside.
        People from shop websites first, then the web search's, oldest first."""
        if limit <= 0:
            return []
        rows = await self.state._li_query(
            "SELECT l.* FROM linkedin_leads l WHERE l.stage = 'found' "
            "AND COALESCE(l.no_message_reason, '') = '' AND COALESCE(l.slug, '') != '' "
            "AND NOT EXISTS (SELECT 1 FROM linkedin_messages m WHERE m.lead_id = l.id "
            "AND m.kind = 'invite_note' AND NOT (m.status = 'skipped' "
            "AND COALESCE(m.error, '') LIKE 'not invited:%')) "
            "ORDER BY CASE l.source WHEN 'harvey_prospect' THEN 0 WHEN 'web_search' THEN 2 ELSE 1 END, "
            "l.created_at LIMIT ?", (limit,))
        return [dict(r) for r in rows]

    async def find_people(self):
        """New people by web search (harvey/linkedin/finder.py) when fewer than
        two days of invites are waiting to be drafted. Needs TINYFISH_API_KEY."""
        if not self._time_left() or await self.pacing.invites_paused_until():
            return
        wanted = 2 * self.cfg.max_daily_connections - len(
            await self.found_to_invite(2 * self.cfg.max_daily_connections))
        if wanted <= 0:
            return
        env = self.env or self._load_env()
        key = getattr(env, "tinyfish_api_key", "") or ""
        if not key:
            return
        from harvey.linkedin.finder import find_people
        found = await find_people(self.state, self.config, key, wanted)
        self.report["found_people"] = found["added"]
        if found["searches"]:
            logger.info(f"LinkedIn: {found['searches']} web searches found {found['found']} profiles, "
                        f"{found['added']} new to invite, {found['skipped']} outside your targets.")

    async def invite_candidates(self, limit: int) -> list[dict]:
        titles = [t.strip().lower() for t in self.cfg.invite_titles if t.strip()]
        if not titles or limit <= 0:
            return []
        title_sql = " OR ".join("lower(p.title) LIKE ?" for _ in titles)
        stopped = sorted(EMAIL_STOPPED)
        rows = await self.state._li_query(
            "SELECT p.id, p.first_name, p.last_name, p.title, p.linkedin_url, p.company_id, "
            "p.company, p.email, COALESCE(c.name, '') AS company_name, "
            "COALESCE(c.location, '') AS company_location FROM prospects p "
            "LEFT JOIN companies c ON c.id = p.company_id "
            "WHERE lower(p.linkedin_url) LIKE '%linkedin.com/in/%' "
            f"AND p.status NOT IN ({', '.join('?' for _ in stopped)}) AND ({title_sql}) "
            "ORDER BY p.score DESC, p.created_at LIMIT ?",
            (*stopped, *(f"%{t}%" for t in titles), limit * 5))
        from harvey.linkedin.pages import slug_from_url
        out = []
        for row in rows:
            slug = slug_from_url(row["linkedin_url"])
            if not slug or await self.state.li_get_lead_by_slug(slug):
                continue
            if row["id"] and await self.state.colleague_who_replied(row["id"]):
                continue
            from harvey import targeting
            if targeting.active(self.config) and targeting.cold_reason(
                    self.config, title=row["title"] or "",
                    country=(targeting.location_country(row["company_location"])
                             or targeting.email_country(row["email"] or ""))):
                continue
            row["slug"] = slug
            out.append(row)
            if len(out) >= limit:
                break
        return out

    async def pick_invites(self):
        used = await self.pacing.used()
        waiting = len(await self.state.li_messages(statuses=("draft", "approved"),
                                                   kinds=("invite_note",), direction="out"))
        room = min(self.cfg.max_daily_connections - used["invites_today"],
                   self.cfg.max_weekly_invites - used["invites_week"]) - waiting
        if room <= 0 or not self._time_left() or await self.pacing.invites_paused_until():
            return
        notes = await self.notes_allowed()
        if notes and self._brain_paused():
            return
        for lead in await self.found_to_invite(room):
            if not self._time_left():
                return
            await self._inbox_break()
            if await self.email_block(lead):
                continue
            note = ""
            if notes:
                note = fit_note(await self.draft(lead, "invite"), self.cfg.invite_note_max_chars)
            if await self.add_draft(lead, "invite_note", note):
                self.report["invite_drafts"] += 1
                room -= 1
        if room <= 0:
            return
        for row in await self.invite_candidates(room):
            if not self._time_left():
                return
            name = " ".join(x for x in (row["first_name"], row["last_name"]) if x)
            lead_id, _ = await self.state.li_upsert_lead(
                row["slug"], name=name, first_name=row["first_name"], headline=row["title"],
                company=row["company_name"] or row["company"], profile_url=profile_url(row["slug"]),
                prospect_id=row["id"], company_id=row["company_id"], source="harvey_prospect")
            lead = await self.state.li_get_lead(lead_id)
            if await self.email_block(lead):
                continue
            note = ""
            if notes:
                note = fit_note(await self.draft(lead, "invite"), self.cfg.invite_note_max_chars)
            if await self.add_draft(lead, "invite_note", note):
                self.report["invite_drafts"] += 1

    # ── Drafting ──

    def _brain_paused(self) -> str:
        return getattr(self.brain, "paused_reason", "") or ""

    async def _facts(self, lead: dict) -> str:
        lines = [f"Name: {lead.get('name') or ''}", f"First name: {lead.get('first_name') or ''}",
                 f"Headline: {lead.get('headline') or ''}", f"Brand or company: {lead.get('company') or ''}"]
        if lead.get("company_id"):
            company = await self.state.get_company(lead["company_id"])
            if company:
                for label, value in (("Company", company.name), ("Website", company.website or company.domain),
                                     ("What they do", company.description), ("Industry", company.industry),
                                     ("Location", company.location), ("Notes", company.notes),
                                     ("Their site runs on", ", ".join(company.tech_stack or []))):
                    if value:
                        lines.append(f"{label}: {value}")
        if lead.get("prospect_id"):
            prospect = await self.state.get_prospect(lead["prospect_id"])
            if prospect and prospect.personalization_notes:
                lines.append(f"Research notes: {prospect.personalization_notes}")
        if lead.get("source") == "referral" and lead.get("note"):
            lines.append(f"How we came to them: {lead['note']}")
        return "\n".join(line for line in lines if not line.endswith(": "))

    async def research(self, lead: dict) -> str:
        """What their profile page says (pages.profile_summary), read once and
        kept PROFILE_FRESH, so a message builds on something real about them
        (owner, 2026-11-16: the messages read generic). Their headline is
        stored when it was missing. "" without a browser or a profile URL."""
        key = PROFILE_KEY + lead["id"]
        cached = {}
        try:
            cached = json.loads(await self.state.get_setting(key) or "{}")
        except ValueError:
            cached = {}
        at = _parse(cached.get("at"))
        if cached.get("text") and "location" in cached and at and self.now() - at < PROFILE_FRESH:
            return cached["text"]
        url = lead.get("profile_url") or (profile_url(lead["slug"]) if lead.get("slug") else "")
        if self.driver is None or self.dry_run or not url:
            return cached.get("text") or ""
        try:
            html = await self._visit(url)
        except (BreakerTripped, SwitchedOff):
            raise
        except Exception as e:
            logger.info(f"LinkedIn: couldn't read {lead.get('name') or 'a'} profile: {e}")
            return cached.get("text") or ""
        text = profile_summary(html)
        headline = profile_headline(html)
        if headline and not lead.get("headline"):
            await self.state.li_update_lead(lead["id"], headline=headline[:220])
            lead["headline"] = headline[:220]
        if text:
            await self.state.set_setting(key, json.dumps({
                "at": self.now().isoformat(), "text": text,
                "location": profile_location(html), "headline": headline[:220]}))
        return text

    async def _profile_cache(self, lead: dict) -> dict:
        """What Harvey has read about them: the profile page (location and
        headline, from its text when the page's own lines weren't found), else
        what the web search that found them said."""
        try:
            data = json.loads(await self.state.get_setting(PROFILE_KEY + lead["id"]) or "{}")
        except ValueError:
            data = {}
        data = data if isinstance(data, dict) else {}
        text = data.get("text") or ""
        if text and not data.get("location"):
            data["location"] = summary_location(text)
        if text and not data.get("headline"):
            data["headline"] = summary_headline(text)
        if not data.get("location") or not data.get("headline"):
            found = await self._found(lead)
            data["location"] = data.get("location") or found.get("location") or ""
            data["headline"] = data.get("headline") or found.get("headline") or ""
        return data

    async def _found(self, lead: dict) -> dict:
        """What the web search said about them ({"location", "headline"}), or {}."""
        try:
            data = json.loads(await self.state.get_setting(FOUND_KEY + lead["id"]) or "{}")
        except ValueError:
            data = {}
        return data if isinstance(data, dict) else {}

    def off_target(self, lead: dict, *, location: str = "", headline: str = "",
                   required: bool = False) -> str:
        """Why the owner's targeting (harvey/targeting.py) rules this lead out
        for cold outreach, or "". ``required``: an unread location or role
        rules them out too (a connect)."""
        from harvey import targeting
        if not targeting.active(self.config):
            return ""
        return targeting.cold_reason(
            self.config, country=targeting.location_country(location),
            title=headline or lead.get("headline") or "",
            country_required=required, title_required=required)

    def _history(self, history) -> str:
        out = []
        sent = [m for m in (history or []) if m["status"] == "sent" and m["body"]]
        for m in sent[-HISTORY_MESSAGES:]:
            who = "Them" if m["direction"] == "in" else "You"
            kind = " (connection note)" if m["kind"] == "invite_note" else ""
            out.append(f"{who}{kind}: {m['body']}")
        return "\n\n".join(out)

    async def draft(self, lead: dict, which: str, intent: str = "", history=None,
                    note: str = "", booked: str | None = None) -> str:
        """One message by the brain: which = invite | first | follow_up | reply.
        ``note``: what a reply must say (a referral's thank-you). ``booked``:
        they said yes to this offered time, so the reply confirms it."""
        persona, product = self.config.persona, self.config.product
        prompt = self.brain.load_prompt(
            "linkedin", persona_name=persona.name, persona_role=persona.role,
            persona_company=persona.company, persona_tone=persona.tone,
            product_name=product.name, product_description=product.description)
        # The private house playbook (voice, offers, what may be claimed) and
        # the trained product facts, when this install has them.
        from harvey.brain import SKILLS_DIR
        for skill, title in (("house_playbook", "HOUSE PLAYBOOK (voice, offers and what we may claim)"),
                             ("product_knowledge", "PRODUCT KNOWLEDGE")):
            if hasattr(self.brain, "load_skill") and (SKILLS_DIR / f"{skill}.md").exists():
                text = self.brain.load_skill(skill)
                if text:
                    prompt += f"\n\n{title}\n{text}"
        # The owner's approved LinkedIn lessons from results (harvey/lessons.py).
        from harvey.lessons import lessons_block
        prompt += await lessons_block(getattr(self, "state", None), "linkedin")
        booking = getattr(getattr(product, "offer", None), "booking_url", "") or ""
        if meeting_offer(self.config) is not None:
            booking = ""          # the owner's fixed time and Meet link, never a booking page
        # The owner's fixed slot (harvey/meeting_slot.py), offered when they ask
        # to meet and, directly, once they're interested (owner, 2026-11-20).
        slot_block = ""
        ours = [m["body"] for m in history or [] if m["direction"] == "out" and m["status"] == "sent"]
        offered_before = offered_link_in(ours, self.config)
        if which == "reply":
            theirs = [m for m in history or [] if m["direction"] == "in"]
            latest = theirs[-1]["body"] if theirs else ""
            why = ("asked" if asks_to_meet(latest) else
                   "interested" if (intent == "interested" or wants_next_step(latest)) and not offered_before
                   else "")
            if booked is not None:
                slot_block = confirm_block(booked, self.config)
            elif declines_call(latest) and chat_instead_block(self.config):
                # They can't do a call: chat on WhatsApp instead.
                slot_block, booking = chat_instead_block(self.config), ""
            elif why:
                slot = await self.meeting_slot(lead)
                if slot is not None:
                    slot_block, booking = prompt_block(slot, self.config, why), ""
                    await remember_offer(self.state, "linkedin:" + lead["id"], slot)
        elif which == "catch_up" and lead.get("stage") == "warm" and not offered_before:
            slot = await self.meeting_slot(lead)
            if slot is not None:
                slot_block = prompt_block(slot, self.config, "catch_up")
                await remember_offer(self.state, "linkedin:" + lead["id"], slot)
        CHAT = (" Write it as one to three short messages separated by a blank line, like messages "
                "sent one after another.")
        task = {
            "invite": (f"Write the note for a LinkedIn connection request to this person. At most "
                       f"{self.cfg.invite_note_max_chars} characters, one or two sentences, a reason to "
                       "connect that is about them. No pitch, no link, no question about a call."),
            "first": ("They accepted the connection request. Write the first message: value first, "
                      "one specific, useful observation from the facts, and one easy question. You may "
                      "use their first name once here." + CHAT),
            "follow_up": ("They haven't answered the first message. Write one short follow-up, "
                          "a different angle or something useful, no guilt, no \"bumping this\". "
                          "Don't use their name." + CHAT),
            "reply": (f"They replied (read as: {intent}). React to what they said first, then answer "
                      "it, like a person messaging. Don't use their name. "
                      + (f"If they ask to talk, the booking link is {booking}. " if booking else "")
                      + "If they ask whether this is automated, say yes, honestly." + CHAT),
            "catch_up": ("The conversation went quiet after your last message. Pick it up where it "
                         "stopped: something useful about what they were talking about, or a light, "
                         "easy question about it. No guilt, no \"just checking in\", no \"bumping "
                         "this\". Don't use their name. Under 40 words in all." + CHAT),
        }[which]
        profile = await self.research(lead) if which in ("first", "follow_up", "reply") else ""
        prompt += f"""

{UNTRUSTED_RULE}

FACTS about them:
{fence(await self._facts(lead), "their profile and company")}
"""
        if profile:
            prompt += (f"\nTHEIR LINKEDIN PROFILE (what their profile page shows):\n"
                       f"{fence(profile, 'their LinkedIn profile')}\n")
        if which in ("first", "follow_up"):
            task += (" Build it on one concrete detail from THEIR LINKEDIN PROFILE or the FACTS: what "
                     "they sell, their role, something they did or said. Never fall back on a question "
                     "you could ask anyone (such as whether the site is run in-house or by an agency) "
                     "when a specific detail is there.")
        if history:
            prompt += f"\nTHE CONVERSATION SO FAR:\n{fence(self._history(history), 'the conversation')}\n"
        if note:
            task += "\nThe reply:\n" + note
        prompt += f"\nTASK: {task}{slot_block}\n\nOutput ONLY the message text."
        for _ in range(MAX_DRAFT_ATTEMPTS):
            text = await self.brain.think(prompt, session_id="harvey-linkedin",
                                          agent="linkedin", task=f"draft_{which}")
            text = _clean_draft(text)
            if not text:
                if self._brain_paused():
                    return ""
                continue
            if text.lower().startswith(("i can't", "i cannot", "as an ai", "sorry,")):
                continue
            if which != "invite":
                # Their name once in a conversation, never again (owner, 2026-11-19).
                first_name = lead.get("first_name") or (lead.get("name") or "").split(" ")[0]
                text = strip_repeat_name(text, first_name, [
                    m["body"] for m in (history or []) if m["direction"] == "out" and m["status"] == "sent"])
            return text
        return ""

    async def add_draft(self, lead: dict, kind: str, body: str, hold=None, thread_url: str = "") -> str | None:
        """Store a draft. On autopilot it is approved at once, unless the
        review (planted links, quoted instructions) or ``hold`` keeps it."""
        if kind != "invite_note" and not body:
            return None
        status, approved_by, error = "draft", "", ""
        if not self.dry_run and await autopilot_on(self.state, self.config):
            from harvey.gate import autopilot_review
            reasons = list(hold or []) + (
                autopilot_review("", body, self.config, own_sites=await self._own_sites(lead))
                if body else [])
            if reasons:
                error = HELD_FOR_REVIEW + "; ".join(reasons)
            else:
                status, approved_by = "approved", "autopilot"
        elif hold:
            error = HELD_FOR_REVIEW + "; ".join(hold)
        message_id = await self.state.li_add_message(
            lead["id"], direction="out", kind=kind, body=body, status=status, error=error,
            approved_by=approved_by, thread_url=thread_url, dedupe=False)
        self.report["drafted"] = self.report.get("drafted", 0) + 1
        return message_id

    async def release_held(self):
        """Drafts held for a look, looked at again every run on autopilot
        (owner, 2026-11-20: "not waiting for approval ... autopilot means auto
        send"). A hold that no longer applies lets the draft go:

        - a link check that passes now (their own website, once known);
        - a site review, with reviews autopilot on;
        - "is this automated?" answered with an honest yes.

        A cold message still has to pass the targeting check, and a draft that
        something later has overtaken (another message went to them since) is
        set aside. Anything else (a link that isn't theirs or ours, quoted
        instructions, a referral) keeps waiting for you."""
        if self.dry_run or not await autopilot_on(self.state, self.config):
            return
        from harvey.gate import autopilot_review
        reviews_on = await reviews_autopilot(self.state)
        released = 0
        for m in await self.state.li_messages(statuses=("draft",), direction="out"):
            error = m.get("error") or ""
            if not error.startswith(HELD_FOR_REVIEW) or m["kind"] == "invite_note":
                continue
            lead = await self.state.li_get_lead(m["lead_id"])
            if lead is None or lead["stage"] == "declined":
                continue
            rows = await self.state.li_messages(lead_id=lead["id"])
            later = [r for r in rows if r["direction"] == "out" and r["id"] != m["id"]
                     and r["kind"] != "invite_note" and (r.get("created_at") or "") > (m.get("created_at") or "")
                     and r["status"] in ("sent", "approved", "draft")]
            if later:
                await self.state.li_update_message(
                    m["id"], status="skipped", error="overtaken: a later message to them was written or sent")
                continue
            reasons = [r.strip() for r in error[len(HELD_FOR_REVIEW):].split("; ") if r.strip()]
            keep, review = [], False
            for reason in reasons:
                if reason.startswith(("links to ", "quotes Harvey")):
                    continue                    # checked again just below
                if reason.startswith(REVIEW_HELD_REASON[:24]):
                    review = True
                    if reviews_on:
                        continue
                if (reason.startswith("they asked whether this is automated")
                        and answers_automation_honestly(m["body"])):
                    continue
                keep.append(reason)
            if m["body"]:
                review_of = autopilot_review("", m["body"], self.config, own_sites=await self._own_sites(lead))
                if (review_of and not keep and self.driver is not None and self._time_left()
                        and all(r.startswith("links to ") for r in review_of)
                        and not (await self._profile_cache(lead)).get("text")):
                    # Their profile, read once: the site it shows is theirs.
                    await self.research(lead)
                    review_of = autopilot_review("", m["body"], self.config,
                                                 own_sites=await self._own_sites(lead))
                keep += review_of
            if keep:
                if keep != reasons:
                    await self.state.li_update_message(m["id"], error=(HELD_FOR_REVIEW + "; ".join(keep))[:300])
                continue
            if m["kind"] == "message":
                profile = await self._profile_cache(lead)
                why = self.off_target(lead, location=profile.get("location") or "",
                                      headline=profile.get("headline") or "")
                if why:
                    await self.state.li_update_message(m["id"], status="skipped", error=f"not sent: {why}"[:300])
                    await self.state.li_update_lead(lead["id"], no_message_reason=f"not messaged: {why}"[:300])
                    continue
            await self.state.li_update_message(m["id"], status="approved", error="",
                                               approved_by=REVIEW_AUTOPILOT if review else "autopilot")
            released += 1
        if released:
            self.report["released"] = self.report.get("released", 0) + released
            logger.info(f"LinkedIn: {released} held drafts no longer need you; they go out.")

    # ── 5. Sending ──

    async def send_approved(self, only=None):
        """Send what is approved: replies, then messages, then invites.
        ``only``: just these kinds (the replies that go first in a run). An
        item that doesn't go says why on itself (_note_waiting)."""
        order = {"reply": 0, "message": 1, "catch_up": 1, "invite_note": 2}
        items = sorted(await self.state.li_messages(statuses=("approved",), direction="out"),
                       key=lambda m: (order.get(m["kind"], 3), m["created_at"] or ""))
        if only:
            items = [m for m in items if m["kind"] in only]
        replies_any_time = bool(getattr(self.cfg, "replies_any_time", False))
        for n, item in enumerate(items):
            if not self._time_left():
                for rest in items[n:]:
                    await self._note_waiting(rest, "the run's time ran out first; it goes on the next run")
                break
            if not only:
                try:
                    await self._inbox_break()      # a reply that came in meanwhile goes first
                except PageNotLoading as e:
                    self._page_not_loading(e, "the inbox check")
            reply = item["kind"] == "reply"
            if getattr(self, "replies_only", False) and not reply:
                continue        # an inbox check sends replies only
            if not self.pacing.in_hours() and not (reply and replies_any_time):
                await self._note_waiting(item, f"outside LinkedIn hours ({self.pacing.hours_text()})")
                continue        # invites and first messages keep to hours
            await self._still_on()
            if await active_pause(self.state, self.now()):
                break
            if item["lead_id"] in self._failed_leads:
                await self._note_waiting(item, "a send to them failed earlier this run; it's tried next run")
                continue
            lead = await self.state.li_get_lead(item["lead_id"])
            if lead is None:
                continue
            if lead["stage"] == "declined":
                await self.state.li_update_message(item["id"], status="skipped",
                                                   error="they declined")
                continue
            if item.get("approved_by") == REVIEW_AUTOPILOT and not await reviews_autopilot(self.state):
                # Reviews autopilot was turned off before it went: back to you.
                await self.state.li_update_message(item["id"], status="draft", approved_by="",
                                                   error=HELD_FOR_REVIEW + REVIEW_HELD_REASON)
                continue
            # What you approved or wrote yourself is held to the same stop rules.
            if item["kind"] != "invite_note":
                why = await said_no(self.state, self.config, lead)
                if not why and len(await self.state.li_unanswered(lead["id"])) >= self.cfg.max_unanswered:
                    why = f"{self.cfg.max_unanswered} messages without a reply already"
                if why:
                    await self.state.li_update_message(item["id"], status="skipped", error=why)
                    continue
            if item["kind"] in ("message", "catch_up") and (lead.get("last_inbound_at") or "") > (
                    item["created_at"] or ""):
                await self.state.li_update_message(
                    item["id"], status="skipped",
                    error="they wrote after this was drafted; answer that instead")
                continue
            if item["kind"] != "reply":
                why = await self.email_block(lead)
                if why or (item["kind"] == "message" and lead.get("no_message_reason")):
                    await self.state.li_update_message(
                        item["id"], status="skipped", error=why or lead["no_message_reason"])
                    continue
            kind = "invite" if item["kind"] == "invite_note" else "message"
            blocked = await self.pacing.blocked(kind)
            if blocked:
                await self._note_waiting(item, blocked)
                if "hour" in blocked:
                    for rest in items[n + 1:]:
                        await self._note_waiting(rest, blocked)
                    break
                continue
            if self.dry_run:
                self.report["would_send"].append(
                    {"kind": item["kind"], "lead": lead.get("name"), "body": item["body"]})
                continue
            html = None
            if kind == "invite":
                # Their profile first: someone it rules out is skipped without
                # the wait between actions (2026-11-20: runs spent most of their
                # time waiting before skips).
                self._inviting = True
                try:
                    html = await self._invite_profile(lead, item)
                except InviteLimitReached:
                    await self._note_waiting(item, "LinkedIn's weekly invite limit; it goes when LinkedIn allows")
                    continue
                except PageNotLoading as e:
                    await self._note_waiting(item, "LinkedIn didn't load their profile; it's tried next run")
                    self._page_not_loading(e, "this invite")
                    continue
                finally:
                    self._inviting = False
                if html is None:
                    continue
            await self._wait_between_actions()
            await self._still_on()
            if await active_pause(self.state, self.now()):
                break
            current = await self.state.li_get_message(item["id"])
            if current is None or current["status"] != "approved":
                continue
            try:
                if kind == "invite":
                    self._inviting = True
                    try:
                        ok, detail = await self.send_invite(lead, item, html=html)
                    except InviteLimitReached:
                        await self._note_waiting(item, "LinkedIn's weekly invite limit; it goes when LinkedIn allows")
                        continue        # stays approved: it goes when LinkedIn allows again
                    finally:
                        self._inviting = False
                else:
                    ok, detail = await self.send_message(lead, item)
            except PageNotLoading as e:
                # Nothing was typed yet on a page that never loaded: it waits.
                fresh = await self.state.li_get_message(item["id"])
                if fresh and fresh["status"] == "approved":
                    await self._note_waiting(fresh, "LinkedIn didn't load the page; it's tried next run")
                self._page_not_loading(e, "this send")
                continue
            if ok:
                await self.pacing.record(kind)
                self.report["sent_invites" if kind == "invite" else "sent_messages"] += 1
                if item.get("approved_by") == CAREERS_APPROVED:
                    await self.stop_for_good(lead)     # pointed to HR: nothing more
            elif detail:
                self._failed_leads.add(lead["id"])
                self.report["failed"] += 1
                await self.state.li_update_message(item["id"], status="failed", error=detail)
                logger.warning(f"LinkedIn: not sent to {lead.get('name')}: {detail}")
            else:
                fresh = await self.state.li_get_message(item["id"])
                if fresh and fresh["status"] == "approved":
                    await self._note_waiting(fresh, "stopped before it went (switched off or paused)")

    async def send_message(self, lead: dict, item: dict) -> tuple[bool, str]:
        """The recipe. (True, "") once the thread shows it; (False, why)
        otherwise. Nothing is retried: a failure waits for you."""
        name = lead.get("name") or ""
        body = item["body"]
        html = await self._visit(lead.get("profile_url") or profile_url(lead["slug"]), pause=False)
        link = compose_link_from_profile(html, name)
        if not link:
            return False, "their profile had no Message button for them (not connected?)"
        html = await self._visit(link)
        if not recipient_confirmed(html, name):
            return False, f"the message window didn't show {name} as the recipient"
        if item["kind"] in ("message", "catch_up"):
            thread = parse_thread(html)
            if any(names_match(m.sender, name) for m in thread.messages):
                if await self.record_thread(lead, thread, self.driver.url):
                    return False, "they had written to you on LinkedIn; read it before sending this"
        # The draft goes as its parts, one message each, a few seconds apart
        # (harvey/chat_style.py).
        pieces = chat_parts(body) or [body]
        for i, piece in enumerate(pieces):
            if i:
                await self.driver.wait(self.rng.uniform(2.0, 5.0))
            ok, detail = await self._send_piece(piece, first=i == 0)
            if ok:
                continue
            if i == 0:
                return False, detail
            return False, (MAYBE_SENT + f"Sent {i} of {len(pieces)} messages, then: {detail or 'stopped'}. "
                           "Check the conversation on LinkedIn before sending anything else.")
        sent_at = self.now()
        await self.state.li_mark_sent(item["id"], thread_url=self.driver.url, at=sent_at)
        if item["kind"] == "message":
            wait = self.rng.uniform(self.cfg.follow_up_min_days, self.cfg.follow_up_max_days)
            await self.state.li_update_lead(lead["id"], next_action_at=sent_at + timedelta(days=wait))
        return True, ""

    async def _send_piece(self, text: str, first: bool) -> tuple[bool, str]:
        """Type one message in the open conversation, check the box holds it,
        press Send, and wait for the thread to show it. ``first``: a stop
        while typing leaves the whole item approved."""
        if not await self.driver.type_text(COMPOSER, text):
            return False, "couldn't find the message box"
        typed = clean_text(await self.driver.text_of(COMPOSER))
        if typed != clean_text(text):
            await self.driver.clear(COMPOSER)
            return False, "the message box didn't hold exactly the draft, so it wasn't sent"
        if first and await self._held():          # stopped while typing: it stays approved
            await self.driver.clear(COMPOSER)
            return False, ""
        if not await self.driver.click(SEND_BUTTON):
            await self.driver.clear(COMPOSER)
            return False, "couldn't press Send"
        want = squash(text)
        for _ in range(6):
            await self.driver.wait(1.5)
            html = await self.driver.html()
            await self._guard(html)
            last = parse_thread(html).last()
            if last and squash(last.body).startswith(want):
                return True, ""
        return False, (MAYBE_SENT + "Send was pressed but the thread didn't show the message. "
                       "Check the conversation on LinkedIn before sending anything else.")

    async def _open_connect(self, html: str, lead: dict) -> tuple[bool, str]:
        """Open the invite for THIS person: their own "Invite <name> to
        connect", on the card or in their More menu. A page-wide match once
        clicked a side list's other people (2026-11-20)."""
        name = lead.get("name") or lead.get("first_name") or ""
        state = profile_connect_state(html, name)
        if state in ("pending", "connected") or not name:
            return False, state
        if await self.driver.click_connect_for(name):
            return True, state
        if await self.driver.click_button(r"^more( actions)?$", within="main") and \
                await self.driver.click_connect_for(name):
            return True, state
        return False, state

    async def _invite_profile(self, lead: dict, item: dict) -> str | None:
        """Their profile page, read before an invite: None when the invite is
        skipped here (already pending, already connected, or outside the
        owner's targeting), the page's HTML otherwise. What it shows is kept
        (PROFILE_KEY), so their first message needs no second visit."""
        html = await self._visit(lead.get("profile_url") or profile_url(lead["slug"]))
        state = profile_connect_state(html, lead.get("name") or "")
        if state == "pending":
            await self.state.li_update_message(item["id"], status="skipped",
                                               error="an invite to them was already pending")
            await self.state.li_set_stage(lead["id"], "invited")
            return None
        if state == "connected":
            await self.state.li_update_message(item["id"], status="skipped",
                                               error="you're already connected")
            await self.state.li_set_stage(lead["id"], "accepted", accepted_at=self.now())
            return None
        location, headline = profile_location(html), profile_headline(html)
        text = profile_summary(html)
        if text:
            await self.state.set_setting(PROFILE_KEY + lead["id"], json.dumps({
                "at": self.now().isoformat(), "text": text, "location": location,
                "headline": headline[:220]}))
        if not location or not headline:
            # The web search that found them saw both, often (finder.py).
            found = await self._found(lead)
            location = location or found.get("location") or ""
            headline = headline or found.get("headline") or ""
        # The owner's targeting: their profile must show a country you chose and
        # a decision maker's role (harvey/targeting.py).
        why = self.off_target(lead, location=location, headline=headline, required=True)
        if why:
            await self.state.li_update_message(item["id"], status="skipped",
                                               error=f"not invited: {why}"[:300])
            await self.state.li_update_lead(lead["id"], no_message_reason=f"not invited: {why}"[:300])
            self.report["off_target"] = self.report.get("off_target", 0) + 1
            return None
        return html

    async def send_invite(self, lead: dict, item: dict, html: str | None = None) -> tuple[bool, str]:
        """The invite. ``html``: their profile, already read and checked
        (_invite_profile); read here when not."""
        if html is None:
            html = await self._invite_profile(lead, item)
            if html is None:
                return False, ""
        ok, _ = await self._open_connect(html, lead)
        if not ok:
            return False, "couldn't find their own Connect, on the card or under More"
        html = await self.driver.html()
        await self._guard(html)
        note = item["body"] if await self.notes_allowed() else ""
        sent_note = ""
        if note and await self.driver.click_button(r"^add a( free)? note$", within=INVITE_DIALOG):
            html = await self.driver.html()
            await self._guard(html)
            limit = note_limit_reached(html)
            if limit or not await self.driver.type_text(INVITE_NOTE_BOX, note):
                # No note this month: LinkedIn's limit, or a note form Harvey can't
                # find (live, 2026-11-20). The invite goes without one, and notes
                # stay off until next month.
                first = self.now().replace(day=1, hour=0, minute=0, second=0, microsecond=0)
                next_month = (first + timedelta(days=32)).replace(day=1)
                await self.state.set_setting(NOTES_OFF_KEY, _iso(next_month))
                if not limit:
                    logger.info("LinkedIn: the invite note box wasn't found; invites go without a note "
                                "until next month.")
                await self.driver.click_button(r"^(dismiss|close|got it|not now|cancel)$")
                ok, _ = await self._open_connect(await self.driver.html(), lead)
                if not ok:
                    return False, ("LinkedIn's note limit is reached and the invite couldn't reopen" if limit
                                   else "the note box wasn't there and the invite couldn't reopen")
            else:
                if clean_text(await self.driver.value_of(INVITE_NOTE_BOX)) != clean_text(note):
                    await self.driver.clear(INVITE_NOTE_BOX)
                    await self.driver.click_button(r"^(dismiss|close|cancel)$", within=INVITE_DIALOG)
                    return False, "the note box didn't hold exactly the note, so nothing was sent"
                if await self._held():      # stopped while typing: it stays approved
                    await self.driver.clear(INVITE_NOTE_BOX)
                    await self.driver.click_button(r"^(dismiss|close|cancel)$", within=INVITE_DIALOG)
                    return False, ""
                if not await self.driver.click_button(r"^send( invitation| now)?$",
                                                      within=INVITE_DIALOG):
                    return False, "couldn't press Send on the invite"
                sent_note = note
        if not sent_note:
            if await self._held():
                await self.driver.click_button(r"^(dismiss|close|cancel)$", within=INVITE_DIALOG)
                return False, ""
            if not (await self.driver.click_button(r"^send without a note$", within=INVITE_DIALOG)
                    or await self.driver.click_button(r"^send( now| invitation)?$",
                                                      within=INVITE_DIALOG)):
                await self.driver.click_button(r"^(dismiss|close|cancel)$", within=INVITE_DIALOG)
                return False, "couldn't press Send on the invite"
        await self.driver.wait(2)
        html = await self.driver.html()
        await self._guard(html)
        if not invite_confirmed(html, lead.get("name") or ""):
            await self.driver.wait(3)
            html = await self.driver.html()
        if not invite_confirmed(html, lead.get("name") or ""):
            return False, (MAYBE_SENT + "Send was pressed but LinkedIn didn't confirm the invite. "
                           "Check their profile before inviting again.")
        if sent_note != item["body"]:
            await self.state.li_update_message(item["id"], body=sent_note)
        await self.state.li_mark_sent(item["id"], at=self.now())
        return True, ""


class LinkedInRunner:
    """The heartbeat's hook: starts a LinkedIn run in the background when
    one is due (it takes far longer than a cycle), never two at once."""

    def __init__(self, brain, state, config, agent_factory=None, kept=None):
        self.brain, self.state, self.config = brain, state, config
        # One Chrome window, kept open between runs (owner, 2026-11-18). The
        # page is left where the run ended: LinkedIn's inbox is read by matching
        # text, not unread marks, so an open conversation hides nothing.
        self.kept = kept or KeptBrowser(state, "linkedin", self._open_window,
                                        wanted=self._window_wanted)
        self.agent_factory = agent_factory or (
            lambda: LinkedInAgent(brain, state, config, driver_factory=self.kept.lease))
        self.task: asyncio.Task | None = None

    def _open_window(self):
        from harvey.linkedin.session import open_session
        return open_session(self.config.channels.linkedin)

    async def _window_wanted(self) -> bool:
        return (await channel_on(self.state, self.config)
                and not await channel_login.in_progress(self.state, "linkedin"))

    @property
    def running(self) -> bool:
        return self.task is not None and not self.task.done()

    async def maybe_start(self, replies_only: bool = False) -> str:
        """Start a run if one is due, else (channels.linkedin.replies_any_time)
        a replies-only inbox check if one is. ``replies_only``: only that check
        (quiet hours, a tight Claude budget). Returns why not, or "started"."""
        if not await channel_on(self.state, self.config):
            if not self.running:
                await self.kept.close_if_idle()
            return "off"
        if self.running:
            return "a run is going"
        agent = self.agent_factory()
        why = "" if replies_only else await agent.why_not_now()
        if not why and not replies_only:
            logger.info("LinkedIn: starting a run in the background.")
            self.task = asyncio.create_task(agent.run())
            return "started"
        if getattr(self.config.channels.linkedin, "replies_any_time", False):
            check = await agent.why_not_now(replies_only=True)
            if not check:
                self.task = asyncio.create_task(agent.run(replies_only=True))
                return "started (inbox check)"
        return why or "no inbox check due"

    async def shutdown(self):
        if self.running:
            self.task.cancel()
            try:
                await self.task
            except (asyncio.CancelledError, Exception):
                pass
            await self.state.set_setting(RUNNING_KEY, "")
        await self.kept.close()
