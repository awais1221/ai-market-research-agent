from agent.differ import (compare_pricing, content_hash, diff_text, format_plans, normalize,
                          side_by_side)
from agent.models import PricingPlan


def test_normalize_removes_noise():
    text = "\n".join([
        "# Pricing",
        "Posted 5 minutes ago",
        "Updated Nov 12, 2026",
        "We use cookies to improve your experience",
        "© 2024-2026 Example Inc.",
        "Pro $20/month",
        "Last deploy 2026-11-12T08:15:00Z",
    ])
    out = normalize(text)
    assert "Pro $20/month" in out
    assert "# Pricing" in out
    assert "ago" not in out and "cookies" not in out and "2026" not in out


def test_normalize_custom_ignore_patterns():
    text = "Pro $20/month\nTrusted by 1,234,567 teams\nCustomer of the day: ACME"
    out = normalize(text, ignore=[r"trusted by [\d,]+", r"customer of the day"])
    assert out == "Pro $20/month"


def test_noise_only_changes_hash_the_same():
    a = normalize("Pro $20/month\nUpdated 3 hours ago\n© 2025 X")
    b = normalize("Pro $20/month\nUpdated 2 days ago\n© 2026 X")
    assert content_hash(a) == content_hash(b)


def test_diff_added_removed_and_moves_ignored():
    old = "A line\nB line\nC line"
    new = "C line\nA line\nB line changed\nD line"
    d = diff_text(old, new)
    assert "D line" in d.added and "B line changed" in d.added
    assert d.removed == ["B line"]
    assert "C line" not in d.added  # only moved
    assert d.as_text().startswith("- B line")


def test_side_by_side_rows():
    rows = side_by_side("a\nb\nc", "a\nB\nc")
    ops = [r["op"] for r in rows]
    assert "removed" in ops and "added" in ops and "equal" in ops


def _p(name, price, limits="", **kw):
    return PricingPlan(name=name, price=price, limits=limits, **kw)


def test_compare_pricing_detects_exact_changes():
    old = [_p("Free", 0, "100 tasks"), _p("Pro plan", 20, "750 tasks"), _p("Legacy", 5)]
    new = [_p("Free", 0, "100 tasks"), _p("Pro", 24, "750 tasks"), _p("Team", 69)]
    changes = {c["kind"]: c for c in compare_pricing(old, new)}
    assert changes["price_increase"]["text"] == "Pro: $20/mo → $24/mo"
    assert changes["plan_added"]["plan"] == "Team"
    assert changes["plan_removed"]["plan"] == "Legacy"
    assert len(changes) == 3


def test_compare_pricing_ignores_reworded_limits():
    old = [_p("Pro", 20, "750 tasks per month")]
    new = [_p("Pro", 20, "750 tasks/month")]
    assert compare_pricing(old, new) == []
    newer = [_p("Pro", 20, "1,000 tasks/month")]
    assert compare_pricing(old, newer)[0]["kind"] == "limits_changed"


def test_compare_pricing_custom_and_currency():
    old = [_p("Enterprise", None), _p("Starter", 20, currency="EUR")]
    new = [_p("Enterprise", 99), _p("Starter", 20, currency="EUR")]
    (change,) = compare_pricing(old, new)
    assert change["text"] == "Enterprise: custom → $99/mo"
    assert "€20/mo" in format_plans(new)


def test_short_list_items_survive_and_noise_leftovers_go():
    out = normalize("- Webhooks\n- 25 users\nUpdated 3 days ago\nPosted on Nov 3, 2026")
    assert out.splitlines() == ["Webhooks", "25 users"]
