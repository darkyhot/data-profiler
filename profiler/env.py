"""Мини-загрузчик .env без зависимостей.

profiler.run() зовёт load_dotenv() на старте: переменные из файла .env (в
рабочем каталоге или корне проекта) попадают в os.environ, если ещё не заданы.
Так токены (JPY_API_TOKEN, DEEPSEEK_API_KEY, …) не нужно вводить каждый раз.
Файл .env в .gitignore — в репозиторий не коммитится.
"""

from __future__ import annotations

import os
from pathlib import Path


def load_dotenv(path: str | Path | None = None) -> bool:
    """Загрузить .env. Возвращает True, если файл найден и прочитан.
    Ищем: явный path → ./.env → .env рядом с пакетом (корень проекта)."""
    candidates = []
    if path:
        candidates.append(Path(path))
    candidates.append(Path.cwd() / ".env")
    candidates.append(Path(__file__).resolve().parent.parent / ".env")

    for p in candidates:
        if p.is_file():
            _apply(p)
            return True
    return False


def _apply(p: Path) -> None:
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        key, val = key.strip(), val.strip().strip('"').strip("'")
        if key and key not in os.environ:      # не перетираем уже заданное
            os.environ[key] = val
