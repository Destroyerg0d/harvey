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
run and pauses LinkedIn for 48 hours (harvey/linkedin/guards.py).
"""

import asyncio
import json
import logging
import random
import re
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from harvey.linkedin import guards
from harvey.linkedin.guards import BreakerTripped, active_pause, detect_breaker
from harvey.linkedin.pacing import Pacing
from harvey.linkedin.pages import (
    COMPOSER, CONNECTIONS_URL, CONVERSATION_LIST, FEED_URL, INVITE_NOTE_BOX, MESSAGING_URL,
    SEND_BUTTON, SENT_INVITES_URL, clean_text, compose_link_from_profile, invite_confirmed,
    names_match, note_limit_reached, parse_connections, parse_conversation_list,
    parse_sent_invitations, parse_thread, profile_connect_state, profile_url,
    recipient_confirmed, signed_in_from,
)
from harvey.linkedin.store import HELD_FOR_REVIEW, MAYBE_SENT, _iso
from harvey.untrusted import UNTRUSTED_RULE, fence

logger = logging.getLogger("harvey.linkedin")

LAST_RUN_KEY = "linkedin:last_run"
RUNNING_KEY = "linkedin:running"
AUTOPILOT_KEY = "linkedin:autopilot"
NOTES_OFF_KEY = "linkedin:notes_unavailable_until"

DECLINE_INTENTS = {"not_interested", "unsubscribe"}
REPLY_INTENTS = {"interested", "question", "objection", "wrong_person"}
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

    def now(self) -> datetime:
        """Naive UTC, as stored."""
        return self._now().astimezone(timezone.utc).replace(tzinfo=None)

    # ── When it may run ──

    async def why_not_now(self, ignore_interval: bool = False) -> str:
        if not self.cfg.enabled:
            return "LinkedIn is off (channels.linkedin.enabled is false)"
        pause = await active_pause(self.state, self.now())
        if pause:
            return "LinkedIn is paused: " + str(pause.get("reason") or "paused")
        if not self.pacing.in_hours():
            return f"outside LinkedIn hours ({self.pacing.hours_text()})"
        running = _parse(await self.state.get_setting(RUNNING_KEY))
        if running and self.now() - running < timedelta(minutes=self.cfg.max_run_minutes + 15):
            return "a LinkedIn run is already going"
        if not ignore_interval:
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

    async def run(self, dry_run: bool = False, ignore_interval: bool = False) -> dict:
        reason = await self.why_not_now(ignore_interval)
        if reason:
            return {"ran": False, "reason": reason}
        self.dry_run = dry_run
        self._failed_leads = set()
        started = self.now()
        self.deadline = started + timedelta(minutes=self.cfg.max_run_minutes)
        self.report = {"ran": True, "dry_run": dry_run, "started": _iso(started), "outcome": "",
                       "inbound": 0, "accepted": 0, "withdrawn": 0, "declined": 0, "drafted": 0,
                       "invite_drafts": 0, "sent_messages": 0, "sent_invites": 0, "failed": 0,
                       "would_send": []}
        await self.state.set_setting(RUNNING_KEY, _iso(started))
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
        except Exception as e:
            self.report["outcome"] = f"error: {e}"
            logger.error(f"LinkedIn run failed: {e}", exc_info=True)
        finally:
            self.driver = None
            self.report["finished"] = _iso(self.now())
            await self.state.set_setting(RUNNING_KEY, "")
            await self.state.set_setting(LAST_RUN_KEY, json.dumps(
                {k: v for k, v in self.report.items() if k != "would_send"}))
            try:
                await self.state.log_action("linkedin_run", "linkedin",
                                            {k: v for k, v in self.report.items()})
            except Exception:
                pass
        return self.report

    async def _steps(self):
        await self.pacing.prune()
        await self._check_signed_in()
        await self.sync_inbox()
        await self.sync_connections()
        await self.sync_sent_invites()
        await self.handle_inbound()
        await self.draft_messages()
        await self.pick_invites()
        await self.send_approved()

    def _time_left(self) -> bool:
        return self.deadline is None or self.now() < self.deadline

    async def _guard(self, html: str):
        breaker = detect_breaker(self.driver.url, html)
        if breaker:
            await guards.trip(self.state, breaker, hours=self.cfg.pause_hours_on_warning)
            raise BreakerTripped(breaker)

    async def _visit(self, url: str, pause: bool = True) -> str:
        if pause:
            await self.pacing.between_pages()
        await self.driver.goto(url)
        html = await self.driver.html()
        await self._guard(html)
        return html

    async def _check_signed_in(self):
        html = await self._visit(FEED_URL, pause=False)
        if not signed_in_from(self.driver.url, html):
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
        known_out = {clean_text(m["body"]) for m in mine if m["direction"] == "out"}
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
            elif message.sender and body not in known_out and not any(
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
            await self.pacing.between_actions()
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
        return await Handler._classify_intent(self, text, self._as_prospect(lead))

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
            if intent == "interested":
                await self.state.li_set_stage(lead["id"], "warm")
            if intent in REPLY_INTENTS or intent == "escalate":
                await self.stop_email_pitch(lead, keep=referred["keep"])
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
            body = await self.draft(lead, "reply", intent=message["intent"], history=history,
                                    note=message.get("referral_note") or "")
            if body:
                hold = []
                if asks_if_automated(message["body"]):
                    hold.append("they asked whether this is automated")
                await self.add_draft(lead, "reply", body, hold=hold,
                                     thread_url=message.get("thread_url") or "")

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
            if lead.get("no_message_reason") or await self.state.li_pending_for(
                    lead["id"], kinds=("message", "reply")):
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
            history = await self.state.li_messages(lead_id=lead["id"])
            body = await self.draft(lead, which, history=history)
            if body and await self.add_draft(lead, "message", body):
                room -= 1

    # ── 4. Invites ──

    async def notes_allowed(self) -> bool:
        if not self.cfg.invite_notes:
            return False
        until = _parse(await self.state.get_setting(NOTES_OFF_KEY))
        return not (until and until > self.now())

    async def invite_candidates(self, limit: int) -> list[dict]:
        titles = [t.strip().lower() for t in self.cfg.invite_titles if t.strip()]
        if not titles or limit <= 0:
            return []
        title_sql = " OR ".join("lower(p.title) LIKE ?" for _ in titles)
        stopped = sorted(EMAIL_STOPPED)
        rows = await self.state._li_query(
            "SELECT p.id, p.first_name, p.last_name, p.title, p.linkedin_url, p.company_id, "
            "p.company, COALESCE(c.name, '') AS company_name FROM prospects p "
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
        if room <= 0 or not self._time_left():
            return
        notes = await self.notes_allowed()
        if notes and self._brain_paused():
            return
        for row in await self.invite_candidates(room):
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

    def _history(self, history) -> str:
        out = []
        for m in (history or [])[-12:]:
            who = "Them" if m["direction"] == "in" else "You"
            kind = " (connection note)" if m["kind"] == "invite_note" else ""
            if m["status"] == "sent" and m["body"]:
                out.append(f"{who}{kind}: {m['body']}")
        return "\n\n".join(out)

    async def draft(self, lead: dict, which: str, intent: str = "", history=None,
                    note: str = "") -> str:
        """One message by the brain: which = invite | first | follow_up | reply.
        ``note``: what a reply must say (a referral's thank-you)."""
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
        task = {
            "invite": (f"Write the note for a LinkedIn connection request to this person. At most "
                       f"{self.cfg.invite_note_max_chars} characters, one or two sentences, a reason to "
                       "connect that is about them. No pitch, no link, no question about a call."),
            "first": ("They accepted the connection request. Write the first message: value first, "
                      "one specific, useful observation from the facts, and one easy question."),
            "follow_up": ("They haven't answered the first message. Write one short follow-up, "
                          "a different angle or something useful, no guilt, no \"bumping this\"."),
            "reply": (f"They replied (read as: {intent}). Answer what they said. "
                      + (f"If they ask to talk, the booking link is {booking}. " if booking else "")
                      + "If they ask whether this is automated, say yes, honestly."),
        }[which]
        prompt += f"""

{UNTRUSTED_RULE}

FACTS about them:
{fence(await self._facts(lead), "their profile and company")}
"""
        if history:
            prompt += f"\nTHE CONVERSATION SO FAR:\n{fence(self._history(history), 'the conversation')}\n"
        if note:
            task += "\nThe reply:\n" + note
        prompt += f"\nTASK: {task}\n\nOutput ONLY the message text."
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
            reasons = list(hold or []) + (autopilot_review("", body, self.config) if body else [])
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

    # ── 5. Sending ──

    async def send_approved(self):
        order = {"reply": 0, "message": 1, "invite_note": 2}
        items = sorted(await self.state.li_messages(statuses=("approved",), direction="out"),
                       key=lambda m: (order.get(m["kind"], 3), m["created_at"] or ""))
        for item in items:
            if not self._time_left() or not self.pacing.in_hours():
                break
            if item["lead_id"] in self._failed_leads:
                continue
            lead = await self.state.li_get_lead(item["lead_id"])
            if lead is None:
                continue
            if lead["stage"] == "declined":
                await self.state.li_update_message(item["id"], status="skipped",
                                                   error="they declined")
                continue
            if item["kind"] == "message" and (lead.get("last_inbound_at") or "") > (
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
                if "hour" in blocked:
                    break
                continue
            if self.dry_run:
                self.report["would_send"].append(
                    {"kind": item["kind"], "lead": lead.get("name"), "body": item["body"]})
                continue
            await self.pacing.between_actions()
            if kind == "invite":
                ok, detail = await self.send_invite(lead, item)
            else:
                ok, detail = await self.send_message(lead, item)
            if ok:
                await self.pacing.record(kind)
                self.report["sent_invites" if kind == "invite" else "sent_messages"] += 1
            elif detail:
                self._failed_leads.add(lead["id"])
                self.report["failed"] += 1
                await self.state.li_update_message(item["id"], status="failed", error=detail)
                logger.warning(f"LinkedIn: not sent to {lead.get('name')}: {detail}")

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
        if item["kind"] == "message":
            thread = parse_thread(html)
            if any(names_match(m.sender, name) for m in thread.messages):
                if await self.record_thread(lead, thread, self.driver.url):
                    return False, "they had written to you on LinkedIn; read it before sending this"
        if not await self.driver.type_text(COMPOSER, body):
            return False, "couldn't find the message box"
        typed = clean_text(await self.driver.text_of(COMPOSER))
        if typed != clean_text(body):
            await self.driver.clear(COMPOSER)
            return False, "the message box didn't hold exactly the draft, so it wasn't sent"
        if not await self.driver.click(SEND_BUTTON):
            await self.driver.clear(COMPOSER)
            return False, "couldn't press Send"
        want = clean_text(body)
        for _ in range(6):
            await self.driver.wait(1.5)
            html = await self.driver.html()
            await self._guard(html)
            last = parse_thread(html).last()
            if last and clean_text(last.body).startswith(want):
                break
        else:
            return False, (MAYBE_SENT + "Send was pressed but the thread didn't show the message. "
                           "Check the conversation on LinkedIn before sending anything else.")
        sent_at = self.now()
        await self.state.li_mark_sent(item["id"], thread_url=self.driver.url, at=sent_at)
        if item["kind"] == "message":
            wait = self.rng.uniform(self.cfg.follow_up_min_days, self.cfg.follow_up_max_days)
            await self.state.li_update_lead(lead["id"], next_action_at=sent_at + timedelta(days=wait))
        return True, ""

    async def _open_connect(self, html: str) -> tuple[bool, str]:
        state = profile_connect_state(html)
        if state == "connect":
            ok = await self.driver.click_button(r"^(invite .+ to connect|connect)$", within="main")
        elif state == "more":
            ok = await self.driver.click_button(r"^more( actions)?$", within="main") and \
                await self.driver.click_button(r"^(invite .+ to connect|connect)$")
        else:
            return False, state
        return ok, state

    async def send_invite(self, lead: dict, item: dict) -> tuple[bool, str]:
        html = await self._visit(lead.get("profile_url") or profile_url(lead["slug"]), pause=False)
        state = profile_connect_state(html)
        if state == "pending":
            await self.state.li_update_message(item["id"], status="skipped",
                                               error="an invite to them was already pending")
            await self.state.li_set_stage(lead["id"], "invited")
            return False, ""
        if state == "connected":
            await self.state.li_update_message(item["id"], status="skipped",
                                               error="you're already connected")
            await self.state.li_set_stage(lead["id"], "accepted", accepted_at=self.now())
            return False, ""
        ok, _ = await self._open_connect(html)
        if not ok:
            return False, "couldn't find a Connect button on their profile"
        html = await self.driver.html()
        await self._guard(html)
        note = item["body"] if await self.notes_allowed() else ""
        sent_note = ""
        if note and await self.driver.click_button(r"^add a note$"):
            html = await self.driver.html()
            await self._guard(html)
            if note_limit_reached(html):
                first = self.now().replace(day=1, hour=0, minute=0, second=0, microsecond=0)
                next_month = (first + timedelta(days=32)).replace(day=1)
                await self.state.set_setting(NOTES_OFF_KEY, _iso(next_month))
                await self.driver.click_button(r"^(dismiss|close|got it|not now)$")
                ok, _ = await self._open_connect(await self.driver.html())
                if not ok:
                    return False, "LinkedIn's note limit is reached and the invite couldn't reopen"
            else:
                if not await self.driver.type_text(INVITE_NOTE_BOX, note):
                    return False, "couldn't find the invite note box"
                if clean_text(await self.driver.value_of(INVITE_NOTE_BOX)) != clean_text(note):
                    await self.driver.clear(INVITE_NOTE_BOX)
                    await self.driver.click_button(r"^(dismiss|close|cancel)$")
                    return False, "the note box didn't hold exactly the note, so nothing was sent"
                if not await self.driver.click_button(r"^send( invitation| now)?$",
                                                      within="[role='dialog']"):
                    return False, "couldn't press Send on the invite"
                sent_note = note
        if not sent_note:
            if not (await self.driver.click_button(r"^send without a note$")
                    or await self.driver.click_button(r"^send( now| invitation)?$",
                                                      within="[role='dialog']")):
                return False, "couldn't press Send on the invite"
        await self.driver.wait(2)
        html = await self.driver.html()
        await self._guard(html)
        if not invite_confirmed(html):
            return False, (MAYBE_SENT + "Send was pressed but LinkedIn didn't confirm the invite. "
                           "Check their profile before inviting again.")
        if sent_note != item["body"]:
            await self.state.li_update_message(item["id"], body=sent_note)
        await self.state.li_mark_sent(item["id"], at=self.now())
        return True, ""


class LinkedInRunner:
    """The heartbeat's hook: starts a LinkedIn run in the background when
    one is due (it takes far longer than a cycle), never two at once."""

    def __init__(self, brain, state, config, agent_factory=None):
        self.brain, self.state, self.config = brain, state, config
        self.agent_factory = agent_factory or (lambda: LinkedInAgent(brain, state, config))
        self.task: asyncio.Task | None = None

    @property
    def running(self) -> bool:
        return self.task is not None and not self.task.done()

    async def maybe_start(self) -> str:
        """Start a run if one is due. Returns why not, or "started"."""
        if not getattr(self.config.channels.linkedin, "enabled", False):
            return "off"
        if self.running:
            return "a run is going"
        agent = self.agent_factory()
        why = await agent.why_not_now()
        if why:
            return why
        logger.info("LinkedIn: starting a run in the background.")
        self.task = asyncio.create_task(agent.run())
        return "started"

    async def shutdown(self):
        if self.running:
            self.task.cancel()
            try:
                await self.task
            except (asyncio.CancelledError, Exception):
                pass
            await self.state.set_setting(RUNNING_KEY, "")
