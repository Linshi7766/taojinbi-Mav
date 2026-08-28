"""taojinbi_mav.runtime.watch 面板 State 解析回归测试。"""

import unittest

from taojinbi_mav.runtime.watch import State, _should_exit


def _event(kind, **kw):
    base = {
        "schema_version": 1,
        "timestamp": "2026-08-27T05:00:00.000000+00:00",
        "run_id": "r1",
        "level": "info",
        "event": kind,
    }
    base.update(kw)
    return base


class WatchStateTests(unittest.TestCase):
    def test_run_started_resets_previous_run(self):
        state = State()
        state.apply(_event("run_started", mode="execute", run_id="r1"))
        state.apply(_event("task_started", task_key="search"))
        state.apply(_event(
            "run_finished", status="success", reason="completed",
            counts={"detected": 1, "supported": 1},
        ))
        self.assertEqual(state.run_id, "r1")
        self.assertTrue(state.finished)
        state.apply(_event("run_started", mode="dry_run", run_id="r2"))
        self.assertEqual(state.run_id, "r2")
        self.assertEqual(state.mode, "dry_run")
        self.assertIsNone(state.final_status)
        self.assertIsNone(state.final_reason)
        self.assertEqual(state.counts, {})
        self.assertEqual(len(state.events), 1)

    def test_task_started_and_finished_track_current_task(self):
        state = State()
        state.apply(_event("run_started"))
        self.assertIsNone(state.current_task)
        state.apply(_event("task_started", task_key="search"))
        self.assertEqual(state.current_task, "search")
        state.apply(_event("task_finished", task_key="search", reason="progress_reset"))
        self.assertIsNone(state.current_task)

    def test_run_finished_captures_status_reason_and_counts(self):
        state = State()
        state.apply(_event("run_started"))
        state.apply(_event(
            "dry_run_row_decided", task_key="hashtag", phase="scan",
            status="skipped", reason="unsupported_task",
            counts={"detected": 1, "supported": 0, "skipped": 1},
        ))
        state.apply(_event(
            "run_finished", status="success", reason="completed",
            counts={"detected": 4, "supported": 1, "skipped": 3,
                    "attempted": 0, "completed": 0,
                    "likely_completed": 0, "unfinished": 0},
        ))
        self.assertTrue(state.finished)
        self.assertEqual(state.final_status, "success")
        self.assertEqual(state.final_reason, "completed")
        self.assertEqual(state.counts.get("supported"), 1)
        self.assertEqual(state.counts.get("skipped"), 3)
        self.assertEqual(state.last_event["event"], "run_finished")

    def test_counts_update_from_intermediate_events(self):
        state = State()
        state.apply(_event("run_started"))
        state.apply(_event(
            "task_started", task_key="search",
            counts={"detected": 1, "supported": 1, "attempted": 1},
        ))
        self.assertEqual(state.counts.get("attempted"), 1)


class AutoExitPolicyTests(unittest.TestCase):
    """auto-exit 退出策略：run_finished 或空闲超时都触发退出。

    真机经验：主进程可能被后台任务机制提前终止、不写 run_finished，
    面板若无兜底会永久残留；空闲超时是兜底，不依赖 run_finished。
    """

    def test_no_auto_exit_never_exits(self):
        self.assertFalse(_should_exit(
            State(), last_activity=100.0, now=1000.0,
            auto_exit=False, idle_limit=90.0,
        ))

    def test_finished_triggers_exit(self):
        state = State()
        state.finished = True
        self.assertTrue(_should_exit(
            state, last_activity=100.0, now=101.0,
            auto_exit=True, idle_limit=90.0,
        ))

    def test_idle_timeout_triggers_exit(self):
        self.assertTrue(_should_exit(
            State(), last_activity=100.0, now=200.0,
            auto_exit=True, idle_limit=90.0,
        ))

    def test_active_does_not_exit_before_idle_limit(self):
        self.assertFalse(_should_exit(
            State(), last_activity=100.0, now=150.0,
            auto_exit=True, idle_limit=90.0,
        ))


if __name__ == "__main__":
    unittest.main()
