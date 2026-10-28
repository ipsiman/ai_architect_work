"""Настройки приложения SecureKB (pydantic-settings, префикс SECUREKB_)."""

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Глобальные настройки SecureKB; читаются из env с префиксом SECUREKB_."""

    model_config = SettingsConfigDict(
        env_prefix="SECUREKB_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    host: str = "0.0.0.0"
    port: int = 8000
    log_level: str = "INFO"

    # Qdrant (векторная БД)
    qdrant_url: str = "http://localhost:6333"

    # Ingestion-конвейер (этап B)
    corpus_dir: str = "data/corpus"
    graph_db_path: str = "data/graph.db"
    entities_path: str = "data/entities.json"
    embedding_model: str = "intfloat/multilingual-e5-small"
    qdrant_collection: str = "securekb_chunks"
    chunk_size_tokens: int = 500
    chunk_overlap_tokens: int = 50

    # OpenTelemetry
    otel_enabled: bool = True
    otel_endpoint: str = "http://localhost:4318"  # OTLP over HTTP
    otel_grpc_endpoint: str = "http://localhost:4317"  # OTLP over gRPC (заготовка)

    # Retrieval + LLM (этап C)
    model_path: str = "models/qwen2.5-1.5b-instruct-q4_k_m.gguf"
    llm_n_ctx: int = 4096
    llm_n_gpu_layers: int = -1  # Metal: все слои на GPU
    llm_max_tokens: int = 512
    llm_temperature: float = 0.2
    retrieval_top_k: int = 5

    # Guardrails + аудит (этап D)
    guard_injection_enabled: bool = True   # InjectionGuard на входе
    guard_input_pii_enabled: bool = True   # PII-маскировка вопроса
    guard_output_enabled: bool = True      # санитизация ответа LLM
    audit_enabled: bool = True             # журнал audit_log в graph.db


@lru_cache
def get_settings() -> Settings:
    """Кэшированный доступ к настройкам (lru_cache — один инстанс на процесс)."""
    return Settings()
