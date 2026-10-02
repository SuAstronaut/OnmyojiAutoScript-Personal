# This Python file uses the following encoding: utf-8
# @author runhey
# github https://github.com/runhey
import os
import re
import subprocess
import time
import ctypes
from ctypes import c_long, byref, POINTER, Structure, wintypes

from contextlib import contextmanager
from enum import Enum
from pathlib import Path
from time import sleep
from cached_property import cached_property
from anytree import NodeMixin, RenderTree, PreOrderIter
from win32api import (GetSystemMetrics, SendMessage, MAKELONG, PostMessage,
                      MonitorFromWindow, GetMonitorInfo)
from win32print import GetDeviceCaps
from win32process import GetWindowThreadProcessId
from win32gui import (GetWindowText, EnumWindows, FindWindow, FindWindowEx,
                      IsWindow, IsIconic, ShowWindow, GetWindowRect,
                      GetWindowDC, DeleteObject, SetForegroundWindow,
                      IsWindowVisible, GetDC, GetParent, EnumChildWindows,
                      GetClientRect, ClientToScreen, GetWindowLong, SetWindowPos,
                      GetClassName)
from win32con import (SRCCOPY, DESKTOPHORZRES, DESKTOPVERTRES, WM_LBUTTONUP,
                      WM_LBUTTONDOWN, WM_ACTIVATE, WA_ACTIVE, MK_LBUTTON,
                      WM_NCHITTEST, WM_SETCURSOR, HTCLIENT, WM_MOUSEMOVE,
                      WM_CLOSE, WM_KEYDOWN, WM_KEYUP, VK_RETURN,
                      GWL_STYLE, GWL_EXSTYLE, HWND_TOP, SWP_NOMOVE,
                      SWP_SHOWWINDOW, SW_RESTORE, MONITOR_DEFAULTTONEAREST)
from module.config.config import Config
from module.base.decorator import del_cached_property
from module.logger import logger
from module.exception import *

# 桌面客户端窗口标题（官方桌面版，多开共用同一标题）
DESKTOP_WINDOW_TITLES = ('阴阳师-网易游戏', '阴阳师-MuMu模拟器专版')

# 网易 MPay 账号登录弹窗的窗口类名。它是与游戏主窗口同 PID 的独立顶层窗口
# （DirectUI 绘制，无子控件），不在游戏渲染面内，因此主窗口 BitBlt 截不到它，
# 也无法用图像识别处理，只能按类名单独定位并注入消息。
DESKTOP_LOGIN_POPUP_CLASS = 'MPAY_LOGIN'

# 调整窗口尺寸时撞上客户端重建窗口的重试轮数与间隔（秒）。客户端确认登录弹窗后销毁
# 登录界面、重建游戏主窗口，实测这段空窗期在几百毫秒到数秒之间，6 轮 × 1s 足够覆盖
DESKTOP_RESIZE_ATTEMPTS = 6
DESKTOP_RESIZE_RETRY_INTERVAL = 1.0
# 关闭桌面客户端的强杀轮数。TerminateProcess 可能因权限被拒（实测本机出现过
# (5, '拒绝访问。')），也可能进程正在退出但还没消失，因此杀完必须验证进程真的没了，
# 没死就再杀一轮，而不是发完指令就当成功
DESKTOP_KILL_ATTEMPTS = 3
DESKTOP_KILL_POLL_INTERVAL = 0.5

# DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2，SetThreadDpiAwarenessContext 的入参
_PER_MONITOR_AWARE_V2 = ctypes.c_void_p(-4)

# 桌面客户端运行期间强制保持显示器常亮：桌面模式截图走 BitBlt，依赖 DWM 合成，
# 屏幕一旦因电源超时关闭，DWM 停止合成，BitBlt 只能返回静止黑帧，登录循环会因此
# 死等 300s 超时。在客户端启动时开启、关闭时清除，只锁客户端生命周期，不影响
# OAS 空闲时用户正常让屏幕休眠。
ES_CONTINUOUS = 0x80000000
ES_DISPLAY_REQUIRED = 0x00000002


def desktop_keep_screen_on(enable: bool) -> None:
    """按桌面客户端生命周期开关「强制保持显示器常亮」。

    SetThreadExecutionState 是线程级状态：ES_CONTINUOUS 保持该线程的请求，直到
    再次调用清除。OAS worker 的任务在同一长驻线程内启停客户端，因此这里设置即可。

    注意 ES_DISPLAY_REQUIRED 只「阻止未来关闭」，屏幕若已因电源超时熄灭，必须先用
    SC_MONITORPOWER(-1) 广播把显示器物理点亮，否则 BitBlt 仍返回黑帧。
    """
    if enable:
        # 物理点亮：SC_MONITORPOWER(-1) 单独发消息对部分显示器/驱动不触发硬件唤醒
        # （实测只发它屏幕仍黑），必须再模拟一次无害键盘输入与鼠标移动，产生真实
        # 输入事件才能真正点亮。VK_F15(0x7E) 无副作用，不会干扰游戏。
        try:
            # 必须用 SendMessageTimeoutW 而不是 SendMessageW：目标是
            # HWND_BROADCAST(0xFFFF)，SendMessageW 会同步等系统里每一个顶层窗口
            # 处理完才返回，只要有一个窗口消息循环繁忙或无响应就永久阻塞调用线程
            # （实测 pytest 里这一行挂死 26 分钟不返回，生产上等于 worker 线程卡住）。
            # SMTO_ABORTIFHUNG(0x0002) 遇到无响应窗口直接放弃，1000ms 上限兜底。
            ctypes.windll.user32.SendMessageTimeoutW(
                0xFFFF, 0x0111, 0xF170, -1, 0x0002, 1000, None)
        except Exception as e:
            logger.warning(f'SC_MONITORPOWER wake failed: {e}')
        try:
            user32 = ctypes.windll.user32
            user32.keybd_event(0x7E, 0, 0, 0)
            user32.keybd_event(0x7E, 0, 2, 0)
            user32.SetCursorPos(400, 300)
        except Exception as e:
            logger.warning(f'wake input failed: {e}')
    state = ES_CONTINUOUS | (ES_DISPLAY_REQUIRED if enable else 0)
    try:
        ctypes.windll.kernel32.SetThreadExecutionState(state)
        logger.info(f'Desktop screen keep-awake {"ON" if enable else "OFF"}')
    except Exception as e:
        logger.warning(f'SetThreadExecutionState({enable}) failed: {e}')


@contextmanager
def dpi_awareness():
    """临时把当前线程切到 Per-Monitor V2 DPI 感知，退出时恢复原上下文。

    OAS 进程自身未声明 DPI 感知，GDI 调用默认被系统虚拟化成逻辑像素：125% 缩放下
    物理 1280x720 的客户区只报 1024x576，BitBlt 也只能取到画面左上角那一块（右侧
    侧边栏与底部菜单栏直接丢失）。桌面客户端进程是 Per-Monitor DPI 感知、按物理
    像素原生渲染，因此桌面分支的窗口测量与截图必须在感知上下文里做才能拿到完整的
    物理像素画面。用线程级而非进程级，是为了不影响模拟器直控路径既有的逻辑像素假设。
    """
    user32 = ctypes.windll.user32
    previous = None
    try:
        previous = user32.SetThreadDpiAwarenessContext(_PER_MONITOR_AWARE_V2)
    except AttributeError:
        # Windows 10 1607 以前没有该 API，退化为原有逻辑像素行为
        pass
    try:
        yield
    finally:
        if previous:
            user32.SetThreadDpiAwarenessContext(previous)


def _window_total_size(width: int, height: int, style: int, ex_style: int) -> tuple:
    """用 user32.AdjustWindowRectEx 计算包含标题栏/边框的窗口总尺寸 (w, h)。

    pywin32 未导出 AdjustWindowRectEx，故用 ctypes 调用。
    """
    user32 = ctypes.windll.user32

    class RECT(Structure):
        _fields_ = [('left', c_long), ('top', c_long), ('right', c_long), ('bottom', c_long)]

    user32.AdjustWindowRectEx.argtypes = [POINTER(RECT), c_long, c_long, c_long]
    user32.AdjustWindowRectEx.restype = wintypes.BOOL
    rect = RECT(0, 0, width, height)
    user32.AdjustWindowRectEx(byref(rect), style, False, ex_style)
    return rect.right - rect.left, rect.bottom - rect.top


def _window_center_args(hwnd, total_w: int, total_h: int) -> tuple:
    """给 SetWindowPos 算出「在所在显示器工作区居中」的 (x, y, flags)。

    客户端自己启动时窗口是居中摆放的，OAS 把客户区放大到 1280x720 时若带 SWP_NOMOVE，
    左上角钉死不动、窗口只朝右下膨胀，视觉上就从居中变成偏右下（小屏还会捅出屏幕边界）。
    因此调整尺寸时同步把窗口移回正中。

    取显示器信息失败时返回 SWP_NOMOVE 退化为保持原位：居中只是观感，resize 才是识别
    1:1 的刚需，不能让前者的失败连带把后者也搞失败。

    必须在 dpi_awareness() 上下文内调用。工作区与 SetWindowPos 必须同处物理像素空间，
    否则 125% 缩放下拿到的是被虚拟化的逻辑工作区，算出的位置整体偏左上。
    按 Work（工作区）而非 Monitor（整屏）居中，才不会被任务栏压掉一截。
    """
    try:
        monitor = MonitorFromWindow(hwnd, MONITOR_DEFAULTTONEAREST)
        left, top, right, bottom = GetMonitorInfo(monitor)['Work']
    except Exception as e:
        logger.warning(f'GetMonitorInfo failed (hwnd={hwnd}): {e}, keep window position')
        return 0, 0, SWP_NOMOVE | SWP_SHOWWINDOW
    # 窗口比工作区还大时夹到工作区左上角，保证标题栏可见而不是负坐标跑到屏幕外
    x = max(left, left + (right - left - total_w) // 2)
    y = max(top, top + (bottom - top - total_h) // 2)
    return x, y, SWP_SHOWWINDOW


def list_desktop_windows() -> list:
    """枚举当前所有桌面客户端窗口。

    返回 [{'pid': int, 'title': str, 'x': int, 'y': int}]，按屏幕位置排序。
    桌面客户端多开时窗口标题完全相同，只能靠 PID 区分，附带左上角坐标是为了
    让用户在界面上按"窗口摆在哪"对号入座。按坐标排序保证同一组窗口每次枚举
    顺序稳定（EnumWindows 的返回顺序随 Z 序变化）。
    """
    def enum_cb(hwnd, param):
        param.append(hwnd)
        return True

    handles = []
    EnumWindows(enum_cb, handles)
    result = []
    for hwnd in handles:
        if GetWindowText(hwnd) not in DESKTOP_WINDOW_TITLES:
            continue
        try:
            rect = GetWindowRect(hwnd)
        except Exception:
            # 窗口在枚举与取矩形之间被关闭，跳过即可
            continue
        result.append({
            'pid': GetWindowThreadProcessId(hwnd)[1],
            'title': GetWindowText(hwnd),
            'hwnd': hwnd,
            'x': rect[0],
            'y': rect[1],
        })
    result.sort(key=lambda w: (w['x'], w['y'], w['pid']))
    return result


def desktop_window_option(window: dict) -> str:
    """把窗口信息格式化成界面下拉项文本，形如 `27272 (154,38)`。

    不显示窗口标题：桌面客户端多开时标题完全相同，且中文字样在英文界面里冗余。
    PID 放在最前，desktop_option2pid 只认第一段数字，坐标纯属给用户辨识用，
    改动展示格式不会影响解析。
    """
    return f"{window['pid']} ({window['x']},{window['y']})"


def desktop_option2pid(option: str) -> str:
    """从下拉项文本中取回纯 PID；取不到时返回空串。

    界面回传的是展示文本，但落盘的 handle 必须是纯 PID（Handle 按数字消费），
    因此写入配置前统一在这里剥掉标题与坐标。用户手工填的纯数字原样通过。
    """
    if not option:
        return ''
    matched = re.match(r'\s*(\d+)', str(option))
    return matched.group(1) if matched else ''


def handle_title2num(title: str) -> int:
    """
    从标题到句柄号
    :param title:
    :return:  如果没有找到就是返回零
    """
    return FindWindow(None, title)


def handle_num2title(num: int) -> str:
    """
    通过句柄号返回窗口的标题，如果传入句柄号不合法则返回None
    :param num:
    :return:
    """
    return None if num is None or num == 0 or num == '' else GetWindowText(num)


def is_handle_valid(num: int) -> bool:
    """
    输入一个句柄号，如果还在返回True
    :param num:
    :return:
    """
    return IsWindow(num)


def handle_num2pid(num: int) -> int:
    """
    通过句柄号获取句柄进程id，如果句柄号非法则返回0
    :param num:
    :return:
    """
    return 0 if num is None or num == 0 or num == '' else GetWindowThreadProcessId(num)[1]


def window_scale_rate() -> float:
    """
    获取window的系统缩放 一遍是1
    :return:
    """
    hDC = GetDC(0)
    # 物理上（真实的）的 横纵向分辨率
    wReal = GetDeviceCaps(hDC, DESKTOPHORZRES)
    hReal = GetDeviceCaps(hDC, DESKTOPVERTRES)
    # 缩放后的 分辨率
    wAfter = GetSystemMetrics(0)
    hAfter = GetSystemMetrics(1)
    # print(wReal, wAfter)
    return round(wReal / wAfter, 2)


class WindowNode(NodeMixin):
    def __init__(self, name, num, parent=None):
        super().__init__()
        self.name = name
        self.num = num
        self.parent = parent

    @classmethod
    def get_tree_depth(cls, root_node: 'WindowNode'):
        if not root_node.children:
            return 1 if root_node else 0
        return max(node.depth for node in root_node.descendants) + 1


class EmulatorFamily(Enum):
    FAMILY_MUMU = 10  # mumu模拟器
    FAMILY_NOX = 20  # 夜神模拟器
    FAMILY_LD = 30  # 雷电模拟器
    FAMILY_MEMU = 40  # 逍遥模拟器
    FAMILY_BLUESTACKS = 50  # 蓝叠模拟器
    FAMILY_OTHER = 60  # 其他模拟器 待定


# 各个模拟器的句柄树*******************************************************************************************************
""""
<MuMu>系列
模拟器的窗口名字
----MuMuPlayer      (!如果是mumu12是MuMuPlayer, 否则是NemuPlayer)
--------nemudisplay

<雷电模拟器系列>
雷电模拟器的窗口名字
----TheRender
--------sub

<夜神模拟器系列>  =====> 这个模拟器窗口很复杂，而且有的时候还会变化
夜神模拟器的窗口名字
----Nox
----Nox
--------toolbar_nox
--------Nox
------------Nox
----------------sub
---Nox
--------Nox
--------Nox    ==> 妈的太多了自己用spy++看吧

<蓝叠模拟器>
蓝叠模拟器的窗口名字
----HD-Player
--------_ctl.W
"""""
# **********************************************************************************************************************

class Handle:
    emulator_list = ['MuMu12',
                     'MuMu',
                     '雷电',
                     '夜神',
                     '蓝叠',
                     '逍遥',
                     '模拟器']  # 最后一个我又不知道还有哪些模拟器
    emulator_handle = {
        # 夜神
        'nox_player': ['root_handle_title', 'Nox'],
        'nox_player_64': ['root_handle_title', 'Nox'],
        'nox_player_family': ['root_handle_title', 'Nox'],
        # 雷电
        'ld_player': ['TheRender'],
        'ld_player_4': ['TheRender'],
        'ld_player_9': ['TheRender'],
        'ld_player_family': ['TheRender'],
        # 逍遥
        'memu_player': ['root_handle_title'],
        'memu_player_family': ['root_handle_title'],
        # mumu
        'mumu_player': ['root_handle_title', 'NemuPlayer'],
        'mumu_player_12': ['root_handle_title', 'MuMuPlayer'],
        'mumu_player_family': ['root_handle_title', 'MuMuPlayer'],
        # 蓝叠
        'bluestacks_5': ['root_handle_title'],
        'bluestacks_family': ['root_handle_title']
    }
    config: Config = None
    is_desktop_window: bool = False
    """是否为桌面客户端模式（serial='desktop'），桌面分支统一用此标志隔离"""

    def __init__(self, config) -> None:
        """

        :param config:
        """
        logger.hr('Handle')
        if self.config is None:
            if isinstance(config, str):
                self.config = Config(config, task=None)
            else:
                self.config = config
        if not self.config.script.device.handle or self.config.script.device.handle == '':
            logger.info('Handle is empty, oas not use handle')
            return

        # 桌面客户端模式：按 PID 定位窗口，跳过模拟器窗口树逻辑
        if self.config.script.device.serial == 'desktop':
            self.root_handle_title = ''
            self.root_handle_num = 0
            self.root_handle = self.config.script.device.handle
            logger.info(f'Desktop handle PID is {self.root_handle}')
            self.root_handle_num = self.find_desktop_window_by_pid(self.root_handle)
            self.root_handle_title = DESKTOP_WINDOW_TITLES[0]
            self.is_desktop_window = True
            logger.info(f'Desktop client window found: title={self.root_handle_title}, hwnd={self.root_handle_num}')
            return

        # 获取根的句柄
        self.root_handle_title = ''
        self.root_handle_num = 0
        self.root_handle = self.config.script.device.handle
        logger.info(f'Handle is {self.root_handle}')
        if self.root_handle == "auto":
            logger.info('Handle is auto. oas will find window emulator')
            window_list = Handle.all_windows()
            self.root_handle_title = self.auto_handle_title(window_list)
            if not self.root_handle_title:
                logger.error('Auto handle failed, no emulator window found')
                raise EmulatorNotRunningError
            self.root_handle_num = handle_title2num(self.root_handle_title)
        if isinstance(self.root_handle, str):
            try:
                self.root_handle_num = int(self.root_handle)
                logger.info('Handle is a number, using it as root handle num')
                if is_handle_valid(self.root_handle_num):
                    logger.info(f'Handle number {self.root_handle_num} is valid')
                    self.root_handle_title = handle_num2title(self.root_handle_num)
            except ValueError:
                logger.info('Handle is a string, looking up window by title')
                if handle_title2num(self.root_handle) != 0:
                    self.root_handle_num = handle_title2num(self.root_handle)
                    self.root_handle_title = self.root_handle
                else:
                    logger.error(f'Handle title "{self.root_handle}" not found, emulator may not be running')
                    raise EmulatorNotRunningError
        logger.info(f'The root handle title is {self.root_handle_title} and num is {self.root_handle_num}')

        # 获取句柄树（加重试，等待子窗口渲染就绪）
        self.root_node = WindowNode(name=self.root_handle_title, num=self.root_handle_num)
        Handle.handle_tree(self.root_handle_num, self.root_node)
        if not self.root_node.children:
            logger.info('Window child tree not ready, waiting for emulator to finish initializing')
            for i in range(9):
                sleep(1)
                self.root_node = WindowNode(name=self.root_handle_title, num=self.root_handle_num)
                Handle.handle_tree(self.root_handle_num, self.root_node)
                if self.root_node.children:
                    logger.info(f'Window child tree ready after {i + 2} attempts')
                    break
            else:
                logger.warning('Window child tree still not ready after 10 attempts, will use title-based fallback')

        logger.info('Emulator handle structure:')
        for pre, fill, node in RenderTree(self.root_node):
            pass
            #logger.info("%s%s" % (pre, node.name))
        for pre, fill, node in RenderTree(self.root_node):
            pass
            #logger.info("%s%s" % (pre, node.num))

        # 判断是哪一个模拟器 通过句柄树结构
        logger.info(f'Emulator family: {self.emulator_family}')
        # window系统的缩放
        logger.info(f'Window screen scale rate: {window_scale_rate()}')
        # screenshot_handle_num 和 screenshot_size 延迟到首次截屏时按需求值，不在初始化时预计算

    def find_desktop_window_by_pid(self, pid) -> int:
        """按 PID 查找桌面客户端窗口，返回窗口句柄；未找到抛 EmulatorNotRunningError。

        「找不到窗口」记 warning 而非 error：本方法的调用方多为探测存在性
        （desktop_window_exists 判断是否需要重拉、_desktop_wait_closed 确认已关闭），
        找不到是正常答案。真正的故障由上层在拿到 EmulatorNotRunningError 后判定，
        这样真故障不会被淹没在每轮任务收尾都出现的噪音里。
        """
        try:
            pid_int = int(str(pid))
        except (TypeError, ValueError):
            logger.error(f'Invalid desktop PID: {pid}')
            raise EmulatorNotRunningError(f'Invalid desktop client PID: {pid}')

        def enum_cb(hwnd, param):
            param.append(hwnd)
            return True

        windows = []
        EnumWindows(enum_cb, windows)
        for hwnd in windows:
            if GetWindowText(hwnd) in DESKTOP_WINDOW_TITLES:
                _, win_pid = GetWindowThreadProcessId(hwnd)
                if win_pid == pid_int:
                    return hwnd
        logger.warning(f'Desktop client window not found, PID={pid}')
        raise EmulatorNotRunningError(f'Desktop client window not found, PID={pid}')

    def desktop_pid(self):
        """返回当前桌面客户端 PID（int）；取不到返回 None。

        优先用实例 root_handle（运行时自动启动绑定后的新 PID），未设置时回退配置里的
        handle。script.device 是 COLD 受保护子树，任务边界 reload 会覆盖运行模型，
        因此本会话的 PID 判断必须以实例状态为准，否则重新拉起后会误判。
        """
        pid = getattr(self, 'root_handle', None) or self.config.script.device.handle
        try:
            return int(str(pid))
        except (TypeError, ValueError):
            return None

    def find_desktop_login_popup(self) -> int:
        """按 PID + 类名查找 MPay 账号登录弹窗，返回句柄；没有则返回 0。

        弹窗与游戏主窗口同 PID 但是独立顶层窗口（owner 为主窗口，非子窗口），
        没有子控件可枚举，只能按窗口类名定位。
        按 PID 过滤保证多开时各实例只处理自己客户端的弹窗。

        主窗口 BitBlt 是否包含这个 owner 窗口受桌面合成状态影响，同一个弹窗可能在
        不同错误截图中出现或消失，所以登录状态不能依赖截图判断。
        改按「HWND 是否仍存活」判断而不再过滤 IsWindowVisible：真机验证过已登录的
        客户端（PID 29512）全系统枚举不到任何 MPAY_LOGIN 顶层窗口，说明客户端确认
        弹窗后是销毁它、而非隐藏（与下方 DESKTOP_RESIZE_ATTEMPTS 注释记录的
        「销毁登录界面、重建游戏主窗口」一致）。因此 HWND 存活即代表尚未登录。
        """
        pid_int = self.desktop_pid()
        if pid_int is None:
            return 0

        def enum_cb(hwnd, param):
            param.append(hwnd)
            return True

        windows = []
        EnumWindows(enum_cb, windows)
        for hwnd in windows:
            try:
                if GetClassName(hwnd) != DESKTOP_LOGIN_POPUP_CLASS:
                    continue
                if GetWindowThreadProcessId(hwnd)[1] == pid_int:
                    return hwnd
            except Exception:
                # 窗口在枚举与取属性之间被关闭，跳过即可
                continue
        return 0

    def desktop_confirm_login_popup(self, wait: float = 15.0) -> bool:
        """向 MPay 登录弹窗发送回车，并确认对应 HWND 已真实消失。

        弹窗是 DirectUI 独立顶层窗口，"进入游戏"只是绘制出来的像素、没有真实控件，
        实测后台鼠标消息（WM_MOUSEMOVE/LBUTTONDOWN/LBUTTONUP，Post 与 Send 都试过）
        完全无响应；同步与异步回车在无前台焦点时均可触发默认按钮。因此这里只发回车，
        并在 wait 秒内轮询确认弹窗 HWND 真的消失。

        返回 True 表示弹窗确实已关闭；False 表示没有弹窗，或超时后弹窗仍然存活。
        """
        if not self.find_desktop_login_popup():
            return False
        logger.info('Desktop MPay login popup found, wait 5s before closing to let it load')
        # 实测弹窗出现太早（启动后约 6s）时，回车关掉弹窗后客户端仍停在黑屏；给弹窗
        # 5 秒加载时间再回车，降低 Enter 后黑屏卡死的概率（成功案例弹窗出现都偏晚）。
        time.sleep(5)
        deadline = time.time() + wait
        while time.time() < deadline:
            hwnd = self.find_desktop_login_popup()
            if not hwnd:
                logger.info('Desktop MPay login popup confirmed closed')
                return True
            self._desktop_send_enter(hwnd)
            time.sleep(1)
        logger.warning(f'Desktop MPay login popup still present after {wait}s, Enter had no effect')
        return False

    def desktop_window_exists(self) -> bool:
        """桌面模式：目标 PID 对应窗口仍存在即视为客户端运行中。

        优先用实例 root_handle（运行时自动启动绑定后的新 PID），未设置时回退配置里的
        handle。script.device 是 COLD 受保护子树，任务边界 reload 会覆盖运行模型，
        因此本会话的窗口存在性判断必须以实例状态为准，否则重新拉起后会误判为未运行。
        """
        pid = getattr(self, 'root_handle', None) or self.config.script.device.handle
        try:
            self.find_desktop_window_by_pid(pid)
            return True
        except EmulatorNotRunningError:
            return False

    def desktop_client_offset(self) -> tuple:
        """返回客户区在窗口 DC 内的偏移 (x, y)，用于 BitBlt 扣除标题栏/边框。"""
        with dpi_awareness():
            window_rect = GetWindowRect(self.screenshot_handle_num)
            client_origin = ClientToScreen(self.screenshot_handle_num, (0, 0))
            return client_origin[0] - window_rect[0], client_origin[1] - window_rect[1]

    def desktop_client_size(self) -> tuple:
        """返回桌面客户端窗口客户区的物理像素尺寸 (width, height)。"""
        with dpi_awareness():
            rect = GetClientRect(self.screenshot_handle_num)
            return rect[2] - rect[0], rect[3] - rect[1]

    def desktop_client_size_virtual(self) -> tuple:
        """返回客户区在 DPI 虚拟化空间下的尺寸 (width, height)。

        OAS 进程未声明 DPI 感知，PostMessage 的 lParam 会被系统按该空间解释，
        因此后台输入的坐标必须换算到这里，而不是截图所用的物理空间。
        """
        rect = GetClientRect(self.screenshot_handle_num)
        return rect[2] - rect[0], rect[3] - rect[1]

    def _desktop_client_size(self, hwnd):
        """读窗口客户区物理尺寸，句柄已失效返回 None。

        客户端从登录界面切到游戏主窗口时会销毁重建渲染窗口，因此调整窗口期间的任何
        一次 Win32 调用都可能撞上失效句柄。GetClientRect 对废句柄抛
        (1400, '无效的窗口句柄')，不接住会直接崩掉整个脚本进程。
        """
        if not IsWindow(hwnd):
            return None
        try:
            rect = GetClientRect(hwnd)
        except Exception as e:
            logger.warning(f'GetClientRect failed (hwnd={hwnd}): {e}')
            return None
        return rect[2] - rect[0], rect[3] - rect[1]

    def _desktop_clear_handle_cache(self) -> None:
        """失效截图相关 cached_property，使其按当前 root_handle_num 重新求值。

        桌面模式下 screenshot_handle_num 直接返回 root_handle_num，但它是
        cached_property：客户端重开后 root_handle_num 已换成新 hwnd，缓存仍指向
        旧句柄，截图时 GetClientRect 会拿废句柄抛 (1400)。
        """
        for prop in ('screenshot_handle_num', 'screenshot_size'):
            if prop in self.__dict__:
                del_cached_property(self, prop)

    def _desktop_rebind_window(self) -> bool:
        """按 PID 重新查找客户端窗口并绑定新 hwnd，返回是否绑定成功。

        用于窗口重建（登录界面切游戏主窗口）后刷新句柄：进程还活着，不需要重拉客户端，
        只要拿到重建后的新窗口即可。
        """
        try:
            hwnd = self.find_desktop_window_by_pid(self.root_handle)
        except EmulatorNotRunningError:
            return False
        if hwnd != self.root_handle_num:
            logger.info(f'Desktop window rebuilt, rebind hwnd {self.root_handle_num} -> {hwnd}')
            self.root_handle_num = hwnd
            self._desktop_clear_handle_cache()
        return True

    def desktop_window_set_size(self, width: int = 1280, height: int = 720) -> bool:
        """桌面模式：检测窗口客户区尺寸，非目标大小时用 SetWindowPos 调整到 width×height。

        调整尺寸时一并把窗口移回所在显示器工作区正中（尺寸本来就对则不动窗口，保留用户
        手动摆放的位置）；返回是否执行了调整。全过程在 DPI 感知上下文内完成，
        GetClientRect/SetWindowPos 处理的都是物理像素，因此目标尺寸无需按缩放比换算，
        调整后客户区物理尺寸恰为 width×height，游戏画面与资产 1:1 对应。

        客户端确认登录弹窗后会销毁登录界面、重建游戏主窗口，调整过程中旧 hwnd 随时可能
        失效。此时不抛异常也不重拉客户端（进程还活着），而是按 PID 重新绑定重建后的
        窗口再试，最多 DESKTOP_RESIZE_ATTEMPTS 轮。
        """
        if not getattr(self, 'is_desktop_window', False):
            return False
        for attempt in range(1, DESKTOP_RESIZE_ATTEMPTS + 1):
            result = self._desktop_try_set_size(width, height)
            if result is not None:
                return result
            if attempt >= DESKTOP_RESIZE_ATTEMPTS:
                break
            logger.info(f'Desktop window invalid, rebind and retry resize '
                        f'({attempt}/{DESKTOP_RESIZE_ATTEMPTS})')
            time.sleep(DESKTOP_RESIZE_RETRY_INTERVAL)
            self._desktop_rebind_window()
        logger.warning(f'Desktop window resize gave up after {DESKTOP_RESIZE_ATTEMPTS} attempts, '
                       f'window kept invalid')
        return False

    def _desktop_try_set_size(self, width: int, height: int):
        """单轮尝试调整窗口尺寸。

        返回 True/False 表示本轮已得出结论（是否执行了调整）；返回 None 表示句柄失效，
        需由调用方重新绑定窗口后再试。三态的意义在于把「尺寸本来就对」（False）和
        「窗口正在重建」（None）区分开，否则前者会白等重试。
        """
        hwnd = self.root_handle_num
        if not hwnd or not IsWindow(hwnd):
            logger.warning(f'Desktop window handle invalid (hwnd={hwnd})')
            return None
        with dpi_awareness():
            size = self._desktop_client_size(hwnd)
            if size is None:
                logger.warning(f'Desktop window vanished before resize (hwnd={hwnd})')
                return None
            client_w, client_h = size
            logger.info(f'Desktop client size: {client_w}x{client_h} (physical), target {width}x{height}')
            if client_w == width and client_h == height:
                logger.info('Desktop client size already matches target')
                return False
            # 用 AdjustWindowRectEx 精确计算含标题栏/边框的窗口总尺寸
            style = GetWindowLong(hwnd, GWL_STYLE)
            ex_style = GetWindowLong(hwnd, GWL_EXSTYLE)
            total_w, total_h = _window_total_size(width, height, style, ex_style)
            # 与 resize 同一次调用里把窗口摆回正中：分两次 SetWindowPos 会让窗口先跳到
            # 偏位再挪回来，用户能看到明显的闪动
            x, y, flags = _window_center_args(hwnd, total_w, total_h)
            logger.info(f'Resize desktop window to {total_w}x{total_h} at ({x}, {y}) '
                        f'to get client {width}x{height}')
            try:
                SetWindowPos(hwnd, HWND_TOP, x, y, total_w, total_h, flags)
            except Exception as e:
                # 拒绝访问通常是目标窗口权限更高（游戏以管理员运行）或客户端锁定窗口大小
                logger.error(f'SetWindowPos failed: {e}. '
                             f'请以管理员身份运行 OAS，或手动把游戏窗口设为 1280x720')
                return False
            # 校准：SetWindowPos 后的实际客户区可能与目标差几像素，按差值持续修正。
            # SetWindowPos 本身可能正好撞上客户端重建窗口，因此每轮都要重新确认句柄有效
            for _ in range(5):
                size = self._desktop_client_size(hwnd)
                if size is None:
                    logger.warning(f'Desktop window vanished during resize (hwnd={hwnd}), '
                                   f'client is rebuilding its window')
                    return None
                cw, ch = size
                if cw == width and ch == height:
                    return True
                total_w += width - cw
                total_h += height - ch
                # 总尺寸变了就要重算居中位置，否则校准完的窗口会偏离中心几个像素
                x, y, flags = _window_center_args(hwnd, total_w, total_h)
                logger.info(f'Calibrate desktop window to {total_w}x{total_h} at ({x}, {y}), '
                            f'current client {cw}x{ch}')
                try:
                    SetWindowPos(hwnd, HWND_TOP, x, y, total_w, total_h, flags)
                except Exception as e:
                    logger.warning(f'SetWindowPos failed during calibration (hwnd={hwnd}): {e}')
                    return None
            return True

    # ------------------------------------------------------------------ 桌面客户端自动生命周期

    def desktop_resolve_install_root(self) -> str:
        """解析桌面客户端安装目录：优先 desktop_game_path 配置，其次自动发现。

        只认含 bin\\onmyoji.exe（或根目录 Launch.exe）的安装目录，找不到返回空串。
        仅桌面模式调用，不影响模拟器流程。
        """
        configured = getattr(self.config.script.device, 'desktop_game_path', '') or ''
        if configured:
            root = self._desktop_root_from_path(configured)
            if root is not None:
                return str(root)
            logger.warning(f'desktop_game_path 配置无效: {configured}，尝试自动发现')
        root = self._desktop_discover_install_root()
        return str(root) if root else ''

    @staticmethod
    def _desktop_root_from_path(value: str):
        """把用户填的路径归一化成安装目录；无效返回 None。"""
        path = Path(value).expanduser()
        if path.is_file():
            # 允许直接填 bin\\onmyoji.exe 或 Launch.exe 的完整路径
            if path.name.lower() == 'onmyoji.exe' and path.parent.name.lower() == 'bin':
                path = path.parent.parent
            elif path.name.lower() == 'launch.exe':
                path = path.parent
            else:
                path = path.parent
        candidate = path / 'bin' / 'onmyoji.exe'
        if candidate.is_file():
            return path.resolve()
        return None

    @staticmethod
    def _desktop_discover_install_root():
        """按 %ProgramFiles%\\Onmyoji 与注册表 Uninstall 的 InstallLocation 自动发现安装目录。"""
        for env in ('ProgramFiles', 'ProgramFiles(x86)'):
            raw = os.environ.get(env)
            if raw and (Path(raw) / 'Onmyoji' / 'bin' / 'onmyoji.exe').is_file():
                return (Path(raw) / 'Onmyoji').resolve()
        try:
            import winreg
            roots = (
                (winreg.HKEY_LOCAL_MACHINE, r'SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall'),
                (winreg.HKEY_LOCAL_MACHINE, r'SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall'),
                (winreg.HKEY_CURRENT_USER, r'SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall'),
            )
            for hive, key_path in roots:
                try:
                    with winreg.OpenKey(hive, key_path) as parent:
                        for index in range(winreg.QueryInfoKey(parent)[0]):
                            try:
                                with winreg.OpenKey(parent, winreg.EnumKey(parent, index)) as child:
                                    display, _ = winreg.QueryValueEx(child, 'DisplayName')
                                    if '阴阳师' not in str(display) and 'Onmyoji' not in str(display):
                                        continue
                                    location, _ = winreg.QueryValueEx(child, 'InstallLocation')
                                    if location and (Path(location) / 'bin' / 'onmyoji.exe').is_file():
                                        return Path(location).resolve()
                            except OSError:
                                continue
                except OSError:
                    continue
        except Exception:
            pass
        return None

    @staticmethod
    def desktop_game_exe(root) -> str:
        """返回游戏可执行文件路径；找不到返回空串。

        顺序即优先级：必须优先游戏本体 bin/onmyoji.exe。
        Launch.exe 是登录器，它会再拉起 bin/onmyoji.exe，
        导致双开且 OAS 绑定到登录器 PID 上找不到游戏窗口。
        """
        root = Path(root)
        for name in ('bin/onmyoji.exe', 'Launch.exe'):
            exe = root / name
            if exe.is_file():
                return str(exe)
        return ''

    def launch_desktop_client(self, timeout: int = 90) -> bool:
        """自动启动桌面客户端并绑定新窗口的 PID/HWND，成功返回 True。

        单轮 = 启动 exe → 等新窗口出现并稳定 → 绑定其 PID/HWND。到此启动即完成；
        MPay 登录弹窗与进游戏属登录流程，由 Restart 的 app_handle_login 负责。
        timeout 内没等到窗口说明客户端起歪了（卡加载、崩在启动期等），强杀本轮进程后
        整轮重跑一次；第二轮仍失败则记 error 返回 False，由上层停下等人工，不无限重试。
        安装目录/exe 找不到属配置问题，重试无意义，直接返回 False。
        """
        root = self.desktop_resolve_install_root()
        if not root:
            logger.error('未找到阴阳师桌面客户端安装目录，请在 设置-Script-设备 中填写 desktop_game_path')
            return False
        exe = self.desktop_game_exe(root)
        if not exe:
            logger.error(f'未找到游戏程序（bin\\onmyoji.exe 或 Launch.exe）：{root}')
            return False

        for attempt in (1, 2):
            pids = self._desktop_launch_attempt(exe, root, timeout, attempt)
            if pids is None:
                # Popen 本身失败，重试也起不来
                return False
            bound_pid, spawned_pid = pids
            if bound_pid:
                # 启动成功也要记住本轮拉起的全部 PID：onmyoji.exe 是启动器，它会派生出
                # 真正的游戏窗口进程后自己继续存活（实测启动器 8932 派生窗口进程 6816，
                # 89 线程 42s CPU 却无窗口）。OAS 靠枚举窗口只绑到窗口进程，关闭时若只杀
                # 它，启动器就成了无主残留。这里留档，交给 desktop_stop_client 一并清理
                self._desktop_spawned_pids = set(spawned_pid)
                # 客户端已拉起，强制保持显示器常亮，防止电源超时关屏导致截图黑帧卡死
                desktop_keep_screen_on(True)
                return True
            if attempt == 1:
                # 只杀本轮确切启动的进程：绑定阶段就失败时 root_handle 仍是上一次的陈旧
                # PID，直接调 desktop_force_kill 会误杀（PID 可能已被系统复用）
                logger.warning('第 1 轮启动未就绪，清理本轮客户端后重试')
                self._desktop_kill_pids(spawned_pid)
        logger.error('桌面客户端连续 2 轮启动均未就绪，请检查客户端状态与机器负载')
        return False

    def _desktop_launch_attempt(self, exe: str, root: str, timeout: int, attempt: int):
        """启动客户端并绑定其窗口的单轮尝试。

        返回 (bound_pid, spawned_pid)：bound_pid 非 0 表示本轮成功；spawned_pid 是本轮
        Popen 出的进程与绑定到的窗口 PID 集合，供失败清理精确定位。Popen 失败返回 None。
        """
        before_pids = {w['pid'] for w in list_desktop_windows()}
        logger.info(f'自动启动桌面客户端（第 {attempt} 轮）: {exe}')
        spawned = set()
        try:
            proc = subprocess.Popen([exe], cwd=root,
                                    creationflags=getattr(subprocess, 'CREATE_NEW_PROCESS_GROUP', 0))
            spawned.add(proc.pid)
        except Exception as e:
            logger.error(f'启动桌面客户端失败: {e}')
            return None

        deadline = time.time() + timeout
        stable_hwnd = 0
        stable_count = 0
        while time.time() < deadline:
            candidates = [w for w in list_desktop_windows() if w['pid'] not in before_pids]
            if candidates:
                candidate = candidates[0]
                if candidate['hwnd'] == stable_hwnd:
                    stable_count += 1
                else:
                    stable_hwnd = candidate['hwnd']
                    stable_count = 1
                if stable_count >= 2:
                    self.desktop_bind_pid(candidate['pid'], candidate['hwnd'])
                    spawned.add(candidate['pid'])
                    logger.info(f'桌面客户端已自动启动并绑定 PID={candidate["pid"]}')
                    # 到这里启动就完成了：进程在跑、窗口句柄已绑定。
                    # MPay 登录弹窗与进游戏属于登录流程，由 Restart 的 app_handle_login
                    # 负责（登录循环每轮都复查弹窗，中途冒出来也能接住）。
                    # 这里不再等弹窗：启动侧等一遍、登录侧再确认一遍是串行叠加的重复工作，
                    # 实测白等约 20 秒，而登录循环本可以边截图边处理掉它
                    return candidate['pid'], spawned
            time.sleep(1)
        logger.warning(f'桌面客户端进程已启动，但 {timeout} 秒内未识别到游戏窗口')
        return 0, spawned

    def _desktop_kill_pids(self, pids) -> None:
        """强杀指定 PID 集合（本轮启动失败的清理），逐个容错并验证真的退出。"""
        kernel32 = ctypes.windll.kernel32
        wait = self._desktop_close_wait_seconds()
        for pid in pids:
            try:
                pid_int = int(pid)
            except (TypeError, ValueError):
                continue
            for attempt in range(1, DESKTOP_KILL_ATTEMPTS + 1):
                # PROCESS_TERMINATE(0x0001)
                handle = kernel32.OpenProcess(0x0001, False, pid_int)
                if not handle:
                    # 进程已自行退出
                    break
                try:
                    kernel32.TerminateProcess(handle, 0)
                    logger.info(f'清理桌面客户端 PID={pid_int}')
                finally:
                    kernel32.CloseHandle(handle)
                # 杀完必须确认进程真的没了，被拒或正在退出都会让下一轮启动撞上残留
                if self._desktop_wait_pid_gone(pid_int, wait):
                    break
                logger.warning(f'桌面客户端 PID={pid_int} 强杀后仍存活，重试 '
                               f'({attempt}/{DESKTOP_KILL_ATTEMPTS})')
            else:
                logger.error(f'桌面客户端 PID={pid_int} 无法清理，可能需要手动结束进程')
        # 等窗口真的消失，避免残留窗口干扰下一轮的新窗口识别
        self._desktop_wait_closed(wait)

    def _desktop_wait_pid_gone(self, pid, wait: float) -> bool:
        """在 wait 秒内等待指定进程退出，返回是否已退出。"""
        deadline = time.time() + wait
        while True:
            if not self._desktop_pid_alive(pid):
                return True
            if time.time() >= deadline:
                return False
            time.sleep(DESKTOP_KILL_POLL_INTERVAL)

    def desktop_bind_pid(self, pid, hwnd=0) -> None:
        """把新 PID/HWND 绑定到实例，并尽量持久化到配置。"""
        self.root_handle = str(pid)
        if hwnd:
            self.root_handle_num = hwnd
        self.root_handle_title = DESKTOP_WINDOW_TITLES[0]
        self.is_desktop_window = True
        # 换了新 hwnd，截图句柄缓存必须同步失效，否则截图仍走上一个客户端的废句柄
        self._desktop_clear_handle_cache()
        # 新绑定的客户端刚启动，必然未登录，需先走 restart 登录流程
        self._desktop_login_done = False
        logger.info(f'Desktop client bound: PID={pid}, hwnd={hwnd}')
        try:
            config = self.config
            # 设备初始化阶段走 startup_normalize（同步 provisional 快照，避免 COLD 快照
            # 永久把 handle 报成待重启）；运行期（快照已冻结）以实例状态为准，仅持久化磁盘
            try:
                config.startup_normalize({("script", "device", "handle"): str(pid)})
            except RuntimeError:
                config.script.device.handle = str(pid)
                config.save()
        except Exception as e:
            logger.warning(f'持久化桌面 PID 到配置失败: {e}')

    def _desktop_send_enter(self, hwnd) -> None:
        """向窗口发送回车键，用于确认 MPay 账号登录弹窗的默认按钮"进入游戏"。

        窗口可能在两条消息之间被销毁（回车已生效），此时忽略异常即可。
        """
        try:
            SendMessage(hwnd, WM_KEYDOWN, VK_RETURN, 0)
            SendMessage(hwnd, WM_KEYUP, VK_RETURN, 0)
        except Exception as e:
            logger.info(f'Send Enter to window {hwnd} failed (window may be closed): {e}')

    def _desktop_pid_alive(self, pid) -> bool:
        """进程是否还活着。无法判定时按「还活着」返回，宁可多等一轮也不误报已关闭。

        不能只看 OpenProcess 是否成功：进程已退出但仍有内核对象引用时 OpenProcess
        照样返回句柄，必须再用 GetExitCodeProcess 看退出码是否还是 STILL_ACTIVE(259)。
        窗口枚举不能替代这里——强杀被拒时窗口可能已销毁而进程还在，只验窗口会误判。
        """
        try:
            pid_int = int(str(pid))
        except (TypeError, ValueError):
            return False
        kernel32 = ctypes.windll.kernel32
        # PROCESS_QUERY_LIMITED_INFORMATION(0x1000)，比 QUERY_INFORMATION 权限要求更低
        handle = kernel32.OpenProcess(0x1000, False, pid_int)
        if not handle:
            # 打不开通常就是进程已退出；权限不足时也走这里，交给窗口检查兜底
            return False
        try:
            code = ctypes.c_ulong()
            if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return True
            return code.value == 259  # STILL_ACTIVE
        finally:
            kernel32.CloseHandle(handle)

    def _desktop_wait_closed(self, wait: float) -> bool:
        """在 wait 秒内轮询等待桌面客户端窗口消失，返回是否已关闭。"""
        deadline = time.time() + wait
        while True:
            if not self.desktop_window_exists():
                return True
            if time.time() >= deadline:
                return False
            time.sleep(0.5)

    def _desktop_wait_released(self, pid, wait: float) -> bool:
        """在 wait 秒内等待客户端真正释放：窗口消失 **且** 进程退出。

        两个条件都要，缺一个都可能是假关闭：
        - 只验窗口：强杀被拒时窗口已销毁但进程还在，会漏掉残留进程
        - 只验进程：进程刚退出时窗口可能还在被系统回收，下一轮新窗口识别会撞上
        """
        deadline = time.time() + wait
        while True:
            if not self.desktop_window_exists() and not self._desktop_pid_alive(pid):
                return True
            if time.time() >= deadline:
                return False
            time.sleep(DESKTOP_KILL_POLL_INTERVAL)

    def desktop_stop_client(self) -> bool:
        """关闭桌面客户端：强杀进程并验证真的释放，返回是否确认关闭。

        客户端的退出确认框画在游戏窗口内部（不是独立顶层窗口），实测回车确认经常无效
        （进程不退出、主窗口只是被移到屏幕外），走 WM_CLOSE + 确认反而要多等几秒还未必
        关得掉，因此这里不发任何窗口消息，直接强杀进程。

        强杀不等于已关闭：TerminateProcess 可能因权限被拒（本机出现过 (5, '拒绝访问。')），
        进程也可能正在退出还没消失。所以每轮杀完都验证「窗口消失 且 进程退出」，没释放
        就再杀一轮，全部轮次用尽仍在则返回 False——调用方据此决定是重建还是交人工，
        绝不能发完 kill 指令就当成功返回。

        关闭后保留原 PID 在配置里；下次检测到该 PID 无效（客户端未运行）时，
        由启动/重拉链路重启客户端并把配置更新为重启后的新 PID。
        """
        # 客户端关闭后必然未登录，下次启动需重新走登录流程
        self._desktop_login_done = False
        # PID 要在清理句柄前取，后面的验证全靠它
        pid = getattr(self, 'root_handle', None) or self.config.script.device.handle
        hwnd = self.root_handle_num
        if (not hwnd or not IsWindow(hwnd)) and not self._desktop_pid_alive(pid):
            logger.info('Desktop client not running, skip stop')
            # 客户端已不在，解除强制亮屏，允许系统恢复正常的显示器电源管理
            desktop_keep_screen_on(False)
            self._reset_desktop_handle_state()
            # 窗口进程已没了也要收启动器：它无窗口，只看窗口永远发现不了
            self._desktop_kill_spawned_leftovers(pid)
            return True

        wait = self._desktop_close_wait_seconds()
        released = False
        for attempt in range(1, DESKTOP_KILL_ATTEMPTS + 1):
            logger.info(f'Stopping desktop client: force kill '
                        f'({attempt}/{DESKTOP_KILL_ATTEMPTS}, PID={pid})')
            self.desktop_force_kill()
            if self._desktop_wait_released(pid, wait):
                logger.info(f'Desktop client released (PID={pid})')
                # 客户端已确认关闭，解除强制亮屏，允许系统恢复正常的显示器电源管理
                desktop_keep_screen_on(False)
                released = True
                break
            logger.warning(f'Desktop client PID={pid} still present after {wait}s, retry kill')
        if not released:
            # 到这里客户端确实没关掉：句柄状态照常清零（它已不可信），但把失败如实报出去
            logger.error(f'Desktop client PID={pid} not released after '
                         f'{DESKTOP_KILL_ATTEMPTS} force kill attempts, manual cleanup needed')
        self._reset_desktop_handle_state()
        # 无论窗口进程是否关掉，本轮自己拉起的启动器都要一并收掉
        leftovers_cleared = self._desktop_kill_spawned_leftovers(pid)
        return released and leftovers_cleared

    def _desktop_kill_spawned_leftovers(self, bound_pid) -> bool:
        """清理本次自己拉起、但没被绑定的客户端进程，返回是否已全部清掉。

        onmyoji.exe 是启动器：Popen 起来后它派生真正的游戏窗口进程，自己继续存活且
        没有窗口（实测启动器 89 线程、42s CPU、主窗口句柄为 0）。OAS 靠枚举游戏窗口
        绑定，只会绑到窗口进程，关闭时若只杀它，启动器就成了无主残留——既占内存，
        也让下一轮启动的窗口识别多一个干扰源。这些 PID 由 launch_desktop_client
        在启动成功时留档到 _desktop_spawned_pids。
        """
        spawned = getattr(self, '_desktop_spawned_pids', None)
        if not spawned:
            return True
        try:
            bound = int(str(bound_pid))
        except (TypeError, ValueError):
            bound = None
        # 已绑定的那个由主流程负责，这里只收剩下的
        leftovers = {p for p in spawned if p != bound and self._desktop_pid_alive(p)}
        self._desktop_spawned_pids = set()
        if not leftovers:
            return True
        logger.info(f'清理本次启动残留的客户端进程（启动器）: {sorted(leftovers)}')
        self._desktop_kill_pids(leftovers)
        still = {p for p in leftovers if self._desktop_pid_alive(p)}
        if still:
            logger.error(f'客户端启动器进程无法清理: {sorted(still)}，可能需要手动结束')
            return False
        return True

    def _reset_desktop_handle_state(self) -> None:
        """清零桌面窗口句柄与截图句柄缓存。

        进程已杀，hwnd 随窗口销毁立即失效。必须清零，否则后续在同一个 device 对象
        生命周期内被唤醒的任务（如配置变更触发的即时调度）会跳过 Handle.__init__，
        直接拿这个废句柄去 GetClientRect，抛 (1400, '无效的窗口句柄') 搞崩整个进程。
        """
        self.root_handle_num = 0
        # 截图句柄缓存指向的也是刚被销毁的窗口，一并失效
        self._desktop_clear_handle_cache()

    def _desktop_close_wait_seconds(self) -> int:
        """关闭游戏等待时长（秒），读 config.script.optimization.close_game_wait_duration。"""
        try:
            t = self.config.script.optimization.close_game_wait_duration
            return t.hour * 3600 + t.minute * 60 + t.second
        except Exception:
            return 10

    def desktop_force_kill(self) -> None:
        """强杀桌面客户端进程（WM_CLOSE 超时未退出的兜底）。"""
        pid = getattr(self, 'root_handle', None) or self.config.script.device.handle
        try:
            pid_int = int(str(pid))
        except (TypeError, ValueError):
            logger.error(f'Invalid desktop PID: {pid}, skip force kill')
            return
        kernel32 = ctypes.windll.kernel32
        # PROCESS_TERMINATE(0x0001)
        handle = kernel32.OpenProcess(0x0001, False, pid_int)
        if not handle:
            # 进程已自行退出
            return
        try:
            kernel32.TerminateProcess(handle, 0)
            logger.info(f'Desktop client PID={pid_int} force terminated')
        finally:
            kernel32.CloseHandle(handle)

    def desktop_window_restore_if_minimized(self, wait: float = 3.0) -> bool:
        """桌面窗口被最小化（用户误操作）时还原并等待客户区恢复，返回是否发生过还原。

        还原成功后若客户区尺寸偏离目标（1280x720），一并重校准，保证识别 1:1。
        """
        if not getattr(self, 'is_desktop_window', False):
            return False
        hwnd = self.root_handle_num
        if not hwnd or not IsIconic(hwnd):
            return False
        logger.warning('Desktop client window is minimized, restoring')
        ShowWindow(hwnd, SW_RESTORE)
        deadline = time.time() + wait
        restored = False
        while time.time() < deadline:
            with dpi_awareness():
                rect = GetClientRect(hwnd)
            if (rect[2] - rect[0]) > 0 and (rect[3] - rect[1]) > 0:
                restored = True
                break
            time.sleep(0.2)
        if restored:
            self.desktop_window_set_size()
        return restored

    @staticmethod
    def all_windows() -> list:
        """
        获取桌面上的所有窗体

        :return:  类似这样['MuMu模拟器']
        """

        def enum_windows_callback(hwnd, windows):
            window_text = GetWindowText(hwnd)
            windows.append(window_text)

        windows = []
        EnumWindows(enum_windows_callback, windows)
        return windows

    @classmethod
    def auto_handle_title(cls, windows: list) -> str:
        """
        返回第一个找到的有模拟器的标题
        :param windows:
        :return:
        """
        if windows is None:
            logger.error("auto_handle_title: windows list is None")

        emu_list = []
        for window_title in windows:
            for item in Handle.emulator_list:
                if window_title.find(item) != -1:
                    emu_list.append(window_title)

        if not len(emu_list):
            logger.error('Can not find emulator handle, please check your emulator is running')
            return None

        emulator_title = ''
        # 测试mumu12的时候发现 获取的全部的窗体标题有这样的: 'MuMuPlayer', 'MuMuPlayer', 'MuMuPlayer', 'MuMu模拟器12'
        # 事实上 我们只需要最后一个 'MuMu模拟器12'，其他的不重要
        if 'MuMu模拟器12' in emu_list and 'MuMuPlayer' in emu_list:
            emulator_title = 'MuMu模拟器12'
        
        # MuMu5.0更新，窗体标题改动: 'MuMu模拟器','MuMuNxDevice','MuMu安卓设备'
        # 如果没有匹配上旧版本，尝试匹配MuMu5.0窗口名                                                                  
        if emulator_title == '' and 'MuMu安卓设备' in emu_list:
            emulator_title = 'MuMu安卓设备'

        if len(emu_list) > 1 and emulator_title == '':
            logger.warning(f'Find more than one emulator handle, oas will use the first one {emu_list[0]}')
            emulator_title = emu_list[0]

        if len(emu_list) == 1:
            emulator_title = emu_list[0]

        logger.info(f'Auto-detected emulator window: {emulator_title}')
        return emulator_title

    @staticmethod
    def handle_tree(hwnd, node: WindowNode, level: int = 0) -> None:
        """
        生成一个窗口的句柄树
        :param hwnd:
        :param node:
        :param level:
        :return:
        """
        child_windows = []
        EnumChildWindows(hwnd, lambda hwnd, param: param.append(hwnd), child_windows)

        if not child_windows:
            return
        for child_hwnd in child_windows:
            if GetParent(child_hwnd) == hwnd:
                child_text = GetWindowText(child_hwnd)
                child_node = WindowNode(name=child_text, num=child_hwnd, parent=node)

                # 递归遍历子窗体的子窗体
                Handle.handle_tree(child_hwnd, child_node, level + 1)
    @cached_property
    def emulator_family(self) -> EmulatorFamily:
        """
        通过句柄树来判断这个是那个模拟器大类
        :return:
        """
        children_num = len(self.root_node.children)
        if children_num == 1:  #
            if len(self.root_node.children) == 0:
                logger.error('No children found in root node for emulator detection')
                return EmulatorFamily.FAMILY_OTHER
            name = self.root_node.children[0].name
            if name == 'MuMuPlayer':
                return EmulatorFamily.FAMILY_MUMU
            elif name == 'NemuPlayer':
                return EmulatorFamily.FAMILY_MUMU
            elif name == 'TheRender':
                return EmulatorFamily.FAMILY_LD
            elif name == 'HD-Player':
                return EmulatorFamily.FAMILY_BLUESTACKS
        elif children_num >= 3:
            if len(self.root_node.children) == 0:
                logger.error('No children found in root node for emulator detection')
                return EmulatorFamily.FAMILY_OTHER
            name = self.root_node.children[0].name
            if name == 'Nox':
                return EmulatorFamily.FAMILY_NOX

        # 基于句柄标题的判定
        for emu in Handle.emulator_list:
            if self.root_handle_title.find(emu) != -1:
                if emu == 'MuMu':
                    return EmulatorFamily.FAMILY_MUMU
                elif emu == '雷电':
                    return EmulatorFamily.FAMILY_LD
                elif emu == '夜神':
                    return EmulatorFamily.FAMILY_NOX
                elif emu == '蓝叠':
                    return EmulatorFamily.FAMILY_BLUESTACKS
                elif emu == '逍遥':
                    return EmulatorFamily.FAMILY_MEMU
        return EmulatorFamily.FAMILY_OTHER
    """ @cached_property
    def emulator_family(self) -> EmulatorFamily:
        
        通过句柄树来判断这个是那个模拟器大类
        :return:
        
        children_num = len(self.root_node.children)
        if children_num == 1:  #
            name = self.root_node.children[0].name
            if name == 'MuMuPlayer':
                return EmulatorFamily.FAMILY_MUMU
            elif name == 'MuMuNxDevice':
                return EmulatorFamily.FAMILY_MUMU
            elif name == 'NemuPlayer':
                return EmulatorFamily.FAMILY_MUMU
            elif name == 'TheRender':
                return EmulatorFamily.FAMILY_LD
            elif name == 'HD-Player':
                return EmulatorFamily.FAMILY_BLUESTACKS
        elif children_num >= 3:
            name = self.root_node.children[0].name
            if name == 'Nox':
                return EmulatorFamily.FAMILY_NOX

        # 基于句柄标题的判定
        for emu in Handle.emulator_list:
            if self.root_handle_title.find(emu) != -1:
                if emu == 'MuMu':
                    return EmulatorFamily.FAMILY_MUMU
                elif emu == '雷电':
                    return EmulatorFamily.FAMILY_LD
                elif emu == '夜神':
                    return EmulatorFamily.FAMILY_NOX
                elif emu == '蓝叠':
                    return EmulatorFamily.FAMILY_BLUESTACKS
                elif emu == '逍遥':
                    return EmulatorFamily.FAMILY_MEMU
        return EmulatorFamily.FAMILY_OTHER """

    @cached_property
    def screenshot_handle_num(self) -> int:
        """
        截屏的句柄其实并不是根句柄
        :return:  出错返回None
        """
        if getattr(self, 'is_desktop_window', False):
            return self.root_handle_num
        if self.emulator_family == EmulatorFamily.FAMILY_MUMU:
            # 使用正则匹配12 来判定是不是mumu12这并不是一个好的方法
            if len(self.root_node.children) == 0:
                logger.error('No children found in root node for MuMu emulator')
                return self.root_node.num
            name = self.root_node.children[0].name
            num = self.root_node.children[0].num
            if name == 'MuMuPlayer':
                logger.info('The emulator is MuMu模拟器12')
                return num
            elif name == 'NemuPlayer':
                logger.info('The emulator is MuMu模拟器')
                return num
        # 夜神
        elif self.emulator_family == EmulatorFamily.FAMILY_NOX:
            try:
                if len(self.root_node.children) < 2 or len(self.root_node.children[1].children) < 2:
                    logger.error('Insufficient children nodes for Nox emulator')
                    return self.root_node.num
                return self.root_node.children[1].children[1].num
            except:
                if len(self.root_node.children) < 3 or len(self.root_node.children[2].children) < 2:
                    logger.error('Insufficient children nodes for Nox emulator')
                    return self.root_node.num
                return self.root_node.children[2].children[1].num

        elif self.emulator_family == EmulatorFamily.FAMILY_LD:
            for node in PreOrderIter(self.root_node):
                if node.name == Handle.emulator_handle['ld_player_family'][0]:
                    return node.num

        elif self.emulator_family == EmulatorFamily.FAMILY_MEMU:
            for node in PreOrderIter(self.root_node):
                if node.name == Handle.emulator_handle['memu_player_family']:
                    return node.num

        elif self.emulator_family == EmulatorFamily.FAMILY_BLUESTACKS:
            for node in PreOrderIter(self.root_node):
                if node.name == Handle.emulator_handle['bluestacks_family']:
                    return node.num
        return self.root_node.num
    """  @cached_property
    def screenshot_handle_num(self) -> int:
        
        截屏的句柄其实并不是根句柄
        :return:  出错返回None
        
        if self.emulator_family == EmulatorFamily.FAMILY_MUMU:
            # 使用正则匹配12 来判定是不是mumu12这并不是一个好的方法
            name = self.root_node.children[0].name
            num = self.root_node.children[0].num
            if name == 'MuMuPlayer':
                logger.info('The emulator is MuMu模拟器12')
                return num
            elif name == 'NemuPlayer':
                logger.info('The emulator is MuMu模拟器')
                return num
            elif name == 'MuMuNxDevice':
                logger.info('The emulator is MuMu模拟器5.0')
                return num
        # 夜神
        elif self.emulator_family == EmulatorFamily.FAMILY_NOX:
            try:
                return self.root_node.children[1].children[1].num
            except:
                return self.root_node.children[2].children[1].num

        elif self.emulator_family == EmulatorFamily.FAMILY_LD:
            for node in PreOrderIter(self.root_node):
                if node.name == Handle.emulator_handle['ld_player_family'][0]:
                    return node.num

        elif self.emulator_family == EmulatorFamily.FAMILY_MEMU:
            for node in PreOrderIter(self.root_node):
                if node.name == Handle.emulator_handle['memu_player_family']:
                    return node.num

        elif self.emulator_family == EmulatorFamily.FAMILY_BLUESTACKS:
            for node in PreOrderIter(self.root_node):
                if node.name == Handle.emulator_handle['bluestacks_family']:
                    return node.num
        return self.root_node.num """

    @cached_property
    def screenshot_size(self) -> tuple or None:
        """
        第一个是width 第二个是heigth
        2023.7.1 在高缩放的设备上应该输出1280X720
        :return:
        """
        if getattr(self, 'is_desktop_window', False):
            # 桌面模式固定输出 1280x720（物理目标，与资产 1:1）；
            # 实际位图由截图方法截取逻辑客户区后 resize 到该尺寸
            return 1280, 720
        winRect = GetWindowRect(self.screenshot_handle_num)
        scale_rate = window_scale_rate()
        width_before: int = winRect[2] - winRect[0]  # 右x-左x
        height_before: int = winRect[3] - winRect[1]  # 下y - 上y 计算高度
        width, height = width_before, height_before
        if abs((width_before * scale_rate) - 1280) < 5:
            width = 1280
        if abs((height_before * scale_rate) - 720) < 5:
            height = 720
        if width is None or height is None:
            logger.error(f'Get screenshot size error, width={width}, height={height}')
            return None
        return width, height

    @cached_property
    def window_scale_rate(self) -> float:
        """
        获取window的系统缩放 一般是1
        :return:
        """
        hDC = GetDC(0)
        # 物理上（真实的）的 横纵向分辨率
        wReal = GetDeviceCaps(hDC, DESKTOPHORZRES)
        hReal = GetDeviceCaps(hDC, DESKTOPVERTRES)
        # 缩放后的 分辨率
        wAfter = GetSystemMetrics(0)
        hAfter = GetSystemMetrics(1)
        # print(wReal, wAfter)
        return round(wReal / wAfter, 2)


    @classmethod
    def handle_has_children(cls, hwnd: int, name: str = 'MuMuPlayer12') -> bool:
        root_node = WindowNode(name=name, num=hwnd)
        Handle.handle_tree(hwnd=hwnd, node=root_node)
        handle_depth = WindowNode.get_tree_depth(root_node)
        if handle_depth > 1:
            logger.info(f'Window handle [{hwnd}] depth: {handle_depth}')
            return True
        return False


if __name__ == '__main__':
    h = Handle(config='oas1')
    # logger.info(h.auto_handle_title(h.all_windows()))
    # logger.info(h.root_handle_num)
    # logger.info(h.emulator_family)
