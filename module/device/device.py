
from collections import deque
from datetime import datetime

# Patch pkg_resources before importing adbutils and uiautomator2
from module.device.pkg_resources import get_distribution
# Just avoid being removed by import optimization
_ = get_distribution

from module.device.env import IS_WINDOWS
from module.base.decorator import del_cached_property
from module.base.timer import Timer
from module.config.utils import get_server_next_update
from module.device.app_control import AppControl
from module.device.control import Control
from module.device.platform2 import Platform
from module.device.screenshot import Screenshot
from module.exception import (GameNotRunningError,
                              GameStuckError,
                              GameTooManyClickError,
                              RequestHumanTakeover,
                              EmulatorNotRunningError)
from module.logger import logger
from module.device.humanize import HumanizerContext, set_current_humanizer
import time
from enum import Enum


class EmulatorState(Enum):
    COLD = "cold"
    HEALTHY = "healthy"
    ZOMBIE = "zombie"


_VALID_TRANSITIONS = {
    EmulatorState.COLD: {EmulatorState.HEALTHY},
    EmulatorState.HEALTHY: {EmulatorState.ZOMBIE},
    EmulatorState.ZOMBIE: {EmulatorState.COLD},
}


class Device(Platform, Screenshot, Control, AppControl):
    _screen_size_checked = False
    detect_record = set()
    click_record = deque(maxlen=15)
    stuck_timer = Timer(60, count=60).start()
    stuck_timer_long = Timer(300, count=300).start()
    stuck_long_wait_list = ['BATTLE_STATUS_S', 'PAUSE', 'LOGIN_CHECK', 'PREPARE_BEFORE_BATTLE']
    retry_times :int = 0
    # 桌面客户端登录态：OAS 自动启动/重启客户端后为 False（需先走 restart 登录流程），
    # 登录成功由 desktop_mark_logged_in() 置 True，运行期发现 MPay 弹窗由
    # desktop_mark_logged_out() 置回 False；默认 True 表示假设已在游戏中（不强制登录）
    _desktop_login_done = True
    def __init__(self, *args, cancel_event=None, **kwargs):
        # cancel_event: 可选取消信号（threading.Event）。Web 标注采集线程断开时置位，
        # 用于在模拟器拉起链路的检查点尽快放弃拉起。默认 None 表示从不取消，行为与原先一致。
        self._cancel_event = cancel_event
        self.emulator_state = EmulatorState.COLD
        # 维度 G 空闲计时基准（Task 20）：每个 Device 实例独立持有、互不共享，
        # 保证多开实例的时间戳彼此隔离。放在 __init__ 最前面，四条初始化路径
        # （含 desktop 早返回分支）都会执行；首次点击时 since_last ≈ 0，低于
        # 2s 阈值 → 首击不游移。
        self._last_action_ts = None
        from module.device.emulator_health import EmulatorHealth
        self.health = EmulatorHealth(self)
        from module.device.emulator_reset import FullReset
        self.reset = FullReset(self)

        initialized = False
        emulator_down = False
        try:
            super().__init__(*args, **kwargs)
            initialized = True
            # 路径 1：首次 super 初始化成功后立即绑定拟人化上下文（须早于 Rule.coord()）
            self._ensure_humanizer_context()
        except EmulatorNotRunningError:
            emulator_down = True
            logger.warning('super().__init__ saw EmulatorNotRunningError — will run full_recovery')

        # 桌面客户端模式：无模拟器生命周期；窗口缺失或 PID 未绑定时自动启动客户端并绑定
        if self.is_desktop:
            if not self._desktop_ensure_launched():
                logger.critical('Desktop client window not found and auto-launch failed, '
                                'please start the game and bind PID first')
                raise RequestHumanTakeover from None
            self._init_desktop()
            # 路径 3：desktop 分支提前 return 前再保证一次绑定（不得只在函数尾部调用，
            # 否则 desktop 分支会漏绑）
            self._ensure_humanizer_context()
            return

        # Probe health first — if the user already has a working emulator,
        # don't disturb it. Only run full_recovery if unhealthy.
        if not emulator_down and self.health.is_alive():
            self._transition_to(EmulatorState.HEALTHY)
        else:
            if emulator_down:
                logger.warning('Initial emulator connection failed, run full_recovery directly')
            else:
                logger.warning(f'Initial health check failed: {self.health.why_dead()}')
            recovered = False
            for trial in range(3):
                if self.full_recovery():
                    recovered = True
                    break
                logger.warning(f'full_recovery attempt {trial + 1}/3 failed')
            if not recovered:
                logger.critical('Failed to bring emulator to HEALTHY after 3 attempts')
                raise RequestHumanTakeover from None

        if not initialized:
            super().__init__(*args, **kwargs)
            # 路径 2：恢复后的第二次 super 成功后立即绑定
            self._ensure_humanizer_context()

        # Auto-fill emulator info
        if IS_WINDOWS and self.config.script.device.emulatorinfo_type == 'auto':
            _ = self.emulator_instance

        self.screenshot_interval_set()

        # Auto-select the fastest screenshot method
        if self.config.script.device.screenshot_method == 'auto':
            self.run_simple_screenshot_benchmark()

        # 路径 4：普通初始化末尾再幂等保证一次（已在路径 1/2 绑定时为 no-op）
        self._ensure_humanizer_context()

    def _ensure_humanizer_context(self) -> None:
        # 幂等：配置就绪后的每一条成功初始化路径都会调用，已绑定或配置缺失时直接返回。
        # 绑定须发生在 tasks/base_task.py 调用 Rule.coord() 之前——只在 Control.click
        # 层绑定会错过落点采样（Spec §4.3）。
        if getattr(self, 'humanizer', None) is not None:
            return
        config = getattr(self, 'config', None)
        if config is None:
            return
        self.humanizer = HumanizerContext.from_config(config, canvas_size=(1280, 720))
        set_current_humanizer(self.humanizer)

    def _desktop_ensure_launched(self) -> bool:
        """桌面模式确保客户端已启动并绑定 PID：窗口缺失/PID 未绑定时自动启动。

        返回 True 表示客户端窗口可用（可能刚自动启动完成）；False 表示启动失败需人工接管。
        仅桌面模式调用，不影响模拟器流程。
        """
        if not self.config.script.device.handle or not self.desktop_window_exists():
            return self.launch_desktop_client()
        return True

    def desktop_mark_logged_in(self) -> None:
        """标记桌面客户端已完成登录（仅桌面模式由 Restart 登录流程成功后调用）。"""
        self._desktop_login_done = True

    def desktop_mark_logged_out(self) -> None:
        """标记桌面客户端已掉回未登录态（发现 MPay 弹窗时调用）。

        运行期客户端可能自己掉回 MPay 登录界面（掉线、账号被顶、客户端重登），此时
        窗口仍在、登录标记却还是上次登录成功留下的 True，app_is_running() 因此会说
        「游戏在运行」，任务被白白启动一次才在 ui_get_current_page 里发现问题。
        复位标记后 script.py 的任务前置检查就能直接拦下并转交 Restart。
        """
        self._desktop_login_done = False

    def _init_desktop(self) -> None:
        """桌面客户端模式初始化：跳过模拟器健康检查/full_recovery。"""
        logger.info('Desktop client mode: skip emulator health check and full recovery')
        self._transition_to(EmulatorState.HEALTHY)
        # 桌面模式固定用 BitBlt 后台截图 + 后台窗口输入
        # （PrintWindow 对客户端的 DirectX 渲染窗口全 flag 返回纯黑，不可用）
        # 通过 startup_normalize 只把声明路径合入 provisional COLD 快照，不依赖普通 save
        updates = {}
        if self.config.script.device.screenshot_method in ('auto', 'printwindow'):
            updates[("script", "device", "screenshot_method")] = 'window_background'
        if self.config.script.device.control_method != 'window_message':
            logger.warning(
                f'Desktop mode requires control_method=window_message, '
                f'current={self.config.script.device.control_method}, overriding'
            )
            updates[("script", "device", "control_method")] = 'window_message'
        if updates:
            self.config.startup_normalize(updates)
        self.screenshot_interval_set()
        # 检测并调整桌面客户端窗口客户区到 1280x720，保证识别 1:1。
        # 窗口存在性已由 __init__ 的 _desktop_ensure_launched 保证（device 对象要可用
        # 必须先绑定到窗口句柄），这里不再重复确保；运行期客户端掉了由 Restart 负责重拉
        self.desktop_window_set_size()

    def _transition_to(self, target: EmulatorState) -> None:
        """Transition emulator state with validation. Same state is a no-op."""
        from module.exception import ScriptError
        if target == self.emulator_state:
            return
        if target not in _VALID_TRANSITIONS[self.emulator_state]:
            raise ScriptError(
                f'Illegal emulator state transition: {self.emulator_state.name} → {target.name}'
            )
        logger.info(f'EmulatorState: {self.emulator_state.name} → {target.name}')
        self.emulator_state = target

    def full_recovery(self) -> bool:
        """
        ZOMBIE/COLD → HEALTHY recovery.

        恢复顺序：清理成功 → 启动 → 等待就绪 → 健康复检；监视或复检失败均最多重试两轮。
        清理失败时不继续启动，重试耗尽后清理残留；取消后不再启动或强杀模拟器。

        NOTE: this method MUST NOT call itself recursively (D14). Retries are
        bounded inside this method; callers still decide whether to run another
        full_recovery cycle after this method returns False.
        """
        logger.hr('Device full_recovery', level=1)

        # 桌面模式：只做窗口存在性检查，不 kill/重启客户端
        if self.is_desktop:
            if self.desktop_window_exists():
                self._transition_to(EmulatorState.HEALTHY)
                logger.info('Desktop mode: target window alive, healthy')
                return True
            logger.error('Desktop mode: target window not found, please start the game manually')
            return False

        instance = self._resolve_emulator_instance()
        if instance is None:
            logger.error('full_recovery: emulator instance not found')
            return False

        for attempt in range(2):
            # 取消检查点：标注采集线程断开后置位 cancel_event，此处尽快放弃拉起，
            # 不再执行新一轮 reset/start，也不杀用户已运行的模拟器进程。
            if self._is_cancelled():
                logger.info('full_recovery: cancelled before attempt, abort launch')
                return False
            # 每轮启动前都强杀一次，确保上轮 180s 超时后的 MuMu 残留被清掉。
            # 清理失败意味着旧进程仍可能存活，不能继续叠加启动或宣告 COLD。
            if not self.reset.execute():
                logger.error('full_recovery: reset failed, abort launch')
                return False
            if self.emulator_state != EmulatorState.COLD:
                self._transition_to(EmulatorState.COLD)

            # reset 可能耗时数秒，返回后再次检查，防止取消期间又拉起模拟器。
            if self._is_cancelled():
                return False
            if not self._emulator_function_wrapper(self._emulator_start):
                logger.warning(f'full_recovery: _emulator_start failed (attempt {attempt + 1}/2)')
                continue

            ready = self.emulator_start_watch()
            # 最后一轮收到取消信号也应直接退出，不能落入循环后的强杀清理。
            if self._is_cancelled():
                return False
            if ready:
                if not self.health.is_alive():
                    logger.warning(f'full_recovery: health check failed: {self.health.why_dead()}')
                    continue
                self._transition_to(EmulatorState.HEALTHY)
                logger.info('full_recovery: HEALTHY')
                return True

            logger.warning(f'full_recovery: emulator_start_watch returned False (attempt {attempt + 1}/2)')

        # 启动调用或健康复检期间也可能取消，最终清理前仍需确认。
        if self._is_cancelled():
            return False
        logger.warning('full_recovery: all attempts failed, kill emulator before returning False')
        # full_recovery 最终失败时再强杀一次，确保脚本进程退出前模拟器进程已被清理。
        self.reset.execute()
        return False

    def _resolve_emulator_instance(self):
        """查找模拟器实例，必要时重新发现"""
        if self.emulator_instance is not None:
            return self.emulator_instance

        logger.warning('Emulator instance not found, re-discovering...')
        del_cached_property(self, 'emulator_instance')
        del_cached_property(self, 'emulator_info')
        del_cached_property(self, 'all_emulator_instances')

        if self.emulator_instance is not None:
            logger.info(f'Re-discovered emulator instance: {self.emulator_instance}')
            return self.emulator_instance

        logger.critical(
            f'No emulator with serial "{self.serial}" found, '
            f'please set a correct serial'
        )
        return None

    def run_simple_screenshot_benchmark(self):
        """
        Perform a screenshot method benchmark, test 3 times on each method.
        The fastest one will be set into config.
        """
        logger.info('run_simple_screenshot_benchmark')
        # Check resolution first
        # self.resolution_check_uiautomator2()
        # Perform benchmark
        from module.daemon.benchmark import Benchmark
        bench = Benchmark(config=self.config, device=self)
        method = bench.run_simple_screenshot_benchmark()
        # startup_normalize 事务成功后由 session 合入 provisional 快照与 model/base
        self.config.startup_normalize({("script", "device", "screenshot_method"): method})

    def handle_night_commission(self, daily_trigger='21:00', threshold=30):
        """
        Args:
            daily_trigger (int): Time for commission refresh.
            threshold (int): Seconds around refresh time.

        Returns:
            bool: If handled.
        """
        update = get_server_next_update(daily_trigger=daily_trigger)
        now = datetime.now()
        diff = (update.timestamp() - now.timestamp()) % 86400
        if threshold < diff < 86400 - threshold:
            return False

        # if GET_MISSION.match(self.image, offset=True):
        #     logger.info('Night commission appear.')
        #     self.click(GET_MISSION)
        #     return True

        return False

    def screenshot(self):
        """
        Returns:
            np.ndarray:
        """
        # 全操作共享 CD 的预付等待（2026-08-27）：上一次操作挂起的间隔要求在
        # 截图前等满——截图是 appear_then_click 等决策模式的依据，等待发生在
        # 「看」之前既保住节奏语义，又保证决策画面新鲜：目标已消失（弹窗过期、
        # 结算画面关闭）时识别自然失败，不再产生按旧目标点击的过期点击
        if self._humanizer_enabled():
            self.humanizer.pace_view()
        self.stuck_record_check()

        # 桌面模式：窗口缺失说明客户端没在运行（空闲期被「Close emulator during wait」
        # 关掉，或客户端自己崩了）。这里只报告事实，不自己拉客户端——启动客户端、等登录
        # 弹窗、进游戏是 Restart 任务的职责。script.py 接住这个异常后 task_call('Restart')，
        # 由 Restart 走 app_start 完成整套启动流程，重拉逻辑因此只存在一处。
        # 直接截图会抛 (1400, '无效的窗口句柄') 搞崩整个进程，所以必须在截图前拦下。
        if self.is_desktop and not self.desktop_window_exists():
            raise GameNotRunningError('Desktop client window not found')

        try:
            super().screenshot()
        except RequestHumanTakeover as e:
            raise RequestHumanTakeover

        if self.handle_night_commission():
            super().screenshot()

        return self.image

    def release_during_wait(self):
        # 空闲时释放实际使用的截图通道，避免旧版配置字段让普通等待抛 AttributeError。
        if self.config.script.device.screenshot_method == 'scrcpy':
            self._scrcpy_server_stop()
        elif self.config.script.device.screenshot_method == 'nemu_ipc':
            self.nemu_ipc_release()

    def stuck_record_add(self, button):
        """
        当你要设置这个时候检测为长时间的时候，你需要在这里添加
        如果取消后，需要在`stuck_record_clear`中清除
        :param button:
        :return:
        """
        self.detect_record.add(str(button))
        logger.info(f'Add stuck record: {button}')

    def stuck_record_clear(self):
        self.detect_record = set()
        self.stuck_timer.reset()
        self.stuck_timer_long.reset()

    def stuck_record_check(self):
        """
        Raises:
            GameStuckError:
        """
        reached = self.stuck_timer.reached()
        reached_long = self.stuck_timer_long.reached()

        if not reached:
            return False
        if not reached_long:
            for button in self.stuck_long_wait_list:
                if button in self.detect_record:
                    return False

        logger.warning('Wait too long')
        logger.warning(f'Waiting for {self.detect_record}')
        self.stuck_record_clear()

        # 桌面模式的 app_is_running() 还带「已登录」判定，登录流程里它恒为 False，
        # 直接拿来判活会把「登录界面卡住」误报成「客户端死了」：实测登录卡满 5 分钟被
        # 报成 Game died，Restart 于是白杀一个其实还活着的客户端再重开。判活只看窗口
        alive = self.desktop_window_exists() if self.is_desktop else self.app_is_running()
        if alive:
            raise GameStuckError(f'Wait too long')
        else:
            raise GameNotRunningError('Game died')

    def handle_control_check(self, button):
        self.stuck_record_clear()
        self.click_record_add(button)
        self.click_record_check()

    def handle_swipe_control_check(self):
        """滑动前重置状态，但不把滑动计入连续点击保护。"""
        self.stuck_record_clear()
        self.click_record_clear()

    def click_record_add(self, button):
        self.click_record.append(str(button))

    def click_record_clear(self):
        self.click_record.clear()

    def click_record_remove(self, button):
        """
        Remove a button from `click_record`

        Args:
            button (Button):

        Returns:
            int: Number of button removed
        """
        removed = 0
        for _ in range(self.click_record.maxlen):
            try:
                self.click_record.remove(str(button))
                removed += 1
            except ValueError:
                # Value not in queue
                break

        return removed

    def click_record_check(self):
        """
        Raises:
            GameTooManyClickError:
        """
        count = {}
        for key in self.click_record:
            count[key] = count.get(key, 0) + 1
        count = sorted(count.items(), key=lambda item: item[1], reverse=True)
        if count[0][1] >= 12:
            logger.warning(f'Too many click for a button: {count[0][0]}')
            logger.warning(f'History click: {[str(prev) for prev in self.click_record]}')
            
            # 特殊处理sa_account_list_up，这是滑动列表的操作，不应该抛出异常
            if str(count[0][0]) == "sa_account_list_up":
                self.click_record_clear()
                return
            self.click_record_clear()
            if self.retry_times >=2:
                self.retry_times = 0
                raise GameTooManyClickError(f'Too many click for a button: {count[0][0]}')
            self.retry_times += 1
            time.sleep(10)
            return
        if len(count) >= 2 and count[0][1] >= 6 and count[1][1] >= 6:
            logger.warning(f'Too many click between 2 buttons: {count[0][0]}, {count[1][0]}')
            logger.warning(f'History click: {[str(prev) for prev in self.click_record]}')
            self.click_record_clear()
            if self.retry_times >= 5:
                self.retry_times = 0
                raise GameTooManyClickError(f'Too many click for a button: {count[0][0]}')
            self.retry_times += 1
            time.sleep(10)
            return

    def disable_stuck_detection(self):
        """
        Disable stuck detection and its handler. Usually uses in semi auto and debugging.
        """
        logger.info('Disable stuck detection')

        def empty_function(*arg, **kwargs):
            return False

        self.click_record_check = empty_function
        self.stuck_record_check = empty_function

    def app_start(self):
        # 桌面模式：窗口缺失时自动启动客户端并绑定 PID（空闲关闭后由 Restart 链路重新拉起）
        if self.is_desktop:
            if not self._desktop_ensure_launched():
                logger.error('Desktop client not running and auto-launch failed, '
                             'please start the game manually or check desktop_game_path')
                raise GameNotRunningError('Desktop client auto-launch failed')
            # 登录流程之前必须把客户区校准到 1280x720，否则 app_handle_login 的 OCR 与
            # 点击都落在未校准的窗口上、坐标全错。这里是拉起客户端的唯一入口，且必然
            # 早于 app_handle_login；运行期重拉时 Device 对象是复用的，_init_desktop
            # 不会再跑，所以校准不能只挂在初始化路径上
            self.desktop_window_set_size()
            return
        if not self.config.script.error.handle_error:
            logger.critical('No app stop/start, because HandleError disabled')
            logger.critical('Please enable Alas.Error.HandleError or manually login to AzurLane')
            raise RequestHumanTakeover
        super().app_start()
        self.stuck_record_clear()
        self.click_record_clear()

    def app_stop(self):
        # 桌面模式：关闭客户端并验证真的释放（窗口消失 且 进程退出），
        # 关不掉时 desktop_stop_client 已记 ERROR，这里把结论透出给调用方
        if self.is_desktop:
            if not self.desktop_stop_client():
                logger.warning('Desktop client not released, residual process may remain')
            return
        if not self.config.script.error.handle_error:
            logger.critical('No app stop/start, because HandleError disabled')
            logger.critical('Please enable Alas.Error.HandleError or manually login to AzurLane')
            raise RequestHumanTakeover
        super().app_stop()
        self.stuck_record_clear()
        self.click_record_clear()


if __name__ == "__main__":
    # 调试入口：Device 构造前后必须划定 COLD 启动边界，否则 serial_check 里的内部归一化
    # （中文冒号 serial / benchmark / emulatorinfo 回写）会因缺少 provisional 快照抛
    # RuntimeError。原来直接传配置名依赖 ConnectionAttr 内部构造 Config，拿不到实例做接线。
    from module.config.config import Config

    debug_config = Config("oas1", task=None)
    debug_config.begin_device_initialization()
    device = Device(config=debug_config)
    debug_config.freeze_startup_device_snapshot()
    # cv2.imshow("imgSrceen", device.screenshot())  # 显示
    # cv2.waitKey(0)
    # cv2.destroyAllWindows()
