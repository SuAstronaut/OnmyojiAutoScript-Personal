from deploy.patch import pre_checks

pre_checks()

from deploy.adb import AdbManager
from deploy.process import ProcessManager
from deploy.config import ExecutionError
from deploy.git import GitManager
from deploy.logger import logger
from deploy.ocr_deps import OcrDepsManager
from deploy.pip import PipManager


class Installer(GitManager, PipManager, OcrDepsManager, AdbManager, ProcessManager):
    def install(self):
        try:
            # 先确保内置 toolkit git 可用（不可用则下载完整版替换），再 git_install 拉代码
            self.ensure_git_ready()
            self.git_install()
            # 未确认安装目录内进程已退出时禁止继续 pip 换包，避免 DLL 仍被占用。
            if not self.process_kill():
                raise ExecutionError('无法确认 OAS 进程已退出，停止安装以保护现有环境')
            self.pip_install()
            # OCR 依赖必须在 process_kill 之后对齐：Windows 会锁定已加载的
            # onnxruntime.dll，有推理进程存活时换包会留下损坏的 distribution
            self.ocr_install()
            self.adb_install()
        except ExecutionError:
            exit(1)

    def ocr_install(self):
        """对齐 PP-OCRv6 依赖与模型，失败不阻断启动（OCR 会在运行时报错提示）。"""
        ok, info = self.align()
        if not ok:
            logger.warning(f'OCR dependencies not aligned: {info}')


if __name__ == '__main__':
    Installer().install()
