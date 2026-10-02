# This Python file uses the following encoding: utf-8
# @author runhey
# github https://github.com/runhey
from pydantic import BaseModel, Field
from tasks.Component.config_scheduler import Scheduler
from tasks.Component.config_base import ConfigBase, Time


class ReturnGiftConfig(BaseModel):
    return_gift_timeout: Time = Field(default=Time(hour=0, minute=40, second=0), description='回礼限制运行时间')
    return_gift_mode: str = Field(default='gift', description='回礼运行模式：gift=送礼，sr_count=统计SR碎片')


class ReturnGift(ConfigBase):
    scheduler: Scheduler = Field(default_factory=Scheduler)
    return_gift_config: ReturnGiftConfig = Field(default_factory=ReturnGiftConfig)

