# This Python file uses the following encoding: utf-8
from pydantic import Field

from tasks.Component.config_base import ConfigBase, TimeDelta
from tasks.Component.config_scheduler import Scheduler


class ActivitySignInScheduler(Scheduler):
    # 单账号活动签到：每日一次，失败 2 小时后重试（沿用旧 MultiAccountSignIn 的节奏）
    priority: int = Field(default=5, description='priority_help')
    success_interval: TimeDelta = Field(default=TimeDelta(days=1), description='success_interval_help')
    failure_interval: TimeDelta = Field(default=TimeDelta(hours=2), description='failure_interval_help')


class ActivitySignInConfig(ConfigBase):
    """活动签到流程开关：可多选，按下方声明顺序依次执行。

    字段名的后半段与 script_task.py 的 FLOWS 表 key 一一对应，两边必须同步增删。
    """

    # 1 五个活动式神
    flow_shikigami: bool = Field(default=True, description='flow_shikigami_help')
    # 2 石长姬
    flow_iwanaga: bool = Field(default=True, description='flow_iwanaga_help')
    # 3 十连十金
    flow_summon_ten: bool = Field(default=True, description='flow_summon_ten_help')
    # 4 典藏
    flow_collection: bool = Field(default=True, description='flow_collection_help')
    # 5 唤新礼（资源待采集，勾选后运行时仅记日志跳过）
    flow_huanxin_gift: bool = Field(default=True, description='flow_huanxin_gift_help')
    # 6 晴明皮（资源待采集，勾选后运行时仅记日志跳过）
    flow_seimei_skin: bool = Field(default=True, description='flow_seimei_skin_help')


class ActivitySignIn(ConfigBase):
    scheduler: ActivitySignInScheduler = Field(default_factory=ActivitySignInScheduler)
    activity_sign_in_config: ActivitySignInConfig = Field(default_factory=ActivitySignInConfig)
