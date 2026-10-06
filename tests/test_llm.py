"""Тесты LLM-клиента на локальной заглушке http.server (этап 3)."""

import http.server
import json
import socket
import threading
import unittest

from tamagotchi.llm import LLMClient, _clean_phrase, build_user_message


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class _Handler(http.server.BaseHTTPRequestHandler):
    """Заглушка: /v1/chat/completions, /api/tags; поведение через self.server."""

    def log_message(self, *args):
        pass

    def _reply(self, status: int, body: bytes, delay: float = 0.0):
        if delay:
            import time
            time.sleep(delay)
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        self.rfile.read(length)
        mode = self.server.mode
        if mode == "ok":
            body = json.dumps(
                {"choices": [{"message": {"content": "  \"Привет! Я рад.\"\n"}}]}
            ).encode()
            self._reply(200, body)
        elif mode == "empty":
            self._reply(200, b'{"choices": [{"message": {"content": "  "}}]}')
        elif mode == "slow":
            self._reply(200, b"{}" * 0, 2.2)
        else:
            self._reply(500, b'{"error": "boom"}')

    def do_GET(self):
        if self.server.tags_ok:
            names = self.server.model_names
            body = json.dumps({"models": [{"name": n} for n in names]})
            self._reply(200, body.encode())
        else:
            self._reply(500, b"{}")


class StubServer(http.server.HTTPServer):
    daemon_threads = True

    def __init__(self, port):
        super().__init__(("127.0.0.1", port), _Handler)
        self.mode = "ok"
        self.tags_ok = True
        self.model_names = ["qwen2.5:3b"]


class LLMStubTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.port = _free_port()
        cls.httpd = StubServer(cls.port)
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()

    def _client(self, timeout=5.0) -> LLMClient:
        return LLMClient(f"http://127.0.0.1:{self.port}", "qwen2.5:3b", timeout)

    def test_chat_ok_and_cleanup(self):
        phrase = self._client().chat("Пушок", "happy", {"hunger": 80}, "fed")
        self.assertEqual(phrase, "Привет! Я рад.")

    def test_chat_empty_is_none(self):
        self.httpd.mode = "empty"
        try:
            self.assertIsNone(self._client().chat("П", "neutral", {}, None))
        finally:
            self.httpd.mode = "ok"

    def test_chat_500_is_none(self):
        self.httpd.mode = "err"
        try:
            self.assertIsNone(self._client().chat("П", "neutral", {}, None))
        finally:
            self.httpd.mode = "ok"

    def test_chat_timeout_is_none(self):
        self.httpd.mode = "slow"
        try:
            self.assertIsNone(self._client(timeout=0.5)
                              .chat("П", "neutral", {}, None))
        finally:
            self.httpd.mode = "ok"

    def test_closed_port_is_none(self):
        client = LLMClient("http://127.0.0.1:1", "m", 1.0)
        self.assertIsNone(client.chat("П", "neutral", {}, None))

    def test_check_available_true_false(self):
        self.assertTrue(self._client().check_available())
        self.httpd.tags_ok = False
        try:
            self.assertFalse(self._client().check_available())
        finally:
            self.httpd.tags_ok = True

    def test_missing_model_is_false(self):
        self.httpd.model_names = ["other:latest"]
        try:
            self.assertFalse(self._client().check_available())
        finally:
            self.httpd.model_names = ["qwen2.5:3b"]


class CleanTests(unittest.TestCase):
    def test_clean_phrase(self):
        self.assertEqual(_clean_phrase('  "Хорошо!" \n'), "Хорошо!")
        self.assertEqual(_clean_phrase("first\nsecond"), "first")
        self.assertEqual(_clean_phrase("   "), None)
        self.assertEqual(_clean_phrase(None), None)
        self.assertEqual(_clean_phrase("a" * 120), "a" * 80)
        self.assertEqual(_clean_phrase("Кавычки «внутри» остаются"),
                         "Кавычки «внутри» остаются")

    def test_user_message_contains_state(self):
        msg = build_user_message("Пушок", "hungry",
                                 {"hunger": 10, "energy": 50, "fun": 50,
                                  "hygiene": 50}, "fed")
        self.assertIn("Пушок", msg)
        self.assertIn("hungry", msg)
        self.assertIn("сытость=10", msg)


if __name__ == "__main__":
    unittest.main()
