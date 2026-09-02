import importlib
import unittest

from taojinbi_mav.runtime.deadline import DeadlineExceeded


class CoreImportTests(unittest.TestCase):
    def test_core_module_exists(self):
        try:
            module = importlib.import_module("taojinbi_mav.task_core")
        except ModuleNotFoundError:
            module = None

        self.assertIsNotNone(module)


class ImmersiveProgressTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.core = importlib.import_module("taojinbi_mav.task_core")

    def test_records_only_observed_progress_growth(self):
        runner = getattr(
            self.core,
            "run_verified_immersive_progress",
            None,
        )
        self.assertTrue(callable(runner))
        progress = iter(["0/5", "1/5", "2/5", "3/5", "4/5", "5/5"])

        result = runner(
            read_progress=lambda: next(progress),
            perform_one=lambda: True,
            still_allowed=lambda: True,
        )

        self.assertTrue(result.completed)
        self.assertEqual(result.progress, 5)
        self.assertEqual(result.successful_steps, 5)
        self.assertEqual(result.reason, "completed")

    def test_progress_jump_counts_one_observed_action_not_five(self):
        runner = self.core.run_verified_immersive_progress
        progress = iter(["0/5", "5/5"])

        result = runner(
            read_progress=lambda: next(progress),
            perform_one=lambda: True,
            still_allowed=lambda: True,
        )

        self.assertTrue(result.completed)
        self.assertEqual(result.progress, 5)
        self.assertEqual(result.successful_steps, 1)
        self.assertEqual(result.transitions, ((0, 5),))

    def test_stops_after_two_actions_without_progress(self):
        runner = getattr(
            self.core,
            "run_verified_immersive_progress",
            None,
        )
        self.assertTrue(callable(runner))

        result = runner(
            read_progress=lambda: "0/5",
            perform_one=lambda: True,
            still_allowed=lambda: True,
        )

        self.assertFalse(result.completed)
        self.assertEqual(result.successful_steps, 0)
        self.assertEqual(result.reason, "stalled")

    def test_stops_when_package_is_not_allowed(self):
        runner = self.core.run_verified_immersive_progress

        result = runner(
            read_progress=lambda: "0/5",
            perform_one=lambda: self.fail("must not perform an action"),
            still_allowed=lambda: False,
        )

        self.assertEqual(result.reason, "unsafe_package")

    def test_rejects_wrong_progress_total(self):
        runner = self.core.run_verified_immersive_progress

        result = runner(
            read_progress=lambda: "0/3",
            perform_one=lambda: self.fail("must not perform an action"),
            still_allowed=lambda: True,
        )

        self.assertEqual(result.reason, "missing_progress")

    def test_fixed_total_mismatch_after_progress_is_not_reported_as_ok(self):
        progress = iter(["1/3", "2/3", "2/5", "2/5", "2/5"])

        result = self.core.run_verified_immersive_progress(
            read_progress=lambda: next(progress),
            perform_one=lambda: True,
            still_allowed=lambda: True,
            target=3,
            progress_read_delay=0,
            missing_progress_reason=lambda: "ok",
        )

        self.assertFalse(result.completed)
        self.assertEqual(result.progress, 2)
        self.assertEqual(result.successful_steps, 1)
        self.assertEqual(result.reason, "progress_total_mismatch")

    def test_progress_reset_after_browse_reported_as_likely_complete(self):
        runner = self.core.run_verified_immersive_progress
        # 搜一搜类：读到 4/5 → 浏览一轮 → 计数重置为 0/5（完成后进入下一周期）；
        # 回落必须判为“疑似完成(progress_reset)”，不能误当停滞 stalled。
        progress = iter(["4/5", "0/5", "0/5"])
        result = runner(
            read_progress=lambda: next(progress),
            perform_one=lambda: True,
            still_allowed=lambda: True,
        )
        self.assertFalse(result.completed)
        self.assertEqual(result.reason, "progress_reset")
        self.assertEqual(result.transitions, ((4, 0),))

    def test_retries_missing_progress_after_browse(self):
        runner = self.core.run_verified_immersive_progress
        # 返回列表后的第一帧可能尚未加载任务行；有限回读应等到下一帧，
        # 不能把一次 OCR 空结果误判为任务失败。
        progress = iter(["0/1", None, "1/1"])

        result = runner(
            read_progress=lambda: next(progress),
            perform_one=lambda: True,
            still_allowed=lambda: True,
            target=1,
            progress_read_retries=1,
            progress_read_delay=0,
        )

        self.assertTrue(result.completed)
        self.assertEqual(result.transitions, ((0, 1),))

    def test_reports_custom_reason_after_bounded_progress_read_failure(self):
        runner = self.core.run_verified_immersive_progress
        progress = iter(["0/1", None])

        result = runner(
            read_progress=lambda: next(progress),
            perform_one=lambda: True,
            still_allowed=lambda: True,
            target=1,
            progress_read_retries=0,
            progress_read_delay=0,
            missing_progress_reason=lambda: "task_row_unobserved",
        )

        self.assertFalse(result.completed)
        self.assertEqual(result.reason, "task_row_unobserved")
        self.assertEqual(result.progress, 0)

    def test_stops_safely_when_progress_read_raises(self):
        runner = self.core.run_verified_immersive_progress

        def read_progress():
            raise RuntimeError("device disconnected")

        result = runner(
            read_progress=read_progress,
            perform_one=lambda: self.fail("must not browse without progress"),
            still_allowed=lambda: True,
        )

        self.assertFalse(result.completed)
        self.assertEqual(result.reason, "device_io_error")

    def test_device_error_takes_priority_over_missing_progress_callback(self):
        # Keep the sequence explicit so a device exception cannot be
        # relabeled as a normal missing-row state by the device callback.
        reads = iter(["0/1", RuntimeError("device disconnected")])

        def read_once():
            value = next(reads)
            if isinstance(value, Exception):
                raise value
            return value

        result = self.core.run_verified_immersive_progress(
            read_progress=read_once,
            perform_one=lambda: True,
            still_allowed=lambda: True,
            target=1,
            progress_read_retries=0,
            progress_read_delay=0,
            missing_progress_reason=lambda: "task_row_unobserved",
        )

        self.assertEqual(result.reason, "device_io_error")

    def test_stops_safely_when_browse_action_raises(self):
        progress = iter(["0/1"])

        def perform_one():
            raise RuntimeError("device disconnected")

        result = self.core.run_verified_immersive_progress(
            read_progress=lambda: next(progress),
            perform_one=perform_one,
            still_allowed=lambda: True,
            target=1,
        )

        self.assertFalse(result.completed)
        self.assertEqual(result.reason, "device_io_error")

    def test_retries_after_transient_progress_read_error(self):
        progress = iter(["0/1", RuntimeError("temporary disconnect"), "1/1"])

        def read_progress():
            value = next(progress)
            if isinstance(value, Exception):
                raise value
            return value

        result = self.core.run_verified_immersive_progress(
            read_progress=read_progress,
            perform_one=lambda: True,
            still_allowed=lambda: True,
            target=1,
            progress_read_retries=1,
            progress_read_delay=0,
        )

        self.assertTrue(result.completed)
        self.assertEqual(result.transitions, ((0, 1),))

    def test_dynamic_total_resets_baseline_and_continues(self):
        runner = self.core.run_verified_immersive_progress
        # 看看# 任务可能从 0/5 轮换为 4/7；分母变化不应把 4 格
        # 计入上一轮，而应以 4/7 作为新基线继续观察到 7/7。
        progress = iter(["0/5", "4/7", "5/7", "7/7"])

        result = runner(
            read_progress=lambda: next(progress),
            perform_one=lambda: True,
            still_allowed=lambda: True,
            target=5,
            allow_dynamic_total=True,
            max_total_changes=1,
            progress_read_retries=0,
            progress_read_delay=0,
        )

        self.assertTrue(result.completed)
        self.assertEqual(result.progress, 7)
        self.assertEqual(result.successful_steps, 2)
        self.assertEqual(result.transitions, ((4, 5), (5, 7)))
        self.assertEqual(result.total_changes, ((5, 7),))

    def test_dynamic_total_stops_after_bounded_rotations(self):
        runner = self.core.run_verified_immersive_progress
        progress = iter(["0/5", "4/7", "1/6"])

        result = runner(
            read_progress=lambda: next(progress),
            perform_one=lambda: True,
            still_allowed=lambda: True,
            allow_dynamic_total=True,
            max_total_changes=1,
            progress_read_retries=0,
            progress_read_delay=0,
        )

        self.assertFalse(result.completed)
        self.assertEqual(result.reason, "task_rotated")
        self.assertEqual(result.progress, 1)
        self.assertEqual(result.successful_steps, 0)
        self.assertEqual(result.transitions, ())
        self.assertEqual(result.total_changes, ((5, 7), (7, 6)))

    def test_deadline_exception_is_not_reclassified_as_device_error(self):
        def stop():
            raise DeadlineExceeded("task")

        with self.assertRaises(DeadlineExceeded):
            self.core.run_verified_immersive_progress(
                read_progress=lambda: "0/5",
                perform_one=lambda: True,
                still_allowed=lambda: True,
                target=5,
                checkpoint=stop,
                sleeper=lambda _seconds: None,
            )

    def test_progress_delay_uses_injected_sleeper(self):
        sleeps = []
        progress = iter(["0/1", None, "1/1"])

        result = self.core.run_verified_immersive_progress(
            read_progress=lambda: next(progress),
            perform_one=lambda: True,
            still_allowed=lambda: True,
            target=1,
            progress_read_retries=1,
            progress_read_delay=2,
            sleeper=sleeps.append,
        )

        self.assertTrue(result.completed)
        self.assertIn(2, sleeps)


class PackagePollingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.core = importlib.import_module("taojinbi_mav.task_core")

    def get_waiter(self):
        waiter = getattr(self.core, "wait_for_package_name", None)
        self.assertTrue(callable(waiter), "wait_for_package_name must exist")
        return waiter

    def test_none_package_times_out_without_type_error(self):
        waiter = self.get_waiter()
        now = [0.0]

        def clock():
            return now[0]

        def sleeper(seconds):
            now[0] += seconds

        result = waiter(
            lambda: (None, None),
            timeout=1,
            poll_interval=0.25,
            clock=clock,
            sleeper=sleeper,
        )

        self.assertIsNone(result)

    def test_polling_returns_first_nonempty_package(self):
        waiter = self.get_waiter()
        results = iter([
            (None, None),
            ("com.taobao.taobao", "SomeActivity"),
        ])
        now = [0.0]

        def clock():
            return now[0]

        def sleeper(seconds):
            now[0] += seconds

        result = waiter(
            lambda: next(results),
            timeout=1,
            poll_interval=0.25,
            clock=clock,
            sleeper=sleeper,
        )

        self.assertEqual(result, "com.taobao.taobao")

    def test_getter_returning_bare_none_is_tolerated(self):
        waiter = self.get_waiter()
        now = [0.0]

        def clock():
            return now[0]

        def sleeper(seconds):
            now[0] += seconds

        result = waiter(
            lambda: None,
            timeout=0.5,
            poll_interval=0.25,
            clock=clock,
            sleeper=sleeper,
        )

        self.assertIsNone(result)
