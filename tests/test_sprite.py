"""Тесты чистых параметров рисунка (без Tk, этап 4)."""

import unittest

from tamagotchi.sprite import sprite_params


class SpriteTests(unittest.TestCase):
    def test_all_moods_have_params(self):
        moods = ["happy", "neutral", "hungry", "dirty", "sleepy", "sad",
                 "sleeping"]
        for mood in moods:
            p = sprite_params(mood)
            self.assertTrue(p["body"].startswith("#"))
            self.assertTrue(p["accent"].startswith("#"))
            self.assertIn(p["eye"], ("open", "closed", "happy", "sad_eye",
                                     "half"))
            self.assertIsInstance(p["fx"], list)

    def test_distinct_moods(self):
        self.assertEqual(sprite_params("sleeping")["eye"], "closed")
        self.assertIn("zzz", sprite_params("sleeping")["fx"])
        self.assertIn("drops", sprite_params("dirty")["fx"])
        self.assertIn("rumble", sprite_params("hungry")["fx"])
        self.assertEqual(sprite_params("happy")["mouth"], "smile")
        self.assertEqual(sprite_params("sad")["mouth"], "sad")
        self.assertEqual(sprite_params("sleepy")["mouth"], "o_big")

    def test_blink_overrides_eye(self):
        self.assertEqual(sprite_params("happy", blink=True)["eye"], "closed")

    def test_bounce_passthrough(self):
        self.assertEqual(sprite_params("happy", bounce=7.5)["bounce"], 7.5)

    def test_unknown_mood_falls_back(self):
        self.assertEqual(sprite_params("чешуя")["mouth"], "flat")


if __name__ == "__main__":
    unittest.main()
