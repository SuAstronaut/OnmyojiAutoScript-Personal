# This Python file uses the following encoding: utf-8
# 分级热重载策略：COLD prefix allowlist > HOT exact-path allowlist > 其余全部 WARM（default-deny）。
# HOT 白名单由 derive_hot_paths() 从 ConfigModel schema 自动派生：所有 scalar/Enum/单值时间
# 叶子字段均可中途生效，list/dict/动态 key 子树/scheduler/script.device 按结构性规则排除。
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from enum import Enum
from functools import lru_cache
from typing import Sequence, get_origin

HOT = "hot"
WARM = "warm"
COLD = "cold"

# COLD 前缀单一事实源：ReloadPolicy 默认值与 derive_hot_paths 排除规则共用，避免两处漂移。
COLD_PREFIXES: Sequence[tuple[str, ...]] = (("script", "device"),)

# 允许中途生效的叶子类型。bool 是 int 子类、datetime 是 date 子类，重复覆盖无害。
_HOT_LEAF_TYPES = (bool, int, float, str, Enum, time, date, datetime, timedelta)

# 不下降的子模型字段名：每个任务的 scheduler 子树由调度器自身管理，spec §11.1 明令排除。
_HOT_EXCLUDED_FIELD_NAMES = frozenset({"scheduler"})

# 中央撤回钩子：除结构性排除外全面开放，这里只扣除「不是用户设置」的运行期簿记字段。
# 若日后发现某用户字段中途生效会让脚本与游戏 UI 失步（例如脚本已进入某层副本 UI 时改层数），
# 在此加一条 path 即可撤回该字段，无需改任何任务代码，也不必改遍历逻辑。
HOT_DENY_PATHS: frozenset = frozenset({
    # 实例身份与「当前运行任务」由 manager/调度器写入，不是用户可调设置；
    # 让磁盘值中途覆盖运行值会破坏调度判断（config.py get_next 依赖 running_task）。
    # 与 config_manager.CONFIG_TASK_TRANSFER_EXCLUDED_KEYS 对这两个字段的定性一致。
    ("config_name",),
    ("running_task",),
})


@dataclass(frozen=True)
class ReloadPolicy:
    """把 canonical tuple path 分类为 HOT/WARM/COLD。

    分类优先级固定为 COLD prefix allowlist > HOT exact-path allowlist > WARM；
    未被 derive_hot_paths 收录的字段（含结构性排除项与新增未分类字段）一律 WARM。

    HOT 契约（规格 §11.1）：生产 hot_paths 由 derive_hot_paths(ConfigModel) 派生，
    收录全部 scalar/Enum/time/datetime/timedelta 单值叶子；list/dict、extra='allow'
    动态 key 子树、scheduler 子树、script.device 子树按结构性规则排除。
    直接构造 ReloadPolicy() 仍得到空 hot（供测试注入合成 Schema 使用）；
    生产默认策略取 default_reload_policy()。
    """
    hot_paths: frozenset = field(default_factory=frozenset)
    cold_prefixes: Sequence[tuple[str, ...]] = COLD_PREFIXES

    def classify(self, path: tuple[str, ...]) -> str:
        for prefix in self.cold_prefixes:
            if path[:len(prefix)] == prefix:
                return COLD
        if path in self.hot_paths:
            return HOT
        return WARM


def _is_model_class(annotation) -> bool:
    """annotation 是否为可下降的 Pydantic 子模型类（排除带 origin 的容器构造）。"""
    if get_origin(annotation) is not None or not isinstance(annotation, type):
        return False
    from pydantic import BaseModel
    return issubclass(annotation, BaseModel)


def _is_hot_leaf(annotation) -> bool:
    """判断 annotation 是否为允许中途生效的单值叶子类型。

    Pydantic v2 已把 config_base 的 Annotated 别名（Time/DateTime/TimeDelta/MultiLine）
    解析成裸 time/datetime/timedelta/str，因此这里只需判裸类型。
    """
    if get_origin(annotation) is not None:
        # list[X]/dict[K,V]/Optional[X]/Union[...] 等带 origin 的构造一律不是单值叶子
        return False
    if not isinstance(annotation, type):
        # 字符串前向引用、TypeVar 等无法静态判定的一律不收
        return False
    if _is_model_class(annotation):
        return False
    return issubclass(annotation, _HOT_LEAF_TYPES)


def derive_hot_paths(model_cls) -> frozenset:
    """从 Pydantic 模型 schema 递归派生 HOT 白名单（canonical tuple path 叶子集合）。

    收录：int/float/str/bool、Enum 子类、time/date/datetime/timedelta 单值字段。
    排除（spec §11.1 明令不支持中途生效）：
    - list/dict/set/tuple 等容器字段与 Optional/Union 构造；
    - extra='allow' 的动态 key 子树（如 FindJadeConfig、AccountConfigSelection），
      其字段集合运行期可变，无法预先枚举成 exact path；
    - 名为 scheduler 的子模型子树；
    - COLD_PREFIXES 覆盖的子树（classify 里 COLD 本就优先，此处再排一次保持白名单精简）。
    最后扣除 HOT_DENY_PATHS。
    """
    paths: set = set()

    def walk(cls, prefix: tuple[str, ...]) -> None:
        if cls.model_config.get("extra") == "allow":
            # 动态 key 子树整体不进白名单：未声明字段的路径无法预先枚举
            return
        for name, field_info in cls.model_fields.items():
            path = prefix + (name,)
            if any(path[:len(p)] == p for p in COLD_PREFIXES):
                continue
            annotation = field_info.annotation
            if _is_model_class(annotation):
                if name in _HOT_EXCLUDED_FIELD_NAMES:
                    continue
                walk(annotation, path)
                continue
            if _is_hot_leaf(annotation):
                paths.add(path)

    walk(model_cls, ())
    return frozenset(paths) - HOT_DENY_PATHS


@lru_cache(maxsize=1)
def default_reload_policy() -> ReloadPolicy:
    """生产默认策略：script.device 子树 COLD、HOT 由 ConfigModel schema 派生、其余 WARM。

    ConfigModel 在函数内延迟导入：config_model 会拉起全部任务配置模块，模块级导入
    会让 config_reload 变重并反转 config.py 的既有导入顺序（沿用 config_base
    _strict_config_validation_active 的延迟导入手法）。派生结果按进程缓存一次。
    """
    from module.config.config_model import ConfigModel
    return ReloadPolicy(hot_paths=derive_hot_paths(ConfigModel))


def coerce_path(p) -> tuple:
    """防御式把事件路径转成 tuple；str 视为单段路径，避免被 tuple() 拆成字符。"""
    if isinstance(p, (tuple, list)):
        return tuple(p)
    return (p,)
