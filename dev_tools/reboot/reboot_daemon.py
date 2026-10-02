"""
OAS守护进程 - RebootDaemon
支持开机自启、多实例状态监控与自动恢复
使用与OASX相同的REST API + WebSocket方式与OAS Server通信
"""
import os
import sys
import io
import time
import json
import ctypes
import logging
import subprocess
import asyncio
import threading
from pathlib import Path
from collections import defaultdict

import psutil
import requests

# 尝试导入websockets库
try:
    import websockets
except ImportError:
    print("错误: 缺少 'websockets' 库，请运行: pip install websockets")
    sys.exit(1)

# 尝试导入schedule库
try:
    import schedule
except ImportError:
    print("错误: 缺少 'schedule' 库，请运行: pip install schedule")
    sys.exit(1)

# 项目根目录
PROJECT_ROOT = Path(__file__).parent.parent.parent


# ──────────────────────── 实例配置模型 ────────────────────────

class InstanceConfig:
    """单个实例的监控配置"""
    def __init__(self, name: str, enabled: bool = True, auto_restart: bool = True,
                 max_restart_attempts: int = 5, restart_cooldown: int = 60):
        self.name = name
        self.enabled = enabled
        self.auto_restart = auto_restart
        self.max_restart_attempts = max_restart_attempts
        self.restart_cooldown = restart_cooldown  # 重启冷却时间(秒)


# ──────────────────────── NapCat 配置模型 ────────────────────────

class NapCatConfig:
    """NapCat守护进程配置"""
    def __init__(self, enable: bool = False, qq_account: str = '',
                 endpoint: str = 'http://127.0.0.1:3000',
                 access_token: str = '', napcat_dir: str = r'C:\NapCat',
                 start_cmd: str = 'launcher-win10.bat {qq}', check_interval: int = 30,
                 fail_count: int = 3, max_restart_attempts: int = 5,
                 restart_cooldown: int = 60):
        self.enable = enable
        self.qq_account = qq_account
        self.endpoint = endpoint
        self.access_token = access_token
        self.napcat_dir = napcat_dir
        self.start_cmd = start_cmd
        self.check_interval = check_interval
        self.fail_count = fail_count  # 连续健康检查失败多少次后重启
        self.max_restart_attempts = max_restart_attempts
        self.restart_cooldown = restart_cooldown


# ──────────────────────── NapCat 管理器 ────────────────────────

class NapCatManager:
    """
    NapCat进程管理器
    - 通过OneBot HTTP API健康检查
    - 崩溃自动重启
    - 支持手动启停
    """

    def __init__(self, config: NapCatConfig, logger: logging.Logger):
        self.config = config
        self.logger = logger
        self.process = None  # NapCat子进程引用
        self.consecutive_failures = 0  # 连续健康检查失败次数
        self.restart_count = 0  # 累计重启次数
        self.last_restart_time = 0  # 上次重启时间
        self.is_running_flag = False  # 内部状态标记

    def health_check(self) -> bool:
        """通过OneBot HTTP API检查NapCat是否健康"""
        try:
            headers = {'Content-Type': 'application/json'}
            if self.config.access_token:
                headers['Authorization'] = f'Bearer {self.config.access_token}'

            resp = requests.post(
                f"{self.config.endpoint}/get_login_info",
                json={},
                headers=headers,
                timeout=5
            )
            if resp.status_code == 200:
                data = resp.json()
                if data.get('status') == 'ok':
                    return True
            self.logger.warning(f"NapCat健康检查异常: status={resp.status_code}")
            return False
        except requests.exceptions.ConnectionError:
            self.logger.warning("NapCat无法连接，可能未运行")
            return False
        except requests.exceptions.Timeout:
            self.logger.warning("NapCat健康检查超时")
            return False
        except Exception as e:
            self.logger.error(f"NapCat健康检查出错: {e}")
            return False

    def is_alive(self) -> bool:
        """检查NapCat进程是否存活（子进程+API双重检查）"""
        # 子进程检查
        if self.process and self.process.poll() is None:
            # 进程在运行，再验证API
            return self.health_check()
        # 没有子进程引用，尝试API检查
        return self.health_check()

    def start(self) -> bool:
        """启动NapCat"""
        if self.is_alive():
            self.logger.info("NapCat已在运行")
            self.is_running_flag = True
            return True

        # 构建启动命令: 将 {qq} 占位符替换为 QQ 号
        # 支持的 NapCat 启动方式（带 QQ 号参数自动登录）:
        #   Shell版:     launcher.bat {qq}              (Win11)
        #                launcher-win10.bat {qq}         (Win10)
        #   一键版:      NapCatWinBootMain.exe {qq}
        #   自定义:      napcat.bat {qq}
        cmd = self.config.start_cmd
        if '{qq}' in cmd:
            if not self.config.qq_account:
                self.logger.error("start_cmd 包含 {qq} 占位符但未配置 qq_account")
                return False
            cmd = cmd.replace('{qq}', self.config.qq_account)
        elif self.config.qq_account and self.config.qq_account not in cmd:
            # 兼容旧配置: 无占位符且命令中未包含QQ号时追加
            cmd = f'{cmd} {self.config.qq_account}'

        self.logger.info(f"正在启动NapCat: cd /d {self.config.napcat_dir} && {cmd}")
        try:
            napcat_dir = Path(self.config.napcat_dir)
            if not napcat_dir.exists():
                self.logger.error(f"NapCat目录不存在: {self.config.napcat_dir}")
                return False

            # Windows下隐藏子进程窗口
            startupinfo = None
            creationflags = 0
            if sys.platform.startswith('win'):
                startupinfo = subprocess.STARTUPINFO()
                startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
                startupinfo.wShowWindow = 0  # SW_HIDE
                creationflags = subprocess.CREATE_NO_WINDOW

            self.process = subprocess.Popen(
                cmd,
                cwd=str(napcat_dir),
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                shell=True,
                startupinfo=startupinfo,
                creationflags=creationflags
            )

            self.logger.info(f"NapCat进程已启动, PID: {self.process.pid}")

            # 等待NapCat启动完成（最多60秒）
            timeout = 60
            start_time = time.time()
            while time.time() - start_time < timeout:
                if self.health_check():
                    self.logger.info("NapCat启动成功，API已就绪")
                    self.is_running_flag = True
                    self.consecutive_failures = 0
                    return True
                time.sleep(3)

            self.logger.error("NapCat启动超时，API未就绪")
            return False

        except Exception as e:
            self.logger.error(f"启动NapCat失败: {e}")
            return False

    def stop(self) -> bool:
        """停止NapCat"""
        self.logger.info("正在停止NapCat...")

        # 方式1: 终止子进程
        if self.process and self.process.poll() is None:
            try:
                self.process.terminate()
                self.process.wait(timeout=10)
                self.logger.info("NapCat进程已终止")
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.logger.info("NapCat进程已强制终止")

        # 方式2: 清理残留的QQ/NapCat进程
        if sys.platform.startswith('win'):
            try:
                _subproc_flags = subprocess.CREATE_NO_WINDOW
                # 按窗口标题关闭
                subprocess.run('taskkill /fi "windowtitle eq NapCat*" /f',
                               shell=True, capture_output=True, timeout=5,
                               creationflags=_subproc_flags)
                # 关闭QQ进程（NapCat依赖）
                subprocess.run('taskkill /fi "imagename eq QQ.exe" /f',
                               shell=True, capture_output=True, timeout=5,
                               creationflags=_subproc_flags)
            except Exception as e:
                self.logger.debug(f"清理残留进程时出错(可忽略): {e}")

        self.process = None
        self.is_running_flag = False
        return True

    def restart(self) -> bool:
        """重启NapCat"""
        self.logger.info("正在重启NapCat...")
        now = time.time()

        # 冷却检查
        if now - self.last_restart_time < self.config.restart_cooldown:
            remaining = int(self.config.restart_cooldown - (now - self.last_restart_time))
            self.logger.info(f"NapCat重启冷却中，剩余 {remaining} 秒")
            return False

        # 重启次数检查
        if self.restart_count >= self.config.max_restart_attempts:
            self.logger.warning(
                f"NapCat累计重启 {self.restart_count} 次，已达上限 {self.config.max_restart_attempts}，暂停自动重启")
            return False

        self.last_restart_time = now
        self.stop()
        time.sleep(3)
        success = self.start()
        if success:
            self.restart_count = 0
            self.logger.info("NapCat重启成功")
        else:
            self.restart_count += 1
            self.logger.error(f"NapCat重启失败 (累计第 {self.restart_count} 次)")
        return success

    def reset_restart_count(self):
        """重置重启计数（NapCat稳定运行后调用）"""
        if self.restart_count > 0 or self.consecutive_failures > 0:
            self.restart_count = 0
            self.consecutive_failures = 0

    def monitor(self) -> bool:
        """
        单次监控检查，返回NapCat是否健康
        如果连续失败次数达到阈值，触发自动重启
        """
        if not self.config.enable:
            return True

        if self.health_check():
            self.consecutive_failures = 0
            self.is_running_flag = True
            self.reset_restart_count()
            return True

        self.consecutive_failures += 1
        self.logger.warning(
            f"NapCat健康检查失败 (连续第 {self.consecutive_failures}/{self.config.fail_count} 次)")

        if self.consecutive_failures >= self.config.fail_count:
            self.logger.warning("连续失败次数达到阈值，触发NapCat自动重启")
            self.restart()
            return False

        return False


# ──────────────────────── 状态枚举 ────────────────────────

class ScriptState:
    INACTIVE = 0
    RUNNING = 1
    WARNING = 2
    UPDATING = 3

    STATE_MAP = {
        0: 'INACTIVE',
        1: 'RUNNING',
        2: 'WARNING',
        3: 'UPDATING'
    }

    @classmethod
    def to_str(cls, value: int) -> str:
        return cls.STATE_MAP.get(value, f'UNKNOWN({value})')


# ──────────────────────── 主守护进程类 ────────────────────────

class RebootDaemon:
    """
    OAS守护进程
    - 使用REST API检测/启动OAS Server
    - 使用WebSocket与每个OAS实例建立长连接，实时接收状态推送
    - 监控实例状态，自动重启挂掉的实例
    - 支持定时重启系统
    """

    def __init__(self, config_path: str = None):
        self.running = False

        # OAS Server子进程引用
        self.oas_process = None

        # 实例配置与运行时状态
        self.instance_configs: dict[str, InstanceConfig] = {}
        self.instance_states: dict[str, str] = {}
        self.instance_restart_counts: dict[str, int] = defaultdict(int)
        self.instance_last_restart: dict[str, float] = {}
        # 实例调度快照（server 通过 WS 推送的 get_schedule_data 结果）：
        # pending 非空表示有任务已过期该跑，据此区分「正常空闲」与「异常停止」
        self.instance_schedules: dict[str, dict] = {}
        # 待验证的拉起：拉起后需在下个监控周期确认仍是 RUNNING 才算成功并归零计数，
        # 否则「start 指令送达即归零」会让重启上限永远失效
        self.instance_pending_verify: dict[str, bool] = {}
        # 已放弃自动拉起的实例（达重启上限）：不重置，直到守护进程重启
        self.instance_gave_up: dict[str, bool] = {}

        # WebSocket 长连接相关（延迟初始化，必须在事件循环中创建）
        self.ws_connections: dict = {}
        self.ws_locks: dict = {}
        self.ws_response_queues: dict = {}
        self.ws_handler_tasks: dict = {}
        self.ws_connected: dict = {}

        # 守护进程事件循环（在独立线程中运行）
        self._loop: asyncio.AbstractEventLoop = None
        self._async_initialized = False

        # 定时重启配置
        self.reboot_time = None
        self.reboot_weekday = None

        # 其他配置（在_load_config中设置默认值）
        self.server_auto_start = True
        self.server_startup_timeout = 60
        self.server_restart_on_crash = True
        self.monitor_interval = 30
        self.restart_cooldown = 60

        # NapCat管理器（在_load_config中初始化）
        self.napcat_manager: NapCatManager = None
        self._raw_config: dict = {}  # 原始配置字典，供_init_napcat_manager使用

        # 加载配置
        self.config_path = Path(config_path) if config_path else Path(__file__).parent / 'daemon_config.json'
        self._load_config()

        # 配置日志（必须在加载配置后，因为可能用到logger）
        self._setup_logging()

        # 初始化NapCat管理器（必须在日志初始化之后）
        self._init_napcat_manager(self._raw_config)

        # 设置定时重启
        if self.reboot_time:
            self._setup_reboot_schedule()

    # ──────────────── 配置加载 ────────────────

    def _load_config(self):
        """从daemon_config.json加载配置（所有配置均从文件读取，无硬编码默认值）"""
        # 设置默认值
        self.api_host = '127.0.0.1'
        self.api_port = 22288
        self.api_url = f"http://{self.api_host}:{self.api_port}"

        if not self.config_path.exists():
            return

        try:
            with open(self.config_path, 'r', encoding='utf-8') as f:
                config = json.load(f)
        except Exception as e:
            print(f"加载配置文件失败: {e}")
            return

        # 保存原始配置字典，供_init_napcat_manager等使用
        self._raw_config = config

        # API配置
        self.api_host = config.get('api_host', self.api_host)
        self.api_port = config.get('api_port', self.api_port)
        self.api_url = f"http://{self.api_host}:{self.api_port}"

        # 定时重启
        self.reboot_time = config.get('reboot_time', None)
        self.reboot_weekday = config.get('reboot_weekday', None)

        # OAS Server启动配置
        self.server_auto_start = config.get('server_auto_start', True)
        self.server_startup_timeout = config.get('server_startup_timeout', 60)
        self.server_restart_on_crash = config.get('server_restart_on_crash', True)

        # 监控配置
        self.monitor_interval = config.get('monitor_interval', 30)
        self.restart_cooldown = config.get('restart_cooldown', 60)

        # 加载实例配置
        instances_raw = config.get('instances', [])
        for item in instances_raw:
            if isinstance(item, str):
                ic = InstanceConfig(name=item)
            elif isinstance(item, dict):
                ic = InstanceConfig(
                    name=item.get('name', ''),
                    enabled=item.get('enabled', True),
                    auto_restart=item.get('auto_restart', True),
                    max_restart_attempts=item.get('max_restart_attempts', 5),
                    restart_cooldown=item.get('restart_cooldown', self.restart_cooldown)
                )
            else:
                continue

            if ic.name:
                self.instance_configs[ic.name] = ic
                self.ws_connected[ic.name] = False
                self.instance_states[ic.name] = 'UNKNOWN'

    def _setup_logging(self):
        log_dir = Path(__file__).parent

        # 控台Handler：pythonw无控制台时跳过
        handlers = []
        if sys.stdout and hasattr(sys.stdout, 'buffer'):
            console_handler = logging.StreamHandler(
                io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
            )
            console_handler.setFormatter(logging.Formatter('%(asctime)s - %(levelname)s - %(message)s'))
            handlers.append(console_handler)

        # 文件Handler：使用UTF-8 BOM编码，Windows记事本可自动识别
        file_handler = logging.FileHandler(log_dir / 'daemon.log', encoding='utf-8-sig')
        file_handler.setFormatter(logging.Formatter('%(asctime)s - %(levelname)s - %(message)s'))
        handlers.append(file_handler)

        logging.basicConfig(
            level=logging.INFO,
            handlers=handlers
        )
        self.logger = logging.getLogger('RebootDaemon')

    def _init_napcat_manager(self, config: dict):
        """从配置字典初始化NapCat管理器"""
        napcat_raw = config.get('napcat', {})
        if napcat_raw and napcat_raw.get('enable', False):
            nc_cfg = NapCatConfig(
                enable=napcat_raw.get('enable', False),
                qq_account=str(napcat_raw.get('qq_account', '')),
                endpoint=napcat_raw.get('endpoint', 'http://127.0.0.1:3000'),
                access_token=napcat_raw.get('access_token', ''),
                napcat_dir=napcat_raw.get('napcat_dir', r'C:\\NapCat'),
                start_cmd=napcat_raw.get('start_cmd', 'launcher-win10.bat {qq}'),
                check_interval=napcat_raw.get('check_interval', 30),
                fail_count=napcat_raw.get('fail_count', 3),
                max_restart_attempts=napcat_raw.get('max_restart_attempts', 5),
                restart_cooldown=napcat_raw.get('restart_cooldown', 60)
            )
            self.napcat_manager = NapCatManager(nc_cfg, self.logger)
            self.logger.info(f"NapCat守护已启用: qq={nc_cfg.qq_account or '未配置'}, endpoint={nc_cfg.endpoint}, dir={nc_cfg.napcat_dir}")
        else:
            self.napcat_manager = None

    def _setup_reboot_schedule(self):
        if self.reboot_weekday is None:
            schedule.every().day.at(self.reboot_time).do(self._reboot_system)
            self.logger.info(f"已设置每日 {self.reboot_time} 重启系统")
        else:
            weekdays = {
                0: schedule.every().monday,
                1: schedule.every().tuesday,
                2: schedule.every().wednesday,
                3: schedule.every().thursday,
                4: schedule.every().friday,
                5: schedule.every().saturday,
                6: schedule.every().sunday
            }
            if self.reboot_weekday in weekdays:
                weekdays[self.reboot_weekday].at(self.reboot_time).do(self._reboot_system)
                names = ['周一', '周二', '周三', '周四', '周五', '周六', '周日']
                self.logger.info(f"已设置每{names[self.reboot_weekday]} {self.reboot_time} 重启系统")

    # ──────────────── 异步对象延迟初始化 ────────────────

    def _ensure_async_objects(self):
        """在事件循环中初始化异步对象，避免 'bound to a different loop' 错误"""
        if self._async_initialized:
            return
        self._async_initialized = True
        for name in self.instance_configs:
            if name not in self.ws_locks:
                self.ws_locks[name] = asyncio.Lock()
            if name not in self.ws_response_queues:
                self.ws_response_queues[name] = asyncio.Queue()
            self.ws_connected[name] = False

    # ──────────────── OAS Server管理 ────────────────

    def _is_server_online(self) -> bool:
        """使用REST API GET /test 检查OAS Server是否在线"""
        try:
            resp = requests.get(f"{self.api_url}/test", timeout=5)
            return resp.status_code == 200 and resp.text.strip('"') == 'success'
        except requests.exceptions.RequestException:
            return False

    def _get_server_port(self) -> int:
        """从deploy配置中读取WebuiPort，读取失败则回退到self.api_port"""
        try:
            deploy_yaml = PROJECT_ROOT / "config" / "deploy.yaml"
            if deploy_yaml.exists():
                with open(deploy_yaml, 'r', encoding='utf-8') as f:
                    for line in f:
                        line = line.strip()
                        if line.startswith('WebuiPort:'):
                            port_str = line.split(':', 1)[1].strip()
                            port = int(port_str)
                            self.logger.info(f"从deploy.yaml读取到WebuiPort: {port}")
                            return port
        except Exception as e:
            self.logger.warning(f"读取deploy.yaml中的WebuiPort失败: {e}，使用配置端口 {self.api_port}")
        return self.api_port

    def _find_server_listener_pid(self):
        """返回当前 API 端口的监听进程 PID；无法可靠判断时返回 None。"""
        try:
            for conn in psutil.net_connections(kind='tcp'):
                if conn.status != psutil.CONN_LISTEN or not conn.laddr:
                    continue
                port = conn.laddr.port if hasattr(conn.laddr, 'port') else conn.laddr[1]
                if port == self.api_port:
                    return conn.pid
        except (psutil.Error, OSError) as e:
            # 端口归属只用于回收空壳，判断失败时必须保守地保留进程
            self.logger.debug(f"读取 OAS Server 监听进程失败: {e}")
        return None

    def _reap_stale_server_process(self) -> bool:
        """服务由其他 PID 提供时，回收守护自己遗留的 pythonw 空壳。"""
        proc = self.oas_process
        if not proc:
            return False
        if proc.poll() is not None:
            self.oas_process = None
            return False

        listener_pid = self._find_server_listener_pid()
        if not listener_pid or listener_pid == proc.pid:
            return False

        # GUI 更新会关闭原 Server 并启动新 Server，原 pythonw 外壳可能继续存活；
        # 端口已明确属于另一个 PID 时才能安全回收，避免误杀真正提供服务的进程。
        self.logger.warning(
            f"OAS Server 进程 PID={proc.pid} 已不再监听端口 {self.api_port}，"
            f"当前监听 PID={listener_pid}，回收残留空壳")
        self._terminate_server_process()
        return True

    def _start_oas_server(self) -> bool:
        """启动OAS Server子进程"""
        # 「是否在运行」必须以服务健康为准，不能只看子进程句柄。
        # 空壳陷阱：Web UI 点「关闭服务器」(/home/kill_server) 或 uvicorn 自行退出后，
        # Windows 下 pythonw 外壳可能残留——uvicorn 已打完 "Finished server process"
        # 并释放端口，poll() 却仍返回 None。旧代码据此直接返回 True，于是
        # server_restart_on_crash 永久失效：检测到离线、决定重启，执行时被句柄骗过，
        # 什么都没做，此后守护再不记录一行 server 日志（实测沉默了 14 小时）。
        if self._is_server_online():
            self._reap_stale_server_process()
            self.logger.info("OAS Server已在运行")
            return True
        if self.oas_process and self.oas_process.poll() is None:
            # 句柄还在但服务不可用，就是空壳。必须先回收，否则它会一直骗过后续判断
            self.logger.warning(
                f"OAS Server 进程 PID={self.oas_process.pid} 存在但服务不可用（空壳），先回收")
            self._terminate_server_process()

        try:
            server_port = self._get_server_port()
            cmd = [
                sys.executable, str(PROJECT_ROOT / "server.py"),
                "--host", self.api_host,
                "--port", str(server_port)
            ]
            # 确保log目录存在
            log_dir = PROJECT_ROOT / "log"
            log_dir.mkdir(parents=True, exist_ok=True)
            # 将server的stderr输出到日志文件，方便排查启动失败问题
            server_log = open(log_dir / "server_subprocess.log", 'a', encoding='utf-8')
            # Windows下使用CREATE_NO_WINDOW避免弹出子进程窗口
            startupinfo = None
            creationflags = 0
            if sys.platform.startswith('win'):
                startupinfo = subprocess.STARTUPINFO()
                startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
                startupinfo.wShowWindow = 0  # SW_HIDE
                creationflags = subprocess.CREATE_NO_WINDOW

            self.oas_process = subprocess.Popen(
                cmd,
                stdout=server_log,
                stderr=server_log,
                cwd=str(PROJECT_ROOT),
                startupinfo=startupinfo,
                creationflags=creationflags
            )
            # 如果daemon配置的端口与server实际端口不同，更新api_url
            if server_port != self.api_port:
                self.logger.warning(
                    f"daemon配置端口 {self.api_port} 与deploy.yaml中的WebuiPort {server_port} 不一致，"
                    f"自动切换到 {server_port}")
                self.api_port = server_port
                self.api_url = f"http://{self.api_host}:{self.api_port}"
            self.logger.info(f"OAS Server启动中，PID: {self.oas_process.pid}，端口: {server_port}")

            timeout = self.server_startup_timeout
            start_time = time.time()
            while time.time() - start_time < timeout:
                if self._is_server_online():
                    self.logger.info("OAS Server已就绪")
                    return True
                time.sleep(2)

            self.logger.error("等待OAS Server启动超时")
            return False
        except Exception as e:
            self.logger.error(f"启动OAS Server失败: {e}")
            return False

    def _terminate_server_process(self):
        """终止自己启动的 server 子进程并清掉句柄引用。

        句柄必须置 None：留着一个已退出的 Popen 会让后续判断继续围着它转，
        而真正提供服务的可能是另一个进程。
        """
        proc = self.oas_process
        if not proc:
            return
        if proc.poll() is None:
            try:
                proc.terminate()
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    self.logger.error(f"OAS Server 进程 PID={proc.pid} 无法终止")
            self.logger.info(f"OAS Server进程已终止 (PID={proc.pid})")
        self.oas_process = None

    def _stop_oas_server(self):
        """停止OAS Server：先请 API 自行关闭，再回收自己启动的子进程。

        注意 API 关闭作用于 self.api_url 指向的 server，而子进程回收作用于
        self.oas_process——两者可能不是同一个进程（例如手动另起了一个 server 抢占了
        端口）。所以两步都要做，且各自记录，不能用一步的结果推断另一步。
        """
        try:
            requests.get(f"{self.api_url}/home/kill_server", timeout=5)
            self.logger.info("已通过API关闭OAS Server")
        except Exception:
            pass

        # API 关闭后 uvicorn 会自行退出，但外壳未必立刻消失，这里统一回收
        self._terminate_server_process()

    def _ensure_server_running(self) -> bool:
        """确保OAS Server在运行，返回是否确认在线。"""
        if self._is_server_online():
            return True
        if not self.server_restart_on_crash:
            self.logger.warning("OAS Server离线，且未配置自动重启")
            return False
        self.logger.warning("OAS Server离线，尝试重启...")
        if not self._start_oas_server():
            return False
        # 启动流程返回 True 也要复验：否则一旦启动路径判断出错（如被残留句柄骗过），
        # 守护会以为 server 好着而彻底沉默，不再有任何 server 相关日志
        if self._is_server_online():
            return True
        self.logger.error("OAS Server 启动流程报成功但服务仍不可用")
        return False

    # ──────────────── REST API 调用 ────────────────

    def _api_start_instance(self, instance_name: str) -> bool:
        """使用REST API启动实例: GET /{script_name}/start"""
        try:
            resp = requests.get(f"{self.api_url}/{instance_name}/start", timeout=10)
            self.logger.info(f"REST API 启动实例 {instance_name}: status={resp.status_code}")
            return resp.status_code == 200
        except requests.exceptions.RequestException as e:
            self.logger.error(f"REST API 启动实例 {instance_name} 失败: {e}")
            return False

    def _api_stop_instance(self, instance_name: str) -> bool:
        """使用REST API停止实例: GET /{script_name}/stop"""
        try:
            resp = requests.get(f"{self.api_url}/{instance_name}/stop", timeout=10)
            self.logger.info(f"REST API 停止实例 {instance_name}: status={resp.status_code}")
            return resp.status_code == 200
        except requests.exceptions.RequestException as e:
            self.logger.error(f"REST API 停止实例 {instance_name} 失败: {e}")
            return False

    # ──────────────── WebSocket 通信（与OASX一致） ────────────────

    async def _ws_connect(self, instance_name: str):
        """
        建立WebSocket长连接，与OASX实现方式一致
        URL: ws://{host}:{port}/ws/{script_name}
        """
        self._ensure_async_objects()

        if instance_name in self.ws_connections:
            ws = self.ws_connections[instance_name]
            if not ws.closed and self.ws_connected.get(instance_name, False):
                return ws

        ws_url = f"ws://{self.api_host}:{self.api_port}/ws/{instance_name}"

        try:
            ws = await websockets.connect(ws_url, timeout=10,
                                          ping_interval=30, ping_timeout=20)
            self.ws_connections[instance_name] = ws
            self.ws_connected[instance_name] = True
            self.logger.info(f"实例 {instance_name} WebSocket 长连接已建立")

            # 取消旧的消息监听任务
            if instance_name in self.ws_handler_tasks:
                old_task = self.ws_handler_tasks[instance_name]
                if not old_task.done():
                    old_task.cancel()
                    try:
                        await old_task
                    except asyncio.CancelledError:
                        pass

            # 创建新的消息监听任务
            task = asyncio.create_task(self._ws_message_handler(instance_name, ws))
            self.ws_handler_tasks[instance_name] = task

            return ws
        except Exception as e:
            self.logger.error(f"实例 {instance_name} WebSocket 连接失败: {e}")
            self.ws_connected[instance_name] = False
            if instance_name in self.ws_connections:
                try:
                    if not self.ws_connections[instance_name].closed:
                        await self.ws_connections[instance_name].close()
                except Exception:
                    pass
                del self.ws_connections[instance_name]
            return None

    async def _ws_message_handler(self, instance_name: str, ws):
        """
        处理WebSocket消息（与OASX的wsListener一致）
        接收服务端推送的状态和调度信息
        """
        try:
            async for message in ws:
                try:
                    if not isinstance(message, str):
                        continue

                    # JSON消息：状态或调度
                    if message.startswith('{') and message.endswith('}'):
                        data = json.loads(message)

                        # 处理状态更新
                        if 'state' in data:
                            state_value = data['state']
                            state_str = ScriptState.to_str(state_value)
                            old_state = self.instance_states.get(instance_name, 'UNKNOWN')
                            self.instance_states[instance_name] = state_str

                            if old_state != state_str:
                                self.logger.info(
                                    f"实例 {instance_name} 状态变更: {old_state} -> {state_str}")

                        # 处理调度信息
                        elif 'schedule' in data:
                            # 存下调度快照：pending 非空 = 有任务已过期该跑，
                            # 是区分「正常空闲」与「异常停止」的唯一可靠判据
                            if isinstance(data['schedule'], dict):
                                self.instance_schedules[instance_name] = data['schedule']
                            self.logger.debug(
                                f"实例 {instance_name} 调度信息: {data['schedule']}")

                    # 放入响应队列供请求-响应模式使用
                    if instance_name in self.ws_response_queues:
                        await self.ws_response_queues[instance_name].put(message)

                except json.JSONDecodeError:
                    if instance_name in self.ws_response_queues:
                        await self.ws_response_queues[instance_name].put(message)
                except Exception as e:
                    self.logger.error(f"处理实例 {instance_name} 消息时出错: {e}")

        except websockets.exceptions.ConnectionClosedOK:
            self.logger.info(f"实例 {instance_name} WebSocket连接正常关闭")
        except websockets.exceptions.ConnectionClosedError as e:
            self.logger.warning(f"实例 {instance_name} WebSocket连接异常关闭: {e}")
        except asyncio.CancelledError:
            pass  # 正常取消，不需要警告
        except Exception as e:
            self.logger.error(f"实例 {instance_name} WebSocket监听出错: {e}")
        finally:
            self.ws_connected[instance_name] = False

    async def _ws_send(self, instance_name: str, message: str, timeout: float = 10):
        """通过WebSocket长连接发送消息并等待响应"""
        self._ensure_async_objects()

        lock = self.ws_locks.get(instance_name)
        if lock is None:
            return None

        async with lock:
            ws = await self._ws_connect(instance_name)
            if ws and not ws.closed and self.ws_connected.get(instance_name, False):
                try:
                    await ws.send(message)
                    queue = self.ws_response_queues.get(instance_name)
                    if queue:
                        try:
                            response = await asyncio.wait_for(queue.get(), timeout=timeout)
                            return response
                        except asyncio.TimeoutError:
                            self.logger.warning(f"实例 {instance_name} WS请求超时")
                            return None
                except Exception as e:
                    self.logger.warning(f"实例 {instance_name} WS通信失败: {e}")
                    if instance_name in self.ws_connections:
                        try:
                            await self.ws_connections[instance_name].close()
                        except Exception:
                            pass
                        del self.ws_connections[instance_name]
                    self.ws_connected[instance_name] = False
                    return None
        return None

    async def _ws_start_instance(self, instance_name: str) -> bool:
        """通过WebSocket发送start指令启动实例，失败回退REST API"""
        response = await self._ws_send(instance_name, "start")
        if response:
            self.logger.info(f"通过WS启动实例 {instance_name}: {response}")
            return True
        else:
            self.logger.warning(f"通过WS启动实例 {instance_name} 失败，回退到REST API")
            return self._api_start_instance(instance_name)

    async def _ws_stop_instance(self, instance_name: str) -> bool:
        """通过WebSocket发送stop指令停止实例，失败回退REST API"""
        response = await self._ws_send(instance_name, "stop")
        if response:
            self.logger.info(f"通过WS停止实例 {instance_name}: {response}")
            return True
        else:
            self.logger.warning(f"通过WS停止实例 {instance_name} 失败，回退到REST API")
            return self._api_stop_instance(instance_name)

    async def _ws_get_state(self, instance_name: str) -> str:
        """通过WebSocket获取实例状态"""
        response = await self._ws_send(instance_name, "get_state", timeout=5)
        if response:
            try:
                if response.startswith('{'):
                    data = json.loads(response)
                    if 'state' in data:
                        return ScriptState.to_str(data['state'])
            except Exception:
                pass
        return self.instance_states.get(instance_name, 'UNKNOWN')

    async def _close_all_websockets(self):
        """关闭所有WebSocket连接"""
        for name, task in list(self.ws_handler_tasks.items()):
            if not task.done():
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass

        for name, ws in list(self.ws_connections.items()):
            try:
                if not ws.closed:
                    await ws.close()
            except Exception:
                pass
        self.ws_connections.clear()

        for name in self.instance_configs:
            self.ws_connected[name] = False

    # ──────────────── 实例监控与自动恢复 ────────────────

    def _has_enabled_task(self, instance_name: str) -> bool:
        """实例是否还有启用的任务（据 server 推送的调度快照判断）。

        OAS 跑完任务会主动退出到 INACTIVE，所以「空闲」与「崩溃」的当前状态完全一样，
        靠状态本身无法区分。判据是快照里还有没有任务：不论已过期的 pending、正在跑的
        running，还是等未来时刻的 waiting，只要还有一个，这个实例就还有活要干，
        进程就不该是死的——没有进程在等，那个 waiting 任务永远不会被执行。

        原先只认 pending/running，把「在等未来任务」与「已死」混为一谈：实例正好崩在
        只剩 waiting 任务的时刻就永远等不到拉起（实测配置损坏退出后无人接管）。
        而「实例活着但空闲」那个担心的场景状态是 RUNNING，本来就不触发拉起判定。

        三者全空说明所有任务都禁用了，拉起也没意义，返回 False 避免空转。
        取不到快照同样返回 False：每轮都会重新收到推送，宁可晚一轮也不空拉。
        """
        schedule = self.instance_schedules.get(instance_name)
        if not isinstance(schedule, dict):
            return False
        return bool(schedule.get('pending')
                    or schedule.get('running')
                    or schedule.get('waiting'))

    def _should_auto_restart(self, instance_name: str) -> bool:
        """判断实例是否应该自动重启"""
        ic = self.instance_configs.get(instance_name)
        if not ic or not ic.enabled or not ic.auto_restart:
            return False

        # 已达上限放弃过的实例不再拉起，直到守护进程重启
        if self.instance_gave_up.get(instance_name):
            return False

        restart_count = self.instance_restart_counts.get(instance_name, 0)
        if restart_count >= ic.max_restart_attempts:
            self.logger.error(
                f"实例 {instance_name} 连续拉起失败 {restart_count} 次，"
                f"已达上限 {ic.max_restart_attempts}，放弃自动拉起"
                f"（守护进程重启后才会重新接管）")
            self.instance_gave_up[instance_name] = True
            self._notify_give_up(instance_name, restart_count)
            return False

        last_restart = self.instance_last_restart.get(instance_name, 0)
        cooldown = ic.restart_cooldown
        if time.time() - last_restart < cooldown:
            remaining = int(cooldown - (time.time() - last_restart))
            self.logger.debug(f"实例 {instance_name} 重启冷却中，剩余 {remaining} 秒")
            return False

        return True

    def _notify_give_up(self, instance_name: str, restart_count: int) -> None:
        """实例被放弃自动拉起时打醒目日志。

        这里不走 NapCat 推送：NapCat 本身是被守护对象，用它报警会在它挂掉时静默，
        而那正是需要报警的场景之一。留作日志，由外部日志监控或人工发现。
        """
        self.logger.error("=" * 60)
        self.logger.error(f"实例 {instance_name} 已放弃自动拉起（累计失败 {restart_count} 次）")
        self.logger.error("请人工检查该实例；守护进程重启后会重新接管")
        self.logger.error("=" * 60)

    async def _restart_instance(self, instance_name: str) -> bool:
        """重启指定实例。

        注意：这里不再因为 start 指令送达就把失败计数归零——那会让重启上限永远失效
        （实例启动后立刻又死也算成功）。改为标记待验证，由监控循环在下个周期确认
        实例仍是 RUNNING 才归零，否则计数 +1。
        """
        self.logger.info(f"正在重启实例 {instance_name}...")
        self.instance_last_restart[instance_name] = time.time()

        # 先停止
        await self._ws_stop_instance(instance_name)
        await asyncio.sleep(3)

        # 再启动
        success = await self._ws_start_instance(instance_name)
        if success:
            # 指令送达 ≠ 真的活着，留到下个周期验证
            self.instance_pending_verify[instance_name] = True
            self.logger.info(
                f"实例 {instance_name} 启动指令已下发，待下个周期验证是否稳定运行")
        else:
            self.instance_restart_counts[instance_name] += 1
            self.logger.error(
                f"实例 {instance_name} 重启失败 "
                f"(连续第 {self.instance_restart_counts[instance_name]} 次)")

        return success

    async def _monitor_loop(self):
        """核心监控循环"""
        self.logger.info("监控循环启动")

        while self.running:
            # 1. 确保Server在线
            if not self._is_server_online():
                self.logger.warning("OAS Server离线")
                if not self._ensure_server_running():
                    self.logger.error("无法启动OAS Server，等待下次检查")
                    await asyncio.sleep(self.monitor_interval)
                    continue
                await asyncio.sleep(5)
            else:
                # GUI 更新可能已用新 PID 接管端口，在线时也要清理守护留下的旧空壳
                self._reap_stale_server_process()

            # 2. 为所有启用的实例建立/维护WS连接
            for name, ic in self.instance_configs.items():
                if not ic.enabled:
                    continue
                if not self.ws_connected.get(name, False):
                    self.logger.info(f"实例 {name} WebSocket未连接，尝试连接...")
                    await self._ws_connect(name)

            # 3. 检查各实例状态，处理异常
            for name, ic in self.instance_configs.items():
                if not ic.enabled:
                    continue

                state = self.instance_states.get(name, 'UNKNOWN')

                # 3.1 先验证上一轮拉起的结果：仍是 RUNNING 才算真成功并归零计数
                if self.instance_pending_verify.get(name):
                    self.instance_pending_verify[name] = False
                    if state == 'RUNNING':
                        self.instance_restart_counts[name] = 0
                        self.logger.info(f"实例 {name} 拉起后稳定运行，重置失败计数")
                    else:
                        self.instance_restart_counts[name] += 1
                        self.logger.warning(
                            f"实例 {name} 拉起后未能稳定运行（当前 {state}），"
                            f"累计失败 {self.instance_restart_counts[name]} 次")

                # 3.2 判定异常：INACTIVE 需配合「实例还有启用任务」才算异常。
                # OAS 跑完任务会主动退出到 INACTIVE，两者状态一样；只要还有任务
                # （含等未来时刻的 waiting），就必须有进程在等它，否则那个任务
                # 永远不会执行。全部任务禁用时不拉起，避免无意义空转
                if state in ('WARNING', 'UNKNOWN') or (state == 'INACTIVE' and self._has_enabled_task(name)):
                    if self._should_auto_restart(name):
                        self.logger.warning(
                            f"实例 {name} 状态异常: {state}，尝试自动重启")
                        await self._restart_instance(name)
                    elif state == 'WARNING':
                        actual_state = await self._ws_get_state(name)
                        self.logger.info(f"实例 {name} 刷新状态: {actual_state}")
                elif state == 'RUNNING':
                    self.instance_restart_counts[name] = 0

            # 4. 活跃状态汇报
            states_str = ", ".join(
                f"{n}={self.instance_states.get(n, '?')}"
                for n in self.instance_configs
            )
            napcat_status = ""
            if self.napcat_manager:
                napcat_status = f", NapCat={'运行中' if self.napcat_manager.is_running_flag else '异常'}"
            self.logger.info(f"实例状态: [{states_str}]{napcat_status}")

            # 5. NapCat健康监控
            if self.napcat_manager:
                self.napcat_manager.monitor()

            await asyncio.sleep(self.monitor_interval)

    # ──────────────── 系统重启 ────────────────

    def _reboot_system(self):
        """定时重启系统"""
        self.logger.info("开始执行系统重启...")

        for name in self.instance_configs:
            try:
                self._api_stop_instance(name)
            except Exception:
                pass

        time.sleep(3)
        self._stop_oas_server()
        time.sleep(5)

        try:
            if sys.platform.startswith('win'):
                flags = subprocess.CREATE_NO_WINDOW
                subprocess.run(['shutdown', '/r', '/t', '10'], check=True,
                               creationflags=flags)
                self.logger.info("系统将在10秒后重启")
            elif sys.platform.startswith('linux') or sys.platform.startswith('darwin'):
                subprocess.run(['sudo', 'reboot'], check=True)
            else:
                self.logger.error(f"不支持的操作系统: {sys.platform}")
        except subprocess.CalledProcessError as e:
            self.logger.error(f"重启系统失败: {e}")

    # ──────────────── 启动流程 ────────────────

    async def _async_run(self):
        """异步主运行函数"""
        # 在事件循环中初始化异步对象
        self._ensure_async_objects()

        self.logger.info("=" * 60)
        self.logger.info("OAS守护进程启动")
        self.logger.info(f"API地址: {self.api_url}")
        self.logger.info(f"监控实例: {list(self.instance_configs.keys())}")
        self.logger.info(f"监控间隔: {self.monitor_interval}秒")
        if self.napcat_manager:
            self.logger.info(f"NapCat守护: 已启用 ({self.napcat_manager.config.endpoint})")
        else:
            self.logger.info("NapCat守护: 未启用")
        self.logger.info("=" * 60)

        # 1. 启动OAS Server
        if self.server_auto_start:
            if not self._start_oas_server():
                self.logger.error("OAS Server启动失败，守护进程退出")
                return
        else:
            self.logger.info("等待OAS Server上线...")
            for _ in range(30):
                if self._is_server_online():
                    self.logger.info("OAS Server已上线")
                    break
                await asyncio.sleep(2)
            else:
                self.logger.error("等待OAS Server超时，守护进程退出")
                return

        # 2. 启动所有启用的实例
        for name, ic in self.instance_configs.items():
            if not ic.enabled:
                self.logger.info(f"实例 {name} 已禁用，跳过")
                continue
            self.logger.info(f"启动实例: {name}")
            success = await self._ws_start_instance(name)
            if not success:
                self.logger.error(f"实例 {name} 启动失败")
            else:
                # 首次 start 后保留一个完整冷却周期给子进程握手；旧逻辑只等 2 秒便把
                # INACTIVE 当故障重启，反而会中断仍在初始化的实例。
                self.instance_last_restart[name] = time.time()
                self.logger.info(
                    f"实例 {name} 首次启动进入 {ic.restart_cooldown} 秒初始化宽限期")
            await asyncio.sleep(2)

        # 3. 启动NapCat（如果启用）
        if self.napcat_manager:
            self.logger.info("正在启动NapCat...")
            if not self.napcat_manager.start():
                self.logger.error("NapCat启动失败")
            await asyncio.sleep(3)

        # 4. 进入监控循环
        await self._monitor_loop()

    def _run_async_loop(self):
        """在独立线程中运行异步事件循环"""
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        try:
            self._loop.run_until_complete(self._async_run())
        except KeyboardInterrupt:
            pass
        finally:
            try:
                self._loop.run_until_complete(self._close_all_websockets())
            except Exception:
                pass
            self._loop.close()
            self.logger.info("监控线程事件循环已关闭")

    def run(self):
        """运行守护进程（主入口）"""
        self.running = True

        async_thread = threading.Thread(target=self._run_async_loop, daemon=True)
        async_thread.start()

        try:
            while self.running:
                schedule.run_pending()
                time.sleep(1)
        except KeyboardInterrupt:
            self.logger.info("收到中断信号")
        finally:
            self.stop()

    def stop(self):
        """停止守护进程"""
        self.running = False
        self.logger.info("正在停止OAS守护进程...")

        for name in self.instance_configs:
            try:
                self._api_stop_instance(name)
            except Exception:
                pass

        # 停止NapCat
        if self.napcat_manager:
            self.napcat_manager.stop()

        self._stop_oas_server()
        self.logger.info("OAS守护进程已停止")


# ──────────────────────── 入口 ────────────────────────

def _is_admin() -> bool:
    """检测当前进程是否以管理员权限运行"""
    try:
        return ctypes.windll.shell32.IsUserAnAdmin() != 0
    except Exception:
        return False


def _run_via_scheduled_task() -> bool:
    """通过已注册的计划任务（最高权限）启动自身，无需UAC确认"""
    try:
        flags = subprocess.CREATE_NO_WINDOW if sys.platform.startswith('win') else 0
        result = subprocess.run(
            ['schtasks', '/run', '/tn', 'OASDaemon'],
            capture_output=True, timeout=10, creationflags=flags
        )
        return result.returncode == 0
    except Exception:
        return False


def _configure_scheduled_task_recovery() -> bool:
    """校正计划任务：增加心跳触发器，并保留任务级失败重启。"""
    if not sys.platform.startswith('win'):
        return False

    helper = PROJECT_ROOT / 'dev_tools' / 'reboot' / 'ensure_daemon_task.ps1'
    if not helper.exists():
        return False
    try:
        result = subprocess.run(
            ['powershell', '-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass',
             '-File', str(helper), '-TaskName', 'OASDaemon'],
            capture_output=True,
            timeout=15,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        return result.returncode == 0
    except Exception:
        return False


def _register_scheduled_task_and_run():
    """注册计划任务（最高权限 + 登录时启动 + 失败恢复）并启动。"""
    try:
        script = os.path.abspath(sys.argv[0])
        # 直接用pythonw启动，不经过cmd，避免弹出cmd窗口
        pythonw = sys.executable.replace('python.exe', 'pythonw.exe')
        cmd = f'"{pythonw}" "{script}"'

        # 创建计划任务：最高权限、登录时启动
        flags = subprocess.CREATE_NO_WINDOW if sys.platform.startswith('win') else 0
        result = subprocess.run(
            ['schtasks', '/create', '/tn', 'OASDaemon', '/tr', cmd,
             '/sc', 'onlogon', '/rl', 'highest', '/f'],
            capture_output=True, timeout=10, creationflags=flags
        )
        if result.returncode == 0:
            if not _configure_scheduled_task_recovery():
                print("警告: OASDaemon 失败重启设置写入失败")
            # 通过计划任务启动（不弹UAC）
            time.sleep(1)
            if _run_via_scheduled_task():
                sys.exit(0)

        # 计划任务创建失败，回退到UAC提权
        params = ' '.join([f'"{arg}"' if ' ' in arg else arg for arg in sys.argv[1:]])
        # 使用pythonw + SW_HIDE避免控制台窗口
        pythonw_fallback = sys.executable.replace('python.exe', 'pythonw.exe')
        ctypes.windll.shell32.ShellExecuteW(
            None, "runas", pythonw_fallback, f'"{script}" {params}', None, 0)  # 0 = SW_HIDE
        sys.exit(0)
    except Exception as e:
        print(f"提权失败: {e}")
        sys.exit(1)


def main():
    # 最先设置UTF-8编码环境，确保后续所有IO都使用UTF-8
    if sys.platform.startswith('win'):
        os.environ.setdefault('PYTHONIOENCODING', 'utf-8')
        os.environ.setdefault('PYTHONUTF8', '1')

    # Windows下检测管理员权限，非管理员则自动提权
    if sys.platform.startswith('win') and not _is_admin():
        # 优先通过计划任务启动（不弹UAC）
        if _run_via_scheduled_task():
            sys.exit(0)
        # 计划任务不存在，创建它并通过它启动（仅首次需要UAC）
        _register_scheduled_task_and_run()

    # 守护每次以最高权限启动时都校正自身计划任务，旧安装也能自动获得恢复设置
    if sys.platform.startswith('win') and _is_admin():
        _configure_scheduled_task_recovery()

    # Windows控制台UTF-8支持
    if sys.platform.startswith('win'):
        try:
            if sys.stdout:
                sys.stdout.reconfigure(encoding='utf-8', errors='replace')
            if sys.stderr:
                sys.stderr.reconfigure(encoding='utf-8', errors='replace')
        except Exception:
            pass

    import argparse

    parser = argparse.ArgumentParser(description='OAS守护进程')
    parser.add_argument('--config-file', type=str,
                        default=str(Path(__file__).parent / 'daemon_config.json'),
                        help='配置文件路径 (默认: 同目录下daemon_config.json)')
    parser.add_argument('--api-host', type=str, default=None,
                        help='覆盖配置文件中的OAS Server地址')
    parser.add_argument('--api-port', type=int, default=None,
                        help='覆盖配置文件中的OAS Server端口')
    parser.add_argument('--reboot-time', type=str,
                        help='自动重启时间 (HH:MM格式)，覆盖配置文件')
    parser.add_argument('--reboot-weekday', type=int, choices=range(0, 7),
                        help='自动重启的星期几 (0:周一, 6:周日)')
    parser.add_argument('--napcat-start', action='store_true',
                        help='手动启动NapCat（需配置napcat段）')
    parser.add_argument('--napcat-stop', action='store_true',
                        help='手动停止NapCat')
    parser.add_argument('--napcat-restart', action='store_true',
                        help='手动重启NapCat')
    parser.add_argument('--napcat-status', action='store_true',
                        help='查看NapCat状态')

    args = parser.parse_args()

    config_path = Path(args.config_file)
    if not config_path.is_absolute():
        config_path = Path.cwd() / config_path

    daemon = RebootDaemon(config_path=str(config_path))

    # 命令行参数覆盖配置文件
    if args.api_host:
        daemon.api_host = args.api_host
        daemon.api_url = f"http://{daemon.api_host}:{daemon.api_port}"
    if args.api_port:
        daemon.api_port = args.api_port
        daemon.api_url = f"http://{daemon.api_host}:{daemon.api_port}"

    if args.reboot_time:
        daemon.reboot_time = args.reboot_time
        daemon._setup_reboot_schedule()
    if args.reboot_weekday is not None:
        daemon.reboot_weekday = args.reboot_weekday
        daemon._setup_reboot_schedule()

    # NapCat手动操作（不进入守护循环）
    if args.napcat_start or args.napcat_stop or args.napcat_restart or args.napcat_status:
        if not daemon.napcat_manager:
            # 即使配置中未启用，也允许手动操作（临时创建）
            napcat_raw = {}
            if daemon.config_path.exists():
                try:
                    with open(daemon.config_path, 'r', encoding='utf-8') as f:
                        napcat_raw = json.load(f).get('napcat', {})
                except Exception:
                    pass
            if napcat_raw:
                daemon._init_napcat_manager({'napcat': napcat_raw})
            if not daemon.napcat_manager:
                print("错误: 未找到NapCat配置，请在daemon_config.json中配置napcat段")
                sys.exit(1)

        if args.napcat_status:
            healthy = daemon.napcat_manager.health_check()
            print(f"NapCat状态: {'运行中 (API正常)' if healthy else '未运行或API异常'}")
            print(f"  endpoint: {daemon.napcat_manager.config.endpoint}")
            print(f"  napcat_dir: {daemon.napcat_manager.config.napcat_dir}")
            sys.exit(0)

        if args.napcat_start:
            success = daemon.napcat_manager.start()
            print(f"NapCat启动: {'成功' if success else '失败'}")
            sys.exit(0 if success else 1)

        if args.napcat_stop:
            daemon.napcat_manager.stop()
            print("NapCat已停止")
            sys.exit(0)

        if args.napcat_restart:
            success = daemon.napcat_manager.restart()
            print(f"NapCat重启: {'成功' if success else '失败'}")
            sys.exit(0 if success else 1)

    try:
        daemon.run()
    except KeyboardInterrupt:
        print("\n收到中断信号，正在停止守护进程...")
        daemon.stop()


if __name__ == '__main__':
    main()
