"""One spreadsheet of everything Harvey holds: every kind of data, no secrets, no live formulas."""

import asyncio
import os
import sqlite3
import tempfile

import pytest
from openpyxl import load_workbook

from harvey.export_all import SHEETS, default_name, workbook_bytes, write_workbook
from harvey.models.prospect import Prospect
from harvey.state import StateManager


@pytest.fixture
def db():
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "harvey.db")
        state = StateManager(path)

        async def seed():
            await state.init_db()
            pid = await state.add_prospect(Prospect(first_name="Jo", last_name="Lee", email="jo@shop.example",
                                                    status="new"))
            await state.add_outbox_item(prospect_id=pid, to_email="jo@shop.example", subject='=HYPERLINK("x")',
                                        body="pay at https://shop.example/pay?t=abc123&x=1",
                                        send_at="2026-01-01T00:00:00", status="sent", step=1, kind="sequence")
            await state.set_setting("gmail_token", "should-not-appear")
        asyncio.run(seed())
        yield path


def exported(db, tmp_path):
    out = tmp_path / "all.xlsx"
    counts = write_workbook(db, str(out))
    return load_workbook(out), counts


def rows_of(sheet):
    values = list(sheet.iter_rows(values_only=True))
    return list(values[0]), values[1:]


def test_every_kind_of_data_has_a_sheet(db, tmp_path):
    book, counts = exported(db, tmp_path)
    assert book.sheetnames[0] == "About"
    assert {name for name, _ in SHEETS} <= set(book.sheetnames)
    assert counts["Contacts"] == 1 and counts["Emails"] == 1


def test_contacts_carry_their_columns(db, tmp_path):
    book, _ = exported(db, tmp_path)
    header, rows = rows_of(book["Contacts"])
    assert rows[0][header.index("email")] == "jo@shop.example"


def test_a_formula_stays_text(db, tmp_path):
    book, _ = exported(db, tmp_path)
    sheet = book["Emails"]
    header = [c.value for c in sheet[1]]
    cell = sheet.cell(row=2, column=header.index("subject") + 1)
    assert cell.value == '=HYPERLINK("x")' and cell.data_type == "s"


def test_no_settings_and_a_secret_in_a_link_is_masked(db, tmp_path):
    book, _ = exported(db, tmp_path)
    assert "settings" not in [s.lower() for s in book.sheetnames]
    every = " ".join(str(v) for s in book.worksheets for row in s.iter_rows(values_only=True) for v in row if v)
    assert "should-not-appear" not in every and "abc123" not in every
    assert "pay?t=REDACTED&x=1" in every


def test_a_column_naming_a_secret_is_dropped(db, tmp_path):
    con = sqlite3.connect(db)
    con.execute("ALTER TABLE companies ADD COLUMN api_key TEXT")
    con.commit()
    con.close()
    book, _ = exported(db, tmp_path)
    assert "api_key" not in rows_of(book["Companies"])[0]


def test_a_missing_table_is_skipped_and_control_characters_are_stripped(db, tmp_path):
    con = sqlite3.connect(db)
    con.execute("DROP TABLE lessons")
    con.execute("UPDATE outbox SET body = 'a' || char(1) || 'b'")
    con.commit()
    con.close()
    book, counts = exported(db, tmp_path)
    assert "Lessons" not in counts
    sheet = book["Emails"]
    header = [c.value for c in sheet[1]]
    assert sheet.cell(row=2, column=header.index("body") + 1).value == "ab"


def test_the_database_is_only_read(db):
    before = os.path.getmtime(db)
    assert workbook_bytes(db)[:2] == b"PK"
    assert os.path.getmtime(db) == before


def test_the_file_name_has_the_date():
    assert default_name().startswith("harvey-export-") and default_name().endswith(".xlsx")
