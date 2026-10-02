# This Python file uses the following encoding: utf-8
# 怪物名+品阶 -> 战斗预设 查表逻辑 (纯函数, 便于单元测试)
from tasks.ActivityShikigami.season_boss.config import (
    parse_group_team, parse_monster_preset_text,
)


def resolve_monster_preset(
    monster_name: str,
    rank: str,
    monster_preset_text: str,
    default_group_team: str,
    default_soul_group_team: str = '-1,-1',
) -> tuple[tuple[int, int] | None, tuple[int, int] | None]:
    """
    根据识别到的怪物名+品阶, 在多行怪物预设文本中查表得到 (队伍预设, 御魂预设)。
    文本每行格式: 怪物名,品阶,队伍组,队伍队[,御魂组,御魂队]。
    匹配规则: 怪物名优先。
      1. 先按 名字+品阶 精确匹配;
      2. 未命中时退化为只按名字匹配(品阶 OCR 识别不到/不一致也能命中该名字的预设);
      3. 名字也未命中 -> 用 default_group_team / default_soul_group_team 兜底。
    命中行未配御魂段(4段)时, 御魂跟随队伍预设(切相同组/队)。
    元素为 None 表示该项不切换 (-1,-1 或解析失败)。
    """
    rows = parse_monster_preset_text(monster_preset_text)
    # ① 名字+品阶 精确匹配
    for monster, preset_rank, team_preset, soul_preset in rows:
        if monster == monster_name and preset_rank == rank:
            # 命中行未配御魂段(4段)时, 御魂跟随队伍预设
            if soul_preset is None:
                soul_preset = team_preset
            return team_preset, soul_preset
    # ② 怪物名优先: 精确匹配未命中(品阶识别不到/不一致), 退化为只按名字匹配
    for monster, preset_rank, team_preset, soul_preset in rows:
        if monster == monster_name:
            if soul_preset is None:
                soul_preset = team_preset
            return team_preset, soul_preset
    # ③ 名字也未命中, 两项都用兜底预设
    return parse_group_team(default_group_team), parse_group_team(default_soul_group_team)


def should_skip_soul_switch(
    soul_preset: tuple[int, int] | None,
    last_soul_preset: tuple[int, int] | None,
) -> bool:
    """
    判断本场是否需要进式神录切御魂。True 跳过(不切), False 需要切。
    跳过规则(命中任一即跳过):
      1. soul_preset 为 None: 无可切御魂目标(队伍预设也未配), 跳过
      2. soul_preset 与 last_soul_preset 相同: 同次任务内上次已切过, 不重复切换
    其余情况(有明确御魂预设且与上次不同)必须进式神录切换。
    """
    if soul_preset is None:
        return True
    if soul_preset == last_soul_preset:
        return True
    return False
