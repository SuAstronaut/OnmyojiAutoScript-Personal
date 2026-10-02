"""账号导出只配置运行时间，设备与保存路径复用现有 OAS 设置。"""
from pydantic import Field

from tasks.Component.config_base import ConfigBase, TimeDelta
from tasks.Component.config_scheduler import Scheduler


class AccountExportScheduler(Scheduler):
    """默认关闭，启用后每天导出一次，普通失败两小时后重试。"""
    priority: int = Field(default=5, description='priority_help')
    success_interval: TimeDelta = Field(default=TimeDelta(days=1), description='success_interval_help')
    failure_interval: TimeDelta = Field(default=TimeDelta(hours=2), description='failure_interval_help')


class AccountExport(ConfigBase):
    """独立导出任务的配置入口。"""
    scheduler: AccountExportScheduler = Field(default_factory=AccountExportScheduler)
