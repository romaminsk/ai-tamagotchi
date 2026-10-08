"""Тесты bench на синтетике: p95, доли, правило выбора OPTIMIZED."""

import unittest

from tamagotchi.bench import (choose_optimized, parse_ps_line, pct_95,
                              share_cyrillic, share_unique)


class StatsTests(unittest.TestCase):
    def test_pct_95_basic_20_points(self):
        values = list(range(1, 21))  # типично 20 точек
        self.assertAlmostEqual(pct_95(values), 19.05)

    def test_pct_95_single_and_empty(self):
        self.assertEqual(pct_95([7.0]), 7.0)
        self.assertIsNone(pct_95([]))

    def test_pct_95_less_than_20_is_approx_flagged(self):
        from tamagotchi.bench import p95_is_approx
        self.assertTrue(p95_is_approx(19))
        self.assertFalse(p95_is_approx(20))

    def test_shares(self):
        phrases = ["один", "два", "один"]
        self.assertAlmostEqual(share_unique(phrases), 2 / 3)
        self.assertIsNone(share_unique([]))
        self.assertGreater(share_cyrillic("привет"), 0.99)
        self.assertEqual(share_cyrillic("hello"), 0.0)
        self.assertEqual(share_cyrillic("😊"), 0.0)


class PsLineTests(unittest.TestCase):
    def test_parse_rss(self):
        self.assertEqual(parse_ps_line("210000 /usr/local/bin/ollama runner"),
                         210000)
        self.assertEqual(parse_ps_line("  500 ollama serve"), 500)
        self.assertIsNone(parse_ps_line("999 /Applications/Safari.app"))
        self.assertIsNone(parse_ps_line("не число ollama"))


class ChooseOptimizedTests(unittest.TestCase):
    def _c(self, share, t, ln):
        return {"accepted_share": share, "avg_time": t, "avg_len": ln}

    def test_single_passing_candidate(self):
        cand = {"B": self._c(0.95, 4.0, 60),
                "C": self._c(0.5, 4.0, 300),      # мало принятых
                "E": self._c(1.0, 20.0, 400)}     # время > 15 с
        name, why = choose_optimized(cand)
        self.assertEqual(name, "B")
        self.assertIn("chars/sec", why)

    def test_two_passing_close_chars_per_sec_picks_faster(self):
        cand = {"B": self._c(0.95, 10.0, 200),    # 20 симв./с
                "C": self._c(0.95, 9.5, 190)}     # 20 симв./с, но быстрее
        name, why = choose_optimized(cand)
        self.assertEqual(name, "C")
        self.assertIn("скорости", why)

    def test_none_passes_threshold_stays(self):
        cand = {"B": self._c(0.8, 2.0, 20),       # доля < 90%
                "C": self._c(0.85, 5.0, 500),     # доля < 90%
                "E": self._c(1.0, 30.0, 500)}     # время > 15 с
        name, why = choose_optimized(cand)
        self.assertIsNone(name)
        self.assertIn("порог", why)


if __name__ == "__main__":
    unittest.main()
