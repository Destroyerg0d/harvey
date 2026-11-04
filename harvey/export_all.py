"""Everything Harvey holds, as one spreadsheet: a sheet per kind of data.

Read-only. Left out on purpose: the settings table (it can hold keys and tokens)
and Harvey's own bookkeeping (usage, runs, the processed-reply ledger). Columns
that name a secret are dropped, and a secret in a link is masked.

Every value is stored as text, so an email body or a web page that starts with
"=" stays words and never runs as a formula when the sheet is opened.
"""

import io
import re
import sqlite3
from datetime import datetime

MEDIA_TYPE = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"

# (sheet name, table), in the order a person would look for them.
SHEETS = (
    ("Contacts", "prospects"),
    ("Companies", "companies"),
    ("Emails", "outbox"),
    ("Conversations", "conversations"),
    ("Website leads", "inbound_leads"),
    ("LinkedIn leads", "linkedin_leads"),
    ("LinkedIn messages", "linkedin_messages"),
    ("WhatsApp contacts", "whatsapp_contacts"),
    ("WhatsApp messages", "whatsapp_messages"),
    ("Facts", "observations"),
    ("Campaigns", "campaigns"),
    ("Lessons", "lessons"),
    ("Feedback", "feedback"),
    ("Activity", "actions"),
)
LEFT_OUT = "settings, keys and tokens, and Harvey's own bookkeeping (usage, runs)"

_SECRET_COLUMN_RE = re.compile(r"token|secret|password|api_?key|credential", re.I)
_SECRET_IN_LINK_RE = re.compile(r"([?&](?:t|token|key|api_key|apikey|secret|password)=)[^&\s\"'<>]+", re.I)
# The limits of a worksheet: longer cells and extra rows can't be stored.
MAX_CELL_CHARS = 32000
MAX_ROWS = 1_048_000


def default_name(now: datetime | None = None) -> str:
    return f"harvey-export-{(now or datetime.now()).strftime('%Y-%m-%d')}.xlsx"


def _text(value) -> str:
    from openpyxl.cell.cell import ILLEGAL_CHARACTERS_RE
    text = "" if value is None else str(value)
    text = _SECRET_IN_LINK_RE.sub(r"\1REDACTED", text)
    return ILLEGAL_CHARACTERS_RE.sub("", text)[:MAX_CELL_CHARS]


def _cell(sheet, value, font=None):
    from openpyxl.cell import WriteOnlyCell
    cell = WriteOnlyCell(sheet, value=None)
    # Set the type by hand: given the value directly, a leading "=" makes a formula.
    cell._value = _text(value)
    cell.data_type = "s"
    if font is not None:
        cell.font = font
    return cell


def _table_exists(db, table: str) -> bool:
    return db.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (table,)).fetchone() is not None


def _write_sheet(book, db, name: str, table: str, bold) -> int:
    sheet = book.create_sheet(name)
    sheet.freeze_panes = "A2"
    columns = [row[1] for row in db.execute(f"PRAGMA table_info({table})")
               if not _SECRET_COLUMN_RE.search(row[1])]
    sheet.append([_cell(sheet, column, bold) for column in columns])
    quoted = ", ".join(f'"{column}"' for column in columns)
    count = 0
    for row in db.execute(f"SELECT {quoted} FROM {table} LIMIT {MAX_ROWS}"):
        sheet.append([_cell(sheet, value) for value in row])
        count += 1
    return count


def write_workbook(db_path: str, target) -> dict:
    """Write the workbook to ``target`` (a path or a file object). Returns
    {sheet name: rows}. Opens the database read-only."""
    from openpyxl import Workbook
    from openpyxl.styles import Font
    bold = Font(bold=True)
    book = Workbook(write_only=True)
    summary = book.create_sheet("About")
    db = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    counts: dict[str, int] = {}
    try:
        for name, table in SHEETS:
            if _table_exists(db, table):
                counts[name] = _write_sheet(book, db, name, table, bold)
    finally:
        db.close()
    summary.append([_cell(summary, "Harvey export", bold)])
    summary.append([_cell(summary, "Made"), _cell(summary, datetime.now().strftime("%Y-%m-%d %H:%M"))])
    summary.append([_cell(summary, "Left out"), _cell(summary, LEFT_OUT)])
    summary.append([])
    summary.append([_cell(summary, "Sheet", bold), _cell(summary, "Rows", bold)])
    for name, rows in counts.items():
        summary.append([_cell(summary, name), _cell(summary, rows)])
    book.save(target)
    return counts


def workbook_bytes(db_path: str) -> bytes:
    buffer = io.BytesIO()
    write_workbook(db_path, buffer)
    return buffer.getvalue()
