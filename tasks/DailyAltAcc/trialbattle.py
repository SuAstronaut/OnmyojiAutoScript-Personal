# This Python file uses the following encoding: utf-8
import time
from module.base.timer import Timer
from module.logger import logger
from tasks.GameUi.page import page_main, page_summon
from tasks.DailyAltAcc.utils import DailyAltAccBase
from tasks.Plotline.assets import PlotlineAssets
from tasks.Component.GeneralBattle.assets import GeneralBattleAssets


class Trialbattle(DailyAltAccBase):
    def run_trialbattle(self):
        """
        试炼战斗主循环：导航到召唤页面，循环执行 fire -> battle_wait，
        直到出现 I_TRIALBATTLE_STOP_FLAG 停止
        """
        self.screenshot()
        if self.ui_get_current_page() != page_main:
            self.ui_goto(page_main)
        self.ui_goto(page_summon)
        # 任务运行内 I_TO_TRIALBATTLE 首次识别需画面稳定确认，点击过后置位不再等待
        self._trial_to_stable_clicked = False
        start_time = time.time()
        while time.time()-start_time < 5:
            self.screenshot()
            if self.appear(self.I_TRIALBATTLE_FIRE):
                start_time = time.time()
                break
            if self.appear_then_click(self.I_TO_TRIALBATTLE_2,interval=1):
                start_time = time.time()
                continue
            if self.appear(self.I_TO_TRIALBATTLE, interval=1) and self._trial_to_stable_wait():
                # 首次点击前已确认连续三帧可见，排除过渡页闪现误触；点击后重置超时锚点
                self.appear_then_click(self.I_TO_TRIALBATTLE, action=self.C_TO_TRIALBATTLE)
                self._trial_to_stable_clicked = True
                start_time = time.time()
                continue
            if self.appear_then_click(self.I_TRIALBATTLE_START_2,action=self.C_TRIALBATTLE_START_2,interval=1):
                            start_time = time.time()
                            logger.info('试炼战斗: 前往集结')
                            continue
            if self.appear_then_click(self.I_TRIALBATTLE_START,action=self.C_TRIALBATTLE_START,interval=1):
                start_time = time.time()
                logger.info('试炼战斗: 前往集结')
                continue
        if time.time()-start_time >= 5:
            return 
        while time.time()-start_time < 5:
            self.screenshot()
            if self.appear(self.I_TRIALBATTLE_END):
                start_time = time.time()
                logger.info('试炼战斗: 检测到结束标志')
                break
            if self.appear(self.I_TRIALBATTLE_FIRE):
                logger.info('试炼战斗: 检测到FIRE')
                if self.trial_fire():
                    self.trial_battle_wait()
                start_time = time.time()
                continue
            if self.appear_then_click(self.I_TO_TRIALBATTLE_2,interval=1):
                start_time = time.time()
                continue
            if self.appear(self.I_TO_TRIALBATTLE, interval=1) and self._trial_to_stable_wait():
                # 首次点击前已确认连续三帧可见，排除过渡页闪现误触；点击后重置超时锚点
                self.appear_then_click(self.I_TO_TRIALBATTLE, action=self.C_TO_TRIALBATTLE)
                self._trial_to_stable_clicked = True
                start_time = time.time()
                continue

        while time.time()-start_time < 3:
            self.screenshot() 
            if self.appear_then_click(PlotlineAssets.I_PAGE_CLICK_ANY, interval=1):
                continue
            if self.appear_then_click(self.I_TRIALBATTLE_BACK_RED,interval=2):
                start_time = time.time()
                continue
            if self.appear_then_click(self.I_BACK_Y,interval=2):
                start_time = time.time()
                logger.info('试炼战斗: 退出集结')
                continue
        self.screenshot()
        if self.ui_get_current_page() != page_main:
            self.ui_goto(page_main)

    def _trial_to_stable_wait(self) -> bool:
        """
        I_TO_TRIALBATTLE 首次识别的画面稳定确认：以约 1 秒间隔连截 3 帧，
        全部连续可见才放行点击。点击已发生过一次（_trial_to_stable_clicked）后
        直接放行，不再等待——误触只发生在任务内第一次经过过渡页时。
        """
        # 已点击过一次，后续识别直接放行
        if self._trial_to_stable_clicked:
            return True
        stable_count = 0
        while stable_count < 3:
            # 每次截图前等待约 1 秒，观察页面是否停留
            time.sleep(1)
            self.screenshot()
            if self.appear(self.I_TO_TRIALBATTLE):
                stable_count += 1
            else:
                # 中途消失说明是过渡页闪现，判为不稳定
                logger.info('试炼战斗: I_TO_TRIALBATTLE 稳定确认失败，疑似过渡页')
                return False
        logger.info('试炼战斗: I_TO_TRIALBATTLE 连续三帧可见，画面稳定')
        return True

    def trial_fire(self):
        """开战逻辑"""
        start_time = time.time()
        while time.time()-start_time < 5:
            self.screenshot()
            if self.appear(self.I_TRIALBATTLE_END, interval=1):
                return False
            if self.appear(GeneralBattleAssets.I_BATTLE_INFO, interval=1):
                return True
            if self.appear_then_click(self.I_TRIALBATTLE_FIRE,interval=1):
                start_time = time.time()
                continue
        return False

    def trial_battle_wait(self):
        """
        等待战斗结束：时刻检查 I_PAGE_CLICK_ANY 出现就点击，
        胜利出现则点击消失或 fire 重新出现表示战斗结束
        """
        self.device.stuck_record_add('BATTLE_STATUS_S')
        self.device.click_record_clear()
        logger.info("试炼战斗: Start battle process")
        click_timer = Timer(10).start()         
        while 1:
            self.screenshot()
            self.device.click_record_clear()
            
            # 时刻检查 I_PAGE_CLICK_ANY，出现就点击
            if self.appear_then_click(PlotlineAssets.I_PAGE_CLICK_ANY, interval=1):
                continue
            # 胜利出现，点击消失
            if self.appear(GeneralBattleAssets.I_WIN, threshold=0.8) or self.appear(GeneralBattleAssets.I_DE_WIN):
                logger.info("试炼战斗: Battle result is win")
                if self.appear(GeneralBattleAssets.I_DE_WIN):
                    self.ui_click_until_disappear(GeneralBattleAssets.I_DE_WIN)
                else:
                    self.ui_click_until_disappear(GeneralBattleAssets.I_WIN)
                break
            # fire 重新出现表示战斗结束
            if self.appear(self.I_TRIALBATTLE_FIRE):
                logger.info("试炼战斗: Fire button reappeared, battle ended")
                break
            if  click_timer.reached() or "伤害" in self.O_CLICK_SKILL.ocr(self.device.image):
                click_timer.reset()
                self.click(PlotlineAssets.C_CLICK_RANDOM_3)
                time.sleep(0.5)
                self.click(PlotlineAssets.C_CLICK_RANDOM_2)
                time.sleep(0.5)
                self.click(PlotlineAssets.C_CLICK_RANDOM_1)
                time.sleep(0.5)


if __name__ == "__main__":
    from module.config.config import Config
    from module.device.device import Device
    c = Config('oas2')
    d = Device(c)
    self = Trialbattle(c, d)
    self.screenshot()
    self.run_trialbattle()
