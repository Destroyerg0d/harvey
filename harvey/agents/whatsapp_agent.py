"""WhatsApp agent: one paced session in the dedicated Chrome profile.

A run, one at a time:

1. Read: the chat list's unread rows, opening only chats whose number is a
   known contact's. No other chat on the account is opened, read or stored.
2. Replies: the Handler's rules, plus "no" on its own (the first message asks
   for exactly that). A no declines them for good and stops their email too;
   questions and interest get a draft reply.
3. Messages: a first message for each queued contact within the day's cap, and
   one follow-up 3-5 days later if silent, never more than 3 without a reply.
4. Send what is approved, each inside its recipient's hours: open the chat by
   number with no prefilled text, check it is theirs, type at human speed, check
   the composer holds the draft, press Enter, check the bubble.

Three rules the recipe never bends. Unsure which chat is open, or no composer,
means nothing is typed. Enter is only pressed after the message is marked
maybe_sent, and only a confirmed bubble clears that, so a crash or a missing
bubble can't lead to a second send. And any WhatsApp warning stops the run and
pauses the channel (harvey/whatsapp/guards.py).
"""

import asyncio
import json
import logging
import random
import re
import unicodedata
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from harvey.agents.linkedin_agent import DECLINE_INTENTS, REPLY_INTENTS, _clean_draft, asks_if_automated
from harvey.gate import find_links
from harvey.linkedin.pages import clean_text, names_match
from harvey.linkedin.store import _iso
from harvey.untrusted import UNTRUSTED_RULE, fence
from harvey.whatsapp import guards
from harvey.whatsapp.contacts import find_prospect, stop_reason
from harvey.whatsapp.guards import Breaker, BreakerTripped, active_pause, detect_breaker
from harvey.whatsapp.numbers import digits_of
from harvey.whatsapp.pacing import WhatsAppPacing, recipient_zones
from harvey.whatsapp.pages import (
    CHAT_LIST, COMPOSER, HOME_URL, app_ready, chat_is_for, chat_open, invalid_number,
    parse_chat_list, parse_messages, send_url,
)
from harvey.whatsapp.store import HELD_FOR_REVIEW, HOLDING_STATUSES

logger = logging.getLogger("harvey.whatsapp")

LAST_RUN_KEY = "whatsapp:last_run"
RUNNING_KEY = "whatsapp:running"
AUTOPILOT_KEY = "whatsapp:autopilot"

MAX_CHATS_PER_RUN = 12
MAX_DRAFT_ATTEMPTS = 3
READY_POLLS = 45          # about 90 seconds for WhatsApp Web to load
CHAT_POLLS = 45           # ...and again for a chat, since send?phone= reloads the whole page
SEND_POLLS = 10           # about 15 seconds for the bubble to show
OFFLINE_RECHECK_SECONDS = 15
MEDIA_BODY = "[a photo, voice note or file: open the chat on your phone]"

# Under these many words, as the prompt says.
WORD_LIMITS = {"first": 60, "follow_up": 35, "reply": 80}

_EMOJI_RE = re.compile("[\U0001F1E6-\U0001F1FF\U0001F300-\U0001FAFF\u2600-\u27bf\ufe0f\u200d]")
_DASH_RE = re.compile(r"[\u2013\u2014\u2015]|[ \t]-[ \t]")
_MARKDOWN_RE = re.compile(r"\*\*|__|`|^\s*(?:[-*+]\s|#{1,6}\s|\d+\.\s)|\[[^\]]+\]\(|\*[^*\n]+\*", re.MULTILINE)
_PRICE_RE = re.compile(
    r"[$€£₹¥]\s?\d|\d\s?[$€£₹¥]|"
    r"\b(?:usd|eur|gbp|inr|aud|cad|rs\.?|rupees?|dollars?|euros?|pounds?|bucks)\s?\d|"
    r"\d[\d,.]*\s?k?\s?(?:usd|eur|gbp|inr|aud|cad|rs\b|rupees?|dollars?|euros?|pounds?|bucks|lakhs?)\b|"
    r"\d[\d,.]*\s?(?:/|per\s)(?:mo|month|yr|year|hr|hour)\b|"
    r"\b(?:hundred|thousand|lakh|crore)s?\s+(?:of\s+)?(?:usd|eur|gbp|inr|rupees?|dollars?|euros?|pounds?|bucks)\b|"
    r"\b(?:cost|costs|price|priced|pricing|charge|charges|fee|fees|budget)\b[^.?!\n]{0,40}\d",
    re.IGNORECASE)
# "no" is a word in several languages, so it has to come with a verb: in Spanish
# and Italian ("responde no") as much as in English.
_WAY_OUT_RE = re.compile(
    r"\b(?:reply|say|text|write|answer|type|send|responde|responda|contesta|escribe|rispondi|scrivi)\b"
    r"[^.?!]*\bno\b|"
    r"\bno\b[^.?!]*\b(?:won'?t|will not|stop|not (?:message|write|contact))\b",
    re.IGNORECASE)
# "No" in the languages a first message is most likely to be written in. A way
# out in another script can't be recognised, so that draft goes back and is
# written again rather than sent without one.
_NO_WORDS = frozenset("""
no non nein nee nej nei nie não nao ne nem nu ei tidak hayır hayir nahi nahin нет ні не
नहीं না لا いいえ 不 아니요 아니오 ไม่
""".split())
# In an English draft, these are how it says where the number came from. In
# another language that is left to the prompt and to you.
_LOOKS_ENGLISH_RE = re.compile(r"\b(?:the|you|your|our|and|reply|won't)\b", re.IGNORECASE)
_SAYS_HOW_RE = re.compile(r"\b(?:number|whatsapp)\b", re.IGNORECASE)
_ASKS_TO_TALK_RE = re.compile(
    r"\b(?:call|talk|speak|meet|meeting|book|schedule|zoom|phone|how (?:do|can) (?:i|we) start|"
    r"get started)\b", re.IGNORECASE)
# On its own, what the first message asks for, or just as plain.
_BARE_NO = _NO_WORDS | {"nope", "nah", "n", "stop", "not interested", "no thanks", "no thank you"}
# WhatsApp's own ways of saying stop. Not a number they gave us means stop too.
_STOP_PHRASES = ("stop messaging", "stop texting", "stop writing", "don't message", "do not message",
                 "dont message", "don't text", "do not text", "dont text", "wrong number",
                 "not my number", "never gave you", "didn't give you", "did not give you")
_HOW_DID_YOU_GET_RE = re.compile(
    r"how did you get (?:my|this) (?:number|whatsapp)|where did you get (?:my|this) (?:number|whatsapp)|"
    r"who gave you (?:my|this) number", re.IGNORECASE)


def _parse(value) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "")).replace(tzinfo=None)
    except ValueError:
        return None


async def autopilot_on(state, config) -> bool:
    """WhatsApp autopilot: the dashboard's switch once used, else the config."""
    value = (await state.get_setting(AUTOPILOT_KEY) or "").strip().lower()
    if value in ("on", "off"):
        return value == "on"
    return bool(config.channels.whatsapp.autopilot)


def clean_draft(text: str) -> str:
    """The model's answer, with what a chat doesn't use taken off."""
    text = _clean_draft(text)
    text = re.sub(r"\*\*|__|`", "", text)
    text = re.sub(r"^[ \t]*(?:[-*+]\s+|#{1,6}\s+)", "", text, flags=re.MULTILINE)
    text = re.sub(r"[ \t]+-[ \t]+", ", ", text)
    return text.strip()


def has_way_out(text: str) -> bool:
    """The end of a first message offers an easy way out: reply no. Where "no"
    is the word it has to say so; elsewhere a word for no near the end will do."""
    tail = " ".join((text or "").split()[-18:])
    if _WAY_OUT_RE.search(tail):
        return True
    words = {w.strip(".,!?;:\"'()।。！？").lower() for w in tail.split()}
    words.discard("no")     # "no pressure" isn't a way out
    return bool(words & _NO_WORDS) or any(w in tail for w in ("いいえ", "아니요", "不"))


def draft_problem(kind: str, text: str, booking_url: str = "") -> str:
    """What's wrong with this message under the channel's rules, or "".

    ``booking_url``: the one link this reply may carry (they asked to talk)."""
    body = (text or "").strip()
    if not body:
        return "it is empty"
    words, limit = len(body.split()), WORD_LIMITS[kind]
    if words >= limit:
        return f"it is {words} words, and must be under {limit}"
    if _EMOJI_RE.search(body):
        return "it has an emoji"
    if _DASH_RE.search(body):
        return "it has a dash"
    if _MARKDOWN_RE.search(body):
        return "it has markdown"
    if _PRICE_RE.search(body):
        return "it mentions a price"
    links = find_links(body)
    if links and kind != "reply":
        return "it has a link"
    if links:
        booking = re.sub(r"^https?://", "", booking_url or "").rstrip("/").lower()
        for _, link in links:
            if not booking or not link.rstrip("/").lower().startswith(booking):
                return "it has a link that isn't the booking link they asked for"
    if kind == "first" and not has_way_out(body):
        return "it doesn't end with an easy way out (reply no and I won't message again)"
    if kind == "first" and _LOOKS_ENGLISH_RE.search(body) and not _SAYS_HOW_RE.search(body):
        return "it doesn't say how we got their number (you left this number on our website)"
    return ""


def is_bare_no(text: str) -> bool:
    bare = "".join(c for c in (text or "").lower() if not unicodedata.category(c).startswith(("P", "S")))
    return " ".join(bare.split()) in _BARE_NO


class WhatsAppAgent:
    def __init__(self, brain, state, config, *, driver_factory=None, pacing=None, now=None,
                 sleep=None, rng=None):
        self.brain = brain
        self.state = state
        self.config = config
        self.cfg = config.channels.whatsapp
        self.rng = rng or random.Random()
        self._now = now or (lambda: datetime.now(timezone.utc))
        self.pacing = pacing or WhatsAppPacing(state, self.cfg, now=self._now, rng=self.rng, sleep=sleep)
        if driver_factory is None:
            from harvey.whatsapp.session import open_session
            driver_factory = lambda: open_session(self.cfg)  # noqa: E731
        self.driver_factory = driver_factory
        self.driver = None
        self.dry_run = False
        self.deadline = None
        self.report: dict = {}
        self._failed_contacts: set[str] = set()

    def now(self) -> datetime:
        """Naive UTC, as stored."""
        return self._now().astimezone(timezone.utc).replace(tzinfo=None)

    # ── When it may run ──

    async def _recipient_open(self, contact: dict) -> bool:
        return self.pacing.recipient_open(await recipient_zones(self.state, contact, self.config))

    async def sendable_now(self) -> bool:
        """Something approved can go right now: its recipient's hours are open
        and the caps have room. This is why a run can start at any hour of
        yours: WhatsApp goes by the recipient's clock."""
        for item in await self.state.wa_messages(statuses=("approved",), direction="out", limit=200):
            contact = await self.state.wa_get_contact(item["contact_id"])
            if contact and await self._recipient_open(contact) and \
                    not await self.pacing.blocked(item["kind"]):
                return True
        return False

    async def why_not_now(self, ignore_interval: bool = False, send_only: bool = False) -> str:
        if not self.cfg.enabled:
            return "WhatsApp is off (channels.whatsapp.enabled is false)"
        pause = await active_pause(self.state, self.now())
        if pause:
            return "WhatsApp is paused: " + str(pause.get("reason") or "paused")
        if not await self.pacing.linked_at():
            return "the phone isn't linked yet (run `harvey whatsapp login`)"
        running = _parse(await self.state.get_setting(RUNNING_KEY))
        if running and self.now() - running < timedelta(minutes=self.cfg.max_run_minutes + 15):
            return "a WhatsApp run is already going"
        if not ignore_interval:
            started = _parse((await self.last_run()).get("started"))
            if started and self.now() - started < timedelta(minutes=self.cfg.run_every_minutes):
                return (f"the last WhatsApp run started at {started:%H:%M} UTC; the next one "
                        f"waits {self.cfg.run_every_minutes} minutes")
        if (send_only or not self.pacing.in_hours()) and not await self.sendable_now():
            return f"nothing can go now, and it's outside WhatsApp hours ({self.pacing.hours_text()})"
        return ""

    async def last_run(self) -> dict:
        try:
            data = json.loads(await self.state.get_setting(LAST_RUN_KEY) or "{}")
        except ValueError:
            data = {}
        return data if isinstance(data, dict) else {}

    # ── A run ──

    async def run(self, dry_run: bool = False, ignore_interval: bool = False, send_only: bool = False) -> dict:
        """One session. ``send_only`` (the owner's quiet hours): no reading of
        the inbox and no model calls, just what is already approved and due."""
        reason = await self.why_not_now(ignore_interval, send_only)
        if reason:
            return {"ran": False, "reason": reason}
        self.dry_run = dry_run
        self._failed_contacts = set()
        started = self.now()
        self.deadline = started + timedelta(minutes=self.cfg.max_run_minutes)
        self.report = {"ran": True, "dry_run": dry_run, "send_only": send_only, "started": _iso(started),
                       "outcome": "", "inbound": 0, "declined": 0, "drafted": 0, "sent": 0,
                       "failed": 0, "maybe_sent": 0, "no_whatsapp": 0, "would_send": []}
        await self.state.set_setting(RUNNING_KEY, _iso(started))
        await self.state.set_setting(LAST_RUN_KEY, json.dumps(
            {"started": _iso(started), "finished": None, "outcome": "running"}))
        try:
            async with self.driver_factory() as driver:
                self.driver = driver
                await self._steps(send_only)
            self.report["outcome"] = "done"
        except BreakerTripped as e:
            self.report["outcome"] = "stopped: " + e.breaker.reason
            logger.warning(f"WhatsApp: stopped and paused: {e.breaker.reason}")
        except Exception as e:
            self.report["outcome"] = f"error: {e}"
            logger.error(f"WhatsApp run failed: {e}", exc_info=True)
        finally:
            self.driver = None
            self.report["finished"] = _iso(self.now())
            await self.state.set_setting(RUNNING_KEY, "")
            shown = {k: v for k, v in self.report.items() if k != "would_send"}
            await self.state.set_setting(LAST_RUN_KEY, json.dumps(shown))
            try:
                await self.state.log_action("whatsapp_run", "whatsapp", shown)
            except Exception:
                pass
        return self.report

    async def _steps(self, send_only: bool = False):
        await self.pacing.prune()
        await self._wait_ready()
        if not send_only:
            await self.sync_inbox()
            await self.handle_inbound()
            await self.draft_messages()
        await self.send_approved()

    def _time_left(self) -> bool:
        return self.deadline is None or self.now() < self.deadline

    async def _guard(self, html: str):
        url = (self.driver.url or "").split("?")[0]
        breaker = detect_breaker(url, html)
        if breaker and breaker.kind == "phone_offline":
            # A page still connecting can say this for a moment. Only one that stays counts.
            await self.driver.wait(OFFLINE_RECHECK_SECONDS)
            breaker = detect_breaker(url, await self.driver.html())
        if breaker:
            await guards.trip(self.state, breaker, self.cfg, now=self.now())
            raise BreakerTripped(breaker)

    async def _wait_ready(self) -> str:
        """WhatsApp Web's chat list, however long it takes to load (it syncs
        with the phone), or an exception: nothing runs on a page Harvey can't read."""
        await self.driver.goto(HOME_URL)
        for _ in range(READY_POLLS):
            html = await self.driver.html()
            await self._guard(html)
            if app_ready(html):
                return html
            await self.driver.wait(2)
        raise RuntimeError("WhatsApp Web didn't show its chat list in time, so nothing was done")

    async def _open_chat(self, contact: dict) -> tuple[str, str]:
        """Open this contact's chat by number: ("open" | "invalid" | "timeout", html)."""
        await self.driver.goto(send_url(contact["phone"]))
        html = ""
        for _ in range(CHAT_POLLS):
            html = await self.driver.html()
            await self._guard(html)
            if invalid_number(html):
                return "invalid", html
            if chat_open(html):
                # The bubbles arrive just after the composer.
                await self.driver.wait(self.rng.uniform(2, 4))
                html = await self.driver.html()
                await self._guard(html)
                return "open", html
            await self.driver.wait(2)
        return "timeout", html

    # ── 1. Read: unread chats of known contacts, and nothing else ──

    def _match_row(self, row, by_digits: dict, contacts: list[dict]) -> dict | None:
        """The known contact an unread row is, or None, in which case that chat
        is never opened. A saved name shows no number: it only counts when it is
        exactly one contact's, and the chat still opens by that contact's number."""
        if row.digits:
            return by_digits.get(row.digits)
        same = [c for c in contacts if c.get("name") and names_match(row.title, c["name"])]
        return same[0] if len(same) == 1 else None

    async def sync_inbox(self):
        html = await self.driver.html()
        rows = parse_chat_list(html)
        await self.driver.scroll(CHAT_LIST, 1)
        html = await self.driver.html()
        await self._guard(html)
        seen = {r.title for r in rows}
        rows += [r for r in parse_chat_list(html) if r.title not in seen]
        contacts = [c for c in await self.state.wa_contacts()
                    if c.get("phone") and c["stage"] != "no_whatsapp"]
        by_digits = {digits_of(c["phone"]): c for c in contacts}
        opened = 0
        for row in rows:
            if not row.unread or not self._time_left() or opened >= MAX_CHATS_PER_RUN:
                continue
            contact = self._match_row(row, by_digits, contacts)
            if contact is None:
                continue
            opened += 1
            await self.pacing.between_pages()
            status, chat = await self._open_chat(contact)
            if status != "open" or not chat_is_for(chat, contact["phone"], contact.get("name") or ""):
                logger.info("WhatsApp: an unread chat couldn't be confirmed as a known contact's; "
                            "nothing was stored.")
                continue
            await self.record_chat(contact, parse_messages(chat))

    async def record_chat(self, contact: dict, messages) -> int:
        """Store what the open chat shows that Harvey hasn't recorded: their
        messages (the contact moves to replied) and any you sent from the phone.
        What was waiting to go to them goes back: they've said something since."""
        mine = await self.state.wa_messages(contact_id=contact["id"])
        ids = {m["wa_id"] for m in mine if m["wa_id"]}
        known_in = {clean_text(m["body"]) for m in mine if m["direction"] == "in"}
        known_out = {clean_text(m["body"]) for m in mine if m["direction"] == "out"}
        base = self.now() - timedelta(seconds=len(messages) + 1)
        new_in = 0
        for i, message in enumerate(messages):
            if message.wa_id and message.wa_id in ids:
                continue
            body = message.body if message.text else MEDIA_BODY
            flat = clean_text(body)
            at = base + timedelta(seconds=i + 1)
            if message.direction == "in":
                if not message.wa_id and flat in known_in:
                    continue
                known_in.add(flat)
                if await self.state.wa_add_message(contact["id"], direction="in", kind="reply", body=body,
                                                   status="sent", sent_at=at, wa_id=message.wa_id,
                                                   intent="escalate" if not message.text else ""):
                    new_in += 1
            elif flat not in known_out:
                known_out.add(flat)
                await self.state.wa_add_message(contact["id"], direction="out", kind="reply", body=body,
                                                status="sent", sent_at=at, wa_id=message.wa_id,
                                                approved_by="you on WhatsApp")
        if new_in:
            self.report["inbound"] = self.report.get("inbound", 0) + new_in
            for waiting in await self.state.wa_pending_for(contact["id"]):
                await self.state.wa_skip(waiting["id"], "they wrote after this was drafted")
            await self.state.wa_set_stage(contact["id"], "replied", last_inbound_at=self.now(),
                                          next_action_at=None)
        return new_in

    # ── 2. Replies ──

    def _as_prospect(self, contact: dict):
        name = contact.get("name") or "them"
        return SimpleNamespace(full_name=lambda: name, title="", company="", company_id="")

    async def classify(self, text: str, contact: dict) -> str:
        """The Handler's opt-out and escalation rules first (they override the
        model), this channel's own stop words, then its model classifier."""
        from harvey.agents.handler import ESCALATION_PATTERNS, Handler, _flat, _opt_out_reason
        flat = _flat(text)
        if _opt_out_reason(text, text) or is_bare_no(text) or any(p in flat for p in _STOP_PHRASES):
            return "unsubscribe"
        if any(p in flat for p in ESCALATION_PATTERNS) or _HOW_DID_YOU_GET_RE.search(text or ""):
            return "escalate"
        return await Handler._classify_intent(self, text, self._as_prospect(contact))

    async def handle_inbound(self):
        unread = [m for m in await self.state.wa_messages(direction="in", limit=100000) if not m["intent"]]
        latest: dict[str, dict] = {}
        for message in unread:
            contact = await self.state.wa_get_contact(message["contact_id"])
            if contact is None:
                continue
            try:
                intent = await self.classify(message["body"], contact)
            except Exception as e:
                # The brain is down, not this message: a plain "no" was already
                # honoured above, and what's left is read on the next run.
                if self._brain_paused():
                    continue
                logger.warning(f"WhatsApp: couldn't classify a reply ({e}); it waits for you.")
                intent = "escalate"
            await self.state.wa_update_message(message["id"], intent=intent)
            if intent in DECLINE_INTENTS:
                await self.decline(contact, message["body"], intent)
                latest.pop(contact["id"], None)
                continue
            if contact["stage"] == "declined":
                continue
            if intent == "wrong_person":
                # The number isn't whom we were given it for: never again on
                # WhatsApp, though it says nothing about their email.
                await self._stop_whatsapp(contact, "they said this is the wrong person")
                latest.pop(contact["id"], None)
                continue
            if intent == "interested":
                await self.state.wa_set_stage(contact["id"], "warm")
            if intent in REPLY_INTENTS or intent == "escalate":
                await self.stop_email_pitch(contact)
            latest[contact["id"]] = {**message, "intent": intent}
        for contact_id, message in latest.items():
            if message["intent"] not in REPLY_INTENTS or not self._time_left() or self._brain_paused():
                continue
            contact = await self.state.wa_get_contact(contact_id)
            history = await self.state.wa_messages(contact_id=contact_id)
            if not history or history[-1]["direction"] != "in":
                continue          # answered already (by you, on the phone)
            if await self.state.wa_pending_for(contact_id, kinds=("reply",)):
                continue
            body = await self.draft(contact, "reply", intent=message["intent"], history=history,
                                    their_text=message["body"])
            if body:
                hold = ["they asked whether this is automated"] if asks_if_automated(message["body"]) else []
                await self.add_draft(contact, "reply", body, hold=hold)

    async def _skip_waiting(self, contact: dict, why: str):
        for waiting in await self.state.wa_messages(contact_id=contact["id"], direction="out",
                                                    statuses=("draft", "approved", "failed")):
            await self.state.wa_skip(waiting["id"], why)

    async def _stop_whatsapp(self, contact: dict, why: str):
        await self.state.wa_set_stage(contact["id"], "declined", no_message_reason=why, next_action_at=None)
        await self._skip_waiting(contact, why)
        self.report["declined"] = self.report.get("declined", 0) + 1

    async def _prospect_of(self, contact: dict):
        """Their Harvey contact, linking it if this is how we first learn of it."""
        if contact.get("prospect_id"):
            return await self.state.get_prospect(contact["prospect_id"])
        prospect = await find_prospect(self.state, contact.get("email") or "", contact.get("phone") or "")
        if prospect is not None:
            await self.state.wa_update_contact(contact["id"], prospect_id=prospect.id)
        return prospect

    async def decline(self, contact: dict, text: str, intent: str):
        """A no: declined for good, nothing queued goes, and their email side
        stops too. An address Harvey hasn't met yet is recorded as opted out, so
        finding it later can't start email to someone who already said no."""
        await self._stop_whatsapp(contact, "they said no on WhatsApp")
        prospect = await self._prospect_of(contact)
        email = (contact.get("email") or "").strip().lower()
        if prospect is None and email:
            from harvey.models.prospect import Prospect
            first, _, last = (contact.get("name") or "").partition(" ")
            prospect_id = await self.state.add_prospect(Prospect(
                email=email, first_name=first, last_name=last, status="opted_out", source="whatsapp"))
            prospect = await self.state.get_prospect(prospect_id)
            await self.state.wa_update_contact(contact["id"], prospect_id=prospect_id)
        if prospect is None:
            return
        await self.state.update_prospect_status(prospect.id, "opted_out")
        await self.state.cancel_pending_outbox_for_prospect(prospect.id, "said no on WhatsApp")
        await self.state.stop_company(prospect.id)
        await self.state.log_action("opt_out", "whatsapp", {
            "prospect_id": prospect.id, "rule": "whatsapp", "matched": intent,
            "why": "They said no on WhatsApp.", "message": (text or "")[:1000],
            "whatsapp_contact_id": contact["id"]})

    async def stop_email_pitch(self, contact: dict):
        """They're talking on WhatsApp: no cold email pitch on top of it."""
        from harvey.agents.linkedin_agent import EMAIL_PITCHABLE
        prospect = await self._prospect_of(contact)
        if prospect is None or prospect.status not in EMAIL_PITCHABLE:
            return
        await self.state.update_prospect_status(prospect.id, "replied")
        await self.state.cancel_pending_outbox_for_prospect(prospect.id, "talking on WhatsApp")
        await self.state.stop_company(prospect.id)

    # ── 3. First messages and the follow-up ──

    async def _next_due(self, contact: dict, now: datetime) -> str:
        """Which message this contact is due, or "". A row of that kind in any
        state, skipped included, means it has had its chance: a first message
        you skipped isn't written again, and there is at most one follow-up."""
        rows = await self.state.wa_messages(contact_id=contact["id"])
        if any(m["direction"] == "in" for m in rows):
            return ""
        out = [m for m in rows if m["direction"] == "out"]
        if any(m["status"] in HOLDING_STATUSES for m in out):
            return ""
        kinds = {m["kind"] for m in out}
        if contact["stage"] == "queued":
            return "" if "first" in kinds else "first"
        if contact["stage"] != "messaged" or "follow_up" in kinds:
            return ""
        sent = [m for m in out if m["status"] == "sent"]
        if not sent or len(sent) >= self.cfg.max_unanswered:
            return ""
        last = _parse(sent[-1].get("sent_at")) or now
        due = max(_parse(contact.get("next_action_at")) or last,
                  last + timedelta(days=self.cfg.follow_up_min_days))
        return "follow_up" if due <= now else ""

    async def draft_messages(self):
        used, caps = await self.pacing.used(), await self.pacing.caps()
        waiting = await self.state.wa_messages(statuses=("draft", "approved"), direction="out")
        first_room = caps["first_today"] - used["first_today"] - sum(m["kind"] == "first" for m in waiting)
        room = caps["messages_today"] - used["messages_today"] - len(waiting)
        now = self.now()
        for contact in await self.state.wa_contacts(stages=("queued", "messaged"), oldest_first=True):
            if room <= 0 or not self._time_left() or self._brain_paused():
                break
            which = await self._next_due(contact, now)
            if not which or (which == "first" and first_room <= 0):
                continue
            why = await stop_reason(self.state, self.config, contact)
            if why != (contact.get("no_message_reason") or ""):
                await self.state.wa_update_contact(contact["id"], no_message_reason=why)
            if why:
                continue
            history = await self.state.wa_messages(contact_id=contact["id"])
            body = await self.draft(contact, which, history=history)
            if body and await self.add_draft(contact, which, body):
                room -= 1
                first_room -= which == "first"

    # ── Drafting ──

    def _brain_paused(self) -> str:
        return getattr(self.brain, "paused_reason", "") or ""

    def _facts(self, contact: dict) -> str:
        lines = [f"Their name: {contact.get('name') or '(not given)'}",
                 f"HOW WE GOT THEIR NUMBER: {contact.get('source') or ''}"]
        if contact.get("context"):
            lines.append(f"WHAT THEY TOLD US: {contact['context']}")
        else:
            lines.append("WHAT THEY TOLD US: nothing beyond their number")
        return "\n".join(lines)

    def _history(self, history) -> str:
        out = []
        for m in (history or [])[-12:]:
            who = "Them" if m["direction"] == "in" else "You"
            if m["status"] == "sent" and m["body"]:
                out.append(f"{who}: {m['body']}")
        return "\n\n".join(out)

    async def draft(self, contact: dict, which: str, intent: str = "", history=None,
                    their_text: str = "") -> str:
        """One message by the brain: which = first | follow_up | reply. It has to
        pass ``draft_problem``; what doesn't after a few tries is left unwritten
        and comes up again next run. Nothing is sent to anyone from here."""
        persona, product = self.config.persona, self.config.product
        prompt = self.brain.load_prompt(
            "whatsapp", persona_name=persona.name, persona_role=persona.role,
            persona_company=persona.company, persona_tone=persona.tone,
            product_name=product.name, product_description=product.description)
        from harvey.brain import SKILLS_DIR
        for skill, title in (("house_playbook", "HOUSE PLAYBOOK (voice, offers and what we may claim)"),
                             ("product_knowledge", "PRODUCT KNOWLEDGE")):
            if hasattr(self.brain, "load_skill") and (SKILLS_DIR / f"{skill}.md").exists():
                text = self.brain.load_skill(skill)
                if text:
                    prompt += f"\n\n{title}\n{text}"
        from harvey.lessons import lessons_block
        prompt += await lessons_block(self.state, "whatsapp")
        booking = ""
        if which == "reply" and _ASKS_TO_TALK_RE.search(their_text or ""):
            booking = getattr(getattr(product, "offer", None), "booking_url", "") or ""
        task = {
            "first": ("Write the first message to this person. Say how we got their number, one useful "
                      "thing from WHAT THEY TOLD US if there is one, and finish with the way out: reply "
                      "no and I won't message again. No link, no price. Under 60 words."),
            "follow_up": ("They haven't answered the first message. Write the one follow-up: something "
                          "useful or a different angle, no guilt, no \"bumping this\". No link, no "
                          "price. Under 35 words."),
            "reply": (f"They replied (read as: {intent}). Answer what they said, in their language. "
                      + (f"They asked to talk: the booking link is {booking}. " if booking else "No link. ")
                      + "No price, even if they ask. If they ask whether this is automated, say yes, "
                        "honestly. Under 80 words."),
        }[which]
        prompt += f"\n\n{UNTRUSTED_RULE}\n\nWHAT WE KNOW:\n{fence(self._facts(contact), 'what they told us')}\n"
        if history and self._history(history):
            prompt += f"\nTHE CONVERSATION SO FAR:\n{fence(self._history(history), 'the conversation')}\n"
        prompt += f"\nTASK: {task}\n\nOutput ONLY the message text."
        problem = ""
        for _ in range(MAX_DRAFT_ATTEMPTS):
            again = f"\n\nYour last draft was refused: {problem}. Write it again." if problem else ""
            text = clean_draft(await self.brain.think(prompt + again, session_id="harvey-whatsapp",
                                                      agent="whatsapp", task=f"draft_{which}"))
            if not text:
                if self._brain_paused():
                    return ""
                continue
            if text.lower().startswith(("i can't", "i cannot", "as an ai", "sorry,")):
                continue
            problem = draft_problem(which, text, booking)
            if not problem:
                return text
            logger.info(f"WhatsApp: a {which} draft was refused: {problem}.")
        return ""

    async def add_draft(self, contact: dict, kind: str, body: str, hold=None) -> str | None:
        """Store a draft. On autopilot it is approved at once, unless the review
        (planted links, quoted instructions) or ``hold`` keeps it."""
        if not body:
            return None
        status, approved_by, error = "draft", "", ""
        if not self.dry_run and await autopilot_on(self.state, self.config):
            from harvey.gate import autopilot_review
            reasons = list(hold or []) + autopilot_review("", body, self.config)
            if reasons:
                error = HELD_FOR_REVIEW + "; ".join(reasons)
            else:
                status, approved_by = "approved", "autopilot"
        elif hold:
            error = HELD_FOR_REVIEW + "; ".join(hold)
        message_id = await self.state.wa_add_message(contact["id"], direction="out", kind=kind, body=body,
                                                     status=status, error=error, approved_by=approved_by)
        self.report["drafted"] = self.report.get("drafted", 0) + 1
        return message_id

    # ── 4. Sending ──

    async def _why_not_send(self, contact: dict, item: dict) -> str:
        """Why this approved message must not go after all, or "". Checked just
        before every send: any of this can have changed since it was approved."""
        if contact["stage"] in ("declined", "no_whatsapp", "needs_review") or not contact.get("phone"):
            return f"they are {contact['stage']}"
        rows = await self.state.wa_messages(contact_id=contact["id"])
        if item["kind"] != "reply":
            if any(m["direction"] == "in" for m in rows):
                return "they wrote after this was drafted"
            if item["kind"] == "follow_up" and sum(
                    m["kind"] == "follow_up" and m["status"] == "sent" for m in rows):
                return "they've already had the one follow-up"
        if len(await self.state.wa_unanswered(contact["id"])) >= self.cfg.max_unanswered:
            return f"{self.cfg.max_unanswered} messages without a reply already"
        return await stop_reason(self.state, self.config, contact, replying=item["kind"] == "reply")

    async def send_approved(self):
        order = {"reply": 0, "follow_up": 1, "first": 2}
        items = sorted(await self.state.wa_messages(statuses=("approved",), direction="out"),
                       key=lambda m: (order.get(m["kind"], 3), m["created_at"] or ""))
        for item in items:
            if not self._time_left():
                break
            # The kill switch works mid-run, not just at the start of one.
            if await active_pause(self.state, self.now()):
                break
            if item["contact_id"] in self._failed_contacts:
                continue
            contact = await self.state.wa_get_contact(item["contact_id"])
            if contact is None:
                continue
            # Something may already have reached them: nothing more until you've looked.
            if await self.state.wa_messages(contact_id=contact["id"], statuses=("maybe_sent",)):
                continue
            why = await self._why_not_send(contact, item)
            if why:
                await self.state.wa_skip(item["id"], why)
                continue
            if not await self._recipient_open(contact):
                continue
            blocked = await self.pacing.blocked(item["kind"])
            if blocked:
                if "hour" in blocked:
                    break
                continue
            if self.dry_run:
                self.report["would_send"].append({"kind": item["kind"], "contact": contact.get("name") or "",
                                                  "body": item["body"]})
                continue
            await self.pacing.between_actions()
            if await active_pause(self.state, self.now()):
                break
            current = await self.state.wa_get_message(item["id"])
            if current is None or current["status"] != "approved":
                continue
            try:
                outcome, detail = await self.send_message(contact, item)
            except BreakerTripped:
                raise
            except Exception as e:
                logger.warning(f"WhatsApp: the send recipe broke: {e}")
                outcome, detail = "failed", f"the send recipe broke ({type(e).__name__})"
                current = await self.state.wa_get_message(item["id"])
                if current and current["status"] == "maybe_sent":
                    outcome = "maybe_sent"
            await self._after_send(contact, item, outcome, detail)

    async def _after_send(self, contact: dict, item: dict, outcome: str, detail: str):
        if outcome == "sent":
            self.report["sent"] += 1
            await self.state.set_setting(guards.FAILED_KEY, "0")
            return
        if outcome in ("no_whatsapp", "held"):
            return
        self._failed_contacts.add(contact["id"])
        if outcome == "failed":
            self.report["failed"] += 1
            await self.state.wa_update_message(item["id"], status="failed", error=detail)
        else:
            self.report["maybe_sent"] += 1
        logger.warning(f"WhatsApp: a {item['kind']} wasn't confirmed: {detail or outcome}")
        failures = await self.state.increment_setting(guards.FAILED_KEY)
        if failures >= self.cfg.max_failed_sends_in_a_row:
            reason = (f"{failures} sends in a row didn't go through, which usually means WhatsApp "
                      "changed its page. Check the failed messages before you resume")
            await guards.set_pause(self.state, reason, kind="failures", hours=None, now=self.now())
            raise BreakerTripped(Breaker("failures", reason))

    async def send_message(self, contact: dict, item: dict) -> tuple[str, str]:
        """The recipe: ("sent" | "failed" | "maybe_sent" | "no_whatsapp" | "held", detail)."""
        phone, name, body = contact["phone"], contact.get("name") or "", item["body"]
        status, html = await self._open_chat(contact)
        if status == "invalid":
            await self.state.wa_set_stage(contact["id"], "no_whatsapp", next_action_at=None)
            await self._skip_waiting(contact, "this number isn't on WhatsApp")
            self.report["no_whatsapp"] += 1
            return "no_whatsapp", ""
        if status != "open":
            return "failed", "the chat didn't open"
        if not chat_is_for(html, phone, name):
            return "failed", "couldn't confirm the open chat is theirs, so nothing was typed"
        messages = parse_messages(html)
        if await self.record_chat(contact, messages):
            return "held", ""
        if item["kind"] == "first" and messages:
            return "failed", "this chat already has messages, so a first message would be wrong; check it"
        if item["kind"] != "first" and not messages:
            return "failed", "couldn't read the chat, so can't tell whether they replied"
        if clean_text(await self.driver.text_of(COMPOSER)):
            await self.driver.clear(COMPOSER)
            if clean_text(await self.driver.text_of(COMPOSER)):
                return "failed", "the message box already held text that wouldn't clear"
        if not await self.driver.type_text(COMPOSER, body):
            return "failed", "couldn't find the message box, so nothing was typed"
        want = clean_text(body)
        if clean_text(await self.driver.text_of(COMPOSER)) != want:
            await self.driver.clear(COMPOSER)
            return "failed", "the message box didn't hold exactly the draft, so it wasn't sent"
        html = await self.driver.html()
        await self._guard(html)
        if not chat_is_for(html, phone, name):
            await self.driver.clear(COMPOSER)
            return "failed", "the open chat changed while typing, so nothing was sent"
        before = sum(1 for m in parse_messages(html) if m.direction == "out" and want in clean_text(m.body))
        # From here it may have gone: only a confirmed bubble clears maybe_sent.
        if not await self.state.wa_claim(item["id"]):
            await self.driver.clear(COMPOSER)
            return "held", ""
        await self.driver.press_enter()
        await self.pacing.record(item["kind"])
        for _ in range(SEND_POLLS):
            await self.driver.wait(1.5)
            html = await self.driver.html()
            await self._guard(html)
            mine = [m for m in parse_messages(html) if m.direction == "out"]
            shown = [m for m in mine if want in clean_text(m.body)]
            if mine and want in clean_text(mine[-1].body) and not mine[-1].pending and len(shown) > before:
                sent_at = self.now()
                await self.state.wa_mark_sent(item["id"], at=sent_at)
                if item["kind"] == "first":
                    wait = self.rng.uniform(self.cfg.follow_up_min_days, self.cfg.follow_up_max_days)
                    await self.state.wa_update_contact(contact["id"], next_action_at=sent_at + timedelta(days=wait))
                elif item["kind"] == "follow_up":
                    await self.state.wa_update_contact(contact["id"], next_action_at=None)
                return "sent", ""
        return "maybe_sent", "Enter was pressed but the chat didn't show the message"


class WhatsAppRunner:
    """The heartbeat's hook: starts a WhatsApp run in the background when one is
    due (it takes far longer than a cycle), never two at once, never blocking
    the email sends of the cycle."""

    def __init__(self, brain, state, config, agent_factory=None):
        self.brain, self.state, self.config = brain, state, config
        self.agent_factory = agent_factory or (lambda: WhatsAppAgent(brain, state, config))
        self.task: asyncio.Task | None = None

    @property
    def running(self) -> bool:
        return self.task is not None and not self.task.done()

    async def maybe_start(self, send_only: bool = False) -> str:
        """Start a run if one is due. Returns why not, or "started"."""
        if not getattr(self.config.channels.whatsapp, "enabled", False):
            return "off"
        if self.running:
            return "a run is going"
        agent = self.agent_factory()
        why = await agent.why_not_now(send_only=send_only)
        if why:
            return why
        logger.info("WhatsApp: starting a run in the background.")
        self.task = asyncio.create_task(agent.run(send_only=send_only))
        return "started"

    async def shutdown(self):
        if self.running:
            self.task.cancel()
            try:
                await self.task
            except (asyncio.CancelledError, Exception):
                pass
            await self.state.set_setting(RUNNING_KEY, "")
