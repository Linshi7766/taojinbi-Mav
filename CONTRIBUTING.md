# 贡献指南

本项目采用**先失败测试、再最小实现**的开发方式。提交前请确认下面的检查全部通过。

## 开发环境

- Python 3.11（CI 也在 3.11 上运行 Windows 与 Linux）；
- 依赖只在函数内惰性导入，因此**离线测试可以在纯标准库环境中运行**：

```bash
PYTHONPATH=src python -m unittest discover -s tests -t . -v   # 全量离线回归（与 CI 一致）
python -m unittest -v tests.test_taojinbi_ocr_ui             # 聚焦单个模块
```

> Windows PowerShell 下：`$env:PYTHONPATH="src"; python -m unittest discover -s tests -t . -v`。

真机相关依赖（`uiautomator2`、`easyocr`）只在连接设备或初始化 OCR 时才导入，
测试用注入的假模块替代它们，不会连接手机或下载模型。

## 变更流程

1. 先写一个**会失败**的测试，说明目标行为为什么缺失；记录失败输出；
2. 写最小实现让测试通过；
3. 运行全量回归，确认没有破坏既有行为；
4. 只提交本次变更范围内的文件，提交前自检：

```bash
python -m compileall -q src/taojinbi_mav scripts tests
PYTHONPATH=src python -m unittest discover -s tests -t .
git diff --check
git status --short
```

## 安全约束（不接受违反这些约束的改动）

- 扩大支持范围前必须有真机证据：新的任务形态需要先在真机确认页面锚点与返回路径；
- 不得引入坐标点击商品、绕过风控、交易动作或第三方应用任务；
- 不得放宽 fail-closed：包名、风险词、入口唯一性、有界返回任一失败都必须停止；
- 不得以“提高成功率”为由增加重试、延长连续浏览或自动执行下一个任务。

## 隐私与提交内容

提交前确认变更中不含：真实设备序列号、局域网地址、本机绝对路径、账号信息、
Cookie/Token、API Key，以及 `logs/` 下的运行日志和 OCR 临时截图。

## 真机验证

真机验证始终：

- 从 `--max-tasks 1` 开始；
- 每次只验证一个模块；
- 由人工确认当前页面后再执行；完成后人工核对金币到账，不以脚本自报结果为准。
