"""Data models shared across the agent."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Literal

from pydantic import BaseModel, Field, field_validator

SourceType = Literal["pricing", "changelog", "blog", "page", "news"]
SourceFormat = Literal["html", "rss", "sitemap"]
Importance = Literal["high", "medium", "low"]
Category = Literal["pricing", "feature", "announcement", "other"]

IMPORTANCE_RANK = {"low": 0, "medium": 1, "high": 2}


# ------------------------------------------------------------------ sources
class SourceSpec(BaseModel):
    """One page or feed to watch (a row in sources.yaml or the Competitors sheet)."""

    competitor: str = Field(min_length=1, max_length=80)
    type: SourceType = "page"
    url: str = Field(min_length=8, max_length=2000)
    format: SourceFormat = "html"
    render: bool = False            # needs a real browser (JavaScript-built page)
    ignore: list[str] = Field(default_factory=list)  # regexes: matching lines are noise
    include: str = ""               # sitemap only: regex a URL must match (e.g. /blog/)
    active: bool = True

    @field_validator("competitor")
    @classmethod
    def _strip(cls, v: str) -> str:
        return v.strip()

    @field_validator("url")
    @classmethod
    def _check_url(cls, v: str) -> str:
        v = v.strip()
        if not re.match(r"^https?://[^\s/]+\.[^\s]+$", v):
            raise ValueError("must be a full http(s) web address")
        return v

    @field_validator("ignore")
    @classmethod
    def _check_regexes(cls, v: list[str]) -> list[str]:
        for pattern in v:
            try:
                re.compile(pattern)
            except re.error as exc:
                raise ValueError(f"invalid ignore pattern {pattern!r}: {exc}") from exc
        return v

    @field_validator("include")
    @classmethod
    def _check_include(cls, v: str) -> str:
        if v:
            re.compile(v)
        return v


# ------------------------------------------------------------------ pricing
class PricingPlan(BaseModel):
    name: str
    price: float | None = None      # None = free-form ("Contact sales", "Custom")
    currency: str = "USD"
    period: str = "month"           # month | year | one-time | usage
    billing: str | None = None      # monthly | annual (how the shown price is billed)
    limits: str = ""                # key limits, e.g. "750 tasks/month, 2 users"

    @field_validator("name", mode="before")
    @classmethod
    def _name(cls, v) -> str:
        return re.sub(r"\s+", " ", str(v or "")).strip()

    # Models are not perfectly consistent: accept "$20", "Custom", null, lists...
    @field_validator("price", mode="before")
    @classmethod
    def _price(cls, v):
        if v is None or isinstance(v, (int, float)):
            return v
        match = re.search(r"\d+(?:[.,]\d+)?", str(v).replace(",", ""))
        return float(match.group()) if match else None

    @field_validator("currency", "period", mode="before")
    @classmethod
    def _defaults(cls, v, info):
        if v is None or str(v).strip() == "":
            return "USD" if info.field_name == "currency" else "month"
        return str(v).strip()

    @field_validator("limits", mode="before")
    @classmethod
    def _limits(cls, v):
        if v is None:
            return ""
        if isinstance(v, (list, tuple)):
            return ", ".join(str(x) for x in v)
        if isinstance(v, dict):
            return ", ".join(f"{k}: {val}" for k, val in v.items())
        return str(v)

    @field_validator("billing", mode="before")
    @classmethod
    def _billing(cls, v):
        return None if v in (None, "", "null") else str(v)


class PricingTable(BaseModel):
    plans: list[PricingPlan] = Field(default_factory=list)


# ------------------------------------------------------------------ analysis
class Analysis(BaseModel):
    summary: str
    importance: Importance = "low"
    category: Category = "other"
    why_it_matters: str = ""


# ------------------------------------------------------------------ RAG
@dataclass
class Document:
    text: str
    doc_id: str                     # "page:3", "change:17", "item:42"
    title: str
    url: str
    competitor: str
    doc_type: str                   # pricing | changelog | blog | page | news | change
    date_ts: int                    # unix time the content is "about"
    content_hash: str = ""


@dataclass
class Chunk:
    text: str
    doc_id: str
    title: str
    url: str
    competitor: str
    doc_type: str
    date_ts: int
    chunk_index: int
    content_hash: str = ""
    heading: str = ""


@dataclass
class SearchHit:
    text: str
    doc_id: str
    title: str
    url: str
    competitor: str
    doc_type: str
    date_ts: int
    score: float
    extra: dict = field(default_factory=dict)
