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
