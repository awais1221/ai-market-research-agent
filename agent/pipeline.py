"""One monitoring run: collect → detect changes → analyse → notify.

The same page/feed processing is used by the live run and by the backfill
(which feeds in archived versions from the Wayback Machine), so history and
live changes look exactly the same in the database.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import sqlite3
import time
from dataclasses import dataclass, field

from agent import analyzer
from agent.collector import Collector, FeedItem, FetchError
from agent.config import Settings, get_settings
from agent.differ import compare_pricing, content_hash, diff_text, normalize
from agent.models import IMPORTANCE_RANK, PricingPlan
from agent.sources import load_sources
from agent.storage import Storage, now

logger = logging.getLogger(__name__)


@dataclass
class RunSummary:
    checked: int = 0
    unchanged: int = 0
    baselines: int = 0
    changes: list[int] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    source_warnings: list[str] = field(default_factory=list)
    alerts_sent: int = 0
    sheet_rows: int = 0
    llm_calls: int = 0
    reanalyzed: int = 0
    seconds: float = 0.0

    def as_dict(self) -> dict:
        return {"checked": self.checked, "unchanged": self.unchanged,
                "baselines": self.baselines, "changes": len(self.changes),
                "change_ids": self.changes, "errors": self.errors,
                "source_warnings": self.source_warnings, "alerts_sent": self.alerts_sent,
                "sheet_rows": self.sheet_rows, "llm_calls": self.llm_calls,
                "reanalyzed": self.reanalyzed,
                "seconds": round(self.seconds, 1)}

    def text(self) -> str:
        lines = [f"Checked {self.checked} source(s) in {self.seconds:.0f}s: "
                 f"{len(self.changes)} change(s), {self.unchanged} unchanged, "
                 f"{self.baselines} first snapshot(s), {self.llm_calls} AI call(s)."]
        if self.reanalyzed:
            lines.append(f"Added AI summaries to {self.reanalyzed} earlier change(s).")
        if self.alerts_sent or self.sheet_rows:
            lines.append(f"Telegram alerts: {self.alerts_sent}, Sheet rows: {self.sheet_rows}.")
        lines += [f"  ! {e}" for e in self.errors]
        lines += [f"  ? {w}" for w in self.source_warnings]
        return "\n".join(lines)


# ======================================================================= pages
def _numbers_in(text: str) -> set[str]:
    return {n.rstrip("0").rstrip(".") if "." in n else n
            for n in re.findall(r"\d+(?:\.\d+)?", text.replace(",", ""))}


def _labels_plan(text: str, plan: str) -> bool:
    """True if a line or table cell is the plan's label: "### Standard",
    "Standard", "Standard plan", "Pro $20/mo", "Agents Pro" - but not
    "Standard roles" or "Everything in Pro"."""
    label = re.compile(rf"(?:\w+ )?{re.escape(plan)}(\s+plan)?(\s*[$€£\d].*)?", re.I)
    for line in text.splitlines():
        line = line.lstrip("#-* ").strip()
        if any(label.fullmatch(cell.strip()) for cell in line.split("|")):
            return True
    return False


def _still_named(text: str, plan: str) -> bool:
    """Is the plan still on the page? A distinctive multi-word name ("Community
    Edition") counts anywhere; a single common word ("Standard", "Team") only as
    a plan label."""
    if " " in plan.strip():
        return plan.lower() in text.lower()
    return _labels_plan(text, plan)


def confirmed_pricing_changes(changes: list[dict], added: str, removed: str) -> list[dict]:
    """Keep only plan changes the page diff actually shows, so a different
    wording in two AI extractions of the same prices never becomes an alert."""
    added_nums, removed_nums = _numbers_in(added), _numbers_in(removed)
    keep = []
    for c in changes:
        kind = c["kind"]
        if kind in ("price_increase", "price_decrease", "price_changed"):
            new = c.get("new_price")
            old = c.get("old_price")
            fmt = lambda v: (f"{v:.2f}".rstrip("0").rstrip(".") if v is not None else None)  # noqa: E731
            if (new is not None and fmt(new) in added_nums) or \
                    (old is not None and fmt(old) in removed_nums):
                keep.append(c)
        elif kind == "plan_added":
            # ...unless the page already had it (now just shown differently)
            if _still_named(added, c["plan"]) and not _still_named(removed, c["plan"]):
                keep.append(c)
        elif kind == "plan_removed":
            if _still_named(removed, c["plan"]) and not _still_named(added, c["plan"]):
                keep.append(c)
        elif kind == "limits_changed":
            if _numbers_in(c["new"]) & added_nums:
                keep.append(c)
    return keep


def process_page(storage: Storage, src: sqlite3.Row, title: str, raw_text: str,
                 captured_at: int, *, origin: str = "live", previous: dict | None = None,
                 use_latest: bool = True, budget: analyzer.Budget | None = None,
                 notified: bool = False) -> tuple[str, int | None]:
    """Store a page version and record a change if it differs from the
    previous version. Returns (status, change_id): status is
    baseline | unchanged | minor | change."""
    settings = get_settings()
    ignore = json.loads(src["ignore_json"] or "[]")
    text = normalize(raw_text, ignore)
    digest = content_hash(text)
    if previous is None and use_latest:
        previous = storage.latest_snapshot(src["id"])
    if previous is not None and previous["content_hash"] == digest:
        return "unchanged", None

    snapshot_id = storage.add_snapshot(src["id"], text, digest, title, captured_at, origin)
    plans: list[PricingPlan] | None = None
    if src["type"] == "pricing":
        plans = analyzer.extract_pricing(text, src["competitor"], storage, budget)
        if plans:
            storage.save_pricing(src["id"], snapshot_id, captured_at, plans)
    if previous is None:
        return "baseline", None

    diff = diff_text(previous["text"], text)
    pricing_changes: list[dict] = []
    old_plans = storage.pricing_for_snapshot(previous["id"])
    if plans and old_plans:
        pricing_changes = confirmed_pricing_changes(
            compare_pricing(old_plans, plans), "\n".join(diff.added), "\n".join(diff.removed))
    if diff.size < settings.min_change_chars and not pricing_changes:
        return "minor", None

    change_id = storage.add_change(
        source_id=src["id"], kind="pricing_change" if pricing_changes else "page_change",
        dedupe_key=f"page:{src['id']}:{previous['content_hash'][:20]}:{digest[:20]}",
        effective_at=captured_at, old_snapshot_id=previous["id"], new_snapshot_id=snapshot_id,
        diff_text=diff.as_text(), pricing_changes=pricing_changes, origin=origin,
        notified=notified)
    if change_id is None:
        return "unchanged", None
    analysis, by = analyzer.analyze_change(src["type"], src["competitor"], diff.as_text(),
                                           pricing_changes, plans, storage, budget)
    storage.set_analysis(change_id, analysis.summary, analysis.importance, analysis.category,
                         analysis.why_it_matters, by)
    return "change", change_id


# ======================================================================= feeds
def process_feed(storage: Storage, src: sqlite3.Row, items: list[FeedItem], *,
                 origin: str = "live", history_since: int | None = None,
                 budget: analyzer.Budget | None = None) -> tuple[str, list[int]]:
    """Store new feed items. On the first check of a feed, existing posts are
    stored as background knowledge without alerts; with ``history_since``
    (backfill) posts newer than that date are also recorded as past changes."""
    baseline = not storage.has_items(src["id"])
    new_rows: list[tuple[int, FeedItem]] = []
    for it in sorted(items, key=lambda i: i.published_ts or 0):
        item_id = storage.add_item(src["id"], it.guid, it.title, it.url, it.published_ts,
                                   it.summary)
        if item_id is None:
            continue
        if baseline:
            if history_since and it.published_ts and it.published_ts >= history_since:
                new_rows.append((item_id, it))
        else:
            new_rows.append((item_id, it))
    if not new_rows:
        return ("baseline" if baseline else "unchanged"), []

    is_history = baseline  # posts found on the first check are history, not news
    analyses = analyzer.analyze_items(
        src["type"], src["competitor"],
        [{"title": it.title, "summary": it.summary} for _, it in new_rows], storage, budget)
    change_ids = []
    for (item_id, it), (analysis, by) in zip(new_rows, analyses):
        change_id = storage.add_change(
            source_id=src["id"], kind="new_item",
            dedupe_key=f"item:{src['id']}:{hashlib.sha1(it.guid.encode()).hexdigest()[:24]}",
            effective_at=it.published_ts or now(), item_id=item_id,
            diff_text=f"+ {it.title}\n{it.summary[:1500]}",
            origin="archive" if is_history else origin, notified=is_history)
        if change_id is not None:
            storage.set_analysis(change_id, analysis.summary, analysis.importance,
                                 analysis.category, analysis.why_it_matters, by)
            change_ids.append(change_id)
    return ("baseline" if baseline else "change"), change_ids


# ======================================================================= run
def check_source(storage: Storage, collector: Collector, src: sqlite3.Row,
                 budget: analyzer.Budget) -> tuple[str, list[int]]:
    settings = get_settings()
    if src["format"] == "rss":
        items = collector.fetch_feed(src["url"], settings.max_feed_items)
        return process_feed(storage, src, items, budget=budget)
    if src["format"] == "sitemap":
        items = collector.fetch_sitemap(src["url"], src["include"], settings.max_feed_items)
        return process_feed(storage, src, items, budget=budget)
    page = collector.fetch_page(src["url"], render=bool(src["render"]))
    status, change_id = process_page(storage, src, page.title, page.text, now(), budget=budget)
    return status, [change_id] if change_id else []


def reanalyze_rule_changes(storage: Storage, budget: analyzer.Budget, limit: int = 20) -> int:
    """Give an AI summary to changes that only got the rule-based fallback
    (the AI was down, over quota or not set up yet). Returns how many were fixed."""
    if not get_settings().llm_configured:
        return 0
    pending = storage.rule_analyzed(limit)
    fixed = 0
    # posts: one batched call per source, like a normal run
    by_source: dict[int, list[dict]] = {}
    for change in pending:
        if change["kind"] == "new_item" and change["item_id"]:
            by_source.setdefault(change["source_id"], []).append(change)
    for changes in by_source.values():
        items = []
        for change in changes:
            item = storage.item(change["item_id"])
            items.append({"title": item["title"], "summary": item["summary"] or ""})
        first = changes[0]
        results = analyzer.analyze_items(first["source_type"], first["competitor"], items,
                                         storage, budget)
        for change, (analysis, by) in zip(changes, results):
            if by == "llm":
                storage.set_analysis(change["id"], analysis.summary, analysis.importance,
                                     analysis.category, analysis.why_it_matters, by)
                fixed += 1
    # page changes: one call each
    for change in pending:
        if change["kind"] == "new_item":
            continue
        plans = storage.pricing_for_snapshot(change["new_snapshot_id"]) \
            if change["new_snapshot_id"] else None
        analysis, by = analyzer.analyze_change(
            change["source_type"], change["competitor"], change["diff_text"],
            change["pricing_changes"], plans, storage, budget)
        if by == "llm":
            storage.set_analysis(change["id"], analysis.summary, analysis.importance,
                                 analysis.category, analysis.why_it_matters, by)
            fixed += 1
    if fixed:
        logger.info("Added AI summaries to %d earlier change(s)", fixed)
    return fixed


def should_alert(change: dict, settings: Settings) -> bool:
    threshold = IMPORTANCE_RANK.get(settings.alert_min_importance, 2)
    return IMPORTANCE_RANK.get(change["importance"], 0) >= threshold


def open_storage(settings: Settings | None = None) -> Storage:
    settings = settings or get_settings()
    return Storage(settings.db_path)


def run(storage: Storage | None = None, collector: Collector | None = None,
        telegram=None, sheets=None, budget: analyzer.Budget | None = None) -> RunSummary:
    """Check every active source once, then send alerts and sync the Sheet."""
    from agent.notify import sheets as sheets_mod
    from agent.notify import telegram as telegram_mod

    settings = get_settings()
    started = time.perf_counter()
    storage = storage or open_storage(settings)
    collector = collector or Collector()
    budget = budget or analyzer.Budget()
    telegram = telegram if telegram is not None else telegram_mod.from_settings()
    sheets = sheets if sheets is not None else sheets_mod.from_settings()
    summary = RunSummary()
    run_id = storage.start_run("run")

    reader = sheets.read_sources if (sheets and settings.sources_from_sheet) else None
    source_list = load_sources(settings.sources_file, storage, reader)
    summary.source_warnings = source_list.errors
    active = storage.sync_sources(source_list.specs)
    logger.info("Monitoring %d source(s) from %s", len(active), source_list.origin)

    for src in active:
        label = f"{src['competitor']} {src['type']}"
        summary.checked += 1
        try:
            status, change_ids = check_source(storage, collector, src, budget)
        except FetchError as exc:
            _record_failure(storage, src, str(exc), summary, settings)
            continue
        except Exception as exc:  # one broken source never stops the run
            logger.exception("Unexpected error on %s", src["url"])
            _record_failure(storage, src, f"{type(exc).__name__}: {exc}", summary, settings)
            continue
        storage.mark_checked(src["id"])
        if status == "baseline":
            summary.baselines += 1
        elif not change_ids:
            summary.unchanged += 1
        summary.changes += change_ids
        logger.info("%-28s %s%s", label, status,
                    f" ({len(change_ids)} change(s))" if change_ids else "")

    try:
        summary.reanalyzed = reanalyze_rule_changes(storage, budget)
    except Exception as exc:  # a repair step must never break the run
        logger.warning("Could not re-analyse earlier changes: %s", exc)
    summary.llm_calls = budget.used
    # ---- notify
    if telegram is not None:
        try:
            summary.alerts_sent = send_alerts(storage, telegram, settings)
            _report_problems(storage, telegram, summary)
        except Exception as exc:
            summary.errors.append(f"Telegram: {type(exc).__name__}: {exc}")
    else:
        storage.mark_notified([c["id"] for c in storage.pending_alerts()])
    if sheets is not None:
        try:
            summary.sheet_rows = sheets.sync(storage)
        except Exception as exc:
            summary.errors.append(f"Google Sheets: {type(exc).__name__}: {exc}")
    summary.seconds = time.perf_counter() - started
    storage.finish_run(run_id, summary.as_dict())
    return summary


def _record_failure(storage: Storage, src: sqlite3.Row, message: str, summary: RunSummary,
                    settings: Settings) -> None:
    failures = storage.mark_checked(src["id"], message)
    summary.errors.append(f"{src['competitor']} {src['type']}: {message}")
    logger.warning("%s %s failed (%d in a row): %s", src["competitor"], src["type"],
                   failures, message)


def send_alerts(storage: Storage, telegram, settings: Settings) -> int:
    """One Telegram message per important new change. Every pending change is
    marked as handled, so nothing is ever alerted twice."""
    pending = storage.pending_alerts()
    sent = 0
    for change in pending:
        if should_alert(change, settings):
            telegram.send_alert(change)
            sent += 1
        storage.mark_notified([change["id"]])
    return sent


def _report_problems(storage: Storage, telegram, summary: RunSummary) -> None:
    """Warn once when a source keeps failing or the Competitors sheet has bad rows."""
    settings = get_settings()
    problems = [f"{s['competitor']} {s['type']} has failed {s['failures']} runs in a row: "
                f"{s['last_error']}"
                for s in storage.sources(active_only=True)
                if s["failures"] == settings.report_after_failures]
    if summary.source_warnings:
        key = hashlib.sha1("\n".join(summary.source_warnings).encode()).hexdigest()
        if storage.get_kv("reported_source_warnings") != key:
            storage.set_kv("reported_source_warnings", key)
            problems += summary.source_warnings
    if problems:
        telegram.send_problems(problems)
