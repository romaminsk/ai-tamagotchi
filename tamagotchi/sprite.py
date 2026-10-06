"""Чистые функции внешнего вида питомца (тестируются без Tk)."""

MOOD_COLORS = {
    "happy": ("#fff3d6", "#f7b32b"),
    "neutral": ("#f4f0ff", "#9a8ce0"),
    "hungry": ("#ffe8e0", "#e07a5f"),
    "dirty": ("#e8f0d8", "#7d9b4e"),
    "sleepy": ("#e6eef7", "#6f8fb5"),
    "sad": ("#ece6f2", "#8d7ab5"),
    "sleeping": ("#e2e8f0", "#4a6274"),
}

MOOD_EYE = {          # ("open", "closed", "happy")
    "happy": "happy",
    "neutral": "open",
    "hungry": "open",
    "dirty": "sad_eye",
    "sleepy": "half",
    "sad": "sad_eye",
    "sleeping": "closed",
}

MOOD_MOUTH = {        # smile / sad / o / flat / sleep
    "happy": "smile",
    "neutral": "flat",
    "hungry": "o",
    "dirty": "sad",
    "sleepy": "o_big",
    "sad": "sad",
    "sleeping": "flat",
}

MOOD_FX = {           # дополнительные элементы
    "happy": [],
    "neutral": [],
    "hungry": ["rumble"],
    "dirty": ["drops"],
    "sleepy": ["yawn"],
    "sad": ["tear"],
    "sleeping": ["zzz"],
}


def sprite_params(mood: str, blink: bool = False,
                  bounce: float = 0.0) -> dict:
    """Параметры рисунка по настроению: чистая функция, без Tk.

    bounce — смещение тела по вертикали в пикселях (покачивание).
    blink  — глаза закрыты на миг (моргание).
    """
    mood = mood if mood in MOOD_EYE else "neutral"
    body, accent = MOOD_COLORS[mood]
    eye = "closed" if blink else MOOD_EYE[mood]
    return {
        "body": body,
        "accent": accent,
        "eye": eye,
        "mouth": MOOD_MOUTH[mood],
        "fx": list(MOOD_FX[mood]),
        "bounce": bounce,
        "ear_wiggle": mood == "happy",
    }
