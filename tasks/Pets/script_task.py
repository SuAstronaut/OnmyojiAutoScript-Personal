# This Python file uses the following encoding: utf-8
# @author runhey
# github https://github.com/runhey
from module.logger import logger
from module.exception import TaskEnd

from tasks.GameUi.game_ui import GameUi
from tasks.GameUi.page import page_main
from tasks.Pets.assets import PetsAssets
from tasks.Pets.config import PetsConfig
import time
from datetime import timedelta, datetime
class ScriptTask(GameUi, PetsAssets):

    def run(self):
        self.ui_get_current_page()
        self.ui_goto(page_main)
        con: PetsConfig = self.config.pets.pets_config
        # 进入
        start_time = time.time()
        while time.time() - start_time < 10:
            self.screenshot()
            if self.appear(self.I_PET_YARD):
                break
            if self.appear_then_click(self.I_PET_HOUSE, interval=1):
                start_time = time.time()
                continue
            if self.appear_then_click(self.I_PET_TO_YARD, interval=1):
                start_time = time.time()
                continue
        if time.time()-start_time >= 5:
            if self.ui_get_current_page() != page_main:
                self.ui_goto(page_main)
                self.set_next_run('Pets', target=datetime.now() + timedelta(minutes=5))
            raise TaskEnd ('Pets')
        start_time = time.time()
        click_group_flag1 = [self.C_CLICK_PETS_4, 
                            self.C_CLICK_PETS_5,
                             self.C_CLICK_PETS_6]
        click_group_flag2 = [self.C_CLICK_PETS_1, self.C_CLICK_PETS_2,
                             self.C_CLICK_PETS_3]
        click_idx_flag1 = 0
        click_idx_flag2 = 0
        while time.time() - start_time < 5:
            self.screenshot()
            if self.appear_rgb(self.I_PET_FLAG_1) and self.appear_rgb(self.I_PET_FLAG_2):
                break
            """ if self.appear_rgb(self.I_PET_YARD) and not self.appear(self.I_PET_TO_FEED):
                start_time = time.time()
                break """
            if self.appear_then_click(self.I_PET_AWARD,action=self.C_CLICK_AWARD,interval=1):
                start_time = time.time()
                continue
            if self.appear(self.I_PET_SORT):
                self.click(self.I_PET_SORT, interval=1)
                start_time = time.time()
                continue
            if self.appear(self.I_PET_SELECT_FOOD):
                start_time = time.time()
                self.screenshot()
                if self.appear_then_click(self.I_PET_ENSURE, interval=1):
                    start_time = time.time()
                continue
            if self.appear_then_click(self.I_PET_TO_FEED, interval=1):
                start_time = time.time()
                continue
            if self.appear_then_click(self.I_PET_FIND_FOOD, interval=1):
                start_time = time.time()
                continue            
            if self.appear_rgb(self.I_PET_YARD) and not self.appear_rgb(self.I_PET_FLAG_1):
                # FLAG_1没出现，按 6,5,4 顺序依次点击，点完重置
                if click_idx_flag1 >= len(click_group_flag1):
                    click_idx_flag1 = 0
                self.click(click_group_flag1[click_idx_flag1], interval=1)
                click_idx_flag1 += 1
                continue
            if self.appear_rgb(self.I_PET_YARD) and not self.appear_rgb(self.I_PET_FLAG_2):
                # FLAG_2没出现，按 3,2,1 顺序依次点击，点完重置
                if click_idx_flag2 >= len(click_group_flag2):
                    click_idx_flag2 = 0
                self.click(click_group_flag2[click_idx_flag2], interval=1)
                click_idx_flag2 += 1
                continue
        self.ui_click(self.I_PET_EXIT, stop=self.I_CHECK_MAIN,interval=2)
        logger.info('Enter Pets')
        self.set_next_run(task='Pets', success=True, finish=True)
        raise TaskEnd('Pets')

    def _feed(self):
        """
        投喂
        :return:
        """
        logger.hr('Feed', 3)
        self.ui_click(self.I_PET_FEAST, self.I_PET_FEED)
        number = self.O_PET_FEED_AP.ocr(self.device.image)
        if number == 0:
            # 已经投喂过了
            logger.warning('Already feed')
            return
        self.ui_click(self.I_PET_FEED, self.I_PET_SKIP)
        self.wait_until_disappear(self.I_PET_SKIP)

    def _play(self):
        """
        玩耍
        :return:
        """
        logger.hr('Play', 3)
        self.ui_click(self.I_PET_HAPPY, self.I_PET_PLAY)
        number = self.O_PET_PLAY_GOLD.ocr(self.device.image)
        if number == 0:
            # 金币不足
            logger.warning('Gold not enough')
            return
        # 点击玩耍三次不出现就退出
        play_count = 0
        while 1:
            self.screenshot()
            if self.appear(self.I_PET_SKIP):
                break
            if play_count >= 3:
                logger.warning('Play count > 3')
                break
            if self.appear_then_click(self.I_PET_PLAY, interval=1):
                play_count += 1
                logger.info(f'Play {play_count}')
                continue
        self.wait_until_disappear(self.I_PET_SKIP)


if __name__ == '__main__':
    from module.config.config import Config
    from module.device.device import Device
    c = Config('oas1')
    d = Device(c)
    t = ScriptTask(c, d)
    t.screenshot()
    
    
    #t.appear_then_click(t.I_PET_FIND_FOOD, interval=1)
    t.run()
                
