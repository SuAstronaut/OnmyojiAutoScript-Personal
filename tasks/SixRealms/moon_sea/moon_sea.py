import time
import re
from typing import Callable

from module.logger import logger
from cached_property import cached_property
from datetime import datetime, timedelta

from tasks.SixRealms.moon_sea.map import MoonSeaMap
from tasks.SixRealms.moon_sea.l101 import MoonSeaL101
from tasks.SixRealms.moon_sea.l102 import MoonSeaL102
from tasks.SixRealms.moon_sea.l103 import MoonSeaL103
from tasks.SixRealms.moon_sea.l104 import MoonSeaL104
from tasks.SixRealms.moon_sea.l105 import MoonSeaL105
from tasks.SixRealms.common import MoonSeaType


# OAS 1280x720 points captured with click_collector and manually hit-validated.
# Detection resources cover only the supplied boss/central-target formation.
# Prefer P1; P2/P3 are bounded, deterministic fallbacks, not a random rectangle.
JUE_SAFE_POINTS = (
    (625, 544),  # P1
    (624, 526),  # P2
    (631, 569),  # P3: valid hitbox point, outside the detection ROI.
)
JUE_TARGET_RULE = (MoonSeaMap.I_JUE_TARGET_A, MoonSeaMap.I_JUE_TARGET_B,
                   MoonSeaMap.I_JUE_TARGET_UNMARKED)
JUE_MARK_CONFIRM_RULE = MoonSeaMap.I_JUE_MARK  # Only the observed central target ROI.
BOSS_REAL_SCENE_RULES = (MoonSeaMap.I_SIX_BOSS_NAME, MoonSeaMap.I_SIX_BOSS_AUTO)


def mark_with_safe_points(points: tuple[tuple[int, int], ...],
                          target_present: Callable[[], bool],
                          marked: Callable[[], bool],
                          click: Callable[[int, int], None]) -> tuple[str, int]:
    """Deterministic, bounded attempts; confirmation must belong to Jue's target."""
    if not points:
        return "unconfigured", 0
    if (len(points) > 3 or any(len(point) != 2 or any(type(v) is not int for v in point)
                              or not (0 <= point[0] < 1280 and 0 <= point[1] < 720)
                              for point in points)):
        return "invalid_points", 0
    if not target_present():
        return "target_missing", 0
    if marked():
        return "already_marked", 0
    for attempts, (x, y) in enumerate(points, 1):
        if not target_present():
            return "target_missing", attempts - 1
        click(x, y)
        if marked():
            return "marked", attempts
    return "failed", len(points)


class MoonSea(MoonSeaMap, MoonSeaL101, MoonSeaL102, MoonSeaL103, MoonSeaL104, MoonSeaL105):

    @property
    def _conf(self):
        return self.config.model.six_realms.six_realms_gate

    def _run_moon_sea(self):
        # 在六道界面
        limit_time = self._conf.limit_time
        max_cont = self._conf.limit_count
        max_time: timedelta = timedelta(
            hours=limit_time.hour,
            minutes=limit_time.minute,
            seconds=limit_time.second
        )
        cnt = 0
        while 1:
            #self._check_first_priority_task()
            if cnt >= max_cont:
                logger.info('Run out of count, exit')
                break
            if datetime.now() - self.start_time >= max_time:
                logger.info('Run out of time, exit')
                break
            if self.one():
                cnt += 1
                logger.info(f'Run {cnt} times,avg time: {timedelta(seconds=int((datetime.now() - self.start_time).total_seconds() / cnt))}, total time: {timedelta(seconds=int((datetime.now() - self.start_time).total_seconds()))}')
            else:
                break
        self.push_notify(content=f'任务已完成{cnt}次,用时: {timedelta(seconds=int((datetime.now() - self.start_time).total_seconds()))}')
        logger.info('Exit Moon Sea')

    def one(self):
        self._six_guarantee_remaining = None
        self.get_skill101 = False
        if not self._start():
            return False
        while 1:
            self.screenshot()            
            # Direct takeover must consume an already running battle, rather than
            # send it through an island handler that waits for the vanished fire button.
            if self.six_real_boss_scene():
                if self.boss_battle():
                    return True
                continue
            if self.appear(self.I_SIX_BOSS_AUTO) and self.appear(self.I_BATTLE_SKILL101):
                self.island_battle()
                continue
            if self.select_skill(refresh=True):
                continue
            if self.maybe_honglei_guarantee():
                continue
            if self.enter_island():
                continue
            isl_type = self.island_name()
            if not isl_type:
                continue
            match isl_type:
                case MoonSeaType.island101: self.run_l101()
                case MoonSeaType.island102: self.run_l102()
                case MoonSeaType.island103: self.run_l103()
                case MoonSeaType.island104: self.run_l104()
                case MoonSeaType.island105: self.run_l105()
                case MoonSeaType.island106:
                    logger.info('Is boss island')
                    self.boss_team_lock()
                    if self.boss_battle():
                        return True
                    else:
                        continue

    def _continue(self):
        logger.warning('Moon Sea Continue')
        while 1:
            self.screenshot()
            if self.in_main():
                break
            if self.appear_then_click(self.I_MCONINUE, interval=1):
                continue

    def _start(self):
        logger.hr('Moon Sea', 1)
        while 1:
            self.screenshot()
            if self.appear(self.I_MSTART,interval=1):
                if self._conf.number_enable:
                    cu = self.O_SIXREALMS_NUMBER.ocr(self.device.image)
                    logger.info(f"六道门票数量：{cu}")
                    if not cu > 0:
                        self.push_notify("六道门票数量不足, 退出！")
                        return False
                break
            if self.appear(self.I_MCONINUE):
                # 继续上一把的
                self.load_run_flags()
                self.get_skill101 = True
                self.ui_click_until_disappear(self.I_MCONINUE)
                return True
            if self.sixrealms_in_run_scene():
                self.load_run_flags()
                return True
            if self.appear_then_click(self.I_MENTER, interval=1):
                continue
        self.load_run_flags(new_run=True)
        logger.info("Ensure select ShouZu")
        while 1:
            self.screenshot()
            if self.appear(self.I_MSHOUZU):
                break
            if self.appear_then_click(self.I_MSHUTEN, interval=1):
                continue
            if self.appear_then_click(self.I_MSHOUZU_SELECT, interval=1):
                continue
        logger.info("Ensure selected ShouZu")
        while 1:
            self.screenshot()
            if self.appear(self.I_PREPARE_BATTLE):
                break
            if self.appear_then_click(self.I_MSTART_UNCHECK, interval=0.6):
                continue
            if self.appear_then_click(self.I_UI_CONFIRM, interval=1):
                continue
            if self.appear_then_click(self.I_UI_CONFIRM_SAMLL, interval=1):
                continue
            if self.appear_then_click(self.I_MSKIP, interval=1.5):
                continue
            if self.appear_then_click(self.I_MSTART, interval=1):
                continue
            if self.appear_then_click(self.I_MSTART_CONFIRM, interval=1):
                continue
            if self.appear_then_click(self.I_MSTART_CONFIRM2, interval=1):
                continue
            if self.appear_then_click(self.I_MCONINUE, interval=1):
                continue
            if self.appear_then_click(self.I_MPEACOCK_SKILL, interval=1):
                continue
        logger.info("Start Roguelike")
        while 1:
            self.screenshot()
            if self.appear(self.I_M_STORE):
                break
            if self.appear_then_click(self.I_MFIRST_SKILL, interval=1):
                continue
        # 选中第一个柔风
        logger.info("Select first skill")
        return True

    def island_name(self):
        while 1:
            self.screenshot()
            text = self.O_ISLAND_NAME.ocr(self.device.image)
            if '绽放' in text:
                return MoonSeaType.island105
            if '战之' in text:
                return MoonSeaType.island104
            if '混' in text:
                return MoonSeaType.island103
            if '神秘' in text:
                return MoonSeaType.island102
            if '宁息' in text:
                return MoonSeaType.island101
            if '恋色' in text:
                return MoonSeaType.island106
            else:
                return False

    def boss_team_lock(self):
        while 1:
            self.screenshot()
            if self.appear(self.I_BOSS_TEAM_LOCK):
                break
            if self.appear_then_click(self.I_BOSS_TEAM_UNLOCK, interval=1):
                logger.info('Click lock Boss Team')
                continue

    def boss_battle(self) -> bool:
        logger.hr('Boss Battle')
        self.ui_click_until_disappear(self.I_BOSS_FIRE, interval=1)
        self._six_jue_mark_checked = False
        self.device.stuck_record_clear()
        self.device.stuck_record_add('BATTLE_STATUS_S')
        while 1:
            self.screenshot()
            if self.appear(self.I_BOSS_SHARE):
                break
            if self.appear(self.I_BOSS_BATTLE_GIVEUP):
                # 打boss失败了
                logger.warning('Boss battle give up')
                self.ui_click_until_disappear(self.I_BOSS_BATTLE_GIVEUP, interval=1)
                continue

            self.observe_honglei_in_battle()
            if self.charm_selected and not self._six_jue_mark_checked:
                self.maybe_mark_jue_in_boss()

            if self.appear(self.I_BOSS_USE_DOUBLE, interval=1):
                # 双倍奖励
                logger.info('Double reward')
                self.ui_get_reward(self.I_BOSS_USE_DOUBLE)
            if self.ui_reward_appear_click():
                continue
            if self.appear_then_click(self.I_BOSS_GET_EXP, interval=1):
                logger.info('Get EXP')
                continue
            if self.appear_then_click(self.I_UI_CANCEL, interval=1):
                # 取消购买 万相赐福
                continue
            if self.appear_then_click(self.I_UI_CONFIRM_SAMLL, interval=1):
                continue
            if self.appear(self.I_BOSS_SKIP, interval=1):
                # 第二个boss
                self.device.stuck_record_clear()
                self.device.stuck_record_add('BATTLE_STATUS_S')
                continue
        logger.info('Boss battle end')
        #self.save_image()
        if self.wait_until_appear(self.I_BOSS_SHUTU, wait_time=20):
            self.ui_click(self.I_BOSS_SHUTU, stop=self.I_MSTART)
        return True


    def map_scene_ready(self):
        # Summon markers can remain visible behind dialogs. OCR only a clean map.
        return (self.in_main() and not self.detect_enhancement_layout()
                and not self.enhancement_reward_popup_visible()
                and not any(self.appear(rule) for rule in (
                    self.I_UI_CONFIRM, self.I_UI_CANCEL, self.I_NINGXI_INSUFFICIENT_ENTER,
                    self.I_SIX_SUMMON_CONFIRM_TITLE, self.I_SIX_SUMMON_INSUFFICIENT_TITLE,
                    self.I_SKILL_REFRESH, self.I_STORE_EXIT, self.I_BOSS_FIRE)))

    def read_boss_rounds_remaining(self):
        # Fresh run map19. Only final guarantee uses this reading; no cell history.
        image = self.device.image
        if image.shape[:2] != (720, 1280):
            return None
        rule = self.O_BOSS_ROUNDS_REMAINING
        try:
            text, confidence = rule.model.ocr_single_line(rule.pre_process(rule.crop(image, rule.roi)))
            text = text.strip()
            if (0.9 <= confidence <= 1.0 and re.fullmatch(r'[1-9][0-9]?', text)
                    and 1 <= int(text) <= 19):
                return int(text)
        except Exception as exc:
            logger.warning(f'SixRealms countdown OCR unavailable: {type(exc).__name__}')
        return None

    def maybe_honglei_guarantee(self):
        if self.has_honglei or self.honglei_guarantee_attempted or not self.map_scene_ready():
            self._six_guarantee_remaining = None
            return False
        remaining = self.read_boss_rounds_remaining()
        if remaining not in (1, 2):
            self._six_guarantee_remaining = None
            return False  # Unreadable is never guessed from a local counter.
        if getattr(self, '_six_guarantee_remaining', None) != remaining:
            self._six_guarantee_remaining = remaining
            return True  # One existing outer-loop capture confirms the small number.
        self.honglei_guarantee_attempted = True
        if not self.save_run_flags():
            logger.warning('SixRealms guarantee skipped: could not persist its one-time flag')
            return False
        if not self.appear_then_click(self.I_M_STORE_ACTIVITY, interval=1):
            logger.warning('SixRealms guarantee skipped: summon button unavailable')
            return False
        answered = entered = False
        for _ in range(24):
            self.screenshot()
            if self.appear(self.I_SIX_SUMMON_INSUFFICIENT_TITLE):
                if not entered:
                    entered = self.appear_then_click(self.I_NINGXI_INSUFFICIENT_ENTER, interval=1)
                continue
            if self.appear(self.I_SIX_SUMMON_CONFIRM_TITLE):
                if not answered:
                    answered = self.appear_then_click(self.I_UI_CONFIRM, interval=1)
                continue
            if answered and not entered and self.appear(self.I_NINGXI):
                entered = self.appear_then_click(self.I_NINGXI, interval=1)
                continue
            if entered and self.appear(self.I_STORE_EXIT):
                self.run_l101()  # Original buying, coin checks, store refresh and exit.
                if not self.has_honglei:
                    logger.warning('SixRealms final guarantee finished without Honglei; continue run')
                return True
        logger.warning('SixRealms final guarantee outcome not observed; return to outer loop, no second summon')
        return True

    def six_real_boss_scene(self):
        return bool(BOSS_REAL_SCENE_RULES) and all(self.appear(rule) for rule in BOSS_REAL_SCENE_RULES)

    def six_jue_target_present(self):
        rules = JUE_TARGET_RULE if isinstance(JUE_TARGET_RULE, tuple) else (JUE_TARGET_RULE,)
        return any(self.appear(rule) for rule in rules if rule is not None)

    def maybe_mark_jue_in_boss(self):
        """Called only by boss_battle; unknown coordinates/resources never click."""
        if not self.charm_selected:
            return
        if (JUE_TARGET_RULE is None or JUE_MARK_CONFIRM_RULE is None
                or not BOSS_REAL_SCENE_RULES):
            self._six_jue_mark_checked = True
            logger.warning('NEEDS_REAL_GAME_VALIDATION: Jue safe points/target confirmation/boss scene not configured; skip marking')
            return
        if not self.six_real_boss_scene() or not self.six_jue_target_present():
            return
        self._six_jue_mark_checked = True
        if not JUE_SAFE_POINTS:
            logger.warning('NEEDS_REAL_GAME_VALIDATION: record a manually verified exact Jue P1; skip marking')
            return

        def target_present():
            self.screenshot()
            return self.six_real_boss_scene() and self.six_jue_target_present()

        def marked():
            # Wait briefly for input/animation, then require stable target marking.
            stable = 0
            for _ in range(8):
                self.screenshot()
                if not self.six_real_boss_scene():
                    return False
                stable = stable + 1 if (self.six_jue_target_present()
                                         and self.appear(JUE_MARK_CONFIRM_RULE)) else 0
                if stable >= 3:
                    return True
                self.device.sleep(0.2)
            return False

        result, attempts = mark_with_safe_points(JUE_SAFE_POINTS, target_present, marked,
                                                 self.device.click)
        logger.info(f'SixRealms Jue green mark: {result}, attempts={attempts}')
        if result not in {'marked', 'already_marked'}:
            logger.warning(f'Jue green mark stopped: {result}, attempts={attempts}')


if __name__ == '__main__':
    from module.config.config import Config

    c = Config('du')
    t = MoonSea(c)
    t.one()
    # t.select_skill()
