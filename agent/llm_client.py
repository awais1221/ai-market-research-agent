"""Provider-agnostic LLM access via the OpenAI-compatible API.

Works with Google Gemini (free tier), Groq, GitHub Models, Ollama, OpenAI...
``call_llm`` / ``stream_llm`` are the only functions that touch the network,
so they are the single place tests need to mock.

Free tiers allow only a few requests per minute, so calls are spaced out
(``LLM_MIN_INTERVAL``) and 429 responses are retried after the wait the
provider asks for.
"""

from __future__ import annotations

import json
import logging
import re
import threading
import time
from collections.abc import Iterator
from functools import lru_cache
from typing import TypeVar

from openai import APIConnectionError, APIError, APITimeoutError, OpenAI, RateLimitError
from pydantic import BaseModel, ValidationError

from agent.config import get_settings

logger = logging.getLogger(__name__)

T = TypeVar("T", bound=BaseModel)


class LLMNotConfiguredError(Exception):
    """BASE_URL / API_KEY / MODEL_NAME are missing."""


class LLMError(Exception):
    """The provider returned an error (rate limit, auth, timeout, bad JSON...)."""


_pace_lock = threading.Lock()
_last_call = 0.0


def _pace() -> None:
    """Keep at least LLM_MIN_INTERVAL seconds between calls."""
    global _last_call
    interval = get_settings().llm_min_interval
    with _pace_lock:
        wait = interval - (time.monotonic() - _last_call)
        if wait > 0:
            time.sleep(wait)
        _last_call = time.monotonic()


@lru_cache
def _get_client() -> OpenAI:
    s = get_settings()
    return OpenAI(base_url=s.base_url, api_key=s.api_key, timeout=s.llm_timeout, max_retries=2)


def _retry_delay(exc: RateLimitError) -> float:
    header = exc.response.headers.get("retry-after") if exc.response is not None else None
    if header:
        try:
            return float(header)
        except ValueError:
            pass
    match = re.search(r"retry in ([\d.]+)\s*s|retryDelay['\"]?\s*:\s*['\"]([\d.]+)s",
                      f"{exc.body} {exc}", re.IGNORECASE)
    if match:
        return float(match.group(1) or match.group(2))
    return 20.0


def _create(kwargs: dict):
    s = get_settings()
    client = _get_client()
    for attempt in range(s.rate_limit_retries + 1):
        _pace()
        try:
            return client.chat.completions.create(**kwargs)
        except RateLimitError as exc:
            delay = _retry_delay(exc)
            if delay > s.rate_limit_max_wait:
                raise LLMError("The free AI quota is used up for now. Try again later.") from exc
            if attempt >= s.rate_limit_retries:
                raise
            logger.info("Rate limited; waiting %.0fs (retry %d/%d)",
                        delay + 1, attempt + 1, s.rate_limit_retries)
            time.sleep(delay + 1)


def _ensure_configured() -> None:
    if not get_settings().llm_configured:
        raise LLMNotConfiguredError(
            "The AI model is not configured. Set BASE_URL, API_KEY and MODEL_NAME in .env."
        )


def _translate(exc: Exception) -> LLMError:
    if isinstance(exc, LLMError):
        return exc
    if isinstance(exc, APITimeoutError):
        return LLMError("The AI provider took too long to respond.")
    if isinstance(exc, APIConnectionError):
        return LLMError("Could not connect to the AI provider.")
    if isinstance(exc, APIError):
        status = getattr(exc, "status_code", None)
        logger.warning("LLM request failed (status=%s, %s)", status, type(exc).__name__)
        return LLMError(_friendly_error(status))
    logger.warning("LLM request failed: %s", type(exc).__name__)
    return LLMError(f"Could not reach the AI provider ({type(exc).__name__}).")


def _kwargs(messages: list[dict]) -> dict:
    s = get_settings()
    return {"model": s.model_name, "messages": messages, "temperature": s.llm_temperature}


def call_llm(messages: list[dict]) -> str:
    """Send chat messages and return the full text reply."""
    _ensure_configured()
    started = time.perf_counter()
    try:
        response = _create(_kwargs(messages))
    except Exception as exc:
        raise _translate(exc) from exc
    logger.debug("LLM call took %.1fs", time.perf_counter() - started)
    content = response.choices[0].message.content if response.choices else None
    return content or ""


class FullReply(str):
    """Yielded by ``stream_llm`` when a stream broke off: the complete reply,
    which replaces everything streamed so far."""


def stream_llm(messages: list[dict]) -> Iterator[str]:
    """Send chat messages and yield the reply in pieces as they arrive."""
    _ensure_configured()
    finished = False
    try:
        for chunk in _create({**_kwargs(messages), "stream": True}):
            if chunk.choices and chunk.choices[0].delta.content:
                yield chunk.choices[0].delta.content
            if chunk.choices and chunk.choices[0].finish_reason:
                finished = True
    except Exception as exc:
        raise _translate(exc) from exc
    if not finished:
        # some OpenAI-compatible providers occasionally close a stream early
        logger.warning("The AI stream ended early; fetching the full reply")
        yield FullReply(call_llm(messages))


# ------------------------------------------------------------------ JSON
_FENCE = re.compile(r"^```(?:json)?\s*|\s*```$", re.IGNORECASE)


def parse_json(text: str):
    """Parse JSON from a model reply, tolerating ```json fences and extra prose."""
    text = _FENCE.sub("", text.strip()).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    for opener, closer in (("{", "}"), ("[", "]")):
        start, end = text.find(opener), text.rfind(closer)
        if start != -1 and end > start:
            try:
                return json.loads(text[start: end + 1])
            except json.JSONDecodeError:
                continue
    raise LLMError("The AI reply was not valid JSON.")


def call_llm_json(messages: list[dict], model: type[T]) -> T:
    """Ask for JSON and validate it against a pydantic model (one repair retry)."""
    reply = call_llm(messages)
    try:
        return model.model_validate(parse_json(reply))
    except (LLMError, ValidationError) as first_error:
        logger.info("Invalid JSON from the model, asking once more (%s)",
                    type(first_error).__name__)
        repair = messages + [
            {"role": "assistant", "content": reply[:4000]},
            {"role": "user", "content": "That was not valid JSON for the requested schema. "
                                        "Reply again with ONLY the JSON object."},
        ]
        try:
            return model.model_validate(parse_json(call_llm(repair)))
        except ValidationError as exc:
            raise LLMError("The AI reply did not match the expected format.") from exc


def _friendly_error(status: int | None) -> str:
    if status in (401, 403):
        return "The AI provider rejected the API key (check API_KEY)."
    if status == 404:
        return "AI model or endpoint not found (check BASE_URL and MODEL_NAME)."
    if status == 429:
        return "The AI is getting too many requests right now. Try again in a minute."
    if status in (500, 502, 503, 504):
        return "The AI provider is temporarily unavailable."
    return f"AI provider error (status {status})."
