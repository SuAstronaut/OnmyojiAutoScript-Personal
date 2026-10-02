# This Python file uses the following encoding: utf-8
# @author runhey
# github https://github.com/runhey
from time import sleep, time
from datetime import datetime, timedelta
import cv2
import numpy as np
import random
from typing import Any
from cached_property import cached_property

from module.atom.click import RuleClick
from module.atom.ocr import RuleOcr
from module.base.protect import random_sleep
from module.base.timer import Timer
from module.exception import TaskEnd
from module.logger import logger

from tasks.base_task import BaseTask
from tasks.Component.GeneralBattle.config_general_battle import GeneralBattleConfig
# 通用组件的 OCR 运行数字判定（0-200 纯数字 = 自动中），自动段覆盖方法复用
from tasks.Component.GeneralBattle.general_battle import auto_ocr_running
# 结算落点：奖励页安全区域加权挑选（与基类 battle_wait 同一套落点）
from tasks.Component.GeneralBattle.reward_frame import (
    weighted_choice, FORBIDDEN_ACTIVITY, HotShape)
from tasks.ActivityShikigami.assets import ActivityShikigamiAssets
from tasks.ActivityShikigami.config import SwitchSoulConfig, GeneralBattleConfig, ActivityShikigami
from tasks.Component.BaseActivity.base_activity import BaseActivity
from tasks.Component.BaseActivity.config_activity import GeneralClimb
from tasks.Component.SwitchSoul.switch_soul import SwitchSoul
from tasks.GameUi.game_ui import GameUi
from tasks.GameUi.page import page_main, page_shikigami_records
from tasks.ActivityShikigami.pass_monopoly import PassMonopolyMixin
from tasks.ActivityShikigami.season_boss.mixin import SeasonBossMixin
import tasks.Component.GeneralBattle.config_general_battle
import tasks.ActivityShikigami.page as game


# 爬塔自动段专用参数（覆盖通用组件实现时使用，见 ScriptTask 内覆盖方法）：
# 通用组件的自动段已按御魂节奏真机验证（2026-09-08 21:13 那轮全流程正常），
# 不改通用代码；爬塔节奏（进场动画慢、单场 60s+）暴露的问题全部在本文件覆盖
# 段内 stuck 续窗间隔（秒）：device 的 stuck 计时只被点击/滑动重置，段内
# 零输入（硬约束：自动战斗过程中不能点），长窗到期必炸——改为纯状态续窗
# （clear 重置计时器后立即补回长战斗标记，不产生任何输入），每 120s 一次
AUTO_BATTLE_STUCK_REFRESH_S = 120
# 段内单场墙钟超时（秒）：从本场开始到见到结算页的最长等待，超过视为
# 流转异常（游戏卡死/自动失效），退出段交 battle_wait 既有逻辑兜底。
# 爬塔自动模式的慢战斗（全屏技能动画）实测可达 60s+，3 分钟是宽松上限
AUTO_BATTLE_BATTLE_TIMEOUT_S = 180

# boss 提前退出的成绩识别间隔（秒）：battle_wait 单轮约 1~2s，不节流等于每轮
# 都跑一次成绩 OCR。成绩是秒级滚动更新的，4s 的粒度足够——阈值是「打到多少分
# 就不打了」，早一两秒退出没有收益，反而更容易在多退一次点击上出错。
BOSS_EARLY_EXIT_SCAN_INTERVAL_S = 4

# boss 提前退出的成绩确认帧数：首帧读到达标后还要连读 N-1 帧，N 帧全达标才真退出。
# 单帧 OCR 误读（数字滚动的那一瞬、模板被技能特效压住）可能读出一个远超真值的
# 数，只凭一帧就退出会误退——多打几帧确认能把这个风险压掉，代价是退出晚约 1s。
# 确认阶段是**帧连续**的，不走 4s 节流：三帧摊成 12s 会让退出晚十几秒，与节流
# 「只是省开销、不延后决策」的初衷相反。
BOSS_EARLY_EXIT_CONFIRM_FRAMES = 3

# boss 主循环「既不在战斗、也看不到入场按钮」的空转上限（秒）。正常换页/开场
# 动画几秒内就恢复；超过说明停在未知页面，再空转只会把 device 的 stuck 计时器
# （60s）拖炸。必须明显小于 60s，保证收口权在本循环而不是 GameStuckError
BOSS_IDLE_TIMEOUT_S = 45


def _prepare_image_for_ocr(image: np.ndarray, asset: RuleOcr) -> np.ndarray:
    image_copy = image.copy()
    x, y, w, h = asset.roi
    roi_to_process = image_copy[y:y + h, x:x + w]
    if len(roi_to_process.shape) == 3:
        gray_image = cv2.cvtColor(roi_to_process, cv2.COLOR_BGR2GRAY)
    else:
        gray_image = roi_to_process
    # 自适应二值化
    _, binary_norm = cv2.threshold(gray_image, 127, 255, cv2.THRESH_BINARY | cv2.THRESH_OTSU)
    _, binary_inv = cv2.threshold(gray_image, 127, 255, cv2.THRESH_BINARY_INV | cv2.THRESH_OTSU)
    if cv2.countNonZero(binary_norm) < cv2.countNonZero(binary_inv):
        binary_correct = binary_norm
    else:
        binary_correct = binary_inv
    kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (5, 1))
    dilated_image = cv2.dilate(binary_correct, kernel, iterations=1)
    # 找轮廓
    contours, _ = cv2.findContours(dilated_image, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    processed_roi_content = None
    if contours:
        all_points = np.concatenate(contours, axis=0)
        bx, by, bw, bh = cv2.boundingRect(all_points)
        processed_roi_content = binary_correct[by:by + bh, bx:bx + bw]
    centered_roi = np.full((h, w), 255, dtype=np.uint8)  # 255代表白色
    if processed_roi_content is not None:
        content_h, content_w = processed_roi_content.shape
        if content_h <= h and content_w <= w:
            # 计算居中粘贴的位置，放到中间
            start_y = (h - content_h) // 2
            start_x = (w - content_w) // 2
            paste_area = centered_roi[start_y:start_y + content_h, start_x:start_x + content_w]
            paste_area[processed_roi_content == 255] = 0
        else:
            logger.warning(f"Content for asset '{asset.name}' is larger than ROI. Skipping centering.")
            # 内容过大，直接使用原始二值图的反转作为结果
            centered_roi = cv2.bitwise_not(binary_correct)
    else:
        logger.warning(f"No content found in ROI for asset: {asset.name}. ROI will be blank.")
    processed_roi_bgr = cv2.cvtColor(centered_roi, cv2.COLOR_GRAY2BGR)
    image_copy[y:y + h, x:x + w] = processed_roi_bgr
    return image_copy


class LimitTimeOut(Exception):
    pass


class LimitCountOut(Exception):
    pass

class StateMachine(BaseTask):
    run_idx: int = 0  # 当前爬塔类型
    _count_map = None

    @cached_property
    def conf(self) -> GeneralClimb:
        return self.config.model.activity_shikigami

    @property
    def climb_type(self) -> str:
        if self.run_idx >= len(self.conf.general_climb.run_sequence_v):
            return self.conf.general_climb.run_sequence_v[-1]
        return self.conf.general_climb.run_sequence_v[self.run_idx]

    @property
    def count_map(self) -> dict[str, int]:
        """
        :return: key: climb type, value: run count
        """
        if not getattr(self, "_count_map", None):
            self._count_map = {climb_type: 0 for climb_type in self.conf.general_climb.run_sequence_v}
        return self._count_map

    # ----------------------------------------------------
    def put_status(self):
        """
        更新全局状态
        """

        def get_count(self) -> int:
            return self.count_map[self.climb_type]

        def get_limit(self) -> int:
            limit = getattr(self.conf.general_climb, f'{self.climb_type}_limit', 0)
            return 0 if not limit else limit

        # 超过运行时间
        if self.limit_time is not None and datetime.now() - self.start_time >= self.limit_time:
            logger.info(f"Climb type {self.climb_type} time out")
            raise LimitTimeOut
        # 次数达到限制
        if get_count(self) >= get_limit(self):
            logger.info(f"Climb type {self.climb_type} count limit reached")
            raise LimitCountOut

    def switch_next(self):
        """
        切换下一种爬塔类型
        :return: True 切换成功 or False
        """
        self.run_idx += 1
        if self.run_idx >= len(self.conf.general_climb.run_sequence_v):
            logger.info('All climbing activities have been completed')
            return False
        # 切换爬塔类型了, 恢复所有状态
        self.current_count = 0
        logger.hr(f'Climb switch to {self.climb_type}', 2)
        return True

class ScriptTask(StateMachine, GameUi, BaseActivity, SwitchSoul, PassMonopolyMixin, SeasonBossMixin, ActivityShikigamiAssets):
    """
    更新前请先看 ./README.md
    """
    # 随机自动战斗段接入的爬塔类型：门票缓存与场次限制齐全。ap20 每个 entry 独立
    # 20 次计数，段跨 entry 时主循环的切入口逻辑在段内（零输入）不可控；
    # pass_monopoly/season_boss 玩法特殊——这三类 remaining 传 None 静默跳过
    AUTO_SEG_CLIMB_TYPES = ('pass', 'ap', 'boss')
    # 结算奖励框检测：本任务按**画面**分派（见 reward_grid_detect_enabled），
    # 类属性只作默认值。普通爬塔结算走卷轴面板，无标准三行网格，检测恒为空
    # （实测每帧白跑 60~150ms），禁区由 FORBIDDEN_ACTIVITY 的卷轴面板矩形
    # 静态兜住；boss 结算是标准三行网格，故默认关闭、boss 单独开启。
    REWARD_GRID_DETECT = False

    def reward_grid_detect_enabled(self) -> bool:
        """boss 结算页有标准三行奖励网格，其余爬塔结算没有。

        pass/ap 走卷轴面板结算（2026-09-09 起检测关闭，由 FORBIDDEN_ACTIVITY
        的 act_panel 静态兜住）；boss 结算是标准三行网格，2026-09-16 实测
        3 行命中、落点分布与关闭时同为 0% 压奖励格，开启后额外拿到
        reward_grid_appear 这个「仍在奖励页」判据——battle_wait 的
        settlement_click_grid 兜底与自动段的 _settle_appear 都吃它。
        """
        return self.climb_type == 'boss'

    def __init__(self, config, device):
        super().__init__(config, device)
        # check_tickets_enough 缓存: 避免每次循环都 OCR 剩余门票
        # 策略: pass/ap 剩余 >= 50 时 10 分钟 OCR 一次; boss 剩余 >= 10 时 10 分钟 OCR 一次
        # 缓存期内每次调用预估递减 1 (一场战斗 1 张门票)
        # 五倍消耗生效时改为递减 5, 且 OCR 间隔缩短为原来的五分之一
        self._ticket_cache: dict[str, int] = {}
        self._ticket_last_ocr: dict[str, datetime] = {}
        # 当前是否处于五倍消耗状态(游戏内五倍开关已打开且五倍券充足)
        # 仅在 pass/ap 战斗循环中维护, 供 check_tickets_enough 判断递减步长与 OCR 间隔
        self._x5_active: bool = False

    def run(self) -> None:
        self.limit_time: timedelta = self.conf.general_climb.limit_time_v
        #
        for climb_type in self.conf.general_climb.run_sequence_v:
            # 切换御魂(回到 page_main 后进入 page_shikigami_records 再切回 page_main)
            self.ui_get_current_page()
            self.switch_soul()
            # 进入到活动的主页面，不是具体的战斗页面
            self.ui_goto(game.page_climb_act)
            try:
                method_func = getattr(self, f'_run_{climb_type}')
                method_func()
            except LimitCountOut as e:
                self.ui_click(self.I_UI_BACK_YELLOW, stop=self.I_TO_BATTLE_MAIN, interval=1)
            except LimitTimeOut as e:
                break
            finally:
                # 切换下一个爬塔类型
                self.switch_next()

        # 自动段收尾兜底：仍在战斗界面且自动未关时补一次取消（非战斗界面直接返回）
        self.auto_battle_finish_sweep()
        # 返回庭院
        logger.hr("Exit Shikigami", 2)
        self.ui_get_current_page(False)
        self.ui_goto(game.page_main)
        if self.conf.general_climb.active_souls_clean:
            self.set_next_run(task='SoulsTidy', success=False, finish=False, target=datetime.now())
        self.set_next_run(task="ActivityShikigami", success=True)
        raise TaskEnd('ActivityShikigami')

    def _run_pass(self):
        """
            更新前请先看 ./README.md
        """
        logger.hr(f'Start run climb type PASS', 1)
        self.ui_click(self.I_TO_BATTLE_MAIN, stop=self.I_TO_BATTLE_MAIN_2, interval=1)
        self.ui_click(self.I_TO_BATTLE_MAIN_2, stop=self.I_CHECK_BATTLE_MAIN, interval=1)
        self.switch_climb_mode_in_game('pass')
        # 进入 pass 模式, 重置五倍状态, 由首次 check_tickets_enough 的 OCR 重新判断
        self._x5_active = False

        ocr_limit_timer = Timer(1).start()
        click_limit_timer = Timer(4).start()
        while 1:
            self.screenshot()
            self.put_status()
            # --------------------------------------------------------------
            if (self.appear_then_click(self.I_UI_CONFIRM, interval=0.5)
                    or self.appear_then_click(self.I_UI_CONFIRM_SAMLL, interval=0.5)):
                continue
            if self.ui_reward_appear_click():
                continue
            if not ocr_limit_timer.reached():
                continue
            ocr_limit_timer.reset()
            if not self.ocr_appear(self.O_FIRE):
                continue
            #  --------------------------------------------------------------
            self.lock_team(self.conf.general_battle)
            if not self.check_tickets_enough():
                logger.warning(f'No tickets left, wait for next time')
                break
            if self.conf.general_climb.random_sleep:
                random_sleep(probability=0.2)
            if self.start_battle():
                continue

        self.ui_click(self.I_UI_BACK_YELLOW, stop=self.I_TO_BATTLE_MAIN, interval=1)

    def _run_ap(self):
        """
            更新前请先看 ./README.md
        """
        logger.hr(f'Start run climb type AP')
        self.ui_click(self.I_TO_BATTLE_MAIN, stop=self.I_TO_BATTLE_MAIN_2, interval=1)
        self.ui_click(self.I_TO_BATTLE_MAIN_2, stop=self.I_CHECK_BATTLE_MAIN, interval=1)
        self.switch_climb_mode_in_game('ap')
        # 进入 ap 模式, 重置五倍状态, 由首次 check_tickets_enough 的 OCR 重新判断
        self._x5_active = False

        ocr_limit_timer = Timer(1).start()
        while 1:
            self.screenshot()
            self.put_status()
            # --------------------------------------------------------------
            if not ocr_limit_timer.reached():
                continue
            ocr_limit_timer.reset()
            if not self.ocr_appear(self.O_FIRE):
                self.appear_then_click(self.I_CHECK_BATTLE_MAIN, interval=4)
                continue
            #  --------------------------------------------------------------
            self.lock_team(self.conf.general_battle)
            if not self.check_tickets_enough():
                logger.warning(f'No tickets left, wait for next time')
                break
            if self.conf.general_climb.random_sleep:
                random_sleep(probability=0.2)
            if self.start_battle():
                continue

        self.ui_click(self.I_UI_BACK_YELLOW, stop=self.I_TO_BATTLE_MAIN, interval=1)
    def _run_boss(self):
        def start_battle() -> bool:
            """尝试进入并打完一场 boss 战。

            :return: True = 已跑完一场（战斗流程内被上层接管也算）；
                     False = 点满次数仍未进入战斗
            """
            click_times, max_times = 0, random.randint(2, 4)
            # 墙钟兜底：click_times 只在真正点到入场按钮时才递增，若按钮文字一闪
            # 而过（OCR 时有时无）就永远点不上，原来会在里面零输入空转到 device
            # 的 stuck 计时器（60s）抛 GameStuckError。20s 是入场动画的宽松上限
            enter_timer = Timer(20).start()
            while 1:
                self.screenshot()
                if self.is_in_battle(False):
                    break
                if click_times >= max_times or enter_timer.reached():
                    logger.warning(f'Climb {self.climb_type} cannot enter, maybe already end, try next')
                    return False
                if (self.appear_then_click(self.I_UI_CONFIRM_SAMLL, interval=1) or
                        self.appear_then_click(self.I_UI_CONFIRM, interval=1) ):
                    continue
                if self.ocr_appear_click(self.O_FIRE2, interval=2):
                    click_times += 1
                    logger.info(f'Try click fire, remain times[{max_times - click_times}]')
                    continue
            # 运行战斗
            self.run_general_battle(config=self.get_general_battle_conf())
            return True
        """
        更新前请先看 ./README.md
        """
        logger.hr(f'Start run climb type BOSS')
        #logger.hr(f'Start run climb type AP')
        self.ui_clicks([self.I_TO_BATTLE_BOSS],
                       stop=self.I_CHECK_BATTLE_BOSS, interval=1)
        logger.hr(f'Start run climb type BOSS')
        ocr_limit_timer = Timer(1).start()
        # 连续进不去战斗的次数：入口消失（活动结束）时靠它收口，不再无限重试。
        # start_battle 自身最多耗 20s，2 次即 40s 的尝试余量
        enter_fail, max_enter_fail = 0, 2
        # 「既不在战斗、也看不到入场按钮」的空转计时（见 BOSS_IDLE_TIMEOUT_S）。
        # 这是**挂钟**计时器，而本循环会被 run_general_battle 阻塞整场战斗
        # （约 2 分钟）——那期间循环不跑、没有任何 reset，计时器却照走。
        # 所以每个阻塞点返回后必须显式 reset，否则整场战斗的时长会被一次性
        # 记成「界面空转」：战斗结束后的第一帧只要没读到「挑战」就立刻判定
        # 超时、停掉整个爬塔（2026-09-17 oas1 10:08:58 实测，当时刚打完第 11
        # 场，BATTLE1_EXIT_1 退出后回到主页读到「战」即误停）
        idle_timer = Timer(BOSS_IDLE_TIMEOUT_S).start()
        while 1:
            self.screenshot()
            #self.put_status()
            # --------------------------------------------------------------
            if not ocr_limit_timer.reached():
                continue
            ocr_limit_timer.reset()
            # 已在真正的战斗中：点入场按钮到战斗界面出现之间有进场动画，
            # start_battle 那几帧的 is_in_battle 可能判否并误报"进不去"——
            # 但票已经扣了，这场不能丢，直接接战斗流程。安全性与正常入场一致：
            # battle_before 首判就是 is_in_real_battle，已在战斗中会立即返回、
            # 不发任何点击。若这里不查战斗状态，本循环既看不到 FIRE2 又不产生
            # 输入，会把 device 的 stuck 计时器（60s）拖炸成 GameStuckError
            # （2026-09-16 oas1 22:14 实测：整场 boss 战都在空转，日志刷屏
            #  [FIRE2]: No text detected in ROI，60s 后 Wait too long）
            if self.is_in_real_battle(False):
                enter_fail = 0
                self.run_general_battle(config=self.get_general_battle_conf())
                # 阻塞点：战斗整场都在上面这句里面，返回即重置空转计时
                idle_timer.reset()
                continue
            # 严格判据（appear = result == '挑战'）会被 OCR 丢字判否：这块按钮
            # 常被读成「战」「挑」半个词（2026-09-17 实测 24 个可见帧里 10 帧
            # 如此）。丢字帧上按钮明明在屏幕上，按「不存在」处理只会让空转计时
            # 白白累积，所以再问一次宽松判据（ROI 里有没有字）
            if not self.ocr_appear(self.O_FIRE2) and not self.boss_fire_text_present():
                # 点 boss 主页返回；注意 appear_then_click 的返回值不能当"模板不在"
                # 用——interval 没到时它不查模板直接返回 False。所以这里只按
                # 「持续多久没恢复」判超时，不看这一句的返回值
                self.appear_then_click(self.I_CHECK_BATTLE_MAIN, interval=4)
                if idle_timer.reached():
                    logger.warning('boss 界面既不在战斗也看不到入场按钮, stop this climb')
                    break
                continue
            idle_timer.reset()
            #  --------------------------------------------------------------
            #self.lock_team(self.conf.general_battle)
            if not self.check_tickets_enough():
                logger.warning(f'No tickets left, wait for next time')
                break
            if self.conf.general_climb.random_sleep:
                random_sleep(probability=0.2)
            # start_battle 自带 20s 墙钟（enter_timer）负责「点了但进不去」，
            # 它的耗时（正常进场也有 5~10s）不该算进本循环的界面空转，
            # 返回即重置，无论成没成
            entered = start_battle()
            idle_timer.reset()
            if entered:
                enter_fail = 0
                continue
            # 进不去：入口可能已消失（活动结束）。原实现是 `if start_battle(): continue`，
            # 而 start_battle 没有返回值（恒 None）→ 这个分支永远不成立 → 一直重进，
            # 连入口都没了也停不下来。连续几次进不去就交上层 switch_next 换玩法
            enter_fail += 1
            if enter_fail >= max_enter_fail:
                logger.warning(f'Climb {self.climb_type} 连续 {max_enter_fail} 次无法进入, stop this climb')
                break

    def boss_fire_text_present(self) -> bool:
        """boss 主循环的宽松「入场按钮还在」判据：ROI 里读得到任何文本即真。

        ocr_appear 对 O_FIRE2 走的是严格等值（appear = result == '挑战'），
        而这块按钮的 OCR 会丢字——2026-09-17 实测 24 个按钮可见帧里 10 帧
        只读到「战」「挑」半个词。丢字帧上按钮确实在屏幕上，此时若按
        「不存在」处理，空转计时器会平白累积到 BOSS_IDLE_TIMEOUT_S 而误停爬塔。
        这里只问「ROI 里有没有字」，不问那字是不是「挑战」。

        O_FIRE2.ocr_single_line 按 ROI 裁剪后走单行识别，低于置信阈值的
        结果返回空串，因此空串即「没读到可信文本」。
        """
        return bool(self.O_FIRE2.ocr_single_line(self.device.image))

    def _run_ap20(self):
        """
        AP20爬塔模式：7入口遍历，每入口不限次数battle循环
        """
        logger.hr('Start run climb type AP20', 1)

        self.ui_click(self.I_TO_AP20, stop=self.I_CHECK_AP20, interval=1)

        for entry_idx in range(7):
            logger.hr(f'AP20 entry [{entry_idx}/6]', 2)

            entry_img = getattr(self, f'I_AP20_ENTRY_{entry_idx}')
            swipe_count = 0
            max_swipe = 20
            while not self.appear(entry_img):
                if swipe_count >= max_swipe:
                    logger.warning(f'Cannot find AP20 entry {entry_idx}, skip')
                    break
                logger.info(f'Swiping left to find entry {entry_idx} (attempt {swipe_count + 1})')
                self.swipe(self.S_AP20_SWIPE_LEFT, interval=2)
                sleep(2)
                self.screenshot()
                swipe_count += 1
            if swipe_count >= max_swipe:
                continue

            self.appear_then_click(entry_img, interval=2)
            self.ui_click(self.I_TO_AP20_BOSS, stop=self.I_CHECK_AP20_BOSS, interval=1)

            ocr_limit_timer = Timer(1).start()
            fire_retry = 0
            max_fire_retry = 3
            while True:
                self.screenshot()

                if not ocr_limit_timer.reached():
                    continue
                ocr_limit_timer.reset()

                if not self.ocr_appear(self.O_FIRE_AP20):
                    fire_retry += 1
                    if fire_retry >= max_fire_retry:
                        logger.info(f'AP20 entry {entry_idx} complete (no fire detected)')
                        break
                    logger.info(f'Fire not detected, retry {fire_retry}/{max_fire_retry}')
                    self.appear_then_click(self.I_CHECK_AP20_BOSS, interval=2)
                    continue
                fire_retry = 0

                # AP20 每个 entry 独立 20 次, ocr_digit_counter 返回 (current, remain, total)
                # 20/20 表示已用完, remain==0 时切换到下一个 entry
                _, remain, total = self.O_REMAIN_AP20.ocr_digit_counter(self.device.image)
                logger.info(f'AP20 entry {entry_idx} remain: {remain}/{total}')
                if total > 0 and remain <= 0:
                    logger.info(f'AP20 entry {entry_idx} exhausted, back to battle main and re-enter for next entry')
                    self.ui_click(self.I_UI_BACK_YELLOW, stop=self.I_TO_BATTLE_MAIN, interval=3)
                    self.ui_click(self.I_TO_AP20, stop=self.I_CHECK_AP20, interval=3)
                    break

                if self.conf.general_climb.random_sleep:
                    random_sleep(probability=0.2)
                self._start_battle_ap20()

        self.ui_click(self.I_UI_BACK_YELLOW, stop=self.I_TO_BATTLE_MAIN, interval=1)
        logger.info('AP20 all entries completed')

    def _start_battle_ap20(self):
        click_times, max_times = 0, random.randint(2, 4)
        while True:
            self.screenshot()
            if self.is_in_battle(False):
                break
            if click_times >= max_times:
                logger.warning(f'AP20 cannot enter battle, maybe already end')
                return
            if (self.appear_then_click(self.I_UI_CONFIRM_SAMLL, interval=1) or
                    self.appear_then_click(self.I_UI_CONFIRM, interval=1)):
                continue
            if self.ocr_appear_click(self.O_FIRE_AP20, interval=2):
                click_times += 1
                logger.info(f'Try click fire AP20, remain times[{max_times - click_times}]')
                continue
        self.run_general_battle(config=self.get_general_battle_conf())

    def start_battle(self):
        click_times, max_times = 0, random.randint(2, 4)
        while 1:
            self.screenshot()
            if self.is_in_battle(False):
                break
            if click_times >= max_times:
                logger.warning(f'Climb {self.climb_type} cannot enter, maybe already end, try next')
                return
            if (self.appear_then_click(self.I_UI_CONFIRM_SAMLL, interval=1) or
                    self.appear_then_click(self.I_UI_CONFIRM, interval=1) ):
                continue
            if self.ocr_appear_click(self.O_FIRE, interval=2):
                click_times += 1
                logger.info(f'Try click fire, remain times[{max_times - click_times}]')
                continue
        # 运行战斗
        self.run_general_battle(config=self.get_general_battle_conf())

    # ---------------------------------------------------------------- boss 提前退出
    # 战斗中读「当前成绩」OCR，连续 BOSS_EARLY_EXIT_CONFIRM_FRAMES 帧达标就主动点
    # 左上角退出战斗。资产 O_BOSS_SCORE 由 fire/ocr.json 的 boss_score 生成；
    # 未采集时静默禁用（只告警一次），便于先落地逻辑框架、资产后补。
    def boss_early_exit_ready(self) -> bool:
        """boss 提前退出是否生效：玩法是 boss + 开关开 + 阈值有效 + 资产已采。

        每场战斗判一次，任一条不满足都按不启用处理。
        """
        if self.climb_type != 'boss':
            return False
        gb_conf = self.conf.general_battle
        if not gb_conf.enable_boss_early_exit:
            return False
        if gb_conf.boss_early_exit_score <= 0:
            self._boss_early_exit_warn('阈值 <= 0')
            return False
        if getattr(self, 'O_BOSS_SCORE', None) is None:
            self._boss_early_exit_warn('缺少 O_BOSS_SCORE 资产（见 fire/ocr.json 的 boss_score）')
            return False
        return True

    def _boss_early_exit_warn(self, reason: str) -> None:
        """同一任务实例只告警一次，避免每场战斗刷屏。"""
        if getattr(self, '_boss_early_exit_warned', False):
            return
        self._boss_early_exit_warned = True
        logger.warning(f'boss 提前退出已开启但{reason}，本次按不启用处理')

    def boss_early_exit_trigger(self) -> bool:
        """读「当前成绩」，连续 BOSS_EARLY_EXIT_CONFIRM_FRAMES 帧达标才主动退出战斗。

        首帧用调用方已经截好的画面，之后每帧自己再截一次——所以三帧是**紧邻**的
        连续帧（约 1s），不是摊在 4s 节流上的三次。任一帧未达标就立刻放弃、
        退回节流轮询：成绩只增不减，但不达标说明这一帧读到的数字不可信
        （OCR 误读 / 特效遮挡），宁可多打一场也不误退。

        本方法自身不做**节流**，调用方负责配速（battle_wait 用
        BOSS_EARLY_EXIT_SCAN_INTERVAL_S 的计时器压到每 4s 一次）——新增调用点时
        记得一并节流，否则会变成逐帧 OCR。

        :return: True = 本场已发起退出；False = 未达标，继续打
        """
        threshold = self.conf.general_battle.boss_early_exit_score
        for i in range(BOSS_EARLY_EXIT_CONFIRM_FRAMES):
            if i:
                # 第 2、3 帧重新截图；第 1 帧沿用调用方刚截的画面，不重复截
                self.screenshot()
            score = self.O_BOSS_SCORE.ocr(self.device.image)
            # OCR 失败返回 0，一并当作未达标
            if not score or score < threshold:
                return False
        logger.info(f'Boss score {score} >= {threshold} '
                    f'in {BOSS_EARLY_EXIT_CONFIRM_FRAMES} frames, quit battle early')
        self.quit_boss_battle()
        return True

    def quit_boss_battle(self, timeout: float = 20) -> None:
        """战斗中主动退出：点左上角退出 → 确认弹窗走通用 I_EXIT_ENSURE。

        只负责把「退出 + 确认」点完，不处理结算——退出后的胜利页/奖励页由
        调用方 battle_wait 的既有分支接手（本期 boss 无失败页）。确认已发出后
        不再重复点退出：胜利页有约 1s 的入场动画，那期间退出的判据全不命中，
        继续点会落在新画面上。
        超时则告警退出，交 battle_wait 循环与 stuck 兜底。
        """
        timeout_timer = Timer(timeout).start()
        confirmed = False
        while not timeout_timer.reached():
            self.screenshot()
            # 已离开战斗（出了结算页 / 回到挑战主页）就交回上层
            if (self.appear(self.I_WIN) or self.appear(self.I_WIN_2)
                    or self.appear(self.I_FALSE) or self.ocr_appear(self.O_FIRE2)):
                return
            if self.appear_then_click(self.I_EXIT_ENSURE, interval=1):
                confirmed = True
                continue
            if not confirmed and self.appear_then_click(self.I_EXIT, interval=2):
                continue
        logger.warning('Quit boss battle timeout, hand back to battle_wait')

    def battle_wait(self, random_click_swipt_enable: bool) -> bool:
        # 通用战斗结束判断
        self.device.stuck_record_add("BATTLE_STATUS_S")
        self.device.click_record_clear()
        logger.info(f"General Start {self.climb_type} battle process ")
        self.count_map[self.climb_type] = self.current_count
        for btn in (self.C_RANDOM_LEFT, self.C_RANDOM_RIGHT, self.C_RANDOM_TOP, self.C_RANDOM_BOTTOM):
            btn.name = "BATTLE_RANDOM"
        fire_ocr = {'boss': self.O_FIRE2, 'ap20': self.O_FIRE_AP20}.get(self.climb_type, self.O_FIRE)
        ok_cnt, max_retry = 0, 5
        # boss 提前退出：每场判一次是否生效；本场已发起过退出就不再触发
        boss_exit_ready = self.boss_early_exit_ready()
        boss_quit_done = False
        # 成绩 OCR 的节流器（见 BOSS_EARLY_EXIT_SCAN_INTERVAL_S）：start 在本场
        # 开头，所以首轮不识别、第 4s 起每 4s 一次
        boss_score_timer = Timer(BOSS_EARLY_EXIT_SCAN_INTERVAL_S).start()
        while 1:
            sleep(random.uniform(0.5, 1.5))
            self.screenshot()
            # 达到最大重试次数则直接交给上层处理
            if ok_cnt > max_retry:
                break
            # 识别到挑战说明已经退出战斗。boss 提前退出可能直接落回挑战主页
            # （不经结算页），那时 ok_cnt 仍为 0，靠 boss_quit_done 一并放行
            if (ok_cnt > 0 or boss_quit_done) and self.ocr_appear(fire_ocr):
                return True
            # 战斗失败
            if self.appear(self.I_FALSE):
                logger.warning("Battle failed")
                self.ui_click_until_smt_disappear(self.random_reward_click(click_now=False), self.I_FALSE, interval=1.5)
                return False
            # 战斗成功：点赢的画面走安全区域落点 + 结算连点（I_WIN/I_WIN_2 共判），
            # 落点与基类 battle_wait 同一套（全屏挖掉禁点区域与奖励行）
            action_click = weighted_choice(self.reward_click_actions())
            if (self.settlement_click(self.I_WIN, action_click, interval=0.8) or
                    self.settlement_click(self.I_WIN_2, action_click, interval=0.8)):
                continue
            #  出现 "魂" 紫蛇皮 金币：统一点安全区域并按概率连点（与基类同一套落点）；
            #  原先皮肤/金币页随机点 C_RANDOM_TOP/BOTTOM，现由各模板触发 + 奖励框兜底
            if (self.settlement_click(self.I_REWARD, action_click, interval=0.9)
                    or self.settlement_click(self.I_REWARD_PURPLE_SNAKE_SKIN, action_click, interval=1.5)
                    or self.settlement_click(self.I_PURPLE_SNAKE_SKIN, action_click, interval=1.5)
                    or self.settlement_click(self.I_AS_REWARD_GOLD, action_click, interval=1.5)
                    # 卷轴结算页顶部"获得奖励"标题：本期活动结算的主判据
                    # （无标准网格，I_REWARD 系可能全失配；页面点哪都能继续）
                    or self.settlement_click(self.I_A_REWARD, action_click, interval=1.5)
                    # I_REWARD 系模板全部失配时的兜底：只要还检测到奖励框就照样点安全区域
                    or self.settlement_click_grid(action_click, interval=1.5)):
                logger.info('Win battle')
                ok_cnt += 1
                continue
            # 走到这里说明这一帧既没失败、也没出结算页 —— 仍在战斗中，才考虑提前退出。
            # is_in_real_battle 每轮都过（两次模板匹配，便宜）：准备页/过渡动画上读到的
            # 「当前成绩」不可信，而退出点击本身有误触风险，只在真正的战斗过程界面上才发。
            # 真正要节流的是后面的成绩 OCR（几十毫秒级），由 boss_score_timer 压到
            # 每 BOSS_EARLY_EXIT_SCAN_INTERVAL_S 秒一次。两者顺序不能颠倒：短路后
            # 不满足战斗界面时不去碰计时器，否则守卫失败也会吃掉一个识别窗口。
            if ok_cnt == 0 and boss_exit_ready and not boss_quit_done:
                if (self.is_in_real_battle(False)
                        and boss_score_timer.reached_and_reset()
                        and self.boss_early_exit_trigger()):
                    boss_quit_done = True
                    continue
            # 已经不在战斗中了, 且奖励也识别过了, 则随机点击
            # if ok_cnt > 0 and not self.is_in_battle(False):
            #     self.random_reward_click(exclude_click=[self.C_RANDOM_BOTTOM])
            #     ok_cnt += 1
            #     continue
            # 战斗中随机滑动
            if ok_cnt == 0 and random_click_swipt_enable:
                self.random_click_swipt()
        return True

    def activity_shikigami_battle_wait(self, random_click_swipt_enable: bool) -> bool:
        """
        专为活动式神战斗设计的等待方法，只在handle_fire_scene中使用
        :param random_click_swipt_enable: 是否启用随机点击和滑动防封
        :return: 战斗结果 (True表示胜利，False表示失败)
        """
        ## 通用战斗结束判断
        self.device.stuck_record_add("BATTLE_STATUS_S")
        self.device.click_record_clear()
        logger.info(f"Start {self.climb_type} battle process")
        self.count_map[self.climb_type] = self.current_count
        for btn in (self.C_RANDOM_LEFT, self.C_RANDOM_RIGHT, self.C_RANDOM_TOP, self.C_RANDOM_BOTTOM):
            btn.name = "BATTLE_RANDOM"
        # season_boss 战斗结束回到的是修行合训主页, 没有"挑战"字样, 用主页标题「修行合训」判定退出战斗
        fire_ocr = {'boss': self.O_FIRE2, 'ap20': self.O_FIRE_AP20,
                    'season_boss': self.O_SEASON_BOSS_CHECK_MAIN}.get(self.climb_type, self.O_FIRE)
        ok_cnt, max_retry = 0, 5
        while 1:
            sleep(random.uniform(0.5, 1.5))
            self.screenshot()
            # 达到最大重试次数则直接交给上层处理
            if ok_cnt > max_retry:
                break
            # 识别到挑战说明已经退出战斗
            if ok_cnt > 0 and self.ocr_appear(fire_ocr):
                return True
            # 战斗失败
            if self.appear(self.I_FALSE):
                logger.warning("Battle failed")
                self.ui_click_until_smt_disappear(self.random_reward_click(click_now=False), self.I_FALSE, interval=1.5)
                return False
            if self.ui_reward_appear_click():
                continue
            # 战斗成功：点赢的画面走安全区域落点 + 结算连点（I_WIN/I_WIN_2 共判）
            action_click = weighted_choice(self.reward_click_actions())
            if (self.settlement_click(self.I_WIN, action_click, interval=0.8) or
                    self.settlement_click(self.I_WIN_2, action_click, interval=0.8)):
                return True
            # 已经不在战斗中了, 且奖励也识别过了, 则随机点击
            # if ok_cnt > 0 and not self.is_in_battle(False):
            #     self.random_reward_click(exclude_click=[self.C_RANDOM_BOTTOM])
            #     ok_cnt += 1
            #     continue
            # 战斗中随机滑动
            if ok_cnt == 0 and random_click_swipt_enable:
                self.random_click_swipt()
        return True

    def reward_hot(self):
        """爬塔专属热区形状（2026-09-09 用户指示，观察散点图后调整）：
        比通用校准值更窄更右、峰值更低——本期卷轴结算页的点击集中在
        面板下方右缘。只覆盖本任务，通用热区与其他任务不受影响；
        活动轮换后可回退为 None（用通用校准值）。
        """
        return HotShape(hot_x=(850, 380), sigma_left=62, sigma_right=86,
                        peak_ratio=0.70)

    def reward_forbidden(self) -> tuple:
        """活动爬塔结算的常驻禁点区域（默认预设 + 本期活动的奖励卷轴面板）。

        2026-09-09 这期活动结算页有居中的奖励卷轴面板 (272,118,740,438)，
        面板本体是物品与按钮区，结算点击只能落在面板四周与下方；
        活动轮换后面板位置变化时需同步更新 reward_frame.FORBIDDEN_ACTIVITY。
        """
        return FORBIDDEN_ACTIVITY

    def settlement_click_count(self, page_clicks: int) -> int:
        """爬塔结算连击取消，固定单击（2026-09-10 用户指示）。

        爬塔战斗结算画面切换快，连点手势内的追加击容易跨越画面切换点，
        落到已切换的新界面上误触按钮。固定单击（每次手势只有首击、
        无任何追加击）彻底消除追加击误触窗口；事件衰减查表
        （page_clicks）对固定簇长无意义，忽略。
        """
        return 1

    def switch_soul(self):
        conf = self.conf.switch_soul_config
        conf.validate_switch_soul()
        if not getattr(conf, f"enable_switch_{self.climb_type}", False):
            return
        self.ui_goto(page_shikigami_records)
        self.run_switch_soul(getattr(conf, f"{self.climb_type}_group_team"))
        self.ui_goto(page_main)

    def switch_climb_mode_in_game(self, mode: str = 'ap'):
        map_check = {
            'ap': self.I_CLIMB_MODE_AP,
            'pass': self.I_CLIMB_MODE_PASS,
        }
        logger.info(f'Switch climb mode to {mode}')
        self.ui_click(self.I_CLIMB_MODE_SWITCH, stop=map_check[mode], interval=1.9)

    # ---------------------------------------------------------------- 五倍消耗
    def read_x5_ticket(self) -> int:
        """
        读取当前五倍券剩余数量 (O_REMAIN_X5)
        注意: O_REMAIN_X5 资产虽标记为 DigitCounter, 但五倍券界面显示的是纯数字(如 50),
        没有 "X/Y" 分隔符, 用 ocr_digit_counter 会正则匹配失败返回 0(误判券耗尽)。
        因此改用 ocr_digit 直接读纯数字, 与 O_REMAIN_PASS/O_REMAIN_AP 的读法一致。
        :return: 五倍券剩余数量, 读取失败按 0 处理(视为无券)
        """
        self.screenshot()
        return self.O_REMAIN_X5.ocr_digit(self.device.image)

    def is_x5_on(self) -> bool:
        """
        判断游戏内五倍开关是否已开启
        I_ON_X5 存在 -> 已开启; I_OFF_X5 存在 -> 未开启
        """
        return self.appear(self.I_ON_X5)

    def switch_x5_in_game(self, on: bool):
        """
        将游戏内五倍开关切换到目标状态(点击 I_ON_X5/I_OFF_X5 同一按钮切换)
        :param on: True 打开五倍, False 关闭五倍
        """
        target_on = self.is_x5_on()
        if target_on == on:
            return
        logger.info(f'Switch x5 to {"ON" if on else "OFF"}')
        # 开关按钮位置固定, 点击后状态互斥翻转; 用目标态图标作为停止条件
        stop_img = self.I_ON_X5 if on else self.I_OFF_X5
        self.ui_click(self.I_OFF_X5 if on else self.I_ON_X5, stop=stop_img, interval=1.5)

    def update_x5_state(self, remain_ticket: int) -> bool:
        """
        根据配置、五倍券数量、当前门票剩余, 决定并同步游戏内五倍开关状态。
        规则(仅 pass/ap 调用):
        - 未开启五倍消耗配置 -> 保持关闭
        - 五倍券为 0 -> 关闭五倍(用一倍打完剩余门票)
        - 剩余门票 < 5(不足一次五倍) -> 关闭五倍(用一倍打完零头)
        - 否则 -> 打开五倍
        :param remain_ticket: 当前爬塔剩余门票数
        :return: 同步后是否处于五倍状态
        """
        if not self.conf.general_climb.five_times_enable:
            self._x5_active = False
            return False
        x5_ticket = self.read_x5_ticket()
        # 五倍券耗尽或门票零头不足 5, 关闭五倍改用一倍
        want_x5 = x5_ticket > 0 and remain_ticket >= 5
        if not want_x5:
            reason = 'x5 ticket exhausted' if x5_ticket <= 0 else f'remain {remain_ticket} < 5'
            logger.info(f'Disable x5: {reason}')
        self.switch_x5_in_game(want_x5)
        self._x5_active = want_x5
        return want_x5

    def lock_team(self, battle_conf: GeneralBattleConfig):
        """
        根据配置判断当前爬塔类型是否锁定阵容, 并执行锁定或解锁
        """
        enable_preset = getattr(battle_conf, f"enable_{self.climb_type}_preset", False)
        if not enable_preset:
            logger.info(f'Lock {self.climb_type} team')
            self.ui_click(self.I_UNLOCK, stop=self.I_LOCK, interval=1.5)
            return
        logger.info(f'Unlock {self.climb_type} team')
        self.ui_click(self.I_LOCK, stop=self.I_UNLOCK, interval=1.5)

    def check_tickets_enough(self) -> bool:
        """
        判断当前爬塔门票是否足够
        :return: True 可以运行 or False

        缓存策略:
        - pass/ap: 剩余 < 50 时每次都 OCR; >= 50 时 10 分钟 OCR 一次
        - boss:    剩余 < 10 时每次都 OCR; >= 10 时 10 分钟 OCR 一次
        缓存命中时预估递减 1 (一场战斗 1 张门票)

        五倍消耗 (仅 pass/ap):
        - 一场战斗消耗 5 张门票, 故缓存命中时预估递减 5
        - OCR 间隔缩短为五分之一 (10 分钟 -> 2 分钟), 因门票消耗速度是原来的 5 倍
        - 每次真实 OCR 后按剩余门票同步游戏内五倍开关(update_x5_state)
        - 缓存期内一旦预估剩余不足 5, 强制下一轮重新 OCR 以便关闭五倍打零头
        """
        climb = self.climb_type
        # 一场战斗消耗的门票数: 五倍生效为 5, 否则为 1
        step = 5 if self._x5_active else 1
        # 五倍生效时门票消耗快 5 倍, OCR 间隔相应缩短为原来的五分之一
        ocr_interval = timedelta(minutes=2) if self._x5_active else timedelta(minutes=10)

        # 缓存判断
        threshold = {'pass': 50, 'ap': 50, 'boss': 10}.get(climb, 0)
        cached = self._ticket_cache.get(climb)
        last_ocr = self._ticket_last_ocr.get(climb)
        now = datetime.now()
        need_ocr = (
            cached is None
            or cached < threshold
            or last_ocr is None
            or (now - last_ocr) >= ocr_interval
            # 五倍生效且预估剩余不足一次五倍, 需重新 OCR 以关闭五倍打零头
            or (self._x5_active and cached < 5)
        )
        if not need_ocr:
            self._ticket_cache[climb] = max(0, cached - step)
            elapsed = int((now - last_ocr).total_seconds())
            logger.info(
                f'Skip OCR for {climb} tickets, cached={self._ticket_cache[climb]} '
                f'(last OCR {elapsed}s ago, step={step})'
            )
            return self._ticket_cache[climb] > 0

        logger.hr(f'Check {climb} tickets')

        if climb == 'boss':
            if not self.wait_until_appear(self.O_FIRE2, wait_time=3):
                logger.warning(f'Detect fire fail, try reidentify')
                return False
        else:
            if not self.wait_until_appear(self.O_FIRE, wait_time=3):
                logger.warning(f'Detect fire fail, try reidentify')
                return False
        self.screenshot()
        remain_times = 0
        if climb == 'pass':
            remain_times = self.O_REMAIN_PASS.ocr_digit(
                _prepare_image_for_ocr(self.device.image, asset=self.O_REMAIN_PASS))
        if climb == 'ap':
            remain_times = self.O_REMAIN_AP.ocr_digit(
                _prepare_image_for_ocr(self.device.image, asset=self.O_REMAIN_AP))
        if climb == 'boss':
            _, remain_times, _ = self.O_REMAIN_BOSS.ocr_digit_counter(self.device.image)

        self._ticket_cache[climb] = remain_times
        self._ticket_last_ocr[climb] = datetime.now()

        # pass/ap 且门票充足时, 按真实剩余门票同步游戏内五倍开关状态
        if climb in ('pass', 'ap') and remain_times > 0:
            self.update_x5_state(remain_times)

        return remain_times > 0

    def get_general_battle_conf(self) -> tasks.Component.GeneralBattle.config_general_battle.GeneralBattleConfig:
        from tasks.Component.GeneralBattle.config_general_battle import GeneralBattleConfig as gbc
        self.conf.validate_switch_preset()
        gb_conf = self.conf.general_battle
        enable_preset = getattr(gb_conf, f'enable_{self.climb_type}_preset', False)
        group, team = getattr(self.conf.switch_soul_config, f'{self.climb_type}_group_team').split(',')
        return gbc(lock_team_enable=not enable_preset,
                   preset_enable=enable_preset,
                   preset_group=group if enable_preset else 1,
                   preset_team=team if enable_preset else 1,
                   random_click_swipt_enable=getattr(gb_conf, f'enable_{self.climb_type}_anti_detect',
                                                     False),
                   # 自动段配置透传：爬塔自己的 GeneralBattleConfig 只是配置存储，
                   # 组件 gbc 持有同名字段，不透传则组件侧永远是默认关闭
                   auto_battle_enable=gb_conf.auto_battle_enable,
                   auto_segment_count=gb_conf.auto_segment_count,
                   auto_total_count=gb_conf.auto_total_count, )

    # ---------------------------------------------------------------- 随机自动战斗段
    def _auto_seg_remaining(self):
        """自动段规划口径（含本场）：min(场次上限剩余, 门票缓存剩余)。

        - 配置未开或类型不接：None（组件层静默跳过，双保险之一）
        - 五倍消耗期间（一场抵 5 张门票）：场次与门票口径错配会导致超额多打，
          fail-closed 禁用（与 Orochi 五倍券同策略），五倍关闭后自动恢复
        - boss 提前退出同时开启时以段优先：两者不互斥、段照跑；段内强制零输入
          （见文件头 AUTO_BATTLE_* 注释）故不提前退出，段后的手动战斗照常生效
        - 场次口径对齐序言计数时序：调用发生在父类序言 current_count += 1 之前，
          limit - current_count 即"本场 + 未来"剩余场数
        :return: 剩余场次 or None（不启用）
        """
        gb_conf = self.conf.general_battle
        if not gb_conf.auto_battle_enable:
            return None
        if self.climb_type not in self.AUTO_SEG_CLIMB_TYPES:
            return None
        # boss 提前退出与自动段同时开启时以段优先（2026-09-16 用户指定）：段内是
        # 强制零输入，退出点击插不进去，所以段执行期间提前退出不生效；触发点在
        # battle_wait（位于段之外），段结束后的手动战斗照常生效。这里只提示一次，
        # 免得以为提前退出坏了
        if self.boss_early_exit_ready() and not getattr(self, '_boss_early_exit_seg_noted', False):
            self._boss_early_exit_seg_noted = True
            logger.info('boss 自动段与提前退出同时开启：段优先——段内零输入不提前退出，段后手动战斗照常生效')
        # 五倍消耗 fail-closed：告警只在首次触发时打一次，避免每场刷屏
        if self._x5_active:
            if not getattr(self, '_auto_seg_x5_blocked', False):
                logger.warning('五倍消耗期间禁用自动战斗段（场次/门票口径错配），五倍关闭后自动恢复')
                self._auto_seg_x5_blocked = True
            return None
        # 场次上限：limit<=0 表示该类型未启用（与 put_status 的 get_limit 同语义）
        limit = getattr(self.conf.general_climb, f'{self.climb_type}_limit', 0)
        if limit <= 0:
            return None
        remaining = limit - self.current_count
        # 门票缓存（pass/ap/boss 由 check_tickets_enough 维护，三类都参与缓存）。
        # 缓存为 0 时取 0——规划要求 remaining >= M+1，不会开新段。
        # 缓存语义与场次口径一致（均为"本场 + 未来"可用量），段内递减由 count_hook 同步
        ticket = self._ticket_cache.get(self.climb_type)
        if ticket is not None:
            remaining = min(remaining, ticket)
        return remaining

    def run_general_battle(self, config=None, buff=None) -> bool:
        """重写通用战斗：为自动段提供剩余场次口径。

        父类内部按 序言计数 → auto_battle_plan → battle_before → 段分支 → battle_wait
        执行，本重写只注入 remaining，爬塔无御魂式的场后结算（五倍门票扣减走
        check_tickets_enough 的缓存预估，段内由 auto_battle_count_hook 同步递减）。
        """
        remaining = self._auto_seg_remaining()
        return super().run_general_battle(config=config, buff=buff,
                                          remaining_count=remaining)

    def auto_battle_count_hook(self) -> None:
        """段内每完成一场：同步递减门票缓存。

        主循环 check_tickets_enough 的预估递减只发生在"脚本点 fire 前"，
        段内 fire 由游戏自己点，不递减会让缓存虚高、门票耗尽误判有票。
        段被启用时五倍必关（_auto_seg_remaining 已 fail-closed），步长恒 1。
        """
        climb = self.climb_type
        if climb in self._ticket_cache:
            self._ticket_cache[climb] = max(0, self._ticket_cache[climb] - 1)

    def auto_battle_remaining_now(self):
        """段内截断口径：复用规划口径实时计算（段运行期间配置必开、五倍必关，
        与规划时的约束一致）。"""
        return self._auto_seg_remaining()

    # ------------------------------------------------ 随机自动战斗段（爬塔覆盖）
    # 以下四个方法覆盖通用组件 GeneralBattle 的同名实现。通用版按御魂节奏
    # 真机验证正常（2026-09-08），爬塔 2026-09-09 oas1 真机暴露三类问题，
    # 全部由爬塔节奏差异导致，故在本文件覆盖而非改通用代码：
    #   1. 虚开：进场动画期按钮未渲染，通用 OR 语义（按钮不在=自动中）假成功
    #   2. 误判中断：虚开后段内每帧监测发现按钮+×2 反判中断（改为 AND+只认边界）
    #   3. stuck 误杀：开启点击清空 BATTLE_STATUS_S 标志，首场 60s+ 慢战斗
    #      等不到跨场续挂，60s 空窗被 stuck 检查撞上（改为 re-add+纯状态续窗）
    def is_auto_battle_page(self) -> bool:
        """自动战斗页判定（2026-09-09 爬塔真机定稿）：OCR 读到 0-200 纯数字
        且 I_PAPER_TOSTART 消失，两者同时成立才视为自动中（AND）。

        游戏内两态的稳定特征：手动时按钮在、速度控件显示 ×2/X2 倍速字样；
        开启自动后按钮立即消失、控件变为纯数字（0-200）。任一单独成立都
        不可信——按钮不在也可能是进场动画未渲染（虚开事故），OCR 数字也
        可能是 ×2 被误读成 2，必须双证据。
        """
        if self.appear(self.I_PAPER_TOSTART):
            # 按钮在 = 手动（开启后按钮即消失），OCR 数字属误读不作数
            return False
        return auto_ocr_running(self.O_POINT_OR_SPEED.ocr(self.device.image))

    def _auto_start(self, retry: int = 5) -> bool:
        """开启自动战斗（2026-09-09 爬塔真机定稿语义）：

        - 前置：必须在战斗过程界面上（准备页/结算页/过渡动画上按钮与速度
           控件都不可靠，点击有误触风险）
        - 已开启 = OCR 读到 0-200 纯数字 且 按钮消失（AND，见 is_auto_battle_page）
        - 手动（应点击 C_PAPER_TOSTART）= 按钮在，或 OCR 读到 ×2/X2 等
           非运行内容——按钮没匹配到但 OCR 是手动字样时同样要点（进场动画
           期按钮未渲染、模板失配都不能漏点）
        - 两者都无证据（按钮不在 + OCR 读空）= 进场动画/页面过渡，等待重试
        retry 放宽到 5 覆盖约 2s 的进场动画。
        """
        for _ in range(retry + 1):
            self.screenshot()
            if not self.is_in_real_battle(False):
                # 不在战斗过程界面：等待，不点击
                sleep(0.3)
                continue
            if self.is_auto_battle_page():
                # 数字且按钮消失：上次残留的自动状态，视为已开启
                return True
            ocr_text = self.O_POINT_OR_SPEED.ocr(self.device.image)
            if self.appear(self.I_PAPER_TOSTART) or ocr_text:
                # 手动页：按钮在，或 OCR 读到 ×2 等内容（按钮没看到也要点）
                self.click(self.C_PAPER_TOSTART)
                # 等页面切换，节奏拟人
                sleep(random.uniform(0.5, 1.0))
            else:
                # 按钮不在且 OCR 读空：进场动画期控件未渲染，稍等再试
                sleep(0.3)
        logger.warning('Auto battle start failed, fallback to manual battle')
        return False

    def _auto_cancel(self, retry: int = 2) -> bool:
        """取消自动战斗：确认自动页→点击→确认按钮回归。

        "已是手动"的判定看按钮回归（自动中按钮必不在）——is_auto_battle_page
        是 AND 语义，OCR 读空时会假失败，不能反过来当手动证据。
        """
        for _ in range(retry + 1):
            self.screenshot()
            if self.appear(self.I_PAPER_TOSTART):
                logger.info('Auto battle canceled (already manual)')
                return True
            self.click(self.C_PAPER_TOSTART)
            sleep(random.uniform(0.5, 1.0))
            # appear 基于 device.image（上一次截图的旧帧）：点击后必须重新截图刷新，
            # 否则查的是点击前的旧帧，按钮明明已回归却查不到，白白多试一轮
            self.screenshot()
            if self.appear(self.I_PAPER_TOSTART):
                logger.info('Auto battle canceled')
                return True
        logger.warning('Auto battle cancel failed, continue manual flow')
        return False

    def auto_battle_run(self) -> None:
        """自动段主体（爬塔覆盖版）：点开自动→游戏连打 M 场（脚本零输入）→点回手动。

        与通用版（御魂节奏）的差异：开启后 re-add 长战斗标志（开启点击会清空
        它，首场慢战斗 60s 空窗会触发 stuck 误杀重启）、段内不识别自动状态
        只认"结算页出现→回战斗界面"的场次边界、120s 纯状态续窗、单场 180s
        墙钟兜底。返回时页面处于第 M+1 场的战斗过程界面（或准备页），
        battle_wait 继续手动流程。
        """
        seg = self._seg_state()
        # 一次性计划版（2026-09-09）由 _auto_seg_consume 统一推进 plan_idx 与
        # planned，段不再"领取即消耗"，此处不改状态；段长取当前计划段
        # （进入本函数前 _auto_seg_reached 已确认段有效，cur 不会为 None）
        cur = self._auto_seg_current()
        m = cur[1] if cur is not None else seg['seg_len']

        def _settle_appear() -> bool:
            """结算页系模板共判 + 活动卷轴标识 + 奖励框检测兜底。

            模板认的是具体图案（胜利鼓/失败/领奖图标），活动副本的奖励底色
            或结算动画中间帧可能全部失配——失配时段内看不到"结算页出现"，
            游戏自动翻进下一场后不会记次，段会卡死在等待结算。
            本期活动普通结算走卷轴面板（无标准三行网格），reward_grid_appear
            在其中恒 False——I_A_REWARD（卷轴顶部"获得奖励"标题标识，2026-09-09
            新增资产）补上这个判据缺口，与奖励内容无关；boss 结算有标准网格，
            该判据由 reward_grid_detect_enabled 单独开启，两者互补。
            """
            return (self.win_appear(threshold=0.8)
                    or self.appear(self.I_FALSE, threshold=0.8)
                    or self.appear(self.I_REWARD, threshold=0.6)
                    or self.appear(self.I_REWARD_GOLD, threshold=0.8)
                    or self.appear(self.I_A_REWARD)
                    or self.reward_grid_appear())

        # ---- 1. 开启自动 ----
        if not self._auto_start():
            # 开启失败回退手动，本场照常打，不消耗 T
            return
        # _auto_start 的点击会触发 stuck_record_clear（record 清空、计时重置），
        # 此处必须 re-add：否则首个 60s 窗口内 detect_record 为空，stuck 检查
        # 不按长战斗放行，单场超 60s 的慢战斗会被误杀重启（oas1 爬塔事故：
        # 全屏技能动画打了 60s，GameStuckError 白重启了游戏）
        self.device.stuck_record_add('BATTLE_STATUS_S')
        # 第 1 场的 T 消耗（其 current_count 已由 run_general_battle 序言计入）
        seg['total_left'] -= 1
        seg_done = 1
        waiting_settle = False   # 是否已见到本场的结算页
        unknown_frames = 0       # 结算后连续未知页面帧数（既非战斗/准备也非结算）
        stuck_refresh_ts = time()   # 上次 stuck 续窗时刻
        battle_deadline = time() + AUTO_BATTLE_BATTLE_TIMEOUT_S  # 单场墙钟
        # ---- 2. 段内循环：零输入，只识别场次边界 ----
        # 不再每帧识别自动状态（POINT_OR_SPEED/按钮）——开启已在 _auto_start
        # 确认，段内只认"结算页出现→回到战斗界面"的边界。中途被取消/虚开的
        # 出口交给 unknown 超时：手动模式下结算页会停住等玩家点击，30 帧不
        # 翻转即退出段，battle_wait 接管手动流程（含点结算）
        while seg_done < m:
            self.screenshot()
            now = time()
            if not waiting_settle:
                # stuck 续窗：段内不能产生输入（零输入约束），改纯状态操作——
                # clear 重置 60s/300s 计时器后立即补回长战斗标记。段内卡死
                # 保护由 unknown 超时与单场墙钟负责，stuck 只防段外
                if now - stuck_refresh_ts >= AUTO_BATTLE_STUCK_REFRESH_S:
                    self.device.stuck_record_clear()
                    self.device.stuck_record_add('BATTLE_STATUS_S')
                    stuck_refresh_ts = now
                # 单场墙钟超时：等不到结算页视为流转异常（游戏卡死/自动失效），
                # 退出段交 battle_wait 的既有卡死处理兜底
                if now > battle_deadline:
                    logger.warning(f'Auto battle no settle in '
                                   f'{AUTO_BATTLE_BATTLE_TIMEOUT_S}s at {seg_done}/{m}')
                    return
                # 场次边界第一步：结算页出现
                if _settle_appear():
                    waiting_settle = True
                    unknown_frames = 0
            else:
                # 场次边界第二步：结算页过后回到战斗/准备界面 = 跨过一场
                if self.is_in_real_battle(False) or self.is_in_prepare(False):
                    unknown_frames = 0
                    self.auto_battle_count_step()
                    seg_done += 1
                    waiting_settle = False
                    # 新一场开始：重置续窗与单场墙钟。
                    # stuck 续窗本身已由 auto_battle_count_step 完成（clear+re-add）——
                    # 原先这里只推 stuck_refresh_ts，而单场约 16.5s 永远够不到 120s 的
                    # AUTO_BATTLE_STUCK_REFRESH_S 阈值，上面那段续窗一次都跑不到，
                    # 段起点点击后满 300s 必被 GameStuckError 误判卡死（2026-09-11 oas2 事故）
                    stuck_refresh_ts = now
                    battle_deadline = now + AUTO_BATTLE_BATTLE_TIMEOUT_S
                    # 计数偏差截断：剩余场数不够"段剩余+1 场取消"
                    remaining_now = self.auto_battle_remaining_now()
                    if remaining_now is not None and remaining_now <= m - seg_done:
                        logger.warning(f'Auto battle segment truncated at {seg_done}/{m}')
                        break
                elif _settle_appear():
                    # 结算页系模板还在（动画/翻页中）属于正常等待，不算未知界面
                    unknown_frames = 0
                else:
                    # 页面流失去未知界面（异常弹窗/跳转/手动模式结算停住等点击）：
                    # 没有退出条件会段内死循环，连续超过 30 帧告警退出
                    unknown_frames += 1
                    if unknown_frames > 30:
                        logger.warning(f'Auto battle lost in unknown ui at {seg_done}/{m}')
                        return
        # ---- 3. 段尾取消（按钮只在战斗过程界面出现，先等界面再取消） ----
        if self._auto_wait_battle_ui():
            self._auto_cancel()
        else:
            logger.warning('Auto battle cancel skipped: not in battle ui')

    def random_reward_click(self, exclude_click: list = None, click_now: bool = True) -> RuleClick:
        """
        随机点击
        :param exclude_click: 排除的点击位置
        :param click_now: 是否立即点击
        :return: 随机的点击位置
        """
        options = [self.C_RANDOM_TOP, self.C_RANDOM_BOTTOM]
        if exclude_click:
            options = [option for option in options if option not in exclude_click]
        target = random.choice(options)
        if click_now:
            self.click(target, interval=1.8)
        return target
if __name__ == "__main__":
    from module.config.config import Config
    from module.device.device import Device
    c = Config('OAS1')
    d = Device(c)
    t = ScriptTask(c, d)
    t.screenshot()
    t.run()