"""E2E-тест ingestion-графа на мини-корпусе (FakeEmbedder, Qdrant опционален).

Реальная модель не нужна: эмбеддинги считает FakeEmbedder (384 dim, hash).
Если Qdrant на localhost:6333 не поднят — векторная часть пропускается
(pytest.skip), графовая часть всё равно проверяется.
"""

import json
import uuid

import pytest

from backend.app.ingestion.embedder import FakeEmbedder
from backend.app.ingestion.graph_extractor import GraphExtractor
from backend.app.ingestion.graph_store import GraphStore
from backend.app.ingestion.graphs.ingest_graph import build_ingest_graph, IngestDeps
from backend.app.ingestion.qdrant_store import make_client

QDRANT_URL = "http://localhost:6333"

ENTITIES = {
    "persons": [
        {"id": "ivanov_ap", "full_name": "Иванов Алексей Петрович",
         "short_name": "Иванов А.П.", "aliases": ["Иванов Алексей Петрович", "Иванов А.П.", "Иванов"]},
        {"id": "petrova_es", "full_name": "Петрова Елена Сергеевна",
         "short_name": "Петрова Е.С.", "aliases": ["Петрова Елена Сергеевна", "Петрова Е.С.", "Петрова"]},
    ],
    "projects": [
        {"id": "phoenix", "name": "Феникс", "aliases": ["Феникс", "«Феникс»", "проект «Феникс»"]},
        {"id": "vektor", "name": "Вектор", "aliases": ["Вектор", "«Вектор»", "проект «Вектор»"]},
    ],
    "departments": [
        {"id": "it", "name": "ИТ", "aliases": ["ИТ", "отдел ИТ"]},
    ],
}

DOC_TEMPLATE = """---
doc_id: DOC-{num:04d}
title: "{title}"
acl: {acl}
department: "{dep}"
doc_type: {doc_type}
date: 2026-05-1{num}
---

# {title}

{body}
"""

DOCS = [
    dict(num=1, title="Регламент отдела ИТ", acl="public", dep="ИТ", doc_type="regulation",
         body="Автор регламента — Иванов А.П., отдел ИТ. "
              "Проект «Вектор» упоминается как смежная инициатива. "
              "Соблюдение контролируется ежеквартально."),
    dict(num=2, title="Статус проекта «Вектор»", acl="internal", dep="ИТ", doc_type="project_status",
         body="Петрова Е.С. доложила о готовности проекта «Вектор» на девяносто процентов. "
              "Контакт для вопросов: +7-912-345-67-89. Риски переносятся на следующий спринт."),
    dict(num=3, title="Проект «Феникс»: оценка", acl="secret", dep="ИТ", doc_type="project_phoenix",
         body="Закрытые параметры сделки по проекту «Феникс» согласованы Ивановым А.П. "
              "Паспорт уполномоченного лица 4509 123456 сдан в архив. "
              "Бюджет проекта «Феникс» — 240 млн руб."),
]


@pytest.fixture()
def mini_corpus(tmp_path):
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    for d in DOCS:
        (corpus / f"DOC-{d['num']:04d}.md").write_text(
            DOC_TEMPLATE.format(**d), encoding="utf-8")
    entities_path = tmp_path / "entities.json"
    entities_path.write_text(json.dumps(ENTITIES, ensure_ascii=False), encoding="utf-8")
    return corpus, entities_path, tmp_path / "graph.db"


def qdrant_or_skip():
    try:
        client = make_client(QDRANT_URL, timeout=2)
        client.get_collections()
        return client
    except Exception:
        pytest.skip("qdrant not running")


def test_e2e_ingestion(mini_corpus):
    corpus, entities_path, graph_db = mini_corpus
    client = qdrant_or_skip()
    collection = f"securekb_test_{uuid.uuid4().hex[:8]}"

    extractor = GraphExtractor.from_file(entities_path)
    store = GraphStore(graph_db)
    deps = IngestDeps(
        embedder=FakeEmbedder(),
        extractor=extractor,
        store=store,
        client_factory=lambda: client,
        collection=collection,
        recreate=True,
        chunk_size=30,
        chunk_overlap=5,
    )
    result = build_ingest_graph(deps).compile().invoke({"corpus_dir": str(corpus)})

    report = result["report"]
    assert report["docs"] == 3
    assert report["chunks"] >= 3
    assert report["vectors"] == report["chunks"]
    assert report["nodes"] > 0
    assert report["edges"] > 0
    # PII: телефон в DOC-0002 и паспорт в DOC-0003 замаскированы
    assert report["pii_found"] >= 2
    pii_types = {p["type"] for p in result["pii_found"]}
    assert "PHONE" in pii_types and "PASSPORT_RU" in pii_types
    # тексты чанков очищены
    assert all("+7-" not in c.text for c in result["chunks"])
    assert all("4509 123456" not in c.text for c in result["chunks"])

    # Qdrant: точки записаны с payload-метаданными
    info = client.get_collection(collection)
    assert info.points_count == report["chunks"]
    scroll, _ = client.scroll(collection_name=collection, limit=50,
                              with_payload=True, with_vectors=False)
    assert scroll, "коллекция пуста"
    by_acl = {p.payload["acl"] for p in scroll}
    assert by_acl == {"public", "internal", "secret"}
    sample = next(p for p in scroll if p.payload["doc_id"] == "DOC-0001")
    assert sample.payload["chunk_id"].startswith("DOC-0001#")
    assert "entities" in sample.payload
    assert "person:ivanov_ap" in sample.payload["entities"]

    # граф: соседи сущностей Иванова с ACL-фильтром (только public-доступ)
    neighbors_public = store.neighbors(
        ["person:ivanov_ap"], exclude_doc_id="DOC-0003",
        allowed_doc_ids={"DOC-0001"},
    )
    assert neighbors_public == ["DOC-0001"]  # secret/internal отфильтрованы
    neighbors_all = store.neighbors(["person:ivanov_ap"], exclude_doc_id="DOC-0003")
    assert "DOC-0001" in neighbors_all
    assert "DOC-0003" not in neighbors_all  # источник исключён

    # рёбра со-встречаемости заполнены в обе стороны
    assert store.edge_count() > 0

    client.delete_collection(collection_name=collection)
    store.close()


def test_graph_store_persistence_unit(mini_corpus):
    """Графовое хранилище без Qdrant: запись/чтение/фильтры."""
    _, _, graph_db = mini_corpus
    store = GraphStore(graph_db)
    store.add_chunk_entities(
        [("person:ivanov_ap", "person", "Иванов А.П."),
         ("project:phoenix", "project", "Феникс")],
        doc_id="DOC-0009",
    )
    store.add_chunk_entities([("person:ivanov_ap", "person", "Иванов А.П.")], doc_id="DOC-0010")

    assert store.node_count() == 2
    docs = store.neighbors(["person:ivanov_ap"])
    assert set(docs) == {"DOC-0009", "DOC-0010"}
    # co-occurs-ребро связывает Иванова и Феникс: соседи Феникса тянут и доки Иванова
    assert store.neighbors(["project:phoenix"]) == ["DOC-0009", "DOC-0010"]
    # ACL-фильтр отсекает недоступные документы
    assert store.neighbors(["person:ivanov_ap"], allowed_doc_ids={"DOC-0010"}) == ["DOC-0010"]
    assert store.neighbors(["person:ivanov_ap"], exclude_doc_id="DOC-0009") == ["DOC-0010"]
    assert store.neighbors([]) == []
    store.close()
