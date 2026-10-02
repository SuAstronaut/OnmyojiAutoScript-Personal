# This Python file uses the following encoding: utf-8
# copy from alas https://github.com/LmeSzinc/AzurLaneAutoScript
import os
import re
import subprocess
import time

from deploy.config import DeployConfig
from deploy.logger import logger
from deploy.utils import *


class ProcessManager(DeployConfig):
    @cached_property
    def process_folder(self):
        # 只允许结束当前安装根目录下的进程，比较时必须带目录边界，
        # 避免 C:/OAS 误匹配 C:/OAS-backup。
        return [os.path.normcase(os.path.abspath(self.root_filepath))]

    @staticmethod
    def _path_under(path, root):
        path = os.path.normcase(os.path.abspath(path.replace('/', os.sep)))
        root = os.path.normcase(os.path.abspath(root.replace('/', os.sep)))
        return path == root or path.startswith(root + os.sep)

    @staticmethod
    def _wait_process_exit(pid, timeout=5.0):
        # 引导层允许在 psutil 尚未安装时先启动安装器；优先按需导入，
        # 缺少第三方依赖时退回 Windows 自带 tasklist，不让 bootstrap 直接崩溃。
        try:
            import psutil
        except ModuleNotFoundError:
            return ProcessManager._wait_process_exit_with_tasklist(pid, timeout)

        # taskkill 返回成功只代表已发出终止请求，必须等待进程真正退出后再换 DLL。
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if not psutil.pid_exists(int(pid)):
                return True
            time.sleep(0.1)
        return not psutil.pid_exists(int(pid))

    @staticmethod
    def _wait_process_exit_with_tasklist(pid, timeout=5.0):
        """无 psutil 时用系统 tasklist 确认进程是否已退出。"""
        deadline = time.monotonic() + timeout
        pattern = re.compile(rf'^\S+\s+{int(pid)}\s', re.MULTILINE)
        while time.monotonic() < deadline:
            try:
                result = subprocess.run(
                    ['tasklist', '/FI', f'PID eq {int(pid)}', '/NH'],
                    capture_output=True,
                    text=True,
                    encoding='utf-8',
                    errors='replace',
                    creationflags=subprocess.CREATE_NO_WINDOW
                    if os.name == 'nt' else 0,
                )
            except OSError as e:
                logger.warning(f'无法使用 tasklist 确认进程 {pid}：{e}')
                return False
            if result.returncode != 0:
                logger.warning(f'tasklist 查询进程 {pid} 失败：{result.returncode}')
                return False
            if not pattern.search(result.stdout or ''):
                return True
            time.sleep(0.1)
        return False

    @cached_property
    def self_pid(self):
        return os.getpid()

    def iter_process_by_name(self, name):
        """
        Args:
            name (str): process name, such as 'alas.exe'

        Yields:
            str, str, str: executable_path, process_name, process_id
        """
        try:
            from win32com.client import GetObject
        except ModuleNotFoundError:
            logger.info('pywin32 not installed, skip')
            return False

        try:
            wmi = GetObject('winmgmts:')
            processes = wmi.InstancesOf('Win32_Process')
            for p in processes:
                executable_path = p.Properties_["ExecutablePath"].Value
                process_name = p.Properties_("Name").Value
                process_id = p.Properties_["ProcessID"].Value

                if executable_path is not None and process_name == name and process_id != self.self_pid:
                    executable_path = executable_path.replace(r'\\', '/').replace('\\', '/')
                    for folder in self.process_folder:
                        if self._path_under(executable_path, folder):
                            yield executable_path, process_name, process_id
        except Exception as e:
            # Possible exception
            # pywintypes.com_error: (-2147217392, 'OLE error 0x80041010', None, None)
            logger.info(str(e))
            return False

    def kill_by_name(self, name):
        """按已校验的安装目录筛选并等待目标进程退出。"""
        logger.hr(f'Kill {name}', 1)
        success = True
        for row in self.iter_process_by_name(name):
            logger.info(' '.join(map(str, row)))
            self.execute(f'taskkill /f /t /pid {row[2]}',
                         allow_failure=True, output=False)
            # taskkill 报错不必然代表失败：若枚举列表里父进程排在子进程之前，
            # 对父进程的 /t 树杀会连带终止子进程，随后对已死子进程 PID 的
            # taskkill 会报「找不到进程」。以进程是否真正退出为唯一判据。
            if not self._wait_process_exit(row[2]):
                logger.warning(f'Process {row[2]} did not exit after taskkill')
                success = False
        return success

    def process_kill(self):
        logger.hr(f'Kill existing Alas', 0)
        # 更新前必须能枚举进程；WMI 不可用时不能假定“没有进程”，否则 DLL
        # 可能仍被外部 OAS 实例占用，随后换包会损坏环境。
        try:
            from win32com.client import GetObject
            GetObject('winmgmts:')
        except Exception as e:
            logger.warning(f'无法枚举 OAS 进程，拒绝继续更新：{e}')
            return False
        return all([
            self.kill_by_name('oas.exe'),
            self.kill_by_name('python.exe'),
            self.kill_by_name('pythonw.exe'),
        ])


if __name__ == '__main__':
    ProcessManager().kill_by_name('pythonw')
