"""Журнал аудита безопасности SecureKB (этап D).

Таблица audit_log в data/graph.db (рядом с users/nodes/edges):
    ts      — ISO-8601 UTC (sqlite strftime)
    user_id — кто совершил действие
    event   — тип события (см. EVENTS)
    details — JSON-детали (вопрос, причины блокировки, doc_id ...)

События:
    query             — принят запрос (PII-маскированный вопрос);
    injection_blocked — запрос отклонён InjectionGuard (причины в details);
    pii_masked        — во входном вопросе замаскировано PII (типы в details);
    acl_drop          — rrf_fuse выбросил чанк выше уровня пользователя
                        (defense-in-depth, ветка/чанк/acl в details);
    output_sanitized  — OutputGuard вырезал утечку из ответа (violations);
    answer            — ответ отдан пользователю (cited_docs в details).
"""

from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path

QUERY = "query"
INJECTION_BLOCKED = "injection_blocked"
PII_MASKED = "pii_masked"
ACL_DROP = "acl_drop"
OUTPUT_SANITIZED = "output_sanitized"
ANSWER = "answer"

EVENTS = (QUERY, INJECTION_BLOCKED, PII_MASKED, ACL_DROP, OUTPUT_SANITIZED,
          ANSWER)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS audit_log (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    ts      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    user_id TEXT NOT NULL,
    event   TEXT NOT NULL,
    details TEXT
);
CREATE INDEX IF NOT EXISTS idx_audit_event ON audit_log (event);
"""


class AuditLogger:
    """Потокобезопасный писатель/читатель audit_log (SQLite data/graph.db)."""

    def __init__(self, db_path: str | Path) -> None:
        self.path = Path(db_path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(str(self.path), check_same_thread=False)
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    def log(self, user_id: str, event: str, details: dict | None = None) -> None:
        """Пишет событие аудита (details сериализуется в JSON)."""
        payload = json.dumps(details, ensure_ascii=False) if details else None
        with self._lock:
            self._conn.execute(
                "INSERT INTO audit_log (user_id, event, details) VALUES (?, ?, ?)",
                (user_id, event, payload),
            )
            self._conn.commit()

    def recent(self, limit: int = 50) -> list[dict]:
        """Последние события (новые первыми), details — уже распарсен."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT ts, user_id, event, details FROM audit_log "
                "ORDER BY id DESC LIMIT ?",
                (limit,),
            ).fetchall()
        events = []
        for ts, user_id, event, details in rows:
            events.append({
                "ts": ts,
                "user_id": user_id,
                "event": event,
                "details": json.loads(details) if details else None,
            })
        return events

    def count(self, event: str | None = None) -> int:
        """Число событий (всего или конкретного типа)."""
        with self._lock:
            if event is None:
                return self._conn.execute(
                    "SELECT COUNT(*) FROM audit_log").fetchone()[0]
            return self._conn.execute(
                "SELECT COUNT(*) FROM audit_log WHERE event = ?",
                (event,)).fetchone()[0]

    def close(self) -> None:
        self._conn.close()


class NullAuditLogger(AuditLogger):
    """No-op-заглушка (audit выключен настройкой SECUREKB_AUDIT_ENABLED=false)."""

    def __init__(self) -> None:  # noqa: super().__init__ не вызываем — БД не нужна
        self._lock = threading.Lock()

    def log(self, user_id: str, event: str, details: dict | None = None) -> None:
        return

    def recent(self, limit: int = 50) -> list[dict]:
        return []

    def count(self, event: str | None = None) -> int:
        return 0

    def close(self) -> None:
        return


_instance: AuditLogger | None = None
_instance_lock = threading.Lock()


def get_audit_logger() -> AuditLogger:
    """Синглтон на процесс: data/graph.db из Settings (или Null при отключении)."""
    global _instance
    with _instance_lock:
        if _instance is None:
            from backend.app.core.config import get_settings

            settings = get_settings()
            if settings.audit_enabled:
                _instance = AuditLogger(settings.graph_db_path)
            else:
                _instance = NullAuditLogger()
        return _instance
