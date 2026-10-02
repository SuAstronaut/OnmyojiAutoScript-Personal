# This Python file uses the following encoding: utf-8
from module.logger import logger
from tasks.GameUi.page import page_main, page_guild
from tasks.DailyAltAcc.utils import DailyAltAccBase, guild_popup_guard


class Returngift(DailyAltAccBase):
    @guild_popup_guard
    def run_returngift(self):
        self.screenshot()
        if self.ui_get_current_page() != page_main:
            self.ui_goto(page_main)
        self.ui_goto(page_guild)
        retry_count = 0
        while 1:
            self.screenshot()
            if retry_count >= 3:
                self.appear_then_click(self.I_TO_THINK, threshold=0.9, interval=1)
                break
            if self.appear_then_click(self.I_SUDDEN, action=self.C_BACK_RED, interval=1):
                logger.info(f"I_SUDDEN found ")
                continue
            """ if self.appear_then_click(self.I_H_BACK_RED, interval=1):
                logger.info(f"I_BACK_RED found ")
                continue """
            if self.appear(self.I_TO_THINK, threshold=0.9, interval=1):
                retry_count += 1
                logger.info(f"I_TO_THINK found ")
                continue
            if self.appear_then_click(self.I_QIYUAN, interval=1):
                continue
        retry_count = 0
        while 1:
            self.screenshot()
            logger.info(f"screenshot found ")
            # 祈愿界面可能随机弹出 I_PAGE_TK，优先点返回红键跳过。
            if self.appear_then_click(self.I_PAGE_TK, action=self.C_BACK_RED, interval=1):
                logger.info(f"I_PAGE_TK found ")
                continue
            if retry_count >= 3:
                break
            if not self.appear(self.I_FLAG_THINK) and self.appear(self.I_TO_THINK,):
                self.appear_then_click(self.I_TO_THINK, interval=3)
                continue
            if self.appear(self.I_BTN_ENSURE):
                self.ui_click_until_disappear(self.I_BTN_ENSURE, interval=1)
                break
            if self.appear_then_click(self.I_BTN_THINK, interval=1):
                continue
            if self.appear(self.I_FLAG_THINK):
                retry_count += 1
                continue
            if self.appear_then_click(self.I_SUDDEN, action=self.C_BACK_RED, interval=1):
                continue
        while 1:
            self.screenshot()
            if self.appear_then_click(self.I_H_BACK_RED2, interval=1):
                continue
            if self.appear_then_click(self.I_BACK_YELLOW, interval=1):
                continue
            if self.appear_then_click(self.I_SUDDEN, action=self.C_BACK_RED, interval=1):
                logger.info(f"I_SUDDEN found ")
                continue
            if self.ui_get_current_page() == page_main:
                break
            else:
                self.ui_goto(page_main)


if __name__ == "__main__":
    from module.config.config import Config
    from module.device.device import Device
    c = Config('oas2')
    d = Device(c)
    self = Returngift(c, d)
    self.screenshot()
    self.run_returngift()
