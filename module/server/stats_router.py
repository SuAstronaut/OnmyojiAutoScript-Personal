# This Python file uses the following encoding: utf-8
from __future__ import annotations

from datetime import date, datetime

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from module.server.log_stats import log_stats_service

stats_app = APIRouter(
    prefix="/stats",
    tags=["stats"],
)


class BattleStatsResponse(BaseModel):
    count: int
    avg_duration_seconds: float


class TaskRunStatsResponse(BaseModel):
    start_time: str
    end_time: str
    duration_seconds: float
    battle: BattleStatsResponse | None = None


class TaskStatsResponse(BaseModel):
    run_count: int
    total_duration_seconds: float
    battle: BattleStatsResponse | None = None
    runs: list[TaskRunStatsResponse] = Field(default_factory=list)


class MultiTaskResponse(BaseModel):
    task: str
    ok: bool
    start_time: str | None = None  # 子任务起点时间（需求3 hover 展示）
    duration_seconds: float | None = None
    battle_count: int = 0
    battle_total_duration_seconds: float = 0.0
    battle_avg_duration_seconds: float = 0.0


class MultiErrorResponse(BaseModel):
    task: str | None = None
    etype: str
    emsg: str
    time: str | None = None  # 事件时刻，供前端按会话筛选（修复2）


class MultiCoopResponse(BaseModel):
    ctype: str
    real: bool = False
    time: str | None = None  # 事件时刻，供前端按会话筛选（需求2）


class MultiMshopResponse(BaseModel):
    goods: str
    price: int | float | None = None
    time: str | None = None  # 事件时刻，供前端按会话筛选（需求2）


class MultiSegmentResponse(BaseModel):
    """账号单次运行段：按账号切换/任务结束标志切分的连续运行区间（需求6）。"""

    start_time: str | None = None
    end_time: str | None = None
    duration_seconds: float = 0.0
    session: int = 0  # 所属 MultiAcc 会话索引


class MultiAccountResponse(BaseModel):
    account: str
    character: str
    svr: str
    switch_ok: bool | None = None
    duration_seconds: float
    error_count: int
    battle_count: int
    battle_total_duration_seconds: float = 0.0
    battle_avg_duration_seconds: float = 0.0
    coop_total: int
    tasks: list[MultiTaskResponse] = Field(default_factory=list)
    errors: list[MultiErrorResponse] = Field(default_factory=list)
    coops: list[MultiCoopResponse] = Field(default_factory=list)
    mshops: list[MultiMshopResponse] = Field(default_factory=list)
    segments: list[MultiSegmentResponse] = Field(default_factory=list)


class MultiSessionResponse(BaseModel):
    """每次 MultiAcc 运行会话的元数据（需求2 时间筛选）。"""

    index: int
    start_time: str | None = None
    end_time: str | None = None
    duration_seconds: float = 0.0
    account_count: int = 0


class MultiStatsResponse(BaseModel):
    accounts: list[MultiAccountResponse] = Field(default_factory=list)
    sessions: list[MultiSessionResponse] = Field(default_factory=list)  # 每次运行会话（需求2）
    total_duration_seconds: float = 0.0  # 全天总耗时 = 各账号运行段耗时之和（需求6）


class StatsResponse(BaseModel):
    script_name: str
    total_runtime_seconds: float
    total_task_run_count: int
    total_battle_count: int
    tasks: dict[str, TaskStatsResponse] = Field(default_factory=dict)
    multi: MultiStatsResponse | None = None


class StatsAvailableDatesResponse(BaseModel):
    script_name: str
    dates: list[str] = Field(default_factory=list)


class StatsUpdateResponse(BaseModel):
    script_name: str
    total_runtime_seconds: float
    total_task_run_count: int
    total_battle_count: int
    changed_tasks: dict[str, TaskStatsResponse] = Field(default_factory=dict)
    removed_tasks: list[str] = Field(default_factory=list)
    multi: MultiStatsResponse | None = None


def _parse_target_date(date_text: str) -> date:
    try:
        return datetime.strptime(date_text, "%Y-%m-%d").date()
    except ValueError as exc:
        raise HTTPException(status_code=422, detail="Invalid date format, expected YYYY-MM-DD") from exc


@stats_app.get("/{script_name}/dates", response_model=StatsAvailableDatesResponse)
async def stats_available_dates(script_name: str):
    return log_stats_service.list_available_dates(script_name)


@stats_app.get("/{script_name}", response_model=StatsResponse)
async def stats_snapshot(
    script_name: str,
    date_text: str = Query(..., alias="date", description="YYYY-MM-DD"),
):
    target_day = _parse_target_date(date_text)
    return log_stats_service.build_stats(script_name, target_day)


@stats_app.get("/{script_name}/stream")
async def stats_stream(
    script_name: str,
    date_text: str = Query(..., alias="date", description="YYYY-MM-DD"),
):
    target_day = _parse_target_date(date_text)
    if target_day != date.today():
        raise HTTPException(status_code=400, detail="Only today's date supports SSE stream")

    response = StreamingResponse(
        log_stats_service.stream_events(script_name, target_day),
        media_type="text/event-stream",
    )
    response.headers["Cache-Control"] = "no-cache"
    response.headers["Connection"] = "keep-alive"
    response.headers["X-Accel-Buffering"] = "no"
    return response
