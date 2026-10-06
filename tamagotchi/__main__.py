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
                             event)
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
             "hygiene": pet.hygiene}, event)
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


def run_smoke(config) -> int:
    """GUI-смоук: окно, 3 тика и 1 действие, закрыть. Exit 0 при успехе."""
    import tkinter as tk
    from .ui import TamagotchiApp
    for key in ("TAMAGOTCHI_HOME",):
        os.environ.setdefault(key, os.path.join(
            os.path.expanduser("~"), ".ai-tamagotchi-test"))
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
        """Кнопки маппятся и целиком внутри окна."""
        root.update_idletasks()
        win_top = root.winfo_rooty()
        win_bottom = win_top + root.winfo_height()
        problems = []
        for index, btn in sorted(app.buttons.items()):
            if not btn.winfo_ismapped():
                problems.append(f"кнопка {index} не отображена")
                continue
            bottom = btn.winfo_rooty() + btn.winfo_height()
            if bottom > win_bottom:
                problems.append(
                    f"кнопка {index} выступает на {bottom - win_bottom}px")
        if problems:
            print("[SMOKE FAIL] раскладка кнопок:")
            for problem in problems:
                print("  -", problem)
            return 1
        print("[SMOKE OK] все 4 кнопки отображены и внутри окна")
        return 0

    layout_exit = None

    try:
        root.after(300, do_three_ticks)
        root.update_idletasks()
        root.update()
        layout_exit = check_buttons_layout()
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
