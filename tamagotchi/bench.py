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


# ——— правило выбора OPTIMIZED ———

ACCEPT_MIN = 0.90
TIME_FACTOR = 2.5
TIE_WINDOW = 0.10


def choose_optimized(a_stats: dict, candidates: dict[str, dict]
                     ) -> tuple[str | None, str]:
    """Правило: среди B/C/D/E с accepted >= 90% и avg_time <= 2.5x A
    выбрать больше «средняя длина принятых реплик»; при равенстве
    (в пределах 10%) — быстрее. Возвращает (имя, объяснение).

    Статистика конфигурации:
      accepted_share (0..1), avg_time (сек), avg_len (символы принятых).
    """
    if not a_stats.get("avg_time"):
        return None, "нет baseline-времени (A), правило не применено"
    limit = a_stats["avg_time"] * TIME_FACTOR
    passed = []
    for name, s in sorted(candidates.items()):
        if (s.get("accepted_share", 0.0) >= ACCEPT_MIN
                and s.get("avg_time") is not None
                and s["avg_time"] <= limit):
            passed.append(name)
    if not passed:
        return None, ("ни одна из B/C/D/E не проходит (accepted>=90%, "
                      f"время<={TIME_FACTOR}x A) — порог не выполнен")
    best_len = max(candidates[n]["avg_len"] or 0.0 for n in passed)
    close = [n for n in passed
             if candidates[n]["avg_len"]
             >= best_len * (1 - TIE_WINDOW)]
    if len(close) == 1:
        return close[0], ("пороги прошли " + ",".join(passed)
                          + "; самая длинная реплика у %s" % close[0])
    fastest = min(close, key=lambda n: candidates[n]["avg_time"])
    return fastest, ("несколько прошли по порогам; длины в пределах 10%%; "
                     "взят самый быстрый из близких по длине: %s"
                     % fastest)


# ——— раннер бенчмарка (вызывается через python -m tamagotchi --bench) ———

import dataclasses as _dc
import itertools as _itertools
import json as _json
import os as _os
import subprocess as _subprocess
import threading as _threading
import time as _time

TOTAL_DEADLINE_S = 900.0        # 15 минут на весь бенчмарк
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
        "D-q8_0": _dc.replace(c, model="qwen2.5:3b-instruct-q8_0"),
        "D-q2_K": _dc.replace(c, model="qwen2.5:3b-instruct-q2_K"),
        "E-2048": _dc.replace(c, num_ctx=2048),
        "E-4096": c,   # идентична C — данные переиспользуются
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


def collect_config(client: object, runs: int) -> dict:
    """1 прогрев + runs×6 сценариев; возвращает сырые данные конфигурации."""
    perf_counter = _time.perf_counter
    warm = warmup_query(client)
    samples = []
    for run_index in range(runs):
        for scenario_id, event in SCENARIOS:
            mood, params = _pet_mood(scenario_id)
            started = perf_counter()
            reply = client.chat("Пушок", mood, params, event)
            elapsed = perf_counter() - started
            sample = {
                "run": run_index,
                "scenario": scenario_id,
                "event": event,
                "time": round(elapsed, 3),
                "text": reply.text,
                "rejected_reason": reply.rejected_reason,
                "eval_count": reply.metrics.eval_count,
                "eval_duration": reply.metrics.eval_duration,
                "prompt_eval_count": reply.metrics.prompt_eval_count,
                "prompt_eval_duration": reply.metrics.prompt_eval_duration,
                "load_duration": reply.metrics.load_duration,
                "total_duration": reply.metrics.total_duration,
            }
            samples.append(sample)
    return {"warm": warm, "samples": samples}


def _is_cold_warmup(client, name: str) -> dict:
    """Один прогрев-запрос конфигурации (не в статистике)"""
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


def run_bench(ollama_url: str, timeout_per_query: float = 120.0
              ) -> dict:
    """Полный бенчмарк: конфигурации A-E, raw + сводка, без записи файлов."""
    import random
    from .llm import LLMClient
    started = _time.perf_counter()
    configs = build_configs()
    names = sorted(configs)
    rng = random.Random(42)
    rng.shuffle(names)
    raw_order_full = list(names)
    # отклонение от промта (зафиксировано): A и B идут первыми — они
    # дёшевы и дают базу правилу; пуллы D — в конце (съедают бюджет).
    # Воспроизводимость сохраняется: оба порядка в raw.
    others = [n for n in names if n not in ("A", "B")]
    rng.shuffle(others)
    names = ["A", "B"] + others
    print("Порядок конфигураций (Random(42)):", names)
    raw: dict = {
        "date": _time.strftime("%Y-%m-%dT%H:%M:%S"),
        "ollama_version": ollama_version(),
        "runs_note": "прогонов: adaptive 3->2 по бюджету; per-config в notes",
        "config_order": list(names),
        "config_order_full_shuffle": raw_order_full,
        "est_per_query_s": {"A": 3.0, "B": 8.0, "C": 27.0, "E-2048": 27.0,
                            "E-4096": 27.0, "E-8192": 30.0,
                            "D-q2_K": 12.0, "D-q8_0": 30.0},
        "est_note": "оценки для планирования бюджета (по замерам прежних"
                    " прогонов), не входят в результаты",
        "configs": {},
        "cold_start": None,
        "choose_optimized": None,
        "notes": [],
    }
    sampler = RssSampler()
    sampler.start()
    runs = 3
    cold_done = False
    for name in list(names):
        remaining = TOTAL_DEADLINE_S - (_time.perf_counter() - started)
        cfg = configs[name]
        if name == "E-4096":
            raw["notes"].append("E-4096 идентична C — сырые данные C")
            raw["configs"]["E-4096"] = {"reused": "C"}
            continue
        raw["configs"].setdefault(name, {})
        raw["configs"][name]["params"] = _dc.asdict(cfg)
        raw["configs"][name]["model_file_size"] = ollama_list_size(cfg.model)
        # pull для D (пропускаем, если тег уже скачан)
        if name.startswith("D-") and ollama_list_size(cfg.model) is None:
            pull_budget = min(PULL_TIMEOUT_CAP_S,
                              remaining - PULL_RESULT_BUFFER_S)
            print(f"[bench] pull {cfg.model} (бюджет {pull_budget:.0f} с)")
            result = pull_model(cfg.model, pull_budget)
            raw["configs"][name]["pull"] = result
            if not result["ok"]:
                raw["configs"][name]["skipped"] = (
                    "недоступно: " + result["reason"])
                raw["configs"][name]["samples"] = []
                continue
        # холодный старт: один раз в самом начале, stop + первый запрос
        if not cold_done:
            raw["cold_start"] = {
                "model": cfg.model,
                "stop_output": stop_model(cfg.model),
            }
            cold_client = LLMClient(ollama_url, cfg.model,
                                    timeout_per_query, params=cfg)
            cold = _is_cold_warmup(cold_client, name)
            raw["cold_start"]["query"] = cold
            cold_done = True
            print(f"[bench] холодный старт {name}: load="
                  f"{(cold['load_duration'] or 0)/1e9:.1f}с "
                  f"total={cold['time']:.1f}с")
        client = LLMClient(ollama_url, cfg.model, timeout_per_query,
                           params=_dc.replace(cfg))
        est = raw["est_per_query_s"].get(name, 16.0)
        runs_i = runs if name in ("A", "B") else min(runs, 2)
        if runs_i < runs:
            raw["notes"].append(
                f"{name}: 2 прогона вместо 3 (полный сет конфигураций"
                " не укладывается в дедлайн; escape по промту)")
        while (TOTAL_DEADLINE_S - (_time.perf_counter() - started)
               < runs_i * 6 * est + 120) and runs_i > 2:
            runs_i = 2
            raw["notes"].append(
                f"{name}: снижено до 2 прогонов (бюджет времени)")
        if TOTAL_DEADLINE_S - (_time.perf_counter() - started) \
                < runs_i * 6 * est + 80:
            raw["configs"][name]["skipped"] = (
                "не уложился по времени (threshold)")
            raw["configs"][name]["samples"] = []
            continue
        print(f"[bench] конфигурация {name}({cfg.model}, runs={runs_i}) "
              f"осталось {remaining:.0f} с")
        data = collect_config(client, runs_i)
        raw["configs"][name]["samples"] = data["samples"]
        raw["configs"][name]["summary"] = summarize(data["samples"])
        raw["configs"][name]["ollama_ps"] = ollama_ps(cfg.model)
        raw["configs"][name]["max_rss_kb"] = sampler.take()
    elapsed = _time.perf_counter() - started
    print(f"[bench] запросы завершены за {elapsed:.0f} с")
    sampler.stop()
    raw["bench_elapsed_s"] = round(elapsed, 1)
    raw["max_rss_kb"] = sampler.max_rss_kb
    raw["rss_samples"] = sampler.samples
    return raw


def apply_rule(raw: dict) -> tuple[str | None, str]:
    """Применяет правило выбора OPTIMIZED к сырым данным бенчмарка."""
    a = (raw.get("configs", {}).get("A", {}) or {}).get("summary")
    if not a:
        return None, "нет статистики A — правило не применено"
    candidates = {}
    for name in ("B", "C", "D-q8_0", "D-q2_K", "E-2048", "E-4096", "E-8192"):
        cfg = raw.get("configs", {}).get(name, {})
        if cfg.get("skipped"):
            continue
        if cfg.get("reused"):
            src = (raw.get("configs", {}).get(cfg["reused"], {}) or {}) \
                .get("summary")
            if src:
                candidates[name] = src
            continue
        if cfg.get("summary"):
            candidates[name] = cfg["summary"]
    return choose_optimized(a, candidates)


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
    ap(f"- Прогонов: {raw.get('runs_summary', 'см. raw')};"
       f" порядок конфигураций перемешан и зафиксирован"
       f" `random.Random(42)` (полный shuffle: "
       f"{', '.join(raw.get('config_order_full_shuffle', []))};"
       f" фактический порядок запуска: "
       f"{', '.join(raw.get('config_order', []))}).")
    ap("- Автометрики качества — прокси: не заменяют человеческую оценку.")
    ap("")
    ap ("## Таблица «до/после» (A–E)")
    ap("")
    ap("| Конфиг | Среднее,с | p95,с | т/с | Символ. | Принято | Откл. |"
       " Model | SIZE | PROCESSOR | maxRSS,МБ |")
    ap("|---|---|---|---|---|---|---|---|---|---|---|")
    for name in sorted(raw.get("configs", {})):
        cfg = raw.get("configs", {}).get(name, {})
        if cfg.get("reused"):
            ap(f"| {name} | переиспользует {cfg['reused']} | | | | |"
               " | | | | |")
            continue
        if cfg.get("skipped"):
            ap(f"| {name} | {cfg['skipped']} | | | | | | | | | |")
            continue
        s = cfg.get("summary") or {}
        ps = cfg.get("ollama_ps") or {}
        rej = s.get("rejected_by_reason") or {}
        rej_txt = (", ".join(f"{k}:{v}" for k, v in sorted(rej.items()))
                   or "—")
        rss_mb = cfg.get("max_rss_kb", 0) / 1024.0
        model = (cfg.get("params", {}) or {}).get("model", "")
        ap(f"| {name} | {_fmt(s.get('avg_time'))} | "
           f"{_fmt(s.get('p95_time'))}"
           f"{'*' if s.get('p95_approx') else ''} | "
           f"{_fmt(s.get('tokens_per_sec_avg'), 1)} | "
           f"{_fmt(s.get('avg_len'), 0)} | "
           f"{_fmt(100 * float(s.get('accepted_share') or 0), 0)}% | "
           f"{rej_txt} | {model} | {ps.get('SIZE','—')} | "
           f"{ps.get('PROCESSOR','—')} | {rss_mb:.0f} |")
    ap("")
    ap("`*` — p95 приблизительный: меньше 20 точек.")
    ap("")
    cs = raw.get("cold_start") or {}
    cq = cs.get("query") or {}
    ap(f"Холодный старт (`{cs.get('model')}`): load_duration = "
       f"{(cq.get('load_duration') or 0)/1e9:.1f} с, полный запрос = "
       f"{cq.get('time')} с (в статистику не входит).")
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
    chosen = chosen or {}
    if chosen:
        ap("Записанный пресет OPTIMIZED (modelsettings.py): "
           + ", ".join(f"{k}={v}" for k, v in chosen.items()))
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
