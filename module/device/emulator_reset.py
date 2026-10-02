"""
FullReset: 模拟器完整重置流程。

按 Layer 3 → 2 → 1 → 0 顺序拆除资源：
- Layer 3: 截图通道（nemu_ipc dll handle, scrcpy server, droidcast）
- Layer 2: ADB 连接
- Layer 1: 模拟器进程（2 档强杀）
- Layer 0: Device 实例级 cached_property（**不动**类属性）

类属性 detect_record / click_record / stuck_timer / stuck_timer_long 承载
跨 reset 累积的卡死检测语义，FullReset 永不重置（D10）。
"""

import os
import subprocess
import time
from typing import TYPE_CHECKING

import psutil

from module.base.decorator import del_cached_property
from module.device.platform2.emulator_windows import Emulator
from module.logger import logger

if TYPE_CHECKING:
    from module.device.device import Device


class FullReset:
    """
    模拟器完整重置 — 4 层串行拆除。

    Layer 3-2-0 是 best-effort；Layer 1（进程）是关键路径，2 档强杀
    任何一档成功即返回 True。
    """

    def __init__(self, device: 'Device'):
        self.device = device

    # ------------------------------------------------------------------
    # Entry point
    # ------------------------------------------------------------------

    def execute(self) -> bool:
        """按 Layer 3 → 2 → 1 → 0 顺序拆除。返回 Layer 1 是否成功。"""
        logger.hr('FullReset start', level=2)
        self._teardown_layer3_screenshot_channels()
        self._teardown_layer2_adb()
        layer1_ok = self._teardown_layer1_process()
        self._teardown_layer0_cached_properties()
        logger.hr(f'FullReset done (layer1_ok={layer1_ok})', level=2)
        return layer1_ok

    # ------------------------------------------------------------------
    # Layer 3: 截图通道
    # ------------------------------------------------------------------

    def _teardown_layer3_screenshot_channels(self) -> None:
        """释放截图通道（NemuIpc dll、scrcpy server、droidcast 等）。"""
        logger.info('FullReset Layer 3: screenshot channels')

        if 'nemu_ipc' in self.device.__dict__:
            try:
                self.device.nemu_ipc.__exit__(None, None, None)
                logger.info('  - nemu_ipc.__exit__() done')
            except Exception as e:
                logger.warning(f'  - nemu_ipc.__exit__() failed: {e}')
            del_cached_property(self.device, 'nemu_ipc')
            logger.info('  - nemu_ipc cached_property cleared')

        if hasattr(self.device, '_scrcpy_server_stop'):
            try:
                self.device._scrcpy_server_stop()
                logger.info('  - scrcpy server stopped')
            except Exception as e:
                logger.warning(f'  - scrcpy stop failed: {e}')

    # ------------------------------------------------------------------
    # Layer 2: ADB
    # ------------------------------------------------------------------

    def _teardown_layer2_adb(self) -> None:
        """断开 ADB 连接。"""
        logger.info('FullReset Layer 2: ADB')
        try:
            serial = self.device.serial
            self.device.adb_client.disconnect(serial)
            logger.info(f'  - adb disconnected {serial}')
        except Exception as e:
            logger.warning(f'  - adb disconnect failed: {e}')

    # ------------------------------------------------------------------
    # Layer 1: 进程（2 档强杀）
    # ------------------------------------------------------------------

    def _target_processes(self) -> list[psutil.Process]:
        """按实例标识查找恢复时需要强杀的模拟器进程。"""
        instance = self.device.emulator_instance
        if instance is None:
            return []

        official_pids = self._official_target_pids()
        if official_pids:
            if instance == Emulator.NoxPlayerFamily:
                # Nox 官方输出只保留 PID，额外校验进程名以降低输出漂移导致的误杀风险。
                return self._processes_by_pids(official_pids, allowed_names={'nox.exe', 'noxvmhandle.exe'})
            return self._processes_by_pids(official_pids)

        target_name = getattr(instance, 'name', '') or ''
        target_id = getattr(instance, 'MuMuPlayer12_id', None)

        targets = []
        for proc in psutil.process_iter(['pid', 'name', 'cmdline']):
            try:
                name = proc.info['name'] or ''
                cmdline = proc.info.get('cmdline') or []
                if instance == Emulator.MuMuPlayer12:
                    if name == 'MuMuVMMHeadless.exe' and target_name and self._cmdline_has_option(cmdline, '--comment', target_name):
                        targets.append(proc)
                    elif name == 'MuMuNxDevice.exe' and target_id is not None and self._cmdline_has_option(cmdline, '-v', str(target_id)):
                        targets.append(proc)
                    elif name in {'MuMuPlayer.exe', 'MuMuNxMain.exe'} and target_id is not None and self._cmdline_has_option(cmdline, '-v', str(target_id)):
                        targets.append(proc)
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
        return targets

    def _processes_by_pids(self, pids: set[int], allowed_names: set[str] | None = None) -> list[psutil.Process]:
        """按官方控制台返回的 PID 构造进程对象，可按进程名白名单降低误杀风险。"""
        try:
            # 只遍历一次进程快照，避免每个 PID 都重复扫描全量进程。
            alive_pids = {proc.info['pid'] for proc in psutil.process_iter(['pid'])}
        except Exception as e:
            logger.warning(f'    official pid snapshot failed: {e}')
            alive_pids = set(pids)

        targets = []
        for pid in pids:
            if pid not in alive_pids:
                continue
            try:
                proc = psutil.Process(pid)
                if allowed_names is not None:
                    # 只接受指定模拟器相关进程名，避免官方输出格式漂移时强杀无关 PID。
                    proc_name = (getattr(proc, 'info', {}).get('name') or proc.name() or '').lower()
                    if proc_name not in allowed_names:
                        logger.warning(f'    official pid {pid} skipped by unexpected process name: {proc_name}')
                        continue
                targets.append(proc)
            except psutil.NoSuchProcess:
                continue
            except Exception as e:
                logger.warning(f'    official pid {pid} lookup failed: {e}')
        return targets

    def _official_target_pids(self) -> set[int]:
        """优先使用模拟器官方控制台查询当前实例 PID。"""
        instance = self.device.emulator_instance
        if instance == Emulator.LDPlayerFamily:
            return self._ldplayer_official_pids()
        if instance == Emulator.MEmuPlayer:
            return self._memu_official_pids()
        if instance == Emulator.NoxPlayerFamily:
            return self._nox_official_pids()
        return set()

    def _nox_official_pids(self) -> set[int]:
        """解析 NoxConsole list 输出中的当前实例进程 PID。"""
        instance = self.device.emulator_instance
        target_name = getattr(instance, 'name', '') or ''
        if not target_name:
            return set()

        exe = getattr(getattr(instance, 'emulator', None), 'path', '')
        if not exe:
            return set()
        console = os.path.join(os.path.dirname(exe), 'NoxConsole.exe')

        try:
            result = subprocess.run(
                [console, 'list'],
                capture_output=True, text=True, timeout=10, shell=False
            )
        except Exception as e:
            logger.warning(f'    NoxConsole list failed: {e}')
            return set()

        if result.returncode != 0:
            logger.warning(f'    NoxConsole list returned {result.returncode}')
            return set()

        pids = set()
        line_prefix = f'{target_name},'
        for line in result.stdout.splitlines():
            if not line.startswith(line_prefix):
                continue
            fields_tail = line[len(line_prefix):].split(',')
            if not fields_tail:
                continue
            try:
                # 实例名可能包含英文逗号，先剥离完整实例名前缀，再从行尾读取 PID。
                pid = int(fields_tail[-1])
            except ValueError:
                return set()
            if pid > 0:
                pids.add(pid)
            break
        return pids

    def _memu_official_pids(self) -> set[int]:
        """解析 MEmu listvms 输出中的当前实例进程 PID。"""
        instance = self.device.emulator_instance
        target_name = getattr(instance, 'name', '') or ''
        if not target_name:
            return set()

        exe = getattr(getattr(instance, 'emulator', None), 'path', '')
        if not exe:
            return set()
        console = Emulator.single_to_console(exe)

        try:
            result = subprocess.run(
                [console, 'listvms'],
                capture_output=True, text=True, timeout=10, shell=False
            )
        except Exception as e:
            logger.warning(f'    MEmu listvms failed: {e}')
            return set()

        if result.returncode != 0:
            logger.warning(f'    MEmu listvms returned {result.returncode}')
            return set()

        pids = set()
        for line in result.stdout.splitlines():
            parts = line.split(',')
            if len(parts) < 2 or parts[1] != target_name:
                continue
            try:
                # 按官方 listvms 行尾 PID 解析，避免按 exe 名误杀其它实例。
                pid = int(parts[-1])
            except ValueError:
                return set()
            if pid > 0:
                pids.add(pid)
            break
        return pids

    def _ldplayer_official_pids(self) -> set[int]:
        """解析 LDPlayer list2 输出中的当前实例进程 PID 和 VBox PID。"""
        instance = self.device.emulator_instance
        target_id = getattr(instance, 'LDPlayer_id', None)
        if target_id is None:
            return set()

        exe = getattr(getattr(instance, 'emulator', None), 'path', '')
        if not exe:
            return set()
        console = Emulator.single_to_console(exe)

        try:
            result = subprocess.run(
                [console, 'list2'],
                capture_output=True, text=True, timeout=10, shell=False
            )
        except Exception as e:
            logger.warning(f'    LDPlayer list2 failed: {e}')
            return set()

        if result.returncode != 0:
            logger.warning(f'    LDPlayer list2 returned {result.returncode}')
            return set()

        pids = set()
        for line in result.stdout.splitlines():
            parts = line.split(',')
            if len(parts) < 3:
                continue
            try:
                index = int(parts[0])
            except ValueError:
                continue
            if index != target_id:
                continue
            # 实例名可能包含英文逗号，PID 固定从 list2 行尾两个字段读取。
            for raw_pid in (parts[-2], parts[-1]):
                try:
                    pid = int(raw_pid)
                except ValueError:
                    continue
                if pid > 0:
                    pids.add(pid)
            break
        return pids

    @staticmethod
    def _cmdline_has_option(cmdline: list[str], option: str, value: str) -> bool:
        """检查命令行是否包含成对参数，避免正则误伤其他实例。"""
        for index, token in enumerate(cmdline[:-1]):
            if token == option and cmdline[index + 1] == value:
                return True
        return False

    def _process_alive(self) -> bool:
        """优先按目标进程匹配检测，兼容旧 health 检测作为兜底。"""
        if self._target_processes():
            return True
        try:
            ok, _reason = self.device.health._process_check()
        except Exception as e:
            logger.warning(f'    进程健康检查失败，按已退出处理: {e}')
            return False
        return ok

    def _wait_process_dead(self, timeout: float) -> bool:
        """轮询等待目标进程消失，每 0.5s 查一次。"""
        deadline = time.time() + timeout
        while time.time() < deadline:
            if not self._target_processes():
                return True
            time.sleep(0.5)
        return not self._target_processes()

    def _teardown_layer1_process_psutil_kill(self) -> bool:
        """档 1: psutil 精确强杀目标实例进程。"""
        logger.info('  Kill tier 1: psutil precise kill')
        targets = self._target_processes()
        if not targets:
            logger.warning('    no matching process found')
            return not self._process_alive()

        for proc in targets:
            try:
                proc.kill()
                logger.info(f'    killed pid {proc.pid}')
            except psutil.NoSuchProcess:
                pass
            except Exception as e:
                logger.warning(f'    kill pid {proc.pid} failed: {e}')

        return self._wait_process_dead(timeout=5)

    def _teardown_layer1_process_taskkill(self) -> bool:
        """档 2: Windows taskkill /F /PID 兜底强杀。"""
        logger.info('  Kill tier 2: taskkill /F /PID')
        targets = self._target_processes()
        if not targets:
            logger.warning('    no matching process found')
            return not self._process_alive()

        for proc in targets:
            try:
                subprocess.run(
                    ['taskkill', '/F', '/PID', str(proc.pid)],
                    capture_output=True, timeout=5, shell=False
                )
                logger.info(f'    taskkill /F /PID {proc.pid} issued')
            except Exception as e:
                logger.warning(f'    taskkill pid {proc.pid} failed: {e}')

        return self._wait_process_dead(timeout=5)

    def _teardown_layer1_process(self) -> bool:
        """Layer 1: 恢复路径只做强杀，不调用模拟器 stop。"""
        logger.info('FullReset Layer 1: process')

        if not self._process_alive():
            logger.info('  - process already dead, skip')
            return True

        tiers = [
            ('psutil_kill', self._teardown_layer1_process_psutil_kill),
            ('taskkill', self._teardown_layer1_process_taskkill),
        ]
        for name, tier in tiers:
            if tier():
                logger.info(f'  - tier {name} succeeded')
                return True
            logger.warning(f'  - tier {name} failed, falling back to next')
        logger.error('FullReset Layer 1: all force kill tiers failed')
        return False

    # ------------------------------------------------------------------
    # Layer 0: Device 实例级 cached_property
    # ------------------------------------------------------------------

    def _teardown_layer0_cached_properties(self) -> None:
        """
        Device 实例级 cached_property 失效（硬件标识符）。

        注意：class-level detect_record / click_record / stuck_timer /
        stuck_timer_long 一律不动（承载跨 reset 累积的卡死检测语义，D10）。
        """
        logger.info('FullReset Layer 0: instance cached_property')
        targets = [
            'emulator_instance',
            'nemu_ipc',  # 即使 Layer 3 已清，幂等
            'root_handle_num',
            'screenshot_handle_num',
            'screenshot_size',
        ]
        for prop in targets:
            if prop in self.device.__dict__:
                del_cached_property(self.device, prop)
                logger.info(f'  - cleared {prop}')


if __name__ == '__main__':
    # Manual REPL smoke test. Usage: ./toolkit/python.exe -m module.device.emulator_reset [config_name]
    # WARNING: This will actually kill the running emulator. Use with care.
    import sys
    from module.config.config import Config
    from module.device.device import Device

    config_name = sys.argv[1] if len(sys.argv) > 1 else 'oas1'
    config = Config(config_name)
    device = Device(config)
    reset = FullReset(device)
    print('Calling FullReset.execute() — this will tear down the emulator!')
    ok = reset.execute()
    print(f'Result: layer1_ok={ok}')
