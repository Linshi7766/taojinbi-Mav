# taojinbi-Mav 公开导出清单（供发布前审查）

> 2026-08-28 更名：仓库名 `coin11-tb` → `taojinbi-Mav`，Python 包 `coin11_tb` → `taojinbi_mav`；本清单与导出内容已同步。

生成时间：2026-08-28 19:35
验证：2026-08-28 21:40 复验，导出目录离线测试 309/309 通过（含 auto-exit 修复）；CLI --help 正常；敏感扫描零命中。

## 包含文件（48 个）

```text
./.github/workflows/offline-tests.yml
./.gitignore
./CHANGELOG.md
./CONTRIBUTING.md
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
./LICENSE
./PUBLISH_MANIFEST.md
./pyproject.toml
./README.md
./scripts/run_taojinbi.py
./SECURITY.md
./src/taojinbi_mav/__init__.py
./src/taojinbi_mav/ocr_ui.py
./src/taojinbi_mav/runtime/__init__.py
./src/taojinbi_mav/runtime/config.py
./src/taojinbi_mav/runtime/deadline.py
./src/taojinbi_mav/runtime/logging.py
./src/taojinbi_mav/runtime/outcome.py
./src/taojinbi_mav/runtime/watch.py
./src/taojinbi_mav/task_core.py
./src/taojinbi_mav/task_strategies.py
./src/taojinbi_mav/tasks/__init__.py
./src/taojinbi_mav/tasks/featured_goods.py
./src/taojinbi_mav/tasks/hashtag.py
./src/taojinbi_mav/tasks/registry.py
./src/taojinbi_mav/tasks/search.py
./tests/__init__.py
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
```

## 排除项（保留在私有仓库）

- 根目录轻量 shim（taojinbi_task_core.py 等 8 个，供旧脚本导入）
- utils.py、taojinbi_task_ui.py、taojinbi_runtime_watch.py（根目录旧版）
- 淘宝多任务执行.py、淘金币系/（旧入口）
- 天猫系/、支付宝系/、淘宝活动系/、闲鱼系/、杂项/
- img/ 旧模板素材、_ocr_*.png 临时截图
- 旧版测试：test_launcher_safety / test_multi_task_launcher / test_taojinbi_task_ui / test_utils_lazy_ocr / test_utils_task_loop_integration
- scripts/wait_for_task.py、scripts/wait_panel.py（私有守候脚本与整场监控面板：自动重检循环，不进入公开版运行合同）
- docs/superpowers/plans/（内部计划，含设备信息）
- 进度跟踪/、.workbuddy/、.venv/、.Python/、.cache/、.worktrees/、.superpowers/、logs/、__pycache__/
- git 历史（导出将用全新初始提交，历史中旧设备地址不会进入公开仓库）
