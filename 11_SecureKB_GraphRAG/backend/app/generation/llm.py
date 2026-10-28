"""LL-модель для генерации: протокол + Qwen (llama.cpp, Metal) + Fake (тесты).

Qwen2.5-Instruct использует chatml-шаблон (<|im_start|>role … <|im_end|>);
llama-cpp-python рендерит его при chat_format="chatml", стрим идёт по токенам.
"""

from __future__ import annotations

import threading
from collections.abc import Iterator
from typing import Protocol

from backend.app.core.config import get_settings
from backend.app.core.logging import get_logger

logger = get_logger(__name__)


class LLM(Protocol):
    """Абстракция LLM: позволяет подменять модель фейком в тестах."""

    def generate_stream(self, messages: list[dict], max_tokens: int = 512,
                        temperature: float = 0.2) -> Iterator[str]:
        """Потоковая генерация: yields текстовые фрагменты по мере готовности."""
        ...


class QwenLLM:
    """Ленивый синглтон-обёртка llama_cpp.Llama (GGUF, n_gpu_layers=-1 → Metal)."""

    _instance: "QwenLLM | None" = None
    _instance_lock = threading.Lock()

    def __init__(self, model_path: str, n_ctx: int = 4096,
                 n_gpu_layers: int = -1) -> None:
        self._model_path = model_path
        self._n_ctx = n_ctx
        self._n_gpu_layers = n_gpu_layers
        self._model = None  # лениво
        # Один llama.cpp-контекст НЕ потокобезопасен: Starlette исполняет
        # sync SSE-генераторы в threadpool, и два параллельных decode()
        # одного контекста роняют процесс (SIGSEGV, llama_decode -1).
        # Лок сериализует генерацию: один одновременный стрим на процесс
        # (осознанное ограничение one-process MVP), остальные ждут в очереди.
        self._decode_lock = threading.Lock()

    @classmethod
    def get(cls, model_path: str | None = None) -> "QwenLLM":
        """Синглтон-доступ (модель грузится с диска один раз на процесс)."""
        with cls._instance_lock:
            if cls._instance is None:
                settings = get_settings()
                cls._instance = cls(
                    model_path=model_path or settings.model_path,
                    n_ctx=settings.llm_n_ctx,
                    n_gpu_layers=settings.llm_n_gpu_layers,
                )
            return cls._instance

    @property
    def model(self):
        """Ленивая загрузка GGUF при первом обращении."""
        if self._model is None:
            import llama_cpp

            logger.info("llm: загружаю %s (n_ctx=%d, n_gpu_layers=%d)",
                        self._model_path, self._n_ctx, self._n_gpu_layers)
            self._model = llama_cpp.Llama(
                model_path=self._model_path,
                n_ctx=self._n_ctx,
                n_gpu_layers=self._n_gpu_layers,
                chat_format="chatml",  # шаблон Qwen2.5: <|im_start|>…<|im_end|>
                verbose=False,
            )
        return self._model

    def generate_stream(self, messages: list[dict], max_tokens: int = 512,
                        temperature: float = 0.2) -> Iterator[str]:
        """Стрим под инстанс-локом: весь жизненный цикл генератора — от первого
        next() до исчерпания/закрытия/исключения — держит ``_decode_lock``,
        поэтому к llama.cpp-контексту всегда обращается ровно один поток.
        Лок берётся/освобождается внутри генератора-обёртки (а не в методе!),
        т.е. только на время фактического потребления; при раннем закрытии
        (GeneratorExit) или ошибке ``with`` освобождает лок через finally.

        Сериализация — осознанный компромисс one-process MVP: параллельные
        вызовы выстраиваются в очередь (латентность растёт), но процесс не
        падает. В целевой архитектуре лок снимается переходом на vLLM:
        continuous batching декодирует запросы общими батчами, и слой
        генерации потокобезопасен без сериализации.
        """
        def _locked_stream() -> Iterator[str]:
            with self._decode_lock:
                stream = self.model.create_chat_completion(
                    messages=messages,
                    max_tokens=max_tokens,
                    temperature=temperature,
                    stream=True,
                )
                for part in stream:
                    delta = part["choices"][0]["delta"].get("content")
                    if delta:
                        yield delta

        return _locked_stream()


class FakeLLM:
    """Эхо-LLM для тестов: повторяет ВОПРОС (не весь промпт), цитирует [1]."""

    def __init__(self, answer: str | None = None) -> None:
        self.answer = answer
        self.calls: list[list[dict]] = []

    def generate_stream(self, messages: list[dict], max_tokens: int = 512,
                        temperature: float = 0.2) -> Iterator[str]:
        self.calls.append(messages)
        question = self._question(messages)
        text = self.answer or f"Эхо: {question} — подтверждено источником [1]."
        for word in text.split(" "):
            yield word + " "

    @staticmethod
    def _question(messages: list[dict]) -> str:
        """Вопрос из последнего user-сообщения (строка после «Вопрос: »)."""
        import re

        content = next(
            (m["content"] for m in reversed(messages) if m["role"] == "user"), ""
        )
        m = re.search(r"Вопрос:\s*(.+)", content)
        return m.group(1).strip() if m else content.strip()[:80]
