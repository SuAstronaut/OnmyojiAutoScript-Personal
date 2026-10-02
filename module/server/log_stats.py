# This Python file uses the following encoding: utf-8
from __future__ import annotations

import asyncio
import copy
import json
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

# 统计日志固定读取项目根目录下的 log，避免启动目录变化影响路径。
PROJECT_ROOT = Path(__file__).resolve().parents[2]
LOG_ROOT = (PROJECT_ROOT / "log").resolve()
POLL_INTERVAL_SECONDS = 1.0
HISTORICAL_CACHE_TTL = timedelta(hours=1)
_LOG_FILE_RE = re.compile(r"^(?P<day>\d{4}-\d{2}-\d{2})_(?P<script>.+)\.txt$")

_TIMESTAMP_RE = re.compile(r"^(?P<ts>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\.\d{3})\s+\|")
_TASK_LINE_RE = re.compile(r"\[Task\]\s+(?P<task>[A-Za-z0-9_]+)\s+\(")
_EQ_LINE_RE = re.compile(r"^═{15,}\s*$")
_TITLE_LINE_RE = re.compile(r"^─{10,}\s*(?P<title>.*?)\s*─{10,}\s*$")
_BATTLE_TITLE = "GENERAL BATTLE START"
# 战斗结束标记行：GeneralBattle 的 win/false 分支输出 "Battle result is"，
# 领奖分支（I_REWARD/I_REWARD_GOLD）无该行，但四条退出路径都会经过 "Reconfirm the results of the battle"；
# 任一标记命中即闭合战斗，极端路径仍由边界/task_end/快照兜底
_BATTLE_END_MARKERS = ("Battle result is", "Reconfirm the results of the battle")
_START_TITLE = "START"
_STAT_PREFIX = "[STAT] "


def _format_multi_ts(ts: datetime | None) -> str | None:
    """把时间戳格式化为前端可读的毫秒级字符串，None 原样返回。"""
    if ts is None:
        return None
    return ts.strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]


@dataclass
class MultiAccountState:
    """多账号统计中间状态，聚合单个账号在一次运行周期内的 STAT 事件。"""
    account: str = ""
    character: str = ""
    svr: str = ""
    start_time: datetime | None = None
    last_time: datetime | None = None
    switch_ok: bool | None = None
    error_count: int = 0
    battle_count: int = 0
    battle_total_duration_seconds: float = 0.0
    boundary_battle_count: int = 0  # 边界计时器累加的战斗次数，用于弥补无 STAT 事件时的战斗计数
    coop_total: int = 0
    tasks: list[dict] = field(default_factory=list)
    errors: list[dict] = field(default_factory=list)
    coops: list[dict] = field(default_factory=list)
    mshops: list[dict] = field(default_factory=list)
    # 运行段（change-B 需求6）：按账号切换/任务结束标志切分，避免跨空闲段重复计算耗时。
    # 每段为 {"start": datetime, "end": datetime, "duration": float, "session": int}
    segments: list[dict] = field(default_factory=list)
    # 当前未闭合运行段的起点与其所属会话索引，None 表示无未闭合段
    segment_open_start: datetime | None = None
    segment_open_session: int | None = None

    def to_payload(self) -> dict[str, Any]:
        """将账号中间状态转换为对外输出的字典格式。"""
        # 账号总耗时 = 所有运行段耗时之和（需求6 新口径）
        duration = round(sum(seg["duration"] for seg in self.segments), 3)
        # 兜底：无运行段时（例如仅被占位创建、未经历 acc_start）回退到首末事件跨度
        if (
            not self.segments
            and self.start_time is not None
            and self.last_time is not None
            and self.last_time >= self.start_time
        ):
            duration = round((self.last_time - self.start_time).total_seconds(), 3)
        # 按战斗边界计算的真实战斗耗时，与前端 snake_case 契约一致
        # 战斗次数取 STAT 事件与边界计时两者的最大值，确保无 STAT 事件时仍能反映真实战斗次数
        effective_battle_count = max(self.battle_count, self.boundary_battle_count)
        battle_avg = (
            round(self.battle_total_duration_seconds / effective_battle_count, 3)
            if effective_battle_count > 0 else 0.0
        )
        return {
            "account": self.account,
            "character": self.character,
            "svr": self.svr,
            "switch_ok": self.switch_ok,
            "duration_seconds": duration,
            "error_count": self.error_count,
            "battle_count": effective_battle_count,
            "battle_total_duration_seconds": self.battle_total_duration_seconds,
            "battle_avg_duration_seconds": battle_avg,
            "coop_total": self.coop_total,
            "tasks": self.tasks,
            "errors": self.errors,
            "coops": self.coops,
            "mshops": self.mshops,
            # 运行段明细：供前端按会话时间窗筛选（需求2）及展示每次运行（需求3）
            "segments": [
                {
                    "start_time": _format_multi_ts(seg["start"]),
                    "end_time": _format_multi_ts(seg["end"]),
                    "duration_seconds": seg["duration"],
                    "session": seg["session"],
                }
                for seg in self.segments
            ],
        }


@dataclass
class BattleState:
    start_time: datetime | None = None
    last_time: datetime | None = None


@dataclass
class TaskRunState:
    name: str
    start_time: datetime | None = None
    last_time: datetime | None = None
    battle_count: int = 0
    battle_total_duration_seconds: float = 0.0


@dataclass
class RuntimeState:
    saw_start_boundary: bool = False
    pre_start_first: datetime | None = None
    region_last: datetime | None = None
    session_start: datetime | None = None
    total_duration: float = 0.0


@dataclass
class HistoricalStatsCacheEntry:
    expires_at: datetime
    payload: dict[str, Any]


class LogStatsParser:
    def __init__(self) -> None:
        self.summary: dict[str, dict[str, Any]] = {}
        self.runtime = RuntimeState()
        self._pending_task_name: str | None = None
        self._pending_task_start_name: str | None = None
        self._pending_battle_start = False
        self._active_task: TaskRunState | None = None
        self._active_battle: BattleState | None = None

    def consume_lines(self, lines: list[str]) -> None:
        index = 0
        total = len(lines)
        while index < total:
            line = lines[index].rstrip("\r\n")

            if self._is_task_boundary(lines, index):
                title = self._extract_boundary_title(lines[index + 1])
                self._handle_task_boundary(title)
                index += 3
                continue

            if self._is_battle_boundary(line):
                self._handle_battle_boundary()
                index += 1
                continue

            self._consume_regular_line(line)
            index += 1

    def snapshot(self, script_name: str = "", tail_lines: list[str] | None = None) -> dict[str, Any]:
        parser = copy.deepcopy(self)
        if tail_lines:
            parser.consume_lines(tail_lines)
        parser._close_active_battle()
        parser._close_active_task()

        tasks = parser._build_tasks_payload()
        return {
            "script_name": script_name,
            "total_runtime_seconds": parser._current_total_runtime(),
            "total_task_run_count": sum(task["run_count"] for task in tasks.values()),
            "total_battle_count": sum(
                0 if task["battle"] is None else int(task["battle"]["count"])
                for task in tasks.values()
            ),
            "tasks": tasks,
        }

    @classmethod
    def parse_lines(cls, lines: list[str], script_name: str = "") -> dict[str, Any]:
        parser = cls()
        parser.consume_lines(lines)
        return parser.snapshot(script_name=script_name)

    @staticmethod
    def _is_task_boundary(lines: list[str], index: int) -> bool:
        if index + 2 >= len(lines):
            return False
        return bool(
            _EQ_LINE_RE.match(lines[index].strip())
            and _TITLE_LINE_RE.match(lines[index + 1].strip())
            and _EQ_LINE_RE.match(lines[index + 2].strip())
        )

    @staticmethod
    def _extract_boundary_title(line: str) -> str:
        matched = _TITLE_LINE_RE.match(line.strip())
        return matched.group("title").strip() if matched else ""

    @staticmethod
    def _extract_timestamp(line: str) -> datetime | None:
        matched = _TIMESTAMP_RE.match(line.strip())
        if not matched:
            return None
        return datetime.strptime(matched.group("ts"), "%Y-%m-%d %H:%M:%S.%f")

    @staticmethod
    def _is_battle_boundary(line: str) -> bool:
        matched = _TITLE_LINE_RE.match(line.strip())
        if not matched:
            return False
        return matched.group("title").strip().upper() == _BATTLE_TITLE

    def _handle_task_boundary(self, title: str) -> None:
        self._close_active_battle()
        self._close_active_task()
        if title.upper() == _START_TITLE:
            self._handle_start_boundary()
            self._pending_task_start_name = None
            return
        self._pending_task_start_name = self._pending_task_name or title
        self._pending_task_name = None

    def _handle_start_boundary(self) -> None:
        runtime = self.runtime
        if not runtime.saw_start_boundary:
            runtime.saw_start_boundary = True
            runtime.total_duration = self._append_duration(
                runtime.total_duration,
                runtime.pre_start_first,
                runtime.region_last,
            )
        else:
            runtime.total_duration = self._append_duration(
                runtime.total_duration,
                runtime.session_start,
                runtime.region_last,
            )
        runtime.session_start = None
        runtime.region_last = None

    def _handle_battle_boundary(self) -> None:
        self._close_active_battle()
        if self._active_task is not None:
            self._pending_battle_start = True

    def _consume_regular_line(self, line: str) -> None:
        matched = _TASK_LINE_RE.search(line)
        if matched:
            self._pending_task_name = matched.group("task").strip()

        ts = self._extract_timestamp(line)
        if ts is None:
            return

        self._consume_runtime_timestamp(ts)

        if self._pending_task_start_name:
            self._active_task = TaskRunState(name=self._pending_task_start_name, start_time=ts, last_time=ts)
            self._pending_task_start_name = None
        elif self._active_task is not None:
            self._active_task.last_time = ts

        if self._pending_battle_start and self._active_task is not None:
            self._active_battle = BattleState(start_time=ts, last_time=ts)
            self._pending_battle_start = False
        elif self._active_battle is not None:
            self._active_battle.last_time = ts

        # 战斗结束标记行：立即闭合当前战斗，避免战后动作计入战斗时长（修复3）
        if self._active_battle is not None and any(marker in line for marker in _BATTLE_END_MARKERS):
            self._close_active_battle()

    def _consume_runtime_timestamp(self, ts: datetime) -> None:
        runtime = self.runtime
        runtime.region_last = ts
        if not runtime.saw_start_boundary:
            if runtime.pre_start_first is None:
                runtime.pre_start_first = ts
            return
        if runtime.session_start is None:
            runtime.session_start = ts

    def _close_active_battle(self) -> None:
        if self._active_task is None or self._active_battle is None:
            self._active_battle = None
            self._pending_battle_start = False
            return

        start_time = self._active_battle.start_time
        end_time = self._active_battle.last_time
        if start_time is not None and end_time is not None and end_time >= start_time:
            duration = round((end_time - start_time).total_seconds(), 3)
            self._active_task.battle_count += 1
            self._active_task.battle_total_duration_seconds = round(
                self._active_task.battle_total_duration_seconds + duration,
                3,
            )

        self._active_battle = None
        self._pending_battle_start = False

    def _close_active_task(self) -> None:
        if self._active_task is None:
            self._pending_task_start_name = None
            return

        start_time = self._active_task.start_time
        end_time = self._active_task.last_time
        if start_time is None or end_time is None or end_time < start_time:
            self._active_task = None
            self._pending_task_start_name = None
            return

        duration = round((end_time - start_time).total_seconds(), 3)
        task_data = self.summary.setdefault(
            self._active_task.name,
            {
                "run_count": 0,
                "total_duration_seconds": 0.0,
                "battle_count": 0,
                "battle_total_duration_seconds": 0.0,
                "runs": [],
            },
        )
        task_data["run_count"] += 1
        task_data["total_duration_seconds"] = round(task_data["total_duration_seconds"] + duration, 3)
        task_data["battle_count"] += self._active_task.battle_count
        task_data["battle_total_duration_seconds"] = round(
            task_data["battle_total_duration_seconds"] + self._active_task.battle_total_duration_seconds,
            3,
        )
        task_data["runs"].append(
            {
                "start_time": start_time.strftime("%Y-%m-%d %H:%M:%S.%f")[:-3],
                "end_time": end_time.strftime("%Y-%m-%d %H:%M:%S.%f")[:-3],
                "duration_seconds": duration,
                "battle": self._build_battle_payload(
                    self._active_task.battle_count,
                    self._active_task.battle_total_duration_seconds,
                ),
            }
        )

        self._active_task = None
        self._pending_task_start_name = None

    def _build_tasks_payload(self) -> dict[str, dict[str, Any]]:
        cleaned_tasks: dict[str, dict[str, Any]] = {}
        for task_name, task_data in self.summary.items():
            cleaned_tasks[task_name] = {
                "run_count": int(task_data.get("run_count", 0)),
                "total_duration_seconds": round(float(task_data.get("total_duration_seconds", 0.0)), 3),
                "battle": self._build_battle_payload(
                    int(task_data.get("battle_count", 0)),
                    float(task_data.get("battle_total_duration_seconds", 0.0)),
                ),
                "runs": task_data.get("runs", []),
            }
        return cleaned_tasks

    def _current_total_runtime(self) -> float:
        runtime = self.runtime
        if runtime.saw_start_boundary:
            total_duration = self._append_duration(runtime.total_duration, runtime.session_start, runtime.region_last)
        else:
            total_duration = self._append_duration(0.0, runtime.pre_start_first, runtime.region_last)
        return round(total_duration, 3)

    @staticmethod
    def _build_battle_payload(count: int, total_duration_seconds: float) -> dict[str, Any] | None:
        if count <= 0:
            return None
        return {
            "count": int(count),
            "avg_duration_seconds": round(total_duration_seconds / count, 3),
        }

    @staticmethod
    def _append_duration(total_duration: float, start_time: datetime | None, end_time: datetime | None) -> float:
        if start_time is None or end_time is None or end_time < start_time:
            return total_duration
        return round(total_duration + (end_time - start_time).total_seconds(), 3)


class MultiStatAggregator:
    """解析日志中的 [STAT] 单行事件，聚合成多账号统计。"""

    _TRACKED_EVENTS = frozenset({
        "acc_start",
        "acc_end",
        "switch",
        "task_start",
        "task_end",
        "error",
        "battle",
        "coop",
        "mshop",
    })

    def __init__(self) -> None:
        self._accounts: dict[tuple[str, str, str], MultiAccountState] = {}
        self._active_key: tuple[str, str, str] | None = None
        self._pending_account: MultiAccountState | None = None
        # 战斗边界计时上下文：复用 LogStatsParser 的 BattleState 结构
        self._active_battle: BattleState | None = None
        self._pending_battle_start: bool = False
        self._current_task_name: str | None = None
        self._current_task_start_ts: datetime | None = None  # 当前子任务起点时间戳，供任务记录输出开始时间（需求3）
        self._current_task_battle_count: int = 0
        self._current_task_battle_duration: float = 0.0
        # 战斗开始时的账号归属快照：防止战斗中途 acc_start 切换 _active_key 导致耗时错账
        self._battle_owner_key: tuple[str, str, str] | None = None
        # 运行段/会话追踪：会话按 run_start 事件切分（一次调度 = 一个会话）
        self._open_account_key: tuple[str, str, str] | None = None  # 当前有未闭合运行段的账号
        self._session_index: int = 0  # 当前会话索引
        self._run_started: bool = False  # 是否已见到首个 run_start（首个保持索引 0，其后 +1）

    # 历史 STAT 最多尝试八条续行；达到上限仍无法解析时也释放增量读取的残块，
    # 避免损坏日志使 tail 随文件增长。新写入的 STAT 已不再按控制台宽度折行。
    _STAT_CONTINUATION_LIMIT = 8

    def consume_lines(self, lines: list[str]) -> None:
        """逐行扫描，提取 [STAT] JSON 事件并分发处理。

        RichHandler 把一次 logger.info("[STAT] "+json) 写成相邻多行：
        第一行是 ``时间戳 | stat_log.py | INFO | [STAT]``（无 JSON），
        其后才是纯 JSON 的续行（无时间戳，文件 console 定宽 160 会把长载荷
        折成一段以上）。因此需要把前缀行与全部续行合并后再解析，见
        ``_parse_json_block``；旧格式（前缀行内自带 JSON）仍兼容。

        同时检测战斗边界线（GENERAL BATTLE START），按时间戳行计算真实战斗耗时。
        """
        index = 0
        total = len(lines)
        while index < total:
            raw = lines[index].rstrip("\r\n")

            # 检测战斗边界 ───────────── GENERAL BATTLE START ─────────────
            if LogStatsParser._is_battle_boundary(raw):
                self._handle_battle_boundary()
                index += 1
                continue

            ts = LogStatsParser._extract_timestamp(raw)
            if ts is None:
                index += 1
                continue

            # 任一含时间戳的行都更新战斗计时（无论是否 STAT 行）
            self._update_battle_timestamp(ts)

            # 战斗结束标记行：立即闭合当前战斗，避免战后动作计入战斗时长（修复3）；
            # 不能 continue——标记行可能同时是旧格式内联 STAT 行，事件仍需解析（审查m3）
            if self._active_battle is not None and any(marker in raw for marker in _BATTLE_END_MARKERS):
                self._close_active_battle()

            # 命中 [STAT]（带或不带尾随空格/JSON）
            prefix_idx = raw.find("[STAT]")
            if prefix_idx == -1:
                index += 1
                continue
            # 前缀行内自带 JSON（旧格式）或紧随其后的续行，一并交给块解析
            payload = self._parse_json_block(lines, index)
            if payload is not None:
                self._consume_event(payload, ts)
            index += 1

    @classmethod
    def _parse_json_block(cls, lines: list[str], index: int) -> dict[str, Any] | None:
        """解析以 ``lines[index]`` 为前缀行的 STAT 消息，必要时跨续行拼接。

        只拼接紧邻且无时间戳的续行，普通日志与任务分隔线都会结束当前块。
        逐行去掉 Rich 的行尾补位空格，但这不能无损恢复历史载荷：断点处的原始
        空格可能已被吞掉，也无法与补位区分，身份字段落在断点时同样可能受影响。
        本方法仅尽力兼容旧统计；新日志由 RichFileHandler 保证原始 JSON 单行落盘。
        """
        head = lines[index].rstrip("\r\n") if index < len(lines) else ""
        payload = cls._extract_payload(head)
        if payload is not None:
            return payload
        # JSON 可能已经在 [STAT] 同行开始，不能丢掉这段再从下一行单独解析。
        buffer = head.partition("[STAT]")[2].strip()
        limit = min(len(lines), index + 1 + cls._STAT_CONTINUATION_LIMIT)
        for cursor in range(index + 1, limit):
            line = lines[cursor].rstrip("\r\n")
            # 分隔线没有时间戳，同样不能当作 JSON 的一部分或跨过它借用后续文本。
            if (LogStatsParser._extract_timestamp(line) is not None
                    or _EQ_LINE_RE.match(line.strip()) or _TITLE_LINE_RE.match(line.strip())):
                break
            buffer += line.rstrip()
            # STAT 载荷必须从对象开头开始；不从无关正文中另找一个花括号补造事件。
            if buffer.strip() and not buffer.lstrip().startswith("{"):
                return None
            payload = cls._parse_json_line(buffer)
            if payload is not None:
                return payload
        return None

    @classmethod
    def _dangling_stat_size(cls, lines: list[str]) -> int:
        """仅回扣文件末端可能尚未写完的 STAT，已结束或超限的坏块直接释放。

        拼不出 JSON 也可能是记录损坏。遇到下一条时间戳或分隔线即确定旧块已结束；
        达到续行上限后再等待也无济于事。只有仍可补齐的前缀与续行整体留在 tail。
        """
        first = max(0, len(lines) - cls._STAT_CONTINUATION_LIMIT)
        for back in range(len(lines) - 1, first - 1, -1):
            raw = lines[back].strip()
            if _EQ_LINE_RE.match(raw) or _TITLE_LINE_RE.match(raw):
                return 0
            if LogStatsParser._extract_timestamp(raw) is None:
                continue
            # 只把带时间戳的行视作协议起点，续行值内的 [STAT] 只是普通文本。
            if "[STAT]" not in raw:
                return 0
            if cls._parse_json_block(lines, back) is None:
                return len(lines) - back
            return 0
        return 0

    def _handle_battle_boundary(self) -> None:
        """处理战斗边界线：关闭前一个战斗，标记下一个战斗待开始（仅当前有活跃子任务时）。"""
        self._close_active_battle()
        if self._current_task_name is not None:
            self._pending_battle_start = True

    def _update_battle_timestamp(self, ts: datetime) -> None:
        """根据时间戳启动或更新活跃战斗计时。"""
        if self._pending_battle_start:
            self._active_battle = BattleState(start_time=ts, last_time=ts)
            self._pending_battle_start = False
            # 快照战斗开始时的活跃账号，防止战斗中途 acc_start 切换归属
            self._battle_owner_key = self._active_key
        elif self._active_battle is not None:
            self._active_battle.last_time = ts

    def _close_active_battle(self) -> None:
        """关闭当前活跃战斗，将耗时累加到战斗开始时的账号与当前子任务。"""
        if self._active_battle is None:
            self._pending_battle_start = False
            return

        start_time = self._active_battle.start_time
        end_time = self._active_battle.last_time
        # 仅正耗时战斗计入统计，避免边界后紧接 task_end 等零耗时"空战斗"
        if start_time is not None and end_time is not None and end_time > start_time:
            duration = round((end_time - start_time).total_seconds(), 3)
            self._accumulate_battle_duration(duration)

        self._active_battle = None
        self._pending_battle_start = False
        self._battle_owner_key = None

    def _accumulate_battle_duration(self, duration: float) -> None:
        """将一次战斗耗时累加到战斗开始时的账号（而非当前活跃账号）与当前子任务计数器。"""
        if self._battle_owner_key is None:
            return
        account = self._accounts.get(self._battle_owner_key)
        if account is None:
            return
        account.battle_total_duration_seconds = round(
            account.battle_total_duration_seconds + duration, 3
        )
        # 边界计时同步累加账号级战斗次数，弥补无 STAT battle 事件时的计数缺失
        account.boundary_battle_count += 1
        self._current_task_battle_count += 1
        self._current_task_battle_duration = round(
            self._current_task_battle_duration + duration, 3
        )

    def _close_open_segment(self, end_ts: datetime) -> None:
        """以给定终点时间闭合当前未闭合的运行段，并计入对应账号的运行段列表。"""
        key = self._open_account_key
        if key is None:
            return
        account = self._accounts.get(key)
        if account is not None and account.segment_open_start is not None:
            start = account.segment_open_start
            if end_ts >= start:
                duration = round((end_ts - start).total_seconds(), 3)
                account.segments.append(
                    {
                        "start": start,
                        "end": end_ts,
                        "duration": duration,
                        "session": account.segment_open_session,
                    }
                )
            account.segment_open_start = None
            account.segment_open_session = None
        self._open_account_key = None

    def _close_open_segment_pending(self) -> None:
        """快照收尾：用未闭合段所属账号的最后事件时刻闭合该段。"""
        if self._open_account_key is None:
            return
        account = self._accounts.get(self._open_account_key)
        end_ts = account.last_time if account is not None else None
        if end_ts is None:
            self._open_account_key = None
            return
        self._close_open_segment(end_ts)

    def _reset_task_context(self) -> None:
        """清空子任务级追踪状态，防止跨运行会话的战斗错误归属。"""
        self._current_task_name = None
        self._current_task_start_ts = None
        self._current_task_battle_count = 0
        self._current_task_battle_duration = 0.0

    def _begin_account_segment(self, payload: dict[str, Any], ts: datetime) -> None:
        """以 ts 抢占闭合当前未闭合运行段，并为事件所属账号开启新运行段。"""
        key = self._event_key(payload)
        # 抢占闭合：上一账号未见结束标志时，以本次起点为终点闭合其运行段
        self._close_open_segment(end_ts=ts)
        # 确保账号聚合状态存在；重复开段仅新增运行段，不清零已累加的战斗/协作等字段
        if key in self._accounts:
            account = self._accounts[key]
            self._active_key = key
            self._pending_account = None
        else:
            account = self._create_account_state(payload, ts)
        # 开启新运行段
        account.segment_open_start = ts
        account.segment_open_session = self._session_index
        account.last_time = ts
        if account.start_time is None:
            account.start_time = ts
        self._open_account_key = key

    def _build_sessions_payload(self) -> list[dict[str, Any]]:
        """按运行段的会话索引聚合出每次 MultiAcc 运行会话的元数据（需求2）。"""
        grouped: dict[int, dict[str, Any]] = {}
        for account in self._accounts.values():
            for seg in account.segments:
                idx = seg["session"]
                group = grouped.setdefault(
                    idx,
                    {"start": None, "end": None, "duration": 0.0, "accounts": set()},
                )
                if group["start"] is None or seg["start"] < group["start"]:
                    group["start"] = seg["start"]
                if group["end"] is None or seg["end"] > group["end"]:
                    group["end"] = seg["end"]
                group["duration"] = round(group["duration"] + seg["duration"], 3)
                group["accounts"].add(
                    (account.character, account.account, account.svr)
                )
        sessions: list[dict[str, Any]] = []
        for idx in sorted(grouped):
            group = grouped[idx]
            sessions.append(
                {
                    "index": idx,
                    "start_time": _format_multi_ts(group["start"]),
                    "end_time": _format_multi_ts(group["end"]),
                    "duration_seconds": group["duration"],
                    "account_count": len(group["accounts"]),
                }
            )
        return sessions

    def snapshot(
        self, tail_lines: list[str] | None = None
    ) -> dict[str, Any] | None:
        """返回当前多账号统计快照，无账号数据时返回 None。"""
        aggregator = copy.deepcopy(self)
        if tail_lines:
            aggregator.consume_lines(tail_lines)
        # 关闭正在进行的战斗，确保不计时的战斗被清理
        aggregator._close_active_battle()
        # 关闭尚未闭合的运行段（以该账号最后事件时刻为终点），确保耗时入账
        aggregator._close_open_segment_pending()
        if not aggregator._accounts:
            return None
        accounts = [ac.to_payload() for ac in aggregator._accounts.values()]
        accounts.sort(
            key=lambda item: (
                item.get("character", ""),
                item.get("account", ""),
                item.get("svr", ""),
            )
        )
        # 总耗时 = 各账号运行段耗时之和（账号在会话内顺序执行，累加即真实运行时长）
        total_duration = round(sum(ac["duration_seconds"] for ac in accounts), 3)
        return {
            "accounts": accounts,
            "sessions": aggregator._build_sessions_payload(),
            "total_duration_seconds": total_duration,
        }

    @staticmethod
    def _extract_payload(line: str) -> dict[str, Any] | None:
        """从日志行中提取 [STAT] 后的 JSON 负载，非法 JSON 静默忽略。"""
        idx = line.find(_STAT_PREFIX)
        if idx == -1:
            return None
        json_str = line[idx + len(_STAT_PREFIX):]
        return MultiStatAggregator._parse_json_line(json_str)

    @staticmethod
    def _parse_json_line(text: str) -> dict[str, Any] | None:
        """尝试把一行文本解析成 JSON 字典，失败或非 dict 时返回 None。"""
        candidate = text.strip()
        # RichHandler 的续行是纯 JSON，定位首个 ``{`` 到行尾即可。
        brace = candidate.find("{")
        if brace == -1:
            return None
        try:
            payload = json.loads(candidate[brace:])
        except json.JSONDecodeError:
            return None
        return payload if isinstance(payload, dict) else None

    @staticmethod
    def _event_key(payload: dict[str, Any]) -> tuple[str, str, str]:
        """按角色、账号、区服定位同一账号的统计段。"""
        return (
            str(payload.get("char", "") or "").strip(),
            str(payload.get("acc", "") or "").strip(),
            str(payload.get("svr", "") or "").strip(),
        )

    @classmethod
    def _has_complete_identity(cls, payload: dict[str, Any]) -> bool:
        """最小可展示账号统计必须同时具备角色、账号和区服。"""
        char, acc, svr = cls._event_key(payload)
        return bool(char and acc and svr)

    @classmethod
    def _should_track_event(cls, payload: dict[str, Any]) -> bool:
        """只对多账号统计相关事件建立账号聚合状态。"""
        return str(payload.get("ev", "") or "").strip() in cls._TRACKED_EVENTS

    def _store_account_state(
        self,
        key: tuple[str, str, str],
        state: MultiAccountState,
    ) -> MultiAccountState:
        """保存正式账号状态并刷新当前活动账号指针。"""
        self._accounts[key] = state
        self._active_key = key
        self._pending_account = None
        return state

    def _create_account_state(
        self,
        payload: dict[str, Any],
        ts: datetime,
        key: tuple[str, str, str] | None = None,
    ) -> MultiAccountState:
        """为一个账号事件创建最小聚合状态，缺失字段时使用默认值。"""
        resolved_key = key or self._event_key(payload)
        state = MultiAccountState(
            account=resolved_key[1],
            character=resolved_key[0],
            svr=resolved_key[2],
            start_time=ts,
            last_time=ts,
        )
        if self._has_complete_identity(payload):
            return self._store_account_state(resolved_key, state)
        self._pending_account = state
        return state

    def _promote_pending_account(
        self,
        payload: dict[str, Any],
        key: tuple[str, str, str],
    ) -> MultiAccountState | None:
        """把匿名待定账号提升为携带完整身份信息的正式账号。"""
        pending = self._pending_account
        if pending is None:
            return None
        if not self._has_complete_identity(payload):
            return None
        pending.character = key[0]
        pending.account = key[1]
        pending.svr = key[2]
        return self._store_account_state(key, pending)

    def _account_for_event(
        self,
        payload: dict[str, Any],
        ts: datetime,
        *,
        create_placeholder: bool = False,
    ) -> MultiAccountState | None:
        """获取当前事件所属账号；必要时为不完整事件缓存最小待定状态。"""
        key = self._event_key(payload)
        if key in self._accounts:
            self._active_key = key
            self._pending_account = None
            return self._accounts[key]

        promoted_account = self._promote_pending_account(payload, key)
        if promoted_account is not None:
            return promoted_account

        has_identity = any(key)
        if not has_identity:
            if self._active_key is not None:
                return self._accounts.get(self._active_key)
            if create_placeholder and self._should_track_event(payload):
                return self._create_account_state(payload, ts, key=key)
            return self._pending_account

        if create_placeholder and self._should_track_event(payload):
            return self._create_account_state(payload, ts, key=key)
        return None

    def _consume_event(self, payload: dict[str, Any], ts: datetime) -> None:
        """根据 STAT 事件类型更新账号聚合状态。"""
        ev = str(payload.get("ev", ""))

        if ev == "run_start":
            # 一次运行的开始边界：闭合上一会话遗留状态并推进会话索引（首个 run_start 保持 0）
            self._close_active_battle()
            self._close_open_segment_pending()
            self._reset_task_context()
            if self._run_started:
                self._session_index += 1
            self._run_started = True
            return

        if ev == "run_end":
            # 一次运行的结束边界：以事件时刻闭合活跃战斗与未闭合运行段
            self._close_active_battle()
            self._close_open_segment(end_ts=ts)
            # 清空子任务追踪状态：run_end 之后同一日志里其他任务（如 Orochi）的战斗边界
            # 不得再武装战斗并错误归属到上一会话的账号（审查发现的跨会话泄漏口）
            self._reset_task_context()
            return

        if ev == "switch_start":
            # 上一账号的战斗若未闭合（task_end/acc_end 丢失），先就地结算，
            # 时长不得延伸进本账号的切号窗口（审查m4）
            self._close_active_battle()
            # 切号起点：以此刻抢占闭合上一账号运行段，目标账号耗时（含切号）从此刻起算
            self._begin_account_segment(payload, ts)
            return

        if ev == "acc_start":
            key = self._event_key(payload)
            if self._open_account_key == key and key in self._accounts:
                # 正常路径：段已由 switch_start 开启，不关闭重开，保留切号耗时
                account = self._accounts[key]
                self._active_key = key
                self._pending_account = None
                account.last_time = ts
                if account.start_time is None:
                    account.start_time = ts
                return
            # 兜底路径（无 switch_start 的旧日志或异常流）：维持原有闭合重开行为
            self._begin_account_segment(payload, ts)
            return

        account = self._account_for_event(payload, ts, create_placeholder=True)
        if account is None:
            return

        if ev == "switch":
            account.switch_ok = payload.get("ok")
            account.last_time = ts
        elif ev == "task_start":
            # 上一子任务的战斗若未闭合（task_end 丢失），先就地结算，防止错误归属到本任务（修复4）
            self._close_active_battle()
            # 记录当前子任务名与起点时间，重置任务级战斗计数器
            self._current_task_name = str(payload.get("task", ""))
            self._current_task_start_ts = ts
            self._current_task_battle_count = 0
            self._current_task_battle_duration = 0.0
            account.last_time = ts
        elif ev == "task_end":
            # 关闭可能仍在进行的战斗，将耗时归入当前子任务
            self._close_active_battle()
            task_battle_avg = (
                round(self._current_task_battle_duration / self._current_task_battle_count, 3)
                if self._current_task_battle_count > 0 else 0.0
            )
            account.tasks.append({
                "task": str(payload.get("task", "")),
                "ok": bool(payload.get("ok", False)),
                "start_time": _format_multi_ts(self._current_task_start_ts),  # 子任务起点（需求3 hover 展示）
                "duration_seconds": float(payload.get("dur", 0.0) or 0.0),
                "battle_count": self._current_task_battle_count,
                "battle_total_duration_seconds": self._current_task_battle_duration,
                "battle_avg_duration_seconds": task_battle_avg,
            })
            account.last_time = ts
            self._current_task_name = None
            self._current_task_start_ts = None
            self._current_task_battle_count = 0
            self._current_task_battle_duration = 0.0
        elif ev == "error":
            account.errors.append({
                "task": str(payload.get("task", "")),
                "etype": str(payload.get("etype", "")),
                "emsg": str(payload.get("emsg", "")),
                "time": _format_multi_ts(ts),  # 事件时刻，供前端按会话筛选（修复2）
            })
            account.error_count += 1
            account.last_time = ts
        elif ev == "battle":
            # 发出侧（alliedteam）给出的是增量场数，按累加统计；count<=0 的清理事件累加无影响
            new_count = int(payload.get("count", 0) or 0)
            if new_count > 0:
                account.battle_count += new_count
            account.last_time = ts
        elif ev == "coop":
            account.coops.append({
                "ctype": str(payload.get("ctype", "")),
                "real": bool(payload.get("real", False)),
                "time": _format_multi_ts(ts),  # 事件时刻，供前端按会话筛选（需求2）
            })
            total = int(payload.get("total", 0) or 0)
            if total > account.coop_total:
                account.coop_total = total
            account.last_time = ts
        elif ev == "mshop":
            account.mshops.append({
                "goods": str(payload.get("goods", "")),
                "price": payload.get("price", 0),
                "time": _format_multi_ts(ts),  # 事件时刻，供前端按会话筛选（需求2）
            })
            account.last_time = ts
        elif ev == "acc_end":
            # 本账号的战斗若未闭合（task_end 丢失），以此刻就地结算（审查m4）
            self._close_active_battle()
            account.last_time = ts
            # 正常结束：以 acc_end 时刻闭合本账号运行段
            if self._open_account_key == self._event_key(payload):
                self._close_open_segment(end_ts=ts)


@dataclass
class TodayLogCache:
    day: str
    path: Path
    position: int = 0
    signature: tuple[int, int] = (0, 0)
    parser: LogStatsParser = field(default_factory=LogStatsParser)
    multi_aggregator: MultiStatAggregator = field(default_factory=MultiStatAggregator)
    tail_lines: list[str] = field(default_factory=list)
    snapshot: dict[str, Any] = field(default_factory=dict)


class LogStatsService:
    def __init__(self) -> None:
        self._today_cache: dict[str, TodayLogCache] = {}
        self._historical_cache: dict[tuple[str, str], HistoricalStatsCacheEntry] = {}

    @staticmethod
    def _normalize_script_name(script_name: str) -> str:
        name = str(script_name or "").strip()
        return name.split("_", 1)[0] if "_" in name else name

    def build_stats(self, script_name: str, target_day: date) -> dict[str, Any]:
        normalized = self._normalize_script_name(script_name)
        if target_day == date.today():
            return self._build_today_stats(normalized)
        return self._build_historical_stats(normalized, target_day)

    def list_available_dates(self, script_name: str) -> dict[str, Any]:
        normalized = self._normalize_script_name(script_name)
        dates: set[str] = set()
        if LOG_ROOT.exists():
            for path in LOG_ROOT.iterdir():
                if not path.is_file():
                    continue

                matched = _LOG_FILE_RE.match(path.name)
                if not matched:
                    continue

                day_text = matched.group("day")
                try:
                    date.fromisoformat(day_text)
                except ValueError:
                    continue

                if matched.group("script") != normalized:
                    continue
                dates.add(day_text)

        return {
            "script_name": normalized,
            "dates": sorted(dates, reverse=True),
        }

    async def stream_events(self, script_name: str, target_day: date):
        normalized = self._normalize_script_name(script_name)
        snapshot = self.build_stats(normalized, target_day)
        yield self._encode_sse("snapshot", snapshot)

        last_signature = self._today_log_signature(normalized)
        last_snapshot = copy.deepcopy(snapshot)
        while True:
            await asyncio.sleep(POLL_INTERVAL_SECONDS)
            current_signature = self._today_log_signature(normalized)
            if current_signature == last_signature:
                continue
            last_signature = current_signature
            current_snapshot = self.build_stats(normalized, target_day)
            payload = self._build_update_payload(normalized, last_snapshot, current_snapshot)
            if payload is None:
                continue
            last_snapshot = copy.deepcopy(current_snapshot)
            yield self._encode_sse("update", payload)

    def _build_historical_stats(self, script_name: str, target_day: date) -> dict[str, Any]:
        self._cleanup_historical_cache()
        cache_key = (script_name, target_day.isoformat())
        cached = self._historical_cache.get(cache_key)
        if cached is not None and cached.expires_at > datetime.now():
            return copy.deepcopy(cached.payload)

        payload = self._parse_log_file(self._log_path(script_name, target_day))
        self._historical_cache[cache_key] = HistoricalStatsCacheEntry(
            expires_at=datetime.now() + HISTORICAL_CACHE_TTL,
            payload=copy.deepcopy(payload),
        )
        return payload

    def _build_today_stats(self, script_name: str) -> dict[str, Any]:
        cache = self._refresh_today_cache(script_name)
        return copy.deepcopy(cache.snapshot)

    def _cleanup_historical_cache(self) -> None:
        now = datetime.now()
        expired_keys = [
            key for key, entry in self._historical_cache.items()
            if entry.expires_at <= now
        ]
        for key in expired_keys:
            del self._historical_cache[key]

    def _build_update_payload(
        self,
        script_name: str,
        previous_snapshot: dict[str, Any],
        current_snapshot: dict[str, Any],
    ) -> dict[str, Any] | None:
        previous_tasks = previous_snapshot.get("tasks", {}) if isinstance(previous_snapshot, dict) else {}
        current_tasks = current_snapshot.get("tasks", {}) if isinstance(current_snapshot, dict) else {}

        changed_tasks: dict[str, Any] = {}
        for task_name, task_data in current_tasks.items():
            if previous_tasks.get(task_name) != task_data:
                changed_tasks[task_name] = task_data

        removed_tasks = sorted(name for name in previous_tasks.keys() if name not in current_tasks)

        totals_changed = any(
            previous_snapshot.get(field) != current_snapshot.get(field)
            for field in ("total_runtime_seconds", "total_task_run_count", "total_battle_count")
        )
        multi_changed = previous_snapshot.get("multi") != current_snapshot.get("multi")
        if not totals_changed and not changed_tasks and not removed_tasks and not multi_changed:
            return None

        return {
            "script_name": script_name,
            "total_runtime_seconds": current_snapshot.get("total_runtime_seconds", 0),
            "total_task_run_count": current_snapshot.get("total_task_run_count", 0),
            "total_battle_count": current_snapshot.get("total_battle_count", 0),
            "changed_tasks": changed_tasks,
            "removed_tasks": removed_tasks,
            "multi": current_snapshot.get("multi"),
        }

    def _parse_log_file(self, path: Path) -> dict[str, Any]:
        script_name = self._extract_script_name_from_path(path)
        if not path.exists():
            return self._empty_stats(script_name)

        with path.open("r", encoding="utf-8", errors="ignore") as file:
            lines = file.readlines()
        return self._parse_lines(lines, script_name=script_name)

    @staticmethod
    def _extract_script_name_from_path(path: Path) -> str:
        parts = path.stem.split("_", 1)
        return parts[1] if len(parts) == 2 else path.stem

    @staticmethod
    def _parse_lines(lines: list[str], script_name: str = "") -> dict[str, Any]:
        payload = LogStatsParser.parse_lines(lines, script_name=script_name)
        # 同时解析多账号 STAT 行
        multi_aggregator = MultiStatAggregator()
        multi_aggregator.consume_lines(lines)
        multi = multi_aggregator.snapshot()
        return {
            "script_name": payload.get("script_name", script_name),
            "total_runtime_seconds": round(float(payload.get("total_runtime_seconds", 0.0)), 3),
            "total_task_run_count": int(payload.get("total_task_run_count", 0)),
            "total_battle_count": int(payload.get("total_battle_count", 0)),
            "tasks": payload.get("tasks", {}),
            "multi": multi,
        }

    def _today_log_signature(self, script_name: str) -> tuple[int, int]:
        today_file = self._today_log_path(script_name)
        if not today_file.exists():
            return (0, 0)
        stat = today_file.stat()
        return (int(stat.st_size), int(stat.st_mtime_ns))

    @staticmethod
    def _log_path(script_name: str, target_day: date) -> Path:
        return LOG_ROOT / f"{target_day.isoformat()}_{script_name}.txt"

    def _today_log_path(self, script_name: str) -> Path:
        return self._log_path(script_name, date.today())

    def _refresh_today_cache(self, script_name: str) -> TodayLogCache:
        today = date.today().isoformat()
        today_file = self._today_log_path(script_name)
        cache = self._today_cache.get(script_name)

        if cache is not None and cache.day != today:
            cache = None

        if not today_file.exists():
            empty_cache = self._new_today_cache(today, today_file, script_name)
            self._today_cache[script_name] = empty_cache
            return empty_cache

        stat = today_file.stat()
        signature = (int(stat.st_size), int(stat.st_mtime_ns))
        if cache is not None and cache.path == today_file and cache.signature == signature:
            return cache

        should_reset = (
            cache is None
            or cache.path != today_file
            or stat.st_size < cache.position
        )
        if should_reset:
            cache = self._new_today_cache(today, today_file, script_name)

        with today_file.open("r", encoding="utf-8", errors="ignore") as file:
            if cache.position > 0:
                file.seek(cache.position)
            new_lines = file.readlines()
            cache.position = file.tell()

        merged_lines = self._merge_tail_lines(cache.tail_lines, new_lines)
        confirmed_lines, cache.tail_lines = self._split_confirmed_lines(merged_lines)
        if confirmed_lines:
            cache.parser.consume_lines(confirmed_lines)
            cache.multi_aggregator.consume_lines(confirmed_lines)

        cache.signature = signature
        cache.snapshot = cache.parser.snapshot(script_name=script_name, tail_lines=cache.tail_lines)
        cache.snapshot["multi"] = cache.multi_aggregator.snapshot(cache.tail_lines)
        self._today_cache[script_name] = cache
        return cache

    def _new_today_cache(self, today: str, today_file: Path, script_name: str) -> TodayLogCache:
        snapshot = self._empty_stats(script_name)
        return TodayLogCache(
            day=today,
            path=today_file,
            snapshot=snapshot,
        )

    @staticmethod
    def _merge_tail_lines(tail_lines: list[str], new_lines: list[str]) -> list[str]:
        """合并上轮回扣的 tail 与本轮新行；tail 末尾的半行与新行首段拼成完整行（审查M1）。"""
        if tail_lines and not tail_lines[-1].endswith("\n") and new_lines:
            return tail_lines[:-1] + [tail_lines[-1] + new_lines[0]] + new_lines[1:]
        return tail_lines + new_lines

    @staticmethod
    def _split_confirmed_lines(lines: list[str]) -> tuple[list[str], list[str]]:
        if not lines:
            return [], []

        # 半行回扣：文件尾部尚未写完换行的行不能当完整行消费，
        # 留在 tail 待下轮与后续字节拼接后再解析（审查M1）
        partial_tail: list[str] = []
        if not lines[-1].endswith("\n"):
            partial_tail = [lines[-1]]
            lines = lines[:-1]
            if not lines:
                return [], partial_tail

        if len(lines) >= 3 and LogStatsParser._is_task_boundary(lines, len(lines) - 3):
            return lines, partial_tail

        tail_size = 0
        if _EQ_LINE_RE.match(lines[-1].strip()):
            tail_size = 1
        if (
            len(lines) >= 2
            and _EQ_LINE_RE.match(lines[-2].strip())
            and _TITLE_LINE_RE.match(lines[-1].strip())
        ):
            tail_size = 2
        # 悬空 STAT 块回扣：RichHandler 把 STAT 写成"前缀行 + 若干续行"（文件
        # console 定宽 160，长载荷会折成一段以上），若轮询恰好切在块中间，整段
        # 需留在 tail 等余下续行到达后合并解析；否则续行下轮单独到达因无时间戳被
        # 跳过，事件永久丢失——新口径下丢失 run_start 会导致全天会话索引错位且
        # 不自愈（审查M1）
        if tail_size == 0:
            tail_size = MultiStatAggregator._dangling_stat_size(lines)

        if tail_size <= 0:
            return lines, partial_tail
        return lines[:-tail_size], lines[-tail_size:] + partial_tail

    @staticmethod
    def _empty_stats(script_name: str) -> dict[str, Any]:
        return {
            "script_name": script_name,
            "total_runtime_seconds": 0,
            "total_task_run_count": 0,
            "total_battle_count": 0,
            "tasks": {},
            "multi": None,
        }

    @staticmethod
    def _compact_json(payload: dict[str, Any]) -> str:
        return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))

    def _encode_sse(self, event: str, payload: dict[str, Any]) -> str:
        return f"event: {event}\ndata: {self._compact_json(payload)}\n\n"


log_stats_service = LogStatsService()
