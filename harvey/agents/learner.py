"""Learner: once a day, proposes lessons from Harvey's own results.

Inputs, for the last WINDOW_DAYS days:

- the outcome table (harvey/outcomes.py): sequence emails sent, replies from
  people, positive replies, no's, opt-outs and bounces per segment, angle,
  step, mailbox and country;
- first emails' subject variants (a/b), per campaign;
- LinkedIn: invites with and without a note, first messages, follow-ups and
  replies, with what came back to each (accepted, replied, positive);
- the owner's edits to drafts (before and after) and their feedback notes;
- how many replies were automatic and how many came from people.

The data minimums come first (``comparable_groups``): a comparison needs
MIN_SENDS_PER_SIDE sends on each side and MIN_HUMAN_REPLIES replies from people
between them; a segment_mix or targeting lesson needs MIN_SEGMENT_SENDS sends in
each segment it names; a lesson from the owner's edits needs MIN_OWNER_EDITS of
them. Below every minimum it records "not enough data yet" and asks nothing.
Otherwise ONE Brain call (prompts/learner.md) asks for at most MAX_LESSONS
lessons, and each answer is checked: its kind, its text
(harvey/lessons.lesson_problem) and its evidence against the minimums, with
the numbers recomputed here rather than taken from the model. What passes is
proposed; what fails is logged and dropped. The owner decides on the Learning
tab or with ``harvey lessons``.

The heartbeat starts a run in the background through ``LearnerRunner``: once
per RUN_EVERY, on any cycle, never two at once (``learner:running``), and not
while the Brain is unavailable or over budget.
"""

import asyncio
import json
import logging
import math
import uuid
from datetime import datetime, timedelta, timezone

import aiosqlite

from harvey import lessons as store
from harvey.outcomes import build_outcomes
from harvey.segment_stamp import is_inbound
from harvey.state import AUTO_CONVERSATION, AUTOMATIC_INTENTS
from harvey.untrusted import UNTRUSTED_RULE, fence

logger = logging.getLogger("harvey.learner")

LAST_RUN_KEY = "learner:last_run"
RUNNING_KEY = "learner:running"
RUN_EVERY = timedelta(hours=24)
RUNNING_STALE = timedelta(minutes=30)

WINDOW_DAYS = 30
MIN_SENDS_PER_SIDE = 40
MIN_HUMAN_REPLIES = 3
MIN_SEGMENT_SENDS = 100
MIN_OWNER_EDITS = 3
MAX_LESSONS = 5
MINIMUMS = {
    "window_days": WINDOW_DAYS, "sends_per_side": MIN_SENDS_PER_SIDE,
    "human_replies": MIN_HUMAN_REPLIES, "segment_sends": MIN_SEGMENT_SENDS,
    "owner_edits": MIN_OWNER_EDITS, "max_lessons": MAX_LESSONS,
}
NOT_ENOUGH = "Not enough data yet"

EMAIL_DIMENSIONS = ("segment", "angle", "step", "mailbox", "country")
COUNT_FIELDS = ("sent", "human_replies", "positive", "not_interested", "opt_outs", "bounces")
# The groups each kind of lesson may rest on.
KIND_GROUPS = {
    "writing": ("email.",),
    "subject": ("subject.",),
    "linkedin": ("linkedin.",),
    "targeting": ("email.segment",),
    "segment_mix": ("email.segment",),
}
ACCEPTED_STAGES = ("accepted", "messaged", "replied", "warm", "meeting")
REPLIED_STAGES = ("replied", "warm", "meeting")
POSITIVE_STAGES = ("warm", "meeting")
# The reply-rate lift a subject test is sized to see (half as good again),
# at 95% confidence and 80% power.
SUBJECT_LIFT = 1.5
_Z = 1.96 + 0.84


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _parse(value) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt.astimezone(timezone.utc).replace(tzinfo=None) if dt.tzinfo else dt


# ── Cells: one kind of message and what came back ──

def _cell(group: str, value: str, **counts) -> dict:
    channel, dimension = group.split(".", 1)
    cell = {"key": f"{group}:{value}", "group": group, "channel": channel,
            "dimension": dimension, "value": value, **dict.fromkeys(COUNT_FIELDS, 0)}
    cell.update(counts)
    cell.setdefault("responses", cell["human_replies"])
    cell.setdefault("response_label", "replies from people")
    return cell


def email_cells(rows: list[dict]) -> dict[str, dict]:
    """The outcome table folded per dimension: every angle, every step..."""
    cells: dict[str, dict] = {}
    for row in rows or []:
        for dim in EMAIL_DIMENSIONS:
            value = str(row.get(dim) if row.get(dim) not in (None, "") else "none")
            key = f"email.{dim}:{value}"
            cell = cells.get(key) or _cell(f"email.{dim}", value)
            for field in COUNT_FIELDS:
                cell[field] += int(row.get(field) or 0)
            cell["responses"] = cell["human_replies"]
            cells[key] = cell
    return cells


def subject_cells(variants: list[dict]) -> dict[str, dict]:
    """Subject variants a and b across every campaign."""
    totals = {v: {"sent": 0, "human_replies": 0} for v in ("a", "b")}
    for campaign in variants or []:
        for v in ("a", "b"):
            totals[v]["sent"] += int((campaign.get(v) or {}).get("sent") or 0)
            totals[v]["human_replies"] += int((campaign.get(v) or {}).get("human_replies") or 0)
    return {f"subject.variant:{v}": _cell("subject.variant", v, **totals[v])
            for v in ("a", "b") if totals[v]["sent"]}


def linkedin_cells(li: dict) -> dict[str, dict]:
    return {c["key"]: c for c in (li or {}).get("cells", [])}


def comparable_groups(cells: dict[str, dict]) -> dict[str, dict]:
    """Per group: the cells with enough sends to compare, and whether the
    group can be compared at all (two such cells, enough responses between
    them), with why not."""
    groups: dict[str, list] = {}
    for cell in cells.values():
        groups.setdefault(cell["group"], []).append(cell)
    out = {}
    for group, members in sorted(groups.items()):
        big = [c for c in members if c["sent"] >= MIN_SENDS_PER_SIDE]
        responses = sum(c["responses"] for c in big)
        label = members[0]["response_label"]
        if len(big) < 2:
            why = (f"needs two kinds with {MIN_SENDS_PER_SIDE} sends each; "
                   f"{len(big)} {'has' if len(big) == 1 else 'have'} that")
        elif responses < MIN_HUMAN_REPLIES:
            why = f"needs {MIN_HUMAN_REPLIES} {label} between them; has {responses}"
        else:
            why = ""
        out[group] = {"group": group, "eligible": [c["key"] for c in big], "ok": not why,
                      "why": why, "most_sent": max((c["sent"] for c in members), default=0)}
    return out


def big_segments(cells: dict[str, dict]) -> list[dict]:
    return [c for c in cells.values()
            if c["group"] == "email.segment" and c["sent"] >= MIN_SEGMENT_SENDS]


def sends_needed(rate: float) -> int:
    """Sends per variant to tell a subject that does SUBJECT_LIFT times as
    well from chance at this reply rate (95% confidence, 80% power), rounded
    to two significant figures."""
    p1 = min(max(float(rate or 0), 0.002), 0.5)
    p2 = min(p1 * SUBJECT_LIFT, 0.99)
    n = (_Z ** 2) * (p1 * (1 - p1) + p2 * (1 - p2)) / ((p2 - p1) ** 2)
    digits = max(0, int(math.floor(math.log10(n))) - 1)
    return int(math.ceil(n / 10 ** digits) * 10 ** digits)


def variant_progress(a: dict, b: dict) -> dict:
    """An honest note on a subject test: how far it is from meaning anything."""
    sent_a, sent_b = int(a.get("sent") or 0), int(b.get("sent") or 0)
    replies = int(a.get("human_replies") or 0) + int(b.get("human_replies") or 0)
    sent = sent_a + sent_b
    assumed = not replies
    rate = replies / sent if replies and sent else 0.01
    needed = sends_needed(rate)
    more = max(0, needed - min(sent_a, sent_b))
    basis = ("assuming about one reply per hundred emails" if assumed
             else "at the reply rate so far")
    if more:
        note = (f"Needs about {more:,} more sends per variant ({needed:,} each, {basis}) "
                "before a difference between the subjects means anything.")
    else:
        note = f"Has the {needed:,} sends per variant a real difference needs ({basis})."
    return {"needed_per_variant": needed, "more_per_variant": more, "assumed_rate": assumed,
            "note": note}


# ── Reading the data ──

async def _rows(state, sql: str, params=()) -> list[dict]:
    async with aiosqlite.connect(state.db_path, timeout=30) as db:
        db.row_factory = aiosqlite.Row
        try:
            async with db.execute(sql, params) as cur:
                return [dict(r) for r in await cur.fetchall()]
        except aiosqlite.OperationalError as e:
            logger.debug(f"Learner: query skipped ({e})")
            return []


def _automatic(conversation: dict) -> bool:
    return (conversation.get("status") == AUTO_CONVERSATION
            or (conversation.get("intent") or "") in AUTOMATIC_INTENTS)


async def subject_variants(state, days: int = WINDOW_DAYS, now: datetime | None = None) -> list[dict]:
    """First emails sent with a subject variant, per campaign: sends and the
    people who replied after one (any later reply in that thread counts for
    the subject that opened it)."""
    now = now or _utcnow()
    since = (now - timedelta(days=days)).isoformat()
    sent = await _rows(state, (
        "SELECT o.campaign_id, COALESCE(c.name, '') AS campaign, o.variant, o.prospect_id, o.sent_at "
        "FROM outbox o LEFT JOIN campaigns c ON c.id = o.campaign_id "
        "WHERE o.kind = 'sequence' AND o.status = 'sent' AND o.step = 1 "
        "AND o.variant IN ('a', 'b') AND o.sent_at >= ?"), (since,))
    conversations = await _rows(state, (
        "SELECT prospect_id, status, intent, created_at FROM conversations "
        "WHERE COALESCE(channel, 'email') = 'email' AND datetime(created_at) >= datetime(?)"), (since,))
    human: dict[str, str] = {}
    for c in conversations:
        if _automatic(c) or not c.get("prospect_id"):
            continue
        at = str(c.get("created_at") or "").replace(" ", "T")
        if c["prospect_id"] not in human or at < human[c["prospect_id"]]:
            human[c["prospect_id"]] = at
    campaigns: dict[str, dict] = {}
    for r in sent:
        cid = r.get("campaign_id") or ""
        row = campaigns.setdefault(cid, {
            "campaign_id": cid, "campaign": r.get("campaign") or "",
            "a": {"sent": 0, "human_replies": 0}, "b": {"sent": 0, "human_replies": 0}})
        side = row[r["variant"]]
        side["sent"] += 1
        replied = human.get(r.get("prospect_id") or "")
        if replied and replied >= str(r.get("sent_at") or ""):
            side["human_replies"] += 1
    out = sorted(campaigns.values(), key=lambda c: -(c["a"]["sent"] + c["b"]["sent"]))
    for row in out:
        row["progress"] = variant_progress(row["a"], row["b"])
    return out


async def linkedin_outcomes(state, days: int = WINDOW_DAYS, now: datetime | None = None) -> dict:
    """LinkedIn's funnel, and per kind of message sent in the window what
    came back: invites (with a note or without) accepted, first messages,
    follow-ups and replies answered."""
    now = now or _utcnow()
    since = (now - timedelta(days=days)).isoformat()
    try:
        funnel = await state.li_funnel()
    except Exception:
        funnel = {}
    leads = {r["id"]: r for r in await _rows(
        state, "SELECT id, stage, accepted_at, last_inbound_at FROM linkedin_leads")}
    messages = await _rows(state, (
        "SELECT lead_id, direction, kind, body, COALESCE(sent_at, created_at) AS at "
        "FROM linkedin_messages WHERE status = 'sent' ORDER BY at"))
    inbound: dict[str, list[str]] = {}
    outbound: dict[str, list[dict]] = {}
    for m in messages:
        at = str(m.get("at") or "").replace(" ", "T")
        if m["direction"] == "in":
            inbound.setdefault(m["lead_id"], []).append(at)
        else:
            outbound.setdefault(m["lead_id"], []).append({**m, "at": at})

    counts = {key: {"sent": 0, "accepted": 0, "human_replies": 0, "positive": 0}
              for key in ("invite:note", "invite:no_note", "message:first",
                          "message:follow_up", "message:reply")}
    for lead_id, sent in outbound.items():
        lead = leads.get(lead_id) or {}
        stage = lead.get("stage") or ""
        positive = stage in POSITIVE_STAGES
        replies_in = sorted(inbound.get(lead_id, []))
        first_in = replies_in[0] if replies_in else ""
        # The message their first reply answers: the last one sent before it.
        credited = None
        if first_in:
            before = [m for m in sent if m["kind"] == "message" and m["at"] <= first_in]
            credited = before[-1] if before else None
        n_message = 0
        for m in sent:
            if m["kind"] == "message":
                key = "message:first" if n_message == 0 else "message:follow_up"
                n_message += 1
            elif m["kind"] == "invite_note":
                key = "invite:note" if (m.get("body") or "").strip() else "invite:no_note"
            else:
                key = "message:reply"
            if m["at"] < since:
                continue
            cell = counts[key]
            cell["sent"] += 1
            if key.startswith("invite"):
                if lead.get("accepted_at") or stage in ACCEPTED_STAGES:
                    cell["accepted"] += 1
                if replies_in or stage in REPLIED_STAGES:
                    cell["human_replies"] += 1
                cell["positive"] += positive
            elif key == "message:reply":
                if any(t > m["at"] for t in replies_in):
                    cell["human_replies"] += 1
                    cell["positive"] += positive
            elif m is credited:
                cell["human_replies"] += 1
                cell["positive"] += positive
    cells = []
    for key, c in counts.items():
        if not c["sent"]:
            continue
        dimension, value = key.split(":")
        invite = dimension == "invite"
        cells.append(_cell(f"linkedin.{dimension}", value, sent=c["sent"],
                           human_replies=c["human_replies"], positive=c["positive"],
                           accepted=c["accepted"],
                           responses=c["accepted"] if invite else c["human_replies"],
                           response_label="accepted" if invite else "replies"))
    return {"funnel": funnel, "cells": cells}


async def owner_edits(state, days: int = WINDOW_DAYS, now: datetime | None = None,
                      limit: int = 40) -> list[dict]:
    """Drafts the owner changed in the Outbox: what Harvey wrote, what they sent."""
    now = now or _utcnow()
    since = (now - timedelta(days=days)).isoformat()
    actions = await _rows(state, (
        "SELECT details_json, created_at FROM actions WHERE action_type = 'outbox_edited' "
        "AND datetime(created_at) >= datetime(?) ORDER BY created_at DESC LIMIT ?"), (since, int(limit)))
    out = []
    for a in actions:
        try:
            details = json.loads(a.get("details_json") or "{}")
        except (TypeError, ValueError):
            continue
        if not isinstance(details, dict) or not details.get("outbox_id"):
            continue
        rows = await _rows(state, "SELECT kind, step, subject, body FROM outbox WHERE id = ?",
                           (details["outbox_id"],))
        if not rows:
            continue
        old_s, old_b = str(details.get("old_subject") or ""), str(details.get("old_body") or "")
        new_s, new_b = str(rows[0].get("subject") or ""), str(rows[0].get("body") or "")
        if (old_s, old_b) == (new_s, new_b):
            continue
        out.append({"kind": rows[0].get("kind") or "", "step": rows[0].get("step") or 1,
                    "old_subject": old_s, "new_subject": new_s, "old_body": old_b,
                    "new_body": new_b, "at": a.get("created_at")})
    return out


async def owner_feedback(state, limit: int = 15) -> list[dict]:
    return await _rows(state, (
        "SELECT entity_type, comment, created_at FROM feedback WHERE TRIM(comment) != '' "
        "ORDER BY created_at DESC LIMIT ?"), (int(limit),))


async def reply_kinds(state, days: int = WINDOW_DAYS, now: datetime | None = None) -> dict:
    """Email replies in the window: from people, and automatic ones."""
    now = now or _utcnow()
    since = (now - timedelta(days=days)).isoformat()
    rows = await _rows(state, (
        "SELECT status, intent FROM conversations WHERE COALESCE(channel, 'email') = 'email' "
        "AND datetime(updated_at) >= datetime(?)"), (since,))
    automatic = sum(1 for r in rows if _automatic(r))
    return {"human": len(rows) - automatic, "automatic": automatic}


async def gather(state, now: datetime | None = None, days: int = WINDOW_DAYS) -> dict:
    """Everything a run reads, with its cells and minimums worked out."""
    now = now or _utcnow()
    # How cold email did: someone who asked us first would flatter any comparison.
    rows = [r for r in await build_outcomes(state, days=days, now=now)
            if not is_inbound(r.get("angle"))]
    variants = await subject_variants(state, days, now)
    li = await linkedin_outcomes(state, days, now)
    cells = {**email_cells(rows), **subject_cells(variants), **linkedin_cells(li)}
    return {
        "rows": rows, "variants": variants, "linkedin": li, "cells": cells,
        "groups": comparable_groups(cells), "segments": big_segments(cells),
        "edits": await owner_edits(state, days, now), "feedback": await owner_feedback(state),
        "replies": await reply_kinds(state, days, now),
        "segment_weights": await store.current_weights(state),
    }


def enough_data(data: dict) -> tuple[bool, str]:
    """Whether any lesson could pass its minimums, and if not, why not."""
    groups = data["groups"]
    if any(g["ok"] for g in groups.values()):
        return True, ""
    segments = data["segments"]
    if segments and sum(c["responses"] for c in segments) >= MIN_HUMAN_REPLIES:
        return True, ""
    if len(data["edits"]) >= MIN_OWNER_EDITS:
        return True, ""
    most = max((g["most_sent"] for g in groups.values()), default=0)
    return False, (
        f"{NOT_ENOUGH}: a comparison needs {MIN_SENDS_PER_SIDE} sends on each side and "
        f"{MIN_HUMAN_REPLIES} replies from people between them (the most-sent kind of message "
        f"has {most}), a segment needs {MIN_SEGMENT_SENDS} sends before its mix or targeting is "
        f"judged, and a lesson from your edits needs {MIN_OWNER_EDITS} of them "
        f"({len(data['edits'])} so far).")


# ── Checking what the model proposed ──

def _cited(evidence: dict, cells: dict[str, dict]) -> list[dict]:
    out = []
    for item in (evidence or {}).get("cells") or []:
        key = ""
        if isinstance(item, str):
            key = item.strip()
        elif isinstance(item, dict):
            key = str(item.get("key") or item.get("cell") or "").strip()
            if not key and item.get("dimension") is not None:
                want = (str(item.get("dimension")), str(item.get("value")))
                key = next((k for k, c in cells.items()
                            if (c["dimension"], str(c["value"])) == want), "")
        if key in cells and cells[key] not in out:
            out.append(cells[key])
    return out


def _cell_view(cell: dict) -> dict:
    return {k: cell[k] for k in ("key", "channel", "dimension", "value", "sent", "human_replies",
                                 "positive", "responses", "response_label")}


def check_evidence(kind: str, evidence, data: dict) -> tuple[dict | None, str]:
    """The evidence to store for a proposed lesson, recomputed from the data,
    or (None, why it falls short of the minimums)."""
    evidence = evidence if isinstance(evidence, dict) else {}
    if evidence.get("source") == "owner_edits":
        if kind not in ("writing", "subject"):
            return None, "only writing and subject lessons can rest on your edits"
        n = len(data["edits"])
        if n < MIN_OWNER_EDITS:
            return None, f"rests on {n} edits; needs {MIN_OWNER_EDITS}"
        return {"source": "owner_edits", "edits": n, "window_days": WINDOW_DAYS,
                **({"note": f"From {n} of your edits; no send results behind it."}
                   if kind == "subject" else {})}, ""
    cells = data["cells"]
    prefixes = KIND_GROUPS[kind]
    cited = [c for c in _cited(evidence, cells)
             if any(c["group"] == p or (p.endswith(".") and c["group"].startswith(p))
                    for p in prefixes)]
    if kind in ("targeting", "segment_mix"):
        small = [c for c in cited if c["sent"] < MIN_SEGMENT_SENDS]
        if small:
            return None, f"names a segment with fewer than {MIN_SEGMENT_SENDS} sends"
        if not cited or (kind == "segment_mix" and len(cited) < 2):
            return None, ("compares fewer than two segments with enough sends" if kind == "segment_mix"
                          else "names no segment with enough sends")
    else:
        by_group: dict[str, list] = {}
        for c in cited:
            if c["sent"] >= MIN_SENDS_PER_SIDE:
                by_group.setdefault(c["group"], []).append(c)
        pairs = [g for g in by_group.values() if len(g) >= 2]
        if not pairs:
            return None, (f"compares no two kinds of message with {MIN_SENDS_PER_SIDE} sends each")
        cited = max(pairs, key=lambda g: sum(c["responses"] for c in g))
    responses = sum(c["responses"] for c in cited)
    if responses < MIN_HUMAN_REPLIES:
        return None, f"rests on {responses} responses; needs {MIN_HUMAN_REPLIES}"
    out = {"source": "results", "window_days": WINDOW_DAYS,
           "cells": [_cell_view(c) for c in cited],
           "sent": sum(c["sent"] for c in cited),
           "human_replies": sum(c["human_replies"] for c in cited),
           "positive": sum(c["positive"] for c in cited)}
    if kind == "segment_mix":
        weights = store.valid_weights(evidence.get("weights"))
        if weights is None:
            return None, "has no usable segment weights"
        out["weights"] = weights
    if kind == "subject":
        sides = {c["value"]: c for c in cited}
        progress = variant_progress(sides.get("a", {}), sides.get("b", {}))
        out["note"] = ("A small sample: " + progress["note"])
        out["needed_per_variant"] = progress["needed_per_variant"]
    return out, ""


# ── The prompt ──

def _cell_line(c: dict) -> str:
    extra = f", accepted {c['accepted']}" if "accepted" in c and c["channel"] == "linkedin" else ""
    return (f"  {c['key']}: sent {c['sent']}, replies from people {c['human_replies']}, positive "
            f"{c['positive']}{extra}, not interested {c['not_interested']}, opt-outs "
            f"{c['opt_outs']}, bounces {c['bounces']}")


def build_prompt(data: dict, active: list[dict], closed: list[dict], header: str) -> str:
    parts = [header, "", UNTRUSTED_RULE, ""]
    ok = [g for g in data["groups"].values() if g["ok"]]
    parts.append("RESULTS YOU MAY COMPARE (every listed cell has enough sends; cite cells by key):")
    if ok:
        for g in ok:
            parts.append(f"- {g['group']}")
            parts += [_cell_line(data["cells"][k]) for k in g["eligible"]]
    else:
        parts.append("- none yet")
    waiting = [g for g in data["groups"].values() if not g["ok"]]
    if waiting:
        parts.append("NOT COMPARABLE YET (propose nothing about these):")
        parts += [f"- {g['group']}: {g['why']}" for g in waiting]
    parts.append(f"SEGMENTS WITH {MIN_SEGMENT_SENDS}+ SENDS (the only ones a targeting or "
                 "segment_mix lesson may name):")
    parts += ([_cell_line(c) for c in data["segments"]] or ["  none yet"])
    weights = data.get("segment_weights")
    parts.append("CURRENT SEGMENT MIX: " + (json.dumps(weights, sort_keys=True) if weights
                                            else "the config's default"))
    for v in data["variants"][:10]:
        progress = v.get("progress") or variant_progress(v["a"], v["b"])
        parts.append(f"SUBJECT VARIANTS in {v.get('campaign') or 'a campaign'}: a sent {v['a']['sent']}, "
                     f"replies {v['a']['human_replies']}; b sent {v['b']['sent']}, replies "
                     f"{v['b']['human_replies']}. {progress['note']}")
    funnel = data["linkedin"].get("funnel") or {}
    if funnel:
        parts.append("LINKEDIN FUNNEL: " + ", ".join(
            f"{k} {funnel[k]}" for k in ("invited", "accepted", "messaged", "replied", "positive",
                                         "meetings", "declined") if k in funnel))
    replies = data["replies"]
    parts.append(f"EMAIL REPLIES: {replies['human']} from people, {replies['automatic']} automatic "
                 "(out of office, helpdesk acknowledgements).")
    edits = data["edits"]
    if len(edits) >= MIN_OWNER_EDITS:
        shown = "\n\n".join(
            f"[{e['kind']} step {e['step']}]\nHarvey wrote: {e['old_subject'][:120]}\n"
            f"{e['old_body'][:500]}\nThe owner sent: {e['new_subject'][:120]}\n{e['new_body'][:500]}"
            for e in edits[:10])
        parts.append(f"THE OWNER'S EDITS ({len(edits)}; cite as \"source\": \"owner_edits\"):\n"
                     + fence(shown, "the owner's edits"))
    if data["feedback"]:
        notes = "\n".join(f"- {n.get('entity_type')}: {' '.join(str(n.get('comment')).split())[:300]}"
                          for n in data["feedback"])
        parts.append("THE OWNER'S NOTES (context only):\n" + fence(notes, "the owner's notes"))
    if active:
        parts.append("ACTIVE LESSONS (don't repeat them):\n" + "\n".join(
            f"- [{l['kind']}] {l['text']}" for l in active))
    if closed:
        parts.append("REJECTED OR RETIRED LESSONS (never propose them again):\n" + "\n".join(
            f"- [{l['kind']}] {l['text']}" for l in closed[:30]))
    return "\n".join(parts)


FALLBACK_HEADER = (
    "You are Harvey's learner. Propose at most five short lessons for future writing and "
    "targeting from the results below, each with kind, text and evidence (cell keys). No "
    "numbers, links, names, quotes or claims about the sender's company in the text. "
    'Return ONLY JSON: {"lessons": [...], "note": "..."}')


def _candidates(reply) -> tuple[list[dict], str]:
    if isinstance(reply, dict):
        items, note = reply.get("lessons"), str(reply.get("note") or "")
    else:
        items, note = reply, ""
    return [i for i in (items if isinstance(items, list) else []) if isinstance(i, dict)], note


# ── A run ──

async def last_run(state) -> dict | None:
    raw = await state.get_setting(LAST_RUN_KEY)
    if not raw:
        return None
    try:
        value = json.loads(raw)
    except (TypeError, ValueError):
        value = {"at": raw}
    return value if isinstance(value, dict) else None


class Learner:
    def __init__(self, brain, state, config=None, now=None, gather_fn=None):
        self.brain, self.state, self.config = brain, state, config
        self._now = now
        self.gather_fn = gather_fn or gather

    def now(self) -> datetime:
        value = self._now() if self._now else _utcnow()
        return value.astimezone(timezone.utc).replace(tzinfo=None) if value.tzinfo else value

    async def due(self) -> bool:
        run = await last_run(self.state)
        at = _parse((run or {}).get("at"))
        return at is None or self.now() - at >= RUN_EVERY

    async def _claim(self) -> bool:
        """Take the run lock; False while another run holds a fresh one."""
        now = self.now()
        async with aiosqlite.connect(self.state.db_path, timeout=30) as db:
            cur = await db.execute(
                "INSERT INTO settings (key, value, updated_at) VALUES (?, ?, CURRENT_TIMESTAMP) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value, "
                "updated_at = CURRENT_TIMESTAMP WHERE settings.value = '' OR settings.value < ?",
                (RUNNING_KEY, now.isoformat(timespec="seconds"),
                 (now - RUNNING_STALE).isoformat(timespec="seconds")))
            await db.commit()
            return cur.rowcount > 0

    async def _release(self):
        await self.state.set_setting(RUNNING_KEY, "")

    async def _unavailable(self) -> str:
        """Why the Brain can't be used now ("" when it can)."""
        paused = getattr(self.brain, "paused_reason", "") or ""
        if paused:
            return paused
        preflight = getattr(self.brain, "preflight", None)
        if preflight is not None:
            problem = await preflight()
            if problem:
                return problem
        usage = getattr(self.config, "usage", None)
        check = getattr(self.brain, "is_within_budget", None)
        if usage is not None and check is not None:
            pct = usage.max_daily_claude_percent
            if not await check(max(int(200 * (pct / 100)), 1), max_percent=pct):
                return "over the usage budget"
        return ""

    async def _record(self, report: dict) -> dict:
        report = {"at": self.now().isoformat(timespec="seconds"), **report}
        await self.state.set_setting(LAST_RUN_KEY, json.dumps(report, sort_keys=True))
        return report

    async def run(self, force: bool = False) -> dict:
        """One pass. Returns its report; a run that couldn't happen (not due,
        another one going, the Brain unavailable or over budget) is not
        recorded, so the next cycle tries again."""
        if not force and not await self.due():
            return {"status": "not_due", "note": "ran less than a day ago"}
        if not await self._claim():
            return {"status": "busy", "note": "another learner run is going"}
        try:
            problem = await self._unavailable()
            if problem:
                logger.info(f"Learner: skipped, the brain can't be used now ({problem}).")
                return {"status": "skipped", "note": f"the brain can't be used now: {problem}"}
            return await self._run()
        except Exception as e:
            logger.exception("Learner: run failed")
            return await self._record({"status": "failed", "note": f"the run failed: {e}",
                                       "proposed": 0})
        finally:
            await self._release()

    async def _run(self) -> dict:
        run_id = uuid.uuid4().hex[:12]
        data = await self.gather_fn(self.state, now=self.now())
        ok, why = enough_data(data)
        if not ok:
            logger.info(f"Learner: {why}")
            return await self._record({"status": "not_enough_data", "note": why, "proposed": 0,
                                       "run_id": run_id})
        lessons = await store.list_lessons(self.state)
        active = [l for l in lessons if l["status"] in ("active", "proposed")]
        closed = [l for l in lessons if l["status"] in ("rejected", "retired")]
        persona = getattr(self.config, "persona", None)
        product = getattr(self.config, "product", None)
        header = ""
        if hasattr(self.brain, "load_prompt"):
            header = self.brain.load_prompt(
                "learner", persona_company=getattr(persona, "company", "") or "the sender",
                product_name=getattr(product, "name", "") or "its product",
                window_days=WINDOW_DAYS, max_lessons=MAX_LESSONS) or ""
        prompt = build_prompt(data, active, closed, header or FALLBACK_HEADER)
        reply = await self.brain.think_json(prompt, session_id="harvey-learner",
                                            agent="learner", task="propose_lessons")
        if reply is None and (getattr(self.brain, "paused_reason", "")
                              or getattr(self.brain, "last_reply_empty", False)):
            return {"status": "skipped", "note": "the brain gave no answer; trying again next cycle"}
        if reply is None:
            return await self._record({"status": "failed", "proposed": 0, "run_id": run_id,
                                       "note": "the model's answer wasn't usable JSON"})
        candidates, model_note = _candidates(reply)
        context = await store.filter_context(self.state)
        proposed = refreshed = 0
        reasons: list[str] = []
        for item in candidates[:MAX_LESSONS]:
            kind = str(item.get("kind") or "").strip().lower()
            text = store.clean_text(item.get("text"))
            why = ""
            if kind not in store.KINDS:
                why = f"has an unknown kind ({kind or 'none'})"
            if not why:
                why = store.lesson_problem(text, context["names"], context["excerpts"])
                why = f"its text {why}" if why else ""
            evidence = None
            if not why:
                evidence, why = check_evidence(kind, item.get("evidence"), data)
            if why:
                logger.info(f"Learner: left out a {kind or 'lesson'} proposal: {why}. "
                            f"Text: {text[:160]!r}")
                reasons.append(why)
                continue
            _, created = await store.propose(self.state, kind, text, evidence,
                                             source_run=run_id, context=context)
            proposed += created
            refreshed += not created
        left_out = len(candidates) - proposed - refreshed
        if proposed:
            note = f"Proposed {proposed} lesson{'' if proposed == 1 else 's'} for you to review."
        else:
            note = "Nothing new to propose."
        if refreshed:
            note += f" {refreshed} matched a lesson already on file."
        if left_out:
            kinds = sorted(set(r.split(";")[0] for r in reasons))
            note += (f" Left out {left_out} that didn't pass the checks"
                     + (f" ({'; '.join(kinds[:3])})" if kinds else "") + ".")
        logger.info(f"Learner: {note}")
        return await self._record({
            "status": "proposed" if proposed else "nothing_new", "note": note,
            "model_note": store.clean_text(model_note)[:300] if model_note else "",
            "proposed": proposed, "refreshed": refreshed, "left_out": left_out,
            "run_id": run_id})


async def run_once(state, brain=None, config=None) -> dict:
    """One learner run now, whatever the timer says (``harvey lessons --run``)."""
    if config is None:
        from harvey.config import load_config
        config = load_config()
    if brain is None:
        from harvey.brain import Brain
        brain = Brain(state, config.brain)
    return await Learner(brain, state, config).run(force=True)


class LearnerRunner:
    """The heartbeat's daily hook: starts a learner run in the background
    when one is due, never two at once. Each call also refreshes the leak
    guard's view of the active lessons."""

    def __init__(self, brain, state, config, learner_factory=None):
        self.brain, self.state, self.config = brain, state, config
        self.learner_factory = learner_factory or (lambda: Learner(brain, state, config))
        self.task: asyncio.Task | None = None

    @property
    def running(self) -> bool:
        return self.task is not None and not self.task.done()

    async def maybe_start(self, start: bool = True) -> str:
        """Start a run if one is due (and ``start``: false in quiet hours).
        Returns why not, or "started"."""
        await store.refresh_active(self.state)
        if not start:
            return "not now"
        if self.running:
            return "a run is going"
        learner = self.learner_factory()
        if not await learner.due():
            return "not due"
        if getattr(self.brain, "paused_reason", ""):
            return "the brain is paused"
        logger.info("Learner: starting the daily run in the background.")
        self.task = asyncio.create_task(learner.run())
        return "started"

    async def shutdown(self):
        if self.running:
            self.task.cancel()
            try:
                await self.task
            except (asyncio.CancelledError, Exception):
                pass
