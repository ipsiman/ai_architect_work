"""LangGraph-конвейер инжеста: parse → chunk → pii → extract → embed → store_vec → store_graph.

Отличие от порядка в ТЗ: extract выполняется ДО store_vec, а не после —
иначе payload точек Qdrant не смог бы содержать найденные сущности
(требование «payload: …, entities»). На результат это не влияет.

run_ingestion() — публичная точка входа: поднимает зависимости из Settings
и возвращает IngestReport.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from langgraph.graph import END, START, StateGraph

from backend.app.core.config import get_settings
from backend.app.core.logging import get_logger
from backend.app.ingestion.chunker import make_chunks
from backend.app.ingestion.embedder import E5Embedder, Embedder
from backend.app.ingestion.graph_extractor import GraphExtractor
from backend.app.ingestion.graph_store import GraphStore
from backend.app.ingestion.parser import read_corpus
from backend.app.ingestion.pii import PIIDetector
from backend.app.ingestion.qdrant_store import ensure_collection, make_client, upsert_chunks
from backend.app.ingestion.state import IngestReport, IngestState

logger = get_logger(__name__)


@dataclass(slots=True)
class IngestDeps:
    """Зависимости конвейера (подставляются в узлы замыканием)."""

    embedder: Embedder
    extractor: GraphExtractor
    store: GraphStore
    client_factory: Callable[[], object]  # ленивое подключение к Qdrant
    collection: str
    recreate: bool
    chunk_size: int
    chunk_overlap: int


def build_ingest_graph(deps: IngestDeps) -> "StateGraph":
    """Собирает (не компилирует) граф инжеста с зависимостями deps."""
    detector = PIIDetector()

    def parse_node(state: IngestState) -> IngestState:
        docs = read_corpus(state["corpus_dir"])
        logger.info("parse: %d документов из %s", len(docs), state["corpus_dir"])
        return {"docs": docs}

    def chunk_node(state: IngestState) -> IngestState:
        chunks = []
        for doc in state["docs"]:
            chunks.extend(
                make_chunks(doc, size=deps.chunk_size, overlap=deps.chunk_overlap)
            )
        logger.info("chunk: %d чанков", len(chunks))
        return {"chunks": chunks}

    def pii_node(state: IngestState) -> IngestState:
        found: list[dict] = []
        for chunk in state["chunks"]:
            masked, hits = detector.mask(chunk.text)
            chunk.text = masked
            for hit in hits:
                found.append({"chunk_id": chunk.chunk_id, **hit})
        logger.info("pii: замаскировано %d вхождений", len(found))
        return {"pii_found": found}

    def extract_node(state: IngestState) -> IngestState:
        entities_found = []
        for chunk in state["chunks"]:
            extracted = deps.extractor.extract(chunk.text)
            entities_found.append(extracted)
            chunk.entities = deps.extractor.node_ids(chunk.text)
        total = sum(len(ids) for ids in entities_found)
        logger.info("extract: %d упоминаний сущностей", total)
        return {"entities_found": entities_found}

    def embed_node(state: IngestState) -> IngestState:
        vectors = deps.embedder.embed_passages([c.text for c in state["chunks"]])
        logger.info("embed: %d векторов", len(vectors))
        return {"vectors": vectors}

    def store_vec_node(state: IngestState) -> IngestState:
        client = deps.client_factory()
        ensure_collection(client, deps.collection, recreate=deps.recreate)
        upserted = upsert_chunks(client, deps.collection, state["chunks"], state["vectors"])
        logger.info("store_vec: %d точек в коллекции %s", upserted, deps.collection)
        return {"vectors": state["vectors"]}

    def store_graph_node(state: IngestState) -> IngestState:
        for chunk in state["chunks"]:
            nodes = []
            for node_id in chunk.entities:
                info = deps.extractor.node_info(node_id)
                if info:
                    nodes.append((node_id, info["type"], info["name"]))
            if nodes:
                deps.store.add_chunk_entities(nodes, doc_id=chunk.doc_id)
        report = {
            "docs": len(state["docs"]),
            "chunks": len(state["chunks"]),
            "vectors": len(state["vectors"]),
            "nodes": deps.store.node_count(),
            "edges": deps.store.edge_count(),
            "pii_found": len(state["pii_found"]),
        }
        logger.info("store_graph: %d узлов, %d рёбер", report["nodes"], report["edges"])
        return {"report": report}

    graph = StateGraph(IngestState)
    graph.add_node("parse", parse_node)
    graph.add_node("chunk", chunk_node)
    graph.add_node("pii", pii_node)
    graph.add_node("extract", extract_node)
    graph.add_node("embed", embed_node)
    graph.add_node("store_vec", store_vec_node)
    graph.add_node("store_graph", store_graph_node)
    graph.add_edge(START, "parse")
    graph.add_edge("parse", "chunk")
    graph.add_edge("chunk", "pii")
    graph.add_edge("pii", "extract")
    graph.add_edge("extract", "embed")
    graph.add_edge("embed", "store_vec")
    graph.add_edge("store_vec", "store_graph")
    graph.add_edge("store_graph", END)
    return graph


def run_ingestion(
    corpus_dir: str | None = None,
    embedder: Embedder | None = None,
    qdrant_url: str | None = None,
    collection: str | None = None,
    recreate: bool = False,
    graph_db_path: str | None = None,
    entities_path: str | None = None,
    chunk_size: int | None = None,
    chunk_overlap: int | None = None,
) -> IngestReport:
    """Полный прогон инжеста; возвращает IngestReport со счётчиками."""
    settings = get_settings()
    deps = IngestDeps(
        embedder=embedder or E5Embedder.get(settings.embedding_model),
        extractor=GraphExtractor.from_file(entities_path or settings.entities_path),
        store=GraphStore(graph_db_path or settings.graph_db_path),
        client_factory=lambda: make_client(qdrant_url or settings.qdrant_url),
        collection=collection or settings.qdrant_collection,
        recreate=recreate,
        chunk_size=chunk_size or settings.chunk_size_tokens,
        chunk_overlap=chunk_overlap or settings.chunk_overlap_tokens,
    )
    app = build_ingest_graph(deps).compile()
    final: IngestState = app.invoke({"corpus_dir": corpus_dir or settings.corpus_dir})
    report = IngestReport(**final["report"])
    deps.store.close()
    return report
