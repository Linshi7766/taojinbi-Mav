"""淘宝内部纯浏览任务的交互策略；不负责安全白名单或进度状态机。"""

import random
import time
from dataclasses import dataclass
from typing import Callable

from taojinbi_mav.ocr_ui import (
    find_discovery_candidates,
    revalidate_discovery_candidate,
    is_search_entry_page,
    is_search_result_feed,
    page_fingerprint,
    parse_browse_badge,
)


FEED_BROWSE = "feed_browse"
SEARCH_DISCOVERY_BROWSE = "search_discovery_browse"
DWELL_SECONDS = 10
SWIPE_SETTLE = 2
MAX_SEARCH_ATTEMPTS = 4
SEARCH_FEED_POLLS = 4
SEARCH_FEED_EXPLORE_SWIPES = 2   # 静等未命中后轻滑探索次数（"可领"锚点可能在首屏外）
SEARCH_KEYWORDS_PER_ROUND = 1  # 每轮浏览的搜索发现关键词数量（真机：每次点 1 个关键词浏览 14 秒）
SEARCH_SCROLLS = 15            # 徽标不可读时的保底滑动次数（约 30 秒；2026-08-29 实测要求 25 秒）
SEARCH_SCROLL_INTERVAL = 2     # 每次滑动间隔秒数（用户手动节奏：约 2 秒/滑）
SEARCH_BADGE_MAX_SCROLLS = 40  # 徽标可见时的滑动上限（约 80 秒，防页面状态异常时无限滑）
SEARCH_BADGE_ABSENCE_CONFIRMATIONS = 2  # 徽标连续消失次数（确认计时已满足）
FEED_BADGE_MAX_CYCLES = 6      # 信息流徽标可见时的停留上限（约 72 秒，防呆）
FEED_BADGE_ABSENCE_CONFIRMATIONS = 2    # 信息流徽标连续消失次数（确认计时已满足）
SWIPE_X_JITTER_PX = 24                  # 滑动横向抖动像素（拟人节奏，避免固定轨迹）
SWIPE_Y_JITTER_RATIO = 0.02             # 滑动纵向抖动（占屏高比例）
SWIPE_DURATION_JITTER_S = 0.12          # 滑动时长抖动秒数
SWIPE_DURATION_MIN_S = 0.25             # 滑动时长下限


@dataclass(frozen=True)
class StrategyResult:
    ok: bool
    reason: str = ""


def _default_sleep(seconds):
    time.sleep(seconds)


def _noop_checkpoint():
    return None


@dataclass(frozen=True)
class StrategyContext:
    device: object
    reader: object
    screen: tuple[int, int]
    read_screen: Callable[[], object]
    screen_is_safe: Callable[[object], bool]
    package_is_safe: Callable[[], bool]
    safe_tap: Callable[[tuple[int, int]], bool]
    sleep: Callable[[float], None] = _default_sleep
    checkpoint: Callable[[], None] = _noop_checkpoint
    swipe: Callable[..., bool] | None = None
    back: Callable[[], None] | None = None
    emit_diagnostic: Callable[[dict], None] | None = None

    def __post_init__(self):
        # 动作包装器：先 checkpoint 再触碰设备，保证 DeadlineExceeded 能在
        # 任何设备动作之前生效；runtime 覆盖的 sleep/checkpoint/swipe/back
        # 依旧经过同样的检查路径。
        object.__setattr__(self, "_raw_sleep", self.sleep)
        object.__setattr__(
            self,
            "_raw_swipe",
            self.swipe if self.swipe is not None else (
                lambda *args: self.device.swipe(*args)
            ),
        )
        object.__setattr__(
            self,
            "_raw_back",
            self.back if self.back is not None else (
                lambda: self.device.press("back")
            ),
        )
        object.__setattr__(self, "_raw_safe_tap", self.safe_tap)
        object.__setattr__(self, "sleep", self._checked_sleep)
        object.__setattr__(self, "swipe", self._checked_swipe)
        object.__setattr__(self, "back", self._checked_back)
        object.__setattr__(self, "safe_tap", self._checked_safe_tap)

    def _checked_sleep(self, seconds):
        self.checkpoint()
        self._raw_sleep(seconds)
        self.checkpoint()

    def _last_moment_package_safe(self):
        """动作前最后一刻包名确认（Codex 审计 P0-2，2026-09-02）。

        等待/浏览期间前台可能被切到第三方应用；package_is_safe 为
        None（旧调用方未注入）时保持原行为，否则非淘宝立即拒绝。
        """
        if self.package_is_safe is None:
            return True
        return bool(self.package_is_safe())

    def _checked_swipe(self, *args):
        self.checkpoint()
        if not self._last_moment_package_safe():
            return False
        return self._raw_swipe(*args)

    def _checked_back(self):
        self.checkpoint()
        if not self._last_moment_package_safe():
            return False
        return self._raw_back()

    def _checked_safe_tap(self, center):
        self.checkpoint()
        if not self._last_moment_package_safe():
            return False
        return self._raw_safe_tap(center)


def select_task_strategy(profile):
    if profile is None or not hasattr(profile, "strategy"):
        return None
    if profile.strategy in {FEED_BROWSE, SEARCH_DISCOVERY_BROWSE}:
        return profile.strategy
    return None


def _jittered_vertical_swipe(context, up):
    """生成一次带轻微抖动的竖直滑动（坐标/时长均小扰动，拟人轨迹）。"""
    width, height = context.screen
    cx = width / 2 + random.uniform(-SWIPE_X_JITTER_PX, SWIPE_X_JITTER_PX)
    down_y = height * 0.75 + random.uniform(-1, 1) * height * SWIPE_Y_JITTER_RATIO
    up_y = height * 0.30 + random.uniform(-1, 1) * height * SWIPE_Y_JITTER_RATIO
    duration = max(
        SWIPE_DURATION_MIN_S,
        0.4 + random.uniform(-SWIPE_DURATION_JITTER_S, SWIPE_DURATION_JITTER_S),
    )
    if up:
        return (cx, down_y, cx, up_y, duration)
    return (cx, up_y, cx, down_y, duration)


def _execute_feed_browse(context, browse_count):
    """信息流停留+上滑浏览，时长跟随“浏览N秒可领”徽标（2026-08-29 实测看看# 10 秒）。

    - 首次读到徽标：打印要求秒数（监控平台要求时长），停留上限提升为
      FEED_BADGE_MAX_CYCLES；
    - 徽标连续 FEED_BADGE_ABSENCE_CONFIRMATIONS 次消失：计时已满足，立即收；
    - 徽标不可读：按 browse_count 原行为保底。
    """
    width, height = context.screen
    badge_seen = False
    absent_streak = 0
    cycles = 0
    max_cycles = max(0, browse_count)
    while cycles < max_cycles:
        if not context.package_is_safe():
            return StrategyResult(False, "unsafe_package")
        context.sleep(DWELL_SECONDS)
        context.swipe(*_jittered_vertical_swipe(context, up=True))
        context.sleep(SWIPE_SETTLE)
        cycles += 1
        spans = context.read_screen()
        if not context.screen_is_safe(spans):
            return StrategyResult(False, "unsafe_screen")
        required = parse_browse_badge(spans)
        if required is not None:
            absent_streak = 0
            if not badge_seen:
                badge_seen = True
                max_cycles = FEED_BADGE_MAX_CYCLES
                print(f"信息流：浏览要求 {required} 秒（右上角徽标）")
        elif badge_seen:
            absent_streak += 1
            if absent_streak >= FEED_BADGE_ABSENCE_CONFIRMATIONS:
                return StrategyResult(True)
    return StrategyResult(True)


def _wait_for_search_result(context):
    """等待搜索结果页出现（搜索后浏览得币页）。

    阶段 A（静等）：SWIPE_SETTLE 间隔轮询 SEARCH_FEED_POLLS 次读屏，任一帧
    命中即返回；命中判定 is_search_result_feed 要求"可领"奖励条可见且非入口页。

    阶段 B（滑动探索）：静等全部未命中时，轻滑一屏再读
    SEARCH_FEED_EXPLORE_SWIPES 次——"可领"锚点可能在首屏之外（首屏全是
    商品图/加载未完成时 OCR 读不到文本），滑动后出现即命中。两阶段都保持
    fail-closed：unsafe_screen 立即返回，绝不盲滑/盲点。
    """
    for _ in range(SEARCH_FEED_POLLS):
        context.sleep(SWIPE_SETTLE)
        spans = context.read_screen()
        if not context.screen_is_safe(spans):
            return StrategyResult(False, "unsafe_screen")
        if is_search_result_feed(spans):
            return StrategyResult(True)
    width, height = context.screen
    for _ in range(SEARCH_FEED_EXPLORE_SWIPES):
        context.swipe(
            width // 2, int(height * 0.72),
            width // 2, int(height * 0.28),
        )
        context.sleep(SWIPE_SETTLE)
        spans = context.read_screen()
        if not context.screen_is_safe(spans):
            return StrategyResult(False, "unsafe_screen")
        if is_search_result_feed(spans):
            return StrategyResult(True)
    return StrategyResult(False, "search_result_unavailable")


def _scroll_search_results(context):
    """搜索结果页滑动浏览，时长跟随“浏览N秒可领”徽标（2026-08-29 实测要求 25 秒）。

    - 首次读到徽标：打印要求秒数（监控平台要求时长），滑动上限提升为
      SEARCH_BADGE_MAX_SCROLLS；
    - 徽标连续 SEARCH_BADGE_ABSENCE_CONFIRMATIONS 次消失：计时已满足，立即收；
    - 徽标始终不可读（页面变体/OCR 漏读）：按 SEARCH_SCROLLS 次保底（约 30 秒）；
    - 每次滑动后做安全检查，页面异常即 fail-closed。
    """
    width, height = context.screen
    badge_seen = False
    absent_streak = 0
    scrolls = 0
    max_scrolls = SEARCH_SCROLLS
    while scrolls < max_scrolls:
        context.swipe(
            *_jittered_vertical_swipe(context, up=(scrolls % 2 == 0))
        )
        context.sleep(SEARCH_SCROLL_INTERVAL)
        scrolls += 1
        spans = context.read_screen()
        if not context.screen_is_safe(spans):
            return StrategyResult(False, "unsafe_screen")
        required = parse_browse_badge(spans)
        if required is not None:
            absent_streak = 0
            if not badge_seen:
                badge_seen = True
                max_scrolls = SEARCH_BADGE_MAX_SCROLLS
                print(f"搜一搜：浏览要求 {required} 秒（右上角徽标）")
        elif badge_seen:
            absent_streak += 1
            if absent_streak >= SEARCH_BADGE_ABSENCE_CONFIRMATIONS:
                return StrategyResult(True)
    return StrategyResult(True)


def _emit_page_diagnostic(context, spans, reason):
    """失败诊断：发页面识别指纹（无原文）；诊断失败不影响主流程。"""
    emit = getattr(context, "emit_diagnostic", None)
    if emit is None:
        return
    try:
        emit({"reason": reason, **page_fingerprint(spans)})
    except Exception:
        pass


def _browse_one_keyword(context):
    """点击一个搜索发现关键词并浏览其结果页；成功返回 ok，失败返回稳定 reason。"""
    for _ in range(MAX_SEARCH_ATTEMPTS):
        spans = context.read_screen()
        if not context.screen_is_safe(spans):
            return StrategyResult(False, "unsafe_screen")
        if is_search_result_feed(spans):
            return _scroll_search_results(context)
        if not is_search_entry_page(spans):
            _emit_page_diagnostic(context, spans, "search_entry_unavailable")
            return StrategyResult(False, "search_entry_unavailable")
        candidates = find_discovery_candidates(spans, context.screen)
        if not candidates:
            _emit_page_diagnostic(
                context, spans, "discovery_candidate_unavailable"
            )
            return StrategyResult(False, "discovery_candidate_unavailable")
        pick = random.choice(candidates)
        # 第二帧确认（P0-1）：重新读屏重定位，消失/歧义/移位即零点击；
        # 点击必须使用第二帧坐标，不复用第一帧旧坐标。
        fresh_spans = context.read_screen()
        if not context.screen_is_safe(fresh_spans):
            return StrategyResult(False, "unsafe_screen")
        if not is_search_entry_page(fresh_spans):
            _emit_page_diagnostic(
                context, fresh_spans, "search_entry_unavailable"
            )
            return StrategyResult(False, "search_entry_unavailable")
        repick = revalidate_discovery_candidate(
            pick, fresh_spans, context.screen
        )
        if repick is None:
            _emit_page_diagnostic(
                context, fresh_spans, "candidate_revalidate_failed"
            )
            continue
        print("搜一搜：第二帧确认通过，点击搜索发现关键词")
        if not context.safe_tap(repick.center):
            continue
        waited = _wait_for_search_result(context)
        if waited.ok:
            return _scroll_search_results(context)
        if waited.reason == "unsafe_screen":
            return waited
        if waited.reason == "search_result_unavailable":
            _emit_page_diagnostic(
                context, context.read_screen(), "search_result_unavailable"
            )
        context.back()
        context.sleep(SWIPE_SETTLE)
    return StrategyResult(False, "search_result_unavailable")


def _execute_search_discovery_browse(context):
    """浏览多个搜索发现关键词（每轮 SEARCH_KEYWORDS_PER_ROUND 个）。

    每个关键词：点 → 等结果页 → 上滑/下滑浏览 → 返回词列表 → 点下一个；
    最后一个关键词浏览后不主动返回（留给外层收尾）。任一环节失败即返回
    稳定 reason 并停止。
    """
    for index in range(max(1, SEARCH_KEYWORDS_PER_ROUND)):
        result = _browse_one_keyword(context)
        if not result.ok:
            return result
        if index < SEARCH_KEYWORDS_PER_ROUND - 1:
            context.back()
            context.sleep(SWIPE_SETTLE)
    return StrategyResult(True)


def execute_task_strategy(strategy, context, browse_count):
    if strategy == FEED_BROWSE:
        return _execute_feed_browse(context, browse_count)
    if strategy == SEARCH_DISCOVERY_BROWSE:
        return _execute_search_discovery_browse(context)
    return StrategyResult(False, "unknown_strategy")
