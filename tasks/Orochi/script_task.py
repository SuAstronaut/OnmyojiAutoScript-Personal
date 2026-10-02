# This Python file uses the following encoding: utf-8
# @author runhey
# github https://github.com/runhey
from time import sleep, monotonic
from datetime import time, datetime, timedelta

from tasks.Component.GeneralBattle.general_battle import GeneralBattle
from tasks.Component.GeneralBattle.reward_frame import (
    weighted_choice, AVOID_WIN_TEAM2)
from tasks.Component.GeneralInvite.general_invite import GeneralInvite
from tasks.Component.GeneralBuff.general_buff import GeneralBuff
from tasks.Component.GeneralRoom.general_room import GeneralRoom
from tasks.Component.SwitchSoul.switch_soul import SwitchSoul
from tasks.GameUi.game_ui import GameUi
from tasks.GameUi.page import page_main, page_soul_zones, page_shikigami_records
from tasks.Orochi.assets import OrochiAssets
from tasks.Orochi.config import Orochi, TeamMode, UserStatus, Layer
from tasks.Orochi.team_state import (
    PHASE_FINISHED,
    PHASE_INVITING,
    PHASE_RESETTING,
    PHASE_RUNNING,
    PHASE_WAIT_NEXT_ROUND,
    REALM_RAID_PAIRING_WAIT_SECONDS,
    RESET_COMMAND,    RoundNotDueError,
    StaleSessionError,
    TeamSession,
    TeamStateStore,
    parse_state_datetime,
)
from module.logger import logger
from module.exception import TaskEnd


class ScriptTask(GeneralBattle, GeneralInvite, GeneralBuff, GeneralRoom, GameUi, SwitchSoul, OrochiAssets):

    def reward_avoid(self) -> tuple:
        """御魂本组队时是两人，胜利画面上多出一块队友战绩框，落点回避它。

        单人跑时那块区域没有战绩框、本可点击，一并回避是刻意取舍：省去判断
        本场是否组队，而回避区不改变热区几何，代价只是那一小块概率被摊到周围。
        """
        return AVOID_WIN_TEAM2

    def run(self) -> bool:
        config: Orochi = self.config.orochi
        self.current_count = 0
        # 单轮限制无论组队还是单人都在副本设置中配置
        self.start_time = datetime.now()
        self.limit_count = config.orochi_config.limit_count
        self.limit_time = self._time_to_delta(config.orochi_config.limit_time)
        self.team_store: TeamStateStore | None = None
        self.team_session: TeamSession | None = None
        self.team_role: str | None = None
        # 御魂加成与结界突破链一律按本实例配置生效，组队时不再从队长同步
        self.team_soul_buff_enable = config.orochi_config.soul_buff_enable
        self.team_enable_realm_raid_chain = config.orochi_config.enable_realm_raid_chain
        self._team_last_heartbeat = 0.0
        self._soul_buff_should_close = False

        # 组队选项：选择组队后进入脚本组队流程，单人时按身份手动组队或单人刷
        is_team = config.team_config.team_mode == TeamMode.TEAM
        if is_team:
            # 组队模式必须先完成 JSON 配对，队员随后使用队长发布的单轮限制。
            self._connect_team()

        self._switch_soul_before_battle()

        success = True
        if is_team:
            success = self._prepare_team_round()
        elif not self.is_in_battle(True):
            self.ui_get_current_page()
            self.ui_goto(page_main)
            if config.orochi_config.soul_buff_enable:
                self._set_soul_buff(True)
                self._soul_buff_should_close = True

        if success:
            if is_team:
                if self.team_role == 'leader':
                    success = self.run_leader()
                else:
                    success = self.run_member()
            else:
                match config.orochi_config.user_status:
                    case UserStatus.LEADER:
                        success = self.run_leader()
                    case UserStatus.MEMBER:
                        success = self.run_member()
                    case UserStatus.ALONE:
                        self.run_alone()
                    case UserStatus.WILD:
                        success = self.run_wild()
                    case _:
                        logger.error('Unknown user status')
                        success = False

        # 任务收尾兜底：仍处于自动战斗状态时补一次取消，避免把自动状态带出任务
        self.auto_battle_finish_sweep()

        # 只有本次流程确认需要开启加成时才关闭，配对失败不会误操作加成开关。
        if self._soul_buff_should_close:
            self._set_soul_buff(False)

        if is_team:
            self._finish_team_task(success)
        else:
            self._finish_local_task(success)

        raise TaskEnd('Orochi')

    @staticmethod
    def _time_to_delta(value: time) -> timedelta:
        return timedelta(hours=value.hour, minutes=value.minute, seconds=value.second)

    @staticmethod
    def _time_to_seconds(value: time) -> int:
        return value.hour * 3600 + value.minute * 60 + value.second

    def _switch_soul_before_battle(self) -> None:
        """保留原有三种换魂方式，但在组队模式下移动到配对成功之后。"""
        # 御魂切换方式一
        if self.config.orochi.switch_soul.enable:
            self.ui_get_current_page()
            self.ui_goto(page_shikigami_records)
            self.run_switch_soul(self.config.orochi.switch_soul.switch_group_team)

        # 御魂切换方式二
        if self.config.orochi.switch_soul.enable_switch_by_name:
            self.ui_get_current_page()
            self.ui_goto(page_shikigami_records)
            self.run_switch_soul_by_name(self.config.orochi.switch_soul.group_name,
                                         self.config.orochi.switch_soul.team_name)
        # 根据选层切换御魂
        self.orochi_switch_soul()

    def _set_soul_buff(self, is_open: bool) -> None:
        """统一开关御魂加成，便于准备屏障严格控制调用时机。"""
        self.open_buff()
        self.soul(is_open=is_open)
        self.close_buff()

    def _sync_epoch(self, progress_epoch: str) -> None:
        """把状态文件中的真实 Epoch 回写本实例，RESET 因此只会消费一次。"""
        config = self.config.orochi.team_config
        if config.epoch == progress_epoch:
            return
        config.epoch = progress_epoch
        self.config.save()

    def _team_wait_seconds(self) -> int:
        wait_time = self.config.orochi.invite_config.wait_time
        return max(10, self._time_to_seconds(wait_time))

    def _pairing_wait_seconds(self, state: dict | None = None) -> int:
        """配对等待超时：对方正在打突破时放宽，避免没突破的一方提前超时。

        优先看对方的"突破进行中"实时标记；标记缺失时退回上一轮的静态推断
        （pairing_needs_realm_raid），覆盖对方还没来得及落标记就被查询的窗口。
        突破耗时远超邀请等待，而换魂/加成屏障仍沿用各自的邀请等待时间。
        """
        base = self._team_wait_seconds()
        if state is None and self.team_store is not None:
            state = self.team_store.read()
        if not state:
            return base
        if (self.team_store is not None
                and self.team_store.realm_raid_pending(state, self._other_role())):
            logger.info('对方正在打结界突破，本次配对使用长等待')
            return max(base, REALM_RAID_PAIRING_WAIT_SECONDS)
        if state.get('pairing_needs_realm_raid'):
            return max(base, REALM_RAID_PAIRING_WAIT_SECONDS)
        return base

    def _other_role(self) -> str:
        """对方角色名，用于查询对方的突破进行中标记。"""
        return 'member' if self.team_role == 'leader' else 'leader'

    def _clear_own_realm_raid_pending(self) -> None:
        """回到御魂就立刻清掉自己的突破标记，好让对方结束等待。"""
        if self.team_store is None or self.team_role is None:
            return
        try:
            self.team_store.clear_realm_raid_pending(self.team_role)
        except Exception as e:
            # 标记只用于缩短对方等待，清不掉最多让对方多等到兜底过期，不能中断任务
            logger.warning(f'清除结界突破进行中标记失败: {e}')

    def _apply_team_round_config(self, state: dict) -> None:
        """队员和队长都使用状态文件中的有效单轮限制，保证只有一个配置来源。

        御魂加成与结界突破链不在此同步：两者已改为各自读本实例配置。
        """
        self.limit_count = int(state.get('effective_limit_count', 0) or 0)
        self.limit_time = timedelta(seconds=int(state.get('effective_limit_seconds', 0) or 0))

    def _schedule_reset_retry(self) -> bool:
        """场次被 RESET 取代时复制状态内的统一时间，避免双方退回各自失败间隔。"""
        if self.team_store is None:
            return False
        state = self.team_store.read()
        if state.get('phase') != PHASE_RESETTING:
            return False
        next_run = parse_state_datetime(state.get('next_orochi_at'))
        if next_run is None:
            return False
        self.set_next_run('Orochi', target=next_run, server=False)
        return True

    def _connect_team(self) -> None:
        """队长发布新 Join Token；队员只加入新鲜且开放的目标队长场次。"""
        team_config = self.config.orochi.team_config
        orochi_config = self.config.orochi.orochi_config
        config_name = self.config.config_name

        if orochi_config.user_status not in {UserStatus.LEADER, UserStatus.MEMBER}:
            # 组队流程依赖队长/队员配对，身份只能是队长或队员
            logger.error('组队流程身份必须选择队长或队员')
            self.set_next_run('Orochi', finish=False, success=False)
            raise TaskEnd('Orochi')

        if orochi_config.user_status == UserStatus.LEADER:
            self.team_role = 'leader'
            self.team_store = TeamStateStore(config_name)
            # 已经回到御魂，先撤掉自己的突破标记，队员才不会继续空等
            self._clear_own_realm_raid_pending()
            if str(team_config.epoch).strip().upper() == RESET_COMMAND:
                reset_state = self.team_store.reset(config_name)
                self._sync_epoch(reset_state['progress_epoch'])

            # 队员可能与队长同时启动并先消费 RESET；检测到 RESETTING 后队长原地
            # 重新发布一次即可，不必让双方等到各自失败调度间隔。
            for _ in range(2):
                try:
                    state = self.team_store.open_round(
                        round_limit_count=orochi_config.limit_count,
                        round_limit_seconds=self._time_to_seconds(orochi_config.limit_time),
                        total_limit_count=team_config.total_limit_count,
                        total_limit_seconds=self._time_to_seconds(team_config.total_limit_time),
                        leader_realm_raid=self.team_enable_realm_raid_chain,
                    )
                except RoundNotDueError as exc:
                    logger.info(f'组队御魂下一轮尚未到时: {exc.next_orochi_at}')
                    self.set_next_run('Orochi', target=exc.next_orochi_at, server=False)
                    raise TaskEnd('Orochi')

                self._sync_epoch(state['progress_epoch'])
                if state.get('phase') == PHASE_FINISHED:
                    logger.info('组队御魂总次数或总时间已完成')
                    self.set_next_run('Orochi', finish=True, success=True)
                    raise TaskEnd('Orochi')

                self.team_session = TeamSession.from_state(state)
                self._apply_team_round_config(state)
                # 上一轮有人打突破时队员会晚回来，这里必须用放宽后的配对超时
                joined_state = self._wait_team_state(
                    lambda value: bool(value.get('member_instance')),
                    timeout=self._pairing_wait_seconds(state),
                )
                if joined_state is not None:
                    return
                current = self.team_store.read()
                if current.get('phase') != PHASE_RESETTING:
                    break
                logger.info('检测到队员 RESET 请求，队长重新发布组队场次')

            logger.warning('等待御魂队员连接超时，本轮不会开启加成')
            return

        leader_instance = str(team_config.leader_instance or '').strip()
        if not leader_instance:
            logger.error('队员未选择 leader_instance，无法进入组队御魂流程')
            self.set_next_run('Orochi', finish=False, success=False)
            raise TaskEnd('Orochi')
        if leader_instance == config_name:
            logger.error('leader_instance 不能选择当前队员实例自身')
            self.set_next_run('Orochi', finish=False, success=False)
            raise TaskEnd('Orochi')

        self.team_role = 'member'
        self.team_store = TeamStateStore(leader_instance)
        # 已经回到御魂，先撤掉自己的突破标记，队长才不会继续空等
        self._clear_own_realm_raid_pending()
        if str(team_config.epoch).strip().upper() == RESET_COMMAND:
            reset_state = self.team_store.reset(config_name)
            self._sync_epoch(reset_state['progress_epoch'])

        # 队长可能正在打结界突破，此时要等它打完回来才会发布新场次；配对超时据此放宽
        pairing_state = self.team_store.read()
        deadline = monotonic() + self._pairing_wait_seconds(pairing_state)
        state = None
        while monotonic() < deadline:
            state = self.team_store.try_join_member(
                config_name,
                member_realm_raid=self.team_enable_realm_raid_chain,
            )
            if state is not None:
                break
            sleep(1)
        if state is None:
            logger.warning(f'未发现队长 {leader_instance} 发布的新组队场次')
            if not self._schedule_reset_retry():
                self._schedule_pairing_retry(pairing_state)
            raise TaskEnd('Orochi')

        self._sync_epoch(state['progress_epoch'])
        self.team_session = TeamSession.from_state(state)
        self._apply_team_round_config(state)

    def _team_session_matches(self, state: dict) -> bool:
        return self.team_session is not None and TeamSession.from_state(state) == self.team_session

    def _team_heartbeat_if_due(self) -> None:
        if self.team_store is None or self.team_session is None or self.team_role is None:
            return
        now = monotonic()
        if now - self._team_last_heartbeat < 5:
            return
        self.team_store.heartbeat(self.team_session, self.team_role)
        self._team_last_heartbeat = now

    def _wait_team_state(self, predicate, timeout: int) -> dict | None:
        """轮询只读状态；运行期间每五秒刷新一次本角色心跳，任务结束后不再写。"""
        deadline = monotonic() + timeout
        while monotonic() < deadline:
            try:
                state = self.team_store.read()
                if not self._team_session_matches(state):
                    raise StaleSessionError('STALE_SESSION')
                if predicate(state):
                    return state
                self._team_heartbeat_if_due()
            except StaleSessionError as exc:
                logger.warning(f'御魂组队场次已失效: {exc}')
                return None
            sleep(1)
        return None

    def _prepare_team_round(self) -> bool:
        """双方换魂完成后才开加成，双方加成就绪后队长才允许邀请。"""
        if self.team_store is None or self.team_session is None:
            return False
        try:
            self.team_store.mark_ready(self.team_session, self.team_role, 'soul')
            if self._wait_team_state(
                lambda state: bool(state.get('leader_soul_ready') and state.get('member_soul_ready')),
                timeout=self._team_wait_seconds(),
            ) is None:
                logger.warning('等待双方换魂完成超时，本轮不会开启加成')
                return False

            if self.team_soul_buff_enable:
                self.ui_get_current_page()
                self.ui_goto(page_main)
                self._set_soul_buff(True)
                self._soul_buff_should_close = True

            self.team_store.mark_ready(self.team_session, self.team_role, 'buff')
            if self._wait_team_state(
                lambda state: bool(state.get('leader_buff_ready') and state.get('member_buff_ready')),
                timeout=self._team_wait_seconds(),
            ) is None:
                logger.warning('等待双方加成就绪超时')
                return False

            if self.team_role == 'leader':
                state = self.team_store.start_inviting(self.team_session)
            else:
                state = self._wait_team_state(
                    lambda value: value.get('phase') in {PHASE_INVITING, PHASE_RUNNING},
                    timeout=self._team_wait_seconds(),
                )
                if state is None:
                    logger.warning('等待队长进入邀请阶段超时')
                    return False
            self._apply_team_round_config(state)
            return True
        except StaleSessionError as exc:
            logger.warning(f'御魂组队准备被新场次取代: {exc}')
            return False

    def _team_limit_reached(self) -> bool:
        if self.team_store is None or self.team_session is None:
            # 手动队长场景没有 JSON 会话，退回本地单轮限制
            if self.current_count >= self.limit_count:
                return True
            return datetime.now() - self.start_time >= self.limit_time
        try:
            self._team_heartbeat_if_due()
            return self.team_store.round_limit_reached(self.team_session)
        except StaleSessionError:
            return True

    def _team_can_start_battle(self) -> bool:
        if self.team_store is None or self.team_session is None:
            return True
        try:
            self._team_heartbeat_if_due()
            return self.team_store.can_start_battle(self.team_session)
        except StaleSessionError:
            return False

    def _run_team_invite(self, is_first: bool = False) -> bool:
        """记录 click_fire 的最终门禁结果，避免 run_invite 把被拦截误判为成功。"""
        self._team_fire_blocked = False
        invited = self.run_invite(
            config=self.config.orochi.invite_config,
            is_first=is_first,
        )
        return invited and not self._team_fire_blocked

    def click_fire(self):
        """在每次实际点击挑战前同时确认游戏房间与当前 JSON 会话状态。"""
        if getattr(self, 'team_store', None) is None:
            return super().click_fire()
        while 1:
            self.screenshot()
            if not self.is_in_room(False):
                return True
            if not self._team_can_start_battle():
                logger.warning('组队 JSON 状态失效，禁止点击挑战')
                self._team_fire_blocked = True
                return False
            if self.appear_then_click(self.I_FIRE, interval=1, threshold=0.7):
                continue
            if self.appear_then_click(self.I_FIRE_SEA, interval=1, threshold=0.7):
                continue

    def _schedule_pairing_retry(self, state: dict | None = None) -> None:
        """配对超时的退避：对方在打突破时用短间隔重试，而不是整段失败间隔。

        failure_interval 默认是一天，如果只因为对方还在打突破就退避一天，等于整个
        组队流程被一次超时废掉。这种轮次改为几分钟后重试，让它自然等到对方回来。
        """
        if state is None and self.team_store is not None:
            state = self.team_store.read()
        pending = bool(
            state
            and self.team_store is not None
            and self.team_store.realm_raid_pending(state, self._other_role())
        )
        if pending or (state and state.get('pairing_needs_realm_raid')):
            target = datetime.now().replace(microsecond=0) + timedelta(minutes=3)
            logger.info(f'对方可能仍在打结界突破，稍后重试配对: {target}')
            self.set_next_run('Orochi', target=target, server=False)
            return
        self.set_next_run('Orochi', finish=False, success=False)

    def _chain_realm_raid(self) -> None:
        """拉起结界突破：未启用先启用，并把时间设为当下立即执行。

        时间必须是"现在"而不是往后推：只要留出等待窗口，窗口内任何到点的任务都
        可能被调度器优先选走，突破就被插队挤到后面。RealmRaid 在 SCHEDULER_PRIORITY
        中排在 Orochi 之前，同一时刻到点时突破先跑，回来正好接上下一轮御魂。

        启用必须先落盘再改时间：set_next_run 内部会 reload 配置，未保存的启用会被丢弃。
        """
        # 先告知队友"我要去打突破了"，它据此切换到长等待而不是按邀请超时退避
        if self.team_store is not None and self.team_role is not None:
            try:
                self.team_store.mark_realm_raid_pending(self.team_role)
            except Exception as e:
                # 标记只影响对方等多久，写不进去也必须继续把突破拉起来
                logger.warning(f'写入结界突破进行中标记失败: {e}')

        scheduler = self.config.model.realm_raid.scheduler
        if not scheduler.enable:
            logger.info('结界突破未启用，本次自动启用以便拉起')
            scheduler.enable = True
            self.config.save()
        target = datetime.now().replace(microsecond=0)
        logger.info(f'拉起结界突破，立即执行: {target}')
        self.set_next_run('RealmRaid', target=target, server=False)

    def _finish_team_task(self, success: bool) -> None:
        """队长唯一计算调度时间；队员等队长落盘后复制同一时间。

        结界突破改为各自按本地配置拉起，不再由队长下发时间。
        """
        if self.team_store is None or self.team_session is None:
            # 手动队员没有 JSON 会话，沿用单人调度与 RealmRaid 链
            self._finish_local_task(success)
            return

        state = None
        if self.team_role == 'leader':
            # 下一轮设在一分钟后：本轮若要打突破，突破会因优先级更高而先跑，
            # 跑完御魂正好到点接上；无人打突破时总量已一次打完，不会走到这里。
            next_orochi_at = datetime.now().replace(microsecond=0) + timedelta(minutes=1)
            try:
                state = self.team_store.finish_round(
                    self.team_session,
                    success=success,
                    next_orochi_at=next_orochi_at,
                )
            except StaleSessionError as exc:
                logger.warning(f'御魂组队结束状态写入失败: {exc}')
        else:
            state = self._wait_team_state(
                lambda value: value.get('phase') in {PHASE_WAIT_NEXT_ROUND, PHASE_FINISHED},
                timeout=max(60, self._team_wait_seconds()),
            )

        if state is None:
            if not self._schedule_reset_retry():
                self._schedule_pairing_retry()
            # 调度写失败也不该吞掉本地突破链，否则本轮的突破整轮丢失
            if self.team_enable_realm_raid_chain:
                self._chain_realm_raid()
            return

        if state.get('phase') == PHASE_WAIT_NEXT_ROUND:
            next_run = parse_state_datetime(state.get('next_orochi_at'))
            if next_run is not None:
                self.set_next_run('Orochi', target=next_run, server=False)
            else:
                self.set_next_run('Orochi', finish=False, success=False)
        else:
            self.set_next_run('Orochi', finish=True, success=bool(state.get('round_success')))

        # 各自按本实例配置拉起突破，与对方是否开启无关
        if self.team_enable_realm_raid_chain:
            self._chain_realm_raid()

    def _finish_local_task(self, success: bool) -> None:
        """单人和野队保持原有调度行为。"""
        if success:
            self.set_next_run('Orochi', finish=True, success=True)
        else:
            self.set_next_run('Orochi', finish=False, success=False)

        # 根据配置决定是否拉起RealmRaid任务
        if self.config.orochi.orochi_config.enable_realm_raid_chain:
            logger.info("Orochi task completed, starting RealmRaid task")
            self._chain_realm_raid()
        else:
            logger.info("Orochi task completed, RealmRaid chain disabled")

    def _battle_count_settle(self) -> None:
        """一场战斗结束后的计数收尾：五倍券补次 + 组队进度/心跳。

        从原 run_general_battle 重写抽出，供手动场（run_general_battle）与
        自动段（auto_battle_count_hook）共用，保证两处口径一致。
        五倍券判断收在内部：进段前有券即可用，允许超额（原语义不变）。
        """
        orochi_config = self.config.orochi.orochi_config
        # 判断本场是否使用五倍券：已开启且仍有券即可，允许超额
        use_ticket = (orochi_config.five_times_enable
                      and orochi_config.five_times_ticket > 0)
        if use_ticket:
            # 五倍券生效：本场已计 1 次，这里再补 4 次凑成一场抵 5 次
            self.current_count += 4
            # 扣减一张券并立即回写，保证下次运行读到真实剩余
            orochi_config.five_times_ticket -= 1
            self.config.save()
            logger.info(f'Five times ticket used, remaining: {orochi_config.five_times_ticket}, '
                        f'current_count: {self.current_count}/{self.limit_count}')
        # 组队总进度只允许队长写；队员仅刷新心跳，避免双方计数差异造成重复累计。
        team_store = getattr(self, 'team_store', None)
        team_session = getattr(self, 'team_session', None)
        if team_store is not None and team_session is not None:
            try:
                if self.team_role == 'leader':
                    team_store.update_progress(team_session, self.current_count)
                else:
                    team_store.heartbeat(team_session, 'member')
                    self._team_last_heartbeat = monotonic()
            except StaleSessionError as exc:
                logger.warning(f'战斗完成时组队场次已失效: {exc}')

    def auto_battle_count_hook(self) -> None:
        """自动段内每完成一场的计数钩子：走与手动场同一份收尾口径（spec 4.5.2）。"""
        self._battle_count_settle()

    def auto_battle_remaining_now(self) -> int:
        """自动段截断口径：本地限制 - 当前计数（五倍券 +4 已计入，偏保守）。"""
        return self.limit_count - self.current_count

    def run_general_battle(self, config=None, buff=None) -> bool:
        """
        重写通用战斗：支持五倍消耗
        父类 run_general_battle 内部会执行 self.current_count += 1（一次战斗计 1 次）。
        当开启五倍消耗且仍有券时，本次战斗视为 5 次：
        父类已计 1 次，这里再补 4 次，并扣减一张五倍券后立即回写 config，
        保证下次运行读取到的是真实剩余券数。
        能进入本方法说明尚未达到目标次数（否则外层循环已退出），
        因此只要有券就用券，允许略微超过目标次数（例如目标 99、每战 +5 时最终为 100）。
        :param config: 通用战斗配置
        :param buff: 战斗加成
        :return: 是否胜利
        """
        # 五倍券的计数口径是"一场抵 5 次"（current_count 每场 +5），与自动段的"真实场数"
        # 单位错配会导致超额多打；有券期间 fail-closed 禁用自动段（不传 remaining），
        # 券用完后自动恢复。告警只在首次触发时打一次，避免每场刷屏
        orochi_config = self.config.orochi.orochi_config
        if orochi_config.five_times_enable and orochi_config.five_times_ticket > 0:
            if not getattr(self, '_auto_seg_ticket_blocked', False):
                logger.warning('五倍券启用期间禁用自动战斗段（次数/场数口径错配），券用完后自动恢复')
                self._auto_seg_ticket_blocked = True
            remaining = None
        else:
            # 自动段规划口径：调用前的剩余次数（含本场，与序言计数时序对齐）
            remaining = self.limit_count - self.current_count
        result = super().run_general_battle(config=config, buff=buff,
                                            remaining_count=remaining)
        self._battle_count_settle()
        return result

    def orochi_enter(self) -> bool:
        logger.info('Enter orochi')
        while True:
            self.screenshot()
            if self.appear(self.I_FORM_TEAM):
                return True
            if self.appear_then_click(self.I_OROCHI, interval=1):
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
        检查是否锁定阵容, 要求在八岐大蛇界面
        :param lock:
        :return:
        """
        logger.info('Check lock: %s', lock)
        if lock:
            while 1:
                self.screenshot()
                if self.appear(self.I_OROCHI_LOCK):
                    return True
                if self.appear_then_click(self.I_OROCHI_UNLOCK, interval=1):
                    continue
        else:
            while 1:
                self.screenshot()
                if self.appear(self.I_OROCHI_UNLOCK):
                    return True
                if self.appear_then_click(self.I_OROCHI_LOCK, interval=1):
                    continue











    def run_leader(self):
        logger.info('Start run leader')
        self.ui_get_current_page()
        self.ui_goto(page_soul_zones)
        self.orochi_enter()
        layer = self.config.orochi.orochi_config.layer
        self.check_layer(layer)
        # https://github.com/runhey/OnmyojiAutoScript/issues/592
        self.config.orochi.general_battle_config.lock_team_enable = True
        self.check_lock(self.config.orochi.general_battle_config.lock_team_enable)
        # 创建队伍
        logger.info('Create team')
        while 1:
            self.screenshot()
            if self.appear(self.I_CHECK_TEAM):
                break
            if self.appear_then_click(self.I_FORM_TEAM, interval=1):
                continue
        # 创建房间
        if not self.create_room():
            return False
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
            if self.check_and_invite(self.config.orochi.invite_config.default_invite):
                continue

            # 检查猫咪奖励
            if self.appear_then_click(self.I_PET_PRESENT, action=self.C_WIN_3, interval=1):
                continue

            if self._team_limit_reached():
                if self.is_in_room():
                    logger.info('Orochi count limit out')
                    break



            # 如果没有进入房间那就不需要后面的邀请
            if not self.is_in_room():
                if self.is_room_dead():
                    logger.warning('Orochi task failed')
                    success = False
                    break
                continue

            # 点击挑战
            if not is_first:
                # JSON 准备状态与游戏内房间人数必须同时满足，才允许点击挑战。
                if not self._team_can_start_battle():
                    logger.warning('Team JSON state is not ready, stop inviting')
                    success = False
                    break
                if self._run_team_invite():
                    if self.team_store is not None:
                        self.team_store.mark_running(self.team_session)
                    self.run_general_battle(config=self.config.orochi.general_battle_config)
                else:
                    # 邀请失败，退出任务
                    logger.warning('Invite failed and exit this orochi task')
                    success = False
                    break

            # 第一次会邀请队友
            if is_first:
                if not self._team_can_start_battle():
                    logger.warning('Team JSON state is not ready, stop first invitation')
                    success = False
                    break
                if not self._run_team_invite(is_first=True):
                    logger.warning('Invite failed and exit this orochi task')
                    success = False
                    break
                else:
                    is_first = False
                    if self.team_store is not None:
                        self.team_store.mark_running(self.team_session)
                    self.run_general_battle(config=self.config.orochi.general_battle_config)

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
        # self.orochi_enter()
        # self.check_lock(self.config.orochi.general_battle_config.lock_team_enable)

        # 进入战斗流程
        self.device.stuck_record_add('BATTLE_STATUS_S')
        while 1:
            self.screenshot()

            # 检查猫咪奖励
            if self.appear_then_click(self.I_PET_PRESENT, action=self.C_WIN_3, interval=1):
                continue

            if self._team_limit_reached():
                logger.info('Orochi count limit out')
                break

            if self.check_then_accept():
                continue

            if self.is_in_room():
                self.device.stuck_record_clear()
                if self.wait_battle(wait_time=self.config.orochi.invite_config.wait_time):
                    self.run_general_battle(config=self.config.orochi.general_battle_config)
                else:
                    break
            # 队长秒开的时候，检测是否进入到战斗中
            elif self.check_take_over_battle(False, config=self.config.orochi.general_battle_config):
                continue

        while 1:
            self.screenshot()
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

    def run_alone(self):
        logger.info('Start run alone')
        self.ui_get_current_page()
        self.ui_goto(page_soul_zones)
        self.orochi_enter()
        layer = self.config.orochi.orochi_config.layer
        self.check_layer(layer)
        self.check_lock(self.config.orochi.general_battle_config.lock_team_enable)

        def is_in_orochi(screenshot=False) -> bool:
            if screenshot:
                self.screenshot()
            return self.appear(self.I_OROCHI_FIRE)

        while 1:
            self.screenshot()

            # 检查猫咪奖励
            if self.appear_then_click(self.I_PET_PRESENT, action=self.C_WIN_3, interval=1):
                continue

            if not is_in_orochi():
                continue

            if self.current_count >= self.limit_count:
                logger.info('Orochi count limit out')
                break
            if datetime.now() - self.start_time >= self.limit_time:
                logger.info('Orochi time limit out')
                break

            # 点击挑战
            while 1:
                self.screenshot()
                if self.appear_then_click(self.I_OROCHI_FIRE, interval=1):
                    pass

                if not self.appear(self.I_OROCHI_FIRE):
                    self.run_general_battle(config=self.config.orochi.general_battle_config)
                    break

        # 回去
        while 1:
            self.screenshot()
            if not self.appear(self.I_FORM_TEAM):
                break
            if self.appear_then_click(self.I_UI_BACK_YELLOW, interval=1):
                continue

        self.ui_current = page_soul_zones
        self.ui_goto(page_main)

    def run_wild(self):
        logger.info('Start run wild')

        # 已经在战斗中不必初始化，保证已经组队开始战斗的情况下可以自动执行后续任务
        if not self.is_in_battle(True):
            self.ui_get_current_page()
            self.ui_goto(page_soul_zones)
            self.orochi_enter()
            layer = self.config.orochi.orochi_config.layer
            self.check_layer(layer)
            self.check_lock(self.config.orochi.general_battle_config.lock_team_enable)
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
            self.ensure_public()
            self.create_ensure()

        success = True
        while 1:
            self.screenshot()
            # 无论胜利与否, 都会出现是否邀请一次队友
            # 区别在于，失败的话不会出现那个勾选默认邀请的框
            if self.check_and_invite(self.config.orochi.invite_config.default_invite):
                continue

            # 检查猫咪奖励
            if self.appear_then_click(self.I_PET_PRESENT, action=self.C_WIN_3, interval=1):
                continue

            if self.current_count >= self.limit_count:
                if self.is_in_room():
                    logger.info('Orochi count limit out')
                    break

            if datetime.now() - self.start_time >= self.limit_time:
                if self.is_in_room():
                    logger.info('Orochi time limit out')
                    break

            if not self.is_in_room():
                if self.is_room_dead():
                    logger.warning('Orochi task failed')
                    success = False
                    break
                continue

            # 点击挑战
            logger.info('Wait for starting')
            while 1:
                self.screenshot()
                # 在进入战斗前必然会出现挑战界面，因此点击失败必须重复点击，防止卡在挑战界面，
                # 点击成功后如果网络卡顿，导致没有进入战斗，则无法进入 run_general_battle 流程，
                # 所以如果判断是在战斗中，则执行通用战斗流程
                if not self.is_in_battle(False):
                    if not self.is_in_room() and self.is_room_dead():
                        break
                    if not self.appear_then_click(self.I_OROCHI_WILD_FIRE, interval=1, threshold=0.8):
                        continue

                self.screenshot()
                if not self.appear(self.I_OROCHI_WILD_FIRE, threshold=0.8):
                    self.run_general_battle(config=self.config.orochi.general_battle_config)
                    break

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

    def is_room_dead(self) -> bool:
        # 如果在探索界面或者是出现在组队界面，那就是可能房间死了
        sleep(0.5)
        if self.appear(self.I_MATCHING) or self.appear(self.I_CHECK_EXPLORATION):
            sleep(0.5)
            if self.appear(self.I_MATCHING) or self.appear(self.I_CHECK_EXPLORATION):
                return True
        return False

    def battle_wait(self, random_click_swipt_enable: bool) -> bool:
        """
        重写战斗等待
        # https://github.com/runhey/OnmyojiAutoScript/issues/95
        :param random_click_swipt_enable:
        :return:
        """
        # 重写
        self.device.stuck_record_add('BATTLE_STATUS_S')
        self.device.click_record_clear()
        # 战斗过程 随机点击和滑动 防封
        logger.info("Start battle process")
        while 1:
            self.screenshot()
            # 点击赢了/领奖励：全屏减去常驻禁点区域与检测出的奖励行（与基类同一套安全落点）；
            # 结算场景按概率连点（双击/三击），见 settlement_click；
            # 胜利画面 I_WIN/I_WIN_2 共判
            action_click = weighted_choice(self.reward_click_actions())
            if (self.settlement_click(self.I_WIN, action_click, interval=0.8) or
                    self.settlement_click(self.I_WIN_2, action_click, interval=0.8)):
                # 赢的那个鼓
                continue
            if self.appear(self.I_GREED_GHOST):
                # 贪吃鬼
                logger.info('Win battle')
                self.wait_until_appear(self.I_REWARD, wait_time=1.5)
                self.screenshot()
                if not self.appear(self.I_GREED_GHOST):
                    logger.warning('Greedy ghost disappear. Maybe it is a false battle')
                    continue
                while 1:
                    self.screenshot()
                    action_click = weighted_choice(self.reward_click_actions())
                    if not self.appear(self.I_GREED_GHOST):
                        break
                    # 结算贪吃鬼场景按概率连点（双击/三击），见 settlement_gesture
                    if self.appear(self.I_GREED_GHOST, interval=1.5):
                        self.settlement_gesture(action_click, control_name=self.I_GREED_GHOST.name)
                        continue
                return True
            if self.appear(self.I_REWARD)\
                or self.appear(self.I_GREED_GHOST)\
                or self.appear(self.I_REWARD_PURPLE_SNAKE_SKIN)\
                or self.appear(self.I_REWARD_GOLD):
                # 魂
                logger.info('Win battle')
                while 1:
                    self.screenshot()
                    action_click = weighted_choice(self.reward_click_actions())
                    # I_REWARD 失配时的兜底：只要还检测到奖励框就照样点安全区域
                    if (self.settlement_click(self.I_REWARD, action_click, interval=1.5) or
                            self.settlement_click_grid(action_click, interval=1.5)):
                        continue
                    if not self.appear(self.I_REWARD) and not self.reward_grid_appear():
                        break
                return True

            if self.appear(self.I_FALSE):
                logger.warning('False battle')
                self.ui_click_until_disappear(self.I_FALSE)
                return False

            # 如果开启战斗过程随机滑动
            if random_click_swipt_enable:
                self.random_click_swipt()

    def orochi_switch_soul(self) -> None:
        # 判断是否开启根据选层切换御魂
        orochi_switch_soul = self.config.orochi.switch_soul
        if not orochi_switch_soul.auto_switch_soul:
            return

        group_team: str = None
        layer = self.config.orochi.orochi_config.layer
        match layer:
            case Layer.TEN:
                group_team = orochi_switch_soul.ten_switch
            case Layer.ELEVEN:
                group_team = orochi_switch_soul.eleven_switch
            case Layer.TWELVE:
                group_team = orochi_switch_soul.twelve_switch
            case Layer.THIRTEEN:
                group_team = orochi_switch_soul.thirteen_switch

        if orochi_switch_soul.auto_switch_soul:
            self.ui_get_current_page()
            self.ui_goto(page_shikigami_records)
            self.run_switch_soul(group_team)


if __name__ == '__main__':
    from module.config.config import Config
    from module.device.device import Device
    c = Config('oas2')
    d = Device(c)
    t = ScriptTask(c, d)

    t.run()
