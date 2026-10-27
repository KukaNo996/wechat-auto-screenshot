# WeChat Auto Screenshot

**微信截图自动化工具** —— 你按 `Alt+A`，剩下的全自动：识别「下载」按钮 → 点它 → 填写日期时间文件名 → 点保存 → 归档重命名。截完继续监听，`Ctrl+C` 退出。

支持 **区域框选** / **全屏** / **微信聊天独立窗口** 三种模式。

[![Python](https://img.shields.io/badge/Python-3.9%2B-blue?logo=python&logoColor=white)](https://www.python.org/)
[![Platform](https://img.shields.io/badge/Platform-Windows-0078D4?logo=windows&logoColor=white)](#环境要求)
[![License](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)
[![WeChat](https://img.shields.io/badge/WeChat-4.0-07C160?logo=wechat&logoColor=white)](#环境要求)

> 已在 **微信 4.0（`Weixin.exe`）+ Windows 10/11 @ 1920×1080** 上完整实测通过。

---

## English Overview

A Windows automation tool that completes the entire WeChat screenshot workflow for you.

Press `Alt+A` as usual — the tool then detects the floating screenshot toolbar via OpenCV template matching (score `1.0000`, interference ≤ `0.60`), clicks the **Download** button, fills the Windows save dialog with a timestamped filename, clicks **Save**, and finally moves/renames the file into your target folder. It keeps listening for the next screenshot until `Ctrl+C`.

**Highlights**

- **Three capture modes**: region selection, full screen, or a standalone WeChat chat window
- **Template matching over icon counting** — survives WeChat UI revision, no hard-coded icon index
- **Manual fallback** — if detection fails, just click the button yourself; the tool takes over from there
- **Safety-first input injection** — every keystroke and click is gated on the overlay actually existing, so it can never hit your own windows
- **Single-instance lock** — prevents two instances from fighting over the same save dialog
- **Fully configurable** — save dir, filename pattern, hotkey, timeouts, thresholds, all via JSON

> Licensed under MIT. Windows only (depends on `pywin32` and Win32 window APIs).

---

## 目录

- [特性](#特性)
- [环境要求](#环境要求)
- [安装](#安装)
- [快速开始](#快速开始)
- [三种截图模式](#三种截图模式)
- [监听模式](#监听模式)
- [配置说明](#配置说明)
- [命令行参数](#命令行参数)
- [工作原理](#工作原理)
- [常见问题](#常见问题)
- [安全说明与免责声明](#安全说明与免责声明)
- [贡献](#贡献)

---

## 特性

| 特性 | 说明 |
|---|---|
| **全自动归档** | 从点「下载」到文件落到目标目录并重命名，全程无需干预 |
| **日期时间命名** | `wechat_20261027_143025.png`，天然不重名；重名时自动加 `_1` 后缀，**绝不覆盖** |
| **三种截图模式** | 区域框选 / 全屏 / 聊天独立窗口 |
| **常驻监听** | 按一次截一张，连续多张不用重启；`Ctrl+C` 干净退出 |
| **模板匹配识别** | 不依赖图标序号，微信改版后重新生成模板即可 |
| **手动兜底** | 识别失败不中断，你点一下 ⬇，后续仍自动完成 |
| **不误伤窗口** | 所有输入注入都以「浮层确实存在」为前提 |
| **单实例保护** | 拒绝多开，避免实例间抢对话框 |
| **详细日志** | 每步都有输出，失败给出具体原因和排查建议 |
| **纯本地运行** | 不联网、不上传、无遥测 |

---

## 环境要求

| 项目 | 要求 |
|---|---|
| 操作系统 | **Windows 10 / 11**（依赖 `pywin32` 与 Win32 窗口 API，不支持 macOS/Linux） |
| 微信 | 微信 4.0（`Weixin.exe`）或 3.x（`WeChat.exe`），需已登录 |
| Python | 3.9+（开发环境 3.13） |
| 屏幕缩放 | **建议 100%**。非 100% 时需重新生成按钮模板（见 [Q1](#q1自动识别下载按钮失败怎么办)） |
| 权限 | 普通用户即可，无需管理员权限 |

---

## 安装

```bash
git clone https://github.com/ZhuShang/wechat-auto-screenshot.git
cd wechat-auto-screenshot

# 建议使用虚拟环境
python -m venv .venv
.venv\Scripts\activate

pip install -r requirements.txt
```

依赖清单：`pyautogui`、`pywin32`、`pillow`、`opencv-python`、`numpy`、`psutil`、`pyperclip`

准备配置：

```bash
# Windows
copy config.example.json config.json

# 然后编辑 config.json，至少改掉 save_dir
```

> `config.example.json` 可直接使用，开头的 `_` 前缀键仅作注释，程序会忽略。

---

## 快速开始

```bash
# 1. 启动监听（默认手动触发）
python wechat_screenshot.py -c config.json

# 2. 终端显示「● 监听中」后，按 Alt+A 启动微信截图并框选
# 3. 剩下的自动完成，终端会打印完整路径
# 4. 继续按 Alt+A 截下一张；Ctrl+C 结束
```

典型输出：

```
==================================================================
监听模式已启动
  截图模式: 全屏
  保存目录: C:\Users\you\Pictures\WeChatShot
  触发方式: 你手动按 alt+a
  退出方式: Ctrl+C
==================================================================
------------------------------------------------------------------
第 1 次 | 模式=全屏
● 监听中：按 alt + a 截图（Ctrl+C 退出监听）
------------------------------------------------------------------
检测到截图浮层 hwnd=789618
模式: 全屏截图（自动拖拽选区 1915x1040，底边 y=1042，已避开任务栏）
识别到「保存/下载」按钮: 匹配度=1.0000 坐标=(1699,1001)
点击「保存/下载」按钮
检测到保存对话框: hwnd=3476194
已写入文件名: OK_20261027_143025.png
点击对话框「保存(&S)」按钮
✅ 截图已保存: C:\Users\you\Pictures\WeChatShot\OK_20261027_143025.png
------------------------------------------------------------------
第 2 次 | 模式=全屏
● 监听中：按 alt + a 截图（Ctrl+C 退出监听）
```

### 常用命令

| 需求 | 命令 |
|---|---|
| 手动框选 + 存到 D 盘 | `python wechat_screenshot.py -c config.json -d D:\Shots` |
| 固定区域（不用手动拖） | `python wechat_screenshot.py -c config.json -r 100 100 800 600` |
| 全屏 | `python wechat_screenshot.py -c config.json --mode full` |
| 聊天独立窗口 | `python wechat_screenshot.py -c config.json --mode chat` |
| 工具代按快捷键 | `python wechat_screenshot.py -c config.json --trigger auto` |
| 每张之间停 5 秒 | `python wechat_screenshot.py -c config.json --watch-interval 5` |
| 只看识别不点击 | `python wechat_screenshot.py -c config.json --dry-run -v` |
| 列出可截的聊天窗口 | `python wechat_screenshot.py --list-windows` |
| 重建按钮模板 | `python wechat_screenshot.py --make-template` |

---

## 三种截图模式

### 1. 区域截图 `region`（默认）

按下 `Alt+A` 后自己拖拽框选，工具等工具栏出现后接管。

若不想手动拖，可给坐标让它自动框选：

```bash
python wechat_screenshot.py -c config.json -r 400 200 1000 560
```

### 2. 全屏截图 `full`

```bash
python wechat_screenshot.py -c config.json --mode full
```

工具会**主动拖拽**整个屏幕作为选区 —— 不依赖微信的"默认全屏选区"。
后者只有在微信主界面处于特定状态时才存在，而工具运行时微信未必在前台。

选区底边会自动避开任务栏，否则工具栏被任务栏遮住会导致点不到下载按钮。

### 3. 聊天独立窗口 `chat`

```bash
# 先看有哪些窗口可截
python wechat_screenshot.py --list-windows

# 自动选（优先聊天记录窗口）
python wechat_screenshot.py -c config.json --mode chat

# 按会话名过滤
python wechat_screenshot.py -c config.json --mode chat --session 产品群

# 精确指定窗口句柄
python wechat_screenshot.py -c config.json --mode chat --chat-hwnd 1234567
```

聊天窗口模式**不需要按快捷键**，直接抓取窗口矩形区域的屏幕像素。

> 微信是 Qt 渲染，`PrintWindow` 只能拿到空白图，因此采用「窗口置前 + 按矩形抓屏幕」。
> 主界面与独立聊天窗口靠**左侧会话列表**区分（主界面有一列头像，独立窗口没有），
> 因为两者标题可能完全相同。

---

## 监听模式

工具启动后常驻监听，直到 `Ctrl+C`。

| `trigger` | 行为 | 适用 |
|---|---|---|
| `manual`（默认） | 工具只等，快捷键由你按 | 交互使用。可在任意窗口下按，不受 Windows 前台锁定影响 |
| `auto` | 工具自己切微信前台并代按快捷键 | 无人值守 |

- 每轮结束打印完整路径，文件名带时间戳
- 某轮失败（没按快捷键、浮层未出现等）**不中断监听**，继续等下一次
- 等待期间你的鼠标键盘完全自由 —— 工具不读取你的按键，因此不会抢走 `Alt+A`
- `timeout_overlay` 设为 `0` 表示**无限等待**

### 单实例保护

工具使用命名互斥体保证同时只能跑一个实例。多开时两个实例会互相抢微信浮层和
保存对话框（表现为「A 报没有对话框，但文件其实已被 B 保存」），极难排查。
检测到已有实例时会直接拒绝启动，退出码 `3`。

若怀疑有残留进程：

```powershell
Get-CimInstance Win32_Process -Filter "Name like '%python%'" |
  Where-Object { $_.CommandLine -like '*wechat_screenshot*' } |
  Select-Object ProcessId, CommandLine
```

---

## 配置说明

### 输出设置

| 字段 | 默认值 | 说明 |
|---|---|---|
| `save_dir` | `~/Pictures/WeChatShot` | 目标目录 |
| `filename_format` | `{prefix}_{date}_{time}` | 占位符：`{prefix}` `{date}` `{time}` `{datetime}` `{millis}` `{timestamp}` `{user}` |
| `prefix` | `wechat` | 文件名前缀，可用 `--prefix` 覆盖 |
| `file_format` | `png` | `png` / `jpg` |
| `jpeg_quality` | `95` | JPG 质量 |
| `overwrite` | `false` | `false` 时重名自动加 `_1` 后缀，**绝不覆盖** |
| `keep_wechat_temp` | `true` | 是否保留微信自己写入的临时文件 |

### 交互设置

| 字段 | 默认值 | 说明 |
|---|---|---|
| `hotkey` | `["alt", "a"]` | 微信截图快捷键 |
| `trigger` | `"manual"` | `manual` = 你按；`auto` = 工具代按 |
| `capture_mode` | `"region"` | `region` / `full` / `chat` |
| `region` | `null` | `[x1, y1, x2, y2]`，`null` = 手动拖拽 |
| `drag_duration` | `0.5` | 拖拽耗时（秒） |
| `session_keyword` | `null` | `chat` 模式：按会话名过滤 |
| `chat_hwnd` | `null` | `chat` 模式：直接指定窗口句柄 |
| `watch_interval` | `0.0` | 每次保存后的静默秒数 |
| `keep_overlay_on_error` | `false` | 失败时是否保留浮层 |

### 延时与超时（秒）

| 字段 | 默认值 | 说明 |
|---|---|---|
| `delay_before_hotkey` | `0.6` | 按快捷键前的等待 |
| `delay_after_hotkey` | `1.2` | 等待浮层弹出 |
| `delay_after_select` | `0.9` | 等待工具栏出现 |
| `delay_after_download` | `1.5` | 等待保存对话框弹出 |
| `delay_after_save` | `1.0` | 点保存后等待落盘 |
| `poll_interval` | `0.15` | 轮询间隔 |
| `timeout_overlay` | `0.0` | 等待浮层，`0` = 无限等待 |
| `timeout_toolbar` | `8.0` | 等待「下载」按钮出现 |
| `timeout_dialog` | `10.0` | 等待保存对话框 |
| `timeout_save` | `15.0` | 等待文件落盘 |
| `timeout_manual` | `60.0` | 识别失败后等待手动点下载的上限，`0` = 禁用兜底 |

> 低配机器或高负载下若频繁超时，适当调大 `delay_*` 与 `timeout_*`。

### 识别参数

| 字段 | 默认值 | 说明 |
|---|---|---|
| `match_threshold` | `0.75` | 模板匹配阈值。实测目标 `1.0000`、干扰项 ≤ `0.60`，`0.75` 余量充足 |
| `template_path` | `null` | 自定义模板路径，`null` 用内置 `assets/save_button.png` |

### 进程与窗口

| 字段 | 默认值 | 说明 |
|---|---|---|
| `wechat_processes` | `["Weixin.exe", "WeChat.exe"]` | 微信进程名 |
| `activate_wechat` | `true` | `auto` 模式下是否把微信切到前台 |
| `wechat_window_title` | `"微信"` | 微信主窗口标题 |

### 日志

| 字段 | 默认值 | 说明 |
|---|---|---|
| `log_level` | `INFO` | `DEBUG` / `INFO` / `WARNING` / `ERROR` |
| `log_file` | `null` | 日志文件路径，`null` = 不写文件 |
| `log_to_console` | `true` | 是否输出到终端 |

---

## 命令行参数

```
python wechat_screenshot.py [选项]

  -c, --config PATH      配置文件路径 (JSON)
  -d, --save-dir DIR     覆盖保存目录
  -r, --region X1 Y1 X2 Y2   自动框选区域坐标
  --prefix PREFIX        覆盖文件名前缀
  --format {png,jpg}     图片格式
  --mode {region,full,chat}  覆盖截图模式
  --trigger {auto,manual}    触发方式：manual=你自己按（默认） auto=工具代按
  --session KEYWORD      chat 模式：按会话名关键字过滤聊天窗口
  --chat-hwnd HWND       chat 模式：直接指定窗口句柄 hwnd
  --watch-interval SEC   每次保存后到下次监听的间隔（默认 0=立即可再次触发）
  --timeout-manual SEC   自动识别失败后等待手动点「下载」的上限（默认 60，0=禁用兜底）
  --list-windows         列出当前可截图的微信聊天窗口后退出
  --make-template        交互式生成保存按钮模板后退出
  --dry-run              只识别不点击（调试用）
  -v, --verbose          输出调试日志
```

**退出码**：`0` 成功 / `1` 业务失败 / `2` 配置或模板错误 / `3` 已有实例在运行

---

## 工作原理

### 整体流程

```
① 检测微信进程；确认无残留浮层（有则定向关闭，不发全局 ESC）
      ↓
② 提示你按 Alt+A（或由工具代按）→ 等待浮层出现（校验类名+标题+全屏尺寸）
      ↓
③ 框选区域：全屏自动拖到底（避开任务栏）/ region 按坐标 / 你手动拖
      ↓
④ 全屏 OpenCV 模板匹配「下载」按钮
      ↓
⑤ 点击「下载」→ 弹出 Windows「保存图片」对话框
      ↓
⑥ 向文件名框写入日期时间文件名 → 点击「保存」
      ↓
⑦ 等待文件落盘 → 移动到目标目录 → 按规则重命名归档
      ↓
⑧ 打印完整路径 → 回到监听
```

### 识别微信截图浮层

微信 4.0 的截图浮层是一个全屏独立窗口，特征稳定：

| 特征 | 值 |
|---|---|
| 窗口类名 | `Qt51514QWindowIcon` |
| 窗口标题 | `Weixin` |
| 窗口尺寸 | 全屏 |

工具同时校验**类名 + 标题 + 尺寸**，避免与微信主窗口混淆。

### 识别「下载」按钮（核心）

采用 **OpenCV 模板匹配**（`TM_CCOEFF_NORMED`）在**整幅屏幕**中搜索，
而非按序号数图标位置 —— 微信改版或不同版本图标数量会变，数序号不可靠。

- 模板保留**原始比例**（44×20，不做归一化缩放），否则形状信息丢失、匹配度会从 `1.0` 掉到 `0.49`
- **全屏直接搜索**，不先裁剪工具栏区域
- 实测判别力（1920×1080 深色主题）：

  | 画面状态 | 最高得分 | 次高干扰 |
  |---|---|---|
  | 工具栏已出现 | **1.0000** | 0.5986 |
  | 仅浮层、无工具栏 | 0.3975 | 0.3393 |
  | 完全无浮层 | 0.3393 | 0.3345 |

  分离度极大，阈值 `0.75` 有充足余量。

#### 为什么不先找工具栏矩形

早期实现用「灰度阈值 `>195` + 水平闭运算（核宽 31px）+ 形状约束（宽高比 ≥5、高度 18~120px）」
先定位工具栏矩形，再在其中匹配。在真实屏幕上这个几何假设**几乎必然失败**：

| 失败原因 | 说明 |
|---|---|
| 浮层激活时整屏变亮 | 工具栏底色 `(249,249,249)` 与遮罩几乎同色，"浅色长条"判据失效 |
| 工具栏压在其它浅色面板上 | 实测曾命中浏览器顶部标签栏 `(322,106)-(765,123)`，位置完全错误 |
| 任务栏/状态栏混入 | 底部行聚类把工具栏与任务栏白色区并成一组（高 74px），段被拆散 |
| 行聚类整组过滤 | 组高超过 `MAX_HEIGHT` 时整组被丢弃，工具栏随之丢失 |

实测对照（同一批真实截图）：

| 样本 | 期望 | 几何法 | 全屏模板法 |
|---|---|---|---|
| 工具栏已出现 | 命中 | ❌ 定位到错误区域 | ✅ 1.0000 |
| 工具栏已出现（另一张） | 命中 | ❌ 候选为空 | ✅ 1.0000 |
| 仅浮层无工具栏 | 未命中 | ❌ 误报 | ✅ 正确未命中 |
| 无浮层 | 未命中 | ❌ 误报 | ✅ 正确未命中 |

**「下载」按钮是整个浮层上唯一稳定可识别的特征**，直接匹配它即可，无需先定位工具栏。

### 操作保存对话框

Windows 标准保存对话框（`#32770`）控件结构固定：

| 控件 | 定位方式 |
|---|---|
| 文件名输入框 | `Edit` 类中面积最大的那个（排除搜索框） |
| 保存按钮 | `Button` 类，文本含「保存」/「Save」，排除「取消」 |

写文件名用 `WM_SETTEXT` 消息（不依赖焦点），点击用 `BM_CLICK` 消息（不受前台限制）。

### 手动兜底：识别失败也不中断

万一微信改版导致图标变化、自动识别失败，工具不会直接退出，而是提示：

```
==================================================================
自动识别「下载」按钮失败（已等待 8.0s）。
请手动点击截图工具栏上的「下载」按钮（⬇ 图标）。
工具会继续等待保存对话框出现并自动完成后续步骤。
==================================================================
```

你点一下 ⬇，工具检测到对话框后**自动接管**：写文件名 → 点保存 → 归档重命名。
手动只影响「点哪一下」，不影响「存到哪、叫什么名」。

### 输入注入的安全约束

自动化工具最容易出的事故是**操作落到用户自己的窗口上**。本工具有三条硬约束：

1. **只在确认浮层存在时才发按键**。清理残留浮层用 `PostMessage` 定向投递给浮层
   窗口，不抢你正在操作的窗口的键盘焦点。
   > 早期版本无条件 `pyautogui.press("esc")`，监听模式下会把 ESC 打到你的浏览器或
   > 编辑器上，导致页面被误关。这是真实踩过的坑。
2. **鼠标操作前校验浮层仍在**。拖拽选区、点击「下载」之前都重新确认浮层存活；
   已消失则中止本轮，不做任何点击。
3. **点击点避让任务栏**。全屏模式把选区底边抬到任务栏上方，避免工具栏被遮住
   导致「点了没反应」。

### 文件归档

微信保存到 `~/Pictures` 等位置且文件名不可控，工具通过**目录快照 diff** 找到新增文件，
再移动到目标目录并按命名规则重命名。

---

## 常见问题

### Q1：自动识别「下载」按钮失败怎么办？

**不会导致任务失败。** 按提示手动点一下 ⬇ 即可，工具会自动完成后续步骤。

若想彻底解决（微信升级改了图标、或系统缩放非 100%），重新生成模板：

```bash
python wechat_screenshot.py --make-template
```

流程：自动触发 Alt+A → 你框选任意区域 → 工具导出各图标 → 用旧模板自动匹配 →
生成新模板。

> 建议框选**内容较少的区域**（如空白页），避免模板混入复杂背景。

无人值守场景不希望等人手动点，可禁用兜底：

```bash
python wechat_screenshot.py --timeout-manual 0
```

### Q2：提示「等待 N 秒后仍未检测到微信截图浮层」

- 确认微信已启动并登录
- `manual` 模式下确认你真的按了快捷键
- 检查快捷键是否被其它软件占用
- 监听模式下把 `timeout_overlay` 设为 `0`，工具会一直等

### Q3：提示「检测到保存对话框」但文件没保存？

检查 `save_dir` 是否有写权限、磁盘是否已满。日志会打印实际落盘路径。

### Q4：文件名总是多出 `_1` 后缀？

说明目标文件名已存在。`overwrite: false` 时工具**绝不覆盖**，会追加序号。
若允许覆盖，把 `overwrite` 设为 `true`。

### Q5：截图范围不对 / 位置偏移？

- 检查屏幕缩放是否为 100%
- 多显示器下坐标基于整个虚拟桌面，可能需要调整 `region`
- 缩放非 100% 时重新生成模板

### Q6：连续截很多张会互相干扰吗？

不会。每轮开始前检查微信进程与浮层状态；失败只影响当轮，监听继续。
清理残留浮层时不会盲发 ESC，鼠标操作前也会确认浮层存活（见
[输入注入的安全约束](#输入注入的安全约束)）。

### Q7：截图截着截着，我的页面被关掉了 / 键盘失灵？

这是旧版本的缺陷，已修复 —— 根因是无条件注入 ESC。当前版本只在确认浮层存在时
才发按键。若仍遇到，`Ctrl+C` 结束工具后重试，并检查是否多开。

### Q8：多开会有什么影响？

两个实例会互相抢微信浮层和保存对话框，故障极难定位。工具已内置单实例锁，
检测到已有实例会拒绝启动（退出码 `3`）。

### Q9：`--mode chat` 提示「未找到聊天独立窗口」

说明微信当前只显示了主界面。请在微信中**双击某个会话**打开独立聊天窗口后重试。
可先用 `--list-windows` 确认有哪些窗口可截。

### Q10：为什么全屏模式不用微信自带的「默认全屏选区」？

因为那个选区只在微信主界面处于特定状态时才存在，而工具运行时微信未必在前台
（Windows 前台锁定会阻止抢焦点）。改成**主动拖拽**后不挑窗口状态，稳定性大幅提升。

### Q11：能否在其它分辨率 / 多显示器下使用？

可以，但按钮模板与缩放相关。非 100% 缩放或不同分辨率下建议重新生成模板。
多显示器场景下选区坐标基于虚拟桌面。

### Q12：支持 macOS / Linux 吗？

不支持。核心依赖 Win32 窗口 API（`EnumWindows`、`GetWindowRect`、`PostMessage` 等）
与 Windows 标准保存对话框结构。

---

## 安全说明与免责声明

- **本工具纯本地运行**，不联网、不上传任何数据、无遥测。
- 工具通过模拟键盘鼠标与窗口消息操作微信，**不修改微信程序、不读取聊天内容**。
- 请遵守微信软件许可协议及所在地法律法规，仅用于个人效率提升。
- 由于依赖界面特征识别，微信大幅改版后可能需要重新生成模板。
- 按 MIT 协议「按现状」提供，使用风险自负。

---

## 贡献

欢迎 Issue 与 PR。

1. Fork 本仓库
2. 创建分支：`git checkout -b feat/your-feature`
3. 提交：`git commit -m "feat: add something"`
4. 推送：`git push origin feat/your-feature`
5. 打开 Pull Request

提交前请确认：

- 改动后 `python -m py_compile wechat_screenshot.py` 通过
- 在真实微信环境跑过一次端到端（区域 / 全屏 / 聊天窗口至少一种）
- 新增配置项同步更新 `config.example.json` 与本文档

**特别欢迎**：不同微信版本的窗口特征、其它分辨率下的模板、非 100% DPI 的适配经验。

---

## License

[MIT](LICENSE) © 2026 ZhuShang
