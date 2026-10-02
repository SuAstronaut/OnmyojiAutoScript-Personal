"""拟人化输入策略层。

档位枚举定义在本包的 __init__ 里，是它对外的公开类型（配置层
`tasks/Script/config_device.py` 直接引用它做字段校验）。
2026-09-13 档位收敛后，timing / geometry / gesture 三个子模块已不再从包级
import 任何名字，因此这里**不再有**循环 import 的约束——枚举留在原处只是
保持公开 API 的位置不变。

本文件追加 HumanizerContext 门面与 ContextVar 绑定 API（Plan Task 11）：
- 门面的方法名与模块函数同名是刻意的，但签名差别就是门面的职责——门面**不收
  option / rng / persona**（它自己持有），**可返回 None**（off 旁路契约）。
- `_choose()` 是唯一读取 persona.weights 并按允许集挑选方案的地方（Plan 契约 12），
  策略函数永远只接收已选定的 option。
- 模块级策略函数一律经由 `timing.xxx` / `geometry.xxx` / `gesture.xxx` 调用，
  避免门面方法被同名函数遮蔽（不能 `from module.device.humanize import press_seconds`）。
"""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from typing import Literal

import math
import time

import numpy as np

from module.device.humanize.plan import DwellPlan, MovePlan, Point, TailPlan
from module.logger import logger

# 两档：off 全旁路，medium 是唯一拟人档（2026-09-13 收敛：原 light/heavy 的
# 实现路径已删除，磁盘上的旧档位值由 config_validation._migrate_humanize_level
# 迁移到 medium）。
HumanizeLevel = Literal['off', 'medium']

LEVELS: tuple[str, ...] = ('off', 'medium')

# 门面对外类型别名（Spec §4.5）
GestureKind = Literal['pointer_move', 'swipe', 'drag', 'idle']
TimingMode = Literal['python_sleep', 'device_wait']
_TIMING_MODES = ('python_sleep', 'device_wait')

# 策略模块经 `from module.device.humanize import xxx` 引入：门面方法名与模块函数
# 同名，必须经由模块引用调用（如 timing.press_seconds），避免被同名方法遮蔽。
from module.device.humanize import geometry
from module.device.humanize import gesture
from module.device.humanize import persona
from module.device.humanize import plan
from module.device.humanize import timing
# 模块级别名：_overshoot_track_fits 的包含校验用原始函数对象（测试 monkeypatch
# geometry.shape_points 时不受影响），真实计划生成仍走 geometry.shape_points 属性
from module.device.humanize.geometry import shape_points as _geometry_shape_points

# plan_move 近恒定延迟的兜底基准（秒）。桌面指针移动的预算 40~120ms 由 backend
# 通过 budget_ms 传入；本常量只在预算缺失、且 profile 降点后仍不可信时的
# legacy_move_delays 退化路径里使用（12 点约 60ms，属可接受回退）。
# 原名 _LIGHT_MOVE_BASE_DELAY_S——light 档于 2026-09-13 删除后仅剩这条退化路径消费
_MOVE_FALLBACK_BASE_DELAY_S = 0.005

# plan_swipe 的逐点基准 delay（秒）。minitouch 显式传 base_delay_s=0.010；
# 此处是预算缺失时的默认值，也是预算的推导基准（base × PROFILE_MAX_POINTS）
_SWIPE_BASE_DELAY_S = 0.010

# plan_move 未传 budget_ms 时的默认预算（秒）。桌面请求 40~120ms，
# 取中值 60ms 作回退
_MOVE_BUDGET_DEFAULT_S = 0.060

# 端点越界 warning 每进程每类型只记一次，避免刷屏（Spec §4.11）。类型 = 门面方法名。
_OOB_WARNED_TYPES: set[str] = set()

# ContextVar：让无 device 引用的 Rule 层读取当前 context（Plan 契约 2）。
# 每个 Device 持有一个独立 HumanizerContext；正式运行时不变量是"同一执行 Context
# 同时最多一个活动 Device"——ContextVar 提供隔离机制，但不自动证明该不变量。
_current_humanizer: ContextVar['HumanizerContext | None'] = ContextVar(
    'current_humanizer', default=None)


def set_current_humanizer(context: 'HumanizerContext') -> None:
    """把 context 绑到当前执行上下文（Device.__init__ 的 _ensure_humanizer_context 调用）。"""
    _current_humanizer.set(context)


def get_current_humanizer() -> 'HumanizerContext | None':
    """读取当前执行上下文绑定的 context；无绑定返回 None（Rule 层走原始均匀采样）。"""
    return _current_humanizer.get()


@contextmanager
def bind_humanizer(context: 'HumanizerContext | None'):
    """测试 / 明确嵌套覆盖用：进入时绑定、退出时用 token 恢复。

    生产代码用 set_current_humanizer，不通过 reset 掩盖错误绑定（Plan 契约 2）。
    """
    token = _current_humanizer.set(context)
    try:
        yield
    finally:
        _current_humanizer.reset(token)


class HumanizerContext:
    """每个 Device 一份的拟人化门面。

    enabled 为 False（off 档）时所有策略入口返回 None 且不消费 RNG；开档时
    内部持有 persona + rng，按权重在允许集内挑选方案后委托给模块级策略函数。

    2026-09-13 收敛后档位只剩 off/medium 两值，与 enabled 一一对应，因此门面
    不再持有 level 字段——档位只在配置层与日志里存在，实现层只用布尔。
    """

    enabled: bool
    persona: 'persona.Persona | None'
    rng: 'np.random.Generator | None'
    canvas_size: tuple[int, int]

    def __init__(
        self,
        *,
        enabled: bool,
        persona: 'persona.Persona | None',
        rng: 'np.random.Generator | None',
        canvas_size: tuple[int, int] = (1280, 720),
    ) -> None:
        self.enabled = enabled
        self.persona = persona
        self.rng = rng
        self.canvas_size = canvas_size
        # 全操作共享间隔（2026-08-27 新增）状态：click/long_click/swipe/drag
        # 共用同一份 CD，跟随 Device 生命周期。
        # 预付制：操作结束时计算下一次操作的间隔要求（_pending_require），
        # 由下一次截图入口（pace_view）等满——等待全部发生在「看」之前，
        # 动作一旦决定立即执行（反应慢、动作快，人类模型）。
        # 2026-09-12 起间隔取值完全开环（单档区间随距离平移，不读历史节奏），
        # 原先为 lognormal 分支服务的意图间隔窗口（_gap_window）与自适应基准
        # （_gap_base）、以及配合它们的机制等待记账（_mech_wait）一起移除：
        # 那条反馈回路在主导路径上从不参与取值
        self._gap_last_ts: float | None = None
        self._pending_require: float = 0.0
        # 上一个落点（2026-09-06）：record_action 用它与本次 target 算距离，
        # 距离决定间隔区间在 [0.20,0.35]~[0.65,0.80] 之间的位置
        self._last_record_point: 'tuple[int, int] | None' = None
        # 同一资源重复点击的指数退避状态：判定键优先用点击控件名 + roi_front
        # （不同按钮即便相邻也不会误判；同名且 ROI 一致才是同一资源——同名但
        # ROI 变化是任务在复用同一 RuleClick 遍历列表，判为新资源），无名点击
        # 退回坐标半径兜底
        self._repeat_point: 'tuple[int, int] | None' = None
        self._repeat_name: str | None = None
        self._repeat_count: int = 0
        # 上次有名点击的 roi_front（已规范化为 tuple）：None 表示该路径拿不到
        # 稳定 ROI（直调 device.click / 匹配结果驱动区域），判重退化为仅按名
        self._repeat_roi: 'tuple[int, ...] | None' = None

    # ---------------------------------------------------------------- 构造

    @classmethod
    def from_config(
        cls,
        config,
        *,
        canvas_size: tuple[int, int] = (1280, 720),
    ) -> 'HumanizerContext':
        """按配置档位构造门面。

        off 时不加载/生成人格、不访问人格文件、不调用 os.urandom（零回归）——
        这既保证 off 路径零 I/O，也让默认档位的初始化不依赖 persona 存储。
        """
        level = getattr(config.script.device, 'humanize_level', 'off')
        if level not in LEVELS:
            raise ValueError(f'未知 humanize_level {level!r}，可选 {LEVELS}')
        if level == 'off':
            return cls(enabled=False, persona=None, rng=None,
                       canvas_size=canvas_size)
        config_name = getattr(config, 'config_name', None) or 'default'
        p = persona.PersonaStore(config_name).load_or_create()
        # 同一人格固定 seed 派生 RNG：同一个"人"的重启行为可复现
        rng = np.random.Generator(np.random.PCG64(p.seed))
        return cls(enabled=True, persona=p, rng=rng,
                   canvas_size=canvas_size)

    # ---------------------------------------------------------------- 内部

    def _endpoint_ok(self, p) -> bool:
        """端点必须在画布闭区间内；越界走整体回退（Spec §4.11），绝不修改端点。"""
        w, h = self.canvas_size
        return (
            isinstance(p, (tuple, list)) and len(p) == 2
            and 0 <= p[0] <= w - 1 and 0 <= p[1] <= h - 1
        )

    def _warn_endpoint_oob(self, method: str, start, end) -> None:
        """端点越界 warning，每进程每类型只记一次（Spec §4.11 防刷屏）。"""
        if method in _OOB_WARNED_TYPES:
            return
        _OOB_WARNED_TYPES.add(method)
        logger.warning(
            f'拟人化 {method} 跳过：端点越界 start={start} end={end} canvas={self.canvas_size}')

    def _choose(self, dim: str, allowed) -> str | None:
        """唯一读取 persona.weights 并按允许集抽一个方案的地方（Plan 契约 12）。

        在 allowed ∩ 权重表内归一化后抽样；交集为空时返回 None（调用方整体回退）。
        按权重表自身的 key 顺序迭代，保证同 seed 结果可复现。
        """
        weights = self.persona.weights[dim]
        allowed_set = set(allowed)
        total = 0.0
        for key in weights:
            if key in allowed_set:
                total += weights[key]
        if total <= 0:
            return None
        r = self.rng.random() * total
        acc = 0.0
        for key in weights:
            if key in allowed_set:
                acc += weights[key]
                if r < acc:
                    return key
        return None

    def _choose_shape_option(self, gesture_kind: str, safe_region, exclude=None,
                             dist: float | None = None) -> str | None:
        """按 gesture_kind 允许集过滤形状方案（Plan 契约 8）。

        pointer_move 永禁 two_phase（停顿会叠加在预算之上，破坏 §4.7 请求值守恒，
        启用需要独立的预算交互设计）；overshoot 满足其一即进入候选：
        - 显式 safe_region（调用方给出控件边界，终端段校验可执行）；
        - 距离门控（2026-08-26 调研吸收）：dist ≥ CORRECTIVE_MIN_DIST_PX 的
          长距离移动——真人长距离弹道常冲过目标再修正（ballistic+corrective
          子动作结构），越界风险由几何层 _clip_control 画布裁剪兜底。
        exclude 用于 overshoot 包含校验失败后剔除该候选重选。
        """
        if gesture_kind == 'pointer_move':
            allowed = {'bezier', 's_curve', 'jitter_line', 'arc'}
            if safe_region is not None or (
                    dist is not None and dist >= geometry.CORRECTIVE_MIN_DIST_PX):
                allowed.add('overshoot')
        elif gesture_kind == 'swipe':
            # two_phase / overshoot 对普通滑动默认禁用
            allowed = {'bezier', 's_curve', 'arc'}
        elif gesture_kind == 'drag':
            # 契约 8：two_phase 需要"容差足够"判据，API 暂无容差参数，默认不进入候选
            allowed = {'bezier', 's_curve', 'arc'}
        elif gesture_kind == 'idle':
            allowed = {'jitter_line'}
        else:
            raise ValueError(f'plan_move: 未知 gesture_kind {gesture_kind!r}')
        if exclude:
            allowed = allowed - set(exclude)
        return self._choose('shape', allowed)

    def _geometry_seed(self) -> int:
        # 固定几何 seed：派生自人格 seed，同一人格的几何可复现，且不消费父 RNG（契约 6）
        return self.persona.seed + 0x5EED

    def _overshoot_track_fits(self, start: Point, end: Point, safe_region) -> bool:
        """契约 8（2026-08-26 语义修订）：overshoot 的**终端段**必须落在 safe_region 内。

        终端段 = 过冲顶点（主段末点）+ 修正段（2~3 点，rng.integers 半开区间），即返回
        points 的末 n_correct+1 ≤ 4 个点；弹道主段允许越出 safe_region——真人弹道本来就会
        扫过目标区域外，"先过冲再修正回控件内"正是 ballistic+corrective 子动作
        结构。safe_region 为 None 时恒通过（距离门控在 _choose_shape_option
        已放行，画布内越界由几何层 _clip_control 兜底）。用固定 geometry seed
        生成（不消费父 RNG），与 _downscale 的同 seed 生成保持一致。走模块级
        别名以绕过测试对 geometry.shape_points 的 monkeypatch——这里是校验
        不是真实计划生成。
        """
        if safe_region is None:
            return True
        g = np.random.Generator(np.random.PCG64(self._geometry_seed()))
        result = _geometry_shape_points(
            g, start, end, option='overshoot', max_points=timing.PROFILE_MAX_POINTS,
            persona=self.persona, canvas_size=self.canvas_size)
        if result is None:
            return False
        points, _extra = result
        sx, sy, sw, sh = safe_region
        # 顶点 = 距 start 最远的点（过冲主段末点），终端段 = 顶点起的修正段；
        # 修正段点数随机（2~4），用最远点定位对点数稳健，不硬编码窗口长度
        apex_idx = max(range(len(points)),
                       key=lambda i: (points[i][0] - start[0]) ** 2 + (points[i][1] - start[1]) ** 2)
        return all(sx <= px <= sx + sw and sy <= py <= sy + sh
                   for px, py in points[apex_idx:])

    def _downscale(
        self,
        start: Point,
        end: Point,
        shape_option: str,
        speed_option: str,
        budget_s: float,
        timing_mode: str,
        max_points: int | None = None,
        t_map=None,
        interval_s: float | None = None,
    ):
        """先生成点、算真实段长、生成 delay、按实际 delay 验证，失败后降点。

        逐字实现 Task 6「下游契约 ①」的 6 步，**禁止**平均值公式
        int(total_budget / PROFILE_MIN_DELAY_S)：
        1. 固定 geometry_seed 生成点（不重复消耗父 RNG，每次尝试可复现）；
        2. distances = segment_distances(start, points)；
        3. delays = profiled_move_delays(rng_for_timing, distances, T, profile)；
        4. python_sleep：min(delays) < PROFILE_MIN_DELAY_S 则降点重试；
        5. device_wait：不检查 Python 地板，改做整数毫秒可表示性检查
           （目标总毫秒 < 正 delay 数则降点）；
        6. count=2 仍失败时退化为 legacy_move_delays 的近恒定 delays，
           并显式记录未启用可信 profile。

        两个滑动加密扩展（等时间采样）：
        - max_points：点数上限，默认 PROFILE_MAX_POINTS；swipe 由恒定回报率
          模型（预算 × 回报率）计算后传入；
        - t_map + interval_s：等时间映射与恒定回报率间隔。传入时速度编码进
          点密度（慢速区密集），delays = 1/rate ± 1ms 调度抖动——真实 USB/
          触摸上报的间隔围绕采样周期波动而非完美恒定；抖动零均值、不归一化
          （保留自然方差）。不传 t_map 走原 profiled delay 路径。

        返回 (points, delays, extra) 或 None（端点越界，几何生成失败）。
        """
        geometry_seed = self._geometry_seed()
        cap = timing.PROFILE_MAX_POINTS if max_points is None else max_points
        points: list[Point] | None = None
        extra: dict[int, float] = {}
        delays: list[float] | None = None
        profile_untrusted = False
        for count in range(cap, 1, -1):
            g = np.random.Generator(np.random.PCG64(geometry_seed))
            result = geometry.shape_points(
                g, start, end, option=shape_option, max_points=count,
                persona=self.persona, canvas_size=self.canvas_size, t_map=t_map)
            if result is None:
                # 几何层端点越界（理论上 facade 已提前拦截），整体回退
                return None
            points, extra = result
            if t_map is None:
                distances = timing.segment_distances(start, points)
                delays = timing.profiled_move_delays(self.rng, distances, budget_s, speed_option)
            else:
                # 恒定回报率 + 调度抖动：每点间隔 = 1/rate ± 1ms（零均值，不归一化）。
                # python_sleep 的下限是可信门槛地板；device_wait 下限 1ms（w 的量化粒度）
                floor = (timing.PROFILE_MIN_DELAY_S if timing_mode == 'python_sleep'
                         else 0.001)
                delays = [
                    max(interval_s + float(self.rng.uniform(-0.001, 0.001)), floor)
                    for _ in range(len(points))
                ]
            if timing_mode == 'python_sleep':
                if min(delays) >= timing.PROFILE_MIN_DELAY_S:
                    break
            else:  # device_wait：整数毫秒可表示性检查（契约 #6 step 5）
                # 目标总毫秒 = floor(sum * 1000 + 0.5)，与 Task 16 的量化口径一致
                total_ms = int(sum(delays) * 1000 + 0.5)
                positive = sum(1 for d in delays if d > 0)
                if total_ms >= positive:
                    break
        else:
            # count=2 仍失败：退化近恒定 delay，并显式记录未启用可信 profile
            profile_untrusted = True
        if points is None:
            return None
        if profile_untrusted:
            delays = timing.legacy_move_delays(
                self.rng, len(points), _MOVE_FALLBACK_BASE_DELAY_S, total_budget_s=budget_s)
            logger.warning('拟人化 profile 未达可信门槛（含降点后），退化为近恒定 delay')
        return points, delays, extra

    # ---------------------------------------------------------------- 公开方法

    def sample_point(self, roi, prev: Point | None = None) -> Point | None:
        """维度 A 落点。只使用调用方显式传入的 prev，不在 ContextVar/模块级保存历史。"""
        if not self.enabled:
            return None
        option = self._choose('point', geometry.POINT_OPTIONS)
        return geometry.sample_point(self.rng, roi, self.persona, option=option, prev=prev)

    def press_seconds(self, *, fast: bool = False) -> float | None:
        """维度 B 按压时长（秒）。"""
        if not self.enabled:
            return None
        option = self._choose('press', timing.PRESS_OPTIONS)
        return timing.press_seconds(self.rng, self.persona, option=option, fast=fast)

    def plan_move(
        self,
        start: Point,
        end: Point,
        *,
        gesture_kind: str,
        budget_ms: float | None = None,
        safe_region=None,
    ) -> MovePlan | None:
        """一次移动/点击前定位的完整计划；失败（off/越界/无候选/几何失败）返回 None。"""
        if not self.enabled:
            return None
        if not self._endpoint_ok(start) or not self._endpoint_ok(end):
            self._warn_endpoint_oob('plan_move', start, end)
            return None
        return self._plan_move_profiled(start, end, gesture_kind, budget_ms, safe_region)

    def _plan_move_profiled(self, start, end, gesture_kind, budget_ms, safe_region):
        """新二维几何 + 真实段长 profile + 动态降点，two_phase 停顿并入 delay。"""
        # 距离门控：长距离指针移动才允许纠正性子动作（overshoot）进入候选
        dist = math.hypot(float(end[0] - start[0]), float(end[1] - start[1]))
        shape_option = self._choose_shape_option(gesture_kind, safe_region, dist=dist)
        if shape_option is None:
            return None
        if shape_option == 'overshoot' and not self._overshoot_track_fits(start, end, safe_region):
            # 契约 8：终端段必须落在 safe_region 内才启用；越界时剔除候选重选
            shape_option = self._choose_shape_option(
                gesture_kind, safe_region, exclude={'overshoot'}, dist=dist)
            if shape_option is None:
                return None
        speed_option = self._choose('speed', timing.SPEED_OPTIONS)
        budget_s = (budget_ms / 1000.0) if budget_ms is not None else _MOVE_BUDGET_DEFAULT_S
        result = self._downscale(start, end, shape_option, speed_option, budget_s,
                                 timing_mode='python_sleep')
        if result is None:
            return None
        points, delays, extra = result
        # two_phase 的停顿（点索引 → 秒）加到对应 delay 上再构造 MovePlan
        for idx, sec in extra.items():
            delays[idx] += sec
        return plan.MovePlan(points=tuple(points), delays=tuple(delays))

    def plan_swipe(
        self,
        start: Point,
        end: Point,
        *,
        base_delay_s: float | None = None,
        timing_mode: TimingMode = 'python_sleep',
        safe_region=None,
        mouse: bool = False,
        point_cap: int | None = None,
    ) -> MovePlan | None:
        """一次滑动的完整计划。

        timing_mode 只允许 python_sleep（默认，逐点 Python sleep）与 device_wait
        （仅 minitouch 使用）；非法值立即拒绝，不静默回退。
        mouse=True 用鼠标回报率区间（桌面窗口拖拽语义），默认触摸面板区间。
        point_cap 是调用方的通道上限（如 u2 逐点 HTTP RPC 不能承载高回报率）。
        """
        if timing_mode not in _TIMING_MODES:
            raise ValueError(
                f'plan_swipe: 未知 timing_mode {timing_mode!r}，可选 {_TIMING_MODES}')
        if not self.enabled:
            return None
        if not self._endpoint_ok(start) or not self._endpoint_ok(end):
            self._warn_endpoint_oob('plan_swipe', start, end)
            return None
        return self._plan_swipe_profiled(
            start, end, base_delay_s, timing_mode, safe_region, mouse, point_cap)

    def _plan_swipe_profiled(self, start, end, base_delay_s, timing_mode, safe_region,
                             mouse=False, point_cap=None):
        """恒定回报率设备仿真——间隔严格相等的点流。

        真实输入设备按固定采样率（回报率）上报事件：每个事件的时间间隔相同、
        速度编码在位置增量里。回报率来自人格分位数映射到真实设备区间（触摸
        面板 100~240Hz / 鼠标 125~1000Hz），同人格固定；点数 = 预算 × 回报率
        （floor），位置由路径弧长按速度剖面分布（t_map 等时间采样）。
        预算 = base_delay_s × PROFILE_MAX_POINTS（调用方把总时长换算成 base 传入）。

        通道适配：device_wait（minitouch w 由设备端执行）用整毫秒间隔，回报率
        直接可达；python_sleep 受 Windows sleep 地板限制，回报率 clamp 到
        1/PROFILE_MIN_DELAY_S（200Hz），且调用方可传 point_cap 表达通道上限
        （u2 逐点 HTTP RPC）。
        """
        shape_option = self._choose_shape_option('swipe', safe_region)
        if shape_option is None:
            return None
        speed_option = self._choose('speed', timing.SPEED_OPTIONS)
        base = base_delay_s if base_delay_s is not None else _SWIPE_BASE_DELAY_S
        budget_s = base * timing.PROFILE_MAX_POINTS
        rate = timing.report_rate_hz(self.persona.report_rate_q, mouse=mouse)
        if timing_mode == 'device_wait':
            # 整毫秒间隔：w 只能整 ms，恒定回报率下全部取同一整数值，
            # 有效回报率 = 1000/interval_ms 的离散档位（与真实硬件档位同构）
            interval_ms = max(1, int(1000.0 / rate + 0.5))
            interval_s = interval_ms / 1000.0
            count = int(budget_s * 1000.0 + 0.5) // interval_ms
        else:
            # python_sleep：Windows sleep 精度 ~2.9ms、可信门槛 5ms，
            # 高于此的回报率物理上投递不出来，clamp 到门槛
            rate = min(rate, 1.0 / timing.PROFILE_MIN_DELAY_S)
            interval_s = 1.0 / rate
            count = int(budget_s / interval_s)
        cap = timing.SWIPE_MAX_POINTS_CAP if point_cap is None else point_cap
        count = max(timing.SWIPE_MIN_POINTS, min(count, cap))
        t_map = timing.time_param_map(speed_option)
        result = self._downscale(start, end, shape_option, speed_option, budget_s, timing_mode,
                                 max_points=count, t_map=t_map, interval_s=interval_s)
        if result is None:
            return None
        points, delays, extra = result
        for idx, sec in extra.items():
            delays[idx] += sec
        # 速度已编码进点密度（末段减速 = 末段点距变小、间隔恒定），不再替换末段
        # delay：50~130ms 的大 delay 会让事件间隔突增——USB/触摸上报不会这样，
        # 间隔方差本身就是指纹。"到达后停顿再抬起"由维度 F 的 touch_liftoff 表达
        # （UP 前小步微位移 + 恒定间隔）。原维度 H 只在 light 档生效，已随 light
        # 于 2026-09-13 一并删除
        return plan.MovePlan(points=tuple(points), delays=tuple(delays))

    def plan_dwell(self, target: Point) -> DwellPlan | None:
        """维度 E 到位停顿。target 作为业务端点先校验，越界返回 None 且不调用模块函数。"""
        if not self.enabled:
            return None
        if not self._endpoint_ok(target):
            self._warn_endpoint_oob('plan_dwell', target, None)
            return None
        option = self._choose('dwell', timing.DWELL_OPTIONS)
        return timing.plan_dwell(
            self.rng, target, self.persona, option=option,
            canvas_size=self.canvas_size)

    def plan_pointer_tail(self, target: Point) -> TailPlan | None:
        """维度 F（指针语义）UP 后漂移。target 同样按业务端点校验。"""
        if not self.enabled:
            return None
        if not self._endpoint_ok(target):
            self._warn_endpoint_oob('plan_pointer_tail', target, None)
            return None
        return gesture.plan_pointer_tail(
            self.rng, target, self.persona, canvas_size=self.canvas_size)

    def plan_touch_liftoff(self, target: Point) -> TailPlan | None:
        """维度 F（触摸语义）UP 前微位移。None 可能是 off 旁路或策略 none（20% 人类方差）。"""
        if not self.enabled:
            return None
        if not self._endpoint_ok(target):
            self._warn_endpoint_oob('plan_touch_liftoff', target, None)
            return None
        option = self._choose('touch_liftoff', gesture.TOUCH_LIFTOFF_OPTIONS)
        return gesture.plan_touch_liftoff(
            self.rng, target, self.persona, option=option,
            canvas_size=self.canvas_size)

    def plan_hold(
        self,
        target: Point,
        duration_s: float,
        *,
        timing_mode: TimingMode = 'python_sleep',
        mouse: bool = False,
        point_cap: int | None = None,
    ) -> MovePlan | None:
        """维度 J：长按 hold 期间的微颤事件流（2026-08-26 调研对标新增）。

        平台长按识别器留 8~10px 移动容差（iOS allowableMovement / Android
        touch slop）正是因为真人按住期间手指持续微动；旧长按 hold 期间零事件
        是整秒级的事件流死寂指纹。本方法把死寂替换为恒定回报率的微颤
        MOVE 流：间隔 = 1/回报率 ± 1ms 调度抖动（与 swipe 的恒定回报率模型
        同构），位置是围绕 target 的 ±1~3px 随机游走（远离平台容差，
        不会取消长按）。duration_s 是业务时长（预算），点数 = 预算 × 回报率。
        通道适配语义与 plan_swipe 完全一致：device_wait 整毫秒档位、
        python_sleep clamp 200Hz、point_cap 表达通道上限。预算守恒精度：
        通道上限命中时 sum(delays) 精确等于 duration（间隔摊为 预算/点数）；
        未命中时 count 向下取整，sum(delays) = duration − (duration mod 间隔)
        ——UP 最多提前一个回报率间隔（4~10ms），对秒级长按可忽略。

        返回 None 表示本次不做微颤（off 或 'none' 策略，约 20% 人类方差：
        真人偶发的"按得很稳"），调用方回退纯 sleep。
        """
        if not self.enabled:
            return None
        if not self._endpoint_ok(target):
            self._warn_endpoint_oob('plan_hold', target, None)
            return None
        if isinstance(duration_s, bool) or not isinstance(duration_s, (int, float)):
            raise ValueError(f'plan_hold: duration_s 必须是数值，收到 {duration_s!r}')
        if not math.isfinite(duration_s) or duration_s < 0:
            raise ValueError(f'plan_hold: duration_s 必须是有限非负数，收到 {duration_s}')
        option = self._choose('hold', gesture.HOLD_OPTIONS)
        if option == 'none':
            return None
        # 恒定回报率：与 _plan_swipe_profiled 相同的通道适配推导
        rate = timing.report_rate_hz(self.persona.report_rate_q, mouse=mouse)
        if timing_mode == 'device_wait':
            interval_ms = max(1, int(1000.0 / rate + 0.5))
            interval_s = interval_ms / 1000.0
            count = int(duration_s * 1000.0 + 0.5) // interval_ms
        else:
            rate = min(rate, 1.0 / timing.PROFILE_MIN_DELAY_S)
            interval_s = 1.0 / rate
            count = int(duration_s / interval_s)
        cap = timing.SWIPE_MAX_POINTS_CAP if point_cap is None else point_cap
        if count > cap:
            # 通道上限命中：点数钉在 cap、间隔拉长为 预算/点数——有效回报率
            # 降为 cap/预算，但 sum(delays) 恒等于 duration_s（长按时长是业务
            # 参数，UP 不得提前；截断点数会让 hold 短掉 (1-cap/natural)×预算）
            count = cap
            interval_s = duration_s / count
        if count <= 0:
            # 预算短于一个回报率间隔：物理上放不下任何微颤事件，回退纯 sleep
            return None
        points = gesture.plan_hold_jitter(
            self.rng, target, count, canvas_size=self.canvas_size)
        if not points:
            return None
        # 间隔抖动与 _downscale 的恒定回报率分支同式：±1ms 零均值不归一化
        floor = (timing.PROFILE_MIN_DELAY_S if timing_mode == 'python_sleep'
                 else 0.001)
        delays = [
            max(interval_s + float(self.rng.uniform(-0.001, 0.001)), floor)
            for _ in range(len(points))
        ]
        return plan.MovePlan(points=tuple(points), delays=tuple(delays))

    def gap_seconds(self, default: float) -> float | None:
        """维度 I 动作间隔：把固定常量换成同均值抖动。

        维度 I 没有独立权重维（DEFAULT_WEIGHTS 无 'gap'），恒走 jitter 打散常量
        指纹；'fixed' 等价于 off 的原值，开档没必要再选它。
        """
        if not self.enabled:
            return None
        return timing.gap_seconds(self.rng, self.persona, default, option='jitter')

    def pace_view(self) -> float:
        """预付制等待的主消费点：在下一次**截图**之前等满操作间隔要求。

        由 Device.screenshot 入口调用。截图是 appear_then_click 等决策模式
        的依据——等待发生在「看」之前保证决策画面新鲜：目标仍在 → 动作
        立即执行（执行前零等待）；目标已消失（弹窗过期、结算画面自动关闭）
        → 识别自然失败、不会产生按旧目标点击的过期点击。这是 2026-08-27
        修复两个线上现象（接受邀请过期没进房、结算关闭后误点庭院）的核心：
        旧模型把等待插在「决策→执行」之间，执行时画面早已切换。

        Returns:
            本次等待的秒数（off / 无要求 / 已自然满足返回 0）。
        """
        if not self.enabled:
            return 0.0
        if self._gap_last_ts is None or self._pending_require <= 0:
            return 0.0
        elapsed = time.time() - self._gap_last_ts
        wait = self._pending_require - elapsed
        if wait <= 0:
            self._pending_require = 0.0
            return 0.0
        self._pending_require = 0.0
        time.sleep(wait)
        return wait

    def pace_execute(self) -> float:
        """执行前兜底等待：仅覆盖「无截图背靠背操作」的罕见场景。

        正常流程的操作间隔要求已由 pace_view（截图入口）等满，这里恒为 0。
        只有两次操作之间没有任何截图时（如 multi_click 循环），要求未被
        消费才在这里补——封顶 EXECUTE_PACE_MAX_S（保护决策-执行的画面
        有效期窗口），剩余要求留给下一次 pace_view / pace_execute。
        """
        if not self.enabled:
            return 0.0
        if self._gap_last_ts is None or self._pending_require <= 0:
            return 0.0
        elapsed = time.time() - self._gap_last_ts
        wait = min(max(0.0, self._pending_require - elapsed),
                   timing.EXECUTE_PACE_MAX_S)
        if wait > 0:
            time.sleep(wait)
        # 扣除已流逝/已等待的部分，剩余要求留给后续消费点
        self._pending_require = max(
            0.0, self._pending_require - (elapsed + wait))
        return wait

    def record_action(self, target=None, name=None, roi=None,
                      repeat_exempt: bool = False) -> None:
        """操作结束打点：同一资源重复判定 + 计算下次要求。

        由 Control 在每次输入操作（click/long_click/swipe/drag）完成后调用。
        - 同一资源判定（优先级从高到低）：
          ① name + roi（点击控件名与其 roi_front）：同名且 ROI 一致才是同一
            资源——同名但 ROI 不同说明任务在复用同一 RuleClick 遍历列表
            （如 KekkaiUtilize 逐个点结界卡），判为新资源重新计数；本次或
            上次拿不到稳定 ROI（直调 device.click / 匹配结果驱动的区域）时
            退化为仅按名判重，静态控件行为不变；
          ② target 坐标半径（REPEAT_BACKOFF_RADIUS_PX）：无名点击（直接
            device.click 未传控件名）的兜底；
          ③ 都没有（swipe/drag）：重置计数。
          判定命中 → 连续计数 +1，下次要求并入退避（连续第 2/3/4/5/6/7/8+
          次 1.5/1.5/2/2/4/10/16s 封顶）；换资源重新从 1 计；
          repeat_exempt=True 的操作恒按首次点击计、不累计退避——供业务语义
          就是「预期内连点」的资源在点击处显式声明豁免（每次点击都真实
          生效、退避前提「点了没反应」不成立，如十连召唤的金按钮每出一抽
          重现一次）；豁免点击仍更新判重基准，不影响其他控件的计数重置；
        - 下次要求由 timing.next_action_requirement 计算（单档区间随距离平移，
          uniform 抽样；再与退避取 max），挂起待 pace_view 消费。

        off 档无副作用。
        """
        if not self.enabled:
            return
        # 只留计时原点（预付制用它算「已流逝」）；原意图间隔记账
        # （_mech_wait 扣除 + _gap_window 追加）随 base 自适应一并移除
        self._gap_last_ts = time.time()
        # 同一资源判定：控件名 + roi_front 判重（任一方缺 ROI 退化为仅按名），
        # 坐标半径兜底，swipe/drag 重置
        if name is not None and name not in ('Click', 'LongClick', 'SWIPE', 'DRAG'):
            # 有真实控件名的点击按名+ROI 判重（泛称视同无名，走坐标兜底）：
            # 同名且（任一方缺稳定 ROI 或两 ROI 相同）才累计——同名但 ROI
            # 不同是任务在复用同一 RuleClick 遍历列表（每次改写 roi_front
            # 点不同项，如 KekkaiUtilize 的 select_card 逐卡点击），判为新资源
            roi_key = tuple(roi) if roi is not None else None
            roi_same = (roi_key is None or self._repeat_roi is None
                        or roi_key == self._repeat_roi)
            # repeat_exempt：预期内连点的资源在点击处声明豁免，恒按首次
            # 点击计、不累计退避——退避前提「点了没反应」对它们不成立
            if not repeat_exempt and self._repeat_name == name and roi_same:
                self._repeat_count += 1
            else:
                self._repeat_count = 1
            self._repeat_name = name
            self._repeat_roi = roi_key
            self._repeat_point = None  # 名称判定后坐标兜底不再参与
        elif target is not None:
            if (not repeat_exempt
                    and self._repeat_point is not None
                    and math.hypot(target[0] - self._repeat_point[0],
                                   target[1] - self._repeat_point[1])
                    <= timing.REPEAT_BACKOFF_RADIUS_PX):
                self._repeat_count += 1
            else:
                self._repeat_count = 1
            self._repeat_point = (int(target[0]), int(target[1]))
            self._repeat_name = None
            self._repeat_roi = None  # 坐标兜底路径不持有 ROI，清空避免跨路径比较
        else:
            self._repeat_count = 0
            self._repeat_name = None
            self._repeat_point = None
            self._repeat_roi = None
        # 本次落点与上一落点的距离：决定间隔区间的位置（区间随距离整体平移，
        # 越远越慢）。无落点（首次操作 / 无名 swipe）时 dist 为 None
        dist = None
        if target is not None and self._last_record_point is not None:
            dist = math.hypot(target[0] - self._last_record_point[0],
                              target[1] - self._last_record_point[1])
        if target is not None:
            self._last_record_point = (int(target[0]), int(target[1]))
        else:
            self._last_record_point = None
        self._pending_require = timing.next_action_requirement(
            self.rng, self._repeat_count, dist)

    def plan_idle(self, since_last_s: float, cursor: Point | None) -> MovePlan | None:
        """维度 G 点击间空闲。cursor 未知或未达阈值时返回 None（策略层语义）。"""
        if not self.enabled:
            return None
        if cursor is not None and not self._endpoint_ok(cursor):
            # cursor 是既有业务状态而非策略新增点，越界时必须整体回退，不能静默裁剪。
            self._warn_endpoint_oob('plan_idle', cursor, None)
            return None
        option = self._choose('idle', gesture.IDLE_OPTIONS)
        return gesture.plan_idle(
            self.rng, since_last_s, cursor, self.persona, option=option,
            canvas_size=self.canvas_size)


__all__ = [
    'HumanizeLevel',
    'LEVELS',
    'GestureKind',
    'TimingMode',
    'HumanizerContext',
    '_current_humanizer',
    'set_current_humanizer',
    'get_current_humanizer',
    'bind_humanizer',
]
