# -*- coding: utf-8 -*-
"""手动点击采集器：记录真人鼠标点击，产出 jsonl 供分布对比分析。

对照校准工具链（参考 SmartOnmyoji issue #45）的采集端。与 demo
（click_monitor_demo.py）的差别：jsonl 落盘、精确归一化到
1280×720 授权空间、--title 绑定支持。

归一化基准是「游戏画面矩形」，不是整个客户区：MuMu 12 的窗口客户区顶部还有
约 40px 自绘标题栏（实测客户区 508×326 比例 1.558，而画面子窗口 509×286
比例 1.780 才等于 1280:720），拿客户区归一化会让 Y 系统性偏高——点画面顶边
得到 88 而非 0、点正中得到 404 而非 360。故先按宽高比在子窗口里认出画面区域，
认不出才回退到客户区（桌面客户端模式的客户区本就等于画面）。
画面矩形每次点击时重取，因此窗口中途被移动或缩放都不影响坐标。

人机分离原理：GetAsyncKeyState 只反映真实硬件输入——
- ADB / nemu_ipc 的脚本点击发生在模拟器内部，根本不经过 Windows；
- window_message（桌面客户端模式）用 PostMessage 注入，不改变异步键状态；
所以轮询采到的天然只有真人手动点击，模拟器端与桌面端通吃。

禁止改回 WH_MOUSE_LL 低级钩子：LL 钩子同步等待回调，纯 Python 处理
不够快会堵住全系统输入（实测鼠标卡顿），轮询方案零输入延迟风险。

用法：
    toolkit/python.exe -m dev_tools.click_collector                    # 列出候选窗口
    toolkit/python.exe -m dev_tools.click_collector --index 4          # 按序号绑定
    toolkit/python.exe -m dev_tools.click_collector --title 大号       # 按标题绑定
    toolkit/python.exe -m dev_tools.click_collector --hwnd 0x60AF8     # 按句柄绑定

停止：Ctrl+C。输出：log/click_monitor/<YYYYmmdd_HHMMSS>_manual.jsonl
"""
import argparse
import ctypes
import json
import sys
import time
from datetime import datetime
from pathlib import Path
import ctypes.wintypes as wintypes

# 控制台可能是 GBK，窗口标题含零宽字符等会炸 UnicodeEncodeError，强制 stdout 走 UTF-8
sys.stdout.reconfigure(encoding='utf-8', errors='replace')

# ---------------------------------------------------------------- Windows API 声明
user32 = ctypes.windll.user32

GWL_STYLE = -16
WS_CHILD = 0x40000000
VK_LBUTTON = 0x01
KEY_PRESSED = 0x8000  # GetAsyncKeyState 返回值最高位：当前按住

# wintypes 里没有 WNDENUMPROC，64 位下需用 WINFUNCTYPE 自行定义
WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

EnumWindows = user32.EnumWindows
EnumWindows.argtypes = [WNDENUMPROC, wintypes.LPARAM]

EnumChildWindows = user32.EnumChildWindows
EnumChildWindows.argtypes = [wintypes.HWND, WNDENUMPROC, wintypes.LPARAM]

IsWindowVisible = user32.IsWindowVisible
GetWindowTextW = user32.GetWindowTextW
GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]

GetWindowRect = user32.GetWindowRect
GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]

GetClientRect = user32.GetClientRect
GetClientRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]

GetAsyncKeyState = user32.GetAsyncKeyState
GetAsyncKeyState.argtypes = [ctypes.c_int]
GetAsyncKeyState.restype = ctypes.c_short

GetCursorPos = user32.GetCursorPos
GetCursorPos.argtypes = [ctypes.POINTER(wintypes.POINT)]

WindowFromPoint = user32.WindowFromPoint
WindowFromPoint.argtypes = [wintypes.POINT]
WindowFromPoint.restype = wintypes.HWND

GetAncestor = user32.GetAncestor
GetAncestor.argtypes = [wintypes.HWND, wintypes.UINT]
GetAncestor.restype = wintypes.HWND

ClientToScreen = user32.ClientToScreen
ClientToScreen.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.POINT)]

# 模拟器 / 游戏窗口标题关键词（用于候选列表排序与展示）
TITLE_KEYWORDS = ['阴阳师', 'Onmyoji', 'MuMu', 'mumu', '雷电', 'LDPlayer', '夜神', 'Nox',
                  '逍遥', 'MEmu', 'BlueStacks', '蓝叠']

# 轮询周期（秒）：5ms 足够捕捉点击按下边沿，时间戳误差 ±5ms 级
POLL_INTERVAL = 0.005

# 授权空间常宽（与 module/base/canvas.py 的 BASE_W/BASE_H 一致）
BASE_W, BASE_H = 1280, 720

# 游戏画面识别：宽高比须接近 16:9，且面积占客户区足够大，避免把 16:9 的小控件当成画面
BASE_RATIO = BASE_W / BASE_H          # 1.7778
RATIO_TOLERANCE = 0.03
MIN_FRAME_AREA_RATIO = 0.3

# 输出目录：沿用 OAS 日志目录，分析页按此约定找文件
OUTPUT_DIR = Path('./log/click_monitor')


# ---------------------------------------------------------------- 窗口枚举
def list_windows():
    """枚举可见顶层窗口，返回 [(hwnd, title, rect)]，含模拟器关键词的排前面。"""
    results = []

    @WNDENUMPROC
    def _cb(hwnd, _lparam):
        if not IsWindowVisible(hwnd):
            return True
        if user32.GetWindowLongW(hwnd, GWL_STYLE) & WS_CHILD:
            return True
        buf = ctypes.create_unicode_buffer(256)
        GetWindowTextW(hwnd, buf, 256)
        title = buf.value
        if not title:
            return True
        rect = wintypes.RECT()
        GetWindowRect(hwnd, ctypes.byref(rect))
        results.append((hwnd, title, (rect.left, rect.top, rect.right, rect.bottom)))
        return True

    EnumWindows(_cb, 0)
    # 标题含关键词的排前面，方便 --index 直接选
    results.sort(key=lambda r: 0 if any(k in r[1] for k in TITLE_KEYWORDS) else 1)
    return results


def root_ancestor(hwnd):
    """取 hwnd 的根所有者窗口：WindowFromPoint 可能命中目标窗口的子窗口。"""
    while hwnd:
        root = GetAncestor(hwnd, 2)  # GA_ROOTOWNER
        if not root or root == hwnd:
            return hwnd
        hwnd = root
    return 0


def resolve_target(args):
    """按 --index / --title / --hwnd 解析目标窗口，返回 (hwnd, title) 或 None。"""
    if args.hwnd is not None:
        buf = ctypes.create_unicode_buffer(256)
        GetWindowTextW(args.hwnd, buf, 256)
        return args.hwnd, buf.value
    wins = list_windows()
    if args.index is not None:
        if args.index >= len(wins):
            print('序号超范围，共 %d 个候选窗口' % len(wins))
            return None
        return wins[args.index][0], wins[args.index][1]
    if args.title is not None:
        for hwnd, title, _ in wins:
            if args.title in title:
                return hwnd, title
        print('未找到标题含 %r 的窗口' % args.title)
        return None
    return None


def find_frame_hwnd(target_hwnd):
    """在目标窗口的子窗口里认出游戏画面，返回其句柄；认不出返回 0。

    判据是宽高比接近 1280:720 且面积占客户区 MIN_FRAME_AREA_RATIO 以上。
    MuMu 12 的画面是独立子窗口（客户区顶部另有约 40px 自绘标题栏），桌面客户端
    模式没有这层子窗口，此时返回 0 让调用方回退到客户区。
    """
    client = wintypes.RECT()
    GetClientRect(target_hwnd, ctypes.byref(client))
    client_area = (client.right - client.left) * (client.bottom - client.top)

    best = [0, 0]  # [hwnd, 面积]，用列表避免 nonlocal 在 ctypes 回调里的可读性问题

    @WNDENUMPROC
    def _cb(child, _lparam):
        rect = wintypes.RECT()
        GetWindowRect(child, ctypes.byref(rect))
        w, h = rect.right - rect.left, rect.bottom - rect.top
        if w <= 0 or h <= 0:
            return True
        if abs(w / h - BASE_RATIO) > RATIO_TOLERANCE:
            return True
        if client_area and w * h < client_area * MIN_FRAME_AREA_RATIO:
            return True
        if w * h > best[1]:
            best[0], best[1] = child, w * h
        return True

    EnumChildWindows(target_hwnd, _cb, 0)
    return best[0]


def frame_rect(target_hwnd, frame_hwnd):
    """取游戏画面当前的屏幕矩形 (left, top, w, h)。

    每次点击都重取，所以窗口中途被移动或缩放都不会让坐标错位。
    frame_hwnd 为 0 时回退到客户区，并用 ClientToScreen 把客户区原点换成屏幕坐标，
    使两条路径对调用方是同一种「屏幕矩形」语义。
    """
    if frame_hwnd:
        rect = wintypes.RECT()
        GetWindowRect(frame_hwnd, ctypes.byref(rect))
        return rect.left, rect.top, rect.right - rect.left, rect.bottom - rect.top
    client = wintypes.RECT()
    GetClientRect(target_hwnd, ctypes.byref(client))
    origin = wintypes.POINT(0, 0)
    ClientToScreen(target_hwnd, ctypes.byref(origin))
    return origin.x, origin.y, client.right - client.left, client.bottom - client.top


def run_collect(target_hwnd, target_title, out_path):
    """主采集循环：轮询左键按下边沿，归属目标窗口则写一行 jsonl。

    返回 (点击数, 采集时长秒)。
    """
    # 归一化基准是游戏画面矩形而非整个客户区，理由见模块文档字符串
    frame_hwnd = find_frame_hwnd(target_hwnd)
    fx, fy, fw, fh = frame_rect(target_hwnd, frame_hwnd)
    if fw <= 0 or fh <= 0:
        print('目标窗口画面尺寸异常（%dx%d），中止' % (fw, fh))
        return 0, 0.0
    if frame_hwnd:
        print('画面子窗口: 0x%X  %dx%d（比例 %.3f）' % (frame_hwnd, fw, fh, fw / fh))
    else:
        print('未找到画面子窗口，回退用客户区 %dx%d（比例 %.3f）' % (fw, fh, fw / fh))
        if abs(fw / fh - BASE_RATIO) > RATIO_TOLERANCE:
            print('警告: 客户区比例偏离 16:9，采到的坐标可能整体偏移，建议核对窗口边框')
    # 放大倍率提示：窗口远小于 1280x720 时，一个鼠标像素会放大成数个授权像素
    print('归一化倍率: X=%.2f Y=%.2f（>1 表示存在量化误差，需精确坐标请把画面调到 1280x720）'
          % (BASE_W / fw, BASE_H / fh))

    clicks = 0
    skipped = 0           # 落在画面外（标题栏 / 工具栏）的点击，不计入数据
    pressed_last = False  # 上一轮轮询时左键是否按住（检测按下边沿用）
    start = time.time()
    pt = wintypes.POINT()

    print('绑定窗口: 0x%X %s' % (target_hwnd, target_title))
    print('输出文件: %s' % out_path)
    print('开始采集：在目标窗口上用鼠标点击，Ctrl+C 停止\n')

    # 逐行追加写：每次点击后立即 flush，进程被强杀也不丢已采数据
    with open(out_path, 'a', encoding='utf-8') as f:
        try:
            while True:
                pressed = bool(GetAsyncKeyState(VK_LBUTTON) & KEY_PRESSED)
                if pressed and not pressed_last:
                    # 按下边沿：取当前光标屏幕坐标，归属到目标窗口才记录
                    GetCursorPos(ctypes.byref(pt))
                    hit = WindowFromPoint(pt)
                    if hit == target_hwnd or root_ancestor(hit) == target_hwnd:
                        # 画面矩形每次重取，跟随窗口移动/缩放
                        fx, fy, fw, fh = frame_rect(target_hwnd, frame_hwnd)
                        # 相对画面原点归一化到 720p 授权空间，与脚本点击同坐标系
                        x = round((pt.x - fx) * BASE_W / fw)
                        y = round((pt.y - fy) * BASE_H / fh)
                        if 0 <= x <= BASE_W and 0 <= y <= BASE_H:
                            event = {
                                'ts': int(time.time() * 1000),
                                'x': x,
                                'y': y,
                                'source': 'manual',
                                'window': target_title,
                            }
                            f.write(json.dumps(event, ensure_ascii=False) + '\n')
                            f.flush()
                            clicks += 1
                            print('点击 #%d: 画面内 (%d, %d) -> 720p (%d, %d)'
                                  % (clicks, pt.x - fx, pt.y - fy, x, y))
                        else:
                            # 点在标题栏或模拟器工具栏上，不是游戏内操作，丢弃
                            skipped += 1
                            print('跳过 #%d: 画面外 (%d, %d)' % (skipped, x, y))
                pressed_last = pressed
                time.sleep(POLL_INTERVAL)
        except KeyboardInterrupt:
            pass

    if skipped:
        print('（另有 %d 次点击落在画面外被丢弃）' % skipped)
    return clicks, time.time() - start


def main():
    parser = argparse.ArgumentParser(description='手动点击采集器（jsonl 输出）')
    parser.add_argument('--list', action='store_true', help='仅列出候选窗口')
    parser.add_argument('--hwnd', type=lambda s: int(s, 0), help='直接指定窗口句柄（支持 0x 前缀）')
    parser.add_argument('--index', type=int, help='按候选列表序号绑定')
    parser.add_argument('--title', help='按标题子串绑定')
    args = parser.parse_args()

    if args.list or (args.hwnd is None and args.index is None and args.title is None):
        wins = list_windows()
        print('候选窗口（含模拟器/游戏关键词的排前面）：')
        for i, (hwnd, title, rect) in enumerate(wins):
            print('  [%d] hwnd=0x%X  %dx%d  %s' % (i, hwnd, rect[2] - rect[0], rect[3] - rect[1], title))
        if not args.list:
            print('\n用 --index <序号> / --title <子串> / --hwnd <句柄> 开始采集')
        return

    target = resolve_target(args)
    if not target:
        return
    target_hwnd, target_title = target

    # 输出文件：启动时刻命名，同一会话内重启不会互相覆盖
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    out_path = OUTPUT_DIR / ('%s_manual.jsonl' % datetime.now().strftime('%Y%m%d_%H%M%S'))

    clicks, duration = run_collect(target_hwnd, target_title, out_path)
    print('\n摘要: %d 次点击, 时长 %.1fs, 文件 %s' % (clicks, duration, out_path))


if __name__ == '__main__':
    main()
