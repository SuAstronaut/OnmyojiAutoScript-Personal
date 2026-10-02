from enum import Enum
from typing import TYPE_CHECKING

import psutil

from module.device.platform2.emulator_windows import Emulator
from module.logger import logger

if TYPE_CHECKING:
    from module.device.device import Device


class HealthCriterion(Enum):
    PROCESS = "process"
    ADB = "adb"
    STATE = "state"
    CHANNEL = "channel"


STATE_UNAVAILABLE_REASON = 'MuMuManager state unavailable (non-MuMu12 or query failed)'


class EmulatorHealth:
    """
    模拟器健康判定 — 4 标准 AND 综合体检。

    Each sub-check returns (ok, reason). Sub-checks never raise; any exception
    is caught and converted to (False, "<reason>") so is_alive() is always
    terminable.

    is_alive() and why_dead() are implemented in Task 9.
    """

    def __init__(self, device: 'Device'):
        self.device = device
        self._last_failures: list = []  # populated by is_alive() in Task 9

    # ------------------------------------------------------------------
    # Sub-checks (Tasks 5-8)
    # ------------------------------------------------------------------

    def _process_check(self) -> tuple:
        """Process 存活检查：MuMu12 精确匹配，其他模拟器使用通用进程探测。"""
        instance = self.device.emulator_instance
        if instance is None:
            return False, 'emulator_instance is None'

        if instance != Emulator.MuMuPlayer12:
            try:
                if self.device._is_emulator_process_alive():
                    return True, 'generic emulator process alive'
                return False, 'generic emulator process dead'
            except Exception as e:
                return False, f'generic process check exception: {e}'

        target_name = getattr(instance, 'name', None)
        if not target_name:
            return False, 'instance.name unavailable'

        warned_unreadable = False
        for proc in psutil.process_iter(['pid', 'name', 'cmdline']):
            try:
                if proc.info['name'] != 'MuMuVMMHeadless.exe':
                    continue
                cmdline_str = ''
                cmdline_unreadable = False
                try:
                    cmdline_str = ' '.join(proc.info['cmdline'] or [])
                except psutil.AccessDenied:
                    cmdline_unreadable = True
                if f'--comment {target_name}' in cmdline_str:
                    return True, f'pid={proc.info["pid"]}'
                if cmdline_unreadable:
                    if not warned_unreadable:
                        logger.warning(
                            'psutil cmdline unavailable, falling back to name-only match'
                        )
                        warned_unreadable = True
                    return True, f'pid={proc.info["pid"]} (name-only fallback)'
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue

        # MuMu 新版（安卓 15 / nx_main）不再以 MuMuVMMHeadless.exe --comment 方式运行，
        # 严格扫描未命中时回退到窗口句柄优先的通用探测，避免把已运行的模拟器误判为死亡。
        try:
            if self.device._is_emulator_process_alive():
                return True, 'generic emulator process alive (fallback)'
        except Exception as e:
            return False, f'generic process fallback exception: {e}'
        return False, f'no MuMuVMMHeadless.exe with --comment {target_name}'

    def _adb_check(self) -> tuple:
        """ADB device state check: serial 在 adb devices 列表且 status == 'device'。"""
        try:
            serial = self.device.serial
            devices = self.device.list_device().select(serial=serial)
            if not devices:
                return False, f'{serial} not in adb devices list'
            adb_device = devices.first_or_none()
            if adb_device is None:
                return False, f'{serial} not found'
            if adb_device.status != 'device':
                return False, f'{serial} status={adb_device.status}'
            return True, f'{serial} device'
        except Exception as e:
            return False, f'adb check exception: {e}'

    def _state_check(self) -> tuple:
        """
        MuMuManager 状态优先；starting 滞后时用 shell 和游戏包就绪验证，保持与启动监视一致。

        Note: device._query_mumu12_state() returns None for non-MuMu12 emulators
        (LDPlayer / BlueStacks / etc). For those, this check returns False with
        a clear reason — callers should not require this criterion for non-MuMu12.
        Subprocess timeout is bumped to 10s (in platform_windows.py) so transient
        slowness doesn't false-fail health check.
        """
        try:
            state = self.device._query_mumu12_state()
        except Exception as e:
            return False, f'_query_mumu12_state raised: {e}'
        if state is None:
            return False, STATE_UNAVAILABLE_REASON
        player_state = state.get('player_state', '')
        if player_state != 'start_finished':
            # MuMuManager 可能仍显示 starting，而 Android 已能正常响应。
            # 与 emulator_start_watch 使用相同的实际可用性证据，避免刚启动成功就被再次强杀。
            # 仅放行启动中的滞后状态，停止中等状态仍判失败。
            if player_state == 'starting' and state.get('is_process_started'):
                try:
                    pong = self.device.adb_shell(['echo', 'pong'])
                    if str(pong).strip() == 'pong' and self.device.list_app_packages(show_log=False):
                        return True, 'starting (shell and game packages ready)'
                except Exception as e:
                    return False, f'player_state={player_state!r}, readiness probe failed: {e}'
            return False, f'player_state={player_state!r}'
        return True, 'start_finished'

    def _screenshot_channel_check(self) -> tuple:
        """
        截图通道检查：根据 config.screenshot_method 动态选择对应通道的探测方式。

        screenshot_method == 'auto'：benchmark 未完成，跳过该项（D14 fallback）。
        is_alive() 在 Task 9 实现时会把此项当作 True 处理。
        """
        method = self.device.config.script.device.screenshot_method
        # ScreenshotMethod 是 str Enum（如 ADB='ADB' 大写），直接与字面量比较会漏判，
        # 统一取 .value 后再做字符串判断。
        method = method.value if hasattr(method, 'value') else method

        if method == 'auto':
            logger.info('screenshot_method=auto, skipping channel check')
            return True, 'auto-skip'

        if method == 'nemu_ipc':
            try:
                _ = self.device.nemu_ipc  # 触发 cached_property
                return True, 'nemu_ipc connected'
            except Exception as e:
                return False, f'nemu_ipc connect failed: {e}'

        if method in ('uiautomator2', 'minitouch', 'scrcpy'):
            try:
                info = self.device.u2.device_info
                if info:
                    return True, f'u2 ok ({method})'
                return False, 'u2 device_info empty'
            except Exception as e:
                return False, f'u2 check failed: {e}'

        if method in ('adb', 'ADB', 'ADB_nc'):
            try:
                pong = self.device.adb_shell(['echo', 'pong'])
                if pong and 'pong' in str(pong):
                    return True, 'adb shell ok'
                return False, f'adb shell pong empty: {pong!r}'
            except Exception as e:
                return False, f'adb shell failed: {e}'

        if method == 'window_background':
            # 窗口直控截图：用配置的窗口句柄/标题判断目标窗口仍存在
            try:
                alive = self.device._is_configured_handle_alive()
                if alive is not None:
                    return alive, f'window handle alive: {alive}'
                return False, 'window_background requires configured handle'
            except Exception as e:
                return False, f'window_background check failed: {e}'

        return False, f'unknown screenshot_method: {method!r}'

    def is_alive(self) -> bool:
        """
        Failure details stored in _last_failures for why_dead().
        MuMuManager state unavailable is skipped; all other failed checks are fatal.
        """
        self._last_failures = []
        checks = [
            (HealthCriterion.PROCESS, self._process_check),
            (HealthCriterion.ADB, self._adb_check),
            (HealthCriterion.STATE, self._state_check),
            (HealthCriterion.CHANNEL, self._screenshot_channel_check),
        ]
        for criterion, fn in checks:
            try:
                ok, reason = fn()
            except Exception as e:
                ok, reason = False, f'unexpected exception: {e}'
            if not ok:
                if criterion == HealthCriterion.STATE and reason == STATE_UNAVAILABLE_REASON:
                    logger.info(f'Health check skipped: {criterion.value}={reason}')
                    continue
                self._last_failures.append((criterion, reason))
                logger.warning(f'Health check failed: {criterion.value}={reason}')
        return not self._last_failures

    def why_dead(self) -> str:
        """Return diagnostic string from most recent is_alive() call."""
        if not self._last_failures:
            return 'alive (no failures recorded)'
        parts = [f'{c.value}={r}' for c, r in self._last_failures]
        return '; '.join(parts)


if __name__ == '__main__':
    # Manual REPL smoke test. Usage: ./toolkit/python.exe -m module.device.emulator_health [config_name]
    import sys
    from module.config.config import Config
    from module.device.device import Device

    config_name = sys.argv[1] if len(sys.argv) > 1 else 'oas1'
    config = Config(config_name)
    device = Device(config)
    health = EmulatorHealth(device)
    for name, fn in [
        ('process', health._process_check),
        ('adb', health._adb_check),
        ('state', health._state_check),
        ('channel', health._screenshot_channel_check),
    ]:
        ok, reason = fn()
        print(f'{name}: ok={ok}, reason={reason}')
