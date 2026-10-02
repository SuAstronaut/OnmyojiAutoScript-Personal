# This Python file uses the following encoding: utf-8
# @author runhey
# github https://github.com/runhey
from datetime import datetime, timedelta
import re  # 名单支持换行、逗号和分号分隔。

from module.config.utils import convert_to_underscore

from tasks.Restart.login import LoginHandler

from module.logger import logger
from module.exception import (TaskEnd, RequestHumanTakeover, GameNotRunningError,
                              GameStuckError, GameTooManyClickError)

# 桌面模式客户端启动失败后的重建轮数。Restart 必须自己扛住启动失败，
# 抛给调度器会被 task_call('Restart') 打回这里形成无限循环
DESKTOP_RESTART_ATTEMPTS = 3


class ScriptTask(LoginHandler):

    def _get_login_handoff_task(self) -> str:
        """按当前调度顺序选择接手任务，不提前运行未来任务或越过普通任务。"""
        # 桌面客户端不支持切号，名单仅对模拟器生效，桌面继续完整登录。
        if self.device.is_desktop:
            return ''
        handoff = getattr(self.config.restart, 'login_handoff_config', None)
        names = getattr(handoff, 'login_handoff_tasks', '')
        allowed = {convert_to_underscore(name) for name in re.split(r'[\s,，;；]+', names) if name}
        if not allowed:
            return ''
        self.config.update_scheduler()
        for task in self.config.pending_task:
            if task.command == 'Restart':
                continue
            # 调度器会跳过禁止时段内的任务；这里使用同一规则，但不修改其计划时间。
            if self.config.get_forbidden_time_end(task.command) is not None:
                continue
            if convert_to_underscore(task.command) in allowed:
                return task.command
            return ''
        return ''

    def run(self) -> None:
        """
        主要就是登录的模块
        :return:
        """
        if not self.delay_pending_tasks():
            self.app_restart()
        raise TaskEnd('ScriptTask end')

    def app_stop(self):
        logger.hr('App stop')
        self.device.app_stop()

    def app_start(self):
        logger.hr('App start')
        # 首次启动也纳入各平台的登录重试边界，避免启动失败反复回调 Restart。
        if self.device.is_desktop:
            self._desktop_start_and_login()
        else:
            self.app_handle_login(start_app=True)

    def app_restart(self):
        logger.hr('App restart')
        # 桌面分支：客户端可能刚被 OAS 自动启动（已在登录页），直接停掉会白关一次再重开，
        # 只需确保客户端运行并走登录；交互与模拟器不同，隔离在桌面分支
        if self.device.is_desktop:
            self._desktop_start_and_login()
        else:
            self.device.app_stop()
            # 启动与登录共享重试次数，首次启动失败也不能逃出重试边界。
            self.app_handle_login(start_app=True)

        # 仅启用领取时安排每天 12/20 点的体力登录；明确目标时间不再被 server_update 覆盖。
        harvest = self.config.restart.harvest_config
        if harvest.enable_ap and harvest.enable:
            now = datetime.now()
            if now.hour < 12:
                target = now.replace(hour=12, minute=0, second=0, microsecond=0)
            elif now.hour < 20:
                target = now.replace(hour=20, minute=0, second=0, microsecond=0)
            else:
                target = (now + timedelta(days=1)).replace(hour=12, minute=0, second=0, microsecond=0)
            self.set_next_run(task='Restart', target=target, finish=True, server=False)
        else:
            # 不领取体力时保留默认重启计划；每次完成只保存一次调度时间。
            self.set_next_run(task='Restart', success=True, finish=True, server=True)

    def _desktop_start_and_login(self) -> None:
        """桌面模式：启动客户端并登录，客户端没起来就重建，不把异常抛给调度器。

        Restart 是负责启动客户端的那个任务，所以它必须自己扛住客户端起不来的情况：
        若把 GameNotRunningError 抛出去，script.py 接住后又会 task_call('Restart')
        重新进到这里，形成无限重启循环——每轮日志都「正常」，比直接崩更难排查。
        GameStuckError 同理，script.py 对它也是 task_call('Restart')，所以登录卡死
        也必须在这里就地消化，不能放跑。
        因此这里就地重试：杀掉残留进程后重新走一遍启动+登录，连续失败才交人工。
        """
        for attempt in range(1, DESKTOP_RESTART_ATTEMPTS + 1):
            try:
                self.device.app_start()
                self.app_handle_login()
                return
            except (GameNotRunningError, GameStuckError, GameTooManyClickError) as e:
                logger.warning(f'桌面客户端启动后仍未就绪（第 {attempt}/{DESKTOP_RESTART_ATTEMPTS} 轮）: {e}')
                # 每轮失败都必须清掉本轮客户端，最后一轮也不例外：原来最后一轮直接 break
                # 跳过清理，紧接着 RequestHumanTakeover 让进程退出，那个客户端就成了无主
                # 残留（实测泄漏过一个 PID）；守护进程重启实例后又会新起一个，越积越多。
                # 中途轮次清理还有另一层作用：避免残留窗口干扰下一轮的新窗口识别
                logger.info('清理本轮残留客户端')
                if not self.device.desktop_stop_client():
                    # 关不掉就别重建：残留窗口会让下一轮的新窗口识别绑错句柄，
                    # 越试越乱，不如立刻停下交人工处理
                    logger.critical('桌面客户端无法关闭，残留进程会干扰重建，请手动结束该进程')
                    raise RequestHumanTakeover
        logger.critical(f'桌面客户端连续 {DESKTOP_RESTART_ATTEMPTS} 轮启动失败，请检查客户端与机器状态')
        raise RequestHumanTakeover

    def delay_pending_tasks(self) -> bool:
        """
        周三更新游戏的时候延迟
        @return:
        """
        datetime_now = datetime.now()
        if not (datetime_now.weekday() == 2 and 6 <= datetime_now.hour <= 8):
            return False
        logger.info("The game server is updating, delay the pending tasks to 9:00")
        logger.warning('Delay pending tasks')
        # 维护结束当天恢复待执行任务；传 server=False，避免强制运行时间把任务延到次日。
        target = datetime_now.replace(hour=9, minute=0, second=0, microsecond=0)
        for task in self.config.pending_task:
            if task.command != 'Restart':
                self.set_next_run(task=task.command, target=target, server=False)
        # Restart 也在维护结束后补登录，不将本次跳过误当作已完成登录。
        self.set_next_run(task='Restart', target=target, server=False)
        return True


if __name__ == '__main__':
    from module.config.config import Config
    from module.device.device import Device

    config = Config('oas1')
    device = Device(config)
    task = ScriptTask(config, device)
    task.app_restart()
