# This Python file uses the following encoding: utf-8
# @author runhey
# github https://github.com/runhey
import time

from module.logger import logger
from tasks.Utils.coop_store import CoopStoreWriteError
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

from tasks.GameUi.page import page_main
from tasks.GameUi.assets import GameUiAssets
from tasks.DailyAltAcc.config import MSGType
from tasks.WantedQuests.assets import WantedQuestsAssets
from tasks.RichMan.guild import Guild
from tasks.RichMan.config import GuildStore
from tasks.WeeklyTrifles.script_task import ScriptTask as WeeklyTrifles
from tasks.KekkaiActivation.script_task import ScriptTask as KekkaiActivation
from tasks.KekkaiUtilize.script_task import ScriptTask as KekkaiUtilize
from tasks.Utils.config_enum import ShikigamiClass
from tasks.KekkaiActivation.config import CardType
from tasks.KekkaiUtilize.config import UtilizeRule, SelectFriendList
from tasks.DailyAltAcc.stat_log import StatEvent, StatLogMixin
from tasks.MultiDailyAltAcc.progress import STATUS_DONE, STATUS_FAILED

# Sub-tasks
from tasks.DailyAltAcc.courtyard import Courtyard
from tasks.DailyAltAcc.mail import Mail
from tasks.DailyAltAcc.donatejade import Donatejade
from tasks.DailyAltAcc.cooperation import Cooperation
from tasks.DailyAltAcc.returngift import Returngift
from tasks.DailyAltAcc.alliedteam import Alliedteam
from tasks.DailyAltAcc.mshop import Mshop
from tasks.DailyAltAcc.tree import Tree
from tasks.DailyAltAcc.summon_up import SummonUp
from tasks.DailyAltAcc.trialbattle import Trialbattle
from tasks.DailyAltAcc.publish_sr import PublishSr


class ScriptTask(StatLogMixin, Courtyard, Mail, Donatejade, Cooperation,
                 Returngift, Alliedteam, Mshop, Tree,
                 SummonUp, Trialbattle, PublishSr,
                 Guild, WeeklyTrifles):
    account_info: dict = None
    # 子任务进度存储，由 MultiDailyAltAcc 注入；单任务直跑时为 None（不做持久化）
    _progress = None
    # 当前账号在进度文件中的键
    _progress_key: str = None
    # 即使子任务异常被内部捕获，也要通知多账号父任务本轮登录不可复用。
    _subtask_error_occurred: bool = False
    # 当前账号的同心战斗上限，仅用于异常邮件里报告「已打 N/M 场」
    _alliedteam_limit: int = 0
    # 回礼与子任务段起点之间的最小间隔秒数：沿用原 sleep(10-delay_time) 的语义。
    # 回礼（进寮页面祈愿）需要距本账号任务开始至少这么久，前置任务实际耗时
    # 不足时在回礼段前补足差额，已超过则零等待直接进
    RETURNGIFT_MIN_INTERVAL = 10

    # 设备级异常：不属于某个子任务的问题，必须上抛给账号级重试/调度级恢复，
    # 不能被吞掉。GameStuckError / GameTooManyClickError / GameBugError 由
    # script.py 捕获后 task_call('Restart') 重启游戏，一旦在这里被吞掉，
    # 游戏会一直卡着，后续子任务每个都超时抛错、逐个标 failed 并各发一封邮件。
    # GamePageUnknownError 是页面持续无法识别（活动弹窗/更新公告等），同属
    # 环境故障——吞掉会连锁误标所有后续子任务并把账号误判完成；
    # ScriptError 是开发级错误，吞掉会掩盖 bug，维持旧行为直达 script.py。
    # 注意：异常照旧上抛，但当前子任务会先被标记 failed（见 _run_with_stat）——
    # 卡死往往就源自该子任务自身的 UI 分支，不标记会导致每轮接续重复同一次失败。
    # 若增删此清单，须同步 MultiDailyAltAcc.ScriptTask._DEVICE_LEVEL_ERRORS。
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

    def _mark_progress(self, task_key: str, status: str, **extra) -> bool:
        """写子任务进度；没有注入 store 或写入异常都不影响任务流程。"""
        if self._progress is None or not self._progress_key:
            return False
        try:
            return self._progress.mark_task(self._progress_key, task_key, status, **extra)
        except Exception:
            logger.exception('写子任务进度失败')
            return False

    def _mark_progress_false(self, task_key: str) -> None:
        """记录一次显式返回 False；达到上限迁移 skipped 时记一条 warning。

        skipped 不发通知——它不是异常，而是「多次尝试后判定本阶段无事可做」，
        发邮件会在空邮箱/无奖励的正常日子里天天刷屏。
        """
        if self._progress is None or not self._progress_key:
            return
        try:
            if self._progress.mark_task_false(self._progress_key, task_key):
                logger.warning(f'子任务 {task_key} 连续多次未完成，本阶段不再重试（skipped）')
        except Exception:
            logger.exception('写子任务 False 计数失败')

    def _should_skip(self, task_key: str) -> bool:
        """该子任务在本阶段是否已了结（done / failed / skipped），已了结则跳过。"""
        if self._progress is None or not self._progress_key:
            return False
        try:
            return self._progress.is_task_finished(self._progress_key, task_key)
        except Exception:
            logger.exception('读取子任务进度失败，按未完成处理')
            return False

    def _battle_count_now(self) -> int:
        """读当前已落盘的同心战斗场次；无 store 或读失败一律按 0 处理。"""
        if self._progress is None or not self._progress_key:
            return 0
        try:
            return self._progress.get_battle_count(self._progress_key)
        except Exception:
            logger.exception('读取同心战斗场次失败，按 0 处理')
            return 0

    def _should_resume_instead_of_fail(self, task_key: str, battle_before: int) -> bool:
        """同心战斗报错后是否保持 pending，留给下轮接续剩余场次。

        只有同心享受这个豁免：它是计数型子任务，每打一场就落盘一次
        （见 Alliedteam._persist_battle_count），中断后能从断点继续；其余子任务
        没有可累积的进度，照旧标 failed 跳过。

        判据是本轮有没有实质进展：打过至少一场说明是「打到一半被打断」，剩余
        场次值得接续；一场未打就报错说明卡在选关/邀请/组队等入口环节，接续
        只会每轮重复同一次失败，仍按原语义标 failed 跳过。这也保证了收敛性——
        每轮必须有新场次才能继续接续，零进展的那一轮即终止。
        """
        if task_key != 'alliedteam':
            return False
        if self._progress is None or not self._progress_key:
            return False
        return self._battle_count_now() > battle_before

    def _notify_task_failed(self, task_key: str, error: Exception) -> None:
        """子任务首次失败时推送通知，说明本轮后续接续会跳过它。

        整体包 try：设备级异常路径下本方法在 raise 之前调用，
        组装通知内容时的任何取值异常都不能打断异常上抛。
        """
        try:
            # 账号上下文取多账号运行注入的 _stat_ctx，单实例直跑时退化为配置实例名，
            # 保证通知里始终能看出是「哪个账号/角色的哪个子任务」失败
            ctx = getattr(self, '_stat_ctx', None) or {}
            lines = []
            char = ctx.get('char')
            if char:
                lines.append(f'角色：{char}（{ctx.get("svr") or "未知区服"}）')
                if ctx.get('acc'):
                    lines.append(f'账号：{ctx["acc"]}')
            else:
                lines.append(f'实例：{getattr(self.config, "config_name", "未知实例")}')
            lines.append(f'子任务：{task_key}')
            if task_key == 'alliedteam' and self._progress is not None:
                done = self._progress.get_battle_count(self._progress_key)
                lines.append(f'已打场次：{done}/{self._alliedteam_limit}')
            lines.append(f'异常：{error.__class__.__name__} - {str(error).splitlines()[0] if str(error) else ""}')
            lines.append('本轮后续接续将跳过该子任务')
            lines.append(f'如需重试请删除进度文件：{getattr(self._progress, "path", "")}')
            self.config.notifier.push(content='\n'.join(lines), title='子任务异常已跳过')
        except Exception:
            logger.exception('推送子任务异常通知失败')

    def _run_with_stat(self, task_key: str, func, *args, **kwargs):
        """记录子任务起止与进度。

        业务异常（UI 卡死、OCR 失败等）不再炸掉整个账号：标记 failed、发一封
        通知后吞掉异常返回 None，让 run() 继续执行该账号的下一个子任务。
        设备级异常仍原样上抛给 script.py 走恢复，但同样先标记 failed，
        使接续时跳过该子任务，避免同一失败每轮无限重复。

        同心战斗是唯一例外：本轮打过至少一场就保持 pending 让下轮接着打剩余
        场次，详见 _should_resume_instead_of_fail。
        """
        start_time = time.time()
        # 同心战斗进入前的场次基线，异常时用来判断本轮有无实质进展
        battle_before = self._battle_count_now() if task_key == 'alliedteam' else 0
        self.emit_stat(StatEvent.TASK_START, task=task_key)
        try:
            result = func(*args, **kwargs)
        except TaskEnd:
            # TaskEnd 是部分子任务的正常结束信号，算完成，交给原有外层逻辑处理。
            self._mark_progress(task_key, STATUS_DONE)
            self.emit_stat(
                StatEvent.TASK_END,
                task=task_key,
                ok=True,
                dur=round(time.time() - start_time, 3),
            )
            raise
        except self._DEVICE_LEVEL_ERRORS as e:
            # 异常标记只在新一轮 run 开始时重置，后续子任务成功不能覆盖它。
            self._subtask_error_occurred = True
            # 设备级异常仍原样上抛给 script.py 走 Restart/恢复，但**先把当前子任务标记
            # failed**：否则重调度接续时会再跑同一个子任务，若卡死源自该子任务自身的
            # UI 分支（如结界经验弹窗与【一键完成】互点触发 GameTooManyClickError），
            # 就会每轮无限重复同一次失败（实测同一账号连续三轮同点报错）。
            # 代价：真正可恢复的环境故障也不再重试该子任务，改由通知告知人工介入。
            # 同心战斗按本轮进展豁免（见下方 _should_resume_instead_of_fail）。
            emsg = str(e).splitlines()[0] if str(e) else ""
            self.emit_stat(
                StatEvent.ERROR,
                task=task_key,
                etype=e.__class__.__name__,
                emsg=emsg,
            )
            self.emit_stat(
                StatEvent.TASK_END,
                task=task_key,
                ok=False,
                dur=round(time.time() - start_time, 3),
            )
            # 同心打到一半被打断时保持 pending，下轮接续剩余场次；不发「已跳过」
            # 通知——那条内容会误报成放弃，且设备级异常本身已由 script.py 通知
            if self._should_resume_instead_of_fail(task_key, battle_before):
                logger.warning(
                    f'子任务 {task_key} 本轮已打 '
                    f'{self._battle_count_now() - battle_before} 场后中断，'
                    f'保留进度待下轮接续剩余场次'
                )
            # 首次「未失败 → failed」迁移才发通知，避免每轮重调度重复轰炸
            elif self._mark_progress(task_key, STATUS_FAILED, etype=e.__class__.__name__, emsg=emsg):
                self._notify_task_failed(task_key, e)
            raise
        except Exception as e:
            # 同心保持 pending 等吞异常分支，也必须使父任务的登录缓存失效。
            self._subtask_error_occurred = True
            emsg = str(e).splitlines()[0] if str(e) else ""
            self.emit_stat(
                StatEvent.ERROR,
                task=task_key,
                etype=e.__class__.__name__,
                emsg=emsg,
            )
            self.emit_stat(
                StatEvent.TASK_END,
                task=task_key,
                ok=False,
                dur=round(time.time() - start_time, 3),
            )
            if isinstance(e, CoopStoreWriteError):
                # 协作未保存不能标 failed 后跳过；保持待重试，由父任务保留本轮进度。
                raise
            # 未注入 store（单任务直跑）时维持旧行为原样上抛：
            # 此时无进度可标、无跳过机制，吞掉会让故障从「显式报错」变成静默成功
            if self._progress is None or not self._progress_key:
                raise
            # 同心打到一半被打断时同样保持 pending（与设备级分支同一判据）
            if self._should_resume_instead_of_fail(task_key, battle_before):
                logger.warning(
                    f'子任务 {task_key} 本轮已打 '
                    f'{self._battle_count_now() - battle_before} 场后异常中断，'
                    f'保留进度待下轮接续剩余场次: {e}'
                )
                return None
            logger.error(f'子任务 {task_key} 执行异常，已标记跳过: {e}')
            # 首次「未失败 → failed」迁移才发通知，避免每轮重调度重复轰炸
            if self._mark_progress(task_key, STATUS_FAILED, etype=e.__class__.__name__, emsg=emsg):
                self._notify_task_failed(task_key, e)
            return None
        # 显式返回 False 表示业务上未完成（如 courtyard 进入超时）：不标 done
        # （否则接续时被跳过导致漏领奖励），交给 False 计数决定保持 pending
        # 重跑一次，还是达到上限迁移 skipped（避免无奖可领时账号永远无法收尾）。
        if result is not False:
            self._mark_progress(task_key, STATUS_DONE)
        else:
            self._mark_progress_false(task_key)
        self.emit_stat(
            StatEvent.TASK_END,
            task=task_key,
            ok=result is not False,
            dur=round(time.time() - start_time, 3),
        )
        return result

    def _create_nested_task(self, task_cls):
        """创建本任务内部嵌套运行的单账号任务实例（挂卡、寄养）。

        单独抽成方法只为提供扩展点：MultiDailyAltAcc 会覆写它，把嵌套实例换成
        屏蔽调度副作用的子类，避免小号批量执行时改掉大号这些任务的下次运行时间。
        单账号直跑时行为与原来的 task_cls(self.config, self.device) 完全一致。
        """
        return task_cls(self.config, self.device)

    def run(self):
        # 记录本轮全部子任务是否发生异常，不把上一轮的结果带入新运行。
        self._subtask_error_occurred = False
        con = self.get_config()
        self.msg = []
        net_normal_flag = False
        retry_count = 0
        while 1:    
            self.screenshot()
            if  retry_count >=5:
                self.msg.append([MSGType.neterror, "网络错误"])
                raise TaskEnd(self.msg)
            if self.appear(self.I_NET_NORMAL_FLAG,interval=1):
                net_normal_flag = True
                continue

            if self.appear_then_click(self.I_NET_CHECK,action=self.C_NET_CLICK,interval=1):
                time.sleep(7)
                retry_count += 1
                self.screenshot()

            if self.appear_then_click(WantedQuestsAssets.I_WQ_SEAL,interval=1) or self.appear_then_click(WantedQuestsAssets.I_WQ_DONE,interval=1):
                continue

            if self.appear(self.I_UI_BACK_RED):
                self.device.click_record_clear()
                self.ui_click_until_disappear(self.I_UI_BACK_RED,interval=4)
                if net_normal_flag:
                    break
                continue
        # 子任务段起点：回礼需要距本账号任务开始至少间隔 RETURNGIFT_MIN_INTERVAL
        # （原 delay_time 静态估算语义），改用真实计时——前置任务实际跑多久就
        # 吃掉多久等待，轮到回礼时只补足差额
        phase_start = time.time()
        self.screenshot()
        if self.ui_get_current_page() != page_main:
            self.ui_goto(page_main)

        if con.daily_alt_acc_config.courtyard_enable and not self._should_skip("courtyard"):
            courtyard_result = self._run_with_stat("courtyard", self.run_courtyard)
            # 仅当庭院明确返回 False（业务上未完成）才走回主界面补救；
            # 业务异常已被 _run_with_stat 吞掉并返回 None，此时游戏可能仍卡着，
            # 不能进入这个没有超时的补救循环。
            if courtyard_result is False:
                while 1:
                    self.screenshot()
                    if self.appear(GameUiAssets.I_CHECK_MAIN) or self.appear(self.I_M_MAIN_TO_MAIL):
                        break
                    if self.appear_then_click(self.I_TASK_TO_MAIN, interval=1):
                        time.sleep(1)
                        continue
                time.sleep(1)
                self.screenshot()
                if self.ui_get_current_page() != page_main:
                    self.ui_goto(page_main)
                
        if con.daily_alt_acc_config.mail_enable and not self._should_skip("mail"):
            self._run_with_stat("mail", self.run_mail)
        if con.daily_alt_acc_config.cooperation_enable and not self._should_skip("cooperation"):
            self._run_with_stat("cooperation", self.run_cooperation)
        if con.daily_alt_acc_config.donatejade_enable and not self._should_skip("donatejade"):
            self._run_with_stat("donatejade", self.run_donatejade)
        # 神秘商店从回礼之后提前到此处：回礼轮里它和勾协是仅有的前置任务，
        # 先跑商店再查计时，正好用真实耗时填补回礼前的等待窗口
        if con.daily_alt_acc_config.mysteryshop_enable and not self._should_skip("mysteryshop"):
            self._run_with_stat("mysteryshop", self.run_mysteryshop)
        if con.daily_alt_acc_config.returngift_enable and not self._should_skip("returngift"):
            # 回礼最小间隔：距本账号子任务段开始至少 10 秒（沿用原 sleep(10-delay_time)
            # 语义）。前置任务跑够就零等待直接进，不足才补睡差额
            elapsed = time.time() - phase_start
            if elapsed < self.RETURNGIFT_MIN_INTERVAL:
                logger.info(
                    f'回礼前等待补足: 已过 {elapsed:.1f}s / '
                    f'{self.RETURNGIFT_MIN_INTERVAL}s'
                )
                time.sleep(self.RETURNGIFT_MIN_INTERVAL - elapsed)
            self._run_with_stat("returngift", self.run_returngift)
        if con.daily_alt_acc_config.weekaward_enable and not self._should_skip("weekaward"):
            def run_weekaward():
                """执行寮商店、寮商城和分享领取，作为 weekaward 统计单元。"""
                # 寮商店导航经寮主界面（点击神社/寮商店入口），全程启用突发弹窗跳过
                with self.guild_popup_scope():
                    xzconfig= GuildStore(enable=True,mystery_amulet=True,black_daruma_scrap=False,skin_ticket=0)
                    self.execute_guild(xzconfig)
                    self.execute_mall()
                    self._share_collect()
            self._run_with_stat("weekaward", run_weekaward)
        if con.daily_alt_acc_config.tree_planting_enable > 0 and not self._should_skip("tree"):
            self._run_with_stat("tree", self.run_tree_planting)
        if con.daily_alt_acc_config.trialbattle_enable and not self._should_skip("trialbattle"):
            self._run_with_stat("trialbattle", self.run_trialbattle)
        if con.daily_alt_acc_config.summon_up_enable and not self._should_skip("summon_up"):
            self._run_with_stat("summon_up", self.run_summon_up)
        if con.daily_alt_acc_config.publish_sr_enable and not self._should_skip("publish_sr"):
            self._run_with_stat("publish_sr", self.run_publish_sr)

        if con.daily_alt_acc_config.kekkaiActivation_enable and not self._should_skip("kekkaiActivation"):
            try:
                activation_task = self._create_nested_task(KekkaiActivation)
                activation_conf=activation_task.config.kekkai_activation.activation_config
                activation_conf.card_type=CardType.DAILY
                activation_conf.min_taiko_num=1
                activation_conf.exchange_before=False
                activation_conf.exchange_max=False
                activation_conf.card_not_found_count=0
                activation_conf.shikigami_class=ShikigamiClass.MATERIAL
                self._run_with_stat("kekkaiActivation", activation_task.run)
            except TaskEnd:
                pass  # 忽略挂卡任务的结束信号
        if con.daily_alt_acc_config.KekkaiUtilize_enable and not self._should_skip("KekkaiUtilize"):
            # 执行蹭卡
            try:
                utilize_task = self._create_nested_task(KekkaiUtilize)
                # 确保在运行前修改配置
                utilize_task.config.kekkai_utilize.utilize_config.utilize_rule = UtilizeRule.DAILY
                # 同时也设置其他参数
                utilize_task.config.kekkai_utilize.utilize_config.select_friend_list = SelectFriendList.SAME_SERVER
                utilize_task.config.kekkai_utilize.utilize_config.shikigami_class = ShikigamiClass.MATERIAL
                utilize_task.config.kekkai_utilize.utilize_config.shikigami_order = 1
                utilize_task.config.kekkai_utilize.utilize_config.harvest_guild_max_times = 0
                utilize_task.config.kekkai_utilize.utilize_config.utilize_enable = True
                utilize_task.config.kekkai_utilize.utilize_config.guild_ap_enable = False
                utilize_task.config.kekkai_utilize.utilize_config.guild_assets_enable = False
                utilize_task.config.kekkai_utilize.utilize_config.box_ap_enable = False
                utilize_task.config.kekkai_utilize.utilize_config.box_exp_enable = False
                utilize_task.config.kekkai_utilize.utilize_config.box_exp_waste = False
                utilize_task.config.kekkai_utilize.utilize_config.exchange_before = False
                self._run_with_stat("KekkaiUtilize", utilize_task.run)
            except TaskEnd as msg:
                # 直接将KekkaiUtilize的消息透传给Daily，不做额外处理
                if msg.args and msg.args[0]:  # 如果TaskEnd带有参数且不为空
                    for msg_item in msg.args[0]:
                        # 直接将消息添加到当前任务的消息列表中
                        self.msg.append(msg_item)
                pass  # 如果蹭卡任务也有TaskEnd，也需要处理
        if (con.daily_alt_acc_config.alliedteam_battle_enable or con.daily_alt_acc_config.alliedteam_ap_enable) \
                and not self._should_skip("alliedteam"):
            self._run_with_stat("alliedteam", self.run_alliedteam, con.daily_alt_acc_config.alliedteam_battle_enable, con.daily_alt_acc_config.alliedteam_ap_enable)

        self.set_next_run(task='DailyAltAcc', finish=True, success=True)
        logger.info(self.msg)
        raise TaskEnd (self.msg)


if __name__ == "__main__":
    from module.config.config import Config
    from module.device.device import Device

    # 手动调试入口：使用 oas2 测试 SR16 碎片发布流程
    c = Config('oas2')
    d = Device(c)
    self = ScriptTask(c, d)
    self.screenshot()
    result = self._do_publish_sr('I_SR_16')
    print(f'_do_publish_sr(I_SR_16) result: {result}')
