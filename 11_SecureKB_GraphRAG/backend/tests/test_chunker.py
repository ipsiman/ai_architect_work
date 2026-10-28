"""Тесты чанкера: размеры, перекрытие, детерминированность, пустой текст."""

from backend.app.ingestion.chunker import chunk_text, make_chunks
from backend.app.ingestion.parser import Document


def test_short_text_single_chunk() -> None:
    text = " ".join(f"слово{i}" for i in range(50))
    chunks = chunk_text(text, size=500, overlap=50)
    assert len(chunks) == 1
    assert chunks[0] == text


def test_long_text_respects_size_and_overlap() -> None:
    words = [f"w{i}" for i in range(600)]
    text = " ".join(words)
    chunks = chunk_text(text, size=100, overlap=20)
    assert len(chunks) >= 2
    first = chunks[0].split()
    assert len(first) == 100
    # перекрытие: первые 20 слов второго чанка == последние 20 первого
    second = chunks[1].split()
    assert second[:20] == first[80:100]
    # покрытие: последнее слово корпуса есть в последнем чанке
    assert words[-1] in chunks[-1].split()
    # ни один чанк не длиннее лимита
    assert all(len(c.split()) <= 100 for c in chunks)


def test_deterministic() -> None:
    text = " ".join(f"токен{i}" for i in range(1000))
    assert chunk_text(text, size=120, overlap=30) == chunk_text(text, size=120, overlap=30)


def test_empty_text() -> None:
    assert chunk_text("") == [""]
    assert chunk_text("   \n\t ") == [""]


def test_invalid_params() -> None:
    import pytest

    with pytest.raises(ValueError):
        chunk_text("текст", size=0)
    with pytest.raises(ValueError):
        chunk_text("текст", size=10, overlap=10)


def test_make_chunks_metadata() -> None:
    from datetime import date

    from backend.app.ingestion.acl import ACL

    doc = Document(
        doc_id="DOC-9999", title="Тест", acl=ACL.internal, department="ИТ",
        doc_type="plan", date=date(2026, 1, 1), body=" ".join(f"сл{i}" for i in range(700)),
    )
    chunks = make_chunks(doc, size=300, overlap=50)
    assert len(chunks) >= 2
    assert all(c.doc_id == "DOC-9999" for c in chunks)
    assert all(c.acl == ACL.internal for c in chunks)
    assert chunks[0].chunk_id == "DOC-9999#0"
    assert chunks[1].chunk_id == "DOC-9999#1"
