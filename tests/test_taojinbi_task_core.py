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


class TaskClassificationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.core = importlib.import_module("taojinbi_mav.task_core")

    def get_function(self, name):
        function = getattr(self.core, name, None)
        self.assertTrue(callable(function), f"{name} must be callable")
        return function

    def test_none_package_does_not_match(self):
        package_contains = self.get_function("package_contains")
        self.assertFalse(package_contains(None, "com.sina.weibo"))

    def test_nonempty_package_can_match(self):
        package_contains = self.get_function("package_contains")
        self.assertTrue(
            package_contains("com.sina.weibo", "com.sina.weibo")
        )

    def test_runtime_package_gate_allows_only_explicit_packages(self):
        package_allowed = self.get_function("package_allowed")
        allowed_packages = {"com.taobao.taobao"}

        self.assertTrue(
            package_allowed("com.taobao.taobao", allowed_packages)
        )
        self.assertFalse(
            package_allowed(
                "com.ss.android.article.lite",
                allowed_packages,
            )
        )
        self.assertFalse(package_allowed(None, allowed_packages))

    def test_runtime_package_gate_keeps_legacy_callers_unrestricted(self):
        package_allowed = self.get_function("package_allowed")
        self.assertTrue(package_allowed("com.taobao.idlefish"))

    def test_closed_allowlist_rejects_unknown_brands_and_added_actions(self):
        classify_task = self.get_function("classify_task")

        for text, expected_reason in (
            ("京东浏览精选好物", "external_app"),
            ("抖音浏览精选好物", "external_app"),
            ("拼多多浏览精选好物", "not_pure_browse"),
            ("发现精选好物后收藏", "unsafe_action"),
            ("发现精选好物后关注", "unsafe_action"),
            ("发现精选好物后加购", "unsafe_action"),
            ("发现精选好物后加入购物车", "unsafe_action"),
            ("发现精选好物后领券", "unsafe_action"),
        ):
            with self.subTest(text=text):
                self.assertEqual(
                    classify_task(text).reason,
                    expected_reason,
                )

    def test_parses_complete_immersive_task_row(self):
        parser = self.get_function("parse_task_row_texts")

        row = parser([
            "好物沉浸看(0/5)",
            "浏览",
            "+35",
            "去完成",
        ], button_text="去完成")

        self.assertEqual(row.title, "好物沉浸看")
        self.assertEqual(row.description, "浏览")
        self.assertEqual(row.reward, "+35")
        self.assertEqual(row.progress_text, "0/5")
        self.assertEqual(row.full_text, "好物沉浸看 浏览 +35")

    def test_rejects_incomplete_generic_browse_row(self):
        parser = self.get_function("parse_task_row_texts")
        classifier = self.get_function("classify_task_row")

        row = parser(["浏览", "+35", "去完成"], button_text="去完成")

        self.assertIsNone(row.title)
        self.assertEqual(classifier(row).reason, "missing_text")

    def test_row_classifier_allows_only_exact_featured_goods_pair(self):
        parser = self.get_function("parse_task_row_texts")
        classifier = self.get_function("classify_task_row")

        accepted = classifier(parser(["发现精选好物(0/5)", "浏览", "+35"]))
        self.assertTrue(accepted.allowed)
        self.assertEqual(accepted.handler, "default")
        for title in ("好物沉浸看", "拍立淘逛感兴趣的宝贝"):
            with self.subTest(title=title):
                decision = classifier(parser([title, "浏览", "+35"]))
                self.assertFalse(decision.allowed)
                self.assertEqual(decision.reason, "not_pure_browse")

    def test_dangerous_text_overrides_featured_goods_title(self):
        parser = self.get_function("parse_task_row_texts")
        classifier = self.get_function("classify_task_row")

        for description in ("看视频得红包", "浏览后下单", "邀请好友浏览"):
            with self.subTest(description=description):
                decision = classifier(
                    parser(["发现精选好物(0/5)", description, "+35"])
                )
                self.assertFalse(decision.allowed)
                self.assertEqual(decision.reason, "unsafe_action")

    def test_exact_title_description_contract_rejects_additional_actions(self):
        parser = self.get_function("parse_task_row_texts")
        classifier = self.get_function("classify_task_row")

        for title, description in (
            ("发现精选好物", "分享商品"),
            ("发现精选好物", "签到"),
            ("发现精选好物", "浏览并分享"),
        ):
            with self.subTest(title=title, description=description):
                decision = classifier(
                    parser([title, description, "+35"])
                )
                self.assertFalse(decision.allowed)
                self.assertEqual(decision.reason, "not_pure_browse")

    def test_legacy_classifier_allows_only_featured_goods(self):
        classifier = self.get_function("classify_task")
        self.assertTrue(classifier("发现精选好物").allowed)
        for title in (
            "拍立淘逛感兴趣的宝贝",
            "好物沉浸看",
        ):
            with self.subTest(title=title):
                decision = classifier(title)
                self.assertFalse(decision.allowed)
                self.assertEqual(decision.reason, "not_pure_browse")

    def test_legacy_blacklist_text_no_longer_removes_pailitao(self):
        prepare_text = self.get_function("legacy_blacklist_text")
        self.assertEqual(
            prepare_text("拍立淘逛感兴趣的宝贝"),
            "拍立淘逛感兴趣的宝贝",
        )

    def test_rejects_transaction_even_when_browse_marker_exists(self):
        classify_task = self.get_function("classify_task")
        decision = classify_task("浏览精选好物后下单")
        self.assertEqual(decision.reason, "unsafe_action")

    def test_rejects_external_app_even_when_browse_marker_exists(self):
        classify_task = self.get_function("classify_task")
        self.assertEqual(
            classify_task("去支付宝浏览好物").reason,
            "external_app",
        )
        self.assertEqual(
            classify_task("蚂蚁森林浏览任务").reason,
            "external_app",
        )

    def test_rejects_interaction_task(self):
        classify_task = self.get_function("classify_task")
        self.assertEqual(
            classify_task("邀请好友浏览得红包").reason,
            "unsafe_action",
        )

    def test_rejects_every_required_unsafe_action_marker(self):
        classify_task = self.get_function("classify_task")
        unsafe_markers = (
            "下单",
            "购买",
            "邀请",
            "充值",
            "助力",
            "游戏",
            "红包",
            "外卖",
        )
        for marker in unsafe_markers:
            with self.subTest(marker=marker):
                decision = classify_task(f"浏览精选好物并{marker}")
                self.assertEqual(decision.reason, "unsafe_action")

    def test_rejects_every_required_external_app_marker(self):
        classify_task = self.get_function("classify_task")
        for marker in ("头条", "支付宝", "蚂蚁森林"):
            with self.subTest(marker=marker):
                decision = classify_task(f"去{marker}浏览精选好物")
                self.assertEqual(decision.reason, "external_app")

    def test_unsafe_marker_overrides_exact_pailitao_exception(self):
        classify_task = self.get_function("classify_task")
        decision = classify_task(
            "拍立淘逛感兴趣的宝贝后下单",
            legacy_blocked=True,
        )
        self.assertEqual(decision.reason, "unsafe_action")

    def test_legacy_blacklist_blocks_nonexception_browse_task(self):
        classify_task = self.get_function("classify_task")
        decision = classify_task(
            "浏览今日好物",
            legacy_blocked=True,
        )
        self.assertEqual(decision.reason, "safety_blacklist")

    def test_rejects_missing_or_unreadable_text(self):
        classify_task = self.get_function("classify_task")
        self.assertEqual(classify_task(None).reason, "missing_text")
        self.assertEqual(classify_task("   ").reason, "missing_text")

    def test_rejects_task_without_internal_browse_marker(self):
        classify_task = self.get_function("classify_task")
        self.assertEqual(
            classify_task("普通签到任务").reason,
            "not_pure_browse",
        )

    def test_rejects_task_at_retry_limit(self):
        classify_task = self.get_function("classify_task")
        self.assertEqual(
            classify_task("发现精选好物", attempts=2).reason,
            "retry_limit",
        )

    def test_builds_identifier_from_text_and_progress(self):
        task_identifier = self.get_function("task_identifier")
        self.assertIsNone(task_identifier(None))
        self.assertEqual(
            task_identifier("发现精选好物", "0/1"),
            "发现精选好物|0/1",
        )

    def test_selection_gate_returns_only_first_allowed_task(self):
        snapshot_class = getattr(self.core, "TaskSnapshot", None)
        choose_task = self.get_function("choose_first_allowed")
        self.assertTrue(callable(snapshot_class), "TaskSnapshot must exist")
        snapshots = [
            snapshot_class(None),
            snapshot_class("去支付宝浏览好物"),
            snapshot_class("浏览精选好物后下单"),
            snapshot_class("发现精选好物"),
            snapshot_class("拍立淘逛感兴趣的宝贝"),
        ]

        index, decisions = choose_task(snapshots)

        self.assertEqual(index, 3)
        self.assertEqual(
            [decision.reason for decision in decisions],
            [
                "missing_text",
                "external_app",
                "unsafe_action",
                "allowed",
                "not_pure_browse",
            ],
        )

    def test_selection_gate_returns_none_when_all_tasks_are_unsafe(self):
        snapshot_class = getattr(self.core, "TaskSnapshot", None)
        choose_task = self.get_function("choose_first_allowed")
        self.assertTrue(callable(snapshot_class), "TaskSnapshot must exist")
        snapshots = [
            snapshot_class("邀请好友浏览得红包"),
            snapshot_class("发现精选好物", attempts=2),
            snapshot_class("普通签到任务"),
        ]

        index, decisions = choose_task(snapshots)

        self.assertIsNone(index)
        self.assertFalse(any(decision.allowed for decision in decisions))


class TaskScanStateTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.core = importlib.import_module("taojinbi_mav.task_core")

    def make_state(self, **kwargs):
        state_class = getattr(self.core, "TaskScanState", None)
        self.assertTrue(callable(state_class), "TaskScanState must exist")
        return state_class(**kwargs)

    def test_success_resets_consecutive_error_count(self):
        state = self.make_state(error_count=1)
        state.record_success()
        self.assertEqual(state.error_count, 0)

    def test_loading_does_not_increment_error_count(self):
        state = self.make_state(error_count=1)
        state.record_loading()
        self.assertEqual(state.error_count, 1)

    def test_exhausted_scan_increments_error_count(self):
        state = self.make_state()
        self.assertEqual(state.record_exhausted(), 1)

    def test_new_screen_resets_unchanged_swipe_count(self):
        state = self.make_state()
        self.assertFalse(state.observe_screen({"任务A"}, after_swipe=False))
        self.assertFalse(state.observe_screen({"任务B"}, after_swipe=True))
        self.assertEqual(state.unchanged_swipes, 0)

    def test_two_unchanged_swipes_reach_bottom(self):
        state = self.make_state()
        self.assertFalse(state.observe_screen({"任务A"}, after_swipe=False))
        self.assertFalse(state.observe_screen({"任务A"}, after_swipe=True))
        self.assertTrue(state.observe_screen({"任务A"}, after_swipe=True))


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


class TaskAttemptLimitTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.core = importlib.import_module("taojinbi_mav.task_core")

    def test_two_revalidation_failures_exhaust_a_task(self):
        consume = getattr(
            self.core,
            "consume_task_attempt",
            None,
        )
        self.assertTrue(callable(consume), "consume_task_attempt must exist")
        attempts = {}

        self.assertTrue(consume(attempts, "发现精选好物"))
        self.assertTrue(consume(attempts, "发现精选好物"))
        self.assertFalse(consume(attempts, "发现精选好物"))
        self.assertEqual(attempts["发现精选好物"], 2)


if __name__ == "__main__":
    unittest.main()
