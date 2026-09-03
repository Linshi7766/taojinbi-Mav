"""Codex 审计 P1-7：并发与临时文件隔离。

- 截图路径按进程唯一（两进程并发不会互相覆盖截图）；
- 按设备序列号单实例锁（同一台手机同时只跑一个实例）。
"""
import importlib.util
import os
import tempfile
import unittest
from pathlib import Path

# Load the CLI entry from scripts/ (not a package) as `runtime`.
_spec = importlib.util.spec_from_file_location(
    "run_taojinbi",
    Path(__file__).resolve().parent.parent / "scripts" / "run_taojinbi.py",
)
runtime = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(runtime)


class RuntimeShotIsolationTests(unittest.TestCase):
    """截图临时文件：路径随进程唯一，且落在忽略前缀内。"""

    def test_shot_path_is_process_unique(self):
        # 不同进程的截图路径必须不同（否则 A 进程会 OCR 到 B 进程的截图）
        self.assertNotEqual(
            runtime.runtime_shot_path(pid=1111),
            runtime.runtime_shot_path(pid=2222),
        )

    def test_shot_path_keeps_ignored_prefix(self):
        # 保持 _ocr_ 前缀，否则会污染 git 工作区
        self.assertIn("_ocr_", runtime.runtime_shot_path(pid=3333))

    def test_shot_path_stable_within_process(self):
        # 同进程内必须复用同一路径（不能每帧新建文件累积垃圾）
        self.assertEqual(
            runtime.runtime_shot_path(pid=4444),
            runtime.runtime_shot_path(pid=4444),
        )


class DeviceLockTests(unittest.TestCase):
    """按设备序列号的单实例锁：同序列号互斥、异序列号并存、陈旧锁可回收。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.lock_dir = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)

    def _acquire(self, serial="A", pid=1001, alive=lambda pid: True):
        return runtime.acquire_device_lock(
            serial, lock_dir=self.lock_dir, pid=pid, pid_alive=alive,
        )

    def test_second_instance_rejected(self):
        first = self._acquire(serial="A", pid=1001)
        self.assertIsNotNone(first)
        # 第二个实例（不同 pid）必须失败关闭，绝不并发操作同一台手机
        self.assertIsNone(self._acquire(serial="A", pid=1002))
        first.release()

    def test_different_serial_coexists(self):
        # 多台设备互不影响
        first = self._acquire(serial="A", pid=1001)
        second = self._acquire(serial="B", pid=1002)
        self.assertIsNotNone(first)
        self.assertIsNotNone(second)
        first.release()
        second.release()

    def test_release_allows_reacquire(self):
        first = self._acquire(serial="A", pid=1001)
        first.release()
        second = self._acquire(serial="A", pid=1002)
        self.assertIsNotNone(second)
        second.release()

    def test_stale_lock_reclaimed_when_process_dead(self):
        # 上次崩溃残留锁：持有者已死 → 允许接管（否则永久卡死）
        stale = self.lock_dir / runtime.device_lock_name("A")
        stale.write_text("9999", encoding="utf-8")
        lock = self._acquire(serial="A", pid=1001, alive=lambda pid: False)
        self.assertIsNotNone(lock)
        lock.release()

    def test_live_lock_never_stolen(self):
        stale = self.lock_dir / runtime.device_lock_name("A")
        stale.write_text("9999", encoding="utf-8")
        # 持有者存活 → 绝不抢占
        self.assertIsNone(
            self._acquire(serial="A", pid=1001, alive=lambda pid: True)
        )

    def test_lock_records_owner_pid(self):
        lock = self._acquire(serial="A", pid=1234)
        self.assertEqual(
            (self.lock_dir / runtime.device_lock_name("A")).read_text(
                encoding="utf-8"
            ).strip(),
            "1234",
        )
        lock.release()

    def test_lock_path_is_serial_scoped(self):
        self.assertNotEqual(
            runtime.device_lock_name("A"), runtime.device_lock_name("B")
        )


if __name__ == "__main__":
    unittest.main()
