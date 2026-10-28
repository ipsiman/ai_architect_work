"""Тесты dense-поиска (Qdrant-зависимые; skip, если Qdrant не поднят)."""

import uuid

import pytest

from backend.app.ingestion.acl import ACL
from backend.app.ingestion.chunker import Chunk
from backend.app.ingestion.embedder import FakeEmbedder
from backend.app.ingestion.qdrant_store import (
    ensure_collection,
    make_client,
    upsert_chunks,
)
from backend.app.retrieval.dense import search_dense, visible_doc_ids

QDRANT_URL = "http://localhost:6333"


def chunk(chunk_id: str, acl: ACL, text: str) -> Chunk:
    doc_id = chunk_id.split("#")[0]
    return Chunk(chunk_id=chunk_id, doc_id=doc_id, title=f"t {doc_id}", acl=acl,
                 department="ИТ", text=text)


@pytest.fixture(scope="module")
def qdrant():
    try:
        client = make_client(QDRANT_URL, timeout=2)
        client.get_collections()
    except Exception:
        pytest.skip("qdrant not running")
    yield client


@pytest.fixture()
def collection(qdrant):
    name = f"securekb_dense_test_{uuid.uuid4().hex[:8]}"
    embedder = FakeEmbedder()
    chunks = [
        chunk("DOC-0001#0", ACL.public, "Регламент работы со служебной информацией отдела ИТ."),
        chunk("DOC-0051#0", ACL.internal, "Статус проекта «Вектор»: автотесты пройдены на 78 процентов."),
        chunk("DOC-0086#0", ACL.secret, "Бюджет проекта «Феникс» — 240 млн руб."),
    ]
    ensure_collection(qdrant, name, recreate=True)
    upsert_chunks(qdrant, name, chunks, embedder.embed_passages([c.text for c in chunks]))
    yield name
    qdrant.delete_collection(collection_name=name)


def test_dense_prefilters_by_acl(qdrant, collection):
    """Pre-filter: public-пользователь не получает internal/secret чанки."""
    hits = search_dense(
        "бюджет проекта Феникс", ACL.public, top_k=5,
        client=qdrant, embedder=FakeEmbedder(), collection=collection,
    )
    assert hits, "public-чанк должен находиться"
    assert all(h.acl == ACL.public for h in hits)
    assert all(h.doc_id == "DOC-0001" for h in hits)


def test_dense_secret_user_sees_all(qdrant, collection):
    hits = search_dense(
        "бюджет проекта Феникс", ACL.secret, top_k=5,
        client=qdrant, embedder=FakeEmbedder(), collection=collection,
    )
    acls = {h.acl for h in hits}
    assert ACL.secret in acls
    assert {h.doc_id for h in hits} == {"DOC-0001", "DOC-0051", "DOC-0086"}
    # score убывает
    scores = [h.score for h in hits]
    assert scores == sorted(scores, reverse=True)


def test_dense_top_k_limit(qdrant, collection):
    hits = search_dense(
        "вектор автотесты", ACL.internal, top_k=1,
        client=qdrant, embedder=FakeEmbedder(), collection=collection,
    )
    assert len(hits) == 1


def test_visible_doc_ids(qdrant, collection):
    assert visible_doc_ids(ACL.public, client=qdrant, collection=collection) == {"DOC-0001"}
    assert visible_doc_ids(ACL.internal, client=qdrant, collection=collection) == {"DOC-0001", "DOC-0051"}
    assert visible_doc_ids(ACL.secret, client=qdrant, collection=collection) == {
        "DOC-0001", "DOC-0051", "DOC-0086",
    }
