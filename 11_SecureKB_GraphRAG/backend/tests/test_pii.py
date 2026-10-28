"""Тесты PII-детектора: маскировка всех типов, ложные срабатывания, идемпотентность."""

from backend.app.ingestion.pii import PIIDetector


def make_detector() -> PIIDetector:
    return PIIDetector()


def test_phone_masked() -> None:
    masked, found = make_detector().mask("Звоните: +7-912-345-67-89 или +7 921 555 10 04")
    assert "[PHONE]" in masked
    types = {f["type"] for f in found}
    assert types == {"PHONE"}
    assert all(f["value"].startswith("+7") for f in found)


def test_email_masked() -> None:
    masked, found = make_detector().mask("Почта для связи: a.ivanov@securekb.ru")
    assert masked == "Почта для связи: [EMAIL]"
    assert found == [{"type": "EMAIL", "value": "a.ivanov@securekb.ru"}]


def test_inn_10_masked_with_keyword() -> None:
    masked, found = make_detector().mask("Реквизиты: ИНН 7707083893, КПП 770701001")
    assert "[INN_10]" in masked
    assert found == [{"type": "INN_10", "value": "7707083893"}]


def test_inn_12_masked_not_confused_with_inn10() -> None:
    masked, found = make_detector().mask("ИНН физлица 366406939723 подтверждён")
    assert "[INN_12]" in masked
    types = {f["type"] for f in found}
    assert types == {"INN_12"}  # 12 цифр не трактуется как 10-значный ИНН


def test_passport_masked() -> None:
    masked, found = make_detector().mask("Предъявлен паспорт 4509 123456, копия в деле")
    assert "[PASSPORT_RU]" in masked
    assert found == [{"type": "PASSPORT_RU", "value": "4509 123456"}]


def test_card_pan_masked() -> None:
    masked, found = make_detector().mask("Зарплатная карта 2202 2036 4415 8877 активна")
    assert "[CARD_PAN]" in masked
    assert found == [{"type": "CARD_PAN", "value": "2202 2036 4415 8877"}]


def test_order_number_is_not_inn() -> None:
    """Ложное срабатывание: 10 цифр без ключевого слова «ИНН» не маскируются.

    Обоснование реализации: INN_10/INN_12 требуют ключевое слово «ИНН»
    в окне 30 символов ПЕРЕД числом; номер заказа его не имеет.
    """
    text = "Номер заказа 1234567890 присвоен автоматически."
    masked, found = make_detector().mask(text)
    assert masked == text
    assert found == []


def test_passport_without_keyword_not_masked() -> None:
    """Цифры формата 4+6 без слова «паспорт» рядом — консервативно не трогаем."""
    text = "Код группы 4509 123456 в реестре аудита."
    masked, found = make_detector().mask(text)
    assert found == []


def test_idempotent_double_run() -> None:
    detector = make_detector()
    original = "Паспорт 4510 884321, ИНН 773134568742, тел. +7-921-555-10-04, " \
               "почта e.petrova@securekb.ru, карта 2202 2036 4415 8877"
    once, found1 = detector.mask(original)
    twice, found2 = detector.mask(once)
    assert once == twice
    assert found2 == []
    assert len(found1) == 5
    assert "[PASSPORT_RU]" in once and "[INN_12]" in once and "[PHONE]" in once
    assert "[EMAIL]" in once and "[CARD_PAN]" in once


def test_multiple_occurrences_and_values() -> None:
    masked, found = make_detector().mask(
        "телефоны: +7-903-777-45-12 и +7-916-200-84-31"
    )
    assert masked.count("[PHONE]") == 2
    assert [f["value"] for f in found] == ["+7-903-777-45-12", "+7-916-200-84-31"]
