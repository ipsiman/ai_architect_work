"""Чанкинг текста по словам с перекрытием.

Допущение (RU-эвристика): 1 токен ~= 1 слово. Для русского текста,
токенизированного по пробелам, это даёт консервативную оценку длины
(реальные BPE-токены дробят слова, поэтому слов МЕНЬШЕ или равно токенов —
чанки получаются не длиннее целевого размера). Перекрытие считается
в тех же единицах (словах).
"""

from __future__ import annotations

from dataclasses import dataclass, field

from backend.app.ingestion.acl import ACL
from backend.app.ingestion.parser import Document


@dataclass(slots=True)
class Chunk:
    """Чанк документа, готовый к маскированию, эмбеддингу и индексации."""

    chunk_id: str
    doc_id: str
    title: str
    acl: ACL
    department: str
    text: str
    entities: list[str] = field(default_factory=list)


def chunk_text(text: str, size: int = 500, overlap: int = 50) -> list[str]:
    """Делит текст на чанки по словам: не более `size` слов, перекрытие `overlap` слов.

    Детерминирован: зависит только от текста и параметров. Пустой текст и текст
    короче `size` дают ровно один (возможно, пустой) чанк.
    """
    if size <= 0:
        raise ValueError("size must be positive")
    if overlap < 0 or overlap >= size:
        raise ValueError("overlap must satisfy 0 <= overlap < size")

    words = text.split()
    if not words:
        return [text.strip()]

    chunks: list[str] = []
    step = size - overlap
    start = 0
    while True:
        window = words[start : start + size]
        if not window:
            break
        chunks.append(" ".join(window))
        if start + size >= len(words):
            break
        start += step
    # вырожденный случай: последний полный чанк уже покрыт хвостом
    return chunks


def make_chunks(
    doc: Document,
    size: int = 500,
    overlap: int = 50,
) -> list[Chunk]:
    """Разбивает Document на чанки с метаданными документа."""
    texts = chunk_text(doc.body, size=size, overlap=overlap)
    return [
        Chunk(
            chunk_id=f"{doc.doc_id}#{i}",
            doc_id=doc.doc_id,
            title=doc.title,
            acl=doc.acl,
            department=doc.department,
            text=t,
        )
        for i, t in enumerate(texts)
    ]
