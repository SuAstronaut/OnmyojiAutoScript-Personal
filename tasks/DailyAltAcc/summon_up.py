# This Python file uses the following encoding: utf-8
import time

from module.atom.click import RuleClick
from module.atom.ocr import RuleOcr
from module.logger import logger
from tasks.GameUi.page import page_main, page_summon
from tasks.DailyAltAcc.utils import DailyAltAccBase


class _SummonUpStop(Exception):
    def __init__(self, outcome, reason):
        self.outcome, self.reason = outcome, reason
        super().__init__(reason)


class SummonUp(DailyAltAccBase):
    # Hard elapsed budgets, including 30s reserved for safe exit. No click resets.
    SUMMON_UP_TOTAL_TIMEOUT = 180
    SUMMON_UP_WORK_TIMEOUT = 150
    SUMMON_UP_RETURN_TIMEOUT = 30
    # All recognition/click regions are verified on native 1280x720 user PNGs.
    SUMMON_UP_TITLE_ROI = (96, 12, 190, 54)
    # User-confirmed entrance: chest/abdomen within the large left portrait.
    # Original candidate was xyxy=(230,250,325,385), not an xywh ROI.
    SUMMON_UP_PORTRAIT_ROI = (245, 270, 65, 95)
    SUMMON_UP_WRONG_TAB_ROI = (193, 391, 150, 66)
    SUMMON_UP_HOUSE_LABEL_ROI = (5, 90, 88, 95)
    SUMMON_UP_HOUSE_CLICK_ROI = (32, 128, 38, 38)
    SUMMON_UP_STATE_ROI = (318, 571, 160, 79)
    SUMMON_UP_FREE_ROI = (327, 586, 146, 55)
    SUMMON_UP_DETAIL_FREE_ROI = (553, 506, 176, 63)
    # Banner text, not the artwork or the bottom summon buttons (y >= 640).
    SUMMON_UP_EXPERIENCE_CLICK_ROI = (610, 500, 60, 30)
    SUMMON_UP_EXPERIENCE_MASK_ROIS = ((400, 0, 450, 65), (104, 320, 26, 150),
                                     (1150, 320, 26, 150))

    def _su_note(self, stage, message):
        logger.info(f'[summon_up] stage={stage} {message}')

    def _su_check(self):
        if time.monotonic() >= self._su_deadline:
            raise _SummonUpStop('FAILED', 'TOTAL_TIMEOUT')

    def _su_poll(self, stage, seconds, observe):
        self._su_stage = stage
        deadline = min(self._su_deadline, time.monotonic() + seconds)
        while time.monotonic() < deadline:
            self._su_check()
            self.screenshot()
            self._su_check()
            done = observe()
            self._su_check()
            if time.monotonic() >= deadline:
                break
            if done:
                return
            time.sleep(min(0.3, max(0, deadline - time.monotonic())))
        self._su_check()
        raise _SummonUpStop('FAILED', f'{stage}_TIMEOUT')

    def _su_click(self, rule, stage, limit=2, action=None, cooldown=4):
        """Click an already validated frame, with independent attempt/cooldown limits."""
        self._su_check()
        key = (stage, rule.name)
        count, previous = self._su_clicks.get(key, (0, -1000))
        if time.monotonic() - previous < cooldown:
            return False
        if count >= limit:
            raise _SummonUpStop('FAILED', f'{stage}_CLICK_LIMIT')
        self._su_clicks[key] = (count + 1, time.monotonic())
        self._su_note(stage, f'click={rule.name} attempt={count + 1}/{limit}')
        self.click(action or rule)
        self._su_check()
        return True

    def _su_boxes(self, roi):
        """Read exact labels in existing asset regions, without per-frame OCR logs.

        OCR reads only verified context/button regions and never expands click
        regions. Mailed login rewards and their red dots are not inspected.
        """
        self._su_check()
        rule = RuleOcr(name='summon_up_evidence', mode='Full', method='Default',
                       roi=roi, area=roi, keyword='')
        results = rule.model.detect_and_ocr(rule.crop(self.device.image, roi))
        self._su_check()
        boxes = []
        for item in results:
            if item.score < 0.9 or not item.ocr_text:
                continue
            text = ''.join(item.ocr_text.split())
            xs, ys = [p[0] for p in item.box], [p[1] for p in item.box]
            box = (min(xs) + roi[0], min(ys) + roi[1],
                   max(xs) - min(xs), max(ys) - min(ys))
            x, y, w, h = box
            if w > 0 and h > 0 and x >= roi[0] and y >= roi[1] and \
                    x + w <= roi[0] + roi[2] and y + h <= roi[1] + roi[3]:
                boxes.append((text, box))
        return boxes

    @staticmethod
    def _su_contains(box, point):
        x, y, w, h = box
        return x <= point[0] < x + w and y <= point[1] < y + h


    def _su_reward_popup(self):
        return self.appear(self.I_SUMMON_UP_PURCHASE_SUCCESS) and \
            self.appear(self.I_SUMMON_UP_COPPER_CARD)

    def _su_text(self, roi, keyword):
        return any(text == keyword for text, _ in self._su_boxes(roi))

    def _su_experience_page(self):
        title = self.appear(self.I_SUMMON_UP_EXPERIENCE_TITLE)
        prompt = self.appear(self.I_SUMMON_UP_JUMP)
        image = self.device.image
        means = [float(image[y:y+h, x:x+w].mean())
                 for x, y, w, h in self.SUMMON_UP_EXPERIENCE_MASK_ROIS] \
            if getattr(image, 'shape', ())[:2] == (720, 1280) else []
        mask = len(means) == 3 and all(value <= 45 for value in means)
        self._su_experience_evidence = dict(
            title_match=title, title_score=getattr(self.I_SUMMON_UP_EXPERIENCE_TITLE, 'last_match_score', None),
            title_roi=tuple(self.I_SUMMON_UP_EXPERIENCE_TITLE.roi_front),
            prompt_match=prompt, prompt_score=getattr(self.I_SUMMON_UP_JUMP, 'last_match_score', None),
            prompt_roi=tuple(self.I_SUMMON_UP_JUMP.roi_front), mask_means=means, mask_confirmed=mask)
        if not title and not prompt:
            return None
        # Partial evidence blocks unknown-page recovery through a possible overlay.
        return 'EXPERIENCE' if title and prompt and mask else 'UNVERIFIED_EXPERIENCE'

    def _su_dismiss_experience(self):
        self._su_stage = 'DISMISS_EXPERIENCE'
        roi = self.SUMMON_UP_EXPERIENCE_CLICK_ROI
        action = RuleClick(roi_front=roi, roi_back=roi, name='summon_up_experience_dismiss')
        count, previous = self._su_clicks.get(('DISMISS_EXPERIENCE', action.name), (0, -1000))
        if time.monotonic() - previous < 4:
            return  # Next poll takes a fresh screenshot, even during the animation wait.
        if count >= 2:
            self._su_note('DISMISS_EXPERIENCE', f'NOT_DISMISSED evidence={self._su_experience_evidence}')
            raise _SummonUpStop('FAILED', 'EXPERIENCE_NOT_DISMISSED')
        self._su_note('DISMISS_EXPERIENCE', f'evidence={self._su_experience_evidence} click_roi={roi}')
        if self._su_click(action, 'DISMISS_EXPERIENCE', limit=2):
            self._su_experience_pending = True

    def _su_page(self):
        # Popup priority prevents clicking the valid FREE button behind a modal.
        experience = self._su_experience_page()
        if experience:
            return experience
        if self._su_reward_popup():
            return 'REWARD'
        if self.appear(self.I_SUMMON_UP_COPPER_DETAIL):
            return 'DETAIL'
        if self.appear(self.I_SUMMON_UP_SELECT_POOL) or \
                self.appear(self.I_SUMMON_UP_ENSURE_POOL) or \
                self.appear(self.I_SUMMON_UP_ENSURE_POOL_2):
            return 'UNVERIFIED_DIALOG'
        if self._su_text(self.SUMMON_UP_TITLE_ROI, '祈愿福礼') and \
                self.appear(self.I_SUMMON_UP_COPPER_CARD) and \
                self._su_text(self.I_SUMMON_UP_COPPER_CARD.roi_back, '铜铃礼包'):
            return 'WELFARE'
        if self._su_text(self.SUMMON_UP_TITLE_ROI, '召唤活动') and \
                self.appear(self.I_SUMMON_UP_TO_GIFT):
            return 'ACTIVITY'
        if self.ui_page_appear(page_summon):
            # Native frame7 still matches page_summon at .948 under its mask.
            # If both overlay templates fail, darkness blocks background actions;
            # it is never sufficient evidence to click a dismissal by itself.
            if (self._su_experience_evidence or {}).get('mask_confirmed', False):
                return 'UNVERIFIED_EXPERIENCE'
            return 'SUMMON'
        if self.appear(self.I_SUMMON_GOTO_MAIN):
            labels = {text for text, _ in self._su_boxes(self.SUMMON_UP_WRONG_TAB_ROI)}
            if {'获得咲耶后', '开启召唤活动'} <= labels:
                return 'WRONG_SUMMON_TAB'
        if self.ui_page_appear(page_main):
            return 'MAIN'
        return 'UNKNOWN'

    def _su_failure_evidence(self, reason):
        stage = getattr(self, '_su_stage', 'INIT')
        self._summon_up_result['failure_stage'] = stage
        try:
            # Save the frame already examined, without another device screenshot.
            saved = self.device.save_screenshot(genre='summon_up_failure', interval=0)
            self._summon_up_result['evidence_saved'] = bool(saved)
            self._su_note('EVIDENCE', f'stage={stage} reason={reason} saved={saved} folder=log/screenshots')
        except Exception as error:
            self._summon_up_result['evidence_saved'] = False
            self._su_note('EVIDENCE', f'stage={stage} reason={reason} save_failed={type(error).__name__}')

    def _su_welfare(self):
        return self._su_page() == 'WELFARE'

    def _su_navigate_to_gift(self):
        stable, previous, unknown_since = 0, None, None

        def navigate():
            nonlocal stable, previous, unknown_since
            page = self._su_page()
            if page != previous:
                self._su_note('NAVIGATE', f'page={page}')
                stable, previous, unknown_since = 0, page, None
            if page == 'EXPERIENCE':
                self._su_session = True
                self._su_dismiss_experience()
                return False  # Never navigate using the frame on which we clicked.
            if page == 'UNVERIFIED_EXPERIENCE':
                self._su_stage = 'EXPERIENCE_VERIFY'
                self._su_note('EXPERIENCE_VERIFY', f'evidence={self._su_experience_evidence}')
                raise _SummonUpStop('NEEDS_RUNTIME_EVIDENCE', 'EXPERIENCE_CONTEXT_UNVERIFIED')
            if self._su_experience_pending:
                self._su_note('DISMISS_EXPERIENCE', f'popup_absent next_page={page}')
                self._su_experience_pending = False
            self._su_stage = 'NAVIGATE'
            if page in ('WELFARE', 'DETAIL', 'REWARD'):
                self._su_session = True
                stable += 1
                if stable >= 2:
                    self._su_entry_page = page
                    return True
                return False
            stable = 0
            if page == 'UNVERIFIED_DIALOG':
                raise _SummonUpStop('NEEDS_RUNTIME_EVIDENCE', 'POOL_OR_CONFIRM_DIALOG_UNVERIFIED')
            if page == 'ACTIVITY':
                self._su_session = True
                # Path A: never close an already open activity page.
                self._su_click(self.I_SUMMON_UP_TO_GIFT, 'OPEN_GIFT', limit=2)
            elif page == 'SUMMON':
                self._su_session = True
                # Path B: click only inside the verified chest/abdomen region.
                # 同辉 is a different tab; no OCR label is an entrance here.
                roi = self.SUMMON_UP_PORTRAIT_ROI
                action = RuleClick(roi_front=roi, roi_back=roi, name='summon_up_activity_entry')
                if self._su_click(action, 'ENTER_ACTIVITY', limit=2):
                    self._su_note('ENTER_ACTIVITY', f'portrait_roi={roi}; await activity page confirmation')
            elif page == 'WRONG_SUMMON_TAB':
                self._su_session = True
                self._su_stage = 'RECOVER_SUMMON_TAB'
                labels = sorted(self._su_boxes(self.SUMMON_UP_HOUSE_LABEL_ROI), key=lambda item: item[1][1])
                if ''.join(text for text, _ in labels) != '召唤屋':
                    raise _SummonUpStop('NEEDS_RUNTIME_EVIDENCE', 'SUMMON_HOUSE_TAB_UNVERIFIED')
                # Only this explicit wrong-page state permits switching tabs.
                roi = self.SUMMON_UP_HOUSE_CLICK_ROI
                action = RuleClick(roi_front=roi, roi_back=roi, name='summon_up_return_summon_house')
                if self._su_click(action, 'RECOVER_SUMMON_TAB', limit=1):
                    self._su_note('RECOVER_SUMMON_TAB', f'wrong tab lock confirmed; house_roi={roi}; await fresh frame')
            elif page == 'MAIN':
                if self.appear(self.I_TO_SUMMON):
                    self._su_click(self.I_TO_SUMMON, 'ENTER_SUMMON', limit=2)
            else:
                if unknown_since is None:
                    unknown_since = time.monotonic()
                # Only the existing back-arrow template is a recovery action.
                # Give transitions time to settle before using this bounded fallback.
                if time.monotonic() - unknown_since >= 8 and self.appear(self.I_SUMMON_GOTO_MAIN):
                    if self._su_click(self.I_SUMMON_GOTO_MAIN, 'UNKNOWN_RECOVERY', limit=1):
                        self._su_note('RECOVERY', 'UNKNOWN_PAGE_WITH_VERIFIED_BACK_ARROW')
                if time.monotonic() - unknown_since >= 12:
                    raise _SummonUpStop('NEEDS_RUNTIME_EVIDENCE', 'UNKNOWN_ENTRY_CONTEXT')
            return False

        self._su_poll('NAVIGATE', 60, navigate)


    def _su_gift_evidence(self):
        labels = self._su_boxes(self.SUMMON_UP_STATE_ROI)
        if any(text in ('未解锁', '尚未解锁', '暂不可领取', '不可领取') for text, _ in labels):
            raise _SummonUpStop('NOT_AVAILABLE', 'COPPER_EXPLICITLY_UNAVAILABLE')
        free = self.appear(self.I_SUMMON_UP_BUY_GIFT)
        claimed = self.appear(self.I_SUMMON_UP_BUY_FLAG)
        if claimed and not free:
            # Native screenshot 4 verifies the overall tilted stamp. OCR cannot
            # reliably read it; the card title + narrow stamp template identify it.
            return 'CLAIMED'
        if free and not claimed:
            self._su_free_label(self.I_SUMMON_UP_BUY_GIFT, self.SUMMON_UP_FREE_ROI)
            return 'FREE'
        raise _SummonUpStop('NEEDS_RUNTIME_EVIDENCE', 'COPPER_STATE_UNVERIFIED')

    def _su_share(self):
        self.device.click_record_clear()  # Stage initialization, never per frame.
        self._su_note('SHARE', '开始分享流程；扫码提示不等同于外部分享成功')
        qr_seen, stable = False, 0

        def share():
            nonlocal qr_seen, stable
            if self._su_welfare():
                if self.appear(self.I_SUMMON_UP_BACK_RED):
                    self._su_click(self.I_SUMMON_UP_BACK_RED, 'LEAVE_GIFT')
                return False
            if self.appear(self.I_SUMMON_UP_WECHAT_SHARE_2) and \
                    self._su_clicks.get(('SHARE_WECHAT', self.I_SUMMON_UP_WECHAT_SHARE.name)):
                if not qr_seen:
                    self._su_note('SHARE', '扫码提示已确认，等待返回活动页')
                    self._summon_up_result['share_state'] = 'QR_SEEN'
                qr_seen = True
            if qr_seen:
                if self.appear_rgb(self.I_SUMMON_UP_TO_GIFT) and \
                        not self.appear(self.I_SUMMON_UP_WECHAT_SHARE_2):
                    stable += 1
                    if stable >= 2:
                        self._summon_up_result['share_state'] = 'FLOW_CONFIRMED'
                        self._su_note('SHARE', '分享页面流程已确认；领取资格由礼包结果验证')
                        return True
                    return False
                else:
                    stable = 0
                for rule in (self.I_SUMMON_UP_BACK_SHARE, self.I_SUMMON_UP_BACK_RED):
                    if self.appear(rule):
                        self._su_click(rule, 'SHARE_RETURN')
                        break
            elif self.appear(self.I_SUMMON_UP_WECHAT_SHARE) and \
                    self._su_clicks.get(('SHARE_OPEN', self.I_SUMMON_UP_TO_SHARE_2.name)):
                self._su_click(self.I_SUMMON_UP_WECHAT_SHARE, 'SHARE_WECHAT')
            elif self.appear(self.I_SUMMON_UP_TO_GIFT) and \
                    self.appear(self.I_SUMMON_UP_TO_SHARE_2):
                if self._su_click(self.I_SUMMON_UP_TO_SHARE_2, 'SHARE_OPEN'):
                    self._summon_up_result['share_state'] = 'ATTEMPTED'
            return False

        self._su_poll('SHARE', 35, share)

    def _su_free_label(self, rule, allowed_roi):
        x, y, w, h = rule.roi_front
        ax, ay, aw, ah = allowed_roi
        if not (w > 0 and h > 0 and ax <= x and ay <= y and x + w <= ax + aw and y + h <= ay + ah):
            raise _SummonUpStop('NEEDS_RUNTIME_EVIDENCE', 'FREE_BUTTON_OUTSIDE_COPPER_REGION')
        labels = self._su_boxes(allowed_roi)
        if len(labels) != 1 or labels[0][0] != '免费':
            raise _SummonUpStop('NEEDS_RUNTIME_EVIDENCE', 'FREE_BUTTON_UNVERIFIED')
        bx, by, bw, bh = labels[0][1]
        if not self._su_contains(rule.roi_front, (bx + bw / 2, by + bh / 2)):
            raise _SummonUpStop('NEEDS_RUNTIME_EVIDENCE', 'FREE_TEXT_OUTSIDE_MATCHED_BUTTON')

    def _su_claim(self):
        opened, purchased, reward_seen, dismissed, stable = False, False, False, False, 0
        resumed_feedback = self._su_entry_page == 'REWARD'

        def claim():
            nonlocal opened, purchased, reward_seen, dismissed, stable
            page = self._su_page()
            if page != 'WELFARE':
                stable = 0
            if page == 'REWARD':
                if not purchased and not resumed_feedback:
                    raise _SummonUpStop('NEEDS_RUNTIME_EVIDENCE', 'UNEXPECTED_PURCHASE_FEEDBACK')
                if not reward_seen:
                    reward_seen = True
                    self._summon_up_result['free_gift_state'] = (
                        'REWARD_CONFIRMED' if purchased else 'EXISTING_FEEDBACK_OBSERVED')
                    self._su_note('REWARD', '购买成功反馈已确认；仍须验证铜铃礼包整体已领取')
                if not dismissed:
                    self._su_click(self.I_SUMMON_UP_PURCHASE_SUCCESS, 'DISMISS_REWARD', limit=1,
                                   action=self.C_SUMMON_UP_AWARD)
                    dismissed = True
                return False
            if page == 'DETAIL':
                if not purchased and not reward_seen and self.appear(self.I_SUMMON_UP_BUY_GIFT_2):
                    self._su_free_label(self.I_SUMMON_UP_BUY_GIFT_2, self.SUMMON_UP_DETAIL_FREE_ROI)
                    self._su_click(self.I_SUMMON_UP_BUY_GIFT_2, 'FREE_GIFT_CONFIRM', limit=1)
                    purchased = True
                    self._summon_up_result['free_gift_state'] = 'FREE_CONFIRM_ATTEMPTED'
                return False
            if page == 'UNVERIFIED_DIALOG':
                raise _SummonUpStop('NEEDS_RUNTIME_EVIDENCE', 'UNVERIFIED_PURCHASE_DIALOG')
            if page != 'WELFARE':
                stable = 0
                return False
            state = self._su_gift_evidence()
            if state == 'CLAIMED':
                if purchased and not reward_seen:
                    return False  # A changed badge alone cannot confirm our transaction.
                stable += 1
                if stable >= 2:
                    outcome = 'SUCCESS' if purchased else 'ALREADY_CLAIMED'
                    self._summon_up_result.update(outcome=outcome,
                        reason='COPPER_PURCHASE_AND_CLAIMED_CONFIRMED' if purchased else 'COPPER_ALREADY_CLAIMED',
                        free_gift_state='CLAIMED' if purchased else 'ALREADY_CLAIMED',
                        reward_state='PURCHASE_FEEDBACK_CONFIRMED' if reward_seen else 'NOT_REQUIRED')
                    self._su_note('FREE_GIFT', f'{outcome} 铜铃礼包整体已领取；连续登录奖励由邮件发放')
                    return True
                return False
            stable = 0
            if not opened and not purchased and not reward_seen:
                self._su_click(self.I_SUMMON_UP_BUY_GIFT, 'FREE_GIFT_OPEN', limit=1)
                opened = True
                self._summon_up_result['free_gift_state'] = 'DETAIL_OPEN_ATTEMPTED'
            return False

        self._su_poll('CLAIM', 50, claim)

    def _su_return_main(self):
        """Safe task-local exit. ui_goto's False is a failure, not completion.

        Shared navigation is called only on confirmed main. Unsafe recovery and
        additional actions are disabled on this instance and always restored.
        Unknown pages never fall through to a generic purchase confirmation.
        """
        deadline = min(self._su_deadline, time.monotonic() + self.SUMMON_UP_RETURN_TIMEOUT)
        while time.monotonic() < deadline:
            self.screenshot()
            if time.monotonic() >= deadline:
                break
            if self.ui_page_appear(page_main):
                saved = {name: (name in self.__dict__, getattr(self, name))
                         for name in ('ui_close', 'run_additional', 'ui_get_current_page')}

                def main_only(*args, **kwargs):
                    if time.monotonic() >= deadline or not self.ui_page_appear(page_main):
                        raise _SummonUpStop('FAILED', 'RETURN_MAIN_PAGE_CHANGED')
                    self.ui_current = page_main
                    return page_main

                self.ui_close = ()
                self.run_additional = lambda *args, **kwargs: None
                self.ui_get_current_page = main_only
                self.ui_current = page_main
                try:
                    ok = self.ui_goto(page_main, timeout=min(5, deadline - time.monotonic()))
                except _SummonUpStop:
                    ok = False
                finally:
                    for name, (owned, value) in saved.items():
                        if owned:
                            setattr(self, name, value)
                        else:
                            delattr(self, name)
                self.screenshot()
                return bool(ok and time.monotonic() < deadline and self.ui_page_appear(page_main))
            if self._su_session:
                page = self._su_page()
                if page in ('DETAIL', 'WELFARE', 'ACTIVITY', 'SUMMON', 'WRONG_SUMMON_TAB') and \
                        self.appear(self.I_SUMMON_GOTO_MAIN):
                    self._su_click(self.I_SUMMON_GOTO_MAIN, f'RETURN_MAIN_{page}', limit=2)
                # A persistent reward popup has already received its one safe
                # dismissal. Unknown/paid dialogs do not enter generic recovery.
            time.sleep(min(0.3, max(0, deadline - time.monotonic())))
        return False

    def run_summon_up(self):
        """Copper free gift only; independent of sharing and mailed login rewards."""
        started = time.monotonic()
        self._su_deadline = started + self.SUMMON_UP_WORK_TIMEOUT
        self._su_clicks, self._su_session, self._su_stage = {}, False, 'INIT'
        self._su_entry_page = None
        self._su_experience_pending, self._su_experience_evidence = False, None
        self._summon_up_result = dict(outcome='FAILED', reason='NOT_STARTED',
                                     share_state='DISABLED_BY_USER', free_gift_state='UNVERIFIED',
                                     reward_state='UNVERIFIED')
        self.device.click_record_clear()
        try:
            self._su_navigate_to_gift()
            self._su_claim()
        except _SummonUpStop as error:
            self._summon_up_result.update(outcome=error.outcome, reason=error.reason)
            self._su_failure_evidence(error.reason)
        except Exception as error:
            self._summon_up_result.update(outcome='FAILED', reason=f'EXCEPTION_{type(error).__name__}')
            self._su_failure_evidence(self._summon_up_result['reason'])
            self._su_note('FINAL', str(self._summon_up_result))
            raise
        self._su_deadline = started + self.SUMMON_UP_TOTAL_TIMEOUT
        self._su_stage = 'RETURN_MAIN'
        try:
            returned = self._su_return_main()
        except _SummonUpStop:
            returned = False
        except Exception as error:
            self._summon_up_result.update(return_main=False,
                                         claim_outcome=self._summon_up_result['outcome'],
                                         claim_reason=self._summon_up_result['reason'],
                                         outcome='FAILED', reason=f'RETURN_EXCEPTION_{type(error).__name__}')
            self._su_failure_evidence(self._summon_up_result['reason'])
            self._su_note('FINAL', str(self._summon_up_result))
            raise
        self._summon_up_result['return_main'] = returned
        if not returned:
            self._summon_up_result.update(claim_outcome=self._summon_up_result['outcome'],
                                         claim_reason=self._summon_up_result['reason'],
                                         claim_failure_stage=self._summon_up_result.get('failure_stage'),
                                         outcome='FAILED', reason='RETURN_MAIN_FAILED')
            self._su_failure_evidence('RETURN_MAIN_FAILED')
        self._su_note('RETURN_MAIN', '返回庭院成功' if returned else '返回庭院失败')
        self._su_note('FINAL', str(self._summon_up_result))
        return returned and self._summon_up_result['outcome'] in ('SUCCESS', 'ALREADY_CLAIMED')
