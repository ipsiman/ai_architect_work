# Архитектура системы SecureKB

Архитектура защищённой базы корпоративных знаний SecureKB: RAG (Retrieval-Augmented
Generation — генерация, дополненная поиском) с гибридным
поиском, графовым расширением (GraphRAG-lite) и защитой данных на всех этапах.
Требования — `docs/01-требования.md`, план и обоснования — `docs/00-master-plan.md`,
нагрузочные испытания — `docs/04-load-report.md`. Диаграммы приведены в нотации
Mermaid и соответствуют реализованному коду; целевые, не реализованные в MVP
схемы помечены отдельно.

## 1. Обзор и принципы

SecureKB — FastAPI-приложение, оркестрируемое двумя графами LangGraph (ingest
и query). Внешние сервисы — контейнеры docker compose: Qdrant, Prometheus,
Grafana, Jaeger. Граф знаний, пользователи и аудит — один файл SQLite
(data/graph.db); инференс LLM (LLM — Large Language Model, большая языковая
модель; Qwen2.5-1.5B GGUF) и эмбеддер
(multilingual-e5-small) исполняются в процессе приложения на Apple Silicon
(Metal).

Принципы:

- Security by design: ACL (Access Control List — список контроля доступа)
  попадает в payload точки при индексации, PII (Personally Identifiable
  Information — персональные данные) маскируется до эмбеддинга, фильтры действуют
  в каждой ветке поиска.
- Defense in depth: три независимых контура ACL — pre-filter Qdrant, per-ACL
  BM25-индексы (BM25 — Best Match 25), проверка после слияния RRF (Reciprocal
  Rank Fusion — обратное ранговое слияние) — плюс выходной guardrail.
- Закрытый контур: модели и данные локальны, внешние вызовы из пути запроса
  исключены.
- MVP и целевая архитектура: llama.cpp вместо vLLM, SQLite вместо Neo4j, текст
  вместо мультимодальности (раздел 5 требований); целевые замены — в разделе 8.

## 2. Контекст системы (C4 — Context, Containers, Components, Code; уровень 1)

Единственный актор — пользователь; внешних интеграций нет, контур закрыт.

```mermaid
flowchart LR
    U["Пользователь знаний<br/>alice (secret) / bob (internal) / guest (public)"]
    S["SecureKB<br/>защищённая база корпоративных знаний<br/>RAG + гибридный поиск + GraphRAG-lite"]
    X["Внешние облачные API и LLM-сервисы<br/>отсутствуют: закрытый контур"]

    U -->|"вопрос на русском,<br/>POST /api/chat :8000"| S
    S -->|"SSE: ответ с цитатами [N],<br/>sources, done(timings)"| U

    classDef external fill:#eee,stroke:#999,stroke-dasharray: 5 5
    class X external
```

## 3. Контейнеры (C4, уровень 2)

Приложение и LLM-рантайм — host-процесс uvicorn; инфраструктура — контейнеры
`infra/docker-compose.yml`.

```mermaid
flowchart LR
    subgraph HOST["Рабочая станция (macOS arm64)"]
        HC["HTTP-клиент / curl (REST + SSE)"]
        subgraph PROC["Host-процесс: uvicorn :8000"]
            API["FastAPI App<br/>/api/chat (SSE/JSON), /admin/*, /health, /metrics"]
            ENG["LangGraph-движки: ingest_graph, query_graph,<br/>guardrails, аудит, эмбеддер e5-small"]
            LRT["LLM-рантайм llama-cpp-python (Metal)<br/>Qwen2.5-1.5B GGUF Q4_K_M, n_ctx 4096, decode-lock"]
            SQLF[("SQLite data/graph.db<br/>nodes, edges, users, audit_log")]
            OTS["OTel SDK / экспортёр"]
        end
        subgraph DOCK["Docker Desktop (docker compose)"]
            QD[("Qdrant v1.12.5 :6333")]; PR["Prometheus :9090"]; GR["Grafana :3000"]; JA["Jaeger :16686, OTLP :4317"]
        end
    end

    HC -->|"HTTP/SSE :8000"| API
    API --- ENG
    ENG ---|"нативные вызовы GGUF"| LRT
    ENG -->|"upsert, search, pre-filter acl"| QD
    ENG ---|"файловый доступ"| SQLF
    API --- OTS
    OTS -->|"спаны OTLP :4317"| JA
    PR -->|"scrape /metrics :8000"| API
    GR -->|"datasource"| PR
```

Связи: клиент — HTTP/SSE (SSE — Server-Sent Events, потоковая передача событий); Qdrant — REST с payload-фильтром acl; Jaeger — OTLP;
Prometheus опрашивает /metrics; Grafana читает Prometheus (дашборд securekb,
6 панелей).

## 4. Компоненты (C4, уровень 3): графы LangGraph

### 4.1. Граф ингеста

Последовательность: parse → chunk → pii → extract → embed → store_vec →
store_graph. Отличие от порядка мастер-плана: extract до store_vec, иначе
payload точки Qdrant не содержал бы найденные сущности (entities).

```mermaid
flowchart LR
    ST(("START")) --> P1["parse: read_corpus, txt/md → документы"] --> P2["chunk: 500 слов, перекрытие 50"]
    P2 --> P3["pii: маскирование PII<br/>телефоны, e-mail, ИНН, паспорта"] --> P4["extract: GraphExtractor (gazetteer),<br/>node_ids по словарю сущностей"]
    P4 --> P5["embed: префикс passage:, 384d"] --> P6["store_vec: upsert в Qdrant,<br/>payload: acl, department, entities"]
    P6 --> P7["store_graph: nodes, edges (co_occurs)"] --> EN(("END"))
```

### 4.2. Граф запроса

После input_guard условное ребро ведёт в END (короткий отказ) либо в цепочку
поиска. Каждый узел обёрнут таймером стадии (securekb_stage_latency_seconds)
и спаном OTel (OTel — OpenTelemetry) с атрибутами user_acl, chunks_count.

```mermaid
flowchart TB
    ST(("START")) --> IG["input_guard<br/>InjectionGuard RU/EN: строгие и умеренные паттерны<br/>InputPIIGuard: маскировка PII вопроса"]
    IG -->|"blocked: инъекция"| BLK["Короткий отказ, аудит injection_blocked"]
    BLK --> E1(("END"))
    IG -->|"ok: вопрос замаскирован"| RD["retrieve_dense: Qdrant, префикс query:<br/>контур ACL 1: pre-filter acl ≤ user"] --> RL["retrieve_lexical: rank-bm25<br/>контур ACL 2: per-ACL индексы"]
    RL --> GE["graph_expand: сущности вопроса → соседи → чанки<br/>по allowed_doc_ids пользователя"] --> FR["fuse_rerank: RRF k=60 по трём веткам<br/>контур ACL 3: сброс чужих чанков, аудит acl_drop"]
    FR --> GN["generate: Qwen2.5-1.5B, decode-lock,<br/>стрим обёрнут OutputGuard"] --> E2(("END: ленивый answer_stream → API-слой (SSE)"))
```

## 5. Потоки данных (DFD — Data Flow Diagram, диаграмма потоков данных)

Путь документа и путь запроса: где маскируется PII, где действует каждый
контур ACL, куда пишется аудит.

```mermaid
flowchart LR
    subgraph ING["Путь документа (ingest_graph)"]
        DOC[("Корпус: txt/md, 100 документов")]
        N1["parse"]; N2["chunk 500/50"]; N3["pii: маскирование PII"]
        N4["extract: gazetteer-сущности"]; N5["embed: e5-small, passage:"]
        VS[("Qdrant securekb_chunks: chunk_id, doc_id, title,<br/>acl, department, text, entities")]
        GS[("SQLite-граф: nodes, edges")]
        DOC --> N1 --> N2 --> N3 --> N4 --> N5 --> VS
        N4 --> GS
    end

    subgraph QRY["Путь запроса (query_graph)"]
        USR(("Пользователь<br/>alice / bob / guest"))
        G1["input_guard: InjectionGuard + PII-маска"]
        G2["retrieve_dense: контур ACL 1, pre-filter MatchAny acl ≤ user"]; G3["retrieve_lexical: контур ACL 2, per-ACL BM25-индексы"]
        G4["graph_expand: соседи по allowed_doc_ids"]; G5["fuse_rerank: RRF k=60, контур ACL 3, финальный сброс"]
        G6["generate: Qwen2.5-1.5B"]; G7["output_guard: PII, чужие цитаты [N], SECRET"]
        OUT["SSE: sources → token → done"]
        USR --> G1 --> G2 --> G3 --> G4 --> G5 --> G6 --> G7 --> OUT
    end

    BMI["BM25-индексы в памяти процесса"]
    AUD[("audit_log: query, injection_blocked, pii_masked,<br/>acl_drop, output_sanitized, answer")]

    G2 -.->|"чтение с фильтром acl"| VS
    G3 -.->|"поиск по доступным индексам"| BMI
    G4 -.->|"чтение nodes и edges"| GS
    G1 -->|"события входа"| AUD
    G5 -->|"acl_drop"| AUD
    G7 -->|"output_sanitized"| AUD
```

Контролируемые точки: PII маскируется дважды (N3 — до индексации, G1 — на
входе); ACL проверяется трижды (G2, G3, G5) и ещё раз по цитатам на выходе;
срабатывания фильтров попадают в audit_log и метрику
securekb_guardrail_events_total.

## 6. Взаимодействие по запросу (sequence, пользователь bob)

```mermaid
sequenceDiagram
    autonumber
    participant C as Клиент (bob, internal)
    participant A as FastAPI /api/chat
    participant QG as QueryEngine (LangGraph)
    participant QD as Qdrant
    participant BM as BM25 (per-ACL)
    participant GS as SQLite-граф
    participant L as QwenLLM (decode-lock)
    participant OG as OutputGuard

    C->>A: POST /api/chat {question, user_id=bob, stream=true}
    A->>QG: run(question, bob, top_k)
    QG->>QG: input_guard: InjectionGuard RU/EN + PII-маскировка
    alt найдена инъекция
        QG-->>A: blocked (аудит injection_blocked)
        A-->>C: SSE: короткий отказ, done
    else запрос легитимный
        QG-->>A: замаскированный вопрос (аудит query, pii_masked)
        QG->>QD: retrieve_dense, pre-filter acl ≤ internal
        QG->>BM: retrieve_lexical, индексы public + internal
        QG->>GS: graph_expand: сущности → соседи (allowed_doc_ids)
        QG->>QG: fuse_rerank: RRF k=60, чужой чанк сброшен (аудит acl_drop)
        QG->>L: generate — decode-lock занят на весь стрим
        QG-->>A: chunks + ленивый answer_stream
        A-->>C: SSE event: sources
        loop по каждому фрагменту ответа
            L-->>OG: фрагмент
            OG->>OG: проверка PII, цитат [N], SECRET (аудит output_sanitized)
            OG-->>A: санитизированный фрагмент
            A-->>C: SSE event: token
        end
        A-->>C: SSE event: done {cited_docs, retrieval_ms, ttft_ms}
    end
    note over C,OG: Ветка alice (secret): та же цепочка, порог acl ≤ secret —<br/>допускаются и secret-чанки, для guest порог public.
```

Узкое место — узел generate: на одном llama.cpp-контексте стримы сериализованы
decode-lock, параллельные запросы выстраиваются в очередь; retrieval занимает
около 1% времени запроса (docs/04-load-report.md).

## 7. Модель данных (ER — Entity-Relationship, модель «сущность—связь»)

Qdrant хранит точки коллекции securekb_chunks (вектор + payload); SQLite —
четыре таблицы одного файла graph.db. Связи между хранилищами концептуальные:
чанк ссылается на узлы графа через entities/doc_ids, записи аудита — на
пользователей. Таблицы sessions в схеме нет — история диалогов не хранится.

```mermaid
erDiagram
    USERS ||--o{ AUDIT_LOG : "создаёт записи"
    NODES ||--o{ EDGES : "источник src"
    NODES ||--o{ EDGES : "приёмник dst"
    CHUNK }o--o{ NODES : "entities чанка и doc_ids узла"

    CHUNK {
        string chunk_id PK
        string doc_id
        string title
        string acl "public, internal или secret"
        string department
        string text "маскированный текст"
        string entities "список id узлов графа"
        float vector "384 измерения, e5-small"
    }
    NODES {
        string id PK "person, project, department"
        string type
        string name
        string doc_ids "список документов с узлом"
    }
    EDGES {
        string src PK
        string dst PK
        string relation PK "co_occurs"
    }
    USERS {
        string user_id PK
        string name
        string acl
    }
    AUDIT_LOG {
        int id PK
        string ts
        string user_id
        string event
        string details "JSON"
    }
```

Объём демонстрационных данных: 100 точек в Qdrant; 20 узлов и 276 рёбер в
SQLite; 3 пользователя; 24 находки PII замаскированы при ингесте.

## 8. Развёртывание

### 8.1. Реализованная схема (MVP)

Рабочая станция разработчика: macOS arm64, Docker Desktop; приложение и модели
— host-процесс, инфраструктура — контейнеры.

```mermaid
flowchart TB
    subgraph WS["Рабочая станция разработчика (macOS arm64, Docker Desktop)"]
        subgraph HP["Host-процессы"]
            CLI["Клиент: curl / HTTP-клиент"]
            UV["uvicorn :8000: FastAPI + LangGraph + guardrails + OTel SDK,<br/>модели: Qwen2.5-1.5B GGUF Q4_K_M ~1 ГБ, e5-small ~470 МБ"]
            GDB[("data/graph.db: граф, users, audit_log")]
            BKP[("data/backups: снапшот Qdrant + копия graph.db")]
        end
        subgraph DC["Контейнеры docker compose"]
            QDC[("qdrant v1.12.5 :6333, :6334")]; PRC["prometheus v2.54.1 :9090"]; GRC["grafana 11.3.0 :3000"]; JAC["jaeger :16686, OTLP :4317, :4318"]
        end
    end

    CLI -->|"HTTP/SSE :8000"| UV
    UV --- GDB
    UV -->|"upsert, search :6333"| QDC
    UV -->|"OTLP :4317"| JAC
    PRC -->|"scrape /metrics :8000"| UV
    GRC -->|"Prometheus datasource :9090"| PRC
    GDB -.->|"backup.sh"| BKP
    QDC -.->|"snapshot"| BKP
```

### 8.2. Целевая схема (проект, в MVP не реализована)

Перенос в контур предприятия: доступ извне только через DMZ (DMZ — Demilitarized
Zone, демилитаризованная зона), инференс — GPU-ноды
с vLLM, граф — Neo4j, секреты — Vault.

```mermaid
flowchart LR
    EXT["Внешний доступ: сотрудники, VPN / интернет"]
    subgraph DMZ["DMZ"]
        WAF["API-шлюз / WAF: TLS, аутентификация, rate-limit"]
    end
    subgraph INT["Internal (закрытый контур)"]
        APP["App-поды: FastAPI + LangGraph + guardrails"]; VLLM["vLLM GPU-ноды: continuous batching"]
        QDC2[("Qdrant-кластер")]; NEO[("Neo4j: граф знаний")]; VLT["Vault: секреты, учётные записи"]; ADB[("БД: пользователи, аудит")]
    end

    EXT -->|"доступ извне"| WAF
    WAF --> APP
    APP --> VLLM
    APP --> QDC2
    APP --> NEO
    APP --> ADB
    APP --> VLT
```

Отличия от MVP обоснованы в требованиях (раздел 5): vLLM снимает сериализацию
генерации (батч-мультиплексирование вместо очереди), Neo4j заменяет SQLite при
росте графа, DMZ и Vault закрывают периметр и управление секретами.

## 9. Control Plane и Data Plane

Интерпретация по составу (оркестрация против исполнения):

| Плоскость | Компоненты |
|---|---|
| Control Plane | LangGraph-оркестрация (ingest_graph, query_graph: порядок этапов, условные переходы), guardrails-политика (InjectionGuard, InputPIIGuard, OutputGuard), RBAC-логика (ACL, контроль в rrf_fuse), модуль аудита, административный API /admin/*, конфигурация Settings, скрипты ingest и backup.sh |
| Data Plane | Qdrant (векторы, payload), SQLite graph.db (nodes, edges, users, audit_log), LLM-рантайм llama-cpp-python, эмбеддер e5-small, BM25-индексы в памяти процесса |

Интерпретация по плоскости доступа (API-маршруты):

| Плоскость | Маршруты | Назначение |
|---|---|---|
| Control Plane | GET /admin/users, /admin/audit, /admin/stats | управление и наблюдение, вне пути пользовательского запроса |
| Data Plane | POST /api/chat (SSE/JSON) | обработка пользовательских запросов |
| Служебные | GET /health, GET /metrics | проверки живости, экспорт метрик для Prometheus |

## 10. Механизмы безопасности

Guardrails-конвейер (backend/app/security/guardrails.py): каждый фильтр решает
одну задачу и имеет явную зону ответственности.

| Guardrail | Точка применения | Назначение | Что не проверяет |
|---|---|---|---|
| InjectionGuard | вход, до поиска | бинарный вердикт по паттернам RU/EN: блок при одном строгом (ignore instructions, system prompt, jailbreak, DAN) или двух умеренных срабатываниях (act as, «представь что ты», developer mode, повторяющиеся роли) | не маскирует PII и не проверяет ответ LLM; ложные срабатывания допустимы только как исключение |
| InputPIIGuard | вход, после InjectionGuard | PII пользователя не попадает в контекст, логи и эмбеддинги; тот же словарь паттернов, что при ингесте | не блокирует запрос (PII в вопросе — не атака), не детектирует инъекции |
| OutputGuard | выход, стрим после генерации | перехватывает утечки модели: цитату недопустимого источника [N], повтор PII из контекста, копирование маркера SECRET; санитизация на лету | не решает, пускать ли запрос (вход уже проверен), не фильтрует ветки поиска |

Три контура контроля ACL (какой чанк попадает в кандидаты):

| Контур | Узел | Механизм | Покрытие |
|---|---|---|---|
| 1 | retrieve_dense | pre-filter Qdrant: MatchAny по уровням acl не выше уровня пользователя | векторная ветка не возвращает чужие чанки |
| 2 | retrieve_lexical | отдельные BM25-индексы на каждый уровень ACL, поиск только по доступным | лексическая ветка физически разделена по уровням |
| 3 | fuse_rerank | финальная проверка после RRF: чанки с уровнем выше пользовательского сбрасываются (аудит acl_drop, метрика guardrail) | страховка на случай ошибки в любой ветке — defense in depth |

Графовая ветка подчинена тому же правилу: graph_expand выбирает соседей только
по allowed_doc_ids пользователя. Все срабатывания фильтров фиксируются в
audit_log и в метрике securekb_guardrail_events_total(kind). Эффективность
подтверждена red-team-набором: 10 инъекций блокированы, 8 легитимных вопросов
пропущены.
