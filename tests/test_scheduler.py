"""Тесты планировщика реплик: троттлинг, приоритеты, инъекция времени."""

import unittest

from tamagotchi.llm import (PRIO_ACTION, PRIO_IDLE, PRIO_MOOD,
                            PhraseScheduler)


class FakeClock:
    def __init__(self, start=0.0):
        self.t = start

    def __call__(self):
        return self.t

    def advance(self, dt):
        self.t += dt


class SchedulerTests(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock()
        self.sched = PhraseScheduler(now=self.clock, idle_min=60.0,
                                     idle_max=60.0, min_pause=8.0)

    def test_action_always_accepted(self):
        accepted, _ = self.sched.decide(PRIO_ACTION)
        self.assertTrue(accepted)
        self.sched.note_sent()
        self.clock.advance(1.0)
        accepted, _ = self.sched.decide(PRIO_ACTION)
        self.assertTrue(accepted)

    def test_mood_dropped_during_pause(self):
        self.sched.note_sent()
        self.clock.advance(3.0)
        accepted, reason = self.sched.decide(PRIO_MOOD)
        self.assertFalse(accepted)
        self.assertEqual(reason, "paused")

    def test_mood_accepted_after_pause(self):
        self.sched.note_sent()
        self.clock.advance(self.sched.min_pause + 0.1)
        self.assertTrue(self.sched.decide(PRIO_MOOD)[0])

    def test_mood_accepted_if_idle_due_even_paused(self):
        self.sched.note_sent()
        self.clock.advance(61.0)
        self.assertTrue(self.sched.decide(PRIO_MOOD)[0])

    def test_idle_not_due_dropped(self):
        accepted, reason = self.sched.decide(PRIO_IDLE)
        self.assertFalse(accepted)
        self.assertEqual(reason, "not_due")

    def test_idle_due_but_paused_dropped(self):
        self.sched.note_sent()            # idle-срок перенесён на +60 с
        self.clock.advance(30.0)          # ещё идёт пауза и срок не наступил
        accepted, reason = self.sched.decide(PRIO_IDLE)
        self.assertFalse(accepted)
        self.assertEqual(reason, "not_due")
        # отдельный случай: срок наступил, но пауза ещё идёт
        clock2 = FakeClock()
        s2 = PhraseScheduler(now=clock2, idle_min=1.0, idle_max=1.0,
                             min_pause=8.0)
        clock2.advance(1.0)
        s2.note_sent()
        clock2.advance(1.0)
        accepted, reason = s2.decide(PRIO_IDLE)
        self.assertFalse(accepted)
        self.assertEqual(reason, "paused")

    def test_idle_due_accepted(self):
        self.clock.advance(60.0)
        accepted, _ = self.sched.decide(PRIO_IDLE)
        self.assertTrue(accepted)

    def test_note_sent_reschedules_idle(self):
        self.clock.advance(60.0)
        self.assertTrue(self.sched.idle_due())
        self.sched.note_sent()
        self.assertFalse(self.sched.idle_due())
        self.clock.advance(60.0)
        self.assertTrue(self.sched.idle_due())

    def test_pause_left(self):
        self.sched.note_sent()
        self.clock.advance(5.0)
        self.assertAlmostEqual(self.sched.pause_left(), 3.0)
        self.clock.advance(10.0)
        self.assertEqual(self.sched.pause_left(), 0.0)


if __name__ == "__main__":
    unittest.main()
