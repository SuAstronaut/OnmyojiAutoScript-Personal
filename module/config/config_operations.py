# This Python file uses the following encoding: utf-8
# 配置 canonical 操作基础库：
# - tuple path 只遍历 dict，缺失用单例 MISSING 表示，避免与 None 混淆
# - 操作类型不可变（frozen dataclass）
# - diff_trees/normalize_operations/merge_operations 提供三方合并与冲突指纹
# - 传入动态 path-set registry 时，count/成员改动折叠为一个原子 ReplacePathSet
import copy
from dataclasses import dataclass, field
from typing import Any, Sequence, Union


class _Missing:
    """缺失路径的哨兵单例，与显式 None 严格区分。"""
    __slots__ = ()

    def __repr__(self) -> str:
        return "MISSING"

    def __copy__(self):
        return self

    def __deepcopy__(self, memo):
        return self


MISSING = _Missing()


@dataclass(frozen=True)
class SetPath:
    """把 path 上的值设为 value。"""
    path: tuple[str, ...]
    value: Any

    def __post_init__(self):
        object.__setattr__(self, "value", copy.deepcopy(self.value))


@dataclass(frozen=True)
class DeletePath:
    """删除 path，expected 是删除前该路径应保持的 base 值（CAS 式）。"""
    path: tuple[str, ...]
    expected: Any

    def __post_init__(self):
        object.__setattr__(self, "expected", copy.deepcopy(self.expected))


@dataclass(frozen=True)
class ReplaceSubtree:
    """整体替换 path 子树；expected 是替换前该子树应保持的 base 值。"""
    path: tuple[str, ...]
    value: Any
    expected: Any

    def __post_init__(self):
        object.__setattr__(self, "value", copy.deepcopy(self.value))
        object.__setattr__(self, "expected", copy.deepcopy(self.expected))


@dataclass(frozen=True)
class ReplacePathSet:
    """原子替换一组声明路径（动态 serializer 的 count/成员集合）。"""
    registry_key: str
    values: dict[tuple[str, ...], Any]
    expected: dict[tuple[str, ...], Any]

    def __post_init__(self):
        object.__setattr__(self, "values", copy.deepcopy(self.values))
        object.__setattr__(self, "expected", copy.deepcopy(self.expected))


@dataclass(frozen=True)
class BlockedChange:
    """三方合并冲突指纹：blocked_local_value 是 local 想写入的值，observed_disk_value 是磁盘现值。"""
    path: tuple[str, ...]
    operation: str
    blocked_local_value: Any
    observed_disk_value: Any

    def __post_init__(self):
        object.__setattr__(self, "blocked_local_value", copy.deepcopy(self.blocked_local_value))
        object.__setattr__(self, "observed_disk_value", copy.deepcopy(self.observed_disk_value))


@dataclass
class MergeResult:
    value: dict
    applied_paths: list[tuple[str, ...]] = field(default_factory=list)
    already_equal_paths: list[tuple[str, ...]] = field(default_factory=list)
    conflicted_paths: list[tuple[str, ...]] = field(default_factory=list)
    blocked: list[BlockedChange] = field(default_factory=list)
    changed: bool = False


Operation = Union[SetPath, DeletePath, ReplaceSubtree, ReplacePathSet]


def _eq(a: Any, b: Any) -> bool:
    """MISSING 只与 MISSING 相等；其余按值比较（dict/list 递归）。"""
    if a is MISSING or b is MISSING:
        return a is b
    return a == b


def get_path(tree: dict, path: tuple[str, ...]) -> Any:
    """读取 path 值；任意一层缺失返回 MISSING，不抛异常。"""
    node: Any = tree
    for key in path:
        if not isinstance(node, dict) or key not in node:
            return MISSING
        node = node[key]
    return node


def _get_node(tree: dict, path: tuple[str, ...]) -> Any:
    """读取 path 值；缺失返回 None（供 registry 查找使用，与 MISSING 语义不同）。"""
    node: Any = tree
    for key in path:
        if not isinstance(node, dict) or key not in node:
            return None
        node = node[key]
    return node


def set_path(tree: dict, path: tuple[str, ...], value: Any) -> dict:
    """返回把 path 设为 value 的深拷贝，不修改入参。"""
    result = copy.deepcopy(tree)
    node: dict = result
    for key in path[:-1]:
        if not isinstance(node.get(key), dict):
            node[key] = {}
        node = node[key]
    node[path[-1]] = copy.deepcopy(value)
    return result


def delete_path(tree: dict, path: tuple[str, ...], expected: Any = MISSING) -> dict:
    """返回删除 path 的深拷贝；当前值不等于 expected 或路径缺失时原样返回。"""
    result = copy.deepcopy(tree)
    node: Any = result
    for key in path[:-1]:
        if not isinstance(node, dict) or key not in node:
            return result
        node = node[key]
    if isinstance(node, dict) and path[-1] in node:
        if _eq(node[path[-1]], expected) or expected is MISSING:
            del node[path[-1]]
    return result


def _diff_leaf_ops(base: dict, local: dict, prefix: tuple[str, ...] = ()) -> list[Operation]:
    """把 base→local 的差异编码为叶子操作；空 dict 仍使用单个 SetPath 表达。"""
    ops: list[Operation] = []
    base_keys = set(base) if isinstance(base, dict) else set()
    local_keys = set(local) if isinstance(local, dict) else set()
    for key in sorted(local_keys | base_keys):
        path = prefix + (key,)
        b = base[key] if isinstance(base, dict) and key in base else MISSING
        l = local[key] if isinstance(local, dict) and key in local else MISSING
        if l is MISSING:
            # local 删除了该 key：删除前必须仍等于 base 值
            ops.append(DeletePath(path, expected=copy.deepcopy(b)))
        elif b is MISSING:
            if isinstance(l, dict) and l:
                # 新增非空 dict 必须递归为叶子操作，才能合并磁盘并发新增的其他叶子。
                ops.extend(_diff_leaf_ops({}, l, path))
            else:
                ops.append(SetPath(path, copy.deepcopy(l)))
        elif isinstance(b, dict) and isinstance(l, dict):
            ops.extend(_diff_leaf_ops(b, l, path))
        elif not _eq(b, l):
            ops.append(SetPath(path, copy.deepcopy(l)))
    return ops


def _entry_covering_path(path: Any, dynamic_path_sets: Sequence) -> Any:
    """返回覆盖该 path 的动态 path-set 注册项。

    path 等于 count 路径，或以成员扁平 key（field_N）为前缀（可深入成员子字段）时命中。
    """
    if path is None:
        return None
    for entry in dynamic_path_sets:
        if entry.count_path is not None and path == entry.count_path:
            return entry
        parent = entry.member_path[:-1]
        field = entry.member_path[-1]
        if len(path) > len(parent) and path[:len(parent)] == parent:
            member_key = path[len(parent)]
            # 先确认字段前缀，避免从不相关 key 的中间位置截取出伪索引。
            if not member_key.startswith(field + "_"):
                continue
            index = member_key[len(field) + 1:]
            if index.isdigit() and int(index) >= 1 and index == str(int(index)):
                return entry
    return None


def _member_paths_for(entry, *trees: dict) -> set[tuple[str, ...]]:
    """收集多个 canonical tree 中出现的全部扁平成员 key 路径（list_1, list_2, ...）。"""
    parent = entry.member_path[:-1]
    field = entry.member_path[-1]
    prefix = field + "_"
    paths: set[tuple[str, ...]] = set()
    for tree in trees:
        node = _get_node(tree, parent)
        if not isinstance(node, dict):
            continue
        for key in node:
            if not key.startswith(prefix):
                continue
            index = key[len(prefix):]
            if index.isdigit() and int(index) >= 1 and index == str(int(index)):
                paths.add(parent + (key,))
    return paths


def _fold_dynamic_path_sets(
    ops: list[Operation],
    base: dict,
    local: dict,
    dynamic_path_sets: Sequence,
    observed_disk: dict | None = None,
) -> list[Operation]:
    """把动态列表的 count/成员改动折叠为一个原子 ReplacePathSet。

    任一成员或 count 变化都会生成覆盖整个逻辑列表（全部成员 + count）的 path-set，
    避免逐叶子应用导致撕裂写入。
    """
    if not dynamic_path_sets:
        return ops
    covered: set[int] = set()
    folded: list[Operation] = []
    for idx, op in enumerate(ops):
        if idx in covered:
            continue
        entry = _entry_covering_path(getattr(op, "path", None), dynamic_path_sets)
        if entry is None:
            folded.append(op)
            continue
        member_keys = _member_paths_for(entry, base, local, observed_disk or {})
        count_key = entry.count_path
        all_paths = sorted(member_keys | ({count_key} if count_key else set()))
        if not all_paths:
            folded.append(op)
            continue
        values = {p: copy.deepcopy(get_path(local, p)) for p in all_paths}
        expected = {p: copy.deepcopy(get_path(base, p)) for p in all_paths}
        folded.append(ReplacePathSet(entry.key, values, expected))
        # 同一注册项的所有叶子 op 都被该 path-set 吸收
        for j, other in enumerate(ops):
            if j == idx or j in covered:
                continue
            if _entry_covering_path(getattr(other, "path", None), dynamic_path_sets) is entry:
                covered.add(j)
    return folded


def diff_trees(
    base: dict,
    local: dict,
    prefix: tuple[str, ...] = (),
    dynamic_path_sets: Sequence = (),
    observed_disk: dict | None = None,
) -> list[Operation]:
    """把 base→local 差异编码为操作列表；顶层调用时折叠动态 path-set。

    merge 阶段传入 observed_disk，确保磁盘并发新增的历史成员以 expected=MISSING
    纳入 path-set 全集合 CAS；单纯生成 local diff 时可省略。
    """
    ops = _diff_leaf_ops(base, local, prefix)
    if not prefix:
        ops = _fold_dynamic_path_sets(ops, base, local, dynamic_path_sets, observed_disk)
    return ops


def _is_descendant(path: tuple[str, ...], prefix: tuple[str, ...]) -> bool:
    """path 是否在 prefix 之下（含自身），用于吸收覆盖。"""
    return path[:len(prefix)] == prefix


def _apply_to_path_set_value(
    values: dict[tuple[str, ...], Any],
    root: tuple[str, ...],
    op: Operation,
) -> None:
    """把 path-set 之后的成员操作折叠进目标 values，expected 保持原始 CAS 基线。"""
    relative = op.path[len(root):]
    if not relative:
        if isinstance(op, DeletePath):
            values[root] = MISSING
        else:
            values[root] = copy.deepcopy(op.value)
        return

    current = values.get(root, MISSING)
    if current is MISSING or not isinstance(current, dict):
        current = {}
    if isinstance(op, DeletePath):
        values[root] = delete_path(current, relative, MISSING)
    else:
        values[root] = set_path(current, relative, op.value)


def normalize_operations(operations: list[Operation]) -> list[Operation]:
    """规范重叠操作，保留原有相对顺序且不拆散 path-set 原子单元。

    1. 同一路径只保留最后一个会话内操作；同一 registry 只保留最后一个 path-set。
    2. ancestor ReplaceSubtree 覆盖严格后代及与其重叠的整个 path-set。
    3. path-set 前的成员操作被其目标值吸收；path-set 后的成员操作合入 values。
    """
    copied = copy.deepcopy(operations)
    last_identity: dict[tuple[str, Any], int] = {}
    for index, op in enumerate(copied):
        identity = (
            ("path_set", op.registry_key)
            if isinstance(op, ReplacePathSet)
            else ("path", op.path)
        )
        last_identity[identity] = index

    indexed = [
        (index, op) for index, op in enumerate(copied)
        if last_identity[
            ("path_set", op.registry_key)
            if isinstance(op, ReplacePathSet)
            else ("path", op.path)
        ] == index
    ]

    subtree_paths = [op.path for _, op in indexed if isinstance(op, ReplaceSubtree)]

    for _, path_set in indexed:
        if not isinstance(path_set, ReplacePathSet):
            continue
        for prefix in subtree_paths:
            covered = [
                path != prefix and _is_descendant(path, prefix)
                for path in path_set.values
            ]
            if any(covered) and not all(covered):
                raise ValueError(
                    f"ReplaceSubtree {prefix!r} partially overlaps "
                    f"dynamic path-set {path_set.registry_key!r}"
                )

    def _strictly_covered_by_subtree(op: Operation) -> bool:
        paths = list(op.values) if isinstance(op, ReplacePathSet) else [op.path]
        return any(
            all(path != prefix and _is_descendant(path, prefix) for path in paths)
            for prefix in subtree_paths
        )

    survivors = [(index, op) for index, op in indexed if not _strictly_covered_by_subtree(op)]
    removed: set[int] = set()
    replacements: dict[int, ReplacePathSet] = {}

    for path_set_index, path_set in survivors:
        if not isinstance(path_set, ReplacePathSet):
            continue
        values = copy.deepcopy(path_set.values)
        roots = sorted(values, key=len, reverse=True)
        for other_index, other in survivors:
            if other_index == path_set_index or isinstance(other, ReplacePathSet):
                continue
            root = next(
                (candidate for candidate in roots if _is_descendant(other.path, candidate)),
                None,
            )
            if root is None:
                continue
            removed.add(other_index)
            if other_index > path_set_index:
                _apply_to_path_set_value(values, root, other)
        replacements[path_set_index] = ReplacePathSet(
            path_set.registry_key,
            values,
            path_set.expected,
        )

    return [
        replacements.get(index, op)
        for index, op in survivors
        if index not in removed
    ]


def merge_operations(
    base: dict,
    local: dict,
    disk: dict,
    dynamic_path_sets: Sequence = (),
) -> MergeResult:
    """三方合并：disk == base 应用 local、disk == local 视为 already-equal、其余冲突保留 disk。

    返回深拷贝结果，不修改入参。
    """
    ops = normalize_operations(diff_trees(
        base,
        local,
        dynamic_path_sets=dynamic_path_sets,
        observed_disk=disk,
    ))
    merged = copy.deepcopy(disk)
    result = MergeResult(value=merged)

    for op in ops:
        if isinstance(op, SetPath):
            path = op.path
            base_val = get_path(base, path)
            disk_val = get_path(result.value, path)
            if _eq(disk_val, base_val):
                result.value = set_path(result.value, path, op.value)
                result.applied_paths.append(path)
            elif _eq(disk_val, op.value):
                result.already_equal_paths.append(path)
            else:
                result.conflicted_paths.append(path)
                result.blocked.append(BlockedChange(path, "SET", op.value, disk_val))
        elif isinstance(op, DeletePath):
            path = op.path
            disk_val = get_path(result.value, path)
            if _eq(disk_val, op.expected):
                result.value = delete_path(result.value, path, op.expected)
                result.applied_paths.append(path)
            elif disk_val is MISSING:
                result.already_equal_paths.append(path)
            else:
                result.conflicted_paths.append(path)
                result.blocked.append(BlockedChange(path, "DELETE", MISSING, disk_val))
        elif isinstance(op, ReplaceSubtree):
            path = op.path
            disk_val = get_path(result.value, path)
            if _eq(disk_val, op.expected):
                result.value = set_path(result.value, path, op.value)
                result.applied_paths.append(path)
            elif _eq(disk_val, op.value):
                result.already_equal_paths.append(path)
            else:
                result.conflicted_paths.append(path)
                result.blocked.append(BlockedChange(path, "REPLACE_SUBTREE", op.value, disk_val))
        elif isinstance(op, ReplacePathSet):
            # 路径集合原子合并：任一成员与 expected 不一致即整体冲突，保持 disk 不变
            paths = sorted(op.values)
            if all(_eq(get_path(result.value, p), op.expected[p]) for p in paths):
                for p in paths:
                    v = op.values[p]
                    if v is MISSING:
                        result.value = delete_path(result.value, p, op.expected[p])
                    else:
                        result.value = set_path(result.value, p, v)
                result.applied_paths.extend(paths)
            elif all(_eq(get_path(result.value, p), op.values[p]) for p in paths):
                result.already_equal_paths.extend(paths)
            else:
                result.conflicted_paths.append(tuple(paths))
                result.blocked.append(BlockedChange(
                    tuple(paths), "REPLACE_PATH_SET", op.values,
                    {p: get_path(result.value, p) for p in paths},
                ))

    result.changed = bool(result.applied_paths)
    return result
