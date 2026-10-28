"""Потоковая генерация RAG-ответа + извлечение процитированных документов."""

from __future__ import annotations

import re
from collections.abc import Iterator, Sequence

from backend.app.generation.llm import LLM
from backend.app.generation.prompts import build_context, build_messages
from backend.app.retrieval.fuse import RetrievedChunk

_CITE_RE = re.compile(r"\[(\d+)\]")


def stream_answer(
    question: str,
    chunks: Sequence[RetrievedChunk],
    llm: LLM,
    max_tokens: int = 512,
    temperature: float = 0.2,
) -> Iterator[str]:
    """Собирает промпт из чанков и стримит ответ LLM по фрагментам."""
    context = build_context(chunks)
    messages = build_messages(question, context)
    yield from llm.generate_stream(messages, max_tokens=max_tokens,
                                   temperature=temperature)


def extract_citations(answer: str) -> list[int]:
    """Номера [N], упомянутые в ответе (отсортированы, без дубликатов)."""
    return sorted({int(n) for n in _CITE_RE.findall(answer)})


def extract_cited_docs(answer: str, chunks: Sequence[RetrievedChunk]) -> list[str]:
    """doc_id документов, процитированных в ответе как [N] (без дубликатов)."""
    doc_ids: list[str] = []
    for n in extract_citations(answer):
        if 1 <= n <= len(chunks):
            doc_id = chunks[n - 1].doc_id
            if doc_id not in doc_ids:
                doc_ids.append(doc_id)
    return doc_ids
