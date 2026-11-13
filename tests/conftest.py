"""Shared fixtures. Tests never touch the network: HTTP is served by
``httpx.MockTransport``, the LLM is a scripted fake and embeddings are a tiny
bag-of-words hash, so everything runs offline without API keys."""

import hashlib
import json
import math
import os
import re
import tempfile

_TMP = tempfile.mkdtemp(prefix="mra-test-")
os.environ.update({
    "BASE_URL": "http://llm.test/v1",
    "API_KEY": "test-key",
    "MODEL_NAME": "test-model",
    "DATA_DIR": _TMP,
    "WARMUP": "0",
    "MIN_SCORE": "0.2",
    "LLM_MIN_INTERVAL": "0",
    "REQUEST_DELAY": "0",
    "MIN_CHANGE_CHARS": "10",
    "TELEGRAM_BOT_TOKEN": "",
    "TELEGRAM_CHAT_ID": "",
    "GOOGLE_SERVICE_ACCOUNT_JSON": "",
    "SHEET_ID": "",
    "SOURCES_FROM_SHEET": "false",
})

import httpx  # noqa: E402
import pytest  # noqa: E402

from agent import llm_client  # noqa: E402
from agent.collector import Collector  # noqa: E402
from agent.storage import Storage  # noqa: E402
from agent.vectorstore import VectorStore  # noqa: E402

_WORD = re.compile(r"[a-z0-9]+")
_DIM = 512


class FakeEmbedder:
    """Hashed bag-of-words: texts sharing words get high cosine similarity."""

    def _vec(self, text):
        vec = [0.0] * _DIM
        for word in _WORD.findall(text.lower()):
            if len(word) > 2:
                vec[int(hashlib.md5(word.encode()).hexdigest(), 16) % _DIM] += 1.0
        norm = math.sqrt(sum(v * v for v in vec)) or 1.0
        return [v / norm for v in vec]

    def embed_documents(self, texts):
        return [self._vec(t) for t in texts]

    def embed_query(self, text):
        return self._vec(text)


class FakeLLM:
    """Scripted LLM. Pricing prompts get plans parsed from "## Name $price"
    lines; other prompts get canned JSON / text. Every call is recorded."""

    def __init__(self):
        self.calls: list[list[dict]] = []
        self.rag_reply = "Zapier's Professional plan costs $24.99/month [1]."
        self.fail = False

    def __call__(self, messages):
        self.calls.append(messages)
        if self.fail:
            raise llm_client.LLMError("provider down")
        system = messages[0]["content"]
        user = messages[-1]["content"]
        if "extract pricing plans" in system:
            plans = []
            for name, price, limits in re.findall(
                    r"## (\w[\w+ ]*?) \$(\d+(?:\.\d+)?)/month(?: - (.*))?", user):
                plans.append({"name": name, "price": float(price), "currency": "USD",
                              "period": "month", "limits": limits or ""})
            return json.dumps({"plans": plans})
        if "page of the competitor" in system:
            return json.dumps({"summary": "The page changed.", "importance": "medium",
                               "category": "feature", "why_it_matters": "Worth a look."})
        if "New " in system and "posts were published" in system:
            n = len(re.findall(r"^\[\d+\]", user, re.M))
            return json.dumps({"items": [
                {"n": i, "summary": f"Post {i} summary.", "importance": "medium",
                 "category": "feature", "why_it_matters": ""} for i in range(1, n + 1)]})
        if "Highlights" in system:
            return "Zapier raised prices [1]."
        return self.rag_reply

    def stream(self, messages):
        text = self(messages)
        for i in range(0, len(text), 8):
            yield text[i:i + 8]


@pytest.fixture
def fake_llm(monkeypatch):
    fake = FakeLLM()
    monkeypatch.setattr(llm_client, "call_llm", fake)
    monkeypatch.setattr(llm_client, "stream_llm", fake.stream)
    return fake


@pytest.fixture
def storage():
    s = Storage(":memory:")
    yield s
    s.close()


@pytest.fixture
def vstore(tmp_path):
    return VectorStore(str(tmp_path / "chroma"), "test", FakeEmbedder())


class Site:
    """A fake internet: register url -> (status, body, content-type)."""

    def __init__(self):
        self.pages: dict[str, tuple[int, str, str]] = {}
        self.hits: list[str] = []

    def add(self, url, body, status=200, ctype="text/html; charset=utf-8"):
        self.pages[url] = (status, body, ctype)

    def handler(self, request: httpx.Request):
        url = str(request.url)
        self.hits.append(url)
        if url in self.pages:
            status, body, ctype = self.pages[url]
            return httpx.Response(status, text=body, headers={"content-type": ctype})
        if url.endswith("/robots.txt"):
            return httpx.Response(404, text="")
        return httpx.Response(404, text="not found")


@pytest.fixture
def site():
    return Site()


@pytest.fixture
def collector(site):
    client = httpx.Client(transport=httpx.MockTransport(site.handler), follow_redirects=True)
    return Collector(client=client, delay=0, retries=1)


def pricing_html(plans: list[tuple[str, str]], extra: str = "") -> str:
    """A small pricing page: plans = [(name, "$20/month - 750 tasks"), ...]."""
    blocks = "".join(f"<section><h2>{n} {p}</h2><p>Great for teams of every size.</p></section>"
                     for n, p in plans)
    return (f"<html><head><title>Pricing</title></head><body>"
            f"<nav>Home Pricing Login</nav><div class='cookie-banner'>We use cookies</div>"
            f"<main><h1>Pricing</h1>{blocks}<p>{extra}</p></main>"
            f"<footer>© 2026 Example Inc.</footer></body></html>")


def rss(items: list[tuple[str, str, str]]) -> str:
    """items = [(guid, title, pubDate RFC822)]."""
    body = "".join(f"<item><guid>{g}</guid><title>{t}</title><link>{g}</link>"
                   f"<pubDate>{d}</pubDate><description>About {t}.</description></item>"
                   for g, t, d in items)
    return f'<?xml version="1.0"?><rss version="2.0"><channel><title>Blog</title>{body}</channel></rss>'
