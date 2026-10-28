"""Настройка OpenTelemetry-трейсинга (OTLP over HTTP → Jaeger)."""

from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor

from backend.app.core.config import Settings

SERVICE_NAME = "securekb"

_tracer_provider: TracerProvider | None = None


def setup_tracing(settings: Settings) -> None:
    """Инициализирует TracerProvider с OTLP/HTTP-экспортёром (idempotent)."""
    global _tracer_provider
    if _tracer_provider is not None:
        return
    resource = Resource.create({"service.name": SERVICE_NAME})
    provider = TracerProvider(resource=resource)
    exporter = OTLPSpanExporter(endpoint=f"{settings.otel_endpoint.rstrip('/')}/v1/traces")
    provider.add_span_processor(BatchSpanProcessor(exporter))
    trace.set_tracer_provider(provider)
    _tracer_provider = provider


def get_tracer() -> trace.Tracer:
    """Tracer для ручных span'ов (no-op, если трейсинг не настроен)."""
    return trace.get_tracer(SERVICE_NAME)


def shutdown_tracing() -> None:
    """Аккуратно завершает работу: сбрасывает буфер span'ов в экспортёр."""
    global _tracer_provider
    if _tracer_provider is not None:
        _tracer_provider.shutdown()
        _tracer_provider = None
