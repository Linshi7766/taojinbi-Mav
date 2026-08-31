"""只读核对淘金币余额：连接真机 → OCR → 解析余额（零点击零滑动）。

用法:
    python scripts/check_balance.py --serial <序列号> [--before <历史值>]
安全: 只截图+OCR，无任何 input 动作；要求手机已停在淘金币首页。
"""
import argparse
import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO / "src"))

from taojinbi_mav import ocr_ui  # noqa: E402


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="只读核对淘金币余额")
    parser.add_argument("--serial", required=True, help="设备序列号")
    parser.add_argument("--before", type=int, default=None,
                        help="历史余额值，用于输出差值")
    args = parser.parse_args(argv)

    import uiautomator2 as u2

    try:
        import easyocr
        reader = easyocr.Reader(["ch_sim", "en"], gpu=False)
    except Exception as error:
        print(f"OCR 初始化失败：{type(error).__name__}")
        return 1

    device = u2.connect(args.serial)
    size = device.window_size()
    screen = (size[0], size[1]) if isinstance(size, tuple) else (size["width"], size["height"])
    import run_taojinbi as rt
    sys.path.insert(0, str(_REPO / "scripts"))
    spans = rt.ocr_screen(device, reader)
    balance = ocr_ui.parse_coin_balance(spans)
    if balance is None:
        print("未识别到余额（请确认手机停在淘金币首页）")
        return 2
    print(f"当前余额: {balance}")
    if args.before is not None:
        print(f"对比历史 {args.before}: 差值 {balance - args.before:+d}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
