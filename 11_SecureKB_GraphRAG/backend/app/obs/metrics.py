"""Метрики Prometheus для SecureKB (этапы A, E1)."""

import time
from collections.abc import Iterator
from contextlib import contextmanager

from prometheus_client import Counter, Histogram

# HTTP-запросы: всего, по шаблону маршрута и коду ответа
REQUESTS_TOTAL = Counter(
    "securekb_requests_total",
    "Total number of HTTP requests",
    labelnames=["route", "status"],
)

# Задержка обработки HTTP-запросов (бакеты до 10 секунд)
REQUEST_LATENCY_SECONDS = Histogram(
    "securekb_request_latency_seconds",
    "HTTP request latency in seconds",
    labelnames=["route"],
    buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0),
)

# События guardrail-ов (этап D: PII-фильтры, инъекции и т.п.)
GUARDRAIL_EVENTS_TOTAL = Counter(
    "securekb_guardrail_events_total",
    "Total number of guardrail events",
    labelnames=["kind"],
)

# Этап E1: задержки стадий query graph (input_guard, retrieve_dense,
# retrieve_lexical, graph_expand, fuse, generate, output_guard)
STAGE_LATENCY_SECONDS = Histogram(
    "securekb_stage_latency_seconds",
    "Query graph stage latency in seconds",
    labelnames=["stage"],
    buckets=(0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0),
)

# Этап E1: time-to-first-token стриминга ответа
TTFT_SECONDS = Histogram(
    "securekb_ttft_seconds",
    "Time to first streamed token in seconds",
    buckets=(0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0),
)

# Этап E1: счётчик «токенов» — фрагментов ответа, отданных в стриминге
TOKENS_TOTAL = Counter(
    "securekb_tokens_total",
    "Total number of streamed answer fragments (tokens)",
)


@contextmanager
def stage_timer(stage: str) -> Iterator[None]:
    """Контекст-менеджер: замеряет время выполнения стадии query graph
    и наблюдает его в securekb_stage_latency_seconds{stage}."""
    start = time.perf_counter()
    try:
        yield
    finally:
        STAGE_LATENCY_SECONDS.labels(stage=stage).observe(
            time.perf_counter() - start
        )


def observe_ttft(seconds: float) -> None:
    """Зафиксировать time-to-first-token (секунды)."""
    TTFT_SECONDS.observe(seconds)


def track_tokens(n: int = 1) -> None:
    """Увеличить счётчик стриминговых фрагментов ответа на n."""
    TOKENS_TOTAL.inc(n)


def track_request(route: str, status: int, latency_seconds: float) -> None:
    """Зафиксировать завершённый HTTP-запрос (счётчик + гистограмма)."""
    REQUESTS_TOTAL.labels(route=route, status=str(status)).inc()
    REQUEST_LATENCY_SECONDS.labels(route=route).observe(latency_seconds)


def record_guardrail_event(kind: str) -> None:
    """Зафиксировать событие guardrail (этап D)."""
    GUARDRAIL_EVENTS_TOTAL.labels(kind=kind).inc()
