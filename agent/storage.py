"""SQLite storage: the agent's long-term memory.

Everything (sources, page snapshots, feed items, detected changes, pricing
tables, digests, LLM cache) lives in one small file, ``data/agent.db``. The
vector index is rebuilt from it, and GitHub Actions only has to keep this file.
Snapshot text is zlib-compressed to keep the file small.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
import zlib
from pathlib import Path

from agent.models import PricingPlan, SourceSpec

SCHEMA = """
CREATE TABLE IF NOT EXISTS sources (
    id INTEGER PRIMARY KEY,
    competitor TEXT NOT NULL,
    type TEXT NOT NULL,
    url TEXT NOT NULL,
    format TEXT NOT NULL DEFAULT 'html',
    render INTEGER NOT NULL DEFAULT 0,
    ignore_json TEXT NOT NULL DEFAULT '[]',
    include TEXT NOT NULL DEFAULT '',
    active INTEGER NOT NULL DEFAULT 1,
    last_checked INTEGER,
    last_error TEXT,
    failures INTEGER NOT NULL DEFAULT 0,
    UNIQUE(competitor, url)
);
CREATE TABLE IF NOT EXISTS snapshots (
    id INTEGER PRIMARY KEY,
    source_id INTEGER NOT NULL REFERENCES sources(id),
    captured_at INTEGER NOT NULL,
    content_hash TEXT NOT NULL,
    title TEXT NOT NULL DEFAULT '',
    text_z BLOB NOT NULL,
    origin TEXT NOT NULL DEFAULT 'live'          -- live | archive
);
CREATE INDEX IF NOT EXISTS ix_snap_source ON snapshots(source_id, captured_at);
CREATE TABLE IF NOT EXISTS items (
    id INTEGER PRIMARY KEY,
    source_id INTEGER NOT NULL REFERENCES sources(id),
    guid TEXT NOT NULL,
    title TEXT NOT NULL,
    url TEXT NOT NULL DEFAULT '',
    published_at INTEGER,
    summary TEXT NOT NULL DEFAULT '',
    first_seen INTEGER NOT NULL,
    UNIQUE(source_id, guid)
);
CREATE TABLE IF NOT EXISTS changes (
    id INTEGER PRIMARY KEY,
    source_id INTEGER NOT NULL REFERENCES sources(id),
    kind TEXT NOT NULL,                  -- page_change | pricing_change | new_item
    detected_at INTEGER NOT NULL,        -- when the agent noticed it
    effective_at INTEGER NOT NULL,       -- when it happened (archive date / publish date)
    old_snapshot_id INTEGER,
    new_snapshot_id INTEGER,
    item_id INTEGER,
    dedupe_key TEXT NOT NULL UNIQUE,     -- the same change is never stored twice
    diff_text TEXT NOT NULL DEFAULT '',
    pricing_json TEXT NOT NULL DEFAULT '[]',
    summary TEXT NOT NULL DEFAULT '',
    importance TEXT NOT NULL DEFAULT 'low',
    category TEXT NOT NULL DEFAULT 'other',
    why TEXT NOT NULL DEFAULT '',
    analyzed_by TEXT NOT NULL DEFAULT '', -- llm | rules
    origin TEXT NOT NULL DEFAULT 'live',
    notified INTEGER NOT NULL DEFAULT 0,
    sheet_synced INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS ix_changes_time ON changes(effective_at);
CREATE TABLE IF NOT EXISTS pricing (
    id INTEGER PRIMARY KEY,
    source_id INTEGER NOT NULL REFERENCES sources(id),
    snapshot_id INTEGER NOT NULL REFERENCES snapshots(id) UNIQUE,
    captured_at INTEGER NOT NULL,
    plans_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS digests (
    id INTEGER PRIMARY KEY,
    created_at INTEGER NOT NULL,
    period_start INTEGER NOT NULL,
    period_end INTEGER NOT NULL,
    markdown TEXT NOT NULL,
    change_ids TEXT NOT NULL DEFAULT '[]',
    sent INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY,
    started_at INTEGER NOT NULL,
    finished_at INTEGER,
    kind TEXT NOT NULL,
    summary_json TEXT NOT NULL DEFAULT '{}'
);
CREATE TABLE IF NOT EXISTS kv (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


def _z(text: str) -> bytes:
    return zlib.compress(text.encode("utf-8"), 9)


def _unz(blob: bytes) -> str:
    return zlib.decompress(blob).decode("utf-8")


def now() -> int:
    return int(time.time())


class Storage:
    def __init__(self, path: str):
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self._lock = threading.RLock()
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    def _q(self, sql: str, params: tuple | list = ()) -> list[sqlite3.Row]:
        with self._lock:
            return self.conn.execute(sql, params).fetchall()

    def _x(self, sql: str, params: tuple | list = ()) -> int:
        with self._lock:
            cur = self.conn.execute(sql, params)
            self.conn.commit()
            return cur.lastrowid

    # ------------------------------------------------------------ kv
    def get_kv(self, key: str, default: str | None = None) -> str | None:
        rows = self._q("SELECT value FROM kv WHERE key = ?", (key,))
        return rows[0]["value"] if rows else default

    def set_kv(self, key: str, value: str) -> None:
        self._x("INSERT INTO kv(key, value) VALUES(?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value", (key, value))

    # ------------------------------------------------------------ sources
    def sync_sources(self, specs: list[SourceSpec]) -> list[sqlite3.Row]:
        """Upsert the configured sources; sources no longer listed become
        inactive (their history is kept)."""
        keep: set[int] = set()
        with self._lock:
            for s in specs:
                self.conn.execute(
                    """INSERT INTO sources(competitor, type, url, format, render, ignore_json,
                                           include, active)
                       VALUES(?, ?, ?, ?, ?, ?, ?, ?)
                       ON CONFLICT(competitor, url) DO UPDATE SET
                         type = excluded.type, format = excluded.format,
                         render = excluded.render, ignore_json = excluded.ignore_json,
                         include = excluded.include, active = excluded.active""",
                    (s.competitor, s.type, s.url, s.format, int(s.render),
                     json.dumps(s.ignore), s.include, int(s.active)))
                row = self.conn.execute("SELECT id FROM sources WHERE competitor = ? AND url = ?",
                                        (s.competitor, s.url)).fetchone()
                keep.add(row["id"])
            if keep:
                marks = ",".join("?" * len(keep))
                self.conn.execute(f"UPDATE sources SET active = 0 WHERE id NOT IN ({marks})",
                                  tuple(keep))
            self.conn.commit()
        return self.sources(active_only=True)

    def sources(self, active_only: bool = False) -> list[sqlite3.Row]:
        sql = "SELECT * FROM sources" + (" WHERE active = 1" if active_only else "")
        return self._q(sql + " ORDER BY competitor, id")

    def source(self, source_id: int) -> sqlite3.Row | None:
        rows = self._q("SELECT * FROM sources WHERE id = ?", (source_id,))
        return rows[0] if rows else None

    def competitors(self) -> list[str]:
        return [r["competitor"] for r in
                self._q("SELECT DISTINCT competitor FROM sources ORDER BY competitor")]

    def mark_checked(self, source_id: int, error: str | None = None) -> int:
        """Record a check. Returns the number of consecutive failures."""
        if error:
            self._x("UPDATE sources SET last_checked = ?, last_error = ?, "
                    "failures = failures + 1 WHERE id = ?", (now(), error[:500], source_id))
        else:
            self._x("UPDATE sources SET last_checked = ?, last_error = NULL, failures = 0 "
                    "WHERE id = ?", (now(), source_id))
        return self.source(source_id)["failures"]

    # ------------------------------------------------------------ snapshots
    def add_snapshot(self, source_id: int, text: str, content_hash: str, title: str,
                     captured_at: int | None = None, origin: str = "live") -> int:
        return self._x(
            "INSERT INTO snapshots(source_id, captured_at, content_hash, title, text_z, origin) "
            "VALUES(?, ?, ?, ?, ?, ?)",
            (source_id, captured_at or now(), content_hash, title, _z(text), origin))

    def _snap(self, row: sqlite3.Row | None) -> dict | None:
        if row is None:
            return None
        data = dict(row)
        data["text"] = _unz(data.pop("text_z"))
        return data

    def latest_snapshot(self, source_id: int, before: int | None = None) -> dict | None:
        if before is None:
            rows = self._q("SELECT * FROM snapshots WHERE source_id = ? "
                           "ORDER BY captured_at DESC, id DESC LIMIT 1", (source_id,))
        else:
            rows = self._q("SELECT * FROM snapshots WHERE source_id = ? AND captured_at < ? "
                           "ORDER BY captured_at DESC, id DESC LIMIT 1", (source_id, before))
        return self._snap(rows[0] if rows else None)

    def earliest_snapshot(self, source_id: int) -> dict | None:
        rows = self._q("SELECT * FROM snapshots WHERE source_id = ? "
                       "ORDER BY captured_at ASC, id ASC LIMIT 1", (source_id,))
        return self._snap(rows[0] if rows else None)

    def snapshot(self, snapshot_id: int | None) -> dict | None:
        if snapshot_id is None:
            return None
        rows = self._q("SELECT * FROM snapshots WHERE id = ?", (snapshot_id,))
        return self._snap(rows[0] if rows else None)

    # ------------------------------------------------------------ pricing
    def save_pricing(self, source_id: int, snapshot_id: int, captured_at: int,
                     plans: list[PricingPlan]) -> None:
        self._x("INSERT OR REPLACE INTO pricing(source_id, snapshot_id, captured_at, plans_json) "
                "VALUES(?, ?, ?, ?)",
                (source_id, snapshot_id, captured_at,
                 json.dumps([p.model_dump() for p in plans])))

    def pricing_for_snapshot(self, snapshot_id: int) -> list[PricingPlan] | None:
        rows = self._q("SELECT plans_json FROM pricing WHERE snapshot_id = ?", (snapshot_id,))
        if not rows:
            return None
        return [PricingPlan(**p) for p in json.loads(rows[0]["plans_json"])]

    def latest_pricing(self) -> list[dict]:
        """Newest pricing table per active pricing source."""
        rows = self._q(
            """SELECT s.id AS source_id, s.competitor, s.url, p.captured_at, p.plans_json
               FROM pricing p JOIN sources s ON s.id = p.source_id
               WHERE s.active = 1 AND p.captured_at = (
                   SELECT MAX(captured_at) FROM pricing WHERE source_id = p.source_id)
               ORDER BY s.competitor""")
        return [{"source_id": r["source_id"], "competitor": r["competitor"], "url": r["url"],
                 "captured_at": r["captured_at"],
                 "plans": [PricingPlan(**p) for p in json.loads(r["plans_json"])]}
                for r in rows]

    def pricing_history(self, source_id: int) -> list[dict]:
        rows = self._q("SELECT captured_at, plans_json FROM pricing WHERE source_id = ? "
                       "ORDER BY captured_at", (source_id,))
        return [{"captured_at": r["captured_at"], "plans": json.loads(r["plans_json"])}
                for r in rows]

    # ------------------------------------------------------------ items
    def has_items(self, source_id: int) -> bool:
        return bool(self._q("SELECT 1 FROM items WHERE source_id = ? LIMIT 1", (source_id,)))

    def add_item(self, source_id: int, guid: str, title: str, url: str,
                 published_at: int | None, summary: str) -> int | None:
        """Insert a feed item. Returns its id, or None if it was already known."""
        with self._lock:
            cur = self.conn.execute(
                "INSERT OR IGNORE INTO items(source_id, guid, title, url, published_at, summary, "
                "first_seen) VALUES(?, ?, ?, ?, ?, ?, ?)",
                (source_id, guid, title, url, published_at, summary, now()))
            self.conn.commit()
            return cur.lastrowid if cur.rowcount else None

    def item(self, item_id: int | None) -> sqlite3.Row | None:
        if item_id is None:
            return None
        rows = self._q("SELECT * FROM items WHERE id = ?", (item_id,))
        return rows[0] if rows else None

    def items(self, source_id: int | None = None) -> list[sqlite3.Row]:
        sql = ("SELECT i.*, s.competitor, s.type FROM items i "
               "JOIN sources s ON s.id = i.source_id")
        if source_id is None:
            return self._q(sql + " ORDER BY COALESCE(i.published_at, i.first_seen) DESC")
        return self._q(sql + " WHERE i.source_id = ? "
                             "ORDER BY COALESCE(i.published_at, i.first_seen) DESC", (source_id,))

    # ------------------------------------------------------------ changes
    def add_change(self, *, source_id: int, kind: str, dedupe_key: str, effective_at: int,
                   old_snapshot_id: int | None = None, new_snapshot_id: int | None = None,
                   item_id: int | None = None, diff_text: str = "",
                   pricing_changes: list[dict] | None = None, origin: str = "live",
                   notified: bool = False) -> int | None:
        """Store a change. Returns its id, or None if it was already stored."""
        with self._lock:
            cur = self.conn.execute(
                """INSERT OR IGNORE INTO changes(source_id, kind, detected_at, effective_at,
                       old_snapshot_id, new_snapshot_id, item_id, dedupe_key, diff_text,
                       pricing_json, origin, notified)
                   VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (source_id, kind, now(), effective_at, old_snapshot_id, new_snapshot_id,
                 item_id, dedupe_key, diff_text, json.dumps(pricing_changes or []), origin,
                 int(notified)))
            self.conn.commit()
            return cur.lastrowid if cur.rowcount else None

    def set_analysis(self, change_id: int, summary: str, importance: str, category: str,
                     why: str, analyzed_by: str) -> None:
        self._x("UPDATE changes SET summary = ?, importance = ?, category = ?, why = ?, "
                "analyzed_by = ? WHERE id = ?",
                (summary, importance, category, why, analyzed_by, change_id))

    _CHANGE_SQL = """SELECT c.*, s.competitor, s.type AS source_type, s.url AS source_url,
                            i.title AS item_title, i.url AS item_url
                     FROM changes c JOIN sources s ON s.id = c.source_id
                     LEFT JOIN items i ON i.id = c.item_id"""

    @staticmethod
    def _change(row: sqlite3.Row) -> dict:
        data = dict(row)
        data["pricing_changes"] = json.loads(data.pop("pricing_json") or "[]")
        data["url"] = data.get("item_url") or data["source_url"]
        return data

    def change(self, change_id: int) -> dict | None:
        rows = self._q(self._CHANGE_SQL + " WHERE c.id = ?", (change_id,))
        return self._change(rows[0]) if rows else None

    def changes(self, *, competitor: str | None = None, since: int | None = None,
                until: int | None = None, importance: list[str] | None = None,
                kind: str | None = None, limit: int = 200, offset: int = 0) -> list[dict]:
        where, params = [], []
        if competitor:
            where.append("s.competitor = ?")
            params.append(competitor)
        if since is not None:
            where.append("c.effective_at >= ?")
            params.append(since)
        if until is not None:
            where.append("c.effective_at < ?")
            params.append(until)
        if importance:
            where.append(f"c.importance IN ({','.join('?' * len(importance))})")
            params += importance
        if kind:
            where.append("c.kind = ?")
            params.append(kind)
        sql = self._CHANGE_SQL + (" WHERE " + " AND ".join(where) if where else "")
        sql += " ORDER BY c.effective_at DESC, c.id DESC LIMIT ? OFFSET ?"
        return [self._change(r) for r in self._q(sql, params + [limit, offset])]

    def rule_analyzed(self, limit: int = 20) -> list[dict]:
        """Newest changes that got the rule-based fallback instead of an AI summary."""
        rows = self._q(self._CHANGE_SQL + " WHERE c.analyzed_by = 'rules' "
                                          "ORDER BY c.effective_at DESC, c.id DESC LIMIT ?",
                       (limit,))
        return [self._change(r) for r in rows]

    def pending_alerts(self) -> list[dict]:
        rows = self._q(self._CHANGE_SQL + " WHERE c.notified = 0 AND c.origin = 'live' "
                                          "ORDER BY c.effective_at, c.id")
        return [self._change(r) for r in rows]

    def mark_notified(self, change_ids: list[int]) -> None:
        if change_ids:
            self._x(f"UPDATE changes SET notified = 1 WHERE id IN "
                    f"({','.join('?' * len(change_ids))})", change_ids)

    def unsynced_changes(self) -> list[dict]:
        rows = self._q(self._CHANGE_SQL + " WHERE c.sheet_synced = 0 "
                                          "ORDER BY c.effective_at, c.id")
        return [self._change(r) for r in rows]

    def mark_synced(self, change_ids: list[int]) -> None:
        if change_ids:
            self._x(f"UPDATE changes SET sheet_synced = 1 WHERE id IN "
                    f"({','.join('?' * len(change_ids))})", change_ids)

    def change_counts(self) -> dict[str, dict]:
        rows = self._q("""SELECT s.competitor, COUNT(c.id) AS n, MAX(c.effective_at) AS last,
                                 SUM(c.importance = 'high') AS high
                          FROM sources s LEFT JOIN changes c ON c.source_id = s.id
                          GROUP BY s.competitor""")
        return {r["competitor"]: {"changes": r["n"], "last_change": r["last"],
                                  "high": r["high"] or 0} for r in rows}

    # ------------------------------------------------------------ digests
    def add_digest(self, period_start: int, period_end: int, markdown: str,
                   change_ids: list[int]) -> int:
        return self._x("INSERT INTO digests(created_at, period_start, period_end, markdown, "
                       "change_ids) VALUES(?, ?, ?, ?, ?)",
                       (now(), period_start, period_end, markdown, json.dumps(change_ids)))

    def mark_digest_sent(self, digest_id: int) -> None:
        self._x("UPDATE digests SET sent = 1 WHERE id = ?", (digest_id,))

    def digests(self, limit: int = 20) -> list[dict]:
        return [dict(r) for r in
                self._q("SELECT * FROM digests ORDER BY created_at DESC LIMIT ?", (limit,))]

    # ------------------------------------------------------------ runs
    def start_run(self, kind: str) -> int:
        return self._x("INSERT INTO runs(started_at, kind) VALUES(?, ?)", (now(), kind))

    def finish_run(self, run_id: int, summary: dict) -> None:
        self._x("UPDATE runs SET finished_at = ?, summary_json = ? WHERE id = ?",
                (now(), json.dumps(summary), run_id))

    def last_run(self, kind: str = "run") -> dict | None:
        rows = self._q("SELECT * FROM runs WHERE kind = ? AND finished_at IS NOT NULL "
                       "ORDER BY id DESC LIMIT 1", (kind,))
        if not rows:
            return None
        data = dict(rows[0])
        data["summary"] = json.loads(data.pop("summary_json"))
        return data
