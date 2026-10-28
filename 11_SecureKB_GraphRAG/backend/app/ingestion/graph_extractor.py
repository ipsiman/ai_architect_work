"""Gazetteer-экстрактор сущностей (persons/projects/departments).

Загружает справочник entities.json и ищет сущности по алиасам
(полное ФИО, краткая форма «Иванов А.П.», фамилия, названия проектов
с кавычками и без). Матчинг регистронезависимый, с границами слов:
«Иванов» НЕ совпадает с «Иванова»/«Ивановой» (лишняя буква за границей
шаблона блокирует совпадение).
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable
from pathlib import Path

ENTITY_TYPES = ("person", "project", "department")
PLURAL_TO_TYPE = {"persons": "person", "projects": "project", "departments": "department"}


def _alias_pattern(alias: str) -> re.Pattern[str]:
    """Компилирует алиас в regex с границами слов (юникодными)."""
    escaped = re.escape(alias)
    return re.compile(rf"(?<!\w){escaped}(?!\w)", flags=re.IGNORECASE)


class GraphExtractor:
    """Ищет известные сущности в тексте чанка (gazetteer-подход)."""

    def __init__(self, entities: dict) -> None:
        # alias (в исходном регистре) -> (type, entity_id); длинные алиасы — первыми
        self._by_alias: dict[str, tuple[str, str]] = {}
        self._names: dict[str, dict] = {}  # node_id -> {"type":..., "name":...}
        for plural, etype in PLURAL_TO_TYPE.items():
            for entry in entities.get(plural, []):
                node_id = f"{etype}:{entry['id']}"
                self._names[node_id] = {"type": etype, "name": entry.get("name") or entry.get("full_name", "")}
                for alias in entry.get("aliases", []):
                    if alias and alias not in self._by_alias:
                        self._by_alias[alias] = (etype, entry["id"])

    @classmethod
    def from_file(cls, path: str | Path) -> "GraphExtractor":
        """Загружает справочник из entities.json."""
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        return cls(data)

    @property
    def aliases(self) -> list[str]:
        """Все известные алиасы, отсортированные по убыванию длины."""
        return sorted(self._by_alias, key=len, reverse=True)

    def extract(self, text: str) -> dict[str, list[str]]:
        """Возвращает найденные сущности по типам: {type: [entity_id, ...]}.

        entity_id — «сырой» идентификатор (ivanov_ap); node_id для графа —
        f"{type}:{entity_id}" (см. GraphStore).
        """
        found: dict[str, set[str]] = {t: set() for t in ENTITY_TYPES}
        for alias in self.aliases:
            etype, entity_id = self._by_alias[alias]
            if found[etype] and entity_id in found[etype]:
                continue  # сущность уже найдена через другой алиас
            if _alias_pattern(alias).search(text):
                found[etype].add(entity_id)
        return {t: sorted(ids) for t, ids in found.items()}

    def node_ids(self, text: str) -> list[str]:
        """Список node_id («person:ivanov_ap») всех сущностей в тексте."""
        extracted = self.extract(text)
        result: list[str] = []
        for etype, ids in extracted.items():
            result.extend(f"{etype}:{eid}" for eid in ids)
        return result

    def node_info(self, node_id: str) -> dict | None:
        """Метаданные узла по его id (для записи в граф)."""
        return self._names.get(node_id)


def iter_entity_ids(found: dict[str, Iterable[str]]) -> set[str]:
    """Извлекает полные node_id из результата extract() одного вызова."""
    ids: set[str] = set()
    for etype, entity_ids in found.items():
        if etype in ENTITY_TYPES:
            ids.update(f"{etype}:{eid}" for eid in entity_ids)
    return ids
