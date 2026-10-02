# -*- coding: utf-8 -*-
"""模拟器手动点击监测 demo（轮询版）。

验证「能否采到模拟器窗口上的真人点击」。

方案演进：第一版用 WH_MOUSE_LL 低级钩子，实测会让全系统鼠标卡顿——LL 钩子是
同步的，Windows 要等回调返回才放行事件，纯 Python 回调处理不够快就堵住输入。
本版改为轮询：GetAsyncKeyState 检测左键按下边沿 + GetCursorPos 取坐标，完全不
装钩子、不碰系统输入路径，零卡顿风险。代价是时间戳精度受轮询周期限制（±5ms 级）。

人机分离原理：GetAsyncKeyState 只反映真实硬件输入状态——
- ADB / nemu_ipc 的脚本点击发生在模拟器内部，根本不经过 Windows；
- window_message（桌面客户端模式）用 PostMessage 注入，不改变异步键状态；
所以轮询采到的天然只有真人手动点击。

用法：
    toolkit/python.exe dev_tools/click_monitor_demo.py            # 列出候选窗口
    toolkit/python.exe dev_tools/click_monitor_demo.py --list     # 同上
    toolkit/python.exe dev_tools/click_monitor_demo.py --hwnd 0x102A4  # 绑定指定窗口采集
    toolkit/python.exe dev_tools/click_monitor_demo.py --index 1  # 按候选序号绑定

停止：Ctrl+C，退出时打印采集到的点击摘要。
"""
import argparse
import ctypes
import sys
import time
import ctypes.wintypes as wintypes

# 控制台可能是 GBK，窗口标题含零宽字符等会炸 UnicodeEncodeError，强制 stdout 走 UTF-8
sys.stdout.reconfigure(encoding='utf-8', errors='replace')

# ---------------------------------------------------------------- Windows API 声明
user32 = ctypes.windll.user32

GWL_STYLE = -16
WS_CHILD = 0x40000000
VK_LBUTTON = 0x01
KEY_PRESSED = 0x8000  # GetAsyncKeyState 返回值最高位：当前按住

# WNDENUMPROC 在 wintypes 里不存在，64 位下需用 WINFUNCTYPE 自行定义
WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

EnumWindows = user32.EnumWindows
EnumWindows.argtypes = [WNDENUMPROC, wintypes.LPARAM]

IsWindowVisible = user32.IsWindowVisible
GetWindowTextW = user32.GetWindowTextW
GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]

GetWindowRect = user32.GetWindowRect
GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]

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

ScreenToClient = user32.ScreenToClient
ScreenToClient.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.POINT)]

# 模拟器 / 游戏窗口标题关键词（用于候选列表排序与展示）
TITLE_KEYWORDS = ['阴阳师', 'Onmyoji', 'MuMu', 'mumu', '雷电', 'LDPlayer', '夜神', 'Nox',
                  '逍遥', 'MEmu', 'BlueStacks', '蓝叠']

# 轮询周期（秒）：5ms 足够捕捉点击按下边沿，时间戳误差 ±5ms 级
POLL_INTERVAL = 0.005


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
    return hwnd


def main():
    parser = argparse.ArgumentParser(description='模拟器手动点击监测 demo（轮询版）')
    parser.add_argument('--list', action='store_true', help='仅列出候选窗口')
    parser.add_argument('--hwnd', type=lambda s: int(s, 0), help='直接指定窗口句柄（支持 0x 前缀）')
    parser.add_argument('--index', type=int, help='按候选列表序号绑定')
    args = parser.parse_args()

    if args.list or (args.hwnd is None and args.index is None):
        wins = list_windows()
        print('候选窗口（含模拟器/游戏关键词的排前面）：')
        for i, (hwnd, title, rect) in enumerate(wins):
            print('  [%d] hwnd=0x%X  %dx%d  %s' % (i, hwnd, rect[2] - rect[0], rect[3] - rect[1], title))
        if not args.list:
            print('\n用 --index <序号> 或 --hwnd <句柄> 开始采集')
        return

    if args.index is not None:
        wins = list_windows()
        if args.index >= len(wins):
            print('序号超范围，共 %d 个候选窗口' % len(wins))
            return
        target_hwnd, target_title, _ = wins[args.index]
    else:
        target_hwnd, target_title = args.hwnd, ''
        buf = ctypes.create_unicode_buffer(256)
        GetWindowTextW(target_hwnd, buf, 256)
        target_title = buf.value

    print('绑定窗口: 0x%X %s' % (target_hwnd, target_title))
    print('开始采集：在目标窗口上用鼠标点几下，Ctrl+C 停止\n')

    rect = wintypes.RECT()
    GetWindowRect(target_hwnd, ctypes.byref(rect))
    win_w, win_h = rect.right - rect.left, rect.bottom - rect.top

    clicks = []
    pressed_last = False  # 上一轮轮询时左键是否按住（检测按下边沿用）
    start = time.time()
    pt = wintypes.POINT()
    client_pt = wintypes.POINT()

    try:
        while True:
            # GetAsyncKeyState 只看真实硬件输入：ADB/nemu_ipc 点击在模拟器内部、
            # window_message 用 PostMessage 注入，都不会置位，因此天然人机分离
            pressed = bool(GetAsyncKeyState(VK_LBUTTON) & KEY_PRESSED)
            if pressed and not pressed_last:
                # 按下边沿：取当前光标屏幕坐标，归属到目标窗口才记录
                GetCursorPos(ctypes.byref(pt))
                hit = WindowFromPoint(pt)
                if root_ancestor(hit) == target_hwnd or hit == target_hwnd:
                    client_pt.x, client_pt.y = pt.x, pt.y
                    ScreenToClient(target_hwnd, ctypes.byref(client_pt))
                    ts = time.time()
                    clicks.append((ts, client_pt.x, client_pt.y))
                    print('点击 #%d: 客户区坐标 (%d, %d)' % (len(clicks), client_pt.x, client_pt.y))
            pressed_last = pressed
            time.sleep(POLL_INTERVAL)
    except KeyboardInterrupt:
        pass

    print('\n摘要: %d 次点击, 时长 %.1fs, 窗口尺寸 %dx%d'
          % (len(clicks), time.time() - start, win_w, win_h))


if __name__ == '__main__':
    main()
