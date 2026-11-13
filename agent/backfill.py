"""History from day one: archived page versions + existing feed posts.

Normally a monitor needs weeks before it has anything to show. The backfill
fetches one archived copy per month of each pricing page from the free
Wayback Machine (archive.org) and runs them through the *same* change
detector, so real price changes from the past year appear immediately.
Feed posts from the last N months are recorded as past changes too.

Archived changes are marked ``origin = archive`` and never trigger alerts.
Run it once, before the first ``run`` (running it again is harmless).
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone

from agent import analyzer
from agent.collector import Collector, FetchError
from agent.config import get_settings
from agent.pipeline import process_feed, process_page
from agent.sources import load_sources
from agent.storage import Storage, now

logger = logging.getLogger(__name__)

CDX = "https://web.archive.org/cdx/search/cdx"


@dataclass
class BackfillSummary:
    snapshots: int = 0
    changes: list[int] = field(default_factory=list)
    posts: int = 0
    errors: list[str] = field(default_factory=list)
    llm_calls: int = 0

    def text(self) -> str:
        lines = [f"Backfill: {self.snapshots} archived page version(s), "
                 f"{len(self.changes)} historical change(s), {self.posts} post(s), "
                 f"{self.llm_calls} AI call(s)."]
        lines += [f"  ! {e}" for e in self.errors]
        return "\n".join(lines)


def _ts(wayback_ts: str) -> int:
    return int(datetime.strptime(wayback_ts[:14], "%Y%m%d%H%M%S")
               .replace(tzinfo=timezone.utc).timestamp())


def wayback_snapshots(collector: Collector, url: str, since: int,
                      until: int | None = None) -> list[tuple[str, str]]:
    """[(timestamp, original_url)], at most one per month, oldest first."""
    params = {"url": url, "output": "json", "fl": "timestamp,original",
              "filter": "statuscode:200", "collapse": "timestamp:6",
              "from": time.strftime("%Y%m%d", time.gmtime(since))}
    if until:
        params["to"] = time.strftime("%Y%m%d%H%M%S", time.gmtime(until))
    query = "&".join(f"{k}={v}" for k, v in params.items())
    resp = collector.get(f"{CDX}?{query}", check_robots=False)
    rows = resp.json() if resp.text.strip() else []
    return [(r[0], r[1]) for r in rows[1:]]


def backfill(storage: Storage | None = None, months: int = 12,
             collector: Collector | None = None, budget: analyzer.Budget | None = None,
             types: tuple[str, ...] = ("pricing",)) -> BackfillSummary:
    from agent.pipeline import open_storage

    settings = get_settings()
    storage = storage or open_storage(settings)
    collector = collector or Collector(delay=max(settings.request_delay, 2.0))
    budget = budget or analyzer.Budget(limit=max(settings.max_llm_calls_per_run, 150))
    summary = BackfillSummary()
    since = now() - months * 30 * 86400

    from agent.notify import sheets as sheets_mod
    sheets = sheets_mod.from_settings() if settings.sources_from_sheet else None
    source_list = load_sources(settings.sources_file, storage,
                               sheets.read_sources if sheets else None)
    sources = storage.sync_sources(source_list.specs)

    for src in sources:
        label = f"{src['competitor']} {src['type']}"
        try:
            if src["format"] in ("rss", "sitemap"):
                items = (collector.fetch_feed(src["url"], 100) if src["format"] == "rss"
                         else collector.fetch_sitemap(src["url"], src["include"], 100))
                before = len(storage.items(src["id"]))
                _, ids = process_feed(storage, src, items, history_since=since, budget=budget)
                summary.posts += len(storage.items(src["id"])) - before
                summary.changes += ids
                logger.info("%-28s %d post(s) stored", label, len(items))
                continue
            if src["type"] not in types:
                continue
            _backfill_page(storage, collector, src, since, budget, summary)
        except FetchError as exc:
            summary.errors.append(f"{label}: {exc}")
            logger.warning("%s: %s", label, exc)
    summary.llm_calls = budget.used
    return summary


def _backfill_page(storage: Storage, collector: Collector, src, since: int,
                   budget: analyzer.Budget, summary: BackfillSummary) -> None:
    label = f"{src['competitor']} {src['type']}"
    earliest = storage.earliest_snapshot(src["id"])
    latest = storage.latest_snapshot(src["id"])
    # older history than anything stored
    until = earliest["captured_at"] - 1 if earliest else None
    if until is None or until > since:
        older = wayback_snapshots(collector, src["url"], since, until)
        logger.info("%-28s %d archived version(s) found", label, len(older))
        _process_versions(storage, collector, src, older, None, budget, summary)
    # an interrupted backfill: continue after the newest archived version
    # (once a live check has run, the live snapshots carry the story on)
    if latest and latest["origin"] == "archive":
        newer = [(ts, o) for ts, o in wayback_snapshots(collector, src["url"], latest["captured_at"])
                 if _ts(ts) > latest["captured_at"]]
        logger.info("%-28s resuming: %d newer archived version(s)", label, len(newer))
        _process_versions(storage, collector, src, newer, latest, budget, summary)
    elif until is not None and until <= since:
        logger.info("%-28s history already collected", label)


def _process_versions(storage: Storage, collector: Collector, src, snapshots: list,
                      previous: dict | None, budget: analyzer.Budget,
                      summary: BackfillSummary) -> None:
    for wb_ts, original in snapshots:
        archive_url = f"https://web.archive.org/web/{wb_ts}id_/{original}"
        try:
            html = collector.get(archive_url, check_robots=False).text
            page = collector.page_from_html(src["url"], html)
        except FetchError as exc:
            logger.info("  skip %s: %s", wb_ts, exc)
            continue
        captured = _ts(wb_ts)
        status, change_id = process_page(
            storage, src, page.title, page.text, captured, origin="archive",
            previous=previous, use_latest=False, budget=budget, notified=True)
        if status != "unchanged":
            summary.snapshots += 1
            previous = storage.latest_snapshot(src["id"], before=captured + 1)
        if change_id:
            summary.changes.append(change_id)
            logger.info("  %s change: %s", time.strftime("%Y-%m-%d", time.gmtime(captured)),
                        storage.change(change_id)["summary"][:110])
