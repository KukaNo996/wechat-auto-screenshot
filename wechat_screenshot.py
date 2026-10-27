"""
微信区域截图自动化工具
======================

模拟微信截图快捷键 (Alt+A)，完成区域框选，自动点击工具栏的「下载/保存」按钮，
在弹出的 Windows 保存对话框中写入按日期时间命名的文件名并保存到指定目录。

关键实现要点（均已在微信 4.0 / Weixin.exe 上实测验证）：
  1. 微信进程名        : Weixin.exe (4.0) / WeChat.exe (3.x)
  2. 截图浮层窗口      : class = Qt51514QWindowIcon, title = Weixin, 全屏尺寸
  3. 工具栏定位        : 灰度阈值 195 -> 水平闭运算(31px) -> 取最长亮条
  4. 保存按钮识别      : OpenCV 模板匹配（assets/save_button.png），阈值 0.75
  5. 保存对话框        : class = #32770, title = 保存图片, Button 文本 "保存(&S)"

作者: 使用 pywin32 + OpenCV + Pillow 实现
"""

from __future__ import annotations

import argparse
import ctypes
import datetime as dt
import glob
import json
import logging
import os
import re
import shutil
import sys
import tempfile
import time
import traceback
from dataclasses import dataclass, field, asdict
from typing import Optional, Sequence

try:
    import cv2
    import numpy as np
    import pyautogui
    import psutil
    import win32con
    import win32gui
    import win32process
    from PIL import Image
    from PIL import ImageGrab
except ImportError as exc:  # pragma: no cover
    print(f"[致命错误] 缺少依赖库: {exc}\n请先执行: pip install -r requirements.txt")
    raise

# ---------------------------------------------------------------------------
# 全局常量
# ---------------------------------------------------------------------------

APP_NAME = "wechat-screenshot"
LOGGER = logging.getLogger(APP_NAME)

#: 微信进程名候选（4.0 为 Weixin.exe，3.x 为 WeChat.exe）
WECHAT_PROCESSES = ("Weixin.exe", "WeChat.exe")

#: 微信截图浮层窗口类名
OVERLAY_CLASS = "Qt51514QWindowIcon"

#: 微信截图浮层窗口标题
OVERLAY_TITLE = "Weixin"

#: 微信截图工具栏上「下载/保存」按钮的模板图（相对本文件）
TEMPLATE_NAME = "save_button.png"

#: 工具栏检测参数
TOOLBAR_GRAY_THRESHOLD = 195   # 判定"浅色工具栏"的灰度阈值
TOOLBAR_MIN_WIDTH = 300        # 工具栏最短像素宽度
TOOLBAR_CLOSE_KERNEL = 31      # 水平闭运算核宽（桥接图标间隙）
TOOLBAR_MIN_SEGMENT = 40      # 单个亮段的最小长度
TOOLBAR_MIN_HEIGHT = 18        # 工具栏最小高度
TOOLBAR_MAX_HEIGHT = 120       # 工具栏最大高度

#: 保存对话框识别
SAVE_DIALOG_CLASS = "#32770"
SAVE_DIALOG_TITLE = "保存图片"
SAVE_DIALOG_TITLES = ("保存图片", "保存", "Save As", "Save Image", "另存为")


class ScreenshotError(Exception):
    """截图流程中可预期的业务异常（会被捕获并记录清晰日志）。"""


@dataclass
class RoundSpec:
    """单轮截图的任务描述。"""

    index: int                              # 轮次序号（从 1 开始）
    capture_mode: str = "region"            # region(框选) / full(全屏) / chat(聊天窗口)
    region: Optional[Sequence[int]] = None  # [x1, y1, x2, y2]；None = 手动框选
    prefix: Optional[str] = None            # 本轮文件名前缀
    session_keyword: Optional[str] = None   # chat 模式：按会话名过滤窗口
    chat_hwnd: Optional[int] = None         # chat 模式：直接指定窗口句柄

    @property
    def mode_label(self) -> str:
        if self.capture_mode == "full":
            return "全屏"
        if self.capture_mode == "chat":
            return f"聊天窗口({self.session_keyword or '自动'})"
        if self.region:
            return f"区域{list(self.region)}"
        return "手动框选"


# ---------------------------------------------------------------------------
# 配置
# ---------------------------------------------------------------------------

@dataclass
class Config:
    """全部可配置参数。字段含义见 README.md。"""

    # --- 输出 ---
    save_dir: str = os.path.join(os.path.expanduser("~"), "Pictures", "WeChatShot")
    filename_format: str = "{prefix}_{date}_{time}"
    prefix: str = "wechat"
    file_format: str = "png"          # png / jpg
    jpeg_quality: int = 95
    overwrite: bool = False           # 同名时是否覆盖
    keep_wechat_temp: bool = True     # 是否保留微信临时目录里的原文件

    # --- 交互 ---
    hotkey: Sequence[str] = ("alt", "a")
    trigger: str = "manual"# auto=工具代按快捷键 / manual=等你自己按
    capture_mode: str = "region"      # region(框选) / full(全屏) / chat(聊天窗口)
    region: Optional[Sequence[int]] = (0, 0, 1920, 1080)  # [x1, y1, x2, y2]；None 表示由用户手动框选
    drag_duration: float = 0.5
    session_keyword: Optional[str] = None    # chat 模式：按会话名过滤聊天窗口
    chat_hwnd: Optional[int] = None           # chat 模式：直接指定聊天窗口句柄

    # --- 监听模式 ---
    watch_interval: float = 0.0# 每次保存后到下次监听的间隔秒数（0=立即可再次触发）

    # --- 延时（秒）---
    delay_before_hotkey: float = 0.6      # 按快捷键前的等待
    delay_after_hotkey: float = 1.2       # 等待浮层弹出
    delay_after_select: float = 0.9       # 等待工具栏出现
    delay_after_download: float = 1.5     # 等待保存对话框弹出
    delay_after_save: float = 1.0         # 点击保存后等待落盘
    poll_interval: float = 0.15           # 轮询间隔

    # --- 超时（秒）---
    timeout_overlay: float = 0.0          # 等待浮层（0=不限时，一直等到你按快捷键）
    timeout_toolbar: float = 8.0          # 等待工具栏
    timeout_dialog: float = 10.0          # 等待保存对话框
    timeout_save: float = 15.0            # 等待文件真正落盘
    timeout_manual: float = 60.0          # 自动识别失败后，等用户手动点「下载」的上限

    # --- 识别参数 ---
    match_threshold: float = 0.75         # 模板匹配阈值
    template_path: Optional[str] = None   # 自定义模板图路径（留空用内置）

    # --- 进程 / 窗口 ---
    wechat_processes: Sequence[str] = WECHAT_PROCESSES
    activate_wechat: bool = True          # 是否先把微信窗口切到前台
    wechat_window_title: str = "微信"

    # --- 日志 ---
    log_level: str = "INFO"
    log_file: Optional[str] = None       # None 表示不写文件
    log_to_console: bool = True

    # --- 水印 / 防伪码 ---
    watermark_enabled: bool = True            # 截图后是否在左下角打水印
    watermark_font_size: int = 15             # 水印字号（自适应缩小以适配小图）
    watermark_opacity: float = 0.92           # 文字不透明度 0~1
    watermark_margin: int = 14                # 距图片边缘的内边距
    watermark_show_timestamp: bool = True     # 是否显示 Unix 时间戳那一行
    watermark_qr: bool = True                 # 是否在右侧放二维码（扫码即可读防伪码）
    watermark_qr_size: int = 120              # 二维码目标边长
    watermark_code_prefix: str = "WSS"        # 防伪码前缀标识
    watermark_secret_file: Optional[str] = None  # 密钥文件；None = ~/.wechat_screenshot/watermark.key

    # --- 其它 ---
    keep_overlay_on_error: bool = False   # 出错时是否保留浮层（便于排查）
    dry_run: bool = False                 # 只识别不点击（调试用）

    def __post_init__(self) -> None:
        self.hotkey = tuple(self.hotkey)
        self.wechat_processes = tuple(self.wechat_processes)
        if self.region is not None:
            self.region = tuple(int(v) for v in self.region)
            if len(self.region) != 4:
                raise ValueError("region 必须是 [x1, y1, x2, y2] 四个数字")
        if self.file_format.lower() not in ("png", "jpg", "jpeg"):
            raise ValueError("file_format 只能是 png 或 jpg")
        if not (0 < self.match_threshold <= 1.0):
            raise ValueError("match_threshold 必须在 (0, 1] 区间")
        if self.capture_mode not in ("region", "full", "chat"):
            raise ValueError("capture_mode 只能是 'region'、'full' 或 'chat'")
        if self.trigger not in ("auto", "manual"):
            raise ValueError("trigger 只能是 'auto'（工具代按）或 'manual'（你手动按）")
        if self.watch_interval < 0:
            raise ValueError("watch_interval 不能为负数")
        if not 6 <= int(self.watermark_font_size) <= 48:
            raise ValueError("watermark_font_size 建议在 6~48 之间")
        if not 0.0 <= float(self.watermark_opacity) <= 1.0:
            raise ValueError("watermark_opacity 必须在 0~1 之间")
        if int(self.watermark_qr_size) < 32:
            raise ValueError("watermark_qr_size 至少 32")

        # 水印核验依赖无损像素：配了 jpg 就自动纠正为 png，
        # 否则会出现「图片上有防伪码，但一核验就报篡改」的自相矛盾。
        self.format_forced_by_watermark = (
            bool(self.watermark_enabled) and self.file_format.lower() in ("jpg", "jpeg")
        )
        if self.format_forced_by_watermark:
            self.file_format = "png"

    @property
    def extension(self) -> str:
        return "jpg" if self.file_format.lower() in ("jpg", "jpeg") else "png"

    def build_filename(self, when: dt.datetime, prefix: Optional[str] = None) -> str:
        """按命名规则生成文件名（不含扩展名）。"""
        name = self.filename_format.format(
            prefix=prefix if prefix is not None else self.prefix,
            date=when.strftime("%Y%m%d"),
            time=when.strftime("%H%M%S"),
            datetime=when.strftime("%Y%m%d_%H%M%S"),
            millis=when.strftime("%f")[:-3],
            timestamp=int(when.timestamp()),
            user=os.environ.get("USERNAME", "user"),
        )
        # 清理非法文件名字符
        name = re.sub(r'[\\/:*?"<>|]+', "_", name)
        name = re.sub(r"_+", "_", name).strip(" ._")
        return name or f"wechat_{when.strftime('%Y%m%d_%H%M%S')}"

    def task_spec(self) -> RoundSpec:
        """
        生成本次监听要重复执行的任务描述（监听模式只有一种任务，
        触发一次跑一轮，循环到 Ctrl+C）。
        """
        return RoundSpec(
            index=1,
            capture_mode=self.capture_mode,
            region=self.region,
            prefix=self.prefix,
            session_keyword=self.session_keyword,
            chat_hwnd=self.chat_hwnd,
        )


def acquire_single_instance_lock():
    """
    获取单实例锁（基于命名互斥体）。

    为什么必须：多开时各实例会互相抢微信浮层与「保存图片」对话框 ——
    表现为 A 实例点击了下载、B 实例截获对话框并保存，A 则报
    "未出现对话框"。这类故障极难排查，从入口上直接禁止多开。

    返回 (句柄, 是否成功)。已存在实例时句柄为 None。
    """
    try:
        import ctypes
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.CreateMutexW(None, False, "Global\\WeChatShotToolSingleton")
        if not handle:
            return None, True                     # API 不可用时不阻断
        already = kernel32.GetLastError() == 183   # 183 = ERROR_ALREADY_EXISTS
        if already:
            kernel32.CloseHandle(handle)
            return None, False
        return handle, True
    except Exception as exc:
        LOGGER.debug("单实例锁不可用(忽略): %s", exc)
        return None, True


def load_config(path: Optional[str]) -> Config:
    """
    从 JSON 文件载入配置；未提供则使用默认值。

    支持以 `_` 开头的注释键（如 "_说明": "…"），它们只用于阅读，
    不会传给 Config，也不会触发"未知项"告警。这样示例配置可以既保持
    分段可读、又能直接复制使用。
    """
    if not path:
        return Config()
    if not os.path.isfile(path):
        raise FileNotFoundError(f"配置文件不存在: {path}")
    with open(path, "r", encoding="utf-8") as fh:
        raw = json.load(fh)

    known = {f.name for f in Config.__dataclass_fields__.values()}  # type: ignore[attr-defined]
    # 过滤注释键（_ 前缀，通常是嵌套的分组对象）
    raw = {k: v for k, v in raw.items()
           if not k.startswith("_") or k in known}
    unknown = set(raw) - known
    if unknown:
        LOGGER.warning("配置中存在未知项，已忽略: %s", ", ".join(sorted(unknown)))
    raw = {k: v for k, v in raw.items() if k in known}
    return Config(**raw)


# ---------------------------------------------------------------------------
# 日志
# ---------------------------------------------------------------------------

def setup_logging(cfg: Config) -> None:
    """初始化日志：控制台 + 可选文件。"""
    LOGGER.setLevel(getattr(logging, cfg.log_level.upper(), logging.INFO))
    LOGGER.handlers.clear()
    fmt = logging.Formatter(
        "%(asctime)s [%(levelname)-7s] %(message)s", datefmt="%Y-%m-%d %H:%M:%S"
    )

    if cfg.log_to_console:
        sh = logging.StreamHandler(sys.stdout)
        sh.setFormatter(fmt)
        LOGGER.addHandler(sh)

    if cfg.log_file:
        # 日志文件只是辅助，写不进去不应该让整个程序启动失败
        # （典型场景：配置里写了一个本机不存在的盘符）
        try:
            log_dir = os.path.dirname(os.path.abspath(cfg.log_file))
            os.makedirs(log_dir, exist_ok=True)
            fh = logging.FileHandler(cfg.log_file, encoding="utf-8")
            fh.setFormatter(fmt)
            LOGGER.addHandler(fh)
            LOGGER.info("日志文件: %s", cfg.log_file)
        except Exception as exc:
            LOGGER.warning(
                "无法写入日志文件 %s（%s），已降级为仅输出到终端", cfg.log_file, exc
            )


# ---------------------------------------------------------------------------
# Windows 底层辅助
# ---------------------------------------------------------------------------

def _ensure_dpi_awareness() -> None:
    """让进程感知 DPI，避免高分屏坐标错位。"""
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)   # PROCESS_PER_MONITOR_DPI_AWARE
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass


def _process_name(pid: int) -> str:
    try:
        return psutil.Process(pid).name()
    except Exception:
        return ""


def find_wechat_processes(names: Sequence[str]) -> list:
    """查找正在运行的微信进程。"""
    wanted = {n.lower() for n in names}
    found = []
    for proc in psutil.process_iter(["name", "pid"]):
        try:
            if (proc.info["name"] or "").lower() in wanted:
                found.append(proc.info)
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    return found


def has_session_sidebar(hwnd: int) -> bool:
    """
    判断窗口是否是"微信主界面"（左侧带会话列表栏）。

    微信 4.0 的独立聊天窗口与主界面的标题可能完全相同（都是会话名），
    唯一稳定的区别是：主界面左侧有会话列表（一列头像方块），
    独立聊天窗口没有。

    判据：窗口左侧 300px 区域内，暗色小方块（头像）的密度。
    """
    try:
        l, t, r, b = win32gui.GetWindowRect(hwnd)
    except Exception:
        return False
    if r - l < 400 or b - t < 300:
        return False
    try:
        strip = ImageGrab.grab(
            bbox=(l, t, l + 300, min(t + 500, b)), all_screens=True)
    except Exception:
        return False
    arr = np.array(strip.convert("L")).astype(int)
    if arr.size == 0:
        return False
    count = 0
    for yy in range(0, max(arr.shape[0] - 30, 1), 10):
        for xx in range(0, max(arr.shape[1] - 30, 1), 10):
            if arr[yy:yy + 30, xx:xx + 30].mean() < 120:
                count += 1
    return count >= 40


def find_wechat_window(title: str = "微信") -> Optional[int]:
    """
    查找微信**主界面**窗口句柄。

    微信 4.0 的窗口标题会跟随当前会话变化（可能是某个聊天名），
    而且还存在"独立聊天窗口"，两者标题可能一模一样。
    区分方法：主界面左侧有会话列表栏，独立聊天窗口没有。
    """
    exact = None
    candidates = []

    def cb(hwnd, _):
        nonlocal exact
        if not win32gui.IsWindowVisible(hwnd):
            return True
        try:
            _, pid = win32process.GetWindowThreadProcessId(hwnd)
        except Exception:
            return True
        if _process_name(pid).lower() not in ("weixin.exe", "wechat.exe"):
            return True
        if win32gui.GetClassName(hwnd) != OVERLAY_CLASS:
            return True
        wtitle = win32gui.GetWindowText(hwnd)
        if wtitle == OVERLAY_TITLE:      # 截图浮层
            return True
        try:
            l, t, r, b = win32gui.GetWindowRect(hwnd)
        except Exception:
            return True
        w, h = r - l, b - t
        if w < 400 or h < 400:
            return True
        if wtitle == title and has_session_sidebar(hwnd):
            exact = hwnd
        candidates.append((hwnd, w * h))
        return True

    try:
        win32gui.EnumWindows(cb, None)
    except Exception:
        pass

    if exact is not None:
        return exact
    # 兜底：面积最大的可见窗口
    if candidates:
        return max(candidates, key=lambda x: x[1])[0]
    return None


def force_foreground(hwnd: int) -> bool:
    """
    强制把窗口切到前台。

    Windows 的「前台锁定」会阻止外部进程直接 SetForegroundWindow ——
    尤其是当用户刚在别的程序（如 IDE）里操作过时，抢焦点几乎必然失败。

    这里用组合策略，逐级加强：
      1. SetForegroundWindow（最轻，无副作用）
      2. 模拟 F13 解除锁定后重试
      3. Alt+Tab 循环切换，直到目标窗口成为前台

    注意：绝不能用 Shift/Alt 之类可能被应用绑定为快捷键的按键 ——
    微信独立聊天窗口的图钉就绑定在鼠标上，误触会改变窗口状态。
    F13 几乎不会被任何应用绑定，可安全使用。
    """
    def is_foreground() -> bool:
        try:
            fg = win32gui.GetForegroundWindow()
            if fg == hwnd:
                return True
            root = win32gui.GetAncestor(hwnd, win32con.GA_ROOT)
            return bool(root) and fg == root
        except Exception:
            return False

    try:
        if not win32gui.IsWindowVisible(hwnd):
            win32gui.ShowWindow(hwnd, 5)  # SW_SHOW
            time.sleep(0.3)
        if win32gui.IsIconic(hwnd):
            win32gui.ShowWindow(hwnd, 9)  # SW_RESTORE
            time.sleep(0.3)
    except Exception:
        pass

    # 策略 1 & 2：直接置前
    for attempt in range(2):
        if attempt == 1:
            try:
                pyautogui.keyDown("f13")
                pyautogui.keyUp("f13")
            except Exception:
                pass
        try:
            win32gui.BringWindowToTop(hwnd)
            win32gui.SetForegroundWindow(hwnd)
        except Exception as exc:
            LOGGER.debug("SetForegroundWindow 失败: %s", exc)
        time.sleep(0.3)
        if is_foreground():
            return True

    # 策略 3：Alt+Tab 循环切换
    try:
        for _ in range(12):
            pyautogui.hotkey("alt", "tab")
            time.sleep(0.35)
            if is_foreground():
                LOGGER.debug("通过 Alt+Tab 成功切到目标窗口")
                return True
    except Exception as exc:
        LOGGER.debug("Alt+Tab 切换失败: %s", exc)

    return is_foreground()


def activate_wechat(title: str = "微信") -> bool:
    """
    把微信主界面切到前台。

    微信主窗口有时是隐藏的（只显示独立聊天窗口），这时需要先 ShowWindow
    把它显示出来，否则 Alt+A 会被发到聊天小窗上，导致截图浮层行为异常。
    """
    hwnd = find_wechat_window(title)
    if not hwnd:
        LOGGER.debug("未找到微信主窗口 (title=%s)，跳过激活", title)
        return False
    try:
        if not win32gui.IsWindowVisible(hwnd):
            LOGGER.info("微信主窗口未显示，先将其显示出来")
            win32gui.ShowWindow(hwnd, 5)  # SW_SHOW
            time.sleep(0.3)
        if win32gui.IsIconic(hwnd):
            win32gui.ShowWindow(hwnd, 9)  # SW_RESTORE
            time.sleep(0.2)
        ok = force_foreground(hwnd)
        if ok:
            LOGGER.debug("已激活微信主窗口 hwnd=%s", hwnd)
        else:
            LOGGER.debug("微信主窗口置前未完全成功 hwnd=%s", hwnd)
        return ok
    except Exception as exc:
        LOGGER.debug("激活微信窗口失败(可忽略): %s", exc)
        return False


def scan_overlay_once(class_name: str = OVERLAY_CLASS,
                      title: str = OVERLAY_TITLE) -> list:
    """扫描一次窗口，返回当前所有微信截图浮层的 hwnd 列表（不等待）。"""
    hits = []

    def cb(hwnd, _):
        if not win32gui.IsWindowVisible(hwnd):
            return True
        try:
            _, pid = win32process.GetWindowThreadProcessId(hwnd)
        except Exception:
            return True
        if _process_name(pid).lower() not in ("weixin.exe", "wechat.exe"):
            return True
        if win32gui.GetClassName(hwnd) != class_name:
            return True
        if win32gui.GetWindowText(hwnd) != title:
            return True
        try:
            l, t, r, b = win32gui.GetWindowRect(hwnd)
        except Exception:
            return True
        # 浮层是全屏尺寸
        if (r - l) > 800 and (b - t) > 600:
            hits.append(hwnd)
        return True

    try:
        win32gui.EnumWindows(cb, None)
    except Exception:
        pass
    return hits


def find_overlay_window(timeout: float = 0.0,
                        poll: float = 0.15,
                        class_name: str = OVERLAY_CLASS,
                        title: str = OVERLAY_TITLE) -> Optional[int]:
    """查找微信截图浮层窗口（全屏、标题 Weixin）。

    timeout <= 0 表示**无限等待**（监听模式用），直到浮层出现为止。
    Ctrl+C 会在 sleep 间隙被抛出，因此长等待不会卡住退出。
    """
    deadline = time.time() + max(timeout, 0.0)
    while True:
        hits = scan_overlay_once(class_name, title)
        if hits:
            return hits[0]
        if timeout > 0 and time.time() >= deadline:
            return None
        time.sleep(poll)


def _require_overlay(overlay_hwnd: int, action: str) -> None:
    """
    在执行鼠标操作（拖拽选区、点击按钮）前确认浮层仍在。

    若浮层已消失，说明用户已取消或微信超时关闭了它。此时若继续拖拽/点击，
    命中的是用户自己的窗口 —— 这正是"截图截着截着把页面关掉"的成因。
    直接中止本轮，不产生任何副作用。
    """
    try:
        alive = bool(scan_overlay_once())
    except Exception:
        alive = True                      # 探测失败时不阻塞正常流程
    if not alive:
        raise ScreenshotError(
            f"截图浮层已消失，已放弃{action}以免误操作你的窗口。\n"
            f"可能原因：① 你按了 Esc 取消截图；② 微信浮层超时自动关闭；"
            f"③ 微信窗口被关闭。\n工具会继续等待下一次触发。"
        )


def close_overlay() -> bool:
    """
    关闭可能残留的截图浮层。

    安全约束（重要）：
      只在**确实检测到浮层窗口**时才动作。没有浮层时绝不发任何按键 ——
      否则监听模式下每轮开始前都会把 ESC 打到你的前台窗口上，
      导致浏览器/编辑器无辜关闭页面。

      即使检测到浮层，也用 PostMessage 定向投递给浮层自身，
      不抢你正在操作的那个窗口的键盘焦点。

    返回 True 表示确实清理过一个浮层。
    """
    try:
        hits = scan_overlay_once()
    except Exception:
        hits = []
    if not hits:
        return False
    overlay = hits[0]

    try:
        win32gui.PostMessage(overlay, win32con.WM_KEYDOWN, win32con.VK_ESCAPE, 0)
        win32gui.PostMessage(overlay, win32con.WM_KEYUP, win32con.VK_ESCAPE, 0)
        LOGGER.debug("已向浮层 %s 投递 ESC 消息", overlay)
        return True
    except Exception as exc:
        LOGGER.debug("投递 ESC 消息失败，回退为模拟按键: %s", exc)
        # 仅在浮层确证存在时才回退，作用域仍然安全
        try:
            pyautogui.press("esc")
        except Exception:
            pass
        return True


# ---------------------------------------------------------------------------
# 保存对话框
# ---------------------------------------------------------------------------

@dataclass
class SaveDialogInfo:
    hwnd: int
    filename_edit: Optional[int] = None
    save_button: Optional[int] = None
    cancel_button: Optional[int] = None
    button_text: str = ""


def find_save_dialog(timeout: float = 10.0, poll: float = 0.2) -> Optional[SaveDialogInfo]:
    """查找 Windows 保存对话框并定位文件名输入框与「保存」按钮。"""
    deadline = time.time() + max(timeout, 0.0)
    while True:
        found = None

        def cb(hwnd, _):
            nonlocal found
            if not win32gui.IsWindowVisible(hwnd):
                return True
            if win32gui.GetClassName(hwnd) != SAVE_DIALOG_CLASS:
                return True
            title = win32gui.GetWindowText(hwnd)
            if title not in SAVE_DIALOG_TITLES:
                return True
            found = hwnd
            return True

        try:
            win32gui.EnumWindows(cb, None)
        except Exception:
            pass

        if found:
            info = SaveDialogInfo(hwnd=found)
            buttons = []
            edits = []

            def cb_child(hc, _):
                cls = win32gui.GetClassName(hc)
                txt = win32gui.GetWindowText(hc)
                if cls == "Button":
                    buttons.append((hc, txt))
                elif cls == "Edit":
                    edits.append(hc)
                return True

            try:
                win32gui.EnumChildWindows(found, cb_child, None)
            except Exception:
                pass

            # 文件名输入框：取尺寸最大的 Edit（对话框里搜索框也是 Edit）
            if edits:
                def area(hc):
                    try:
                        l, t, r, b = win32gui.GetWindowRect(hc)
                        return (r - l) * (b - t)
                    except Exception:
                        return 0
                info.filename_edit = max(edits, key=area)

            # 保存按钮：文本里含 "保存" 或 "Save"，排除 "取消"
            for hc, txt in buttons:
                clean = re.sub(r"[&()]", "", txt)
                if ("保存" in clean or clean.lower().startswith("save")) and \
                   "取消" not in clean and "cancel" not in clean.lower():
                    info.save_button = hc
                    info.button_text = txt
                    break
            for hc, txt in buttons:
                clean = re.sub(r"[&()]", "", txt)
                if "取消" in clean or clean.lower().startswith("cancel"):
                    info.cancel_button = hc
                    break
            return info

        if time.time() >= deadline:
            return None
        time.sleep(poll)


def set_filename(edit_hwnd: int, filename: str) -> bool:
    """向对话框的文件名输入框写入文件名。"""
    try:
        win32gui.SendMessage(edit_hwnd, win32con.WM_SETTEXT, 0, filename)
        return True
    except Exception as exc:
        LOGGER.debug("SendMessage 写文件名失败，改用键盘输入: %s", exc)

    # 回退：聚焦后全选输入
    try:
        win32gui.SetForegroundWindow(win32gui.GetParent(edit_hwnd) or edit_hwnd)
        time.sleep(0.15)
        pyautogui.hotkey("ctrl", "a")
        time.sleep(0.05)
        pyautogui.typewrite(filename, interval=0.01)
        return True
    except Exception as exc:
        LOGGER.error("写入文件名失败: %s", exc)
        return False


def click_button(btn_hwnd: int) -> bool:
    """点击对话框按钮（用 BM_CLICK，避免依赖前台焦点）。"""
    try:
        win32gui.SendMessage(btn_hwnd, win32con.BM_CLICK, 0, 0)
        return True
    except Exception as exc:
        LOGGER.debug("BM_CLICK 失败，改用鼠标点击: %s", exc)
        try:
            l, t, r, b = win32gui.GetWindowRect(btn_hwnd)
            pyautogui.click((l + r) // 2, (t + b) // 2)
            return True
        except Exception as exc2:
            LOGGER.error("点击按钮失败: %s", exc2)
            return False


# ---------------------------------------------------------------------------
# 微信聊天独立窗口
# ---------------------------------------------------------------------------

#: 微信 4.0 聊天记录独立窗口的标题特征，形如  "群名"的聊天记录(24)
CHAT_WINDOW_TITLE_MARK = "的聊天记录"

#: 独立聊天窗口的最小尺寸（用于排除托盘/输入法等辅助窗口）
CHAT_WINDOW_MIN_SIZE = 200

#: 聊天窗口被"图钉"后会被缩成一条小横条，这类窗口无内容可截，需排除
PINNED_WINDOW_MAX_HEIGHT = 120


@dataclass
class ChatWindowInfo:
    """一个微信聊天窗口。"""

    hwnd: int
    title: str
    rect: tuple                       # (left, top, right, bottom)
    kind: str = "chat"                # chat(聊天主界面) / history(聊天记录)

    @property
    def size(self) -> tuple:
        l, t, r, b = self.rect
        return (r - l, b - t)

    @property
    def session_name(self) -> str:
        """从标题中提取会话名。"""
        title = self.title
        m = re.match(r'^"(.*?)"的聊天记录', title)
        if m:
            return m.group(1)
        # 去掉末尾的 (N) 未读数后缀
        return re.sub(r"\(\d+\)$", "", title).strip()

    @property
    def kind_label(self) -> str:
        return "聊天记录窗口" if self.kind == "history" else "独立聊天窗口"


def find_chat_windows(keyword: Optional[str] = None,
                      include_main: bool = True) -> list:
    """
    查找微信的聊天相关顶层窗口。

    微信 4.0 的顶层窗口可分为：
      1. 微信主界面    —— 标题「微信」，左侧有会话列表栏
      2. 独立聊天窗口  —— 标题是会话名，左上角有图钉图标
      3. 聊天记录窗口  —— 标题形如  "群名"的聊天记录(24)
      4. 截图浮层      —— 标题 Weixin

    主界面与独立聊天窗口的标题可能相同（都可能是会话名），
    靠肉眼难以区分，因此这里把它们都列出来，由调用方通过
    关键字 / hwnd / 列表序号来指定。

    include_main=False 时排除标题为「微信」的主界面。
    """
    results = []

    def cb(hwnd, _):
        if not win32gui.IsWindowVisible(hwnd):
            return True
        title = win32gui.GetWindowText(hwnd)
        if not title:
            return True
        try:
            _, pid = win32process.GetWindowThreadProcessId(hwnd)
        except Exception:
            return True
        if _process_name(pid).lower() not in ("weixin.exe", "wechat.exe"):
            return True
        if win32gui.GetClassName(hwnd) != OVERLAY_CLASS:
            return True
        # 排除截图浮层
        if title == OVERLAY_TITLE:
            return True
        try:
            l, t, r, b = win32gui.GetWindowRect(hwnd)
        except Exception:
            return True
        w, h = r - l, b - t
        if w < CHAT_WINDOW_MIN_SIZE or h < CHAT_WINDOW_MIN_SIZE:
            return True
        # 排除被"图钉"缩成小横条的窗口（无内容可截）
        if h < PINNED_WINDOW_MAX_HEIGHT:
            return True
        if not include_main and title == "微信":
            return True

        kind = "history" if CHAT_WINDOW_TITLE_MARK in title else "chat"
        # 主界面（标题「微信」）不算独立聊天窗口
        if not include_main and (title == "微信" or has_session_sidebar(hwnd)):
            return True
        results.append(ChatWindowInfo(hwnd, title, (l, t, r, b), kind))
        return True

    try:
        win32gui.EnumWindows(cb, None)
    except Exception:
        pass

    if keyword:
        kw = keyword.strip()
        results = [c for c in results if kw in c.title or kw in c.session_name]
    return results


def resolve_chat_window(spec: RoundSpec) -> ChatWindowInfo:
    """
    解析出要截图的聊天窗口。

    优先级：
      1. spec.chat_hwnd  —— 直接指定窗口句柄（最精确）
      2. spec.session_keyword —— 按标题/会话名关键字匹配
      3. 唯一一个候选 —— 直接用
      4. 多候选 —— 优先选「聊天记录窗口」，否则取第一个并给出提示
    """
    # 1. 指定 hwnd
    if spec.chat_hwnd:
        if not win32gui.IsWindow(spec.chat_hwnd):
            raise ScreenshotError(f"指定的窗口句柄无效: {spec.chat_hwnd}")
        title = win32gui.GetWindowText(spec.chat_hwnd)
        l, t, r, b = win32gui.GetWindowRect(spec.chat_hwnd)
        kind = "history" if CHAT_WINDOW_TITLE_MARK in title else "chat"
        return ChatWindowInfo(spec.chat_hwnd, title, (l, t, r, b), kind)

    windows = find_chat_windows(spec.session_keyword)
    if not windows:
        # 看看是不是只有主界面
        main_only = find_wechat_window("微信")
        if spec.session_keyword:
            raise ScreenshotError(
                f"未找到标题包含「{spec.session_keyword}」的微信聊天窗口。\n"
                f"可能原因：① 该聊天窗口未打开（需在微信里打开该会话）；"
                f"② 会话名与关键字不匹配。\n"
                f"提示：用 --list-windows 查看当前所有可截图窗口。"
            )
        raise ScreenshotError(
            "未找到微信聊天独立窗口。\n"
            "当前微信只显示了主界面（含左侧会话列表），没有独立的聊天窗口。\n"
            "解决办法（二选一）：\n"
            "  ① 在微信中双击某个会话，打开独立聊天窗口后重试；\n"
            "  ② 改用 --mode region / --mode full 截取屏幕区域。"
            + (f"\n当前微信主窗口句柄: {main_only}（可用 --chat-hwnd 指定）" if main_only else "")
        )

    if len(windows) == 1:
        return windows[0]

    # 多候选：优先聊天记录窗口
    for c in windows:
        if c.kind == "history":
            return c
    return windows[0]


def capture_chat_window(cfg: Config, spec: RoundSpec, when: dt.datetime) -> str:
    """
    抓取微信聊天独立窗口并保存。

    与 Alt+A 流程不同，这里直接按窗口矩形抓取屏幕像素
    （微信是 Qt 渲染，PrintWindow 拿不到内容，只能走屏幕抓取）。
    因此必须先把目标窗口置前，否则会截到遮挡它的其它窗口。
    """
    target = resolve_chat_window(spec)
    LOGGER.info("目标聊天窗口: [%s] (%s) 尺寸=%s hwnd=%s",
                target.title, target.kind_label, target.size, target.hwnd)

    # 置前，避免截到遮挡内容
    if not force_foreground(target.hwnd):
        LOGGER.warning(
            "无法将聊天窗口切到前台（Windows 前台锁定）。\n"
            "        抓取结果可能被其它窗口遮挡。\n"
            "        建议：先手动点击一下该聊天窗口，再重新运行。"
        )
    else:
        LOGGER.info("已将聊天窗口切到前台")
    time.sleep(0.5)

    # 重新取矩形（置前后位置可能变化）
    try:
        l, t, r, b = win32gui.GetWindowRect(target.hwnd)
        target.rect = (l, t, r, b)
    except Exception:
        pass

    l, t, r, b = target.rect
    if r - l < 50 or b - t < 50:
        raise ScreenshotError(f"聊天窗口尺寸异常: {(l, t, r, b)}")

    img = ImageGrab.grab(bbox=(l, t, r, b), all_screens=True)

    # 校验：全黑或全白说明抓取失败
    arr = np.array(img.convert("L"))
    if arr.std() < 1.0:
        raise ScreenshotError(
            f"抓取到的聊天窗口内容为纯色（标准差 {arr.std():.2f}），判定为失败。\n"
            f"可能原因：窗口最小化、被完全遮挡、或显卡加速导致无法抓取。\n"
            f"建议：确保聊天窗口可见且未最小化。"
        )

    ensure_save_dir(cfg)
    stem = cfg.build_filename(when, spec.prefix)
    dest = unique_path(cfg.save_dir, stem, cfg.extension, cfg.overwrite)

    if cfg.extension == "jpg":
        img.convert("RGB").save(dest, "JPEG", quality=cfg.jpeg_quality)
    else:
        img.save(dest)

    LOGGER.info("✅ 聊天窗口截图已保存: %s (%dx%d)", dest, img.width, img.height)
    return dest


# ---------------------------------------------------------------------------
# 工具栏与模板匹配
# ---------------------------------------------------------------------------

def toolbar_band_around(img: Image.Image, cx: int, cy: int) -> tuple:
    """
    已知「下载」按钮中心，反推工具栏的横向范围。

    从按钮处向左右扫描"浅色底"像素，遇到明显的暗色间隙（图标）继续，
    遇到真正的背景（连续暗区）停止。返回 (x0, y0, x1, y1)。

    仅用于日志展示与 --make-template，不参与按钮定位。
    """
    gray = np.array(img.convert("L"))
    H, W = gray.shape
    y0 = max(0, cy - 18)
    y1 = min(H - 1, cy + 18)

    probe = gray[y0:y1 + 1, :]
    light_col = (probe > TOOLBAR_GRAY_THRESHOLD).mean(axis=0)

    def walk(direction: int) -> int:
        pos = cx
        while True:
            nxt = pos + direction
            if nxt < 0 or nxt >= W:
                return pos
            # 连续 8 列都偏暗 -> 认为是工具栏外部背景
            if light_col[max(0, nxt - 4):nxt + 4].mean() < 0.5:
                return pos
            pos = nxt

    left = walk(-1)
    right = walk(1)
    return (max(0, left), y0, min(W - 1, right), y1)


def find_toolbar_candidates(img: Image.Image) -> list:
    """
    找出所有可能是工具栏的扁长浅色区域（仅用于 --make-template 与调试）。

    注意：主流程**不再**依赖本函数定位按钮——浮层激活时整屏变亮、
    工具栏可能压在其它浅色面板（如图片预览、状态栏）上，基于
    "浅色扁长条"的几何假设极易误判。按钮定位请用
    find_save_button_anywhere()。

    返回 [(x0, y0, x1, y1), ...]，按宽度从大到小排序。
    """
    arr = np.array(img.convert("RGB"))
    gray = cv2.cvtColor(arr, cv2.COLOR_RGB2GRAY)
    light = (gray > TOOLBAR_GRAY_THRESHOLD).astype(np.uint8)
    closed = cv2.morphologyEx(light, cv2.MORPH_CLOSE,
                              np.ones((1, TOOLBAR_CLOSE_KERNEL), np.uint8))

    H, W = closed.shape
    row_bands = []

    for y in range(H):
        diff = np.diff(np.concatenate(([0], closed[y], [0])))
        starts = np.where(diff == 1)[0]
        ends = np.where(diff == -1)[0]
        if len(starts) == 0:
            continue
        widths = ends - starts
        segs = [(int(s), int(e) - 1) for s, e, w in zip(starts, ends, widths)
                if w >= TOOLBAR_MIN_SEGMENT]
        if segs:
            row_bands.append((y, segs))

    if not row_bands:
        return []

    # 把 y 连续的行聚成水平带
    groups = []
    current = [row_bands[0]]
    for band in row_bands[1:]:
        if band[0] - current[-1][0] <= 3:
            current.append(band)
        else:
            groups.append(current)
            current = [band]
    groups.append(current)

    candidates = []
    for grp in groups:
        y0 = grp[0][0]
        y1 = grp[-1][0]
        h = y1 - y0 + 1
        if h < TOOLBAR_MIN_HEIGHT or h > TOOLBAR_MAX_HEIGHT:
            continue

        # 合并该水平带内相邻的亮段（间隙 <= 80px 视为同一物体）
        segs = []
        for _, ss in grp:
            for s, e in ss:
                if segs and s - segs[-1][1] <= 80:
                    segs[-1] = (segs[-1][0], e)
                else:
                    segs.append((s, e))

        for s, e in segs:
            w = e - s + 1
            if w < TOOLBAR_MIN_WIDTH:
                continue
            if w / h < 5:
                continue
            candidates.append((s, y0, e, y1))

    candidates.sort(key=lambda c: c[2] - c[0], reverse=True)
    return candidates


def detect_toolbar(img: Image.Image, template: Optional[np.ndarray] = None,
                   threshold: float = 0.75) -> Optional[tuple]:
    """
    定位截图浮层。返回工具栏带 (x0, y0, x1, y1)；找不到返回 None。

    有模板时走"全屏模板匹配"（稳健，不依赖几何假设）；
    无模板时退回几何候选（仅够 --make-template 用）。
    """
    if template is not None:
        hit = find_save_button_anywhere(img, template, threshold)
        if hit:
            _, cx, cy = hit
            return toolbar_band_around(img, cx, cy)
        return None

    candidates = find_toolbar_candidates(img)
    return candidates[0] if candidates else None


def load_template(cfg: Config) -> np.ndarray:
    """载入「保存」按钮模板图。"""
    path = cfg.template_path
    if not path:
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "assets", TEMPLATE_NAME)
    if not os.path.isfile(path):
        raise ScreenshotError(
            f"找不到保存按钮模板: {path}\n"
            f"请执行 python wechat_screenshot.py --make-template 重新生成模板。"
        )
    tpl = cv2.imread(path, cv2.IMREAD_GRAYSCALE)
    if tpl is None:
        raise ScreenshotError(f"模板图无法读取: {path}")
    return tpl


def get_taskbar_top() -> int:
    """
    返回任务栏顶边 y 坐标；没有任务栏时返回 -1。

    为什么需要它：全屏框选时微信工具栏会被顶到屏幕最底部，其下半部分
    可能被任务栏遮住。模板匹配在整幅截图上仍能匹配到（任务栏上方的
    可见部分），但算出的按钮中心 cy 可能正好落在任务栏内 —— 那一击
    会被任务栏吃掉，表现为"点了下载却没反应"。
    """
    tops = []

    def cb(hwnd, _):
        try:
            if win32gui.GetClassName(hwnd) == "Shell_TrayWnd":
                _, t, _, _ = win32gui.GetWindowRect(hwnd)
                tops.append(int(t))
        except Exception:
            pass
        return True

    try:
        win32gui.EnumWindows(cb, None)
    except Exception:
        pass
    return min(tops) if tops else -1


def avoid_taskbar(cx: int, cy: int, template: np.ndarray) -> tuple:
    """
    若 (cx, cy) 落在任务栏内，把点击点移到任务栏上沿（按钮可见部分）。

    只调整 y，x 保持不变 —— 水平方向任务栏不会遮挡。
    """
    top = get_taskbar_top()
    th = int(template.shape[0])
    if top < 0 or cy < top:
        return cx, cy
    # 上移到按钮可见区域的中部，留 2px 余量避免压边
    safe_y = top - max(th // 2, 2) - 2
    LOGGER.info("按钮中心 y=%d 落在任务栏内(top=%d)，点击点上移至 %d",
                cy, top, safe_y)
    return cx, max(safe_y, 1)


def find_save_button_anywhere(img: Image.Image, template: np.ndarray,
                             threshold: float = 0.75,
                             top_n: int = 5) -> Optional[tuple]:
    """
    在整幅屏幕中定位「下载/保存」按钮，返回 (score, cx, cy)；找不到返回 None。

    为什么不先"找工具栏再匹配"：
        工具栏是浮层动画的一部分，出现时机与位置都不稳定；而"下载"按钮
        是整个浮层上唯一稳定可识别的特征。实测（1920x1080 深色主题）：
          - 工具栏已出现时，最高得分 1.0000，次高干扰 0.5986
          - 工具栏未出现时，全屏最高得分仅 0.34~ 0.40
        两者分离度极大，说明"全屏直接匹配 + 阈值"已经足够可靠，
        且完全不依赖几何假设（浮层激活时整屏变亮、工具栏可能压在
        其它浅色面板上、任务栏/状态栏都可能与它同色 —— 这些都会让
        基于"浅色扁长条"的定位失败）。

    返回前会做一次局部峰值抑制，确保 top_n 是彼此分离的候选。
    """
    gray = np.array(img.convert("L"))
    th, tw = template.shape[:2]
    if gray.shape[0] < th or gray.shape[1] < tw:
        return None

    result = cv2.matchTemplate(gray, template, cv2.TM_CCOEFF_NORMED)
    work = result.copy()
    pad_x, pad_y = tw, th          # 抑制窗口：与模板同尺寸，避免取到同一个峰
    best = None

    for _ in range(max(top_n, 1)):
        _, max_val, _, max_loc = cv2.minMaxLoc(work)
        if max_val < threshold:
            break
        cx = max_loc[0] + tw // 2
        cy = max_loc[1] + th // 2
        if best is None or max_val > best[0]:
            best = (float(max_val), cx, cy)
        x0 = max(0, max_loc[0] - pad_x)
        y0 = max(0, max_loc[1] - pad_y)
        work[y0:max_loc[1] + pad_y, x0:max_loc[0] + pad_x] = -1.0

    return best


def match_save_button(img: Image.Image, toolbar: tuple, template: np.ndarray,
                      threshold: float = 0.75) -> Optional[tuple]:
    """
    在给定区域（通常是工具栏带）内用模板匹配定位「下载/保存」按钮。

    保留此函数用于 --make-template：在已知的工具栏范围内匹配比全屏更可控。
    返回 (score, cx, cy)；未找到返回 None。
    """
    x0, y0, x1, y1 = toolbar
    gray = np.array(img.convert("L"))
    band = gray[y0:y1 + 1, x0:x1 + 1]
    th, tw = template.shape[:2]
    if band.shape[0] < th or band.shape[1] < tw:
        return None

    result = cv2.matchTemplate(band, template, cv2.TM_CCOEFF_NORMED)
    _, max_val, _, max_loc = cv2.minMaxLoc(result)
    if max_val < threshold:
        return None
    cx = x0 + max_loc[0] + tw // 2
    cy = y0 + max_loc[1] + th // 2
    return float(max_val), cx, cy


# ---------------------------------------------------------------------------
# 区域选择
# ---------------------------------------------------------------------------

def select_region(cfg: Config, spec: Optional[RoundSpec] = None) -> tuple:
    """
    执行区域框选。

    - capture_mode == "full"  : 主动拖拽整个屏幕作为选区。
                                不依赖微信的"默认全屏选区"——后者只有在微信
                                主界面处于特定状态时才存在，而工具运行时
                                微信未必在前台（Windows 前台锁定）。
                                主动拖拽不挑窗口状态，最稳。
    - region 有值              : 按坐标自动拖拽
    - 否则                    : 由用户手动拖拽，工具等待工具栏出现

    返回 (x1, y1, x2, y2)。
    """
    spec = spec or RoundSpec(index=1, capture_mode=cfg.capture_mode,
                              region=cfg.region, prefix=cfg.prefix)
    screen = ImageGrab.grab(all_screens=True)
    sw, sh = screen.size

    if spec.capture_mode == "full":
        # 微信浮层在有任务栏时，任务栏区域通常不在可截范围内；
        # 这里留出 2px 边距，拖到接近全屏即可。
        #
        # 重要：选区底边不能压到任务栏上。微信会把工具栏紧贴在选区下方，
        # 选区一旦触到屏幕底部，工具栏就会被任务栏遮住下半部分，
        # 导致模板匹配到的按钮中心落在任务栏内、点击被吃掉（实测 y=1036
        # vs 任务栏 top=1050）。这里主动让出任务栏高度。
        tb_top = get_taskbar_top()
        bottom = sh - 3 if tb_top < 0 else min(sh - 3, tb_top - 8)
        x1, y1 = 2, 2
        x2, y2 = sw - 3, bottom
        LOGGER.info("模式: 全屏截图（自动拖拽选区 %dx%d，底边 y=%d%s）",
                    x2 - x1, y2 - y1, y2,
                    "，已避开任务栏" if tb_top > 0 else "，无任务栏")
        pyautogui.moveTo(x1, y1)
        time.sleep(0.2)
        pyautogui.dragTo(x2, y2, duration=max(cfg.drag_duration, 0.8), button="left")
        return (x1, y1, x2, y2)

    if spec.region:
        x1, y1, x2, y2 = spec.region
        LOGGER.info("自动框选区域: (%d,%d) -> (%d,%d)", x1, y1, x2, y2)
        pyautogui.moveTo(x1, y1)
        time.sleep(0.15)
        pyautogui.dragTo(x2, y2, duration=cfg.drag_duration, button="left")
        return (x1, y1, x2, y2)

    LOGGER.info("请在屏幕上拖拽框选要截图的区域…")
    return (0, 0, sw, sh)


# ---------------------------------------------------------------------------
# 文件落盘处理
# ---------------------------------------------------------------------------

def _snapshot_files(roots: Sequence[str]) -> dict:
    """快照若干目录下的图片文件 mtime，用于识别新增文件。"""
    found = {}
    for root in roots:
        if not os.path.isdir(root):
            continue
        for ext in ("*.png", "*.jpg", "*.jpeg", "*.bmp", "*.webp"):
            for fp in glob.glob(os.path.join(root, "**", ext), recursive=True):
                try:
                    found[fp] = os.stat(fp).st_mtime
                except OSError:
                    pass
    return found


def _watch_roots() -> list:
    """需要监控的目录：微信默认图片目录 + 用户图片/桌面/下载 + 临时目录。"""
    home = os.path.expanduser("~")
    return [
        os.path.join(home, "Pictures"),
        os.path.join(home, "Desktop"),
        os.path.join(home, "Downloads"),
        os.path.join(home, "Documents"),
        os.path.join(os.environ.get("APPDATA", ""), "Tencent"),
        os.path.join(os.environ.get("LOCALAPPDATA", ""), "Tencent"),
        tempfile.gettempdir(),
    ]


def wait_for_saved_file(before: dict, timeout: float, poll: float = 0.25) -> Optional[str]:
    """等待出现新的图片文件，返回其路径。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        now = _snapshot_files(_watch_roots())
        new = [f for f, m in now.items() if before.get(f) != m]
        if new:
            return max(new, key=lambda f: os.path.getmtime(f))
        time.sleep(poll)
    return None


def unique_path(directory: str, stem: str, ext: str, overwrite: bool) -> str:
    """生成不冲突的目标路径。"""
    if overwrite:
        return os.path.join(directory, f"{stem}.{ext}")
    candidate = os.path.join(directory, f"{stem}.{ext}")
    idx = 1
    while os.path.exists(candidate):
        candidate = os.path.join(directory, f"{stem}_{idx}.{ext}")
        idx += 1
    return candidate


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------

def capture_once(cfg: Config, template: np.ndarray, spec: Optional[RoundSpec] = None) -> str:
    """
    执行一次完整的截图流程，返回最终文件路径。

    流程：检查微信 -> 触发快捷键 -> 等待浮层 -> 框选 -> 等待工具栏
          -> 模板匹配定位保存按钮 -> 点击 -> 等待保存对话框
          -> 写入文件名 -> 点击保存 -> 等待落盘 -> 归档重命名

    spec 指定本轮的截图模式（全屏/区域/手动框选）与文件名前缀。
    """
    spec = spec or RoundSpec(index=1, capture_mode=cfg.capture_mode,
                              region=cfg.region, prefix=cfg.prefix)

    # 0. 聊天窗口模式：走独立路径，不经过 Alt+A 流程
    if spec.capture_mode == "chat":
        procs = find_wechat_processes(cfg.wechat_processes)
        if not procs:
            raise ScreenshotError(
                "微信未启动。请先启动微信（进程名: "
                + " / ".join(cfg.wechat_processes) + "）后重试。"
            )
        LOGGER.info("检测到微信进程 %d 个: %s",
                    len(procs), ", ".join(sorted({p['name'] for p in procs})))
        return capture_chat_window(cfg, spec, dt.datetime.now())

    # 1. 检查微信是否运行
    procs = find_wechat_processes(cfg.wechat_processes)
    if not procs:
        raise ScreenshotError(
            "微信未启动。请先启动微信（进程名: "
            + " / ".join(cfg.wechat_processes) + "）后重试。"
        )
    LOGGER.info("检测到微信进程 %d 个: %s",
                len(procs), ", ".join(sorted({p['name'] for p in procs})))

    # 2. 清理可能残留的浮层
    close_overlay()
    time.sleep(0.3)

    # 3. 触发截图浮层
    #    两种方式：
    #      auto   —— 工具替你按快捷键（默认）
    #      manual —— 工具只等，由你自己按快捷键
    #    manual 的好处：不受 Windows 前台锁定影响，也不必把微信切到前台，
    #    你可以在任意窗口下按快捷键（微信截图是全局热键）。
    if cfg.trigger == "auto":
        # 3a. 把微信切到前台，让快捷键落在微信上（置前失败只降级不致命，
        #     因为微信 Alt+A 是全局热键，即使不在前台通常也能触发）
        if cfg.activate_wechat:
            if activate_wechat(cfg.wechat_window_title):
                LOGGER.info("已切换到微信主窗口")
            else:
                LOGGER.warning(
                    "未能将微信窗口切到前台（Windows 前台锁定），"
                    "将继续发送快捷键（微信 Alt+A 为全局热键，通常仍可触发）"
                )
        time.sleep(cfg.delay_before_hotkey)
        LOGGER.info("发送快捷键: %s", " + ".join(cfg.hotkey))
        pyautogui.hotkey(*cfg.hotkey)
    else:
        # 3b. 手动模式：只提示，不代按
        LOGGER.info("=" * 66)
        LOGGER.info("手动触发模式：请自行按 %s 启动微信截图",
                    " + ".join(cfg.hotkey))
        if spec.capture_mode == "region" and not spec.region:
            LOGGER.info("按下后会出现截图框，请拖拽框选区域")
        elif spec.capture_mode == "full":
            LOGGER.info("按下后会出现截图框，选区将由工具自动拖拽全屏")
        else:
            LOGGER.info("按下后会出现截图框，选区将由工具按配置坐标自动拖拽")
        LOGGER.info("等待中…（Ctrl+C 退出监听）")
        LOGGER.info("=" * 66)

    # 4. 等待浮层（timeout_overlay <= 0 表示无限等待）
    overlay = find_overlay_window(timeout=cfg.timeout_overlay, poll=cfg.poll_interval)
    if overlay is None:
        who = "你" if cfg.trigger == "manual" else "工具"
        raise ScreenshotError(
            f"等待 {cfg.timeout_overlay:.1f}s 后仍未检测到微信截图浮层。\n"
            f"可能原因：① {who}的快捷键未生效（微信未登录/不在前台）；"
            f"② 快捷键被其它软件占用；③ 微信版本不兼容。\n"
            f"可尝试手动按 {'+'.join(cfg.hotkey)} 验证是否弹出截图框。"
        )
    LOGGER.info("检测到截图浮层 hwnd=%s", overlay)
    time.sleep(cfg.delay_after_hotkey)

    # 5. 框选区域（full 模式主动拖拽全屏；region 模式按配置坐标；
    #    两者都没有时由用户手动拖拽）
    _require_overlay(overlay, "框选区域")
    region = select_region(cfg, spec)
    LOGGER.info("选区: %s", region)
    time.sleep(cfg.delay_after_select)

    # 6. 全屏轮询：等待「下载/保存」按钮出现
    #    不再先"找工具栏矩形"——浮层激活时整屏变亮、工具栏可能压在其它
    #    浅色面板上，几何定位极易误判。直接全屏模板匹配更稳。
    hit = None
    deadline = time.time() + cfg.timeout_toolbar
    while time.time() < deadline:
        img = ImageGrab.grab(all_screens=True)
        hit = find_save_button_anywhere(img, template, cfg.match_threshold)
        if hit:
            break
        time.sleep(cfg.poll_interval)

    if hit:
        score, cx, cy = hit
        LOGGER.info("识别到「保存/下载」按钮: 匹配度=%.4f 坐标=(%d,%d)", score, cx, cy)
    else:
        if cfg.timeout_manual <= 0:
            raise ScreenshotError(
                f"等待 {cfg.timeout_toolbar:.1f}s 后未识别到「下载」按钮"
                f"（匹配度阈值 {cfg.match_threshold}），且已禁用手动兜底。\n"
                f"可能原因：未完成框选、微信版本升级导致图标变化、屏幕缩放非 100%。\n"
                f"解决办法：① 重新生成模板 -> python wechat_screenshot.py --make-template；"
                f"② 允许手动兜底 -> 去掉 --timeout-manual 0"
            )
        # 兜底：交给用户手动点。后续流程（等待对话框/写文件名/点保存/归档）
        # 完全一致，因此手动只影响"点哪一下"，不影响"存到哪、叫什么名"。
        LOGGER.warning("=" * 66)
        LOGGER.warning("自动识别「下载」按钮失败（已等待 %.1fs）。", cfg.timeout_toolbar)
        LOGGER.warning("请手动点击截图工具栏上的「下载」按钮（⬇ 图标）。")
        LOGGER.warning("工具会继续等待保存对话框出现并自动完成后续步骤。")
        LOGGER.warning("=" * 66)
        manual_deadline = time.time() + cfg.timeout_manual
        while time.time() < manual_deadline:
            time.sleep(cfg.poll_interval)
            dialog = find_save_dialog(timeout=0.3, poll=cfg.poll_interval)
            if dialog:
                LOGGER.info("检测到保存对话框: hwnd=%s", dialog.hwnd)
                break
        else:
            raise ScreenshotError(
                f"等待 {cfg.timeout_manual:.1f}s 后仍未出现「保存图片」对话框。\n"
                f"请确认：① 已点击工具栏上的「下载」按钮；"
                f"② 微信截图浮层仍处于选区状态（未按 Esc）。"
            )

    if cfg.dry_run and hit:
        LOGGER.info("[dry-run] 已识别按钮但不执行点击")
        return ""

    # 7. 点击「下载/保存」-> 弹出 Windows 保存对话框
    if hit:
        # 点击前再确认一次：浮层若已被用户取消/超时消失，这一击会落在
        # 用户的真实页面上（曾发生"截图截着截着把页面关掉"）。
        _require_overlay(overlay, "点击「下载」按钮")
        cx, cy = avoid_taskbar(cx, cy, template)
        LOGGER.info("点击「保存/下载」按钮")
        pyautogui.click(cx, cy)
        time.sleep(cfg.delay_after_download)

        # 8. 等待保存对话框
        dialog = find_save_dialog(timeout=cfg.timeout_dialog, poll=cfg.poll_interval)
        if dialog is None:
            raise ScreenshotError(
                f"等待 {cfg.timeout_dialog:.1f}s 后未出现「保存图片」对话框。\n"
                f"可能原因：微信下载按钮未生效、微信版本交互不同、桌面弹出被拦截。"
            )
        LOGGER.info("检测到保存对话框: hwnd=%s", dialog.hwnd)

    # 9. 写入目标文件名
    when = dt.datetime.now()
    stem = cfg.build_filename(when)
    target_name = f"{stem}.{cfg.extension}"

    if dialog.filename_edit:
        ok = set_filename(dialog.filename_edit, target_name)
        if not ok:
            raise ScreenshotError("无法向对话框写入文件名")
        LOGGER.info("已写入文件名: %s", target_name)
    else:
        LOGGER.warning("未找到文件名输入框，将依赖对话框默认名称")

    # 10. 点击「保存」
    if dialog.save_button:
        LOGGER.info("点击对话框「%s」按钮", dialog.button_text or "保存")
        click_button(dialog.save_button)
    else:
        LOGGER.warning("未找到「保存」按钮，回退为回车键")
        pyautogui.press("enter")

    time.sleep(cfg.delay_after_save)

    # 11. 确认文件已落盘
    saved = wait_for_saved_file({}, timeout=cfg.timeout_save)
    if saved is None:
        raise ScreenshotError(
            f"点击保存后 {cfg.timeout_save:.1f}s 内未检测到新文件落盘。\n"
            f"可能原因：目标目录无写入权限、磁盘空间不足、微信保存失败。"
        )
    LOGGER.info("文件已落盘: %s", saved)

    # 14. 归档到目标目录并按规则重命名
    return archive(cfg, saved, stem)


def ensure_save_dir(cfg: Config) -> None:
    """
    确保保存目录存在。

    直接调 os.makedirs 在路径不可用时会抛原始 OSError（比如配置里写了
    本机不存在的盘符 D:\\），对第一次使用的用户极不友好。这里翻译成
    带排查建议的 ScreenshotError。
    """
    try:
        os.makedirs(cfg.save_dir, exist_ok=True)
    except OSError as exc:
        raise ScreenshotError(
            f"无法创建/访问保存目录: {cfg.save_dir}\n"
            f"系统错误: {exc}\n"
            f"请检查：① 该盘符是否存在；② 是否有写入权限；③ 路径是否合法。\n"
            f"可用 -d 参数或修改 config.json 的 save_dir 指定其它目录。"
        ) from exc


def apply_watermark_to_file(cfg: Config, path: str):
    """
    给已落盘的图片加上水印（时间 + 时间戳 + 防伪码 + 可选二维码）。

    顺序很重要：文件必须先定型（归档 + 转格式），再画水印；
    而指纹的计算又必须先于绘制。两者由 watermark 模块内部的
    「先算指纹、后画水印」保证，这里只负责串起来。
    """
    try:
        import watermark as wm
    except ImportError as exc:
        raise ScreenshotError(
            "水印功能不可用：缺少 watermark.py 或 pillow/opencv 依赖。"
        ) from exc

    if cfg.extension != "png":
        raise ScreenshotError(
            "水印核验要求无损格式，请把 file_format 设为 png（jpg 会改变像素）。"
        )

    opts = wm.WatermarkOptions(
        enabled=True,
        font_size=int(cfg.watermark_font_size),
        opacity=float(cfg.watermark_opacity),
        margin=int(cfg.watermark_margin),
        show_timestamp=bool(cfg.watermark_show_timestamp),
        qr=bool(cfg.watermark_qr),
        qr_size=int(cfg.watermark_qr_size),
        code_prefix=str(cfg.watermark_code_prefix),
    )
    secret = wm.load_secret(cfg.watermark_secret_file)

    with Image.open(path) as im:
        im.load()
        marked, res = wm.apply_watermark(im, secret, opts)

    marked.save(path, "PNG")
    return res


def archive(cfg: Config, source_path: str, stem: str) -> str:
    """把微信保存的文件移动/复制到目标目录并按命名规则重命名。"""
    ensure_save_dir(cfg)
    dest = unique_path(cfg.save_dir, stem, cfg.extension, cfg.overwrite)

    same = os.path.abspath(source_path) == os.path.abspath(dest)
    if not same:
        try:
            shutil.move(source_path, dest)
            LOGGER.debug("已移动文件到目标目录")
        except Exception as exc:
            LOGGER.warning("移动失败(%s)，改为复制", exc)
            shutil.copy2(source_path, dest)
            if not cfg.keep_wechat_temp:
                try:
                    os.remove(source_path)
                except OSError:
                    pass
    else:
        # 已在目标目录（如微信直接保存到了目标目录）
        try:
            shutil.copy2(source_path, dest)
        except Exception:
            pass

    # 转格式（若目标是 jpg 而源是 png）
    if cfg.extension == "jpg":
        try:
            with Image.open(dest) as im:
                im.convert("RGB").save(dest, "JPEG", quality=cfg.jpeg_quality)
            LOGGER.debug("已转换为 JPEG (质量 %d)", cfg.jpeg_quality)
        except Exception as exc:
            LOGGER.warning("转 JPEG 失败(保留原格式): %s", exc)

    # 加水印：放在最后一步，此时文件名与格式都已定型
    if cfg.watermark_enabled:
        try:
            wm_res = apply_watermark_to_file(cfg, dest)
            LOGGER.info(
                "🔏 水印已添加 | 防伪码 %s | %s | 指纹 %s",
                wm_res.code,
                "含二维码" if wm_res.has_qr else "仅文字",
                wm_res.fingerprint_hex,
            )
        except Exception as exc:
            LOGGER.warning("水印未添加（截图本身已保存）: %s", exc)

    LOGGER.info("✅ 截图已保存: %s", dest)
    return dest


# ---------------------------------------------------------------------------
# 模板生成
# ---------------------------------------------------------------------------

def make_template() -> str:
    """
    交互式生成「保存/下载」按钮模板。

    步骤：触发微信截图 -> 用户框选 -> 自动定位工具栏 -> 提示用户确认 ->
          截取下载按钮存为 assets/save_button.png
    """
    LOGGER.info("=" * 66)
    LOGGER.info("生成「保存/下载」按钮模板")
    LOGGER.info("请确保微信已启动并登录。")
    LOGGER.info("=" * 66)

    procs = find_wechat_processes(WECHAT_PROCESSES)
    if not procs:
        raise ScreenshotError("微信未启动，无法生成模板")

    close_overlay()
    time.sleep(0.3)
    activate_wechat("微信")
    time.sleep(0.6)

    LOGGER.info("正在自动触发微信截图（Alt+A）…")
    pyautogui.hotkey("alt", "a")
    overlay = find_overlay_window(timeout=10.0, poll=0.2)
    if overlay is None:
        raise ScreenshotError(
            "触发截图浮层失败，请确认微信已启动并登录，"
            "或手动按 Alt+A 后再运行本命令。"
        )
    LOGGER.info("检测到截图浮层 hwnd=%s", overlay)
    time.sleep(0.8)

    LOGGER.info("请在屏幕上框选任意区域（建议框选一块空白区域，识别更稳定）")
    time.sleep(0.5)

    deadline = time.time() + 120.0
    toolbar = None
    while time.time() < deadline:
        img = ImageGrab.grab(all_screens=True)
        toolbar = detect_toolbar(img)
        if toolbar:
            break
        time.sleep(0.3)
    if not toolbar:
        raise ScreenshotError("未检测到工具栏，已取消模板生成")

    LOGGER.info("工具栏: %s", toolbar)
    x0, y0, x1, y1 = toolbar

    # 分割出工具栏内各图标簇
    gray = np.array(ImageGrab.grab(all_screens=True).convert("L"))
    band = gray[y0 + 4:y1 - 3, x0:x1 + 1]
    dark = (band < 170).sum(axis=0)
    clusters = []
    s = None
    gap = 0
    for i, v in enumerate(dark):
        if v > 0:
            if s is None:
                s = i
            gap = 0
        else:
            if s is not None:
                gap += 1
                if gap >= 8:
                    clusters.append((s, i - gap))
                    s = None
    if s is not None:
        clusters.append((s, len(dark) - 1))

    valid = [(cs, ce) for cs, ce in clusters if ce - cs + 1 >= 6]
    LOGGER.info("工具栏内识别到 %d 个图标", len(valid))

    # 保存每个图标的截图供人工核对
    preview_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "assets", "icons")
    os.makedirs(preview_dir, exist_ok=True)
    img = ImageGrab.grab(all_screens=True)
    for i, (cs, ce) in enumerate(valid):
        crop = img.crop((x0 + cs - 4, y0 + 2, x0 + ce + 5, y1 - 1)).convert("L")
        out = os.path.join(preview_dir, f"icon_{i:02d}_x{x0+cs}.png")
        cv2.imwrite(out, np.array(crop))
    LOGGER.info("各图标已导出至: %s", preview_dir)

    # 优先尝试用旧模板自动定位（若旧模板仍可用，直接确认即可）
    old_tpl_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "assets", TEMPLATE_NAME)
    auto_chosen = None
    if os.path.isfile(old_tpl_path):
        old_tpl = cv2.imread(old_tpl_path, cv2.IMREAD_GRAYSCALE)
        if old_tpl is not None:
            hit = match_save_button(img, (x0, y0, x1, y1), old_tpl, threshold=0.6)
            if hit:
                score, cx, cy = hit
                LOGGER.info("用现有模板自动定位到「保存」按钮: 匹配度=%.4f 坐标=(%d,%d)", score, cx, cy)
                auto_chosen = (cx, cy)

    print("\n工具栏图标从左到右（微信 4.0 标准顺序）：")
    print("  0矩形 1圆形 2表情 3箭头 4画笔 5马赛克 6文字 7文字A 8OCR")
    print("  9取字 10撤销 11【下载/保存 ⬇】 12图钉 13转发 14取消X 15完成✓")
    print(f"\n各图标已导出到: {preview_dir}")

    if auto_chosen:
        cx, cy = auto_chosen
        print(f"已用现有模板自动识别到「保存」按钮 ({cx},{cy})，匹配度良好。")
        try:
            raw = input("直接回车确认，或输入其它图标序号覆盖（默认直接确认）: ").strip()
        except (EOFError, ValueError):
            raw = ""
        if raw:
            try:
                idx = int(raw)
            except ValueError:
                idx = None
            if idx is not None and 0 <= idx < len(valid):
                cs, ce = valid[idx]
                cx, cy = x0 + (cs + ce) // 2, (y0 + y1) // 2
            else:
                print("输入无效，沿用自动识别结果")
    else:
        print("未能用现有模板自动识别，请手动指定「下载/保存」按钮序号。")
        try:
            raw = input("请输入该图标的序号（默认 11，直接回车使用）: ").strip()
        except (EOFError, ValueError):
            raw = ""
        try:
            idx = int(raw) if raw else 11
        except ValueError:
            idx = 11
        if idx < 0 or idx >= len(valid):
            raise ScreenshotError(f"序号越界: {idx}（有效范围 0-{len(valid)-1}）")
        cs, ce = valid[idx]
        cx, cy = x0 + (cs + ce) // 2, (y0 + y1) // 2

    # 以点击点为中心裁剪模板（保持与运行时一致的尺寸）
    th, tw = 44, 20
    left = max(0, min(cx - tw // 2, x1 - tw))
    top = max(0, min(cy - th // 2, y1 - th))
    crop = img.crop((left, top, left + tw, top + th)).convert("L")
    arr = np.array(crop)
    out_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "assets", TEMPLATE_NAME)
    cv2.imwrite(out_path, arr)
    LOGGER.info("模板已保存: %s  尺寸=%s  中心=(%d,%d)", out_path, arr.shape, cx, cy)
    print(f"\n模板已保存到: {out_path}")
    return out_path


# ---------------------------------------------------------------------------
# 监听执行
# ---------------------------------------------------------------------------


def wait_for_trigger(cfg: Config) -> None:
    """
    监听提示：告诉用户按快捷键，然后返回，由调用方去等浮层。

    这里**刻意不去读控制台键盘**——msvcrt.kbhit()/getch() 会把用户按下的
    Alt+A 读进本进程的控制台缓冲区，微信就收不到这次按键了（实测踩过）。
    浮层检测只查窗口、不碰键盘，两者互不干扰。

    本函数只输出一条带"正在监听"标记的提示，并让日志换行，
    便于用户确认程序还活着。真正的等待发生在 find_overlay_window()
    （timeout<=0 时无限等待），Ctrl+C 由主线程的 sleep 间隙抛出。
    """
    hotkeys = " + ".join(cfg.hotkey)
    LOGGER.info("● 监听中：按 %s 截图（Ctrl+C 退出监听）", hotkeys)


def run_watch(cfg: Config, template: Optional[np.ndarray]) -> tuple:
    """
    常驻监听模式：一直等待用户触发，直到 Ctrl+C。

    每次触发走一轮完整流程（截图 -> 存盘 -> 归档）。
    单次失败不会终止监听——清理现场后继续等下一次触发。
    返回 (成功数, 失败数, 结果文件列表)。
    """
    spec = cfg.task_spec()

    LOGGER.info("=" * 66)
    LOGGER.info("监听模式已启动")
    LOGGER.info("  截图模式: %s", spec.mode_label)
    LOGGER.info("  保存目录: %s", cfg.save_dir)
    if spec.capture_mode == "chat":
        LOGGER.info("  说明    : 将自动抓取聊天窗口，无需按键")
    else:
        LOGGER.info("  触发方式: %s",
                    f"工具自动按 {'+'.join(cfg.hotkey)}" if cfg.trigger == "auto"
                    else f"你手动按 {'+'.join(cfg.hotkey)}")
    LOGGER.info("  退出方式: Ctrl+C")
    LOGGER.info("=" * 66)

    results = []
    ok = 0
    failed = 0
    n = 0

    while True:
        n += 1
        LOGGER.info("-" * 66)
        LOGGER.info("第 %d 次 | 模式=%s", n, spec.mode_label)

        try:
            if spec.capture_mode != "chat" and cfg.trigger == "manual":
                wait_for_trigger(cfg)
            path = capture_once(cfg, template, spec)
            ok += 1
            if path:
                results.append(path)
                print(path)
        except KeyboardInterrupt:
            raise
        except ScreenshotError as exc:
            failed += 1
            LOGGER.error("✗ 第 %d 次失败: %s", n, exc)
            if not cfg.keep_overlay_on_error:
                close_overlay()
            LOGGER.info("将继续等待下一次触发…")
        except Exception:
            failed += 1
            LOGGER.error("✗ 第 %d 次未预期异常:\n%s", n, traceback.format_exc())
            if not cfg.keep_overlay_on_error:
                close_overlay()
            LOGGER.info("将继续等待下一次触发…")

        sys.stdout.write("\r" + " " * 72 + "\r")
        sys.stdout.flush()

        if cfg.watch_interval > 0:
            LOGGER.info("已保存，等待 %.1fs 后继续监听…", cfg.watch_interval)
            time.sleep(cfg.watch_interval)

    return ok, failed, results      # 实际不会走到这里（Ctrl+C 抛出）


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="wechat_screenshot.py",
        description="微信截图自动化工具（监听模式：按快捷键截图，自动保存命名，Ctrl+C 退出）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
用法:
  启动后进入监听模式，按一次快捷键截一张，Ctrl+C 退出监听。

示例:
  python wechat_screenshot.py                             # 默认配置（你自己按 Alt+A）
  python wechat_screenshot.py --trigger manual            # 你自己按 Alt+A，工具只等
  python wechat_screenshot.py -c config.json --mode full  # 监听全屏截图
  python wechat_screenshot.py -c config.json --mode chat  # 监听聊天独立窗口截图
  python wechat_screenshot.py -c config.json -r 0 0 1920 1080  # 监听固定区域
  python wechat_screenshot.py --make-template             # 重建保存按钮模板
  python wechat_screenshot.py --list-windows              # 列出可截图的聊天窗口
  python wechat_screenshot.py -c config.json --dry-run    # 只识别不点击（调试）
        """,
    )
    p.add_argument("-c", "--config", help="配置文件路径 (JSON)")
    p.add_argument("-d", "--save-dir", help="覆盖保存目录")
    p.add_argument("-r", "--region", nargs=4, type=int, metavar=("X1", "Y1", "X2", "Y2"),
                   help="自动框选区域坐标")
    p.add_argument("--prefix", help="覆盖文件名前缀")
    p.add_argument("--format", dest="file_format", choices=["png", "jpg"], help="图片格式")
    p.add_argument("--mode", choices=["region", "full", "chat"],
                   help="覆盖截图模式：region=框选 full=全屏 chat=聊天独立窗口")
    p.add_argument("--trigger", choices=["auto", "manual"],
                   help="触发方式：manual=等你自己按快捷键（默认） auto=工具代按快捷键")
    p.add_argument("--session", help="chat 模式：按会话名关键字过滤聊天窗口")
    p.add_argument("--chat-hwnd", type=int, help="chat 模式：直接指定聊天窗口句柄 hwnd")
    p.add_argument("--watch-interval", type=float,
                   help="每次保存后到下次监听的间隔秒数（默认 0=立即可再次触发）")
    p.add_argument("--timeout-manual", type=float,
                   help="自动识别失败后等待用户手动点「下载」的上限秒数（默认 60，0=不等待直接报错）")
    p.add_argument("--list-windows", action="store_true",
                   help="列出当前可截图的微信聊天窗口后退出")
    p.add_argument("--make-template", action="store_true", help="交互式生成保存按钮模板后退出")
    p.add_argument("--dry-run", action="store_true", help="只识别不点击")
    p.add_argument("--no-watermark", action="store_true", help="本次运行不添加水印与防伪码")
    p.add_argument("--watermark-secret", metavar="FILE",
                   help="水印密钥文件路径（默认 ~/.wechat_screenshot/watermark.key）")
    p.add_argument("-v", "--verbose", action="store_true", help="输出调试日志")
    return p


def apply_overrides(cfg: Config, args: argparse.Namespace) -> Config:
    """命令行参数覆盖配置文件。"""
    if args.save_dir:
        cfg.save_dir = os.path.abspath(os.path.expanduser(args.save_dir))
    if args.region:
        cfg.region = tuple(args.region)
    if args.prefix:
        cfg.prefix = args.prefix
    if args.file_format:
        cfg.file_format = args.file_format
    if args.mode:
        cfg.capture_mode = args.mode
    if args.trigger:
        cfg.trigger = args.trigger
    if args.session:
        cfg.session_keyword = args.session
    if args.chat_hwnd:
        cfg.chat_hwnd = args.chat_hwnd
    if args.watch_interval is not None:
        cfg.watch_interval = args.watch_interval
    if args.timeout_manual is not None:
        cfg.timeout_manual = args.timeout_manual
    if args.dry_run:
        cfg.dry_run = True
    if args.no_watermark:
        cfg.watermark_enabled = False
    if args.watermark_secret:
        cfg.watermark_secret_file = args.watermark_secret
    if args.verbose:
        cfg.log_level = "DEBUG"

    # 命令行可能把格式改回 jpg，这里再兜一次，保证「开水印必无损」
    if cfg.watermark_enabled and cfg.file_format.lower() in ("jpg", "jpeg"):
        cfg.file_format = "png"
        cfg.format_forced_by_watermark = True
    return cfg


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)

    try:
        cfg = load_config(args.config)
    except Exception as exc:
        print(f"[致命错误] 配置加载失败: {exc}")
        return 2

    cfg = apply_overrides(cfg, args)
    setup_logging(cfg)

    # 单实例锁：多开会互相抢微信浮层与保存对话框
    lock_handle, acquired = acquire_single_instance_lock()
    if not acquired:
        LOGGER.error("=" * 66)
        LOGGER.error("已有一个截图工具实例在运行，请先在那个窗口按 Ctrl+C 结束。")
        LOGGER.error("多开会导致两个实例互相抢微信浮层和保存对话框，故障难以排查。")
        LOGGER.error("=" * 66)
        return 3

    _ensure_dpi_awareness()
    try:
        pyautogui.FAILSAFE = False
    except Exception:
        pass

    # 水印核验依赖无损像素；配了 jpg 的话 Config 层已纠正，这里只做告知
    if getattr(cfg, "format_forced_by_watermark", False):
        LOGGER.warning("水印核验需要无损格式，输出格式已自动改为 png（原配置为 jpg）")

    # 提前初始化密钥：第一张图才报错的话，监听模式下会很难发现
    secret_hint = ""
    if cfg.watermark_enabled:
        try:
            import watermark as _wm
            key = _wm.load_secret(cfg.watermark_secret_file)
            secret_hint = os.path.abspath(cfg.watermark_secret_file or _wm.DEFAULT_SECRET_FILE)
            LOGGER.debug("水印密钥: %s (%d 字节)", secret_hint, len(key))
        except Exception as exc:
            LOGGER.error("水印初始化失败: %s", exc)
            LOGGER.error("可先运行 python wechat_screenshot.py --no-watermark 跳过水印")
            return 2

    LOGGER.info("=" * 66)
    LOGGER.info("微信截图自动化工具启动")
    LOGGER.info("保存目录: %s", cfg.save_dir)
    LOGGER.info("命名规则: %s -> 例: %s.%s",
                cfg.filename_format, cfg.build_filename(dt.datetime.now()), cfg.extension)
    LOGGER.info("快捷键  : %s", " + ".join(cfg.hotkey))
    LOGGER.info("触发方式: %s", "工具自动按" if cfg.trigger == "auto" else "你手动按")
    LOGGER.info("截图模式: %s", cfg.capture_mode)
    if cfg.watermark_enabled:
        LOGGER.info("水印    : 开启（左下角：时间 + 时间戳 + 防伪码%s）",
                    " + 二维码" if cfg.watermark_qr else "")
        LOGGER.info("密钥文件: %s", secret_hint)
        LOGGER.info("核验命令: python verify_watermark.py <图片路径>")
    else:
        LOGGER.info("水印    : 关闭")
    LOGGER.info("=" * 66)

    # 提前校验保存目录，避免进入监听后每轮都因目录不可用而失败
    try:
        ensure_save_dir(cfg)
    except ScreenshotError as exc:
        LOGGER.error("%s", exc)
        return 2

    # 列出可截图的聊天窗口
    if args.list_windows:
        wins = find_chat_windows()
        if not wins:
            print("当前没有可截图的微信聊天窗口。")
            print("在微信中打开一个会话（或打开聊天记录）后重试。")
            return 1
        print(f"找到 {len(wins)} 个微信窗口：\n")
        for i, c in enumerate(wins):
            print(f"  [{i}] {c.kind_label}")
            print(f"      标题  : {c.title}")
            print(f"      会话名: {c.session_name}")
            print(f"      尺寸  : {c.size[0]}x{c.size[1]}   hwnd={c.hwnd}")
        print("\n用法示例：")
        print("  --mode chat                        抓取（多窗口时取第一个）")
        print("  --mode chat --session <关键字>     按会话名过滤")
        print("  --mode chat --chat-hwnd <hwnd>     精确指定窗口")
        return 0

    if args.make_template:
        try:
            make_template()
            return 0
        except ScreenshotError as exc:
            LOGGER.error("模板生成失败: %s", exc)
            return 1
        except Exception:
            LOGGER.error("模板生成异常:\n%s", traceback.format_exc())
            return 1

    # 只有走 Alt+A 流程的模式才需要模板（chat 模式直接抓窗口，不需要）
    template = None
    if cfg.capture_mode != "chat":
        try:
            template = load_template(cfg)
        except ScreenshotError as exc:
            LOGGER.error("%s", exc)
            return 2

    try:
        run_watch(cfg, template)
    except KeyboardInterrupt:
        sys.stdout.write("\r" + " " * 72 + "\r")
        sys.stdout.flush()
        LOGGER.info("=" * 66)
        LOGGER.info("已退出监听（Ctrl+C）")
        LOGGER.info("=" * 66)
        return 130

    return 0


if __name__ == "__main__":
    sys.exit(main())
