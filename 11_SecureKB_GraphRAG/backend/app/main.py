"""Точка входа FastAPI-приложения SecureKB (этап A: каркас + observability)."""

import time
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, Response
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

from backend.app.api import admin, chat, health
from backend.app.core.config import get_settings
from backend.app.core.logging import get_logger, setup_logging
from backend.app.obs import metrics, tracing

logger = get_logger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Инициализация/завершение: логирование, трейсинг."""
    setup_logging()
    settings = get_settings()
    if settings.otel_enabled:
        tracing.setup_tracing(settings)
        logger.info(
            "OpenTelemetry enabled (OTLP HTTP endpoint: %s)",
            settings.otel_endpoint,
        )
    else:
        logger.info("OpenTelemetry disabled by config")
    logger.info(
        "SecureKB v%s started on %s:%s (log_level=%s, qdrant=%s)",
        health.get_app_version(),
        settings.host,
        settings.port,
        settings.log_level,
        settings.qdrant_url,
    )
    try:
        yield
    finally:
        tracing.shutdown_tracing()
        logger.info("SecureKB stopped")


app = FastAPI(
    title="SecureKB",
    description="Защищённая база корпоративных знаний (RAG-платформа)",
    version=health.get_app_version(),
    lifespan=lifespan,
)

app.include_router(health.router)
app.include_router(chat.router)
app.include_router(admin.router)


@app.middleware("http")
async def metrics_middleware(request: Request, call_next):
    """Считает запросы и задержки по шаблону маршрута (route template)."""
    start = time.perf_counter()
    response = await call_next(request)
    route = request.scope.get("route")
    route_template = getattr(route, "path", None) or "unmatched"
    metrics.track_request(
        route=route_template,
        status=response.status_code,
        latency_seconds=time.perf_counter() - start,
    )
    return response


@app.get("/metrics", include_in_schema=False)
async def prometheus_metrics() -> Response:
    """Экспорт метрик в формате Prometheus."""
    return Response(content=generate_latest(), media_type=CONTENT_TYPE_LATEST)
