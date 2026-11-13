"""Settings loaded from environment variables / a .env file.

Secrets (API key, Telegram token, Google service account) only ever come from
the environment: a local ``.env`` file or GitHub Actions secrets.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent

load_dotenv(PROJECT_ROOT / ".env")

_PLACEHOLDER_MARKERS = ("your-", "your_", "xxx", "changeme")


def _int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, default))
    except ValueError:
        return default


def _float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, default))
    except ValueError:
        return default


def _bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def _str(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()


def is_placeholder(value: str) -> bool:
    value = value.strip().lower()
    return not value or any(m in value for m in _PLACEHOLDER_MARKERS)


@dataclass(frozen=True)
class Settings:
    # LLM (any OpenAI-compatible provider, e.g. Google Gemini's free tier)
    base_url: str = ""
    api_key: str = ""
    model_name: str = ""
    llm_timeout: int = 60
    llm_temperature: float = 0.1
    rate_limit_retries: int = 2
    rate_limit_max_wait: int = 65
    llm_min_interval: float = 4.0     # seconds between LLM calls (free tiers: ~15/min)
    max_llm_calls_per_run: int = 60   # above this, rule-based summaries are used

    # Storage
    data_dir: str = str(PROJECT_ROOT / "data")
    db_file: str = "agent.db"
    collection_name: str = "market_research"

    # Retrieval
    embedding_model: str = "BAAI/bge-small-en-v1.5"
    chunk_size: int = 800
    chunk_overlap: int = 120
    top_k: int = 6
    min_score: float = 0.5
    history_turns: int = 3

    # Collection
    sources_file: str = str(PROJECT_ROOT / "sources.yaml")
    request_delay: float = 1.5        # seconds between requests to the same domain
    request_timeout: int = 30
    max_feed_items: int = 30          # newest items read per feed
    min_change_chars: int = 40        # smaller text changes are ignored as noise
    report_after_failures: int = 3    # Telegram warning after N failed runs in a row

    # Telegram
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""
    alert_min_importance: str = "high"  # high | medium | low

    # Google Sheets
    google_service_account_json: str = ""  # the JSON itself, or a path to the file
    sheet_id: str = ""
    sources_from_sheet: bool = False      # read the competitor list from the "Competitors" tab

    @property
    def db_path(self) -> str:
        return str(Path(self.data_dir) / self.db_file)

    @property
    def chroma_path(self) -> str:
        return str(Path(self.data_dir) / "chroma")

    @property
    def llm_configured(self) -> bool:
        return (not is_placeholder(self.api_key)) and bool(self.base_url) and bool(self.model_name)

    @property
    def telegram_configured(self) -> bool:
        return not is_placeholder(self.telegram_bot_token) and bool(self.telegram_chat_id)

    @property
    def sheets_configured(self) -> bool:
        return not is_placeholder(self.google_service_account_json) and bool(self.sheet_id)


@lru_cache
def get_settings() -> Settings:
    return Settings(
        base_url=_str("BASE_URL"),
        api_key=_str("API_KEY"),
        model_name=_str("MODEL_NAME"),
        llm_timeout=_int("LLM_TIMEOUT", 60),
        llm_temperature=_float("LLM_TEMPERATURE", 0.1),
        rate_limit_retries=max(0, _int("RATE_LIMIT_RETRIES", 2)),
        rate_limit_max_wait=_int("RATE_LIMIT_MAX_WAIT", 65),
        llm_min_interval=max(0.0, _float("LLM_MIN_INTERVAL", 4.0)),
        max_llm_calls_per_run=max(0, _int("MAX_LLM_CALLS_PER_RUN", 60)),
        data_dir=_str("DATA_DIR") or str(PROJECT_ROOT / "data"),
        db_file=_str("DB_FILE") or "agent.db",
        collection_name=_str("COLLECTION_NAME") or "market_research",
        embedding_model=_str("EMBEDDING_MODEL") or "BAAI/bge-small-en-v1.5",
        chunk_size=max(200, _int("CHUNK_SIZE", 800)),
        chunk_overlap=max(0, _int("CHUNK_OVERLAP", 120)),
        top_k=max(1, _int("TOP_K", 6)),
        min_score=_float("MIN_SCORE", 0.5),
        history_turns=max(0, _int("HISTORY_TURNS", 3)),
        sources_file=_str("SOURCES_FILE") or str(PROJECT_ROOT / "sources.yaml"),
        request_delay=max(0.0, _float("REQUEST_DELAY", 1.5)),
        request_timeout=max(5, _int("REQUEST_TIMEOUT", 30)),
        max_feed_items=max(1, _int("MAX_FEED_ITEMS", 30)),
        min_change_chars=max(0, _int("MIN_CHANGE_CHARS", 40)),
        report_after_failures=max(1, _int("REPORT_AFTER_FAILURES", 3)),
        telegram_bot_token=_str("TELEGRAM_BOT_TOKEN"),
        telegram_chat_id=_str("TELEGRAM_CHAT_ID"),
        alert_min_importance=(_str("ALERT_MIN_IMPORTANCE") or "high").lower(),
        google_service_account_json=_str("GOOGLE_SERVICE_ACCOUNT_JSON"),
        sheet_id=_str("SHEET_ID"),
        sources_from_sheet=_bool("SOURCES_FROM_SHEET", False),
    )
