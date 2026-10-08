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
from .llm import (LAST_METRICS, LLMClient, LLMWorker, PhraseScheduler,
                  PRIO_ACTION, PRIO_IDLE, PRIO_MOOD)
from .modelsettings import (BASELINE, CTX_ALLOWED, LENGTH_MODES, OPTIMIZED,
                            TEMP_MAX, TEMP_MIN, TOK_MAX, clamp_params,
                            load_params, save_params)
from .pet import TICK_SECONDS, Pet

W, H = 480, 780
MIN_H = 740
MAX_H = 780
CANVAS_H = 380
BTN_FONT = 14
BTN_PANEL_FONT = 12
PANEL_PIC_PX = 4
TEMP_STEP = 0.1
TOKEN_STEPS = (60, 120, 220, 320, 400)
SPEECH_MAX_CHARS = 320
SPEECH_MAX_LINES = 6
SPEECH_FONT = 12
LENGTH_RU = {"short": "кратко", "medium": "средне", "long": "развёрнуто"}
CTX_RU = {2048: "2048", 4096: "4096", 8192: "8192"}
BTN_PRESS_MS = 130      # эффект нажатия, мс

MOOD_LABELS = {
    "happy": "Настроение: счастлив",
    "neutral": "Настроение: спокойное",
    "hungry": "Настроение: голодный",
    "dirty": "Настроение: грязный",
    "sleepy": "Настроение: сонный",
    "sad": "Настроение: грустный",
    "sleeping": "Настроение: спит",
}

BTN_BG = "#37474f"
BTN_FG = "#eceff1"
BTN_BG_PRESS = "#78909c"
BTN_DISABLE_BG = "#546e7a"
BTN_DISABLE_NOTE = ""


class BigButton(tk.Label):
    """own-виджет кнопки: Label + bind кликов (надёжно на macOS Aqua).

    tk.Button/ttk.Button на Aqua игнорируют bg/fg и срабатывают только
    по ButtonRelease «точно внутри» виджета; здесь действие привязано
    к <Button-1>, с эффектом нажатия и курсором hand2.
    """

    def __init__(self, master, title: str, command,
                 font_size: int = BTN_FONT, pady: int = 10):
        super().__init__(master, text=title, bg=BTN_BG, fg=BTN_FG,
                         font=("TkDefaultFont", font_size, "bold"),
                         padx=10, pady=pady, cursor="hand2",
                         relief="raised", bd=1)
        self._title_base = title
        self._command = command
        self._press_job = None
        self._armed = False           # флаг занятости эффекта, не блокирует
        self._press_flowing = False   # ok для тестов
        self._enabled = True
        self.bind("<Button-1>", self.on_press)
        self.bind("<ButtonRelease-1>", self.on_release)
        self.bind("<Enter>", lambda e: self._set_hover())
        self.bind("<Leave>", lambda e: self._set_idle())

    def _set_idle(self):
        try:
            self.configure(bg=BTN_BG)
        except tk.TclError:
            pass

    def _set_hover(self):
        try:
            self.configure(bg="#455a64")
        except tk.TclError:
            pass

    def on_press(self, _event=None):
        if not self._enabled:
            return
        if self._press_flowing:
            return  # не дублируем, пока эффект активен
        self._press_flowing = True
        try:
            self.configure(bg=BTN_BG_PRESS)
        except tk.TclError:
            pass
        self.click()
        # цвет вернётся в исходный через BTN_PRESS_MS либо при release
        self._press_job = self.after(BTN_PRESS_MS, self._restore)

    def on_release(self, _event=None):
        if self._press_flowing and self._press_job is None:
            # release пришёл раньше таймера — восстановить немедленно
            self._restore()

    def _restore(self):
        try:
            if self._press_job is not None:
                self.after_cancel(self._press_job)
        except tk.TclError:
            pass
        self._press_job = None
        self._press_flowing = False
        try:
            self.configure(bg=BTN_BG)
        except tk.TclError:
            pass

    def click(self):
        """Общая точка для кликов и клавиш."""
        self._command()

    def invoke(self):
        # совместимость со смоуком/клавишами
        self.click()

    def set_title(self, title: str):
        self._title_base = title
        self.configure(text=title)

    def set_enabled(self, enabled: bool):
        """Серый фон и no-op клик, если выключена."""
        self._enabled = enabled
        try:
            self.configure(bg=BTN_DISABLE_BG if not enabled else BTN_BG,
                           cursor="hand2" if enabled else "arrow")
        except tk.TclError:
            pass
        if enabled and self._title_base:
            self.configure(text=self._title_base)


CHECK_MS = 30_000        # период проверки доступности Ollama, мс
IDLE_CHECK_MS = 2_000
ANIM_MS = 120
POLL_MS = 150            # забор результатов воркера, мс
SAVE_EVERY_TICKS = 30


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
        self.actions_count = 0  # для смоук-проверки кликов
        self.model_cache: list[str] = []   # кэш /api/tags (фон. поток)
        self._model_index = 0

        self.root.title(f"Тамагочи: {config['PET_NAME']}")
        self.root.geometry(f"{W}x{H}")
        self.root.minsize(W, MIN_H)
        self.root.resizable(True, False)
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

        self._build()
        self._fit_window_height()
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

        # нижние блоки: сначала кнопки (самый низ), затем панель модели,
        # подсказка, полосы, пузырь — Canvas добавляется последним
        panel = tk.Frame(self.root, bg="#263238")
        panel.pack(side=tk.BOTTOM, fill="x", padx=6, pady=6)
        self.buttons = {}
        for column, (title, handler) in enumerate((
                ("🍖 Кормить 1", self._feed),
                ("🎾 Играть 2", self._play),
                ("😴 Спать 3", self._sleep_toggle),
                ("🛁 Мыть 4", self._clean))):
            panel.columnconfigure(column, weight=1, uniform="btn")
            btn = BigButton(panel, title, handler)
            btn.grid(row=0, column=column, sticky="nsew")
            self.buttons[column + 1] = btn

        # ——— панель «Модель» (между кнопками и подсказкой) ———
        model_panel = tk.LabelFrame(self.root, text="Модель", bg="#263238",
                                    fg="#cfd8dc")
        model_panel.pack(side=tk.BOTTOM, fill="x", padx=6, pady=(0, 2))
        self.model_buttons = {}
        rows = (
            [("Temp −", self._temp_down), ("Temp +", self._temp_up),
             ("Длина ⟳", self._length_cycle)],
            [("Токены ⟳", self._tokens_cycle),
             ("Контекст ⟳", self._ctx_cycle),
             ("Пресет ⟳", self._preset_cycle)],
            [("Модель ⟳", self._model_cycle)],
        )
        for row, items in enumerate(rows):
            for column, (title, handler) in enumerate(items):
                if len(items) == 1:
                    model_panel.columnconfigure(0, weight=1)
                    model_panel.columnconfigure(1, weight=1)
                    btn = BigButton(model_panel, title, handler,
                                    font_size=BTN_PANEL_FONT, pady=4)
                    btn.grid(row=row, column=0, columnspan=2, sticky="we",
                             padx=2, pady=2)
                else:
                    model_panel.columnconfigure(column, weight=1,
                                                uniform="mpanel")
                    btn = BigButton(model_panel, title, handler,
                                    font_size=BTN_PANEL_FONT, pady=4)
                    btn.grid(row=row, column=column, sticky="we",
                             padx=2, pady=2)
                key = title.split()[0].rstrip("−/+⟳ ") or f"r{row}c{column}"
                self.model_buttons[key] = btn

        self.params_label = tk.Label(
            model_panel, text="", bg="#263238", fg="#b0bec5",
            font=("TkDefaultFont", 10), justify="center")
        self.params_label.grid(row=len(rows), column=0, columnspan=3,
                               sticky="we")
        self.metrics_label = tk.Label(
            model_panel, text="последний ответ: —", bg="#263238",
            fg="#b0bec5", font=("TkDefaultFont", 10), justify="center")
        self.metrics_label.grid(row=len(rows) + 1, column=0, columnspan=3,
                                sticky="we")
        self._refresh_params_line()
        self._update_model_button_state()

        hint = ("Нажимай кнопки или клавиши 1-4. Следи за полосами: "
                "когда они падают, питомцу плохо.")
        self.hint_label = tk.Label(self.root, text=hint, bg="#263238",
                                   fg="#cfd8dc", font=("TkDefaultFont", 11),
                                   justify="center")
        self.hint_label.pack(side=tk.BOTTOM, fill="x", padx=6, pady=(0, 4))

        bars_box = ttk.Frame(self.root)
        bars_box.pack(side=tk.BOTTOM, fill="x", padx=14)
        self.bars = {}
        for title, key in (("Сытость", "hunger"), ("Энергия", "energy"),
                           ("Веселье", "fun"), ("Чистота", "hygiene")):
            row = ttk.Frame(bars_box)
            row.pack(fill="x", pady=3)
            ttk.Label(row, text=title, width=9).pack(side=tk.LEFT)
            bar = ttk.Progressbar(row, maximum=100, mode="determinate")
            bar.pack(side=tk.LEFT, fill="x", expand=True)
            self.bars[key] = bar

        self.mood_label = ttk.Label(bars_box, text="Настроение: —")
        self.mood_label.pack(pady=2)

        self.speech = tk.Label(self.root, text="", wraplength=W - 60,
                               justify="center", relief="solid", bd=1,
                               bg="#37474f", fg="#eceff1",
                               padx=10, pady=8,
                               font=("TkDefaultFont", SPEECH_FONT))
        self.speech.pack(side=tk.BOTTOM, fill="x", padx=24, pady=8)
        self.speech.configure(height=SPEECH_MAX_LINES)  # фикс. высота пузыря
        self.set_speech(f"Привет! Я {self.config['PET_NAME']}.")

        # Canvas — последним: растягивается (expand) и отдаёт место
        self.canvas = tk.Canvas(self.root, width=W, height=CANVAS_H,
                                name="scene", highlightthickness=0)
        self.canvas.pack(fill=tk.BOTH, expand=True)

    def _fit_window_height(self):
        """Высота окна не выше MAX_H: лишнее «съедает» Canvas (expand)."""
        self.root.update_idletasks()
        required = self.root.winfo_reqheight()
        current = self.root.winfo_height()
        if required > current:
            self.root.geometry(f"{W}x{min(required, MAX_H)}")

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
        cw = max(W, canvas.winfo_width())
        ch = max(220, canvas.winfo_height())

        canvas.configure(bg="#101c33" if night else "#cde9f7")
        if night:
            canvas.create_oval(cw - 80, 26, cw - 30, 76, fill="#f5f2da",
                               outline="")
            for x, y, s in ((40, 40, 2), (110, 66, 3), (178, 44, 2),
                            (300, 58, 3), (250, 36, 2)):
                canvas.create_oval(x - s, y - s, x + s, y + s, fill="#e8eeff",
                                   outline="")
        else:
            canvas.create_oval(cw - 96, 18, cw - 34, 80, fill="#ffe066",
                               outline="")

        ground_y = ch - 36
        canvas.create_rectangle(0, ground_y, cw, ch,
                                fill="#8bbf6a" if not night else "#274227",
                                width=0)

        cx = cw // 2
        cy = ch // 2 + int(p["bounce"])
        r = max(48, min(120, int((ch - 110) * 0.34)))
        scale = r / 52.0
        accent, body = p["accent"], p["body"]

        # тело harder и уши
        canvas.create_oval(cx - r, cy - r, cx + r, cy + r, fill=body,
                           outline=accent, width=3)
        ear_dx = 30 * scale
        for dx in (-ear_dx, ear_dx):
            canvas.create_oval(cx + dx - 11 * scale, cy - r - 18 * scale,
                               cx + dx + 11 * scale, cy - r + 10 * scale,
                               fill=accent, outline="")
        # глаза
        eye_y = cy - 12 * scale
        eye_dx = 19 * scale
        ink = "#2b2b2b"
        if p["eye"] == "closed":
            for sx in (-1, 1):
                canvas.create_line(cx + sx * eye_dx - 7 * scale, eye_y,
                                   cx + sx * eye_dx + 7 * scale, eye_y,
                                   fill=ink, width=3)
            canvas.create_text(cx + r - 6, cy - r - 16, text="Z z z",
                               fill="#c7d4e8",
                               font=("TkDefaultFont", 14, "bold"))
        elif p["eye"] == "happy":
            for sx in (-1, 1):
                x0 = cx + sx * eye_dx
                canvas.create_line(x0 - 7 * scale, eye_y, x0,
                                   eye_y - 8 * scale, fill=ink, width=3)
                canvas.create_line(x0, eye_y - 8 * scale,
                                   x0 + 7 * scale, eye_y, fill=ink, width=3)
        elif p["eye"] == "sad_eye":
            for sx in (-1, 1):
                x0 = cx + sx * eye_dx
                canvas.create_line(x0 - 6 * scale, eye_y - 4 * scale,
                                   x0 + 6 * scale, eye_y + 4 * scale,
                                   fill=ink, width=3)
        elif p["eye"] == "half":
            for sx in (-1, 1):
                x0 = cx + sx * eye_dx
                canvas.create_oval(x0 - 6 * scale, eye_y - 3 * scale,
                                   x0 + 6 * scale, eye_y + 5 * scale,
                                   fill=ink, outline="")
        else:
            for sx in (-1, 1):
                x0 = cx + sx * eye_dx
                canvas.create_oval(x0 - 6 * scale, eye_y - 6 * scale,
                                   x0 + 6 * scale, eye_y + 6 * scale,
                                   fill=ink, outline="")

        # рот
        my = cy + 24 * scale
        arc_r = 17 * scale
        if p["mouth"] == "smile":
            canvas.create_arc(cx - arc_r, my - 12 * scale, cx + arc_r,
                              my + 12 * scale, start=200, extent=140,
                              style=tk.ARC, outline=ink, width=3)
        elif p["mouth"] == "sad":
            canvas.create_arc(cx - arc_r, my + 10 * scale, cx + arc_r,
                              my - 8 * scale, start=20, extent=140,
                              style=tk.ARC, outline=ink, width=3)
        elif p["mouth"] in ("o", "o_big"):
            delta = (7 if p["mouth"] == "o" else 10) * scale
            canvas.create_oval(cx - delta, my - 6 * scale, cx + delta,
                               my + 14 * scale, fill="#7c4a43", outline=ink,
                               width=2)
        else:
            canvas.create_line(cx - 12 * scale, my + 4 * scale,
                               cx + 12 * scale, my + 4 * scale, fill=ink,
                               width=3)

        # щёки
        for sx in (-1, 1):
            x0 = cx + sx * 34 * scale
            canvas.create_oval(x0 - 6 * scale, cy + 8 * scale,
                               x0 + 6 * scale, cy + 20 * scale,
                               fill="#f4a3a0", outline="")

        # fx-элементы
        if "drops" in p["fx"]:
            for dx, dy in ((-62, -68), (58, -60), (-44, -50)):
                dx, dy = dx * scale, dy * scale
                canvas.create_oval(cx + dx, cy + dy, cx + dx + 9 * scale,
                                   cy + dy + 9 * scale, fill="#6b7d3f",
                                   outline="")

        if "rumble" in p["fx"]:
            canvas.create_text(cx, cy - r - 34 * scale, text="урр...",
                               fill="#c77", font=("TkDefaultFont", 10,
                                                  "italic"))
        if "yawn" in p["fx"]:
            canvas.create_text(cx + r + 16 * scale, cy + 18 * scale,
                               text="зевок!", fill="#667",
                               font=("TkDefaultFont", 10, "italic"))
        if "tear" in p["fx"]:
            canvas.create_line(cx - eye_dx, eye_y + 8 * scale, cx - eye_dx,
                               eye_y + 20 * scale, fill="#5fa8d3", width=4)
        if "zzz" in p["fx"]:
            canvas.create_text(cx - r - 4 * scale, cy - r - 2,
                               text="z", fill="#9fb4d0",
                               font=("TkDefaultFont", 11, "italic"))

        canvas.create_text(10, 10, anchor="nw",
                           text=(f"{self.pet.name} · настроение: "
                                 + MOOD_LABELS.get(self.pet.mood(),
                                                   self.pet.mood())
                                   .replace("Настроение: ", "")),
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
        self.buttons[3].set_title(
            "⏰ Будить 3" if self.pet.sleeping else "😴 Спать 3")
        self._refresh_params_line()
        self._refresh_metrics_line()

    def set_speech(self, text):
        if isinstance(text, str) and text:
            width = max(320, min(600, self.root.winfo_width() or W))
            try:
                self.speech.configure(wraplength=width - 60)
            except tk.TclError:
                pass
            shown = text[:SPEECH_MAX_CHARS]
            if len(text) > SPEECH_MAX_CHARS:
                shown = shown.rstrip() + "…"
            self.speech.config(text=shown)

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
        self.actions_count += 1
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

    # ——— панель «Модель»: параметры со следующего запроса ———

    def _apply_params(self):
        """Кламп, сохранение в model-settings.json, обновление строки."""
        self.client.params = clamp_params(self.client.params)
        self._refresh_params_line()
        try:
            save_params(self.client.params)
        except OSError:
            pass

    def _refresh_params_line(self):
        p = self.client.params
        txt = (f"temp {p.temperature} · top_p {p.top_p} · "
               f"токены {p.max_tokens} · ctx {p.num_ctx} · "
               f"длина: {LENGTH_RU[p.length_mode]} · {p.model}")
        try:
            self.params_label.config(text=txt)
        except tk.TclError:
            pass

    def _refresh_metrics_line(self):
        m = LAST_METRICS.get()
        if m is None:
            txt = "последний ответ: —"
        else:
            secs = (m.total_duration / 1e9) if m.total_duration else None
            tps = m.tokens_per_sec
            parts = []
            if m.eval_count:
                parts.append(f"{m.eval_count} ток")
            if tps:
                parts.append(f"{tps:.1f} ток/с")
            if secs:
                parts.append(f"{secs:.1f} с")
            txt = ("последний ответ: " + (" · ".join(parts) if parts
                                          else "—"))
        try:
            self.metrics_label.config(text=txt)
        except tk.TclError:
            pass

    def _temp_up(self):
        self.client.params.temperature = min(
            TEMP_MAX, round(self.client.params.temperature + TEMP_STEP, 1))
        self._apply_params()

    def _temp_down(self):
        self.client.params.temperature = max(
            TEMP_MIN, round(self.client.params.temperature - TEMP_STEP, 1))
        self._apply_params()

    def _length_cycle(self):
        modes = LENGTH_MODES
        i = modes.index(self.client.params.length_mode)
        self.client.params.length_mode = modes[(i + 1) % len(modes)]
        self._apply_params()

    def _tokens_cycle(self):
        steps = TOKEN_STEPS
        i = (steps.index(self.client.params.max_tokens) + 1) % len(steps) \
            if self.client.params.max_tokens in steps else 0
        self.client.params.max_tokens = steps[i]
        self._apply_params()

    def _ctx_cycle(self):
        i = (CTX_ALLOWED.index(self.client.params.num_ctx) + 1) \
            % len(CTX_ALLOWED)
        self.client.params.num_ctx = CTX_ALLOWED[i]
        self._apply_params()

    def _preset_cycle(self):
        p = self.client.params
        if p.prompt_variant == "baseline":
            import dataclasses
            self.client.params = dataclasses.replace(OPTIMIZED)
        else:
            import dataclasses
            self.client.params = dataclasses.replace(BASELINE)
        self._apply_params()

    def _model_cycle(self):
        """Цикл по моделям из кэша /api/tags (без сетевых вызовов)."""
        names = [n for n in self.model_cache if n]
        if len(names) < 2:
            return
        i = (self._model_index + 1) % len(names) if names else 0
        self._model_index = i
        self.client.params.model = names[i]
        self._apply_params()

    def _update_model_button_state(self):
        btn = self.model_buttons.get("Модель")
        if btn is None:
            return
        enabled = len([n for n in self.model_cache if n]) >= 2
        btn.set_enabled(enabled)

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
            models = self.client.list_models() or []
            try:
                self.availability.put((online, models))
            except Exception:
                pass

        threading.Thread(target=run, daemon=True).start()

    def _check_loop(self):
        try:
            online, models = self.availability.get_nowait()
            if online != self.online:
                self.online = online
            if models:
                self.model_cache = models
                if self.client.params.model not in models:
                    self._model_index = 0
                self._update_model_button_state()
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
