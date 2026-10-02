# This Python file uses the following encoding: utf-8
"""协作表：把「找协作」识别到的协作按账号与过期时间持久化。

与进度文件分开存放是刻意的：进度文件的 `coop` 列表跟随阶段生命周期，
`ensure_phase` / `clear` 每轮会重建或清空（见
tasks/Component/MultiAccountRunner/progress.py），服务的是「本轮发现了什么」。
而「完成协作」单独开启时要读「哪些协作还没过期」，这个判断必须跨轮次存活，
所以用独立文件。通用实现放在 Utils，供需要保存协作记录的任务共用。
"""
import json
import os
from copy import deepcopy
from datetime import datetime, timedelta
from pathlib import Path

from tasks.Component.MultiAccountRunner.progress import STATUS_DONE, STATUS_PENDING

# 游戏侧协作刷新时刻（小时）。协作在 05:00 与 18:00 各刷新一次，
# 一条协作的失效时刻即其发现时刻之后的下一个刷新点。
COOP_REFRESH_HOURS: tuple[int, int] = (5, 18)


def next_refresh_point(moment: datetime) -> datetime:
    """返回严格晚于 moment 的下一个协作刷新时刻。

    严格晚于是必须的：恰好落在 18:00:00 上发现的协作，若返回它自己会被
    立刻判为过期，等于丢掉一次协作。
    """
    for hour in COOP_REFRESH_HOURS:
        candidate = moment.replace(hour=hour, minute=0, second=0, microsecond=0)
        if candidate > moment:
            return candidate
    # 当天两个刷新点都已过，滚到次日第一个刷新点
    return (moment + timedelta(days=1)).replace(
        hour=COOP_REFRESH_HOURS[0], minute=0, second=0, microsecond=0)


def coop_expires_at(found_at: datetime) -> datetime:
    """一条协作的失效时刻。"""
    return next_refresh_point(found_at)


_TS_FORMAT = '%Y-%m-%d %H:%M:%S'


class CoopStoreWriteError(RuntimeError):
    """协作表读取或写入失败，调用方必须保留原文件、本轮记录并安排重试。"""


def _fmt(moment: datetime) -> str:
    return moment.strftime(_TS_FORMAT)


def _parse(value) -> datetime | None:
    """解析时间戳；非法或缺失返回 None（调用方按「不可判定」处理）。

    **刻意与 _fmt 不对称**：写用 `strftime(_TS_FORMAT)`（严格、单一格式），读用
    `fromisoformat`（宽松，同时吃 'T' 分隔符与毫秒）。协作表是人会手工编辑的
    对账依据，读端严格化会把「格式略有不同但语义正确」的行静默判成不可判定，
    从而从 pending() 里消失——宽进严出在这里是安全的一侧。

    代价：改 `_TS_FORMAT` 不会自动同步本函数。只要新格式仍是 ISO 8601 的子集
    （fromisoformat 能吃）就无需改动；否则必须同步改这里，并补往返用例。
    """
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def coop_key(account: str, character: str, svr, apple_or_android: bool,
             coop_type: str, real: bool, food_kind: str | None = None) -> str:
    """一条协作记录的**逻辑键**（不含窗口）。

    刻意不含发现时刻：`CoopStore.record` 靠「同键且同窗口即覆盖」实现「找协作
    重试不产生重复行」。行标识是记录自带的 `id`（本键 + 刷新窗口），跨窗口重新
    发现时是**追加新行**而不是覆盖——桌面应用按窗口对账协作数量，不能被下一个
    窗口的发现抹掉前一条。

    代价：同一窗口内同账号同类型同 `real` 的**两张**协作会被并成一行。
    现世不会构成这个场景（同类型现世协作不会同时出现两张），所以代价只可能
    落在普通协作上，且需要同账号同窗口恰好识别到两张同类型普通协作。
    对账口径已接受这一少算。

    food_kind 必须进键：`_coop_emit` 的 type 取自 `COOP_TYPE_SPEC[type_name]['event_type']`，
    而狗粮与猫粮都是 `'food'`（tasks/DailyAltAcc/cooperation.py:100-107），
    只按 type 区分会让两类粮协互相覆盖、丢记录。

    apple_or_android 进键：它是账号身份的一段（用户在账号配置里勾选，「勾选为
    安卓，不勾选苹果」），与 account/character/svr 同属「这是谁」。抛开平台只
    按前三段做键，一旦该标记变动（换端、重绑、配置改错），同一张还挂着的协作
    会在**同一窗口内**裂成两行，`pending()` 返回两条，真实实现（Plan 2）就会
    对同一张协作重复开打、重复耗体力——这个代价比键长一段贵得多。
    """
    svr_name = getattr(svr, 'name', None) or str(svr or '')
    return (f'{account}|{character}|{svr_name}|{int(bool(apple_or_android))}'
            f'|{coop_type}|{int(bool(real))}|{food_kind or ""}')


class CoopStore:
    """协作表读写。每个配置实例一份文件，互不干扰。

    刻意不复用 ProgressStore：进度文件跟随阶段生命周期会被重建或清空，而本表
    要服务两个跨轮次的消费方——「哪些协作还没过期」（完成协作）与「每个窗口发现
    了哪些协作」（桌面应用对账订单的溢出/缺少）。后者要求历史存活，所以
    record() 按 `id`（逻辑键 + 刷新窗口）匹配：同窗口内重跑覆盖，跨窗口追加新行。

    **落盘粒度是刷新窗口，不是自然日**：窗口为 [05:00, 18:00) 与
    [18:00, 次日 05:00)，后者横跨午夜。下游若按 `found_at` 的日期分天统计，
    18:00 后那次运行产出的记录会归到次日——两套粒度不同，对账方需自行决定按
    哪一套切分。

    **本表刻意不清理、无上限**，这与同仓的 `MultiDailyAltAcc.progress`
    （`COOP_ARCHIVE_LIMIT = 5`，只保留最近 5 份未发通知的协作归档）相反，
    是经过权衡的：那边的归档只为「发了通知就别再发」，过期的没有价值；这边
    是桌面应用做长期订单对账的依据，删旧行等于删证据。**量级可控**——每个
    刷新窗口每账号至多 3 条（协作板三槽），一天 2 个窗口，一年约 2200 条/账号，
    单文件几十 KB 量级；`pending()` 的全表扫描在这个规模下不是瓶颈。
    下游按 `found_at` 自行过滤所需时间范围，不要指望本表裁剪。

    与 ProgressStore 一样只做文件读写，不依赖 device / Config，便于单元测试。
    """

    def __init__(self, config_name: str, base_dir='config/tasks_config'):
        self.config_name = config_name
        # 公共默认文件名不绑定某个任务；本机可接入已有消费方使用的历史路径。
        from tasks.Utils.optional_tasks import extend
        self.path = extend('coop_path', Path(base_dir) / f'cooperation_records_{config_name}.json', config_name)
        self._data: list = self._load()

    # ---------- 底层读写 ----------

    def _load(self) -> list:
        """不存在时创建空表；已有文件不可读取时拒绝继续，避免覆盖真实历史。"""
        try:
            with open(self.path, 'r', encoding='utf-8') as f:
                data = json.load(f)
        except FileNotFoundError:
            return []
        except (OSError, ValueError) as e:
            raise CoopStoreWriteError(f'协作表无法读取，拒绝覆盖原文件: {self.path}: {e}') from e
        if not isinstance(data, list):
            raise CoopStoreWriteError(f'协作表格式错误（顶层必须为列表），拒绝覆盖原文件: {self.path}')
        return data

    def _save(self, data: list) -> None:
        """原子写候选协作表；失败抛错，由调用方保留旧内存状态并安排重试。

        用与 ProgressStore._write_json_atomic 相同的手法：tmp + fsync + os.replace，
        避免进程被杀时留下截断 JSON。这里不导入那个函数是为了让本模块不依赖
        ProgressStore 的私有名，实现只有十来行，重复的代价小于跨模块的私有耦合。

        本轮观测与协作事实写入同一文件，只有替换成功才允许调用方更新内存；
        否则进程继续收尾会把未落盘的发现当成成功，并在清理进度时丢掉唯一副本。
        """
        tmp_path = self.path.with_suffix('.json.tmp')
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with open(tmp_path, 'w', encoding='utf-8') as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp_path, self.path)
        except Exception as e:
            try:
                tmp_path.unlink(missing_ok=True)
            except OSError:
                pass
            raise CoopStoreWriteError(f'协作表写入失败，本次未持久化: {self.path}: {e}') from e

    # ---------- 写入 ----------

    def record(self, account: str, character: str, svr, apple_or_android: bool,
               event: dict, found_at: datetime | None = None,
               round_id: str | None = None) -> dict:
        """写入一条协作；同一刷新窗口内原地覆盖，跨窗口追加新行。

        窗口粒度是 05:00 / 18:00 两档刷新，**不是自然日**——[18:00, 次日 05:00)
        横跨午夜，用日期做行标识会把同一张协作拆成两行。

        :param event: 来自 Cooperation._coop_emit 的事件 dict，键为
                      type / real / label / food_kind(可选) /
                      discoverer_monster(可选) / friend_monster(可选) / monster_text(可选)
        :param found_at: **识别帧的时刻**，不是写入时刻。两个刷新窗口的边界是
                         05:00 / 18:00，找协作的收尾（点返回、ui_goto 回主页）
                         要 1~5 秒，恰好跨过边界时按写入时刻取会把一条版面已经
                         刷掉的协作归进**下一个**窗口——它会在 pending() 里活满
                         11 小时，把账号范围撑大、并让桌面应用把它计入错误窗口。
                         调用方应传识别帧时刻；None 退化为当前时间（单测与
                         手工调用），此时上述偏差成立。
        :param round_id: 多账号任务的稳定轮次标识；提供时 event 必须带 slot。
                         同轮同槽重扫更新快照，不同槽即使同类型也保留各自目标与数量。
        :return: 已落盘记录的深副本（含 'key' 与 'id'）；写盘失败抛 CoopStoreWriteError。
        """
        if round_id is not None and 'slot' not in event:
            raise ValueError('本轮协作记录缺少卡槽 slot')
        found_at = found_at or datetime.now()
        food_kind = event.get('food_kind')
        key = coop_key(account, character, svr, apple_or_android,
                       str(event.get('type', '') or ''), bool(event.get('real', False)),
                       food_kind)
        expires_at = coop_expires_at(found_at)
        record = {
            'key': key,
            # 行标识 = 逻辑键 + **刷新窗口**。窗口由 expires_at 唯一标识（它就是
            # found_at 之后的下一个刷新点），所以「同一窗口内重跑覆盖、跨窗口
            # 追加新行」只需比较这一个值。
            #
            # 刻意**不用自然日**做行标识：窗口 [18:00, 次日 05:00) 横跨午夜，
            # 23:00 与次日 03:00 同属一个窗口却分属两个自然日，按日期分会把同
            # 一张协作落成两行、待办里出现两条——Plan 2 接上真实动作后会重复
            # 开打、重复耗体力。
            'id': f'{key}|{_fmt(expires_at)}',
            'account': str(account or ''),
            'character': str(character or ''),
            'svr': getattr(svr, 'name', None) or str(svr or ''),
            'apple_or_android': bool(apple_or_android),
            'type': str(event.get('type', '') or ''),
            'real': bool(event.get('real', False)),
            'food_kind': food_kind,
            'label': str(event.get('label', '') or ''),
            'discoverer_monster': str(event.get('discoverer_monster', '') or ''),
            'friend_monster': str(event.get('friend_monster', '') or ''),
            'monster_text': str(event.get('monster_text', '') or ''),
            'found_at': _fmt(found_at),
            'expires_at': _fmt(expires_at),
            'status': STATUS_PENDING,
        }
        # 只修改候选列表，文件成功替换之前不改变已确认落盘的内存状态。
        updated = list(self._data)
        for index, existing in enumerate(self._data):
            if existing.get('id') != record['id']:
                continue
            # id 相同即「同一刷新窗口内的重跑」（失败重调度、手动重跑、以及跨
            # 午夜的同窗口），保留已完成的 status：否则完成协作重跑会对同一张
            # 还挂在界面上的协作重复开打、重复耗体力。
            # expires_at 已含在 id 里，故此处不必再比一次。
            record['status'] = existing.get('status', STATUS_PENDING)
            if 'round_observations' in existing:
                record['round_observations'] = deepcopy(existing['round_observations'])
            updated[index] = record
            break
        else:
            updated.append(record)
        if round_id is not None:
            # 顶层仍按旧窗口 ID 去重供管家读取；轮次快照另存真实卡槽，避免同型卡
            # 覆盖后推送少算数量或把前一张卡的目标替换成后一张的目标。
            snapshot = {field: record[field] for field in (
                'type', 'real', 'food_kind', 'label', 'discoverer_monster',
                'friend_monster', 'monster_text', 'found_at',
            )}
            snapshot['slot'] = event['slot']
            observations = record.setdefault('round_observations', {}).setdefault(round_id, [])
            for index, previous in enumerate(observations):
                if previous.get('slot') == snapshot['slot']:
                    observations[index] = snapshot
                    break
            else:
                observations.append(snapshot)
        self._save(updated)
        self._data = updated
        # 嵌套快照也必须复制，调用方改返回值不能绕过持久化修改表内数据。
        return deepcopy(record)

    def mark_done(self, record_id: str) -> bool:
        """把一条协作标为已完成；当前口径是我方普通勾协进度已满。

        :param record_id: 记录的 `id`（= 逻辑键 + 刷新窗口），**不是** `key`。
                          跨窗口之后同一条逻辑协作在表里有多行，按 key 找会命中
                          最早那条，把旧的记录误标成已完成。
        :return: True 表示本次确实发生了状态迁移；id 不存在或已是 done 返回 False。
                 写盘失败抛 CoopStoreWriteError，内存和磁盘都保留原状态。
        """
        for index, row in enumerate(self._data):
            if row.get('id') != record_id:
                continue
            if row.get('status') == STATUS_DONE:
                return False
            # 完成状态同样先写候选表，不能用未落盘的内存状态报告完成。
            updated = list(self._data)
            updated[index] = dict(row, status=STATUS_DONE)
            self._save(updated)
            self._data = updated
            return True
        return False

    # ---------- 读取 ----------

    def all(self) -> list:
        """表内全部记录（副本，调用方改动不影响表内数据）。"""
        return deepcopy(self._data)

    def records_for_round(self, round_id: str) -> list:
        """按本轮真实卡槽展开记录，保留推送重数、各卡目标和各自识别时间。"""
        result = []
        for row in self._data:
            for observation in row.get('round_observations', {}).get(round_id, []):
                # 对外只暴露标准行，内部历史轮次快照不混入 formatter 的输入。
                record = {key: value for key, value in row.items() if key != 'round_observations'}
                record.update(observation)
                result.append(deepcopy(record))
        return result

    def pending(self, now: datetime | None = None) -> list:
        """未完成且未过期的记录。

        时间戳缺失或非法一律视为不可判定，不计入待办（宁可少打，不能拿一条
        永远不过期的记录把账号范围撑大到每轮都去切号）。
        """
        now = now or datetime.now()
        result = []
        for row in self._data:
            if row.get('status') != STATUS_PENDING:
                continue
            expires = _parse(row.get('expires_at'))
            if expires is None or expires <= now:
                continue
            result.append(deepcopy(row))
        return result
