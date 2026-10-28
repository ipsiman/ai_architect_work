"""Тесты graph-expand: сущности вопроса и уважение allowed_doc_ids."""

import json

from backend.app.ingestion.acl import ACL
from backend.app.ingestion.graph_extractor import GraphExtractor
from backend.app.ingestion.graph_store import GraphStore
from backend.app.retrieval.graph_expand import expand_doc_ids, extract_entities_from_question

ENTITIES = {
    "persons": [
        {"id": "ivanov_ap", "full_name": "Иванов Алексей Петрович",
         "aliases": ["Иванов Алексей Петрович", "Иванов А.П.", "Иванов"]},
    ],
    "projects": [
        {"id": "phoenix", "name": "Феникс", "aliases": ["Феникс", "«Феникс»", "проект «Феникс»"]},
    ],
    "departments": [{"id": "it", "name": "ИТ", "aliases": ["ИТ", "отдел ИТ"]}],
}


def make_extractor() -> GraphExtractor:
    return GraphExtractor(json.loads(json.dumps(ENTITIES)))


def make_store(tmp_path) -> GraphStore:
    store = GraphStore(tmp_path / "graph.db")
    # DOC-0001 (public): Иванов + Феникс; DOC-0002 (internal): Иванов; DOC-0003 (secret): Феникс
    store.add_chunk_entities(
        [("person:ivanov_ap", "person", "Иванов А.П."),
         ("project:phoenix", "project", "Феникс")], doc_id="DOC-0001")
    store.add_chunk_entities([("person:ivanov_ap", "person", "Иванов А.П.")],
                             doc_id="DOC-0002")
    store.add_chunk_entities([("project:phoenix", "project", "Феникс")],
                             doc_id="DOC-0003")
    return store


def test_extract_entities_from_question(tmp_path):
    extractor = make_extractor()
    q = "Какой бюджет у проекта «Феникс» и что думает Иванов А.П.?"
    assert set(extract_entities_from_question(q, extractor)) == {
        "project:phoenix", "person:ivanov_ap",
    }
    # вопрос без сущностей — пустой результат
    assert extract_entities_from_question("какая погода?", extractor) == []


def test_expand_respects_allowed_doc_ids(tmp_path):
    """КЛЮЧЕВОЕ: expand возвращает только документы из allowed_doc_ids —
    secret-документ не попадает в выдачу public-пользователя."""
    extractor = make_extractor()
    store = make_store(tmp_path)

    # public-пользователь: доступен только DOC-0001
    public_docs = expand_doc_ids(
        "Какой бюджет у проекта «Феникс»?",
        allowed_doc_ids={"DOC-0001"},
        extractor=extractor, store=store,
    )
    assert public_docs == ["DOC-0001"]
    assert "DOC-0003" not in public_docs  # secret-документ отфильтрован

    # secret-пользователь: доступны все три документа
    all_docs = expand_doc_ids(
        "Какой бюджет у проекта «Феникс»?",
        allowed_doc_ids={"DOC-0001", "DOC-0002", "DOC-0003"},
        extractor=extractor, store=store,
    )
    assert set(all_docs) == {"DOC-0001", "DOC-0002", "DOC-0003"}
    # DOC-0002 связан с Ивановым (co_occurs с Фениксом в DOC-0001 → 1 hop)


def test_expand_no_entities_returns_empty(tmp_path):
    extractor = make_extractor()
    store = make_store(tmp_path)
    assert expand_doc_ids(
        "погода на выходных", allowed_doc_ids={"DOC-0001"},
        extractor=extractor, store=store,
    ) == []


def test_expand_empty_allowed_returns_empty(tmp_path):
    extractor = make_extractor()
    store = make_store(tmp_path)
    assert expand_doc_ids(
        "бюджет Феникс", allowed_doc_ids=set(),
        extractor=extractor, store=store,
    ) == []


def test_expand_limit(tmp_path):
    extractor = make_extractor()
    store = make_store(tmp_path)
    docs = expand_doc_ids(
        "бюджет Феникс", allowed_doc_ids={"DOC-0001", "DOC-0002", "DOC-0003"},
        extractor=extractor, store=store, limit=2,
    )
    assert len(docs) == 2
