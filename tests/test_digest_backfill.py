import json
from dataclasses import replace
from urllib.parse import parse_qs, urlparse

import httpx

from agent import backfill as backfill_mod
from agent import pipeline
from agent.backfill import backfill
from agent.config import get_settings
from agent.digest import build_digest
from agent.models import SourceSpec
from agent.storage import now
from tests.conftest import pricing_html, rss


def _changes(storage):
    (src,) = storage.sync_sources([SourceSpec(competitor="Zapier", type="pricing",
                                              url="https://zapier.com/pricing")])
    ids = []
    for i, (summary, imp) in enumerate([("Raised Pro to $24.", "high"),
                                        ("Reworded the FAQ.", "low")]):
        cid = storage.add_change(source_id=src["id"], kind="page_change", dedupe_key=f"d{i}",
                                 effective_at=now() - 86400 * (i + 1))
        storage.set_analysis(cid, summary, imp, "pricing", "", "llm")
        ids.append(cid)
    return ids


def test_digest_lists_changes_and_ai_highlights(storage, fake_llm):
    _changes(storage)
    d = build_digest(storage, days=7)
    md = d["markdown"]
    assert md.startswith("# Competitor digest:")
    assert "**Highlights.** Zapier raised prices [1]." in md
    assert "- [1] 🔴 **Pricing**: Raised Pro to $24." in md
    assert md.index("Raised Pro") < md.index("Reworded")  # important first
    assert storage.digests()[0]["id"] == d["id"]


def test_digest_without_llm_and_quiet_week(storage, fake_llm):
    _changes(storage)
    fake_llm.fail = True
    md = build_digest(storage, days=7, save=False)["markdown"]
    assert "Zapier: Raised Pro to $24 [1]." in md
    assert "quiet period" in build_digest(storage, days=7, save=False,
                                          until=now() - 30 * 86400)["markdown"]


def test_backfill_builds_history_from_the_wayback_machine(tmp_path, monkeypatch, storage,
                                                          fake_llm):
    path = tmp_path / "sources.yaml"
    path.write_text("""
competitors:
  - name: Acme
    sources:
      - {type: pricing, url: "https://acme.test/pricing"}
      - {type: blog, url: "https://acme.test/feed", format: rss}
""", encoding="utf-8")
    settings = replace(get_settings(), sources_file=str(path))
    monkeypatch.setattr(backfill_mod, "get_settings", lambda: settings)
    monkeypatch.setattr(pipeline, "get_settings", lambda: settings)

    versions = {
        "20260115000000": pricing_html([("Pro", "$20/month - 750 tasks")]),
        "20260215000000": pricing_html([("Pro", "$20/month - 750 tasks")], "Updated 3 days ago"),
        "20260601000000": pricing_html([("Pro", "$24/month - 750 tasks")]),
    }
    recent = now() - 20 * 86400
    import time
    pub = time.strftime("%a, %d %b %Y %H:%M:%S GMT", time.gmtime(recent))

    def handler(request: httpx.Request):
        url = str(request.url)
        if url.startswith(backfill_mod.CDX):
            query = parse_qs(urlparse(url).query)
            assert query["url"] == ["https://acme.test/pricing"]
            to = query.get("to", ["99999999999999"])[0]
            rows = [["timestamp", "original"]] + [[ts, "https://acme.test/pricing"]
                                                  for ts in versions if ts <= to]
            return httpx.Response(200, text=json.dumps(rows))
        if "web.archive.org/web/" in url:
            ts = url.split("/web/")[1].split("id_")[0]
            return httpx.Response(200, text=versions[ts], headers={"content-type": "text/html"})
        if url == "https://acme.test/feed":
            return httpx.Response(200, text=rss([
                ("https://acme.test/new", "Launching Teams", pub),
                ("https://acme.test/old", "Old news", "Mon, 02 Jan 2023 10:00:00 GMT")]),
                headers={"content-type": "application/rss+xml"})
        return httpx.Response(404)

    from agent.collector import Collector
    collector = Collector(client=httpx.Client(transport=httpx.MockTransport(handler)), delay=0)
    monkeypatch.setattr(backfill_mod, "now", lambda: 1794500000)  # 12 Nov 2026
    summary = backfill(storage, months=12, collector=collector)

    assert summary.snapshots == 2  # the noise-only February version is skipped
    kinds = [storage.change(c)["kind"] for c in summary.changes]
    assert sorted(kinds) == ["new_item", "pricing_change"]
    price = next(storage.change(c) for c in summary.changes
                 if storage.change(c)["kind"] == "pricing_change")
    assert price["pricing_changes"][0]["text"] == "Pro: $20/mo → $24/mo"
    assert price["origin"] == "archive" and price["notified"] == 1
    assert len(storage.items()) == 2  # old post kept as knowledge, not as a change

    again = backfill(storage, months=12, collector=collector)  # idempotent
    assert again.snapshots == 0 and not again.changes


def test_interrupted_backfill_resumes_with_newer_versions(tmp_path, monkeypatch, storage,
                                                         fake_llm):
    path = tmp_path / "sources.yaml"
    path.write_text("competitors:\n  - name: Acme\n    sources:\n"
                    "      - {type: pricing, url: \"https://acme.test/pricing\"}\n", encoding="utf-8")
    settings = replace(get_settings(), sources_file=str(path))
    monkeypatch.setattr(backfill_mod, "get_settings", lambda: settings)
    monkeypatch.setattr(pipeline, "get_settings", lambda: settings)
    versions = {"20260115000000": pricing_html([("Pro", "$20/month - 750 tasks")])}

    def handler(request: httpx.Request):
        url = str(request.url)
        if url.startswith(backfill_mod.CDX):
            to = parse_qs(urlparse(url).query).get("to", ["99999999999999"])[0]
            rows = [["timestamp", "original"]] + [[ts, "https://acme.test/pricing"]
                                                  for ts in sorted(versions) if ts <= to]
            return httpx.Response(200, text=json.dumps(rows))
        if "web.archive.org/web/" in url:
            ts = url.split("/web/")[1].split("id_")[0]
            return httpx.Response(200, text=versions[ts], headers={"content-type": "text/html"})
        return httpx.Response(404)

    from agent.collector import Collector
    collector = Collector(client=httpx.Client(transport=httpx.MockTransport(handler)), delay=0)
    monkeypatch.setattr(backfill_mod, "now", lambda: 1794500000)
    first = backfill(storage, months=12, collector=collector)  # "interrupted" after January
    assert first.snapshots == 1 and not first.changes

    versions["20260601000000"] = pricing_html([("Pro", "$24/month - 750 tasks")])
    resumed = backfill(storage, months=12, collector=collector)
    assert [storage.change(c)["pricing_changes"][0]["text"] for c in resumed.changes] == \
        ["Pro: $20/mo → $24/mo"]
    assert backfill(storage, months=12, collector=collector).snapshots == 0  # nothing left
