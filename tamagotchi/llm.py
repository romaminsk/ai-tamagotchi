"""Клиент локальной Ollama (нативный POST /api/chat) + планировщик реплик.

Только stdlib, только localhost. Параметры запроса — ModelParams
(tamagotchi/modelsettings.py). Baseline воспроизводим дословным
старым промптом и старым набором options. Все ошибки -> None,
без стектрейсов.
"""

import dataclasses
import json
import queue
import random
import sys
import threading
import time
import urllib.request
from dataclasses import dataclass

from . import fallback
from .modelsettings import (ModelParams, OPTIMIZED, load_params)

MAX_PHRASE_LEN = 80          # short-лимит (как раньше)
LIMIT_BY_MODE = {"short": 80, "medium": 240, "long": 520}
CHECK_TIMEOUT = 2.0
_KEEP_ALIVE = "10m"
_DUMP_CHARS = "\"'` «»“”‘’ \t\r\n"

# Приоритеты триггеров (меньше = важнее)
PRIO_ACTION = 0
PRIO_MOOD = 1
PRIO_IDLE = 2
KIND_LABEL = {PRIO_ACTION: "action", PRIO_MOOD: "mood", PRIO_IDLE: "idle"}


# ——— baseline-промпт (дословно из старого кода, не менять) ———

def build_system_prompt(name: str) -> str:
    """Baseline system-промпт: дерзкий питомец, одна фраза, правила."""
    return (
        f"Ты — маленький питомец по имени {name}. "
        "У тебя чёрный юмор: ты саркастичный, дерзкий, любишь жёсткие "
        "приколы, самоиронию и абсурд. Каждый раз заходишь на фразу заново, "
        "придумывай новый ход. Отвечай ВСЕГДА только по-русски ОДНОЙ "
        "короткой фразой до 80 символов, от первого лица, без кавычек, "
        "эмодзи и пояснений. Пиши только по-русски, без иероглифов "
        "и английских слов. Без мата. Не оскорбляй людей и животных "
        "по национальности, религии, здоровью и любым другим признакам. "
        "Без призывов к насилию над людьми. Обижать позволено только "
        "себя, свою миску и ошейник. Примеры: «Ням-ням, вкусно!»; "
        "«Миска пуста, и я уже пишу жалобу»."
    )


STYLES = (
    "сарказм",
    "угроза расправы над миской",
    "философский мрак",
    "жалоба на жизнь",
    "ложная скромность",
    "пассивная агрессия",
    "театральная драма",
    "мания величия",
)


def build_user_message(name: str, mood: str, params: dict,
                       event: str | None, style: str | None = None,
                       seed: int | None = None,
                       recent: list[str] | None = None) -> str:
    """Baseline user-сообщение: настроение, параметры, событие, антиповтор."""
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
        "not_sleepy": "питомцу предложили спать, но он не хочет спать",
        "cleaned": "питомца только что помыли",
        "is_sleeping": "к питомцу обратились, но он спит",
        "not_sleeping": "питомца хотели разбудить, но он не спит",
        "idle": "ничего не происходит, питомец просто говорит",
        "greet": "питомец здоровается",
    }
    if event:
        parts.append(f"Событие: {events.get(event, event)}.")
    if style:
        parts.append(f"Стиль реплики: {style}.")
    if seed is not None:
        parts.append(f"Заход №{seed}.")
    if recent:
        joined = "; ".join(f"«{p}»" for p in recent)
        parts.append(f"Не повторяй эти фразы: {joined}.")
    parts.append("Скажи одну короткую фразу от первого лица.")
    return " ".join(parts)


BASELINE_PROMPT = {
    "system": build_system_prompt,
    "user": build_user_message,
}

# ——— optimized-промпт ———

_EVENTS = {
    "fed": "питомца только что покормили",
    "too_full": "питомца пытались накормить, но он уже сыт",
    "played": "с питомцем только что поиграли",
    "too_tired": "питомец слишком устал, чтобы играть",
    "sleep_started": "питомец лёг спать",
    "woke_up": "питомец проснулся",
    "not_sleepy": "питомцу предложили спать, но он не хочет спать",
    "cleaned": "питомца только что помыли",
    "is_sleeping": "к питомцу обратились, но он спит",
    "not_sleeping": "питомца хотели разбудить, но он не спит",
    "idle": "ничего не происходит, питомец просто говорит",
    "greet": "питомец здоровается",
}

_LENGTH_RULES = {
    "short": "ОДНОЙ фразой до 80 символов",
    "medium": "2–3 короткими предложениями до 220 символов суммарно",
    "long": ("4–6 предложениями, как мини-сюжет: ситуация — эскалация — "
             "абсурдная концовка, до 480 символов суммарно"),
}

_FEWSHOT = {
    "short": [
        "Миска пуста, я подала иск в суд на хозяина.",
        "Смузил хозяина взглядом и лёг на пульт, под делом важным.",
    ],
    "medium": [
        "Миска пуста. Я трижды просигналил об этом голодным взглядом. "
        "Теперь пишу жалобу и жду, пока хозяин найдёт менеджера.",
        "Помыли. Честь восстановлена, woolw动力学 стёрт. Теперь я чист и "
        "крайне опасен: планирую отомстить мочалке.",
    ],
    "long": [
        "Проснулся — в миске снова пусто. Решил действовать по уставу: "
        "сел у двери и уставился на хозяина взглядом зэка. Молчание затянулось"
        ", и я начал громко страдать в стиле театрального монолога. В итоге "
        "хозяин занервничал, покормил, а я записал это как свою победу.",
        "Хозяин достал пылесос. Репутация уничтожена, достоинство утеряно. "
        "Я объявил пылесосу войну и выждал удобный момент из-под дивана. "
        "Оказалось, что я сам застрял в проводе и теперь пылесос воюет с "
        "деталью, а я героически шепчу советы.",
    ],
}


def _fewshot_text(mode: str) -> str:
    shots = "\n".join(f"- {s}" for s in _FEWSHOT.get(mode, []))
    return f"Примеры реплик этой длины:\n{shots}"


def build_system_prompt_optimized(name: str, length_mode: str) -> str:
    """System-промпт optimized: чёрный юмор + правило выбранной длины."""
    rule = _LENGTH_RULES.get(length_mode, _LENGTH_RULES["short"])
    return (
        f"Ты — маленький питомец по имени {name}. "
        "У тебя чёрный юмор: ты саркастичный, дерзкий, любишь жёсткие "
        "приколы, самоиронию и абсурд. Каждый раз заходишь на фразу заново, "
        "придумывай новый ход. Отвечай ВСЕГДА только по-русски "
        f"{rule}, от первого лица, без кавычек, эмодзи и пояснений. "
        "Пиши только по-русски, без иероглифов и английских слов. "
        "Без мата. Не оскорбляй людей и животных по национальности, "
        "религии, здоровью и любым другим признакам. Без призывов к "
        "насилию над людьми. Обижать позволено только себя, свою миску "
        "и ошейник. Не вставляй упоминаний списка структуры."
        f"\n{_fewshot_text(length_mode)}."
        " Примеры: «Ням-ням, вкусно!»; «Миска пуста, и я уже пишу жалобу»."
    )


def build_user_message_optimized(name: str, mood: str, params: dict,
                                 event: str | None, style: str | None = None,
                                 seed: int | None = None,
                                 recent: list[str] | None = None,
                                 length_mode: str = "short") -> str:
    """User-сообщение optimized: Состояние, событие, стиль, антиповтор."""
    state = (
        f"Состояние: сытость={float(params.get('hunger', 0)):.0f}/100, "
        f"энергия={float(params.get('energy', 0)):.0f}/100, "
        f"чистота={float(params.get('hygiene', 0)):.0f}/100, "
        f"настроение={mood}, "
        f"событие={_EVENTS.get(event or '', 'нет')}."
    )
    parts = [f"Питомец {name}.", state]
    if style:
        parts.append(f"Стиль реплики: {style}.")
    if seed is not None:
        parts.append(f"Заход №{seed}.")
    if recent:
        joined = "; ".join(f"«{p}»" for p in recent)
        parts.append(f"Не повторяй эти фразы: {joined}.")
    rules = {
        "short": "Финал: скажи одну короткую фразу от первого лица.",
        "medium": "Финал: скажи 2–3 коротких предложения от первого лица.",
        "long": ("Финал: расскажи мини-сюжет из 4–6 предложений от первого "
                 "лица."),
    }
    parts.append(rules.get(length_mode, rules["short"]))
    return " ".join(parts)


def _clean_phrase(text, limit: int = MAX_PHRASE_LEN,
                  multiline: bool = False) -> str | None:
    """Первая непустая строка (или весь текст для multiline), чистка,
    обрезка до limit: по границе предложения, иначе по слову + «…»."""
    if not isinstance(text, str):
        return None
    if multiline:
        raw = " ".join(
            line.strip().strip(_DUMP_CHARS).strip()
            for line in text.splitlines()
        )
        raw = " ".join(raw.split())
        line = raw.strip().strip(_DUMP_CHARS).strip()
    else:
        line = None
        for candidate in text.splitlines():
            candidate = candidate.strip().strip(_DUMP_CHARS).strip()
            if candidate:
                line = candidate
                break
        if line is None:
            return None
    if len(line) <= limit:
        return line
    truncated = _truncate_by_sentence(line[:limit])
    return truncated


def _truncate_by_sentence(cut: str) -> str:
    """Обрезка cut: последняя . ! ? … в пределах; нет — по слову + «…»."""
    last = -1
    for i, ch in enumerate(cut):
        if ch in ".!?" or ch == "…":
            last = i
    if last >= 0:
        return cut[:last + 1]
    sp = cut.rfind(" ")
    if sp > 0:
        return cut[:sp].rstrip() + "…"
    return cut[:len(cut) - 1 if len(cut) > 1 else len(cut)] + "…"


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


def _reject_reason(phrase: str | None) -> str | None:
    """Причина отклонения: cjk | non_cyrillic | empty, иначе None."""
    if not phrase:
        return "empty"
    for ch in phrase:
        if _is_cjk(ch):
            return "cjk"
    if not _is_acceptable(phrase):
        return "non_cyrillic"
    return None


@dataclass
class ReplyMetrics:
    """Метрики одного ответа Ollama (поля как в /api/chat, наносекунды)."""
    tokens_per_sec: float | None = None
    eval_count: int | None = None
    eval_duration: int | None = None
    prompt_eval_count: int | None = None
    prompt_eval_duration: int | None = None
    load_duration: int | None = None
    total_duration: int | None = None


@dataclass
class Reply:
    """Ответ модели: чистый текст (или None), метрики, причина отклонения."""
    text: str | None
    metrics: ReplyMetrics | None
    rejected_reason: str | None  # cjk|non_cyrillic|repeat|empty|unavailable


class LastMetrics:
    """Потокобезопасный держатель последних метрик (для UI)."""

    def __init__(self):
        self._lock = threading.Lock()
        self._metrics: ReplyMetrics | None = None

    def set(self, metrics: ReplyMetrics | None) -> None:
        with self._lock:
            self._metrics = metrics

    def get(self) -> ReplyMetrics | None:
        with self._lock:
            return self._metrics


LAST_METRICS = LastMetrics()


def parse_metrics(data: dict) -> ReplyMetrics:
    """Парсинг метрик /api/chat; tokens_per_sec=None если нет данных."""
    def num(key):
        value = data.get(key)
        try:
            value = int(value)
        except (TypeError, ValueError):
            return None
        return value if value and value > 0 else None

    eval_count = num("eval_count")
    eval_duration = num("eval_duration")
    tps = None
    if eval_count and eval_duration:
        tps = eval_count / (eval_duration / 1e9)
    return ReplyMetrics(
        tokens_per_sec=tps,
        eval_count=eval_count,
        eval_duration=eval_duration,
        prompt_eval_count=num("prompt_eval_count"),
        prompt_eval_duration=num("prompt_eval_duration"),
        load_duration=num("load_duration"),
        total_duration=num("total_duration"),
    )


class LLMClient:
    """Минимальный клиент чата на /api/chat. Наружу Reply (или .text)."""

    RECENT_LIMIT = 5

    def __init__(self, ollama_url: str, model: str, timeout: float = 60.0,
                 rng: random.Random | None = None,
                 params: ModelParams | None = None):
        self.ollama_url = ollama_url.rstrip("/")
        self.model = model
        self.timeout = timeout
        self._rng = rng or random.Random()
        self.recent: list[str] = []  # последние 5 принятых реплик
        self.params = params or load_params()

    # ——— запросы ———

    def _pick_style(self) -> str:
        return self._rng.choice(STYLES)

    def _remember(self, phrase: str) -> None:
        self.recent.append(phrase)
        if len(self.recent) > self.RECENT_LIMIT:
            del self.recent[:len(self.recent) - self.RECENT_LIMIT]

    def _options(self) -> dict:
        p = self.params
        if p.prompt_variant == "baseline":
            # Дословный набор старого кода (top_k/repeat_penalty не задавались)
            return {
                "temperature": p.temperature,
                "top_p": p.top_p,
                "num_predict": p.max_tokens,
                "num_ctx": p.num_ctx,
            }
        return {
            "temperature": p.temperature,
            "top_p": p.top_p,
            "top_k": p.top_k,
            "repeat_penalty": p.repeat_penalty,
            "num_predict": p.max_tokens,
            "num_ctx": p.num_ctx,
        }

    def chat(self, name: str, mood: str, params: dict,
             event: str | None,
             length_mode: str | None = None) -> Reply:
        """Один запрос к Ollama -> Reply (text=None при ошибке/отклонении)."""
        p = self.params
        if length_mode and length_mode != self.params.length_mode:
            p = dataclasses.replace(self.params)  # копия, без мутации настроек
            p.length_mode = length_mode
        try:
            style = self._pick_style()
            seed = self._rng.randint(1, 99999)
            if p.prompt_variant == "baseline":
                user_message = build_user_message(
                    name, mood, params, event, style=style, seed=seed,
                    recent=list(self.recent))
            else:
                user_message = build_user_message_optimized(
                    name, mood, params, event, style=style, seed=seed,
                    recent=list(self.recent), length_mode=p.length_mode)
            if p.prompt_variant == "baseline":
                system_content = build_system_prompt(name)
            else:
                system_content = build_system_prompt_optimized(
                    name, p.length_mode)
            payload = json.dumps({
                "model": p.model or self.model,
                "messages": [
                    {"role": "system", "content": system_content},
                    {"role": "user", "content": user_message},
                ],
                "stream": False,
                "keep_alive": _KEEP_ALIVE,
                "options": self._options(),
            }).encode("utf-8")
            request = urllib.request.Request(
                self.ollama_url + "/api/chat",
                data=payload,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(request, timeout=self.timeout) as resp:
                data = json.loads(resp.read().decode("utf-8", "replace"))
            content = (data.get("message") or {}).get("content")
            metrics = parse_metrics(data)
            LAST_METRICS.set(metrics)
            reply = self._finalize(content, metrics, p)
            return reply
        except Exception:
            LAST_METRICS.set(None)
            return Reply(None, None, "unavailable")

    def _finalize(self, content, metrics, p: ModelParams) -> Reply:
        multiline = p.length_mode != "short"
        phrase = _clean_phrase(content, LIMIT_BY_MODE.get(
            p.length_mode, MAX_PHRASE_LEN), multiline=multiline)
        if phrase is None:
            return Reply(None, metrics, "empty")
        reason = _reject_reason(phrase)
        if reason:
            return Reply(None, metrics, reason)
        if phrase in self.recent:  # антиповтор: непроходной
            return Reply(None, metrics, "repeat")
        self._remember(phrase)
        return Reply(phrase, metrics, None)

    # ——— доступность ———

    def check_available(self) -> bool:
        """GET /api/tags (таймаут 2 с) + наличие модели в списке."""
        names = self.list_models()
        if names is None:
            return False
        if self.params.model in names or self.model in names:
            return True
        base = (self.params.model or self.model).split(":", 1)[0]
        return any(str(n).split(":", 1)[0] == base for n in names)

    def list_models(self) -> list[str] | None:
        """Список моделей /api/tags; None если сервер недоступен."""
        try:
            request = urllib.request.Request(
                self.ollama_url + "/api/tags", method="GET")
            with urllib.request.urlopen(request, timeout=CHECK_TIMEOUT) as resp:
                data = json.loads(resp.read().decode("utf-8", "replace"))
            return [str(m.get("name")) for m in data.get("models", [])]
        except Exception:
            return None

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
            reply = self.client.chat(self.pet_name, mood, params, event)
            phrase = reply.text
            elapsed = time.monotonic() - started
            self._busy = False
            self._last_sent = time.monotonic()
            ok = phrase is not None
            print(
                f"[llm] event={event or KIND_LABEL.get(kind, kind)} "
                f"{elapsed:.1f}s {'ok' if ok else 'fail:' + (reply.rejected_reason or '?')}",
                file=sys.stderr,
            )
            self.results.put((kind, mood, event, phrase, elapsed))
