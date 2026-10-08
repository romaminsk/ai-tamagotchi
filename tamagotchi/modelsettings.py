"""Параметры модели: dataclass, пресеты, хранение в ~/.ai-tamagotchi."""

import copy
import dataclasses
import json
import os
import tempfile
from dataclasses import dataclass

TEMP_MIN, TEMP_MAX = 0.1, 1.5
TOPP_MIN, TOPP_MAX = 0.5, 1.0
TOPK_MIN, TOPK_MAX = 1, 100
RPEN_MIN, RPEN_MAX = 1.0, 1.5
TOK_MIN, TOK_MAX = 40, 400
CTX_ALLOWED = (2048, 4096, 8192)
LENGTH_MODES = ("short", "medium", "long")
PROMPT_VARIANTS = ("baseline", "optimized")
SETTINGS_FILE = "model-settings.json"

# Дефолтные (не задававшиеся старым кодом) значения Ollama для baseline
OLLAMA_DEFAULT_TOP_K = 40
OLLAMA_DEFAULT_REPEAT_PENALTY = 1.1


@dataclass
class ModelParams:
    temperature: float = 0.8
    top_p: float = 0.9
    top_k: int = 40
    repeat_penalty: float = 1.15
    max_tokens: int = 220
    num_ctx: int = 4096
    length_mode: str = "long"
    model: str = "qwen2.5:3b"
    prompt_variant: str = "optimized"


def _clamp(value: float, lo: float, hi: float) -> float:
    return min(hi, max(lo, value))


def _snap(value: float, allowed: tuple[int, ...]) -> int:
    return min(allowed, key=lambda x: abs(x - value))


def clamp_params(p: ModelParams) -> ModelParams:
    """Кламп значений в допустимые границы (на месте)."""
    p.temperature = _clamp(float(p.temperature), TEMP_MIN, TEMP_MAX)
    p.top_p = _clamp(float(p.top_p), TOPP_MIN, TOPP_MAX)
    p.top_k = int(_clamp(int(p.top_k), TOPK_MIN, TOPK_MAX))
    p.repeat_penalty = _clamp(float(p.repeat_penalty), RPEN_MIN, RPEN_MAX)
    p.max_tokens = int(_clamp(int(p.max_tokens), TOK_MIN, TOK_MAX))
    p.num_ctx = _snap(int(p.num_ctx), CTX_ALLOWED)
    if p.length_mode not in LENGTH_MODES:
        p.length_mode = "long"
    if p.prompt_variant not in PROMPT_VARIANTS:
        p.prompt_variant = "optimized"
    p.model = str(p.model) if p.model else OPTIMIZED.model
    return p


# Пресет baseline: зафиксированные параметры старого кода (этап 0).
BASELINE = ModelParams(
    temperature=1.0,          # тем же числом шло в payload
    top_p=0.95,               # тем же числом шло в payload
    top_k=OLLAMA_DEFAULT_TOP_K,
    repeat_penalty=OLLAMA_DEFAULT_REPEAT_PENALTY,
    max_tokens=60,
    num_ctx=4096,             # дефолтный CONTEXT этой машины (ollama ps)
    length_mode="short",
    model="qwen2.5:3b",
    prompt_variant="baseline",
)

# Пресет optimized: итог перемера (docs/optimization-report.md):
# правило v3 (принято >= 90%, время <= 15 с, максимум символов/с; при
# равенстве в 10% — более быстрая) выбрало M-medium: 13.4 симв/с против
# 13.7 у B (в пределах 10%), тай-брейк по скорости (6.70 с против
# 11.31 с). Значения пресета:
OPTIMIZED = ModelParams(
    temperature=1.0,          # как в M-medium (baseline-параметры)
    top_p=0.95,
    top_k=40,
    repeat_penalty=1.1,
    max_tokens=160,
    num_ctx=4096,
    length_mode="medium",     # 2–3 предложения до 240 символов
    model="qwen2.5:3b",
    prompt_variant="optimized",
)


def settings_path() -> str:
    home = os.environ.get("TAMAGOTCHI_HOME") or os.path.join(
        os.path.expanduser("~"), ".ai-tamagotchi")
    return os.path.join(home, SETTINGS_FILE)


def save_params(params: ModelParams, path: str | None = None) -> str:
    """Атомарная запись настроек: tmp + os.replace."""
    path = path or settings_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    data = dataclasses.asdict(clamp_params(params))
    fd, tmp = tempfile.mkstemp(prefix=".model-settings-", dir=os.path.dirname(
        path))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=1)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return path


def load_params(path: str | None = None) -> ModelParams:
    """Читает settings; битый/неполный -> OPTIMIZED; кламп значений."""
    path = path or settings_path()
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            return copy.deepcopy(OPTIMIZED)
        known = {f.name for f in dataclasses.fields(ModelParams)}
        patch = {k: v for k, v in data.items() if k in known}
        params = dataclasses.replace(OPTIMIZED, **patch)
        return clamp_params(params)
    except (OSError, ValueError, TypeError):
        return copy.deepcopy(OPTIMIZED)
