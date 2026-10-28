"""Оркестрация ретрива: dense (Qdrant) + lexical (BM25 per-ACL) + graph-expand
(SQLite) → RRF-fusion. Одна точка входа для query_graph и тестов."""

from __future__ import annotations

import threading
from collections.abc import Callable
from dataclasses import dataclass

from qdrant_client import QdrantClient

from backend.app.core.config import get_settings
from backend.app.core.logging import get_logger
from backend.app.ingestion.acl import ACL
from backend.app.ingestion.embedder import E5Embedder, Embedder
from backend.app.ingestion.graph_extractor import GraphExtractor
from backend.app.ingestion.graph_store import GraphStore
from backend.app.retrieval.dense import search_dense, visible_doc_ids
from backend.app.retrieval.fuse import RetrievedChunk, rrf_fuse
from backend.app.retrieval.graph_expand import expand
from backend.app.retrieval.lexical import LexicalIndex

logger = get_logger(__name__)


@dataclass(slots=True)
class RetrievalDeps:
    """Зависимости retrieval-стека (инъекция для продакшена и тестов)."""

    client_factory: Callable[[], QdrantClient]
    embedder: Embedder
    extractor: GraphExtractor
    graph_db_path: str
    collection: str


class RetrievalService:
    """Лениво инициализирует клиент Qdrant, BM25-индекс и граф; кэширует
    allowed_doc_ids по уровню ACL (список меняется только после ре-инжеста)."""

    def __init__(self, deps: RetrievalDeps) -> None:
        self.deps = deps
        self._client: QdrantClient | None = None
        self._store: GraphStore | None = None
        self._lexical: LexicalIndex | None = None
        self._visible: dict[ACL, set[str]] = {}
        self._lock = threading.Lock()

    # -- ленивые зависимости ----------------------------------------------------

    @property
    def client(self) -> QdrantClient:
        if self._client is None:
            self._client = self.deps.client_factory()
        return self._client

    @property
    def store(self) -> GraphStore:
        if self._store is None:
            self._store = GraphStore(self.deps.graph_db_path)
        return self._store

    @property
    def lexical(self) -> LexicalIndex:
        """BM25-индекс строится при первом обращении (полный scroll Qdrant)."""
        if self._lexical is None:
            with self._lock:
                if self._lexical is None:
                    logger.info("lexical: строю BM25-индекс из %s",
                                self.deps.collection)
                    index = LexicalIndex.from_qdrant(self.client, self.deps.collection)
                    logger.info("lexical: индекс готов (%d чанков)", index.size())
                    self._lexical = index
        return self._lexical

    def allowed_doc_ids(self, user_acl: ACL | str) -> set[str]:
        """doc_id, доступные пользователю (кэш по уровню ACL)."""
        acl = ACL.parse(user_acl)
        if acl not in self._visible:
            self._visible[acl] = visible_doc_ids(
                acl, client=self.client, collection=self.deps.collection
            )
        return self._visible[acl]

    # -- ветки ретрива ------------------------------------------------------------

    def dense_search(self, question: str, user_acl: ACL | str,
                     top_k: int = 5) -> list[RetrievedChunk]:
        return search_dense(
            question, user_acl, top_k,
            client=self.client, embedder=self.deps.embedder,
            collection=self.deps.collection,
        )

    def lexical_search(self, question: str, user_acl: ACL | str,
                       top_k: int = 5) -> list[RetrievedChunk]:
        return self.lexical.search(question, user_acl, top_k=top_k)

    def graph_search(self, question: str, user_acl: ACL | str,
                     top_k: int = 5) -> list[RetrievedChunk]:
        return expand(
            question, self.allowed_doc_ids(user_acl), top_k,
            extractor=self.deps.extractor, store=self.store,
            client=self.client, collection=self.deps.collection,
            user_acl=user_acl,
        )

    # -- оркестрация ----------------------------------------------------------------

    def orchestrate_retrieval(
        self, question: str, user_acl: ACL | str, top_k: int = 5
    ) -> tuple[list[RetrievedChunk], dict]:
        """Все три ветки + RRF; возвращает (чанки, debug-инфа по веткам)."""
        dense = self.dense_search(question, user_acl, top_k)
        lexical = self.lexical_search(question, user_acl, top_k)
        graph = self.graph_search(question, user_acl, top_k)

        fused = rrf_fuse(
            {"dense": dense, "lexical": lexical, "graph": graph},
            user_acl, top_n=top_k,
        )
        debug = {
            "branches": {
                "dense": [{"chunk_id": c.chunk_id, "score": round(c.score, 4)} for c in dense],
                "lexical": [{"chunk_id": c.chunk_id, "score": round(c.score, 4)} for c in lexical],
                "graph": [{"chunk_id": c.chunk_id} for c in graph],
            },
            "fused": [{"chunk_id": c.chunk_id, "doc_id": c.doc_id, "acl": c.acl.name}
                      for c in fused],
        }
        return fused, debug

    def close(self) -> None:
        if self._store is not None:
            self._store.close()
            self._store = None


_instance: RetrievalService | None = None
_instance_lock = threading.Lock()


def get_retrieval_service() -> RetrievalService:
    """Синглтон на процесс: Qdrant + E5 + граф + BM25 из Settings."""
    global _instance
    with _instance_lock:
        if _instance is None:
            settings = get_settings()
            from backend.app.ingestion.qdrant_store import make_client

            _instance = RetrievalService(
                RetrievalDeps(
                    client_factory=lambda: make_client(settings.qdrant_url),
                    embedder=E5Embedder.get(settings.embedding_model),
                    extractor=GraphExtractor.from_file(settings.entities_path),
                    graph_db_path=settings.graph_db_path,
                    collection=settings.qdrant_collection,
                )
            )
        return _instance
