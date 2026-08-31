import json
import random
import importlib
import importlib.util
import unittest
from unittest.mock import patch

from taojinbi_mav.ocr_ui import OcrSpan
from taojinbi_mav.runtime.deadline import DeadlineExceeded


MODULE_NAME = "taojinbi_mav.task_strategies"
MODULE_SPEC = importlib.util.find_spec(MODULE_NAME)
strategies = importlib.import_module(MODULE_NAME) if MODULE_SPEC else None


class StrategyContractTests(unittest.TestCase):
    def test_strategy_module_exposes_fail_closed_contract(self):
        self.assertIsNotNone(strategies)
        context = strategies.StrategyContext(
            device=object(),
            reader=None,
            screen=(1080, 1920),
            read_screen=lambda: [],
            screen_is_safe=lambda _spans: True,
            package_is_safe=lambda: True,
            safe_tap=lambda _center: True,
        )
        result = strategies.execute_task_strategy("unknown", context, 1)
        self.assertEqual(
            result,
            strategies.StrategyResult(False, "unknown_strategy"),
        )

    def test_strategy_selection_accepts_only_registered_profiles(self):
        from taojinbi_mav.tasks.registry import profile_for_title

        search = profile_for_title("搜一搜你心仪的宝贝")
        featured = profile_for_title("发现精选好物")
        self.assertEqual(
            strategies.select_task_strategy(search),
            strategies.SEARCH_DISCOVERY_BROWSE,
        )
        self.assertEqual(
            strategies.select_task_strategy(featured),
            strategies.FEED_BROWSE,
        )
        self.assertIsNone(strategies.select_task_strategy(None))
        self.assertIsNone(
            strategies.select_task_strategy("任意未注册标题")
        )


class GestureDevice:
    def __init__(self):
        self.swipes = []
        self.backs = []

    def swipe(self, *args):
        self.swipes.append(args)

    def click(self, *_args):
        raise AssertionError("feed browse must not click page content")


class FeedBrowseStrategyTests(unittest.TestCase):
    def _context(self, device, package_is_safe=lambda: True):
        return strategies.StrategyContext(
            device=device,
            reader=None,
            screen=(1080, 1920),
            read_screen=lambda: [],
            screen_is_safe=lambda _spans: True,
            package_is_safe=package_is_safe,
            safe_tap=lambda _center: self.fail("feed browse must not tap"),
        )

    def test_feed_strategy_performs_exact_bounded_up_swipes_without_clicks(self):
        device = GestureDevice()
        with patch.object(strategies.time, "sleep"):
            result = strategies.execute_task_strategy(
                strategies.FEED_BROWSE, self._context(device), 3
            )
        self.assertEqual(result, strategies.StrategyResult(True))
        self.assertEqual(len(device.swipes), 3)
        self.assertTrue(all(swipe[1] > swipe[3] for swipe in device.swipes))

    def test_feed_strategy_stops_before_next_swipe_when_package_is_unsafe(self):
        device = GestureDevice()
        checks = iter([True, False])
        with patch.object(strategies.time, "sleep"):
            result = strategies.execute_task_strategy(
                strategies.FEED_BROWSE,
                self._context(device, lambda: next(checks)),
                3,
            )
        self.assertEqual(result.reason, "unsafe_package")
        self.assertEqual(len(device.swipes), 1)

    def test_feed_strategy_follows_badge_until_confirmed_absent(self):
        """feed 徽标驱动：徽标可见时持续停留滑动，连续消失 2 次即收。

        真机 2026-08-29：看看#内容页同样挂“浏览10秒可领”徽标（要求逐周期
        变化），固定停留 10 秒可能不达标。
        """
        device = GestureDevice()
        badge_feed = [
            OcrSpan("浏览10秒可领", 0.97, (548, 103), (548, 90, 880, 130))
        ]
        plain_feed = [
            OcrSpan("七天退换", 0.9, (710, 998), (650, 980, 780, 1010))
        ]
        screens = iter([badge_feed] * 3 + [plain_feed] * 2)
        context = strategies.StrategyContext(
            device=device,
            reader=None,
            screen=(1080, 1920),
            read_screen=lambda: next(screens),
            screen_is_safe=lambda spans: bool(spans),
            package_is_safe=lambda: True,
            safe_tap=lambda _center: self.fail("feed browse must not tap"),
        )
        with patch.object(strategies.time, "sleep"), patch("builtins.print") as output:
            result = strategies.execute_task_strategy(
                strategies.FEED_BROWSE, context, 2
            )
        self.assertEqual(result, strategies.StrategyResult(True))
        self.assertEqual(len(device.swipes), 5)  # 徽标 3 轮 + 消失确认 2 轮
        printed = " ".join(str(call) for call in output.call_args_list)
        self.assertIn("浏览要求 10 秒", printed)

    def test_feed_strategy_fallback_when_badge_never_seen(self):
        """徽标从未出现：按 browse_count 原行为停留滑动。"""
        device = GestureDevice()
        plain_feed = [
            OcrSpan("七天退换", 0.9, (710, 998), (650, 980, 780, 1010))
        ]
        context = strategies.StrategyContext(
            device=device,
            reader=None,
            screen=(1080, 1920),
            read_screen=lambda: plain_feed,
            screen_is_safe=lambda spans: bool(spans),
            package_is_safe=lambda: True,
            safe_tap=lambda _center: self.fail("feed browse must not tap"),
        )
        with patch.object(strategies.time, "sleep"):
            result = strategies.execute_task_strategy(
                strategies.FEED_BROWSE, context, 2
            )
        self.assertEqual(result, strategies.StrategyResult(True))
        self.assertEqual(len(device.swipes), 2)

    def test_feed_strategy_caps_when_badge_persists(self):
        """徽标一直在也必须收：FEED_BADGE_MAX_CYCLES 上限。"""
        device = GestureDevice()
        badge_feed = [
            OcrSpan("浏览10秒可领", 0.97, (548, 103), (548, 90, 880, 130))
        ]
        context = strategies.StrategyContext(
            device=device,
            reader=None,
            screen=(1080, 1920),
            read_screen=lambda: badge_feed,
            screen_is_safe=lambda spans: bool(spans),
            package_is_safe=lambda: True,
            safe_tap=lambda _center: self.fail("feed browse must not tap"),
        )
        with patch.object(strategies.time, "sleep"):
            result = strategies.execute_task_strategy(
                strategies.FEED_BROWSE, context, 2
            )
        self.assertEqual(result, strategies.StrategyResult(True))
        self.assertEqual(
            len(device.swipes), strategies.FEED_BADGE_MAX_CYCLES
        )

    def test_strategy_uses_context_sleeper_not_module_sleep(self):
        device = GestureDevice()
        context = strategies.StrategyContext(
            device=device,
            reader=None,
            screen=(1080, 1920),
            read_screen=lambda: [],
            screen_is_safe=lambda _spans: True,
            package_is_safe=lambda: True,
            safe_tap=lambda _center: True,
            sleep=lambda _seconds: None,
        )
        with patch.object(
            strategies.time,
            "sleep",
            side_effect=AssertionError("strategy must not sleep via module"),
        ):
            result = strategies.execute_task_strategy(
                strategies.FEED_BROWSE, context, 1
            )
        self.assertEqual(result, strategies.StrategyResult(True))
        self.assertEqual(len(device.swipes), 1)

    def test_checkpoint_deadline_prevents_device_actions(self):
        device = GestureDevice()

        def stop():
            raise DeadlineExceeded("task")

        context = strategies.StrategyContext(
            device=device,
            reader=None,
            screen=(1080, 1920),
            read_screen=lambda: [],
            screen_is_safe=lambda _spans: True,
            package_is_safe=lambda: True,
            safe_tap=lambda _center: True,
            checkpoint=stop,
            sleep=lambda _seconds: None,
        )
        with self.assertRaises(DeadlineExceeded):
            strategies.execute_task_strategy(
                strategies.FEED_BROWSE, context, 1
            )
        self.assertEqual(len(device.swipes), 0)


class SwipeJitterTests(unittest.TestCase):
    """滑动几何/时长轻微抖动：拟人节奏，方向交替与屏幕边界保持不变。"""

    def _context(self, device):
        return strategies.StrategyContext(
            device=device,
            reader=None,
            screen=(1080, 1920),
            read_screen=lambda: [],
            screen_is_safe=lambda spans: True,
            package_is_safe=lambda: True,
            safe_tap=lambda _center: True,
        )

    def test_consecutive_feed_swipes_vary_in_geometry(self):
        device = GestureDevice()
        random.seed(20260829)
        try:
            with patch.object(strategies.time, "sleep"):
                strategies.execute_task_strategy(
                    strategies.FEED_BROWSE, self._context(device), 5
                )
        finally:
            random.seed()
        xs = [s[0] for s in device.swipes]
        durations = [s[4] for s in device.swipes]
        self.assertEqual(len(set(xs)), 5)          # 每次横向位置都不同
        self.assertEqual(len(set(durations)), 5)   # 每次时长都不同
        # 信息流保持只向上滑（拟人浏览方向）
        self.assertTrue(all(s[1] > s[3] for s in device.swipes))

    def test_search_swipes_alternate_direction_with_jitter(self):
        device = GestureDevice()
        random.seed(99)
        try:
            with patch.object(strategies.time, "sleep"):
                strategies._scroll_search_results(self._context(device))
        finally:
            random.seed()
        self.assertGreaterEqual(len(device.swipes), strategies.SEARCH_SCROLLS)
        ups = [s for s in device.swipes if s[1] > s[3]]
        downs = [s for s in device.swipes if s[1] < s[3]]
        self.assertGreaterEqual(len(ups), 1)
        self.assertGreaterEqual(len(downs), 1)

    def test_swipe_stays_within_screen_bounds(self):
        device = GestureDevice()
        random.seed(7)
        try:
            with patch.object(strategies.time, "sleep"):
                strategies.execute_task_strategy(
                    strategies.FEED_BROWSE, self._context(device), 8
                )
        finally:
            random.seed()
        for s in device.swipes:
            self.assertTrue(0 <= s[0] <= 1080, s)
            self.assertTrue(0 <= s[1] <= 1920 and 0 <= s[3] <= 1920, s)
            self.assertGreaterEqual(s[4], 0.25, s)


class SearchBrowseStrategyTests(unittest.TestCase):
    def test_search_strategy_taps_one_keyword_per_round(self):
        device = GestureDevice()
        taps = []
        entry = [
            OcrSpan("搜索发现", 0.99, (200, 500), (100, 480, 300, 520)),
            OcrSpan("鱼油推荐", 0.99, (300, 700), (200, 680, 400, 720)),
        ]
        result_feed = [
            OcrSpan("金币可领取", 0.99, (300, 600), (100, 580, 500, 620))
        ]
        # 每词帧数（入口判定 + 第二帧确认 + 等结果 + 15 次保底滑动检查）：
        # 词1: entry, entry, feed×16（徽标不可读 → SEARCH_SCROLLS 保底 15 滑）
        screens = iter([entry, entry] + [result_feed] * 16)
        context = strategies.StrategyContext(
            device=device,
            reader=None,
            screen=(1080, 1920),
            read_screen=lambda: next(screens),
            screen_is_safe=lambda spans: bool(spans),
            package_is_safe=lambda: True,
            safe_tap=lambda center: taps.append(center) or True,
            back=lambda: device.backs.append("back"),
        )
        with patch.object(
            strategies.random,
            "choice",
            side_effect=lambda items: items[0],
        ), patch.object(strategies.time, "sleep"), patch("builtins.print") as output:
            result = strategies.execute_task_strategy(
                strategies.SEARCH_DISCOVERY_BROWSE, context, 6
            )
        self.assertEqual(result, strategies.StrategyResult(True))
        self.assertEqual(taps, [(300, 700)])
        self.assertEqual(
            len(device.swipes), strategies.SEARCH_SCROLLS
        )  # 徽标不可读时按保底次数滑动
        self.assertEqual(len(device.backs), 0)
        # 保底滑 = 上、下交替 → 多滑一次上滑
        self.assertEqual(
            sum(1 for swipe in device.swipes if swipe[1] > swipe[3]),
            strategies.SEARCH_SCROLLS // 2 + 1,
        )
        self.assertEqual(
            sum(1 for swipe in device.swipes if swipe[1] < swipe[3]),
            strategies.SEARCH_SCROLLS // 2,
        )
        self.assertEqual(output.call_count, 1)
        output.assert_any_call("搜一搜：第二帧确认通过，点击搜索发现关键词")

    def test_scroll_follows_badge_until_confirmed_absent(self):
        """徽标“浏览N秒可领”可见时持续滑动，连续消失 2 次即收（计时已满足）。"""
        device = GestureDevice()
        taps = []
        entry = [
            OcrSpan("搜索发现", 0.99, (200, 500), (100, 480, 300, 520)),
            OcrSpan("鱼油推荐", 0.99, (300, 700), (200, 680, 400, 720)),
        ]
        badge_feed = [
            OcrSpan("浏览25秒可领", 0.94, (548, 103), (548, 90, 880, 130))
        ]
        plain_feed = [
            OcrSpan("商品结果标题", 0.99, (300, 600), (100, 580, 500, 620))
        ]
        screens = iter([entry, entry] + [badge_feed] * 4 + [plain_feed] * 2)
        context = strategies.StrategyContext(
            device=device,
            reader=None,
            screen=(1080, 1920),
            read_screen=lambda: next(screens),
            screen_is_safe=lambda spans: bool(spans),
            package_is_safe=lambda: True,
            safe_tap=lambda center: taps.append(center) or True,
            back=lambda: None,
        )
        with patch.object(
            strategies.random,
            "choice",
            side_effect=lambda items: items[0],
        ), patch.object(strategies.time, "sleep"), patch("builtins.print") as output:
            result = strategies.execute_task_strategy(
                strategies.SEARCH_DISCOVERY_BROWSE, context, 1
            )
        self.assertEqual(result, strategies.StrategyResult(True))
        self.assertEqual(len(device.swipes), 5)  # 徽标 3 轮 + 消失确认 2 轮
        printed = " ".join(str(call) for call in output.call_args_list)
        self.assertIn("浏览要求 25 秒", printed)

    def test_scroll_caps_when_badge_persists(self):
        """徽标一直在也必须收：滑动上限 SEARCH_BADGE_MAX_SCROLLS。"""
        device = GestureDevice()
        entry = [
            OcrSpan("搜索发现", 0.99, (200, 500), (100, 480, 300, 520)),
            OcrSpan("鱼油推荐", 0.99, (300, 700), (200, 680, 400, 720)),
        ]
        badge_feed = [
            OcrSpan("浏览25秒可领", 0.94, (548, 103), (548, 90, 880, 130))
        ]
        screens = iter([entry, entry] + [badge_feed] * 60)
        context = strategies.StrategyContext(
            device=device,
            reader=None,
            screen=(1080, 1920),
            read_screen=lambda: next(screens),
            screen_is_safe=lambda spans: bool(spans),
            package_is_safe=lambda: True,
            safe_tap=lambda _center: True,
            back=lambda: None,
        )
        with patch.object(
            strategies.random,
            "choice",
            side_effect=lambda items: items[0],
        ), patch.object(strategies.time, "sleep"):
            result = strategies.execute_task_strategy(
                strategies.SEARCH_DISCOVERY_BROWSE, context, 1
            )
        self.assertEqual(result, strategies.StrategyResult(True))
        self.assertEqual(
            len(device.swipes), strategies.SEARCH_BADGE_MAX_SCROLLS
        )

    def test_wait_for_search_result_explores_after_static_polls_fail(self):
        """静等 4 帧未命中（页面加载中）→ 滑动探索第 1 次命中 → 成功。"""
        device = GestureDevice()
        loading_page = [
            OcrSpan("搜索中", 0.9, (300, 600), (100, 580, 500, 620))
        ]
        result_feed = [
            OcrSpan("金币可领取", 0.99, (300, 600), (100, 580, 500, 620))
        ]
        # 4 帧静等全是加载页 → 探索第 1 次滑后出现结果页锚点
        screens = iter([loading_page] * 4 + [result_feed] + [result_feed])
        context = strategies.StrategyContext(
            device=device,
            reader=None,
            screen=(1080, 1920),
            read_screen=lambda: next(screens),
            screen_is_safe=lambda spans: bool(spans),
            package_is_safe=lambda: True,
            safe_tap=lambda _center: True,
            back=lambda: None,
        )
        with patch.object(strategies.time, "sleep"):
            result = strategies._wait_for_search_result(context)
        self.assertEqual(result, strategies.StrategyResult(True))
        self.assertEqual(len(device.swipes), 1)  # 探索只滑 1 次就命中

    def test_wait_for_search_result_explore_fails_returns_unavailable(self):
        """静等 + 探索全部未命中 → search_result_unavailable（不谎报）。"""
        device = GestureDevice()
        loading_page = [
            OcrSpan("搜索中", 0.9, (300, 600), (100, 580, 500, 620))
        ]
        # 4 帧静等 + 2 帧探索（滑 2 次）全部未出现结果页锚点
        screens = iter([loading_page] * 6)
        context = strategies.StrategyContext(
            device=device,
            reader=None,
            screen=(1080, 1920),
            read_screen=lambda: next(screens),
            screen_is_safe=lambda spans: bool(spans),
            package_is_safe=lambda: True,
            safe_tap=lambda _center: True,
            back=lambda: None,
        )
        with patch.object(strategies.time, "sleep"):
            result = strategies._wait_for_search_result(context)
        self.assertEqual(
            result, strategies.StrategyResult(False, "search_result_unavailable")
        )
        self.assertEqual(len(device.swipes), 2)

    def test_wait_for_search_result_explore_swipes_upward_only(self):
        """滑动探索方向固定向上（结果页内容向下滚动，锚点从上方露出）。"""
        device = GestureDevice()
        loading_page = [
            OcrSpan("搜索中", 0.9, (300, 600), (100, 580, 500, 620))
        ]
        screens = iter([loading_page] * 6)
        context = strategies.StrategyContext(
            device=device,
            reader=None,
            screen=(1080, 1920),
            read_screen=lambda: next(screens),
            screen_is_safe=lambda spans: bool(spans),
            package_is_safe=lambda: True,
            safe_tap=lambda _center: True,
            back=lambda: None,
        )
        with patch.object(strategies.time, "sleep"):
            strategies._wait_for_search_result(context)
        for swipe in device.swipes:
            self.assertGreater(swipe[1], swipe[3])  # y 起 > y 止 = 上滑

    def test_entry_page_with_badge_taps_keyword_not_scroll(self):
        """入口页带徽标：必须点关键词进结果页，绝不在入口页空滑。"""
        device = GestureDevice()
        taps = []
        entry_with_badge = [
            OcrSpan("搜索发现", 0.99, (200, 500), (100, 480, 300, 520)),
            OcrSpan("杯子", 0.87, (300, 700), (200, 680, 400, 720)),
            OcrSpan("浏览25秒可领", 0.94, (548, 103), (548, 90, 880, 130)),
            OcrSpan("搜索", 1.0, (887, 283), (850, 260, 930, 300)),
        ]
        result_feed = [
            OcrSpan("浏览25秒可领", 0.94, (548, 103), (548, 90, 880, 130)),
            OcrSpan("金币可领取", 0.99, (300, 600), (100, 580, 500, 620)),
        ]
        result_plain = [
            OcrSpan("金币可领取", 0.99, (300, 600), (100, 580, 500, 620)),
        ]
        # 帧：入口(带徽标) → 第二帧确认 → 等结果 → 4 滑徽标帧 + 2 滑消失确认帧
        screens = iter(
            [entry_with_badge, entry_with_badge]
            + [result_feed] * 4 + [result_plain] * 2
        )
        context = strategies.StrategyContext(
            device=device,
            reader=None,
            screen=(1080, 1920),
            read_screen=lambda: next(screens),
            screen_is_safe=lambda spans: bool(spans),
            package_is_safe=lambda: True,
            safe_tap=lambda center: taps.append(center) or True,
            back=lambda: None,
        )
        with patch.object(
            strategies.random,
            "choice",
            side_effect=lambda items: items[0],
        ), patch.object(strategies.time, "sleep"):
            result = strategies.execute_task_strategy(
                strategies.SEARCH_DISCOVERY_BROWSE, context, 1
            )
        self.assertEqual(result, strategies.StrategyResult(True))
        self.assertEqual(taps, [(300, 700)])  # 点了关键词（标题下方候选）
        self.assertEqual(len(device.swipes), 5)  # 3 徽标帧 + 2 消失确认帧

    def test_entry_failure_emits_page_diagnostic_without_raw_text(self):
        """入口识别失败时发诊断指纹（只含布尔/计数，不含 OCR 原文）。"""
        device = GestureDevice()
        unknown_page = [
            OcrSpan("神秘中间页甲乙丙", 0.99, (300, 600), (100, 580, 500, 620))
        ]
        diagnostics = []
        context = strategies.StrategyContext(
            device=device,
            reader=None,
            screen=(1080, 1920),
            read_screen=lambda: unknown_page,
            screen_is_safe=lambda spans: bool(spans),
            package_is_safe=lambda: True,
            safe_tap=lambda _center: self.fail("must not tap"),
            emit_diagnostic=lambda payload: diagnostics.append(payload),
        )
        with patch.object(strategies.time, "sleep"):
            result = strategies.execute_task_strategy(
                strategies.SEARCH_DISCOVERY_BROWSE, context, 1
            )
        self.assertEqual(result.reason, "search_entry_unavailable")
        self.assertEqual(len(diagnostics), 1)
        payload = diagnostics[0]
        self.assertEqual(payload["reason"], "search_entry_unavailable")
        self.assertEqual(payload["span_count"], 1)
        self.assertFalse(payload["is_entry_page"])
        self.assertNotIn("神秘中间页", json.dumps(payload, ensure_ascii=False))

    def test_search_output_masks_discovery_keyword(self):
        device = GestureDevice()
        taps = []
        entry = [
            OcrSpan("搜索发现", 0.99, (200, 500), (100, 480, 300, 520)),
            OcrSpan("鱼油推荐", 0.99, (300, 700), (200, 680, 400, 720)),
        ]
        result_feed = [
            OcrSpan("金币可领取", 0.99, (300, 600), (100, 580, 500, 620))
        ]
        screens = iter([entry, entry] + [result_feed] * 16)
        context = strategies.StrategyContext(
            device=device,
            reader=None,
            screen=(1080, 1920),
            read_screen=lambda: next(screens),
            screen_is_safe=lambda spans: bool(spans),
            package_is_safe=lambda: True,
            safe_tap=lambda center: taps.append(center) or True,
            back=lambda: None,
        )
        with patch.object(
            strategies.random,
            "choice",
            side_effect=lambda items: items[0],
        ), patch.object(strategies.time, "sleep"), patch(
            "builtins.print"
        ) as output:
            result = strategies.execute_task_strategy(
                strategies.SEARCH_DISCOVERY_BROWSE, context, 1
            )
        self.assertEqual(result, strategies.StrategyResult(True))
        printed = " ".join(
            str(call) for call in output.call_args_list
        )
        self.assertNotIn("鱼油推荐", printed)
        self.assertIn("搜一搜：第二帧确认通过，点击搜索发现关键词", printed)

    def test_search_strategy_fails_closed_without_reliable_candidate(self):
        context = strategies.StrategyContext(
            device=GestureDevice(),
            reader=None,
            screen=(1080, 1920),
            read_screen=lambda: [
                OcrSpan(
                    "搜索发现",
                    0.99,
                    (200, 500),
                    (100, 480, 300, 520),
                )
            ],
            screen_is_safe=lambda spans: bool(spans),
            package_is_safe=lambda: True,
            safe_tap=lambda _center: self.fail(
                "must not tap without candidate"
            ),
        )
        result = strategies.execute_task_strategy(
            strategies.SEARCH_DISCOVERY_BROWSE, context, 1
        )
        self.assertEqual(result.reason, "discovery_candidate_unavailable")

    def test_search_strategy_stops_after_swipe_when_screen_becomes_unsafe(self):
        device = GestureDevice()
        result_feed = [
            OcrSpan(
                "浏览10秒可领金币",
                0.99,
                (300, 300),
                (100, 280, 500, 320),
            )
        ]
        safe_checks = iter([True, False])
        context = strategies.StrategyContext(
            device=device,
            reader=None,
            screen=(1080, 1920),
            read_screen=lambda: result_feed,
            screen_is_safe=lambda _spans: next(safe_checks),
            package_is_safe=lambda: True,
            safe_tap=lambda _center: self.fail("already in result feed"),
        )
        with patch.object(strategies.time, "sleep"):
            result = strategies.execute_task_strategy(
                strategies.SEARCH_DISCOVERY_BROWSE, context, 1
            )
        self.assertEqual(result.reason, "unsafe_screen")
        self.assertEqual(len(device.swipes), 1)
