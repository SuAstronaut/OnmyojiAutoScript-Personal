# This Python file uses the following encoding: utf-8
"""
ActivityShikigami 大富翁玩法（pass_monopoly climb_type）的逻辑模块。

包含：
- PassMonopolyScene 枚举：场景识别结果
- PassMonopolyMixin：被 ScriptTask 多继承，提供 _run_pass_monopoly 与场景处理器
"""
import time as _time
from enum import Enum

from module.logger import logger

from tasks.base_task import BaseTask
from tasks.ActivityShikigami.assets import ActivityShikigamiAssets


class PassMonopolyScene(Enum):
    MAIN = 0
    SELECT_DICE = 1
    SHOP = 2
    FIRE = 3
    QA = 4
    ROLL = 5
    REWARD = 6
    SKIP = 7
    UNKNOWN = 99


class PassMonopolyMixin(BaseTask):
    """大富翁玩法 mixin，被 ScriptTask 多继承。

    依赖 ScriptTask 链上其他 mixin 提供的方法（screenshot/appear/click/
    ui_click/ui_reward_appear_click/run_general_battle/get_general_battle_conf
    /put_status/battle_wait/activity_shikigami_battle_wait 等）。
    """

    def __init__(self, config, device):
        super().__init__(config, device)
        # selected_dice 由 handle_main_scene 写入,handle_select_dice_scene 读取
        self.selected_dice = None

    def _run_pass_monopoly(self):
        """
        大富翁玩法主循环。原 _run_pass_1（参见 git history）。
        逻辑零变化，仅做命名替换。
        LimitTimeOut/LimitCountOut 使用 lazy import 避免与 script_task 循环依赖。
        """
        from tasks.ActivityShikigami.script_task import LimitTimeOut, LimitCountOut

        logger.hr(f'Start run climb type pass_monopoly')
        self.screenshot()
        # 进入战斗主界面
        self.appear_then_click(self.I_TO_MONOPOLY_MAIN, interval=1)
        current_scene = self.get_current_scene()
        while 1:
            self.screenshot()
            if current_scene == PassMonopolyScene.UNKNOWN:
                logger.warning("未能在10秒内识别到有效场景，返回主页面重新尝试")
                if self.appear(self.I_TO_MONOPOLY_MAIN):
                    self.ui_click(self.I_TO_MONOPOLY_MAIN, stop=self.I_CHECK_MONOPOLY_MAIN, interval=1)
                    continue
            current_scene = self.get_current_scene()
            if current_scene == PassMonopolyScene.MAIN and self.O_REMAIN_PASS.ocr_digit(self.device.image) <= 0:
                return
            self.handle_scene(current_scene)

            # 检查任务限制
            try:
                self.put_status()
            except (LimitTimeOut, LimitCountOut):
                break

    def get_current_scene(self) -> PassMonopolyScene:
        """获取当前场景，轮询最多10秒后如果仍未识别到有效场景则返回 UNKNOWN"""
        start_time = _time.time()

        while _time.time() - start_time < 10:
            self.screenshot()
            if self.appear(self.I_SKIP_BUTTON, interval=1):
                return PassMonopolyScene.SKIP
            if self.appear(self.I_MONOPOLY_PAGE_SHOP, interval=1):
                return PassMonopolyScene.SHOP
            if self.appear(self.I_MONOPOLY_PAGE_SELECT_DICE, interval=1):
                return PassMonopolyScene.SELECT_DICE
            if self.appear(self.I_MONOPOLY_PAGE_FIRE, interval=1) or self.appear(self.I_MONOPOLY_PAGE_FIRE_DICE, interval=1):
                return PassMonopolyScene.FIRE
            if self.appear(self.I_MONOPOLY_PAGE_QA, interval=1):
                return PassMonopolyScene.QA
            if self.appear(self.I_MONOPOLY_PAGE_ROLL, interval=1):
                return PassMonopolyScene.ROLL
            if self.appear(self.I_CHECK_MONOPOLY_MAIN, interval=3):
                return PassMonopolyScene.MAIN
            _time.sleep(0.1)

        return PassMonopolyScene.UNKNOWN

    def handle_scene(self, scene: PassMonopolyScene) -> None:
        """根据当前场景执行对应的操作"""
        scene_handlers = {
            PassMonopolyScene.MAIN: self.handle_main_scene,
            PassMonopolyScene.SELECT_DICE: self.handle_select_dice_scene,
            PassMonopolyScene.SHOP: self.handle_shop_scene,
            PassMonopolyScene.FIRE: self.handle_fire_scene,
            PassMonopolyScene.QA: self.handle_qa_scene,
            PassMonopolyScene.ROLL: self.handle_roll_scene,
            PassMonopolyScene.REWARD: self.handle_reward_scene,
            PassMonopolyScene.SKIP: self.handle_skip_scene,
            PassMonopolyScene.UNKNOWN: self.handle_unknown_scene,
        }
        handler = scene_handlers.get(scene, self.handle_unknown_scene)
        handler()

    def handle_main_scene(self) -> None:
        """处理大富翁主界面场景"""
        logger.info("当前在主界面场景")
        self.screenshot()
        if (self.appear_then_click(self.I_UI_CONFIRM, interval=1)
                or self.appear_then_click(self.I_UI_CONFIRM_SAMLL, interval=1)):
            logger.info("点击确认按钮")
            return
        current_image = self.device.image
        # 检测 monopoly_dice_1/2/3 是否分别出现两次
        dice_1_count = len(self.I_MONOPOLY_DICE_1_ICON.match_all_any(current_image))
        dice_2_count = len(self.I_MONOPOLY_DICE_2_ICON.match_all_any(current_image))
        dice_3_count = len(self.I_MONOPOLY_DICE_3_ICON.match_all_any(current_image))

        # 如果任意一个骰子出现两次，则点击 fire_btn_dice
        pass_2_count = self.O_MONOPOLY_REMAIN_PASS.ocr_digit(current_image)
        if pass_2_count > 0 and (dice_1_count >= 2 or dice_2_count >= 2 or dice_3_count >= 2):
            if dice_1_count >= 2:
                self.selected_dice = self.C_MONOPOLY_DICE_1_CLICK
            elif dice_2_count >= 2:
                self.selected_dice = self.C_MONOPOLY_DICE_2_CLICK
            elif dice_3_count >= 2:
                self.selected_dice = self.C_MONOPOLY_DICE_3_CLICK

            # 检查当前是否有 fire_btn_dice，如果没有则通过 fire_switch 切换
            if not self.appear(self.I_MONOPOLY_FIRE_BTN_DICE):
                self.click(self.I_MONOPOLY_FIRE_SWITCH)
                self.screenshot()

            self.appear_then_click(self.I_MONOPOLY_FIRE_BTN_DICE, interval=2)
        else:
            # 如果没有骰子出现两次，则切换到 fire_btn_normal 模式
            if not self.appear(self.I_MONOPOLY_FIRE_BTN_NORMAL):
                self.click(self.I_MONOPOLY_FIRE_SWITCH)
                self.screenshot()
            self.appear_then_click(self.I_MONOPOLY_FIRE_BTN_NORMAL, interval=2)

    def handle_select_dice_scene(self) -> None:
        """处理选择骰子场景"""
        logger.info(f"当前在选择骰子场景，选中的骰子: {self.selected_dice.name if self.selected_dice else 'None'}")
        if self.selected_dice:
            self.click(self.selected_dice)
        else:
            self.click(self.I_MONOPOLY_DICE_3_ICON)
            logger.info("没有指定骰子，执行默认操作")

    def handle_shop_scene(self) -> None:
        """处理商店场景"""
        logger.info("当前在商店场景")
        self.screenshot()
        current_image = self.device.image
        buy_enables = self.I_MONOPOLY_BUY_ENABLE.match_all_any(current_image)
        logger.info(f"buy_enables: {buy_enables}")
        if len(buy_enables) == 1:
            logger.info("buy_enables ==1")
            self.I_MONOPOLY_BUY_BTN.roi_back = (buy_enables[0][1] - 40, buy_enables[0][2] - 140, 230, 120)
            buy_enable = 1
        else:
            buy_enable = 0
        start_time = _time.time()
        while _time.time() - start_time < 5:
            self.screenshot()
            if buy_enable == 0 and self.appear_then_click(self.I_RED_EXIT, interval=2.5):
                break
            if buy_enable == 1 and self.appear_then_click(self.I_MONOPOLY_BUY_BTN, interval=2.5):
                start_time = _time.time()
                continue
            if (self.appear_then_click(self.I_UI_CONFIRM, interval=1)
                    or self.appear_then_click(self.I_UI_CONFIRM_SAMLL, interval=1)):
                logger.info("点击确认按钮")
                break
        logger.info("商店场景处理完成")

    def handle_fire_scene(self) -> None:
        """处理战斗开始场景（出现火图标）"""
        logger.info("当前在战斗开始场景")
        self.screenshot()

        if self.appear_then_click(self.I_MONOPOLY_PAGE_FIRE, interval=2.5):
            # 临时替换 battle_wait 方法以使用自定义的战斗等待逻辑
            original_battle_wait = self.battle_wait
            self.battle_wait = self.activity_shikigami_battle_wait
            try:
                self.run_general_battle(config=self.get_general_battle_conf())
            finally:
                self.battle_wait = original_battle_wait
            return
        if self.appear(self.I_MONOPOLY_PAGE_FIRE_DICE):
            if self.appear_then_click(self.I_UI_BACK_YELLOW, interval=1):
                logger.info("点击确认按钮")
            return

    def handle_qa_scene(self) -> None:
        """处理问答场景"""
        logger.info("当前在问答场景")
        self.screenshot()
        if self.appear_then_click(self.I_MONOPOLY_PAGE_QA, action=self.C_MONOPOLY_QA_ANSWER_1, interval=2.5):
            pass

    def handle_roll_scene(self) -> None:
        """处理投掷场景"""
        logger.info("当前在投掷场景")
        self.screenshot()
        if self.appear_then_click(self.I_MONOPOLY_PAGE_ROLL, interval=2.5):
            pass

    def handle_reward_scene(self) -> None:
        """处理奖励场景"""
        logger.info("当前在奖励场景")
        self.ui_reward_appear_click()

    def handle_skip_scene(self) -> None:
        """处理跳过场景"""
        logger.info("当前在跳过场景")
        self.screenshot()
        self.appear_then_click(self.I_SKIP_BUTTON, interval=2)
        logger.info("跳过场景处理完成，准备返回主场景")

    def handle_unknown_scene(self) -> None:
        """处理未知场景"""
        logger.info("当前场景未知")
        self.screenshot()
        # 回退到主界面（注意：使用 I_TO_BATTLE_MAIN 是因为 unknown 时希望回到通用 ActivityShikigami 主页，
        # 而不是大富翁专属的 I_TO_MONOPOLY_MAIN——保留原行为）
        self.ui_click(self.I_UI_BACK_YELLOW, stop=self.I_TO_BATTLE_MAIN, interval=1)
