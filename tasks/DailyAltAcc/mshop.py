# This Python file uses the following encoding: utf-8
import re
import time
from pathlib import Path
from functools import cached_property

import cv2
import numpy as np

from module.atom.image import RuleImage
from module.base.timer import Timer
from module.base.utils import save_image
from module.logger import logger
from module.exception import GameStuckError, GamePageUnknownError
from tasks.GameUi.page import page_main, page_mall
from tasks.DailyAltAcc.utils import DailyAltAccBase
from tasks.RichMan.mall.mall import Mall
from tasks.RichMan.config import Consignment, MedalRoom
from tasks.MysteryShop.assets import MysteryShopAssets
from tasks.DailyAltAcc.config import GoodsType, CoinType, MSGType
from tasks.DailyAltAcc.stat_log import StatEvent
from tasks.DailyAltAcc.mshop_grid import (
    COIN_NAMES,
    GOODS_ASSETS,
    GOODS_NAMES,
    SlotItem,
    coin_of,
    enabled_goods,
    is_unlocked,
    locate_slot,
    price_rule,
    refine_goods,
)


class MshopRefreshError(RuntimeError):
    """A bounded shop failure; the existing daily-task adapter records it failed."""


class Mshop(Mall, DailyAltAccBase):
    # Unguarded buttons are only overlay/layout probes. Never confirm from them alone.
    _MS_CONFIRM = RuleImage(roi_front=(718,408,81,43), roi_back=(684,404,155,56),
                            threshold=0.8, method="Template matching",
                            file="./tasks/DailyAltAcc/mshop/mshop_ms_ensure.png")
    _MS_TITLE_ROI = (446, 291, 310, 53)
    _MS_COST_ROI = (798, 293, 39, 51)
    _MS_OFFER_ROI = (1195, 589, 80, 31)
    _MS_SHELF_ROI = (139, 107, 886, 454)
    _MS_DIALOG_TIMEOUT = 10.0
    _MS_RESULT_TIMEOUT = 12.0
    _MS_EXIT_TIMEOUT = 6.0
    _MS_BUTTON_TIMEOUT = 3.0
    # Only the control itself; excludes the dynamic cost line and all account UI.
    _MS_REFRESH_ENABLED = RuleImage(roi_front=(1195,525,74,64), roi_back=(1195,525,74,64),
                                    threshold=0.98, method="Template matching",
                                    file="./tasks/DailyAltAcc/mshop/mshop_ms_refresh_enabled.png")
    _MS_REFRESH_DISABLED = RuleImage(roi_front=(1195,525,74,64), roi_back=(1195,525,74,64),
                                     threshold=0.98, method="Template matching",
                                     file="./tasks/DailyAltAcc/mshop/mshop_ms_refresh_disabled.png")
    _MS_REFRESH_LIMIT = RuleImage(roi_front=(529,225,225,26), roi_back=(525,221,233,34),
                                  threshold=0.95, method="Template matching",
                                  file="./tasks/DailyAltAcc/mshop/mshop_ms_refresh_limit.png")

    def execute_mall(self):
            logger.hr('Mall', 1)
            self.screenshot()
            self.ui_get_current_page()
            self.ui_goto(page_mall, confirm_wait=2.5)

            # 勋章屋黑蛋：必须放在寄售屋之前。勋章页不是 page_main，也没有
            # 黄色返回可点，下面的退出循环在那里两个分支都不成立会死循环；
            # 而寄售屋兑换子页有黄色返回，退出循环能正常走完。
            self._buy_medal_black_daruma()

            # 寄售屋
            config = Consignment(enable=True,buy_sale_ticket=True)
            self.execute_consignment(config)

            # 退出
            start_time = time.time()
            while time.time()>start_time-5:
                self.screenshot()
                if self.ui_get_current_page() == page_main:
                    break
                if self.appear_then_click(self.I_BACK_YELLOW, interval=1):
                    start_time = time.time()
                    continue
            if self.ui_get_current_page() != page_main:
                self.ui_goto(page_main)

    def run_consignment_ticket(self):
        """独立购买寄售券；购买实现与商城返回均复用现有链路。"""
        self.screenshot()
        self.ui_get_current_page()
        self.ui_goto(page_mall, confirm_wait=2.5)
        self.execute_consignment(Consignment(enable=True, buy_sale_ticket=True))
        if not self._back_to_mall_root():
            raise GameStuckError('寄售屋后回到商城根页面失败')
        self.ui_goto(page_main)

    def _back_to_mall_root(self, timeout: float = 15) -> bool:
        """退回商城根页面（底部导航栏可见的那一层）。

        必须是有界循环：商城内各屋的入口按钮都在底部导航栏上，而 MallNavbar
        里的 ui_click 没有超时，一旦起始页面不对就会永久自旋，_run_with_stat
        也救不回来。所以这里超时就返回 False 让调用方跳过。

        :return: True 已在商城根页面
        """
        timer = Timer(timeout).start()
        while 1:
            self.screenshot()
            if self.appear(self.I_CHECK_MALL):
                return True
            if timer.reached():
                return False
            if self.appear_then_click(self.I_UI_BACK_YELLOW, interval=3):
                continue

    def _buy_medal_black_daruma(self):
        """勋章屋购买黑蛋（480 勋章，每周限购一颗）。

        买不到的情况（按钮未出现/本周剩余为 0/勋章不足）由 buy_mall_one
        内部判断并跳过，这里不用重复检查。
        买完停在勋章页，必须退回商城根页面，否则后续寄售屋点不到底部导航栏
        的入口，_enter_consignment 的 ui_click 无超时会死循环。
        """
        logger.hr('Medal black daruma', 2)
        self.execute_medal(MedalRoom(enable=True, black_daruma=True))
        if not self._back_to_mall_root():
            logger.warning('勋章屋后回到商城根页面失败')

    def run_mysteryshop(self):
        # Reset per-account evidence, including after a previous account failed.
        self._mshop_result = dict(state='ENTER', requests=0, confirms=0, completed=0,
                                  outcome='FAILED', reason='NOT_FINISHED',
                                  refresh_outcome='NOT_ATTEMPTED', dialog_verified=False)
        self._mshop_ocr_cache = {}
        try:
            self._mshop_capture()
            if self.ui_get_current_page() != page_main:
                if not self.ui_goto(page_main):
                    raise MshopRefreshError('ENTER_MAIN_FAILED')
            if not self.ui_goto(page_mall):
                raise MshopRefreshError('ENTER_MALL_FAILED')
            deadline, next_click = time.monotonic() + 10, 0.0
            while True:
                self._mshop_capture()
                now = time.monotonic()
                if now >= deadline:
                    raise MshopRefreshError('ENTER_SHOP_TIMEOUT')
                if self._mshop_overlay():
                    raise MshopRefreshError('UNEXPECTED_ENTRY_DIALOG')
                if self._mshop_shelf_visible():
                    break
                if now >= next_click:
                    self.appear_then_click(MysteryShopAssets.I_ME_ENTER)
                    next_click = now + 2
            logger.info('Mysteryshop')
            self._save_mshop_page(1)
            self._mshop_capture()
            initial_button = self._mshop_button_state()
            self._mshop_result['initial_button'] = initial_button
            self._mshop_result['state'] = 'SCAN_ITEMS'
            found = self.MsFind()  # Always keep the existing goods scan/notification path.
            if initial_button == 'DISABLED':
                self._mshop_result.update(reason='ALREADY_REFRESHED', refresh_outcome='ALREADY_REFRESHED')
            elif found:
                self._mshop_result.update(reason='ITEM_FOUND', refresh_outcome='SKIPPED_ITEM_FOUND')
            elif self._mshop_refresh_once():
                self._save_mshop_page(2)
                self.MsFind()
            if not self._mshop_exit():
                raise GamePageUnknownError('MSHOP RETURN_MAIN_UNVERIFIED')
            outcome = ('ALREADY_REFRESHED' if self._mshop_result['refresh_outcome'] == 'ALREADY_REFRESHED'
                       else 'SUCCESS')
            self._mshop_result.update(state='DONE', outcome=outcome, task_outcome='SUCCESS', return_main=True)
        except MshopRefreshError as error:
            self._mshop_result.update(outcome='FAILED', reason=str(error), refresh_outcome='UNKNOWN')
            logger.warning(f'[MSHOP] {self._mshop_result}')
            # Cancel only a positively identified shop dialog; never use generic
            # navigation to auto-confirm an unknown consumption overlay.
            try:
                recovered = self._mshop_exit(cancel_refresh=True)
            except Exception:
                recovered = False
            self._mshop_result['return_main'] = recovered
            if not recovered:
                # Preserve existing failed-progress + scheduler recovery semantics.
                raise GamePageUnknownError(f'MSHOP {error}; RECOVERY_UNVERIFIED') from error
            raise
        except Exception as error:
            self._mshop_result.update(outcome='FAILED', reason=type(error).__name__, refresh_outcome='UNKNOWN')
            logger.warning(f'[MSHOP] {self._mshop_result}')
            raise  # Real device errors keep their original recovery classification.
        logger.info(f'[MSHOP] {self._mshop_result}')

    @staticmethod
    def _mshop_crop(image, roi):
        x, y, w, h = roi
        return image[y:y+h, x:x+w]

    def _mshop_text(self, image, roi):
        crop = self._mshop_crop(image, roi)
        # Reuse only byte-identical local crops, never a result from different pixels.
        cache = getattr(self, '_mshop_ocr_cache', None)
        if cache is None:
            cache = self._mshop_ocr_cache = {}
        key = (crop.shape, crop.tobytes())
        if key not in cache:
            text, score = self.O_EXP_DAILOG.model.ocr_single_line(crop)
            if len(cache) >= 16:
                cache.clear()
            cache[key] = (re.sub(r'\s+', '', text).replace('（', '(').replace('）', ')')
                          if score >= 0.92 else '')
        return cache[key]

    def _mshop_offer(self, image, roi):
        match = re.fullmatch(r'([1-9]\d*)\(仅(\d+)次\)', self._mshop_text(image, roi))
        return tuple(map(int, match.groups())) if match else None

    def _mshop_cost(self, image, roi):
        match = re.fullmatch(r'[xX×]([1-9]\d*)', self._mshop_text(image, roi))
        return int(match[1]) if match else None

    def _mshop_overlay(self):
        return self._mshop_cached('overlay', lambda:
                                 self.appear(self._MS_CONFIRM) or self.appear(self.I_M_PICTURE_REFUSE))

    def _mshop_shelf_visible(self):
        return self._mshop_cached('shelf', lambda:
                                 not self._mshop_overlay() and self.appear_rgb(MysteryShopAssets.I_MS_SHARE))

    def _mshop_capture(self):
        self.screenshot()
        self._mshop_frame_cache = {}

    def _mshop_cached(self, key, compute):
        cache = getattr(self, '_mshop_frame_cache', None)
        if cache is None:
            return compute()
        if key not in cache:
            cache[key] = compute()
        return cache[key]

    @staticmethod
    def _mshop_color_hist(image):
        hist = cv2.calcHist([image], [0,1,2], None, [16,16,16], [0,256]*3)
        return cv2.normalize(hist, hist).flatten()

    @cached_property
    def _mshop_button_references(self):
        return [(state, rule.image[8:64, :70], self._mshop_color_hist(rule.image[8:64, :70]))
                for state, rule in [('ENABLED', self._MS_REFRESH_ENABLED),
                                    ('DISABLED', self._MS_REFRESH_DISABLED)]]

    def _mshop_button_evidence(self):
        def classify():
            image = self.device.image
            result = dict(state='UNKNOWN', enabled_ncc=None, disabled_ncc=None,
                          enabled_hist=None, disabled_hist=None, border_score=None)
            if image.shape != (720,1280,3) or not self._mshop_shelf_visible():
                return result
            # The arrow animates. Its NCC cannot distinguish dimming (cross-score
            # ~0.994), so use a static border plus absolute RGB/histogram evidence.
            border = self._MS_REFRESH_ENABLED.image[:9, 4:68]
            region = self._mshop_crop(image, (1197,523,68,13))
            result['border_score'] = cv2.minMaxLoc(cv2.matchTemplate(region, border, cv2.TM_CCOEFF_NORMED))[1]
            crop = self._mshop_crop(image, (1195,533,70,56))
            hist = self._mshop_color_hist(crop)
            candidates = []
            for state, reference, ref_hist in self._mshop_button_references:
                name = state.lower()
                result[name+'_ncc'] = float(cv2.matchTemplate(crop, reference, cv2.TM_CCOEFF_NORMED)[0,0])
                distance = cv2.compareHist(hist, ref_hist, cv2.HISTCMP_BHATTACHARYYA)
                delta = float(np.abs(crop.mean((0,1))-reference.mean((0,1))).max())
                result[name+'_hist'] = distance
                result[name+'_color_delta'] = delta
                if result['border_score'] >= 0.98 and distance <= 0.15 and delta <= 8:
                    candidates.append(state)
            if len(candidates) == 1:
                result['state'] = candidates[0]
            return result
        return self._mshop_cached('button', classify)

    def _mshop_button_state(self):
        return self._mshop_button_evidence()['state']

    def _mshop_limit_reached(self):
        return self._mshop_cached('limit', lambda: self.appear(self._MS_REFRESH_LIMIT))

    def _mshop_completion_evidence(self, button, before_offer, current_offer):
        evidence = self._mshop_result
        chain = (evidence.get('initial_button') == 'ENABLED' and evidence.get('requests') == 1
                 and evidence.get('confirms') == 1 and evidence.get('dialog_verified') is True)
        if not chain or button == 'UNKNOWN':
            return False
        # DISABLED is a positively identified state, not a failed ENABLED match.
        # Still-enabled success needs a positively read opportunity reduction.
        return (button == 'DISABLED' or
                (button == 'ENABLED' and current_offer is not None
                 and current_offer[1] < before_offer[1]))

    def _mshop_dialog(self):
        # Keep the legacy guarded path. The fallback is local to this shop flow,
        # and still requires the entire title, jade icon, cost and button layout.
        legacy = self.appear(self.I_MS_ENSURE)
        confirm = self.appear(self._MS_CONFIRM)
        cancel = self.appear(self.I_M_PICTURE_REFUSE)
        title = self._mshop_text(self.device.image, self._MS_TITLE_ROI)
        cost = self._mshop_cost(self.device.image, self._MS_COST_ROI)
        template = self.I_MS_ENSURE.guard.image[:, 302:346]
        region = self._mshop_crop(self.device.image, (746, 289, 58, 57))
        _, jade_score, _, _ = cv2.minMaxLoc(cv2.matchTemplate(region, template, cv2.TM_CCOEFF_NORMED))
        cx, cy, cw, ch = self._MS_CONFIRM.roi_front
        x, y, w, h = self.I_M_PICTURE_REFUSE.roi_front
        layout = confirm and cancel and x+w < cx and abs((y+h/2)-(cy+ch/2)) <= 16
        valid = bool(layout and title == '刷新神秘商店货物需消耗' and jade_score >= 0.95 and cost)
        return dict(valid=valid, cost=cost, legacy=legacy, jade_score=jade_score)

    def _mshop_refresh_once(self):
        self._mshop_result['state'] = 'CHECK_REFRESH'
        deadline = time.monotonic() + self._MS_BUTTON_TIMEOUT
        while True:
            self._mshop_capture()
            if time.monotonic() >= deadline:
                raise MshopRefreshError('REFRESH_BUTTON_UNKNOWN')
            if self._mshop_overlay() or not self._mshop_shelf_visible():
                raise MshopRefreshError('SHELF_NOT_VISIBLE')
            button = self._mshop_button_state()
            if button == 'DISABLED':
                self._mshop_result.update(reason='ALREADY_REFRESHED', refresh_outcome='ALREADY_REFRESHED')
                return False
            if self._mshop_limit_reached():
                self._mshop_result.update(reason='REFRESH_LIMIT_REACHED', refresh_outcome='LIMIT_REACHED')
                return False
            if button == 'ENABLED':
                break
        # The deployed resources are the legacy policy, not the example screenshot.
        # Read both independently; never broaden the accepted fee/opportunity.
        resource = self.I_MS_REFRESH.image
        allowed = self._mshop_offer(resource, (22, 0, resource.shape[1]-22, resource.shape[0]))
        guard = self.I_MS_ENSURE.guard.image
        legacy_cost = self._mshop_cost(guard, (347, 0, guard.shape[1]-347, guard.shape[0]))
        offer = self._mshop_offer(self.device.image, self._MS_OFFER_ROI)
        if not allowed or allowed[0] != legacy_cost or offer != allowed or offer[1] <= 0:
            raise MshopRefreshError('OFFER_OUTSIDE_LEGACY_POLICY')
        before = self._mshop_crop(self.device.image, self._MS_SHELF_ROI).copy()
        self._mshop_result.update(state='WAIT_DIALOG', requests=1, initial_button='ENABLED',
                                  refresh_outcome='PENDING')
        deadline = time.monotonic() + self._MS_DIALOG_TIMEOUT
        self.click(self.C_MS_REFRESH_ACTION)  # One request, even if delivery is uncertain.
        seen, stable = False, 0
        while True:
            self._mshop_capture()
            if time.monotonic() >= deadline:
                raise MshopRefreshError('WAIT_DIALOG_TIMEOUT')
            if self._mshop_overlay():
                seen = True
                evidence = self._mshop_dialog()
                if not evidence['valid']:
                    raise MshopRefreshError('UNRECOGNIZED_REFRESH_DIALOG')
                if evidence['cost'] != offer[0]:
                    raise MshopRefreshError('DIALOG_FEE_CHANGED')
                stable += 1
                if stable >= 2:
                    # Recheck elapsed time after OCR, immediately before input.
                    if time.monotonic() >= deadline:
                        raise MshopRefreshError('VERIFY_DIALOG_TIMEOUT')
                    self._mshop_result.update(state='WAIT_RESULT', confirms=1, dialog_verified=True)
                    self.click(self._MS_CONFIRM)  # Never resend a consumption confirmation.
                    break
            elif seen:
                raise MshopRefreshError('DIALOG_DISAPPEARED_BEFORE_CONFIRM')
            elif self._mshop_limit_reached():
                raise MshopRefreshError('LIMIT_REACHED_BEFORE_CONFIRM')
        deadline = time.monotonic() + self._MS_RESULT_TIMEOUT
        last, stable_since, frames = None, 0.0, 0
        while True:
            self._mshop_capture()
            now = time.monotonic()
            if now >= deadline:
                raise MshopRefreshError('REFRESH_COMPLETION_UNVERIFIED')
            if not self._mshop_shelf_visible():
                last, frames = None, 0
                continue
            shelf = self._mshop_crop(self.device.image, self._MS_SHELF_ROI).copy()
            button = self._mshop_button_state()
            current_offer = (self._mshop_offer(self.device.image, self._MS_OFFER_ROI)
                             if button == 'ENABLED' else None)
            if time.monotonic() >= deadline:
                raise MshopRefreshError('REFRESH_COMPLETION_UNVERIFIED')
            consumed = self._mshop_completion_evidence(button, offer, current_offer)
            self._mshop_result['final_button'] = button
            if not consumed and self._mshop_limit_reached():
                raise MshopRefreshError('LIMIT_WITHOUT_COMPLETION_EVIDENCE')
            changed = np.abs(shelf.astype(np.int16)-before.astype(np.int16)).mean() > 2
            if not consumed or not changed:
                last, frames = None, 0
                continue
            if last is None or np.abs(shelf.astype(np.int16)-last.astype(np.int16)).mean() > 1.5:
                stable_since, frames = now, 1
            else:
                frames += 1
            last = shelf
            if frames >= 2 and now-stable_since >= 0.3:
                self._mshop_result.update(state='SCAN_ITEMS', completed=1, reason='REFRESH_VERIFIED',
                                          refresh_outcome='SUCCESS')
                return True

    def _mshop_exit(self, cancel_refresh=False):
        deadline, next_click, backs, cancelled = time.monotonic()+self._MS_EXIT_TIMEOUT, 0.0, 0, False
        while True:
            self._mshop_capture()
            now = time.monotonic()
            if now >= deadline:
                return False
            if self._mshop_overlay():
                if cancel_refresh and not cancelled and self._mshop_dialog()['valid']:
                    cancelled = True
                    self.click(self.I_M_PICTURE_REFUSE)
                    continue
                return False
            if self.appear_rgb(self.I_CHECK_MAIN):
                return True
            if now >= next_click and backs < 3 and (
                    self._mshop_shelf_visible() or self.appear_rgb(self.I_CHECK_MALL)):
                if self.appear_rgb(self.I_BACK_MAIN):
                    self.click(self.I_BACK_MAIN)
                elif self.appear_rgb(self.I_BACK_Y):
                    self.click(self.I_BACK_Y)
                backs += 1
                next_click = now + 1

    def _save_mshop_page(self, index: int):
        """保存神秘商店货架截图到 screenshots/mshop/，文件名 商店_角色名_{index}.png。

        序号1 = 进入商店时的初始货架；序号2 = 刷新后的货架（多次刷新时覆盖为最新）。
        角色名优先取多账号运行注入的统计上下文（_stat_ctx），单实例退化为配置实例名。
        """
        # 角色名优先取多账号运行注入的统计上下文，单实例运行时退化为配置实例名
        char_name = (getattr(self, '_stat_ctx', None) or {}).get('char') or self.config.config_name
        # 替换 Windows 文件名非法字符，避免保存失败
        char_name = re.sub(r'[\\/:*?"<>|]', '_', str(char_name))
        image = self.screenshot()
        save_dir = Path('screenshots/mshop')
        save_dir.mkdir(parents=True, exist_ok=True)
        save_path = save_dir / f'商店_{char_name}_{index}.png'
        save_image(image, str(save_path))
        logger.info(f'神秘商店货架截图已保存: {save_path}')

    def _ocr_price(self, slot: int) -> int:
        """读取指定格位的价格数字，返回 0 表示识别失败。

        ROI 由 price_rule 按点阵现造，不再走 assets.py 的 O_MS_PRICENUM_*。

        必须显式兜底 detect_and_ocr：Digit 模式下 after_process 把空串转成 int 0
        （module/ocr/sub_ocr.py:121），而 ocr_single 的判空是 `!= ""`
        （module/ocr/sub_ocr.py:86），`0 != ""` 恒真，导致 RuleOcr.ocr 内部那段
        竖排 / detect_and_ocr 兜底对 Digit 模式是死代码。
        """
        rule = price_rule(slot)
        price = rule.ocr(self.device.image)
        if price:
            return int(price)
        results = rule.detect_and_ocr(self.device.image)
        if results:
            return int(results[0].ocr_text)
        return 0

    def _scan_slots(self, flower: int) -> list[SlotItem]:
        """扫描当前货架，返回识别到的商品。要求已在神秘商店页面且已截图。

        每类货物在总区域做一次多点匹配（NMS 去重叠）→ 命中框中心映射到格位
        → 同格冲突取匹配得分高者 → 只对有命中的格位 OCR 价格
        → 用价格校正御行达摩碎片/黑蛋的交叉误判 → 丢掉当前花数未解锁的货。
        价格 OCR 失败的格位直接丢弃：价格未知无法判定，宁漏不错。

        :param flower: 账号花数（isflower），决定扫哪几类货
        :return: 按格位号升序的商品列表
        """
        image = self.device.image
        # 格位号 -> (货物类型, 匹配得分)，同格只保留得分最高的一条
        best: dict[int, tuple[GoodsType, float]] = {}
        for goods in enabled_goods(flower):
            # 只调 match_all_any：assets 实例是共享类属性，match() 会写脏 roi_front
            rule = getattr(self, GOODS_ASSETS[goods])
            matches = rule.match_all_any(image)
            for score, x, y, w, h in matches:
                cx, cy = x + w // 2, y + h // 2
                slot = locate_slot(cx, cy)
                if slot is None:
                    logger.warning(f'命中中心 ({cx},{cy}) 落在格位网格外，跳过')
                    continue
                exist = best.get(slot)
                if exist is None:
                    best[slot] = (goods, score)
                    continue
                # 同格命中两种货物，必有一个是误匹配，留日志便于回溯
                keep = goods if score > exist[1] else exist[0]
                logger.warning(f'格位 {slot} 同时命中 {exist[0].name} 与 {goods.name}，'
                               f'保留得分高的 {keep.name}')
                if keep is goods:
                    best[slot] = (goods, score)

        items: list[SlotItem] = []
        for slot in sorted(best):
            goods, score = best[slot]
            price = self._ocr_price(slot)
            if price <= 0:
                logger.warning(f'格位 {slot}（{goods.name}）价格 OCR 失败，跳过该格')
                continue
            # 御行达摩碎片与黑蛋图标相近，以价格为准做二次校正
            refined = refine_goods(goods, price)
            if refined is not goods:
                logger.info(f'格位 {slot} 按价格 {price} 从 {GOODS_NAMES[goods]} '
                            f'校正为 {GOODS_NAMES[refined]}')
            if not is_unlocked(refined, flower):
                logger.info(f'格位 {slot} 是{GOODS_NAMES[refined]}，'
                            f'当前 {flower} 花未解锁，跳过')
                continue
            items.append(SlotItem(slot=slot, goods=refined, price=price,
                                  coin=coin_of(price), score=score))
        return items

    def MsFind(self) -> bool:
        """扫描当前神秘商店货架，播报规则判定要买的商品。

        :return: True 表示有要推送的商品；run_mysteryshop 据此决定是否刷新商店。
        """
        flower = self.get_config().daily_alt_acc_config.isflower
        self.screenshot()
        items = self._scan_slots(flower)
        logger.info(f'神秘商店（{flower}花）扫描到 {len(items)} 件商品: '
                    f'{[(i.slot, i.goods.name, i.coin.name, i.price) for i in items]}')
        found = False
        for item in items:
            if not self._should_buy(item.goods, item.coin, item.price, item.slot):
                continue
            found = True
            self._notify_item(item)
            # TODO 购买：点 price_roi(slot) 这块 ROI（价格条就是购买按钮），
            #      确认弹窗用 I_MS_ENSURE（货物无关的「确定」按钮），
            #      每格一件走 Buy.buy_one 而非 buy_more
        if not found:
            logger.info('没有找到物品')
        return found

    def _should_buy(self, goods: GoodsType, coin: CoinType, price: int, slot: int) -> bool:
        """判定该格位商品是否值得留下。

        这个判定有两重作用，改规则时两边都会一起变：
        1. 推送：命中的货走 _notify_item 播报 / 汇总；
        2. 是否刷新商店：MsFind 全部未命中才返回 False，run_mysteryshop 据此
           刷新货架；有任何一件命中就停止刷新，保住这件货。
           旧流程确认成功即结束，3 是分享按钮轮询计数，不是三笔消费授权。

        规则沿用原 InfoFilter，另外补上两类新货：

        - 金币价的大蛇的逆鳞 / 逢魔之魂：一律要
        - 勾玉价的御行达摩碎片：只要 20<价<45、70<价<96、价>=120 三段
        - 神秘符咒（蓝票）：价格 50~60（含端点）
        - 御行达摩（黑蛋）：只要判定为黑蛋就要，不看价格

        :param goods: 货物类型（已按价格校正过碎片/黑蛋）
        :param coin: 币种，由价格数值判定（>10000 金币，否则勾玉）
        :param price: 价格数值，调用方保证 > 0
        :param slot: 格位号 1..8
        :return: True 表示值得留下（推送且不再刷新）
        """
        # 金币价的大蛇的逆鳞与逢魔之魂，一律要
        if coin == CoinType.gold and goods in (GoodsType.orochi_scale, GoodsType.demon_soul):
            return True
        # 勾玉价的御行达摩碎片，只要特定的三段价格区间
        if coin == CoinType.jade and goods == GoodsType.skill_shard:
            return 20 < price < 45 or 70 < price < 96 or price >= 120
        # 神秘符咒（蓝票），50~60 才推
        if goods == GoodsType.mystery_amulet:
            return 50 <= price <= 60
        # 御行达摩（黑蛋）本身稀有，判定出来就推，不设价格条件
        if goods == GoodsType.black_daruma:
            return True
        return False

    def _notify_item(self, item: SlotItem) -> None:
        """播报一件命中商品，msg / push_notify / emit_stat 三个通道全发。

        原实现按货物类型分通道，分支依据已移入 _should_buy，通道层不再区分类型。
        emit_stat 的 goods 字段传中文名而非枚举英文名，面板上不用再翻译一层。
        emit_stat 用 getattr 守卫：Mshop 不一定混入了 StatLogMixin。

        msg 载荷是结构化 dict（与 cooperation.py 同构）：多账号模式下
        MultiDailyAltAcc 落盘到本轮进度，整轮完成时统一汇总一条推送；
        单账号模式仍靠这里的 push_notify 即时提醒。
        """
        goods_name = GOODS_NAMES[item.goods]
        coin_name = COIN_NAMES[item.coin]
        content = f'发现{item.price}{coin_name}{goods_name}'
        logger.info(f'格位 {item.slot} {content}')
        self.msg.append([MSGType.mshop, {
            'goods': goods_name,
            'coin': coin_name,
            'price': item.price,
            'slot': item.slot,
            'label': content,
        }])
        self.push_notify(content=f' {content}', title='神秘商店提醒')
        emit_stat = getattr(self, 'emit_stat', None)
        if emit_stat:
            emit_stat(StatEvent.MSHOP, goods=goods_name, price=item.price)


if __name__ == "__main__":
    from module.config.config import Config
    from module.device.device import Device
    c = Config('oas3')
    d = Device(c)
    self = Mshop(c, d)
    self.screenshot()
    self.run_mysteryshop()
