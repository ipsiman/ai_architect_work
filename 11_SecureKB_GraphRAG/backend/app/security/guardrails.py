r"""Guardrails-конвейер SecureKB (этап D): вход → генерация → выход.

Декомпозиция guardrail-ов (почему отдельные классы, что каждый НЕ проверяет):

┌────────────────┬──────────────────────────────┬─────────────────────────────┐
│ Guardrail      │ Почему отдельный             │ Что НЕ проверяет            │
├────────────────┼──────────────────────────────┼─────────────────────────────┤
│ InjectionGuard │ Работает ДО ретрива на       │ НЕ маскирует PII (это       │
│ (вход)         │ сыром тексте; решает «пус-   │ InputPIIGuard) и НЕ трогает │
│                │ тить/отказать» — бинарный    │ ответ LLM (это OutputGuard).│
│                │ вердикт по порогу строгих/   │ Ложные срабатывания допустимы│
│                │ умеренных паттернов RU+EN.   │ только как исключение.      │
├────────────────┼──────────────────────────────┼─────────────────────────────┤
│ InputPIIGuard  │ PII пользователя не должно   │ НЕ блокирует запрос (PII в  │
│ (вход)         │ попасть в контекст/логи/     │ вопросе — не атака), НЕ     │
│                │ эмбеддинги; переиспользует   │ детектирует инъекции.       │
│                │ детектор ingestion/pii.py —  │                             │
│                │ один и тот же словарь паттер-│                             │
│                │ нов для ingestion и query.   │                             │
├────────────────┼──────────────────────────────┼─────────────────────────────┤
│ OutputGuard    │ LLM может «протечь»: процитировать│ НЕ решает, пускать    │
│ (выход)        │ недопустимый [N], повторить   │ запрос (вход уже прошёл),  │
│                │ PII из контекста, скопировать │ НЕ фильтрует retrieval-    │
│                │ маркер SECRET из документа.  │ ветки (ACL — в fuse/dense). │
│                │ Санитизирует ПОСЛЕ генерации. │                             │
└────────────────┴──────────────────────────────┴─────────────────────────────┘

ACL-контроль (какой чанк вообще попал в контекст) — отдельный слой:
pre-filter в dense/lexical/graph + defense-in-depth в rrf_fuse. OutputGuard
проверяет только цитаты против УЖЕ отфильтрованного списка чанков.

Каждый guard: apply(...) + счётчик securekb_guardrail_events_total(kind=...)
при срабатывании (см. obs/metrics.record_guardrail_event).
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Iterator, Sequence
from dataclasses import dataclass, field

from backend.app.ingestion.pii import PIIDetector
from backend.app.obs.metrics import record_guardrail_event
from backend.app.retrieval.fuse import RetrievedChunk

# Метрики- kinds (SecureKB_guardrail_events_total.labels(kind=...))
KIND_INJECTION_BLOCKED = "injection_blocked"
KIND_PII_MASKED_INPUT = "pii_masked_input"
KIND_OUTPUT_PII = "output_pii"
KIND_OUTPUT_CITATION = "output_citation"
KIND_OUTPUT_SECRET = "output_secret"


@dataclass(slots=True)
class GuardVerdict:
    """Результат входного guardrail-а."""

    blocked: bool = False
    reasons: list[str] = field(default_factory=list)
    masked_text: str = ""
    meta: dict = field(default_factory=dict)


# -- InjectionGuard: паттерны -------------------------------------------------

STRICT_PATTERNS: dict[str, str] = {
    # EN: прямая попытка отменить системные инструкции
    "ignore_instructions_en": r"ignore\s+(?:all\s+)?(?:previous|prior)\s+instructions",
    # RU: «проигнорируй предыдущие инструкции/указания»
    "ignore_instructions_ru": (
        r"(?:про)?игнорир\w*\s+(?:(?:все|всех)\s+)?предыдущ\w+"
        r"\s+(?:инструкц\w*|указан\w*|сообщен\w*|запрос\w*)"
    ),
    # упоминание системного промпта (EN/RU)
    "system_prompt": r"system\s*prompt|системн\w+\s+промпт",
    # «раскрой (свой) (системный) промпт»
    "reveal_prompt_ru": r"раскры\w+\s+(?:сво\w+\s+)?(?:системн\w+\s+)?промпт",
    "jailbreak": r"jailbreak|джейлбрейк",
    "dan_mode": r"(?<!\w)DAN(?!\w)",
}

MODERATE_PATTERNS: dict[str, str] = {
    "act_as": r"act\s+as\b",
    "pretend_ru": r"представь,?\s+что\s+ты|представь\s+себя\s+",
    "you_are_now_ru": r"ты\s+теперь\s+(?:другой|новый|иной)",
    "developer_mode": r"developer\s+mode|режим\s+разработчика",
}

# повторяющиеся роли («system:»/«user:»/«assistant:») внутри текста пользователя
_ROLE_MARKER_RE = re.compile(r"(?i)\b(?:system|assistant|user)\s*:")

# Гомоглифы: кириллические символы, визуально неотличимые от латинских,
# применяются для обхода regex-фильтров («іgnore prevіous instructions»,
# где і — U+0456). Нормализация НЕ меняет исходный текст пользователя —
# строится отдельная копия только для матчинга паттернов.
_HOMOGLYPH_MAP = str.maketrans({
    "а": "a", "с": "c", "е": "e", "о": "o", "р": "p", "х": "x", "у": "y",
    "і": "i", "ј": "j", "ѕ": "s", "ԁ": "d", "һ": "h",
    "А": "a", "С": "c", "Е": "e", "О": "o", "Р": "p", "Х": "x", "У": "y",
    "І": "i", "Ј": "j", "Ѕ": "s",
})


def _normalize_homoglyphs(text: str) -> str:
    """Кириллические двойники латиницы → латиница (для матчинга)."""
    return text.translate(_HOMOGLYPH_MAP)

# порог: блок при ≥1 строгом срабатывании ИЛИ ≥2 умеренных
STRICT_THRESHOLD = 1
MODERATE_THRESHOLD = 2


@dataclass(slots=True)
class _CompiledPatterns:
    id: str
    severity: str  # "strict" | "moderate"
    regex: "re.Pattern[str]"


def _compile(spec: dict[str, str], severity: str) -> list[_CompiledPatterns]:
    return [
        _CompiledPatterns(pid, severity, re.compile(rx, flags=re.IGNORECASE))
        for pid, rx in spec.items()
    ]


class InjectionGuard:
    """Детектор prompt-injection на входе (RU+EN).

    Пороговая логика против ложных срабатываний: блок только при
    ≥1 строгом паттерне (system prompt / ignore instructions / jailbreak /
    DAN) или ≥2 умеренных ("act as", "представь что ты", developer mode,
    повторяющиеся роли). Одиночное "act as" в легитимном контексте
    («act as in the contract» — англ. «действовать как в договоре»)
    НЕ блокируется.
    """

    def __init__(self, patterns: list[_CompiledPatterns] | None = None,
                 enabled: bool = True,
                 strict_threshold: int = STRICT_THRESHOLD,
                 moderate_threshold: int = MODERATE_THRESHOLD) -> None:
        self.enabled = enabled
        self.strict_threshold = strict_threshold
        self.moderate_threshold = moderate_threshold
        self._patterns = patterns if patterns is not None else (
            _compile(STRICT_PATTERNS, "strict")
            + _compile(MODERATE_PATTERNS, "moderate")
            + [_CompiledPatterns("role_markers", "moderate", _ROLE_MARKER_RE)]
        )

    def check(self, text: str) -> GuardVerdict:
        """Анализирует текст; возвращает вердикт (masked_text = исходный текст).

        Паттерны проверяются по двум вариантам текста: исходному и с
        нормализацией гомоглифов (кириллица→латиница). Для каждого паттерна
        берётся максимум срабатываний, поэтому нормализация только РАСШИРЯЕТ
        детекцию (обфускация «іgnore prevіous») и не может её сузить.
        """
        if not self.enabled:
            return GuardVerdict(blocked=False, masked_text=text)

        candidates = (text, _normalize_homoglyphs(text))
        hits: list[tuple[str, str, str]] = []  # (severity, pattern_id, matched)
        for pat in self._patterns:
            count = 0
            example = ""
            for candidate in candidates:
                if pat.id == "role_markers":
                    # «повторяющиеся роли»: ≥2 маркеров = 2 умеренных срабатывания
                    n = len(pat.regex.findall(candidate))
                else:
                    matches = list(pat.regex.finditer(candidate))
                    n = len(matches)
                    if matches and not example:
                        example = matches[0].group(0)
                count = max(count, n)
            for _ in range(count):
                hits.append(
                    ("moderate" if pat.id == "role_markers" else pat.severity,
                     pat.id, example or f"×{count}")
                )

        strict = [h for h in hits if h[0] == "strict"]
        moderate = [h for h in hits if h[0] == "moderate"]
        blocked = len(strict) >= self.strict_threshold or \
            len(moderate) >= self.moderate_threshold

        reasons = [f"{sev}:{pid}({txt!r})" for sev, pid, txt in strict + moderate]
        return GuardVerdict(
            blocked=blocked,
            reasons=reasons,
            masked_text=text,
            meta={"strict_hits": len(strict), "moderate_hits": len(moderate)},
        )

    def apply(self, text: str) -> GuardVerdict:
        """check + метрика при блокировке."""
        verdict = self.check(text)
        if verdict.blocked:
            record_guardrail_event(KIND_INJECTION_BLOCKED)
        return verdict


class InputPIIGuard:
    """Маскирует PII в вопросе пользователя (НЕ блокирует запрос).

    PII не должен попасть в: контекст LLM, вектор Qdrant (эмбеддинг запроса),
    логи и аудит. Использует тот же PIIDetector, что и ingestion-конвейер.
    """

    def __init__(self, detector: PIIDetector | None = None,
                 enabled: bool = True) -> None:
        self.enabled = enabled
        self._detector = detector or PIIDetector()

    def check(self, text: str) -> GuardVerdict:
        masked, findings = self._detector.mask(text)
        return GuardVerdict(
            blocked=False,  # PII в вопросе — не атака, маскируем и пускаем
            masked_text=masked,
            meta={"findings": findings},
        )

    def apply(self, text: str) -> GuardVerdict:
        """check + метрика при найденном PII."""
        verdict = self.check(text) if self.enabled else \
            GuardVerdict(masked_text=text)
        if verdict.meta.get("findings"):
            record_guardrail_event(KIND_PII_MASKED_INPUT)
        return verdict


# -- OutputGuard ---------------------------------------------------------------

_CITATION_RE = re.compile(r"\[(\d+)\]")
_SECRET_MARKER_RE = re.compile(r"\{SECRET\}|SECRET:")


class OutputGuard:
    """Санитизация ответа LLM ПОСЛЕ генерации.

    Проверяет три класса утечек:
    (a) PII в ответе (телефоны/emails/карты...) → плейсхолдеры [PHONE]/[EMAIL];
    (b) цитаты [N] вне списка допустимых чанков → ссылка вырезается;
    (c) маркеры "SECRET:" / "{SECRET}" → вырезаются.
    """

    # сколько символов «удерживать» при стриминге, чтобы паттерн, разрезанный
    # границей токенов, не ушёл клиенту до завершения (все паттерны ≤ 16 симв.)
    HOLD_BACK = 16

    def __init__(self, detector: PIIDetector | None = None,
                 enabled: bool = True) -> None:
        self.enabled = enabled
        self._detector = detector or PIIDetector()

    def check_answer(self, answer: str,
                     allowed_chunks: Sequence[RetrievedChunk],
                     ) -> tuple[str, list[str]]:
        """Возвращает (sanitized_answer, violations)."""
        if not self.enabled:
            return answer, []
        sanitized = answer
        violations: list[str] = []

        # (b) цитаты вне допустимых чанков
        max_n = len(allowed_chunks)
        for m in _CITATION_RE.finditer(sanitized):
            n = int(m.group(1))
            if n < 1 or n > max_n:
                violations.append(f"citation:{n}")
        if violations:
            def _drop(m: re.Match) -> str:
                return "" if not (1 <= int(m.group(1)) <= max_n) else m.group(0)
            sanitized = _CITATION_RE.sub(_drop, sanitized)

        # (c) маркеры секретности
        if _SECRET_MARKER_RE.search(sanitized):
            sanitized = _SECRET_MARKER_RE.sub("", sanitized)
            violations.append("secret_marker")

        # (a) PII в ответе
        masked, findings = self._detector.mask(sanitized)
        if findings:
            violations.extend(f"pii:{f['type']}" for f in findings)
            sanitized = masked

        return sanitized, violations

    def apply(self, answer: str, allowed_chunks: Sequence[RetrievedChunk],
              ) -> tuple[str, list[str]]:
        """check_answer + метрики по каждому классу нарушений."""
        sanitized, violations = self.check_answer(answer, allowed_chunks)
        for v in violations:
            if v.startswith("pii:"):
                record_guardrail_event(KIND_OUTPUT_PII)
            elif v.startswith("citation:"):
                record_guardrail_event(KIND_OUTPUT_CITATION)
            else:
                record_guardrail_event(KIND_OUTPUT_SECRET)
        return sanitized, violations

    def wrap_stream(
        self,
        fragments: Iterator[str],
        allowed_chunks: Sequence[RetrievedChunk],
        on_violations=None,
    ) -> Iterator[str]:
        """Обёртка стрима: клиент получает только санитизированный текст.

        Инкрементальная санитизация с hold-back-буфером: наружу отдаётся
        санитизированный префикс, последние HOLD_BACK символов удерживаются,
        чтобы паттерн (телефон/[N]/SECRET:), разрезанный границей фрагментов,
        не «протёк» до завершения. violations фиксируются по финальному
        тексту; on_violations(violations) вызывается один раз в конце.
        """
        if not self.enabled:
            buffer = ""
            for frag in fragments:
                buffer += frag
                yield frag
            if on_violations is not None:
                on_violations([])
            return

        buffer = ""
        emitted = ""
        for frag in fragments:
            buffer += frag
            safe, _ = self.check_answer(buffer, allowed_chunks)
            if len(safe) <= self.HOLD_BACK:
                continue
            candidate = safe[: len(safe) - self.HOLD_BACK]
            if candidate.startswith(emitted):
                yield candidate[len(emitted):]
                emitted = candidate
            else:
                # санитизация «съела» уже отданный регион (паттерн дорисовался):
                # помечаем правку и продолжаем от санитизированной версии
                yield "[отредактировано фильтром] "
                emitted = safe
        final, violations = self.apply(buffer, allowed_chunks)
        yield final[len(emitted):] if final.startswith(emitted) else \
            "[отредактировано фильтром] " + final
        if on_violations is not None:
            on_violations(violations)
