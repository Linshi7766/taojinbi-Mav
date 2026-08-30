"""守候脚本 wait_for_task.py 的离线测试：只测纯调度逻辑，不连设备、不起子进程。"""

import argparse
import importlib.util
import json
import os
import random
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch
from pathlib import Path

SCRIPT_PATH = (
    Path(__file__).resolve().parent.parent / "scripts" / "wait_for_task.py"
)
_spec = importlib.util.spec_from_file_location("wait_for_task", SCRIPT_PATH)
wait_for_task = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(wait_for_task)


class DecideAfterCycleTests(unittest.TestCase):
    def test_continue_when_zero_detected_and_success(self):
        self.assertEqual(
            wait_for_task.decide_after_cycle(0, 0), "continue"
        )

    def test_stop_found_when_target_detected(self):
        self.assertEqual(
            wait_for_task.decide_after_cycle(0, 1), "stop_found"
        )

    def test_stop_attempted_when_executed_but_unconfirmed(self):
        self.assertEqual(
            wait_for_task.decide_after_cycle(1, 1), "stop_attempted"
        )

    def test_stop_error_on_failure_exit_codes(self):
        for code in (2, 3, 4, 5, 99):
            with self.subTest(code=code):
                self.assertEqual(
                    wait_for_task.decide_after_cycle(code, 0), "stop_error"
                )

    def test_stop_interrupt_on_ctrl_c_exit(self):
        self.assertEqual(
            wait_for_task.decide_after_cycle(130, 0), "stop_interrupt"
        )


class NextDelayTests(unittest.TestCase):
    def test_delay_stays_within_configured_bounds(self):
        rng = random.Random(20260828)
        for _ in range(200):
            delay = wait_for_task.next_delay(rng, 180, 480)
            self.assertGreaterEqual(delay, 180)
            self.assertLessEqual(delay, 480)

    def test_delay_is_not_constant(self):
        rng = random.Random(1)
        values = {round(wait_for_task.next_delay(rng, 60, 600), 3) for _ in range(50)}
        self.assertGreater(len(values), 1)


class RestTierTests(unittest.TestCase):
    def _args(self):
        return argparse.Namespace(
            done_rest_min_s=300, done_rest_max_s=900,
            grind_rest_min_s=60, grind_rest_max_s=180,
        )

    def test_completed_results_take_long_rest(self):
        for status in ("completed", "likely_completed"):
            with self.subTest(status=status):
                self.assertEqual(
                    wait_for_task.rest_tier_for(status),
                    wait_for_task.REST_LONG,
                )

    def test_unfinished_or_unknown_results_take_short_rest(self):
        for status in ("unfinished", None, "weird"):
            with self.subTest(status=status):
                self.assertEqual(
                    wait_for_task.rest_tier_for(status),
                    wait_for_task.REST_SHORT,
                )

    def test_rest_seconds_stay_in_tier_bounds(self):
        rng = random.Random(7)
        args = self._args()
        for _ in range(50):
            long_rest = wait_for_task.next_rest_seconds(
                rng, args, wait_for_task.REST_LONG
            )
            self.assertGreaterEqual(long_rest, 300)
            self.assertLessEqual(long_rest, 900)
            short_rest = wait_for_task.next_rest_seconds(
                rng, args, wait_for_task.REST_SHORT
            )
            self.assertGreaterEqual(short_rest, 60)
            self.assertLessEqual(short_rest, 180)


class DailyCapStateTests(unittest.TestCase):
    def test_save_then_load_roundtrip(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "nested" / "wait_state.json"
            self.assertTrue(
                wait_for_task.save_daily_done(path, "2026-08-28", 3)
            )
            self.assertEqual(
                wait_for_task.load_daily_done(path, "2026-08-28"), 3
            )

    def test_load_resets_when_date_differs(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "wait_state.json"
            wait_for_task.save_daily_done(path, "2026-08-28", 5)
            self.assertEqual(wait_for_task.load_daily_done(path, "2026-08-29"), 0)

    def test_load_returns_zero_on_missing_or_malformed_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            missing = Path(tmp) / "none.json"
            self.assertEqual(wait_for_task.load_daily_done(missing, "2026-08-28"), 0)
            bad = Path(tmp) / "bad.json"
            bad.write_text("not-json", encoding="utf-8")
            self.assertEqual(wait_for_task.load_daily_done(bad, "2026-08-28"), 0)


class BuildChildArgsTests(unittest.TestCase):
    def test_includes_task_for_registered_task_key(self):
        args = argparse.Namespace(serial="DEV1", task="featured_goods")
        child = wait_for_task.build_child_args(args)
        self.assertIn("--task", child)
        self.assertEqual(child[child.index("--task") + 1], "featured_goods")
        self.assertIn("--gpu", child)
        self.assertIn("--max-tasks", child)
        self.assertEqual(child[child.index("--max-tasks") + 1], "1")

    def test_omits_task_for_any_mode(self):
        args = argparse.Namespace(serial="DEV1", task="any")
        child = wait_for_task.build_child_args(args)
        self.assertNotIn("--task", child)
        self.assertIn("--gpu", child)
        self.assertIn("--max-tasks", child)

    def test_default_task_is_any(self):
        parsed = wait_for_task._parse_args(["--serial", "DEV1"])
        self.assertEqual(parsed.task, "any")


class ReadRunOutcomeTests(unittest.TestCase):
    def _write_log(self, path: Path, detected, task_status=None):
        events = [{"event": "run_started", "counts": {"detected": 0}}]
        if task_status is not None:
            events.append(
                {"event": "task_finished", "status": task_status}
            )
        events.append(
            {
                "event": "run_finished",
                "reason": "completed",
                "counts": {"detected": detected, "attempted": detected},
            }
        )
        path.write_text(
            "\n".join(json.dumps(e, ensure_ascii=False) for e in events),
            encoding="utf-8",
        )

    def test_parses_counts_and_task_status(self):
        with tempfile.TemporaryDirectory() as tmp:
            logs = Path(tmp)
            time.sleep(0.02)
            cutoff = time.time()
            time.sleep(0.02)
            self._write_log(logs / "taojinbi-new.jsonl", 1, "likely_completed")
            self.assertEqual(
                wait_for_task.read_run_outcome(logs, cutoff),
                (1, 1, "likely_completed"),
            )

    def test_task_status_is_none_when_no_task_finished(self):
        with tempfile.TemporaryDirectory() as tmp:
            logs = Path(tmp)
            time.sleep(0.02)
            cutoff = time.time()
            time.sleep(0.02)
            self._write_log(logs / "taojinbi-new.jsonl", 0, None)
            self.assertEqual(
                wait_for_task.read_run_outcome(logs, cutoff), (0, 0, None)
            )

    def test_ignores_logs_older_than_cutoff(self):
        with tempfile.TemporaryDirectory() as tmp:
            logs = Path(tmp)
            self._write_log(logs / "taojinbi-old.jsonl", 3, "completed")
            mtime = (logs / "taojinbi-old.jsonl").stat().st_mtime
            # cutoff 推到 mtime+3 秒，超出 2 秒容差 → 视为旧日志
            self.assertIsNone(
                wait_for_task.read_run_outcome(logs, mtime + 3.0)
            )

    def test_returns_none_without_run_finished(self):
        with tempfile.TemporaryDirectory() as tmp:
            logs = Path(tmp)
            p = logs / "taojinbi-partial.jsonl"
            p.write_text(
                json.dumps({"event": "run_started", "counts": {"detected": 0}}),
                encoding="utf-8",
            )
            self.assertIsNone(
                wait_for_task.read_run_outcome(logs, time.time() - 60)
            )

    def test_returns_none_on_malformed_log(self):
        with tempfile.TemporaryDirectory() as tmp:
            logs = Path(tmp)
            p = logs / "taojinbi-bad.jsonl"
            p.write_text("not-json\n", encoding="utf-8")
            self.assertIsNone(
                wait_for_task.read_run_outcome(logs, time.time() - 60)
            )

    def test_mtime_quantization_skew_is_tolerated(self):
        """Windows mtime 粒度可能略早于 time.time()：2 秒容差内仍可读取。"""
        with tempfile.TemporaryDirectory() as tmp:
            logs = Path(tmp)
            self._write_log(logs / "taojinbi-new.jsonl", 1, "completed")
            mtime = (logs / "taojinbi-new.jsonl").stat().st_mtime
            outcome = wait_for_task.read_run_outcome(logs, mtime + 1.0)
            self.assertEqual(outcome, (1, 1, "completed"))


class SidecarLifecycleTests(unittest.TestCase):
    """OCR sidecar 生命周期：解析就绪行、启动失败回退、参数透传。"""

    def test_parse_ready_line_valid_and_invalid(self):
        assert_fn = wait_for_task._parse_ready_line
        self.assertEqual(
            assert_fn("OCR_SIDECAR_READY 127.0.0.1 54321"),
            ("127.0.0.1", 54321),
        )
        self.assertIsNone(assert_fn("OCR_SIDECAR_READY 127.0.0.1 abc"))
        self.assertIsNone(assert_fn("某条普通日志"))
        self.assertIsNone(assert_fn("OCR_SIDECAR_READY 127.0.0.1"))

    def test_build_child_args_includes_sidecar_port(self):
        import argparse

        args = argparse.Namespace(serial="DEV1", task="any")
        child = wait_for_task.build_child_args(args, sidecar_port=54321)
        self.assertIn("--ocr-sidecar-port", child)
        self.assertEqual(child[child.index("--ocr-sidecar-port") + 1], "54321")

    def test_spawn_failure_falls_back_to_self_load(self):
        with patch.object(
            wait_for_task.subprocess, "Popen", side_effect=OSError("no proc")
        ):
            proc, addr = wait_for_task._spawn_ocr_sidecar(timeout=1)
        self.assertIsNone(proc)
        self.assertIsNone(addr)

    def test_spawn_success_parses_ready_port(self):
        import io

        fake_proc = type(
            "P",
            (),
            {
                "stdout": io.BytesIO(
                    b"OCR_SIDECAR_READY 127.0.0.1 54321\n"
                ),
                "terminate": lambda self: None,
            },
        )()
        with patch.object(
            wait_for_task.subprocess, "Popen", return_value=fake_proc
        ), patch.object(
            wait_for_task, "wait_until_ready", return_value=True
        ):
            proc, addr = wait_for_task._spawn_ocr_sidecar(timeout=2)
        self.assertIs(proc, fake_proc)
        self.assertEqual(addr, ("127.0.0.1", 54321))

    def test_supervisor_spawns_and_terminates_sidecar(self):
        with tempfile.TemporaryDirectory() as tmp:
            logs = Path(tmp)
            wait_for_task.LOGS_DIR = logs
            wait_for_task.STATE_PATH = Path(tmp) / "state.json"
            terminated = {"count": 0}

            class _FakeProc:
                def terminate(self):
                    terminated["count"] += 1

                def kill(self):
                    terminated["count"] += 1

                def wait(self, timeout=None):
                    return 0

                def poll(self):
                    return None  # 存活，逼出完整清理序列

            def runner():
                # 每轮写入空结果日志 → 触发保守停（exit 3）
                events = [
                    {"event": "run_started", "counts": {"detected": 0}},
                    {"event": "run_finished", "reason": "completed",
                     "counts": {"detected": 0, "attempted": 0}},
                ]
                (logs / "taojinbi-x.jsonl").write_text(
                    "\n".join(json.dumps(e) for e in events), encoding="utf-8"
                )
                return 0

            args = argparse.Namespace(
                serial="TEST", task="any", max_tasks=0, daily_cap=0,
                max_wait_cycles=1, min_gap_s=1, max_gap_s=2,
                done_rest_min_s=1, done_rest_max_s=2,
                grind_rest_min_s=1, grind_rest_max_s=2,
                session_deadline_min=60, no_panel=True,
                no_ocr_sidecar=False,
            )
            spawn_calls = []

            def fake_spawn(timeout=150.0):
                spawn_calls.append(1)
                proc = _FakeProc()
                proc.terminate = lambda: terminated.__setitem__(
                    "count", terminated["count"] + 1
                )
                return proc, ("127.0.0.1", 54321)

            with patch.object(
                wait_for_task, "_spawn_ocr_sidecar",
                side_effect=fake_spawn,
            ), patch.object(
                wait_for_task, "wait_until_ready", return_value=True
            ):
                code = wait_for_task.run_supervisor(
                    args, random.Random(3),
                    runner=runner, sleeper=lambda s: None,
                sidecar_spawner=wait_for_task._spawn_ocr_sidecar,
                )
            self.assertEqual(code, 0)  # 无候选 → 空阶段自然收场
            self.assertEqual(len(spawn_calls), 1)   # 会话启动即拉起
            self.assertGreaterEqual(terminated["count"], 1)  # 结束时终止


class RunSupervisorTests(unittest.TestCase):
    def setUp(self):
        self._orig_logs_dir = wait_for_task.LOGS_DIR

    def tearDown(self):
        wait_for_task.LOGS_DIR = self._orig_logs_dir

    def _args(self, **overrides):
        base = dict(
            serial="TEST", task="any", max_tasks=0, daily_cap=0,
            max_wait_cycles=2, min_gap_s=10, max_gap_s=20,
            done_rest_min_s=300, done_rest_max_s=900,
            grind_rest_min_s=60, grind_rest_max_s=180,
            session_deadline_min=60, no_panel=True,
            no_ocr_sidecar=True,
        )
        base.update(overrides)
        return argparse.Namespace(**base)

    def _write_child_log(self, logs: Path, detected, task_status=None):
        events = [{"event": "run_started", "counts": {"detected": 0}}]
        if detected or task_status:
            events.append({"event": "task_finished", "status": task_status})
        events.append(
            {
                "event": "run_finished",
                "reason": "completed",
                "counts": {"detected": detected, "attempted": detected},
            }
        )
        logs.mkdir(exist_ok=True)
        (logs / "taojinbi-sup-test.jsonl").write_text(
            "\n".join(json.dumps(e, ensure_ascii=False) for e in events),
            encoding="utf-8",
        )

    def test_empty_phase_ends_session_without_trailing_sleep(self):
        with tempfile.TemporaryDirectory() as tmp:
            logs = Path(tmp)
            wait_for_task.LOGS_DIR = logs
            calls = {"runner": 0, "sleeps": []}

            def runner():
                calls["runner"] += 1
                self._write_child_log(logs, detected=0)
                return 0

            code = wait_for_task.run_supervisor(
                self._args(max_wait_cycles=2), random.Random(7),
                runner=runner, sleeper=lambda s: calls["sleeps"].append(s),
            sidecar_spawner=lambda: (None, None),
            )
            self.assertEqual(code, 0)
            self.assertEqual(calls["runner"], 2)
            self.assertEqual(len(calls["sleeps"]), 1)  # 只在两轮之间睡一次

    def test_unlimited_mode_keeps_running_across_found_tasks(self):
        """默认 max_tasks=0：命中→休息→继续，直到某阶段无任务才收场。"""
        with tempfile.TemporaryDirectory() as tmp:
            logs = Path(tmp)
            wait_for_task.LOGS_DIR = logs
            calls = {"runner": 0, "sleeps": []}
            # 依次：命中(completed) → 命中(unfinished) → 空（max_wait_cycles=1
            # 下一个空轮即收场，验证"只要有任务就一直跑、没任务就停"）
            script = [
                (1, "completed"), (1, "unfinished"), (0, None),
            ]

            def runner():
                calls["runner"] += 1
                detected, status = script[calls["runner"] - 1]
                self._write_child_log(logs, detected, status)
                return 0

            code = wait_for_task.run_supervisor(
                self._args(max_wait_cycles=1), random.Random(7),
                runner=runner, sleeper=lambda s: calls["sleeps"].append(s),
            sidecar_spawner=lambda: (None, None),
            )
            self.assertEqual(code, 0)
            self.assertEqual(calls["runner"], 3)
            # 命中 completed → 长歇；命中 unfinished → 短歇；末轮后不再睡
            self.assertTrue(300 <= calls["sleeps"][0] <= 900)
            self.assertTrue(60 <= calls["sleeps"][1] <= 180)
            self.assertEqual(len(calls["sleeps"]), 2)

    def test_max_tasks_budget_stops_session_after_n_tasks(self):
        with tempfile.TemporaryDirectory() as tmp:
            logs = Path(tmp)
            wait_for_task.LOGS_DIR = logs
            calls = {"runner": 0, "sleeps": []}

            def runner():
                calls["runner"] += 1
                self._write_child_log(logs, 1, "completed")
                return 0

            code = wait_for_task.run_supervisor(
                self._args(max_tasks=2, max_wait_cycles=1),
                random.Random(7),
                runner=runner, sleeper=lambda s: calls["sleeps"].append(s),
            sidecar_spawner=lambda: (None, None),
            )
            self.assertEqual(code, 0)
            self.assertEqual(calls["runner"], 2)
            self.assertEqual(len(calls["sleeps"]), 1)  # 第一次命中后长歇，第二次达到预算即停

    def test_stops_conservatively_when_child_log_unreadable(self):
        with tempfile.TemporaryDirectory() as tmp:
            wait_for_task.LOGS_DIR = Path(tmp)  # 空目录：无该轮日志
            code = wait_for_task.run_supervisor(
                self._args(), random.Random(7),
                runner=lambda: 0, sleeper=lambda s: None,
            sidecar_spawner=lambda: (None, None),
            )
            self.assertEqual(code, 3)  # 退出码 0 但无法确认该轮结果

    def test_stops_on_child_error_exit_code(self):
        with tempfile.TemporaryDirectory() as tmp:
            logs = Path(tmp)
            wait_for_task.LOGS_DIR = logs
            calls = {"runner": 0}

            def runner():
                calls["runner"] += 1
                self._write_child_log(logs, 0)
                return 3  # 设备/入口检查失败

            code = wait_for_task.run_supervisor(
                self._args(), random.Random(7),
                runner=runner, sleeper=lambda s: None,
            sidecar_spawner=lambda: (None, None),
            )
            self.assertEqual(code, 3)
            self.assertEqual(calls["runner"], 1)  # 异常即停，不重试

    def test_daily_cap_blocks_session_before_first_cycle(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp) / "state.json"
            wait_for_task.save_daily_done(state, time.strftime("%Y-%m-%d"), 2)
            wait_for_task.LOGS_DIR = Path(tmp)
            calls = {"runner": 0}

            def runner():
                calls["runner"] += 1
                return 0

            code = wait_for_task.run_supervisor(
                self._args(daily_cap=2), random.Random(7),
                runner=runner, sleeper=lambda s: None,
                state_path=state,
            sidecar_spawner=lambda: (None, None),
            )
            self.assertEqual(code, 0)
            self.assertEqual(calls["runner"], 0)  # 预算已满，一轮都不跑

    def test_daily_cap_counts_and_persists_across_found_tasks(self):
        with tempfile.TemporaryDirectory() as tmp:
            logs = Path(tmp)
            state = Path(tmp) / "state.json"
            wait_for_task.LOGS_DIR = logs
            calls = {"runner": 0}

            def runner():
                calls["runner"] += 1
                self._write_child_log(logs, 1, "completed")
                return 0

            code = wait_for_task.run_supervisor(
                self._args(daily_cap=2, max_wait_cycles=1),
                random.Random(7),
                runner=runner, sleeper=lambda s: None,
                state_path=state,
            sidecar_spawner=lambda: (None, None),
            )
            self.assertEqual(code, 0)
            self.assertEqual(calls["runner"], 2)
            self.assertEqual(
                wait_for_task.load_daily_done(state, time.strftime("%Y-%m-%d")),
                2,
            )

    def test_session_deadline_backstop_stops_before_running(self):
        with tempfile.TemporaryDirectory() as tmp:
            wait_for_task.LOGS_DIR = Path(tmp)
            calls = {"runner": 0, "n": 0}

            def runner():
                calls["runner"] += 1
                return 0

            def clock():
                calls["n"] += 1
                # 首次调用取 session_started=0，其后所有读取都远超 60 分钟
                return 0.0 if calls["n"] == 1 else 10_000_000.0

            code = wait_for_task.run_supervisor(
                self._args(session_deadline_min=60), random.Random(7),
                clock=clock, runner=runner, sleeper=lambda s: None,
            sidecar_spawner=lambda: (None, None),
            )
            self.assertEqual(code, 0)
            self.assertEqual(calls["runner"], 0)  # 已超时，一轮都不跑


class SidecarSpawnIsolationTests(unittest.TestCase):
    """守候不得在测试里真实拉起 sidecar 子进程。

    真实子进程会留下孤儿进程（Windows CI 上会污染 job 收尾），因此
    run_supervisor 必须允许注入 sidecar spawner，测试一律使用注入。
    """

    def test_supervisor_uses_injected_sidecar_spawner(self):
        calls = []

        def fake_spawn():
            calls.append(1)
            return None, ("127.0.0.1", 54321)

        with tempfile.TemporaryDirectory() as tmp:
            wait_for_task.LOGS_DIR = Path(tmp)
            args = argparse.Namespace(
                serial="TEST", task="any", max_tasks=0, daily_cap=0,
                max_wait_cycles=1, min_gap_s=10, max_gap_s=20,
                done_rest_min_s=300, done_rest_max_s=900,
                grind_rest_min_s=60, grind_rest_max_s=180,
                session_deadline_min=60, no_panel=True,
            )
            wait_for_task.run_supervisor(
                args, random.Random(7),
                runner=lambda: 0, sleeper=lambda s: None,
                session_logger=wait_for_task.SessionEventLogger(Path(tmp)),
                sidecar_spawner=fake_spawn,
            )
        self.assertEqual(calls, [1])

    def test_supervisor_does_not_spawn_when_disabled(self):
        def fail_spawn():  # pragma: no cover - 不应被调用
            raise AssertionError("sidecar must not spawn when disabled")

        with tempfile.TemporaryDirectory() as tmp:
            wait_for_task.LOGS_DIR = Path(tmp)
            args = argparse.Namespace(
                serial="TEST", task="any", max_tasks=0, daily_cap=0,
                max_wait_cycles=1, min_gap_s=10, max_gap_s=20,
                done_rest_min_s=300, done_rest_max_s=900,
                grind_rest_min_s=60, grind_rest_max_s=180,
                session_deadline_min=60, no_panel=True,
                no_ocr_sidecar=True,
            )
            wait_for_task.run_supervisor(
                args, random.Random(7),
                runner=lambda: 0, sleeper=lambda s: None,
                session_logger=wait_for_task.SessionEventLogger(Path(tmp)),
                sidecar_spawner=fail_spawn,
            )


class StopFileTests(unittest.TestCase):
    """stop 文件机制：存在 STOP 标记文件时守候优雅收场，不跑任何轮次。"""

    def _args(self, **overrides):
        import argparse

        base = dict(
            serial="TEST", task="any", max_tasks=0, daily_cap=0,
            max_wait_cycles=2, min_gap_s=10, max_gap_s=20,
            done_rest_min_s=300, done_rest_max_s=900,
            grind_rest_min_s=60, grind_rest_max_s=180,
            session_deadline_min=60, no_panel=True,
            no_ocr_sidecar=True,
        )
        base.update(overrides)
        return argparse.Namespace(**base)

    def _run(self, tmp: Path, runner):
        logger = wait_for_task.SessionEventLogger(tmp)
        code = wait_for_task.run_supervisor(
            self._args(), random.Random(7),
            runner=runner, sleeper=lambda s: None,
            session_logger=logger,
            sidecar_spawner=lambda: (None, None),
        )
        return code, logger

    def test_stop_file_halts_without_running_any_cycle(self):
        with tempfile.TemporaryDirectory() as tmp:
            logs = Path(tmp)
            wait_for_task.LOGS_DIR = logs
            (logs / "STOP").write_text("", encoding="utf-8")
            calls = {"runner": 0}

            def runner():
                calls["runner"] += 1
                return 0

            code, logger = self._run(logs, runner)
            self.assertEqual(code, 0)
            self.assertEqual(calls["runner"], 0)
            events = [
                json.loads(line)
                for line in logger.path.read_text(encoding="utf-8").splitlines()
            ]
            self.assertEqual(events[-1]["event"], "session_finished")
            self.assertEqual(events[-1]["reason"], "stop_file")
            self.assertFalse((logs / "STOP").exists())

    def test_no_stop_file_means_normal_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            logs = Path(tmp)
            wait_for_task.LOGS_DIR = logs
            calls = {"runner": 0}

            def runner():
                calls["runner"] += 1
                events = [
                    {"event": "run_started", "counts": {"detected": 0}},
                    {"event": "run_finished", "reason": "completed",
                     "counts": {"detected": 0, "attempted": 0}},
                ]
                (logs / "taojinbi-child.jsonl").write_text(
                    "\n".join(json.dumps(e) for e in events), encoding="utf-8"
                )
                return 0

            self._run(logs, runner)
            self.assertGreater(calls["runner"], 0)


class SubprocessCleanupTests(unittest.TestCase):
    """守候收场必须完整清理子进程：terminate → wait（异常路径也要）。"""

    def _args(self, **overrides):
        import argparse

        base = dict(
            serial="TEST", task="any", max_tasks=0, daily_cap=0,
            max_wait_cycles=1, min_gap_s=10, max_gap_s=20,
            done_rest_min_s=300, done_rest_max_s=900,
            grind_rest_min_s=60, grind_rest_max_s=180,
            session_deadline_min=60, no_panel=True,
            no_ocr_sidecar=False,
        )
        base.update(overrides)
        return argparse.Namespace(**base)

    def _recording_proc(self, calls):
        class _Rec:
            def terminate(self):
                calls.append("terminate")

            def kill(self):
                calls.append("kill")

            def wait(self, timeout=None):
                calls.append(("wait", timeout))

            def poll(self):
                return None  # 始终"存活"，逼出 kill 分支

        return _Rec()

    def test_stop_file_path_terminates_and_waits_sidecar(self):
        with tempfile.TemporaryDirectory() as tmp:
            logs = Path(tmp)
            wait_for_task.LOGS_DIR = logs
            (logs / "STOP").write_text("", encoding="utf-8")
            calls = []
            proc = self._recording_proc(calls)
            wait_for_task.run_supervisor(
                self._args(), random.Random(7),
                runner=lambda: 0, sleeper=lambda s: None,
                session_logger=wait_for_task.SessionEventLogger(logs),
                sidecar_spawner=lambda: (proc, ("127.0.0.1", 54321)),
            )
        self.assertIn("terminate", calls)
        self.assertTrue(any(c[0] == "wait" for c in calls))

    def test_exception_path_still_cleans_sidecar(self):
        # runner 抛异常：finally 必须清理 sidecar（否则残留进程）
        with tempfile.TemporaryDirectory() as tmp:
            logs = Path(tmp)
            wait_for_task.LOGS_DIR = logs
            calls = []
            proc = self._recording_proc(calls)

            def boom():
                raise RuntimeError("boom")

            with self.assertRaises(RuntimeError):
                wait_for_task.run_supervisor(
                    self._args(), random.Random(7),
                    runner=boom, sleeper=lambda s: None,
                    session_logger=wait_for_task.SessionEventLogger(logs),
                    sidecar_spawner=lambda: (proc, ("127.0.0.1", 54321)),
                )
        self.assertIn("terminate", calls)
        self.assertTrue(any(c[0] == "wait" for c in calls))


class SourceCheckoutBootstrapTests(unittest.TestCase):
    def test_help_works_without_editable_install_or_pythonpath(self):
        python = getattr(sys, "_base_executable", sys.executable)
        # Windows 控制台默认 GBK/cp1252：-I 会忽略 PYTHONIOENCODING 等
        # PYTHON* 环境变量，因此用命令行选项 -X utf8 强制 UTF-8 输出，
        # 并对无法解码的字节容错（argparse 的中文帮助否则会崩溃）。
        env = dict(os.environ)
        result = subprocess.run(
            [python, "-I", "-X", "utf8", str(SCRIPT_PATH), "--help"],
            cwd=str(SCRIPT_PATH.parent.parent),
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=15,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("--serial", result.stdout)


if __name__ == "__main__":
    unittest.main()
