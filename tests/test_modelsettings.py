"""Тесты modelsettings: кламп, битый файл, атомарность, пресеты."""

import copy
import json
import os
import unittest

from tamagotchi import modelsettings as ms


class ClampTests(unittest.TestCase):
    def test_clamp_all_fields(self):
        p = ms.ModelParams(
            temperature=99, top_p=0.1, top_k=1000, repeat_penalty=0.5,
            max_tokens=9999, num_ctx=100, length_mode="wat",
            model="", prompt_variant="wat")
        c = ms.clamp_params(p)
        self.assertEqual(c.temperature, 1.5)
        self.assertEqual(c.top_p, 0.5)
        self.assertEqual(c.top_k, 100)
        self.assertEqual(c.repeat_penalty, 1.0)
        self.assertEqual(c.max_tokens, 400)
        self.assertEqual(c.num_ctx, 2048)
        self.assertEqual(c.length_mode, "long")
        self.assertEqual(c.prompt_variant, "optimized")
        self.assertTrue(c.model)

    def test_ctx_snaps_to_allowed(self):
        for value, expected in ((1000, 2048),                                   (3000, 2048), (6000, 4096),
                                (9000, 8192)):
            c = ms.clamp_params(ms.ModelParams(num_ctx=value))
            self.assertEqual(c.num_ctx, expected)

    def test_presets_valid(self):
        for preset in (ms.BASELINE, ms.OPTIMIZED):
            c = ms.clamp_params(copy.deepcopy(preset))
            self.assertEqual(dataclasses_asdict(c), dataclasses_asdict(preset))

    def test_baseline_values_from_stage0(self):
        self.assertEqual(ms.BASELINE.temperature, 1.0)
        self.assertEqual(ms.BASELINE.top_p, 0.95)
        self.assertEqual(ms.BASELINE.max_tokens, 60)
        self.assertEqual(ms.BASELINE.num_ctx, 4096)
        self.assertEqual(ms.BASELINE.length_mode, "short")
        self.assertEqual(ms.BASELINE.prompt_variant, "baseline")


def dataclasses_asdict(p):
    return {f.name: getattr(p, f.name)
            for f in __import__("dataclasses").fields(p)}


class StoreTests(unittest.TestCase):
    def setUp(self):
        import tempfile
        self.dir = tempfile.mkdtemp()
        os.environ["TAMAGOTCHI_HOME"] = self.dir
        self.path = os.path.join(self.dir, "model-settings.json")

    def tearDown(self):
        os.environ.pop("TAMAGOTCHI_HOME", None)

    def test_save_load_roundtrip(self):
        ms.save_params(ms.ModelParams(temperature=0.3, max_tokens=120),
                       self.path)
        p = ms.load_params(self.path)
        self.assertEqual(p.temperature, 0.3)
        self.assertEqual(p.max_tokens, 120)
        self.assertEqual(p.model, copy.deepcopy(ms.OPTIMIZED).model)

    def test_broken_file_gives_optimized(self):
        with open(self.path, "w", encoding="utf-8") as f:
            f.write("{битый json")
        p = ms.load_params(self.path)
        self.assertEqual(p.prompt_variant, ms.OPTIMIZED.prompt_variant)
        self.assertEqual(p.temperature, ms.OPTIMIZED.temperature)

    def test_partial_file_filled_from_optimized(self):
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump({"temperature": 0.5}, f)
        p = ms.load_params(self.path)
        self.assertEqual(p.temperature, 0.5)
        self.assertEqual(p.max_tokens, ms.OPTIMIZED.max_tokens)

    def test_unknown_keys_ignored(self):
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump({"temperature": 0.4, "hacker": True}, f)
        p = ms.load_params(self.path)
        self.assertEqual(p.temperature, 0.4)
        self.assertNotIn("hacker", as_dict(p))

    def test_atomic_save_no_tmp_leftovers(self):
        ms.save_params(ms.OPTIMIZED, self.path)
        leftovers = [n for n in os.listdir(self.dir)
                     if n.startswith(".model-settings-")]
        self.assertEqual(leftovers, [])


def as_dict(p):
    import dataclasses
    return dataclasses.asdict(p)


if __name__ == "__main__":
    unittest.main()
