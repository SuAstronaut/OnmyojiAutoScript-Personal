# This Python file uses the following encoding: utf-8
# @author runhey
# github https://github.com/runhey

from module.exception import TaskEnd

from tasks.Component.SwitchSoul.switch_soul import SwitchSoul
from tasks.GameUi.game_ui import GameUi
from tasks.GameUi.page import page_shikigami_records,page_six_gates
from tasks.SixRealms.moon_sea.moon_sea import MoonSea


class ScriptTask(SwitchSoul, MoonSea, GameUi):
    """ 六道之门 """
    @property
    def _config(self):
        return self.config.model.six_realms

    def run(self):
        if self._config.switch_soul_config.enable:
            self.ui_get_current_page()
            self.ui_goto(page_shikigami_records)
            # 六道之门使用通用单个御魂预设配置。
            self.run_switch_soul(self._config.switch_soul_config.switch_group_team)
        self.ui_get_current_page()
        self.ui_goto_page(page_six_gates)
        self.run_moon_sea()

        # 退出六道
        while 1:
            self.screenshot()
            if self.appear(self.I_CHECK_EXPLORATION) or self.appear(self.I_CHECK_MAIN):
                break
            if self.appear_then_click(self.I_BACK_EXIT, interval=2):
                continue
        # 设置下一次运行时间是周一
        #self.next_run_week(1)
        self.set_next_run('SixRealms', success=True, finish=False)
        raise TaskEnd('SixRealms')

    def run_moon_sea(self):
        self._run_moon_sea()
        self.ui_click(self.I_BACK_EXIT, self.I_CHECK_SIX_GATES)


if __name__ == '__main__':
    from module.config.config import Config
    from module.device.device import Device

    c = Config('oas1')
    d = Device(c)

    self = ScriptTask(c, d)
    #self._run_moon_sea()
    self.run()

