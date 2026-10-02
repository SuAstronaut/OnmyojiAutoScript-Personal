# This Python file uses the following encoding: utf-8
# @author runhey
# github https://github.com/runhey
import time
from datetime import timedelta, datetime

from tasks.Component.SwitchSoul.switch_soul import SwitchSoul
from tasks.Exploration.assets import ExplorationAssets
from tasks.Exploration.config import ChooseRarity, AutoRotate, AttackNumber
from tasks.Component.GeneralBattle.general_battle import GeneralBattle
from tasks.GameUi.game_ui import GameUi
from tasks.GameUi.page import page_exploration, page_shikigami_records, page_main
from tasks.RealmRaid.script_task import ScriptTask as RealmRaidScriptTask
from tasks.Exploration.solo import ScriptTask as SoloScriptTask

from module.logger import logger
from module.exception import RequestHumanTakeover, TaskEnd
from module.atom.image_grid import ImageGrid


class ScriptTask(SoloScriptTask):
    pass


if __name__ == "__main__":
    import sys
    from module.config.config import Config
    from module.device.device import Device

    config_name = sys.argv[1] if len(sys.argv) > 1 else 'oas2'
    config = Config(config_name)
    device = Device(config)
    t = ScriptTask(config, device)
    t.run()

