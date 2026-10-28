"""Единая настройка логирования SecureKB."""

import logging
import sys

from backend.app.core.config import get_settings

_LOG_FORMAT = "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s"


def setup_logging() -> None:
    """Инициализирует корневой логгер (формат: дата / уровень / имя / сообщение)."""
    settings = get_settings()
    logging.basicConfig(
        level=settings.log_level.upper(),
        format=_LOG_FORMAT,
        stream=sys.stdout,
        force=True,
    )
    # Шумные библиотеки приглушаем до WARNING
    for noisy in ("httpx", "httpcore", "uvicorn.access"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def get_logger(name: str) -> logging.Logger:
    """Возвращает логгер с заданным именем."""
    return logging.getLogger(name)
