"""守候脚本：只要任务池还有任务就一直循环执行（每轮唯一 CLI，单任务上限）。

设计边界（与唯一 CLI 运行合同一致）：
- 本脚本不包含任何设备操作代码；每一轮都完整调用 scripts/run_taojinbi.py
  （它自行完成开弹窗、OCR 扫描、命中即执行单任务、收尾与安全退出）。
- 守候循环只做三件事：调起 CLI、解析该轮 JSONL 结果、按档位随机休息。
- 外层任务循环默认无任务个数上限（--max-tasks 0）：命中即执行一个任务，
  按结果分档休息后继续守候；一个完整守候阶段（--max-wait-cycles 轮）颗粒
  无收 = 任务池已空/未刷新，整场收手（--max-tasks N 可限制本场个数）。
- 三档节奏（模拟真人）：同一任务未完成轮次间 1–3 分钟；做完一个任务
  5–15 分钟；空窗检查间隔 3–8 分钟（--min-gap-s/--max-gap-s）。
- 安全兜底：整场时限默认 240 分钟（--session-deadline-min 0 关闭；7-31
  风控事件为约 6 小时前台挂机触发，不做无界常驻）；可选 --daily-cap N
  跨会话日上限（状态文件 logs/wait_state.json）；异常退出码（1/2/3/4/5/130）
  或日志无法确认结果时整场硬停，绝不带病续跑。
- 结算与到账永远以用户人工核对余额为准；运行时不因 likely_completed
  放宽后续判定。

用法（项目根目录，.venv 解释器）：
    & .\\.venv\\Scripts\\python.exe .\\scripts\\wait_for_task.py --serial <设备序列号>
    & .\\.venv\\Scripts\\python.exe .\\scripts\\wait_for_task.py --serial <设备序列号> --task search

Ctrl+C：子进程与守候循环同进程组接收中断；子进程按其两阶段退出处理，
守候循环不再启动下一轮并立即结束。
"""

from __future__ import annotations

import argparse
import json
import os
import random
import subprocess
import sys
import threading
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
_SRC = REPO_ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from taojinbi_mav.runtime.ocr_service import wait_until_ready

CLI_PATH = REPO_ROOT / "scripts" / "run_taojinbi.py"
LOGS_DIR = REPO_ROOT / "logs"
STATE_PATH = LOGS_DIR / "wait_state.json"
STOP_FILE_NAME = "STOP"


def _parse_ready_line(line: str):
    """解析 sidecar 就绪行 "OCR_SIDECAR_READY <host> <port>"；不符返回 None。"""
    parts = line.strip().split()
    if len(parts) == 3 and parts[0] == "OCR_SIDECAR_READY":
        try:
            return parts[1], int(parts[2])
        except ValueError:
            return None
    return None


def _spawn_ocr_sidecar(timeout: float = 150.0):
    """启动 OCR sidecar 子进程并等待就绪；返回 (proc, (host, port))。

    失败（启动异常/超时）返回 (None, None)，守候回退到每轮自行加载模型。
    """
    import subprocess as sp

    try:
        proc = sp.Popen(
            [sys.executable, "-m", "taojinbi_mav.runtime.ocr_service", "--gpu"],
            stdout=sp.PIPE,
            stderr=sp.DEVNULL,
        )
    except OSError as error:
        print(f"OCR sidecar 启动失败（{type(error).__name__}），回退自加载")
        return None, None
    result = {}

    def _reader():
        try:
            for raw in proc.stdout:
                text = raw.decode("utf-8", errors="replace").strip()
                parsed = _parse_ready_line(text)
                if parsed:
                    result["addr"] = parsed
                    return
        except Exception:
            pass

    reader_thread = threading.Thread(target=_reader, daemon=True)
    reader_thread.start()
    reader_thread.join(timeout)
    addr = result.get("addr")
    if addr and wait_until_ready(addr, timeout=30.0):
        print(f"OCR sidecar 就绪（{addr[0]}:{addr[1]}），本轮守候复用常驻模型")
        return proc, addr
    print("OCR sidecar 未能在时限内就绪，回退每轮自加载")
    try:
        proc.terminate()
    except Exception:
        pass
    return None, None

TASK_CHOICES = ("search", "hashtag", "featured_goods", "any")
DEFAULT_MIN_GAP_S = 180        # 空窗检查间隔下限（3 分钟）
DEFAULT_MAX_GAP_S = 480        # 空窗检查间隔上限（8 分钟）
DEFAULT_DONE_REST_MIN_S = 300  # 做完一个任务后的休息下限（5 分钟）
DEFAULT_DONE_REST_MAX_S = 900  # 做完一个任务后的休息上限（15 分钟）
DEFAULT_GRIND_REST_MIN_S = 60  # 同一任务未完成轮次间休息下限（1 分钟）
DEFAULT_GRIND_REST_MAX_S = 180 # 同一任务未完成轮次间休息上限（3 分钟）
DEFAULT_MAX_WAIT_CYCLES = 8    # 一个守候阶段最多检查轮数
DEFAULT_SESSION_DEADLINE_MIN = 240  # 整场硬上限（分钟）；0 = 不限时
EXIT_INTERRUPT = 130
EXIT_ATTEMPTED_UNCONFIRMED = 1

REST_LONG = "long"    # 做完一个任务（completed / likely_completed）
REST_SHORT = "short"  # 同一任务继续刷（unfinished / 未知）

DONE_STATUSES = ("completed", "likely_completed")


def decide_after_cycle(exit_code: int, detected: int) -> str:
    """根据子进程退出码与该轮 detected 计数决定本轮处置。"""
    if exit_code == EXIT_INTERRUPT:
        return "stop_interrupt"
    if exit_code == 0:
        return "stop_found" if detected > 0 else "continue"
    if exit_code == EXIT_ATTEMPTED_UNCONFIRMED:
        return "stop_attempted"
    return "stop_error"


def next_delay(rng: random.Random, min_gap_s: int, max_gap_s: int) -> float:
    """空窗检查间隔：在配置区间内均匀随机，模拟人工节奏。"""
    return rng.uniform(min_gap_s, max_gap_s)


def rest_tier_for(task_status):
    """按该轮任务结果选休息档位：做完一个任务长歇，未完成短歇继续刷。"""
    return REST_LONG if task_status in DONE_STATUSES else REST_SHORT


def next_rest_seconds(rng: random.Random, args, tier: str) -> float:
    if tier == REST_LONG:
        return rng.uniform(args.done_rest_min_s, args.done_rest_max_s)
    return rng.uniform(args.grind_rest_min_s, args.grind_rest_max_s)


def load_daily_done(state_path: Path, today: str) -> int:
    """读取当日已执行任务数；日期不同自动归零；任何异常按 0 处理。"""
    try:
        data = json.loads(state_path.read_text(encoding="utf-8"))
        if data.get("date") == today:
            return int(data.get("done", 0))
    except (OSError, ValueError, AttributeError, TypeError):
        pass
    return 0


def save_daily_done(state_path: Path, today: str, done: int) -> bool:
    """保存当日计数；写失败不阻断守候，仅返回 False 供提示。"""
    try:
        state_path.parent.mkdir(parents=True, exist_ok=True)
        state_path.write_text(
            json.dumps({"date": today, "done": done}, ensure_ascii=False),
            encoding="utf-8",
        )
        return True
    except OSError:
        print("警告：日上限状态文件写入失败，跨会话计数可能不准")
        return False


def read_run_outcome(logs_dir: Path, since_ts: float):
    """读取 since_ts 之后最新运行日志的该轮结果。

    返回 (detected, attempted, task_status)：task_status 取该轮
    task_finished 事件的 status（completed / likely_completed / unfinished）。
    找不到日志、无 run_finished 或解析失败返回 None。
    """
    # Windows 下文件 mtime 量化精度可能略低于 time.time()，留 2 秒容差
    # 防止本轮日志被时间戳过滤误杀（轮间隔最小 10 秒，不会误读上一轮）。
    skew = 2.0
    candidates = [
        p
        for p in logs_dir.glob("taojinbi-*.jsonl")
        if p.is_file() and p.stat().st_mtime >= since_ts - skew
    ]
    if not candidates:
        return None
    latest = max(candidates, key=lambda p: p.stat().st_mtime)
    try:
        finished = None
        task_status = None
        for line in latest.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            event = json.loads(line)
            if event.get("event") == "task_finished":
                task_status = event.get("status")
            elif event.get("event") == "run_finished":
                finished = event
        if finished is None:
            return None
        counts = finished.get("counts") or {}
        return (
            int(counts.get("detected", 0)),
            int(counts.get("attempted", 0)),
            task_status,
        )
    except (OSError, ValueError, AttributeError, TypeError):
        return None


def build_child_args(args, sidecar_port: int = 0) -> list:
    """构造子进程唯一 CLI 参数：GPU 固定开启、单任务上限、sidecar 端口；
    task 为 any 时省略 --task（CLI 默认扫描全部已注册任务）。"""
    child = [
        sys.executable,
        str(CLI_PATH),
        "--serial",
        args.serial,
        "--gpu",
        "--max-tasks",
        "1",
    ]
    if sidecar_port:
        child.extend(["--ocr-sidecar-port", str(sidecar_port)])
    if args.task != "any":
        child.extend(["--task", args.task])
    return child


# 守候会话日志允许落盘的字段白名单。
# 隐私承诺（README/公开版日志约束）：不记录设备序列号、OCR 原文、商品名、
# 截图路径、坐标等。任何不在白名单里的字段静默丢弃，避免未来误写隐私。
ALLOWED_SESSION_LOG_FIELDS = frozenset({
    "event", "ts",
    "reason", "tasks_session", "tasks_today",
    "cycle", "exit_code", "detected", "task_status", "status",
    "kind", "seconds", "stage", "attempt",
})


def _terminate_proc(proc, timeout: float = 3.0) -> None:
    """完整清理子进程：terminate → wait → kill → wait（幂等）。"""
    if proc is None or proc.poll() is not None:
        return
    try:
        proc.terminate()
        proc.wait(timeout=timeout)
    except Exception:
        pass
    if proc.poll() is None:
        try:
            proc.kill()
            proc.wait(timeout=timeout)
        except Exception:
            pass


class SessionEventLogger:
    """守候会话事件日志（JSONL）：供 wait_panel 整场监控面板读取。

    线程安全（心跳线程与主循环并发追加）；写失败静默忽略，不影响守候。
    仅白名单字段可落盘，其他字段（含序列号/OCR 原文/坐标）静默丢弃。
    """

    def __init__(self, logs_dir: Path):
        stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
        self.path = Path(logs_dir) / f"wait-session-{stamp}.jsonl"
        self._lock = threading.Lock()

    def emit(self, event: str, **fields) -> None:
        record = {"event": event, "ts": time.time()}
        for key, value in fields.items():
            if key in ALLOWED_SESSION_LOG_FIELDS:
                record[key] = value
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self._lock, self.path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(record, ensure_ascii=False) + "\n")
        except OSError:
            pass


def _spawn_wait_panel():
    """Windows 下以独立控制台窗口启动整场监控面板；非 Windows 返回 None。"""
    if os.name != "nt":
        return None
    import subprocess

    try:
        return subprocess.Popen(
            [
                sys.executable,
                str(Path(__file__).resolve().parent / "wait_panel.py"),
                "--auto-exit",
            ],
            creationflags=getattr(subprocess, "CREATE_NEW_CONSOLE", 0),
        )
    except OSError as error:
        # 面板拉起失败不影响守候主流程。
        print(f"监控面板启动失败（{type(error).__name__}），继续守候")
        return None


def _parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description="守候任务池：只要有任务就一直循环执行（每轮唯一 CLI，"
        "单任务上限；任务池空或触发安全停止时收场）"
    )
    parser.add_argument("--serial", required=True, help="设备序列号")
    parser.add_argument(
        "--task",
        default="any",
        choices=TASK_CHOICES,
        help="目标任务键：any=扫描全部已注册任务（默认）；"
        "或 search / hashtag / featured_goods",
    )
    parser.add_argument(
        "--max-tasks",
        type=int,
        default=0,
        help="本场最多执行几个任务；0 = 不限（默认，任务池空才停）",
    )
    parser.add_argument(
        "--daily-cap",
        type=int,
        default=0,
        help="跨会话每日任务上限（状态文件计数）；0 = 不启用（默认）",
    )
    parser.add_argument(
        "--max-wait-cycles",
        type=int,
        default=DEFAULT_MAX_WAIT_CYCLES,
        help="一个守候阶段最多检查轮数，整阶段无任务即收场（默认 8）",
    )
    parser.add_argument(
        "--min-gap-s",
        type=int,
        default=DEFAULT_MIN_GAP_S,
        help="空窗检查间隔下限秒数（默认 180）",
    )
    parser.add_argument(
        "--max-gap-s",
        type=int,
        default=DEFAULT_MAX_GAP_S,
        help="空窗检查间隔上限秒数（默认 480）",
    )
    parser.add_argument(
        "--done-rest-min-s",
        type=int,
        default=DEFAULT_DONE_REST_MIN_S,
        help="做完一个任务后的休息下限秒数（默认 300）",
    )
    parser.add_argument(
        "--done-rest-max-s",
        type=int,
        default=DEFAULT_DONE_REST_MAX_S,
        help="做完一个任务后的休息上限秒数（默认 900）",
    )
    parser.add_argument(
        "--grind-rest-min-s",
        type=int,
        default=DEFAULT_GRIND_REST_MIN_S,
        help="同一任务未完成轮次间休息下限秒数（默认 60）",
    )
    parser.add_argument(
        "--grind-rest-max-s",
        type=int,
        default=DEFAULT_GRIND_REST_MAX_S,
        help="同一任务未完成轮次间休息上限秒数（默认 180）",
    )
    parser.add_argument(
        "--session-deadline-min",
        type=int,
        default=DEFAULT_SESSION_DEADLINE_MIN,
        help="整场硬上限分钟数（默认 240；0 = 不限时，不建议）",
    )
    parser.add_argument(
        "--no-ocr-sidecar",
        action="store_true",
        help="不为每轮 CLI 启动 OCR 推理 sidecar（默认启动，省去每轮模型冷启动）",
    )
    parser.add_argument(
        "--no-panel",
        action="store_true",
        help="不启动整场监控面板窗口（默认启动）",
    )
    return parser.parse_args(argv)


def run_supervisor(
    args,
    rng: random.Random,
    clock=time.monotonic,
    sleeper=time.sleep,
    runner=None,
    state_path=None,
    session_logger=None,
    sidecar_spawner=None,
) -> int:
    """守候主循环：外层任务循环 × 内层守候阶段；异常即停，任务池空收场。

    ``sidecar_spawner`` 用于注入 sidecar 启动动作（测试必须注入，避免
    真实子进程留下孤儿进程）；为 None 时使用默认 ``_spawn_ocr_sidecar``。

    全程向 session_logger 发整场事件（session_started / cycle_started /
    cycle_finished / task_done / rest_started / heartbeat /
    session_finished），供 wait_panel 整场监控面板读取。
    """
    if session_logger is None:
        session_logger = SessionEventLogger(LOGS_DIR)
    sidecar_proc = None
    panel_proc = None
    sidecar_port = 0
    if not getattr(args, "no_ocr_sidecar", False):
        spawn_sidecar = sidecar_spawner or _spawn_ocr_sidecar
        sidecar_proc, sidecar_addr = spawn_sidecar()
        if sidecar_addr:
            sidecar_port = sidecar_addr[1]
    try:
        session_started_ts = clock()
        session_deadline_s = args.session_deadline_min * 60
        today = time.strftime("%Y-%m-%d")
        state_path = state_path or STATE_PATH
        tasks_done_total = load_daily_done(state_path, today)
        session_start_count = tasks_done_total
        session_logger.emit(
            "session_started",
            task=args.task,
            max_tasks=args.max_tasks,
            daily_cap=args.daily_cap,
            session_deadline_min=args.session_deadline_min,
        )
        if not getattr(args, "no_panel", False):
            panel_proc = _spawn_wait_panel()

        stop_heartbeat = threading.Event()
        if runner is None:
            # 心跳：面板据此判断主进程存活（90 秒无心跳即自动退出）。
            def _heartbeat():
                while not stop_heartbeat.wait(30.0):
                    session_logger.emit("heartbeat")

            threading.Thread(target=_heartbeat, daemon=True).start()

        def finish(reason: str, code: int) -> int:
            stop_heartbeat.set()
            if sidecar_proc is not None:
                try:
                    sidecar_proc.terminate()
                except Exception:
                    pass
            session_logger.emit(
                "session_finished",
                reason=reason,
                tasks_session=tasks_done_total - session_start_count,
                tasks_today=tasks_done_total,
            )
            return code

        if args.daily_cap and tasks_done_total >= args.daily_cap:
            print(
                f"守候结束：stop_daily_cap（今日已执行 {tasks_done_total}"
                f"/{args.daily_cap}）"
            )
            return finish("stop_daily_cap", 0)

        def deadline_reached():
            return bool(session_deadline_s) and (
                clock() - session_started_ts >= session_deadline_s
            )

        while True:
            # stop 标记文件：后台会话/用户可用它优雅停止守候（一次性，
            # 处理后删除；下次运行不受影响）。
            stop_marker = LOGS_DIR / STOP_FILE_NAME
            if stop_marker.exists():
                print("检测到 stop 标记文件，守候优雅收场")
                try:
                    stop_marker.unlink()
                except OSError:
                    pass
                return finish("stop_file", 0)
            if deadline_reached():
                print(
                    f"守候结束：stop_deadline（本场已执行 "
                    f"{tasks_done_total - session_start_count} 个任务）"
                )
                return finish("stop_deadline", 0)

            # ---- 内层守候阶段：最多 max_wait_cycles 轮，命中或用尽即出 ----
            phase_outcome = "empty"
            found_status = None
            error_exit_code = 0
            cycles_done = 0
            while True:
                if cycles_done >= args.max_wait_cycles:
                    phase_outcome = "empty"
                    break
                if deadline_reached():
                    phase_outcome = "deadline"
                    break
                cycle_index = cycles_done + 1
                scope = (
                    "扫描全部注册任务"
                    if args.task == "any"
                    else f"--task {args.task}"
                )
                print(
                    f"[守候 {cycle_index}/{args.max_wait_cycles}] "
                    f"启动唯一 CLI：{scope} --max-tasks 1"
                )
                session_logger.emit("cycle_started", cycle=cycle_index)
                cycle_started_ts = time.time()
                if runner is None:
                    proc = subprocess.run(build_child_args(args, sidecar_port))
                    exit_code = proc.returncode
                else:
                    exit_code = runner()
                outcome = read_run_outcome(LOGS_DIR, cycle_started_ts)
                if outcome is None:
                    if exit_code == 0:
                        print("守候结束：退出码 0 但日志无法确认该轮结果，保守停止")
                        return finish("stop_unverified_log", 3)
                    detected, found_status = 0, None
                else:
                    detected, _attempted, found_status = outcome
                session_logger.emit(
                    "cycle_finished",
                    cycle=cycle_index,
                    exit_code=exit_code,
                    detected=detected,
                    task_status=found_status,
                )
                decision = decide_after_cycle(exit_code, detected)
                cycles_done += 1
                if decision == "continue":
                    if cycles_done >= args.max_wait_cycles:
                        phase_outcome = "empty"
                        break
                    if deadline_reached():
                        phase_outcome = "deadline"
                        break
                    delay = next_delay(rng, args.min_gap_s, args.max_gap_s)
                    print(f"本轮无候选，{delay:.0f} 秒后检查下一轮（Ctrl+C 可随时停止）")
                    session_logger.emit("rest_started", kind="gap", seconds=delay)
                    sleeper(delay)
                    continue
                if decision == "stop_found":
                    phase_outcome = "found"
                    break
                phase_outcome = "error"
                error_exit_code = exit_code
                break

            # ---- 阶段结果分派 ----
            if phase_outcome == "found":
                tasks_done_total += 1
                session_logger.emit(
                    "task_done",
                    tasks_session=tasks_done_total - session_start_count,
                    tasks_today=tasks_done_total,
                    task_status=found_status,
                )
                if args.daily_cap:
                    save_daily_done(state_path, today, tasks_done_total)
                    if tasks_done_total >= args.daily_cap:
                        print(
                            f"守候结束：stop_daily_cap（今日已执行 "
                            f"{tasks_done_total}/{args.daily_cap}）；"
                            "结算与到账请人工核对余额"
                        )
                        return finish("stop_daily_cap", 0)
                if args.max_tasks and (
                    tasks_done_total - session_start_count
                ) >= args.max_tasks:
                    print(
                        f"守候结束：stop_task_budget（本场已执行 "
                        f"{tasks_done_total - session_start_count}"
                        f"/{args.max_tasks}）；结算与到账请人工核对余额"
                    )
                    return finish("stop_task_budget", 0)
                tier = rest_tier_for(found_status)
                rest = next_rest_seconds(rng, args, tier)
                print(
                    f"任务已执行（结果档位：{tier}），{rest:.0f} 秒后继续守候"
                )
                session_logger.emit("rest_started", kind=tier, seconds=rest)
                sleeper(rest)
                continue
            if phase_outcome == "deadline":
                print(
                    f"守候结束：stop_deadline（本场已执行 "
                    f"{tasks_done_total - session_start_count} 个任务）"
                )
                return finish("stop_deadline", 0)
            if phase_outcome == "error":
                if error_exit_code == EXIT_INTERRUPT:
                    print("守候结束：stop_interrupt")
                    return finish("stop_interrupt", EXIT_INTERRUPT)
                if error_exit_code == EXIT_ATTEMPTED_UNCONFIRMED:
                    print(
                        "守候结束：stop_attempted（已执行但未确认完成）；"
                        "请人工核对余额后再决定是否重启守候"
                    )
                    return finish("stop_attempted", EXIT_ATTEMPTED_UNCONFIRMED)
                print(
                    f"守候结束：stop_error（exit={error_exit_code}）；"
                    "请人工排查后再决定是否重启守候"
                )
                return finish("stop_error", error_exit_code)
            print(
                f"守候结束：stop_no_tasks（一个守候阶段内未见任务，"
                f"本场共执行 {tasks_done_total - session_start_count} 个）"
            )
            return finish("stop_no_tasks", 0)
    finally:
        _terminate_proc(sidecar_proc)
        _terminate_proc(panel_proc)


def main(argv=None) -> int:
    args = _parse_args(argv)
    if args.min_gap_s > args.max_gap_s:
        print("参数错误：--min-gap-s 不能大于 --max-gap-s")
        return 2
    if args.done_rest_min_s > args.done_rest_max_s:
        print("参数错误：--done-rest-min-s 不能大于 --done-rest-max-s")
        return 2
    if args.grind_rest_min_s > args.grind_rest_max_s:
        print("参数错误：--grind-rest-min-s 不能大于 --grind-rest-max-s")
        return 2
    return run_supervisor(args, random.Random())


if __name__ == "__main__":
    raise SystemExit(main())
