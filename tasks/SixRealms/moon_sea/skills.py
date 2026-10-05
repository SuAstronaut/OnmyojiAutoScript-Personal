import re
import json
from pathlib import Path
from uuid import uuid4

from cached_property import cached_property
from module.logger import logger
from module.base.timer import Timer
from module.exception import ScriptError
from tasks.base_task import BaseTask
from tasks.SixRealms.assets import SixRealmsAssets


# Fixed slots establish layout; independent attribute titles establish page identity.
ATTRIBUTE_LAYOUTS = {
    'THREE': (
        (SixRealmsAssets.I_ATTRIBUTE_THREE_STRENGTH_TITLE,
         SixRealmsAssets.I_ATTRIBUTE_THREE_TECHNIQUE_TITLE,
         SixRealmsAssets.I_ATTRIBUTE_THREE_CHARM_TITLE,
         SixRealmsAssets.I_ATTRIBUTE_PAGE_STRENGTH_ALT),
        (SixRealmsAssets.I_ATTRIBUTE_THREE_STRENGTH,
         SixRealmsAssets.I_ATTRIBUTE_THREE_TECHNIQUE,
         SixRealmsAssets.I_ATTRIBUTE_THREE_CHARM)),
    'TWO': (
        (SixRealmsAssets.I_ATTRIBUTE_TWO_TECHNIQUE_TITLE,
         SixRealmsAssets.I_ATTRIBUTE_TWO_CHARM_TITLE,
         SixRealmsAssets.I_ATTRIBUTE_PAGE_TECHNIQUE_ALT),
        (SixRealmsAssets.I_ATTRIBUTE_TWO_TECHNIQUE,
         SixRealmsAssets.I_ATTRIBUTE_TWO_CHARM)),
}


class MoonSeaSkills(BaseTask, SixRealmsAssets):
    cnt_skill101 = 0
    cnt_skillpower = 1  # Baseline route/refresh proxy, NOT the exact game power level.
    get_skill101 = False
    honglei_owned = False
    charm_selected = False
    honglei_guarantee_attempted = False

    @property
    def _conf(self):
        return self.config.model.six_realms.six_realms_gate

    @property
    def has_honglei(self):
        return self.honglei_owned

    @cached_property
    def six_flags_path(self):
        # Separate from abandoned experimental journals; only three sticky flags.
        import hashlib
        scope = f'{self.config.config_name}:{getattr(self.config, "generation", "")}'
        return (Path(__file__).resolve().parents[3] / 'config/tasks_config/sixrealms_flags'
                / (hashlib.sha256(scope.encode()).hexdigest() + '.json'))

    def save_run_flags(self):
        path = self.six_flags_path
        temporary = path.with_suffix('.' + uuid4().hex + '.tmp')
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            temporary.write_text(json.dumps({key: getattr(self, key) for key in (
                'honglei_owned', 'charm_selected', 'honglei_guarantee_attempted')}), encoding='utf-8')
            temporary.replace(path)
            return True
        except OSError as exc:
            logger.warning(f'SixRealms flags could not be saved: {type(exc).__name__}')
            return False
        finally:
            if temporary.exists():
                try:
                    temporary.unlink()
                except OSError:
                    pass

    def load_run_flags(self, new_run=False):
        self.honglei_owned = self.charm_selected = self.honglei_guarantee_attempted = False
        self.cnt_skill101 = 0
        self.cnt_skillpower = 1
        if new_run:
            return self.save_run_flags()
        try:
            data = json.loads(self.six_flags_path.read_text(encoding='utf-8'))
            for key in ('honglei_owned', 'charm_selected', 'honglei_guarantee_attempted'):
                if type(data.get(key)) is bool:
                    setattr(self, key, data[key])
        except (OSError, ValueError, TypeError, AttributeError):
            pass  # Missing old flags never block Continue or an already active scene.
        self.cnt_skill101 = int(self.honglei_owned)
        return True

    def confirm_honglei_owned(self, source):
        if not self.honglei_owned:
            self.honglei_owned = True
            self.save_run_flags()
            logger.info(f'SixRealms Honglei owned: {source}')
        self.cnt_skill101 = max(1, self.cnt_skill101)

    def in_main(self, screenshot=False):
        if screenshot:
            self.screenshot()
        return any(self.appear(rule) for rule in (self.I_M_STORE, self.I_M_STORE_ACTIVITY, self.I_BOSS_FIRE))

    def detect_enhancement_layout(self):
        found = [name for name, (titles, buttons) in ATTRIBUTE_LAYOUTS.items()
                 if any(self.appear(rule) for rule in titles)
                 and all(self.appear(rule) for rule in buttons)]
        return found[0] if len(found) == 1 else None

    def enhancement_reward_popup_visible(self):
        return (self.appear(self.I_SIX_ATTRIBUTE_REWARD_TITLE)
                and self.appear(self.I_SIX_ATTRIBUTE_REWARD_CLOSE_HINT))

    def sixrealms_in_run_scene(self):
        return (self.in_main() or self.detect_enhancement_layout() is not None
                or self.enhancement_reward_popup_visible() or self.appear(self.I_STORE_EXIT)
                or self.appear(self.I_SKILL_REFRESH) and self.appear(self.I_SELECT_3)
                or self.appear(self.I_SIX_BOSS_AUTO) and (
                    self.appear(self.I_SIX_BOSS_NAME) or self.appear(self.I_BATTLE_SKILL101)))

    def read_technique_level(self):
        # Native TWO card's badge number only; no card text or inferred counter.
        if self.device.image.shape[:2] != (720, 1280):
            return None
        rule = self.O_TWO_TECHNIQUE_LEVEL
        try:
            text, confidence = rule.model.ocr_single_line(rule.pre_process(rule.crop(self.device.image, rule.roi)))
            text = text.strip()
            if 0.9 <= confidence <= 1.0 and re.fullmatch(r'[1-9][0-9]?', text):
                return int(text)
        except Exception as exc:
            logger.warning(f'SixRealms technique OCR unavailable: {type(exc).__name__}')
        return None

    def select_enhancement(self):
        layout = self.detect_enhancement_layout()
        if layout is None:
            return False
        if layout == 'THREE':
            selected, rule = 'strength', self.I_ATTRIBUTE_THREE_STRENGTH
        else:
            level = self.read_technique_level()
            if level is None:
                self.screenshot()  # One fresh retry; the layout may have changed.
                if self.detect_enhancement_layout() != 'TWO':
                    return True
                level = self.read_technique_level()
            if level is None:
                logger.warning('SixRealms technique level unreadable after retry; stop before a blind choice')
                self.device.save_screenshot(genre='sixrealms_technique_level_unknown', interval=10)
                raise ScriptError('SixRealms TWO technique level unreadable after one fresh retry; '
                                  'no selection sent, inspect the saved diagnostic')
            selected = 'technique' if level < 3 else 'charm'
            rule = self.I_ATTRIBUTE_TWO_TECHNIQUE if selected == 'technique' else self.I_ATTRIBUTE_TWO_CHARM
        # Baseline outer-loop consumption: no transaction, outcome proof or retry loop.
        # In particular every following TWO page reads its own actual level again.
        if self.appear_then_click(rule, interval=1):
            if selected == 'strength':
                self.cnt_skillpower += 1
            elif selected == 'charm':
                self.charm_selected = True
                self.save_run_flags()
                logger.info('[SixRealms] charm selected, Jue green mark armed')
            logger.info(f'SixRealms attribute selected: layout={layout}, selected={selected}')
        return True

    def battle_lock_team(self):
        self.ui_click(self.I_BATTLE_TEAM_UNLOCK, self.I_BATTLE_TEAM_LOCK)

    def observe_honglei_in_battle(self):
        # Positive icon-body evidence only. No negative frame clears an owned flag.
        if self.appear(self.I_SIX_BOSS_AUTO) and self.appear(self.I_BATTLE_SKILL101):
            self.confirm_honglei_owned('battle_skill_icon')
            return True
        return False

    def island_battle(self):
        self.screenshot()
        while 1:
            self.screenshot()
            self.observe_honglei_in_battle()
            if self.appear(self.I_SKILL_REFRESH) or self.appear(self.I_COIN) or self.enhancement_reward_popup_visible():
                break
            if self.appear_then_click(self.I_NPC_FIRE, interval=1):
                self.device.stuck_record_clear()
                self.device.stuck_record_add('BATTLE_STATUS_S')
                continue
        self.get_skill101 = False
        self.device.stuck_record_clear()

    @cached_property
    def selects_button(self):
        return [self.I_SELECT_0, self.I_SELECT_1, self.I_SELECT_2, self.I_SELECT_3]

    def _select_skill(self):
        self.screenshot()
        select = 3
        self._selected_skill101 = self.appear(self.I_SKILL101)
        if self._selected_skill101:
            x, _ = self.I_SKILL101.front_center()
            select = 0 if x < 360 else 1 if x < 640 else 2 if x < 960 else 3
        logger.info(f'Select {select}')
        return select

    def select_skill(self, refresh=False):
        def check_coin_skill():
            return self.O_COIN_NUM.ocr(self.device.image) >= 50

        def check_refresh():
            match = re.search(r'剩(\d+)次', self.O_SKILL_REFRESH.ocr(self.device.image))
            return bool(match and int(match.group(1)) > 0)

        # Generic popup is consumed by the outer scan; its changing contents never gate selection.
        if self.enhancement_reward_popup_visible():
            self.appear_then_click(self.I_SIX_ATTRIBUTE_REWARD_TITLE, action=self.C_UI_REWARD, interval=1)
            return True
        if self.appear(self.I_UI_CONFIRM):
            self.ui_click_until_disappear(self.I_UI_CONFIRM)
            return True
        if self.select_enhancement():
            return True
        if self.appear(self.I_SKILL_REFRESH) and self.appear(self.I_SELECT_3) and not self.appear(self.I_COIN):
            logger.info('Start select skill')
            select = self._select_skill()
            self.screenshot()
            # Original refresh policy; no countdown/bloom/page-budget gates.
            while (self.cnt_skill101 < 1 and self.cnt_skillpower >= self._conf.power_enhance_level
                   and refresh and select == 3 and not self._selected_skill101
                   and check_coin_skill() and check_refresh()):
                logger.info('Refresh skill')
                self.screenshot()
                self.appear_then_click(self.I_SKILL_REFRESH)
                self.screenshot()
                self.wait_until_stable(self.I_SELECT_3, timer=Timer(0.3, count=1), timeout=Timer(0.8, count=3))
                select = self._select_skill()
            self.screenshot()
            if self.appear(self.selects_button[select]):
                while self.appear_rgb(self.selects_button[select]):
                    self.screenshot()
                    if self.appear_then_click(self.selects_button[select], interval=1) and self._selected_skill101:
                        self.confirm_honglei_owned('ordinary_skill_selection')
            return True
        if self.appear_then_click(self.I_COIN, action=self.C_UI_REWARD, interval=1.5):
            return True
        return False
