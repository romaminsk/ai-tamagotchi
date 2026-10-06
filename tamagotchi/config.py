"""Конфигурация тамагочи (только stdlib, значения из окружения)."""

import os

DEFAULT_OLLAMA_URL = "http://localhost:11434"
DEFAULT_OLLAMA_MODEL = "qwen2.5:3b"
DEFAULT_LLM_TIMEOUT = 60
DEFAULT_PET_NAME = "Пушок"

_ALLOWED_HOSTS = {"localhost", "127.0.0.1", "::1"}


class ConfigError(Exception):
    """Понятная ошибка конфигурации."""


def _validate_url(url: str) -> str:
    """Разрешаем обращения только к локальным адресам."""
    parsed = urlparse_custom(url)
    host = parsed.hostname
    if host is None:
        raise ConfigError(
            f"OLLAMA_URL='{url}' не похоже на адрес: ожидается http://localhost:11434"
        )
    if parsed.scheme not in ("http",):
        raise ConfigError(
            f"OLLAMA_URL='{url}': схема должна быть http:// (без TLS, "
            "трафик ходит только внутри вашей машины)"
        )
    if host.lower() not in _ALLOWED_HOSTS:
        raise ConfigError(
            f"OLLAMA_URL='{url}': разрешены только локальные адреса "
            f"(localhost, 127.0.0.1, ::1), получено '{host}'"
        )
    return url


def urlparse_custom(url: str):
    """Мини-разбор URL без внешних библиотек (включая IPv6 [::1])."""
    rest = url
    scheme = "http"
    if "://" in rest:
        scheme, rest = rest.split("://", 1)
    netloc = rest.split("/", 1)[0]
    if netloc.startswith("[") and "]" in netloc:
        host = netloc[1:netloc.index("]")]
    else:
        host = netloc.rsplit(":", 1)[0] if ":" in netloc else netloc
    return type("Parsed", (), {"scheme": scheme, "hostname": host})()


def load_config():
    """Загружает конфигурацию из переменных окружения и валидирует её."""
    env = os.environ
    llm_timeout_raw = env.get("LLM_TIMEOUT", str(DEFAULT_LLM_TIMEOUT))
    try:
        llm_timeout = float(llm_timeout_raw)
    except ValueError:
        raise ConfigError(
            f"LLM_TIMEOUT='{llm_timeout_raw}' не число (секунды)"
        )
    config = {
        "OLLAMA_URL": _validate_url(
            env.get("OLLAMA_URL", DEFAULT_OLLAMA_URL)
        ),
        "OLLAMA_MODEL": env.get("OLLAMA_MODEL", DEFAULT_OLLAMA_MODEL),
        "LLM_TIMEOUT": llm_timeout,
        "PET_NAME": env.get("PET_NAME", DEFAULT_PET_NAME),
    }
    return config
