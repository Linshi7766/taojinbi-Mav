"""OCR 版"好物沉浸看"处理器（设备层）。

复用纯逻辑：taojinbi_mav.ocr_ui（识别/定位/坐标校验）与 taojinbi_mav.task_core
（run_verified_immersive_progress 进度状态机）。本模块导入时不执行任何设备操作，
easyocr / uiautomator2 仅在 __main__ 直接运行时加载。

真机确认的交互模型（详见 docs/superpowers/specs/2026-07-30-taojinbi-ocr-immersive-design.md）：
普通沉浸浏览任务进入信息流后停留并滑动；"搜一搜"在选择"搜索发现"关键词后，
只在搜索结果页上下滑动，不点击商品。进度只在任务列表弹窗可读，绝不点击交易按钮。
"""

import os
import re
import sys
import time
from dataclasses import dataclass, replace
from pathlib import Path

# src 布局 bootstrap：允许 `python scripts/run_taojinbi.py` 直接运行；
# pip install -e . 后此插入无副作用。
_SRC = Path(__file__).resolve().parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from taojinbi_mav.ocr_ui import (
    COIN_ENTRY_BUTTON,
    TASK_KEYS,
    UNKNOWN_TASK_LABEL,
    find_immersive_progress,
    find_immersive_target,
    find_safe_browse_target,
    find_unique_ocr_span,
    inspect_visible_task_rows,
    is_product_detail_page,
    is_safe_tap_point,
    is_search_entry_page,
    is_search_flow_page,
    is_taobao_home_page,
    is_coin_task_product_page,
    is_search_result_feed,
    page_fingerprint,
    locate_by_scroll,
    ocr_has_risk,
    parse_ocr_spans,
    read_safe_browse_progress,
    screen_text_signature,
    scroll_to_top_then_find,
    titles_equivalent,
)
from taojinbi_mav.runtime.ocr_service import make_sidecar_reader_factory
from taojinbi_mav.runtime.config import (
    DEFAULT_DRY_RUN_TIMEOUT,
    DEFAULT_RECOVERY_TIMEOUT,
    DEFAULT_RUN_TIMEOUT,
    DEFAULT_TASK_TIMEOUT,
    build_ocr_arg_parser,
    resolve_device_serial,
    resolve_ocr_gpu,
)
from taojinbi_mav.runtime.deadline import Deadline, DeadlineExceeded
from taojinbi_mav.runtime.logging import create_runtime_logger
from taojinbi_mav.runtime.outcome import (
    ExitCode,
    RunCounts,
    RunMode,
    RunOutcome,
    RunStatus,
)
from taojinbi_mav.task_core import (
    ImmersiveRunResult,
    run_verified_immersive_progress,
)
from taojinbi_mav.task_strategies import (
    FEED_BROWSE,
    StrategyContext,
    execute_task_strategy,
    select_task_strategy,
)
from taojinbi_mav.tasks.registry import profile_for_title, registered_profiles

TB_APP = "com.taobao.taobao"
LIST_ANCHOR = "赚金币抵钱"   # 任务列表弹窗标题，作为“在列表”的稳定锚点
RUNTIME_SHOT = "_ocr_runtime_shot.png"  # 单一临时截图，_ocr_ 前缀已被 gitignore

# 阶段 3 经验参数（GPU 加速后可适当收紧等待）
SWIPE_SETTLE = 2            # 翻页/加载稳定等待（GPU 下 OCR 快，可从 3s 收紧到 2s）
BROWSE_PER_ROUND = 6        # 每个浏览往返覆盖的商品数上限（按剩余量自适应，见 perform_one）
MAX_BACKS = 6              # 返回任务列表的最大 back 次数
REFRESH_LAG_GRACE_S = 120  # 任务行消失后的展示滞后宽限（浏览计数常延迟数分钟反映）
MAX_LIST_SCROLLS = 8       # 弹窗内滚动查找“好物沉浸看”的最大次数
ENTRY_VALIDATION_RETRIES = 2  # 入口二次校验最多重试次数，防止 OCR 抖动导致无限循环
ENTRY_RETRY_DELAY = 0.5       # 重试前短暂等待页面稳定


REFRESH_RECOVERY_ATTEMPTS = 2
COIN_PAGE_ANCHOR = "淘金币"
MORE_COINS_ACTION = "赚更多金币"
POPUP_CLOSE_CONTROL = "更多"   # 任务弹窗右上角关闭控件（OCR 可读，真机 2026-08-29 用户演示验证）

# dry-run 行判定 task_key（稳定内部标识）→ 日志 TASK_LABELS 键（脱敏标签键）
_DRY_RUN_LOG_KEY = {value: key for key, value in TASK_KEYS.items()}


def _checkpoint(deadline):
    """可选 deadline 检查点：无 deadline 时是空操作。"""
    if deadline is not None:
        deadline.checkpoint()


def _deadline_sleep(deadline, seconds):
    """可选 deadline 睡眠：等待只能用 deadline sleep（受剩余时间上限约束）。"""
    if deadline is not None:
        deadline.sleep(seconds)
    else:
        time.sleep(seconds)


def safe_task_label(title):
    """脱敏标签：已注册任务用 profile.safe_label()，未注册统一返回“未知任务”。

    公开 CLI 可达输出一律使用该标签，禁止打印原始标题/轮换后缀。
    """
    profile = profile_for_title(title)
    if profile is None:
        return UNKNOWN_TASK_LABEL
    return profile.safe_label()


@dataclass(frozen=True)
class RefreshRecoveryResult:
    status: str
    target: object = None
    attempts: int = 0
    reason: str = ""


def classify_refreshed_task(target, expected_progress=None, expected_total=None):
    """Classify whether a refreshed row still belongs to the current cycle."""
    if expected_total is not None and target.total != expected_total:
        return "rotated", "task_total_changed"
    if expected_progress is not None and target.progress < expected_progress:
        return "rotated", "task_progress_reset"
    if target.progress >= target.total:
        return "completed", ""
    return "continue", ""


def ocr_screen(d, reader, min_confidence=0.5):
    """截屏 + easyocr + 过滤，返回 OcrSpan 列表（只读）。"""
    try:
        d.screenshot(RUNTIME_SHOT)
        raw_results = reader.readtext(RUNTIME_SHOT)
        return parse_ocr_spans(raw_results, min_confidence=min_confidence)
    except Exception as error:
        # 截图、OCR 后端或结果解析任一环节失败时，不把旧截图/空结果当成安全页面。
        print(f"OCR 读取失败，安全停止：{type(error).__name__}")
        return None


def current_package(d):
    try:
        app_info = d.app_current()
    except Exception:
        return None
    if not isinstance(app_info, dict):
        return None
    package_name = app_info.get("package")
    return package_name if isinstance(package_name, str) and package_name else None


def read_progress_current_then_full(current_read, full_scan_read,
                                    full_scan_used=False):
    """Read the current list screen, allowing one bounded full scan fallback."""
    current = current_read()
    if current is not None:
        return current, full_scan_used
    if full_scan_used:
        return None, True
    return full_scan_read(), True


def classify_progress_read(spans, title, pair):
    """Classify a progress read without treating an absent row as success."""
    if pair is not None:
        return "ok"
    if not spans:
        return "ocr_unavailable"
    title_seen = any(
        titles_equivalent(
            re.sub(r"\s*\(\s*\d+\s*/\s*\d+\s*\)\s*$", "", span.text).strip(),
            title,
        )
        for span in spans
    )
    return "progress_unreadable" if title_seen else "task_row_unobserved"


def in_taobao_and_safe(d, spans):
    """当前在淘宝且当前屏无风控词。"""
    if not spans:
        return False
    try:
        package_name = current_package(d)
    except Exception:
        return False
    return package_name == TB_APP and not ocr_has_risk(spans)


def safe_tap(d, center, screen_size):
    """仅在坐标落于安全区时点击；坐标从 OCR 边界框动态得来，不写死。

    失败原因用稳定标识 action_point_outside_safe_area，绝不打印坐标。
    """
    if not is_safe_tap_point(center, screen_size):
        print("安全点击失败：action_point_outside_safe_area")
        return False
    d.click(center[0], center[1])
    return True


def build_strategy_context(d, reader, screen, deadline=None, logger=None):
    """用现有设备安全边界构造策略上下文，避免策略模块反向依赖 runtime。

    deadline 非 None 时创建 deadline-aware sleep/checkpoint/swipe/back/tap：
    每次 OCR/设备动作前后 checkpoint，等待只能走 deadline sleep。
    """

    def read_screen():
        _checkpoint(deadline)
        spans = ocr_screen(d, reader)
        _checkpoint(deadline)
        return spans

    def screen_is_safe(spans):
        _checkpoint(deadline)
        ok = in_taobao_and_safe(d, spans)
        _checkpoint(deadline)
        return ok

    def package_is_safe():
        _checkpoint(deadline)
        ok = current_package(d) == TB_APP
        _checkpoint(deadline)
        return ok

    def checked_safe_tap(center):
        _checkpoint(deadline)
        return safe_tap(d, center, screen)

    def emit_diagnostic(payload):
        if logger is not None:
            reason = payload.get("reason", "diagnostic")
            diagnostic = {
                key: value
                for key, value in payload.items()
                if key != "reason"
            }
            logger.emit(
                "page_diagnostic", reason=reason, diagnostic=diagnostic
            )

    return StrategyContext(
        device=d,
        reader=reader,
        screen=screen,
        read_screen=read_screen,
        screen_is_safe=screen_is_safe,
        package_is_safe=package_is_safe,
        safe_tap=checked_safe_tap,
        sleep=lambda seconds: _deadline_sleep(deadline, seconds),
        checkpoint=lambda: _checkpoint(deadline),
        emit_diagnostic=emit_diagnostic if logger is not None else None,
    )


def on_task_list(d, reader):
    """任务列表判据：在淘宝、无风控、且能看到弹窗标题“赚金币抵钱”。"""
    spans = ocr_screen(d, reader)
    return in_taobao_and_safe(d, spans) and any(LIST_ANCHOR in s.text for s in spans)


def back_to_task_list_ocr(d, reader, max_backs=MAX_BACKS, deadline=None):
    """按返回键直到 OCR 判定回到任务列表；失败关闭。每次动作/等待走 deadline。"""
    for _ in range(max_backs):
        _checkpoint(deadline)
        if on_task_list(d, reader):
            return True
        _checkpoint(deadline)
        d.press("back")
        _checkpoint(deadline)
        _deadline_sleep(deadline, SWIPE_SETTLE)
    return on_task_list(d, reader)


def on_coin_page(d, reader):
    """Require a safe Taobao coin page before opening the task popup."""
    spans = ocr_screen(d, reader)
    if not in_taobao_and_safe(d, spans):
        return False
    return (
        any(COIN_PAGE_ANCHOR in span.text for span in spans)
        and find_unique_ocr_span(spans, MORE_COINS_ACTION) is not None
    )


def back_to_coin_page_ocr(d, reader, max_backs=MAX_BACKS, deadline=None):
    """Press back until the Taobao coin page and its refresh action are visible."""
    for _ in range(max(0, max_backs)):
        _checkpoint(deadline)
        spans = ocr_screen(d, reader)
        if not in_taobao_and_safe(d, spans):
            return False
        coin_page_visible = (
            any(COIN_PAGE_ANCHOR in span.text for span in spans)
            and find_unique_ocr_span(spans, MORE_COINS_ACTION) is not None
        )
        if coin_page_visible:
            return True
        task_list_visible = any(LIST_ANCHOR in span.text for span in spans)
        # 任务流程内页面（列表/详情/搜索发现/搜索结果）都允许按返回键退出；
        # 其他未知页面立即停止（避免把弹窗/任务流程外的页面一路退掉）。
        if not (
            task_list_visible
            or is_product_detail_page(spans)
            or is_search_entry_page(spans)
            or is_search_result_feed(spans)
        ):
            return False
        _checkpoint(deadline)
        d.press("back")
        _checkpoint(deadline)
        _deadline_sleep(deadline, SWIPE_SETTLE)
    return False


def refresh_task_after_disappearance(
    d,
    reader,
    screen,
    title,
    max_attempts=REFRESH_RECOVERY_ATTEMPTS,
    expected_progress=None,
    expected_total=None,
    deadline=None,
):
    """Refresh the coin task popup at most twice after a missing task row."""
    attempts_limit = max(0, max_attempts)
    for attempt in range(1, attempts_limit + 1):
        _checkpoint(deadline)
        spans = ocr_screen(d, reader)
        _checkpoint(deadline)
        if not in_taobao_and_safe(d, spans):
            return RefreshRecoveryResult(
                "unsafe", attempts=attempt, reason="unsafe_screen"
            )
        if not back_to_coin_page_ocr(d, reader, deadline=deadline):
            return RefreshRecoveryResult(
                "unsafe", attempts=attempt, reason="coin_page_unavailable"
            )
        _checkpoint(deadline)
        spans = ocr_screen(d, reader)
        _checkpoint(deadline)
        if not in_taobao_and_safe(d, spans):
            return RefreshRecoveryResult(
                "unsafe", attempts=attempt, reason="unsafe_screen"
            )
        action = find_unique_ocr_span(spans, MORE_COINS_ACTION)
        _checkpoint(deadline)
        action_ok = action is not None and safe_tap(d, action.center, screen)
        _checkpoint(deadline)
        if not action_ok:
            return RefreshRecoveryResult(
                "unsafe", attempts=attempt, reason="more_coins_action_unreadable"
            )
        _deadline_sleep(deadline, SWIPE_SETTLE)
        loaded = retry_entry_validation(
            lambda: on_task_list(d, reader),
            max_retries=2,
            retry_delay=ENTRY_RETRY_DELAY,
            sleeper=lambda seconds: _deadline_sleep(deadline, seconds),
            checkpoint=lambda: _checkpoint(deadline),
        )
        if not loaded:
            continue
        _popup_scroll(d, screen, deadline=deadline)
        target = locate_safe_browse_target(
            d, reader, screen, only_titles=(title,), deadline=deadline,
        )
        if target is None:
            continue
        status, reason = classify_refreshed_task(
            target,
            expected_progress=expected_progress,
            expected_total=expected_total,
        )
        return RefreshRecoveryResult(
            status, target=target, attempts=attempt, reason=reason
        )
    return RefreshRecoveryResult(
        "not_found",
        attempts=attempts_limit,
        reason="refresh_not_found",
    )


def _popup_scroll(d, screen, deadline=None):
    """在弹窗内容区上滑，滚动查看下方任务（安全手势，不点按钮）。"""
    width, height = screen
    _checkpoint(deadline)
    d.swipe(width // 2, int(height * 0.70), width // 2, int(height * 0.45), 0.4)
    _checkpoint(deadline)
    _deadline_sleep(deadline, SWIPE_SETTLE)


def _popup_scroll_up(d, screen, deadline=None):
    """在弹窗内容区下滑，回看上方任务（安全手势，不点按钮）。"""
    width, height = screen
    _checkpoint(deadline)
    d.swipe(width // 2, int(height * 0.45), width // 2, int(height * 0.70), 0.4)
    _checkpoint(deadline)
    _deadline_sleep(deadline, SWIPE_SETTLE)


def locate_immersive_target(d, reader, screen, max_scrolls=MAX_LIST_SCROLLS,
                            deadline=None):
    """在任务列表弹窗内滚动查找“好物沉浸看”；找不到（到底/超限）即失败关闭返回 None。"""
    def probe():
        _checkpoint(deadline)
        spans = ocr_screen(d, reader)
        _checkpoint(deadline)
        if not in_taobao_and_safe(d, spans):
            return None, None
        return (
            find_immersive_target(spans, screen),
            screen_text_signature(spans),
        )

    def scroll():
        _popup_scroll(d, screen, deadline=deadline)

    return locate_by_scroll(probe, scroll, max_scrolls=max_scrolls)


def locate_immersive_progress(d, reader, screen, max_scrolls=MAX_LIST_SCROLLS,
                              deadline=None):
    """滚动查找“好物沉浸看”并读取其自身进度（不依赖按钮，完成态 5/5 也能读）。

    返回进度整数（0..5）；滚动到底/超限仍读不到则返回 None。
    """
    def probe():
        _checkpoint(deadline)
        spans = ocr_screen(d, reader)
        _checkpoint(deadline)
        if not in_taobao_and_safe(d, spans):
            return None, None
        value = find_immersive_progress(spans)
        # 用 (value, sig) 交给 locate_by_scroll；value 为 int（含 0）表示命中
        return value, screen_text_signature(spans)

    def scroll():
        _popup_scroll(d, screen, deadline=deadline)

    return locate_by_scroll(probe, scroll, max_scrolls=max_scrolls)


def retry_entry_validation(
    validate,
    max_retries=ENTRY_VALIDATION_RETRIES,
    retry_delay=ENTRY_RETRY_DELAY,
    sleeper=None,
    checkpoint=lambda: None,
):
    """有界重试入口验证，返回最后一次稳定目标，失败返回 None。

    回调返回真值（True / 目标对象 / 非空字符串）视为成功；falsy（False /
    None）视为失败并继续重试——布尔校验（如 on_task_list）返回 False 时
    绝不能提前短路，否则 OCR 抖动会被误报为锚点缺失。
    DeadlineExceeded 显式透传（绝不捕获后降级）；其余异常按类型名脱敏后重试。
    等待只能用可注入 sleeper（deadline 场景传入 deadline sleep）。
    """
    if sleeper is None:
        sleeper = time.sleep
    for attempt in range(max(0, max_retries) + 1):
        try:
            candidate = validate()
        except DeadlineExceeded:
            raise
        except Exception as error:
            print(f"入口校验失败，安全重试：{type(error).__name__}")
            candidate = None
        if candidate:
            return candidate
        checkpoint()
        if attempt < max_retries and retry_delay > 0:
            sleeper(retry_delay)
    return None


def enter_immersive_from_list(d, reader, deadline=None):
    """滚动定位“好物沉浸看”，点前在静止屏重定位（动态坐标）后点击“去完成”。"""
    screen = d.window_size()
    def locate_and_validate():
        target = locate_immersive_target(d, reader, screen, deadline=deadline)
        if target is None:
            return None
        # 每次重试都重新截图、重新定位，绝不复用上一次的 OCR 坐标。
        _checkpoint(deadline)
        spans = ocr_screen(d, reader)
        _checkpoint(deadline)
        if not in_taobao_and_safe(d, spans):
            return None
        target2 = find_immersive_target(spans, screen)
        if target2 is None or target2.progress_text != target.progress_text:
            print("好物沉浸看：点前重定位不稳定/进度变化，准备有界重试")
            return None
        return target2

    target2 = retry_entry_validation(
        locate_and_validate,
        sleeper=lambda seconds: _deadline_sleep(deadline, seconds),
        checkpoint=lambda: _checkpoint(deadline),
    )
    if target2 is None:
        print("好物沉浸看：入口二次校验连续失败，失败关闭")
        return False
    print("好物沉浸看：定位成功，点击去完成（entry_validated）")
    _checkpoint(deadline)
    ok = safe_tap(d, target2.action_center, screen)
    _checkpoint(deadline)
    return ok


def run_immersive_goods_task_ocr(d, reader, back_to_list=None, target_progress=5,
                                 deadline=None):
    """在任务列表上调用；用往返验证推进 x/target 直到完成或停滞。"""
    if back_to_list is None:
        def back_to_list():
            return back_to_task_list_ocr(d, reader, deadline=deadline)
    screen = d.window_size()

    def read_progress():
        # 只认“好物沉浸看”自身进度（精确标题 + 滚动查找，不依赖按钮，完成态 5/5 也能读）
        value = locate_immersive_progress(
            d, reader, screen, deadline=deadline,
        )
        return f"{value}/{target_progress}" if value is not None else None

    def still_allowed():
        _checkpoint(deadline)
        allowed = in_taobao_and_safe(d, ocr_screen(d, reader))
        _checkpoint(deadline)
        return allowed

    def perform_one():
        # 一次浏览往返：进 feed → 停留+上滑覆盖若干商品 → 返回列表交给 read_progress 验证
        if not enter_immersive_from_list(d, reader, deadline=deadline):
            print("好物沉浸看：无法定位并进入信息流，失败关闭")
            return False
        _deadline_sleep(deadline, SWIPE_SETTLE)
        strategy_result = execute_task_strategy(
            FEED_BROWSE,
            build_strategy_context(d, reader, screen, deadline=deadline),
            BROWSE_PER_ROUND,
        )
        if not strategy_result.ok:
            print(f"好物沉浸看：策略执行失败（{strategy_result.reason}）")
            return False
        if not back_to_list():
            print("好物沉浸看：浏览后未能返回任务列表，失败关闭")
            return False
        return True

    result = run_verified_immersive_progress(
        read_progress=read_progress,
        perform_one=perform_one,
        still_allowed=still_allowed,
        target=target_progress,
        sleeper=lambda seconds: _deadline_sleep(deadline, seconds),
        checkpoint=lambda: _checkpoint(deadline),
    )
    for before, after in result.transitions:
        print(f"好物沉浸看进度：{before}/{target_progress} -> {after}/{target_progress}")
    if result.completed:
        print("好物沉浸看已完成（已读到 5/5）")
    elif result.reason == "missing_progress" and result.progress > 0:
        # 起始读到过进度、浏览后再也读不到：不谎报“已完成”，如实说明并建议核对余额
        print(
            f"好物沉浸看：起始 {result.progress}/{target_progress}，浏览后无法再读到进度——"
            "可能已完成，也可能任务已轮换或 OCR 未读到，请核对金币余额确认"
        )
    elif result.reason == "missing_progress":
        print("好物沉浸看：未读到起始进度（未找到任务或 OCR 失败），未执行")
    else:
        print(f"好物沉浸看未完成：{result.reason}")
    return result


def locate_safe_browse_target(d, reader, screen, only_titles=None,
                              exclude_titles=(), max_scrolls=MAX_LIST_SCROLLS,
                              deadline=None):
    """先回到弹窗顶部，再从顶向下全覆盖查找安全浏览任务候选；无则返回 None。"""
    def probe():
        _checkpoint(deadline)
        spans = ocr_screen(d, reader)
        _checkpoint(deadline)
        if not in_taobao_and_safe(d, spans):
            return None, None
        target = find_safe_browse_target(
            spans, screen, only_titles=only_titles, exclude_titles=exclude_titles,
        )
        return target, screen_text_signature(spans)

    return scroll_to_top_then_find(
        probe,
        scroll_up=lambda: _popup_scroll_up(d, screen, deadline=deadline),
        scroll_down=lambda: _popup_scroll(d, screen, deadline=deadline),
        max_scrolls=max_scrolls,
    )


def locate_task_progress(d, reader, screen, title, max_scrolls=MAX_LIST_SCROLLS,
                         deadline=None):
    """先回顶再下扫，查找标题（含轮换等价）任务并读 (num, total)；读不到返回 None。

    返回列表后弹窗常停在中部，目标行可能在上方；只向下扫会系统性漏读，
    故与发现阶段一致采用全覆盖扫描。
    """
    def probe():
        _checkpoint(deadline)
        spans = ocr_screen(d, reader)
        _checkpoint(deadline)
        if not in_taobao_and_safe(d, spans):
            return None, None
        return read_safe_browse_progress(spans, title), screen_text_signature(spans)

    return scroll_to_top_then_find(
        probe,
        scroll_up=lambda: _popup_scroll_up(d, screen, deadline=deadline),
        scroll_down=lambda: _popup_scroll(d, screen, deadline=deadline),
        max_scrolls=max_scrolls,
    )


def enter_task_from_list(d, reader, title, deadline=None):
    """滚动定位标题（含轮换等价）任务，点前静止屏重定位后点击其动作按钮。"""
    screen = d.window_size()
    label = safe_task_label(title)
    def locate_and_validate():
        target = locate_safe_browse_target(
            d,
            reader,
            screen,
            only_titles=(title,),
            deadline=deadline,
        )
        if target is None or not titles_equivalent(target.title, title):
            return None
        _checkpoint(deadline)
        spans = ocr_screen(d, reader)
        _checkpoint(deadline)
        if not in_taobao_and_safe(d, spans):
            return None
        target2 = find_safe_browse_target(
            spans,
            screen,
            only_titles=(title,),
        )
        if (target2 is None or not titles_equivalent(target2.title, title)
                or (target2.progress, target2.total)
                != (target.progress, target.total)):
            print(f"{label}：点前重定位不稳定/进度变化，准备有界重试")
            return None
        return target2

    target2 = retry_entry_validation(
        locate_and_validate,
        sleeper=lambda seconds: _deadline_sleep(deadline, seconds),
        checkpoint=lambda: _checkpoint(deadline),
    )
    if target2 is None:
        print(f"{label}：入口二次校验连续失败，失败关闭")
        return False
    print(f"{label}：定位成功，点击动作按钮（entry_validated）")
    _checkpoint(deadline)
    ok = safe_tap(d, target2.action_center, screen)
    _checkpoint(deadline)
    return ok


def run_one_safe_browse_task(d, reader, title, total, deadline=None,
                             logger=None):
    """完成单个安全浏览任务：往返验证推进 x/total 直到完成或停滞。"""
    profile = profile_for_title(title)
    if profile is None:
        result = ImmersiveRunResult(
            False, 0, 0, "unsupported_task", ()
        )
        print(f"跳过任务：{safe_task_label(title)}；原因：unsupported_task")
        return result, False
    label = safe_task_label(title)
    screen = d.window_size()
    last_progress = {"value": None, "total": total}
    entered = {"count": 0}   # 记录是否真的进入并浏览过（用于诚实上报）
    progress_state = {
        "after_return": False,
        "full_scan_used": False,
        "last_read_reason": "not_started",
    }

    def read_current_progress():
        _checkpoint(deadline)
        spans = ocr_screen(d, reader)
        _checkpoint(deadline)
        if not spans:
            progress_state["last_read_reason"] = "ocr_unavailable"
            return None
        if not in_taobao_and_safe(d, spans):
            progress_state["last_read_reason"] = "unsafe_screen"
            return None
        pair = read_safe_browse_progress(spans, title)
        progress_state["last_read_reason"] = classify_progress_read(
            spans, title, pair,
        )
        if pair is None:
            return None
        last_progress["value"] = pair[0]
        last_progress["total"] = pair[1]
        return f"{pair[0]}/{pair[1]}"

    def read_full_progress():
        _checkpoint(deadline)
        pair = locate_task_progress(
            d, reader, screen, title, deadline=deadline,
        )
        _checkpoint(deadline)
        if pair is None:
            if progress_state["last_read_reason"] == "not_started":
                progress_state["last_read_reason"] = "task_row_unobserved"
            return None
        progress_state["last_read_reason"] = "ok"
        last_progress["value"] = pair[0]
        last_progress["total"] = pair[1]
        return f"{pair[0]}/{pair[1]}"

    def read_progress():
        if not progress_state["after_return"]:
            return read_full_progress()
        value, full_scan_used = read_progress_current_then_full(
            read_current_progress,
            read_full_progress,
            full_scan_used=progress_state["full_scan_used"],
        )
        progress_state["full_scan_used"] = full_scan_used
        return value

    def still_allowed():
        _checkpoint(deadline)
        allowed = in_taobao_and_safe(d, ocr_screen(d, reader))
        _checkpoint(deadline)
        return allowed

    def perform_one():
        progress_state["after_return"] = False
        progress_state["full_scan_used"] = False
        if not enter_task_from_list(d, reader, title, deadline=deadline):
            print(f"{label}：无法定位并进入信息流，失败关闭")
            return False
        entered["count"] += 1
        _deadline_sleep(deadline, SWIPE_SETTLE)
        strategy = select_task_strategy(profile)
        if strategy is None:
            print(f"{label}：无法选择安全浏览策略，失败关闭")
            back_to_task_list_ocr(d, reader, deadline=deadline)
            return False

        current = last_progress["value"] or 0
        current_total = last_progress["total"] or total
        browse_count = (
            1 if strategy != FEED_BROWSE
            else min(max(current_total - current, 1) + 1, BROWSE_PER_ROUND)
        )
        strategy_result = execute_task_strategy(
            strategy,
            build_strategy_context(
                d, reader, screen, deadline=deadline, logger=logger,
            ),
            browse_count,
        )
        if not strategy_result.ok:
            print(f"{label}：策略执行失败（{strategy_result.reason}），停止本轮")
            back_to_task_list_ocr(d, reader, deadline=deadline)
            return False
        # 真机经验：每轮浏览后必须退出弹窗到"赚更多金币"界面结算，
        # 任务才会计数；停留弹窗内连续浏览不结算会导致进度不推进（stalled）。
        # 用 _safe_back 而非 back_to_coin_page_ocr：搜索结果页 OCR 特征偶发
        # 不可识别，安全回退不依赖页面类型判定（包名/风险词/目标检测兜底）。
        if not _safe_back_to_coin_page(d, reader, deadline=deadline):
            print(f"{label}：浏览后未能退出到赚更多金币界面，失败关闭")
            _emit_recovery_diagnostic(
                d, reader, logger, "settle_back_failed"
            )
            return False
        if not _reopen_task_popup(d, reader, screen, deadline=deadline):
            print(f"{label}：重新打开任务弹窗失败，失败关闭")
            _emit_recovery_diagnostic(
                d, reader, logger, "reopen_popup_failed"
            )
            return False
        progress_state["after_return"] = True
        return True

    refresh_attempts_used = 0
    while True:
        result = run_verified_immersive_progress(
            read_progress=read_progress,
            perform_one=perform_one,
            still_allowed=still_allowed,
            target=last_progress["total"] or total,
            allow_dynamic_total=profile.allow_dynamic_total,
            max_total_changes=2,
            missing_progress_reason=lambda: progress_state["last_read_reason"],
            sleeper=lambda seconds: _deadline_sleep(deadline, seconds),
            checkpoint=lambda: _checkpoint(deadline),
        )
        browsed = entered["count"] > 0
        remaining_refreshes = REFRESH_RECOVERY_ATTEMPTS - refresh_attempts_used
        if (
            result.reason != "task_row_unobserved"
            or not browsed
            or remaining_refreshes <= 0
        ):
            break
        grace_pending = entered["count"] > 0
        resumed = False
        while True:
            # 每次尝试前重算剩余预算：宽限不得重置预算（全局两次上限）。
            remaining_refreshes = (
                REFRESH_RECOVERY_ATTEMPTS - refresh_attempts_used
            )
            if remaining_refreshes <= 0:
                result = replace(result, reason="refresh_not_found")
                break
            recovery = refresh_task_after_disappearance(
                d,
                reader,
                screen,
                title,
                max_attempts=remaining_refreshes,
                expected_progress=result.progress,
                expected_total=last_progress["total"] or total,
                deadline=deadline,
            )
            refresh_attempts_used += recovery.attempts
            print(
                f"{label}：任务行消失后的刷新尝试 "
                f"{refresh_attempts_used}/{REFRESH_RECOVERY_ATTEMPTS}，"
                f"结果={recovery.status} {recovery.reason or ''}".strip()
            )
            if recovery.status == "continue" and recovery.target is not None:
                last_progress["value"] = recovery.target.progress
                last_progress["total"] = recovery.target.total
                progress_state["after_return"] = True
                progress_state["full_scan_used"] = False
                resumed = True
                break
            if recovery.status == "completed" and recovery.target is not None:
                result = replace(
                    result,
                    completed=True,
                    progress=recovery.target.progress,
                    reason="completed_after_refresh",
                )
                break
            if recovery.status == "not_found" and grace_pending:
                # 展示滞后宽限（2026-08-29：浏览计数常延迟数分钟才反映到
                # 读数，立即结论会误停——宽限后做一次末次复核）。
                # 预算不重置：两次刷新上限是全局硬边界。预算已用尽时
                # 宽限等待无意义（不会复核），直接收场，不空等。
                if refresh_attempts_used >= REFRESH_RECOVERY_ATTEMPTS:
                    result = replace(result, reason="refresh_not_found")
                    break
                grace_pending = False
                print(f"{label}：等待展示滞后宽限 "
                      f"{REFRESH_LAG_GRACE_S} 秒后末次复核")
                _deadline_sleep(deadline, REFRESH_LAG_GRACE_S)
                continue
            if recovery.status == "not_found":
                result = replace(result, reason="refresh_not_found")
                break
            if recovery.status == "rotated":
                result = replace(result, reason="task_rotated_after_refresh")
                break
            result = replace(
                result,
                reason=recovery.reason or "refresh_recovery_failed",
            )
            break
        if resumed:
            continue
        break
    browsed = entered["count"] > 0   # 是否真的进入并浏览过（供汇总口径一致）
    display_total = result.total_changes[-1][1] if result.total_changes else total
    for before, after in result.transitions:
        if profile.allow_dynamic_total:
            print(f"{label} 进度变化：{before} -> {after}")
        else:
            print(f"{label} 进度：{before}/{display_total} -> {after}/{display_total}")
    for before, after in result.total_changes:
        print(f"{label} 分母变化：{before} -> {after}，已重置本轮基线")
    if result.completed:
        print(f"{label}：已完成（读到 {result.progress}/{display_total}）")
    elif result.reason == "progress_reset":
        # 浏览后进度回落：任务完成后计数重置进入下一周期，很可能已完成
        print(f"{label}：浏览后进度回落（读到 {result.progress}/{display_total} 后重置）——"
              "任务很可能已完成并进入下一周期，请核对金币余额")
    elif result.reason == "task_rotated_after_refresh" and browsed:
        print(f"{label}：浏览后任务周期已轮换——"
              "任务很可能已完成，请核对金币余额")
    elif result.reason == "missing_progress" and browsed and total == 1:
        # 已进入并浏览过、回来读不到进度：短任务(0/1)完成后行会消失/变已完成，
        # 不能说“未执行”，如实报“已浏览、很可能完成”
        print(f"{label}：已进入并浏览 {entered['count']} 轮，返回后读不到进度——"
              "短任务很可能已完成，请核对金币余额")
    elif result.reason == "task_row_unobserved" and browsed:
        print(
            f"{label}：返回后未观测到任务行，可能已完成、随机轮换或 OCR 漏读——"
            "未确认完成，不自动重试"
        )
    elif result.reason == "progress_unreadable" and browsed:
        print(
            f"{label}：任务行仍可见但进度不可读——未确认完成，不自动重试"
        )
    elif result.reason == "ocr_unavailable" and browsed:
        print(f"{label}：返回后 OCR 不可用——未确认完成，不自动重试")
    elif result.successful_steps > 0:
        print(f"{label}：本轮已推进 {result.successful_steps} 格"
              f"（最后读到 {result.progress}/{display_total}），"
              f"未确认完成（{result.reason}）")
    elif result.reason == "missing_progress":
        print(f"{label}：未读到进度（未找到任务或 OCR 失败），未执行")
    else:
        print(f"{label}：未完成（{result.reason}）")
    return result, browsed


def _task_log_key(title):
    """注册表任务标题 → 日志白名单 task_key（search/hashtag/featured_goods）。"""
    profile = profile_for_title(title)
    return profile.key if profile is not None else None


def titles_for_task_key(task_key):
    """注册表 task_key → only_titles 标题模式列表；None/未知返回 None（不限定）。"""
    if task_key is None:
        return None
    for profile in registered_profiles():
        if profile.key == task_key:
            if profile.exact_title is not None:
                return [profile.exact_title]
            return [profile.title_prefix]
    return None


def _emit_task_finished(logger, task_log_key, status, reason):
    """emit 单任务结束事件；logger 或 task_key 缺失时静默跳过。"""
    if logger is not None and task_log_key is not None:
        logger.emit("task_finished", task_key=task_log_key, status=status, reason=reason)


def _scroll_coin_page_to_top(d, screen, deadline=None, max_swipes=3):
    """淘金币页滚回顶部：连续向上滑（页面下移方向），最多 max_swipes 次。

    真机经验：页面可能被系统/用户滚动到推荐区，推荐卡片上的"赚更多金币"
    是假入口（点进去不是任务弹窗）。调用方需已确认包名安全（淘金币页）。
    """
    width, height = screen
    for _ in range(max_swipes):
        _checkpoint(deadline)
        d.swipe(
            width // 2, int(height * 0.45),
            width // 2, int(height * 0.70),
            0.3,
        )
        _deadline_sleep(deadline, SWIPE_SETTLE)


def _is_coin_root_page(spans, screen_size):
    """淘金币根页面身份合同：顶部区域存在"淘金币"锚点。

    顶部区域 = 锚点中心 y < 40% 屏高。真机首页"淘金币"文本出现多处
    （标题 + 任务卡片等），故不要求全局唯一；推荐区假入口在页面下方
    （顶部无"淘金币"锚点）即被拒绝。入口唯一性属点击前校验，按钮缺失
    走有界重试，不在此判定。
    """
    _width, height = screen_size
    return any(
        COIN_PAGE_ANCHOR in span.text and span.center[1] <= height * 0.4
        for span in spans
    )


def _navigate_home_to_coin_page(d, reader, screen, deadline=None):
    """自动导航：淘宝首页 → 淘金币根页。

    入口：ocr_screen 读屏 → is_taobao_home_page 强信号（顶 tab ≥ 2 + 底栏 ≥ 2）
    → find_unique_ocr_span("领淘金币") → safe_tap → 等待"淘金币"根页身份合同
    可见（顶部 y < 40% 有"淘金币"）。任何一步失败返回 False，**绝不盲点**。
    非首页 / 不可识别"领淘金币"图标 / 进入淘金币失败：均视为 False，由
    上层决定是否降级为 list_anchor_missing。
    """
    try:
        max_attempts = ENTRY_VALIDATION_RETRIES + 1
        for attempt in range(max_attempts):
            _checkpoint(deadline)
            spans = ocr_screen(d, reader)
            if not in_taobao_and_safe(d, spans):
                return False
            is_home, has_coin_entry = is_taobao_home_page(spans)
            if not is_home or not has_coin_entry:
                # 弱信号（仅"领淘金币"图标）或非首页：不盲点
                return False
            entry = find_unique_ocr_span(spans, COIN_ENTRY_BUTTON)
            _checkpoint(deadline)
            if entry is None:
                if attempt == max_attempts - 1:
                    return False
                _deadline_sleep(deadline, ENTRY_RETRY_DELAY)
                continue
            if not safe_tap(d, entry.center, screen):
                return False
            # 等待淘金币根页身份合同
            loaded = retry_entry_validation(
                lambda: _on_coin_root_after_navigation(d, reader, screen),
                max_retries=ENTRY_VALIDATION_RETRIES,
                retry_delay=ENTRY_RETRY_DELAY,
                sleeper=lambda seconds: _deadline_sleep(deadline, seconds),
                checkpoint=lambda: _checkpoint(deadline),
            )
            if loaded:
                return True
            if attempt >= max_attempts - 1:
                return False
            _deadline_sleep(deadline, ENTRY_RETRY_DELAY)
        return False
    except Exception:
        return False


def _on_coin_root_after_navigation(d, reader, screen):
    """导航后的根页身份校验：safe + 顶部 40% 有'淘金币'锚点。

    与 _reopen_task_popup 内的 _is_coin_root_page 重复提取是因为该 helper
    还要被 retry_entry_validation 调用做二次校验，单独函数便于测试覆盖。
    """
    try:
        spans = ocr_screen(d, reader)
    except Exception:
        return False
    if not in_taobao_and_safe(d, spans):
        return False
    return _is_coin_root_page(spans, screen)


def _reopen_task_popup(d, reader, screen, deadline=None):
    """在淘金币首页点击"赚更多金币"重新打开任务弹窗并等待列表锚点。

    真机经验：每轮浏览后必须退出弹窗到"赚更多金币"界面结算，任务才会计数；
    结算后需重开弹窗才能读取下一轮进度。失败返回 False，不抛异常。
    """
    try:
        max_attempts = ENTRY_VALIDATION_RETRIES + 1
        for attempt in range(max_attempts):
            _checkpoint(deadline)
            spans = ocr_screen(d, reader)
            if not in_taobao_and_safe(d, spans):
                return False
            if attempt == 0:
                # 根页面身份合同：唯一顶部锚点 + 唯一入口，缺一即非根页
                # （零动作失败关闭）；按钮缺失仍走下方有界重试。
                if not _is_coin_root_page(spans, screen):
                    return False
                _scroll_coin_page_to_top(d, screen, deadline=deadline)
                spans = ocr_screen(d, reader)
                if not in_taobao_and_safe(d, spans):
                    return False
                # 滚动后重新确认根页身份（滚动可能落入推荐区假入口）
                if not _is_coin_root_page(spans, screen):
                    return False

            action = find_unique_ocr_span(spans, MORE_COINS_ACTION)
            _checkpoint(deadline)
            if action is None:
                if attempt == max_attempts - 1:
                    return False
                _deadline_sleep(deadline, ENTRY_RETRY_DELAY)
                continue

            if not safe_tap(d, action.center, screen):
                return False
            _deadline_sleep(deadline, SWIPE_SETTLE)
            loaded = retry_entry_validation(
                lambda: on_task_list(d, reader),
                max_retries=ENTRY_VALIDATION_RETRIES,
                retry_delay=ENTRY_RETRY_DELAY,
                sleeper=lambda seconds: _deadline_sleep(deadline, seconds),
                checkpoint=lambda: _checkpoint(deadline),
            )
            if loaded:
                return True
            if attempt < max_attempts - 1:
                _deadline_sleep(deadline, ENTRY_RETRY_DELAY)
        return False
    except Exception:
        return False


def _emit_recovery_diagnostic(d, reader, logger, reason):
    """恢复/结算失败时发页面识别指纹（无 OCR 原文）；任何异常静默。"""
    if logger is None:
        return
    try:
        spans = ocr_screen(d, reader)
        logger.emit(
            "page_diagnostic",
            reason=reason,
            diagnostic=page_fingerprint(spans),
        )
    except Exception:
        pass


def _close_task_popup_via_more(d, reader, screen, deadline=None):
    """结算前置：任务弹窗开着会遮挡“赚更多金币”且回合计数不结算。

    在淘金币根页面 + 弹窗标题（赚金币抵钱）同屏时，点击右上角关闭控件
    （“更多”，OCR 精确唯一匹配，坐标经安全校验）关闭弹窗，并确认
    “赚更多金币”恢复可见。任何前置不满足即失败关闭，绝不盲点。
    """
    max_attempts = ENTRY_VALIDATION_RETRIES + 1
    for attempt in range(max_attempts):
        _checkpoint(deadline)
        spans = ocr_screen(d, reader)
        if not spans or not in_taobao_and_safe(d, spans):
            return False
        if find_unique_ocr_span(spans, MORE_COINS_ACTION) is not None:
            return True   # 弹窗已关闭，按钮恢复可见
        if not any(LIST_ANCHOR in span.text for span in spans):
            return False  # 弹窗标题不可见：无从关闭，原地失败
        close = find_unique_ocr_span(spans, POPUP_CLOSE_CONTROL)
        _checkpoint(deadline)
        if close is None:
            if attempt == max_attempts - 1:
                return False
            _deadline_sleep(deadline, ENTRY_RETRY_DELAY)
            continue
        if not safe_tap(d, close.center, screen):
            return False
        _deadline_sleep(deadline, SWIPE_SETTLE)
    return False


def _safe_back_to_coin_page(d, reader, max_backs=MAX_BACKS, deadline=None,
                            screen=None, require_action=True):
    """在淘宝包内按返回键回退到淘金币首页；页面身份感知，绝不越界。

    页面身份规则（用户原则：脚本以当前界面为开始和结束，绝不 wander）：
    - 淘金币标题可见 = 任务生态的根：赚更多金币可见即成功；被遮挡（如奖励
      卡片）时**原地失败**，绝不站在根页面上按返回（防止过冲到淘宝首页）；
    - 已知任务流程页（弹窗列表/商品详情/搜索发现/搜索结果）：按一次返回；
    - 其他未知页面或非淘宝/OCR 不可用：原地失败，不按返回。
    """
    if screen is None:
        screen = d.window_size()
    for _ in range(max(0, max_backs)):
        _checkpoint(deadline)
        spans = ocr_screen(d, reader)
        if not spans or not in_taobao_and_safe(d, spans):
            return False
        if any(COIN_PAGE_ANCHOR in span.text for span in spans):
            # 已到根页面生态：锚点可见即视为到达（按钮/弹窗状态由后续
            # _reopen_task_popup 处理）。按钮可见直接成功；弹窗遮挡时
            # 尽力关弹窗，关不掉（纯遮挡/非弹窗态）也不失败关闭——
            # 绝不在根页按返回（防止过冲到淘宝首页）。
            if find_unique_ocr_span(spans, MORE_COINS_ACTION) is not None:
                return True
            if _close_task_popup_via_more(
                d, reader, screen, deadline=deadline
            ):
                return True  # 关弹窗成功 → 结算界面达成
            # 关不掉：require_action=False（启动导航）锚点可见即已到根页，
            # 弹窗状态由后续 _reopen_task_popup 处理；True（防过冲收尾）
            # 原地失败，绝不 back 过冲首页。
            return not require_action
        if not (
            any(LIST_ANCHOR in span.text for span in spans)
            or is_product_detail_page(spans)
            or is_search_entry_page(spans)
            or is_search_result_feed(spans)
            or is_search_flow_page(spans)
            or is_coin_task_product_page(spans)
        ):
            return False   # 未知页面：原地停止，不盲按返回
        _checkpoint(deadline)
        d.press("back")
        _checkpoint(deadline)
        _deadline_sleep(deadline, SWIPE_SETTLE)
    return False


def _settle_back_to_coin_page(d, reader, deadline=None):
    """正常收尾时退出到淘金币首页（赚更多金币界面），便于刷新后续任务。

    真机经验：任务完成后必须回到"赚更多金币"界面才能刷新出后续任务，
    停留在任务弹窗列表无法继续。只处理安全分支：已在淘金币首页 → 不动；
    任务弹窗列表页/商品详情页 → 有界返回；其他页面或包名不安全 → 不动。
    任何异常（含 deadline 到期）都吞掉并返回 False，绝不外泄或覆盖结果。
    """
    try:
        _checkpoint(deadline)
        spans = ocr_screen(d, reader)
        if not in_taobao_and_safe(d, spans):
            return False
        if (any(COIN_PAGE_ANCHOR in span.text for span in spans)
                and find_unique_ocr_span(spans, MORE_COINS_ACTION) is not None):
            return True   # 已在淘金币首页（赚更多金币可见）
        if not (any(LIST_ANCHOR in span.text for span in spans)
                or is_product_detail_page(spans)
                or is_search_entry_page(spans)
                or is_search_result_feed(spans)
                or is_search_flow_page(spans)
                or is_coin_task_product_page(spans)):
            return False  # 未知页面不动
        return _safe_back_to_coin_page(d, reader, deadline=deadline)
    except Exception:
        return False


def run_safe_browse_tasks(d, reader, max_tasks=8, run_deadline=None,
                          task_timeout=DEFAULT_TASK_TIMEOUT,
                          recovery_timeout=DEFAULT_RECOVERY_TIMEOUT,
                          clock=time.monotonic,
                          logger=None,
                          only_titles=None):
    """逐个发现并完成当前列表里的安全浏览任务（多任务泛化入口）。

    定位阶段用总 deadline（run_deadline）；定位成功后创建受总 deadline 限制的
    子 deadline（``run_deadline.child(task_timeout, "task")``）交给单任务执行。
    捕获 DeadlineExceeded 后立刻冻结新任务/新策略动作，只允许有界调用
    ``back_to_task_list_ocr()``（独立 recovery deadline，默认 10 秒）；无论恢复
    结果如何都返回 reason 为 run_timeout / task_timeout 的 TIMED_OUT outcome。
    传入 logger 时，每个任务定位成功 emit ``task_started``、执行后 emit
    ``task_finished``（task_key 为注册表键，status 为 completed /
    likely_completed / unfinished 的稳定标识）。
    """
    screen = d.window_size()
    attempted = set()
    done, likely_done, unfinished = [], [], []
    try:
        for _ in range(max_tasks):
            _checkpoint(run_deadline)
            target = locate_safe_browse_target(
                d, reader, screen, only_titles=only_titles,
                exclude_titles=tuple(attempted),
                deadline=run_deadline,
            )
            if target is None:
                break
            attempted.add(target.title)
            label = safe_task_label(target.title)
            print(f"发现安全浏览任务：{label} {target.progress}/{target.total}")
            task_log_key = _task_log_key(target.title)
            if logger is not None and task_log_key is not None:
                logger.emit("task_started", task_key=task_log_key, reason="located")
            task_deadline = (
                run_deadline.child(task_timeout, "task")
                if run_deadline is not None else None
            )
            result, browsed = run_one_safe_browse_task(
                d, reader, target.title, target.total, deadline=task_deadline,
                logger=logger,
            )
            if result.reason in {"device_io_error", "unsafe_package", "unsafe_screen"}:
                unfinished.append(f"{label}({result.reason})")
                _emit_task_finished(logger, task_log_key, "unfinished", result.reason)
                break
            if result.completed:
                done.append(label)
                _emit_task_finished(logger, task_log_key, "completed", result.reason)
                continue
            if result.reason == "progress_reset":
                likely_done.append(label)   # 进度回落=完成后重置，很可能已完成
                task_status = "likely_completed"
            elif result.reason == "task_rotated_after_refresh" and browsed:
                likely_done.append(label)
                task_status = "likely_completed"
            elif (
                result.reason == "missing_progress"
                and browsed
                and target.total == 1
            ):
                # 与单任务上报口径一致：已浏览过但完成后行消失/变已完成读不到进度
                likely_done.append(label)
                task_status = "likely_completed"
            elif (result.reason == "task_row_unobserved" and browsed
                  and getattr(result, "progress", 0) > 0):
                # 已浏览+返回后行消失+读到过进度：完成特征（看看#真机证实），
                # 与 missing_progress+browsed 同口径归类"很可能完成"
                likely_done.append(label)
                task_status = "likely_completed"
            elif (result.reason == "progress_total_mismatch" and browsed
                  and getattr(result, "progress", 0) > 0):
                # 浏览后读进度 total 不匹配 = 展示滞后（计数延迟刷新，真机
                # 证实：浏览徽标计时到账但任务行计数数分钟后才更新）。
                # 已浏览且读到过进度：与 task_row_unobserved 同口径"很可能完成"。
                likely_done.append(label)
                task_status = "likely_completed"
            else:
                unfinished.append(f"{label}({result.reason})")
                task_status = "unfinished"
            _emit_task_finished(logger, task_log_key, task_status, result.reason)
            # 单任务边界：任何非 completed 结果都必须先人工核对，
            # 不能因为任务行消失、OCR 漏读或“很可能完成”而继续点击下一项。
            break
        # 正常收尾：尽力退出到淘金币首页（赚更多金币界面），便于刷新后续任务
        _settle_back_to_coin_page(d, reader, run_deadline)
    except DeadlineExceeded as error:
        scope = getattr(error, "scope", "run") or "run"
        if scope not in ("run", "task"):
            scope = "run"
        reason = f"{scope}_timeout"
        recovered = False
        if run_deadline is not None:
            try:
                recovery = Deadline.after(
                    recovery_timeout,
                    "recovery",
                    clock=run_deadline.clock,
                    sleeper=run_deadline.sleeper,
                )
                recovered = back_to_task_list_ocr(
                    d, reader, deadline=recovery,
                )
            except Exception:
                recovered = False
        print(f"运行超时（{reason}），"
              f"{'已安全返回任务列表' if recovered else '安全返回任务列表失败'}")
        counts = RunCounts(
            detected=len(attempted),
            supported=len(attempted),
            attempted=len(attempted),
            completed=len(done),
            likely_completed=len(likely_done),
            unfinished=len(unfinished),
        )
        return RunOutcome(RunMode.EXECUTE, RunStatus.TIMED_OUT, reason, counts)
    print(f"安全浏览任务汇总：已确认完成 {done or '无'}；"
          f"很可能完成(请核对余额) {likely_done or '无'}；"
          f"未完成 {unfinished or '无'}")
    return done, likely_done, unfinished


def _dry_run_no_candidates(logger, mode):
    """空屏/无可报告候选：正常空结果，退出 0。"""
    return RunOutcome(mode, RunStatus.SUCCESS, "no_candidates", RunCounts())


def _dry_run_scan(device, reader, deadline, logger, mode):
    """单屏只读扫描：一次截图 + OCR，行判定后逐项 emit，绝不调用动作方法。"""
    try:
        spans = ocr_screen(device, reader)
    except Exception:
        return RunOutcome(mode, RunStatus.STARTUP_FAILED, "ocr_unavailable")
    deadline.checkpoint()
    if spans is None:
        return RunOutcome(mode, RunStatus.STARTUP_FAILED, "ocr_unavailable")
    if current_package(device) != TB_APP:
        return RunOutcome(mode, RunStatus.STARTUP_FAILED, "not_in_taobao")
    if ocr_has_risk(spans):
        return RunOutcome(mode, RunStatus.SAFETY_STOPPED, "unsafe_marker")
    if not spans:
        return _dry_run_no_candidates(logger, mode)
    if not any(LIST_ANCHOR in span.text for span in spans):
        return RunOutcome(mode, RunStatus.STARTUP_FAILED, "list_anchor_missing")
    try:
        screen = device.window_size()
        decisions = inspect_visible_task_rows(spans, screen)
    except Exception:
        return RunOutcome(mode, RunStatus.STARTUP_FAILED, "row_inspection_failed")
    deadline.checkpoint()
    if not decisions:
        return _dry_run_no_candidates(logger, mode)
    detected = supported = skipped = 0
    for decision in decisions:
        item_supported = 1 if decision.status == "supported" else 0
        item_skipped = 1 if decision.status == "skipped" else 0
        logger.emit(
            "dry_run_row_decided",
            task_key=_DRY_RUN_LOG_KEY.get(decision.task_key),
            phase="scan",
            status=decision.status,
            reason=decision.reason,
            counts=RunCounts(
                detected=1,
                supported=item_supported,
                skipped=item_skipped,
            ),
        )
        detected += 1
        supported += item_supported
        skipped += item_skipped
    return RunOutcome(
        mode,
        RunStatus.SUCCESS,
        "completed",
        RunCounts(detected=detected, supported=supported, skipped=skipped),
    )


def _recover_after_stop(device, reader, recovery_timeout, clock, sleeper):
    """Ctrl+C 后的安全恢复：仅允许回列表检查与返回动作（独立 recovery deadline）。

    恢复期间再次捕获 KeyboardInterrupt（第二次 Ctrl+C）立即返回 False，
    不再读屏、不再做任何设备动作。恢复超时/设备异常同样失败关闭。
    """
    try:
        recovery = Deadline.after(
            recovery_timeout,
            "recovery",
            clock=clock,
            sleeper=sleeper,
        )
        back_to_task_list_ocr(device, reader, deadline=recovery)
    except KeyboardInterrupt:
        return False
    except Exception:
        return False
    return True


def _execute_scan(device, reader, max_tasks, logger, mode, task_key=None,
                  run_deadline=None,
                  task_timeout=DEFAULT_TASK_TIMEOUT,
                  recovery_timeout=DEFAULT_RECOVERY_TIMEOUT):
    """execute 路径：任务列表检查 + 限时安全浏览（Task 6 起接入 deadline）。

    首次 KeyboardInterrupt 停止新动作，只进入 _recover_after_stop() 尝试回列表；
    恢复中第二次中止由该函数内部捕获并立即终止（不再调用设备）。
    """
    clock = getattr(run_deadline, "clock", time.monotonic)
    sleeper = getattr(run_deadline, "sleeper", time.sleep)
    try:
        _checkpoint(run_deadline)
        on_list = on_task_list(device, reader)
        _checkpoint(run_deadline)
        if not on_list:
            screen = device.window_size()
            # 1) 尝试从淘宝首页自动导航到淘金币根页（点"领淘金币"图标）
            if _navigate_home_to_coin_page(
                device, reader, screen, deadline=run_deadline
            ):
                # 已在淘金币根页：直接尝试打开弹窗
                if not _reopen_task_popup(
                    device, reader, screen, deadline=run_deadline
                ):
                    return RunOutcome(
                        mode, RunStatus.STARTUP_FAILED, "list_anchor_missing"
                    )
            elif not _reopen_task_popup(
                device, reader, screen, deadline=run_deadline
            ):
                # 2) 已在淘金币根页（_reopen_task_popup 失败可能因为按钮抖动）
                # 走页面感知回退；只在已知流程页按返回，未知页面零动作
                if not _safe_back_to_coin_page(
                    device, reader, deadline=run_deadline,
                    require_action=False,
                ):
                    _emit_recovery_diagnostic(
                        device, reader, logger, "entry_walk_back_failed"
                    )
                    return RunOutcome(
                        mode, RunStatus.STARTUP_FAILED, "list_anchor_missing"
                    )
                if not _reopen_task_popup(
                    device, reader, screen, deadline=run_deadline
                ):
                    return RunOutcome(
                        mode, RunStatus.STARTUP_FAILED, "list_anchor_missing"
                    )
    except DeadlineExceeded:
        raise   # 统一由 run_ocr_entry 映射为 TIMED_OUT
    except KeyboardInterrupt:
        _recover_after_stop(device, reader, recovery_timeout, clock, sleeper)
        return RunOutcome(mode, RunStatus.CANCELLED, "interrupted")
    except Exception:
        return RunOutcome(mode, RunStatus.STARTUP_FAILED, "task_list_check_failed")
    try:
        result = run_safe_browse_tasks(
            device,
            reader,
            max_tasks=max_tasks,
            run_deadline=run_deadline,
            task_timeout=task_timeout,
            recovery_timeout=recovery_timeout,
            logger=logger,
            only_titles=titles_for_task_key(task_key),
        )
    except DeadlineExceeded:
        raise   # run_safe_browse_tasks 内部已完成恢复；统一映射为 TIMED_OUT
    except KeyboardInterrupt:
        _recover_after_stop(device, reader, recovery_timeout, clock, sleeper)
        return RunOutcome(mode, RunStatus.CANCELLED, "interrupted")
    except Exception:
        return RunOutcome(mode, RunStatus.STARTUP_FAILED, "task_execution_failed")
    if isinstance(result, RunOutcome):
        return result   # 超时 outcome 已带 counts
    done, likely_done, unfinished = result
    counts = RunCounts(
        detected=len(done) + len(likely_done) + len(unfinished),
        supported=len(done) + len(likely_done) + len(unfinished),
        attempted=len(done) + len(likely_done) + len(unfinished),
        completed=len(done),
        likely_completed=len(likely_done),
        unfinished=len(unfinished),
    )
    fatal_reasons = ("(device_io_error)", "(unsafe_package)", "(unsafe_screen)")
    if any(item.endswith(fatal_reasons) for item in unfinished):
        return RunOutcome(mode, RunStatus.SAFETY_STOPPED, "fatal_stop", counts)
    if unfinished:
        return RunOutcome(mode, RunStatus.PARTIAL, "incomplete", counts)
    return RunOutcome(mode, RunStatus.SUCCESS, "completed", counts)


def resolve_reader_factory(reader_factory, sidecar_port):
    """reader 选择：显式注入优先；配置 sidecar 端口则连服务；否则 None。"""
    if reader_factory is not None:
        return reader_factory
    if sidecar_port:
        return make_sidecar_reader_factory(sidecar_port)
    return None


def _run_entry(mode, dry_run, max_tasks, dry_run_timeout, run_timeout,
               task_timeout, recovery_timeout, task_key,
               serial, use_gpu, connect, reader_factory, logger, clock, sleeper,
               ocr_sidecar_port=0):
    """连接、OCR 初始化与按模式分派；所有异常只映射稳定 reason。"""
    deadline = Deadline.after(
        dry_run_timeout if dry_run else run_timeout,
        scope="dry_run" if dry_run else "run",
        clock=clock,
        sleeper=sleeper,
    )
    if connect is None:
        import uiautomator2 as u2

        connect = u2.connect
    resolved_serial = resolve_device_serial(serial)
    if not resolved_serial:
        # 公开仓库不保存私人设备默认值：序列号缺失时安全失败，
        # 不尝试连接空设备（避免隐式连上唯一的在线设备）。
        return RunOutcome(
            mode, RunStatus.STARTUP_FAILED, "device_serial_missing"
        )
    try:
        device = connect(resolved_serial)
    except Exception:
        return RunOutcome(mode, RunStatus.STARTUP_FAILED, "device_connect_failed")
    reader_factory = resolve_reader_factory(reader_factory, ocr_sidecar_port)
    if reader_factory is None:
        import easyocr

        reader_factory = easyocr.Reader
    try:
        ocr_reader = reader_factory(
            ["ch_sim", "en"],
            gpu=resolve_ocr_gpu(use_gpu),
        )
    except Exception:
        return RunOutcome(mode, RunStatus.STARTUP_FAILED, "ocr_initialization_failed")
    if dry_run:
        return _dry_run_scan(device, ocr_reader, deadline, logger, mode)
    return _execute_scan(
        device,
        ocr_reader,
        max_tasks,
        logger,
        mode,
        task_key=task_key,
        run_deadline=deadline,
        task_timeout=task_timeout,
        recovery_timeout=recovery_timeout,
    )


def run_ocr_entry(
    serial=None,
    use_gpu=None,
    max_tasks=1,
    dry_run=False,
    ocr_sidecar_port=0,
    dry_run_timeout=DEFAULT_DRY_RUN_TIMEOUT,
    task_timeout=DEFAULT_TASK_TIMEOUT,
    run_timeout=DEFAULT_RUN_TIMEOUT,
    recovery_timeout=DEFAULT_RECOVERY_TIMEOUT,
    task_key=None,
    connect=None,
    reader_factory=None,
    logger_factory=create_runtime_logger,
    log_dir="logs",
    clock=time.monotonic,
    sleeper=time.sleep,
):
    """Connect and run the OCR entry point with injectable offline boundaries.

    顺序：确定 RunMode → 创建 logger → 创建总/dry deadline → 连接设备 →
    初始化 OCR → 单屏扫描 → emit 每项与汇总 → 返回 RunOutcome → finally 关闭 logger。
    dry-run 只做只读扫描；日志初始化失败在连接前以退出码 3 停止。
    所有结束路径（含超时与 Ctrl+C）都只 emit 一次 run_finished 并关闭 logger；
    logger 写入/关闭失败不覆盖原始 outcome/退出码，也不输出异常正文。
    """
    mode = RunMode.DRY_RUN if dry_run else RunMode.EXECUTE
    logger = None
    try:
        logger = logger_factory(log_dir, mode)
    except Exception:
        return RunOutcome(mode, RunStatus.STARTUP_FAILED, "log_initialization_failed")
    outcome = None
    try:
        logger.emit("run_started", reason="started")
        outcome = _run_entry(
            mode=mode,
            dry_run=dry_run,
            max_tasks=max_tasks,
            dry_run_timeout=dry_run_timeout,
            run_timeout=run_timeout,
            task_timeout=task_timeout,
            recovery_timeout=recovery_timeout,
            task_key=task_key,
            serial=serial,
            use_gpu=use_gpu,
            connect=connect,
            reader_factory=reader_factory,
            ocr_sidecar_port=ocr_sidecar_port,
            logger=logger,
            clock=clock,
            sleeper=sleeper,
        )
    except DeadlineExceeded as error:
        scope = getattr(error, "scope", "run") or "run"
        outcome = RunOutcome(mode, RunStatus.TIMED_OUT, f"{scope}_timeout")
    except KeyboardInterrupt:
        outcome = RunOutcome(mode, RunStatus.CANCELLED, "interrupted")
    finally:
        if logger is not None:
            if outcome is not None:
                try:
                    logger.emit(
                        "run_finished",
                        status=outcome.status.value,
                        reason=outcome.reason,
                        counts=outcome.counts,
                    )
                except Exception:
                    pass   # 日志写入失败不覆盖原始 outcome/退出码
            try:
                logger.close()
            except Exception:
                pass   # 关闭失败不覆盖原始 outcome/退出码、不输出异常正文
    return outcome


def _spawn_watch_panel(enabled=True):
    """Windows 下启动轻量进度面板进程；非 Windows 或禁用时返回 None。

    面板以独立控制台窗口运行 ``python -m taojinbi_mav.runtime.watch --auto-exit``，
    主进程退出后它仍可继续显示，并在 run_finished 后 3 秒自动关闭。
    """
    if not enabled or os.name != "nt":
        return None
    import subprocess
    env = dict(os.environ)
    existing = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = str(_SRC) + (os.pathsep + existing if existing else "")
    return subprocess.Popen(
        [sys.executable, "-m", "taojinbi_mav.runtime.watch", "--auto-exit"],
        creationflags=getattr(subprocess, "CREATE_NEW_CONSOLE", 0),
        env=env,
    )


def main(argv=None):
    args = build_ocr_arg_parser().parse_args(argv)
    _spawn_watch_panel(enabled=args.watch)
    outcome = run_ocr_entry(
        serial=args.serial,
        use_gpu=args.gpu,
        max_tasks=args.max_tasks,
        ocr_sidecar_port=args.ocr_sidecar_port,
        dry_run=args.dry_run,
        dry_run_timeout=args.dry_run_timeout,
        task_timeout=args.task_timeout,
        run_timeout=args.run_timeout,
        recovery_timeout=args.recovery_timeout,
        task_key=args.task,
    )
    return int(outcome.exit_code)


if __name__ == "__main__":
    raise SystemExit(main())
