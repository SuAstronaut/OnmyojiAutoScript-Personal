# This Python file uses the following encoding: utf-8
"""两个日常任务共用的协作记录入口，不从写入时刻推测协作刷新窗口。"""
import json
from datetime import datetime

from module.logger import logger
from tasks.Utils.coop_store import CoopStoreWriteError


def cooperation_round_id(task_name, progress):
    """使用已落盘阶段身份，避免进度写入失败后产生无法接续的临时轮次。"""
    try:
        try:
            saved = json.loads(progress.path.read_text(encoding='utf-8'))
            if not isinstance(saved, dict):
                raise ValueError('进度主文件不是对象')
        except (OSError, ValueError):
            # ProgressStore 可从备份恢复到内存；先修复主文件再复读确认，不能永久卡住接续。
            progress._save()
            saved = json.loads(progress.path.read_text(encoding='utf-8'))
        token = f'{saved["phase_id"]}|{saved["created_at"]}'
        if token != progress.phase_token:
            raise ValueError('磁盘阶段与内存阶段不一致')
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise CoopStoreWriteError('协作轮次进度尚未可靠落盘，保留任务等待重试') from exc
    return f'{task_name}:{token}'


def record_cooperation(event, store, identity, round_id=None):
    """使用识别帧时间和完整账号身份落盘；无账号上下文的单任务不伪造记录。"""
    if store is None or not identity:
        return None
    if not all(identity[:3]) or not isinstance(identity[3], bool):
        # 旧记录缺平台时不能默认成 iOS，否则会关联到另一个同名角色。
        logger.warning('协作账号身份不完整，未写入持久表')
        return None
    raw_time = event.get('found_at')
    try:
        found_at = datetime.fromisoformat(str(raw_time))
    except (TypeError, ValueError):
        # 旧消息可能没有时间，保留原日志供核对，不能补 now() 冒充当前窗口的供给。
        logger.warning('协作缺少有效识别时间，未写入持久表: %s', raw_time)
        return None
    return store.record(*identity, event, found_at=found_at, round_id=round_id)
