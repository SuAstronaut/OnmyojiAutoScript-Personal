# This Python file uses the following encoding: utf-8
# @author runhey
# github https://github.com/runhey
from pydantic import BaseModel, Field, model_validator, field_validator

from tasks.Component.config_scheduler import Scheduler
from tasks.Component.config_base import ConfigBase, TimeDelta, dynamic_hide
from tasks.Component.BaseActivity.config_activity import GeneralClimb
from tasks.ActivityShikigami.season_boss.config import SeasonBossConfig


def check_soul_by_number(enable_switch: bool, group_team: str, label: str):
    if not enable_switch:
        return
    if not group_team or group_team == "-1,-1":
        raise ValueError(f"[{label}]Switch Soul configuration is enabled, but there is no setting")
    if ',' not in group_team:
        raise ValueError(f"[{label}]The switch soul configuration must be in English ','")
    parts = group_team.split(',')
    if len(parts) != 2:
        raise ValueError(f"[{label}]The length of the switch soul configuration must be equal to 2")
    if not all(p.strip().isdigit() for p in parts):
        raise ValueError(f"[{label}]Switching soul configurations must be numeric")


class SwitchSoulConfig(BaseModel):
    enable_switch_pass: bool = Field(default=False, description='是否切换门票爬塔御魂')
    pass_group_team: str = Field(default='-1,-1', description='组1-7,队伍1-4 中间用英文,分隔')

    enable_switch_ap: bool = Field(default=False, description='是否切换体力爬塔御魂')
    ap_group_team: str = Field(default='-1,-1', description='组1-7,队伍1-4 中间用英文,分隔')

    enable_switch_boss: bool = Field(default=False, description='是否切换boss爬塔御魂')
    boss_group_team: str = Field(default='-1,-1', description='组1-7,队伍1-4 中间用英文,分隔')

    enable_switch_ap20: bool = Field(default=False, description='是否切换ap20御魂')
    ap20_group_team: str = Field(default='-1,-1', description='组1-7,队伍1-4 中间用英文,分隔')

    enable_switch_pass_monopoly: bool = Field(default=False, description='是否切换大富翁御魂')
    pass_monopoly_group_team: str = Field(default='-1,-1', description='组1-7,队伍1-4 中间用英文,分隔')

    # ap20/大富翁 当前活动不存在对应入口，配置在前端隐藏（功能与字段保留，
    # 活动切换回来后去掉本行即可恢复显示）
    hide_fields = dynamic_hide('enable_switch_ap20', 'ap20_group_team',
                               'enable_switch_pass_monopoly', 'pass_monopoly_group_team')

    # @model_validator(mode='after')
    def validate_switch_soul(self):
        for label in self.get_label_set():
            enable_num = getattr(self, f"enable_switch_{label}", False)
            team = getattr(self, f"{label}_group_team", None)
            check_soul_by_number(enable_num, team, label=label.upper())
        return self

    def get_label_set(self):
        return {field.replace("enable_switch_", "") for field in self.model_fields if
                     field.startswith("enable_switch_")}


class GeneralBattleConfig(BaseModel):
    enable_pass_preset: bool = Field(default=False, description='是否切换门票爬塔预设, 仅数字切换御魂可用')
    enable_pass_anti_detect: bool = Field(default=False, description='门票爬塔战斗过程是否随机点击或滑动')

    enable_ap_preset: bool = Field(default=False, description='是否切换体力爬塔预设, 仅数字切换御魂可用')
    enable_ap_anti_detect: bool = Field(default=False, description='体力爬塔战斗过程是否随机点击或滑动')

    enable_boss_preset: bool = Field(default=False, description='是否切换boss爬塔预设, 仅数字切换御魂可用')
    enable_boss_anti_detect: bool = Field(default=False, description='boss爬塔战斗过程是否随机点击或滑动')

    enable_ap20_preset: bool = Field(default=False, description='是否切换ap20爬塔预设, 仅数字切换御魂可用')
    enable_ap20_anti_detect: bool = Field(default=False, description='ap20爬塔战斗过程是否随机点击或滑动')

    enable_pass_monopoly_preset: bool = Field(default=False, description='是否切换大富翁预设, 仅数字切换御魂可用')
    enable_pass_monopoly_anti_detect: bool = Field(default=False, description='大富翁战斗过程是否随机点击或滑动')

    # ap20/大富翁 当前活动不存在对应入口，配置在前端隐藏（功能与字段保留，
    # 活动切换回来后去掉本行即可恢复显示）
    hide_fields = dynamic_hide('enable_ap20_preset', 'enable_ap20_anti_detect',
                               'enable_pass_monopoly_preset', 'enable_pass_monopoly_anti_detect')

    # 随机自动战斗段（与御魂 Orochi 同功能）：战斗过程随机插入若干场自动战斗（点开→游戏连打→点回手动）
    # 字段平铺一层，不嵌套子模型（script_task 的 merge_value 与 OASX 前端只支持一层 group）
    # 总开关：默认关闭；仅对 门票/体力/boss/100体 爬塔生效，ap20/大富翁/修行合训不接
    auto_battle_enable: bool = Field(default=False, description='是否启用随机自动战斗段')
    # 单段连续自动战斗场数（进入自动后连续 M 场由游戏自动完成，脚本零输入）。
    # 2026-09-09 起支持区间随机："34" 表示每段在 [1,34] 随机取长，"1,34" 同义；
    # 历史纯 int 值自动兼容（2 → [1,2]）。规划改为开局一次性生成全部段计划
    auto_segment_count: str = Field(default='2', description='单段自动战斗场数区间, 如 1,34')
    # 本次任务运行内自动战斗总场数上限 T（不持久化，任务重启重新规划）
    auto_total_count: int = Field(default=4, ge=1, le=200, description='自动战斗总场数上限')

    # 旧版字段为 int，同名 str 化兼容：归一化复用组件侧 segment_count_normalize
    @field_validator('auto_segment_count', mode='before')
    @classmethod
    def _segment_count_compat(cls, v):
        from tasks.Component.GeneralBattle.config_general_battle import segment_count_normalize
        return segment_count_normalize(v)

    # boss 战提前退出：战斗中 OCR 读「当前成绩」，达到阈值就主动退出战斗。
    # 退出后走常规结算（本期 boss 无失败页），胜利页/奖励页交 battle_wait 既有分支处理。
    # 与上面的自动战斗段同时开启时**以段优先**：段内是强制零输入（见 script_task
    # 文件头注释），退出点击插不进去，所以段执行期间不提前退出；段结束后的手动
    # 战斗（battle_wait 接管）照常生效。
    enable_boss_early_exit: bool = Field(
        default=False, description='boss爬塔是否启用提前退出(当前成绩达到阈值即退出战斗)')
    # 阈值 <= 0 视为不启用（fail-closed：避免"开了开关但阈值没填"变成静默失效）
    boss_early_exit_score: int = Field(
        default=0, description='当前成绩达到该值即退出boss战斗, 0表示不启用')


class ActivityShikigami(ConfigBase):
    scheduler: Scheduler = Field(default_factory=Scheduler)
    general_climb: GeneralClimb = Field(default_factory=GeneralClimb)
    switch_soul_config: SwitchSoulConfig = Field(default_factory=SwitchSoulConfig)
    general_battle: GeneralBattleConfig = Field(default_factory=GeneralBattleConfig)
    season_boss: SeasonBossConfig = Field(default_factory=SeasonBossConfig)

    # @model_validator(mode='after')
    def validate_switch_preset(self):
        label_set = self.switch_soul_config.get_label_set()
        for label in label_set:
            enable_preset = getattr(self.general_battle, f"enable_{label}_preset", False)
            group_team = getattr(self.switch_soul_config, f"{label}_group_team", None)
            try:
                check_soul_by_number(enable_preset, group_team, label=label.upper())
            except ValueError:
                raise ValueError(f'The switch preset is enabled, but the switch soul is configured incorrectly')
        return self
