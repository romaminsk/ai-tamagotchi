"""Точка входа: GUI-режим, --check, --demo-log, TAMAGOTCHI_SMOKE."""

import argparse
import json
import os
import sys
import time
import urllib.request

from .config import load_config
from .fallback import get_fallback
from .pet import TICK_SECONDS, Pet
from .storage import apply_offline_time, load, save

CHECK_SCENARIOS = [
    ("приветствие", "happy", "greet"),
    ("голодный питомец", "hungry", "fed"),
    ("после игры", "happy", "played"),
]

DEMO_SCENARIOS = [
    ("приветствие", "greet"),
    ("покормили", "fed"),
    ("поиграли", "played"),
    ("помыли", "cleaned"),
    ("лёг спать", "sleep_started"),
    ("голодный idle", "idle"),
]


def _get(url: str, timeout: float = 2.0) -> bytes | None:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            return resp.read()
    except Exception:
        return None


def _make_pet_situation(pet: Pet, event: str) -> str:
    """Настраивает параметры питомца под сценарий и возвращает настроение."""
    if event == "greet":
        return pet.mood()
    if event == "fed":
        pet.hunger = 15.0
        pet.feed()
        return pet.mood()
    if event == "played":
        pet.play()
        return pet.mood()
    if event == "cleaned":
        pet.hygiene = 10.0
        pet.clean()
        return pet.mood()
    if event == "sleep_started":
        pet.energy = 40.0
        pet.sleep()
        return pet.mood()
    if event == "idle":
        pet.hunger = 15.0
        pet.fun = 20.0
        return pet.mood()
    return pet.mood()


def run_check(config) -> int:
    """Проверка Ollama: доступность + 3 запроса разной сложности."""
    from .llm import LLMClient
    client = LLMClient(config["OLLAMA_URL"], config["OLLAMA_MODEL"],
                       config["LLM_TIMEOUT"])
    name = config["PET_NAME"]
    print("Проверка доступности Ollama на "
          f"{config['OLLAMA_URL']} ...")
    if not client.check_available():
        print(f"[FAIL] Ollama недоступна (URL {config['OLLAMA_URL']}, "
              f"модель {config['OLLAMA_MODEL']})")
        return 1
    print(f"[OK] Модель {config['OLLAMA_MODEL']} доступна")
    all_ok = True
    for title, mood, event in CHECK_SCENARIOS:
        started = time.monotonic()
        phrase = client.chat(name, mood,
                             {"hunger": 20 if mood == "hungry" else 80,
                              "energy": 60, "fun": 70, "hygiene": 80},
                             event).text
        elapsed = time.monotonic() - started
        status = "OK " if phrase else "FAIL"
        if not phrase:
            all_ok = False
        print(f"[{status}] {title}: {elapsed:.1f} сек -> "
              f"{phrase!r}")
    return 0 if all_ok else 1


def run_demo_log(config, out_path: str | None) -> int:
    """6 сценариев через реальную Ollama; таблица 'событие | настроение | ответ | сек'."""
    from .llm import LLMClient
    print("Проверка доступности Ollama на "
          f"{config['OLLAMA_URL']} ...")
    client = LLMClient(config["OLLAMA_URL"], config["OLLAMA_MODEL"],
                       config["LLM_TIMEOUT"])
    if not client.check_available():
        print(f"[FAIL] Ollama недоступна (URL {config['OLLAMA_URL']})")
        return 1
    name = config["PET_NAME"]
    rows = []
    for title, event in DEMO_SCENARIOS:
        pet = Pet(name)
        mood = _make_pet_situation(pet, event)
        started = time.monotonic()
        phrase = client.chat(
            name, mood,
            {"hunger": pet.hunger, "energy": pet.energy, "fun": pet.fun,
             "hygiene": pet.hygiene}, event).text
        elapsed = time.monotonic() - started
        rows.append((event, mood, phrase or "(нет ответа)", f"{elapsed:.1f}"))
    lines = [f"{'событие':<18} | {'настроение':<9} | ответ | сек",
             "-" * 90]
    for event, mood, phrase, secs in rows:
        lines.append(f"{event:<18} | {mood:<9} | {phrase} | {secs}")
    text = "\n".join(lines)
    print(text)
    if out_path:
        with open(out_path, "w", encoding="utf-8") as f:
            f.write(text + "\n")
        print(f"Сохранено: {out_path}")
    return 0


def run_bench(config) -> int:
    """Бенчмарк A–E: пишет docs/bench-raw.json и docs/optimization-report.md."""
    import dataclasses
    from . import bench
    if not bench.ollama_version():
        print("[BENCH FAIL] ollama CLI недоступна — бенчмарк не запускался")
        return 1
    url = config["OLLAMA_URL"]
    print(f"Бенчмарк против {url}; дедлайн {bench.TOTAL_DEADLINE_S:.0f} с")
    raw = bench.run_bench(url)
    name, why = bench.apply_rule(raw)
    raw["choose_optimized"] = {"name": name, "why": why}
    quality = bench.quality_examples(raw)
    raw["quality_examples"] = quality
    chosen_params = {}
    if name:
        params = bench.build_configs()[name]
        chosen_params = dataclasses.asdict(params)
        raw["chosen_config"] = chosen_params
    raw_path = bench.write_raw(raw)
    report_path = bench.write_report(raw, chosen=chosen_params)
    print(f"[BENCH OK] raw: {raw_path}; отчёт: {report_path}")
    if name:
        print(f"[BENCH] выбранный OPTIMIZED: {name} — {why}")
        print("[BENCH] обновите tamagotchi/modelsettings.py: OPTIMIZED"
              " (см. отчёт).")
    else:
        print(f"[BENCH] правило не выбрало конфигурацию: {why}")
    return 0


def run_smoke(config) -> int:
    """GUI-смоук: окно, 3 тика, действие, кнопки панели, закрыть. Exit 0."""
    import tempfile
    import tkinter as tk
    from .ui import TamagotchiApp
    home = tempfile.mkdtemp(prefix="tamagotchi-smoke-")
    os.environ["TAMAGOTCHI_HOME"] = home
    try:
        root = tk.Tk()
    except tk.TclError as e:
        print(f"[SMOKE FAIL] окно не создалось: {e}")
        return 1
    pet = load()[0]
    app = TamagotchiApp(root, config, pet, tick_ms=100)
    ticks_done = {"n": 0}

    closed = {"flag": False}

    def do_three_ticks():
        app.pet.tick()
        ticks_done["n"] += 1
        app.render()
        if ticks_done["n"] < 3:
            root.after(50, do_three_ticks)
        else:
            root.after(50, do_action)

    def do_action():
        app._act("feed")
        root.after(300, finish)

    def finish():
        closed["flag"] = True
        try:
            root.destroy()
        except tk.TclError:
            pass

    def check_buttons_layout() -> int:
        """Кнопки маппятся, целиком внутри окна; Canvas питомца ≥ 240 px."""
        root.update_idletasks()
        canvas_h = app.canvas.winfo_height()
        if canvas_h < 240:
            print(f"[SMOKE FAIL] canvas питомца {canvas_h}px < 240px "
                  "(вытесняется нижними панелями)")
            return 1
        win_top = root.winfo_rooty()
        win_bottom = win_top + root.winfo_height()
        win_right = root.winfo_rootx() + root.winfo_width()
        win_left = root.winfo_rootx()
        problems = []
        all_buttons = list(app.buttons.items()) + list(
            (f"панель:{n}", b) for n, b in app.model_buttons.items())
        for index, btn in all_buttons:
            try:
                mapped = btn.winfo_ismapped()
            except tk.TclError:
                problems.append(f"кнопка {index} не отображена")
                continue
            if not mapped:
                problems.append(f"кнопка {index} не отображена")
                continue
            bottom = btn.winfo_rooty() + btn.winfo_height()
            right = btn.winfo_rootx() + btn.winfo_width()
            if bottom > win_bottom:
                problems.append(
                    f"кнопка {index} выступает на {bottom - win_bottom}px")
            if right > win_right or btn.winfo_rootx() < win_left:
                problems.append(f"кнопка {index} по ширине за окном")
        if problems:
            print("[SMOKE FAIL] раскладка кнопок:")
            for problem in problems:
                print("  -", problem)
            return 1
        print("[SMOKE OK] все кнопки отображены и внутри окна,"
              f" Canvas питомца {canvas_h}px")
        return 0

    def check_button_clicks() -> int:
        """Клик по каждой из 4 кнопок вызывает действие.

        Основная дорожка — event_generate('<Button-1>'); если на macOS
        событие не дошло, fallback: прямой вызов bind-обработчика
        с проверкой, что '<Button-1>' у виджета зарегистрирован.
        """
        before = app.actions_count
        generated = True
        try:
            for btn in app.buttons.values():
                btn.event_generate("<Button-1>")
            root.update()
        except tk.TclError:
            generated = False
        raised = app.actions_count - before
        if raised >= 4:
            print(f"[SMOKE OK] клики по кнопкам: {raised}/4 события")
            return 0
        bound = all(btn.bind("<Button-1>") is not None
                    for btn in app.buttons.values())
        if not bound:
            print("[SMOKE FAIL] <Button-1> не зарегистрирован на кнопках")
            return 1
        try:
            for btn in app.buttons.values():
                btn.on_press()
            root.update()
        except tk.TclError:
            pass
        raised = app.actions_count - before
        if raised >= 4:
            print(f"[SMOKE OK] клики (fallback-дорожка): "
                  f"{raised}/4 действия вызвали обработчики")
            return 0
        print(f"[SMOKE FAIL] клики: сработало {raised}/4, event_generate "
              f"{'работал' if generated else 'не работал'}")
        return 1

    def check_panel_buttons() -> int:
        """Клик по каждой кнопке панели меняет значение в ModelParams.

        Неактивная (серая) кнопка «Модель» пропускается. Обработчики
        не должны делать сетевых вызовов и блокировать UI.
        """
        import dataclasses
        root.update_idletasks()
        problems = []
        for name, btn in sorted(app.model_buttons.items()):
            if not getattr(btn, "_enabled", True):
                continue
            before = dataclasses.asdict(app.client.params)
            t0 = time.monotonic()
            btn.on_press()
            root.update()
            elapsed = time.monotonic() - t0
            after = dataclasses.asdict(app.client.params)
            if after == before:
                problems.append(f"кнопка {name} не изменила параметры")
            if elapsed > 0.5:
                problems.append(f"кнопка {name} заняла {elapsed:.2f} с")
        if problems:
            print("[SMOKE FAIL] панель модели:")
            for problem in problems:
                print("  -", problem)
            return 1
        print("[SMOKE OK] кнопки панели модели меняют параметры")
        return 0

    layout_exit = None
    clicks_exit = None
    panel_exit = None

    try:
        root.after(300, do_three_ticks)
        root.update_idletasks()
        root.update()
        layout_exit = check_buttons_layout()
        clicks_exit = check_button_clicks()
        panel_exit = check_panel_buttons()
        deadline = time.monotonic() + 30
        while not closed["flag"] and time.monotonic() < deadline:
            still_open = True
            try:
                still_open = bool(root.winfo_exists())
            except tk.TclError:
                closed["flag"] = True
                break
            if not still_open:
                break
            try:
                root.update()
            except tk.TclError:
                break
            time.sleep(0.02)
    finally:
        try:
            root.destroy()
        except tk.TclError:
            pass
    if ticks_done["n"] != 3:
        print(f"[SMOKE FAIL] тиков {ticks_done['n']}, ожидалось 3")
        return 1
    if layout_exit not in (0, None):
        return 1
    if clicks_exit not in (0, None):
        return 1
    if panel_exit not in (0, None):
        return 1
    print("[SMOKE OK] окно, 3 тика и действие выполены")
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="tamagotchi", description="Тамагочи с локальной LLM (Ollama)")
    parser.add_argument("--check", action="store_true",
                        help="проверка Ollama и 3 запроса")
    parser.add_argument("--demo-log", action="store_true",
                        help="6 сценариев без GUI, таблица ответов")
    parser.add_argument("--demo-out", default="docs/check-output.txt",
                        help="куда сохранить вывод --demo-log")
    parser.add_argument("--bench", action="store_true",
                        help="бенчмарк A-E: позволять только при живой "
                             "Ollama (не для тестов)")
    args = parser.parse_args(argv)

    try:
        config = load_config()
    except Exception as e:
        print(f"Конфигурация: {e}")
        return 2

    if args.check:
        return run_check(config)
    if args.demo_log:
        return run_demo_log(config, args.demo_out)
    if args.bench:
        return run_bench(config)

    home = os.environ.get("TAMAGOTCHI_HOME")

    # обычный (или смоук) GUI-запуск
    if os.environ.get("TAMAGOTCHI_SMOKE") == "1":
        return run_smoke(config)

    import tkinter as tk
    from .ui import TamagotchiApp
    pet, saved_at = load()
    apply_offline_time(pet, saved_at)
    root = tk.Tk()
    TamagotchiApp(root, config, pet)
    try:
        root.mainloop()
    finally:
        save(pet)
    return 0


if __name__ == "__main__":
    sys.exit(main())
