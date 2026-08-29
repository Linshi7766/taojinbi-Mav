"""Runtime configuration shared by the offline-testable entry points."""

import argparse
import os


# 公开仓库不保存私人设备默认值：设备序列号必须由 --serial 或
# TAOJINBI_DEVICE_SERIAL 显式提供，缺失时安全失败（设计 4.1 / 9.4）。
DEFAULT_DEVICE_SERIAL = ""
DEFAULT_OCR_GPU = False
DEVICE_SERIAL_ENV = "TAOJINBI_DEVICE_SERIAL"
OCR_GPU_ENV = "TAOJINBI_OCR_GPU"

DEFAULT_DRY_RUN_TIMEOUT = 120
DEFAULT_TASK_TIMEOUT = 1200
DEFAULT_RUN_TIMEOUT = 1800
DEFAULT_RECOVERY_TIMEOUT = 10

TASK_KEY_CHOICES = ("search", "hashtag", "featured_goods")


def _env_flag(name, default=False):
    value = os.getenv(name)
    if value is None:
        return default
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "on", "gpu"}:
        return True
    if normalized in {"0", "false", "no", "off", "cpu"}:
        return False
    return default


def resolve_device_serial(value=None):
    """Resolve an explicit serial, environment value, or documented default."""
    serial = value or os.getenv(DEVICE_SERIAL_ENV) or DEFAULT_DEVICE_SERIAL
    return serial.strip()


def resolve_ocr_gpu(value=None):
    """Resolve GPU mode without forcing a machine-specific accelerator."""
    if value is None:
        return _env_flag(OCR_GPU_ENV, DEFAULT_OCR_GPU)
    return bool(value)


def nonnegative_int(value):
    """Parse a task limit while allowing zero for read-only startup checks."""
    parsed = int(value)
    if parsed < 0:
        raise argparse.ArgumentTypeError("must be >= 0")
    return parsed


def positive_int(value):
    """Parse a positive integer for runtime time budgets (must be > 0)."""
    parsed = int(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be > 0")
    return parsed


def build_ocr_arg_parser():
    parser = argparse.ArgumentParser(description="运行淘金币 OCR 纯浏览任务")
    parser.add_argument(
        "--serial",
        default=None,
        help=f"设备序列号（默认读取 {DEVICE_SERIAL_ENV}）",
    )
    parser.add_argument(
        "--gpu",
        dest="gpu",
        action="store_true",
        default=None,
        help="启用 EasyOCR GPU；默认 CPU，或读取环境变量",
    )
    parser.add_argument(
        "--cpu",
        dest="gpu",
        action="store_false",
        help="强制使用 EasyOCR CPU",
    )
    parser.add_argument(
        "--max-tasks",
        type=nonnegative_int,
        default=1,
        help="本次最多执行的安全浏览任务数；默认 1，0 表示只检查入口",
    )
    parser.add_argument(
        "--dry-run",
        dest="dry_run",
        action="store_true",
        default=False,
        help="只读检查当前“赚金币抵钱”任务列表弹窗，零点击/滑动/返回",
    )
    parser.add_argument(
        "--dry-run-timeout",
        type=positive_int,
        default=DEFAULT_DRY_RUN_TIMEOUT,
        help=f"dry-run 总时限秒数；默认 {DEFAULT_DRY_RUN_TIMEOUT}",
    )
    parser.add_argument(
        "--task-timeout",
        type=positive_int,
        default=DEFAULT_TASK_TIMEOUT,
        help=f"单个任务时限秒数；默认 {DEFAULT_TASK_TIMEOUT}",
    )
    parser.add_argument(
        "--ocr-sidecar-port",
        type=nonnegative_int,
        default=0,
        help="OCR 推理 sidecar 端口；0 表示自行加载 EasyOCR（默认）",
    )
    parser.add_argument(
        "--run-timeout",
        type=positive_int,
        default=DEFAULT_RUN_TIMEOUT,
        help=f"整次运行时限秒数；默认 {DEFAULT_RUN_TIMEOUT}",
    )
    parser.add_argument(
        "--recovery-timeout",
        type=positive_int,
        default=DEFAULT_RECOVERY_TIMEOUT,
        help=f"超时/中止后的安全恢复时限秒数；默认 {DEFAULT_RECOVERY_TIMEOUT}",
    )
    parser.add_argument(
        "--task",
        choices=TASK_KEY_CHOICES,
        default=None,
        help="只执行指定注册任务类型（search/hashtag/featured_goods）；"
             "默认不限定，扫描全部已注册任务",
    )
    parser.add_argument(
        "--no-watch",
        dest="watch",
        action="store_false",
        default=True,
        help="不自动弹出终端实时进度面板（默认自动弹出）",
    )
    return parser
