# This Python file uses the following encoding: utf-8
# @author runhey
# github https://github.com/runhey
from time import sleep
from datetime import time, datetime, timedelta

from tasks.Component.GeneralBattle.general_battle import GeneralBattle
from tasks.Component.GeneralBattle.config_general_battle import GeneralBattleConfig
from tasks.Component.GeneralBuff.config_buff import BuffClass
from tasks.Component.GeneralInvite.general_invite import GeneralInvite
from tasks.Component.GeneralBuff.general_buff import GeneralBuff
from tasks.Component.GeneralRoom.general_room import GeneralRoom
from tasks.Component.SwitchSoul.switch_soul import SwitchSoul
from tasks.Component.SwitchHelpShikigami import SwitchHelpShikigami
from tasks.Component.SwitchHelpShikigami.switch_help_shikigami import HelpTargetReached
from tasks.GameUi.game_ui import GameUi
from tasks.GameUi.page import page_main, page_awake_zones, page_shikigami_records
from tasks.Utils.optional_ability import run_optional_ability
from tasks.EvoZone.assets import EvoZoneAssets
from tasks.EvoZone.config import EvoZone, UserStatus, KirinType
from module.logger import logger
from module.exception import TaskEnd, GamePageUnknownError

# 需要「准备界面切换援助式神」的次数标记：该值来自游戏侧的好友协战计数玩法
# （每天固定 13 场），不是可调阈值，所以按字面量比较而非抽成配置项。
HELP_SHIKIGAMI_LIMIT_COUNT = 13

# 场次进度在 ProgressStore 里的子任务键。同心战斗用默认的 'alliedteam'，
# 两者可能共用同一个 store 的 tasks 命名空间，键必须区分开，否则同一账号的
# 两种计数会互相覆盖。
BATTLE_TASK_KEY = 'evo_zone'


class ScriptTask(SwitchHelpShikigami, GeneralBattle, GeneralInvite, GeneralBuff,
                 GeneralRoom, GameUi, EvoZoneAssets, SwitchSoul):
    # 场次进度存储与当前账号的键。**只由 MultiTasks 注入**——接续是多账号批量
    # 执行的诉求，单实例直跑始终保持 None，既不做恢复也不回写任何进度文件。
    _progress = None
    _progress_key: str = None
    # 协战模式：由复用方注入。None 表示按单实例直跑的原有语义
    # ——次数等于 HELP_SHIKIGAMI_LIMIT_COUNT 才走「准备界面切换援助式神」。
    # 之所以要拆成独立开关而非放宽上面的字面量比较：次数在多账号场景是可配的，
    # 一旦不是 13，协战式神模式会静默失效。
    _help_shikigami_mode: bool = None
    # 协战截图命名模式：由复用方注入，透传给
    # save_friend_help_screenshot。None 表示沿用默认的「只带角色名」命名。
    _help_screenshot_name_mode: str = None
    # 仅原本要求 13 次的单人协战启用 OCR 目标，普通刷本与自定义场次沿用原逻辑。
    _help_target_enabled: bool = False
    _help_battle_count_restored: bool = False
    _help_battle_blocked: bool = False

    def _is_help_shikigami_account(self) -> bool:
        """本账号是否要走「准备界面切换援助式神」的协战路线。

        复用方注入 _help_shikigami_mode 时以它为准；未注入时退化为单实例直跑的
        原有判据（次数等于 HELP_SHIKIGAMI_LIMIT_COUNT），行为零变化。
        """
        if self._help_shikigami_mode is not None:
            return self._help_shikigami_mode
        return self.config.evo_zone.evo_zone_config.limit_count == HELP_SHIKIGAMI_LIMIT_COUNT

    def run(self) -> bool:

        limit_count = self.config.evo_zone.evo_zone_config.limit_count
        limit_time = self.config.evo_zone.evo_zone_config.limit_time
        self.current_count = 0
        self.limit_count: int = limit_count
        self.limit_time: timedelta = timedelta(hours=limit_time.hour, minutes=limit_time.minute,
                                               seconds=limit_time.second)
        con = self.config.evo_zone
        self._help_target_enabled = (
            self._is_help_shikigami_account()
            and limit_count == HELP_SHIKIGAMI_LIMIT_COUNT
            and con.evo_zone_config.user_status == UserStatus.ALONE)
        # 每轮重新初始化：OCR 只修正本轮战斗上限，不把已有协战数写入场次进度。
        self._help_battle_limit = self.limit_count
        self._help_target_synced = False
        self._help_target_complete = None
        self._help_battle_count_restored = False
        self._help_battle_blocked = False
        if con.switch_soul_config.enable:
            self.ui_get_current_page()
            self.ui_goto(page_shikigami_records)
            self.run_switch_soul(con.switch_soul_config.switch_group_team)
        if con.switch_soul_config.enable_switch_by_name:
            self.ui_get_current_page()
            self.ui_goto(page_shikigami_records)
            self.run_switch_soul_by_name(con.switch_soul_config.group_name, con.switch_soul_config.team_name)

        self.ui_get_current_page()
        self.ui_goto(page_main)
        config: EvoZone = self.config.evo_zone
        if config.evo_zone_config.soul_buff_enable:
            self.open_buff()
            self.awake(is_open=True)
            self.close_buff()

        success = True
        match config.evo_zone_config.user_status:
            case UserStatus.LEADER:
                success = self.run_leader()
            case UserStatus.MEMBER:
                success = self.run_member()
            case UserStatus.ALONE:
                alone_success = self.run_alone()
                if self._help_target_enabled:
                    success = alone_success is not False
            case UserStatus.WILD:
                self.run_wild()
            case _:
                logger.error('Unknown user status')

        # 记得关掉
        if config.evo_zone_config.soul_buff_enable:
            self.open_buff()
            self.awake(is_open=False)
            self.close_buff()
        # 13 次协战先按实际读数补齐，再保存最终截图；其余协战沿用原有截图流程。
        if self._is_help_shikigami_account():
            if self._help_target_enabled:
                self.save_friend_help_screenshot(
                    name_mode=self._help_screenshot_name_mode or 'char',
                    refill=self._refill_help_battles)
                # 三次 OCR 均失败时保留原运行结果；读数明确不足则不能误报完成。
                if self._help_target_complete is not None:
                    success = self._help_target_complete
            else:
                self.save_friend_help_screenshot(name_mode=self._help_screenshot_name_mode or 'char')
            run_optional_ability('post_task_hook', self)
        # 下一次运行时间
        if success:
            self.set_next_run('EvoZone', finish=True, success=True)
        else:
            self.set_next_run('EvoZone', finish=False, success=False)

        raise TaskEnd('EvoZone')

    def evozone_enter(self) -> bool:
        logger.info('Enter evozone')
        kirintype = self.I_LIGHTNING_KIRIN
        match self.config.evo_zone.evo_zone_config.kirin_type:
            case KirinType.FIREKIRIN:
                kirintype = self.I_FIRE_KIRIN
            case KirinType.WINDKIRIN:
                kirintype = self.I_WIND_KIRIN
            case KirinType.WATERKIRIN:
                kirintype = self.I_WATER_KIRIN
            case KirinType.LIGHTNINGKIRIN:
                kirintype = self.I_LIGHTNING_KIRIN
        while True:
            self.screenshot()
            if self.appear(self.I_FORM_TEAM):
                return True
            if self.appear_then_click(kirintype, interval=1):
                continue

    def check_layer(self, layer: str) -> bool:
        """
        检查挑战的层数, 并选中挑战的层
        :return:
        """
        pos = self.list_find(self.L_LAYER_LIST, layer)
        if pos:
            self.device.click(x=pos[0], y=pos[1])
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
                if self.appear(self.I_EVOZONE_LOCK):
                    return True
                if self.appear_then_click(self.I_EVOZONE_UNLOCK, interval=1):
                    continue
        else:
            while 1:
                self.screenshot()
                if self.appear(self.I_EVOZONE_UNLOCK):
                    return True
                if self.appear_then_click(self.I_EVOZONE_LOCK, interval=1):
                    continue

    def run_leader(self):
        logger.info('Start run leader')
        self.ui_get_current_page()
        # self.ui_goto(page_soul_zones)
        self.ui_goto(page_awake_zones)
        self.evozone_enter()
        layer = self.config.evo_zone.evo_zone_config.layer
        logger.info("test0")
        self.check_layer(layer)
        logger.info("test1")
        self.check_lock(self.config.evo_zone.general_battle_config.lock_team_enable)
        logger.info("test2")
        # 创建队伍
        logger.info('Create team')
        while 1:
            self.screenshot()
            if self.appear(self.I_CHECK_TEAM):
                break
            if self.appear_then_click(self.I_FORM_TEAM, interval=1):
                continue
        # 创建房间
        self.create_room()
        self.ensure_private()
        self.create_ensure()

        # 邀请队友
        success = True
        is_first = True
        # 这个时候我已经进入房间了哦
        while 1:
            self.screenshot()
            # 无论胜利与否, 都会出现是否邀请一次队友
            # 区别在于，失败的话不会出现那个勾选默认邀请的框
            if self.check_and_invite(self.config.evo_zone.invite_config.default_invite):
                continue

            # 检查猫咪奖励
            if self.appear_then_click(self.I_PET_PRESENT, action=self.C_WIN_3, interval=1):
                continue

            if self.current_count >= self.limit_count:
                if self.is_in_room():
                    logger.info('EvoZone count limit out')
                    break

            if datetime.now() - self.start_time >= self.limit_time:
                if self.is_in_room():
                    logger.info('EvoZone time limit out')
                    break

            # 如果没有进入房间那就不需要后面的邀请
            if not self.is_in_room():
                # 如果在探索界面或者是出现在组队界面， 那就是可能房间死了
                # 要结束任务
                sleep(0.5)
                if self.appear(self.I_MATCHING) or self.appear(self.I_CHECK_EXPLORATION):
                    sleep(0.5)
                    if self.appear(self.I_MATCHING) or self.appear(self.I_CHECK_EXPLORATION):
                        logger.warning('EvoZone task failed')
                        success = False
                        break
                continue

            # 点击挑战
            if not is_first:
                if self.run_invite(config=self.config.evo_zone.invite_config):
                    self.run_general_battle(config=self.config.evo_zone.general_battle_config)
                else:
                    # 邀请失败，退出任务
                    logger.warning('Invite failed and exit this EvoZone task')
                    success = False
                    break

            # 第一次会邀请队友
            if is_first:
                if not self.run_invite(config=self.config.evo_zone.invite_config, is_first=True):
                    logger.warning('Invite failed and exit this evozone task')
                    success = False
                    break
                else:
                    is_first = False
                    self.run_general_battle(config=self.config.evo_zone.general_battle_config)

        # 当结束或者是失败退出循环的时候只有两个UI的可能，在房间或者是在组队界面
        # 如果在房间就退出
        if self.exit_room():
            pass
        # 如果在组队界面就退出
        if self.exit_team():
            pass

        self.ui_get_current_page()
        self.ui_goto(page_main)

        if not success:
            return False
        return True

    def run_member(self):
        logger.info('Start run member')
        self.ui_get_current_page()
        # self.ui_goto(page_soul_zones)
        # self.evozone_enter()
        # self.check_lock(self.config.evo_zone.general_battle_config.lock_team_enable)

        # 进入战斗流程
        self.device.stuck_record_add('BATTLE_STATUS_S')
        while 1:
            self.screenshot()

            # 检查猫咪奖励
            if self.appear_then_click(self.I_PET_PRESENT, action=self.C_WIN_3, interval=1):
                continue

            if self.current_count >= self.limit_count:
                logger.info('EvoZone count limit out')
                break
            if datetime.now() - self.start_time >= self.limit_time:
                logger.info('EvoZone time limit out')
                break

            if self.check_then_accept():
                continue

            if self.is_in_room():
                self.device.stuck_record_clear()
                if self.wait_battle(wait_time=self.config.evo_zone.invite_config.wait_time):
                    self.run_general_battle(config=self.config.evo_zone.general_battle_config)
                else:
                    break
            # 队长秒开的时候，检测是否进入到战斗中
            elif self.check_take_over_battle(False, config=self.config.evo_zone.general_battle_config):
                continue

        while 1:
            # 有一种情况是本来要退出的，但是队长邀请了进入的战斗的加载界面
            if self.appear(self.I_CHECK_MAIN) or self.appear(self.I_CHECK_EXPLORATION):
                break
            # 如果可能在房间就退出
            if self.exit_room():
                pass
            # 如果还在战斗中，就退出战斗
            if self.exit_battle():
                pass

        self.ui_get_current_page()
        self.ui_goto(page_main)
        return True

    def battle_before(self, buff: BuffClass | list[BuffClass], config: GeneralBattleConfig,
                      timeout: float = 5) -> bool:
        """战斗前设置分发。

        只在显式启用（enable_help_shikigami_switch）时走「准备界面切换援助式神」
        流程，其余一律交回通用战斗准备的原有实现——未启用时行为与改动前完全一致。
        """
        if self._need_switch_help_shikigami:
            return self.battle_before_switch_help(buff, config, timeout)
        return super().battle_before(buff, config, timeout)

    def _restore_battle_count(self) -> int:
        """从进度文件恢复已完成场次到 current_count（同心战斗同款接续）。

        必须在战斗循环之前调用：恢复后 current_count 已是历史场次，
        run_general_battle 序言的 +1 与循环的次数上限判断天然只打剩余场次。
        未注入 store（单实例直跑）时保持 0，行为与改动前一致。
        """
        progress = getattr(self, '_progress', None)
        key = getattr(self, '_progress_key', None)
        if progress is None or not key:
            return 0
        try:
            done = progress.get_battle_count(key, BATTLE_TASK_KEY)
        except Exception:
            logger.exception('读取觉醒副本场次失败，从 0 开始')
            return 0
        if done > 0:
            logger.info(f'觉醒副本接续：已完成 {done} 场')
            self.current_count = done
        return done

    def _persist_battle_count(self) -> None:
        """每完成一场立刻回写，保证中断后能接续剩余场次。"""
        progress = getattr(self, '_progress', None)
        key = getattr(self, '_progress_key', None)
        if progress is None or not key:
            return
        try:
            progress.add_battle_count(key, 1, BATTLE_TASK_KEY)
        except Exception:
            logger.exception('回写觉醒副本场次失败')

    def _refill_help_battles(self, remaining: int) -> bool:
        """从庭院补足好友页确认的缺额，不重跑任务收尾或重新恢复历史场次。"""
        if not self._help_target_enabled or self._help_battle_blocked:
            return False
        if datetime.now() - self.start_time >= self.limit_time:
            self._help_battle_blocked = True
            logger.warning('觉醒协战已达时间上限，保留当前截图，不再补打')
            return False
        # 计数保持已尝试场次，只扩展本次缺额；重新识别上场标签以防好友页读数滞后。
        self._help_battle_limit = self.current_count + remaining
        self._help_target_synced = False
        self._help_battle_count_restored = True
        return self.run_alone()

    def run_alone(self):
        logger.info('Start run alone')
        self.ui_get_current_page()
        self.ui_goto(page_awake_zones)
        self.evozone_enter()
        layer = self.config.evo_zone.evo_zone_config.layer
        self.check_layer(layer)
        # 协战号：改为在准备界面切换援助式神，因此必须先解锁阵容
        # （锁定状态下准备界面无法切换援助式神）。其余账号维持原有的按配置锁定。
        if self._is_help_shikigami_account():
            self.check_lock(False)
            self.enable_help_shikigami_switch()
        else:
            self.check_lock(self.config.evo_zone.general_battle_config.lock_team_enable)

        # 中断接续只恢复一次，截图后的补打继续当前场次，避免被旧进度覆盖。
        if not self._help_target_enabled or not self._help_battle_count_restored:
            self._restore_battle_count()
            self._help_battle_count_restored = True

        def is_in_evozone(screenshot=False) -> bool:
            if screenshot:
                self.screenshot()
            return self.appear(self.I_EVOZONE_FIRE)

        # 体力不足提前结束的标记：置位后关掉弹窗跳出战斗循环，走原有收尾流程
        ap_exhausted = False
        while 1:
            self.screenshot()

            # 检查猫咪奖励
            if self.appear_then_click(self.I_PET_PRESENT, action=self.C_WIN_3, interval=1):
                continue

            if not is_in_evozone():
                continue

            # OCR 上限包含中断恢复场次；常规刷本仍只使用用户配置的次数。
            battle_limit = self._help_battle_limit if self._help_target_enabled else self.limit_count
            if self.current_count >= battle_limit:
                logger.info('EvoZone count limit out')
                break
            if datetime.now() - self.start_time >= self.limit_time:
                logger.info('EvoZone time limit out')
                if self._help_target_enabled:
                    self._help_battle_blocked = True
                break

            # 点击挑战
            while 1:
                self.screenshot()
                # 体力不足时游戏会弹出「购买体力」对话框，而挑战按钮在弹窗后面
                # 依然可见——下面那条否定式的退出条件永远不成立，继续点挑战只会
                # 反复撞弹窗，直到 12 次点击触发 GameTooManyClickError 把任务炸掉。
                # 所以必须在点之前判：识别到就关窗并跳过本账号剩余场次，
                # 交给收尾流程去截好友协战次数图。
                if self.appear(self.I_EVOZONE_BUY_AP_CLOSE):
                    ap_exhausted = True
                    break

                if self.appear_then_click(self.I_EVOZONE_FIRE, interval=1):
                    pass

                if not self.appear(self.I_EVOZONE_FIRE):
                    try:
                        self.run_general_battle(config=self.config.evo_zone.general_battle_config)
                        # 本场结束立刻落盘，中断后可从这里接续。
                        self._persist_battle_count()
                    except HelpTargetReached:
                        # 通用战斗在准备前已加一；已达 13 次时撤销预增并退出，不能记作打过。
                        self.current_count -= 1
                        if not self.exit_battle():
                            raise GamePageUnknownError('协战已达目标，但无法退出觉醒准备界面')
                    break

            if ap_exhausted:
                # 关窗点到 X 消失为止；真关不掉时由设备层的连续点击判定升级到
                # 既有恢复链路，不在这里静默空转
                self.ui_click_until_disappear(self.I_EVOZONE_BUY_AP_CLOSE, interval=1)
                logger.warning(
                    f'体力不足：已打 {self.current_count}/{battle_limit} 场，'
                    f'已关闭购买体力弹窗并跳过本账号剩余场次'
                )
                if self._help_target_enabled:
                    self._help_battle_blocked = True
                break

        # 回去
        while 1:
            self.screenshot()
            if not self.appear(self.I_FORM_TEAM):
                break
            if self.appear_then_click(self.I_BACK_Y, interval=1):
                continue

        self.ui_current = page_awake_zones
        self.ui_goto(page_main)
        # 补打层据此停止体力不足或超时重试，最终仍由截图读数确认是否达到目标。
        return not self._help_battle_blocked

    def run_wild(self):
        logger.error('Wild mode is not implemented')
        pass


if __name__ == '__main__':
    from module.config.config import Config
    from module.device.device import Device

    c = Config('oas1')
    d = Device(c)
    t = ScriptTask(c, d)

    t.run()

    # t.check_layer('悲')

    from module.base.timer import timer
