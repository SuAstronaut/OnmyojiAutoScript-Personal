import importlib
from datetime import datetime, timedelta
import os
import threading
import json
import time
from pathlib import Path

from module.exception import (
    EmulatorNotRunningError,
    GameBugError,
    GameNotRunningError,
    GamePageUnknownError,
    GameStuckError,
    GameTooManyClickError,
    RequestHumanTakeover,
    ScriptError,
    TaskEnd,
)
from module.logger import logger
from tasks.Component.SwitchAccount.switch_account import SwitchAccount
from tasks.MultiDailyAltAcc import DailyAltAccEx
from tasks.MultiDailyAltAcc.assets import MultiDailyAltAccAssets
from tasks.MultiDailyAltAcc.config import MultiDailyAltAcc, ExtendedAccountInfo
from tasks.MultiDailyAltAcc.progress import (
    ProgressStore, acc_key, phase_flags_of, phase_id_of,
)
from tasks.MultiDailyAltAcc.task_plan import TaskPlan, load_task_plan
from tasks.GameUi.game_ui import GameUi
from tasks.Utils.coop_summary import build_summary_content
from tasks.Utils.coop_records import cooperation_round_id, record_cooperation
from tasks.Utils.coop_store import CoopStore
from tasks.DailyAltAcc.config import MSGType
from tasks.DailyAltAcc.stat_log import StatEvent, StatLogMixin
from script import Script


class ScriptTask(StatLogMixin, GameUi, MultiDailyAltAccAssets):
    daily_conf: MultiDailyAltAcc = None
    _task_plan: TaskPlan | None = None
    _normal_plan_phase: str | None = None
    # 子任务进度存储，run() 中按配置实例创建
    _progress: ProgressStore = None
    # 协作跨轮次持久化；逐卡轮次快照与记录在同一文件原子保存。
    _coop_store = None
    _coop_round_id = None
    _legacy_unmigrated_coops = None
    # 仅供同一账号的紧邻子任务重试使用，新账号、新调度和异常都会作废。
    _retry_account_identity: tuple | None = None
    # 添加一个类级别的锁，用于同步关机操作
    _shutdown_lock = threading.Lock()

    # 设备级异常：必须穿透到 script.py 的 Restart / 人工接管恢复逻辑，
    # 任何一层被宽泛 except Exception 吞掉，游戏都会一直卡着、后续账号连环失败。
    # 与 DailyAltAcc.ScriptTask._DEVICE_LEVEL_ERRORS 保持同一清单，增删须两边同步。
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

    def _save_recovery_error_log(self):
        """归档只是辅助诊断，磁盘或权限错误不能覆盖正在上抛的设备异常。"""
        try:
            Script.save_error_log(self)
        except Exception as error:
            logger.warning("保存错误现场失败，继续设备恢复：%s", error)

    def run(self):
        # 恢复后重新调度必须重新登录，不跨运行复用上次的账号身份。
        self._retry_account_identity = None
        import os
        pid = os.getpid()
        config_name = self.config.config_name  # 获取配置名称，例如oas1, oas2等
        logger.info(f"Starting script task with PID {pid} for config {config_name}")
        # 开始执行任务时，在进度文件中标记当前进程
        self._mark_task_start(config_name, pid)
        
        try:
            # 本次运行的开始边界：后端据此把统计切分为独立会话（一次调度 = 一个会话）；
            # 放在 try 内首行，与 finally 中的 run_end 严格成对
            self.emit_stat(StatEvent.RUN_START)
            # 加载配置，获取returngift_enable状态
            self.daily_conf = self.config.multi_daily_alt_acc
            base_config = self.daily_conf.multi_daily_alt_acc_config
            # 关闭自动轮转时不依赖 task_plan.json（任务集与运行时间都由手动开关
            # 和通用调度器决定），因此不读取——plan 文件损坏也不影响该模式
            self._task_plan = None if self._task_rotation_disabled() else load_task_plan()
            self._normal_plan_phase = self._current_normal_plan_phase(base_config)
            # 单用途轮运行前过滤：回礼/同心轮屏蔽其他日常任务。
            # 屏蔽值随收尾落盘（与物化同哲学），且保证接续重试时
            # phase_flags 快照稳定。回礼轮例外放行 plan.returngift 控制的
            # 勾协/神秘商店翻找。
            self._apply_single_purpose_filter(base_config)
            base_config = self.daily_conf.multi_daily_alt_acc_config
            returngift_enable = base_config.total_returngift_enable
            # 更新进度文件中的returngift_enable状态
            self._update_task_returngift_enable(config_name, returngift_enable)

            # 建立子任务进度：开关快照与上次一致则接续（失败重调度/崩溃重启），
            # 不一致说明上轮已成功并安排了新阶段，旧进度作废重建。
            self._progress = ProgressStore(config_name)
            self._progress.ensure_phase(
                phase_flags_of(base_config, self._normal_plan_phase), phase_id_of(self.start_time)
            )
            self._coop_round_id = cooperation_round_id('MultiDailyAltAcc', self._progress)
            self._coop_store = CoopStore(config_name, base_dir=self._progress.path.parent)
            self._migrate_legacy_coops()

            sup_account_list = self._get_sorted_accounts()

            # 记录本轮是否有账号因异常走了失败调度（原结构 except 里调度后 break
            # 不上抛，会继续处理后续账号并落到尾部，必须用标志避免成功收尾覆盖失败调度）
            phase_failed = False
            for accountInfo in sup_account_list:

                # 复用只发生在这个账号的重试循环里，不能跳过新账号的首次切号。
                self._retry_account_identity = None
                max_retries = 3
                retry_count = 0

                while retry_count < max_retries:
                    try:
                        if self._process_single_account(accountInfo):
                            # 成功处理账号，跳出重试循环
                            break
                        else:
                            retry_count += 1
                            if retry_count < max_retries:
                                logger.info(f"Account {accountInfo.character} failed, retrying ({retry_count}/{max_retries})...")
                            else:
                                logger.error(f"Failed to process account {accountInfo.character} after {max_retries} attempts")
                                phase_failed = True
                    except self._DEVICE_LEVEL_ERRORS:
                        # 切号闪退与其他设备异常统一交给 Restart / 人工接管：
                        # 先安排三分钟退避、保留进度，再原样上抛，禁止处理下个账号。
                        self._retry_account_identity = None
                        self.next_run("MultiDailyAltAcc", success=False)
                        self._save_recovery_error_log()
                        raise
                    except Exception as e:
                        # 未知异常后账号状态不再可信，不留给后续调用复用。
                        self._retry_account_identity = None
                        logger.error(f"Error processing account {accountInfo.character}: {e}")
                        self.config.notifier.push(
                            content=f"{accountInfo.character}-{accountInfo.svr} 任务执行错误\nError: {e}",
                            title="ERROR"
                        )
                        # 非设备级账号异常保留进度，留给下一轮接续
                        phase_failed = True
                        Script.save_error_log(self)
                        break
            if phase_failed:
                # 有账号失败：保留进度文件，3 分钟后重调度接续未完成部分
                self.next_run("MultiDailyAltAcc", success=False)
            else:
                # 整轮真正完成：先发送最终协作汇总（此时 coop 尚未 clear）。
                # 放在 next_run(success=True) 之前，避免「phase_flags 已改、coop 未发」窗口；
                # 通知失败只记日志，不影响后续收尾（见 _notify_daily_completion）。
                self._notify_daily_completion()
                # 检查是否需要关机：仅闲时(00:00-08:00)触发，避免整轮白天完成时误关机
                if (self.daily_conf.multi_daily_alt_acc_config.shutdown_after_finish
                        and self.daily_conf.multi_daily_alt_acc_config.total_alliedteam_battle_enable
                        and datetime.now().hour < 8):
                    self._coordinated_shutdown_system(config_name)
                # 先安排下一阶段（_schedule_* 改写开关并落盘），最后才删进度文件：
                # 若反过来先删，进程在开关落盘前被杀会导致「开关没变 + 进度没了」，
                # 下次启动重建进度把刚完成的阶段整个重跑（同心战斗重打 13 场）。
                # 反向窗口（开关已改、clear 前崩溃）由快照不一致自动重建兜底，无害。
                self.next_run("MultiDailyAltAcc", success=True)
                if self._progress is not None:
                    self._progress.clear()
        finally:
            # 无论成功、失败还是外层恢复接管，都清除本轮临时登录记录。
            self._retry_account_identity = None
            try:
                # 本次运行的结束边界：正常结束与异常上抛均会发出，供后端闭合运行段
                self.emit_stat(StatEvent.RUN_END)
            except Exception:
                # 统计埋点失败（如日志 IO 故障）不得阻断任务收尾与完成标记（审查m5）
                logger.exception("emit run_end stat failed")
            # 无论任务是否成功完成，都要标记为完成
            self._mark_task_completed(config_name)
        
        raise TaskEnd("MultiDailyAltAcc")

    def _mark_task_start(self, config_name, pid):
        """
        标记进程开始执行任务，基于配置名称而非PID
        """
        progress_file = Path('./config/tasks_config/daily_progress.json')
        progress_file.parent.mkdir(parents=True, exist_ok=True)
        
        with self._shutdown_lock:
            # 读取现有进度信息
            progress_data = {}
            if progress_file.exists():
                try:
                    with open(progress_file, 'r', encoding='utf-8') as f:
                        progress_data = json.load(f)
                except (json.JSONDecodeError, FileNotFoundError):
                    progress_data = {}
            
            # 使用配置名称标记当前进程为运行中，同时记录PID以便追踪
            progress_data[f'config_{config_name}'] = {
                'status': 'running',
                'start_time': datetime.now().isoformat(),
                'completed': False,
                'pid': pid,
                'returngift_enable': False  # 初始为False，后续更新
            }
            
            # 保存更新后的进度
            with open(progress_file, 'w', encoding='utf-8') as f:
                json.dump(progress_data, f, ensure_ascii=False, indent=2)

    def _update_task_returngift_enable(self, config_name, returngift_enable):
        """更新进度文件中的returngift_enable状态"""
        progress_file = Path('./config/tasks_config/daily_progress.json')
        
        with self._shutdown_lock:
            if not progress_file.exists():
                return
            
            try:
                with open(progress_file, 'r', encoding='utf-8') as f:
                    progress_data = json.load(f)
            except (json.JSONDecodeError, FileNotFoundError):
                return
            
            key = f'config_{config_name}'
            if key in progress_data:
                progress_data[key]['returngift_enable'] = returngift_enable
                with open(progress_file, 'w', encoding='utf-8') as f:
                    json.dump(progress_data, f, ensure_ascii=False, indent=2)
    
    def _mark_task_completed(self, config_name):
        """
        标记进程任务已完成，基于配置名称而非PID
        """
        progress_file = Path('./config/tasks_config/daily_progress.json')
        
        with self._shutdown_lock:
            # 读取现有进度信息
            progress_data = {}
            if progress_file.exists():
                try:
                    with open(progress_file, 'r', encoding='utf-8') as f:
                        progress_data = json.load(f)
                except (json.JSONDecodeError, FileNotFoundError):
                    progress_data = {}
                    
            # 标记当前配置为已完成
            if f'config_{config_name}' in progress_data:
                progress_data[f'config_{config_name}'].update({
                    'status': 'completed',
                    'completed': True,
                    'completed_time': datetime.now().isoformat()
                })
            
            # 保存更新后的进度
            with open(progress_file, 'w', encoding='utf-8') as f:
                json.dump(progress_data, f, ensure_ascii=False, indent=2)

    def _coordinated_shutdown_system(self,config_name):
        """
        协调多个进程的关机操作
        使用文件标记来跟踪完成的进程数
        """
        import os
        import time
        
        pid = os.getpid()
        progress_file = Path('./config/tasks_config/daily_progress.json')
        
        # 标记当前进程已完成
        self._mark_task_completed(config_name)
        
        # 等待一小段时间，让其他进程也有机会更新状态
        time.sleep(5)
        
        # 检查是否所有进程都完成了
        with self._shutdown_lock:
            # 重新读取进度信息
            if progress_file.exists():
                try:
                    with open(progress_file, 'r', encoding='utf-8') as f:
                        progress_data = json.load(f)
                except (json.JSONDecodeError, FileNotFoundError):
                    progress_data = {}
                    
                # 检查是否所有标记的进程都完成了
                all_completed = all(
                    v.get('completed', False) for v in progress_data.values()
                )
                
                if all_completed and len(progress_data) > 0:
                    logger.info(f"All {len(progress_data)} processes completed, executing shutdown")
                    self._execute_shutdown()
                    
                    # 清理进度文件
                    try:
                        progress_file.unlink()
                    except:
                        pass
                else:
                    remaining = sum(1 for v in progress_data.values() if not v.get('completed', False))
                    logger.info(f"Not all processes completed yet, {remaining} remaining")
    
    def _execute_shutdown(self):
        """实际执行系统关机操作"""
        import platform
        import subprocess
        
        system = platform.system()
        try:
            if system == "Windows":
                logger.info("正在关闭系统...")
                # Windows 关机命令，/s 表示关机，/t 30 表示30秒后关机
                subprocess.run(["shutdown", "/s", "/t", "30"], check=True)
                self.config.notifier.push(
                    content="系统将在30秒后关机，请及时保存工作",
                    title="系统关机提醒"
                )
            elif system == "Linux" or system == "Darwin":  # Darwin 是 macOS
                logger.info("正在关闭系统...")
                # Linux/macOS 关机命令
                subprocess.run(["sudo", "shutdown", "-h", "now"], check=True)
                self.config.notifier.push(
                    content="系统即将关机",
                    title="系统关机提醒"
                )
            else:
                logger.warning(f"不支持的操作系统: {system}，无法执行关机操作")
        except subprocess.CalledProcessError as e:
            logger.error(f"关机命令执行失败: {e}")
            self.config.notifier.push(
                content=f"关机失败: {e}",
                title="关机错误"
            )
        except Exception as e:
            logger.error(f"执行关机时发生错误: {e}")

    def _shutdown_system(self):
        """执行系统关机操作（旧版本，保持向后兼容）"""
        self._execute_shutdown()

    def _get_sorted_accounts(self):
        """获取按最后完成时间排序的账号列表，先剔除已完成的账号，再按账号分组排序"""
        if not self.daily_conf.sup_account_list:
            return []

        from collections import defaultdict
        from datetime import datetime

        # 第一步：剔除本阶段已完成的账号（依据持久化进度，而非登录时间推断）
        filtered_accounts = []
        for account_info in self.daily_conf.sup_account_list:
            # 配置表需要保留编辑中的空行，运行队列必须在读进度和排序前过滤它们。
            if account_info is None or not account_info.is_valid():
                logger.warning("跳过账号、角色名或区服名为空的配置")
                continue
            if not self._should_process_account(account_info):
                logger.info(f"Filtering out account {account_info.character} (already completed)")
                continue
            filtered_accounts.append(account_info)
        
        if not filtered_accounts:
            logger.info("No accounts need to be processed after filtering")
            return []
        
        # 第二步：按账号（account）分组，使同一邮箱下的角色连续排列
        account_groups = defaultdict(list)
        for account_info in filtered_accounts:
            account_groups[account_info.account].append(account_info)
        
        # 计算每个账号的最后完成时间的最大值（即最晚完成的那个），用于排序整个账号组
        account_times = {}
        for account, account_list in account_groups.items():
            # 计算该账号下所有角色的最后完成时间的最大值（最晚完成的那个）
            latest_completion_time = max([acc.last_complete_time for acc in account_list])
            account_times[account] = latest_completion_time
        
        # 按账号的最晚完成时间排序（从大到小，即最晚完成的账号在前）
        sorted_accounts_by_time = sorted(
            account_groups.keys(),
            key=lambda acc: account_times[acc],
            reverse=True  # 从大到小排序
        )
        
        # 按账号排序后，对每个账号内的角色也进行排序
        result = []
        for account in sorted_accounts_by_time:
            # 对同一账号内的角色按最后完成时间排序（最新完成的在前）
            sorted_account_chars = sorted(
                account_groups[account],
                key=lambda x: x.last_complete_time,
                reverse=True
            )
            result.extend(sorted_account_chars)
        
        # 打印排序后的结果
        logger.info("_get_sorted_accounts result: account character last_complete_time")
        for account_info in result:
            logger.info(f"{account_info.account} {account_info.character} {account_info.last_complete_time}")
        
        return result

    def _progress_key_of(self, account_info) -> str:
        """账号在进度文件中的键。"""
        return acc_key(account_info.account, account_info.character, account_info.svr)

    def _should_process_account(self, account_info):
        """判断是否应该处理该账号：完全依据持久化进度，不再比较登录时间。"""
        if self._progress is None:
            return True
        try:
            key = self._progress_key_of(account_info)
            done = self._progress.is_account_done(key)
        except Exception:
            logger.exception('读取账号进度失败，按未完成处理')
            return True
        if done:
            logger.info(f"{account_info.character} 本阶段已完成，跳过")
        return not done

    def _process_single_account(self, account_info):
        """处理单个账号的逻辑"""
        # 旧身份仅作为本次候选，配置校验、截图或识别报错时不能留下可复用记录。
        retry_identity = self._retry_account_identity
        self._retry_account_identity = None
        # 防止直接调用绕过队列校验，在错误角色上执行默认启用的日常任务。
        if account_info is None or not account_info.is_valid():
            logger.error("账号、角色名、区服名均必填，跳过存在空项的配置")
            return False
        # 创建配置对象
        config = self._create_account_config(account_info)
        # 只执行账号已启用的日常子任务，无任务时直接跳过。
        enabled_tasks = self._enabled_task_keys(config)
        if not enabled_tasks:
            logger.info(f"Skipping account {account_info.character} - No tasks enabled")
            return True
         
        logger.info("Start processing %s-%s", account_info.character, account_info.svr) 
        
        # 仅当四项身份都相同且新截图仍在庭院时，复用紧邻重试前已经成功的登录。
        identity = (account_info.account, account_info.character,
                    account_info.svr, account_info.apple_or_android)
        reuse_login = retry_identity == identity
        if reuse_login:
            self.screenshot()
            reuse_login = self.appear(self.I_CHECK_MAIN)
        if not reuse_login:
            if not self._switch_to_account(account_info):
                return False
        else:
            logger.info("复用当前角色登录，继续未完成子任务：%s-%s",
                        account_info.character, account_info.svr)

        self.emit_stat(
            StatEvent.ACC_START,
            acc=account_info.account,
            char=account_info.character,
            svr=account_info.svr,
            tasks=enabled_tasks,
        )

        # 异常后的登录记录必须作废；正常返回 False 的待重试子任务才可保留。
        self._retry_account_identity = identity
        try:
            return self._execute_daily_tasks(config, account_info)
        except Exception:
            self._retry_account_identity = None
            raise

    def _create_account_config(self, account_info):
        """创建针对特定账号的配置"""
        config = ExtendedAccountInfo()

        # 全局配置
        base_config = self.daily_conf.multi_daily_alt_acc_config

        # 运行时只看 total AND account：plan 的阶段勾选已在排程时刻物化进 total_*
        # 落盘，运行时不再过滤。用户在轮次间隙手动开 total_*（如捐勾）就会带着
        # 跑一轮，下一次排程物化重新接管——与试炼战斗等一次性任务同款行为。
        # 普通轮的 plan 键不再需要 enabled() 闭包。
        # 例外：同心战斗/回礼是轮次身份开关（total 决定 next_run 分流到哪个
        # 阶段），不能物化——它们的 plan 勾选（single_purpose 段）只能在运行时
        # AND：关了就整轮空跑（所有账号 skip，轮次正常完成、照常排下一阶段）。
        # 关闭自动轮转后没有轮转可言，回礼/同心战斗降级为普通任务，plan 不参与。
        if self._task_rotation_disabled():
            config.alliedteam_battle_enable = (base_config.total_alliedteam_battle_enable
                                               and account_info.alliedteam_battle_enable)
            config.returngift_enable = (base_config.total_returngift_enable
                                        and account_info.returngift_enable)
        else:
            plan = self._get_task_plan()
            single_purpose_on = lambda key: plan.enabled("single_purpose", key)
            config.alliedteam_battle_enable = (base_config.total_alliedteam_battle_enable
                                               and single_purpose_on("alliedteam_battle")
                                               and account_info.alliedteam_battle_enable)
            config.returngift_enable = (base_config.total_returngift_enable
                                        and single_purpose_on("returngift")
                                        and account_info.returngift_enable)

        config.alliedteam_ap_enable = base_config.total_alliedteam_ap_enable and account_info.alliedteam_ap_enable
        config.mail_enable = base_config.total_mail_enable and account_info.mail_enable
        config.donatejade_enable = base_config.total_donatejade_enable and account_info.donatejade_enable
        config.courtyard_enable = base_config.total_courtyard_enable and account_info.courtyard_enable
        config.cooperation_enable = base_config.total_cooperation_enable and account_info.cooperation_enable
        config.weekaward_enable = base_config.total_weekaward_enable and account_info.weekaward_enable
        config.mysteryshop_enable = base_config.total_mysteryshop_enable and account_info.mysteryshop_enable
        config.kekkaiActivation_enable = base_config.total_kekkaiActivation_enable and account_info.kekkaiActivation_enable
        config.KekkaiUtilize_enable = base_config.total_KekkaiUtilize_enable and account_info.KekkaiUtilize_enable
        config.tree_planting_enable = min(base_config.total_tree_planting_enable, account_info.tree_planting_enable)
        config.trialbattle_enable = base_config.total_trialbattle_enable and account_info.trialbattle_enable
        config.summon_up_enable = base_config.total_summon_up_enable and account_info.summon_up_enable
        # 发布SR碎片：全局 AND 每个小号配置
        config.publish_sr_enable = base_config.total_publish_sr_enable and account_info.publish_sr_enable
        # 账号特定配置
        config.isflower = account_info.isflower
        config.alliedteam_limit_count = account_info.alliedteam_limit_count
        config.alliedteam_invite_count = account_info.alliedteam_invite_count

        return config

    def _task_rotation_disabled(self) -> bool:
        """「关闭任务自动轮转」开关：开启后不做时间轮转（字段说明见 config.py）。

        关闭时全流程走原有的早/晚轮 + 回礼轮 + 同心战斗轮轮转。
        读不到配置（如测试替身没有该字段）时按未开启处理，保持既有行为。
        """
        config = getattr(self.daily_conf, 'multi_daily_alt_acc_config', None)
        return bool(getattr(config, 'disable_task_rotation', False))

    def _get_task_plan(self) -> TaskPlan:
        plan = getattr(self, "_task_plan", None)
        if plan is None:
            plan = load_task_plan()
            self._task_plan = plan
        return plan

    def _current_normal_plan_phase(self, base_config) -> str | None:
        """仅普通轮进入 plan 语义（排程物化时用）；回礼/同心轮返回 None。

        phase 现只喂 phase_flags_of 做进度重建分组，不再参与运行时任务过滤
        （过滤已在排程物化时完成）。"""
        if self._task_rotation_disabled():
            # 关闭自动轮转后没有阶段概念，进度分组只靠开关快照
            return None
        if base_config.total_returngift_enable or base_config.total_alliedteam_battle_enable:
            return None
        if 5 <= self.start_time.hour < 18:
            return "morning"
        if 18 <= self.start_time.hour <= 23:
            return "afternoon"
        return None

    # 单用途轮的保留任务：回礼轮放行回礼本身 + plan.returngift 勾选的勾协/商店；
    # 同心战斗轮放行同心战斗（AP 属于普通轮任务，单用途轮不开）。
    _RETURNGIFT_ALLOW = (
        ("total_returngift_enable", None),
        # 勾协/神秘商店均为排程决策类：晚轮排回礼轮时已按 plan.returngift
        # 勾选（商店另带星期门控）写好 total_*，运行前过滤照单执行，
        # 不做二次判定——这里放行的是"排程已决策为开"的值
        ("total_cooperation_enable", True),
        ("total_mysteryshop_enable", True),
    )
    _ALLIEDTEAM_ALLOW = ("total_alliedteam_battle_enable",)

    # 可屏蔽的全部任务开关（种树是 0/1/2 三值，屏蔽值用 0 而非 False）
    _MASKABLE_TOTAL_KEYS = (
        ("total_alliedteam_battle_enable", False),
        ("total_alliedteam_ap_enable", False),
        ("total_mail_enable", False),
        ("total_donatejade_enable", False),
        ("total_courtyard_enable", False),
        ("total_cooperation_enable", False),
        ("total_returngift_enable", False),
        ("total_weekaward_enable", False),
        ("total_mysteryshop_enable", False),
        ("total_kekkaiActivation_enable", False),
        ("total_KekkaiUtilize_enable", False),
        ("total_tree_planting_enable", 0),
        ("total_trialbattle_enable", False),
        ("total_summon_up_enable", False),
        ("total_publish_sr_enable", False),
    )

    def _apply_single_purpose_filter(self, base_config) -> None:
        """单用途轮（回礼/同心战斗）过滤其他日常开关。

        用户在轮次间隙手动勾选的任务（如捐勾）不会泄漏进单用途轮；屏蔽值随
        daily_conf 在收尾 save_config() 落盘（与排程物化同哲学：磁盘=本轮实际
        执行内容）。手动勾选本就是一次性行为，被单用途轮屏蔽即消费完毕。
        回礼轮的勾协/神秘商店是排程决策类：晚轮（_schedule_evening）已按
        plan.returngift 勾选（商店另带星期门控）写好 total_*，过滤照单执行。
        关闭自动轮转后不存在单用途轮：全部开关都是普通任务，不做屏蔽。
        """
        if self._task_rotation_disabled():
            return
        if base_config.total_returngift_enable:
            # 勾协/商店放行值固定 True：total 本身就是排程决策结果
            allowed = {total for total, plan_key in self._RETURNGIFT_ALLOW
                       if plan_key is None or plan_key is True}
            purpose = "returngift"
        elif base_config.total_alliedteam_battle_enable:
            allowed = set(self._ALLIEDTEAM_ALLOW)
            purpose = "alliedteam"
        else:
            return  # 普通轮：任务内容已由排程物化决定，不需要过滤

        masked = [key for key, false_value in self._MASKABLE_TOTAL_KEYS
                  if key not in allowed and getattr(base_config, key, False)]
        if not masked:
            return
        for key, false_value in self._MASKABLE_TOTAL_KEYS:
            if key not in allowed and getattr(base_config, key, False):
                setattr(base_config, key, false_value)
        logger.info("[%s轮] 运行前过滤：屏蔽非本任务开关（含手动勾选）: %s",
                    purpose, ", ".join(masked))
        # daily_conf 与 config.model 同源，屏蔽值会随收尾 save_config() 落盘
        self.config.model.multi_daily_alt_acc = self.daily_conf

    # plan 键 → total_* 开关的映射：排程时物化 plan 阶段勾选的唯一事实源。
    # 排程落盘的开关就是下一轮的执行内容。周奖励/神秘商店两键需再 AND 星期
    # 列表（schedule.*_weekdays），由 _schedule_plan_phase 的 weekday 逻辑处理。
    _PLAN_TASK_TOTAL = (
        ("courtyard", "total_courtyard_enable"),
        ("mail", "total_mail_enable"),
        ("cooperation", "total_cooperation_enable"),
        ("donatejade", "total_donatejade_enable"),
        ("alliedteam_ap", "total_alliedteam_ap_enable"),
        ("kekkaiActivation", "total_kekkaiActivation_enable"),
        ("KekkaiUtilize", "total_KekkaiUtilize_enable"),
        ("weekaward", "total_weekaward_enable"),
        ("mysteryshop", "total_mysteryshop_enable"),
    )

    # 周奖励/神秘商店的星期门控：物化到这两键时需再判断排程参考日的星期
    # 是否命中 plan 的 schedule.*_weekdays。统一为执行日语义——列表=任务实际
    # 翻找的日子；早/晚轮参考日即执行日，回礼轮决策在前一晚判断次日。
    _WEEKDAY_GATED = (
        ("weekaward", "weekaward_weekdays"),
        ("mysteryshop", "mysteryshop_weekdays"),
    )

    def _schedule_plan_phase(self, phase: str, start_time: datetime) -> None:
        scheduled = self._get_task_plan().schedule_target(phase, start_time)
        logger.info(
            "MultiDaily %s target: %s + %s minutes = %s",
            phase,
            scheduled.base_time,
            scheduled.delay_minutes,
            scheduled.target,
        )
        self.set_next_run("MultiDailyAltAcc", target=scheduled.target, persist=False)
        # 物化：plan 对本阶段的勾选直接写进 total_* 开关落盘。排程没勾的任务
        # 下一轮真的不跑（total=False 短路），用户不再需要手动开 total——
        # plan 就是普通轮的执行清单。注意这必须发生在 set_next_run 之后，
        # 因为 task_delay 会先从磁盘重载模型再改写。
        self.daily_conf = self.config.model.multi_daily_alt_acc
        plan = self._get_task_plan()
        weekday_gated = dict(self._WEEKDAY_GATED)
        for plan_key, total_key in self._PLAN_TASK_TOTAL:
            value = plan.enabled(phase, plan_key)
            if value and plan_key in weekday_gated:
                # 周奖励/神秘商店：早晚轮勾选 AND 排程参考日星期命中才开
                value = start_time.weekday() in getattr(plan, weekday_gated[plan_key])
            setattr(
                self.daily_conf.multi_daily_alt_acc_config,
                total_key,
                value,
            )

    @staticmethod
    def _enabled_task_keys(config):
        """按实际开启状态返回本账号会执行的子任务键。"""
        task_flags = [
            ("courtyard", config.courtyard_enable),
            ("mail", config.mail_enable),
            ("cooperation", config.cooperation_enable),
            ("donatejade", config.donatejade_enable),
            ("returngift", config.returngift_enable),
            ("weekaward", config.weekaward_enable),
            ("mysteryshop", config.mysteryshop_enable),
            ("kekkaiActivation", config.kekkaiActivation_enable),
            ("KekkaiUtilize", config.KekkaiUtilize_enable),
            ("tree", config.tree_planting_enable > 0),
            ("trialbattle", config.trialbattle_enable),
            ("summon_up", config.summon_up_enable),
            ("publish_sr", config.publish_sr_enable),
            ("alliedteam", config.alliedteam_battle_enable or config.alliedteam_ap_enable),
        ]
        return [task for task, enabled in task_flags if enabled]

    def _emit_account_error(self, account_info, task, error):
        """记录账号级异常，保留异常类型和首行错误消息。"""
        self.emit_stat(
            StatEvent.ERROR,
            acc=account_info.account,
            char=account_info.character,
            svr=account_info.svr,
            task=task,
            etype=error.__class__.__name__,
            emsg=str(error).splitlines()[0] if str(error) else "",
        )

    def _emit_account_end(self, account_info, err_count: int):
        """记录账号运行结束，供后端计算账号总耗时。"""
        self.emit_stat(
            StatEvent.ACC_END,
            acc=account_info.account,
            char=account_info.character,
            svr=account_info.svr,
            err_count=err_count,
        )

    def _switch_to_account(self, account_info):
        """切换到指定账号"""
        # 完整切号开始后旧身份失效，只有调用方确认成功才重新记录。
        self._retry_account_identity = None
        # 在切换账号前，重置检测记录，避免影响后续账号
        self.device.stuck_record_clear()
        # 切号起点标记：账号耗时（含切号过程与失败重试）从此刻起算
        self.emit_stat(
            StatEvent.SWITCH_START,
            acc=account_info.account,
            char=account_info.character,
            svr=account_info.svr,
        )
        success = SwitchAccount(self.config, self.device, account_info).switchAccount()
        self.emit_stat(
            StatEvent.SWITCH,
            acc=account_info.account,
            char=account_info.character,
            svr=account_info.svr,
            ok=success,
        )
        if not success:
            logger.warning("Switch to %s-%s Failed", account_info.character, account_info.svr)
            self.config.notifier.push(
                content=f"Switch to {account_info.character}-{account_info.svr} Failed, account info: {account_info.account}",  
                title="未找到账号"
            )
        return success

    def _execute_daily_tasks(self, config, account_info):
        """执行日常任务；设备级异常必须穿透到 script.py 的恢复逻辑。"""
        # 执行已启用的日常子任务，由子实例记录各项进度。
        dff = self._create_task_instance(config, account_info)

        try:
            dff.run()
            # 正常返回虽不是当前主路径，仍按相同规则收尾，避免遗漏账号完成判断
            success = self._finalize_account_progress(account_info, config)
            self._emit_account_end(account_info, err_count=0 if success else 1)
            return success
        except TaskEnd as msg:
            success = self._handle_task_end(msg, account_info, config)
            self._emit_account_end(account_info, err_count=0 if success else 1)
            return success
        except self._DEVICE_LEVEL_ERRORS as e:
            # _run_with_stat 已判定为设备级异常；这里不得再被宽泛 Exception 吞成 False，
            # 否则 GameStuckError 到不了 script.py 的 task_call('Restart')。
            self._retry_account_identity = None
            self._emit_account_error(account_info, None, e)
            self._emit_account_end(account_info, err_count=1)
            # 子执行器同样保留原设备异常，诊断归档失败不阻断上层恢复。
            self._save_recovery_error_log()
            raise
        except Exception as e:
            # 通用异常虽然会转成 False 返回，但不能把它当成安全的普通子任务重试。
            self._retry_account_identity = None
            logger.error(f"Error in daily tasks for {account_info.character}: {e}")
            self._emit_account_error(account_info, None, e)
            self._emit_account_end(account_info, err_count=1)
            Script.save_error_log(self)
            return False
        finally:
            # 子任务可能吞异常后正常返回或抛 TaskEnd，统一在收尾检查本轮异常标记。
            if getattr(dff, "_subtask_error_occurred", False):
                self._retry_account_identity = None

    def CreatObjectFromModule(self, task_name: str, **kwargs):
        module_name = 'script_task'
        from pathlib import Path
        module_path = str(Path.cwd() / 'tasks' / task_name / (module_name + '.py'))

        spec = importlib.util.spec_from_file_location(module_name, module_path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)

        WQEX = type("WQEX", (module.ScriptTask,), {
            "get_config": DailyAltAccEx.get_config,
            # 屏蔽 DailyAltAcc 自身的调度：本任务就是它的多账号版本，
            # 每个小号跑完都改一次大号的下次运行时间属于污染。
            "set_next_run": DailyAltAccEx.shield_self(module.ScriptTask).set_next_run,
            # 屏蔽 DailyAltAcc 内部嵌套的挂卡/寄养实例的调度（它们是独立对象，
            # 上面这个 set_next_run 覆写拦不到，必须在创建这一步换成屏蔽子类）。
            "_create_nested_task": DailyAltAccEx.create_nested_task,
        })
        wq = WQEX(**kwargs)
        return wq
    def _create_task_instance(self, config, source_account_info):
        """创建任务实例，并注入统计日志的账号上下文与子任务进度上下文。"""
        dff = self.CreatObjectFromModule("DailyAltAcc", config=self.config, device=self.device)
        dff.daily_conf = self.daily_conf
        dff.account_info = config
        dff._stat_ctx = {
            "acc": source_account_info.account,
            "char": source_account_info.character,
            "svr": source_account_info.svr,
            "sys": "android" if source_account_info.apple_or_android else "ios",
        }
        # 多账号截图统一使用四项身份，供 Butler 准确关联。
        dff._help_screenshot_name_mode = 'full'
        # 识别时直接记录，避免后续子任务异常导致 TaskEnd 消息来不及交回父任务。
        dff._coop_store = self._coop_store
        dff._coop_round_id = self._coop_round_id
        dff._account_identity = (
            source_account_info.account, source_account_info.character,
            source_account_info.svr, source_account_info.apple_or_android,
        )
        # 注入进度上下文：子任务据此标记 done/failed/skipped 并接续同心战斗场次
        dff._progress = self._progress
        dff._progress_key = self._progress_key_of(source_account_info)
        dff._alliedteam_limit = config.alliedteam_limit_count
        return dff

    def _finalize_account_progress(self, account_info, account_config) -> bool:
        """仅当本轮所有启用子任务均已了结时，才标记账号完成。

        日常子任务的 done / failed / skipped 都算已了结。
        返回 False 保持账号待重试，接续时只补未完成部分。
        """
        if self._progress is not None:
            key = self._progress_key_of(account_info)
            enabled_tasks = self._enabled_task_keys(account_config)
            if self._progress.has_pending_tasks(key, enabled_tasks):
                logger.warning(f"{account_info.character} 仍有未完成子任务，保留进度并重试")
                return False
            # 日常全部了结即可收尾，不再依赖账号数据导出记录或文件。
            self._progress.mark_account_done(key)

        # last_complete_time 仅用于排序与展示，不再参与完成判定
        self.daily_conf.update_account_login_history(account_info)
        # 将修改后的 daily_conf 同步回主配置模型,确保保存时包含最新数据
        self.config.model.multi_daily_alt_acc = self.daily_conf
        self.save_config()
        return True

    def _handle_task_end(self, msg, account_info, account_config):
        """处理任务结束消息；网络错误先返回 False，其余按子任务进度决定账号完成。"""
        logger.info(f"TaskEnd received: {msg.args}")

        if msg.args and msg.args != []:
            logger.info(f"Message count: {len(msg.args)}")
            for item in msg.args[0]:
                logger.info(f"Processing item: {item}")
                if len(item) >= 2:
                    msg_type = item[0]  # MSGType
                    msg_content = item[1]  # Content

                    if self._process_message_type(msg_type, msg_content, account_info):
                        return False  # 如果是网络错误，需要重试

        return self._finalize_account_progress(account_info, account_config)

    def _process_message_type(self, msg_type, msg_content, account_info):
        """处理不同类型的消息"""
        should_retry = False
        
        match msg_type:
            case MSGType.cooperation:
                # 新识别已直接写入持久表；保留旧消息入口，并按同轮同槽幂等处理。
                self._persist_coop_event(msg_content, account_info)
            case MSGType.mshop:
                # 与协作同策：不再「发现一条立即推送」，落盘到本轮 ProgressStore，
                # 整轮真正完成时随协作汇总一起发送（_notify_daily_completion）。
                self._persist_mshop_event(msg_content, account_info)
            case MSGType.Utilize:
                logger.info("由于未找到寄养卡,已将所有账号的KekkaiUtilize_enable设置为False")
                self.daily_conf.multi_daily_alt_acc_config.total_KekkaiUtilize_enable = False
            case MSGType.neterror:
                # 网络错误后的会话状态不确定，重试前必须重新确认登录。
                self._retry_account_identity = None
                logger.info("网络错误,准备重试")
                should_retry = True
            case _:
                logger.info(f"未知消息类型: {msg_type}, 内容: {msg_content}")
                
        return should_retry

    def _persist_coop_event(self, event, account_info):
        """新事件走共用持久化入口，未建立该入口的旧调用继续保留临时记录。"""
        if not isinstance(event, dict):
            # 旧版纯字符串事件不再推送，仅记录日志，避免破坏兼容
            logger.info(f'协作事件（旧格式，跳过推送）: {event}')
            return
        if self._coop_store is not None and 'slot' in event:
            record_cooperation(
                event, self._coop_store,
                (account_info.account, account_info.character, account_info.svr,
                 account_info.apple_or_android),
                round_id=self._coop_round_id,
            )
            return
        record = {
            "account": str(getattr(account_info, "account", "") or ""),
            "character": str(getattr(account_info, "character", "") or ""),
            "svr": str(getattr(account_info, "svr", "") or ""),
            # 平台：True=安卓，False=iOS（来自账号配置，不从角色名猜测）
            "apple_or_android": bool(getattr(account_info, "apple_or_android", False)),
            "type": str(event.get("type", "") or ""),
            "real": bool(event.get("real", False)),
            "food_kind": event.get("food_kind"),
            "label": str(event.get("label", "") or ""),
        }
        found_at = str(event.get("found_at", "") or "").strip()
        if found_at:
            # 识别帧时刻随事件从 _coop_emit 带出来（见 tasks/DailyAltAcc/cooperation.py），
            # 这里只做透传，不自己算窗口。带上它之后，本文件里的协作才和别处的协作
            # 记录是同一种东西，不再被当成「缺少采集时间」排除出供需。
            # 缺了这个字段就**不写**，而不是补一个 now()：下游把没有采集时间的记录
            # 判为不可判定并排除供需，凭空补时刻等于把一条不知道属于哪个刷新窗口的
            # 协作算成可用供给。
            record["found_at"] = found_at
        for key in ("discoverer_monster", "friend_monster", "monster_text"):
            value = str(event.get(key, "") or "").strip()
            if value:
                record[key] = value
        if self._progress is not None:
            self._progress.append_coop(record)
            if self._coop_store is not None:
                # 旧模块可能在本轮启动后才交回无 slot 消息，也必须赶在汇总/clear 前迁移。
                self._migrate_legacy_coops()
        else:
            logger.info(f'协作事件（无进度存储，仅记录）: {record}')

    def _migrate_legacy_coops(self):
        """接续升级前的逐条进度，保留重复卡片；缺时间的旧记录只参与原推送。"""
        self._legacy_unmigrated_coops = []
        for index, record in enumerate(self._progress.load_coops()):
            event = dict(record, slot=f'legacy_{index}')
            saved = record_cooperation(
                event, self._coop_store,
                (record.get('account', ''), record.get('character', ''),
                 record.get('svr', ''), record.get('apple_or_android')),
                round_id=self._coop_round_id,
            )
            if saved is None:
                self._legacy_unmigrated_coops.append(record)

    def _coops_found_this_round(self):
        """完整轮次的逐卡快照，重启接续和清除进度都不会删除持久记录。"""
        if self._coop_store is not None and self._coop_round_id:
            return (self._coop_store.records_for_round(self._coop_round_id)
                    + list(self._legacy_unmigrated_coops or []))
        return self._progress.load_coops() if self._progress is not None else []

    def _persist_mshop_event(self, event, account_info):
        """把结构化神秘商店事件 + 当前账号信息落盘到当前配置的 ProgressStore。

        与 _persist_coop_event 同构。旧格式（纯字符串 content）也接：把整串塞进
        label，汇总时直接显示，保证跨版本不丢记录 —— 商店命中比协作稀有得多，
        宁可显示得糙一点也不能丢。
        """
        if isinstance(event, dict):
            label = str(event.get('label', '') or '')
            goods = str(event.get('goods', '') or '')
            coin = str(event.get('coin', '') or '')
            price = event.get('price')
        else:
            # 旧版纯字符串事件（形如「发现82500金币大蛇的逆鳞」）：整串当 label
            label = str(event or '').strip()
            goods = coin = ''
            price = None
        record = {
            "account": str(getattr(account_info, "account", "") or ""),
            "character": str(getattr(account_info, "character", "") or ""),
            "svr": str(getattr(account_info, "svr", "") or ""),
            "apple_or_android": bool(getattr(account_info, "apple_or_android", False)),
            "goods": goods,
            "coin": coin,
            "price": price,
            "label": label,
        }
        if self._progress is not None:
            self._progress.append_mshop(record)
        else:
            logger.info(f'神秘商店事件（无进度存储，仅记录）: {record}')

    @staticmethod
    def _build_notify_title(msg_content, fallback_title):
        clean_content = str(msg_content).strip()
        return clean_content if clean_content else fallback_title

    @staticmethod
    def _build_notify_content(account_info):
        device_type = "android" if account_info.apple_or_android else "ios"
        return "\n".join([
            f"角色：{account_info.character}",
            f"客户端：{device_type}",
            f"账号：{account_info.account}",
        ])

    def _notify_daily_completion(self):
        """整轮真正完成：发送一条汇总 PushPlus（每个配置每轮一条，最多一次）。

        首次进入完成分支：coop_notified 为 false/不存在 → 发送 → 发送成功后才写
        coop_notified=true 并 _save()，从而消除「push 成功后、clear 前崩溃导致重启
        后重复推送」的窗口；已标记（如崩溃后重启接续再次进入完成分支）→ 跳过推送，
        继续正常 next_run / clear。PushPlus 失败不写标记，仍不阻塞整轮收尾（best-effort）。

        汇总含协作与神秘商店两部分，各自跟随自己的总开关：
        total_cooperation_enable 关闭则不出协作段落，total_mysteryshop_enable
        关闭则不出商店段落。

        「无汇总可发」（协作关且商店无记录，如同心战斗轮/回礼轮）时，改为发送
        普通完成推送 _notify_plain_completion（内容列出本轮实际启用的项目），
        保证任意轮次完成都有且仅有一条完成通知；两种推送共用 coop_notified
        幂等标记，防止崩溃重启后重复推送。MultiDailyAltAcc 已从 script.py 的
        TASK_END_NOTIFY_LIST 移除，通用「任务提醒」不再参与本任务。
        读不到开关配置（如测试环境）时保持原发送行为，避免误吞完成通知。
        """
        if self._progress is None:
            return
        coop_on, mshop_on = True, True
        try:
            cfg = getattr(self.daily_conf, 'multi_daily_alt_acc_config', None)
            if cfg is not None:
                coop_on = bool(getattr(cfg, 'total_cooperation_enable', True))
                mshop_on = bool(getattr(cfg, 'total_mysteryshop_enable', True))
        except Exception:
            # 读取开关失败（如测试环境无 daily_conf）→ 保持原行为，不阻断完成通知
            pass
        # 本轮完成通知（汇总或普通）只发一次：已标记（如崩溃后重启接续再次进入
        # 完成分支）→ 跳过，继续正常 next_run / clear
        if self._progress.is_coop_notified():
            logger.info('本轮已完成完成通知，跳过重复推送')
            return
        coops = self._coops_found_this_round() if coop_on else []
        mshops = self._progress.load_mshops() if mshop_on else []
        # 协作关闭时不发空轮汇总，改发普通完成推送（含本轮执行项目）
        if not coop_on and not mshops:
            logger.info('寻找协作关闭且无神秘商店记录，改发普通完成通知')
            self._notify_plain_completion()
            return
        try:
            # title 自带完整前缀「config_name｜…」，并跳过 Notifier 的全局 config_name 拼接，
            # 保证显示为「小号1｜多账号日常完成」且不影响其他通知的「config_name 标题」格式。
            cfg = getattr(self.daily_conf, 'multi_daily_alt_acc_config', None)
            show_account = bool(getattr(cfg, 'coop_notify_show_account', False))
            show_system = bool(getattr(cfg, 'coop_notify_show_system', True))
            ok = self.config.notifier.push(
                content=self._build_summary_content(
                    coops, show_account=show_account, show_system=show_system,
                    mshops=mshops),
                title=f"{self.config.config_name}｜多账号日常完成",
                skip_config_prefix=True,
            )
        except Exception as e:
            logger.warning(f'汇总通知发送失败（不影响整轮结果）: {e}')
            return
        if ok:
            self._progress.mark_coop_notified()
        else:
            # best-effort：失败不标记已通知、不重试、不阻塞收尾（可能漏通知，可接受）
            logger.warning('汇总通知返回失败（不标记已通知，整轮仍视为成功）')

    # 普通完成推送：total_* 全局开关 → 中文名；普通轮 plan 键按早晚阶段物化
    _PLAIN_PUSH_TASKS = (
        ('total_alliedteam_battle_enable', '同心战斗', None),
        ('total_alliedteam_ap_enable', '同心体力', 'alliedteam_ap'),
        ('total_donatejade_enable', '捐勾', 'donatejade'),
        ('total_courtyard_enable', '庭院事务', 'courtyard'),
        ('total_mail_enable', '邮件', 'mail'),
        ('total_cooperation_enable', '协作', 'cooperation'),
        ('total_returngift_enable', '回礼', None),
        ('total_weekaward_enable', '每周奖励', None),
        ('total_mysteryshop_enable', '神秘商店', None),
        ('total_kekkaiActivation_enable', '挂卡', 'kekkaiActivation'),
        ('total_KekkaiUtilize_enable', '蹭卡', 'KekkaiUtilize'),
        ('total_tree_planting_enable', '种树', None),
        ('total_trialbattle_enable', '试炼战斗', None),
        ('total_summon_up_enable', 'UP召唤礼包', None),
        ('total_publish_sr_enable', '发布SR碎片', None),
    )

    def _build_plain_items(self) -> list[str]:
        """列出本轮实际启用的项目中文名。

        与 _create_account_config 的开关判定同源：total_* 决定做不做——plan 的
        阶段勾选已在排程时刻物化进 total_*，这里不需要（也不能）再过滤。
        种树是 0/1/2 三值开关，分别显示为买花/买花捐树。
        """
        try:
            cfg = self.daily_conf.multi_daily_alt_acc_config
        except Exception:
            return []
        items = []
        for flag_name, label, plan_key in self._PLAIN_PUSH_TASKS:
            value = getattr(cfg, flag_name, None)
            if flag_name == 'total_tree_planting_enable':
                # 种树：0 不运行 / 1 买花 / 2 买花捐树
                if value == 1:
                    items.append('买花')
                elif value == 2:
                    items.append('买花捐树')
                continue
            if not value:
                continue
            items.append(label)
        return items

    def _notify_plain_completion(self):
        """无协作/商店汇总时的普通完成推送：列出本轮实际执行的项目。

        与汇总推送共用 coop_notified 幂等标记与「{config_name}｜多账号日常完成」
        标题；推送失败不写标记、不阻塞整轮收尾（best-effort，与汇总路径一致）。
        项目列表读不出来时仍发推送，只是不含项目行——完成通知本身不能丢。
        """
        now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        lines = ["多账号日常完成", "", f"完成时间：{now_str}"]
        items = self._build_plain_items()
        if items:
            lines.append(f"本轮执行项目：{'、'.join(items)}")
        try:
            ok = self.config.notifier.push(
                content="\n".join(lines),
                title=f"{self.config.config_name}｜多账号日常完成",
                skip_config_prefix=True,
            )
        except Exception as e:
            logger.warning(f'普通完成通知发送失败（不影响整轮结果）: {e}')
            return
        if ok:
            self._progress.mark_coop_notified()
        else:
            # best-effort：失败不标记已通知、不重试、不阻塞收尾
            logger.warning('普通完成通知返回失败（不标记已通知，整轮仍视为成功）')

    @classmethod
    def _build_summary_content(cls, coops, completed_at=None, show_account=False,
                               show_system=True, mshops=None) -> str:
        """协作汇总正文。实现见 ``tasks/Utils/coop_summary.build_summary_content``
        ——各调用方共用同一份，确保推送正文格式一致。
        """
        return build_summary_content(
            coops, completed_at=completed_at, show_account=show_account,
            show_system=show_system, mshops=mshops)

    def save_config(self):
        """保存配置"""
        self.config.save()

    def next_run(self, task: str, finish: bool = False,
                 success: bool = None, server: bool = True, target: datetime = None) -> None:
        """设置下一次运行时间"""
        start_time = self.start_time  # 使用任务开始时间而不是当前时间
        if success:
            if self._task_rotation_disabled():
                # 关闭自动轮转：不做任何阶段排程与开关改写，下次运行交给通用调度器
                self._schedule_by_scheduler()
            elif self.daily_conf.multi_daily_alt_acc_config.total_returngift_enable:
                self._schedule_alliedteam_after_returngift()
            elif self.daily_conf.multi_daily_alt_acc_config.total_alliedteam_battle_enable:
                # 同心战斗模式：不分时段，完成后直接走凌晨后流程（6:05执行上午任务）
                self._schedule_after_midnight(start_time)
            elif 5 <= start_time.hour < 18:
                # 工作时间段：18:05执行
                self._schedule_normal_day(start_time)
            elif start_time.hour < 5:
                # 凌晨时段：6:05执行
                self._schedule_after_midnight(start_time)
            elif 18 <= start_time.hour <= 23:
                # 晚上时段：次日00:20执行
                self._schedule_evening(start_time)
            else:
                # 异常情况：第二天6:05执行
                self.set_next_run(task, target=start_time.replace(hour=6, minute=5) + timedelta(days=1))
        else:
            # 失败情况：3分钟后重试
            self.set_next_run(task, target=datetime.now() + timedelta(minutes=3))

    def _reset_one_shot_flags(self):
        """重置单次运行标志（只由用户手动开启，运行一次后自动关闭）"""
        self.daily_conf.multi_daily_alt_acc_config.total_tree_planting_enable = 0
        self.daily_conf.multi_daily_alt_acc_config.total_trialbattle_enable = False
        self.daily_conf.multi_daily_alt_acc_config.total_summon_up_enable = False
        self.daily_conf.multi_daily_alt_acc_config.total_publish_sr_enable = False

    def _schedule_by_scheduler(self):
        """关闭自动轮转模式的收尾：下次运行完全交给通用调度器。

        不传 target，next_run = start_time + Scheduler.success_interval（默认 1 天），
        运行节奏由用户在调度器面板自行约束；失败仍走 next_run(success=False) 的
        3 分钟重试与进度接续，不经本方法。四个 _schedule_* 阶段方法一个都不调用，
        因此 total_* 一个都不改写——它们只由用户手动维护。

        一次性开关（种树/试炼/UP召唤/发布SR）仍在这里清零：它们本就只跑一轮，
        与时间轮转无关，保留「只由用户手动开启、运行一次后自动关闭」的既有语义。
        """
        # task_delay 会先 reload；先写内存中的 next_run，最后与开关一起原子落盘
        self.set_next_run("MultiDailyAltAcc", success=True, persist=False)
        self.daily_conf = self.config.model.multi_daily_alt_acc
        self._reset_one_shot_flags()
        self.config.model.multi_daily_alt_acc = self.daily_conf
        self.save_config()

    def _schedule_normal_day(self, start_time: datetime):
        """安排白天的运行时间"""
        # task_delay 会先 reload；必须先保存 next_run，再基于重载后的模型修改阶段开关。
        self._schedule_plan_phase("afternoon", start_time)
        self.daily_conf = self.config.model.multi_daily_alt_acc
        # 每周奖励/神秘商店已改由早晨 6:05 那趟领取（_schedule_after_midnight 开启），
        # 这里显式关闭，避免 18:05 晚间这趟重复领取
        self.daily_conf.multi_daily_alt_acc_config.total_weekaward_enable = False
        self.daily_conf.multi_daily_alt_acc_config.total_mysteryshop_enable = False

        self.daily_conf.multi_daily_alt_acc_config.total_alliedteam_battle_enable = False
        # normal 阶段各任务开关由 _schedule_plan_phase 物化 plan 的 afternoon 勾选
        self.daily_conf.multi_daily_alt_acc_config.total_returngift_enable = False
        self._reset_one_shot_flags()
        self.config.model.multi_daily_alt_acc = self.daily_conf
        self.save_config()

    def _schedule_after_midnight(self, start_time: datetime):
        """安排凌晨的运行时间

        回礼阶段的分流由 next_run() 统一前置判断（优先级最高），进入本方法时
        total_returngift_enable 必然为 False，因此这里不再重复判断回礼。
        """
        # task_delay 会先 reload；必须先保存 next_run，再基于重载后的模型修改阶段开关。
        self._schedule_plan_phase("morning", start_time)
        self.daily_conf = self.config.model.multi_daily_alt_acc

        # 如果开启了同心战斗，则调整设置
        if self.daily_conf.multi_daily_alt_acc_config.total_alliedteam_battle_enable:
            self.daily_conf.multi_daily_alt_acc_config.total_alliedteam_battle_enable = False
            # 早轮 plan 键开关（含周奖励/神秘商店的早晚轮+星期门控）已由
            # _schedule_plan_phase 物化 plan 的 morning 勾选；同心战斗转 AP 走
            # plan 的 alliedteam_ap 表达（默认早轮开）。
            self._reset_one_shot_flags()
        # 普通凌晨与同心战斗分支都必须提交内存中的 next_run；两者共用一次原子保存。
        self.config.model.multi_daily_alt_acc = self.daily_conf
        self.save_config()

    def _schedule_alliedteam_after_returngift(self):
        self.set_next_run("MultiDailyAltAcc", target=datetime.now() + timedelta(minutes=3), persist=False)
        self.daily_conf = self.config.model.multi_daily_alt_acc
        self.daily_conf.multi_daily_alt_acc_config.total_alliedteam_battle_enable = True
        self.daily_conf.multi_daily_alt_acc_config.total_alliedteam_ap_enable = False
        self.daily_conf.multi_daily_alt_acc_config.total_returngift_enable = False
        self.daily_conf.multi_daily_alt_acc_config.total_courtyard_enable = False
        self.daily_conf.multi_daily_alt_acc_config.total_mail_enable = False
        self.daily_conf.multi_daily_alt_acc_config.total_cooperation_enable = False
        # 单用途轮（phase=None 不走 plan 过滤）必须显式关掉捐勾/挂卡/蹭卡，
        # 否则承接上一普通轮的 True 会在回礼轮误跑。
        self.daily_conf.multi_daily_alt_acc_config.total_donatejade_enable = False
        self.daily_conf.multi_daily_alt_acc_config.total_kekkaiActivation_enable = False
        self.daily_conf.multi_daily_alt_acc_config.total_KekkaiUtilize_enable = False
        self._reset_one_shot_flags()
        self.config.model.multi_daily_alt_acc = self.daily_conf
        self.save_config()

    def _schedule_evening(self, start_time: datetime):
        """安排晚上的运行时间（排次日 00:20 回礼轮）"""
        self.set_next_run("MultiDailyAltAcc", target=start_time.replace(hour=0, minute=20) + timedelta(days=1),
                          persist=False)
        self.daily_conf = self.config.model.multi_daily_alt_acc
        self.daily_conf.multi_daily_alt_acc_config.total_weekaward_enable = False
        # 回礼轮附加任务（勾协/神秘商店）的决策时刻都提前一晚：晚轮完成排程时
        # 按 plan.returngift 勾选写好开关，回礼轮运行前过滤照单执行。
        # 神秘商店星期为执行日语义：列表=实际翻找日，这里判断次日（回礼轮执行日）
        # 是否命中 plan 的 schedule.mysteryshop_weekdays。
        # 周奖励仍由早轮物化领取。
        plan = self._get_task_plan()
        mysteryshop = (plan.enabled("returngift", "mysteryshop")
                       and (start_time.weekday() + 1) % 7 in plan.mysteryshop_weekdays)
        self.daily_conf.multi_daily_alt_acc_config.total_mysteryshop_enable = mysteryshop
        self.daily_conf.multi_daily_alt_acc_config.total_alliedteam_battle_enable = False
        self.daily_conf.multi_daily_alt_acc_config.total_alliedteam_ap_enable = False
        self.daily_conf.multi_daily_alt_acc_config.total_returngift_enable = True
        self.daily_conf.multi_daily_alt_acc_config.total_courtyard_enable = False
        self.daily_conf.multi_daily_alt_acc_config.total_mail_enable = False
        self.daily_conf.multi_daily_alt_acc_config.total_cooperation_enable = \
            plan.enabled("returngift", "cooperation")
        # 同 _schedule_alliedteam_after_returngift：单用途轮显式关闭，防承接误跑。
        self.daily_conf.multi_daily_alt_acc_config.total_donatejade_enable = False
        self.daily_conf.multi_daily_alt_acc_config.total_kekkaiActivation_enable = False
        self.daily_conf.multi_daily_alt_acc_config.total_KekkaiUtilize_enable = False
        self._reset_one_shot_flags()
        self.config.model.multi_daily_alt_acc = self.daily_conf
        self.save_config()
        
if __name__ == '__main__':
    from module.config.config import Config
    from module.device.device import Device
    # from mypatch import SimplePatch

    # SimplePatch.patch()

    c = Config('oas1')
    d = Device(c)
    t = ScriptTask(c, d)
    """ t.daily_conf = t.config.daily 
    sup_account_list = t._get_sorted_accounts() """
    t.run()
