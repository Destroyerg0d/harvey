"""Gmail / Google Workspace mailbox provider — raw REST, no SDK weight.

Setup (one-time):
1. Google Cloud Console → create a project → enable the Gmail API.
2. OAuth consent screen (internal or testing) → create an OAuth client of
   type "Desktop app". Put its id/secret in .env as GMAIL_CLIENT_ID /
   GMAIL_CLIENT_SECRET.
3. Run ``harvey gmail auth`` — opens a browser, stores the refresh token
   in data/gmail_token.json. Done: Harvey can send and read.

More than one mailbox (channels.email.mailboxes, see harvey/mailboxes.py):
each one signs in with ``harvey gmail auth --mailbox <address>``. The
primary (persona.email) keeps data/gmail_token.json; the others are stored
in data/gmail_tokens/<address>.json. A sign-in for any other account is
refused, and a provider built for a mailbox checks once which account its
token belongs to before it sends or reads anything.

Deliverability notes: use a mailbox on a dedicated SECONDARY domain (never
your main one), configure SPF/DKIM/DMARC, ramp volume slowly, and watch
Google Postmaster Tools. Harvey's outbox pacing + kill switches handle the
rest.
"""

import base64
import codecs
import json
import logging
import os
import subprocess
import tempfile
import time
import webbrowser
from email.message import EmailMessage, Message
from email.utils import parseaddr
from pathlib import Path

import httpx

from harvey.paths import PROJECT_ROOT
from harvey.integrations.mail_provider import (
    InboundMessage,
    MailProvider,
    SendResult,
    looks_like_bounce,
)

logger = logging.getLogger("harvey.gmail")

TOKEN_FILE = PROJECT_ROOT / "data" / "gmail_token.json"
# Tokens of the mailboxes besides the primary, one file per address.
TOKENS_DIR = PROJECT_ROOT / "data" / "gmail_tokens"
API_BASE = "https://gmail.googleapis.com/gmail/v1/users/me"
TOKEN_URL = "https://oauth2.googleapis.com/token"
REVOKE_URL = "https://oauth2.googleapis.com/revoke"
AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
# Consumer Gmail: no Workspace domain to pass Google as ``hd``.
_CONSUMER_DOMAINS = frozenset({"gmail.com", "googlemail.com"})
SCOPE = "https://www.googleapis.com/auth/gmail.modify"
LOOPBACK_PORT = 8765
MAX_INBOX_PAGES = 40
# A full 7-day read runs at least this often, whatever the history says.
FULL_READ_EVERY_SECONDS = 24 * 3600
# Labels that mean "not a reply to handle".
_NOT_REPLIES = frozenset({"SENT", "DRAFT", "SPAM", "TRASH"})
# Codecs Python accepts that are no real mail charset (see _extract_text).
_NOT_MAIL_CHARSETS = frozenset({"punycode", "idna", "utf_7", "unicode_escape", "raw_unicode_escape"})
MAX_DECODE_BYTES = 200_000
# _request's answer for a 404 when the caller asked to know.
_GONE = object()

class GmailError(RuntimeError):
    pass


def _entry_id(entry, page: int) -> str:
    msg = entry.get("message") if isinstance(entry, dict) else None
    msg_id = msg.get("id") if isinstance(msg, dict) else None
    if not isinstance(msg_id, str) or not msg_id:
        raise GmailError(f"history page {page}: a change has no message id")
    return msg_id


TIMEOUT = httpx.Timeout(connect=10.0, read=30.0, write=30.0, pool=10.0)


def _load_token(path=None) -> dict | None:
    """The stored sign-in at ``path`` (None: TOKEN_FILE, the primary's)."""
    path = Path(path) if path is not None else TOKEN_FILE
    try:
        if path.is_file():
            data = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data, dict) and data.get("refresh_token"):
                return data
    except (OSError, json.JSONDecodeError) as e:
        logger.warning(f"Could not read Gmail token file: {e}")
    return None


def _save_token(data: dict, path=None):
    """Store a sign-in at ``path`` (None: TOKEN_FILE). Written to a temporary
    file beside it and moved into place, so a crash mid-write never leaves
    half a token where a good one was."""
    path = Path(path) if path is not None else TOKEN_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    if _same_path(path.parent, TOKENS_DIR):
        _restrict_folder(path.parent)
    fd, tmp = tempfile.mkstemp(prefix=".gmail-token-", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(json.dumps(data, indent=2))
        try:
            os.chmod(tmp, 0o600)
        except OSError:
            pass
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _same_path(a: Path, b: Path) -> bool:
    try:
        return Path(a).resolve() == Path(b).resolve()
    except OSError:
        return False


def _restrict_folder(folder: Path):
    """Windows: only the signed-in user may open the token folder (chmod
    600 means nothing there). Best effort: a failure leaves the folder as it
    was, and is logged without anything from inside it."""
    if os.name != "nt":
        return
    user = os.environ.get("USERNAME", "")
    if not user:
        return
    domain = os.environ.get("USERDOMAIN", "")
    who = f"{domain}\\{user}" if domain else user
    try:
        done = subprocess.run(
            ["icacls", str(folder), "/inheritance:r", "/grant:r", f"{who}:(OI)(CI)F"],
            capture_output=True, timeout=20, check=False,
        )
    except (OSError, subprocess.SubprocessError) as e:
        logger.warning(f"Couldn't restrict {folder} to {who}: {type(e).__name__}")
        return
    if done.returncode != 0:
        logger.warning(f"Couldn't restrict {folder} to {who} (icacls exit {done.returncode})")


class GmailProvider(MailProvider):
    name = "gmail"

    def __init__(self, config, env, mailbox=None):
        """``mailbox`` (a harvey.mailboxes.Mailbox): send and read as that
        mailbox, with its token and From name, after checking the token is
        its account. None: persona.email's single mailbox, as before."""
        self.config = config
        self.mailbox = mailbox
        self.client_id = getattr(env, "gmail_client_id", "")
        self.client_secret = getattr(env, "gmail_client_secret", "")
        self._access_token = ""
        self._access_expires = 0.0
        # HTTP status of the last Gmail API answer (0: none, or no call made).
        self.last_status = 0
        # Why this mailbox must not be used ("" when it's fine): its token
        # belongs to another account. Decided once, by _check_identity.
        self.unavailable_reason = ""
        self._identity_checked = False
        # Messages already read, by Gmail id (None: not a reply we use).
        # A Gmail message's content never changes, so each cycle's complete
        # read only downloads what's new. Memory only.
        self._read_cache: dict[str, InboundMessage | None] = {}
        self._label_cache: dict[str, frozenset] = {}
        self._token_problem = ""
        # Where the last confirmed read ended (Gmail history id), when the
        # last full 7-day read was confirmed, and what the read in flight
        # reached (kept by confirm_read).
        self._history_id: str | None = None
        self._last_full_read = 0.0
        self._verify_week = False      # list the week once more after a full read
        self._pending: tuple | None = None

    def is_configured(self) -> bool:
        return bool(self.client_id and self.client_secret and self._stored_token())

    def _stored_token(self) -> dict | None:
        # Called with no argument for the single mailbox: tests replace
        # _load_token with a stand-in that takes none.
        if self.mailbox is None:
            return _load_token()
        return _load_token(self.mailbox.token_path)

    def _auth_command(self) -> str:
        if self.mailbox is None:
            return "harvey gmail auth"
        return f"harvey gmail auth --mailbox {self.mailbox.email}"

    def _expected_address(self) -> str:
        """The account this provider's token must belong to, or ""."""
        if self.mailbox is not None:
            return self.mailbox.email
        persona = getattr(self.config, "persona", None)
        return (getattr(persona, "email", "") or "").strip().lower()

    async def _check_identity(self) -> bool:
        """With a mailbox: is its token for that mailbox's account? Asked of
        Gmail once (/profile) and remembered; a wrong account is never used
        (unavailable_reason says why). False also while it can't be asked
        (no token, Gmail unreachable), which is tried again next time.
        Without a mailbox there's nothing to check, as before."""
        if self.mailbox is None:
            return True
        if self._identity_checked:
            return not self.unavailable_reason
        profile = await self._request("GET", "/profile")
        address = profile.get("emailAddress") if isinstance(profile, dict) else None
        if not isinstance(address, str) or not address.strip():
            return False
        self._identity_checked = True
        if address.strip().lower() != self.mailbox.email:
            self.unavailable_reason = (
                f"the Gmail sign-in for {self.mailbox.email} is for {address.strip()}. "
                f"Run '{self._auth_command()}' and sign in as {self.mailbox.email}."
            )
            logger.error("Gmail: " + self.unavailable_reason)
            return False
        return True

    def _identity_problem(self) -> str:
        return (self.unavailable_reason or self._token_problem
                or f"couldn't confirm which account the Gmail sign-in for "
                   f"{self.mailbox.email} belongs to; Harvey tries again next cycle.")

    async def _token(self) -> str | None:
        """Valid access token, refreshing via the stored refresh token."""
        if self._access_token and time.time() < self._access_expires - 60:
            return self._access_token
        stored = self._stored_token()
        if not stored:
            if self.mailbox is None:
                self._token_problem = "not signed in to Gmail. Run 'harvey gmail auth'."
            else:
                self._token_problem = (f"{self.mailbox.email} is not signed in to Gmail. "
                                       f"Run '{self._auth_command()}'.")
            logger.error("Gmail: " + self._token_problem)
            return None
        # Why there's no token, for whoever asked (get_replies says it).
        unreachable = ("couldn't reach Google to refresh the Gmail sign-in{}; "
                       "Harvey tries again next cycle.")
        try:
            async with httpx.AsyncClient(timeout=TIMEOUT) as client:
                resp = await client.post(TOKEN_URL, data={
                    "client_id": self.client_id,
                    "client_secret": self.client_secret,
                    "refresh_token": stored["refresh_token"],
                    "grant_type": "refresh_token",
                })
        except httpx.HTTPError as e:
            logger.error(f"Gmail token refresh failed: {e}")
            self._token_problem = unreachable.format(f" ({type(e).__name__})")
            return None
        if resp.status_code in (400, 401, 403):
            whose = f" for {self.mailbox.email}" if self.mailbox is not None else ""
            self._token_problem = (f"Google rejected the saved Gmail sign-in{whose} "
                                   f"({resp.status_code}). Run '{self._auth_command()}'.")
            logger.error(f"Gmail token refresh rejected{whose} ({resp.status_code}). "
                         f"Re-run '{self._auth_command()}'.")
            return None
        if resp.status_code != 200:
            self._token_problem = unreachable.format(f" (HTTP {resp.status_code})")
            logger.error(f"Gmail token refresh failed ({resp.status_code}).")
            return None
        # A garbled answer is no token, not an exception: one raised here
        # would escape a send and be recorded as "maybe sent".
        try:
            data = resp.json()
        except ValueError:
            data = None
        token = data.get("access_token") if isinstance(data, dict) else None
        if not isinstance(token, str) or not token:
            self._token_problem = unreachable.format(" (no token in Google's answer)")
            logger.error("Gmail token refresh returned no access token.")
            return None
        self._token_problem = ""
        try:
            expires_in = float(data.get("expires_in") or 3600)
        except (TypeError, ValueError):
            expires_in = 3600.0
        self._access_token = token
        self._access_expires = time.time() + expires_in
        return self._access_token or None

    async def _request(
        self, method: str, path: str, *, sending: bool = False, not_found=None, **kwargs
    ):
        # last_status stays 0 unless Gmail answers: no token means no call.
        self.last_status = 0
        token = await self._token()
        if not token:
            return None
        headers = {"Authorization": f"Bearer {token}"}
        try:
            async with httpx.AsyncClient(timeout=TIMEOUT) as client:
                resp = await client.request(
                    method, f"{API_BASE}{path}", headers=headers, **kwargs
                )
        except httpx.HTTPError as e:
            logger.error(f"Gmail request failed ({path}): {e}")
            if sending:
                # The request may have reached Gmail before the connection failed.
                raise RuntimeError("Gmail send outcome is unknown") from e
            return None
        self.last_status = resp.status_code
        if sending and not (200 <= resp.status_code < 300
                            or 400 <= resp.status_code < 500):
            raise RuntimeError("Gmail send outcome is unknown")
        if resp.status_code == 404 and not_found is not None:
            return not_found
        if resp.status_code >= 400:
            logger.error(f"Gmail API {resp.status_code} on {path}: {resp.text[:200]}")
            return None
        try:
            data = resp.json()
            if sending and (not isinstance(data, dict)
                            or not isinstance(data.get("id"), str)
                            or not data["id"].strip()):
                raise ValueError("Missing Gmail send message ID")
            return data
        except ValueError as e:
            logger.error(f"Gmail returned malformed JSON ({path})")
            if sending:
                raise RuntimeError("Gmail send returned an unreadable response") from e
            return None

    async def send_email(
        self,
        to_email: str,
        subject: str,
        body: str,
        thread_ref: str = "",
        in_reply_to: str = "",
    ) -> SendResult:
        if not await self._check_identity():
            # Nothing was sent: no send call is made for a mailbox whose
            # account isn't confirmed (attempted=False, so it may retry).
            reason = self._identity_problem()
            logger.error(f"Gmail: not sending from {self.mailbox.email}: {reason}")
            return SendResult(ok=False, error=reason[:300], status=self.last_status,
                              attempted=False)
        msg = EmailMessage()
        if self.mailbox is not None:
            from_name, from_address = self.mailbox.name, self.mailbox.email
        else:
            persona = self.config.persona
            from_name, from_address = persona.name, persona.email
        msg["To"] = to_email
        msg["From"] = f"{from_name} <{from_address}>" if from_address else from_name
        msg["Subject"] = subject
        if in_reply_to:
            msg["In-Reply-To"] = in_reply_to
            msg["References"] = in_reply_to
        msg.set_content(body)

        raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
        payload: dict = {"raw": raw}
        if thread_ref:
            payload["threadId"] = thread_ref

        data = await self._request("POST", "/messages/send", sending=True, json=payload)
        if data is None:
            # Gmail answered 4xx (status says which), or there was no token
            # to call it with (status 0: never attempted).
            return SendResult(ok=False, error="gmail send failed", status=self.last_status,
                              attempted=self.last_status != 0)
        sent_status = self.last_status

        # Fetch the assigned RFC Message-ID for bounce/thread matching later.
        message_id = ""
        gmail_id = data.get("id", "")
        if gmail_id:
            try:
                meta = await self._request(
                    "GET", f"/messages/{gmail_id}",
                    params={"format": "metadata", "metadataHeaders": "Message-ID"},
                )
            except Exception:
                # Sending already succeeded; optional metadata cannot undo it.
                logger.warning("Gmail sent message metadata could not be read")
                meta = None
            metadata = meta.get("payload") if isinstance(meta, dict) else None
            headers = metadata.get("headers") if isinstance(metadata, dict) else None
            for h in headers if isinstance(headers, list) else []:
                if (isinstance(h, dict) and isinstance(h.get("name"), str)
                        and h["name"].lower() == "message-id"
                        and isinstance(h.get("value"), str)):
                    message_id = h["value"]
        return SendResult(
            ok=True,
            message_id=message_id or gmail_id,
            thread_ref=(data.get("threadId", "")
                        if isinstance(data.get("threadId", ""), str) else ""),
            status=sent_status,
        )

    async def get_replies(self, limit: int = 500) -> list[InboundMessage]:
        """Every reply since the last confirmed read, or raise, so sends wait.

        The first read, and one a day after that, is the complete 7-day
        inbox (_read_week). In between, Gmail's history list says only what
        was added since the marker, so a big mailbox costs what a small one
        does. The marker moves on only when the handler confirms every reply
        was handled (confirm_read), so a reply that couldn't be handled
        comes back. A marker Gmail no longer has (404, usually after about a
        week) means the 7-day read again.
        """
        if not await self._token():
            raise GmailError(self._token_problem
                             or f"not signed in to Gmail. Run '{self._auth_command()}'.")
        if not await self._check_identity():
            raise GmailError(self._identity_problem())
        messages = await self._read_replies(limit)
        if self.mailbox is not None:
            for m in messages:
                m.mailbox = self.mailbox.email
        return messages

    async def _read_replies(self, limit: int) -> list[InboundMessage]:
        self._pending = None
        recent_full = (self._last_full_read
                       and time.time() - self._last_full_read < FULL_READ_EVERY_SECONDS)
        if self._history_id and recent_full:
            changed = await self._read_history(self._history_id)
            if changed is not None:
                messages, marker = changed
                verified = False
                if self._verify_week:
                    # The full read listed through Gmail search, which can lag
                    # new mail by seconds: a reply just under the marker could
                    # be in neither read. A cycle later the index has caught
                    # up, so the first history read lists the week once more.
                    have = {m.provider_id for m in messages}
                    messages = messages + [m for m in await self._read_week(limit, prune=False)
                                           if m.provider_id not in have]
                    verified = True
                self._pending = (marker, False, verified)
                return messages
            logger.info("Gmail: the history marker has expired; reading the last week in full.")
        # The marker comes first: anything arriving during the read is in
        # the next history list (and deduplicated if it's in both).
        marker = await self._current_history_id()
        messages = await self._read_week(limit)
        self._pending = (marker, True, False)
        return messages

    def confirm_read(self):
        """The handler handled everything the last get_replies returned:
        the next read starts after it."""
        if not self._pending:
            return
        marker, full, verified = self._pending
        self._pending = None
        self._history_id = marker
        if full:
            self._last_full_read = time.time()
            self._verify_week = True
        elif verified:
            self._verify_week = False

    async def _current_history_id(self) -> str | None:
        """Gmail's current history marker, or None (the next read is the
        whole week again)."""
        try:
            profile = await self._request("GET", "/profile")
        except Exception as e:
            logger.warning(f"Gmail: couldn't read the history marker: {e}")
            return None
        marker = profile.get("historyId") if isinstance(profile, dict) else None
        return str(marker) if isinstance(marker, (str, int)) and str(marker) else None

    async def _fetch(self, msg_id: str, where: str):
        """(message or None, its labels) for ``msg_id``, from the cache when
        it was read before; _GONE when Gmail no longer has it (deleted)."""
        if msg_id in self._read_cache:
            return self._read_cache[msg_id], self._label_cache.get(msg_id, frozenset())
        data = await self._request(
            "GET", f"/messages/{msg_id}", params={"format": "full"}, not_found=_GONE,
        )
        if data is _GONE:
            return _GONE
        if not isinstance(data, dict) or data.get("id") != msg_id:
            raise GmailError(f"{where}: could not read message {msg_id}")
        msg = self._parse_message(data)
        labels = data.get("labelIds")
        self._read_cache[msg_id] = msg
        self._label_cache[msg_id] = frozenset(l for l in labels if isinstance(l, str)) \
            if isinstance(labels, list) else frozenset()
        return msg, self._label_cache[msg_id]

    async def _read_history(self, start: str):
        """(replies added to the inbox since ``start``, the new marker), or
        None when Gmail no longer has ``start``."""
        params = {"startHistoryId": start, "historyTypes": ["messageAdded", "labelAdded"],
                  "labelId": "INBOX", "maxResults": 500}
        wanted: list[tuple[str, bool]] = []      # (id, moved into the inbox)
        seen_tokens = set()
        marker = None
        for page in range(1, MAX_INBOX_PAGES + 1):
            data = await self._request("GET", "/history", params=dict(params), not_found=_GONE)
            if data is _GONE:
                return None
            if not isinstance(data, dict) or not isinstance(data.get("history", []), list):
                raise GmailError(f"history page {page}: could not read changes")
            if page == 1:
                # The first page's marker: anything recorded while paging is
                # read again next time (deduplicated) rather than skipped.
                # Without one there's no safe marker, so fail closed.
                marker = data.get("historyId") if isinstance(data, dict) else None
                if not isinstance(marker, (str, int)) or not str(marker):
                    raise GmailError("history page 1: no historyId")
            for record in data.get("history", []):
                if not isinstance(record, dict):
                    raise GmailError(f"history page {page}: a record is not an object")
                for entry in record.get("messagesAdded") or []:
                    wanted.append((_entry_id(entry, page), False))
                for entry in record.get("labelsAdded") or []:
                    if "INBOX" in ((entry.get("labelIds") if isinstance(entry, dict) else None) or []):
                        wanted.append((_entry_id(entry, page), True))
            token = data.get("nextPageToken")
            if token is None:
                break
            if not isinstance(token, str) or not token or token in seen_tokens:
                raise GmailError(f"history page {page}: invalid or repeated nextPageToken")
            seen_tokens.add(token)
            params["pageToken"] = token
        else:
            raise GmailError(f"history has more than {MAX_INBOX_PAGES} pages; "
                             "refusing a partial read")
        out, done = [], set()
        for msg_id, moved_in in wanted:
            if msg_id in done:
                continue
            done.add(msg_id)
            if moved_in:
                # Judge it on its labels now, not ones cached when it was,
                # say, in the trash.
                self._read_cache.pop(msg_id, None)
                self._label_cache.pop(msg_id, None)
            got = await self._fetch(msg_id, "history")
            if got is _GONE:
                continue
            msg, labels = got
            # Harvey's own mail and drafts aren't replies; a message added
            # outside the inbox counts once it's moved in (labelsAdded).
            if labels & _NOT_REPLIES or (not moved_in and "INBOX" not in labels):
                continue
            if msg:
                out.append(msg)
        return out, str(marker)

    async def _read_week(self, limit: int, prune: bool = True) -> list[InboundMessage]:
        """The complete 7-day inbox, or raise. ``limit`` is a page size,
        not a cap: a reply beyond the first page must still stop its
        follow-ups. Bounded like the Graph provider (MAX_INBOX_PAGES of
        Gmail's largest pages, which are ids only), never partial."""
        params = {"q": "in:inbox -from:me newer_than:7d",
                  "maxResults": max(1, min(500, int(limit)))}
        out = []
        seen_ids = set()
        seen_tokens = set()
        for page in range(1, MAX_INBOX_PAGES + 1):
            listing = await self._request("GET", "/messages", params=dict(params))
            if not isinstance(listing, dict):
                raise GmailError(f"inbox page {page}: could not read messages")
            # Gmail omits 'messages' when a successful query has no matches.
            messages = listing.get("messages", [])
            if not isinstance(messages, list):
                raise GmailError(f"inbox page {page}: messages is not a list")
            for stub in messages:
                msg_id = stub.get("id") if isinstance(stub, dict) else None
                if not isinstance(msg_id, str) or not msg_id:
                    raise GmailError(f"inbox page {page}: message has no id")
                if msg_id in seen_ids:
                    continue
                seen_ids.add(msg_id)
                got = await self._fetch(msg_id, f"inbox page {page}")
                if got is _GONE:          # deleted since the listing
                    continue
                if got[0]:
                    out.append(got[0])
            token = listing.get("nextPageToken")
            if token is None:
                # A complete read: forget what has left the week's window
                # (not on a recheck, which shouldn't drop what history fetched).
                if prune:
                    self._read_cache = {k: v for k, v in self._read_cache.items() if k in seen_ids}
                    self._label_cache = {k: v for k, v in self._label_cache.items() if k in seen_ids}
                return out
            if not isinstance(token, str) or not token or token in seen_tokens:
                raise GmailError(f"inbox page {page}: invalid or repeated nextPageToken")
            seen_tokens.add(token)
            params["pageToken"] = token
        raise GmailError(
            f"inbox has more than {MAX_INBOX_PAGES} pages; refusing a partial read"
        )

    @staticmethod
    def _parse_message(data: dict) -> InboundMessage | None:
        payload = data.get("payload") or {}
        headers = {
            h.get("name", "").lower(): h.get("value", "")
            for h in payload.get("headers", [])
        }
        from_email = parseaddr(headers.get("from", ""))[1].lower()
        if not from_email:
            return None
        subject = headers.get("subject", "")
        body = GmailProvider._extract_text(payload) or data.get("snippet", "")
        return InboundMessage(
            provider_id=data.get("id", ""),
            from_email=from_email,
            subject=subject,
            body=body[:5000],
            message_id=headers.get("message-id", ""),
            in_reply_to=headers.get("in-reply-to", ""),
            thread_ref=data.get("threadId", ""),
            date=headers.get("date", ""),
            is_bounce=looks_like_bounce(from_email, subject),
            headers=headers,
        )

    @staticmethod
    def _extract_text(payload: dict) -> str:
        """Depth-first hunt for the text/plain part of a MIME tree."""
        def decode(part) -> str:
            data = (part.get("body") or {}).get("data", "")
            if not data:
                return ""
            try:
                raw = base64.urlsafe_b64decode(data + "===")
            except Exception:
                return ""
            # Finding the charset must never cost the text: odd headers mean
            # UTF-8, as before, not an empty body (an unsubscribe in it would
            # be lost).
            charset = "utf-8"
            try:
                headers = part.get("headers")
                for header in headers if isinstance(headers, list) else []:
                    if (isinstance(header, dict) and isinstance(header.get("value"), str)
                            and str(header.get("name") or "").lower() == "content-type"):
                        mime = Message()
                        mime["Content-Type"] = header["value"]
                        charset = mime.get_content_charset() or "utf-8"
                        break
            except Exception:
                charset = "utf-8"
            try:
                # A sender picks the label. Codecs that aren't mail charsets
                # are refused: punycode is quadratic (megabytes stall the
                # heartbeat) and the escape codecs let them shape the text.
                if codecs.lookup(charset).name.replace("-", "_") in _NOT_MAIL_CHARSETS:
                    charset = "utf-8"
            except (LookupError, ValueError, TypeError):   # unknown, or a null in the name
                charset = "utf-8"
            # Harvey keeps 5,000 characters of a body; decode no further than
            # that could need.
            raw = raw[:MAX_DECODE_BYTES]
            try:
                return raw.decode(charset, errors="replace")
            except (LookupError, ValueError):
                # Unknown or invalid charsets keep the existing UTF-8 fallback.
                return raw.decode("utf-8", errors="replace")

        if payload.get("mimeType", "").startswith("text/plain"):
            return decode(payload)
        for part in payload.get("parts", []) or []:
            text = GmailProvider._extract_text(part)
            if text:
                return text
        if payload.get("mimeType", "").startswith("text/html"):
            import re
            html = decode(payload)
            return re.sub(r"<[^>]+>", " ", html)
        return ""

    async def test_connection(self) -> tuple[bool, str]:
        if not self.client_id or not self.client_secret:
            return False, "GMAIL_CLIENT_ID / GMAIL_CLIENT_SECRET missing from .env"
        if not self._stored_token():
            return False, f"Not authorized yet — run '{self._auth_command()}'"
        data = await self._request("GET", "/profile")
        if data and data.get("emailAddress"):
            actual = str(data["emailAddress"]).strip()
            expected = self._expected_address()
            if expected and actual.lower() != expected:
                return False, (f"Signed in as {actual}, but this mailbox is {expected}. "
                               f"Run harvey gmail auth --mailbox {expected} again.")
            return True, f"Connected as {data['emailAddress']}"
        return False, "Token refresh or profile fetch failed"


def _auth_url(client_id: str, redirect_uri: str, state: str, expected_email: str = "") -> str:
    """Google's consent page. With an expected address Google is told who
    should sign in (login_hint, and hd for a Workspace domain), and the
    account chooser always shows, so a browser signed in elsewhere doesn't
    pick the wrong account silently."""
    import urllib.parse
    params = {
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": SCOPE,
        "access_type": "offline",
        "prompt": "consent select_account",
        "state": state,
    }
    expected = (expected_email or "").strip().lower()
    if expected:
        params["login_hint"] = expected
        domain = expected.rpartition("@")[2]
        if domain and domain not in _CONSUMER_DOMAINS:
            params["hd"] = domain
    return AUTH_URL + "?" + urllib.parse.urlencode(params)


def _finish_auth(client_id: str, client_secret: str, code: str, redirect_uri: str,
                 token_path=None, expected_email: str = "") -> bool:
    """Exchange the browser's code for tokens, check whose they are, and
    store them at ``token_path`` (None: TOKEN_FILE). With an expected
    address, a sign-in for any other account, or one whose account Gmail
    won't say, is revoked and nothing is saved."""
    path = Path(token_path) if token_path is not None else TOKEN_FILE
    expected = (expected_email or "").strip().lower()
    with httpx.Client(timeout=30) as client:
        resp = client.post(TOKEN_URL, data={
            "client_id": client_id,
            "client_secret": client_secret,
            "code": code,
            "grant_type": "authorization_code",
            "redirect_uri": redirect_uri,
        })
        if resp.status_code != 200:
            print(f"  Token exchange failed ({resp.status_code}): {resp.text[:200]}")
            return False
        try:
            data = resp.json()
        except ValueError:
            data = None
        if not isinstance(data, dict):
            print("  Google's answer to the token exchange couldn't be read. Try again.")
            return False
        if not data.get("refresh_token"):
            print("  Google returned no refresh token. Remove Harvey from your"
                  " account's third-party access list and retry.")
            return False

        signed_in = ""
        access = data.get("access_token")
        if isinstance(access, str) and access:
            try:
                profile = client.get(f"{API_BASE}/profile",
                                     headers={"Authorization": f"Bearer {access}"})
                answer = profile.json() if profile.status_code == 200 else None
            except (httpx.HTTPError, ValueError):
                answer = None
            address = answer.get("emailAddress") if isinstance(answer, dict) else None
            signed_in = address.strip() if isinstance(address, str) else ""

        if expected and signed_in.lower() != expected:
            # Not kept, so not left valid either.
            try:
                client.post(REVOKE_URL, data={"token": data["refresh_token"]})
            except httpx.HTTPError:
                logger.warning("Gmail: couldn't revoke a refused sign-in")
            if signed_in:
                print(f"  ✗ Signed in as {signed_in}, but this mailbox is {expected}.")
            else:
                print(f"  ✗ Couldn't confirm that this sign-in is {expected}.")
            print(f"  Nothing was saved and the sign-in was withdrawn. Run 'harvey gmail auth"
                  f" --mailbox {expected}' again and choose {expected}.")
            return False

    _save_token({
        "refresh_token": data["refresh_token"],
        "scope": data.get("scope", SCOPE),
        "obtained_at": int(time.time()),
    }, path)
    who = f" as {signed_in}" if signed_in else ""
    print(f"  ✓ Gmail authorized{who}. Token stored at {path}")
    return True


def run_auth_flow(client_id: str, client_secret: str, token_path=None,
                  expected_email: str = "") -> bool:
    """One-time OAuth (installed-app loopback). Blocks until the browser
    round-trip completes; stores the refresh token for GmailProvider at
    ``token_path`` (None: TOKEN_FILE). With ``expected_email``, only a
    sign-in as that account is kept (_finish_auth)."""
    import http.server
    import secrets
    import threading
    import urllib.parse

    if not client_id or not client_secret:
        print("  Set GMAIL_CLIENT_ID and GMAIL_CLIENT_SECRET in .env first.")
        print("  (Google Cloud Console → APIs → Credentials → OAuth client, Desktop app)")
        return False

    state = secrets.token_urlsafe(16)
    redirect_uri = f"http://localhost:{LOOPBACK_PORT}"
    auth_url = _auth_url(client_id, redirect_uri, state, expected_email)

    result: dict = {}
    done = threading.Event()

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            if query.get("state", [""])[0] != state:
                self.send_response(400); self.end_headers()
                return
            result["code"] = query.get("code", [""])[0]
            result["error"] = query.get("error", [""])[0]
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            # Whether the sign-in is kept is decided after this (the account
            # check), so the page doesn't claim it worked.
            self.wfile.write(
                b"<h2>Harvey got the Google sign-in.</h2>"
                b"You can close this tab. The terminal says whether it was accepted.")
            done.set()

        def log_message(self, *args):
            pass

    server = http.server.HTTPServer(("localhost", LOOPBACK_PORT), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    if expected_email:
        print(f"\n  Sign in as {expected_email.strip().lower()}.")
    print(f"\n  Opening browser for Google sign-in...\n  {auth_url}\n")
    webbrowser.open(auth_url)
    if not done.wait(timeout=300):
        server.shutdown()
        server.server_close()
        print("  Timed out waiting for the browser round-trip.")
        return False
    server.shutdown()
    server.server_close()

    if result.get("error") or not result.get("code"):
        print(f"  Authorization failed: {result.get('error') or 'no code returned'}")
        return False

    return _finish_auth(client_id, client_secret, result["code"], redirect_uri,
                        token_path=token_path, expected_email=expected_email)
