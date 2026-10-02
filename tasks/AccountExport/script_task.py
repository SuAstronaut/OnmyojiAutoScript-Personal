"""导出当前登录角色；MultiTasks 可复用同一流程逐账号执行。"""
from pathlib import Path

from module.exception import TaskEnd
from module.logger import logger
from tasks.AccountExport.assets import AccountExportAssets
from tasks.Component.MultiAccountRunner.progress import STATUS_DONE, STATUS_PENDING
from tasks.GameUi.game_ui import GameUi
from tasks.GameUi.page import page_main


class ScriptTask(GameUi, AccountExportAssets):
    """独立运行不切号，批量运行使用父任务注入的角色身份和进度。"""
    _stat_ctx = None
    _progress = None
    _progress_key = None

    def run(self):
        """只有完整文件写入成功才发出成功结束信号，设备异常保持上抛。"""
        # 延迟加载导出器，其他 MultiTasks 子任务不需要初始化采集依赖。
        from tasks.Utils.yyx_export import YyxExportError, export_account_snapshot

        progress, key = self._progress, self._progress_key
        if progress is not None and key:
            # 重新采集前清除旧成功状态，超时或中断后仍可接续。
            progress.mark_task(key, 'account_export', STATUS_PENDING, path='', etype='', emsg='')
        try:
            self.ui_get_current_page()
            self.ui_goto(page_main)
            # 账号上下文用于写入来源身份，角色数据仍以实际采集结果为准。
            result = export_account_snapshot(self.device, self.config.config_name, self._stat_ctx)
            path = result.get('path')
            if not isinstance(path, str) or not path or not Path(path).is_file():
                raise YyxExportError('导出未生成有效文件，保留任务等待重试')
            if progress is not None and key:
                progress.mark_task(key, 'account_export', STATUS_DONE, path=path, etype='', emsg='')
            logger.info('账号数据导出成功：%s', path)
        except YyxExportError as error:
            # 普通失败不冒充 TaskEnd 成功；批量父任务会按 False 保留待重试账号。
            if progress is not None and key:
                progress.mark_task(key, 'account_export', STATUS_PENDING,
                                   path='', etype=type(error).__name__, emsg=str(error))
            logger.error('账号数据导出失败：%s', error)
            self.set_next_run('AccountExport', finish=True, success=False, server=False)
            return False

        # 批量调用时由标准调度屏蔽适配器拦截，仅父任务安排整轮下次运行。
        self.set_next_run('AccountExport', finish=True, success=True, server=False)
        raise TaskEnd('AccountExport')


if __name__ == '__main__':
    # 命令行显式选择 OAS 配置，导出该实例当前登录角色。
    import argparse
    from module.config.config import Config
    from module.device.device import Device

    parser = argparse.ArgumentParser(description='导出当前登录角色的账号数据')
    parser.add_argument('--config', required=True, help='OAS 配置名')
    args = parser.parse_args()
    config = Config(args.config)
    ScriptTask(config, Device(config)).run()
