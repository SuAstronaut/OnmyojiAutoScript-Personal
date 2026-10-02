# This Python file uses the following encoding: utf-8
"""多账号通用任务：按选定的账号来源轮转切号，为每个账号执行选定的单账号任务。

子任务参数一律读单账号任务自身的配置，本任务不持有第二份副本。
"""
from module.exception import TaskEnd
from module.logger import logger
from tasks.Component.MultiAccountRunner import MultiAccountRunner
from tasks.Component.MultiAccountRunner.progress import acc_key
from tasks.GameUi.game_ui import GameUi
from tasks.MultiTasks.config import SubTaskType  # 导出使用严格的文件完成判定。
from tasks.MultiTasks.progress import ProgressStore
from tasks.MultiTasks.runners import ADAPTERS, SUB_TASKS
from tasks.MultiTasks.sources import ACCOUNT_SOURCES


class _MultiTasksRunner(MultiAccountRunner):
    """按同邮箱连续、最近登录优先的通用规则确定切号顺序。"""


class ScriptTask(GameUi):
    # 账号级续做进度，run() 中按配置实例创建；中断后接续时已完成账号直接跳过
    _progress: ProgressStore = None
    _runner: _MultiTasksRunner = None
    # {acc_key: 来源配置名}，仅供日志使用（Runner 的 account_list 不带来源信息）
    _source_names: dict = None
    def _record_login_time(self, account) -> None:
        """登记账号来源，由统一配置事务同步全部任务及本机映射的原配置。"""
        source_name = (self._source_names or {}).get(
            acc_key(account.account, account.character, account.svr), '?')
        self.config.record_account_login(
            account, config_names=() if source_name == '?' else (source_name,))

    def _notify_warnings(self, warnings: list) -> None:
        """来源提醒（如未匹配的角色名）汇总后只推送一次，本身不判失败。"""
        if not warnings:
            return
        logger.warning(f'[MultiTasks] 未匹配角色名: {warnings}')
        self.config.notifier.push(
            title='多账号任务配置未找到',
            content='以下角色名未匹配到任何账号：' + '、'.join(warnings),
        )

    def _switch_and_run(self, account) -> bool:
        """切换到指定账号并执行选定的子任务。

        @return: True 表示当前账号成功，False 表示可隔离的账号级失败。
                 必须向上抛出的设备级异常不在此处吞掉（由 Runner 穿透）。
        """
        sub_task = self.config.multi_tasks.multi_tasks_config.sub_task
        spec = SUB_TASKS[sub_task]
        source_name = (self._source_names or {}).get(
            acc_key(account.account, account.character, account.svr), '?')

        logger.hr(f'MultiTasks {spec.task_end_name}: {account.character}/{account.svr}', 2)
        logger.info(
            f'[MultiTasks] 切换账号: config={source_name}, '
            f'character={account.character}, server={account.svr}'
        )
        if not self._runner.switch_to_account(account):
            logger.error(
                f'[MultiTasks] 切换账号失败: '
                f'{source_name}/{account.character}/{account.svr}'
            )
            return False

        # 成功登录登记来源；保存时批量回写，后续子任务失败也保留真实登录记录。
        self._record_login_time(account)

        # 切号成功后创建全新的子任务适配器，确保可变状态不跨账号共享
        adapter = ADAPTERS[sub_task](self.config, self.device)
        # 注入「当前跑的是哪个账号」的上下文，与 MultiDailyAltAcc 用同一约定。
        # 子任务据此给产物命名（如觉醒副本的协战截图 screenshots/EvoZone_Screenshots/<角色名>.png）；
        # 不注入时读取方只会静默退化成配置实例名，导致多账号互相覆盖同一张图。
        # 账号导出用该上下文写入来源身份 oas_identity；其他子任务可忽略。
        adapter._stat_ctx = {
            'acc': account.account,
            'char': account.character,
            'svr': account.svr,
        }
        # 注入账号级场次进度：计数型子任务（觉醒副本）逐场落盘，任务被中断后
        # 重跑能从断点接续剩余场次；键同样按账号取，否则多账号会共用一份计数。
        # 不读进度的子任务拿到也无副作用（它们不访问这两个属性）。
        adapter._progress = self._progress
        adapter._progress_key = acc_key(account.account, account.character, account.svr)
        try:
            adapter.run()
        except TaskEnd as e:
            # 仅当 TaskEnd 表示本子任务才视为当前账号正常完成；其他 TaskEnd 上抛
            if e.args and e.args[0] == spec.task_end_name:
                if sub_task == SubTaskType.ACCOUNT_EXPORT:
                    # 导出必须有落盘文件，不能只因收到结束信号就跳过这个账号。
                    if (self._progress is None or not self._progress.is_task_finished(
                            adapter._progress_key, 'account_export')):
                        logger.error('[MultiTasks] 导出没有成功文件，保留账号等待重试')
                        return False
                logger.info(
                    f'[MultiTasks] 账号完成: {source_name}/{account.character}/{account.svr}'
                )
                return True
            raise
        if sub_task == SubTaskType.ACCOUNT_EXPORT:
            # 独立导出任务的普通失败返回 False；任何普通返回都不能标记导出成功。
            logger.error('[MultiTasks] 账号导出未成功结束，保留账号等待重试')
            return False
        # 其他既有子任务未抛 TaskEnd 时仍沿用原来的成功加警告行为。
        logger.warning(f'[MultiTasks] 子任务未抛出 TaskEnd({spec.task_end_name})')
        return True

    def run(self):
        cfg = self.config.multi_tasks.multi_tasks_config

        # 1. 按选定来源加载账号
        items, warnings, load_failure = ACCOUNT_SOURCES[cfg.account_source](self.config)

        # 2. 来源提醒只推一次（未匹配本身不判失败）
        self._notify_warnings(warnings)

        # 3. 空账号集合属配置错误：判失败，按失败间隔重调度
        if not items:
            logger.error(
                f'[MultiTasks] 未加载到有效账号: source={cfg.account_source.value}'
            )
            self.set_next_run('MultiTasks', finish=True, success=False, server=False)
            raise TaskEnd('MultiTasks')

        self._source_names = {
            acc_key(account.account, account.character, account.svr): source_name
            for source_name, account in items
        }
        accounts = [account for _source_name, account in items]

        # 4. 建立账号级续做进度。阶段标识含子任务 + 来源方式 + 账号集合 + 自然日：
        #    改子任务或改来源都必须重建（绝不能沿用另一组合的完成标记）；
        #    success_interval 为 1 天，跨天必须重做；失败重调度不改标识，接续时
        #    已完成账号直接跳过，避免重复领奖或重复消耗体力。
        self._progress = ProgressStore('multi_tasks', self.config.config_name)
        self._progress.ensure_phase(
            {
                'sub_task': cfg.sub_task.value,
                'account_source': cfg.account_source.value,
                'accounts': [acc_key(a.account, a.character, a.svr) for a in accounts],
                'day': self.start_time.strftime('%Y-%m-%d'),
            },
            self.start_time.strftime('%Y%m%d-%H%M'),
        )

        # 5. 轮转执行。账号完成状态由 progress 驱动；登录时间由 _switch_and_run
        #    在切号成功后主动回写，Runner 自身不重复触发回调。
        self._runner = _MultiTasksRunner(
            task_name='MultiTasks',
            config=self.config,
            device=self.device,
            account_list=accounts,
            need_login=False,
            login_time=self.start_time,
            update_login_history_func=lambda account: None,
            save_config_func=lambda: None,
            on_account_error=self._on_account_error,
            progress=self._progress,
        )
        success = self._runner.run(process_func=self._switch_and_run)

        # 6. 汇总结果，只更新 MultiTasks 自身调度
        self.set_next_run(
            'MultiTasks',
            finish=True,
            success=success and not load_failure,
            server=False,
        )
        # 7. 全部成功收尾后才清进度：先调度后清，顺序不可颠倒（否则有「调度已改、
        #    进度已删」的窗口导致整轮重跑）；load_failure 时账号集合不完整，同样保留
        if success and not load_failure:
            self._progress.clear()
        raise TaskEnd('MultiTasks')

    def _on_account_error(self, account, error) -> None:
        """账号级异常回调：只负责通知留痕，续接由进度文件驱动。"""
        sub_task = self.config.multi_tasks.multi_tasks_config.sub_task
        self.config.notifier.push(
            title='ERROR',
            content=(f'{account.character}-{account.svr} '
                     f'{SUB_TASKS[sub_task].task_end_name} 执行错误\nError: {error}'),
        )


if __name__ == '__main__':
    from module.config.config import Config
    from module.device.device import Device

    config = Config('oas')
    device = Device(config)
    task = ScriptTask(config, device)
    task.run()
