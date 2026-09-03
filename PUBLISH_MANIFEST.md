# taojinbi-Mav 公开导出清单（公开仓库已上线，本清单与实际跟踪文件一致）

> 2026-08-28 更名：仓库名 `coin11-tb` → `taojinbi-Mav`，Python 包 `coin11_tb` → `taojinbi_mav`；本清单与导出内容已同步。

生成时间：2026-09-02
验证：2026-09-02 复验（代码树 HEAD `7538044`；命令
`PYTHONPATH=src python -m unittest discover -s tests -p "test_*.py"`；
Python 3.13 / Windows），导出目录离线测试 **511/511** 通过。

本次同步 Codex 只读安全审计第 2/3 轮：扫描结果四态化（unsafe/OCR 失败绝不
滚动或按返回）、likely_completed 证据链收紧、死代码清理、文档与四任务+签到
行为一致；并移除早期批量脚本 `run_full_round.py` 及其测试。此前 "527/527"
记录（2026-09-02 第 1 轮，HEAD `9bbf5d2`/`2fe5bb7`）已过时，以本次为准。

## 包含文件（58 个）

```text
./.github/workflows/offline-tests.yml
./.gitignore
./CHANGELOG.md
./CONTRIBUTING.md
./LICENSE
./PUBLISH_MANIFEST.md
./README.md
./SECURITY.md
./docs/superpowers/specs/2026-07-30-taojinbi-immersive-browse-design.md
./docs/superpowers/specs/2026-07-30-taojinbi-ocr-immersive-design.md
./docs/superpowers/specs/2026-07-30-taojinbi-task-recovery-design.md
./docs/superpowers/specs/2026-08-02-taojinbi-task-refresh-recovery-design.md
./docs/superpowers/specs/2026-08-03-taojinbi-task-strategy-extraction-design.md
./docs/superpowers/specs/2026-08-21-coin11-public-beta-repository-design.md
./docs/superpowers/specs/2026-08-25-taojinbi-public-runtime-hardening-design.md
./docs/superpowers/specs/2026-08-28-featured-goods-progress-classification-design.md
./docs/superpowers/specs/2026-08-28-readme-validation-status-design.md
./docs/superpowers/specs/2026-08-28-taojinbi-reopen-popup-retry-design.md
./docs/superpowers/specs/2026-08-28-taojinbi-startup-popup-recovery-design.md
./docs/superpowers/specs/2026-08-30-public-release-consistency-design.md
./pyproject.toml
./scripts/check_balance.py
./scripts/run_taojinbi.py
./scripts/wait_for_task.py
./scripts/wait_panel.py
./src/taojinbi_mav/__init__.py
./src/taojinbi_mav/ocr_ui.py
./src/taojinbi_mav/runtime/__init__.py
./src/taojinbi_mav/runtime/config.py
./src/taojinbi_mav/runtime/deadline.py
./src/taojinbi_mav/runtime/logging.py
./src/taojinbi_mav/runtime/ocr_service.py
./src/taojinbi_mav/runtime/outcome.py
./src/taojinbi_mav/runtime/watch.py
./src/taojinbi_mav/task_core.py
./src/taojinbi_mav/task_strategies.py
./src/taojinbi_mav/tasks/__init__.py
./src/taojinbi_mav/tasks/featured_goods.py
./src/taojinbi_mav/tasks/hashtag.py
./src/taojinbi_mav/tasks/immersive.py
./src/taojinbi_mav/tasks/registry.py
./src/taojinbi_mav/tasks/search.py
./tests/__init__.py
./tests/test_ocr_service.py
./tests/test_readme_runtime_contract.py
./tests/test_taojinbi_ocr_runtime.py
./tests/test_taojinbi_ocr_ui.py
./tests/test_taojinbi_runtime_config.py
./tests/test_taojinbi_runtime_deadline.py
./tests/test_taojinbi_runtime_logging.py
./tests/test_taojinbi_runtime_outcome.py
./tests/test_taojinbi_runtime_watch.py
./tests/test_taojinbi_task_core.py
./tests/test_taojinbi_task_registry.py
./tests/test_taojinbi_task_strategies.py
./tests/test_wait_for_task.py
./tests/test_wait_panel.py
```

## 排除项（保留在私有仓库）

- 根目录轻量 shim（taojinbi_task_core.py 等 8 个，供旧脚本导入）
- utils.py、taojinbi_task_ui.py、taojinbi_runtime_watch.py（根目录旧版）
- 早期批量脚本 scripts/run_full_round.py（吞子进程退出码/按最新日志选择/
  恒返回 0，存在已知缺陷）及其测试 tests/test_run_full_round.py，仅私有保留
- 淘宝多任务执行.py、淘金币系/（旧入口）
- 天猫系/、支付宝系/、淘宝活动系/、闲鱼系/、杂项/
- img/ 旧模板素材、_ocr_*.png 临时截图
- 旧版测试：test_launcher_safety / test_multi_task_launcher /
  test_taojinbi_task_ui / test_utils_lazy_ocr / test_utils_task_loop_integration
- docs/superpowers/plans/（内部计划，含设备信息）
- 进度跟踪/、.workbuddy/、.venv/、.Python/、.cache/、.worktrees/、.superpowers/、logs/、__pycache__/
- git 历史（公开仓库使用全新历史，历史中旧设备地址不会进入公开仓库）
