# This Python file uses the following encoding: utf-8
import time
from module.logger import logger
from tasks.GameUi.page import page_main, page_guild
from tasks.DailyAltAcc.utils import DailyAltAccBase, guild_popup_guard
from tasks.Component.Buy.guild_shop import GuildShopSafety, ShopState


class Tree(GuildShopSafety, DailyAltAccBase):
    @guild_popup_guard
    def run_tree_planting(self):
        state = self._shop_purchase('leaf')
        self._shop_leave()
        self._shop_back_to_guild()
        if state == ShopState.UNKNOWN:
            self._shop_fail('Autumn-leaf purchase progress is unknown')
        # Buy-only mode keeps the original setting and does not enter cultivation.
        if self.get_config().daily_alt_acc_config.tree_planting_enable < 2:
            self.ui_goto(page_main)
            return
        self._tree_enter_cultivation()
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


    def _tree_enter_cultivation(self):
        # Navigation only: retain the existing donation strategy below.
        self._shop_frame()
        if self._shop_page() != 'GUILD':
            self._shop_fail('Tree entry requires a verified guild scene')
        button = self._shop_match(self.I_TO_TREE)
        if button:
            roi = tuple(button.roi_front)
        else:
            # Current autumn event renamed/repainted the old entry template.
            hits = [b for b in self._shop_text((0, 135, 260, 380))
                    if b.score >= 0.9 and self._shop_normalize(b.ocr_text) == '枫树培育']
            if len(hits) != 1:
                self._shop_fail('Maple cultivation entry is unreadable')
            box = hits[0].box
            x = int(min(float(p[0]) for p in box))
            y = int(min(float(p[1]) for p in box)) + 135
            roi = (x, y, int(max(float(p[0]) for p in box)) - x,
                   int(max(float(p[1]) for p in box)) + 135 - y)
        self._shop_click(roi, 'tree_cultivation_entry')
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            time.sleep(0.8)
            self._shop_frame()
            if self.appear(self.I_GET_FLOWER) or self.appear(self.I_TO_PLANTING) or self.appear(self.I_PLANTING):
                return
        self._shop_fail('Maple cultivation page did not load within 10s')


if __name__ == "__main__":
    from module.config.config import Config
    from module.device.device import Device
    c = Config('oas3')
    d = Device(c)
    self = Tree(c, d)
    self.screenshot()
    self.run_tree_planting()
