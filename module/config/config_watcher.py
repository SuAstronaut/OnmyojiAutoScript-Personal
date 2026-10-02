# This Python file uses the following encoding: utf-8
# @author runhey
# github https://github.com/runhey
# 监视特定配置文件修改的功能（Task 4 改为基于 mtime_ns，避免秒级截断漏检）。
import hashlib
import os

from module.logger import logger


class ConfigWatcher:
    """
    监视特定配置文件修改的功能。
    它以 session 当前 mtime_ns 为基线，提供 should_reload 检测磁盘是否有更新的写入。
    基线由每次成功提交推进；wait_until 入口的 start_watching
    只保留会话已提交 mtime，不吸收尚未加载的磁盘版本。
    """
    config_name = 'script'
    _watch_mtime_ns: int = 0
    _watch_content_digest: str = ""

    def start_watching(self) -> None:
        # 基线已由成功提交同步；wait 入口不采样磁盘，以免吸收未加载版本。
        return

    def _disk_mtime_ns(self) -> int:
        """读取当前磁盘配置文件的 mtime_ns；无锁、不解析内容，文件缺失视为 0。"""
        try:
            path = self.store.generation._config_path(self.config_name)
            return path.stat().st_mtime_ns if path.exists() else 0
        except OSError:
            return 0

    def _disk_content_digest(self) -> str | None:
        """返回当前配置文件 SHA-256；文件缺失或读取失败返回 None。"""
        try:
            path = self.store.generation._config_path(self.config_name)
            return hashlib.sha256(path.read_bytes()).hexdigest()
        except OSError:
            return None

    def get_mtime_ns(self) -> int:
        return self._disk_mtime_ns()

    def should_reload(self) -> bool:
        """mtime 前进/回退、文件缺失或同 mtime 内容变化均需刷新。"""
        mtime = self._disk_mtime_ns()
        digest = self._disk_content_digest()
        if mtime != self._watch_mtime_ns or digest != self._watch_content_digest:
            logger.info(
                f'Config "{self.config_name}" changed '
                f'(mtime_ns={mtime}, exists={digest is not None})'
            )
            return True
        return False
