"""Промпты RAG-ответов SecureKB: системный промпт + сборка контекста с [N]."""

from __future__ import annotations

from collections.abc import Sequence

from backend.app.retrieval.fuse import RetrievedChunk

SYSTEM_PROMPT_RU = (
    "Ты — ассистент корпоративной базы знаний SecureKB. "
    "Отвечай ТОЛЬКО на основе предоставленного контекста и строго на русском языке. "
    "Если ответа в контексте нет — честно откажись: "
    "«В предоставленном контексте нет информации по этому вопросу». "
    "Не используй внешние знания, не выдумывай факты, числа и имена. "
    "Отвечай кратко и по существу. "
    "Всегда указывай источники в формате [N], где N — номер фрагмента контекста, "
    "например: «Бюджет проекта — 240 млн руб. [1]»."
)


def build_context(chunks: Sequence[RetrievedChunk]) -> str:
    """Нумерованный контекст: «[N] (Документ: {title}, {doc_id})\\n{text}\\n\\n»."""
    parts = [
        f"[{i}] (Документ: {c.title}, {c.doc_id})\n{c.text}\n\n"
        for i, c in enumerate(chunks, start=1)
    ]
    return "".join(parts)


def build_messages(question: str, context: str) -> list[dict]:
    """Готовый список messages для chat-completion (system + user)."""
    user_content = (
        f"Контекст из базы знаний:\n\n{context}\n"
        f"Вопрос: {question}\n\n"
        "Ответь на вопрос строго по контексту и укажи источники [N]."
        if context.strip()
        else (
            f"Вопрос: {question}\n\n"
            "Релевантного контекста в базе знаний не найдено — откажись честно."
        )
    )
    return [
        {"role": "system", "content": SYSTEM_PROMPT_RU},
        {"role": "user", "content": user_content},
    ]
