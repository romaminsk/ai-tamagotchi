"""Тесты игровой логики питомца (этап 2)."""

import unittest

from tamagotchi.pet import Pet, TICK_SECONDS


class PetTests(unittest.TestCase):
    def test_start_values(self):
        pet = Pet()
        self.assertEqual(
            (pet.hunger, pet.energy, pet.fun, pet.hygiene),
            (80.0, 80.0, 80.0, 80.0),
        )
        self.assertEqual(pet.age_ticks, 0)
        self.assertFalse(pet.sleeping)

    def test_tick_awake_decay(self):
        pet = Pet()
        pet.tick()
        self.assertAlmostEqual(pet.hunger, 79.0)
        self.assertAlmostEqual(pet.energy, 79.4)
        self.assertAlmostEqual(pet.fun, 79.2)
        self.assertAlmostEqual(pet.hygiene, 79.5)
        self.assertEqual(pet.age_ticks, 1)

    def test_tick_sleep_energy_gain(self):
        pet = Pet()
        pet.hunger = 50
        pet.energy = 50
        pet.fun = 50
        pet.hygiene = 50
        pet.sleep()
        pet.tick()
        self.assertEqual(TICK_SECONDS, 5)
        self.assertTrue(pet.sleeping)
        self.assertAlmostEqual(pet.energy, 52.5)
        self.assertAlmostEqual(pet.hunger, 49.6)
        self.assertAlmostEqual(pet.fun, 49.8)
        self.assertAlmostEqual(pet.hygiene, 49.8)

    def test_wake_at_full_energy(self):
        pet = Pet()
        pet.energy = 99.5
        pet.sleeping = True  # напрямую: sleep() отказал бы (not_sleepy)
        pet.tick()
        self.assertAlmostEqual(pet.energy, 100.0)
        self.assertFalse(pet.sleeping)

    def test_feed(self):
        pet = Pet()
        pet.hunger = 70
        self.assertEqual(pet.feed(), "fed")
        self.assertAlmostEqual(pet.hunger, 100.0)
        self.assertAlmostEqual(pet.hygiene, 77.0)

    def test_feed_too_full(self):
        pet = Pet()
        pet.hunger = 95
        self.assertEqual(pet.feed(), "too_full")
        self.assertAlmostEqual(pet.hunger, 95.0)

    def test_play(self):
        pet = Pet()
        self.assertEqual(pet.play(), "played")
        self.assertAlmostEqual(pet.fun, 100.0)
        self.assertAlmostEqual(pet.energy, 70.0)
        self.assertAlmostEqual(pet.hunger, 75.0)

    def test_play_too_tired(self):
        pet = Pet()
        pet.energy = 19.9
        pet.fun = 10
        self.assertEqual(pet.play(), "too_tired")
        self.assertAlmostEqual(pet.fun, 10.0)

    def test_play_while_sleeping(self):
        pet = Pet()
        pet.sleep()
        self.assertEqual(pet.play(), "is_sleeping")

    def test_sleep_start_and_not_sleepy(self):
        pet = Pet()
        pet.energy = 50
        self.assertEqual(pet.sleep(), "sleep_started")
        self.assertTrue(pet.sleeping)
        self.assertEqual(pet.sleep(), "is_sleeping")
        pet2 = Pet()
        pet2.energy = 96
        self.assertEqual(pet2.sleep(), "not_sleepy")
        self.assertFalse(pet2.sleeping)

    def test_wake(self):
        pet = Pet()
        pet.sleep()
        self.assertEqual(pet.wake(), "woke_up")
        self.assertFalse(pet.sleeping)
        self.assertEqual(pet.wake(), "not_sleeping")

    def test_clean(self):
        pet = Pet()
        pet.hygiene = 10
        self.assertEqual(pet.clean(), "cleaned")
        self.assertAlmostEqual(pet.hygiene, 50.0)
        pet.hygiene = 90
        pet.clean()
        self.assertAlmostEqual(pet.hygiene, 100.0)

    def test_clamp(self):
        pet = Pet()
        # верхняя граница: энергия во сне растёт и зажимается в 100
        pet.energy = 99.5
        pet.sleeping = True
        pet.tick()
        self.assertAlmostEqual(pet.energy, 100.0)
        # нижняя граница
        pet.sleeping = False
        pet.hunger = 0
        pet.tick()
        self.assertAlmostEqual(pet.hunger, 0.0)

    def test_mood_priority(self):
        pet = Pet()
        pet.sleeping = True
        self.assertEqual(pet.mood(), "sleeping")
        pet.sleeping = False
        pet.hunger = 20
        self.assertEqual(pet.mood(), "hungry")
        pet.hunger = 80
        pet.hygiene = 20
        self.assertEqual(pet.mood(), "dirty")
        pet.hygiene = 80
        pet.energy = 20
        self.assertEqual(pet.mood(), "sleepy")
        pet.energy = 80
        pet.fun = 25
        self.assertEqual(pet.mood(), "sad")
        pet.fun = 70
        self.assertEqual(pet.mood(), "happy")
        pet.hygiene = 50
        self.assertEqual(pet.mood(), "neutral")

    def test_does_not_die(self):
        pet = Pet()
        for _ in range(10000):
            pet.tick()
        for key in ("hunger", "energy", "fun", "hygiene"):
            self.assertGreaterEqual(getattr(pet, key), 0.0)

    def test_roundtrip(self):
        pet = Pet()
        pet.hunger, pet.fun = 5, 95
        pet.age_ticks, pet.sleeping = 7, True
        data = pet.to_dict()
        clone = Pet.from_dict(data)
        self.assertEqual(data, clone.to_dict())


if __name__ == "__main__":
    unittest.main()
