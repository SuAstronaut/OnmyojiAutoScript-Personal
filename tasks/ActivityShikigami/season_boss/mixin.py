# This Python file uses the following encoding: utf-8
# 修行合训（season_boss）玩法 mixin, 被 ScriptTask 多继承
from time import sleep

from module.base.timer import Timer
from module.logger import logger

from tasks.base_task import BaseTask
from tasks.ActivityShikigami.season_boss.config import (
    PHASE_NORMAL, PHASE_PREMIUM, SeasonBossConfig,
)
from tasks.ActivityShikigami.season_boss.resolver import resolve_monster_preset, should_skip_soul_switch


class SeasonBossMixin(BaseTask):
    """
    修行合训玩法主循环, 一个 climb_type 内部分普通/注灵两阶段。
    依赖 ScriptTask 链上其他 mixin 提供 screenshot/appear/click/ui_click/
    put_status/run_general_battle/switch_preset_team/wait_until_appear 等方法。
    """

    def __init__(self, config, device):
        super().__init__(config, device)
        # 每场识别出的战斗预设 (group, team), 由 handle_monster_select 写入
        self._sb_preset: tuple[int, int] | None = None
        # 同次任务内上次真正进式神录切好的御魂预设, 用于跳过重复切换。
        # 仅真正进式神录切过才更新; 无可切目标时保持原值。
        self._sb_last_soul_preset: tuple[int, int] | None = None

    @property
    def sb_conf(self) -> SeasonBossConfig:
        return self.config.model.activity_shikigami.season_boss

    # ---------------------------------------------------------- 主循环
    def _run_season_boss(self):
        """
        修行合训主入口。进入主页后按 phase_order 逐阶段执行。
        LimitTimeOut/LimitCountOut 由 put_status 抛出, 冒泡到 StateMachine.run() 处理。
        """
        from tasks.ActivityShikigami.script_task import LimitTimeOut, LimitCountOut

        logger.hr('Start run climb type season_boss', 1)
        # season_boss 首领战可能超过默认 5 分钟卡死上限, 长耗时战斗的卡死判定放宽到 10 分钟, 结束后还原
        self.device.stuck_timer_long = Timer(600, count=600).start()
        try:
            # ① 进入修行合训主页: 复用现有 boss 入口, 进入后即为修行合训主页
            self.ui_click(self.I_TO_BATTLE_BOSS, stop=self.I_CHECK_BATTLE_BOSS, interval=1)
            # 确认已在修行合训主页 (OCR 标题「修行合训」), 防止与原 boss 页混淆
            self.wait_until_appear(self.O_SEASON_BOSS_CHECK_MAIN, wait_time=3)

            for phase in self.sb_conf.phase_order_v:
                logger.hr(f'season_boss phase: {phase}', 2)
                try:
                    self._run_sb_phase(phase)
                except (LimitTimeOut, LimitCountOut):
                    break
        finally:
            # 还原通用卡死判定时长, 不影响其他任务
            self.device.stuck_timer_long = Timer(300, count=300).start()

        # ③ 两阶段结束, 返回活动主页
        self.ui_click(self.I_UI_BACK_YELLOW, stop=self.I_TO_BATTLE_MAIN, interval=1)

    def _run_sb_phase(self, phase: str):
        """
        单阶段战斗循环。每轮重新判断页面状态, 再决定 打掉遗留御灵 / 切模式 / 搜寻。
        """
        ocr_limit_timer = Timer(1).start()
        # 连续无法识别页面状态的保护计时器, 识别到任一已知状态就重置; 战斗结束后也需重置
        unknown_timer = Timer(30).start()
        while 1:
            self.screenshot()
            self.put_status()  # 总上限 season_boss_limit / 超时

            # ① 搜寻按钮被锁 = 上次搜到的御灵还没打掉。
            #    此时普通/注灵两个搜寻按钮都不渲染, 无从判断当前门票模式,
            #    必须先把这只打完, 战斗结束回到主页锁自动解除, 再回到本循环重新判断。
            if self.appear(self.I_SEASON_BOSS_LOCKED):
                unknown_timer.reset()
                logger.info('season_boss search locked, fight pending monster first')
                self._resolve_pending_monster()
                # 处理遗留御灵可能含一场战斗, 耗时不计入未知页面保护, 回循环重新判断
                unknown_timer.reset()
                continue

            # ② 未锁, 切到目标门票模式。没切成就重新截图再判断, 不在这里死等。
            if not self._switch_ticket_mode(phase):
                if unknown_timer.reached():
                    logger.warning('season_boss stuck on unknown page, exit phase')
                    break
                continue
            unknown_timer.reset()

            # ③ 阶段内剩余搜寻币 <=0 -> 结束本阶段
            remain = self._read_remain(phase)
            if remain <= 0:
                logger.info(f'season_boss phase {phase} tickets exhausted, next phase')
                break
            if not ocr_limit_timer.reached():
                continue
            ocr_limit_timer.reset()

            # ④ 点击搜寻按钮
            self.appear_then_click(self._discover_button(phase), interval=1)
            # ⑤ 等收服御灵页; 未出现则点左上角卡片触发; 仍未出现则点红X退出重来
            if not self._wait_monster_page():
                continue
            # ⑥ 识别怪物名+品阶 -> 预设 -> 开战
            self._sb_preset = self.handle_monster_select()
            self._open_fight()
            # 战斗耗时不计入未知页面保护, 战斗结束回循环重新判断页面状态
            unknown_timer.reset()

    def _resolve_pending_monster(self) -> bool:
        """
        处理"搜寻被锁"的遗留御灵: 直接点左上角卡片重新打开收服御灵页, 识别后开战。
        战斗结束回到修行合训主页, 锁解除。
        :return: True 已开战; False 未能打开收服页(已点红X回主页)
        """
        # 锁住时收服页必然是关着的, 所以跳过等待直接点卡片, 省 6 秒空等
        if not self._wait_monster_page(click_card_first=True):
            return False
        self._sb_preset = self.handle_monster_select()
        return self._open_fight()

    # ---------------------------------------------------------- 阶段工具
    def _switch_ticket_mode(self, phase: str) -> bool:
        """
        将搜寻模式切到目标阶段 (normal=普通, premium=注灵)。
        单次判断即返回, 不在内部死循环等待——锁住时两个搜寻按钮都不显示,
        死等 stop 会永久卡住(这是之前卡在切换模式的原因)。
        :return: True 已在目标模式; False 本轮未切成, 由调用方重新截图再判断
        """
        target = self._discover_button(phase)
        if self.appear(target):
            return True
        # 锁住时点切换无效, 交回上层先打完遗留御灵
        if self.appear(self.I_SEASON_BOSS_LOCKED):
            return False
        other = self._discover_button(PHASE_PREMIUM if phase == PHASE_NORMAL else PHASE_NORMAL)
        if self.appear(other):
            logger.info(f'season_boss switch mode to {phase}')
            self.appear_then_click(self.I_SEASON_BOSS_MODE_SWITCH, interval=1.9)
        return False

    def _discover_button(self, phase: str):
        """返回对应阶段的搜寻按钮资产"""
        if phase == PHASE_PREMIUM:
            return self.I_SEASON_BOSS_DISCOVER_PREMIUM
        return self.I_SEASON_BOSS_DISCOVER

    def _read_remain(self, phase: str) -> int:
        """读取当前阶段剩余搜寻币 (纯数字, Digit 模式)"""
        self.screenshot()
        ocr = self.O_SEASON_BOSS_REMAIN_PREMIUM if phase == PHASE_PREMIUM else self.O_SEASON_BOSS_REMAIN_NORMAL
        return ocr.ocr_digit(self.device.image)

    def _wait_monster_page(self, click_card_first: bool = False) -> bool:
        """
        等收服御灵页出现。最多等待6秒。
        未出现 -> 点左上角怪物卡片触发; 仍未出现 -> 点红X退出重来, 返回 False。
        :param click_card_first: True 跳过首轮等待直接点卡片。
                                 处理"搜寻被锁"的遗留御灵时收服页必然是关着的, 省 6 秒空等
        """
        if not click_card_first:
            t = Timer(6).start()
            while 1:
                self.screenshot()
                if self.appear(self.I_CHECK_SEASON_BOSS_MONSTER):
                    return True
                if t.reached():
                    break
                sleep(0.3)
        # 兜底1: 点左上角卡片触发怪物页
        # 检测用 I_SEASON_BOSS_CARD(「自己发现」标签, 不随怪物品阶变色),
        # 点击用 C_SEASON_BOSS_CARD(卡面本体), 因为标签有一部分露在卡面之外, 直接点会点空
        logger.warning('season_boss monster page not appear, click card')
        if self.appear(self.I_SEASON_BOSS_CARD):
            self.click(self.C_SEASON_BOSS_CARD, interval=1)
        t = Timer(6).start()
        while 1:
            self.screenshot()
            if self.appear(self.I_CHECK_SEASON_BOSS_MONSTER):
                return True
            if t.reached():
                break
            sleep(0.3)
        # 兜底2: 点红X退出收服页, 回到修行合训主页重来
        logger.warning('season_boss monster page still not appear, click red exit')
        self.ui_click(self.I_SEASON_BOSS_RED_EXIT, stop=self.O_SEASON_BOSS_CHECK_MAIN, interval=1)
        return False

    # ---------------------------------------------------------- 中间界面
    def handle_monster_select(self) -> tuple[int, int] | None:
        """
        中间界面识别怪物名(竖排) + 品阶, 查表得队伍预设与御魂预设。
        御魂在本页进式神录切完就退回; 队伍预设留到战斗准备界面切, 作为返回值。
        返回 (group, team) 或 None(不切队伍预设)。
        """
        self.screenshot()
        # 怪物名是竖排文字, ocr_single_line 不旋转竖排会识别为空; 用 ocr_single 走 detect 兜底
        monster_name = self.O_SEASON_BOSS_MONSTER_NAME.ocr_single(self.device.image)
        rank = self.O_SEASON_BOSS_RANK.ocr_single_line(self.device.image)
        logger.info(f'season_boss monster name=[{monster_name}] rank=[{rank}]')
        if not self.sb_conf.enable_preset:
            return None
        team_preset, soul_preset = resolve_monster_preset(
            monster_name, rank,
            self.sb_conf.monster_preset_text,
            self.sb_conf.default_group_team,
            self.sb_conf.default_soul_group_team,
        )
        logger.info(f'season_boss resolved team={team_preset} soul={soul_preset}')
        # 御魂切换必须在收服页做(战斗准备界面没有式神录入口)
        if self.sb_conf.enable_switch_soul:
            if soul_preset is None:
                # 未配御魂预设 -> 御魂跟随队伍预设, 进式神录切相同预设
                soul_preset = team_preset
                logger.info(f'season_boss soul follow team: {soul_preset}')
            # 有明确御魂目标且与上次已切不一致才进式神录, 同次任务内不重复切换
            if soul_preset is not None and not should_skip_soul_switch(soul_preset, self._sb_last_soul_preset):
                self._switch_soul_on_monster_page(soul_preset)
                self._sb_last_soul_preset = soul_preset
        return team_preset

    def _switch_soul_on_monster_page(self, soul_preset: tuple[int, int]):
        """
        在收服御灵页点「式神录」进入式神录, 复用通用 run_switch_soul 切御魂, 再退回收服页。
        退回带 20s 超时: ui_click 无 timeout 时是死循环, 退不回来会永久卡住。
        """
        group, team = soul_preset
        logger.info(f'season_boss switch soul to ({group},{team})')
        # 进入式神录: 点狐面图标直到它消失
        self.ui_click_until_disappear(self.I_SEASON_BOSS_SHIKIGAMI_RECORD, interval=2)
        # 复用通用御魂切换(内部自行点预设并选组/队)
        self.run_switch_soul((group, team))
        # 退回收服御灵页
        if not self.ui_click(self.I_UI_BACK_YELLOW, stop=self.I_CHECK_SEASON_BOSS_MONSTER,
                             interval=1.5, timeout=20):
            logger.warning('season_boss back to monster page timeout after switch soul')

    # ---------------------------------------------------------- 开战与战斗
    def _open_fight(self) -> bool:
        """
        在收服御灵页开战: 免费/消耗按钮都打。
        点不动 -> 点红X退出重来, 返回 False。
        """
        t = Timer(4).start()
        while 1:
            self.screenshot()
            if (self.appear_then_click(self.I_SEASON_BOSS_FIGHT_FREE, interval=1) or
                    self.appear_then_click(self.I_SEASON_BOSS_FIGHT_PAY, interval=1)):
                # 点开战, 进入战斗准备界面 -> 切预设 -> 跑通用战斗
                return self._run_battle_with_preset(self._sb_preset)
            if t.reached():
                logger.warning('season_boss fight button not clickable, red exit')
                self.ui_click(self.I_SEASON_BOSS_RED_EXIT, stop=self.O_SEASON_BOSS_CHECK_MAIN, interval=1)
                return False
            sleep(0.3)

    def _run_battle_with_preset(self, preset: tuple[int, int] | None) -> bool:
        """
        进入战斗准备界面后 switch_preset_team 切预设, 再以 lock_team_enable=True
        跑 run_general_battle(跳过其内部首战切预设, 由准备按钮自动进入战斗)。
        battle_wait 临时替换为 activity_shikigami_battle_wait(奖励/胜利判定更稳)。
        """
        from tasks.Component.GeneralBattle.config_general_battle import GeneralBattleConfig

        # 等进入战斗准备界面
        t = Timer(5).start()
        while 1:
            self.screenshot()
            if self.is_in_real_battle(False) or self.is_in_prepare(False):
                break
            if t.reached():
                logger.warning('season_boss battle prepare timeout')
                break
            sleep(0.3)

        # 每战切预设 (战斗准备界面)
        if preset is not None:
            group, team = preset
            self.switch_preset_team(True, group, team)

        battle_config = GeneralBattleConfig(
            lock_team_enable=True,               # 切完预设后锁队, 由准备按钮自动进战斗
            preset_enable=False,                 # 预设已手动切换, 不再触发通用切预设
            random_click_swipt_enable=self.sb_conf.enable_anti_detect,
        )
        # 替换 battle_wait, 用奖励/胜利判定更稳
        original_battle_wait = self.battle_wait
        self.battle_wait = self.activity_shikigami_battle_wait
        try:
            self.run_general_battle(config=battle_config)
        finally:
            self.battle_wait = original_battle_wait
        return True
