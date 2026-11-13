"""Which competitors and pages to watch.

Two ways to configure the list:

1. ``sources.yaml`` in the repo (default)
2. A **"Competitors" tab in the client's Google Sheet** (``SOURCES_FROM_SHEET=true``):
   one row per page, so the client can add, pause or remove competitors
   without touching code. Invalid rows are skipped and reported. If the Sheet
   can't be read, the last good list (cached in SQLite) is used, then the YAML.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

import yaml
from pydantic import ValidationError

from agent.models import SourceSpec

logger = logging.getLogger(__name__)

SHEET_COLUMNS = ["competitor", "type", "url", "format", "render", "ignore", "include", "active"]
_CACHE_KEY = "sheet_sources_cache"


@dataclass
class SourceList:
    specs: list[SourceSpec]
    origin: str                                       # yaml | sheet | sheet-cache
    errors: list[str] = field(default_factory=list)   # human-readable row problems


def _error_text(exc: ValidationError) -> str:
    parts = []
    for err in exc.errors():
        loc = ".".join(str(x) for x in err["loc"]) or "row"
        parts.append(f"{loc}: {err['msg']}")
    return "; ".join(parts)


def load_yaml(path: str | Path) -> SourceList:
    path = Path(path)
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    specs, errors = [], []
    for c_index, competitor in enumerate(data.get("competitors") or [], start=1):
        name = (competitor or {}).get("name", "")
        for s_index, source in enumerate(competitor.get("sources") or [], start=1):
            try:
                specs.append(SourceSpec(competitor=name, **source))
            except (ValidationError, TypeError) as exc:
                detail = _error_text(exc) if isinstance(exc, ValidationError) else str(exc)
                errors.append(f"{name or f'competitor {c_index}'} source {s_index}: {detail}")
    return SourceList(_dedupe(specs), "yaml", errors)


def _cell_bool(value, default: bool) -> bool:
    text = str(value).strip().lower()
    if not text:
        return default
    return text in ("1", "true", "yes", "y", "on", "x", "✓")


def parse_rows(rows: list[dict]) -> tuple[list[SourceSpec], list[str]]:
    """Validate Competitors-sheet rows. Row numbers in errors match the sheet
    (row 1 is the header)."""
    specs, errors = [], []
    for number, row in enumerate(rows, start=2):
        row = {str(k).strip().lower(): v for k, v in row.items()}
        if not any(str(v).strip() for v in row.values()):
            continue  # blank row
        try:
            ignore = [p.strip() for p in str(row.get("ignore", "")).split("|") if p.strip()]
            specs.append(SourceSpec(
                competitor=str(row.get("competitor", "")).strip(),
                type=str(row.get("type", "") or "page").strip().lower(),
                url=str(row.get("url", "")).strip(),
                format=str(row.get("format", "") or "html").strip().lower(),
                render=_cell_bool(row.get("render", ""), False),
                ignore=ignore,
                include=str(row.get("include", "")).strip(),
                active=_cell_bool(row.get("active", ""), True),
            ))
        except ValidationError as exc:
            errors.append(f"Row {number}: {_error_text(exc)}")
    return _dedupe(specs), errors


def _dedupe(specs: list[SourceSpec]) -> list[SourceSpec]:
    seen, out = set(), []
    for s in specs:
        key = (s.competitor.lower(), s.url)
        if key not in seen:
            seen.add(key)
            out.append(s)
    return out


def specs_to_rows(specs: list[SourceSpec]) -> list[list[str]]:
    """Rows for filling the Competitors sheet from sources.yaml."""
    return [[s.competitor, s.type, s.url, s.format, "yes" if s.render else "",
             " | ".join(s.ignore), s.include, "yes" if s.active else "no"] for s in specs]


def load_sources(sources_file: str, storage=None, sheet_reader: Callable[[], list[dict]] | None = None
                 ) -> SourceList:
    """The list to monitor this run: Sheet → cached Sheet → sources.yaml."""
    if sheet_reader is not None:
        try:
            specs, errors = parse_rows(sheet_reader())
            if specs:
                if storage is not None:
                    storage.set_kv(_CACHE_KEY, json.dumps([s.model_dump() for s in specs]))
                return SourceList(specs, "sheet", errors)
            errors.append("The Competitors sheet has no valid rows.")
            logger.warning("Competitors sheet has no valid rows; falling back")
        except Exception as exc:  # network / auth / missing tab
            logger.warning("Could not read the Competitors sheet (%s); falling back",
                           type(exc).__name__)
            errors = [f"Could not read the Competitors sheet ({type(exc).__name__})."]
        if storage is not None and (cached := storage.get_kv(_CACHE_KEY)):
            specs = [SourceSpec(**s) for s in json.loads(cached)]
            return SourceList(specs, "sheet-cache", errors)
        result = load_yaml(sources_file)
        result.errors = errors + result.errors
        return result
    return load_yaml(sources_file)
