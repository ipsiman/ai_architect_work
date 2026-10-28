"""Health-check эндпоинт."""

from fastapi import APIRouter

from backend.app.core.config import get_settings
from backend.app.obs.tracing import get_tracer

router = APIRouter(tags=["health"])

_APP_VERSION = "0.1.0"


def get_app_version() -> str:
    """Версия приложения (из метаданных пакета, с фолбэком)."""
    try:
        from importlib.metadata import PackageNotFoundError, version

        return version("securekb")
    except PackageNotFoundError:
        return _APP_VERSION


@router.get("/health")
async def health() -> dict:
    """Проверка живости сервиса."""
    with get_tracer().start_as_current_span("health_check"):
        return {"status": "ok", "version": get_app_version()}
