# This Python file uses the following encoding: utf-8
"""Server 端日志接入工具。"""

import logging
from typing import Iterable

from module.logger import logger

UVICORN_LOGGER_NAMES = ("uvicorn", "uvicorn.error", "uvicorn.access")


class OasLogHandler(logging.Handler):
    """把第三方日志转发到 OAS 统一 logger。"""

    def emit(self, record: logging.LogRecord):
        """按原始等级写入 OAS logger，保持 server 日志格式一致。"""
        try:
            message = self.format(record)
            logger.log(record.levelno, message)
        except Exception:
            self.handleError(record)


def setup_server_logging(logger_names: Iterable[str] = UVICORN_LOGGER_NAMES):
    """接管 server 相关 logger，让 uvicorn 日志进入 OAS 日志文件。"""
    handler = OasLogHandler()
    handler.setFormatter(logging.Formatter("%(message)s"))

    for logger_name in logger_names:
        third_party_logger = logging.getLogger(logger_name)
        # 清空 uvicorn 默认 handler，避免控制台和文件里重复打印同一条日志。
        third_party_logger.handlers = [handler]
        third_party_logger.propagate = False
        third_party_logger.setLevel(logger.level)
