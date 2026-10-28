"""Admin Control Plane API (этап D): пользователи, аудит, статистика.

Эндпоинты:
    GET /admin/users         — список пользователей и их ACL-уровни;
    GET /admin/audit?limit=50 — последние события аудита (новые первыми);
    GET /admin/stats         — счётчики: документы/чанки (Qdrant),
                               узлы/рёбра графа (SQLite data/graph.db).

БЕЗОПАСНОСТЬ (MVP): авторизации НЕТ — роутер открыт для локального демо.
В целевой архитектуре доступ ограничивается на ingress-уровне: mTLS между
сервисами + JWT с ролью admin, проверка в middleware до бизнес-логики.
"""

from __future__ import annotations

import sqlite3

from fastapi import APIRouter, Depends, HTTPException, Query, status

from backend.app.core.config import get_settings
from backend.app.obs.tracing import get_tracer
from backend.app.security.audit import AuditLogger, get_audit_logger
from backend.app.security.users import UserStore, get_user_store

router = APIRouter(prefix="/admin", tags=["admin"])


def get_audit() -> AuditLogger:
    """Зависимость-аудит (переопределяется в тестах через dependency_overrides)."""
    return get_audit_logger()


@router.get("/users")
async def list_users(user_store: UserStore = Depends(get_user_store)) -> dict:
    """Список пользователей с уровнями доступа."""
    with get_tracer().start_as_current_span("admin_users"):
        users = user_store.list_users()
        return {
            "count": len(users),
            "users": [
                {"user_id": u.user_id, "name": u.name, "acl": u.acl.name}
                for u in users
            ],
        }


@router.get("/audit")
async def audit_events(
    limit: int = Query(default=50, ge=1, le=1000),
    audit: AuditLogger = Depends(get_audit),
) -> dict:
    """Последние события аудита (новые первыми)."""
    with get_tracer().start_as_current_span("admin_audit"):
        events = audit.recent(limit=limit)
        return {"count": len(events), "events": events}


@router.get("/stats")
async def stats() -> dict:
    """Счётчики системы: docs/chunks — Qdrant, nodes/edges/users — SQLite."""
    with get_tracer().start_as_current_span("admin_stats"):
        settings = get_settings()

        from backend.app.ingestion.qdrant_store import make_client

        try:
            client = make_client(settings.qdrant_url, timeout=5)
            count_resp = client.count(collection_name=settings.qdrant_collection,
                                      exact=True)
            chunks = count_resp.count
            docs: set[str] = set()
            offset = None
            while True:
                points, offset = client.scroll(
                    collection_name=settings.qdrant_collection,
                    limit=256, offset=offset, with_payload=["doc_id"],
                )
                docs.update(p.payload["doc_id"] for p in points)
                if offset is None:
                    break
        except Exception as exc:  # qdrant недоступен — не отдаём частичные числа
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail=f"qdrant unavailable: {exc}",
            ) from exc

        conn = sqlite3.connect(str(settings.graph_db_path))
        try:
            nodes = conn.execute("SELECT COUNT(*) FROM nodes").fetchone()[0]
            edges = conn.execute("SELECT COUNT(*) FROM edges").fetchone()[0]
        finally:
            conn.close()

        return {
            "docs": len(docs),
            "chunks": chunks,
            "nodes": nodes,
            "edges": edges,
            "users": len(get_user_store().list_users()),
        }
