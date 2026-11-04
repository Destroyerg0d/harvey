"""Import a hand-kept LinkedIn tracker into Harvey's LinkedIn tables.

Two files, either optional:

* a pipeline JSON: a list of rows with name, first, brand, slug, profile,
  domain, connected ("Oct28"), stage, draft and sent (a date, or "");
* a sends workbook (.xlsx, or .csv) whose sheet "Verified Sends" has a
  header row with "Brand / Company" and the columns Name, Brand / Company,
  LinkedIn Profile, Sent at (IST) and Personalized Note: invites sent.

Re-importing is safe: leads are keyed by profile slug, stages only move
forward, and a message already recorded is not recorded twice.

    sends row                          -> invited, invite note recorded as sent
    accepted_no_reply                  -> accepted
    followed_up / warm / hot /
      warm_partner (with a sent date)  -> messaged, that message recorded as sent
    declined / soft_no                 -> declined (never contacted again)
    same_company_skip                  -> accepted, never messaged
"""

import csv
import json
import re
import zipfile
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from xml.etree import ElementTree

import pytz

from harvey.linkedin.pages import names_match, profile_url, slug_from_url
from harvey.linkedin.store import _iso

SENDS_SHEET = "Verified Sends"
TRACKER_ZONE = "Asia/Kolkata"   # "Sent at (IST)"
SAME_COMPANY_REASON = "the tracker marks a colleague as the one to message (same company)"

PIPELINE_STAGES = {
    "accepted_no_reply": "accepted",
    "followed_up": "accepted",
    "warm": "accepted",
    "hot": "accepted",
    "warm_partner": "accepted",
    "soft_no": "declined",
    "declined": "declined",
    "same_company_skip": "accepted",
}


@dataclass
class ImportReport:
    leads_created: int = 0
    leads_updated: int = 0
    invites: int = 0
    messages: int = 0
    drafts: int = 0
    declined: int = 0
    no_message: int = 0
    linked_companies: int = 0
    skipped: list[str] = field(default_factory=list)

    def lines(self) -> list[str]:
        out = [f"{self.leads_created} leads added, {self.leads_updated} already known",
               f"{self.invites} invites and {self.messages} sent messages recorded, "
               f"{self.drafts} unsent drafts waiting for approval",
               f"{self.declined} declined (never contacted again), "
               f"{self.no_message} never to be messaged (same company)",
               f"{self.linked_companies} linked to a Harvey company by domain"]
        if self.skipped:
            out.append(f"{len(self.skipped)} rows skipped: " + "; ".join(self.skipped[:5])
                       + (" ..." if len(self.skipped) > 5 else ""))
        return out


# ── Dates ──

_MONTHS = {m: i for i, m in enumerate(
    ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"), 1)}


def parse_short_date(raw: str, today: date | None = None) -> date | None:
    """'Oct28', 'Oct 28', '28 Oct' (the year is the latest that isn't in the
    future), or any full date parse_when understands."""
    raw = (raw or "").strip()
    if not raw:
        return None
    m = re.fullmatch(r"([A-Za-z]{3,9})\.?\s*(\d{1,2})", raw) or None
    day_first = re.fullmatch(r"(\d{1,2})\s*([A-Za-z]{3,9})\.?", raw)
    if m or day_first:
        month_name, day = (m.group(1), m.group(2)) if m else (day_first.group(2), day_first.group(1))
        month = _MONTHS.get(month_name[:3].lower())
        if not month:
            return None
        today = today or date.today()
        for year in (today.year, today.year - 1):
            try:
                d = date(year, month, int(day))
            except ValueError:
                return None
            if d <= today + timedelta(days=1):
                return d
        return None
    when = parse_when(raw)
    return when.date() if when else None


def parse_when(raw, zone: str = TRACKER_ZONE) -> datetime | None:
    """A tracker timestamp as naive UTC. Naive text is read in ``zone``
    (IST); an Excel serial number is a date too."""
    if raw is None or raw == "":
        return None
    if isinstance(raw, datetime):
        local = raw
    elif isinstance(raw, (int, float)):
        local = datetime(1899, 12, 30) + timedelta(days=float(raw))
    else:
        text = str(raw).strip()
        if re.fullmatch(r"\d+(\.\d+)?", text) and float(text) > 20000:
            local = datetime(1899, 12, 30) + timedelta(days=float(text))
        else:
            local = None
            text = re.sub(r"\s*(IST|UTC|GMT)$", "", text, flags=re.IGNORECASE)
            try:
                local = datetime.fromisoformat(text.replace("Z", "+00:00"))
            except ValueError:
                for fmt in ("%d/%m/%Y %H:%M", "%d/%m/%Y %H:%M:%S", "%d/%m/%Y", "%d-%m-%Y %H:%M",
                            "%d-%m-%Y", "%b %d, %Y %H:%M", "%b %d, %Y", "%d %b %Y %H:%M",
                            "%d %b %Y", "%B %d, %Y", "%d %B %Y", "%Y/%m/%d %H:%M", "%Y/%m/%d",
                            "%b %d %Y", "%d %b %Y, %I:%M %p", "%b %d, %Y, %I:%M %p"):
                    try:
                        local = datetime.strptime(text, fmt)
                        break
                    except ValueError:
                        continue
            if local is None:
                return None
    if local.tzinfo is None:
        local = pytz.timezone(zone).localize(local)
    return local.astimezone(pytz.utc).replace(tzinfo=None)


# ── Reading the workbook ──

_NS_STRIP = re.compile(r"^\{[^}]*\}")


def _tag(el) -> str:
    return _NS_STRIP.sub("", el.tag)


def _col_index(ref: str) -> int:
    letters = re.match(r"[A-Z]+", ref or "")
    if not letters:
        return -1
    n = 0
    for ch in letters.group(0):
        n = n * 26 + (ord(ch) - 64)
    return n - 1


def _text_of(el) -> str:
    return "".join(t.text or "" for t in el.iter() if _tag(t) == "t")


def xlsx_sheet_names(path) -> list[str]:
    """The workbook's sheets, in order."""
    with zipfile.ZipFile(path) as z:
        book = ElementTree.fromstring(z.read("xl/workbook.xml"))
    return [el.get("name") or "" for el in book.iter() if _tag(el) == "sheet"]


def read_xlsx_sheet(path, sheet_name: str = SENDS_SHEET) -> list[list]:
    """The rows of one sheet as lists of cell values (strings or numbers),
    read with the standard library only."""
    with zipfile.ZipFile(path) as z:
        names = set(z.namelist())
        shared = []
        if "xl/sharedStrings.xml" in names:
            root = ElementTree.fromstring(z.read("xl/sharedStrings.xml"))
            shared = [_text_of(si) for si in root if _tag(si) == "si"]
        book = ElementTree.fromstring(z.read("xl/workbook.xml"))
        rels = ElementTree.fromstring(z.read("xl/_rels/workbook.xml.rels"))
        targets = {r.get("Id"): r.get("Target") for r in rels}
        sheets = [el for el in book.iter() if _tag(el) == "sheet"]
        chosen = next((s for s in sheets if (s.get("name") or "").strip().lower()
                       == sheet_name.lower()), None)
        if chosen is None:
            raise ValueError(f"{Path(path).name} has no sheet named \"{sheet_name}\" "
                             f"(it has: {', '.join(s.get('name') or '?' for s in sheets)})")
        rid = next((v for k, v in chosen.attrib.items() if k.endswith("}id") or k == "id"), "")
        target = (targets.get(rid) or "").lstrip("/")
        target = target if target.startswith("xl/") else "xl/" + target
        root = ElementTree.fromstring(z.read(target))
    rows = []
    for row in (el for el in root.iter() if _tag(el) == "row"):
        values: dict[int, object] = {}
        nxt = 0
        for c in row:
            if _tag(c) != "c":
                continue
            idx = _col_index(c.get("r") or "")
            idx = nxt if idx < 0 else idx
            nxt = idx + 1
            kind = c.get("t") or ""
            v = next((x for x in c if _tag(x) == "v"), None)
            if kind == "inlineStr":
                value = _text_of(c)
            elif kind == "s" and v is not None and (v.text or "").strip().isdigit():
                i = int(v.text)
                value = shared[i] if i < len(shared) else ""
            elif v is None:
                value = ""
            elif kind in ("str", "e", "b"):
                value = v.text or ""
            else:
                try:
                    value = float(v.text) if "." in (v.text or "") else int(v.text)
                except (TypeError, ValueError):
                    value = v.text or ""
            values[idx] = value
        width = max(values) + 1 if values else 0
        rows.append([values.get(i, "") for i in range(width)])
    return rows


def read_sends(path) -> list[dict]:
    """The sends sheet as dicts keyed by its header (the row naming
    "Brand / Company"). A .csv with the same header works too."""
    path = Path(path)
    if path.suffix.lower() == ".csv":
        with open(path, newline="", encoding="utf-8-sig") as f:
            rows = list(csv.reader(f))
    else:
        rows = read_xlsx_sheet(path)
    norm = lambda v: " ".join(str(v or "").split()).lower()  # noqa: E731
    header_at = next((i for i, r in enumerate(rows)
                      if any(norm(v) == "brand / company" for v in r)), None)
    if header_at is None:
        raise ValueError(f"{path.name}: no header row with \"Brand / Company\"")
    header = [" ".join(str(v or "").split()) for v in rows[header_at]]
    out = []
    for r in rows[header_at + 1:]:
        if not any(str(v).strip() for v in r):
            continue
        out.append({h: (r[i] if i < len(r) else "") for i, h in enumerate(header) if h})
    return out


def _get(row: dict, *names: str):
    low = {k.lower(): v for k, v in row.items()}
    for n in names:
        if n.lower() in low:
            return low[n.lower()]
    return ""


def _first_name(name: str) -> str:
    parts = (name or "").split()
    return parts[0] if parts else ""


# ── The import ──

async def _link_company(state, domain: str) -> str:
    domain = (domain or "").strip().lower()
    domain = re.sub(r"^https?://", "", domain).split("/")[0].removeprefix("www.")
    if not domain:
        return ""
    company = await state.get_company_by_domain(domain)
    return company.id if company else ""


async def _link_prospect(state, slug: str, company_id: str, name: str) -> str:
    """A Harvey contact for this person: same LinkedIn slug, else the same
    name at the same company."""
    rows = await state._li_query(
        "SELECT id, linkedin_url, first_name, last_name, company_id FROM prospects "
        "WHERE lower(linkedin_url) LIKE ? OR (company_id != '' AND company_id = ?)",
        (f"%/in/{slug}%", company_id or "\x00"))
    for r in rows:
        if slug_from_url(r["linkedin_url"] or "") == slug:
            return r["id"]
    for r in rows:
        if company_id and r["company_id"] == company_id and names_match(
                f"{r['first_name']} {r['last_name']}", name):
            return r["id"]
    return ""


async def import_tracker(state, pipeline_path=None, sends_path=None,
                         today: date | None = None) -> ImportReport:
    report = ImportReport()
    seen: set[str] = set()

    async def lead_for(slug, **fields):
        lead_id, created = await state.li_upsert_lead(slug, **fields)
        if slug not in seen:
            seen.add(slug)
            if created:
                report.leads_created += 1
            else:
                report.leads_updated += 1
        return lead_id

    if sends_path:
        for row in read_sends(sends_path):
            url = str(_get(row, "LinkedIn Profile") or "").strip()
            slug = slug_from_url(url)
            name = " ".join(str(_get(row, "Name") or "").split())
            if not slug:
                report.skipped.append(f"invite to {name or 'a row'} has no profile URL")
                continue
            when = parse_when(_get(row, "Sent at (IST)"))
            note = str(_get(row, "Personalized Note") or "").strip()
            lead_id = await lead_for(slug, stage="invited", name=name, first_name=_first_name(name),
                                     company=str(_get(row, "Brand / Company") or "").strip(),
                                     profile_url=profile_url(slug), invited_at=when,
                                     source="import_sends")
            if await state.li_add_message(lead_id, direction="out", kind="invite_note", body=note,
                                          status="sent", sent_at=when, created_at=when,
                                          approved_by="you"):
                report.invites += 1
            lead = await state.li_get_lead(lead_id)
            if when and (lead.get("last_outbound_at") or "") < _iso(when):
                await state.li_update_lead(lead_id, last_outbound_at=when)

    if pipeline_path:
        rows = json.loads(Path(pipeline_path).read_text(encoding="utf-8-sig"))
        if isinstance(rows, dict):
            rows = rows.get("rows") or rows.get("leads") or list(rows.values())
        for row in rows if isinstance(rows, list) else []:
            if not isinstance(row, dict):
                continue
            slug = (str(row.get("slug") or "").strip().lower().strip("/")
                    or slug_from_url(str(row.get("profile") or "")))
            name = " ".join(str(row.get("name") or "").split())
            if not slug:
                report.skipped.append(f"{name or 'a row'} has no slug or profile")
                continue
            tracker_stage = str(row.get("stage") or "").strip().lower()
            stage = PIPELINE_STAGES.get(tracker_stage, "accepted")
            connected = parse_short_date(str(row.get("connected") or ""), today)
            accepted_at = datetime.combine(connected, datetime.min.time()) if connected else None
            company_id = await _link_company(state, str(row.get("domain") or ""))
            if company_id:
                report.linked_companies += 1
            note = f"tracker stage: {tracker_stage}" if tracker_stage else ""
            if tracker_stage not in PIPELINE_STAGES and tracker_stage:
                note += " (not a known stage, imported as accepted)"
            lead_id = await lead_for(
                slug, stage=stage, name=name,
                first_name=str(row.get("first") or "").strip() or _first_name(name),
                company=str(row.get("brand") or "").strip(),
                profile_url=str(row.get("profile") or "").strip() or profile_url(slug),
                accepted_at=accepted_at,
                company_id=company_id, note=note, source="import_pipeline")
            if company_id:
                prospect_id = await _link_prospect(state, slug, company_id, name)
                if prospect_id:
                    await state.li_update_lead(lead_id, prospect_id=prospect_id)
            lead = await state.li_get_lead(lead_id)
            if tracker_stage == "same_company_skip":
                if not lead.get("no_message_reason"):
                    await state.li_update_lead(lead_id, no_message_reason=SAME_COMPANY_REASON)
                report.no_message += 1
            if stage == "declined":
                await state.li_set_stage(lead_id, "declined")
                report.declined += 1
            draft = str(row.get("draft") or "").strip()
            sent_at = parse_when(row.get("sent")) if row.get("sent") else None
            if draft and sent_at:
                if await state.li_add_message(lead_id, direction="out", kind="message", body=draft,
                                              status="sent", sent_at=sent_at, created_at=sent_at,
                                              approved_by="you"):
                    report.messages += 1
                if stage != "declined":
                    await state.li_set_stage(lead_id, "messaged")
                lead = await state.li_get_lead(lead_id)
                if (lead.get("last_outbound_at") or "") < _iso(sent_at):
                    await state.li_update_lead(lead_id, last_outbound_at=sent_at)
            elif draft and stage != "declined" and tracker_stage != "same_company_skip":
                # Written in the tracker, never sent: it waits on the dashboard.
                pending = await state.li_pending_for(lead_id, kinds=("message",))
                sent = [m for m in await state.li_messages(lead_id=lead_id, kinds=("message",),
                                                           statuses=("sent",))]
                if not pending and not sent and await state.li_add_message(
                        lead_id, direction="out", kind="message", body=draft, status="draft"):
                    report.drafts += 1
    await state.log_action("linkedin_import", "linkedin", {
        "leads_created": report.leads_created, "leads_updated": report.leads_updated,
        "invites": report.invites, "messages": report.messages, "drafts": report.drafts,
        "declined": report.declined})
    return report
