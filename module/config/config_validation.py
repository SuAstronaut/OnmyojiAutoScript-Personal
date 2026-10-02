# This Python file uses the following encoding: utf-8
# 配置严格持久化校验：
# - ValidationProfile 允许测试注入任意 Pydantic model，不写死 ConfigModel
# - validate_persisted_config 顺序：legacy -> 动态预检/排序 -> unknown 剔除(记入 repaired_paths) -> strict model -> canonical 不变量
# - STRICT_CONFIG_VALIDATION 上下文变量关闭 ConfigBase 的 range 降级回退
import contextvars
import copy
import re
import types
from dataclasses import dataclass
from typing import Annotated, Any, Callable, Sequence, Union, get_args, get_origin

from pydantic import BaseModel, TypeAdapter, ValidationError

from tasks.Component.config_base import ConfigBase
from tasks.Utils.optional_tasks import extend
from module.config.config_model import ConfigModel

# 严格持久化校验开关：开启时 ConfigBase.__init__ 不再把 range 错误降级为默认值
STRICT_CONFIG_VALIDATION = contextvars.ContextVar("strict_config_validation", default=False)


class ConfigValidationError(ValueError):
    """严格持久化校验失败，磁盘保持不变。"""


@dataclass(frozen=True)
class DynamicPathSet:
    """动态 serializer 的路径集合注册项。

    mode:
      counted     由 count_path 控制成员数量，写入 _N 扁平 key
      contiguous  无 count，成员连续索引，至少保留一项
      single      固定只保留 _1 一项
    """
    key: str
    member_path: tuple[str, ...]
    count_path: tuple[str, ...] | None = None
    mode: str = "counted"


@dataclass(frozen=True)
class DynamicFieldSet:
    """声明固定对象下允许按模式扩展的同类型字段。"""
    path: tuple[str, ...]
    key_pattern: str
    value_type: type


@dataclass
class ValidationProfile:
    model_type: type[BaseModel]
    legacy_migrations: Sequence[tuple[tuple[str, ...], Callable[[dict], None]]] = ()
    dynamic_path_sets: Sequence[DynamicPathSet] = ()
    dynamic_field_sets: Sequence[DynamicFieldSet] = ()
    # 自动修复回收站：调用方可传入空列表，校验过程中被剔除的未知键路径会填充进来
    # （结构演化后磁盘残留的失效字段自动对齐；None 表示调用方不关心，剔除照常发生）
    # 注意 dataclass 由 frozen 改为可变，仅为挂这个运行期回收字段，无 hash 依赖
    repaired_paths: list | None = None


def _migrate_master_battle_mode(raw: dict) -> None:
    """master_battle_mode 旧字段迁移：normal_battle 映射两个 exit-after-prepare 为 False，其余为 True。

    legacy key 只为缺失的新字段补值；已有新字段优先，保证迁移幂等且不覆盖新配置。
    """
    task = raw.get("master_disciple")
    if not isinstance(task, dict):
        return
    section = task.get("master_disciple_config")
    if not isinstance(section, dict):
        return
    legacy = section.pop("master_battle_mode", None)
    if legacy is None:
        return
    mapped = legacy != "normal_battle"
    section.setdefault("master_coin_exit_after_prepare", mapped)
    section.setdefault("master_exp_exit_after_prepare", mapped)


def _migrate_orochi_team_fields(raw: dict) -> None:
    """orochi_config 旧组队字段迁移到 team_config，并从旧位置删除。

    与 Orochi.migrate_legacy_team_fields 保持同一套补值规则；严格校验必须在
    Pydantic before-validator 之前移除旧字段，否则 unknown 检查会先隔离配置。
    """
    task = raw.get("orochi")
    if not isinstance(task, dict):
        return
    team = task.get("team_config")
    if isinstance(team, dict):
        # 兼容旧版 enable_team 布尔开关：True 转组队、False 转单人
        if "enable_team" in team:
            team.setdefault("team_mode", "team" if bool(team.pop("enable_team")) else "alone")
    section = task.get("orochi_config")
    if not isinstance(section, dict):
        return
    if not isinstance(team, dict):
        team = {}
        task["team_config"] = team
    legacy_status = section.get("user_status")
    if legacy_status is not None:
        team.setdefault("team_mode", "team" if legacy_status in ("leader", "member") else "alone")
    for key in ("leader_instance", "epoch", "total_limit_time", "total_limit_count"):
        if key in section:
            team.setdefault(key, section.pop(key))


def _migrate_multi_tasks(raw: dict) -> None:
    """三个旧多账号任务合并进 multi_tasks。

    MultiAccExp / MultiAccountSignIn / MultiActivityShikigami 的参数分属三组、
    互不冲突，全部搬入 multi_tasks；scheduler 一律不继承（enable 保持模型默认
    False），避免升级后自动跑一个用户从未选过的 (子任务, 来源) 组合。
    ExtendedAccountInfo 的账号级开关与 total_* 全局开关随旧节点一起丢弃：
    经验妖怪的加成开关改为直接沿用 experience_youkai 自身的配置。

    本函数在 LEGACY_ALIAS_MIGRATIONS 中按三个旧任务根节点注册 3 条，
    normalize_legacy_config 会调用它 3 次，因此幂等是硬要求。

    缺 multi_tasks 节点时必须建出完整节点：严格校验会在
    _validate_canonical_dynamic_payloads 报 "changed members during canonicalization"
    —— 因为 _validate_dynamic_payloads 对缺失父节点算出 expected={}，而
    model_validate 填默认值后 serializer 会吐出 sup_account_list_1，两者不一致。
    """
    legacy_keys = ("multi_acc_exp", "multi_account_sign_in", "multi_activity_shikigami")
    if "multi_tasks" in raw and not any(key in raw for key in legacy_keys):
        # 已迁移过（或本来就是新形状的 template）：直接返回，保证幂等。
        # 幂等的判据必须是「multi_tasks 已存在且旧节点已清空」，不能只看旧节点：
        # 只看旧节点会让第 2、3 次调用把已搬入的账号表重置成一条默认空条目。
        return

    from tasks.Component.SwitchAccount.switch_account_config import AccountInfo

    target = raw.setdefault("multi_tasks", {})
    section = target.setdefault("multi_tasks_config", {})

    # ---- MultiAccExp：账号表 ----
    # 只搬 character 非空的条目：validator_all 会丢弃空条目再把默认值补到末尾，
    # 老配置里的「有效、空、有效」夹心会让 canonical 后索引 2 的 payload 变化，
    # 撞上 _validate_canonical_dynamic_payloads 的一致性检查
    entries = []
    exp = raw.pop("multi_acc_exp", None)
    if isinstance(exp, dict):
        allowed = set(AccountInfo.model_fields)
        prefix = "sup_account_list_"
        member_keys = sorted(
            (key for key in exp
             if key.startswith(prefix) and key[len(prefix):].isdigit()),
            key=lambda key: int(key[len(prefix):]),
        )
        for key in member_keys:
            payload = exp[key]
            if not isinstance(payload, dict) or not payload.get("character"):
                continue
            entries.append({k: v for k, v in payload.items() if k in allowed})

    # ---- MultiAccountSignIn：勾选实例 ----
    sign_in = raw.pop("multi_account_sign_in", None)
    if isinstance(sign_in, dict):
        selection = sign_in.get("account_config_selection")
        if isinstance(selection, dict):
            target.setdefault("account_config_selection", dict(selection))

    # ---- MultiActivityShikigami：角色名串 ----
    shikigami = raw.pop("multi_activity_shikigami", None)
    if isinstance(shikigami, dict):
        old_section = shikigami.get("multi_activity_shikigami_config")
        if isinstance(old_section, dict) and "account_characters" in old_section:
            section.setdefault("account_characters", old_section["account_characters"])

    # 只要创建了 multi_tasks 就必须满足 counted 不变量：索引 1..N 连续、
    # sup_account_count == N、且 N >= 1（模型 ge=1 不允许 0）。
    # 这里用直接赋值而非 setdefault，保证不变量精确成立。
    if not entries:
        entries.append(AccountInfo().model_dump(mode="json"))
    for index, payload in enumerate(entries, start=1):
        target[f"sup_account_list_{index}"] = payload
    section["sup_account_count"] = len(entries)


def _migrate_drop_desktop_login_wait(raw: dict) -> None:
    """丢弃已废弃的 script.device.desktop_login_wait。

    它原本是「启动客户端后等 MPay 登录弹窗的轮询上限」。等弹窗与进游戏已移交
    Restart 的 app_handle_login（登录循环每轮都复查弹窗），启动侧
    不再等待，配置项因此失去作用。字段从模型删除后磁盘残留会被 _reject_unknown_keys
    判为非法整份配置隔离，所以必须在严格校验前 pop 掉。
    """
    script = raw.get("script")
    if not isinstance(script, dict):
        return
    device = script.get("device")
    if isinstance(device, dict):
        device.pop("desktop_login_wait", None)


def _migrate_drop_multi_daily_need_login(raw: dict) -> None:
    """丢弃已废弃的 multi_daily_alt_acc_config.need_login / need_login_time。

    账号完成判定已完全由进度文件驱动，字段从模型删除后磁盘残留会被
    _reject_unknown_keys 判为非法整份配置隔离，所以必须在严格校验前 pop 掉。
    """
    task = raw.get("multi_daily_alt_acc")
    if not isinstance(task, dict):
        return
    section = task.get("multi_daily_alt_acc_config")
    if isinstance(section, dict):
        section.pop("need_login", None)
        section.pop("need_login_time", None)


def _migrate_humanize_level(raw: dict) -> None:
    """拟人化档位收敛（2026-09-13）：已删除的 light/heavy 映射到 medium。

    档位从四档收敛为 off/medium 两档，旧值留在磁盘上会被 pydantic 枚举校验
    拒绝 → ConfigValidationError → 整份配置隔离、实例停止。迁移只改写被删掉的
    两个值：off/medium 原样保留（幂等），字段缺失时无操作（模型填默认 off），
    其余任意值故意不动——那是真正的配置损坏，必须继续 fail-closed，不能被
    迁移悄悄掩盖成 medium。
    """
    script = raw.get("script")
    if not isinstance(script, dict):
        return
    device = script.get("device")
    if not isinstance(device, dict):
        return
    if device.get("humanize_level") in ("light", "heavy"):
        device["humanize_level"] = "medium"


LEGACY_ALIAS_MIGRATIONS: Sequence[tuple[tuple[str, ...], Callable[[dict], None]]] = (
    (("master_disciple", "master_disciple_config", "master_battle_mode"), _migrate_master_battle_mode),
    (("orochi", "orochi_config", "leader_instance"), _migrate_orochi_team_fields),
    (("orochi", "orochi_config", "epoch"), _migrate_orochi_team_fields),
    (("orochi", "orochi_config", "total_limit_time"), _migrate_orochi_team_fields),
    (("orochi", "orochi_config", "total_limit_count"), _migrate_orochi_team_fields),
    # 三个旧多账号任务合并进 multi_tasks：账号表的源键是动态的 sup_account_list_N，
    # 写不出静态字段路径，因此用旧任务根节点作为源路径。
    (("multi_acc_exp",), _migrate_multi_tasks),
    (("multi_account_sign_in",), _migrate_multi_tasks),
    (("multi_activity_shikigami",), _migrate_multi_tasks),
    # 纯删除：等登录弹窗已移交 Restart 登录流程，该配置项不再有读取方
    (("script", "device", "desktop_login_wait"), _migrate_drop_desktop_login_wait),
    # 纯删除：多账号完成判定已由进度文件驱动，旧字段随配置残留须 pop 掉
    (("multi_daily_alt_acc", "multi_daily_alt_acc_config", "need_login"), _migrate_drop_multi_daily_need_login),
    (("multi_daily_alt_acc", "multi_daily_alt_acc_config", "need_login_time"), _migrate_drop_multi_daily_need_login),
    # 档位收敛：light/heavy 已从枚举删除，旧值必须改写成 medium 才通得过校验
    (("script", "device", "humanize_level"), _migrate_humanize_level),
)

# 本机模型所需的补节点逻辑与模型同步接入，避免默认值和磁盘成员不一致。
LEGACY_ALIAS_MIGRATIONS = extend('migrations', LEGACY_ALIAS_MIGRATIONS)


def legacy_source_paths() -> set[tuple[str, ...]]:
    """返回所有 legacy alias 的源路径，供 AST 门禁校验 before-validator 迁移均已登记。"""
    return {path for path, _ in LEGACY_ALIAS_MIGRATIONS}


DYNAMIC_PATH_SET_REGISTRY: Sequence[DynamicPathSet] = (
    DynamicPathSet("find_jade.invite_info_list", ("find_jade", "invite_info_list"),
                   ("find_jade", "find_jade_config", "invite_info_count")),
    DynamicPathSet("find_jade.sup_account_list", ("find_jade", "sup_account_list"),
                   ("find_jade", "find_jade_config", "sup_account_count")),
    DynamicPathSet("multi_daily_alt_acc.sup_account_list", ("multi_daily_alt_acc", "sup_account_list"),
                   ("multi_daily_alt_acc", "multi_daily_alt_acc_config", "sup_account_count")),
    DynamicPathSet("multi_tasks.sup_account_list", ("multi_tasks", "sup_account_list"),
                   ("multi_tasks", "multi_tasks_config", "sup_account_count")),
    DynamicPathSet("meta_demon.md_strategies", ("meta_demon", "md_strategies"),
                   ("meta_demon", "meta_demon_config", "md_strategy_count")),
    DynamicPathSet("master_disciple.disciple_account_list", ("master_disciple", "disciple_account_list"),
                   mode="contiguous"),
    DynamicPathSet("bondling_fairyland.switch_account_list", ("bondling_fairyland", "switch_account_list"),
                   mode="single"),
    DynamicPathSet("abyss_shadows.switch_account_list", ("abyss_shadows", "switch_account_list"),
                   mode="single"),
)

# 扩展使用相同的注册项类型，使账号表继续经过严格持久化校验。
DYNAMIC_PATH_SET_REGISTRY = extend('dynamic_paths', DYNAMIC_PATH_SET_REGISTRY, DynamicPathSet)


DYNAMIC_FIELD_SET_REGISTRY: Sequence[DynamicFieldSet] = (
    DynamicFieldSet(
        ("multi_tasks", "account_config_selection"),
        r"config_[0-9a-f]{16}",
        bool,
    ),
)


DEFAULT_CONFIG_PROFILE = ValidationProfile(
    model_type=ConfigModel,
    legacy_migrations=LEGACY_ALIAS_MIGRATIONS,
    dynamic_path_sets=DYNAMIC_PATH_SET_REGISTRY,
    dynamic_field_sets=DYNAMIC_FIELD_SET_REGISTRY,
)


def normalize_legacy_config(
    raw: dict,
    config_name: str,
    profile: ValidationProfile = None,
) -> dict:
    """返回深拷贝：写入 config_name 并执行 legacy alias 迁移，不修改入参。"""
    profile = profile or DEFAULT_CONFIG_PROFILE
    normalized = copy.deepcopy(raw)
    if not isinstance(normalized, dict):
        raise ConfigValidationError("config root must be an object")
    for _path, migrate in profile.legacy_migrations:
        migrate(normalized)
    normalized["config_name"] = config_name
    return normalized


def _unwrap_annotation(annotation: Any) -> tuple[Any, ...]:
    """展开 Annotated/Union/Optional，返回所有可能的实际类型。"""
    origin = get_origin(annotation)
    if origin is Annotated:
        return _unwrap_annotation(get_args(annotation)[0])
    if origin in (Union, types.UnionType):
        result: list[Any] = []
        for item in get_args(annotation):
            result.extend(_unwrap_annotation(item))
        return tuple(result)
    return (annotation,)


def _model_types(annotation: Any) -> tuple[type[BaseModel], ...]:
    """从任意常见 Pydantic 注解中提取嵌套 BaseModel 类型。"""
    models: list[type[BaseModel]] = []
    for item in _unwrap_annotation(annotation):
        if isinstance(item, type) and issubclass(item, BaseModel):
            models.append(item)
        else:
            origin = get_origin(item)
            if origin is list:
                for nested in _model_types(get_args(item)[0] if get_args(item) else Any):
                    models.append(nested)
    return tuple(models)


def _list_item_models(annotation: Any) -> tuple[type[BaseModel], ...]:
    models: list[type[BaseModel]] = []
    for item in _unwrap_annotation(annotation):
        if get_origin(item) is not list:
            continue
        args = get_args(item)
        if args:
            models.extend(_model_types(args[0]))
    return tuple(dict.fromkeys(models))


def _list_item_model(annotation: Any) -> type[BaseModel] | None:
    """动态 registry 必须映射到唯一 item model。"""
    models = _list_item_models(annotation)
    return models[0] if len(models) == 1 else None


def _dynamic_entry_for_key(
    path: tuple[str, ...],
    key: str,
    profile: ValidationProfile,
) -> DynamicPathSet | None:
    """只有显式 registry 声明的 member_N key 才能绕过 unknown 拒绝。"""
    for entry in profile.dynamic_path_sets:
        if path != entry.member_path[:-1]:
            continue
        field = entry.member_path[-1]
        prefix = field + "_"
        if not key.startswith(prefix):
            continue
        suffix = key[len(prefix):]
        if suffix.isdigit():
            return entry
    return None


def _dynamic_field_for_key(
    path: tuple[str, ...],
    key: str,
    profile: ValidationProfile,
) -> DynamicFieldSet | None:
    """返回覆盖当前未知字段的受约束动态字段注册项。"""
    for entry in profile.dynamic_field_sets:
        if path == entry.path and re.fullmatch(entry.key_pattern, key):
            return entry
    return None


def _selected_model_type(field_info, data: dict, path: tuple[str, ...]) -> type[BaseModel] | None:
    """用字段完整注解/元数据执行一次独立验证，获取 Pydantic 实际选择的 Union 分支。"""
    annotation = field_info.rebuild_annotation()
    # list[Union[...]] 必须连同列表字段的完整注解一起验证，才能保留 item 上的
    # discriminator/union_mode 等元数据，并与父模型实际选择的分支完全一致。
    candidate = [copy.deepcopy(data)] if _list_item_models(annotation) else copy.deepcopy(data)
    try:
        selected = TypeAdapter(annotation).validate_python(candidate)
    except (ValidationError, TypeError, ValueError) as exc:
        raise ConfigValidationError(f"invalid field {'/'.join(path)}: {exc}") from exc
    if isinstance(selected, list) and len(selected) == 1:
        selected = selected[0]
    return type(selected) if isinstance(selected, BaseModel) else None


def _reject_unknown_keys(
    data: Any,
    model_type: type[BaseModel],
    profile: ValidationProfile,
    path: tuple[str, ...] = (),
) -> list[tuple[str, ...]]:
    """递归剔除除显式 alias 与动态 registry 之外的未知字段，返回被剔除的键路径。

    结构演化（删字段/改结构）后磁盘残留旧键是可预期演化：就地从 data 删除并
    收集路径，由调用方决定备份与告警；类型/形状错误仍然抛错。
    模型均为 pydantic 默认 extra='ignore'，未知键进不了 model，但显式剔除保证
    上游（父任务 before-validator 等）消费的 normalized 数据与模型一致。
    """
    removed: list[tuple[str, ...]] = []
    if not isinstance(data, dict):
        return removed
    fields = model_type.model_fields
    for key in list(data.keys()):
        value = data[key]
        full = path + (key,)
        if key == "config_name" and not path:
            continue
        field_set = _dynamic_field_for_key(path, key, profile)
        if field_set is not None:
            try:
                TypeAdapter(field_set.value_type).validate_python(value, strict=True)
            except (ValidationError, TypeError, ValueError) as exc:
                raise ConfigValidationError(f"invalid dynamic field {'/'.join(full)}: {exc}") from exc
        if key not in fields:
            entry = _dynamic_entry_for_key(path, key, profile)
            if entry is not None:
                item_model = _list_item_model(fields[entry.member_path[-1]].annotation)
                if item_model is None:
                    raise ConfigValidationError(f"invalid dynamic registry {'/'.join(full)}")
                removed.extend(_reject_unknown_keys(value, item_model, profile, full))
                continue
            if field_set is None:
                # 未知键：结构演化残留，剔除整个子树（值可以是任意形状）
                del data[key]
                removed.append(full)
                continue
            continue
        annotation = fields[key].annotation
        if isinstance(value, list) and _list_item_models(annotation):
            candidates = _list_item_models(annotation)
            if len(candidates) == 1:
                for index, item in enumerate(value):
                    if isinstance(item, dict):
                        removed.extend(
                            _reject_unknown_keys(item, candidates[0], profile, full + (str(index),))
                        )
        elif isinstance(value, dict):
            model_candidates = _model_types(annotation)
            if len(model_candidates) == 1:
                removed.extend(_reject_unknown_keys(value, model_candidates[0], profile, full))
    return removed


def _reject_unknown_by_instance(
    data: Any,
    instance: Any,
    profile: ValidationProfile,
    path: tuple[str, ...] = (),
) -> list[tuple[str, ...]]:
    """按正式 model_validate 产生的实例类型检查 Union 最终分支 unknown，剔除并返回路径。

    模型均为 pydantic 默认 extra='ignore'，canonical 不含未知键；本层是防御性
    二次检查（Union 实际选支），剔除只影响 normalized 数据与 repaired_paths 记录，
    无需重跑 model_validate。
    """
    removed: list[tuple[str, ...]] = []
    if not isinstance(data, dict) or not isinstance(instance, BaseModel):
        return removed
    fields = type(instance).model_fields
    for key in list(data.keys()):
        value = data[key]
        full = path + (key,)
        if key == "config_name" and not path:
            continue
        if key not in fields:
            if _dynamic_field_for_key(path, key, profile) is not None:
                continue
            entry = _dynamic_entry_for_key(path, key, profile)
            if entry is not None:
                continue
            # Union 分支下的未知键：同样是结构演化残留，剔除
            del data[key]
            removed.append(full)
            continue
        child = getattr(instance, key, None)
        if isinstance(value, dict) and isinstance(child, BaseModel):
            removed.extend(_reject_unknown_by_instance(value, child, profile, full))
        elif isinstance(value, list) and isinstance(child, list):
            for index, item in enumerate(value):
                if index < len(child) and isinstance(item, dict):
                    removed.extend(
                        _reject_unknown_by_instance(item, child[index], profile, full + (str(index),))
                    )
    return removed


def _get_node(data: Any, path: tuple[str, ...]) -> Any:
    """读取 path 值；缺失返回 None。"""
    node: Any = data
    for key in path:
        if not isinstance(node, dict) or key not in node:
            return None
        node = node[key]
    return node


def _validate_dynamic_path_sets(data: dict, profile: ValidationProfile) -> None:
    """按注册 mode 校验动态列表的精确 cardinality。

    counted 要求索引恰为 1..count；contiguous 要求从 1 连续且至少一项；
    single 只允许 _1。首次 migration 必须先归一化旧形状，再调用本严格入口。
    """
    for entry in profile.dynamic_path_sets:
        parent = entry.member_path[:-1]
        field = entry.member_path[-1]
        node = _get_node(data, parent)
        if not isinstance(node, dict):
            continue
        # 持久化 canonical 只允许 field_N；逻辑 list 与扁平成员混用会被父 validator 重组。
        if field in node:
            raise ConfigValidationError(
                f"dynamic list {'/'.join(entry.member_path)} must use flattened members"
            )
        prefix = field + "_"
        member_suffixes = [
            key[len(prefix):] for key in node
            if key.startswith(prefix) and key[len(prefix):].isdigit()
        ]
        if any(not suffix or int(suffix) < 1 or suffix != str(int(suffix)) for suffix in member_suffixes):
            raise ConfigValidationError(
                f"dynamic list {'/'.join(entry.member_path)} has non-canonical member index"
            )
        indexes = sorted(int(suffix) for suffix in member_suffixes)
        if entry.mode == "counted" and entry.count_path is not None:
            count = _get_node(data, entry.count_path)
            if type(count) is not int or count < 0:
                raise ConfigValidationError(
                    f"dynamic count {'/'.join(entry.count_path)} must be a non-negative integer"
                )
            if indexes != list(range(1, len(indexes) + 1)) or len(indexes) != count:
                raise ConfigValidationError(
                    f"dynamic list {'/'.join(entry.member_path)} indexes {indexes} "
                    f"do not match 1..{count}"
                )
        elif entry.mode == "contiguous":
            if not indexes or indexes != list(range(1, len(indexes) + 1)):
                raise ConfigValidationError(
                    f"dynamic list {'/'.join(entry.member_path)} must have contiguous "
                    f"indexes starting at 1"
                )
        elif entry.mode == "single":
            if indexes != [1]:
                raise ConfigValidationError(
                    f"dynamic list {'/'.join(entry.member_path)} must keep exactly _1"
                )


def _sort_dynamic_member_keys(data: dict, profile: ValidationProfile) -> None:
    """按数字索引重排扁平动态 key，禁止父 validator 依赖 JSON 插入顺序。"""
    for entry in profile.dynamic_path_sets:
        parent = entry.member_path[:-1]
        field = entry.member_path[-1]
        node = _get_node(data, parent)
        if not isinstance(node, dict):
            continue
        prefix = field + "_"
        members = [
            (int(key[len(prefix):]), key, node[key])
            for key in list(node)
            if key.startswith(prefix) and key[len(prefix):].isdigit()
        ]
        if len(members) < 2:
            continue
        for _index, key, _payload in members:
            del node[key]
        for _index, key, payload in sorted(members):
            node[key] = payload


def _validate_dynamic_payloads(
    data: dict,
    profile: ValidationProfile,
) -> dict[str, dict[str, Any]]:
    """在父任务 before-validator 前验证 payload，并保存索引到 canonical payload 的映射。"""
    expected: dict[str, dict[str, Any]] = {}
    for entry in profile.dynamic_path_sets:
        expected_members: dict[str, Any] = {}
        expected[entry.key] = expected_members
        parent = entry.member_path[:-1]
        field = entry.member_path[-1]
        node = _get_node(data, parent)
        if not isinstance(node, dict):
            continue
        task_model = profile.model_type
        for part in parent:
            candidates = _model_types(task_model.model_fields[part].annotation)
            if len(candidates) != 1:
                raise ConfigValidationError(f"dynamic registry {entry.key} has ambiguous task model")
            task_model = candidates[0]
        item_model = _list_item_model(task_model.model_fields[field].annotation)
        if item_model is None:
            raise ConfigValidationError(f"dynamic registry {entry.key} has invalid item model")
        prefix = field + "_"
        for key, payload in node.items():
            if not key.startswith(prefix):
                continue
            suffix = key[len(prefix):]
            if not suffix.isdigit():
                continue
            if not isinstance(payload, dict):
                raise ConfigValidationError(
                    f"dynamic member {'/'.join(parent + (key,))} must be an object"
                )
            member_path = parent + (key,)
            try:
                # unknown 剔除先于父任务 before-validator 消费 payload（语义反转：
                # 残留键剔除，损坏字段仍靠下面 model_validate 抛错兜底）。
                member_removed = _reject_unknown_keys(payload, item_model, profile, member_path)
                if member_removed:
                    if profile.repaired_paths is None:
                        profile.repaired_paths = []
                    profile.repaired_paths.extend(member_removed)
                item = item_model.model_validate(copy.deepcopy(payload))
                # 配置允许逐字段填写；是否具备执行条件不能决定账号草稿能否保存。
                # 保留类型与范围校验，并在序列化后核对成员内容和索引未被改写。
                expected_members[key] = item.model_dump(mode="json")
            except ConfigValidationError:
                raise
            except (ValidationError, TypeError, ValueError, AttributeError) as exc:
                raise ConfigValidationError(
                    f"invalid dynamic member {'/'.join(member_path)}: {exc}"
                ) from exc
    return expected


def _validate_canonical_dynamic_payloads(
    canonical: dict,
    expected: dict[str, dict[str, Any]],
    profile: ValidationProfile,
) -> None:
    """确认父 validator/serializer 未改变动态索引集合或索引对应 payload。"""
    _validate_dynamic_path_sets(canonical, profile)
    for entry in profile.dynamic_path_sets:
        parent = entry.member_path[:-1]
        field = entry.member_path[-1]
        prefix = field + "_"
        node = _get_node(canonical, parent)
        actual = {}
        if isinstance(node, dict):
            actual = {
                key: value for key, value in node.items()
                if key.startswith(prefix) and key[len(prefix):].isdigit()
            }
        expected_members = expected.get(entry.key, {})
        if set(actual) != set(expected_members):
            raise ConfigValidationError(
                f"dynamic list {'/'.join(entry.member_path)} changed members during canonicalization"
            )
        for key, payload in expected_members.items():
            if actual[key] != payload:
                raise ConfigValidationError(
                    f"dynamic member {'/'.join(parent + (key,))} changed during canonicalization"
                )


def validate_persisted_config(
    raw: dict,
    config_name: str,
    profile: ValidationProfile = None,
) -> tuple[BaseModel, dict]:
    """严格持久化校验：迁移 legacy -> dynamic cardinality/payload -> unknown 剔除 -> model -> canonical。

    unknown field 不再 fail-closed：结构演化后磁盘残留的失效字段被就地剔除并
    记入 profile.repaired_paths（调用方传入空列表即可收到），缺失字段由
    model_validate 自动补默认值。其余失败（类型/cardinality/payload 损坏）仍抛错。
    """
    profile = profile or DEFAULT_CONFIG_PROFILE
    normalized = normalize_legacy_config(raw, config_name, profile)
    token = STRICT_CONFIG_VALIDATION.set(True)
    try:
        # 动态成员必须在任何父模型 before-validator 运行前独立拒绝损坏 payload
        _validate_dynamic_path_sets(normalized, profile)
        expected_dynamic = _validate_dynamic_payloads(normalized, profile)
        # 先稳定 key 顺序，再允许任何父任务 before-validator 消费扁平成员。
        _sort_dynamic_member_keys(normalized, profile)
        removed = _reject_unknown_keys(normalized, profile.model_type, profile)
        model = profile.model_type.model_validate(normalized)
        # Union 的真实分支只由这次正式校验决定；随后按实例类型精确剔除 unknown，
        # 避免 TypeAdapter 预选导致 validator 提前或重复执行。
        removed.extend(_reject_unknown_by_instance(normalized, model, profile))
        canonical = model.model_dump(mode="json")
        _validate_canonical_dynamic_payloads(canonical, expected_dynamic, profile)
        # 回收剔除结果给调用方（备份与告警由调用方决定；剔除本身无条件发生）
        if removed:
            if profile.repaired_paths is None:
                profile.repaired_paths = []
            profile.repaired_paths.extend(removed)
    except ConfigValidationError:
        raise
    except (ValidationError, TypeError, ValueError, AttributeError, KeyError, IndexError) as exc:
        # 旧任务 before-validator 常假定父节点是 dict，或直接索引嵌套结构；
        # 边界统一包装其形状异常，避免 KeyError/IndexError 泄漏到事务层。
        raise ConfigValidationError(str(exc)) from exc
    finally:
        STRICT_CONFIG_VALIDATION.reset(token)
    return model, canonical
