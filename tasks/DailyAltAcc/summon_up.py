# This Python file uses the following encoding: utf-8
import time
from module.logger import logger
from tasks.GameUi.page import page_main
from tasks.DailyAltAcc.utils import DailyAltAccBase


class SummonUp(DailyAltAccBase):
    def run_summon_up(self):
        """
        召唤子任务主函数
        """
        def select_pool():
            start_time = time.time()
            while time.time()-start_time < 5:
                self.screenshot()
                if self.appear(self.I_SUMMON_UP_TO_SHARE, interval=1):
                    start_time = time.time()
                    return
                if self.appear_then_click(self.I_SUMMON_UP_ENSURE_POOL_2, interval=1):
                    start_time = time.time()
                    continue
                if self.appear_then_click(self.I_SUMMON_UP_ENSURE_POOL, interval=1):
                    start_time = time.time()
                    continue

        def share_summon_up():
            start_time = time.time()
            share_flag = False
            while time.time()-start_time < 5:
                self.screenshot()
                self.device.click_record_clear()
                if not share_flag and self.appear(self.I_SUMMON_UP_WECHAT_SHARE_2):
                    logger.info("分享成功")
                    share_flag=True
                    start_time = time.time()
                    continue
                if share_flag and self.appear_rgb(self.I_SUMMON_UP_TO_GIFT):
                    return True
                if share_flag and self.appear_then_click(self.I_SUMMON_UP_BACK_SHARE, interval=2):
                    start_time = time.time()
                    continue
                if self.appear_then_click(self.I_SUMMON_UP_BACK_RED, interval=1):
                    start_time = time.time()
                    continue 
                if not share_flag and self.appear_then_click(self.I_SUMMON_UP_WECHAT_SHARE, interval=3):
                    time.sleep(2)
                    start_time = time.time()
                    continue
                if not share_flag and self.appear_then_click(self.I_SUMMON_UP_TO_SHARE_2, interval=1):
                    start_time = time.time()
                    continue
            self.screenshot()
            if self.ui_get_current_page() != page_main:
                self.ui_goto(page_main)
            return False 

        def buy_summon_up():
            start_time = time.time()
            buy_flag = False
            award_count = 0
            while time.time()-start_time < 5:
                self.screenshot()
                if award_count>=3:
                    break 
                if not buy_flag and self.appear(self.I_SUMMON_UP_BUY_FLAG):
                    buy_flag = True
                    logger.info("购买成功")
                    start_time = time.time()
                    continue
                if buy_flag and self.appear_then_click(self.I_SUMMON_UP_AWARD_2,action=self.C_SUMMON_UP_AWARD, interval=1):
                    award_count+=1
                    start_time = time.time()
                    continue
                if buy_flag and self.appear(self.I_SUMMON_UP_GET_AWARD):
                    roi=self.I_SUMMON_UP_GET_AWARD.roi_front
                    self.I_SUMMON_UP_GET_AWARD.roi_front =[roi[0]-31,roi[1]+26,roi[2],roi[3]]
                    self.click(self.I_SUMMON_UP_GET_AWARD,interval=2)
                    time.time()-start_time
                    continue
                if self.appear_then_click(self.I_SUMMON_UP_AWARD,action=self.C_SUMMON_UP_AWARD, interval=1):
                    buy_flag = True
                    start_time = time.time()
                    continue
                if self.appear_then_click(self.I_SUMMON_UP_BUY_GIFT_2, interval=1):
                    start_time = time.time()
                    continue
                if self.appear_then_click(self.I_SUMMON_UP_BUY_GIFT, interval=1):
                    start_time = time.time()
                    continue
            if buy_flag:
                return True
            else:
                self.screenshot()
                if self.ui_get_current_page() != page_main:
                    self.ui_goto(page_main)
                return False

        logger.info('run_summon_up start')
        self.screenshot()
        if self.ui_get_current_page() != page_main:
            self.ui_goto(page_main)
        start_time = time.time()
        share_end =False 
        buy_end = False
        while time.time()-start_time < 5:
            self.screenshot()
            if share_end and buy_end:
                start_time = time.time()
                break
            if self.appear_then_click(self.I_SUMMON_UP_JUMP, interval=1):
                    start_time = time.time()
                    continue
            if self.appear(self.I_SUMMON_UP_TO_GIFT):
                if not share_end:
                    share_end =share_summon_up()
                    start_time = time.time()
                    continue
                if not buy_end:
                    self.click(self.I_SUMMON_UP_TO_GIFT,interval=1)
                    buy_end =buy_summon_up()
                    start_time = time.time()
                    continue
            if self.appear_then_click(self.I_TO_SUMMON, interval=1):
                start_time = time.time()
                continue
            if self.appear_then_click(self.I_SUMMON_UP_TO_SHARE, interval=1):
                start_time = time.time()
                continue
            if self.appear(self.I_SUMMON_UP_SELECT_POOL, interval=1):
                select_pool()
                start_time = time.time()
                continue
        self.screenshot()
        if self.ui_get_current_page() != page_main:
            self.ui_goto(page_main)


if __name__ == "__main__":
    from module.config.config import Config
    from module.device.device import Device
    c = Config('oas1')
    d = Device(c)
    self = SummonUp(c, d)
    self.screenshot()
    self.run_summon_up()
