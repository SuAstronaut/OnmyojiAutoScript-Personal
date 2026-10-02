# This Python file uses the following encoding: utf-8
# @author runhey
# github https://github.com/runhey
from enum import Enum
from pydantic import BaseModel, Field
from tasks.Component.config_base import ConfigBase, TimeDelta
from tasks.Component.config_scheduler import Scheduler
from tasks.Utils.config_enum import ShikigamiClass


class CardType(str, Enum):
    FISH = '斗鱼'
    TAIKO = '太鼓'
    DAILY = '日常'
    

class ActivationScheduler(Scheduler):
    priority: int = Field(default=2, description='priority_help')
    success_interval: TimeDelta = Field(default=TimeDelta(days=1), description='success_interval_help')
    failure_interval: TimeDelta = Field(default=TimeDelta(hours=10), description='failure_interval_help')


class ActivationConfig(BaseModel):
    card_type: CardType = Field(default=CardType.TAIKO, description='card_rule_help')
    min_taiko_num: int = Field(default=1, description='挂卡太鼓每小时最少收益,低于则不挂卡')
    min_fish_num: int = Field(default=16, description='挂卡斗鱼每小时最少收益,低于则不挂卡')
    exchange_before: bool = Field(default=True, description='exchange_before_help')
    exchange_max: bool = Field(default=False, description='exchange_max_help')
    shikigami_class: ShikigamiClass = Field(default=ShikigamiClass.N, description='shikigami_class_help')
    card_not_found_count: int = Field(default=0, description='未发现卡次数')
    pets_enable: bool = Field(default=False, description='pets_enable_help')
    # 禁止运行时间段：命中时跳过本次运行并将下次时间设为区间结束时刻
    forbidden_time_enable: bool = Field(default=False, description='是否启用禁止运行时间段')
    forbidden_time_range: str = Field(default='', description='禁止运行的时间段，24小时制精确到分钟，多个用英文逗号分隔，如 01:00-02:00,02:30-04:00，支持跨天如 23:00-01:00')
    # 下次上号随机延时：正常挂卡完成后或禁止时间段解禁时，在原定下次运行时间上叠加随机分钟数，避免准点上线
    random_delay_enable: bool = Field(default=False, description='是否启用下次上号随机延时')
    random_delay_min: int = Field(default=10, description='随机延时下限，单位分钟')
    random_delay_max: int = Field(default=30, description='随机延时上限，单位分钟')
    # 连续失败上限：达到后本次直接把下次运行时间推到明天同一时刻，不再 5 分钟
    # 一次地重复失败；<=0 表示不启用上限（保持原有的 5 分钟重试）
    max_consecutive_failures: int = Field(default=3, description='连续失败上限，达到后推迟到明天同一时刻；0 表示不启用')

class KekkaiActivation(ConfigBase):
    scheduler: ActivationScheduler = Field(default_factory=ActivationScheduler)
    activation_config: ActivationConfig = Field(default_factory=ActivationConfig)
