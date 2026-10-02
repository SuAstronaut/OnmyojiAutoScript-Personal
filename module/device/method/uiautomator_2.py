import json
import subprocess
import sys
import time
import typing as t
import numpy as np
import cv2
from contextvars import ContextVar
from dataclasses import dataclass
from functools import wraps
from json.decoder import JSONDecodeError
from subprocess import list2cmdline

import requests
import uiautomator2 as u2
from urllib3.util import Timeout as Urllib3Timeout
from adbutils.errors import AdbError
from lxml import etree

from module.base.utils import point2str, random_rectangle_point, random_line_segments
from module.device.connection import Connection
from module.device.humanize import timing
from module.device.method.utils import (RETRY_TRIES, retry_sleep, handle_adb_error,
                                        ImageTruncated, PackageNotInstalled, possible_reasons,
    random_port, humanized_http_request)
from module.exception import RequestHumanTakeover
from module.logger import logger


def retry(func):
    @wraps(func)
    def retry_wrapper(self, *args, **kwargs):
        """
        Args:
            self (Uiautomator2):
        """
        init = None
        for _ in range(RETRY_TRIES):
            try:
                if callable(init):
                    retry_sleep(_)
                    init()
                return func(self, *args, **kwargs)
            # Can't handle
            except RequestHumanTakeover:
                break
            # When adb server was killed
            except ConnectionResetError as e:
                logger.error(e)

                def init():
                    self.adb_reconnect()
            # In `device.set_new_command_timeout(604800)`
            # json.decoder.JSONDecodeError: Expecting value: line 1 column 2 (char 1)
            except JSONDecodeError as e:
                logger.error(e)

                def init():
                    self.install_uiautomator2()
            # AdbError
            except AdbError as e:
                if handle_adb_error(e):
                    def init():
                        self.adb_reconnect()
                else:
                    break
            # RuntimeError: USB device 127.0.0.1:5555 is offline
            except RuntimeError as e:
                if handle_adb_error(e):
                    def init():
                        self.adb_reconnect()
                else:
                    break
            # In `assert c.read string(4) == _OKAY`
            # ADB on emulator not enabled
            except AssertionError as e:
                logger.exception(e)
                possible_reasons(
                    'If you are using BlueStacks or LD player or WSA, '
                    'please enable ADB in the settings of your emulator'
                )
                break
            # Package not installed
            except PackageNotInstalled as e:
                logger.error(e)

                def init():
                    self.detect_package()
            # ImageTruncated
            except ImageTruncated as e:
                logger.error(e)

                def init():
                    pass
            # Unknown
            except Exception as e:
                logger.exception(e)

                def init():
                    pass

        logger.critical(f'Retry {func.__name__}() failed')
        raise RequestHumanTakeover

    return retry_wrapper


@dataclass
class ProcessInfo:
    pid: int
    ppid: int
    thread_count: int
    cmdline: str
    name: str


@dataclass
class ShellBackgroundResponse:
    success: bool
    pid: int
    description: str


# ---- 拟人化 uiautomator2 接入常量（Plan Task 18 / 契约 11）----
# 单次原生输入 RPC 的二元 timeout：connect 与 read 分开给，绝不传标量——
# uiautomator2 的 TimeoutRequestsSession 会把标量扩成 (3, scalar)。
HUMANIZED_U2_RPC_CONNECT_TIMEOUT_S = 1.0
HUMANIZED_U2_RPC_READ_TIMEOUT_S = 1.0
# injectInputEvent 的 action 常量（与 uiautomator2 touch 内定义一致）
U2_ACTION_DOWN = 0
U2_ACTION_UP = 1
U2_ACTION_MOVE = 2
_HUMANIZED_U2_TRANSPORT = ContextVar('humanized_u2_transport', default=None)
# B 类投递异常白名单：仅这些连接/协议异常可被 _run_humanized_uiautomator2 按
# B0/B1 收口。RequestHumanTakeover 不在此列（恒原样上抛）。
_HUMANIZED_U2_TRANSPORT_ERRORS = (
    requests.RequestException,
    ConnectionError,
    TimeoutError,
    OSError,
    AdbError,
    JSONDecodeError,
    RuntimeError,
    u2.exceptions.BaseError,
)


class _HumanizedU2ProtocolError(RuntimeError):
    """HTTP 非 200 / 坏 JSON / JSON-RPC error 的统一协议异常，归入 B 类。"""
    pass


def _humanized_u2_timeout(http):
    """真实 uiautomator2 session 使用总 deadline，测试 fake 保留二元 timeout 契约。"""
    if isinstance(http, getattr(u2, '_AgentRequestSession', ())):
        return Urllib3Timeout(
            connect=HUMANIZED_U2_RPC_CONNECT_TIMEOUT_S,
            read=HUMANIZED_U2_RPC_READ_TIMEOUT_S,
            total=HUMANIZED_U2_RPC_CONNECT_TIMEOUT_S,
        )
    return (HUMANIZED_U2_RPC_CONNECT_TIMEOUT_S, HUMANIZED_U2_RPC_READ_TIMEOUT_S)


def _humanized_u2_rpc_url(device):
    """返回不触发 uiautomator2 path2url/forward_port 的绝对 RPC URL。"""
    serial = getattr(device, 'serial', '')
    if serial.startswith(('http://', 'https://')):
        return serial.rstrip('/') + '/jsonrpc/0', None
    if not serial or not hasattr(device, 'adb_binary'):
        # 单元 fake 没有设备连接属性时继续走相对 URL；真实设备永远走上面的
        # HTTP 或下面的显式 forward 分支。
        return '/jsonrpc/0', None

    # USB 模式的第三方 path2url() 会无 deadline 调用 forward_port()；开档单次
    # RPC 改用可超时的 adb CLI forward，并在请求后尽力移除临时 forward。
    port = random_port(device.config.FORWARD_PORT_RANGE)
    command = [device.adb_binary, '-s', serial, 'forward', f'tcp:{port}', 'tcp:7912']
    # 无控制台宿主下 adb.exe 会闪 cmd 窗口（与 connection.adb_command 同因），抑制之
    flags = subprocess.CREATE_NO_WINDOW if sys.platform.startswith('win') else 0
    try:
        subprocess.run(
            command,
            check=True,
            timeout=HUMANIZED_U2_RPC_CONNECT_TIMEOUT_S,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            creationflags=flags,
        )
    except (subprocess.TimeoutExpired, OSError, subprocess.CalledProcessError) as exc:
        raise requests.ConnectionError('humanized u2 forward failed') from exc
    return f'http://127.0.0.1:{port}/jsonrpc/0', port


def _humanized_u2_device(device):
    """取得不触发项目 cached_property 的 u2 客户端，仅构造本地会话对象。"""
    cached = device.__dict__.get('u2')
    if cached is not None:
        return cached
    serial = getattr(device, 'serial', '')
    if not serial:
        raise requests.ConnectionError('humanized u2 serial is unavailable')
    try:
        # 直接构造 Device 不会调用 connect_*、set_new_command_timeout 或 ATX 探测；
        # 后续 HTTP 请求使用项目侧绝对 URL，避免第三方 path2url/forward/retry。
        return u2.Device(serial)
    except Exception as exc:
        raise requests.ConnectionError('humanized u2 session construction failed') from exc


def _remove_humanized_u2_forward(device, port):
    """移除单次 RPC 建立的临时 forward；清理失败不改变输入结果判定。"""
    if port is None:
        return
    try:
        subprocess.run(
            [device.adb_binary, '-s', device.serial, 'forward', '--remove', f'tcp:{port}'],
            check=True,
            timeout=HUMANIZED_U2_RPC_CONNECT_TIMEOUT_S,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            # 同上：无控制台宿主下抑制 adb 的 cmd 窗口闪烁
            creationflags=subprocess.CREATE_NO_WINDOW if sys.platform.startswith('win') else 0,
        )
    except (subprocess.TimeoutExpired, OSError, subprocess.CalledProcessError):
        logger.warning('拟人化 u2 临时 forward 清理失败：tcp:%s', port)


class Uiautomator2(Connection):
    @retry
    def screenshot_uiautomator2(self):
        image = self.u2.screenshot(format='raw')
        image = np.frombuffer(image, np.uint8)
        if image is None:
            raise ImageTruncated('Empty image after reading from buffer')

        image = cv2.imdecode(image, cv2.IMREAD_COLOR)
        if image is None:
            raise ImageTruncated('Empty image after cv2.imdecode')

        image = cv2.cvtColor(image, cv2.COLOR_BGR2RGB)
        if image is None:
            raise ImageTruncated('Empty image after cv2.cvtColor')

        return image

    def _click_uiautomator2_legacy_impl(self, x, y):
        # 无装饰 legacy 实现：off 档与既有调用方经公开 @retry 包装进入（Plan 契约 11）。
        # 抽 _impl 时不得改动方法体，保证黄金基线逐事件不变。
        self.u2.click(x, y)

    @retry
    def click_uiautomator2(self, x, y):
        return self._click_uiautomator2_legacy_impl(x, y)

    def _long_click_uiautomator2_legacy_impl(self, x, y, duration=(1, 1.2)):
        # 无装饰 legacy 实现：off 档与既有调用方经公开 @retry 包装进入
        # （与 click/swipe 的 legacy impl 拓扑一致，Plan 契约 11）
        self.u2.long_click(x, y, duration=duration)

    @retry
    def long_click_uiautomator2(self, x, y, duration=(1, 1.2)):
        # 公开 @retry 方法只包装 legacy impl（契约 #11）
        return self._long_click_uiautomator2_legacy_impl(x, y, duration=duration)

    def _swipe_uiautomator2_legacy_impl(self, p1, p2, duration=0.1):
        # 无装饰 legacy 实现：off 档与既有调用方经公开 @retry 包装进入（Plan 契约 11）。
        self.u2.swipe(*p1, *p2, duration=duration)

    @retry
    def swipe_uiautomator2(self, p1, p2, duration=0.1):
        return self._swipe_uiautomator2_legacy_impl(p1, p2, duration=duration)

    def _u2_single_input_rpc(self, action, x, y):
        # 直达单次 HTTP JSON-RPC，绕开 uiautomator2 的 touch/jsonrpc 自动恢复与重发：
        # _jsonrpc_retry_call / _AgentRequestSession 会在 ReadTimeout / ServerError 后
        # reset_uiautomator() / _prepare_atx_agent() 并重发，破坏「一次 DOWN」边界。
        # 必须传二元 timeout 与 retry=False，不得传标量（会被扩成 (3, scalar)）。
        # Control 已提供授权的像素坐标；禁止调用 pos_rel2abs()，因为其边缘坐标会
        # 触发第三方 window_size()/shell 查询，而这些 HTTP/ADB 入口可能隐式恢复 ATX。
        x, y = int(x), int(y)
        transport = _HUMANIZED_U2_TRANSPORT.get()
        if transport is not None and transport[0] is self:
            _, u2_device, rpc_url = transport
        else:
            # 未绑定 transport 时同样使用受控的本地 Device 构造，绝不触发
            # connection_attr.u2 的 cached_property 初始化。
            u2_device = _humanized_u2_device(self)
            rpc_url = '/jsonrpc/0'
        request_id = (
            u2_device._jsonrpc_id('injectInputEvent')
            if u2_device is not None
            else f'humanized-{time.monotonic_ns()}'
        )
        request = {
            'jsonrpc': '2.0',
            'id': request_id,
            'method': 'injectInputEvent',
            'params': (action, x, y, 0),
        }
        # 同线程执行单次请求，避免调用方接管后仍有 daemon worker 迟到发送输入。
        deadline = time.monotonic() + HUMANIZED_U2_RPC_READ_TIMEOUT_S
        if (
            rpc_url.startswith(('http://', 'https://'))
            and (
                u2_device is None
                or isinstance(u2_device.http, getattr(u2, '_AgentRequestSession', ()))
            )
        ):
            # 真实 uiautomator2 session 的 read timeout 可被滴流续期；使用项目侧
            # 单次 socket 请求，以 monotonic deadline 限制整个 RPC，且无隐式重试。
            response = humanized_http_request(
                rpc_url,
                'post',
                data=json.dumps(request),
                headers={'Content-Type': 'application/json'},
                deadline=deadline,
            )
        elif u2_device is not None:
            response = u2_device.http.post(
                rpc_url,
                headers={'Content-Type': 'application/json'},
                data=json.dumps(request),
                timeout=_humanized_u2_timeout(u2_device.http),
                retry=False,
            )
        if response.status_code != 200:
            raise _HumanizedU2ProtocolError(f'input RPC returned HTTP {response.status_code}')
        try:
            payload = response.json()
        except (ValueError, JSONDecodeError) as exc:
            raise _HumanizedU2ProtocolError('input RPC returned invalid JSON') from exc
        if (
            not isinstance(payload, dict)
            or payload.get('jsonrpc') != '2.0'
            or payload.get('id') != request_id
            or 'result' not in payload
            or payload.get('result') is not True
            or 'error' in payload
        ):
            raise _HumanizedU2ProtocolError(f'input RPC returned error: {payload!r}')
        return payload.get('result')

    def _run_humanized_uiautomator2(self, run_humanized, run_legacy):
        # B 类投递异常收口：任何「RPC 已发出或结果未知」的异常一律保守归 B1
        # RequestHumanTakeover，不尝试轻量 legacy 重放、不发第二个 DOWN。宁可过度
        # 接管也不冒第二次 DOWN 的风险，勿当 bug 改掉（Plan 契约 11）。
        rpc_started = False

        def rpc(call):
            # 在 call() 之前切换 rpc_started：因此单次输入入口内部任何异常
            # 都按 B1 处理，绝不尝试第二次输入。
            nonlocal rpc_started
            rpc_started = True
            return call()

        try:
            return run_humanized(rpc)
        except RequestHumanTakeover:
            raise
        except _HUMANIZED_U2_TRANSPORT_ERRORS:
            if not rpc_started:
                return run_legacy()
            raise RequestHumanTakeover

    def _click_uiautomator2_humanized_impl(self, x, y):
        # 开档点击（无 @retry）：press_seconds 消费维度 B；None（off/策略回退）时
        # 单次调用无装饰 legacy。DOWN/UP 各经一次 _u2_single_input_rpc 发送。
        press_seconds = self.humanizer.press_seconds()
        if press_seconds is None:
            return self._click_uiautomator2_legacy_impl(x, y)

        def run(rpc):
            # u2 初始化和 USB forward 均发生在首个 RPC 之前；失败归 B0，
            # 事件发送阶段只使用已准备好的 session 与绝对 URL。
            # USB forward / HTTP URL 是首个原生 RPC 前唯一需要的准备步骤；不访问
            # connection_attr.u2，避免第三方 connect/set_new_command_timeout 的
            # 隐式 retry、ATX recovery 和无界 forward_port。
            u2_device = _humanized_u2_device(self)
            rpc_url, forward_port = _humanized_u2_rpc_url(self)
            token = _HUMANIZED_U2_TRANSPORT.set((self, u2_device, rpc_url))
            try:
                rpc(lambda: self._u2_single_input_rpc(U2_ACTION_DOWN, x, y))
                self.sleep(press_seconds)
                rpc(lambda: self._u2_single_input_rpc(U2_ACTION_UP, x, y))
            finally:
                _HUMANIZED_U2_TRANSPORT.reset(token)
                _remove_humanized_u2_forward(self, forward_port)

        return self._run_humanized_uiautomator2(
            run, lambda: self._click_uiautomator2_legacy_impl(x, y)
        )

    def _long_click_uiautomator2_humanized_impl(self, x, y, duration=1.0):
        # 开档长按（无 @retry，维度 J hold 微颤）：plan_hold 返回 None
        # （off/'none'/预算过短）时单次调用无装饰 legacy。点数 cap=20：
        # 逐点 HTTP RPC 一个往返 1~10ms，1s hold 也只发 20 个 MOVE，
        # 每点有效间隔 ~50ms——回报率虽低但事件流不再死寂（死寂才是指纹）。
        # 墙钟注意：hold 段 = sum(sleeps) + 20×RPC 往返，0.5s hold 实际最长
        # 拉长 ~0.2s（+40%）——业务时长只会变长不会缩短，长按不会被取消。
        plan = self.humanizer.plan_hold(
            (int(x), int(y)), float(duration), point_cap=20)
        if plan is None:
            return self._long_click_uiautomator2_legacy_impl(x, y, duration)

        def run(rpc):
            # 同 click：u2 初始化和 USB forward 均发生在首个 RPC 之前；失败归 B0，
            # 事件发送阶段只使用已准备好的 session 与绝对 URL。
            u2_device = _humanized_u2_device(self)
            rpc_url, forward_port = _humanized_u2_rpc_url(self)
            token = _HUMANIZED_U2_TRANSPORT.set((self, u2_device, rpc_url))
            try:
                rpc(lambda: self._u2_single_input_rpc(U2_ACTION_DOWN, x, y))
                # delays[i] 是发送 points[i] 前的等待（全局契约 4）
                for (px, py), dt in zip(plan.points, plan.delays):
                    self.sleep(dt)
                    rpc(lambda: self._u2_single_input_rpc(U2_ACTION_MOVE, px, py))
                rpc(lambda: self._u2_single_input_rpc(U2_ACTION_UP, x, y))
            finally:
                _HUMANIZED_U2_TRANSPORT.reset(token)
                _remove_humanized_u2_forward(self, forward_port)

        return self._run_humanized_uiautomator2(
            run, lambda: self._long_click_uiautomator2_legacy_impl(x, y, duration)
        )

    def _drag_along_impl(self, path, verbose=True, delay_before_move=False, emit_input=None):
        # 无装饰逐点拖拽：默认参数保持 drag_uiautomator2 的事件后等待与日志行为；
        # humanized swipe 传 verbose=False、delay_before_move=True、emit_input 接
        # 单次 RPC。delay_before_move=True 时首项只 DOWN、中间项先 sleep 自身 second
        # 再 MOVE、末项只 UP——delays[0] 只在首个 MOVE 前消费一次、UP 后不等待
        # （Plan 契约 4）。默认 emit 经 u2.touch.* 保留 legacy 路径。
        def emit(action, x, y):
            if emit_input is not None:
                return emit_input(action, x, y)
            if action == U2_ACTION_DOWN:
                return self.u2.touch.down(x, y)
            if action == U2_ACTION_MOVE:
                return self.u2.touch.move(x, y)
            return self.u2.touch.up(x, y)

        last_index = len(path) - 1
        for index, (x, y, second) in enumerate(path):
            if delay_before_move and 0 < index < last_index:
                self.sleep(second)
            if index == 0:
                emit(U2_ACTION_DOWN, x, y)
                if verbose:
                    logger.info(point2str(x, y) + ' down')
            elif index == last_index:
                emit(U2_ACTION_UP, x, y)
                if verbose:
                    logger.info(point2str(x, y) + ' up')
            else:
                emit(U2_ACTION_MOVE, x, y)
                if verbose:
                    logger.info(point2str(x, y) + ' move')
            if not delay_before_move:
                self.sleep(second)

    @retry
    def _drag_along(self, path):
        # 公开 @retry 版本只包装无装饰 legacy impl，供既有 drag_uiautomator2 使用
        return self._drag_along_impl(path)

    def _swipe_uiautomator2_humanized_impl(self, p1, p2, duration=0.1):
        # 开档滑动（无 @retry）：plan_swipe 消费 C/D；None（off/越界/几何失败）
        # 时单次调用无装饰 legacy。path 结构：[起点(0)] + 每个计划点前带 delay +
        # 末尾 target 的 UP 哨兵(0)——points 已含 target，故只补 UP 不补第二个 MOVE。
        # 预算 = 调用方 duration（legacy 总时长语义）：budget = base × 12。此前
        # base=duration 直接传 → 预算 = duration×12（2s 滑动膨胀成 24s）。
        # point_cap：u2 逐点走 HTTP RPC，一个 HTTP 往返 1~10ms，回报率高了
        # 物理上投递不出来，限到 100 点（100 点/2s ≈ 50Hz 有效回报率）
        plan = self.humanizer.plan_swipe(
            p1, p2, base_delay_s=duration / timing.PROFILE_MAX_POINTS,
            point_cap=100)
        if plan is None:
            return self._swipe_uiautomator2_legacy_impl(p1, p2, duration=duration)
        path = [
            (*p1, 0),
            *[(x, y, delay) for (x, y), delay in zip(plan.points, plan.delays)],
            (*p2, 0),
        ]

        def run(rpc):
            # 同 click：真实开档不触发 u2 cached_property，fake 仍可复用已注入对象。
            u2_device = _humanized_u2_device(self)
            rpc_url, forward_port = _humanized_u2_rpc_url(self)
            token = _HUMANIZED_U2_TRANSPORT.set((self, u2_device, rpc_url))

            def emit_input(action, x, y):
                return rpc(lambda: self._u2_single_input_rpc(action, x, y))

            try:
                return self._drag_along_impl(
                    path, verbose=False, delay_before_move=True, emit_input=emit_input
                )
            finally:
                _HUMANIZED_U2_TRANSPORT.reset(token)
                _remove_humanized_u2_forward(self, forward_port)

        return self._run_humanized_uiautomator2(
            run,
            lambda: self._swipe_uiautomator2_legacy_impl(p1, p2, duration=duration),
        )

    def drag_uiautomator2(self, p1, p2, segments=1, shake=(0, 15), point_random=(-10, -10, 10, 10),
                          shake_random=(-5, -5, 5, 5), swipe_duration=0.25, shake_duration=0.1):
        """Drag and shake, like:
                     /\
        +-----------+  +  +
                        \/
        A simple swipe or drag don't work well, because it only has two points.
        Add some way point to make it more like swipe.

        Args:
            p1 (tuple): Start point, (x, y).
            p2 (tuple): End point, (x, y).
            segments (int):
            shake (tuple): Shake after arrive end point.
            point_random: Add random to start point and end point.
            shake_random: Add random to shake array.
            swipe_duration: Duration between way points.
            shake_duration: Duration between shake points.
        """
        p1 = np.array(p1) - random_rectangle_point(point_random)
        p2 = np.array(p2) - random_rectangle_point(point_random)
        path = [(x, y, swipe_duration) for x, y in random_line_segments(p1, p2, n=segments, random_range=point_random)]
        path += [
            (*p2 + shake + random_rectangle_point(shake_random), shake_duration),
            (*p2 - shake - random_rectangle_point(shake_random), shake_duration),
            (*p2, shake_duration)
        ]
        path = [(int(x), int(y), d) for x, y, d in path]
        self._drag_along(path)

    @retry
    def app_current_uiautomator2(self):
        """
        Returns:
            str: Package name.
        """
        result = self.u2.app_current()
        return result['package']

    @retry
    def app_start_uiautomator2(self, package_name=None):
        if not package_name:
            package_name = self.package
        try:
            self.u2.app_start(package_name)
        except u2.exceptions.BaseError as e:
            # BaseError: package "com.bilibili.azurlane" not found
            logger.error(e)
            raise PackageNotInstalled(package_name)

    @retry
    def app_stop_uiautomator2(self, package_name=None):
        if not package_name:
            package_name = self.package
        self.u2.app_stop(package_name)

    @retry
    def dump_hierarchy_uiautomator2(self) -> etree._Element:
        content = self.u2.dump_hierarchy(compressed=True)
        hierarchy = etree.fromstring(content.encode('utf-8'))
        return hierarchy

    @retry
    def resolution_uiautomator2(self, cal_rotation=True) -> t.Tuple[int, int]:
        """
        Faster u2.window_size(), cause that calls `dumpsys display` twice.

        Returns:
            (width, height)
        """
        info = self.u2.http.get('/info').json()
        w, h = info['display']['width'], info['display']['height']
        if cal_rotation:
            rotation = self.get_orientation()
            if (w > h) != (rotation % 2 == 1):
                w, h = h, w
        return w, h

    def resolution_check_uiautomator2(self):
        """
        Alas does not actively check resolution but the width and height of screenshots.
        However, some screenshot methods do not provide device resolution, so check it here.

        Returns:
            (width, height)

        Raises:
            RequestHumanTakeover: If resolution is not 1280x720
        """
        width, height = self.resolution_uiautomator2()
        logger.attr('Screen_size', f'{width}x{height}')
        if width == 1280 and height == 720:
            return (width, height)
        if width == 720 and height == 1280:
            return (width, height)

        logger.critical(f'Resolution not supported: {width}x{height}')
        logger.critical('Please set emulator resolution to 1280x720')
        raise RequestHumanTakeover

    @retry
    def proc_list_uiautomator2(self) -> t.List[ProcessInfo]:
        """
        Get info about current processes.
        """
        resp = self.u2.http.get("/proc/list", timeout=10)
        resp.raise_for_status()
        result = [
            ProcessInfo(
                pid=proc['pid'],
                ppid=proc['ppid'],
                thread_count=proc['threadCount'],
                cmdline=' '.join(proc['cmdline']) if proc['cmdline'] is not None else '',
                name=proc['name'],
            ) for proc in resp.json()
        ]
        return result

    @retry
    def u2_shell_background(self, cmdline, timeout=10) -> ShellBackgroundResponse:
        """
        Run at background.

        Note that this function will always return a success response,
        as this is a untested and hidden method in ATX.
        """
        if isinstance(cmdline, (list, tuple)):
            cmdline = list2cmdline(cmdline)
        elif isinstance(cmdline, str):
            cmdline = cmdline
        else:
            raise TypeError("cmdargs type invalid", type(cmdline))

        data = dict(command=cmdline, timeout=str(timeout))
        ret = self.u2.http.post("/shell/background", data=data, timeout=timeout + 10)
        ret.raise_for_status()

        resp = ret.json()
        resp = ShellBackgroundResponse(
            success=bool(resp.get('success', False)),
            pid=resp.get('pid', 0),
            description=resp.get('description', '')
        )
        return resp

if __name__ == '__main__':
    ui2 = Uiautomator2(config='oas1')
    cv2.imshow("iiii", ui2.screenshot_uiautomator2())
    cv2.waitKey(0)
