# This Python file uses the following encoding: utf-8
import time
from module.logger import logger
from tasks.GameUi.page import page_main, page_guild
from tasks.DailyAltAcc.utils import DailyAltAccBase, guild_popup_guard


class Tree(DailyAltAccBase):
    @guild_popup_guard
    def run_tree_planting(self):
        def buy_flower():
            logger.info("买花") 
            buy_count=0
            start_time = time.time()
            while time.time()-start_time<5:
                self.screenshot()
                if buy_count>=4:
                    break
                if self.appear_then_click(self.I_TREE_AWARD,interval=1):
                    start_time = time.time()
                    break
                if self.appear(self.I_BUY_FLOWER):
                    buy_flower_image = self.I_BUY_FLOWER.match_all_any(self.device.image)
                    if len(buy_flower_image) ==1:
                        self.I_TO_BUY.roi_back=(buy_flower_image[0][1],buy_flower_image[0][2],652,108)
                        self.I_TO_BUY_DISABLE.roi_back=(buy_flower_image[0][1],buy_flower_image[0][2],652,108)
                        if self.appear_rgb(self.I_TO_BUY):
                            self.appear_then_click(self.I_TO_BUY)
                            continue
                        elif self.appear_rgb(self.I_TO_BUY_DISABLE):
                            break
                    start_time=time.time()
                    continue
                if self.appear_then_click(self.I_CLICK_MAX, interval=1):
                    start_time = time.time()
                    time.sleep(1)
                    self.screenshot()
                    if self.appear_then_click(self.I_CLICK_BUY, interval=1):
                        buy_count+=1
                        start_time = time.time()
                        continue
                if self.appear_then_click(self.I_FLOWER, interval=1):
                    start_time = time.time()
                    
            start_time = time.time()
            while time.time()-start_time<5:
                self.screenshot()
                if self.appear_then_click(self.I_H_BACK_RED, interval=1):
                    start_time = time.time()
                    continue
                if self.appear_then_click(self.I_TREE_BACK_Y, interval=1):
                    start_time = time.time()
                    continue
                if self.appear_rgb(self.I_TO_TREE):
                    start_time = time.time()
                    break
            return buy_count
        if self.ui_get_current_page() != page_guild:
            self.ui_goto(page_guild)
        start_time = time.time()
        retry_count = 0
        while time.time()-start_time < 5:
            self.screenshot()
            if self.appear(self.I_GET_FLOWER, interval=1):
                start_time = time.time()
                break
            if self.appear(self.I_BUY_FLOWER):
                logger.info("没有花,直接买")
                retry_count+=1
                if buy_flower()>=4 or retry_count>=3:
                    self.appear_then_click(self.I_BACK_Y, interval=1)
                    self.ui_goto(page_main)
                    return
                start_time = time.time()
                continue
            if self.appear_then_click(self.I_TO_TREE, interval=2):
                start_time = time.time()
                continue
        logger.info("开始买花")
        while time.time()-start_time < 5:
            self.screenshot()
            if self.appear(self.I_BUY_FLOWER):
                buy_flower()
                start_time = time.time()
                break  
            if self.appear_then_click(self.I_GET_FLOWER, interval=1):
                continue
             
        logger.info("开始捐赠") 
        if self.get_config().daily_alt_acc_config.tree_planting_enable < 2:
            logger.info("种树配置为仅买花，跳过捐赠")
            self.appear_then_click(self.I_BACK_Y, interval=1)
            self.ui_goto(page_main)
            return
        start_time = time.time()
        while time.time()-start_time < 5:
            self.screenshot()
            if self.appear_then_click(self.I_TREE_AWARD, interval=1):
                start_time = time.time()
                break
            if self.appear_then_click(self.I_DONATE_2, interval=1):
                start_time = time.time()
                break
            if self.appear_then_click(self.I_PLANTING, interval=1):
                start_time = time.time()
                continue
            if not self.appear(self.I_PLANTING) and self.appear_then_click(self.I_TO_PLANTING, interval=1):
                start_time = time.time()
                continue
        time.sleep(1)
        self.screenshot()
        self.appear_then_click(self.I_TREE_AWARD)       
        self.appear_then_click(self.I_BACK_Y, interval=1)
        self.ui_goto(page_main)


if __name__ == "__main__":
    from module.config.config import Config
    from module.device.device import Device
    c = Config('oas3')
    d = Device(c)
    self = Tree(c, d)
    self.screenshot()
    self.run_tree_planting()
