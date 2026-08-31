"""OCR 结果解析层（纯函数，不触碰设备、不导入 easyocr）。

输入为 easyocr.readtext 的原始输出 [(bbox_points, text, confidence), ...]，
bbox_points 为 4 个 [x, y] 角点。输出过滤后的文本段、定位到的“好物沉浸看”目标、
进度值与风险判定。全部为纯函数，便于离线测试与复用。
"""

import re
from dataclasses import dataclass
from difflib import SequenceMatcher

from taojinbi_mav.tasks.registry import profile_for_title
from taojinbi_mav.task_core import (
    EXTERNAL_APP_MARKERS,
    REWARD_RE,
    UNSAFE_ACTION_MARKERS,
)


IMMERSIVE_TITLE = "好物沉浸看"
ACTION_WORDS = ("去完成", "去逛逛", "去浏览", "去看看", "逛一逛")
RISK_WORDS = (
    "验证码",
    "安全验证",
    "滑块验证",
    "人机验证",
    "访问受限",
    "操作频繁",
    "账号异常",
    "风险控制",
    "风控",
)
PROGRESS_RE = re.compile(r"(\d+)\s*/\s*(\d+)")

UNSAFE_BROWSE_MARKERS = UNSAFE_ACTION_MARKERS + (
    "付款",
    "支付",
    "结算",
    "充话费",
    "抽奖",
    "夺宝",
    "盲盒",
)

# 搜索入口页锦点（图2）：有“搜索发现/历史搜索”即说明还停在入口页、未进结果流
SEARCH_ENTRY_ANCHORS = ("搜索发现", "历史搜索")
SEARCH_DISCOVERY_ANCHOR = "搜索发现"     # 发现栏区块标题，其下方为可点的推荐词卡片
SEARCH_RESULT_ANCHOR = "可领"           # 结果页顶部“浏览N秒可领币M”奖励条（图3）
# 发现栏卡片的区块名/元信息/广告噪声（非可搜索词，点了不产生结果流）
SEARCH_NOISE_MARKERS = (
    "口碑商品", "新趋商品", "近一个月", "近一周", "曝光超", "点击超",
    "下单超", "上涨", "快速上热门", "在线投放", "免费设计", "官方立减",
    "正品保证",
    # 充值/交易/红包类：点击不产生搜索结果流，可能进入充值/交易页
    "充值", "红包", "提现", "余额",
    # 促销/属性副文案（卡片小字，非可搜索词）
    "原封", "正品", "官方",
)
# 商品详情页锦点（仅用于识别“已进详情”，绝不点击这些按钮）
PRODUCT_DETAIL_ANCHORS = ("加入购物车", "立即购买", "领券购买")
# 结果页里不是商品标题的噪声（奖励条/搜索框/促销标签等，点了进不了详情）
RESULT_TILE_NOISE = (
    "搜索", "可领", "浏览", "淘金币", "金币", "已抵", "立减", "下单",
    "正品", "七天", "退换", "包邮", "旗舰", "官方", "保证", "免费",
    "直供", "回买", "声索赔", "￥", "¥",
    # 淘宝偶发错误页文案（真机见“系统出了点问题”被当商品点）
    "系统出了", "点问题", "重新加载", "稍后再试", "网络异常",
)

# 注册任务 key → dry-run/execute 共享的稳定内部标识（绝不携带轮换标题）
TASK_KEYS = {
    "search": "search_discovery",
    "hashtag": "hashtag_browse",
    "featured_goods": "featured_goods",
    "immersive": "immersive_goods",
}
UNKNOWN_TASK_LABEL = "未知任务"
# dry-run 行判定稳定 reason 优先级（external → unsafe → unsupported → desc →
# progress → action → confidence → supported）
DRY_RUN_REASONS = (
    "external_app_marker",
    "unsafe_marker",
    "unsupported_task",
    "missing_description_evidence",
    "progress_unreadable",
    "action_not_unique",
    "row_unreadable",
    "supported",
)


@dataclass(frozen=True)
class OcrSpan:
    text: str
    confidence: float
    center: tuple[int, int]
    bounds: tuple[int, int, int, int]


def find_unique_ocr_span(spans, text, min_confidence=0.5):
    """Return one exact, high-confidence OCR span or fail closed."""
    candidates = [
        span
        for span in spans or []
        if span.text.strip() == text
        and span.confidence >= min_confidence
    ]
    return candidates[0] if len(candidates) == 1 else None


@dataclass(frozen=True)
class ImmersiveTarget:
    title: str
    progress_text: str
    title_center: tuple[int, int]
    action_text: str
    action_center: tuple[int, int]
    confidence: float


@dataclass(frozen=True)
class BrowseTarget:
    """通用安全浏览任务目标（支持任意分母）。"""
    title: str
    progress: int
    total: int
    title_center: tuple[int, int]
    action_text: str
    action_center: tuple[int, int]
    confidence: float


@dataclass(frozen=True)
class DryRunDecision:
    """单行 dry-run 判定（脱敏：绝不保存原始标题或 OCR 原文）。"""
    task_key: str | None
    label: str
    status: str
    reason: str
    progress: int | None
    total: int | None


def _bounds_from_points(points):
    xs = [point[0] for point in points]
    ys = [point[1] for point in points]
    return (
        int(round(min(xs))),
        int(round(min(ys))),
        int(round(max(xs))),
        int(round(max(ys))),
    )


def parse_ocr_spans(raw_results, min_confidence=0.5):
    """把 easyocr 原始输出转为过滤后的 OcrSpan 列表。"""
    spans = []
    for points, text, confidence in raw_results:
        text = (text or "").strip()
        if not text or float(confidence) < min_confidence:
            continue
        left, top, right, bottom = _bounds_from_points(points)
        center = ((left + right) // 2, (top + bottom) // 2)
        spans.append(
            OcrSpan(
                text=text,
                confidence=float(confidence),
                center=center,
                bounds=(left, top, right, bottom),
            )
        )
    return spans


def _strip_progress(text):
    """去掉文本中的 (x/y) 进度尾巴与括号，返回归一化标题。"""
    without_progress = PROGRESS_RE.sub("", text)
    for bracket in ("(", ")", "（", "）"):
        without_progress = without_progress.replace(bracket, "")
    return without_progress.strip()


def _progress_in_text(text, target):
    """从文本中抽取 x/target 的 x（0..target）；分母不符或越界返回 None。"""
    match = PROGRESS_RE.search(text)
    if not match:
        return None
    numerator, denominator = int(match.group(1)), int(match.group(2))
    if denominator != target or not (0 <= numerator <= target):
        return None
    return numerator


def find_immersive_target(
    spans,
    screen_size,
    row_band_ratio=0.04,
    min_confidence=0.5,
):
    """定位标题精确为“好物沉浸看”的行，抽取 x/5 并配对同一行的动作按钮。

    任何歧义（标题不唯一、进度缺失、同行按钮不唯一、置信度不足）均返回 None。
    """
    _, screen_h = screen_size
    band = screen_h * row_band_ratio

    title_spans = [
        span for span in spans if _strip_progress(span.text) == IMMERSIVE_TITLE
    ]
    if len(title_spans) != 1:
        return None
    title = title_spans[0]

    progress = _progress_in_text(title.text, target=5)
    if progress is None:
        return None

    title_cx, title_cy = title.center
    candidates = [
        span
        for span in spans
        if span.text in ACTION_WORDS
        and abs(span.center[1] - title_cy) <= band
        and span.center[0] > title_cx
    ]
    if len(candidates) != 1:
        return None
    action = candidates[0]

    confidence = min(title.confidence, action.confidence)
    if confidence < min_confidence:
        return None

    return ImmersiveTarget(
        title=IMMERSIVE_TITLE,
        progress_text=f"{progress}/5",
        title_center=title.center,
        action_text=action.text,
        action_center=action.center,
        confidence=confidence,
    )


def find_immersive_progress(spans, target=5):
    """读取精确“好物沉浸看”标题段内嵌的 x/target 进度（不依赖动作按钮）。

    用于完成态（按钮已变“已完成”）也能读到 5/5。标题必须恰好一个且精确匹配；
    进度需内嵌于标题段（如“好物沉浸看(5/5)”）。歧义、缺失或分母不符均返回 None。
    """
    title_spans = [
        span for span in spans if _strip_progress(span.text) == IMMERSIVE_TITLE
    ]
    if len(title_spans) != 1:
        return None
    return _progress_in_text(title_spans[0].text, target)


def titles_equivalent(left, right):
    """标题等价：精确相等，或由已注册任务声明为同一轮换身份。"""
    a = _strip_progress(left)
    b = _strip_progress(right)
    if a == b:
        return True
    profile = profile_for_title(a)
    return profile is not None and profile.same_identity(a, b)


def _read_progress_pair(text):
    """读任意 x/y 进度，返回 (x, y)；分母<=0 或 x 越界返回 None。"""
    match = PROGRESS_RE.search(text or "")
    if not match:
        return None
    numerator, denominator = int(match.group(1)), int(match.group(2))
    if denominator <= 0 or not (0 <= numerator <= denominator):
        return None
    return (numerator, denominator)


def _is_reward_text(text):
    """奖励数值不是可证明任务类型的描述证据。

    仅当文本整体仅由货币字符/加减符号/数字/空白组成时才视为 reward；
    任何汉字都意味着这是“描述 + 奖励数字”合并段（如 OCR 误读出
    “浏览(+35”、“签到+50”），不能整段当 reward 排除。
    """
    if not text:
        return False
    return (
        REWARD_RE.match(text) is not None
        and re.fullmatch(r"[\s币金币+＋\-\d]+", text) is not None
    )


def _row_spans_for_title(spans, title, band):
    """返回与标题段同在一行（纵坐标带宽内）的全部 OCR 段。"""
    return [
        span for span in spans
        if abs(span.center[1] - title.center[1]) <= band
    ]


def _evaluate_browse_row(
    title,
    spans,
    band,
    min_confidence=0.5,
):
    """单行安全判定（execute 与 dry-run 唯一共享源）。

    按锁定优先级 external_app_marker → unsafe_marker → unsupported_task →
    missing_description_evidence → progress_unreadable → action_not_unique →
    row_unreadable → supported 逐级判定；返回 ``(DryRunDecision,
    BrowseTarget | None)``，仅当 reason 为 ``supported`` 时才返回目标。
    decision 只保存标准键与脱敏标签，绝不写入原始标题或 OCR 原文。
    """
    stripped = _strip_progress(title.text)
    profile = profile_for_title(stripped)
    if profile is not None:
        task_key = TASK_KEYS[profile.key]
        label = profile.safe_label()
    else:
        task_key = None
        label = UNKNOWN_TASK_LABEL

    row_spans = _row_spans_for_title(spans, title, band)
    row_text = " ".join(span.text for span in row_spans)

    progress = None
    total = None
    action = None
    confidence = None
    if any(marker in row_text for marker in EXTERNAL_APP_MARKERS):
        reason = "external_app_marker"
    elif any(marker in row_text for marker in UNSAFE_BROWSE_MARKERS):
        reason = "unsafe_marker"
    elif profile is None:
        reason = "unsupported_task"
    else:
        description_text = " ".join(
            span.text
            for span in row_spans
            if span is not title
            and span.text not in ACTION_WORDS
            and not _is_reward_text(span.text)
        )
        if not profile.accepts_row(description_text):
            reason = "missing_description_evidence"
        else:
            pair = _read_progress_pair(title.text)
            if pair is None:
                reason = "progress_unreadable"
            else:
                progress, total = pair
                actions = [
                    span for span in row_spans
                    if span.text in ACTION_WORDS
                    and span.center[0] > title.center[0]
                ]
                if len(actions) != 1:
                    reason = "action_not_unique"
                else:
                    action = actions[0]
                    confidence = min(title.confidence, action.confidence)
                    if confidence < min_confidence:
                        reason = "row_unreadable"
                    else:
                        reason = "supported"

    decision = DryRunDecision(
        task_key=task_key,
        label=label,
        status="supported" if reason == "supported" else "skipped",
        reason=reason,
        progress=progress if reason == "supported" else None,
        total=total if reason == "supported" else None,
    )
    if reason != "supported":
        return decision, None
    return decision, BrowseTarget(
        title=stripped,
        progress=progress,
        total=total,
        title_center=title.center,
        action_text=action.text,
        action_center=action.center,
        confidence=confidence,
    )


def find_safe_browse_target(
    spans,
    screen_size,
    only_titles=None,
    row_band_ratio=0.04,
    min_confidence=0.5,
    exclude_titles=(),
):
    """返回最靠上的已注册安全浏览任务候选（支持任意分母）。

    保留既有筛选/排序语义：exclude_titles 排除等价轮换身份，only_titles 仅
    收窄已注册候选、不能授予支持；行级安全判定全部交由共享 evaluator，
    只收集 evaluator 返回的 target。无候选返回 None。
    """
    screen_h = screen_size[1]
    band = screen_h * row_band_ratio
    candidates = []
    for title in spans:
        stripped = _strip_progress(title.text)
        if any(titles_equivalent(stripped, t) for t in exclude_titles):
            continue
        if only_titles is not None and not any(
            titles_equivalent(stripped, t) or stripped.startswith(t)
            for t in only_titles
        ):
            continue
        _decision, target = _evaluate_browse_row(
            title, spans, band, min_confidence=min_confidence
        )
        if target is not None:
            candidates.append(target)
    if not candidates:
        return None
    return min(candidates, key=lambda item: item.title_center[1])


def inspect_visible_task_rows(
    spans,
    screen_size,
    row_band_ratio=0.04,
    min_confidence=0.5,
):
    """单屏 dry-run 行判定（纯函数：只解析传入 OCR，不接受设备/回调）。

    以“进度段 + 同行动作段”识别可报告行：段文本含 x/y 进度且同行存在动作词；
    按纵坐标排序、同一行（带宽内）去重。每行经共享 evaluator 判定，未知行只
    输出 ``未知任务``；返回的 decision 绝不保存原始标题或 OCR 原文。
    """
    screen_h = screen_size[1]
    band = screen_h * row_band_ratio
    anchors = [
        span
        for span in spans
        if PROGRESS_RE.search(span.text) is not None
        and any(
            other.text in ACTION_WORDS
            and abs(other.center[1] - span.center[1]) <= band
            for other in spans
        )
    ]
    anchors.sort(key=lambda span: span.center[1])
    decisions = []
    last_anchor_y = None
    for anchor in anchors:
        if (
            last_anchor_y is not None
            and abs(anchor.center[1] - last_anchor_y) <= band
        ):
            continue
        decision, _target = _evaluate_browse_row(
            anchor, spans, band, min_confidence=min_confidence
        )
        decisions.append(decision)
        last_anchor_y = anchor.center[1]
    return decisions


def read_safe_browse_progress(spans, title):
    """按标题（含轮换前缀等价）读该任务自身 x/y；不唯一/缺失返回 None。"""
    title_spans = [
        span for span in spans
        if titles_equivalent(_strip_progress(span.text), title)
    ]
    if len(title_spans) != 1:
        return None
    return _read_progress_pair(title_spans[0].text)


def find_progress_value(spans, target=5):
    """在所有段中找唯一的 x/target 进度并返回 x；不唯一或缺失返回 None。"""
    values = [
        value
        for span in spans
        for value in [_progress_in_text(span.text, target)]
        if value is not None
    ]
    return values[0] if len(values) == 1 else None


def ocr_has_risk(spans):
    """任一段文本命中风控词即判为风险页（失败关闭用）。"""
    return any(word in span.text for span in spans for word in RISK_WORDS)


def is_safe_tap_point(center, screen_size, top_guard_ratio=0.03,
                      bottom_guard_ratio=0.03):
    """校验点击坐标落在屏幕内、且避开顶部状态栏与底部导航栏区域。"""
    cx, cy = center
    width, height = screen_size
    if not (0 < cx < width):
        return False
    return height * top_guard_ratio < cy < height - height * bottom_guard_ratio


def needs_search_first(title):
    """该任务是否需先触发一次搜索再浏览（如“搜一搜你心仪的宝贝”）。"""
    stripped = _strip_progress(title)
    profile = profile_for_title(stripped)
    return (
        profile is not None
        and profile.strategy == "search_discovery_browse"
    )


def is_search_entry_page(spans):
    """是否停在搜索入口页（图2）：出现“搜索发现/历史搜索”等入口锦点。"""
    return any(
        anchor in span.text
        for span in spans
        for anchor in SEARCH_ENTRY_ANCHORS
    )


def is_search_result_feed(spans):
    """是否已进入“搜索后浏览得币”结果页（图3）：出现“可领”奖励条。

    回归（2026-08-29）：徽标“浏览N秒可领”同时出现在发现入口页，入口页被
    误判为结果页会让策略在入口页空滑、跳过关键词浏览。因此结果页判定必须
    排除入口页。
    """
    return any(
        SEARCH_RESULT_ANCHOR in span.text for span in spans
    ) and not is_search_entry_page(spans)


BROWSE_BADGE_RE = re.compile(r"浏览(\d+)秒可领")


def parse_browse_badge(spans, min_confidence=0.5):
    """解析任务徽标“浏览N秒可领”，返回要求的浏览秒数；无徽标返回 None。

    真机 2026-08-29 实测：结果页顶部右侧徽标“浏览25秒可领”（25 为要求秒数，
    徽标在计时满足后消失）。用 search 而非 fullmatch：徽标文本可能带
    “金币”等后缀；置信度过低或文本无关时返回 None。
    """
    for span in spans or []:
        if span.confidence < min_confidence:
            continue
        match = BROWSE_BADGE_RE.search(span.text.strip())
        if match:
            return int(match.group(1))
    return None


def _is_search_noise(text):
    """发现栏里的区块名/卡片元信息/广告（不是可搜索的词）。"""
    return "%" in text or any(marker in text for marker in SEARCH_NOISE_MARKERS)


HOME_NAV_MARKERS = ("我的淘宝", "购物车", "消息", "视频")


def is_home_feed_screen(spans, min_markers=2):
    """淘宝首页判据：底部导航稳定标记（精确匹配）至少 min_markers 个。

    仅用于排除：回退动作绝不允许把首页当作流程页按下返回。
    """
    texts = {span.text.strip() for span in spans or []}
    hits = sum(1 for marker in HOME_NAV_MARKERS if marker in texts)
    return hits >= min_markers


def is_coin_task_product_page(spans):
    """淘金币折扣商品卡（看看#卡片页/搜索结果页）。

    真机 2026-08-29：卡片带“金币已抵”，搜索结果页 OCR 作“金币己抵”（己/已
    变体都收）；淘宝首页商品卡没有该标签。用作回退路径的流程页身份。
    """
    if is_home_feed_screen(spans):
        return False
    for span in spans or []:
        text = span.text.strip()
        if "金币已抵" in text or "金币己抵" in text:
            return True
    return False


def is_search_flow_page(spans, min_confidence=0.9):
    """搜索任务流程页（发现入口页/结果页，含徽标已消失的结果页）。

    真机 2026-08-29：结果页徽标消失后只剩“搜索”按钮与商品卡，若不识别
    会导致回退把手机留在原地。必须排除淘宝首页（底部导航标记），防止在
    首页误按返回。
    """
    if is_home_feed_screen(spans):
        return False
    for span in spans or []:
        if (
            span.text.strip() == "搜索"
            and span.confidence >= min_confidence
        ):
            return True
    return False


def page_fingerprint(spans):
    """页面识别指纹：诊断用布尔/计数信号，绝不携带 OCR 原文（隐私合同）。"""
    safe_spans = list(spans or [])
    texts = [span.text.strip() for span in safe_spans]
    return {
        "span_count": len(safe_spans),
        "is_result_feed": is_search_result_feed(safe_spans),
        "is_entry_page": is_search_entry_page(safe_spans),
        "is_flow_page": is_search_flow_page(safe_spans),
        "has_badge": parse_browse_badge(safe_spans) is not None,
        "has_coin_title": any("淘金币" in text for text in texts),
        "has_popup_title": any("赚金币抵钱" in text for text in texts),
        "has_search_button": any(text == "搜索" for text in texts),
    }


def is_product_detail_page(spans):
    """是否已进商品详情页：出现加购/立即购买等底栏锦点（只识别、绝不点）。"""
    return any(
        anchor in span.text
        for span in spans
        for anchor in PRODUCT_DETAIL_ANCHORS
    )


# 淘宝首页特征：顶部 tab 锚点（关注/推荐/闪购 等至少 2 个命中）+ 底栏导航（视频/消息/购物车/我的淘宝 至少 2 个命中）。
# "领淘金币"图标作为强信号加分（用于自动导航到淘金币根页），但不是必须
# （弹窗/子页可能遮挡首页 tab）。
TAOBAO_HOME_TAB_ANCHORS = ("关注", "推荐", "闪购", "盒马", "国补", "飞猪")
TAOBAO_HOME_FOOTER_ANCHORS = ("视频", "消息", "购物车", "我的淘宝")
COIN_ENTRY_BUTTON = "领淘金币"


def is_taobao_home_page(spans):
    """是否在淘宝首页（不是淘金币根页，不是子页/详情页）：顶 tab + 底栏双命中。

    返回 (is_home, has_coin_entry)：has_coin_entry 为 True 时表示屏幕上
    能看到"领淘金币"图标，可作为自动导航到淘金币根页的入口。
    """
    if not spans:
        return False, False
    texts = [span.text for span in spans]
    tab_hits = sum(1 for tab in TAOBAO_HOME_TAB_ANCHORS if any(tab in t for t in texts))
    footer_hits = sum(1 for anchor in TAOBAO_HOME_FOOTER_ANCHORS if any(anchor == t for t in texts))
    has_coin_entry = COIN_ENTRY_BUTTON in texts
    # 双命中：顶 tab ≥ 2 AND 底栏 ≥ 2 → 强信号是首页
    # 单命中：仅靠"领淘金币"图标，可能在弹窗/子页也露出（弱信号，不视为首页）
    return (tab_hits >= 2 and footer_hits >= 2), has_coin_entry


def find_result_product_candidates(spans, screen_size,
                                   top_ratio=0.30, bottom_ratio=0.85,
                                   min_len=6):
    """返回搜索结果页可点进详情的商品标题候选（按 y 升序）。

    商品标题特征：位于奖励条/搜索框之下（top_ratio 以下）、屏幕中下部安全区内、
    文本较长、不含价格/促销/奖励条噪声词。点其中心即进入对应商品详情页。
    """
    width, height = screen_size
    candidates = [
        span for span in spans
        if height * top_ratio < span.center[1] < height * bottom_ratio
        and len(span.text) >= min_len
        and not any(marker in span.text for marker in RESULT_TILE_NOISE)
        and is_safe_tap_point(span.center, screen_size)
    ]
    return sorted(candidates, key=lambda span: span.center[1])


def find_discovery_candidates(spans, screen_size):
    """返回"搜索发现"栏下方可随机点击的真实推荐词候选。

    以"搜索发现"区块标题的 y 为界，取其严格下方、落在安全点击区、且非广告/区块
    元信息噪声的段；随机点其一即对该词发起搜索并进入结果流。无区块标题时返回 []。
    """
    anchors = [s for s in spans if SEARCH_DISCOVERY_ANCHOR in s.text]
    if not anchors:
        return []
    header_y = min(anchor.center[1] for anchor in anchors)
    return [
        span for span in spans
        if span.center[1] > header_y
        and SEARCH_DISCOVERY_ANCHOR not in span.text
        and not _is_search_noise(span.text)
        and is_safe_tap_point(span.center, screen_size)
    ]


# ---------------------------------------------------------------------------
# P0-1 两帧安全边界：结构化区域 + 第一行限定 + 第二帧重定位。
# 设计来自 Codex 第二轮核验（DiscoveryRegion / DiscoveryCandidate）。
# ---------------------------------------------------------------------------

_MIN_DISCOVERY_CONFIDENCE = 0.70   # 候选最低置信度（低于视为不可靠）
_ROW_TOLERANCE_RATIO = 0.03        # "第一行"聚类容差（占屏高比例）
_REVALIDATE_SHIFT_RATIO = 0.12     # 第二帧重定位最大位移（占屏宽比例，抗轻微位移/动画）


@dataclass(frozen=True)
class DiscoveryRegion:
    """"搜索发现"区域边界（锚点下方第一行文本带），不含任何 OCR 原文。"""

    header_y: float
    top_y: float
    bottom_y: float


@dataclass(frozen=True)
class DiscoveryCandidate:
    """单个候选词：仅在本次两帧匹配的内存中使用，绝不写入事件/日志。"""

    text: str
    center: tuple
    bbox: tuple
    confidence: float


def locate_discovery_region(spans, screen_size):
    """定位"搜索发现"区域：锚点必须唯一，区域 = 锚点下方第一个文本行。

    第一行按屏高比例容差聚类；第一行整行不可用时返回仅含上界信息的
    region（候选函数会因下界过窄返回空）——绝不向下继续寻找商品行。
    锚点缺失或不唯一返回 None。
    """
    _width, height = screen_size
    anchors = [s for s in spans if SEARCH_DISCOVERY_ANCHOR in s.text]
    if len(anchors) != 1:
        return None
    header_y = anchors[0].center[1]
    below = [
        s for s in spans
        if s.center[1] > header_y
        and SEARCH_DISCOVERY_ANCHOR not in s.text
    ]
    if not below:
        return DiscoveryRegion(
            header_y=header_y, top_y=header_y, bottom_y=header_y
        )
    tolerance = height * _ROW_TOLERANCE_RATIO
    first_row_y = min(s.center[1] for s in below)
    row_spans = [
        s for s in below if abs(s.center[1] - first_row_y) <= tolerance
    ]
    bottom_y = max(s.bounds[3] for s in row_spans)
    return DiscoveryRegion(
        header_y=header_y, top_y=header_y, bottom_y=bottom_y
    )


def find_discovery_candidates(spans, screen_size):
    """返回"搜索发现"栏下方可随机点击的真实推荐词候选（区域受限版）。

    在 locate_discovery_region 的第一行边界内取候选：交易/噪声排除 +
    安全点击区 + 最低置信度。第一行全部不安全时返回空（不向下找）。
    """
    region = locate_discovery_region(spans, screen_size)
    if region is None:
        return []
    width, height = screen_size
    row_tolerance = height * _ROW_TOLERANCE_RATIO
    return [
        span for span in spans
        if region.top_y < span.center[1] <= region.bottom_y + row_tolerance
        and span.confidence >= _MIN_DISCOVERY_CONFIDENCE
        and SEARCH_DISCOVERY_ANCHOR not in span.text
        and not _is_search_noise(span.text)
        and is_safe_tap_point(span.center, screen_size)
    ]


def _normalize_ocr_text(text):
    """OCR 文本标准化：去空白与全角/半角标点，只留中英文数字（抗两帧抖动）。"""
    return re.sub(r"[\s\u3000，。、！？；：""''（）《》【】\-—_—·~!?,.;:()\[\]{}|/\\<>\"']+", "", text)


def revalidate_discovery_candidate(original, fresh_spans, screen_size):
    """第二帧重定位：同一词必须唯一且位置接近；否则 None（零点击）。

    真机 OCR 两帧存在抖动（标点/空格/个别字符），故匹配用标准化文本 +
    相似度（>=0.8）而非严格相等；位置容差为屏宽 12%（抗轻微位移/动画）。
    返回第二帧新坐标（不复用第一帧旧坐标）；消失、歧义、移位过大、
    坐标不安全均返回 None。
    """
    width, _height = screen_size
    max_shift = width * _REVALIDATE_SHIFT_RATIO
    norm = _normalize_ocr_text(original.text)
    if not norm:
        return None
    candidates = [
        s for s in fresh_spans
        if SequenceMatcher(None, norm, _normalize_ocr_text(s.text)).ratio() >= 0.8
    ]
    if len(candidates) != 1:
        return None  # 消失或歧义（无匹配 / 多条近似）
    fresh = candidates[0]
    if (abs(fresh.center[0] - original.center[0]) > max_shift
            or abs(fresh.center[1] - original.center[1]) > max_shift):
        return None  # 移位过大
    if not is_safe_tap_point(fresh.center, screen_size):
        return None
    return DiscoveryCandidate(
        text=fresh.text,
        center=fresh.center,
        bbox=fresh.bounds,
        confidence=fresh.confidence,
    )



COIN_BALANCE_ANCHOR = "淘金币"
_BALANCE_ROW_TOLERANCE = 40  # 余额数字与锚点的同行 y 容差


def parse_coin_balance(spans):
    """解析淘金币首页余额（只读纯函数）。

    锚点 = 含"淘金币"的 span；取同行（y 容差内）最近的含数字 span 的
    首位 4+ 位数字（余额通常 >=4 位）；锚点自身含数字也接受。
    返回 int（金币数）或 None。
    """
    anchors = [s for s in spans if COIN_BALANCE_ANCHOR in s.text]
    if not anchors:
        return None
    anchor = anchors[0]
    same_row = [
        s for s in spans
        if abs(s.center[1] - anchor.center[1]) <= _BALANCE_ROW_TOLERANCE
        and re.search(r"\d", s.text)
    ]
    for s in sorted(
        same_row, key=lambda s: abs(s.center[0] - anchor.center[0])
    ):
        m = (re.search(r"(\d{4,}[\d,]*)", s.text)
             or re.search(r"(\d[\d,]*)", s.text))
        if m:
            value = int(m.group(1).replace(",", ""))
            if value > 0:
                return value
    m = re.search(r"(\d[\d,]*)", anchor.text)
    return int(m.group(1).replace(",", "")) if m else None


def screen_text_signature(spans):
    """当前屏的文本指纹（顺序/置信度无关），用于判断滚动后是否到底（无变化）。"""
    return frozenset(span.text for span in spans)


def _find_until_scroll_stable(probe, scroll, max_scrolls=8):
    """滚动查找直到命中或连续两次滑动后签名不变。"""
    last_signature = None
    unchanged_swipes = 0
    for attempt in range(max_scrolls + 1):
        found, signature = probe()
        if found is not None:
            return found
        if signature == last_signature:
            unchanged_swipes += 1
        else:
            unchanged_swipes = 0
        last_signature = signature
        if unchanged_swipes >= 2:
            return None
        if attempt < max_scrolls:
            scroll()
    return None


def locate_by_scroll(probe, scroll, max_scrolls=8):
    """通用滚动查找控制器（纯逻辑，靠回调驱动，便于离线测试）。

    - ``probe() -> (found, signature)``：``found`` 非 None 即命中目标并返回；
      ``signature`` 为当前屏文本指纹。
    - ``scroll()``：执行一次滚动（副作用）。
    连续两次滑动后屏幕指纹仍相同（判定到底）或超过 ``max_scrolls`` 仍未命中，
    则失败关闭返回 None。最多 probe ``max_scrolls + 1`` 次、scroll ``max_scrolls`` 次。
    """
    return _find_until_scroll_stable(probe, scroll, max_scrolls=max_scrolls)


def scroll_to_top_then_find(probe, scroll_up, scroll_down, max_scrolls=8):
    """先上滚到顶，再从顶向下逐屏查找目标（全覆盖，避免漏掉上方任务）。

    - ``probe() -> (found, signature)``：found 非 None 立即返回（两个阶段都机会命中）。
    - ``scroll_up()`` / ``scroll_down()``：一次上滚 / 下滚（副作用）。
    连续两次滑动后签名仍相同才视为到顶/到底。任一阶段命中即返回 found；
    全程未命中返回 None。
    """
    # 阶段1：上滚到顶；阶段2：从顶向下逐屏查找。
    found = _find_until_scroll_stable(probe, scroll_up, max_scrolls=max_scrolls)
    if found is not None:
        return found
    return _find_until_scroll_stable(probe, scroll_down, max_scrolls=max_scrolls)
