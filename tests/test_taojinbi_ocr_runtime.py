import contextlib
import importlib.util
import io
import json
import os
import runpy
import sys
import tempfile
import types
import unittest
from contextlib import ExitStack, redirect_stdout
from pathlib import Path
from unittest.mock import ANY, patch

# Load the CLI entry from scripts/ (not a package) as `runtime`.
_spec = importlib.util.spec_from_file_location(
    "run_taojinbi",
    Path(__file__).resolve().parent.parent / "scripts" / "run_taojinbi.py",
)
runtime = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(runtime)
import taojinbi_mav.runtime.logging as runtime_logging
from taojinbi_mav.ocr_ui import (
    BrowseTarget, ImmersiveTarget, OcrSpan, ScanOutcome, ScanStatus,
)
from taojinbi_mav.task_core import ImmersiveRunResult
from taojinbi_mav.runtime.config import (
    build_ocr_arg_parser,
    resolve_device_serial,
    resolve_ocr_gpu,
)
from taojinbi_mav.runtime.deadline import Deadline, DeadlineExceeded
from taojinbi_mav.runtime.logging import create_runtime_logger
from taojinbi_mav.runtime.outcome import ExitCode, RunMode, RunOutcome, RunStatus
from taojinbi_mav.task_strategies import StrategyResult


def _box(cx, cy, w=140, h=40):
    """按中心构造 easyocr 风格的四角点边界框。"""
    left, right = cx - w / 2, cx + w / 2
    top, bottom = cy - h / 2, cy + h / 2
    return [[left, top], [right, top], [right, bottom], [left, bottom]]


def _raw_row(cy, title, desc="浏览", action="去完成"):
    return [
        (_box(391, cy), title, 0.98),
        (_box(327, cy + 40), desc, 0.9),
        (_box(943, cy + 20), action, 0.97),
    ]


ANCHOR_RAW = (_box(540, 150), "赚金币抵钱", 0.99)
SUPPORTED_SEARCH_RAW = _raw_row(300, "搜一搜你心仪的宝贝(0/5)")
UNKNOWN_ROW_RAW = _raw_row(600, "秘密商品(0/1)")


class ReadOnlyDevice:
    """dry-run 只读桩：动作方法一旦被调用立即失败并记录。"""

    def __init__(self, package=runtime.TB_APP):
        self.package = package
        self.actions = []

    def window_size(self):
        return (1080, 2400)

    def app_current(self):
        return {"package": self.package}

    def screenshot(self, format="opencv"):
        return object()

    def click(self, *args):
        self.actions.append(("click", args))
        raise AssertionError("dry-run must not click")

    def swipe(self, *args):
        self.actions.append(("swipe", args))
        raise AssertionError("dry-run must not swipe")

    def press(self, *args):
        self.actions.append(("press", args))
        raise AssertionError("dry-run must not press")


class _FakeReader:
    def __init__(self, raw_results):
        self.raw_results = raw_results
        self.calls = 0

    def readtext(self, _path):
        self.calls += 1
        return self.raw_results


class _FakeRuntimeLogger:
    def __init__(self):
        self.events = []
        self.closed = False

    def emit(self, event, **kwargs):
        self.events.append((event, kwargs))

    def close(self):
        self.closed = True
        self.events.append(("closed", {}))

    def events_named(self, event):
        return [item for item in self.events if item[0] == event]


def _fake_logger_factory(*_args, **_kwargs):
    return _FakeRuntimeLogger()


class _ScreenshotFailureDevice:
    def screenshot(self, _path):
        raise RuntimeError("screencap unavailable")


class _ReaderFailure:
    def readtext(self, _path):
        raise RuntimeError("ocr backend unavailable")


class _InterruptingReader:
    def readtext(self, _path):
        raise KeyboardInterrupt()


class _WorkingDevice:
    def window_size(self):
        return (1080, 1920)

    def screenshot(self, _path):
        return None

    def app_current(self):
        return {"package": runtime.TB_APP}

    def swipe(self, *_args):
        return None


class _GestureDevice(_WorkingDevice):
    def __init__(self):
        self.swipes = []

    def swipe(self, *args):
        self.swipes.append(args)

    def click(self, *_args):
        raise AssertionError("search result browsing must not click a product")


class FakeClock:
    """execute 时限测试的可注入时钟（从 0 单调递增）。"""

    def __init__(self, start=0.0):
        self._now = start

    def __call__(self):
        return self._now

    def advance(self, seconds):
        self._now += seconds


class FakeSleeper:
    """可注入 sleeper：推进 FakeClock，记录每次睡眠时长。"""

    def __init__(self, clock):
        self.clock = clock
        self.calls = []

    def __call__(self, seconds):
        self.calls.append(seconds)
        self.clock.advance(seconds)


class RecordingDeadlineDevice:
    """execute 时限测试桩：记录带时间戳的动作，绝不连接真机。"""

    def __init__(self, clock, package=runtime.TB_APP):
        self.clock = clock
        self.package = package
        self.actions = []

    def window_size(self):
        return (1080, 1920)

    def app_current(self):
        return {"package": self.package}

    def screenshot(self, _path):
        return object()

    def click(self, *args):
        self.actions.append((self.clock(), "click", args))

    def swipe(self, *args):
        self.actions.append((self.clock(), "swipe", args))

    def press(self, *args):
        self.actions.append((self.clock(), "press", args))


class OcrRuntimeSafetyTests(unittest.TestCase):
    def test_current_package_fails_closed_for_missing_app_info(self):
        class MissingAppDevice:
            def app_current(self):
                return None

        self.assertIsNone(runtime.current_package(MissingAppDevice()))

    def test_current_package_fails_closed_when_device_query_raises(self):
        class BrokenAppDevice:
            def app_current(self):
                raise RuntimeError("app query unavailable")

        self.assertIsNone(runtime.current_package(BrokenAppDevice()))

    def test_ocr_screen_returns_none_when_screenshot_fails(self):
        result = runtime.ocr_screen(
            _ScreenshotFailureDevice(),
            _ReaderFailure(),
        )
        self.assertIsNone(result)

    def test_ocr_screen_returns_none_when_reader_fails(self):
        result = runtime.ocr_screen(_WorkingDevice(), _ReaderFailure())
        self.assertIsNone(result)

    def test_empty_or_missing_ocr_evidence_is_not_safe(self):
        device = _WorkingDevice()
        self.assertFalse(runtime.in_taobao_and_safe(device, None))
        self.assertFalse(runtime.in_taobao_and_safe(device, []))

    def test_valid_ocr_evidence_remains_safe_inside_taobao(self):
        spans = [
            OcrSpan(
                text="赚金币抵钱",
                confidence=0.99,
                center=(300, 300),
                bounds=(200, 280, 400, 320),
            )
        ]
        self.assertTrue(runtime.in_taobao_and_safe(_WorkingDevice(), spans))


class RuntimeConfigTests(unittest.TestCase):
    def test_serial_resolution_prefers_explicit_value_then_environment(self):
        with patch.dict("os.environ", {"TAOJINBI_DEVICE_SERIAL": "env-device"}):
            self.assertEqual(resolve_device_serial("explicit-device"), "explicit-device")
            self.assertEqual(resolve_device_serial(), "env-device")

    def test_gpu_resolution_defaults_to_cpu_and_accepts_environment_flag(self):
        with patch.dict("os.environ", {}, clear=True):
            self.assertFalse(resolve_ocr_gpu())
        with patch.dict("os.environ", {"TAOJINBI_OCR_GPU": "true"}):
            self.assertTrue(resolve_ocr_gpu())

    def test_missing_serial_fails_closed_without_connecting(self):
        """无 --serial 且无环境变量时安全失败，不连接任何设备。"""
        connected = []

        def fake_connect(serial):
            connected.append(serial)
            return _ActionDevice()

        with patch.dict("os.environ", {}, clear=True):
            outcome = runtime.run_ocr_entry(
                dry_run=True, dry_run_timeout=30,
                connect=fake_connect,
                reader_factory=lambda *_a, **_k: _FakeReader([[ANCHOR_RAW]]),
                logger_factory=_fake_logger_factory,
            )
        self.assertEqual(connected, [])
        self.assertEqual(outcome.status, RunStatus.STARTUP_FAILED)
        self.assertEqual(outcome.reason, "device_serial_missing")

    def test_ocr_parser_accepts_serial_and_cpu_gpu_switches(self):
        parser = build_ocr_arg_parser()
        args = parser.parse_args(
            ["--serial", "usb-device", "--gpu", "--max-tasks", "3"]
        )
        self.assertEqual(args.serial, "usb-device")
        self.assertTrue(args.gpu)
        self.assertEqual(args.max_tasks, 3)
        self.assertFalse(parser.parse_args(["--cpu"]).gpu)
        self.assertEqual(parser.parse_args([]).max_tasks, 1)
        with self.assertRaises(SystemExit):
            parser.parse_args(["--max-tasks", "-1"])

    def test_ocr_entry_injects_device_and_reader_boundaries(self):
        calls = []

        def connect(serial):
            calls.append(("connect", serial))
            return object()

        def reader_factory(languages, gpu):
            calls.append(("reader", languages, gpu))
            return object()

        with patch.object(runtime, "on_task_list", return_value=True), patch.object(
            runtime,
            "run_safe_browse_tasks",
            return_value=([], [], []),
        ):
            result = runtime.run_ocr_entry(
                serial="usb-device",
                use_gpu=False,
                max_tasks=1,
                connect=connect,
                reader_factory=reader_factory,
                logger_factory=_fake_logger_factory,
            )

        self.assertEqual(result.exit_code, ExitCode.SUCCESS)
        self.assertEqual(calls[0], ("connect", "usb-device"))
        self.assertEqual(calls[1], ("reader", ["ch_sim", "en"], False))

    def test_ocr_entry_passes_explicit_task_limit(self):
        calls = []

        def connect(_serial):
            return object()

        def reader_factory(_languages, gpu):
            return object()

        def run_tasks(_device, _reader, max_tasks, **kwargs):
            calls.append(max_tasks)
            return ([], [], [])

        with patch.object(runtime, "on_task_list", return_value=True), patch.object(
            runtime,
            "run_safe_browse_tasks",
            side_effect=run_tasks,
        ):
            result = runtime.run_ocr_entry(
                serial="usb-device",
                use_gpu=False,
                max_tasks=3,
                connect=connect,
                reader_factory=reader_factory,
                logger_factory=_fake_logger_factory,
            )

        self.assertEqual(result.exit_code, ExitCode.SUCCESS)
        self.assertEqual(calls, [3])

    def test_ocr_entry_stops_safely_when_device_connection_fails(self):
        def connect(_serial):
            raise RuntimeError("device offline")

        def reader_factory(_languages, _gpu):
            self.fail("reader must not load without a device")

        result = runtime.run_ocr_entry(
            serial="offline-device",
            use_gpu=False,
            connect=connect,
            reader_factory=reader_factory,
            logger_factory=_fake_logger_factory,
        )

        self.assertEqual(result.exit_code, ExitCode.STARTUP_FAILED)

    def test_ocr_entry_reports_fatal_task_loop_failure(self):
        def connect(_serial):
            return object()

        def reader_factory(_languages, gpu):
            return object()

        with patch.object(runtime, "on_task_list", return_value=True), patch.object(
            runtime,
            "run_safe_browse_tasks",
            return_value=([], [], ["好物沉浸看(device_io_error)"]),
        ):
            result = runtime.run_ocr_entry(
                serial="offline-device",
                use_gpu=False,
                connect=connect,
                reader_factory=reader_factory,
                logger_factory=_fake_logger_factory,
            )

        self.assertEqual(result.exit_code, ExitCode.SAFETY_STOPPED)


class DryRunEntryTests(unittest.TestCase):
    """dry-run 入口合同：单屏零动作、稳定退出码、脱敏日志与控制台。"""

    def setUp(self):
        self.device = ReadOnlyDevice()
        self.reader = _FakeReader([ANCHOR_RAW] + SUPPORTED_SEARCH_RAW)
        self.logger_factory = _fake_logger_factory

    def run_dry(self, raw_results, device=None):
        device = device or ReadOnlyDevice()
        reader = _FakeReader(raw_results)
        return runtime.run_ocr_entry(
            serial="test-device",
            dry_run=True,
            connect=lambda _serial: device,
            reader_factory=lambda *_args, **_kwargs: reader,
            logger_factory=self.logger_factory,
        )

    def test_dry_run_reads_one_screen_and_performs_no_action(self):
        outcome = runtime.run_ocr_entry(
            serial="test-device",
            dry_run=True,
            connect=lambda _serial: self.device,
            reader_factory=lambda *_args, **_kwargs: self.reader,
            logger_factory=self.logger_factory,
        )
        self.assertEqual(outcome.status, RunStatus.SUCCESS)
        self.assertEqual(outcome.mode, RunMode.DRY_RUN)
        self.assertEqual(self.device.actions, [])
        self.assertEqual(self.reader.calls, 1)

    def test_dry_run_no_candidates_is_success(self):
        outcome = self.run_dry([])
        self.assertEqual(
            (outcome.exit_code, outcome.reason),
            (ExitCode.SUCCESS, "no_candidates"),
        )

    def test_logger_failure_stops_before_connect(self):
        connected = []
        outcome = runtime.run_ocr_entry(
            serial="test-device",
            dry_run=True,
            logger_factory=lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError()),
            connect=lambda serial: connected.append(serial),
        )
        self.assertEqual(
            (outcome.exit_code, outcome.reason),
            (ExitCode.STARTUP_FAILED, "log_initialization_failed"),
        )
        self.assertEqual(connected, [])

    def test_dry_run_not_in_taobao_exits_startup_failed(self):
        outcome = self.run_dry(
            [ANCHOR_RAW],
            device=ReadOnlyDevice(package="com.ss.android.article.lite"),
        )
        self.assertEqual(
            (outcome.exit_code, outcome.reason),
            (ExitCode.STARTUP_FAILED, "not_in_taobao"),
        )

    def test_dry_run_missing_anchor_exits_startup_failed(self):
        outcome = self.run_dry([(_box(540, 300), "淘金币", 0.99)])
        self.assertEqual(
            (outcome.exit_code, outcome.reason),
            (ExitCode.STARTUP_FAILED, "list_anchor_missing"),
        )

    def test_dry_run_connect_failure_exits_startup_failed(self):
        def connect(_serial):
            raise RuntimeError("device offline")

        outcome = runtime.run_ocr_entry(
            serial="test-device",
            dry_run=True,
            connect=connect,
            logger_factory=self.logger_factory,
        )
        self.assertEqual(
            (outcome.exit_code, outcome.reason),
            (ExitCode.STARTUP_FAILED, "device_connect_failed"),
        )

    def test_dry_run_ocr_failure_exits_startup_failed(self):
        class _BrokenReader:
            def readtext(self, _path):
                raise RuntimeError("ocr backend unavailable")

        outcome = runtime.run_ocr_entry(
            serial="test-device",
            dry_run=True,
            connect=lambda _serial: ReadOnlyDevice(),
            reader_factory=lambda *_args, **_kwargs: _BrokenReader(),
            logger_factory=self.logger_factory,
        )
        self.assertEqual(
            (outcome.exit_code, outcome.reason),
            (ExitCode.STARTUP_FAILED, "ocr_unavailable"),
        )

    def test_dry_run_jsonl_and_console_only_fixed_labels(self):
        device = ReadOnlyDevice()
        reader = _FakeReader([ANCHOR_RAW] + UNKNOWN_ROW_RAW)
        console = []
        with tempfile.TemporaryDirectory() as folder:
            logger = create_runtime_logger(
                folder,
                RunMode.DRY_RUN,
                run_id_factory=lambda: "abcd1234",
                console=console.append,
            )
            outcome = runtime.run_ocr_entry(
                serial="test-device",
                dry_run=True,
                connect=lambda _serial: device,
                reader_factory=lambda *_args, **_kwargs: reader,
                logger_factory=lambda *_args, **_kwargs: logger,
            )
            raw = Path(logger.path).read_text(encoding="utf-8")
        self.assertEqual(outcome.exit_code, ExitCode.SUCCESS)
        lines = [json.loads(line) for line in raw.splitlines() if line.strip()]
        row_events = [
            line for line in lines if line["event"] == "dry_run_row_decided"
        ]
        self.assertEqual(len(row_events), 1)
        self.assertIsNone(row_events[0]["task_key"])
        self.assertEqual(row_events[0]["reason"], "unsupported_task")
        self.assertNotIn("秘密商品", raw)
        self.assertEqual(console, ["dry-run：未注册任务 unsupported_task"])


class EntryValidationRetryTests(unittest.TestCase):
    def test_retries_entry_validation_until_latest_candidate_is_stable(self):
        attempts = []

        def validate():
            attempts.append(len(attempts) + 1)
            return None if len(attempts) < 3 else "stable-candidate"

        result = runtime.retry_entry_validation(
            validate,
            max_retries=2,
            retry_delay=0,
        )

        self.assertEqual(result, "stable-candidate")
        self.assertEqual(attempts, [1, 2, 3])

    def test_false_candidate_does_not_short_circuit_retries(self):
        # on_task_list 返回布尔：False 表示"不在列表"，必须继续重试，
        # 不能把 False 当成有效结果提前返回（否则 OCR 抖动被误报为锚点缺失）。
        attempts = []

        def validate():
            attempts.append(1)
            return False

        result = runtime.retry_entry_validation(
            validate, max_retries=2, retry_delay=0,
        )
        self.assertIsNone(result)
        self.assertEqual(attempts, [1, 1, 1])

    def test_stops_after_two_retries_when_entry_never_stabilizes(self):
        attempts = []

        def validate():
            attempts.append(1)
            return None

        result = runtime.retry_entry_validation(
            validate,
            max_retries=2,
            retry_delay=0,
        )

        self.assertIsNone(result)
        self.assertEqual(len(attempts), 3)

    def test_immersive_entry_retries_jitter_and_clicks_latest_target(self):
        target = ImmersiveTarget(
            title="好物沉浸看",
            progress_text="0/5",
            title_center=(300, 500),
            action_text="去完成",
            action_center=(800, 500),
            confidence=0.99,
        )
        with patch.object(
            runtime,
            "locate_immersive_target",
            side_effect=[ScanOutcome.found(target), ScanOutcome.found(target)],
        ) as locate, patch.object(
            runtime,
            "ocr_screen",
            side_effect=[object(), object()],
        ), patch.object(
            runtime,
            "in_taobao_and_safe",
            return_value=True,
        ), patch.object(
            runtime,
            "find_immersive_target",
            side_effect=[None, target],
        ), patch.object(
            runtime,
            "safe_tap",
            return_value=True,
        ) as tap, patch.object(runtime.time, "sleep"):
            result = runtime.enter_immersive_from_list(_WorkingDevice(), None)

        self.assertTrue(result)
        self.assertEqual(locate.call_count, 2)
        tap.assert_called_once_with(
            ANY,
            target.action_center,
            (1080, 1920),
        )


class SafeBrowseLoopTests(unittest.TestCase):
    def test_refreshed_task_rotation_is_likely_done_and_stops_loop(self):
        target = BrowseTarget(
            title="搜一搜你心仪的宝贝",
            progress=1,
            total=5,
            title_center=(300, 500),
            action_text="去完成",
            action_center=(800, 500),
            confidence=0.99,
        )
        result = type(
            "Result",
            (),
            {
                "completed": False,
                "successful_steps": 0,
                "reason": "task_rotated_after_refresh",
            },
        )()

        with patch.object(
            runtime,
            "locate_safe_browse_target",
            side_effect=[ScanOutcome.found(target), ScanOutcome.found(target), ScanOutcome.not_found()],
        ) as locate, patch.object(
            runtime,
            "run_one_safe_browse_task",
            return_value=(result, True),
        ) as run_one:
            done, likely_done, unfinished = runtime.run_safe_browse_tasks(
                _WorkingDevice(), None, max_tasks=3,
            )

        self.assertEqual(locate.call_count, 1)
        self.assertEqual(run_one.call_count, 1)
        self.assertEqual(done, [])
        self.assertEqual(likely_done, ["搜一搜…"])
        self.assertEqual(unfinished, [])

    def test_unconfirmed_task_stops_loop_before_next_task(self):
        target = BrowseTarget(
            title="拍立淘逛感兴趣的宝贝",
            progress=0,
            total=5,
            title_center=(300, 500),
            action_text="去完成",
            action_center=(800, 500),
            confidence=0.99,
        )
        result = type(
            "Result",
            (),
            {
                "completed": False,
                "successful_steps": 0,
                "reason": "task_row_unobserved",
            },
        )()

        with patch.object(
            runtime,
            "locate_safe_browse_target",
            side_effect=[ScanOutcome.found(target), ScanOutcome.found(target), ScanOutcome.not_found()],
        ) as locate, patch.object(
            runtime,
            "run_one_safe_browse_task",
            return_value=(result, True),
        ) as run_one:
            done, likely_done, unfinished = runtime.run_safe_browse_tasks(
                _WorkingDevice(), None, max_tasks=3,
            )

        self.assertEqual(locate.call_count, 1)
        self.assertEqual(run_one.call_count, 1)
        self.assertEqual(done, [])
        self.assertEqual(likely_done, [])
        self.assertEqual(unfinished, ["未知任务(task_row_unobserved)"])

    def test_device_error_stops_loop_before_next_task(self):
        target = BrowseTarget(
            title="拍立淘逛感兴趣的宝贝",
            progress=0,
            total=5,
            title_center=(300, 500),
            action_text="去完成",
            action_center=(800, 500),
            confidence=0.99,
        )
        result = type(
            "Result",
            (),
            {
                "completed": False,
                "successful_steps": 0,
                "reason": "device_io_error",
            },
        )()

        with patch.object(
            runtime,
            "locate_safe_browse_target",
            side_effect=[ScanOutcome.found(target), ScanOutcome.found(target), ScanOutcome.found(target), ScanOutcome.not_found()],
        ) as locate, patch.object(
            runtime,
            "run_one_safe_browse_task",
            return_value=(result, False),
        ) as run_one:
            done, likely_done, unfinished = runtime.run_safe_browse_tasks(
                _WorkingDevice(), None, max_tasks=3,
            )

        self.assertEqual(locate.call_count, 1)
        self.assertEqual(run_one.call_count, 1)
        self.assertEqual(done, [])
        self.assertEqual(likely_done, [])
        self.assertEqual(unfinished, ["未知任务(device_io_error)"])

    def test_browse_entry_retries_jitter_and_clicks_latest_target(self):
        target = BrowseTarget(
            title="发现精选好物",
            progress=0,
            total=1,
            title_center=(300, 500),
            action_text="去完成",
            action_center=(800, 500),
            confidence=0.99,
        )
        with patch.object(
            runtime,
            "locate_safe_browse_target",
            side_effect=[ScanOutcome.found(target), ScanOutcome.found(target)],
        ) as locate, patch.object(
            runtime,
            "ocr_screen",
            side_effect=[object(), object()],
        ), patch.object(
            runtime,
            "in_taobao_and_safe",
            return_value=True,
        ), patch.object(
            runtime,
            "find_safe_browse_target",
            side_effect=[None, target],
        ), patch.object(
            runtime,
            "safe_tap",
            return_value=True,
        ) as tap, patch.object(runtime.time, "sleep"):
            result = runtime.enter_task_from_list(
                _WorkingDevice(),
                None,
                "发现精选好物",
            )

        self.assertTrue(result)
        self.assertEqual(locate.call_count, 2)
        tap.assert_called_once_with(
            ANY,
            target.action_center,
            (1080, 1920),
        )


class ProgressReadFallbackTests(unittest.TestCase):
    def test_classifies_missing_progress_without_claiming_completion(self):
        title = "看看#鱼油"
        matching_title = OcrSpan(
            text="看看#斯维诗鱼油(0/6)",
            confidence=0.99,
            center=(300, 500),
            bounds=(200, 480, 400, 520),
        )
        unrelated = OcrSpan(
            text="其他任务(0/1)",
            confidence=0.99,
            center=(300, 500),
            bounds=(200, 480, 400, 520),
        )

        self.assertEqual(
            runtime.classify_progress_read(None, title, None),
            "ocr_unavailable",
        )
        self.assertEqual(
            runtime.classify_progress_read([matching_title], title, None),
            "progress_unreadable",
        )
        self.assertEqual(
            runtime.classify_progress_read([unrelated], title, None),
            "task_row_unobserved",
        )
        self.assertEqual(
            runtime.classify_progress_read([matching_title], title, (0, 6)),
            "ok",
        )

    def test_current_screen_first_and_full_scan_only_once(self):
        calls = []

        def current_read():
            calls.append("current")
            return None

        def full_scan():
            calls.append("full")
            return None

        first, full_scan_used = runtime.read_progress_current_then_full(
            current_read,
            full_scan,
            full_scan_used=False,
        )
        second, full_scan_used = runtime.read_progress_current_then_full(
            current_read,
            full_scan,
            full_scan_used=full_scan_used,
        )

        self.assertIsNone(first)
        self.assertIsNone(second)
        self.assertEqual(calls, ["current", "full", "current"])
        self.assertTrue(full_scan_used)


class StrategyRuntimeIntegrationTests(unittest.TestCase):
    def test_safe_browse_task_delegates_one_round_to_selected_strategy(self):
        from taojinbi_mav.tasks.registry import profile_for_title
        from taojinbi_mav.task_strategies import StrategyResult

        result = ImmersiveRunResult(False, 0, 0, "stalled", ())

        def fake_core(**kwargs):
            self.assertTrue(kwargs["perform_one"]())
            return result

        device = _GestureDevice()
        with patch.object(runtime, "enter_task_from_list", return_value=True), \
             patch.object(
                 runtime, "select_task_strategy", return_value="search", create=True,
             ) as select, \
             patch.object(
                 runtime, "execute_task_strategy",
                 return_value=StrategyResult(True), create=True,
             ) as execute, \
             patch.object(
                 runtime, "trigger_search_from_discovery",
                 return_value=True, create=True,
             ), \
             patch.object(
                 runtime, "browse_search_results_by_scroll",
                 return_value=True, create=True,
             ), \
             patch.object(runtime, "back_to_task_list_ocr", return_value=True), \
             patch.object(runtime, "_safe_back_to_coin_page", return_value=True), \
             patch.object(runtime, "_reopen_task_popup", return_value=True), \
             patch.object(runtime, "run_verified_immersive_progress", side_effect=fake_core), \
             patch.object(runtime, "BROWSE_PER_ROUND", 1), \
             patch.object(runtime.time, "sleep"):
            actual, browsed = runtime.run_one_safe_browse_task(
                device, None, "搜一搜你心仪的宝贝", 6
            )

        self.assertIs(actual, result)
        self.assertTrue(browsed)
        select.assert_called_once_with(
            profile_for_title("搜一搜你心仪的宝贝")
        )

    def test_browse_round_exits_to_coin_page_and_reopens_popup(self):
        """每轮浏览后必须退出到赚更多金币界面结算并重开弹窗（真机计数要求）。"""
        from taojinbi_mav.task_strategies import StrategyResult

        result = ImmersiveRunResult(False, 0, 0, "stalled", ())

        def fake_core(**kwargs):
            self.assertTrue(kwargs["perform_one"]())
            return result

        device = _GestureDevice()
        with patch.object(runtime, "enter_task_from_list", return_value=True), \
             patch.object(
                 runtime, "select_task_strategy", return_value="search", create=True,
             ), \
             patch.object(
                 runtime, "execute_task_strategy",
                 return_value=StrategyResult(True), create=True,
             ), \
             patch.object(runtime, "_safe_back_to_coin_page", return_value=True) as settle, \
             patch.object(runtime, "_reopen_task_popup", return_value=True) as reopen, \
             patch.object(runtime, "run_verified_immersive_progress", side_effect=fake_core), \
             patch.object(runtime.time, "sleep"):
            runtime.run_one_safe_browse_task(
                device, None, "搜一搜你心仪的宝贝", 6
            )
        settle.assert_called()
        reopen.assert_called()

    def test_runtime_reads_dynamic_total_policy_from_profile(self):
        captured = {}

        def fake_core(**kwargs):
            captured.update(kwargs)
            return ImmersiveRunResult(False, 0, 0, "stalled", ())

        with patch.object(
            runtime, "run_verified_immersive_progress", side_effect=fake_core
        ), patch.object(runtime, "locate_task_progress", return_value=ScanOutcome.found((0, 6))):
            runtime.run_one_safe_browse_task(
                _WorkingDevice(), None, "看看#斯维诗鱼油", 6
            )

        self.assertTrue(captured["allow_dynamic_total"])

    def test_runtime_disables_dynamic_total_for_search_profile(self):
        captured = {}

        def fake_core(**kwargs):
            captured.update(kwargs)
            return ImmersiveRunResult(False, 0, 0, "stalled", ())

        with patch.object(
            runtime, "run_verified_immersive_progress", side_effect=fake_core
        ), patch.object(runtime, "locate_task_progress", return_value=ScanOutcome.found((0, 6))):
            runtime.run_one_safe_browse_task(
                _WorkingDevice(), None, "搜一搜你心仪的宝贝", 6
            )

        self.assertFalse(captured["allow_dynamic_total"])

    def test_runtime_disables_dynamic_total_for_featured_goods_profile(self):
        captured = {}

        def fake_core(**kwargs):
            captured.update(kwargs)
            return ImmersiveRunResult(False, 0, 0, "stalled", ())

        with patch.object(
            runtime, "run_verified_immersive_progress", side_effect=fake_core
        ), patch.object(runtime, "locate_task_progress", return_value=ScanOutcome.found((0, 4))):
            runtime.run_one_safe_browse_task(
                _WorkingDevice(), None, "发现精选好物", 4
            )

        self.assertFalse(captured["allow_dynamic_total"])

    def test_dynamic_total_policy_comes_from_resolved_profile_not_raw_title(self):
        from taojinbi_mav.tasks.registry import profile_for_title

        captured = {}

        def fake_core(**kwargs):
            captured.update(kwargs)
            return ImmersiveRunResult(False, 0, 0, "stalled", ())

        featured = profile_for_title("发现精选好物")
        with patch.object(
            runtime, "profile_for_title", return_value=featured, create=True
        ), patch.object(
            runtime, "run_verified_immersive_progress", side_effect=fake_core
        ), patch.object(runtime, "locate_task_progress", return_value=ScanOutcome.found((0, 6))):
            runtime.run_one_safe_browse_task(
                _WorkingDevice(), None, "看看#斯维诗鱼油", 6
            )

        self.assertFalse(captured["allow_dynamic_total"])

    def test_unsupported_title_fails_before_device_entry(self):
        class DeviceMustNotBeEntered:
            def window_size(self):
                raise AssertionError(
                    "unsupported title must fail before device entry"
                )

        result, browsed = runtime.run_one_safe_browse_task(
            DeviceMustNotBeEntered(), None, "任意未注册标题", 5
        )

        self.assertFalse(result.completed)
        self.assertEqual(result.reason, "unsupported_task")
        self.assertFalse(browsed)

    def test_strategy_failure_returns_to_list_and_stops_round(self):
        from taojinbi_mav.task_strategies import StrategyResult

        observed = []

        def fake_core(**kwargs):
            observed.append(kwargs["perform_one"]())
            return ImmersiveRunResult(False, 0, 0, "stalled", ())

        with patch.object(runtime, "enter_task_from_list", return_value=True), \
             patch.object(
                 runtime, "select_task_strategy", return_value="feed", create=True,
             ), \
             patch.object(
                 runtime, "execute_task_strategy",
                 return_value=StrategyResult(False, "unsafe_package"),
                 create=True,
             ), \
             patch.object(runtime, "back_to_task_list_ocr", return_value=True) as back, \
             patch.object(runtime, "run_verified_immersive_progress", side_effect=fake_core), \
             patch.object(runtime.time, "sleep"):
            runtime.run_one_safe_browse_task(
                _GestureDevice(), None, "发现精选好物", 5
            )

        self.assertEqual(observed, [False])
        back.assert_called_once()

    def test_entered_but_round_incomplete_without_progress_not_browsed(self):
        # 2B 证据链：只进入信息流(enter 成功)但完整往返失败(_safe_back 失败→
        # no_safe_control)、且从未读到进度推进 → browsed 必须为 False，
        # 不得因 entered>0 就谎报已浏览（真机 run8 误报 likely 但余额没涨）
        from taojinbi_mav.task_strategies import StrategyResult

        def fake_core(**kwargs):
            performed = kwargs["perform_one"]()
            self.assertFalse(performed)
            return ImmersiveRunResult(False, 0, 0, "no_safe_control", ())

        with patch.object(runtime, "enter_task_from_list", return_value=True), \
             patch.object(runtime, "select_task_strategy",
                          return_value="feed", create=True), \
             patch.object(runtime, "execute_task_strategy",
                          return_value=StrategyResult(True), create=True), \
             patch.object(runtime, "_safe_back_to_coin_page", return_value=False), \
             patch.object(runtime, "run_verified_immersive_progress",
                          side_effect=fake_core), \
             patch.object(runtime.time, "sleep"):
            result, browsed = runtime.run_one_safe_browse_task(
                _GestureDevice(), None, "发现精选好物", 5
            )

        self.assertEqual(result.reason, "no_safe_control")
        self.assertFalse(browsed)

    def test_completed_round_without_final_progress_is_browsed(self):
        # 对照：完整往返(enter→策略→_safe_back→重开弹窗全成功)即 browse_completed，
        # 即使返回后读不到进度也计 browsed（短任务完成后行消失的真机到账场景）
        from taojinbi_mav.task_strategies import StrategyResult

        def fake_core(**kwargs):
            self.assertTrue(kwargs["perform_one"]())
            return ImmersiveRunResult(False, 0, 0, "missing_progress", ())

        with patch.object(runtime, "enter_task_from_list", return_value=True), \
             patch.object(runtime, "select_task_strategy",
                          return_value="feed", create=True), \
             patch.object(runtime, "execute_task_strategy",
                          return_value=StrategyResult(True), create=True), \
             patch.object(runtime, "_safe_back_to_coin_page", return_value=True), \
             patch.object(runtime, "_reopen_task_popup", return_value=True), \
             patch.object(runtime, "run_verified_immersive_progress",
                          side_effect=fake_core), \
             patch.object(runtime.time, "sleep"):
            result, browsed = runtime.run_one_safe_browse_task(
                _GestureDevice(), None, "发现精选好物", 5
            )

        self.assertEqual(result.reason, "missing_progress")
        self.assertTrue(browsed)

    def test_legacy_immersive_wrapper_delegates_to_feed_strategy(self):
        from taojinbi_mav.task_strategies import FEED_BROWSE, StrategyResult

        result = ImmersiveRunResult(False, 0, 0, "stalled", ())

        def fake_core(**kwargs):
            self.assertTrue(kwargs["perform_one"]())
            return result

        device = _GestureDevice()
        with patch.object(runtime, "enter_immersive_from_list", return_value=True), \
             patch.object(
                 runtime, "execute_task_strategy",
                 return_value=StrategyResult(True), create=True,
             ) as execute, \
             patch.object(runtime, "run_verified_immersive_progress", side_effect=fake_core), \
             patch.object(runtime.time, "sleep"):
            actual = runtime.run_immersive_goods_task_ocr(
                device, None, back_to_list=lambda: True
            )

        self.assertIs(actual, result)
        execute.assert_called_once()
        self.assertEqual(execute.call_args.args[0], FEED_BROWSE)


class RefreshIntegrationTests(unittest.TestCase):
    def _run_one_patches(self):
        return (
            patch.object(runtime, "enter_task_from_list", return_value=True),
            patch.object(runtime, "back_to_task_list_ocr", return_value=True),
            patch.object(runtime, "BROWSE_PER_ROUND", 0),
            patch.object(runtime.time, "sleep"),
            # perform_one 完整往返需要这两步收尾成功才计 browse_completed
            patch.object(runtime, "_safe_back_to_coin_page", return_value=True),
            patch.object(runtime, "_reopen_task_popup", return_value=True),
        )

    def test_task_row_unobserved_uses_bounded_refresh_result(self):
        first = ImmersiveRunResult(False, 1, 0, "task_row_unobserved", ())
        recovery = runtime.RefreshRecoveryResult(
            "not_found", attempts=2, reason="refresh_not_found"
        )

        def fake_core(**kwargs):
            kwargs["perform_one"]()
            return first

        patches = self._run_one_patches()
        sleeps = []
        with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5], \
             patch.object(runtime, "_deadline_sleep",
                          side_effect=lambda _d, s: sleeps.append(s)), \
             patch.object(runtime, "run_verified_immersive_progress", side_effect=fake_core), \
             patch.object(runtime, "refresh_task_after_disappearance", return_value=recovery) as refresh:
            result, browsed = runtime.run_one_safe_browse_task(
                _WorkingDevice(), None, "发现精选好物", 3
            )
        self.assertTrue(browsed)
        self.assertEqual(result.reason, "refresh_not_found")
        # 预算 attempts=2 已用尽：不等宽限（不会复核），直接收场
        self.assertEqual(refresh.call_count, 1)
        self.assertNotIn(runtime.REFRESH_LAG_GRACE_S, sleeps)
        refresh_kwargs = refresh.call_args.kwargs
        self.assertEqual(refresh_kwargs.get("expected_progress"), 1)
        self.assertEqual(refresh_kwargs.get("expected_total"), 3)

    def test_incomplete_task_after_refresh_is_automatically_resumed(self):
        first = ImmersiveRunResult(False, 1, 0, "task_row_unobserved", ())
        target = BrowseTarget(
            "发现精选好物", 1, 3, (300, 500), "去完成", (800, 500), 0.99
        )
        resumed = ImmersiveRunResult(
            True, 3, 2, "completed", ((1, 2), (2, 3))
        )
        core_results = iter([first, resumed])

        def fake_core(**kwargs):
            kwargs["perform_one"]()
            return next(core_results)

        patches = self._run_one_patches()
        with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5], \
             patch.object(runtime, "run_verified_immersive_progress", side_effect=fake_core), \
             patch.object(
                 runtime,
                 "refresh_task_after_disappearance",
                 return_value=runtime.RefreshRecoveryResult(
                     "continue", target=target, attempts=1
                 ),
             ):
            result, browsed = runtime.run_one_safe_browse_task(
                _WorkingDevice(), None, "发现精选好物", 3
            )
        self.assertTrue(browsed)
        self.assertTrue(result.completed)

    def test_completed_task_after_refresh_is_not_entered_again(self):
        first = ImmersiveRunResult(False, 1, 0, "task_row_unobserved", ())
        target = BrowseTarget(
            "发现精选好物", 3, 3, (300, 500), "去完成", (800, 500), 0.99
        )

        def fake_core(**kwargs):
            kwargs["perform_one"]()
            return first

        patches = self._run_one_patches()
        with patches[0] as enter, patches[1], patches[2], patches[3], patches[4], patches[5], \
             patch.object(runtime, "run_verified_immersive_progress", side_effect=fake_core), \
             patch.object(
                 runtime,
                 "refresh_task_after_disappearance",
                 return_value=runtime.RefreshRecoveryResult(
                     "completed", target=target, attempts=1
                 ),
             ):
            result, _ = runtime.run_one_safe_browse_task(
                _WorkingDevice(), None, "发现精选好物", 3
            )
        self.assertTrue(result.completed)
        self.assertEqual(enter.call_count, 1)

    def test_refresh_not_found_grace_enables_recovery(self):
        """滞后宽限后末次复核重新定位任务行：继续浏览而非立即误停。"""
        first = ImmersiveRunResult(False, 1, 0, "task_row_unobserved", ())
        target = BrowseTarget(
            "发现精选好物", 2, 3, (300, 500), "去完成", (800, 500), 0.99
        )
        recoveries = iter([
            runtime.RefreshRecoveryResult("not_found", attempts=1,
                                          reason="refresh_not_found"),
            runtime.RefreshRecoveryResult("continue", target=target,
                                          attempts=1),
        ])
        sleeps = []

        def fake_core(**kwargs):
            kwargs["perform_one"]()
            return first

        patches = self._run_one_patches()
        with patches[0] as enter, patches[1], patches[2], patches[3], patches[4], patches[5], \
             patch.object(runtime, "_deadline_sleep",
                          side_effect=lambda _d, s: sleeps.append(s)), \
             patch.object(runtime, "run_verified_immersive_progress",
                          side_effect=fake_core), \
             patch.object(runtime, "refresh_task_after_disappearance",
                          side_effect=lambda *a, **k: next(recoveries)):
            result, browsed = runtime.run_one_safe_browse_task(
                _WorkingDevice(), None, "发现精选好物", 3
            )
        self.assertIn(runtime.REFRESH_LAG_GRACE_S, sleeps)  # 宽限恰好一次
        self.assertEqual(enter.call_count, 2)               # 恢复后再次进入
        # 预算 2 次用尽（宽限 1 + 复核 1），核心执行结果如实返回
        self.assertEqual(result.reason, "task_row_unobserved")

    def test_refresh_not_found_grace_expires_then_concludes(self):
        """宽限后仍未找到：按原语义结论 refresh_not_found。"""
        first = ImmersiveRunResult(False, 1, 0, "task_row_unobserved", ())
        recovery = runtime.RefreshRecoveryResult(
            "not_found", attempts=2, reason="refresh_not_found"
        )
        sleeps = []

        def fake_core(**kwargs):
            kwargs["perform_one"]()
            return first

        patches = self._run_one_patches()
        with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5], \
             patch.object(runtime, "_deadline_sleep",
                          side_effect=lambda _d, s: sleeps.append(s)), \
             patch.object(runtime, "run_verified_immersive_progress",
                          side_effect=fake_core), \
             patch.object(runtime, "refresh_task_after_disappearance",
                          return_value=recovery) as refresh:
            result, browsed = runtime.run_one_safe_browse_task(
                _WorkingDevice(), None, "发现精选好物", 3
            )
        # 预算 attempts=2 一次用尽：不等宽限（不会复核），直接收场
        self.assertEqual(refresh.call_count, 1)
        self.assertEqual(sleeps.count(runtime.REFRESH_LAG_GRACE_S), 0)
        self.assertEqual(result.reason, "refresh_not_found")

    def test_refresh_budget_not_reset_by_grace_window(self):
        """宽限不得重置刷新预算：两次刷新上限是全局硬边界，预算用尽即收场。"""
        first = ImmersiveRunResult(False, 1, 0, "task_row_unobserved", ())
        recovery = runtime.RefreshRecoveryResult(
            "not_found", attempts=2, reason="refresh_not_found"
        )
        sleeps = []

        def fake_core(**kwargs):
            kwargs["perform_one"]()
            return first

        patches = self._run_one_patches()
        with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5], \
             patch.object(runtime, "_deadline_sleep",
                          side_effect=lambda _d, s: sleeps.append(s)), \
             patch.object(runtime, "run_verified_immersive_progress",
                          side_effect=fake_core), \
             patch.object(runtime, "refresh_task_after_disappearance",
                          return_value=recovery) as refresh:
            result, browsed = runtime.run_one_safe_browse_task(
                _WorkingDevice(), None, "发现精选好物", 3
            )
        # 预算 attempts=2 一次用尽 → 不等宽限直接收场（总刷新不超过 2）
        self.assertEqual(refresh.call_count, 1)
        self.assertEqual(sleeps.count(runtime.REFRESH_LAG_GRACE_S), 0)
        self.assertEqual(result.reason, "refresh_not_found")

    def test_rotated_task_after_refresh_is_not_entered_again(self):
        first = ImmersiveRunResult(False, 1, 0, "task_row_unobserved", ())
        target = BrowseTarget(
            "发现精选好物", 0, 6,
            (300, 500), "去完成", (800, 500), 0.99,
        )

        def fake_core(**kwargs):
            kwargs["perform_one"]()
            return first

        patches = self._run_one_patches()
        with patches[0] as enter, patches[1], patches[2], patches[3], patches[4], patches[5], \
             patch.object(runtime, "run_verified_immersive_progress", side_effect=fake_core), \
             patch.object(
                 runtime,
                 "refresh_task_after_disappearance",
                 return_value=runtime.RefreshRecoveryResult(
                     "rotated", target=target, attempts=1,
                     reason="task_total_changed",
                 ),
             ):
            result, browsed = runtime.run_one_safe_browse_task(
                _WorkingDevice(), None, "发现精选好物", 5
            )

        self.assertTrue(browsed)
        self.assertEqual(result.reason, "task_rotated_after_refresh")
        self.assertEqual(enter.call_count, 1)


class CoinRootIdentityTests(unittest.TestCase):
    """淘金币根页面身份合同：唯一顶部锚点 + 唯一入口，缺一即非根页。"""

    def _root_spans(self, anchor_y=100, anchor_count=1, action=True):
        spans = [
            OcrSpan(
                "淘金币", 0.99, (300, anchor_y),
                (250, anchor_y - 20, 350, anchor_y + 20),
            )
            for _ in range(anchor_count)
        ]
        if action:
            spans.append(
                OcrSpan("赚更多金币", 0.99, (500, 800), (450, 780, 550, 820))
            )
        return spans

    def test_unique_top_anchor_with_unique_action_is_root(self):
        self.assertTrue(
            runtime._is_coin_root_page(self._root_spans(), (1080, 1920))
        )

    def test_multiple_anchors_with_top_presence_is_still_root(self):
        # 真机首页"淘金币"出现多处：顶部有锚点即根页，不要求全局唯一
        self.assertTrue(
            runtime._is_coin_root_page(
                self._root_spans(anchor_count=2), (1080, 1920)
            )
        )

    def test_anchor_below_top_region_fails(self):
        self.assertFalse(
            runtime._is_coin_root_page(
                self._root_spans(anchor_y=1000), (1080, 1920)
            )
        )

    def test_missing_action_still_root_if_anchor_unique_and_top(self):
        # 按钮缺失属点击前校验（走有界重试），不影响根页身份判定
        self.assertTrue(
            runtime._is_coin_root_page(
                self._root_spans(action=False), (1080, 1920)
            )
        )

    def test_no_anchor_fails(self):
        spans = [
            OcrSpan("赚更多金币", 0.99, (500, 800), (450, 780, 550, 820))
        ]
        self.assertFalse(runtime._is_coin_root_page(spans, (1080, 1920)))


class PopupReopenRetryTests(unittest.TestCase):
    def _coin_page_spans(self, include_action=False):
        spans = [
            OcrSpan("淘金币", 0.99, (300, 100), (250, 80, 350, 120)),
        ]
        if include_action:
            spans.append(
                OcrSpan(
                    "赚更多金币", 0.99, (500, 800),
                    (450, 780, 550, 820),
                )
            )
        return spans

    def test_refuses_to_scroll_when_not_coin_root_page(self):
        # 仅包名安全但非淘金币根页（无"淘金币"锚点）时：零滑动、零点击。
        device = _GestureDevice()
        spans_no_anchor = [
            OcrSpan("赚更多金币", 0.99, (500, 800), (450, 780, 550, 820)),
        ]
        with patch.object(
            runtime, "ocr_screen",
            side_effect=[spans_no_anchor],
        ) as read, patch.object(
            runtime, "in_taobao_and_safe", return_value=True
        ) as safe, patch.object(runtime, "safe_tap") as tap, patch.object(
            runtime, "_deadline_sleep"
        ):
            result = runtime._reopen_task_popup(
                device, None, (1080, 1920), deadline=None
            )
        self.assertFalse(result)
        self.assertEqual(device.swipes, [])  # 未知页面零滑动
        tap.assert_not_called()

    def test_scrolls_to_top_before_first_action_click(self):
        # 页面可能被系统/用户滚动到推荐区：推荐卡片上的"赚更多金币"是
        # 假入口，点进去不是任务弹窗。首attempt必须先滚回顶部再点击。
        device = _GestureDevice()
        spans = self._coin_page_spans(True)
        with patch.object(
            runtime, "ocr_screen",
            side_effect=[spans, spans],
        ) as read, patch.object(
            runtime, "in_taobao_and_safe", return_value=True
        ) as safe, patch.object(
            runtime, "safe_tap", return_value=True
        ) as tap, patch.object(
            runtime, "retry_entry_validation", return_value=True
        ), patch.object(
            runtime, "_deadline_sleep"
        ):
            result = runtime._reopen_task_popup(
                device, None, (1080, 1920), deadline=None
            )
        self.assertTrue(result)
        # 滚顶 = 手指从 y0.45 滑到 y0.70（页面上滚回顶部）
        self.assertTrue(
            any(s[0] == s[2] and s[3] > s[1] for s in device.swipes),
            f"应包含向上滚顶动作: {device.swipes}",
        )
        tap.assert_called_once_with(device, (500, 800), (1080, 1920))

    def test_retries_when_action_is_missing_on_first_ocr(self):
        device = _WorkingDevice()
        with patch.object(
            runtime,
            "ocr_screen",
            side_effect=[
                self._coin_page_spans(False),
                self._coin_page_spans(True),
            ],
        ) as read, patch.object(
            runtime, "in_taobao_and_safe", return_value=True
        ) as safe, patch.object(
            runtime, "safe_tap", return_value=True
        ) as tap, patch.object(
            runtime, "retry_entry_validation", return_value=True
        ), patch.object(
            runtime, "_deadline_sleep"
        ):
            result = runtime._reopen_task_popup(
                device, None, (1080, 1920), deadline=None
            )

        self.assertTrue(result)
        self.assertEqual(read.call_count, 2)
        self.assertEqual(safe.call_count, read.call_count)
        tap.assert_called_once_with(device, (500, 800), (1080, 1920))

    def test_returns_false_without_click_after_bounded_missing_action_attempts(self):
        device = _WorkingDevice()
        missing = self._coin_page_spans(False)
        with patch.object(
            runtime,
            "ocr_screen",
            side_effect=[missing] * (runtime.ENTRY_VALIDATION_RETRIES + 2) * 4,
        ) as read, patch.object(
            runtime, "in_taobao_and_safe", return_value=True
        ) as safe, patch.object(
            runtime, "safe_tap"
        ) as tap, patch.object(
            runtime, "_deadline_sleep"
        ):
            result = runtime._reopen_task_popup(
                device, None, (1080, 1920), deadline=None
            )

        self.assertFalse(result)
        # 按钮缺失：绝不点击。读屏次数含 attempt 0 的下滑探索读屏
        # （2026-09-04 起入口可能被卡片挤出首屏），故用下界断言
        self.assertGreaterEqual(
            read.call_count, runtime.ENTRY_VALIDATION_RETRIES + 2
        )
        self.assertEqual(safe.call_count, read.call_count)
        tap.assert_not_called()

    def test_returns_false_when_action_is_not_unique_after_bounded_attempts(self):
        device = _WorkingDevice()
        duplicate = self._coin_page_spans(True)
        duplicate.append(
            OcrSpan(
                "赚更多金币", 0.99, (700, 800),
                (650, 780, 750, 820),
            )
        )
        with patch.object(
            runtime,
            "ocr_screen",
            side_effect=[duplicate] * (runtime.ENTRY_VALIDATION_RETRIES + 2) * 4,
        ) as read, patch.object(
            runtime, "in_taobao_and_safe", return_value=True
        ) as safe, patch.object(runtime, "safe_tap") as tap, patch.object(
            runtime, "_deadline_sleep"
        ):
            result = runtime._reopen_task_popup(
                device, None, (1080, 1920), deadline=None
            )

        self.assertFalse(result)
        # 按钮缺失：绝不点击。读屏次数含 attempt 0 的下滑探索读屏
        # （2026-09-04 起入口可能被卡片挤出首屏），故用下界断言
        self.assertGreaterEqual(
            read.call_count, runtime.ENTRY_VALIDATION_RETRIES + 2
        )
        self.assertEqual(safe.call_count, read.call_count)
        tap.assert_not_called()

    def test_stops_when_retry_screen_is_unsafe(self):
        device = _WorkingDevice()
        with patch.object(
            runtime,
            "ocr_screen",
            side_effect=[
                self._coin_page_spans(False),
                self._coin_page_spans(True),
            ],
        ) as read, patch.object(
            runtime,
            "in_taobao_and_safe",
            side_effect=[True, False],
        ) as safe, patch.object(runtime, "safe_tap") as tap, patch.object(
            runtime, "_deadline_sleep"
        ):
            result = runtime._reopen_task_popup(
                device, None, (1080, 1920), deadline=None
            )

        self.assertFalse(result)
        self.assertEqual(read.call_count, 2)
        self.assertEqual(safe.call_count, read.call_count)
        tap.assert_not_called()

    def test_rechecks_action_after_validation_failure_before_second_tap(self):
        device = _WorkingDevice()
        events = []

        def read_screen(*_args, **_kwargs):
            events.append("ocr")
            # attempt0 首读有按钮 → 滚顶后重读仍有按钮（主页顶部）→ 点击并
            # 校验失败 → attempt1/2 无按钮 → 有界失败
            frames = [
                self._coin_page_spans(True),
                self._coin_page_spans(True),
                self._coin_page_spans(False),
                self._coin_page_spans(False),
            ]
            return frames[events.count("ocr") - 1]

        def check_safety(*_args, **_kwargs):
            events.append("safe")
            return True

        def tap(*_args, **_kwargs):
            events.append("tap")
            return True

        def validate(*_args, **_kwargs):
            events.append("validate")
            return None

        with patch.object(
            runtime,
            "ocr_screen",
            side_effect=read_screen,
        ) as read, patch.object(
            runtime, "in_taobao_and_safe", side_effect=check_safety
        ) as safe, patch.object(
            runtime, "safe_tap", side_effect=tap
        ) as tap, patch.object(
            runtime, "retry_entry_validation", side_effect=validate
        ), patch.object(
            runtime, "_deadline_sleep"
        ):
            result = runtime._reopen_task_popup(
                device, None, (1080, 1920), deadline=None
            )

        self.assertFalse(result)
        first_validation = events.index("validate")
        # 校验失败后必须重新读屏再重试（而不是原地重复点击）
        ocrs_after_validation = [
            i for i, event in enumerate(events)
            if event == "ocr" and i > first_validation
        ]
        self.assertTrue(ocrs_after_validation)
        self.assertEqual(read.call_count, 4)
        self.assertEqual(safe.call_count, read.call_count)
        self.assertEqual(tap.call_count, 1)


class RefreshRecoveryTests(unittest.TestCase):
    def test_refresh_candidate_total_change_is_a_new_task_cycle(self):
        target = BrowseTarget(
            "搜一搜你心仪的宝贝", 0, 6,
            (300, 500), "去完成", (800, 500), 0.99,
        )
        classifier = getattr(
            runtime,
            "classify_refreshed_task",
            lambda *_args, **_kwargs: ("continue", ""),
        )

        status, reason = classifier(
            target, expected_progress=1, expected_total=5
        )

        self.assertEqual(status, "rotated")
        self.assertEqual(reason, "task_total_changed")

    def test_refresh_candidate_progress_reset_is_a_new_task_cycle(self):
        target = BrowseTarget(
            "搜一搜你心仪的宝贝", 0, 5,
            (300, 500), "去完成", (800, 500), 0.99,
        )
        classifier = getattr(
            runtime,
            "classify_refreshed_task",
            lambda *_args, **_kwargs: ("continue", ""),
        )

        status, reason = classifier(
            target, expected_progress=1, expected_total=5
        )

        self.assertEqual(status, "rotated")
        self.assertEqual(reason, "task_progress_reset")

    def test_refresh_recovery_applies_task_identity_classification(self):
        target = BrowseTarget(
            "搜一搜你心仪的宝贝", 0, 6,
            (300, 500), "去完成", (800, 500), 0.99,
        )
        action_spans = [
            OcrSpan("淘金币", 0.99, (300, 100), (250, 80, 350, 120)),
            OcrSpan("赚更多金币", 0.99, (500, 800), (450, 780, 550, 820)),
        ]
        with patch.object(runtime, "in_taobao_and_safe", return_value=True), \
             patch.object(runtime, "back_to_coin_page_ocr", return_value=True), \
             patch.object(runtime, "ocr_screen", return_value=action_spans), \
             patch.object(runtime, "safe_tap", return_value=True), \
             patch.object(runtime, "on_task_list", return_value=True), \
             patch.object(runtime, "locate_safe_browse_target", return_value=ScanOutcome.found(target)), \
             patch.object(runtime, "_popup_scroll"), \
             patch.object(runtime.time, "sleep"), \
             patch.object(
                 runtime,
                 "classify_refreshed_task",
                 return_value=("rotated", "task_total_changed"),
                 create=True,
             ) as classify:
            result = runtime.refresh_task_after_disappearance(
                _WorkingDevice(), None, (1080, 1920),
                "搜一搜你心仪的宝贝", max_attempts=2
            )

        self.assertEqual(result.status, "rotated")
        self.assertEqual(result.reason, "task_total_changed")
        classify.assert_called_once()

    def test_back_to_coin_page_does_not_back_out_from_unknown_taobao_page(self):
        class PressDevice(_WorkingDevice):
            def __init__(self):
                self.presses = []

            def press(self, key):
                self.presses.append(key)

        device = PressDevice()
        unknown_spans = [
            OcrSpan("关注", 0.99, (100, 120), (50, 100, 150, 140)),
        ]
        with patch.object(runtime, "ocr_screen", return_value=unknown_spans), \
             patch.object(runtime, "in_taobao_and_safe", return_value=True), \
             patch.object(runtime, "is_product_detail_page", return_value=False), \
             patch.object(runtime.time, "sleep"):
            result = runtime.back_to_coin_page_ocr(
                device, None, max_backs=runtime.MAX_BACKS
            )
        self.assertFalse(result)
        self.assertEqual(device.presses, [])

    def test_refresh_recovery_reopens_more_coins_and_returns_same_task(self):
        target = BrowseTarget(
            "发现精选好物", 1, 3, (300, 500), "去完成", (800, 500), 0.99
        )
        action_spans = [
            OcrSpan("淘金币", 0.99, (300, 100), (250, 80, 350, 120)),
            OcrSpan("赚更多金币", 0.99, (500, 800), (450, 780, 550, 820)),
        ]
        calls = []
        with patch.object(runtime, "in_taobao_and_safe", return_value=True), \
             patch.object(runtime, "back_to_coin_page_ocr", return_value=True) as back, \
             patch.object(runtime, "ocr_screen", return_value=action_spans), \
             patch.object(runtime, "safe_tap", side_effect=lambda *_: calls.append("tap") or True), \
             patch.object(runtime, "on_task_list", return_value=True), \
             patch.object(runtime, "locate_safe_browse_target", return_value=ScanOutcome.found(target)), \
             patch.object(runtime, "_popup_scroll",
                          side_effect=lambda *a, **k: calls.append("scroll")), \
             patch.object(runtime.time, "sleep"):
            result = runtime.refresh_task_after_disappearance(
                _WorkingDevice(), None, (1080, 1920), "发现精选好物", max_attempts=2
            )
        self.assertEqual(result.status, "continue")
        self.assertEqual(result.target.progress, 1)
        self.assertEqual(back.call_count, 1)
        self.assertEqual(calls, ["tap", "scroll"])

    def test_refresh_recovery_stops_after_two_not_found_attempts(self):
        action_spans = [
            OcrSpan("淘金币", 0.99, (300, 100), (250, 80, 350, 120)),
            OcrSpan("赚更多金币", 0.99, (500, 800), (450, 780, 550, 820)),
        ]
        with patch.object(runtime, "in_taobao_and_safe", return_value=True), \
             patch.object(runtime, "back_to_coin_page_ocr", return_value=True) as back, \
             patch.object(runtime, "ocr_screen", return_value=action_spans), \
             patch.object(runtime, "safe_tap", return_value=True), \
             patch.object(runtime, "on_task_list", return_value=True), \
             patch.object(runtime, "locate_safe_browse_target", return_value=ScanOutcome.not_found()), \
             patch.object(runtime, "_popup_scroll"), \
             patch.object(runtime.time, "sleep"):
            result = runtime.refresh_task_after_disappearance(
                _WorkingDevice(), None, (1080, 1920), "发现精选好物", max_attempts=2
            )
        self.assertEqual(result.status, "not_found")
        self.assertEqual(result.attempts, 2)
        self.assertEqual(back.call_count, 2)

    def test_refresh_recovery_fails_closed_before_external_screen_click(self):
        class ExternalDevice(_WorkingDevice):
            def app_current(self):
                return {"package": "com.ss.android.article.lite"}

        with patch.object(runtime, "ocr_screen", return_value=[]), \
             patch.object(runtime, "safe_tap") as tap:
            result = runtime.refresh_task_after_disappearance(
                ExternalDevice(), None, (1080, 1920), "发现精选好物", max_attempts=2
            )
        self.assertEqual(result.status, "unsafe")
        tap.assert_not_called()


class StartupPopupRecoveryTests(unittest.TestCase):
    def test_execute_reopens_popup_when_initial_anchor_is_missing(self):
        device = _WorkingDevice()
        logger = _FakeRuntimeLogger()
        with patch.object(runtime, "on_task_list", return_value=False), \
             patch.object(runtime, "_reopen_task_popup", return_value=True) as reopen, \
             patch.object(runtime, "run_safe_browse_tasks", return_value=([], [], [])) as run:
            outcome = runtime._execute_scan(
                device, None, 1, logger, RunMode.EXECUTE,
            )

        reopen.assert_called_once_with(device, None, (1080, 1920), deadline=None)
        run.assert_called_once()
        self.assertEqual(
            (outcome.status, outcome.reason),
            (RunStatus.SUCCESS, "completed"),
        )

    def test_build_strategy_context_wires_logger_diagnostic(self):
        device = _WorkingDevice()
        logger = _FakeRuntimeLogger()
        context = runtime.build_strategy_context(
            device, None, (1080, 1920), logger=logger
        )
        context.emit_diagnostic({"reason": "probe", "span_count": 3})
        self.assertIn(
            ("page_diagnostic",
             {"reason": "probe", "diagnostic": {"span_count": 3}}),
            logger.events,
        )

    def test_build_strategy_context_without_logger_has_no_diagnostic(self):
        device = _WorkingDevice()
        context = runtime.build_strategy_context(device, None, (1080, 1920))
        self.assertIsNone(context.emit_diagnostic)

    def test_entry_failure_emits_recovery_fingerprint(self):
        device = _WorkingDevice()
        logger = _FakeRuntimeLogger()
        stuck = [OcrSpan("神秘中间页", 0.9, (100, 100), (50, 80, 200, 130))]
        with patch.object(runtime, "on_task_list", return_value=False),              patch.object(runtime, "_reopen_task_popup", return_value=False),              patch.object(runtime, "_safe_back_to_coin_page", return_value=False),              patch.object(runtime, "_recover_to_home_and_renavigate", return_value=False),              patch.object(runtime, "ocr_screen", return_value=stuck),              patch.object(runtime, "run_safe_browse_tasks") as run:
            outcome = runtime._execute_scan(
                device, None, 1, logger, RunMode.EXECUTE,
            )
        self.assertEqual(
            (outcome.status, outcome.reason),
            (RunStatus.STARTUP_FAILED, "list_anchor_missing"),
        )
        diag = [e for e in logger.events if e[0] == "page_diagnostic"]
        self.assertEqual(len(diag), 1)
        self.assertEqual(diag[0][1]["reason"], "entry_walk_back_failed")
        run.assert_not_called()

    def test_execute_walks_back_from_flow_page_then_reopens(self):
        """弹窗重开失败但当前页是已知流程页：页面感知回退到根页后重开成功。"""
        device = _WorkingDevice()
        logger = _FakeRuntimeLogger()
        with patch.object(runtime, "on_task_list", return_value=False), \
             patch.object(
                 runtime, "_reopen_task_popup", side_effect=[False, True]
             ) as reopen, \
             patch.object(
                 runtime, "_safe_back_to_coin_page", return_value=True
             ) as walk_back, \
             patch.object(
                 runtime, "run_safe_browse_tasks", return_value=([], [], [])
             ) as run:
            outcome = runtime._execute_scan(
                device, None, 1, logger, RunMode.EXECUTE,
            )

        walk_back.assert_called_once()
        self.assertEqual(reopen.call_count, 2)
        run.assert_called_once()
        self.assertEqual(
            (outcome.status, outcome.reason),
            (RunStatus.SUCCESS, "completed"),
        )

    def test_execute_stops_when_popup_reopen_returns_false(self):
        device = _WorkingDevice()
        logger = _FakeRuntimeLogger()
        with patch.object(runtime, "on_task_list", return_value=False), \
             patch.object(runtime, "_reopen_task_popup", return_value=False) as reopen, \
             patch.object(
                 runtime, "_safe_back_to_coin_page", return_value=False
             ) as walk_back, \
             patch.object(runtime, "run_safe_browse_tasks") as run:
            outcome = runtime._execute_scan(
                device, None, 1, logger, RunMode.EXECUTE,
            )

        walk_back.assert_called_once()
        reopen.assert_called_once()
        run.assert_not_called()
        self.assertEqual(
            (outcome.status, outcome.reason),
            (RunStatus.STARTUP_FAILED, "list_anchor_missing"),
        )

    def test_execute_maps_popup_reopen_exception_to_startup_failure(self):
        device = _WorkingDevice()
        logger = _FakeRuntimeLogger()
        with patch.object(runtime, "on_task_list", return_value=False), \
             patch.object(
                 runtime,
                 "_reopen_task_popup",
                 side_effect=RuntimeError("ocr failed"),
             ), \
             patch.object(runtime, "run_safe_browse_tasks") as run:
            outcome = runtime._execute_scan(
                device, None, 1, logger, RunMode.EXECUTE,
            )

        run.assert_not_called()
        self.assertEqual(
            (outcome.status, outcome.reason),
            (RunStatus.STARTUP_FAILED, "task_list_check_failed"),
        )

    def test_execute_does_not_reopen_popup_when_already_on_task_list(self):
        device = _WorkingDevice()
        logger = _FakeRuntimeLogger()
        with patch.object(runtime, "on_task_list", return_value=True), \
             patch.object(runtime, "_reopen_task_popup") as reopen, \
             patch.object(
                 runtime,
                 "run_safe_browse_tasks",
                 return_value=([], [], []),
             ):
            outcome = runtime._execute_scan(
                device, None, 1, logger, RunMode.EXECUTE,
            )

        reopen.assert_not_called()
        self.assertEqual(
            (outcome.status, outcome.reason),
            (RunStatus.SUCCESS, "completed"),
        )

    def test_execute_keeps_unverified_partial_progress_incomplete(self):
        device = _WorkingDevice()
        logger = _FakeRuntimeLogger()
        target = BrowseTarget(
            title="发现精选好物", progress=1, total=3,
            title_center=(391, 300), action_text="去完成",
            action_center=(943, 320), confidence=0.9,
        )
        result = ImmersiveRunResult(
            completed=False,
            progress=2,
            successful_steps=1,
            reason="ok",
            transitions=((1, 2),),
        )
        with patch.object(runtime, "on_task_list", return_value=True), \
             patch.object(runtime, "locate_safe_browse_target", return_value=ScanOutcome.found(target)), \
             patch.object(runtime, "run_one_safe_browse_task", return_value=(result, True)), \
             patch.object(runtime, "_settle_back_to_coin_page", return_value=True):
            outcome = runtime._execute_scan(
                device, None, 1, logger, RunMode.EXECUTE,
                task_key="featured_goods",
            )

        self.assertEqual(outcome.status, RunStatus.PARTIAL)
        self.assertEqual(outcome.reason, "incomplete")


class ExecuteDeadlineTests(unittest.TestCase):
    """execute 路径总时限/单任务时限/超时恢复（fake clock/sleeper + 记录动作设备）。"""

    def _run_execute(self, raw_results, *, run_timeout=60, task_timeout=30,
                     recovery_timeout=10, max_tasks=2, patches=()):
        clock = FakeClock()
        sleeper = FakeSleeper(clock)
        device = RecordingDeadlineDevice(clock)
        reader = _FakeReader(raw_results)
        with ExitStack() as stack:
            for attr, value in patches:
                stack.enter_context(patch.object(runtime, attr, new=value))
            # 收尾回退（2026-09-02 新增）在这些测试里是 no-op：本类验证
            # deadline/中断语义，收尾行为由专门的 finally 收尾测试覆盖。
            stack.enter_context(patch.object(
                runtime, "_settle_back_to_coin_page", return_value=True,
            ))
            outcome = runtime.run_ocr_entry(
                serial="test-device",
                max_tasks=max_tasks,
                run_timeout=run_timeout,
                task_timeout=task_timeout,
                recovery_timeout=recovery_timeout,
                connect=lambda _serial: device,
                reader_factory=lambda *_args, **_kwargs: reader,
                logger_factory=_fake_logger_factory,
                clock=clock,
                sleeper=sleeper,
            )
        return outcome, clock, device

    def test_run_entry_finally_settles_back_to_coin_page(self):
        # 用户 2026-09-02：执行完（含超时/异常路径）必须退出到初始界面——
        # _run_entry 的 finally 必须调用收尾回退；且收尾带独立动作预算
        # （scope="settle"，Codex 审计 P0-1 后半：超时后不无限期动作）
        clock = FakeClock()
        sleeper = FakeSleeper(clock)
        device = RecordingDeadlineDevice(clock)
        reader = _FakeReader([ANCHOR_RAW] + UNKNOWN_ROW_RAW)
        called = {}
        with ExitStack() as stack:
            stack.enter_context(patch.object(
                runtime, "_settle_back_to_coin_page",
                side_effect=lambda _d, _r, deadline=None: (
                    called.setdefault("settled", True),
                    called.setdefault("deadline", deadline),
                ),
            ))
            runtime.run_ocr_entry(
                serial="test-device",
                max_tasks=2,
                run_timeout=5,
                task_timeout=30,
                recovery_timeout=10,
                connect=lambda _serial: device,
                reader_factory=lambda *_args, **_kwargs: reader,
                logger_factory=_fake_logger_factory,
                clock=clock,
                sleeper=sleeper,
            )
        self.assertTrue(called.get("settled"))
        settle_deadline = called.get("deadline")
        self.assertIsNotNone(settle_deadline)
        self.assertEqual(settle_deadline.scope, "settle")

    def test_total_deadline_includes_target_location(self):
        # 列表锚点在屏、但无已注册任务：定位阶段滚动把总时限耗尽，
        # 超时后不得再有任何点击/滑动/返回动作（恢复只读屏即成功）。
        outcome, clock, device = self._run_execute(
            [ANCHOR_RAW] + UNKNOWN_ROW_RAW,
            run_timeout=5,
            recovery_timeout=10,
        )
        self.assertEqual(
            (outcome.status, outcome.reason, outcome.exit_code),
            (RunStatus.TIMED_OUT, "run_timeout", ExitCode.TIMED_OUT),
        )
        self.assertEqual([a for a in device.actions if a[0] >= 5.0], [])
        self.assertEqual(clock(), 5.0)

    def test_task_deadline_starts_after_target_is_located(self):
        target = BrowseTarget(
            "搜一搜绝密关键词", 0, 5, (300, 500), "去完成", (800, 500), 0.99,
        )

        def slow_locate(d, reader, screen, only_titles=None, exclude_titles=(),
                        max_scrolls=runtime.MAX_LIST_SCROLLS, deadline=None):
            # 定位阶段消耗 30 秒（受总 deadline 约束）
            if deadline is not None:
                deadline.sleep(30)
            return ScanOutcome.found(target)

        def consuming_strategy(strategy, context, browse_count):
            for _ in range(1000):
                context.sleep(2)
            return StrategyResult(True)

        outcome, clock, device = self._run_execute(
            [ANCHOR_RAW] + _raw_row(300, "搜一搜绝密关键词(0/5)"),
            run_timeout=60,
            task_timeout=20,
            max_tasks=1,
            patches=[
                ("locate_safe_browse_target", slow_locate),
                ("enter_task_from_list", lambda *a, **k: True),
                ("execute_task_strategy", consuming_strategy),
                ("back_to_task_list_ocr", lambda *a, **k: True),
            ],
        )
        self.assertEqual(
            (outcome.status, outcome.reason, outcome.exit_code),
            (RunStatus.TIMED_OUT, "task_timeout", ExitCode.TIMED_OUT),
        )
        # 定位 30s 之后任务时限再走 20s → 50s；若从运行开始计时则只会到 20s
        self.assertEqual(clock(), 50.0)

    def test_timeout_never_advances_to_next_task(self):
        entered = []

        def fake_enter(d, reader, title, deadline=None):
            profile = runtime.profile_for_title(title)
            entered.append(runtime.TASK_KEYS[profile.key] if profile else title)
            return True

        def consuming_strategy(strategy, context, browse_count):
            for _ in range(1000):
                context.sleep(2)
            return StrategyResult(True)

        outcome, clock, device = self._run_execute(
            [ANCHOR_RAW] + _raw_row(300, "搜一搜绝密关键词(0/5)"),
            run_timeout=60,
            task_timeout=5,
            max_tasks=2,
            patches=[
                ("enter_task_from_list", fake_enter),
                ("execute_task_strategy", consuming_strategy),
                ("back_to_task_list_ocr", lambda *a, **k: True),
            ],
        )
        self.assertEqual(
            (outcome.status, outcome.reason, outcome.exit_code),
            (RunStatus.TIMED_OUT, "task_timeout", ExitCode.TIMED_OUT),
        )
        self.assertEqual(entered, ["search_discovery"])
        self.assertEqual(outcome.counts.attempted, 1)
        self.assertEqual(len(entered), 1)

    def test_retry_validation_rethrows_deadline(self):
        with self.assertRaises(DeadlineExceeded):
            runtime.retry_entry_validation(
                lambda: (_ for _ in ()).throw(DeadlineExceeded("task"))
            )

    def test_recovery_success_still_returns_timeout(self):
        # 恢复期列表锚点在屏：on_task_list 立即成功，恢复不额外消耗时间
        outcome, clock, device = self._run_execute(
            [ANCHOR_RAW] + UNKNOWN_ROW_RAW,
            run_timeout=5,
            recovery_timeout=10,
        )
        self.assertEqual(outcome.exit_code, ExitCode.TIMED_OUT)
        self.assertEqual(outcome.reason, "run_timeout")
        self.assertEqual(clock(), 5.0)

    def test_recovery_failure_still_returns_timeout_and_bounds_window(self):
        # 恢复期始终“安全但不在列表”(off_list)：有界按 back，直到 recovery deadline
        def fake_off_list(d, reader, *args, **kwargs):
            return "off_list"

        outcome, clock, device = self._run_execute(
            [ANCHOR_RAW] + UNKNOWN_ROW_RAW,
            run_timeout=5,
            recovery_timeout=10,
            patches=[("read_task_list_state", fake_off_list)],
        )
        self.assertEqual(
            (outcome.status, outcome.reason, outcome.exit_code),
            (RunStatus.TIMED_OUT, "run_timeout", ExitCode.TIMED_OUT),
        )
        # 恢复窗口最多消费配置的 10 秒：5（运行）+ 10（恢复）
        self.assertEqual(clock(), 15.0)
        presses = [a for a in device.actions if a[1] == "press"]
        self.assertTrue(presses)

    def test_task_phase_respects_earlier_run_deadline(self):
        # 总时限 25s、定位耗 20s、任务时限 20s → 子 deadline 到期日取 min(25, 40)=25
        target = BrowseTarget(
            "搜一搜绝密关键词", 0, 5, (300, 500), "去完成", (800, 500), 0.99,
        )

        def slow_locate(d, reader, screen, only_titles=None, exclude_titles=(),
                        max_scrolls=runtime.MAX_LIST_SCROLLS, deadline=None):
            if deadline is not None:
                deadline.sleep(20)
            return ScanOutcome.found(target)

        def consuming_strategy(strategy, context, browse_count):
            for _ in range(1000):
                context.sleep(2)
            return StrategyResult(True)

        outcome, clock, device = self._run_execute(
            [ANCHOR_RAW] + _raw_row(300, "搜一搜绝密关键词(0/5)"),
            run_timeout=25,
            task_timeout=20,
            max_tasks=1,
            patches=[
                ("locate_safe_browse_target", slow_locate),
                ("enter_task_from_list", lambda *a, **k: True),
                ("execute_task_strategy", consuming_strategy),
                ("back_to_task_list_ocr", lambda *a, **k: True),
            ],
        )
        self.assertEqual(
            (outcome.status, outcome.reason, outcome.exit_code),
            (RunStatus.TIMED_OUT, "task_timeout", ExitCode.TIMED_OUT),
        )
        # 若子 deadline 忽略总时限会走到 40s；这里被 run deadline 提前截断到 25s
        self.assertEqual(clock(), 25.0)

    def test_child_deadline_takes_earlier_of_run_and_task(self):
        clock = FakeClock()
        sleeper = FakeSleeper(clock)
        parent = Deadline.after(25, "run", clock, sleeper)
        clock.advance(20)
        child = parent.child(20, "task")
        self.assertEqual(child.expires_at, parent.expires_at)  # 25 < 40
        self.assertEqual(child.scope, "task")
        roomy = Deadline.after(100, "run", clock, sleeper)
        roomy_child = roomy.child(20, "task")
        self.assertEqual(roomy_child.expires_at, clock() + 20)  # 100 > 40

    def test_executed_without_confirmed_completion_exits_one(self):
        entered = []

        def fake_enter(d, reader, title, deadline=None):
            entered.append(title)
            return True

        def fake_core(**kwargs):
            kwargs["perform_one"]()
            return ImmersiveRunResult(False, 0, 0, "stalled", ())

        outcome, clock, device = self._run_execute(
            [ANCHOR_RAW] + _raw_row(300, "搜一搜绝密关键词(0/5)"),
            run_timeout=60,
            task_timeout=40,
            max_tasks=1,
            patches=[
                ("enter_task_from_list", fake_enter),
                ("execute_task_strategy", lambda s, c, n: StrategyResult(True)),
                ("back_to_task_list_ocr", lambda *a, **k: True),
                ("run_verified_immersive_progress", fake_core),
            ],
        )
        self.assertEqual(outcome.status, RunStatus.PARTIAL)
        self.assertEqual(outcome.exit_code, ExitCode.PARTIAL)
        self.assertEqual(outcome.counts.attempted, 1)
        self.assertEqual(len(entered), 1)


class PublicOutputRedactionTests(unittest.TestCase):
    """execute 公开控制台输出脱敏：只用 safe_label，无坐标/原始标题/OCR 原文。"""

    def _run_execute_output(self, raw_results, *, run_timeout=60, task_timeout=40,
                            max_tasks=1, patches=()):
        clock = FakeClock()
        sleeper = FakeSleeper(clock)
        device = RecordingDeadlineDevice(clock)
        reader = _FakeReader(raw_results)
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            with ExitStack() as stack:
                for attr, value in patches:
                    stack.enter_context(patch.object(runtime, attr, new=value))
                outcome = runtime.run_ocr_entry(
                    serial="test-device",
                    max_tasks=max_tasks,
                    run_timeout=run_timeout,
                    task_timeout=task_timeout,
                    recovery_timeout=10,
                    connect=lambda _serial: device,
                    reader_factory=lambda *_args, **_kwargs: reader,
                    logger_factory=_fake_logger_factory,
                    clock=clock,
                    sleeper=sleeper,
                )
        return outcome, buffer.getvalue()

    def test_execute_console_output_only_safe_labels(self):
        outputs = []
        # 场景1：看看#… 真实进入（定位成功 entry_validated）+ 停滞 + 汇总
        _outcome, text = self._run_execute_output(
            [ANCHOR_RAW] + _raw_row(300, "看看#秘密商品(0/6)"),
            patches=[
                ("execute_task_strategy", lambda s, c, n: StrategyResult(True)),
                ("back_to_task_list_ocr", lambda *a, **k: True),
            ],
        )
        outputs.append(text)
        # 场景2：搜一搜… 真实策略选择发现词 + 策略失败 + 汇总
        _outcome, text = self._run_execute_output(
            [ANCHOR_RAW] + _raw_row(300, "搜一搜绝密关键词(0/5)") + [
                (_box(540, 750), "搜索发现", 0.99),
                (_box(540, 900), "绝密关键词", 0.99),
            ],
            run_timeout=120,
            task_timeout=60,
            patches=[("back_to_task_list_ocr", lambda *a, **k: True)],
        )
        outputs.append(text)
        # 场景3：点前重定位失败 + 安全点击失败（坐标绝不打印）
        clock = FakeClock()
        device = RecordingDeadlineDevice(clock)
        reader = _FakeReader([ANCHOR_RAW] + _raw_row(300, "看看#秘密商品(0/6)"))
        target = BrowseTarget(
            "看看#秘密商品", 0, 6, (300, 500), "去完成", (800, 500), 0.99,
        )
        buffer = io.StringIO()
        with redirect_stdout(buffer), \
             patch.object(runtime, "locate_safe_browse_target",
                          new=lambda *a, **k: ScanOutcome.found(target)), \
             patch.object(runtime, "find_safe_browse_target",
                          new=lambda *a, **k: None), \
             patch.object(runtime, "in_taobao_and_safe",
                          new=lambda *a, **k: True), \
             patch.object(runtime.time, "sleep"):
            runtime.enter_task_from_list(device, reader, "看看#秘密商品")
            runtime.safe_tap(device, (0, 0), (1080, 1920))
        outputs.append(buffer.getvalue())
        # 场景4：未确认完成（任务行消失）+ 汇总
        raw = [ANCHOR_RAW] + _raw_row(300, "看看#秘密商品(0/6)")

        def fake_core(**kwargs):
            kwargs["perform_one"]()
            return ImmersiveRunResult(False, 1, 0, "task_row_unobserved", ())

        _outcome, text = self._run_execute_output(
            raw,
            patches=[
                ("run_verified_immersive_progress", fake_core),
                ("enter_task_from_list", lambda *a, **k: True),
                ("execute_task_strategy", lambda s, c, n: StrategyResult(True)),
                ("back_to_task_list_ocr", lambda *a, **k: True),
                ("REFRESH_RECOVERY_ATTEMPTS", 0),
            ],
        )
        outputs.append(text)

        combined = "\n".join(outputs)
        self.assertIn("看看#…", combined)
        self.assertIn("搜一搜…", combined)
        self.assertIn("entry_validated", combined)
        self.assertIn("action_point_outside_safe_area", combined)
        for forbidden in (
            "秘密商品",
            "绝密关键词",
            "看看#秘密商品",
            "搜一搜绝密关键词",
            "(800, 500)",
            "(943, 320)",
            "(540, 900)",
            "(0, 0)",
        ):
            self.assertNotIn(forbidden, combined)


class InterruptHandlingTests(unittest.TestCase):
    """两阶段 Ctrl+C：dry-run 立即中止退出 130；execute 首次安全恢复、二次立即停止。"""

    def _run_execute(self, on_task_list, logger_factory=_fake_logger_factory,
                     state_fn=None):
        clock = FakeClock()
        sleeper = FakeSleeper(clock)
        device = RecordingDeadlineDevice(clock)
        reader = _FakeReader([ANCHOR_RAW] + _raw_row(300, "搜一搜绝密关键词(0/5)"))

        def raise_interrupt(*_args, **_kwargs):
            raise KeyboardInterrupt()

        state_patch = (
            patch.object(runtime, "read_task_list_state", new=state_fn)
            if state_fn is not None
            else contextlib.nullcontext()
        )
        with patch.object(runtime, "on_task_list", new=on_task_list), \
             patch.object(runtime, "run_safe_browse_tasks", side_effect=raise_interrupt), \
             patch.object(runtime, "_settle_back_to_coin_page", return_value=True), \
             state_patch:
            outcome = runtime.run_ocr_entry(
                serial="test-device",
                max_tasks=1,
                run_timeout=60,
                task_timeout=40,
                recovery_timeout=10,
                connect=lambda _serial: device,
                reader_factory=lambda *_args, **_kwargs: reader,
                logger_factory=logger_factory,
                clock=clock,
                sleeper=sleeper,
            )
        return outcome, clock, device

    def test_dry_run_interrupt_exits_130_without_recovery(self):
        device = ReadOnlyDevice()
        outcome = runtime.run_ocr_entry(
            serial="test-device",
            dry_run=True,
            connect=lambda _serial: device,
            reader_factory=lambda *_args, **_kwargs: _InterruptingReader(),
            logger_factory=_fake_logger_factory,
        )
        self.assertEqual(
            (outcome.status, outcome.exit_code),
            (RunStatus.CANCELLED, ExitCode.CANCELLED),
        )
        self.assertEqual(device.actions, [])

    def test_first_execute_interrupt_attempts_only_safe_recovery(self):
        states = iter(["off_list", "on_list"])  # 恢复首读不在列表→按一次 back，再读回到列表

        def fake_state(_d, _reader, *_args, **_kwargs):
            return next(states)

        def fake_on_list(_d, _reader, *_args, **_kwargs):
            return True  # 初始弹窗检查通过

        outcome, _clock, device = self._run_execute(
            fake_on_list, state_fn=fake_state,
        )
        self.assertEqual(
            (outcome.status, outcome.exit_code),
            (RunStatus.CANCELLED, ExitCode.CANCELLED),
        )
        presses = [a for a in device.actions if a[1] == "press"]
        self.assertEqual(presses, [(0.0, "press", ("back",))])
        self.assertEqual(
            [a for a in device.actions if a[1] in ("click", "swipe")],
            [],
        )

    def test_second_interrupt_stops_recovery_immediately(self):
        calls = {"n": 0}

        def fake_on_task_list(_d, _reader, *_args, **_kwargs):
            calls["n"] += 1
            if calls["n"] == 1:
                return True  # 初始检查通过
            raise KeyboardInterrupt()  # 恢复期第二次 Ctrl+C

        outcome, _clock, device = self._run_execute(fake_on_task_list)
        self.assertEqual(outcome.exit_code, ExitCode.CANCELLED)
        # 二次中止后不再有任何设备动作（不读屏、不返回）
        self.assertEqual(device.actions, [])

    def test_logger_closes_and_finishes_once_on_interrupt(self):
        calls = {"n": 0}

        def fake_on_task_list(_d, _reader, *_args, **_kwargs):
            calls["n"] += 1
            return calls["n"] == 1  # 初始检查通过，恢复读屏失败

        logger = _FakeRuntimeLogger()
        outcome, _clock, _device = self._run_execute(
            fake_on_task_list, logger_factory=lambda *_a, **_k: logger
        )
        self.assertEqual(outcome.exit_code, ExitCode.CANCELLED)
        self.assertEqual(len(logger.events_named("run_finished")), 1)
        self.assertTrue(logger.closed)


class ExitCodeTableTests(unittest.TestCase):
    """表驱动：从稳定场景到退出码（0/1/3/4/5/130），含 main 返回整数与 __main__ SystemExit。"""

    def _entry(self, **kwargs):
        return runtime.run_ocr_entry(
            serial="test-device",
            connect=lambda _serial: ReadOnlyDevice(),
            reader_factory=lambda *_args, **_kwargs: _FakeReader([]),
            logger_factory=_fake_logger_factory,
            **kwargs,
        )

    def test_exit_codes_are_locked_for_every_stable_path(self):
        cases = {
            ExitCode.SUCCESS: self._exit_success,
            ExitCode.PARTIAL: self._exit_unconfirmed,
            ExitCode.STARTUP_FAILED: self._exit_startup,
            ExitCode.SAFETY_STOPPED: self._exit_safety_stop,
            ExitCode.TIMED_OUT: self._exit_timeout,
            ExitCode.CANCELLED: self._exit_interrupt,
        }
        for expected, scenario in cases.items():
            with self.subTest(exit_code=expected.value):
                self.assertEqual(scenario(), expected)

    def _exit_success(self):
        # 空屏无可报告候选：正常空结果，退出 0
        return self._entry(dry_run=True).exit_code

    def _exit_unconfirmed(self):
        # execute 未确认完成：退出 1
        with patch.object(runtime, "on_task_list", return_value=True), \
             patch.object(runtime, "run_safe_browse_tasks",
                          return_value=([], [], ["好物沉浸看(stalled)"])):
            return self._entry().exit_code

    def _exit_startup(self):
        # 设备连接失败：退出 3
        def connect(_serial):
            raise RuntimeError("device offline")

        return runtime.run_ocr_entry(
            serial="test-device",
            connect=connect,
            reader_factory=lambda *_args, **_kwargs: None,
            logger_factory=_fake_logger_factory,
        ).exit_code

    def _exit_safety_stop(self):
        # 致命停止（device_io_error）：退出 4
        with patch.object(runtime, "on_task_list", return_value=True), \
             patch.object(runtime, "run_safe_browse_tasks",
                          return_value=([], [], ["好物沉浸看(device_io_error)"])):
            return self._entry().exit_code

    def _exit_timeout(self):
        # run 超时：退出 5
        with patch.object(runtime, "on_task_list",
                          side_effect=DeadlineExceeded("run")):
            return self._entry().exit_code

    def _exit_interrupt(self):
        # Ctrl+C：退出 130
        def raise_interrupt(*_args, **_kwargs):
            raise KeyboardInterrupt()

        with patch.object(runtime, "on_task_list", return_value=True), \
             patch.object(runtime, "run_safe_browse_tasks", side_effect=raise_interrupt):
            return self._entry().exit_code

    def test_main_returns_integer_exit_code(self):
        # main 默认 watch=True 会真实启动面板进程：测试必须禁用
        with patch.object(
            runtime, "_spawn_watch_panel", return_value=None
        ) as panel, patch.object(
            runtime,
            "run_ocr_entry",
            return_value=RunOutcome(RunMode.EXECUTE, RunStatus.CANCELLED, "interrupted"),
        ):
            code = runtime.main(["--max-tasks", "1"])
        self.assertIsInstance(code, int)
        panel.assert_called_once_with(enabled=True)
        self.assertEqual(code, 130)

    def test_main_block_uses_system_exit(self):
        # subprocess-free：以 runpy 以 __main__ 重放模块入口，设备连接失败应 SystemExit(3)
        fake_u2 = types.ModuleType("uiautomator2")
        fake_u2.connect = lambda _serial: (_ for _ in ()).throw(RuntimeError("offline"))
        with patch.dict(sys.modules, {"uiautomator2": fake_u2}), \
             patch.object(sys, "argv", ["run_taojinbi.py", "--no-watch"]), \
             patch.object(runtime_logging, "create_runtime_logger",
                          return_value=_FakeRuntimeLogger()):
            with self.assertRaises(SystemExit) as raised:
                runpy.run_path(str(runtime.__file__), run_name="__main__")
        self.assertEqual(raised.exception.code, ExitCode.STARTUP_FAILED)


class _ActionDevice:
    """记录 press / swipe 与 click 的设备桩（无 deadline 语义）。"""

    def __init__(self, package=runtime.TB_APP):
        self.package = package
        self.press_count = 0
        self.tap_count = 0
        self.swipe_count = 0
        self.last_swipe = None
        self.last_tap = None

    def window_size(self):
        return (1080, 2400)

    def app_current(self):
        return {"package": self.package}

    def screenshot(self, format="opencv"):
        return object()

    def press(self, *_args, **_kwargs):
        self.press_count += 1

    def swipe(self, *args, **_kwargs):
        self.swipe_count += 1
        self.last_swipe = args

    def click(self, x, y, *_args, **_kwargs):
        self.tap_count += 1
        self.last_tap = (x, y)


class _SequenceReader:
    """按调用顺序返回 OCR 帧的 reader 桩。"""

    def __init__(self, frames):
        self.frames = list(frames)
        self.calls = 0

    def readtext(self, _path):
        frame = self.frames[min(self.calls, len(self.frames) - 1)]
        self.calls += 1
        return frame


_DETAIL_FRAME = [(_box(540, 2000), "立即购买", 0.98)]
_UNKNOWN_FRAME = [(_box(540, 400), "淘宝首页", 0.98)]
_COIN_PAGE_FRAME = [
    (_box(540, 300), "淘金币", 0.99),
    (_box(540, 500), "赚更多金币", 0.99),
]
_LIST_FRAME = [ANCHOR_RAW]
_SEARCH_RESULT_FRAME = [(_box(540, 300), "浏览10秒可领金币", 0.99)]


class BackToTaskListFailClosedTests(unittest.TestCase):
    """back_to_task_list_ocr：OCR 失败/非淘宝包时绝不盲目按返回（Codex P0-3）。"""

    def test_on_list_returns_true_without_back(self):
        device = _ActionDevice()
        reader = _SequenceReader([_LIST_FRAME])
        self.assertTrue(runtime.back_to_task_list_ocr(device, reader))
        self.assertEqual(device.press_count, 0)

    def test_presses_back_through_safe_non_list_page(self):
        device = _ActionDevice()
        # 首帧在淘金币根页（安全但非列表），按一次返回后到达列表
        reader = _SequenceReader([_COIN_PAGE_FRAME, _LIST_FRAME, _LIST_FRAME])
        self.assertTrue(
            runtime.back_to_task_list_ocr(device, reader, max_backs=3)
        )
        self.assertGreaterEqual(device.press_count, 1)

    def test_ocr_failure_never_presses_back(self):
        class RaisingReader:
            def readtext(self, _path):
                raise RuntimeError("ocr backend down")

        device = _ActionDevice()
        self.assertFalse(
            runtime.back_to_task_list_ocr(device, RaisingReader(), max_backs=3)
        )
        self.assertEqual(device.press_count, 0)

    def test_unsafe_package_never_presses_back_even_with_anchor(self):
        # 即使 OCR 文本像任务列表，前台不是淘宝也绝不按返回
        device = _ActionDevice(package="com.other.app")
        reader = _SequenceReader([_LIST_FRAME, _LIST_FRAME, _LIST_FRAME])
        self.assertFalse(
            runtime.back_to_task_list_ocr(device, reader, max_backs=3)
        )
        self.assertEqual(device.press_count, 0)


class SettleBackToCoinPageTests(unittest.TestCase):
    """正常收尾必须退出到淘金币首页（赚更多金币界面）：首页不动、列表/详情返回、未知页不动。"""

    def test_in_coin_page_does_not_press_back(self):
        device = _ActionDevice()
        reader = _SequenceReader([_COIN_PAGE_FRAME])
        self.assertTrue(runtime._settle_back_to_coin_page(device, reader))
        self.assertEqual(device.press_count, 0)

    def test_list_page_returns_to_coin_page(self):
        device = _ActionDevice()
        reader = _SequenceReader(
            [_LIST_FRAME, _LIST_FRAME, _COIN_PAGE_FRAME, _COIN_PAGE_FRAME]
        )
        self.assertTrue(runtime._settle_back_to_coin_page(device, reader))
        self.assertGreaterEqual(device.press_count, 1)

    def test_detail_page_returns_to_coin_page(self):
        device = _ActionDevice()
        reader = _SequenceReader(
            [_DETAIL_FRAME, _DETAIL_FRAME, _COIN_PAGE_FRAME, _COIN_PAGE_FRAME]
        )
        self.assertTrue(runtime._settle_back_to_coin_page(device, reader))
        self.assertGreaterEqual(device.press_count, 1)

    def test_search_result_page_returns_to_coin_page(self):
        # 搜索结果页（"可领"奖励条）是任务流程内页面，允许按返回键退出到首页
        device = _ActionDevice()
        reader = _SequenceReader(
            [_SEARCH_RESULT_FRAME, _SEARCH_RESULT_FRAME,
             _COIN_PAGE_FRAME, _COIN_PAGE_FRAME]
        )
        self.assertTrue(runtime._settle_back_to_coin_page(device, reader))
        self.assertGreaterEqual(device.press_count, 1)

    def test_unknown_page_probes_back_boundedly(self):
        # 用户 2026-09-02：执行完必须退出到初始界面——未知页面也按返回
        # 试探（有界），不再原地停
        device = _ActionDevice()
        reader = _SequenceReader([_UNKNOWN_FRAME] * 6)
        self.assertFalse(runtime._settle_back_to_coin_page(device, reader))
        self.assertGreaterEqual(device.press_count, 1)

    def test_unknown_page_backs_into_coin_page(self):
        # 未知页面按返回后到达淘金币根页 → 成功（收尾达成初始界面）
        device = _ActionDevice()
        reader = _SequenceReader(
            [_UNKNOWN_FRAME, _UNKNOWN_FRAME, _COIN_PAGE_FRAME, _COIN_PAGE_FRAME]
        )
        self.assertTrue(runtime._settle_back_to_coin_page(device, reader))
        self.assertGreaterEqual(device.press_count, 1)

    def test_unsafe_package_does_not_press_back(self):
        device = _ActionDevice(package="com.other.app")
        reader = _SequenceReader([_LIST_FRAME])
        self.assertFalse(runtime._settle_back_to_coin_page(device, reader))
        self.assertEqual(device.press_count, 0)


class SafeBackToCoinPageTests(unittest.TestCase):
    """_safe_back_to_coin_page：页面身份感知回退，根页面绝不越界。"""

    def test_unknown_page_probes_back_boundedly(self):
        """未知页面（含淘宝首页）有界按返回试探（用户 2026-09-02：执行完
        必须退出到初始界面，不原地停）；连续未知 → 耗尽次数失败。"""
        device = _ActionDevice()
        reader = _SequenceReader([_UNKNOWN_FRAME] * 12)
        self.assertFalse(runtime._safe_back_to_coin_page(device, reader))
        self.assertEqual(device.press_count, runtime.MAX_BACKS)

    def test_unknown_page_backs_into_coin_page(self):
        """未知页面按返回后到达淘金币根页 → 成功停下（绝不 back 过冲）。"""
        device = _ActionDevice()
        frames = [_UNKNOWN_FRAME, _COIN_PAGE_FRAME, _COIN_PAGE_FRAME]
        reader = _SequenceReader(frames)
        self.assertTrue(runtime._safe_back_to_coin_page(device, reader))
        self.assertEqual(device.press_count, 1)

    def test_unknown_page_backs_out_of_taobao_stops(self):
        """未知页面按返回后越出淘宝包（非淘宝）→ 立即停止不越界。"""
        device = _ActionDevice(package="com.android.launcher")
        reader = _SequenceReader([_UNKNOWN_FRAME, _UNKNOWN_FRAME])
        self.assertFalse(runtime._safe_back_to_coin_page(device, reader))
        self.assertEqual(device.press_count, 0)

    def test_coin_page_never_backs_past_root(self):
        """已在淘金币根页（赚更多金币可见）→ 不动，零返回。"""
        device = _ActionDevice()
        reader = _SequenceReader([_COIN_PAGE_FRAME, _COIN_PAGE_FRAME])
        self.assertTrue(runtime._safe_back_to_coin_page(device, reader))
        self.assertEqual(device.press_count, 0)


class DailyCheckinHandlerTests(unittest.TestCase):
    """_handle_daily_checkin：有签到弹窗则领取，无则跳过，识别失败不盲点。"""

    def test_claims_when_checkin_popup_present(self):
        # 签到弹窗 + 唯一"立即领取" → 点击 → 等弹窗消失 → True
        checkin = [
            (_box(540, 600), "每日签到", 0.98),
            (_box(540, 900), "立即领取", 0.97),
        ]
        settled = [
            (_box(540, 300), "淘金币", 0.99),
            (_box(540, 597), "赚更多金币", 0.97),
        ]
        reader = _SequenceReader([checkin, settled, settled])
        device = _ActionDevice()
        result = runtime._handle_daily_checkin(device, reader)
        self.assertTrue(result)
        self.assertEqual(device.tap_count, 1)
        self.assertEqual(device.last_tap, (540, 900))

    def test_skips_when_no_checkin_popup(self):
        # 无签到弹窗：正常跳过，零点击
        spans = [
            (_box(540, 300), "淘金币", 0.99),
            (_box(540, 597), "赚更多金币", 0.97),
        ]
        reader = _SequenceReader([spans])
        device = _ActionDevice()
        self.assertTrue(runtime._handle_daily_checkin(device, reader))
        self.assertEqual(device.tap_count, 0)

    def test_ambiguous_claim_button_returns_false_without_tap(self):
        # 同屏两个"领取"歧义 → 不盲点 → False（签到不阻塞但也不误领）
        spans = [
            (_box(540, 600), "每日签到", 0.98),
            (_box(300, 900), "领取", 0.97),
            (_box(700, 900), "领取", 0.96),
        ]
        reader = _SequenceReader([spans, spans, spans])
        device = _ActionDevice()
        result = runtime._handle_daily_checkin(device, reader)
        self.assertFalse(result)
        self.assertEqual(device.tap_count, 0)

    def test_low_confidence_claim_button_returns_false(self):
        # 领取按钮置信度过低（< 0.5）→ find_checkin_claim_button 拒绝 → False
        spans = [
            (_box(540, 600), "每日签到", 0.98),
            (_box(540, 900), "立即领取", 0.4),
        ]
        reader = _SequenceReader([spans, spans, spans])
        device = _ActionDevice()
        result = runtime._handle_daily_checkin(device, reader)
        self.assertFalse(result)
        self.assertEqual(device.tap_count, 0)

    def test_signin_entry_tap_completes_checkin(self):
        # 根页"签到领金币"入口：点击 → 入口消失（变"赚更多金币"）→ True
        with_signin = [
            (_box(220, 138), "淘金币", 0.99),
            (_box(536, 649), "签到领金币", 0.98),
        ]
        settled = [
            (_box(220, 138), "淘金币", 0.99),
            (_box(537, 599), "赚更多金币", 0.97),
        ]
        reader = _SequenceReader([with_signin, settled, settled])
        device = _ActionDevice()
        result = runtime._handle_daily_checkin(device, reader)
        self.assertTrue(result)
        self.assertEqual(device.tap_count, 1)
        self.assertEqual(device.last_tap, (536, 649))

    def test_signin_entry_absent_skips(self):
        # 根页无"签到领金币"入口：跳过（正常路径），零点击
        spans = [
            (_box(220, 138), "淘金币", 0.99),
            (_box(537, 599), "赚更多金币", 0.97),
        ]
        reader = _SequenceReader([spans])
        device = _ActionDevice()
        self.assertTrue(runtime._handle_daily_checkin(device, reader))
        self.assertEqual(device.tap_count, 0)

    def test_signin_entry_tap_but_not_settled_returns_false(self):
        # 点击后入口一直没消失（签到未完成）→ False（不谎报完成）
        with_signin = [
            (_box(220, 138), "淘金币", 0.99),
            (_box(536, 649), "签到领金币", 0.98),
        ]
        reader = _SequenceReader([with_signin, with_signin, with_signin])
        device = _ActionDevice()
        result = runtime._handle_daily_checkin(device, reader)
        self.assertFalse(result)
        self.assertEqual(device.tap_count, 1)


class NavigateHomeToCoinPageTests(unittest.TestCase):
    """_navigate_home_to_coin_page：淘宝首页 → 淘金币根页自动导航。"""

    @staticmethod
    def _home_spans():
        # 标准淘宝首页强信号（顶 tab≥2 + 底栏≥2 + 领淘金币入口）
        return [
            (_box(80, 95), "关注", 0.95),
            (_box(200, 95), "推荐", 0.95),
            (_box(70, 1880), "视频", 0.9),
            (_box(330, 1880), "消息", 0.9),
            (_box(540, 1880), "购物车", 0.9),
            (_box(870, 1880), "我的淘宝", 0.9),
            (_box(290, 410), "领淘金币", 0.95),
        ]

    def test_taps_coin_entry_button_when_home_is_confirmed(self):
        # 淘宝首页强信号（顶 tab + 底栏双命中）+ 领淘金币图标 → 点击后到达淘金币根页
        home_spans = self._home_spans()
        root_spans = [
            (_box(150, 130), "淘金币", 0.99),
            (_box(540, 597), "赚更多金币", 0.97),
        ]
        reader = _SequenceReader([home_spans, root_spans])
        device = _ActionDevice()
        result = runtime._navigate_home_to_coin_page(device, reader, (1080, 1920))
        self.assertTrue(result)
        self.assertEqual(device.tap_count, 1)
        # 校验点到了"领淘金币"图标的中心
        self.assertEqual(device.last_tap, (290, 410))

    def test_returns_false_when_weak_signal_only(self):
        # 弱信号（仅"领淘金币"图标在屏）→ 不盲点
        spans = [(_box(290, 410), "领淘金币", 0.95)]
        reader = _SequenceReader([spans])
        device = _ActionDevice()
        result = runtime._navigate_home_to_coin_page(device, reader, (1080, 1920))
        self.assertFalse(result)
        self.assertEqual(device.tap_count, 0)

    def test_returns_false_on_unknown_page(self):
        # 完全无关页面（无首页锚点）→ 原地停止
        spans = [(_box(150, 130), "未知页面", 0.9)]
        reader = _SequenceReader([spans])
        device = _ActionDevice()
        result = runtime._navigate_home_to_coin_page(device, reader, (1080, 1920))
        self.assertFalse(result)
        self.assertEqual(device.tap_count, 0)

    def test_returns_false_when_entry_icon_missing(self):
        # 强信号首页 + 但 OCR 误读没看到"领淘金币" → 不盲点
        spans = [
            (_box(80, 95), "关注", 0.95),
            (_box(200, 95), "推荐", 0.95),
            (_box(70, 1880), "视频", 0.9),
            (_box(330, 1880), "消息", 0.9),
            (_box(540, 1880), "购物车", 0.9),
            (_box(870, 1880), "我的淘宝", 0.9),
        ]
        reader = _SequenceReader([spans, spans, spans, spans])
        device = _ActionDevice()
        result = runtime._navigate_home_to_coin_page(device, reader, (1080, 1920))
        self.assertFalse(result)
        self.assertEqual(device.tap_count, 0)

    def test_low_confidence_entry_icon_is_rejected(self):
        # 强信号首页 + "领淘金币"图标置信度过低（< 0.5）→ 不盲点
        spans = [
            (_box(80, 95), "关注", 0.95),
            (_box(200, 95), "推荐", 0.95),
            (_box(70, 1880), "视频", 0.9),
            (_box(330, 1880), "消息", 0.9),
            (_box(540, 1880), "购物车", 0.9),
            (_box(870, 1880), "我的淘宝", 0.9),
            (_box(290, 410), "领淘金币", 0.4),
        ]
        device = _ActionDevice()
        reader = _SequenceReader([spans, spans, spans, spans])
        result = runtime._navigate_home_to_coin_page(device, reader, (1080, 1920))
        self.assertFalse(result)
        self.assertEqual(device.tap_count, 0)


    def test_coin_page_stops_without_pressing(self):
        device = _ActionDevice()
        reader = _SequenceReader([_COIN_PAGE_FRAME])
        self.assertTrue(runtime._safe_back_to_coin_page(device, reader))
        self.assertEqual(device.press_count, 0)

    def test_leaving_taobao_stops(self):
        device = _ActionDevice()
        reader = _SequenceReader([_UNKNOWN_FRAME])
        device.package = "com.other.app"
        self.assertFalse(runtime._safe_back_to_coin_page(device, reader))
        self.assertEqual(device.press_count, 0)

    def test_backs_exhaustion_fails_closed(self):
        """已知流程页按返回次数耗尽仍未到根页面 → 失败关闭。"""
        device = _ActionDevice()
        reader = _SequenceReader([_SEARCH_RESULT_FRAME] * 3)
        self.assertFalse(runtime._safe_back_to_coin_page(
            device, reader, max_backs=2,
        ))
        self.assertEqual(device.press_count, 2)


class ReopenTaskPopupScrollTests(unittest.TestCase):
    """2026-09-04 真机：根页"确认收货奖励"等卡片把"赚更多金币"挤出首屏。

    原 `_reopen_task_popup` 重试只读屏不滚动 → 必然失败；现在允许有界
    下滑探索找入口，但**拒绝推荐区假入口**（下滑后顶部无"淘金币"锚点
    即视为已落入推荐区）。
    """

    SCREEN = (1080, 1920)

    @staticmethod
    def _root_without_action():
        # 顶部有"淘金币"锚点，但入口被卡片挤到首屏之外
        return [
            (_box(220, 138), "淘金币", 0.99),
            (_box(305, 251), "确认收货奖励,", 0.95),
            (_box(602, 251), "淘金币+17", 0.95),
        ]

    @staticmethod
    def _root_with_action():
        return [
            (_box(220, 138), "淘金币", 0.99),
            (_box(540, 600), "赚更多金币", 0.97),
        ]

    @staticmethod
    def _feed_fake_action():
        # 推荐区假入口：有"赚更多金币"但顶部无"淘金币"锚点
        return [
            (_box(540, 900), "赚更多金币", 0.97),
            (_box(300, 1200), "话费立减", 0.9),
        ]

    def test_scroll_finds_action_below_the_fold(self):
        device = _ActionDevice()
        reader = _SequenceReader([
            self._root_without_action(),   # 滑动前安全检查（无入口）
            self._root_with_action(),      # 下滑一屏后命中入口
            self._root_with_action(),      # 备用帧
        ])
        action = runtime._scroll_coin_page_for_action(
            device, reader, self.SCREEN,
        )
        self.assertIsNotNone(action)
        self.assertEqual(device.swipe_count, 1)

    def test_rejects_feed_fake_action(self):
        # 下滑后落入推荐区（顶部无"淘金币"锚点）→ 拒绝假入口
        device = _ActionDevice()
        reader = _SequenceReader([self._feed_fake_action()] * 4)
        self.assertIsNone(runtime._scroll_coin_page_for_action(
            device, reader, self.SCREEN,
        ))

    def test_stops_outside_taobao(self):
        device = _ActionDevice(package="com.other.app")
        reader = _SequenceReader([self._root_without_action()])
        self.assertIsNone(runtime._scroll_coin_page_for_action(
            device, reader, self.SCREEN,
        ))
        self.assertEqual(device.swipe_count, 0)

    def test_reopen_succeeds_when_action_below_fold(self):
        # 整体链路：入口被挤出首屏 → 下滑找到 → 点击 → 弹窗打开
        device = _ActionDevice()
        reader = _SequenceReader([
            self._root_without_action(),
            self._root_without_action(),
            self._root_with_action(),
            self._root_with_action(),
            [(ANCHOR_RAW[0], ANCHOR_RAW[1], ANCHOR_RAW[2])],
            [(ANCHOR_RAW[0], ANCHOR_RAW[1], ANCHOR_RAW[2])],
            [(ANCHOR_RAW[0], ANCHOR_RAW[1], ANCHOR_RAW[2])],
        ])
        with patch.object(runtime, "on_task_list", return_value=True):
            result = runtime._reopen_task_popup(device, reader, self.SCREEN)
        self.assertTrue(result)
        self.assertEqual(device.tap_count, 1)


class RecoverToHomeAndRenavigateTests(unittest.TestCase):
    """遗留 #6（2026-09-03 真机实测）：Welcome/频道子页时导航链全败。

    _recover_to_home_and_renavigate 从任意淘宝内页面有界 BACK 回到标准
    淘宝首页（is_home True），让上层能重走完整导航链。
    """

    def _back_to_home_frames(self):
        """未知页(Welcome) → 未知页 → 标准首页 → 标准首页。"""
        home = NavigateHomeToCoinPageTests._home_spans()
        welcome = [(_box(540, 400), "限时秒杀", 0.98)]
        return [welcome, welcome, home, home]

    def test_backs_until_standard_home(self):
        device = _ActionDevice()
        reader = _SequenceReader(self._back_to_home_frames())
        self.assertTrue(runtime._recover_to_home_and_renavigate(
            device, reader, (1080, 1920),
        ))
        self.assertEqual(device.press_count, 2)

    def test_stops_when_already_home(self):
        device = _ActionDevice()
        home = NavigateHomeToCoinPageTests._home_spans()
        reader = _SequenceReader([home, home])
        self.assertTrue(runtime._recover_to_home_and_renavigate(
            device, reader, (1080, 1920),
        ))
        self.assertEqual(device.press_count, 0)

    def test_stops_outside_taobao(self):
        device = _ActionDevice(package="com.android.launcher")
        reader = _SequenceReader([_UNKNOWN_FRAME])
        self.assertFalse(runtime._recover_to_home_and_renavigate(
            device, reader, (1080, 1920),
        ))
        self.assertEqual(device.press_count, 0)

    def test_exhausts_backs_when_never_home(self):
        device = _ActionDevice()
        welcome = [(_box(540, 400), "限时秒杀", 0.98)]
        reader = _SequenceReader([welcome] * 12)
        self.assertFalse(runtime._recover_to_home_and_renavigate(
            device, reader, (1080, 1920),
        ))
        self.assertEqual(device.press_count, runtime.MAX_BACKS)


class ExecuteScanHomeRecoveryIntegrationTests(unittest.TestCase):
    """_execute_scan 导航链全败（Welcome 页/卡片遮挡）时，最终兜底必须
    把手机带回标准首页并重走导航，而不是直接 list_anchor_missing 退出。"""

    def test_recovery_rescues_welcome_page_chain_failure(self):
        device = _WorkingDevice()
        logger = _FakeRuntimeLogger()
        with patch.object(runtime, "on_task_list", return_value=False), \
             patch.object(
                 runtime, "_navigate_home_to_coin_page",
                 side_effect=[False, True],   # Welcome 页失败 → 回首页后成功
             ), \
             patch.object(
                 runtime, "_reopen_task_popup",
                 side_effect=[False, True],   # 首轮失败 → 兜底后成功
             ), \
             patch.object(
                 runtime, "_safe_back_to_coin_page", return_value=False,
             ), \
             patch.object(
                 runtime, "_recover_to_home_and_renavigate", return_value=True,
             ), \
             patch.object(
                 runtime, "run_safe_browse_tasks", return_value=([], [], []),
             ) as run:
            outcome = runtime._execute_scan(
                device, None, 1, logger, RunMode.EXECUTE,
            )
        run.assert_called_once()
        self.assertEqual(
            (outcome.status, outcome.reason),
            (RunStatus.SUCCESS, "completed"),
        )

    def test_recovery_failure_still_stops_closed(self):
        device = _WorkingDevice()
        logger = _FakeRuntimeLogger()
        with patch.object(runtime, "on_task_list", return_value=False), \
             patch.object(runtime, "_navigate_home_to_coin_page",
                          return_value=False), \
             patch.object(runtime, "_reopen_task_popup",
                          return_value=False), \
             patch.object(runtime, "_safe_back_to_coin_page",
                          return_value=False), \
             patch.object(runtime, "_recover_to_home_and_renavigate",
                          return_value=False), \
             patch.object(runtime, "run_safe_browse_tasks") as run:
            outcome = runtime._execute_scan(
                device, None, 1, logger, RunMode.EXECUTE,
            )
        run.assert_not_called()
        self.assertEqual(
            (outcome.status, outcome.reason),
            (RunStatus.STARTUP_FAILED, "list_anchor_missing"),
        )



class TaskKeyRoutingTests(unittest.TestCase):
    """--task 指定任务类型时必须路由为 locate 的 only_titles 标题模式。"""

    def _run(self, task_key):
        device = ReadOnlyDevice()
        reader = _FakeReader([ANCHOR_RAW])
        captured = {}

        def fake_locate(*_args, **_kwargs):
            captured.update(_kwargs)
            return ScanOutcome.not_found()

        with ExitStack() as stack:
            stack.enter_context(patch.object(
                runtime, "locate_safe_browse_target", side_effect=fake_locate,
            ))
            runtime.run_ocr_entry(
                serial="test-device",
                task_key=task_key, max_tasks=1,
                run_timeout=60, task_timeout=40, recovery_timeout=10,
                connect=lambda _serial: device,
                reader_factory=lambda *_a, **_k: reader,
                logger_factory=_fake_logger_factory,
            )
        return captured

    def test_hashtag_routes_to_prefix_title(self):
        captured = self._run("hashtag")
        self.assertEqual(captured.get("only_titles"), ["看看#"])

    def test_search_routes_to_prefix_title(self):
        captured = self._run("search")
        self.assertEqual(captured.get("only_titles"), ["搜一搜"])

    def test_featured_goods_routes_to_exact_title(self):
        captured = self._run("featured_goods")
        self.assertEqual(captured.get("only_titles"), ["发现精选好物"])

    def test_immersive_routes_to_exact_title(self):
        captured = self._run("immersive")
        self.assertEqual(captured.get("only_titles"), ["好物沉浸看"])

    def test_none_routes_to_all_titles(self):
        captured = self._run(None)
        self.assertIsNone(captured.get("only_titles"))


class WatchPanelSpawnTests(unittest.TestCase):
    """进度面板自动弹出：禁用不启动，Windows 上以独立控制台启动。"""

    def test_disabled_returns_none(self):
        self.assertIsNone(runtime._spawn_watch_panel(enabled=False))

    def test_spawns_panel_with_auto_exit_on_windows(self):
        with patch("subprocess.Popen") as popen:
            proc = runtime._spawn_watch_panel(enabled=True)
            if os.name != "nt":
                self.assertIsNone(proc)
                return
            popen.assert_called_once()
            args = popen.call_args[0][0]
            self.assertEqual(args[-1], "--auto-exit")
            self.assertEqual(args[-2], "taojinbi_mav.runtime.watch")
            self.assertEqual(args[-3], "-m")
            self.assertIsNotNone(proc)


class TaskEventLoggingTests(unittest.TestCase):
    """run_safe_browse_tasks 在定位成功后必须 emit task_started/task_finished。"""

    def _build(self):
        class Recorder:
            def __init__(self):
                self.events = []

            def emit(self, event, **kwargs):
                self.events.append((event, kwargs))

        logger = Recorder()
        device = ReadOnlyDevice()
        reader = _FakeReader([ANCHOR_RAW])
        target = BrowseTarget(
            title="搜一搜你心仪的宝贝", progress=0, total=5,
            title_center=(391, 300), action_text="去完成",
            action_center=(943, 320), confidence=0.9,
        )
        result = ImmersiveRunResult(
            completed=False, progress=1, successful_steps=1,
            reason="progress_reset",
            transitions=((0, 1), (1, 0)),
        )
        return logger, device, reader, target, result

    def _run(self, result):
        logger, device, reader, target, _result = self._build()
        with ExitStack() as stack:
            stack.enter_context(patch.object(
                runtime, "locate_safe_browse_target",
             return_value=ScanOutcome.found(target),
            ))
            stack.enter_context(patch.object(
                runtime, "run_one_safe_browse_task", return_value=(result, True),
            ))
            runtime.run_safe_browse_tasks(
                device, reader, max_tasks=1, logger=logger,
            )
        return logger

    def test_emits_task_started_with_registered_key(self):
        logger, device, reader, target, result = self._build()
        with ExitStack() as stack:
            stack.enter_context(patch.object(
                runtime, "locate_safe_browse_target",
             return_value=ScanOutcome.found(target),
            ))
            stack.enter_context(patch.object(
                runtime, "run_one_safe_browse_task", return_value=(result, True),
            ))
            runtime.run_safe_browse_tasks(
                device, reader, max_tasks=1, logger=logger,
            )
        started = [e for e in logger.events if e[0] == "task_started"]
        self.assertEqual(len(started), 1)
        self.assertEqual(started[0][1]["task_key"], "search")
        self.assertEqual(started[0][1]["reason"], "located")

    def test_emits_task_finished_with_stable_status_and_reason(self):
        logger = self._run(
            ImmersiveRunResult(
                completed=False, progress=1, successful_steps=1,
                reason="progress_reset", transitions=((0, 1), (1, 0)),
            )
        )
        finished = [e for e in logger.events if e[0] == "task_finished"]
        self.assertEqual(len(finished), 1)
        self.assertEqual(finished[0][1]["task_key"], "search")
        self.assertEqual(finished[0][1]["status"], "likely_completed")
        self.assertEqual(finished[0][1]["reason"], "progress_reset")

    def test_total_mismatch_after_browse_is_likely_completed(self):
        # 浏览已发生+读到过进度：total 不匹配 = 展示滞后（计数延迟刷新），
        # 与 task_row_unobserved 同口径归"很可能完成"，不误判 unfinished
        result = ImmersiveRunResult(
            completed=False, progress=1, successful_steps=1,
            reason="progress_total_mismatch",
            transitions=((0, 1), (1, 0)),
        )
        logger = self._run(result)
        finished = [e for e in logger.events if e[0] == "task_finished"]
        self.assertEqual(finished[0][1]["status"], "likely_completed")
        self.assertEqual(finished[0][1]["reason"], "progress_total_mismatch")

    def test_no_safe_control_after_browse_is_likely_completed(self):
        # _safe_back 失败但已浏览+读到过进度：浏览实际发生（金币通常到账），
        # 真机 2026-09-01 证实（看看# 1 步 _safe_back 失败但余额 +50）
        result = ImmersiveRunResult(
            completed=False, progress=1, successful_steps=1,
            reason="no_safe_control",
            transitions=((0, 1),),
        )
        logger = self._run(result)
        finished = [e for e in logger.events if e[0] == "task_finished"]
        self.assertEqual(finished[0][1]["status"], "likely_completed")
        self.assertEqual(finished[0][1]["reason"], "no_safe_control")

    def test_no_safe_control_without_browse_stays_unfinished(self):
        # _safe_control 失败但 browsed=False：真没浏览过，不谎报
        logger, device, reader, target, _result = self._build()
        result = ImmersiveRunResult(
            completed=False, progress=0, successful_steps=0,
            reason="no_safe_control", transitions=(),
        )
        with ExitStack() as stack:
            stack.enter_context(patch.object(
                runtime, "locate_safe_browse_target",
             return_value=ScanOutcome.found(target),
            ))
            stack.enter_context(patch.object(
                runtime, "run_one_safe_browse_task",
                return_value=(result, False),   # browsed=False
            ))
            runtime.run_safe_browse_tasks(
                device, reader, max_tasks=1, logger=logger,
            )
        finished = [e for e in logger.events if e[0] == "task_finished"]
        self.assertEqual(finished[0][1]["status"], "unfinished")
        self.assertEqual(finished[0][1]["reason"], "no_safe_control")

    def test_search_result_unavailable_after_browse_is_likely_completed(self):
        # 点击搜索发现关键词但 OCR 漏读结果页：浏览实际发生（淘宝自动计时），
        # 真机 2026-09-01 证实（search 0/5 触发该 reason 但余额 +100）
        result = ImmersiveRunResult(
            completed=False, progress=1, successful_steps=1,
            reason="search_result_unavailable",
            transitions=((0, 1),),
        )
        logger = self._run(result)
        finished = [e for e in logger.events if e[0] == "task_finished"]
        self.assertEqual(finished[0][1]["status"], "likely_completed")
        self.assertEqual(finished[0][1]["reason"], "search_result_unavailable")

    def test_finished_completed_status_for_confirmed_result(self):
        logger = self._run(
            ImmersiveRunResult(
                completed=True, progress=5, successful_steps=5,
                reason="completed", transitions=((4, 5),),
            )
        )
        finished = [e for e in logger.events if e[0] == "task_finished"]
        self.assertEqual(finished[0][1]["status"], "completed")
        self.assertEqual(finished[0][1]["reason"], "completed")

    def test_partial_progress_with_ok_reason_stays_unfinished(self):
        logger = self._run(
            ImmersiveRunResult(
                completed=False,
                progress=2,
                successful_steps=1,
                reason="ok",
                transitions=((1, 2),),
            )
        )
        finished = [event for event in logger.events if event[0] == "task_finished"]
        self.assertEqual(finished[0][1]["status"], "unfinished")
        self.assertEqual(finished[0][1]["reason"], "ok")

    def test_no_task_events_when_logger_is_none(self):
        logger, device, reader, target, result = self._build()
        with ExitStack() as stack:
            stack.enter_context(patch.object(
                runtime, "locate_safe_browse_target",
             return_value=ScanOutcome.found(target),
            ))
            stack.enter_context(patch.object(
                runtime, "run_one_safe_browse_task", return_value=(result, True),
            ))
            runtime.run_safe_browse_tasks(device, reader, max_tasks=1)
        self.assertEqual(logger.events, [])


class RowUnobservedClassificationTests(unittest.TestCase):
    """task_row_unobserved：已浏览+有进度+行消失=完成特征→很可能完成。"""

    def _build(self):
        device = _ActionDevice()
        reader = _FakeReader([ANCHOR_RAW])
        target = BrowseTarget(
            title="看看#斯维诗鱼油", progress=1, total=5,
            title_center=(391, 300), action_text="去完成",
            action_center=(943, 320), confidence=0.9,
        )
        return device, reader, target

    def _run(self, result, browsed=True):
        device, reader, target = self._build()
        with ExitStack() as stack:
            stack.enter_context(patch.object(
                runtime, "locate_safe_browse_target",
             return_value=ScanOutcome.found(target),
            ))
            stack.enter_context(patch.object(
                runtime, "run_one_safe_browse_task", return_value=(result, browsed),
            ))
            return runtime.run_safe_browse_tasks(device, reader, max_tasks=1)

    def _result(self, progress, reason="task_row_unobserved", steps=0):
        return ImmersiveRunResult(
            completed=False, progress=progress, successful_steps=steps,
            reason=reason, transitions=(),
        )

    def test_browsed_with_progress_classifies_likely(self):
        done, likely, unfinished = self._run(self._result(progress=2))
        self.assertEqual(likely, ["看看#…"])
        self.assertEqual(unfinished, [])
        self.assertEqual(done, [])

    def test_browsed_without_progress_stays_unfinished(self):
        done, likely, unfinished = self._run(self._result(progress=0))
        self.assertEqual(unfinished, ["看看#…(task_row_unobserved)"])
        self.assertEqual(likely, [])

    def test_not_browsed_stays_unfinished(self):
        done, likely, unfinished = self._run(self._result(progress=2), browsed=False)
        self.assertEqual(unfinished, ["看看#…(task_row_unobserved)"])
        self.assertEqual(likely, [])


class SettleBackPageAwareTests(unittest.TestCase):
    """结算回退的页面身份感知：以淘金币页为根，绝不越界按返回到首页。"""

    class _PressDevice(_WorkingDevice):
        def __init__(self):
            self.presses = []

        def press(self, *args):
            self.presses.append(args)

    @staticmethod
    def _coin_spans(with_action=True):
        spans = [OcrSpan("淘金币", 0.9, (300, 100), (250, 80, 350, 120))]
        if with_action:
            spans.append(
                OcrSpan("赚更多金币", 0.99, (500, 800), (450, 780, 550, 820))
            )
        return spans

    @staticmethod
    def _entry_spans():
        return [
            OcrSpan("搜索发现", 0.99, (200, 500), (100, 480, 300, 520)),
            OcrSpan("鱼油推荐", 0.99, (300, 700), (200, 680, 400, 720)),
        ]

    def test_arrives_at_coin_page_returns_true_without_backs(self):
        device = self._PressDevice()
        with patch.object(
            runtime, "ocr_screen", return_value=self._coin_spans(True)
        ) as read, patch.object(
            runtime, "in_taobao_and_safe", return_value=True
        ):
            result = runtime._safe_back_to_coin_page(
                device, None, deadline=None
            )
        self.assertTrue(result)
        self.assertEqual(device.presses, [])
        self.assertEqual(read.call_count, 1)

    def test_coin_root_with_obscured_action_stops_in_place(self):
        """站在淘金币页但按钮被遮挡（奖励卡）：原地失败，绝不按返回过冲首页。"""
        device = self._PressDevice()
        with patch.object(
            runtime, "ocr_screen", return_value=self._coin_spans(False)
        ) as read, patch.object(
            runtime, "in_taobao_and_safe", return_value=True
        ):
            result = runtime._safe_back_to_coin_page(
                device, None, deadline=None
            )
        self.assertFalse(result)
        self.assertEqual(device.presses, [])
        self.assertEqual(read.call_count, 2)  # 本体 1 次 + 关弹窗探测 1 次

    def test_unknown_taobao_page_probes_back_boundedly(self):
        # 用户 2026-09-02：执行完必须退出到初始界面——未知页面也按返回
        # 试探（有界 max_backs），连续未知才耗尽次数失败
        device = self._PressDevice()
        with patch.object(
            runtime,
            "ocr_screen",
            return_value=[OcrSpan("随便什么页面", 0.9, (10, 10), (5, 5, 50, 30))],
        ), patch.object(
            runtime, "in_taobao_and_safe", return_value=True
        ):
            result = runtime._safe_back_to_coin_page(
                device, None, deadline=None
            )
        self.assertFalse(result)
        self.assertEqual(len(device.presses), runtime.MAX_BACKS)

    def test_known_flow_page_backs_once_then_arrives(self):
        device = self._PressDevice()
        with patch.object(
            runtime,
            "ocr_screen",
            side_effect=[self._entry_spans(), self._coin_spans(True)],
        ) as read, patch.object(
            runtime, "in_taobao_and_safe", return_value=True
        ):
            result = runtime._safe_back_to_coin_page(
                device, None, deadline=None
            )
        self.assertTrue(result)
        self.assertEqual(len(device.presses), 1)
        self.assertEqual(read.call_count, 2)

    def test_backs_stop_at_coin_root_even_when_action_obscured(self):
        """过冲回归测试：入口页按一次返回到淘金币页（按钮被遮挡）→ 停，不按第二次。"""
        device = self._PressDevice()
        with patch.object(
            runtime,
            "ocr_screen",
            side_effect=[
                self._entry_spans(),
                self._coin_spans(False),
                self._coin_spans(False),
            ],
        ), patch.object(
            runtime, "in_taobao_and_safe", return_value=True
        ):
            result = runtime._safe_back_to_coin_page(
                device, None, deadline=None
            )
        self.assertFalse(result)
        self.assertEqual(len(device.presses), 1)  # 只按一次，未冲到首页


    def test_stuck_result_page_is_flow_page_and_backs(self):
        """徽标消失的结果页是流程页：回退会按返回走出卡页（不再原地卡死）。"""
        device = self._PressDevice()
        stuck = [
            OcrSpan("搜索", 1.0, (887, 283), (850, 260, 930, 300)),
            OcrSpan("七天退换", 0.9, (710, 998), (650, 980, 780, 1010)),
        ]
        with patch.object(
            runtime,
            "ocr_screen",
            side_effect=[stuck, stuck, self._coin_spans(False), self._coin_spans(False)],
        ), patch.object(
            runtime, "in_taobao_and_safe", return_value=True
        ):
            result = runtime._safe_back_to_coin_page(
                device, None, deadline=None
            )
        self.assertFalse(result)  # 根页面按钮被遮挡 → 原地停
        self.assertEqual(len(device.presses), 2)  # 但已从卡页走出两步

    def test_settle_recognizes_stuck_result_page_as_flow(self):
        device = self._PressDevice()
        stuck = [
            OcrSpan("搜索", 1.0, (887, 283), (850, 260, 930, 300)),
            OcrSpan("七天退换", 0.9, (710, 998), (650, 980, 780, 1010)),
        ]
        with patch.object(
            runtime, "ocr_screen", return_value=stuck
        ), patch.object(
            runtime, "in_taobao_and_safe", return_value=True
        ), patch.object(
            runtime, "_safe_back_to_coin_page", return_value=True
        ) as walk:
            result = runtime._settle_back_to_coin_page(device, None)
        self.assertTrue(result)
        walk.assert_called_once()


class PopupCloseControlTests(unittest.TestCase):
    """弹窗右上角关闭控件（“更多”）：结算时关闭弹窗让回合计数生效。

    真机 2026-08-29 11:13 用户演示：点右上角“更多”关闭任务弹窗，淘金币页
    恢复“赚更多金币”可见。前置条件全部 OCR 验证后才允许点击。
    """

    class _TapDevice(_WorkingDevice):
        def __init__(self):
            self.taps = []

        def click(self, *args):
            self.taps.append(args)

    def _coin_with_popup(self, with_more=True, with_action=False):
        spans = [
            OcrSpan("淘金币", 0.9, (300, 100), (250, 80, 350, 120)),
            OcrSpan("赚金币抵钱", 0.96, (390, 468), (300, 450, 500, 490)),
        ]
        if with_more:
            spans.append(
                OcrSpan("更多", 1.0, (1010, 139), (975, 119, 1045, 159))
            )
        if with_action:
            spans.append(
                OcrSpan("赚更多金币", 0.99, (500, 800), (450, 780, 550, 820))
            )
        return spans

    def test_closes_popup_and_confirms_action_visible(self):
        device = self._TapDevice()
        with patch.object(
            runtime,
            "ocr_screen",
            side_effect=[
                self._coin_with_popup(with_more=True, with_action=False),
                self._coin_with_popup(with_action=True),
            ],
        ) as read, patch.object(
            runtime, "in_taobao_and_safe", return_value=True
        ), patch.object(runtime, "_deadline_sleep"):
            result = runtime._close_task_popup_via_more(
                device, None, (1080, 1920), deadline=None
            )
        self.assertTrue(result)
        self.assertEqual(device.taps, [(1010, 139)])  # 真实 safe_tap 边界校验后点击
        self.assertEqual(read.call_count, 2)

    def test_fails_closed_when_more_control_missing(self):
        device = self._TapDevice()
        with patch.object(
            runtime,
            "ocr_screen",
            side_effect=[self._coin_with_popup(with_more=False)] * 3,
        ) as read, patch.object(
            runtime, "in_taobao_and_safe", return_value=True
        ), patch.object(runtime, "safe_tap") as tap, patch.object(
            runtime, "_deadline_sleep"
        ):
            result = runtime._close_task_popup_via_more(
                device, None, (1080, 1920), deadline=None
            )
        self.assertFalse(result)
        tap.assert_not_called()
        self.assertEqual(
            read.call_count, runtime.ENTRY_VALIDATION_RETRIES + 1
        )

    def test_noop_when_popup_title_absent(self):
        """弹窗标题不可见（非弹窗状态）：不点任何东西。"""
        device = self._TapDevice()
        plain_coin = [OcrSpan("淘金币", 0.9, (300, 100), (250, 80, 350, 120))]
        with patch.object(
            runtime, "ocr_screen", return_value=plain_coin
        ) as read, patch.object(
            runtime, "in_taobao_and_safe", return_value=True
        ), patch.object(runtime, "safe_tap") as tap:
            result = runtime._close_task_popup_via_more(
                device, None, (1080, 1920), deadline=None
            )
        self.assertFalse(result)
        tap.assert_not_called()
        self.assertEqual(read.call_count, 1)

    def test_safe_back_true_when_root_visible_but_close_fails(self):
        """锚点可见 = 已到淘金币根页：弹窗关闭失败（纯遮挡/非弹窗态）
        不再失败关闭——后续 _reopen_task_popup 会处理弹窗状态。"""
        device = self._TapDevice()
        plain_coin = [OcrSpan("淘金币", 0.9, (300, 100), (250, 80, 350, 120))]
        with patch.object(
            runtime, "ocr_screen", return_value=plain_coin
        ), patch.object(
            runtime, "in_taobao_and_safe", return_value=True
        ), patch.object(
            runtime, "_close_task_popup_via_more", return_value=False
        ):
            result = runtime._safe_back_to_coin_page(
                device, None, deadline=None, require_action=False
            )
        self.assertTrue(result)
        self.assertEqual(device.taps, [])

    def test_safe_back_uses_close_control_when_popup_blocks_root(self):
        """集成：回退到根页面但弹窗遮挡按钮 → 关闭弹窗 → 确认成功。"""
        device = self._TapDevice()
        with patch.object(
            runtime,
            "ocr_screen",
            side_effect=[
                self._coin_with_popup(with_more=True, with_action=False),
                self._coin_with_popup(with_more=True, with_action=False),
                self._coin_with_popup(with_action=True),
            ],
        ), patch.object(
            runtime, "in_taobao_and_safe", return_value=True
        ), patch.object(runtime, "_deadline_sleep"):
            result = runtime._safe_back_to_coin_page(
                device, None, deadline=None
            )
        self.assertTrue(result)
        self.assertEqual(device.taps, [(1010, 139)])


class SettleDiagnosticTests(unittest.TestCase):
    """结算/重开失败时发页面指纹（无原文），供失败定位。"""

    def test_emits_fingerprint_on_settle_failure(self):
        device = _WorkingDevice()
        logger = _FakeRuntimeLogger()
        frame = [OcrSpan("淘金币", 0.9, (300, 100), (250, 80, 350, 120))]
        with patch.object(
            runtime, "ocr_screen", return_value=frame
        ), patch.object(
            runtime, "in_taobao_and_safe", return_value=True
        ), patch.object(
            runtime, "_deadline_sleep"
        ):
            runtime._emit_recovery_diagnostic(d=device, reader=None, logger=logger,
                                              reason="settle_back_failed")
        events = [e for e in logger.events if e[0] == "page_diagnostic"]
        self.assertEqual(len(events), 1)
        payload = events[1 - 1][1]
        self.assertEqual(payload["reason"], "settle_back_failed")
        self.assertTrue(payload["diagnostic"]["has_coin_title"])
        self.assertNotIn("淘金币", json.dumps(payload, ensure_ascii=False))

    def test_swallows_diagnostic_errors(self):
        device = _WorkingDevice()
        logger = _FakeRuntimeLogger()
        with patch.object(
            runtime, "ocr_screen", side_effect=RuntimeError("ocr down")
        ):
            runtime._emit_recovery_diagnostic(d=device, reader=None, logger=logger,
                                              reason="settle_back_failed")
        self.assertEqual(logger.events, [])


class ReaderFactorySelectionTests(unittest.TestCase):
    def test_explicit_factory_passes_through(self):
        sentinel = lambda *a, **k: object()  # noqa: E731
        self.assertIs(
            runtime.resolve_reader_factory(sentinel, 12345), sentinel
        )

    def test_sidecar_port_returns_sidecar_reader(self):
        from taojinbi_mav.runtime.ocr_service import SidecarReader

        factory = runtime.resolve_reader_factory(None, 55555)
        reader = factory(["ch_sim", "en"], gpu=True)
        self.assertIsInstance(reader, SidecarReader)
        self.assertEqual(reader.port, 55555)
        reader.close()

    def test_no_port_no_factory_returns_none(self):
        self.assertIsNone(runtime.resolve_reader_factory(None, 0))


class OcrSidecarPortWiringTests(unittest.TestCase):
    def test_run_ocr_entry_passes_sidecar_port_to_run_entry(self):
        outcome = RunOutcome(RunMode.EXECUTE, RunStatus.SUCCESS, "completed")
        with patch.object(runtime, "_run_entry", return_value=outcome) as entry:
            runtime.run_ocr_entry(
                serial="test-device",
                ocr_sidecar_port=12345,
                run_timeout=60,
                task_timeout=40,
                recovery_timeout=10,
                connect=lambda _s: _WorkingDevice(),
                reader_factory=lambda *a, **k: object(),
                logger_factory=_fake_logger_factory,
            )
        self.assertEqual(
            entry.call_args.kwargs.get("ocr_sidecar_port"), 12345
        )


if __name__ == "__main__":
    unittest.main()
