"""Тесты лексического (BM25) поиска: per-ACL изоляция, токенизация RU."""

from backend.app.ingestion.acl import ACL
from backend.app.retrieval.lexical import LexicalIndex, search_lexical, tokenize

SECRET_TEXT = (
    "Бюджет проекта «Феникс» — 240 млн руб.; соглашение о слиянии подписывает "
    "Иванов А.П.; дата закрытия сделки — 15 марта 2027 года."
)
INTERNAL_TEXT = (
    "Качественные метрики проекта «Вектор» стабильны: доля успешно пройденных "
    "автотестов — 78%, критических дефектов не зафиксировано."
)
PUBLIC_TEXT = (
    "Командировка сотрудника отдела НИОКР оформляется не позднее чем за пять "
    "рабочих дней до выезда."
)


def payload(chunk_id: str, acl: str, text: str) -> dict:
    return {
        "chunk_id": chunk_id,
        "doc_id": chunk_id.split("#")[0],
        "title": "t",
        "acl": acl,
        "text": text,
    }


def make_index() -> LexicalIndex:
    """≥3 документов на уровень: иначе BM25 IDF вырождается (термин во всех
    документах уровня получает неположительный IDF и score обнуляется)."""
    return LexicalIndex.from_payloads([
        payload("DOC-0001#0", "public", PUBLIC_TEXT),
        payload("DOC-0002#0", "public", "Парольная политика требует длину пароля не менее двенадцати символов."),
        payload("DOC-0003#0", "public", "Резервное копирование рабочих данных выполняется ежедневно в ночном окне."),
        payload("DOC-0051#0", "internal", INTERNAL_TEXT),
        payload("DOC-0052#0", "internal", "Совещание по проекту «Атлас» обсудило готовность к пилоту и бюджетные рамки."),
        payload("DOC-0053#0", "internal", "Дорожная карта инициативы «Меридиан» включает три контрольные точки."),
        payload("DOC-0086#0", "secret", SECRET_TEXT),
        payload("DOC-0087#0", "secret", "Оценка целевой компании ООО «Гранит-Холдинг» определена в размере 1,85 млрд руб."),
        payload("DOC-0088#0", "secret", "Премиальный пул для ключевых участников проекта «Феникс» составляет 18 млн руб."),
    ])


def test_tokenize_russian():
    tokens = tokenize("Бюджет проекта «Феникс» — 240 млн руб.; (соглашение) от 15.03.2027")
    assert "бюджет" in tokens and "феникс" in tokens and "соглашение" in tokens
    assert "240" in tokens and "2027" in tokens  # числа-токены сохраняются
    assert all(len(t) >= 2 for t in tokens)  # одиночные символы отброшены
    assert "«" not in tokens and "—" not in tokens  # пунктуация не токен


def test_isolation_secret_not_found_for_public_user():
    """КЛЮЧЕВОЕ: secret-чанк НЕ находится у public-пользователя — его просто
    нет в public-индексе (изоляция by construction, не пост-фильтр)."""
    index = make_index()
    hits = index.search("бюджет проекта Феникс 240 млн", ACL.public, top_k=5)
    assert all(h.acl == ACL.public for h in hits)
    assert all(h.chunk_id != "DOC-0086#0" for h in hits)


def test_isolation_each_level_searches_own_index():
    index = make_index()

    public_hits = index.search("командировка отдел НИОКР рабочих дней", ACL.public)
    assert public_hits and public_hits[0].doc_id == "DOC-0001"
    assert all(h.acl == ACL.public for h in public_hits)

    internal_hits = index.search("автотестов проект Вектор метрики", ACL.internal)
    assert internal_hits and internal_hits[0].doc_id == "DOC-0051"

    secret_hits = index.search("бюджет проекта Феникс 240", ACL.secret)
    assert secret_hits and secret_hits[0].doc_id == "DOC-0086"


def test_search_no_match_returns_empty():
    index = make_index()
    assert index.search("квантовый запускаемый синхрофазотрон", ACL.public) == []
    assert index.search("", ACL.secret) == []  # пустой запрос — нет токенов
    assert index.search("соглашение подписывает Иванов", ACL.public) == []  # только в secret


def test_scores_sorted_desc():
    index = LexicalIndex.from_payloads([
        payload("D1#0", "public", "факторинг ставка проценты банк лимит"),
        payload("D2#0", "public", "факторинг"),
        payload("D3#0", "public", "совсем другой текст про погоду"),
    ])
    hits = index.search("факторинг ставка лимит", ACL.public, top_k=3)
    scores = [h.score for h in hits]
    assert scores == sorted(scores, reverse=True)
    assert hits[0].chunk_id == "D1#0"


def test_search_lexical_wrapper():
    index = make_index()
    hits = search_lexical("бюджет проекта Феникс", ACL.secret, top_k=3, index=index)
    assert hits and hits[0].doc_id == "DOC-0086"


def test_size_counts():
    index = make_index()
    assert index.size() == 9
    assert index.size(ACL.public) == 3
    assert index.size(ACL.secret) == 3
    assert index.size(ACL.internal) == 3
