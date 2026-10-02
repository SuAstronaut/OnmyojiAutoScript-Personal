from module.logger import logger
from tasks.SixRealms.moon_sea.skills import MoonSeaSkills
from cached_property import cached_property

class MoonSeaMap(MoonSeaSkills):
    priority_queue=[[0,3,1,5,4,2],[0,1,5,3,4,2]]  # 两种优先级方案
    @cached_property
    def island_list(self):
        return [
            self.I_UI_CANCEL, 
            self.I_SHENMI,
            self.I_HUNDUN,
            self.I_ZHAN,
            self.I_XING,
            self.I_NINGXI
        ]
    
    def enter_island(self):
        self.screenshot()
        logger.info(f'Entering island self.cnt_skill101={self.cnt_skill101}, self.cnt_skillpower={self.cnt_skillpower}')
        # 万相铃不足：点击宁息之屿后弹出「是否仍要进入宁息之屿？」确认框（左取消/右进入）。
        # 必须点右侧「进入」——用 SixRealms 专用模板 I_NINGXI_INSUFFICIENT_ENTER 识别，
        # 否则下一轮遍历 island_list 会把左侧「取消」当作 I_UI_CANCEL 点掉，
        # 形成 A_NINGXI→取消→A_NINGXI→取消 死循环并报 TooManyClick。
        # 该分支在 island_list 扫描之前，且仅作用于本方法（选岛阶段），
        # 不影响其他场景对 I_UI_CANCEL / I_UI_CONFIRM 的使用。
        if self.appear(self.I_NINGXI_INSUFFICIENT_ENTER):
            logger.info('Wanxiangling insufficient: confirm enter Ningxi island')
            self.ui_click_until_disappear(self.I_NINGXI_INSUFFICIENT_ENTER, interval=1)
            return True
        if self.cnt_skill101 < 1 and self.cnt_skillpower < self._conf.power_enhance_level:
            i=0
            for i in range(6):
                if self.appear_then_click(self.island_list[self.priority_queue[0][i]], interval=1):
                    return True
        else:
            i=0
            for i in range(6):
                if self.appear_then_click(self.island_list[self.priority_queue[1][i]], interval=1):
                    return True
        logger.info('Entering island')
        return False

    def activate_store(self) -> bool:
        """
        最后打boss前面激活一次商店买东西
        @return: 有钱够就是True
        """
        if self.cnt_skill101 >= 1:
            # 如果柔风满级就不召唤
            return False
        self.screenshot()
        if not self.appear_rgb(self.I_M_STORE_ACTIVITY):
            return False
        cnt_act = 0
        logger.info('Activating store')
        while 1:
            self.screenshot()
            if self.appear(self.I_UI_CONFIRM):
                self.ui_click_until_disappear(self.I_UI_CONFIRM, interval=2)
                break
            if cnt_act >= 3:
                logger.warning('Store is not active')
                return False
            if self.appear_then_click(self.I_M_STORE_ACTIVITY, interval=1.5):
                cnt_act += 1
                continue
        return True


if __name__ == '__main__':
    from module.config.config import Config

    c = Config('du')
    t = MoonSeaMap(c)
    # t.screenshot()
    # t.device.image = load_image(r'C:\Users\Ryland\Desktop\Desktop\34.png')
    # match = re.search(r'\d{1,2}', '<17回合后迎战月读')
    # if match:
    #     isl_num = int(match.group())
    #     print(isl_num)

