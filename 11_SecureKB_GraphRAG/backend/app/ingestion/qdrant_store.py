"""Хранилище чанков в Qdrant (коллекция securekb_chunks)."""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from pathlib import Path

from qdrant_client import QdrantClient, models

from backend.app.ingestion.chunker import Chunk

VECTOR_SIZE = 384  # intfloat/multilingual-e5-small
_NAMESPACE = uuid.UUID("6ba7b810-9dad-11d1-80b4-00c04fd430c8")  # dns-namespace, детерминизм


def point_id_for(chunk_id: str) -> str:
    """Детерминированный UUID v5 по chunk_id (повторный инжест — тот же id)."""
    return str(uuid.uuid5(_NAMESPACE, chunk_id))


def make_client(url: str, timeout: float = 10.0) -> QdrantClient:
    """HTTP-клиент Qdrant (совместимость major-версий не блокирует работу)."""
    return QdrantClient(
        url=url, timeout=timeout, prefer_grpc=False, check_compatibility=False
    )


def ensure_collection(client: QdrantClient, collection: str,
                      recreate: bool = False, size: int = VECTOR_SIZE) -> None:
    """Создаёт коллекцию (cosine); при recreate — удаляет существующую."""
    exists = client.collection_exists(collection_name=collection)
    if recreate and exists:
        client.delete_collection(collection_name=collection)
        exists = False
    if not exists:
        client.create_collection(
            collection_name=collection,
            vectors_config=models.VectorParams(size=size, distance=models.Distance.COSINE),
        )


def chunk_payload(chunk: Chunk) -> dict:
    """Payload точки Qdrant: метаданные + текст + найденные сущности."""
    return {
        "chunk_id": chunk.chunk_id,
        "doc_id": chunk.doc_id,
        "title": chunk.title,
        "acl": chunk.acl.name,
        "department": chunk.department,
        "text": chunk.text,
        "entities": chunk.entities,
    }


def upsert_chunks(client: QdrantClient, collection: str,
                  chunks: Sequence[Chunk], vectors: Sequence[Sequence[float]]) -> int:
    """Загружает чанки с векторами батчами; возвращает число точек."""
    if len(chunks) != len(vectors):
        raise ValueError(f"chunks ({len(chunks)}) and vectors ({len(vectors)}) mismatch")
    points = [
        models.PointStruct(
            id=point_id_for(chunk.chunk_id),
            vector=list(vec),
            payload=chunk_payload(chunk),
        )
        for chunk, vec in zip(chunks, vectors)
    ]
    batch = 256
    for i in range(0, len(points), batch):
        client.upsert(collection_name=collection, points=points[i : i + batch])
    return len(points)
