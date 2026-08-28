import unittest

from taojinbi_mav.runtime.outcome import (
    ExitCode,
    RunCounts,
    RunMode,
    RunOutcome,
    RunStatus,
)


class RuntimeOutcomeTests(unittest.TestCase):
    def test_status_maps_to_stable_exit_code(self):
        expected = {
            RunStatus.SUCCESS: ExitCode.SUCCESS,
            RunStatus.PARTIAL: ExitCode.PARTIAL,
            RunStatus.STARTUP_FAILED: ExitCode.STARTUP_FAILED,
            RunStatus.SAFETY_STOPPED: ExitCode.SAFETY_STOPPED,
            RunStatus.TIMED_OUT: ExitCode.TIMED_OUT,
            RunStatus.CANCELLED: ExitCode.CANCELLED,
        }
        for status, exit_code in expected.items():
            with self.subTest(status=status):
                outcome = RunOutcome(RunMode.EXECUTE, status, "test_reason")
                self.assertEqual(outcome.exit_code, exit_code)

    def test_counts_are_immutable_and_default_to_zero(self):
        counts = RunCounts()
        self.assertEqual(
            (
                counts.detected,
                counts.supported,
                counts.skipped,
                counts.attempted,
                counts.completed,
                counts.likely_completed,
                counts.unfinished,
            ),
            (0, 0, 0, 0, 0, 0, 0),
        )
        with self.assertRaises(AttributeError):
            counts.detected = 1

    def test_negative_counts_fail_closed(self):
        with self.assertRaises(ValueError):
            RunCounts(skipped=-1)

    def test_reason_rejects_text_that_could_leak_details(self):
        for reason in ("", "device offline", "C:\\private\\path", "商品名"):
            with self.subTest(reason=reason):
                with self.assertRaises(ValueError):
                    RunOutcome(RunMode.EXECUTE, RunStatus.STARTUP_FAILED, reason)


if __name__ == "__main__":
    unittest.main()
