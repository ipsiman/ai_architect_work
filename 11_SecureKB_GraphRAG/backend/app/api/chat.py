"""Chat API: RAG-запрос → SSE-стрим ответа (этап C).

POST /api/chat body: {question, user_id, stream=true, top_k=5}
SSE-события:
    event: sources  data: [{doc_id, title, acl}, ...]
    event: token    data: <фрагмент ответа>   (много раз)
    event: done     data: {cited_docs, timings: {retrieval_ms, ttft_ms}}
При stream=false — обычный JSON с теми же полями.
"""

from __future__ import annotations

import json
import time
from collections.abc import Iterator

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field

from backend.app.generation.answer_service import extract_cited_docs
from backend.app.generation.llm import LLM, QwenLLM
from backend.app.graphs.query_graph import QueryEngine, QueryResult, build_engine, consume_answer
from backend.app.obs import metrics
from backend.app.obs.tracing import get_tracer
from backend.app.security.audit import ANSWER, AuditLogger, get_audit_logger
from backend.app.security.users import User, UserStore, get_user_store

router = APIRouter(prefix="/api", tags=["chat"])


class ChatRequest(BaseModel):
    """Запрос к RAG-чату."""

    question: str = Field(min_length=1)
    user_id: str = Field(min_length=1)
    stream: bool = True
    top_k: int = Field(default=5, ge=1, le=20)


class SourceOut(BaseModel):
    doc_id: str
    title: str
    acl: str


# -- FastAPI-зависимости (переопределяются в тестах через dependency_overrides) --

def get_llm() -> LLM:
    """LLM по умолчанию: Qwen2.5-1.5B (ленивый синглтон llama.cpp)."""
    return QwenLLM.get()


def get_engine(llm: LLM = Depends(get_llm)) -> QueryEngine:
    """Query graph с реальным retrieval-стеком и выбранной LLM."""
    return build_engine(llm)


def get_audit() -> AuditLogger:
    """Журнал аудита (переопределяется в тестах)."""
    return get_audit_logger()


# -- SSE ---------------------------------------------------------------------------


def _sse(event: str, data: str) -> str:
    """SSE-кадр; данные с переводами строк кодируются несколькими data:-строками
    (спецификация SSE: клиент склеивает их через \\n)."""
    lines = "".join(f"data: {line}\n" for line in data.split("\n"))
    return f"event: {event}\n{lines}\n"


def sources_payload(result: QueryResult) -> str:
    sources = [
        {"doc_id": c.doc_id, "title": c.title, "acl": c.acl.name}
        for c in result.chunks
    ]
    return json.dumps(sources, ensure_ascii=False)


def sse_events(result: QueryResult, retrieval_ms: float,
               user_id: str | None = None,
               audit: AuditLogger | None = None) -> Iterator[str]:
    """Генератор SSE-событий: sources → token* → done (+audit answer)."""
    yield _sse("sources", sources_payload(result))

    parts: list[str] = []
    ttft_ms: float | None = None
    start = time.perf_counter()
    assert result.answer_stream is not None
    for fragment in result.answer_stream:
        metrics.track_tokens()  # счётчик фрагментов (этап E1)
        if ttft_ms is None:  # time-to-first-token
            ttft = time.perf_counter() - start
            ttft_ms = ttft * 1000
            metrics.observe_ttft(ttft)
        parts.append(fragment)
        yield _sse("token", fragment)

    answer = "".join(parts)
    cited = extract_cited_docs(answer, result.chunks)
    if audit is not None and user_id is not None:
        audit.log(user_id, ANSWER,
                  {"cited_docs": cited, "chunks": len(result.chunks),
                   "blocked": result.blocked})
    done = {
        "cited_docs": cited,
        "timings": {
            "retrieval_ms": round(retrieval_ms, 1),
            "ttft_ms": round(ttft_ms or 0.0, 1),
        },
    }
    yield _sse("done", json.dumps(done, ensure_ascii=False))


# -- эндпоинт ------------------------------------------------------------------------


@router.post("/chat", response_model=None)
async def chat(
    request: ChatRequest,
    user_store: UserStore = Depends(get_user_store),
    engine: QueryEngine = Depends(get_engine),
    audit: AuditLogger = Depends(get_audit),
) -> StreamingResponse | JSONResponse:
    """RAG-чат: retrieval (ACL-aware) + потоковая генерация LLM."""
    user: User | None = user_store.get_user(request.user_id)
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"unknown user: {request.user_id}",
        )

    with get_tracer().start_as_current_span("chat") as span:
        span.set_attribute("chat.user_id", user.user_id)
        span.set_attribute("chat.user_acl", user.acl.name)
        span.set_attribute("chat.stream", request.stream)

        start = time.perf_counter()
        result = engine.run(request.question, user, top_k=request.top_k)
        retrieval_ms = (time.perf_counter() - start) * 1000
        span.set_attribute("chat.chunks", len(result.chunks))

        if request.stream:
            return StreamingResponse(
                sse_events(result, retrieval_ms, user_id=user.user_id,
                           audit=audit),
                media_type="text/event-stream",
                headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
            )

    answer = consume_answer(result)
    cited_docs = extract_cited_docs(answer, result.chunks)
    audit.log(user.user_id, ANSWER,
              {"cited_docs": cited_docs, "chunks": len(result.chunks),
               "blocked": result.blocked})
    return JSONResponse(
        {
            "answer": answer,
            "sources": [
                {"doc_id": c.doc_id, "title": c.title, "acl": c.acl.name}
                for c in result.chunks
            ],
            "cited_docs": cited_docs,
            "timings": {
                "retrieval_ms": round(retrieval_ms, 1),
                "ttft_ms": None,
            },
        }
    )
