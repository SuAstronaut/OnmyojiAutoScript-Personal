"""Per-spawn execution marker; guarded by the existing config lifecycle FileLock.

No schedule or user setting lives here. A fresh shared buffer for every spawn
prevents an old child/finally from clearing the next child's execution marker.
"""
import multiprocessing


class SchedulerRuntime:
    def __init__(self, generation: str):
        self.generation = generation
        self._task = multiprocessing.Array('u', 256, lock=False)

    def read_locked(self) -> str:
        return self._task.value

    def claim_locked(self, task_key: str) -> bool:
        if self._task.value:
            return False
        if len(task_key) >= len(self._task):
            raise ValueError('Task identity is too long')
        self._task.value = task_key
        return True

    def clear_locked(self, task_key: str) -> None:
        if self._task.value == task_key:
            self._task.value = ''
