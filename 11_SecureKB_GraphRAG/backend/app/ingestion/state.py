"""Состояние и отчёт ingestion-конвейера (этап B)."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, TypedDict

from backend.app.ingestion.chunker import Chunk
from backend.app.ingestion.parser import Document


class IngestState(TypedDict, total=False):
    """Состояние графа LangGraph: узлы дописывают свои ключи."""

    corpus_dir: str
    docs: list[Document]
    chunks: list[Chunk]
    vectors: list[list[float]]
    entities_found: list[dict[str, list[str]]]  # extract() по каждому чанку
    pii_found: list[dict]  # {"chunk_id": ..., "type": ..., "value": ...}
    report: dict[str, Any]


@dataclass(slots=True)
class IngestReport:
    """Итоговые счётчики одного прогона ingestion."""

    docs: int = 0
    chunks: int = 0
    vectors: int = 0
    nodes: int = 0
    edges: int = 0
    pii_found: int = 0

    def as_dict(self) -> dict[str, int]:
        return {
            "docs": self.docs,
            "chunks": self.chunks,
            "vectors": self.vectors,
            "nodes": self.nodes,
            "edges": self.edges,
            "pii_found": self.pii_found,
        }
