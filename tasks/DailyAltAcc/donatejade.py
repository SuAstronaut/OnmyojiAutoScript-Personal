# This Python file uses the following encoding: utf-8
from tasks.GameUi.page import page_main, page_guild
from tasks.DailyAltAcc.utils import DailyAltAccBase, guild_popup_guard
from tasks.KekkaiUtilize.assets import KekkaiUtilizeAssets


class Donatejade(DailyAltAccBase):
    @guild_popup_guard
    def run_donatejade(self):
        self.screenshot()
        if self.ui_get_current_page() != page_main:
            self.ui_goto(page_main)
        self.ui_goto(page_guild)
        self.goto_realm()
        self.donatejade()
        self.back_guild()
        self.screenshot()
        if self.ui_get_current_page() != page_main:
            self.ui_goto(page_main)
    
    def donatejade(self):
        retry_count = 0 
        while 1:
            self.screenshot()
            if retry_count >= 3:
                break
            if self.appear_then_click(self.I_AWARD, interval=1):
                break
            if self.appear(self.I_LIMIT) and self.appear_then_click(self.I_CLICK_DONATE, interval=1):
                continue
            if not self.appear(self.I_LIMIT) and self.appear_then_click(self.I_ADD,interval=0.5):
                continue
            if not self.appear(self.I_ADD) and self.appear_then_click(self.I_DONATE, interval=1):
                retry_count += 1
                continue
            
            
    def goto_realm(self):
        """
        从寮的主界面进入寮信息界面
        :return:
        """
        while 1:
            self.screenshot()
            if self.appear(self.I_DONATE):
                break
            if self.appear_then_click(KekkaiUtilizeAssets.I_PLANT_TREE_CLOSE):
                continue
            if self.appear_then_click(KekkaiUtilizeAssets.I_GUILD_INFO, interval=1):
                continue

    def back_guild(self):
        """
        回到寮的界面
        :return:
        """
        while 1:
            self.screenshot()

            if self.appear(KekkaiUtilizeAssets.I_GUILD_INFO):
                break
            if self.appear(KekkaiUtilizeAssets.I_GUILD_REALM):
                break
            if self.appear_then_click(KekkaiUtilizeAssets.I_PLANT_TREE_CLOSE):
                continue
            if self.appear_then_click(self.I_UI_BACK_RED, interval=1):
                continue
            if self.appear_then_click(self.I_UI_BACK_BLUE, interval=1):
                continue
            if self.appear_then_click(self.I_UI_BACK_YELLOW, interval=1):
                continue


if __name__ == "__main__":
    from module.config.config import Config
    from module.device.device import Device
    c = Config('oas2')
    d = Device(c)
    self = Donatejade(c, d)
    self.screenshot()
    self.run_donatejade()
