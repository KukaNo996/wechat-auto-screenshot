#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
截图防伪核验工具。

用法
----
    python verify_watermark.py shot.png
    python verify_watermark.py shot.png --code AFVODNH7-H4VBXHHE-UZ7GEBRF
    python verify_watermark.py *.png --json
    python verify_watermark.py shot.png --secret-file D:\\keys\\wm.key

退出码
------
    0  核验通过（签名有效 + 指纹一致）
    1  核验不通过（被改动 / 签名无效 / 读不到防伪码）
    2  参数或环境错误（找不到文件、缺密钥等）
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Optional, Sequence

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import watermark as W


REASON_TEXT = {
    "verified": "核验通过",
    "tampered": "图片已被改动",
    "bad_signature": "防伪码签名无效",
    "bad_code": "防伪码格式不正确",
    "no_code": "未找到防伪码",
    "no_secret": "找不到密钥",
    "not_found": "文件不存在",
    "unreadable": "图片无法读取",
    "payload_conflict": "二维码与防伪码信息矛盾",
}


def _human_size(size) -> str:
    return f"{size[0]}x{size[1]}" if size else "未知"


def print_report(r: dict, verbose: bool = False) -> None:
    print(f"文件    : {r['path']}")
    print(f"尺寸    : {_human_size(r.get('size'))}")

    if r.get("time"):
        print(f"水印时间: {r['time']}  (Unix {r['timestamp']})")
    if r.get("code"):
        print(f"防伪码  : {r['code']}")
    print(f"二维码  : {'已识别' if r.get('has_qr') else '未识别（可用 --code 手工输入）'}")

    if r.get("code_fp4") or r.get("image_fp4"):
        match = (r.get("code_fp4") and r.get("code_fp4") == r.get("image_fp4"))
        print(f"指纹    : 水印记录 {r.get('code_fp4') or '-'} / "
              f"当前图片 {r.get('image_fp4') or '-'}  {'✓ 一致' if match else '✗ 不一致'}")

    if verbose and r.get("reason"):
        print(f"内部状态: {r['reason']}")

    print()
    if r["ok"]:
        print("结论: ✅ 通过 —— 防伪码真实，图片内容自截图后未被改动")
    else:
        label = REASON_TEXT.get(r["reason"], r["reason"])
        print(f"结论: ❌ 不通过 —— {label}")
        for line in str(r.get("detail") or "").splitlines():
            print(f"      {line}")
    print("-" * 66)


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="核验微信截图是否被篡改（读取左下角水印的防伪码）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  python verify_watermark.py shot.png
  python verify_watermark.py shot.png --code AFVODNH7-H4VBXHHE-UZ7GEBRF
  python verify_watermark.py *.png --json
  python verify_watermark.py shot.png --secret-file D:\\keys\\wm.key
        """,
    )
    parser.add_argument("images", nargs="+", help="要核验的图片路径")
    parser.add_argument("--code", "-C",
                        help="手工输入防伪码（二维码读不出来时用；仅对单张图片有效）")
    parser.add_argument("--secret-file", "-k",
                        help=f"密钥文件路径（默认 {W.DEFAULT_SECRET_FILE}）")
    parser.add_argument("--json", action="store_true", help="以 JSON 输出结果")
    parser.add_argument("--verbose", "-v", action="store_true", help="输出内部状态便于排查")
    args = parser.parse_args(argv)

    if args.code and len(args.images) > 1:
        print("[错误] --code 只能配合单张图片使用。", file=sys.stderr)
        return 2

    secret = None
    try:
        secret = W.load_secret(args.secret_file, create=False)
    except W.WatermarkError as exc:
        if not args.json:
            print(f"[警告] {exc}", file=sys.stderr)
            print("[警告] 仍会尝试用 --code 解析，但无法校验签名。\n", file=sys.stderr)

    results = []
    for path in args.images:
        r = W.verify_image(path, secret=secret, code=args.code,
                           secret_file=args.secret_file)
        results.append(r)

    if args.json:
        print(json.dumps(results, ensure_ascii=False, indent=2))
    else:
        print("=" * 66)
        print("微信截图防伪核验")
        print("=" * 66)
        for r in results:
            print_report(r, verbose=args.verbose)

        ok = sum(1 for r in results if r["ok"])
        print(f"共 {len(results)} 张，通过 {ok} 张，未通过 {len(results) - ok} 张")

        if any(r["reason"] in ("tampered", "payload_conflict") for r in results):
            print("\n提示: 若图片经过微信压缩、聊天工具转发或另存为 JPEG，像素会被重新编码，")
            print("      同样会被判定为「改动」。核验请使用工具直接产出的原始 PNG 文件。")

    if any(r["reason"] in ("not_found", "unreadable", "no_secret") for r in results):
        return 2
    return 0 if all(r["ok"] for r in results) else 1


if __name__ == "__main__":
    sys.exit(main())
