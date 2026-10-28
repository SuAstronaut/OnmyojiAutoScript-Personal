# This Python file uses the following encoding: utf-8
# @author runhey
# github https://github.com/runhey
import time

from module.logger import logger
from module.atom.ocr import RuleOcr
from module.exception import GameStuckError

from tasks.Component.Buy.buy import Buy
# from tasks.RichMan.assets import RichManAssets
from tasks.RichMan.mall.navbar import MallNavbar
from tasks.RichMan.config import Consignment as ConsignmentStore


class Consignment(Buy, MallNavbar):

    def execute_consignment(self, con: ConsignmentStore=None):
        """
        必须要求这个是第一个执行的对于 商店来说
        :param con:
        :return:
        """
        if not con:
            con = self.config.rich_man.consignment
        if not con.enable:
            logger.info('Consignment is not enable')
            return
        if not con.buy_sale_ticket:
            logger.info('Consignment buy_sale_ticket is not enable')
            return
        self._enter_consignment()
        # 等待页面加载
        time.sleep(0.5)
        self.screenshot()
        # 进入寄售屋兑换页面
        self.ui_click(self.I_CON_ENTER, self.I_CON_ENTER_CHECK)
        #检查勾玉是否足够
        if not self.mall_check_money_legacy(1, 100):
            logger.warning('Consignment money is not enough')
            return
        # 仅识别下排 10 勾玉商品的数量行，避免读到上排的剩余 50。
        stock = RuleOcr(roi=(460, 378, 170, 32), area=(460, 378, 170, 32),
                        mode='Single', method='Default', keyword='', name='con_ticket_stock')
        failure = RuleOcr(roi=(435, 220, 410, 40), area=(435, 220, 410, 40),
                          mode='Single', method='Default', keyword='', name='con_ticket_limit')

        def sold_out():
            text = ''.join(stock.ocr(self.device.image).split())
            if text == '本周剩余数量0':
                return True
            text = ''.join(failure.ocr(self.device.image).split()).rstrip('。！!')
            return text.endswith('本周剩余购买次数为0')

        if sold_out():
            logger.info('Consignment ticket is sold out this week')
            return
        remain_number = self.O_CON_NUMBER.ocr(self.device.image)
        if remain_number == 0:
            logger.warning('Consignment number is 0')
            return
        class TicketSoldOut(Exception):
            pass

        class TicketPurchased(Exception):
            pass

        # 复用原购买/数量逻辑；奖励即完成，不再等待购买失败才退出。
        screenshot = self.screenshot
        had_screenshot = 'screenshot' in self.__dict__
        deadline = time.monotonic() + 30
        next_ocr = 0
        last_region = None
        last_text = ''
        completed = False

        def limit_text():
            nonlocal last_region, last_text
            region = self.device.image[220:260, 435:845].tobytes()
            # 空识别允许下次重试；已识别的相同画面直接复用结果。
            if region != last_region or not last_text:
                last_text = ''.join(failure.ocr_single_line(self.device.image).split()).rstrip('。！!')
                last_region = region
            return last_text

        def checked_screenshot():
            nonlocal next_ocr
            image = screenshot()
            if self.appear(self.I_UI_REWARD, threshold=0.6):
                raise TicketPurchased
            now = time.monotonic()
            if now >= deadline:
                raise GameStuckError('Consignment ticket purchase timed out')
            if now >= next_ocr:
                next_ocr = now + 0.8
                text = limit_text()
                if text == '获得奖励':
                    raise TicketPurchased
                if text.endswith('本周剩余购买次数为0'):
                    raise TicketSoldOut
            return image

        self.screenshot = checked_screenshot
        try:
            if remain_number == 10:
                self.buy_more(self.I_CON_TICKET)
            else:
                self.buy_more(self.I_CON_TICKET, number=remain_number)
        except TicketSoldOut:
            logger.info('Consignment ticket is sold out this week')
            return
        except TicketPurchased:
            completed = True
        finally:
            if had_screenshot:
                self.screenshot = screenshot
            else:
                del self.screenshot
        if completed:
            # 使用现有奖励关闭位置，有限重试并确认弹窗消失。
            for _ in range(5):
                self.click(self.C_UI_REWARD, interval=0.4)
                time.sleep(0.5)
                self.screenshot()
                if not self.appear(self.I_UI_REWARD, threshold=0.6) and limit_text() != '获得奖励':
                    break
            else:
                raise GameStuckError('Consignment ticket reward did not close')
        logger.info('Consignment buy_sale_ticket is success')






if __name__ == '__main__':
    from module.config.config import Config
    from module.device.device import Device

    c = Config('oas1')
    d = Device(c)
    t = Consignment(c, d)

    t.execute_consignment()


