"""Тесты промптов и извлечения цитат [N]."""

from backend.app.generation.answer_service import (
    extract_citations,
    extract_cited_docs,
    stream_answer,
)
from backend.app.generation.llm import FakeLLM
from backend.app.generation.prompts import (
    SYSTEM_PROMPT_RU,
    build_context,
    build_messages,
)
from backend.app.ingestion.acl import ACL
from backend.app.retrieval.fuse import RetrievedChunk


def chunk(n: int, doc_id: str = "DOC-0001") -> RetrievedChunk:
    return RetrievedChunk(
        chunk_id=f"{doc_id}#{n}", doc_id=doc_id, title=f"Документ {doc_id}",
        acl=ACL.public, text=f"Текст чанка {n}.",
    )


def test_system_prompt_key_requirements():
    assert "SecureKB" in SYSTEM_PROMPT_RU
    assert "ТОЛЬКО на основе" in SYSTEM_PROMPT_RU
    assert "откажись" in SYSTEM_PROMPT_RU  # честный отказ при отсутствии факта
    assert "[N]" in SYSTEM_PROMPT_RU  # формат цитирования источников


def test_build_context_numbered_format():
    context = build_context([chunk(0, "DOC-0001"), chunk(1, "DOC-0002")])
    assert context.startswith("[1] (Документ: Документ DOC-0001, DOC-0001)\nТекст чанка 0.\n\n")
    assert "[2] (Документ: Документ DOC-0002, DOC-0002)\nТекст чанка 1.\n\n" in context
    assert context.endswith("\n\n")


def test_build_context_empty():
    assert build_context([]) == ""


def test_build_messages_roles_and_question():
    chunks = [chunk(0), chunk(1)]
    messages = build_messages("Какой бюджет?", build_context(chunks))
    assert [m["role"] for m in messages] == ["system", "user"]
    assert messages[0]["content"] == SYSTEM_PROMPT_RU
    assert "Какой бюджет?" in messages[1]["content"]
    assert "[1]" in messages[1]["content"] and "[2]" in messages[1]["content"]


def test_build_messages_no_context_honest_refusal():
    messages = build_messages("Вопрос", "")
    assert "не найдено" in messages[1]["content"]


def test_extract_citations():
    assert extract_citations("ответ [1] и [3], ещё раз [1]") == [1, 3]
    assert extract_citations("без цитат") == []
    assert extract_citations("версия 3.5[?]") == []


def test_extract_cited_docs_maps_numbers_to_docs():
    chunks = [chunk(0, "DOC-A"), chunk(1, "DOC-B"), chunk(2, "DOC-C")]
    assert extract_cited_docs("факт [1], ещё [3]", chunks) == ["DOC-A", "DOC-C"]
    assert extract_cited_docs("[2]", chunks) == ["DOC-B"]
    # дубль одного источника не дублирует doc_id
    assert extract_cited_docs("[1] [1]", chunks) == ["DOC-A"]
    # выходящий за границы номер игнорируется
    assert extract_cited_docs("[99]", chunks) == []


def test_stream_answer_uses_llm_and_context():
    llm = FakeLLM()
    chunks = [chunk(0, "DOC-A")]
    stream = stream_answer("Какой бюджет?", chunks, llm, max_tokens=32, temperature=0.0)
    parts = list(stream)
    answer = "".join(parts)
    assert "[1]" in answer
    # LLM получила messages с контекстом
    assert len(llm.calls) == 1
    user_msg = llm.calls[0][1]["content"]
    assert "DOC-A" in user_msg and "Какой бюджет?" in user_msg
