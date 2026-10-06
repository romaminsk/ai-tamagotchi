"""Тесты фильтра нерусских вкраплений на заглушке http.server."""

import http.server
import json
import socket
import threading
import unittest

from tamagotchi.llm import LLMClient, _is_acceptable


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class _FilterHandler(http.server.BaseHTTPRequestHandler):
    content = "Привет, я рад тебя видеть!"

    def log_message(self, *args):
        pass

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        self.rfile.read(length)
        body = json.dumps(
            {"choices": [{"message": {"content": self.server.content}}]}
        ).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class FilterStub(http.server.HTTPServer):
    daemon_threads = True


class FilterStubTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.port = _free_port()
        cls.httpd = FilterStub(("127.0.0.1", cls.port), _FilterHandler)
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()

    def _chat(self):
        return LLMClient(f"http://127.0.0.1:{self.port}", "qwen2.5:3b",
                         5.0).chat("Пушок", "happy", {"hunger": 80}, "greet")

    def test_cjk_returns_none(self):
        self.httpd.content = "Привет 主人, я рад!"
        self.assertIsNone(self._chat())

    def test_latin_over_30pct_returns_none(self):
        self.httpd.content = "Hello my friend, как дела?"  # латиница > 30%
        self.assertIsNone(self._chat())

    def test_russian_passes(self):
        self.httpd.content = "  «Ням-ням, как вкусно!» "
        self.assertEqual(self._chat(), "Ням-ням, как вкусно!")

    def test_russian_with_digits_punct_passes(self):
        self.httpd.content = "Мне 5 лет, и я счастлив! :)"
        self.assertEqual(self._chat(), "Мне 5 лет, и я счастлив! :)")


class FilterUnitTests(unittest.TestCase):
    def test_units(self):
        self.assertFalse(_is_acceptable("主人 привет"))
        self.assertFalse(_is_acceptable("Привет カタカナ"))
        self.assertFalse(_is_acceptable("Zzz 안녕"))
        self.assertTrue(_is_acceptable("Я почти-fill, но кириллицы больше"))
        self.assertFalse(_is_acceptable("filler filler filler слов мало"))
        self.assertFalse(_is_acceptable(None))
        self.assertFalse(_is_acceptable(""))
        self.assertFalse(_is_acceptable("😊 😊 😊"))  # только не-буквы
        self.assertTrue(_is_acceptable("Ну ладно, пойду спать"))


if __name__ == "__main__":
    unittest.main()
