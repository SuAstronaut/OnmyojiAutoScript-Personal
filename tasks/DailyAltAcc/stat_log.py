# This Python file uses the following encoding: utf-8
import json
from enum import Enum
from typing import Any

from module.logger import logger


class StatEvent:
    """DailyAltAcc 结构化统计事件名。"""

    RUN_START = "run_start"
    RUN_END = "run_end"
    SWITCH_START = "switch_start"
    ACC_START = "acc_start"
    ACC_END = "acc_end"
    SWITCH = "switch"
    TASK_START = "task_start"
    TASK_END = "task_end"
    ERROR = "error"
    BATTLE = "battle"
    COOP = "coop"
    MSHOP = "mshop"


class StatLogMixin:
    """把统计事件以 [STAT] 单行 JSON 写入主日志。"""

    _stat_ctx: dict[str, Any] | None = None

    def emit_stat(self, ev: str, **fields: Any) -> None:
        """合并账号上下文并输出一行可解析的统计日志。"""
        payload: dict[str, Any] = {"ev": ev}
        if self._stat_ctx:
            payload.update(self._stat_ctx)
        payload.update(fields)
        logger.info(
            "[STAT] "
            + json.dumps(
                self._json_safe(payload),
                ensure_ascii=False,
                separators=(",", ":"),
            )
        )

    @classmethod
    def _json_safe(cls, value: Any) -> Any:
        """将 Enum 等对象转换成 JSON 可稳定序列化的基础类型。"""
        if isinstance(value, Enum):
            return value.name
        if isinstance(value, dict):
            return {str(k): cls._json_safe(v) for k, v in value.items()}
        if isinstance(value, (list, tuple)):
            return [cls._json_safe(item) for item in value]
        return value
