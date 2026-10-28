"""Тесты парсера frontmatter."""

from datetime import date

import pytest

from backend.app.ingestion.acl import ACL
from backend.app.ingestion.parser import read_document, read_corpus

DOC_MD = """---
doc_id: DOC-0042
title: "Регламент работы со служебной информацией отдела ИТ"
acl: internal
department: "ИТ"
doc_type: regulation
date: 2026-03-15
---

# Регламент работы со служебной информацией отдела ИТ

Первый абзац тела документа.

Второй абзац тела документа.
"""


def test_read_document(tmp_path):
    p = tmp_path / "DOC-0042.md"
    p.write_text(DOC_MD, encoding="utf-8")
    doc = read_document(p)
    assert doc.doc_id == "DOC-0042"
    assert doc.title == "Регламент работы со служебной информацией отдела ИТ"
    assert doc.acl is ACL.internal
    assert doc.department == "ИТ"
    assert doc.doc_type == "regulation"
    assert doc.date == date(2026, 3, 15)
    # тело: без заголовка frontmatter, но с содержимым
    assert doc.body.startswith("# Регламент")
    assert "Второй абзац" in doc.body
    assert "---" not in doc.body


def test_read_corpus_sorted(tmp_path):
    for i in (3, 1, 2):
        body = DOC_MD.replace("DOC-0042", f"DOC-000{i}")
        (tmp_path / f"DOC-000{i}.md").write_text(body, encoding="utf-8")
    docs = read_corpus(tmp_path)
    assert [d.doc_id for d in docs] == ["DOC-0001", "DOC-0002", "DOC-0003"]


def test_missing_frontmatter_raises(tmp_path):
    p = tmp_path / "bad.md"
    p.write_text("Просто текст без frontmatter.", encoding="utf-8")
    with pytest.raises(ValueError, match="frontmatter not found"):
        read_document(p)


def test_missing_fields_raises(tmp_path):
    p = tmp_path / "incomplete.md"
    p.write_text("---\ndoc_id: DOC-0001\n---\n\nТекст", encoding="utf-8")
    with pytest.raises(ValueError, match="missing frontmatter fields"):
        read_document(p)


def test_unknown_acl_raises(tmp_path):
    p = tmp_path / "badacl.md"
    p.write_text(
        "---\ndoc_id: DOC-0001\ntitle: T\nacl: topsecret\ndepartment: ИТ\n"
        "doc_type: policy\ndate: 2026-01-01\n---\n\nТекст",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="unknown acl"):
        read_document(p)
