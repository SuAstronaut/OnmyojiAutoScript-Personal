"""维度 A（落点分布）与维度 C（轨迹形状）。

坐标一律在 1280×720 授权空间。本模块**不做**设备尺寸换算——minitouch 的换算
仍只在 builder/backend 层执行，两处都换会得到双重缩放。

几何层不读 ContextVar、不读人格权重选方案：option 由 facade 传入（Plan 契约 12）。
"""
from __future__ import annotations

import math

import numpy as np

from module.device.humanize.persona import Persona
from module.device.humanize.plan import Point
from module.logger import logger

# ---------------------------------------------------------------- 维度 A 常量

# center_gauss 的 σ = (w/5, h/5)（Spec §5 A）
CENTER_SIGMA_RATIO = 0.2
# edge_avoid 四边各内缩 15%
EDGE_AVOID_INSET = 0.15
# prev_biased 朝 prev 方向的偏移上限，以及距离衰减常数（Spec §5 A）
PREV_BIAS_MAX = 0.15
PREV_BIAS_DIST_K = 20.0

POINT_OPTIONS = ('center_gauss', 'offset_gauss', 'edge_avoid', 'prev_biased')


def _validate_roi(roi) -> tuple[int, int, int, int]:
    """ROI 必须是 (x, y, w, h) 且宽高为正。

    空 ROI 明确报错而不是返回原点：一个 w=0 的 ROI 说明上游 Rule 出了问题，
    静默返回坐标会让那个 bug 在点击到错误位置时才暴露。
    """
    if not isinstance(roi, (tuple, list)) or len(roi) != 4:
        raise ValueError(f'roi 必须是 (x, y, w, h)，收到 {roi!r}')
    x, y, w, h = (int(v) for v in roi)
    if w <= 0 or h <= 0:
        raise ValueError(f'roi 宽高必须为正，收到 w={w}, h={h}')
    return x, y, w, h


def _clip_into_roi(px: float, py: float, x: int, y: int, w: int, h: int) -> Point:
    """把采样值收进 ROI 闭区间 [x, x+w-1] × [y, y+h-1] 并取整。

    这里裁剪是安全的：A 的语义就是"在这个 ROI 里挑一点"，ROI 本身是业务给的
    边界。与 §4.11 禁止裁剪的"移动端点"不是一回事。
    """
    return (
        int(np.clip(round(px), x, x + w - 1)),
        int(np.clip(round(py), y, y + h - 1)),
    )


def sample_point(
    rng: np.random.Generator,
    roi,
    persona: Persona,
    *,
    option: str,
    prev: Point | None = None,
) -> Point:
    """维度 A：在 ROI 内采一个落点。

    今天的行为是 `np.random.randint(x, x+w)`——在矩形内均匀分布。均匀分布是
    机器特征：真人点击集中在控件中心附近，且有固定的握姿偏心。
    """
    if option not in POINT_OPTIONS:
        raise ValueError(f'sample_point: 未知 option {option!r}，可选 {POINT_OPTIONS}')
    x, y, w, h = _validate_roi(roi)
    cx, cy = x + w / 2.0, y + h / 2.0
    sx, sy = w * CENTER_SIGMA_RATIO, h * CENTER_SIGMA_RATIO

    if option == 'edge_avoid':
        ix, iy = w * EDGE_AVOID_INSET, h * EDGE_AVOID_INSET
        return _clip_into_roi(
            rng.uniform(x + ix, x + w - ix),
            rng.uniform(y + iy, y + h - iy),
            x, y, w, h)

    if option == 'offset_gauss':
        # 握姿偏心：同一个人固定偏向同一侧，所以 aim_bias 是人格字段
        mu_x = x + w * (0.5 + persona.aim_bias[0])
        mu_y = y + h * (0.5 + persona.aim_bias[1])
        return _clip_into_roi(rng.normal(mu_x, sx), rng.normal(mu_y, sy), x, y, w, h)

    mu_x, mu_y = cx, cy
    if option == 'prev_biased':
        if prev is None:
            # 无 prev 时退化为 center_gauss，而不是报错：Rule 层的首次点击
            # 本来就没有上一个落点
            pass
        else:
            dist = math.hypot(cx - prev[0], cy - prev[1])
            if dist > 0:
                # 距离越近偏移越大（手还没完全移开），上限 0.15 × roi 半宽
                ratio = min(PREV_BIAS_MAX, PREV_BIAS_DIST_K / dist)
                mu_x = cx + (prev[0] - cx) / dist * ratio * (w / 2.0)
                mu_y = cy + (prev[1] - cy) / dist * ratio * (h / 2.0)

    return _clip_into_roi(rng.normal(mu_x, sx), rng.normal(mu_y, sy), x, y, w, h)


# ---------------------------------------------------------------- 维度 C 常量

# bezier 控制点横向偏移 clip(dist*0.05, 8, 40) px（Spec §5 C）
BEZIER_OFFSET = (0.05, 8.0, 40.0)
# arc 控制点垂直偏移 clip(dist*0.12, 15, 60) px
ARC_OFFSET = (0.12, 15.0, 60.0)
# overshoot 外推 clip(dist*0.06, 4, 20) px，硬上限 20
OVERSHOOT_OFFSET = (0.06, 4.0, 20.0)
OVERSHOOT_MAX_PX = 20
OVERSHOOT_PERP_RATIO = 0.3          # 垂直抖动 ±0.3d
OVERSHOOT_CORRECT_POINTS = (2, 4)   # 修正段 2~4 点
# 纠正性子动作的距离门控（2026-08-26 调研吸收）：ballistic+corrective 两段结构
# 是人类瞄准的特征签名，但只出现在长距离移动——小幅移动一次弹道即精确到位，
# 没有纠正阶段。指针移动距离低于该值时 overshoot 不进入候选
CORRECTIVE_MIN_DIST_PX = 200
# two_phase 的弹道段落点距目标 20~60px，停顿 gauss(60, 20) ms
TWO_PHASE_GAP_PX = (20.0, 60.0)
TWO_PHASE_PAUSE_MS = (60.0, 20.0)
JITTER_LINE_PX = (1, 3)

SHAPE_OPTIONS = ('bezier', 's_curve', 'overshoot', 'two_phase', 'arc', 'jitter_line')


def _in_canvas(p, canvas_size) -> bool:
    return 0 <= p[0] <= canvas_size[0] - 1 and 0 <= p[1] <= canvas_size[1] - 1


def _quad(p0, p1, p2, t: float) -> tuple[float, float]:
    """二次贝塞尔。用 t ∈ [0,1] 参数化，与坐标轴无关——所以垂直路径不除零。"""
    u = 1.0 - t
    return (u * u * p0[0] + 2 * u * t * p1[0] + t * t * p2[0],
            u * u * p0[1] + 2 * u * t * p1[1] + t * t * p2[1])


def _cubic(p0, p1, p2, p3, t: float) -> tuple[float, float]:
    """三次贝塞尔，同样按 t 参数化。"""
    u = 1.0 - t
    return (u ** 3 * p0[0] + 3 * u * u * t * p1[0] + 3 * u * t * t * p2[0] + t ** 3 * p3[0],
            u ** 3 * p0[1] + 3 * u * u * t * p1[1] + 3 * u * t * t * p2[1] + t ** 3 * p3[1])


def _perp(start, end) -> tuple[float, float]:
    """start→end 的单位法向量。零长度时退化为 (0, 0)，调用方按直线处理。"""
    dx, dy = float(end[0] - start[0]), float(end[1] - start[1])
    dist = math.hypot(dx, dy)
    if dist == 0:
        return 0.0, 0.0
    return -dy / dist, dx / dist


def _clip_control(p, canvas_size) -> tuple[float, float]:
    """中间控制点可以裁剪到画布内——它不是端点，裁剪不改变业务目标。"""
    return (min(max(p[0], 0.0), canvas_size[0] - 1.0),
            min(max(p[1], 0.0), canvas_size[1] - 1.0))


def _sample_curve(fn, count: int, end: Point, t_map=None) -> list[Point]:
    """在 t ∈ (0, 1] 上取 count 个点，末项强制精确等于 end。

    t_map 把等间隔的 u=i/count 映射到曲线参数 t：None 时恒等（参数均匀采样）；
    传入等时间映射（timing.time_param_map 的产物）后按时间等间隔采样——
    点距正比局部速度，慢速区自然密集，速度编码进点密度而不是 delay。
    强制末项是契约的一部分：贝塞尔在 t=1 时数学上就是终点，但浮点取整可能
    差 1px，而"终点永不修改"不接受 1px 误差。
    """
    pts: list[Point] = []
    for i in range(1, count + 1):
        u = i / count
        t = t_map(u) if t_map is not None else u
        x, y = fn(t)
        pts.append((int(round(x)), int(round(y))))
    pts[-1] = end
    return pts


def shape_points(
    rng: np.random.Generator,
    start: Point,
    end: Point,
    *,
    option: str,
    max_points: int,
    persona: Persona,
    canvas_size: tuple[int, int] = (1280, 720),
    t_map=None,
) -> tuple[list[Point], dict[int, float]] | None:
    """维度 C：生成轨迹形状。返回 (points, extra) 或 None。

    points 不含起点，末项恒为 end，长度不超过 max_points。
    extra 是点索引→额外停顿秒数，只有 two_phase 非空。
    t_map 是可选的 u→t 等时间映射（timing.time_param_map），只作用于单曲线
    分支（bezier/s_curve/arc）：overshoot/two_phase 的分段结构自身已编码
    相位速度，且调用方（swipe 允许集）也不启用它们。

    返回 None 的唯一原因是**端点越界**——此时 backend 走原始分支。不 clip 端点：
    同一次调用在不同档位得到不同终点是语义变化，不是拟人化（复审 3.9）。

    只生成形状，不做 gesture_kind / safe_region 过滤（那是 facade 的职责），
    也不做速度分配（那是 timing 层的 profiled_move_delays）。
    """
    if option not in SHAPE_OPTIONS:
        raise ValueError(f'shape_points: 未知 option {option!r}，可选 {SHAPE_OPTIONS}')
    if max_points < 1:
        raise ValueError(f'shape_points: max_points 必须 >= 1，收到 {max_points}')
    if not _in_canvas(start, canvas_size) or not _in_canvas(end, canvas_size):
        # 只 warning 不 raise：越界端点是上游 ROI 问题，拟人化不该因此中断动作
        logger.warning(
            f'拟人化几何跳过：端点越界 start={start} end={end} canvas={canvas_size}')
        return None

    dist = math.hypot(float(end[0] - start[0]), float(end[1] - start[1]))
    px, py = _perp(start, end)
    side = float(persona.arc_side)   # 同一个人手腕转动方向固定
    extra: dict[int, float] = {}

    if option == 'jitter_line':
        count = max(1, max_points)
        lo, hi = JITTER_LINE_PX
        pts: list[Point] = []
        for i in range(1, count + 1):
            t = i / count
            bx = start[0] + (end[0] - start[0]) * t
            by = start[1] + (end[1] - start[1]) * t
            amp = float(rng.integers(lo, hi + 1)) * float(rng.choice([-1.0, 1.0]))
            point = _clip_control((bx + px * amp, by + py * amp), canvas_size)
            pts.append((int(round(point[0])), int(round(point[1]))))
        pts[-1] = end
        return pts, extra

    if option == 'arc':
        ratio, lo, hi = ARC_OFFSET
        off = float(np.clip(dist * ratio, lo, hi)) * side
        mid = ((start[0] + end[0]) / 2.0 + px * off,
               (start[1] + end[1]) / 2.0 + py * off)
        mid = _clip_control(mid, canvas_size)
        return _sample_curve(
            lambda t: _quad(start, mid, end, t), max(1, max_points), end, t_map), extra

    if option == 'bezier':
        ratio, lo, hi = BEZIER_OFFSET
        off = float(np.clip(dist * ratio, lo, hi)) * side
        # 两个控制点分别落在 1/3、2/3 处，同侧偏移，形成平滑单弧
        c1 = _clip_control((start[0] + (end[0] - start[0]) / 3.0 + px * off,
                            start[1] + (end[1] - start[1]) / 3.0 + py * off), canvas_size)
        c2 = _clip_control((start[0] + (end[0] - start[0]) * 2 / 3.0 + px * off * 0.6,
                            start[1] + (end[1] - start[1]) * 2 / 3.0 + py * off * 0.6),
                           canvas_size)
        return _sample_curve(
            lambda t: _cubic(start, c1, c2, end, t), max(1, max_points), end, t_map), extra

    if option == 's_curve':
        # S 形拐点：两个控制点分居法线两侧，轨迹先向一侧弯再向另一侧弯。
        # 吸收自 HumanCursor 的多节点随机贝塞尔——真人远距离移动常带方向修正，
        # 单弧 bezier 永远只有一个弯曲方向，方向翻转本身是低频人类特征。
        # 第二控制点幅度带随机缩放，避免两个弧段机械对称。
        ratio, lo, hi = BEZIER_OFFSET
        off1 = float(np.clip(dist * ratio, lo, hi)) * side
        off2 = -float(np.clip(dist * ratio, lo, hi)) * side * float(rng.uniform(0.6, 1.0))
        c1 = _clip_control((start[0] + (end[0] - start[0]) / 3.0 + px * off1,
                            start[1] + (end[1] - start[1]) / 3.0 + py * off1), canvas_size)
        c2 = _clip_control((start[0] + (end[0] - start[0]) * 2 / 3.0 + px * off2,
                            start[1] + (end[1] - start[1]) * 2 / 3.0 + py * off2), canvas_size)
        return _sample_curve(
            lambda t: _cubic(start, c1, c2, end, t), max(1, max_points), end, t_map), extra

    if option == 'overshoot':
        ratio, lo, hi = OVERSHOOT_OFFSET
        d = float(np.clip(dist * ratio, lo, min(hi, OVERSHOOT_MAX_PX)))
        dx = (end[0] - start[0]) / dist if dist else 0.0
        dy = (end[1] - start[1]) / dist if dist else 0.0
        jitter = float(rng.uniform(-OVERSHOOT_PERP_RATIO, OVERSHOOT_PERP_RATIO)) * d
        over = _clip_control((end[0] + dx * d + px * jitter,
                             end[1] + dy * d + py * jitter), canvas_size)
        over_pt = (int(round(over[0])), int(round(over[1])))
        # 修正段点数在 2~4，主段拿剩下的；max_points 很小时优先保证终点可达
        n_correct = int(rng.integers(*OVERSHOOT_CORRECT_POINTS))
        n_correct = max(1, min(n_correct, max_points - 1)) if max_points > 1 else 0
        n_main = max(1, max_points - n_correct)
        c1 = _clip_control((start[0] + (over[0] - start[0]) / 2.0 + px * d,
                            start[1] + (over[1] - start[1]) / 2.0 + py * d), canvas_size)
        main = _sample_curve(lambda t: _quad(start, c1, over, t), n_main, over_pt)
        if n_correct == 0:
            return [end], extra
        back = _sample_curve(lambda t: _quad(over, over, end, t), n_correct, end)
        return (main + back)[-max_points:], extra

    # two_phase：弹道段先到目标附近，停一下，再精调段到目标。
    # extra 记录停顿，由 facade 合并进对应 delay——不在几何层加 delay
    gap = float(rng.uniform(*TWO_PHASE_GAP_PX))
    ang = float(rng.uniform(0.0, 2.0 * math.pi))
    near = _clip_control((end[0] + math.cos(ang) * gap,
                          end[1] + math.sin(ang) * gap), canvas_size)
    near_pt = (int(round(near[0])), int(round(near[1])))
    n_fine = max(1, min(2, max_points - 1)) if max_points > 1 else 0
    n_ball = max(1, max_points - n_fine)
    ratio, lo, hi = BEZIER_OFFSET
    off = float(np.clip(dist * ratio, lo, hi)) * side
    c1 = _clip_control((start[0] + (near[0] - start[0]) / 2.0 + px * off,
                        start[1] + (near[1] - start[1]) / 2.0 + py * off), canvas_size)
    ballistic = _sample_curve(lambda t: _quad(start, c1, near, t), n_ball, near_pt)
    if n_fine == 0:
        return [end], extra
    fine = _sample_curve(lambda t: _quad(near, near, end, t), n_fine, end)
    points = (ballistic + fine)[-max_points:]
    # 停顿挂在弹道段最后一个点上（真人在目标附近的那次减速停顿）
    pause_idx = len(points) - n_fine - 1
    if pause_idx >= 0:
        pause_ms = max(0.0, float(rng.normal(*TWO_PHASE_PAUSE_MS)))
        extra[pause_idx] = pause_ms / 1000.0
    return points, extra
