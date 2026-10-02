# This Python file uses the following encoding: utf-8
"""师徒任务徒弟/师父实例间的原子 JSON 状态存储。

仿照 tasks/Orochi/team_state.py 的实现骨架：FileLock 串行化读改写、
临时文件 + os.replace 原子落盘、会话令牌防串场。状态文件按师父实例名
命名，存放于 config/tasks_config/（运行期数据，不入库）。

协议总览：
- 徒弟(disciple)是主动方：发布会话、下发任务序列、等师父就位后开战
- 师父(master)是被动方：加入会话、按任务指令提前开加成并回报就绪、
  接受邀请进入房间后置位「在房间里」，离开房间时复位
- 进房信号是**状态**而不是事件：师父确认进入房间后把 master_in_room 置 True，
  开战/房间销毁/超时退出时复位 False；徒弟只读这个布尔值，不问「第几次」，
  因此双方都不需要对齐编号或快照时机——完全取代旧的「反复识别房间加号
  数量并等它稳定」的进房检测。
  历史上先后用过两版事件对齐（徒弟递增邀请序号、师父累加进房次数），都栽在
  同一件事上：游戏在徒弟战斗结算时自动发出下一场邀请，师父响应快慢不受脚本
  控制。序号版会让师父读到递增前的旧序号、徒弟死等一个新序号（真机表现为
  徒弟 15 秒重邀请、师父在房间里点表情干等到超时）。状态版没有编号可比，
  谁先谁后都不影响结论
"""
import json
import os
import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from urllib.parse import quote

from filelock import FileLock

from module.logger import logger


# 阶段：徒弟发布等待师父加入 → 配对成功 → 徒弟序列完成
PHASE_WAIT_MASTER = 'WAIT_MASTER'
PHASE_PAIRED = 'PAIRED'
PHASE_FINISHED = 'FINISHED'

# 任务类型（current_task 取值）
TASK_GUARD = 'guard'              # 守护历练
TASK_STONE = 'stone'              # 石距
TASK_COIN = 'coin'                # 金币妖怪
TASK_EXP = 'exp'                  # 经验妖怪
TASK_EXPLORATION = 'exploration'  # 探索（单人，师父只需长超时等待）
TASK_SWITCHING = 'switching'      # 徒弟切号中（师父长超时等待）

# 加成指令（buff_command 取值）：空字符串 = 无需提前准备
# 金币/经验的「打完」变体同时要求师父提前开对应加成；
# 「准备后退出」变体不开加成（退出的场次开加成是浪费）
BUFF_COIN = 'coin'        # 金币场：师父提前开金币加成，战斗正常打完
BUFF_COIN_EXIT = 'coin_exit'  # 金币场：师父不开加成，准备后退出
BUFF_EXP = 'exp'          # 经验场：师父提前开经验加成，战斗正常打完
BUFF_EXP_EXIT = 'exp_exit'    # 经验场：师父不开加成，等击杀数达标后退出

# 徒弟心跳新鲜窗口。两个用途共用同一个含义「徒弟最近还在推进」：
# - 配对门槛：徒弟发布后即去切号/买体力（长 UI 流程期间无法刷心跳），
#   窗口必须覆盖这些单人环节
# - 师父的存活判断：单人环节耗时不可预知，用固定超时只能靠猜；只要徒弟
#   还在刷心跳就一直陪等，心跳过期才认定它已停止
# 300 秒的依据：徒弟唯一不刷心跳的长流程是切号，而切号前会先写 switching
# 任务、那次写入本身就刷新了心跳，所以实际空档就是一次切号加买体力；
# 其余环节（房间等待、探索战斗之间）都是几秒到几十秒刷一次。
# 上一轮残留至少间隔一轮任务周期（小时级），300 秒仍能明确区分
DISCIPLE_FRESH_SECONDS = 300


class StaleSessionError(RuntimeError):
    """调用方携带的会话令牌已过期（对方实例重启发布了新会话），禁止继续写。"""


@dataclass(frozen=True)
class MasterDiscipleSession:
    """所有写操作必须携带的不可变会话标识。"""

    progress_epoch: str
    session_id: str

    @classmethod
    def from_state(cls, state: dict) -> 'MasterDiscipleSession':
        return cls(
            progress_epoch=str(state.get('progress_epoch', '')),
            session_id=str(state.get('session_id', '')),
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


class MasterDiscipleStateStore:
    """按师父实例名保存一份师徒同步状态，用 FileLock 串行化读改写。"""

    def __init__(self, master_instance: str, base_dir='config/tasks_config'):
        master_instance = str(master_instance or '').strip()
        if not master_instance:
            raise ValueError('master_instance cannot be empty')
        self.master_instance = master_instance
        # quote 防止实例名中的空格或非 ASCII 字符直接参与 Windows 路径解析（同 Orochi）
        filename = quote(master_instance.casefold(), safe='-_.')
        self.path = Path(base_dir) / f'master_disciple_team_{filename}.json'
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.lock = FileLock(f'{self.path}.lock', timeout=10)

    # -------------------------------- 底层原子读写（同 Orochi TeamStateStore） --------------------------------

    def _read_unlocked(self) -> dict:
        if not self.path.exists():
            return {}
        try:
            with open(self.path, 'r', encoding='utf-8') as file:
                data = json.load(file)
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            # 损坏文件按无状态处理，徒弟下一次发布会原子覆盖重建
            logger.warning(f'师徒同步状态文件损坏，将等待徒弟重建: {exc}')
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

    @staticmethod
    def _require_session(state: dict, session: MasterDiscipleSession) -> None:
        current = MasterDiscipleSession.from_state(state)
        if current != session:
            raise StaleSessionError('STALE_SESSION')

    # -------------------------------- 徒弟侧写操作 --------------------------------

    def publish_session(self, disciple_instance: str, now: datetime | None = None) -> dict:
        """徒弟发布新会话：无条件覆盖旧文件，旧会话双方凭令牌失配自行退出。"""
        now = now or _now()
        state = {
            'version': 1,
            'master_instance': self.master_instance,
            'disciple_instance': str(disciple_instance),
            'progress_epoch': uuid.uuid4().hex,
            'session_id': uuid.uuid4().hex,
            'phase': PHASE_WAIT_MASTER,
            'current_task': '',
            'buff_command': '',
            'master_ready': False,
            # 师父此刻是否在房间里等开战（状态位，不是事件计数）：徒弟据此决定开战
            'master_in_room': False,
            'disciple_seen_at': _iso(now),
            'master_seen_at': None,
            'master_joined_instance': '',
            'updated_at': _iso(now),
        }
        with self.lock:
            self._write_unlocked(state)
        return state

    def set_current_task(self, session: MasterDiscipleSession, task: str,
                         buff_command: str = '') -> dict:
        """徒弟下发当前任务与加成指令；切换任务时重置师父就绪标记。

        不校验 phase：徒弟在未配对（WAIT_MASTER）时也照常推进序列，
        迟到的师父加入后能顺着 current_task 一路跟到 FINISHED 自然退出。
        """
        def mutate(state: dict) -> dict:
            self._require_session(state, session)
            state['current_task'] = str(task)
            state['buff_command'] = str(buff_command or '')
            state['master_ready'] = False
            state['disciple_seen_at'] = _iso(_now())
            state['updated_at'] = _iso(_now())
            return state

        return self._update(mutate)

    def mark_finished(self, session: MasterDiscipleSession) -> dict:
        """徒弟整个任务序列完成；师父读到 FINISHED 后正常退出。"""
        def mutate(state: dict) -> dict:
            self._require_session(state, session)
            state['phase'] = PHASE_FINISHED
            state['current_task'] = ''
            state['buff_command'] = ''
            state['disciple_seen_at'] = _iso(_now())
            state['updated_at'] = _iso(_now())
            return state

        return self._update(mutate)

    # -------------------------------- 师父侧写操作 --------------------------------

    def try_join(self, master_instance: str, now: datetime | None = None) -> dict | None:
        """师父加入「新鲜的」WAIT_MASTER 会话；本实例已加入时幂等返回当前状态。

        新鲜度要求徒弟最近 DISCIPLE_FRESH_SECONDS 秒内心跳过，防止误连
        上一轮残留的 WAIT_MASTER（那一场徒弟早已退出）。
        :return: 加入成功（或已加入）返回最新状态，否则 None
        """
        now = (now or _now()).replace(microsecond=0)
        joined = None

        def mutate(state: dict) -> dict:
            nonlocal joined
            # 实例名比较统一 casefold：文件名已按 casefold 折叠，大小写不同的
            # 配置必须指向同一会话（徒弟配 OAS1、师父实例叫 oas1 时也能配上）
            if str(state.get('master_instance') or '').casefold() != self.master_instance.casefold():
                return None
            if str(master_instance or '').strip().casefold() != self.master_instance.casefold():
                return None
            phase = state.get('phase')
            bound_master = str(state.get('master_joined_instance') or '')
            # 幂等：本实例已经加入过，直接返回当前状态
            if phase == PHASE_PAIRED and bound_master == master_instance:
                joined = state
                return None
            if phase != PHASE_WAIT_MASTER:
                return None
            # 已被其他师父实例占用时拒绝加入
            if bound_master:
                return None
            disciple_seen_at = _parse_datetime(state.get('disciple_seen_at'))
            if disciple_seen_at is None:
                return None
            elapsed = (now - disciple_seen_at).total_seconds()
            if not 0 <= elapsed <= DISCIPLE_FRESH_SECONDS:
                return None
            state.update({
                'phase': PHASE_PAIRED,
                'master_joined_instance': str(master_instance),
                'master_seen_at': _iso(now),
                'updated_at': _iso(now),
            })
            joined = state
            return state

        self._update(mutate)
        return joined

    def mark_master_ready(self, session: MasterDiscipleSession, task: str) -> bool:
        """师父对当前任务的准备（提前开加成等）已完成，徒弟可以发起邀请。

        校验 current_task 与决策时的任务一致：师父开加成的 UI 操作耗时较长，
        期间徒弟可能已超时切到下一个任务（ready 被重置），迟到的就绪回报
        若照写会把新任务误标记为就绪。不匹配时不写入，师父下一轮读到新
        任务会重新准备并回报。
        :return: True 已写入就绪标记；False 任务已切换，本次回报被忽略
        """
        rejected = False

        def mutate(state: dict) -> dict:
            nonlocal rejected
            self._require_session(state, session)
            if state.get('phase') != PHASE_PAIRED:
                raise StaleSessionError('STALE_PHASE')
            if str(state.get('current_task') or '') != str(task):
                logger.warning(f"就绪回报的任务 [{task}] 已被切换为 [{state.get('current_task')}]，忽略本次回报")
                rejected = True
                return None
            state['master_ready'] = True
            state['master_seen_at'] = _iso(_now())
            state['updated_at'] = _iso(_now())
            return state

        self._update(mutate)
        return not rejected

    def mark_master_in_room(self, session: MasterDiscipleSession) -> dict:
        """师父接受邀请且确认进入房间：置位供徒弟判断可以开战。

        幂等：徒弟只看当前是不是 True，重复置位不产生额外语义。
        """
        def mutate(state: dict) -> dict:
            self._require_session(state, session)
            if state.get('phase') != PHASE_PAIRED:
                raise StaleSessionError('STALE_PHASE')
            state['master_in_room'] = True
            state['master_seen_at'] = _iso(_now())
            state['updated_at'] = _iso(_now())
            return state

        return self._update(mutate)

    def clear_master_in_room(self, session: MasterDiscipleSession) -> dict:
        """师父已不在房间里等开战（开战/房间销毁/超时退出）：复位标志。

        师父必须在离开房间的每个出口都调用：标志停留在 True 会让徒弟下一场看到
        陈旧的 True 而提前开战，把还没进房的师父打断。复位比置位更危险，因此
        这里不复用 _require_session 抛错——会话已失效说明本轮跟随即将结束，
        标志会随徒弟发布的新会话重建，不写即可。
        """
        def mutate(state: dict) -> dict:
            if MasterDiscipleSession.from_state(state) != session:
                return None
            if not state.get('master_in_room'):
                return None
            state['master_in_room'] = False
            state['master_seen_at'] = _iso(_now())
            state['updated_at'] = _iso(_now())
            return state

        return self._update(mutate)

    # -------------------------------- 通用 --------------------------------

    def heartbeat(self, session: MasterDiscipleSession, role: str,
                  now: datetime | None = None) -> dict:
        """双方在轮询等待期间刷新心跳，供对方做新鲜度判断与诊断。

        心跳不设自己的超时（同 Orochi）：掉线兜底由各业务级超时负责。
        但它对师父侧是「徒弟还在不在」的唯一判据——单人环节耗时不可预知，
        固定超时只能靠猜，见 disciple_alive。
        """
        if role not in {'disciple', 'master'}:
            raise ValueError('invalid role')
        now = now or _now()

        def mutate(state: dict) -> dict:
            self._require_session(state, session)
            if state.get('phase') == PHASE_FINISHED:
                return None
            state[f'{role}_seen_at'] = _iso(now)
            state['updated_at'] = _iso(now)
            return state

        return self._update(mutate)


def disciple_alive(state: dict, now: datetime | None = None) -> bool:
    """徒弟是否还在推进（最近刷过心跳）。

    徒弟在切号发布、房间等待、探索战斗之间都会刷新 disciple_seen_at，所以
    「心跳过期」只可能是它真的停了（崩溃或被停），而不是某个环节耗时长。
    状态缺失/时间戳非法一律返回 False，由调用方决定怎么处理。
    """
    if not state:
        return False
    seen_at = _parse_datetime(state.get('disciple_seen_at'))
    if seen_at is None:
        return False
    now = (now or _now()).replace(microsecond=0)
    return 0 <= (now - seen_at).total_seconds() <= DISCIPLE_FRESH_SECONDS
