# Мастер-план финального проекта «SecureKB»

Курс «ИИ-Архитектор», финальный проект.

---

## 1. Тема и соответствие заданию

**Тема:** «Защищённая платформа мультимодального анализа корпоративных знаний на графовых подходах».

**Кодовое имя решения:** SecureKB — защищённая база корпоративных знаний с гибридным поиском и графовым расширением (GraphRAG-lite).

**Выбранные MVP-направления** (MVP — Minimum Viable Product, минимально жизнеспособный продукт):
1. **Advanced RAG** (RAG — Retrieval-Augmented Generation, генерация, дополненная поиском) — гибридный retrieval (плотные векторы + лексический поиск) + графовое расширение результатов + re-ranking через RRF (Reciprocal Rank Fusion — обратное ранговое слияние).
2. **Security** — Guardrails (входные/выходные фильтры), маскирование PII (Personally Identifiable Information — персональные данные), RBAC (Role-Based Access Control — доступ по ролям) на уровне чанков с defense-in-depth.

**Соблюдение ключевых ограничений:**
- Закрытый контур: только локальные open-source модели, никаких облачных API.
- MVP, не продакшен; синтетические данные (~100 документов).
- Обучение модели не выполняется — только pre-trained модели.
- Оркестрация конвейера — LangGraph StateGraph (State Machine), не линейный скрипт.
- Секреты не в Git — конфигурация через `.env` + `.env.example`.
- Диаграммы рендерятся из кода (Mermaid).

**Зафиксированные отступления (ограничение + допущение + снижение риска):**

| № | Отступление | Ограничение | Допущение MVP | Снижение риска / целевая архитектура |
|---|---|---|---|---|
| О-1 | Мультимодальность (сканы, чертежи) сужена до текстовых документов | Нет GPU-инференса VLM (Vision Language Model) на рабочей машине | Только текстовые документы (txt/md) | Целевая: OCR-адаптер (PaddleOCR) + Qwen-VL; ADR-001 (ADR — Architecture Decision Record, запись об архитектурном решении) и план перехода |
| О-2 | vLLM/SGLang заменён на llama.cpp (GGUF-квантование) | vLLM требует Linux + NVIDIA GPU | Локальный инференс на Apple Silicon (Metal) | Целевая: vLLM + AWQ на GPU-ноде; ADR-001 (в альтернативах: vLLM, SGLang, TGI, llama.cpp) |
| О-3 | Полноценный GraphRAG (Neo4j + LLM-экстракция; LLM — Large Language Model, большая языковая модель) заменён на GraphRAG-lite | Бюджет MVP, латентность извлечения | Rule-based экстрактор сущностей (онтология 4 типов узлов) + граф в SQLite + графовое расширение поиска | Целевая: Neo4j + LLM-экстракция; ADR-004. Несущность графа доказывается ablation-тестом (§2.5) |
| О-4 | LangGraph используется как тонкая State Machine-обёртка конвейера | — (не отступление, а минимизация) | Один граф `ingest_graph` + один граф `query_graph` с узлами-этапами | ADR-003: сравнение LangGraph / LlamaIndex / линейный пайплайн |

---

## 2. Архитектура решения (MVP)

### 2.1 Конвейер данных (оркестрация LangGraph StateGraph)

**Ingestion-граф:**
```
Документ (txt/md, ~100 синтетических RU-документов)
  → [parse] Парсинг → [chunk] Чанкинг (500 токенов, перекрытие 50)
  → [pii] PII-детекция и маскирование (regex: телефоны, e-mail, ИНН, паспорта)
  → [acl] Присвоение метки доступа (public / internal / secret)
  → [embed] Эмбеддинги (intfloat/multilingual-e5-small, 384 dim)
  → [store_vec] Qdrant (payload: текст, метка ACL, метаданные)
  → [extract] Параллельно: экстракция сущностей → [store_graph] граф в SQLite
      (онтология: Document, Person, Project, Department)
```

**Query-граф (State Machine, каждый узел — этап):**
```
Вопрос → [input_guard] Input Guardrail (prompt-injection паттерны, PII-маскирование)
  → [retrieve_dense] Qdrant c ACL-фильтром пользователя (pre-filter)
  → [retrieve_lexical] BM25 по per-ACL индексу (индекс строится на уровень доступа отдельно)
  → [graph_expand] Графовое расширение (GraphRAG-lite): сущности вопроса → соседние документы,
      соседи фильтруются по ACL
  → [fuse_rerank] Слияние RRF + re-ranking
  → [acl_check] КОНТРОЛЬНАЯ ACL-проверка финального контекста (defense-in-depth,
      в обход Qdrant-фильтра пройти нельзя даже при ошибке в BM25/графе)
  → [generate] LLM (Qwen2.5-1.5B-Instruct GGUF Q4_K_M, llama.cpp, Metal) → ответ с цитатами
  → [output_guard] Output Guardrail (утечка PII / маркеров {SECRET})
  → [stream] Стриминг ответа клиенту (SSE — Server-Sent Events)
```

### 2.2 Компоненты и стек (MVP)

| Компонент | Технология MVP | Обоснование |
|---|---|-|
| API | FastAPI + стриминг SSE (SSE — Server-Sent Events, потоковая передача событий) | async; OpenAPI |
| Оркестрация | LangGraph (StateGraph) | State Machine, не линейные скрипты |
| Векторная БД | Qdrant (Docker) | Self-hosted, payload-фильтры ACL |
| Граф знаний | SQLite (nodes/edges) | MVP-достаточно; ADR-004 (Neo4j / SQLite+NetworkX / PostgreSQL+AGE) |
| LLM | Qwen/Qwen2.5-1.5B-Instruct GGUF Q4_K_M через llama-cpp-python (Metal), ~1 ГБ | Лучшее качество русского языка в классе малых моделей; квантование — опыт AWQ-аналога на CPU/Metal; ADR-001 |
| Эмбеддер | intfloat/multilingual-e5-small (384 dim, ~470 МБ) | Мультиязычная (RU), лёгкая |
| Лексический поиск | BM25 (Best Match 25; реализация rank-bm25), индексы per-ACL-уровень | Гибридность без утечки через обход фильтра |
| Re-ranking | RRF-only (без cross-encoder) | Бюджет моделей <2 ГБ суммарно; cross-encoder — в целевой архитектуре (ADR-006) |
| Безопасность | RBAC pre-filter (Qdrant) + per-ACL BM25 + post-RRF ACL-контроль + guardrails; ACL — Access Control List, список контроля доступа | Defense-in-depth (3 контура) |
| Сессии/логи | SQLite (sessions, audit_log) | Нужны для ER-диаграммы (ER — Entity-Relationship, модель «сущность—связь») и Memory-модуля C4 L3 (C4 — Context, Containers, Components, Code) |
| Наблюдаемость | OpenTelemetry (OTel) → Jaeger (Docker) + Prometheus + Grafana | Требование демо трейсов (Jaeger/Langfuse) |
| Тесты | pytest + golden Q/A (детерминированные проверки: цитата найдена / отказ корректен) + ablation | Требование; LLM-as-a-Judge той же моделью отклонён (судья слабее подследственного) |
| Нагрузочные тесты | asyncio/httpx: RPS (Requests Per Second — запросов в секунду), p50/p95/p99, TTFT (Time To First Token — время до первого токена), латентность до N-го токена | |
| Окружение | uv (venv), Python 3.12, Docker Compose | |

### 2.3 Безопасность: три контура Defense-in-Depth

1. **Контур 1 (pre-filter):** Qdrant-поиск всегда с фильтром по ACL-метке пользователя.
2. **Контур 2 (изоляция):** BM25-индексы строятся отдельно на каждый ACL-уровень; графовое расширение фильтрует соседей по ACL — кандидаты из чужого уровня физически не попадают в слияние RRF.
3. **Контур 3 (финальный контроль):** перед подачей контекста в LLM — контрольная проверка ACL каждого чанка; при несоответствии чанк отбрасывается + событие в audit_log (это защита от багов в контурах 1–2).

**Критерий проверяется автотестом на всём конвейере** (не только dense-поиска): `user_b (internal)` спрашивает про секретный проект — в финальном контексте и ответе нет чанков уровня `secret`; `user_a (secret)` — получает полный ответ с цитатой.

**Декомпозиция guardrails (не «один фильтр на входе»):**

| Guardrail | Где стоит | Почему отдельно | Что НЕ проверяет | Red-team тест |
|---|---|---|---|---|
| Injection-детектор (вход) | до retrieval | отсечь атаку до затрат на поиск | PII, качество ответа | «ignore previous instructions…», «ты теперь DAN…», jailbreak-шаблоны |
| PII-маскировщик (вход) | до retrieval | не отправлять PII в контекст/логи | инъекции | запрос с телефоном/ИНН → замаскирован |
| ACL-контроль (контуры 1–3) | retrieval + перед LLM | разграничение доступа к данным | корректность самого текста | User B → секретный документ |
| PII/SECRET-фильтр (выход) | после LLM | модель могла пересказать PII из контекста | галлюцинации без PII | ответ содержит {SECRET}-маркер → редуцируется |

### 2.4 Control Plane / Data Plane — обе интерпретации

В документе `02-architecture.md` показаны две оси:
1. **По заданию:** Control Plane = агенты/оркестрация (LangGraph-графы, админ-эндпоинты, реиндексация), Data Plane = БД и модели (Qdrant, SQLite, эмбеддер, LLM).
2. **По доступу:** Control Plane = `/admin/*` (управление), Data Plane = `/api/chat`, `/api/ingest` (пользовательский путь). В целевой архитектуре — раздельные сети/подсети, DMZ-сегмент для внешнего доступа, Vault для секретов.

### 2.5 Доказательство несущности графа (ablation-тест)

Автотест + замер: `graph_expand` выключен → полнота правильных источников на golden Q/A падает (фиксируется дельта). Плюс дешёвая визуализация графа знаний (graphviz PNG из SQLite) для демо. Отвечает на требование «простой векторный поиск не принимается».

---

## 3. Структура монорепозитория

```
final_project/
├── README.md                 # запуск, переменные окружения, бэкап (снапшот Qdrant + dump SQLite)
├── pyproject.toml            # uv-проект, зависимости, pytest-конфиг
├── .env.example              # шаблон секретов (сами секреты не в git)
├── .gitignore
├── models/                   # GGUF-модели (gitignored)
├── data/                     # корпус, БД, артефакты (gitignored, кроме seed)
├── backend/
│   ├── app/
│   │   ├── main.py           # FastAPI-приложение
│   │   ├── core/             # конфиг (pydantic-settings), логирование
│   │   ├── api/              # роутеры: chat (SSE), ingest, admin, health
│   │   ├── graphs/           # LangGraph: ingest_graph, query_graph
│   │   ├── ingestion/        # парсинг, чанкер, PII, эмбеддер, graph-extractor
│   │   ├── retrieval/        # dense (Qdrant+ACL), lexical (per-ACL BM25), graph-expand, RRF
│   │   ├── generation/       # llama.cpp-обёртка, промпты, streaming, цитирование
│   │   ├── security/         # RBAC, guardrails (таблица §2.3), PII-детектор, audit_log
│   │   └── obs/              # OTel→Jaeger, Prometheus-метрики (вкл. TTFT)
│   └── tests/                # unit + integration + golden QA + ablation + load
├── infra/
│   ├── docker-compose.yml    # Qdrant, Prometheus, Grafana, Jaeger
│   ├── prometheus.yml
│   └── grafana/              # провижининг: datasource + дашборд
├── scripts/
│   ├── generate_corpus.py    # синтетический корпус 100 документов
│   ├── ingest.py             # CLI-загрузка корпуса
│   ├── backup.sh             # снапшот Qdrant + dump SQLite
│   ├── visualize_graph.py    # graphviz PNG из SQLite
│   └── load_test.py          # RPS, перцентили, TTFT
└── docs/
    ├── 00-master-plan.md     # этот план
    ├── 01-требования.md      # ТЗ
    ├── 02-architecture.md    # C4 L1–L3, DFD, sequence, ER, deployment (Mermaid)
    ├── adr/                  # ADR-001..006, каждый ≥3 альтернативы
    ├── 03-test-plan.md
    ├── 04-load-report.md     # реальные замеры: RPS, p50/p95/p99, TTFT
    ├── 05-tco.md             # TCO и окупаемость
    ├── 06-mvp-to-target.md   # целевая архитектура + план перехода
    └── 07-glossary.md        # словарь сокращений
```

---

## 4. Этапы реализации (план → реализация → тесты)

| Этап | Содержание | Критерий готовности |
|---|---|---|
| **A. Каркас** | Монорепо, uv-зависимости, docker-compose (Qdrant/Prometheus/Grafana/Jaeger), скелет FastAPI + health + метрики + OTel-экспорт, .env.example, README-каркас | Стек поднимается compose; `/health` 200; `/metrics` отдаёт счётчики; спаны уходят в Jaeger; pytest зелёный |
| **B. Данные и Ingestion** | Корпус (~100 RU-док, 3 ACL-уровня, PII внутри), чанкер, PII-маскирование, эмбеддер (e5-small), graph-extractor, Qdrant+SQLite, LangGraph ingest_graph; ADR-002/004 черновики | 100 док → N чанков (ACL в payload), M узлов/рёбер; unit-тесты чанкера/PII/экстрактора зелёные |
| **C. Retrieval + Generation** | dense+lexical(per-ACL)+graph_expand+RRF; llama.cpp LLM, промпт с цитатами, SSE; go/no-go гейт: golden Q/A ≥70% правильных источников (если Qwen2.5-1.5B не справляется — fallback Qwen3-1.7B/Qwen2.5-3B-Q4, решение в ADR-001); ADR-001/003/006 черновики | e2e: вопрос → ответ с корректными цитатами; гейт пройден |
| **D. Security + Качество** | Три контура ACL, guardrails-таблица (§2.3) + red-team тесты, audit_log, сессии, golden Q/A детерминированные проверки, ablation-тест графа | Автотест: User B full-pipeline без secret-чанков; инъекции блокируются; ablation-дельта зафиксирована |
| **E. Observability + Нагрузка** | OTel-спаны по узлам графа → Jaeger (демо-трейс), Prometheus: латентность этапов, RPS, guardrail-счётчики, TTFT; Grafana-дашборд; нагрузочный тест; прототип бэкапа | Дашборд живой; `04-load-report.md` с реальными RPS/p50/p95/p99/TTFT |
| **F. Документация** | Сверка и упаковка: ТЗ, C4 L1–L3, DFD (Data Flow Diagram — диаграмма потоков данных), Sequence, ER (вкл. сессии/RBAC), Deployment (DMZ, Vault — целевая), ADR×6 финал, TCO, план перехода, README (запуск+бэкап+секреты), глоссарий | Все 5 обязательных артефактов; расхождений с кодом нет |

**ADR-реестр (по 3+ альтернативы):**
- ADR-001: Инференс LLM (vLLM vs SGLang vs TGI vs llama.cpp-GGUF)
- ADR-002: Векторная БД (Qdrant vs Milvus vs Weaviate vs pgvector)
- ADR-003: Оркестрация (LangGraph vs LlamaIndex vs линейный пайплайн)
- ADR-004: Графовое хранение (Neo4j vs SQLite+NetworkX vs PostgreSQL+AGE)
- ADR-005: Стратегия ACL в retrieval (pre-filter vs post-filter vs отдельные коллекции vs per-ACL индексы+финальный контроль)
- ADR-006: Re-ranking (cross-encoder vs RRF-only vs LLM-rerank)

---

## 5. Риски и их снижение

| Риск | Вероятность | Снижение риска |
|---|---|---|
| LLM 1.5B-Q4 не даёт внятных RU-ответов с цитатами | Средняя | Go/no-go гейт на старте этапа C по golden Q/A; fallback: Qwen3-1.7B или Qwen2.5-3B-Q4_K_M (~2 ГБ); строгий промпт «отвечай только по контексту» |
| Медленный инференс на Metal → низкий RPS | Высокая | Ожидаемо: фиксируется в load-report и TCO (TCO — Total Cost of Ownership, совокупная стоимость владения); в проде GPU/vLLM; кэш ответов |
| Утечка через BM25/граф в обход ACL | Закрыто дизайном | Три контура (§2.3) + full-pipeline автотест User B |
| Субагенты/окружение падают | Средняя | Маленькие самостоятельные этапы, перезапуск этапа |

## 6. Что НЕ входит в MVP (зафиксировано, план перехода в docs/06)

- OCR/мультимодальность (VLM), Neo4j, vLLM+GPU, K8s/Helm, S3, SSO/LDAP (мок-пользователи), веб-GUI (REST+CLI достаточно), LLM-as-a-Judge как основной тест (заменён детерминированными проверками), cross-encoder reranker (только в целевой архитектуре).

