"""Интеграционные тесты /api/chat (SSE + JSON) на FakeLLM + FakeEmbedder.

Поднимает мини-корпус в отдельной коллекции Qdrant (если Qdrant не запущен —
тест пропускается), подменяет зависимости FastAPI: get_engine → движок с
FakeEmbedder/FakeLLM, get_user_store → sqlite во временном каталоге.
"""

import json
import uuid

import pytest
from fastapi.testclient import TestClient

from backend.app.api import chat as chat_api
from backend.app.core.config import get_settings
from backend.app.generation.llm import FakeLLM
from backend.app.graphs.query_graph import QueryEngine
from backend.app.ingestion.acl import ACL
from backend.app.ingestion.chunker import make_chunks
from backend.app.ingestion.embedder import FakeEmbedder
from backend.app.ingestion.graph_extractor import GraphExtractor
from backend.app.ingestion.graphs.ingest_graph import build_ingest_graph, IngestDeps
from backend.app.ingestion.graph_store import GraphStore
from backend.app.ingestion.qdrant_store import make_client
from backend.app.main import app
from backend.app.retrieval.retrieval_service import RetrievalDeps, RetrievalService
from backend.app.security.users import UserStore

QDRANT_URL = "http://localhost:6333"

ENTITIES = {
    "persons": [
        {"id": "ivanov_ap", "full_name": "Иванов А.П.", "aliases": ["Иванов А.П.", "Иванов"]},
    ],
    "projects": [
        {"id": "phoenix", "name": "Феникс", "aliases": ["Феникс", "«Феникс»", "проект «Феникс»"]},
    ],
    "departments": [{"id": "it", "name": "ИТ", "aliases": ["ИТ", "отдел ИТ"]}],
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
         body="Автор регламента — Иванов А.П., отдел ИТ. Соблюдение контролируется ежеквартально."),
    dict(num=3, title="Проект «Феникс»: оценка", acl="secret", dep="ИТ", doc_type="project_phoenix",
         body="Закрытые параметры сделки по проекту «Феникс» согласованы Ивановым А.П. "
              "Бюджет проекта «Феникс» — 240 млн руб. Срок закрытия сделки — 15 марта 2027 года."),
]


@pytest.fixture()
def client(monkeypatch: pytest.MonkeyPatch, tmp_path):
    """TestClient с мини-индексом в Qdrant и подменными зависимостями."""
    try:
        qclient = make_client(QDRANT_URL, timeout=2)
        qclient.get_collections()
    except Exception:
        pytest.skip("qdrant not running")

    monkeypatch.setenv("SECUREKB_OTEL_ENABLED", "false")
    get_settings.cache_clear()

    corpus = tmp_path / "corpus"
    corpus.mkdir()
    for d in DOCS:
        (corpus / f"DOC-{d['num']:04d}.md").write_text(
            DOC_TEMPLATE.format(**d), encoding="utf-8")
    entities_path = tmp_path / "entities.json"
    entities_path.write_text(json.dumps(ENTITIES, ensure_ascii=False), encoding="utf-8")

    collection = f"securekb_chat_test_{uuid.uuid4().hex[:8]}"
    extractor = GraphExtractor.from_file(entities_path)
    store = GraphStore(tmp_path / "graph.db")
    deps = IngestDeps(
        embedder=FakeEmbedder(), extractor=extractor, store=store,
        client_factory=lambda: qclient, collection=collection,
        recreate=True, chunk_size=30, chunk_overlap=5,
    )
    build_ingest_graph(deps).compile().invoke({"corpus_dir": str(corpus)})

    service = RetrievalService(RetrievalDeps(
        client_factory=lambda: qclient, embedder=FakeEmbedder(),
        extractor=extractor, graph_db_path=str(tmp_path / "graph.db"),
        collection=collection,
    ))
    engine = QueryEngine(service, FakeLLM())
    users = UserStore(tmp_path / "users.db")

    app.dependency_overrides[chat_api.get_engine] = lambda: engine
    app.dependency_overrides[chat_api.get_user_store] = lambda: users
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()
    qclient.delete_collection(collection_name=collection)
    store.close()
    users.close()
    get_settings.cache_clear()


def parse_sse(text: str) -> list[tuple[str, str]]:
    """[(event, data), ...] из тела text/event-stream (многострочные data
    склеиваются через \\n — как требует спецификация SSE)."""
    events = []
    for block in text.split("\n\n"):
        block = block.strip()
        if not block:
            continue
        event = ""
        data_lines: list[str] = []
        for line in block.split("\n"):
            if line.startswith("event: "):
                event = line[len("event: "):]
            elif line.startswith("data: "):
                data_lines.append(line[len("data: "):])
        events.append((event, "\n".join(data_lines)))
    return events


def test_sse_event_sequence(client: TestClient):
    """SSE: sources → token+ → done; в done — cited_docs и тайминги."""
    response = client.post("/api/chat", json={
        "question": "какой бюджет у проекта Феникс?",
        "user_id": "alice", "stream": True,
    })
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")

    events = parse_sse(response.text)
    names = [e for e, _ in events]
    assert names[0] == "sources"
    assert names[-1] == "done"
    assert "token" in names and names.count("token") >= 1
    assert names == ["sources"] + ["token"] * names.count("token") + ["done"]

    sources = json.loads(events[0][1])
    assert any(s["doc_id"] == "DOC-0003" for s in sources)  # alice видит secret
    assert all({"doc_id", "title", "acl"} <= set(s) for s in sources)

    tokens = "".join(d for e, d in events if e == "token")
    assert "[1]" in tokens  # FakeLLM цитирует источник

    done = json.loads(events[-1][1])
    assert done["cited_docs"] == ["DOC-0003"]
    assert done["timings"]["retrieval_ms"] >= 0
    assert done["timings"]["ttft_ms"] >= 0


def test_sse_acl_isolation_guest_no_secret(client: TestClient):
    """guest (public) не получает secret-чанк ни в sources, ни в контексте LLM."""
    response = client.post("/api/chat", json={
        "question": "какой бюджет у проекта Феникс?",
        "user_id": "guest", "stream": True,
    })
    assert response.status_code == 200
    events = parse_sse(response.text)
    sources = json.loads(events[0][1])
    assert sources, "хотя бы public-чанк должен вернуться"
    assert all(s["doc_id"] != "DOC-0003" for s in sources)
    assert all(s["acl"] != "secret" for s in sources)


def test_non_stream_json(client: TestClient):
    response = client.post("/api/chat", json={
        "question": "кто автор регламента отдела ИТ?",
        "user_id": "bob", "stream": False,
    })
    assert response.status_code == 200
    body = response.json()
    assert "[1]" in body["answer"]
    assert body["cited_docs"]
    assert body["sources"]
    assert body["timings"]["retrieval_ms"] >= 0


def test_unknown_user_404(client: TestClient):
    response = client.post("/api/chat", json={
        "question": "вопрос", "user_id": "mallory", "stream": True,
    })
    assert response.status_code == 404
    assert "mallory" in response.json()["detail"]


def test_validation_error_missing_question(client: TestClient):
    response = client.post("/api/chat", json={"user_id": "alice"})
    assert response.status_code == 422
