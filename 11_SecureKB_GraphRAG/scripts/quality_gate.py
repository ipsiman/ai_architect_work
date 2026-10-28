"""Go/No-Go гейт качества RAG-пайплайна SecureKB (этап C).

Для каждого вопроса из data/golden_qa.json выполняет run_query_graph от
пользователя с ACL, соответствующим ожидаемому уровню документа
(secret → alice, internal → bob, public → guest).

Вопрос засчитан, если:
  (a) expected_doc_id встречается среди top-k чанков результата, И
  (b) key_fact_substring найден в ответе LLM (регистронезависимо).

Итог: доля успешных ≥ THRESHOLD → GO.

Запуск: uv run python scripts/quality_gate.py [--top-k 5] [--max-tokens 512]
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backend.app.core.logging import get_logger, setup_logging  # noqa: E402

logger = get_logger(__name__)

GOLDEN_PATH = ROOT / "data" / "golden_qa.json"
THRESHOLD = 0.70

# ожидаемый уровень документа → пользователь, который его видит
ACL_TO_USER = {
    "secret": "alice",
    "internal": "bob",
    "public": "guest",
}


def main() -> None:
    parser = argparse.ArgumentParser(description="Go/No-Go гейт качества SecureKB")
    parser.add_argument("--top-k", type=int, default=5)
    parser.add_argument("--max-tokens", type=int, default=None,
                        help="переопределить лимит генерации (по умолчанию из Settings)")
    args = parser.parse_args()

    setup_logging()
    questions = json.loads(GOLDEN_PATH.read_text(encoding="utf-8"))
    print(f"Гейт качества SecureKB: {len(questions)} вопросов, top_k={args.top_k}, "
          f"порог GO = {THRESHOLD:.2f}")

    from backend.app.core.config import get_settings
    from backend.app.generation.llm import QwenLLM
    from backend.app.graphs.query_graph import QueryEngine, build_engine, consume_answer
    from backend.app.security.users import get_user_store

    settings = get_settings()
    engine: QueryEngine = build_engine(
        QwenLLM.get(),
        **({"max_tokens": args.max_tokens} if args.max_tokens else {}),
    )
    users = get_user_store()

    rows = []
    passed = 0
    for i, item in enumerate(questions, start=1):
        user = users.get_user(ACL_TO_USER[item["expected_acl"]])
        t0 = time.perf_counter()
        result = engine.run(item["question"], user, top_k=args.top_k)
        answer = consume_answer(result)
        elapsed = time.perf_counter() - t0

        top_docs = [c.doc_id for c in result.chunks]
        hit_doc = item["expected_doc_id"] in top_docs
        hit_fact = item["key_fact_substring"].lower() in answer.lower()
        ok = hit_doc and hit_fact
        passed += int(ok)
        rows.append((i, item, top_docs, answer, hit_doc, hit_fact, ok, elapsed))

    print()
    header = f"{'#':>2}  {'ACL':<8} {'doc':<9} {'top5':<6} {'факт':<6} {'OK':<3} вопрос"
    print(header)
    print("-" * len(header))
    for i, item, top_docs, answer, hit_doc, hit_fact, ok, elapsed in rows:
        mark = "OK" if ok else "--"
        print(f"{i:>2}  {item['expected_acl']:<8} "
              f"{'да' if hit_doc else 'НЕТ':<8} "
              f"{'да' if hit_fact else 'НЕТ':<6} "
              f"{mark:<3} {item['question'][:52]}")
        if not ok:
            print(f"    ожидался {item['expected_doc_id']} (факт: "
                  f"{item['key_fact_substring']!r}); top-5: {top_docs}")
            print(f"    ответ: {answer[:200]!r}")

    share = passed / len(questions)
    verdict = "GO" if share >= THRESHOLD else "NO-GO"
    print("-" * len(header))
    print(f"Успешных: {passed}/{len(questions)} = {share:.2f} (порог {THRESHOLD:.2f})")
    print(f"Вердикт: {verdict}")
    sys.exit(0 if verdict == "GO" else 1)


if __name__ == "__main__":
    main()
