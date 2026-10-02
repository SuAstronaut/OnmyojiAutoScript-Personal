# This Python file uses the following encoding: utf-8
"""结界进入失败的连续计数（运行期数据，落 config/tasks_config/）。

进入结界/子界面失败会 raise TaskEnd 结束本次任务、实例随之销毁，所以计数必须
落盘：下次运行的是全新实例，内存里的计数必丢。

按 config 实例分文件，多实例（oas1 / oas2 / oas3 ...）各自独立计数、互不干扰。
"""
import json
from pathlib import Path

from module.logger import logger


class EntryFailStreak:
    """按任务名记录「进入结界」的连续失败次数。

    文件内容形如 {"KekkaiUtilize": 2, "KekkaiActivation": 0}。
    """

    def __init__(self, config_name: str, base_dir: str = 'config/tasks_config'):
        self.file = Path(base_dir) / f'kekkai_entry_fail_{config_name}.json'

    def _load(self) -> dict:
        """读计数文件；缺失或损坏一律按空计数处理，计数丢了不该阻塞任务。"""
        try:
            with open(self.file, 'r', encoding='utf-8') as f:
                data = json.load(f)
            return data if isinstance(data, dict) else {}
        except FileNotFoundError:
            return {}
        except Exception as e:
            logger.warning(f'读取连续失败计数失败，按 0 处理: {e}')
            return {}

    def _save(self, data: dict) -> None:
        """落盘；写失败只告警不抛出，计数不该成为任务能不能跑的前提。"""
        try:
            self.file.parent.mkdir(parents=True, exist_ok=True)
            with open(self.file, 'w', encoding='utf-8') as f:
                json.dump(data, f, ensure_ascii=False)
        except Exception as e:
            logger.warning(f'保存连续失败计数失败: {e}')

    def increase(self, task_name: str) -> int:
        """记一次失败，返回累计的连续失败次数。"""
        data = self._load()
        count = int(data.get(task_name, 0)) + 1
        data[task_name] = count
        self._save(data)
        return count

    def reset(self, task_name: str) -> None:
        """清零（成功一次即归零）；本来就是 0 时不写盘。"""
        data = self._load()
        if data.get(task_name):
            data[task_name] = 0
            self._save(data)
