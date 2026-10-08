"""Чистые функции бенчмарка: p95, доли, правило выбора OPTIMIZED.
"""

def pct_95(values: list[float]) -> float | None:
    """p95 линейной интерполяцией; при <20 точек — приблизительный."""
    if not values:
        return None
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    rank = 0.95 * (len(ordered) - 1)
    lo = int(rank)
    hi = min(lo + 1, len(ordered) - 1)
    frac = rank - lo
    return ordered[lo] * (1 - frac) + ordered[hi] * frac


def p95_is_approx(n: int) -> bool:
    """True, если p95 меньше 20 точек (приближение)."""
    return n < 20


def avg(values: list[float]) -> float | None:
    return sum(values) / len(values) if values else None


def share_unique(phrases: list[str]) -> float | None:
    return len(set(phrases)) / len(phrases) if phrases else None


def share_cyrillic(phrase: str) -> float:
    letters = 0
    cyr = 0
    for ch in phrase:
        if 0x400 <= ord(ch) <= 0x4FF:
            cyr += 1
            letters += 1
        elif ch.isalpha():
            letters += 1
    return cyr / letters if letters else 0.0


def parse_ps_line(line: str) -> int | None:
    """RSS в КБ из строки `ps -axo rss,comm` для процесса с ollama."""
    parts = line.split(None, 1)
    if len(parts) != 2:
        return None
    if "ollama" not in parts[1].lower():
        return None
    try:
        return int(parts[0])
    except ValueError:
        return None


# ——— правило выбора OPTIMIZED (v2) ———

ACCEPT_MIN = 0.90
TIME_LIMIT_S = 15.0           # абсолютный лимит среднего времени
TIE_WINDOW = 0.10


def choose_optimized(candidates: dict[str, dict]) -> tuple[str | None, str]:
    """Правило v3: среди кандидатов с accepted_share >= 90% и
    avg_time <= 15 с выбрать максимум chars/sec (симв./с =
    длина принятых / время); при равенстве в пределах 10% —
    более быстрая. Возвращает (имя, объяснение).
    """
    passed = []
    for name, s in sorted(candidates.items()):
        if (s.get("accepted_share", 0.0) >= ACCEPT_MIN
                and s.get("avg_time") is not None
                and s["avg_time"] <= TIME_LIMIT_S):
            passed.append(name)
    if not passed:
        return None, (f"ни одна не проходит (accepted>={ACCEPT_MIN:.0%},"
                      f" время<={TIME_LIMIT_S} с) — порог не выполнен")
    cps = {n: (candidates[n]["avg_len"] or 0.0) / candidates[n]["avg_time"]
           for n in passed}
    best_cps = max(cps.values())
    close = [n for n in passed if cps[n] >= best_cps * (1 - TIE_WINDOW)]
    if len(close) == 1:
        return close[0], (f"пороги прошли: {','.join(passed)};"
                          f" лучший chars/sec у {close[0]} "
                          f"({cps[close[0]]:.1f} симв./с)")
    fastest = min(close, key=lambda n: candidates[n]["avg_time"])
    return fastest, (f"пороги прошли: {','.join(passed)}; chars/sec"
                     f" в пределах {TIE_WINDOW:.0%}; тай-брейк по скорости:"
                     f" {fastest} ({candidates[fastest]['avg_time']:.2f} с)")


# ——— раннер бенчмарка (вызывается через python -m tamagotchi --bench) ———

import dataclasses as _dc
import itertools as _itertools
import json as _json
import os as _os
import subprocess as _subprocess
import threading as _threading
import time as _time

TOTAL_DEADLINE_S = 2400.0       # 40 минут на весь бенчмарк
PULL_TIMEOUT_CAP_S = 600.0      # таймаут ollama pull, сек
PULL_RESULT_BUFFER_S = 240.0    # запас времени после pull на прогоны
RSS_SAMPLE_S = 0.5

SCENARIOS = (
    ("fed", "fed"),
    ("play", "played"),
    ("clean", "cleaned"),
    ("wake", "woke_up"),
    ("hungry", "idle"),
    ("sad", "idle"),
)

SCENARIO_PARAMS = {
    "fed": {"hunger": 40, "energy": 80, "fun": 70, "hygiene": 80},
    "play": {"hunger": 70, "energy": 80, "fun": 60, "hygiene": 80},
    "clean": {"hunger": 80, "energy": 80, "fun": 70, "hygiene": 20},
    "wake": {"hunger": 60, "energy": 70, "fun": 60, "hygiene": 80},
    "hungry": {"hunger": 10, "energy": 50, "fun": 20, "hygiene": 50},
    "sad": {"hunger": 40, "energy": 40, "fun": 10, "hygiene": 60},
}


def build_configs() -> dict:
    from .modelsettings import BASELINE, OPTIMIZED
    a = _dc.replace(BASELINE)
    b = _dc.replace(BASELINE, prompt_variant="optimized", length_mode="long")
    c = _dc.replace(OPTIMIZED)
    return {
        "A": a,
        "B": b,
        "C": c,
        "M-medium": _dc.replace(c, length_mode="medium", max_tokens=160),
        "D-q8_0": _dc.replace(c, model="qwen2.5:3b-instruct-q8_0"),
        "E-2048": _dc.replace(c, num_ctx=2048),
        "E-8192": _dc.replace(c, num_ctx=8192),
    }


class RssSampler:
    """Фоновый семплер суммарного RSS всех процессов ollama (ps, 0.5 с)."""

    def __init__(self):
        self.max_rss_kb = 0
        self.samples = 0
        self._stop = _threading.Event()
        self._thread = None

    def start(self):
        self._thread = _threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2.0)

    def take(self) -> int:
        """Максимум RSS с прошлого вызова take (и сброс счётчика)."""
        value = self.max_rss_kb
        self.max_rss_kb = 0
        return value

    def _loop(self):
        while not self._stop.is_set():
            try:
                out = _subprocess.check_output(
                    ["ps", "-axo", "rss,comm"], text=True)
                total = 0
                for line in out.splitlines():
                    rss = parse_ps_line(line)
                    if rss:
                        total += rss
                if total > self.max_rss_kb:
                    self.max_rss_kb = total
                self.samples += 1
            except _subprocess.SubprocessError:
                pass
            self._stop.wait(RSS_SAMPLE_S)


def run_cmd(cmd: list[str], timeout: float = 30.0) -> str:
    try:
        out = _subprocess.run(cmd, capture_output=True, text=True,
                              timeout=timeout)
        return (out.stdout or "") + (out.stderr or "")
    except (_subprocess.SubprocessError, OSError):
        return ""


def pull_model(tag: str, timeout: float) -> dict:
    """ollama pull с таймаутом; текст вывода фиксируется в raw."""
    if not timeout or timeout < 60:
        return {"ok": False, "reason": "не хватило бюджета времени",
                "output": ""}
    try:
        out = _subprocess.run(["ollama", "pull", tag], capture_output=True,
                              text=True, timeout=timeout)
        text = (out.stdout or "") + (out.stderr or "")
        rows = run_cmd(["ollama", "list"])
        present = any(line.split()[0] == tag
                      for line in rows.splitlines()[1:] if line.split())
        return ({"ok": True, "reason": "", "output": text[-500:]}
                if present else
                {"ok": False, "reason": "pull завершился, тег не в ollama list",
                 "output": text[-500:]})
    except _subprocess.TimeoutExpired as e:
        return {"ok": False,
                "reason": f"pull превысил таймаут {timeout:.0f} с",
                "output": (str(e)[-500:] if e else "")}


def stop_model(tag: str) -> str:
    """ollama stop (выгрузка из памяти) — вывод фиксируется в raw."""
    return run_cmd(["ollama", "stop", tag], timeout=30.0)


def ollama_ps(tag: str) -> dict:
    """Строка «SIZE» и «PROCESSOR» для модели из ollama ps.

    Колонки: NAME ID SIZE(2 поля) PROCESSOR CONTEXT UNTIL.
    """
    out = run_cmd(["ollama", "ps"])
    for line in out.splitlines()[1:]:
        parts = line.split()
        if parts and parts[0] == tag:
            if len(parts) < 6:
                return {}
            return {"name": parts[0], "SIZE": f"{parts[2]} {parts[3]}",
                    "PROCESSOR": f"{parts[4]} {parts[5]}",
                    "CONTEXT": parts[6] if len(parts) > 6 else ""}
    return {}


def ollama_list_size(tag: str) -> str | None:
    """Размер файла модели из ollama list (SIZE = «X GB»)."""
    out = run_cmd(["ollama", "list"])
    for line in out.splitlines()[1:]:
        parts = line.split()
        if parts and parts[0] == tag and len(parts) >= 4:
            return f"{parts[2]} {parts[3]}"
    return None


def ollama_version() -> str:
    return run_cmd(["ollama", "--version"]).strip() or ""


def _pet_mood(scenario_id: str) -> tuple[str, dict]:
    """Параметры и настроение сценария (через Pet, код держит правила)."""
    from .pet import Pet
    pet = Pet("Пушок")
    params = SCENARIO_PARAMS[scenario_id]
    for key, value in params.items():
        setattr(pet, key, float(value))
    mood = pet.mood()
    if scenario_id == "sad":
        mood = "sad"
    if scenario_id == "hungry":
        mood = "hungry"
    return mood, params


def warmup_query(client) -> dict:
    """Один прогрев-запрос конфигурации (не в статистике)."""
    mood, params = _pet_mood("fed")
    started = _time.perf_counter()
    reply = client.chat("Пушок", mood, params, "fed")
    return {"time": round(_time.perf_counter() - started, 3),
            "text": reply.text,
            "rejected_reason": reply.rejected_reason,
            "load_duration": reply.metrics.load_duration,
            "eval_count": reply.metrics.eval_count,
            "eval_duration": reply.metrics.eval_duration,
            "total_duration": reply.metrics.total_duration}


def summarize(samples: list[dict]) -> dict:
    """Статистика конфигурации из сырых сэмплов."""
    accepted = [s for s in samples if s["rejected_reason"] is None]
    rejected = [s for s in samples if s["rejected_reason"] is not None]
    times = [s["time"] for s in samples]
    lens = [len(s["text"]) for s in accepted]
    reasons: dict[str, int] = {}
    for s in rejected:
        key = s["rejected_reason"] or "?"
        reasons[key] = reasons.get(key, 0) + 1
    tps = [s["eval_count"] / (s["eval_duration"] / 1e9)
           for s in accepted if s["eval_count"] and s["eval_duration"]]
    loads = [s["load_duration"] for s in samples if s.get("load_duration")]
    texts = [s["text"] for s in accepted] if accepted else []
    return {
        "n": len(samples),
        "accepted": len(accepted),
        "accepted_share": len(accepted) / len(samples) if samples else 0.0,
        "rejected_by_reason": reasons,
        "avg_time": avg(times),
        "p95_time": pct_95(times),
        "p95_approx": p95_is_approx(len(times)),
        "tokens_per_sec_avg": avg(tps),
        "avg_len": avg(lens),
        "unique_share": share_unique(texts),
        "cyr_share_avg": avg([share_cyrillic(t) for t in texts]),
        "eval_count_avg": avg([s["eval_count"] for s in accepted
                               if s["eval_count"]]),
        "prompt_eval_avg": avg([s["prompt_eval_count"] for s in samples
                                if s["prompt_eval_count"]]),
        "load_duration_avg_s": avg(
            [l / 1e9 for l in loads]),
        "load_duration_max_s": max(loads) / 1e9 if loads else None,
    }


def run_bench(ollama_url: str, timeout_per_query: float = 120.0) -> dict:
    """Перемер: A, B, C, M-medium, E-2048, E-8192, D-q8_0.

    Пуллы — отдельная фаза до замеров; перед стартом все модели остановлены
    (чистые условия); порядок перемешан Random(42) целиком; 2 прогона
    на сценарий; первый warmup конфигурации заходит холодным стартом.
    """
    import random
    from .llm import LLMClient
    started = _time.perf_counter()
    configs = build_configs()
    names = sorted(configs)
    rng = random.Random(42)
    rng.shuffle(names)
    raw: dict = {
        "date": _time.strftime("%Y-%m-%dT%H:%M:%S"),
        "ollama_version": ollama_version(),
        "config_order": list(names),
        "runs": 2,
        "scenarios_per_run": len(SCENARIOS),
        "est_per_query_s": {"A": 3.0, "B": 8.0, "C": 27.0,
                            "M-medium": 20.0, "E-2048": 25.0,
                            "E-8192": 30.0, "D-q8_0": 30.0},
        "est_note": "оценки для планирования бюджета (по замерам прежних"
                    " прогонов), не входят в результаты",
        "total_deadline_s": TOTAL_DEADLINE_S,
        "configs": {},
        "cold_start": None,
        "notes": [],
    }
    print("Порядок конфигураций (Random(42)):", names)
    # чистые условия: остановить все загруженные модели
    stopped = []
    out = run_cmd(["ollama", "ps"])
    for line in out.splitlines()[1:]:
        parts = line.split()
        if parts:
            stopped.append(parts[0])
    for model in stopped:
        raw["notes"].append(f"ollama stop {model} перед стартом")
        stop_model(model)
    sampler = RssSampler()
    sampler.start()
    runs = 2
    cold_done = False
    for name in list(names):
        remaining = TOTAL_DEADLINE_S - (_time.perf_counter() - started)
        cfg = configs[name]
        raw["configs"].setdefault(name, {})
        raw["configs"][name]["params"] = _dc.asdict(cfg)
        raw["configs"][name]["model_file_size"] = ollama_list_size(cfg.model)
        est = raw["est_per_query_s"].get(name, 20.0)
        if TOTAL_DEADLINE_S - (_time.perf_counter() - started) \
                < runs * len(SCENARIOS) * est + 90:
            raw["configs"][name]["skipped"] = (
                "не уложилась: "
                f"осталось {remaining:.0f} с, планировалось "
                f"{runs * len(SCENARIOS) * est:.0f} с (лимит 40 мин)")
            raw["configs"][name]["samples"] = []
            continue
        print(f"[bench] конфигурация {name} ({cfg.model}, runs={runs}),"
              f" осталось {remaining:.0f} с")
        client = LLMClient(ollama_url, cfg.model, timeout_per_query,
                           params=_dc.replace(cfg))
        warm = warmup_query(client)
        if not cold_done:
            cold_done = True
            raw["cold_start"] = {"config": name, "model": cfg.model,
                                 "query": warm}
            print(f"[bench] холодный старт {name}: load="
                  f"{(warm['load_duration'] or 0) / 1e9:.1f}с"
                  f", total={warm['time']:.1f}с")
        data = {"samples": [], "warm": warm}
        perf_counter = _time.perf_counter
        for run_index in range(runs):
            for scenario_id, event in SCENARIOS:
                mood, params = _pet_mood(scenario_id)
                s0 = perf_counter()
                reply = client.chat("Пушок", mood, params, event)
                elapsed = perf_counter() - s0
                data["samples"].append({
                    "run": run_index, "scenario": scenario_id,
                    "event": event, "time": round(elapsed, 3),
                    "text": reply.text,
                    "rejected_reason": reply.rejected_reason,
                    "eval_count": reply.metrics.eval_count,
                    "eval_duration": reply.metrics.eval_duration,
                    "prompt_eval_count": reply.metrics.prompt_eval_count,
                    "prompt_eval_duration": reply.metrics
                    .prompt_eval_duration,
                    "load_duration": reply.metrics.load_duration,
                    "total_duration": reply.metrics.total_duration,
                })
        raw["configs"][name]["samples"] = data["samples"]
        raw["configs"][name]["summary"] = summarize(data["samples"])
        raw["configs"][name]["ollama_ps"] = ollama_ps(cfg.model)
        raw["configs"][name]["max_rss_kb"] = sampler.take()
    elapsed = _time.perf_counter() - started
    sampler.stop()
    raw["bench_elapsed_s"] = round(elapsed, 1)
    raw["max_rss_kb"] = sampler.max_rss_kb
    print(f"[bench] замеры завершены за {elapsed:.0f} с")
    return raw


def apply_rule(raw: dict) -> tuple[str | None, str]:
    """Применяет правило v2 к сырым данным бенчмарка."""
    candidates = {}
    for name, cfg in sorted((raw.get("configs", {}) or {}).items()):
        if cfg.get("skipped"):
            continue
        if cfg.get("rule_candidate") is False:
            continue  # стравнение-база (A) не кандидат
        if cfg.get("reused"):
            src = (raw.get("configs", {}).get(cfg["reused"], {}) or {}) \
                .get("summary")
            if src:
                candidates[name] = src
            continue
        if cfg.get("summary") and cfg.get("summary").get("accepted_share") \
                is not None:
            candidates[name] = cfg["summary"]
    return choose_optimized(candidates)


def _fmt(v, digits=2, suffix="", dash="—"):
    return dash if v is None else f"{v:.{digits}f}{suffix}"


def write_raw(raw: dict, path: str = "docs/bench-raw.json") -> str:
    _os.makedirs(_os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        _json.dump(raw, f, ensure_ascii=False, indent=1)
    return path


REPORT_PHRASES_PER_CONFIG = 3


def quality_examples(raw: dict, per_config: int = REPORT_PHRASES_PER_CONFIG
                     ) -> dict[str, list[str]]:
    """По per_config дословных реплик на конфигурацию (random seed 42)."""
    import random
    result: dict[str, list[str]] = {}
    for name, cfg in sorted(raw.get("configs", {}).items()):
        texts = [s["text"] for s in cfg.get("samples", [])
                 if s.get("text")]
        if not texts and cfg.get("reused"):
            texts = [s["text"]
                     for s in raw["configs"].get(cfg["reused"], {})
                     .get("samples", []) if s.get("text")]
        rng = random.Random(42)
        picks = rng.sample(texts, min(per_config, len(texts))) \
            if texts else []
        result[name] = picks
    return result


def write_report(raw: dict, path: str = "docs/optimization-report.md",
                 chosen: dict | None = None) -> str:
    """Формирует русский отчёт только из сырых данных raw."""
    per_config_ex = len(next(iter((raw.get('quality_examples') or {'x': []}).values()), [])) or REPORT_PHRASES_PER_CONFIG
    _os.makedirs(_os.path.dirname(path), exist_ok=True)
    lines: list[str] = []
    ap = lines.append
    ap("# Оптимизация локальной LLM (ai-tamagotchi)")
    ap("")
    ap(f"Дата: {raw.get('date')} · Ollama: {raw.get('ollama_version')}")
    ap("")
    ap("## Цель")
    ap("")
    ap("Подобрать параметры и промпт для qwen2.5-3B так, чтобы реплики"
       " питомца могли быть объёмными без потери скорости; честно сравнить"
       " baseline и оптимизации и вынести управление параметрами в UI.")
    ap("")
    ap("## Методика")
    ap("")
    ap("- 6 фиксированных сценариев: fed, play, clean, wake,"
       " настроение «голодный», настроение «грустный».")
    ap("- Прогрев: 1 запрос на конфигурацию (не в статистике)."
       " Холодный старт: один раз `ollama stop` и первый запрос"
       " (load_duration отдельно).")
    order_txt = ""
    if raw.get("config_order_full_shuffle"):
        order_txt = (f"полный shuffle: "
                     f"{', '.join(raw['config_order_full_shuffle'])}; ")
    order_txt += (f"фактический порядок запуска: "
                  f"{', '.join(raw.get('config_order', []))}")
    ap(f"- Прогонов: {raw.get('runs_summary', 'см. raw')};"
       f" порядок конфигураций перемешан и зафиксирован"
       f" `random.Random(42)` ({order_txt}).")
    ap("- Автометрики качества — прокси: не заменяют человеческую оценку.")
    ap(raw.get("single_run_note",
               "Все замеры — один запуск бенчмарка (один прогон целиком),"
               " если не помечено иное (`source_run`)."))
    ap("")
    ap ("## Таблица «до/после»")
    ap("")
    ap("| Конфиг | Среднее,с | p95,с | ток/с | Символ. | с/100 симв. |"
       " симв./с | Принято | Откл. | Model | SIZE | PROCESSOR |"
       " maxRSS,МБ |")
    ap("|---|---|---|---|---|---|---|---|---|---|---|---|---|")
    for name in sorted(raw.get("configs", {})):
        cfg = raw.get("configs", {}).get(name, {})
        if cfg.get("reused"):
            ap(f"| {name} | переиспользует {cfg['reused']} | | | | | | |"
               " | | | | |")
            continue
        if cfg.get("skipped"):
            ap(f"| {name} | не уложилась: {cfg['skipped']} |"
               " | | | | | | | | | |")
            continue
        s = cfg.get("summary") or {}
        ps = cfg.get("ollama_ps") or {}
        rej = s.get("rejected_by_reason") or {}
        rej_txt = (", ".join(f"{k}:{v}" for k, v in sorted(rej.items()))
                   or "—")
        rss_mb = cfg.get("max_rss_kb", 0) / 1024.0
        model = (cfg.get("params", {}) or {}).get("model", "")
        avg_t = s.get("avg_time")
        avg_len = s.get("avg_len")
        per100 = (avg_t / avg_len * 100) if avg_t and avg_len else None
        cps = (avg_len / avg_t) if avg_t and avg_len else None
        source = f" ({cfg['source_run']})" if cfg.get("source_run") else ""
        ap(f"| {name}{source} | {_fmt(avg_t)} | "
           f"{_fmt(s.get('p95_time'))}"
           f"{'*' if s.get('p95_approx') else ''} | "
           f"{_fmt(s.get('tokens_per_sec_avg'), 1)} | "
           f"{_fmt(avg_len, 0)} | {_fmt(per100)} | {_fmt(cps, 1)} | "
           f"{_fmt(100 * float(s.get('accepted_share') or 0), 0)}% | "
           f"{rej_txt} | {model} | {ps.get('SIZE','—')} | "
           f"{ps.get('PROCESSOR','—')} | {rss_mb:.0f} |")
    ap("")
    ap("`*` — p95 приблизительный: меньше 20 точек.")
    ap("")
    cs = raw.get("cold_start") or {}
    cq = cs.get("query") or {}
    ap(f"Холодный старт (`{cs.get('model')}`, приблизительно: один замер):"
       f" load_duration = {(cq.get('load_duration') or 0)/1e9:.1f} с,"
       f" полный запрос = {(cq.get('time') or 0):.1f} с"
       f" (в статистику не входит).")
    ap("")
    ap("Качество ответов: см. файл `docs/bench-raw.json` (полные реплики).")
    ap("")
    ap("## Примеры ответов (дословно, случайный выбор seed 42)")
    ap("")
    for name, picks in sorted(
            (raw.get("quality_examples") or {}).items()):
        ap(f"### {name}")
        ap("")
        if picks:
            for i, text in enumerate(picks, 1):
                ap(f"{i}. «{text}»")
        else:
            ap("(нет принятых ответов)")
        ap("")
    ap("## Таблица ручной оценки (заполняется человеком, 1–5)")
    ap("")
    ap("| Конфиг | Реплика | Смешно | Связно | Грамматика |")
    ap("|---|---|---|---|---|")
    for name, picks in sorted(
            (raw.get("quality_examples") or {}).items()):
        for i in range(1, per_config_ex + 1):
            ap(f"| {name} | {min(i, len(picks)) if picks else '—'}"
               " | | | |")
    ap("")
    ap("## Правило выбора OPTIMIZED")
    ch = raw.get("choose_optimized") or {}
    ap(f"Итог: `{ch.get('name') or 'не выбран'}`")
    ap(f"Обоснование: {ch.get('why') or '—'}")
    if raw.get("rule_change_note"):
        ap("")
        ap(f"Примечание: {raw['rule_change_note']}")
    chosen = chosen or {}
    if chosen:
        ap("Записанный пресет OPTIMIZED (modelsettings.py): "
           + ", ".join(f"{k}={v}" for k, v in chosen.items()))
    ap("")
    ap("Режим long доступен кнопкой «Длина ⟳» в панели «Модель»;"
       " при лимите 60 токенов длинная реплика обрезается по лимиту"
       " (конфигурация B: в среднем 155 символов при 60 ток).")
    ap("")
    ap("## Ограничения")
    ap("")
    ap("- Одна модель семейства 3B на одном компьютере; малая выборка"
       " (12–18 ответов на конфигурацию).")
    ap("- Автометрики (принимаемость, уникальность, доля кириллицы) —"
       " только прокси качества.")
    ap("- Пиковый RSS — суммарный по всем процессам ollama на машине"
       " во время запросов (ps, шаг 0,5 с).")
    ap("")
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    return path
