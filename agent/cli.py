"""Command line: ``python -m agent <command>``.

  run                  check every source once, alert on Telegram, sync the Sheet
  backfill             load the past year of pricing history from the Wayback Machine
  digest [--send]      build the weekly digest (and send it to Telegram)
  ask "question"       ask a question, get a cited answer
  status               what is monitored and what was found
  reindex              rebuild the search index from the database
  serve                start the local dashboard (http://127.0.0.1:8000)
  bot                  answer /ask, /latest and /digest on Telegram while running
  sources              show the competitor list (--export-sheet fills the Sheet tab)
  test-telegram        send a test message
  test-sheets          check the Google Sheet connection
"""

from __future__ import annotations

import argparse
import logging
import re
import sys
import time

from agent.config import get_settings


class RedactSecrets(logging.Filter):
    """Belt and braces: Telegram puts the bot token in the request URL, so any
    log line that contains one is redacted before it is written."""

    _TOKEN = re.compile(r"bot\d{5,}:[A-Za-z0-9_-]{20,}")

    def filter(self, record: logging.LogRecord) -> bool:
        message = record.getMessage()
        if self._TOKEN.search(message):
            record.msg, record.args = self._TOKEN.sub("bot<redacted>", message), ()
        return True


def _setup_logging(verbose: bool) -> None:
    logging.basicConfig(level=logging.DEBUG if verbose else logging.INFO,
                        format="%(asctime)s | %(levelname)-7s | %(message)s", datefmt="%H:%M:%S")
    for handler in logging.getLogger().handlers:
        handler.addFilter(RedactSecrets())
    # httpx logs full request URLs (for Telegram they contain the bot token)
    for noisy in ("httpx", "httpx2", "httpcore", "openai", "chromadb", "urllib3", "fastembed"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def _print(text: str) -> None:
    data = (text + "\n").encode(sys.stdout.encoding or "utf-8", errors="replace")
    sys.stdout.buffer.write(data)
    sys.stdout.flush()


def cmd_run(args) -> int:
    from agent.pipeline import run

    summary = run()
    _print(summary.text())
    return 0 if summary.checked and len(summary.errors) < summary.checked else 1


def cmd_backfill(args) -> int:
    from agent.backfill import backfill

    types = tuple(t.strip() for t in args.types.split(",") if t.strip())
    _print(backfill(months=args.months, types=types).text())
    return 0


def cmd_digest(args) -> int:
    from agent.digest import build_digest
    from agent.notify import telegram
    from agent.pipeline import open_storage

    storage = open_storage()
    digest = build_digest(storage, days=args.days)
    _print(digest["markdown"])
    if args.send:
        client = telegram.from_settings()
        if client is None:
            _print("\nTelegram is not configured (TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID).")
            return 1
        client.send(telegram.markdown_to_telegram(digest["markdown"]))
        storage.mark_digest_sent(digest["id"])
        _print("\nSent to Telegram.")
    return 0


def cmd_ask(args) -> int:
    from agent import rag
    from agent.indexer import sync_index
    from agent.pipeline import open_storage

    storage = open_storage()
    sync_index(storage)
    result = rag.answer(" ".join(args.question), storage)
    _print("\n" + result.answer + "\n")
    for s in result.sources:
        _print(f"  [{s.index}] {s.title} ({s.date}) {s.url}")
    return 0


def cmd_status(args) -> int:
    from agent.pipeline import open_storage

    storage = open_storage()
    counts = storage.change_counts()
    _print(f"Database: {get_settings().db_path}")
    for src in storage.sources():
        checked = time.strftime("%Y-%m-%d %H:%M", time.gmtime(src["last_checked"])) \
            if src["last_checked"] else "never"
        state = "active" if src["active"] else "paused"
        err = f"  ERROR: {src['last_error']}" if src["last_error"] else ""
        _print(f"  {src['competitor']:<14} {src['type']:<10} {state:<7} checked {checked}  "
               f"{src['url']}{err}")
    _print("\nChanges per competitor:")
    for name, c in sorted(counts.items()):
        _print(f"  {name:<14} {c['changes']:>4} total, {c['high']} high importance")
    last = storage.last_run()
    if last:
        _print(f"\nLast run: {time.strftime('%Y-%m-%d %H:%M UTC', time.gmtime(last['started_at']))}"
               f" - {last['summary'].get('changes', 0)} change(s), "
               f"{len(last['summary'].get('errors', []))} error(s)")
    return 0


def cmd_reindex(args) -> int:
    from agent.indexer import reindex
    from agent.pipeline import open_storage

    _print(str(reindex(open_storage())))
    return 0


def cmd_serve(args) -> int:
    import uvicorn

    uvicorn.run("web.main:app", host=args.host, port=args.port, reload=False)
    return 0


def cmd_bot(args) -> int:
    from agent.notify.telegram import run_bot

    try:
        run_bot()
    except KeyboardInterrupt:
        pass
    return 0


def cmd_sources(args) -> int:
    from agent.notify import sheets
    from agent.pipeline import open_storage
    from agent.sources import load_sources, load_yaml

    settings = get_settings()
    if args.export_sheet:
        client = sheets.from_settings()
        if client is None:
            _print("Google Sheets is not configured (GOOGLE_SERVICE_ACCOUNT_JSON / SHEET_ID).")
            return 1
        n = client.export_sources(load_yaml(settings.sources_file).specs)
        _print(f"Wrote {n} row(s) to the 'Competitors' tab of '{client.title()}'. "
               "Set SOURCES_FROM_SHEET=true to use it.")
        return 0
    client = sheets.from_settings() if settings.sources_from_sheet else None
    result = load_sources(settings.sources_file, open_storage(),
                          client.read_sources if client else None)
    _print(f"{len(result.specs)} source(s) from {result.origin}:")
    for s in result.specs:
        _print(f"  {s.competitor:<14} {s.type:<10} {s.format:<8} {'' if s.active else '(paused) '}"
               f"{s.url}")
    for e in result.errors:
        _print(f"  ! {e}")
    return 0


def cmd_test_telegram(args) -> int:
    from agent.notify import telegram

    client = telegram.from_settings()
    if client is None:
        _print("Set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID first.")
        return 1
    me = client.get_me()
    client.send("✅ <b>Market Research Agent</b> is connected. Alerts will arrive here.")
    _print(f"Test message sent by @{me.get('username', '?')}.")
    return 0


def cmd_test_sheets(args) -> int:
    from agent.notify import sheets

    client = sheets.from_settings()
    if client is None:
        _print("Google Sheets is not configured, or the credentials / sheet ID are wrong, "
               "or the Sheet is not shared with the service account e-mail.")
        return 1
    _print(f"Connected to the Sheet '{client.title()}'.")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m agent", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("-v", "--verbose", action="store_true")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("run").set_defaults(func=cmd_run)
    p = sub.add_parser("backfill")
    p.add_argument("--months", type=int, default=12)
    p.add_argument("--types", default="pricing", help="page types to backfill, e.g. pricing,page")
    p.set_defaults(func=cmd_backfill)
    p = sub.add_parser("digest")
    p.add_argument("--days", type=int, default=7)
    p.add_argument("--send", action="store_true")
    p.set_defaults(func=cmd_digest)
    p = sub.add_parser("ask")
    p.add_argument("question", nargs="+")
    p.set_defaults(func=cmd_ask)
    sub.add_parser("status").set_defaults(func=cmd_status)
    sub.add_parser("reindex").set_defaults(func=cmd_reindex)
    p = sub.add_parser("serve")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8000)
    p.set_defaults(func=cmd_serve)
    sub.add_parser("bot").set_defaults(func=cmd_bot)
    p = sub.add_parser("sources")
    p.add_argument("--export-sheet", action="store_true")
    p.set_defaults(func=cmd_sources)
    sub.add_parser("test-telegram").set_defaults(func=cmd_test_telegram)
    sub.add_parser("test-sheets").set_defaults(func=cmd_test_sheets)

    args = parser.parse_args(argv)
    _setup_logging(args.verbose)
    return args.func(args)
