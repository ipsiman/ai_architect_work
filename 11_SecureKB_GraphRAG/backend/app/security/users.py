"""Пользователи SecureKB (упрощённо для демо; аутентификация — этап D).

Хранение — таблица users в SQLite (data/graph.db), создаётся при старте,
дефолтные пользователи засеиваются INSERT OR IGNORE:
    alice / alice@corp.ru → secret
    bob   / bob@corp.ru   → internal
    guest / guest@corp.ru → public
"""

from __future__ import annotations

import sqlite3
import threading
from dataclasses import dataclass
from pathlib import Path

from backend.app.ingestion.acl import ACL

_SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    user_id TEXT PRIMARY KEY,
    name    TEXT NOT NULL,
    acl     TEXT NOT NULL
);
"""

# (user_id, name, acl) — засеиваются при первом обращении к БД
DEFAULT_USERS: tuple[tuple[str, str, str], ...] = (
    ("alice", "alice@corp.ru", ACL.secret.name),
    ("bob", "bob@corp.ru", ACL.internal.name),
    ("guest", "guest@corp.ru", ACL.public.name),
)


@dataclass(slots=True)
class User:
    """Пользователь с уровнем доступа."""

    user_id: str
    name: str
    acl: ACL


class UserStore:
    """SQLite-хранилище пользователей (таблица users в data/graph.db)."""

    def __init__(self, db_path: str | Path) -> None:
        self.path = Path(db_path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(str(self.path), check_same_thread=False)
        self._conn.executescript(_SCHEMA)
        self._conn.executemany(
            "INSERT OR IGNORE INTO users (user_id, name, acl) VALUES (?, ?, ?)",
            DEFAULT_USERS,
        )
        self._conn.commit()

    def get_user(self, user_id: str) -> User | None:
        """Пользователь по user_id или None, если не существует."""
        row = self._conn.execute(
            "SELECT user_id, name, acl FROM users WHERE user_id = ?", (user_id,)
        ).fetchone()
        if row is None:
            return None
        return User(user_id=row[0], name=row[1], acl=ACL.parse(row[2]))

    def list_users(self) -> list[User]:
        rows = self._conn.execute(
            "SELECT user_id, name, acl FROM users ORDER BY user_id"
        ).fetchall()
        return [User(user_id=r[0], name=r[1], acl=ACL.parse(r[2])) for r in rows]

    def close(self) -> None:
        self._conn.close()


_instance: UserStore | None = None
_instance_lock = threading.Lock()


def get_user_store() -> UserStore:
    """Синглтон на процесс: data/graph.db из Settings."""
    global _instance
    with _instance_lock:
        if _instance is None:
            from backend.app.core.config import get_settings

            _instance = UserStore(get_settings().graph_db_path)
        return _instance
