# This Python file uses the following encoding: utf-8
# @author runhey
# github https://github.com/runhey
import time

import random
import re
from cached_property import cached_property
from datetime import datetime, timedelta
from module.atom.click import RuleClick

from module.base.timer import Timer
from module.atom.image_grid import ImageGrid
from module.atom.image import RuleImage
from module.base.utils import point2str
from module.logger import logger
from module.exception import TaskEnd, GameStuckError

from tasks.KekkaiUtilize.script_task import ScriptTask as KU
from tasks.KekkaiUtilize.utils import CardClass
from tasks.KekkaiActivation.assets import KekkaiActivationAssets
from tasks.KekkaiActivation.utils import parse_rule
from tasks.KekkaiActivation.config import ActivationConfig
from tasks.Utils.config_enum import ShikigamiClass
from tasks.GameUi.page import page_main, page_guild
from tasks.KekkaiActivation.config import CardType
from tasks.KekkaiUtilize.config import UtilizeRule
from tasks.Pets.script_task import ScriptTask as Pets

""" 结界挂卡 """
class ScriptTask(KU, KekkaiActivationAssets):
    # 卡片效果标志异常时最多等待10秒，避免未知状态进入无限截图循环
    CARD_EFFECT_TIMEOUT = 10
    # 激活确认最长等待15秒，失败后退出卡页并短延迟重试
    ACTIVATION_CONFIRM_TIMEOUT = 15
    # 卡片剩余时间异常时延迟1分钟重试，避免 next_run=now 形成紧密循环
    ACTIVATION_RETRY_DELAY = timedelta(minutes=1)

    def run(self):
        con = self.config.kekkai_activation.activation_config
        # 检查是否处于禁止运行时间段，命中则跳过本次运行
        self.check_forbidden_time('KekkaiActivation', con.forbidden_time_enable, con.forbidden_time_range)
        self.ui_get_current_page()
        if not self.ui_goto(page_guild):
            self.realm_entry_failed('寮')
        logger.info(f'开始挂卡{self.config.kekkai_activation.activation_config.card_type}')
        # 在寮的主界面 检查是否有收取体力或者是收取寮资金
        # self.check_guild_ap_or_assets()

        # 进入寮结界
        if not self.goto_realm():
            self.realm_entry_failed('寮结界')

        if con.exchange_before:
            self.check_max_lv(con.shikigami_class)
        time.sleep(1)
        # 收取经验
        self.harvest_card()
        # 开始挂卡
        self.run_activation(con)
        while 1:
            # 关闭到结界界面
            self.screenshot()
            if self.appear(self.I_REALM_SHIN):
                break
            if self.appear(self.I_SHI_GROWN):
                break
            if self.appear_then_click(self.I_UI_BACK_RED, interval=1):
                continue

        if con.exchange_max:
            self.check_max_lv(con.shikigami_class)
        # self.back_guild()
        self.ui_get_current_page()
        self.ui_goto(page_main)
        if con.pets_enable:
            pets = Pets(self.config, self.device)
            pets.run()
        # 走到这里说明本次运行没有走过失败收尾，连续失败计数归零
        self.reset_entry_failure('KekkaiActivation')
        raise TaskEnd('KekkaiActivation')

    def realm_entry_failed(self, name: str) -> None:
        """挂卡侧的失败收尾：通知渠道换成挂卡标题，延后与父类一致。"""
        # 连续失败达上限就不再 5 分钟一次地重复，直接推到明天同一时刻
        if self.record_entry_failure(
                'KekkaiActivation',
                self.config.kekkai_activation.activation_config.max_consecutive_failures):
            fail_msg = f'进入{name}失败, 连续失败已达上限, 明天再试'
            target = datetime.now() + timedelta(days=1)
        else:
            fail_msg = (f'进入{name}失败, '
                        f'{int(self.REALM_ENTRY_FAIL_DELAY.total_seconds() // 60)}分钟后再次挂卡')
            target = datetime.now() + self.REALM_ENTRY_FAIL_DELAY
        logger.warning(fail_msg)
        # 与同文件既有的两处写法一致：DAILY 由 DailyAltAcc 嵌套调用时强制指定，
        # 那条路径的失败通知应由 DailyAltAcc 自己出，这里静默
        if not self.config.kekkai_activation.activation_config.card_type == CardType.DAILY:
            self.config.notifier.push(content=fail_msg, title='结界挂卡')
        self.set_next_run('KekkaiActivation', target=target)
        raise TaskEnd('KekkaiActivation')

    @cached_property
    def dict_card_image(self) -> dict:
        match_targets = {
            CardClass.TAIKO6: self.I_CARDS_KAIKO_6,
            CardClass.TAIKO5: self.I_CARDS_KAIKO_5,
            CardClass.TAIKO4: self.I_CARDS_KAIKO_4,
            CardClass.TAIKO3: self.I_CARDS_KAIKO_3,
            CardClass.FISH6: self.I_CARDS_FISH_6,
            CardClass.FISH5: self.I_CARDS_FISH_5,
            CardClass.FISH4: self.I_CARDS_FISH_4,
            CardClass.FISH3: self.I_CARDS_FISH_3,
            CardClass.MOON6: self.I_CARDS_MOON_6,
            CardClass.MOON5: self.I_CARDS_MOON_5,
            CardClass.MOON4: self.I_CARDS_MOON_4,
            CardClass.MOON3: self.I_CARDS_MOON_3,
            CardClass.MOON2: self.I_CARDS_MOON_2,
            CardClass.MOON1: self.I_CARDS_MOON_1
        }
        return match_targets

    @cached_property
    def dict_image_card(self) -> dict:
        return {v: k for k, v in self.dict_card_image.items()}

    @cached_property
    def order_targets(self) -> ImageGrid:
        rule = self.config.kekkai_activation.activation_config.card_type
        if rule == CardType.TAIKO:
            return ImageGrid([self.I_CARDS_KAIKO_6, self.I_CARDS_KAIKO_5])
        elif rule == CardType.FISH:
            return ImageGrid([self.I_CARDS_FISH_6, self.I_CARDS_FISH_5])
        elif rule == CardType.DAILY:
            return ImageGrid([self.I_CARDS_KAIKO_6, self.I_CARDS_KAIKO_5, self.I_CARDS_KAIKO_4, self.I_CARDS_KAIKO_3])
        else:
            logger.error('Unknown utilize rule')
            raise ValueError('Unknown utilize rule')

    def run_activation(self, _config: ActivationConfig) -> bool:
        """
        执行挂卡，要求在结界的界面
        顺便把下一次执行也设置了
        :return: 挂卡成功（）返回True，失败(时间没到提前来了)返回False
        退出的时候还是在挂卡界面而不是结界界面
        """
        self.goto_cards()
        # 太诡异了 为什么有这么长的动画, 那么长的动画先休息一会
        logger.hr('Start activation')
        time.sleep(0.5)
        while 1:
            self.screenshot()
            card_status = self.check_card_status()

            # 空槽位先筛选并选择目标卡，不能先调用 check_card_effect() 等待按钮标志。
            # 当前中央槽位为空时，邀请/激活按钮本来就不存在，旧顺序会因此永久阻塞。
            if not card_status:
                # 这些标志表示卡片转场或已选卡尚未渲染完成，继续等待一轮。
                if self.appear_rgb(self.I_A_ACTIVATE_YELLOW):
                    continue
                if self.appear_rgb(self.I_A_INVITE):
                    continue
                if self.appear(self.I_A_DEMOUNT):
                    logger.info('Now in the animation')
                    logger.info('Now there is no card')
                    continue
                logger.info('Card is not selected also not using')
                self.screening_card(_config.card_type)
                continue

            card_effect = self.check_card_effect()
            if card_effect is None:
                # 未识别到任何效果标志时返回外层重新截图，避免卡死在内部循环。
                logger.warning('Card effect is unknown, retrying card page detection')
                continue

            # 如果这张卡生效着，在使用中
            if card_status and card_effect:
                logger.info('Card is using')
                interval = self.ocr_time()
                if interval is None:
                    logger.warning('Card time is unavailable, retry activation after 1 minute')
                    self.set_next_run(
                        "KekkaiActivation",
                        target=datetime.now() + self.ACTIVATION_RETRY_DELAY,
                    )
                    return False
                # 正常挂卡中的下次上号时间叠加随机延时，避免准点上线
                next_time = self.apply_random_delay('KekkaiActivation', interval + datetime.now())
                if not self.config.kekkai_activation.activation_config.card_type == CardType.DAILY :
                    self.config.notifier.push(content=f'结界下次挂卡时间: {next_time}', title='结界挂卡')
                self.set_next_run("KekkaiActivation", target=next_time)
                return False
            # 如果已经选中这张卡了， 那就激活这张卡
            if card_status and not card_effect:
                logger.info('Card is selected but not using')
                confirm_timeout = Timer(self.ACTIVATION_CONFIRM_TIMEOUT).start()
                while not confirm_timeout.reached():
                    self.screenshot()
                    if self.appear(self.I_A_INVITE, threshold=0.8):
                        logger.info('Card is activated')
                        break
                    if self.appear_then_click(self.I_UI_CONFIRM, interval=0.6):
                        continue
                    if self.appear_rgb(self.I_A_ACTIVATE_YELLOW):
                        self.click(self.I_A_ACTIVATE_YELLOW, interval=1)
                        continue
                else:
                    logger.warning('Card activation confirmation timeout, retry after 1 minute')
                    self.set_next_run(
                        "KekkaiActivation",
                        target=datetime.now() + self.ACTIVATION_RETRY_DELAY,
                    )
                    return False
                interval = self.ocr_time(True)
                if interval is None:
                    logger.warning('Activated card time is unavailable, retry after 1 minute')
                    self.set_next_run(
                        "KekkaiActivation",
                        target=datetime.now() + self.ACTIVATION_RETRY_DELAY,
                    )
                    return False
                # 正常挂卡后的下次上号时间叠加随机延时，避免准点上线
                next_time = self.apply_random_delay('KekkaiActivation', interval + datetime.now())
                if not self.config.kekkai_activation.activation_config.card_type == CardType.DAILY :
                    self.config.notifier.push(content=f'结界下次挂卡时间: {next_time}', title='结界挂卡')
                self.set_next_run("KekkaiActivation", target=next_time)
                return True
    def goto_cards(self) -> None:
        """寮结界 -> 挂卡界面。

        入口由模板匹配改为固定区域点击，并由父类的 goto_sub_page 负责「回寮重进结界」
        的重试；旧实现是 while 1 死循环，进不去就永远卡住。失败时交由 realm_entry_failed
        通知 + 延后并抛 TaskEnd 结束本次任务，不再返回调用方。
        """
        def _enter_once() -> bool:
            # 先截图，否则 harvest_card 用的是上一轮遗留的 device.image（appear 不强制重截）。
            # 语义相对旧实现略放宽：旧的是「先判是否已在卡页，已到就 break 且不收菜」，
            # 这里每次尝试都先收一次；差异极小（harvest_card 全是 appear_then_click，不命中即空操作）。
            self.screenshot()
            self.harvest_card()
            return self.realm_entry_click(
                self._realm_region(self.C_REALM_CARD, 'realm_card'),
                lambda: self.appear(self.I_A_CHECK_CARD) or self.appear(self.I_A_AUTO_INVITE))

        if not self.goto_sub_page(_enter_once, '挂卡'):
            # 通知 + 5 分钟延后 + TaskEnd 都在这里完成；文案由本类覆写的实现生成
            self.realm_entry_failed('挂卡')
        logger.info('Enter card page')

    def check_card_status(self, screenshot=False) -> bool:
        """
        判断使用有挂卡在上面了， 判断依据就是如果没看就可以显示背景图
        :param screenshot:
        :return: 如果有卡在上面了返回True，否则返回False
        """
        if screenshot:
            self.screenshot()
        return not self.appear(self.I_A_EMPTY)

    def check_card_effect(self, screenshot=False) -> bool | None:
        """
        检查这张卡是否生效了, 如果是出现的“邀请”那就是生效了， 如果是“激活”那就是还没生效
        :param screenshot:
        :return: 生效返回True
        """
        if screenshot:
            self.screenshot()
        if self.appear(self.I_A_INVITE, threshold=0.8):
            return True
        elif self.appear_rgb(self.I_A_ACTIVATE_YELLOW):
            return False
        logger.info('Unknown card effect')
        timeout = Timer(self.CARD_EFFECT_TIMEOUT).start()
        while not timeout.reached():
            self.screenshot()
            if self.appear(self.I_A_INVITE, threshold=0.7):
                return True
            elif self.appear_rgb(self.I_A_ACTIVATE_YELLOW):
                return False
            elif self.appear_rgb(self.I_A_ACTIVATE_GRAY):
                return False
        logger.warning(f'Card effect detection timeout ({self.CARD_EFFECT_TIMEOUT}s)')
        return None

    def ocr_time(self, screenshot=False) -> timedelta | None:
        """读取卡片剩余时间；连续三次无有效正数时返回 None。"""
        for attempt in range(1, 4):
            if screenshot or attempt > 1:
                self.screenshot()
            delta = self.O_CARD_ALL_TIME.ocr_duration(self.device.image)
            if isinstance(delta, timedelta) and delta > timedelta(0):
                return delta
            if isinstance(delta, timedelta):
                logger.warning(f'Card remaining time is 0 ({attempt}/3)')
            else:
                logger.warning(f'Card time OCR error ({attempt}/3)')
            time.sleep(0.5)
        return None

    def screening_card(self, rule: str):
        """
        开始挑选卡
        :return:
        """

        if rule == CardType.TAIKO:
            card_class = CardClass.TAIKO
            target_class = self.I_A_CARD_KAIKO
        elif rule == CardType.FISH:
            card_class = CardClass.FISH
            target_class = self.I_A_CARD_FISH
        elif rule == CardType.DAILY:
            card_class = CardClass.TAIKO
            target_class = self.I_A_CARD_KAIKO
        else:
            logger.warning('Unknown card rule')
            self.push_notify(content='Unknown card rule')
            return

        while 1:
            self.screenshot()

            if self.appear(target_class):
                time.sleep(0.3)
                self.screenshot()
                if self.appear(target_class):
                    break
            if self.click(self.C_A_SELECT_CARD_LIST, interval=2.5):
                continue
        logger.info('Appear card class: {}'.format(card_class))
        while 1:
            self.screenshot()
            if not self.appear(target_class):
                break
            if self.appear_then_click(target_class, interval=1):
                continue
        logger.info('Selected card class: {}'.format(card_class))

        # 找最优卡
        while 1:
            self.screenshot()
            target = self.check_card_num()
            if target is None:
                # 未发现卡，处理逻辑
                self._card_not_found()
            # 筛选过程中卡槽可能已经被选中；交回外层统一判断激活状态。
            if not self.appear(self.I_A_EMPTY):
                logger.info('Card slot changed while screening, return to activation state check')
                return
            while 1:
                self.screenshot()
                if not self.appear(self.I_A_EMPTY):
                    self.config.kekkai_activation.activation_config.card_not_found_count = 0
                    self.config.save()
                    message = f'✅ 确认挂卡: {rule}'
                    self.save_image(content=message, push_flag=False, wait_time=0)
                    return
                if self.click(target, interval=1):
                    continue

    def check_card_num(self):
        rule = self.config.kekkai_activation.activation_config.card_type
        if rule == CardType.TAIKO:
            min_card_num = self.config.kekkai_activation.activation_config.min_taiko_num
            check_card = "勾玉"
        elif rule == CardType.FISH:
            min_card_num = self.config.kekkai_activation.activation_config.min_fish_num
            check_card = "体力"
        elif rule == CardType.DAILY:
            # DAILY 规则使用太鼓规则，但不设最小值限制，选择任意太鼓卡
            min_card_num = 1
            check_card = "勾玉"
        else:
            logger.error('Unknown utilize rule')
            raise ValueError('Unknown utilize rule')

        ocr_count = 0
        while 1:
            self.screenshot()
            results = self.O_CHECK_CARD_NUMBER.detect_and_ocr(self.device.image)
            ocr_count += 1
            # 第一步：筛选出包含 "体力或者勾玉" 的结果
            filtered_results = [result for result in results if check_card in result.ocr_text]
            logger.info(f"识别到卡: {[result.ocr_text for result in filtered_results]}")

            # 第二步：提取数字并按数字排序
            numeric_results = []
            for result in filtered_results:
                # 使用正则表达式提取所有数字
                numbers = [int(num) for num in re.findall(r'\d+', result.ocr_text)]
                if numbers:  # 如果提取到数字
                    if numbers[0] < min_card_num:
                        continue
                    numeric_results.append((numbers[0], result))  # 按第一个数字排序

            if numeric_results:
                # 按数字大到小排序
                sorted_results = [result for _, result in sorted(numeric_results, key=lambda x: x[0], reverse=True)]
                max_result = sorted_results[0]  # 获取数字最大的结果对象

                box = max_result.box  # 获取边界框坐标
                x_min = self.O_CHECK_CARD_NUMBER.roi[0] + box[0][0]
                y_min = self.O_CHECK_CARD_NUMBER.roi[1] + box[0][1]
                width = box[1][0] - box[0][0]
                height = box[2][1] - box[1][1]
                roi = int(x_min), int(y_min), int(width), int(height)

                target = RuleClick(roi_front=roi, roi_back=roi, name="tmpclick")
                logger.info(f"选择挂卡: [{max_result.ocr_text}] {roi}")

                return target
            else:
                if ocr_count > 3:
                    logger.error('多次未找到符合条件的结果, 退出')
                    return None
                logger.warning("未找到符合条件的结果, 准备往上滑动")
                duration = 2
                safe_pos_x = random.randint(200, 400)
                safe_pos_y = random.randint(580, 600)
                p1 = (safe_pos_x, safe_pos_y)
                p2 = (safe_pos_x, safe_pos_y - 410)
                logger.info('Swipe %s -> %s, %sS ' % (point2str(*p1), point2str(*p2), duration))
                # 改走 device.swipe 分派：跟随 control_method 配置并可进入拟人化路径
                self.device.swipe(p1, p2, duration=duration)
                time.sleep(1)
                continue

    def _card_not_found(self):
        # 获取配置引用
        activation_config = self.config.kekkai_activation.activation_config
        # 多少分钟后重试
        retry_minutes = 180
        retry_count = 3
        # 递增未找到卡的计数器
        activation_config.card_not_found_count += 1

        if activation_config.card_not_found_count >= retry_count:
            # 达到重试上限时的处理
            log_msg = f"⚠️{activation_config.card_type}卡未检出（累计{retry_count}次），{retry_minutes}分钟后重试"
            activation_config.card_not_found_count = 0  # 重置计数器并延长下次执行时间
            next_run = datetime.now() + timedelta(minutes=retry_minutes)
        else:
            # # 未达上限切换卡类型
            new_type = (
                CardType.FISH
                if activation_config.card_type == CardType.TAIKO
                else CardType.TAIKO
            )
            log_msg = f"🔄{activation_config.card_type}卡未检出 → 切换{new_type}"
            activation_config.card_type = new_type
            next_run = datetime.now()

        # 统一记录日志和推送
        self.save_image(content=log_msg, push_flag=True)

        # 保存配置并设置下次执行
        self.config.save()
        self.set_next_run("KekkaiActivation", target=next_run)
        raise TaskEnd('KekkaiActivation')

    def check_max_lv(self, shikigami_class: ShikigamiClass = ShikigamiClass.N):
        """
        在结界界面，进入式神育成，检查是否有满级的，如果有就换下一个
        退出的时候还是结界界面
        :return:
        """
        if not self.realm_goto_grown():
            logger.warning('进入式神育成界面失败，跳过满级检查')
            return
        if self.appear(self.I_RS_LEVEL_MAX):
            # 存在满级的式神
            logger.info('Exist max level shikigami and replace it')
            self.unset_shikigami_max_lv()
            self.switch_shikigami_class(shikigami_class)
            self.set_shikigami(shikigami_order=7, stop_image=self.I_RS_NO_ADD)
        else:
            logger.info('No max level shikigami')
        if self.detect_no_shikigami():
            logger.warning('There are no any shikigami grow room')
            self.switch_shikigami_class(shikigami_class)
            self.set_shikigami(shikigami_order=7, stop_image=self.I_RS_NO_ADD)

        # 回到结界界面
        while 1:
            self.screenshot()

            if self.appear(self.I_REALM_SHIN) and self.appear_multi_scale(self.I_SHI_GROWN):
                self.screenshot()
                if not self.appear(self.I_REALM_SHIN):
                    continue
                break
            if self.appear_then_click(self.I_UI_BACK_BLUE, interval=2.5):
                continue

    def harvest_card(self):
        """
        收卡的经验
        :return:
        """
        self.appear_then_click(self.I_A_HARVEST_EXP, interval=1)\
        or self.appear_then_click(self.I_A_HARVEST_FISH4, interval=1)\
        or self.appear_then_click(self.I_A_HARVEST_KAIKO_4, interval=1)\
        or self.appear_then_click(self.I_A_HARVEST_KAIKO_3, interval=1)\
        or self.appear_then_click(self.I_A_HARVEST_KAIKO_6, interval=1)\
        or self.appear_then_click(self.I_A_HARVEST_FISH_6, interval=1)\
        or self.appear_then_click(self.I_A_HARVEST_MOON_3, interval=1)\
        or self.appear_then_click(self.I_A_HARVEST_FISH_3, interval=1)
if __name__ == "__main__":
    from module.config.config import Config
    from module.device.device import Device
    import cv2

    c = Config('oas1')
    d = Device(c)

    t = ScriptTask(c, d)
    t.run()
    # t.run_activation(t.config.kekkai_activation.activation_config)
