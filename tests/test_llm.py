"""Тесты LLM-клиента на локальной заглушке http.server (этап 3)."""

import http.server
import json
import socket
import threading
import unittest

import dataclasses

from tamagotchi.llm import (LLMClient, LastMetrics, _clean_phrase,
                            build_system_prompt, build_system_prompt_optimized,
                            build_user_message, build_user_message_optimized,
                            parse_metrics, ReplyMetrics)
from tamagotchi.modelsettings import BASELINE, ModelParams


def baseline() -> ModelParams:
    return dataclasses.replace(BASELINE)


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class _Handler(http.server.BaseHTTPRequestHandler):
    """Заглушка: POST /api/chat, GET /api/tags; поведение через self.server."""

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
        self.server.last_path = self.path
        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length)
        try:
            self.server.last_body = json.loads(raw.decode("utf-8"))
        except Exception:
            self.server.last_body = {}
        mode = self.server.mode
        if mode == "ok":
            body = json.dumps({
                "message": {"role": "assistant",
                            "content": "  \"Привет! Я рад.\"\n"},
                "eval_count": 14,
                "eval_duration": 1_000_000_000,
                "prompt_eval_count": 32,
                "prompt_eval_duration": 264_000_000,
                "load_duration": 0,
                "total_duration": 2_000_000_000,
            }).encode()
            self._reply(200, body)
        elif mode == "empty":
            self._reply(200, b'{"message": {"content": "  "}}')
        elif mode == "slow":
            self._reply(200, b"{}", 2.2)
        elif mode == "cjk":
            self._reply(200, json.dumps(
                {"message": {"content": "こんにちは Привет."}}).encode())
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
        self.last_body = {}
        self.last_path = ""


class LLMStubTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.port = _free_port()
        cls.httpd = StubServer(cls.port)
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()

    def _client(self, timeout=5.0, params: ModelParams | None = None
                ) -> LLMClient:
        return LLMClient(f"http://127.0.0.1:{self.port}", "qwen2.5:3b",
                         timeout, params=params)

    def test_chat_ok_and_cleanup(self):
        reply = self._client(params=baseline()).chat(
            "Пушок", "happy", {"hunger": 80}, "fed")
        self.assertEqual(reply.text, "Привет! Я рад.")
        self.assertIsNone(reply.rejected_reason)
        self.assertEqual(reply.metrics.eval_count, 14)

    def test_request_hits_api_chat_with_options(self):
        params = ModelParams(temperature=0.7, top_p=0.85, top_k=33,
                             repeat_penalty=1.2, max_tokens=150,
                             num_ctx=4096, model="qwen2.5:3b",
                             prompt_variant="optimized")
        self._client(params=params).chat("П", "happy", {"hunger": 80}, "fed")
        body = self.httpd.last_body
        self.assertIn("/api/chat", self.httpd.last_path)
        self.assertFalse(body.get("stream"))
        self.assertEqual(body.get("keep_alive"), "10m")
        options = body["options"]
        self.assertEqual(options["temperature"], 0.7)
        self.assertEqual(options["top_p"], 0.85)
        self.assertEqual(options["top_k"], 33)
        self.assertEqual(options["repeat_penalty"], 1.2)
        self.assertEqual(options["num_predict"], 150)
        self.assertEqual(options["num_ctx"], 4096)

    def test_baseline_sends_old_options_only(self):
        self._client(params=baseline()).chat(
            "П", "happy", {"hunger": 80}, "fed")
        options = self.httpd.last_body["options"]
        self.assertEqual(set(options), {"temperature", "top_p",
                                        "num_predict", "num_ctx"})
        self.assertEqual(options["temperature"], 1.0)
        self.assertEqual(options["num_predict"], 60)
        self.assertIn("только по-русски", self.httpd.last_body["messages"][0]
                      ["content"])
        self.assertIn("одну короткую фразу", self.httpd.last_body["messages"][1]
                      ["content"])

    def test_chat_empty_is_none(self):
        self.httpd.mode = "empty"
        try:
            reply = self._client(params=baseline()).chat(
                "П", "neutral", {}, None)
            self.assertIsNone(reply.text)
            self.assertEqual(reply.rejected_reason, "empty")
        finally:
            self.httpd.mode = "ok"

    def test_chat_500_is_none(self):
        self.httpd.mode = "err"
        try:
            reply = self._client(params=baseline()).chat(
                "П", "neutral", {}, None)
            self.assertIsNone(reply.text)
            self.assertEqual(reply.rejected_reason, "unavailable")
        finally:
            self.httpd.mode = "ok"

    def test_chat_timeout_is_none(self):
        self.httpd.mode = "slow"
        try:
            reply = self._client(timeout=0.5, params=baseline()).chat(
                "П", "neutral", {}, None)
            self.assertIsNone(reply.text)
            self.assertEqual(reply.rejected_reason, "unavailable")
        finally:
            self.httpd.mode = "ok"

    def test_rejected_reason_cjk_and_non_cyrillic(self):
        self.httpd.mode = "cjk"
        try:
            reply = self._client(params=baseline()).chat(
                "П", "neutral", {}, None)
            # контент заглушки содержит иероглифы
            self.assertIsNone(reply.text)
            self.assertEqual(reply.rejected_reason, "cjk")
        finally:
            self.httpd.mode = "ok"

    def test_rejected_reason_repeat(self):
        client = self._client(params=baseline())
        self.assertEqual(client.chat("П", "neutral", {}, None).text,
                         "Привет! Я рад.")
        reply = client.chat("П", "neutral", {}, None)
        self.assertIsNone(reply.text)
        self.assertEqual(reply.rejected_reason, "repeat")

    def test_closed_port_is_none(self):
        client = LLMClient("http://127.0.0.1:1", "m", 1.0)
        self.assertIsNone(client.chat("П", "neutral", {}, None).text)

    def test_check_available_true_false(self):
        self.assertTrue(self._client(params=baseline()).check_available())
        self.httpd.tags_ok = False
        try:
            self.assertFalse(
                self._client(params=baseline()).check_available())
        finally:
            self.httpd.tags_ok = True

    def test_missing_model_is_false(self):
        self.httpd.model_names = ["other:latest"]
        try:
            self.assertFalse(
                self._client(params=baseline()).check_available())
        finally:
            self.httpd.model_names = ["qwen2.5:3b"]


class CleanTests(unittest.TestCase):
    def test_clean_phrase(self):
        self.assertEqual(_clean_phrase('  "Хорошо!" \n'), "Хорошо!")
        self.assertEqual(_clean_phrase("first\nsecond"), "first")
        self.assertEqual(_clean_phrase("   "), None)
        self.assertEqual(_clean_phrase(None), None)
        # старый жёсткий лимит 80 сохранён по умолчанию (short)
        self.assertEqual(_clean_phrase("a" * 120),
                         "a" * 79 + "…")
        self.assertEqual(_clean_phrase("Кавычки «внутри» остаются"),
                         "Кавычки «внутри» остаются")

    def test_clean_phrase_modes(self):
        text = "Абзац один. Второй всё ещё идёт. Третий."
        self.assertEqual(_clean_phrase(text, 80), text)  # short: влезло
        long_text = _clean_phrase(text + " " + "Длинно" * 40, 120)
        self.assertLessEqual(len(long_text), 120)

    def test_truncate_at_sentence_boundary(self):
        text = ("Первое предложение тут. Второе предложение заканчивается "
                "точкой. " + "И второй ещё тянет" * 20)
        cut = _clean_phrase(text, 100)
        self.assertTrue(cut.endswith("."))
        self.assertEqual(cut, "Первое предложение тут. Второе предложение "
                              "заканчивается точкой.")

    def test_truncate_no_sentence_by_word(self):
        text = "полно слов и совсем никаких точек " + "о" * 60
        cut = _clean_phrase(text, 40)
        self.assertTrue(cut.endswith("…"))
        self.assertLessEqual(len(cut), 41)
        self.assertIn("слов", cut)

    def test_modes_limits(self):
        text = "Целая фраза. " * 60
        for mode, limit in (("short", 80), ("medium", 240), ("long", 520)):
            cut = _clean_phrase(text, limit)
            self.assertLessEqual(len(cut), limit)
            self.assertEqual(limit,
                             {"short": 80, "medium": 240, "long": 520}[mode])


class UserMessageOptimizedTests(unittest.TestCase):
    def test_state_structure_and_restrictions(self):
        msg = build_user_message_optimized(
            "Пушок", "hungry", {"hunger": 10, "energy": 50, "fun": 60,
                                "hygiene": 70}, "fed",
            style="сарказм", seed=42, recent=["Прошлая реплика"],
            length_mode="long")
        self.assertIn("Состояние:", msg)
        self.assertIn("сытость=10", msg)
        self.assertIn("энергия=50", msg)
        self.assertIn("чистота=70", msg)
        self.assertIn("настроение=hungry", msg)
        self.assertIn("покормили", msg)
        self.assertIn("Прошлая реплика", msg)
        self.assertIn("42", msg)

    def test_system_optimized_has_length_rules(self):
        from tamagotchi.llm import _LENGTH_RULES
        for mode in ("short", "medium", "long"):
            prompt = build_system_prompt_optimized("Пушок", mode)
            low = prompt.lower()
            self.assertIn("по-русски", low)
            self.assertIn("без мата", low)
            self.assertIn("саркаст", low)
            self.assertIn("не оскорбля", low)
            self.assertIn("без призывов к насилию", low)
            self.assertIn("примеры реплик", low)
            rule = _LENGTH_RULES[mode]
            self.assertTrue(rule)


class MetricsTests(unittest.TestCase):
    def test_parse_metrics_tokens_per_sec(self):
        data = {"eval_count": 10, "eval_duration": 2_000_000_000,
                "prompt_eval_count": 100, "prompt_eval_duration": 5_000_000,
                "load_duration": 200, "total_duration": 3_000_000}
        m = parse_metrics(data)
        self.assertAlmostEqual(m.tokens_per_sec, 5.0)
        self.assertEqual(m.eval_count, 10)
        self.assertEqual(m.load_duration, 200)
        self.assertEqual(m.total_duration, 3_000_000)

    def test_parse_metrics_zero_and_missing(self):
        m = parse_metrics({})
        self.assertIsNone(m.tokens_per_sec)
        self.assertIsNone(m.eval_count)
        m2 = parse_metrics({"eval_count": 0, "eval_duration": 0})
        self.assertIsNone(m2.tokens_per_sec)
        m3 = parse_metrics({"eval_count": 5, "eval_duration": 0})
        self.assertIsNone(m3.tokens_per_sec)
        self.assertEqual(m3.eval_count, 5)

    def test_last_metrics_singleton_threadsafe(self):
        lm = LastMetrics()
        lm.set(ReplyMetrics(tokens_per_sec=1.0))
        self.assertEqual(lm.get().tokens_per_sec, 1.0)
        lm.set(None)
        self.assertIsNone(lm.get())

    def test_baseline_prompt_unchanged(self):
        prompt = build_system_prompt("Пушок")
        self.assertIn("до 80 символов", prompt)
        self.assertIn("жалобу", prompt)
        self.assertIn("здоров", prompt)
        msg = build_user_message("Пушок", "sad", {"hunger": 10}, "idle",
                                 style="сарказм", seed=1,
                                 recent=["Реплика старая"])
        self.assertIn("Параметры:", msg)
        self.assertIn("Стиль реплики: сарказм", msg)
        self.assertIn("Реплика старая", msg)
        self.assertIn("одну короткую фразу", msg)


if __name__ == "__main__":
    unittest.main()
