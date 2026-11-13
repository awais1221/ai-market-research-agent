from dataclasses import replace

import pytest

from agent import pipeline
from agent.config import get_settings
from agent.pipeline import confirmed_pricing_changes, run
from tests.conftest import pricing_html, rss

SOURCES = """
competitors:
  - name: Acme
    sources:
      - {type: pricing, url: "https://acme.test/pricing"}
      - {type: blog, url: "https://acme.test/feed", format: rss}
  - name: Globex
    sources:
      - {type: page, url: "https://globex.test/"}
"""


class FakeTelegram:
    def __init__(self):
        self.alerts, self.problems = [], []

    def send_alert(self, change):
        self.alerts.append(change)

    def send_problems(self, problems):
        self.problems.append(problems)


class FakeSheets:
    def __init__(self):
        self.synced = 0

    def sync(self, storage):
        pending = storage.unsynced_changes()
        storage.mark_synced([c["id"] for c in pending])
        self.synced += len(pending)
        return len(pending)


@pytest.fixture
def setup(tmp_path, monkeypatch, site, collector, storage, fake_llm):
    path = tmp_path / "sources.yaml"
    path.write_text(SOURCES, encoding="utf-8")
    settings = replace(get_settings(), sources_file=str(path))
    monkeypatch.setattr(pipeline, "get_settings", lambda: settings)
    site.add("https://acme.test/pricing",
             pricing_html([("Free", "$0/month - 100 tasks"), ("Pro", "$20/month - 750 tasks")]))
    site.add("https://acme.test/feed",
             rss([("https://acme.test/a", "Hello world", "Mon, 02 Nov 2026 10:00:00 GMT")]),
             ctype="application/rss+xml")
    site.add("https://globex.test/", "<main><h1>Globex</h1><p>Automation for everyone.</p></main>")
    tg, sh = FakeTelegram(), FakeSheets()

    def go():
        return run(storage=storage, collector=collector, telegram=tg, sheets=sh)
    return go, site, storage, tg, sh, fake_llm


def test_first_run_is_a_silent_baseline(setup):
    go, site, storage, tg, sh, llm = setup
    summary = go()
    assert summary.checked == 3 and summary.baselines == 3 and not summary.changes
    assert not tg.alerts
    plans = storage.latest_pricing()[0]["plans"]
    assert [(p.name, p.price) for p in plans] == [("Free", 0.0), ("Pro", 20.0)]
    assert len(storage.items()) == 1  # existing posts become background knowledge


def test_unchanged_page_costs_no_llm_call(setup):
    go, site, storage, tg, sh, llm = setup
    go()
    calls = len(llm.calls)
    # only noise changes: copyright year and a relative date
    site.add("https://globex.test/", "<main><h1>Globex</h1><p>Automation for everyone.</p>"
                                     "<p>Updated 3 hours ago</p></main><footer>© 2027</footer>")
    summary = go()
    assert summary.unchanged == 3 and not summary.changes
    assert len(llm.calls) == calls


def test_price_increase_alerts_once(setup):
    go, site, storage, tg, sh, llm = setup
    go()
    site.add("https://acme.test/pricing",
             pricing_html([("Free", "$0/month - 100 tasks"), ("Pro", "$24/month - 750 tasks"),
                           ("Team", "$69/month - 2000 tasks")]))
    summary = go()
    assert len(summary.changes) == 1
    change = storage.change(summary.changes[0])
    assert change["kind"] == "pricing_change"
    assert change["importance"] == "high" and change["category"] == "pricing"
    texts = [p["text"] for p in change["pricing_changes"]]
    assert "Pro: $20/mo → $24/mo" in texts and "New plan: Team ($69/mo)" in texts
    assert len(tg.alerts) == 1 and tg.alerts[0]["id"] == change["id"]
    assert sh.synced == 1

    again = go()  # same page again: nothing new, no duplicate alert
    assert not again.changes and len(tg.alerts) == 1 and sh.synced == 1


def test_new_blog_post_detected(setup):
    go, site, storage, tg, sh, llm = setup
    go()
    site.add("https://acme.test/feed",
             rss([("https://acme.test/b", "Introducing AI agents", "Tue, 10 Nov 2026 10:00:00 GMT"),
                  ("https://acme.test/a", "Hello world", "Mon, 02 Nov 2026 10:00:00 GMT")]),
             ctype="application/rss+xml")
    summary = go()
    (change_id,) = summary.changes
    change = storage.change(change_id)
    assert change["kind"] == "new_item" and change["item_title"] == "Introducing AI agents"
    assert change["summary"] == "Post 1 summary." and change["importance"] == "medium"
    assert not tg.alerts  # medium is below the default "high" alert threshold


def test_failing_source_does_not_stop_the_run(setup):
    go, site, storage, tg, sh, llm = setup
    site.add("https://globex.test/", "down", status=500)
    summary = go()
    assert summary.checked == 3 and summary.baselines == 2
    assert any("Globex page" in e for e in summary.errors)
    go()
    go()  # third failure in a row -> one warning on Telegram
    assert len(tg.problems) == 1 and "failed 3 runs in a row" in tg.problems[0][0]
    go()
    assert len(tg.problems) == 1


def test_llm_outage_falls_back_to_rules(setup):
    go, site, storage, tg, sh, llm = setup
    go()
    llm.fail = True
    site.add("https://globex.test/", "<main><h1>Globex</h1><p>Automation for everyone.</p>"
                                     "<p>Now with an AI agent builder for every team.</p></main>")
    summary = go()
    change = storage.change(summary.changes[0])
    assert change["analyzed_by"] == "rules"
    assert "AI agent builder" in change["summary"]


def test_pricing_changes_must_appear_in_the_page_diff():
    changes = [
        {"kind": "price_increase", "plan": "Pro", "old_price": 20.0, "new_price": 24.0,
         "text": ""},
        {"kind": "plan_added", "plan": "Team", "text": ""},
        {"kind": "limits_changed", "plan": "Pro", "new": "1000 tasks", "text": ""},
    ]
    # the page diff only shows the new Pro price: the other two were AI wording drift
    kept = confirmed_pricing_changes(changes, added="## Pro $24/month", removed="## Pro $20/month")
    assert [c["kind"] for c in kept] == ["price_increase"]


def test_removed_plan_mentioned_only_in_a_sentence_still_counts_as_removed():
    changes = [{"kind": "plan_removed", "plan": "Standard", "old": "$0", "text": "Plan removed: Standard"}]
    removed = "### Standard\nthen $5 per active flow per month"
    added = ("### Free\n### Plus\nSSO, standard roles, and email support start on Team. "
             "Ultimate adds SCIM provisioning and audit logs.")
    assert confirmed_pricing_changes(changes, added=added, removed=removed) == changes
    # ...but a plan that is still a heading in the new version was not removed
    assert confirmed_pricing_changes(changes, added=added + "\n### Standard", removed=removed) == []


def test_plan_shown_differently_is_neither_added_nor_removed():
    removed = "### Standard\n### Community Edition\nStandard roles | Not included"
    added = ("### Free\nStandard roles | Not included | Included\nStandard rolesNot included\n"
             "Every plan works on-prem, from the open source Community Edition to Enterprise.")
    changes = [{"kind": "plan_removed", "plan": "Standard", "text": "Plan removed: Standard"},
               {"kind": "plan_removed", "plan": "Community Edition", "text": "x"}]
    assert [c["plan"] for c in confirmed_pricing_changes(changes, added, removed)] == ["Standard"]
    # a card that used to be a sentence ("Community EditionMIT Licensed") is not a new plan
    new = [{"kind": "plan_added", "plan": "Community Edition", "text": "x"},
           {"kind": "plan_added", "plan": "Unlimited", "text": "y"}]
    kept = confirmed_pricing_changes(new, added="### Community Edition\n### Unlimited",
                                     removed="Community EditionMIT Licensed\n### Ultimate\nUnlimited runs")
    assert [c["plan"] for c in kept] == ["Unlimited"]


def test_common_word_inside_other_words_does_not_confirm_a_plan_change():
    changes = [{"kind": "plan_removed", "plan": "Pro", "text": "Plan removed: Pro"}]
    removed = "AI needs infrastructure: Zapier now includes Tables on all products"
    assert confirmed_pricing_changes(changes, added="### Team $69/month", removed=removed) == []


def test_renamed_heading_keeps_the_plan():
    changes = [{"kind": "plan_removed", "plan": "Pro", "text": "Plan removed: Pro"}]
    assert confirmed_pricing_changes(changes, added="### Agents Pro\n$33.33/month",
                                     removed="## Pro\n$33.33 /mo") == []
    assert confirmed_pricing_changes(changes, added="Everything in Pro, plus SSO",
                                     removed="## Pro\n$33.33 /mo") == changes
