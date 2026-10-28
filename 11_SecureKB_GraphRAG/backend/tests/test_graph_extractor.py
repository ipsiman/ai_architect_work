"""Тесты gazetteer-экстрактора сущностей."""

import pytest

from backend.app.ingestion.graph_extractor import GraphExtractor


@pytest.fixture()
def entities() -> dict:
    return {
        "persons": [
            {"id": "ivanov_ap", "full_name": "Иванов Алексей Петрович",
             "short_name": "Иванов А.П.", "position": "директор по стратегии",
             "aliases": ["Иванов Алексей Петрович", "Иванов А.П.", "Иванов"]},
            {"id": "petrova_es", "full_name": "Петрова Елена Сергеевна",
             "short_name": "Петрова Е.С.", "position": "финансовый директор",
             "aliases": ["Петрова Елена Сергеевна", "Петрова Е.С.", "Петрова"]},
        ],
        "projects": [
            {"id": "phoenix", "name": "Феникс", "aliases": ["Феникс", "«Феникс»", "проект «Феникс»"]},
            {"id": "vektor", "name": "Вектор", "aliases": ["Вектор", "«Вектор»", "проект «Вектор»"]},
        ],
        "departments": [
            {"id": "it", "name": "ИТ", "aliases": ["ИТ", "отдел ИТ"]},
            {"id": "finance", "name": "Финансы", "aliases": ["Финансы", "отдел Финансы"]},
        ],
    }


@pytest.fixture()
def extractor(entities: dict) -> GraphExtractor:
    return GraphExtractor(entities)


def test_finds_person_project_department(extractor: GraphExtractor) -> None:
    text = ("Совещание вёл Иванов А.П.; обсуждали проект «Вектор» "
            "и миграцию отдела ИТ.")
    found = extractor.extract(text)
    assert found["person"] == ["ivanov_ap"]
    assert found["project"] == ["vektor"]
    assert found["department"] == ["it"]


def test_full_name_and_surname_forms(extractor: GraphExtractor) -> None:
    assert extractor.extract("Доклад утверждает Иванов Алексей Петрович")["person"] == ["ivanov_ap"]
    assert extractor.extract("Ответственный — Иванов")["person"] == ["ivanov_ap"]


def test_case_insensitive(extractor: GraphExtractor) -> None:
    found = extractor.extract("статус по проекту феникс готовит иванов а.п.")
    assert found["person"] == ["ivanov_ap"]
    assert found["project"] == ["phoenix"]


def test_surname_does_not_match_other_surname(extractor: GraphExtractor) -> None:
    """«Иванов» не должен ложно матчититься в «Иванова» (другая фамилия)."""
    text = "Иванова Мария Сергеевна оформила заявку в отдел Финансы."
    found = extractor.extract(text)
    assert found["person"] == []  # ни Иванов, ни Петрова — Иванова это другой человек
    assert found["department"] == ["finance"]


def test_no_false_suffix_match(extractor: GraphExtractor) -> None:
    """Фамилия в косвенном падеже не должна совпадать с точной формой."""
    found = extractor.extract("Документ согласован с Ивановой и Петровой.")
    assert found["person"] == []


def test_multiple_entities(extractor: GraphExtractor) -> None:
    text = "Иванов А.П. и Петрова Е.С. обсудили «Феникс» и отдел ИТ."
    found = extractor.extract(text)
    assert found["person"] == ["ivanov_ap", "petrova_es"]
    assert extractor.node_ids(text) == [
        "person:ivanov_ap", "person:petrova_es", "project:phoenix", "department:it",
    ]


def test_empty_text(extractor: GraphExtractor) -> None:
    found = extractor.extract("")
    assert found == {"person": [], "project": [], "department": []}
