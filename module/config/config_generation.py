# This Python file uses the following encoding: utf-8
# 配置 generation 生命周期与崩溃恢复库：
# - sidecar 记录 config 身份（creating/active/tombstone），generation 表示文件身份而非内容版本
# - create/delete/rename 使用持久 journal + fault point，进程崩溃后可按协议前滚/回滚
# - migration 首次升级做原字节备份与 legacy 归一化，marker 存在后幂等且损坏输入 fail closed
# - 本模块不接生产 Config/ConfigStore；所有文件事务均发生在注入的 config_root 内
import copy
import hashlib
import json
import os
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from filelock import FileLock

from module.config.atomicwrites import atomic_write
from module.config.config_validation import (
    DEFAULT_CONFIG_PROFILE,
    ConfigValidationError,
    ValidationProfile,
    normalize_legacy_config,
    validate_persisted_config,
)
from module.config.utils import _read_file_unlocked, _write_file_unlocked
# 延迟导入 logger 会打断备份告警的就近性，这里直接顶部导入（config 模块自身无循环依赖）
from module.logger import logger

# --- fault points 常量（测试与生产共用同一命名，见计划 Step 2） ---
MIGRATION_FAULT_POINTS = (
    "migration.after_backup",
    "migration.after_normalized_config",
    "migration.after_active_sidecar",
    "migration.before_initialized_marker",
)
CREATE_FAULT_POINTS = (
    "create.after_creating_sidecar",
    "create.after_config_write",
    "create.after_active_sidecar",
)
DELETE_FAULT_POINTS = (
    "delete.after_tombstone",
    "delete.after_config_unlink",
)
RENAME_FAULT_POINTS = (
    "rename.after_prepared_journal",
    "rename.after_target_creating",
    "rename.after_target_config",
    "rename.after_committed_journal",
    "rename.after_target_active",
    "rename.after_source_tombstone",
    "rename.after_source_unlink",
)


class ConfigGenerationError(ValueError):
    """配置身份或生命周期事务失败，磁盘保持可恢复状态。"""


class ConfigIdentityNotFoundError(ConfigGenerationError):
    """请求的配置身份不存在或已不再 active。"""


class ConfigIdentityConflictError(ConfigGenerationError):
    """生命周期目标身份已存在，当前操作不能提交。"""


class ConfigIdentityNameError(ConfigGenerationError):
    """配置身份名称不符合路径安全约束。"""


@dataclass(frozen=True)
class GenerationRecord:
    """配置身份记录。

    creating 必须携带待提交配置 digest（崩溃恢复依据）；切为 active/tombstone 后 digest=None。
    generation 表示文件身份而非内容版本，正常字段保存不更新 sidecar。
    """
    generation: str
    state: str
    digest: Optional[str] = None

    def __post_init__(self):
        if self.state not in ("creating", "active", "tombstone"):
            raise ValueError(f"invalid sidecar state {self.state!r}")
        if not self.generation:
            raise ValueError("sidecar generation must not be empty")
        if self.state == "creating" and not self.digest:
            raise ValueError("creating sidecar must carry a digest")
        if self.state != "creating" and self.digest is not None:
            raise ValueError("active/tombstone sidecar must not carry a digest")


class FaultInjector:
    """生产默认 no-op；测试实现可在命名落盘点终止进程模拟崩溃。"""

    def hit(self, point: str) -> None:
        pass


# legacy 归一化规则：registry key -> 处理方式（规格 §10.3）
MIGRATION_RULES = {
    "find_jade.invite_info_list": "raise",
    "find_jade.sup_account_list": "raise",
    "multi_daily_alt_acc.sup_account_list": "raise",
    "multi_tasks.sup_account_list": "raise",
    "meta_demon.md_strategies": "exact",
    "master_disciple.disciple_account_list": "contiguous",
    "bondling_fairyland.switch_account_list": "single",
    "abyss_shadows.switch_account_list": "single",
}


def _config_bytes(canonical: dict) -> bytes:
    """与 utils._write_file_unlocked 的 json 分支完全一致的落盘字节。"""
    s = json.dumps(canonical, indent=2, ensure_ascii=False, sort_keys=False, default=str)
    return s.encode("utf-8")


def _walk_get(data: dict, path: tuple[str, ...]) -> Optional[dict]:
    """沿 tuple path 读取嵌套节点；任一缺失返回 None。"""
    node = data
    for key in path:
        if not isinstance(node, dict) or key not in node:
            return None
        node = node[key]
    return node


def _member_indexes(node: dict, prefix: str) -> Optional[list[int]]:
    """收集 field_N 的规范数字索引；存在非规范 key（如 _01/_abc）返回 None 表示损坏。"""
    indexes = []
    for key in node:
        if not key.startswith(prefix):
            continue
        suffix = key[len(prefix):]
        if not suffix or not suffix.isdigit() or int(suffix) < 1 or suffix != str(int(suffix)):
            return None
        indexes.append(int(suffix))
    return sorted(indexes)


def _set_count(raw: dict, count_path: tuple[str, ...], value: int) -> None:
    """就地写 count 值；父节点非 dict 视为损坏输入。"""
    node = raw
    for key in count_path[:-1]:
        if not isinstance(node.get(key), dict):
            raise ConfigValidationError(f"invalid count path {'/'.join(count_path)}")
        node = node[key]
    node[count_path[-1]] = value


def _normalize_dynamic_lists(raw: dict, profile: ValidationProfile = None) -> None:
    """按 legacy 归一化规则调整动态列表（就地修改深拷贝后的 raw）。

    - raise：旧 validator"只补不截"，有效成员数大于 count 时把 count 上调到成员数，保留全部账号；
    - exact（MetaDemon）：按其精确 count 语义只保留 1..count，历史 count=0 的默认 _1 占位被删除；
    - contiguous（MasterDisciple）：保留全部连续成员；
    - single（Bondling/Abyss）：只保留 _1；
    - 空洞、非连续、越界、非规范 key 一律抛 ConfigValidationError 中止迁移（fail closed）。
    """
    profile = profile or DEFAULT_CONFIG_PROFILE
    for entry in profile.dynamic_path_sets:
        rule = MIGRATION_RULES.get(entry.key)
        if rule is None:
            continue
        node = _walk_get(raw, entry.member_path[:-1])
        if not isinstance(node, dict):
            continue
        field = entry.member_path[-1]
        prefix = field + "_"
        indexes = _member_indexes(node, prefix)
        if indexes is None:
            raise ConfigValidationError(f"dynamic list {entry.key} has non-canonical member keys")
        member_count = len(indexes)
        contiguous = indexes == list(range(1, member_count + 1))
        if rule in ("raise", "exact") and entry.count_path is not None:
            count = _walk_get(raw, entry.count_path)
            if not isinstance(count, int) or isinstance(count, bool) or count < 0:
                raise ConfigValidationError(f"dynamic count {'/'.join(entry.count_path)} is invalid")
            if not contiguous:
                raise ConfigValidationError(f"dynamic list {entry.key} has gapped member indexes")
            if rule == "raise":
                if member_count > count:
                    # count 上调到成员数以保留全部账号
                    _set_count(raw, entry.count_path, member_count)
                elif member_count < count:
                    raise ConfigValidationError(
                        f"dynamic list {entry.key} missing members for count {count}")
            else:
                # exact：只保留 1..count，删除 count 之外的成员（含 count=0 的占位）
                for index in indexes[count:]:
                    del node[f"{field}_{index}"]
                if member_count < count:
                    raise ConfigValidationError(
                        f"dynamic list {entry.key} missing members for count {count}")
        elif rule == "contiguous":
            if not contiguous:
                raise ConfigValidationError(f"dynamic list {entry.key} must be contiguous from 1")
        elif rule == "single":
            for index in indexes:
                if index != 1:
                    del node[f"{field}_{index}"]
            if 1 not in indexes:
                raise ConfigValidationError(f"dynamic list {entry.key} must keep exactly _1")


def _atomic_write_bytes(path: Path, data: bytes) -> None:
    """原字节原子写（用于无扩展名 marker 等文件）。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    with atomic_write(str(path), mode="wb", overwrite=True) as f:
        f.write(data)


class GenerationManager:
    """配置身份与生命周期事务管理器（本 Task 不接生产 Config/ConfigStore）。"""

    def __init__(
        self,
        config_root: Path,
        fault_injector: FaultInjector = None,
        profile: ValidationProfile = None,
        timeout: float = 10.0,
    ):
        self.config_root = Path(config_root)
        self.profile = profile or DEFAULT_CONFIG_PROFILE
        self.fault_injector = fault_injector or FaultInjector()
        self._timeout = timeout
        self.generations_dir = self.config_root / ".generations"
        self.backups_dir = self.generations_dir / "backups"
        self.transactions_dir = self.generations_dir / "transactions"
        self.locks_dir = self.generations_dir / "locks"
        # 内容级校验失败被隔离的身份：{name: 异常}，由 sync_identity_state 刷新。
        # 这些身份不会被枚举/load/启动，调用方（MainManager）负责上报给用户。
        self.quarantined_identities: dict[str, Exception] = {}
        # 目录延后到首次 I/O 前创建（_ensure_dirs），保证 import 主进程模块
        # （模块级 MainManager() 构造 ConfigStore）不产生 .generations 目录副作用。

    def _ensure_dirs(self) -> None:
        """创建事务目录树；调用方必须在使用 lifecycle/migration 锁或写入前调用。"""
        for directory in (self.config_root, self.generations_dir,
                          self.backups_dir, self.transactions_dir, self.locks_dir):
            directory.mkdir(parents=True, exist_ok=True)

    # ---- 路径与锁 ----

    @staticmethod
    def _validate_name(name: str) -> str:
        """校验配置身份名，确保所有派生路径都停留在 config_root 内。"""
        reserved = set('/\\:*?"<>|')
        if (not isinstance(name, str) or not name or name != name.strip()
                or "." in name or any(ch in reserved for ch in name)
                or any(ord(ch) < 32 for ch in name)):
            raise ConfigIdentityNameError(f"invalid config name: {name!r}")
        return name

    @staticmethod
    def _validate_txid(txid: str) -> str:
        """journal 文件名固定使用 UUID transaction_id。"""
        try:
            parsed = uuid.UUID(txid)
        except (TypeError, ValueError, AttributeError) as exc:
            raise ConfigGenerationError(f"invalid transaction id: {txid!r}") from exc
        if str(parsed) != txid:
            raise ConfigGenerationError(f"invalid transaction id: {txid!r}")
        return txid

    def _same_config_identity(self, left: str, right: str) -> bool:
        """按当前平台文件系统语义判断两个配置名是否指向同一物理身份。"""
        left_path = os.path.abspath(self._config_path(left))
        right_path = os.path.abspath(self._config_path(right))
        return os.path.normcase(left_path) == os.path.normcase(right_path)

    def _reject_reserved_identity(self, name: str) -> None:
        """禁止删除或重命名 template 保留身份，包含大小写等文件系统别名。"""
        if self._same_config_identity(name, "template"):
            raise ConfigIdentityNameError("template is a reserved identity")

    def _sidecar_path(self, name: str) -> Path:
        return self.generations_dir / f"{self._validate_name(name)}.json"

    def _config_path(self, name: str) -> Path:
        return self.config_root / f"{self._validate_name(name)}.json"

    def _initialized_path(self) -> Path:
        return self.generations_dir / ".initialized"

    def _journal_path(self, txid: str) -> Path:
        return self.transactions_dir / f"{self._validate_txid(txid)}.json"

    def _lifecycle_lock(self, name: str) -> FileLock:
        """OAS 与管家共用单配置锁；运行期回写只锁提交校验、备份和原子覆盖。"""
        self._ensure_dirs()
        return FileLock(str(self.locks_dir / f"{self._validate_name(name)}.lock"), timeout=self._timeout)

    def _migration_lock(self) -> FileLock:
        self._ensure_dirs()
        return FileLock(str(self.generations_dir / "migration.lock"), timeout=self._timeout)

    def _validation_lock(self) -> FileLock:
        """全局身份锁：身份恢复/校验与 create/delete/rename 事务互斥。

        lifecycle lock 是 per-name 的，无法覆盖全局扫描；这把锁确保身份校验期间
        没有并发生命周期事务的合法中间态（creating sidecar、config 已写未切 active、
        tombstone 未删配置）被误判为身份损坏。与 lifecycle lock 的获取顺序恒为
        先 validation 后 lifecycle，避免死锁。
        放 generations_dir（与 migration.lock 同命名空间）而不是 locks_dir，
        避免与某个名为 identity 的配置的 lifecycle lock 路径冲突。
        """
        self._ensure_dirs()
        return FileLock(str(self.generations_dir / "identity.lock"), timeout=self._timeout)

    @contextmanager
    def identity_lifecycle_lock(self, name: str):
        """统一事务锁序：identity → pending journal 恢复 → per-name lifecycle。

        普通读写路径（load / patch / save）只恢复与本 name 相关的残留状态：
        - transactions/ 里的 rename journal 仍每次重放（稳态下目录为空，一次 glob 即短路）；
        - creating/tombstone sidecar 只在已持有该 name lifecycle 锁时就地恢复。

        原实现在每次取锁前都做全量 recover_lifecycle_transactions()，会为每个身份
        额外获取一次 lifecycle FileLock，使 load() 退化为 O(N) 次取锁、
        active_config_names()（内部逐个 load）退化为 O(N²)；本机 7 份配置时
        GET args 最坏要取 150 次文件锁。全量恢复交给 initialize()/sync_identity_state()
        与 create/delete/rename 承担，语义不变但普通路径恒定 2 次取锁。
        """
        name = self._validate_name(name)
        with self._validation_lock():
            self._recover_pending_journals()
            with self._lifecycle_lock(name):
                self._recover_name_sidecar(name)
                yield

    # ---- sidecar / journal 读写 ----

    def _write_sidecar(self, name: str, record: GenerationRecord) -> None:
        self._ensure_dirs()
        payload = {"generation": record.generation, "state": record.state, "digest": record.digest}
        _write_file_unlocked(str(self._sidecar_path(name)), payload)

    def _read_sidecar(self, name: str) -> Optional[GenerationRecord]:
        """读取 sidecar；文件缺失返回 None，内容损坏抛 ConfigGenerationError（fail closed）。"""
        path = self._sidecar_path(name)
        if not path.exists():
            return None
        try:
            raw = _read_file_unlocked(str(path))
        except (json.JSONDecodeError, OSError, UnicodeDecodeError) as exc:
            raise ConfigGenerationError(f"{name}: corrupt sidecar") from exc
        if not isinstance(raw, dict):
            raise ConfigGenerationError(f"{name}: corrupt sidecar")
        try:
            generation = raw.get("generation")
            state = raw.get("state")
            digest = raw.get("digest")
            if (set(raw) != {"generation", "state", "digest"}
                    or not isinstance(generation, str) or not generation
                    or not isinstance(state, str)
                    or (digest is not None and not isinstance(digest, str))):
                raise ConfigGenerationError(f"{name}: corrupt sidecar")
            return GenerationRecord(generation, state, digest)
        except ConfigGenerationError:
            raise
        except ValueError as exc:
            raise ConfigGenerationError(f"{name}: corrupt sidecar") from exc

    def _write_journal(self, txid: str, journal: dict) -> None:
        self._ensure_dirs()
        _write_file_unlocked(str(self._journal_path(txid)), journal)

    def _config_digest(self, name: str) -> str:
        """返回配置文件原字节 SHA-256；恢复 CAS 使用，不做 canonical 重写。"""
        path = self._config_path(name)
        if not path.exists():
            raise ConfigGenerationError(f"{name}: config not found")
        return hashlib.sha256(path.read_bytes()).hexdigest()

    def _parse_rename_journal(self, journal_path: Path, raw: object) -> tuple[dict, dict, str]:
        """严格解析 rename journal；任何字段缺失、冗余或类型错误均 fail closed。"""
        if not isinstance(raw, dict) or set(raw) != {
            "transaction_id", "operation", "state", "source", "target"
        }:
            raise ConfigGenerationError(f"{journal_path.name}: corrupt journal")
        txid = raw.get("transaction_id")
        try:
            self._validate_txid(txid)
        except ConfigGenerationError as exc:
            raise ConfigGenerationError(f"{journal_path.name}: corrupt journal") from exc
        if (txid != journal_path.stem
                or raw.get("operation") != "rename"
                or raw.get("state") not in ("prepared", "committed")):
            raise ConfigGenerationError(f"{journal_path.name}: corrupt journal")
        source = raw.get("source")
        target = raw.get("target")
        for item in (source, target):
            if not isinstance(item, dict) or set(item) != {"name", "generation", "digest"}:
                raise ConfigGenerationError(f"{journal_path.name}: corrupt journal")
            name = item.get("name")
            generation = item.get("generation")
            digest = item.get("digest")
            self._validate_name(name)
            if (not isinstance(generation, str) or not generation
                    or not isinstance(digest, str) or len(digest) != 64
                    or any(ch not in "0123456789abcdef" for ch in digest)):
                raise ConfigGenerationError(f"{journal_path.name}: corrupt journal")
        if self._same_config_identity(source["name"], target["name"]):
            raise ConfigGenerationError(f"{journal_path.name}: corrupt journal")
        return source, target, raw["state"]

    # ---- 读取 ----

    def read_active_generation(self, name: str) -> Optional[GenerationRecord]:
        """读取当前 sidecar 记录；不存在返回 None（不区分状态）。"""
        return self._read_sidecar(name)

    def load_canonical(self, name: str) -> Optional[dict]:
        """返回 active 身份的严格 canonical；tombstone/缺失返回 None，身份损坏抛错。

        磁盘残留的失效字段（结构演化遗留）会先备份原字节再剔除，不隔离该身份。
        """
        record = self._read_sidecar(name)
        if record is None or record.state != "active":
            return None
        config_path = self._config_path(name)
        if not config_path.exists():
            raise ConfigGenerationError(f"{name}: active sidecar but config missing")
        raw = self._read_raw_json(name)
        if raw.get("config_name") != name:
            raise ConfigGenerationError(f"{name}: config_name mismatch with sidecar name")
        canonical = self._validate_with_repair(name, raw)
        return canonical

    def _validate_with_repair(self, name: str, raw: dict) -> dict:
        """带自动修复的严格校验：剔除 unknown field 时备份原始配置并告警。

        修复只发生在内存（validate_persisted_config 深拷贝后再剔）；canonical
        落盘与否由调用方的现有写盘机制决定。新导入尚无磁盘文件时备份传入内容，
        已有配置仍保留磁盘原字节；其它校验失败仍原样抛出（fail closed）。
        """
        repaired_paths: list = []
        profile = copy.copy(self.profile)
        profile.repaired_paths = repaired_paths
        _model, canonical = validate_persisted_config(raw, name, profile)
        if repaired_paths:
            # 新名称导入在首次落盘前就会清理旧字段，不能读取尚不存在的目标文件。
            # 复用迁移备份目录保存未清理的完整内容，同名已有备份仍不覆盖。
            try:
                original_bytes = self._config_path(name).read_bytes()
            except FileNotFoundError:
                original_bytes = _config_bytes(raw)
            backup_path = self._write_backup(name, original_bytes)
            for path in repaired_paths:
                logger.warning(
                    f"[{name}] unknown field stripped: {'/'.join(path)}; "
                    f"原字节备份: {backup_path}"
                )
        return canonical

    def _read_raw_json(self, name: str) -> dict:
        config_path = self._config_path(name)
        if not config_path.exists():
            raise ConfigGenerationError(f"{name}: config not found")
        try:
            raw = json.loads(config_path.read_bytes().decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ConfigGenerationError(f"{name}: invalid json") from exc
        if not isinstance(raw, dict):
            raise ConfigGenerationError(f"{name}: config root must be an object")
        return raw

    def _logically_exists(self, name: str) -> bool:
        """目标逻辑存在性：tombstone 允许同名重建，其余状态与物理文件均视为已存在。"""
        record = self._read_sidecar(name)
        if record is None:
            return self._config_path(name).exists()
        if record.state == "tombstone":
            return False
        return True

    # ---- migration ----

    def ensure_migration_complete(self) -> None:
        """首次 migration：原字节备份 → legacy 归一化 → strict 校验 → active sidecar → marker。

        marker 存在即返回（幂等，不静默生成新 token）；任一文件损坏/归一化失败抛错中止（fail closed）。
        """
        with self._migration_lock():
            if self._initialized_path().exists():
                return
            for config_path in sorted(self.config_root.glob("*.json")):
                self._migrate_one(config_path.stem)
            self.fault_injector.hit("migration.before_initialized_marker")
            _atomic_write_bytes(self._initialized_path(), b'{"initialized": true}')

    def _migrate_one(self, name: str) -> None:
        """迁移单份配置：原字节备份 → legacy 归一化 → strict 校验 → 落盘 → active sidecar。

        内容级失败（JSON 损坏、legacy 归一化失败、strict 校验不通过）只隔离该身份：
        配置原字节保持不变（备份已在归一化前写入），但仍写 active sidecar 以维持
        「每份配置文件都有身份」的结构不变量，随后由 survey_active_identities 识别为
        quarantined。这样一份含历史遗留字段的坏配置不会让整批 migration 无法完成
        （marker 写不进去 → 每次启动重试重跑 → 服务永远起不来），也不会阻断其余
        健康配置升级；该身份自身仍然不会被枚举、load 或启动。
        """
        # 首次迁移也必须先取得共享锁，不能先归一化改写、最后才等管家释放锁。
        with self._lifecycle_lock(name):
            config_path = self._config_path(name)
            raw_bytes = config_path.read_bytes()
            self._write_backup(name, raw_bytes)
            self.fault_injector.hit("migration.after_backup")
            try:
                canonical = self._normalize_for_migration(name, raw_bytes)
            except (ConfigGenerationError, ConfigValidationError) as exc:
                self.quarantined_identities[name] = exc
            else:
                new_bytes = _config_bytes(canonical)
                if new_bytes != raw_bytes:
                    _write_file_unlocked(str(config_path), canonical)
            self.fault_injector.hit("migration.after_normalized_config")
            self._write_sidecar(name, GenerationRecord(str(uuid.uuid4()), "active", None))
            self.fault_injector.hit("migration.after_active_sidecar")

    def _normalize_for_migration(self, name: str, raw_bytes: bytes) -> dict:
        """legacy 归一化 + strict 校验，返回可落盘 canonical；内容非法抛内容级异常。

        unknown field 走自动修复（原字节备份已在调用方 _migrate_one 落盘前完成，
        剔除仅发生在内存副本）。
        """
        try:
            raw = json.loads(raw_bytes.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ConfigGenerationError(f"{name}: invalid json") from exc
        if not isinstance(raw, dict):
            raise ConfigGenerationError(f"{name}: config root must be an object")
        # 必须先 legacy normalize（含 alias 迁移），再 strict validate
        normalized = normalize_legacy_config(raw, name, self.profile)
        _normalize_dynamic_lists(normalized, self.profile)
        canonical = self._validate_with_repair(name, normalized)
        return canonical

    def _write_backup(self, name: str, raw_bytes: bytes) -> Path:
        """exclusive create 写原字节备份；该 name 已有备份则跳过（重复恢复不覆盖原备份）。"""
        self._ensure_dirs()
        existing = sorted(self.backups_dir.glob(f"{name}.*.json"))
        if existing:
            # 崩溃恢复重跑时 config 可能已被部分归一化，再次备份会污染原字节备份；只保留首次备份
            return existing[0]
        sha = hashlib.sha256(raw_bytes).hexdigest()
        backup_path = self.backups_dir / f"{name}.{sha}.json"
        with atomic_write(str(backup_path), mode="wb", overwrite=False) as f:
            f.write(raw_bytes)
        return backup_path

    # ---- 崩溃恢复 ----

    def recover_lifecycle_transactions(self) -> None:
        """全量恢复：先恢复 rename journal（prepared 回滚 / committed 前滚），再逐身份处理 create/delete 遗留 sidecar。"""
        self._recover_pending_journals()
        for sidecar_path in sorted(self.generations_dir.glob("*.json")):
            name = sidecar_path.stem
            with self._lifecycle_lock(name):
                self._recover_name_sidecar(name)

    def _recover_pending_journals(self) -> None:
        """重放 transactions/ 下全部 rename journal；稳态目录为空时只花一次 glob。"""
        self._ensure_dirs()
        for journal_path in sorted(self.transactions_dir.glob("*.json")):
            self._recover_rename_journal(journal_path)

    def _recover_name_sidecar(self, name: str) -> None:
        """恢复单个身份的 creating/tombstone 残留；调用方必须已持有该 name lifecycle 锁。"""
        record = self._read_sidecar(name)
        if record is None:
            return
        if record.state == "creating":
            self._recover_creating(name, record)
        elif record.state == "tombstone":
            self._recover_tombstone(name)

    def _recover_creating(self, name: str, record: GenerationRecord) -> None:
        config_path = self._config_path(name)
        if config_path.exists():
            actual = hashlib.sha256(config_path.read_bytes()).hexdigest()
            if actual == record.digest:
                # creating + 匹配 digest 的完整配置：前滚为 active
                self._write_sidecar(name, GenerationRecord(record.generation, "active", None))
                return
            config_path.unlink()
        # 其余情况删除残缺配置并写带新 generation 的 tombstone（旧 generation 永久不匹配）
        self._write_sidecar(name, GenerationRecord(str(uuid.uuid4()), "tombstone", None))

    def _recover_tombstone(self, name: str) -> None:
        config_path = self._config_path(name)
        if config_path.exists():
            config_path.unlink()

    def _recover_rename_journal(self, journal_path: Path) -> None:
        try:
            raw = json.loads(journal_path.read_text(encoding="utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ConfigGenerationError(f"{journal_path.name}: corrupt journal") from exc
        source, target, state = self._parse_rename_journal(journal_path, raw)
        # source==target 的 journal 会在双锁排序后重复获取同一把锁并阻塞 10s；
        # 必须在取锁前判为损坏快速失败，避免中断后续所有 journal/sidecar 恢复
        src_lock = self._lifecycle_lock(source["name"])
        tgt_lock = self._lifecycle_lock(target["name"])
        # 按 canonical lock path 排序获取，避免与并发事务死锁
        ordered = sorted((src_lock, tgt_lock), key=lambda lock: lock.lock_file)
        with ordered[0]:
            with ordered[1]:
                if state == "prepared":
                    self._rollback_target(target)
                else:
                    self._forward_roll_rename(source, target)
                if journal_path.exists():
                    journal_path.unlink()

    def _rollback_target(self, target: dict) -> None:
        """prepared 阶段只可能已写目标 creating sidecar/config：完全回滚删除目标 artifacts。"""
        record = self._read_sidecar(target["name"])
        if record is None or record.state == "tombstone":
            # prepared 写入 target creating 之前，目标可能不存在或保留旧 tombstone；均未被事务触碰。
            return
        if record.generation != target["generation"]:
            raise ConfigGenerationError("rename rollback: target generation changed")
        if record.state != "creating":
            # prepared 协议不可能产生 active；若出现说明 journal/sidecar 状态已损坏。
            raise ConfigGenerationError("rename rollback: target state changed")
        tgt_path = self._config_path(target["name"])
        if tgt_path.exists() and self._config_digest(target["name"]) != target["digest"]:
            raise ConfigGenerationError("rename rollback: target digest mismatch")
        if tgt_path.exists():
            tgt_path.unlink()
        sidecar = self._sidecar_path(target["name"])
        if sidecar.exists():
            sidecar.unlink()

    def _forward_roll_rename(self, source: dict, target: dict) -> None:
        """committed 是提交点：校验目标 digest 后幂等前滚（目标 active、源 tombstone、源文件删除）。"""
        tgt_path = self._config_path(target["name"])
        if not tgt_path.exists() or self._config_digest(target["name"]) != target["digest"]:
            raise ConfigGenerationError("rename forward-roll: target digest mismatch")
        target_record = self._read_sidecar(target["name"])
        if (target_record is None or target_record.generation != target["generation"]
                or target_record.state not in ("creating", "active")):
            raise ConfigGenerationError("rename forward-roll: target identity changed")
        source_record = self._read_sidecar(source["name"])
        if (source_record is None or source_record.generation != source["generation"]
                or source_record.state not in ("active", "tombstone")):
            raise ConfigGenerationError("rename forward-roll: source identity changed")
        src_path = self._config_path(source["name"])
        if source_record.state == "active":
            if not src_path.exists() or self._config_digest(source["name"]) != source["digest"]:
                raise ConfigGenerationError("rename forward-roll: source digest changed")
        elif src_path.exists() and self._config_digest(source["name"]) != source["digest"]:
            raise ConfigGenerationError("rename forward-roll: source digest changed")
        self._write_sidecar(target["name"], GenerationRecord(target["generation"], "active", None))
        self._write_sidecar(source["name"], GenerationRecord(source["generation"], "tombstone", None))
        if src_path.exists():
            src_path.unlink()

    # ---- 身份校验 ----

    def sync_identity_state(self) -> None:
        """migration → 生命周期恢复 → 身份校验 在同一把全局身份锁内原子执行。

        这把锁同时被 create/delete/rename 持有：锁内扫描时不可能有并发事务的合法
        中间态（config 已写但 active sidecar 未切换、tombstone 已写但配置未删），
        因此 validate 看到的就是稳定身份快照，既消除误判又保持 fail closed。
        内容级校验失败的身份记入 quarantined_identities 供调用方上报，不中断本次同步。
        """
        with self._validation_lock():
            self.ensure_migration_complete()
            self.recover_lifecycle_transactions()
            self.quarantined_identities = self.survey_active_identities()

    def survey_active_identities(self) -> dict[str, Exception]:
        """扫描全部身份，返回 {name: 内容级失败异常}；结构级损坏仍抛错。

        结构级身份损坏（残留 creating、tombstone 仍有物理配置、active 缺文件、
        配置存在但 sidecar 缺失/损坏）说明生命周期不变量已被破坏，无法判断磁盘能否
        安全写入，必须 fail closed。

        内容级失败（JSON 损坏、config_name 不一致、strict 校验不通过）只隔离该身份：
        它不会被 active_config_names 枚举、不会被 load、也不会创建/启动实例，因此不存在
        用陈旧模型覆盖磁盘的风险。没有理由让一份历史遗留字段导致的坏配置，
        连带全部健康配置一起不可用。
        """
        quarantined: dict[str, Exception] = {}
        seen = set()
        for sidecar_path in sorted(self.generations_dir.glob("*.json")):
            name = sidecar_path.stem
            seen.add(name)
            record = self._read_sidecar(name)
            if record is None:
                continue
            config_path = self._config_path(name)
            if record.state == "active":
                if not config_path.exists():
                    raise ConfigGenerationError(f"{name}: active sidecar but config missing")
                try:
                    self.load_canonical(name)
                except (ConfigGenerationError, ConfigValidationError) as exc:
                    quarantined[name] = exc
            elif record.state == "tombstone":
                if config_path.exists():
                    raise ConfigGenerationError(f"{name}: tombstone but config exists")
            else:
                raise ConfigGenerationError(f"{name}: leftover creating sidecar")
        for config_path in sorted(self.config_root.glob("*.json")):
            name = config_path.stem
            if name not in seen:
                raise ConfigGenerationError(f"{name}: config exists but sidecar missing")
        return quarantined

    def validate_active_identities(self) -> None:
        """严格身份校验：任何身份不稳定（含内容级校验失败）都抛错（fail closed）。

        供测试与显式体检使用；生产启动路径走 sync_identity_state，内容级失败在那里
        被隔离而不是阻断整个服务。
        """
        quarantined = self.survey_active_identities()
        if quarantined:
            # 保留原始异常类型（ConfigGenerationError / ConfigValidationError），
            # 便于调用方按类别映射；多份损坏时按名称取第一个上报。
            raise quarantined[sorted(quarantined)[0]]

    # ---- 生命周期操作 ----

    def create_config(self, name: str, raw: dict) -> str:
        """creating → config write → active 的显式创建；返回新 generation。

        全程持有全局身份锁，保证并发 validate 不会把 config 已写未切 active 的
        中间态误判为身份损坏。
        """
        name = self._validate_name(name)
        with self._validation_lock():
            self.recover_lifecycle_transactions()
            with self._lifecycle_lock(name):
                if self._logically_exists(name):
                    raise ConfigIdentityConflictError(f"{name} already exists")
                canonical = self._validate_with_repair(name, raw)
                config_bytes = _config_bytes(canonical)
                digest = hashlib.sha256(config_bytes).hexdigest()
                generation = str(uuid.uuid4())
                self._write_sidecar(name, GenerationRecord(generation, "creating", digest))
                self.fault_injector.hit("create.after_creating_sidecar")
                _write_file_unlocked(str(self._config_path(name)), canonical)
                self.fault_injector.hit("create.after_config_write")
                self._write_sidecar(name, GenerationRecord(generation, "active", None))
                self.fault_injector.hit("create.after_active_sidecar")
                return generation

    def delete_config(self, name: str) -> None:
        """tombstone 是删除提交点；随后物理删除配置。

        持全局身份锁，避免并发 validate 把 tombstone 已写配置未删的中间态误判为损坏。
        """
        name = self._validate_name(name)
        self._reject_reserved_identity(name)
        with self._validation_lock():
            self.recover_lifecycle_transactions()
            with self._lifecycle_lock(name):
                record = self._read_sidecar(name)
                if record is None:
                    raise ConfigIdentityNotFoundError(f"{name} not found")
                if record.state == "tombstone":
                    config_path = self._config_path(name)
                    if config_path.exists():
                        # 完成 tombstone 提交后崩溃遗留的物理删除，保持恢复幂等。
                        config_path.unlink()
                        return
                    raise ConfigIdentityNotFoundError(f"{name} not found")
                self._write_sidecar(name, GenerationRecord(str(uuid.uuid4()), "tombstone", None))
                self.fault_injector.hit("delete.after_tombstone")
                config_path = self._config_path(name)
                if config_path.exists():
                    config_path.unlink()
                self.fault_injector.hit("delete.after_config_unlink")

    def rename_config(self, source: str, destination: str) -> None:
        """prepared journal → 目标 creating → 目标配置 → committed → 目标 active → 源 tombstone → 源删除。

        持全局身份锁，rename 各中间态（目标 config 已写未切 active、源 tombstone
        已写源配置未删）不会与并发 validate 交错。
        """
        source = self._validate_name(source)
        destination = self._validate_name(destination)
        self._reject_reserved_identity(source)
        self._reject_reserved_identity(destination)
        with self._validation_lock():
            self.recover_lifecycle_transactions()
            if self._same_config_identity(source, destination):
                # 同一物理身份的 rename 是名称错误，不得进入 stop/双锁/事务阶段。
                raise ConfigIdentityNameError("rename source and destination must differ")
            src_lock = self._lifecycle_lock(source)
            tgt_lock = self._lifecycle_lock(destination)
            # 按 canonical lock path 排序获取 source/destination 两把 lifecycle lock
            ordered = sorted((src_lock, tgt_lock), key=lambda lock: lock.lock_file)
            with ordered[0]:
                with ordered[1]:
                    src_record = self._read_sidecar(source)
                    if src_record is None or src_record.state != "active":
                        raise ConfigIdentityNotFoundError(f"{source} is not active")
                    if self._logically_exists(destination):
                        raise ConfigIdentityConflictError(f"{destination} already exists")
                    src_raw = self._read_raw_json(source)
                    if src_raw.get("config_name") != source:
                        raise ConfigGenerationError(f"{source}: config_name mismatch")
                    src_canonical = self._validate_with_repair(source, src_raw)
                    # 计算目标 digest 与 strict 校验前先深拷贝源 canonical 并设置目标 config_name
                    tgt_raw = copy.deepcopy(src_canonical)
                    tgt_raw["config_name"] = destination
                    tgt_canonical = self._validate_with_repair(destination, tgt_raw)
                    tgt_bytes = _config_bytes(tgt_canonical)
                    tgt_digest = hashlib.sha256(tgt_bytes).hexdigest()
                    src_digest = hashlib.sha256(self._config_path(source).read_bytes()).hexdigest()
                    src_gen = src_record.generation
                    tgt_gen = str(uuid.uuid4())
                    txid = str(uuid.uuid4())
                    journal = {
                        "transaction_id": txid,
                        "operation": "rename",
                        "state": "prepared",
                        "source": {"name": source, "generation": src_gen, "digest": src_digest},
                        "target": {"name": destination, "generation": tgt_gen, "digest": tgt_digest},
                    }
                    self._write_journal(txid, journal)
                    self.fault_injector.hit("rename.after_prepared_journal")
                    self._write_sidecar(destination, GenerationRecord(tgt_gen, "creating", tgt_digest))
                    self.fault_injector.hit("rename.after_target_creating")
                    _write_file_unlocked(str(self._config_path(destination)), tgt_canonical)
                    self.fault_injector.hit("rename.after_target_config")
                    journal["state"] = "committed"
                    self._write_journal(txid, journal)
                    self.fault_injector.hit("rename.after_committed_journal")
                    self._write_sidecar(destination, GenerationRecord(tgt_gen, "active", None))
                    self.fault_injector.hit("rename.after_target_active")
                    self._write_sidecar(source, GenerationRecord(src_gen, "tombstone", None))
                    self.fault_injector.hit("rename.after_source_tombstone")
                    src_path = self._config_path(source)
                    if src_path.exists():
                        src_path.unlink()
                    self.fault_injector.hit("rename.after_source_unlink")
                    journal_path = self._journal_path(txid)
                    if journal_path.exists():
                        journal_path.unlink()
