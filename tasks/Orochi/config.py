# This Python file uses the following encoding: utf-8
# @author runhey
# github https://github.com/runhey
from pydantic import BaseModel, Field, model_validator
from enum import Enum
from datetime import datetime, time

from tasks.Component.SwitchSoul.switch_soul_config import SwitchSoulConfig as BaseSwitchSoulConfig
from tasks.Component.config_scheduler import Scheduler
from tasks.Component.config_base import ConfigBase, Time
from tasks.Component.GeneralInvite.config_invite import InviteConfig
from tasks.Component.GeneralBattle.config_general_battle import GeneralBattleConfig


class UserStatus(str, Enum):
    LEADER = 'leader'
    MEMBER = 'member'
    ALONE = 'alone'
    WILD = 'wild'  # 还不打算实现


class TeamMode(str, Enum):
    ALONE = 'alone'
    TEAM = 'team'


class Layer(str, Enum):
    ONE = '壹层'
    TWO = '贰层'
    THREE = '叁层'
    FOUR = '肆层'
    FIVE = '伍层'
    SIX = '陆层'
    SEVEN = '柒层'
    EIGHT = '捌层'
    NINE = '玖层'
    TEN = '拾层'
    ELEVEN = '悲鸣'
    TWELVE = '神罚'
    THIRTEEN = '虚无'


class OrochiTeamConfig(ConfigBase):
    """组队御魂的独立配置，显示在副本设置之前。"""

    # 组队/单人选项：选择组队后进入脚本组队流程并使用组队配置
    team_mode: TeamMode = Field(default=TeamMode.ALONE, description='team_mode_help')
    # 队员通过运行中的实例列表选择队长；队长身份下可以留空
    leader_instance: str = Field(default='', description='leader_instance_help')
    # 场次 Epoch 由脚本自动回写，输入固定值 RESET 可丢弃旧场次并重新配对
    epoch: str = Field(default='', description='epoch_help')
    # 多轮累计战斗时间，仅组队流程使用
    total_limit_time: Time = Field(default=Time(hour=4), description='total_limit_time_help')
    # 多轮累计战斗次数，仅组队流程使用
    total_limit_count: int = Field(default=300, description='total_limit_count_help')


class OrochiConfig(ConfigBase):
    # 身份：队长/队员/单人；组队流程使用队长或队员，非组队时与手动队友组队同样适用
    user_status: UserStatus = Field(default=UserStatus.LEADER, description='user_status_help')
    # 层数
    layer: Layer = Field(default=Layer.ELEVEN, description='layer_help')
    # 单轮限制时间，组队与单人共用
    limit_time: Time = Field(default=Time(minute=30), description='limit_time_help')
    # 单轮限制次数，组队与单人共用
    limit_count: int = Field(default=30, description='limit_count_help')
    # 是否开启御魂加成
    soul_buff_enable: bool = Field(default=False, description='soul_buff_enable_help')
    # 是否开启五倍消耗（游戏内五倍卡需已开启，脚本只负责按五倍计数并扣减券）
    five_times_enable: bool = Field(default=False, description='five_times_enable_help')
    # 五倍消耗券剩余数量，每消耗一张会立即回写，保证下次运行读到真实剩余
    five_times_ticket: int = Field(default=0, description='five_times_ticket_help')
    # 是否在完成后拉起RealmRaid任务
    enable_realm_raid_chain: bool = Field(default=True, description='enable_realm_raid_chain_help')

class SwitchSoulConfig(BaseSwitchSoulConfig):
    enable: bool = Field(default=False)
    switch_group_team: str = Field(default='-1,-1', description='switch_group_team_help')
    enable_switch_by_name: bool = Field(default=False, description='enable_switch_by_name_help')
    group_name: str = Field(default='')
    team_name: str = Field(default='')
    auto_switch_soul: bool = Field(default=False, description='auto_switch_soul_orochi_help')
    # 十层 config
    ten_switch: str = Field(default='-1,-1', description='ten_switch_help')
    # 悲鸣 config
    eleven_switch: str = Field(default='-1,-1', description='eleven_switch_help')
    # 神罚 config
    twelve_switch: str = Field(default='-1,-1', description='twelve_switch_help')
    # 虚无 config
    thirteen_switch: str = Field(default='-1,-1', description='thirteen_switch_help')


class Orochi(ConfigBase):
    scheduler: Scheduler = Field(default_factory=Scheduler)
    # 组队配置独立放在副本设置之前，便于一键切换组队/单人
    team_config: OrochiTeamConfig = Field(default_factory=OrochiTeamConfig)
    orochi_config: OrochiConfig = Field(default_factory=OrochiConfig)
    invite_config: InviteConfig = Field(default_factory=InviteConfig)
    general_battle_config: GeneralBattleConfig = Field(default_factory=GeneralBattleConfig)
    switch_soul: SwitchSoulConfig = Field(default_factory=SwitchSoulConfig)

    @model_validator(mode='before')
    @classmethod
    def migrate_legacy_team_fields(cls, data):
        """旧版本把组队字段放在 orochi_config 里，这里自动迁移到 team_config。

        迁移只发生在旧字段存在时，新配置不会受影响；单轮限制仍保留在
        副本设置中，组队与单人共用同一份单轮配置。
        """
        if not isinstance(data, dict):
            return data
        # 复制一份再迁移，避免修改调用方传入的原始字典
        migrated = dict(data)
        orochi_config = data.get('orochi_config')
        old = dict(orochi_config) if isinstance(orochi_config, dict) else {}
        team = dict(migrated.get('team_config')) if isinstance(migrated.get('team_config'), dict) else {}

        # 兼容旧版 enable_team 布尔开关：True 转组队、False 转单人
        if 'enable_team' in team:
            team.setdefault('team_mode', 'team' if bool(team.pop('enable_team')) else 'alone')
        legacy_status = old.get('user_status')
        if legacy_status is not None:
            # 旧版本队长/队员身份默认就是脚本组队流程，迁移后保持同一行为
            team.setdefault('team_mode', 'team' if legacy_status in ('leader', 'member') else 'alone')
        for key in ('leader_instance', 'epoch', 'total_limit_time', 'total_limit_count'):
            if key in old:
                team.setdefault(key, old.pop(key))

        migrated['team_config'] = team
        if isinstance(orochi_config, dict):
            migrated['orochi_config'] = old
        return migrated
