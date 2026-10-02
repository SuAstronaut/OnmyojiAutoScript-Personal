# This Python file uses the following encoding: utf-8
# @author runhey
# github https://github.com/runhey
import cv2
from numpy import frombuffer, uint8, array, random
import numpy as np
import time
from contextlib import nullcontext
from ctypes import windll

from math import dist
from cached_property import cached_property
from win32gui import (GetWindowText, EnumWindows, FindWindow, FindWindowEx,
                      IsWindow, GetWindowRect, GetWindowDC, DeleteObject,
                      SetForegroundWindow, IsWindowVisible, GetDC, GetParent,
                      EnumChildWindows, SetForegroundWindow, GetClientRect)
from win32con import (SRCCOPY, DESKTOPHORZRES, DESKTOPVERTRES, WM_LBUTTONUP,
                      WM_LBUTTONDOWN, WM_ACTIVATE, WA_ACTIVE, MK_LBUTTON,
                      WM_NCHITTEST, WM_SETCURSOR, HTCLIENT, WM_MOUSEMOVE,
                      WM_PARENTNOTIFY, WM_MOUSEACTIVATE, WM_MOUSEWHEEL,
                      WM_SETFOCUS, WM_CAPTURECHANGED,
                      WM_CHAR, WM_KEYDOWN, WM_KEYUP, VK_RETURN)
from win32ui import CreateDCFromHandle, CreateBitmap
from win32api import GetSystemMetrics, SendMessage, MAKELONG, PostMessage
from win32con import SRCCOPY


from module.base.cBezier import BezierTrajectory
from module.exception import RequestHumanTakeover, ScriptError
from module.base.decorator import Config
from module.base.timer import timer
from module.logger import logger
from module.device.handle import Handle, window_scale_rate, EmulatorFamily, dpi_awareness
from module.device.humanize.timing import PROFILE_MAX_POINTS




class Window(Handle):

    # 桌面模式鼠标移动：每 N 像素取一个轨迹点，并限制单次移动的最大点数。
    # 移动仅用于让客户端更新悬停状态，点越少越快；上限保证跨屏移动也不会拖慢。
    DESKTOP_MOVE_STEP = 60
    DESKTOP_MOVE_MAX_POINTS = 12
    # 桌面模式清空输入框时发送的退格次数，取昵称长度上限，空框时为无害空操作
    DESKTOP_CLEAR_BACKSPACE = 20

    def __init__(self, *args, **kwargs):
        logger.info("Window init")
        super().__init__(*args, **kwargs)

    @property
    def _humanizer(self):
        """拟人化门面（Device 绑定）。黄金基线桩或尚未绑定时返回 None，走原始旁路。"""
        return getattr(self, 'humanizer', None)

    def _desktop_move_budget_ms(self, start, end) -> float:
        """桌面指针移动的请求预算（Spec §5 D）：40~120ms × 人格速度缩放。

        facade 的 plan_move 按传入 budget_ms 生成 delays，不自行套公式，
        因此预算推导落在消费点（backend）——这正是契约 #10「消费点决定能力与预算」。
        humanizer 未启用时 plan_move 直接返回 None，本函数的返回值不被消费。
        """
        humanizer = self._humanizer
        d = dist(start, end)
        if humanizer is not None and humanizer.enabled:
            return min(max(d * 0.35, 40.0), 120.0) * humanizer.persona.move_speed_scale
        return min(max(d * 0.12, 15.0), 30.0)

    def screenshot_window_background(self):
        """
        后台截屏
        :return:
        """
        if getattr(self, 'is_desktop_window', False):
            return self.screenshot_desktop_bitblt()
        src_x, src_y = 0, 0
        widthScreen, heightScreen = self.screenshot_size
        # 返回句柄窗口的设备环境，覆盖整个窗口，包括非客户区，标题栏，菜单，边框
        hwndDc = GetWindowDC(self.screenshot_handle_num)
        # 创建设备描述表
        mfcDc = CreateDCFromHandle(hwndDc)
        # 创建内存设备描述表
        saveDc = mfcDc.CreateCompatibleDC()
        # 创建位图对象准备保存图片
        saveBitMap = CreateBitmap()
        # 为bitmap开辟存储空间
        saveBitMap.CreateCompatibleBitmap(mfcDc, widthScreen, heightScreen)
        # 将截图保存到saveBitMap中
        saveDc.SelectObject(saveBitMap)
        # 保存bitmap到内存设备描述表
        saveDc.BitBlt((0, 0), (widthScreen, heightScreen), mfcDc, (src_x, src_y), SRCCOPY)

        # 保存图像
        signedIntsArray = saveBitMap.GetBitmapBits(True)
        imgSrceen = frombuffer(signedIntsArray, dtype='uint8')
        imgSrceen.shape = (heightScreen, widthScreen, 4)
        # 这点很重要 在alas中图片以np.ndarray（RGB）的顺序存储。但是opencv是以BGR
        imgSrceen = cv2.cvtColor(imgSrceen, cv2.COLOR_BGR2RGB)
        # imgSrceen = cv2.resize(imgSrceen, (win_size[0], win_size[1]))
        # 很奇怪的

        # # 测试显示截图图片
        # cv2.namedWindow('imgSrceen')  # 命名窗口
        # cv2.imshow("imgSrceen", imgSrceen)  # 显示
        # cv2.waitKey(0)
        # cv2.destroyAllWindows()

        # 内存释放
        DeleteObject(saveBitMap.GetHandle())
        saveDc.DeleteDC()
        mfcDc.DeleteDC()
        return imgSrceen

    def screenshot_desktop_bitblt(self):
        """桌面客户端后台截图：DPI 感知上下文内对客户区 DC 做 BitBlt。

        客户端进程按物理像素原生渲染，感知上下文里 GetDC 拿到的就是物理客户区
        （1280x720），BitBlt 出的位图与资产 1:1，无需任何缩放。客户区 DC 原点即
        画面左上角，不含标题栏，因此不需要偏移。PrintWindow 对该 DirectX 渲染窗口
        全 flag 返回纯黑，故桌面模式统一走 BitBlt。
        """
        # 用户可能把桌面窗口最小化（误操作），先还原再截图；还原失败仍按原逻辑报错
        self.desktop_window_restore_if_minimized()
        with dpi_awareness():
            client_rect = GetClientRect(self.screenshot_handle_num)
            widthScreen = client_rect[2] - client_rect[0]
            heightScreen = client_rect[3] - client_rect[1]
            if widthScreen <= 0 or heightScreen <= 0:
                # 窗口最小化时客户区为 0x0，BitBlt 取不到任何内容。桌面模式允许窗口
                # 被其他窗口遮挡，但不能最小化（PrintWindow 虽支持最小化，却对该
                # DirectX 客户端返回纯黑，无法替代）。
                raise RequestHumanTakeover(
                    'Desktop client window is minimized, screenshot unavailable. '
                    '请恢复游戏窗口（可被其他窗口遮挡，但不能最小化）'
                )
            hwndDc = GetDC(self.screenshot_handle_num)
            mfcDc = CreateDCFromHandle(hwndDc)
            saveDc = mfcDc.CreateCompatibleDC()
            saveBitMap = CreateBitmap()
            saveBitMap.CreateCompatibleBitmap(mfcDc, widthScreen, heightScreen)
            saveDc.SelectObject(saveBitMap)
            saveDc.BitBlt((0, 0), (widthScreen, heightScreen), mfcDc, (0, 0), SRCCOPY)
            signedIntsArray = saveBitMap.GetBitmapBits(True)
            imgSrceen = frombuffer(signedIntsArray, dtype='uint8')
            imgSrceen.shape = (heightScreen, widthScreen, 4)
            imgSrceen = cv2.cvtColor(imgSrceen, cv2.COLOR_BGR2RGB)
            DeleteObject(saveBitMap.GetHandle())
            saveDc.DeleteDC()
            mfcDc.DeleteDC()
        # 窗口尺寸未能精确校准到目标时（如客户端锁定大小）兜底缩放，保证与资产同尺寸
        target_w, target_h = self.screenshot_size
        if (widthScreen, heightScreen) != (target_w, target_h):
            imgSrceen = cv2.resize(imgSrceen, (target_w, target_h))
        return imgSrceen

    def screenshot_printwindow(self):
        """PrintWindow 后台截图：遮挡/最小化也能捕获窗口内容，返回客户区画面。

        注意：对阴阳师桌面客户端这类 DirectX 渲染窗口，PrintWindow 全 flag 返回纯黑，
        桌面模式请用 screenshot_window_background（BitBlt）。
        """
        target_w, target_h = self.screenshot_size
        is_desktop = getattr(self, 'is_desktop_window', False)
        with dpi_awareness() if is_desktop else nullcontext():
            widthScreen, heightScreen = target_w, target_h
            if is_desktop:
                # 感知上下文内 GetClientRect 返回物理像素，位图按实际客户区开
                client_rect = GetClientRect(self.screenshot_handle_num)
                widthScreen, heightScreen = client_rect[2] - client_rect[0], client_rect[3] - client_rect[1]
            hwndDc = GetDC(self.screenshot_handle_num)
            mfcDc = CreateDCFromHandle(hwndDc)
            saveDc = mfcDc.CreateCompatibleDC()
            saveBitMap = CreateBitmap()
            saveBitMap.CreateCompatibleBitmap(mfcDc, widthScreen, heightScreen)
            saveDc.SelectObject(saveBitMap)
            # PW_CLIENTONLY(1)|PW_RENDERFULLCONTENT(2)=3：只渲染客户区且强制重绘
            windll.user32.PrintWindow(self.screenshot_handle_num, saveDc.GetSafeHdc(), 3)
            signedIntsArray = saveBitMap.GetBitmapBits(True)
            imgSrceen = frombuffer(signedIntsArray, dtype='uint8')
            imgSrceen.shape = (heightScreen, widthScreen, 4)
            imgSrceen = cv2.cvtColor(imgSrceen, cv2.COLOR_BGR2RGB)
            DeleteObject(saveBitMap.GetHandle())
            saveDc.DeleteDC()
            mfcDc.DeleteDC()
        if (widthScreen, heightScreen) != (target_w, target_h):
            imgSrceen = cv2.resize(imgSrceen, (target_w, target_h))
        return imgSrceen

    @cached_property
    def control_handle_list(self) -> list:
        """
        不同的模拟器需要的子句柄不同
        mumu模拟器的控制需要 根窗口和第一个子窗口
        雷电模拟器只需要TheRender窗口
        夜神模拟器
        :return:
        """
        result = []
        if getattr(self, 'is_desktop_window', False):
            # 桌面客户端：控制句柄即根窗口本身，无子渲染窗口
            return [self.root_handle_num]
        if self.emulator_family == EmulatorFamily.FAMILY_MUMU:
            result.append(self.root_node.num)
            result.append(self.root_node.children[0].num)
            return result
        elif self.emulator_family == EmulatorFamily.FAMILY_NOX:
            result.append(self.root_node.num)
            try:
                result.append(self.root_node.children[1].num)
                result.append(self.root_node.children[1].children[1].num)
                result.append(self.root_node.children[1].children[1].children[0].num)
            except:
                result.append(self.root_node.children[2].num)
                result.append(self.root_node.children[2].children[1].num)
                result.append(self.root_node.children[2].children[1].children[0].num)
            return result
        elif self.emulator_family == EmulatorFamily.FAMILY_LD:
            result.append(self.root_node.children[0].num)
            return result
        elif self.emulator_family == EmulatorFamily.FAMILY_MEMU:
            pass
        elif self.emulator_family == EmulatorFamily.FAMILY_BLUESTACKS:
            pass

    @cached_property
    def mumu_head_height(self):
        """
        不同mumu模拟器的头部高度不同
        :return:
        """
        father_win_Rect = GetWindowRect(self.control_handle_list[0])
        father_height: int = father_win_Rect[3] - father_win_Rect[1]  # 下y - 上y 计算高度
        children_win_Rect = GetWindowRect(self.control_handle_list[1])
        children_height: int = children_win_Rect[3] - children_win_Rect[1]  # 下y - 上y 计算高度
        height = father_height - children_height
        if int(height * self.window_scale_rate) == 37:
            # 说明是mumu模拟器 不做处理
            pass
        if int(height * self.window_scale_rate) == 45:
            # 说明是mumu12模拟器 不做处理
            pass
        logger.info(f"Mumu emulator head height: {height}")
        return height

    def click_window_message(self, x: int, y: int, fast: bool = False):
        """

        :param x:
        :param y:
        :param fast:
        :return:
        """
        if getattr(self, 'is_desktop_window', False):
            return self.click_desktop_window_message(x, y, fast)
        # 我不知道为什么的使用的pywin32==306的版本会导致获取的图片的是(1024, 576)
        # 所有我在点击的时候会除以这个缩放比例
        # 但是后面发现又不是影响的很奇怪

        # humanizer 使用 1280x720 authoring 坐标；Win32 消息坐标可能受 DPI 缩放。
        # 先在 authoring 画布生成并裁剪附加点，再逐点换算，避免缩放后仍按
        # 1280x720 裁剪而把 liftoff 发出真实客户区。
        target = (int(x), int(y))
        x = int(target[0] / self.window_scale_rate)
        y = int(target[1] / self.window_scale_rate)
        humanizer = self._humanizer
        # 维度 B：开档用拟人化按压时长；None（off/失败）回退原矩形均匀分布
        press = humanizer.press_seconds(fast=fast) if humanizer else None
        if press is not None:
            press_time = press
        elif fast:
            press_time: float = (random.randint(10, 40)) / 1000.0
        else:
            press_time: float = (random.randint(100, 200)) / 1000.0
        # 维度 F（触摸语义）：UP 前微位移。策略点保留 authoring 坐标，backend
        # 在发送时统一换算到消息空间；业务 UP 仍使用原目标换算值。
        # 模拟器点击只消费 B/F（契约 #10）：不接 plan_move/plan_dwell，medium/heavy
        # 对该点击动作没有新增维度，行为与耗时模型均同 light。
        liftoff = humanizer.plan_touch_liftoff(target) if humanizer else None
        emulator_type = len(self.control_handle_list)
        if emulator_type == 2:  # mumu模拟器
            SendMessage(self.control_handle_list[0], WM_ACTIVATE, WA_ACTIVE, 0)  # 激活窗口
            # SendMessage(self.control_handle_list[1], WM_ACTIVATE, WA_ACTIVE, 0)  # 激活窗口
            # SendMessage(self.control_handle_list[0], WM_LBUTTONDOWN, 0, MAKELONG(x, y+self.mumu_head_height))  # 模拟鼠标按下 先是父窗口 上面的框高度是57
            # mumu12模拟器 V3.5.16 之后后可以用下面的方式
            SendMessage(self.control_handle_list[1], WM_LBUTTONDOWN, 0, MAKELONG(x, y))
            time.sleep(press_time)
            if liftoff is not None:
                # 触摸 liftoff：delay 在发点前消费，且必须在 UP 前（模拟器路径原本
                # 没有任何收尾事件，这里是新增的 before-UP 微位移，不是搬移既有事件）
                for (px, py), dt in zip(liftoff.points, liftoff.delays):
                    time.sleep(dt)
                    px = int(px / self.window_scale_rate)
                    py = int(py / self.window_scale_rate)
                    PostMessage(self.control_handle_list[1], WM_MOUSEMOVE, MK_LBUTTON, MAKELONG(px, py))
            SendMessage(self.control_handle_list[1], WM_LBUTTONUP, 0, MAKELONG(x, y))  # 模拟鼠标弹起 后是子窗口
        elif emulator_type > 2:  # 夜神模拟器
            SendMessage(self.control_handle_list[0], WM_LBUTTONDOWN, 0, MAKELONG(x, y))  # 模拟鼠标按下 先是父窗口 上面的框高度是57
            SendMessage(self.control_handle_list[1], WM_LBUTTONDOWN, 0, MAKELONG(x, y))
            SendMessage(self.control_handle_list[2], WM_LBUTTONDOWN, 0, MAKELONG(x, y))
            SendMessage(self.control_handle_list[3], WM_LBUTTONDOWN, 0, MAKELONG(x, y))
            time.sleep(press_time)
            if liftoff is not None:
                for (px, py), dt in zip(liftoff.points, liftoff.delays):
                    time.sleep(dt)
                    px = int(px / self.window_scale_rate)
                    py = int(py / self.window_scale_rate)
                    PostMessage(self.control_handle_list[3], WM_MOUSEMOVE, MK_LBUTTON, MAKELONG(px, py))
            SendMessage(self.control_handle_list[3], WM_LBUTTONUP, 0, MAKELONG(x, y))  # 模拟鼠标弹起 后是子窗口
        elif emulator_type == 1:  # 雷电模拟器
            clickPos = MAKELONG(x, y)
            SendMessage(self.control_handle_list[0], WM_LBUTTONDOWN, 0, clickPos)
            time.sleep(press_time)
            if liftoff is not None:
                for (px, py), dt in zip(liftoff.points, liftoff.delays):
                    time.sleep(dt)
                    px = int(px / self.window_scale_rate)
                    py = int(py / self.window_scale_rate)
                    PostMessage(self.control_handle_list[0], WM_MOUSEMOVE, MK_LBUTTON, MAKELONG(px, py))
            SendMessage(self.control_handle_list[0], WM_LBUTTONUP, 0, clickPos)

    def desktop_message_coord(self, x, y) -> tuple:
        """把资产坐标（截图空间 1280x720）换算成窗口消息坐标。

        截图在 DPI 感知上下文内取得，是物理像素；而 PostMessage 由 DPI-unaware 的
        OAS 进程发出，lParam 会被系统按虚拟化（逻辑）空间解释后再交给目标窗口。
        两者相差系统缩放比，不换算会导致点击位置整体外扩（离左上角越远偏移越大）。
        """
        target_w, target_h = self.screenshot_size
        virtual_w, virtual_h = self.desktop_client_size_virtual()
        return int(round(x * virtual_w / target_w)), int(round(y * virtual_h / target_h))

    def click_desktop_window_message(self, x: int, y: int, fast: bool = False):
        """桌面客户端后台点击：先把鼠标沿轨迹移到目标点再按下，坐标为客户区（1280x720）。

        桌面客户端是鼠标语义（不同于模拟器的触摸协议），控件普遍依赖 hover 状态，
        直接在目标点发 down/up 会被部分控件忽略，因此必须带鼠标移动过程。
        """
        # 最小化的窗口收不到有效的后台点击，误操作导致最小化时先还原
        self.desktop_window_restore_if_minimized()
        hwnd = self.root_handle_num
        humanizer = self._humanizer
        # 维度 B：开档用拟人化按压时长；None（off/失败）回退原矩形均匀分布。
        # fast 只缩放中位数，下界仍为 45ms（Spec §5 B「fast 的语义」）
        press = humanizer.press_seconds(fast=fast) if humanizer else None
        if press is not None:
            press_time = press
        elif fast:
            press_time: float = (random.randint(10, 25)) / 1000.0
        else:
            press_time: float = (random.randint(30, 60)) / 1000.0
        # 预定位：move_desktop_window_message 内部消费 plan_move（pointer_move 语义），
        # medium 的 C 与 light 的 D 因此计入完整点击动作
        self.move_desktop_window_message(x, y)
        mx, my = self.desktop_message_coord(x, y)
        lparam = MAKELONG(mx, my)
        # 维度 E：到位停顿，仅桌面指针语义消费（触摸协议无此维度）。
        # DwellPlan 语义写死为"先发点、再等待"；None 段只等待，settle 段发
        # ±(1~2)px 的 WM_MOUSEMOVE 保持悬停刷新
        if humanizer is not None and humanizer.enabled:
            dwell = humanizer.plan_dwell((int(x), int(y)))
        else:
            dwell = None
        if dwell is not None:
            for point, sec in dwell.segments:
                if point is not None:
                    px, py = self.desktop_message_coord(*point)
                    PostMessage(hwnd, WM_MOUSEMOVE, 0, MAKELONG(px, py))
                time.sleep(sec)
        PostMessage(hwnd, WM_LBUTTONDOWN, MK_LBUTTON, lparam)
        time.sleep(press_time)
        PostMessage(hwnd, WM_LBUTTONUP, 0, lparam)
        SendMessage(hwnd, WM_CAPTURECHANGED, 0, 0)
        # 维度 F（指针语义）：UP 后漂移替换原有同坐标移动（hover 刷新）。
        # plan_pointer_tail 永不返回 None——"完全不补"会丢 hover，风险不值
        tail = humanizer.plan_pointer_tail((int(x), int(y))) if humanizer else None
        if tail is not None:
            for (px, py), dt in zip(tail.points, tail.delays):
                time.sleep(dt)
                qx, qy = self.desktop_message_coord(px, py)
                PostMessage(hwnd, WM_MOUSEMOVE, 0, MAKELONG(qx, qy))
            # tail 是最后实际发送的指针位置，后续移动与 idle 必须从这里开始。
            self._desktop_cursor = (int(tail.points[-1][0]), int(tail.points[-1][1]))
        else:
            # 抬起后补一次移动，让客户端刷新悬停状态
            PostMessage(hwnd, WM_MOUSEMOVE, 0, lparam)

    def move_desktop_window_message(self, x: int, y: int) -> None:
        """桌面客户端后台鼠标移动：从上一落点沿贝塞尔轨迹移动到 (x, y)。

        入参为资产坐标；维护 _desktop_cursor 记录当前鼠标位置（同为资产坐标），
        使连续操作之间的移动连贯；轨迹与模拟器路径同源（BezierTrajectory），
        避免直线等距的机器特征。发消息前统一换算到窗口消息坐标空间。

        移动只为让客户端更新悬停状态，无需逐像素铺满：轨迹按 DESKTOP_MOVE_STEP
        像素取点并整体限制在 DESKTOP_MOVE_MAX_POINTS 个以内，长距离移动的耗时
        因此与距离解耦，不会随屏幕跨度线性增长。
        """
        hwnd = self.root_handle_num
        start = getattr(self, '_desktop_cursor', None)
        target = (int(x), int(y))

        def post_move(px, py):
            mx, my = self.desktop_message_coord(px, py)
            PostMessage(hwnd, WM_MOUSEMOVE, 0, MAKELONG(mx, my))

        if start is None:
            # 首次操作没有历史位置，直接跳到目标点
            post_move(target[0], target[1])
            self._desktop_cursor = target
            return
        if start == target:
            post_move(target[0], target[1])
            return
        trace = self.desktop_trace(start, target, interval=self.DESKTOP_MOVE_STEP)
        # 点数超限时等间隔抽稀，保留轨迹形状但把消息量压到上限内
        if len(trace) > self.DESKTOP_MOVE_MAX_POINTS:
            step = len(trace) / self.DESKTOP_MOVE_MAX_POINTS
            trace = [trace[int(i * step)] for i in range(self.DESKTOP_MOVE_MAX_POINTS)]
        # 维度 D：把既有轨迹点交给 plan_move 生成新二维几何与速度剖面。
        # None（off/越界/失败）回退原裸循环（逐点连发）
        humanizer = self._humanizer
        if humanizer is not None:
            plan = humanizer.plan_move(
                start, target, gesture_kind='pointer_move',
                budget_ms=self._desktop_move_budget_ms(start, target))
            if plan is not None:
                # MovePlan.delays[i] 是发送 points[i] 前的等待；无 DOWN 的纯 pointer
                # move 中 delays[0] 是上一动作到第一点的前置反应延迟，不得跳过
                for (px, py), dt in zip(plan.points, plan.delays):
                    time.sleep(dt)
                    post_move(px, py)
                self._desktop_cursor = target
                return
        for px, py in trace:
            post_move(px, py)
        post_move(target[0], target[1])
        self._desktop_cursor = target

    def move_desktop_plan(self, plan) -> None:
        """把 MovePlan 逐点投递为桌面 WM_MOUSEMOVE（维度 G 空闲 / 计划直投）。

        供 Control.click 在 plan_idle 返回非 None 时调用：桌面指针语义下把空闲游移
        逐点投递并更新光标位置。delays[i] 在 points[i] 前消费；不产生 DOWN/UP，
        因此不会触发点击、截图或 control check。
        """
        hwnd = self.root_handle_num
        for (px, py), dt in zip(plan.points, plan.delays):
            time.sleep(dt)
            mx, my = self.desktop_message_coord(px, py)
            PostMessage(hwnd, WM_MOUSEMOVE, 0, MAKELONG(mx, my))
        self._desktop_cursor = (int(plan.points[-1][0]), int(plan.points[-1][1]))

    @staticmethod
    def desktop_trace(start_pos, end_pos, interval: int = 10) -> list:
        """生成 start_pos → end_pos 的贝塞尔轨迹点，与模拟器滑动同一套拟人参数。"""
        number_list: int = int(dist(start_pos, end_pos) / (1 * interval))
        if number_list < 1:
            return [tuple(end_pos)]
        # 贝塞尔曲线以 x 方向做参数化（t=(x-x0)/(x1-x0)），起点终点 x 相同时分母
        # 为 0 会产生 NaN，int(NaN) 崩溃；纯垂直移动退化为垂直直线插值
        if start_pos[0] == end_pos[0]:
            dy = end_pos[1] - start_pos[1]
            return [[int(start_pos[0]), int(start_pos[1] + dy * i / number_list)]
                    for i in range(1, number_list + 1)]
        le = random.randint(2, 4)
        deviation = random.randint(20, 40)
        # 0.8 概率先快中间慢后面快，0.1 先快后慢，0.1 先慢后快
        obbs_type = random.random()
        if 0 < obbs_type <= 0.8:
            b_type = 3
        elif obbs_type < 0.9:
            b_type = 2
        else:
            b_type = 1
        return BezierTrajectory.trackArray(start=list(start_pos), end=list(end_pos), numberList=number_list,
                                           le=le, deviation=deviation, bias=0.5, type=b_type, cbb=0, yhh=20)

    def _hold_jitter_moves(self, hwnd, x, y, duration: float, press_wparam: int) -> None:
        """长按 hold 期间发送微颤 MOVE 流（维度 J，2026-08-26 调研对标新增）。

        入参 x,y 必须是 authoring 坐标（1280×720 截图空间）——plan_hold 在
        facade 的 authoring 画布上生成微颤点，发送时由 _hold_jitter_coord
        单次换算到消息空间（与 click 的 liftoff 同模式，绝不双重缩放）。
        平台长按识别器留 8~10px 移动容差（iOS allowableMovement / Android touch
        slop）正说明真人按住期间手指持续微动；旧实现 hold 期间零事件（纯 sleep）
        是整秒级事件流死寂。plan_hold 返回 None（off/'none'/预算过短）时回退
        纯 sleep——注意 none 时可能已消费 hold 权重 RNG，但 sleep 不依赖 RNG。
        press_wparam：按住期间的 MOVE 用 MK_LBUTTON（模拟器语义，按键按住中），
        桌面指针语义用 0。python_sleep 通道 point_cap=50：wall-clock 由 Python
        sleep 驱动（精度 ~2.9ms/点 + 抖动），点数过多累积偏差会吃掉业务时长。
        """
        humanizer = self._humanizer
        is_desktop = getattr(self, 'is_desktop_window', False)
        plan = (humanizer.plan_hold((int(x), int(y)), float(duration), point_cap=50,
                                    mouse=is_desktop)
                if humanizer is not None else None)
        if plan is None:
            time.sleep(duration)
            return
        for (px, py), dt in zip(plan.points, plan.delays):
            # delays[i] 是发送 points[i] 前的等待（全局契约 4）
            time.sleep(dt)
            qx, qy = self._hold_jitter_coord(px, py)
            PostMessage(hwnd, WM_MOUSEMOVE, press_wparam, MAKELONG(qx, qy))

    def _hold_jitter_coord(self, x, y) -> tuple:
        """hold 微颤点的消息坐标换算；模拟器入口走 window_scale_rate，
        桌面入口走 desktop_message_coord（与各自 DOWN/UP 的换算同源）。"""
        if getattr(self, 'is_desktop_window', False):
            return self.desktop_message_coord(x, y)
        return int(x / self.window_scale_rate), int(y / self.window_scale_rate)

    def long_click_window_message(self, x: int, y: int, duration: float):
        """

        :param x:
        :param y:
        :param duration: 持续时间 单位秒
        :return:
        """
        if getattr(self, 'is_desktop_window', False):
            return self.long_click_desktop_window_message(x, y, duration)
        # 我不知道为什么的使用的pywin32==306的版本会导致获取的图片的是(1024, 576)
        # 所有我在点击的时候会除以这个缩放比例
        # hold 微颤的 target 用 authoring 坐标（缩放前）：plan_hold 在 authoring
        # 画布生成微颤点，发送时才单次换算（与 click 的 liftoff 同模式）
        hold_target = (int(x), int(y))
        x = int(x / self.window_scale_rate)
        y = int(y / self.window_scale_rate)

        emulator_type = len(self.control_handle_list)
        if self.emulator_family == EmulatorFamily.FAMILY_MUMU:  # mumu模拟器
            SendMessage(self.control_handle_list[1], WM_ACTIVATE, WA_ACTIVE, 0)  # 激活窗口
            # SendMessage(self.control_handle_list[0], WM_LBUTTONDOWN, 0, MAKELONG(x, y+self.mumu_head_height))  # 模拟鼠标按下 先是父窗口 上面的框高度是57
            SendMessage(self.control_handle_list[1], WM_LBUTTONDOWN, 0, MAKELONG(x, y))
            # 维度 J：hold 期间微颤 MOVE 流替换纯 sleep（None 时内部回退 sleep）
            self._hold_jitter_moves(self.control_handle_list[1], *hold_target, duration, MK_LBUTTON)
            SendMessage(self.control_handle_list[1], WM_LBUTTONUP, 0, MAKELONG(x, y))  # 模拟鼠标弹起 后是子窗口
        elif emulator_type > 2:  # 夜神模拟器
            SendMessage(self.control_handle_list[0], WM_LBUTTONDOWN, 0, MAKELONG(x, y))  # 模拟鼠标按下 先是父窗口 上面的框高度是57
            SendMessage(self.control_handle_list[1], WM_LBUTTONDOWN, 0, MAKELONG(x, y))
            SendMessage(self.control_handle_list[2], WM_LBUTTONDOWN, 0, MAKELONG(x, y))
            SendMessage(self.control_handle_list[3], WM_LBUTTONDOWN, 0, MAKELONG(x, y))
            self._hold_jitter_moves(self.control_handle_list[3], *hold_target, duration, MK_LBUTTON)
            SendMessage(self.control_handle_list[3], WM_LBUTTONUP, 0, MAKELONG(x, y))  # 模拟鼠标弹起 后是子窗口
        elif emulator_type == 1:  # 雷电模拟器
            clickPos = MAKELONG(x, y)
            SendMessage(self.control_handle_list[0], WM_LBUTTONDOWN, 0, clickPos)  # 模拟鼠标按下
            self._hold_jitter_moves(self.control_handle_list[0], *hold_target, duration, MK_LBUTTON)
            SendMessage(self.control_handle_list[0], WM_LBUTTONUP, 0, clickPos)  # 模拟鼠标弹起

    def long_click_desktop_window_message(self, x: int, y: int, duration: float):
        """桌面客户端后台长按：先沿轨迹移到目标点，按下保持 duration 秒后释放。"""
        # 最小化时后台长按同样不可靠，先还原窗口
        self.desktop_window_restore_if_minimized()
        hwnd = self.root_handle_num
        # 预定位：move_desktop_window_message 内部消费 plan_move（pointer_move 语义）
        self.move_desktop_window_message(x, y)
        lparam = MAKELONG(*self.desktop_message_coord(x, y))
        humanizer = self._humanizer
        # 维度 E：到位停顿，仅桌面指针语义消费。
        # 既有长按时长是业务参数，不由维度 B 重采样
        if humanizer is not None and humanizer.enabled:
            dwell = humanizer.plan_dwell((int(x), int(y)))
        else:
            dwell = None
        if dwell is not None:
            for point, sec in dwell.segments:
                if point is not None:
                    px, py = self.desktop_message_coord(*point)
                    PostMessage(hwnd, WM_MOUSEMOVE, 0, MAKELONG(px, py))
                time.sleep(sec)
        PostMessage(hwnd, WM_LBUTTONDOWN, MK_LBUTTON, lparam)
        # 维度 J：hold 期间微颤 MOVE 流替换纯 sleep（桌面指针语义，wparam=0）
        self._hold_jitter_moves(hwnd, int(x), int(y), duration, 0)
        PostMessage(hwnd, WM_LBUTTONUP, 0, lparam)
        SendMessage(hwnd, WM_CAPTURECHANGED, 0, 0)
        # 维度 F（指针语义）：UP 后漂移替换原有同坐标移动（hover 刷新）
        tail = humanizer.plan_pointer_tail((int(x), int(y))) if humanizer else None
        if tail is not None:
            for (px, py), dt in zip(tail.points, tail.delays):
                time.sleep(dt)
                qx, qy = self.desktop_message_coord(px, py)
                PostMessage(hwnd, WM_MOUSEMOVE, 0, MAKELONG(qx, qy))
            # 与点击路径一致，记录 after-UP tail 的真实末点。
            self._desktop_cursor = (int(tail.points[-1][0]), int(tail.points[-1][1]))
        else:
            # 抬起后补一次移动，让客户端刷新悬停状态
            PostMessage(hwnd, WM_MOUSEMOVE, 0, lparam)

    # @timer
    def swipe_window_message(self, startPos: list, endPos: list, duration=None) -> None:
        """
        后台滑动
        :param startPos:
        :param endPos:
        :param duration: 手势总时长（秒）。提供时均摊到轨迹各点（轨迹几何仍由
            interval 距离步长决定，时长与几何解耦）；None 保持默认每点 10ms，
            时长随距离伸缩
        :return:
        """
        if getattr(self, 'is_desktop_window', False):
            return self.swipe_desktop_window_message(startPos, endPos, duration=duration)
        # 生成的坐标点列表
        interval: int = 10  # 每次移动的间隔时间
        numberList: int = int(dist(startPos, endPos) / (1 * interval))  # 表示每毫秒移动1.5个像素点， 总的时间除以每个点10ms就得到总的点的个数
        le = random.randint(2, 4)  #
        deviation = random.randint(20, 40)  # 幅度
        _type: int = 3
        obbsType = random.random()  # 0.8的概率是先快中间慢后面快， 0.1概率是先快后慢， 0.1概率先慢后快
        if 0 < obbsType <= 0.8:
            _type = 3
        elif obbsType < 0.9:
            _type = 2
        else:
            _type = 1
        trace: list = BezierTrajectory.trackArray(start=startPos, end=endPos, numberList=numberList, le=le,
                                                  deviation=deviation, bias=0.5, type=_type, cbb=0, yhh=20)

        # 使用生成的点列表进行拖拽
        handleNum = None
        if self.emulator_family == EmulatorFamily.FAMILY_MUMU:  # mumu模拟器
            handleNum = self.control_handle_list[1]
        elif self.emulator_family == EmulatorFamily.FAMILY_NOX:  # 夜神模拟器
            handleNum = self.control_handle_list[3]
        elif self.emulator_family == EmulatorFamily.FAMILY_LD:  # 雷电模拟器
            handleNum = self.control_handle_list[0]

        # 激活窗口
        # SendMessage(self.control_handle_list[0], WM_PARENTNOTIFY, WM_LBUTTONDOWN, tmpPos)
        # SendMessage(self.control_handle_list[0], WM_MOUSEACTIVATE, WM_LBUTTONDOWN, tmpPos)
        # PostMessage(handleNum, WM_ACTIVATE, WA_ACTIVE, 0)

        humanizer = self._humanizer
        # 维度 D：手势主体走最终 plan_swipe（新几何 + 恒定回报率点流）。
        # None（off/失败）回退原逐点循环。维度 I：UP 前固定 sleep(0.05) 换成同均值抖动
        manual_control: int = 3  # 手动控制最后几个点的数量
        total_len: int = len(trace)
        # 每点基础延迟（秒）：duration 提供时均摊到轨迹各点——轨迹几何仍由 interval
        # 的距离步长决定，时长与几何解耦；None 保持默认每点 10ms（时长随距离伸缩）
        if duration is not None and total_len > 0:
            per_point_delay = duration / total_len
        else:
            per_point_delay = interval / 1000.0
        plan = None
        if humanizer is not None and humanizer.enabled:
            # 预算 = legacy 逐点延迟的总和。2026-09-13 之前这里为 light 档预生成
            # 整份 legacy_delays 再求和，而 profiled 路径从不消费那份随机数——纯粹
            # 白消费全局 RNG（成功后不回滚）。改用各点标称值直接算总和：抖动项
            # 均值为 0，在可达输入上与原期望相等（唯一的偏离是旧式非尾点
            # max(..., 0.001) 的 1ms 地板在 duration/total_len < 1.25ms 时抬高预算，
            # 而调用方 duration 至少 0.5s 或由 swipe_duration_by_dist 推导，
            # 到不了那个区间）；配套的 random.set_state 回滚一并省掉
            n_tail = min(manual_control, total_len)
            budget_s = (total_len - n_tail) * per_point_delay + n_tail * 0.08
            plan = humanizer.plan_swipe(
                tuple(startPos), tuple(endPos),
                # 不传时 facade 用固定 120ms，长滑动会被压快
                base_delay_s=budget_s / PROFILE_MAX_POINTS)
            # 维度 I：UP 前固定 sleep(0.05) 换成同均值抖动。只随主计划成功生效——
            # plan 失败（off/越界）时整体回退原旁路，连 gap 也保持原常量
            gap = humanizer.gap_seconds(0.05) if plan is not None else None
        else:
            gap = None
        final_gap = gap if gap is not None else 0.05

        # 只有计划实际生成成功时才换算 DPI 消息坐标；策略失败必须完整回退原始
        # legacy 轨迹，不能因为 enabled 仍然改写业务端点或中间点。
        scale_humanized = plan is not None

        def message_coord(x, y):
            if not scale_humanized:
                return int(x), int(y)
            return int(x / self.window_scale_rate), int(y / self.window_scale_rate)

        # 先移动到第一个点。off/legacy 必须保持原始消息坐标；只有 enabled 且
        # 计划成功时使用 authoring 坐标换算到 DPI 虚拟化客户区。
        tx, ty = message_coord(trace[0][0], trace[0][1])
        tmpPos = MAKELONG(tx, ty)
        SendMessage(handleNum, WM_NCHITTEST, 0, tmpPos)
        SendMessage(handleNum, WM_SETCURSOR, handleNum, MAKELONG(HTCLIENT, WM_LBUTTONDOWN))
        PostMessage(handleNum, WM_LBUTTONDOWN, 0, tmpPos)

        if plan is not None:
            # 最终计划逐点投递：delays[i] 在 points[i] 前消费；UP 后不消费计划 delay
            for (px, py), dt in zip(plan.points, plan.delays):
                time.sleep(dt)
                mx, my = message_coord(px, py)
                PostMessage(handleNum, WM_MOUSEMOVE, MK_LBUTTON, MAKELONG(mx, my))
            time.sleep(final_gap)
            ex, ey = message_coord(endPos[0], endPos[1])
            end_lparam = MAKELONG(ex, ey)
            PostMessage(handleNum, WM_LBUTTONUP, 0, end_lparam)
            return

        # 一点一点移动鼠标
        for index, pos in enumerate(trace):
            mx, my = message_coord(pos[0], pos[1])
            lparam = MAKELONG(mx, my)
            PostMessage(handleNum, WM_MOUSEMOVE, MK_LBUTTON, lparam)
            if manual_control >= total_len - index:
                time.sleep(0.08)
            elif duration is None:
                # duration 缺省路径保持原表达式（off 零回归：RNG 消费序列不变）
                time.sleep((interval + random.randint(-2, 2)) / 1000.0)
            else:
                # duration 提供时按均摊延迟走 ±20% 比例抖动，下限 1ms 防负 sleep
                time.sleep(max(per_point_delay + random.uniform(-per_point_delay * 0.2,
                                                                 per_point_delay * 0.2), 0.001))

        # 最后释放鼠标
        time.sleep(final_gap)
        ex, ey = message_coord(endPos[0], endPos[1])
        end_lparam = MAKELONG(ex, ey)
        PostMessage(handleNum, WM_LBUTTONUP, 0, end_lparam)

    def swipe_desktop_window_message(self, startPos: list, endPos: list, duration=None) -> None:
        """桌面客户端后台滑动：贝塞尔轨迹 PostMessage，与模拟器路径同一套拟人参数。

        duration 提供时为手势总时长（秒），均摊到轨迹各点——轨迹几何仍由
        desktop_trace 的距离步长决定，时长与几何解耦；None 保持默认每点 10ms。
        """
        # 最小化时后台滑动不可靠，先还原窗口
        self.desktop_window_restore_if_minimized()
        hwnd = self.root_handle_num
        interval: int = 10
        trace = self.desktop_trace(startPos, endPos, interval=interval)
        # 先把鼠标移到起点，再按下，避免客户端因缺少 hover 忽略按下事件
        # （预定位内部消费 plan_move，pointer_move 语义）
        self.move_desktop_window_message(startPos[0], startPos[1])
        start_lparam = MAKELONG(*self.desktop_message_coord(startPos[0], startPos[1]))
        PostMessage(hwnd, WM_LBUTTONDOWN, MK_LBUTTON, start_lparam)
        # 维度 D：手势主体走最终 plan_swipe（新几何 + 恒定回报率点流）。
        # None（off/失败）回退原逐点循环。维度 I：UP 前固定 sleep(0.05) 换同均值抖动
        humanizer = self._humanizer
        manual_control: int = 3
        total_len: int = len(trace)
        # 每点基础延迟（秒）：duration 提供时均摊到轨迹各点——轨迹几何仍由
        # desktop_trace 的距离步长决定，时长与几何解耦；None 保持默认每点 10ms
        if duration is not None and total_len > 0:
            per_point_delay = duration / total_len
        else:
            per_point_delay = interval / 1000.0
        plan = None
        if humanizer is not None and humanizer.enabled:
            # 预算 = legacy 逐点延迟的总和，同模拟器入口：改用各点标称值直接算
            # （抖动项均值为 0，可达输入上与原期望相等），不再为已删除的 light 档
            # 预生成整份 legacy_delays
            n_tail = min(manual_control, total_len)
            budget_s = (total_len - n_tail) * per_point_delay + n_tail * 0.08
            plan = humanizer.plan_swipe(
                tuple(startPos), tuple(endPos),
                # 桌面拖拽是鼠标语义，回报率走鼠标区间（125~1000Hz，python_sleep
                # 下被 clamp 到 200Hz）
                base_delay_s=budget_s / PROFILE_MAX_POINTS,
                mouse=True)
            # 维度 I：UP 前固定 sleep(0.05) 换成同均值抖动。只随主计划成功生效——
            # plan 失败（off/越界）时整体回退原旁路，连 gap 也保持原常量
            gap = humanizer.gap_seconds(0.05) if plan is not None else None
        else:
            gap = None
        final_gap = gap if gap is not None else 0.05
        if plan is not None:
            # 最终计划逐点投递：delays[i] 在 points[i] 前消费；UP 后不消费计划 delay
            for (px, py), dt in zip(plan.points, plan.delays):
                time.sleep(dt)
                lparam = MAKELONG(*self.desktop_message_coord(px, py))
                PostMessage(hwnd, WM_MOUSEMOVE, MK_LBUTTON, lparam)
            time.sleep(final_gap)
            end_lparam = MAKELONG(*self.desktop_message_coord(endPos[0], endPos[1]))
            PostMessage(hwnd, WM_LBUTTONUP, 0, end_lparam)
            SendMessage(hwnd, WM_CAPTURECHANGED, 0, 0)
            self._desktop_cursor = (int(endPos[0]), int(endPos[1]))
            return
        # 一点一点移动鼠标，最后几个点放慢，让客户端识别为拖拽而非瞬移
        for index, pos in enumerate(trace):
            lparam = MAKELONG(*self.desktop_message_coord(pos[0], pos[1]))
            PostMessage(hwnd, WM_MOUSEMOVE, MK_LBUTTON, lparam)
            if manual_control >= total_len - index:
                time.sleep(0.08)
            elif duration is None:
                # duration 缺省路径保持原表达式（off 零回归：RNG 消费序列不变）
                time.sleep((interval + random.randint(-2, 2)) / 1000.0)
            else:
                # duration 提供时按均摊延迟走 ±20% 比例抖动，下限 1ms 防负 sleep
                time.sleep(max(per_point_delay + random.uniform(-per_point_delay * 0.2,
                                                                 per_point_delay * 0.2), 0.001))
        time.sleep(final_gap)
        end_lparam = MAKELONG(*self.desktop_message_coord(endPos[0], endPos[1]))
        PostMessage(hwnd, WM_LBUTTONUP, 0, end_lparam)
        SendMessage(hwnd, WM_CAPTURECHANGED, 0, 0)
        self._desktop_cursor = (int(endPos[0]), int(endPos[1]))

    def input_text_desktop(self, text: str, clear: bool = False) -> None:
        """桌面客户端后台文本输入：向目标窗口逐字符注入键盘消息。

        桌面模式没有 adb/uiautomator2，只能通过 Windows 消息模拟键盘输入。
        文本统一只发 WM_CHAR（Unicode 码点，ASCII 与中文同路径）——若同时补发
        WM_KEYDOWN，客户端经 TranslateMessage 转译会重复生成字符；SendMessage
        同步直达窗口过程，不经消息队列，也不会被转译。clear=True 时先发若干次
        WM_CHAR(0x08) 退格清空输入框，对齐 uiautomator2 的 send_keys(clear=True)
        语义（不用 Ctrl+A：后台注入无法更新全局键状态，标准控件不识别 Ctrl+A，
        退格对空框无害、对已填内容可整框清空）。

        注意：客户端输入框是否在后台状态走标准 WM_CHAR 消息循环未知，需实测；
        若游戏不识别 WM_CHAR，可降级为剪贴板 + Ctrl+V 方案。
        """
        # 最小化时注入的消息可能被客户端丢弃，先还原窗口
        self.desktop_window_restore_if_minimized()
        hwnd = self.root_handle_num

        if clear:
            # 退格次数取昵称长度上限，空框时为无害空操作
            for _ in range(self.DESKTOP_CLEAR_BACKSPACE):
                SendMessage(hwnd, WM_CHAR, 0x08, 0)
            time.sleep(0.05)

        for char in text:
            code = ord(char)
            if code == 0x0A:
                # 换行
                SendMessage(hwnd, WM_KEYDOWN, VK_RETURN, 0)
                SendMessage(hwnd, WM_KEYUP, VK_RETURN, 0)
            else:
                # 字符一律只发 WM_CHAR，携带真实 Unicode 码点
                SendMessage(hwnd, WM_CHAR, code, 0)
            # 逐字符小间隔，避免消息过快被客户端丢弃或乱序
            time.sleep(0.03)

    def swipe_vector_window_message2(self, startPos: list, endPos: list) -> None:
        """
        后台滑动, 直线滑动
        :param startPos:
        :param endPos:
        :return:
        """
        # 生成的坐标点列表
        interval: int = 8  # 每次移动的间隔时间
        numberList: int = int(dist(startPos, endPos) / (1.5 * interval))  # 表示每毫秒移动1.5个像素点， 总的时间除以每个点10ms就得到总的点的个数

        def generate_points(start_pos: list, end_pos: list, number: int) -> list:
            # 确定两点之间的步长
            step_x = (end_pos[0] - start_pos[0]) / (number + 1)
            step_y = (end_pos[1] - start_pos[1]) / (number + 1)
            # 生成中间点坐标列表
            points = []
            for i in range(number):
                x = start_pos[0] + step_x * (i + 1)
                y = start_pos[1] + step_y * (i + 1)
                points.append((x, y))
            # 添加起始点和最终点到列表中
            points.insert(0, tuple(start_pos))
            points.append(tuple(end_pos))
            return points

        trace: list = generate_points(start_pos=startPos, end_pos=endPos, number=numberList)

        # 使用生成的点列表进行拖拽
        emulator_type = len(self.control_handle_list)
        if emulator_type == 1:  # 雷电模拟器
            handleNum = self.control_handle_list[0]
        elif emulator_type == 2:  # mumu模拟器
            handleNum = self.control_handle_list[1]
        elif emulator_type > 2:  # 夜神模拟器
            handleNum = self.control_handle_list[3]
        PostMessage(handleNum, WM_ACTIVATE, WA_ACTIVE, 0)  # 激活窗口
        # 先移动到第一个点
        tmpPos = MAKELONG(trace[0][0], trace[0][1])
        SendMessage(handleNum, WM_NCHITTEST, 0, tmpPos)
        SendMessage(handleNum, WM_SETCURSOR, handleNum, MAKELONG(HTCLIENT, WM_LBUTTONDOWN))
        PostMessage(handleNum, WM_LBUTTONDOWN, MK_LBUTTON, tmpPos)
        # 一点一点移动鼠标
        for pos in trace:
            tmpPos = MAKELONG(pos[0], pos[1])
            PostMessage(handleNum, WM_MOUSEMOVE, MK_LBUTTON, tmpPos)
            time.sleep((interval + random.randint(-2, 2)) / 1000.0)
        # 最后释放鼠标
        tmpPos = MAKELONG(trace[-1][0], trace[-1][1])
        PostMessage(handleNum, WM_LBUTTONUP, 0, tmpPos)

    def scroll_window_message(self, x: int, y: int, delta: int=-120) -> None:
        """
        弃置
        https://github.com/runhey/OnmyojiAutoScript/issues/43
        :param x:
        :param y:
        :param delta:
        :return:
        """
        wparam = MAKELONG(0, delta)
        lparam = MAKELONG(x, y)
        handle_num = None
        if self.emulator_family == EmulatorFamily.FAMILY_MUMU:  # mumu模拟器
            handle_num = self.control_handle_list[1]

        elif self.emulator_family == EmulatorFamily.FAMILY_NOX:  # 夜神模拟器
            handle_num = self.control_handle_list[3]
        elif self.emulator_family == EmulatorFamily.FAMILY_LD:  # 雷电模拟器
            handle_num = self.control_handle_list[0]

        SetForegroundWindow(handle_num)
        time.sleep(2)
        # SendMessage(handle_num, WM_SETFOCUS, 0, 0)
        # SendMessage(handle_num, WM_ACTIVATE, WA_ACTIVE, 0)
        # SendMessage(handle_num, WM_MOUSEACTIVATE, WM_LBUTTONDOWN, lparam)


        SendMessage(handle_num, WM_NCHITTEST, 0, lparam)
        SendMessage(handle_num, WM_SETCURSOR, handle_num, lparam)
        PostMessage(handle_num, WM_MOUSEMOVE, 0, lparam)
        self.click_window_message(x, y)

        for i in range(5):
            PostMessage(handle_num, WM_SETCURSOR, handle_num, lparam)
            PostMessage(handle_num, WM_MOUSEMOVE, 0, lparam)
            PostMessage(handle_num, WM_MOUSEWHEEL, wparam, lparam)
            time.sleep(0.5)
        # 抄网上的
        # SendMessage(handle_num, WM_NCHITTEST, 0, lparam)





if __name__ == "__main__":
    w = Window(config='oas1')
    # img = w.screenshot_window_background()
    # handle = 459852
    # wparam = MAKELONG(0, -120)
    # lparam = MAKELONG(300, 300)
    # PostMessage(handle, WM_MOUSEWHEEL, wparam, lparam)

    w.long_click_window_message(142, 310, 1.5)
