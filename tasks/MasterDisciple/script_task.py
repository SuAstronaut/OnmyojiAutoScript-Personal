# This Python file uses the following encoding: utf-8
# @author 
# github 
import random
import re
from pathlib import Path
import time
from time import sleep
from datetime import datetime, timedelta, time as dtime

from tasks.Component.GeneralBattle.general_battle import GeneralBattle
from tasks.Component.GeneralBattle.reward_frame import (
    weighted_choice, AVOID_WIN_TEAM2)
from tasks.Component.GeneralInvite.general_invite import GeneralInvite, RoomType
from tasks.Component.GeneralInvite.config_invite import InviteConfig, InviteNumber, FindMode
from tasks.BondlingFairyland.assets import BondlingFairylandAssets
from tasks.Component.GeneralRoom.general_room import GeneralRoom
from tasks.Component.SwitchSoul.switch_soul import SwitchSoul
# 探索协战存证共用次数加载校验和庭院重试，避免单独入口仍保存未加载界面。
from tasks.Component.SwitchHelpShikigami.switch_help_shikigami import capture_friend_help_image
from tasks.GameUi.game_ui import GameUi
from tasks.GameUi.page import page_main, page_team, page_shikigami_records, page_exploration,page_youki, page_mall
from tasks.MasterDisciple.assets import MasterDiscipleAssets
from tasks.MasterDisciple.config import MasterDisciple, MasterDiscipleMode
from tasks.MasterDisciple.team_state import (
    BUFF_COIN,
    BUFF_COIN_EXIT,
    BUFF_EXP,
    BUFF_EXP_EXIT,
    disciple_alive,
    MasterDiscipleSession,
    MasterDiscipleStateStore,
    PHASE_FINISHED,
    PHASE_PAIRED,
    StaleSessionError,
    TASK_COIN,
    TASK_EXP,
    TASK_EXPLORATION,
    TASK_GUARD,
    TASK_STONE,
    TASK_SWITCHING,
)
from tasks.Exploration.solo import SoloExploration
from tasks.Exploration.config import ExplorationLevel, UpType
from tasks.Plotline.assets import PlotlineAssets
from tasks.ExperienceYoukai.assets import ExperienceYoukaiAssets
from tasks.GoldYoukai.assets import GoldYoukaiAssets
from tasks.Restart.assets import RestartAssets
from tasks.DailyTrifles.assets import DailyTriflesAssets
from tasks.RichMan.assets import RichManAssets
from tasks.Utils.optional_ability import run_optional_ability
from tasks.Component.SwitchAccount.switch_account import SwitchAccount
from tasks.Component.MultiAccountRunner.progress import ProgressStore, acc_key
from tasks.Component.GeneralBattle.config_general_battle import GeneralBattleConfig
from tasks.Component.GeneralBuff.config_buff import BuffClass
from tasks.Component.config_base import Time

from module.logger import logger
from module.exception import TaskEnd, RequestHumanTakeover, GameNotRunningError
from module.base.timer import Timer
from module.base.utils import save_image


class ScriptTask(GeneralBattle, GeneralInvite, GeneralRoom, SwitchSoul, GameUi, MasterDiscipleAssets,
                 ExperienceYoukaiAssets, GoldYoukaiAssets, BondlingFairylandAssets):
    # 探索任务中切换援助式神相关标记：确认援助式神上场后置 False（后续场次
    # 直接点准备）；未确认上场前保持 True，下一场继续尝试——一次切换失败
    # 不再葬送后续场次（与 DailyAltAcc/alliedteam.py 同步的修复）
    help_shikigami_detect: bool = True
    # 同一场战斗内援助式神锚点（N/15 标签）连续识别失败次数，每场战斗重置
    _help_anchor_miss: int = 0
    # 锚点识别失败的重试上限：一轮约 1.3s（OCR 0.3s + sleep 1s），3 次约 4s；
    # 超限后本场放弃切换直接开打（保持原有降级语义，不会卡死在准备界面）
    HELP_ANCHOR_RETRY_LIMIT: int = 3
    coin_buff: bool =False
    # 经验加成开关状态（师父侧跟随同步指令提前开关，与 coin_buff 同语义）
    exp_buff_on: bool = False
    # 徒弟轮询的账号级续做进度，run_as_disciple 中创建；中断后接续时已完成徒弟直接跳过
    _progress: ProgressStore = None
    # run_guard 每次启动任务第一次战斗必须勾选"默认邀请"并确认勾选成功，防止首次未勾选导致后续战斗不自动邀请
    _guard_default_invite_checked: bool = False
    # 本次运行是否动过账号（徒弟模式自动切号）。
    # 切号后设备停在徒弟号上且没有切回逻辑，此时把探索/经验妖怪等标记成已完成，
    # 改的是本配置实例大号的调度——徒弟干的活算到大号头上，大号会白白跳过一轮。
    _account_switched: bool = False
    # 当前徒弟角色名（切号时记录）：探索完成截图存证用它命名文件；未切号时为 None，退化为配置实例名
    _current_disciple_name: str = None
    # ===== 师徒同步（JSON 状态文件）=====
    # 同步状态存储与会话令牌：仅当开启房间任务且成功发布会话时非 None
    _md_store: MasterDiscipleStateStore = None
    _md_session: MasterDiscipleSession = None
    # 师父实例是否已加入会话（配对成功）；False 时房间任务跳过但状态序列照写
    _md_paired: bool = False
    # 配对等待是否已结束（成功或放弃）：等待只发生在第一个房间任务前，之后不再重复
    _md_join_wait_done: bool = False
    # 徒弟侧心跳节流时间戳（monotonic 秒）
    _md_last_heartbeat: float = 0.0
    # 师父侧设备层卡死检测的续期时间戳（monotonic 秒，见 _md_keepalive_stuck_detection）
    _md_last_keepalive: float = 0.0

    def reward_avoid(self) -> tuple:
        """师徒是两人组队，胜利画面上多出一块队友战绩框，落点回避它。"""
        return AVOID_WIN_TEAM2

    def run(self) -> bool:
        """
        师徒任务主入口
        """
        # 御魂切换：MasterDisciple不再暴露switch_soul配置，但保留师父模式的御魂预设切换
        # 预设切换在run_as_master中处理

        # 每次运行重置切号标记，避免同一实例复用时残留上一轮的状态
        self._account_switched = False
        # 同理重置徒弟角色名，避免残留上一轮切号对象导致截图命名错误
        self._current_disciple_name = None

        limit_count = self.config.master_disciple.master_disciple_config.limit_count
        limit_time = self.config.master_disciple.master_disciple_config.limit_time
        self.current_count = 0
        self.limit_count: int = limit_count
        self.limit_time: timedelta = timedelta(
            hours=limit_time.hour,
            minutes=limit_time.minute,
            seconds=limit_time.second
        )
        self.screenshot()
        self.ui_get_current_page()
        self.ui_goto(page_main)

        config: MasterDisciple = self.config.master_disciple

        success = True
        try:
            match config.master_disciple_config.mode:
                case MasterDiscipleMode.MASTER:
                    success = self.run_as_master()
                case MasterDiscipleMode.DISCIPLE:
                    success = self.run_as_disciple()
                case _:
                    logger.error('Unknown master-disciple mode')
        except TaskEnd:
            # TaskEnd 是任务正常结束信号（守护完成/师父走到探索退出等）：
            # 此时 success 仍为 True，finally 按成功间隔调度；旧逻辑把它当
            # 普通异常置 False，导致正常结束也按失败间隔调度
            raise
        except Exception as e:
            # 异常上抛前必须置 success=False，否则 finally 会误判为成功并按成功间隔调度
            success = False
            raise e
        finally:
            # 下一次运行时间
            if success:
                self.set_next_run('MasterDisciple', finish=True, success=True)
                # 代做标记：师徒流程已在「当前账号」做过这些活动，故推迟其单账号任务。
                # 徒弟模式自动切号时活动是在徒弟号上做的，标记会污染大号调度，必须跳过。
                if self._account_switched:
                    logger.info('[MasterDisciple] 本次运行切换过账号，屏蔽单账号任务的代做标记')
                else:
                    if config.master_disciple_config.run_exploration:
                        self.set_next_run('Exploration', success=True)
                    if config.master_disciple_config.run_exp_monster:
                        self.set_next_run('ExperienceYoukai', success=True)
                    if config.master_disciple_config.run_stone_ju:
                        self.set_next_run('Tako', success=True)
                    if config.master_disciple_config.run_coin_monster:
                        self.set_next_run('GoldYoukai', success=True)
                    if config.master_disciple_config.run_guard:
                        pass
                # 徒弟轮询全部成功收尾后清进度：先调度后清，顺序不可颠倒
                if self._progress is not None:
                    self._progress.clear()
            else:
                self.set_next_run('MasterDisciple', finish=False, success=False)

        raise TaskEnd

    # ======================== 徒弟模式 ========================

    def _run_task_with_retry(self, task_func, task_name: str, max_retries: int = 3) -> bool:
        """
        带重试机制执行小任务，捕获异常后重试，超过重试次数则推送通知并跳过

        :param task_func: 要执行的任务函数
        :param task_name: 任务名称（用于日志和通知）
        :param max_retries: 最大重试次数
        :return: 是否成功
        """
        for attempt in range(1, max_retries + 1):
            try:
                logger.info(f"[{task_name}] 第{attempt}次执行")
                self.device.stuck_record_clear()
                task_func()
                logger.info(f"[{task_name}] 第{attempt}次执行成功")
                return True
            except GameNotRunningError:
                raise
            except RequestHumanTakeover:
                raise
            except TaskEnd:
                break
            except Exception as e:
                logger.error(f"[{task_name}] 第{attempt}次执行异常: {e}")
                if attempt < max_retries:
                    logger.info(f"[{task_name}] 准备重试 ({attempt}/{max_retries})...")
                    # 异常恢复：回到庭院，清理可能残留的状态
                    try:
                        self.device.stuck_record_clear()
                        self.screenshot()
                        self.ui_get_current_page()
                        self.ui_goto(page_main)
                    except Exception:
                        logger.warning(f"[{task_name}] 恢复到庭院失败，继续重试")
                else:
                    logger.error(f"[{task_name}] 已重试{max_retries}次，仍然失败，跳过该任务")
                    self.config.notifier.push(
                        content=f"{task_name}任务异常，已重试{max_retries}次仍失败，已跳过\nError: {e}",
                        title=f"{task_name}任务失败"
                    )
        return False

    def run_as_disciple(self):
        """
        以徒弟身份运行（同步模式：先与师父实例完成 JSON 配对）
        支持 cycle_all_disciples 配置：
        - False: 只切换到第一个徒弟账号执行任务
        - True: 轮询所有徒弟账号，依次切换并执行任务
        """
        logger.info("Running as disciple")

        # 同步配对：发布状态文件并等师父实例加入（只跑单人任务时自动跳过）
        self._md_setup_disciple()
        try:
            account_list = self.config.master_disciple.disciple_account_list
            cycle_all = self.config.master_disciple.master_disciple_config.cycle_all_disciples
            auto_switch = self.config.master_disciple.master_disciple_config.auto_switch_account

            if not auto_switch or not account_list:
                # 不切换账号或没有账号列表，直接在当前账号执行任务
                self._execute_disciple_tasks()
                return True

            if not cycle_all:
                # 只执行第一个徒弟账号
                logger.info("Cycle all disciples is disabled, switching to first disciple account only")
                if not self.switch_to_disciple_account(account_list[0]):
                    return False
                self._execute_disciple_tasks()
                return True

            # 轮询所有徒弟账号：建账号级续做进度，阶段标识 = 徒弟账号集合 + 自然日
            logger.info(f"Cycle all disciples enabled, total {len(account_list)} account(s) to process")
            self._progress = ProgressStore('master_disciple', self.config.config_name)
            self._progress.ensure_phase(
                {'disciples': [acc_key(a.account, a.character, a.svr) for a in account_list],
                 'day': self.start_time.strftime('%Y-%m-%d')},
                self.start_time.strftime('%Y%m%d-%H%M'),
            )
            all_success = True
            for index, account_info in enumerate(account_list):
                key = acc_key(account_info.account, account_info.character, account_info.svr)
                if self._progress.is_account_done(key):
                    logger.info(f"Disciple {account_info.character}-{account_info.svr} already done, skipping")
                    continue
                logger.info(f"Processing disciple account {index + 1}/{len(account_list)}: {account_info.character}-{account_info.svr}")
                if not self.switch_to_disciple_account(account_info):
                    logger.warning(f"Failed to switch to disciple account {account_info.character}-{account_info.svr}, skipping")
                    all_success = False
                    continue
                try:
                    self._execute_disciple_tasks()
                    # 徒弟任务正常完成：即时落盘，中断后接续时整个跳过
                    self._progress.mark_account_done(key)
                except TaskEnd:
                    raise
                except RequestHumanTakeover:
                    raise
                except GameNotRunningError:
                    raise
                except Exception as e:
                    logger.error(f"Error executing tasks for disciple {account_info.character}-{account_info.svr}: {e}")
                    all_success = False
                    # 异常恢复：回到庭院
                    try:
                        self.device.stuck_record_clear()
                        self.screenshot()
                        self.ui_get_current_page()
                        self.ui_goto(page_main)
                    except Exception:
                        logger.warning("Failed to recover to main page after error")

            return all_success
        finally:
            # 通知师父本轮流结束（异常上抛路径也通知，让师父侧立即退出而非等超时）
            self._md_finish()

    def _check_and_buy_ap(self):
        """
        徒弟模式：在庭院检测体力，不足则进商店购买
        流程：庭院OCR读取体力(O_SUSHI_NUM) → 计算需购买次数 → 进商店购买 → 回庭院
        """
        import math
        ap_threshold = self.config.master_disciple.master_disciple_config.ap_threshold
        logger.info(f"[体力购买] 目标体力: {ap_threshold}")

        try:
            # 确保在庭院
            self.screenshot()
            self.ui_get_current_page()
            self.ui_goto(page_main)

            # 在庭院OCR读取当前体力
            self.screenshot()
            current_ap = self.O_SUSHI_NUM.ocr_digit(self.device.image)
            logger.info(f"[体力购买] 当前体力: {current_ap}, 目标: {ap_threshold}")

            # 体力已足够，不需要购买
            if current_ap >= ap_threshold:
                logger.info(f"[体力购买] 当前体力 {current_ap} >= {ap_threshold}，无需购买")
                return

            # 计算需要购买的次数，每次购买获得100体力
            need_ap = ap_threshold - current_ap
            buy_count = math.ceil(need_ap / 100)
            logger.info(f"[体力购买] 还需 {need_ap} 体力，需购买 {buy_count} 次")

            # 导航到商店
            self.ui_goto(page_mall, confirm_wait=3)

            # 进入Special页面（带超时保护）
            enter_timer = Timer(30)
            enter_timer.start()
            while 1:
                self.screenshot()
                if self.appear(RichManAssets.I_SIDE_CHECK_SPECIAL):
                    break
                if enter_timer.reached():
                    logger.warning("[体力购买] 进入Special页面超时，跳过")
                    return
                if self.appear_then_click(RichManAssets.I_SIDE_SURE_SPECIAL, interval=1):
                    continue
                if self.appear_then_click(RichManAssets.I_MALL_SUNDRY, interval=1):
                    continue

            # 循环购买指定次数的体力
            bought = 0
            buy_loop_timer = Timer(60)
            buy_loop_timer.start()
            while bought < buy_count and not buy_loop_timer.reached():
                self.screenshot()

                # 执行一次购买
                logger.info(f"[体力购买] 正在购买第 {bought + 1}/{buy_count} 次")
                if self.appear(DailyTriflesAssets.I_STORE_COST_TYPE_JADE):
                    self.ui_click_until_disappear(DailyTriflesAssets.I_STORE_COST_TYPE_JADE, interval=2)
                    bought += 1
                    logger.info(f"[体力购买] 第 {bought} 次购买成功")
                elif self.appear(DailyTriflesAssets.I_SPECIAL_SUSHI):
                    self.ui_click(DailyTriflesAssets.I_SPECIAL_SUSHI, stop=DailyTriflesAssets.I_STORE_COST_TYPE_JADE, interval=2)
                    self.screenshot()
                    if self.appear(DailyTriflesAssets.I_STORE_COST_TYPE_JADE):
                        self.ui_click_until_disappear(DailyTriflesAssets.I_STORE_COST_TYPE_JADE, interval=2)
                        bought += 1
                        need_harvest_mail = True
                        logger.info(f"[体力购买] 第 {bought} 次购买成功")
                else:
                    logger.warning("[体力购买] 未找到体力购买项，跳过")
                    return
                # 购买后等待页面刷新
                sleep(1)

            if bought >= buy_count:
                logger.info(f"[体力购买] 完成，共购买 {bought} 次，获得 {bought * 100} 体力")
            else:
                logger.warning(f"[体力购买] 购买循环超时，已购买 {bought} 次")

        except Exception as e:
            logger.warning(f"[体力购买] 执行异常: {e}，跳过购买")
        finally:
            # 确保回到庭院
            try:
                self.screenshot()
                self.ui_get_current_page()
                self.ui_goto(page_main)
                logger.info("[体力购买] 已回到庭院，开始领取一次邮件")
                self._harvest_mail_after_buy_ap()
                self.screenshot()
                self.ui_get_current_page()
                self.ui_goto(page_main)
            except Exception:
                logger.warning("[体力购买] 返回庭院或领取邮件失败")

    def _harvest_mail_after_buy_ap(self) -> bool:
        """
        体力检测流程结束后领取一次邮件
        该方法由体力检测流程在回到庭院后调用，复用DailyAltAcc的邮件领取流程
        """
        logger.info("[体力购买] 体力检测流程结束后领取一次邮件")
        from tasks.DailyAltAcc.mail import Mail
        return Mail(self.config, self.device).run_mail()

    def _execute_disciple_tasks(self):
        """
        在当前徒弟账号上执行所有已启用的任务（同步模式：房间任务前先与师父对齐）

        轮询模式（cycle_all_disciples）为纯单人流程：只跑买体力与探索，
        即使勾选了房间任务开关也一律失效跳过（无师父跟随）。
        """
        cfg = self.config.master_disciple.master_disciple_config
        # 轮询模式：只跑单人环节，房间任务开关全部失效
        if self._md_is_cycle_mode():
            if cfg.run_guard or cfg.run_stone_ju or cfg.run_coin_monster or cfg.run_exp_monster:
                logger.info('轮询模式为纯单人流程，房间任务（守护/石距/金币/经验）开关失效不执行')
            # 体力检测与购买
            if cfg.buy_ap_when_low:
                self._run_task_with_retry(self._check_and_buy_ap, "体力检测购买")
            # 执行探索任务
            if cfg.run_exploration:
                self._run_task_with_retry(self.run_exploration_as_disciple, "探索")
            return

        # 体力检测与购买
        if cfg.buy_ap_when_low:
            self._run_task_with_retry(self._check_and_buy_ap, "体力检测购买")
        # 执行守护历练任务
        if cfg.run_guard:
            if self._md_announce_room_task(TASK_GUARD, "守护历练"):
                self._run_task_with_retry(self.run_guard_as_disciple, "守护历练")
        # 执行石距任务
        if cfg.run_stone_ju:
            if self._md_announce_room_task(TASK_STONE, "石距"):
                self._run_task_with_retry(self.run_stone_ju_as_disciple, "石距")
        # 执行金币妖怪任务
        if cfg.run_coin_monster:
            if self._md_announce_room_task(TASK_COIN, "金币妖怪"):
                self._run_task_with_retry(self.run_coin_monster_as_disciple, "金币妖怪")

        # 执行经验妖怪任务
        if cfg.run_exp_monster:
            if self._md_announce_room_task(TASK_EXP, "经验妖怪"):
                self._run_task_with_retry(self.run_exp_monster_as_disciple, "经验妖怪")

        # 执行探索任务（单人环节，不等师父就绪；师父侧看到该任务即结束跟随，
        # 探索期间不再占用师父实例）
        if cfg.run_exploration:
            self._md_set_current_task(TASK_EXPLORATION)
            self._run_task_with_retry(self.run_exploration_as_disciple, "探索")

    def switch_to_disciple_account(self, account_info=None):
        """
        切换到徒弟账号

        :param account_info: 要切换的账号信息，若为None则从列表取第一个账号
        """
        logger.info("Switching to disciple account")

        # 【同步】先告知师父「切号中」：切号耗时数分钟，师父侧按长超时等待
        self._md_set_current_task(TASK_SWITCHING)

        if account_info is None:
            account_list = self.config.master_disciple.disciple_account_list
            if not account_list:
                logger.warning("Disciple account list is empty, cannot switch")
                return False
            account_info = account_list[0]

        # 重置检测记录，避免影响后续操作
        self.device.stuck_record_clear()

        # 只要发起过切号就置标记（不论成败）：设备已无法保证仍停在原账号上，
        # 收尾时不得再把探索/经验妖怪等标记成大号已完成。
        self._account_switched = True
        # 记录当前徒弟角色名：探索完成截图存证用它命名（切号失败不会执行任务，无消费方）
        self._current_disciple_name = account_info.character

        success = SwitchAccount(self.config, self.device, account_info).switchAccount()
        if not success:
            logger.warning(f"Switch to disciple account failed: {account_info.character}-{account_info.svr}")
            self.config.notifier.push(
                content=f"Switch to {account_info.character}-{account_info.svr} Failed, account info: {account_info.account}",
                title="未找到账号"
            )
        else:
            logger.info(f"Successfully switched to disciple account: {account_info.character}-{account_info.svr}")

        return success
    # ======================== 师徒同步辅助（JSON 状态文件） ========================

    def _md_is_cycle_mode(self) -> bool:
        """轮询模式判定：自动切号 + 轮询开关同时开启。

        该模式下徒弟是纯单人流程（只跑探索与领体力，房间任务一律跳过），
        任何时候都不连接师父。
        """
        cfg = self.config.master_disciple.master_disciple_config
        return bool(cfg.auto_switch_account and cfg.cycle_all_disciples)

    def _md_needs_pairing(self) -> bool:
        """是否需要师父配合：守护/石距/金币/经验任一开启即需要同步配对。

        只跑探索、买体力等单人任务时完全不连接师父（不发布、不等待），
        避免无意义的配对等待。
        """
        # 轮询模式是纯单人流程（只有探索/买体力），任何时候都不连接师父
        if self._md_is_cycle_mode():
            return False
        cfg = self.config.master_disciple.master_disciple_config
        return bool(cfg.run_guard or cfg.run_stone_ju or cfg.run_coin_monster or cfg.run_exp_monster)

    def _md_setup_disciple(self) -> None:
        """徒弟侧同步初始化：任务一开始就发布新会话，不等待师父加入。

        发布后立即返回去切号/买体力——师父那边切完御魂预设随时可以加入
        （join 与徒弟的单人环节并行），配对确认推迟到第一个房间任务前
        （见 _md_wait_master_join）。
        - 未开启任何房间任务：跳过同步直接返回（_md_store 保持 None）
        - master_instance 未配置：保持未配对（房间任务将跳过）
        """
        self._md_store = None
        self._md_session = None
        self._md_paired = False
        self._md_join_wait_done = False
        self._md_last_heartbeat = time.monotonic()
        if not self._md_needs_pairing():
            logger.info('未开启任何房间任务，跳过师徒同步配对')
            return
        master_instance = str(self.config.master_disciple.master_disciple_config.master_instance or '').strip()
        if not master_instance:
            logger.error('已开启房间任务但未配置师父实例名(master_instance)，无法建立同步')
            self.config.notifier.push(
                content='师徒任务开启了房间任务但未配置师父实例名(master_instance)，守护/石距/金币/经验将全部跳过',
                title='师徒同步配置缺失')
            return
        self._md_store = MasterDiscipleStateStore(master_instance)
        state = self._md_store.publish_session(self.config.config_name)
        self._md_session = MasterDiscipleSession.from_state(state)
        logger.info(f'已发布师徒同步会话，师父实例 [{master_instance}] 切完预设后可随时加入')

    def _md_wait_master_join(self, timeout: int = 300) -> bool:
        """等待师父加入会话：在第一个房间任务前确认配对（只等待一次）。

        徒弟发布后与师父的御魂切换并行（切号/买体力不等师父），到需要
        师父配合的第一个房间任务时才汇合：师父此时通常已切完预设。
        等待失败后后续房间任务不再重复等待（_md_join_wait_done 标记），
        但状态序列照写——迟到的师父加入后能顺着 current_task 跟到
        FINISHED 自然退出。
        :return: 是否配对成功
        """
        wait_pair = Timer(timeout).start()
        sweep_count = 0
        while not wait_pair.reached():
            state = self._md_store.read()
            if state.get('phase') == PHASE_PAIRED:
                self._md_paired = True
                logger.info(f'师父实例 [{state.get("master_joined_instance")}] 已加入同步会话')
                return True
            # 心跳保新鲜：师父只加入「最近心跳过的」会话，防止误连上一轮残留
            self._md_heartbeat_disciple_if_due()
            # 每约30秒清一次弹窗（拒绝好友邀请/关确认弹窗），防长等待期间堆积遮挡；
            # 同时给设备层的空闲看门狗续期（两侧的配对等待都是 300 秒）
            sweep_count += 1
            if sweep_count % 30 == 0:
                self._md_idle_popup_sweep()
            self._md_keepalive_stuck_detection()
            sleep(1)
        logger.warning('等待师父实例加入配对超时，房间任务将全部跳过')
        self.config.notifier.push(
            content='师父实例在300秒内未加入同步会话，守护/石距/金币/经验已跳过',
            title='师徒配对超时')
        return False

    def _md_heartbeat_disciple_if_due(self) -> None:
        """徒弟侧心跳（5 秒节流）：等待期间刷新 disciple_seen_at 保会话新鲜。"""
        if self._md_store is None or self._md_session is None:
            return
        now = time.monotonic()
        if now - self._md_last_heartbeat < 5:
            return
        try:
            self._md_store.heartbeat(self._md_session, 'disciple')
        except StaleSessionError:
            # 会话被新发布的会话取代（非 1:1 部署才会发生）：不再视为已配对
            logger.warning('师徒同步会话已失效，停止徒弟侧心跳')
            self._md_paired = False
        self._md_last_heartbeat = now

    def _md_set_current_task(self, task: str, buff_command: str = '') -> None:
        """向师父下发当前任务与加成指令；会话缺失/失效时仅记日志不中断本地流程。"""
        if self._md_store is None or self._md_session is None:
            return
        try:
            self._md_store.set_current_task(self._md_session, task, buff_command)
            logger.info(f'已下发任务同步: [{task}] 加成指令: [{buff_command or "无"}]')
        except StaleSessionError:
            logger.warning(f'师徒同步会话已失效，无法下发任务 [{task}]')
            self._md_paired = False

    def _md_master_in_room(self) -> bool:
        """师父此刻是否在房间里（读共享状态，无截图依赖）。

        这是**状态**查询而不是事件对齐：师父进房置位、离开房间复位，徒弟只看
        当前值，不问这是第几次邀请、也不需要记录自己消费到哪一版。此前两版
        事件对齐（徒弟递增邀请序号 / 师父累加进房次数）都要求双方对某个编号
        达成共识，而游戏自动发邀请的时机不受脚本控制，共识必然出现窗口期。
        取代旧的「反复识别加号数量并等稳定」进房检测。
        """
        if self._md_store is None or self._md_session is None or not self._md_paired:
            return False
        try:
            state = self._md_store.read()
        except Exception as e:
            logger.warning(f'读取师徒同步状态失败: {e}')
            return False
        if MasterDiscipleSession.from_state(state) != self._md_session:
            return False
        return bool(state.get('master_in_room'))

    def _md_idle_popup_sweep(self) -> None:
        """等待配对/就绪期间的轻量弹窗清理：拒绝好友邀请、关确认类弹窗。

        长等待循环若不截图，游戏内弹窗会堆积遮挡画面，后续导航只能靠
        ui_goto 的 unknown 兜底；每约 30 秒清一次把风险压回正常水平。
        """
        try:
            self.screenshot()
            self._reject_invite_popups()
            self._handle_popup()
        except Exception as e:
            # 清理失败不中断等待主流程（弹窗兜底交给后续导航）
            logger.warning(f'等待期间弹窗清理失败: {e}')

    def _md_wait_master_ready(self, task: str, timeout: int = 240) -> bool:
        """等待师父对当前任务的准备完成（提前开加成）标记。

        :param timeout: 超时秒数（开加成最多一两分钟，240 秒留足余量）
        :return: True 师父已就绪；False 未配对/超时/会话失效，调用方跳过该房间任务
        """
        if not self._md_paired or self._md_store is None or self._md_session is None:
            return False
        wait_ready = Timer(timeout).start()
        sweep_count = 0
        while not wait_ready.reached():
            state = self._md_store.read()
            if MasterDiscipleSession.from_state(state) != self._md_session:
                logger.warning('师徒同步会话已失效，停止等待师父就绪')
                self._md_paired = False
                return False
            if state.get('master_ready'):
                logger.info(f'师父已就绪，开始执行任务 [{task}]')
                return True
            self._md_heartbeat_disciple_if_due()
            # 每约30秒清一次弹窗，防长等待期间堆积遮挡
            sweep_count += 1
            if sweep_count % 30 == 0:
                self._md_idle_popup_sweep()
            sleep(1)
        logger.warning(f'等待师父就绪超时({timeout}秒)，跳过任务 [{task}]')
        # 师父就绪超时按「师父失联」处理：后续房间任务不再各等一轮超时，直接全部跳过
        self._md_paired = False
        self.config.notifier.push(content=f'等待师父就绪超时，{task} 任务已跳过', title='师徒同步超时')
        return False

    def _md_announce_room_task(self, task: str, task_label: str) -> bool:
        """下发房间任务与战斗行为指令，并等师父就绪。

        buff_command 同时承载「是否开加成」与「打完还是退出」两个决策，
        师父侧只看同步指令、不读自己配置，杜绝两边开关不一致：
          - 金币/经验的「打完」变体 → 师父提前开对应加成
          - 「准备后退出」变体 → 不开加成（退出的场次开加成是浪费）
        未配对也照写状态序列（迟到的师父能跟随到 FINISHED），但不再等待就绪。

        :param task: team_state 任务类型常量
        :param task_label: 日志与通知用的中文任务名
        :return: True 可以执行；False 跳过该房间任务
        """
        cfg = self.config.master_disciple.master_disciple_config
        buff_command = ''
        if task == TASK_COIN:
            buff_command = BUFF_COIN if not cfg.master_coin_exit_after_prepare else BUFF_COIN_EXIT
        elif task == TASK_EXP:
            buff_command = BUFF_EXP if not cfg.master_exp_exit_after_prepare else BUFF_EXP_EXIT
        # 师父尚未加入时先等配对（只等一次）：发布后切号/买体力与师父切预设
        # 并行进行，这里是与师父的汇合点
        if not self._md_paired and not self._md_join_wait_done:
            self._md_join_wait_done = True
            if not self._md_wait_master_join():
                # 配对失败：照写状态序列推进（迟到师父可跟随），但跳过执行
                self._md_set_current_task(task, buff_command)
                logger.warning(f'跳过房间任务 [{task_label}]（师父未配对）')
                return False
        self._md_set_current_task(task, buff_command)
        if not self._md_wait_master_ready(task_label):
            logger.warning(f'跳过房间任务 [{task_label}]')
            return False
        return True

    def _md_finish(self) -> None:
        """徒弟整个序列完成时通知师父退出；失败仅记日志不影响调度收尾。"""
        if self._md_store is None or self._md_session is None:
            return
        try:
            self._md_store.mark_finished(self._md_session)
            logger.info('已通知师父本轮流结束')
        except StaleSessionError:
            logger.warning('师徒同步会话已失效，无法通知师父结束')

    def _reject_invite_popups(self) -> None:
        """关掉他人发来的邀请弹窗（会遮挡加号区域）；结束时最后一帧为干净画面。

        原 _get_add_count 的内嵌逻辑抽出，拒绝优先级保持原样。
        """
        from tasks.Component.GeneralInvite.assets import GeneralInviteAssets as gia
        while 1:
            self.screenshot()
            if not (self.appear(gia.I_I_REJECT_1) or self.appear(gia.I_I_REJECT_2)
                    or self.appear(gia.I_I_REJECT_3) or self.appear(gia.I_I_REJECT_4)):
                break
            if self.appear(gia.I_I_REJECT_4):
                self.click(gia.I_I_REJECT_4, 1)
                continue
            if self.appear(gia.I_I_REJECT_1):
                self.click(gia.I_I_REJECT_1, 1)
                continue
            if self.appear(gia.I_I_REJECT_3):
                self.click(gia.I_I_REJECT_3, 1)
                continue
            if self.appear(gia.I_I_REJECT_2):
                self.click(gia.I_I_REJECT_2, 1)
                continue

    def _count_add_icons_once(self) -> int:
        """单次统计房间加号图标数量，不再等待读数稳定。

        仅用于公开房补位判定：师父进房信号改由 JSON 同步后，加号计数
        只剩「等路人补位」一个用途——加号变少即有人进，无需稳定确认。
        """
        self.device.stuck_record_add('BATTLE_STATUS_S')
        # 拒绝他人邀请弹窗后最后一帧即为干净画面；留半秒给弹窗消失/进人
        # 动画走完再计数，防动画帧上加号模板瞬时缺失导致误判
        self._reject_invite_popups()
        sleep(0.5)
        self.screenshot()
        count = len(self.I_CLICK_INVITE_ADD.match_all_any(self.device.image))
        logger.info(f'当前加号数量:[{count}]')
        return count

    def _create_room_and_invite(self, task_name: str, room_type: RoomType = RoomType.NORMAL_5,
                                 navigate_and_create_func=None, invite_timeout: int = None,
                                 wait_for_others: bool = True) -> bool:
        """
        创建房间并邀请师父/好友的通用流程（同步模式）
        通用流程：导航并创建房间 → 等待进入房间 → 递增邀请序号 → 邀请师父 → 轮询同步状态等师父回报进房
        师父进房判定由 JSON 同步信号取代旧的加号数量稳定检测；公开补位阶段保留单次加号计数

        :param task_name: 任务名称，如 '金币妖怪'、'守护历练'（用于日志和通知）
        :param room_type: 房间类型，决定加号图标和邀请逻辑
        :param navigate_and_create_func: 导航并创建房间的函数，无参数，返回bool（True=成功进入房间）
            若为None，则使用默认流程：ui_goto(page_team) → check_zones(task_name) → create_room → ensure_private → create_ensure
        :param invite_timeout: 等待师父进入房间的超时时间（秒），None时使用全局配置 invite_timeout
        :param wait_for_others: 师父进入后是否公开房间并等待其他人（默认True保持现有行为）
        :return: True 师父已进入房间，False 邀请失败或超时
        """
        master_name = self.config.master_disciple.master_disciple_config.master_name
        # 未指定超时时回退到全局配置，保证守护历练等既有调用行为不变
        if invite_timeout is None:
            invite_timeout = self.config.master_disciple.master_disciple_config.invite_timeout

        if navigate_and_create_func is not None:
            # 使用自定义的导航+创建房间函数
            if not navigate_and_create_func():
                logger.warning(f"[{task_name}] Failed to navigate and create room")
                return False
        else:
            # 默认流程：导航到组队页面 → 创建私人房间
            sleep(2)
            self.screenshot()
            self.ui_get_current_page()
            self.ui_goto(page_team)
            self.check_zones(task_name)

            if not self.create_room():
                logger.warning(f"[{task_name}] Failed to create room")
                return False

            self.ensure_private()
            self.create_ensure()

        # 等待进入房间
        wait_enter = Timer(10)
        wait_enter.start()
        while 1:
            self.screenshot()
            if self.is_in_room():
                break
            if wait_enter.reached():
                logger.warning(f"[{task_name}] Failed to enter room")
                return False

        # 未配对时不应走到这里（房间任务已被上层跳过），防御性退出避免死等
        if not self._md_paired:
            logger.warning(f"[{task_name}] 师徒未配对，无法确认师父进房，跳过")
            return False

        # 根据房间类型选择初始加号图标数量（公开切换后的补位目标值）
        add_num = self._get_add_icons(room_type)
        # wait_for_others=False 时跳过公开房间和等待他人步骤
        if add_num == 4 and wait_for_others:
            add_other=True
        else:
            add_other=False
        # 公开切换完成后置 True：该阶段改为单次加号计数等路人补位
        public_waiting = False

        # 等待师父进入房间，每15秒重新邀请一次
        self.device.stuck_record_clear()
        self.device.stuck_record_add('BATTLE_STATUS_S')
        wait_timer = Timer(invite_timeout)
        wait_timer.start()
        reinvite_timer = Timer(15)
        reinvite_timer.start()
        # 师父可能已经被游戏自己拉进房间了：守卫第一场勾选「默认邀请」后，
        # 之后每次建房游戏都会自动邀请上一次的队友，通常比这里的手动邀请更快
        # （实测建房后 5~7 秒师父就已就位）。同步信号说师父已在房间时就不再
        # 从好友列表邀请一遍——那要开列表、OCR 找人、确认，约 6 秒，而且只有
        # 邀请态确认后房间权限才会停在「仅邀请」，让路人补位更晚
        if self._md_master_in_room():
            logger.info(f"[{task_name}] 师父已在房间（游戏自动邀请），跳过手动邀请")
        else:
            # 首次邀请师父（即使失败也不退出，继续等待重试）
            self._invite_by_room_type(master_name, room_type)

        while 1:
            self.screenshot()

            if not self.is_in_room():
                continue

            if wait_timer.reached():
                logger.warning(f"[{task_name}] Master did not accept invite within {invite_timeout}s")
                self.config.notifier.push(
                    content=f"师父{master_name}在{invite_timeout}秒内未接受邀请，任务：{task_name}已跳过",
                    title="邀请师父超时"
                )
                self.exit_room()
                return False
            # 【同步】师父进房信号（师父此刻是否在房间里），取代旧的加号稳定检测。
            # 公开补位阶段必须跳过该判定：师父进房后该状态恒真，继续命中会直接
            # return True，「等路人补位」分支将永远不可达
            if not public_waiting and self._md_master_in_room():
                if add_other:
                    # 师父已进房且需要公开补位：切换为「所有人可见」。
                    # 两段循环都必须带超时兜底——它们是纯 UI 操作，切不过去时
                    # （弹窗遮挡导致文字识别不到、点了没反应）不能把整轮任务
                    # 挂死在这里：外层等师父的超时盖不住本段，而师父那边的
                    # wait_battle 到点就会超时离房，结果是整场白打
                    switch_ok = False
                    open_timer = Timer(20).start()
                    while not open_timer.reached():
                        self.screenshot()
                        if self.appear(self.I_ENSURE_SWITCH):
                            switch_ok = True
                            break
                        self.appear_then_click(self.I_TO_SWITCH, interval=1)
                    if switch_ok:
                        confirmed = False
                        confirm_timer = Timer(20).start()
                        while not confirm_timer.reached():
                            self.screenshot()
                            if "所有人"in self.O_ADD_ALL.ocr(self.device.image):
                                confirmed = True
                                break
                            if self.ui_click(self.I_SWITCH_ALL,stop=self.I_SWITCH_ALL_OVER, interval=1):
                                if self.appear_then_click(self.I_ENSURE_SWITCH,interval=1):
                                    continue
                        switch_ok = confirmed
                    if not switch_ok:
                        # 切不过去就放弃补位：师父已在房间里，直接按「仅邀请」开战。
                        # 少几个路人的收益，远好过把整场挂死（师父会先超时离房）
                        logger.warning(f"[{task_name}] 切换「所有人」失败，放弃公开补位直接开战")
                        # 存一张现场：这一步失败说明 O_ADD_ALL 认不出房间权限文字，
                        # 只看日志分不清是弹窗遮挡还是 UI 布局变了
                        self.save_image(content='切换所有人失败')
                        add_other = False
                        public_waiting = False
                        return True
                    # 公开后加号目标值：5人房4-2=2，即等路人补到4/5人
                    add_num-=2
                    add_other=False
                    public_waiting=True
                    continue
                logger.info(f"return True")
                return True
            # 公开补位阶段：单次读加号数（不等稳定），小于目标值即有路人进来，直接开战
            if public_waiting and self._count_add_icons_once() < add_num:
                logger.info(f"[{task_name}] 路人已补位，开始挑战 (加号目标值:{add_num})")
                return True
            # 每15秒重新邀请师父（重邀请不产生新的进房计数，纯兜底动作）。
            # 师父已在房间时再邀请一遍是空动作：它在房间里收不到邀请，白开一次
            # 好友列表；公开补位阶段（此时师父必然已在房间）尤其不该再发邀请
            if reinvite_timer.reached():
                reinvite_timer.reset()
                if self._md_master_in_room():
                    logger.info(f"[{task_name}] 师父已在房间，跳过重新邀请")
                else:
                    logger.info(f"[{task_name}] Re-inviting master: {master_name}")
                    self._invite_by_room_type(master_name, room_type)
            # 等待期间保持同步会话心跳
            self._md_heartbeat_disciple_if_due()

        return False

    def _get_add_icons(self, room_type: RoomType) -> int:
        """
        根据房间类型获取需要监测的加号图标个数

        :param room_type: 房间类型
        :return: 加号图标个数
        """
        if room_type == RoomType.NORMAL_2 or room_type == RoomType.ETERNITY_SEA:
            return 1
        elif room_type == RoomType.NORMAL_3:
            return 2
        else:
            return 4
    def _goto_invite(self):
        while 1:
            self.screenshot() 
            if self.appear(self.I_LOAD_FRIEND):
                break
            if self.appear(self.I_INVITE_ENSURE):
                break
            if self.appear_then_click(self.I_CLICK_INVITE_ADD,interval=1):
                continue
    def _invite_by_room_type(self, name: str, room_type: RoomType) -> bool:
        """
        根据房间类型分派邀请逻辑

        :param name: 被邀请人名字
        :param room_type: 房间类型
        :return: 是否邀请成功
        """
        # 邀请前先关闭可能遮挡好友名字的拒绝按钮，金币妖怪房间中偶发出现
        from tasks.Component.GeneralInvite.assets import GeneralInviteAssets as gia
        reject_timer = Timer(3).start()
        while not reject_timer.reached():
            self.screenshot()
            if self.appear_then_click(gia.I_I_REJECT_4, interval=0.5):
                continue
        if room_type == RoomType.NORMAL_2 :
            return self._guard_invite_friend_no_tab(name)
        elif room_type == RoomType.NORMAL_3:
            return self._invite_friend_3room(name)
        else:
            return self.invite_friend(name, FindMode.AUTO_FIND)

    def _invite_friend_3room(self, name: str = None) -> bool:
        """
        3人房邀请好友（石距等），只有好友/跨区两个标签
        参照BondlingFairyland的invite_friend实现
        :param name: 好友名字
        :return: 是否邀请成功
        """
        logger.info('Click add to invite friend (3-room)')
        # 点击＋号（3人房用I_ADD_1和I_ADD_2）
        self._goto_invite()

        # 识别好友标签（只有好友和跨区）
        friend_class = []
        list_1 = self.O_FRIEND.ocr(self.device.image)
        list_2 = self.O_KUAQU.ocr(self.device.image)
        list_1 = list_1.replace(' ', '').replace('、', '')
        list_2 = list_2.replace(' ', '').replace('、', '')
        if list_1 is not None and list_1 != '' and list_1 in self.friend_class:
            friend_class.append(list_1)
        if list_2 is not None and list_2 != '' and list_2 in self.friend_class:
            friend_class.append(list_2)
        for i in range(len(friend_class)):
            if friend_class[i] == '蔡友':
                friend_class[i] = '寮友'
            elif friend_class[i] == '路区':
                friend_class[i] = '跨区'
            elif friend_class[i] == '察友':
                friend_class[i] = '寮友'
            elif friend_class[i] == '区':
                friend_class[i] = '跨区'
        logger.info(f'Friend class: {friend_class}')

        is_select: bool = False

        for index in range(len(friend_class)):
            if is_select:
                continue
            # 切换到对应的好友标签
            while index == 0:
                self.screenshot()
                if self.appear(self.I_SELECT_FRIEND_ON):
                    break
                if self.appear_then_click(self.I_SELECT_FRIEND_OFF, interval=1):
                    continue
            while index == 1:
                self.screenshot()
                if self.appear(self.I_SELECT_KUAQU_ON):
                    break
                if self.appear_then_click(self.I_SELECT_KUAQU_OFF, interval=1):
                    continue

            # 等待好友列表加载后搜索
            logger.info(f'Now find friend in {friend_class[index]}')
            sleep(1)
            if not is_select:
                if self.detect_select(name):
                    is_select = True
            sleep(1)
            if not is_select:
                if self.detect_select(name):
                    is_select = True

        # 点击确定
        logger.info('Click invite ensure')
        if not self.appear(self.I_INVITE_ENSURE):
            logger.warning('No appear invite ensure while invite friend')
        while 1:
            self.screenshot()
            if not self.appear(self.I_INVITE_ENSURE):
                break
            if self.appear_then_click(self.I_INVITE_ENSURE):
                continue
        # 没有找到好友也点击确认以退出好友列表
        if not is_select:
            logger.warning('No find friend')
            logger.info('Task failed')
            return False

        return True

    def _run_battle_with_invite(self, zones_name: str, battle_count: int = 2,
                                 buff_open_func=None, buff_close_func=None,
                                 battle_wait_func=None,
                                 room_type: RoomType = RoomType.NORMAL_5,
                                 wait_for_others: bool = True) -> bool:
        """
        徒弟模式下带师父邀请的战斗通用流程

        :param zones_name: 副本名称
        :param battle_count: 战斗次数
        :param buff_open_func: 开buff的函数
        :param buff_close_func: 关buff的函数
        :param battle_wait_func: 战斗等待函数（默认使用 run_general_battle）
        :param room_type: 房间类型，5人房(NORMAL_5)或3人房(NORMAL_3)
        :param wait_for_others: 是否在师父进入后公开房间等待其他人（默认True）
        :return: 是否成功完成
        """
        
        start_time =time.time() 
        while  time.time() - start_time< 5:
            self.screenshot()
            if self.appear_then_click(self.I_UI_BACK_YELLOW,interval=1):
                start_time =time.time() 
                continue
            if self.appear(PlotlineAssets.I_PAGE_MAIN,interval=1):
                start_time =time.time() 
                break
        # 开启加成
        if buff_open_func:
            self.screenshot()
            self.ui_get_current_page()
            self.ui_goto(page_main)
            #buff_open_func()

        count = 0
        while count < battle_count:
            self.device.stuck_record_clear()
            # 创建私人房间并邀请师父（石距/金币/经验固定等待4分钟）
            if not self._create_room_and_invite(zones_name, room_type=room_type, invite_timeout=240, wait_for_others=wait_for_others):
                # 邀请失败，跳过该任务
                logger.warning(f"Skip {zones_name} due to invite failure")
                break
            logger.info(f"click_fire")
            # 师父已进入房间，点击挑战
            self.click_fire()
            count += 1

            # 执行战斗（battle_before内会点击准备按钮）
            if battle_wait_func:
                # 使用通用battle_before处理准备阶段，再用自定义battle_wait处理结算
                battle_config = GeneralBattleConfig(lock_team_enable=True)
                self.battle_before(buff=None, config=battle_config)
                battle_wait_func()
            else:
                self.run_general_battle(config=GeneralBattleConfig(lock_team_enable=True))

            # 战斗结束后处理可能出现的弹窗（绑定手机、活动弹窗等）
            self._handle_post_battle_popup()

            # 战斗结束后检查是否需要再次邀请（默认邀请）
            self.device.stuck_record_add('BATTLE_STATUS_S')
            self.check_and_invite(default_invite=True)

        # 关闭加成
        if buff_close_func:
            self.screenshot()
            self.ui_get_current_page()
            self.ui_goto(page_main)
            #buff_close_func()

        return count > 0

    def _handle_popup(self):
        """
        处理战斗胜利后可能出现的弹窗（绑定手机、活动弹窗等）
        在 battle_wait 循环内和战斗结束后调用
        """
        # 绑定手机弹窗：先检测"前往绑定"，再点击"取消绑定"
        if self.appear(RestartAssets.I_LOGIN_LOGIN_GOTO_BIND_PHONE):
            logger.info("Detected bind phone popup, closing it")
            if self.appear_then_click(RestartAssets.I_LOGIN_LOGIN_CANCEL_BIND_PHONE, interval=1):
                logger.info("Closed bind phone popup")
                return True
        if self.appear_then_click(PlotlineAssets.I_PAGE_CLICK_ANY, interval=1):
            logger.info("Closed bind phone popup")
            return True
        # 通用确认弹窗
        if self.appear_then_click(self.I_UI_CONFIRM, interval=1):
            logger.info("Closed UI confirm popup")
            return True
        if self.appear_then_click(self.I_UI_CONFIRM_SAMLL, interval=1):
            logger.info("Closed small UI confirm popup")
            return True
        # "知道了"弹窗
        if self.appear_then_click(self.I_UI_GOTIT_SMALL, interval=1):
            logger.info("Closed 'got it' popup")
            return True
        # 取消按钮弹窗
        if self.appear_then_click(self.I_UI_CANCEL, interval=1):
            logger.info("Closed UI cancel popup")
            return True
        if self.appear_then_click(self.I_UI_CANCEL_SAMLL, interval=1):
            logger.info("Closed small UI cancel popup")
            return True
        return False

    def _exp_youkai_battle_wait(self):
        """
        经验妖怪战斗结算（参照ExperienceYoukai.battle_wait）
        检测 I_DE_WIN 或 I_EXP_WIN，处理胜利后弹窗
        """
        self.device.stuck_record_add('BATTLE_STATUS_S')
        self.device.click_record_clear()
        logger.info("Start exp youkai battle process")
        while 1:
            self.screenshot()
            if self.appear_then_click(self.I_PREPARE_HIGHLIGHT, interval=1):
                logger.info('click prepare')
            # 处理弹窗（绑定手机、活动弹窗等）
            self._handle_popup()
            if self.appear(self.I_DE_WIN):
                logger.info('Win battle (DE_WIN)')
                self.ui_click_until_disappear(self.I_DE_WIN)
                # 胜利后可能出现弹窗，循环处理直到回到组队/主界面
                self._handle_post_battle_popup()
                return True
            if self.appear(self.I_EXP_WIN):
                logger.info('Win battle (EXP_WIN)')
                self.ui_click_until_disappear(self.I_EXP_WIN)
                self._handle_post_battle_popup()
                return True
            if self.appear(self.I_FALSE):
                logger.warning('False battle')
                self.ui_click_until_disappear(self.I_FALSE)
                return False

    def _handle_post_battle_popup(self, timeout: float = 5):
        """
        处理战斗胜利点击后可能出现的弹窗（绑定手机、活动提示等）
        最多等待timeout秒，确保弹窗被关闭
        """
        start = time.time()
        while time.time() - start < timeout:
            self.screenshot()
            if self._handle_popup():
                # 处理了一个弹窗，重置计时
                start = time.time()
                continue
            # 没有弹窗了，退出
            break

    def _gold_youkai_battle_wait(self):
        """
        金币妖怪战斗结算（参照GoldYoukai.battle_wait）
        检测 I_DE_WIN 或 I_GOLD_WIN，处理胜利后弹窗
        """
        self.device.stuck_record_add('BATTLE_STATUS_S')
        self.device.click_record_clear()
        logger.info("Start gold youkai battle process")
        while 1:
            self.screenshot()
            if self.appear_then_click(self.I_PREPARE_HIGHLIGHT, interval=1):
                logger.info('click prepare')
            # 处理弹窗
            self._handle_popup()
            if self.appear(self.I_DE_WIN):
                logger.info('Win battle (DE_WIN)')
                self.ui_click_until_disappear(self.I_DE_WIN)
                self._handle_post_battle_popup()
                return True
            if self.appear(self.I_GOLD_WIN):
                logger.info('Win battle (GOLD_WIN)')
                self.ui_click_until_disappear(self.I_GOLD_WIN)
                self._handle_post_battle_popup()
                return True
            if self.appear(self.I_FALSE):
                logger.warning('False battle')
                self.ui_click_until_disappear(self.I_FALSE)
                return False

    def _tako_battle_wait(self):
        """
        石距战斗结算（参照Tako.battle_wait）
        检测 I_WIN 或 I_REWARD，再点击领奖直到回到主界面/组队
        """
        self.device.stuck_record_add('BATTLE_STATUS_S')
        self.device.click_record_clear()
        logger.info("Start tako battle process")
        while 1:
            self.screenshot()
            if self.appear_then_click(self.I_PREPARE_HIGHLIGHT, interval=1):
                logger.info('click prepare')
            # 处理弹窗
            self._handle_popup()
            # 胜利画面 I_WIN/I_WIN_2/I_DE_WIN 共判
            if self.win_appear() or self.appear(self.I_REWARD):
                logger.info('Win battle')
                self.ui_click_until_disappear(self.I_WIN)
                self.ui_click_until_disappear(self.I_WIN_2)
                while 1:
                    self.screenshot()
                    # 处理弹窗
                    self._handle_popup()
                    if self.appear(self.I_CHECK_MAIN) or self.appear(self.I_CHECK_TEAM):
                        break
                    if self.click(self.C_REWARD_2, interval=2):
                        continue
                return True
            if self.appear(self.I_FALSE):
                logger.warning('False battle')
                self.ui_click_until_disappear(self.I_FALSE)
                return False

    def run_coin_monster_as_disciple(self):
        """
        徒弟模式 - 金币妖怪（2次，邀请师父，5人房）
        战斗结算：检测 I_DE_WIN 或 I_GOLD_WIN（参照GoldYoukai）
        等待路人：师父准备后退出→不等直接开战（快速收尾）；
        师父打完→公开房间等路人补位到4/5人（凑满打收益最大）
        """
        logger.info("Running coin monster as disciple")

        # 等路人与师父退出开关反向联动：退出=不等，打完=等
        wait = not self.config.master_disciple.master_disciple_config.master_coin_exit_after_prepare
        self._run_battle_with_invite(
            zones_name='金币妖怪',
            battle_count=2,
            battle_wait_func=self._gold_youkai_battle_wait,
            room_type=RoomType.NORMAL_5,
            wait_for_others=wait
        )

    def run_exp_monster_as_disciple(self):
        """
        徒弟模式 - 经验妖怪（2次，邀请师父，5人房）
        战斗结算：检测 I_DE_WIN 或 I_EXP_WIN（参照ExperienceYoukai）
        写死开启50%和100%经验加成
        """
        logger.info("Running experience monster as disciple")

        def open_buff():
            self.open_buff()
            self.exp_50()
            self.exp_100()
            self.close_buff()

        def close_buff():
            self.open_buff()
            self.exp_50(False)
            self.exp_100(False)
            self.close_buff()

        # 等路人与师父退出开关反向联动：退出=不等直接开战，打完=等路人凑满
        wait = not self.config.master_disciple.master_disciple_config.master_exp_exit_after_prepare
        self._run_battle_with_invite(
            zones_name='经验妖怪',
            battle_count=2,
            buff_open_func=open_buff,
            buff_close_func=close_buff,
            battle_wait_func=self._exp_youkai_battle_wait,
            room_type=RoomType.NORMAL_5,
            wait_for_others=wait
        )

    def run_stone_ju_as_disciple(self):
        """
        徒弟模式 - 石距（周一到周五2次，周六周日1次，邀请师父，3人房；超时退出）
        战斗结算：检测 I_WIN 或 I_REWARD，再点击领奖（参照Tako）
        """
        logger.info("Running stone ju (tako) as disciple")

        # 石距每周五六是愤怒的石距
        if 5 <= self.start_time.weekday() <= 6:
            zones_name = '愤怒的石距'
            battle_count = 1
        else:
            zones_name = '石距'
            battle_count = 2

        def open_buff():
            self.open_buff()
            self.exp_50()
            self.exp_100()
            self.close_buff()

        def close_buff():
            self.open_buff()
            self.exp_50(False)
            self.exp_100(False)
            self.close_buff()

        self._run_battle_with_invite(
            zones_name=zones_name,
            battle_count=battle_count,
            buff_open_func=open_buff,
            buff_close_func=close_buff,
            battle_wait_func=self._tako_battle_wait,
            room_type=RoomType.NORMAL_3
        )

    # ======================== 守护历练 Guard ========================

    def _guard_goto_team(self) -> bool:
        """
        从庭院导航到守护历练组队房间
        流程：庭院 → 旅途中 → 任务页 → 找到守护历练 → 进入组队
        :return: 是否成功进入组队房间
        """
        logger.info("Guard: navigating to team room")

        # 导航到旅途中
        self.screenshot()
        self.ui_get_current_page()
        self.ui_goto(page_main)
        self.ui_goto(page_youki)

        # 等待任务页面出现
        """ while 1:
            self.screenshot()
            if self.appear(self.I_PAGE_TASK):
                logger.info('Guard: task page appeared')
                break
            if self.appear_then_click(self.I_TO_TASK, interval=1):
                continue """

        # 在任务列表中找到"守护历练"并点击
        """ start_time = time.time()
        swipe_count = 0
        while time.time() - start_time < 30:
            self.screenshot()
            if self.appear(self.I_PAGE_BATTLE_GUARD):
                break
            roi = list(self.O_FLAG_TASK_GUARD.ocr(self.device.image))
            if roi != [0, 0, 0, 0]:
                roi[2] = 424
                roi[3] = 77
                self.I_TO_BATTLE_GUARD.roi_back = roi
                if self.appear_then_click(self.I_TO_BATTLE_GUARD, interval=1):
                    start_time = time.time()
                    continue
            if time.time() - start_time > (5 + swipe_count * 2):
                self.swipe(self.S_FIND_TASK_GUARD, 2)
                sleep(1)
                swipe_count += 1

        if not self.appear(self.I_PAGE_BATTLE_GUARD):
            logger.warning('Guard: failed to find battle guard page')
            return False """

        start_time = time.time()
        to_team_click_cnt = 0
        while time.time() - start_time < 15:
            self.screenshot()
            if self.appear(self.I_PAGE_TEAM):
                logger.info('Guard: entered room')
                return True
            if self.appear_then_click(self.I_TO_TEAM, interval=1):
                to_team_click_cnt += 1
                if to_team_click_cnt >=5:
                    logger.warning('Guard: clicked I_TO_TEAM over 5 times, guard daily limit may be exhausted')
                    raise  TaskEnd('Guard daily limit may be exhausted')
                start_time = time.time()
                continue
            """ if self.appear(self.I_PAGE_BATTLE_GUARD) and not self.appear(self.I_TO_TEAM):
                start_time = time.time()
                logger.info('Guard: swiping to find team button')
                self.swipe(self.S_TO_BATTLE_SWIPE, 5)
                continue """

        logger.warning('Guard: failed to enter team room')
        return False

    def _guard_invite_friend_no_tab(self, name: str) -> bool:
        """
        Guard房间2人房邀请好友（没有好友/跨区标签的邀请界面）
        流程：点击+号 → 等待好友列表加载 → OCR搜索好友 → 选中 → 确定
        :param name: 好友名字
        :return: 是否邀请成功
        """
        logger.info(f'Guard: inviting friend [{name}]')

        # 点击+号邀请
        self._goto_invite()
        # 等待好友列表加载
        sleep(1.5)

        # OCR搜索并选中好友（guard邀请没有标签，直接在列表中搜索）
        is_select = False
        self.O_FRIEND_NAME_1.keyword = name
        self.O_FRIEND_NAME_2.keyword = name

        for _ in range(3):
            if is_select:
                break
            sleep(1)
            if self.detect_select(name):
                is_select = True
            sleep(1)
            if not is_select:
                if self.detect_select(name):
                    is_select = True

        # 点击确定
        logger.info('Guard: click invite ensure')
        while 1:
            self.screenshot()
            if not self.appear(self.I_INVITE_ENSURE):
                break
            if self.appear_then_click(self.I_INVITE_ENSURE):
                continue

        if not is_select:
            logger.warning(f'Guard: friend [{name}] not found')
            return False

        return True

    def _guard_battle_wait(self, random_click_swipt_enable: bool) -> bool:
        """
        Guard专用战斗等待：战斗胜利时邀请弹窗（I_GI_SURE）在I_WIN上层，
        必须先处理邀请弹窗才能点击I_WIN
        """
        self.device.stuck_record_add('BATTLE_STATUS_S')
        self.device.click_record_clear()
        logger.info("Guard: start battle process")
        win: bool = False

        # 阶段1：等待战斗结束（胜利画面 I_WIN/I_WIN_2/I_DE_WIN 三模板共判）
        while 1:
            self.screenshot()
            if self.win_appear(threshold=0.8):
                logger.info("Guard: battle result is win")
                win = True
                break
            if self.appear(self.I_FALSE, threshold=0.8):
                logger.info("Guard: battle result is false")
                win = False
                break
            if self.appear(self.I_REWARD, threshold=0.6):
                win = True
                break
            if self.appear(self.I_REWARD_GOLD, threshold=0.8):
                win = True
                break
            if random_click_swipt_enable:
                self.random_click_swipt()

        if not win:
            while 1:
                self.screenshot()
                if self.appear_then_click(self.I_FALSE, threshold=0.6):
                    continue
                if not self.appear(self.I_FALSE, threshold=0.6):
                    break
            return False

        # 阶段2：胜利 — 先处理邀请弹窗（在I_WIN上层），再点I_WIN
        logger.info("Guard: handling invite dialog on top of WIN")
        self.I_REWARD.roi_back=[0,0,1280,720]
        # 防提前break：邀请弹窗未完全弹出/奖励页尚未出现时，I_WIN、I_REWARD、I_GI_SURE
        # 会短暂同时不可见，需持续消失一段时间（idle）才认为已进入奖励阶段
        idle_timer = Timer(3)
        idle_timer.start()
        while 1:
            self.screenshot()
            # 优先处理邀请弹窗
            if self.appear(self.I_GI_SURE):
                idle_timer.reset()
                # 任务启动后第一次出现邀请弹窗：必须先勾选"默认邀请"（点击I_I_NO_DEFAULT），
                # 并确认勾选成功（发现I_I_DEFAULT）后，才允许点击确定（I_GI_SURE）
                if not self._guard_default_invite_checked:
                    if self.appear_then_click(self.I_I_NO_DEFAULT, interval=0.5):
                        continue
                    if self.appear(self.I_I_DEFAULT):
                        logger.info("Guard: default invite checked")
                        self._guard_default_invite_checked = True
                        continue
                    # 尚未确认勾选成功，不点击确定
                    continue
                if self.appear(self.I_I_NO_DEFAULT):
                    self.appear_then_click(self.I_I_NO_DEFAULT, interval=0.5)
                    continue
                if self.appear_then_click(self.I_GI_SURE, interval=0.5):
                    continue
            # 邀请弹窗消失后，点击胜利：全屏减去常驻禁点区域（与奖励页共用安全区域）；
            # 结算场景按概率连点（双击/三击），见 settlement_click。
            # 胜利画面 I_WIN/I_WIN_2 共判（I_DE_WIN 在阶段1已点掉）
            if self.win_appear(threshold=0.8):
                action_click = weighted_choice(self.reward_click_actions())
                self.settlement_click(self.I_WIN, action_click, interval=0.5) or \
                    self.settlement_click(self.I_WIN_2, action_click, interval=0.5)
                sleep(2)
                # 点掉胜利后重新计空闲，给奖励页留足出现时间，避免过渡期提前break
                idle_timer.reset()
                continue
            if self.appear_multi_scale(self.I_REWARD):
                idle_timer.reset()
                self.ui_click_until_smt_disappear(self.I_REWARD, self.I_REWARD, interval=1.5)
                continue
            if self.appear_multi_scale(self.I_REWARD_GOLD):
                idle_timer.reset()
                self.ui_click_until_smt_disappear(self.I_REWARD_GOLD, self.I_REWARD_GOLD, interval=1.5)
                continue
            # I_WIN和邀请弹窗持续消失一段时间，才进入奖励阶段
            if idle_timer.reached():
                logger.info("Guard: win and invite dialog both gone, entering reward stage")
                break
                

        return True

    def _guard_run_battle(self, config: GeneralBattleConfig):
        """
        Guard房间战斗流程：使用自定义_guard_battle_wait处理邀请弹窗
        """
        logger.hr("Guard battle start", 2)
        self.current_count += 1
        logger.info(f"Current count: {self.current_count}")
        self.battle_before(None, config)
        if self.is_in_battle(False):
            self.green_mark(config.green_enable, config.green_mark)
        win = self._guard_battle_wait(config.random_click_swipt_enable)
        return win

    def run_guard_as_disciple(self):
        """
        徒弟模式 - 守护历练任务
        流程：
        1. 使用_create_room_and_invite导航到守护历练组队页面并邀请师父
        2. 等待师父进入 → 开战
        3. 战斗胜利 → 自动处理邀请弹窗 → 等师父再进入 → 循环
        """
        logger.info("Running guard as disciple")
        # 每次启动任务重置守卫标志：第一次战斗必须确认勾选"默认邀请"
        self._guard_default_invite_checked = False

        guard_count = self.config.master_disciple.master_disciple_config.guard_battle_count
        master_name = self.config.master_disciple.master_disciple_config.master_name
        invite_timeout = self.config.master_disciple.master_disciple_config.invite_timeout

        # 使用通用函数导航并创建房间、邀请师父
        # Guard的特殊导航通过navigate_and_create_func传入
        if not self._create_room_and_invite(
            task_name='守护历练',
            room_type=RoomType.NORMAL_2,
            navigate_and_create_func=self._guard_goto_team
        ):
            logger.warning("Guard: failed to create room and invite master")
            return

        # 确认房间类型（2人房）
        self.room_type = RoomType.NORMAL_2
        logger.info('Guard: room type is NORMAL_2')

        # 初始化战斗配置
        battle_config = GeneralBattleConfig(lock_team_enable=True)

        count = 0

        while count < guard_count:
            # 等待师父进入房间（同步信号：师父进房次数大于上次开战时的已消费值）
            self.device.stuck_record_clear()
            self.device.stuck_record_add('BATTLE_STATUS_S')
            self.screenshot()
            wait_timer = Timer(invite_timeout)
            wait_timer.start()
            reinvite_timer = Timer(15)
            reinvite_timer.start()

            while 1:
                self.screenshot()
                if not self.is_in_room():
                    continue

                # 【同步】师父进房信号（取代2人房加号归零检测）：读的是「师父此刻
                # 在不在房间里」这个状态——第 2+ 场的邀请由游戏在上一场战斗结算时
                # 自动发出，师父何时进房不受脚本控制，问状态就不必对时序做假设
                if self._md_master_in_room():
                    break

                if wait_timer.reached():
                    logger.warning(f'Guard: master did not enter within {invite_timeout}s')
                    raise Exception("wait timeout")

                if reinvite_timer.reached():
                    logger.info('Guard: re-inviting master')
                    reinvite_timer.reset()
                    self._invite_by_room_type(master_name,RoomType.NORMAL_2)
                # 等待期间保持同步会话心跳
                self._md_heartbeat_disciple_if_due()

            # 师父已进入，点击挑战
            self.click_fire()
            count += 1
            # _guard_run_battle 内部在战斗胜利时会自动处理邀请弹窗并点击确认
            # 确认后师父会自动收到邀请，下一轮等师父进入即可
            win = self._guard_run_battle(battle_config)
            if  self.appear(self.I_TO_TEAM):
                start_time = time.time()
                while time.time()-start_time<5:
                    self.screenshot()
                    if self.appear(PlotlineAssets.I_PAGE_MAIN):
                        raise TaskEnd ("Guard: returned to main page, ending task")
                    if self.appear_then_click(self.I_BACK_YELLOW, interval=1):
                        continue
                self.screenshot()
                self.ui_get_current_page()
                self.ui_goto(page_main) 
                raise TaskEnd ("Guard: returned to main page, ending task")
            if not win:
                logger.warning('Guard: battle failed')
                break

        # 退出房间
        if self.exit_room():
            pass
        # 退出组队界面
        if self.exit_team():
            pass
        start_time=time.time()
        while time.time()>start_time-5:
            self.screenshot()
            if self.appear(self.I_CHECK_MAIN):
                break
            if self.appear_then_click(self.I_BACK_YELLOW, interval=1):
                start_time = time.time()
                continue
        self.screenshot()
        if self.ui_get_current_page()!=page_main:
            self.ui_goto(page_main) 
        logger.info(f'Guard: completed {count} battles')
        raise TaskEnd ("Guard: completed")

    def run_exploration_as_disciple(self):
        """
        徒弟模式 - 探索任务
        参照Plotline流程：自动寻找最高章节 → 执行15次战斗 → 切换援助式神 → 锁定队伍
        配置不暴露给用户，全部在代码中初始化
        正常结束（含15分钟超时退出）后导航好友协战页截图存证
        """
        logger.info("Running exploration as disciple")

        # 创建SoloExploration实例
        solo_exploration = SoloExploration(self.config, self.device)

        solo_exploration.config.model.exploration.exploration_config.exploration_level = ExplorationLevel.AUTO
        logger.info("Set exploration chapter to: AUTO")

        # 在代码中初始化探索配置，不暴露给用户
        solo_exploration._config.general_battle_config.lock_team_enable = False
        solo_exploration._config.exploration_config.minions_cnt =14
        solo_exploration._config.exploration_config.limit_time = dtime(0, 15, 0)  # 15分钟上限兜底
        solo_exploration._config.exploration_config.up_type = UpType.ALL
        solo_exploration._config.scrolls.scrolls_enable = False

        solo_exploration.ui_get_current_page()
        solo_exploration.ui_goto(page_exploration)

        # 切换援助式神相关标记
        self.help_shikigami_detect = True

        # 临时替换battle_wait和battle_before方法
        original_battle_wait = solo_exploration.battle_wait
        original_battle_before = solo_exploration.battle_before

        def battle_wait_with_heartbeat(random_click_swipt_enable: bool) -> bool:
            # 探索是长流程单人环节：每场战斗刷一次同步心跳，师父侧凭它确认徒弟仍在推进
            self._md_heartbeat_disciple_if_due()
            return self.battle_wait(random_click_swipt_enable)

        solo_exploration.battle_wait = battle_wait_with_heartbeat
        solo_exploration.battle_before = self._disciple_exploration_battle_before

        try:
            solo_exploration.run_solo()
        except Exception as e:
            logger.error(f"Exploration task error: {e}")
            self.config.notifier.push(content=f'探索任务异常: {e}', title='MasterDisciple')
        else:
            # 探索正常结束（含超时退出）：导航好友协战次数页截图存证，与同心协战一致
            self._save_exploration_evidence()
            run_optional_ability('post_task_hook', self)
        finally:
            # 恢复原始方法
            solo_exploration.battle_wait = original_battle_wait
            solo_exploration.battle_before = original_battle_before

    def _save_exploration_evidence(self):
        """
        探索任务完成后截图存证：导航到好友协战次数页（与同心协战的存证方式一致，
        参照 tasks/DailyAltAcc/alliedteam.py 的 return_to_main），
        截图保存到 screenshots/Battle_Screenshots_<年_月_日>/<角色名>.png，同天同角色覆盖。
        整体异常只记日志不上抛，避免存证失败触发探索整任务重试。
        """
        try:
            # 从探索章节入口页回到庭院，再进好友页
            self.screenshot()
            self.ui_get_current_page()
            self.ui_goto(page_main)
            # 次数未显示时回庭院重进，含首次共三次，最后一次仍失败也保存现场。
            image = capture_friend_help_image(self)
            now = datetime.now()
            # 角色名：切号跑徒弟时用徒弟角色名，未切号退化为配置实例名
            char_name = self._current_disciple_name or self.config.config_name
            # 替换 Windows 文件名非法字符，避免保存失败
            char_name = re.sub(r'[\\/:*?"<>|]', '_', str(char_name))
            save_dir = Path(f'screenshots/Battle_Screenshots_{now.year}_{now.month:02d}_{now.day:02d}')
            save_dir.mkdir(parents=True, exist_ok=True)
            # 同一角色同一天重复运行时直接覆盖，只保留最新一张
            save_path = save_dir / f'{char_name}.png'
            # 保存 OCR 校验的同一帧，避免另取未经检查的画面。
            save_image(image, str(save_path))
            logger.info(f'探索任务完成截图已保存: {save_path}')
            # 退出好友协战页回庭院（最多等5秒，期间点一次红色返回）
            exit_timer = Timer(5)
            exit_timer.start()
            while 1:
                self.screenshot()
                if exit_timer.reached():
                    break
                if self.appear_then_click(self.I_UI_BACK_RED, interval=1):
                    break
            self.screenshot()
            if self.ui_get_current_page() != page_main:
                self.ui_goto(page_main)
        except Exception as e:
            logger.warning(f'探索任务截图存证失败: {e}')

    def _locate_help_shikigami(self) -> list:
        """OCR 定位援助式神卡上的协战次数标签（N/15），返回其屏幕 roi。

        原 O_FIND_SHIKIGAMI_HELP（keyword="15"）在准备界面上并不存在 "15"
        整串文本，实际一直靠 FULL 模式的「keyword 任一单字命中」降级匹配到
        式神卡旁的 "N/15" 标签——位置碰巧正确所以平时能用。但候选列表里
        没有该标签时（09-01 师徒日志实测出现过两帧缺失），单字降级会误中
        "9999991" 等垃圾串，锚点偏移导致援助式神未上场、好友协战不计数
        （同心场景 09-01 角色A / 09-06 角色B 两次事故同因）。
        这里改为直接遍历 OCR 候选，严格按 \\d+/15 匹配协战标签（坐标换算
        与 Full.ocr_full 保持一致），匹配不到返回 [0,0,0,0] 交给调用方
        按锚点丢失处理，不再有降级误匹配面。与 DailyAltAcc/alliedteam.py
        的同名方法同源同步。
        """
        ocr_obj = self.O_FIND_SHIKIGAMI_HELP
        try:
            boxed_results = ocr_obj.detect_and_ocr(self.device.image)
        except Exception:
            logger.exception('援助式神锚点 OCR 失败，按未识别处理')
            return [0, 0, 0, 0]
        for result in boxed_results:
            if re.search(r'\d+/15', result.ocr_text):
                # detect_and_ocr 的 box 坐标相对 roi 裁剪图，需加回 roi 偏移
                box = result.box
                return [box[0][0] + ocr_obj.roi[0], box[0][1] + ocr_obj.roi[1],
                        box[1][0] - box[0][0], box[2][1] - box[0][1]]
        return [0, 0, 0, 0]

    def _disciple_exploration_battle_before(self, buff: BuffClass | list[BuffClass],
                                              config: GeneralBattleConfig, timeout: float = 10) -> bool:
        """
        徒弟探索战斗前：切换援助式神 → 锁定队伍 → 准备
        参照Plotline的battle_before实现
        """
        timeout_timer = Timer(timeout).start()
        # 每场战斗重置锚点丢失计数：重试上限只约束本场，超限降级后下一场从头计
        self._help_anchor_miss = 0
        confed = False
        while not timeout_timer.reached():
            self.screenshot()
            if self.is_in_real_battle(False) :
                return True
            if self.appear_then_click(self.I_DISABLE_7DAYS_DIFF_SOUL, interval=0.6):
                continue
            if self.appear_then_click(self.I_CONFIRM_CLOSE_DIFF_SOUL, interval=0.6):
                continue
            if self.is_in_prepare(False):
                timeout_timer.reset()
                # 切换援助式神逻辑（参照Plotline）
                if self.help_shikigami_detect:
                    if not self.appear(PlotlineAssets.I_FLAG_CHANGE):
                        # 未展开助战列表，先点击切换按钮
                        self.click(PlotlineAssets.C_CLICK_CHANGE)
                        sleep(2)
                        continue
                    # 已展开助战列表，OCR 定位助战位并确保援助式神上场
                    self.screenshot()
                    roi = self._locate_help_shikigami()
                    if roi == [0, 0, 0, 0]:
                        # 锚点丢失：先复验上场旗标——滑动后标签可能消失但式神已上场
                        if self.appear(PlotlineAssets.I_FLAG_ON_FIELD):
                            self.help_shikigami_detect = False
                        else:
                            # 告警 + 场内重试；超限后本场放弃切换直接开打（标记
                            # 保持 True，下一场继续尝试，一次失败不葬送后续场次）
                            self._help_anchor_miss += 1
                            logger.warning(f'援助式神锚点(N/15标签)未识别到，'
                                           f'第 {self._help_anchor_miss} 次')
                            if self._help_anchor_miss < self.HELP_ANCHOR_RETRY_LIMIT:
                                sleep(1)
                                continue
                            logger.warning(f'连续 {self._help_anchor_miss} 次未识别到锚点，'
                                           f'本场放弃切换直接开打（本场可能不计好友协战），下一场将重试')
                            # 不 continue，落到下方点准备，避免在准备界面死循环
                    else:
                        self._help_anchor_miss = 0
                        PlotlineAssets.I_FLAG_ON_FIELD.roi_back = (
                            roi[0] + roi[2] - 81, roi[1] + roi[3] - 160, 130, 160
                        )
                        logger.info(f"I_FLAG_ON_FIELD.roi_back ={PlotlineAssets.I_FLAG_ON_FIELD.roi_back}")
                        if not self.appear(PlotlineAssets.I_FLAG_ON_FIELD):
                            # 援助式神未上场，滑动将其拖入出战位
                            PlotlineAssets.S_SWIPE_SHIKIGAMI.roi_front = (
                                roi[0], roi[1],
                                roi[2], roi[3]
                            )
                            self.swipe(PlotlineAssets.S_SWIPE_SHIKIGAMI, 4)
                            sleep(2)
                            continue
                        # 援助式神确认在场：清除切换标记，后续场次直接点准备
                        self.help_shikigami_detect = False
                # 点击准备
                if self.appear_then_click(self.I_PREPARE_HIGHLIGHT, interval=0.8):
                    continue
                continue

            logger.info('Wait for preparation page')
            sleep(random.uniform(0.4, 0.8))
        return False


    # ======================== 师父模式 ========================

    def run_as_master(self):
        """
        以师父身份运行（同步模式）：
        1. 先一次性切换三组御魂预设（类似SixRealms）
        2. 再与徒弟实例完成 JSON 配对（超时则本轮结束，按失败调度重试）
        3. 跟随徒弟下发的任务序列：提前开加成 → 回报就绪 → 等邀请 → 战斗 → 循环
        """
        logger.info("Running as master")

        try:
            # 同步准备与配对：御魂切换在配对前完成（见 _md_setup_master）
            if not self._md_setup_master():
                return False

            # 确保在庭院等待
            self.screenshot()
            self.ui_get_current_page()
            self.ui_goto(page_main)

            # 进入跟随循环
            self.master_battle_flow()

        except GameNotRunningError:
            raise
        except RequestHumanTakeover:
            raise
        except TaskEnd:
            raise
        except Exception as e:
            logger.error(f"师父模式执行异常: {e}")
            self.config.notifier.push(
                content=f"师父模式任务异常\nError: {e}",
                title="师父模式任务失败"
            )
            return False
        finally:
            # 收尾兜底：关掉可能还开着的加成（跟随循环内部已关，这里覆盖异常路径）；
            # 游戏异常时关不掉也不能掩盖原异常
            try:
                if self.coin_buff:
                    self._master_close_coin_buff()
                if self.exp_buff_on:
                    self._master_close_exp_buff()
            except Exception:
                pass

    def _master_switch_presets(self):
        """
        任务开始前，去式神录一次性切换三组御魂预设
        类似SixRealms的switch_soul模式，依次切换1→2→3组
        """
        config = self.config.master_disciple
        switch_targets = []
        for preset_cfg in [config.master_preset_1, config.master_preset_2, config.master_preset_3]:
            if preset_cfg.enable:
                switch_targets.append((preset_cfg.preset_group, preset_cfg.preset_team))
                logger.info(f"Master preset enabled: group={preset_cfg.preset_group}, team={preset_cfg.preset_team}")

        if not switch_targets:
            logger.info("No master preset enabled, skip switching")
            return

        logger.info(f"Master switching {len(switch_targets)} preset(s) before battle")

        # 导航到式神录
        self.screenshot()
        self.ui_get_current_page()
        self.ui_goto(page_shikigami_records)

        # 依次切换每组预设
        self.run_switch_soul(switch_targets)

        logger.info("Master preset switching completed")

    def _md_setup_master(self) -> bool:
        """师父侧准备与配对：先一次性切御魂预设，再循环尝试加入徒弟的新鲜会话。

        御魂切换（三组预设约 2-3 分钟）发生在配对之前；徒弟侧的配对等待
        是 300 秒，覆盖这段时间还有约两分钟余量，两边同时启动也不会错过
        配对窗口。
        :return: 是否配对成功
        """
        # 先切御魂预设：不依赖徒弟，切完再去找会话（徒弟此时已在等待配对）
        self._master_switch_presets()
        # 状态文件按师父实例名（自己）命名，与徒弟配置的 master_instance 对应
        store = MasterDiscipleStateStore(self.config.config_name)
        self._md_last_heartbeat = time.monotonic()
        # 加入窗口 300 秒：切完预设后等待徒弟发布的会话（徒弟发布后即去切号/
        # 买体力，两边单人环节并行进行，会话在任务一开始就已存在）
        wait_join = Timer(300).start()
        sweep_count = 0
        while not wait_join.reached():
            state = store.try_join(self.config.config_name)
            if state is not None:
                self._md_store = store
                self._md_session = MasterDiscipleSession.from_state(state)
                self._md_paired = True
                logger.info(f'已加入徒弟 [{state.get("disciple_instance")}] 的同步会话')
                return True
            # 每约30秒清一次弹窗（拒绝好友邀请/关确认弹窗），防长等待期间堆积遮挡；
            # 同时给设备层的空闲看门狗续期（两侧的配对等待都是 300 秒）
            sweep_count += 1
            if sweep_count % 30 == 0:
                self._md_idle_popup_sweep()
            self._md_keepalive_stuck_detection()
            sleep(1)
        logger.warning('未发现徒弟发布的同步会话，师父本轮退出')
        self.config.notifier.push(
            content='师父实例300秒内未发现徒弟发布的同步会话，本轮按失败调度重试',
            title='师徒配对超时')
        return False

    def _md_mark_master_in_room(self) -> None:
        """师父确认进房后置位「我在房间里」，徒弟据此开战。

        会话失效只记日志：徒弟那边会等到超时并重邀请，不必中断师父的跟随循环。
        """
        if self._md_store is None or self._md_session is None:
            return
        try:
            self._md_store.mark_master_in_room(self._md_session)
        except StaleSessionError:
            pass

    def _md_clear_master_in_room(self) -> None:
        """师父离开房间（开战/房间销毁/超时退出）时复位状态位。

        必须在离开房间的每个出口都调用：漏掉一次标志就停在 True，
        徒弟下一场会看到陈旧的 True 而提前开战，把还没进房的师父打断。
        """
        if self._md_store is None or self._md_session is None:
            return
        try:
            self._md_store.clear_master_in_room(self._md_session)
        except StaleSessionError:
            # 会话已失效：本轮跟随即将结束，标志随徒弟发布的新会话重建
            pass

    def _md_disciple_alive(self) -> bool:
        """徒弟是否还在推进（心跳新鲜）。

        单人环节（切号/买体力/探索）耗时不可预知，固定超时只能靠猜——本次
        真机故障就是 1800 秒档位太钝。改看心跳：徒弟在这些环节里会持续刷新
        disciple_seen_at，停了才说明它真走了。
        读不到状态时按「还活着」处理：师父宁可多等，也不要因为一次读取失败
        就把整轮任务判失败（失败调度会把它推到下一个调度周期）。
        """
        if self._md_store is None or self._md_session is None:
            return True
        try:
            state = self._md_store.read()
        except Exception:
            return True
        return disciple_alive(state)

    def _md_keepalive_stuck_detection(self) -> None:
        """跟随等待期给设备层的卡死检测续期（30 秒节流）。

        设备层规则（module/device/device.py:372）：detect_record 里有
        BATTLE_STATUS_S 时给 300 秒窗口，集合为空只给 60 秒，到点就抛
        GameStuckError。师父在庭院等徒弟的下一个动作按设计最长可等 30 分钟，
        远超这个窗口，因此必须周期性重置计时器——师徒之间的「对方还在不在」
        由 wait_out 业务档位负责，不该由设备层的空闲看门狗来判。
        师父是跟随方，等待期挂着「等一场战斗」的标记是准确的语义。
        """
        now = time.monotonic()
        if now - self._md_last_keepalive < 30:
            return
        self.device.stuck_record_clear()
        self.device.stuck_record_add('BATTLE_STATUS_S')
        self._md_last_keepalive = now

    def _md_heartbeat_master_if_due(self) -> None:
        """师父侧心跳（5 秒节流）：跟随期间刷新 master_seen_at。"""
        if self._md_store is None or self._md_session is None:
            return
        now = time.monotonic()
        if now - self._md_last_heartbeat < 5:
            return
        try:
            self._md_store.heartbeat(self._md_session, 'master')
        except StaleSessionError:
            # 会话失效由跟随循环每轮的状态校验处理，心跳失败不中断任务
            pass
        self._md_last_heartbeat = now

    def _master_open_coin_buff(self) -> None:
        """提前开金币加成（50%+100%）：徒弟下发金币「打完」指令时调用。

        取代旧的「收到邀请先不点、去开加成、等徒弟15秒重发邀请再接受」串行流程。
        """
        self.open_buff()
        self.gold_50(True)
        self.gold_100(True)
        self.close_buff()
        self.coin_buff = True

    def _master_close_coin_buff(self) -> None:
        """关金币加成：离开金币任务（切任务/序列结束/异常收尾）时调用。"""
        self.open_buff()
        self.gold_50(False)
        self.gold_100(False)
        self.close_buff()
        self.coin_buff = False

    def _master_open_exp_buff(self) -> None:
        """提前开经验加成（50%+100%）：徒弟下发经验「打完」指令时调用。"""
        self.open_buff()
        self.exp_50(True)
        self.exp_100(True)
        self.close_buff()
        self.exp_buff_on = True

    def _master_close_exp_buff(self) -> None:
        """关经验加成：离开经验任务（切任务/序列结束/异常收尾）时调用。"""
        self.open_buff()
        self.exp_50(False)
        self.exp_100(False)
        self.close_buff()
        self.exp_buff_on = False

    def _md_current_task_changed(self, task: str) -> bool:
        """徒弟下发的任务是否已不是 task（读不到状态时按「未变」处理）。"""
        if self._md_store is None or self._md_session is None:
            return False
        try:
            state = self._md_store.read()
        except Exception:
            return False
        if MasterDiscipleSession.from_state(state) != self._md_session:
            return False
        return str(state.get('current_task') or '') != str(task)

    def _master_check_and_accept(self, task: str, keyword: str) -> bool:
        """检测并接受徒弟的邀请弹窗（OCR 文字与当前任务匹配才接受）。

        同步模式下任务类型已知，OCR 只做弹窗身份校验：防止接受到非徒弟
        （其他好友/陌生人）发来的邀请。
        本函数必须快速收场：师父是跟随方，一旦卡在「接受邀请 → 进不去房间」
        里不动，就会停止处理徒弟下发的下一个任务（不开加成、不回报就绪），
        徒弟那边只能干等到超时。因此三条早退路径——徒弟已切任务、接受后
        迟迟进不去、弹窗文字对不上——都会立刻收场返回 False，把控制权交还
        给跟随循环。
        :param task: 当前同步任务类型（日志用）
        :param keyword: 任务关键词（守护/石距/金币/经验）
        :return: True 已接受并确认进入房间
        """
        if not self.appear(self.I_ACCEPT):
            return False
        logger.info('Click accept')
        start_time = time.time()
        accept_clicked_at = 0.0
        while time.time() - start_time < 30:
            self.screenshot()
            if self.is_in_room():
                return True
            # 被秒开
            # https://github.com/runhey/OnmyojiAutoScript/issues/230
            if self.appear(self.I_EXIT):
                return False
            # 徒弟已经下发下一个任务：这次接受已经没有意义（多半是上一轮的
            # 过期邀请），必须立刻回去跟随新任务，不能把切任务的窗口耗光
            if self._md_current_task_changed(task):
                logger.warning(f'接受 [{task}] 邀请期间徒弟已切换任务，放弃本次接受')
                self._dismiss_accept_dialog()
                return False
            if self.appear(self.I_ACCEPT, interval=1):
                # OCR 校验弹窗文字与当前任务匹配（房间标题带任务名）
                self.O_ACCEPT_NAME.roi = [self.I_ACCEPT.roi_front[0] + 167, self.I_ACCEPT.roi_front[1] + 25, 180, 47]
                text = self.O_ACCEPT_NAME.ocr(self.device.image)
                logger.info(f"accept text={text}")
                if keyword not in text:
                    logger.warning(f'邀请弹窗文字 [{text}] 与当前任务 [{task}] 不匹配，拒绝该邀请')
                    self._dismiss_accept_dialog()
                    return False
                self.click(self.I_I_ACCEPT, interval=2)
                accept_clicked_at = time.time()
                continue
            # 点过接受、弹窗也消失了，却迟迟没有进房：这次邀请已经失效
            # （过期 / 房间已解散），接受动作发出去也没用，同样立刻收场。
            # 8 秒是给正常进房加载留的余量（实测 1.2~1.5 秒进房）
            if accept_clicked_at and time.time() - accept_clicked_at > 8:
                logger.warning(f'接受 [{task}] 邀请后未进入房间，返回跟随')
                self._dismiss_accept_dialog()
                return False
        return False

    def _dismiss_accept_dialog(self) -> None:
        """点掉还挂着的邀请弹窗（拒绝按钮在接受按钮同一行的左侧）。

        模态弹窗不关掉就一直遮挡画面：师父既进不了房也回不到庭院跟随，
        每轮截图都在同一个弹窗上重复决策。
        """
        for _ in range(5):
            self.screenshot()
            if not self.appear(self.I_ACCEPT):
                return
            if not self.appear_then_click(self.I_I_REJECT, interval=1):
                return

    def master_battle_flow(self):
        """
        师父的战斗流程（同步模式）：
        跟随徒弟下发的任务序列：任务变化时按指令提前开/关加成并回报就绪，
        庭院轮询邀请弹窗（OCR 文字与当前任务匹配才接受），进房后回报邀请
        序号，等徒弟开战，按同步指令选择战斗方式。
        退出时机：徒弟走到探索（单人任务，无需师父陪跑）→ 立即结束释放实例；
        徒弟回报 FINISHED → 正常退出；会话失效/等待超时 → 退出本轮。
        参照Orochi的run_member模式实现；配置不暴露给用户，在代码中初始化
        """
        logger.info("Master battle flow started, following disciple's task sequence")
        # 房间任务的弹窗关键词：OCR 文字与当前任务匹配才接受（防误接受他人邀请）
        task_keywords = {
            TASK_GUARD: '守护',
            TASK_STONE: '石距',
            TASK_COIN: '金币',
            TASK_EXP: '经验',
        }
        # 已回报就绪的任务（变化检测用）
        handled_task = None
        # 当前等待档位的超时计时器（任务变化时按档位重置）
        wait_out: Timer = None

        def reset_wait_out(task: str) -> None:
            nonlocal wait_out
            # 房间任务用短档：徒弟此时应当在主动邀请，240 秒等不到就是异常。
            # 其余状态（空任务/切号/买体力等单人环节）不设固定超时——那些环节
            # 耗时不可预知，改由徒弟心跳判断它是否还在推进（见 _md_disciple_alive）
            wait_out = Timer(240).start() if task in task_keywords else None

        # 挂 BATTLE_STATUS_S：师父是跟随方，整个循环都在等徒弟把战斗开起来。
        # 它同时是设备层空闲看门狗的长窗口标记（挂着 300 秒 vs 空集 60 秒），
        # 等待期间由 _md_keepalive_stuck_detection 周期续期
        self.device.stuck_record_clear()
        self.device.stuck_record_add('BATTLE_STATUS_S')
        self._md_last_keepalive = time.monotonic()
        while 1:
            self.screenshot()
            # 读取共享状态：徒弟序列完成/会话失效/徒弟走到探索（单人）都结束跟随
            try:
                state = self._md_store.read()
            except Exception as e:
                logger.warning(f'读取师徒同步状态失败: {e}')
                break
            if state.get('phase') == PHASE_FINISHED:
                logger.info('徒弟任务序列已完成，师父正常退出')
                break
            if state.get('current_task') == TASK_EXPLORATION:
                # 探索是单人任务：走到这里说明所有房间任务已完成，
                # 师父立即结束任务释放实例，不陪跑整个探索过程
                logger.info('徒弟已进入探索（单人任务），师父任务完成')
                break
            if MasterDiscipleSession.from_state(state) != self._md_session:
                logger.warning('师徒同步会话已失效（徒弟重启了新一轮），师父本轮退出')
                break

            task = str(state.get('current_task') or '')
            buff_command = str(state.get('buff_command') or '')

            # 任务变化：按指令切换加成后回报就绪（徒弟等 ready 才发起邀请）
            if task != handled_task:
                # 离开金币/经验任务时关掉对应加成
                if self.coin_buff and task != TASK_COIN:
                    self._master_close_coin_buff()
                if self.exp_buff_on and task != TASK_EXP:
                    self._master_close_exp_buff()
                # 按指令提前开加成（「打完」变体才开；「退出」变体不开）
                if buff_command == BUFF_COIN and not self.coin_buff:
                    self._master_open_coin_buff()
                elif buff_command == BUFF_EXP and not self.exp_buff_on:
                    self._master_open_exp_buff()
                try:
                    # 就绪回报带任务校验：任务已被徒弟切换时写入被拒，
                    # 下一轮读到新任务会重新准备并回报（自然自愈）
                    self._md_store.mark_master_ready(self._md_session, task)
                    logger.info(f'已就绪当前任务 [{task or "等待中"}]')
                except StaleSessionError:
                    break
                handled_task = task
                reset_wait_out(task)
                continue

            # 房间任务：庭院轮询邀请弹窗，接受并确认进房后置位「师父在房间里」
            if task in task_keywords:
                if self._master_check_and_accept(task, task_keywords[task]):
                    # 置位供徒弟判断可以开战；离开房间的每个出口都要复位，
                    # 否则徒弟下一场会看到陈旧的 True 提前开战
                    self._md_mark_master_in_room()
                    # 已在房间：等待徒弟（队长）开战。等待时长按任务档位——
                    # 金币/经验的「打完」变体下，徒弟还要把房间公开等路人补位
                    # （_run_battle_with_invite 给的上限是 240 秒），师父必须等
                    # 得一样久，否则会先超时离房，徒弟随后开战时房里已没有师父
                    self.device.stuck_record_clear()
                    self.device.stuck_record_add('BATTLE_STATUS_S')
                    wait_minutes = 4 if buff_command in (BUFF_COIN, BUFF_EXP) else 2
                    try:
                        battle_started = self.wait_battle(wait_time=dtime(minute=wait_minutes))
                    finally:
                        # 一离开候战房间就清位，不能等战斗/结算返回后再清；
                        # 否则师父结算较慢或异常时，徒弟会拿上一轮的 True 提前开战。
                        self._md_clear_master_in_room()
                    if battle_started:
                        # 按同步指令选择战斗方式（类型不再依赖 OCR 识别）
                        if task == TASK_GUARD:
                            # 守护历练：始终正常完成战斗
                            self.run_general_battle(config=GeneralBattleConfig())
                        elif task == TASK_COIN:
                            if buff_command == BUFF_COIN_EXIT:
                                # 金币场指令=准备后退出
                                self.master_run_battle_back(config=GeneralBattleConfig())
                            else:
                                # 金币场指令=正常打完
                                battle_config = GeneralBattleConfig(lock_team_enable=True)
                                self.battle_before(buff=None, config=battle_config)
                                self._gold_youkai_battle_wait()
                        elif task == TASK_EXP:
                            if buff_command == BUFF_EXP_EXIT:
                                # 经验场指令=等击杀数达标后退出
                                self.master_run_exp_battle_back(config=GeneralBattleConfig())
                            else:
                                # 经验场指令=正常打完
                                battle_config = GeneralBattleConfig(lock_team_enable=True)
                                self.battle_before(buff=None, config=battle_config)
                                self._exp_youkai_battle_wait()
                        elif task == TASK_STONE:
                            # 石距：始终进入后退出
                            self.master_run_battle_back_stone(config=GeneralBattleConfig())
                    # 战斗处理完成或候战已结束，把「等下一场战斗」的标记重新挂上，
                    # 房间状态已在候战结束时复位；设备层给等待标记的窗口是
                    # 300 秒（空集只有 60 秒），随后由空闲续期接管
                    wait_out.reset()
                    self.device.stuck_record_clear()
                    self.device.stuck_record_add('BATTLE_STATUS_S')
                    sleep(2)
                    self.screenshot()
                    continue

                # 被徒弟秒开拉进战斗：接受流程被打断但战斗已经开始，直接接管，
                # 否则会落到回庭院检查在战斗页面报 Unknown page 异常退出。
                # 仅在真的接管了一场战斗（返回非 None）时才重置等待
                if self.check_take_over_battle(False, config=GeneralBattleConfig()) is not None:
                    # 被拉进战斗 = 已经不在房间等开战，复位状态位
                    self._md_clear_master_in_room()
                    wait_out.reset()
                    self.device.stuck_record_clear()
                    self.device.stuck_record_add('BATTLE_STATUS_S')
                    sleep(2)
                    self.screenshot()
                    continue

            # 不在房间也不在战斗：确保回到庭院
            if self.ui_get_current_page() != page_main:
                self.ui_get_current_page()
                self.ui_goto(page_main)
                continue

            # 等待超时（当前档位）：徒弟迟迟没有下一个动作，师父退出本轮
            # 等待超时（房间任务短档）：徒弟迟迟不发下一场邀请，师父退出本轮
            if wait_out is not None and wait_out.reached():
                logger.warning(f'等待徒弟下一个动作超时（当前任务 [{task}]），师父本轮退出')
                break

            # 单人环节不设固定超时，改看徒弟心跳：它还在推进就一直陪等，
            # 心跳停了（崩溃/被用户停掉）才退出释放实例
            if wait_out is None and not self._md_disciple_alive():
                logger.warning(f'徒弟心跳已停止（当前任务 [{task}]），师父本轮退出释放实例')
                break

            # 跟随期间保持同步会话心跳，并给设备层的空闲看门狗续期
            self._md_heartbeat_master_if_due()
            self._md_keepalive_stuck_detection()

        # 收尾：先撤掉「在房间里」标志（退出时机可能有徒弟还在等），
        # 再关掉可能还开着的加成，避免把加成状态带出任务
        self._md_clear_master_in_room()
        if self.coin_buff:
            self._master_close_coin_buff()
        if self.exp_buff_on:
            self._master_close_exp_buff()
        raise TaskEnd

    def master_run_exp_battle_back(self, config: GeneralBattleConfig = None, exit_four: bool = False) -> bool:
        """
        经验妖怪击杀数达到30后退出；失败视为退出成功，胜利沿用徒弟的判定与点击处理。
        :param config:
        :return:
        """
        # 先点击准备，后续补点与退出、结算共用循环，避免固定等待期间漏过胜利。
        self.wait_until_appear_then_click(self.I_PREPARE_HIGHLIGHT)
        logger.info(f"Click {self.I_PREPARE_HIGHLIGHT.name}")
        # OCR 计时从退出框出现开始；达标后锁定退出意图，后续只重试确认、不再读数。
        wait_ocr_timer = None
        exit_requested = False
        settlement_started = False
        while 1:
            self.screenshot()

            # 胜败结算优先于退出框：最后一波可能在 OCR 或确认点击期间直接打完。
            result = None
            for target in (self.I_DE_WIN, self.I_EXP_WIN, self.I_FALSE):
                if self.appear(target):
                    result = target
                    break
            if result is not None:
                settlement_started = True
                # 失败页表示主动退出成功；胜利使用徒弟经验流程的两个模板，
                # 同样点击到页面消失，两条路径都按本场成功处理。
                if self.appear_then_click(result, interval=1):
                    if result is self.I_FALSE:
                        logger.info("[经验妖怪] 已成功退出战斗，处理失败页")
                    else:
                        logger.info(f"[经验妖怪] 战斗胜利，按徒弟胜利逻辑处理: {result.name}")
                continue
            # 胜败页面消失后等待返回庭院或组队，再交回跟随流程接收下一次邀请。
            if self.appear(self.I_CHECK_MAIN) or self.appear(self.I_CHECK_TEAM):
                return True
            if settlement_started:
                continue

            # 先检查退出框，避免背后的返回按钮仍可匹配而反复点击；准备页只做补点。
            if not self.appear(self.I_EXIT_ENSURE):
                if self.appear(self.I_PREPARE_HIGHLIGHT):
                    # 补点受节流时也仍在准备页，不能因此落到返回按钮。
                    self.appear_then_click(self.I_PREPARE_HIGHLIGHT, interval=1)
                    continue
                self.appear_then_click(self.I_EXIT, interval=1.5)
                continue

            if wait_ocr_timer is None:
                wait_ocr_timer = Timer(120).start()
            if not exit_requested:
                # 保留识别超时后的退出兜底，但胜利页面始终由本轮开头优先接管。
                if wait_ocr_timer.reached():
                    logger.warning("[经验妖怪] 等待OCR数字达到30超时，确认退出战斗")
                    exit_requested = True
                else:
                    try:
                        value = self.O_KILL_CNT.ocr_digit(self.device.image)
                    except Exception as e:
                        logger.warning(f"[经验妖怪] O_KILL_CNT识别异常: {e}")
                        value = 0
                    logger.info(f"[经验妖怪] 击杀数量: {value}/30")
                    if value >= 30:
                        logger.info("[经验妖怪] 击杀数量已达到30，确认退出战斗")
                        exit_requested = True
            if exit_requested:
                self.appear_then_click(self.I_EXIT_ENSURE, interval=1.5)
            else:
                sleep(1)

    def master_run_battle_back_stone(self, config: GeneralBattleConfig = None, exit_four: bool = False) -> bool:
        """
        进入挑战然后直接返回
        :param config:
        :return:
        """
        # 如果没有锁定队伍那么在点击准备后才退出的,退四的话就直接退出
        #if not config.lock_team_enable and not exit_four:
        # 点击准备按钮
        self.wait_until_appear(self.I_PREPARE_HIGHLIGHT)
        # 点击返回
        while 1:
            self.screenshot()
            # 先查退出确认弹窗再点返回：弹窗弹出后 I_EXIT 在背后仍可匹配，
            # 单轮耗时>=interval 时旧顺序会每轮 continue 饿死 break（慢节奏死循环）
            if self.appear(self.I_EXIT_ENSURE):
                break
            if self.appear_then_click(self.I_EXIT, interval=1.5):
                continue
        logger.info(f"Click {self.I_EXIT.name}")

        # 点击返回确认
        while 1:
            self.screenshot()
            if self.appear(self.I_CHECK_MAIN):
                return True
            # 先查失败确认框再点返回确认：I_EXIT_ENSURE 弹出后 I_FALSE 在其后可同屏共存，
            # 同理先 break 再点，避免慢节奏下饿死 break
            if self.appear(self.I_FALSE):
                break
            if self.appear_then_click(self.I_EXIT_ENSURE, interval=1.5):
                continue
        logger.info(f"Click {self.I_EXIT_ENSURE.name}")

        # 点击失败确认
        self.wait_until_appear(self.I_FALSE)
        while 1:
            self.screenshot()
            if self.appear_then_click(self.I_FALSE, interval=1.5):
                continue
            if not self.appear(self.I_FALSE):
                break
        logger.info(f"Click {self.I_FALSE.name}")

        return True
    def master_run_battle_back(self, config: GeneralBattleConfig = None, exit_four: bool = False) -> bool:
        """
        进入挑战然后直接返回
        :param config:
        :return:
        """
        # 如果没有锁定队伍那么在点击准备后才退出的,退四的话就直接退出
        #if not config.lock_team_enable and not exit_four:
        # 点击准备按钮
        sleep(5)
        self.wait_until_appear_then_click(self.I_PREPARE_HIGHLIGHT)
        self.click(self.I_PREPARE_HIGHLIGHT)
        logger.info(f"Click {self.I_PREPARE_HIGHLIGHT.name}")
        # 点击返回
        while 1:
            self.screenshot()
            # 先查退出确认弹窗再点返回：弹窗弹出后 I_EXIT 在背后仍可匹配，
            # 单轮耗时>=interval 时旧顺序会每轮 continue 饿死 break（慢节奏死循环）
            if self.appear(self.I_EXIT_ENSURE):
                break
            if self.appear_then_click(self.I_EXIT, interval=1.5):
                continue
        logger.info(f"Click {self.I_EXIT.name}")

        # 点击返回确认
        while 1:
            self.screenshot()
            if self.appear(self.I_CHECK_MAIN):
                return True
            # 先查失败确认框再点返回确认：I_EXIT_ENSURE 弹出后 I_FALSE 在其后可同屏共存，
            # 同理先 break 再点，避免慢节奏下饿死 break
            if self.appear(self.I_FALSE):
                break
            if self.appear_then_click(self.I_EXIT_ENSURE, interval=1.5):
                continue
        logger.info(f"Click {self.I_EXIT_ENSURE.name}")

        # 点击失败确认
        self.wait_until_appear(self.I_FALSE)
        while 1:
            self.screenshot()
            if self.appear_then_click(self.I_FALSE, interval=1.5):
                continue
            if not self.appear(self.I_FALSE):
                break
        logger.info(f"Click {self.I_FALSE.name}")

        return True
if __name__ == "__main__":
    from module.config.config import Config
    from module.device.device import Device
    from tasks.Component.GeneralInvite.assets import GeneralInviteAssets as gia
    c = Config('oas3')
    d = Device(c)
    self = ScriptTask(c, d)
    self.screenshot()
    self.master_run_battle_back_stone(config=GeneralBattleConfig())
    #self.run()
    """ while 1:
        self.screenshot()
        if  self.appear(self.I_ENSURE_SWITCH):
            break
        self.appear_then_click(self.I_TO_SWITCH, interval=1)
    while 1:
        self.screenshot()
        if "所有人"in self.O_ADD_ALL.ocr(self.device.image):
            break
        if self.ui_click(self.I_SWITCH_ALL,stop=self.I_SWITCH_ALL_OVER, interval=1):
            if self.appear_then_click(self.I_ENSURE_SWITCH,interval=1):
                continue """
        
    """ click_add=self.I_CLICK_INVITE_ADD.match_all_any(self.device.image)
    logger.info (f"len(click_add{len(click_add)})") 
    self._goto_invite() """
    """ self.ui_goto(page_main)
    self.run() """
    """ roi=list(self.O_FIND_SHIKIGAMI_HELP.ocr(self.device.image))
    if not roi==[0,0,0,0]:
        PlotlineAssets.I_FLAG_ON_FIELD.roi_back = (
                                 roi[0] + roi[2]-81, roi[1] + roi[3]-160,130,160
                            )
        logger.info(f"I_FLAG_ON_FIELD.roi_back ={PlotlineAssets.I_FLAG_ON_FIELD.roi_back}")
        if not self.appear(PlotlineAssets.I_FLAG_ON_FIELD):
            PlotlineAssets.S_SWIPE_SHIKIGAMI.roi_front = (
                roi[0], roi[1], 
                roi[2], roi[3]
            )
            self.swipe(PlotlineAssets.S_SWIPE_SHIKIGAMI, 4)
            sleep(2) """

    #self.run()
        
    #self._guard_goto_team()