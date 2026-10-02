# This Python file uses the following encoding: utf-8
# @author runhey
# github https://github.com/runhey
from pydantic import BaseModel, Field

from tasks.Component.config_scheduler import Scheduler
from tasks.Component.config_base import ConfigBase, Time
from tasks.Component.SwitchSoul.switch_soul_config import SwitchSoulConfig


class SixRealmsGate(BaseModel):
    number_enable: bool = Field(default=False, description='只打门票')
    # 限制时间
    limit_time: Time = Field(default=Time(minute=30), description='limit_time_help')
    # 限制次数
    limit_count: int = Field(default=1, description='limit_count_help')
    # 力量强化目标等级
    power_enhance_level: int = Field(default=4, title='力量强化等级', description='力量强化目标等级，默认4。未达到该等级时优先进入战之岛刷力量强化，达到后优先神秘岛')


class SixRealms(ConfigBase):
    scheduler: Scheduler = Field(default_factory=Scheduler)
    switch_soul_config: SwitchSoulConfig = Field(default_factory=SwitchSoulConfig)
    six_realms_gate: SixRealmsGate = Field(default_factory=SixRealmsGate)













