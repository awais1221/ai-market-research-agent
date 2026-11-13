"""Builds the RAG knowledge base from the SQLite database.

What gets indexed (each with competitor / type / date metadata):

* ``page:<source>``: the **latest** version of every monitored page. It is
  replaced when the page changes, so answers never use stale text.
* ``plans:<source>``: the latest extracted pricing table, as readable text.
* ``post:<item>``: every blog / changelog / sitemap item (+ its AI summary).
* ``change:<id>``: every detected page or pricing change: what changed, when,
  the exact old → new prices. This is how history stays searchable.

``sync_index`` is incremental and idempotent: it compares content hashes with
what is already in Chroma and only embeds new or changed documents, so it can
run before every question. ``reindex`` rebuilds everything from SQLite.
"""

from __future__ import annotations

import hashlib
import logging
import time

from agent.chunker import chunk_documents
from agent.config import get_settings
from agent.differ import format_plans
from agent.models import Document

logger = logging.getLogger(__name__)

CURRENT_TYPES = ["pricing", "changelog", "blog", "page", "news", "plans"]


def _date(ts: int) -> str:
    return time.strftime("%d %b %Y", time.gmtime(ts))


def _doc(doc_id: str, text: str, **kw) -> Document:
    return Document(text=text, doc_id=doc_id,
                    content_hash=hashlib.sha1(text.encode("utf-8")).hexdigest(), **kw)


def build_documents(storage) -> list[Document]:
    docs: list[Document] = []
    sources = {s["id"]: s for s in storage.sources(active_only=True)}

    for src in sources.values():
        if src["format"] != "html":
            continue
        snap = storage.latest_snapshot(src["id"])
        if snap is None:
            continue
        since = snap["captured_at"]
        docs.append(_doc(
            f"page:{src['id']}",
            f"# {src['competitor']} {src['type']} page (current version, unchanged since "
            f"{_date(since)})\n{snap['text']}",
            title=f"{src['competitor']} {src['type']} page", url=src["url"],
            competitor=src["competitor"], doc_type=src["type"], date_ts=since))

    for entry in storage.latest_pricing():
        if entry["source_id"] not in sources:
            continue
        docs.append(_doc(
            f"plans:{entry['source_id']}",
            f"# {entry['competitor']} current pricing plans (as of "
            f"{_date(entry['captured_at'])})\n{format_plans(entry['plans'])}",
            title=f"{entry['competitor']} pricing plans", url=entry["url"],
            competitor=entry["competitor"], doc_type="plans", date_ts=entry["captured_at"]))

    analyses = {c["item_id"]: c for c in storage.changes(kind="new_item", limit=100000)}
    for item in storage.items():
        if item["source_id"] not in sources:
            continue
        when = item["published_at"] or item["first_seen"]
        change = analyses.get(item["id"])
        text = f"# {item['competitor']} {item['type']} post: {item['title']}\n" \
               f"Published {_date(when)}.\n"
        if change and change["summary"] and change["summary"] != item["title"]:
            text += f"Summary: {change['summary']}\n"
        text += item["summary"][:2500]
        docs.append(_doc(f"post:{item['id']}", text, title=item["title"],
                         url=item["url"] or sources[item["source_id"]]["url"],
                         competitor=item["competitor"], doc_type="post", date_ts=when))

    for c in storage.changes(limit=100000):
        if c["kind"] == "new_item" or c["source_id"] not in sources:
            continue
        lines = [f"# {c['competitor']} {c['source_type']} page changed on "
                 f"{_date(c['effective_at'])}",
                 f"Importance: {c['importance']}. Category: {c['category']}.",
                 f"Summary: {c['summary']}"]
        if c["pricing_changes"]:
            lines.append("Exact pricing changes: "
                         + "; ".join(p["text"] for p in c["pricing_changes"]))
        if c["why"]:
            lines.append(f"Why it matters: {c['why']}")
        lines.append("Text that changed:\n" + c["diff_text"][:1500])
        docs.append(_doc(f"change:{c['id']}", "\n".join(lines),
                         title=f"{c['competitor']} {c['source_type']} change, "
                               f"{_date(c['effective_at'])}",
                         url=c["url"], competitor=c["competitor"], doc_type="change",
                         date_ts=c["effective_at"]))
    return docs


def sync_index(storage, store=None) -> dict:
    """Embed new / changed documents, delete removed ones."""
    from agent.vectorstore import get_store

    store = store or get_store()
    s = get_settings()
    wanted = {d.doc_id: d for d in build_documents(storage)}
    indexed = store.indexed_docs()
    added = removed = 0
    for doc_id, doc in wanted.items():
        if indexed.get(doc_id) != doc.content_hash:
            store.replace_doc(doc_id, chunk_documents([doc], s.chunk_size, s.chunk_overlap))
            added += 1
    for doc_id in set(indexed) - set(wanted):
        store.delete_doc(doc_id)
        removed += 1
    if added or removed:
        logger.info("Index updated: %d document(s) added/updated, %d removed", added, removed)
    return {"documents": len(wanted), "updated": added, "removed": removed}


def reindex(storage, store=None) -> dict:
    from agent.vectorstore import get_store

    store = store or get_store()
    store.reset()
    return sync_index(storage, store)
