import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from taojinbi_mav.runtime.logging import create_runtime_logger
from taojinbi_mav.runtime.outcome import RunCounts, RunMode, RunStatus


class RuntimeLoggingTests(unittest.TestCase):
    def test_writes_one_utf8_json_object_per_event(self):
        output = []
        with tempfile.TemporaryDirectory() as folder:
            logger = create_runtime_logger(
                folder,
                RunMode.DRY_RUN,
                now=lambda: datetime(2026, 8, 25, tzinfo=timezone.utc),
                run_id_factory=lambda: "abcd1234",
                console=output.append,
            )
            logger.emit(
                "dry_run_row_decided",
                task_key="hashtag",
                phase="scan",
                status=RunStatus.SUCCESS.value,
                reason="supported",
                counts=RunCounts(detected=1, supported=1),
            )
            path = logger.path
            logger.close()

            raw = Path(path).read_text(encoding="utf-8")
            lines = raw.splitlines()
            self.assertEqual(len(lines), 1)
            event = json.loads(lines[0])
            self.assertEqual(event["schema_version"], 1)
            self.assertEqual(event["run_id"], "abcd1234")
            self.assertEqual(event["task_key"], "hashtag")
            self.assertEqual(event["counts"]["supported"], 1)
            self.assertNotIn("斯维诗鱼油", raw)
            self.assertEqual(output, ["dry-run：看看#… supported"])

    def test_accepts_page_diagnostic_with_whitelisted_fields(self):
        output = []
        with tempfile.TemporaryDirectory() as folder:
            logger = create_runtime_logger(
                folder,
                RunMode.EXECUTE,
                now=lambda: datetime(2026, 8, 25, tzinfo=timezone.utc),
                run_id_factory=lambda: "abcd1234",
                console=output.append,
            )
            logger.emit(
                "page_diagnostic",
                reason="anchor_missing",
                diagnostic={
                    "span_count": 12,
                    "has_coin_title": True,
                    "has_popup_title": False,
                },
            )
            path = logger.path
            logger.close()
            event = json.loads(Path(path).read_text(encoding="utf-8"))
        self.assertEqual(event["event"], "page_diagnostic")
        self.assertEqual(event["reason"], "anchor_missing")
        self.assertEqual(event["diagnostic"]["span_count"], 12)
        self.assertTrue(event["diagnostic"]["has_coin_title"])

    def test_rejects_unknown_diagnostic_keys(self):
        # 诊断字段必须来自白名单：OCR 原文/商品名等隐私键一律拒绝
        with tempfile.TemporaryDirectory() as folder:
            logger = create_runtime_logger(
                folder,
                RunMode.EXECUTE,
                run_id_factory=lambda: "abcd1234",
                console=lambda _text: None,
            )
            try:
                with self.assertRaises(ValueError):
                    logger.emit(
                        "page_diagnostic",
                        reason="anchor_missing",
                        diagnostic={"ocr_text": "搜一搜你心仪的宝贝"},
                    )
            finally:
                logger.close()

    def test_rejects_unknown_event_task_key_and_free_text_reason(self):
        with tempfile.TemporaryDirectory() as folder:
            logger = create_runtime_logger(
                folder,
                RunMode.EXECUTE,
                run_id_factory=lambda: "abcd1234",
                console=lambda _text: None,
            )
            for kwargs in (
                {"event": "arbitrary_event"},
                {"event": "task_started", "task_key": "unknown"},
                {"event": "run_finished", "reason": "C:\\private\\path"},
            ):
                with self.subTest(kwargs=kwargs):
                    with self.assertRaises(ValueError):
                        logger.emit(**kwargs)
            logger.close()

    def test_each_emit_flushes_immediately(self):
        class RecordingStream:
            def __init__(self):
                self.flushes = 0

            def write(self, _text):
                return None

            def flush(self):
                self.flushes += 1

            def close(self):
                return None

        stream = RecordingStream()
        from taojinbi_mav.runtime.logging import RuntimeEventLogger

        logger = RuntimeEventLogger(
            stream,
            Path("ignored.jsonl"),
            RunMode.EXECUTE,
            "abcd1234",
            now=lambda: datetime(2026, 8, 25, tzinfo=timezone.utc),
            console=lambda _text: None,
        )
        logger.emit("run_started", reason="started")
        self.assertEqual(stream.flushes, 1)


if __name__ == "__main__":
    unittest.main()
