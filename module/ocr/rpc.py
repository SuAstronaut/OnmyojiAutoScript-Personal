# This Python file uses the following encoding: utf-8
# @author runhey
# github https://github.com/runhey
import atexit
from contextlib import contextmanager
import gc
import os
import pickle
import re
import socket
import subprocess
import sys
import tempfile
import threading
import time
from typing import Any, Dict, List, Optional

import numpy as np
import zerorpc

from module.exception import ScriptError
from module.logger import logger
from module.ocr.result import BoxedResult

# 服务进程句柄。用 subprocess 而不是 multiprocessing：
# Windows spawn 会 re-import 本模块，而本模块顶层就 import zerorpc(gevent)，
# 那样 onnxruntime 的 DLL 初始化必然失败。必须用独立入口 module.ocr.server_boot
# 保证 onnxruntime 先于 gevent 加载，详见 server_boot.py 的说明。
_OCR_SERVER_PROCESS: Optional[subprocess.Popen] = None
_OCR_CONTROL_CLIENT = None
_OCR_CONTROL_ADDRESS: Optional[str] = None
_OCR_CONTROL_LOCK = threading.Lock()
# 非 Windows 环境的进程内兜底锁；Windows 使用下方文件锁实现跨实例协调。
_OCR_RECOVERY_LOCKS: Dict[int, threading.Lock] = {}
_OCR_RECOVERY_LOCKS_GUARD = threading.Lock()


def _normalize_address(address: str) -> str:
    if address.startswith("tcp://"):
        return address
    return f"tcp://{address}"


def _split_host_port(address: str) -> tuple[str, int]:
    addr = address.replace("tcp://", "")
    if ":" not in addr:
        return addr, 22268
    host, port = addr.rsplit(":", 1)
    return host, int(port)


def _is_port_in_use(host: str, port: int, timeout: float = 0.5) -> bool:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.settimeout(timeout)
        s.connect((host, port))
        s.shutdown(2)
        return True
    except Exception:
        return False
    finally:
        s.close()


def ensure_ocr_server_started() -> bool:
    from module.server.setting import State

    deploy_config = State.deploy_config
    if not deploy_config.StartOcrServer:
        return False

    if deploy_config.OcrServerPort:
        port = int(deploy_config.OcrServerPort)
    else:
        _, port = _split_host_port(str(deploy_config.OcrClientAddress))
    host = "0.0.0.0"

    if _is_port_in_use("127.0.0.1", port):
        logger.info(f"OCR server already running on port {port}")
        return True

    global _OCR_SERVER_PROCESS
    if _OCR_SERVER_PROCESS is not None and _OCR_SERVER_PROCESS.poll() is None:
        logger.info("OCR server process already started")
        return True

    _OCR_SERVER_PROCESS = _spawn_server_process(host, port)
    if _OCR_SERVER_PROCESS is None:
        return False

    logger.info(f"Start OCR server on {host}:{port}")
    for _ in range(100):
        if _is_port_in_use("127.0.0.1", port):
            return True
        # 进程提前退出说明启动失败，不用再等
        if _OCR_SERVER_PROCESS.poll() is not None:
            logger.error(f"OCR server exited with code {_OCR_SERVER_PROCESS.returncode}")
            _OCR_SERVER_PROCESS = None
            return False
        time.sleep(0.1)
    logger.error(f"OCR server is not ready on port {port}")
    return False


def _spawn_server_process(host: str, port: int) -> Optional[subprocess.Popen]:
    """以独立进程启动 OCR 服务。

    走 `python -m module.ocr.server_boot` 而不是 multiprocessing：
    Windows spawn 会 re-import 本模块（顶层已 import zerorpc/gevent），
    导致服务进程里 onnxruntime 的 DLL 初始化失败。
    """
    project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
    command = [sys.executable, '-m', 'module.ocr.server_boot',
               '--host', str(host), '--port', str(port)]
    flags = subprocess.CREATE_NO_WINDOW if sys.platform.startswith('win') else 0
    try:
        return subprocess.Popen(
            command,
            cwd=project_root,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=flags,
        )
    except Exception as e:
        logger.error(f'Failed to spawn OCR server process: {e}')
        return None


def shutdown_ocr_server(timeout: float = 2.0) -> bool:
    global _OCR_SERVER_PROCESS

    process = _OCR_SERVER_PROCESS
    if process is None:
        return False

    if process.poll() is not None:
        _OCR_SERVER_PROCESS = None
        return False

    logger.info("Stopping OCR server process")
    try:
        process.terminate()
        try:
            process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            logger.warning("OCR server process did not exit in time, force killing")
            process.kill()
            process.wait(timeout=1.0)
        logger.info("OCR server process stopped")
        return True
    except Exception as e:
        logger.exception(e)
        return False
    finally:
        _OCR_SERVER_PROCESS = None


def _ocr_process_alive(pid: int) -> bool:
    """用 WMI 查询进程是否仍存活，查询失败时保守地视为仍存活。"""
    try:
        from win32com.client import GetObject
        wmi = GetObject('winmgmts:')
        for process in wmi.InstancesOf('Win32_Process'):
            try:
                if int(process.Properties_('ProcessID').Value) == int(pid):
                    return True
            except Exception:
                continue
        return False
    except Exception as e:
        logger.debug(f'确认 OCR 进程退出失败：{e}')
        return True


def _wait_ocr_process_exit(pid: int, timeout: float = 3.0) -> bool:
    """等待 taskkill 的目标真正退出，避免 DLL 仍被占用就开始换包。"""
    deadline = time.monotonic() + timeout
    while _ocr_process_alive(pid):
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.1)
    return True


def kill_orphan_ocr_servers(port: Optional[int] = None) -> int:
    """终止 OCR RPC 服务进程，含由其它进程拉起的。

    shutdown_ocr_server 只能停本进程记录的 _OCR_SERVER_PROCESS 子进程；
    多开 / 残留 / GUI 先启动等场景下，22268 上的 OCR 服务由别的进程持有，
    更新器换 onnxruntime 包时 DLL 仍被锁（WinError 5 拒绝访问）。
    这里按 CommandLine 匹配 server_boot 入口，把这类进程一并终止，
    确保换包前 onnxruntime.dll 真正释放。

    Args:
        port: 仅终止指定端口的服务；为空时保持更新器原有行为，终止全部服务。

    Returns:
        int: 终止的进程数。
    """
    # 测试环境下拒绝真实 taskkill：本函数会杀掉本机所有 OAS OCR 服务，
    # 而 test_updater 里成片用例会走到 execute_pull 尾段的真实 align_ocr。
    # 实际踩过——跑一次测试套件就把用户正在运行的实例打成 LostRemote。
    # 需要覆盖终止逻辑的用例请 monkeypatch subprocess.run 后再断言。
    if 'PYTEST_CURRENT_TEST' in os.environ:
        logger.info('Running under pytest, skip killing OCR server processes')
        return 0

    try:
        from win32com.client import GetObject
        wmi = GetObject('winmgmts:')
    except Exception as e:
        logger.warning(f'无法枚举 OCR 服务进程（{e}），可能有外部进程仍占用 onnxruntime')
        return -1

    project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
    # 统一斜杠方向后再比较：两边都归一化，否则 'C:\a' in 'C:/a/b' 恒为 False，
    # 会让下面的路径校验把所有进程都跳过（函数等于失效）。
    normalized_root = project_root.replace('\\', '/').lower().rstrip('/')
    root_prefix = normalized_root + '/'
    # 路径比较必须带目录边界，避免 C:/OAS 误匹配 C:/OAS-backup。
    def path_in_project(path: str) -> bool:
        # WMI 的 ExecutablePath 通常无引号，CommandLine 在路径含空格时通常以引号开头；
        # 去掉开头引号后再做带目录边界的前缀判断，不能因引号跳过应清理的 OCR 进程。
        value = (path or '').replace('\\', '/').lower().strip().lstrip('"')
        return value == normalized_root or value.startswith(root_prefix)

    # 自愈只处理发生故障的端口，避免影响同一安装目录下的其它 OCR 服务。
    port_pattern = None if port is None else re.compile(
        rf'(?<!\S)--port(?:\s+|=){int(port)}(?=\s|$)'
    )
    killed = 0
    cleanup_failed = False
    for p in wmi.InstancesOf('Win32_Process'):
        try:
            pid = p.Properties_('ProcessID').Value
            name = p.Properties_('Name').Value or ''
            cmdline = p.Properties_('CommandLine').Value or ''
            exe = p.Properties_('ExecutablePath').Value or ''
        except Exception:
            continue
        if pid == os.getpid():
            continue
        if name not in ('python.exe', 'pythonw.exe'):
            continue
        if 'server_boot' not in cmdline:
            continue
        if port_pattern is not None and port_pattern.search(cmdline) is None:
            continue
        # 无法从可执行路径或命令行确认属于当前安装时宁可不杀，避免多开安装
        # 之间互相终止 OCR 服务。
        if not (path_in_project(exe) or path_in_project(cmdline)):
            continue
        logger.info(f'Kill OCR server process: PID {pid} ({exe or cmdline})')
        try:
            flags = subprocess.CREATE_NO_WINDOW if sys.platform.startswith('win') else 0
            result = subprocess.run(['taskkill', '/f', '/pid', str(pid)],
                                    capture_output=True,
                                    creationflags=flags)
            if result.returncode != 0:
                logger.warning(f'taskkill OCR server {pid} failed with code {result.returncode}')
                cleanup_failed = True
                continue
            if not _wait_ocr_process_exit(pid):
                logger.warning(f'OCR server process {pid} did not exit after taskkill')
                cleanup_failed = True
                continue
            killed += 1
        except Exception as e:
            logger.warning(f'Failed to kill OCR server process {pid}: {e}')
            cleanup_failed = True
    return -1 if cleanup_failed else killed


@contextmanager
def _ocr_recovery_lock(port: int, timeout: float = 30.0):
    """按端口协调 OCR 恢复，Windows 下可跨脚本实例互斥。"""
    if not sys.platform.startswith('win'):
        with _OCR_RECOVERY_LOCKS_GUARD:
            lock = _OCR_RECOVERY_LOCKS.setdefault(port, threading.Lock())
        acquired = lock.acquire(timeout=timeout)
        try:
            yield acquired
        finally:
            if acquired:
                lock.release()
        return

    import msvcrt

    lock_path = os.path.join(tempfile.gettempdir(), f'oas_ocr_recovery_{port}.lock')
    lock_file = open(lock_path, 'a+b')
    if os.path.getsize(lock_path) == 0:
        lock_file.write(b'\0')
        lock_file.flush()

    acquired = False
    deadline = time.monotonic() + timeout
    try:
        while True:
            try:
                lock_file.seek(0)
                msvcrt.locking(lock_file.fileno(), msvcrt.LK_NBLCK, 1)
                acquired = True
                break
            except OSError:
                if time.monotonic() >= deadline:
                    break
                time.sleep(0.1)
        yield acquired
    finally:
        if acquired:
            lock_file.seek(0)
            msvcrt.locking(lock_file.fileno(), msvcrt.LK_UNLCK, 1)
        lock_file.close()


def _ping_ocr_server(address: str, timeout: float = 1.0) -> bool:
    """用独立短连接确认 RPC 是否已被其它实例恢复。"""
    client = zerorpc.Client(timeout=timeout)
    try:
        client.connect(_normalize_address(address))
        return bool(client.ping())
    except Exception:
        return False
    finally:
        try:
            client.close()
        except Exception:
            pass


def restart_ocr_server(address: str) -> bool:
    """重启当前代理连接的本机 OCR 服务。

    仅处理由本安装配置托管的本机地址；远程 OCR 地址不能由客户端擅自终止。
    服务重启只恢复进程，失败的 OCR 请求仍由 ModelProxy 重新发送。
    """
    from module.server.setting import State

    deploy_config = State.deploy_config
    if not bool(deploy_config.StartOcrServer):
        return False

    host, port = _split_host_port(address)
    host = host.strip().lower()
    local_hosts = {'127.0.0.1', 'localhost', '0.0.0.0'}
    if host not in local_hosts:
        return False

    configured_port = int(deploy_config.OcrServerPort or port)
    if port != configured_port:
        return False

    with _ocr_recovery_lock(port) as acquired:
        if not acquired:
            logger.warning(f'Timeout waiting for OCR recovery lock on port {port}')
            return False

        # 进入跨进程锁后重新检查：其它实例可能已经完成恢复，避免再次杀服务。
        if _ping_ocr_server(address):
            logger.info(f'OCR server on port {port} has already recovered')
            return True

        logger.warning(f'Restart unresponsive OCR server on port {port}')
        shutdown_ocr_server()
        orphan_result = kill_orphan_ocr_servers(port=port)
        if orphan_result < 0:
            logger.error(f'Unable to confirm OCR server cleanup on port {port}')
            return False

        # taskkill 返回后端口可能还未立即释放，先等待再拉起，避免误判已有服务。
        deadline = time.monotonic() + 3.0
        while _is_port_in_use('127.0.0.1', port, timeout=0.1):
            if time.monotonic() >= deadline:
                logger.error(f'OCR server port {port} is still occupied after restart cleanup')
                return False
            time.sleep(0.1)

        stale_control_client = _OCR_CONTROL_CLIENT
        if stale_control_client is not None:
            _reset_ocr_control_client(stale_control_client)
        return ensure_ocr_server_started()


def notify_ocr_instance_state(instance_id: str, active: bool) -> bool:
    """通知 OCR RPC 某个实例是否正在执行任务。"""
    try:
        from module.server.setting import State
        deploy_config = State.deploy_config
    except Exception as e:
        logger.debug(f'OCR instance state configuration unavailable: {e}')
        return False
    try:
        use_server = bool(deploy_config.UseOcrServer)
        start_server = bool(deploy_config.StartOcrServer)
    except Exception as e:
        logger.debug(f'OCR instance state options unavailable: {e}')
        return False
    if not use_server or not start_server:
        return False

    address = deploy_config.OcrClientAddress or '127.0.0.1:22268'
    client = _get_ocr_control_client(address)
    if client is None:
        return False

    try:
        return bool(client.set_instance_active(str(instance_id), bool(active)))
    except Exception as e:
        _reset_ocr_control_client(client)
        logger.debug(f'OCR instance state notification failed: {e}')
        # RPC 服务可能刚完成重启，当前通知重连一次，避免丢失本次任务边界。
        client = _get_ocr_control_client(address)
        if client is None:
            return False
        try:
            return bool(client.set_instance_active(str(instance_id), bool(active)))
        except Exception as retry_error:
            _reset_ocr_control_client(client)
            logger.debug(f'OCR instance state retry failed: {retry_error}')
            return False


def _get_ocr_control_client(address: str):
    """复用每个实例进程的轻量控制连接，避免每个任务重复握手。"""
    global _OCR_CONTROL_CLIENT, _OCR_CONTROL_ADDRESS
    normalized = _normalize_address(address)
    with _OCR_CONTROL_LOCK:
        if _OCR_CONTROL_CLIENT is not None and _OCR_CONTROL_ADDRESS == normalized:
            return _OCR_CONTROL_CLIENT
        try:
            host, port = _split_host_port(address)
            # 服务未监听时快速返回，避免每个无 OCR 任务边界等待 RPC 超时。
            if not _is_port_in_use(host, port, timeout=0.1):
                return None
            client = zerorpc.Client(timeout=0.5)
            client.connect(normalized)
            client.ping()
        except Exception as e:
            logger.debug(f'OCR RPC control connection failed: {e}')
            return None
        _OCR_CONTROL_CLIENT = client
        _OCR_CONTROL_ADDRESS = normalized
        return client


def _reset_ocr_control_client(client) -> None:
    """连接失效后清空缓存，下一次状态通知会重新连接。"""
    global _OCR_CONTROL_CLIENT, _OCR_CONTROL_ADDRESS
    with _OCR_CONTROL_LOCK:
        if _OCR_CONTROL_CLIENT is client:
            _OCR_CONTROL_CLIENT = None
            _OCR_CONTROL_ADDRESS = None


def serve_forever(host: str, port: int) -> None:
    """在当前进程里启动 zerorpc 服务并阻塞。

    调用方必须已经先 import 过 onnxruntime（见 module/ocr/server_boot.py）：
    gevent 打补丁后再加载 ORT 的原生 DLL 会失败。
    """
    server = zerorpc.Server(OcrServer())
    server.bind(f"tcp://{host}:{port}")
    server.run()


def _get_server_model():
    """RPC 服务端使用的模型。

    与本地推理走同一个工厂，保证两条路径识别行为一致。
    独立成函数便于测试注入，也让 rapidocr 的加载留在服务进程内。
    """
    from module.ocr.models import get_local_ocr_model
    return get_local_ocr_model('ch')


class OcrServer:
    """常驻 RPC 服务端，按需加载并回收 OCR 模型。"""

    # 模型连续 10 分钟未被调用时释放，RPC 监听进程本身继续保留。
    MODEL_IDLE_TIMEOUT = 10 * 60
    MODEL_IDLE_CHECK_INTERVAL = 30

    def __init__(
        self,
        idle_timeout: float = MODEL_IDLE_TIMEOUT,
        idle_check_interval: float = MODEL_IDLE_CHECK_INTERVAL,
    ) -> None:
        # 服务启动时不构造模型，避免空闲实例也占用 GPU。
        self.model = None
        self._model_lock = threading.Lock()
        self._active_requests = 0
        self._active_instances: set[str] = set()
        self._instance_tracking_enabled = False
        self._last_used = time.monotonic()
        self._idle_timeout = max(0.0, float(idle_timeout))
        self._idle_check_interval = max(0.01, float(idle_check_interval))
        self._idle_monitor = threading.Thread(
            target=self._release_idle_model,
            name='ocr-model-idle-monitor',
            daemon=True,
        )
        self._idle_monitor.start()

    def ping(self) -> bool:
        return True

    def set_instance_active(self, instance_id: str, active: bool) -> bool:
        """记录实例任务状态，全部实例空闲后立即释放 OCR 模型。"""
        instance_id = str(instance_id or '').strip()
        if not instance_id:
            return False
        with self._model_lock:
            if active:
                self._instance_tracking_enabled = True
                self._active_instances.add(instance_id)
                return True
            if not self._instance_tracking_enabled:
                return True
            self._active_instances.discard(instance_id)
            if not self._active_instances and not self._active_requests:
                self._release_model_locked('all instances are idle')
            return True

    def _acquire_model(self):
        """获取模型并记录进行中的请求，模型只在首次 OCR 时加载。"""
        with self._model_lock:
            if self.model is None:
                self.model = _get_server_model()
                logger.info('OCR model loaded on first request')
            self._active_requests += 1
            self._last_used = time.monotonic()
            return self.model

    def _release_request(self) -> None:
        with self._model_lock:
            self._active_requests = max(0, self._active_requests - 1)
            self._last_used = time.monotonic()
            if (self._instance_tracking_enabled and not self._active_instances
                    and not self._active_requests):
                self._release_model_locked('all instances are idle')

    def _run_with_model(self, callback):
        model = self._acquire_model()
        try:
            return callback(model)
        finally:
            self._release_request()

    def _release_idle_model(self) -> None:
        """回收空闲模型，避免后台线程在识别进行时清理模型。"""
        while True:
            time.sleep(self._idle_check_interval)
            with self._model_lock:
                if self.model is None:
                    continue
                idle_seconds = time.monotonic() - self._last_used
                if self._active_requests or idle_seconds < self._idle_timeout:
                    continue
                self._release_model_locked('10 minutes without requests')

    def _release_model_locked(self, reason: str) -> bool:
        """在持有模型锁时清理模型及工厂缓存。"""
        if self.model is None:
            return False
        self.model = None
        # 清理统一工厂缓存，随后由 gc 尽快释放推理引擎及其 GPU 资源。
        from module.ocr.models import clear_ocr_model_cache
        clear_ocr_model_cache()
        gc.collect()
        logger.info(f'OCR model released: {reason}')
        return True

    def detect_and_ocr(
        self,
        image_bytes: bytes,
        drop_score: float = 0.5,
        unclip_ratio: Optional[float] = None,
        box_thresh: Optional[float] = None,
        vertical: bool = False,
    ) -> List[Dict[str, Any]]:
        def recognize(model):
            # 竖排旋转已下沉到 RapidOcrModel 内部，服务端只做参数转发与序列化
            image = pickle.loads(image_bytes)
            results = model.detect_and_ocr(image,
                                           drop_score=drop_score,
                                           unclip_ratio=unclip_ratio,
                                           box_thresh=box_thresh,
                                           vertical=vertical)
            return [
                {"box": _box_to_list(r.box), "ocr_text": r.ocr_text, "score": float(r.score)}
                for r in results
            ]

        return self._run_with_model(recognize)

    def ocr_single_line(self, image_bytes: bytes):
        def recognize(model):
            image = pickle.loads(image_bytes)
            result, score = model.ocr_single_line(image)
            return result, float(score)

        return self._run_with_model(recognize)


def _box_to_list(box) -> list:
    """检测框统一转成嵌套 list，msgpack 无法序列化 ndarray。"""
    return box.tolist() if hasattr(box, 'tolist') else box


class ModelProxy:
    """OCR RPC 客户端代理，接口与本地 RapidOcrModel 一致。"""

    is_proxy = True

    # 单次调用超时。首次调用要加载模型（GPU 建 session 更慢），
    # 因此给足余量；正常单帧只需数百毫秒。
    TIMEOUT = 120
    # 连接握手重试次数：服务进程刚拉起时端口已监听但模型可能还在加载。
    CONNECT_RETRY = 3
    # 传输故障后重放一次原请求；OCR 无副作用，重放不会产生重复操作。
    REQUEST_RETRY = 1
    # RPC 持续不可用时，fallback 期间最多每 30 秒探测/重启一次，避免每帧 OCR
    # 都重复经历连接、重启和请求重放，形成重启风暴。
    FALLBACK_RETRY_COOLDOWN = 30.0
    RECOVERABLE_ERRORS = (zerorpc.LostRemote, zerorpc.TimeoutExpired)
    # 重连阶段失败会包装成 ScriptError，公开 OCR 接口同样需要兜底。
    FALLBACK_ERRORS = RECOVERABLE_ERRORS + (ScriptError,)

    def __init__(self, address: str, timeout: int = None) -> None:
        self.address = _normalize_address(address)
        self.timeout = self.TIMEOUT if timeout is None else timeout
        self.client = None
        self._fallback_model = None
        # 记录下一次允许探测 RPC 的时间；0 表示当前 RPC 健康，无需限流。
        self._next_rpc_retry = 0.0
        # 同一实例可能有多个 OCR 调用并发失败，只允许其中一个执行重启。
        self._recovery_lock = threading.Lock()
        self._connect()

    @staticmethod
    def _close_client(client) -> None:
        """关闭失效连接；清理失败不能覆盖原始 RPC 异常。"""
        if client is None:
            return
        try:
            client.close()
        except Exception as e:
            logger.debug(f'Close OCR RPC client failed: {e}')

    def _connect(self) -> None:
        """创建新连接并完成 ping，失败的客户端不会继续复用。"""
        last_error = None
        for attempt in range(self.CONNECT_RETRY):
            client = zerorpc.Client(timeout=self.timeout)
            try:
                client.connect(self.address)
                client.ping()
                self.client = client
                return
            except Exception as e:
                last_error = e
                self._close_client(client)
                logger.warning(
                    f'OCR server ping failed ({attempt + 1}/{self.CONNECT_RETRY}): {e}'
                )
                if attempt + 1 < self.CONNECT_RETRY:
                    time.sleep(1.0)
        raise ScriptError(f'OCR server connection failed: {self.address}') from last_error

    def _recover_connection(self, failed_client) -> None:
        """恢复 RPC 连接；本机托管服务卡死时先重启服务。"""
        with self._recovery_lock:
            # 其它并发请求已经换好连接时，直接复用新连接，避免重复重启服务。
            if self.client is not failed_client:
                return
            self._close_client(failed_client)
            self.client = None
            try:
                restarted = restart_ocr_server(self.address)
            except Exception as e:
                # 服务清理自身失败也继续尝试重连，最终由公开接口执行本地兜底。
                logger.exception(f'Restart OCR server failed: {e}')
                restarted = False
            if restarted:
                logger.info('OCR server recovered, reconnecting failed request')
            else:
                logger.warning('OCR server was not restarted, reconnecting RPC client directly')
            self._connect()

    def _ensure_connection(self) -> None:
        """等待并复用并发恢复中的新连接，避免读到空连接。"""
        with self._recovery_lock:
            if self.client is None:
                # 上一次恢复若连不上会留下空连接；后续 OCR 仍应继续尝试连接或兜底。
                self._connect()

    def _call_with_recovery(self, method: str, *args):
        """执行 RPC 请求；连接故障恢复后重放同一次 OCR。"""
        for attempt in range(self.REQUEST_RETRY + 1):
            if self.client is None:
                self._ensure_connection()
            client = self.client
            try:
                return getattr(client, method)(*args)
            except self.RECOVERABLE_ERRORS as e:
                if attempt >= self.REQUEST_RETRY:
                    logger.error(f'OCR RPC request failed after recovery: {method}: {e}')
                    raise
                logger.warning(
                    f'OCR RPC request interrupted, recover and retry: {method}: {e}'
                )
                self._recover_connection(client)
        raise RuntimeError('Unreachable OCR RPC retry state')

    def _rpc_retry_allowed(self) -> bool:
        """判断并领取一次 fallback 后的 RPC 探测机会。"""
        now = time.monotonic()
        with self._recovery_lock:
            # 0 表示 RPC 当前健康：正常并发 OCR 都可直接请求，不做限流。
            if self._next_rpc_retry == 0.0:
                return True
            if now < self._next_rpc_retry:
                return False
            # 冷却到期后只放一个请求探测；探测完成会在成功/失败分支重设时间。
            self._next_rpc_retry = float('inf')
            return True

    def _get_local_fallback_model(self):
        """获取并缓存本地 OCR，避免 RPC 故障后每帧重复连接和加载。"""
        if self._fallback_model is None:
            from module.ocr.models import get_local_ocr_model
            self._fallback_model = get_local_ocr_model('ch')
            logger.warning('Switch OCR proxy to cached local fallback')
        return self._fallback_model

    def _local_ocr_single_line(self, image: np.ndarray):
        """执行本地兜底；本地模型异常必须向上抛出诊断错误，不能伪装成空结果。"""
        try:
            return self._get_local_fallback_model().ocr_single_line(image)
        except Exception as e:
            logger.exception(f'Local OCR fallback failed: {e}')
            raise ScriptError(f'Local OCR fallback failed: {e}') from e

    def _local_detect_and_ocr(self, image: np.ndarray, **kwargs):
        """执行本地检测兜底并保留原始异常上下文。"""
        try:
            return self._get_local_fallback_model().detect_and_ocr(image, **kwargs)
        except Exception as e:
            logger.exception(f'Local OCR fallback failed: {e}')
            raise ScriptError(f'Local OCR fallback failed: {e}') from e

    def ocr_single_line(self, image: np.ndarray):
        payload = pickle.dumps(image, protocol=4)
        if not self._rpc_retry_allowed():
            # RPC 仍处于冷却期，直接复用本地模型，不重复重启远端服务。
            return self._local_ocr_single_line(image)
        try:
            result = self._call_with_recovery('ocr_single_line', payload)
            if self._fallback_model is not None:
                self._fallback_model = None
                logger.info('OCR RPC recovered, stop using local fallback')
            self._next_rpc_retry = 0.0
            return result
        except self.FALLBACK_ERRORS as e:
            self._next_rpc_retry = time.monotonic() + self.FALLBACK_RETRY_COOLDOWN
            logger.error(f'OCR RPC unavailable after retry, fall back to local OCR: {e}')
            return self._local_ocr_single_line(image)

    def detect_and_ocr(
        self,
        image: np.ndarray,
        drop_score: float = 0.5,
        unclip_ratio: Optional[float] = None,
        box_thresh: Optional[float] = None,
        vertical: bool = False,
    ):
        payload = pickle.dumps(image, protocol=4)
        kwargs = {
            'drop_score': drop_score,
            'unclip_ratio': unclip_ratio,
            'box_thresh': box_thresh,
            'vertical': vertical,
        }
        if not self._rpc_retry_allowed():
            # 与单行识别相同：冷却期内只走缓存的本地 OCR。
            return self._local_detect_and_ocr(image, **kwargs)
        try:
            results = self._call_with_recovery(
                'detect_and_ocr', payload, drop_score, unclip_ratio, box_thresh, vertical
            )
            if self._fallback_model is not None:
                self._fallback_model = None
                logger.info('OCR RPC recovered, stop using local fallback')
            self._next_rpc_retry = 0.0
        except self.FALLBACK_ERRORS as e:
            self._next_rpc_retry = time.monotonic() + self.FALLBACK_RETRY_COOLDOWN
            logger.error(f'OCR RPC unavailable after retry, fall back to local OCR: {e}')
            return self._local_detect_and_ocr(image, **kwargs)
        return [
            BoxedResult(item["box"], None, item["ocr_text"], item["score"])
            for item in results
        ]



atexit.register(shutdown_ocr_server)
