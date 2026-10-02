# This Python file uses the following encoding: utf-8
# 庭院皮肤资源可达性测试：实机检测当前皮肤下 5 个资源能否被识别。
#
# 与 costume_test.py 的差别：
# 1. 不手动替换资产。先跑一次运行时自动识别（try_detect_costume_main），识别到的那套
#    就是被测对象——与真实运行时走同一条路径，顺带验证新皮肤有没有进探测候选表。
# 2. 只检测、不点击。5 个资源本来就都在庭院这一屏上，点击既没必要、又会改变游戏状态；
#    检测跑完画面仍停在庭院，不留任何副作用。
# 3. 多帧统计而非单帧：庭院有飘落、呼吸之类的小动画，单帧命中说明不了模板稳不稳。
# 4. 结论以汇总表打印，不依赖中间日志。

from module.base.timer import Timer
from module.logger import logger
from module.server.i18n import Addition, I18n

from tasks.Component.Costume.assets import CostumeAssets
from tasks.Component.Costume.config import MainType
from tasks.Component.Costume.costume_base import (release_costume_probe_locks,
                                                  _MAIN_PROBE_DEFAULT, _MAIN_PROBE_NAME)
from tasks.Component.GeneralBattle.general_battle import GeneralBattle
from tasks.Component.SwitchSoul.switch_soul import SwitchSoul
from tasks.GameUi.game_ui import GameUi
from tasks.GameUi.page import page_main
from tasks.Pets.assets import PetsAssets

# 连拍时长（秒）。庭院是静态画面，5s 足够攒出十几帧，够看出模板稳不稳。
DETECT_SECONDS = 5


class ScriptTask(GeneralBattle, GameUi, SwitchSoul, PetsAssets):

    def run(self):
        self.ui_get_current_page()
        self.ui_goto(page_main)

        logger.hr('Costume Main Test Start')

        # ui_goto 的页面识别循环里可能已经探过一轮并上锁，不解锁这次调用会直接返回 None，
        # 把「资产其实已经套好了」误报成零命中
        release_costume_probe_locks()
        # 探测只读 self.device.image（见 probe_costume_main 的 docstring），不自己截图，
        # 所以先取一帧再调
        self.screenshot()
        detected = self.try_detect_costume_main()

        suffix = self._suffix_of(detected)
        if detected is None:
            logger.warning('未识别出庭院皮肤：check_main 不可达，或当前不在庭院页。'
                           '下面仍会继续检测五个资源，但测的是哪一套无从确认')
        else:
            logger.info(f'当前庭院皮肤：{self._name_of(detected)}')

        # 五个资源都在庭院这一屏上，逐个检测即可，不需要点击
        # check_main 这一行要用**皮肤探针的模板**，不能用 GameUiAssets.I_CHECK_MAIN：
        # 后者已经换成皮肤无关的活动图标，在任何庭院上都命中，测不出这套皮肤的图采没采好。
        targets = [
            (f'check_main{suffix}', self._probe_template_of(detected)),
            (f'main_goto_town{suffix}', self.I_MAIN_GOTO_TOWN),
            (f'main_goto_exploration{suffix}', self.I_MAIN_GOTO_EXPLORATION),
            (f'main_goto_summon{suffix}', self.I_MAIN_GOTO_SUMMON),
            (f'pet_house{suffix}', self.I_PET_HOUSE),
        ]

        frames, counts = self._detect(targets)
        self._report(targets, counts, frames)

    def _detect(self, targets) -> tuple:
        """连拍若干帧，统计每个资源命中的帧数，返回 (总帧数, 命中数列表)。

        appear 不传 interval 时每次都真调 match，不会被计时器短接，计数是准的。
        """
        frames = 0
        counts = [0] * len(targets)
        timer = Timer(DETECT_SECONDS)
        timer.start()
        while 1:
            self.screenshot()
            frames += 1
            for i, (_, asset) in enumerate(targets):
                if self.appear(asset):
                    counts[i] += 1
            if timer.reached():
                break
        return frames, counts

    @staticmethod
    def _probe_template_of(detected: MainType | None):
        """取该套皮肤在庭院探针里当模板用的那张图（CostumeAssets 上、永久出厂态的那张）。

        名字与 costume_base 的 _MAIN_PROBE_DEFAULT / _MAIN_PROBE_NAME 对应，是同两个名字的
        第三处副本；零命中时退回默认套模板，只为让汇总表有东西可打。
        """
        name = _MAIN_PROBE_DEFAULT
        if detected is not None and detected is not MainType.COSTUME_MAIN:
            _, _, number = detected.value.rpartition('_')
            if number.isdigit():
                name = _MAIN_PROBE_NAME.format(i=number)
        return getattr(CostumeAssets(), name)

    @staticmethod
    def _suffix_of(detected: MainType | None) -> str:
        """由识别结果推出资源名后缀（costume_main_15 -> _15）。

        默认套（costume_main）没有编号，返回空串——它的资源名本来就不带后缀
        （check_main / main_goto_town ...）；零命中返回 _? 占位。
        """
        if detected is None:
            return '_?'
        _, _, suffix = detected.value.rpartition('_')
        if suffix.isdigit():
            return f'_{suffix}'
        return '' if detected is MainType.COSTUME_MAIN else '_?'

    @staticmethod
    def _name_of(main_type: MainType) -> str:
        """把枚举翻成游戏内中文名。

        两侧 i18n 各存一半：新皮肤在补充源 assets/i18n/zh-CN.json，老皮肤在前端镜像
        module/config/i18n/zh-CN.json，所以两边都要查；都查不到则退回枚举值原文。
        """
        names = I18n.load_zh_cn()
        names.update(Addition.load_additions().get('zh-CN', {}))
        return names.get(main_type.value, main_type.value)

    @staticmethod
    def _report(targets, counts, frames):
        """打汇总表，一次看全 5 个资源哪些不可达。"""
        width = 32
        line = '-' * (width + 36)
        print(line)
        print('%-*s %-9s %s' % (width, 'Resource', 'Hit', 'Result'))
        print(line)
        for (name, _), n in zip(targets, counts):
            print('%-*s %-9s %s' % (width, name, f'{n}/{frames}', '可达' if n else '不可达'))
        print(line)

        missed = [name for (name, _), n in zip(targets, counts) if not n]
        if missed:
            logger.warning(f'不可达 {len(missed)}/{len(targets)}: {", ".join(missed)}')
        else:
            logger.info(f'全部 {len(targets)} 个资源可达（共 {frames} 帧）')


if __name__ == '__main__':
    from module.config.config import Config
    from module.device.device import Device

    c = Config('oas3')
    d = Device(c)
    t = ScriptTask(c, d)
    t.run()
