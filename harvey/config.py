"""Configuration loader for Harvey. Reads harvey.yaml + .env."""

import logging
import os
import re
from datetime import date as _date, time as _time
from pathlib import Path

import yaml

from harvey.textio import read_user_text
from dotenv import load_dotenv
from pydantic import (
    BaseModel, PrivateAttr, TypeAdapter, ValidationError, field_validator, model_validator,
)

from harvey.money import parse_price_entry
from harvey.paths import PROJECT_ROOT

logger = logging.getLogger("harvey.config")


class ConfigError(Exception):
    """Raised when Harvey's configuration is missing or invalid."""


class ConfigFileNotFoundError(ConfigError, FileNotFoundError):
    """Config file is missing. Subclasses FileNotFoundError for
    backward compatibility with existing callers/tests."""


class PersonaConfig(BaseModel):
    name: str
    company: str
    role: str
    email: str
    linkedin: str
    tone: str


class OfferConfig(BaseModel):
    primary: str = ""
    entry: str = ""
    goal: str = "book_call"  # book_call, start_trial, get_reply
    booking_method: str = "calendar_link"  # calendar_link, suggest_times, ask_preference
    booking_url: str = ""
    meeting_duration: str = "15 minutes"
    meeting_owner: str = ""


class ProductConfig(BaseModel):
    name: str
    description: str
    pricing: str
    key_benefits: list[str]
    objection_responses: dict[str, str]
    offer: OfferConfig = OfferConfig()


class ICPConfig(BaseModel):
    industries: list[str]
    company_size: str
    titles: list[str]
    geography: list[str]
    # Role keywords that indicate a company is in-market right now (a company
    # hiring a "Head of Growth" is buying growth tooling). Empty → falls back
    # to `titles`. Used for careers-page scanning and job-board discovery.
    hiring_signals: list[str] = []
    # Discovery needs a radius, not a place name. Maps each entry in
    # `geography` to "lat,lng,radius_km" — e.g.
    #   "Denver, CO": "39.7392,-104.9903,50"
    # Without one, listings providers can only match on the business name.
    geo_coordinates: dict[str, str] = {}
    # Extra searches per industry for rank discovery, e.g.
    #   "roofing contractor": ["roofer", "roof repair"]
    # Each variant runs in every city alongside the industry itself, so the
    # estimate grows with it. Empty → each industry is its own one search.
    keyword_variants: dict[str, list[str]] = {}
    # Footprint searches for old shops (harvey/footprints.py), opt-in: Scout
    # runs them only when one of these is set. `legacy_niches` ("garden
    # furniture", "model railways") are combined with the built-in footprints
    # ('"Powered by osCommerce"') and `geography`; `footprint_queries` are
    # your own searches, run as written. A few per cycle, never more than
    # Scout's per-cycle query cap.
    footprint_queries: list[str] = []
    legacy_niches: list[str] = []


class WarmupConfig(BaseModel):
    """Cold-email ramp for a new domain or mailbox.

    Harvey doesn't run a warmup network itself; that needs a pool of real
    inboxes (Instantly, Lemwarm, Mailreach...). This is the other half: no
    cold email goes out for ``min_days`` after ``start_date``, then the cold
    cap climbs from ``start_sends`` by ``daily_increase`` a day until it
    reaches ``max_daily_sends``. Replies are never held. Empty
    ``start_date`` means no ramp.
    """

    start_date: str = ""      # YYYY-MM-DD the warmup service was switched on
    min_days: int = 14        # warmup-only days before any cold email
    start_sends: int = 5      # cold-email cap on the first sending day
    daily_increase: int = 3   # added to the cap each day after that
    dkim_selector: str = ""   # for `harvey warmup`, if not a common one

    @field_validator("start_date", "dkim_selector", mode="before")
    @classmethod
    def _blank_is_empty(cls, v, info):
        if v is None:
            return ""
        if info.field_name != "start_date":
            return v
        # YAML reads an unquoted 2026-10-01 as a date (with a time, a datetime).
        if isinstance(v, _date):
            return (v.date() if hasattr(v, "date") and callable(v.date) else v).isoformat()
        if isinstance(v, str) and len(v.strip()) > 10 and v.strip()[10] in " T":
            return v.strip()[:10]  # "2026-10-01 09:00": the day is what counts
        return "" if v is None else v

    @field_validator("min_days", "start_sends", "daily_increase", mode="before")
    @classmethod
    def _blank_is_default(cls, v, info):
        return cls.model_fields[info.field_name].default if v is None else v

    @field_validator("start_date")
    @classmethod
    def _valid_date(cls, v: str) -> str:
        v = (v or "").strip()
        if v:
            try:
                _date.fromisoformat(v)
            except ValueError:
                raise ValueError(f"warmup.start_date '{v}' must be YYYY-MM-DD.")
        return v

    @field_validator("min_days", "start_sends", "daily_increase")
    @classmethod
    def _non_negative(cls, v: int) -> int:
        if v < 0:
            raise ValueError("warmup values must be >= 0")
        return v

    def days_in(self, today: _date | None = None) -> int | None:
        """Days since warmup started, or None when no ramp is configured."""
        if not self.start_date:
            return None
        return ((today or _date.today()) - _date.fromisoformat(self.start_date)).days

    def daily_cap(self, ceiling: int, today: _date | None = None) -> int:
        """Today's cold-email cap under the ramp (never above ``ceiling``)."""
        days = self.days_in(today)
        if days is None:
            return ceiling
        if days < self.min_days:
            return 0
        return min(ceiling, self.start_sends + (days - self.min_days) * self.daily_increase)


class SendWindowConfig(BaseModel):
    """When cold email may go out, in each recipient's local time.

    Off unless ``channels.email.send_window`` is set. A cold email goes out
    only between ``start`` and ``end`` (on weekdays, with ``weekdays_only``)
    in the recipient's own time zone, worked out from what Harvey knows
    about them (see harvey/send_window.py). Recipients whose zone can't be
    told get the window in ``usage.quiet_hours.timezone``; so does everyone
    with ``recipient_timezone: false``. Follow-ups keep their delay_days and
    move into the window. Replies are never held.
    """

    start: str = "08:00"
    end: str = "17:00"
    weekdays_only: bool = True     # Monday to Friday, in every country
    recipient_timezone: bool = True

    @field_validator("start", "end", mode="before")
    @classmethod
    def _valid_clock(cls, v, info):
        if v is None:
            return cls.model_fields[info.field_name].default
        # YAML 1.1 reads an unquoted 17:00 as the number 1020 (base 60).
        if isinstance(v, int) and not isinstance(v, bool) and 0 <= v < 24 * 60:
            v = f"{v // 60:02d}:{v % 60:02d}"
        v = str(v).strip()
        try:
            _time.fromisoformat(v)
        except ValueError:
            raise ValueError(
                f"send_window time '{v}' is not valid. Use 24h HH:MM, e.g. '08:00'."
            )
        return v

    @model_validator(mode="after")
    def _start_before_end(self):
        if _time.fromisoformat(self.start) >= _time.fromisoformat(self.end):
            raise ValueError(
                "send_window.start must be earlier than send_window.end "
                "(a send window can't run past midnight)."
            )
        return self


def send_window_of(config) -> "SendWindowConfig | None":
    """The configured send window, or None when there isn't one."""
    email = getattr(getattr(config, "channels", None), "email", None)
    window = getattr(email, "send_window", None)
    return window if isinstance(window, SendWindowConfig) else None


def operator_timezone(config) -> str:
    """usage.quiet_hours.timezone: the zone Harvey itself runs on."""
    quiet = getattr(getattr(config, "usage", None), "quiet_hours", None)
    return getattr(quiet, "timezone", "") or "UTC"


# A sending address: plain enough to be a file name too (the mailbox's Gmail
# token is stored as data/gmail_tokens/<address>.json).
_MAILBOX_ADDRESS_RE = re.compile(r"^[a-z0-9._%+-]+@[a-z0-9-]+(?:\.[a-z0-9-]+)+$")


class MailboxConfig(BaseModel):
    """One Gmail mailbox Harvey sends from (channels.email.mailboxes).

    ``name`` is the From name and the sign-off; ``daily_limit`` caps this
    mailbox's sends a day (0: max_daily_sends). See harvey/mailboxes.py.
    """

    email: str
    name: str
    daily_limit: int = 0

    @field_validator("email", "name", mode="before")
    @classmethod
    def _strip(cls, v, info):
        if v is None:
            return ""
        if not isinstance(v, str):
            raise ValueError(f"mailbox {info.field_name} must be text")
        v = v.strip()
        return v.lower() if info.field_name == "email" else v

    @field_validator("email")
    @classmethod
    def _looks_like_address(cls, v: str) -> str:
        if not _MAILBOX_ADDRESS_RE.match(v):
            raise ValueError(f"'{v}' is not an email address, e.g. name@yourdomain.com")
        return v

    @field_validator("name")
    @classmethod
    def _has_name(cls, v: str) -> str:
        if not v:
            raise ValueError("every mailbox needs a name (the From name it signs with)")
        return v

    @field_validator("daily_limit", mode="before")
    @classmethod
    def _blank_limit(cls, v):
        return 0 if v is None else v

    @field_validator("daily_limit")
    @classmethod
    def _limit_non_negative(cls, v: int) -> int:
        if v < 0:
            raise ValueError("daily_limit must be >= 0 (0 means max_daily_sends)")
        return v


class InboundConfig(BaseModel):
    """Follow-ups to people who gave their address in the website's live chat
    (channels.email.inbound, harvey/inbound.py).

    ``enabled`` turns on the background poll; `harvey inbound run` works
    either way. Each draft waits for you, held for review, unless ``autopilot``
    (its own switch: channels.email.require_approval doesn't change it, and
    `harvey sending pause` stops it like everything else). A written estimate
    waits for you whatever this says."""

    enabled: bool = False
    # A chat counts as finished once its last message is this old.
    idle_minutes: int = 30
    poll_minutes: int = 5
    autopilot: bool = False
    # A person on our side wrote in the chat this recently: it's theirs.
    rep_active_days: int = 14
    # The only amounts a written estimate may name, each with its currency
    # (harvey/money.py reads any common format). Empty: no estimates, and leads
    # ready for one get an ordinary email instead.
    price_list: list[str] = []

    @field_validator("price_list", mode="before")
    @classmethod
    def _valid_price_list(cls, v):
        """A typo here must stop Harvey, not quietly refuse every estimate."""
        if v is None:
            return []
        if isinstance(v, str):
            v = [v]
        if not isinstance(v, (list, tuple)):
            raise ValueError('price_list must be a list of amounts, e.g. ["$1,200", "EUR 2,400"]')
        for item in v:
            if parse_price_entry(item) is None:
                raise ValueError(f"'{item}' in price_list isn't one amount with its currency, "
                                 'e.g. "$1,200". Quote each one.')
        return list(v)

    @field_validator("idle_minutes", "rep_active_days")
    @classmethod
    def _not_negative(cls, v: int) -> int:
        if v < 0:
            raise ValueError("must be 0 or more")
        return v

    @field_validator("poll_minutes")
    @classmethod
    def _at_least_a_minute(cls, v: int) -> int:
        if v < 1:
            raise ValueError("poll_minutes must be at least 1")
        return v


def inbound_of(config) -> InboundConfig:
    """channels.email.inbound, or the defaults (off) when a config has none."""
    email = getattr(getattr(config, "channels", None), "email", None)
    found = getattr(email, "inbound", None)
    return found if isinstance(found, InboundConfig) else InboundConfig()


class EmailChannelConfig(BaseModel):
    enabled: bool = True
    # "instantly" (legacy), "gmail" (Gmail/Workspace via API — recommended),
    # "microsoft" (Microsoft 365 / Outlook via Graph), or "smtp" (any
    # SMTP+IMAP mailbox: AgentMail, Fastmail, ...)
    provider: str = "instantly"
    max_daily_sends: int = 50
    # When True, also send to catch-all ("risky") domains, not just verified
    # mailboxes. Off by default — catch-alls accept everything, so a bad
    # guess still bounces.
    send_to_risky: bool = False
    # Native providers queue mail for approval. Instantly cannot use that
    # outbox, so it holds campaigns and escalates replies while this is true.
    # Set false explicitly for full autopilot once you trust the output.
    require_approval: bool = True
    # Your own domains besides the sending address's and the booking link's
    # (e.g. your main site when you send from a secondary domain). On
    # autopilot, an email linking anywhere else waits in the Outbox.
    link_domains: list[str] = []
    # Kill switch: pause all sending when bounces exceed this fraction of
    # sent mail (measured over the trailing sends). 0 disables the switch.
    max_bounce_rate: float = 0.05
    # New domain? Ramp cold email up slowly (see WarmupConfig). Native
    # providers only: Instantly has its own warmup and per-account limits.
    warmup: WarmupConfig = WarmupConfig()
    # Send cold email only in office hours, in each recipient's own time
    # zone (see SendWindowConfig). None: no window, as before. Native
    # providers only; Instantly schedules its own sends.
    send_window: SendWindowConfig | None = None
    # More than one sending mailbox (Gmail only). Empty: Harvey sends from
    # persona.email alone, as before. When set, persona.email must be one of
    # them: it is the primary, which keeps data/gmail_token.json.
    mailboxes: list[MailboxConfig] = []
    # Each segment's target share of the day's first emails, e.g.
    # {custom: 0.40, wordpress: 0.35, shopify: 0.25} (shares are scaled to add
    # up to 1). Follow-ups and replies always go first; a segment with
    # nothing due leaves its share to the others. The custom segment ramps up
    # from 15% (harvey/segment_mix.py). Empty: first emails go in queue
    # order, as before.
    segment_weights: dict[str, float] = {}
    # Segments whose first emails you see before they go out on autopilot,
    # e.g. [wordpress, custom]: a few wait in the Outbox as samples and the
    # rest wait in the queue; approve one sample and that segment sends on
    # its own from then on. Empty: no samples.
    review_segments: list[str] = []
    # Follow-ups to people who gave their address in the website chat.
    inbound: InboundConfig = InboundConfig()

    @field_validator("inbound", mode="before")
    @classmethod
    def _blank_inbound(cls, v):
        return InboundConfig() if v is None else v

    @field_validator("review_segments", mode="before")
    @classmethod
    def _valid_review_segments(cls, v):
        if v is None:
            return []
        if isinstance(v, str):
            v = [v]
        from harvey.segment_mix import MIX_SEGMENTS
        out = []
        for key in v:
            name = str(key).strip().lower()
            if name not in MIX_SEGMENTS:
                raise ValueError(f"'{key}' in review_segments is not a segment; use "
                                 + ", ".join(MIX_SEGMENTS) + ".")
            if name not in out:
                out.append(name)
        return out

    @field_validator("segment_weights", mode="before")
    @classmethod
    def _valid_segment_weights(cls, v):
        if v is None:
            return {}
        if not isinstance(v, dict):
            raise ValueError("segment_weights must be a mapping such as {custom: 0.4, shopify: 0.6}")
        from harvey.segment_mix import MIX_SEGMENTS
        out = {}
        for key, value in v.items():
            name = str(key).strip().lower()
            if name not in MIX_SEGMENTS:
                raise ValueError(f"'{key}' in segment_weights is not a segment; use "
                                 + ", ".join(MIX_SEGMENTS) + ".")
            if isinstance(value, bool):
                raise ValueError(f"segment_weights.{name} must be a number, e.g. 0.4")
            try:
                weight = float(value)
            except (TypeError, ValueError):
                raise ValueError(f"segment_weights.{name} must be a number, e.g. 0.4") from None
            if weight < 0 or weight != weight:
                raise ValueError(f"segment_weights.{name} must be 0 or more")
            out[name] = weight
        return out

    @field_validator("warmup", mode="before")
    @classmethod
    def _blank_warmup(cls, v):
        return WarmupConfig() if v is None else v

    @field_validator("mailboxes", mode="before")
    @classmethod
    def _blank_mailboxes(cls, v):
        return [] if v is None else v

    @field_validator("max_daily_sends")
    @classmethod
    def _sends_non_negative(cls, v: int) -> int:
        if v < 0:
            raise ValueError("max_daily_sends must be >= 0")
        return v

    @model_validator(mode="after")
    def _mailboxes_fit(self):
        if not self.mailboxes:
            return self
        seen: set[str] = set()
        for mailbox in self.mailboxes:
            if mailbox.email in seen:
                raise ValueError(
                    f"channels.email.mailboxes lists {mailbox.email} twice; "
                    "each mailbox goes in once."
                )
            seen.add(mailbox.email)
        if (self.provider or "").strip().lower() != "gmail":
            raise ValueError(
                "channels.email.mailboxes works with provider: gmail only "
                f"(this config has provider: {self.provider or 'none'})."
            )
        return self

    def cold_daily_cap(self, today: _date | None = None) -> int:
        """Today's cap on cold (sequence) email: max_daily_sends, throttled
        by the warmup ramp. Replies count only toward max_daily_sends."""
        return self.warmup.daily_cap(self.max_daily_sends, today)


def cold_daily_cap(email_config, today: _date | None = None) -> int:
    """cold_daily_cap for any email config, including ones without a ramp."""
    warmup = getattr(email_config, "warmup", None)
    if isinstance(warmup, WarmupConfig):
        return warmup.daily_cap(email_config.max_daily_sends, today)
    return email_config.max_daily_sends


def legacy_sending_held(config) -> bool:
    """True when Instantly is the provider and approval is required.

    Instantly has no per-email approval queue, so with require_approval on
    Harvey holds campaign deployment and hands replies to a human.
    """
    email = getattr(getattr(config, "channels", None), "email", None)
    return (getattr(email, "provider", "") == "instantly"
            and bool(getattr(email, "require_approval", True)))


def sending_notice(config) -> str:
    """A plain-language warning when outbound sending is held by config, or ""."""
    if not legacy_sending_held(config):
        return ""
    return (
        "Instantly sending is held: channels.email.require_approval is true, and "
        "Instantly has no approval queue. Campaigns stay as drafts and replies go to "
        "a human (dashboard Activity tab). Set require_approval: false to let Instantly "
        "send automatically, or switch to gmail, microsoft or smtp to approve each email."
    )


class LinkedInHoursConfig(BaseModel):
    """When Harvey may act on LinkedIn (harvey/linkedin/pacing.py)."""

    start: str = "10:00"
    end: str = "19:00"
    timezone: str = "Asia/Kolkata"
    weekdays_only: bool = True

    @field_validator("start", "end")
    @classmethod
    def _clock(cls, v: str) -> str:
        try:
            _time.fromisoformat(str(v))
        except ValueError:
            raise ValueError(f"'{v}' is not a time like 10:00")
        return str(v)

    @field_validator("timezone")
    @classmethod
    def _zone(cls, v: str) -> str:
        import pytz
        try:
            pytz.timezone(v)
        except pytz.UnknownTimeZoneError:
            raise ValueError(f"'{v}' is not a time zone name like Asia/Kolkata")
        return v


class LinkedInChannelConfig(BaseModel):
    """LinkedIn invites, messages, follow-ups and replies (harvey/linkedin/).

    Automating a LinkedIn account is against LinkedIn's terms: these limits
    are the account's safety margin, so they are deliberately low."""

    enabled: bool = False
    # Daily caps: connection invites, and messages (first messages,
    # follow-ups and replies together).
    max_daily_connections: int = 15
    max_daily_messages: int = 25
    max_weekly_invites: int = 75
    # Sends, invites and withdrawals in any one hour, and old invites withdrawn a day.
    max_actions_per_hour: int = 12
    max_daily_withdrawals: int = 10
    # Drafts wait for your approval on the dashboard until this is true
    # (the dashboard's switch overrides it once used).
    autopilot: bool = False
    hours: LinkedInHoursConfig = LinkedInHoursConfig()
    # Human pacing: a random wait between actions, and a longer break every few.
    min_delay_seconds: int = 60
    max_delay_seconds: int = 240
    break_every_actions: int = 5
    break_min_seconds: int = 300
    break_max_seconds: int = 900
    # One run at a time, at most one every run_every_minutes, each at most this long.
    run_every_minutes: int = 120
    max_run_minutes: int = 90
    # A warning, captcha, sign-in challenge or limit notice pauses LinkedIn this long.
    pause_hours_on_warning: int = 48
    withdraw_after_days: int = 21
    # First message 1-3 days after they accept, one follow-up 5-7 days later
    # if silent, never more than max_unanswered messages without a reply.
    first_message_min_days: int = 1
    first_message_max_days: int = 3
    follow_up_min_days: int = 5
    follow_up_max_days: int = 7
    max_unanswered: int = 3
    # A short note with each invite while the account allows notes.
    invite_notes: bool = True
    invite_note_max_chars: int = 200
    # Who gets invites: Harvey contacts with a LinkedIn URL and one of these in their title.
    invite_titles: list[str] = ["founder", "co-founder", "owner", "ceo", "chief executive",
                                "managing director", "director", "president", "proprietor",
                                "partner"]
    # The dedicated Chrome profile (relative to the project), and Chrome itself
    # ("" finds the installed Google Chrome).
    profile_dir: str = "data/linkedin-profile"
    login_timeout_minutes: int = 30
    chrome_path: str = ""
    headless: bool = False

    @field_validator("max_daily_connections", "max_daily_messages", "max_weekly_invites",
                     "max_actions_per_hour", "max_daily_withdrawals", "min_delay_seconds",
                     "max_delay_seconds", "break_every_actions", "break_min_seconds",
                     "break_max_seconds", "run_every_minutes", "max_run_minutes",
                     "pause_hours_on_warning", "withdraw_after_days", "first_message_min_days",
                     "first_message_max_days", "follow_up_min_days", "follow_up_max_days",
                     "max_unanswered", "invite_note_max_chars")
    @classmethod
    def _not_negative(cls, v: int) -> int:
        if v < 0:
            raise ValueError("must be 0 or more")
        return v

    @model_validator(mode="after")
    def _ranges(self):
        for low, high in (("min_delay_seconds", "max_delay_seconds"),
                          ("break_min_seconds", "break_max_seconds"),
                          ("first_message_min_days", "first_message_max_days"),
                          ("follow_up_min_days", "follow_up_max_days")):
            if getattr(self, low) > getattr(self, high):
                raise ValueError(f"channels.linkedin.{low} is more than {high}")
        return self


class WhatsAppHoursConfig(LinkedInHoursConfig):
    """A message goes out only in these hours of the recipient's own time zone
    when Harvey knows it, and of ``timezone`` when it doesn't."""

    @model_validator(mode="after")
    def _daytime(self):
        if _time.fromisoformat(self.start) >= _time.fromisoformat(self.end):
            raise ValueError("channels.whatsapp.hours.start must be before end")
        return self


class WhatsAppChannelConfig(BaseModel):
    """First messages, one follow-up and replies through WhatsApp Web
    (harvey/whatsapp/), only to people who gave us their number themselves.

    Automating WhatsApp breaks its terms and can get the number banned, so use a
    separate number. A new number is the riskiest pattern there is: these limits
    are its safety margin, so they are deliberately low."""

    enabled: bool = False
    # Drafts wait for your approval until this is true (the dashboard's switch
    # overrides it once used).
    autopilot: bool = False
    # The linked number, shown on the dashboard so you can tell which phone it is.
    number: str = ""
    # First messages a day: this few for new_number_days after linking, then more.
    max_daily_first_messages_new: int = 5
    max_daily_first_messages: int = 10
    new_number_days: int = 14
    # All messages (first, follow-up, reply), a day and in any one hour.
    max_daily_messages: int = 30
    max_messages_per_hour: int = 8
    hours: WhatsAppHoursConfig = WhatsAppHoursConfig()
    # Human pacing: a random wait between actions, and a longer break every few.
    min_delay_seconds: int = 45
    max_delay_seconds: int = 180
    break_every_actions: int = 5
    break_min_seconds: int = 300
    break_max_seconds: int = 900
    # One run at a time, at most one every run_every_minutes, each at most this long.
    run_every_minutes: int = 60
    max_run_minutes: int = 60
    # A ban or warning pauses this long; a phone that dropped off, this long;
    # this many failed sends in a row pause until you resume.
    pause_hours_on_warning: int = 48
    pause_minutes_phone_offline: int = 30
    max_failed_sends_in_a_row: int = 3
    # One follow-up 3-5 days later if silent, never more than max_unanswered
    # messages without a reply.
    follow_up_min_days: int = 3
    follow_up_max_days: int = 5
    max_unanswered: int = 3
    # How long `harvey whatsapp login` waits for you to scan the QR code.
    login_timeout_minutes: int = 30
    # Numbers never to message, cold or in reply (compliance.do_not_contact
    # takes addresses and domains, not numbers). Each with its + country code.
    do_not_contact: list[str] = []
    # The dedicated Chrome profile (relative to the project), and Chrome itself
    # ("" finds the installed Google Chrome). Always visible: no headless setting.
    profile_dir: str = "data/whatsapp-profile"
    chrome_path: str = ""

    @field_validator("max_daily_first_messages_new", "max_daily_first_messages", "new_number_days",
                     "max_daily_messages", "max_messages_per_hour", "min_delay_seconds",
                     "max_delay_seconds", "break_every_actions", "break_min_seconds",
                     "break_max_seconds", "run_every_minutes", "max_run_minutes",
                     "pause_hours_on_warning", "pause_minutes_phone_offline",
                     "max_failed_sends_in_a_row", "follow_up_min_days", "follow_up_max_days",
                     "max_unanswered", "login_timeout_minutes")
    @classmethod
    def _not_negative(cls, v: int) -> int:
        if v < 0:
            raise ValueError("must be 0 or more")
        return v

    @field_validator("do_not_contact")
    @classmethod
    def _numbers(cls, v: list[str]) -> list[str]:
        from harvey.whatsapp.numbers import normalize_phone
        out = []
        for raw in v or []:
            number = normalize_phone(raw)
            if not number.ok:
                raise ValueError(f"'{raw}' isn't a number with a + country code: {number.problem}")
            out.append(number.e164)
        return out

    @model_validator(mode="after")
    def _ranges(self):
        for low, high in (("min_delay_seconds", "max_delay_seconds"),
                          ("break_min_seconds", "break_max_seconds"),
                          ("follow_up_min_days", "follow_up_max_days")):
            if getattr(self, low) > getattr(self, high):
                raise ValueError(f"channels.whatsapp.{low} is more than {high}")
        return self


class ChannelsConfig(BaseModel):
    email: EmailChannelConfig = EmailChannelConfig()
    linkedin: LinkedInChannelConfig = LinkedInChannelConfig()
    whatsapp: WhatsAppChannelConfig = WhatsAppChannelConfig()


PRECLEARED_ADDRESS_SOURCES = ("published", "bought", "scraped", "given", "unknown")


def normalize_prospect_country(v) -> str:
    """A two-letter country code ("us" -> "US", "UK" -> "GB"), or "" for none.

    Raises ValueError on anything else, including the booleans YAML makes of
    an unquoted NO (Norway) or ON, so a typo is never silently ignored.
    """
    if isinstance(v, bool):
        raise ValueError(
            f"prospect_country was read as {v}. YAML turns an unquoted NO or "
            'ON into true/false, so quote the code, e.g. prospect_country: "NO".'
        )
    v = str(v or "").strip().upper()
    v = {"UK": "GB", "USA": "US"}.get(v, v)
    if v and not (len(v) == 2 and v.isascii() and v.isalpha()):
        raise ValueError(
            f"'{v}' is not a valid prospect_country. Use a two-letter "
            "country code such as US or GB, or leave it empty."
        )
    return v


class PreclearedConfig(BaseModel):
    """Optional per-recipient cold-email check through Precleared.

    Off by default. When on, each new recipient is checked once, before the
    first cold email, against UK PECR, US CAN-SPAM, Canada's CASL, Germany's
    UWG and France's rules. Any verdict other than ok_to_email holds the
    email back with the reason. See harvey/integrations/precleared.py.
    """

    enabled: bool = False
    # Where your business is based: GB or US. Required when enabled.
    sender_country: str = ""
    # Where prospect addresses come from, in Precleared's terms. Harvey
    # builds most addresses from a learned pattern, so the honest default
    # is "unknown"; use "published" only if yours are copied from the
    # businesses' own websites.
    address_source: str = "unknown"
    # Optional: the country your prospects are in (two letters, e.g. "US"),
    # if they are all in one. Precleared uses it only when it can't work out
    # the recipient's country from the address, company records or the
    # company's website. Leave empty if your prospects are spread out.
    prospect_country: str = ""
    # Optional: countries (two letters, e.g. ["US"]) whose recipients aren't
    # checked, to save checks where you already know the answer. Only when
    # Harvey is sure of the recipient's country from the company's location;
    # prospect_country never counts. A verdict already stored still applies.
    skip_countries: list[str] = []

    @field_validator("sender_country", mode="before")
    @classmethod
    def _upper_country(cls, v) -> str:
        return str(v or "").strip().upper()

    @field_validator("address_source", mode="before")
    @classmethod
    def _valid_source(cls, v) -> str:
        v = str(v or "unknown").strip().lower()
        if v not in PRECLEARED_ADDRESS_SOURCES:
            raise ValueError(
                f"'{v}' is not a valid address_source. "
                f"Use one of: {', '.join(PRECLEARED_ADDRESS_SOURCES)}."
            )
        return v

    @field_validator("prospect_country", mode="before")
    @classmethod
    def _valid_prospect_country(cls, v) -> str:
        return normalize_prospect_country(v)

    @field_validator("skip_countries", mode="before")
    @classmethod
    def _valid_skip_countries(cls, v) -> list[str]:
        """Two-letter codes, upper-cased ("uk" -> "GB"), each once. A typo
        or an unquoted NO (YAML's false) is an error, never ignored."""
        if v is None or v == "":
            return []
        if isinstance(v, str):
            v = v.split(",")
        if not isinstance(v, (list, tuple)):
            raise ValueError(
                'skip_countries must be a list of two-letter country codes, e.g. ["US"].')
        codes: list[str] = []
        for item in v:
            if isinstance(item, bool):
                raise ValueError(
                    f"an entry in skip_countries was read as {item}. YAML turns an unquoted "
                    'NO or ON into true/false, so quote the code, e.g. skip_countries: ["NO"].')
            if not isinstance(item, str):
                raise ValueError(
                    f"'{item}' in skip_countries is not a two-letter country code such as US or GB.")
            code = item.strip().upper()
            code = {"UK": "GB", "USA": "US"}.get(code, code)
            if not code:
                continue
            if not (len(code) == 2 and code.isascii() and code.isalpha()):
                raise ValueError(
                    f"'{code}' in skip_countries is not a two-letter country code such as US or GB.")
            if code not in codes:
                codes.append(code)
        return codes

    @model_validator(mode="after")
    def _country_when_enabled(self):
        if self.enabled and self.sender_country not in ("GB", "US"):
            raise ValueError(
                "compliance.precleared.sender_country must be GB or US "
                "when the check is enabled."
            )
        return self


DEFAULT_OPT_OUT_LINE = 'Not interested? Reply "no thanks" and I won\'t email again.'
# The replies the Handler always honours as an opt-out on their own; an
# opt_out_line must ask for one of them ("Reply STOP", 'reply "no thanks"').
OPT_OUT_WORDS = ("no thanks", "no thank you", "unsubscribe", "stop", "remove me",
                 "opt out")
# The same in Hebrew ('השיבו "הסר"', 'תגידו לי "לא תודה"').
HEBREW_OPT_OUT_WORDS = ("הסר", "הסירו", "הסרה", "לא תודה", "הסירו אותי", "תורידו אותי")
_OPT_OUT_WORD_RE = "(?:" + "|".join(
    r"[\s,]+".join(map(re.escape, w.split()))
    for w in OPT_OUT_WORDS + HEBREW_OPT_OUT_WORDS) + ")"
_ASKS_FOR_OPT_OUT_RE = re.compile(
    r"\breply(?:\s+with)?\s*[\"'\u201c\u2018]?\s*" + _OPT_OUT_WORD_RE + r"\b", re.IGNORECASE)
# Reply / answer / write / tell me, then a few words at most ("במייל חוזר").
_ASKS_FOR_OPT_OUT_HE_RE = re.compile(
    r"(?<!\w)ו?(?:השיב[וי]?|השב|תשיב[וי]?|ענ[הוי]|תענ[הוי]|כתוב|כתב[וי]|תכתוב|תכתב[וי]"
    r"|הגב|הגיב[וי]|תגיב[וי]?|שלח[וי]?|תשלח[וי]?|תגיד[וי]?|אמור|אמר[וי])"
    r"(?:\s+[^\s\"'\u05f3\u05f4\u201c\u201d\u2018\u2019]+){0,3}?"
    r"\s*[\"'\u05f3\u05f4\u201c\u201d\u2018\u2019]?\s*" + _OPT_OUT_WORD_RE + r"(?!\w)",
    re.IGNORECASE)
# "Click" in Hebrew: a footer has no link to click.
_HEBREW_CLICK_RE = re.compile(r"(?<!\w)ו?(?:לחצו|לחצי|לחץ\s+(?:כאן|על)|הקליקו|הקליקי|הקלק)(?!\w)")


class ComplianceConfig(BaseModel):
    precleared: PreclearedConfig = PreclearedConfig()
    # Added under every cold (sequence) email at send time; never to replies.
    # CAN-SPAM requires a postal address you receive mail at (a street
    # address, PO box or virtual mailbox) and a working way to opt out.
    postal_address: str = ""
    # Must ask for wording Harvey recognises as an opt-out (OPT_OUT_WORDS,
    # HEBREW_OPT_OUT_WORDS).
    # "" leaves it off (and harvey warns).
    opt_out_line: str = DEFAULT_OPT_OUT_LINE
    # Companies this operator will never email: competitors, existing
    # customers, anyone under a legal hold, a partner you agreed not to
    # approach. An entry is an address ("jane@acme.example") or a domain
    # ("acme.example", which also covers its subdomains). Enforced by the gate at
    # send time, so it holds for an email a human already approved, and for
    # replies as well as cold mail — "never contact" means never.
    do_not_contact: list[str] = []

    @field_validator("postal_address", "opt_out_line", mode="before")
    @classmethod
    def _blank_is_empty(cls, v):
        return "" if v is None else v

    @field_validator("do_not_contact", mode="before")
    @classmethod
    def _clean_do_not_contact(cls, v):
        """One string is a list; blanks and duplicates are dropped.

        Normalising here rather than at the point of use keeps the gate's
        matching free of parsing, and means " @Acme.EXAMPLE " or a pasted
        "https://www.acme.example/" behaves the same as "acme.example". An
        entry that names no address or domain is an error, not a no-op.
        """
        from harvey.gate import do_not_contact_entries
        if v is None:
            return []
        if isinstance(v, str):
            v = [v]
        if not isinstance(v, (list, tuple)):
            raise ValueError("must be a list of email addresses and domains")
        seen: list[str] = []
        for item in v:
            if item is None:
                continue
            if not isinstance(item, str):
                raise ValueError(f"{item!r} is not an email address or a domain")
            for cleaned in do_not_contact_entries(item):
                if cleaned not in seen:
                    seen.append(cleaned)
        return seen

    @field_validator("opt_out_line")
    @classmethod
    def _recognisable_opt_out(cls, v: str) -> str:
        v = v.strip()
        lowered = v.lower()
        if not v:
            return v
        if (any(bit in lowered for bit in ("http", "www.", "click", "@"))
                or _HEBREW_CLICK_RE.search(v)):
            raise ValueError(
                "opt_out_line can't contain a link or an email address: cold email "
                "allows one link, and opting out must be as simple as replying."
            )
        if not (_ASKS_FOR_OPT_OUT_RE.search(v) or _ASKS_FOR_OPT_OUT_HE_RE.search(v)):
            raise ValueError(
                "opt_out_line must ask for a reply Harvey recognises as an opt-out, "
                'e.g. Reply "no thanks" / Reply STOP, or in Hebrew השיבו "הסר". Recognised: '
                + ", ".join(f'"{w}"' for w in OPT_OUT_WORDS + HEBREW_OPT_OUT_WORDS) + "."
            )
        return v


def compliance_of(config) -> ComplianceConfig:
    """The compliance section, or the defaults when a config has none."""
    found = getattr(config, "compliance", None)
    return found if isinstance(found, ComplianceConfig) else ComplianceConfig()


def compliance_footer_lines(config) -> list[str]:
    """The lines added under every cold email: address, then opt-out line."""
    compliance = compliance_of(config)
    return [line for line in (compliance.postal_address.strip(),
                              compliance.opt_out_line.strip()) if line]


def compliance_notice(config) -> str:
    """A plain-language warning when cold email goes out without a postal
    address, or "". Sending isn't held for it: this is the nudge."""
    email = getattr(getattr(config, "channels", None), "email", None)
    if not getattr(email, "enabled", False):
        return ""
    compliance = compliance_of(config)
    missing = []
    if not compliance.postal_address.strip():
        missing.append("a postal address (compliance.postal_address: a street "
                       "address, PO box or virtual mailbox)")
    if not compliance.opt_out_line.strip():
        missing.append("a way to opt out (compliance.opt_out_line)")
    if not missing:
        return ""
    return (
        "Cold emails are going out without " + " or ".join(missing) + ". US law "
        "(CAN-SPAM) requires both in every cold email. Set them in harvey.yaml "
        "(or harvey.local.yaml if you trained Harvey)."
    )


class QuietHoursConfig(BaseModel):
    start: str = "22:00"
    end: str = "07:00"
    timezone: str = "America/New_York"

    @field_validator("start", "end")
    @classmethod
    def _valid_time(cls, v: str) -> str:
        try:
            _time.fromisoformat(v)
        except ValueError:
            raise ValueError(
                f"'{v}' is not a valid time. Use 24h HH:MM format, e.g. '22:00'."
            )
        return v

    @field_validator("timezone")
    @classmethod
    def _valid_timezone(cls, v: str) -> str:
        import pytz

        if v not in pytz.all_timezones_set:
            raise ValueError(
                f"'{v}' is not a valid timezone. Use an IANA name like 'America/New_York'."
            )
        return v


class UsageConfig(BaseModel):
    max_daily_claude_percent: float = 80.0
    heartbeat_interval_minutes: int = 15
    quiet_hours: QuietHoursConfig = QuietHoursConfig()

    @field_validator("max_daily_claude_percent")
    @classmethod
    def _valid_percent(cls, v: float) -> float:
        if not 0 < v <= 100:
            raise ValueError("max_daily_claude_percent must be between 0 and 100")
        return v

    @field_validator("heartbeat_interval_minutes")
    @classmethod
    def _valid_interval(cls, v: int) -> int:
        if v < 1:
            raise ValueError("heartbeat_interval_minutes must be at least 1")
        return v


BRAIN_PROVIDERS = ("claude", "codex")
CODEX_REASONING_EFFORTS = ("minimal", "low", "medium", "high", "xhigh")

# Model names are passed to a CLI, so they must look like a model name and
# never like a flag ("-x") or anything a shell would treat specially.
_MODEL_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/\[\]-]{0,127}$")


class BrainConfig(BaseModel):
    """Which subscription Harvey thinks with.

    ``claude`` (default) runs Claude Code headless on your Claude plan.
    ``codex`` runs Codex on your ChatGPT plan. There is never an automatic
    fallback between them, and an explicit ``model`` only affects Harvey —
    your own Claude Code / Codex settings are never read or changed.
    """

    provider: str = "claude"
    model: str = ""  # empty = the provider's own default
    reasoning_effort: str = ""  # codex only; empty = model default

    @field_validator("provider", mode="before")
    @classmethod
    def _valid_provider(cls, v) -> str:
        v = str(v or "claude").strip().lower()
        if v not in BRAIN_PROVIDERS:
            raise ValueError(
                f"'{v}' is not a supported brain provider. "
                f"Use one of: {', '.join(BRAIN_PROVIDERS)}."
            )
        return v

    @field_validator("model", mode="before")
    @classmethod
    def _valid_model(cls, v) -> str:
        v = str(v or "").strip()
        if v and not _MODEL_NAME_RE.match(v):
            raise ValueError(f"'{v}' is not a valid model name.")
        return v

    @field_validator("reasoning_effort", mode="before")
    @classmethod
    def _valid_effort(cls, v) -> str:
        v = str(v or "").strip().lower()
        if v and v not in CODEX_REASONING_EFFORTS:
            raise ValueError(
                f"'{v}' is not a valid reasoning effort. "
                f"Use one of: {', '.join(CODEX_REASONING_EFFORTS)}."
            )
        return v


class WriterConfig(BaseModel):
    # Run every draft through one extra editing-only brain call using the
    # Humanizer skill's instructions (~/.claude/skills/humanizer/SKILL.md,
    # or HARVEY_HUMANIZER_SKILL). Off by default: installing the skill for
    # your own Claude Code use never changes what Harvey sends on its own.
    humanizer: bool = False
    # Your own rules for the emails Harvey drafts, as a markdown file:
    # length, structure, what to say about the product, links, subject lines,
    # sign-off. When set, it replaces the Writer's built-in shape (email 1
    # under 75 words, an observation and a question, no pitch). A path
    # relative to the Harvey folder; skills/email_rules.md is gitignored.
    # The FACTS rule, the send gate and do-not-contact still apply. A file
    # that is set but can't be read stops writing until it's fixed.
    email_rules: str = ""

    @field_validator("email_rules", mode="before")
    @classmethod
    def _blank_rules(cls, v):
        if v is None:
            return ""
        return v.strip() if isinstance(v, str) else v


# Long enough for a full house style with examples; anything bigger is
# probably the wrong file and would crowd out the facts in every prompt.
MAX_EMAIL_RULES_CHARS = 20_000


class EmailRulesError(ConfigError):
    """writer.email_rules names a file Harvey can't use."""


def load_email_rules(config) -> str:
    """The operator's email rules (writer.email_rules), or "" when none are set.

    Raises EmailRulesError when a file is set but missing, unreadable, empty
    or too long: the Writer then writes nothing rather than fall back to the
    built-in shape the operator chose to replace.
    """
    raw = str(getattr(getattr(config, "writer", None), "email_rules", "") or "").strip()
    if not raw:
        return ""
    path = Path(raw).expanduser()
    if not path.is_absolute():
        path = PROJECT_ROOT / path
    try:
        text = read_user_text(path).strip()
    except FileNotFoundError:
        raise EmailRulesError(f"{raw} doesn't exist") from None
    except OSError as e:
        raise EmailRulesError(f"{raw} can't be read ({e})") from None
    if not text:
        raise EmailRulesError(f"{raw} is empty")
    if len(text) > MAX_EMAIL_RULES_CHARS:
        raise EmailRulesError(
            f"{raw} is {len(text):,} characters; the limit is {MAX_EMAIL_RULES_CHARS:,}")
    return text


class HarveyConfig(BaseModel):
    persona: PersonaConfig
    product: ProductConfig
    icp: ICPConfig
    channels: ChannelsConfig = ChannelsConfig()
    usage: UsageConfig = UsageConfig()
    brain: BrainConfig = BrainConfig()
    compliance: ComplianceConfig = ComplianceConfig()
    writer: WriterConfig = WriterConfig()
    # The file it was loaded from, so a running Harvey can re-read the
    # sending mode from that same file (refresh_sending_mode).
    _source_path: str | None = PrivateAttr(default=None)

    @model_validator(mode="after")
    def _primary_is_a_mailbox(self):
        mailboxes = self.channels.email.mailboxes
        if not mailboxes:
            return self
        primary = (self.persona.email or "").strip().lower()
        if primary not in {m.email for m in mailboxes}:
            raise ValueError(
                f"persona.email ({self.persona.email or 'empty'}) must be one of "
                "channels.email.mailboxes: it is the primary mailbox. Configured: "
                + ", ".join(m.email for m in mailboxes) + "."
            )
        return self


class EnvConfig(BaseModel):
    instantly_api_key: str = ""
    # Discovery providers
    dataforseo_login: str = ""
    dataforseo_password: str = ""
    dataforseo_sandbox: str = ""   # any truthy value routes to the free sandbox
    linkedin_email: str = ""
    linkedin_password: str = ""
    hunter_api_key: str = ""
    serper_api_key: str = ""
    scavio_api_key: str = ""
    tinyfish_api_key: str = ""
    reoon_api_key: str = ""
    zerobounce_api_key: str = ""
    # Free-tier verifiers whose allowances Harvey counts itself
    # (*_MONTHLY_CHECKS and optional *_DAILY_CHECKS in .env, read by
    # integrations/email_finder.py).
    bounceless_api_key: str = ""
    emailverify_api_key: str = ""
    getprospect_api_key: str = ""
    abstract_api_key: str = ""
    mailboxvalidator_api_key: str = ""
    sniffmail_api_key: str = ""
    emailvalidationio_api_key: str = ""
    bouncezero_api_key: str = ""
    myemailverifier_api_key: str = ""
    # Native mail providers (channels.email.provider: gmail | smtp)
    gmail_client_id: str = ""
    gmail_client_secret: str = ""
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_username: str = ""
    smtp_password: str = ""
    imap_host: str = ""
    imap_port: int = 993
    imap_username: str = ""
    imap_password: str = ""
    # Microsoft 365 (channels.email.provider: microsoft)
    m365_client_id: str = ""
    m365_tenant_id: str = ""
    # Optional: only used when compliance.precleared.enabled is true.
    precleared_api_key: str = ""
    # Optional: the website chat's Supabase, read-only (harvey/collectors/erp_chat.py).
    erp_supabase_url: str = ""
    erp_supabase_key: str = ""


def _format_validation_error(e: ValidationError) -> str:
    """Turn a pydantic ValidationError into a readable, actionable message."""
    lines = []
    for err in e.errors():
        loc = ".".join(str(p) for p in err["loc"]) or "(root)"
        lines.append(f"  - {loc}: {err['msg']}")
    return "\n".join(lines)


def load_config(config_path: str | None = None) -> HarveyConfig:
    """Load Harvey configuration from YAML file.

    Raises ConfigError with a clear, actionable message on any problem.
    """
    if config_path is None:
        config_path = _find_config_file()

    try:
        data = yaml.safe_load(read_user_text(config_path))
    except FileNotFoundError:
        raise ConfigFileNotFoundError(
            f"Config file not found: {config_path}. "
            "Create one from harvey.yaml.example or run 'harvey setup'."
        )
    except yaml.YAMLError as e:
        raise ConfigError(f"Invalid YAML in {config_path}:\n  {e}")
    except OSError as e:
        raise ConfigError(f"Could not read {config_path}: {e}")

    if data is None:
        raise ConfigError(f"{config_path} is empty. Run 'harvey setup' to configure Harvey.")
    if not isinstance(data, dict):
        raise ConfigError(
            f"{config_path} must contain a YAML mapping (key: value pairs), "
            f"got {type(data).__name__}."
        )

    try:
        config = HarveyConfig(**data)
        config._source_path = str(config_path)
        return config
    except ValidationError as e:
        # Log a friendly, actionable summary, then re-raise the original
        # ValidationError so callers (and tests) keep the pydantic type.
        logger.error(
            f"Invalid configuration in {config_path}:\n{_format_validation_error(e)}\n"
            "Fix the fields above or re-run 'harvey setup'."
        )
        raise


def load_brain_config(config_path: str | None = None) -> BrainConfig:
    """Read just the ``brain:`` section, without validating the rest.

    Training runs before a product is configured (the template harvey.yaml
    still has placeholder values), but it must still think with the
    provider the user chose. A missing file or section means the Claude
    default; an invalid section raises rather than silently switching
    providers.
    """
    if config_path is None:
        try:
            config_path = _find_config_file()
        except ConfigFileNotFoundError:
            return BrainConfig()
    try:
        data = yaml.safe_load(read_user_text(config_path))
    except FileNotFoundError:
        return BrainConfig()
    except (yaml.YAMLError, OSError) as e:
        raise ConfigError(f"Could not read the brain settings in {config_path}: {e}")
    section = data.get("brain") if isinstance(data, dict) else None
    if section is None:
        return BrainConfig()
    if not isinstance(section, dict):
        raise ConfigError(f"'brain' in {config_path} must be a mapping.")
    try:
        return BrainConfig(**section)
    except ValidationError as e:
        raise ConfigError(
            f"Invalid brain settings in {config_path}:\n{_format_validation_error(e)}"
        )


def load_env() -> EnvConfig:
    """Load environment variables from .env file."""
    load_dotenv()
    env = EnvConfig(
        instantly_api_key=os.getenv("INSTANTLY_API_KEY", "").strip(),
        dataforseo_login=os.getenv("DATAFORSEO_LOGIN", "").strip(),
        dataforseo_password=os.getenv("DATAFORSEO_PASSWORD", "").strip(),
        dataforseo_sandbox=os.getenv("DATAFORSEO_SANDBOX", "").strip(),
        linkedin_email=os.getenv("LINKEDIN_EMAIL", "").strip(),
        linkedin_password=os.getenv("LINKEDIN_PASSWORD", "").strip(),
        hunter_api_key=os.getenv("HUNTER_API_KEY", "").strip(),
        serper_api_key=os.getenv("SERPER_API_KEY", "").strip(),
        scavio_api_key=os.getenv("SCAVIO_API_KEY", "").strip(),
        tinyfish_api_key=os.getenv("TINYFISH_API_KEY", "").strip(),
        reoon_api_key=os.getenv("REOON_API_KEY", "").strip(),
        zerobounce_api_key=os.getenv("ZEROBOUNCE_API_KEY", "").strip(),
        bounceless_api_key=os.getenv("BOUNCELESS_API_KEY", "").strip(),
        emailverify_api_key=os.getenv("EMAILVERIFY_API_KEY", "").strip(),
        getprospect_api_key=os.getenv("GETPROSPECT_API_KEY", "").strip(),
        abstract_api_key=os.getenv("ABSTRACT_API_KEY", "").strip(),
        mailboxvalidator_api_key=os.getenv("MAILBOXVALIDATOR_API_KEY", "").strip(),
        sniffmail_api_key=os.getenv("SNIFFMAIL_API_KEY", "").strip(),
        emailvalidationio_api_key=os.getenv("EMAILVALIDATIONIO_API_KEY", "").strip(),
        bouncezero_api_key=os.getenv("BOUNCEZERO_API_KEY", "").strip(),
        myemailverifier_api_key=os.getenv("MYEMAILVERIFIER_API_KEY", "").strip(),
        gmail_client_id=os.getenv("GMAIL_CLIENT_ID", "").strip(),
        gmail_client_secret=os.getenv("GMAIL_CLIENT_SECRET", "").strip(),
        smtp_host=os.getenv("SMTP_HOST", "").strip(),
        smtp_port=int(os.getenv("SMTP_PORT", "587").strip() or 587),
        smtp_username=os.getenv("SMTP_USERNAME", "").strip(),
        smtp_password=os.getenv("SMTP_PASSWORD", "").strip(),
        imap_host=os.getenv("IMAP_HOST", "").strip(),
        imap_port=int(os.getenv("IMAP_PORT", "993").strip() or 993),
        imap_username=os.getenv("IMAP_USERNAME", "").strip(),
        imap_password=os.getenv("IMAP_PASSWORD", "").strip(),
        m365_client_id=os.getenv("M365_CLIENT_ID", "").strip(),
        m365_tenant_id=os.getenv("M365_TENANT_ID", "").strip(),
        precleared_api_key=os.getenv("PRECLEARED_API_KEY", "").strip(),
        erp_supabase_url=os.getenv("ERP_SUPABASE_URL", "").strip(),
        erp_supabase_key=os.getenv("ERP_SUPABASE_KEY", "").strip(),
    )
    return env


# ── Approval or autopilot ─────────────────────────────────────────────────
#
# channels.email.require_approval: true (the default) means every email
# waits in the Outbox for you; false is autopilot. The dashboard can switch
# it, so it is edited in place: one line changes and the comments stay.

_APPROVAL_LINE_RE = re.compile(
    r"^(?P<lead>[ \t]*require_approval[ \t]*:[ \t]*)(?P<val>[^\s#]+)", re.MULTILINE)
_EMAIL_KEY_RE = re.compile(r"^(?P<ind>[ \t]+)email[ \t]*:[ \t]*(?:#.*)?$", re.MULTILINE)


def _email_section(data):
    channels = data.get("channels") if isinstance(data, dict) else None
    email = channels.get("email") if isinstance(channels, dict) else None
    return email if isinstance(email, dict) else None


def _with_approval_line(raw: str, word: str) -> list[str]:
    """Ways to say require_approval: <word> in ``raw``: change the line that
    is there, or add one under an ``email:`` key."""
    out = [raw[:m.start("val")] + word + raw[m.end("val"):] for m in _APPROVAL_LINE_RE.finditer(raw)]
    for m in _EMAIL_KEY_RE.finditer(raw):
        child = m.group("ind") + "  "
        for line in raw[m.end():].split("\n")[1:]:
            if not line.strip() or line.lstrip().startswith("#"):
                continue
            indent = line[:len(line) - len(line.lstrip(" \t"))]
            if len(indent) > len(m.group("ind")):
                child = indent
            break
        out.append(raw[:m.end()] + f"\n{child}require_approval: {word}" + raw[m.end():])
    return out


def set_require_approval(value: bool, path=None) -> Path:
    """Write channels.email.require_approval into the config file Harvey
    reads (harvey.local.yaml wins, as in load_config). Only that value
    changes: the result must parse to the same settings apart from it, or
    nothing is written and ConfigError says to set it by hand."""
    import copy
    import shutil
    path = Path(path or _find_config_file())
    target = path.resolve()          # a symlinked config stays a symlink
    data_bytes = target.read_bytes()
    bom = data_bytes.startswith(b"\xef\xbb\xbf")
    try:
        raw = data_bytes.decode("utf-8-sig").replace("\r\n", "\n")
    except UnicodeDecodeError:
        raise ConfigError(f"{path.name} isn't UTF-8, so it isn't rewritten. Set "
                          f"channels.email.require_approval there by hand.")
    try:
        data = yaml.safe_load(raw)
    except yaml.YAMLError as e:
        raise ConfigError(f"Invalid YAML in {path.name}: {e}")
    if _email_section(data) is None:
        raise ConfigError(f"{path.name} has no channels.email section. Set "
                          f"channels.email.require_approval there by hand.")
    expected = copy.deepcopy(data)
    _email_section(expected)["require_approval"] = bool(value)
    word = "true" if value else "false"
    for new in _with_approval_line(raw, word):
        try:
            if yaml.safe_load(new) != expected:
                continue
        except yaml.YAMLError:
            continue
        HarveyConfig(**expected)          # still a valid config
        if b"\r\n" in data_bytes:
            new = new.replace("\n", "\r\n")
        tmp = target.with_name(target.name + ".tmp")
        try:
            tmp.write_bytes((b"\xef\xbb\xbf" if bom else b"") + new.encode("utf-8"))
            shutil.copymode(target, tmp)
            os.replace(tmp, target)
        finally:
            tmp.unlink(missing_ok=True)
        _mode_cache.pop(str(target), None)
        return path
    raise ConfigError(f"Couldn't change require_approval in {path.name} safely. "
                      f"Set channels.email.require_approval: {word} there by hand.")


_mode_cache: dict[str, tuple[tuple, object]] = {}


def refresh_sending_mode(config, path=None) -> bool:
    """Bring a running config's require_approval in line with the file it was
    loaded from, so a switch made while Harvey runs counts at once: the
    sender, writer and handler call this before they queue or send. Cheap:
    the file is only read again when it changed. True when the value
    changed. A config not loaded from a file, or a file that can't be read,
    leaves the running value alone."""
    path = path or getattr(config, "_source_path", None)
    if not path:
        return False
    try:
        target = Path(path).resolve()
        st = target.stat()
        # Not the mtime alone: a restore or `cp -p` keeps it while the file changes.
        stamp = (st.st_mtime_ns, st.st_size, st.st_ino, st.st_ctime_ns)
        cached = _mode_cache.get(str(target))
        if cached and cached[0] == stamp:
            value = cached[1]
        else:
            email = _email_section(yaml.safe_load(read_user_text(target)))
            value = email.get("require_approval", True) if email is not None else None
            try:
                # Read the way load_config reads it: "true", 'no', on...
                value = TypeAdapter(bool).validate_python(value)
            except ValidationError:
                value = None
            _mode_cache[str(target)] = (stamp, value)
    except Exception:
        return False
    if not isinstance(value, bool) or value == config.channels.email.require_approval:
        return False
    config.channels.email.require_approval = value
    return True


_dnc_cache: dict[str, tuple[tuple, list[str] | None]] = {}


def live_do_not_contact(config) -> list[str]:
    """The do-not-contact list as the config file says it is now.

    Someone who sees an approved email to a company they must not contact
    adds it to the list while Harvey runs; waiting for a restart would let
    that email go. Re-read only when the file changed (as refresh_sending_mode
    does). A file that can't be read, or a list with a mistake in it, keeps
    the last good list: never an empty one.
    """
    compliance = compliance_of(config)
    path = getattr(config, "_source_path", None)
    if not path:
        return compliance.do_not_contact
    try:
        target = Path(path).resolve()
        st = target.stat()
        stamp = (st.st_mtime_ns, st.st_size, st.st_ino, st.st_ctime_ns)
        cached = _dnc_cache.get(str(target))
        if cached and cached[0] == stamp:
            value = cached[1]
        else:
            value = None
            try:
                value = do_not_contact_from_file(target)
            except Exception as e:
                logger.warning(f"compliance.do_not_contact in {target.name} couldn't be read "
                               f"({e}); still using the last good list.")
            _dnc_cache[str(target)] = (stamp, value)
    except OSError:
        return compliance.do_not_contact
    if value is None:
        return compliance.do_not_contact
    if value != compliance.do_not_contact and isinstance(
            getattr(config, "compliance", None), ComplianceConfig):
        config.compliance.do_not_contact = value
    return value


def do_not_contact_from_file(path=None) -> list[str]:
    """Just ``compliance.do_not_contact`` from a config file, validated.
    Raises on a file that can't be read or a list with a mistake in it."""
    data = yaml.safe_load(read_user_text(path or _find_config_file()))
    section = data.get("compliance") if isinstance(data, dict) else None
    raw = section.get("do_not_contact") if isinstance(section, dict) else None
    return ComplianceConfig(do_not_contact=raw).do_not_contact


def _find_config_file() -> str:
    """Search for Harvey's config.

    ``harvey.local.yaml`` wins when present. It is gitignored, so a fork can
    carry a real product configuration (trained on an actual company) while
    the tracked ``harvey.yaml`` stays an untrained template — nobody
    publishes their positioning, pricing, and prospect targeting by accident.
    """
    candidates = [
        Path.cwd() / "harvey.local.yaml",
        PROJECT_ROOT / "harvey.local.yaml",
        Path.cwd() / "harvey.yaml",
        Path.cwd().parent / "harvey.yaml",
        PROJECT_ROOT / "harvey.yaml",
    ]
    for path in candidates:
        if path.exists():
            return str(path)
    raise ConfigFileNotFoundError(
        "harvey.yaml not found in "
        + ", ".join(str(p.parent) for p in candidates)
        + ". Create one from harvey.yaml.example or run 'harvey setup'."
    )
