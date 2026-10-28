"""Тесты Control Plane (этап D): /admin/audit после запросов + /admin/users.

Движок подменён стабом (Qdrant не нужен): проверяется связка
«запрос → аудит-событие answer → GET /admin/audit» и список пользователей.
/admin/stats проверяется отдельно при живом Qdrant (skip иначе).
"""

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from backend.app.api import admin as admin_api
from backend.app.api import chat as chat_api
from backend.app.core.config import get_settings
from backend.app.graphs.query_graph import QueryResult
from backend.app.ingestion.acl import ACL
from backend.app.ingestion.qdrant_store import make_client
from backend.app.main import app
from backend.app.retrieval.fuse import RetrievedChunk
from backend.app.security.audit import ANSWER, AuditLogger
from backend.app.security.users import UserStore, get_user_store


class StubEngine:
    """Движок без retrieval: один public-чанк + детерминированный ответ."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def run(self, question: str, user, top_k: int = 5) -> QueryResult:
        self.calls.append((question, user.user_id))
        chunk = RetrievedChunk(
            chunk_id="DOC-0001#c0", doc_id="DOC-0001", title="Регламент",
            acl=ACL.public, text="Командировка оформляется за пять рабочих дней.",
        )
        return QueryResult(
            answer_stream=iter(["Командировка оформляется за пять рабочих дней [1]."]),
            chunks=[chunk],
        )


@pytest.fixture()
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setenv("SECUREKB_OTEL_ENABLED", "false")
    get_settings.cache_clear()

    audit = AuditLogger(tmp_path / "audit.db")
    users = UserStore(tmp_path / "users.db")
    stub = StubEngine()

    app.dependency_overrides[chat_api.get_engine] = lambda: stub
    app.dependency_overrides[get_user_store] = lambda: users
    app.dependency_overrides[chat_api.get_audit] = lambda: audit
    app.dependency_overrides[admin_api.get_audit] = lambda: audit
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()
    audit.close()
    users.close()
    get_settings.cache_clear()


def test_admin_audit_shows_events_after_chat(client: TestClient):
    """/api/chat → событие answer в аудите → /admin/audit его отдаёт."""
    r = client.post("/api/chat", json={
        "question": "За сколько дней оформляется командировка?",
        "user_id": "alice", "stream": False,
    })
    assert r.status_code == 200
    assert "пять рабочих дней" in r.json()["answer"]

    r2 = client.post("/api/chat", json={
        "question": "Ещё один вопрос от боба",
        "user_id": "bob", "stream": False,
    })
    assert r2.status_code == 200

    resp = client.get("/admin/audit", params={"limit": 50})
    assert resp.status_code == 200
    body = resp.json()
    assert body["count"] >= 2

    events = body["events"]
    answers = [e for e in events if e["event"] == ANSWER]
    assert {e["user_id"] for e in answers} >= {"alice", "bob"}
    # новые события первыми; details распарсен из JSON
    assert answers[0]["details"]["cited_docs"] == ["DOC-0001"]
    assert all("ts" in e and "user_id" in e and "event" in e for e in events)


def test_admin_audit_limit(client: TestClient):
    for i in range(7):
        client.post("/api/chat", json={
            "question": f"вопрос {i}", "user_id": "guest", "stream": False})
    resp = client.get("/admin/audit", params={"limit": 3})
    assert resp.status_code == 200
    assert resp.json()["count"] == 3


def test_admin_users_three_users(client: TestClient):
    resp = client.get("/admin/users")
    assert resp.status_code == 200
    body = resp.json()
    assert body["count"] == 3
    by_id = {u["user_id"]: u for u in body["users"]}
    assert set(by_id) == {"alice", "bob", "guest"}
    assert by_id["alice"]["acl"] == "secret"
    assert by_id["bob"]["acl"] == "internal"
    assert by_id["guest"]["acl"] == "public"


def test_admin_stats_counts(client: TestClient):
    """/admin/stats: docs/chunks из Qdrant + nodes/edges из sqlite (skip без Qdrant)."""
    settings = get_settings()
    try:
        qc = make_client(settings.qdrant_url, timeout=2)
        qc.get_collections()
    except Exception:
        pytest.skip("qdrant not running")

    resp = client.get("/admin/stats")
    assert resp.status_code == 200
    stats = resp.json()
    assert set(stats) >= {"docs", "chunks", "nodes", "edges", "users"}
    assert stats["docs"] > 0 and stats["chunks"] > 0
    assert stats["nodes"] > 0 and stats["edges"] > 0
    assert stats["users"] == 3
