# 精选好物未达分母状态归类修复设计

## 背景

2026-08-28 的 GPU 真机单任务运行中，`发现精选好物` 从 `1/3` 推进到 `2/3`，后续进度未能按当前固定分母继续验证。结构化日志却记录：

- `task_finished.status = likely_completed`
- `task_finished.reason = ok`
- `run_finished.status = success`

最后可靠进度仍为 `2/3`，因此上述结果不能作为完成或到账证据。

## 根因

问题由两层宽松行为叠加产生：

1. `run_verified_immersive_progress()` 在固定分母模式下会拒绝分母不匹配的进度 pair，但随后允许 `missing_progress_reason()` 返回 UI 层遗留的 `ok`，形成“进度不可采用但 reason 为 ok”的矛盾结果。
2. `run_safe_browse_tasks()` 对所有 `successful_steps > 0` 的未完成结果一律归为 `likely_completed`，即使最后可靠进度仍小于目标分母，且没有归零、轮换或完成态证据。

## 目标

- 固定分母不匹配时输出稳定、非成功的原因 `progress_total_mismatch`。
- 未达到分母且缺少明确完成证据时，统一归为 `unfinished`，运行结果不得为成功。
- 保留现有已验证完成证据：明确读到分子等于分母、进度归零/回落、已验证的完成后刷新结果，以及现有针对任务行轮换的保守规则。
- 保持日志脱敏，不输出商品名、坐标、设备序列号或异常正文。

## 非目标

- 不修改任务白名单、任务标题匹配或描述证据。
- 不新增 OCR 坐标点击、固定坐标、第三方任务或交易任务。
- 不修改 GPU/CPU 依赖配置、ADB 连接恢复或浏览手势。
- 不把本次代码修复视为金币到账证明；到账仍由用户核对。

## 设计

### 1. 核心进度原因规范化

在 `run_verified_immersive_progress()` 的进度重试循环中记录是否读到过“格式有效但分母与固定目标不一致”的 pair。

- 若最终没有可接受读数且出现过该情况，返回 `progress_total_mismatch`。
- 只有未出现分母不匹配时，才允许 `missing_progress_reason()` 提供更具体的 UI 原因。
- `ok` 和 `not_started` 不得作为缺失进度的最终失败原因；无法得到更具体原因时退回 `missing_progress`。
- 动态分母任务继续使用原有轮换逻辑，不受此规则影响。

### 2. 运行结果保守归类

`run_safe_browse_tasks()` 不再把 `successful_steps > 0` 单独视为完成证据。

- `result.completed` 仍归为 `completed`。
- `progress_reset`、`task_rotated_after_refresh` 及现有明确完成后刷新结果继续归为 `likely_completed`。
- 现有 `task_row_unobserved + browsed + progress > 0` 规则保持不变，避免改变已经人工到账验证的 `看看#` 行为。
- `missing_progress + browsed` 仅在任务总数为 1 时保留短任务的 `likely_completed` 口径。
- `progress_total_mismatch`、`stalled`、`no_safe_control`、`ok` 或其他未完成原因，即使 `successful_steps > 0`，也归为 `unfinished`。

因此，真机证据 `progress=2, total=3, successful_steps=1, reason=ok` 必须输出未完成，并使 `_execute_scan()` 返回 `RunStatus.PARTIAL`，而不是 `SUCCESS`。

### 3. 控制台文案

`run_one_safe_browse_task()` 只有在结果确有完成证据时才打印“很可能已完成”。若本轮增长过但未达到分母，应打印最后可靠进度和失败原因，并明确“未确认完成”。

## 测试策略

严格按 TDD：

1. 核心 RED：固定目标为 3，读数从 `1/3`、`2/3` 变为 `2/5`，断言结果为 `progress_total_mismatch`，不是 `ok`。
2. 汇总 RED：构造 `progress=2`、`successful_steps=1`、`reason=ok`、目标 `3`，断言进入 `unfinished`，日志 `task_finished.status=unfinished`。
3. 入口 RED：同一结果经 `_execute_scan()` 后断言 `RunStatus.PARTIAL`。
4. GREEN 后运行相关核心、OCR runtime 聚焦测试，再运行项目 `.venv` 全量 `unittest`、`py_compile` 和 `git diff --check`。

## 安全与验收

修复不增加任何设备动作。离线测试通过后，真机复测仍使用：

- 项目 `.venv`
- `--gpu`
- `--task featured_goods`
- `--max-tasks 1`

真机验收成功条件是：未达到最新分母时绝不报告完成或成功；只有读到明确完成证据时才提升状态。完整执行和金币到账仍需用户在页面中核对。
