# This Python file uses the following encoding: utf-8
# @author runhey
# github https://github.com/runhey
import re
from enum import Enum
from datetime import datetime, time
from pydantic import BaseModel, ValidationError, validator, Field, field_validator

# 段长上限：与 auto_total_count 的单段量级对齐（2026-09-09 前为固定 10，后放宽 50）
SEGMENT_COUNT_MAX = 500


def segment_count_normalize(v):
    """段长区间配置归一化（组件与 ActivityShikigami 两处字段共用）。

    兼容历史 int 值：旧版 auto_segment_count 是 int（如 2），直接 str 化。
    语义："34" = 区间 [1,34]（单值即上限）；"1,34" = 区间 [1,34]。
    :param v: 用户输入（int 或 str）
    :return: 归一化后的 "lo,hi" 或 "hi" 形式字符串
    :raises ValueError: 格式非法或 lo > hi 或超出 [1, SEGMENT_COUNT_MAX]
    """
    if isinstance(v, bool):
        raise ValueError('auto_segment_count 不支持布尔值')
    if isinstance(v, int):
        v = str(v)
    if not isinstance(v, str):
        raise ValueError('auto_segment_count 必须是数字或 "低,高" 区间字符串')
    s = v.strip().replace(' ', '').replace('，', ',')
    m_single = re.fullmatch(r'(\d+)', s)
    m_pair = re.fullmatch(r'(\d+),(\d+)', s)
    if m_single:
        lo, hi = 1, int(m_single.group(1))
    elif m_pair:
        lo, hi = int(m_pair.group(1)), int(m_pair.group(2))
    else:
        raise ValueError(f'auto_segment_count 格式非法: {v!r}，应为 "34" 或 "1,34"')
    if not 1 <= lo <= hi <= SEGMENT_COUNT_MAX:
        raise ValueError(f'auto_segment_count 区间越界: {v!r}，需 1 <= 低 <= 高 <= {SEGMENT_COUNT_MAX}')
    return f'{lo},{hi}' if lo > 1 else s


def parse_segment_range(s) -> tuple:
    """解析段长配置为 (lo, hi) 元组。

    "2" → (1, 2)；"1,34" → (1, 34)。运行期直接属性赋值（如测试/代码内
    cfg.auto_segment_count = 2）不经过 validator，故此处容忍 int 原值，
    与归一化路径同语义（int 单值 = 区间 [1, v]）。
    """
    # 非法形态兜底走归一化（int / 带空格等），同路径同语义
    s = segment_count_normalize(s) if not isinstance(s, str) else s
    m_pair = re.fullmatch(r'(\d+),(\d+)', s.strip())
    if m_pair:
        return int(m_pair.group(1)), int(m_pair.group(2))
    return 1, int(s.strip())

class GreenMarkType(str, Enum):
    GREEN_LEFT1 = 'green_left1'
    GREEN_LEFT2 = 'green_left2'
    GREEN_LEFT3 = 'green_left3'
    GREEN_LEFT4 = 'green_left4'
    GREEN_LEFT5 = 'green_left5'
    GREEN_MAIN = 'green_main'

class GeneralBattleConfig(BaseModel):

    # 是否锁定阵容, 有些的战斗是外边的锁定阵容甚至有些的战斗没有锁定阵容的
    lock_team_enable: bool = Field(default=False, description='lock_team_enable_help')

    # 是否启动 预设队伍
    preset_enable: bool = Field(default=False, description='preset_enable_help')
    # 选哪一个预设组
    preset_group: int = Field(default=1, description='preset_group_help', ge=1, le=7)
    # 选哪一个队伍
    preset_team: int = Field(default=1, description='preset_team_help', ge=1, le=5)
    # 是否启动开启buff
    # buff_enable: bool = Field(default=False, description='buff_enable_help')
    # 是否点击觉醒Buff
    # buff_awake_click: bool = Field(default=False, description='')
    # 是否点击御魂buff
    # buff_soul_click: bool = Field(default=False, description='')
    # 是否点击金币50buff
    # buff_gold_50_click: bool = Field(default=False, description='')
    # 是否点击金币100buff
    # buff_gold_100_click: bool = Field(default=False, description='')
    # 是否点击经验50buff
    # buff_exp_50_click: bool = Field(default=False, description='')
    # 是否点击经验100buff
    # buff_exp_100_click: bool = Field(default=False, description='')

    # 是否开启绿标
    green_enable: bool = Field(default=False, description='green_enable_help')
    # 选哪一个绿标
    green_mark: GreenMarkType = Field(default=GreenMarkType.GREEN_LEFT1, description='green_mark_help')

    # 是否启动战斗时随机点击或者随机滑动
    random_click_swipt_enable: bool = Field(default=False, description='random_click_swipt_enable_help')

    # 随机自动战斗段：战斗过程中随机插入若干场自动战斗（点开→游戏连打→点回手动）
    # 字段直接拍平在 GeneralBattleConfig 上而非嵌套子模型——script_task() 的
    # merge_value 与 OASX 前端都只支持一层 group，嵌套 BaseModel 会 KeyError。
    # 总开关：部分战斗不存在自动战斗按钮，默认关闭
    auto_battle_enable: bool = Field(default=False, description='auto_battle_enable_help')
    # 单段连续自动战斗场数（进入自动后连续 M 场由游戏自动完成，脚本零输入）。
    # 2026-09-09 起支持区间随机："34" 表示每段在 [1,34] 随机取长，"1,34" 同义；
    # 历史纯 int 值自动兼容（2 → [1,2]）。规划改为开局一次性生成全部段计划。
    auto_segment_count: str = Field(default='2', description='auto_segment_count_help')
    # 本次任务运行内自动战斗总场数上限 T（不持久化，任务重启重新规划）
    auto_total_count: int = Field(default=4, ge=1, le=200, description='auto_total_count_help')

    # 旧版字段为 int，同名 str 化兼容：int 值在此归一化（见 segment_count_normalize）
    @field_validator('auto_segment_count', mode='before')
    @classmethod
    def _segment_count_compat(cls, v):
        return segment_count_normalize(v)


