"""Graph-expand: сущности вопроса → смежные документы (SQLite-граф) → чанки.

Идея: если в вопросе упомянута сущность (человек/проект/департамент),
graph_store.neighbors находит связанные с ней документы; их чанки добавляются
в ретрив как буст-кандидаты. Ключевой фильтр безопасности — allowed_doc_ids
(документы, доступные пользователю по ACL): недоступные документы отсекаются
ещё до обращения к Qdrant.
"""

from __future__ import annotations

from qdrant_client import QdrantClient, models

from backend.app.ingestion.acl import ACL
from backend.app.ingestion.graph_extractor import GraphExtractor
from backend.app.ingestion.graph_store import GraphStore
from backend.app.retrieval.dense import acl_match_any
from backend.app.retrieval.fuse import RetrievedChunk


def extract_entities_from_question(question: str,
                                   extractor: GraphExtractor) -> list[str]:
    """node_id сущностей, упомянутых в вопросе (gazetteer по entities.json)."""
    return extractor.node_ids(question)


def expand_doc_ids(question: str, allowed_doc_ids: set[str], *,
                   extractor: GraphExtractor, store: GraphStore,
                   limit: int = 50) -> list[str]:
    """Документы, смежные с сущностями вопроса, ДО allowed_doc_ids фильтра
    применяется внутри neighbors: возвращаются только доступные документы."""
    entity_ids = extract_entities_from_question(question, extractor)
    if not entity_ids:
        return []
    return store.neighbors(entity_ids, allowed_doc_ids=allowed_doc_ids, limit=limit)


def expand(question: str, allowed_doc_ids: set[str], top_k: int = 5, *,
           extractor: GraphExtractor, store: GraphStore,
           client: QdrantClient | None = None,
           collection: str | None = None,
           user_acl: ACL | str | None = None) -> list[RetrievedChunk]:
    """Graph-expand кандидат: по одному чанку на каждый смежный документ.

    Чанки достаются из Qdrant фильтром doc_id ∈ смежных ДОКУМЕНТАХ; при этом
    повторно применяется ACL-фильтр (defense-in-depth: даже если allowed_doc_ids
    ошибочно шире прав пользователя, секретные чанки не вернутся).
    """
    from backend.app.core.config import get_settings

    doc_ids = expand_doc_ids(question, allowed_doc_ids,
                             extractor=extractor, store=store, limit=top_k * 2)
    if not doc_ids:
        return []

    settings = get_settings()
    client = client or _default_client()
    collection = collection or settings.qdrant_collection

    must = [models.FieldCondition(
        key="doc_id", match=models.MatchAny(any=doc_ids),
    )]
    if user_acl is not None:
        must.append(acl_match_any(user_acl))

    points, _ = client.scroll(
        collection_name=collection,
        scroll_filter=models.Filter(must=must),
        limit=len(doc_ids) * 2,  # до двух чанков на документ
        with_payload=True,
    )
    by_doc: dict[str, list[dict]] = {}
    for p in points:
        by_doc.setdefault(p.payload["doc_id"], []).append(p.payload)
    for doc_chunks in by_doc.values():
        doc_chunks.sort(key=lambda p: p["chunk_id"])  # первый чанк документа

    results: list[RetrievedChunk] = []
    for doc_id in doc_ids:  # порядок соседства из графа = релевантность
        payloads = by_doc.get(doc_id)
        if not payloads:
            continue
        p = payloads[0]
        results.append(
            RetrievedChunk(
                chunk_id=p["chunk_id"],
                doc_id=p["doc_id"],
                title=p["title"],
                acl=ACL.parse(p["acl"]),
                text=p["text"],
                score=0.0,  # буст-кандидат: вклад через ранг в RRF, не через score
            )
        )
        if len(results) >= top_k:
            break
    return results


def _default_client() -> QdrantClient:
    from backend.app.ingestion.qdrant_store import make_client

    return make_client(get_settings().qdrant_url)
