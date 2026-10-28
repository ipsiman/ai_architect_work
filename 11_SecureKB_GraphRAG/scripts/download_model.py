"""Скачивание GGUF-модели Qwen2.5-1.5B-Instruct в models/ (каталог gitignored).

Идемпотентен: если файл уже лежит в models/ и имеет ожидаемый размер —
печатает путь и размер, ничего не качает. Иначе докачивает через
huggingface_hub.hf_hub_download (repo_type="model", возобновление Supported).

Запуск: uv run python scripts/download_model.py
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

REPO_ID = "Qwen/Qwen2.5-1.5B-Instruct-GGUF"
FILENAME = "qwen2.5-1.5b-instruct-q4_k_m.gguf"
MODELS_DIR = ROOT / "models"


def human(size_bytes: int) -> str:
    """Размер в человекочитаемом виде."""
    value = float(size_bytes)
    for unit in ("Б", "КБ", "МБ", "ГБ"):
        if value < 1024 or unit == "ГБ":
            return f"{value:.2f} {unit}"
        value /= 1024
    return f"{value:.2f} ГБ"


def main() -> None:
    MODELS_DIR.mkdir(parents=True, exist_ok=True)
    local = MODELS_DIR / FILENAME

    if local.exists() and local.stat().st_size > 900_000_000:  # ~1 ГБ уже есть
        print(f"[ok] модель уже скачана: {local} ({human(local.stat().st_size)})")
        print(local.resolve())
        return

    print(f"[..] скачиваю {REPO_ID}/{FILENAME} (~1.0 ГБ) …")
    from huggingface_hub import hf_hub_download

    path = hf_hub_download(
        repo_id=REPO_ID,
        filename=FILENAME,
        repo_type="model",
        local_dir=str(MODELS_DIR),
    )
    size = Path(path).stat().st_size
    print(f"[ok] скачано: {path}")
    print(f"     размер: {size} байт ({human(size)})")
    print(str(Path(path).resolve()))


if __name__ == "__main__":
    main()
