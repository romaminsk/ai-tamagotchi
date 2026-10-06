"""Тесты «без облака»: нет внешних URL/ключей/заголовков в коде пакета."""

import os
import unittest

from tamagotchi.config import ConfigError, load_config

PACKAGE_DIR = os.path.join(os.path.dirname(__file__), "..", "tamagotchi")
FORBIDDEN = ("https://", "api_key", "Bearer")


class OfflineTests(unittest.TestCase):
    def test_no_external_urls_keys_bearer(self):
        violations = []
        for name in sorted(os.listdir(PACKAGE_DIR)):
            if not name.endswith(".py"):
                continue
            with open(os.path.join(PACKAGE_DIR, name), encoding="utf-8") as f:
                text = f.read()
            for token in FORBIDDEN:
                if token in text:
                    violations.append(f"{name}: {token!r}")
        self.assertEqual(violations, [])

    def test_local_url_accepted(self):
        for url in ("http://localhost:11434", "http://127.0.0.1:11434"):
            cfg = load_config_with_url(url + "/")
            self.assertEqual(cfg["OLLAMA_URL"].rstrip("/"), url)
        cfg6 = load_config_with_url("http://[::1]:11434")
        self.assertEqual(cfg6["OLLAMA_URL"], "http://[::1]:11434")

    def test_remote_url_rejected(self):
        for url in ("http://example.com:11434", "https://api.openai.com/v1",
                    "http://192.168.1.10:11434"):
            with self.assertRaises(ConfigError):
                load_config_with_url(url)

    def test_bad_timeout_rejected(self):
        with self.assertRaises(ConfigError):
            load_config_with_url("http://localhost:11434",
                                 extra_env={"LLM_TIMEOUT": "abc"})


def load_config_with_url(url, extra_env=None):
    import os
    saved = {}
    env = {"OLLAMA_URL": url}
    if extra_env:
        env.update(extra_env)
    for key, value in env.items():
        saved[key] = os.environ.get(key)
        os.environ[key] = value
    try:
        return load_config()
    finally:
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


if __name__ == "__main__":
    unittest.main()
