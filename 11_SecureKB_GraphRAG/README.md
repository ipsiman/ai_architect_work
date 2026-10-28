# SecureKB

Защищённая RAG-платформа (Retrieval-Augmented Generation — генерация,
дополненная поиском) корпоративных знаний — завершённый финальный проект курса
«ИИ-Архитектор»: гибридный поиск Qdrant (плотный векторный) + BM25 (Best Match 25 —
лексический) со слиянием RRF (Reciprocal Rank Fusion — обратное ранговое слияние),
графовое расширение GraphRAG-lite (SQLite), локальная LLM Qwen2.5-1.5B (llama.cpp), guardrails (инъекции,
PII — персональные данные, санитизация ответа), RBAC (Role-Based Access Control —
доступ по ролям): public / internal / secret, стриминг SSE (Server-Sent Events),
наблюдаемость OTel (OpenTelemetry) / Prometheus / Grafana / Jaeger.

Точка входа в документацию — `docs/00-master-plan.md`, сокращения — `docs/07-glossary.md`.

## Быстрый старт

```bash
cp .env.example .env                              # 1. конфигурация
docker compose -f infra/docker-compose.yml up -d  # 2. qdrant, prometheus, grafana, jaeger
uv sync                                           # 3. зависимости в .venv
uv run python scripts/download_model.py           # 4. GGUF-модель → models/
uv run python scripts/generate_corpus.py && uv run python scripts/ingest.py --recreate  # 5. корпус + ингест (чанки, эмбеддинги, граф)
uv run uvicorn backend.app.main:app --port 8000   # 6. поднять API
curl localhost:8000/health                        # 7. проверка: {"status":"ok",...}
```

Пример запроса (стриминг SSE):

```bash
curl -N -X POST localhost:8000/api/chat -H 'Content-Type: application/json' \
  -d '{"question":"какой бюджет у проекта Феникс?","user_id":"alice","stream":true}'
```

Пользователи и их уровни ACL (Access Control List — список контроля доступа):
`alice` (secret) / `bob` (internal) / `guest` (public); демо защиты —
`bash scripts/demo.sh`.

## Структура проекта

```
backend/app/
  core/        config (pydantic-settings), logging
  api/         health, chat (SSE), admin (users/audit/stats)
  graphs/      query graph (LangGraph: guard → retrieve → fuse → generate)
  ingestion/   parser, chunker, PII, ACL, embedder, qdrant/graph stores,
               graphs/ (ingestion graph)
  retrieval/   dense (Qdrant), lexical (BM25), graph_expand, fuse (RRF)
  generation/  LLM (llama.cpp), prompts, answer_service
  security/    users (RBAC), guardrails, audit
  obs/         metrics (Prometheus), tracing (OTel)
backend/tests/    18 файлов, 129 тестов (pytest)
infra/            docker-compose (qdrant, prometheus, grafana, jaeger)
scripts/          download_model.py, generate_corpus.py, ingest.py, demo.sh,
                 load_test.py, quality_gate.py, backup.sh, visualize_graph.py
docs/             00–07 + adr/ (6 ADR — Architecture Decision Record, записи
                 об архитектурных решениях); data/, models/ — артефакты (gitignored)
```

## Наблюдаемость

| Сервис     | URL                          |
|------------|------------------------------|
| Grafana    | http://localhost:3000 (admin/admin), дашборд uid `securekb` |
| Jaeger     | http://localhost:16686 (сервис `securekb`) |
| Prometheus | http://localhost:9090        |
| Qdrant     | http://localhost:6333/dashboard |

Основные метрики (одной строкой): `securekb_requests_total{route,status}`,
`securekb_request_latency_seconds{route}`, `securekb_stage_latency_seconds{stage}`
(стадии: input_guard, retrieve_dense, retrieve_lexical, graph_expand, fuse, generate, output_guard),
`securekb_ttft_seconds`, `securekb_tokens_total`,
`securekb_guardrail_events_total{kind}`. OTel-спаны: FastAPI-уровень (`chat`)
+ по узлу query graph (`input_guard`, `retrieve_dense`, `retrieve_lexical`,
`graph_expand`, `fuse_rerank`, `generate`) с атрибутами `user_acl`, `chunks_count`, `blocked`.

## Бэкап и восстановление

```bash
bash scripts/backup.sh   # снапшот Qdrant + data/graph.db → data/backups/
```

Скрипт создаёт `data/backups/qdrant-DATE.snapshot` (снапшот коллекции `securekb_chunks`)
и `data/backups/graph-DATE.db` (согласованная копия графа через `sqlite3 .backup`),
затем печатает созданные файлы.

Восстановление:

- Qdrant из снапшота (восстановление коллекции `securekb_chunks`):
  `curl -X PUT localhost:6333/collections/securekb_chunks -H 'Content-Type: application/json' -d '{"snapshot": "<абсолютный путь к data/backups/qdrant-DATE.snapshot>", "priority": "snapshot"}'`
- graph.db: остановить бэкенд, заменить `data/graph.db` файлом
  `data/backups/graph-DATE.db`, запустить бэкенд снова.

## Качество и тесты

- `uv run pytest -q` — 129 тестов, 0 пропущено (docs/03).
- `uv run python scripts/quality_gate.py` — golden Q/A гейт: порог ≥ 0.70, факт 0.87 (13/15).
- `uv run python scripts/load_test.py` — нагрузочный тест (отчёт — docs/04).

## Интерактивная презентация

`demo/presentation.html` — автономный offline-файл (открыть в браузере; 
листание — стрелки/клик/свайп, F — полный экран). Слайды 08 содержат графики, 
отрисованные из реальных метрик Prometheus, снятых с работающего стенда. 

