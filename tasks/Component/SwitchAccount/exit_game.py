from tasks.Component.SwitchAccount.assets import SwitchAccountAssets
from tasks.base_task import BaseTask
from module.logger import logger
from module.exception import RequestHumanTakeover
from tasks.GameUi.return_main import ReturnTrace
from tasks.GameUi.page import page_login
import time


class ExitGame(BaseTask, SwitchAccountAssets):

    def exitGame(self):
        logger.info("start game exit")
        # 三阶段共用总预算；3/6/3 秒仍是原来的重复输入间隔。
        # 每一帧重新确认阶段，不信任上一次返回缓存，不对未知页点头像。
        trace = ReturnTrace(self, 'switch_exit_game')
        deadline = time.monotonic() + 30
        last_progress = time.monotonic()
        actions = 0
        avatar_sent = False
        while time.monotonic() < deadline:
            self.screenshot()
            self.ui_current = None
            if time.monotonic() >= deadline:
                break
            if self.appear(self.I_NETWORK_ERROR) or self.appear(self.I_NETWORK_ABNORMAL):
                trace.finish('network_blocked')
                raise RequestHumanTakeover('Network dialog blocks exit; see RETURN_DIAG')
            overlay = self._current_overlay_page()
            if overlay is not None:
                self.ui_current = overlay
                trace.finish('overlay_blocked')
                raise RequestHumanTakeover('Overlay interrupted exit; see RETURN_DIAG')
            if self.appear(self.I_SA_SWITCH_ACCOUNT_BTN):
                target, interval = self.I_SA_SWITCH_ACCOUNT_BTN, 3
            elif (self.appear(self.I_SA_USER_CENTER)
                  and (avatar_sent or self.appear(self.I_SA_USER_CENTER_PROFILE))):
                target, interval = self.I_SA_USER_CENTER, 6
            elif ((self.appear(self.I_SA_NETEASE_GAME_LOGO)
                   and self.appear(self.I_SA_ACCOUNT_LOGIN_BTN))
                  or self._match_page(page_login)):
                self.ui_current = page_login
                trace.finish('arrived')
                return True
            elif self.ui_main_confirmed():
                target, interval = self.C_SA_EG_PROFILE_PHOTO, 3
            else:
                if time.monotonic() - last_progress >= 6 and trace.folder is None:
                    trace.capture('unrecognised')
                continue
            if actions >= 6:
                trace.finish('action_limit')
                raise RequestHumanTakeover('Exit-game action limit; see RETURN_DIAG')
            sent = self.ui_traced_click(target, interval, trace)
            if sent:
                actions += 1
                last_progress = time.monotonic()
                if target is self.C_SA_EG_PROFILE_PHOTO:
                    avatar_sent = True
            self.ui_current = None
        trace.finish('timeout')
        raise RequestHumanTakeover('Exit-game total budget exhausted; see RETURN_DIAG')
