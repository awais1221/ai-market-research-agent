from datetime import datetime, timezone

import pytest

from agent import rag
from agent.indexer import build_documents, sync_index
from agent.models import PricingPlan, SourceSpec
from agent.rag import build_where, find_competitors, parse_period

TODAY = datetime(2026, 11, 12, 15, 0, tzinfo=timezone.utc)
DAY = 86400


def _ts(y, m, d):
    return int(datetime(y, m, d, tzinfo=timezone.utc).timestamp())


@pytest.mark.parametrize("question,since,until,label", [
    ("What changed in the last 3 months?", _ts(2026, 8, 14), None, "last 3 months"),
    ("anything new in the past week", _ts(2026, 11, 5), None, "past week"),
    ("changes this month", _ts(2026, 11, 1), None, "this month"),
    ("What happened last month?", _ts(2026, 10, 1), _ts(2026, 11, 1), "last month"),
    ("pricing changes this year", _ts(2026, 1, 1), None, "this year"),
    ("What did they launch in 2025?", _ts(2025, 1, 1), _ts(2026, 1, 1), "2025"),
    ("Who changed prices in March?", _ts(2026, 3, 1), _ts(2026, 4, 1), "March 2026"),
    ("anything since September 2025", _ts(2025, 9, 1), None, "since September 2025"),
    ("Who launched AI features this quarter?", _ts(2026, 10, 1), None, "this quarter"),
    ("How much is Zapier Pro?", None, None, ""),
])
def test_parse_period(question, since, until, label):
    assert parse_period(question, TODAY) == (since, until, label)


def test_find_competitors_whole_words_only():
    names = ["Zapier", "n8n", "Make", "IFTTT"]
    assert find_competitors("Did Zapier's or N8N's pricing change?", names) == ["Zapier", "n8n"]
    assert find_competitors("Which one makes more money?", names) == []  # not "Make"


def test_build_where_keeps_current_pages_in_open_periods():
    f = rag.Filters(competitors=["Zapier"], since=100)
    where = build_where(f)
    assert where["$and"][0] == {"competitor": "Zapier"}
    assert {"date_ts": {"$gte": 100}} in where["$and"][1]["$or"]
    assert build_where(rag.Filters()) is None


@pytest.fixture
def kb(storage, vstore):
    """A small knowledge base: two competitors, one price change, one post."""
    rows = storage.sync_sources([
        SourceSpec(competitor="n8n", type="blog", url="https://blog.n8n.io/rss/", format="rss"),
        SourceSpec(competitor="Zapier", type="pricing", url="https://zapier.com/pricing"),
    ])
    src_n, src_z = sorted(rows, key=lambda r: r["id"])
    old = storage.add_snapshot(src_z["id"], "## Professional $19.99/month", "h1", "Pricing",
                               _ts(2026, 8, 1))
    new = storage.add_snapshot(src_z["id"], "## Professional $24.99/month\nTeam plan for teams",
                               "h2", "Pricing", _ts(2026, 9, 14))
    storage.save_pricing(src_z["id"], new, _ts(2026, 9, 14),
                         [PricingPlan(name="Professional", price=24.99, limits="750 tasks")])
    cid = storage.add_change(source_id=src_z["id"], kind="pricing_change", dedupe_key="k1",
                             effective_at=_ts(2026, 9, 14), old_snapshot_id=old,
                             new_snapshot_id=new, diff_text="- $19.99\n+ $24.99",
                             pricing_changes=[{"kind": "price_increase", "plan": "Professional",
                                               "text": "Professional: $19.99/mo → $24.99/mo"}])
    storage.set_analysis(cid, "Zapier raised the Professional plan price.", "high", "pricing",
                         "Zapier is more expensive now.", "llm")
    storage.add_item(src_n["id"], "g1", "Introducing the AI agent builder",
                     "https://blog.n8n.io/agents", _ts(2026, 10, 20),
                     "n8n launches a visual AI agent builder with memory and tools.")
    sync_index(storage, vstore)
    return storage, vstore


def test_index_documents(kb):
    storage, vstore = kb
    ids = sorted(d.doc_id for d in build_documents(storage))
    assert ids == ["change:1", "page:2", "plans:2", "post:1"]
    assert sync_index(storage, vstore)["updated"] == 0  # idempotent: nothing re-embedded


def test_price_question_gets_exact_pricing_table(kb, fake_llm):
    storage, vstore = kb
    result = rag.answer("How much does Zapier cost per month?", storage, vstore, today=TODAY)
    assert result.found and result.filters["competitors"] == ["Zapier"]
    system = fake_llm.calls[-1][0]["content"]
    assert "[1] Zapier pricing table" in system and "Professional: $24.99/mo" in system
    assert "Today is 12 Nov 2026" in system
    assert [s.title for s in result.sources] == ["Zapier pricing plans"]  # only cited ones


def test_competitor_and_date_filters_narrow_the_search(kb, fake_llm):
    storage, vstore = kb
    r = rag.retrieve("What did n8n launch in the last 2 months?", storage, vstore, today=TODAY)
    assert r.found and {s.competitor for s in r.sources} == {"n8n"}
    r = rag.retrieve("What did n8n launch in 2025?", storage, vstore, today=TODAY)
    assert not r.found  # the only n8n post is from 2026


def test_comparison_questions_search_every_competitor(kb, fake_llm):
    storage, vstore = kb
    r = rag.retrieve("Which competitors launched AI agent features or changed pricing?",
                     storage, vstore, today=TODAY)
    assert {s.competitor for s in r.sources} == {"Zapier", "n8n"}


def test_off_topic_question_never_calls_the_llm(kb, fake_llm):
    storage, vstore = kb
    result = rag.answer("What is the weather in Paris tomorrow?", storage, vstore, today=TODAY)
    assert not result.found and "don't have any collected data" in result.answer
    assert not fake_llm.calls


def test_stream_answer_events(kb, fake_llm):
    storage, vstore = kb
    events = list(rag.stream_answer("Zapier Professional plan price", storage, vstore))
    assert events[0]["type"] == "sources" and events[0]["found"]
    assert "".join(e["text"] for e in events if e["type"] == "token") == fake_llm.rag_reply
    assert events[-1] == {"type": "done", "cited": [1]}


def test_follow_up_keeps_competitor(kb, fake_llm):
    storage, vstore = kb
    history = [rag.ChatTurn(role="user", content="What does Zapier charge?"),
               rag.ChatTurn(role="assistant", content="$24.99 [1]")]
    r = rag.retrieve("and when did that change?", storage, vstore, history, TODAY)
    assert r.filters.competitors == ["Zapier"]


def test_price_change_question_gets_the_full_change_log(kb, fake_llm):
    storage, vstore = kb
    rag.answer("Which competitors changed their pricing in the last 3 months?", storage, vstore,
               today=TODAY)
    system = fake_llm.calls[-1][0]["content"]
    assert "Zapier pricing change log" in system
    assert "exact: Professional: $19.99/mo → $24.99/mo" in system
    fake_llm.calls.clear()
    rag.answer("Which competitors changed their pricing in the last 7 days?", storage, vstore,
               today=TODAY)  # the September change is outside this period
    assert not fake_llm.calls or "pricing change log" not in fake_llm.calls[-1][0]["content"]


def test_stream_that_breaks_off_is_replaced_by_the_full_reply(monkeypatch):
    from types import SimpleNamespace as NS

    from agent import llm_client

    def chunk(text, finish=None):
        return NS(choices=[NS(delta=NS(content=text), finish_reason=finish)])

    monkeypatch.setattr(llm_client, "_ensure_configured", lambda: None)
    monkeypatch.setattr(llm_client, "call_llm", lambda messages: "The whole answer [1].")
    monkeypatch.setattr(llm_client, "_create", lambda kw: iter([chunk("The wh")]))
    assert list(llm_client.stream_llm([])) == ["The wh", "The whole answer [1]."]
    assert isinstance(list(llm_client.stream_llm([]))[-1], llm_client.FullReply)
    monkeypatch.setattr(llm_client, "_create", lambda kw: iter([chunk("Done."), chunk("", "stop")]))
    assert list(llm_client.stream_llm([])) == ["Done."]


def test_recently_means_the_last_30_days():
    since, until, label = parse_period("What has n8n launched recently?", TODAY)
    assert label == "last 30 days" and until is None
    assert since == _ts(2026, 10, 13)
    assert parse_period("Pricing in the last 3 months, recently?", TODAY)[2] == "last 3 months"


def test_whats_new_question_lists_important_posts_of_the_period(kb, fake_llm):
    storage, vstore = kb
    rows = storage.sources()
    src_n = next(r for r in rows if r["competitor"] == "n8n")
    item = storage.add_item(src_n["id"], "g2", "Meet Nod, a local AI agent app",
                            "https://blog.n8n.io/meet-nod/", _ts(2026, 11, 10), "Nod runs locally.")
    cid = storage.add_change(source_id=src_n["id"], kind="new_item", dedupe_key="k2",
                             effective_at=_ts(2026, 11, 10), item_id=item)
    storage.set_analysis(cid, "n8n released Nod, a local AI agent app.", "high",
                         "announcement", "", "llm")
    sync_index(storage, vstore)
    rag.answer("What has n8n launched recently?", storage, vstore, today=TODAY)
    system = fake_llm.calls[-1][0]["content"]
    assert "Meet Nod, a local AI agent app | n8n | 10 Nov 2026 | importance: high" in system
    assert system.count("Meet Nod") == 1  # not repeated from the search results
