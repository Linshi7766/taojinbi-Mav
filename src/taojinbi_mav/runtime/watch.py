"""taojinbi_mav.runtime.watch — 淘金币运行实时进度面板（终端版，零依赖）

实时跟踪 ``logs/`` 目录最新一份 UTF-8 JSONL 事件日志，在终端绘制结构化
状态面板：run 信息、当前阶段、任务、计数、最近事件流。每 1 秒刷新一次，
Ctrl+C 退出。适合在 execute/dry-run 后台运行时并排查看进度。

用法：
    python -m taojinbi_mav.runtime.watch [log_dir] [interval_seconds]

默认 log_dir 为 ``logs``（项目根相对路径），间隔 1 秒。
"""

import json
import os
import sys
import time
from pathlib import Path

# auto-exit 模式的空闲兜底：主进程可能被后台任务机制提前终止、不写
# run_finished；超过该秒数没有读到新日志事件即自动关闭，避免面板残留。
AUTO_EXIT_IDLE_SECONDS = 90.0


def _should_exit(state, last_activity, now, auto_exit, idle_limit):
    """auto-exit 退出策略：run_finished 或空闲超时都触发退出。

    - 非 auto_exit（手动查看）永不自动退出；
    - auto_exit 下已读到 run_finished 立即准备退出；
    - auto_exit 下 idle_limit 秒无新事件（主进程异常终止等）也准备退出。
    """
    if not auto_exit:
        return False
    if state.finished:
        return True
    return (now - last_activity) >= idle_limit


EVENT_LABELS = {
    "run_started": "运行开始",
    "startup_checked": "启动检查",
    "dry_run_row_decided": "dry-run 行判定",
    "task_started": "任务开始",
    "task_finished": "任务结束",
    "recovery_started": "恢复开始",
    "recovery_finished": "恢复结束",
    "run_finished": "运行结束",
}

COUNT_LABELS = (
    ("detected", "识别"),
    ("supported", "支持"),
    ("skipped", "跳过"),
    ("attempted", "执行"),
    ("completed", "已确认完成"),
    ("likely_completed", "很可能完成"),
    ("unfinished", "未完成"),
)

_ANSI_RESET = "\033[0m"
_ANSI_BOLD = "\033[1m"
_ANSI_DIM = "\033[2m"
_ANSI_GREEN = "\033[32m"
_ANSI_YELLOW = "\033[33m"
_ANSI_RED = "\033[31m"
_ANSI_CYAN = "\033[36m"


def _clear_screen():
    """Windows 与类 Unix 通用的清屏方式（尽力而为，失败则忽略）。"""
    if os.name == "nt":
        os.system("cls")
    else:
        sys.stdout.write("\033[2J\033[H")


def _latest_log(log_dir):
    """返回 log_dir 中最新的 .jsonl 文件路径，无则 None。"""
    files = [p for p in Path(log_dir).glob("*.jsonl") if p.is_file()]
    if not files:
        return None
    return max(files, key=lambda p: p.stat().st_mtime)


def _status_color(status):
    mapping = {
        "success": _ANSI_GREEN,
        "partial": _ANSI_YELLOW,
        "startup_failed": _ANSI_RED,
        "safety_stopped": _ANSI_RED,
        "timed_out": _ANSI_YELLOW,
        "cancelled": _ANSI_YELLOW,
    }
    return mapping.get(status, _ANSI_RESET)


class State:
    """从事件流累积的面板状态。"""

    def __init__(self):
        self.run_id = None
        self.mode = None
        self.started_at = None
        self.finished = None
        self.final_status = None
        self.final_reason = None
        self.counts = {}
        self.events = []
        self.current_task = None
        self.last_event = None

    def apply(self, event):
        self.events.append(event)
        self.last_event = event
        kind = event.get("event")
        if kind == "run_started":
            self.run_id = event.get("run_id")
            self.mode = event.get("mode")
            self.started_at = event.get("timestamp")
            self.finished = None
            self.final_status = None
            self.final_reason = None
            self.counts = {}
            self.current_task = None
            self.events = [event]
        elif kind == "task_started":
            self.current_task = event.get("task_key")
        elif kind == "task_finished":
            self.current_task = None
        elif kind == "run_finished":
            self.finished = True
            self.final_status = event.get("status")
            self.final_reason = event.get("reason")
            self.counts = event.get("counts") or {}
        counts = event.get("counts")
        if counts:
            self.counts = counts

    def elapsed(self):
        if not self.started_at:
            return 0.0
        try:
            started = time.mktime(
                time.strptime(self.started_at.split(".")[0], "%Y-%m-%dT%H:%M:%S")
            )
        except (ValueError, AttributeError):
            return 0.0
        return max(0.0, time.time() - started + time.timezone)


def _draw(state, interval):
    _clear_screen()
    out = sys.stdout
    out.write(_ANSI_BOLD + "淘金币运行进度 " + _ANSI_RESET)
    out.write(_ANSI_DIM + "(每 %gs 刷新，Ctrl+C 退出)\n" % interval + _ANSI_RESET)
    out.write("-" * 64 + "\n")

    if state.run_id is None:
        out.write(_ANSI_DIM + "暂无运行日志（等待 logs/*.jsonl 出现）…\n" + _ANSI_RESET)
        return

    mode_label = "execute（执行）" if state.mode == "execute" else (
        "dry_run（只读）" if state.mode == "dry_run" else str(state.mode)
    )
    out.write("%s run_id=%s  mode=%s\n" % (state.mode, state.run_id, mode_label))
    out.write("已运行 %s\n" % _fmt_duration(state.elapsed()))

    if state.finished:
        color = _status_color(state.final_status)
        out.write(
            color + "运行结束 status=%s reason=%s" % (state.final_status, state.final_reason)
            + _ANSI_RESET + "\n"
        )
    elif state.current_task is not None:
        out.write(_ANSI_CYAN + "当前任务：%s" % state.current_task + _ANSI_RESET + "\n")
    else:
        out.write(_ANSI_DIM + "等待任务…\n" + _ANSI_RESET)

    out.write("\n" + _ANSI_BOLD + "计数" + _ANSI_RESET + "\n")
    if state.counts:
        for key, label in COUNT_LABELS:
            out.write("  %s: %s\n" % (label, state.counts.get(key, 0)))
    else:
        out.write("  " + _ANSI_DIM + "（暂无）" + _ANSI_RESET + "\n")

    out.write("\n" + _ANSI_BOLD + "最近事件" + _ANSI_RESET + "\n")
    for event in state.events[-8:]:
        kind = event.get("event")
        label = EVENT_LABELS.get(kind, kind)
        ts = (event.get("timestamp") or "")[11:19]
        extra = ""
        if kind == "dry_run_row_decided":
            task = event.get("task_key") or "未知任务"
            extra = " %s %s" % (task, event.get("reason", ""))
        elif kind == "task_finished":
            extra = " %s %s" % (event.get("task_key") or "", event.get("reason", ""))
        out.write("  [%s] %s%s\n" % (ts, label, extra))
    out.flush()


def _fmt_duration(seconds):
    seconds = int(seconds)
    minutes, secs = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return "%d:%02d:%02d" % (hours, minutes, secs)
    return "%d:%02d" % (minutes, secs)


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    auto_exit = "--auto-exit" in argv
    argv = [item for item in argv if item != "--auto-exit"]
    log_dir = argv[0] if argv else "logs"
    try:
        interval = float(argv[1]) if len(argv) > 1 else 1.0
    except ValueError:
        print("间隔参数必须是数字，例如 1 或 0.5", file=sys.stderr)
        return 2
    if interval <= 0:
        print("间隔参数必须为正数", file=sys.stderr)
        return 2

    state = State()
    current_log = None
    current_pos = 0
    clock = time.monotonic
    last_activity = clock()
    try:
        while True:
            latest = _latest_log(log_dir)
            if latest is not None and latest != current_log:
                current_log = latest
                current_pos = 0
                state = State()
                last_activity = clock()
            if current_log is not None:
                try:
                    with current_log.open("r", encoding="utf-8") as handle:
                        handle.seek(current_pos)
                        for line in handle:
                            line = line.strip()
                            if not line:
                                continue
                            try:
                                event = json.loads(line)
                            except ValueError:
                                continue
                            state.apply(event)
                            last_activity = clock()
                        current_pos = handle.tell()
                except OSError:
                    pass
            _draw(state, interval)
            if _should_exit(
                state,
                last_activity,
                clock(),
                auto_exit,
                AUTO_EXIT_IDLE_SECONDS,
            ):
                if state.finished:
                    sys.stdout.write("\n运行结束，面板将在 3 秒后自动关闭…\n")
                else:
                    sys.stdout.write("\n面板已空闲超时（主进程可能已结束），将自动关闭…\n")
                sys.stdout.flush()
                time.sleep(3)
                break
            time.sleep(interval)
    except KeyboardInterrupt:
        _clear_screen()
        print("已退出进度面板")
        return 0
    finally:
        if auto_exit:
            _clear_screen()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
