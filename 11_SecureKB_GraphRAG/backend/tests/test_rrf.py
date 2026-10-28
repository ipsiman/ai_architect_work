"""Тесты RRF-fusion (этап C): порядок слияния, k-параметр, выброс чужого ACL."""

import logging

from backend.app.ingestion.acl import ACL
from backend.app.retrieval.fuse import RetrievedChunk, rrf_fuse


def chunk(chunk_id: str, acl: ACL = ACL.public, doc_id: str | None = None) -> RetrievedChunk:
    return RetrievedChunk(
        chunk_id=chunk_id,
        doc_id=doc_id or chunk_id.split("#")[0],
        title="t",
        acl=acl,
        text="text",
    )


def test_rrf_chunk_found_in_two_branches_wins():
    """Чанк, найденный обеими ветками, получает RRF-бонус и выходит первым."""
    only_dense = [chunk("A#0"), chunk("B#0")]
    both = [chunk("C#0"), chunk("A#0")]
    fused = rrf_fuse({"dense": only_dense, "lexical": both}, ACL.public, top_n=3)

    assert [c.chunk_id for c in fused][0] == "A#0"  # 1/(60+2)+1/(60+1) > 1/(60+1)
    assert {c.chunk_id for c in fused} == {"A#0", "B#0", "C#0"}


def test_rrf_rank_position_matters():
    """Ранг внутри ветки влияет на итоговый порядок (выше ранг — больше балл)."""
    branch1 = [chunk("X#0"), chunk("Y#0")]
    branch2 = [chunk("X#0"), chunk("Y#0")]
    fused = rrf_fuse({"dense": branch1, "lexical": branch2}, ACL.public, top_n=2)
    assert [c.chunk_id for c in fused] == ["X#0", "Y#0"]

    # Y нашёлся в обеих ветках — обгоняет X, хотя в dense у него ранг ниже
    swapped = [chunk("Y#0")]  # Y: 1/62 + 1/61 > X: 1/61
    fused2 = rrf_fuse({"dense": branch1, "lexical": swapped}, ACL.public, top_n=2)
    assert [c.chunk_id for c in fused2] == ["Y#0", "X#0"]


def test_rrf_k_parameter_changes_blend():
    """Маленький k сильнее ценит высокие позиции, большой k — сглаживает."""
    dense = [chunk("A#0"), chunk("B#0"), chunk("C#0")]
    lexical = [chunk("C#0"), chunk("B#0"), chunk("A#0")]

    # ветки зеркальны: A и C симметричны (1/(k+1)+1/(k+3)), B = 2/(k+2) меньше при k=1
    fused_small = rrf_fuse({"dense": dense, "lexical": lexical}, ACL.public, k=1, top_n=3)
    assert [c.chunk_id for c in fused_small] == ["A#0", "C#0", "B#0"]  # при k=1: 0.75, 0.75, 0.67

    # при любом k A=C>B (неравенство о средних), тай-брейк — chunk_id
    fused_big = rrf_fuse({"dense": dense, "lexical": lexical}, ACL.public, k=1000, top_n=3)
    assert [c.chunk_id for c in fused_big] == ["A#0", "C#0", "B#0"]


def test_rrf_top_n_limits_output():
    dense = [chunk(f"D{i}#0") for i in range(10)]
    fused = rrf_fuse({"dense": dense}, ACL.public, top_n=3)
    assert len(fused) == 3
    assert [c.chunk_id for c in fused] == ["D0#0", "D1#0", "D2#0"]


def test_rrf_drops_chunks_above_user_acl_with_warning(caplog):
    """Defense-in-depth: секретный чанк выбрасывается даже если ветка его вернула."""
    leaked = [chunk("S#0", ACL.secret), chunk("P#0", ACL.public)]
    with caplog.at_level(logging.WARNING, logger="backend.app.retrieval.fuse"):
        fused = rrf_fuse({"dense": leaked}, ACL.public, top_n=5)

    assert [c.chunk_id for c in fused] == ["P#0"]
    assert any("defense-in-depth" in rec.message for rec in caplog.records)


def test_rrf_secret_user_sees_all_levels():
    chunks = [chunk("P#0", ACL.public), chunk("I#0", ACL.internal), chunk("S#0", ACL.secret)]
    fused = rrf_fuse({"dense": chunks}, ACL.secret, top_n=5)
    assert len(fused) == 3
