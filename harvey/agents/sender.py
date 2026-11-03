"""Sender — deploys campaigns via Instantly, or sends natively via the
configured mail provider (Gmail / SMTP) through the outbox approval ladder.

Native flow:
  1. Draft campaigns are STAGED: merge variables rendered per prospect,
     one outbox row per (prospect, step) with a scheduled send_at.
     Rows start as pending_review (copilot) or approved (autopilot).
  2. Each heartbeat DRAINS due approved rows: kill-switch check, stop-on-
     reply check, deterministic pre-send gate, then provider.send_email
     with jittered pacing and the daily cap.
"""

import asyncio
import logging
import random
import re
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone

from harvey.brain import Brain
from harvey.config import (
    HarveyConfig, EnvConfig, WarmupConfig, cold_daily_cap, compliance_footer_lines,
    compliance_notice, compliance_of, live_do_not_contact, operator_timezone, send_window_of,
)
from harvey.gate import autopilot_review, do_not_contact_match, pre_send_check
from harvey.send_window import in_window, next_send_time, recipient_country, recipient_zone
from harvey.integrations.instantly import InstantlyClient
from harvey.integrations.mail_provider import NATIVE_PROVIDERS, get_mail_provider
from harvey.integrations.precleared import PreclearedClient, clear_hold, record_hold
from harvey.mailboxes import (
    Mailbox, MailboxProviders, companies_of, configured_mailboxes, connection_problem,
    down_since_key, is_multi, least_loaded, mailbox_for, normalize, pause_problem,
    primary_mailbox, render_sender, takes_new_prospects,
)
from harvey.segment_mix import build_mix, queued_segments, update_ramp, weights_of
from harvey.segment_stamp import segment_for_company, variant_for
from harvey.sending_mode import sync_sending_mode
from harvey.state import (
    AUTOPILOT_OFF_REASON, COLLEAGUE_REPLIED, HAND_SENT_REASON, HELD_FOR_REVIEW,
    MANUAL_SEND_KEY, MAYBE_SENT_PREFIX, RECHECK_PREFIX, StateManager,
)

# A follow-up waits at least this long after the email it follows went out,
# whatever its schedule says.
MIN_FOLLOW_UP_GAP = timedelta(days=1)

logger = logging.getLogger("harvey.sender")

# Max sends drained per heartbeat cycle — spreads volume through the day
# instead of bursting the daily cap in one minute.
MAX_SENDS_PER_CYCLE = 8
# With several mailboxes: each sends at most this many a cycle, taking turns,
# and all of them together at most MULTI_SENDS_PER_CYCLE. With the pacing
# below that stays well inside a cycle's ten minutes, and a mailbox that can't
# send (capped, not signed in, paused) never holds up the others.
MAILBOX_SENDS_PER_CYCLE = 3
MULTI_SENDS_PER_CYCLE = 12
# Gmail's answers to a send that mean the mailbox is the problem, not the
# email: the sign-in was refused (401/403) or it is being rate limited (429).
# Nothing went out, so the email waits and the mailbox rests for the cycle.
MAILBOX_REFUSED_STATUSES = (401, 403, 429)
# A mailbox down this long (not signed in, wrong account, refused) gives
# the prospects nothing has gone to yet to another mailbox.
MOVE_AFTER_DOWN = timedelta(hours=24)
# How many due emails quiet hours look through for one that can go now.
QUIET_HOURS_SCAN = 200
# With a segment mix (channels.email.segment_weights): how many due emails a
# drain reads per mailbox, so each segment has first emails to choose from.
MIX_SCAN = 100
SEND_JITTER_SECONDS = (4, 15)
# Optional Precleared check: an email whose address Precleared couldn't
# check is moved back this far, behind other email, rather than asked about
# again every cycle. Failures for this many different domains with no answer
# in between (across cycles) mean Precleared itself is in trouble, so checks
# pause. And no new check starts after this many seconds of checking in one
# cycle.
ADDRESS_RETRY_AFTER = timedelta(hours=6)
DOMAINS_FAILING_TO_PAUSE = 2
CHECK_SECONDS_PER_CYCLE = 90

# Prospect pipeline statuses that mean "stop emailing this person".
STOP_STATUSES = {"replied", "opted_out", "lost", "meeting", "closed"}

KILL_SWITCH_KEY = "sending_paused"
BOUNCE_COUNT_KEY = "bounce_count"

EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}$")

# Only prospects in these statuses may be added as leads. This guarantees
# we never re-send to someone already contacted, replied, opted out, or lost.
SENDABLE_STATUSES = {"new", "queued"}

# Email confidence levels safe to send to. "verified" only by default;
# "risky" (catch-all domains) is opt-in via config. Never send to a "guess"
# or "invalid" address — that bounces and burns the sending domain.
SENDABLE_EMAIL_STATUSES = {"verified"}
SENDABLE_EMAIL_STATUSES_WITH_RISKY = {"verified", "risky"}


def _utcnow() -> datetime:
    """Naive UTC now, as the outbox stores times. Tests pin it."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _local_midnight_utc() -> str:
    """Today's local midnight as a naive UTC ISO time: the day the warmup
    ramp counts in."""
    midnight = datetime.now().astimezone().replace(hour=0, minute=0, second=0, microsecond=0)
    return midnight.astimezone(timezone.utc).replace(tzinfo=None).isoformat()


@dataclass
class _Lane:
    """One mailbox's share of a drain (several mailboxes)."""
    mailbox: Mailbox
    provider: object
    daily_left: int          # sends left in its last 24 hours
    cold_left: int           # cold email left today under its warmup ramp
    cold_held: str = ""      # why its cold email waits this cycle
    sent: int = 0
    sent_cold: int = 0
    stopped: str = ""        # why it can't send for the rest of this cycle


def _in_turn(queues: list[list]) -> list:
    """The rows of each mailbox's queue taken in turn: the first of each,
    then the second of each, and so on."""
    out = []
    for i in range(max((len(q) for q in queues), default=0)):
        out += [q[i] for q in queues if i < len(q)]
    return out


_WORD_RE = re.compile(r"[^\W\d_][\w'’.-]*")


class Sender:
    def __init__(
        self,
        brain: Brain,
        state: StateManager,
        config: HarveyConfig,
        env: EnvConfig,
    ):
        self.brain = brain
        self.state = state
        self.config = config
        self.instantly = InstantlyClient(env.instantly_api_key)
        self.provider = get_mail_provider(config, env)
        # With several mailboxes, each one's own provider (self.provider is
        # then only the primary's, for its name and the single-mailbox path).
        self.mailbox_providers = MailboxProviders(config, env)
        # Set by the heartbeat from this cycle's reply reading: cold email to
        # these prospects, or from these mailboxes, waits for a later cycle,
        # because an inbox that could hold their reply wasn't read completely.
        self.hold_prospects: set[str] = set()
        self.hold_mailboxes: set[str] = set()
        # Optional per-recipient compliance check; None unless enabled.
        self.precleared = PreclearedClient.from_config(config, env)
        # Domains whose check failed since Precleared last answered. Kept
        # across cycles: one new address a cycle failing is still a pattern.
        self._failing_domains: set[str] = set()
        # Disabled in tests to skip inter-send sleeps.
        self.send_pacing = True
        # Set by the heartbeat in quiet hours (send windows in recipients'
        # zones): only cold email goes, inside its window; replies wait for
        # the operator's own hours, as they always have.
        self.cold_only = False
        # The last drain couldn't ask Precleared: quiet hours wake only for
        # cold email it has already cleared until a drain outside them has run.
        self.compliance_held = False

    @property
    def is_native(self) -> bool:
        return (
            self.config.channels.email.provider in NATIVE_PROVIDERS
            and self.provider is not None
        )

    async def _send_zone(self, prospect, country: str = "", given: str = "",
                         names: tuple[str, str] = ("", "")) -> str:
        """The time zone this recipient's send window is in: their own when
        it can be told (with recipient_timezone on), otherwise the zone in
        usage.quiet_hours, as before. ``country`` is the recipient's country
        from Precleared, ``given`` the one Harvey gave the check (see
        _recipient_country) and ``names`` the company and person names it
        gave (see _recipient_names), which a cached verdict is keyed on."""
        window = send_window_of(self.config)
        if window is not None and window.recipient_timezone:
            if not country and self.precleared is not None and prospect is not None:
                # A country Precleared already found counts on every check,
                # not only on the one that asked it.
                company, name = names
                country = await self.precleared.known_country(
                    prospect.email, self.state, given, company=company, name=name)
            zone = await recipient_zone(self.state, prospect, self.config, country)
            if zone:
                return zone
        return operator_timezone(self.config)

    async def _zone_or_default(self, prospect, country: str = "", given: str = "",
                               names: tuple[str, str] = ("", "")) -> str:
        """_send_zone, but a failure to work the zone out falls back to
        usage.quiet_hours.timezone for this one email instead of stopping
        the whole drain (replies included)."""
        try:
            return await self._send_zone(prospect, country, given, names)
        except Exception as e:
            logger.warning(
                f"Sender: couldn't work out the time zone for "
                f"{getattr(prospect, 'email', '?')}, using quiet_hours.timezone: {e}")
            return operator_timezone(self.config)

    async def _recipient_country(self, prospect) -> str:
        """The recipient's country when Harvey is sure of it, for the
        compliance check (see send_window.recipient_country). A failure to
        work it out means none for this one email, which Precleared then
        works out itself, instead of stopping the drain."""
        try:
            return await recipient_country(self.state, prospect)
        except Exception as e:
            logger.warning(
                f"Sender: couldn't work out the country for "
                f"{getattr(prospect, 'email', '?')}, leaving it to Precleared: {e}")
            return ""

    async def _recipient_names(self, prospect) -> tuple[str, str]:
        """The company's name and the person's full name, for the compliance
        check: the company record's name, else the prospect's own company
        field, and the prospect's first and last name. A company inbox has no
        person, so no name. A failure to load the company record falls back
        to the prospect's own field for this one email instead of stopping
        the drain."""
        if prospect is None:
            return "", ""
        company = ""
        if getattr(prospect, "company_id", ""):
            try:
                record = await self.state.get_company(prospect.company_id)
                company = (getattr(record, "name", "") or "") if record else ""
            except Exception as e:
                logger.warning(
                    f"Sender: couldn't load the company for "
                    f"{getattr(prospect, 'email', '?')}, using the prospect's own: {e}")
        company = company.strip() or (getattr(prospect, "company", "") or "").strip()
        name = ""
        if not getattr(prospect, "is_company_inbox", False):
            first = (getattr(prospect, "first_name", "") or "").strip()
            last = (getattr(prospect, "last_name", "") or "").strip()
            name = f"{first} {last}".strip()
        return company, name

    async def _has_cached_verdict(self, item: dict) -> bool:
        """Use the send loop's recipient facts when counting held email."""
        prospect = await self.state.get_prospect(item["prospect_id"])
        country = await self._recipient_country(prospect)
        company, name = await self._recipient_names(prospect)
        return await self.precleared.cached(
            item["to_email"], self.state, country, company=company, name=name) is not None

    async def cold_due_in_window(self) -> bool:
        """Quiet hours: whether a cold email is due, inside its recipient's
        send window and free to go now (sending not paused, under the daily
        and warmup caps, and after a compliance hold only to someone
        Precleared has already cleared). Only then is it worth waking to read
        replies and send."""
        window = send_window_of(self.config)
        if window is None or not self.is_native:
            return False
        due_before = datetime.now(timezone.utc).replace(tzinfo=None).isoformat()
        if is_multi(self.config):
            # Each mailbox that can send cold email now, under its own caps.
            if await self.state.get_setting(KILL_SWITCH_KEY):
                return False
            lanes, _ = await self._mailbox_lanes(record=False)
            primary = primary_mailbox(self.config).email
            due = []
            for lane in lanes.values():
                if lane.daily_left > 0 and lane.cold_left > 0:
                    due += await self._quiet_hours_rows(
                        due_before, mailboxes=[lane.mailbox.email], primary=primary)
        else:
            if not self.provider.is_configured() or await self.state.get_setting(KILL_SWITCH_KEY):
                return False
            email = self.config.channels.email
            if email.max_daily_sends - await self.state.count_outbox_sent_today() <= 0:
                return False
            if cold_daily_cap(email) - await self.state.count_outbox_sent_today(
                    kind="sequence", since=_local_midnight_utc()) <= 0:
                return False
            due = await self._quiet_hours_rows(due_before)
        now = _utcnow()
        for item in due:
            if item["kind"] != "sequence":
                continue
            prospect = await self.state.get_prospect(item["prospect_id"])
            country = await self._recipient_country(prospect) if self.precleared is not None else ""
            names = await self._recipient_names(prospect) if self.precleared is not None else ("", "")
            if self.compliance_held and self.precleared is not None:
                # New addresses wait for a drain outside quiet hours; a
                # stored verdict still lets email go, as in the drain.
                clearance = await self.precleared.cached(
                    item["to_email"], self.state, country, company=names[0], name=names[1])
                if clearance is None or not clearance.allowed:
                    continue
            if prospect is not None and in_window(
                    now, await self._zone_or_default(prospect, given=country, names=names), window):
                return True
        return False

    async def _quiet_hours_rows(self, due_before: str, mailboxes=None, primary: str = "") -> list[dict]:
        """Due cold email for cold_due_in_window (from these mailboxes when
        given): approved first emails and follow-ups that are ready."""
        where = {"mailboxes": mailboxes, "primary": primary} if mailboxes is not None else {}
        due = await self.state.get_outbox(
            status="approved", due_before=due_before,
            limit=QUIET_HOURS_SCAN, skip_unready_follow_ups=True, kind="sequence", **where,
        )
        if self.compliance_held and self.precleared is not None:
            # Match the drain's separate follow-up scan: waiting first emails
            # must not hide a cleared follow-up and prevent the loop waking.
            seen = {item["id"] for item in due}
            follow_ups = await self.state.get_outbox(
                status="approved", due_before=due_before,
                limit=QUIET_HOURS_SCAN, skip_unready_follow_ups=True, follow_ups_only=True,
                **where,
            )
            due += [item for item in follow_ups if item["id"] not in seen]
        return due

    async def _outside_window(self, item: dict, zone: str, window) -> bool:
        """Move a cold email that isn't in its send window to the window's
        next opening. True when it was moved and must wait."""
        now = _utcnow()
        if in_window(now, zone, window):
            return False
        later = next_send_time(now, zone, window)
        await self.state.update_outbox_item(item["id"], send_at=later.isoformat())
        return True

    async def run(self):
        """Deploy campaigns — natively through the outbox, or via Instantly."""
        if not self.config.channels.email.enabled:
            logger.info("Sender: Email channel disabled. Skipping.")
            return

        if self.is_native:
            await self._run_native()
            return

        logger.info("Sender: Checking for campaigns to deploy...")
        draft_campaigns = await self.state.get_campaigns_by_status("draft")
        if not draft_campaigns:
            logger.info("Sender: No draft campaigns to deploy.")
            return

        # Enforce daily send limit
        max_sends = self.config.channels.email.max_daily_sends
        sends_today = await self._count_sends_today()
        remaining = max_sends - sends_today
        if remaining <= 0:
            logger.info(f"Sender: Daily send limit reached ({sends_today}/{max_sends}). Skipping.")
            return

        for campaign in draft_campaigns:
            try:
                await self._deploy_campaign(campaign)
            except Exception as e:
                logger.error(f"Sender: Failed to deploy campaign {campaign.name}: {e}")

    def _validate_sequence(self, campaign) -> bool:
        """Never deploy a broken or empty sequence."""
        if not campaign.sequence:
            logger.error(f"Sender: Campaign '{campaign.name}' has no email sequence. Marking failed.")
            return False
        for step in campaign.sequence:
            if not (step.subject or "").strip() or not (step.body or "").strip():
                logger.error(
                    f"Sender: Campaign '{campaign.name}' step {step.step} has an empty "
                    "subject or body. Marking failed."
                )
                return False
            if step.delay_days < 0:
                logger.error(
                    f"Sender: Campaign '{campaign.name}' step {step.step} has a negative delay."
                )
                return False
        return True

    def _reviewed_leads(self, prospects: list) -> list:
        """The prospects whose merge fields pass the autopilot review."""
        kept = []
        for p in prospects:
            fields = " \n".join(str(v or "") for v in (
                p.first_name, p.last_name, p.company, p.title, p.personalization_notes))
            held = autopilot_review("", fields, self.config, recipient=p.email)
            if listed := do_not_contact_match(p.email, live_do_not_contact(self.config)):
                held = [f"'{listed}' is on the do-not-contact list", *held]
            if held:
                logger.warning(f"Sender: left {p.email} out of the Instantly upload: {held}")
            else:
                kept.append(p)
        return kept

    async def _deploy_campaign(self, campaign):
        """Deploy a single campaign to Instantly. Idempotent: safe to retry."""
        # Instantly owns campaign scheduling and has no per-message outbox
        # approval here. Fail closed rather than silently bypass the setting,
        # including retries of an already-created remote campaign.
        await sync_sending_mode(self.config, self.state)
        if getattr(self.config.channels.email, "require_approval", True):
            logger.warning(
                "Sender: Instantly campaign held because require_approval is true. "
                "Use a native mail provider for outbox review, or explicitly set "
                "channels.email.require_approval: false for Instantly autopilot."
            )
            return
        logger.info(f"Sender: Deploying campaign '{campaign.name}'...")

        # 0. Validate before touching the network
        if not self._validate_sequence(campaign):
            await self.state.update_campaign(campaign.id, status="failed")
            return
        # Instantly only deploys on autopilot, so nobody has read these
        # templates: what a planted instruction would add holds the campaign.
        held = [f"step {s.step} {r}" for s in campaign.sequence
                for r in autopilot_review(s.subject, s.body, self.config)]
        if held:
            logger.warning(f"Sender: Instantly campaign '{campaign.name}' held for review: {held}")
            key = "instantly_review_hold:" + campaign.id
            if not await self.state.get_setting(key):
                await self.state.set_setting(key, "1")
                await self.state.log_action(
                    action_type="escalation", agent="sender",
                    details={"campaign_name": campaign.name,
                             "reason": "campaign held: " + "; ".join(held)[:200]},
                )
            return

        # 1. Create campaign in Instantly — or resume one from a previous
        # partially-failed deploy. Never create a duplicate.
        campaign_id = campaign.instantly_campaign_id
        if campaign_id:
            logger.info(
                f"Sender: Campaign '{campaign.name}' already has Instantly ID "
                f"{campaign_id}. Resuming deploy instead of recreating."
            )
        else:
            instantly_campaign = await self.instantly.create_campaign(campaign.name)
            if not instantly_campaign or not isinstance(instantly_campaign, dict):
                logger.error(f"Sender: Failed to create Instantly campaign: {campaign.name}")
                return

            campaign_id = instantly_campaign.get("id")
            if not campaign_id:
                logger.error("Sender: No campaign ID returned from Instantly.")
                return

            # Persist the ID immediately so a crash mid-deploy resumes this
            # campaign instead of creating a second one (double-send guard).
            await self.state.update_campaign(campaign.id, instantly_campaign_id=campaign_id)
            campaign.instantly_campaign_id = campaign_id

        # 2. Set email sequence
        sequences = [
            {
                "subject": step.subject.strip(),
                "body": self._with_footer(step.body.strip()),
                "wait": step.delay_days,
            }
            for step in campaign.sequence
        ]

        await self.state.remember_footer_lines(compliance_footer_lines(self.config))
        result = await self.instantly.set_campaign_emails(campaign_id, sequences)
        if result is None:
            logger.error(f"Sender: Failed to set emails for campaign {campaign_id}")
            return

        # 3. Add leads — validated, deduped, never-contacted, deliverable-only
        allow_risky = getattr(self.config.channels.email, "send_to_risky", False)
        sendable_email = (
            SENDABLE_EMAIL_STATUSES_WITH_RISKY if allow_risky
            else SENDABLE_EMAIL_STATUSES
        )
        prospects = []
        seen_emails: set[str] = set()
        skipped_unverified = 0
        for prospect_id in campaign.prospect_ids:
            prospect = await self.state.get_prospect(prospect_id)
            if not prospect or not prospect.email:
                continue
            email = prospect.email.strip().lower()
            if not EMAIL_RE.match(email):
                logger.warning(f"Sender: Skipping invalid email '{prospect.email}'")
                continue
            if email in seen_emails:
                continue
            if prospect.status not in SENDABLE_STATUSES:
                # Already contacted / replied / opted out — never double-send.
                logger.debug(
                    f"Sender: Skipping {email} (status '{prospect.status}' is not sendable)."
                )
                continue
            if await self.state.get_setting(MANUAL_SEND_KEY + prospect.id):
                continue  # the user emailed them by hand
            # Deliverability gate: never send to unverified/guessed addresses —
            # bounces are the fastest way to torch a sending domain.
            if (prospect.email_status or "guess") not in sendable_email:
                skipped_unverified += 1
                logger.debug(
                    f"Sender: Skipping {email} (email_status "
                    f"'{prospect.email_status or 'guess'}' not deliverable)."
                )
                continue
            seen_emails.add(email)
            prospects.append(prospect)

        if skipped_unverified:
            logger.info(
                f"Sender: Held back {skipped_unverified} prospect(s) with "
                f"unverified emails from '{campaign.name}'."
            )

        if not prospects:
            # Retry path: leads were already staged and marked 'contacted'
            # on a previous cycle but activation failed. Just activate.
            already_staged = False
            for pid in campaign.prospect_ids:
                p = await self.state.get_prospect(pid)
                if p and p.status == "contacted":
                    already_staged = True
                    break
            if already_staged:
                logger.info(
                    f"Sender: Leads for '{campaign.name}' already staged. Retrying activation."
                )
                if await self.instantly.activate_campaign(campaign_id) is not None:
                    await self.state.update_campaign(campaign.id, status="active")
                    logger.info(f"Sender: Campaign '{campaign.name}' activated on retry.")
                return
            logger.warning(f"Sender: No valid prospects for campaign {campaign.name}")
            return

        # Check remaining daily send budget
        max_sends = self.config.channels.email.max_daily_sends
        sends_today = await self._count_sends_today()
        remaining = max_sends - sends_today
        if remaining <= 0:
            logger.info(f"Sender: Daily limit reached. Deferring campaign '{campaign.name}'.")
            return
        if len(prospects) > remaining:
            logger.info(f"Sender: Capping leads from {len(prospects)} to {remaining} (daily limit).")
            prospects = prospects[:remaining]

        # Instantly fills these into the templates on its side, unseen:
        # scraped text carrying a planted link keeps that lead out.
        prospects = self._reviewed_leads(prospects)
        if not prospects:
            return
        leads = [
            {
                "email": p.email.strip().lower(),
                "first_name": p.first_name,
                "last_name": p.last_name,
                "company_name": p.company,
                "variables": {
                    "title": p.title,
                    "personalization": p.personalization_notes,
                },
            }
            for p in prospects
        ]

        result = await self.instantly.add_leads(campaign_id, leads)
        if result is None:
            logger.error(f"Sender: Failed to add leads to campaign {campaign_id}")
            return

        # Mark prospects as contacted BEFORE activation: if activation
        # succeeds but this write failed, a retry would re-add the same
        # leads. Better to under-count than double-send.
        for prospect in prospects:
            await self.state.update_prospect_status(prospect.id, "contacted")

        # 4. Activate campaign
        result = await self.instantly.activate_campaign(campaign_id)
        if result is None:
            logger.error(
                f"Sender: Failed to activate campaign {campaign_id}. "
                "Leads are staged; will retry activation next cycle."
            )
            return

        # 5. Update our records
        await self.state.update_campaign(
            campaign.id,
            instantly_campaign_id=campaign_id,
            status="active",
        )

        await self.state.log_action(
            action_type="send_campaign",
            agent="sender",
            details={
                "campaign_name": campaign.name,
                "instantly_campaign_id": campaign_id,
                "leads_added": len(leads),
            },
        )

        logger.info(
            f"Sender: Campaign '{campaign.name}' deployed to Instantly "
            f"with {len(leads)} leads. Campaign ID: {campaign_id}"
        )

    async def _count_sends_today(self) -> int:
        """Count how many prospects were contacted today."""
        import aiosqlite
        today = date.today().isoformat()
        async with aiosqlite.connect(self.state.db_path) as db:
            async with db.execute(
                "SELECT COUNT(*) FROM prospects WHERE status = 'contacted' AND updated_at LIKE ?",
                (f"{today}%",),
            ) as cursor:
                row = await cursor.fetchone()
                return row[0] if row else 0

    # ── Native provider flow (Gmail / SMTP via the outbox) ──

    async def _run_native(self):
        """Stage new campaigns into the outbox, then drain what's due."""
        # Before any early return: a send cut off mid-way must show up as
        # failed even while sending is paused or no mailbox is connected.
        stale = await self.state.fail_stale_sending_outbox()
        if stale:
            logger.warning(
                f"Sender: {stale} email(s) were interrupted mid-send and won't be "
                "retried. Check your Sent folder; they're under Didn't send."
            )
        paused = await self.state.get_setting(KILL_SWITCH_KEY)
        if paused:
            logger.warning(
                f"Sender: SENDING PAUSED ({paused}). Resume from the dashboard "
                "or `harvey sending resume` once the cause is fixed."
            )
            return

        if is_multi(self.config):
            try:
                await self._move_from_down_mailboxes()
            except Exception as e:
                # Moving is housekeeping: it never stops the sending.
                logger.error(f"Sender: couldn't move prospects off a down mailbox: {e}")

        for campaign in await self.state.get_campaigns_by_status("draft"):
            try:
                await self._stage_campaign_native(campaign)
            except Exception as e:
                logger.error(f"Sender: staging '{campaign.name}' failed: {e}")

        if is_multi(self.config):
            try:
                await self._hold_mis_signed_drafts()
            except Exception as e:
                logger.error(f"Sender: couldn't check the drafts' sign-offs: {e}")

        await self._drain_due()

    # ── Several mailboxes ──

    async def _mailbox_lanes(self, record: bool = True) -> tuple[dict[str, "_Lane"], dict[str, str]]:
        """The mailboxes that can send now, by address, and why each of the
        others can't. Each lane carries what its own limits leave: its daily
        limit over the last 24 hours (replies count, but are never held by
        it) and its cold cap today (its daily limit under the warmup ramp).
        ``record``: note when a mailbox stopped working (down_since_key)."""
        primary = primary_mailbox(self.config).email
        day_ago = (_utcnow() - timedelta(hours=24)).isoformat()
        midnight = _local_midnight_utc()
        warmup = getattr(self.config.channels.email, "warmup", None)
        lanes: dict[str, _Lane] = {}
        out: dict[str, str] = {}
        for m in configured_mailboxes(self.config):
            provider = self.mailbox_providers.get(m)
            problem = connection_problem(m, provider)
            if problem:
                out[m.email] = problem
                if record:
                    await self._mark_down(m)
                continue
            paused = await pause_problem(self.state, m)
            if paused:
                out[m.email] = paused
                continue
            sent_day = await self.state.count_outbox_sent_since(
                day_ago, mailbox=m.email, primary=primary)
            cap = warmup.daily_cap(m.daily_limit) if isinstance(warmup, WarmupConfig) else m.daily_limit
            sent_cold = await self.state.count_outbox_sent_since(
                midnight, mailbox=m.email, primary=primary, kind="sequence")
            lane = _Lane(mailbox=m, provider=provider,
                         daily_left=m.daily_limit - sent_day, cold_left=cap - sent_cold)
            if m.email in self.hold_mailboxes:
                lane.cold_held = "its inbox couldn't be read completely"
            lanes[m.email] = lane
        return lanes, out

    async def _mark_down(self, mailbox: Mailbox):
        if not await self.state.get_setting(down_since_key(mailbox.email)):
            await self.state.set_setting(down_since_key(mailbox.email), _utcnow().isoformat())

    async def _mark_up(self, mailbox: Mailbox):
        if await self.state.get_setting(down_since_key(mailbox.email)):
            await self.state.delete_setting(down_since_key(mailbox.email))

    async def _move_from_down_mailboxes(self):
        """A mailbox down for MOVE_AFTER_DOWN gives the prospects nothing
        has gone to (nor to anyone at their company), and what's queued for
        them, to the least busy mailbox that can take new prospects, company
        by company. Anyone it already emailed waits for it to come back. The
        moved emails sign with their new sender (the sender tag)."""
        primary = primary_mailbox(self.config).email
        mailboxes = configured_mailboxes(self.config)
        targets = None
        for m in mailboxes:
            since = await self.state.get_setting(down_since_key(m.email))
            try:
                down_at = datetime.fromisoformat(since) if since else None
            except ValueError:
                down_at = None
            if down_at is None or _utcnow() - down_at < MOVE_AFTER_DOWN:
                continue
            movable = await self.state.movable_prospects(m.email, primary)
            if not movable:
                continue
            if targets is None:
                targets = [o for o in mailboxes
                           if not await takes_new_prospects(self.state, o, self.mailbox_providers.get(o))]
            open_ = [o for o in targets if o.email != m.email]
            if not open_:
                logger.warning(f"Sender: {m.email} has been down since {since}, and no other "
                               f"mailbox can take its {len(movable)} prospect(s); they wait.")
                continue
            for company in await companies_of(self.state, movable):
                chosen = await least_loaded(self.state, self.config, open_)
                rows = await self.state.move_prospects_to_mailbox(company, chosen.email)
                logger.warning(f"Sender: {m.email} has been down since {since}: moved "
                               f"{len(company)} prospect(s) and {rows} queued email(s) to {chosen.email}.")
                await self.state.log_action(
                    action_type="mailbox_reassigned", agent="sender",
                    details={"from": m.email, "to": chosen.email,
                             "prospects": len(company), "emails": rows})

    def _sign_off_problems(self, item: dict, mailbox: Mailbox) -> list[str]:
        """Several mailboxes: why this email (as it will read, sender tag
        filled in, without the footer) doesn't fit the mailbox it goes out
        from. A cold email must close with that sender's name (first, or
        full); no email may sign as, or introduce itself as, another of them."""
        text = (item.get("body") or "").rstrip()
        footer = "\n".join(compliance_footer_lines(self.config))
        if footer and text.endswith(footer):
            text = text[:-len(footer)].rstrip()
        lines = [line.strip() for line in text.splitlines() if line.strip()]
        if not lines:
            return []
        words = [w.strip(".,'’-").lower() for w in _WORD_RE.findall(lines[-1])]

        def signs_as(m: Mailbox) -> bool:
            # "Sam", "Thanks, Sam", "Best wishes, Sam Sample": a short closing
            # line that ends with the name.
            full = m.name.lower().split()
            if not words or not full or len(words) > 3 + len(full):
                return False
            return words[-1] == full[0] or words[-len(full):] == full

        own = mailbox.first_name
        problems = []
        if item.get("kind") == "sequence" and not signs_as(mailbox):
            problems.append(f"it doesn't sign off as {own}, the mailbox it goes out from")
        # Below the greeting: "Hi Mary," greets a prospect called Mary.
        body = "\n".join(lines[1:])
        seen = {own.lower()}
        for other in configured_mailboxes(self.config):
            if not other.first_name or other.first_name.lower() in seen:
                continue
            seen.add(other.first_name.lower())
            name = re.escape(other.first_name)
            if signs_as(other):
                problems.append(f"it signs off as {other.first_name}, but goes out from {mailbox.email}")
            elif re.search(rf"\b(?:I['’]?m|I am)\s+{name}\b|\b{name}\s+here\b", body, re.IGNORECASE):
                problems.append(f"it introduces the sender as {other.first_name}, but goes out "
                                f"from {mailbox.email}")
        return problems

    def _as_sent_from(self, item: dict, mailbox: Mailbox | None) -> dict:
        """The row with the sender tag filled in from ``mailbox`` (several
        mailboxes; unchanged with one)."""
        if mailbox is None:
            return item
        return {**item, "subject": render_sender(item.get("subject") or "", mailbox),
                "body": render_sender(item.get("body") or "", mailbox)}

    async def _hold_mis_signed_drafts(self):
        """Several mailboxes: drafts waiting for review whose sign-off
        doesn't fit their mailbox are marked held, so "Approve all" leaves
        them for a one-by-one look. Once you approve one, it's yours."""
        drafts = await self.state.get_outbox(status="pending_review", limit=500)
        for item in drafts:
            if item.get("error"):
                continue      # already says why it waits
            mailbox = mailbox_for(self.config, item.get("mailbox"))
            if mailbox is None:
                continue
            problems = self._sign_off_problems(self._as_sent_from(item, mailbox), mailbox)
            if problems:
                await self.state.update_outbox_item(
                    item["id"], error=(HELD_FOR_REVIEW + "; ".join(problems))[:300])

    def _render(self, text: str, prospect) -> str:
        """Fill merge variables. The pre-send gate rejects any leftovers."""
        replacements = {
            # A company inbox has no name; "there" keeps "Hi {{first_name}}," readable.
            "first_name": prospect.first_name or ("there" if prospect.is_company_inbox else ""),
            "last_name": prospect.last_name,
            "company": prospect.company,
            "title": prospect.title,
            "personalization": prospect.personalization_notes,
        }
        # "{{company}}'s" for "Parkes Builders" should read "Builders'".
        company = prospect.company or ""
        if company.endswith(("s", "S")):
            for tag in ("{{company}}", "{{ company }}"):
                text = text.replace(tag + "'s", company + "'")
                text = text.replace(tag + "’s", company + "’")
        for key, value in replacements.items():
            text = text.replace("{{" + key + "}}", value or "")
            text = text.replace("{{ " + key + " }}", value or "")
        return text.strip()

    async def _stage_campaign_native(self, campaign):
        """Render + schedule a draft campaign's emails into the outbox."""
        if not self._validate_sequence(campaign):
            await self.state.update_campaign(campaign.id, status="failed")
            return

        await sync_sending_mode(self.config, self.state)
        require_approval = getattr(
            self.config.channels.email, "require_approval", True
        )
        initial_status = "pending_review" if require_approval else "approved"
        allow_risky = getattr(self.config.channels.email, "send_to_risky", False)
        sendable_email = (
            SENDABLE_EMAIL_STATUSES_WITH_RISKY if allow_risky
            else SENDABLE_EMAIL_STATUSES
        )

        staged = 0
        skipped = 0
        answered = 0
        now = _utcnow()
        seen_emails: set[str] = set()
        window = send_window_of(self.config)

        for prospect_id in campaign.prospect_ids:
            prospect = await self.state.get_prospect(prospect_id)
            if not prospect or not prospect.email:
                continue
            email = prospect.email.strip().lower()
            if not EMAIL_RE.match(email) or email in seen_emails:
                continue
            if prospect.status not in SENDABLE_STATUSES:
                continue
            if (prospect.email_status or "guess") not in sendable_email:
                skipped += 1
                continue
            if await self.state.colleague_who_replied(prospect.id):
                answered += 1     # their company already answered
                continue
            seen_emails.add(email)

            # With a send window, each email is planned for the recipient's
            # office hours; follow-ups keep their delay, then move into it.
            zone = await self._zone_or_default(prospect) if window is not None else ""
            cumulative_days = 0
            for step in campaign.sequence:
                cumulative_days += max(0, step.delay_days)
                send_at = now + timedelta(days=cumulative_days)
                if window is not None:
                    send_at = next_send_time(send_at, zone, window)
                item_id = await self.state.add_outbox_item(
                    prospect_id=prospect.id,
                    campaign_id=campaign.id,
                    step=step.step,
                    to_email=email,
                    subject=self._render(step.subject, prospect),
                    body=self._render(step.body, prospect),
                    send_at=send_at.isoformat(),
                    status=initial_status,
                    provider=self.provider.name,
                    approved_by="" if require_approval else "autopilot",
                    variant=variant_for(prospect.id) if step.step == 1 else "",
                )
                if item_id:
                    staged += 1
            # Conditional: never overwrite a status set while staging
            # (marked sent by hand, a reply).
            await self.state.queue_prospect_if_unsent(prospect.id)

        await self.state.update_campaign(campaign.id, status="active")
        if skipped:
            logger.info(
                f"Sender: held back {skipped} prospect(s) with unverified "
                f"emails from '{campaign.name}'."
            )
        if answered:
            logger.info(
                f"Sender: left out {answered} prospect(s) from '{campaign.name}': "
                f"{COLLEAGUE_REPLIED} at their company."
            )
        if staged:
            mode = "awaiting your approval" if require_approval else "approved"
            logger.info(
                f"Sender: staged {staged} email(s) for '{campaign.name}' "
                f"({mode}). Review in the dashboard Outbox tab."
            )
            await self.state.log_action(
                action_type="stage_campaign",
                agent="sender",
                details={"campaign": campaign.name, "staged": staged, "mode": mode},
            )

    async def _outcome_stamp(self, item: dict, prospect) -> dict:
        """What the outcome table keys this email by, fixed as it goes out:
        the segment its company is in now, and a first email's subject
        variant when it was queued without one. Never stops a send."""
        stamp = {}
        try:
            if not item.get("segment"):
                stamp["segment"] = await segment_for_company(
                    self.state, getattr(prospect, "company_id", "") or "", prospect)
            if (item.get("kind") == "sequence" and int(item.get("step") or 0) == 1
                    and not item.get("variant")):
                stamp["variant"] = variant_for(item.get("prospect_id") or "")
        except Exception as e:
            logger.debug(f"Sender: couldn't stamp the outcome fields: {e}")
        return stamp

    def _with_footer(self, body: str) -> str:
        """A cold email as it leaves: the postal address and opt-out line
        underneath (CAN-SPAM). Added at send time, so a changed address
        applies to mail already queued; not added again when the body already
        ends with it."""
        footer = "\n".join(compliance_footer_lines(self.config))
        if not footer or body.rstrip().endswith(footer):
            return body
        return body.rstrip() + "\n\n" + footer

    def _outgoing_body(self, item: dict) -> str:
        """The exact body that goes out: cold email gets the footer, replies don't."""
        if item["kind"] == "sequence":
            return self._with_footer(item["body"])
        return item["body"]

    async def _follow_up_order(self, item: dict, prospect=None) -> str | tuple[str, str] | None:
        """For a follow-up: "wait" until the email before it went out and its
        gap has passed, (status, error) when that email never will, else None.

        Without this, a step held for any reason (approval, a pause, a
        compliance hold, the warmup ramp) lets its follow-ups' scheduled
        times arrive first: a late "Approve all" sent the whole sequence in
        one go, or step 2 before step 1.
        """
        if item["kind"] != "sequence" or int(item["step"]) <= 1:
            return None
        prev = await self.state.get_previous_step(item)
        if prev is None:
            return None
        if prev["status"] in ("pending_review", "approved", "sending"):
            return "wait"
        if prev["status"] != "sent":
            return "cancelled", f"step {prev['step']} wasn't sent ({prev['status']})"

        def _at(value):
            try:
                return datetime.fromisoformat(str(value))
            except (TypeError, ValueError):
                return None
        sent_at = _at(prev["sent_at"])
        if sent_at is None:
            return None
        delay = await self.state.get_step_delay_days(item.get("campaign_id"), item["step"])
        if delay is None:  # campaign unknown: fall back to the planned spacing
            planned, prev_planned = _at(item["send_at"]), _at(prev["send_at"])
            gap = (planned - prev_planned) if planned and prev_planned else MIN_FOLLOW_UP_GAP
        else:
            gap = timedelta(days=delay)
        ready_at = sent_at + max(MIN_FOLLOW_UP_GAP, gap)
        window = send_window_of(self.config)
        if window is not None and prospect is not None:
            ready_at = next_send_time(ready_at, await self._zone_or_default(prospect), window)
        if _utcnow() >= ready_at:
            return None
        # Reschedule it for then, so it leaves the due queue until it's ready.
        await self.state.update_outbox_item(item["id"], send_at=ready_at.isoformat())
        return "wait"

    async def _retry_later(self, item: dict, reason: str) -> datetime:
        """Move a cold email back by ADDRESS_RETRY_AFTER, saying why; return
        the new time. Sending it clears the note."""
        retry_at = _utcnow() + ADDRESS_RETRY_AFTER
        await self.state.update_outbox_item(
            item["id"], send_at=retry_at.isoformat(),
            error=f"{RECHECK_PREFIX}{retry_at:%Y-%m-%d %H:%M} UTC: {reason}"[:300])
        return retry_at

    async def _blocked(self, item: dict, prospect, allow_risky: bool,
                       mailbox: Mailbox | None = None) -> tuple[str, str] | None:
        """Why this email must not go out, as (status, error), or None.
        ``mailbox``: the one it goes out from, with several (its sender tag
        already filled in): a sign-off for someone else, or a cold email
        from a mailbox other than the prospect's, waits for you."""
        await sync_sending_mode(self.config, self.state)   # "Turn off autopilot" counts at once
        if prospect is None:
            return "failed", "prospect missing"
        if item["kind"] == "sequence":
            # Stop-on-reply / opt-out: never continue a sequence after contact.
            if prospect.status in STOP_STATUSES:
                return "cancelled", f"prospect status '{prospect.status}'"
            # The user already emailed them by hand: whatever Harvey staged
            # (before, during or after that) would be a second email.
            if await self.state.get_setting(MANUAL_SEND_KEY + prospect.id):
                return "cancelled", HAND_SENT_REASON
            # A first email to someone already contacted is always a duplicate.
            if item["step"] == 1 and prospect.status == "contacted":
                return "cancelled", "already contacted"
            # Someone else at their company answered: the rest of the
            # company gets no more cold email.
            colleague = await self.state.colleague_who_replied(prospect.id)
            if colleague:
                return "cancelled", f"{COLLEAGUE_REPLIED} ({colleague})"
        gate = pre_send_check(
            item["to_email"], item["subject"], self._outgoing_body(item),
            prospect=prospect, allow_risky=allow_risky, kind=item["kind"],
            postal_address=compliance_of(self.config).postal_address,
            do_not_contact=live_do_not_contact(self.config),
        )
        if not gate:
            logger.warning(
                f"Sender: gate blocked email to {item['to_email']}: "
                f"{gate.reasons}"
            )
            return "failed", "gate: " + "; ".join(gate.reasons)[:300]
        if (item.get("approved_by") == "autopilot"
                and getattr(self.config.channels.email, "require_approval", True)):
            # Autopilot approved it, and autopilot has since been turned off.
            return "pending_review", AUTOPILOT_OFF_REASON
        if item.get("approved_by") != "you":
            # Nobody approved this one (autopilot): what a planted
            # instruction would add sends it back to you instead.
            held = autopilot_review(item["subject"], self._outgoing_body(item), self.config,
                                    recipient=item["to_email"])
            if mailbox is not None:
                # Nobody read who it signs as, or noticed it moved mailbox.
                theirs = normalize(self.config, getattr(prospect, "mailbox", ""))
                if item["kind"] == "sequence" and theirs != mailbox.email:
                    held.append(f"it goes out from {mailbox.email}, but {prospect.email} "
                                f"belongs to {theirs}")
                held += self._sign_off_problems(item, mailbox)
            if held:
                logger.warning(f"Sender: holding email to {item['to_email']} for review: {held}")
                return "pending_review", (HELD_FOR_REVIEW + "; ".join(held))[:300]
        return None

    async def _due_rows(self, now: str, page: int, cold: bool = True,
                        mailboxes=None, primary: str = "", mix: bool = False) -> list[dict]:
        """The due, approved rows a drain goes through (only those going out
        from ``mailboxes`` when given), oldest first. ``cold``: False when no
        cold email can go this cycle, so only the rest is read. ``mix``: a
        segment mix reorders them, so more first emails are read (MIX_SCAN)."""
        where = {"mailboxes": mailboxes, "primary": primary} if mailboxes is not None else {}
        # In quiet hours only cold email can go, so only cold email is
        # fetched: replies due ahead of it can't fill the page.
        due = await self.state.get_outbox(
            status="approved", due_before=now, limit=max(page, MIX_SCAN) if mix else page,
            skip_unready_follow_ups=True, kind="sequence" if self.cold_only else None, **where,
        ) if cold else []
        # Cold emails held (by an unavailable compliance check, the cold
        # cap, a follow-up's spacing) stay approved and could fill the
        # page above, so replies (and any other kind) are fetched on
        # their own too and never wait behind them. Bounded: one extra
        # query of the same size.
        seen = {item["id"] for item in due}
        unchecked = [] if self.cold_only else await self.state.get_outbox(
            status="approved", due_before=now, limit=page,
            exclude_kind="sequence", **where,
        )
        extra = [item for item in unchecked if item["id"] not in seen]
        if cold and (self.precleared is not None or mix):
            # First emails waiting on Precleared keep their place and could
            # fill the page too, hiding follow-ups to people already cleared,
            # which a stored verdict lets go out (and with a mix, follow-ups
            # go before every first email). Fetched on their own as well.
            seen.update(item["id"] for item in extra)
            follow_ups = await self.state.get_outbox(
                status="approved", due_before=now, limit=page,
                skip_unready_follow_ups=True, follow_ups_only=True, **where,
            )
            extra += [item for item in follow_ups if item["id"] not in seen]
        if extra:
            due = sorted(
                due + extra,
                key=lambda i: (i.get("send_at") or "", i.get("created_at") or ""),
            )
        return due

    async def _segment_mix(self):
        """Today's segment mix (harvey/segment_mix.py), its ramp brought up
        to date, or None: no segment_weights, or it couldn't be worked out
        (then the drain goes in queue order, as without one)."""
        weights = weights_of(self.config)
        if not weights:
            return None
        try:
            ramp = await update_ramp(self.state, weights)
            return await build_mix(self.state, weights, ramp["level"], _local_midnight_utc())
        except Exception as e:
            logger.warning(f"Sender: couldn't work out the segment mix; sending in queue order: {e}")
            return None

    async def _mixed(self, mix, queues: list[list[dict]], limits: list[int]) -> list[list[dict]]:
        """``queues`` reordered by the segment mix (follow-ups and replies
        first, then first emails by segment), or as they are without one.
        ``limits``: what each queue's mailbox can send this cycle."""
        if mix is None:
            return queues
        try:
            mix.segments.update(await queued_segments(
                self.state.db_path, [row for q in queues for row in q]))
            return mix.order(queues, limits)
        except Exception as e:
            logger.warning(f"Sender: couldn't order by segment; sending in queue order: {e}")
            return queues

    async def _drain_due(self):
        """Send due, approved outbox items through the provider. With
        several mailboxes, each row goes out from its own mailbox, and the
        mailboxes take turns."""
        if self.precleared is None:
            # Check switched off: cold email no longer waits on it, so a
            # hold recorded while it was on must not stay on Today.
            await clear_hold(self.state)
        multi = is_multi(self.config)
        now = datetime.now(timezone.utc).replace(tzinfo=None).isoformat()
        lanes: dict[str, _Lane] = {}
        mix = await self._segment_mix()
        if multi:
            lanes, down = await self._mailbox_lanes()
            for address, why in down.items():
                logger.warning(f"Sender: not sending from {address}: {why}")
            if not lanes:
                logger.warning("Sender: no mailbox can send right now; the outbox is holding.")
                return
            # Each mailbox under its own limits (_Lane); together, the cycle's.
            max_daily = sum(lane.mailbox.daily_limit for lane in lanes.values())
            sent_today = 0
            budget = cycle_cap = MULTI_SENDS_PER_CYCLE
            cold_cap = cold_left = MULTI_SENDS_PER_CYCLE
            primary = primary_mailbox(self.config).email
            queues, limits = [], []
            for lane in lanes.values():
                cold = lane.daily_left > 0 and lane.cold_left > 0 and not lane.cold_held
                queues.append(await self._due_rows(
                    now, MAILBOX_SENDS_PER_CYCLE * 3, cold=cold,
                    mailboxes=[lane.mailbox.email], primary=primary, mix=mix is not None))
                limits.append(min(MAILBOX_SENDS_PER_CYCLE, lane.daily_left, lane.cold_left)
                              if cold else 0)
            # One mailbox's backlog can't fill the page: each has its own,
            # taken in turn.
            due = _in_turn(await self._mixed(mix, queues, limits))
        else:
            if not self.provider.is_configured():
                logger.warning(
                    f"Sender: mail provider '{self.provider.name}' is not configured "
                    "yet — outbox is holding. See CLAUDE.md → provider setup."
                )
                return
            max_daily = self.config.channels.email.max_daily_sends
            sent_today = await self.state.count_outbox_sent_today()
            # Cold email's share of the cycle: the daily cap counts every
            # send, but a spent one holds only cold email; replies still go.
            budget = min(MAX_SENDS_PER_CYCLE, max_daily - sent_today)
            cycle_cap = MAX_SENDS_PER_CYCLE
            if budget <= 0:
                logger.info(f"Sender: daily send cap reached ({sent_today}/{max_daily}); "
                            "only replies go out.")
                if self.cold_only:
                    return
            # Cold email has its own cap under the warmup ramp; replies don't.
            # Counted from local midnight, the day the ramp counts in.
            cold_cap = cold_daily_cap(self.config.channels.email)
            cold_left = cold_cap - await self.state.count_outbox_sent_today(
                kind="sequence", since=_local_midnight_utc())
            due = await self._due_rows(
                now, (budget if budget > 0 else MAX_SENDS_PER_CYCLE) * 3, cold=budget > 0,
                mix=mix is not None)
            due = (await self._mixed(mix, [due], [max(0, min(budget, cold_left))]))[0]
        if not due:
            return
        # Whether cold email had a chance this cycle (what the compliance
        # notes below say about it).
        cold_possible = multi or budget > 0

        allow_risky = getattr(self.config.channels.email, "send_to_risky", False)
        window = send_window_of(self.config)
        sent = 0
        sent_cold = 0
        outside_window = 0
        held_unread = 0
        # Set when Precleared can't be asked (allowance used up, network):
        # cold emails wait for a later cycle, replies still go out.
        compliance_paused = False
        held_cold = 0
        # For the Today page and `harvey status`: what held cold email this
        # cycle, how many waited behind it, and whether a fresh call was
        # answered (which clears an earlier hold).
        hold = None
        waiting_on_check = 0
        answered = False
        # Addresses Precleared couldn't check this cycle: their other emails
        # aren't asked about again. (self._failing_domains: failures across
        # several domains pause checks.)
        failed_addresses: set[str] = set()
        check_seconds = 0.0
        for item in due:
            if sent >= cycle_cap:
                break
            if self.cold_only and item["kind"] != "sequence":
                continue  # quiet hours: replies wait for the operator's hours
            is_cold = item["kind"] == "sequence"
            lane = None
            if multi:
                lane = lanes.get(normalize(self.config, item.get("mailbox")))
                if lane is None or lane.stopped or lane.sent >= MAILBOX_SENDS_PER_CYCLE:
                    continue
                if is_cold and (lane.sent >= lane.daily_left or lane.cold_held):
                    continue  # its own daily limit or an unread inbox; replies still go
                if is_cold and lane.sent_cold >= lane.cold_left:
                    held_cold += 1
                    continue
            elif is_cold and sent >= budget:
                continue  # the day's cap is spent: only replies go
            if is_cold and item["prospect_id"] in self.hold_prospects:
                # An inbox their reply could be in wasn't read completely.
                held_unread += 1
                continue
            if not multi and is_cold and sent_cold >= cold_left:
                held_cold += 1
                continue
            if mix is not None and not mix.allows(item):
                mix.held += 1     # its segment is at its ramp's ceiling today
                continue
            needs_check = self.precleared is not None and is_cold
            mailbox = lane.mailbox if lane is not None else None
            provider = lane.provider if lane is not None else self.provider

            prospect = await self.state.get_prospect(item["prospect_id"])
            item = self._as_sent_from(item, mailbox)
            blocked = await self._blocked(item, prospect, allow_risky, mailbox)
            if blocked:
                await self.state.update_outbox_item(
                    item["id"], status=blocked[0], error=blocked[1])
                continue
            order = await self._follow_up_order(item, prospect)
            if order == "wait":
                continue
            if order:
                await self.state.update_outbox_item(item["id"], status=order[0], error=order[1])
                continue
            # The recipient's country when Harvey is sure of it, and the
            # company's and person's names: they go with the compliance
            # check, and a cached verdict is keyed on them.
            country = await self._recipient_country(prospect) if needs_check else ""
            names = await self._recipient_names(prospect) if needs_check else ("", "")
            # Cold email waits for the recipient's send window, however
            # late it was approved; replies don't.
            if (window is not None and is_cold
                    and await self._outside_window(
                        item, await self._zone_or_default(prospect, given=country, names=names),
                        window)):
                outside_window += 1
                continue

            # Optional compliance check (compliance.precleared): one per
            # address, before the first cold email. Replies are never checked.
            receipt_id = ""
            if needs_check:
                company, name = names
                # A stored verdict still counts once checks are paused, so
                # people already cleared keep getting their follow-ups; only
                # new addresses wait for the next cycle. A recipient Harvey
                # is sure is in a skip_countries country needs no check.
                clearance = await self.precleared.cached(
                    item["to_email"], self.state, country, company=company, name=name)
                if clearance is None and (compliance_paused
                                          or check_seconds > CHECK_SECONDS_PER_CYCLE):
                    waiting_on_check += 1
                    continue
                address = (item["to_email"] or "").strip().lower()
                if clearance is None and address in failed_addresses:
                    await self._retry_later(item, "the check failed for this address earlier this cycle")
                    continue
                if clearance is None:
                    started = time.monotonic()
                    clearance = await self.precleared.check(
                        item["to_email"], self.state, country, company=company, name=name)
                    check_seconds += time.monotonic() - started
                if clearance.held and not clearance.pause_checks:
                    # Possibly about this address alone: move its email back,
                    # behind other email, and carry on with the rest. Only a
                    # first failure counts towards pausing: an address failing
                    # again says nothing new about Precleared.
                    again = (item.get("error") or "").startswith(RECHECK_PREFIX)
                    failed_addresses.add(address)
                    if not again:
                        self._failing_domains.add(address.rpartition("@")[2])
                    waiting_on_check += 1
                    retry_at = await self._retry_later(item, clearance.reason)
                    logger.warning(
                        f"Sender: Precleared couldn't check {item['to_email']}, trying "
                        f"again after {retry_at:%Y-%m-%d %H:%M} UTC: {clearance.reason}"
                    )
                    if again or len(self._failing_domains) < DOMAINS_FAILING_TO_PAUSE:
                        continue
                if clearance.held:
                    # About Precleared, not just this address: the others
                    # would likely fail too, so no more fresh checks this cycle.
                    compliance_paused = True
                    hold = clearance
                    if clearance.pause_checks:  # else counted just above
                        waiting_on_check += 1
                        if clearance.retry_address:
                            # It may still be this address: don't let it be
                            # first in line, and asked first, every cycle.
                            await self._retry_later(item, clearance.reason)
                    logger.warning(
                        f"Sender: compliance check unavailable, holding cold "
                        f"email until a later cycle: {clearance.reason}"
                    )
                    continue
                if not clearance.cached and not clearance.skipped:
                    answered = True
                    self._failing_domains.clear()
                if not clearance.allowed:
                    await self.state.update_outbox_item(
                        item["id"], status="failed",
                        error=f"precleared: {clearance.verdict}: {clearance.reason}"[:300],
                    )
                    logger.warning(
                        f"Sender: Precleared held back email to {item['to_email']}: "
                        f"{clearance.verdict} ({clearance.reason})"
                    )
                    continue
                receipt_id = clearance.receipt_id
                # Precleared found where the recipient is, and nothing else
                # had: their own window applies from now on.
                if (window is not None and window.recipient_timezone and clearance.country
                        and not prospect.timezone):
                    zone = await self._zone_or_default(prospect, clearance.country)
                    if zone and await self._outside_window(item, zone, window):
                        outside_window += 1
                        continue

            # Claim it. A reply, bounce, hand send or rejection may have
            # cancelled it since the queue was read; then it stays cancelled.
            if not await self.state.claim_outbox_item(item["id"]):
                continue
            # Send the email as it is now, not as the queue read it: it may
            # have been edited and approved again since.
            item = await self.state.get_outbox_item(item["id"]) or item
            if mailbox is not None and normalize(self.config, item.get("mailbox")) != mailbox.email:
                # Moved to another mailbox meanwhile: that one sends it.
                await self.state.release_outbox_item(item["id"])
                continue
            item = self._as_sent_from(item, mailbox)
            # Whatever landed between the checks above and the claim (a reply
            # sets the status before it cancels) is caught here, with fresh
            # data. From now on nothing else can cancel this email.
            prospect = await self.state.get_prospect(item["prospect_id"])
            blocked = await self._blocked(item, prospect, allow_risky, mailbox)
            if blocked:
                await self.state.update_outbox_item(
                    item["id"], status=blocked[0], error=blocked[1])
                continue

            body = self._outgoing_body(item)
            try:
                result = await provider.send_email(
                    item["to_email"], item["subject"], body,
                    thread_ref=item.get("thread_ref", ""),
                    in_reply_to=item.get("in_reply_to", ""),
                )
            except Exception as e:
                # It may have gone out before the error: never retry it.
                await self.state.update_outbox_item(
                    item["id"], status="failed",
                    error=f"{MAYBE_SENT_PREFIX}send error: {e}"[:300],
                )
                logger.warning(f"Sender: send to {item['to_email']} raised: {e}")
                continue
            if not result.ok:
                if lane is not None and (not result.attempted
                                         or result.status in MAILBOX_REFUSED_STATUSES):
                    # The mailbox, not the email: nothing went out, so the
                    # email waits and this mailbox rests for the cycle.
                    await self.state.release_outbox_item(item["id"])
                    lane.stopped = (result.error or f"HTTP {result.status}")[:200]
                    await self._mark_down(lane.mailbox)
                    logger.warning(
                        f"Sender: {lane.mailbox.email} couldn't send ({lane.stopped}); its "
                        "email waits and the other mailboxes carry on.")
                    continue
                await self.state.update_outbox_item(
                    item["id"], status="failed", error=result.error[:300]
                )
                continue

            sent += 1
            if is_cold:
                sent_cold += 1
            if mix is not None:
                mix.record(item)
            if lane is not None:
                lane.sent += 1
                if is_cold:
                    lane.sent_cold += 1
                await self._mark_up(lane.mailbox)
            now_iso = datetime.now(timezone.utc).replace(tzinfo=None).isoformat()
            # A note that the check moved it back no longer applies.
            note = {"error": ""} if (item.get("error") or "").startswith(RECHECK_PREFIX) else {}
            await self.state.update_outbox_item(
                item["id"], status="sent", sent_at=now_iso, body=body,
                message_id=result.message_id, thread_ref=result.thread_ref, **note,
                **await self._outcome_stamp(item, prospect),
            )
            if prospect.status in ("new", "queued"):
                await self.state.update_prospect_status(prospect.id, "contacted")

            # A sent reply belongs in its conversation thread.
            if item["kind"] == "reply" and item.get("conversation_id"):
                try:
                    convo = await self.state.get_conversation(item["conversation_id"])
                    if convo:
                        from harvey.models.conversation import Message
                        convo.thread.append(
                            Message(sender="harvey", content=item["body"])
                        )
                        await self.state.update_conversation(
                            convo.id, thread_json=convo.thread_json()
                        )
                except Exception as e:
                    logger.debug(f"Sender: convo append failed: {e}")
            details = {
                "to": item["to_email"], "step": item["step"],
                "kind": item["kind"], "provider": provider.name,
            }
            if mailbox is not None:
                details["mailbox"] = mailbox.email
            if receipt_id:
                details["precleared_receipt"] = receipt_id
            await self.state.log_action(
                action_type="email_sent",
                agent="sender",
                details=details,
            )
            logger.info(
                f"Sender: sent step {item['step']} to {item['to_email']} "
                f"via {provider.name}" + (f" from {mailbox.email}." if mailbox else ".")
            )
            # Human-ish pacing BETWEEN sends (not after the last, and never
            # in tests where pacing is disabled).
            if self.send_pacing and sent < cycle_cap:
                await asyncio.sleep(random.uniform(*SEND_JITTER_SECONDS))

        if cold_possible:
            self.compliance_held = compliance_paused
        if outside_window:
            logger.info(
                f"Sender: {outside_window} cold email(s) wait for the recipient's "
                f"send window ({window.start}-{window.end} their time).")
        if held_unread:
            logger.info(
                f"Sender: {held_unread} cold email(s) wait a cycle: an inbox their reply "
                "could be in wasn't read completely.")
        if mix is not None and mix.held:
            logger.info(
                f"Sender: {mix.held} custom first email(s) wait: the custom segment is ramping "
                "and has its share of today's first emails. See the Mailboxes tab.")
        if self.precleared is not None and cold_possible:
            if hold is not None:
                # The page read above stops at a few times the cycle's
                # budget; count every cold email still waiting on the check:
                # due with no stored verdict to go on, or moved back by it.
                waiting_on_check = max(waiting_on_check, await self.state.count_due_cold_outbox(
                    now, has_cached_verdict=self._has_cached_verdict,
                    moved_back_prefix=RECHECK_PREFIX))
                await record_hold(self.state, hold, waiting_on_check)
            elif answered:
                await clear_hold(self.state)
        if held_cold:
            warmup = getattr(self.config.channels.email, "warmup", None)
            days = warmup.days_in() if warmup is not None else None
            if multi:
                logger.info(f"Sender: {held_cold} cold email(s) wait: their mailboxes reached "
                            "today's cold cap. Replies still go out. See `harvey status`.")
            elif days is not None and cold_cap < max_daily:
                logger.info(
                    f"Sender: warmup day {days}: {held_cold} cold email(s) wait; today's "
                    f"cold cap is {cold_cap}. Replies still go out. See `harvey warmup`.")
            else:
                logger.info(f"Sender: cold email cap reached for today ({cold_cap}).")
        if sent_cold:
            await self.state.remember_footer_lines(compliance_footer_lines(self.config))
            notice = compliance_notice(self.config)
            if notice:
                logger.warning(f"Sender: {notice}")
        if sent:
            if multi:
                logger.info(f"Sender: {sent} email(s) sent this cycle (" + ", ".join(
                    f"{lane.mailbox.email} {lane.sent}" for lane in lanes.values() if lane.sent)
                    + ").")
            else:
                logger.info(f"Sender: {sent} email(s) sent this cycle "
                            f"({sent_today + sent}/{max_daily} today).")
