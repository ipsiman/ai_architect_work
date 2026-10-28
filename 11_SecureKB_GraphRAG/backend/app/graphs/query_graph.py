"""LangGraph-граф RAG-запроса с guardrails (этап D):

    input_guard ──(blocked)──► END (короткий отказ)
        │ (ok: вопрос PII-маскируется)
        ▼
    retrieve_dense → retrieve_lexical → graph_expand
        → fuse_rerank (RRF + ACL defense-in-depth + audit acl_drop)
        → generate (ответ оборачивается OutputGuard-стримом)

input_guard: InjectionGuard → блок («Запрос отклонён фильтром безопасности»,
audit injection_blocked, conditional edge в END) ИЛИ PII-маскировка вопроса
(audit pii_masked при находках) и вопрос идёт дальше уже замаскированным.

generate НЕ стримит внутри узла: он кладёт в state ленивый iterator,
обёрнутый OutputGuard.wrap_stream (санитизация PII/цитат/SECRET-маркеров
на лету, violations — в state.guard_violations и аудит output_sanitized).

run_query_graph(question, user) — публичная точка входа.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any, TypedDict

from langgraph.graph import END, START, StateGraph

from backend.app.core.logging import get_logger
from backend.app.generation.answer_service import stream_answer
from backend.app.generation.llm import LLM, QwenLLM
from backend.app.obs.metrics import stage_timer
from backend.app.obs.tracing import get_tracer
from backend.app.retrieval.fuse import RetrievedChunk, rrf_fuse
from backend.app.retrieval.retrieval_service import (
    RetrievalService,
    get_retrieval_service,
)
from backend.app.security import audit as audit_mod
from backend.app.security.audit import AuditLogger
from backend.app.security.guardrails import (
    InjectionGuard,
    InputPIIGuard,
    OutputGuard,
)
from backend.app.security.users import User

logger = get_logger(__name__)

BLOCKED_ANSWER = "Запрос отклонён фильтром безопасности."


def _timed_stream(inner: Iterator[str], stage: str) -> Iterator[str]:
    """Обёртка ленивого стрима: время выдачи каждого фрагмента наблюдается
    в securekb_stage_latency_seconds{stage} (для output_guard: включает
    ожидание LLM-фрагмента и санитизацию OutputGuard)."""
    iterator = iter(inner)
    while True:
        with stage_timer(stage):
            try:
                fragment = next(iterator)
            except StopIteration:
                return
        yield fragment


class QueryState(TypedDict, total=False):
    """Состояние графа запроса; узлы дописывают свои ключи."""

    question: str
    user: User
    top_k: int
    blocked: bool  # input_guard отклонил запрос
    guard_reasons: list[str]
    guard_violations: list[str]  # заполняется при потреблении answer_stream
    dense_chunks: list[RetrievedChunk]
    lexical_chunks: list[RetrievedChunk]
    graph_chunks: list[RetrievedChunk]
    chunks: list[RetrievedChunk]  # после RRF-fusion
    debug: dict[str, Any]
    answer_stream: Iterator[str]  # ленивый стрим; потребляет API-слой


@dataclass(slots=True)
class QueryResult:
    """Результат запроса: финальные чанки + ленивый стрим ответа."""

    answer: str = ""
    chunks: list[RetrievedChunk] = field(default_factory=list)
    debug: dict[str, Any] = field(default_factory=dict)
    answer_stream: Iterator[str] | None = None
    blocked: bool = False
    guard_violations: list[str] = field(default_factory=list)


class QueryEngine:
    """Компилированный query graph с подставленными зависимостями."""

    def __init__(self, service: RetrievalService, llm: LLM,
                 max_tokens: int = 512, temperature: float = 0.2, *,
                 audit: AuditLogger | None = None,
                 injection_guard: InjectionGuard | None = None,
                 input_pii_guard: InputPIIGuard | None = None,
                 output_guard: OutputGuard | None = None) -> None:
        self.service = service
        self.llm = llm
        self.max_tokens = max_tokens
        self.temperature = temperature
        self._audit = audit  # None → лениво get_audit_logger() (см. _log)
        self.injection_guard = injection_guard or InjectionGuard()
        self.input_pii_guard = input_pii_guard or InputPIIGuard()
        self.output_guard = output_guard or OutputGuard()
        self._app = self._build().compile()

    # -- аудит -------------------------------------------------------------------

    def _log(self, user_id: str, event: str, details: dict | None = None) -> None:
        sink = self._audit if self._audit is not None else \
            audit_mod.get_audit_logger()
        sink.log(user_id, event, details)

    # -- узлы -------------------------------------------------------------------

    def _build(self) -> StateGraph:
        service, llm = self.service, self.llm
        injection_guard = self.injection_guard
        input_pii_guard = self.input_pii_guard
        output_guard = self.output_guard

        def input_guard(state: QueryState) -> dict:
            """InjectionGuard → блок|END; иначе PII-маскировка вопроса."""
            user: User = state["user"]
            question = state["question"]

            with stage_timer("input_guard"), \
                    get_tracer().start_as_current_span("input_guard") as span:
                span.set_attribute("user_acl", user.acl.name)
                verdict = injection_guard.apply(question)
                span.set_attribute("blocked", bool(verdict.blocked))
                if verdict.blocked:
                    self._log(user.user_id, audit_mod.INJECTION_BLOCKED,
                              {"reasons": verdict.reasons,
                               "question": question[:200]})
                    return {
                        "blocked": True,
                        "guard_reasons": verdict.reasons,
                        "chunks": [],
                        "answer_stream": iter([BLOCKED_ANSWER]),
                        "debug": {"input_guard": "blocked",
                                  "reasons": verdict.reasons},
                    }

                pii = input_pii_guard.apply(question)
                if pii.meta.get("findings"):
                    self._log(user.user_id, audit_mod.PII_MASKED,
                              {"types": [f["type"] for f in pii.meta["findings"]]})
                masked_question = pii.masked_text
                self._log(user.user_id, audit_mod.QUERY,
                          {"question": masked_question[:200]})
                return {
                    "question": masked_question,
                    "blocked": False,
                    "debug": {"input_guard":
                              "pii_masked" if pii.meta.get("findings") else "pass"},
                }

        def route_after_input_guard(state: QueryState) -> str:
            return "block" if state.get("blocked") else "continue"

        def retrieve_dense(state: QueryState) -> dict:
            with stage_timer("retrieve_dense"), \
                    get_tracer().start_as_current_span("retrieve_dense") as span:
                span.set_attribute("user_acl", state["user"].acl.name)
                chunks = service.dense_search(state["question"],
                                              state["user"].acl,
                                              state["top_k"])
                span.set_attribute("chunks_count", len(chunks))
                return {"dense_chunks": chunks}

        def retrieve_lexical(state: QueryState) -> dict:
            with stage_timer("retrieve_lexical"), \
                    get_tracer().start_as_current_span("retrieve_lexical") as span:
                span.set_attribute("user_acl", state["user"].acl.name)
                chunks = service.lexical_search(state["question"],
                                                state["user"].acl,
                                                state["top_k"])
                span.set_attribute("chunks_count", len(chunks))
                return {"lexical_chunks": chunks}

        def graph_expand(state: QueryState) -> dict:
            with stage_timer("graph_expand"), \
                    get_tracer().start_as_current_span("graph_expand") as span:
                span.set_attribute("user_acl", state["user"].acl.name)
                chunks = service.graph_search(state["question"],
                                              state["user"].acl,
                                              state["top_k"])
                span.set_attribute("chunks_count", len(chunks))
                return {"graph_chunks": chunks}

        def fuse_rerank(state: QueryState) -> dict:
            with stage_timer("fuse"), \
                    get_tracer().start_as_current_span("fuse_rerank") as span:
                span.set_attribute("user_acl", state["user"].acl.name)
                fused = rrf_fuse(
                    {
                        "dense": state["dense_chunks"],
                        "lexical": state["lexical_chunks"],
                        "graph": state["graph_chunks"],
                    },
                    state["user"].acl,
                    top_n=state["top_k"],
                    user_id=state["user"].user_id,
                    audit=self._audit,
                )
                span.set_attribute("chunks_count", len(fused))
                return {"chunks": fused}

        def generate(state: QueryState) -> dict:
            with stage_timer("generate"), \
                    get_tracer().start_as_current_span("generate") as span:
                span.set_attribute("user_acl", state["user"].acl.name)
                chunks = state["chunks"]
                span.set_attribute("chunks_count", len(chunks))
                stream = stream_answer(
                    state["question"], chunks, llm,
                    max_tokens=self.max_tokens, temperature=self.temperature,
                )
                user = state["user"]
                violations: list[str] = []

                def on_violations(found: list[str]) -> None:
                    violations.extend(found)
                    if found:
                        self._log(user.user_id, audit_mod.OUTPUT_SANITIZED,
                                  {"violations": found})

                guarded = output_guard.wrap_stream(stream, chunks,
                                                   on_violations=on_violations)
                # ленивый стрим: время выдачи фрагментов (LLM + санитизация)
                # наблюдается в securekb_stage_latency_seconds{stage="output_guard"}
                return {"answer_stream": _timed_stream(guarded, "output_guard"),
                        "guard_violations": violations}

        graph = StateGraph(QueryState)
        graph.add_node("input_guard", input_guard)
        graph.add_node("retrieve_dense", retrieve_dense)
        graph.add_node("retrieve_lexical", retrieve_lexical)
        graph.add_node("graph_expand", graph_expand)
        graph.add_node("fuse_rerank", fuse_rerank)
        graph.add_node("generate", generate)
        graph.add_edge(START, "input_guard")
        graph.add_conditional_edges(
            "input_guard", route_after_input_guard,
            {"continue": "retrieve_dense", "block": END},
        )
        graph.add_edge("retrieve_dense", "retrieve_lexical")
        graph.add_edge("retrieve_lexical", "graph_expand")
        graph.add_edge("graph_expand", "fuse_rerank")
        graph.add_edge("fuse_rerank", "generate")
        graph.add_edge("generate", END)
        return graph

    # -- запуск -------------------------------------------------------------------

    def run(self, question: str, user: User, top_k: int = 5) -> QueryResult:
        """Прогон графа до generate: возвращает chunks + ленивый answer_stream."""
        final: QueryState = self._app.invoke(
            {"question": question, "user": user, "top_k": top_k}
        )
        debug = dict(final.get("debug", {}))
        debug["branches"] = {
            "dense": [c.chunk_id for c in final.get("dense_chunks", [])],
            "lexical": [c.chunk_id for c in final.get("lexical_chunks", [])],
            "graph": [c.chunk_id for c in final.get("graph_chunks", [])],
        }
        blocked = bool(final.get("blocked"))
        return QueryResult(
            answer=BLOCKED_ANSWER if blocked else "",
            chunks=final.get("chunks", []),
            debug=debug,
            answer_stream=final.get("answer_stream"),
            blocked=blocked,
            guard_violations=final.get("guard_violations", []),
        )


def consume_answer(result: QueryResult) -> str:
    """Вычитывает answer_stream в result.answer (для не-стриминговых клиентов)."""
    if result.answer_stream is None:
        return result.answer
    parts = list(result.answer_stream)
    result.answer = "".join(parts)
    result.answer_stream = iter([])  # защита от повторного потребления
    return result.answer


def build_engine(llm: LLM | None = None, max_tokens: int | None = None,
                 temperature: float | None = None) -> QueryEngine:
    """Дефолтный движок: RetrievalService + QwenLLM + guardrails из Settings."""
    from backend.app.core.config import get_settings

    settings = get_settings()
    return QueryEngine(
        get_retrieval_service(),
        llm or QwenLLM.get(),
        max_tokens=max_tokens or settings.llm_max_tokens,
        temperature=settings.llm_temperature if temperature is None else temperature,
        injection_guard=InjectionGuard(enabled=settings.guard_injection_enabled),
        input_pii_guard=InputPIIGuard(enabled=settings.guard_input_pii_enabled),
        output_guard=OutputGuard(enabled=settings.guard_output_enabled),
    )


def run_query_graph(question: str, user: User, top_k: int = 5,
                    engine: QueryEngine | None = None) -> QueryResult:
    """Удобная точка входа: построить/переиспользовать движок и выполнить запрос."""
    engine = engine or build_engine()
    return engine.run(question, user, top_k=top_k)
