# This Python file uses the following encoding: utf-8
import time
import numpy as np

from module.exception import GameStuckError
from module.logger import logger
from tasks.GameUi.assets import GameUiAssets
from tasks.GameUi.page import page_main
from tasks.DailyAltAcc.utils import DailyAltAccBase
from tasks.Plotline.assets import PlotlineAssets
from tasks.Restart.assets import RestartAssets


class Courtyard(DailyAltAccBase):
    COMPLETE_PAGE_CONFIRM_FRAMES = 3
    COMPLETE_PAGE_MIN_SECONDS = 1.0
    COLLECTION_TIMEOUT = 120

    def _courtyard_popup_appear(self):
        # helper 的返回值可能受点击 interval 限制；存在判定不得使用 interval。
        # 这里只做门禁，关闭动作仍由原 get_award_daliy 负责。
        return any(self.appear(rule) for rule in (
            self.I_SUCCESS, self.I_M_AWARD, self.I_M_PICTURE_REFUSE,
            self.I_M_PICTURE, self.I_CORD_EXIT, self.I_CORD_BACK_RED,
            self.I_M_FRAME_BACK_RED, self.I_T_BACK_RED_SIGN,
            self.I_T_SIGN_FLAG, self.I_T_SIGN_FLAG2,
            PlotlineAssets.I_CLICK_CURSOR, PlotlineAssets.I_PAGE_CLICK_ANY,
        ))

    def _courtyard_button_state(self):
        if not self.appear(self.I_FINISH):
            return 'unknown'
        if self.appear_rgb(self.I_FINISH):
            return 'active'
        # 完成后按钮形状保留，实机已完成图中的 RGB 约为原亮态的一半。
        # 复用原模板确认形状和局部变暗；轻微色差/缺失不能当作不可执行。
        x, y, w, h = self.I_FINISH.roi_front
        button = self.device.image[y:y + h, x:x + w]
        reference = self.I_FINISH.image
        if button.shape != reference.shape:
            return 'unknown'
        ratio = button.mean(axis=(0, 1)) / np.maximum(reference.mean(axis=(0, 1)), 1)
        if np.all((ratio >= 0.35) & (ratio <= 0.65)):
            return 'inactive'
        return 'unknown'

    def run_courtyard(self):
        self.screenshot()
        if self.ui_get_current_page() != page_main:
            if not self.ui_goto(page_main):
                raise GameStuckError('Courtyard: cannot reach page_main before collection')
        start_time = time.monotonic()
        collection_started = False
        return_requested = False
        stable_finish_frames = 0
        stable_since = None
        last_check = None
        logger.info('[COURTYARD_STATE] collection_started=false')
        while time.monotonic() - start_time < self.COLLECTION_TIMEOUT:
            self.screenshot()

            # 所有奖励优先于主页判定、完成确认和返回点击。
            if self.get_award_daliy():
                stable_finish_frames = 0
                stable_since = None
                last_check = None
                continue
            if self.appear(self.I_SUCCESS):
                stable_finish_frames = 0
                stable_since = None
                last_check = None
                self.appear_then_click(self.I_SUCCESS, action=self.C_T_EXIT_SUCCESS, interval=1)
                continue
            if self._courtyard_popup_appear():
                stable_finish_frames = 0
                stable_since = None
                last_check = None
                continue

            if return_requested and (self.appear(GameUiAssets.I_CHECK_MAIN)
                                     or self.appear(self.I_M_MAIN_TO_MAIL)):
                logger.info('[COURTYARD_DONE] confirmed=true')
                return

            # 复用现有标题模板，并用颜色门禁排除弹窗造成的整页遮罩。
            page = self.appear_rgb(RestartAssets.I_PAGE) and self.appear(self.I_TASK_TO_MAIN)
            button_state = self._courtyard_button_state() if page else 'unknown'

            if page and button_state == 'active':
                stable_finish_frames = 0
                stable_since = None
                return_requested = False
                if self.appear_then_click(self.I_FINISH, interval=1):
                    if not collection_started:
                        logger.info('[COURTYARD_STATE] collection_started=true')
                    collection_started = True
            elif collection_started and page and button_state == 'inactive':
                if stable_since is None:
                    stable_since = time.monotonic()
                stable_finish_frames = min(stable_finish_frames + 1, self.COMPLETE_PAGE_CONFIRM_FRAMES)
            else:
                stable_finish_frames = 0
                stable_since = None

            check = (page, button_state, stable_finish_frames)
            if check != last_check:
                logger.info(f'[COURTYARD_FINISH_CHECK] page={page} '
                            f'button_state={button_state} stable_frames={stable_finish_frames}')
                last_check = check

            if (stable_finish_frames >= self.COMPLETE_PAGE_CONFIRM_FRAMES
                    and time.monotonic() - stable_since >= self.COMPLETE_PAGE_MIN_SECONDS):
                if self.appear_then_click(self.I_TASK_TO_MAIN, interval=1):
                    return_requested = True
                continue

            if not collection_started:
                if time.monotonic() - start_time >= 5:
                    return False
                if self.appear_then_click(self.I_MIAN_TO_TASK, interval=1):
                    continue
                self.appear_then_click(self.I_T_SPECIAL_FLAG, action=self.C_T_TONORMAL, interval=1)

        # 上抛现有设备级恢复，避免返回 False 后进入外层无超时补救循环。
        raise GameStuckError(f'Courtyard collection/return timed out after {self.COLLECTION_TIMEOUT}s')


if __name__ == "__main__":
    from module.config.config import Config
    from module.device.device import Device
    c = Config('oas2')
    d = Device(c)
    self = Courtyard(c, d)
    self.screenshot()
    self.run_courtyard()
