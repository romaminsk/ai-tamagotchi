"""Чистая игровая логика питомца. Правила держит код, модель только говорит."""

TICK_SECONDS = 5

START = {"hunger": 80.0, "energy": 80.0, "fun": 80.0, "hygiene": 80.0}

# Энергия: в бодрствовании тратится втрое медленнее прежнего (-0.6 -> -0.2),
# во сне растё втрое быстрее прежнего (+2.5 -> +7.5).
# Восстановление с низкого уровня: 100/7.5 ≈ 14 тиков ≈ 70 секунд < 2-3 мин.
ENERGY_AWAKE_LOSS = -0.2
ENERGY_SLEEP_GAIN = 7.5

AWAKE_DECAY = {
    "hunger": -1.0,
    "energy": ENERGY_AWAKE_LOSS,
    "fun": -0.8,
    "hygiene": -0.5,
}
SLEEP_DECAY = {
    "hunger": -0.4,
    "energy": ENERGY_SLEEP_GAIN,
    "fun": -0.2,
    "hygiene": -0.2,
}

_P = ("hunger", "energy", "fun", "hygiene")


def _clamp(value: float) -> float:
    return max(0.0, min(100.0, value))


class Pet:
    """Питомец. Не умирает; все параметры зажаты в 0..100."""

    def __init__(self, name: str = "Пушок"):
        self.name = name
        self.hunger = START["hunger"]
        self.energy = START["energy"]
        self.fun = START["fun"]
        self.hygiene = START["hygiene"]
        self.age_ticks = 0
        self.sleeping = False

    # ——— состояние ———

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "hunger": self.hunger,
            "energy": self.energy,
            "fun": self.fun,
            "hygiene": self.hygiene,
            "age_ticks": self.age_ticks,
            "sleeping": self.sleeping,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "Pet":
        pet = cls(name=data.get("name", "Пушок"))
        for key in _P:
            value = data.get(key, START[key])
            try:
                value = float(value)
            except (TypeError, ValueError):
                value = START[key]
            setattr(pet, key, _clamp(value))
        pet.age_ticks = int(data.get("age_ticks", 0) or 0)
        pet.sleeping = bool(data.get("sleeping", False))
        return pet

    # ——— тики ———

    def tick(self) -> None:
        """Один тик времени (TICK_SECONDS секунд)."""
        self.age_ticks += 1
        decay = SLEEP_DECAY if self.sleeping else AWAKE_DECAY
        for key, delta in decay.items():
            setattr(self, key, _clamp(getattr(self, key) + delta))
        if self.sleeping and self.energy >= 100:
            self.wake()

    # ——— действия ———

    def feed(self) -> str:
        if self.sleeping:
            return "is_sleeping"
        if self.hunger > 90:
            return "too_full"
        self.hunger = _clamp(self.hunger + 30)
        self.hygiene = _clamp(self.hygiene - 3)
        return "fed"

    def play(self) -> str:
        if self.sleeping:
            return "is_sleeping"
        if self.energy < 20:
            return "too_tired"
        self.fun = _clamp(self.fun + 25)
        self.energy = _clamp(self.energy - 10)
        self.hunger = _clamp(self.hunger - 5)
        return "played"

    def sleep(self) -> str:
        if self.sleeping:
            return "is_sleeping"
        if self.energy > 95:
            return "not_sleepy"
        self.sleeping = True
        return "sleep_started"

    def wake(self) -> str:
        if not self.sleeping:
            return "not_sleeping"
        self.sleeping = False
        return "woke_up"

    def clean(self) -> str:
        if self.sleeping:
            return "is_sleeping"
        self.hygiene = _clamp(self.hygiene + 40)
        return "cleaned"

    # ——— настроение ———

    def mood(self) -> str:
        if self.sleeping:
            return "sleeping"
        if self.hunger < 25:
            return "hungry"
        if self.hygiene < 25:
            return "dirty"
        if self.energy < 25:
            return "sleepy"
        if self.fun < 30:
            return "sad"
        if self.hunger >= 60 and self.energy >= 60 \
                and self.fun >= 60 and self.hygiene >= 60:
            return "happy"
        return "neutral"
