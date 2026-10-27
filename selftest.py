#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
自检脚本：不需要微信、不需要屏幕，直接验证「水印 / 防伪码 / 篡改检测」是否正确。

    python selftest.py            # 跑全部用例
    python selftest.py --keep     # 保留中间产物便于人工查看
    python selftest.py -v         # 显示每条用例的细节

覆盖的关键性质
--------------
1. 防伪码能被解码还原成「时间 + 图片指纹」，且签名可校验
2. 水印**严格落在被排除的区域内**（否则核验永远不成立）
3. 原始图片核验通过
4. 改一个像素 / 裁剪 / 缩放 / 改内容后保留水印 —— 全部被检出
5. 篡改防伪码字符 / 换密钥 —— 签名不通过
6. 经 JPEG 有损重编码 —— 被判定为改动（并提示使用原始 PNG）
"""

from __future__ import annotations

import argparse
import os
import shutil
import sys
import tempfile

import numpy as np
from PIL import Image

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import watermark as W


class Runner:
    def __init__(self) -> None:
        self.fails = []
        self.total = 0

    def check(self, name: str, cond: bool, extra: str = "") -> bool:
        self.total += 1
        mark = "PASS" if cond else "FAIL"
        line = f"  [{mark}] {name}"
        if extra:
            line += f"   ({extra})"
        print(line)
        if not cond:
            self.fails.append(name)
        return cond

    def section(self, title: str) -> None:
        print(f"\n{title}")
        print("-" * 62)


def rand_image(w: int, h: int, seed: int = 1) -> Image.Image:
    rng = np.random.default_rng(seed)
    arr = rng.integers(0, 256, (h, w, 3), dtype=np.uint8)
    return Image.fromarray(arr, "RGB")


def run(keep: bool = False, verbose: bool = False) -> int:
    r = Runner()
    tmp = tempfile.mkdtemp(prefix="wss_selftest_")
    key_file = os.path.join(tmp, "watermark.key")
    secret = W.load_secret(key_file)
    print(f"临时目录: {tmp}")
    print(f"测试密钥: {key_file} ({len(secret)} 字节)")

    ts = 1793176831

    # ---------------------------------------------------------------- 1
    r.section("1. 防伪码：编码 / 解码 / 签名")
    fp = bytes.fromhex("3f2a1b9c") + b"\x00" * 28
    code = W.encode_code(ts, fp, secret)
    print(f"       防伪码 = {code}")
    r.check("长度为 24 字符（3 组 × 8）", len(code.replace("-", "")) == 24)
    r.check("按 8 字符分组", code.count("-") == 2)

    info = W.verify_code(code, secret)
    r.check("签名校验通过", info["signature_ok"] is True)
    r.check("可还原时间信息", info["timestamp"] == ts, info["time"])
    r.check("可还原图片信息", info["fp4_hex"] == "3f2a1b9c", info["fp4_hex"])

    tolerant = W.verify_code(code.lower().replace("-", " "), secret)
    r.check("抄写容错（小写 + 空格）",
            tolerant["signature_ok"] and tolerant["timestamp"] == ts)

    # ---------------------------------------------------------------- 2
    r.section("2. 水印落位约束（核验能否成立的前提）")
    for size in [(1920, 1080), (1366, 768), (800, 600), (601, 361), (300, 200)]:
        base = rand_image(*size, seed=size[0])
        rect = W.watermark_rect(size)
        marked, res = W.apply_watermark(base, secret, timestamp=ts)
        a = np.array(base)
        b = np.array(marked)
        x0, y0, x1, y1 = rect
        mask = np.ones(a.shape[:2], dtype=bool)
        mask[y0:y1 + 1, x0:x1 + 1] = False
        max_diff = int(np.abs(a.astype(int) - b.astype(int)).max(axis=2)[mask].max())
        r.check(
            f"{size[0]}x{size[1]} 水印区外像素零改动",
            max_diff == 0,
            f"字号={res.font_size} 二维码={res.has_qr} 最大差异={max_diff}",
        )

    # ---------------------------------------------------------------- 3
    r.section("3. 原始图片应核验通过")
    base = rand_image(1920, 1080, seed=7)
    marked, res = W.apply_watermark(base, secret, timestamp=ts)
    ok_path = os.path.join(tmp, "original.png")
    marked.save(ok_path)

    v = W.verify_image(ok_path, secret=secret)
    r.check("扫码自动核对通过", v["ok"] is True, v["reason"])
    r.check("成功识别二维码", v["has_qr"] is True)

    v2 = W.verify_image(ok_path, secret=secret, code=res.code)
    r.check("手工输入防伪码通过", v2["ok"] is True, v2["reason"])

    # ---------------------------------------------------------------- 4
    r.section("4. 篡改必须被检出")
    arr = np.array(marked).copy()
    arr[200, 500] = [255, 0, 0]
    p = os.path.join(tmp, "tamper_1px.png")
    Image.fromarray(arr).save(p)
    v = W.verify_image(p, secret=secret)
    r.check("改动 1 个像素", v["ok"] is False and v["reason"] == "tampered", v["reason"])

    arr = np.array(marked).copy()
    arr[100:300, 100:400] = 0
    p = os.path.join(tmp, "tamper_area.png")
    Image.fromarray(arr).save(p)
    v = W.verify_image(p, secret=secret)
    r.check("涂抹一大块内容（保留水印）",
            v["ok"] is False and v["reason"] == "tampered", v["reason"])

    p = os.path.join(tmp, "tamper_crop.png")
    marked.crop((0, 0, 1900, 1070)).save(p)
    v = W.verify_image(p, secret=secret)
    r.check("裁剪边缘", v["ok"] is False, v["reason"])

    p = os.path.join(tmp, "tamper_scale.png")
    marked.resize((960, 540), Image.LANCZOS).save(p)
    v = W.verify_image(p, secret=secret)
    r.check("整体缩放", v["ok"] is False, v["reason"])

    x0, y0, x1, y1 = res.rect
    arr = np.array(marked).copy()
    arr[y0:y1 + 1, x0:x1 + 1] = 128
    p = os.path.join(tmp, "tamper_wipe.png")
    Image.fromarray(arr).save(p)
    v = W.verify_image(p, secret=secret)
    r.check("把水印整块涂掉 -> 读不到防伪码（可疑信号）",
            v["ok"] is False and v["reason"] == "no_code", v["reason"])

    # ---------------------------------------------------------------- 5
    r.section("5. 伪造防伪码")
    compact = list(res.code.replace("-", ""))
    idx = 3
    compact[idx] = "A" if compact[idx] != "A" else "B"
    rejected = False
    try:
        rejected = W.verify_code("".join(compact), secret)["signature_ok"] is False
    except W.WatermarkError:
        rejected = True
    r.check("改动 1 个字符后签名失效", rejected)

    other = W.load_secret(os.path.join(tmp, "other.key"))
    v = W.verify_image(ok_path, secret=other)
    r.check("用别人的密钥核验不通过",
            v["ok"] is False and v["reason"] == "bad_signature", v["reason"])

    # ---------------------------------------------------------------- 6
    r.section("6. 有损压缩")
    p = os.path.join(tmp, "lossy.jpg")
    marked.save(p, "JPEG", quality=90)
    v = W.verify_image(p, secret=secret)
    r.check("JPEG 重编码被判定为改动", v["ok"] is False, v["reason"])

    # ---------------------------------------------------------------- 7
    r.section("7. 与归档流程串联（需要 Windows 依赖）")
    try:
        import wechat_screenshot as S
    except Exception as exc:                      # pragma: no cover
        print(f"  [SKIP] 无法导入主程序: {exc}")
        S = None

    if S is not None:
        out_dir = os.path.join(tmp, "out")
        cfg = S.Config(save_dir=out_dir, file_format="png",
                       watermark_enabled=True, watermark_secret_file=key_file,
                       prefix="ST")
        src = os.path.join(tmp, "from_wechat.png")
        rand_image(1200, 800, seed=11).save(src)
        dest = S.archive(cfg, src, "ST_20261027_120000")
        r.check("归档后文件存在", os.path.isfile(dest), os.path.basename(dest))
        r.check("源文件已移走", not os.path.exists(src))
        v = W.verify_image(dest, secret=W.load_secret(key_file, create=False))
        r.check("归档产物可直接核验通过", v["ok"] is True, v["reason"])

        cfg_jpg = S.Config(save_dir=out_dir, file_format="jpg",
                           watermark_enabled=True, watermark_secret_file=key_file)
        r.check("jpg + 水印 被自动纠正为 png", cfg_jpg.extension == "png")

    # ---------------------------------------------------------------- 汇总
    print("\n" + "=" * 62)
    if r.fails:
        print(f"失败 {len(r.fails)}/{r.total} 项：")
        for name in r.fails:
            print(f"  - {name}")
        code_out = 1
    else:
        print(f"全部 {r.total} 项通过")
        code_out = 0

    if keep:
        print(f"\n中间产物保留在: {tmp}")
    else:
        shutil.rmtree(tmp, ignore_errors=True)

    return code_out


def main() -> int:
    p = argparse.ArgumentParser(description="水印 / 防伪码 / 篡改检测自检")
    p.add_argument("--keep", action="store_true", help="保留中间产物")
    p.add_argument("-v", "--verbose", action="store_true", help="输出更多细节")
    args = p.parse_args()
    return run(keep=args.keep, verbose=args.verbose)


if __name__ == "__main__":
    sys.exit(main())
