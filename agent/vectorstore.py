"""Chroma vector store with local fastembed embeddings (free, no rate limits).

Every chunk carries metadata (competitor, doc_type, date_ts, doc_id, url), so
questions can be narrowed down *before* the similarity search, for example
"only Zapier, only the last 90 days".
"""

from __future__ import annotations

import hashlib
import logging
import os
import threading
from pathlib import Path
from typing import Protocol

from agent.config import PROJECT_ROOT, get_settings
from agent.models import Chunk, SearchHit

logger = logging.getLogger(__name__)

_BATCH = 64


class Embedder(Protocol):
    def embed_documents(self, texts: list[str]) -> list[list[float]]: ...
    def embed_query(self, text: str) -> list[float]: ...


class FastEmbedder:
    """fastembed (ONNX, CPU). The model (~130 MB) downloads once on first use."""

    def __init__(self, model_name: str):
        from fastembed import TextEmbedding

        cache_dir = os.getenv("FASTEMBED_CACHE_PATH", str(PROJECT_ROOT / ".cache" / "fastembed"))
        logger.info("Loading embedding model %s", model_name)
        self._model = TextEmbedding(model_name=model_name, cache_dir=cache_dir)

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return [v.tolist() for v in self._model.embed(texts, batch_size=_BATCH)]

    def embed_query(self, text: str) -> list[float]:
        return next(iter(self._model.query_embed(text))).tolist()


class VectorStore:
    def __init__(self, path: str, collection: str, embedder: Embedder):
        import chromadb
        from chromadb.config import Settings as ChromaSettings

        Path(path).mkdir(parents=True, exist_ok=True)
        self._client = chromadb.PersistentClient(
            path=path, settings=ChromaSettings(anonymized_telemetry=False))
        self._name = collection
        self._embedder = embedder
        self._lock = threading.Lock()
        self._collection = self._open()

    def _open(self):
        return self._client.get_or_create_collection(
            name=self._name, metadata={"hnsw:space": "cosine"}, embedding_function=None)

    # ------------------------------------------------------------ write
    def add_chunks(self, chunks: list[Chunk]) -> int:
        if not chunks:
            return 0
        embeddings = self._embedder.embed_documents([c.text for c in chunks])
        ids, metas = [], []
        for c in chunks:
            ids.append(hashlib.sha1(f"{c.doc_id}|{c.chunk_index}".encode()).hexdigest())
            metas.append({"doc_id": c.doc_id, "title": c.title, "url": c.url,
                          "competitor": c.competitor, "doc_type": c.doc_type,
                          "date_ts": int(c.date_ts), "chunk_index": c.chunk_index,
                          "content_hash": c.content_hash, "heading": c.heading})
        with self._lock:
            for start in range(0, len(ids), _BATCH * 4):
                end = start + _BATCH * 4
                self._collection.upsert(ids=ids[start:end], embeddings=embeddings[start:end],
                                        documents=[c.text for c in chunks[start:end]],
                                        metadatas=metas[start:end])
        return len(ids)

    def replace_doc(self, doc_id: str, chunks: list[Chunk]) -> int:
        self.delete_doc(doc_id)
        return self.add_chunks(chunks)

    def delete_doc(self, doc_id: str) -> int:
        with self._lock:
            existing = self._collection.get(where={"doc_id": doc_id}, include=[])
            if existing["ids"]:
                self._collection.delete(ids=existing["ids"])
            return len(existing["ids"])

    def reset(self) -> None:
        with self._lock:
            self._client.delete_collection(self._name)
            self._collection = self._open()

    # ------------------------------------------------------------ read
    def count(self) -> int:
        return self._collection.count()

    def indexed_docs(self) -> dict[str, str]:
        """{doc_id: content_hash} of everything in the index."""
        data = self._collection.get(include=["metadatas"])
        return {m["doc_id"]: m.get("content_hash", "") for m in data["metadatas"]}

    def search(self, query: str, k: int, where: dict | None = None) -> list[SearchHit]:
        total = self.count()
        if total == 0:
            return []
        try:
            result = self._collection.query(
                query_embeddings=[self._embedder.embed_query(query)],
                n_results=min(k, total), where=where or None,
                include=["documents", "metadatas", "distances"])
        except Exception as exc:  # e.g. the filter matches nothing
            logger.info("Search returned nothing (%s)", type(exc).__name__)
            return []
        hits = []
        for text, meta, dist in zip(result["documents"][0], result["metadatas"][0],
                                    result["distances"][0]):
            hits.append(SearchHit(
                text=text, doc_id=meta.get("doc_id", ""), title=meta.get("title", ""),
                url=meta.get("url", ""), competitor=meta.get("competitor", ""),
                doc_type=meta.get("doc_type", ""), date_ts=int(meta.get("date_ts", 0)),
                score=round(1.0 - float(dist), 4)))
        return hits


_store: VectorStore | None = None
_store_lock = threading.Lock()


def get_store() -> VectorStore:
    global _store
    if _store is None:
        with _store_lock:
            if _store is None:
                s = get_settings()
                _store = VectorStore(s.chroma_path, s.collection_name,
                                     FastEmbedder(s.embedding_model))
    return _store
