import asyncio
import ctypes
import glob
import os
import sys
import time
from functools import partial, wraps
from pathlib import Path

import cv2
import numpy as np

from module.base.decorator import cached_property, del_cached_property, has_cached_property
from module.base.utils import ensure_time
from module.device.method.minitouch import insert_swipe, random_rectangle_point
from module.device.method.utils import RETRY_TRIES, retry_sleep
from module.exception import RequestHumanTakeover, GameNotRunningError, EmulatorNotRunningError
from module.logger import logger


class NemuIpcIncompatible(Exception):
    pass


class NemuIpcError(Exception):
    pass


class CaptureStd:
    """
    Capture stdout and stderr from both python and C library
    https://stackoverflow.com/questions/5081657/how-do-i-prevent-a-c-shared-library-to-print-on-stdout-in-python/17954769

    ```
    with CaptureStd() as capture:
        # String wasn't printed
        print('whatever')
    # But captured in ``capture.stdout``
    print(f'Got stdout: "{capture.stdout}"')
    print(f'Got stderr: "{capture.stderr}"')
    ```
    """

    def __init__(self):
        self.stdout = b''
        self.stderr = b''

    def _redirect_stdout(self, to):
        if sys.stdout is not None:
            sys.stdout.close()
            os.dup2(to, self.fdout)
            sys.stdout = os.fdopen(self.fdout, 'w')

    def _redirect_stderr(self, to):
        if sys.stderr is not None:
            sys.stderr.close()
            os.dup2(to, self.fderr)
            sys.stderr = os.fdopen(self.fderr, 'w')

    def __enter__(self):
        # Handle cases where stdout or stderr could be None
        sys.stdout = None
        sys.stderr = None
        if sys.stdout is None:
            self.fdout = None
        else:
            self.fdout = sys.stdout.fileno()
            
        if sys.stderr is None:
            self.fderr = None
        else:
            self.fderr = sys.stderr.fileno()
            
        self.reader_out, self.writer_out = os.pipe()
        self.reader_err, self.writer_err = os.pipe()
        
        # Store old file descriptors if they exist
        if self.fdout is not None:
            self.old_stdout = os.dup(self.fdout)
        else:
            self.old_stdout = None
            
        if self.fderr is not None:
            self.old_stderr = os.dup(self.fderr)
        else:
            self.old_stderr = None

        file_out = os.fdopen(self.writer_out, 'w')
        file_err = os.fdopen(self.writer_err, 'w')
        
        if sys.stdout is not None:
            self._redirect_stdout(to=file_out.fileno())
        if sys.stderr is not None:
            self._redirect_stderr(to=file_err.fileno())
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        if sys.stdout is not None and self.old_stdout is not None:
            self._redirect_stdout(to=self.old_stdout)
        if sys.stderr is not None and self.old_stderr is not None:
            self._redirect_stderr(to=self.old_stderr)
            
        if self.old_stdout is not None:
            os.close(self.old_stdout)
        if self.old_stderr is not None:
            os.close(self.old_stderr)

        self.stdout = self.recvall(self.reader_out)
        self.stderr = self.recvall(self.reader_err)
        os.close(self.reader_out)
        os.close(self.reader_err)

    @staticmethod
    def recvall(reader, length=1024) -> bytes:
        fragments = []
        while 1:
            chunk = os.read(reader, length)
            if chunk:
                fragments.append(chunk)
            else:
                break
        output = b''.join(fragments)
        return output


class CaptureNemuIpc(CaptureStd):
    instance = None

    def is_capturing(self):
        """
        Only capture at the topmost wrapper to avoid nested capturing
        If a capture is ongoing, this instance does nothing
        """
        cls = self.__class__
        return isinstance(cls.instance, cls) and cls.instance != self

    def __enter__(self):
        if self.is_capturing():
            return self

        super().__enter__()
        CaptureNemuIpc.instance = self
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        if self.is_capturing():
            return

        CaptureNemuIpc.instance = None
        super().__exit__(exc_type, exc_val, exc_tb)

        self.check_stdout()
        self.check_stderr()

    def check_stdout(self):
        if not self.stdout:
            return
        logger.info(f'NemuIpc stdout: {self.stdout}')

    def check_stderr(self):
        if not self.stderr:
            return
        logger.error(f'NemuIpc stderr: {self.stderr}')

        # Calling an old MuMu12 player
        # Tested on 3.4.0
        # b'nemu_capture_display rpc error: 1783\r\n'
        # Tested on 3.7.3
        # b'nemu_capture_display rpc error: 1745\r\n'
        if b'error: 1783' in self.stderr or b'error: 1745' in self.stderr:
            raise NemuIpcIncompatible(
                f'NemuIpc requires MuMu12 version >= 3.8.13, please check your version')
        # contact_id incorrect
        # b'nemu_capture_display cannot find rpc connection\r\n'
        if b'cannot find rpc connection' in self.stderr:
            raise NemuIpcError(self.stderr)
        # Emulator died
        # b'nemu_capture_display rpc error: 1722\r\n'
        # MuMuVMMSVC.exe died
        # b'nemu_capture_display rpc error: 1726\r\n'
        # No idea how to handle yet
        if b'error: 1722' in self.stderr or b'error: 1726' in self.stderr:
            raise NemuIpcError('Emulator instance is probably dead')


def retry(func):
    @wraps(func)
    def retry_wrapper(self, *args, **kwargs):
        """
        Args:
            self (NemuIpcImpl):
        """
        init = None
        last_error = None
        for _ in range(RETRY_TRIES):
            try:
                if callable(init):
                    retry_sleep(_)
                    init()
                return func(self, *args, **kwargs)
            # Can't handle
            except RequestHumanTakeover:
                break
            # Can't handle
            except NemuIpcIncompatible as e:
                logger.error(e)
                break
            # Function call timeout
            except asyncio.TimeoutError as e:
                last_error = e
                logger.warning(f'Func {func.__name__}() call timeout, retrying: {_}')

                def init():
                    self.reconnect()
            # NemuIpcError
            except NemuIpcError as e:
                last_error = e
                logger.error(e)

                def init():
                    self.reconnect()
            # Unknown, probably a trucked image
            except Exception as e:
                logger.exception(e)

                def init():
                    pass

        logger.critical(f'Retry {func.__name__}() failed')
        if isinstance(last_error, asyncio.TimeoutError):
            raise EmulatorNotRunningError(f'NemuIpc timeout during {func.__name__}(), emulator not responsive')
        if isinstance(last_error, NemuIpcError):
            raise EmulatorNotRunningError(f'NemuIpc unavailable during {func.__name__}(): {last_error}')
        raise RequestHumanTakeover

    return retry_wrapper


class NemuIpcImpl:
    def __init__(self, nemu_folder: str, instance_id: int, display_id: int = 0):
        """
        Args:
            nemu_folder: Installation path of MuMu12, e.g. E:/ProgramFiles/MuMuPlayer-12.0
            instance_id: Emulator instance ID, starting from 0
            display_id: Always 0 if keep app alive was disabled
        """
        self.nemu_folder: str = nemu_folder
        self.instance_id: int = instance_id
        self.display_id: int = display_id

        list_dll = [
            # MuMuPlayer12
            os.path.abspath(os.path.join(nemu_folder, './shell/sdk/external_renderer_ipc.dll')),
            # MuMuPlayer12 nx_device 布局，版本号不写死（安卓12=12.0 / 安卓15=15.0 等）
            *[
                os.path.abspath(p)
                for p in glob.glob(os.path.join(nemu_folder, './nx_device/*/shell/sdk/external_renderer_ipc.dll'))
            ],
        ]
        self.lib = None
        for ipc_dll in list_dll:
            if not os.path.exists(ipc_dll):
                continue
            try:
                self.lib = ctypes.CDLL(ipc_dll)
                break
            except OSError as e:
                logger.error(e)
                logger.error(f'ipc_dll={ipc_dll} exists, but cannot be loaded')
                continue
        if self.lib is None:
            # not found
            raise NemuIpcIncompatible(
                f'NemuIpc requires MuMu12 version >= 3.8.13, please check your version. '
                f'None of the following path exists: {list_dll}')
        # success
        logger.info(
            f'NemuIpcImpl init, '
            f'nemu_folder={nemu_folder}, '
            f'ipc_dll={ipc_dll}, '
            f'instance_id={instance_id}, '
            f'display_id={display_id}'
        )

        self.connect_id: int = 0
        self.width = 0
        self.height = 0

    def connect(self):
        if self.connect_id > 0:
            return

        connect_id = self.ev_run_sync(
            self.lib.nemu_connect,
            self.nemu_folder, self.instance_id
        )
        if connect_id == 0:
            raise NemuIpcError(
                'Connection failed, please check if nemu_folder is correct and emulator is running'
            )

        self.connect_id = connect_id
        # logger.info(f'NemuIpc connected: {self.connect_id}')

    def disconnect(self):
        if self.connect_id == 0:
            return

        self.ev_run_sync(
            self.lib.nemu_disconnect,
            self.connect_id
        )

        # logger.info(f'NemuIpc disconnected: {self.connect_id}')
        self.connect_id = 0

    def reconnect(self):
        self.disconnect()
        self.connect()

    def __enter__(self):
        self.connect()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.disconnect()

    @cached_property
    def _ev(self):
        return asyncio.new_event_loop()

    async def ev_run_async(self, func, *args, **kwargs):
        """
        Args:
            func: Sync function to call
            *args:
            **kwargs:

        Raises:
            asyncio.TimeoutError: If function call timeout
        """
        func_wrapped = partial(func, *args, **kwargs)
        # Increased timeout for slow PCs
        # Default screenshot interval is 0.2s, so a 0.15s timeout would have a fast retry without extra time costs
        result = await asyncio.wait_for(self._ev.run_in_executor(None, func_wrapped), timeout=0.15)
        return result

    def ev_run_sync(self, func, *args, **kwargs):
        """
        Args:
            func: Sync function to call
            *args:
            **kwargs:

        Raises:
            asyncio.TimeoutError: If function call timeout
            NemuIpcIncompatible:
            NemuIpcError
        """
        result = self._ev.run_until_complete(self.ev_run_async(func, *args, **kwargs))

        err = False
        if func.__name__ == 'nemu_connect':
            if result == 0:
                err = True
        else:
            if result > 0:
                err = True
        # Get to actual error message printed in std
        if err:
            logger.warning(f'Failed to call {func.__name__}, result={result}')
            with CaptureNemuIpc():
                result = self._ev.run_until_complete(self.ev_run_async(func, *args, **kwargs))

        return result

    def get_resolution(self):
        """
        Get emulator resolution, `self.width` and `self.height` will be set
        """
        if self.connect_id == 0:
            self.connect()

        width_ptr = ctypes.pointer(ctypes.c_int(0))
        height_ptr = ctypes.pointer(ctypes.c_int(0))
        nullptr = ctypes.POINTER(ctypes.c_int)()

        ret = self.ev_run_sync(
            self.lib.nemu_capture_display,
            self.connect_id, self.display_id, 0, width_ptr, height_ptr, nullptr
        )
        if ret > 0:
            raise NemuIpcError('nemu_capture_display failed during get_resolution()')
        self.width = width_ptr.contents.value
        self.height = height_ptr.contents.value

    @retry
    def screenshot(self):
        """
        Returns:
            np.ndarray: Image array in RGBA color space
                Note that image is upside down
        """
        if self.connect_id == 0:
            self.connect()

        self.get_resolution()

        width_ptr = ctypes.pointer(ctypes.c_int(self.width))
        height_ptr = ctypes.pointer(ctypes.c_int(self.height))
        length = self.width * self.height * 4
        pixels_pointer = ctypes.pointer((ctypes.c_ubyte * length)())

        ret = self.ev_run_sync(
            self.lib.nemu_capture_display,
            self.connect_id, self.display_id, length, width_ptr, height_ptr, pixels_pointer
        )
        if ret > 0:
            raise NemuIpcError('nemu_capture_display failed during screenshot()')

        # image = np.ctypeslib.as_array(pixels_pointer, shape=(self.height, self.width, 4))
        image = np.ctypeslib.as_array(pixels_pointer.contents).reshape((self.height, self.width, 4))
        return image

    def convert_xy(self, x, y):
        """
        坐标透传（恒等）。

        实测依据（实例A, MuMu nx_device 12.0, 横屏 1280x720, 2026-08-27 探针）：
        DLL 参数本身就是屏幕坐标——DLL 内部映射 kernel=(720-b, a) 写入竖屏触摸面板
        "Xiaomi Input"(X∈[0,720], Y∈[0,1280])，框架按 viewport orientation=1 旋转回
        横屏后恰好还原为 (a, b)。原 ALAS 继承的旋转 (height-y, x) 是给竖屏场景用的，
        在横屏 MuMu12 上会把落点转置+翻转（导致右半屏/下方越界坐标被系统丢弃）。
        完整推导与验证记录见 dev_tools/probe_nemu_input.py。

        Returns:
            int, int
        """
        return int(x), int(y)

    @retry
    def down(self, x, y):
        """
        Contact down, continuous contact down will be considered as swipe
        """
        if self.connect_id == 0:
            self.connect()
        if self.height == 0:
            self.get_resolution()

        x, y = self.convert_xy(x, y)

        ret = self.ev_run_sync(
            self.lib.nemu_input_event_touch_down,
            self.connect_id, self.display_id, x, y
        )
        if ret > 0:
            raise NemuIpcError('nemu_input_event_touch_down failed')

    @retry
    def up(self):
        """
        Contact up
        """
        if self.connect_id == 0:
            self.connect()

        ret = self.ev_run_sync(
            self.lib.nemu_input_event_touch_up,
            self.connect_id, self.display_id
        )
        if ret > 0:
            raise NemuIpcError('nemu_input_event_touch_up failed')


def serial_to_id(serial: str):
    """
    Predict instance ID from serial
    E.g.
        "127.0.0.1:16384" -> 0
        "127.0.0.1:16416" -> 1

    Returns:
        int: instance_id, or None if failed to predict
    """
    try:
        port = int(serial.split(':')[1])
    except (IndexError, ValueError):
        return None
    index, offset = divmod(port - 16384, 32)
    if 0 <= index < 32 and offset in [0, 1, 2]:
        return index
    else:
        return None


class NemuIpc():
    @cached_property
    def nemu_ipc(self) -> NemuIpcImpl:
        """
        Initialize a nemu ipc implementation
        """
        # Try existing settings first
        if self.config.script.device.emulatorinfo_path:
            folder = str(Path(self.config.script.device.emulatorinfo_path).parent.parent)
            index = serial_to_id(self.serial)
            if index is not None:
                max_retries = 10
                last_error = None
                for attempt in range(max_retries):
                    try:
                        return NemuIpcImpl(
                            nemu_folder=folder,
                            instance_id=index,
                            display_id=0
                        ).__enter__()
                    except NemuIpcIncompatible as e:
                        logger.error(e)
                        logger.error('Emulator info incorrect')
                        break
                    except (NemuIpcError, TimeoutError, asyncio.TimeoutError) as e:
                        last_error = e
                        if attempt < max_retries - 1:
                            kind = 'timeout' if isinstance(e, (TimeoutError, asyncio.TimeoutError)) else 'error'
                            logger.warning(f'NemuIpc connect {kind} (attempt {attempt + 1}/{max_retries}), retry in 2s...')
                            time.sleep(2)
                else:
                    if last_error is not None:
                        logger.error(last_error)
                        logger.error('Emulator info path retries exhausted, falling back to instance search')

        # Search emulator instance
        # with E:\ProgramFiles\MuMuPlayer-12.0\shell\MuMuPlayer.exe
        # installation path is E:\ProgramFiles\MuMuPlayer-12.0
        if self.emulator_instance is None:
            logger.error('Unable to use NemuIpc because emulator instance not found')
            raise RequestHumanTakeover

        # Retry: IPC service may not be ready immediately after emulator cold boot
        max_retries = 10
        last_error = None
        for attempt in range(max_retries):
            try:
                return NemuIpcImpl(
                    nemu_folder=self.emulator_instance.emulator.abspath('../'),
                    instance_id=self.emulator_instance.MuMuPlayer12_id,
                    display_id=0
                ).__enter__()
            except NemuIpcIncompatible as e:
                logger.error(e)
                raise RequestHumanTakeover
            except (NemuIpcError, TimeoutError, asyncio.TimeoutError) as e:
                last_error = e
                if attempt < max_retries - 1:
                    kind = 'timeout' if isinstance(e, (TimeoutError, asyncio.TimeoutError)) else 'error'
                    logger.warning(f'NemuIpc connect {kind} (attempt {attempt + 1}/{max_retries}), retry in 2s...')
                    time.sleep(2)

        logger.error(last_error)
        logger.error('Unable to initialize NemuIpc after retries')
        if isinstance(last_error, (TimeoutError, asyncio.TimeoutError)):
            raise EmulatorNotRunningError('NemuIpc connect timeout after retries, emulator not responsive')
        raise EmulatorNotRunningError('NemuIpc connect failed after retries, emulator IPC service not ready')

    def nemu_ipc_available(self) -> bool:
        if not self.is_mumu_family:
            return False
        if self.nemud_app_keep_alive == '':
            return False
        try:
            _ = self.nemu_ipc
        except (RequestHumanTakeover, GameNotRunningError):
            return False
        return True

    def nemu_ipc_release(self):
        if has_cached_property(self, 'nemu_ipc'):
            self.nemu_ipc.disconnect()
        del_cached_property(self, 'nemu_ipc')
        logger.info('nemu_ipc released')

    def screenshot_nemu_ipc(self):
        try:
            image = self.nemu_ipc.screenshot()
        except (TimeoutError, asyncio.TimeoutError) as e:
            logger.error(f'NemuIpc screenshot timeout: {e}')
            self.nemu_ipc_release()
            raise EmulatorNotRunningError('NemuIpc timeout, emulator not responsive')

        image = cv2.cvtColor(image, cv2.COLOR_BGRA2BGR)
        cv2.flip(image, 0, dst=image)
        return image

    def click_nemu_ipc(self, x, y):
        down = ensure_time((0.010, 0.020))
        self.nemu_ipc.down(x, y)
        self.sleep(down)
        self.nemu_ipc.up()
        self.sleep(0.050 - down)

    def long_click_nemu_ipc(self, x, y, duration=1.0):
        self.nemu_ipc.down(x, y)
        self.sleep(duration)
        self.nemu_ipc.up()
        self.sleep(0.050)

    def swipe_nemu_ipc(self, p1, p2, duration=None):
        points = insert_swipe(p0=p1, p3=p2)
        # 时长按点数均分；未指定时保持原 10ms/点 的节奏
        step = 0.010 if duration is None else max(float(duration) / len(points), 0.004)

        for point in points:
            self.nemu_ipc.down(*point)
            self.sleep(step)

        self.nemu_ipc.up()
        self.sleep(0.050)

    def drag_nemu_ipc(self, p1, p2, point_random=(-10, -10, 10, 10)):
        p1 = np.array(p1) - random_rectangle_point(point_random)
        p2 = np.array(p2) - random_rectangle_point(point_random)
        points = insert_swipe(p0=p1, p3=p2, speed=20)

        for point in points:
            self.nemu_ipc.down(*point)
            self.sleep(0.010)

        self.nemu_ipc.down(*p2)
        self.sleep(0.140)
        self.nemu_ipc.down(*p2)
        self.sleep(0.140)

        self.nemu_ipc.up()
        self.sleep(0.050)

    # ------------------------------------------------------------------
    # humanized 三件套（无 @retry）：Control 的 humanized_*_methods 在
    # enabled 时直达。nemu 的 down/up 本身是单次原子 IPC 调用（内部自带
    # ev_run_sync 重试），属于契约 #11 允许的 A 类，重放不会产生半截手势。
    # ------------------------------------------------------------------

    def _click_nemu_ipc_humanized_impl(self, x, y):
        # 开档点击：press_seconds 消费维度 B（按压时长）；None（off/策略回退）
        # 时单次调用无装饰 legacy
        press = self.humanizer.press_seconds()
        if press is None:
            return self.click_nemu_ipc(x, y)
        self.nemu_ipc.down(x, y)
        self.sleep(press)
        self.nemu_ipc.up()
        # 维度 I：enabled 用 gap_seconds(0.05) 收尾，与 minitouch/u2 同口径
        gap = self.humanizer.gap_seconds(0.05)
        self.sleep(gap if gap is not None else 0.050)

    def _long_click_nemu_ipc_humanized_impl(self, x, y, duration=1.0):
        # 开档长按（维度 J hold 微颤）：plan_hold 返回 None（off/'none'/预算过短）
        # 时单次调用无装饰 legacy。nemu 单次 down 仅 ~0.3ms IPC 往返，
        # point_cap 与 minitouch 同取 200，微颤点可全量投递
        plan = self.humanizer.plan_hold(
            (int(x), int(y)), float(duration), point_cap=200)
        if plan is None:
            return self.long_click_nemu_ipc(x, y, duration)
        self.nemu_ipc.down(x, y)
        # delays[i] 是发送 points[i] 前的等待（全局契约 4）
        for (px, py), dt in zip(plan.points, plan.delays):
            self.sleep(dt)
            self.nemu_ipc.down(px, py)
        self.nemu_ipc.up()
        gap = self.humanizer.gap_seconds(0.05)
        self.sleep(gap if gap is not None else 0.050)

    def _swipe_nemu_ipc_humanized_impl(self, p1, p2, duration=0.1):
        # 开档滑动：plan_swipe 消费 C/D；None（off/越界/几何失败）时走 legacy。
        # 语义与 swipe_nemu_ipc 相同：沿轨迹逐点 down（连续 down 被内核解释为
        # 同一接触点的 MOVE，见探针 getevent 验证），结尾 up。point_cap=100 与
        # uiautomator2 同口径（100 点/2s ≈ 50Hz 有效回报率）
        from module.device.humanize import timing
        # 分派链路（ensure_int）可能给 list，plan 校验要求 tuple，这里统一归一
        p1 = (int(p1[0]), int(p1[1]))
        p2 = (int(p2[0]), int(p2[1]))
        plan = self.humanizer.plan_swipe(
            p1, p2, base_delay_s=duration / timing.PROFILE_MAX_POINTS,
            point_cap=100)
        if plan is None:
            return self.swipe_nemu_ipc(p1, p2)
        self.nemu_ipc.down(*p1)
        for (px, py), dt in zip(plan.points, plan.delays):
            self.sleep(dt)
            self.nemu_ipc.down(px, py)
        self.nemu_ipc.up()
        gap = self.humanizer.gap_seconds(0.05)
        self.sleep(gap if gap is not None else 0.050)
