# This Python file uses the following encoding: utf-8
# @author runhey
# github https://github.com/runhey

# 必须最先执行：pyzmq（zerorpc 的依赖）会与 onnxruntime 抢 DLL 加载顺序，
# 先加载 pyzmq 会让后续 OCR 后端初始化失败。详见 module/ocr/preload.py
from module.ocr.preload import preload_ocr_backend

preload_ocr_backend()

import atexit
import zerorpc
import zmq
import msgpack
import random
import re
import cv2
import time
import os
import inflection
import asyncio
import json
import threading
import urllib.request
from typing import Callable
from datetime import datetime, timedelta
from pathlib import Path
from cached_property import cached_property
from threading import Thread
from multiprocessing.queues import Queue


from module.config.config import Config
from module.device.device import Device, EmulatorState
from module.base.utils import load_module
from module.logger import logger
from module.exception import *
from module.server.i18n import I18n
from module.ocr.rpc import ensure_ocr_server_started, notify_ocr_instance_state
from module.server.setting import State



_log_switch_lock = threading.Lock()#线程锁


class Script:
    TASK_END_NOTIFY_LIST = [
        'Orochi',
        'RealmRaid',
        'ReturnGift',
        'RyouToppa',
        'SixRealms',
        'FallenSun',
        'Exploration',
        'EvoZone',
        'Dokan',
        'ActivityShikigami',
        'AbyssShadows',
        'EternitySea',
        'BondlingFairyland',
    ]

    # 不需要游戏运行的任务，跳过 app_is_running 检测
    SKIP_APP_CHECK_TASKS = [
        'Restart',
        'AutoCheckinBigGod',
    ]

    def __init__(self, config_name: str ='oas') -> None:
        logger.hr('Start', level=0)
        self.server = None
        self.state_queue: Queue = None
        # 主进程→子进程配置变更提示队列（由 ScriptProcess.func 注入；独立运行脚本时为 None）
        self.config_event_queue: Queue = None
        self.gui_update_task: Callable = None  # 回调函数, gui进程注册当每次config更新任务的时候更新gui的信息
        self.config_name = config_name
        # 仅跳过启动时立即到期的首次 Restart；进入等待后应按正常计划执行。
        self.is_first_task = True
        # 连续页面识别失败（GamePageUnknownError）计数：登录后弹窗链遮挡等偶发
        # 场景下一轮任务重试时常能自愈，给满 3 次机会才重启客户端兜底；
        # 任务正常结束（TaskEnd）即清零。
        self.page_unknown_count = 0
        # 页面未识别失败的放行标志：该异常已自行安排恢复策略（重试/满 3 次
        # Restart），即使 error.handle_error=False 也不允许停摆调度器，由
        # loop() 消费后重置。
        self._page_unknown_recoverable = False
        # Set True by reactive health check (Task 19) when an exception
        # confirms emulator is ZOMBIE; next loop iteration triggers full_recovery.
        self._needs_recovery = False
        # 运行loop的线程
        self.loop_thread: Thread = None
        # OCR 实例状态通知采用任务嵌套计数，避免任务内部调用 run() 时提前注销。
        self._ocr_task_depth = 0
        self._ocr_unregister_registered = False

    @cached_property
    def config(self) -> "Config":
        try:
            from module.config.config import Config
            config = Config(config_name=self.config_name)
            # 注册 HOT 状态上报 hook：HOT 提交/失败后立即向 state_queue 上报最新 config_state
            # （规格 §11.1 点 4；reporter 幂等，state_queue 为 None 时 no-op）
            config._state_reporter = self._report_config_state
            return config
        except RequestHumanTakeover:
            logger.critical('Request human takeover')
            exit(1)
        except Exception as e:
            logger.exception(e)
            exit(1)

    @cached_property
    def device(self) -> "Device":
        try:
            from module.device.device import Device
            # Device 构造前建立 provisional COLD 快照；构造完成后立即冻结为正式快照。
            # 进程内游戏 Restart 不重建快照，只有新脚本进程/新 Config session 才重新划定边界。
            self.config.begin_device_initialization()
            device = Device(config=self.config)
            self.config.freeze_startup_device_snapshot()
            return device
        except RequestHumanTakeover:
            # 初始化阶段 full_recovery 失败，主动请求 server 级重启并退出。
            logger.critical('Request human takeover during device init, request server restart')
            self._exit_for_server_restart()
        except Exception as e:
            logger.exception(e)
            exit(1)

    @cached_property
    def checker(self):
        """
        占位函数，在alas中是检查服务器是否正常的
        :return:
        """
        return None

    def save_error_log(self):
        """
        Save last 60 screenshots in ./log/error/<timestamp>
        Save logs to ./log/error/<timestamp>/log.txt
        """
        from module.base.utils import save_image
        from module.handler.sensitive_info import (handle_sensitive_image,
                                                   handle_sensitive_logs)
        if self.config.script.error.save_error:
            if not os.path.exists('./log/error'):
                os.mkdir('./log/error')
            folder_name = str(int(time.time() * 1000))
            folder = f'./log/error/{folder_name}'
            logger.warning(f'Saving error: {folder}')
            logger.info('保存详细错误的日志和截图到路径:')
            logger.info(f'{str( Path.cwd() / "log" / "error" / folder_name)}')
            os.mkdir(folder)
            for data in self.device.screenshot_deque:
                image_time = datetime.strftime(data['time'], '%Y-%m-%d_%H-%M-%S-%f')
                image = handle_sensitive_image(data['image'])
                save_image(image, f'{folder}/{image_time}.png')
            with open(logger.log_file, 'r', encoding='utf-8') as f:
                lines = f.readlines()
                start = 0
                for index, line in enumerate(lines):
                    line = line.strip(' \r\t\n')
                    if re.match('^═{15,}$', line):
                        start = index
                lines = lines[start - 2:]
                lines = handle_sensitive_logs(lines)
            with open(f'{folder}/log.txt', 'w', encoding='utf-8') as f:
                f.writelines(lines)

    def init_server(self, port: int) -> int:
        """
        初始化zerorpc服务，返回端口号
        :return:
        """
        self.server = zerorpc.Server(self)
        try:
            self.server.bind(f'tcp://127.0.0.1:{port}')
            return port
        except zmq.error.ZMQError:
            logger.error(f"Ocr server cannot bind on port {port}")
            return None

    def run_server(self) -> None:
        """
        启动zerorpc服务
        :return:
        """
        self.server.run()

    def gui_args(self, task: str) -> str:
        """
        获取给gui显示的参数
        :return:
        """
        return self.config.gui_args(task=task)

    def gui_menu(self) -> str:
        """
        获取给gui显示的菜单
        :return:
        """
        return self.config.gui_menu

    def gui_task(self, task: str) -> str:
        """
        获取给gui显示的任务 的参数的具体值
        :return:
        """
        return self.config.model.gui_task(task=task)

    def gui_set_task(self, task: str, group: str, argument: str, value) -> bool:
        """
        设置给gui显示的任务 的参数的具体值
        统一走 ConfigStore.patch_user_argument，脚本是否存活不改变持久化路径
        :return:
        """
        # pandtic验证
        if isinstance(value, str):
            if len(value) == 8:
                try:
                    value = datetime.strptime(value, '%H:%M:%S').time()
                except ValueError:
                    pass

        try:
            result = self.config.store.patch_user_argument(self.config_name, task, group, argument, value)
            if result.success:
                logger.info(f'Set arg {task}.{group}.{argument}.{value}')
            return result.success
        except Exception as e:
            logger.error(f'Set arg {task}.{group}.{argument}.{value} failed: {e}')
            return False

    @zerorpc.stream
    def gui_mirror_image(self):
        """
        获取给gui显示的镜像
        :return: cv2的对象将 numpy 数组转换为字节串。接下来MsgPack 进行序列化发送方将图像数据转换为字节串
        """
        # return msgpack.packb(cv2.imencode('.jpg', self.device.screenshot())[1].tobytes())
        img = cv2.cvtColor(self.device.screenshot(), cv2.COLOR_RGB2BGR)
        self.device.stuck_record_clear()
        ret, buffer = cv2.imencode('.jpg', img)
        yield buffer.tobytes()

    def _gui_update_tasks(self) -> None:
        """
        获取更新任务后 pending waiting 的任务 和 当前的任务的数据。打包给gui显示
        :return:
        """
        data = {}
        pending = []
        waiting = []
        task = {}
        if self.config.task is not None and self.config.task.next_run < datetime.now():
            task["name"] = self.config.task.command
            task["next_run"] = str(self.config.task.next_run)
        data["task"] = task

        for p in self.config.pending_task[1:]:
            item = {"name": p.command, "next_run": str(p.next_run)}
            pending.append(item)

        for w in self.config.waiting_task:
            item = {"name": w.command, "next_run": str(w.next_run)}
            waiting.append(item)


        data["pending"] = pending
        data["waiting"] = waiting

        if self.gui_update_task is not None:
            self.gui_update_task(data)

    def _gui_set_status(self, status: str) -> None:
        """
        设置给gui显示的状态
        :param status: 可以在gui中显示的状态 有 "Init", "Empty"(不显示), "Run"(运行中), "Error", "Free"(空闲)
        :return:
        """
        data = {"status": status}
        if self.gui_update_task is not None:
            self.gui_update_task(data)

    def gui_task_list(self) -> str:
        """
        获取给gui显示的任务列表
        :return:
        """
        result = {}
        for key, value in self.config.model.dict().items():
            if isinstance(value, str):
                continue
            if key == "restart":
                continue
            if "scheduler" not in value:
                continue

            scheduler = value["scheduler"]
            item = {"enable": scheduler["enable"],
                    "next_run": str(scheduler["next_run"])}
            key = self.config.model.type(key)
            result[key] = item
        return json.dumps(result)




    # ------------------------------------------------------------------ 跨进程配置事件

    def _drain_config_events(self) -> list:
        """非阻塞排空 config_event_queue，丢弃旧 generation 事件，返回本次累计 changed_paths。"""
        queue = self.config_event_queue
        if queue is None:
            return []
        changed_paths: list = []
        while True:
            try:
                event = queue.get_nowait()
            except Exception:
                break
            if not isinstance(event, dict):
                continue
            if event.get("type") != "config_changed":
                continue
            if event.get("generation") and event["generation"] != self.config.generation:
                # 旧 generation 事件直接丢弃；真正的身份变化由 refresh_from_disk 检测
                continue
            for p in (event.get("changed_paths") or []):
                # str 路径视为单段，避免被 tuple() 拆成字符（防御）
                changed_paths.append(tuple(p) if isinstance(p, (tuple, list)) else (p,))
        if changed_paths:
            self.config.report_config_changed(changed_paths)
        return changed_paths

    def _report_config_state(self) -> None:
        """把最新 config_state 上报给主进程（经由 state_queue），供 WebSocket 定向首帧使用。"""
        if self.state_queue:
            self.state_queue.put({"config_state": self.config.config_state()})

    def _config_checkpoint(self, trigger: str) -> None:
        """任务边界/检查点：排空配置事件，执行 WARM/COLD 刷新并上报 config_state。

        generation mismatch 或配置被删除时终止当前实例（规格 §10.3）。
        """
        self._drain_config_events()
        result = self.config.refresh_from_disk(trigger)
        self._report_config_state()
        if result.generation_mismatch or self.config.generation_mismatch:
            logger.critical(f'[{self.config_name}] Config generation changed, stop instance')
            exit(1)

    def wait_until(self, future):
        """
        Wait until a specific time.

        Args:
            future (datetime):

        Returns:
            bool: True if wait finished, False if config changed.
        """
        future = future + timedelta(seconds=1)
        self.config.start_watching()
        while 1:
            if datetime.now() > future:
                return True
            # if self.stop_event is not None:
            #     if self.stop_event.is_set():
            #         logger.info("Update event detected")
            #         logger.info(f"[{self.config_name}] exited. Reason: Update")
            #         exit(0)

            time.sleep(5)

            # 先排空配置事件；无事件仍以 mtime_ns 兜底检测（规格 §4.4）
            self._drain_config_events()
            if self.config.should_reload() or self.config.has_pending_changes():
                result = self.config.refresh_from_disk("wait")
                self._report_config_state()
                # mismatch 时立即退出，不能忽略返回值继续调度（避免 wait 忙循环）
                if result.generation_mismatch or self.config.generation_mismatch:
                    logger.critical(f'[{self.config_name}] Config generation changed, stop instance')
                    exit(1)
                return False

    def get_next_task(self) -> str:
        """
        获取下一个任务的名字, 大驼峰。
        :return:
        """
        while 1:
            task = self.config.get_next()
            self.config.task = task
            if self.state_queue:
                self.state_queue.put({"schedule": self.config.get_schedule_data()})

            if task.next_run > datetime.now():
                logger.info(f'Wait until {task.next_run} for task `{task.command}`')
                # 已进入正常调度等待，稍后到期的 Restart 不能再按首次启动任务跳过。
                self.is_first_task = False
                # 首次访问 self.device 会拉起模拟器；冷启动无到期任务时只能等待。
                # 只有已经创建过设备，才执行空闲关闭/回庭院与资源释放。
                device = self.__dict__.get('device')
                if device is not None:
                    method = self.config.script.optimization.when_task_queue_empty
                    close_game_limit_time = self.config.script.optimization.close_game_wait_duration
                    close_emulator_limit_time = self.config.script.optimization.close_emulator_wait_duration

                    if method == 'goto_main':
                        self._handle_goto_main()
                    elif method == 'close_game':
                        self._handle_close_game(task, close_game_limit_time)
                    elif method in ['close_emulator_or_goto_main', 'close_emulator_or_close_game']:
                        self._handle_close_emulator_or(task, close_game_limit_time, close_emulator_limit_time, method)
                    elif method != 'stay_there':
                        logger.warning(f'Invalid Optimization_WhenTaskQueueEmpty: {method}, fallback to stay_there')

                    device.release_during_wait()

                if not self.wait_until(task.next_run):
                    result = self.config.refresh_from_disk("wait_return")
                    if result.generation_mismatch or self.config.generation_mismatch:
                        # exit 前补一次上报，避免主进程缓存停留在旧值直到 is_alive 轮询清理
                        self._report_config_state()
                        logger.critical(f'[{self.config_name}] Config generation changed, stop instance')
                        exit(1)
                    continue
            # 立即到期和等待后到期都要检查禁止时段，避免等待期间跨入禁用区间后上号。
            forbidden_end = self.config.get_forbidden_time_end(task.command)
            if forbidden_end is not None:
                # 解禁时刻叠加随机延时，避免每次都在解禁的整点准点上线。
                random_delay = self.config.get_task_random_delay(task.command)
                if random_delay is not None:
                    forbidden_end = forbidden_end + random_delay
                logger.info(f'Task `{task.command}` 处于禁止运行时间段内，推迟到 {forbidden_end}')
                self.config.task_delay(task.command, target=forbidden_end, server=False)
                continue
            break

        return task.command

    def _handle_goto_main(self):
        logger.info('Goto main page during wait')
        self.run('GotoMain')

    def _handle_close_game(self, task, close_game_limit_time):
        if task.next_run > datetime.now() + timedelta(hours=close_game_limit_time.hour, minutes=close_game_limit_time.minute, seconds=close_game_limit_time.second):
            logger.info('Close game during wait')
            self.device.app_stop()
        else:
            self._handle_goto_main()

    def _handle_close_emulator_or(self, task, close_game_limit_time, close_emulator_limit_time, method):
        if task.next_run > datetime.now() + timedelta(hours=close_emulator_limit_time.hour, minutes=close_emulator_limit_time.minute, seconds=close_emulator_limit_time.second):
            logger.info('Close emulator during wait')
            self.device.emulator_stop()
        elif method == 'close_emulator_or_goto_main':
            self._handle_goto_main()
        else:
            self._handle_close_game(task, close_game_limit_time)

    @staticmethod
    def _normalize_task_name(task_name: str) -> str:
        return task_name.strip().lower()

    def _resolve_task_end_name(self, command: str, error: TaskEnd) -> str:
        task_name = command
        if error.args and isinstance(error.args[0], str) and error.args[0].strip():
            task_name = error.args[0].strip()
        logger.info(f'TaskEnd final task name: {task_name}')
        return task_name

    def _should_notify_task_end(self, task_name: str) -> bool:
        # MultiDailyAltAcc 不在 TASK_END_NOTIFY_LIST 中：其整轮完成通知由任务内
        # _notify_daily_completion 统一负责——有协作/神秘商店汇总发汇总，否则发
        # 普通完成推送（含本轮执行项目），此处通用「任务提醒」不再参与，避免重复。
        # 其他任务保持原有完成提醒，全局 notifier 行为不变。
        notify_task_end_list = self.TASK_END_NOTIFY_LIST
        if isinstance(notify_task_end_list, str):
            notify_task_end_list = [notify_task_end_list]
        if not isinstance(notify_task_end_list, (list, tuple, set)):
            notify_task_end_list = []

        normalized_notify_list = {
            self._normalize_task_name(item)
            for item in notify_task_end_list
            if isinstance(item, str) and item.strip()
        }
        logger.info(f'TaskEnd notify list: {list(normalized_notify_list)}')

        if not normalized_notify_list:
            logger.info('TaskEnd notify list is empty, skip notify')
            return False

        normalized_task_name = self._normalize_task_name(task_name)
        hit = normalized_task_name in normalized_notify_list
        logger.info(f'TaskEnd notify list {"hit" if hit else "miss"}: {task_name}')
        return hit

    def _ocr_task_start(self) -> None:
        """通知 OCR RPC 当前实例开始执行任务。"""
        self._ocr_task_depth = getattr(self, '_ocr_task_depth', 0) + 1
        if self._ocr_task_depth != 1:
            return
        if not getattr(self, '_ocr_unregister_registered', False):
            atexit.register(self._ocr_task_cleanup)
            self._ocr_unregister_registered = True
        notify_ocr_instance_state(getattr(self, 'config_name', 'oas'), True)

    def _ocr_task_end(self) -> None:
        """通知 OCR RPC 当前实例结束执行任务。"""
        self._ocr_task_depth = max(0, getattr(self, '_ocr_task_depth', 0) - 1)
        if self._ocr_task_depth == 0:
            notify_ocr_instance_state(getattr(self, 'config_name', 'oas'), False)

    def _ocr_task_cleanup(self) -> None:
        """进程正常退出时注销实例，避免 RPC 保留过期任务状态。"""
        if getattr(self, '_ocr_task_depth', 0):
            self._ocr_task_depth = 0
            notify_ocr_instance_state(getattr(self, 'config_name', 'oas'), False)

    def run(self, command: str) -> bool:
        """在任务执行期间向 OCR RPC 保持实例活跃状态。"""
        self._ocr_task_start()
        try:
            return self._run_task(command)
        finally:
            # 任务正常或异常收尾都刷新已确认的登录；不为无配置的启动失败新建会话。
            config = self.__dict__.get('config')
            if config is not None:
                try:
                    config.flush_account_login_times()
                except Exception:
                    logger.exception('任务收尾同步登录时间失败，保留待回写记录')
            self._ocr_task_end()

    def _run_task(self, command: str) -> bool:
        """

        :param command:  大写驼峰命名的任务名字
        :return:
        """
        if command == 'start' or command == 'goto_main':
            logger.error(f'Invalid command `{command}`')

        try:
            # SKIP_APP_CHECK_TASKS 里的任务不要求游戏已在运行，启动前截图对它们没有意义：
            # 桌面模式下客户端未启动时截图会抛 GameNotRunningError，而 Restart 正是负责
            # 启动客户端的那个任务，若它自己也被挡在截图这一步就永远起不来（bootstrap 死锁）。
            # 这些任务进入后自行 app_start 拉起客户端再截图。
            if command not in self.SKIP_APP_CHECK_TASKS:
                self.device.screenshot()
                if not self.device.app_is_running():
                    raise GameNotRunningError('Game not running')
            module_name = 'script_task'
            module_path = str(Path.cwd() / 'tasks' / command / (module_name+'.py'))
            logger.info(f'module_path: {module_path}, module_name: {module_name}')
            task_module = load_module(module_name, module_path)
            task_module.ScriptTask(config=self.config, device=self.device).run()
        except TaskEnd as e:
            task_name = self._resolve_task_end_name(command, e)
            # 任务正常结束说明页面识别已恢复，清零连续未识别计数
            self.page_unknown_count = 0
            if self._should_notify_task_end(task_name):
                self.config.notifier.push(
                    title=f'任务提醒',
                    content=f"{I18n.trans_zh_cn(command)} 任务执行完毕，完成时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
                )
            return True
        except EmulatorNotRunningError as e:
            logger.warning(e)
            if self.device.emulator_state == EmulatorState.HEALTHY:
                self.device._transition_to(EmulatorState.ZOMBIE)
            self._needs_recovery = True
            self.config.task_call('Restart')
            return False
        except GameNotRunningError as e:
            logger.warning(e)
            self.config.task_call('Restart')
            return False
        except (GameStuckError, GameTooManyClickError) as e:
            logger.error(e)
            self.save_error_log()
            logger.warning(f'Game stuck, {self.device.package} will be restarted in 10 seconds')
            logger.warning('If you are playing by hand, please stop Alas')
            self.config.notifier.push(title=f'{I18n.trans_zh_cn(command)}{command}', content=f"<{self.config_name}> GameStuckError or GameTooManyClickError")
            self.config.task_call('Restart')
            self.device.sleep(10)
            return False
        except GameBugError as e:
            logger.warning(e)
            self.save_error_log()
            logger.warning('An error has occurred in Azur Lane game client, Alas is unable to handle')
            logger.warning(f'Restarting {self.device.package} to fix it')
            self.config.task_call('Restart')
            self.device.sleep(10)
            return False
        except GamePageUnknownError:
            logger.info('Game server may be under maintenance or network may be broken, check server status now')
            # 连续 3 次无法识别页面才重启客户端：偶发弹窗遮挡在下一轮任务重试时
            # 往往已自行消失（弹窗有有效期），立刻重启代价过高；3 次内恢复识别则
            # 由 TaskEnd 分支清零计数
            self.page_unknown_count += 1
            logger.critical(f'Game page unknown ({self.page_unknown_count}/3)')
            self.save_error_log()
            self.config.notifier.push(title=f'{I18n.trans_zh_cn(command)}{command}', content=f"<{self.config_name}> GamePageUnknownError")
            if self.page_unknown_count >= 3:
                # 连续多次认不出页面（弹窗卡死/维护等），重启客户端兜底并重新计数
                logger.critical('Game page unknown for 3 consecutive tasks, restart app to recover')
                self.config.task_call('Restart')
                self.page_unknown_count = 0
            # 页面未识别已自行安排恢复（下一轮重试或 Restart），标记放行，
            # 避免 error.handle_error=False 时调度器停摆
            self._page_unknown_recoverable = True
            return False
        except ScriptError as e:
            logger.critical(e)
            logger.critical('This is likely to be a mistake of developers, but sometimes just random issues')
            self.config.notifier.push(title=f'{I18n.trans_zh_cn(command)}{command}', content=f"<{self.config_name}> ScriptError")
            exit(1)
        except RequestHumanTakeover as e:
            logger.critical(e)
            logger.critical('Request human takeover')
            self.config.notifier.push(title=f'{I18n.trans_zh_cn(command)}{command}', content=f"<{self.config_name}> RequestHumanTakeover")
            exit(1)
        except Exception as e:
            logger.exception(e)
            self.save_error_log()
            self.config.notifier.push(title=f'{I18n.trans_zh_cn(command)}{command}', content=f"<{self.config_name}> Exception occured")
            exit(1)

    def _server_restart_port(self) -> int:
        """获取当前 WebUI 端口，用于实例主动请求 server 级重启。"""
        return int(os.environ.get('OAS_WEBUI_PORT') or State.deploy_config.WebuiPort)

    def _request_server_restart_from_instance(self) -> None:
        """请求 server 执行 stop/start；失败也不兜底，随后由调用方直接退出进程。"""
        port = self._server_restart_port()
        url = f'http://127.0.0.1:{port}/{self.config_name}/restart_from_instance'
        logger.warning(f'Request server restart from instance: {url}')
        with urllib.request.urlopen(url, timeout=3) as response:
            response.read()

    def _exit_for_server_restart(self) -> None:
        """full_recovery 失败后主动触发 server 重启，并立即强制退出当前脚本进程。"""
        self._request_server_restart_from_instance()
        os._exit(1)

    def loop(self):
        """
        Main loop of scheduler.
        :return:
        """
        logger.set_file_logger(self.config_name)
        logger.info(f'Start scheduler loop: {self.config_name}')

        while 1:
            # Check update event from GUI
            # if self.stop_event is not None:
            #     if self.stop_event.is_set():
            #         logger.info("Update event detected")
            #         logger.info(f"Alas [{self.config_name}] exited.")
            #         break

            # Check game server maintenance
            # self.checker.wait_until_available()
            # if self.checker.is_recovered():
            #     # There is an accidental bug hard to reproduce
            #     # Sometimes, config won't be updated due to blocking
            #     # even though it has been changed
            #     # So update it once recovered
            #     del_cached_property(self, 'config')
            #     logger.info('Server or network is recovered. Restart game client')
            #     self.config.task_call('Restart')

            # 下一任务创建前：排空配置事件并在任务边界做 WARM/COLD 刷新
            self._config_checkpoint("before_task")

            # Get task
            task = self.get_next_task()
            # 更新 gui的任务
            # Init device and change server
            # _ = self.device
            # Skip first restart
            if self.is_first_task and task == 'Restart':
                logger.info('Skip task `Restart` at scheduler start')
                # 跳过动作本身也消耗首次标记，防止仅启用 Restart 时被永久跳过。
                self.is_first_task = False
                self.config.task_delay(task='Restart', success=True, server=True)
                self._config_checkpoint("skip_first_restart")
                continue
            _ = self.device  # trigger cached_property if first access
            if self._needs_recovery:
                logger.warning('Emulator recovery requested, running full_recovery before task')
                if not self.device.full_recovery():
                    # full_recovery 失败后不再兜底，由当前实例主动请求 server 级 stop/start 并退出。
                    logger.critical('full_recovery failed, request server-side restart from instance')
                    self._exit_for_server_restart()
                self._needs_recovery = False

            # Run
            logger.info(f'Scheduler: Start task `{task}`')
            self.device.stuck_record_clear()
            self.device.click_record_clear()
            logger.hr(task, level=0)
            success = self.run(inflection.camelize(task))
            logger.info(f'Scheduler: End task `{task}`')
            self.is_first_task = False

            if success:
                # 任务结束边界：WARM/COLD 刷新并上报 config_state
                self._config_checkpoint("task_end")
                continue
            elif self.config.script.error.handle_error or self._page_unknown_recoverable:
                # page unknown 失败自带恢复策略（重试/满 3 次 Restart），
                # 即使 handle_error=False 也放行继续调度，不停摆
                self._page_unknown_recoverable = False
                # 可恢复异常后边界：WARM/COLD 刷新并上报 config_state
                self._config_checkpoint("task_end")
                # self.checker.check_now()
                continue
            else:
                break

    def start_loop(self) -> None:
        """
        创建一个线程，运行loop
        :return:
        """
        if self.loop_thread is None:
            self.loop_thread = Thread(target=self.loop, name='Script_loop')
            self.loop_thread.start()


if __name__ == "__main__":
    ensure_ocr_server_started()
    script = Script("oas1")
    print(script.gui_task_list())
    print(script.config.gui_menu)
