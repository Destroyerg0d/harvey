"""Importing a hand-kept LinkedIn tracker (fictional rows only)."""

import asyncio
import json
import zipfile
from datetime import date, datetime
from xml.sax.saxutils import escape

import pytest

from harvey.linkedin.importer import (
    SAME_COMPANY_REASON, import_tracker, parse_short_date, parse_when, read_sends,
)
from harvey.models.company import Company
from harvey.state import StateManager

TODAY = date(2026, 11, 4)


def run(coro):
    return asyncio.run(coro)


def write_xlsx(path, rows, sheet="Verified Sends"):
    """A minimal workbook: shared strings for text, a number for the serial date."""
    strings: list[str] = []

    def cell(ref, value):
        if isinstance(value, (int, float)):
            return f'<c r="{ref}"><v>{value}</v></c>'
        strings.append(str(value))
        return f'<c r="{ref}" t="s"><v>{len(strings) - 1}</v></c>'
    body = []
    for r, row in enumerate(rows, 1):
        cells = "".join(cell(f"{chr(65 + c)}{r}", v) for c, v in enumerate(row) if v != "")
        body.append(f'<row r="{r}">{cells}</row>')
    ns = 'xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"'
    rel = 'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"'
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("xl/workbook.xml", f'<workbook {ns} {rel}><sheets>'
                   f'<sheet name="Notes" sheetId="1" r:id="rId1"/>'
                   f'<sheet name="{sheet}" sheetId="2" r:id="rId2"/></sheets></workbook>')
        z.writestr("xl/_rels/workbook.xml.rels",
                   '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                   '<Relationship Id="rId1" Target="worksheets/sheet1.xml"/>'
                   '<Relationship Id="rId2" Target="/xl/worksheets/sheet2.xml"/></Relationships>')
        z.writestr("xl/worksheets/sheet1.xml", f'<worksheet {ns}><sheetData/></worksheet>')
        z.writestr("xl/worksheets/sheet2.xml", f'<worksheet {ns}><sheetData>{"".join(body)}</sheetData></worksheet>')
        z.writestr("xl/sharedStrings.xml", f'<sst {ns}>' + "".join(
            f"<si><t>{escape(s)}</t></si>" for s in strings) + "</sst>")


SENDS = [
    ["Invites sent, fictional", "", "", "", ""],
    [],
    ["Name", "Brand / Company", "LinkedIn Profile", "Sent at (IST)", "Personalized Note"],
    ["Jane Doe", "Example Goods", "https://www.linkedin.com/in/jane-doe-1/", "2026-10-20 11:30",
     "Fellow store owner, would be glad to connect."],
    ["Omar Field", "Field Supply", "https://www.linkedin.com/in/omar-field/", 46315.5, ""],
    ["No Profile", "Nowhere", "", "2026-10-21", ""],
]
PIPELINE = [
    {"name": "Jane Doe", "first": "Jane", "brand": "Example Goods", "slug": "jane-doe-1",
     "profile": "https://www.linkedin.com/in/jane-doe-1/", "domain": "examplegoods.test",
     "connected": "Oct28", "stage": "followed_up", "draft": "Jane, your product pages load slowly on phones.",
     "sent": "2026-11-01"},
    {"name": "Ada Lane", "first": "Ada", "brand": "Lane Co", "slug": "ada-lane", "profile": "",
     "domain": "", "connected": "Oct 30", "stage": "accepted_no_reply", "draft": "Ada, a draft.",
     "sent": ""},
    {"name": "Bo Chen", "first": "Bo", "brand": "Chen Ltd", "slug": "bo-chen", "profile": "",
     "domain": "", "connected": "Oct29", "stage": "declined", "draft": "", "sent": ""},
    {"name": "Cy Park", "first": "Cy", "brand": "Chen Ltd", "slug": "cy-park", "profile": "",
     "domain": "", "connected": "Oct29", "stage": "soft_no", "draft": "", "sent": ""},
    {"name": "Di Ross", "first": "Di", "brand": "Lane Co", "slug": "di-ross", "profile": "",
     "domain": "", "connected": "Oct27", "stage": "same_company_skip", "draft": "Di, a draft.",
     "sent": ""},
    {"name": "Eve Hart", "first": "Eve", "brand": "Hart & Co", "slug": "eve-hart", "profile": "",
     "domain": "", "connected": "Oct26", "stage": "hot", "draft": "Eve, a sent one.",
     "sent": "Nov 2, 2026"},
]


@pytest.fixture
def files(tmp_path):
    sends = tmp_path / "sends.xlsx"
    write_xlsx(sends, SENDS)
    pipeline = tmp_path / "pipeline.json"
    pipeline.write_text(json.dumps(PIPELINE), encoding="utf-8")
    state = StateManager(str(tmp_path / "h.db"))
    run(state.init_db())
    return state, pipeline, sends


def test_dates():
    assert parse_short_date("Oct28", TODAY) == date(2026, 10, 28)
    assert parse_short_date("Dec 30", TODAY) == date(2025, 12, 30)       # never in the future
    assert parse_short_date("28 Oct", TODAY) == date(2026, 10, 28)
    assert parse_short_date("", TODAY) is None and parse_short_date("soon", TODAY) is None
    # IST to UTC: 11:30 IST is 06:00 UTC.
    assert parse_when("2026-10-20 11:30") == datetime(2026, 10, 20, 6, 0)
    assert parse_when(46315.5) == datetime(2026, 10, 20, 6, 30)          # an Excel serial, IST noon
    assert parse_when("Nov 2, 2026") == datetime(2026, 11, 1, 18, 30)


def test_the_sends_sheet_is_found_by_its_header(files):
    _, _, sends = files
    rows = read_sends(sends)
    assert [r["Name"] for r in rows] == ["Jane Doe", "Omar Field", "No Profile"]
    assert rows[0]["Personalized Note"].startswith("Fellow store owner")


def test_import_maps_every_stage(files):
    state, pipeline, sends = files
    run(state.add_company(Company(name="Example Goods", domain="examplegoods.test")))
    report = run(import_tracker(state, pipeline, sends, today=TODAY))
    by = {lead["slug"]: lead for lead in run(state.li_leads())}

    jane = by["jane-doe-1"]
    assert jane["stage"] == "messaged" and jane["company_id"] and jane["accepted_at"].startswith("2026-10-28")
    msgs = run(state.li_messages(lead_id=jane["id"]))
    assert [(m["kind"], m["status"]) for m in msgs] == [("invite_note", "sent"), ("message", "sent")]
    assert jane["last_outbound_at"].startswith("2026-10-31T18:30")      # Nov 1 IST

    assert by["omar-field"]["stage"] == "invited"
    assert by["ada-lane"]["stage"] == "accepted"
    ada = run(state.li_messages(lead_id=by["ada-lane"]["id"]))
    assert [(m["kind"], m["status"]) for m in ada] == [("message", "draft")]   # waits for approval
    assert by["bo-chen"]["stage"] == "declined" and by["cy-park"]["stage"] == "declined"
    assert by["di-ross"]["stage"] == "accepted"
    assert by["di-ross"]["no_message_reason"] == SAME_COMPANY_REASON
    assert run(state.li_messages(lead_id=by["di-ross"]["id"])) == []          # never messaged
    assert by["eve-hart"]["stage"] == "messaged" and "hot" in by["eve-hart"]["note"]
    assert report.invites == 2 and report.messages == 2 and report.drafts == 1
    assert report.declined == 2 and report.no_message == 1 and report.linked_companies == 1
    assert len(report.skipped) == 1 and report.leads_created == 7


def test_reimport_changes_nothing(files):
    state, pipeline, sends = files
    run(import_tracker(state, pipeline, sends, today=TODAY))
    before_leads = run(state.li_leads())
    before_msgs = run(state.li_messages())
    report = run(import_tracker(state, pipeline, sends, today=TODAY))
    assert report.leads_created == 0 and report.invites == 0 and report.messages == 0
    assert report.drafts == 0
    assert len(run(state.li_leads())) == len(before_leads)
    assert len(run(state.li_messages())) == len(before_msgs)
    assert {lead["slug"]: lead["stage"] for lead in run(state.li_leads())} == \
        {lead["slug"]: lead["stage"] for lead in before_leads}


def test_a_csv_sends_file_works_too(tmp_path):
    path = tmp_path / "sends.csv"
    path.write_text("Name,Brand / Company,LinkedIn Profile,Sent at (IST),Personalized Note\n"
                    "Jane Doe,Example Goods,https://www.linkedin.com/in/jane-doe-1/,2026-10-20 11:30,Hi\n",
                    encoding="utf-8")
    state = StateManager(str(tmp_path / "h.db"))
    run(state.init_db())
    report = run(import_tracker(state, sends_path=path))
    assert report.invites == 1 and run(state.li_get_lead_by_slug("jane-doe-1"))["stage"] == "invited"


def test_a_missing_sheet_says_which_sheets_exist(tmp_path):
    path = tmp_path / "other.xlsx"
    write_xlsx(path, [["Name"]], sheet="Something Else")
    with pytest.raises(ValueError, match="Something Else"):
        read_sends(path)
