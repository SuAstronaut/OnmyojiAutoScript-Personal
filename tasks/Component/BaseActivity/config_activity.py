# This Python file uses the following encoding: utf-8
# @author runhey
# github https://github.com/runhey
from datetime import time, timedelta
from pydantic import BaseModel, Field, validator, field_validator, model_validator
from enum import Enum

from module.logger import logger

from tasks.Component.config_base import ConfigBase, TimeDelta, Time, dynamic_hide
from typing import Optional


class ApMode(str, Enum):
    AP_ACTIVITY = 'ap_activity'
    AP_GAME = 'ap_game'


class GeneralClimb(ConfigBase):
    limit_time: Time = Field(default=Time(minute=30), description='限制爬塔运行总时间')
    # boss_limit_time: Time = Field(default=Time(minute=5), description='boss单局限制时间')
    pass_limit: int = Field(default=50, description='门票爬塔的最大次数')
    ap_limit: int = Field(default=300, description='体力爬塔的最大次数')
    boss_limit: int = Field(default=20, description='boss战爬塔的最大次数')
    ap20_limit: int = Field(default=140, description='ap20爬塔的最大次数(7入口×20次)')
    pass_monopoly_limit: int = Field(default=50, description='大富翁模式爬塔的最大次数')
    season_boss_limit: int = Field(default=0, description='修行合训(season_boss)玩法的最大搜寻次数, 0表示不启用(不写进run_sequence即休眠)')
    # 默认序列只含当前活动存在的三种入口（ap100 已删除；ap20/大富翁入口当前活动
    # 不存在，需要时自行加入 run_sequence）
    run_sequence: str = Field(default='pass,boss,ap',
                              description='运行爬塔顺序(pass:门票,boss:boss战,ap:体力,ap20:20体,pass_monopoly:大富翁)\n英文逗号分隔,从左到右运行,可自定义执行顺序\n例如:pass,boss,ap就是门票->boss战->体力')
    # # 门票爬塔buff
    # pass_buff: str = Field(default='buff_4,buff_5', description='门票爬塔加成,buff1-5,加成页从左往右顺序,清空则不切换加成')
    # # 体力爬塔buff
    # ap_buff: str = Field(default='buff_4,buff_5', description='体力爬塔加成,buff1-5,加成页从左往右顺序,清空则不切换加成')
    # # boss爬塔buff
    # boss_buff: str = Field(default='buff_1,buff_3', description='boss战爬塔加成,buff1-5,加成页从左往右顺序,清空则不切换加成')
    # 结束后激活 御魂清理
    active_souls_clean: bool = Field(default=False, description='是否运行结束后清理御魂')
    # 点击战斗随机休息
    random_sleep: bool = Field(default=False, description='是否启用在点击战斗前随机休息')
    # 是否开启五倍消耗（仅 pass/ap 生效，五倍券由脚本读屏并共用于 pass/ap）
    five_times_enable: bool = Field(default=False, description='five_times_enable_help')

    # ap20/大富翁/修行合训 当前活动不存在对应入口，次数上限在前端隐藏
    # （功能与字段保留，活动切换回来后去掉本行即可恢复显示）
    hide_fields = dynamic_hide('ap20_limit', 'pass_monopoly_limit', 'season_boss_limit')

    @property
    def limit_time_v(self) -> timedelta:
        if isinstance(self.limit_time, time):
            return timedelta(hours=self.limit_time.hour, minutes=self.limit_time.minute,
                             seconds=self.limit_time.second)
        return self.limit_time

    @property
    def run_sequence_v(self) -> list[str]:
        self.valid_run_sequence()
        return [climb_type.strip() for climb_type in self.run_sequence.split(',')]

    def get_single_limit_time(self, climb_type: str, default: timedelta = None) -> Optional[timedelta]:
        """
        获取单局限制时间
        :param climb_type: 爬塔类型
        :param default: 默认限制时间
        :return: 单局限制时间 or default
        """
        if not climb_type:
            return default
        if getattr(self, f'{climb_type}_limit_time', None) is not None:
            limit_time = getattr(self, f'{climb_type}_limit_time')
            if isinstance(limit_time, time):
                return timedelta(hours=limit_time.hour, minutes=limit_time.minute,
                                 seconds=limit_time.second)
            return limit_time
        return default

    # @model_validator(mode='after')
    def valid_run_sequence(self):
        if not self.run_sequence or not self.run_sequence.strip():
            raise ValueError('run sequence cannot be empty')
        sequence_list = [climb_type.strip() for climb_type in self.run_sequence.split(',')]
        if not sequence_list or len(sequence_list) < 1:
            raise ValueError('run sequence cannot be empty')
        label_set = {field.replace('_limit', '') for field in self.model_fields if field.endswith('_limit')}
        for climb_type in sequence_list:
            if climb_type not in label_set:
                raise ValueError(f'run sequence can only be one of {", ".join(label_set)}, now is {climb_type}')
        return self

    @validator('limit_time', pre=True, always=True)
    def parse_limit_time(cls, value):
        if isinstance(value, str):
            if value.isdigit():
                try:
                    value = int(value)
                except ValueError:
                    logger.warning('Invalid limit_time value. Expected format: seconds')
                    return time(hour=0, minute=30, second=0)
                delta = timedelta(seconds=value)
                return time(hour=delta.seconds // 3600, minute=delta.seconds // 60 % 60, second=delta.seconds % 60)
            else:
                try:
                    return time.fromisoformat(value)
                except ValueError:
                    logger.warning('Invalid limit_time value. Expected format: HH:MM:SS')
                    return time(hour=0, minute=30, second=0)
        return value
