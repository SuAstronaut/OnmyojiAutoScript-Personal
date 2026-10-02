# This Python file uses the following encoding: utf-8
# @author runhey
# github https://github.com/runhey
from datetime import timedelta, datetime

from module.server.i18n import I18n
from tasks.BondlingFairyland.config import BondlingMode
from tasks.Component.GeneralBattle.general_battle import GeneralBattle
from tasks.Component.GeneralBattle.reward_frame import (
    weighted_choice, AVOID_WIN_TEAM2)
from tasks.BondlingFairyland.assets import BondlingFairylandAssets
from tasks.BondlingFairyland.config_battle import BattleConfig

from module.logger import logger




class BondlingBattle(GeneralBattle, BondlingFairylandAssets):

    def reward_avoid(self) -> tuple:
        """契灵是两人组队，胜利画面上多出一块队友战绩框，落点回避它。"""
        return AVOID_WIN_TEAM2

    def run_battle(self, battle_config: BattleConfig, limit_count: int = None) -> bool:
        """
        :return: 如果结契成功返回True，否则返回False
        """
        logger.hr("General battle start", 2)
        self.current_count += 1
        logger.info(f'Current tasks: {I18n.trans_zh_cn(self.config.task.command)}')
        logger.info(f'Current count: {self.current_count} / {limit_count}')

        task_run_time = datetime.now() - self.start_time
        # 格式化时间，只保留整数部分的秒
        task_run_time_seconds = timedelta(seconds=int(task_run_time.total_seconds()))
        logger.info(f'Current times: {task_run_time_seconds} / {self.limit_time}')

        if self.check_load():
            # 首先要判断进入战斗的界面
            self.green_mark(battle_config.green_enable, battle_config.green_mark)

        # 进入战斗过程
        return self.catch_battle_wait(battle_config.random_click_swipt_enable)


    def check_load(self) -> bool:
        """
        检查战斗时候的加载动画
        如何是还在加载种，有那个要准备的按钮，就返回True
        如果已经进入战斗了，就返回False
        :return:
        """
        while 1:
            self.screenshot()
            if self.appear(self.I_BUFF):
                return True
            if self.appear(self.I_EXIT):
                return False

    def catch_battle_wait(self, random_click_swipt_enable: bool) -> bool:
        """
        重写一个 战斗等待
        :return: 如果捕获成功返回True，否则返回False
        """
        self.device.stuck_record_add('BATTLE_STATUS_S')
        self.device.click_record_clear()
        # 有时候 只会点击 获得奖励和开始战斗
        # 战斗过程 随机点击和滑动 防封
        logger.info("Start battle process")
        win: bool = False
        bondling_mode = self.config.bondling_fairyland.bondling_config.bondling_mode
        cap_again = bondling_mode in [BondlingMode.MODE3, BondlingMode.MODE4]
        cap_cnt, max_cap = 0, 10
        while 1:
            # 捕获次数超过最大次数限制, 则不再进行捕获
            if cap_cnt >= max_cap:
                cap_again = False
            self.screenshot()
            # 如果捕获成功
            if self.appear_then_click(self.I_CAP_SUCCESS, action=self.C_CAP_SUCCESS,  interval=1):
                win = True
            # 连续结契捕获失败
            if self.appear_then_click(self.I_CAP_FAILURE, action=self.C_CAP_SUCCESS, interval=1):
                win = False
            # 非连续结契且单次抓捕失败
            if not cap_again and self.appear_then_click(self.I_BATTLE_FAIL_ABANDON, interval=1):
                win = False
            # 连续结契则继续结契
            if cap_again and self.appear_then_click(self.I_CAP_AGAIN, interval=1):
                self.device.click_record_clear()  # 需要10次结契因此清空点击记录
                cap_cnt += 1
                continue
            # 如果领奖励
            if self.appear(self.I_REWARD, threshold=0.6):
                break
            # 胜利画面：I_WIN/I_WIN_2 共判，点安全区域（热区+回避区+共享CD）；
            # 契灵是两人组队，胜利画面有队友战绩框，旧式「点胜利横幅位置」改为安全落点
            if self.win_appear(threshold=0.6):
                action_click = weighted_choice(self.reward_click_actions())
                self.settlement_click(self.I_WIN, action_click, threshold=0.6, interval=0.5) or \
                    self.settlement_click(self.I_WIN_2, action_click, threshold=0.6, interval=0.5)
                continue
            if self.appear_then_click(self.I_BATTLE_SUCCESS, threshold=0.6, interval=1):
                continue
            if self.appear_then_click(self.I_BATTLE_FAIL, threshold=0.6, interval=1):
                continue
            # 如果开启战斗过程随机滑动
            if random_click_swipt_enable:
                self.random_click_swipt()

        # 确定获得奖励 无论是胜利还是失败
        if win:
            logger.info("Catch success")
        else:
            logger.info("Catch failure")
        while 1:
            self.screenshot()
            # 如果出现领奖励：全屏减去常驻禁点区域与检测出的奖励行（与基类同一套安全落点）；
            # 结算场景按概率连点（双击/三击），见 settlement_click
            action_click = weighted_choice(self.reward_click_actions())
            if (self.settlement_click(self.I_REWARD, action_click, interval=1.5) or
                    # I_REWARD 失配时的兜底：只要还检测到奖励框就照样点安全区域
                    self.settlement_click_grid(action_click, interval=1.5)):
                continue
            # 胜利画面兜底：I_WIN/I_WIN_2 共判，点安全区域
            if self.win_appear(threshold=0.6):
                self.settlement_click(self.I_WIN, action_click, threshold=0.6, interval=0.5) or \
                    self.settlement_click(self.I_WIN_2, action_click, threshold=0.6, interval=0.5)
                continue
            if self.appear_then_click(self.I_BATTLE_SUCCESS, threshold=0.6, interval=1):
                continue
            if self.appear_then_click(self.I_BATTLE_FAIL_ABANDON, interval=1):
                continue
            if self.appear_then_click(self.I_BATTLE_FAIL, threshold=0.6, interval=1):
                continue
            if not self.appear(self.I_REWARD) and not self.reward_grid_appear():
                break
        logger.info("Get reward")
        return win
