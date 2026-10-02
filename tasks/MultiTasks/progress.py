"""多账号通用任务的进度适配：账号导出完成还需要文件实际存在。"""
from pathlib import Path

from tasks.Component.MultiAccountRunner.progress import (
    STATUS_DONE,
    STATUS_PENDING,
    ProgressStore as _BaseProgressStore,
)


class ProgressStore(_BaseProgressStore):
    """其他子任务沿用通用规则，导出阶段可在文件丢失后重新入队。"""

    def is_task_finished(self, key: str, task: str) -> bool:
        """导出只认可成功记录与实际文件，failed/skipped 不能当作导出完成。"""
        if task != 'account_export':
            return super().is_task_finished(key, task)
        record = self.get_task(key, task)
        path = record.get('path')
        return (record.get('status') == STATUS_DONE
                and isinstance(path, str) and bool(path) and Path(path).is_file())

    def is_account_done(self, key: str) -> bool:
        """在 Runner 跳过整个账号前检查导出文件，其他阶段不额外检查。"""
        if not super().is_account_done(key):
            return False
        return (self._data.get('phase_flags', {}).get('sub_task') != 'account_export'
                or self.is_task_finished(key, 'account_export'))

    def mark_task(self, key: str, task: str, status: str, **extra) -> bool:
        """补导开始即撤销旧账号完成标记，失败后下一轮仍可重试。"""
        if task == 'account_export' and status == STATUS_PENDING:
            self._account(key)['status'] = STATUS_PENDING
        return super().mark_task(key, task, status, **extra)
