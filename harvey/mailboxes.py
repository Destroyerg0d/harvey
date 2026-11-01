"""The mailboxes Harvey sends from.

With ``channels.email.mailboxes`` empty (the default), Harvey sends from one
mailbox, persona.email, exactly as before: ``configured_mailboxes`` is that
one, its Gmail token is data/gmail_token.json and its provider is whatever
``get_mail_provider`` builds.

With mailboxes configured (Gmail only), each one sends and reads replies on
its own token. persona.email is the primary: it keeps data/gmail_token.json,
and a prospect, outbox row or conversation whose mailbox is "" (anything
from before, or never assigned) belongs to it. The others' tokens are
data/gmail_tokens/<address>.json.

Per-mailbox state lives in the settings table under the keys below, one per
address (``paused_key(address)`` and so on).
"""

import json
import re
from dataclasses import dataclass
from pathlib import Path

# Sending paused for one mailbox (the value says why); the global
# "sending_paused" still stops every mailbox.
PAUSED_KEY_PREFIX = "sending_paused:"
# That mailbox's bounces, for its own bounce-rate switch.
BOUNCE_KEY_PREFIX = "bounce_count:"
# When its inbox was last read completely.
READ_OK_KEY_PREFIX = "mailbox_read_ok:"
# Since when it has been unusable (no token, wrong account, refused).
DOWN_SINCE_KEY_PREFIX = "mailbox_down_since:"
# The mailboxes the running Harvey read from its config when it started
# (in_use_value), so `harvey status` can say when a restart is needed.
IN_USE_KEY = "mailboxes_in_use"


def _norm(value: str | None) -> str:
    return (value or "").strip().lower()


def paused_key(email: str) -> str:
    return PAUSED_KEY_PREFIX + _norm(email)


def bounce_key(email: str) -> str:
    return BOUNCE_KEY_PREFIX + _norm(email)


def read_ok_key(email: str) -> str:
    return READ_OK_KEY_PREFIX + _norm(email)


def down_since_key(email: str) -> str:
    return DOWN_SINCE_KEY_PREFIX + _norm(email)


@dataclass(frozen=True)
class Mailbox:
    email: str           # lower-case
    name: str            # From name and sign-off
    daily_limit: int     # sends a day, resolved (never 0 for "default")
    token_path: Path
    is_primary: bool

    @property
    def first_name(self) -> str:
        parts = self.name.split()
        return parts[0] if parts else ""

    @property
    def domain(self) -> str:
        return self.email.rpartition("@")[2]


def _email_config(config):
    return getattr(getattr(config, "channels", None), "email", None)


def _primary_email(config) -> str:
    return _norm(getattr(getattr(config, "persona", None), "email", ""))


def is_multi(config) -> bool:
    """Whether channels.email.mailboxes lists any mailboxes."""
    return bool(getattr(_email_config(config), "mailboxes", None))


def token_path_for(email: str, primary_email: str) -> Path:
    """Where ``email``'s Gmail token is stored: the primary keeps
    gmail.TOKEN_FILE (read now, so a test or a patched location counts);
    every other mailbox has its own file in gmail.TOKENS_DIR."""
    from harvey.integrations import gmail
    address = _norm(email)
    if address == _norm(primary_email):
        return gmail.TOKEN_FILE
    if not address or any(c in address for c in "/\\:"):
        raise ValueError(f"'{email}' can't name a token file")
    return gmail.TOKENS_DIR / f"{address}.json"


def configured_mailboxes(config) -> list[Mailbox]:
    """Every mailbox Harvey may send from, in config order. Without
    mailboxes configured, the one mailbox of persona.email."""
    email_config = _email_config(config)
    default_limit = getattr(email_config, "max_daily_sends", 0)
    primary = _primary_email(config)
    if is_multi(config):
        return [
            Mailbox(
                email=_norm(m.email),
                name=m.name,
                daily_limit=m.daily_limit or default_limit,
                token_path=token_path_for(m.email, primary),
                is_primary=_norm(m.email) == primary,
            )
            for m in email_config.mailboxes
        ]
    persona = getattr(config, "persona", None)
    return [Mailbox(
        email=primary,
        name=(getattr(persona, "name", "") or "").strip(),
        daily_limit=default_limit,
        token_path=token_path_for(primary, primary),
        is_primary=True,
    )]


def primary_mailbox(config) -> Mailbox:
    mailboxes = configured_mailboxes(config)
    return next((m for m in mailboxes if m.is_primary), mailboxes[0])


def in_use_value(config) -> str:
    """The mailboxes ``config`` sends from, as stored under IN_USE_KEY."""
    return ",".join(m.email for m in configured_mailboxes(config))


def mailbox_for(config, value: str | None) -> Mailbox | None:
    """The configured mailbox ``value`` names ("" or None: the primary),
    or None when it isn't one of them."""
    address = _norm(value)
    if not address:
        return primary_mailbox(config)
    return next((m for m in configured_mailboxes(config) if m.email == address), None)


def normalize(config, value: str | None) -> str:
    """A stored mailbox value as an address: "" is the primary's."""
    return _norm(value) or primary_mailbox(config).email


def is_connected(mailbox: Mailbox) -> bool:
    """Whether the mailbox has a stored sign-in (a refresh token). Reads
    the file only: no network, and which account it is isn't checked here
    (GmailProvider does that before using it)."""
    try:
        path = Path(mailbox.token_path)
        if not path.is_file():
            return False
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    return isinstance(data, dict) and bool(data.get("refresh_token"))


def providers_for(config, env) -> dict:
    """{address: MailProvider} for every configured mailbox. Without
    mailboxes configured, the primary's address and the provider
    get_mail_provider builds (none for Instantly: an empty dict)."""
    if not is_multi(config):
        from harvey.integrations import mail_provider
        provider = mail_provider.get_mail_provider(config, env)
        return {primary_mailbox(config).email: provider} if provider is not None else {}
    from harvey.integrations.gmail import GmailProvider
    return {m.email: GmailProvider(config, env, mailbox=m) for m in configured_mailboxes(config)}


# The switch that stops every mailbox (the Sender's KILL_SWITCH_KEY).
GLOBAL_PAUSE_KEY = "sending_paused"

# With mailboxes configured, every email the Writer drafts signs off with
# this line, and the Sender fills it in from the mailbox the email goes out
# from, when it goes. A prospect moved to another mailbox signs with that one.
SENDER_FIRST_NAME_TAG = "{{sender_first_name}}"
_SENDER_TAG_RE = re.compile(r"\{\{\s*sender_first_name\s*\}\}", re.IGNORECASE)


def render_sender(text: str, mailbox: Mailbox | None) -> str:
    """``text`` with {{sender_first_name}} filled in from ``mailbox``
    (unchanged without one)."""
    if mailbox is None or not text:
        return text or ""
    return _SENDER_TAG_RE.sub(lambda _: mailbox.first_name, text)


def _token_stamp(path) -> tuple | None:
    try:
        stat = Path(path).stat()
    except OSError:
        return None
    return stat.st_mtime_ns, stat.st_size


class MailboxProviders:
    """Each configured mailbox's provider (what providers_for builds), made
    the first time it's needed and again whenever its token file changes:
    a mailbox signed in again (harvey gmail auth --mailbox) works without a
    restart, and its new sign-in gets a fresh account check.

    ``factory(mailbox)`` builds one; tests replace it."""

    def __init__(self, config, env, factory=None):
        self.config = config
        self.env = env
        self.factory = factory
        self._built: dict[str, tuple] = {}

    def get(self, mailbox: Mailbox):
        stamp = _token_stamp(mailbox.token_path)
        have = self._built.get(mailbox.email)
        if have is not None and have[1] == stamp:
            return have[0]
        if self.factory is not None:
            provider = self.factory(mailbox)
        else:
            from harvey.integrations.mail_provider import get_mail_provider
            provider = get_mail_provider(self.config, self.env, mailbox=mailbox)
        self._built[mailbox.email] = (provider, stamp)
        return provider


def connection_problem(mailbox: Mailbox, provider) -> str:
    """Why ``mailbox`` can't send or read at all ("" when it can): no
    sign-in, Gmail not set up, or a sign-in for another account. These are
    what count towards it being down (``down_since_key``)."""
    if not is_connected(mailbox):
        return (f"{mailbox.email} isn't signed in. Run "
                f"'harvey gmail auth --mailbox {mailbox.email}'.")
    if provider is None or not provider.is_configured():
        return f"{mailbox.email}: Gmail isn't set up (GMAIL_CLIENT_ID / GMAIL_CLIENT_SECRET)."
    return getattr(provider, "unavailable_reason", "") or ""


async def pause_problem(state, mailbox: Mailbox) -> str:
    """Why sending from ``mailbox`` is paused ("" when it isn't): all
    sending, or this mailbox alone (its own switch, or its bounce rate)."""
    if await state.get_setting(GLOBAL_PAUSE_KEY):
        return "all sending is paused"
    paused = await state.get_setting(paused_key(mailbox.email))
    return f"sending from {mailbox.email} is paused ({paused})" if paused else ""


async def takes_new_prospects(state, mailbox: Mailbox, provider) -> str:
    """Why ``mailbox`` can't be given new prospects now ("" when it can):
    as connection_problem and pause_problem, and not while it is down."""
    problem = connection_problem(mailbox, provider) or await pause_problem(state, mailbox)
    if problem:
        return problem
    down = await state.get_setting(down_since_key(mailbox.email))
    return f"{mailbox.email} has been failing since {down}" if down else ""


async def least_loaded(state, config, candidates: list[Mailbox], extra: dict | None = None) -> Mailbox | None:
    """The one of ``candidates`` with the least on its plate: cold first
    emails waiting to go, emails sent in the last 7 days, and new prospects
    given to it that nothing is written for yet (state.mailbox_loads), plus
    ``extra`` ({address: n}). Ties go to the earlier one in the config."""
    if not candidates:
        return None
    from datetime import datetime, timedelta, timezone
    primary = primary_mailbox(config).email
    week_ago = (datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=7)).isoformat()
    loads = await state.mailbox_loads(primary)
    extra = extra or {}

    async def load(m: Mailbox) -> int:
        sent = await state.count_outbox_sent_since(week_ago, mailbox=m.email, primary=primary)
        return loads.get(m.email, 0) + sent + extra.get(m.email, 0)

    scored = [(await load(m), i, m) for i, m in enumerate(candidates)]
    return min(scored, key=lambda x: (x[0], x[1]))[2]


async def companies_of(state, prospect_ids: list[str]) -> list[list[str]]:
    """``prospect_ids`` grouped by company (state.colleague_pairs), in the
    order they were given, so each company goes to one mailbox."""
    ids = list(dict.fromkeys(prospect_ids))
    parent = {pid: pid for pid in ids}

    def root(pid):
        while parent[pid] != pid:
            parent[pid] = parent[parent[pid]]
            pid = parent[pid]
        return pid

    for a, b in await state.colleague_pairs(ids):
        parent[root(a)] = root(b)
    groups: dict[str, list[str]] = {}
    for pid in ids:
        groups.setdefault(root(pid), []).append(pid)
    return list(groups.values())
