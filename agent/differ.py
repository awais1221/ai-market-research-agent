"""Change detection: normalise page text, ignore noise, diff, compare pricing.

Pages change all the time in ways nobody cares about ("posted 5 minutes ago",
the year in the footer, a rotating testimonial, a cookie banner). Those lines
are removed before hashing, so only real content changes count.
"""

from __future__ import annotations

import difflib
import hashlib
import re
from dataclasses import dataclass, field

from agent.models import PricingPlan

_MONTHS = (r"(?:jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|june?|july?|"
           r"aug(?:ust)?|sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)")
_TOKEN_NOISE = [
    re.compile(r"\b\d+\s*(?:seconds?|secs?|minutes?|mins?|hours?|hrs?|days?|weeks?|"
               r"months?|years?)\s+ago\b", re.I),
    re.compile(r"\b(?:just now|yesterday|today)\b", re.I),
    re.compile(rf"\b{_MONTHS}\.?\s+\d{{1,2}}(?:st|nd|rd|th)?,?\s+\d{{4}}\b", re.I),
    re.compile(rf"\b\d{{1,2}}(?:st|nd|rd|th)?\s+{_MONTHS}\.?,?\s+\d{{4}}\b", re.I),
    re.compile(r"\b\d{4}-\d{2}-\d{2}(?:[T ]\d{2}:\d{2}(?::\d{2})?(?:\.\d+)?Z?)?\b"),
    re.compile(r"\b\d{1,2}:\d{2}(?::\d{2})?\s*(?:am|pm|utc|gmt)?\b", re.I),
    re.compile(r"(?:©|&copy;|\(c\)|copyright)\s*(?:\d{4}\s*[-–]\s*)?\d{4}", re.I),
    re.compile(r"\b\d+\s*min(?:ute)?s?\s+read\b", re.I),
]
_LINE_NOISE = re.compile(
    r"cookie|consent|accept all|we use cookies|privacy preferences|all rights reserved|"
    r"\bsign in\b|\blog in\b|skip to (?:main )?content|loading\.\.\.|subscribe to our newsletter",
    re.I,
)


def normalize(text: str, ignore: list[str] | None = None) -> str:
    """Text with noise removed, used for hashing and diffing."""
    patterns = [re.compile(p, re.I) for p in (ignore or [])]
    out: list[str] = []
    for line in text.splitlines():
        line = line.strip()
        if not line or _LINE_NOISE.search(line):
            continue
        if any(p.search(line) for p in patterns):
            continue
        had_noise = False
        for token in _TOKEN_NOISE:
            line, count = token.subn("", line)
            had_noise = had_noise or bool(count)
        line = re.sub(r"\s+", " ", line).strip(" -|·•")
        if had_noise:
            line = line.strip(" ,:")
            if len(line) < 20:
                continue  # "Updated 3 days ago" -> "Updated": the leftover is noise too
        if len(line) >= 2:
            out.append(line)
    return "\n".join(out)


def content_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


@dataclass
class Diff:
    added: list[str] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)

    @property
    def size(self) -> int:
        """Changed characters, counting a line edited in place only once."""
        a = sum(len(x) for x in self.added)
        r = sum(len(x) for x in self.removed)
        return max(a, r)

    @property
    def empty(self) -> bool:
        return not self.added and not self.removed

    def as_text(self, max_chars: int = 6000) -> str:
        lines = [f"- {x}" for x in self.removed] + [f"+ {x}" for x in self.added]
        text = "\n".join(lines)
        return text if len(text) <= max_chars else text[:max_chars] + "\n… (diff truncated)"


def diff_text(old: str, new: str) -> Diff:
    """Line-level diff of two normalised texts. Moved lines don't count."""
    old_lines, new_lines = old.splitlines(), new.splitlines()
    diff = Diff()
    matcher = difflib.SequenceMatcher(a=old_lines, b=new_lines, autojunk=False)
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag in ("replace", "delete"):
            diff.removed.extend(old_lines[i1:i2])
        if tag in ("replace", "insert"):
            diff.added.extend(new_lines[j1:j2])
    # a block that only moved position is not a change
    moved = set(diff.added) & set(diff.removed)
    diff.added = [x for x in diff.added if x not in moved]
    diff.removed = [x for x in diff.removed if x not in moved]
    return diff


def side_by_side(old: str, new: str, context: int = 2) -> list[dict]:
    """Rows for the dashboard diff view: {op: equal|removed|added, text}."""
    old_lines, new_lines = old.splitlines(), new.splitlines()
    rows: list[dict] = []
    matcher = difflib.SequenceMatcher(a=old_lines, b=new_lines, autojunk=False)
    for group in matcher.get_grouped_opcodes(context):
        if rows:
            rows.append({"op": "gap", "text": "…"})
        for tag, i1, i2, j1, j2 in group:
            if tag == "equal":
                rows += [{"op": "equal", "text": x} for x in old_lines[i1:i2]]
            else:
                rows += [{"op": "removed", "text": x} for x in old_lines[i1:i2]]
                rows += [{"op": "added", "text": x} for x in new_lines[j1:j2]]
    return rows


# ------------------------------------------------------------------ pricing
def _plan_key(name: str) -> str:
    key = re.sub(r"[^a-z0-9+ ]", "", name.lower())
    key = re.sub(r"\b(plan|tier|edition|package)\b", "", key)
    return re.sub(r"\s+", " ", key).strip()


def _numbers(text: str) -> list[str]:
    """The numbers in a limits string ("750 tasks, 2 users" -> ['750', '2']).
    Comparing numbers, not wording, means a reworded LLM extraction of the same
    page is not reported as a change."""
    return re.findall(r"\d+(?:[.,]\d+)*\s*[kKmM]?\b", text.replace(",", ""))


def _fmt_price(plan: PricingPlan) -> str:
    if plan.price is None:
        return "custom"
    symbol = {"USD": "$", "EUR": "€", "GBP": "£"}.get(plan.currency.upper(), plan.currency + " ")
    amount = f"{plan.price:,.2f}".rstrip("0").rstrip(".")
    period = {"month": "/mo", "year": "/yr"}.get(plan.period, f" ({plan.period})")
    return f"{symbol}{amount}{period if plan.price else ''}"


def compare_pricing(old: list[PricingPlan], new: list[PricingPlan]) -> list[dict]:
    """Field-by-field plan comparison. Each change is a dict with a ready
    human-readable ``text`` (e.g. "Pro: $20/mo → $24/mo")."""
    old_map = {_plan_key(p.name): p for p in old}
    new_map = {_plan_key(p.name): p for p in new}
    changes: list[dict] = []
    for key, plan in new_map.items():
        before = old_map.get(key)
        if before is None:
            changes.append({"kind": "plan_added", "plan": plan.name, "new": _fmt_price(plan),
                            "text": f"New plan: {plan.name} ({_fmt_price(plan)})"})
            continue
        price_changed = (before.price is None) != (plan.price is None) or (
            before.price is not None and plan.price is not None
            and abs(before.price - plan.price) > 0.009)
        if price_changed or before.currency.upper() != plan.currency.upper() \
                or before.period != plan.period:
            kind = "price_changed"
            if before.price is not None and plan.price is not None:
                kind = "price_increase" if plan.price > before.price else "price_decrease"
            changes.append({
                "kind": kind, "plan": plan.name,
                "old": _fmt_price(before), "new": _fmt_price(plan),
                "old_price": before.price, "new_price": plan.price,
                "text": f"{plan.name}: {_fmt_price(before)} → {_fmt_price(plan)}",
            })
        if sorted(_numbers(before.limits)) != sorted(_numbers(plan.limits)) \
                and before.limits and plan.limits:
            changes.append({"kind": "limits_changed", "plan": plan.name,
                            "old": before.limits, "new": plan.limits,
                            "text": f"{plan.name} limits: {before.limits} → {plan.limits}"})
    for key, plan in old_map.items():
        if key not in new_map:
            changes.append({"kind": "plan_removed", "plan": plan.name, "old": _fmt_price(plan),
                            "text": f"Plan removed: {plan.name} (was {_fmt_price(plan)})"})
    return changes


def format_plans(plans: list[PricingPlan]) -> str:
    return "\n".join(
        f"- {p.name}: {_fmt_price(p)}" + (f" ({p.billing} billing)" if p.billing else "")
        + (f", {p.limits}" if p.limits else "")
        for p in plans
    )
