"""Тесты реплик: фолбэки ≥8 уникальных, антиповтор, промпт со стилем."""

import http.server
import json
import socket
import threading
import unittest

from tamagotchi import fallback as fb
from tamagotchi.llm import LLMClient, build_user_message


class FallbackContentTests(unittest.TestCase):
    def test_min_eight_unique_per_key(self):
        for group in (fb.FALLBACK_PHRASES, fb.EVENT_PHRASES):
            for key, options in group.items():
                self.assertGreaterEqual(len(set(options)), 8,
                                        msg=f"{key}: <8 уникальных")
                self.assertEqual(len(set(options)), len(options),
                                 msg=f"{key}: дубликаты")

    def test_random_cycle_no_consecutive_repeats(self):
        seen_prev = {}
        for mood in list(fb.FALLBACK_PHRASES):
            for _ in range(40):
                phrase = fb.get_fallback(mood=mood)
                self.assertNotEqual(phrase, seen_prev.get(mood),
                                    msg=f"{mood}: подряд повтор")
                seen_prev[mood] = phrase
        for event in list(fb.EVENT_PHRASES):
            for _ in range(40):
                phrase = fb.get_fallback(mood="neutral", event=event)
                self.assertNotEqual(phrase, seen_prev.get(event),
                                    msg=f"{event}: подряд повтор")
                seen_prev[event] = phrase

    def test_wake_phrases_meaningful(self):
        for phrase in fb.EVENT_PHRASES["woke_up"]:
            self.assertTrue(phrase,
                            "пустой строки быть не должно")


# ——— заглушка: отдаёт фиксированный контент ———

class _RepeatHandler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        self.rfile.read(length)
        body = json.dumps({
            "message": {"role": "assistant",
                        "content": "Однообразная реплика детектор."}
        }).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def _free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class PromptTests(unittest.TestCase):
    def test_user_message_contains_style_and_do_not_repeat(self):
        msg = build_user_message(
            "Пушок", "sad", {"hunger": 10, "energy": 50, "fun": 20,
                             "hygiene": 60},
            "idle", style="театральная драма", seed=777,
            recent=["Старая реплика один", "Старая реплика два"])
        self.assertIn("театральная драма", msg)
        self.assertIn("Не повторяй эти фразы", msg)
        self.assertIn("Старая реплика один", msg)
        self.assertIn("777", msg)

    def test_styles_list_nonempty(self):
        from tamagotchi.llm import STYLES
        self.assertGreaterEqual(len(STYLES), 8)
        for style in STYLES:
            self.assertTrue(style)

    def test_system_prompt_has_sass_and_rules(self):
        from tamagotchi.llm import build_system_prompt
        prompt = build_system_prompt("Пушок")
        low = prompt.lower()
        self.assertIn("по-русски", low)
        self.assertIn("без мата", low)
        self.assertIn("саркаст", low)
        self.assertIn("не оскорбля", low)
        self.assertIn("без призывов к насилию", low)


class AntiRepeatStubTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.port = _free_port()
        cls.httpd = http.server.HTTPServer(
            ("127.0.0.1", cls.port), _RepeatHandler)
        cls.httpd.daemon_threads = True
        threading.Thread(target=cls.httpd.serve_forever,
                         daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()

    def test_repeat_reply_rejected(self):
        client = LLMClient(f"http://127.0.0.1:{self.port}", "qwen2.5:3b", 5.0)
        first = client.chat("Пушок", "neutral", {"hunger": 50}, "idle").text
        self.assertEqual(first, "Однообразная реплика детектор.")
        second = client.chat("Пушок", "neutral", {"hunger": 50}, "idle").text
        self.assertIsNone(second)

    def test_recent_window_trims(self):
        client = LLMClient(f"http://127.0.0.1:{self.port}", "qwen2.5:3b", 5.0)
        client._remember("а")
        client._remember("б")
        client._remember("в")
        self.assertEqual(client.recent, ["а", "б", "в"])
        for i in range(7):
            client._remember(f"фраза-{i}")
        self.assertLessEqual(len(client.recent), client.RECENT_LIMIT)
        self.assertNotIn("а", client.recent)
        self.assertIn("фраза-6", client.recent)


if __name__ == "__main__":
    unittest.main()
