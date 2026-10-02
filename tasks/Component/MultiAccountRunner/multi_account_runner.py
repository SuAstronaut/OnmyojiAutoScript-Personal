# This Python file uses the following encoding: utf-8
"""
多账号轮转执行器 (MultiAccountRunner)

统一封装多账号任务的通用流程：账号排序、过滤、切换、重试、进度追踪。
所有需要多账号轮转的任务（MultiDailyAltAcc、MultiTasks、FindJade 等）均可复用此类。

使用方式：
    1. 在 script_task.py 中创建 MultiAccountRunner 实例
    2. 调用 run() 方法，传入自定义的 process_func 回调
    3. process_func 接收 account_info 参数，返回 True/False 表示成功/失败

示例：
    runner = MultiAccountRunner(
        task_name="MultiDailyAltAcc",
        config=self.config,
        device=self.device,
        account_list=self.daily_conf.sup_account_list,
        need_login=False,
        login_time=self.start_time,
        update_login_history_func=self.daily_conf.update_account_login_history,
        save_config_func=self.save_config,
    )
    runner.run(process_func=self._process_single_account)
"""
from collections import defaultdict
from datetime import datetime
import json
import threading
from pathlib import Path
from typing import Callable, List, Optional

from module.exception import (
    EmulatorNotRunningError,
    GameBugError,
    GameNotRunningError,
    GamePageUnknownError,
    GameStuckError,
    GameTooManyClickError,
    RequestHumanTakeover,
    ScriptError,
)
from module.logger import logger
from tasks.Component.MultiAccountRunner.progress import ProgressStore, acc_key
from tasks.Component.SwitchAccount.switch_account import SwitchAccount
from tasks.Component.SwitchAccount.switch_account_config import AccountInfo


class MultiAccountRunner:
    """
    多账号轮转执行器

    核心功能：
    - 账号过滤：根据 need_login 和 login_time 剔除不需要处理的账号
    - 账号排序：先按邮箱分组使同邮箱角色连续，再按最晚完成时间排序
    - 账号切换：统一调用 SwitchAccount 切换账号
    - 重试机制：每个账号失败后最多重试 max_retries 次
    - 进度追踪：基于文件的多进程进度追踪（标记开始/完成）
    - 错误处理：异常时恢复 need_login_time 为初始值

    参数说明：
        task_name:           任务名称，用于日志和进度文件命名（如 "MultiDailyAltAcc"、"MultiTasks"）
        config:              Config 实例
        device:              Device 实例
        account_list:        账号列表 (list[AccountInfo])
        need_login:          是否需要强制登录（无视时间）
        login_time:          需要比较的登录时间点（任务开始时的 need_login_time 值）
        update_login_history_func: 更新账号登录历史的回调函数，接收 AccountInfo 参数
        save_config_func:    保存配置的回调函数
        max_retries:         每个账号的最大重试次数，默认 3
        on_account_error:    账号处理异常时的回调，接收 (account_info, error) 参数，可选
        progress:            账号级进度存储（可选）。传入后账号是否处理改由进度文件判定，
                             need_login / login_time 不再生效；成功处理一个账号会即时落盘。
                             update_login_history_func / save_config_func 为调用方自用，
                             Runner 不触发它们，仅在 process_func 内由调用方自行调用。
    """

    def __init__(
        self,
        task_name: str,
        config,
        device,
        account_list: List[AccountInfo],
        need_login: bool,
        login_time: datetime,
        update_login_history_func: Callable,
        save_config_func: Callable,
        max_retries: int = 3,
        on_account_error: Optional[Callable] = None,
        progress: Optional[ProgressStore] = None,
    ):
        self.task_name = task_name
        self.config = config
        self.device = device
        self.account_list = account_list
        self.need_login = need_login
        self.login_time = login_time
        self.update_login_history_func = update_login_history_func
        self.save_config_func = save_config_func
        self.max_retries = max_retries
        self.on_account_error = on_account_error
        self.progress = progress
        self._lock = threading.Lock()

    # 设备级异常：必须穿透到 script.py 的 Restart / 人工接管恢复逻辑，
    # 任何一层被宽泛 except Exception 吞掉，游戏都会一直卡着、后续账号连环失败。
    # 与 MultiDailyAltAcc.ScriptTask._DEVICE_LEVEL_ERRORS 保持同一清单，增删须两边同步。
    _DEVICE_LEVEL_ERRORS = (
        GameNotRunningError,
        RequestHumanTakeover,
        GameStuckError,
        GameTooManyClickError,
        GameBugError,
        EmulatorNotRunningError,
        GamePageUnknownError,
        ScriptError,
    )
    # 账号循环用的清单：GameNotRunningError 有单独分支处理切号崩溃语义，排除之。
    # 从 _DEVICE_LEVEL_ERRORS 派生，避免两份清单漂移。
    _DEVICE_LEVEL_ERRORS_IN_LOOP = tuple(
        e for e in _DEVICE_LEVEL_ERRORS if e is not GameNotRunningError
    )

    # ======================== 公开接口 ========================

    def run(self, process_func: Callable[[AccountInfo], bool]) -> bool:
        """
        执行多账号轮转任务的主入口

        @param process_func: 处理单个账号的回调函数，接收 AccountInfo，返回 True(成功)/False(失败)
        @return: True 表示所有账号处理完成，False 表示有账号处理失败
        """
        config_name = self.config.config_name
        pid = __import__('os').getpid()

        logger.info(f"[{self.task_name}] Starting with PID {pid} for config {config_name}")
        self._mark_task_start(config_name, pid)

        has_error = False
        try:
            sorted_accounts = self.get_sorted_accounts()

            for account_info in sorted_accounts:
                if account_info is None:
                    logger.warning(f"[{self.task_name}] Skipping None account")
                    continue

                if not self._process_account_with_retry(account_info, process_func):
                    has_error = True

        finally:
            self._mark_task_completed(config_name)

        return not has_error

    def get_sorted_accounts(self) -> List[AccountInfo]:
        """
        获取排序后的账号列表（已剔除不需要处理的账号）

        排序逻辑：
        1. 先剔除不需要处理的账号（need_login 为 False 时，已完成的账号）
        2. 按邮箱（account）分组，使同一邮箱下的角色连续排列
        3. 按每个邮箱分组的最晚完成时间排序（最晚完成的邮箱在前）
        4. 同一邮箱内，按角色的完成时间排序（最晚完成的在前）

        @return: 排序后的账号列表
        """
        if not self.account_list:
            return []

        # 第一步：剔除不需要处理的账号
        filtered_accounts = []
        for account_info in self.account_list:
            if not self.should_process_account(account_info):
                continue
            filtered_accounts.append(account_info)

        if not filtered_accounts:
            logger.info(f"[{self.task_name}] No accounts need to be processed after filtering")
            return []

        # 第二步：按邮箱分组，使同一邮箱下的角色连续排列
        account_groups = defaultdict(list)
        for account_info in filtered_accounts:
            account_groups[account_info.account].append(account_info)

        # 计算每个邮箱分组的最晚完成时间
        account_times = {}
        for account, account_list in account_groups.items():
            latest_completion_time = max(acc.last_complete_time for acc in account_list)
            account_times[account] = latest_completion_time

        # 按邮箱的最晚完成时间排序（从大到小）
        sorted_accounts_by_time = sorted(
            account_groups.keys(),
            key=lambda acc: account_times[acc],
            reverse=True
        )

        # 每个邮箱内也按完成时间排序
        result = []
        for account in sorted_accounts_by_time:
            sorted_account_chars = sorted(
                account_groups[account],
                key=lambda x: x.last_complete_time,
                reverse=True
            )
            result.extend(sorted_account_chars)

        # 打印排序结果
        logger.info(f"[{self.task_name}] get_sorted_accounts result: account character last_complete_time")
        for account_info in result:
            logger.info(f"  {account_info.account} {account_info.character} {account_info.last_complete_time}")

        return result

    def should_process_account(self, account_info: AccountInfo) -> bool:
        """
        判断是否应该处理该账号。

        progress 非空时：完全依据持久化进度判定，不再比较登录时间。
        progress 为 None 时：保留基于 need_login / login_time 的旧行为。

        @param account_info: 账号信息
        @return: True 表示需要处理
        """
        # 将必填校验放在共用过滤入口，MultiTasks 等自定义排序也必须跳过空白配置。
        if account_info is None or not account_info.is_valid():
            logger.warning(f"[{self.task_name}] 跳过账号、角色名或区服名为空的配置")
            return False
        if self.progress is not None:
            try:
                key = acc_key(account_info.account, account_info.character, account_info.svr)
                done = self.progress.is_account_done(key)
            except Exception:
                logger.exception('读取账号进度失败，按未完成处理')
                return True
            if done:
                logger.info(f"[{self.task_name}] {account_info.character} 本阶段已完成，跳过")
            return not done
        # 兼容旧行为：基于 need_login / login_time 推断
        if self.need_login:
            return True
        # need_login 为 False 时，已完成的不需要再处理
        if account_info.last_complete_time >= self.login_time:
            logger.warning(
                f"[{self.task_name}] {account_info.character} Skipped, "
                f"last_complete_time:{account_info.last_complete_time} >= login_time:{self.login_time}"
            )
            return False
        return True

    def switch_to_account(self, account_info: AccountInfo) -> bool:
        """
        切换到指定账号

        @param account_info: 目标账号信息
        @return: True 表示切换成功
        """
        self.device.stuck_record_clear()
        success = SwitchAccount(self.config, self.device, account_info).switchAccount()
        if not success:
            logger.warning(f"[{self.task_name}] Switch to {account_info.character}-{account_info.svr} Failed")
            self.config.notifier.push(
                content=f"Switch to {account_info.character}-{account_info.svr} Failed, account info: {account_info.account}",
                title="未找到账号"
            )
        return success

    # ======================== 内部方法 ========================

    def _process_account_with_retry(self, account_info: AccountInfo, process_func: Callable) -> bool:
        """
        带重试机制地处理单个账号

        @param account_info: 账号信息
        @param process_func: 处理回调
        @return: True 表示成功
        """
        retry_count = 0

        while retry_count < self.max_retries:
            try:
                if process_func(account_info):
                    # 账号成功处理：即时落盘，中断后接续时整个跳过
                    if self.progress is not None:
                        self.progress.mark_account_done(acc_key(
                            account_info.account, account_info.character, account_info.svr))
                    return True
                else:
                    retry_count += 1
                    if retry_count < self.max_retries:
                        logger.info(f"[{self.task_name}] Account {account_info.character} failed, retrying ({retry_count}/{self.max_retries})...")
                    else:
                        logger.error(f"[{self.task_name}] Failed to process account {account_info.character} after {self.max_retries} attempts")
            except GameNotRunningError:
                raise GameNotRunningError("Game Not Running")
            except self._DEVICE_LEVEL_ERRORS_IN_LOOP:
                # 设备级异常（卡死/弹错/模拟器退出等）必须继续穿透到 script.py 的
                # Restart / 人工接管逻辑，不能在这里被宽泛 except Exception 吞成 return False
                raise
            except Exception as e:
                logger.error(f"[{self.task_name}] Error processing account {account_info.character}: {e}")
                if self.on_account_error:
                    self.on_account_error(account_info, e)
                return False

        return False

    def _mark_task_start(self, config_name: str, pid: int):
        """标记进程开始执行任务"""
        progress_file = self._get_progress_file()
        progress_file.parent.mkdir(parents=True, exist_ok=True)

        with self._lock:
            progress_data = self._read_progress(progress_file)
            progress_data[f'config_{config_name}'] = {
                'status': 'running',
                'start_time': datetime.now().isoformat(),
                'completed': False,
                'pid': pid
            }
            self._write_progress(progress_file, progress_data)

    def _mark_task_completed(self, config_name: str):
        """标记进程任务已完成"""
        progress_file = self._get_progress_file()

        with self._lock:
            progress_data = self._read_progress(progress_file)
            if f'config_{config_name}' in progress_data:
                progress_data[f'config_{config_name}'].update({
                    'status': 'completed',
                    'completed': True,
                    'completed_time': datetime.now().isoformat()
                })
            self._write_progress(progress_file, progress_data)

    def _get_progress_file(self) -> Path:
        """获取进度文件路径"""
        return Path(f'./config/tasks_config/{self.task_name.lower()}_progress.json')

    @staticmethod
    def _read_progress(progress_file: Path) -> dict:
        """读取进度文件"""
        if progress_file.exists():
            try:
                with open(progress_file, 'r', encoding='utf-8') as f:
                    return json.load(f)
            except (json.JSONDecodeError, FileNotFoundError):
                return {}
        return {}

    @staticmethod
    def _write_progress(progress_file: Path, data: dict):
        """写入进度文件"""
        with open(progress_file, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
