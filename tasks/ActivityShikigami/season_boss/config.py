# This Python file uses the following encoding: utf-8
# 修行合训（season_boss）玩法专用配置, 自包含于本子包
from pydantic import Field

from tasks.Component.config_base import ConfigBase, MultiLine

# 两阶段名: normal=普通搜寻, premium=注灵搜寻
PHASE_NORMAL = 'normal'
PHASE_PREMIUM = 'premium'
VALID_PHASES = (PHASE_NORMAL, PHASE_PREMIUM)
# 品阶合法值, 与游戏内徽章文字一致
VALID_RANKS = ('普通', '精英', '首领')


def parse_group_team(s: str) -> tuple[int, int] | None:
    """
    解析 '组,队' 字符串, 返回 (group, team)。
    '-1,-1' 或非法值返回 None (表示不切换预设)。
    """
    if not s or ',' not in s:
        return None
    parts = s.split(',')
    if len(parts) != 2:
        return None
    a, b = parts[0].strip(), parts[1].strip()
    if not (a.isdigit() and b.isdigit()):
        return None
    group, team = int(a), int(b)
    if group == -1 and team == -1:
        return None
    if not (1 <= group <= 7 and 1 <= team <= 4):
        return None
    return (group, team)


def parse_monster_preset_text(
        text: str) -> list[tuple[str, str, tuple[int, int] | None, tuple[int, int] | None]]:
    """
    解析多行怪物预设文本, 每行格式:
      4段: 怪物名,品阶,队伍组,队伍队              (只切队伍预设, 御魂跟随队伍)
      6段: 怪物名,品阶,队伍组,队伍队,御魂组,御魂队  (队伍预设 + 御魂预设)
    例:
      雷麒麟,普通,1,1
      雷麒麟,精英,2,2,3,1
    返回 [(怪物名, 品阶, 队伍预设或None, 御魂预设或None), ...]
    空行/段数不为4或6/怪物名为空的行跳过。
    """
    result: list[tuple[str, str, tuple[int, int] | None, tuple[int, int] | None]] = []
    if not text:
        return result
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        parts = [p.strip() for p in line.split(',')]
        if len(parts) not in (4, 6):
            continue  # 格式错误, 跳过该行
        monster, rank = parts[0], parts[1]
        if not monster:
            continue
        team_preset = parse_group_team(f'{parts[2]},{parts[3]}')
        # 6段时末两段为御魂预设; 4段时不切御魂
        soul_preset = parse_group_team(f'{parts[4]},{parts[5]}') if len(parts) == 6 else None
        result.append((monster, rank, team_preset, soul_preset))
    return result


class SeasonBossConfig(ConfigBase):
    phase_order: str = Field(default='normal,premium',
                             description='门票阶段顺序: normal=普通搜寻, premium=注灵搜寻, 英文逗号分隔, 从左到右执行')
    monster_preset_text: MultiLine = Field(
        default='雷麒麟,普通,-1,-1,-1,-1\n火麒麟,普通,-1,-1,-1,-1\n风麒麟,普通,-1,-1,-1,-1\n'
                '凤麒麟,普通,-1,-1,-1,-1\n水麒麟,普通,-1,-1,-1,-1\n战火姥姥,精英,-1,-1,-1,-1\n'
                '幽火姥姥,精英,-1,-1,-1,-1\n炽火姥姥,精英,-1,-1,-1,-1\n冥火姥姥,精英,-1,-1,-1,-1\n'
                '姥姥火·合魂,首领,-1,-1,-1,-1',
        description='怪物->预设映射, 每行一个 (品阶:普通/精英/首领, 组1-7,队1-4)\n'
                    '4段(只切队伍): 怪物名,品阶,队伍组,队伍队\n'
                    '6段(队伍+御魂): 怪物名,品阶,队伍组,队伍队,御魂组,御魂队\n'
                    '4段行未配御魂: 开启御魂切换时御魂跟随队伍预设\n'
                    '匹配规则: 怪物名优先, 先按怪物名+品阶匹配, 品阶识别不到时仅按怪物名也能命中\n'
                    '例:\n雷麒麟,普通,1,1\n雷麒麟,精英,2,2,3,1\n幽火姥姥,精英,3,3,4,2')
    default_group_team: str = Field(default='-1,-1',
                                    description='识别不到/未匹配怪物时兜底队伍预设, 组,队; -1,-1表示不切换')
    default_soul_group_team: str = Field(default='-1,-1',
                                         description='命中行未配御魂段或未匹配怪物时兜底御魂预设, 组,队; -1,-1表示不切换')
    enable_preset: bool = Field(default=False, description='是否启用按怪物识别结果切换战斗预设')
    enable_switch_soul: bool = Field(default=False,
                                     description='是否在收服御灵页进式神录切御魂; 未配御魂预设时御魂跟随队伍预设')
    enable_anti_detect: bool = Field(default=False, description='战斗过程是否随机点击或滑动防检测')

    def valid_phases(self):
        """
        校验 phase_order 只允许 normal/premium。
        (不用 @model_validator(mode='after'): ConfigBase.__init__ 对模型级校验错误会抛 IndexError)
        """
        for phase in self.phase_order.split(','):
            phase = phase.strip()
            if not phase:
                continue
            if phase not in VALID_PHASES:
                raise ValueError(f'phase_order can only be one of {VALID_PHASES}, now is {phase}')

    def valid_ranks(self):
        """校验怪物预设文本中各行的品阶合法"""
        for _, rank, _, _ in parse_monster_preset_text(self.monster_preset_text):
            if rank not in VALID_RANKS:
                raise ValueError(f'rank can only be one of {VALID_RANKS}, now is {rank}')

    @property
    def phase_order_v(self) -> list[str]:
        self.valid_phases()
        return [p.strip() for p in self.phase_order.split(',') if p.strip()]
