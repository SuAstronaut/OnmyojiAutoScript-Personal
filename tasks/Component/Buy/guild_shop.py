"""Bounded merit-shop purchases for MultiDaily blue tickets and autumn leaves.

No account receipt or mutable asset coordinates are retained between calls.
Missing blue goods are UNKNOWN. Autumn-leaf completion follows the verified
activity rule: the first card changes to a recognized normal product.
"""
import copy
import re
import time
from dataclasses import dataclass
from enum import Enum

from module.atom.click import RuleClick
from module.atom.image import RuleImage
from module.atom.ocr import RuleOcr
from module.logger import logger
from tasks.Component.Buy.assets import BuyAssets
from tasks.DailyAltAcc.assets import DailyAltAccAssets
from tasks.RichMan.assets import RichManAssets
from tasks.GameUi.page import page_main, page_theme, page_guild


class ShopState(str, Enum):
    AVAILABLE = 'AVAILABLE'
    COMPLETED = 'COMPLETED'
    UNAVAILABLE = 'UNAVAILABLE'
    UNKNOWN = 'UNKNOWN'


class ShopSafetyError(RuntimeError):
    """Business failure: existing MultiDaily progress handling skips this subtask."""


@dataclass(frozen=True)
class ShopItem:
    name: str
    x: int
    y: int
    remaining: int
    price: int


class GuildShopSafety:
    SHOP_NAMES = {
        'blue': ('神秘的符咒', '神秘符咒'),
        'leaf': ('秋韵红叶',),
    }
    SHELF_ROI = (280, 160, 660, 460)
    LEAF_FIRST_CARD_ROI = (280, 160, 205, 230)
    LEAF_FIRST_TITLE_ROI = (300, 160, 180, 65)
    # Explicit known goods, not "anything other than leaf". The autumn event
    # never relocates an available leaf to another card (human-verified rule).
    LEAF_COMPLETED_FIRST_GOODS = frozenset((
        '招福达摩', '功勋礼包', '樱饼', '櫻饼',
        '风铃', '经验手札·中', '奉为达摩',
    ))

    def _shop_frame(self):
        self.screenshot()
        self._shop_ocr_cache = {}

    @staticmethod
    def _shop_normalize(text):
        return re.sub(r'\s+', '', str(text))

    def _shop_text(self, roi):
        # Local ROI, current frame only. Full mode preserves empty/invalid numbers.
        cache = getattr(self, '_shop_ocr_cache', {})
        if roi in cache:
            return cache[roi]
        rule = RuleOcr(roi=roi, area=(0, 0, 100, 100), mode='Full',
                       method='Default', keyword='', name='guild_shop_safety')
        result = rule.detect_and_ocr(self.device.image)
        cache[roi] = result
        self._shop_ocr_cache = cache
        return result

    def _shop_has_text(self, roi, text):
        return any(box.score >= 0.9 and self._shop_normalize(box.ocr_text) == text
                   for box in self._shop_text(roi))

    def _shop_number(self, roi):
        values = [self._shop_normalize(b.ocr_text) for b in self._shop_text(roi)
                  if b.score >= 0.9]
        if len(values) != 1 or not re.fullmatch(r'\d+', values[0]):
            # Detection can miss a lone "1". Recognition-only fallback keeps
            # the raw text (Digit mode would turn an empty result into zero).
            rule = RuleOcr(roi=roi, area=(0, 0, 100, 100), mode='Full',
                           method='Default', keyword='', name='guild_shop_number')
            rule.score = 0.9
            text = self._shop_normalize(rule.ocr_single_line(self.device.image))
            return int(text) if re.fullmatch(r'\d+', text) else None
        return int(values[0])

    def _shop_match(self, rule):
        # RuleImage.match() writes roi_front; assets are shared class attributes.
        local = copy.copy(rule)
        local.roi_front = list(rule.roi_front)
        return local if local.match(self.device.image) else None

    def _shop_money(self):
        roi = (942, 6, 120, 48)
        value = self._shop_number(roi)
        if value is not None:
            return value
        # Preserve the former Quantity OCR's abbreviated merit display support.
        # Quotas, order quantities and prices remain strict integer fields.
        texts = [self._shop_normalize(b.ocr_text) for b in self._shop_text(roi)
                 if b.score >= 0.9]
        if len(texts) == 1:
            found = re.fullmatch(r'(\d+(?:\.\d+)?)([万亿])', texts[0])
            if found:
                return int(float(found[1]) * (10000 if found[2] == '万' else 100000000))
        return None

    def _shop_click(self, roi, name):
        self.click(RuleClick(roi_front=roi, roi_back=roi, name=name))

    def _shop_page(self):
        if self._shop_match(BuyAssets.I_BUY_PLUS):
            return 'DIALOG'
        if self._shop_match(RichManAssets.I_GUILD_CLOSE_RED):
            if self._shop_has_text((485, 195, 290, 82), '获得奖励'):
                return 'REWARD'
            if self._shop_has_text((485, 40, 240, 66), '功勋商店'):
                return 'SHOP'
            return 'UNKNOWN'
        if self._shop_match(RichManAssets.I_GUILD_STORE):
            return 'SHRINE'
        if self._shop_match(RichManAssets.I_GUILD_SHRINE):
            return 'GUILD'
        if (self._shop_has_text((140, 10, 150, 50), '狭间暗域')
                and self._shop_has_text((1100, 585, 150, 100), '开启')):
            return 'DARK'
        return 'UNKNOWN'

    def _shop_fail(self, reason):
        logger.warning(f'[guild_shop] UNKNOWN: {reason}; no further purchase clicks')
        raise ShopSafetyError(reason)

    def _shop_recover_dark(self):
        # Caller has positively identified DARK in the current frame. One back
        # click only; never touch the lower-right start control or unknown pages.
        self._shop_click((24, 20, 32, 34), 'guild_shop_dark_back')
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            time.sleep(0.8)
            self._shop_frame()
            if self._shop_page() == 'SHRINE':
                logger.info('[guild_shop] DARK -> SHRINE; recheck live shop stock')
                return
        self._shop_fail('Dark-area back did not reach shrine within 8s')

    def _shop_enter(self):
        self._shop_frame()
        page = self._shop_page()
        if page == 'UNKNOWN' and (self.ui_page_appear(page_main)
                                  or self.ui_page_appear(page_theme)):
            if not self.ui_goto(page_guild, timeout=25):
                self._shop_fail('Known-page guild navigation failed')
        deadline = time.monotonic() + 25
        clicked = set()
        recoveries = 0
        while time.monotonic() < deadline:
            self._shop_frame()
            page = self._shop_page()
            if page == 'SHOP':
                return
            if page == 'DARK':
                if recoveries >= 2:
                    self._shop_fail('Dark-area recovery limit (2) reached')
                recoveries += 1
                self._shop_recover_dark()
                clicked.discard('SHRINE')
                continue
            if page in ('GUILD', 'SHRINE') and page not in clicked:
                rule = (RichManAssets.I_GUILD_SHRINE if page == 'GUILD'
                        else RichManAssets.I_GUILD_STORE)
                button = self._shop_match(rule)
                if button:
                    self._shop_click(tuple(button.roi_front), 'guild_shop_' + page.lower())
                    clicked.add(page)
            # UNKNOWN/loading: wait with no action. No deadline resets.
            time.sleep(0.8)
        self._shop_fail(f'Shop entry timeout, page={page}')

    def _shop_find_item(self, target):
        boxes = self._shop_text(self.SHELF_ROI)
        names = self.SHOP_NAMES[target]
        hits = [b for b in boxes if b.score >= 0.9
                and self._shop_normalize(b.ocr_text) in names]
        if len(hits) != 1:
            return None
        box = hits[0]
        x = int(sum(float(p[0]) for p in box.box) / 4) + self.SHELF_ROI[0]
        y = int(sum(float(p[1]) for p in box.box) / 4) + self.SHELF_ROI[1]
        # Associate the quota with this card, not a fixed slot or another item.
        quotas = []
        for b in boxes:
            bx = sum(float(p[0]) for p in b.box) / 4 + self.SHELF_ROI[0]
            by = sum(float(p[1]) for p in b.box) / 4 + self.SHELF_ROI[1]
            if b.score < 0.9 or abs(bx - x) > 95 or not 80 < by - y < 155:
                continue
            found = re.fullmatch(r'本周剩余数量[:：]?(\d+)', self._shop_normalize(b.ocr_text))
            if found:
                quotas.append(int(found[1]))
        if len(quotas) != 1 or y + 195 > 620:
            return None  # incomplete/scrolled card: do not guess zero
        price = self._shop_number((x - 15, y + 145, 110, 48))
        if price is None or price <= 0:
            return None
        if target == 'blue':
            icon = copy.copy(RichManAssets.I_GUILD_BLUE)
            icon.roi_front = list(icon.roi_front)
            icon.roi_back = (x - 60, y + 20, 120, 90)
            if not icon.match(self.device.image):
                return None
        return ShopItem(self._shop_normalize(box.ocr_text), x, y, quotas[0], price)

    def _shop_scan(self, target):
        # Eight viewport observations at most, including already-sold-out cases.
        for index in range(8):
            self._shop_frame()
            page = self._shop_page()
            if page == 'DARK':
                self._shop_enter()
                continue  # inventory is reread, no stale completed flag
            if page != 'SHOP':
                self._shop_fail(f'{target}: shelf obscured or unknown ({page})')
            item = self._shop_find_item(target)
            if item is not None:
                return item
            if index < 7:
                self.swipe(RichManAssets.S_GUILD_STORE)
                time.sleep(0.8)
        return None

    def _shop_leaf_first_state(self):
        """Read the current first card; never scan/swipe to find autumn leaves."""
        if self._shop_page() != 'SHOP':
            return ShopState.UNKNOWN, None
        boxes = self._shop_text(self.LEAF_FIRST_CARD_ROI)
        # Only the first card's title band counts. A quota/description or leaf
        # text elsewhere in the shop is not a first-product identity.
        titles = [b for b in boxes if b.score >= 0.9
                  and 5 < sum(float(p[1]) for p in b.box) / 4 < 60]
        if len(titles) > 1:
            return ShopState.UNKNOWN, None
        if titles:
            title = titles[0]
            name = self._shop_normalize(title.ocr_text)
            x = int(sum(float(p[0]) for p in title.box) / 4) + self.LEAF_FIRST_CARD_ROI[0]
            y = int(sum(float(p[1]) for p in title.box) / 4) + self.LEAF_FIRST_CARD_ROI[1]
        else:
            # The native two-character sakura title can fail detection confidence
            # while recognition-only reads it reliably. Still require >=0.9 and
            # an exact known name; never broaden the region or lower confidence.
            rule = RuleOcr(roi=self.LEAF_FIRST_TITLE_ROI, area=(0, 0, 100, 100),
                           mode='Full', method='Default', keyword='', name='guild_shop_leaf_title')
            rule.score = 0.9
            name = self._shop_normalize(rule.ocr_single_line(self.device.image))
            x, y = 380, 185  # center of the verified fixed first-product title
        if name in self.LEAF_COMPLETED_FIRST_GOODS:
            logger.info(f'[guild_shop] leaf: COMPLETED (first product={name})')
            return ShopState.COMPLETED, None
        if name != '秋韵红叶':
            return ShopState.UNKNOWN, None
        quotas = []
        for b in boxes:
            bx = sum(float(p[0]) for p in b.box) / 4 + self.LEAF_FIRST_CARD_ROI[0]
            by = sum(float(p[1]) for p in b.box) / 4 + self.LEAF_FIRST_CARD_ROI[1]
            if b.score < 0.9 or abs(bx - x) > 95 or not 80 < by - y < 155:
                continue
            found = re.fullmatch(r'本周剩余数量[:：]?(\d+)', self._shop_normalize(b.ocr_text))
            if found:
                quotas.append(int(found[1]))
        if len(quotas) != 1:
            return ShopState.UNKNOWN, None
        if quotas[0] == 0:
            return ShopState.COMPLETED, None
        price = self._shop_number((x - 15, y + 145, 110, 48))
        if price is None or price <= 0:
            return ShopState.UNKNOWN, None
        return ShopState.AVAILABLE, ShopItem(name, x, y, quotas[0], price)

    def _shop_dialog(self, target):
        plus = self._shop_match(BuyAssets.I_BUY_PLUS)
        if plus is None:
            return None
        names = [self._shop_normalize(b.ocr_text)
                 for b in self._shop_text((440, 115, 390, 70)) if b.score >= 0.9]
        if len(names) != 1 or names[0] not in self.SHOP_NAMES[target]:
            return None
        return plus

    def _shop_reward_matches(self, target):
        if target == 'blue':
            rule = copy.copy(RichManAssets.I_GUILD_BLUE)
            rule.roi_front = list(rule.roi_front)
        else:
            rule = RuleImage(roi_front=(348, 219, 52, 44), roi_back=(550, 290, 180, 150),
                             method='Template matching', threshold=0.85,
                             file='./tasks/DailyAltAcc/tree/autumn_leaf.png')
        rule.roi_back = (550, 290, 180, 150)
        return rule.match_multi_scale(self.device.image, scales=[0.9, 1.0, 1.1, 1.2, 1.3])

    def _shop_purchase(self, target):
        # Retry only a positively identified navigation error BEFORE submission.
        # Each attempt rereads account stock. All other unknown states stop.
        for _ in range(2):
            state = self._shop_purchase_once(target)
            if state != 'RECHECK':
                return state
        return ShopState.UNKNOWN

    def _shop_purchase_once(self, target):
        self._shop_enter()
        if target == 'leaf':
            state, item = self._shop_leaf_first_state()
            if state != ShopState.AVAILABLE:
                return state
        else:
            item = self._shop_scan(target)
        if item is None:
            logger.warning(f'[guild_shop] {target}: UNKNOWN (not found in bounded scan)')
            return ShopState.UNKNOWN
        if item.remaining == 0:
            logger.info(f'[guild_shop] {target}: COMPLETED (explicit weekly quota=0)')
            return ShopState.COMPLETED
        money = self._shop_money()
        minimum = 240 if target == 'blue' else item.price
        if money is None:
            return ShopState.UNKNOWN
        if money < max(minimum, item.price):
            logger.info(f'[guild_shop] {target}: UNAVAILABLE (insufficient merit)')
            return ShopState.UNAVAILABLE
        logger.info(f'[guild_shop] {target}: AVAILABLE remaining={item.remaining}')
        self._shop_click((item.x - 18, item.y + 45, 36, 36), 'guild_shop_' + target)
        deadline = time.monotonic() + 8
        plus = None
        while time.monotonic() < deadline:
            time.sleep(0.8)
            self._shop_frame()
            plus = self._shop_dialog(target)
            if plus:
                break
            page = self._shop_page()
            if page == 'DARK':
                self._shop_recover_dark()
                # Nothing was submitted. Restart entry and stock inspection once
                # at the caller level; do not recursively retry purchases.
                return 'RECHECK'
            if page == 'DIALOG':
                self._shop_fail(f'{target}: wrong or unreadable purchase dialog')
        if plus is None:
            self._shop_fail(f'{target}: purchase dialog timeout')
        # Preserve max-purchase semantics: original blue path pressed max twice.
        for _ in range(2 if target == 'blue' else 1):
            self._shop_click(tuple(plus.roi_front), 'guild_shop_max')
            time.sleep(0.8)
            self._shop_frame()
            plus = self._shop_dialog(target)
            if plus is None:
                if self._shop_page() == 'DARK':
                    self._shop_recover_dark()
                    return 'RECHECK'
                self._shop_fail(f'{target}: dialog changed while setting maximum')
        quantity = self._shop_number((550, plus.roi_front[1] - 10, 95, plus.roi_front[3] + 20))
        confirm = self._shop_match(DailyAltAccAssets.I_CLICK_BUY)
        total = (self._shop_number((confirm.roi_front[0] + 42, confirm.roi_front[1] - 7,
                                   100, confirm.roi_front[3] + 14)) if confirm else None)
        expected = min(item.remaining, money // item.price)
        if quantity != expected or expected <= 0 or total != quantity * item.price:
            self._shop_fail(f'{target}: max quantity/merit price validation failed')
        # Current frame has target title, max quantity, merit button and price.
        # Submit exactly once. A stalled response must never resubmit the order.
        self._shop_click((confirm.roi_front[0] - 25, confirm.roi_front[1], 125,
                          confirm.roi_front[3]), 'guild_shop_confirm_' + target)
        deadline = time.monotonic() + 12
        reward_seen = False
        while time.monotonic() < deadline:
            time.sleep(0.8)
            self._shop_frame()
            page = self._shop_page()
            if page == 'REWARD':
                if not self._shop_reward_matches(target):
                    self._shop_fail(f'{target}: reward identity does not match order')
                if not reward_seen:
                    self._shop_click((615, 610, 40, 20), 'guild_shop_reward_close')
                    reward_seen = True
            elif page == 'SHOP':
                if target == 'leaf':
                    state, remaining = self._shop_leaf_first_state()
                    if state == ShopState.COMPLETED and reward_seen:
                        return ShopState.COMPLETED
                else:
                    remaining = self._shop_find_item(target)
                if remaining and remaining.remaining == item.remaining - quantity:
                    return ShopState.COMPLETED
                if target != 'leaf' and reward_seen and remaining is None:
                    # Correlated, identity-checked reward is required for a
                    # disappearing item. Absence alone is never COMPLETED.
                    return ShopState.COMPLETED
            elif page == 'DARK':
                self._shop_recover_dark()
                self._shop_enter()
                # Recheck the quota on the next frame; never submit again.
        self._shop_fail(f'{target}: unconfirmed order after 12s; no resubmission')

    def _shop_leave(self):
        deadline = time.monotonic() + 12
        closed = False
        while time.monotonic() < deadline:
            self._shop_frame()
            page = self._shop_page()
            if page == 'SHRINE':
                return
            if page == 'SHOP' and not closed:
                button = self._shop_match(RichManAssets.I_GUILD_CLOSE_RED)
                self._shop_click(tuple(button.roi_front), 'guild_shop_close')
                closed = True
            elif page == 'DARK':
                self._shop_recover_dark()
                return
            time.sleep(0.8)
        self._shop_fail(f'Shop exit did not reach shrine, page={page}')

    def _shop_back_to_guild(self):
        self._shop_frame()
        if self._shop_page() != 'SHRINE':
            self._shop_fail('Cannot return to guild from unverified shrine')
        self._shop_click((24, 20, 32, 34), 'guild_shop_shrine_back')
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            time.sleep(0.8)
            self._shop_frame()
            if self._shop_page() == 'GUILD':
                return
        self._shop_fail('Shrine back did not reach guild')
