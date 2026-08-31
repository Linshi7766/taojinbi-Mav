"""完整一轮 run_full_round.py 的离线测试（mock 子进程与日志）。"""

import json
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "scripts"))

import run_full_round as rfr


def _write_log(log_dir, status, reason="ok", run_reason="x",
              with_finished=True):
    """写一个最新 run 日志；with_finished=False 模拟任务未执行（list_anchor_missing）。"""
    log_dir.mkdir(parents=True, exist_ok=True)
    path = log_dir / "taojinbi-20260831T000000Z-abc.jsonl"
    parts = [json.dumps({"event": "run_started", "reason": "started"})]
    if with_finished:
        parts.append(json.dumps({
            "event": "task_finished", "task_key": "search",
            "status": status, "reason": reason,
        }))
    parts.append(json.dumps({"event": "run_finished", "reason": run_reason}))
    path.write_text("\n".join(parts) + "\n", encoding="utf-8")
    return path


class LatestTaskFinishedTests(unittest.TestCase):
    def test_reads_latest_file_status(self):
        with tempfile.TemporaryDirectory() as tmp:
            _write_log(Path(tmp), "likely_completed", "progress_reset")
            e = rfr.latest_task_finished(Path(tmp))
            self.assertEqual(e["status"], "likely_completed")
            self.assertEqual(e["reason"], "progress_reset")

    def test_none_without_logs(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertIsNone(rfr.latest_task_finished(Path(tmp)))


class RunFullRoundTests(unittest.TestCase):
    def _run(self, statuses, *, max_rounds=8, stall_limit=3, gap=0):
        """statuses: 每轮写入的 task_finished status 列表（按顺序消费）。"""
        with tempfile.TemporaryDirectory() as tmp:
            log_dir = Path(tmp)
            calls = []

            def fake_run_one(python, serial, task):
                calls.append(task)
                item = statuses.pop(0) if statuses else ("stalled", "x")
                if isinstance(item, str):
                    status, run_reason, with_f = item, "x", True
                else:
                    status, run_reason, with_f = (item + (True,))[:3]
                _write_log(log_dir, status, run_reason=run_reason,
                           with_finished=with_f)

            outcomes, balance = rfr.run_full_round(
                "TEST", before=None, python="py", gap=gap,
                max_rounds=max_rounds, stall_limit=stall_limit,
                sleep=lambda s: None, log_dir=log_dir,
                run_one=fake_run_one,
            )
        return outcomes, calls

    def test_completed_breaks_task_after_one_round(self):
        # 三任务各 completed：每任务只跑 1 轮
        outcomes, calls = self._run(
            ["completed", "completed", "completed"]
        )
        self.assertEqual(
            calls, ["search", "hashtag", "featured_goods"]
        )
        for task in rfr.TASKS:
            self.assertEqual(outcomes[task], "completed")

    def test_likely_progress_continues_until_completed(self):
        # search 前 2 轮 likely（推进中）+ 第 3 轮 completed；其余任务各 1 轮
        outcomes, calls = self._run(
            ["likely_completed", "likely_completed", "completed",
             "completed", "completed"]
        )
        self.assertEqual(calls.count("search"), 3)
        self.assertEqual(outcomes["search"], "completed")
        self.assertEqual(calls, ["search", "search", "search",
                                 "hashtag", "featured_goods"])

    def test_stall_limit_stops_each_task_after_three(self):
        # 无推进日志：每任务 3 次停滞即放弃（3 任务共 9 轮）
        outcomes, calls = self._run([])
        self.assertEqual(len(calls), 9)
        self.assertEqual(outcomes["search"], "stalled(ok)")
        self.assertEqual(calls.count("hashtag"), 3)

    def test_missing_task_skipped_after_two_anchor_failures(self):
        # featured_goods 今日未出现：2 次 list_anchor_missing 即跳过，
        # 不等到 stall_limit（3 次）浪费轮次
        statuses = (
            ["completed", "completed"]            # search, hashtag
            + [("stalled", "list_anchor_missing", False)] * 5  # featured_goods
        )
        outcomes, calls = self._run(statuses)
        self.assertEqual(outcomes["search"], "completed")
        self.assertEqual(outcomes["hashtag"], "completed")
        self.assertEqual(outcomes["featured_goods"], "not_available_today")
        self.assertEqual(calls.count("featured_goods"), 2)

    def test_all_three_tasks_attempted(self):
        outcomes, calls = self._run(
            ["completed", "completed", "completed"]
        )
        self.assertEqual(
            calls, ["search", "hashtag", "featured_goods"]
        )
        for task in rfr.TASKS:
            self.assertEqual(outcomes[task], "completed")


if __name__ == "__main__":
    unittest.main()
