# from module.base.button import Button
import time
from module.base.decorator import cached_property
from module.base.timer import Timer
from module.base.utils import *
# from module.device.method.hermit import Hermit
# from module.device.method.maatouch import MaaTouch
from module.device.env import IS_WINDOWS
from module.device.method.minitouch import Minitouch
from module.device.method.adb import Adb
from module.device.method.scrcpy import Scrcpy
from module.device.method.windows import Window
from module.device.method.nemu_ipc import NemuIpc
# 滑动时长距离推导（2026-09-06 实测）：duration=None 时按滑动距离取默认时长
from module.device.humanize.timing import swipe_duration_by_dist as timing_swipe_duration_by_dist
from module.logger import logger


class Control(Minitouch, Adb, Scrcpy, Window, NemuIpc):
    # NemuIpc 入基类：click/long_click/humanized 分派表在构造期就要解析
    # nemu_ipc 的实现方法；Device 侧经 Screenshot 也会拿到同一 NemuIpc，
    # MRO 自动去重无菱形冲突
    def handle_control_check(self, button):
        # Will be overridden in Device
        pass

    def handle_swipe_control_check(self):
        # 滑动不参与连续点击保护，由 Device 子类负责重置相关状态
        pass

    @cached_property
    def click_methods(self):
        return {
            'ADB': self.click_adb,
            'uiautomator2': self.click_uiautomator2,
            'minitouch': self.click_minitouch,
            # nemu_ipc：MuMu 官方 IPC 注入（设备侧零驻留进程），down/up 见
            # NemuIpcImpl；仅 MuMu 实例可用，坐标恒等透传
            'nemu_ipc': self.click_nemu_ipc,
            'window_message': self.click_window_message if IS_WINDOWS else None,
            # 'Hermit': self.click_hermit,
            # 'MaaTouch': self.click_maatouch,
        }

    def _humanizer_enabled(self):
        # off 档或 Device 未绑定 humanizer 时走原始 click_methods 分派（Plan 契约 1/11）
        context = getattr(self, 'humanizer', None)
        return bool(context is not None and context.enabled)

    def _desktop_pointer_ready(self):
        """维度 G 门控：桌面指针语义（is_desktop_window）且 humanizer 启用。

        模拟器 window_message / minitouch / uiautomator2 / ADB 是触摸协议，契约 #10
        明确禁止接入 G（即使逐点可控也不行）；off 档 humanizer 未启用时返回 False，
        不消费任何策略 RNG。
        """
        return bool(self._humanizer_enabled() and getattr(self, 'is_desktop_window', False))

    def _pace_action_before(self):
        """全操作共享 CD（2026-08-27）：输入操作的执行前兜底等待。

        预付制：正常流程的间隔要求已由 Device.screenshot 的 pace_view 等满
        （等待发生在「看」之前，决策画面新鲜），这里恒为 0；仅「无截图背靠背
        操作」的罕见场景（要求未被消费）补最多 EXECUTE_PACE_MAX_S，剩余留给
        下一个消费点。off 档 pace_execute 返回 0 且不消费 RNG（零回归契约）。

        必须与 _pace_action_after 配对调用。
        """
        if self._humanizer_enabled():
            self.humanizer.pace_execute()

    def _pace_action_after(self, target=None, name=None, roi=None,
                           repeat_exempt: bool = False):
        """全操作共享 CD 的收尾打点：操作完成后更新节奏统计与下次要求。

        name（点击控件名，如 GB_DE_WIN）传入时按名判同一资源（优先级高于
        坐标半径）；roi（点击时 Rule 的 roi_front，任务显式设置/静态定义的
        稳定区域）参与同名判重——同名但 ROI 不同是任务在复用 RuleClick
        遍历列表，判为新资源，任一方缺 ROI 时退化为仅按名判重；target
        （点击坐标）作无名点击的兜底；swipe/drag 两者皆无 → 重置重复计数。
        repeat_exempt（豁免同一资源连点退避，语义见 HumanizerContext.
        record_action）原样透传给打点。
        off 档无副作用。
        """
        if self._humanizer_enabled():
            self.humanizer.record_action(target, name, roi, repeat_exempt)

    def _maybe_deliver_idle(self):
        """把点击间空闲计划投递为桌面指针移动；无计划或光标未知时静默跳过。

        在 Control.click 选择具体 backend 之前调用（Task 20）：plan_idle 返回
        MovePlan 就经 move_desktop_plan 逐点投递为 WM_MOUSEMOVE——无 DOWN/UP，
        因此不会触发 click / screenshot / control check；返回 None（未达阈值 /
        光标未知 / 失败）则调用方原 click 原样执行。
        """
        if not self._desktop_pointer_ready():
            return
        # 首次点击没有历史时间戳时 since_last 记为 0（低于 2s 阈值 → 首击不游移）
        last = getattr(self, '_last_action_ts', None)
        since_last = 0.0 if last is None else max(0.0, time.time() - last)
        # cursor 取当前桌面光标；未知时 plan_idle 返回 None——凭空把光标从未知处
        # 拽到某坐标是引入新的可观测行为而不是拟人化（Spec §4.10）
        cursor = getattr(self, '_desktop_cursor', None)
        plan = self.humanizer.plan_idle(since_last, cursor)
        if plan is not None:
            self.move_desktop_plan(plan)

    @cached_property
    def humanized_click_methods(self):
        # enabled 时 Control.click 直达各 backend 的无装饰 humanized impl，
        # 绝不进入带 @retry 的公开方法（契约 11 的可达拓扑）。minitouch（Task 16）
        # 与 uiautomator2（Task 18）已接入；未接入的 backend 不在此表，click 走
        # click_methods 里的公开 @retry 方法（ADB 单条 shell 命令属 A 类，允许）。
        # nemu_ipc 的 down/up 是原子 IPC 调用（A 类），humanized impl 无 @retry。
        return {
            'minitouch': self._click_minitouch_humanized_impl,
            'uiautomator2': self._click_uiautomator2_humanized_impl,
            'nemu_ipc': self._click_nemu_ipc_humanized_impl,
        }

    @cached_property
    def humanized_swipe_methods(self):
        # enabled 时 Control.swipe 经 _dispatch_humanized_swipe 直达无装饰 humanized
        # impl；minitouch（Task 16）与 uiautomator2（Task 18）均已接入。
        return {
            'minitouch': self._swipe_minitouch_humanized_impl,
            'uiautomator2': self._swipe_uiautomator2_humanized_impl,
            'nemu_ipc': self._swipe_nemu_ipc_humanized_impl,
        }

    @cached_property
    def humanized_long_click_methods(self):
        # enabled 时 Control.long_click 直达各 backend 的无装饰 humanized 长按 impl
        # （维度 J hold 微颤）。未列出的 backend 走 long_click_methods 的公开
        # @retry 方法：window_message 的 humanized 路径在方法体内（0 个 @retry），
        # ADB 单条 shell 命令属 A 类（维度 B 已放弃的同理）。
        return {
            'minitouch': self._long_click_minitouch_humanized_impl,
            'uiautomator2': self._long_click_uiautomator2_humanized_impl,
            'nemu_ipc': self._long_click_nemu_ipc_humanized_impl,
        }

    @cached_property
    def long_click_methods(self):
        return {
            'ADB': self.long_click_adb,
            'uiautomator2': self.long_click_uiautomator2,
            'minitouch': self.long_click_minitouch,
            'nemu_ipc': self.long_click_nemu_ipc,
            'window_message': self.long_click_window_message if IS_WINDOWS else None,
            'scrcpy': self.long_click_scrcpy
            # 'Hermit': self.click_hermit,
            # 'MaaTouch': self.click_maatouch,
        }

    # def click(self, button, control_check=True):
    #     """
    #     后面改一改  不用用button的逻辑
    #     :param button:
    #     :param control_check:
    #     :return:
    #     """
    #     if control_check:
    #         self.handle_control_check(button)
    #     x, y = random_rectangle_point(button.button)
    #     x, y = ensure_int(x, y)
    #     logger.info(
    #         'Click %s @ %s' % (point2str(x, y), button)
    #     )
    #     method = self.click_methods.get(
    #         self.config.script.emulator.control_method,
    #         self.click_adb
    #     )
    #     method(x, y)

    def click(self, x: int, y: int, control_check=True, control_name='Click',
              pace: bool = True, control_roi=None,
              repeat_exempt: bool = False) -> None:
        """

        :param control_name:
        :param x:
        :param y:
        :param control_check:
        :param pace: False 表示本点击属于连点手势的追加击——绕过节奏等待
            （_pace_action_before 的兜底等待会让连点间隔远超人类范围）、不做
            点击间空闲游移（连点时手指不离目标附近）、不记节奏账（整次手势
            在首击处只记一次，追加击不污染节奏统计与同资源退避计数）。
            仅结算连点（GeneralBattle.settlement_gesture）使用，默认 True 原行为不变。
        :param control_roi: 点击时 Rule 的 roi_front（意图层稳定区域：静态定义
            或任务显式改写的点击区域），参与同名控件的同一资源判别——同名但
            ROI 不同（复用 RuleClick 遍历列表）判为新资源；缺省 None 退化为
            按名判重。仅 BaseTask 对 RuleClick/RuleLongClick 的点击传入，
            匹配结果驱动的区域（RuleImage/RuleOcr）不传（有抖动）。
        :param repeat_exempt: True 表示本次点击豁免拟人化的同一资源连点退避
            （打点时恒按首次点击计，不累计 1.5/1.5/2/2/4/10/16s 退避）——供
            业务语义就是「预期内连点」的资源在点击处显式声明，默认 False
            原行为不变。
        :return:
        """
        if control_check:
            self.handle_control_check(control_name)
        x, y = ensure_int(x, y)
        logger.info(
            'Click %s @ %s' % (point2str(x, y), control_name)
        )
        # 维度 G 点击间空闲（Task 20）：选择具体 backend 之前，桌面指针语义
        # 且 humanizer 启用时先做空闲游移（plan_idle → move_desktop_plan）。
        # 返回 None（off / 未达阈值 / 光标未知 / 失败）则下方原 click 原样执行。
        # 全操作共享 CD：执行前兜底（正常要求已由截图入口 pace_view 等满）。
        # 连点追加击（pace=False）跳过等待与游移，仍刷新空闲计时基准
        if pace:
            self._pace_action_before()
            self._maybe_deliver_idle()
        # 所有档位（含 off）都刷新空闲计时基准：off 跳过 plan_idle，不消费策略
        # RNG、不产生游移，但同样更新时间戳——保证开档的首次点击 since_last ≈ 0
        self._last_action_ts = time.time()
        # enabled 时优先走 humanized_click_methods 的无装饰 humanized impl（不触发
        # @retry 重放）；映射没有该 backend 时回退 click_methods 的公开方法——
        # window_message 的 humanized 路径在方法体内（0 个 @retry），ADB 单条 shell
        # 命令属 A 类。绝不能默认落到 click_adb，否则桌面 window_message 会被 ADB 抢占。
        control_method = self.config.script.device.control_method
        if self._humanizer_enabled():
            method = self.humanized_click_methods.get(control_method)
            if method is None:
                method = self.click_methods.get(control_method, self.click_adb)
        else:
            method = self.click_methods.get(control_method, self.click_adb)
        method(x, y)
        # 全操作共享 CD：操作结束打点（控件名+roi_front 优先判同一资源，
        # 坐标兜底）。连点追加击不打点：手势只在首击记一次节奏与退避
        if pace:
            self._pace_action_after((x, y), control_name, control_roi, repeat_exempt)


    def multi_click(self, button, n, interval=(0.1, 0.2)):
        """
        也是不能用button的逻辑
        :param button:
        :param n:
        :param interval:
        :return:
        """
        self.handle_control_check(button)
        click_timer = Timer(0.1)
        for _ in range(n):
            remain = ensure_time(interval) - click_timer.current()
            if remain > 0:
                self.sleep(remain)
            click_timer.reset()

            self.click(button, control_check=False)

    # def long_click(self, button, duration=(1, 1.2)):
    #     """
    #
    #     :param button:
    #     :param duration:
    #     :return:
    #     """
    #     self.handle_control_check(button)
    #     x, y = random_rectangle_point(button.button)
    #     x, y = ensure_int(x, y)
    #     duration = ensure_time(duration)
    #     logger.info(
    #         'Click %s @ %s, %s' % (point2str(x, y), button, duration)
    #     )
    #     method = self.config.script.emulator.control_method
    #     if method == 'minitouch':
    #         self.long_click_minitouch(x, y, duration)
    #     elif method == 'window_message':
    #         self.long_click_window_message(x, y, duration)
    #     elif method == 'uiautomator2':
    #         self.long_click_uiautomator2(x, y, duration)
    #     elif method == 'scrcpy':
    #         self.long_click_scrcpy(x, y, duration)
    #     # elif method == 'MaaTouch':
    #     #     self.long_click_maatouch(x, y, duration)
    #     else:
    #         self.swipe_adb((x, y), (x, y), duration)

    def long_click(self, x: int, y: int, duration=(0.5, 2), control_name='LongClick',
                   control_roi=None) -> None:
        """

        :param control_name:
        :param control_roi: 点击时 Rule 的 roi_front，与 click 同语义——参与
            同名控件的同一资源判别，缺省 None 退化为按名判重
        :param x:
        :param y:
        :param duration: 单位是s
        :return:
        """
        self.handle_control_check(control_name)
        x, y = ensure_int(x, y)
        if duration is None:
            duration = 0.8
        duration = ensure_time(duration)
        logger.info(
            'Click %s @ %s %s' % (point2str(x, y), control_name, duration)
        )
        # enabled 时优先走 humanized_long_click_methods 的无装饰 humanized impl
        # （维度 J hold 微颤，不触发 @retry 重放）；映射没有该 backend 时回退
        # long_click_methods 的公开方法——与 click 的 dispatch 拓扑一致。
        # 全操作共享 CD：执行前兜底（正常要求已由截图入口 pace_view 等满）
        self._pace_action_before()
        control_method = self.config.script.device.control_method
        if self._humanizer_enabled():
            method = self.humanized_long_click_methods.get(control_method)
            if method is None:
                method = self.long_click_methods.get(control_method, self.long_click_adb)
        else:
            method = self.long_click_methods.get(control_method, self.long_click_adb)
        method(x, y, duration)
        # 操作结束打点（控件名+roi_front 优先判同一资源，坐标兜底）
        self._pace_action_after((x, y), control_name, control_roi)

    def swipe(self, p1, p2, duration=None, control_name='SWIPE', distance_check=True):
        # 滑动动作持续时间较长，不参与连续点击保护，避免合法的连续翻页被误判为卡死
        # duration=None（含默认）走距离推导（2026-09-06 实测：100~300px→200~350ms、
        # >500px→500~700ms，见 timing.swipe_duration_by_dist）；显式传参的任务保持原值。
        self.handle_swipe_control_check()
        p1, p2 = ensure_int(p1, p2)
        if duration is None:
            duration = timing_swipe_duration_by_dist(
                float(np.hypot(p2[0] - p1[0], p2[1] - p1[1])))
        duration = ensure_time(duration)
        method = self.config.script.device.control_method
        if method == 'minitouch':
            logger.info('minitouch Swipe %s -> %s, %s ' % (point2str(*p1), point2str(*p2), duration))
        elif method == 'window_message':
            logger.info('Swipe %s -> %s' % (point2str(*p1), point2str(*p2)))
        elif method == 'uiautomator2':
            logger.info('Swipe %s -> %s, %s' % (point2str(*p1), point2str(*p2), duration))
        elif method == 'nemu_ipc':
            # nemu 逐点 down 支持任意节奏，无需 ADB 的 2.5 倍减速
            logger.info('Swipe %s -> %s, %s' % (point2str(*p1), point2str(*p2), duration))
        elif method == 'scrcpy':
            logger.info('Swipe %s -> %s' % (point2str(*p1), point2str(*p2)))
        # elif method == 'MaaTouch':
        #     logger.info('Swipe %s -> %s' % (point2str(*p1), point2str(*p2)))
        else:
            # ADB needs to be slow, or swipe doesn't work
            duration *= 2.5
            logger.info('Swipe %s -> %s, %s ' % (point2str(*p1), point2str(*p2), duration))

        if distance_check:
            if p1[0] == p2[0]:
                logger.info('Swipe x distance is 0')
                p1[0] += 1
            if p1[1] == p2[1]:
                logger.info('Swipe y distance is 0')
                p1[1] += 1

            if np.linalg.norm(np.subtract(p1, p2)) < 10:
                # Should swipe a certain distance, otherwise AL will treat it as click.
                # uiautomator2 should >= 6px, minitouch should >= 5px
                logger.info('Swipe distance < 10px, dropped')
                return

        # 全操作共享 CD：放在 distance_check 早退之后——dropped 的滑动没有
        # 输入事件发生，不等待也不打点，不污染节奏统计
        self._pace_action_before()

        # enabled 且 backend 已接入时唯一早返回：直达无装饰 humanized impl，
        # 不进入下方带 @retry 的公开方法；off 时返回 False 继续原有分支。
        if self._dispatch_humanized_swipe(method, p1, p2, duration):
            self._pace_action_after()
            return

        if method == 'minitouch':
            self.swipe_minitouch(p1, p2, duration=duration)
        elif method == 'window_message':
            # window_message 同样接收 duration：手势总时长预算（几何不变，
            # 均摊到轨迹各点），None 时保持每点 10ms 的距离隐式时长
            self.swipe_window_message(p1, p2, duration=duration)
        elif method == 'uiautomator2':
            self.swipe_uiautomator2(p1, p2, duration=duration)
        elif method == 'nemu_ipc':
            # nemu 逐点 down 由内核解释为 MOVE，时长由每点 sleep 决定，直接透传 duration
            self.swipe_nemu_ipc(p1, p2, duration=duration)
        elif method == 'scrcpy':
            self.swipe_scrcpy(p1, p2)
        # elif method == 'MaaTouch':
        #     self.swipe_maatouch(p1, p2)
        else:
            self.swipe_adb(p1, p2, duration=duration)
        self._pace_action_after()

    def _dispatch_humanized_swipe(self, method, p1, p2, duration):
        # off / 未绑定 humanizer / backend 未接入时返回 False，swipe() 走原分支；
        # enabled 且映射存在时直达无装饰 humanized impl 并返回 True。
        if not self._humanizer_enabled():
            return False
        humanized_method = self.humanized_swipe_methods.get(method)
        if humanized_method is None:
            return False
        humanized_method(p1, p2, duration=duration)
        return True

    def swipe_vector(self, vector, box=(123, 159, 1175, 628), random_range=(0, 0, 0, 0), padding=15,
                     duration=(1.0, 1.5), whitelist_area=None, blacklist_area=None, name='SWIPE', distance_check=True):
        """Method to swipe.

        Args:
            box (tuple): Swipe in box (upper_left_x, upper_left_y, bottom_right_x, bottom_right_y).
            vector (tuple): (x, y).
            random_range (tuple): (x_min, y_min, x_max, y_max).
            padding (int):
            duration (int, float, tuple):
            whitelist_area: (list[tuple[int]]):
                A list of area that safe to click. Swipe path will end there.
            blacklist_area: (list[tuple[int]]):
                If none of the whitelist_area satisfies current vector, blacklist_area will be used.
                Delete random path that ends in any blacklist_area.
            name (str): Swipe name
            distance_check: (bool):
        """
        p1, p2 = random_rectangle_vector_opted(
            vector,
            box=box,
            random_range=random_range,
            padding=padding,
            whitelist_area=whitelist_area,
            blacklist_area=blacklist_area
        )
        self.swipe(p1, p2, duration=duration, name=name, distance_check=distance_check)

    def drag(self, p1, p2, segments=1, shake=(0, 15), point_random=(-10, -10, 10, 10), shake_random=(-5, -5, 5, 5),
             swipe_duration=0.25, shake_duration=0.1, name='DRAG'):
        self.handle_control_check(name)
        p1, p2 = ensure_int(p1, p2)
        logger.info(
            'Drag %s -> %s' % (point2str(*p1), point2str(*p2))
        )
        method = self.config.script.emulator.control_method
        # 全操作共享 CD：drag 与 click/long_click/swipe 共享同一份节奏状态
        self._pace_action_before()
        if method == 'minitouch':
            self.drag_minitouch(p1, p2, point_random=point_random)
        elif method == 'uiautomator2':
            self.drag_uiautomator2(
                p1, p2, segments=segments, shake=shake, point_random=point_random, shake_random=shake_random,
                swipe_duration=swipe_duration, shake_duration=shake_duration)
        elif method == 'nemu_ipc':
            self.drag_nemu_ipc(p1, p2, point_random=point_random)
        elif method == 'scrcpy':
            self.drag_scrcpy(p1, p2, point_random=point_random)
        # elif method == 'MaaTouch':
        #     self.drag_maatouch(p1, p2, point_random=point_random)
        else:
            logger.warning(f'Control method {method} does not support drag well, '
                           f'falling back to ADB swipe may cause unexpected behaviour')
            self.swipe_adb(p1, p2, duration=ensure_time(swipe_duration * 2))
            # self.click(Button(area=(), color=(), button=area_offset(point_random, p2), name=name))
        self._pace_action_after()
