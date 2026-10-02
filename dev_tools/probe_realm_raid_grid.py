# This Python file uses the following encoding: utf-8
"""结界突破九宫格四态识别的离线探针。

直接调用生产代码 ScriptTask.detect_cells() / find_one()，不复制算法——
这样探针验证过的结论就是脚本真实跑出来的行为，不会出现「探针对了但生产代码不同」。

不需要模拟器：用一个只持有静态截图的假 device 顶替真设备，screenshot() 变成 no-op。

用法（仓库根目录执行）：
    ./toolkit/python.exe -m dev_tools.probe_realm_raid_grid <截图路径> [<截图路径> ...]
"""
import sys

import cv2
from numpy import fromfile, uint8

from tasks.RealmRaid.script_task import ScriptTask


class _FakeDevice:
    """只提供 image 属性的假设备，让 BaseTask.screenshot() 之外的匹配链正常工作。"""

    def __init__(self, image):
        self.image = image


class _FakeConfig:
    """喂给 find_one 的最小配置，只需要 realm_raid.raid_config.order_attack。"""

    class _RaidConfig:
        order_attack = '5 > 4 > 3 > 2 > 1 > 0'

    class _RealmRaid:
        raid_config = None

    def __init__(self, order_attack: str = '5 > 4 > 3 > 2 > 1 > 0'):
        raid = self._RaidConfig()
        raid.order_attack = order_attack
        realm = self._RealmRaid()
        realm.raid_config = raid
        self.realm_raid = realm


class _ProbeTask(ScriptTask):
    """绕开 BaseTask.__init__ 的设备/配置依赖，只保留九宫格识别所需的最小状态。"""

    def __init__(self, image, order_attack: str = '5 > 4 > 3 > 2 > 1 > 0'):
        self.device = _FakeDevice(image)
        self.config = _FakeConfig(order_attack)
        self._cells = None

    def screenshot(self):
        # 静态截图，不需要重新抓帧
        return self.device.image


def load_screenshot(path: str):
    """按 device.image 的通道约定读图（RuleImage 内部模板也是 BGR→RGB）。"""
    img = cv2.imdecode(fromfile(path, dtype=uint8), -1)
    if img is None:
        raise FileNotFoundError(path)
    if img.shape[2] == 4:
        return cv2.cvtColor(img, cv2.COLOR_BGRA2RGB)
    return cv2.cvtColor(img, cv2.COLOR_BGR2RGB)


def probe(path: str, order_attack: str = '5 > 4 > 3 > 2 > 1 > 0') -> None:
    image = load_screenshot(path)
    task = _ProbeTask(image, order_attack)

    print(f'=== {path}  shape={image.shape}  order_attack={order_attack}')
    cells = task.detect_cells(screenshot=False)
    for cell in cells:
        total = cell['medal_count'] + cell['no_medal_count']
        # 一致性：有印章应 4 槽、无印章应 5 槽
        expect = 4 if cell['state'] == 'FINISHED' else 5
        print(f"  [{cell['index']}] slot={cell['slot']} region={cell['region']} "
              f"{cell['state']:10s} M={cell['medal_count']} N={cell['no_medal_count']} "
              f"tot={total} consistent={total == expect} click={cell['click_roi']}")

    index = task.find_one(screenshot=False)
    print(f'  -> find_one = {index}')
    print()


if __name__ == '__main__':
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)
    for arg in sys.argv[1:]:
        probe(arg)
