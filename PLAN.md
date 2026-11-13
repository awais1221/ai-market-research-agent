# AI Market Research Agent: Implementation Plan

## Context
A portfolio project combining web scraping, scheduled automation and RAG (retrieval-augmented generation).

**What it does:** it watches competitor websites on a schedule (pricing pages, changelogs, blogs), detects what changed, and uses AI to summarise the change. It stores everything in a searchable knowledge base, so you can ask questions like "Did anyone raise prices this quarter?" and get answers with sources. It sends alerts and a weekly digest to **Telegram** and logs every change in **Google Sheets**.

**Demo niche:** SaaS competitors, tracking workflow-automation tools (Zapier, n8n, Pipedream, Activepieces, IFTTT). Each site was checked for robots.txt permission and readable server-rendered HTML; Make was dropped because it blocks automated requests.

**Constraint: $0, with no card anywhere.** That means a free-tier LLM (Gemini), local embeddings, free GitHub Actions for scheduling (public repo), the free Telegram Bot API, and Google Sheets through a free service account.

Repository: `ai-market-research-agent`.

## Reuse from rag-chatbot (copy and adapt, don't rewrite)
- `app/config.py`: frozen Settings dataclass pattern and the `llm_configured` check
- `app/llm_client.py`: `call_llm`, `stream_llm`, 429 retry, friendly errors. Add a JSON-mode helper for structured pricing extraction (as in invoice-extractor).
- `app/loaders.py`: `WebLoader` (httpx fetch, robots.txt, user agent, sitemap) and `html_to_text` (noise stripping, keeps headings and tables)
- `app/chunker.py`: `chunk_documents`, heading-aware
- `app/vectorstore.py`: `FastEmbedder`, `VectorStore` (Chroma, cosine, thread-safe `get_store`). Add metadata filters for competitor, type and date.
- `app/rag.py`: prompt with cited sources, relevance gate, `cited_indices` and `_only_cited`
- `static/` chat UI and markdown renderer (from app.js), plus the `tests/conftest.py` FakeEmbedder pattern
- `scripts/` recording approach for the GIF and MP4 (Playwright + Pillow + imageio-ffmpeg, already installed)

## Architecture
```
sources.yaml ─► Collector ─► Snapshot store (SQLite) ─► Change detector ─► AI summariser
                 (httpx / RSS /         │                     (diff + noise        │
                  optional Playwright)  │                      filtering)          ▼
                                        ▼                              changes table
                                  RAG index (Chroma) ◄──────────────────────┘
                                        │
            ┌───────────────────────────┼───────────────────────────────┐
            ▼                           ▼                               ▼
   Web dashboard + Q&A        Telegram alerts + weekly digest     Google Sheets log
   (FastAPI, local)           (Bot API)                           (gspread)
```

## Folder structure
```
market-research-agent/
├── agent/
│   ├── config.py          # settings from .env (+ sources.yaml path)
│   ├── sources.py         # load/validate sources.yaml (pydantic)
│   ├── collector.py       # fetch pages, RSS feeds, optional Playwright render
│   ├── storage.py         # SQLite: sources, snapshots, items, changes, digests
│   ├── differ.py          # normalise text, diff, noise filtering, new-item detection
│   ├── analyzer.py        # LLM: change summary + importance + category; pricing JSON extraction
│   ├── indexer.py         # chunk + embed snapshots/items/changes into Chroma (rebuildable from SQLite)
│   ├── rag.py             # cited Q&A with competitor/date filters
│   ├── digest.py          # weekly digest (cited, grouped by competitor)
│   ├── notify/telegram.py # send alerts/digest (HTML, 4096-char split, retry)
│   ├── notify/sheets.py   # append change rows (optional, off without credentials)
│   ├── backfill.py        # history from RSS archives + Wayback Machine pricing snapshots
│   ├── pipeline.py        # run = collect → detect → analyse → index → notify
│   ├── cli.py             # python -m agent run | digest | ask | backfill | bot | serve
│   └── llm_client.py, chunker.py, vectorstore.py, loaders.py   (adapted copies)
├── web/main.py + static/  # dashboard: overview, timeline, diff view, digests, Q&A chat
├── sources.yaml           # demo competitors (+ sources.example.yaml)
├── .github/workflows/
│   ├── monitor.yml        # daily cron: run pipeline, persist DB
│   └── digest.yml         # weekly cron (Monday): send digest
├── tests/                 # offline, mocked HTTP/LLM/Telegram/Sheets
├── Dockerfile, requirements.txt, .env.example, README.md, PLAN.md, LICENSE
```

## Key behaviour

**1. Sources (`sources.yaml`)**
```yaml
competitors:
  - name: n8n
    sources:
      - {type: pricing,   url: https://n8n.io/pricing/}
      - {type: blog,      url: https://blog.n8n.io/rss/, format: rss}
      - {type: changelog, url: https://docs.n8n.io/release-notes/, render: false}
```
**Optional: competitor list from a Google Sheet (client-editable).** If `SOURCES_SHEET_ID` is set, each run reads a **"Competitors"** tab with the columns `competitor | type | url | format | render | ignore | active`. That lets the client add, pause or remove competitors by editing a row, with no code and no developer.
- Rows are validated with the same pydantic schema as the YAML. Invalid rows are skipped and reported in the run summary and in Telegram (for example "Row 7: bad URL"), so one typo never breaks the run.
- If the Sheet can't be reached, the agent falls back to the last good list cached in SQLite, and then to `sources.yaml`.
- A source removed from the Sheet keeps its history but is no longer checked.
- `python -m agent sources --export-sheet` fills the tab from `sources.yaml` for first-time setup.
- The tab lives in the same spreadsheet as the change log, so it uses the same service account.

The source types are `pricing`, `changelog`, `blog`, `page` and `news`. The formats are `html`, `rss` and `sitemap`. Set `render: true` for pages that need JavaScript (Playwright, which runs on GitHub Actions too).

**2. Collector**
- It reuses `WebLoader`: robots.txt check, a polite user agent, 1–2 seconds between requests on the same domain, timeouts and retries.
- RSS is read with `feedparser`, and new items are found by their URL or GUID.
- One failed source never stops a run. Errors are logged in the run summary.

**3. Snapshots and change detection**
- Each fetch stores the normalised text plus a sha256 hash. If the hash is unchanged, nothing happens.
- When the text changes, a `difflib` diff runs on the normalised text. Noise is filtered out first: dates, "x minutes ago", counters, cookie banners, rotating testimonials, and a per-source `ignore` regex list.
- Changes below a minimum size threshold are skipped.
- **Pricing pages:** the LLM extracts structured plans (`plan, price, currency, period, key limits`) as JSON, validated with pydantic. Plans are compared field by field, so "Pro: $20 → $24/mo" is exact and doesn't depend on the AI's wording.

**4. AI analyser (free-tier aware)**
- The LLM only runs on actual changes and new items, never on unchanged pages. Results are cached by content hash.
- It outputs `summary`, `importance` (high/medium/low), `category` (pricing/feature/announcement/other) and `why_it_matters`.
- Scraped text is treated as data in the prompt. Injected instructions are ignored, as in the chatbot's SYSTEM_PROMPT.

**5. RAG knowledge base**
- Snapshots, blog and changelog items, and change summaries are chunked and embedded into Chroma. The metadata is competitor, type, url, date (epoch) and change_id.
- Q&A uses retrieval with optional filters for competitor and date range. Simple date phrases in questions ("this month", "last 30 days") are parsed into filters. Answers carry `[n]` citations.
- **What gets indexed:**
  - Only the **latest** version of each page. It's replaced when the page changes, so answers never use stale text.
  - Every blog or changelog item.
  - Every change record (summary, old→new, date) as its own document. That's how history stays searchable.
- **Questions mentioning a competitor** are filtered to that competitor's chunks.
- **"Current price" questions** also get the structured latest-pricing table from SQLite as an extra numbered source, so numbers come straight from stored data, not from the AI.
- **Relevance gate:** an off-topic question, or one where nothing matches, gets a "no data" answer with no LLM call.
- The index is rebuildable from SQLite (`python -m agent reindex`), so CI only needs to keep the SQLite file.

**6. Telegram**
- After each run it sends an immediate alert for **high** importance changes, such as a price change or a new plan.
- The weekly digest is an AI summary grouped by competitor, with links. Messages use HTML mode and are split at 4096 characters.
- Optional `python -m agent bot` uses long polling **while running locally**, so you can send `/ask`, `/latest` and `/digest` from your phone. (Always-on hosting would need a server, which isn't free without a card.)

**7. Google Sheets**
- Each change is appended as a row: date, competitor, type, importance, summary, url and an old→new price if there is one. There's also a "Pricing" sheet with the latest plans.
- It uses a service-account JSON from an env var or secret. Without credentials it's skipped quietly.

**8. Scheduling (GitHub Actions, free on a public repo)**
- `monitor.yml` runs daily at 06:00 UTC plus manual dispatch: install, cache the fastembed model, `python -m agent run`, then commit `data/agent.db` to a separate **`data` branch** so the main branch history stays clean.
- `digest.yml` runs on Mondays and sends the weekly digest.
- Secrets: `API_KEY`, `MODEL_NAME`, `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`, `GOOGLE_SERVICE_ACCOUNT_JSON`, `SHEET_ID`.
- The README notes that GitHub disables scheduled workflows after 60 days of repo inactivity, and explains how to re-enable them.

**9. Backfill (for a convincing demo on day one)**
- Past blog and changelog entries come from RSS archives.
- Past **pricing page versions** come from the free Wayback Machine CDX API, for example one snapshot per month for 12 months. They go through the same diff and analyser steps, which produces **real historical price changes**. They're marked as `source: archive` in the UI.

**10. Web dashboard (FastAPI, run locally)**
- Overview: competitors, last check time, number of changes and latest plans.
- Timeline of changes with importance badges and filters.
- Change detail with a side-by-side diff and the old/new pricing table.
- Archive of past digests.
- **Ask** panel: cited Q&A, reusing the chatbot's chat UI.

## Security and reliability
- Secrets only come from env vars or GitHub Secrets. The Telegram token is never logged. `.env` and `data/` are gitignored on main.
- Scraping is polite: robots.txt, rate limiting, user agent `MarketResearchAgent/1.0 (+repo URL)`, only public pages, and no personal data collected.
- Every external call has timeouts and retries. A run summary is printed and failures are reported in Telegram if any.
- Duplicate protection: the same change is never alerted twice (a unique constraint on source + hash pair).

## Build order
1. Scaffold the repo and venv, and copy/adapt config, llm_client, loaders, chunker and vectorstore.
2. sources.yaml schema, storage (SQLite) and tests. (The Google Sheet competitor-list reader is added in step 6 with the Sheets client.)
3. Collector (html/rss/render) and differ (noise filtering, pricing JSON compare), with tests.
4. Analyser plus pipeline `run` (mocked-LLM tests).
5. Indexer plus RAG Q&A with filters, with tests.
6. Telegram and Sheets notifiers (mocked), and digest.
7. Backfill (Wayback CDX + RSS), then pick and verify the demo competitors and run a real backfill.
8. Web dashboard, then GitHub Actions workflows (tested with manual dispatch).
9. README, screenshots, GIF and MP4, PLAN.md and LICENSE, then commit and push.

## One-time setup (all free, no card)
- A Telegram bot via @BotFather (gives a token) plus the chat ID.
- A Google Cloud service account plus a Sheet shared with it (no billing needed for the Sheets API).
- GitHub repo secrets for the keys and tokens.

## Verification
- `pytest`: everything offline. HTTP is mocked with `httpx.MockTransport`, and the LLM, Telegram and Sheets are mocked. Includes noise-filter cases and pricing diffs.
- A local real run: `python -m agent backfill`, then `python -m agent run` against the demo competitors. Check the detected historical price changes by hand against the Wayback pages.
- `python -m agent ask "Which competitor changed pricing most recently?"` should return a cited answer. An off-topic question should be declined.
- Telegram: a test alert and the digest arrive on the phone. Sheets: rows appear.
- GitHub Actions: manual dispatch of `monitor.yml` succeeds, the `data` branch updates, and a second run finds no new changes (no duplicate alerts).
- Memory and time: one full run finishes within GitHub Actions limits (target under 10 minutes).

## Limitations to note in the README
- Free LLM rate limits; Wayback history depends on archive coverage; heavy JavaScript pages need `render: true`.
- Interactive Telegram Q&A only works while the bot runs locally.
- Scheduled runs pause after 60 days without repo activity.
- Future work: email digest, Slack, a hosted dashboard, screenshot visual diffs, and sentiment on reviews.
