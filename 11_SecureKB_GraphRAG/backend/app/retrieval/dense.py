"""Dense-поиск по Qdrant с ACL pre-filter (фильтр применяется ДО ранжирования,
поэтому недоступные чанки физически не попадают в выдачу)."""

from __future__ import annotations

from qdrant_client import QdrantClient, models

from backend.app.core.config import get_settings
from backend.app.ingestion.acl import ACL
from backend.app.ingestion.embedder import E5Embedder, Embedder
from backend.app.ingestion.qdrant_store import make_client
from backend.app.retrieval.fuse import RetrievedChunk


def acl_match_any(user_acl: ACL | str) -> models.FieldCondition:
    """Условие acl ∈ уровней, не превышающих user_acl (pre-filter)."""
    level = ACL.parse(user_acl)
    allowed = [m.name for m in ACL if m.value <= level.value]
    return models.FieldCondition(key="acl", match=models.MatchAny(any=allowed))


def acl_filter(user_acl: ACL | str) -> models.Filter:
    return models.Filter(must=[acl_match_any(user_acl)])


def search_dense(
    query: str,
    user_acl: ACL | str,
    top_k: int = 5,
    *,
    client: QdrantClient | None = None,
    embedder: Embedder | None = None,
    collection: str | None = None,
) -> list[RetrievedChunk]:
    """Векторный поиск: e5-эмбеддинг запроса (префикс «query: ») + Qdrant
    query_points с ACL-фильтром. Возвращает чанки, отсортированные по cosine."""
    settings = get_settings()
    client = client or make_client(settings.qdrant_url)
    embedder = embedder or E5Embedder.get(settings.embedding_model)
    collection = collection or settings.qdrant_collection

    vector = embedder.embed_query(query)
    response = client.query_points(
        collection_name=collection,
        query=vector,
        query_filter=acl_filter(user_acl),
        limit=top_k,
        with_payload=True,
    )
    return [
        RetrievedChunk(
            chunk_id=p.payload["chunk_id"],
            doc_id=p.payload["doc_id"],
            title=p.payload["title"],
            acl=ACL.parse(p.payload["acl"]),
            text=p.payload["text"],
            score=float(p.score),
        )
        for p in response.points
    ]


def visible_doc_ids(
    user_acl: ACL | str,
    *,
    client: QdrantClient | None = None,
    collection: str | None = None,
    limit: int = 1024,
) -> set[str]:
    """doc_id всех документов, доступных пользователю по ACL (по чанкам)."""
    settings = get_settings()
    client = client or make_client(settings.qdrant_url)
    collection = collection or settings.qdrant_collection

    docs: set[str] = set()
    offset = None
    while True:
        points, offset = client.scroll(
            collection_name=collection,
            scroll_filter=acl_filter(user_acl),
            limit=limit,
            with_payload=True,
            offset=offset,
        )
        for p in points:
            docs.add(p.payload["doc_id"])
        if offset is None:
            break
    return docs
