import asyncio
import json
import re
import socket
import subprocess
import sys
import time
import numpy as np
from functools import wraps
from typing import List

import websockets
from adbutils.errors import AdbError
import uiautomator2 as u2
from uiautomator2 import _Service
from websockets.exceptions import WebSocketException
from urllib3.util import Timeout as Urllib3Timeout

from module.base.decorator import Config, cached_property, del_cached_property
from module.base.timer import Timer
from module.base.utils import random_rectangle_point
from module.device.connection import Connection
from module.device.humanize import timing
from module.device.method.utils import (
    RETRY_TRIES, retry_sleep, handle_adb_error, random_port, humanized_http_request,
)
from module.exception import RequestHumanTakeover, ScriptError
from module.logger import logger


def random_normal_distribution(a, b, n=5):
    output = np.mean(np.random.uniform(a, b, size=n))
    return output


def random_theta():
    theta = np.random.uniform(0, 2 * np.pi)
    return np.array([np.sin(theta), np.cos(theta)])


def random_rho(dis):
    return random_normal_distribution(-dis, dis)


def insert_swipe(p0, p3, speed=15, min_distance=10):
    """
    Insert way point from start to end.
    First generate a cubic bézier curve

    Args:
        p0: Start point.
        p3: End point.
        speed: Average move speed, pixels per 10ms.
        min_distance:

    Returns:
        list[list[int]]: List of points.

    Examples:
        > insert_swipe((400, 400), (600, 600), speed=20)
        [[400, 400], [406, 406], [416, 415], [429, 428], [444, 442], [462, 459], [481, 478], [504, 500], [527, 522],
        [545, 540], [560, 557], [573, 570], [584, 582], [592, 590], [597, 596], [600, 600]]
    """
    p0 = np.array(p0)
    p3 = np.array(p3)

    # Random control points in Bézier curve
    distance = np.linalg.norm(p3 - p0)
    p1 = 2 / 3 * p0 + 1 / 3 * p3 + random_theta() * random_rho(distance * 0.1)
    p2 = 1 / 3 * p0 + 2 / 3 * p3 + random_theta() * random_rho(distance * 0.1)

    # Random `t` on Bézier curve, sparse in the middle, dense at start and end
    segments = max(int(distance / speed) + 1, 5)
    lower = random_normal_distribution(-85, -60)
    upper = random_normal_distribution(80, 90)
    theta = np.arange(lower + 0., upper + 0.0001, (upper - lower) / segments)
    ts = np.sin(theta / 180 * np.pi)
    ts = np.sign(ts) * abs(ts) ** 0.9
    ts = (ts - min(ts)) / (max(ts) - min(ts))

    # Generate cubic Bézier curve
    points = []
    prev = (-100, -100)
    for t in ts:
        point = p0 * (1 - t) ** 3 + 3 * p1 * t * (1 - t) ** 2 + 3 * p2 * t ** 2 * (1 - t) + p3 * t ** 3
        point = point.astype(int).tolist()
        if np.linalg.norm(np.subtract(point, prev)) < min_distance:
            continue

        points.append(point)
        prev = point

    # Delete nearing points
    if len(points[1:]):
        distance = np.linalg.norm(np.subtract(points[1:], points[0]), axis=1)
        mask = np.append(True, distance > min_distance)
        points = np.array(points)[mask].tolist()
    else:
        points = [p0, p3]

    return points

def smooth_path(points: list, min_distance: float = 30.0, offset_range: float = 3.0):
    """
    路径平滑，基于路径方向的垂直偏移
    Args:
        points: 原始点列表 [(x1, y1), (x2, y2), ...]
        min_distance: 最小插点距离
        offset_range: 垂直偏移范围

    Returns:
        list: 平滑后的点列表
    """
    if len(points) < 2:
        return points

    smooth_points = [points[0]]

    for i in range(len(points) - 1):
        start = np.array(points[i])
        end = np.array(points[i + 1])

        direction = end - start
        distance = np.linalg.norm(direction)

        if distance <= min_distance:
            continue

        # 归一化方向向量
        unit_direction = direction / distance
        # 计算垂直向量
        perpendicular = np.array([-unit_direction[1], unit_direction[0]])

        num_segments = int(distance / min_distance)

        for j in range(1, num_segments + 1):
            ratio = j / num_segments
            interpolated = start + ratio * direction

            # 垂直方向的随机偏移
            perpendicular_offset = np.random.uniform(-offset_range, offset_range)
            final_point = interpolated + perpendicular_offset * perpendicular

            smooth_points.append((int(final_point[0]), int(final_point[1])))

    if smooth_points[-1] != points[-1]:
        smooth_points.append(points[-1])

    return smooth_points

# ---------------------------------------------------------------------------
# B 类投递异常恢复（Plan Task 16 契约 #11）：minitouch 只能以「可验证的 session
# reset + 新连接 ready」作为 B1 恢复判据；builder.reset() / minitouch_send() 返回、
# cleanup=True 或 cache 重建都不是成功事件。所有 deadline 一律用 time.monotonic()，
# 不受系统时间校正影响。
MINITOUCH_RECOVERY_POLL_S = 0.05
MINITOUCH_RECOVERY_HTTP_CONNECT_TIMEOUT_S = 1.0
MINITOUCH_RECOVERY_HTTP_CALL_TIMEOUT_S = 1.0
MINITOUCH_RECOVERY_STOP_TIMEOUT_S = 3.0
MINITOUCH_RECOVERY_START_TIMEOUT_S = 3.0
MINITOUCH_RECOVERY_WS_CONNECT_TIMEOUT_S = 3.0
MINITOUCH_RECOVERY_WS_READY_TIMEOUT_S = 1.0
MINITOUCH_RECOVERY_WS_CLOSE_TIMEOUT_S = 1.0
MINITOUCH_RECOVERY_TCP_CONNECT_TIMEOUT_S = 3.0
# B1 恢复的 ATX 停启段独立预算：不与 forward/握手共享同一 deadline。若共用，网络
# 抖动下两次 adb shell 往返可能吃光整段预算，让本可成功的握手必然超时。
# 取值有意放宽：ATX 停启走两次 adb shell 往返，是全流程中最慢的一段，而恢复失败
# 即实例停摆，代价远高于多等几秒。
MINITOUCH_RECOVERY_ATX_RESTART_TIMEOUT_S = 10.0
MINITOUCH_RECOVERY_TCP_LINE_TIMEOUT_S = 1.0
# 握手读到 EOF 后经 atx-agent 拉起 minitouch 服务的整体预算，首连与 B1 共用
# （临时 forward 创建 + 状态查询 + POST 启动 + running 轮询）。这段独立起算，
# 不与 forward/握手段共享 deadline，否则会被其剩余预算截断在半途。
MINITOUCH_SERVICE_ENSURE_TIMEOUT_S = 5.0
# 单批 WebSocket 投递的墙钟上限，避免 send() 阻塞时永远无法进入 B1 接管。
MINITOUCH_WS_SEND_TIMEOUT_S = 1.0

# TCP 握手三行各自的严格 fullmatch 校验（v <版本> / ^ <触点 宽 高 压> / $ <pid>），
# 不得复用 minitouch_init() 的宽松 split/日志路径。
_MINITOUCH_VERSION_RE = re.compile(r"v [1-9][0-9]*")
_MINITOUCH_CAPABILITY_RE = re.compile(
    # minitouch 的压力上限允许为 0（实际设备会返回 ``^ 10 720 1280 0``），
    # 触点数和坐标范围仍必须是正整数。
    r"\^ [1-9][0-9]* [1-9][0-9]* [1-9][0-9]* (?:0|[1-9][0-9]*)"
)
_MINITOUCH_PID_RE = re.compile(r"\$ ([1-9][0-9]*)")
_MINITOUCH_ORIENTATION_RE = re.compile(
    r'.*DisplayViewport{.*valid=true, .*orientation=(?P<orientation>\d+), '
    r'.*deviceWidth=\d+, deviceHeight=\d+.*'
)


def _humanized_minitouch_ws_url(serial):
    """把 minitouch 的 HTTP 服务地址转换为对应的 WebSocket 地址。"""
    if serial.startswith('https://'):
        return 'wss://' + serial[len('https://'):].rstrip('/') + '/minitouch'
    if serial.startswith('http://'):
        return 'ws://' + serial[len('http://'):].rstrip('/') + '/minitouch'
    return serial.rstrip('/') + '/minitouch'


def _parse_humanized_orientation(output):
    """从 dumpsys display 输出解析 0~3 方向，无法确认时返回 None。"""
    match = _MINITOUCH_ORIENTATION_RE.search(output or '')
    if match is None:
        return None
    orientation = int(match.group('orientation'))
    return orientation if type(orientation) is int and orientation in (0, 1, 2, 3) else None

class _MinitouchRecoveryFailed(RuntimeError):
    """B1 恢复流程内部失败标记：任一事件缺失/超时/非法即抛出，由调用方转 RequestHumanTakeover。"""


def _recovery_remaining(deadline):
    """B1 恢复阶段的剩余时间（秒）；超时立即抛 _MinitouchRecoveryFailed。"""
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise _MinitouchRecoveryFailed('minitouch recovery deadline exceeded')
    return remaining


def _adb_listener_missing(exc):
    """判定 adb 的报错是否为「该 listener 不存在」。

    实测输出形如 ``adb.exe: error: listener 'tcp:20529' not found``（退出码 1）。
    stderr 未开 text=True 时是 bytes，统一解码后再做子串匹配。
    """
    stderr = exc.stderr
    if isinstance(stderr, bytes):
        stderr = stderr.decode('utf-8', 'replace')
    return 'not found' in (stderr or '')


def _run_adb_recovery_command(device, args, deadline, stage, tolerate_missing_listener=False):
    """用可终止的 adb 子进程执行恢复命令，保证超时后不会留下后台 I/O。

    tolerate_missing_listener 只给 ``forward --remove`` 用：该步骤的目标是
    「确保目标 forward 不存在」，listener 本就不存在即目标已达成，属幂等成功。
    把它当失败会在最需要容错的时刻——socket 刚被中止、adb 侧 forward 记录已经
    消失——把一次可自愈的断连升级成 RequestHumanTakeover。
    """
    timeout = _recovery_remaining(deadline)
    try:
        subprocess.run(
            [device.adb_binary, '-s', device.serial, *map(str, args)],
            check=True,
            timeout=timeout,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            # 无控制台宿主（pythonw 启动的 server/GUI）下抑制 adb 的 cmd 窗口闪烁
            creationflags=subprocess.CREATE_NO_WINDOW if sys.platform.startswith('win') else 0,
        )
    except subprocess.CalledProcessError as exc:
        if tolerate_missing_listener and _adb_listener_missing(exc):
            return
        raise _MinitouchRecoveryFailed(f'{stage} failed') from exc
    except (subprocess.TimeoutExpired, OSError) as exc:
        raise _MinitouchRecoveryFailed(f'{stage} failed') from exc


def _quantize_move_delays(delays):
    """把整条 delays 量化成整数毫秒，做累计余量结转（Plan 契约 #6）。

    输入是 MovePlan 的完整浮点 delays（秒）；输出是与输入等长的整数毫秒列表：
    0 delay 输出 0（不生成 w），正 delay 至少 1ms，整条输出总和严格等于
    floor(sum(delays) * 1000 + 0.5)。目标总毫秒小于正 delay 数时返回 None——
    该点数不可表示，禁止用 max(ms, 1) 静默放大预算，交由上层降点或回退。
    """
    positive = [d for d in delays if d > 0]
    target_total_ms = int(sum(delays) * 1000 + 0.5)
    if target_total_ms < len(positive):
        return None
    # 每个正 delay 预留 1ms，剩余毫秒按 max(d_i*1000 - 1, 0) 的比例用最大余数法
    # 补齐；权重相同按原索引稳定打破平局（Python sort 稳定性保证）。
    base = [1 if d > 0 else 0 for d in delays]
    remaining = target_total_ms - len(positive)
    extra = [0] * len(delays)
    weights = [0.0 if d <= 0 else max(d * 1000.0 - 1.0, 0.0) for d in delays]
    total_w = sum(weights)
    if total_w > 0 and remaining > 0:
        exact = [w / total_w * remaining for w in weights]
        floors = [int(v) for v in exact]
        deficit = remaining - sum(floors)
        # 余数最大的索引先补齐；零权重（零 delay）的余数恒为 0，不会入选
        order = sorted(range(len(exact)), key=lambda i: exact[i] - floors[i], reverse=True)
        for i in order[:deficit]:
            floors[i] += 1
        extra = floors
    return [b + e for b, e in zip(base, extra)]


class Command:
    def __init__(
            self,
            operation: str,
            contact: int = 0,
            x: int = 0,
            y: int = 0,
            ms: int = 10,
            pressure: int = 100
    ):
        """
        See https://github.com/openstf/minitouch#writable-to-the-socket

        Args:
            operation: c, r, d, m, u, w
            contact:
            x:
            y:
            ms:
            pressure:
        """
        self.operation = operation
        self.contact = contact
        self.x = x
        self.y = y
        self.ms = ms
        self.pressure = pressure

    def to_minitouch(self) -> str:
        """
        String that write into minitouch socket
        """
        if self.operation == 'c':
            return f'{self.operation}\n'
        elif self.operation == 'r':
            return f'{self.operation}\n'
        elif self.operation == 'd':
            return f'{self.operation} {self.contact} {self.x} {self.y} {self.pressure}\n'
        elif self.operation == 'm':
            return f'{self.operation} {self.contact} {self.x} {self.y} {self.pressure}\n'
        elif self.operation == 'u':
            return f'{self.operation} {self.contact}\n'
        elif self.operation == 'w':
            return f'{self.operation} {self.ms}\n'
        else:
            return ''

    def to_atx_agent(self, max_x=1280, max_y=720) -> str:
        """
        Dict that send to atx-agent, $DEVICE_URL/minitouch
        See https://github.com/openatx/atx-agent#minitouch%E6%93%8D%E4%BD%9C%E6%96%B9%E6%B3%95
        """
        x, y = self.x / max_x, self.y / max_y
        if self.operation == 'c':
            out = dict(operation=self.operation)
        elif self.operation == 'r':
            out = dict(operation=self.operation)
        elif self.operation == 'd':
            out = dict(operation=self.operation, index=self.contact, pressure=self.pressure, xP=x, yP=y)
        elif self.operation == 'm':
            out = dict(operation=self.operation, index=self.contact, pressure=self.pressure, xP=x, yP=y)
        elif self.operation == 'u':
            out = dict(operation=self.operation, index=self.contact)
        elif self.operation == 'w':
            out = dict(operation=self.operation, milliseconds=self.ms)
        else:
            out = dict()
        return json.dumps(out)


class CommandBuilder:
    """Build command str for minitouch.

    You can use this, to custom actions as you wish::

        with safe_connection(_DEVICE_ID) as connection:
            builder = CommandBuilder()
            builder.down(0, 400, 400, 50)
            builder.commit()
            builder.move(0, 500, 500, 50)
            builder.commit()
            builder.move(0, 800, 400, 50)
            builder.commit()
            builder.up(0)
            builder.commit()
            builder.publish(connection)

    """
    DEFAULT_DELAY = 0.05
    max_x = 1280
    max_y = 720

    def __init__(self, device):
        """
        Args:
            device:
        """
        self.device = device
        self.commands = []
        self.delay = 0

    def convert(self, x, y):
        max_x, max_y = self.device.max_x, self.device.max_y
        orientation = self.device.orientation

        if orientation == 0:
            pass
        elif orientation == 1:
            x, y = 720 - y, x
            max_x, max_y = max_y, max_x
        elif orientation == 2:
            x, y = 1280 - x, 720 - y
        elif orientation == 3:
            x, y = y, 1280 - x
            max_x, max_y = max_y, max_x
        else:
            raise ScriptError(f'Invalid device orientation: {orientation}')

        self.max_x, self.max_y = max_x, max_y
        if not self.device.config.DEVICE_OVER_HTTP:
            # Maximum X and Y coordinates may, but usually do not, match the display size.
            x, y = int(x / 1280 * max_x), int(y / 720 * max_y)
        else:
            # When over http, max_x and max_y are default to 1280 and 720, skip matching display size
            x, y = int(x), int(y)
        return x, y

    def commit(self):
        """ add minitouch command: 'c\n' """
        self.commands.append(Command('c'))
        return self

    def reset(self):
        """ add minitouch command: 'r\n' """
        self.commands.append(Command('r'))
        return self

    def wait(self, ms=10):
        """ add minitouch command: 'w <ms>\n' """
        self.commands.append(Command('w', ms=ms))
        self.delay += ms
        return self

    def up(self, contact=0):
        """ add minitouch command: 'u <contact>\n' """
        self.commands.append(Command('u', contact=contact))
        return self

    def down(self, x, y, contact=0, pressure=100):
        """ add minitouch command: 'd <contact> <x> <y> <pressure>\n' """
        x, y = self.convert(x, y)
        self.commands.append(Command('d', x=x, y=y, contact=contact, pressure=pressure))
        return self

    def move(self, x, y, contact=0, pressure=100):
        """ add minitouch command: 'm <contact> <x> <y> <pressure>\n' """
        x, y = self.convert(x, y)
        self.commands.append(Command('m', x=x, y=y, contact=contact, pressure=pressure))
        return self

    def clear(self):
        """ clear current commands """
        self.commands = []
        self.delay = 0

    def to_minitouch(self) -> str:
        return ''.join([command.to_minitouch() for command in self.commands])

    def to_atx_agent(self) -> List[str]:
        return [command.to_atx_agent(self.max_x, self.max_y) for command in self.commands]


class MinitouchNotInstalledError(Exception):
    pass


class MinitouchOccupiedError(Exception):
    pass


# 投递中被 _run_humanized_minitouch 归类为 B 类（写入已开始 / 结果未知）的异常。
# 必须定义在 MinitouchNotInstalledError / MinitouchOccupiedError 之后。
_HUMANIZED_MINITOUCH_TRANSPORT_ERRORS = (
    ConnectionError,
    BrokenPipeError,
    OSError,
    TimeoutError,
    asyncio.TimeoutError,
    socket.timeout,
    WebSocketException,
    AdbError,
    MinitouchNotInstalledError,
    MinitouchOccupiedError,
)


class U2Service(_Service):
    def __init__(self, name, u2obj=None, service_url=None):
        self.name = name
        self.u2obj = u2obj
        if service_url is not None:
            self.service_url = service_url
        else:
            self.service_url = self.u2obj.path2url("/services/" + name)


def _humanized_http_timeout(http, connect, read, total):
    """真实 uiautomator2 session 使用 urllib3 总 deadline，测试 fake 保留 tuple 契约。"""
    if isinstance(http, getattr(u2, '_AgentRequestSession', ())):
        return Urllib3Timeout(connect=connect, read=read, total=total)
    return (connect, read)


def retry(func):
    @wraps(func)
    def retry_wrapper(self, *args, **kwargs):
        """
        Args:
            self (Minitouch):
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
            # Emulator closed
            except ConnectionAbortedError as e:
                logger.error(e)

                def init():
                    self.adb_reconnect()
            # MinitouchNotInstalledError: Received empty data from minitouch
            except MinitouchNotInstalledError as e:
                logger.error(e)

                def init():
                    self.install_uiautomator2()
                    if self._minitouch_port:
                        self.adb_forward_remove(f'tcp:{self._minitouch_port}')
                    del_cached_property(self, 'minitouch_builder')
            # MinitouchOccupiedError: Timeout when connecting to minitouch
            except MinitouchOccupiedError as e:
                logger.error(e)

                def init():
                    self.restart_atx()
                    if self._minitouch_port:
                        self.adb_forward_remove(f'tcp:{self._minitouch_port}')
                    del_cached_property(self, 'minitouch_builder')
            # AdbError
            except AdbError as e:
                if handle_adb_error(e):
                    def init():
                        self.adb_reconnect()
                else:
                    break
            except BrokenPipeError as e:
                logger.error(e)

                def init():
                    del_cached_property(self, 'minitouch_builder')
            # Unknown, probably a trucked image
            except Exception as e:
                logger.exception(e)

                def init():
                    pass

        logger.critical(f'Retry {func.__name__}() failed')
        raise RequestHumanTakeover

    return retry_wrapper


class Minitouch(Connection):
    _minitouch_port: int = 0
    _minitouch_client: socket.socket
    _minitouch_pid: int
    _minitouch_ws: websockets.WebSocketClientProtocol
    max_x: int
    max_y: int
    # 设备能力协商的最大压力值（^ banner）；默认 50 对应 MuMu 内置 minitouch，
    # TCP 握手成功后被真实值覆盖。压力随机化以它为量程基准
    max_pressure: int = 50
    # 首连失败后是否已做过 atx-agent 补救（每个 Device 实例只做一次）
    _atx_agent_ensure_tried: bool = False

    @cached_property
    def minitouch_builder(self):
        self.minitouch_init()
        return CommandBuilder(self)

    @Config.when(DEVICE_OVER_HTTP=False)
    def minitouch_init(self):
        logger.hr('MiniTouch init')
        max_x, max_y = 1280, 720
        max_contacts = 2
        max_pressure = 50
        self.get_orientation()

        self._minitouch_port = self.adb_forward("localabstract:minitouch")

        # No need, minitouch already started by uiautomator2
        # self.adb_shell([self.config.MINITOUCH_FILEPATH_REMOTE])

        retry_timeout = Timer(2).start()
        while 1:
            client = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            client.settimeout(1)
            client.connect(('127.0.0.1', self._minitouch_port))
            self._minitouch_client = client

            # get minitouch server info
            socket_out = client.makefile()

            # v <version>
            # protocol version, usually it is 1. needn't use this
            try:
                out = socket_out.readline().replace("\n", "").replace("\r", "")
            except socket.timeout:
                client.close()
                raise MinitouchOccupiedError(
                    'Timeout when connecting to minitouch, '
                    'probably because another connection has been established'
                )
            logger.info(out)

            # ^ <max-contacts> <max-x> <max-y> <max-pressure>
            out = socket_out.readline().replace("\n", "").replace("\r", "")
            logger.info(out)
            try:
                _, max_contacts, max_x, max_y, max_pressure, *_ = out.split(" ")
                break
            except ValueError:
                client.close()
                if retry_timeout.reached():
                    raise MinitouchNotInstalledError(
                        'Received empty data from minitouch, '
                        'probably because minitouch is not installed'
                    )
                else:
                    # Minitouch may not start that fast
                    self.sleep(1)
                    continue

        # self.max_contacts = max_contacts
        self.max_x = int(max_x)
        self.max_y = int(max_y)
        # 记录压力量程：压力随机化（humanized 路径）按它归一，避免发出 >max 被钳到 1.0
        self.max_pressure = int(max_pressure)

        # $ <pid>
        out = socket_out.readline().replace("\n", "").replace("\r", "")
        logger.info(out)
        _, pid = out.split(" ")
        self._minitouch_pid = pid

        logger.info(
            "minitouch running on port: {}, pid: {}".format(self._minitouch_port, self._minitouch_pid)
        )
        logger.info(
            "max_contact: {}; max_x: {}; max_y: {}; max_pressure: {}".format(
                max_contacts, max_x, max_y, max_pressure
            )
        )

    @Config.when(DEVICE_OVER_HTTP=False)
    def minitouch_send(self, post_send_gap_s=None):
        content = self.minitouch_builder.to_minitouch()
        # logger.info("send operation: {}".format(content.replace("\n", "\\n")))
        byte_content = content.encode('utf-8')
        self._minitouch_client.sendall(byte_content)
        self._minitouch_client.recv(0)
        # 维度 I：enabled 时 post_send_gap_s 由 gap_seconds(0.05) 提供；None（off /
        # 既有调用）继续使用 DEFAULT_DELAY，保证旧调用与 off 旁路逐事件不变
        if post_send_gap_s is None:
            post_send_gap_s = getattr(self, '_humanized_minitouch_gap_s', None)
        if post_send_gap_s is None:
            post_send_gap_s = self.minitouch_builder.DEFAULT_DELAY
        time.sleep(self.minitouch_builder.delay / 1000 + post_send_gap_s)
        self.minitouch_builder.clear()

    @cached_property
    def _minitouch_loop(self):
        return asyncio.new_event_loop()

    def _minitouch_loop_run(self, event):
        """
        Args:
            event: Async function

        Raises:
            MinitouchOccupiedError
        """
        try:
            return self._minitouch_loop.run_until_complete(event)
        except websockets.ConnectionClosedError as e:
            # 保持既有 off/legacy 语义：连接被远端关闭时交给 retry 的
            # MinitouchOccupiedError 分支清理 builder 并重建连接。
            logger.error(e)
            raise MinitouchOccupiedError(
                'ConnectionClosedError, '
                'probably because another connection has been established'
            )
        except WebSocketException as e:
            # 仅 humanized 无装饰投递收口为 B 类；off/legacy 保持原有异常行为，
            # 避免公开 @retry 重放结果未知的旧批次。
            if not getattr(self, '_humanized_minitouch_transport', False):
                raise
            # WebSocketException 覆盖关闭、握手和 URI/协议错误，统一转为 B 类传输异常。
            logger.error(e)
            raise MinitouchOccupiedError(
                'WebSocket transport error, '
                'probably because another connection has been established'
            )

    @Config.when(DEVICE_OVER_HTTP=True)
    def minitouch_init(self):
        logger.hr('MiniTouch init')
        self.max_x, self.max_y = 1280, 720
        self.get_orientation()

        logger.info('Stop minitouch service')
        s = U2Service('minitouch', self.u2)
        s.stop()
        while 1:
            if not s.running():
                break
            self.sleep(0.05)

        logger.info('Start minitouch service')
        s.start()
        while 1:
            if s.running():
                break
            self.sleep(0.05)

        # 保持 HTTP/HTTPS 的安全级别：HTTPS 必须升级到 WSS，不能降级为明文 WS。
        url = _humanized_minitouch_ws_url(self.serial)
        logger.attr('Minitouch', url)

        async def connect():
            ws = await websockets.connect(url)
            # start @minitouch service
            logger.info(await ws.recv())
            # dial unix:@minitouch
            logger.info(await ws.recv())
            return ws

        self._minitouch_ws = self._minitouch_loop_run(connect())

    @Config.when(DEVICE_OVER_HTTP=True)
    def minitouch_send(self, post_send_gap_s=None):
        content = self.minitouch_builder.to_atx_agent()

        async def send():
            for row in content:
                # logger.info("send operation: {}".format(row.replace("\n", "\\n")))
                await self._minitouch_ws.send(row)

        # 只有 humanized 无装饰路径启用写入 deadline；off/legacy 保持原有
        # minitouch_send 行为，避免把新增超时交给公开 @retry 后重放旧批次。
        if getattr(self, '_humanized_minitouch_transport', False):
            self._minitouch_loop_run(
                asyncio.wait_for(send(), timeout=MINITOUCH_WS_SEND_TIMEOUT_S)
            )
        else:
            self._minitouch_loop_run(send())
        # 维度 I：与 TCP 实现同一口径——enabled 用 gap_seconds(0.05)，None 保持 DEFAULT_DELAY
        if post_send_gap_s is None:
            post_send_gap_s = getattr(self, '_humanized_minitouch_gap_s', None)
        if post_send_gap_s is None:
            post_send_gap_s = self.minitouch_builder.DEFAULT_DELAY
        time.sleep(self.minitouch_builder.delay / 1000 + post_send_gap_s)
        self.minitouch_builder.clear()

    def _click_minitouch_legacy_impl(self, x, y):
        """既有 click 方法体（无 @retry），off/未接入调用方与 B 类回退共用。"""
        builder = self.minitouch_builder
        builder.down(x, y).commit()
        builder.up().commit()
        self.minitouch_send()

    @retry
    def click_minitouch(self, x, y):
        # 公开 @retry 方法只包装 legacy impl：off 与既有调用方可观测行为不变；
        # humanized 路径绝不经由本方法进入（契约 #11，避免 @retry 重放半截手势）
        return self._click_minitouch_legacy_impl(x, y)

    def _long_click_minitouch_legacy_impl(self, x, y, duration=1.0):
        """既有 long_click 方法体（无 @retry）。本 Task 未接入 humanized 长按，
        抽取仅为了保持「公开 @retry 只包装 legacy impl」的拓扑一致。"""
        duration = int(duration * 1000)
        builder = self.minitouch_builder
        builder.down(x, y).commit().wait(duration)
        builder.up().commit()
        self.minitouch_send()

    def _long_click_minitouch_humanized_impl(self, x, y, duration=1.0):
        """开档长按（维度 J hold 微颤）：单批 down → (wait+move)×N → up → send。

        plan_hold 返回 None（off/'none' 策略/预算过短）时走 legacy——注意此时
        可能已消费 hold 权重 RNG，但 legacy 不依赖 RNG，行为仍与 off 一致。
        微颤点用 device_wait（w 由设备端执行，间隔精确）；wait 量化复用
        _quantize_move_delays（契约 #6 口径：w 总和严格等于 floor(sum×1000)，
        不用逐点 max(ms,1) 静默放大预算）。point_cap=200：与典型 1s hold 的
        自然点量持平，2s 高回报率时摊长间隔——DEVICE_OVER_HTTP 模式逐条
        ws.send 包在 1.0s 硬 deadline 里，500 点批（1504 条命令）可能超时
        误判 B 类异常。坐标沿用传入值——minitouch 的 authoring→设备换算
        在 builder 层，不在策略点。
        """
        plan = self.humanizer.plan_hold(
            (int(x), int(y)), float(duration), timing_mode='device_wait',
            point_cap=200)
        if plan is None:
            return self._long_click_minitouch_legacy_impl(x, y, duration)
        # plan_hold 的 device_wait 地板已保证 delays ≥1ms；量化目标必然可表示
        waits = _quantize_move_delays(list(plan.delays))
        if waits is None:
            return self._long_click_minitouch_legacy_impl(x, y, duration)
        gap = self.humanizer.gap_seconds(0.05)
        if gap is not None:
            self._humanized_minitouch_gap_s = gap
        try:
            # 压力随机化：down + 微颤点逐个取值；None 时全部保持恒定
            pressures = self._humanized_pressure_seq(1 + len(plan.points))

            def run_humanized(builder, send):
                if pressures is None:
                    builder.down(x, y).commit()
                else:
                    builder.down(x, y, pressure=pressures[0]).commit()
                # delays[i] 是发送 points[i] 前的等待（全局契约 4）；量化 0 不
                # 产生 w（与 swipe 的 MOVE 批同口径）
                for i, ((px, py), wait_ms) in enumerate(zip(plan.points, waits)):
                    if wait_ms > 0:
                        builder.wait(wait_ms)
                    if pressures is None:
                        builder.move(px, py).commit()
                    else:
                        builder.move(px, py, pressure=pressures[1 + i]).commit()
                builder.up().commit()
                send()

            return self._run_humanized_minitouch(
                run_humanized,
                lambda: self._long_click_minitouch_legacy_impl(x, y, duration),
            )
        finally:
            self.__dict__.pop('_humanized_minitouch_gap_s', None)

    @retry
    def long_click_minitouch(self, x, y, duration=1.0):
        # 公开 @retry 方法只包装 legacy impl（契约 #11）
        return self._long_click_minitouch_legacy_impl(x, y, duration)

    def _swipe_minitouch_legacy_impl(self, p1, p2, duration=None):
        """
        Swipe from one point to another with specified duration if provided

        Args:
            p1: Starting point (x, y)
            p2: Ending point (x, y)
            duration: Duration of the swipe in seconds, if None use default behavior (each step 10ms)
        """
        if duration is not None:
            # Calculate number of points based on duration, with 10ms per step
            # So if duration is 1.0s (1000ms), and each step is 10ms, we need 100 points
            num_points = max(int(duration * 100), 5)  # At least 5 points to ensure smoothness
            points = self._generate_bezier_points(p1, p2, num_points)
        else:
            # Use default algorithm when no duration specified
            points = insert_swipe(p0=p1, p3=p2)

        builder = self.minitouch_builder

        builder.down(*points[0]).commit()
        self.minitouch_send()

        for point in points[1:]:
            builder.move(*point).commit().wait(10)  # Each step still takes 10ms as required
        self.minitouch_send()

        builder.up().commit()
        self.minitouch_send()

    @retry
    def swipe_minitouch(self, p1, p2, duration=None):
        # 公开 @retry 方法只包装 legacy impl（契约 #11），off 可观测行为不变
        return self._swipe_minitouch_legacy_impl(p1, p2, duration)
    
    def _generate_bezier_points(self, p1, p2, num_points):
        """
        Generate swipe points between two points using a simple quadratic Bézier curve
        
        Args:
            p1: Starting point (x, y)
            p2: Ending point (x, y)
            num_points: Number of points to generate along the path
        """
        import numpy as np
        
        x1, y1 = p1
        x2, y2 = p2
        
        # Calculate a control point that creates a slight curve
        # The control point is offset from the midpoint perpendicular to the direction
        mid_x, mid_y = (x1 + x2) / 2, (y1 + y2) / 2
        
        # Vector from p1 to p2
        vec_x, vec_y = x2 - x1, y2 - y1
        # Perpendicular vector (rotated 90 degrees)
        perp_x, perp_y = -vec_y, vec_x
        
        # Normalize and scale the perpendicular vector
        length = np.sqrt(perp_x**2 + perp_y**2)
        if length > 0:
            perp_x, perp_y = perp_x / length, perp_y / length
        
        # Create control point with some offset (creating a slight curve)
        curve_strength = min(abs(x2-x1), abs(y2-y1)) * 0.2  # Adjust strength based on distance
        ctrl_x = mid_x + perp_x * curve_strength
        ctrl_y = mid_y + perp_y * curve_strength
        
        points = []
        for i in range(num_points):
            t = i / (num_points - 1) if num_points > 1 else 0
            
            # Quadratic Bézier formula: B(t) = (1-t)²P0 + 2(1-t)tP1 + t²P2
            # Where P0 is start point, P1 is control point, P2 is end point
            x = (1 - t)**2 * x1 + 2 * (1 - t) * t * ctrl_x + t**2 * x2
            y = (1 - t)**2 * y1 + 2 * (1 - t) * t * ctrl_y + t**2 * y2
            
            points.append((int(x), int(y)))
        
        return points

    # ------------------------------------------------------------------
    # B 类恢复与 humanized 投递（Plan Task 16，契约 #11）。以下方法全部无 @retry：
    # 由 Control 的 humanized_*_methods 在 enabled 时直达，绝不经由公开 @retry 方法。
    # ------------------------------------------------------------------

    def _read_humanized_minitouch_line(self, stream, client, deadline, stage):
        client.settimeout(min(MINITOUCH_RECOVERY_TCP_LINE_TIMEOUT_S, _recovery_remaining(deadline)))
        try:
            line = stream.readline()
        except (OSError, socket.timeout) as exc:
            raise _MinitouchRecoveryFailed(f'{stage} handshake read failed') from exc
        if not line:
            raise _MinitouchRecoveryFailed(f'{stage} handshake line missing')
        # readline 可能被逐字节滴流延长；返回后再次检查同一全局 deadline，
        # 不允许过期握手被当作新会话证据。
        _recovery_remaining(deadline)
        return line.rstrip('\r\n')

    def _validate_humanized_minitouch_handshake(self, version, capability, pid_line, old_pid):
        if _MINITOUCH_VERSION_RE.fullmatch(version) is None:
            raise _MinitouchRecoveryFailed(f'invalid minitouch version line: {version!r}')
        capability_match = _MINITOUCH_CAPABILITY_RE.fullmatch(capability)
        if capability_match is None:
            raise _MinitouchRecoveryFailed(f'invalid minitouch capability line: {capability!r}')
        pid_match = _MINITOUCH_PID_RE.fullmatch(pid_line)
        if pid_match is None:
            raise _MinitouchRecoveryFailed(f'invalid minitouch pid line: {pid_line!r}')

        _, contacts, max_x, max_y, pressure = capability.split()
        del contacts, pressure
        new_pid = pid_match.group(1)
        if new_pid == str(old_pid):
            raise _MinitouchRecoveryFailed('minitouch pid did not change after recovery')
        return int(max_x), int(max_y), new_pid

    def _recover_humanized_minitouch_tcp(self, old_pid, deadline=None, restart_atx=True):
        # TCP 建连证据：清理旧 forward → 新 forward → 严格三行握手。
        # 已发生输入的 B1 恢复还要先重启 ATX 并验证 PID 改变；首次开档时
        # uiautomator2 刚完成 ATX 初始化，不能再次 stop/start，否则 minitouch
        # 服务可能尚未重新挂载，首个点击会在握手超时后被误判为人工接管。
        if restart_atx:
            # ATX 停启单独计时，不与 forward/握手共享 deadline
            atx_deadline = time.monotonic() + MINITOUCH_RECOVERY_ATX_RESTART_TIMEOUT_S
            atx_agent_path = '/data/local/tmp/atx-agent'
            _run_adb_recovery_command(
                self, ['shell', atx_agent_path, 'server', '--stop'], atx_deadline, 'tcp-restart-atx-stop')
            _run_adb_recovery_command(
                self,
                ['shell', atx_agent_path, 'server', '--nouia', '-d', '--addr', '127.0.0.1:7912'],
                atx_deadline,
                'tcp-restart-atx-start',
            )
        else:
            logger.info('Skip ATX restart for first minitouch connection')
        if deadline is None:
            # forward 增删与三行握手共用这段预算，从 ATX 恢复结束之后起算
            deadline = time.monotonic() + MINITOUCH_RECOVERY_TCP_CONNECT_TIMEOUT_S
        _recovery_remaining(deadline)
        # 使用 adb CLI 的 --list/创建命令，两个 subprocess 都有真实 timeout；
        # 不调用 adbutils.forward() 的无 timeout 控制连接。冷启动时旧进程可能仍
        # 持有 minitouch socket；若复用陈旧 forward，TCP connect 会成功但读不到
        # v/^/$ 握手，导致首次 humanized 输入被误判为接管。
        def list_minitouch_ports():
            try:
                output = subprocess.run(
                    [self.adb_binary, '-s', self.serial, 'forward', '--list'],
                    check=True,
                    timeout=_recovery_remaining(deadline),
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    # 无控制台宿主下抑制 adb 的 cmd 窗口闪烁
                    creationflags=subprocess.CREATE_NO_WINDOW if sys.platform.startswith('win') else 0,
                ).stdout
            except (subprocess.TimeoutExpired, OSError, subprocess.CalledProcessError) as exc:
                raise _MinitouchRecoveryFailed('tcp-forward-list failed') from exc
            ports = set()
            for line in output.splitlines():
                fields = line.split()
                if len(fields) == 3 and fields[0] == self.serial \
                        and fields[2] == 'localabstract:minitouch' \
                        and fields[1].startswith('tcp:'):
                    try:
                        ports.add(int(fields[1][4:]))
                    except ValueError:
                        raise _MinitouchRecoveryFailed(
                            f'invalid minitouch forward port: {fields[1]!r}')
            return ports

        if old_pid is None:
            # 首次开档没有远端触点，清理全部旧 forward 后必须新建端口，避免
            # 复用上一个已退出 Device 留下的单连接 minitouch endpoint。
            stale_ports = list_minitouch_ports()
            if self._minitouch_port:
                stale_ports.add(int(self._minitouch_port))
            for stale_port in sorted(stale_ports):
                _run_adb_recovery_command(
                    self,
                    ['forward', '--remove', f'tcp:{stale_port}'],
                    deadline,
                    'tcp-forward-remove',
                    tolerate_missing_listener=True,
                )
                _recovery_remaining(deadline)
            port = random_port(self.config.FORWARD_PORT_RANGE)
            _run_adb_recovery_command(
                self,
                ['forward', f'tcp:{port}', 'localabstract:minitouch'],
                deadline,
                'tcp-forward-create',
            )
        else:
            if self._minitouch_port:
                _run_adb_recovery_command(
                    self,
                    ['forward', '--remove', f'tcp:{self._minitouch_port}'],
                    deadline,
                    'tcp-forward-remove',
                    tolerate_missing_listener=True,
                )
                _recovery_remaining(deadline)
            # B1 已发生输入后只复用当前 session 之外的可观察 forward；若没有，
            # 创建新端口并通过新 PID 握手确认 session 已重建。
            ports = list_minitouch_ports()
            port = min(ports) if ports else None
            if port is None:
                port = random_port(self.config.FORWARD_PORT_RANGE)
                _run_adb_recovery_command(
                    self,
                    ['forward', f'tcp:{port}', 'localabstract:minitouch'],
                    deadline,
                    'tcp-forward-create',
                )
        _recovery_remaining(deadline)

        def connect_and_handshake():
            # 单次「建 TCP 连接 → 严格三行握手」；失败时关闭 socket 并抛出，
            # 由首连重试逻辑决定是否拉起 minitouch 服务后重试
            client = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            try:
                client.settimeout(min(MINITOUCH_RECOVERY_TCP_LINE_TIMEOUT_S, _recovery_remaining(deadline)))
                client.connect(('127.0.0.1', port))
                stream = client.makefile()
                version = self._read_humanized_minitouch_line(stream, client, deadline, 'version')
                capability = self._read_humanized_minitouch_line(stream, client, deadline, 'capability')
                pid_line = self._read_humanized_minitouch_line(stream, client, deadline, 'pid')
                max_x, max_y, new_pid = self._validate_humanized_minitouch_handshake(
                    version, capability, pid_line, old_pid
                )
                return client, max_x, max_y, new_pid
            except Exception:
                client.close()
                raise

        def handshake_ensuring_service():
            """握手一次；读到 EOF 时经 atx-agent 拉起设备侧 minitouch 后重试一次。

            两条路径都会撞上「TCP 连得上、设备侧却没人监听」：首连时 u2 3.x 不再
            负责拉起 minitouch；B1 恢复开头的 ATX 重启又会连带走掉作为 atx-agent
            子进程的 minitouch（2026-09-20 现场：EOF 后无补救，直接人工接管）。
            """
            nonlocal deadline
            try:
                return connect_and_handshake()
            except _MinitouchRecoveryFailed as exc:
                logger.info(
                    'Minitouch handshake failed, '
                    f'ensuring device-side minitouch service: {exc}'
                )
                # 拉起服务自成一段预算：它要建临时 forward、走 HTTP GET/POST 并
                # 轮询到 running，耗时量级与握手不同。若沿用 forward/握手段的剩余
                # 预算，会在半途被掐死，且拉起后重试握手也拿不到时间。
                self._ensure_humanized_minitouch_service_started(
                    time.monotonic() + MINITOUCH_SERVICE_ENSURE_TIMEOUT_S)
                # 服务拉起后 socket 才真正监听，重新走一次完整握手（同样重新起算，
                # 不沿用已被拉起段耗尽的旧 deadline）
                deadline = time.monotonic() + MINITOUCH_RECOVERY_TCP_CONNECT_TIMEOUT_S
                return connect_and_handshake()

        client, max_x, max_y, new_pid = handshake_ensuring_service()

        self._minitouch_port = port
        self._minitouch_client = client
        self.max_x = max_x
        self.max_y = max_y
        self._minitouch_pid = new_pid
        self.__dict__['minitouch_builder'] = CommandBuilder(self)

    def _ensure_humanized_minitouch_service_started(self, deadline):
        """握手读到 EOF 时，经 atx-agent 的 /services/minitouch 拉起设备侧进程。

        两个调用场景：首连（u2 3.x 不再负责拉起 minitouch）与 B1 恢复（开头的
        ATX 重启会连带走掉作为其子进程的 minitouch）。

        背景（2026-09-08 实测，MuMu12 / Android 12）：uiautomator2 3.x 的
        check_install() 只检查 atx-agent 与测试 APK，不检查 minitouch 二进制，
        也没有人把它作为进程拉起（legacy 注释「minitouch already started by
        uiautomator2」来自旧版 u2，已不成立）。adb shell 的后台进程
        （& / nohup / setsid）会被会话收割，唯一可靠的拉起方式是 atx-agent
        以守护者身份通过 service 接口启动（父进程为 atx-agent，不随 adb 会话
        退出）。HTTP 模式（_recover_humanized_minitouch_http）同款接口已在用。
        """
        # 临时 forward 到 atx-agent（7912），复用恢复命令通道，全部有超时上界
        atx_port = random_port(self.config.FORWARD_PORT_RANGE)
        _run_adb_recovery_command(
            self,
            ['forward', f'tcp:{atx_port}', 'tcp:7912'],
            deadline,
            'tcp-atx-forward-create',
        )
        try:
            service = U2Service(
                'minitouch', service_url=f'http://127.0.0.1:{atx_port}/services/minitouch')
            # 查询当前状态：已在跑则无需重复启动
            try:
                response = self._humanized_service_request(service, 'get', deadline)
                running = False
                try:
                    running = response.json().get('running') is True
                except (AttributeError, ValueError):
                    pass
                if running:
                    logger.info('minitouch service already running via atx-agent')
                    return
            except _MinitouchRecoveryFailed:
                # 查询失败（服务接口未就绪等）不致命，直接尝试 POST 启动
                pass
            # POST 启动服务；_raise_for_status 已校验 HTTP 状态
            self._humanized_service_request(service, 'post', deadline)
            # 有限轮询到 running，作为「服务真正就绪」的证据
            self._wait_humanized_service_state(service, True, deadline)
        finally:
            # 清理临时 forward，不留悬挂端口；失败不掩盖原异常
            try:
                _run_adb_recovery_command(
                    self,
                    ['forward', '--remove', f'tcp:{atx_port}'],
                    deadline,
                    'tcp-atx-forward-remove',
                    tolerate_missing_listener=True,
                )
            except _MinitouchRecoveryFailed:
                pass

    def _humanized_service_request(self, service, method, deadline):
        # 每个 HTTP 请求显式传二元 timeout（connect, read）与 retry=False：
        # 标量 timeout 会被 TimeoutRequestsSession 改写为 (3, scalar)，
        # retry=True 会在 ReadTimeout 后调用 _prepare_atx_agent() 重发
        remaining = _recovery_remaining(deadline)
        connect_timeout = min(MINITOUCH_RECOVERY_HTTP_CONNECT_TIMEOUT_S, remaining)
        read_timeout = min(MINITOUCH_RECOVERY_HTTP_CALL_TIMEOUT_S, remaining)
        # 只读取已缓存/测试注入的 u2，不触发 connection_attr.u2 的第三方初始化。
        u2_device = self.__dict__.get('u2')
        http = getattr(u2_device, 'http', None)
        timeout = _humanized_http_timeout(http, connect_timeout, read_timeout, remaining)
        try:
            # 同线程执行单次请求；二元 timeout 限制连接与读取，retry=False 禁止
            # uiautomator2 恢复后重发。不得放到无法取消的 daemon worker 中。
            if (
                service.service_url.startswith(('http://', 'https://'))
                and (
                    http is None
                    or isinstance(http, getattr(u2, '_AgentRequestSession', ()))
                )
            ):
                # 真实 uiautomator2 session 的 read timeout 允许滴流续期；使用项目侧
                # 单次 socket 请求，以 monotonic deadline 作为绝对上界，且无隐式重试。
                request_deadline = min(
                    deadline, time.monotonic() + MINITOUCH_RECOVERY_HTTP_CALL_TIMEOUT_S)
                response = humanized_http_request(
                    service.service_url, method, deadline=request_deadline)
            else:
                response = getattr(http, method)(
                    service.service_url, timeout=timeout, retry=False)
            _recovery_remaining(deadline)
            service._raise_for_status(response)
            return response
        except Exception as exc:
            raise _MinitouchRecoveryFailed(f'minitouch service {method} failed') from exc

    def _wait_humanized_service_state(self, service, expected_running, deadline):
        # 每 50ms 轮询一次 service running 状态，全部在阶段 deadline 内
        while True:
            response = self._humanized_service_request(service, 'get', deadline)
            running = response.json().get('running')
            if not isinstance(running, bool):
                raise _MinitouchRecoveryFailed('minitouch service returned invalid running state')
            if running is expected_running:
                return
            self.sleep(min(MINITOUCH_RECOVERY_POLL_S, _recovery_remaining(deadline)))

    async def _await_humanized_minitouch_recovery(self, stage, awaitable, timeout_s):
        return await asyncio.wait_for(awaitable, timeout=timeout_s)

    async def _recover_humanized_minitouch_http_async(self):
        # HTTP 恢复证据：stop → observed not running → start → observed running
        # → 新 WS → 两条非空文本 ready 消息；任一超时/异常都关闭已建 WS 并接管
        # 保持 HTTP/HTTPS 的安全级别：HTTPS 必须升级到 WSS，不能降级为明文 WS。
        url = _humanized_minitouch_ws_url(self.serial)
        ws = None
        try:
            ws = await self._await_humanized_minitouch_recovery(
                'connect', websockets.connect(url), MINITOUCH_RECOVERY_WS_CONNECT_TIMEOUT_S
            )
            for stage in ('ready-1', 'ready-2'):
                message = await self._await_humanized_minitouch_recovery(
                    stage, ws.recv(), MINITOUCH_RECOVERY_WS_READY_TIMEOUT_S
                )
                if not isinstance(message, str) or not message.strip():
                    raise _MinitouchRecoveryFailed(f'minitouch {stage} message is invalid')
        except Exception:
            if ws is not None:
                try:
                    await self._await_humanized_minitouch_recovery(
                        'close', ws.close(), MINITOUCH_RECOVERY_WS_CLOSE_TIMEOUT_S
                    )
                except Exception:
                    pass
            raise
        self._minitouch_ws = ws

    def _recover_humanized_minitouch_http(self):
        # 复用 U2Service 的 _raise_for_status 校验 HTTP 状态；不调用继承的无 timeout 的
        # stop()/start()/running()，也不调用当前会跳过的 restart_atx()
        # 直接构造绝对 service URL，禁止 U2Service.path2url() 在恢复阶段触发
        # 第三方 forward_port、ATX 探测或自动恢复。
        service = U2Service(
            'minitouch', service_url=self.serial.rstrip('/') + '/services/minitouch')
        stop_deadline = time.monotonic() + MINITOUCH_RECOVERY_STOP_TIMEOUT_S
        self._humanized_service_request(service, 'delete', stop_deadline)
        self._wait_humanized_service_state(service, False, stop_deadline)
        start_deadline = time.monotonic() + MINITOUCH_RECOVERY_START_TIMEOUT_S
        self._humanized_service_request(service, 'post', start_deadline)
        self._wait_humanized_service_state(service, True, start_deadline)
        self._minitouch_loop_run(self._recover_humanized_minitouch_http_async())
        self.__dict__['minitouch_builder'] = CommandBuilder(self)

    def _prepare_humanized_orientation(self, deadline):
        """用单次有界通道读取方向，禁止进入 Connection.get_orientation() 的 @retry。"""
        cached = self.__dict__.get('orientation')
        if type(cached) is int and cached in (0, 1, 2, 3):
            return cached
        if self.config.DEVICE_OVER_HTTP:
            request = {
                'jsonrpc': '2.0',
                'id': 'humanized-orientation',
                'method': 'deviceInfo',
                'params': [],
            }
            response = humanized_http_request(
                self.serial.rstrip('/') + '/jsonrpc/0',
                'post',
                data=json.dumps(request),
                headers={'Content-Type': 'application/json'},
                deadline=deadline,
            )
            if response.status_code != 200:
                raise _MinitouchRecoveryFailed('orientation RPC returned HTTP error')
            payload = response.json()
            result = (
                payload.get('result')
                if (
                    isinstance(payload, dict)
                    and payload.get('jsonrpc') == '2.0'
                    and payload.get('id') == request['id']
                    and 'error' not in payload
                )
                else None
            )
            orientation = result.get('displayRotation') if isinstance(result, dict) else None
            if type(orientation) is not int or orientation not in (0, 1, 2, 3):
                raise _MinitouchRecoveryFailed('orientation RPC returned invalid rotation')
        else:
            timeout = _recovery_remaining(deadline)
            try:
                output = subprocess.run(
                    [self.adb_binary, '-s', self.serial, 'shell', 'dumpsys', 'display'],
                    check=True,
                    timeout=timeout,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    # 无控制台宿主下抑制 adb 的 cmd 窗口闪烁
                    creationflags=subprocess.CREATE_NO_WINDOW if sys.platform.startswith('win') else 0,
                ).stdout
            except (subprocess.TimeoutExpired, OSError, subprocess.CalledProcessError) as exc:
                raise _MinitouchRecoveryFailed('orientation query failed') from exc
            _recovery_remaining(deadline)
            orientation = _parse_humanized_orientation(output)
            if orientation is None:
                raise _MinitouchRecoveryFailed('orientation query returned invalid display data')
        self.orientation = orientation
        return orientation

    def _connect_humanized_minitouch_first(self):
        """首连的一次完整尝试：方向查询 + 有界建连握手，两段各有独立 deadline。

        抽成独立方法是为了让 atx-agent 补救后能原样重跑同一段逻辑。
        """
        if self.config.DEVICE_OVER_HTTP:
            orientation_deadline = time.monotonic() + MINITOUCH_RECOVERY_HTTP_CALL_TIMEOUT_S
            self._prepare_humanized_orientation(orientation_deadline)
            self.max_x, self.max_y = 1280, 720
            self._recover_humanized_minitouch_http()
        else:
            orientation_deadline = time.monotonic() + MINITOUCH_RECOVERY_TCP_CONNECT_TIMEOUT_S
            self._prepare_humanized_orientation(orientation_deadline)
            # 方向查询与连接恢复分别有明确上界，避免查询耗时挤占握手预算。
            deadline = time.monotonic() + MINITOUCH_RECOVERY_TCP_CONNECT_TIMEOUT_S
            self._recover_humanized_minitouch_tcp(
                None, deadline=deadline, restart_atx=False)

    def _ensure_atx_agent_once(self, exc):
        """首连失败后的一次性补救：确保设备侧 atx-agent 已安装并在运行。

        返回 True 表示已执行补救、值得重试首连；False 表示不适用或已试过。

        背景（2026-09-11 实测，网络真机 + nemu_ipc 截图 + minitouch 控制）：
        minitouch 由 atx-agent 作为守护者拉起（设备上 minitouch 进程的父进程
        即 atx-agent），而全项目只有 install_uiautomator2() 会推送并启动
        atx-agent。它仅有的两个调用点都在 retry 的自愈分支里——
        MinitouchNotInstalledError（只有 legacy minitouch_init 会抛）与 u2 截图
        的 JSONDecodeError。humanized 首连失败抛的是 RequestHumanTakeover，
        retry_wrapper 见到它直接 break，两条自愈分支都够不着；截图方法又不经过
        u2（nemu_ipc / scrcpy 等）时，全新设备上 atx-agent 永远装不上，
        minitouch 也就永远连不上。这里补上首次部署的自举缺口。
        """
        if self._atx_agent_ensure_tried:
            return False
        self._atx_agent_ensure_tried = True
        # HTTP 模式没有 adb 通道，install_uiautomator2/restart_atx 都不适用
        if self.config.DEVICE_OVER_HTTP:
            return False
        logger.warning(f'Minitouch first connection failed: {exc}')
        try:
            # 二进制已在时只重启服务，避免每次失败都跑一遍耗时的推送/下载
            listed = self.adb_shell(['ls', '/data/local/tmp/atx-agent'])
            if listed.strip() and 'No such file' not in listed:
                logger.info('atx-agent binary exists but unreachable, restarting it')
                self.restart_atx()
            else:
                logger.info('atx-agent not installed, running uiautomator2 init '
                            '(pushing binaries, this may take a while)')
                self.install_uiautomator2()
        except Exception as ensure_exc:
            logger.warning(f'Ensure atx-agent failed: {ensure_exc}')
            return False
        return True

    def _prepare_humanized_minitouch_builder(self):
        """为首次开档输入建立有界连接，不进入 legacy 的无界初始化。"""
        cached = self.__dict__.get('minitouch_builder')
        if cached is not None:
            return cached
        # 轻量 harness 通过未绑定方法复用 runner 时保留其自定义 property。
        if not isinstance(self, Minitouch):
            return self.minitouch_builder
        try:
            self._connect_humanized_minitouch_first()
        except RequestHumanTakeover:
            raise
        except Exception as exc:
            # 初始化失败时旧 legacy 会重入同一无界初始化，不能作为 B0 回退。
            # 但首连失败的一个常见原因是设备侧 atx-agent 缺失/未运行（新机首次
            # 部署），这属于可自愈场景：补救一次后重跑首连，仍失败才转人工接管。
            if not self._ensure_atx_agent_once(exc):
                logger.exception('Minitouch first connection failed')
                raise RequestHumanTakeover('Minitouch first connection failed') from exc
            try:
                self._connect_humanized_minitouch_first()
            except RequestHumanTakeover:
                raise
            except Exception as retry_exc:
                logger.exception('Minitouch first connection failed after ensuring atx-agent')
                raise RequestHumanTakeover(
                    'Minitouch first connection failed') from retry_exc
        return self.__dict__['minitouch_builder']

    def recover_humanized_minitouch_b1(self):
        # B1 恢复唯一出口：只有「已观察到 session reset」且「新连接 ready」后正常返回；
        # 任何超时/异常/缺失事件都转 RequestHumanTakeover，之后不得再发 DOWN/UP 或 legacy。
        # 开头的 builder.clear() 只做本地卫生清理，不是成功判据。
        try:
            builder = self.minitouch_builder
            builder.clear()
            if self.config.DEVICE_OVER_HTTP:
                self._recover_humanized_minitouch_http()
            else:
                old_pid = self._minitouch_pid
                del_cached_property(self, 'minitouch_builder')
                # 不在此处起算 deadline：_recover_humanized_minitouch_tcp 会在 ATX
                # 停启段结束后才为 forward/握手起算预算（两段预算互相独立）。
                self._recover_humanized_minitouch_tcp(old_pid)
        except RequestHumanTakeover:
            raise
        except Exception as exc:
            logger.exception('Minitouch B1 recovery failed')
            raise RequestHumanTakeover('Minitouch B1 recovery failed') from exc

    def _humanized_pressure_seq(self, count):
        """生成一次手势的 minitouch 压力序列（d/m 命令第 5 字段）。

        背景：注入触摸的压力恒为 1.0（原代码恒发 100，超过 MuMu 量程 50 被钳到
        1.0）是框架层可读的注入指纹；真机电容按压压力在 0.6~0.9 区间低频波动。
        生成方式：手势基值 uniform(0.66, 0.84)×max，逐点叠加小 sigma 白噪并钳到
        [0.5, 0.95]×max。

        仅在 humanized 路径调用（off 档 RNG 不消费契约）；RNG 取 humanizer 的
        persona 种子生成器。无 rng（测试桩）或量程非法时返回 None，调用方保持
        原恒定压力。
        """
        rng = getattr(self.humanizer, 'rng', None)
        if rng is None or not self.max_pressure or self.max_pressure <= 0:
            return None
        mx = float(self.max_pressure)
        p = float(rng.uniform(0.66, 0.84)) * mx
        out = []
        for _ in range(count):
            p += float(rng.normal(0.0, 0.015 * mx))
            p = min(max(p, 0.50 * mx), 0.95 * mx)
            out.append(int(round(p)))
        return out

    def _run_humanized_minitouch(self, run_humanized, run_legacy):
        # B0/B1 按事件状态分支（契约 #11）：send() 在进入 minitouch_send() **之前**
        # 先切换 transport_started，因此任何「写入已开始但结果未知」异常保守归 B1，
        # 不会误走 B0 重放。恢复证据完整前绝不调用 run_legacy()。
        transport_started = False

        def send():
            nonlocal transport_started
            transport_started = True
            self._humanized_minitouch_transport = True
            try:
                self.minitouch_send()
            finally:
                self.__dict__.pop('_humanized_minitouch_transport', None)

        def legacy():
            # 清掉 enabled 尝试期间设的拟人 transport gap，否则 B0/B1 的 legacy 重放
            # 会让 minitouch_send 读到它而改用拟人 gap 而非 DEFAULT_DELAY，legacy
            # 就不再"逐事件回退"（契约 #11 A 类）
            self._humanized_minitouch_gap_s = None
            return run_legacy()

        try:
            prepare = getattr(self, '_prepare_humanized_minitouch_builder', None)
            builder = prepare() if callable(prepare) else self.minitouch_builder
            return run_humanized(builder, send)
        except RequestHumanTakeover:
            raise
        except _HUMANIZED_MINITOUCH_TRANSPORT_ERRORS:
            if not transport_started:
                return legacy()
            self.recover_humanized_minitouch_b1()
            return legacy()

    def _click_minitouch_humanized_impl(self, x, y):
        """开档点击：单批 down → wait(press_ms) → up → send。

        只消费 B（按压时长）与 I（transport gap）。按压时长取整数毫秒，正时长
        至少 1ms，零时长不生成 wait；保持既有单个 minitouch_send() 批次，
        不为按压时长拆分 DOWN/UP 两批。
        """
        gap = self.humanizer.gap_seconds(0.05)
        if gap is not None:
            self._humanized_minitouch_gap_s = gap
        try:
            # 压力随机化：手势内单个 down 的压力（None 时保持恒定 100）
            pressures = self._humanized_pressure_seq(1)
            p0 = pressures[0] if pressures else None

            def run_humanized(builder, send):
                press = self.humanizer.press_seconds()
                if p0 is None:
                    builder.down(x, y).commit()
                else:
                    builder.down(x, y, pressure=p0).commit()
                if press is not None and press > 0:
                    builder.wait(max(int(press * 1000 + 0.5), 1))
                builder.up().commit()
                send()

            return self._run_humanized_minitouch(
                run_humanized,
                lambda: self._click_minitouch_legacy_impl(x, y),
            )
        finally:
            self.__dict__.pop('_humanized_minitouch_gap_s', None)

    def _swipe_minitouch_humanized_impl(self, p1, p2, duration=None):
        """开档滑动：DOWN/MOVE/UP 三批，MOVE 批内按 wait(ms) → move → commit 追加。

        几何与最终 MovePlan 均由 facade 生成。触摸 liftoff（维度 F）在 UP 前
        并入 MOVE 批。DOWN 使用 startPos，最终 endpoint 原样发送；UP 后不消费
        MovePlan delay。A 类计划回退（plan/量化不可表示）在事件发出前直接走
        一次无装饰 legacy。
        """
        start = (int(p1[0]), int(p1[1]))
        end = (int(p2[0]), int(p2[1]))
        gap = self.humanizer.gap_seconds(0.05)
        if gap is not None:
            self._humanized_minitouch_gap_s = gap
        try:
            base_delay_s = 0.010
            if duration is not None:
                # 预算 = 调用方 duration（legacy 总时长语义）：
                # budget = base × PROFILE_MAX_POINTS。之前写死 base=0.010 →
                # 预算恒 120ms，长滑动（如 2s）被压成 120ms（17 倍过快）
                base_delay_s = duration / timing.PROFILE_MAX_POINTS
            plan = self.humanizer.plan_swipe(
                start, end, timing_mode='device_wait', base_delay_s=base_delay_s,
            )
            if plan is None:
                # A 类计划回退：事件尚未发出，直接调用一次无装饰 legacy impl
                return self._swipe_minitouch_legacy_impl(p1, p2, duration=duration)
            waits = _quantize_move_delays(list(plan.delays))
            if waits is None:
                # 契约 #6：目标总毫秒 < 正 delay 数，该点数不可表示 → 回退 legacy，
                # 禁止用 max(ms, 1) 静默放大总预算
                return self._swipe_minitouch_legacy_impl(p1, p2, duration=duration)
            liftoff = self.humanizer.plan_touch_liftoff(end)
            # 压力随机化：down + MOVE 点 + liftoff 点逐个取值；None 时全部保持恒定
            n_move = len(plan.points) + (len(liftoff.points) if liftoff is not None else 0)
            pressures = self._humanized_pressure_seq(1 + n_move)

            def run_humanized(builder, send):
                if pressures is None:
                    builder.down(*start).commit()
                else:
                    builder.down(*start, pressure=pressures[0]).commit()
                send()
                # MOVE 批内 wait → move → commit 连续追加后只调用一次 send（契约 #6）。
                # w 是恒定回报率间隔（同值 ±1ms 抖动后量化），事件流与真实设备
                # 固定采样率上报同构
                for i, (ms, point) in enumerate(zip(waits, plan.points)):
                    if pressures is None:
                        builder.wait(ms).move(*point).commit()
                    else:
                        builder.wait(ms).move(*point, pressure=pressures[1 + i]).commit()
                if liftoff is not None:
                    liftoff_waits = _quantize_move_delays(list(liftoff.delays))
                    if liftoff_waits is None:
                        # 极端预算不足：liftoff 退化为不生成 wait（不放大预算）
                        liftoff_waits = [0] * len(liftoff.points)
                    for j, (ms, point) in enumerate(zip(liftoff_waits, liftoff.points)):
                        if pressures is None:
                            builder.wait(ms).move(*point).commit()
                        else:
                            builder.wait(ms).move(*point, pressure=pressures[1 + len(plan.points) + j]).commit()
                send()
                builder.up().commit()
                send()

            return self._run_humanized_minitouch(
                run_humanized,
                lambda: self._swipe_minitouch_legacy_impl(p1, p2, duration=duration),
            )
        finally:
            self.__dict__.pop('_humanized_minitouch_gap_s', None)

    @retry
    def drag_minitouch(self, p1, p2, point_random=(-10, -10, 10, 10)):
        p1 = np.array(p1) - random_rectangle_point(point_random)
        p2 = np.array(p2) - random_rectangle_point(point_random)
        points = insert_swipe(p0=p1, p3=p2, speed=20)
        builder = self.minitouch_builder

        builder.down(*points[0]).commit()
        self.minitouch_send()

        for point in points[1:]:
            builder.move(*point).commit().wait(10)
        self.minitouch_send()

        builder.move(*p2).commit().wait(140)
        builder.move(*p2).commit().wait(140)
        self.minitouch_send()

        builder.up().commit()
        self.minitouch_send()

if __name__ == '__main__':
    mm = Minitouch(config='oas1')
    mm.click_minitouch(200, 150)
