# 公开发布文档与守候链一致性修复设计

## 状态

- 日期：2026-08-30
- 用户选择：A+（文档/元数据归一，并修复公开守候链完整性）
- 实施方法：标准库 `unittest`，严格 RED → GREEN；先私有源仓库，再同步公开导出

## 背景

私有全量仓库 `D:\UniFiles\淘金币` 当前为 `main` / `ac32fd8`，与私有 `origin/main` 一致；公开导出仓库 `D:\UniFiles\taojinbi-mav-public` 已发布到 `Linshi7766/taojinbi-Mav`，本地比公开远端领先两个 sidecar 修复。

只读审计确认，当前问题不只是 README 文案过期：

1. 私有 README、pyproject 仍把项目 URL 指向 `czl0325/taojinbi-Mav`，并声称公开发布尚未完成。
2. 私有与公开 README 都引用不存在的顶层 `taojinbi_task_strategies.py`；真实路径是 `src/taojinbi_mav/task_strategies.py`。
3. 公开 README 要求安装不存在的 `requirements.txt`。
4. 公开 `scripts/wait_for_task.py` 在插入 `src` 路径之前导入 `taojinbi_mav`，干净检出直接运行可能失败；CI 的 `PYTHONPATH=src` 掩盖了该问题。
5. 公开守候器默认启动 `scripts/wait_panel.py`，但公开仓库没有该文件和 `tests/test_wait_panel.py`。
6. 公开 `PUBLISH_MANIFEST.md` 的文件数、列出的路径和实际 Git 跟踪文件不一致，并漏列 OCR sidecar 模块。

因此，在修复并验证这些一致性缺口前，不得只推送本地领先的两个 sidecar 修复。

## 目标

- 准确表达发布状态：GitHub 公开源码仓库已经上线；正式版本标签、GitHub Release、PyPI/独立发行包尚未发布。
- 私有源仓库与公开导出的仓库 URL、策略模块路径、安装说明和安全声明保持一致。
- 公开仓库在干净检出后可按 README 完成 editable 安装，并能直接运行单轮 CLI 与守候器的 `--help`，不连接设备。
- 公开守候器声明的整场面板文件和测试真实存在。
- 发布清单与公开 Git 实际跟踪文件严格一致。
- 私有和公开离线测试、编译、敏感扫描、diff 检查全部通过后，才允许推送公开仓库本地提交。

## 非目标

- 不新增 `[project.scripts]`，不提供安装后的全局命令；当前仍是源码 checkout 下运行 `scripts/run_taojinbi.py`。
- 不迁移或重构 1554 行主 CLI；包内 `cli.py`/console entry point 留作独立项目。
- 不改变三项任务注册表、标题匹配、安全过滤、OCR 点击、浏览策略、进度判定或退出码。
- 不连接 ADB，不运行真机任务，不下载 OCR 模型，不绕过风控。
- 不创建版本 tag、GitHub Release 或 PyPI 发布。
- 不触碰 `.workbuddy/`、历史计划或私有旧业务资产。

## 设计

### 1. 私有源仓库作为文档与代码来源

先在私有全量仓库完成可共享文件的 TDD 修复，再把批准文件同步到公开导出。私有仓库保留旧资产，因此来源说明不能原样声称“旧版多任务资产不再包含”；公开 README 继续保留适用于干净导出的来源与免责声明。

私有 README 补充适用于全量仓库的来源说明和同等免责声明：明确淘金币核心源自 `czl0325/coin11-tb`，同时说明私有仓库仍含历史兼容资产；不得复制公开版“旧版多任务资产不再包含”的不实表述。

私有仓库拟修改：

- `README.md`
- `pyproject.toml`
- `CHANGELOG.md`
- `SECURITY.md`
- `scripts/wait_for_task.py`
- `tests/test_readme_runtime_contract.py`
- `tests/test_wait_for_task.py`

私有仓库已有 `scripts/wait_panel.py` 和 `tests/test_wait_panel.py`，只验证，不重写。

### 2. 发布状态与链接合同

README 统一使用以下事实边界：

- “GitHub 公开源码仓库已上线”：`https://github.com/Linshi7766/taojinbi-Mav`
- “正式版本标签/独立发行包尚未发布”
- 当前安装方式是项目 checkout 内的 editable 安装，不暗示已提供 PyPI 包或全局 CLI

同时完成：

- `pyproject.toml` 的 Homepage、Security、Changelog 指向 `Linshi7766/taojinbi-Mav`。
- description 删除“公开 Beta 准备中”等会立即过期的状态词。
- README Star History 指向 `Linshi7766/taojinbi-Mav`。
- 策略文件写成真实路径 `src/taojinbi_mav/task_strategies.py`。
- “发布状态”表头改为“验证状态”，不把真机到账证据误称为软件发布状态。
- “未来公开导出时删除”改为当前事实：兼容实现不在三模块标准入口中，后续清理由独立任务处理。
- CHANGELOG 保留 `Unreleased` 语义，但明确它表示正式版本尚未打标签/发行，而不是公开源码仓库尚不存在。
- SECURITY 将未来式“公开仓库发布后”改为当前公开仓库 Issues 地址。

### 3. 安装与源码入口合同

私有与公开 README 统一使用 pyproject 元数据安装，不再把 `requirements.txt` 作为公开安装合同；在项目 `.venv` 中执行：

```powershell
& .\.venv\Scripts\python.exe -m pip install -e '.[gpu]'
```

安装后仍从项目根目录运行：

```powershell
& .\.venv\Scripts\python.exe .\scripts\run_taojinbi.py ...
& .\.venv\Scripts\python.exe .\scripts\wait_for_task.py ...
```

文档必须明确：editable 安装提供包与依赖，但本阶段没有 `[project.scripts]`，不承诺 `taojinbi-mav` 全局命令。

### 4. 守候器干净检出启动

`scripts/wait_for_task.py` 必须先计算仓库根目录并把 `src` 加入 `sys.path`，再导入 `taojinbi_mav.runtime.ocr_service`。顺序与 `scripts/run_taojinbi.py` 的源码 bootstrap 保持一致。

此改动只影响模块解析，不改变守候循环、sidecar 生命周期、面板启动、任务预算或安全停止策略。

用隔离子进程验证：

```powershell
& .\.venv\Scripts\python.exe -I .\scripts\wait_for_task.py --help
```

`-I` 排除当前 editable 安装和 `PYTHONPATH` 偶然帮助，确保脚本只依赖自身 bootstrap；命令不得连接设备或启动 OCR 模型。

### 5. 公开面板与测试补齐

从私有源仓库按内容同步到公开导出：

- `scripts/wait_panel.py`
- `tests/test_wait_panel.py`

不重新实现面板。公开测试必须实际发现并运行 `test_wait_panel.py`，从而锁定自动退出、会话状态和日志读取合同。

### 6. 发布清单重建

公开 `PUBLISH_MANIFEST.md` 以当前 `git ls-files` 为唯一来源重建：

- 文件总数等于 Git 跟踪文件数。
- 代码块中每个路径真实存在并恰好出现一次。
- 补入 `src/taojinbi_mav/runtime/ocr_service.py`。
- 补入实际导出的面板文件与测试。
- 更新验证日期、测试数和“已导出”时态。

清单不得加入设备序列号、IP、日志、截图、进度跟踪或私有旧资产。

## TDD 策略

### RED 1：发布文档和 pyproject

在 `tests/test_readme_runtime_contract.py` 增加标准库 `tomllib` 契约：

- README 包含当前公开仓库 URL和“公开源码仓库已上线”。
- README 不含两处“公开发布仍未完成”和 `czl0325/taojinbi-Mav`。
- README 使用真实策略模块路径，并明确没有全局安装命令。
- pyproject description 不含“公开 Beta 准备中”。
- Homepage、Security、Changelog 精确指向用户公开仓库。

只禁止错误的 `czl0325/taojinbi-Mav`，不得禁止合法来源声明 `czl0325/coin11-tb`。

### RED 2：守候器源码 bootstrap

在 `tests/test_wait_for_task.py` 增加隔离 subprocess 测试，运行 `python -I scripts/wait_for_task.py --help`，断言退出码 0。旧顺序应因无法导入 `taojinbi_mav` 而失败。

### GREEN

只做使上述测试通过的最小修改：文档/元数据替换和 bootstrap 顺序调整；不顺手重构守候循环或主 CLI。

### 公开导出验证

同步批准文件和缺失面板后，公开仓库运行同一聚焦测试与完整离线套件。`tests/test_wait_panel.py` 必须进入 discovery。

## 验证门禁

私有仓库：

1. README/pyproject 契约聚焦测试。
2. wait supervisor/bootstrap 聚焦测试。
3. `.venv` 全量 `unittest discover`。
4. 相关 Python 文件语法编译。
5. `git diff --check`。

公开导出：

1. `python -I scripts/run_taojinbi.py --help`。
2. `python -I scripts/wait_for_task.py --help`。
3. 完整 `unittest discover`，确认面板测试被发现。
4. `compileall` 覆盖 `src/taojinbi_mav`、`scripts`、`tests`。
5. 发布清单与 `git ls-files` 一致性检查。
6. 敏感扫描：设备序列号、内网/默认设备地址、日志、截图和进度跟踪零命中；合法 GitHub URL 不得被误判。
7. `git diff --check` 和干净状态审计。

任一门禁失败：不 push，不用跳过测试、放宽 CI 或删除安全合同来换绿。

## 提交与同步顺序

1. 私有仓库提交批准设计（本文件）。
2. 实施阶段按 TDD 提交私有源修改。
3. 私有全量门禁通过后，同步批准的公开文件。
4. 在公开本地已有两个 sidecar 修复之上追加一致性修复提交，不改写已发布历史。
5. 公开全量门禁和敏感扫描通过后，才允许 push `Linshi7766/taojinbi-Mav`。
6. 私有仓库 push 与公开 push 分开记录；不得向原作者 `upstream` 推送。

## 安全与失败处理

- 全过程不连接 ADB，不运行真机入口，不生成运行日志。
- 测试只运行 `--help` 或纯离线 unittest；若出现设备/OCR初始化，视为测试失败。
- 公开导出路径与私有路径分别检查，禁止把私有旧资产、进度文件或设备信息复制到公开仓库。
- Git 分支跟踪仍指向 `upstream/main`；所有私有同步命令必须明确写 `origin`，避免误合并原作者历史。
