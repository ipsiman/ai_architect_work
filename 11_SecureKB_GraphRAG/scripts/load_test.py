"""Нагрузочный тест /api/chat (этап E2): asyncio + httpx, без locust.

Уровни конкурентности выполняются ПОСЛЕДОВАТЕЛЬНО. Каждый воркер итеративно
шлёт POST /api/chat со случайным вопросом из data/golden_qa.json до конца
--duration; user_id ротируется alice/bob/guest. На старте — 1 warmup-запрос
(загрузка модели в память), в статистику не входит.

SSE-клиент ловит первый event: token -> TTFT; event: done -> total_latency
и число token-событий. При stream=false меряется только total_latency.

Вывод: markdown-таблица, одна строка на уровень.

Запуск:
    uv run python scripts/load_test.py --duration 45 --concurrency 1,2,4
    uv run python scripts/load_test.py --duration 45 --concurrency 1,2 --stream false
"""

from __future__ import annotations

import argparse
import asyncio
import json
import random
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

import httpx

GOLDEN_QA_PATH = Path(__file__).resolve().parents[1] / "data" / "golden_qa.json"
USERS = ("alice", "bob", "guest")
SEED = 20261026  # фиксированный seed: воспроизводимость выбора вопросов


# -- статистика ----------------------------------------------------------------------


def percentile(values: list[float], p: float) -> float:
    """Перцентиль с линейной интерполяцией (как numpy.percentile)."""
    if not values:
        return float("nan")
    s = sorted(values)
    if len(s) == 1:
        return s[0]
    k = (len(s) - 1) * p / 100.0
    lo, hi = int(k), min(int(k) + 1, len(s) - 1)
    frac = k - lo
    return s[lo] * (1 - frac) + s[hi] * frac


@dataclass
class LevelStats:
    """Собранная статистика одного уровня конкурентности."""

    level: int
    requests: int = 0
    errors: int = 0
    total_ms: list[float] = field(default_factory=list)
    ttft_ms: list[float] = field(default_factory=list)
    tokens: list[int] = field(default_factory=list)
    elapsed_s: float = 0.0
    consecutive_errors: int = 0  # аварийный стоп уровня, если сервер умер
    aborted: bool = False
    _printed_errors: int = 0

    def note_error(self, msg: str) -> None:
        """Зафиксировать ошибку; подробно печатать только первые 5."""
        self.errors += 1
        self.consecutive_errors += 1
        if self._printed_errors < 5:
            self._printed_errors += 1
            print(f"    [ошибка] {msg}", file=sys.stderr)

    def note_success(self) -> None:
        self.consecutive_errors = 0

    def row(self, stream: bool) -> str:
        rps = self.requests / self.elapsed_s if self.elapsed_s else 0.0
        ttft = (
            f"{percentile(self.ttft_ms, 50):.0f} / {percentile(self.ttft_ms, 95):.0f}"
            if self.ttft_ms else "-"
        )
        avg_tokens = (
            f"{sum(self.tokens) / len(self.tokens):.0f}" if self.tokens else "-"
        )
        if not self.total_ms:  # ни одного успешного запроса (сервер упал)
            lat = "- | - | -"
        else:
            lat = (f"{percentile(self.total_ms, 50):.0f} "
                   f"| {percentile(self.total_ms, 95):.0f} "
                   f"| {percentile(self.total_ms, 99):.0f}")
        return (
            f"| {'stream' if stream else 'json'} | {self.level} "
            f"| {self.requests} | {rps:.3f} | {self.errors} "
            f"| {lat} | {ttft} | {avg_tokens} |"
        )


# -- один запрос ----------------------------------------------------------------------


async def chat_once(
    client: httpx.AsyncClient,
    url: str,
    question: str,
    user_id: str,
    stream: bool,
) -> tuple[float, float | None, int | None, str | None]:
    """Один запрос. Возвращает (total_ms, ttft_ms|None, tokens|None, ошибка|None)."""
    body = {"question": question, "user_id": user_id, "stream": stream}
    start = time.perf_counter()
    ttft_ms: float | None = None
    tokens = 0
    try:
        if not stream:
            resp = await client.post(url, json=body)
            if resp.status_code != 200:
                return 0.0, None, None, f"HTTP {resp.status_code}"
            return (time.perf_counter() - start) * 1000, None, None, None

        async with client.stream("POST", url, json=body) as resp:
            if resp.status_code != 200:
                await resp.aread()
                return 0.0, None, None, f"HTTP {resp.status_code}"
            event: str | None = None
            data_lines: list[str] = []
            async for line in resp.aiter_lines():
                if line == "":
                    if event == "token" and ttft_ms is None:
                        ttft_ms = (time.perf_counter() - start) * 1000
                    if event == "token":
                        tokens += 1
                    elif event == "done":
                        return (time.perf_counter() - start) * 1000, ttft_ms, tokens, None
                    event, data_lines = None, []
                elif line.startswith("event:"):
                    event = line[len("event:"):].strip()
                elif line.startswith("data:"):
                    data_lines.append(line[len("data:"):].strip("\r"))
            # поток оборвался без done
            return (time.perf_counter() - start) * 1000, ttft_ms, tokens, "no done event"
    except Exception as exc:  # таймауты, обрывы соединения
        return 0.0, None, None, f"{type(exc).__name__}: {exc}"[:120]


# -- уровень конкурентности ------------------------------------------------------------


async def run_level(
    url: str,
    level: int,
    duration: float,
    stream: bool,
    timeout: float,
    questions: list[str],
) -> LevelStats:
    stats = LevelStats(level=level)
    rng = random.Random(SEED + level)  # свой seed на уровень — воспроизводимо
    to_client = httpx.Timeout(timeout, connect=min(timeout, 30.0))

    async with httpx.AsyncClient(timeout=to_client, limits=httpx.Limits(
            max_connections=level + 2)) as client:
        sem = asyncio.Semaphore(level)
        user_cycle = iter(())  # ротация user_id

        def next_user() -> str:
            nonlocal user_cycle
            try:
                return next(user_cycle)
            except StopIteration:
                user_cycle = iter(USERS)
                return next(user_cycle)

        async def worker() -> None:
            async with sem:
                while time.perf_counter() < deadline:
                    if stats.consecutive_errors >= 20:
                        stats.aborted = True
                        return  # сервер недоступен — уровень прерываем
                    q = questions[rng.randrange(len(questions))]
                    total, ttft, toks, err = await chat_once(
                        client, url, q, next_user(), stream)
                    stats.requests += 1
                    if err is not None:
                        stats.note_error(err)
                    else:
                        stats.note_success()
                        stats.total_ms.append(total)
                        if ttft is not None:
                            stats.ttft_ms.append(ttft)
                        if toks is not None:
                            stats.tokens.append(toks)

        t0 = time.perf_counter()
        deadline = t0 + duration
        await asyncio.gather(*(worker() for _ in range(level)))
        stats.elapsed_s = time.perf_counter() - t0
    return stats


# -- main -----------------------------------------------------------------------------


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Нагрузочный тест /api/chat (E2)")
    p.add_argument("--url", default="http://localhost:8000",
                   help="базовый URL (default: http://localhost:8000)")
    p.add_argument("--duration", type=float, default=45,
                   help="длительность уровня, сек (default: 45)")
    p.add_argument("--concurrency", default="1,2,4",
                   help='уровни конкурентности через запятую (default: "1,2,4")')
    p.add_argument("--stream", default="true",
                   help="true|false — режим SSE/JSON (default: true)")
    p.add_argument("--timeout", type=float, default=120,
                   help="таймаут запроса, сек (default: 120)")
    return p.parse_args()


async def main() -> int:
    args = parse_args()
    stream = {"true": True, "false": False}[args.stream.lower()]
    levels = [int(x) for x in args.concurrency.split(",") if x.strip()]
    url = args.url.rstrip("/") + "/api/chat"
    questions = [item["question"] for item in
                 json.loads(GOLDEN_QA_PATH.read_text(encoding="utf-8"))]

    # warmup: 1 запрос вне статистики — первый вызов грузит модель в память
    print(f"warmup: 1 запрос (stream={str(stream).lower()}), в статистику не входит…",
          file=sys.stderr)
    async with httpx.AsyncClient(timeout=httpx.Timeout(args.timeout)) as client:
        t0 = time.perf_counter()
        total, _, _, err = await chat_once(client, url, questions[0], "alice", stream)
        print(f"warmup: {total / 1000:.1f} с"
              + (f", ошибка: {err}" if err else ""), file=sys.stderr)
        if err:
            print("warmup не прошёл — сервер недоступен?", file=sys.stderr)
            return 2

    all_stats: list[LevelStats] = []
    for level in levels:
        print(f"уровень concurrency={level}, duration={args.duration:g} с …",
              file=sys.stderr)
        st = await run_level(url, level, args.duration, stream,
                             args.timeout, questions)
        all_stats.append(st)
        print(f"  готово: {st.requests} запросов, ошибок {st.errors}"
              + (" — УРОВЕНЬ ПРЕРВАН (сервер недоступен)" if st.aborted else ""),
              file=sys.stderr)

    mode = "SSE-стрим" if stream else "JSON (stream=false)"
    print(f"\n### {mode}: /api/chat, duration={args.duration:g} с/уровень\n")
    print("| Режим | Конкур. | Запросов | RPS | Ошибки "
          "| p50 total, мс | p95 total, мс | p99 total, мс "
          "| p50/p95 TTFT, мс | Ср. токенов |")
    print("|---|---|---|---|---|---|---|---|---|---|")
    for st in all_stats:
        print(st.row(stream))
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
