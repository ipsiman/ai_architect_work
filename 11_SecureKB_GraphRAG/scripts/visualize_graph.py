"""Выгрузка графа сущностей (data/graph.db) в визуальные форматы (этап D).

Создаёт:
    data/graph.dot  — DOT-файл Graphviz (узлы раскрашены по типу:
                      person=голубой, project=зелёный, department=жёлтый);
    data/graph.png  — рендер системным `dot`, если graphviz установлен;
    data/graph.md   — Mermaid-версия (graph LR + classDef по типам),
                      пригодна для рендера в GitHub/GitLab.

Запуск: uv run python scripts/visualize_graph.py [--db data/graph.db]
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

# тип сущности → (DOT-цвет заливки, Mermaid class)
TYPE_STYLES = {
    "person": ("#cce5ff", "person"),
    "project": ("#d4edda", "project"),
    "department": ("#fff3cd", "department"),
}
DEFAULT_STYLE = ("#f0f0f0", "other")


def load_graph(db_path: Path) -> tuple[list[tuple], list[tuple]]:
    conn = sqlite3.connect(str(db_path))
    try:
        nodes = conn.execute(
            "SELECT id, type, name, doc_ids FROM nodes ORDER BY id").fetchall()
        edges = conn.execute(
            "SELECT src, dst, relation FROM edges ORDER BY src, dst").fetchall()
    finally:
        conn.close()
    return nodes, edges


def to_dot(nodes: list[tuple], edges: list[tuple]) -> str:
    lines = [
        "digraph securekb {",
        '  rankdir=LR;',
        '  label="SecureKB: граф сущностей (со-встречаемость в чанках)";',
        '  labelloc="t";',
        '  node [shape=box, style="filled,rounded", fontsize=10];',
        '  edge [color="#999999", arrowsize=0.6];',
    ]
    for node_id, ntype, name, doc_ids in nodes:
        color, _ = TYPE_STYLES.get(ntype, DEFAULT_STYLE)
        docs_count = len(json.loads(doc_ids))
        label = f"{name}\\n({ntype}, {docs_count} docs)".replace('"', '\\"')
        lines.append(f'  "{node_id}" [label="{label}", fillcolor="{color}"];')
    for src, dst, _relation in edges:
        lines.append(f'  "{src}" -> "{dst}";')
    lines.append("}")
    return "\n".join(lines) + "\n"


def _mermaid_id(node_id: str) -> str:
    return re.sub(r"\W", "_", node_id)


def to_mermaid(nodes: list[tuple], edges: list[tuple]) -> str:
    lines = ["```mermaid", "graph LR"]
    for node_id, ntype, name, doc_ids in nodes:
        mid = _mermaid_id(node_id)
        docs_count = len(json.loads(doc_ids))
        label = f"{name} ({ntype}, {docs_count} docs)"
        label = label.replace('"', "'").replace("[", "(").replace("]", ")")
        lines.append(f'    {mid}["{label}"]')
    for node_id, ntype, _, _ in nodes:
        _, cls = TYPE_STYLES.get(ntype, DEFAULT_STYLE)
        mid = _mermaid_id(node_id)
        lines.append(f"    class {mid} {cls};")
    for src, dst, _relation in edges:
        lines.append(f"    {_mermaid_id(src)} --> {_mermaid_id(dst)}")
    lines += [
        "    classDef person fill:#cce5ff,stroke:#5b9bd5;",
        "    classDef project fill:#d4edda,stroke:#5bb876;",
        "    classDef department fill:#fff3cd,stroke:#d4ac0d;",
        "    classDef other fill:#f0f0f0,stroke:#999999;",
        "```",
    ]
    return "\n".join(lines) + "\n"


def main() -> None:
    parser = argparse.ArgumentParser(description="Визуализация графа сущностей")
    parser.add_argument("--db", default=str(ROOT / "data" / "graph.db"))
    parser.add_argument("--out-dot", default=str(ROOT / "data" / "graph.dot"))
    parser.add_argument("--out-png", default=str(ROOT / "data" / "graph.png"))
    parser.add_argument("--out-md", default=str(ROOT / "data" / "graph.md"))
    args = parser.parse_args()

    db_path = Path(args.db)
    if not db_path.exists():
        print(f"ошибка: {db_path} не найден (сначала запустите ingest)")
        sys.exit(1)

    nodes, edges = load_graph(db_path)
    dot_path = Path(args.out_dot)
    dot_path.write_text(to_dot(nodes, edges), encoding="utf-8")
    print(f"создан {dot_path} ({len(nodes)} узлов, {len(edges)} рёбер)")

    # Mermaid-версия — всегда (не требует внешних зависимостей)
    md_path = Path(args.out_md)
    md_path.write_text(to_mermaid(nodes, edges), encoding="utf-8")
    print(f"создан {md_path} (mermaid, graph LR)")

    # PNG через системный graphviz, если установлен
    if shutil.which("dot"):
        png_path = Path(args.out_png)
        result = subprocess.run(
            ["dot", "-Tpng", str(dot_path), "-o", str(png_path)],
            capture_output=True, text=True,
        )
        if result.returncode == 0:
            print(f"создан {png_path} (рендер graphviz)")
        else:
            print(f"graphviz вернул ошибку: {result.stderr.strip()}")
    else:
        print("graphviz (`dot`) не найден — PNG пропущен, "
              "используйте .dot или mermaid-версию")


if __name__ == "__main__":
    main()
