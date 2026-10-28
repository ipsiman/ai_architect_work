"""Эмбеддеры SecureKB: протокол + E5 (реальный) + Fake (для тестов).

Модель intfloat/multilingual-e5-small требует префиксы:
- "query: " — при поисковом запросе;
- "passage: " — при индексации документов.
"""

from __future__ import annotations

import hashlib
import threading
from collections.abc import Sequence
from typing import Protocol

BATCH_SIZE = 32
E5_DIM = 384


class Embedder(Protocol):
    """Абстракция эмбеддера: позволяет подменять модель фейком в тестах."""

    def embed_passages(self, texts: Sequence[str]) -> list[list[float]]:
        """Эмбеддинги для индексации (с префиксом passage, если модель e5)."""
        ...

    def embed_query(self, query: str) -> list[float]:
        """Эмбеддинг поискового запроса (с префиксом query, если модель e5)."""
        ...


def _auto_device() -> str:
    """Выбирает устройство: MPS на Apple Silicon, иначе CPU."""
    try:
        import torch

        if torch.backends.mps.is_available():
            return "mps"
    except Exception:  # pragma: no cover - платформозависимо
        pass
    return "cpu"


class E5Embedder:
    """Ленивый синглтон-обёртка SentenceTransformer для e5-моделей."""

    _instance: "E5Embedder | None" = None
    _instance_lock = threading.Lock()

    def __init__(self, model_name: str = "intfloat/multilingual-e5-small",
                 device: str | None = None) -> None:
        self._model_name = model_name
        self._device = device
        self._model = None  # лениво

    @classmethod
    def get(cls, model_name: str = "intfloat/multilingual-e5-small") -> "E5Embedder":
        """Синглтон-доступ (модель грузится один раз на процесс)."""
        with cls._instance_lock:
            if cls._instance is None:
                cls._instance = cls(model_name)
            return cls._instance

    @property
    def model(self):
        """Ленивая загрузка SentenceTransformer при первом обращении."""
        if self._model is None:
            from sentence_transformers import SentenceTransformer

            device = self._device or _auto_device()
            self._model = SentenceTransformer(self._model_name, device=device)
        return self._model

    def _embed(self, prefixed: list[str]) -> list[list[float]]:
        vectors = self.model.encode(
            prefixed,
            batch_size=BATCH_SIZE,
            normalize_embeddings=True,
            show_progress_bar=False,
        )
        return [v.tolist() for v in vectors]

    def embed_passages(self, texts: Sequence[str]) -> list[list[float]]:
        return self._embed([f"passage: {t}" for t in texts])

    def embed_query(self, query: str) -> list[float]:
        return self._embed([f"query: {query}"])[0]


class FakeEmbedder:
    """Детерминированный hash-based эмбеддер (dim=384) для тестов без сети."""

    def __init__(self, dim: int = E5_DIM) -> None:
        self.dim = dim

    def _vector(self, text: str) -> list[float]:
        digest = hashlib.sha256(text.encode("utf-8")).digest()
        values = []
        counter = 0
        while len(values) < self.dim:
            block = hashlib.sha256(digest + counter.to_bytes(4, "big")).digest()
            values.extend(b / 255.0 for b in block)
            counter += 1
        vec = values[: self.dim]
        norm = sum(v * v for v in vec) ** 0.5 or 1.0
        return [v / norm for v in vec]

    def embed_passages(self, texts: Sequence[str]) -> list[list[float]]:
        return [self._vector(t) for t in texts]

    def embed_query(self, query: str) -> list[float]:
        return self._vector(query)
