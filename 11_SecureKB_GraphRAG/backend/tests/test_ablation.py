"""Ablation-тест (этап D): вклад graph_expand в recall@5 по golden_qa.json.

Сравнивает долю вопросов, где expected_doc_id ∈ top-5, при graph_expand
ВКЛ (полный пайплайн) vs ВЫКЛ (ветка graph заменена на pass-through —
service.graph_search → []). Печатает таблицу и дельту.

Требует Qdrant с реальной коллекцией securekb_chunks (skip иначе) и
реальный E5-эмбеддер; LLM = FakeLLM (recall не зависит от генерации).

Запуск с выводом таблицы: uv run pytest backend/tests/test_ablation.py -s
"""

import json
from pathlib import Path

import pytest

from backend.app.core.config import get_settings
from backend.app.generation.llm import FakeLLM
from backend.app.graphs.query_graph import QueryEngine
from backend.app.retrieval.retrieval_service import get_retrieval_service
from backend.app.security.audit import NullAuditLogger

GOLDEN_PATH = Path(__file__).resolve().parents[2] / "data" / "golden_qa.json"
ACL_TO_USER = {"secret": "alice", "internal": "bob", "public": "guest"}


@pytest.fixture()
def engine(monkeypatch: pytest.MonkeyPatch):
    """Движок на реальном retrieval-стеке; OTel выключен, аудит no-op."""
    try:
        from backend.app.ingestion.qdrant_store import make_client

        client = make_client(get_settings().qdrant_url, timeout=2)
        client.get_collections()
    except Exception:
        pytest.skip("qdrant not running")

    monkeypatch.setenv("SECUREKB_OTEL_ENABLED", "false")
    get_settings.cache_clear()
    service = get_retrieval_service()
    yield QueryEngine(service, FakeLLM(), audit=NullAuditLogger())
    get_settings.cache_clear()


def top5(engine: QueryEngine, question: str, user) -> list[str]:
    return [c.doc_id for c in engine.run(question, user, top_k=5).chunks]


def test_graph_expand_ablation_recall(engine: QueryEngine):
    """recall@5 (graph ON) ≥ recall@5 (graph OFF): граф не вредит и помогает
    на entity-bridge вопросах (см. поле note в golden_qa.json)."""
    from backend.app.security.users import get_user_store

    questions = json.loads(GOLDEN_PATH.read_text(encoding="utf-8"))
    users = get_user_store()
    service = engine.service

    rows_on, rows_off = [], []
    for item in questions:
        user = users.get_user(ACL_TO_USER[item["expected_acl"]])
        docs_on = top5(engine, item["question"], user)

        # graph ВЫКЛ: узел graph_expand → pass-through (ветка возвращает [])
        original = service.graph_search
        service.graph_search = lambda *a, **k: []
        try:
            docs_off = top5(engine, item["question"], user)
        finally:
            service.graph_search = original

        hit_on = item["expected_doc_id"] in docs_on
        hit_off = item["expected_doc_id"] in docs_off
        rows_on.append(hit_on)
        rows_off.append(hit_off)

        gained = "  ← граф помог" if hit_on and not hit_off else ""
        lost = "  ← граф навредил" if hit_off and not hit_on else ""
        print(f"{'+' if hit_on else '-'}{'+' if hit_off else '-'}  "
              f"{item['question'][:62]:<62} want={item['expected_doc_id']}"
              f"{gained}{lost}")

    recall_on = sum(rows_on) / len(rows_on)
    recall_off = sum(rows_off) / len(rows_off)
    helped = sum(1 for a, b in zip(rows_on, rows_off) if a and not b)
    harmed = sum(1 for a, b in zip(rows_on, rows_off) if b and not a)

    print("-" * 90)
    print(f"recall@5  graph=ON : {sum(rows_on)}/{len(rows_on)} = {recall_on:.3f}")
    print(f"recall@5  graph=OFF: {sum(rows_off)}/{len(rows_off)} = {recall_off:.3f}")
    print(f"дельта = {recall_on - recall_off:+.3f}  "
          f"(граф помог на {helped} вопросах, навредил на {harmed})")

    assert recall_on >= recall_off, (
        f"graph_expand ухудшает retrieval: on={recall_on:.3f} < off={recall_off:.3f}"
    )
