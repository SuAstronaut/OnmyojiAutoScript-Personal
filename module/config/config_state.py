# This Python file uses the following encoding: utf-8
# @author runhey
# github https://github.com/runhey
from dataclasses import dataclass, field


@dataclass
class ConfigStateResult:
    """refresh_from_disk 的返回结果：状态、pending 集合、mtime 与 generation mismatch 标志。"""
    status: str = "current"
    pending_restart_paths: list = field(default_factory=list)
    pending_warm_paths: list = field(default_factory=list)
    mtime_ns: int = 0
    generation_mismatch: bool = False


class ConfigState:
    """
    这个类用于 先定义运行过程中所需要的变量
    """
    def __init__(self, config_name: str) -> None:
        self.config_name = config_name
        self.pending_task: list["Function"] = []
        self.waiting_task: list["Function"] = []
        self.task: str = None  # 任务名大驼峰
        # WARM/COLD pending 状态（Task 4）：路径为 canonical tuple。
        self._pending_restart_paths: set = set()
        self._pending_warm_paths: set = set()
        # generation mismatch 后终止 session 持久化并请求实例停止（规格 §10.3）
        self._generation_mismatch: bool = False
