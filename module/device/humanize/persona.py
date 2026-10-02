"""拟人化人格（Spec §6）。

人格 = "这台机器的这个配置实例像哪个人"。它决定各维度**选哪个方案**的概率，
以及按压中位数、瞄准偏心、手腕转动方向等标量。同一个人格跨重启保持不变，
所以它必须持久化（Task 4），也必须能从 seed 完全复现。

档位收敛为 off/medium 两档后，off 根本不加载人格（不读文件、不生成），
所以人格只属于"开档"这一种状态：方案允许集按 gesture_kind 划分，不再与档位求交。
"""
from __future__ import annotations

import json
import math
import os
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import numpy as np

from module.logger import logger

# v2（2026-08-26）：权重表新增 'hold' 维（长按 hold 微颤）。旧人格 JSON 因
# 严格键校验失败自动重签（换新 seed），日志记本版本号可归因到这次 schema 变更
# v3（2026-09-13）：档位收敛为 off/medium，权重表删除 swipe_tail 整维、
# pointer_tail 整维与 dwell.hesitate；同时删掉 shape 维里永远抽不到的 two_phase
# 权重（_choose_shape_option 的三个 gesture_kind 允许集都不放行它，等 API 能
# 表达"容差足够"判据时再加回来——几何实现 geometry.shape_points 仍然保留）。
# 旧人格同样因键集合不一致自动重签
PERSONA_VERSION = 3

# Dirichlet 浓度系数。用 25 而非 8：浓度太低会采出退化人格（某维度权重逼近 1，
# 等于"永远只选一个方案"），与"多方案随机执行"的目标直接矛盾
DIRICHLET_ALPHA_SCALE = 25

# 各维度的默认权重。键名与 §4.5 的门面方法一一对应（如 touch_liftoff →
# plan_touch_liftoff）。pointer_tail 与 swipe_tail 两维随 2026-09-13 的档位收敛
# 一并删除：前者删掉 slide_away 后只剩 micro_drift 一个候选，单候选的权重没有
# 意义（plan_pointer_tail 已改为直接生成）；后者（维度 H 滑动末段）随 light 档
# 整体下线。
# 标注"今天"的现状方案默认不进本表（uniform / same_point / fixed3 / fixed / none），
# 唯一例外是 touch_liftoff.none —— 保留 0.2 表示约 20% 的触摸动作不产生抬起前漂移，
# 这是真人方差而不是机器指纹
DEFAULT_WEIGHTS: dict[str, dict[str, float]] = {
    'point': {'center_gauss': 0.50, 'offset_gauss': 0.35, 'edge_avoid': 0.10, 'prev_biased': 0.05},
    'press': {'lognormal': 0.60, 'bimodal': 0.20, 'gamma': 0.20},
    'shape': {'bezier': 0.30, 'overshoot': 0.30, 'arc': 0.10, 's_curve': 0.05, 'jitter_line': 0.05},
    'speed': {'min_jerk': 0.60, 'ease_out': 0.25, 'sigmoid': 0.15},
    'dwell': {'gauss': 0.75, 'settle': 0.20},
    'touch_liftoff': {'liftoff_drift': 0.8, 'none': 0.2},
    'idle': {'idle_drift': 0.7, 'park': 0.3},
    # 维度 J（长按 hold 微颤，2026-08-26 新增）：tremor 常态，none 保留约两成
    # "按得很稳"的人类方差
    'hold': {'tremor': 0.8, 'none': 0.2},
}

# 标量取值区间：generate() 的采样与 from_dict() 的校验共用这一张表（Plan 契约 13）。
# 分成两处写会让 generate() 产出的人格通不过自己的 from_dict()
SCALAR_RANGES: dict[str, tuple[float, float]] = {
    'press_median': (60.0, 90.0),       # ms，2026-09-06 用户实测短按 60~90ms（原 70~130）
    'press_sigma': (0.25, 0.45),        # Spec §5 B
    'press_shape': (2.5, 5.0),          # Spec §5 B
    'dwell_mu': (50.0, 110.0),          # ms，Spec §5 E
    'move_speed_scale': (0.85, 1.30),   # Spec §5 D
    # 设备回报率分位数（不是 Hz 值）：触摸/鼠标各自的真实回报率区间不同，
    # 存分位数由 timing.report_rate_hz 映射到对应区间。设备回报率是硬件属性，
    # 同一个人格固定——换算来源不能每次随机
    'report_rate_q': (0.05, 0.95),
}

AIM_BIAS_RANGE = (-0.22, 0.22)          # 每个分量，Spec §5 A offset_gauss


class PersonaInvalid(ValueError):
    """人格 JSON 校验失败。

    只应由 PersonaStore 捕获并触发重建。策略层永远不该看到这个异常——非法人格
    在 from_dict 就被拦住，不会带着 NaN 权重走到 numpy 采样。
    """


@dataclass(frozen=True)
class Persona:
    version: int
    created: str
    seed: int
    aim_bias: tuple[float, float]
    press_median: float
    press_sigma: float
    press_shape: float
    dwell_mu: float
    arc_side: int
    move_speed_scale: float
    # 设备回报率分位数（SCALAR_RANGES 注释同）：timing.report_rate_hz 映射到
    # 触摸/鼠标的真实区间，设备回报率是硬件属性所以同人格固定
    report_rate_q: float
    # frozen 对 dict 只是浅冻结：字段绑定不可改，dict 内容仍可改。
    # 这里接受这个折中——weights 只被 facade 的 _choose() 读取，不对外暴露；
    # 换成 MappingProxyType 会让 to_dict/from_dict 与 == 都要额外处理，不值得
    weights: dict[str, dict[str, float]]

    # ---------------------------------------------------------------- 生成

    @classmethod
    def generate(cls, seed: int | None = None, *, created: str | None = None) -> 'Persona':
        """从 seed 派生一个完整人格。

        所有随机数来自独立的 Generator(PCG64(seed))，**不使用全局 np.random /
        random** —— 污染全局序列会改变既有代码的随机行为，那是零回归的一部分。

        采样顺序（权重 → 标量）必须固定，否则同 seed 不再可复现。
        """
        if seed is None:
            seed = int.from_bytes(os.urandom(4), 'big')
        rng = np.random.Generator(np.random.PCG64(seed))

        weights: dict[str, dict[str, float]] = {}
        for dim, defaults in DEFAULT_WEIGHTS.items():
            keys = list(defaults)
            alpha = [defaults[k] * DIRICHLET_ALPHA_SCALE for k in keys]
            sampled = rng.dirichlet(alpha)
            weights[dim] = {k: float(v) for k, v in zip(keys, sampled)}

        def _u(name: str) -> float:
            lo, hi = SCALAR_RANGES[name]
            return float(rng.uniform(lo, hi))

        return cls(
            version=PERSONA_VERSION,
            # created 在首次生成时定格并持久化，to_dict() 不动态刷新
            created=created or datetime.now().isoformat(timespec='seconds'),
            seed=int(seed),
            aim_bias=(
                float(rng.uniform(*AIM_BIAS_RANGE)),
                float(rng.uniform(*AIM_BIAS_RANGE)),
            ),
            press_median=_u('press_median'),
            press_sigma=_u('press_sigma'),
            press_shape=_u('press_shape'),
            dwell_mu=_u('dwell_mu'),
            # 同一个人的手腕转动方向固定，所以 arc_side 是人格字段而不是每次随机
            arc_side=int(rng.choice([-1, 1])),
            move_speed_scale=_u('move_speed_scale'),
            report_rate_q=_u('report_rate_q'),
            weights=weights,
        )

    # ---------------------------------------------------------------- 序列化

    def to_dict(self) -> dict:
        """转成可持久化的 dict。created 原样输出，不取当前时间。"""
        d = {
            'version': self.version,
            'created': self.created,
            'seed': self.seed,
            'aim_bias': [self.aim_bias[0], self.aim_bias[1]],
            'arc_side': self.arc_side,
            'weights': {dim: dict(opts) for dim, opts in self.weights.items()},
        }
        for name in SCALAR_RANGES:
            d[name] = getattr(self, name)
        return d

    @classmethod
    def from_dict(cls, raw: dict) -> 'Persona':
        """严格解析。任何一项不通过就抛 PersonaInvalid，由调用方重建人格。

        这里刻意"宁可重建也不放过"：一个带 NaN 权重的人格能顺利构造，却会在
        几百次点击之后的某次 numpy 采样里炸掉，现场早已丢失。
        """
        if not isinstance(raw, dict):
            raise PersonaInvalid(f'人格必须是 JSON 对象，收到 {type(raw).__name__}')

        version = raw.get('version')
        if version != PERSONA_VERSION:
            raise PersonaInvalid(f'version 不匹配：期望 {PERSONA_VERSION}，收到 {version!r}')

        # ---- 权重：维度集合、方案 key、数值有效性、每维度和 > 0
        weights_raw = raw.get('weights')
        if not isinstance(weights_raw, dict):
            raise PersonaInvalid(f'weights 必须是对象，收到 {type(weights_raw).__name__}')
        if set(weights_raw) != set(DEFAULT_WEIGHTS):
            missing = sorted(set(DEFAULT_WEIGHTS) - set(weights_raw))
            extra = sorted(set(weights_raw) - set(DEFAULT_WEIGHTS))
            raise PersonaInvalid(f'weights 维度不一致：缺 {missing}，多 {extra}')
        weights: dict[str, dict[str, float]] = {}
        for dim, defaults in DEFAULT_WEIGHTS.items():
            opts = weights_raw[dim]
            if not isinstance(opts, dict):
                raise PersonaInvalid(f'weights.{dim} 必须是对象')
            if set(opts) != set(defaults):
                raise PersonaInvalid(
                    f'weights.{dim} 方案 key 与当前策略表不一致：'
                    f'期望 {sorted(defaults)}，收到 {sorted(opts)}')
            clean: dict[str, float] = {}
            for k, v in opts.items():
                if isinstance(v, bool) or not isinstance(v, (int, float)):
                    raise PersonaInvalid(f'weights.{dim}.{k} 权重必须是数值，收到 {v!r}')
                if not math.isfinite(v) or v < 0:
                    raise PersonaInvalid(f'weights.{dim}.{k} 权重必须是有限非负数，收到 {v!r}')
                clean[k] = float(v)
            if sum(clean.values()) <= 0:
                raise PersonaInvalid(f'weights.{dim} 权重和必须 > 0（拒绝全 0）')
            weights[dim] = clean

        # ---- arc_side：必须精确是 -1 或 +1，且不能是 float
        arc_side = raw.get('arc_side')
        if isinstance(arc_side, bool) or not isinstance(arc_side, int) or arc_side not in (-1, 1):
            raise PersonaInvalid(f'arc_side 必须是 -1 或 +1，收到 {arc_side!r}')

        # ---- aim_bias：两个分量都在区间内
        aim_bias = raw.get('aim_bias')
        if not isinstance(aim_bias, (list, tuple)) or len(aim_bias) != 2:
            raise PersonaInvalid(f'aim_bias 必须是长度 2 的数组，收到 {aim_bias!r}')
        aim: list[float] = []
        for v in aim_bias:
            if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v):
                raise PersonaInvalid(f'aim_bias 分量必须是有限数值，收到 {v!r}')
            if not AIM_BIAS_RANGE[0] <= v <= AIM_BIAS_RANGE[1]:
                raise PersonaInvalid(f'aim_bias 分量 {v} 超出 {AIM_BIAS_RANGE}')
            aim.append(float(v))

        # ---- 标量：存在、为正、落在声明区间内
        scalars: dict[str, float] = {}
        for name, (lo, hi) in SCALAR_RANGES.items():
            if name not in raw:
                raise PersonaInvalid(f'缺少必填字段 {name}')
            v = raw[name]
            if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v):
                raise PersonaInvalid(f'{name} 必须是有限数值，收到 {v!r}')
            if v <= 0:
                raise PersonaInvalid(f'{name} 必须为正数，收到 {v}')
            if not lo <= v <= hi:
                raise PersonaInvalid(f'{name} = {v} 超出设计范围 [{lo}, {hi}]')
            scalars[name] = float(v)

        seed = raw.get('seed')
        if isinstance(seed, bool) or not isinstance(seed, int) or seed < 0:
            raise PersonaInvalid(f'seed 必须是非负整数，收到 {seed!r}')
        created = raw.get('created')
        if not isinstance(created, str) or not created:
            raise PersonaInvalid(f'created 必须是非空字符串，收到 {created!r}')

        return cls(
            version=PERSONA_VERSION,
            created=created,
            seed=seed,
            aim_bias=(aim[0], aim[1]),
            arc_side=arc_side,
            weights=weights,
            **scalars,
        )


def _write_json_atomic(path: Path, data: dict) -> None:
    """tmp 文件 + os.replace 原子写 JSON。

    与 tasks/Component/MultiAccountRunner/progress.py:43 同一模式，但**在本模块
    重实现而不是 import 它**：`module/device/` 反向依赖 `tasks/` 会造成分层倒置，
    而这段逻辑只有 6 行。直接覆写时进程被杀会留下截断 JSON，
    os.replace 在 Windows 上对已存在目标是原子替换，可消除该窗口。
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix('.json.tmp')
    with open(tmp_path, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp_path, path)


class PersonaStore:
    """人格持久化。

    路径 config/tasks_config/humanize_persona_<config>.json，与
    multi_daily_progress_<config>.json 同一命名惯例；该目录已被 .gitignore 忽略，
    属运行期数据而非用户配置。

    按 config 名隔离：两个实例是两个"人"，共用一份人格等于两台机器同一指纹。
    """

    def __init__(self, config_name: str, base_dir: str = 'config/tasks_config'):
        self.config_name = config_name
        self.path = Path(base_dir) / f'humanize_persona_{config_name}.json'

    def load_or_create(self) -> Persona:
        """读取人格；不存在或损坏则重签并落盘。

        任何失败路径都返回一个**可用**的人格，绝不返回 None 或抛异常——调用方
        （Device.__init__）不该为拟人化的存储问题而失败。
        """
        try:
            with open(self.path, 'r', encoding='utf-8') as f:
                raw = json.load(f)
        except FileNotFoundError:
            persona = Persona.generate()
            logger.info(f'人格文件不存在，已新签: {self.path}')
            self._save(persona)
            return persona
        except (json.JSONDecodeError, OSError, UnicodeDecodeError) as e:
            persona = Persona.generate()
            logger.warning(f'人格文件读取失败（{type(e).__name__}: {e}），已重签: {self.path}')
            self._save(persona)
            return persona

        try:
            return Persona.from_dict(raw)
        except PersonaInvalid as e:
            # 记录旧 version 与出错字段，便于回溯是哪次改动引入的不兼容
            old_version = raw.get('version') if isinstance(raw, dict) else None
            persona = Persona.generate()
            logger.warning(f'人格文件校验失败（version={old_version!r}）：{e}；已重签')
            self._save(persona)
            return persona

    def _save(self, persona: Persona) -> None:
        """落盘失败不阻断运行，用内存人格继续。

        代价只是下次启动换一个"人"，而抛异常会让整个 Device 初始化失败。
        """
        try:
            _write_json_atomic(self.path, persona.to_dict())
        except (OSError, TypeError, ValueError) as e:
            logger.warning(f'人格文件写入失败，本次使用内存人格: {e}')
