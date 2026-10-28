"""Графовое хранилище сущностей на SQLite (data/graph.db).

Узлы — сущности (person:ivanov_ap, project:phoenix, department:it),
рёбра — со-встречаемость сущностей в одном чанке (relation=co_occurs).
Список doc_ids узла хранится в JSON и служит для graph-expand: по сущностям
вопроса находим смежные документы.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

_SCHEMA = """
CREATE TABLE IF NOT EXISTS nodes (
    id      TEXT PRIMARY KEY,
    type    TEXT NOT NULL,
    name    TEXT NOT NULL,
    doc_ids TEXT NOT NULL DEFAULT '[]'
);
CREATE TABLE IF NOT EXISTS edges (
    src      TEXT NOT NULL,
    dst      TEXT NOT NULL,
    relation TEXT NOT NULL,
    PRIMARY KEY (src, dst, relation)
);
CREATE INDEX IF NOT EXISTS idx_edges_src ON edges (src);
CREATE INDEX IF NOT EXISTS idx_edges_dst ON edges (dst);
"""


class GraphStore:
    """SQLite-хранилище графа сущности ↔ документы."""

    def __init__(self, db_path: str | Path) -> None:
        self.path = Path(db_path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self.path))
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    # -- запись ---------------------------------------------------------------

    def add_entity_in_doc(self, node_id: str, node_type: str, name: str,
                          doc_id: str) -> None:
        """Создаёт/дополняет узел сущности, добавляя doc_id в список упоминаний."""
        cur = self._conn.execute("SELECT doc_ids FROM nodes WHERE id = ?", (node_id,))
        row = cur.fetchone()
        if row is None:
            doc_ids = [doc_id]
            self._conn.execute(
                "INSERT INTO nodes (id, type, name, doc_ids) VALUES (?, ?, ?, ?)",
                (node_id, node_type, name, json.dumps(doc_ids)),
            )
        else:
            doc_ids = sorted(set(json.loads(row[0])) | {doc_id})
            self._conn.execute(
                "UPDATE nodes SET doc_ids = ? WHERE id = ?",
                (json.dumps(doc_ids), node_id),
            )
        self._conn.commit()

    def add_edge(self, src: str, dst: str, relation: str = "co_occurs") -> None:
        """Добавляет направленное ребро (дубликаты игнорируются)."""
        self._conn.execute(
            "INSERT OR IGNORE INTO edges (src, dst, relation) VALUES (?, ?, ?)",
            (src, dst, relation),
        )
        self._conn.commit()

    def add_chunk_entities(self, node_ids: list[tuple[str, str, str]],
                           doc_id: str) -> None:
        """Пишет узлы чанка и полные попарные рёбра со-встречаемости.

        node_ids — кортежи (node_id, type, name); doc_id — документ чанка.
        """
        for node_id, ntype, name in node_ids:
            self.add_entity_in_doc(node_id, ntype, name, doc_id)
        ids = sorted(n[0] for n in node_ids)
        for i, src in enumerate(ids):
            for dst in ids[i + 1:]:
                self.add_edge(src, dst)
                self.add_edge(dst, src)

    # -- чтение ---------------------------------------------------------------

    def _doc_ids_of_nodes(self, node_ids: list[str]) -> list[str]:
        if not node_ids:
            return []
        placeholders = ",".join("?" * len(node_ids))
        rows = self._conn.execute(
            f"SELECT doc_ids FROM nodes WHERE id IN ({placeholders})", node_ids
        ).fetchall()
        docs: set[str] = set()
        for (raw,) in rows:
            docs.update(json.loads(raw))
        return sorted(docs)

    def neighbors(
        self,
        entity_ids: list[str],
        exclude_doc_id: str | None = None,
        allowed_doc_ids: set[str] | None = None,
        limit: int = 50,
    ) -> list[str]:
        """Документы, связанные с сущностями: их собственные документы плюс
        документы сущностей, со-встречающихся с ними в одном чанке (1 hop).

        Фильтры:
        - exclude_doc_id — исключить документ-источник (самоссылки);
        - allowed_doc_ids — если задано, оставить только документы, доступные
          пользователю по ACL (ключевой фильтр безопасности для graph-expand);
        - limit — ограничить число возвращаемых doc_id.
        """
        if not entity_ids:
            return []
        placeholders = ",".join("?" * len(entity_ids))
        edge_rows = self._conn.execute(
            f"SELECT src, dst FROM edges WHERE src IN ({placeholders}) "
            f"OR dst IN ({placeholders})",
            [*entity_ids, *entity_ids],
        ).fetchall()
        adjacent = {src for src, _ in edge_rows} | {dst for _, dst in edge_rows}
        related_nodes = sorted(set(entity_ids) | adjacent)
        docs = self._doc_ids_of_nodes(related_nodes)

        # Релевантность: документы, СОДЕРЖАЩИЕ сущности вопроса (0-hop),
        # первыми — по числу совпавших сущностей; затем документы смежных
        # сущностей (1-hop); внутри группы — алфавит (детерминизм).
        direct_count: dict[str, int] = {}
        for node_id in entity_ids:
            for doc in json.loads(self._conn.execute(
                    "SELECT doc_ids FROM nodes WHERE id = ?", (node_id,)
            ).fetchone()[0]):
                direct_count[doc] = direct_count.get(doc, 0) + 1

        result = []
        for doc in sorted(docs, key=lambda d: (-direct_count.get(d, 0), d)):
            if exclude_doc_id is not None and doc == exclude_doc_id:
                continue
            if allowed_doc_ids is not None and doc not in allowed_doc_ids:
                continue
            result.append(doc)
            if len(result) >= limit:
                break
        return result

    def node_count(self) -> int:
        return self._conn.execute("SELECT COUNT(*) FROM nodes").fetchone()[0]

    def edge_count(self) -> int:
        return self._conn.execute("SELECT COUNT(*) FROM edges").fetchone()[0]

    def close(self) -> None:
        self._conn.close()
