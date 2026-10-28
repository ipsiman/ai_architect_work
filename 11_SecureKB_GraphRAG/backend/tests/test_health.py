"""Тесты health/metrics (этап A)."""

import pytest
from fastapi.testclient import TestClient

from backend.app.api.health import get_app_version
from backend.app.core.config import get_settings
from backend.app.main import app


@pytest.fixture()
def client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    """TestClient с отключённым OTEL (чтобы не ходить в Jaeger из тестов)."""
    monkeypatch.setenv("SECUREKB_OTEL_ENABLED", "false")
    get_settings.cache_clear()
    with TestClient(app) as c:
        yield c
    get_settings.cache_clear()


def test_health_ok(client: TestClient) -> None:
    response = client.get("/health")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["version"] == get_app_version()


def test_metrics_exposed(client: TestClient) -> None:
    client.get("/health")  # сгенерировать данные метрик
    response = client.get("/metrics")
    assert response.status_code == 200
    assert "securekb_requests_total" in response.text


def test_chat_requires_question_and_user(client: TestClient) -> None:
    """Этап C: /api/chat реализован; без question/user_id — 422 (валидация)."""
    response = client.post("/api/chat", json={})
    assert response.status_code == 422


def test_admin_users_real_endpoint(client: TestClient) -> None:
    """Этап D: /admin/users реализован — список пользователей с ACL."""
    response = client.get("/admin/users")
    assert response.status_code == 200
    body = response.json()
    assert body["count"] == 3
    assert {u["user_id"] for u in body["users"]} == {"alice", "bob", "guest"}
