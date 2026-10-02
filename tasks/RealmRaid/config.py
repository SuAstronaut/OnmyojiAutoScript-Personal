# This Python file uses the following encoding: utf-8
# @author runhey
# github https://github.com/runhey
from pydantic import BaseModel, Field
from enum import Enum

from tasks.Component.config_scheduler import Scheduler
from tasks.Component.GeneralBattle.config_general_battle import GeneralBattleConfig
from tasks.Component.config_base import ConfigBase
from tasks.Component.SwitchSoul.switch_soul_config import SwitchSoulConfig

class RaidMode(str, Enum):
    NORMAL = 'retreat_four_attack_nine'
    ATTACK_ALL = 'attack_all'

class AttackNumber(str, Enum):
    NINE = 'nine'
    ALL = 'all'

class WhenAttackFail(str, Enum):
    EXIT: str = 'Exit'
    CONTINUE: str = 'Continue'
    REFRESH: str = 'Refresh'

class RaidConfig(BaseModel):
    # raid_mode: RaidMode = Field(title='Raid Mode', default=RaidMode.NORMAL,
    #                             description='raid_mode_help')
    # attack_number: AttackNumber = Field(title='Attack Number', default=AttackNumber.ALL,
    #                                     description='')

    number_attack: int = Field(title='Number Attack', default=30, le=30, ge=1, description='number_attack_help')
    number_base: int = Field(title='Number Base', default=0, le=20, ge=0, description='number_base_help')
    # 针对右下角第九格（难度最高）退四；开启 auto_exit_all 且平均等级偏低、勋章不足时会被运行期临时关闭
    exit_four: bool = Field(title='Exit Four', default=True, description='exit_four_help')
    # 填 position 则按格号顺序进攻（格 1 → 格 9），否则按勋章数（星级）优先级
    order_attack: str = Field(title='Order Attack', default='5 > 4 > 3 > 2 > 1 > 0', description='order_attack_help')
    three_refresh: bool = Field(title='Three Refresh', default=False, description='three_refresh_help')
    when_attack_fail: WhenAttackFail = Field(title='WhenAttackFail', default=WhenAttackFail.REFRESH, description='when_attack_fail_help')
    # 九格全为正常状态时按平均等级与总勋章数自动触发全退（退9）降低结界等级；
    # 该选项同时是等级识别的总开关，没开时完全不识别等级、退四只按 exit_four 配置执行
    auto_exit_all: bool = Field(title='Auto Exit All', default=False, description='auto_exit_all_help')

class RealmRaid(ConfigBase):
    scheduler: Scheduler = Field(default_factory=Scheduler)
    raid_config: RaidConfig = Field(default_factory=RaidConfig)
    general_battle_config: GeneralBattleConfig = Field(default_factory=GeneralBattleConfig)
    switch_soul_config: SwitchSoulConfig = Field(default_factory=SwitchSoulConfig)







