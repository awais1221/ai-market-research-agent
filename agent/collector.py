"""Polite fetching of pages, RSS/Atom feeds and sitemaps.

* respects robots.txt and waits ``REQUEST_DELAY`` seconds between requests to
  the same domain
* retries timeouts and 5xx / 429 responses with backoff
* JavaScript-built pages can be rendered with Playwright (``render: true``)

Pages become light Markdown text (``# Heading`` lines, ``a | b`` table rows),
which is what the change detector and the RAG index work on.
"""

from __future__ import annotations

import logging
import re
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime, timezone
from urllib import robotparser
from urllib.parse import urlparse

import feedparser
import httpx
from bs4 import BeautifulSoup, NavigableString

from agent.config import get_settings

logger = logging.getLogger(__name__)

USER_AGENT = "MarketResearchAgent/1.0 (+https://github.com/awais1221/ai-market-research-agent)"
MAX_BYTES = 8 * 1024 * 1024
_RETRY_STATUS = {429, 500, 502, 503, 504}
_NOISE_TAGS = ["script", "style", "noscript", "svg", "nav", "footer", "form", "iframe",
               "template", "header", "dialog", "picture", "video", "audio", "canvas"]
_NOISE_ATTR = re.compile(r"cookie|consent|gdpr|banner|newsletter|modal|popup|skip-link|"
                         r"announcement-bar|chat-widget|intercom", re.I)
_BLOCK = {"p", "div", "section", "article", "main", "li", "ul", "ol", "table", "tr",
          "h1", "h2", "h3", "h4", "h5", "h6", "pre", "blockquote", "dt", "dd", "br",
          "figcaption", "summary", "details", "td", "th", "tbody", "thead"}


class FetchError(Exception):
    """A page or feed could not be fetched (message is safe to show)."""


# ======================================================================= HTML
def html_to_text(html: str) -> tuple[str, str]:
    """Return (title, text) of a page: visible content only, headings and
    table rows kept, menus / footers / cookie banners / scripts removed."""
    soup = BeautifulSoup(html, "html.parser")
    title = soup.title.get_text(" ", strip=True) if soup.title else ""
    for tag in soup(_NOISE_TAGS):
        tag.decompose()
    for tag in soup.find_all(True):
        if tag.attrs is None:  # removed together with a parent
            continue
        marker = " ".join([tag.get("id") or "", *(tag.get("class") or [])])
        if marker and _NOISE_ATTR.search(marker) and tag.name not in ("body", "main", "html"):
            tag.decompose()
        elif tag.get("aria-hidden") == "true" or tag.has_attr("hidden"):
            tag.decompose()

    root = soup.find("main") or soup.body or soup
    if not title:
        h1 = root.find("h1")
        title = h1.get_text(" ", strip=True) if h1 else ""

    for row in root.find_all("tr"):
        cells = [c.get_text(" ", strip=True) for c in row.find_all(["td", "th"])]
        row.replace_with(NavigableString("\n" + " | ".join(c for c in cells if c) + "\n"))
    for level in range(1, 7):
        for h in root.find_all(f"h{level}"):
            h.replace_with(NavigableString(f"\n{'#' * level} {h.get_text(' ', strip=True)}\n"))
    for li in root.find_all("li"):
        li.insert(0, NavigableString("\n- "))
    for tag in root.find_all(_BLOCK):
        tag.insert_after(NavigableString("\n"))

    lines: list[str] = []
    for raw in root.get_text("").split("\n"):
        line = re.sub(r"\s+", " ", raw).strip()
        if not line or line == "-":
            continue
        # glue tiny fragments ("$", "19", "/mo") to the previous line
        if lines and (len(line) <= 3 or len(lines[-1]) <= 2) and not line.startswith("#"):
            lines[-1] = f"{lines[-1]} {line}"
        else:
            lines.append(line)
    return title or "Untitled page", "\n".join(lines)


# ======================================================================= fetch
@dataclass
class Page:
    url: str
    title: str
    text: str


@dataclass
class FeedItem:
    guid: str
    title: str
    url: str
    published_ts: int | None
    summary: str


class Collector:
    def __init__(self, client: httpx.Client | None = None, delay: float | None = None,
                 retries: int = 2):
        s = get_settings()
        self.client = client or httpx.Client(
            # brotli + zstandard are installed: archived pages may use either encoding
            headers={"User-Agent": USER_AGENT, "Accept-Language": "en-US,en;q=0.8"},
            follow_redirects=True, timeout=s.request_timeout,
        )
        self.delay = s.request_delay if delay is None else delay
        self.retries = retries
        self._robots: dict[str, robotparser.RobotFileParser | None] = {}
        self._last: dict[str, float] = {}

    def close(self) -> None:
        self.client.close()

    # ------------------------------------------------------------ low level
    def _wait_for(self, host: str) -> None:
        wait = self.delay - (time.monotonic() - self._last.get(host, 0.0))
        if wait > 0:
            time.sleep(wait)
        self._last[host] = time.monotonic()

    def allowed(self, url: str) -> bool:
        parsed = urlparse(url)
        origin = f"{parsed.scheme}://{parsed.netloc}"
        if origin not in self._robots:
            parser: robotparser.RobotFileParser | None = robotparser.RobotFileParser()
            try:
                resp = self.client.get(f"{origin}/robots.txt")
                if resp.status_code in (401, 403):
                    parser.parse(["User-agent: *", "Disallow: /"])
                else:
                    parser.parse(resp.text.splitlines() if resp.status_code == 200 else [])
            except httpx.HTTPError:
                parser = None  # robots.txt unreachable: treat as allowed
            self._robots[origin] = parser
        parser = self._robots[origin]
        return parser is None or parser.can_fetch(USER_AGENT, url)

    def get(self, url: str, check_robots: bool = True) -> httpx.Response:
        if urlparse(url).scheme not in ("http", "https"):
            raise FetchError(f"Not a web address: {url}")
        if check_robots and not self.allowed(url):
            raise FetchError(f"Blocked by robots.txt: {url}")
        host = urlparse(url).netloc
        last_error = ""
        for attempt in range(self.retries + 1):
            self._wait_for(host)
            try:
                resp = self.client.get(url)
            except httpx.HTTPError as exc:
                last_error = type(exc).__name__
            else:
                if resp.status_code not in _RETRY_STATUS:
                    if resp.status_code >= 400:
                        raise FetchError(f"{url} returned HTTP {resp.status_code}")
                    if len(resp.content) > MAX_BYTES:
                        raise FetchError(f"{url} is larger than 8 MB")
                    return resp
                last_error = f"HTTP {resp.status_code}"
            if attempt < self.retries:
                time.sleep(2 * (attempt + 1))
        raise FetchError(f"Could not fetch {url} ({last_error})")

    # ------------------------------------------------------------ pages
    def fetch_page(self, url: str, render: bool = False) -> Page:
        html = self.render(url) if render else self.get(url).text
        return self.page_from_html(url, html)

    @staticmethod
    def page_from_html(url: str, html: str) -> Page:
        sample = html[:20000]
        junk = sum(1 for ch in sample if ch == "�" or (ord(ch) < 32 and ch not in "\n\r\t"))
        if not sample or junk / len(sample) > 0.01:
            raise FetchError(f"{url} returned unreadable (binary or garbled) content")
        title, text = html_to_text(html)
        if len(text) < 30:
            raise FetchError(f"No readable text on {url} (it may need render: true)")
        return Page(url=url, title=title, text=text)

    def render(self, url: str) -> str:
        """Load the page in headless Chromium so JavaScript-built content appears."""
        if not self.allowed(url):
            raise FetchError(f"Blocked by robots.txt: {url}")
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as exc:
            raise FetchError("render: true needs Playwright "
                             "(pip install playwright && playwright install chromium)") from exc
        self._wait_for(urlparse(url).netloc)
        with sync_playwright() as p:
            browser = p.chromium.launch()
            try:
                page = browser.new_page(user_agent=USER_AGENT)
                page.goto(url, wait_until="networkidle", timeout=45000)
                return page.content()
            finally:
                browser.close()

    # ------------------------------------------------------------ feeds
    def fetch_feed(self, url: str, limit: int | None = None) -> list[FeedItem]:
        return parse_feed(self.get(url).text, url, limit or get_settings().max_feed_items)

    def fetch_sitemap(self, url: str, include: str = "", limit: int = 200) -> list[FeedItem]:
        return parse_sitemap(self.get(url).text, include, limit)


def _entry_ts(entry) -> int | None:
    for key in ("published_parsed", "updated_parsed"):
        value = entry.get(key)
        if value:
            ts = int(datetime(*value[:6], tzinfo=timezone.utc).timestamp())
            # a date in the future is a feed mistake: fall back to "first seen"
            return ts if ts <= time.time() + 86400 else None
    return None


def parse_feed(body: str, url: str, limit: int) -> list[FeedItem]:
    parsed = feedparser.parse(body)
    if not parsed.entries and (parsed.bozo or not parsed.version):
        raise FetchError(f"{url} is not a valid RSS/Atom feed")
    items: list[FeedItem] = []
    for entry in parsed.entries[:limit]:
        link = entry.get("link", "") or ""
        guid = entry.get("id") or link or entry.get("title", "")
        if not guid:
            continue
        raw = ""
        if entry.get("content"):
            raw = entry["content"][0].get("value", "")
        raw = raw or entry.get("summary", "")
        summary = BeautifulSoup(raw, "html.parser").get_text(" ", strip=True) if raw else ""
        items.append(FeedItem(
            guid=guid[:500],
            title=re.sub(r"\s+", " ", entry.get("title", "") or "Untitled").strip(),
            url=link,
            published_ts=_entry_ts(entry),
            summary=summary[:3000],
        ))
    return items


def _slug_title(url: str) -> str:
    slug = urlparse(url).path.rstrip("/").rsplit("/", 1)[-1]
    return re.sub(r"[-_]+", " ", slug).strip().capitalize() or url


def parse_sitemap(body: str, include: str, limit: int) -> list[FeedItem]:
    """URLs in a sitemap become items, so new pages (e.g. blog posts) are detected."""
    try:
        root = ET.fromstring(body.encode("utf-8"))
    except ET.ParseError as exc:
        raise FetchError("Not a valid XML sitemap") from exc
    pattern = re.compile(include) if include else None
    items: list[FeedItem] = []
    for url_el in root.iter():
        if not url_el.tag.endswith("url"):
            continue
        loc = lastmod = None
        for child in url_el:
            if child.tag.endswith("loc") and child.text:
                loc = child.text.strip()
            elif child.tag.endswith("lastmod") and child.text:
                lastmod = child.text.strip()
        if not loc or (pattern and not pattern.search(loc)):
            continue
        ts = None
        if lastmod:
            try:
                dt = datetime.fromisoformat(lastmod.replace("Z", "+00:00"))
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                ts = int(dt.timestamp())
            except ValueError:
                ts = None
        items.append(FeedItem(guid=loc, title=_slug_title(loc), url=loc,
                              published_ts=ts, summary=""))
    items.sort(key=lambda i: i.published_ts or 0, reverse=True)
    return items[:limit]
