"""Writer — crafts personalized email sequences.

With a native mail provider (Gmail/SMTP), email 1 is written PER PROSPECT
from grounded facts — the company's own description, detected tech stack,
and buying signals — instead of one merge-tag template shared by a whole
batch. Mass-templated "token-swap" mail is exactly what inbox filters now
cluster and junk; a specific, verifiable first line is what earns replies.
Steps 2-3 stay campaign-level (proof point + breakup are less personal by
design). Every draft still passes the deterministic pre-send gate.
"""

import json
import logging
import re
from datetime import datetime, timezone

from harvey.brain import Brain
from harvey.collectors.site_age import PLATFORM_LABELS
from harvey.config import EmailRulesError, HarveyConfig, compliance_of, load_email_rules
from harvey.footprints import FOOTPRINT_LABELS, FOOTPRINT_SOURCE
from harvey.humanize import humanize, humanizer_enabled
from harvey.integrations.mail_provider import NATIVE_PROVIDERS
from harvey.mailboxes import (
    SENDER_FIRST_NAME_TAG, MailboxProviders, companies_of, configured_mailboxes, is_multi,
    least_loaded, normalize, takes_new_prospects,
)
from harvey.models.campaign import Campaign, EmailStep
from harvey.segments import (
    OLD_SITE_SEGMENTS, PLATFORM_CLAIM_FLOOR, SEGMENT_LABELS, add_mx, company_site_facts,
    eligible_for_old_site_pitch, segment_of,
)
from harvey.state import WRITABLE_EMAIL_STATUSES, StateManager
from harvey.sending_mode import sync_sending_mode
from harvey.startups import startup_facts
from harvey.untrusted import UNTRUSTED_RULE, fence

logger = logging.getLogger("harvey.writer")

# How many times the model may fail to write a usable sequence for a
# prospect before Harvey stops trying (status 'write_failed'). A provider
# outage never counts; only a reply that came back unusable does.
MAX_WRITE_ATTEMPTS = 3
WRITE_ATTEMPTS_KEY = "write_attempts:"

# Merge variables that name a person; a company inbox has none of them.
PERSON_VAR = re.compile(r"\{\{\s*(first_?name|last_?name|title)\s*\}\}", re.IGNORECASE)

# Native mode: one Claude call per prospect for email 1, so batches stay
# small — the rest of the 'new' pool is picked up on later cycles.
NATIVE_BATCH_CAP = 8
LEGACY_BATCH_CAP = 50
# Age facts named in one email, at most: the few strongest make the point.
MAX_AGE_FACTS = 4
# Said whenever the profile has read the company's site.
SITE_CLAIMS_RULE = (
    "\n- Say nothing about their website's age, platform, phone view or security\n"
    "  unless it is one of the 'Website platform' or 'Seen on their website' FACTS.")

# The built-in "how" lines each prompt ends with: email 1 is an observation
# and a question, with no pitch. Operator email rules replace exactly these.
PERSONAL_SHAPE = """- Under 75 words. One specific observation from the FACTS above, one
  question. No pitch, no product name.
- Write the actual text (no merge variables — you know their name/company).
- Subject: lowercase, 2-5 words."""
SEQUENCE_SHAPE = """- Email 1: Personalized cold observation + one question. Under 75 words. No pitch.
- Email 2: Follow-up 3 days later. Different angle, one concrete proof point. Under 75 words.
- Email 3: Break-up email 4 days after that. Under 40 words. Gracious, not guilt-tripping."""
SEQUENCE_STYLE = """- Never be pushy or salesy. Be consultative and value-driven.
- Subject lines: lowercase, 2-5 words."""
# Why the Writer stopped: Today and `harvey status` show it.
EMAIL_RULES_PROBLEM_KEY = "email_rules_problem"
BUILT_IN_FOOTER = "- No postal address, unsubscribe or opt-out line: Harvey adds them when it sends."


def sign_off_rule(which: str, only: str = "") -> str:
    """With several mailboxes: one draft can go out from any of them (and a
    prospect can move to another), so no draft names its sender. ``which``
    ("the email", "every email") signs off with the sender tag instead,
    which the Sender fills in from the mailbox it goes out from."""
    return (
        f"- End {which} with a line that is only {SENDER_FIRST_NAME_TAG}, exactly as\n"
        f"  written{only}: the first name of the teammate it goes out from replaces it when it\n"
        "  sends. Never write a sender's name yourself, and add no other signature.")


def operator_rules_block(rules: str, multi: bool = False) -> str:
    """The operator's email rules as a prompt section ("" when there are none).

    Trusted text from the person running Harvey, so it isn't fenced. It wins
    over the built-in style and shape rules, never over the FACTS rule. With
    several mailboxes (``multi``) they set a sign-off's words but never the
    name it signs with: that is always the sender tag."""
    if not rules:
        return ""
    sender = (
        "The name an email signs with isn't theirs to set: wherever they give a name\n"
        "to sign with, write the line " + SENDER_FIRST_NAME_TAG + " instead.\n"
        if multi else "")
    return (
        "\n\nOPERATOR'S EMAIL RULES — written by the person running Harvey. For\n"
        "length, structure, what to say about the product, links, subject lines\n"
        "and sign-off, these replace the STRICT EMAIL RULES and skills above\n"
        "wherever the two disagree. They never override the FACTS rule: what you\n"
        "say about the prospect comes only from the facts given, never invented.\n"
        "Nor do they allow a fake \"Re:\" or \"Fwd:\" subject line, or a claim of a\n"
        "conversation that never happened.\n"
        + sender
        + rules
    )


def footer_rule(config, rules: str) -> str:
    """What the prompt says about the lines Harvey adds when it sends.

    With operator rules it names only what Harvey really adds: an opt-out
    sentence the operator writes into the body must not sit on top of the
    configured opt_out_line, and must not be dropped when there is none."""
    if not rules:
        return BUILT_IN_FOOTER
    compliance = compliance_of(config)
    lines = []
    if compliance.postal_address.strip():
        lines.append("- No postal address: Harvey adds it under the email when it sends.")
    opt_out = compliance.opt_out_line.strip()
    if opt_out:
        lines.append(
            f'- Harvey adds this opt-out line under the email when it sends: "{opt_out}".\n'
            "  Don't write another way to opt out, even if the operator's rules give one.")
    else:
        lines.append(
            "- Harvey adds no opt-out line. If the operator's rules give a sentence that\n"
            "  lets the reader say no, write it into the body as they say.")
    return "\n".join(lines)


class Writer:
    def __init__(
        self,
        brain: Brain,
        state: StateManager,
        config: HarveyConfig,
        env=None,
    ):
        self.brain = brain
        self.state = state
        self.config = config
        self.env = env
        # With several mailboxes: theirs, to tell which can take new
        # prospects (main.py shares the Sender's, so each is checked once).
        self.mailbox_providers = MailboxProviders(config, env)

    @property
    def is_native(self) -> bool:
        return self.config.channels.email.provider in NATIVE_PROVIDERS

    @property
    def multi(self) -> bool:
        return is_multi(self.config)

    def _persona_name(self) -> str:
        """Who the prompt says writes. With several mailboxes no one person:
        the email goes out from (and signs as) whichever teammate's mailbox
        sends it."""
        if not self.multi:
            return self.config.persona.name
        return (f"a member of the {self.config.persona.company} team (the first name of "
                "whoever it goes out from is added where the email signs off)")

    async def _assign_mailboxes(self, batches: dict[str, list]) -> dict[str, list]:
        """Several mailboxes: give everyone about to be written for a
        mailbox before anything is drafted, company by company, so a company
        hears from one sender. A company Harvey has already dealt with stays
        on the mailbox it dealt with them from ("" there is the primary);
        otherwise it goes to the least busy mailbox that can take new
        prospects now (mailboxes.least_loaded). Each company is saved before
        the next is chosen, so its load counts. Someone already given a
        mailbox keeps it. Returns the batches without the prospects no
        mailbox could take: they stay new for a later cycle."""
        candidates = {p.id: p for group in batches.values() for p in group}
        if not candidates:
            return batches
        companies = [[candidates[pid] for pid in ids]
                     for ids in await companies_of(self.state, list(candidates))]

        configured = {m.email for m in configured_mailboxes(self.config)}
        open_mailboxes = None    # the ones that can take new prospects, once asked
        left_out: set[str] = set()
        for people in companies:
            unassigned = [p for p in people if not (p.mailbox or "").strip()]
            if not unassigned:
                continue
            stored = await self.state.company_mailbox([p.id for p in people])
            if stored is not None:
                target = normalize(self.config, stored)
                if target not in configured:
                    logger.warning(
                        f"Writer: {unassigned[0].email}'s company is with {target}, which "
                        "isn't in channels.email.mailboxes any more; their emails wait for it.")
            else:
                if open_mailboxes is None:
                    open_mailboxes = []
                    for m in configured_mailboxes(self.config):
                        why = await takes_new_prospects(self.state, m, self.mailbox_providers.get(m))
                        if why:
                            logger.info(f"Writer: {m.email} takes no new prospects now: {why}")
                        else:
                            open_mailboxes.append(m)
                chosen = await least_loaded(self.state, self.config, open_mailboxes)
                if chosen is None:
                    left_out.update(p.id for p in unassigned)
                    continue
                target = chosen.email
            for p in unassigned:
                await self.state.set_prospect_mailbox(p.id, target)
                p.mailbox = target
        if left_out:
            logger.warning(
                f"Writer: no mailbox can take new prospects right now (signed in and not "
                f"paused); {len(left_out)} prospect(s) wait for a later cycle.")
        kept = {name: [p for p in group if p.id not in left_out] for name, group in batches.items()}
        return {name: group for name, group in kept.items() if group}

    async def run(self):
        """Create email campaigns for prospects that need outreach."""
        logger.info("Writer: Crafting email campaigns...")

        # Load foundational skills for this agent
        self.skills = self.brain.load_skills_for_agent("writer")
        try:
            self.email_rules = load_email_rules(self.config)
        except EmailRulesError as e:
            # The operator replaced the built-in shape on purpose: drafting in
            # it anyway would put emails they didn't want in the outbox.
            logger.warning(
                f"Writer: your email rules (writer.email_rules) can't be used: {e}. "
                "Writing nothing until that's fixed.")
            await self.state.set_setting(EMAIL_RULES_PROBLEM_KEY, str(e))
            return
        if await self.state.get_setting(EMAIL_RULES_PROBLEM_KEY):
            await self.state.delete_setting(EMAIL_RULES_PROBLEM_KEY)

        # Get prospects that haven't been contacted yet
        new_prospects = await self.state.get_prospects_by_status("new")
        if not new_prospects:
            logger.info("Writer: No new prospects to write for.")
            return

        # Only write for prospects we can actually deliver to. Guessed and
        # invalid addresses are skipped so we don't spend Claude calls (or
        # sending reputation) on mail that will bounce.
        prospects_with_email = [
            p for p in new_prospects
            if p.email and (p.email_status or "guess") in WRITABLE_EMAIL_STATUSES
        ]
        # Someone at their company already replied: no cold email to write.
        prospects_with_email = [
            p for p in prospects_with_email
            if not await self.state.colleague_who_replied(p.id)
        ]
        # What their sites run on decides the campaign, and two segments wait:
        # the custom segment (legacy shop software, old sites) is written only
        # to verified addresses, and a company a footprint search found waits
        # until the profile has read its site. state.count_writable_prospects
        # leaves the same people out, so the heartbeat doesn't pick writing
        # for them every cycle.
        self._site_cache = {}
        segments = {}
        for p in prospects_with_email:
            site = await self._site_for(p.company_id)
            segments[p.id] = self._segment(p, site)
        held = [p for p in prospects_with_email
                if self._held(p, segments[p.id], self._site_cache.get(p.company_id))]
        if held:
            logger.info(f"Writer: {len(held)} prospect(s) wait: custom-segment sites are written to "
                        "verified addresses only, and footprint finds wait for their profile.")
        held_ids = {id(p) for p in held}
        prospects_with_email = [p for p in prospects_with_email if id(p) not in held_ids]
        if not prospects_with_email:
            logger.info(
                "Writer: No prospects with deliverable (verified/risky) emails yet."
            )
            return

        # Batch prospects into campaign groups (by website segment)
        batches = self._group_prospects(prospects_with_email, segments)
        if self.multi:
            # Who sends to whom, decided before anything is drafted.
            batches = await self._assign_mailboxes(batches)

        for batch_name, prospects in batches.items():
            if not prospects:
                continue

            logger.info(
                f"Writer: Creating campaign '{batch_name}' for "
                f"{len(prospects)} prospects."
            )

            # Generate the email sequence
            self._no_reply = False
            sequence = await self._write_sequence(prospects)
            if not sequence:
                paused = getattr(self.brain, "paused_reason", "") or (
                    "no reply from the model" if self._no_reply else "")
                if paused:
                    # The provider failed (outage, logout, rate limit,
                    # timeout), not the draft: no attempt is counted, and the
                    # other batches would fail the same way.
                    logger.warning(f"Writer: {paused}; leaving '{batch_name}' for later.")
                    return
                logger.warning(f"Writer: Failed to generate sequence for {batch_name}")
                await self._count_failed_attempt(batch_name, prospects)
                continue

            # Create the campaign
            campaign = Campaign(
                id="",
                name=batch_name,
                channel="email",
                sequence=sequence,
                prospect_ids=[p.id for p in prospects],
                status="draft",
            )
            campaign_id = await self.state.add_campaign(campaign)

            # Mark prospects so they aren't picked up again
            for p in prospects:
                await self.state.queue_prospect_if_unsent(p.id)
                await self.state.delete_setting(WRITE_ATTEMPTS_KEY + p.id)

            # Native providers: replace the shared template for email 1 with
            # a per-prospect draft grounded in that company's actual facts.
            if self.is_native:
                await self._personalize_first_emails(campaign_id, prospects)

            await self.state.log_action(
                action_type="write_campaign",
                agent="writer",
                details={
                    "campaign_id": campaign_id,
                    "campaign_name": batch_name,
                    "prospect_count": len(prospects),
                    "steps": len(sequence),
                },
            )
            logger.info(
                f"Writer: Campaign '{batch_name}' created with "
                f"{len(sequence)} emails for {len(prospects)} prospects."
            )

    async def _count_failed_attempt(self, batch_name: str, prospects: list):
        """Count one unusable sequence against each prospect in the batch.

        A prospect left 'new' makes the heartbeat choose write_campaign every
        cycle, so after MAX_WRITE_ATTEMPTS it moves to 'write_failed': visible
        in status and the dashboard, and out of the way of prospecting.
        """
        given_up = []
        for p in prospects:
            key = WRITE_ATTEMPTS_KEY + p.id
            try:
                attempts = int(await self.state.get_setting(key) or 0) + 1
            except ValueError:
                attempts = 1
            if attempts >= MAX_WRITE_ATTEMPTS:
                await self.state.update_prospect_status(p.id, "write_failed")
                await self.state.delete_setting(key)
                given_up.append(p.email)
            else:
                await self.state.set_setting(key, str(attempts))
        if given_up:
            await self.state.log_action(
                action_type="write_failed",
                agent="writer",
                details={"campaign_name": batch_name, "prospects": given_up,
                         "attempts": MAX_WRITE_ATTEMPTS},
            )
            logger.warning(
                f"Writer: Gave up on {len(given_up)} prospect(s) in '{batch_name}' after "
                f"{MAX_WRITE_ATTEMPTS} unusable drafts; marked write_failed."
            )

    async def _personalize_first_emails(self, campaign_id: str, prospects: list):
        """Draft a grounded, per-prospect email 1 and stage it in the outbox.

        The outbox unique index means the sender's later template staging
        can't overwrite these rows — a personalized draft always wins the
        (campaign, prospect, step 1) slot. If a draft fails, the slot stays
        empty and the sender falls back to the campaign template.
        """
        await sync_sending_mode(self.config, self.state)
        require_approval = getattr(
            self.config.channels.email, "require_approval", True
        )
        status = "pending_review" if require_approval else "approved"
        now = datetime.now(timezone.utc).replace(tzinfo=None).isoformat()
        provider = self.config.channels.email.provider

        pending = []
        for prospect in prospects:
            try:
                draft = await self._write_personal_email(prospect)
            except Exception as e:
                logger.warning(f"Writer: personal draft failed for {prospect.email}: {e}")
                continue
            if draft:
                pending.append((prospect, draft))

        # One Humanizer pass over the whole batch before anything is staged.
        polished = await humanize(
            self.brain, [d for _, d in pending], agent="writer",
            enabled=humanizer_enabled(self.config),
        )

        drafted = 0
        for (prospect, _), draft in zip(pending, polished):
            item_id = await self.state.add_outbox_item(
                prospect_id=prospect.id,
                campaign_id=campaign_id,
                step=1,
                to_email=prospect.email,
                subject=draft["subject"],
                body=draft["body"],
                send_at=now,
                status=status,
                provider=provider,
                approved_by="" if require_approval else "autopilot",
            )
            if item_id:
                drafted += 1

        if drafted:
            logger.info(
                f"Writer: drafted {drafted} personalized first email(s) "
                f"({'awaiting approval' if require_approval else 'approved'})."
            )

    async def _startup(self, company_id) -> list[str] | None:
        """Fact lines when the company is a startup (YC, another accelerator,
        a raise it announced; see harvey.startups), else None."""
        return await startup_facts(getattr(self, "state", None), company_id)

    def _startup_playbook(self) -> str:
        """skills/selling_to_startups.md, for startup prospects ("" if absent)."""
        text = self.brain.load_skill("selling_to_startups") if hasattr(self.brain, "load_skill") else ""
        return "\n\nSELLING TO STARTUP FOUNDERS\n" + text if text else ""

    async def _site_for(self, company_id: str, mx: bool = False) -> dict:
        """What the profile found on the company's site (segments.
        company_site_facts), read once per run; {} when nothing can be read.
        ``mx`` adds the MX check an old-site pitch needs, asked only for the
        companies actually being written to, not every waiting prospect's."""
        cache = self.__dict__.setdefault("_site_cache", {})
        if not company_id:
            return {}
        if company_id not in cache:
            try:
                cache[company_id] = await company_site_facts(self.state, company_id, check_mx=False)
            except Exception as e:
                logger.debug(f"Writer: site facts unread for {company_id}: {e}")
                cache[company_id] = {}
        if mx and cache[company_id]:
            try:
                cache[company_id] = await add_mx(cache[company_id])
            except Exception as e:
                logger.debug(f"Writer: MX check failed for {company_id}: {e}")
        return cache[company_id]

    @staticmethod
    def _segment(prospect, site: dict | None) -> str:
        """shopify | wordpress | custom | other, for the campaign and the playbook."""
        if site and site.get("segment"):
            return site["segment"]
        return segment_of(None, [], prospect.industry)

    @staticmethod
    def _held(prospect, segment: str, site: dict | None) -> bool:
        """Left 'new' for now: the custom segment is written only to verified
        addresses, and a company a footprint search found waits until the
        profile has recorded its platform (state._writer_holds agrees)."""
        if not prospect.company_id:
            return False
        if segment == "custom" and (prospect.email_status or "") != "verified":
            return True
        site = site or {}
        found_by_footprint = (site.get("source") == FOOTPRINT_SOURCE
                              or site.get("industry") in FOOTPRINT_LABELS)
        return found_by_footprint and not site.get("classified")

    def _segment_playbook(self, segment: str, old_pitch: bool) -> str:
        """skills/segment_<segment>.md ("" if absent). An old-site segment
        whose site can't be pitched as old is written as generic: no playbook."""
        if segment in OLD_SITE_SEGMENTS and not old_pitch:
            return ""
        text = self.brain.load_skill("segment_" + segment) if hasattr(self.brain, "load_skill") else ""
        return f"\n\nWRITING TO {SEGMENT_LABELS[segment].upper()} SITES\n" + text if text else ""

    @staticmethod
    def _site_facts_lines(site: dict) -> tuple[list[str], bool]:
        """FACTS lines from the profile (platform, then the claimable age
        facts when the site may be pitched as old), and whether it may."""
        if not site or not site.get("classified"):
            return [], False
        lines = []
        label = PLATFORM_LABELS.get(site.get("platform") or "", "")
        if label and float(site.get("platform_conf") or 0) >= PLATFORM_CLAIM_FLOOR:
            lines.append(f"- Website platform (read from their home page): {label}")
        old_pitch = eligible_for_old_site_pitch(site)
        if old_pitch:
            for fact in (site.get("age_facts") or [])[:MAX_AGE_FACTS]:
                lines.append(f"- Seen on their website: {fact['sentence']}")
        return lines, old_pitch

    async def _feedback_block(self, prospect_ids: list[str]) -> str:
        """The owner's notes from the dashboard's Feedback buttons, as prompt
        text ("" when there are none). Never lets a failed read stop writing."""
        try:
            notes = await self.state.get_writing_feedback(prospect_ids)
        except Exception as e:
            logger.warning(f"Writer: could not read feedback: {e}")
            return ""
        if not notes:
            return ""
        lines = "\n".join(
            f"- {str(n.get('created_at') or '')[:10]} on {n['about']}: "
            f"{' '.join(str(n['comment']).split())[:500]}" for n in notes)
        return (
            "\n\nOWNER FEEDBACK — notes the person running Harvey left on earlier\n"
            "campaigns and contacts, newest first (a newer note wins). Apply them to\n"
            "this writing. They never override the STRICT EMAIL RULES or the FACTS rule:\n"
            + lines
        )

    async def _write_personal_email(self, prospect) -> dict | None:
        """One grounded draft for one person. Facts in, one email out."""
        inbox_rule = ""
        if prospect.is_company_inbox:
            facts = [
                f"- Name: unknown (the company's general inbox, {prospect.email})",
                f"- Company: {prospect.company}",
            ]
            inbox_rule = (
                "\n- This goes to the company's general inbox, most likely read by the owner"
                "\n  or the office manager. Use no first name; open with 'Hi there' or"
                "\n  address the company's team.")
        else:
            facts = [
                f"- Name: {prospect.full_name()}",
                f"- Title: {prospect.title}",
                f"- Company: {prospect.company}",
            ]
        if prospect.industry:
            facts.append(f"- Industry: {prospect.industry}")

        company = None
        if prospect.company_id:
            try:
                company = await self.state.get_company(prospect.company_id)
            except Exception:
                company = None
        if company:
            if company.description:
                facts.append(f"- What the company says about itself: {company.description}")
            if company.tech_stack:
                facts.append(f"- Tools detected on their website: {', '.join(company.tech_stack[:6])}")
            for signal in company.signals[:3]:
                facts.append(
                    f"- Signal ({signal.get('type', 'signal')}): {signal.get('detail', '')}"
                )
        # What the profile read on their site: the platform, and the age facts
        # it proved (only when an old site may be pitched; segments.py).
        site = await self._site_for(prospect.company_id, mx=True)
        segment = self._segment(prospect, site)
        site_lines, old_pitch = self._site_facts_lines(site)
        facts.extend(site_lines)
        if prospect.personalization_notes:
            facts.append(f"- Research notes: {prospect.personalization_notes}")
        startup = await self._startup(prospect.company_id)
        if startup is not None:
            facts.extend(startup)

        prompt = self.brain.load_prompt(
            "writer",
            product_name=self.config.product.name,
            product_description=self.config.product.description,
            product_benefits="\n".join(f"- {b}" for b in self.config.product.key_benefits),
            product_pricing=self.config.product.pricing,
            persona_name=self._persona_name(),
            persona_company=self.config.persona.company,
            persona_role=self.config.persona.role,
            persona_tone=self.config.persona.tone,
        ) or ""
        if self.skills:
            prompt += "\n\n" + self.skills
        if startup is not None:
            prompt += self._startup_playbook()
        prompt += self._segment_playbook(segment, old_pitch)
        prompt += await self._feedback_block([prospect.id])
        rules = getattr(self, "email_rules", "")
        prompt += operator_rules_block(rules, multi=self.multi)
        if rules:
            shape = (
                "- Follow the OPERATOR'S EMAIL RULES above for this first email: its length,\n"
                "  structure, what it says about the product, links and subject line.\n"
                "- Write the actual text (no merge variables — you know their name/company).")
            strict = "- Follow every STRICT EMAIL RULE above that the operator's rules don't replace."
        else:
            shape = PERSONAL_SHAPE
            strict = "- Follow every STRICT EMAIL RULE above."
        if site.get("classified"):
            strict += SITE_CLAIMS_RULE
        sign_off = ("\n" + sign_off_rule("the email", " (the one merge variable in it)")
                    if self.multi else "")

        prompt += f"""

Write ONE cold email (the very first touch) to this specific person.

FACTS — everything you know about them, gathered from their website and
the web. Every claim about the prospect in your email must come from these
facts. If a fact isn't listed here, you don't know it — do not invent
funding rounds, mutual contacts, metrics, or anything else:
{fence(chr(10).join(facts), "prospect research")}

{UNTRUSTED_RULE}

Requirements:
{shape}
{footer_rule(self.config, rules)}
{strict}{inbox_rule}{sign_off}

Return ONLY JSON: {{"subject": "...", "body": "..."}}"""

        result = await self.brain.think_json(
            prompt, session_id="harvey-writer",
            agent="writer", task="personal_email",
        )
        if not isinstance(result, dict):
            return None
        subject = str(result.get("subject") or "").strip()
        body = str(result.get("body") or "").strip()
        if not subject or not body:
            return None
        return {"subject": subject[:120], "body": body[:2000]}

    async def _write_sequence(self, prospects: list) -> list[EmailStep]:
        """Ask the brain to write a 3-email sequence."""
        # Build context about the prospects
        prospect_summary = "\n".join(
            f"- {p.full_name()}, {p.title} at {p.company}"
            + (f" | Notes: {p.personalization_notes}" if p.personalization_notes else "")
            for p in prospects[:5]  # Show sample for context
        )
        inbox_batch = all(p.is_company_inbox for p in prospects)
        if inbox_batch:
            merge_rule = (
                "- These go to each company's general inbox, so no names are known.\n"
                "  Use {{company}} as the merge variable in every email and NEVER\n"
                "  {{first_name}} or {{title}}. Greet with 'Hi there' or the company's team."
            )
        else:
            merge_rule = (
                "- Use {{first_name}}, {{company}}, {{title}} as merge variables — every email\n"
                "  must use at least one, and email 1 must reference something specific to\n"
                "  these prospects' industry or role (use the notes above)."
            )

        prompt = self.brain.load_prompt(
            "writer",
            product_name=self.config.product.name,
            product_description=self.config.product.description,
            product_benefits="\n".join(f"- {b}" for b in self.config.product.key_benefits),
            product_pricing=self.config.product.pricing,
            persona_name=self._persona_name(),
            persona_company=self.config.persona.company,
            persona_role=self.config.persona.role,
            persona_tone=self.config.persona.tone,
        )

        if not prompt:
            who = (f"You write for {self.config.persona.company} as {self._persona_name()}."
                   if self.multi else
                   f"You are {self.config.persona.name}, {self.config.persona.role} at "
                   f"{self.config.persona.company}.")
            prompt = f"""{who}
Your tone is: {self.config.persona.tone}

Product: {self.config.product.name}
Description: {self.config.product.description}
Key benefits: {', '.join(self.config.product.key_benefits)}
Pricing: {self.config.product.pricing}"""

        # Inject email framework skills
        if self.skills:
            prompt += "\n\n" + self.skills
        # One template goes to the whole batch: founder framing only when
        # every prospect in it is a startup.
        backed = [await self._startup(p.company_id) for p in prospects]
        if prospects and all(b is not None for b in backed):
            prompt += self._startup_playbook()
        # The segment's playbook when the whole batch is in one segment, and
        # for an old-site segment only when every site may be pitched as old.
        # Age facts stay out of this template: they are each company's own.
        sites = [await self._site_for(p.company_id, mx=True) for p in prospects]
        kinds = {self._segment(p, s) for p, s in zip(prospects, sites)}
        if len(kinds) == 1:
            segment = kinds.pop()
            old_pitch = all(eligible_for_old_site_pitch(s) for s in sites)
            prompt += self._segment_playbook(segment, old_pitch)
        # Campaign notes only: this template goes to the whole batch, so a
        # note about one person ("met her at the expo") must never reach it.
        prompt += await self._feedback_block([])
        rules = getattr(self, "email_rules", "")
        prompt += operator_rules_block(rules, multi=self.multi)
        if rules:
            shape = (
                "- Email 1 is the first touch, email 2 a follow-up 3 days later, email 3 a\n"
                "  last one 4 days after that. Follow the OPERATOR'S EMAIL RULES above for\n"
                "  each one's length, structure, what it says about the product, links and\n"
                "  subject line.")
            style = ""
            strict = ("- Follow every rule in the STRICT EMAIL RULES above that the operator's\n"
                      "  rules don't replace.")
        else:
            shape, style = SEQUENCE_SHAPE, "\n" + SEQUENCE_STYLE
            strict = "- Follow every rule in the STRICT EMAIL RULES above. No exceptions."
        # One template for the batch, sent from whichever mailbox each has.
        sign_off = "\n" + sign_off_rule("every email") if self.multi else ""

        prompt += f"""

Write a 3-email cold outreach sequence for prospects like these:
{fence(prospect_summary, "sample prospects")}

{UNTRUSTED_RULE}

Requirements:
{shape}
{merge_rule}{style}
{footer_rule(self.config, rules)}
{strict}{sign_off}

Return ONLY a JSON array (no markdown fences, no commentary):
[
  {{"step": 1, "subject": "...", "body": "...", "delay_days": 0}},
  {{"step": 2, "subject": "...", "body": "...", "delay_days": 3}},
  {{"step": 3, "subject": "...", "body": "...", "delay_days": 4}}
]"""

        result = await self.brain.think_json(
            prompt, session_id="harvey-writer",
            agent="writer", task="write_sequence",
        )
        # Read right after the call, before anything else can use the brain.
        self._no_reply = result is None and bool(getattr(self.brain, "last_reply_empty", False))
        steps = self._parse_sequence(result)
        if inbox_batch and any(
            PERSON_VAR.search(s.subject + s.body) for s in steps
        ):
            # Nobody's name is known: "Hi {{first_name}}" or "as {{title}}"
            # would render as "Hi there" at best and "as General inbox" at worst.
            logger.warning("Writer: inbox sequence used a person merge variable; discarding it.")
            return []
        polished = await humanize(
            self.brain, [{"subject": s.subject, "body": s.body} for s in steps],
            agent="writer", enabled=humanizer_enabled(self.config),
        )
        for step, draft in zip(steps, polished):
            step.subject, step.body = draft["subject"], draft["body"]
        return steps

    def _parse_sequence(self, result) -> list[EmailStep]:
        """Robustly coerce LLM output into a validated EmailStep list.

        Never raises. Tolerates a wrapper dict ({"emails": [...]}), missing
        or wrong-typed fields, and extra keys. Drops invalid steps rather
        than failing the whole sequence.
        """
        if isinstance(result, dict):
            # Model wrapped the array in an object — unwrap common keys.
            for key in ("emails", "sequence", "steps", "campaign"):
                if isinstance(result.get(key), list):
                    result = result[key]
                    break
        if not isinstance(result, list) or not result:
            logger.error(f"Writer: Brain did not return an email array (got {type(result).__name__}).")
            return []

        steps: list[EmailStep] = []
        for i, raw in enumerate(result[:5]):  # never accept absurdly long sequences
            if not isinstance(raw, dict):
                logger.warning(f"Writer: Skipping non-dict step at index {i}.")
                continue
            subject = str(raw.get("subject") or "").strip()
            body = str(raw.get("body") or "").strip()
            if not subject or not body:
                logger.warning(f"Writer: Skipping step {i + 1} with empty subject/body.")
                continue
            try:
                delay = max(0, int(raw.get("delay_days", 3 if steps else 0)))
            except (TypeError, ValueError):
                delay = 3 if steps else 0
            try:
                steps.append(
                    EmailStep(
                        step=len(steps) + 1,
                        subject=subject,
                        body=body,
                        delay_days=delay,
                    )
                )
            except Exception as e:
                logger.warning(f"Writer: Skipping invalid step {i + 1}: {e}")

        if steps and steps[0].delay_days != 0:
            steps[0].delay_days = 0  # first email always sends immediately

        if len(steps) < 3:
            logger.warning(f"Writer: Expected 3 emails, got {len(steps)}.")
        return steps

    def _group_prospects(self, prospects: list, segments: dict | None = None) -> dict[str, list]:
        """Group prospects into campaign batches by website segment
        ("Shopify-outreach", "Custom-inbox-outreach"). ``segments`` maps a
        prospect id to its segment (from SITE_PLATFORM); without one, the
        prospect's industry label is the fallback (segments.segment_of)."""
        batches: dict[str, list] = {}

        for prospect in prospects:
            segment = (segments or {}).get(prospect.id) or segment_of(None, [], prospect.industry)
            # Inbox prospects get their own campaign: their templates can't
            # greet anyone by name.
            kind = "inbox-outreach" if prospect.is_company_inbox else "outreach"
            key = f"{SEGMENT_LABELS.get(segment, 'Other')}-{kind}"

            if key not in batches:
                batches[key] = []
            batches[key].append(prospect)

        # Cap batch size. Native mode stays small because email 1 is drafted
        # per prospect (one Claude call each); leftovers ride the next cycle.
        # Small segments also outperform blasts on deliverability.
        cap = NATIVE_BATCH_CAP if self.is_native else LEGACY_BATCH_CAP
        return {key: group[:cap] for key, group in batches.items()}
