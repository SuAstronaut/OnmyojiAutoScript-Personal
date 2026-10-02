"""维度 F（抬起收尾）与维度 G（点击间空闲）。

F 拆成两个函数而不是一个带 touch 布尔参数的函数：两者的**事件位置相反**，
用 touch=True/False 表达会让 backend 把漂移发到错误的一侧。
- plan_pointer_tail：UP → sleep → MOVE（桌面 Win32 hover 语义）
- plan_touch_liftoff：sleep → MOVE → UP（真人手指抬起前必有微小位移）

G 只属指针语义：手指离开屏幕后没有可维持的状态，触摸协议不适用。
"""
from __future__ import annotations

import math

import numpy as np

from module.device.humanize.persona import Persona
from module.device.humanize.plan import MovePlan, Point, TailPlan
from module.device.humanize.timing import (
    profiled_move_delays,
    segment_distances,
)

# ---------------------------------------------------------------- 维度 F 常量

MICRO_DRIFT_COUNT = (1, 3)
MICRO_DRIFT_SIGMA_PX = 1.5
MICRO_DRIFT_GAP_S = (0.008, 0.025)

LIFTOFF_DRIFT_PX = (1, 3)
LIFTOFF_GAP_S = (0.010, 0.030)
# 真机验证通过前恒为 1（Spec §5 F / §4.10）。改这个常量前必须先跑通目标机的
# "多出的 before-UP 微位移会不会把短按判成拖拽"验证
LIFTOFF_POINTS = 1

# 画布边缘"放手"路径的分段数（plan_idle 的 park 分支）。原名 SLIDE_AWAY_POINTS：
# slide_away 随 heavy 档于 2026-09-13 删除后，它唯一的消费者是 park，改名以免误导
PARK_PATH_POINTS = (3, 5)

TOUCH_LIFTOFF_OPTIONS = ('liftoff_drift', 'none')

# ---------------------------------------------------------------- 维度 G 常量

IDLE_DRIFT_THRESHOLD_S = 2.0
IDLE_PARK_THRESHOLD_S = 30.0
IDLE_DRIFT_PX = (10.0, 60.0)
IDLE_DRIFT_POINTS = (2, 4)
IDLE_GAP_S = (0.015, 0.045)
IDLE_OPTIONS = ('idle_drift', 'park')

# ---------------------------------------------------------------- 维度 J 常量

# 长按 hold 期间的微颤随机游走（2026-08-26 调研对标）。平台长按识别器的移动
# 容差：iOS allowableMovement 10pt / Android touch slop 8dp / Web ~10px——
# 容差的存在证明真人按住期间手指持续微动；OAS 旧长按 hold 期间零事件，
# 事件流死寂整秒是明显机器指纹。微颤幅度必须远低于容差，否则长按被取消。
HOLD_JITTER_PX = (1, 3)          # 单步位移幅度（远离 8~10px 平台容差）
HOLD_JITTER_WALK_MAX = 6         # 随机游走距目标点的硬上限（px，稳态约 2~4）
HOLD_OPTIONS = ('tremor', 'none')


def _clip_point(p, canvas_size) -> Point:
    """把漂移点收进画布。

    这里裁剪是安全的：漂移点是本策略**凭空生成**的，不是调用方的业务目标，
    与 §4.11 禁止裁剪的"移动端点"不是一回事。
    """
    return (int(min(max(round(p[0]), 0), canvas_size[0] - 1)),
            int(min(max(round(p[1]), 0), canvas_size[1] - 1)))


def plan_pointer_tail(
    rng: np.random.Generator,
    target: Point,
    persona: Persona,
    *,
    canvas_size: tuple[int, int] = (1280, 720),
) -> TailPlan:
    """维度 F（指针语义）：抬起**之后**的漂移。

    **永不返回 None**：桌面路径今天就有一条 after-UP 同坐标移动
    （windows_impl.py:303/:413），去掉它会丢失 hover 刷新。不提供"完全不补"
    的方案——风险不值。

    2026-09-13：slide_away 曾作为第二个候选（仅 heavy），随档位收敛删除；
    本函数从此只有一个行为，故不再有 option 参数与权重抽样。
    """
    count = int(rng.integers(MICRO_DRIFT_COUNT[0], MICRO_DRIFT_COUNT[1] + 1))
    points = [
        _clip_point((target[0] + rng.normal(0.0, MICRO_DRIFT_SIGMA_PX),
                     target[1] + rng.normal(0.0, MICRO_DRIFT_SIGMA_PX)), canvas_size)
        for _ in range(count)
    ]
    delays = [float(rng.uniform(*MICRO_DRIFT_GAP_S)) for _ in range(count)]

    return TailPlan(points=tuple(points), delays=tuple(delays))


def plan_touch_liftoff(
    rng: np.random.Generator,
    target: Point,
    persona: Persona,
    *,
    option: str,
    canvas_size: tuple[int, int] = (1280, 720),
) -> TailPlan | None:
    """维度 F（触摸语义）：抬起**之前**的微位移。

    返回 None 表示"直接 up"——option='none' 保留 0.2 权重，是约 20% 的触摸动作
    不漂移的人类方差，也是"今天方案默认不进 enabled 权重"的唯一显式例外。

    注意这与 off 旁路的 None 不同层：off 时 facade 根本不会调到这里。
    """
    if option not in TOUCH_LIFTOFF_OPTIONS:
        raise ValueError(
            f'plan_touch_liftoff: 未知 option {option!r}，可选 {TOUCH_LIFTOFF_OPTIONS}')
    if option == 'none':
        return None

    lo, hi = LIFTOFF_DRIFT_PX
    points: list[Point] = []
    for _ in range(LIFTOFF_POINTS):
        # 至少偏 1px：偏 0 就等于没漂移，等于 none
        dx = int(rng.integers(lo, hi + 1)) * int(rng.choice([-1, 1]))
        dy = int(rng.integers(lo, hi + 1)) * int(rng.choice([-1, 1]))
        points.append(_clip_point((target[0] + dx, target[1] + dy), canvas_size))
    delays = [float(rng.uniform(*LIFTOFF_GAP_S)) for _ in range(LIFTOFF_POINTS)]
    return TailPlan(points=tuple(points), delays=tuple(delays))


def plan_idle(
    rng: np.random.Generator,
    since_last_s: float,
    cursor: Point | None,
    persona: Persona,
    *,
    option: str,
    canvas_size: tuple[int, int] = (1280, 720),
) -> MovePlan | None:
    """维度 G：点击间空闲。仅指针语义有效。

    返回 None 表示"本次不做空闲动作"，原因有二：未达阈值，或光标位置未知。
    光标未知时必须返回 None——凭空移动会把光标从未知处拽到某个坐标，
    那是引入了一个新的可观测行为而不是拟人化。
    """
    if option not in IDLE_OPTIONS:
        raise ValueError(f'plan_idle: 未知 option {option!r}，可选 {IDLE_OPTIONS}')
    if isinstance(since_last_s, bool) or not isinstance(since_last_s, (int, float)):
        raise ValueError(f'plan_idle: since_last_s 必须是数值，收到 {since_last_s!r}')
    if since_last_s < 0 or not math.isfinite(since_last_s):
        raise ValueError(f'plan_idle: since_last_s 必须是有限非负数，收到 {since_last_s}')
    if cursor is None:
        return None
    # cursor 是已有指针状态，越界时不能像策略新增点一样裁剪，否则会静默改写
    # 下一次操作的起点；由门面负责告警，这里再做纯函数层防线。
    if (
        not isinstance(cursor, (tuple, list))
        or len(cursor) != 2
        or not (0 <= cursor[0] <= canvas_size[0] - 1)
        or not (0 <= cursor[1] <= canvas_size[1] - 1)
    ):
        return None

    if option == 'park':
        if since_last_s <= IDLE_PARK_THRESHOLD_S:
            return None
        # 移到画布边缘随机点，模拟"放手"；下次点击自然从边缘移回
        edge = int(rng.integers(0, 4))
        if edge == 0:
            end = (int(rng.integers(0, canvas_size[0])), 0)
        elif edge == 1:
            end = (int(rng.integers(0, canvas_size[0])), canvas_size[1] - 1)
        elif edge == 2:
            end = (0, int(rng.integers(0, canvas_size[1])))
        else:
            end = (canvas_size[0] - 1, int(rng.integers(0, canvas_size[1])))
        n = int(rng.integers(PARK_PATH_POINTS[0], PARK_PATH_POINTS[1] + 1))
    else:
        if since_last_s <= IDLE_DRIFT_THRESHOLD_S:
            return None
        dist = float(rng.uniform(*IDLE_DRIFT_PX))
        ang = float(rng.uniform(0.0, 2.0 * math.pi))
        end = _clip_point((cursor[0] + math.cos(ang) * dist,
                           cursor[1] + math.sin(ang) * dist), canvas_size)
        n = int(rng.integers(IDLE_DRIFT_POINTS[0], IDLE_DRIFT_POINTS[1] + 1))

    # 空闲游移只允许短距直线插值：Spec §4.10 规定 idle 不使用通用形状允许集，
    # 否则过冲/两段式可能把光标带进危险区域
    points = [
        _clip_point((cursor[0] + (end[0] - cursor[0]) * (i / n),
                     cursor[1] + (end[1] - cursor[1]) * (i / n)), canvas_size)
        for i in range(1, n + 1)
    ]
    points[-1] = _clip_point(end, canvas_size)
    total = float(np.clip(
        math.hypot(end[0] - cursor[0], end[1] - cursor[1]) / 300.0, 0.06, 0.40))
    delays = profiled_move_delays(
        rng, segment_distances(cursor, points), total, 'min_jerk')
    return MovePlan(points=tuple(points), delays=tuple(delays))


def plan_hold_jitter(
    rng: np.random.Generator,
    target: Point,
    count: int,
    *,
    canvas_size: tuple[int, int] = (1280, 720),
) -> list[Point]:
    """维度 J：长按 hold 期间的微颤随机游走，返回 count 个点（不含目标点本身）。

    每步从 HOLD_JITTER_PX 区间取位移幅度、随机方向，构成围绕 target 的
    随机游走；距 target 超过 HOLD_JITTER_WALK_MAX 时拉回（防止游走漂出
    平台长按容差）。UP 前最后一个点不强制回 target——真人手指抬起时
    也不精确回到按下点（那是维度 F liftoff 的事，这里不越权）。
    小幅度经整数化后可能出现与前点相同的坐标：事件仍会发出（时间戳
    前进），真颤也有平台期，死寂指纹的消除不依赖每步都变位置。
    """
    if isinstance(count, bool) or not isinstance(count, int):
        raise ValueError(f'plan_hold_jitter: count 必须是 int，收到 {count!r}')
    if count < 0:
        raise ValueError(f'plan_hold_jitter: count 不能为负，收到 {count}')
    points: list[Point] = []
    for _ in range(count):
        amp = int(rng.integers(HOLD_JITTER_PX[0], HOLD_JITTER_PX[1] + 1))
        ang = float(rng.uniform(0.0, 2.0 * math.pi))
        # 从上一个游走位置（首步从 target）出发走一步
        base = points[-1] if points else target
        cand = (base[0] + math.cos(ang) * amp, base[1] + math.sin(ang) * amp)
        # 游走出界时把候选点按方向折返回上限内（保持"必有位移"不变量）
        dx, dy = cand[0] - target[0], cand[1] - target[1]
        d = math.hypot(dx, dy)
        if d > HOLD_JITTER_WALK_MAX:
            scale = HOLD_JITTER_WALK_MAX / d
            cand = (target[0] + dx * scale, target[1] + dy * scale)
        points.append(_clip_point(cand, canvas_size))
    return points
