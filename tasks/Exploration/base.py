# This Python file uses the following encoding: utf-8
# @author runhey
# github https://github.com/runhey
import time
import numpy as np
import random
import cv2
from enum import Enum
from cached_property import cached_property
from datetime import timedelta, datetime
from module.ocr.result import BoxedResult

from tasks.Component.SwitchSoul.switch_soul import SwitchSoul
from tasks.Component.GeneralRoom.general_room import GeneralRoom
from tasks.Component.GeneralInvite.general_invite import GeneralInvite
from tasks.Component.ReplaceShikigami.replace_shikigami import ReplaceShikigami
from tasks.Exploration.assets import ExplorationAssets
from tasks.Exploration.config import ChooseRarity, AutoRotate, AttackNumber, UpType, ExplorationLevel
from tasks.Component.GeneralBattle.general_battle import GeneralBattle
from tasks.Component.GeneralBattle.reward_frame import FORBIDDEN_KEKKAI
from tasks.GameUi.game_ui import GameUi
from tasks.GameUi.page import page_exploration, page_shikigami_records, page_main
from tasks.RealmRaid.script_task import ScriptTask as RealmRaidScriptTask
from tasks.Utils.config_enum import ShikigamiClass

from module.logger import logger
from module.base.timer import Timer
from module.exception import RequestHumanTakeover, TaskEnd, GameStuckError
from module.atom.image_grid import ImageGrid
from module.atom.animate import RuleAnimate
from module.atom.ocr import RuleOcr
from module.base.utils import load_image

class Scene(Enum):
    UNKNOWN = 0  #
    WORLD = 1  # 探索大世界
    ENTRANCE = 2  # 入口弹窗
    MAIN = 3  # 探索里面
    BATTLE_PREPARE = 4  # 战斗准备
    BATTLE_FIGHTING = 5  # 战斗中
    TEAM = 6  # 组队




class BaseExploration(GameUi, GeneralBattle, GeneralRoom, GeneralInvite, ReplaceShikigami, SwitchSoul, ExplorationAssets):
    minions_cnt = 0
    # 章节列表识别为空时允许的重试次数：空结果多为页面过渡帧（点击章节后的加载
    # 动画），连续多次仍空才认定为异常页面（2026-09-11 事故：重试撞上过渡帧被判死）
    EMPTY_LEVEL_RETRY = 5
    # 「点击章节后没进入」需连续采样成立的轮数：慢加载期间界面会短暂停在列表态，
    # 单轮采样会把慢加载误判成点击无效而重试（2026-09-11 事故）
    NO_ENTER_STABLE = 2

    @cached_property
    def _config(self):
        self.config.exploration.general_battle_config.lock_team_enable = True
        limit_time = self.config.exploration.exploration_config.limit_time
        self.limit_time: timedelta = timedelta(
            hours=limit_time.hour,
            minutes=limit_time.minute,
            seconds=limit_time.second
        )
        return self.config.model.exploration

    @cached_property
    def _match_end(self):
        return RuleAnimate(self.I_SWIPE_END)

    def get_current_scene(self, reuse_screenshot: bool = True) -> Scene:
        if not reuse_screenshot:
            self.screenshot()

        if self.appear(self.I_E_ENTRANCE) and self.appear(self.I_E_EXPLORATION_CLICK):
            logger.info("In entrance")
            return Scene.ENTRANCE
        elif self.appear(self.I_CHECK_EXPLORATION) and not self.appear(self.I_E_SETTINGS_BUTTON):
            from time import sleep
            sleep(1.5)
            self.screenshot()
            # 前往探索动画较长，可能先短暂识别为探索大世界，再加载到章节入口。
            if self.appear(self.I_E_ENTRANCE) and self.appear(self.I_E_EXPLORATION_CLICK):
                logger.info("In entrance after exploration transition")
                return Scene.ENTRANCE
            if self.appear(self.I_CHECK_EXPLORATION) and not self.appear(self.I_E_SETTINGS_BUTTON):
                logger.info("In world")
                return Scene.WORLD
        elif self.appear(self.I_E_SETTINGS_BUTTON) or self.appear(self.I_E_AUTO_ROTATE_ON) or self.appear(self.I_E_AUTO_ROTATE_OFF) or self.appear(self.I_E_MAIN_FLAG):
            logger.info("In main scene")
            return Scene.MAIN
        elif self.is_in_prepare():
            logger.info("In battle prepare")
            return Scene.BATTLE_PREPARE
        elif self.is_in_battle():
            logger.info("In battle fighting")
            return Scene.BATTLE_FIGHTING
        elif self.is_in_room() or self.appear(self.I_CREATE_ENSURE):
            logger.info("In room")
            return Scene.TEAM

        logger.info("Unknown scene")
        return Scene.UNKNOWN

    def pre_process(self):
        explorationConfig = self._config
        if explorationConfig.switch_soul_config.enable:
            self.ui_get_current_page()
            self.ui_goto(page_shikigami_records)
            self.run_switch_soul(explorationConfig.switch_soul_config.switch_group_team)

        if explorationConfig.switch_soul_config.enable_switch_by_name:
            self.ui_get_current_page()
            self.ui_goto(page_shikigami_records)
            self.run_switch_soul_by_name(explorationConfig.switch_soul_config.group_name,
                                         explorationConfig.switch_soul_config.team_name)

        # 开启加成
        con = self.config.exploration.exploration_config
        if con.buff_gold_50_click or con.buff_gold_100_click or con.buff_exp_50_click or con.buff_exp_100_click:
            self.ui_get_current_page()
            self.ui_goto(page_main)
            self.open_buff()
            if con.buff_gold_50_click:
                self.gold_50()
            if con.buff_gold_100_click:
                self.gold_100()
            if con.buff_exp_50_click:
                self.exp_50()
            if con.buff_exp_100_click:
                self.exp_100()
            self.close_buff()

        self.ui_get_current_page()
        # 探索页面
        self.ui_goto(page_exploration)

    def post_process(self):
        self.wait_until_stable(self.I_UI_BACK_RED)
        if self.appear(self.I_UI_BACK_RED):
            self.ui_click_until_disappear(self.I_UI_BACK_RED)
        self.ui_get_current_page()
        self.ui_goto(page_main)
        con = self._config.exploration_config
        if con.buff_gold_50_click or con.buff_gold_100_click or con.buff_exp_50_click or con.buff_exp_100_click:
            self.open_buff()
            self.gold_50(is_open=False)
            self.gold_100(is_open=False)
            self.exp_50(is_open=False)
            self.exp_100(is_open=False)
            self.close_buff()
        self.set_next_run(task='Exploration', success=True, finish=False)
        raise TaskEnd('Exploration')

    def reward_forbidden(self) -> tuple:
        """探索结算界面的常驻禁点区域（顶左条 + 顶右条 + 左下角）。

        取代原先「禁用 reward_1、只点右侧」的覆盖：现在落点是全屏挖掉
        常驻禁点区域 + 检测出的奖励行，由检测保证不误点。
        """
        return FORBIDDEN_KEKKAI

    def _chapter_level_values(self) -> list[ExplorationLevel]:
        return [level for level in ExplorationLevel if level != ExplorationLevel.AUTO]

    def _chapter_level_index(self, level: ExplorationLevel | str) -> int:
        if isinstance(level, str):
            level = self.level_name_to_enum(level)
        if level is None:
            raise GameStuckError('Invalid exploration level')
        return int(level.name.split('_')[-1])

    def level_name_to_enum(self, text: str) -> ExplorationLevel | None:
        for level in self._chapter_level_values():
            if level.value == text:
                return level
        return None

    def max_level_from_names(self, names: list[str]) -> ExplorationLevel:
        levels = [self.level_name_to_enum(name) for name in names]
        levels = [level for level in levels if level is not None]
        if not levels:
            return ExplorationLevel.EXPLORATION_1
        return max(levels, key=self._chapter_level_index)

    def enhance_chapter_text(self, image):
        hsv = cv2.cvtColor(image, cv2.COLOR_BGR2HSV)
        v = hsv[:, :, 2]
        v_inv = 255 - v
        v_norm = cv2.normalize(v_inv, None, 0, 255, cv2.NORM_MINMAX)
        result = cv2.cvtColor(v_norm, cv2.COLOR_GRAY2BGR)
        result = cv2.resize(result, None, fx=2, fy=2, interpolation=cv2.INTER_CUBIC)
        return result

    def normalize_detected_levels(self, boxed_results: list[BoxedResult]) -> list[str]:
        valid_names = {level.value for level in self._chapter_level_values()}
        normalized = []
        for result in boxed_results:
            text = result.ocr_text.strip()
            if text in valid_names:
                normalized.append(text)
        return normalized

    def get_level_roi_image(self):
        x, y, w, h = self.O_E_EXPLORATION_LEVEL_NUMBER.roi
        return self.device.image[y:y + h, x:x + w].copy()

    def _create_enhanced_level_ocr(self, image) -> RuleOcr:
        h, w = image.shape[:2]
        return RuleOcr(
            roi=(0, 0, w, h),
            area=(0, 0, 100, 100),
            mode='Full',
            method='Default',
            keyword='',
            name='enhanced_level_ocr',
        )

    def _detect_levels_from_enhanced_image(self, image) -> list[BoxedResult]:
        return self._create_enhanced_level_ocr(image).detect_and_ocr(image)

    def find_max_chapter(self):
        self.ui_get_current_page()
        self.ui_goto(page_exploration)

        previous_level = None
        best_level = None
        stable_count = 0
        max_checks = 8

        while max_checks > 0:
            self.screenshot()
            # 识别过程中如果误触进入章节入口，说明章节已经打开，直接沿用已识别到的最高章节。
            if self.appear(self.I_E_EXPLORATION_CLICK) or self.is_in_room():
                resolved_level = best_level or ExplorationLevel.EXPLORATION_1
                logger.warning(f'Chapter page entered during OCR, use detected chapter: {resolved_level}')
                return resolved_level

            roi_image = self.get_level_roi_image()
            enhanced = self.enhance_chapter_text(roi_image)
            results = self._detect_levels_from_enhanced_image(enhanced)
            current_names = self.normalize_detected_levels(results)
            logger.info(f'Enhanced OCR levels: {current_names}')

            if current_names:
                current_level = self.max_level_from_names(current_names)
                if best_level is None or self._chapter_level_index(current_level) > self._chapter_level_index(best_level):
                    best_level = current_level

                # OCR 列表可能少识别一项，但最高章节连续一致即可认为已稳定，避免继续滑动误入章节入口。
                if current_level == previous_level:
                    stable_count += 1
                else:
                    stable_count = 1
                previous_level = current_level
            else:
                stable_count = 0
            logger.info(f'Stable count: {stable_count}')

            if stable_count >= 2 and previous_level is not None:
                logger.info(f'Resolved max chapter: {previous_level}')
                return previous_level

            self.swipe(self.S_SWIPE_LEVEL_DOWN, interval=1)
            max_checks -= 1

        if best_level is None:
            logger.warning('No valid chapter found after enhanced OCR, defaulting to chapter 1')
        resolved_level = best_level or ExplorationLevel.EXPLORATION_1
        logger.info(f'Resolved max chapter: {resolved_level}')
        return resolved_level

    def click_level_with_enhanced_ocr(self, target_level, max_swipe=40):
        roi_x, roi_y, _, _ = self.O_E_EXPLORATION_LEVEL_NUMBER.roi
        target_level = self.level_name_to_enum(target_level) if isinstance(target_level, str) else target_level
        if target_level is None:
            raise GameStuckError('Invalid exploration level')
        target_name = target_level.value

        # 空帧重试计数：跨 attempt 累计，识别到章节后清零
        empty_retry = 0
        for attempt in range(max_swipe):
            self.screenshot()
            # 已经进入章节入口时不再按章节列表识别，避免把剧情文案页误判为“无可见章节”。
            if self.appear(self.I_E_EXPLORATION_CLICK) or self.is_in_room():
                logger.warning(f'Already in exploration entrance while looking for {target_name}')
                return True

            roi_image = self.get_level_roi_image()
            enhanced = self.enhance_chapter_text(roi_image)
            results = self._detect_levels_from_enhanced_image(enhanced)
            normalized_names = self.normalize_detected_levels(results)

            for result in results:
                if result.ocr_text.strip() != target_name:
                    continue
                box = result.box
                cx = sum(point[0] for point in box) / 4
                cy = sum(point[1] for point in box) / 4
                raw_x = int(roi_x + cx / 2)
                raw_y = int(roi_y + cy / 2)
                self.device.click(x=raw_x, y=raw_y, control_name=f'enhanced_level_{target_name}')
                return True

            visible_levels = [self.level_name_to_enum(name) for name in normalized_names]
            visible_levels = [level for level in visible_levels if level is not None]
            if not visible_levels:
                # 空结果多为页面过渡帧：不立即判死，短等后重试；连续多次仍空才抛
                empty_retry += 1
                if empty_retry > self.EMPTY_LEVEL_RETRY:
                    raise GameStuckError(
                        f'No visible levels found while looking for {target_name}; attempt={attempt + 1}'
                    )
                logger.warning(f'No visible levels while looking for {target_name}, retry: {empty_retry}')
                time.sleep(1)
                continue
            empty_retry = 0

            min_visible = min(visible_levels, key=self._chapter_level_index)
            max_visible = max(visible_levels, key=self._chapter_level_index)
            target_index = self._chapter_level_index(target_level)
            min_index = self._chapter_level_index(min_visible)
            max_index = self._chapter_level_index(max_visible)

            if target_index > max_index:
                self.swipe(self.S_SWIPE_LEVEL_DOWN, interval=1)
                continue
            if target_index < min_index:
                self.swipe(self.S_SWIPE_LEVEL_UP, interval=1)
                continue

            raise GameStuckError(
                f'Enhanced OCR saw {normalized_names} but could not click {target_name}; attempt={attempt + 1}'
            )

        raise GameStuckError(f'Could not find exploration level with enhanced OCR: {target_name}')

    def target_level_visible_with_enhanced_ocr(self, target_level) -> bool:
        target_level = self.level_name_to_enum(target_level) if isinstance(target_level, str) else target_level
        if target_level is None:
            raise GameStuckError('Invalid exploration level')
        target_name = target_level.value

        roi_image = self.get_level_roi_image()
        enhanced = self.enhance_chapter_text(roi_image)
        results = self._detect_levels_from_enhanced_image(enhanced)
        normalized_names = self.normalize_detected_levels(results)
        logger.info(f'Visible levels after chapter click: {normalized_names}')
        return target_name in normalized_names

    # 打开指定的章节：
    def open_expect_level(self):
        explorationConfig = self.config.exploration
        target_level = explorationConfig.exploration_config.exploration_level
        if target_level == ExplorationLevel.AUTO:
            target_level = self.find_max_chapter()
            logger.info(f'Resolved AUTO exploration level to: {target_level}')

        for click_retry in range(8):
            if not self.click_level_with_enhanced_ocr(target_level, max_swipe=40):
                raise GameStuckError(f'Could not find exploration level with enhanced OCR: {target_level}')

            # 「点击没进章节」需连续多轮采样成立才判定：慢加载期间界面会短暂停在
            # 列表态，单轮 1.5 秒采样会把慢加载误判成点击无效（2026-09-11 事故）
            no_enter_count = 0
            for _ in range(6):
                time.sleep(1.5)
                self.screenshot()
                if self.appear_then_click(self.I_UI_CONFIRM, interval=1):
                    no_enter_count = 0
                    continue
                if self.appear_then_click(self.I_UI_CONFIRM_SAMLL, interval=1):
                    no_enter_count = 0
                    continue
                if self.appear(self.I_E_EXPLORATION_CLICK):
                    return True
                if self.is_in_room():
                    return True
                # 章节点击偶发无效时，仍停留在章节列表且目标章节可见就重新点击
                if self.appear(self.I_CHECK_EXPLORATION) and self.target_level_visible_with_enhanced_ocr(target_level):
                    no_enter_count += 1
                    if no_enter_count >= self.NO_ENTER_STABLE:
                        logger.warning(f'Chapter click did not enter page, retry click: {click_retry + 1}')
                        break
                    continue
                # 既没进章节也不在列表（页面正在过渡）：清掉计数，别急着判失败
                no_enter_count = 0
            else:
                continue

        raise GameStuckError(f'Could not enter exploration level after repeated clicks: {target_level}')

    # 候补：
    def enter_settings_and_do_operations(self):
        # 打开设置
        while 1:
            self.screenshot()
            if self.appear(self.I_E_OPEN_SETTINGS):
                logger.info("Open settings")
                break
            if self.is_in_battle():
                logger.warning('Opening settings failed due to now in battle')
                return
            if self.click(self.C_CLICK_SETTINGS, interval=2):
                continue

        # 候补出战数量识别
        self.screenshot()
        if not self.appear(self.I_E_OPEN_SETTINGS):
            logger.warning('Opening settings failed due to now in battle')
            return
        cu, res, total = self.O_E_ALTERNATE_NUMBER.ocr(self.device.image)
        if cu >= 10:
            logger.info("Alternate number is enough")
            self.ui_click_until_disappear(self.I_E_SURE_BUTTON)
            return
        else:
            self.add_shiki()

    # 添加式神
    def add_shiki(self, screenshot=True):
        if screenshot:
            self.screenshot()
            if not self.appear(self.I_E_OPEN_SETTINGS):
                logger.warning('Opening settings failed due to now in battle')
                return

        # 先点候补式神区域，再切换稀有度，避免点击失败
        self.click(self.C_CLICK_STANDBY_TEAM)

        choose_rarity = self._config.exploration_config.choose_rarity
        rarity = ShikigamiClass.N if choose_rarity == ChooseRarity.N else ShikigamiClass.MATERIAL
        self.switch_shikigami_class(rarity)

        # 移动至未候补的狗粮
        while 1:
            # 慢一点
            time.sleep(0.5)
            self.screenshot()
            if not self.appear(self.I_E_OPEN_SETTINGS):
                logger.warning('Opening settings failed due to now in battle')
                return
            if self.appear(self.I_E_RATATE_EXSIT):
                self.swipe(self.S_SWIPE_SHIKI_TO_LEFT)
            else:
                break
        while 1:
            # 候补出战数量识别
            self.screenshot()
            if not self.appear(self.I_E_OPEN_SETTINGS):
                logger.warning('Opening settings failed due to now in battle')
                return
            cu, res, total = self.O_E_ALTERNATE_NUMBER.ocr(self.device.image)
            if cu >= 40:
                break
            self.swipe(self.S_SWIPE_SHIKI_TO_LEFT_ONE)
            # 慢一点
            time.sleep(0.5)
            self.screenshot()
            self.click(self.L_ROTATE_1)
            self.device.click_record_clear()

        self.appear_then_click(self.I_E_SURE_BUTTON)

    # 找up按钮
    def search_up_fight(self, up_type: UpType = None):
        if up_type is None:
            up_type = self._config.exploration_config.up_type
        
        # 1. 如果选择了特定的 UP 类型 (比如达摩)
        if up_type != UpType.ALL:
            match up_type:
                case UpType.EXP:
                    find_flag = self.I_UP_EXP
                case UpType.COIN:
                    find_flag = self.I_UP_COIN
                case UpType.DARUMAA:
                    find_flag = self.I_UP_DARUMA
                case _:
                    find_flag = self.I_UP_EXP
            
            # 尝试寻找 UP 图标
            if self.appear(find_flag):
                # 获取 UP 图标的坐标和中心点
                x, y, w, h = find_flag.roi_front
                x_center, y_center = find_flag.front_center()
                
                logger.info(f'Found up type: {up_type} at {find_flag.roi_front}')

                # 缩小搜索范围 (ROI)
                # 原来左右各扩 160-200，太宽了容易甚至把隔壁怪算进来
                # 现在改为左右各扩 50-80，强制只找垂直线附近的战斗图标
                roi_back_y = max(0, y - 300)      # 向上找300像素
                roi_back_h = y - 20 - roi_back_y  #直到UP图标上方20像素截止
                
                # 左右范围缩窄：防止误触旁边的怪
                roi_back_x = max(0, x - 60)       
                roi_back_w = min(1280, x + w + 60) - roi_back_x
                
                logger.info(f'Searching sword icon in narrowed area: {roi_back_x, roi_back_y, roi_back_w, roi_back_h}')
                
                matches = self.I_NORMAL_BATTLE_BUTTON.match_all(
                    image=self.device.image,
                    threshold=0.9,
                    roi=[roi_back_x, roi_back_y, roi_back_w, roi_back_h]
                )
                
                if matches:
                    distances = []
                    for match in matches:
                        # 这里假设 match[1], match[2] 是 x, y
                        x_match = match[1] + match[3] / 2  # 战斗图标中心 X
                        y_match = match[2] + match[4] / 2  # 战斗图标中心 Y
                        
                        # 这样能完美避开“距离很近但属于隔壁怪”的情况
                        x_diff = abs(x_center - x_match)
                        y_diff = abs(y_center - y_match)
                        weighted_distance = (x_diff * 3) + y_diff
                        
                        distances.append((weighted_distance, match))
                    
                    # 按加权距离排序，取最正对着的一个
                    distances.sort(key=lambda x: x[0], reverse=False)
                    match = distances[0][1]
                    
                    roi_front = list(match[1:])  # x,y,w,h
                    self.I_NORMAL_BATTLE_BUTTON.roi_front = roi_front
                    logger.info(f"Target locked: sword at {roi_front} (aligned with UP icon)")
                    return self.I_NORMAL_BATTLE_BUTTON
            else:
                # 没找到 UP 图标，返回 None 让外层逻辑去处理(滑动或退出)
                return None

        # 2. 如果是默认情况 (UpType.ALL)，则只要有怪就打
        if self.appear(self.I_NORMAL_BATTLE_BUTTON):
            return self.I_NORMAL_BATTLE_BUTTON
            
        return None

    def activate_realm_raid(self, con_scrolls, con) -> None:
        # 判断是否开启突破票检测
        if not con_scrolls.scrolls_enable:
            return
        if self.appear(self.I_E_EXPLORATION_CLICK) and self.appear(self.I_EXP_CREATE_TEAM):
            cu, res, total = self.O_REALM_RAID_NUMBER1.ocr(self.device.image)
        else:
            cu, res, total = self.O_REALM_RAID_NUMBER.ocr(self.device.image)
        # 判断突破票数量
        if cu < con_scrolls.scrolls_threshold:
            return

        # 关闭加成
        if self.appear(self.I_RED_CLOSE):
            self.ui_click_until_disappear(self.I_RED_CLOSE)
        if self.appear(self.I_UI_CANCEL):
            self.ui_click_until_disappear(self.I_UI_CANCEL)
        if self.appear(self.I_UI_CANCEL_SAMLL):
            self.ui_click_until_disappear(self.I_UI_CANCEL_SAMLL)
        self.ui_goto(page_main)
        if con.buff_gold_50_click or con.buff_gold_100_click or con.buff_exp_50_click or con.buff_exp_100_click:
            self.open_buff()
            self.gold_50(is_open=False)
            self.gold_100(is_open=False)
            self.exp_50(is_open=False)
            self.exp_100(is_open=False)
            self.close_buff()

        # 设置下次执行行时间
        logger.info("RealmRaid and Exploration  set_next_run !")
        next_run = datetime.now() + con_scrolls.scrolls_cd
        self.set_next_run(task='Exploration', success=None, finish=False, target=next_run)
        self.set_next_run(task='RealmRaid', success=None, finish=False, server=False, target=datetime.now())
        self.set_next_run(task='MemoryScrolls', success=None, finish=False, target=datetime.now())
        raise TaskEnd('Exploration')

    #
    def check_exit(self) -> bool:
        # True 表示要退出这个任务
        if self.minions_cnt >= self._config.exploration_config.minions_cnt:
            logger.info('Minions count is enough, exit')
            return True
        if datetime.now() - self.start_time >= self.limit_time:
            logger.info('Exploration time limit out')
            return True
        self.activate_realm_raid(self._config.scrolls, self._config.exploration_config)
        return False

    def quit_explore(self):
        logger.info('Quit explore')
        boss_timer = Timer(15)
        boss_timer.start()
        # click_yellow_button = 0 #用于保证只点一次左上返回按钮，不要直接触发连点回到主界面
        
        while 1:
            self.screenshot()
            
            # 探索章节标题界面
            if self.appear(self.I_UI_BACK_YELLOW) and self.appear(self.I_E_EXPLORATION_CLICK):
                break
            # 探索大世界界面
            if self.appear(self.I_CHECK_EXPLORATION) and not self.appear(self.I_E_SETTINGS_BUTTON):
                break
  
            # 防止BOSS打完箱子刚落地，脚本就手快点退出了
            if self.appear_then_click(self.I_BATTLE_REWARD, interval=1.5):
                logger.info("Found battle reward during exit, picking it up.")
                boss_timer.reset()
                continue

            if boss_timer.reached():
                logger.warning('Exit timeout, force clicking back button')
                boss_timer.reset()
                self.click(self.I_UI_BACK_BLUE)
                continue

            if self.appear_then_click(self.I_E_EXIT_CONFIRM, interval=0.8):
                continue
            if self.appear_then_click(self.I_BACK_YOLLOW, interval=3.5):
                continue
            
            if self.appear(self.I_EXPLORATION_TITLE) or self.appear(self.I_CHECK_EXPLORATION):
                break
            if self.appear_then_click(self.I_UI_BACK_YELLOW, interval=3.5):
                continue

    def _hook_special_reward(self) -> bool:
        if self.appear(self.I_STATISTICS) and not self.appear(self.I_REWARD) and not self.appear(self.I_WIN):
            if self.appear_then_click(self.I_CONFIRM_CLOSE_DIFF_SOUL):
                return True
            self.click(self.C_RANDOM_CLICK, interval=1.5)
        return False

    def fire(self, button) -> bool:
        self.appear_then_click(button, interval=3)
        # 短暂等待场景切换，避免截图过早导致误判仍在探索场景
        time.sleep(1.0)
        self.screenshot()
        if (self.appear(self.I_E_SETTINGS_BUTTON) or
                self.appear(self.I_E_AUTO_ROTATE_ON) or
                self.appear(self.I_E_MAIN_FLAG) or
                self.appear(self.I_E_MAIN_SUSHI) or
                self.appear(self.I_E_AUTO_ROTATE_OFF)):
            # 如果还在探索说明，这个是显示滑动导致挑战按钮不在范围内
            logger.warning('Fire button disappear, but still in exploration')
            return False
        self.run_general_battle(self._config.general_battle_config)
        self.minions_cnt += 1
        return True

    def wait_world_stable(self) -> bool:
        """
        # 打开右边箭头 and https://github.com/runhey/OnmyojiAutoScript/pull/1589/
        https://github.com/runhey/OnmyojiAutoScript/issues/1588
        @return:
        """
        while 1:
            scene = self.get_current_scene(reuse_screenshot=False)
            if scene == Scene.WORLD and self.appear(self.I_EXP_ARROW_RIGHT):
                return True
            if scene == Scene.ENTRANCE:
                logger.warning('World scene unstable, possibly transient frame after paper doll collection')
                return False
            if self.appear_then_click(self.I_EXP_ARROW_LEFT, interval=2):
                continue


if __name__ == "__main__":
    import cv2
    from module.config.config import Config
    from module.device.device import Device
    from module.atom.ocr import RuleOcr
    from module.ocr.models import get_ocr_model

    config = Config('oas3')
    device = Device(config)
    t = BaseExploration(config, device)
    t.screenshot()
    t.O_E_EXPLORATION_LEVEL_NUMBER.ocr(t.device.image)
    # ===== 诊断：L_LEVEL_LIST 章节识别 =====
    print("\n" + "=" * 60)
    print("L_LEVEL_LIST diagnosis")
    print("=" * 60)

    # 1) 保存 ROI 裁剪图，肉眼确认输入是否包含目标章节
    x, y, w, h = t.L_LEVEL_LIST.roi_back
    print(f"roi_back: x={x}, y={y}, w={w}, h={h}")
    print(f"full image shape: {t.device.image.shape}")
    roi_image = t.device.image[y:y + h, x:x + w]
    print(f"ROI crop shape: {roi_image.shape}")
    cv2.imwrite("./debug_level_roi.png", roi_image)
    print("ROI saved -> ./debug_level_roi.png")

    # 2) 直接调用底层 PaddleOCR 模型，绕过所有 score / array 过滤
    print("\n--- Raw OCR (no score/array filter) ---")
    model = get_ocr_model("ch")
    raw_results = model.detect_and_ocr(roi_image)
    print(f"raw count = {len(raw_results)}")
    for r in raw_results:
        try:
            top_left = (int(r.box[0][0]), int(r.box[0][1]))
        except Exception:
            top_left = r.box
        print(f"  text='{r.ocr_text}'  score={r.score:.3f}  top_left={top_left}")

    # 3) 走 BaseCor.detect_and_ocr（含 score>=0.6 过滤）
    print("\n--- After BaseCor.detect_and_ocr (score >= 0.6 filter) ---")
    target_ocr = RuleOcr(
        roi=t.L_LEVEL_LIST.roi_back,
        area=(0, 0, 100, 100),
        mode="Full",
        method="Default",
        keyword="第十二章",
        name="第十二章",
    )
    filtered = target_ocr.detect_and_ocr(t.device.image)
    print(f"filtered count = {len(filtered)}")
    for r in filtered:
        print(f"  text='{r.ocr_text}'  score={r.score:.3f}")

    # 4) 走完整 ocr_appear 流程（最后 array 包含过滤）
    print("\n--- Full ocr_appear pipeline ---")
    result = t.L_LEVEL_LIST.ocr_appear(t.device.image, name="第十二章")
    print(f"ocr_appear -> {result}")
    print("=" * 60 + "\n")
    logger.info(result)
    # IMAGE_FILE = r"C:\Users\萌萌哒\Desktop\QQ20240818-163854.png"
    # image = load_image(IMAGE_FILE)
    # t.device.image = image
    #while 1:
    # print(t.search_up_fight(UpType.EXP))
    #    t.screenshot()
    #    print(t.I_UP_DARUMA.test_match(t.device.image))
    #    time.sleep(0.2)
    #from PIL import Image
    # Image.fromarray(t.device.image.astype(np.uint8)).show()

