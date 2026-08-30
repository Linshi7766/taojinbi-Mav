"""整场监控面板 wait_panel.py 与守候会话事件日志的离线测试。"""

import importlib.util
import json
import random
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parent.parent


def _load(name, rel):
    spec = importlib.util.spec_from_file_location(name, REPO / rel)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


wait_for_task = _load("wait_for_task", "scripts/wait_for_task.py")
wait_panel = _load("wait_panel", "scripts/wait_panel.py")


class SessionEventLoggerTests(unittest.TestCase):
    def test_appends_jsonl_events_with_ts(self):
        with tempfile.TemporaryDirectory() as tmp:
            logger = wait_for_task.SessionEventLogger(Path(tmp))
            logger.emit("session_started", tasks_session=0)
            logger.emit("task_done", tasks_session=1)
            lines = logger.path.read_text(encoding="utf-8").splitlines()
            events = [json.loads(line) for line in lines]
            self.assertEqual(
                [e["event"] for e in events],
                ["session_started", "task_done"],
            )
            self.assertEqual(events[1]["tasks_session"], 1)
            for e in events:
                self.assertIsInstance(e["ts"], float)

    def test_never_records_device_serial(self):
        # README 隐私承诺：日志不记录设备序列号。序列号必须被白名单拒绝。
        with tempfile.TemporaryDirectory() as tmp:
            logger = wait_for_task.SessionEventLogger(Path(tmp))
            logger.emit("session_started", serial="FFKGKX")
            lines = logger.path.read_text(encoding="utf-8").splitlines()
            event = json.loads(lines[0])
            self.assertNotIn("serial", event)

    def test_unknown_private_fields_are_dropped(self):
        # OCR 原文、商品名、坐标等隐私字段不得进入会话日志。
        with tempfile.TemporaryDirectory() as tmp:
            logger = wait_for_task.SessionEventLogger(Path(tmp))
            logger.emit(
                "task_finished", status="likely_completed",
                ocr_text="搜一搜你心仪的宝贝", box=(1, 2, 3, 4),
            )
            lines = logger.path.read_text(encoding="utf-8").splitlines()
            event = json.loads(lines[0])
            self.assertEqual(event.get("status"), "likely_completed")
            self.assertNotIn("ocr_text", event)
            self.assertNotIn("box", event)

    def test_emit_swallows_os_errors(self):
        logger = wait_for_task.SessionEventLogger(
            Path("Z:/definitely/not/writable")
        )
        logger.emit("session_started")  # 不应抛异常

    def test_thread_safe_append(self):
        with tempfile.TemporaryDirectory() as tmp:
            logger = wait_for_task.SessionEventLogger(Path(tmp))

            def emit_many():
                for _ in range(50):
                    logger.emit("heartbeat")

            threads = [threading.Thread(target=emit_many) for _ in range(4)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
            lines = logger.path.read_text(encoding="utf-8").splitlines()
            self.assertEqual(len(lines), 200)


class DeriveSessionStateTests(unittest.TestCase):
    @staticmethod
    def _events(*events):
        return events

    def test_no_events_means_waiting_for_log(self):
        state = wait_panel.derive_session_state([], now=1000.0)
        self.assertEqual(state["phase"], "waiting_log")
        self.assertFalse(state["finished"])
        self.assertEqual(state["tasks_session"], 0)

    def test_cycle_started_means_checking(self):
        events = [
            {"event": "session_started", "ts": 900.0},
            {"event": "cycle_started", "ts": 1000.0, "cycle": 1},
        ]
        state = wait_panel.derive_session_state(events, now=1000.5)
        self.assertEqual(state["phase"], "checking")
        self.assertEqual(state["started_ts"], 900.0)

    def test_rest_started_shows_remaining_seconds(self):
        events = [
            {"event": "session_started", "ts": 900.0},
            {"event": "rest_started", "ts": 1000.0, "kind": "done",
             "seconds": 300.0},
        ]
        state = wait_panel.derive_session_state(events, now=1100.0)
        self.assertEqual(state["phase"], "resting")
        self.assertAlmostEqual(state["rest_remaining"], 200.0)

    def test_expired_rest_reports_zero_remaining(self):
        events = [
            {"event": "rest_started", "ts": 1000.0, "kind": "gap",
             "seconds": 60.0},
        ]
        state = wait_panel.derive_session_state(events, now=2000.0)
        self.assertEqual(state["phase"], "resting")
        self.assertEqual(state["rest_remaining"], 0.0)

    def test_task_done_counts_and_heartbeat_ignored_for_phase(self):
        events = [
            {"event": "session_started", "ts": 900.0},
            {"event": "task_done", "ts": 950.0, "tasks_session": 1,
             "tasks_today": 2},
            {"event": "heartbeat", "ts": 980.0},
            {"event": "heartbeat", "ts": 1010.0},
        ]
        state = wait_panel.derive_session_state(events, now=1020.0)
        self.assertEqual(state["tasks_session"], 1)
        self.assertEqual(state["tasks_today"], 2)
        self.assertEqual(state["phase"], "checking")  # 心跳不改变阶段

    def test_session_finished_wins(self):
        events = [
            {"event": "session_started", "ts": 900.0},
            {"event": "task_done", "ts": 950.0, "tasks_session": 3,
             "tasks_today": 4},
            {"event": "session_finished", "ts": 990.0, "reason": "stop_no_tasks",
             "tasks_session": 3, "tasks_today": 4},
            {"event": "heartbeat", "ts": 995.0},
        ]
        state = wait_panel.derive_session_state(events, now=1000.0)
        self.assertTrue(state["finished"])
        self.assertEqual(state["reason"], "stop_no_tasks")
        self.assertEqual(state["tasks_session"], 3)

    def test_malformed_and_missing_fields_are_tolerated(self):
        events = [
            {"event": "session_started", "ts": 900.0},
            {"unexpected": "shape"},
            "not-a-dict",
        ]
        state = wait_panel.derive_session_state(events, now=1000.0)
        self.assertEqual(state["phase"], "checking")


class PickNewestSessionFileTests(unittest.TestCase):
    def test_returns_newest_by_mtime(self):
        with tempfile.TemporaryDirectory() as tmp:
            logs = Path(tmp)
            old = logs / "wait-session-a.jsonl"
            new = logs / "wait-session-b.jsonl"
            old.write_text("{}", encoding="utf-8")
            time.sleep(0.02)
            new.write_text("{}", encoding="utf-8")
            self.assertEqual(
                wait_panel.pick_newest_session_file(logs), new
            )

    def test_main_accepts_explicit_log_path(self):
        with mock.patch.object(wait_panel, "run_panel") as run:
            wait_panel.main(["--log", "X:/tmp/wait-session-x.jsonl"])
        run.assert_called_once()
        self.assertEqual(
            run.call_args.kwargs["log_path"],
            Path("X:/tmp/wait-session-x.jsonl"),
        )

    def test_run_panel_prefers_explicit_path_over_newest(self):
        with tempfile.TemporaryDirectory() as tmp:
            explicit = Path(tmp) / "wait-session-20260830T000000Z.jsonl"
            explicit.write_text("", encoding="utf-8")
            state = {
                "phase": "waiting_log", "finished": False,
                "tasks_session": 0, "tasks_today": 0, "last_events": [],
                "last_ts": None, "started_ts": None,
            }
            with mock.patch.object(
                wait_panel, "pick_newest_session_file"
            ) as pick, mock.patch.object(
                wait_panel, "read_events", return_value=[]
            ), mock.patch.object(
                wait_panel, "derive_session_state", return_value=state
            ), mock.patch.object(
                wait_panel, "_render", return_value=""
            ):
                def stop(_s):
                    raise KeyboardInterrupt()

                with self.assertRaises(KeyboardInterrupt):
                    wait_panel.run_panel(
                        log_path=explicit,
                        out=lambda _s: None,
                        sleep=stop,
                        clock=lambda: 0.0,
                    )
            pick.assert_not_called()

    def test_returns_none_when_absent(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertIsNone(
                wait_panel.pick_newest_session_file(Path(tmp))
            )


class SupervisorSessionEventsTests(unittest.TestCase):
    def setUp(self):
        self._orig_logs = wait_for_task.LOGS_DIR

    def tearDown(self):
        wait_for_task.LOGS_DIR = self._orig_logs

    def _args(self, **overrides):
        import argparse

        base = dict(
            serial="TEST", task="any", max_tasks=0, daily_cap=0,
            max_wait_cycles=1, min_gap_s=10, max_gap_s=20,
            done_rest_min_s=300, done_rest_max_s=900,
            grind_rest_min_s=60, grind_rest_max_s=180,
            session_deadline_min=60, no_panel=True,
        )
        base.update(overrides)
        return argparse.Namespace(**base)

    def test_supervisor_emits_full_event_lifecycle(self):
        with tempfile.TemporaryDirectory() as tmp:
            logs = Path(tmp)
            wait_for_task.LOGS_DIR = logs
            logger = wait_for_task.SessionEventLogger(logs)
            script = [(1, "completed"), (0, None)]

            def runner():
                detected, status = script.pop(0)
                events = [{"event": "run_started", "counts": {"detected": 0}}]
                if detected:
                    events.append(
                        {"event": "task_finished", "status": status}
                    )
                events.append(
                    {
                        "event": "run_finished",
                        "counts": {"detected": detected,
                                   "attempted": detected},
                    }
                )
                (logs / "taojinbi-child.jsonl").write_text(
                    "\n".join(json.dumps(e) for e in events),
                    encoding="utf-8",
                )
                return 0

            code = wait_for_task.run_supervisor(
                self._args(), random.Random(7),
                runner=runner, sleeper=lambda s: None,
                session_logger=logger,
            sidecar_spawner=lambda: (None, None),
            )
            self.assertEqual(code, 0)
            events = [
                json.loads(line)
                for line in logger.path.read_text(encoding="utf-8").splitlines()
            ]
            names = [e["event"] for e in events]
            self.assertEqual(names[0], "session_started")
            self.assertIn("cycle_started", names)
            self.assertIn("task_done", names)
            self.assertIn("rest_started", names)
            self.assertEqual(names[-1], "session_finished")
            finished = events[-1]
            self.assertEqual(finished["reason"], "stop_no_tasks")
            self.assertEqual(finished["tasks_session"], 1)

    def test_error_session_still_emits_session_finished(self):
        with tempfile.TemporaryDirectory() as tmp:
            logs = Path(tmp)
            wait_for_task.LOGS_DIR = logs
            logger = wait_for_task.SessionEventLogger(logs)
            (logs / "taojinbi-child.jsonl").write_text(
                json.dumps(
                    {
                        "event": "run_finished",
                        "counts": {"detected": 0, "attempted": 0},
                    }
                ),
                encoding="utf-8",
            )
            code = wait_for_task.run_supervisor(
                self._args(), random.Random(7),
                runner=lambda: 3, sleeper=lambda s: None,
                session_logger=logger,
            sidecar_spawner=lambda: (None, None),
            )
            self.assertEqual(code, 3)
            events = [
                json.loads(line)
                for line in logger.path.read_text(encoding="utf-8").splitlines()
            ]
            self.assertEqual(events[-1]["event"], "session_finished")
            self.assertEqual(events[-1]["reason"], "stop_error")


class PanelSpawnGuardTests(unittest.TestCase):
    def test_panel_spawned_once_for_real_sessions(self):
        with tempfile.TemporaryDirectory() as tmp:
            wait_for_task.LOGS_DIR = Path(tmp)
            with mock.patch.object(
                wait_for_task, "_spawn_wait_panel"
            ) as spawn, mock.patch.object(
                wait_for_task.SessionEventLogger, "emit"
            ), mock.patch.object(
                wait_for_task,
                "read_run_outcome",
                return_value=None,
            ):
                code = wait_for_task.run_supervisor(
                    wait_for_task._parse_args(
                        ["--serial", "DEV", "--max-wait-cycles", "1",
                         "--session-deadline-min", "1"]
                    ),
                    random.Random(7),
                    runner=lambda: 0,
                    sleeper=lambda s: None,
                sidecar_spawner=lambda: (None, None),
                )
            self.assertEqual(code, 3)  # 退出码 0 但日志不可确认 → 保守停
            spawn.assert_called_once()

    def test_no_panel_flag_skips_spawn(self):
        with tempfile.TemporaryDirectory() as tmp:
            wait_for_task.LOGS_DIR = Path(tmp)
            args = wait_for_task._parse_args(
                ["--serial", "DEV", "--no-panel",
                 "--max-wait-cycles", "1", "--session-deadline-min", "1"]
            )
            self.assertTrue(args.no_panel)
            with mock.patch.object(
                wait_for_task, "_spawn_wait_panel"
            ) as spawn, mock.patch.object(
                wait_for_task.SessionEventLogger, "emit"
            ), mock.patch.object(
                wait_for_task,
                "read_run_outcome",
                return_value=None,
            ):
                code = wait_for_task.run_supervisor(
                    args,
                    wait_for_task.random.Random(7),
                    runner=lambda: 0,
                    sleeper=lambda s: None,
                sidecar_spawner=lambda: (None, None),
                )
            self.assertEqual(code, 3)
            spawn.assert_not_called()


if __name__ == "__main__":
    unittest.main()
