# This Python file uses the following encoding: utf-8
# @author runhey
# github https://github.com/runhey
import time

import cv2
import numpy as np

from tasks.Component.GeneralBuff.assets import GeneralBuffAssets
from module.atom.ocr import RuleOcr
from module.atom.image import RuleImage
from module.base.timer import Timer
from tasks.base_task import BaseTask
from module.logger import logger


class GeneralBuff(BaseTask, GeneralBuffAssets):
    # 巴纹开关图标固定在加成面板右侧一列，只有行的 y 随 buff 变化。
    # 实测 1280x720：图标 x=860 w=27 h=28，五行 y=140/207/275/342/410，行距 67.5。
    SWITCH_ROI_X = 856  # 搜索列左边界，比图标左移 4px 容忍行定位抖动
    SWITCH_ROI_W = 35  # 搜索列宽度，覆盖 856~891
    SWITCH_CLICK_X = 873  # 点击落点 x，取图标中心

    # 左侧道具图只用来判定「这一行属于哪一类加成」，分不开同类的 50%/100%：
    # 实测金币100%那一行 I_GOLD_50 得 0.897、I_GOLD_100 得 0.847，前者反而更高。
    # 所以同类共用一张模板（取实测泛化更好的那张），百分比一律交给 OCR 裁决。
    ICON_GOLD = GeneralBuffAssets.I_GOLD_50  # 金币类行的道具图
    ICON_EXP = GeneralBuffAssets.I_EXP_100  # 经验类行的道具图

    def open_buff(self):
        """
        打开buff的总界面
        :return:
        """
        logger.info('Open buff')
        while 1:
            self.screenshot()
            if self.appear(self.I_CLOUD):
                break
            if self.appear_then_click(self.I_BUFF_1, interval=2):
                continue

        check_image = self.I_AWAKE
        while 1:
            self.screenshot()
            if self.appear(check_image):
                break

            self.swipe(self.S_BUFF_UP, interval=2)

    def close_buff(self):
        """
        关闭buff的总界面, 但是要确保buff界面已经打开了
        :return:
        """
        logger.info('Close buff')
        while 1:
            self.screenshot()
            if not self.appear(self.I_CLOUD):
                break
            if self.appear_then_click(self.I_BUFF_1, interval=2):
                continue

    def get_area(self, buff: RuleOcr, icon: RuleImage) -> tuple:
        """
        获取要点击的开关buff所在行的区域
        :param buff: 该 buff 的 OCR 规则，用来裁决是 50% 还是 100%
        :param icon: 该类加成的左侧道具图，用来筛出同类的所有行
        :return: 该行左侧道具图的区域(x, y, w, h)，调用方只需要其 y/h 来锁定行；没有就返回None
        """
        # 防止邀请框挡住BUFF框架
        self.reject_invite()
        self.screenshot()
        # 第一步：道具图全列多目标搜索，筛出同类加成的所有候选行。
        # 行坐标取自模板匹配而不是 OCR 的文字框——前者是像素级确定的，定位更稳。
        rows = icon.match_all_any(self.device.image)
        if not rows:
            logger.info(f'No {buff.name} buff')
            return None

        # 第二步：逐个候选行做小区域 OCR，由关键词裁决要的是哪一档百分比。
        # 不做整面板 OCR：RuleOcr.filter 在关键词整体匹配失败后会退化成逐字符匹配
        # （本意是救竖排断行），「金币增加50%」会因此同时命中「金币增加100%」与
        # 「经验增加50%」两行，再被 ocr_full 的 merge_area 合并成一个看似合理的坐标，
        # 于是账号没有该 buff 时会静默操作到别的行。
        target = None
        origin_roi = buff.roi
        try:
            for row in sorted(rows, key=lambda r: r[2]):
                _, x, y, w, h = row
                # x/w 沿用 OCR 资源原有的文字列，y/h 用道具图的匹配结果锁定到这一行
                buff.roi = [origin_roi[0], int(y), origin_roi[2], int(h)]
                texts = [r.ocr_text for r in buff.detect_and_ocr(self.device.image)]
                if any(buff.keyword in t for t in texts):
                    target = row
                    break
        finally:
            buff.roi = origin_roi

        if target is None:
            logger.info(f'No {buff.name} buff in {len(rows)} candidate rows')
            return None
        _, x, y, w, h = target
        return int(x), int(y), int(w), int(h)

    def set_switch_area(self, area):
        """
        设置开关的识别区域
        :param area: get_area/get_area_image 给出的行区域，这里只取其 y/h 用来锁定是哪一行
        :return:
        """
        # area 的 x/w 是按旧版界面「文字右边缘 + 偏移」推出来的，而当前版本的巴纹开关
        # 固定在面板右侧一列、与文字宽度无关，所以只沿用 area 的 y/h 锁定行，x/w 换成固定列。
        # appear_rgb 会先用模板在 roi_back 内定位并回写 roi_front，再比较 roi_front 的均值色，
        # 因此这里设 roi_back 即可，roi_front 由 RuleImage.match() 自动维护。
        roi = [self.SWITCH_ROI_X, int(area[1]), self.SWITCH_ROI_W, int(area[3])]
        self.I_OPEN_HIGHLIGHT_1.roi_back = list(roi)
        self.I_OPEN_HIGHLIGHT_2.roi_back = list(roi)
        self.I_OPEN_HIGHLIGHT_3.roi_back = list(roi)
        self.I_CLOSE_SHADOW.roi_back = list(roi)

    def is_buff_open(self) -> bool:
        """
        当前绑定行的 buff 是否已开启（巴纹图标高亮）
        三张高亮图是旋转动画的不同帧，任一命中即为开启
        :return:
        """
        return (self.appear_rgb(self.I_OPEN_HIGHLIGHT_1)
                or self.appear_rgb(self.I_OPEN_HIGHLIGHT_2)
                or self.appear_rgb(self.I_OPEN_HIGHLIGHT_3))

    def is_buff_close(self) -> bool:
        """
        当前绑定行的 buff 是否已关闭（巴纹图标为暗色阴影）
        :return:
        """
        return self.appear_rgb(self.I_CLOSE_SHADOW)

    def switch_buff(self, is_open: bool, name: str, interval: float = 1, timeout: float = 10) -> bool:
        """
        点击当前绑定行的开关，直到到达目标状态
        :param is_open: True 为要开启，False 为要关闭
        :param name: buff 名字，仅用于日志与点击记录
        :param interval: 两次点击之间的最小间隔
        :param timeout: 超时时间（秒）
        :return: 到达目标状态返回 True，超时返回 False
        """
        # 开关的开态与关态形状相同，模板匹配分不开（实测交叉匹配分数 0.79，同样过阈值），
        # 只能靠均值色区分；而 appear_rgb 不返回可点位置，所以不能复用 ui_click。
        # 落点 y 取绑定行中心、x 取图标中心（实测点巴纹图标与红黄徽章都能触发切换）。
        roi = self.I_CLOSE_SHADOW.roi_back
        click_y = int(roi[1] + roi[3] / 2)
        click_timer = Timer(interval)
        timeout_timer = Timer(timeout).start()
        while not timeout_timer.reached():
            self.screenshot()
            if is_open:
                arrived, reverse = self.is_buff_open(), self.is_buff_close()
            else:
                arrived, reverse = self.is_buff_close(), self.is_buff_open()
            if arrived:
                return True
            # 只有确认停在相反状态才点击。两态都识别不到说明该行被遮挡或位置漂了，
            # 此时盲点会误触其他控件，宁可等下一帧重试。
            if reverse and click_timer.reached_and_reset():
                self.device.click(self.SWITCH_CLICK_X, click_y, control_name=f'BUFF_{name}')
        logger.warning(f'Switch buff {name} timeout')
        return False

    def gold_50(self, is_open: bool = True):
        """
        金币50buff
        :param is_open: 是否打开
        :return:
        """
        logger.info(f'{"Open" if is_open else "Close"} gold 50 buff')
        self.screenshot()
        area = self.get_area(self.O_GOLD_50, self.ICON_GOLD)
        if not area:
            logger.warning('No gold 50 buff')
            return None
        self.set_switch_area(area)
        if not self.switch_buff(is_open, 'gold_50'):
            logger.warning(f'{"Open" if is_open else "Close"} gold 50 buff failed')

    def gold_100(self, is_open: bool = True):
        """
        金币100buff
        :param is_open: 是否打开
        :return:
        """
        logger.info(f'{"Open" if is_open else "Close"} gold 100 buff')
        self.screenshot()
        area = self.get_area(self.O_GOLD_100, self.ICON_GOLD)
        if not area:
            logger.warning('No gold 100 buff')
            return None
        self.set_switch_area(area)
        if not self.switch_buff(is_open, 'gold_100'):
            logger.warning(f'{"Open" if is_open else "Close"} gold 100 buff failed')

    def exp_50(self, is_open: bool = True):
        """
        经验50buff
        :param is_open: 是否打开
        :return:
        """
        logger.info(f'{"Open" if is_open else "Close"} exp 50 buff')
        # 面板一屏只显示 5 行，经验类 buff 可能整行都在可视区外（实测账号里经验50%
        # 排在第 6 行，道具图全列搜索只能搜到经验100%一行）。所以「没搜到该行」和
        # 「搜到了但开关识别不到」都要往上滑一屏再找；滑不出来由超时兜底，不死循环。
        locate_timeout = Timer(20).start()
        while 1:
            if locate_timeout.reached():
                logger.warning('No exp 50 buff')
                return None
            self.screenshot()
            area = self.get_area(self.O_EXP_50, self.ICON_EXP)
            if area:
                self.set_switch_area(area)
                if self.is_buff_open() or self.is_buff_close():
                    break
            # 手指上滑 = 列表内容上移 = 露出下面的行
            self.device.swipe(p2=(530, 240), p1=(580, 320))
            time.sleep(1)

        if not self.switch_buff(is_open, 'exp_50'):
            logger.warning(f'{"Open" if is_open else "Close"} exp 50 buff failed')

    def exp_100(self, is_open: bool = True):
        """
        经验100buff
        :param is_open: 是否打开
        :return:
        """
        logger.info(f'{"Open" if is_open else "Close"} exp 100 buff')
        # 同 exp_50：经验类可能整行在可视区外，没搜到就往上滑一屏再找
        locate_timeout = Timer(20).start()
        while 1:
            if locate_timeout.reached():
                logger.warning('No exp 100 buff')
                return None
            self.screenshot()
            area = self.get_area(self.O_EXP_100, self.ICON_EXP)
            if area:
                self.set_switch_area(area)
                if self.is_buff_open() or self.is_buff_close():
                    break
            # 手指上滑 = 列表内容上移 = 露出下面的行
            self.device.swipe(p2=(530, 240), p1=(580, 320))
            time.sleep(1)

        if not self.switch_buff(is_open, 'exp_100'):
            logger.warning(f'{"Open" if is_open else "Close"} exp 100 buff failed')

    def get_area_image(self, target: RuleImage) -> list:
        """
        获取觉醒加成或者是御魂加成所要点击的区域
        因为实在的图片比ocr快
        :param image:
        :param target:
        :return:
        """
        self.reject_invite()
        self.screenshot()

        if not target.match(self.device.image):
            logger.warning(f'No {target.name} buff')
            return None
            # logger.info(f'front area: {target.roi_front}')
            # logger.info(f'front center: {target.front_center()}')
        start_x = int(target.front_center()[0] + 364)
        start_y = int(target.roi_front[1])
        width = 80
        height = int(target.roi_front[3])
        return [start_x, start_y, width, height]

    def awake(self, is_open: bool = True):
        """
        觉醒buff
        :param is_open: 是否打开
        :return:
        """
        logger.info(f'{"Open" if is_open else "Close"} awake buff')
        self.screenshot()
        area = self.get_area_image(self.I_AWAKE)
        if not area:
            logger.warning('No awake buff')
            return None
        self.set_switch_area(area)
        if not self.switch_buff(is_open, 'awake'):
            logger.warning(f'{"Open" if is_open else "Close"} awake buff failed')

    def soul(self, is_open: bool = True):
        """
        御魂buff
        :param is_open: 是否打开
        :return:
        """
        logger.info(f'{"Open" if is_open else "Close"} soul buff')
        self.screenshot()
        area = self.get_area_image(self.I_SOUL)
        if not area:
            logger.warning('No soul buff')
            return None
        self.set_switch_area(area)
        if not self.switch_buff(is_open, 'soul'):
            logger.warning(f'{"Open" if is_open else "Close"} soul buff failed')

    def reject_invite(self):
        from tasks.Component.GeneralInvite.assets import GeneralInviteAssets as gia
        while 1:
            self.screenshot()
            if not (self.appear(gia.I_I_REJECT_1) or self.appear(gia.I_I_REJECT_2) or self.appear(gia.I_I_REJECT_3)):
                break
            if self.appear(gia.I_I_REJECT_3):
                self.click(gia.I_I_REJECT_3, 6)
                continue
            if self.appear(gia.I_I_REJECT_2):
                self.click(gia.I_I_REJECT_2, 6)
                continue
            if self.appear(gia.I_I_REJECT_1):
                self.click(gia.I_I_REJECT_1, 6)
                continue


if __name__ == '__main__':
    from module.config.config import Config
    from module.device.device import Device

    c = Config('oas1')
    d = Device(c)
    t = GeneralBuff(c, d)

    t.open_buff()
    # t.screenshot()
    #
    t.awake(is_open=True)
    t.soul(is_open=True)
    t.awake(is_open=False)
    t.soul(is_open=False)
    t.gold_50(is_open=True)
    t.gold_100(is_open=True)
    t.gold_100(is_open=False)
    t.gold_50(is_open=False)
    t.exp_50(is_open=True)
    t.exp_50(is_open=False)
    t.exp_100(is_open=True)
    t.exp_100(is_open=False)
