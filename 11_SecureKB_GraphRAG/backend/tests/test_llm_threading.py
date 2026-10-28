"""Потокобезопасность генерации: сериализация стримов инстанс-локом (фикс E2).

Контекст: Starlette исполняет sync SSE-генераторы в threadpool; два потока,
параллельно дергающие один llama.cpp-контекст, роняют процесс (SIGSEGV,
llama_decode -1). QwenLLM.generate_stream возвращает генератор-обёртку,
держащий ``_decode_lock`` весь жизненный цикл потребления: один одновременный
стрим на процесс, остальные ждут в очереди.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Iterator
from pathlib import Path

import pytest

from backend.app.generation.llm import QwenLLM

MODELS_DIR = Path(__file__).resolve().parents[2] / "models"
GGUF = next(MODELS_DIR.glob("*.gguf"), None)

requires_model = pytest.mark.skipif(
    GGUF is None, reason="models/*.gguf не найден — реальный QwenLLM недоступен")


# -- измерительный лок ----------------------------------------------------------------


class CountingLock:
    """Инструментированный лок-контекст: max одновременных владельцев.

    Подставляется вместо QwenLLM._decode_lock: если стримы пересекаются,
    max_concurrent станет > 1.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._stat = threading.Lock()
        self.current = 0
        self.max_concurrent = 0

    def __enter__(self) -> "CountingLock":
        self._lock.acquire()
        with self._stat:
            self.current += 1
            self.max_concurrent = max(self.max_concurrent, self.current)
        return self

    def __exit__(self, *exc: object) -> None:
        with self._stat:
            self.current -= 1
        self._lock.release()


class FakeLockedLLM:
    """Медленная эхо-LLM с той же схемой лока, что QwenLLM (без модели)."""

    def __init__(self, tokens: int = 10, delay: float = 0.02) -> None:
        self._decode_lock = threading.Lock()
        self._tokens = tokens
        self._delay = delay

    def generate_stream(self, messages: list[dict], max_tokens: int = 512,
                        temperature: float = 0.2) -> Iterator[str]:
        def _locked_stream() -> Iterator[str]:
            with self._decode_lock:
                for i in range(self._tokens):
                    time.sleep(self._delay)
                    yield f"t{i} "

        return _locked_stream()


def run_two_streams(llm, max_tokens: int = 10) -> tuple[CountingLock, list[list[str]]]:
    """Два потока параллельно стримят; вернуть (лок-счётчик, фрагменты по потокам)."""
    lock = CountingLock()
    llm._decode_lock = lock
    results: list[list[str]] = [[], []]
    errors: list[BaseException] = []

    def worker(i: int) -> None:
        try:
            for fragment in llm.generate_stream(
                    [{"role": "user", "content": f"Вопрос: тест {i}"}],
                    max_tokens=max_tokens):
                results[i].append(fragment)
        except BaseException as exc:  # noqa: BLE001 — фиксируем всё, assert ниже
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=180)
    assert not errors, f"потоки упали с исключениями: {errors}"
    assert not any(t.is_alive() for t in threads), "поток завис (join timeout)"
    return lock, results


# -- (a) схема лока без модели ----------------------------------------------------------


def test_fake_locked_llm_streams_do_not_overlap() -> None:
    """Два медленных стрима не пересекаются: одновременность ≤ 1, оба полны."""
    lock, results = run_two_streams(FakeLockedLLM(tokens=10, delay=0.02))

    assert lock.max_concurrent == 1
    assert lock.current == 0  # оба стрима завершены — лок свободен
    assert all(len(parts) == 10 for parts in results)


def test_qwen_llm_lock_is_lazy_until_consumption() -> None:
    """Лок берётся только при потреблении генератора, а не при вызове метода."""
    llm = QwenLLM(model_path="unused.gguf")
    llm._decode_lock = CountingLock()
    llm._model = _StubModel(parts=3)

    gen = llm.generate_stream([{"role": "user", "content": "Вопрос: лень?"}])
    assert llm._decode_lock.current == 0  # создан, но не потребляется

    fragments = list(gen)
    assert fragments == ["a", "b", "c"]
    assert llm._decode_lock.max_concurrent == 1
    assert llm._decode_lock.current == 0  # освобождён после исчерпания


def test_qwen_llm_lock_released_on_exception() -> None:
    """Исключение внутри стрима освобождает лок (finally генератора)."""
    llm = QwenLLM(model_path="unused.gguf")
    llm._model = _ExplodingModel()

    with pytest.raises(RuntimeError, match="boom"):
        list(llm.generate_stream([{"role": "user", "content": "Вопрос: упади"}]))

    assert not llm._decode_lock.locked()
    assert llm._decode_lock.acquire(blocking=False), "лок утёк после исключения"
    llm._decode_lock.release()


def test_qwen_llm_lock_released_on_early_close() -> None:
    """Раннее закрытие генератора (клиент ушёл, GeneratorExit) освобождает лок."""
    llm = QwenLLM(model_path="unused.gguf")
    llm._model = _StubModel(parts=100)

    gen = llm.generate_stream([{"role": "user", "content": "Вопрос: стоп"}])
    next(gen)  # взяли лок, получили первый фрагмент
    assert llm._decode_lock.locked()

    gen.close()  # Starlette так закрывает SSE-генератор при disconnect
    assert not llm._decode_lock.locked()


# -- (b) реальный QwenLLM (если есть GGUF) ----------------------------------------------


@requires_model
def test_real_qwen_llm_parallel_streams_serialized() -> None:
    """Реальная модель: 2 потока × 10 токенов — без исключений, одновременность 1."""
    llm = QwenLLM(model_path=str(GGUF))
    lock, results = run_two_streams(llm, max_tokens=10)

    assert lock.max_concurrent == 1, "стримы пересеклись — лок не сериализует"
    assert lock.current == 0
    # 10 токенов лимита → хотя бы один содержательный фрагмент на поток
    assert all(len(parts) >= 1 for parts in results)


# -- стабы модели ------------------------------------------------------------------------


class _StubModel:
    """create_chat_completion → parts фрагментов (быстро, без llama.cpp)."""

    def __init__(self, parts: int) -> None:
        self._parts = parts

    def create_chat_completion(self, **_: object) -> Iterator[dict]:
        for ch in "abcdefghij"[: self._parts]:
            yield {"choices": [{"delta": {"content": ch}}]}


class _ExplodingModel:
    """Падает на третьем фрагменте — проверка освобождения лока при исключении."""

    def create_chat_completion(self, **_: object) -> Iterator[dict]:
        yield {"choices": [{"delta": {"content": "a"}}]}
        yield {"choices": [{"delta": {"content": "b"}}]}
        raise RuntimeError("boom mid-stream")
