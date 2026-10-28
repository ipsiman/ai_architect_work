r"""Детектор и маскировщик PII для текстов чанков.

Типы: PHONE, EMAIL, INN_10, INN_12, PASSPORT_RU, CARD_PAN.

Борьба с ложными срабатываниями (спроектировано консервативно):
- INN_10/INN_12 и PASSPORT_RU требуют ключевое слово рядом («ИНН», «паспорт»)
  в окне CONTEXT_WINDOW символов ПЕРЕД числом — «номер заказа 1234567890»
  не маскируется, потому что рядом нет ключевого слова «ИНН»;
- числовые шаблоны защищены границами (?<!\d)…(?!\d), поэтому 10-значный
  шаблон не срабатывает внутри 12-значного числа;
- CARD_PAN — только 16 цифр группами по 4 с разделителями (типовая
  печатная форма), «голые» длинные цифровые строки не трогаем;
- PHONE — строгий формат +7-9xx.

Повторный mask() по уже маскированному тексту ничего не находит
(в плейсхолдерах нет цифр) — операция идемпотентна.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

CONTEXT_WINDOW = 30  # символов перед числом, где ищем ключевое слово

PatternDef = tuple[str, re.Pattern[str], str | None]

_PHONE = re.compile(r"\+7[-\s]?\(?9\d{2}\)?[-\s]?\d{3}[-\s]?\d{2}[-\s]?\d{2}")
_EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
_INN_12 = re.compile(r"(?<!\d)\d{12}(?!\d)")
_INN_10 = re.compile(r"(?<!\d)\d{10}(?!\d)")
_PASSPORT = re.compile(r"(?<!\d)\d{4}[-\s]\d{6}(?!\d)")
_CARD = re.compile(r"(?<!\d)\d{4}[-\s]\d{4}[-\s]\d{4}[-\s]\d{4}(?!\d)")

# порядок важен: сначала более специфичные/длинные шаблоны
_ORDERED: list[PatternDef] = [
    ("PHONE", _PHONE, None),
    ("EMAIL", _EMAIL, None),
    ("CARD_PAN", _CARD, None),
    ("INN_12", _INN_12, r"\bИНН\b"),
    ("INN_10", _INN_10, r"\bИНН\b"),
    ("PASSPORT_RU", _PASSPORT, r"\bпаспорт"),
]


@dataclass(slots=True)
class PIIFinding:
    """Одно найденное PII-вхождение."""

    type: str
    value: str


class PIIDetector:
    """Детектор PII: mask(text) → (замаскированный текст, список находок)."""

    def __init__(self, patterns: list[PatternDef] | None = None) -> None:
        self._patterns = patterns if patterns is not None else _ORDERED

    @staticmethod
    def _keyword_before(text: str, pos: int, keyword: str) -> bool:
        """Есть ли ключевое слово в окне CONTEXT_WINDOW символов перед pos."""
        window = text[max(0, pos - CONTEXT_WINDOW) : pos]
        return re.search(keyword, window, flags=re.IGNORECASE) is not None

    def find_all(self, text: str) -> list[tuple[int, int, str, str]]:
        """Собирает неперекрывающиеся находки (start, end, type, value)."""
        raw: list[tuple[int, int, str, str]] = []
        for ptype, rx, keyword in self._patterns:
            for m in rx.finditer(text):
                if keyword and not self._keyword_before(text, m.start(), keyword):
                    continue
                raw.append((m.start(), m.end(), ptype, m.group(0)))
        # жадное разрешение перекрытий: кто раньше начал и длиннее — тот выиграл
        raw.sort(key=lambda x: (x[0], -(x[1] - x[0])))
        selected: list[tuple[int, int, str, str]] = []
        last_end = -1
        for item in raw:
            if item[0] >= last_end:
                selected.append(item)
                last_end = item[1]
        return selected

    def mask(self, text: str) -> tuple[str, list[dict]]:
        """Заменяет PII на плейсхолдеры [PHONE], [EMAIL], ... и возвращает находки."""
        findings = self.find_all(text)
        masked = text
        for start, end, ptype, value in reversed(findings):
            masked = masked[:start] + f"[{ptype}]" + masked[end:]
        return masked, [{"type": f_type, "value": value} for _, _, f_type, value in findings]
