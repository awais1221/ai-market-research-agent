import json

import pytest
from fastapi.testclient import TestClient

from agent.models import PricingPlan, SourceSpec
from web import main


@pytest.fixture
def client(storage, vstore, monkeypatch):
    monkeypatch.setattr(main, "get_storage", lambda: storage)
    monkeypatch.setattr(main, "get_vector_store", lambda: vstore)
    (src,) = storage.sync_sources([SourceSpec(competitor="Zapier", type="pricing",
                                              url="https://zapier.com/pricing")])
    old = storage.add_snapshot(src["id"], "## Pro $20/month\nGreat", "a", "P", 1780000000)
    new = storage.add_snapshot(src["id"], "## Pro $24/month\nGreat", "b", "P", 1789000000)
    storage.save_pricing(src["id"], old, 1780000000, [PricingPlan(name="Pro", price=20)])
    storage.save_pricing(src["id"], new, 1789000000, [PricingPlan(name="Pro", price=24)])
    cid = storage.add_change(source_id=src["id"], kind="pricing_change", dedupe_key="x",
                             effective_at=1789000000, old_snapshot_id=old, new_snapshot_id=new,
                             diff_text="- ## Pro $20/month\n+ ## Pro $24/month",
                             pricing_changes=[{"kind": "price_increase", "plan": "Pro",
                                               "text": "Pro: $20/mo → $24/mo"}])
    storage.set_analysis(cid, "Zapier raised Pro to $24.", "high", "pricing", "", "llm")
    return TestClient(main.app)


def test_dashboard_page(client):
    r = client.get("/")
    assert r.status_code == 200 and "AI Market Research Agent" in r.text


def test_overview(client):
    data = client.get("/api/overview").json()
    (zap,) = data["competitors"]
    assert zap["name"] == "Zapier" and zap["changes"] == 1 and zap["high"] == 1
    assert zap["plans"][0]["price"] == 24


def test_changes_and_detail(client):
    changes = client.get("/api/changes?importance=high").json()["changes"]
    assert [c["summary"] for c in changes] == ["Zapier raised Pro to $24."]
    assert client.get("/api/changes?importance=low").json()["changes"] == []
    detail = client.get(f"/api/changes/{changes[0]['id']}").json()
    ops = {r["op"]: r["text"] for r in detail["diff"]}
    assert ops["removed"] == "## Pro $20/month" and ops["added"] == "## Pro $24/month"
    assert detail["old_plans"][0]["price"] == 20 and detail["new_plans"][0]["price"] == 24
    assert client.get("/api/changes/999").status_code == 404


def test_ask_and_stream(client, fake_llm):
    r = client.post("/api/ask", json={"question": "How much is Zapier Pro per month?"})
    assert r.status_code == 200 and r.json()["found"]
    with client.stream("POST", "/api/ask/stream",
                       json={"question": "What is Zapier's price?"}) as resp:
        events = [json.loads(line[6:]) for line in resp.iter_lines() if line.startswith("data: ")]
    assert events[0]["type"] == "sources" and events[-1]["type"] == "done"
    assert client.post("/api/ask", json={"question": ""}).status_code == 422


def test_ask_reports_llm_errors(client, fake_llm):
    fake_llm.fail = True
    r = client.post("/api/ask", json={"question": "How much is Zapier Pro per month?"})
    assert r.status_code == 502 and "provider down" in r.json()["detail"]
