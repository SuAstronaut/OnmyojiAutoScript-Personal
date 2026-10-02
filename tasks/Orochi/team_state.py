# This Python file uses the following encoding: utf-8
"""御魂组队实例间的原子 JSON 状态存储。"""
import json
import os
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import quote

from filelock import FileLock

from module.logger import logger


RESET_COMMAND = 'RESET'

PHASE_RESETTING = 'RESETTING'
PHASE_WAIT_MEMBER = 'WAIT_MEMBER'
PHASE_PREPARING = 'PREPARING'
PHASE_INVITING = 'INVITING'
PHASE_RUNNING = 'RUNNING'
PHASE_WAIT_NEXT_ROUND = 'WAIT_NEXT_ROUND'
PHASE_FINISHED = 'FINISHED'

ACTIVE_PHASES = {
    PHASE_WAIT_MEMBER,
    PHASE_PREPARING,
    PHASE_INVITING,
    PHASE_RUNNING,
}
TERMINAL_PHASES = {PHASE_WAIT_NEXT_ROUND, PHASE_FINISHED}

# 配对阶段要求队长持续刷新状态，避免队员误连上次遗留的 WAIT_MEMBER。
LEADER_FRESH_SECONDS = 15

# 轮次之间有人要打结界突破时，另一方必须等对方打完突破才能配对成功。突破耗时
# 远超邀请等待时间：30 张突破票可能要打 15 分钟以上，因此这种轮次用独立的长
# 超时（留一倍余量），避免没突破的一方提前超时退避。
REALM_RAID_PAIRING_WAIT_SECONDS = 1800

# 突破进行中标记的兜底有效期。正常情况下打完突破的一方回到御魂时会自行清除标记，
# 但如果它中途崩溃或被用户停掉，标记会一直挂着；超过这个时长就不再认它，
# 避免另一方无限等待一个永远不会回来的队友。
REALM_RAID_PENDING_EXPIRE_SECONDS = 2400



class StaleSessionError(RuntimeError):
    """调用方携带的会话令牌已过期，禁止覆盖当前场次。"""


class RoundNotDueError(RuntimeError):
    """上一轮已结束，但队长配置的下一轮时间尚未到达。"""

    def __init__(self, next_orochi_at: datetime):
        super().__init__(str(next_orochi_at))
        self.next_orochi_at = next_orochi_at


@dataclass(frozen=True)
class TeamSession:
    """所有写操作必须携带的不可变会话标识。"""

    progress_epoch: str
    session_id: str
    join_token: str
    round_id: int

    @classmethod
    def from_state(cls, state: dict) -> 'TeamSession':
        return cls(
            progress_epoch=str(state.get('progress_epoch', '')),
            session_id=str(state.get('session_id', '')),
            join_token=str(state.get('join_token', '')),
            round_id=int(state.get('round_id', 0)),
        )


def _now() -> datetime:
    return datetime.now().replace(microsecond=0)


def _iso(value: datetime | None) -> str | None:
    return value.isoformat(sep=' ') if value is not None else None


def _parse_datetime(value) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def _elapsed_seconds(start, end) -> int:
    start_time = _parse_datetime(start)
    end_time = _parse_datetime(end)
    if start_time is None or end_time is None or end_time < start_time:
        return 0
    return int((end_time - start_time).total_seconds())


class TeamStateStore:
    """按队长实例名保存一份组队状态，并用 FileLock 串行化读改写。"""

    def __init__(self, leader_instance: str, base_dir='config/tasks_config'):
        leader_instance = str(leader_instance or '').strip()
        if not leader_instance:
            raise ValueError('leader_instance cannot be empty')
        self.leader_instance = leader_instance
        # quote 防止实例名中的空格或非 ASCII 字符直接参与 Windows 路径解析。
        filename = quote(leader_instance.casefold(), safe='-_.')
        self.path = Path(base_dir) / f'orochi_team_{filename}.json'
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.lock = FileLock(f'{self.path}.lock', timeout=10)

    # -------------------------------- 底层原子读写 --------------------------------

    def _read_unlocked(self) -> dict:
        if not self.path.exists():
            return {}
        try:
            with open(self.path, 'r', encoding='utf-8') as file:
                data = json.load(file)
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            # 损坏文件按无状态处理，队长下一次发布会原子覆盖重建。
            logger.warning(f'御魂组队状态文件损坏，将等待队长重建: {exc}')
            return {}

    def _write_unlocked(self, data: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = self.path.with_suffix('.json.tmp')
        with open(tmp_path, 'w', encoding='utf-8') as file:
            json.dump(data, file, ensure_ascii=False, indent=2)
        os.replace(tmp_path, self.path)

    def read(self) -> dict:
        with self.lock:
            return self._read_unlocked()

    def _update(self, callback) -> dict:
        with self.lock:
            data = self._read_unlocked()
            updated = callback(data)
            if updated is None:
                return data
            self._write_unlocked(updated)
            return updated

    # -------------------------------- Epoch 与场次 --------------------------------

    def reset(self, requested_by: str) -> dict:
        """消费固定 RESET 命令，清空累计进度并生成不可复用的新 Epoch。"""
        now = _now()
        state = {
            'version': 1,
            'leader_instance': self.leader_instance,
            'progress_epoch': uuid.uuid4().hex,
            'session_id': '',
            'join_token': '',
            'round_id': 0,
            'phase': PHASE_RESETTING,
            'accept_member': False,
            'member_instance': '',
            'leader_soul_ready': False,
            'member_soul_ready': False,
            'leader_buff_ready': False,
            'member_buff_ready': False,
            'total_count': 0,
            'total_elapsed_seconds': 0,
            'round_count': 0,
            'round_started_at': None,
            'round_finished_at': None,
            'round_success': None,
            # 双方各自声明本地是否要打结界突破，决定是否需要分轮以及配对等待时长
            'leader_realm_raid': False,
            'member_realm_raid': False,
            'pairing_needs_realm_raid': False,
            # 突破进行中标记：非空表示该方此刻正在打突破，回到御魂时由它自己清除
            'leader_realm_raid_pending_at': None,
            'member_realm_raid_pending_at': None,
            'reset_by': str(requested_by),
            # RESET 发生在运行中时，旧会话双方都能复制同一个兜底重试时间。
            'next_orochi_at': _iso(now + timedelta(minutes=10)),
            'updated_at': _iso(now),
        }
        with self.lock:
            self._write_unlocked(state)
        return state

    @staticmethod
    def _new_progress(leader_instance: str, progress_epoch: str | None = None) -> dict:
        return {
            'version': 1,
            'leader_instance': leader_instance,
            'progress_epoch': progress_epoch or uuid.uuid4().hex,
            'round_id': 0,
            'total_count': 0,
            'total_elapsed_seconds': 0,
        }

    @staticmethod
    def _commit_interrupted_elapsed(state: dict) -> None:
        """重启时只累计到最后一次心跳，不能把脚本离线时间算成战斗时间。"""
        elapsed = _elapsed_seconds(state.get('round_started_at'), state.get('leader_seen_at'))
        if elapsed <= 0:
            return
        limit = int(state.get('effective_limit_seconds', 0) or 0)
        if limit > 0:
            elapsed = min(elapsed, limit)
        state['total_elapsed_seconds'] = int(state.get('total_elapsed_seconds', 0)) + elapsed

    def open_round(
        self,
        round_limit_count: int,
        round_limit_seconds: int,
        total_limit_count: int,
        total_limit_seconds: int,
        leader_realm_raid: bool,
        now: datetime | None = None,
    ) -> dict:
        """由队长发布新一轮配置；WAIT_NEXT_ROUND 未到点时拒绝提前覆盖。

        本轮单轮限制此时只按"总量剩余"发布，真正的分轮收缩推迟到 start_inviting：
        是否需要分轮取决于双方突破声明的并集，而队员声明要等它 join 之后才可见。
        """
        now = (now or _now()).replace(microsecond=0)

        def mutate(state: dict) -> dict:
            phase = state.get('phase')
            if state.get('leader_instance') != self.leader_instance:
                state = {}
                phase = None

            if phase == PHASE_WAIT_NEXT_ROUND:
                next_run = _parse_datetime(state.get('next_orochi_at'))
                if next_run is not None and now < next_run:
                    raise RoundNotDueError(next_run)

            # 上一轮遗留的"需要等对方打突破"标记要跨轮保留，配对超时据此放宽
            pairing_needs_realm_raid = bool(state.get('pairing_needs_realm_raid'))

            if not state or phase == PHASE_FINISHED:
                state = self._new_progress(self.leader_instance)
            elif phase == PHASE_RESETTING:
                state = self._new_progress(
                    self.leader_instance,
                    progress_epoch=str(state.get('progress_epoch') or uuid.uuid4().hex),
                )
            elif phase in ACTIVE_PHASES:
                # 上次进程中断时保留已确认的战斗次数，并结算到最后一次心跳的战斗时间。
                self._commit_interrupted_elapsed(state)
            elif phase != PHASE_WAIT_NEXT_ROUND:
                state = self._new_progress(self.leader_instance)

            total_count = max(0, int(state.get('total_count', 0) or 0))
            total_elapsed = max(0, int(state.get('total_elapsed_seconds', 0) or 0))
            total_limit_count_value = max(0, int(total_limit_count))
            total_limit_seconds_value = max(0, int(total_limit_seconds))
            remaining_count = max(0, total_limit_count_value - total_count)
            remaining_seconds = max(0, total_limit_seconds_value - total_elapsed)

            state.update({
                'total_limit_count': total_limit_count_value,
                'total_limit_seconds': total_limit_seconds_value,
                'total_count': total_count,
                'total_elapsed_seconds': total_elapsed,
                'updated_at': _iso(now),
                'leader_seen_at': _iso(now),
            })
            if remaining_count <= 0 or remaining_seconds <= 0:
                state.update({
                    'phase': PHASE_FINISHED,
                    'accept_member': False,
                    'next_orochi_at': None,
                })
                return state

            state.update({
                'session_id': uuid.uuid4().hex,
                'join_token': uuid.uuid4().hex,
                'round_id': int(state.get('round_id', 0) or 0) + 1,
                'phase': PHASE_WAIT_MEMBER,
                'accept_member': True,
                'member_instance': '',
                'member_seen_at': None,
                'leader_soul_ready': False,
                'member_soul_ready': False,
                'leader_buff_ready': False,
                'member_buff_ready': False,
                'configured_limit_count': max(0, int(round_limit_count)),
                'configured_limit_seconds': max(0, int(round_limit_seconds)),
                # 先按总量剩余发布，start_inviting 再按双方突破声明决定是否收缩为单轮
                'effective_limit_count': remaining_count,
                'effective_limit_seconds': remaining_seconds,
                'remaining_limit_count': remaining_count,
                'remaining_limit_seconds': remaining_seconds,
                'leader_realm_raid': bool(leader_realm_raid),
                'member_realm_raid': False,
                'pairing_needs_realm_raid': pairing_needs_realm_raid,
                'round_count': 0,
                'round_started_at': None,
                'round_finished_at': None,
                'round_success': None,
                'next_orochi_at': None,
            })
            return state

        return self._update(mutate)

    # -------------------------------- 配对与准备屏障 --------------------------------

    @staticmethod
    def _require_session(state: dict, session: TeamSession) -> None:
        current = TeamSession.from_state(state)
        if current != session:
            raise StaleSessionError('STALE_SESSION')

    @staticmethod
    def _leader_is_fresh(state: dict, now: datetime) -> bool:
        leader_seen_at = _parse_datetime(state.get('leader_seen_at'))
        if leader_seen_at is None:
            return False
        return 0 <= (now - leader_seen_at).total_seconds() <= LEADER_FRESH_SECONDS

    def try_join_member(self, member_instance: str, member_realm_raid: bool,
                        now: datetime | None = None) -> dict | None:
        """仅加入队长刚发布且仍开放的 WAIT_MEMBER，旧 JSON 不会被直接复用。

        队员在这里同时声明本地突破开关，供队长在 start_inviting 决定是否分轮。
        """
        now = (now or _now()).replace(microsecond=0)
        joined = None

        def mutate(state: dict) -> dict:
            nonlocal joined
            if state.get('phase') != PHASE_WAIT_MEMBER or not state.get('accept_member'):
                return None
            if not self._leader_is_fresh(state, now):
                return None
            bound_member = str(state.get('member_instance') or '')
            if bound_member and bound_member != member_instance:
                return None
            state.update({
                'phase': PHASE_PREPARING,
                'accept_member': False,
                'member_instance': member_instance,
                'member_realm_raid': bool(member_realm_raid),
                'member_seen_at': _iso(now),
                'updated_at': _iso(now),
            })
            joined = state
            return state

        self._update(mutate)
        return joined

    def heartbeat(self, session: TeamSession, role: str, now: datetime | None = None) -> dict:
        now = (now or _now()).replace(microsecond=0)

        def mutate(state: dict) -> dict:
            self._require_session(state, session)
            if state.get('phase') in TERMINAL_PHASES:
                return None
            state[f'{role}_seen_at'] = _iso(now)
            state['updated_at'] = _iso(now)
            return state

        return self._update(mutate)

    def mark_ready(self, session: TeamSession, role: str, stage: str) -> dict:
        if role not in {'leader', 'member'} or stage not in {'soul', 'buff'}:
            raise ValueError('invalid ready marker')

        def mutate(state: dict) -> dict:
            self._require_session(state, session)
            if state.get('phase') != PHASE_PREPARING:
                raise StaleSessionError('STALE_PHASE')
            state[f'{role}_{stage}_ready'] = True
            state[f'{role}_seen_at'] = _iso(_now())
            state['updated_at'] = _iso(_now())
            return state

        return self._update(mutate)

    def start_inviting(self, session: TeamSession, now: datetime | None = None) -> dict:
        """只有 JSON 中双方换魂、加成均就绪时，队长才可进入邀请阶段。

        同时在这里定稿本轮单轮限制：双方都不打突破就一次性打完总量剩余，不分轮；
        只要有一方要打突破，就收缩到单轮配置，让突破得以插在轮次之间。
        """
        now = (now or _now()).replace(microsecond=0)

        def mutate(state: dict) -> dict:
            self._require_session(state, session)
            ready = all(state.get(key) for key in (
                'leader_soul_ready', 'member_soul_ready',
                'leader_buff_ready', 'member_buff_ready',
            ))
            if state.get('phase') != PHASE_PREPARING or not ready:
                raise StaleSessionError('TEAM_NOT_READY')

            needs_realm_raid = bool(state.get('leader_realm_raid') or state.get('member_realm_raid'))
            remaining_count = max(0, int(state.get('remaining_limit_count', 0) or 0))
            remaining_seconds = max(0, int(state.get('remaining_limit_seconds', 0) or 0))
            if needs_realm_raid:
                # 有人要打突破：按单轮配置切分，轮次之间留出打突破的时间
                effective_count = min(max(0, int(state.get('configured_limit_count', 0) or 0)), remaining_count)
                effective_seconds = min(max(0, int(state.get('configured_limit_seconds', 0) or 0)), remaining_seconds)
            else:
                # 双方都不打突破：没有插入突破的需要，一次性打完总量剩余
                effective_count = remaining_count
                effective_seconds = remaining_seconds

            state.update({
                'phase': PHASE_INVITING,
                'needs_realm_raid': needs_realm_raid,
                'effective_limit_count': effective_count,
                'effective_limit_seconds': effective_seconds,
                'round_started_at': _iso(now),
                'leader_seen_at': _iso(now),
                'updated_at': _iso(now),
            })
            return state

        return self._update(mutate)

    def can_start_battle(self, session: TeamSession) -> bool:
        state = self.read()
        self._require_session(state, session)
        if state.get('phase') not in {PHASE_INVITING, PHASE_RUNNING}:
            return False
        return all(state.get(key) for key in (
            'leader_soul_ready', 'member_soul_ready',
            'leader_buff_ready', 'member_buff_ready',
        ))

    def mark_running(self, session: TeamSession) -> dict:
        def mutate(state: dict) -> dict:
            self._require_session(state, session)
            if state.get('phase') not in {PHASE_INVITING, PHASE_RUNNING}:
                raise StaleSessionError('STALE_PHASE')
            state['phase'] = PHASE_RUNNING
            state['leader_seen_at'] = _iso(_now())
            state['updated_at'] = _iso(_now())
            return state

        return self._update(mutate)

    # -------------------------------- 突破进行中互查 --------------------------------

    def mark_realm_raid_pending(self, role: str, now: datetime | None = None) -> dict:
        """本方即将去打结界突破，落一个带时间戳的标记供对方查询。

        不校验 session：突破发生在轮次之间，此时本轮 session 已经收尾，而下一轮
        session 尚未发布，用旧令牌校验只会把这个标记拒之门外。
        """
        if role not in {'leader', 'member'}:
            raise ValueError('invalid role')
        now = (now or _now()).replace(microsecond=0)

        def mutate(state: dict) -> dict:
            state[f'{role}_realm_raid_pending_at'] = _iso(now)
            state['updated_at'] = _iso(now)
            return state

        return self._update(mutate)

    def clear_realm_raid_pending(self, role: str, now: datetime | None = None) -> dict:
        """本方已从突破回到御魂，清除标记，让对方立刻结束等待。"""
        if role not in {'leader', 'member'}:
            raise ValueError('invalid role')
        now = (now or _now()).replace(microsecond=0)

        def mutate(state: dict) -> dict:
            if state.get(f'{role}_realm_raid_pending_at') is None:
                return None
            state[f'{role}_realm_raid_pending_at'] = None
            state['updated_at'] = _iso(now)
            return state

        return self._update(mutate)

    @staticmethod
    def realm_raid_pending(state: dict, role: str, now: datetime | None = None) -> bool:
        """对方是否正在打突破。带兜底过期，避免对方崩溃后标记永久挂住。"""
        if not state:
            return False
        marked_at = _parse_datetime(state.get(f'{role}_realm_raid_pending_at'))
        if marked_at is None:
            return False
        now = (now or _now()).replace(microsecond=0)
        elapsed = (now - marked_at).total_seconds()
        return 0 <= elapsed <= REALM_RAID_PENDING_EXPIRE_SECONDS

    # -------------------------------- 战斗进度与调度 --------------------------------

    def update_progress(self, session: TeamSession, current_count: int) -> dict:
        """队长每场战斗后以本轮绝对计数更新，避免重复写导致累计翻倍。"""
        current_count = max(0, int(current_count))

        def mutate(state: dict) -> dict:
            self._require_session(state, session)
            if state.get('phase') in TERMINAL_PHASES:
                return None
            previous = max(0, int(state.get('round_count', 0) or 0))
            if current_count > previous:
                state['total_count'] = int(state.get('total_count', 0) or 0) + current_count - previous
                state['round_count'] = current_count
            state['leader_seen_at'] = _iso(_now())
            state['updated_at'] = _iso(_now())
            return state

        return self._update(mutate)

    def round_limit_reached(self, session: TeamSession, now: datetime | None = None) -> bool:
        state = self.read()
        self._require_session(state, session)
        if int(state.get('round_count', 0) or 0) >= int(state.get('effective_limit_count', 0) or 0):
            return True
        started_at = _parse_datetime(state.get('round_started_at'))
        if started_at is None:
            return False
        now = (now or _now()).replace(microsecond=0)
        return (now - started_at).total_seconds() >= int(state.get('effective_limit_seconds', 0) or 0)

    def finish_round(
        self,
        session: TeamSession,
        success: bool,
        next_orochi_at: datetime,
        now: datetime | None = None,
    ) -> dict:
        """队长唯一生成下一轮时间；队员只能读取并复制，禁止独立计算。

        结界突破改为各自本地拉起，因此这里不再下发突破时间，只把"下一轮需要等
        对方打完突破"这一事实落进 pairing_needs_realm_raid，供下一轮放宽配对超时。
        """
        now = (now or _now()).replace(microsecond=0)

        def mutate(state: dict) -> dict:
            self._require_session(state, session)
            if state.get('phase') in TERMINAL_PHASES:
                return state
            started_at = state.get('round_started_at')
            elapsed = _elapsed_seconds(started_at, _iso(now))
            limit = int(state.get('effective_limit_seconds', 0) or 0)
            if limit > 0:
                elapsed = min(elapsed, limit)
            state['total_elapsed_seconds'] = int(state.get('total_elapsed_seconds', 0) or 0) + elapsed

            finished = (
                int(state.get('total_count', 0) or 0) >= int(state.get('total_limit_count', 0) or 0)
                or int(state.get('total_elapsed_seconds', 0) or 0)
                >= int(state.get('total_limit_seconds', 0) or 0)
            )
            needs_realm_raid = bool(state.get('leader_realm_raid') or state.get('member_realm_raid'))
            state.update({
                'phase': PHASE_FINISHED if finished else PHASE_WAIT_NEXT_ROUND,
                'accept_member': False,
                'round_finished_at': _iso(now),
                'round_success': bool(success),
                'next_orochi_at': None if finished else _iso(next_orochi_at),
                # 总量已打完就不会再配对，标记无需保留；否则下一轮要等对方打完突破
                'pairing_needs_realm_raid': False if finished else needs_realm_raid,
                'leader_seen_at': _iso(now),
                'updated_at': _iso(now),
            })
            return state

        return self._update(mutate)

def parse_state_datetime(value) -> datetime | None:
    """供 Orochi 调度层解析 JSON 时间，统一容忍缺失和非法值。"""
    return _parse_datetime(value)
