import json
import re
import uuid
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

from taojinbi_mav.runtime.outcome import RunCounts, RunMode


EVENTS = frozenset({
    "run_started",
    "startup_checked",
    "dry_run_row_decided",
    "task_started",
    "task_finished",
    "recovery_started",
    "recovery_finished",
    "run_finished",
})
TASK_LABELS = {
    "search": "搜一搜…",
    "hashtag": "看看#…",
    "featured_goods": "发现精选好物",
}
_IDENTIFIER_RE = re.compile(r"^[a-z0-9_]*$")


class RuntimeEventLogger:
    def __init__(self, stream, path, mode, run_id, now, console):
        self._stream = stream
        self.path = Path(path)
        self.mode = mode
        self.run_id = run_id
        self._now = now
        self._console = console

    def emit(
        self,
        event,
        level="info",
        task_key=None,
        phase="",
        status="",
        reason="",
        counts=None,
    ):
        if event not in EVENTS:
            raise ValueError("unknown runtime event")
        if task_key is not None and task_key not in TASK_LABELS:
            raise ValueError("unknown task key")
        for value in (level, phase, status, reason):
            if not isinstance(value, str) or _IDENTIFIER_RE.fullmatch(value) is None:
                raise ValueError("event fields must be stable identifiers")
        safe_counts = counts if counts is not None else RunCounts()
        if not isinstance(safe_counts, RunCounts):
            raise TypeError("counts must be RunCounts")
        payload = {
            "schema_version": 1,
            "timestamp": self._now().astimezone(timezone.utc).isoformat(),
            "run_id": self.run_id,
            "level": level,
            "event": event,
            "mode": self.mode.value,
            "task_key": task_key,
            "phase": phase,
            "status": status,
            "reason": reason,
            "counts": asdict(safe_counts),
        }
        self._stream.write(json.dumps(payload, ensure_ascii=False) + "\n")
        self._stream.flush()
        if event == "dry_run_row_decided":
            label = TASK_LABELS.get(task_key, "未注册任务")
            self._console(f"dry-run：{label} {reason}")

    def close(self):
        self._stream.close()


def create_runtime_logger(
    log_dir,
    mode,
    now=lambda: datetime.now(timezone.utc),
    run_id_factory=lambda: uuid.uuid4().hex[:8],
    console=print,
):
    if not isinstance(mode, RunMode):
        raise TypeError("mode must be RunMode")
    folder = Path(log_dir)
    folder.mkdir(parents=True, exist_ok=True)
    run_id = run_id_factory()
    stamp = now().astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    path = folder / f"taojinbi-{stamp}-{run_id}.jsonl"
    stream = path.open("x", encoding="utf-8", newline="\n")
    return RuntimeEventLogger(stream, path, mode, run_id, now, console)
