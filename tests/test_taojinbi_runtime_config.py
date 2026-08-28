"""Task 5：dry-run 与运行时限 CLI 参数解析测试。"""

import re
import unittest

from taojinbi_mav.runtime.config import (
    DEFAULT_DEVICE_SERIAL,
    DEFAULT_DRY_RUN_TIMEOUT,
    DEFAULT_OCR_GPU,
    DEFAULT_RECOVERY_TIMEOUT,
    DEFAULT_RUN_TIMEOUT,
    DEFAULT_TASK_TIMEOUT,
    build_ocr_arg_parser,
    resolve_device_serial,
)


class DeviceSerialPrivacyTests(unittest.TestCase):
    """公开仓库不得保存私人设备默认值（设计 9.4 / 4.1）。"""

    def test_default_serial_is_empty(self):
        self.assertEqual(DEFAULT_DEVICE_SERIAL, "")
        self.assertEqual(resolve_device_serial(), "")

    def test_default_serial_has_no_ip_like_address(self):
        # 用 IP/端口模式断言，避免在公开文件里出现任何真实地址字面量
        self.assertIsNone(re.search(r"\d+\.\d+\.\d+\.\d+", DEFAULT_DEVICE_SERIAL))
        self.assertIsNone(re.search(r":\d{4,5}$", DEFAULT_DEVICE_SERIAL))

    def test_explicit_and_env_values_still_win(self):
        import os
        self.assertEqual(resolve_device_serial("explicit-device"), "explicit-device")
        os.environ["TAOJINBI_DEVICE_SERIAL"] = "env-device"
        try:
            self.assertEqual(resolve_device_serial(), "env-device")
        finally:
            del os.environ["TAOJINBI_DEVICE_SERIAL"]


class DryRunConfigTests(unittest.TestCase):
    def test_parses_dry_run_and_runtime_budgets(self):
        args = build_ocr_arg_parser().parse_args([
            "--dry-run", "--dry-run-timeout", "15", "--task-timeout", "60",
            "--run-timeout", "90", "--recovery-timeout", "3",
        ])
        self.assertTrue(args.dry_run)
        self.assertEqual(
            (args.dry_run_timeout, args.task_timeout, args.run_timeout,
             args.recovery_timeout),
            (15, 60, 90, 3),
        )

    def test_rejects_zero_timeout(self):
        with self.assertRaises(SystemExit) as caught:
            build_ocr_arg_parser().parse_args(["--run-timeout", "0"])
        self.assertEqual(caught.exception.code, 2)

    def test_default_timeouts_match_documented_budgets(self):
        args = build_ocr_arg_parser().parse_args([])
        self.assertFalse(args.dry_run)
        self.assertEqual(args.dry_run_timeout, DEFAULT_DRY_RUN_TIMEOUT)
        self.assertEqual(args.task_timeout, DEFAULT_TASK_TIMEOUT)
        self.assertEqual(args.run_timeout, DEFAULT_RUN_TIMEOUT)
        self.assertEqual(args.recovery_timeout, DEFAULT_RECOVERY_TIMEOUT)

    def test_parses_task_key(self):
        args = build_ocr_arg_parser().parse_args(["--task", "hashtag"])
        self.assertEqual(args.task, "hashtag")

    def test_task_key_defaults_to_none(self):
        args = build_ocr_arg_parser().parse_args([])
        self.assertIsNone(args.task)

    def test_rejects_unknown_task_key(self):
        with self.assertRaises(SystemExit) as caught:
            build_ocr_arg_parser().parse_args(["--task", "unknown_task"])
        self.assertEqual(caught.exception.code, 2)

    def test_watch_defaults_to_enabled(self):
        args = build_ocr_arg_parser().parse_args([])
        self.assertTrue(args.watch)

    def test_no_watch_disables_panel(self):
        args = build_ocr_arg_parser().parse_args(["--no-watch"])
        self.assertFalse(args.watch)


if __name__ == "__main__":
    unittest.main()
