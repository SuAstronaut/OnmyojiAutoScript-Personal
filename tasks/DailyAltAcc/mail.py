# This Python file uses the following encoding: utf-8
import time
from module.logger import logger
from tasks.GameUi.assets import GameUiAssets
from tasks.Restart.assets import RestartAssets
from tasks.GameUi.page import page_main
from tasks.DailyAltAcc.utils import DailyAltAccBase


class Mail(DailyAltAccBase):
    def harvest_mail(self) -> bool:
        logger.info('Harvest mail')

        start_time = time.time()
        while time.time()-start_time < 3:
            self.screenshot()
            if self.appear(self.I_M_PAGE_MAIL):
                start_time = time.time()
                break
            if self.appear_then_click(self.I_M_MAIN_TO_MAIL, interval=1.5):
                start_time = time.time()
                continue
        if time.time()-start_time >= 5:
            return False

        logger.info('Exec harvest mail')
        start_time = time.time()
        while time.time()-start_time < 3:
            self.screenshot()
            if self.appear_then_click(self.I_M_ENSURE_GET, interval=0.8):
                time.sleep(1)
                start_time = time.time()
                logger.info('I_M_ENSURE_GET success')
                break
            if self.appear_rgb(RestartAssets.I_HARVEST_MAIL_ALL):
                if self.appear_then_click(RestartAssets.I_HARVEST_MAIL_ALL, interval=1.5):
                    logger.info('I_HARVEST_MAIL_ALL success')
                start_time = time.time()
                continue
        if time.time()-start_time >= 3:
            self.screenshot()
            self.appear_then_click(self.I_M_BACK_RED, interval=1.5)
            return True
        logger.info('harvest mail get award')
        while 1:
            self.screenshot()
            if self.appear(GameUiAssets.I_CHECK_MAIN) or self.appear(self.I_M_MAIN_TO_MAIL):
                break
            if self.get_award_daliy():
                continue
            if self.appear_then_click(self.I_M_ENSURE_GET, interval=1):
                continue
            if self.appear_rgb(self.I_M_BACK_RED):
                self.appear_then_click(self.I_M_BACK_RED, interval=1.5)
                continue 
        return True

    def run_mail(self):
        self.screenshot()
        if self.ui_get_current_page() != page_main:
            self.ui_goto(page_main)
        result=self.harvest_mail()
        time.sleep(1)
        self.screenshot()
        if self.ui_get_current_page() != page_main:
            self.ui_goto(page_main)
        return result


if __name__ == "__main__":
    from module.config.config import Config
    from module.device.device import Device
    c = Config('oas2')
    d = Device(c)
    self = Mail(c, d)
    self.screenshot()
    self.run_mail()
