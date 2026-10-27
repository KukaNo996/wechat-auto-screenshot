#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
水印与防伪码模块（WeChat Screenshot Signature, 简称 WSS）。

设计目标
--------
在截图左下角打上一块水印，同时提供一个可以被程序复核的「防伪码」，
用来判断这张图**是否被改过**。

核心难点
--------
水印是要画到图片像素上的，画完以后图片就变了，那么"用图片哈希证明图片没变"
就成了循环依赖。解法是**约定一块固定的水印区域，并在计算指纹时把它排除掉**：

    指纹 = SHA256(宽, 高, 除左下角水印区以外的全部像素)

于是：

- 水印画在水印区里  -> 指纹不变
- 任何水印区外的改动 -> 指纹必然变化

防伪码结构（15 字节 -> base32 -> 24 字符）
--------
    ver   1 字节   版本号，当前为 1
    ts    4 字节   时间戳（Unix 秒，大端）
    fp    4 字节   图像指纹前 4 字节
    mac   6 字节   HMAC-SHA256(secret, "WSS1" + ver + ts + fp)[:6]

- 解析防伪码即可还原**时间信息**（ts）与**图片信息**（fp）
- mac 让"没有密钥的人"无法为篡改后的图片伪造新码
- 特别地：水印区被排除在指纹之外，所以攻击者可以改水印上的文字，
  但改完以后 mac 必然对不上 —— 这一层由签名兜住

安全边界（必须诚实说明）
------------------------
1. 密钥泄露 = 防伪失效。密钥保存在本地文件里，不进仓库。
2. JPEG 等有损压缩会改变像素，核验会判定为「已改动」。请保留原始 PNG。
3. 本方案防的是「改图」，不防「改工具」：别人完全可以编译一个自己带密钥的版本。
   若要让他人独立核验而不共享密钥，需要换成非对称签名（Ed25519）。
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import os
import secrets as _secrets
import struct
import time
from dataclasses import dataclass
from typing import Optional, Tuple

import numpy as np
from PIL import Image, ImageDraw, ImageFont

# --------------------------------------------------------------------------
# 协议常量
# --------------------------------------------------------------------------

CODE_VERSION = 1
SIGN_DOMAIN = b"WSS1"          # 签名域前缀，防止跨用途复用同一密钥
CODE_BODY_LEN = 9              # ver(1) + ts(4) + fp(4)
CODE_MAC_LEN = 6               # 截断的 HMAC
CODE_RAW_LEN = CODE_BODY_LEN + CODE_MAC_LEN   # 15 字节
CODE_CHARS = 24                # base32(15B) = 24 字符

DEFAULT_SECRET_FILE = os.path.join(
    os.path.expanduser("~"), ".wechat_screenshot", "watermark.key"
)

# 水印区域的尺寸规则。核验端必须能只看图片宽高就复现同一块区域，
# 因此这里只允许依赖 (W, H)，不许依赖任何运行时状态。
RECT_MAX_W = 640
RECT_MIN_W = 260
RECT_MAX_H = 240
RECT_MIN_H = 90

_FONT_CANDIDATES = (
    r"C:\Windows\Fonts\msyh.ttc",      # 微软雅黑
    r"C:\Windows\Fonts\msyhbd.ttc",
    r"C:\Windows\Fonts\simhei.ttf",    # 黑体
    r"C:\Windows\Fonts\arial.ttf",
    "/System/Library/Fonts/PingFang.ttc",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
)


class WatermarkError(Exception):
    """水印相关的业务异常。"""


# --------------------------------------------------------------------------
# 水印区域与图像指纹
# --------------------------------------------------------------------------

def watermark_rect(size: Tuple[int, int]) -> Tuple[int, int, int, int]:
    """
    返回左下角水印区域的 (x0, y0, x1, y1)，闭区间。

    规则固定为「只由图片宽高决定」，核验端照抄即可得到同一块区域。
    """
    w, h = int(size[0]), int(size[1])
    if w <= 0 or h <= 0:
        raise WatermarkError(f"图片尺寸非法: {size}")

    rect_w = min(RECT_MAX_W, max(RECT_MIN_W, w // 2))
    rect_h = min(RECT_MAX_H, max(RECT_MIN_H, h * 2 // 5))
    rect_w = min(rect_w, w)
    rect_h = min(rect_h, h)
    return (0, h - rect_h, rect_w - 1, h - 1)


def fingerprint(img: Image.Image, rect: Optional[Tuple[int, int, int, int]] = None) -> bytes:
    """
    计算图片指纹（SHA256），**排除水印区域**。

    排除方式是把该区域像素统一置 0 后再整体哈希 —— 比逐行拼接快得多，
    而且核验端可以逐字节复现同一运算。
    """
    rgb = img.convert("RGB")
    arr = np.array(rgb, dtype=np.uint8)
    if rect is None:
        rect = watermark_rect(rgb.size)
    x0, y0, x1, y1 = rect

    work = arr.copy()
    work[y0:y1 + 1, x0:x1 + 1] = 0

    h = hashlib.sha256()
    h.update(struct.pack(">II", arr.shape[1], arr.shape[0]))
    h.update(work.tobytes())
    return h.digest()


# --------------------------------------------------------------------------
# 防伪码：编码 / 解析 / 签名
# --------------------------------------------------------------------------

def load_secret(path: Optional[str] = None, create: bool = True) -> bytes:
    """
    载入（或首次生成）HMAC 密钥。

    密钥不进仓库：默认写到 `~/.wechat_screenshot/watermark.key`，
    也支持在配置里指定路径。丢了密钥 = 旧图无法核验，请自行备份。
    """
    target = os.path.abspath(path or DEFAULT_SECRET_FILE)

    if os.path.isfile(target):
        with open(target, "rb") as fh:
            key = fh.read().strip()
        if key:
            return key
        # 空文件视为损坏，重建

    if not create:
        raise WatermarkError(
            f"找不到密钥文件: {target}\n"
            f"核验需要与生成时相同的密钥；请用 --secret-file 指定正确路径。"
        )

    key = _secrets.token_bytes(32)
    parent = os.path.dirname(target)
    if parent:
        try:
            os.makedirs(parent, exist_ok=True)
        except OSError as exc:
            raise WatermarkError(f"无法创建密钥目录 {parent}: {exc}") from exc
    try:
        with open(target, "wb") as fh:
            fh.write(key)
        try:
            os.chmod(target, 0o600)
        except OSError:
            pass
    except OSError as exc:
        raise WatermarkError(f"无法写入密钥文件 {target}: {exc}") from exc
    return key


def _mac(secret: bytes, body: bytes) -> bytes:
    return hmac.new(secret, SIGN_DOMAIN + body, hashlib.sha256).digest()[:CODE_MAC_LEN]


def encode_code(ts: int, fp: bytes, secret: bytes) -> str:
    """
    生成防伪码：15 字节 -> base32 -> 24 字符，按 8 字符分组便于抄写。

    形如: AB2C3D4E-FG5H6J7K-LM8N9P2Q
    """
    if not 0 <= ts <= 0xFFFFFFFF:
        raise WatermarkError(f"时间戳超出 4 字节范围: {ts}")
    if len(fp) < 4:
        raise WatermarkError("指纹长度不足 4 字节")

    body = bytes([CODE_VERSION]) + struct.pack(">I", ts) + fp[:4]
    raw = body + _mac(secret, body)
    text = base64.b32encode(raw).decode("ascii").rstrip("=")
    if len(text) != CODE_CHARS:
        raise WatermarkError(f"防伪码长度异常: {len(text)} != {CODE_CHARS}")
    return "-".join(text[i:i + 8] for i in range(0, CODE_CHARS, 8))


def decode_code(code: str) -> dict:
    """
    解析防伪码得到**时间信息 + 图片信息**（不校验签名）。

    对手工抄写做了容错：忽略分隔符、空格、大小写。
    """
    compact = "".join(ch for ch in str(code).upper() if ch.isalnum())
    if len(compact) != CODE_CHARS:
        raise WatermarkError(
            f"防伪码应为 {CODE_CHARS} 个字符，实际 {len(compact)} 个: {code!r}"
        )
    pad = "=" * (-len(compact) % 8)
    try:
        raw = base64.b32decode(compact + pad)
    except (binascii.Error, ValueError) as exc:
        raise WatermarkError(f"防伪码不是合法的 base32 编码: {exc}") from exc

    if len(raw) != CODE_RAW_LEN:
        raise WatermarkError(f"防伪码解码后长度异常: {len(raw)} != {CODE_RAW_LEN}")

    ver = raw[0]
    ts = struct.unpack(">I", raw[1:5])[0]
    fp4 = raw[5:9]
    mac = raw[9:15]
    return {
        "version": ver,
        "timestamp": ts,
        "time": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts)),
        "fp4": fp4,
        "fp4_hex": fp4.hex(),
        "mac": mac,
        "body": raw[:CODE_BODY_LEN],
        "compact": compact,
    }


def verify_code(code: str, secret: bytes) -> dict:
    """解析防伪码并校验签名，返回带 `signature_ok` 的字典。"""
    info = decode_code(code)
    if info["version"] != CODE_VERSION:
        raise WatermarkError(
            f"不支持的防伪码版本 {info['version']}（本工具支持 {CODE_VERSION}）"
        )
    expect = _mac(secret, info["body"])
    info["signature_ok"] = hmac.compare_digest(expect, info["mac"])
    return info


# --------------------------------------------------------------------------
# 二维码载荷（比防伪码多带人类可读信息）
# --------------------------------------------------------------------------

def build_payload(ts: int, size: Tuple[int, int], code: str) -> str:
    human = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts))
    return f"WSS1|{human}|{ts}|{code}"


def parse_payload(text: str) -> dict:
    """解析二维码里的载荷文本（3 或 4 字段均兼容）。"""
    parts = str(text).strip().split("|")
    if len(parts) < 3 or not parts[0].startswith("WSS"):
        raise WatermarkError(f"无法识别的载荷格式: {text!r}")

    info = {"magic": parts[0], "time": "", "timestamp": None, "code": ""}
    if len(parts) >= 4:
        info["time"] = parts[1]
        try:
            info["timestamp"] = int(parts[2])
        except ValueError:
            info["timestamp"] = None
        info["code"] = parts[3]
    else:
        info["time"] = parts[1]
        info["code"] = parts[2]
    return info


# --------------------------------------------------------------------------
# 绘制
# --------------------------------------------------------------------------

def _load_font(size: int):
    for path in _FONT_CANDIDATES:
        if os.path.isfile(path):
            try:
                return ImageFont.truetype(path, size)
            except OSError:
                continue
    try:
        return ImageFont.load_default(size=size)   # Pillow >= 10.1
    except TypeError:
        return ImageFont.load_default()


def _text_size(draw: ImageDraw.ImageDraw, text: str, font) -> Tuple[int, int]:
    left, top, right, bottom = draw.textbbox((0, 0), text, font=font)
    return right - left, bottom - top


def _make_qr(text: str, target: int) -> Optional[Image.Image]:
    """
    生成二维码，边长按**模块整数倍**放大。

    这点不能省：如果先把小图生成出来再 resize 到任意尺寸，模块边界会落在
    非整数像素上，边缘被插值糊掉，扫码识别率会大幅下降（实测直接读不出来）。
    所以这里先量出模块数，再选一个能整除的放大倍数，全程不做插值缩放。
    """
    try:
        import qrcode
    except ImportError:
        return None
    try:
        qr = qrcode.QRCode(
            error_correction=qrcode.constants.ERROR_CORRECT_M,
            box_size=1,
            border=2,
        )
        qr.add_data(text)
        qr.make(fit=True)

        modules = qr.modules_count + qr.border * 2
        scale = max(1, int(target) // max(1, modules))
        if scale > 1:
            qr = qrcode.QRCode(
                error_correction=qrcode.constants.ERROR_CORRECT_M,
                box_size=scale,
                border=2,
            )
            qr.add_data(text)
            qr.make(fit=True)

        return qr.make_image(fill_color="black", back_color="white").convert("RGB")
    except Exception:
        return None


@dataclass
class WatermarkOptions:
    """水印外观参数。"""
    enabled: bool = True
    font_size: int = 15
    opacity: float = 0.92          # 文字不透明度 0~1
    margin: int = 14               # 距图片边缘的内边距
    show_timestamp: bool = True    # 是否显示 Unix 时间戳那一行
    qr: bool = True                # 是否在右侧放二维码
    qr_size: int = 120             # 二维码目标边长（自动按模块整数倍取整）
    code_prefix: str = "WSS"       # 防伪码前面的标识


@dataclass
class WatermarkResult:
    code: str
    timestamp: int
    fingerprint_hex: str
    payload: str
    rect: Tuple[int, int, int, int]
    has_qr: bool
    font_size: int


def apply_watermark(img: Image.Image,
                    secret: bytes,
                    options: Optional[WatermarkOptions] = None,
                    timestamp: Optional[int] = None) -> Tuple[Image.Image, WatermarkResult]:
    """
    在图片左下角绘制水印，返回 (新图, 结果信息)。

    绘制顺序很关键：**先算指纹、再画水印**。指纹排除水印区，
    所以画完以后指纹依旧成立。
    """
    opts = options or WatermarkOptions()
    ts = int(timestamp if timestamp is not None else time.time())
    base = img.convert("RGB")
    rect = watermark_rect(base.size)

    # 1) 先在不含水印的图上算指纹
    fp = fingerprint(base, rect)
    code = encode_code(ts, fp, secret)
    payload = build_payload(ts, base.size, code)

    if not opts.enabled:
        return base, WatermarkResult(
            code="", timestamp=ts, fingerprint_hex=fp[:4].hex(), payload="",
            rect=rect, has_qr=False, font_size=0,
        )

    x0, y0, x1, y1 = rect
    rect_w, rect_h = x1 - x0 + 1, y1 - y0 + 1
    margin = max(4, int(opts.margin))

    lines = [
        time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts)),
    ]
    if opts.show_timestamp:
        lines.append(f"TS {ts}")
    lines.append(f"{opts.code_prefix} {code}")

    # 2) 自适应布局：小图放不下就缩字号、缩二维码、最后关掉二维码
    qr_candidates = []
    if opts.qr:
        for cand in (opts.qr_size, 96, 72, 56):
            if cand <= rect_h - margin * 2 and cand not in qr_candidates:
                qr_candidates.append(cand)
    qr_candidates.append(0)

    font = None
    qr_size = 0
    line_h = 0
    gap = 10
    for qr_cand in qr_candidates:
        avail_w = rect_w - margin * 2 - (qr_cand + gap if qr_cand else 0)
        if avail_w < 100:
            continue
        for fs in range(int(opts.font_size), 7, -1):
            probe_font = _load_font(fs)
            probe = ImageDraw.Draw(Image.new("RGB", (10, 10)))
            widths = [_text_size(probe, s, probe_font)[0] for s in lines]
            th = max(_text_size(probe, s, probe_font)[1] for s in lines)
            block_h = int(th * 1.55) * len(lines)
            if max(widths) <= avail_w and block_h <= rect_h - margin * 2:
                font, qr_size, line_h = probe_font, qr_cand, int(th * 1.55)
                break
        if font is not None:
            break

    if font is None:
        # 极端小图：只放一行最短的码
        qr_size = 0
        for fs in range(min(int(opts.font_size), 12), 5, -1):
            probe_font = _load_font(fs)
            probe = ImageDraw.Draw(Image.new("RGB", (10, 10)))
            if _text_size(probe, code, probe_font)[0] <= rect_w - margin * 2:
                font, line_h = probe_font, int(_text_size(probe, code, probe_font)[1] * 1.5)
                lines = [code]
                break
        if font is None:
            raise WatermarkError(
                f"图片太小（{base.size[0]}x{base.size[1]}），放不下防伪码水印"
            )

    # 3) 绘制到独立图层再合成，保证半透明不污染原像素
    overlay = Image.new("RGBA", base.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)

    alpha = max(0, min(255, int(255 * max(0.0, min(1.0, opts.opacity)))))
    block_h = line_h * len(lines)

    # 垂直方向贴着水印区底部，水平从左边距开始
    text_y = y1 - margin - block_h + max(0, (line_h - _text_size(draw, lines[0], font)[1]) // 2)
    text_x = x0 + margin

    for i, line in enumerate(lines):
        yy = text_y + i * line_h
        # 黑色描边 + 浅色填充：深浅背景都能看清
        draw.text((text_x, yy), line, font=font,
                  fill=(255, 255, 255, alpha),
                  stroke_width=2, stroke_fill=(0, 0, 0, min(255, alpha + 20)))

    has_qr = False
    if qr_size:
        qr_img = _make_qr(payload, qr_size)
        if qr_img is not None:
            qw, qh = qr_img.size
            qx = max(x0 + margin, x1 - margin - qw)
            qy = max(y0 + margin, y1 - margin - qh)
            overlay.paste(qr_img, (qx, qy))
            has_qr = True

    out = Image.alpha_composite(base.convert("RGBA"), overlay).convert("RGB")

    return out, WatermarkResult(
        code=code, timestamp=ts, fingerprint_hex=fp[:4].hex(),
        payload=payload, rect=rect, has_qr=has_qr, font_size=int(font.size),
    )


# --------------------------------------------------------------------------
# 读取水印（核验用）
# --------------------------------------------------------------------------

def read_payload(img: Image.Image) -> Optional[str]:
    """
    从图片里读出二维码载荷。只在水印区域内搜索，减少误读。
    需要 opencv-python；不可用或无二维码时返回 None。
    """
    try:
        import cv2
    except ImportError:
        return None

    rgb = np.array(img.convert("RGB"))
    x0, y0, x1, y1 = watermark_rect(img.size)
    crop = rgb[y0:y1 + 1, x0:x1 + 1]
    if crop.size == 0:
        return None

    detector = cv2.QRCodeDetector()
    try:
        data, _pts, _ = detector.detectAndDecode(crop)
    except Exception:
        return None
    if data:
        return str(data)

    # 兜底：放大两倍再试一次（小二维码更容易识别）
    try:
        big = cv2.resize(crop, None, fx=2.0, fy=2.0, interpolation=cv2.INTER_NEAREST)
        data, _pts, _ = detector.detectAndDecode(big)
        return str(data) if data else None
    except Exception:
        return None


def verify_image(path: str,
                 secret: Optional[bytes] = None,
                 code: Optional[str] = None,
                 secret_file: Optional[str] = None) -> dict:
    """
    核验一张图片是否被改动过。

    参数优先级：显式传入的 code > 图片里的二维码 > 报错。
    返回字典，`ok` 为 True 表示「防伪码签名有效 且 图像指纹一致」。
    """
    result = {
        "ok": False,
        "path": path,
        "reason": "",
        "detail": "",
        "code": code or "",
        "timestamp": None,
        "time": None,
        "image_fp4": "",
        "code_fp4": "",
        "has_qr": False,
        "size": None,
    }

    if not os.path.isfile(path):
        result.update(reason="not_found", detail=f"文件不存在: {path}")
        return result

    try:
        with Image.open(path) as im:
            im.load()
            img = im.convert("RGB")
    except Exception as exc:
        result.update(reason="unreadable", detail=f"无法打开图片: {exc}")
        return result

    result["size"] = img.size

    if secret is None:
        try:
            secret = load_secret(secret_file, create=False)
        except WatermarkError as exc:
            result.update(reason="no_secret", detail=str(exc))
            return result

    # 1) 先尝试扫码拿完整载荷
    payload_info = None
    payload_text = read_payload(img)
    if payload_text:
        result["has_qr"] = True
        try:
            payload_info = parse_payload(payload_text)
        except WatermarkError:
            payload_info = None

    if not code and payload_info:
        code = payload_info["code"]
    result["code"] = code or ""

    if not code:
        result.update(
            reason="no_code",
            detail=("图片中未找到可识别的二维码，也没有通过 --code 提供防伪码。\n"
                    "请对照图片左下角的水印，把防伪码抄下来传给 --code。"),
        )
        return result

    # 2) 校验防伪码签名
    try:
        info = verify_code(code, secret)
    except WatermarkError as exc:
        result.update(reason="bad_code", detail=str(exc))
        return result

    result["timestamp"] = info["timestamp"]
    result["time"] = info["time"]
    result["code_fp4"] = info["fp4_hex"]

    if not info["signature_ok"]:
        result.update(
            reason="bad_signature",
            detail=("防伪码签名校验失败：这张图的水印不是用当前密钥签发的。\n"
                    "可能是水印被人改过，也可能你用的密钥与生成时不同。"),
        )
        return result

    # 3) 重算图像指纹并比对
    fp_now = fingerprint(img)
    result["image_fp4"] = fp_now[:4].hex()

    if not hmac.compare_digest(fp_now[:4], info["fp4"]):
        result.update(
            reason="tampered",
            detail=(f"图像指纹不一致：水印记录 {info['fp4_hex']}，"
                    f"当前图片 {fp_now[:4].hex()}。\n"
                    f"水印区以外的像素被改动过（裁剪、缩放、重编码、涂改都算）。"),
        )
        return result

    # 4) 二维码里的时间应与防伪码里的时间一致（发现矛盾说明有人在拼凑）
    if payload_info and payload_info.get("timestamp") is not None:
        if int(payload_info["timestamp"]) != int(info["timestamp"]):
            result.update(
                reason="payload_conflict",
                detail=(f"二维码时间戳 {payload_info['timestamp']} 与"
                        f"防伪码内时间戳 {info['timestamp']} 不一致。"),
            )
            return result

    result.update(ok=True, reason="verified",
                  detail="防伪码签名有效，且图像指纹与水印记录一致。")
    return result
