"""Парсер документов корпуса: Markdown + YAML frontmatter → Document."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from pathlib import Path

import yaml

from backend.app.ingestion.acl import ACL

REQUIRED_FIELDS = ("doc_id", "title", "acl", "department", "doc_type", "date")


@dataclass(slots=True)
class Document:
    """Документ корпуса после парсинга frontmatter и тела."""

    doc_id: str
    title: str
    acl: ACL
    department: str
    doc_type: str
    date: date
    body: str
    source_path: str = ""


def parse_frontmatter(text: str) -> tuple[dict, str]:
    """Разделяет файл на (frontmatter-словарь, тело).

    Ожидается формат:
        ---
        key: value
        ---
        тело
    """
    stripped = text.lstrip("\ufeff")
    if not stripped.startswith("---"):
        raise ValueError("frontmatter not found: file must start with '---'")
    lines = stripped.splitlines()
    # закрывающий разделитель — первая строка '---' или '...' после открывающей
    end_idx = None
    for i in range(1, len(lines)):
        if lines[i].strip() in ("---", "..."):
            end_idx = i
            break
    if end_idx is None:
        raise ValueError("frontmatter not closed: missing closing '---'")
    meta = yaml.safe_load("\n".join(lines[1:end_idx])) or {}
    body = "\n".join(lines[end_idx + 1:]).strip()
    return meta, body


def read_document(path: str | Path) -> Document:
    """Читает один .md-файл корпуса и возвращает Document."""
    p = Path(path)
    meta, body = parse_frontmatter(p.read_text(encoding="utf-8"))
    missing = [f for f in REQUIRED_FIELDS if f not in meta]
    if missing:
        raise ValueError(f"{p.name}: missing frontmatter fields: {missing}")
    raw_date = meta["date"]
    doc_date = raw_date if isinstance(raw_date, date) else date.fromisoformat(str(raw_date))
    return Document(
        doc_id=str(meta["doc_id"]),
        title=str(meta["title"]),
        acl=ACL.parse(meta["acl"]),
        department=str(meta["department"]),
        doc_type=str(meta["doc_type"]),
        date=doc_date,
        body=body,
        source_path=str(p),
    )


def read_corpus(corpus_dir: str | Path) -> list[Document]:
    """Читает все .md-документы каталога (отсортированные по имени файла)."""
    root = Path(corpus_dir)
    return [read_document(p) for p in sorted(root.glob("*.md"))]
