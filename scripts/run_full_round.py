"""完整一轮：三个白名单任务（搜一搜/看看#/发现精选好物）各执行到配额，
结束后自动核对余额。显式批量模式——仅在用户明确要求时使用。

用法:
    python scripts/run_full_round.py --serial <序列号> [--before <历史余额>]
机制:
    每任务循环拉起唯一 CLI（--task X --max-tasks 1，与守候同安全模型），
    依据每轮日志的 task_finished status 判定推进：
      completed       -> 到配额，该任务完成
      likely_completed-> 已推进（展示滞后），继续下一轮
      其他            -> 本轮未推进（记录，连续 3 次则放弃该任务）
"""
import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO / "src"))
sys.path.insert(0, str(_REPO / "scripts"))
_RUN_CLI = _REPO / "scripts" / "run_taojinbi.py"
_LOG_DIR = _REPO / "logs"

TASKS = ("search", "hashtag", "featured_goods")
MAX_ROUNDS_PER_TASK = 8
STALL_LIMIT = 3          # 连续无推进轮数上限
ROUND_GAP_S = 5          # 轮间等待（结算/计数刷新）
_MAX_READ_TRIES = 3


def latest_task_finished(log_dir):
    """最新 run 日志里最后一条 task_finished（含 task_key/status/reason）。"""
    files = sorted(log_dir.glob("taojinbi-*.jsonl"), key=lambda p: p.stat().st_mtime)
    if not files:
        return None
    for line in reversed(files[-1].read_text(encoding="utf-8").splitlines()):
        try:
            e = json.loads(line)
        except json.JSONDecodeError:
            continue
        if e.get("event") == "task_finished":
            return e
    return None


def run_one_round(python, serial, task_key):
    """拉起唯一 CLI 执行一轮该任务；返回子进程退出码。"""
    return subprocess.call(
        [python, "-X", "utf8", str(_RUN_CLI),
         "--serial", serial, "--task", task_key,
         "--max-tasks", "1", "--gpu"],
        cwd=str(_REPO),
    )


def run_full_round(serial, before=None, *, python=None, gap=ROUND_GAP_S,
                   max_rounds=MAX_ROUNDS_PER_TASK, stall_limit=STALL_LIMIT,
                   sleep=time.sleep, log_dir=_LOG_DIR, run_one=run_one_round):
    """执行完整一轮；返回 (task_outcomes, final_balance)。"""
    python = python or sys.executable
    outcomes = {}
    for task in TASKS:
        stall = 0
        for _round in range(1, max_rounds + 1):
            run_one(python, serial, task)
            sleep(gap)
            finished = latest_task_finished(log_dir)
            status = (finished or {}).get("status", "")
            reason = (finished or {}).get("reason", "")
            if status == "completed":
                outcomes[task] = f"completed"
                break
            if status == "likely_completed":
                stall = 0
                outcomes[task] = f"in_progress({reason})"
                continue
            stall += 1
            outcomes[task] = f"stalled({reason or 'no_finish'})"
            if stall >= stall_limit:
                break
        else:
            outcomes[task] = f"round_limit"
    balance = None
    if before is not None:
        from taojinbi_mav import ocr_ui
        import run_taojinbi as rt
        try:
            import easyocr
            import uiautomator2 as u2
            device = u2.connect(serial)
            reader = easyocr.Reader(["ch_sim", "en"], gpu=False)
            spans = rt.ocr_screen(device, reader)
            balance = ocr_ui.parse_coin_balance(spans)
        except Exception as error:
            balance = None
    return outcomes, balance


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="完整一轮：三任务到配额 + 核对余额")
    parser.add_argument("--serial", required=True)
    parser.add_argument("--before", type=int, default=None, help="历史余额（输出差值）")
    args = parser.parse_args(argv)

    print("完整一轮开始：搜一搜 → 看看# → 发现精选好物（各到配额）")
    outcomes, balance = run_full_round(args.serial, before=args.before)
    print("--- 任务结果 ---")
    for task, outcome in outcomes.items():
        print(f"  {task:16}: {outcome}")
    if balance is not None:
        print(f"当前余额: {balance}")
        if args.before is not None:
            print(f"对比历史 {args.before}: 差值 {balance - args.before:+d}")
    else:
        print("余额读取失败（请手动核对）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
