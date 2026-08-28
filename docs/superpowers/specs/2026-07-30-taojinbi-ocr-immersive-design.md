# 淘金币“好物沉浸看”OCR 方案设计

## 背景与根因

此前 `codex/immersive-browse-fix` 分支将任务识别从 OCR/坐标重构为解析
`dump_hierarchy()` 控件树。离线 80 个单元测试全部通过，但真机只读验证证明该方案
在目标页面上**根本无法工作**：

1. 淘金币任务弹窗（“赚金币抵钱”）位于 `com.taobao.themis.container.app.TMSActivity`，
   其内容由 `com.uc.webview.export.WebView` 渲染的 H5 页面绘制。
2. dump 出来的 WebView 子节点全部为空壳：`text=""`、`content-desc=""`、
   `resource-id=""`，且标记 `NAF="true"`（Not Accessibility Friendly）。
3. 因此“好物沉浸看”“去完成”“0/5”等内容**完全不暴露给 Android 无障碍树**，
   `dump_hierarchy()` 看不到任何任务文本或控件。旧方案在真机上永远失败关闭、什么都不做。

对同一屏截图跑 easyocr（`ch_sim+en, gpu=False`）的只读验证结果：

- 初始化 1.6s，单次识别 4.9s，共 45 段文字；
- **“好物沉浸看(0/5)” 置信度 0.99**，中心 `(391, 1707)`；
- 同行“去完成”按钮 0.97，中心 `(943, 1739)`；
- “浏览 +35” 识别到，`(327, 1770)`；进度 `0/5` 可直接从标题段正则抽取。

结论：**WebView 内容对 hierarchy 不可见，但 OCR（截图像素识别）可稳定看见并定位。**
故本设计改用 OCR + 动态坐标，同时保留上一轮重构中经过测试的纯逻辑安全核心。

## 目标

- 用 OCR 稳定识别当前屏上的“好物沉浸看”任务行、其 `x/5` 进度、以及同行“去完成”按钮；
- 用**从 OCR 边界框动态计算**的坐标点击“去完成”，进入沉浸页；
- 在沉浸页会话内推进 `0/5 → 5/5`，且**只有 OCR 观察到进度真实增长才记一次成功**；
- 复用现有 `taojinbi_task_core.py` 的进度状态机与安全判定；
- 无法可靠识别、进度不增长、离开淘宝或出现风控页时，安全停止并返回任务列表。

## 非目标

- 不放宽到“看视频得红包”“下单最高+10000”“评价得金币”“玩游戏”等交易/互动任务；
- 不执行外部应用（头条、支付宝、天猫、京东、抖音等）任务；
- 不写死任何屏幕坐标（坐标必须每次从 OCR 边界框动态计算并按分辨率校验）；
- 不绕过验证码/风控；
- 一次只聚焦“好物沉浸看”这一个任务跑通，不在本设计内一次性迁移全部任务；
- 不推送 GitHub、不创建 PR、不提交任何截图临时文件。

## 方案选择

采用**方案 A：OCR 识别 + 动态坐标点击 + 复用纯逻辑安全核心**。

明确推翻上一版设计的非目标“不增加 OCR/截图坐标点击”——因为该约束建立在“任务内容可
从控件树读取”的错误前提上，已被真机证伪。新方案把风险控制点从“禁用 OCR”转移到
“OCR 结果必须高置信、精确匹配、动态坐标、逐步失败关闭”。

不采用：

- 继续用 hierarchy 解析：目标内容在 WebView 内，物理上读不到；
- WebView 调试通道（CDP/JS 注入）或 mtop 接口：复杂度高、易触发风控，超出当前范围。

## 总体架构

分三层，职责单一、可分别测试：

1. **纯逻辑核心（复用，`taojinbi_task_core.py`，不改或极小改）**
   - `run_verified_immersive_progress(read_progress, perform_one, still_allowed, target=5, max_stalls=2)`
   - `consume_task_attempt`、`wait_for_package_name`、`UNSAFE_ACTION_MARKERS` 等。
2. **OCR 解析层（新增 `taojinbi_ocr_ui.py`，纯函数，无设备、不导入 easyocr）**
   - 输入：easyocr `readtext` 的原始输出（`[(bbox_points, text, conf), ...]`）与屏幕尺寸；
   - 输出：过滤后的文本段、定位到的“好物沉浸看”目标、进度值；
   - 全部为纯函数，用**真实 OCR 样本**作为测试夹具。
3. **设备坐标层（在 `淘金币任务.py` 内新增薄封装，唯一触碰真机的地方）**
   - 截图、调用 easyocr、把解析层结果转成动态坐标点击，并做包名/风控/进度校验。

## 数据结构

```python
@dataclass(frozen=True)
class OcrSpan:
    text: str
    confidence: float
    center: tuple[int, int]              # (cx, cy) 像素
    bounds: tuple[int, int, int, int]    # (left, top, right, bottom)

@dataclass(frozen=True)
class ImmersiveTarget:
    title: str                 # 归一化后应等于 "好物沉浸看"
    progress_text: str         # "0/5"
    title_center: tuple[int, int]
    action_text: str           # "去完成"
    action_center: tuple[int, int]   # 配对到的按钮中心（点击点）
    confidence: float          # 取行内标题与按钮置信度的较小值
```

## 组件设计（OCR 解析层，纯函数，全部可离线测试）

### 1. 结果规整与过滤

```python
def parse_ocr_spans(raw_results, min_confidence=0.5) -> list[OcrSpan]:
    """把 easyocr.readtext 输出转为 OcrSpan，丢弃低于阈值或空文本的段。"""
```

- easyocr bbox 是四个点，转成 `(left, top, right, bottom)` 与中心；
- 低置信度（默认 < 0.5）或纯符号/空白文本直接丢弃，避免噪声影响决策。

### 2. 目标定位与“行内按钮配对”

```python
IMMERSIVE_TITLE = "好物沉浸看"
ACTION_WORDS = ("去完成", "去逛逛", "去浏览", "去看看")

def find_immersive_target(
    spans, screen_size, row_band_ratio=0.04
) -> ImmersiveTarget | None:
    """定位标题精确为“好物沉浸看”的行，抽取 x/5，并配对同一行的动作按钮。"""
```

规则（全部失败关闭）：

- 在 spans 中找“去掉尾部 `(x/y)` 后精确等于 `好物沉浸看`”的标题段；必须**恰好一个**；
- 从该标题段（或其紧邻段）用 `(\d+)\s*/\s*(\d+)` 抽取进度，要求分母等于 5；
- 动作按钮：在文本 ∈ `ACTION_WORDS` 的段里，取满足
  `|cy_action - cy_title| <= screen_h * row_band_ratio` 且 `cx_action > cx_title`
  （按钮在标题右侧）的段；要求这样的段**恰好一个**，否则返回 `None`；
- 组合置信度取标题与按钮的较小值；任一低于阈值则返回 `None`。

真实样本验证该规则可行：标题 `cy=1707`、按钮 `cy=1739`（差 32px），下一个“去完成”
在 `cy=1544`（差 163px）；`row_band_ratio=0.04`（1920×0.04≈77px）可干净隔离唯一按钮。

### 3. 进度读取（沉浸页与列表页通用）

```python
def find_progress_value(spans, target=5) -> int | None:
    """在 spans 中找唯一形如 x/target 的进度并返回 x（0..target）；不唯一或越界则 None。"""
```

### 4. 风控/离开检测（OCR 版）

```python
RISK_WORDS = ("验证码", "安全验证", "滑块验证", "人机验证", "访问受限",
              "操作频繁", "账号异常", "风险控制", "风控")

def ocr_has_risk(spans) -> bool:
    """任一段文本命中风控词即判为风险页（失败关闭）。"""
```

## 组件设计（设备坐标层，`淘金币任务.py`，唯一触碰真机处）

```python
def ocr_screen(d, reader, min_confidence=0.5) -> list[OcrSpan]:
    # 截图到临时文件（单一复用文件，用完即删，绝不提交）
    # raw = reader.readtext(path); return parse_ocr_spans(raw, min_confidence)

def in_taobao_and_safe(d, spans) -> bool:
    # get_current_app(d) 包名 == TB_APP 且 not ocr_has_risk(spans)

def safe_tap(d, center, screen_size) -> bool:
    # 校验 center 落在屏幕内、且不在状态栏/导航栏区域（顶部/底部安全边距）
    # 通过后 d.click(cx, cy)；否则返回 False 不点击
```

沉浸任务处理器（复用核心状态机）：

```python
def run_immersive_goods_task_ocr(d, reader, back_to_task):
    # 真机观察确认（2026-07-30）：
    # - 点“去完成”进入商品详情信息流（NewDetailActivity），不是独立沉浸页；
    # - 每个商品需“停留足够时长”才计入一次 x/5（实测停留 ~6s 不计入，需更久）；
    # - “上滑”翻到下一个商品且仍停留在 NewDetailActivity 内；
    # - 进度 x/5 只在任务列表弹窗可见，详情页读不到（详情页 read_progress 恒为 None）；
    # - 详情页存在危险元素：加入购物车/立即购买/下单再得500/关注/“直播中·去逛逛”，
    #   全程只用“停留 + 上滑”两种动作，天然不触碰这些按钮。

    def read_progress():          # 只在任务列表弹窗调用
        # 标题锚定、按钮无关：完成态（按钮变“已完成”）也能读到 5/5；
        # 精确“好物沉浸看”标题，绝不误读其它 x/5 任务（如“看看#王者荣耀代练(1/5)”）
        value = locate_immersive_progress(d, reader, screen)  # 滚动查找 + find_immersive_progress
        return f"{value}/5" if value is not None else None

    def still_allowed():
        return in_taobao_and_safe(d, ocr_screen(d, reader))

    def perform_one():
        # 一次“浏览往返”：在列表点“去完成”进 feed → 停留+上滑覆盖若干商品 → 返回列表。
        # 进 feed 前必须重定位并动态坐标点击“去完成”（见入口函数）。
        if not enter_immersive_from_list(d, reader, back_to_task):
            return False
        for _ in range(BROWSE_PER_ROUND):     # 每轮浏览的商品数（带余量）
            if not still_allowed():
                break
            time.sleep(DWELL_SECONDS)          # 停留让浏览计时完成（保守 ~10s）
            d.swipe(W // 2, int(H * 0.75), W // 2, int(H * 0.30), 0.4)  # 上滑翻页
            time.sleep(SWIPE_SETTLE)
        back_to_task()                          # 退回列表，交由 read_progress 验证
        return True

    return run_verified_immersive_progress(read_progress, perform_one, still_allowed)
```

编排说明：因进度只在列表可见，`run_verified_immersive_progress` 的
`read_progress` 只在“回到列表后”读取 `x/5`；`perform_one` 完成“进 feed →停留+上滑
若干商品→回列表”一个往返；状态机据前后 `x/5` 是否增长判定成功、停滞即停。
`DWELL_SECONDS`（停留时长）与 `BROWSE_PER_ROUND`（每轮商品数）在阶段 3 受控运行中标定。


入口（进入沉浸页）：

```python
def enter_immersive_from_list(d, reader, back_to_task) -> bool:
    spans = ocr_screen(d, reader)
    if not in_taobao_and_safe(d, spans):
        return False
    target = find_immersive_target(spans, d.window_size())
    if target is None:
        return False
    # 点前再截一次、重定位，要求 target 仍唯一稳定（动态坐标，不缓存旧坐标）
    spans2 = ocr_screen(d, reader)
    target2 = find_immersive_target(spans2, d.window_size())
    if target2 is None or target2.progress_text != target.progress_text:
        return False
    return safe_tap(d, target2.action_center, d.window_size())
```

## 安全约束

- **置信度阈值**：任何参与决策的文本必须 ≥ 阈值（默认 0.5，可调）；
- **精确标题**：标题去进度后必须精确等于“好物沉浸看”，不做模糊匹配；
- **危险词优先**：命中 `UNSAFE_ACTION_MARKERS`/`RISK_WORDS` 一律拒绝或停止；
- **动态坐标**：点击点每次从当前 OCR 边界框计算，按 `window_size()` 校验范围，
  绝不写死、不跨屏复用旧坐标；点击前重新截图重定位并要求目标仍唯一；
- **唯一性**：标题与配对按钮都必须唯一，任何歧义即失败关闭；
- **点击后校验**：每次点击后校验包名仍为 `TB_APP` 且非风控页，否则停止并 `back_to_task`；
- **进度证明**：只有 `read_progress` 观察到 `x/5` 真实增长才计成功，连续无增长达上限即停；
- **有界重试**：沿用 `consume_task_attempt` / `have_clicked` 的两次进入上限；
- **不提交临时产物**：截图写入单一临时文件、用完即删，`.gitignore` 兜底。

## 测试策略

- **纯逻辑（OCR 解析层）**：TDD，先写失败测试。夹具使用**真实 easyocr 输出样本**
  （本次已采集到 45 段，含 `('好物沉浸看(0/5)', 0.99, bbox)` 与配对“去完成”），覆盖：
  - 低置信度/空文本过滤；
  - 精确标题匹配、拒绝“好物沉浸看看”等近似；
  - 行内按钮配对唯一性、跨行按钮不误配；
  - 进度抽取与分母校验；风控词命中。
- **纯核心**：`run_verified_immersive_progress` 已有测试，复用。
- **设备/坐标层**：不做单元测试（依赖真机）；通过**只读观察 + 单次受控真机运行**
  （用户在场、可随时中断）验证，全部路径失败关闭。

## 实施路线（观察驱动、分阶段）

> 每阶段先写 RED 测试再实现；只创建本地提交，不推送。

- **阶段 0：OCR 解析层地基**
  - 新增 `taojinbi_ocr_ui.py` + `tests/test_taojinbi_ocr_ui.py`；
  - 实现 `parse_ocr_spans` / `find_immersive_target` / `find_progress_value` /
    `ocr_has_risk`，用真实样本夹具跑通。
- **阶段 1：入口点击（进入沉浸页）** ✅ 已完成（2026-07-30 受控验证）
  - 设备层 `ocr_screen` / `safe_tap` / `in_taobao_and_safe`；
  - 只读定位连测 3 次完全稳定（好物沉浸看 0.99、去完成 (943,1739)）；
  - 单次受控点击“去完成”成功进入 `NewDetailActivity` 商品详情信息流。
- **阶段 2：观察沉浸页并定稿 `perform_one`** ✅ 已完成（2026-07-30 受控观察）
  - 确认：上滑翻商品、停留计入 x/5（返回列表实测 0/5 → 1/5）、进度只在列表可见、
    详情页含加购/购买/下单/关注/直播等危险元素；据此已定稿 `perform_one`（见上）。
- **阶段 3：端到端 `0/5 → 5/5`**（下一步）
  - 实现 `run_immersive_goods_task_ocr` + 入口，标定 `DWELL_SECONDS`/`BROWSE_PER_ROUND`；
  - 受控真机验证进度真实增长直到 5/5，全程失败关闭。


## 风险与开放问题

1. **沉浸页交互已确认**（原开放问题已关闭）：点“去完成”进入商品详情信息流；
   `perform_one` = 停留（让浏览计时完成）+ 上滑翻商品，只用这两种动作。剩余待标定项
   是 `DWELL_SECONDS`（实测 ~6s 不计入，需更久）与每轮商品数，属阶段 3 经验调参。
2. **进度只在列表可见**：详情页读不到 `x/5`，故采用“进 feed 浏览若干→回列表读进度”
   的往返验证；无法逐商品即时验证，只能验证每往返的净增长（仍失败关闭）。
3. **完成态无法自证 5/5**（真机只读实测确认）：任务完成后其列表行不再暴露可读的
   “好物沉浸看(x/5)”文本（按钮变“已完成”，且 OCR 常读不到该行）。因此
   `read_progress` 会返回 None——**绝不据“行消失/出现已完成”谎报完成**（那可能是
   已轮换/移出滚动范围/OCR 漏读）。仅当真读到 `5/5` 才报“已完成”；起始读到过进度、
   浏览后再读不到时，如实报“可能已完成，请核对金币余额”。金币余额是完成的最终佐证。
2. **入口导航**：从淘宝首页到“赚金币抵钱”弹窗的路径可能是原生控件也可能是 WebView，
   需在阶段 1 前顺带确认；若也是 WebView，则入口同样走 OCR。
3. **OCR 稳定性**：识别受分辨率、字体、滚动位置影响；靠置信度阈值 + 精确匹配 +
   点击后进度校验兜底，宁可失败关闭也不误点。
4. **性能**：单次识别约 5s（CPU）；每步一次 OCR 可接受，但需控制循环频率。
5. **其它任务**：本设计只覆盖“好物沉浸看”；若要迁移其余 WebView 任务，可复用解析层
   与设备层，按同样模式扩展（后续单独设计）。

## 扩展：多安全浏览任务泛化（2026-07-30 追加，采用方案 b）

需求升级为“完成淘金币里所有安全浏览任务”，而任务标题按主题轮换
（如 `看看#王者荣耀代练`、`看看#原神`、`好物沉浸看`、`好物精选好货`）。经真机确认
`看看#…` 顶栏为“浏览N秒可领币”，本质仍是“停留浏览得币”（页面是商品网格，动作仍是
停留+滑动，绝不点商品/不“任意下单”）。

**采用方案 b（最终判据：描述含“浏览” + 风险词兜底 + 逐任务完成）**

- **发现（浏览判据，非标题前缀）**：任务“行”（同 y 带聚合文本）描述含“浏览”即候选，
  兼容主题轮换与各种标题（发现精选好物 / 看看#* / 逛逛金币 / 淘金币充话费 / 好物沉浸看…），
  比标题前缀更全（前缀会漏掉“逛逛金币/充话费”）。
- **动作按钮**：`ACTION_WORDS` 含“去完成/去逛逛/去浏览/去看看/逛一逛”（逛逛金币等用“逛一逛”）。
- **风险兜底（防御纵深，用户决策 A：排除带下单的）**：候选行文本含
  `下单/购买/付款/支付/结算/加购/购物车/红包/邀请/助力/充值/外卖/评价/夺宝/盲盒/游戏/消消乐/领券/拉好友`
  等标记一律拒绝——据此排除“快速得大量金币/来淘宝闪购”（带下单）与“玩消消乐/捉妖游戏”（游戏），
  即便它们描述里也写了“浏览”。
- **逐任务**：`find_safe_browse_target` 返回**最靠上**的合格候选（含唯一动作按钮坐标），
  `only_titles=(标题,)` 用于按精确标题定位某任务；`read_safe_browse_progress(spans, title)`
  按精确标题读该任务自身 `x/N`（不依赖按钮，支持任意分母）。设备层 `run_safe_browse_tasks`
  逐个发现→用 `run_verified_immersive_progress` 往返完成→`exclude_titles` 跳过已尝试/停滞的，
  再找下一个。
- **完成态与安全**：沿用既有原则——只在真读到 `N/N` 报完成，读不到诚实回退；
  全程只“停留+上滑+返回”，动态坐标、失败关闭、离开淘宝/风控即停。
- **可变页面**：`看看#` 进入的是商品网格（非单商品详情流），停留+上滑的浏览动作预期
  通用，但**每种新页面类型仍需真机验证一次**。
