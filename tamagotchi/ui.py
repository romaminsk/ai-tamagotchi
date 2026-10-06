"""Интерфейс tkinter: питомец на Canvas, полосы, кнопки, речевой пузырь.

UI не блокируется: все обращения к сети — в фоновых потоках (LLMWorker).
"""

import math
import queue
import threading
import time
import tkinter as tk
from tkinter import ttk

from . import sprite
from .llm import (LLMClient, LLMWorker, PhraseScheduler, PRIO_ACTION,
                  PRIO_IDLE, PRIO_MOOD)
from .pet import TICK_SECONDS, Pet

W, H = 480, 560
CANVAS_H = 380
CHECK_MS = 30_000        # период проверки доступности Ollama, мс
IDLE_CHECK_MS = 2_000
ANIM_MS = 120
POLL_MS = 150            # забор результатов воркера, мс
SAVE_EVERY_TICKS = 30

MOOD_LABELS = {
    "happy": "Настроение: счастлив",
    "neutral": "Настроение: спокойное",
    "hungry": "Настроение: голодный",
    "dirty": "Настроение: грязный",
    "sleepy": "Настроение: сонный",
    "sad": "Настроение: грустный",
    "sleeping": "Настроение: спит",
}


class TamagotchiApp:
    def __init__(self, root: tk.Tk, config: dict, pet: Pet,
                 tick_ms: int | None = None):
        self.root = root
        self.config = config
        self.pet = pet
        self.tick_ms = tick_ms or TICK_SECONDS * 1000

        self.client = LLMClient(config["OLLAMA_URL"], config["OLLAMA_MODEL"],
                                config["LLM_TIMEOUT"])
        self.worker = LLMWorker(self.client, config["PET_NAME"])
        self.scheduler = PhraseScheduler()
        self.worker.start()

        self.online = False
        self.thinking = False
        self.blink = False
        self.tick_counter = 0
        self.t0 = time.monotonic()
        self._save_counter = 0
        self._last_mood = pet.mood()
        self._greet_done = False

        self.root.title(f"Тамагочи: {config['PET_NAME']}")
        self.root.geometry(f"{W}x{H}")
        self.root.resizable(False, False)
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

        self._build()
        self._bind_keys()

        # Ollama: проверка на старте и каждые 30 с (отдельный поток)
        self.availability = queue.Queue()
        self._trigger_availability_check()
        self._check_loop()

        # внутренние циклы, всё через after — UI не блокируется
        self.root.after(POLL_MS, self._results_loop)
        self.root.after(IDLE_CHECK_MS, self._idle_loop)
        self.root.after(self.tick_ms, self._tick_loop)
        self.root.after(ANIM_MS, self._animate_loop)
        self.render()

    # ——— построение окна ———

    def _build(self):
        self.status_label = ttk.Label(self.root, text="LLM: офлайн")
        self.status_label.pack(side=tk.TOP, anchor="w", padx=8, pady=4)

        self.canvas = tk.Canvas(self.root, width=W, height=CANVAS_H,
                                name="scene", highlightthickness=0)
        self.canvas.pack(fill="x")

        self.speech = ttk.Label(self.root, text="", wraplength=W - 60,
                                justify="center", relief="solid",
                                padding=8, font=("TkDefaultFont", 12))
        self.speech.pack(pady=8, padx=24, fill="x")
        self.set_speech(f"Привет! Я {self.config['PET_NAME']}.")
        self.speech.config(text=f"Привет! Я {self.config['PET_NAME']}.")

        self.bars = {}
        for title, key in (("Сытость", "hunger"), ("Энергия", "energy"),
                           ("Веселье", "fun"), ("Чистота", "hygiene")):
            row = ttk.Frame(self.root)
            row.pack(fill="x", padx=14, pady=3)
            ttk.Label(row, text=title, width=9).pack(side=tk.LEFT)
            bar = ttk.Progressbar(row, maximum=100, length=W - 120,
                                  mode="determinate")
            bar.pack(side=tk.LEFT, fill="x", expand=True)
            self.bars[key] = bar

        self.mood_label = ttk.Label(self.root, text="Настроение: —")
        self.mood_label.pack(pady=2)

        panel = ttk.Frame(self.root)
        panel.pack(pady=6)
        self.buttons = {}
        for row_index, (title, handler) in enumerate((
                ("1: Покормить", self._feed), ("2: Играть", self._play),
                ("3: Спать/Разбудить", self._sleep_toggle),
                ("4: Помыть", self._clean))):
            btn = ttk.Button(panel, text=title, command=handler, width=19)
            btn.grid(row=row_index // 2, column=row_index % 2, padx=6,
                     pady=4)
            self.buttons[row_index + 1] = btn

    def _bind_keys(self):
        for key, index in (("1", 1), ("2", 2), ("3", 3), ("4", 4)):
            self.root.bind(key, lambda e, i=index: self.buttons[i].invoke())
        self.root.bind("<Escape>", lambda e: self._on_close())

    # ——— рендер сцены ———

    def _night_mode(self) -> bool:
        if self.pet.sleeping:
            return True
        return not (7 <= time.localtime().tm_hour < 19)

    def _bounce(self) -> float:
        elapsed = time.monotonic() - self.t0
        phase = math.sin(elapsed * 2.2) * 4.0 if not self.pet.sleeping else 0.0
        return phase

    def render(self):
        """Полная перерисовка: фон, питомец по mood(), полосы, пузырь."""
        canvas = self.canvas
        canvas.delete("all")
        p = sprite.sprite_params(self.pet.mood(), self.blink, self._bounce())
        night = self._night_mode()

        canvas.configure(bg="#101c33" if night else "#cde9f7")
        if night:
            canvas.create_oval(W - 80, 26, W - 30, 76, fill="#f5f2da",
                               outline="")
            for x, y, s in ((40, 40, 2), (110, 66, 3), (178, 44, 2),
                            (300, 58, 3), (250, 36, 2)):
                canvas.create_oval(x - s, y - s, x + s, y + s, fill="#e8eeff",
                                   outline="")
        else:
            canvas.create_oval(W - 96, 18, W - 34, 80, fill="#ffe066",
                               outline="")

        ground_y = CANVAS_H - 36
        canvas.create_rectangle(0, ground_y, W, CANVAS_H,
                                fill="#8bbf6a" if not night else "#274227",
                                width=0)

        cx = W // 2
        cy = ground_y - 66 + int(p["bounce"])
        r = 52
        accent, body = p["accent"], p["body"]

        # тело и уши
        canvas.create_oval(cx - r, cy - r, cx + r, cy + r, fill=body,
                           outline=accent, width=3)
        for dx in (-30, 30):
            canvas.create_oval(cx + dx - 11, cy - r - 18, cx + dx + 11,
                               cy - r + 10, fill=accent, outline="")
        # глаза
        eye_y, eye_dx = cy - 12, 19
        ink = "#2b2b2b"
        if p["eye"] == "closed":
            for sx in (-1, 1):
                canvas.create_line(cx + sx * eye_dx - 7, eye_y,
                                   cx + sx * eye_dx + 7, eye_y,
                                   fill=ink, width=3)
            canvas.create_text(cx + r - 6, cy - r - 16, text="Z z z",
                               fill="#c7d4e8",
                               font=("TkDefaultFont", 14, "bold"))
        elif p["eye"] == "happy":
            for sx in (-1, 1):
                x0 = cx + sx * eye_dx
                canvas.create_line(x0 - 7, eye_y, x0, eye_y - 8, fill=ink,
                                   width=3)
                canvas.create_line(x0, eye_y - 8, x0 + 7, eye_y, fill=ink,
                                   width=3)
        elif p["eye"] == "sad_eye":
            for sx in (-1, 1):
                x0 = cx + sx * eye_dx
                canvas.create_line(x0 - 6, eye_y - 4, x0 + 6, eye_y + 4,
                                   fill=ink, width=3)
        elif p["eye"] == "half":
            for sx in (-1, 1):
                x0 = cx + sx * eye_dx
                canvas.create_oval(x0 - 6, eye_y - 3, x0 + 6, eye_y + 5,
                                   fill=ink, outline="")
        else:
            for sx in (-1, 1):
                x0 = cx + sx * eye_dx
                canvas.create_oval(x0 - 6, eye_y - 6, x0 + 6, eye_y + 6,
                                   fill=ink, outline="")

        # рот
        my = cy + 24
        if p["mouth"] == "smile":
            canvas.create_arc(cx - 17, my - 12, cx + 17, my + 12,
                              start=200, extent=140, style=tk.ARC, outline=ink,
                              width=3)
        elif p["mouth"] == "sad":
            canvas.create_arc(cx - 17, my + 10, cx + 17, my - 8, start=20,
                              extent=140, style=tk.ARC, outline=ink, width=3)
        elif p["mouth"] in ("o", "o_big"):
            delta = 7 if p["mouth"] == "o" else 10
            canvas.create_oval(cx - delta, my - 6, cx + delta, my + 14,
                               fill="#7c4a43", outline=ink, width=2)
        else:
            canvas.create_line(cx - 12, my + 4, cx + 12, my + 4, fill=ink,
                               width=3)

        # щёки
        for sx in (-1, 1):
            x0 = cx + sx * 34
            canvas.create_oval(x0 - 6, cy + 8, x0 + 6, cy + 20,
                               fill="#f4a3a0", outline="")

        # fx-элементы
        if "drops" in p["fx"]:
            for dx, dy in ((-62, -68), (58, -60), (-44, -50)):
                canvas.create_oval(cx + dx, cy + dy, cx + dx + 9, cy + dy + 9,
                                   fill="#6b7d3f", outline="")

        if "rumble" in p["fx"]:
            canvas.create_text(cx, cy - r - 34, text="урр...", fill="#c77",
                               font=("TkDefaultFont", 10, "italic"))
        if "yawn" in p["fx"]:
            canvas.create_text(cx + r + 16, cy + 18, text="зевок!",
                               fill="#667",
                               font=("TkDefaultFont", 10, "italic"))
        if "tear" in p["fx"]:
            canvas.create_line(cx - eye_dx, eye_y + 8, cx - eye_dx,
                               eye_y + 20, fill="#5fa8d3", width=4)
        if "zzz" in p["fx"]:
            canvas.create_text(cx - r - 4, cy - r - 2, text="z",
                               fill="#9fb4d0",
                               font=("TkDefaultFont", 11, "italic"))

        canvas.create_text(10, 10, anchor="nw",
                           text=f"{self.pet.name}, тиков: "
                                f"{self.pet.age_ticks}",
                           fill="#eef" if night else "#333")

        for key, bar in self.bars.items():
            bar["value"] = max(0.0, min(100.0, getattr(self.pet, key)))

        self._render_labels()

    def _render_labels(self):
        base = (f"LLM: онлайн ({self.config['OLLAMA_MODEL']})"
                if self.online else "LLM: офлайн")
        if self.thinking:
            base += " · думает…"
        self.status_label.config(text=base,
                                 foreground="#22814b" if self.online
                                 else "#a25353")
        self.mood_label.config(
            text=MOOD_LABELS.get(self.pet.mood(), self.pet.mood()))
        self.buttons[3].config(text=("3: Разбудить" if self.pet.sleeping
                                     else "3: Спать"))

    def set_speech(self, text):
        if isinstance(text, str) and text:
            self.speech.config(text=text[:200])

    # ——— действия игрока (мгновенная заготовка + запрос LLM) ———

    def _feed(self):
        self._act("feed")

    def _play(self):
        self._act("play")

    def _sleep_toggle(self):
        self._act("wake" if self.pet.sleeping else "sleep")

    def _clean(self):
        self._act("clean")

    ACTIONS = {"feed": "feed", "play": "play", "sleep": "sleep",
               "wake": "wake", "clean": "clean"}

    def _act(self, action):
        event = {"feed": self.pet.feed, "play": self.pet.play,
                 "sleep": self.pet.sleep, "wake": self.pet.wake,
                 "clean": self.pet.clean}[action]()
        mood = self.pet.mood()
        self.set_speech(self.client.fallback_phrase(mood, event))
        self.render()
        self._request_speech(PRIO_ACTION, mood, event)
        self._last_mood = mood

    def _request_speech(self, kind, mood, event):
        """Единая точка запроса через планировщик: троттлинг и приоритеты."""
        accepted, _reason = self.scheduler.decide(kind)
        if not accepted:
            return
        self.thinking = True
        self.worker.submit(kind, mood, self.snapshot_params(), event)

    def snapshot_params(self):
        return {"hunger": self.pet.hunger, "energy": self.pet.energy,
                "fun": self.pet.fun, "hygiene": self.pet.hygiene}

    # ——— фоновые циклы (root.after), UI не блокируется ———

    def _results_loop(self):
        try:
            while True:
                _kind, _mood, _event, phrase, _elapsed = \
                    self.worker.results.get_nowait()
                self.thinking = False
                if phrase:
                    self.set_speech(phrase)
                self.scheduler.note_sent()
        except queue.Empty:
            pass
        self.root.after(POLL_MS, self._results_loop)

    def _idle_loop(self):
        mood = self.pet.mood()
        if mood != self._last_mood and not self.worker.is_busy():
            self._last_mood = mood
            self._request_speech(PRIO_MOOD, mood, None)
        if self.scheduler.idle_due() and not self.worker.is_busy():
            # фактически отправляем только если планировщик разрешил
            self.thinking = True
            self.worker.submit(PRIO_IDLE, mood, self.snapshot_params(),
                               "idle")
            self.scheduler.note_sent()
        self.render()
        self.root.after(IDLE_CHECK_MS, self._idle_loop)

    def _tick_loop(self):
        self.pet.tick()
        self.tick_counter += 1
        self._save_counter += 1
        if self._save_counter >= SAVE_EVERY_TICKS:
            self._save_counter = 0
            self.save_now()
        self.render()
        self.root.after(self.tick_ms, self._tick_loop)

    def _animate_loop(self):
        now_ms = int(time.monotonic() * 1000)
        self.blink = (now_ms % 4600) < 150
        self.render()
        self.root.after(ANIM_MS, self._animate_loop)

    # ——— доступность Ollama ———

    def _trigger_availability_check(self):
        def run():
            online = self.client.check_available()
            try:
                self.availability.put(online)
            except Exception:
                pass

        threading.Thread(target=run, daemon=True).start()

    def _check_loop(self):
        try:
            while True:
                ok = self.availability.get_nowait()
                if ok != self.online:
                    self.online = ok
        except queue.Empty:
            pass
        self.root.after(CHECK_MS, self._trigger_availability_check)
        self.root.after(CHECK_MS, self._check_loop)

    # ——— сохранение и закрытие ———

    def save_now(self):
        try:
            from . import storage
            storage.save(self.pet)
        except Exception:
            pass

    def _on_close(self):
        try:
            self.save_now()
            self.worker.stop(timeout=2.0)
        except Exception:
            pass
        finally:
            self.root.destroy()
