"""Reciprocal Rank Fusion (RRF) для слияния веток ретрива + контрольная
проверка ACL (defense-in-depth).

RetrievedChunk — общий тип результата всех веток ретрива, определён здесь,
потому что fuse.py не зависит от других модулей пакета.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass

from backend.app.ingestion.acl import ACL

logger = logging.getLogger(__name__)

RRF_K = 60  # стандартная константа RRF (TREC-эксперименты)


@dataclass(slots=True)
class RetrievedChunk:
    """Чанк, вернувшийся из ветки ретрива (dense/lexical/graph)."""

    chunk_id: str
    doc_id: str
    title: str
    acl: ACL
    text: str
    score: float = 0.0


def rrf_fuse(
    lists: Mapping[str, list[RetrievedChunk]],
    user_acl: ACL | str,
    k: int = RRF_K,
    top_n: int = 5,
    user_id: str | None = None,
    audit=None,
) -> list[RetrievedChunk]:
    """Сливает ранжированные списки веток по формуле RRF:

        score(chunk) = Σ_branches 1 / (k + rank_branch(chunk)),  rank с 1.

    Чанки, встречающиеся в нескольких ветках, получают бонус. Порядок
    детерминирован: по убыванию RRF-балла, при равенстве — по chunk_id.

    Финальная страховка (defense-in-depth): чанки с acl > user_acl выбрасываются
    с warning-логом — даже если какая-то ветка ретрива забыла фильтр. Каждый
    выброс фиксируется в аудит (event=acl_drop) и метрике guardrail, если
    передан user_id (в чистых unit-тестах rrf_fuse вызывается без него);
    audit — альтернативный AuditLogger (по умолчанию синглтон).
    """
    user_level = ACL.parse(user_acl)
    scores: dict[str, float] = {}
    by_chunk_id: dict[str, RetrievedChunk] = {}

    dropped = []
    for branch, chunks in lists.items():
        for rank, chunk in enumerate(chunks, start=1):
            if ACL.parse(chunk.acl) > user_level:
                dropped.append((branch, chunk.chunk_id, ACL.parse(chunk.acl).name))
                continue
            scores[chunk.chunk_id] = scores.get(chunk.chunk_id, 0.0) + 1.0 / (k + rank)
            by_chunk_id.setdefault(chunk.chunk_id, chunk)

    if dropped:
        from backend.app.obs.metrics import record_guardrail_event
        from backend.app.security import audit as audit_mod

        for branch, chunk_id, acl_name in dropped:
            logger.warning(
                "rrf_fuse: defense-in-depth — выброшен чанк %s (acl=%s > user=%s) из ветки %s",
                chunk_id, acl_name, user_level.name, branch,
            )
            record_guardrail_event("acl_drop")
            if user_id is not None:
                sink = audit if audit is not None else audit_mod.get_audit_logger()
                sink.log(
                    user_id, audit_mod.ACL_DROP,
                    {"branch": branch, "chunk_id": chunk_id, "chunk_acl": acl_name,
                     "user_acl": user_level.name},
                )

    ordered = sorted(scores, key=lambda cid: (-scores[cid], cid))
    return [by_chunk_id[cid] for cid in ordered[:top_n]]
