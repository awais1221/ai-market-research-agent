"""Local dashboard: overview, change timeline, diff view, digests and cited Q&A.

Start it with ``python -m agent serve`` and open http://127.0.0.1:8000.
It binds to localhost only; it is a private tool, not a public website.
"""

from __future__ import annotations

import json
import logging
import threading
from functools import lru_cache
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from agent import llm_client, rag
from agent.config import get_settings
from agent.differ import side_by_side
from agent.indexer import sync_index
from agent.pipeline import open_storage
from agent.storage import Storage

logger = logging.getLogger(__name__)
STATIC = Path(__file__).parent / "static"

app = FastAPI(title="AI Market Research Agent", version="1.0")
app.mount("/static", StaticFiles(directory=STATIC), name="static")

_index_lock = threading.Lock()


@lru_cache
def get_storage() -> Storage:
    return open_storage()


def get_vector_store():
    from agent.vectorstore import get_store
    return get_store()


def ensure_index() -> None:
    """Embed anything new in the database (cheap when nothing changed)."""
    with _index_lock:
        sync_index(get_storage(), get_vector_store())


@app.on_event("startup")
def _warm_up() -> None:
    import os
    if os.getenv("WARMUP", "1") != "0":
        threading.Thread(target=_safe_sync, daemon=True).start()


def _safe_sync() -> None:
    try:
        ensure_index()
    except Exception:
        logger.exception("Index warm-up failed")


# ------------------------------------------------------------------ pages
@app.get("/", include_in_schema=False)
def index() -> FileResponse:
    return FileResponse(STATIC / "index.html")


@app.get("/health")
def health() -> dict:
    return {"status": "ok", "llm_configured": get_settings().llm_configured}


# ------------------------------------------------------------------ data
def _plans(plans) -> list[dict]:
    return [p.model_dump() if hasattr(p, "model_dump") else p for p in plans]


@app.get("/api/overview")
def overview() -> dict:
    storage = get_storage()
    counts = storage.change_counts()
    pricing = {p["competitor"]: p for p in storage.latest_pricing()}
    competitors: dict[str, dict] = {}
    for src in storage.sources(active_only=True):
        entry = competitors.setdefault(src["competitor"], {
            "name": src["competitor"], "sources": [],
            **counts.get(src["competitor"], {"changes": 0, "high": 0, "last_change": None}),
            "plans": [], "pricing_url": "", "pricing_date": None})
        entry["sources"].append({"id": src["id"], "type": src["type"], "url": src["url"],
                                 "format": src["format"], "last_checked": src["last_checked"],
                                 "last_error": src["last_error"]})
        if src["competitor"] in pricing:
            p = pricing[src["competitor"]]
            entry.update(plans=_plans(p["plans"]), pricing_url=p["url"],
                         pricing_date=p["captured_at"])
    last = storage.last_run()
    return {"competitors": list(competitors.values()),
            "last_run": last and {"at": last["started_at"], **last["summary"]},
            "settings": {"llm": get_settings().llm_configured,
                         "telegram": get_settings().telegram_configured,
                         "sheets": get_settings().sheets_configured}}


@app.get("/api/changes")
def list_changes(competitor: str | None = None, importance: str | None = None,
                 kind: str | None = None, limit: int = Query(100, ge=1, le=500),
                 offset: int = Query(0, ge=0)) -> dict:
    levels = [x for x in (importance or "").split(",") if x in ("high", "medium", "low")]
    rows = get_storage().changes(competitor=competitor or None, importance=levels or None,
                                 kind=kind or None, limit=limit, offset=offset)
    return {"changes": [_change_out(c) for c in rows]}


def _change_out(c: dict) -> dict:
    keep = ("id", "competitor", "source_type", "kind", "effective_at", "detected_at",
            "summary", "importance", "category", "why", "origin", "url", "pricing_changes",
            "item_title", "analyzed_by")
    return {k: c.get(k) for k in keep}


@app.get("/api/changes/{change_id}")
def change_detail(change_id: int) -> dict:
    storage = get_storage()
    c = storage.change(change_id)
    if c is None:
        raise HTTPException(404, "Change not found")
    out = _change_out(c)
    old = storage.snapshot(c["old_snapshot_id"])
    new = storage.snapshot(c["new_snapshot_id"])
    out["diff"] = side_by_side(old["text"], new["text"]) if old and new else [
        {"op": "added" if line.startswith("+ ") else "equal", "text": line[2:] if
         line.startswith("+ ") else line} for line in c["diff_text"].splitlines()]
    out["old_plans"] = _plans(storage.pricing_for_snapshot(old["id"]) or []) if old else []
    out["new_plans"] = _plans(storage.pricing_for_snapshot(new["id"]) or []) if new else []
    out["old_date"] = old["captured_at"] if old else None
    out["new_date"] = new["captured_at"] if new else None
    return out


@app.get("/api/digests")
def list_digests() -> dict:
    return {"digests": get_storage().digests()}


# ------------------------------------------------------------------ Q&A
class AskRequest(BaseModel):
    question: str = Field(min_length=1, max_length=500)
    history: list[rag.ChatTurn] = Field(default_factory=list, max_length=12)


def _ask_errors(exc: Exception) -> HTTPException:
    if isinstance(exc, llm_client.LLMNotConfiguredError):
        return HTTPException(503, str(exc))
    return HTTPException(502, str(exc))


@app.post("/api/ask")
def ask(req: AskRequest) -> rag.Answer:
    ensure_index()
    try:
        return rag.answer(req.question.strip(), get_storage(), get_vector_store(), req.history)
    except (llm_client.LLMError, llm_client.LLMNotConfiguredError) as exc:
        raise _ask_errors(exc) from exc


@app.post("/api/ask/stream")
def ask_stream(req: AskRequest) -> StreamingResponse:
    ensure_index()

    def events():
        try:
            for event in rag.stream_answer(req.question.strip(), get_storage(),
                                           get_vector_store(), req.history):
                yield f"data: {json.dumps(event)}\n\n"
        except (llm_client.LLMError, llm_client.LLMNotConfiguredError) as exc:
            yield f"data: {json.dumps({'type': 'error', 'message': str(exc)})}\n\n"

    return StreamingResponse(events(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})
