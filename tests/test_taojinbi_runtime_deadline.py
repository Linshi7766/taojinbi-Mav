import unittest

from taojinbi_mav.runtime.deadline import Deadline, DeadlineExceeded


class FakeClock:
    def __init__(self):
        self.value = 100.0
        self.sleeps = []

    def __call__(self):
        return self.value

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.value += seconds


class DeadlineTests(unittest.TestCase):
    def test_remaining_uses_monotonic_clock(self):
        clock = FakeClock()
        deadline = Deadline.after(10, "run", clock=clock, sleeper=clock.sleep)
        clock.value += 3
        self.assertEqual(deadline.remaining(), 7)

    def test_child_cannot_outlive_parent(self):
        clock = FakeClock()
        parent = Deadline.after(10, "run", clock=clock, sleeper=clock.sleep)
        child = parent.child(20, "task")
        self.assertEqual(child.expires_at, parent.expires_at)
        self.assertEqual(child.scope, "task")

    def test_checkpoint_raises_with_scope_at_expiry(self):
        clock = FakeClock()
        deadline = Deadline.after(2, "dry_run", clock=clock, sleeper=clock.sleep)
        clock.value += 2
        with self.assertRaises(DeadlineExceeded) as raised:
            deadline.checkpoint()
        self.assertEqual(raised.exception.scope, "dry_run")

    def test_sleep_is_clamped_and_then_times_out(self):
        clock = FakeClock()
        deadline = Deadline.after(2, "task", clock=clock, sleeper=clock.sleep)
        with self.assertRaises(DeadlineExceeded):
            deadline.sleep(5)
        self.assertEqual(clock.sleeps, [2])

    def test_non_positive_budget_is_rejected(self):
        for seconds in (0, -1):
            with self.subTest(seconds=seconds):
                with self.assertRaises(ValueError):
                    Deadline.after(seconds, "run")


if __name__ == "__main__":
    unittest.main()
