# 淘金币公开版运行入口加固设计

日期：2026-08-25

状态：已完成分段设计确认，等待书面规格复核

范围：`--dry-run`、运行时限、UTF-8 结构化日志、稳定退出码、`Ctrl+C` 安全退出

## 1. 背景

三模块注册表里程碑已经完成并合并到本地 `main`。标准 OCR 入口仅支持：

- `搜一搜…`；
- `看看#…`；
- 精确标题 `发现精选好物`。

现有 `run_ocr_entry()` 仍以布尔值表示所有结果，CLI 只有设备、OCR 模式和任务数参数；运行过程没有统一总/单任务时间预算、结构化日志或用户中止合同。本设计在不改变三模块白名单和手势策略的前提下，为公开 Beta 建立可审计、可测试、默认失败关闭的运行入口。

## 2. 目标

1. 提供真机只读 `--dry-run`，识别当前可见任务并解释执行或跳过原因，保证零点击、零滑动、零返回、零领取。
2. 建立 dry-run 2 分钟、单任务 20 分钟、整次运行 30 分钟的协作式 deadline。
3. 同时输出易读中文控制台信息和脱敏 UTF-8 JSON Lines 事件日志。
4. 用稳定退出码区分正常、未确认完成、参数、启动、安全、超时和用户中止。
5. 第一次 `Ctrl+C` 尝试短时安全恢复，第二次立即停止所有设备动作。
6. 所有新增行为以标准库 `unittest`、假设备、假时钟和假日志边界离线验证。

## 3. 非目标

- 不迁移到 `src/coin11_tb`，不处理打包、依赖元数据或 CI。
- 不创建、推送或发布 GitHub 仓库。
- 不增加新的任务模块、OCR 坐标点击、验证码/风控绕过或交易任务。
- 不重写现有 OCR 算法、任务注册表或两种浏览策略。
- 不承诺强制中断一次已经卡死的底层 ADB、OCR 或原生库调用。
- 不进行普通执行模式的真机测试，不领取奖励。
- 不清理私有全量仓库中仍保留的“好物沉浸看”遗留专用路径；公开净导出阶段另行处理。

## 4. 方案选择

采用增量拆分运行合同：保留现有 OCR、任务策略和设备编排文件，新增 outcome、deadline 和 logging 小模块，并扩展现有配置与入口。

未采用的方案：

- 把所有逻辑继续堆入 `taojinbi_ocr_immersive.py`：文件已职责过载，难以独立测试和公开维护。
- 本轮直接迁移最终 `src/coin11_tb` 包：会把运行安全、打包、依赖和双仓库迁移混在一个审查范围内。

## 5. 文件与职责

### 5.1 `taojinbi_runtime_config.py`

继续负责 CLI 参数、默认值和参数校验。新增：

- `--dry-run`；
- `--task-timeout 1200`；
- `--run-timeout 1800`；
- `--dry-run-timeout 120`；
- `--log-dir logs`。

三个 timeout 参数均以秒为单位，必须大于 `0`。现有 `--serial`、`--gpu/--cpu`、`--max-tasks` 保持兼容；`--max-tasks` 仍允许 `0`。

### 5.2 新增 `taojinbi_runtime_outcome.py`

定义运行模式、状态、退出码和统一结果：

- `RunMode`：`dry_run`、`execute`；
- `RunStatus`：`success`、`partial`、`startup_failed`、`safety_stopped`、`timed_out`、`cancelled`；
- `ExitCode`：`0`、`1`、`3`、`4`、`5`、`130`；参数错误 `2` 继续由 `argparse` 产生；
- `RunOutcome`：模式、状态、稳定原因、任务计数和退出码。

计数字段包括：`detected`、`supported`、`skipped`、`attempted`、`completed`、`likely_completed`、`unfinished`。结果对象不保存设备序列号、截图、OCR 原始文本或动态商品名称。

### 5.3 新增 `taojinbi_runtime_deadline.py`

定义基于 `time.monotonic()` 的协作式 deadline：

- 创建固定预算；
- 查询剩余时间；
- 在检查点抛出带 scope 的超时异常；
- 创建受父 deadline 限制的子 deadline；
- 将 sleep 自动截断到剩余预算；
- 注入假 clock 和假 sleeper。

不使用后台线程包裹设备动作，因为线程超时后无法安全停止，可能继续点击。

### 5.4 新增 `taojinbi_runtime_logging.py`

负责中文控制台事件和 UTF-8 JSONL 文件：

- 日志文件名：`logs/taojinbi-<UTC时间>-<短运行ID>.jsonl`；
- `encoding="utf-8"`、`ensure_ascii=False`；
- 每个事件写完立即刷新；
- Windows 控制台支持时将 stdout/stderr 重设为 UTF-8，无法重设时使用安全替代，不因编码错误中断恢复逻辑；
- 日志初始化必须在连接设备前完成；创建失败返回 `startup_failed/log_init_failed` 和退出码 `3`。

`.gitignore` 增加 `logs/`。

### 5.5 `taojinbi_ocr_ui.py`

新增纯函数，用一次 OCR 结果检查当前可见屏幕的所有任务行，返回脱敏 dry-run 判断。它复用现有注册表、危险词、外部应用、描述证据、进度和按钮唯一性规则，但不会持有设备对象或动作回调。

每个结果只包含：标准任务键或 `None`、标准化显示名、`supported/skipped`、稳定原因和非敏感进度字段。未知/危险标题不保存完整 OCR 文本。

### 5.6 `淘金币系/taojinbi_ocr_immersive.py`

继续负责连接、OCR 初始化、任务列表检查和执行编排：

- `run_ocr_entry()` 返回 `RunOutcome`，不再返回布尔值；
- dry-run 路径只调用 screenshot/OCR 和纯行检查函数；
- execute 路径为整次运行和单任务建立 deadline；
- 将 deadline 检查放在循环、等待、OCR、点击和滑动的前后边界；
- 捕获 timeout 与 `KeyboardInterrupt`，按本设计恢复和结束；
- `main()` 返回整数，模块入口使用 `raise SystemExit(main())`。

`git grep` 已确认 `run_ocr_entry()` 的生产调用者只有同文件 `main()`；其他调用均为测试，因此可在本阶段迁移返回类型。

## 6. 总数据流

```text
CLI 参数
  → 初始化 UTF-8 控制台与 JSONL（失败则退出 3，不连接设备）
  → 建立运行 ID 和总/dry-run deadline
  → 连接设备与初始化 OCR
  → 检查淘宝包名和任务列表弹窗
  → dry-run 当前屏只读检查，或 execute 限时执行
  → 生成 RunOutcome
  → 写 run_finished 事件
  → main 返回稳定退出码
```

## 7. Dry-run 合同

### 7.1 前置条件

用户必须手动：

1. 连接 ADB；
2. 打开淘宝；
3. 进入淘金币；
4. 打开“赚金币抵钱”任务弹窗。

dry-run 不自动导航。设备连接、OCR 初始化或任务弹窗检查失败返回退出码 `3`；前台不是淘宝或出现安全边界返回 `4`。

### 7.2 只读能力边界

dry-run 只允许：

- 读取包名/当前活动；
- 获取窗口尺寸；
- 截图；
- OCR；
- 纯文本/坐标分析；
- 写脱敏日志。

dry-run 禁止：

- click/tap；
- swipe/scroll；
- press back；
- 自动打开弹窗；
- 领取奖励；
- 调用任何任务执行策略。

实现上不向 dry-run 函数注入动作能力。测试假设备的任何动作方法一旦被调用就立即失败。

### 7.3 扫描范围

只检查当前可见的一屏，不通过滑动扩展范围。允许对同一静止页面重复截图以处理一次 OCR 失败，但不得改变页面。

逐行输出：

- 支持：`search`、`hashtag` 或 `featured_goods`；
- 跳过：`unsupported_task`、`unsafe_marker`、`external_app_marker`、`missing_description_evidence`、`progress_unreadable`、`action_not_unique`、`row_unreadable` 等稳定原因。

无法可靠读取名称、描述、进度或按钮关系的行默认跳过。没有候选是正常安全空结果，退出 `0`。

### 7.4 隐私

`搜一搜…` 和 `看看#…` 只记录标准任务键与标准化显示名。未知任务只记录原因和计数，不记录标题。动态商品名称、OCR 原始文本、截图和设备序列号不得进入控制台或 JSONL。

## 8. Deadline 合同

### 8.1 预算

- dry-run：`120` 秒；
- 单任务：`1200` 秒；
- 整次 execute：`1800` 秒；
- timeout/中止后的安全恢复：独立 `10` 秒宽限，不延长原任务或整次运行结果。

单任务 deadline 是总 deadline 的子预算，其到期时间不得晚于父 deadline。

### 8.2 检查点

以下边界前后检查剩余时间：

- OCR 截图和识别；
- 任务查找与滚动循环；
- 每次 sleep；
- 每次 click/tap；
- 每次 swipe；
- 返回与刷新恢复；
- 开始下一个任务前。

一旦超时，禁止新的任务点击和浏览动作；只允许在 10 秒宽限内执行“返回任务列表”恢复，然后整次停止并返回 `5`。

### 8.3 限制

deadline 是协作式检查。单次底层 ADB、OCR 或原生库调用若自身卡死，Python 只能在调用返回后确认超时。本阶段不通过线程或强杀子进程制造表面上的硬超时，以免被中止的后台动作继续操作手机。

## 9. Ctrl+C 合同

- execute 第一次收到 `KeyboardInterrupt`：立即禁止新任务动作，最多 10 秒尝试返回任务列表，最终固定退出 `130`。
- 恢复期间再次收到 `KeyboardInterrupt`：立即退出，停止所有设备动作。
- dry-run 收到 `KeyboardInterrupt`：不执行页面恢复，直接退出 `130`。
- 恢复成功或失败不覆盖原始 cancelled 结果。
- 普通 `Exception` 捕获不能吞掉 `KeyboardInterrupt`。

## 10. 日志合同

### 10.1 JSONL 固定字段

每行包含：

- `schema_version`：首版固定为 `1`；
- `timestamp`：UTC ISO 8601；
- `run_id`；
- `level`；
- `event`；
- `mode`；
- `task_key` 或 `null`；
- `phase`；
- `status`；
- `reason`；
- 非敏感 `counts`。

### 10.2 事件集合

- `run_started`；
- `startup_checked`；
- `dry_run_row_decided`；
- `task_started`；
- `task_finished`；
- `recovery_started`；
- `recovery_finished`；
- `run_finished`。

事件字段由集中白名单组装，不允许调用者把任意 OCR 文本或异常消息透传到 JSON。

### 10.3 控制台

控制台继续使用中文摘要，但任务名同样标准化脱敏。异常只记录稳定原因和异常类型，不输出异常正文或本地路径。

公开 CLI 可达路径中的现有 `print()` 必须逐项审计：凡输出任务标题的位置，一律改用由 registry profile 生成的标准化标签，内部用于回定位的原始标题不得传入控制台或 JSONL。不能只对新增事件脱敏而保留旧打印旁路。

## 11. RunOutcome 与退出码

| 退出码 | 状态 | 含义 |
|---:|---|---|
| `0` | `success` | 正常执行完成、dry-run 完成或安全无候选 |
| `1` | `partial` | 已执行任务，但未确认完成 |
| `2` | argparse | 命令参数错误 |
| `3` | `startup_failed` | 日志、设备连接、OCR 初始化或入口页面检查失败 |
| `4` | `safety_stopped` | 前台包名、风险屏幕或其他安全边界停止 |
| `5` | `timed_out` | dry-run、单任务或整次运行超时 |
| `130` | `cancelled` | 用户 `Ctrl+C` 中止 |

优先级：用户中止 > 超时 > 安全停止 > 启动失败 > 未确认完成 > 正常。恢复失败只能追加恢复结果事件，不得改写原始退出原因。

## 12. 测试设计

全部使用标准库 `unittest`，不新增依赖。

### 12.1 配置与 outcome

- 新参数默认值和显式值；
- timeout 非正数拒绝；
- `RunOutcome` 状态、计数与退出码；
- argparse 参数错误保持退出 `2`；
- `main()` 将 outcome 映射为 `SystemExit` 码。

### 12.2 Deadline

- 假时钟下剩余时间；
- 子 deadline 不超过父 deadline；
- sleep 按剩余预算截断；
- dry-run、单任务和整次 scope 的超时原因；
- 无真实 2/20/30 分钟等待。

### 12.3 日志

- 文件是 UTF-8 JSONL；
- 每行可独立解析；
- 固定 schema 和即时 flush；
- 设备序列号、OCR 原文、商品名、本地路径和异常正文不出现；
- 日志目录创建失败时连接函数调用次数为 `0`。

### 12.4 Dry-run

- 当前屏所有可识别任务均产生支持/跳过判断；
- 三模块与移除/危险/外部/证据不足任务；
- 无候选退出 `0`；
- 假设备 click/swipe/back 方法调用次数全部为 `0`；
- 不调用任务执行策略；
- timeout 和 `Ctrl+C` 不产生恢复动作。

### 12.5 Execute 超时与取消

- 单任务超时禁止下一任务；
- 总超时优先限制单任务子预算；
- 超时只允许一次限时恢复；
- 第一次 Ctrl+C 尝试恢复并退出 `130`；
- 恢复期间第二次 Ctrl+C 不再操作设备；
- 安全停止、超时、取消和 partial 不互相误分类。

### 12.6 回归

- 三模块 registry 和 OCR 安全测试；
- 搜一搜不点击商品详情；
- 完整私有离线套件；
- 公开依赖闭包；
- 生产模块 `py_compile`；
- `git diff --check`；
- `utils.py` 无改动；
- 测试期间不连接 ADB、不下载 OCR 模型。

## 13. 实施顺序

1. CLI 参数、退出码和 `RunOutcome`；
2. deadline 纯逻辑；
3. UTF-8 JSONL 与脱敏；
4. dry-run 纯行检查和零动作入口；
5. execute 单任务/整次 deadline；
6. Ctrl+C 两阶段退出；
7. CLI 集成、README 和完整离线验收。

每一项严格 TDD：先提交失败测试证据，再做最小实现，运行聚焦与相关回归，独立审查后提交。

## 14. 真机只读验收

完成全部离线验证并再次取得真机操作许可后，只执行：

1. 确认 ADB device 状态；
2. 用户手动打开目标任务弹窗；
3. 记录执行前页面状态；
4. 运行 `--dry-run`；
5. 对比执行后页面，确认无点击、滑动、返回或奖励领取；
6. 核对标准化候选和跳过原因；
7. 检查 JSONL 不含设备序列号、截图、动态商品名或 OCR 原文。

不在本阶段运行 execute 模式或全量真机任务。

## 15. 完成标准

- dry-run 在类型和调用能力上均无法执行设备动作；
- 三类时间预算和两阶段 Ctrl+C 行为有离线确定性测试；
- JSONL 可解析、可审计且通过脱敏测试；
- 所有退出路径得到稳定退出码；
- 三模块安全范围和搜索纯滑动策略不变；
- 完整离线套件、公开依赖闭包和静态检查通过；
- 真机只读验收前不进行任何设备操作；
- 不修改 `utils.py`，不推送 GitHub，不创建 PR 或 release。

## 16. 设计自审结论

- 占位符：未发现任何未决标记或未决定接口。
- 一致性：dry-run 的零动作边界、日志隐私、退出码和真机验收范围互相一致。
- 超时语义：明确为协作式 deadline，不承诺强杀单次底层调用。
- 隐私语义：补充了对公开 CLI 所有既有 `print()` 的审计要求，避免旧输出绕过标准化标签。
- 范围：运行入口加固可独立实施；打包、CI、双仓库迁移和 execute 真机验证继续属于后续计划。
