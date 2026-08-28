import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
README_PATH = REPO_ROOT / "README.md"

SUPPORTED_LABELS = ("搜一搜…", "看看#…", "发现精选好物")
REMOVED_TASKS = ("拍立淘", "酒店超抵", "去省钱卡领红包", "淘金币充话费")


def read_readme() -> str:
    return README_PATH.read_text(encoding="utf-8")


def supported_section(readme: str) -> str:
    """返回“支持模块”标题到下一个标题之间的区块，即支持列表本体。"""
    lines = readme.splitlines()
    start = None
    for i, line in enumerate(lines):
        if line.lstrip().startswith("#") and "支持模块" in line:
            start = i
            break
    if start is None:
        raise AssertionError("README 缺少“支持模块”标题，无法定位支持列表")
    end = len(lines)
    for i in range(start + 1, len(lines)):
        if lines[i].lstrip().startswith("#"):
            end = i
            break
    return "\n".join(lines[start:end])


def module_row(readme: str, module: str) -> str:
    rows = [
        line
        for line in readme.splitlines()
        if line.startswith(f"| {module} |")
    ]
    if len(rows) != 2:
        raise AssertionError(f"README 应恰好包含两行 {module} 模块表记录")
    return "\n".join(rows)


class ReadmeRuntimeContractTests(unittest.TestCase):
    def test_readme_contains_dry_run_and_zero_action_fragments(self):
        readme = read_readme()
        for fragment in ("--dry-run", "只扫描当前可见一屏", "不会点击", "不会滑动"):
            with self.subTest(fragment=fragment):
                self.assertIn(fragment, readme)

    def test_readme_contains_logs_exit_code_and_deadline_defaults(self):
        readme = read_readme()
        for fragment in ("logs/", "130", "20 分钟", "30 分钟"):
            with self.subTest(fragment=fragment):
                self.assertIn(fragment, readme)

    def test_supported_module_section_lists_three_safe_labels(self):
        section = supported_section(read_readme())
        for label in SUPPORTED_LABELS:
            with self.subTest(label=label):
                self.assertIn(label, section)

    def test_removed_tasks_absent_from_supported_module_section(self):
        section = supported_section(read_readme())
        for name in REMOVED_TASKS:
            with self.subTest(name=name):
                self.assertNotIn(name, section)

    def test_readme_removes_stale_device_validation_status(self):
        readme = read_readme()
        self.assertNotIn("等待本轮真机验收", readme)
        self.assertNotIn("真机验收尚未完成", readme)
        self.assertNotIn("待完整真机执行验证", readme)

    def test_module_rows_report_precise_device_evidence(self):
        readme = read_readme()
        for module in ("搜一搜", "看看#"):
            with self.subTest(module=module):
                self.assertIn(
                    "真机执行通过，金币到账已确认",
                    module_row(readme, module),
                )
        self.assertIn(
            "真机执行通过，金币到账已确认",
            module_row(readme, "发现精选好物"),
        )

    def test_readme_records_read_only_dry_run_device_acceptance(self):
        self.assertIn("只读 dry-run 真机验收已通过", read_readme())

    def test_readme_keeps_home_page_out_of_navigation_contract(self):
        readme = read_readme()
        self.assertNotIn("领淘金币", readme)
        self.assertNotIn("首页直达", readme)
        self.assertIn("脚本不会导航到淘金币页面本身", readme)

    def test_readme_requires_the_single_cli_entry_and_progress_panel(self):
        readme = read_readme()
        for fragment in (
            "唯一的真机自动化入口",
            "scripts/run_taojinbi.py",
            "python -m taojinbi_mav.runtime.watch",
            "不要传 `--no-watch`",
            "不要通过 `import` 直接调用 `run_ocr_entry()`",
            "不要运行 `淘宝多任务执行.py`",
            "--gpu",
        ):
            with self.subTest(fragment=fragment):
                self.assertIn(fragment, readme)


if __name__ == "__main__":
    unittest.main()
