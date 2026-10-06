"""Тесты storage: save/load, битый JSON, потолок офлайн-тиков."""

import json
import os
import tempfile
import unittest

from tamagotchi import storage
from tamagotchi.pet import Pet


class StorageTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="tam-test-")
        self.path = os.path.join(self.dir, "save.json")
        self._orig_home = storage.home_dir()
        os.environ["TAMAGOTCHI_HOME"] = self.dir

    def tearDown(self):
        os.environ.pop("TAMAGOTCHI_HOME", None)

    def test_default_path_uses_dotdir(self):
        os.environ.pop("TAMAGOTCHI_HOME", None)
        self.assertTrue(storage.home_dir().endswith(".ai-tamagotchi"))

    def test_save_then_load_roundtrip(self):
        pet = Pet("Барсик")
        pet.fun = 42.0
        storage.save(pet, self.path, now=1000.0)
        loaded, saved_at = storage.load(self.path)
        self.assertEqual(loaded.name, "Барсик")
        self.assertAlmostEqual(loaded.fun, 42.0)
        self.assertEqual(saved_at, 1000.0)

    def test_corrupt_json_starts_fresh(self):
        with open(self.path, "w") as f:
            f.write("{not valid json («")
        pet, saved_at = storage.load(self.path)
        self.assertIsInstance(pet, Pet)
        self.assertEqual(pet.hunger, 80.0)
        self.assertIsNone(saved_at)
        # не-словарь JSON тоже не роняет
        with open(self.path, "w") as f:
            f.write("[1,2,3]")
        pet, _ = storage.load(self.path)
        self.assertIsInstance(pet, Pet)

    def test_offline_ticks_cap_30(self):
        pet = Pet()
        pet.sleeping = True
        saved_at = 0.0
        now = 5 * 1000  # 1000 тиков офлайн
        ticks = storage.apply_offline_time(pet, saved_at, now=now)
        self.assertEqual(ticks, storage.MAX_OFFLINE_TICKS)
        self.assertEqual(pet.age_ticks, 30)

    def test_offline_none_saved_at(self):
        pet = Pet()
        self.assertEqual(storage.apply_offline_time(pet, None, now=1e9), 0)

    def test_atomic_write_leaves_no_tmp(self):
        pet = Pet()
        storage.save(pet, self.path)
        leftovers = [n for n in os.listdir(self.dir) if n != "save.json"]
        self.assertEqual(leftovers, [])


if __name__ == "__main__":
    unittest.main()
