"""Тесты ACL: матрица доступа."""

import pytest

from backend.app.ingestion.acl import ACL, allow


@pytest.mark.parametrize("user,chunk,expected", [
    ("public", "public", True),
    ("public", "internal", False),
    ("public", "secret", False),
    ("internal", "public", True),
    ("internal", "internal", True),
    ("internal", "secret", False),
    ("secret", "public", True),
    ("secret", "internal", True),
    ("secret", "secret", True),
])
def test_allow_matrix(user: str, chunk: str, expected: bool) -> None:
    assert allow(user, chunk) is expected


def test_accepts_enum_values() -> None:
    assert allow(ACL.secret, ACL.internal) is True
    assert allow(ACL.public, ACL.secret) is False


def test_hierarchical_order() -> None:
    assert ACL.public < ACL.internal < ACL.secret


def test_parse_case_insensitive() -> None:
    assert ACL.parse("SECRET") is ACL.secret
    assert ACL.parse(" Internal ") is ACL.internal


def test_parse_unknown_raises() -> None:
    with pytest.raises(ValueError, match="unknown acl"):
        ACL.parse("topsecret")
