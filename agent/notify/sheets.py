"""Google Sheets: a change log, the latest pricing, and the client-editable
competitor list.

Tabs (created automatically if missing):

* **Changes**: one row per detected change (appended, never duplicated)
* **Pricing**: the latest plans of every competitor (rewritten each run)
* **Competitors**: optional, the list of pages to monitor (SOURCES_FROM_SHEET=true)

Auth uses a Google Cloud *service account* (free, no billing): share the Sheet
with the service account's e-mail address as an Editor. Without credentials
this module is simply skipped.
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path

from agent.config import get_settings
from agent.sources import SHEET_COLUMNS, specs_to_rows

logger = logging.getLogger(__name__)

CHANGES_TAB, PRICING_TAB, SOURCES_TAB = "Changes", "Pricing", "Competitors"
CHANGES_HEADER = ["Date", "Competitor", "Type", "Importance", "Category", "Summary",
                  "Price change", "Why it matters", "Link", "Origin", "Change ID"]
PRICING_HEADER = ["Competitor", "Plan", "Price", "Currency", "Period", "Billing", "Limits",
                  "Captured", "Source"]


def _date(ts: int) -> str:
    return time.strftime("%Y-%m-%d", time.gmtime(ts))


def _cell(text: str) -> str:
    """Stop the Sheet from treating scraped text as a formula ("=..." / "+...")."""
    text = (text or "").replace("\r", " ")
    return "'" + text if text[:1] in ("=", "+", "-", "@") else text


def change_row(c: dict) -> list:
    return [_date(c["effective_at"]), c["competitor"], c["source_type"], c["importance"],
            c["category"], _cell(c["summary"][:1000]),
            _cell("; ".join(p["text"] for p in c.get("pricing_changes") or [])),
            _cell(c.get("why", "")[:500]), c.get("url", ""), c["origin"], c["id"]]


class SheetsClient:
    def __init__(self, spreadsheet):
        self.book = spreadsheet  # a gspread Spreadsheet (or a test double)

    def _tab(self, title: str, header: list[str], create: bool = True):
        import gspread

        try:
            ws = self.book.worksheet(title)
        except gspread.WorksheetNotFound:
            if not create:
                raise
            ws = self.book.add_worksheet(title=title, rows=1000, cols=len(header))
            ws.append_row(header)
            return ws
        if not ws.row_values(1):
            ws.append_row(header)
        return ws

    # ------------------------------------------------------------ competitors
    def read_sources(self) -> list[dict]:
        ws = self._tab(SOURCES_TAB, SHEET_COLUMNS, create=False)
        return ws.get_all_records(default_blank="")

    def export_sources(self, specs) -> int:
        ws = self._tab(SOURCES_TAB, SHEET_COLUMNS)
        ws.clear()
        ws.append_rows([SHEET_COLUMNS] + specs_to_rows(specs), value_input_option="RAW")
        return len(specs)

    # ------------------------------------------------------------ sync
    def sync(self, storage) -> int:
        """Append changes not yet in the Sheet and refresh the Pricing tab."""
        pending = storage.unsynced_changes()
        if pending:
            ws = self._tab(CHANGES_TAB, CHANGES_HEADER)
            ws.append_rows([change_row(c) for c in pending], value_input_option="RAW")
            storage.mark_synced([c["id"] for c in pending])
        self.write_pricing(storage)
        return len(pending)

    def write_pricing(self, storage) -> None:
        rows = []
        for entry in storage.latest_pricing():
            for p in entry["plans"]:
                rows.append([entry["competitor"], p.name,
                             "" if p.price is None else p.price, p.currency, p.period,
                             p.billing or "", _cell(p.limits), _date(entry["captured_at"]),
                             entry["url"]])
        if not rows:
            return
        ws = self._tab(PRICING_TAB, PRICING_HEADER)
        ws.clear()
        ws.append_rows([PRICING_HEADER] + rows, value_input_option="RAW")

    def title(self) -> str:
        return self.book.title


def _credentials_info(raw: str) -> dict:
    raw = raw.strip()
    if raw.startswith("{"):
        return json.loads(raw)
    path = Path(raw)
    if not path.is_file():
        raise ValueError("GOOGLE_SERVICE_ACCOUNT_JSON must be the JSON key itself or a path "
                         "to the key file")
    return json.loads(path.read_text(encoding="utf-8"))


def from_settings() -> SheetsClient | None:
    s = get_settings()
    if not s.sheets_configured:
        return None
    try:
        import gspread

        gc = gspread.service_account_from_dict(_credentials_info(s.google_service_account_json))
        return SheetsClient(gc.open_by_key(s.sheet_id))
    except Exception as exc:
        # never log the credentials themselves
        logger.warning("Google Sheets disabled: %s", type(exc).__name__)
        return None
