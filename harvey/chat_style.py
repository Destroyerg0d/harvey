"""How Harvey chats on WhatsApp and LinkedIn: like a person texting.

Owner, 2026-11-19: "you are not communicating well in whatsapp message and
linkedin ... try to talk more engaging ... send multiple message at the same
time since you are chatting to the person and also why taking name again and
again you are messaging them personally not on official mail".

- A draft is one to three short messages, separated by a blank line;
  ``parts`` splits it and each part goes out as its own message, a few seconds
  apart, in the same chat.
- Their first name appears at most once in a conversation: a message that
  opens with it ("Jane, ...", "Hi Jane, ...") after it was already used has the
  name taken off (``strip_repeat_name``).
"""

from __future__ import annotations

import re

MAX_PARTS = 3


def squash(text: str) -> str:
    """Text with every space removed, for telling whether a chat shows a
    message: WhatsApp and LinkedIn draw an address or a link as its own
    element, and reading the bubble back puts spaces around it
    ("you@shop.example :"), so the words match but the spacing doesn't (live,
    2026-11-18: every update naming an address failed its proof)."""
    return "".join((text or "").split())


def parts(body: str, max_parts: int = MAX_PARTS) -> list[str]:
    """The messages a draft is sent as: its blank-line-separated paragraphs,
    at most ``max_parts`` (any more join the last one)."""
    chunks = [c.strip() for c in re.split(r"\n\s*\n", body or "") if c.strip()]
    if len(chunks) > max_parts:
        chunks = chunks[:max_parts - 1] + ["\n".join(chunks[max_parts - 1:])]
    return chunks


def name_used(first_name: str, bodies) -> bool:
    """Whether ``first_name`` already appears in any of these sent messages."""
    name = (first_name or "").strip()
    if len(name) < 2:
        return False
    pattern = re.compile(rf"\b{re.escape(name)}\b", re.IGNORECASE)
    return any(pattern.search(b or "") for b in bodies)


def strip_repeat_name(body: str, first_name: str, earlier) -> str:
    """``body`` without their name at the start of any of its messages, when
    the name was already used in ``earlier`` (sent bodies) or in an earlier
    part of this one. "Hi Jane, thanks" -> "Thanks"."""
    name = (first_name or "").strip()
    if len(name) < 2:
        return body
    opener = re.compile(rf"^(?:(?:hi|hey|hello|thanks|thank you)\s+)?{re.escape(name)}\s*[,.!:;-]?\s+",
                        re.IGNORECASE)
    used = name_used(name, earlier)
    out = []
    for part in parts(body, max_parts=99):
        if used:
            m = opener.match(part)
            if m:
                lead = m.group(0).split()[0].lower()
                rest = part[m.end():]
                if lead in ("thanks", "thank"):
                    rest = ("Thank you, " if lead == "thank" else "Thanks, ") + rest
                part = rest[:1].upper() + rest[1:] if rest else part
        if name_used(name, [part]):
            used = True
        out.append(part)
    return "\n\n".join(out)
