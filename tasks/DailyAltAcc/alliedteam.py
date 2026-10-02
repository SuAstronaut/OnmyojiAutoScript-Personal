# This Python file uses the following encoding: utf-8
import re
import time
import random
from datetime import datetime
from pathlib import Path
from time import sleep
from module.base.timer import Timer
from module.base.utils import save_image
from module.logger import logger
from module.exception import GamePageUnknownError
from tasks.GameUi.assets import GameUiAssets
from tasks.GameUi.page import page_main, page_team
from tasks.DailyAltAcc.utils import DailyAltAccBase
from tasks.Component.GeneralBattle.general_battle import GeneralBattle
from tasks.Component.GeneralBattle.reward_frame import AVOID_WIN_TEAM3
from tasks.Component.GeneralBattle.config_general_battle import GeneralBattleConfig
from tasks.Component.GeneralBuff.config_buff import BuffClass
from tasks.Component.GeneralRoom.general_room import GeneralRoom
# 同心与觉醒协战共用截图前校验，保证未加载时采用相同的庭院重试流程。
from tasks.Component.SwitchHelpShikigami.switch_help_shikigami import (
    build_help_screenshot_name, capture_friend_help_image, capture_completed_help_image,
    parse_help_count, sync_help_battle_target, HelpTargetReached,
)
from tasks.DailyAltAcc.stat_log import StatEvent
from tasks.Plotline.assets import PlotlineAssets
from tasks.MasterDisciple.assets import MasterDiscipleAssets
# 同心协战复用觉醒的购买体力弹窗素材，仅在协战分支处理体力受阻。
from tasks.EvoZone.assets import EvoZoneAssets


class Alliedteam(GeneralBattle, GeneralRoom, DailyAltAccBase):
    # 仅 alliedteam_limit_count==13 的账号启用「准备界面切换援助式神」战斗准备流程
    _need_switch_help_shikigami: bool = False
    # 援助式神切换标记：确认援助式神上场后置 False（后续场次直接点准备）。
    # 未确认上场前保持 True，下一场继续尝试——一次切换失败不再葬送全天场次
    _help_shikigami_detect: bool = True
    # 同一场战斗内援助式神锚点（N/15 标签）连续识别失败次数，每场战斗重置
    _help_anchor_miss: int = 0
    # 锚点识别失败的重试上限：一轮约 1.3s（OCR 0.3s + sleep 1s），3 次约 4s；
    # 超限后本场放弃切换直接开打（保持原有降级语义，不会卡死在准备界面）
    HELP_ANCHOR_RETRY_LIMIT: int = 3
    # 连续多少秒不在战斗画面才确认上一场结束（防 I_BATTLE_INFO 单帧抖动误计场）
    BATTLE_END_CONFIRM_S = 3
    # 仅 13 次协战启用动态目标；实际战斗场次仍由 current_count 和进度文件独立累计。
    _help_target_enabled = False
    _help_battle_limit = None
    _help_target_synced = False
    _help_target_complete = None
    # 体力不足后停止本轮补打，仍由收尾流程保存最终协战次数截图。
    _help_battle_blocked = False
    # 同心选关、组队和战斗期间保留集结；结束后其他任务导航可以自动退出旧队伍。
    _alliedteam_battle_active = False

    def reward_avoid(self) -> tuple:
        """同心协力是三人战斗，胜利画面上多出两块队友战绩框，落点回避它们。

        用回避区而非禁区：两块框横跨 x 449~1280 几乎铺满热区宽度，并入禁区会
        把安全块切成屏幕最底一条 114px，落点全压到底部（实测核心区命中率
        19% vs 两人为主实例的 60%）。回避区只做落点重取，热区形状不变。
        """
        return AVOID_WIN_TEAM3

    def _restore_battle_count(self) -> int:
        """从进度文件恢复已完成场次到 current_count。

        必须在捕获 before_count 之前调用，否则战斗统计的「本次新增场数」会把
        历史场次算进去。恢复后 run_alone 的次数上限判断天然只打剩余场次。
        """
        progress = getattr(self, '_progress', None)
        key = getattr(self, '_progress_key', None)
        if progress is None or not key:
            return 0
        try:
            done = progress.get_battle_count(key)
        except Exception:
            logger.exception('读取同心战斗场次失败，从 0 开始')
            return 0
        if done > 0:
            logger.info(f'同心战斗接续：已完成 {done} 场')
            self.current_count = done
        return done

    def _persist_battle_count(self) -> None:
        """每完成一场立刻回写，保证中断后能接续剩余场次。"""
        progress = getattr(self, '_progress', None)
        key = getattr(self, '_progress_key', None)
        if progress is None or not key:
            return
        try:
            progress.add_battle_count(key, 1)
        except Exception:
            logger.exception('回写同心战斗场次失败')

    def run_alliedteam(self, battle_enable, ap_enable):
        self.screenshot()
        if self.ui_get_current_page() != page_main:
            self.ui_goto(page_main)
        self.ui_goto(page_team)
        # 先恢复历史场次，再捕获基线，确保统计的是本次新增场数
        self._restore_battle_count()
        before_count = getattr(self, "current_count", 0)
        # 每轮仅在协战配置下启用游戏次数目标，补打轮沿用当前场次而不重读历史进度。
        configured_limit = self.get_config().daily_alt_acc_config.alliedteam_limit_count
        self._help_target_enabled = bool(battle_enable and configured_limit == 13)
        self._help_battle_limit = configured_limit
        self._help_target_synced = False
        self._help_target_complete = None
        self._help_battle_blocked = False
        battle_result = None
        if ap_enable:
            self.run_alliedteam_ap()
        if battle_enable:
            battle_result = self.run_alliedteam_battle()
            self.return_to_main()
            # 截图核对与补打完成后再统计，额外补的场次也必须纳入本轮统计。
            if self._help_target_enabled and self._help_target_complete is not None:
                battle_result = self._help_target_complete
            # 只统计本次同心战斗新增场数，不统计胜负结果。
            emit_stat = getattr(self, "emit_stat", None)
            if emit_stat:
                emit_stat(StatEvent.BATTLE, count=getattr(self, "current_count", 0) - before_count)
        elif ap_enable:
            emit_stat = getattr(self, "emit_stat", None)
            if emit_stat:
                emit_stat(StatEvent.BATTLE, count=0)
        # 未打满就结束（选关/邀请失败一场未打，或中途无法回到挑战界面）：透传
        # False 走 False 计数通道（保持 pending 可重跑、两次后 skipped），
        # 不能靠默认返回 None 被标成 done——那会让剩余场次被当作已完成丢弃
        if battle_result is False:
            return False

    def run_alliedteam_ap(self):    
        logger.info('开始执行补体力任务')
        start_time = time.time()
        count_i_ensure_ap=0
        while time.time()-start_time < 5:
            self.screenshot()
            if self.appear_then_click(self.I_TO_TEAM, interval=1):
                start_time = time.time()
                continue
            if self.appear_then_click(self.I_TO_AP, interval=1):
                start_time = time.time()
                continue
            if self.appear(self.I_ENSURE_AP):
                self.ui_click_until_disappear(self.I_ENSURE_AP, interval=1)
                count_i_ensure_ap+=1
                start_time = time.time()
                if count_i_ensure_ap>1:
                    break
                continue
            if self.appear_then_click(self.I_UI_BACK_RED, interval=1):
                start_time = time.time()
                continue
            if self.appear_then_click(self.I_LIST_MEMBER, interval=1):
                start_time = time.time()
                continue
            if self.appear_then_click(self.I_SAVE_ALL, interval=1):
                start_time = time.time()
                continue
    
        while 1:
            self.screenshot()
            if self.appear(GameUiAssets.I_CHECK_MAIN) or self.appear(self.I_M_MAIN_TO_MAIL):
                break
            if self.appear_then_click(self.I_UI_BACK_YELLOW, interval=1):
                continue
            if self.appear_then_click(self.I_BACK_BLACK, interval=1):
                continue

    def is_select_level(self):
        if not self.O_SELECT_LEVEL.ocr(self.device.image)=="觉醒业火轮壹层":
            if not self.check_zones('觉醒业火轮'):
                return False
            while 1:
                self.screenshot()
                if self.O_SELECT_LEVEL.ocr(self.device.image)=="觉醒业火轮壹层":
                    break
                logger.info(f"当前选择关卡:{list(self.O_FLAG_LEVEL.ocr(self.device.image))}")
                if not list(self.O_FLAG_LEVEL.ocr(self.device.image))==[0,0,0,0]:
                    logger.info(f"当前选择关卡:{self.O_SELECT_LEVEL.ocr(self.device.image)}")
                else :
                    self.appear_then_click(self.I_CLICK_EVOZONE, interval=1)
                    continue
                if self.appear_then_click(self.I_CLICK_LEVEL, interval=1):
                    continue
                self.swipe(self.S_SELECT_LEVEL,3)
        return True           

    def run_alliedteam_battle(self):
        """同心组队战斗期间禁用退队 Page，正常返回或异常后都恢复导航退出能力。"""
        previous_active = self._alliedteam_battle_active
        self._alliedteam_battle_active = True
        try:
            return self._run_alliedteam_battle()
        finally:
            self._alliedteam_battle_active = previous_active

    def _run_alliedteam_battle(self):
        """执行同心选关、邀请和连续战斗，入口包装器负责保护当前集结。"""
        logger.info('开始执行战斗任务')
        # 邀请人数阈值由小号配置控制（默认2，可设1）：不足则继续邀请，达标即进入下一流程
        alliedteam_invite_count = self.get_config().daily_alt_acc_config.alliedteam_invite_count
        if not self.is_select_level():
            return False
        start_time = time.time()
        while time.time()-start_time < 5:
            self.screenshot()
            if self.appear(self.I_PAGE_INVITE, interval=1):
                start_time = time.time()
                break
            if self.appear_then_click(self.I_TO_TEAM, interval=1):
                start_time = time.time()
                continue
            
            if self.appear_then_click(self.I_INVITE, interval=1):
                start_time = time.time()
                continue
        if time.time()-start_time >= 5:
            return False
        while time.time()-start_time < 5:
            self.screenshot()
            if self.appear(self.I_FORM_OVER):
                start_time = time.time()
                logger.info("邀请完成")
                break
            if len(self.I_INVITE_FRIEND_OVER.match_all_any(self.device.image)) < alliedteam_invite_count:
                logger.info("邀请好友")
                if self.appear(self.I_INVITE_FRIEND, interval=1):
                    self.I_INVITE_FRIEND.roi_front[2]=13
                    self.I_INVITE_FRIEND.roi_front[3]=12
                    self.click(self.I_INVITE_FRIEND)
                    time.sleep(1)
                start_time = time.time()
                continue
                
            if self.appear_then_click(self.I_FORM, interval=1):
                start_time = time.time()
                continue
                   
        # 首次建队：推到可点「挑战」为止。原来是无超时死循环，界面异常时只能靠
        # 60s stuck 抛异常收场；改为超时返回 False 走「未打满」通道，可下轮接续
        if not self._ensure_battle_ready():
            return False
        return self.run_alone()

    def _ensure_battle_ready(self, timeout: float = 60) -> bool:
        """把界面推回可点「挑战」的状态（I_BATTLE 可见）。

        建队链路与「打完一轮被弹回组队页」的恢复动作完全相同，因此抽出共用：
        队伍打满一轮或队友退出后，游戏会回到组队页（"请在左侧选择目标副本"），
        此时 I_BATTLE 不出现。原来 run_alone 只能空转到 60s stuck 超时把整个
        子任务判失败，剩余场次全丢。

        循环内的点击会经 handle_control_check 重置 stuck 计时，所以只要还在
        尝试恢复就不会误触发 GameStuckError；真正无法恢复时按超时返回 False，
        交给调用方按「未打满」收尾，而不是抛异常。

        :return: True 表示已就绪可点挑战
        """
        timeout_timer = Timer(timeout).start()
        while not timeout_timer.reached():
            self.screenshot()
            if self.appear(self.I_BATTLE, interval=1):
                return True
            if self.appear_then_click(self.I_CREATE_AGAIN, interval=1):
                continue
            if self.appear_then_click(self.I_CREATE_TEAM, interval=1):
                continue
            if self.appear_then_click(self.I_SELECT_LEVEL, interval=1):
                continue
        logger.warning('无法回到同心挑战界面（组队页恢复超时）')
        return False

    def return_to_main(self, capture: bool = True):
        """退出副本回庭院；补打回调只导航，最终截图由外层统一核对保存。"""
        while 1:
            self.screenshot()
            if self.appear(GameUiAssets.I_CHECK_MAIN) or self.appear(self.I_M_MAIN_TO_MAIL):
                    break
            if self.appear_then_click(self.I_EXIT_2, interval=1):
                continue
            if self.appear_then_click(self.I_ENSURE_EXIT, interval=1):
                continue
            if self.appear_then_click(self.I_UI_BACK_YELLOW,interval=1):
                continue
            if self.appear_then_click(self.I_BACK_BLACK,interval=1):
                continue
            if self.appear_then_click(self.I_EXIT3, interval=1):
                continue
            if self.appear_then_click(self.I_UI_BACK_RED, interval=1):
                continue    
        self.ui_goto(page_main)
        if capture and self.get_config().daily_alt_acc_config.alliedteam_limit_count == 13:
            # 次数未显示时回庭院重进，第三次仍失败则保留现场截图。
            image = (capture_completed_help_image(self, self._refill_allied_help_battles)
                     if self._help_target_enabled else capture_friend_help_image(self))
                
            now=datetime.now()
            # 多账号统一使用完整身份，避免同名角色覆盖；单账号保留原命名。
            screenshot_name = build_help_screenshot_name(
                getattr(self, '_stat_ctx', None), self.config.config_name,
                getattr(self, '_help_screenshot_name_mode', None) or 'char',
            )
            save_dir = Path(f'screenshots/Battle_Screenshots_{now.year}_{now.month:02d}_{now.day:02d}')
            save_dir.mkdir(parents=True, exist_ok=True)
            # 同一身份同一天覆盖为最新校验帧（或第三次失败帧），不再另取未经校验的画面。
            save_path = save_dir / f'{screenshot_name}.png'
            save_image(image, str(save_path))
            logger.info(f'同心协战次数截图已保存: {save_path}')
            run_timer=Timer(5)
            run_timer.start()
            while 1:    
                self.screenshot()
                if run_timer.reached():
                    break
                if self.appear_then_click(self.I_UI_BACK_RED, interval=1):
                    break
            self.screenshot()
            if self.ui_get_current_page() != page_main:
                self.ui_goto(page_main)
            

    def _refill_allied_help_battles(self, remaining: int) -> bool:
        """同心协战先解除旧队伍集结再重新组队，补完当前缺额后返回庭院。"""
        if not self._help_target_enabled or self._help_battle_blocked:
            return False
        # 与其他任务共用退队 Page：横幅 X → 退队确定 → 庭院，再进入组队页。
        if not self.ui_goto(page_team):
            raise GamePageUnknownError('同心协战补打无法重新进入组队页')
        self._help_battle_limit = self.current_count + remaining
        self._help_target_synced = False
        result = self.run_alliedteam_battle()
        # 不能再走截图闭环，否则补打会递归调用自身。
        self.return_to_main(capture=False)
        return result

    def _check_help_ap_exhausted(self) -> bool:
        """协战体力不足时关窗并停止补打，交给外层回庭院留图。"""
        if not self._help_target_enabled:
            return False
        if not self.appear(EvoZoneAssets.I_EVOZONE_BUY_AP_CLOSE):
            return False
        self.ui_click_until_disappear(EvoZoneAssets.I_EVOZONE_BUY_AP_CLOSE, interval=1)
        self._help_battle_blocked = True
        logger.warning('同心协战体力不足：已关闭购买体力弹窗，停止本轮补打并保留最终截图')
        return True

    def check_lock(self, lock: bool = True) -> bool:
        """
        检查是否锁定阵容, 要求在觉醒界面
        :param lock:
        :return:
        """
        logger.info('Check lock: %s', lock)
        if lock:
            while 1:
                self.screenshot()
                if self.appear(self.I_LOCK):
                    return True
                if self.appear_then_click(self.I_UNLOCK, interval=1):
                    continue
        else:
            while 1:
                self.screenshot()
                if self.appear(self.I_UNLOCK):
                    return True
                if self.appear_then_click(self.I_LOCK, interval=1):
                    continue

    def run_alone(self) -> bool:
        """连续打到次数上限。

        :return: True 表示已打满上限；False 表示中途无法回到挑战界面（已打场次
                 均已落盘，调用方按「未打满」收尾，剩余场次留给下轮接续）
        """
        def is_in_evozone(screenshot=False) -> bool:
            if screenshot:
                self.screenshot()
            return self.appear(self.I_BATTLE)
        logger.info('Start run alone')
        alliedteam_limit_count=self.get_config().daily_alt_acc_config.alliedteam_limit_count
        logger.info(f' alliedteam_limit_count: {alliedteam_limit_count}')
        # 游戏内自动战斗开关：开启后脚本不点战斗交互，只开自动、数场次、控总数
        auto_battle_enable = self.get_config().daily_alt_acc_config.alliedteam_auto_battle_enable
        # 次数为 13 的账号：改为在准备界面切换援助式神，因此此处必须先解锁阵容
        # （若保持锁定状态，准备界面将无法切换援助式神）。其余次数维持原锁定阵容流程。
        # 协战每次进入（含接续和补打）都先手动检查次数、援助上场，再允许游戏内自动。
        self._help_target_enabled = alliedteam_limit_count == 13
        self._help_target_synced = False
        if alliedteam_limit_count == 13:
            self.check_lock(False)
            self._need_switch_help_shikigami = True
            self._help_shikigami_detect = True
        else:
            # 同一对象切换到普通刷本时清除协战分发标记，避免沿用上轮模式。
            self._need_switch_help_shikigami = False
            self.check_lock(True)
        while 1:
            self.screenshot()
            # 体力弹窗也可能遮住挑战按钮，先处理再决定是否需要恢复组队界面。
            if self._check_help_ap_exhausted():
                return False
            # 准备页首次 OCR 会更新上限，循环中实时读取，不能使用进入循环前的旧值。
            battle_limit = (self._help_battle_limit
                            if self._help_target_enabled and self._help_battle_limit is not None
                            else alliedteam_limit_count)
            if self.current_count >= battle_limit:
                logger.info('Orochi count limit out')
                return True
            if not is_in_evozone():
                # 打完一轮被弹回组队页：复用建队链路推回「挑战」，不再空转到卡死。
                # 恢复不了就带着已落盘的场次正常退出，交给下轮接续剩余场次。
                if not self._ensure_battle_ready():
                    logger.warning(
                        f'同心战斗中断：已打 {self.current_count}/{alliedteam_limit_count} 场，'
                        f'剩余场次留待下轮接续'
                    )
                    return False
                continue
            # 协战确认实际次数并完成援助上场后才挂自动，断点接续也不能跳过首轮检查。
            # 自动模式与手动模式不同：进入后由游戏自动连续挑战（自动准备/开局/
            # 结算），脚本只开自动、数场次、控总数，直到打满收尾或异常退出。
            if auto_battle_enable and not (
                    self._help_target_enabled and (not self._help_target_synced or self._help_shikigami_detect)):
                # 开自动前必须先锁定队伍：未锁定时游戏自动准备可能带上错误阵容。
                # check_lock 幂等，已锁定时一帧截图即返回。
                self.check_lock(True)
                return self._auto_battle_loop(battle_limit)
            # 点击挑战
            while 1:
                self.screenshot()
                # 弹窗后方仍可识别挑战按钮，先关闭体力弹窗再按未完成收尾。
                if self._check_help_ap_exhausted():
                    return False
                if self.appear_then_click(self.I_BATTLE, interval=1):
                    pass
                if not self.appear(self.I_BATTLE):
                    # 准备页可能已达 13 次：撤销通用战斗的预增，不把未开打场次落盘。
                    before_battle = self.current_count
                    try:
                        self.run_general_battle(config=self.config.daily_alt_acc.general_battle_config)
                        # 本场结束立刻落盘，未开打就达标退出的分支不会经过这里。
                        self._persist_battle_count()
                    except HelpTargetReached:
                        self.current_count = before_battle
                        if not self.exit_battle():
                            raise GamePageUnknownError('协战已达目标，但无法退出同心准备界面')
                        return True
                    break

    def _locate_help_shikigami(self) -> list:
        """OCR 定位援助式神卡上的协战次数标签（N/15），返回其屏幕 roi。

        原 O_FIND_SHIKIGAMI_HELP（keyword="15"，师徒场景找 15 级援助式神的锚点）
        在同心准备界面上并不存在 "15" 整串文本，实际一直靠 FULL 模式的
        「keyword 任一单字命中」降级匹配到式神卡旁的 "N/15" 标签——位置碰巧
        正确所以平时能用。但候选列表里没有该标签时（09-01 角色A、09-06 角色B
        两次事故），单字降级会误中 "9999991" 等垃圾串，锚点偏移 500+px，
        从错误位置滑动导致援助式神未上场、全天 13 场好友协战 0 计数。
        这里改为直接遍历 OCR 候选，严格按 \\d+/15 匹配协战标签（坐标换算
        与 Full.ocr_full 保持一致），匹配不到返回 [0,0,0,0] 交给调用方
        按锚点丢失处理，不再有降级误匹配面。
        """
        ocr_obj = MasterDiscipleAssets.O_FIND_SHIKIGAMI_HELP
        try:
            boxed_results = ocr_obj.detect_and_ocr(self.device.image)
        except Exception:
            logger.exception('同心援助式神锚点 OCR 失败，按未识别处理')
            return [0, 0, 0, 0]
        for result in boxed_results:
            count = parse_help_count(result.ocr_text)
            if count is not None:
                # 读取锚点时同步剩余场数；非协战分支不会改动场次目标。
                sync_help_battle_target(self, count)
                # detect_and_ocr 的 box 坐标相对 roi 裁剪图，需加回 roi 偏移
                box = result.box
                return [box[0][0] + ocr_obj.roi[0], box[0][1] + ocr_obj.roi[1],
                        box[1][0] - box[0][0], box[2][1] - box[0][1]]
        return [0, 0, 0, 0]

    def battle_before(self, buff: BuffClass | list[BuffClass], config: GeneralBattleConfig, timeout: float = 5) -> bool:
        """战斗前设置
        次数为 13 的账号走「切换援助式神 → 准备」流程（参照 MasterDisciple 的
        _disciple_exploration_battle_before）；其余账号沿用父类原有的战斗准备逻辑。
        :return: True:进入战斗或点击了准备按钮且识别不到准备按钮了 False:超过timeout还没进入战斗且没点过准备
        """
        # 非 13 账号：维持原有通用战斗准备流程
        if not self._need_switch_help_shikigami:
            return super().battle_before(buff, config, timeout)

        # 13 账号：在准备界面切换援助式神后再点准备（不锁定阵容，仅首场切换）
        timeout_timer = Timer(timeout).start()
        # 每场战斗重置锚点丢失计数：重试上限只约束本场，超限降级后下一场从头计
        self._help_anchor_miss = 0
        while not timeout_timer.reached():
            self.screenshot()
            if self.is_in_real_battle(False):  # 已进入战斗阶段
                return True
            if self.appear_then_click(self.I_DISABLE_7DAYS_DIFF_SOUL, interval=0.6):  # 关闭御魂不一致提示
                continue
            if self.appear_then_click(self.I_CONFIRM_CLOSE_DIFF_SOUL, interval=0.6):  # 确认关闭御魂不一致提示
                continue
            if self.is_in_prepare(False):  # 战斗准备阶段
                timeout_timer.reset()
                # 切换援助式神逻辑（参照 Plotline / MasterDisciple）
                if self._help_shikigami_detect:
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
                            self._help_shikigami_detect = False
                        else:
                            # 告警 + 场内重试；超限后本场放弃切换直接开打（标记
                            # 保持 True，下一场继续尝试，一次失败不葬送全天场次）
                            self._help_anchor_miss += 1
                            logger.warning(f'同心援助式神锚点(N/15标签)未识别到，'
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
                                roi[0], roi[1], roi[2], roi[3]
                            )
                            self.swipe(PlotlineAssets.S_SWIPE_SHIKIGAMI, 4)
                            sleep(2)
                            continue
                        # 援助式神确认在场：清除切换标记，后续场次直接点准备
                        self._help_shikigami_detect = False
                # 点击准备
                if self.appear_then_click(self.I_PREPARE_HIGHLIGHT, interval=0.8):
                    continue
                continue
            logger.info('Wait for preparation page')
            sleep(random.uniform(0.4, 0.8))
        return False

    def _read_room_countdown(self) -> str:
        """读顶部房间倒数文字的原始 OCR 文本。

        不用 ocr_appear：FULL 模式的 filter 在整串匹配失败时会降级成
        「keyword 任一单字命中」（「0/分」会被房名等文本误触发），改用
        detect_text 取原始文本由调用方做严格子串判断——「00分0」只认完整
        出现才算自动开启标志，「01分」~「05分」的分钟级读数才算确认关闭
        （同 _is_exp_extract_dialog）。
        OCR 异常时返回空串按未识别处理，不影响主流程。
        """
        try:
            return self.O_ITEM_3.detect_text(self.device.image)
        except Exception:
            logger.exception('房间倒数文字 OCR 失败，按未识别处理')
            return ''

    def _countdown_minutes_stable(self, stable_seconds: float = 2) -> bool:
        """复测窗口内倒数是否持续为分钟级读数（01分~05分）。

        战斗结束回到房间的瞬间会闪现一帧「0X分XX」的假读数（自动仍开着，
        下一帧就跳回「00分0X」开下一场）。单帧读到分钟级不能确认自动已关，
        必须在 stable_seconds 内持续复读都是分钟级才算真关。
        :return: True 倒数稳定为分钟级（自动确实已关闭）；
                 False 期间出现「00分0」等秒级读数（闪现假读数，仍开着）
        """
        stable_timer = Timer(stable_seconds).start()
        while not stable_timer.reached():
            self.screenshot()
            if not re.search(r'0[1-5]分', self._read_room_countdown()):
                logger.info('复测中倒数回到秒级，判定为闪现假读数，自动仍开着')
                return False
        return True

    def _setup_paper_settings(self) -> None:
        """配置纸人设置弹窗：自动喂养 ON、设置挑战次数 OFF。

        paper/paper_2 与自动喂养、挑战次数的 on/off 开关都是同位置的明暗两态
        模板，纯模板匹配会互相误命中，全部用 appear_rgb（模板匹配 + 平均颜色
        比对）区分明暗态。流程：准备界面（房间）点击 paper 打开弹窗 → 自动
        喂养识别到 OFF 态就点击置 ON、设置挑战次数识别到 ON 态就点击置 OFF
        → 点击 paper_2 关闭弹窗，重新看到 paper 即确认回到准备界面。
        任一步识别超时只告警跳过，不阻断自动战斗主流程。
        """
        logger.info('配置纸人设置：自动喂养 ON、设置挑战次数 OFF')
        # 点击纸人按钮打开设置弹窗
        open_timer = Timer(5).start()
        while not open_timer.reached():
            self.screenshot()
            if self.appear_rgb(self.I_PAPER):
                self.click(self.I_PAPER)
                time.sleep(1)
                break
        else:
            logger.warning('未识别到纸人按钮，跳过纸人设置')
            return
        # 等设置弹窗弹出（任一开关可见即视为已打开）
        popup_timer = Timer(5).start()
        while not popup_timer.reached():
            self.screenshot()
            if (self.appear_rgb(self.I_AUTO_FEED_OFF) or self.appear_rgb(self.I_AUTO_FEED_ON)
                    or self.appear_rgb(self.I_AUTO_CONUT_ON) or self.appear_rgb(self.I_AUTO_CONUT_OFF)):
                break
        else:
            logger.warning('纸人设置弹窗未出现，跳过设置')
            return
        # 自动喂养置 ON：仅当前为 OFF 态时点击切换
        if self.appear_rgb(self.I_AUTO_FEED_OFF):
            self.click(self.I_AUTO_FEED_OFF)
            time.sleep(0.5)
            logger.info('自动喂养已置为 ON')
        # 设置挑战次数置 OFF：仅当前为 ON 态时点击切换
        if self.appear_rgb(self.I_AUTO_CONUT_ON):
            self.click(self.I_AUTO_CONUT_ON)
            time.sleep(0.5)
            logger.info('设置挑战次数已置为 OFF')
        # 点击 paper_2 关闭弹窗；重新看到 paper 即确认回到准备界面
        close_timer = Timer(5).start()
        while not close_timer.reached():
            self.screenshot()
            if self.appear_rgb(self.I_PAPER):
                logger.info('纸人设置完成，已回到准备界面')
                return
            if self.appear_rgb(self.I_PAPER_2):
                self.click(self.I_PAPER_2)
                time.sleep(1)
        logger.warning('纸人设置弹窗关闭确认超时')

    def _auto_battle_loop(self, limit_count: int) -> bool:
        """游戏内自动战斗主循环：脚本不点任何战斗交互，只开自动、数场次、控总数。

        自动开关（I_AUTO_OFF）与锁队按钮同在房间（挑战按钮 I_BATTLE 所在界面）
        左下角。进入循环先配置纸人设置（_setup_paper_settings：自动喂养 ON、
        设置挑战次数 OFF），随后在房间点开自动，游戏会自己点挑战、自动准备、
        自动战斗、自动结算并连续下一场——全程脚本不点 I_BATTLE。脚本只做：
          1. 房间内点击 I_AUTO_OFF（关态模板）开启自动；开启成功的标志是顶部
             出现严格匹配的倒数文字「00分0」，或未经脚本点击却被拉进战斗；
          2. 以「进入战斗」的上升沿累计场次，每场立刻落盘（中断可接续）；
             战斗画面单帧识别抖动由结束确认防抖过滤：连续 BATTLE_END_CONFIRM_S
             秒不在战斗画面才确认上一场结束，确认前的上升沿视为同场抖动不计数；
          3. 场次打满后回到房间：若仍读到「00分0」说明自动还开着，直接点击
             I_AUTO_OFF 坐标（不识别模板，开启态下关态模板匹配不到），直到
             倒数恢复「01分」~「05分」的分钟级读数才确认关闭；
          4. 场次已满却仍被拉进战斗：说明自动没关掉，退出战斗并结束同心队流程。
             场次此时已打完，所以仍判定为成功完成。
        :param limit_count: 同心战斗总场次上限
        :return: True 打满收尾（已确认关闭或场次已满退出战斗）
        """
        logger.info('进入游戏内自动战斗模式')
        # 循环内脚本几乎不点击，登记长战斗标记避免 stuck 误判（同 battle_wait）
        self.device.stuck_record_add('BATTLE_STATUS_S')
        # 开自动前先配置纸人设置（自动喂养 ON / 设置挑战次数 OFF），由配置
        # alliedteam_paper_settings_enable 控制；失败只告警不阻断
        if self.get_config().daily_alt_acc_config.alliedteam_paper_settings_enable:
            self._setup_paper_settings()
        auto_on = False  # 已确认自动开启（读到过「00分0」或被动进过战斗）
        in_battle_prev = False
        # 上一场是否已确认结束：初始视为已结束（首场前无「上一场」）。
        # I_BATTLE_INFO 单模板在战斗动画中偶发丢一帧，若直接按上升沿计数，
        # 同一场战斗会被误计多场（实测日志出现 2.1s 异常短间隔）。只有连续
        # BATTLE_END_CONFIRM_S 秒都不在战斗画面才确认本场结束，确认前的
        # 战斗上升沿一律视为同场识别抖动，不计数。
        battle_confirmed_end = True
        battle_end_timer = None  # 离开战斗画面时启动的计时器；回战斗画面即清空
        while 1:
            self.screenshot()
            in_battle = self.is_in_real_battle(False)
            if not in_battle:
                # 游戏自动挑战也会触发购买体力弹窗，受阻后必须退出循环才能留图。
                if self._check_help_ap_exhausted():
                    return False
                # 不在战斗画面：启动/保持结束计时，连续达标才确认上一场结束
                if battle_end_timer is None:
                    battle_end_timer = Timer(self.BATTLE_END_CONFIRM_S).start()
                elif battle_end_timer.reached():
                    battle_confirmed_end = True
            else:
                # 回到战斗画面：结束计时作废，重新从零开始计
                battle_end_timer = None
            if in_battle and not in_battle_prev:
                if not battle_confirmed_end:
                    # 同一场战斗内的模板识别抖动（丢一帧又匹配上），不是新场次
                    logger.info('战斗画面识别抖动，忽略该次上升沿')
                elif self.current_count >= limit_count:
                    # 场次已满仍被拉进战斗：自动没关掉。退出战斗并结束同心队
                    # 流程；场次已打完，判定为成功完成
                    logger.warning('场次已满但自动未关闭，仍被拉进战斗，退出战斗并结束同心队战斗')
                    self.exit_battle()
                    return True
                else:
                    # 正常自动场次：计数并立刻落盘，保证中断后可接续。
                    # 未经脚本点击却被拉进战斗本身即自动已开启的证据
                    self.current_count += 1
                    self._persist_battle_count()
                    auto_on = True
                    logger.info(f'游戏内自动战斗: {self.current_count}/{limit_count}')
                    # 新场次开始：本场尚未结束，重置结束确认状态
                    battle_confirmed_end = False
                # 每场开始整体重置卡死看门狗（60s 与 300s 计时器一并归零）并重挂
                # 长战斗标记：开自动前的纸人/开关点击会把入口处的标记清掉，而
                # 自动战斗中脚本零点击，不重置会被 60s 普通超时打死（实测第 5
                # 场后 GameStuckError）
                self.device.stuck_record_clear()
                self.device.stuck_record_add('BATTLE_STATUS_S')
            in_battle_prev = in_battle

            if in_battle:
                # 战斗中：游戏自动打，只等本场结束
                continue

            # ---- 战斗外 ----
            # 御魂不一致弹窗在房间/结算界面都可能出现，照常处理避免卡住
            if self.appear_then_click(self.I_DISABLE_7DAYS_DIFF_SOUL, interval=0.6):
                continue
            if self.appear_then_click(self.I_CONFIRM_CLOSE_DIFF_SOUL, interval=0.6):
                continue

            countdown = self._read_room_countdown()
            if self.current_count >= limit_count:
                # 打满收尾：房间内关自动，倒数从「00分0」恢复「01分」~「05分」
                # 的分钟级读数才算确认关闭。战斗结束回房间的瞬间会闪现一帧
                # 「0X分XX」再跳回「00分0X」（自动仍开着下一场），单帧读数不
                # 可信，必须连续 STABLE_CONFIRM_S 秒都是分钟级才确认
                if re.search(r'0[1-5]分', countdown):
                    if self._countdown_minutes_stable():
                        logger.info(f'倒数稳定在分钟级[{countdown}]，游戏内自动战斗已确认关闭')
                        return True
                    # 稳定复测中看到了「00分0」：闪现读数，自动仍开着，
                    # 回到主循环走点开关关闭的分支
                    continue
                if '00分0' in countdown:
                    # 自动还开着：直接点击开关坐标关闭后重读倒数
                    logger.info('场次已满，点击关闭游戏内自动战斗')
                    self.click(self.I_AUTO_OFF)
                    time.sleep(1)
                    continue
                # 结算/过场等其他界面或倒数暂不可读：等游戏流转回房间
                continue

            # ---- 未打满：房间内开自动，之后挑战由游戏自己点 ----
            if self.appear(self.I_BATTLE):
                if '00分0' in countdown:
                    if not auto_on:
                        logger.info('识别到房间倒数「00分0」，游戏内自动战斗已开启')
                        auto_on = True
                    continue
                # 自动开关处于关态：点击开启；点击未生效时关态模板会再次
                # 出现，interval 节流下自然重试
                if self.appear_then_click(self.I_AUTO_OFF, interval=2):
                    logger.info('已点击开启游戏内自动战斗，等待「00分0」确认')
                    continue
            # 其余界面（准备/结算/过场）：游戏自动流转，卡死由 stuck 机制兜底


if __name__ == "__main__":
    from module.config.config import Config
    from module.device.device import Device
    c = Config('oas1')
    d = Device(c)
    self = Alliedteam(c, d)
    self.screenshot()
    self.run_alliedteam_ap()
    #self.run_alliedteam(False, True)
