"""Модель контроля доступа (ACL) SecureKB.

Уровни: public=0 < internal=1 < secret=2. Пользователь видит чанк,
если его уровень доступа не ниже уровня чанка.
"""

from __future__ import annotations

from enum import IntEnum


class ACL(IntEnum):
    """Уровень доступа; порядок значений задаёт иерархию."""

    public = 0
    internal = 1
    secret = 2

    @classmethod
    def parse(cls, value: str | "ACL") -> "ACL":
        """Разбирает строку из frontmatter/payload в ACL (регистронезависимо)."""
        if isinstance(value, cls):
            return value
        key = str(value).strip().lower()
        try:
            return cls[key]  # lookup по имени члена enum
        except KeyError as exc:
            valid = [m.name for m in cls]
            raise ValueError(f"unknown acl {value!r}, expected one of {valid}") from exc


def allow(user_acl: ACL | str, chunk_acl: ACL | str) -> bool:
    """True, если пользователь с уровнем user_acl может видеть чанк уровня chunk_acl."""
    return ACL.parse(user_acl) >= ACL.parse(chunk_acl)
