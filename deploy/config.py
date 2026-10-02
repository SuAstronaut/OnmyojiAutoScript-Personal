# This Python file uses the following encoding: utf-8
# copy from alas https://github.com/LmeSzinc/AzurLaneAutoScript
import copy
import subprocess
import sys
from typing import Optional, Union

from deploy.logger import logger
from deploy.utils import *


class ExecutionError(Exception):
    pass


class ConfigModel:
    # Git
    Repository: str = "https://gitcode.com/OnmyojiAutoScript/OnmyojiAutoScript.git"
    Branch: str = "master"
    GitExecutable: str = "./toolkit/Git/mingw64/bin/git.exe"
    GitProxy: Optional[str] = None
    SSLVerify: bool = False
    AutoUpdate: bool = True
    KeepLocalChanges: bool = False

    # Python
    PythonExecutable: str = "./toolkit/python.exe"
    PypiMirror: Optional[str] = None
    InstallDependencies: bool = True
    RequirementsFile: str = "requirements.txt"

    # Adb
    AdbExecutable: str = "./toolkit/Lib/site-packages/adbutils/binaries/adb.exe"
    ReplaceAdb: bool = True
    AutoConnect: bool = True
    InstallUiautomator2: bool = True

    # Ocr
    # 脚本进程是否共用 OCR RPC 服务：多开时共享一份模型能显著省内存，
    # 服务不可用时自动降级本地模型，不会让任务崩掉
    UseOcrServer: bool = True
    # 启动时是否托管 OCR RPC 服务，配合 UseOcrServer 一起开才有省内存效果
    StartOcrServer: bool = True
    OcrServerPort: int = 22268
    OcrClientAddress: str = "127.0.0.1:22268"
    # PP-OCRv6 推理设备：auto 先探 DirectML 再退 CPU，dml 强制 GPU，cpu 强制 CPU
    OcrDevice: str = "auto"
    # PP-OCRv6 模型档位：small 通用，medium 精度更高但仅在 GPU 下启用
    OcrModelType: str = "small"
    # v6 模型存放目录，必须在项目内，避免写到用户级缓存
    OcrModelDir: str = "./toolkit/ocr_models"
    # CPU 推理线程数，实测 4 是速度与占用的平衡点，多开时更稳定
    OcrCpuThreads: int = 4
    # 更新/安装阶段是否自动对齐 OCR 依赖与模型（一键更新的开关）
    OcrAutoAlignDeps: bool = True

    # Update
    EnableReload: bool = True
    CheckUpdateInterval: int = 5
    AutoRestartTime: str = "03:50"

    # Misc
    DiscordRichPresence: bool = False

    # Remote Access
    EnableRemoteAccess: bool = False
    SSHUser: Optional[str] = None
    SSHServer: Optional[str] = None
    SSHExecutable: Optional[str] = None

    # Webui
    WebuiHost: str = "0.0.0.0"
    WebuiPort: int = 22267
    Language: str = "en-US"
    Theme: str = "default"
    DpiScaling: bool = True
    Password: Optional[str] = None
    CDN: Union[str, bool] = False
    Run: Optional[str] = None


class DeployConfig(ConfigModel):
    def __init__(self, file=DEPLOY_CONFIG):
        """
        Args:
            file (str): User deploy config.
        """
        self.file = file
        self.config = {}
        self.read()
        self.write()
        self.show_config()

    def show_config(self):
        logger.hr("Show deploy config", 1)
        for k, v in self.config.items():
            if k in ("Password", "SSHUser"):
                continue
            if self.config_template[k] == v:
                continue
            logger.info(f"{k}: {v}")

        logger.info(f"Rest of the configs are the same as default")

    def read(self):
        self.config_template = poor_yaml_read(DEPLOY_TEMPLATE)
        self.config = copy.deepcopy(self.config_template)
        self.config.update(poor_yaml_read(self.file))

        # https://e.coding.net/onmyojiautoscript/oas/OnmyojiAutoScript.git
        # 2025.09.01 腾讯coding跑路了
        if self.config["Repository"].startswith("https://e.coding.net/"):
            self.config["Repository"] = "https://gitcode.com/OnmyojiAutoScript/OnmyojiAutoScript.git"

        for key, value in self.config.items():
            if hasattr(self, key):
                super().__setattr__(key, value)

    def write(self):
        poor_yaml_write(self.config, self.file)

    def filepath(self, key):
        """
        Args:
            key (str):

        Returns:
            str: Absolute filepath.
        """
        return (
            os.path.abspath(os.path.join(self.root_filepath, self.config[key]))
            .replace(r"\\", "/")
            .replace("\\", "/")
            .replace('"', '"')
        )

    @cached_property
    def root_filepath(self):
        return (
            os.path.abspath(os.path.join(os.path.dirname(__file__), "../"))
            .replace(r"\\", "/")
            .replace("\\", "/")
            .replace('"', '"')
        )

    def execute(self, command, allow_failure=False, output=True, timeout=None):
        """
        Args:
            command (str):
            allow_failure (bool):
            output(bool):
            timeout (int | None): 进程最长执行秒数，超时 kill 并视为失败；None 表示不限制。

        Returns:
            bool: If success.
                Terminate installation if failed to execute and not allow_failure.
        """
        command = command.replace(r"\\", "/").replace("\\", "/").replace('"', '"')
        if not output:
            command = command + ' >nul 2>nul'
        logger.info(command)
        # GUI(pythonw) 无控制台时 os.system 会为子进程新建 CMD 窗口，改用 CREATE_NO_WINDOW 静默执行
        flags = subprocess.CREATE_NO_WINDOW if sys.platform.startswith('win') else 0
        proc = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            encoding='utf-8',
            errors='replace',
            shell=True,
            creationflags=flags,
        )
        try:
            # 捕获子进程输出：失败时回显尾部，pip/git 的真实报错不再被吞
            out, _ = proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            # 超时视为失败。Windows 下 kill cmd 不会带走它派生的 git/ping 子进程，
            # 残留子进程仍持有 stdout 管道会让 communicate 继续阻塞，必须按进程树杀
            logger.info(f"[ timeout ]: {command[:80]}...")
            out = ''
            if sys.platform.startswith('win'):
                subprocess.Popen(
                    f'taskkill /F /T /PID {proc.pid}',
                    shell=True,
                    creationflags=flags,
                ).wait()
            else:
                proc.kill()
            proc.communicate()
            error_code = -1
        else:
            error_code = proc.returncode
        if error_code:
            # 失败时回显子进程输出尾部，让 pip/git 的真实报错可见。
            # pip/git 输出可能含控制台编码（如 GBK）打不出的字符，先按输出流
            # 编码净化成可打印字符，否则 logger 写日志时自己抛 UnicodeEncodeError
            if out:
                enc = (getattr(sys.stdout, 'encoding', None) or 'utf-8')
                tail = '\n'.join(out.strip().splitlines()[-10:])
                tail = tail.encode(enc, errors='replace').decode(enc, errors='replace')
                logger.info(f'[ output tail ]\n{tail}')
            if allow_failure:
                logger.info(f"[ allowed failure ], error_code: {error_code}")
                return False
            else:
                logger.info(f"[ failure ], error_code: {error_code}")
                self.show_error(command)
                raise ExecutionError
        else:
            logger.info(f"[ success ]")
            return True

    def execute_output(self, command) -> str:
        """静默执行命令并返回完整输出；失败或超时返回空串。

        与 execute 一样用 CREATE_NO_WINDOW，GUI 无控制台时不弹 CMD 窗口。
        """
        command = command.replace(r"\\", "/").replace("\\", "/").replace('"', '"')
        flags = subprocess.CREATE_NO_WINDOW if sys.platform.startswith('win') else 0
        proc = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            encoding='utf-8',
            errors='replace',
            shell=True,
            creationflags=flags,
        )
        try:
            out, _ = proc.communicate(timeout=30)
        except subprocess.TimeoutExpired:
            # 超时按进程树杀，防止命令挂起（如 git 连不上远程）
            if sys.platform.startswith('win'):
                subprocess.Popen(
                    f'taskkill /F /T /PID {proc.pid}',
                    shell=True,
                    creationflags=flags,
                ).wait()
            else:
                proc.kill()
            proc.communicate()
            return ''
        return out or ''

    def show_error(self, command=None):
        logger.hr("Update failed", 0)
        self.show_config()
        logger.info("")
        logger.info(f"Last command: {command}")
        logger.info(
            "Please check your deploy settings in config/deploy.yaml "
            "and re-open Alas.exe"
        )
        logger.info("Take the screenshot of entire window if you need help")

