"""Сохранение/загрузка питомца: ~/.ai-tamagotchi/save.json, атомарно."""

import json
import os
import tempfile
import time

from .pet import Pet, TICK_SECONDS

MAX_OFFLINE_TICKS = 30
SAVE_FILE = "save.json"


def home_dir() -> str:
    """Каталог сохранений (переопределяется TAMAGOTCHI_HOME)."""
    return os.environ.get("TAMAGOTCHI_HOME") or os.path.join(
        os.path.expanduser("~"), ".ai-tamagotchi")


def save_path() -> str:
    return os.path.join(home_dir(), SAVE_FILE)


def save(pet: Pet, path: str | None = None, now: float | None = None) -> str:
    """Атомарная запись: tmp + os.replace. Внутри — метка времени."""
    path = path or save_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    data = pet.to_dict()
    data["saved_at"] = time.time() if now is None else now
    fd, tmp = tempfile.mkstemp(prefix=".save-", dir=os.path.dirname(path))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=1)
        os.replace(tmp, path)  # атомарно на той же файловой системе
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return path


def load(path: str | None = None) -> tuple[Pet, float | None]:
    """Читает save.json -> (питомец, saved_at|None). Битый -> (новый, None)."""
    path = path or save_path()
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            return Pet(), None
        saved_at = data.get("saved_at")
        try:
            saved_at = float(saved_at)
        except (TypeError, ValueError):
            saved_at = None
        return Pet.from_dict(data), saved_at
    except (OSError, ValueError, TypeError):
        return Pet(), None


def apply_offline_time(pet: Pet, saved_at: float | None,
                       now: float | None = None) -> int:
    """Офлайн-время -> тики (потолок 30) и прогон их через питомца.

    5 секунд = 1 тик (TICK_SECONDS).
    """
    now = time.time() if now is None else now
    if saved_at is None:
        return 0
    seconds = max(0.0, now - saved_at)
    ticks = min(int(seconds // TICK_SECONDS), MAX_OFFLINE_TICKS)
    for _ in range(ticks):
        pet.tick()
    return ticks
