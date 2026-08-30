"""守候会话整场监控面板：读取 wait-session-*.jsonl 并常驻刷新显示。

- 每秒轮询最新的 wait-session 日志，显示当前状态、任务计数、休息倒计时
  与最近事件；由 wait_for_task.py 在会话启动时以独立控制台窗口拉起。
- session_finished 后展示最终汇总并在 3 秒后自动退出；超过 90 秒没有任何
  事件（含心跳）判定守候主进程已退出，面板自动退出，不残留窗口。
- 纯读取：不写日志、不触碰设备。
"""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
LOGS_DIR = REPO_ROOT / "logs"

POLL_INTERVAL = 1.0
IDLE_TIMEOUT = 90.0        # 90 秒无任何事件（含心跳）→ 主进程已不在
NO_FILE_TIMEOUT = 60.0     # 迟迟无会话日志 → 提示后退出
FINISHED_GRACE = 3.0       # 会话结束后展示汇总的时长
MAX_RECENT_EVENTS = 6


def pick_newest_session_file(logs_dir: Path):
    """返回最新的 wait-session 日志路径；不存在返回 None。"""
    candidates = [
        p
        for p in Path(logs_dir).glob("wait-session-*.jsonl")
        if p.is_file()
    ]
    if not candidates:
        return None
    return max(candidates, key=lambda p: p.stat().st_mtime)


def read_events(path: Path) -> list:
    """读取全部事件；跳过空行与无法解析的行。"""
    events = []
    try:
        for line in Path(path).read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if isinstance(event, dict):
                events.append(event)
    except OSError:
        return []
    return events


def _humanize(event: dict) -> str:
    ts = event.get("ts")
    clock = (
        time.strftime("%H:%M:%S", time.localtime(ts))
        if isinstance(ts, (int, float))
        else "--:--:--"
    )
    name = event.get("event")
    if name == "session_started":
        return f"{clock} 会话启动（task={event.get('task')}）"
    if name == "cycle_started":
        return f"{clock} 第 {event.get('cycle')} 轮检查启动"
    if name == "cycle_finished":
        status = event.get("task_status") or "无任务"
        return (
            f"{clock} 第 {event.get('cycle')} 轮结束 "
            f"exit={event.get('exit_code')} detected={event.get('detected')}"
            f"（{status}）"
        )
    if name == "task_done":
        return (
            f"{clock} ✅ 任务完成（本场 {event.get('tasks_session')}，"
            f"今日 {event.get('tasks_today')}）"
        )
    if name == "rest_started":
        return (
            f"{clock} 开始休息（{event.get('kind')}）"
            f" {float(event.get('seconds', 0) or 0):.0f} 秒"
        )
    if name == "session_finished":
        return f"{clock} 会话结束：{event.get('reason')}"
    return f"{clock} {name}"


def derive_session_state(events: list, now: float) -> dict:
    """从事件流派生面板状态；对缺失字段与坏行宽容。"""
    state = {
        "phase": "waiting_log",
        "finished": False,
        "reason": "",
        "tasks_session": 0,
        "tasks_today": 0,
        "started_ts": None,
        "rest_remaining": None,
        "last_ts": None,
    }
    for event in events:
        if not isinstance(event, dict):
            continue
        name = event.get("event")
        ts = event.get("ts")
        if isinstance(ts, (int, float)):
            state["last_ts"] = ts
        if name == "session_started":
            if state["started_ts"] is None:
                state["started_ts"] = ts
            state["phase"] = "checking"
        elif name == "task_done":
            state["tasks_session"] = int(event.get("tasks_session", 0) or 0)
            state["tasks_today"] = int(event.get("tasks_today", 0) or 0)
            state["phase"] = "checking"
        elif name == "session_finished":
            state["finished"] = True
            state["reason"] = str(event.get("reason", ""))
            state["tasks_session"] = int(
                event.get("tasks_session", state["tasks_session"]) or 0
            )
            state["tasks_today"] = int(
                event.get("tasks_today", state["tasks_today"]) or 0
            )
        elif name == "rest_started":
            seconds = float(event.get("seconds", 0) or 0)
            elapsed = (
                now - ts
                if isinstance(ts, (int, float))
                else seconds
            )
            state["phase"] = "resting"
            state["rest_remaining"] = max(0.0, seconds - elapsed)
        elif name == "heartbeat":
            pass  # 心跳只用于刷新 last_ts，不改变阶段
        elif name == "cycle_started":
            state["phase"] = "checking"
        elif name == "cycle_finished":
            state["phase"] = "checking"
            state["rest_remaining"] = None
    state["last_events"] = [
        _humanize(event)
        for event in events
        if isinstance(event, dict) and event.get("event") != "heartbeat"
    ][-MAX_RECENT_EVENTS:]
    return state


def _render(state: dict) -> str:
    lines = [
        "\033[2J\033[H守候会话监控  (每 1s 刷新, Ctrl+C 退出)",
        "-" * 56,
    ]
    if state["phase"] == "waiting_log":
        lines.append("状态:      等待守候会话日志出现…")
    elif state["finished"]:
        lines.append(f"状态:      已结束（{state['reason']}）")
    elif state["phase"] == "resting":
        remaining = state["rest_remaining"] or 0.0
        lines.append(f"状态:      休息中，剩余 {remaining:.0f} 秒")
    else:
        lines.append("状态:      检查 / 执行中")
    lines.append(
        f"本场任务:  {state['tasks_session']}      "
        f"今日累计: {state['tasks_today']}"
    )
    lines.append("最近事件:")
    for summary in state["last_events"]:
        lines.append(f"  {summary}")
    return "\n".join(lines)


def run_panel(
    logs_dir=LOGS_DIR,
    log_path=None,
    out=print,
    sleep=time.sleep,
    clock=time.time,
    poll_interval=POLL_INTERVAL,
    idle_timeout=IDLE_TIMEOUT,
    no_file_timeout=NO_FILE_TIMEOUT,
    finished_grace=FINISHED_GRACE,
) -> int:
    """面板主循环；返回退出码。纯读取，任何异常不打断守候主进程。

    显式 ``log_path`` 优先（本轮精确事件文件）；未提供时回退
    ``pick_newest_session_file``（多会话并行时可能读到上一轮）。
    """
    started = clock()
    while True:
        now = clock()
        path = log_path or pick_newest_session_file(logs_dir)
        if path is None:
            if now - started >= no_file_timeout:
                out("60 秒内未出现守候会话日志，面板退出"
                    "（请先启动 scripts/wait_for_task.py）")
                return 1
            sleep(poll_interval)
            continue
        events = read_events(path)
        state = derive_session_state(events, now)
        out(_render(state))
        if state["finished"]:
            sleep(finished_grace)
            return 0
        last_ts = state["last_ts"]
        if (
            state["started_ts"] is not None
            and isinstance(last_ts, (int, float))
            and now - last_ts > idle_timeout
        ):
            out("超过 90 秒无会话心跳，面板退出（守候主进程可能已结束）")
            return 0
        if not events and now - started >= no_file_timeout:
            out("会话日志持续为空，面板退出")
            return 1
        sleep(poll_interval)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="守候会话整场监控面板（由 wait_for_task.py 自动拉起）"
    )
    parser.add_argument(
        "--auto-exit",
        action="store_true",
        help="接受该参数以对齐 CLI 面板习惯（本面板始终自动退出）",
    )
    parser.add_argument(
        "--logs-dir",
        default=str(LOGS_DIR),
        help="会话日志目录（默认项目 logs/）",
    )
    parser.add_argument(
        "--log",
        default=None,
        help="显式监听的本轮会话日志文件（优先于 logs-dir 的最新文件猜测）",
    )
    args = parser.parse_args(argv)
    try:
        return run_panel(
            logs_dir=Path(args.logs_dir),
            log_path=Path(args.log) if args.log else None,
        )
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
