"""拟人化输入的计划数据类（Spec §4.6）。

三个 frozen dataclass 是策略层与 backend 之间的唯一契约载体。backend 只把最终
计划翻译成原生事件，不自行解释语义——这正是"不同后端各自解释同一个 delays"
那类隐患的收口方式。

统一的 delay 语义：delays[i] 是发送 points[i] **之前**的等待。UP 之后绝不消费
计划里的 delay。
"""
from __future__ import annotations

import math
from dataclasses import dataclass

# 授权坐标空间（1280×720）下的整数点。不接受 float / 字符串 / 三元组：
# 坐标一旦带小数，各 backend 的取整策略不同就会产生分歧
Point = tuple[int, int]


def _require_tuple(value, field: str) -> None:
    """容器必须是 tuple。

    facade 负责把 geometry/timing 的内部 list 转成 tuple（Spec §4.6）；这里硬性
    拒绝 list，否则 frozen dataclass 只冻结了字段绑定，内部元素仍可被调用方
    事后 append，不可变语义是假的。
    """
    if not isinstance(value, tuple):
        raise TypeError(f'{field}: 必须是 tuple（facade 负责转换），收到 {type(value).__name__}')


def _validate_point(value, field: str) -> None:
    """坐标必须是长度 2 的整数 tuple。bool 是 int 子类，需显式排除。"""
    if not isinstance(value, tuple) or len(value) != 2:
        raise ValueError(f'{field}: 坐标必须是长度 2 的 tuple，收到 {value!r}')
    for v in value:
        if isinstance(v, bool) or not isinstance(v, int):
            raise ValueError(f'{field}: 坐标分量必须是 int，收到 {v!r}')


def _validate_delay(value, field: str) -> None:
    """delay 必须是有限非负实数：拒绝 NaN、±inf、负数。

    NaN 尤其危险：它会静默污染 sum()，让 total_seconds 变成 NaN，而预算门禁的
    比较对 NaN 恒为 False——门禁会"通过"。
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f'{field}: delay 必须是数值，收到 {value!r}')
    if not math.isfinite(value):
        raise ValueError(f'{field}: delay 必须有限，收到 {value!r}')
    if value < 0:
        raise ValueError(f'{field}: delay 不能为负，收到 {value!r}')


def _validate_parallel(points, delays, cls_name: str) -> None:
    """校验 points/delays 这对等长并行序列。MovePlan 与 TailPlan 共用。"""
    _require_tuple(points, f'{cls_name}.points')
    _require_tuple(delays, f'{cls_name}.delays')
    if not points:
        raise ValueError(f'{cls_name}: points 不能为空')
    if len(points) != len(delays):
        raise ValueError(
            f'{cls_name}: points({len(points)}) 与 delays({len(delays)}) 长度必须相等')
    for i, p in enumerate(points):
        _validate_point(p, f'{cls_name}.points[{i}]')
    for i, d in enumerate(delays):
        _validate_delay(d, f'{cls_name}.delays[{i}]')


@dataclass(frozen=True)
class MovePlan:
    """一次移动/滑动的完整计划。

    points 不含起点，末项恒为调用方要求的终点（终点一致性由 facade 在构造前
    校验——数据类拿不到"调用方要求的终点"这个信息）。
    """

    points: tuple[Point, ...]
    delays: tuple[float, ...]

    def __post_init__(self):
        _validate_parallel(self.points, self.delays, 'MovePlan')

    @property
    def total_seconds(self) -> float:
        """最终计划的请求耗时。

        这是预算门禁的唯一比较对象：delays 是**合并后**的真值（facade 可能把
        two_phase 的停顿并入对应 delay），不能再拿合并前的基础预算断言。
        """
        return float(sum(self.delays))


@dataclass(frozen=True)
class DwellPlan:
    """维度 E 的到位停顿计划。

    segments[i] = (point, second)：point 不为 None 时**先发送 point、再等待
    second**；None 表示只等待。顺序写死在这里，不让 backend 猜。
    """

    segments: tuple[tuple[Point | None, float], ...]

    def __post_init__(self):
        _require_tuple(self.segments, 'DwellPlan.segments')
        if not self.segments:
            raise ValueError('DwellPlan: segments 不能为空')
        for i, seg in enumerate(self.segments):
            if not isinstance(seg, tuple) or len(seg) != 2:
                raise ValueError(
                    f'DwellPlan.segments[{i}]: 必须是 (point|None, second)，收到 {seg!r}')
            point, second = seg
            if point is not None:
                _validate_point(point, f'DwellPlan.segments[{i}][0]')
            _validate_delay(second, f'DwellPlan.segments[{i}][1]')


@dataclass(frozen=True)
class TailPlan:
    """维度 F 的收尾计划。

    与 MovePlan 同样的 delay 语义（delays[i] 在 points[i] 之前），但**事件位置
    相反**，所以必须是独立类型而不是 MovePlan 加一个 bool：
    - 指针语义（plan_pointer_tail）：UP → sleep → MOVE，漂移在抬起之后；
    - 触摸语义（plan_touch_liftoff）：sleep → MOVE → ... → UP，微位移在抬起之前。
    """

    points: tuple[Point, ...]
    delays: tuple[float, ...]

    def __post_init__(self):
        _validate_parallel(self.points, self.delays, 'TailPlan')
