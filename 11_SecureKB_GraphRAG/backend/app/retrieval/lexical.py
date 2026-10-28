"""Лексический (BM25) поиск с per-ACL изоляцией.

Индекс строится из ВСЕХ чанков Qdrant (scroll) при старте; на каждый уровень
ACL — СВОЙ BM25Okapi. Пользователь ищет ТОЛЬКО в индексе своего уровня:
secret-чанк физически отсутствует в индексе public-уровня, пост-фильтрация
не нужна и утечка невозможна by construction.
"""

from __future__ import annotations

import re

from qdrant_client import QdrantClient
from rank_bm25 import BM25Okapi

from backend.app.ingestion.acl import ACL
from backend.app.retrieval.fuse import RetrievedChunk

_TOKEN_RE = re.compile(r"\w\w+", re.UNICODE)  # токены из 2+ букв/цифр (RU)


def tokenize(text: str) -> list[str]:
    """lowercase + выделение слов длиной 2+ символа (юникодные границы)."""
    return _TOKEN_RE.findall(text.lower())


class LexicalIndex:
    """Набор BM25-индексов: по одному на уровень ACL."""

    def __init__(self) -> None:
        self._corpora: dict[ACL, list[dict]] = {}
        self._bm25: dict[ACL, BM25Okapi] = {}

    # -- построение -----------------------------------------------------------

    def add_payload(self, payload: dict) -> None:
        """Добавляет чанк (payload точки Qdrant) в корпус своего уровня."""
        acl = ACL.parse(payload["acl"])
        self._corpora.setdefault(acl, []).append(payload)

    def build(self) -> None:
        """Компилирует BM25 по каждому непустому корпусу."""
        for acl, corpus in self._corpora.items():
            tokenized = [tokenize(p["text"]) for p in corpus]
            if not any(tokenized):
                continue
            self._bm25[acl] = BM25Okapi(tokenized)

    @classmethod
    def from_payloads(cls, payloads: list[dict]) -> "LexicalIndex":
        """Индекс из готовых payload'ов (тесты / внешний scroll)."""
        index = cls()
        for payload in payloads:
            index.add_payload(payload)
        index.build()
        return index

    @classmethod
    def from_qdrant(cls, client: QdrantClient, collection: str,
                    limit: int = 256) -> "LexicalIndex":
        """Полный scroll коллекции и построение per-ACL индексов."""
        index = cls()
        offset = None
        while True:
            points, offset = client.scroll(
                collection_name=collection,
                limit=limit,
                with_payload=True,
                offset=offset,
            )
            for p in points:
                index.add_payload(p.payload)
            if offset is None:
                break
        index.build()
        return index

    # -- поиск ----------------------------------------------------------------

    def search(self, query: str, user_acl: ACL | str, top_k: int = 5) -> list[RetrievedChunk]:
        """BM25-поиск в индексе уровня пользователя (изоляция, без пост-фильтра)."""
        acl = ACL.parse(user_acl)
        bm25 = self._bm25.get(acl)
        corpus = self._corpora.get(acl, [])
        if bm25 is None or not corpus:
            return []
        tokens = tokenize(query)
        if not tokens:
            return []
        scores = bm25.get_scores(tokens)
        # детерминированный порядок: score desc, затем chunk_id
        order = sorted(range(len(corpus)), key=lambda i: (-scores[i], corpus[i]["chunk_id"]))
        results = []
        for i in order[:top_k]:
            if scores[i] <= 0:
                break  # дальше только нулевые совпадения
            p = corpus[i]
            results.append(
                RetrievedChunk(
                    chunk_id=p["chunk_id"],
                    doc_id=p["doc_id"],
                    title=p["title"],
                    acl=ACL.parse(p["acl"]),
                    text=p["text"],
                    score=float(scores[i]),
                )
            )
        return results

    def size(self, user_acl: ACL | str | None = None) -> int:
        """Число чанков в индексе (уровня user_acl, если задан)."""
        if user_acl is None:
            return sum(len(c) for c in self._corpora.values())
        return len(self._corpora.get(ACL.parse(user_acl), []))


def search_lexical(query: str, user_acl: ACL | str, top_k: int = 5, *,
                   index: LexicalIndex) -> list[RetrievedChunk]:
    """Функциональная обёртка над LexicalIndex.search."""
    return index.search(query, user_acl, top_k=top_k)
