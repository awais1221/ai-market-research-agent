import json

import gspread
import httpx
import pytest

from agent import digest
from agent.models import PricingPlan, SourceSpec
from agent.notify import telegram
from agent.notify.sheets import SheetsClient, change_row
from agent.notify.telegram import (TelegramClient, TelegramError, format_alert,
                                   markdown_to_telegram, split_message)

CHANGE = {"id": 7, "competitor": "Zapier <AI>", "source_type": "pricing",
          "effective_at": 1789000000, "summary": "Pro went from $20 to $24 & more.",
          "importance": "high", "category": "pricing", "why": "Costs more.",
          "url": "https://zapier.com/pricing", "origin": "live",
          "pricing_changes": [{"text": "Pro: $20/mo → $24/mo"}]}


def test_split_message_respects_limit():
    text = "\n\n".join(f"Paragraph {i} " + "x" * 900 for i in range(10))
    parts = split_message(text)
    assert all(len(p) <= 4096 for p in parts) and len(parts) >= 3
    assert "".join(parts).replace("\n", "") == text.replace("\n", "")
    assert split_message("y" * 9000) and all(len(p) <= 4096 for p in split_message("y" * 9000))


def test_alert_is_escaped_html():
    html = format_alert(CHANGE)
    assert html.startswith("🔴 <b>Zapier &lt;AI&gt;</b>")
    assert "$20 to $24 &amp; more" in html and "• Pro: $20/mo → $24/mo" in html
    assert '<a href="https://zapier.com/pricing">' in html


def test_markdown_to_telegram():
    md = "# Digest\n**Highlights.** Up [1].\n- [1] **Pricing**: x ([link](https://a.b/c))"
    out = markdown_to_telegram(md)
    assert out.splitlines()[0] == "<b>Digest</b>"
    assert "<b>Highlights.</b>" in out and '<a href="https://a.b/c">link</a>' in out
    assert out.splitlines()[-1].startswith("• [1]")


def _client(handler):
    return TelegramClient("123:SECRET", "42", httpx.Client(transport=httpx.MockTransport(handler)))


def test_send_retries_after_429_and_hides_token(monkeypatch):
    monkeypatch.setattr(telegram.time, "sleep", lambda s: None)
    calls = []

    def handler(request):
        calls.append(json.loads(request.content))
        if len(calls) == 1:
            return httpx.Response(429, json={"ok": False, "parameters": {"retry_after": 1}})
        return httpx.Response(200, json={"ok": True, "result": {}})

    _client(handler).send("hello")
    assert len(calls) == 2 and calls[1]["parse_mode"] == "HTML" and calls[1]["chat_id"] == "42"

    def bad(request):
        return httpx.Response(400, json={"ok": False, "description": "Bad Request: chat not found"})

    with pytest.raises(TelegramError) as err:
        _client(bad).send("hi")
    assert "chat not found" in str(err.value) and "SECRET" not in str(err.value)


class _FakeRag:
    def __init__(self):
        self.questions = []

    def answer(self, question, storage=None):
        from agent.rag import Answer, Source
        self.questions.append(question)
        return Answer(answer="**Yes** [1]", found=True, sources=[
            Source(index=1, title="Zapier pricing", url="https://zapier.com/pricing")])


def test_bot_commands(storage):
    fake = _FakeRag()
    assert "/ask" in telegram.handle_command("/start", storage, fake, digest)
    assert telegram.handle_command("/latest", storage, fake, digest) == "No changes recorded yet."
    reply = telegram.handle_command("/ask@my_bot Did Zapier change prices?", storage, fake, digest)
    assert fake.questions == ["Did Zapier change prices?"]
    assert reply.startswith("<b>Yes</b> [1]") and "zapier.com/pricing" in reply
    telegram.handle_command("plain question", storage, fake, digest)
    assert fake.questions[-1] == "plain question"
    assert "quiet period" in telegram.handle_command("/digest", storage, fake, digest)


# ------------------------------------------------------------------ sheets
class FakeWorksheet:
    def __init__(self, rows=None):
        self.rows = rows or []

    def row_values(self, n):
        return self.rows[n - 1] if len(self.rows) >= n else []

    def append_row(self, row):
        self.rows.append(row)

    def append_rows(self, rows, value_input_option=None):
        self.rows.extend(rows)

    def clear(self):
        self.rows = []

    def get_all_records(self, default_blank=""):
        header = self.rows[0]
        return [dict(zip(header, r)) for r in self.rows[1:]]


class FakeBook:
    title = "Competitor tracker"

    def __init__(self):
        self.tabs = {}

    def worksheet(self, name):
        if name not in self.tabs:
            raise gspread.WorksheetNotFound(name)
        return self.tabs[name]

    def add_worksheet(self, title, rows, cols):
        self.tabs[title] = FakeWorksheet()
        return self.tabs[title]


def test_sheets_sync_appends_once_and_writes_pricing(storage):
    (src,) = storage.sync_sources([SourceSpec(competitor="Zapier", type="pricing",
                                              url="https://zapier.com/pricing")])
    snap = storage.add_snapshot(src["id"], "text", "h", "t", 1789000000)
    storage.save_pricing(src["id"], snap, 1789000000, [PricingPlan(name="Pro", price=24)])
    cid = storage.add_change(source_id=src["id"], kind="page_change", dedupe_key="a",
                             effective_at=1789000000, diff_text="+ =HYPERLINK(evil)")
    storage.set_analysis(cid, "=cmd|' /C calc'!A0", "high", "pricing", "", "llm")
    book = FakeBook()
    client = SheetsClient(book)
    assert client.sync(storage) == 1
    assert client.sync(storage) == 0  # never duplicated
    changes = book.tabs["Changes"].rows
    assert changes[0][0] == "Date" and len(changes) == 2
    assert changes[1][5].startswith("'=")  # formula injection neutralised
    assert book.tabs["Pricing"].rows[1][:3] == ["Zapier", "Pro", 24.0]


def test_sheets_competitor_tab_round_trip(storage):
    book = FakeBook()
    client = SheetsClient(book)
    with pytest.raises(gspread.WorksheetNotFound):
        client.read_sources()
    specs = [SourceSpec(competitor="IFTTT", type="pricing", url="https://ifttt.com/plans")]
    assert client.export_sources(specs) == 1
    from agent.sources import parse_rows
    parsed, errors = parse_rows(client.read_sources())
    assert parsed == specs and not errors


def test_change_row_shape():
    row = change_row(CHANGE)
    assert row[1] == "Zapier <AI>" and row[6] == "Pro: $20/mo → $24/mo" and row[-1] == 7
