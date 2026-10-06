"""Клиент локальной Ollama (OpenAI-совместимый /v1/chat/completions).

Только stdlib, только localhost. Все ошибки -> None, без стектрейсов.
Плюс: чистый планировщик реплик (троттлинг/приоритеты, инъекция времени)
и фоновый воркер с единственным потоком и очередью.
"""

import json
import queue
import random
import sys
import threading
import time
import urllib.request

from . import fallback

MAX_PHRASE_LEN = 80
CHECK_TIMEOUT = 2.0
_DUMP_CHARS = "\"'` «»“”‘’ \t\r\n"

# Приоритеты триггеров (меньше = важнее)
PRIO_ACTION = 0
PRIO_MOOD = 1
PRIO_IDLE = 2
KIND_LABEL = {PRIO_ACTION: "action", PRIO_MOOD: "mood", PRIO_IDLE: "idle"}


def build_system_prompt(name: str) -> str:
    """Системный промпт: питомец, одна короткая фраза, примеры."""
    return (
        f"Ты — маленький питомец по имени {name}. "
        "Отвечай ВСЕГДА только по-русски ОДНОЙ короткой фразой до 80 "
        "символов, от первого лица, без кавычек, эмодзи и пояснений. "
        "Пиши только по-русски, без иероглифов и английских слов. "
        "Примеры: «Ням-ням, вкусно!»; «Поиграй со мной, скучно»."
    )


def build_user_message(name: str, mood: str, params: dict,
                       event: str | None) -> str:
    """User-сообщение строит код: настроение, параметры, событие. Истории нет."""
    stats = (
        f"сытость={float(params.get('hunger', 0)):.0f}, "
        f"энергия={float(params.get('energy', 0)):.0f}, "
        f"веселье={float(params.get('fun', 0)):.0f}, "
        f"чистота={float(params.get('hygiene', 0)):.0f}"
    )
    parts = [f"Питомец {name}. Настроение: {mood}.", f"Параметры: {stats}."]
    events = {
        "fed": "питомца только что покормили",
        "too_full": "питомца пытались накормить, но он уже сыт",
        "played": "с питомцем только что поиграли",
        "too_tired": "питомец слишком устал, чтобы играть",
        "sleep_started": "питомец лёг спать",
        "woke_up": "питомец проснулся",
        "not_sleepy": "питомцу предложили спать, но он не wants спать",
        "cleaned": "питомца только что помыли",
        "is_sleeping": "к питомцу обратились, но он спит",
        "not_sleeping": "питомца хотели разбудить, но он не спит",
        "idle": "ничего не происходит, питомец просто говорит",
        "greet": "питомец здоровается",
    }
    if event:
        parts.append(f"Событие: {events.get(event, event)}.")
    parts.append("Скажи одну короткую фразу от первого лица.")
    return " ".join(parts)


def _clean_phrase(text) -> str | None:
    """Первая непустая строка, снять кавычки/пробелы, обрезать до 80."""
    if not isinstance(text, str):
        return None
    for line in text.splitlines():
        line = line.strip().strip(_DUMP_CHARS).strip()
        if line:
            return line[:MAX_PHRASE_LEN]
    return None


def _is_cjk(ch: str) -> bool:
    """Символ из CJK/кана/хангыль-диапазонов (иероглифы и т.п.)."""
    code = ord(ch)
    return (0x3040 <= code <= 0x30FF      # хирагана/катакана
            or 0x3400 <= code <= 0x9FFF   # CJK-иероглифы
            or 0xAC00 <= code <= 0xD7AF)  # хангыль


def _char_kind(ch: str) -> str:
    code = ord(ch)
    if 0x400 <= code <= 0x4FF:            # кириллица
        return "cyr"
    if ch.isalpha():                      # прочие буквы (латиница и т.д.)
        return "other"
    return "no"                           # эмодзи, знаки, цифры — не считаем


def _is_acceptable(phrase: str | None) -> bool:
    """False: есть иероглифы или кириллица < 70% от всех букв.

    Эмодзи, цифры и знаки препинания в долю букв не входят.
    """
    if not phrase:
        return False
    total_letters = 0
    cyr_letters = 0
    for ch in phrase:
        if _is_cjk(ch):
            return False
        kind = _char_kind(ch)
        if kind == "no":
            continue
        total_letters += 1
        if kind == "cyr":
            cyr_letters += 1
    if total_letters == 0:
        return False
    return cyr_letters / total_letters >= 0.7


class LLMClient:
    """Минимальный клиент чата. Наружу только str или None."""

    def __init__(self, ollama_url: str, model: str, timeout: float = 60.0):
        self.ollama_url = ollama_url.rstrip("/")
        self.model = model
        self.timeout = timeout

    # ——— запросы ———

    def chat(self, name: str, mood: str, params: dict,
             event: str | None) -> str | None:
        """Возвращает чистую фразу или None (ошибка/пустой ответ)."""
        try:
            payload = json.dumps({
                "model": self.model,
                "messages": [
                    {"role": "system", "content": build_system_prompt(name)},
                    {"role": "user", "content": build_user_message(
                        name, mood, params, event)},
                ],
                "temperature": 0.8,
                "max_tokens": 60,
                "stream": False,
            }).encode("utf-8")
            request = urllib.request.Request(
                self.ollama_url + "/v1/chat/completions",
                data=payload,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(request, timeout=self.timeout) as resp:
                data = json.loads(resp.read().decode("utf-8", "replace"))
            content = data["choices"][0]["message"]["content"]
            phrase = _clean_phrase(content)
            if phrase is not None and not _is_acceptable(phrase):
                return None
            return phrase
        except Exception:
            return None

    # ——— доступность ———

    def check_available(self) -> bool:
        """GET /api/tags (таймаут 2 с) + наличие модели в списке."""
        try:
            request = urllib.request.Request(
                self.ollama_url + "/api/tags", method="GET")
            with urllib.request.urlopen(request, timeout=CHECK_TIMEOUT) as resp:
                data = json.loads(resp.read().decode("utf-8", "replace"))
            names = {m.get("name") for m in data.get("models", [])}
            if self.model in names:
                return True
            base = self.model.split(":", 1)[0]
            return any(str(n).split(":", 1)[0] == base for n in names)
        except Exception:
            return False

    # ——— фолбэк ———

    def fallback_phrase(self, mood: str, event: str | None,
                        index: int = 0) -> str:
        return fallback.get_fallback(mood, event, index)


class PhraseScheduler:
    """Чистый планировщик: приоритеты, троттлинг, idle-интервал.

    Инъекция clock (монотонное время) — тестируется без реальных пауз.
    Решения по событиям:
      action  — принимается всегда (паузу отговорит воркер);
      mood    — отбрасывается, если и не idle-срок, и идёт пауза;
      idle    — принимается только по наступлению idle-срока.
    Отметка note_sent() вызывается координатором после фактической отправки.
    """

    def __init__(self, now=time.monotonic, idle_min: float = 60.0,
                 idle_max: float = 90.0, min_pause: float = 8.0,
                 rng: random.Random | None = None):
        self._now = now
        self.min_pause = min_pause
        self.idle_min = idle_min
        self.idle_max = idle_max
        self._rng = rng or random.Random()
        self.reset()

    def reset(self) -> None:
        self.last_sent_at: float | None = None
        self.next_idle_at = self._now() + self._next_idle_interval()

    def _next_idle_interval(self) -> float:
        return self._rng.uniform(self.idle_min, self.idle_max)

    def pause_left(self, now: float | None = None) -> float:
        now = self._now() if now is None else now
        if self.last_sent_at is None:
            return 0.0
        return max(0.0, self.min_pause - (now - self.last_sent_at))

    def idle_due(self, now: float | None = None) -> bool:
        now = self._now() if now is None else now
        return now >= self.next_idle_at

    def note_sent(self, now: float | None = None) -> None:
        now = self._now() if now is None else now
        self.last_sent_at = now
        self.next_idle_at = now + self._next_idle_interval()

    def decide(self, kind: int, now: float | None = None) -> tuple[bool, str | None]:
        """Возвращает (принять событие, причина отказа|None)."""
        now = self._now() if now is None else now
        due = self.idle_due(now)
        paused = self.pause_left(now) > 0
        if kind == PRIO_ACTION:
            return True, None
        if kind == PRIO_MOOD:
            if paused and not due:
                return False, "paused"
            return True, None
        if kind == PRIO_IDLE:
            if not due:
                return False, "not_due"
            if paused:
                return False, "paused"
            return True, None
        return False, "unknown"


class LLMWorker:
    """Один фоновый поток + queue.Queue. Результат — в self.results.

    Воркер сам выдерживает паузу между запросами и отбрасывает устаревшие
    события, оставляя только самое свежее. Логи в stderr: одна строка.
    """

    def __init__(self, client: LLMClient, pet_name: str,
                 min_pause: float = 8.0, poll: float = 0.2):
        self.client = client
        self.pet_name = pet_name
        self.min_pause = min_pause
        self._poll = poll
        self.requests: "queue.Queue" = queue.Queue()
        self.results: "queue.Queue" = queue.Queue()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._busy = False
        self._last_sent = 0.0

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run, name="llm-worker", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 3.0) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=timeout)

    def submit(self, kind: int, mood: str, params: dict,
               event: str | None) -> None:
        self.requests.put((kind, mood, dict(params), event))

    def is_busy(self) -> bool:
        return self._busy

    def _wait_free(self) -> bool:
        """Подождать паузу; вернёт False, если поступила команда стоп."""
        while not self._stop.is_set():
            left = self.min_pause - (time.monotonic() - self._last_sent)
            if left <= 0:
                return True
            self._stop.wait(min(left, 0.2))
        return False

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                item = self.requests.get(timeout=self._poll)
            except queue.Empty:
                continue
            # лишние запросы отбрасываем: берём самый свежий
            while True:
                try:
                    item = self.requests.get_nowait()
                except queue.Empty:
                    break
            if self._stop.is_set():
                break
            if not self._wait_free():
                break
            kind, mood, params, event = item
            self._busy = True
            started = time.monotonic()
            phrase = self.client.chat(self.pet_name, mood, params, event)
            elapsed = time.monotonic() - started
            self._busy = False
            self._last_sent = time.monotonic()
            ok = phrase is not None
            print(
                f"[llm] event={event or KIND_LABEL.get(kind, kind)} "
                f"{elapsed:.1f}s {'ok' if ok else 'fail'}",
                file=sys.stderr,
            )
            self.results.put((kind, mood, event, phrase, elapsed))
