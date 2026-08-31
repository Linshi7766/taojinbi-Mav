"""阶段0：OCR 解析层纯函数测试。

夹具建模自真机只读采集的真实 easyocr 输出（“赚金币抵钱”弹窗）：
- “好物沉浸看(0/5)” 置信度 0.99，中心约 (391,1707)
- 同行“去完成” 0.97，中心约 (943,1739)
- 上方还有多行任务各自带一个“去完成”（0/1）
easyocr 原始输出格式：[(bbox_points, text, confidence), ...]，
bbox_points 为 4 个 [x, y] 角点。
"""

import json
import unittest

from taojinbi_mav.task_core import UNSAFE_ACTION_MARKERS
from taojinbi_mav import ocr_ui
from taojinbi_mav.ocr_ui import (
    OcrSpan,
    PROGRESS_RE,
    BrowseTarget,
    find_discovery_candidates,
    find_immersive_progress,
    find_immersive_target,
    find_progress_value,
    find_result_product_candidates,
    find_safe_browse_target,
    find_unique_ocr_span,
    inspect_visible_task_rows,
    is_product_detail_page,
    is_safe_tap_point,
    is_search_entry_page,
    is_search_flow_page,
    is_search_result_feed,
    is_coin_task_product_page,
    page_fingerprint,
    parse_browse_badge,
    locate_by_scroll,
    needs_search_first,
    ocr_has_risk,
    parse_ocr_spans,
    read_safe_browse_progress,
    scroll_to_top_then_find,
    screen_text_signature,
)


SCREEN = (1080, 1920)


def box(cx, cy, w=140, h=40):
    """按中心构造 easyocr 风格的四角点边界框。"""
    left, right = cx - w / 2, cx + w / 2
    top, bottom = cy - h / 2, cy + h / 2
    return [[left, top], [right, top], [right, bottom], [left, bottom]]


# 建模自真机样本：多行任务、多个“去完成”、奖励文本、低置信噪声与空文本
REAL_SAMPLE = [
    (box(434, 728), "快速得大量金币(0/1)", 0.95),
    (box(943, 760), "去完成", 0.97),
    (box(434, 924), "淘金币趣味课堂(0/1)", 0.95),
    (box(943, 957), "去完成", 0.96),
    (box(391, 1707), "好物沉浸看(0/5)", 0.99),
    (box(943, 1739), "去完成", 0.97),
    (box(327, 1770), "浏览 +35", 0.63),
    (box(172, 38), "仅限紧急呼叫", 0.16),   # 低置信噪声
    (box(529, 38), "", 0.75),               # 空文本
]


class ParseOcrSpansTests(unittest.TestCase):
    def test_filters_low_confidence_and_empty_text(self):
        spans = parse_ocr_spans(REAL_SAMPLE, min_confidence=0.5)

        texts = [s.text for s in spans]
        self.assertNotIn("仅限紧急呼叫", texts)  # 0.16 < 0.5 被丢弃
        self.assertNotIn("", texts)              # 空文本被丢弃
        self.assertIn("好物沉浸看(0/5)", texts)

    def test_computes_center_and_bounds(self):
        spans = parse_ocr_spans([(box(391, 1707), "好物沉浸看(0/5)", 0.99)])
        self.assertEqual(len(spans), 1)
        span = spans[0]
        self.assertIsInstance(span, OcrSpan)
        self.assertEqual(span.center, (391, 1707))
        left, top, right, bottom = span.bounds
        self.assertTrue(left < 391 < right and top < 1707 < bottom)


class FindUniqueOcrSpanTests(unittest.TestCase):
    def test_requires_exact_unique_text_and_confidence(self):
        target = OcrSpan(
            "赚更多金币",
            0.99,
            (500, 800),
            (450, 780, 550, 820),
        )
        self.assertIs(find_unique_ocr_span([target], "赚更多金币"), target)
        self.assertIsNone(find_unique_ocr_span([], "赚更多金币"))
        self.assertIsNone(find_unique_ocr_span([target, target], "赚更多金币"))

    def test_rejects_low_confidence_or_partial_text(self):
        low = OcrSpan("赚更多金币", 0.49, (500, 800), (450, 780, 550, 820))
        partial = OcrSpan("赚更多金币任务", 0.99, (500, 800), (450, 780, 550, 820))
        self.assertIsNone(find_unique_ocr_span([low], "赚更多金币"))
        self.assertIsNone(find_unique_ocr_span([partial], "赚更多金币"))


class FindImmersiveTargetTests(unittest.TestCase):
    def test_pairs_action_button_in_same_row(self):
        spans = parse_ocr_spans(REAL_SAMPLE)
        target = find_immersive_target(spans, SCREEN)

        self.assertIsNotNone(target)
        self.assertEqual(target.title, "好物沉浸看")
        self.assertEqual(target.progress_text, "0/5")
        self.assertEqual(target.action_text, "去完成")
        # 必须配对同一行(y≈1739)的按钮，而不是上方任务行的按钮(y=760/957)
        self.assertEqual(target.action_center, (943, 1739))
        self.assertEqual(target.title_center, (391, 1707))

    def test_rejects_near_miss_title(self):
        sample = [
            (box(391, 1707), "好物沉浸看看(0/5)", 0.99),
            (box(943, 1739), "去完成", 0.97),
        ]
        spans = parse_ocr_spans(sample)
        self.assertIsNone(find_immersive_target(spans, SCREEN))

    def test_fails_closed_when_two_actions_in_same_band(self):
        sample = [
            (box(391, 1707), "好物沉浸看(0/5)", 0.99),
            (box(700, 1735), "去完成", 0.97),
            (box(943, 1739), "去完成", 0.96),
        ]
        spans = parse_ocr_spans(sample)
        self.assertIsNone(find_immersive_target(spans, SCREEN))

    def test_fails_closed_when_no_action_in_band(self):
        sample = [
            (box(391, 1707), "好物沉浸看(0/5)", 0.99),
            (box(943, 760), "去完成", 0.97),  # 远离标题行
        ]
        spans = parse_ocr_spans(sample)
        self.assertIsNone(find_immersive_target(spans, SCREEN))


class FindProgressValueTests(unittest.TestCase):
    def test_extracts_unique_progress_over_target(self):
        spans = parse_ocr_spans(REAL_SAMPLE)
        self.assertEqual(find_progress_value(spans, target=5), 0)

    def test_reads_incremented_progress(self):
        spans = parse_ocr_spans([(box(391, 300), "好物沉浸看(3/5)", 0.99)])
        self.assertEqual(find_progress_value(spans, target=5), 3)

    def test_rejects_ambiguous_progress(self):
        spans = parse_ocr_spans([
            (box(391, 300), "3/5", 0.9),
            (box(391, 600), "4/5", 0.9),
        ])
        self.assertIsNone(find_progress_value(spans, target=5))

    def test_ignores_wrong_denominator(self):
        spans = parse_ocr_spans([(box(391, 300), "快速得大量金币(0/1)", 0.95)])
        self.assertIsNone(find_progress_value(spans, target=5))


class OcrHasRiskTests(unittest.TestCase):
    def test_detects_risk_words(self):
        spans = parse_ocr_spans([(box(500, 900), "请完成安全验证", 0.9)])
        self.assertTrue(ocr_has_risk(spans))

    def test_clean_page_has_no_risk(self):
        spans = parse_ocr_spans(REAL_SAMPLE)
        self.assertFalse(ocr_has_risk(spans))


class IsSafeTapPointTests(unittest.TestCase):
    SCREEN = (1080, 1920)

    def test_accepts_valid_action_point(self):
        # 真机实测“去完成”按钮中心
        self.assertTrue(is_safe_tap_point((943, 1739), self.SCREEN))

    def test_rejects_status_bar_region(self):
        self.assertFalse(is_safe_tap_point((943, 30), self.SCREEN))

    def test_rejects_navigation_bar_region(self):
        self.assertFalse(is_safe_tap_point((943, 1900), self.SCREEN))

    def test_rejects_out_of_horizontal_bounds(self):
        self.assertFalse(is_safe_tap_point((-5, 900), self.SCREEN))
        self.assertFalse(is_safe_tap_point((1100, 900), self.SCREEN))


class ScreenTextSignatureTests(unittest.TestCase):
    def test_signature_ignores_order_and_confidence(self):
        a = parse_ocr_spans([(box(100, 100), "甲", 0.9), (box(200, 200), "乙", 0.8)])
        b = parse_ocr_spans([(box(200, 200), "乙", 0.6), (box(100, 100), "甲", 0.99)])
        self.assertEqual(screen_text_signature(a), screen_text_signature(b))

    def test_signature_differs_on_different_texts(self):
        a = parse_ocr_spans([(box(100, 100), "甲", 0.9)])
        b = parse_ocr_spans([(box(100, 100), "丙", 0.9)])
        self.assertNotEqual(screen_text_signature(a), screen_text_signature(b))


class LocateByScrollTests(unittest.TestCase):
    def test_returns_target_without_scrolling_when_found_immediately(self):
        scrolls = {"n": 0}

        def probe():
            return "TARGET", frozenset({"a"})

        def scroll():
            scrolls["n"] += 1

        self.assertEqual(locate_by_scroll(probe, scroll), "TARGET")
        self.assertEqual(scrolls["n"], 0)

    def test_finds_target_after_two_scrolls(self):
        seq = iter([
            (None, frozenset({"a"})),
            (None, frozenset({"b"})),
            ("T", frozenset({"c"})),
        ])
        scrolls = {"n": 0}

        def probe():
            return next(seq)

        def scroll():
            scrolls["n"] += 1

        self.assertEqual(locate_by_scroll(probe, scroll, max_scrolls=8), "T")
        self.assertEqual(scrolls["n"], 2)

    def test_fails_closed_at_bottom_when_screen_unchanged(self):
        seq = iter([
            (None, frozenset({"a"})),
            (None, frozenset({"a"})),
            (None, frozenset({"a"})),
        ])
        scrolls = {"n": 0}

        def probe():
            return next(seq)

        def scroll():
            scrolls["n"] += 1

        self.assertIsNone(locate_by_scroll(probe, scroll, max_scrolls=8))
        self.assertEqual(scrolls["n"], 2)

    def test_does_not_stop_after_one_unchanged_swipe(self):
        # 第一次滑动后 OCR 集合暂时不变，下一屏变化时仍应继续查找。
        seq = iter([
            (None, frozenset({"a"})),
            (None, frozenset({"a"})),
            ("TARGET", frozenset({"b"})),
        ])
        scrolls = {"n": 0}

        def probe():
            return next(seq)

        def scroll():
            scrolls["n"] += 1

        self.assertEqual(locate_by_scroll(probe, scroll, max_scrolls=8), "TARGET")
        self.assertEqual(scrolls["n"], 2)

    def test_fails_closed_when_max_scrolls_exhausted(self):
        counter = {"i": 0}
        scrolls = {"n": 0}

        def probe():
            counter["i"] += 1
            return None, frozenset({str(counter["i"])})

        def scroll():
            scrolls["n"] += 1

        self.assertIsNone(locate_by_scroll(probe, scroll, max_scrolls=3))
        self.assertEqual(counter["i"], 4)   # max_scrolls + 1 次 probe
        self.assertEqual(scrolls["n"], 3)   # 至多 max_scrolls 次滚动


class FindImmersiveProgressTests(unittest.TestCase):
    def test_reads_progress_without_action_button(self):
        # 完成态：只有标题 + 5/5，没有“去完成”按钮，也要能读出 5
        spans = parse_ocr_spans([(box(391, 300), "好物沉浸看(5/5)", 0.98)])
        self.assertEqual(find_immersive_progress(spans), 5)

    def test_reads_partial_progress(self):
        spans = parse_ocr_spans([(box(391, 300), "好物沉浸看(1/5)", 0.99)])
        self.assertEqual(find_immersive_progress(spans), 1)

    def test_ignores_similar_browse_task(self):
        # “好物精选好货(1/7)”既非精确标题、分母也不是 5，必须忽略
        spans = parse_ocr_spans([(box(391, 300), "好物精选好货(1/7)", 0.95)])
        self.assertIsNone(find_immersive_progress(spans))

    def test_returns_none_for_other_x5_task(self):
        # 别的 x/5 任务（如“看看#王者荣耀代练(1/5)”）不能被误读
        spans = parse_ocr_spans([(box(391, 300), "看看#王者荣耀代练(1/5)", 0.95)])
        self.assertIsNone(find_immersive_progress(spans))

    def test_returns_none_on_duplicate_title(self):
        spans = parse_ocr_spans([
            (box(391, 300), "好物沉浸看(1/5)", 0.99),
            (box(391, 900), "好物沉浸看(2/5)", 0.99),
        ])
        self.assertIsNone(find_immersive_progress(spans))


class SafeBrowseTargetTests(unittest.TestCase):
    SCREEN = (1080, 1920)

    def _row(self, cy, title, desc="浏览", reward="+50", action="去完成"):
        return [
            (box(391, cy), title, 0.98),
            (box(327, cy + 40), desc, 0.9),
            (box(560, cy + 40), reward, 0.7),
            (box(943, cy + 20), action, 0.97),
        ]

    def test_picks_topmost_safe_candidate(self):
        spans = parse_ocr_spans(
            self._row(300, "发现精选好物(0/5)")
            + self._row(900, "看看#王者荣耀代练(2/5)")
        )
        target = find_safe_browse_target(spans, self.SCREEN)
        self.assertIsInstance(target, BrowseTarget)
        self.assertEqual(target.title, "发现精选好物")   # 最靠上
        self.assertEqual((target.progress, target.total), (0, 5))
        self.assertEqual(target.action_center, (943, 320))

    def test_prefix_matches_kankan_family(self):
        spans = parse_ocr_spans(self._row(300, "看看#王者荣耀代练(2/5)"))
        target = find_safe_browse_target(spans, self.SCREEN)
        self.assertIsNotNone(target)
        self.assertEqual(target.title, "看看#王者荣耀代练")
        self.assertEqual((target.progress, target.total), (2, 5))

    def test_kankan_family_remains_safe_when_browse_description_is_missing(self):
        # 真机 OCR 偶尔漏读同行“浏览”，但“看看#”是已批准的轮换任务结构。
        spans = parse_ocr_spans(
            self._row(300, "看看#斯维诗鱼油(0/7)", desc="")
        )
        target = find_safe_browse_target(spans, self.SCREEN)
        self.assertIsNotNone(target)
        self.assertEqual(target.title, "看看#斯维诗鱼油")

    def test_search_task_remains_safe_when_browse_description_is_missing(self):
        # 真机 OCR 也可能漏读“搜一搜”行下方的“浏览”，标题本身已明确批准。
        spans = parse_ocr_spans(
            self._row(300, "搜一搜你心仪的宝贝(0/5)", desc="")
        )
        target = find_safe_browse_target(spans, self.SCREEN)
        self.assertIsNotNone(target)
        self.assertEqual(target.title, "搜一搜你心仪的宝贝")

    def test_only_public_beta_task_families_are_candidates(self):
        accepted = (
            ("搜一搜你心仪的宝贝(0/6)", ""),
            ("看看#斯维诗鱼油(2/7)", ""),
            ("发现精选好物(1/4)", "浏览"),
        )
        for title, description in accepted:
            with self.subTest(title=title):
                spans = parse_ocr_spans(
                    self._row(300, title, desc=description)
                )
                self.assertIsNotNone(
                    find_safe_browse_target(spans, self.SCREEN)
                )

    def test_removed_and_unverified_task_families_are_rejected(self):
        removed = (
            "拍立淘逛感兴趣的宝贝(0/5)",
            "酒店超抵日至高5%(0/1)",
            "去省钱卡领红包(0/1)",
            "淘金币充话费可抵钱(0/1)",
            "好物沉浸看(0/5)",
            "逛逛金币加抵好货(0/1)",
            "逛好店赚一大波金币(0/1)",
        )
        for title in removed:
            with self.subTest(title=title):
                spans = parse_ocr_spans(
                    self._row(300, title, desc="浏览5秒")
                )
                self.assertIsNone(
                    find_safe_browse_target(spans, self.SCREEN)
                )

    def test_only_titles_does_not_bypass_task_registry(self):
        spans = parse_ocr_spans(
            self._row(300, "拍立淘逛感兴趣的宝贝(0/5)", desc="浏览")
        )
        self.assertIsNone(
            find_safe_browse_target(
                spans,
                self.SCREEN,
                only_titles=("拍立淘逛感兴趣的宝贝",),
            )
        )

    def test_only_titles_prefix_matches_rotating_titles(self):
        # --task search 传入前缀"搜一搜"，必须匹配轮换标题"搜一搜你心仪的宝贝"
        spans = parse_ocr_spans(
            self._row(300, "搜一搜你心仪的宝贝(0/5)")
        )
        target = find_safe_browse_target(
            spans, self.SCREEN, only_titles=("搜一搜",)
        )
        self.assertIsNotNone(target)
        self.assertEqual(target.title, "搜一搜你心仪的宝贝")

    def test_only_titles_prefix_matches_hashtag_family(self):
        spans = parse_ocr_spans(
            self._row(300, "看看#斯维诗鱼油(2/7)")
        )
        target = find_safe_browse_target(
            spans, self.SCREEN, only_titles=("看看#",)
        )
        self.assertIsNotNone(target)
        self.assertEqual(target.title, "看看#斯维诗鱼油")

    def test_only_titles_exact_title_still_matches(self):
        spans = parse_ocr_spans(
            self._row(300, "发现精选好物(0/4)")
        )
        target = find_safe_browse_target(
            spans, self.SCREEN, only_titles=("发现精选好物",)
        )
        self.assertIsNotNone(target)
        self.assertEqual(target.title, "发现精选好物")

    def test_only_titles_prefix_does_not_match_unrelated_family(self):
        spans = parse_ocr_spans(
            self._row(300, "搜一搜你心仪的宝贝(0/5)")
        )
        self.assertIsNone(
            find_safe_browse_target(
                spans, self.SCREEN, only_titles=("看看#",)
            )
        )

    def test_featured_goods_requires_browse_row_evidence(self):
        spans = parse_ocr_spans(
            self._row(300, "发现精选好物(0/4)", desc="")
        )
        self.assertIsNone(find_safe_browse_target(spans, self.SCREEN))

    def test_every_global_unsafe_marker_rejects_registered_search_title(self):
        for marker in UNSAFE_ACTION_MARKERS:
            with self.subTest(marker=marker):
                spans = parse_ocr_spans(
                    self._row(300, f"搜一搜{marker}(0/5)", desc="")
                )
                self.assertIsNone(
                    find_safe_browse_target(spans, self.SCREEN)
                )

    def test_featured_goods_action_cannot_supply_browse_description(self):
        spans = parse_ocr_spans(
            self._row(
                300,
                "发现精选好物(0/4)",
                desc="",
                action="去浏览",
            )
        )
        self.assertIsNone(find_safe_browse_target(spans, self.SCREEN))

    def test_featured_goods_reward_cannot_supply_browse_description(self):
        spans = parse_ocr_spans(
            self._row(
                300,
                "发现精选好物(0/4)",
                desc="",
                reward="浏览 +50",
            )
        )
        self.assertIsNone(find_safe_browse_target(spans, self.SCREEN))

    def test_popup_task_action_near_bottom_is_tappable(self):
        # 当前真机弹窗的底部“看看#”按钮中心约在 y=1831，仍明显高于导航区。
        self.assertTrue(is_safe_tap_point((943, 1831), self.SCREEN))

    def test_supports_variable_denominator(self):
        spans = parse_ocr_spans(self._row(300, "发现精选好物(1/7)"))
        target = find_safe_browse_target(spans, self.SCREEN)
        self.assertIsNotNone(target)
        self.assertEqual((target.progress, target.total), (1, 7))

    def test_rejects_risk_row_even_if_prefix_matches(self):
        # 前缀命中，但同行含“下单” → 风险兜底拒绝
        spans = parse_ocr_spans(
            self._row(300, "发现精选好物(0/5)", desc="浏览 下单得500")
        )
        self.assertIsNone(find_safe_browse_target(spans, self.SCREEN))

    def test_rejects_external_app_marker_even_if_prefix_matches(self):
        spans = parse_ocr_spans(
            self._row(300, "好物精选头条任务(0/5)", desc="浏览")
        )
        self.assertIsNone(find_safe_browse_target(spans, self.SCREEN))

    def test_rejects_external_app_marker_in_row_description(self):
        spans = parse_ocr_spans(
            self._row(300, "发现精选好物(0/5)", desc="浏览 头条")
        )
        self.assertIsNone(find_safe_browse_target(spans, self.SCREEN))

    def test_requires_action_button_in_row(self):
        spans = parse_ocr_spans([
            (box(391, 300), "好物沉浸看(0/5)", 0.98),
            (box(327, 340), "浏览", 0.9),
        ])  # 无“去完成”
        self.assertIsNone(find_safe_browse_target(spans, self.SCREEN))

    def test_rejects_task_without_browse_keyword(self):
        # “点击去逛”不是浏览 → 拒绝
        spans = parse_ocr_spans(
            self._row(300, "去金豆夺宝0元领盲盒(0/1)", desc="点击去逛")
        )
        self.assertIsNone(find_safe_browse_target(spans, self.SCREEN))

    def test_rejects_browse_task_carrying_order(self):
        # 快速得大量金币：有“浏览10秒”但带“下单最高+10000” → 风险兜底拒绝
        spans = parse_ocr_spans(
            self._row(300, "快速得大量金币(0/1)",
                      desc="浏览10秒 下单最高+10000")
        )
        self.assertIsNone(find_safe_browse_target(spans, self.SCREEN))

    def test_rejects_game_browse_task(self):
        # 玩消消乐领金币：有“浏览5秒”但本质是游戏 → 拒绝
        spans = parse_ocr_spans(
            self._row(300, "玩消消乐领金币(0/1)", desc="浏览5秒")
        )
        self.assertIsNone(find_safe_browse_target(spans, self.SCREEN))

    def test_rejects_click_redpacket_task(self):
        # 点击商品领优惠红包：描述是“点击3个商品”，无“浏览” → 正向门已挡
        spans = parse_ocr_spans(
            self._row(300, "点击商品领优惠红包(0/1)", desc="点击3个商品立得")
        )
        self.assertIsNone(find_safe_browse_target(spans, self.SCREEN))

    def test_rejects_browse_redpacket_carrying_order(self):
        # 即便是浏览+红包，但带“下单” → 仍被下单兜底拦
        spans = parse_ocr_spans(
            self._row(300, "浏览领红包(0/1)", desc="浏览5秒 下单最高+1000")
        )
        self.assertIsNone(find_safe_browse_target(spans, self.SCREEN))

    def test_rejects_phone_recharge_task_even_with_browse_text(self):
        spans = parse_ocr_spans(
            self._row(
                300,
                "淘金币充话费可抵钱(0/1)",
                desc="浏览5秒",
            )
        )
        self.assertIsNone(find_safe_browse_target(spans, self.SCREEN))

    def test_kankan_hash_is_accepted_but_kankan_you_is_rejected(self):
        accepted = parse_ocr_spans(
            self._row(300, "看看#斯维诗鱼油(0/5)", desc="")
        )
        rejected = parse_ocr_spans(
            self._row(300, "看看你感兴趣的宝贝(0/5)", desc="浏览")
        )

        self.assertIsNotNone(find_safe_browse_target(accepted, self.SCREEN))
        self.assertIsNone(find_safe_browse_target(rejected, self.SCREEN))

    def test_rejects_non_whitelisted_browse_pages(self):
        # 非白名单的“浏览”行（会员/关注/感兴趣等个人页）必须拒绝
        for title in (
            "查看我的会员等级(0/1)",
            "看看你感兴趣的宝贝(0/1)",
            "看看你关注的商品动态(0/1)",
        ):
            with self.subTest(title=title):
                spans = parse_ocr_spans(self._row(300, title, desc="浏览"))
                self.assertIsNone(
                    find_safe_browse_target(spans, self.SCREEN), title
                )

    def test_excludes_given_titles(self):
        # 排除最靠上的已尝试任务后，返回下一个候选
        spans = parse_ocr_spans(
            self._row(300, "发现精选好物(0/5)")
            + self._row(900, "看看#王者荣耀代练(2/5)")
        )
        target = find_safe_browse_target(
            spans, self.SCREEN, exclude_titles=("发现精选好物",)
        )
        self.assertIsNotNone(target)
        self.assertEqual(target.title, "看看#王者荣耀代练")

    def test_exclude_covers_rotating_kankan_hash_titles(self):
        # 看看#话题轮换：排除旧话题后，新话题不得被当作新任务重复发现
        spans = parse_ocr_spans(self._row(300, "看看#鱼油斯维诗(2/7)"))
        self.assertIsNone(
            find_safe_browse_target(
                spans, self.SCREEN, exclude_titles=("看看#鱼油生发",)
            )
        )

    def test_only_titles_matches_rotating_kankan_hash(self):
        # 按旧话题标题重定位时，轮换后的新话题应命中同一任务
        spans = parse_ocr_spans(self._row(300, "看看#鱼油斯维诗(2/7)"))
        target = find_safe_browse_target(
            spans, self.SCREEN, only_titles=("看看#鱼油生发",)
        )
        self.assertIsNotNone(target)
        self.assertEqual(target.title, "看看#鱼油斯维诗")


class ReadSafeBrowseProgressTests(unittest.TestCase):
    def test_reads_exact_title_progress_without_button(self):
        spans = parse_ocr_spans([(box(391, 300), "好物沉浸看(5/5)", 0.98)])
        self.assertEqual(read_safe_browse_progress(spans, "好物沉浸看"), (5, 5))

    def test_supports_variable_denominator(self):
        spans = parse_ocr_spans([(box(391, 300), "好物精选好货(3/7)", 0.98)])
        self.assertEqual(read_safe_browse_progress(spans, "好物精选好货"), (3, 7))

    def test_requires_exact_title_not_prefix(self):
        spans = parse_ocr_spans([(box(391, 300), "好物精选好货(1/7)", 0.98)])
        self.assertIsNone(read_safe_browse_progress(spans, "好物沉浸看"))

    def test_reads_rotating_kankan_hash_progress(self):
        # 看看#话题轮换后，按旧标题仍能读到同一任务的进度
        spans = parse_ocr_spans([(box(391, 300), "看看#鱼油斯维诗(2/7)", 0.95)])
        self.assertEqual(
            read_safe_browse_progress(spans, "看看#鱼油生发"), (2, 7)
        )

    def test_returns_none_when_absent(self):
        spans = parse_ocr_spans([(box(391, 300), "看看#王者荣耀代练(1/5)", 0.95)])
        self.assertIsNone(read_safe_browse_progress(spans, "好物沉浸看"))


class ScrollToTopThenFindTests(unittest.TestCase):
    def test_scrolls_up_to_top_then_finds_downward(self):
        # 模拟：初始在中部(sig=b)，上滚三次后 a 连续两次不变才到顶，
        # 再下滚逐屏 c、d，在第三个下滚后的屏命中目标 T。
        probes = iter([
            (None, "b"),   # 初始
            (None, "a"),   # 上滚1
            (None, "a"),   # 上滚2 → 第一次不变
            (None, "a"),   # 上滚3 → 第二次不变，到顶
            (None, "c"),   # 下滚1
            (None, "d"),   # 下滚2
            ("T", "e"),    # 下滚3 命中
        ])
        ups, downs = {"n": 0}, {"n": 0}

        result = scroll_to_top_then_find(
            probe=lambda: next(probes),
            scroll_up=lambda: ups.__setitem__("n", ups["n"] + 1),
            scroll_down=lambda: downs.__setitem__("n", downs["n"] + 1),
            max_scrolls=8,
        )
        self.assertEqual(result, "T")
        self.assertEqual(ups["n"], 3)     # 上滚 3 次到顶
        self.assertEqual(downs["n"], 2)   # 下滚 2 次后命中

    def test_finds_immediately_at_top(self):
        probes = iter([
            (None, "a"), (None, "a"), (None, "a"),  # 两次不变确认已在顶
            ("T", "a"),                              # 到顶后第 0 屏即命中
        ])
        result = scroll_to_top_then_find(
            probe=lambda: next(probes),
            scroll_up=lambda: None,
            scroll_down=lambda: None,
        )
        self.assertEqual(result, "T")

    def test_returns_none_when_absent_after_full_scan(self):
        # 到顶后逐屏都无目标，连续两次不变才滚到底 → None
        seq = [(None, "a"), (None, "a"), (None, "a")]  # 到顶
        seq += [(None, "x"), (None, "x"), (None, "x")]  # 到底
        probes = iter(seq)
        result = scroll_to_top_then_find(
            probe=lambda: next(probes),
            scroll_up=lambda: None,
            scroll_down=lambda: None,
            max_scrolls=8,
        )
        self.assertIsNone(result)

    def test_does_not_stop_after_one_unchanged_downward_screen(self):
        probes = iter([
            (None, "a"), (None, "a"), (None, "a"),  # 上滚两次不变，到顶
            (None, "x"), (None, "x"),                # 下滚一次后第一次不变
            ("T", "y"),                              # 下一屏变化并命中
        ])
        downs = {"n": 0}

        result = scroll_to_top_then_find(
            probe=lambda: next(probes),
            scroll_up=lambda: None,
            scroll_down=lambda: downs.__setitem__("n", downs["n"] + 1),
        )
        self.assertEqual(result, "T")
        self.assertEqual(downs["n"], 2)


class DryRunInspectionTests(unittest.TestCase):
    """单屏 dry-run 行判定：共享 evaluator、脱敏标签与失败关闭。"""

    SCREEN = (1080, 1920)
    screen = SCREEN
    ALLOWED_LABELS = ("搜一搜…", "看看#…", "发现精选好物", "未知任务")

    def _raw_row(self, cy, title, desc="浏览", reward="+50", action="去完成",
                 title_conf=0.98, action_conf=0.97):
        return [
            (box(391, cy), title, title_conf),
            (box(327, cy + 40), desc, 0.9),
            (box(560, cy + 40), reward, 0.7),
            (box(943, cy + 20), action, action_conf),
        ]

    def row(self, title, desc="浏览", action="去完成", cy=300):
        """构造单行 OCR：标题缺进度段时补 (0/1)，保证能被识别为进度段。"""
        if PROGRESS_RE.search(title) is None:
            title = f"{title}(0/1)"
        return parse_ocr_spans(
            self._raw_row(cy, title, desc=desc, action=action)
        )

    def unknown_row(self, title, cy=300):
        return self.row(title, desc="浏览", action="去完成", cy=cy)

    def supported_rows(self):
        return parse_ocr_spans(
            self._raw_row(300, "搜一搜你心仪的宝贝(0/5)", desc="浏览")
            + self._raw_row(600, "看看#斯维诗鱼油(0/7)", desc="浏览")
            + self._raw_row(900, "发现精选好物(0/5)", desc="浏览")
        )

    def supported_search_row(self):
        return parse_ocr_spans(
            self._raw_row(300, "搜一搜你心仪的宝贝(0/5)", desc="浏览")
        )

    def test_reports_all_three_supported_profiles_with_redacted_labels(self):
        decisions = inspect_visible_task_rows(self.supported_rows(), self.screen)
        self.assertEqual(
            [(d.task_key, d.label, d.status, d.reason) for d in decisions],
            [
                ("search_discovery", "搜一搜…", "supported", "supported"),
                ("hashtag_browse", "看看#…", "supported", "supported"),
                ("featured_goods", "发现精选好物", "supported", "supported"),
            ],
        )
        self.assertNotIn("斯维诗鱼油", repr(decisions))

    def test_unknown_row_never_retains_its_title(self):
        decision = inspect_visible_task_rows(
            self.unknown_row("秘密商品"), self.screen
        )[0]
        self.assertIsNone(decision.task_key)
        self.assertEqual(
            (decision.label, decision.status, decision.reason),
            ("未知任务", "skipped", "unsupported_task"),
        )
        self.assertNotIn("秘密商品", repr(decision))

    def test_external_marker_wins_over_unsupported(self):
        decision = inspect_visible_task_rows(
            self.row("今日头条极速版"), self.screen
        )[0]
        self.assertEqual(decision.reason, "external_app_marker")

    def test_unsafe_marker_wins_before_profile_acceptance(self):
        decision = inspect_visible_task_rows(
            self.row("发现精选好物(0/1)", desc="浏览后下单"), self.screen
        )[0]
        self.assertEqual(decision.reason, "unsafe_marker")

    def test_execute_selector_and_dry_run_share_supported_decision(self):
        spans = self.supported_search_row()
        decision = inspect_visible_task_rows(spans, self.screen)[0]
        target = find_safe_browse_target(spans, self.screen)
        self.assertEqual(decision.reason, "supported")
        self.assertEqual(
            (target.progress, target.total),
            (decision.progress, decision.total),
        )

    def test_missing_description_evidence_fails_closed(self):
        decision = inspect_visible_task_rows(
            self.row("发现精选好物(0/4)", desc=""), self.screen
        )[0]
        self.assertEqual(decision.reason, "missing_description_evidence")
        self.assertEqual(decision.status, "skipped")

    def test_progress_unreadable_fails_closed(self):
        decision = inspect_visible_task_rows(
            self.row("看看#鱼油(9/7)"), self.screen
        )[0]
        self.assertEqual(decision.reason, "progress_unreadable")
        self.assertEqual(decision.status, "skipped")

    def test_action_not_unique_fails_closed(self):
        spans = parse_ocr_spans([
            (box(391, 300), "搜一搜你心仪的宝贝(0/5)", 0.98),
            (box(700, 320), "去完成", 0.97),
            (box(943, 320), "去完成", 0.96),
        ])
        decision = inspect_visible_task_rows(spans, self.screen)[0]
        self.assertEqual(decision.reason, "action_not_unique")

    def test_row_unreadable_low_confidence_fails_closed(self):
        spans = parse_ocr_spans(
            self._raw_row(300, "搜一搜你心仪的宝贝(0/5)", title_conf=0.3),
            min_confidence=0.2,
        )
        decision = inspect_visible_task_rows(spans, self.screen)[0]
        self.assertEqual(decision.reason, "row_unreadable")

    def test_row_without_action_segment_is_not_reported(self):
        spans = parse_ocr_spans([
            (box(391, 300), "搜一搜你心仪的宝贝(0/5)", 0.98),
            (box(327, 340), "浏览", 0.9),
        ])
        self.assertEqual(inspect_visible_task_rows(spans, self.screen), [])

    def test_every_decision_label_is_in_whitelist(self):
        spans = parse_ocr_spans(
            self._raw_row(300, "搜一搜你心仪的宝贝(0/5)", desc="浏览")
            + self._raw_row(600, "看看#斯维诗鱼油(0/7)", desc="浏览")
            + self._raw_row(900, "发现精选好物(0/5)", desc="浏览")
            + self._raw_row(1200, "今日头条极速版", desc="浏览")
            + self._raw_row(1500, "发现精选好物(0/4)", desc="")
        )
        decisions = inspect_visible_task_rows(spans, self.screen)
        self.assertGreaterEqual(len(decisions), 1)
        for decision in decisions:
            self.assertIn(decision.label, self.ALLOWED_LABELS)
            if decision.label == "未知任务":
                self.assertIsNone(decision.task_key)


class SearchFirstTaskTests(unittest.TestCase):
    """搜一搜类任务：搜索入口页/结果页识别与发现栏候选（建模自图2/图3）。"""

    SCREEN = (1080, 1920)

    # 图2 搜索入口页：搜索框、历史搜索词、“搜索发现”栏及其下推荐词卡片
    ENTRY_SAMPLE = [
        (box(250, 90), "搜索有福利", 0.95),
        (box(860, 90), "搜索后浏览立得奖励", 0.93),
        (box(400, 230), "搜索宝贝", 0.90),
        (box(1000, 230), "搜索", 0.96),
        (box(280, 330), "历史搜索", 0.95),
        (box(300, 410), "iPhone17", 0.94),
        (box(620, 410), "裙子女", 0.92),
        (box(250, 720), "搜索发现", 0.96),
        (box(250, 770), "快速上热门", 0.85),          # 区块名噪声
        (box(390, 830), "抖加充值", 0.90),
        (box(800, 830), "VR眼镜", 0.91),
        (box(390, 890), "行业口碑商品", 0.85),        # 卡片元信息噪声
        (box(800, 890), "近一个月曝光超2万", 0.84),   # 元信息噪声
        (box(410, 1010), "PICO 4 Ultra", 0.90),
        (box(820, 1010), "PVC工牌定制", 0.89),
        (box(880, 1450), "苹果iphone17", 0.90),
        (box(870, 1510), "搜索近一周上涨190%+", 0.84),  # 元信息噪声
        (box(350, 1880), "封闭式木制巢箱", 0.88),   # 底部导航区：不安全点击
    ]

    # 图3 结果页：顶部“浏览10秒可领币20”奖励条 + 商品流
    FEED_SAMPLE = [
        (box(540, 100), "浏览10秒可领币20", 0.94),
        (box(540, 160), "任意下单最高另得500淘金币", 0.90),
        (box(300, 260), "iPhone17", 0.95),
        (box(1000, 260), "搜索", 0.95),
        (box(300, 780), "iPhone 17e 苹果手机全新国行", 0.90),
        (box(300, 900), "￥503.43", 0.88),
    ]

    def test_needs_search_first_only_for_search_task(self):
        self.assertTrue(needs_search_first("搜一搜你心仪的宝贝(6/7)"))
        self.assertFalse(needs_search_first("好物沉浸看(0/5)"))
        self.assertFalse(needs_search_first("发现精选好物(1/5)"))
        self.assertFalse(needs_search_first("看看#鱼油生发(2/7)"))

    def test_detects_search_entry_page(self):
        entry = parse_ocr_spans(self.ENTRY_SAMPLE)
        feed = parse_ocr_spans(self.FEED_SAMPLE)
        self.assertTrue(is_search_entry_page(entry))
        self.assertFalse(is_search_entry_page(feed))

    def test_detects_search_result_feed(self):
        entry = parse_ocr_spans(self.ENTRY_SAMPLE)
        feed = parse_ocr_spans(self.FEED_SAMPLE)
        self.assertTrue(is_search_result_feed(feed))
        self.assertFalse(is_search_result_feed(entry))

    def test_discovery_candidates_below_header_and_tappable(self):
        entry = parse_ocr_spans(self.ENTRY_SAMPLE)
        candidates = find_discovery_candidates(entry, self.SCREEN)
        texts = [c.text for c in candidates]
        # 发现栏下方的推荐词被纳入
        self.assertIn("VR眼镜", texts)
        # 充值/交易/红包类入口不得成为候选（点击不产生搜索流，可能进交易页）
        self.assertNotIn("抖加充值", texts)
        # 区块标题本身、上方历史词与搜索框都不得入选
        self.assertNotIn("搜索发现", texts)
        self.assertNotIn("iPhone17", texts)
        self.assertNotIn("历史搜索", texts)
        # 全部候选均在“搜索发现”下方且落在安全点击区
        for c in candidates:
            self.assertGreater(c.center[1], 720)
            self.assertTrue(is_safe_tap_point(c.center, self.SCREEN))

    def test_discovery_candidates_exclude_recharge_and_red_packet(self):
        # 充值/红包/提现等交易特征文本即使出现在发现栏下方也绝不点击
        entry = parse_ocr_spans(self.ENTRY_SAMPLE)
        for extra in ("话费充值", "领红包", "余额提现"):
            spans = entry + [
                OcrSpan(
                    extra, 0.99, (400, 900),
                    (350, 880, 450, 920),
                )
            ]
            texts = [
                c.text for c in find_discovery_candidates(spans, self.SCREEN)
            ]
            self.assertNotIn(extra, texts)

    def test_discovery_candidates_exclude_bottom_nav_item(self):
        entry = parse_ocr_spans(self.ENTRY_SAMPLE)
        texts = [c.text for c in find_discovery_candidates(entry, self.SCREEN)]
        # 底部导航区的词因超出安全点击区而被排除
        self.assertNotIn("封闭式木制巢箱", texts)

    def test_discovery_candidates_empty_without_header(self):
        feed = parse_ocr_spans(self.FEED_SAMPLE)
        self.assertEqual(find_discovery_candidates(feed, self.SCREEN), [])

    def test_discovery_candidates_exclude_noise_labels(self):
        entry = parse_ocr_spans(self.ENTRY_SAMPLE)
        texts = [c.text for c in find_discovery_candidates(entry, self.SCREEN)]
        # 区块名/卡片元信息/广告噪声不得入选（点了不产生结果流）
        for noise in ("快速上热门", "行业口碑商品", "近一个月曝光超2万",
                      "搜索近一周上涨190%+"):
            self.assertNotIn(noise, texts)
        # 真实推荐词仍保留
        self.assertIn("VR眼镜", texts)
        # 充值/交易类（如"抖加充值"）现在按噪声排除
        self.assertNotIn("抖加充值", texts)

    # 真机详情页底栏：加购/立即购买（只用于识别，绝不点）
    DETAIL_SAMPLE = [
        (box(300, 500), "iPhone 17e 苹果手机全新国行", 0.92),
        (box(300, 1800), "加入购物车", 0.95),
        (box(800, 1800), "立即购买", 0.95),
    ]

    def test_detects_product_detail_page(self):
        detail = parse_ocr_spans(self.DETAIL_SAMPLE)
        feed = parse_ocr_spans(self.FEED_SAMPLE)
        self.assertTrue(is_product_detail_page(detail))
        self.assertFalse(is_product_detail_page(feed))

    def test_result_product_candidates_pick_titles_not_noise(self):
        feed = parse_ocr_spans(self.FEED_SAMPLE)
        texts = [c.text for c in
                 find_result_product_candidates(feed, self.SCREEN)]
        # 商品标题入选；奖励条/搜索词/价格不得入选
        self.assertIn("iPhone 17e 苹果手机全新国行", texts)
        self.assertNotIn("浏览10秒可领币20", texts)
        self.assertNotIn("任意下单最高另得500淘金币", texts)
        self.assertNotIn("￥503.43", texts)
        # 顶部搜索框关键词（屏幕上部）也不得入选
        self.assertNotIn("iPhone17", texts)


class BrowseBadgeTests(unittest.TestCase):
    """“浏览N秒可领”要求徽标解析（真机 2026-08-29：浏览25秒可领，右上角）。"""

    @staticmethod
    def _span(text, confidence=0.94):
        return OcrSpan(text, confidence, (700, 110), (548, 90, 880, 130))

    def test_parses_required_seconds_from_badge(self):
        self.assertEqual(
            parse_browse_badge([self._span("浏览25秒可领")]), 25
        )

    def test_tolerates_trailing_suffix(self):
        self.assertEqual(
            parse_browse_badge([self._span("浏览10秒可领金币")]), 10
        )

    def test_absent_or_empty_spans_return_none(self):
        self.assertIsNone(parse_browse_badge([]))
        self.assertIsNone(parse_browse_badge(None))

    def test_low_confidence_badge_is_ignored(self):
        self.assertIsNone(parse_browse_badge([self._span("浏览25秒可领", 0.3)]))

    def test_unrelated_text_returns_none(self):
        self.assertIsNone(parse_browse_badge([self._span("浏览25秒")]))
        self.assertIsNone(parse_browse_badge([self._span("搜索抵钱")]))


class SearchResultFeedClassificationTests(unittest.TestCase):
    """回归：入口页带“浏览N秒可领”徽标时不得误判为结果页。

    真机 2026-08-29：徽标同时出现在发现入口页与结果页。若入口页被误判为
    结果页，策略会在入口页空滑 80 秒、一个关键词都不点，回合必不计数。
    """

    @staticmethod
    def _entry_with_badge():
        return [
            OcrSpan("搜索发现", 0.99, (200, 500), (100, 480, 300, 520)),
            OcrSpan("杯子", 0.87, (137, 289), (100, 270, 180, 310)),
            OcrSpan("浏览25秒可领", 0.94, (548, 103), (548, 90, 880, 130)),
            OcrSpan("搜索", 1.0, (887, 283), (850, 260, 930, 300)),
        ]

    @staticmethod
    def _result_page_with_badge():
        return [
            OcrSpan("浏览25秒可领", 0.94, (548, 103), (548, 90, 880, 130)),
            OcrSpan("七天退换", 0.9, (710, 998), (650, 980, 780, 1010)),
        ]

    def test_entry_with_badge_is_entry_not_result(self):
        self.assertTrue(is_search_entry_page(self._entry_with_badge()))
        self.assertFalse(is_search_result_feed(self._entry_with_badge()))

    def test_result_page_with_badge_is_result(self):
        self.assertTrue(is_search_result_feed(self._result_page_with_badge()))
        self.assertFalse(is_search_entry_page(self._result_page_with_badge()))


class SearchFlowPageTests(unittest.TestCase):
    """搜索流程页身份（入口/结果页，含徽标已消失的结果页）。

    真机 2026-08-29：徽标消失后的结果页只剩“搜索”按钮与商品卡，若不识别
    会导致回退把手机留在原地。必须排除淘宝首页（有底部导航标记）。
    """

    @staticmethod
    def _span(text, x=100, y=100, confidence=0.98):
        return OcrSpan(text, confidence, (x, y), (x, y, x + 80, y + 40))

    def _result_page(self):
        return [
            self._span("搜索", 887, 283),
            self._span("鼠标", 137, 289),
            self._span("七天退换", 710, 998),
        ]

    def _home_feed(self):
        return [
            self._span("搜索", 893, 211),
            self._span("视频", 287, 1857),
            self._span("消息", 503, 1857),
            self._span("购物车", 702, 1855),
        ]

    def test_result_page_without_badge_is_flow_page(self):
        self.assertTrue(is_search_flow_page(self._result_page()))

    def test_home_feed_is_not_flow_page(self):
        self.assertFalse(is_search_flow_page(self._home_feed()))

    def test_coin_page_is_not_flow_page(self):
        self.assertFalse(
            is_search_flow_page(
                [self._span("淘金币"), self._span("赚更多金币")]
            )
        )

    def test_missing_search_button_is_not_flow_page(self):
        self.assertFalse(is_search_flow_page([self._span("商品标题")]))

    def test_low_confidence_search_button_ignored(self):
        self.assertFalse(
            is_search_flow_page([self._span("搜索", 887, 283, 0.5)])
        )


class CoinTaskProductPageTests(unittest.TestCase):
    """淘金币折扣商品卡身份（看看#卡片页/搜索结果页，含 OCR 己/已变体）。

    真机 2026-08-29：看看#内容页卡片带“金币已抵”，搜索结果页带“金币己抵”
    （OCR 变体），淘宝首页商品卡没有该标签——据此区分任务生态与首页。
    """

    @staticmethod
    def _span(text, x=100, y=100, confidence=0.98):
        return OcrSpan(text, confidence, (x, y), (x, y, x + 80, y + 40))

    def test_yi_variant_recognized(self):
        self.assertTrue(
            is_coin_task_product_page([self._span("金币已抵"), self._span("七天退换")])
        )

    def test_ji_variant_recognized(self):
        self.assertTrue(
            is_coin_task_product_page([self._span("金币己抵"), self._span("正品保证")])
        )

    def test_home_feed_excluded(self):
        self.assertFalse(
            is_coin_task_product_page(
                [self._span("视频"), self._span("购物车"), self._span("商品")]
            )
        )

    def test_home_post_mentioning_coin_excluded(self):
        """首页帖子文本含“淘金币”字样也不得被当成任务商品卡。"""
        self.assertFalse(
            is_coin_task_product_page(
                [
                    self._span("视频", 287, 1857),
                    self._span("消息", 503, 1857),
                    self._span("淘金币的抵扣比例是 100个淘金."),
                ]
            )
        )

    def test_empty_returns_false(self):
        self.assertFalse(is_coin_task_product_page([]))
        self.assertFalse(is_coin_task_product_page(None))


class PageFingerprintTests(unittest.TestCase):
    """页面识别指纹：诊断用布尔/计数信号，绝不携带 OCR 原文（隐私合同）。"""

    @staticmethod
    def _span(text, x=100, y=100, confidence=0.98):
        return OcrSpan(text, confidence, (x, y), (x, y, x + 80, y + 40))

    def test_result_page_fingerprint(self):
        payload = page_fingerprint(
            [self._span("搜索", 887, 283), self._span("商品标题甲乙丙", 300, 600)]
        )
        self.assertEqual(payload["span_count"], 2)
        self.assertTrue(payload["is_flow_page"])
        self.assertTrue(payload["has_search_button"])
        self.assertFalse(payload["is_entry_page"])
        self.assertFalse(payload["has_badge"])
        self.assertFalse(payload["has_coin_title"])
        self.assertFalse(payload["has_popup_title"])

    def test_coin_page_fingerprint(self):
        payload = page_fingerprint(
            [self._span("淘金币"), self._span("赚金币抵钱")]
        )
        self.assertTrue(payload["has_coin_title"])
        self.assertTrue(payload["has_popup_title"])
        self.assertFalse(payload["has_search_button"])

    def test_no_raw_text_leaks_into_payload(self):
        payload = page_fingerprint([self._span("机密商品名称甲乙丙丁")])
        self.assertNotIn("机密商品名称", json.dumps(payload, ensure_ascii=False))
        self.assertEqual(payload["span_count"], 1)

    def test_empty_and_none_are_safe(self):
        for spans in ([], None):
            with self.subTest(spans=spans):
                payload = page_fingerprint(spans)
                self.assertEqual(payload["span_count"], 0)
                self.assertFalse(payload["is_entry_page"])


if __name__ == "__main__":
    unittest.main()

class DiscoveryTwoFrameSafetyTests(unittest.TestCase):
    """P0-1 两帧安全边界：第一行限定 + 第二帧重定位（Codex 核验设计）。"""

    SCREEN = (1080, 1920)

    def _entry(self, keyword_text="鱼油推荐", keyword_y=700, conf=0.9):
        return [
            OcrSpan("搜索发现", 0.99, (200, 500), (100, 480, 300, 520)),
            OcrSpan(
                keyword_text, conf, (300, keyword_y),
                (200, keyword_y - 20, 400, keyword_y + 20),
            ),
        ]

    def _candidate(self, text="鱼油推荐", center=(300, 700)):
        return ocr_ui.DiscoveryCandidate(
            text=text, center=center,
            bbox=(center[0] - 100, center[1] - 20,
                  center[0] + 100, center[1] + 20),
            confidence=0.9,
        )

    def test_first_row_all_noise_does_not_fall_to_second_row(self):
        # 第一行全是噪声（不可点）→ 返回空；绝不跳到第二行选商品行
        spans = [
            OcrSpan("搜索发现", 0.99, (200, 500), (100, 480, 300, 520)),
            OcrSpan("快速上热门", 0.99, (300, 700), (200, 680, 400, 720)),
            OcrSpan("鱼油推荐", 0.99, (300, 1100), (200, 1080, 400, 1120)),
        ]
        self.assertEqual(
            ocr_ui.find_discovery_candidates(spans, self.SCREEN), []
        )

    def test_low_confidence_keyword_excluded(self):
        spans = self._entry(conf=0.40)
        self.assertEqual(
            ocr_ui.find_discovery_candidates(spans, self.SCREEN), []
        )

    def test_revalidate_none_when_candidate_absent(self):
        original = self._candidate()
        self.assertIsNone(
            ocr_ui.revalidate_discovery_candidate(original, [], self.SCREEN)
        )

    def test_revalidate_none_when_ambiguous(self):
        original = self._candidate()
        dup = OcrSpan("鱼油推荐", 0.9, (300, 700), (200, 680, 400, 720))
        dup2 = OcrSpan("鱼油推荐", 0.9, (600, 700), (500, 680, 700, 720))
        self.assertIsNone(
            ocr_ui.revalidate_discovery_candidate(
                original, [dup, dup2], self.SCREEN
            )
        )

    def test_revalidate_none_when_shifted_beyond_tolerance(self):
        original = self._candidate(center=(300, 700))
        # 移动 200px > 屏宽 12%（129.6px）→ 视为页面变化，零点击
        shifted = OcrSpan(
            "鱼油推荐", 0.9, (500, 700), (400, 680, 600, 720)
        )
        self.assertIsNone(
            ocr_ui.revalidate_discovery_candidate(
                original, [shifted], self.SCREEN
            )
        )

    def test_revalidate_returns_fresh_coordinates_when_stable(self):
        original = self._candidate(center=(300, 700))
        # 移动 30px（允许内）→ 返回第二帧新坐标
        fresh = OcrSpan("鱼油推荐", 0.9, (330, 700), (230, 680, 430, 720))
        result = ocr_ui.revalidate_discovery_candidate(
            original, [fresh], self.SCREEN
        )
        self.assertIsNotNone(result)
        self.assertEqual(result.center, (330, 700))

    def test_revalidate_tolerates_ocr_jitter(self):
        # 第二帧标点/空格抖动：标准化后相似度>=0.8 仍匹配同一词
        original = self._candidate(text="苹果充电线")
        jittered = OcrSpan(
            "苹果充电线，", 0.75, (300, 700), (200, 680, 400, 720)
        )
        result = ocr_ui.revalidate_discovery_candidate(
            original, [jittered], self.SCREEN
        )
        self.assertIsNotNone(result)
        self.assertEqual(result.center, (300, 700))

    def test_scaled_resolution_judges_consistently(self):
        # 等比缩放 2 倍（2160x3840）：同一布局判定一致（候选仍在第一行）
        base = self._entry()
        scaled = [
            OcrSpan(s.text, s.confidence,
                    (s.center[0] * 2, s.center[1] * 2),
                    (s.bounds[0] * 2, s.bounds[1] * 2,
                     s.bounds[2] * 2, s.bounds[3] * 2))
            for s in base
        ]
        base_candidates = ocr_ui.find_discovery_candidates(base, self.SCREEN)
        scaled_candidates = ocr_ui.find_discovery_candidates(
            scaled, (2160, 3840)
        )
        self.assertEqual(
            [c.text for c in base_candidates],
            [c.text for c in scaled_candidates],
        )


class CoinBalanceParseTests(unittest.TestCase):
    """parse_coin_balance：淘金币首页余额解析（只读纯函数）。"""

    def test_parses_real_device_sample(self):
        spans = [
            OcrSpan("淘金币", 0.62, (220, 138), (180, 128, 260, 148)),
            OcrSpan("16152可抵161元", 0.99, (515, 137), (400, 127, 630, 147)),
            OcrSpan("提醒我来领淘金币", 0.95, (272, 249), (180, 239, 360, 259)),
        ]
        self.assertEqual(ocr_ui.parse_coin_balance(spans), 16152)

    def test_none_without_anchor(self):
        spans = [OcrSpan("随便", 0.9, (1, 1), (0, 0, 2, 2))]
        self.assertIsNone(ocr_ui.parse_coin_balance(spans))

    def test_anchor_contains_number(self):
        spans = [
            OcrSpan("淘金币 12345", 0.9, (220, 138), (180, 128, 260, 148))
        ]
        self.assertEqual(ocr_ui.parse_coin_balance(spans), 12345)

    def test_other_rows_numbers_ignored(self):
        # 下方"每日可领120"等数字不得误读为余额
        spans = [
            OcrSpan("淘金币", 0.62, (220, 138), (180, 128, 260, 148)),
            OcrSpan("16152可抵161元", 0.99, (515, 137), (400, 127, 630, 147)),
            OcrSpan("120", 0.99, (370, 430), (350, 420, 390, 440)),
        ]
        self.assertEqual(ocr_ui.parse_coin_balance(spans), 16152)
