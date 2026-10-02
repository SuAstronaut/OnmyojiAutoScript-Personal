# This Python file uses the following encoding: utf-8
# 配置存储事务层（Task 3 生产切换核心）：
# - load/active_config_names 第一条生产动作固定调用 idempotent initialize()
# - 普通保存仍整段持锁（身份锁 + 单配置锁）；只有登录时间来源回写只锁提交与备份
# - 锁超时统一以 filelock.Timeout（继承 TimeoutError）向上传播，调用方按 TimeoutError 捕获
# - patch_user_argument 解析动态 group_N / count 控制路径，统一 REPLACE_PATH_SET 原子替换
# - save_background 三方合并 + blocked 指纹状态转移，磁盘最新值优先
# - 生命周期 create/import/delete/rename 全委托 GenerationManager
import copy
import hashlib
import os
import re
import threading
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from pydantic import BaseModel

from module.config.account_login import apply_login_times, changed_login_times, latest_login_times
from module.config.config_backup import backup_source_config
from module.config.config_generation import (
    ConfigGenerationError,
    ConfigIdentityConflictError,
    ConfigIdentityNameError,
    ConfigIdentityNotFoundError,
    GenerationManager,
)
from module.config.config_operations import (
    MISSING,
    DeletePath,
    SetPath,
    _eq,
    delete_path,
    get_path,
    merge_operations,
    normalize_operations,
    set_path,
)
from module.config.config_validation import (
    DEFAULT_CONFIG_PROFILE,
    ConfigValidationError,
    ValidationProfile,
    _list_item_model,
    _model_types,
    validate_persisted_config,
)
from module.config.utils import _write_file_unlocked, convert_to_underscore


class ConfigNotFoundError(FileNotFoundError):
    """配置不存在或处于 tombstone/creating 状态。"""


class ConfigJsonError(ValueError):
    """配置 JSON 无法解析或根节点不是对象。"""


class ConfigGenerationMismatchError(ValueError):
    """会话 generation 与磁盘 sidecar generation 不一致。"""


class ConfigWriteConflictError(RuntimeError):
    """锁外读取后配置已被其他写入方修改，需重新读取并合并。"""


@dataclass
class LoadedConfig:
    """严格校验后的运行模型、唯一 canonical、配置身份 generation 与 mtime_ns。"""
    model: Any
    canonical: dict
    generation: str
    mtime_ns: int
    content_digest: str


@dataclass
class PatchResult:
    """OASX 单字段 / 单参数写入结果。"""
    success: bool
    changed_paths: list = field(default_factory=list)
    mtime_ns: int = 0
    generation: str = ""
    operation: str = "SET"


@dataclass
class ReplaceResult:
    """子树或动态 path-set 原子替换结果。"""
    success: bool
    changed_paths: list = field(default_factory=list)
    mtime_ns: int = 0
    generation: str = ""
    operation: str = ""


@dataclass
class SaveResult:
    """后台三方保存结果；base 是供 session 提交的新合并基线。"""
    applied_paths: list = field(default_factory=list)
    already_equal_paths: list = field(default_factory=list)
    conflicted_paths: list = field(default_factory=list)
    deleted_paths: list = field(default_factory=list)
    wrote_file: bool = False
    mtime_ns: int = 0
    # 锁内采样的落盘原字节 SHA-256：供 session 同步 watcher，避免锁释放后再读文件
    # 时把并发进程刚写入的内容误记成自己这次保存的版本。
    content_digest: str = ""
    generation: str = ""
    blocked: list = field(default_factory=list)
    skipped_blocked_paths: list = field(default_factory=list)
    blocked_cleared_paths: list = field(default_factory=list)
    base: dict = field(default_factory=dict)
    # 经提交校验确认的实际登录时间，用于同步运行模型及关联来源配置。
    login_times: dict = field(default_factory=dict)


@dataclass
class BlockedStateResult:
    """blocked 指纹状态转移结果。"""
    skip: list = field(default_factory=list)      # local/disk 均未变 → 跳过
    clear: list = field(default_factory=list)     # disk == blocked local → ALREADY_EQUAL 并清除
    release: list = field(default_factory=list)   # local 或 disk 变化 → 重新三方判断


def advance_blocked_state(blocked, base, local, disk) -> BlockedStateResult:
    """纯函数：根据最新 local/disk 决定每个 blocked 变更的状态转移。

    规则（规格 §7.2）：
      - local == blocked_local 且 disk == observed_disk → 跳过，不重复尝试；
      - local == blocked_local 且 disk == blocked_local → ALREADY_EQUAL，推进 base 并清除；
      - 其余（local 或 disk 变化）→ 解除 blocked，按当前 base/disk/local 重新判断。
    """
    result = BlockedStateResult()
    for bc in blocked:
        if bc.operation == "REPLACE_PATH_SET":
            paths = tuple(bc.path)
            local_match = all(_eq(get_path(local, p), bc.blocked_local_value[p]) for p in paths)
            disk_match = all(_eq(get_path(disk, p), bc.observed_disk_value[p]) for p in paths)
            disk_eq_blocked = all(_eq(get_path(disk, p), bc.blocked_local_value[p]) for p in paths)
        else:
            path = bc.path
            local_match = _eq(get_path(local, path), bc.blocked_local_value)
            disk_match = _eq(get_path(disk, path), bc.observed_disk_value)
            disk_eq_blocked = _eq(get_path(disk, path), bc.blocked_local_value)
        if local_match and disk_match:
            result.skip.append(bc)
        elif local_match and disk_eq_blocked:
            result.clear.append(bc)
        else:
            result.release.append(bc)
    return result


class ConfigStore:
    """配置身份、文件事务与专用写操作的生产入口。"""

    def __init__(
        self,
        config_root: Path,
        profile: ValidationProfile = None,
        timeout: float = 10.0,
    ):
        self.config_root = Path(config_root)
        self.profile = profile or DEFAULT_CONFIG_PROFILE
        self.generation = GenerationManager(self.config_root, profile=self.profile, timeout=timeout)
        self._initialized = False
        self._init_lock = threading.Lock()
        self.last_operation: Optional[PatchResult | ReplaceResult] = None

    # ------------------------------------------------------------------ 初始化与枚举

    def initialize(self) -> None:
        """固定执行 migration → lifecycle 恢复 → active 身份校验（幂等）。

        恢复完成前不得枚举配置或创建 ScriptProcess；任何 create/delete/rename/load
        都会先经过这里，保证并发生命周期操作不会读到半迁移文件。
        三步在 sync_identity_state 内持同一把全局身份锁，避免并发事务中间态
        被身份校验误判为损坏。
        """
        if self._initialized:
            return
        with self._init_lock:
            if self._initialized:
                return
            self.generation.sync_identity_state()
            self._initialized = True

    @property
    def quarantined_identities(self) -> dict[str, Exception]:
        """内容级校验失败被隔离的身份：{name: 异常}。

        这些名称不会出现在 active_config_names 中，load 仍按原异常 fail closed；
        调用方据此向用户上报「哪份配置坏了、备份在哪」。
        """
        return dict(self.generation.quarantined_identities)

    def active_config_names(self, include_template: bool = False) -> list[str]:
        """只返回 sidecar state=active 且严格 load 成功的名称。

        tombstone / creating / 损坏身份一律不枚举；template 默认排除。
        """
        return list(self.active_canonical_snapshots(include_template).keys())

    def active_canonical_snapshots(self, include_template: bool = False) -> dict[str, dict]:
        """一次遍历返回 {name: canonical}，避免调用方枚举后再逐个重复 load。

        整轮枚举只取一次全局身份锁、只重放一次 journal，每个名称只额外取自己的
        lifecycle 锁；原实现让每个名称各走一遍「validation 锁 + 全量恢复」，
        把枚举放大成 O(N²) 次 FileLock。
        """
        self.initialize()
        snapshots: dict[str, dict] = {}
        with self.generation._validation_lock():
            self.generation._recover_pending_journals()
            for sidecar_path in sorted(self.generation.generations_dir.glob("*.json")):
                name = sidecar_path.stem
                if not include_template and name == "template":
                    continue
                record = self.generation.read_active_generation(name)
                if record is None or record.state == "tombstone":
                    # tombstone 永久保留（同名重建靠它区分身份），数量随删除次数累积；
                    # 它们不可能变成 active，无需为每个取一把 lifecycle 锁。遗留物理配置的
                    # 清理由 initialize() 的全量 recover 负责。
                    continue
                try:
                    with self.generation._lifecycle_lock(name):
                        # creating 可能是崩溃残留：锁内恢复后才判定，避免漏掉可前滚为 active 的身份。
                        self.generation._recover_name_sidecar(name)
                        loaded = self._load_unlocked(name)
                except (
                    ConfigNotFoundError,
                    ConfigGenerationError,
                    ConfigJsonError,
                    ConfigValidationError,
                ):
                    # 永久损坏、被隔离或并发删除的身份不枚举；锁超时与 I/O 错误必须向上传播，
                    # 防止调用方用部分列表覆盖仍管理着存活进程的 registry。
                    continue
                snapshots[name] = loaded.canonical
        return snapshots

    # ------------------------------------------------------------------ 读取

    def load(self, config_name: str) -> LoadedConfig:
        self.initialize()
        with self.generation.identity_lifecycle_lock(config_name):
            return self._load_unlocked(config_name)

    def load_canonical_snapshot(self, config_name: str) -> dict:
        return self.load(config_name).canonical

    def _file_revision(self, config_name: str) -> tuple[int, str]:
        """在 lifecycle 锁内返回文件 mtime 与原字节 SHA-256，供 watcher 绑定已加载版本。"""
        config_path = self.generation._config_path(config_name)
        raw = config_path.read_bytes()
        return config_path.stat().st_mtime_ns, hashlib.sha256(raw).hexdigest()

    def _load_unlocked(self, config_name: str) -> LoadedConfig:
        record = self.generation.read_active_generation(config_name)
        if record is None or record.state != "active":
            raise ConfigNotFoundError(f"Config not found or not active: {config_name}")
        config_path = self.generation._config_path(config_name)
        if not config_path.exists():
            raise ConfigGenerationError(f"{config_name}: active sidecar but config missing")
        raw = self.generation._read_raw_json(config_name)
        if raw.get("config_name") != config_name:
            raise ConfigGenerationError(f"{config_name}: config_name mismatch")
        model, canonical = validate_persisted_config(raw, config_name, self.profile)
        mtime_ns, content_digest = self._file_revision(config_name)
        return LoadedConfig(model, canonical, record.generation, mtime_ns, content_digest)

    def _load_canonical_unlocked(self, config_name: str) -> dict:
        return self._load_unlocked(config_name).canonical

    def _write_config(self, config_name: str, canonical: dict) -> None:
        """锁内 atomic write canonical；调用方必须已持有该名称 lifecycle FileLock。"""
        _write_file_unlocked(str(self.generation._config_path(config_name)), canonical)

    # ------------------------------------------------------------------ 单字段写入

    def patch_user_field(self, config_name: str, path: tuple[str, ...], value: Any) -> PatchResult:
        """锁内读取最新磁盘，修改单个字段，严格校验并原子写回。"""
        self.initialize()
        with self.generation.identity_lifecycle_lock(config_name):
            return self._patch_user_field_locked(config_name, path, value)

    def _patch_user_field_locked(self, config_name: str, path: tuple[str, ...], value: Any) -> PatchResult:
        loaded = self._load_unlocked(config_name)
        new_canonical = set_path(loaded.canonical, path, value)
        if new_canonical == loaded.canonical:
            return PatchResult(True, [], loaded.mtime_ns, loaded.generation, "SET")
        _model, canonical = validate_persisted_config(new_canonical, config_name, self.profile)
        if canonical == loaded.canonical:
            return PatchResult(True, [path], loaded.mtime_ns, loaded.generation, "SET")
        self._write_config(config_name, canonical)
        mtime_ns = os.stat(self.generation._config_path(config_name)).st_mtime_ns
        result = PatchResult(True, [path], mtime_ns, loaded.generation, "SET")
        self.last_operation = result
        return result

    # ------------------------------------------------------------------ 动态 group_N / count

    def _normalize_user_group(self, group: str) -> tuple[str, Optional[int]]:
        """拆动态 suffix：inviteInfoList_1 → ("invite_info_list", 1)。

        禁止把含 underscore 的原 inviteInfoList_1 直接交给 convert_to_underscore，
        否则返回原样导致无法匹配 registry。
        """
        match = re.fullmatch(r"(?P<stem>.+)_(?P<index>[1-9]\d*)", group)
        if match:
            stem = convert_to_underscore(match.group("stem"))
            return stem, int(match.group("index"))
        return convert_to_underscore(group), None

    def _find_dynamic_entry(self, task_key: str, field: str):
        for entry in self.profile.dynamic_path_sets:
            parent = entry.member_path[:-1]
            if parent == (task_key,) and entry.member_path[-1] == field:
                return entry
        return None

    def _item_model_for_entry(self, entry):
        """从 profile.model_type 沿 member_path 推导动态列表项模型。"""
        task_model = self.profile.model_type
        for part in entry.member_path[:-1]:
            candidates = _model_types(task_model.model_fields[part].annotation)
            if len(candidates) != 1:
                raise ConfigValidationError(f"dynamic registry {entry.key} has ambiguous task model")
            task_model = candidates[0]
        item_model = _list_item_model(task_model.model_fields[entry.member_path[-1]].annotation)
        if item_model is None:
            raise ConfigValidationError(f"dynamic registry {entry.key} has invalid item model")
        return item_model

    def patch_user_argument(
        self,
        config_name: str,
        task: str,
        group: str,
        argument: str,
        value: Any,
    ) -> PatchResult:
        """OASX / GUI 通用写参数入口。

        先 normalize task/argument/group；随后按两个方向查 registry：
        ① group_N 命中注册项成员 → 更新该成员并按 cardinality 重序列化 REPLACE_PATH_SET；
        ② 普通 group/argument 等于 registry 的 count/control path → 按新 count 扩/缩容；
        ③ 既非成员又非 count/control 才进普通 leaf patch。
        """
        self.initialize()
        task_key = convert_to_underscore(task)
        argument_key = convert_to_underscore(argument)
        stem, index = self._normalize_user_group(group)

        with self.generation.identity_lifecycle_lock(config_name):
            loaded = self._load_unlocked(config_name)
            canonical = loaded.canonical

            # 桌面模式下 handle 在界面上是下拉项，回传展示串需剥回纯 PID（旧 script_set_arg 逻辑）
            if (task_key, stem, argument_key) == ("script", "device", "handle"):
                serial = get_path(canonical, ("script", "device", "serial"))
                if serial == "desktop" and isinstance(value, str):
                    from module.device.handle import desktop_option2pid
                    value = desktop_option2pid(value)

            # ① group_N 成员
            if index is not None:
                entry = self._find_dynamic_entry(task_key, stem)
                if entry is not None:
                    return self._patch_dynamic_member_locked(
                        config_name, loaded, entry, index, argument_key, value)

            # ② count/control 路径
            path = (task_key, stem, argument_key)
            for entry in self.profile.dynamic_path_sets:
                if entry.count_path == path:
                    return self._patch_dynamic_count_locked(config_name, loaded, entry, value)

            # ③ 普通 leaf
            return self._patch_user_field_locked(config_name, path, value)

    def _patch_dynamic_member_locked(
        self,
        config_name: str,
        loaded: LoadedConfig,
        entry,
        index: int,
        argument_key: str,
        value: Any,
    ) -> PatchResult:
        canonical = loaded.canonical
        parent_path = entry.member_path[:-1]
        field = entry.member_path[-1]
        parent = get_path(canonical, parent_path)
        member_keys = self._existing_member_keys(parent, field)
        count = self._dynamic_count(canonical, entry, member_keys)
        if index < 1 or index > count:
            raise ConfigValidationError(
                f"dynamic member {field}_{index} out of range 1..{count}")

        values: dict = {}
        expected: dict = {}
        member_paths = [
            parent_path + (f"{field}_{i}",) for i in range(1, count + 1)
        ]
        for p in member_paths:
            current = get_path(canonical, p)
            expected[p] = copy.deepcopy(current)
            if p[-1] == f"{field}_{index}":
                # 更新指定成员：先取磁盘成员 dict，再深拷贝设置 argument 字段
                member_dict = current if isinstance(current, dict) else {}
                values[p] = set_path(member_dict, (argument_key,), value)
            else:
                values[p] = copy.deepcopy(current)
        if entry.count_path is not None:
            count_path = entry.count_path
            expected[count_path] = get_path(canonical, count_path)
            values[count_path] = get_path(canonical, count_path)
        return self._replace_path_set_locked(config_name, loaded, entry, values, expected)

    def _patch_dynamic_count_locked(
        self,
        config_name: str,
        loaded: LoadedConfig,
        entry,
        new_count: int,
    ) -> PatchResult:
        canonical = loaded.canonical
        parent_path = entry.member_path[:-1]
        field = entry.member_path[-1]
        parent = get_path(canonical, parent_path)
        member_keys = self._existing_member_keys(parent, field)
        current_count = self._dynamic_count(canonical, entry, member_keys)
        if type(new_count) is not int or isinstance(new_count, bool) or new_count < 0:
            raise ConfigValidationError(f"dynamic count must be a non-negative integer")

        item_model = self._item_model_for_entry(entry)
        default_member = item_model().model_dump(mode="json")
        values: dict = {}
        expected: dict = {}
        for i in range(1, new_count + 1):
            p = parent_path + (f"{field}_{i}",)
            current = get_path(canonical, p)
            expected[p] = copy.deepcopy(current)
            values[p] = copy.deepcopy(current) if current is not MISSING else copy.deepcopy(default_member)
        # 缩容时残余 _N（i > new_count）必须删除，expected 记录磁盘现状
        for key in member_keys:
            i = int(key[len(field) + 1:])
            if i > new_count:
                p = parent_path + (key,)
                expected[p] = get_path(canonical, p)
                values[p] = MISSING
        if entry.count_path is not None:
            count_path = entry.count_path
            expected[count_path] = get_path(canonical, count_path)
            values[count_path] = new_count
        return self._replace_path_set_locked(config_name, loaded, entry, values, expected)

    def _existing_member_keys(self, parent, field: str) -> list[str]:
        """收集父节点下 field_N 规范数字 key（按数字升序）。"""
        if not isinstance(parent, dict):
            return []
        prefix = field + "_"
        keys = []
        for key in parent:
            if not key.startswith(prefix):
                continue
            suffix = key[len(prefix):]
            if suffix.isdigit() and int(suffix) >= 1 and suffix == str(int(suffix)):
                keys.append(key)
        return sorted(keys, key=lambda k: int(k[len(prefix):]))

    def _dynamic_count(self, canonical: dict, entry, member_keys: list[str]) -> int:
        if entry.count_path is not None:
            count = get_path(canonical, entry.count_path)
            if type(count) is not int or isinstance(count, bool) or count < 0:
                raise ConfigValidationError(f"dynamic count {'/'.join(entry.count_path)} is invalid")
            return count
        if entry.mode == "single":
            return 1
        return len(member_keys)

    # ------------------------------------------------------------------ 原子替换

    def replace_path_set(
        self,
        config_name: str,
        registry_key: str,
        values: dict,
        expected: dict,
    ) -> ReplaceResult:
        """对动态 serializer 声明路径集合执行原子替换（含残余 _N 删除）。"""
        self.initialize()
        entry = self._registry_entry_by_key(registry_key)
        with self.generation.identity_lifecycle_lock(config_name):
            loaded = self._load_unlocked(config_name)
            return self._replace_path_set_locked(config_name, loaded, entry, values, expected)

    def _registry_entry_by_key(self, registry_key: str):
        for entry in self.profile.dynamic_path_sets:
            if entry.key == registry_key:
                return entry
        raise KeyError(f"unknown dynamic registry: {registry_key}")

    def _replace_path_set_locked(
        self,
        config_name: str,
        loaded: LoadedConfig,
        entry,
        values: dict,
        expected: dict,
    ) -> ReplaceResult:
        canonical = loaded.canonical
        for p, exp in expected.items():
            if not _eq(get_path(canonical, p), exp):
                raise ConfigGenerationMismatchError(
                    f"dynamic path-set {entry.key} changed since expected value")
        new_canonical = copy.deepcopy(canonical)
        for p, v in values.items():
            if v is MISSING:
                new_canonical = delete_path(new_canonical, p, MISSING)
            else:
                new_canonical = set_path(new_canonical, p, v)
        _model, canonical2 = validate_persisted_config(new_canonical, config_name, self.profile)
        changed = canonical2 != canonical
        if changed:
            self._write_config(config_name, canonical2)
            mtime_ns = os.stat(self.generation._config_path(config_name)).st_mtime_ns
        else:
            mtime_ns = loaded.mtime_ns
        result = ReplaceResult(
            success=True,
            changed_paths=sorted(values) if changed else [],
            mtime_ns=mtime_ns,
            generation=loaded.generation,
            operation="REPLACE_PATH_SET",
        )
        self.last_operation = result
        return result

    def replace_subtree(
        self,
        config_name: str,
        path: tuple[str, ...],
        expected: Any,
        value: Any,
        expected_generation: str,
    ) -> ReplaceResult:
        """按 generation 与子树值双重 CAS 原子替换，拒绝同名重建后的陈旧请求。"""
        self.initialize()
        with self.generation.identity_lifecycle_lock(config_name):
            record = self.generation.read_active_generation(config_name)
            if record is None or record.state != "active":
                raise ConfigIdentityNotFoundError(f"{config_name} is not active")
            if record.generation != expected_generation:
                raise ConfigGenerationMismatchError(
                    f"{config_name} generation changed: expected "
                    f"{expected_generation}, disk {record.generation}"
                )
            loaded = self._load_unlocked(config_name)
            disk_val = get_path(loaded.canonical, path)
            if not _eq(disk_val, expected):
                raise ConfigGenerationMismatchError(f"subtree {'/'.join(path)} changed since expected")
            new_canonical = set_path(loaded.canonical, path, value)
            _model, canonical = validate_persisted_config(new_canonical, config_name, self.profile)
            changed = canonical != loaded.canonical
            if changed:
                self._write_config(config_name, canonical)
                mtime_ns = os.stat(self.generation._config_path(config_name)).st_mtime_ns
            else:
                mtime_ns = loaded.mtime_ns
            result = ReplaceResult(
                success=True,
                changed_paths=[path] if changed else [],
                mtime_ns=mtime_ns,
                generation=loaded.generation,
                operation="REPLACE_SUBTREE",
            )
            self.last_operation = result
            return result

    # ------------------------------------------------------------------ 后台三方保存

    def save_background(
        self,
        config_name: str,
        base: dict,
        local: dict,
        generation: str,
        blocked: list,
        login_times: dict = None,
    ) -> SaveResult:
        """三方合并：base 上次确认基线、local 运行模型想保存、disk 锁内最新磁盘。

        blocked 指纹状态转移先行；合并后磁盘最新值优先，同字段冲突不冲回旧值。
        本方法保持原有整段持锁语义（身份锁 + 单配置锁）；只有登录时间来源回写走
        `sync_account_login_times` 的窄锁路径。
        """
        self.initialize()
        if isinstance(base, BaseModel):
            base = base.model_dump(mode="json")
        if isinstance(local, BaseModel):
            local = local.model_dump(mode="json")
        base = copy.deepcopy(base)
        local = copy.deepcopy(local)
        # 把本批成功切号和原有任务完成回写合并为一次配置事务。
        updates = dict(login_times or {})
        for identity, moment in changed_login_times(base, local).items():
            updates[identity] = max(updates.get(identity, moment), moment)
        with self.generation.identity_lifecycle_lock(config_name):
            record = self.generation.read_active_generation(config_name)
            if record is None or record.state != "active":
                raise ConfigGenerationError(f"{config_name} is not active")
            if record.generation != generation:
                raise ConfigGenerationMismatchError(
                    f"{config_name} generation changed: session {generation}, disk {record.generation}")
            loaded = self._load_unlocked(config_name)
            disk = loaded.canonical
            disk_mtime_ns = loaded.mtime_ns
            # 先按快照取最新时间，提交时若快照已过期则释放锁重读重算。
            disk_times = latest_login_times(disk)
            updates = {identity: max(moment, disk_times.get(identity, moment))
                       for identity, moment in updates.items()}

            blocked_result = advance_blocked_state(blocked, base, local, disk)
            # 跳过路径：把 local 压制回 base，diff 不再生成操作，base/磁盘均不推进。
            # 若 base 中该路径为 MISSING（动态列表缩容后的成员），应显式删除而非 set MISSING，
            # 避免每次保存把 MISSING 哨兵写进 local 造成重复 blocked 条目。
            local2 = copy.deepcopy(local)
            for bc in blocked_result.skip:
                if bc.operation == "REPLACE_PATH_SET":
                    for p in bc.path:
                        base_val = get_path(base, p)
                        if base_val is MISSING:
                            local2 = delete_path(local2, p, MISSING)
                        else:
                            local2 = set_path(local2, p, base_val)
                else:
                    base_val = get_path(base, bc.path)
                    if base_val is MISSING:
                        local2 = delete_path(local2, bc.path, MISSING)
                    else:
                        local2 = set_path(local2, bc.path, base_val)

            merge_result = merge_operations(
                base,
                local2,
                disk,
                dynamic_path_sets=self.profile.dynamic_path_sets,
            )

            result = SaveResult(
                applied_paths=list(merge_result.applied_paths),
                already_equal_paths=list(merge_result.already_equal_paths),
                conflicted_paths=list(merge_result.conflicted_paths),
                blocked=list(merge_result.blocked),
                generation=record.generation,
                mtime_ns=disk_mtime_ns,
                content_digest=loaded.content_digest,
            )
            # 被跳过的 blocked 指纹保持原样进入下一轮
            result.blocked.extend(bc for bc in blocked_result.skip)
            result.skipped_blocked_paths = [bc.path for bc in blocked_result.skip]
            result.blocked_cleared_paths = [bc.path for bc in blocked_result.clear]

            # 推进合并基线：applied/already_equal 路径 base=local；deleted 路径从 base 删除
            result_base = copy.deepcopy(base)
            deleted_set = set()
            for p in merge_result.applied_paths:
                local_val = get_path(local2, p)
                if local_val is MISSING:
                    result_base = delete_path(result_base, p, MISSING)
                    deleted_set.add(p)
                else:
                    result_base = set_path(result_base, p, local_val)
            for p in merge_result.already_equal_paths:
                local_val = get_path(local2, p)
                if local_val is MISSING:
                    # 动态 path-set 缩容后，已与磁盘一致的删除成员也必须从基线移除。
                    result_base = delete_path(result_base, p, MISSING)
                    deleted_set.add(p)
                else:
                    result_base = set_path(result_base, p, local_val)
            result.base = result_base
            result.deleted_paths = sorted(deleted_set)

            # 普通参数保持原三方合并规则，登录时间按完整角色身份跨列表取最新值。
            apply_login_times(merge_result.value, updates)
            apply_login_times(result.base, updates)
            result.login_times = updates
            if not merge_result.changed and merge_result.value == disk:
                return result
            # 合并结果严格校验后写盘；校验失败抛 ConfigValidationError，磁盘保持不变
            _model, canonical = validate_persisted_config(merge_result.value, config_name, self.profile)
            if canonical != disk:
                # 多个角色或多个 list 只在实际写入前备份一次完整原文件。
                if changed_login_times(disk, canonical):
                    backup_source_config(self.generation._config_path(config_name))
                self._write_config(config_name, canonical)
                result.wrote_file = True
                # mtime 与 digest 都在锁内采样，保证 session 记下的正是自己写入的版本
                result.mtime_ns, result.content_digest = self._file_revision(config_name)
            return result

    def sync_account_login_times(self, config_name: str, updates: dict) -> dict:
        """来源配置逐份提交，读取不持锁，不同时锁住多份配置。"""
        for attempt in range(3):
            try:
                return self._sync_account_login_times_once(config_name, updates)
            except ConfigWriteConflictError:
                if attempt == 2:
                    raise

    def _commit_login_update(self, config_name, loaded, canonical, *, backup):
        """单配置锁只覆盖提交校验、备份和覆盖写，读取及合并均在锁外完成。"""
        with self.generation._lifecycle_lock(config_name):
            # 原 identity_lifecycle_lock 会在取到该身份锁后就地恢复 creating/tombstone 残留；
            # 提交路径只锁单配置，需要保留同一恢复语义，否则崩溃残留会让保存直接失败。
            self.generation._recover_name_sidecar(config_name)
            record = self.generation.read_active_generation(config_name)
            if record is None or record.state != 'active' or record.generation != loaded.generation:
                raise ConfigGenerationMismatchError(f'{config_name}: identity changed before write')
            # 锁内仅核对快照是否仍有效，变化则释放锁后重读，绝不拿旧内容覆盖新配置。
            path = self.generation._config_path(config_name)
            if hashlib.sha256(path.read_bytes()).hexdigest() != loaded.content_digest:
                raise ConfigWriteConflictError(f'{config_name}: changed before write')
            if backup:
                backup_source_config(path)
            self._write_config(config_name, canonical)
            return self._file_revision(config_name)

    def _sync_account_login_times_once(self, config_name: str, updates: dict) -> dict:
        """锁外读取关联来源并合并时间，只在实际备份和覆盖写入时加锁。"""
        self.initialize()
        loaded = self._load_unlocked(config_name)
        canonical = copy.deepcopy(loaded.canonical)
        previous = latest_login_times(canonical)
        effective = {identity: max(moment, previous.get(identity, moment))
                     for identity, moment in updates.items()}
        if not apply_login_times(canonical, effective):
            return effective
        # loaded 已完成旧配置迁移，专用任务完成时间不会被登录时间覆盖。
        _model, canonical = validate_persisted_config(canonical, config_name, self.profile)
        # 时间序列化可能消去微秒，最终内容无变化时不能覆盖上一份备份。
        if canonical == loaded.canonical:
            return effective
        self._commit_login_update(config_name, loaded, canonical, backup=True)
        return effective

    # ------------------------------------------------------------------ 专用写操作

    def startup_normalize(
        self,
        config_name: str,
        updates: dict[tuple[str, ...], Any],
        generation: str,
    ) -> LoadedConfig:
        """只修改 updates 指定路径的内部归一化事务，返回完整 LoadedConfig。"""
        self.initialize()
        with self.generation.identity_lifecycle_lock(config_name):
            record = self.generation.read_active_generation(config_name)
            if record is None or record.state != "active":
                raise ConfigGenerationError(f"{config_name} is not active")
            if record.generation != generation:
                raise ConfigGenerationMismatchError(
                    f"{config_name} generation changed: session {generation}, disk {record.generation}")
            loaded = self._load_unlocked(config_name)
            new_canonical = copy.deepcopy(loaded.canonical)
            for path, value in updates.items():
                new_canonical = set_path(new_canonical, path, value)
            model, canonical = validate_persisted_config(new_canonical, config_name, self.profile)
            if canonical != loaded.canonical:
                self._write_config(config_name, canonical)
            mtime_ns, content_digest = self._file_revision(config_name)
            return LoadedConfig(model, canonical, record.generation, mtime_ns, content_digest)

    def reset_enabled_next_runs(self, config_name: str) -> PatchResult:
        """原子启用全局重置标志，并更新所有已启用任务的 next_run。"""
        self.initialize()
        flag_path = (
            "restart",
            "tasks_config_reset",
            "reset_task_datetime_enable",
        )
        target_path = (
            "restart",
            "tasks_config_reset",
            "reset_task_datetime",
        )
        with self.generation.identity_lifecycle_lock(config_name):
            loaded = self._load_unlocked(config_name)
            target = get_path(loaded.canonical, target_path)
            if target is MISSING:
                raise ConfigValidationError(
                    "restart/tasks_config_reset/reset_task_datetime is missing"
                )
            target_str = (
                target.strftime("%Y-%m-%d %H:%M:%S")
                if isinstance(target, datetime)
                else str(target)
            )
            new_canonical = set_path(loaded.canonical, flag_path, True)
            changed_paths = []
            if get_path(loaded.canonical, flag_path) is not True:
                changed_paths.append(flag_path)

            for task_key, task_value in new_canonical.items():
                if not isinstance(task_value, dict):
                    continue
                scheduler = task_value.get("scheduler")
                if not isinstance(scheduler, dict) or not scheduler.get("enable"):
                    continue
                next_run_path = (task_key, "scheduler", "next_run")
                if get_path(loaded.canonical, next_run_path) != target_str:
                    changed_paths.append(next_run_path)
                    scheduler["next_run"] = target_str

            _model, canonical = validate_persisted_config(
                new_canonical,
                config_name,
                self.profile,
            )
            changed = canonical != loaded.canonical
            if changed:
                self._write_config(config_name, canonical)
                mtime_ns = os.stat(
                    self.generation._config_path(config_name)
                ).st_mtime_ns
            else:
                mtime_ns = loaded.mtime_ns
            result = PatchResult(
                success=True,
                changed_paths=sorted(changed_paths) if changed else [],
                mtime_ns=mtime_ns,
                generation=loaded.generation,
                operation="RESET_ENABLED_NEXT_RUNS",
            )
            self.last_operation = result
            return result

    def reset_next_runs(
        self,
        config_name: str,
        target,
        expected_generation: str,
    ) -> SaveResult:
        """按 generation CAS 重置启用任务的 next_run，拒绝同名重建后的陈旧请求。"""
        self.initialize()
        target_str = target.strftime("%Y-%m-%d %H:%M:%S") if isinstance(target, datetime) else str(target)
        with self.generation.identity_lifecycle_lock(config_name):
            record = self.generation.read_active_generation(config_name)
            if record is None or record.state != "active":
                raise ConfigGenerationError(f"{config_name} is not active")
            if record.generation != expected_generation:
                raise ConfigGenerationMismatchError(
                    f"{config_name} generation changed: expected "
                    f"{expected_generation}, disk {record.generation}"
                )
            loaded = self._load_unlocked(config_name)
            new_canonical = copy.deepcopy(loaded.canonical)
            for task_key, task_value in new_canonical.items():
                if not isinstance(task_value, dict):
                    continue
                scheduler = task_value.get("scheduler")
                if not isinstance(scheduler, dict):
                    continue
                if scheduler.get("enable"):
                    scheduler["next_run"] = target_str
            _model, canonical = validate_persisted_config(new_canonical, config_name, self.profile)
            changed = canonical != loaded.canonical
            if changed:
                self._write_config(config_name, canonical)
                mtime_ns = os.stat(self.generation._config_path(config_name)).st_mtime_ns
            else:
                mtime_ns = loaded.mtime_ns
            return SaveResult(
                applied_paths=[] if not changed else ["*scheduler.next_run*"],
                wrote_file=changed,
                mtime_ns=mtime_ns,
                generation=loaded.generation,
                base=canonical,
            )

    # ------------------------------------------------------------------ 生命周期

    def create_from_template(self, config_name: str, raw: dict) -> LoadedConfig:
        """基于模板 canonical 创建新配置（走 generation create 流程），返回 LoadedConfig。"""
        self.initialize()
        self.generation.create_config(config_name, raw)
        return self.load(config_name)

    def replace_template(self, raw: dict) -> LoadedConfig:
        """template 特例：已有 active 则同 generation 严格原子替换；首次缺失走 generation create。

        template 不投递运行实例 config event。
        """
        self.initialize()
        try:
            with self.generation.identity_lifecycle_lock("template"):
                loaded = self._load_unlocked("template")
                _model, canonical = validate_persisted_config(raw, "template", self.profile)
                if canonical != loaded.canonical:
                    self._write_config("template", canonical)
                return self._load_unlocked("template")
        except ConfigNotFoundError:
            try:
                self.generation.create_config("template", raw)
                return self.load("template")
            except ConfigGenerationError as create_error:
                # 缺失检查与 create 之间若并发方已创建，转为锁内 replace；
                # 若仍无法 load，说明不是同名创建竞态，保留原 create 异常。
                try:
                    with self.generation.identity_lifecycle_lock("template"):
                        loaded = self._load_unlocked("template")
                        _model, canonical = validate_persisted_config(raw, "template", self.profile)
                        if canonical != loaded.canonical:
                            self._write_config("template", canonical)
                        return self._load_unlocked("template")
                except ConfigNotFoundError:
                    raise create_error

    def import_config(self, config_name: str, raw: dict) -> str:
        """显式导入新配置，返回新 generation。"""
        self.initialize()
        return self.generation.create_config(config_name, raw)

    def validate_rename_names(self, source: str, destination: str) -> None:
        """停止运行实例前预检 rename 名称、源身份与可确定的目标冲突。"""
        source = self.generation._validate_name(source)
        destination = self.generation._validate_name(destination)
        self.generation._reject_reserved_identity(source)
        self.generation._reject_reserved_identity(destination)
        if self.generation._same_config_identity(source, destination):
            raise ConfigIdentityNameError(
                "rename source and destination must differ"
            )
        self.initialize()
        with self.generation._validation_lock():
            # 先收敛既有事务，避免把可恢复的中间态误判为目标冲突。
            self.generation.recover_lifecycle_transactions()
            source_record = self.generation._read_sidecar(source)
            if source_record is None or source_record.state != "active":
                raise ConfigIdentityNotFoundError(f"{source} is not active")
            if self.generation._logically_exists(destination):
                raise ConfigIdentityConflictError(f"{destination} already exists")

    def reconcile_lifecycle_transactions(self) -> None:
        """事务异常后锁内恢复 journal，供调用方按稳定磁盘身份对账 registry。"""
        self.initialize()
        with self.generation._validation_lock():
            self.generation.recover_lifecycle_transactions()

    def delete_config(self, config_name: str) -> None:
        self.initialize()
        self.generation.delete_config(config_name)

    def rename_config(self, source: str, destination: str) -> None:
        self.initialize()
        self.generation.rename_config(source, destination)
