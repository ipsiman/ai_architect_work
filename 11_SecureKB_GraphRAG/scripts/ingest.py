"""CLI инжеста корпуса в Qdrant + SQLite-граф.

Запуск из корня проекта:
    uv run python scripts/ingest.py [--recreate] [--corpus data/corpus]

Если Qdrant не поднят — скрипт пытается поднять его через docker compose
(infra/docker-compose.yml) и ждёт healthz; при неудаче печатает инструкцию.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

COMPOSE_FILE = ROOT / "infra" / "docker-compose.yml"


def qdrant_alive(base_url: str, timeout: float = 2.0) -> bool:
    """Пинг /healthz у Qdrant."""
    try:
        with urllib.request.urlopen(f"{base_url.rstrip('/')}/healthz", timeout=timeout) as r:
            return r.status == 200
    except (urllib.error.URLError, TimeoutError, OSError):
        return False


def wait_qdrant(base_url: str, seconds: int = 60) -> bool:
    """Ждёт готовности Qdrant до `seconds` секунд."""
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if qdrant_alive(base_url):
            return True
        time.sleep(2)
    return False


def ensure_qdrant(base_url: str) -> None:
    """Гарантирует, что Qdrant поднят; иначе понятная ошибка."""
    if qdrant_alive(base_url):
        print(f"[ok] Qdrant доступен: {base_url}")
        return
    print("[..] Qdrant не отвечает, поднимаю docker compose up -d qdrant …")
    try:
        subprocess.run(
            ["docker", "compose", "-f", str(COMPOSE_FILE), "up", "-d", "qdrant"],
            check=True,
            cwd=ROOT,
        )
    except FileNotFoundError:
        fail_and_exit("docker CLI не найден. Установите Docker Desktop.", base_url)
    except subprocess.CalledProcessError as exc:
        fail_and_exit(f"docker compose завершился с ошибкой ({exc.returncode}).", base_url)
    if not wait_qdrant(base_url):
        fail_and_exit("Qdrant не стал здоровым за 60 секунд.", base_url)
    print(f"[ok] Qdrant поднят: {base_url}")


def fail_and_exit(reason: str, base_url: str) -> None:
    print(f"[error] {reason}")
    print(
        "Инструкция:\n"
        f"  1) docker compose -f {COMPOSE_FILE} up -d qdrant\n"
        f"  2) дождитесь healthz: curl -s {base_url}/healthz\n"
        "  3) повторите: uv run python scripts/ingest.py"
    )
    sys.exit(1)


def main() -> None:
    parser = argparse.ArgumentParser(description="Инжест корпуса SecureKB")
    parser.add_argument("--recreate", action="store_true",
                        help="пересоздать коллекцию Qdrant перед загрузкой")
    parser.add_argument("--corpus", default=None, help="каталог корпуса (по умолчанию из Settings)")
    args = parser.parse_args()

    from backend.app.core.config import get_settings
    from backend.app.ingestion.graphs.ingest_graph import run_ingestion

    settings = get_settings()
    corpus_dir = args.corpus or settings.corpus_dir
    if not Path(corpus_dir).resolve().exists():
        sys.exit(f"[error] корпус не найден: {corpus_dir}. Сначала: "
                 "uv run python scripts/generate_corpus.py")

    ensure_qdrant(settings.qdrant_url)

    print(f"[..] инжест: corpus={corpus_dir} collection={settings.qdrant_collection} "
          f"model={settings.embedding_model} recreate={args.recreate}")
    report = run_ingestion(corpus_dir=corpus_dir, recreate=args.recreate)

    print("\n=== Отчёт инжеста SecureKB ===")
    print(f"документы : {report.docs}")
    print(f"чанки     : {report.chunks}")
    print(f"векторы   : {report.vectors}")
    print(f"PII-находки: {report.pii_found}")
    print(f"узлы графа: {report.nodes}")
    print(f"рёбра графа: {report.edges}")


if __name__ == "__main__":
    main()
