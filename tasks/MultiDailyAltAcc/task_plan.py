"""MultiDailyAltAcc 模块级 morning / afternoon 任务计划。"""
from __future__ import annotations

import json
import os
import random
import re
import tempfile
from dataclasses import dataclass
from datetime import datetime, time, timedelta
from pathlib import Path
from typing import Mapping


TASK_PLAN_PATH = Path(__file__).with_name("task_plan.json")
TASK_KEYS = (
    "courtyard",
    "mail",
    "cooperation",
    "donatejade",
    "alliedteam_ap",
    "kekkaiActivation",
    "KekkaiUtilize",
    # 周奖励/神秘商店：早晚轮勾选决定哪个轮次负责领取，星期列表决定哪天开
    "weekaward",
    "mysteryshop",
)
# 回礼轮专属键：回礼时是否顺带翻勾协与神秘商店（只做发现，不做完整子任务流程）
RETURNGIFT_KEYS = (
    "cooperation",
    "mysteryshop",
)
# 已移除功能的 plan 键：撤掉「完成协作」后，旧文件里的遗留勾选必须忽略而不是
# 报错，否则用户升级后整轮任务直接起不来。
REMOVED_TASK_KEYS = (
    "complete_cooperation",
)

# 单用途阶段开关键：控制回礼/同心战斗这两个阶段本身是否开启。
# 与普通任务不同，这两个的 total_* 是轮次身份判定开关（next_run 靠它分流
# 下一阶段），不能被排程物化改写——plan 勾选只在运行时过滤（合成账号配置
# 时 AND 进去），关了就整轮空跑，轮转照常走到下个阶段。
SINGLE_PURPOSE_KEYS = (
    "returngift",
    "alliedteam_battle",
)

DEFAULT_TASK_PLAN = {
    "schedule": {
        "morning_time": "06:05",
        "afternoon_time": "18:05",
        "random_delay_minutes": 30,
        # 星期几开启周奖励/神秘商店（0=周一 … 6=周日）；空列表=不做。
        # 统一为执行日语义：列表=任务实际翻找的日子（早晚轮直接比对当日，
        # 回礼轮由前一晚排程判断次日）
        "weekaward_weekdays": [0],
        "mysteryshop_weekdays": [2, 5],
    },
    "morning": {
        "courtyard": False,
        "mail": True,
        "cooperation": True,
        # 捐勾/挂卡/蹭卡默认关：由用户按需在 plan 勾选
        "donatejade": False,
        "alliedteam_ap": True,
        "kekkaiActivation": False,
        "KekkaiUtilize": False,
        # 周奖励由早轮领取（星期门控：schedule.weekaward_weekdays）
        "weekaward": True,
        # 神秘商店早轮也翻（星期门控：schedule.mysteryshop_weekdays）
        "mysteryshop": True,
    },
    "afternoon": {
        "courtyard": True,
        "mail": False,
        "cooperation": True,
        # 捐勾/挂卡/蹭卡默认关：由用户按需在 plan 勾选
        "donatejade": False,
        "alliedteam_ap": False,
        "kekkaiActivation": False,
        "KekkaiUtilize": False,
        # 下午轮默认不领（避免与早轮重复领取）
        "weekaward": False,
        "mysteryshop": False,
    },
    # 回礼轮专属阶段：cooperation / mysteryshop 两键（见 RETURNGIFT_KEYS）。
    # mysteryshop 需再 AND schedule.mysteryshop_weekdays（执行日语义：列表=
    # 实际翻找日，回礼轮决策由前一晚排程判断次日是否命中）
    "returngift": {
        "cooperation": False,
        "mysteryshop": False,
    },
    # 单用途阶段本身是否开启（见 SINGLE_PURPOSE_KEYS）：默认全开；关闭则该
    # 阶段空跑（轮次照常启动、正常完成、照常排下一阶段），轮转链不受影响
    "single_purpose": {
        "returngift": True,
        "alliedteam_battle": True,
    },
}


class TaskPlanError(ValueError):
    """task_plan.json 不合法；调用方必须保留原文件并明确失败。"""


@dataclass(frozen=True)
class ScheduledTarget:
    base_time: datetime
    delay_minutes: int
    target: datetime


@dataclass(frozen=True)
class TaskPlan:
    morning_time: time
    afternoon_time: time
    random_delay_minutes: int
    morning: Mapping[str, bool]
    afternoon: Mapping[str, bool]
    # 回礼轮专属阶段：cooperation / mysteryshop 两键（见 RETURNGIFT_KEYS）
    returngift: Mapping[str, bool]
    # 单用途阶段开关：returngift / alliedteam_battle（见 SINGLE_PURPOSE_KEYS）
    single_purpose: Mapping[str, bool]
    # 星期几开启周奖励/神秘商店（0=周一 … 6=周日）；空列表=不做
    weekaward_weekdays: tuple = ()
    mysteryshop_weekdays: tuple = ()

    def enabled(self, phase: str, task: str) -> bool:
        if phase == "morning":
            if task not in TASK_KEYS:
                raise KeyError(f"Unknown MultiDaily task-plan key: {task}")
            return self.morning[task]
        if phase == "afternoon":
            if task not in TASK_KEYS:
                raise KeyError(f"Unknown MultiDaily task-plan key: {task}")
            return self.afternoon[task]
        if phase == "returngift":
            if task not in RETURNGIFT_KEYS:
                raise KeyError(f"Unknown MultiDaily returngift-phase key: {task}")
            return self.returngift[task]
        if phase == "single_purpose":
            if task not in SINGLE_PURPOSE_KEYS:
                raise KeyError(f"Unknown MultiDaily single_purpose-phase key: {task}")
            return self.single_purpose[task]
        raise ValueError(f"Unknown MultiDaily task-plan phase: {phase}")

    def schedule_target(self, phase: str, start_time: datetime) -> ScheduledTarget:
        if phase == "morning":
            planned_time = self.morning_time
        elif phase == "afternoon":
            planned_time = self.afternoon_time
        else:
            raise ValueError(f"Unknown MultiDaily task-plan phase: {phase}")

        base_time = start_time.replace(
            hour=planned_time.hour,
            minute=planned_time.minute,
            second=0,
            microsecond=0,
        )
        delay_minutes = random.randint(0, self.random_delay_minutes)
        return ScheduledTarget(
            base_time=base_time,
            delay_minutes=delay_minutes,
            target=base_time + timedelta(minutes=delay_minutes),
        )


def _drop_removed_plan_keys(raw: object) -> object:
    """剔除已移除功能的 plan 勾选，保证旧 plan 文件仍能解析。

    「完成协作」已从多账号日常撤掉，用户文件里遗留的勾选按无效处理，
    既不报错也不写回（已有文件永远只读）。
    """
    if not isinstance(raw, dict):
        return raw
    return {key: value for key, value in raw.items() if key not in REMOVED_TASK_KEYS}


def _parse_time(value: object, field: str) -> time:
    if not isinstance(value, str) or not re.fullmatch(r"\d{2}:\d{2}", value):
        raise TaskPlanError(f"{field} must be a HH:MM string")
    try:
        return time.fromisoformat(value)
    except ValueError as exc:
        raise TaskPlanError(f"{field} is not a valid 24-hour time: {value!r}") from exc


def _parse_phase(raw: object, phase: str, keys: tuple = TASK_KEYS) -> dict[str, bool]:
    """校验阶段表：键集合必须与 keys 完全一致，值全为布尔。"""
    if not isinstance(raw, dict):
        raise TaskPlanError(f"{phase} must be an object")
    actual = set(raw)
    expected = set(keys)
    if actual != expected:
        missing = sorted(expected - actual)
        unknown = sorted(actual - expected)
        details = []
        if missing:
            details.append(f"missing={missing}")
        if unknown:
            details.append(f"unknown={unknown}")
        raise TaskPlanError(f"{phase} task keys invalid ({', '.join(details)})")
    if any(type(raw[key]) is not bool for key in keys):
        raise TaskPlanError(f"{phase} task values must all be boolean")
    return {key: raw[key] for key in keys}


def _parse_weekdays(raw: object, field: str) -> tuple:
    """校验星期列表：int 0-6（0=周一），去重排序；非列表或越界即报错。"""
    if not isinstance(raw, list):
        raise TaskPlanError(f"{field} must be a list of integers (0=Monday .. 6=Sunday)")
    for value in raw:
        if type(value) is not int or not 0 <= value <= 6:
            raise TaskPlanError(f"{field} contains invalid weekday: {value!r} (0=Monday .. 6=Sunday)")
    return tuple(sorted(set(raw)))


def _validate_schedule(morning_time: time, afternoon_time: time, delay_minutes: int) -> None:
    morning_minutes = morning_time.hour * 60 + morning_time.minute
    afternoon_minutes = afternoon_time.hour * 60 + afternoon_time.minute
    if not 5 * 60 <= morning_minutes < 18 * 60:
        raise TaskPlanError("schedule.morning_time must stay between 05:00 and 17:59")
    if not 18 * 60 <= afternoon_minutes < 24 * 60:
        raise TaskPlanError("schedule.afternoon_time must stay between 18:00 and 23:59")
    if morning_minutes + delay_minutes >= 18 * 60:
        raise TaskPlanError("morning_time + random_delay_minutes must stay before 18:00")
    if afternoon_minutes + delay_minutes >= 24 * 60:
        raise TaskPlanError("afternoon_time + random_delay_minutes must stay before midnight")


def parse_task_plan(raw: object) -> TaskPlan:
    if not isinstance(raw, dict):
        raise TaskPlanError("task_plan root must be an object")
    # returngift / single_purpose 阶段可选：旧版文件缺省时按默认值补齐
    # （兼容用户已有 plan 文件，不写回）
    expected = {"schedule", "morning", "afternoon"}
    for optional in ("returngift", "single_purpose"):
        if optional in raw:
            expected = expected | {optional}
    if set(raw) != expected:
        raise TaskPlanError(
            "task_plan must contain only schedule, morning, afternoon, "
            "and optionally returngift and single_purpose")
    schedule = raw["schedule"]
    required_schedule = {"morning_time", "afternoon_time", "random_delay_minutes"}
    if not isinstance(schedule, dict) or not required_schedule.issubset(schedule):
        raise TaskPlanError("schedule must contain morning_time, afternoon_time, and random_delay_minutes")
    unexpected = set(schedule) - required_schedule - {"weekaward_weekdays", "mysteryshop_weekdays"}
    if unexpected:
        raise TaskPlanError(f"schedule contains unknown keys: {sorted(unexpected)}")
    morning_time = _parse_time(schedule["morning_time"], "schedule.morning_time")
    afternoon_time = _parse_time(schedule["afternoon_time"], "schedule.afternoon_time")
    delay_minutes = schedule["random_delay_minutes"]
    if type(delay_minutes) is not int or delay_minutes < 0:
        raise TaskPlanError("schedule.random_delay_minutes must be a non-negative integer")
    _validate_schedule(morning_time, afternoon_time, delay_minutes)
    # 缺省 returngift / single_purpose 时用默认值（不写回文件：已有文件永远只读）
    returngift_raw = raw.get("returngift", DEFAULT_TASK_PLAN["returngift"])
    single_purpose_raw = raw.get("single_purpose", DEFAULT_TASK_PLAN["single_purpose"])
    # 星期列表缺省时用默认值（旧文件兼容，不写回）
    weekaward_weekdays = _parse_weekdays(
        schedule.get("weekaward_weekdays", DEFAULT_TASK_PLAN["schedule"]["weekaward_weekdays"]),
        "schedule.weekaward_weekdays")
    mysteryshop_weekdays = _parse_weekdays(
        schedule.get("mysteryshop_weekdays", DEFAULT_TASK_PLAN["schedule"]["mysteryshop_weekdays"]),
        "schedule.mysteryshop_weekdays")
    return TaskPlan(
        morning_time=morning_time,
        afternoon_time=afternoon_time,
        random_delay_minutes=delay_minutes,
        morning=_parse_phase(_drop_removed_plan_keys(raw["morning"]), "morning"),
        afternoon=_parse_phase(_drop_removed_plan_keys(raw["afternoon"]), "afternoon"),
        returngift=_parse_phase(returngift_raw, "returngift", RETURNGIFT_KEYS),
        single_purpose=_parse_phase(single_purpose_raw, "single_purpose", SINGLE_PURPOSE_KEYS),
        weekaward_weekdays=weekaward_weekdays,
        mysteryshop_weekdays=mysteryshop_weekdays,
    )


def _write_default(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            newline="\n",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as file:
            temp_path = Path(file.name)
            json.dump(DEFAULT_TASK_PLAN, file, ensure_ascii=False, indent=2)
            file.write("\n")
            file.flush()
            os.fsync(file.fileno())

        # os.link() publishes the fully written temp file under the final name only
        # when it does not already exist. This is atomic on the same filesystem and
        # never replaces a user-created task_plan.json.
        os.link(temp_path, path)
    except FileExistsError:
        # 另一个 MultiDaily 进程已先发布完整文件；随后只读取它的内容。
        return
    finally:
        if temp_path is not None:
            try:
                temp_path.unlink()
            except FileNotFoundError:
                pass
            except OSError:
                # The published task_plan.json remains valid; a later startup can
                # remove an antivirus-locked temporary file if necessary.
                pass


def load_task_plan(path: Path | None = None) -> TaskPlan:
    """首次缺失时生成默认文件；已有文件永远只读且严格校验。"""
    path = path or TASK_PLAN_PATH
    if not path.exists():
        _write_default(path)
    if not path.is_file():
        raise TaskPlanError(f"task_plan path is not a file: {path}")
    try:
        with path.open("r", encoding="utf-8") as file:
            raw = json.load(file)
    except (OSError, json.JSONDecodeError) as exc:
        raise TaskPlanError(f"Cannot read task_plan {path}: {exc}") from exc
    return parse_task_plan(raw)
