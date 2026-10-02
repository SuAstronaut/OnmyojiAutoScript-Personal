import json
import random
import re
import socket
import ssl
import time
from urllib.parse import urlsplit

import uiautomator2 as u2
from adbutils import AdbTimeout
from adbutils import _AdbStreamConnection
from lxml import etree

from module.base.decorator import cached_property
from module.logger import logger

RETRY_TRIES = 5
RETRY_DELAY = 3


def _deadline_remaining(deadline):
    """返回绝对 deadline 的剩余秒数；耗尽时立即抛出 TimeoutError。"""
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError('humanized HTTP deadline exceeded')
    return remaining


class HumanizedHttpResponse:
    """拟人化单次 HTTP 请求的最小响应对象。"""

    def __init__(self, status_code, headers, content):
        self.status_code = status_code
        self.headers = headers
        self.content = content

    @property
    def text(self):
        return self.content.decode('utf-8', errors='replace')

    def json(self):
        return json.loads(self.content.decode('utf-8'))

    def raise_for_status(self):
        if self.status_code >= 400:
            raise OSError(f'HTTP status {self.status_code}')


def _recv_humanized_http(sock, deadline, size=4096):
    """执行一次受绝对 deadline 约束的 socket 读取。"""
    sock.settimeout(_deadline_remaining(deadline))
    chunk = sock.recv(size)
    _deadline_remaining(deadline)
    return chunk


def _read_humanized_http_headers(sock, deadline, limit=65536):
    """读取完整 HTTP 响应头；逐字节滴流也不能续期。"""
    data = bytearray()
    marker = b'\r\n\r\n'
    while marker not in data:
        chunk = _recv_humanized_http(sock, deadline)
        if not chunk:
            raise OSError('HTTP response closed before headers completed')
        data.extend(chunk)
        if len(data) > limit:
            raise OSError('HTTP response headers too large')
    return bytes(data)


def _read_humanized_http_exact(sock, buffered, size, deadline):
    """读取固定字节数，优先消费已缓冲的数据。"""
    while len(buffered) < size:
        chunk = _recv_humanized_http(sock, deadline, min(4096, size - len(buffered)))
        if not chunk:
            raise OSError('HTTP response body truncated')
        buffered.extend(chunk)
    data = bytes(buffered[:size])
    del buffered[:size]
    return data


def humanized_http_request(url, method, *, data=None, headers=None, deadline):
    """绕开第三方重试，执行一次带绝对墙钟 deadline 的 HTTP 请求。"""
    parsed = urlsplit(url)
    if parsed.scheme not in ('http', 'https') or not parsed.hostname:
        raise ValueError(f'unsupported humanized HTTP URL: {url!r}')
    port = parsed.port or (443 if parsed.scheme == 'https' else 80)
    body = b'' if data is None else (data.encode('utf-8') if isinstance(data, str) else bytes(data))
    host = parsed.hostname
    host_header = host if parsed.port is None else f'{host}:{port}'
    request_headers = {
        'Host': host_header,
        'Connection': 'close',
        'Content-Length': str(len(body)),
    }
    if headers:
        request_headers.update(headers)
    target = parsed.path or '/'
    if parsed.query:
        target += '?' + parsed.query
    lines = [f'{method.upper()} {target} HTTP/1.1']
    lines.extend(f'{key}: {value}' for key, value in request_headers.items())
    request_bytes = ('\r\n'.join(lines) + '\r\n\r\n').encode('ascii') + body

    sock = socket.create_connection((host, port), timeout=_deadline_remaining(deadline))
    try:
        if parsed.scheme == 'https':
            sock.settimeout(_deadline_remaining(deadline))
            sock = ssl.create_default_context().wrap_socket(sock, server_hostname=host)
            _deadline_remaining(deadline)
        sock.settimeout(_deadline_remaining(deadline))
        sock.sendall(request_bytes)
        _deadline_remaining(deadline)

        raw = _read_humanized_http_headers(sock, deadline)
        header_block, initial_body = raw.split(b'\r\n\r\n', 1)
        header_lines = header_block.split(b'\r\n')
        status_parts = header_lines[0].decode('latin1').split(' ', 2)
        if len(status_parts) < 2:
            raise OSError('invalid HTTP status line')
        try:
            status_code = int(status_parts[1])
        except ValueError as exc:
            raise OSError('invalid HTTP status code') from exc
        response_headers = {}
        for line in header_lines[1:]:
            if b':' not in line:
                raise OSError('invalid HTTP header line')
            key, value = line.split(b':', 1)
            response_headers[key.decode('latin1').lower()] = value.strip().decode('latin1')

        buffered = bytearray(initial_body)
        length = response_headers.get('content-length')
        transfer = response_headers.get('transfer-encoding', '').lower()
        if length is not None:
            try:
                expected = int(length)
            except ValueError as exc:
                raise OSError('invalid HTTP content length') from exc
            if expected < 0:
                raise OSError('negative HTTP content length')
            content = _read_humanized_http_exact(sock, buffered, expected, deadline)
        elif 'chunked' in [part.strip() for part in transfer.split(',')]:
            content = bytearray()

            def read_line():
                while b'\r\n' not in buffered:
                    chunk = _recv_humanized_http(sock, deadline)
                    if not chunk:
                        raise OSError('chunked HTTP response truncated')
                    buffered.extend(chunk)
                line, _, remainder = buffered.partition(b'\r\n')
                buffered[:] = remainder
                return bytes(line)

            while True:
                try:
                    chunk_size = int(read_line().split(b';', 1)[0], 16)
                except ValueError as exc:
                    raise OSError('invalid HTTP chunk size') from exc
                if chunk_size == 0:
                    break
                content.extend(_read_humanized_http_exact(
                    sock, buffered, chunk_size, deadline))
                if _read_humanized_http_exact(sock, buffered, 2, deadline) != b'\r\n':
                    raise OSError('invalid HTTP chunk terminator')
            content = bytes(content)
        else:
            content = bytearray(buffered)
            while True:
                chunk = _recv_humanized_http(sock, deadline)
                if not chunk:
                    break
                content.extend(chunk)
            content = bytes(content)
        return HumanizedHttpResponse(status_code, response_headers, content)
    finally:
        sock.close()


def is_port_using(port_num):
    """ if port is using by others, return True. else return False """
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(2)

    try:
        s.bind(('127.0.0.1', port_num))
        return False
    except OSError:
        # Address already bind
        return True
    finally:
        s.close()


def random_port(port_range):
    """ get a random port from port set """
    new_port = random.choice(list(range(*port_range)))
    if is_port_using(new_port):
        return random_port(port_range)
    else:
        return new_port


def recv_all(stream, chunk_size=4096, recv_interval=0.000) -> bytes:
    """
    Args:
        stream:
        chunk_size:
        recv_interval (float): Default to 0.000, use 0.001 if receiving as server

    Returns:
        bytes:

    Raises:
        AdbTimeout
    """
    if isinstance(stream, _AdbStreamConnection):
        stream = stream.conn
        stream.settimeout(10)
    else:
        stream.settimeout(10)

    try:
        fragments = []
        while 1:
            chunk = stream.recv(chunk_size)
            if chunk:
                fragments.append(chunk)
                # See https://stackoverflow.com/questions/23837827/python-server-program-has-high-cpu-usage/41749820#41749820
                time.sleep(recv_interval)
            else:
                break
        return remove_shell_warning(b''.join(fragments))
    except socket.timeout:
        raise AdbTimeout('adb read timeout')


def possible_reasons(*args):
    """
    Show possible reasons

        Possible reason #1: <reason_1>
        Possible reason #2: <reason_2>
    """
    for index, reason in enumerate(args):
        index += 1
        logger.critical(f'Possible reason #{index}: {reason}')


class PackageNotInstalled(Exception):
    pass


class ImageTruncated(Exception):
    pass


def retry_sleep(trial):
    # First trial
    if trial == 0:
        pass
    # Failed once, fast retry
    elif trial == 1:
        pass
    # Failed twice
    elif trial == 2:
        time.sleep(1)
    # Failed more
    else:
        time.sleep(RETRY_DELAY)


def handle_adb_error(e):
    """
    Args:
        e (Exception):

    Returns:
        bool: If should retry
    """
    text = str(e)
    if 'not found' in text:
        # When you call `adb disconnect <serial>`
        # Or when adb server was killed (low possibility)
        # AdbError(device '127.0.0.1:59865' not found)
        logger.error(e)
        return True
    elif 'timeout' in text:
        # AdbTimeout(adb read timeout)
        logger.error(e)
        return True
    elif 'closed' in text:
        # AdbError(closed)
        # Usually after AdbTimeout(adb read timeout)
        # Disconnect and re-connect should fix this.
        logger.error(e)
        return True
    elif 'device offline' in text:
        # AdbError(device offline)
        # When a device that has been connected wirelessly is disconnected passively,
        # it does not disappear from the adb device list,
        # but will be displayed as offline.
        # In many cases, such as disconnection and recovery caused by network fluctuations,
        # or after VMOS reboot when running Alas on a phone,
        # the device is still available, but it needs to be disconnected and re-connected.
        logger.error(e)
        return True
    elif 'is offline' in text:
        # RuntimeError: USB device 127.0.0.1:7555 is offline
        # Raised by uiautomator2 when current adb service is killed by another version of adb service.
        logger.error(e)
        return True
    elif 'unknown host service' in text:
        # AdbError(unknown host service)
        # Another version of ADB service started, current ADB service has been killed.
        # Usually because user opened a Chinese emulator, which uses ADB from the Stone Age.
        logger.error(e)
        return True
    else:
        # AdbError()
        logger.exception(e)
        possible_reasons(
            'If you are using BlueStacks or LD player or WSA, please enable ADB in the settings of your emulator',
            'Emulator died, please restart emulator',
            'Serial incorrect, no such device exists or emulator is not running'
        )
        return False


def get_serial_pair(serial):
    """
    Args:
        serial (str):

    Returns:
        str, str: `127.0.0.1:5555+{X}` and `emulator-5554+{X}`, 0 <= X <= 32
    """
    if serial.startswith('127.0.0.1:'):
        try:
            port = int(serial[10:])
            if 5555 <= port <= 5555 + 32:
                return f'127.0.0.1:{port}', f'emulator-{port - 1}'
        except (ValueError, IndexError):
            pass
    if serial.startswith('emulator-'):
        try:
            port = int(serial[9:])
            if 5554 <= port <= 5554 + 32:
                return f'127.0.0.1:{port + 1}', f'emulator-{port}'
        except (ValueError, IndexError):
            pass

    return None, None


def remove_prefix(s, prefix):
    """
    Remove prefix of a string or bytes like `string.removeprefix(prefix)`, which is on Python3.9+

    Args:
        s (str, bytes):
        prefix (str, bytes):

    Returns:
        str, bytes:
    """
    return s[len(prefix):] if s.startswith(prefix) else s


def remove_shell_warning(s):
    """
    Remove warnings from shell

    Args:
        s (str, bytes):

    Returns:
        str, bytes:
    """
    # WARNING: linker: [vdso]: unused DT entry: type 0x70000001 arg 0x0\n\x89PNG\r\n\x1a\n\x00\x00\x00\rIH
    if isinstance(s, bytes):
        if s.startswith(b'WARNING'):
            try:
                s = s.split(b'\n', maxsplit=1)[1]
            except IndexError:
                pass
        return s
        # return re.sub(b'^WARNING.+\n', b'', s)
    elif isinstance(s, str):
        if s.startswith('WARNING'):
            try:
                s = s.split('\n', maxsplit=1)[1]
            except IndexError:
                pass
    return s


class IniterNoMinicap(u2.init.Initer):
    @property
    def minicap_urls(self):
        """
        Don't install minicap on emulators, return empty urls.

        binary from https://github.com/openatx/stf-binaries
        only got abi: armeabi-v7a and arm64-v8a
        """
        return []


class Device(u2.Device):
    def show_float_window(self, show=True):
        """
        Don't show float windows.
        """
        pass


# Monkey patch
u2.init.Initer = IniterNoMinicap
u2.Device = Device


class HierarchyButton:
    """
    Convert UI hierarchy to an object like the Button in Alas.
    """
    _name_regex = re.compile('@.*?=[\'\"](.*?)[\'\"]')

    def __init__(self, hierarchy: etree._Element, xpath: str):
        self.hierarchy = hierarchy
        self.xpath = xpath
        self.nodes = hierarchy.xpath(xpath)

    @cached_property
    def name(self):
        res = HierarchyButton._name_regex.findall(self.xpath)
        if res:
            return res[0]
        else:
            return 'HierarchyButton'

    @cached_property
    def count(self):
        return len(self.nodes)

    @cached_property
    def exist(self):
        return self.count == 1

    @cached_property
    def area(self):
        if self.exist:
            bounds = self.nodes[0].attrib.get("bounds")
            lx, ly, rx, ry = map(int, re.findall(r"\d+", bounds))
            return lx, ly, rx, ry
        else:
            return None

    @cached_property
    def button(self):
        return self.area

    def __bool__(self):
        return self.exist

    def __str__(self):
        return self.name

    @cached_property
    def focused(self):
        if self.exist:
            return self.nodes[0].attrib.get("focused").lower() == 'true'
        else:
            return False
