"""Telegram alerts, weekly digest delivery and an optional Q&A bot.

Uses the plain Bot API over HTTPS (free, no extra library). The bot token is
part of the request URL, so it is never logged: httpx request logging is kept
at WARNING level and errors only mention the method name and status code.
"""

from __future__ import annotations

import html
import logging
import re
import time

import httpx

from agent.config import get_settings

logger = logging.getLogger(__name__)

API = "https://api.telegram.org"
LIMIT = 4096
_ICON = {"high": "🔴", "medium": "🟠", "low": "⚪"}


class TelegramError(Exception):
    pass


def esc(text: str) -> str:
    return html.escape(text or "", quote=False)


def split_message(text: str, limit: int = LIMIT) -> list[str]:
    """Split at paragraph / line boundaries so no message exceeds Telegram's
    4096-character limit (and no HTML tag is cut in half)."""
    if len(text) <= limit:
        return [text]
    parts, current = [], ""
    for block in re.split(r"(\n\n)", text):
        if len(current) + len(block) <= limit:
            current += block
            continue
        if current.strip():
            parts.append(current.strip())
        current = ""
        while len(block) > limit:  # one huge paragraph: split by lines, then hard cut
            cut = block.rfind("\n", 0, limit)
            cut = cut if cut > 0 else limit
            parts.append(block[:cut].strip())
            block = block[cut:]
        current = block
    if current.strip():
        parts.append(current.strip())
    return [p for p in parts if p]


class TelegramClient:
    def __init__(self, token: str, chat_id: str, client: httpx.Client | None = None):
        self._token = token
        self.chat_id = str(chat_id)
        self.client = client or httpx.Client(timeout=40)

    def _call(self, method: str, payload: dict, retries: int = 3) -> dict:
        url = f"{API}/bot{self._token}/{method}"
        for attempt in range(retries + 1):
            try:
                resp = self.client.post(url, json=payload)
            except httpx.HTTPError as exc:
                if attempt >= retries:
                    raise TelegramError(f"{method} failed ({type(exc).__name__})") from None
                time.sleep(2 * (attempt + 1))
                continue
            data = resp.json() if resp.headers.get("content-type", "").startswith(
                "application/json") else {}
            if resp.status_code == 429 and attempt < retries:
                wait = (data.get("parameters") or {}).get("retry_after", 5)
                time.sleep(min(float(wait), 60) + 0.5)
                continue
            if resp.status_code >= 500 and attempt < retries:
                time.sleep(2 * (attempt + 1))
                continue
            if not data.get("ok"):
                # description is safe (e.g. "Bad Request: chat not found"); never the URL
                raise TelegramError(f"{method} failed (HTTP {resp.status_code}: "
                                    f"{data.get('description', 'unknown error')})")
            return data
        raise TelegramError(f"{method} failed after retries")

    # ------------------------------------------------------------ sending
    def send(self, text: str, chat_id: str | None = None) -> int:
        """Send HTML text, split into several messages if needed."""
        parts = split_message(text)
        for part in parts:
            self._call("sendMessage", {"chat_id": chat_id or self.chat_id, "text": part,
                                       "parse_mode": "HTML",
                                       "disable_web_page_preview": True})
        return len(parts)

    def send_alert(self, change: dict) -> None:
        self.send(format_alert(change))

    def send_problems(self, problems: list[str]) -> None:
        body = "\n".join(f"• {esc(p)}" for p in problems[:15])
        self.send(f"⚠️ <b>Market Research Agent: needs attention</b>\n\n{body}")

    def get_updates(self, offset: int | None, timeout: int = 30) -> list[dict]:
        payload = {"timeout": timeout, "allowed_updates": ["message"]}
        if offset is not None:
            payload["offset"] = offset
        return self._call("getUpdates", payload, retries=1).get("result", [])

    def get_me(self) -> dict:
        return self._call("getMe", {}).get("result", {})


def from_settings() -> TelegramClient | None:
    s = get_settings()
    return TelegramClient(s.telegram_bot_token, s.telegram_chat_id) if s.telegram_configured else None


# ------------------------------------------------------------------ formatting
def _date(ts: int) -> str:
    return time.strftime("%d %b %Y", time.gmtime(ts))


def format_alert(change: dict) -> str:
    icon = _ICON.get(change["importance"], "⚪")
    lines = [f"{icon} <b>{esc(change['competitor'])}</b> · {esc(change['source_type'])} · "
             f"{_date(change['effective_at'])}",
             "", esc(change["summary"] or "Change detected.")]
    for pc in change.get("pricing_changes") or []:
        lines.append(f"  • {esc(pc['text'])}")
    if change.get("why"):
        lines += ["", f"<i>Why it matters:</i> {esc(change['why'])}"]
    if change.get("url"):
        lines += ["", f'<a href="{esc(change["url"])}">Open page</a>']
    return "\n".join(lines)


def markdown_to_telegram(md: str) -> str:
    """Convert the small Markdown subset our digests use into Telegram HTML."""
    out = []
    for line in md.splitlines():
        line = esc(line)
        line = re.sub(r"\[([^\]]+)\]\((https?://[^)\s]+)\)", r'<a href="\2">\1</a>', line)
        line = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", line)
        if line.startswith("## ") or line.startswith("# "):
            line = f"<b>{line.lstrip('# ').strip()}</b>"
        elif line.startswith("- "):
            line = "• " + line[2:]
        out.append(line)
    return "\n".join(out)


# ------------------------------------------------------------------ Q&A bot
HELP = ("Commands:\n"
        "/ask &lt;question&gt; - ask about your competitors (answers cite sources)\n"
        "/latest - the 5 most recent changes\n"
        "/digest - this week's digest\n"
        "You can also just type a question.")


def run_bot(client: TelegramClient | None = None, storage=None) -> None:
    """Long-polling bot for questions from your phone. Runs while this process
    runs (locally); it only answers the configured chat."""
    from agent import digest as digest_mod
    from agent import rag
    from agent.indexer import sync_index
    from agent.pipeline import open_storage

    client = client or from_settings()
    if client is None:
        raise SystemExit("Set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID in .env first.")
    storage = storage or open_storage()
    sync_index(storage)
    me = client.get_me()
    logger.info("Bot @%s is listening (Ctrl+C to stop)", me.get("username", "?"))
    offset = None
    while True:
        try:
            updates = client.get_updates(offset)
        except TelegramError as exc:
            logger.warning("%s; retrying in 10s", exc)
            time.sleep(10)
            continue
        for update in updates:
            offset = update["update_id"] + 1
            msg = update.get("message") or {}
            chat = str((msg.get("chat") or {}).get("id", ""))
            text = (msg.get("text") or "").strip()
            if not text or chat != client.chat_id:
                continue  # ignore strangers: the bot is private to your chat
            try:
                client.send(handle_command(text, storage, rag, digest_mod), chat)
            except Exception as exc:
                logger.warning("Bot reply failed: %s", type(exc).__name__)
                client.send(f"Sorry, something went wrong ({esc(type(exc).__name__)}).", chat)


def handle_command(text: str, storage, rag, digest_mod) -> str:
    command, _, rest = text.partition(" ")
    command = command.split("@")[0].lower()
    if command in ("/start", "/help"):
        return HELP
    if command == "/latest":
        changes = storage.changes(limit=5)
        if not changes:
            return "No changes recorded yet."
        return "\n\n".join(format_alert(c) for c in changes)
    if command == "/digest":
        return markdown_to_telegram(digest_mod.build_digest(storage, save=False)["markdown"])
    question = rest.strip() if command == "/ask" else text
    if not question:
        return "Usage: /ask Which competitor changed pricing most recently?"
    result = rag.answer(question, storage=storage)
    reply = markdown_to_telegram(result.answer)
    if result.sources:
        reply += "\n\n" + "\n".join(
            f'[{s.index}] <a href="{esc(s.url)}">{esc(s.title)}</a>' if s.url
            else f"[{s.index}] {esc(s.title)}" for s in result.sources)
    return reply
