# This Python file uses the following encoding: utf-8
# @author runhey
# github https://github.com/runhey
import copy
import datetime
import json
import operator
import threading
import random

from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable
from cached_property import cached_property
from threading import Lock

from module.base.filter import Filter
from module.config.account_login import account_identity, apply_login_times, login_moment
from module.config.config_generation import ConfigGenerationError
from module.config.config_operations import MISSING, _eq, get_path, set_path
from module.config.config_reload import COLD, HOT, ReloadPolicy, coerce_path, default_reload_policy
from module.config.config_store import (
    ConfigGenerationMismatchError,
    ConfigJsonError,
    ConfigNotFoundError,
    ConfigStore,
)
from module.config.config_validation import ConfigValidationError
from module.config.config_updater import ConfigUpdater
from module.config.config_manual import ConfigManual
from module.config.config_watcher import ConfigWatcher
from module.config.config_menu import ConfigMenu
from module.config.config_model import ConfigModel
from module.config.config_state import ConfigState, ConfigStateResult
from module.config.scheduler import TaskScheduler
from module.config.utils import *
from module.notify.notify import Notifier

from module.exception import RequestHumanTakeover, ScriptError
from module.logger import logger


class Function:
    def __init__(self, key: str, data: dict):
        """
        输入的是每一个ConfigModel的一个字段对象
        :param data:
        """
        if isinstance(data, dict) is False:
            self.enable = False
            self.command = "Unknown"
            self.next_run = DEFAULT_TIME
            return
        if data.get("scheduler") is None:
            self.enable = False
            self.command = "Unknown"
            self.next_run = DEFAULT_TIME
            return

        self.enable: bool = data['scheduler']['enable']
        self.command: str = ConfigModel.type(key)
        next_run = data['scheduler']['next_run']
        if isinstance(next_run, str):
            next_run = datetime.strptime(next_run, "%Y-%m-%d %H:%M:%S")
        self.next_run: datetime = next_run
        priority = data['scheduler']['priority']
        if isinstance(priority, str):
            priority = int(priority)
        self.priority: int = priority
        if not isinstance(self.priority, int):
            logger.error(f"Invalid priority: {self.priority}")

        # self.enable = deep_get(data, keys="Scheduler.Enable", default=False)
        # self.command = deep_get(data, keys="Scheduler.Command", default="Unknown")
        # self.next_run = deep_get(data, keys="Scheduler.NextRun", default=DEFAULT_TIME)

    def __str__(self):
        enable = "Enable" if self.enable else "Disable"
        return f"{self.command} ({enable}, {self.priority}, {str(self.next_run)})"

    __repr__ = __str__

    def __eq__(self, other):
        if not isinstance(other, Function):
            return False

        if self.command == other.command and self.next_run == other.next_run:
            return True
        else:
            return False


def name_to_function(name):
    """
    Args:
        name (str):

    Returns:
        Function:
    """
    function = Function({})
    function.command = name
    function.enable = True
    return function


def _iter_subtree_paths(node_a: dict, node_b: dict, prefix: tuple[str, ...] = ()) -> list[tuple[str, ...]]:
    """遍历两棵子树键并集的所有路径，用于比较启动快照与磁盘 COLD 子树的差异。"""
    keys: set = set()
    if isinstance(node_a, dict):
        keys.update(node_a)
    if isinstance(node_b, dict):
        keys.update(node_b)
    paths: list[tuple[str, ...]] = []
    for key in sorted(keys):
        path = prefix + (key,)
        paths.append(path)
        a = node_a.get(key) if isinstance(node_a, dict) else None
        b = node_b.get(key) if isinstance(node_b, dict) else None
        if isinstance(a, dict) or isinstance(b, dict):
            paths.extend(_iter_subtree_paths(
                a if isinstance(a, dict) else {},
                b if isinstance(b, dict) else {},
                path,
            ))
    return paths


def _blocked_overlaps(blocked_path, changed_set: set) -> bool:
    """判断 blocked 路径是否与 HOT 变更路径有交集。

    兼容 REPLACE_PATH_SET 的路径元组（path 是多个 tuple path 的 tuple）。
    """
    if isinstance(blocked_path, tuple) and blocked_path and all(isinstance(x, tuple) for x in blocked_path):
        return any(x in changed_set for x in blocked_path)
    return blocked_path in changed_set


# 需要在调度层做"禁止运行时间段"保护的任务名 -> 配置子模型名。
# 命中禁止区间时调度层会把任务推迟到区间结束，避免在区间内因游戏未运行触发 Restart 造成顶号。
# 子模型需提供 forbidden_time_enable(bool) 与 forbidden_time_range(str) 字段。
# 同一张表也用于 get_task_random_delay 的"下次上号随机延时"注册，
# 子模型额外提供 random_delay_enable(bool)/random_delay_min(int)/random_delay_max(int) 字段。
FORBIDDEN_TIME_TASKS = {
    'KekkaiUtilize': 'utilize_config',
    'KekkaiActivation': 'activation_config',
}

# HOT 提交需连带失效的 Config 级派生缓存：(路径前缀, cached_property 名)。
# HOT 白名单现在覆盖 script.error 下的 scalar，而 notifier 是从 notify_config/notify_enable
# 构建的 cached_property；只改模型不清缓存会留下「模型已新、通知目标仍旧」的半生效状态
# （规格 §11.1 禁止）。WARM 重建在 _commit_loaded_state 里已做同样的清理。
_HOT_INVALIDATED_CACHES: tuple = ((("script", "error"), "notifier"),)


class Config(ConfigState, ConfigManual, ConfigWatcher, ConfigMenu):
    """运行期配置会话：持有 model/base/generation/mtime 与 blocked 状态。

    - model/base/generation 由 ConfigStore 读取与三方合并推进
    - save() 是后台三方保存入口，tasks 里的 self.config.save() 无需逐个改写
    - script.device 子树是 COLD：begin_device_initialization → startup_normalize
      → freeze_startup_device_snapshot 划定启动快照，reload 永不让外部 device 修改进入运行模型
    """

    def __init__(self, config_name: str, task=None, store: ConfigStore = None,
                 reload_policy: ReloadPolicy = None) -> None:
        # 先初始化 session 状态字段，避免 __getattr__ 在父类 __init__ 期间递归访问 model
        self._model: ConfigModel | None = None
        self._base: dict | None = None
        self.generation: str | None = None
        self._mtime_ns: int = 0
        self.session_lock = threading.RLock()
        self.blocked_changes: list = []
        self._startup_device_snapshot = None
        self._provisional_device_snapshot = None
        self.scheduler_update_dt = None
        # HOT 两阶段提交状态（Task 5）：
        # - _refresh_revision：会话 model/base 每提交一次即推进，锁外 prepare 候选据此失效
        # - _refresh_in_progress：HOT 刷新进行中标记，作为 reentrancy guard
        # - _hot_failed_fingerprints：prepare 失败时的 disk/local 指纹，同一指纹不重复 prepare
        # - _state_reporter：HOT 提交/失败后向子进程 state_queue 上报 config_state 的可选 hook
        self._refresh_revision: int = 0
        self._refresh_in_progress: bool = False
        self._hot_failed_fingerprints: set = set()
        self._state_reporter: Callable | None = None
        # 成功切号先登记；下一次任务保存或任务收尾按配置批量回写。
        self._pending_account_logins: dict = {}
        self._pending_login_writebacks: dict = {}

        super().__init__(config_name)  # 调用 ConfigState 的初始化方法
        super(ConfigManual, self).__init__()
        super(ConfigWatcher, self).__init__()
        super(ConfigMenu, self).__init__()
        # WARM/COLD 分级热重载策略：默认 script.device 子树 COLD、HOT 由 ConfigModel
        # schema 派生（全部 scalar/Enum/单值时间叶子）、其余 WARM。
        self.reload_policy = reload_policy or default_reload_policy()
        self.store = store or ConfigStore(config_root=Path.cwd() / 'config')
        loaded = self.store.load(self.config_name)
        self._commit_loaded_state(loaded.model, copy.deepcopy(loaded.canonical), loaded)

    # ------------------------------------------------------------------ 会话状态

    def _commit_loaded_state(self, model: ConfigModel, base: dict, loaded) -> None:
        """一次性提交 model/base/generation/mtime，避免部分状态被并发观察到。"""
        # WARM 重建会替换 notifier 依赖的模型字段；丢弃旧缓存，避免继续向旧目标发送通知。
        self.__dict__.pop("notifier", None)
        self._model = model
        self._base = base
        self.generation = loaded.generation
        self._mtime_ns = loaded.mtime_ns
        # 同步推进 watcher 基线，避免同一磁盘状态被重复检测（规格 §4.4 mtime 兜底）
        self._watch_mtime_ns = loaded.mtime_ns
        self._watch_content_digest = loaded.content_digest
        # 会话 model/base 整体替换：推进 HOT refresh revision 使锁外 prepare 候选失效，
        # 并清除 prepare 失败指纹（WARM 重建已吸收磁盘值，重新分类）
        self._refresh_revision += 1
        self._hot_failed_fingerprints = set()

    @property
    def model(self) -> ConfigModel:
        return self._model

    @property
    def base(self) -> dict:
        return self._base

    def __getattr__(self, name):
        """
        一开始是打算直接继承ConfigModel的，但是pydantic会接管所有的变量
        故而选择持有ConfigModel
        :param name:
        :return:
        """
        try:
            return getattr(self.model, name)
        except AttributeError:
            # 这个导致 大量的无用log
            # logger.error(f'can not ask this variable {name}')
            return None  # 或者抛出异常，或者返回其他默认值

    @cached_property
    def lock_config(self) -> Lock:
        return Lock()

    @cached_property
    def notifier(self):
        notifier = Notifier(self.model.script.error.notify_config, enable=self.model.script.error.notify_enable)
        notifier.config_name = self.config_name.upper()
        logger.info(f'Notifier: {notifier.config_name}')
        return notifier

    # ------------------------------------------------------------------ 保存与刷新

    @property
    def generation_mismatch(self) -> bool:
        return self._generation_mismatch

    @property
    def mtime_ns(self) -> int:
        return self._mtime_ns

    def script_task(self, task: str) -> dict:
        """生成 OASX 参数，并按当前 Store active 身份注入动态实例选项。"""
        result = self.model.script_task(task)
        task_name = convert_to_underscore(task)
        if task_name == 'orochi':
            self._inject_orochi_leader_options(result)
            self._hide_orochi_team_config(result)
            return result
        if task_name != "multi_tasks":
            return result

        from tasks.MultiTasks.config import active_account_configs

        selection = self.model.multi_tasks.account_config_selection
        result["account_config_selection"] = [
            {
                "name": field_name,
                "title": config_name,
                "description": f"{config_name} 账号总数：{account_count}",
                "default": False,
                "value": getattr(selection, field_name, False),
                "type": "boolean",
            }
            for field_name, (config_name, account_count)
            in active_account_configs(self.store).items()
        ]
        return result

    def _inject_orochi_leader_options(self, result: dict) -> None:
        """把 leader_instance 参数改成当前有效实例下拉框。

        字段仍以 str 持久化；这里只修改返回给前端的 item 展示信息，避免把运行期
        实例列表固化到 Pydantic Schema。当前实例不应选择自己，已保存但暂时不在
        active 列表中的值仍保留，防止配置页把原值静默显示为空。
        """
        items = result.get('team_config') or []
        leader_item = next((item for item in items if item.get('name') == 'leader_instance'), None)
        if leader_item is None:
            return

        current = str(leader_item.get('value') or '')
        options = [
            name for name in self.store.active_config_names()
            if name != self.config_name
        ]
        if current and current not in options:
            options.append(current)
        options.insert(0, '')
        leader_item['type'] = 'enum'
        leader_item['enumEnum'] = options

    def _hide_orochi_team_config(self, result: dict) -> None:
        """单人模式只保留模式下拉框，隐藏其余组队配置，避免误改。"""
        items = result.get('team_config') or []
        if not items:
            return
        mode_item = next((item for item in items if item.get('name') == 'team_mode'), None)
        if mode_item is None:
            return
        if str(mode_item.get('value') or 'alone') != 'alone':
            return
        result['team_config'] = [mode_item]

    @property
    def pending_restart_paths(self) -> set:
        return set(self._pending_restart_paths)

    @property
    def pending_warm_paths(self) -> set:
        return set(self._pending_warm_paths)

    def save(self) -> None:
        """后台三方保存入口：以会话 base/local/generation 执行 save_background 并推进状态。"""
        if self._generation_mismatch:
            # generation mismatch 后终止该 session 后续持久化（规格 §10.3）
            return
        with self.session_lock:
            local = self.model.model_dump(mode="json")
            try:
                result = self.store.save_background(
                    self.config_name,
                    self._base,
                    local,
                    self.generation,
                    self.blocked_changes,
                    **({'login_times': self._pending_account_logins}
                       if self._pending_account_logins else {}),
                )
            except TimeoutError as e:
                # 锁超时：另一进程持锁超过 timeout，本次保存失败；继续运行会以陈旧模型
                # 持久化，停止更安全（filelock.Timeout 继承 TimeoutError）
                logger.warning(f'[{self.config_name}] config save lock timeout, stop persistence: {e}')
                self._generation_mismatch = True
                self._request_instance_stop()
                return
            except (ConfigGenerationMismatchError, ConfigGenerationError) as e:
                # 磁盘身份已变化或已 tombstone：终止持久化并请求实例停止，不自动 reload 后继续写
                logger.warning(f'[{self.config_name}] config identity changed, stop persistence: {type(e).__name__}')
                self._generation_mismatch = True
                self._request_instance_stop()
                return
            self._apply_save_result(result)
            self._pending_account_logins.clear()
            for name, updates in list(self._pending_login_writebacks.items()):
                if not updates:
                    del self._pending_login_writebacks[name]
                    continue
                # 显式账号来源也吸收本配置已确认的较新时间。
                updates = {identity: max(moment, result.login_times.get(identity, moment))
                           for identity, moment in updates.items()}
                # 失败重试也保留已合并的最新时间，不能退回登记时的旧值。
                self._pending_login_writebacks[name] = updates
                try:
                    self.store.sync_account_login_times(name, updates)
                except Exception:
                    # 来源备份或加锁失败时停止该来源改写，保留队列等待后续保存重试。
                    logger.exception(f'[{self.config_name}] 登录时间回写失败：{name}')
                else:
                    del self._pending_login_writebacks[name]

    def record_account_login(self, account, login_time=None, config_names=()) -> None:
        """仅登记确认成功的登录，多个角色及来源在保存时各合并成一次事务。"""
        identity = account_identity(account)
        moment = login_moment(login_time or datetime.now())
        if identity is None or moment is None:
            return
        with self.session_lock:
            pending = self._pending_account_logins
            pending[identity] = max(pending.get(identity, moment), moment)
            for name in config_names:
                if name and name != self.config_name:
                    target = self._pending_login_writebacks.setdefault(name, {})
                    target[identity] = max(target.get(identity, moment), moment)

    def flush_account_login_times(self) -> None:
        """任务结束时兜底保存尚未落盘的成功登录，包括后续子任务失败的角色。"""
        if self._pending_account_logins or self._pending_login_writebacks:
            self.save()

    def _apply_save_result(self, result) -> None:
        self._base = result.base
        # 原地推进账号对象，避免任务已持有的列表引用在下一次排序时仍是旧时间。
        apply_login_times(self.model, result.login_times, model=True)
        self.blocked_changes = result.blocked
        self._mtime_ns = result.mtime_ns
        # save_background 已提交该磁盘版本，同步 watcher 避免把自身保存误判为外部更新。
        # digest 由锁内采样带出：锁释放后再读文件可能读到并发进程刚写入的内容，
        # 把对方的版本误记成自己的，导致漏检一次外部变更。
        self._watch_mtime_ns = result.mtime_ns
        self._watch_content_digest = result.content_digest
        self.generation = result.generation
        # 会话 base 推进：使锁外 HOT prepare 候选失效（避免提交陈旧候选）
        self._refresh_revision += 1

    def reload(self) -> None:
        """从磁盘重载并把受保护 COLD 快照覆盖回新 model/base。

        委托 refresh_from_disk 统一完成 COLD overlay、WARM/COLD pending 重算与
        blocked/seen 清理，避免与边界刷新走两套平行状态推进（review 项 10）。
        """
        self.refresh_from_disk("reload")

    # ------------------------------------------------------------------ WARM / COLD 状态

    def report_config_changed(self, changed_paths) -> None:
        """子进程排空 config_event_queue 后记录待生效路径。

        COLD 路径只进 pending_restart；WARM 路径在任务边界前保持 pending_warm。
        仅作为事件提示，COLD pending 的权威集合由 refresh_from_disk 从磁盘对比重算。
        """
        with self.session_lock:
            for path in changed_paths:
                path = coerce_path(path)
                if self.reload_policy.classify(path) == COLD:
                    self._pending_restart_paths.add(path)
                else:
                    self._pending_warm_paths.add(path)

    def _config_load_failure_stop(self, reason: str) -> ConfigStateResult:
        """load 失败（身份损坏/JSON 损坏/校验失败/锁超时）统一按 mismatch 语义干净停止。

        锁超时意味着本次读写失败，继续运行会以陈旧模型持久化，停止更安全；
        按 reason 区分日志，避免「generation changed」误导。
        """
        logger.warning(f'[{self.config_name}] config load failed ({reason}), stop instance')
        self._generation_mismatch = True
        self._pending_restart_paths = set()
        self._pending_warm_paths = set()
        self._request_instance_stop()
        return ConfigStateResult(
            status="restart_required",
            pending_restart_paths=[],
            pending_warm_paths=[],
            mtime_ns=self._mtime_ns,
            generation_mismatch=True,
        )

    def refresh_from_disk(self, trigger: str) -> ConfigStateResult:
        """WARM 任务边界/检查点刷新：加载磁盘最新 model，覆盖回启动 COLD 快照后提交。

        - COLD pending 由“磁盘 COLD vs 启动快照”独立计算，WARM 清理不得清除；
        - generation mismatch 或配置无法加载（删除/损坏/锁超时）时终止 session 持久化并请求实例停止。
        """
        with self.session_lock:
            try:
                loaded = self.store.load(self.config_name)
            except TimeoutError as e:
                # 锁超时：另一进程持锁超过 timeout，本次读取失败，按 mismatch 语义干净停止
                return self._config_load_failure_stop(f'lock timeout: {e}')
            except (ConfigNotFoundError, ConfigGenerationError, ConfigJsonError, ConfigValidationError, OSError) as e:
                # 配置 tombstone/缺失/身份损坏/JSON 损坏/校验失败：按 mismatch 语义干净停止，
                # 不得以陈旧模型继续运行，也不让 load 异常穿透到子进程造成 traceback 崩溃
                return self._config_load_failure_stop(f'{type(e).__name__}: {e}')
            if loaded.generation != self.generation:
                # mismatch：只推进 mtime，不吸收磁盘新身份；清空 WARM pending 使终端态收敛
                self._generation_mismatch = True
                self._pending_restart_paths = self._compute_cold_pending(loaded)
                self._pending_warm_paths = set()
                self._mtime_ns = loaded.mtime_ns
                self._request_instance_stop()
                return ConfigStateResult(
                    status="restart_required",
                    pending_restart_paths=[list(p) for p in sorted(self._pending_restart_paths)],
                    pending_warm_paths=[],
                    mtime_ns=loaded.mtime_ns,
                    generation_mismatch=True,
                )

            loaded_model = loaded.model
            loaded_base = copy.deepcopy(loaded.canonical)
            protected_device = self._protected_device_snapshot()
            if protected_device is not None:
                loaded_model.script.device = copy.deepcopy(protected_device)
                loaded_base["script"]["device"] = protected_device.model_dump(mode="json")

            # WARM：提交覆盖后的 model/base；任务边界整体重载清空 blocked/deferred
            self._commit_loaded_state(loaded_model, loaded_base, loaded)
            self.blocked_changes = []
            self._pending_warm_paths = set()
            # COLD pending 独立计算，不受 WARM 清理影响
            self._pending_restart_paths = self._compute_cold_pending(loaded)

            return ConfigStateResult(
                status=self._status(),
                pending_restart_paths=[list(p) for p in sorted(self._pending_restart_paths)],
                pending_warm_paths=[],
                mtime_ns=self._mtime_ns,
                generation_mismatch=False,
            )

    def config_state(self) -> dict[str, object]:
        """返回排序去重 JSON-array paths、pending sets、mtime_ns 与最高优先级 status。

        注意：warm_pending 是子进程内部瞬时态——任务边界 refresh 会先应用 WARM 再上报，
        因此对外 WebSocket 首帧几乎只会看到 current/restart_required；COLD restart_required
        才是用户可见的"重启后生效"提示。
        """
        with self.session_lock:
            return {
                "pending_restart_paths": [list(p) for p in sorted(self._pending_restart_paths)],
                "pending_warm_paths": [list(p) for p in sorted(self._pending_warm_paths)],
                "observed_mtime_ns": self._mtime_ns,
                "status": self._status(),
            }

    def has_pending_changes(self) -> bool:
        """是否存在尚未在任务边界应用的 WARM 变更。

        COLD pending_restart 需要进程级重启才生效，不中止调度等待（wait_until）。
        """
        return bool(self._pending_warm_paths)

    def _status(self) -> str:
        """generation_mismatch > restart_required > warm_pending > current 取最高优先级。"""
        if self._generation_mismatch:
            return "restart_required"
        if self._pending_restart_paths:
            return "restart_required"
        if self._pending_warm_paths:
            return "warm_pending"
        return "current"

    def _compute_cold_pending(self, loaded) -> set:
        """磁盘 script.device 子树 vs 启动 COLD 快照的差异路径集合。

        独立于 WARM/deferred/blocked 计算；WARM 任务边界清理不得清除（规格 §11.2）。
        只对叶子值做差异比较，dict 容器节点只递归不直接上报，避免重复路径。
        """
        snapshot = self._protected_device_snapshot()
        if snapshot is None:
            return set()
        snapshot_raw = snapshot.model_dump(mode="json")
        disk_device = get_path(loaded.canonical, ("script", "device"))
        if not isinstance(disk_device, dict):
            return set()
        pending = set()
        for rel_path in _iter_subtree_paths(snapshot_raw, disk_device):
            full_path = ("script", "device") + tuple(rel_path)
            if self.reload_policy.classify(full_path) != COLD:
                continue
            disk_val = get_path(disk_device, rel_path)
            snapshot_val = get_path(snapshot_raw, rel_path)
            if isinstance(disk_val, dict) or isinstance(snapshot_val, dict):
                continue
            if not _eq(disk_val, snapshot_val):
                pending.add(full_path)
        return pending

    # ------------------------------------------------------------------ HOT 热重载

    def _increment_refresh_revision_for_test(self) -> None:
        """测试专用：手动推进 refresh revision，模拟 prepare 期间会话被其他刷新修改。"""
        with self.session_lock:
            self._refresh_revision += 1

    @staticmethod
    def _model_get(model, path: tuple[str, ...]):
        """沿 canonical tuple path 读取 Pydantic 模型字段值（缺失抛 AttributeError）。"""
        node = model
        for key in path:
            node = getattr(node, key)
        return node

    def _resolve_model_slots(self, model, paths, candidate) -> list:
        """解析 HOT 路径对应的「宿主模型、字段名、候选新值」三元组，不做任何写入。

        原地赋值方案（§11.1）要求提交段保持原子性：本方法是「可能失败」的一趟，
        全程只 getattr，路径与模型结构不匹配时在这里抛 AttributeError，此时运行态零变更；
        调用方拿到完整三元组列表后再统一 setattr，那一趟不会失败。
        :param model: 会话运行模型（写入目标）
        :param paths: 需要同步的 canonical tuple path 列表
        :param candidate: 已校验的候选模型（取值来源）
        :return: [(host_model, field_name, value), ...]
        """
        slots: list = []
        for path in paths:
            # 宿主定位到叶子的父节点；叶子本身用 setattr 写，不能提前 getattr
            host = model
            for key in path[:-1]:
                host = getattr(host, key)
            value = self._model_get(candidate, path)
            slots.append((host, path[-1], value))
        return slots

    def _set_model_value(self, model, path: tuple[str, ...], value):
        """把单个 canonical tuple path 的字段原地赋值为 value，返回同一个根模型实例。

        原地赋值而非 model_copy 重建（§11.1）：任务里大量存在
        `con = self.config.<task>` 这类把配置子模型捕获到局部变量或 cached_property
        的写法，重建会换掉对象标识使捕获者读到旧副本，HOT 白名单开了也不生效；
        原地写则所有捕获同一对象的读法立即看到新值，无需逐任务改读取方式。

        value 必须已是 Pydantic 字段接受的类型（来自候选模型的已校验值）；
        ConfigBase 未设 frozen/validate_assignment，故此处不触发重校验，
        也不会对运行模型整体重校验破坏 transient 状态
        （规格 §12 禁止把 canonical 动态 key 直接反向写入模型）。

        返回根模型是为兼容 `self._model = self._set_model_value(...)` 的既有调用形态；
        原地写下返回值与入参是同一实例。
        """
        host = model
        for key in path[:-1]:
            host = getattr(host, key)
        setattr(host, path[-1], value)
        return model

    def _hot_changed_paths(self, disk_canonical: dict) -> list:
        """返回磁盘相对会话基线发生变化的 HOT 白名单路径。

        分类优先级 COLD prefix > HOT exact-path：即使字段声明在 hot_paths 中，
        只要位于 COLD 子树内仍按 COLD 处理，不进入 HOT 候选。
        """
        changed: list = []
        for path in sorted(self.reload_policy.hot_paths):
            if self.reload_policy.classify(path) != HOT:
                continue
            disk_val = get_path(disk_canonical, path)
            if disk_val is MISSING:
                # HOT 不支持结构性删除；缺失路径交给 WARM 边界重建
                continue
            base_val = get_path(self._base, path)
            if base_val is MISSING or not _eq(disk_val, base_val):
                changed.append(path)
        return changed

    @staticmethod
    def _fingerprint_value(value) -> str:
        """把指纹中的值序列化为可哈希字符串；MISSING 与 None 严格区分。"""
        if value is MISSING:
            return "<MISSING>"
        return json.dumps(value, sort_keys=True, default=str)

    def _hot_fingerprint(self, changed_paths: list, disk_canonical: dict) -> tuple:
        """disk/local 指纹：任何一侧变化都会改变指纹，从而解除失败标记并重新分类。"""
        model_raw = self._model.model_dump(mode="json")
        return tuple(
            (
                path,
                self._fingerprint_value(get_path(disk_canonical, path)),
                self._fingerprint_value(get_path(model_raw, path)),
            )
            for path in sorted(changed_paths)
        )

    def _mark_hot_failure(self, changed_paths: list, fingerprint: tuple) -> None:
        """prepare 失败：保持 model/base/派生缓存原值，仅标为 WARM deferred 并记录失败指纹。

        同一 disk/local 指纹在本任务内不再调用 prepare（规格 §11.1 第 5 条）。
        """
        self._pending_warm_paths.update(changed_paths)
        self._hot_failed_fingerprints.add(fingerprint)
        logger.warning(
            f'[{self.config_name}] HOT prepare failed, paths deferred to WARM: {changed_paths}')

    def refresh_hot_at_checkpoint(self, task) -> bool:
        """外层安全检查点 HOT 刷新入口（规格 §11.1 / §12）。

        两阶段提交：
        ① RLock 内记录 revision、读取磁盘构造候选模型与 changed_paths，不替换运行字段；
        ② 锁外调用 task.prepare_config_reload(candidate, changed_paths)，抛错则无运行态变更；
        ③ 重取 RLock，revision 已变或 generation mismatch 则丢弃候选、下一检查点重新分类
           （mismatch 由 refresh_from_disk mismatch / _config_load_failure_stop / save
           锁超时或身份变化置位，均不推进 revision，提交前必须一并检查）；
        ④ revision 未变则同一临界区替换允许的 HOT scalar、声明的派生缓存、
           对应 base/blocked/deferred 与 revision；提交段异常绝不穿出，按失败转 WARM deferred；
        ⑤ prepare 失败保持原值，仅记录 WARM deferred 与失败指纹，同一指纹不重复 prepare。

        生产默认 HOT 白名单为空，真实任务不发生中途替换；HOT 基础设施
        只由测试专用合成 Schema 注入 ReloadPolicy 验证。
        """
        has_prepare = hasattr(task, "prepare_config_reload")
        with self.session_lock:
            if self._refresh_in_progress or self._generation_mismatch:
                return False
            if not self.reload_policy.hot_paths:
                # 生产默认无 HOT 字段：零开销快速返回
                return False
        # 最小刷新间隔：mtime_ns 未变化不解析配置（使用 st_mtime_ns，同秒多次修改可检出）
        if self._disk_mtime_ns() <= self._mtime_ns:
            return False
        with self.session_lock:
            if self._refresh_in_progress or self._generation_mismatch:
                return False
            try:
                loaded = self.store.load(self.config_name)
            except Exception as e:
                # HOT 检查点不破坏截图主流程；读取失败/身份变化由 WARM 边界统一处理
                logger.warning(
                    f'[{self.config_name}] HOT checkpoint load failed: {type(e).__name__}: {e}')
                return False
            if loaded.generation != self.generation:
                return False
            changed_paths = self._hot_changed_paths(loaded.canonical)
            if not changed_paths:
                # 磁盘有变化但无 HOT 候选（仅 WARM/COLD 变化）：推进已检查 mtime 基线，
                # 避免每帧全量 load+校验（规格 §12「避免每帧解析配置」）
                self._mtime_ns = loaded.mtime_ns
                return False
            fingerprint = self._hot_fingerprint(changed_paths, loaded.canonical)
            if fingerprint in self._hot_failed_fingerprints:
                # 同一 disk/local 指纹失败过：不重复 prepare，交给 WARM 边界；
                # 同样推进已检查 mtime 基线，磁盘再次变化才重新分类
                self._mtime_ns = loaded.mtime_ns
                return False
            # 构造候选模型并覆盖回 COLD 启动快照，保持与运行实例一致的视角
            candidate = copy.deepcopy(loaded.model)
            protected = self._protected_device_snapshot()
            if protected is not None:
                candidate.script.device = copy.deepcopy(protected)
            revision = self._refresh_revision
            self._refresh_in_progress = True

        # ② 锁外调用纯 prepare hook（不得在 FileLock/RLock 内执行 callback）。
        # try/finally 确保无论 prepare 以何种方式退出（含 BaseException）都清除 guard，
        # 避免 KeyboardInterrupt/SystemExit 后 HOT 被 guard 永久挡住。
        prepared: dict = {}
        prepare_error: Exception | None = None
        if has_prepare:
            try:
                try:
                    prepared = task.prepare_config_reload(candidate, changed_paths)
                except Exception as e:
                    prepare_error = e
            finally:
                with self.session_lock:
                    self._refresh_in_progress = False
        else:
            with self.session_lock:
                self._refresh_in_progress = False
        if prepare_error is not None:
            logger.warning(
                f'[{self.config_name}] HOT prepare raised: {type(prepare_error).__name__}: {prepare_error}')
            with self.session_lock:
                # 失败同样推进已检查 mtime 基线：同一磁盘状态下一次检查点直接短路，
                # 避免每个新指纹失败后都多一次全量 load（与 no-candidate/指纹命中分支一致）
                self._mtime_ns = loaded.mtime_ns
                self._mark_hot_failure(changed_paths, fingerprint)
            self._report_hot_state()
            return False
        if has_prepare:
            declared = frozenset(getattr(task, "HOT_RELOAD_DERIVED_FIELDS", frozenset()) or frozenset())
            if not isinstance(prepared, dict) or set(prepared) - declared:
                # 拒绝返回未声明字段：prepare 结果整体丢弃，按失败转 WARM（规格 §11.1）
                logger.warning(
                    f'[{self.config_name}] HOT prepare returned undeclared fields, deferred to WARM')
                with self.session_lock:
                    self._mark_hot_failure(changed_paths, fingerprint)
                self._report_hot_state()
                return False
            prepared = {k: v for k, v in prepared.items() if k in declared}

        # ③④ 重取 RLock 提交；revision 变化或 generation mismatch 则丢弃候选，
        # 不得提交陈旧 prepare 结果（_generation_mismatch 由 refresh_from_disk mismatch /
        # _config_load_failure_stop / save 锁超时或身份变化置位，均不推进 revision）
        with self.session_lock:
            if self._refresh_revision != revision or self._generation_mismatch:
                return False
            try:
                changed_set = set(changed_paths)
                # 第一趟：解析全部槽位与候选值。这是唯一可能失败的一趟，此时零写入；
                # 原实现逐路径 `self._model = ...` 边解析边提交，多路径下第 N 条抛错
                # 会留下前 N-1 条已生效的半成品，本结构顺带修掉该原子性缺口。
                slots = self._resolve_model_slots(self._model, changed_paths, candidate)
                disk_values = [get_path(loaded.canonical, path) for path in changed_paths]
                # 第二趟：只做已解析槽位的原地赋值与 base/deferred 推进，不会失败
                for host, field, value in slots:
                    setattr(host, field, value)
                for path, disk_val in zip(changed_paths, disk_values):
                    self._base = set_path(self._base, path, disk_val)
                    self._pending_warm_paths.discard(path)
                # 连带失效受影响的 Config 级派生缓存，避免模型已新而缓存仍旧
                for prefix, cache_name in _HOT_INVALIDATED_CACHES:
                    if any(p[:len(prefix)] == prefix for p in changed_paths):
                        self.__dict__.pop(cache_name, None)
                if prepared:
                    for name, value in prepared.items():
                        setattr(task, name, value)
                # 清除该路径对应的 blocked/deferred，避免 HOT 已生效但状态表残留
                if self.blocked_changes:
                    self.blocked_changes = [
                        b for b in self.blocked_changes if not _blocked_overlaps(b.path, changed_set)
                    ]
                self._refresh_revision += 1
                self._mtime_ns = loaded.mtime_ns
                self._hot_failed_fingerprints.discard(fingerprint)
            except Exception as e:
                # 提交段异常（模型结构不匹配等）绝不穿出到 BaseTask.screenshot 破坏截图主流程：
                # 按失败转 WARM deferred 并记录指纹，保持运行 model 原值（规格 §11.1）；
                # 锁内直接调用同步内部标记，不重复获取 session_lock
                logger.warning(
                    f'[{self.config_name}] HOT commit failed, deferred to WARM: {type(e).__name__}: {e}')
                self._mark_hot_failure(changed_paths, fingerprint)
                return False
        # 释放锁后广播最新 config_state（规格 §11.1 点 4）
        self._report_hot_state()
        return True

    def _report_hot_state(self) -> None:
        """HOT 提交/失败后向子进程 state_queue 上报最新 config_state。

        必须锁外调用（规格 callback 在 FileLock/RLock 外执行）；无 reporter 时 no-op。
        上报失败只记录日志，不中断截图主流程。
        """
        reporter = self._state_reporter
        if reporter is None:
            return
        try:
            reporter()
        except Exception:
            logger.warning(f'[{self.config_name}] HOT state report failed', exc_info=True)

    def _request_instance_stop(self) -> None:
        """generation mismatch 后终止持久化并请求实例停止；脚本主循环在边界检查后退出。"""
        self._generation_mismatch = True

    # ------------------------------------------------------------------ COLD 启动快照

    def begin_device_initialization(self) -> None:
        """Device 构造前读取一次锁内最新配置，并以此划定 COLD 启动边界。"""
        # 启动期虽是单线程，但仍持 session_lock 保持「session RLock → lifecycle FileLock」
        # 统一锁序，避免与 save/refresh 走两套顺序。
        with self.session_lock:
            if self._startup_device_snapshot is not None or self._provisional_device_snapshot is not None:
                raise RuntimeError("device initialization has already started")
            loaded = self.store.load(self.config_name)
            self._commit_loaded_state(loaded.model, copy.deepcopy(loaded.canonical), loaded)
            self._provisional_device_snapshot = copy.deepcopy(self.model.script.device)

    def freeze_startup_device_snapshot(self) -> None:
        """设备初始化完成后，将只含内部归一化的 provisional 快照转为正式快照。"""
        with self.session_lock:
            if self._provisional_device_snapshot is None:
                raise RuntimeError("device initialization has not started")
            self._startup_device_snapshot = copy.deepcopy(self._provisional_device_snapshot)
            self._provisional_device_snapshot = None

    def _protected_device_snapshot(self):
        if self._startup_device_snapshot is not None:
            return self._startup_device_snapshot
        return self._provisional_device_snapshot

    def startup_normalize(self, updates: dict[tuple[str, ...], object]) -> None:
        """设备初始化阶段只把声明的 script.device 路径合入 provisional 快照与 session model/base。

        正式快照冻结后调用必须失败；不吸收返回 LoadedConfig 中的其他并发 COLD 字段。
        """
        if not updates or any(path[:2] != ("script", "device") for path in updates):
            raise ValueError("startup normalization only accepts device paths")
        with self.session_lock:
            protected = self._provisional_device_snapshot
            if protected is None or self._startup_device_snapshot is not None:
                raise RuntimeError("startup normalization requires active device initialization")

            loaded = self.store.startup_normalize(
                self.config_name,
                updates,
                self.generation,
            )
            next_device_raw = protected.model_dump(mode="json")
            for path in updates:
                relative_path = path[2:]
                normalized_value = get_path(loaded.canonical, path)
                next_device_raw = set_path(next_device_raw, relative_path, normalized_value)
            next_device = type(protected).model_validate(next_device_raw)

            loaded.model.script.device = copy.deepcopy(next_device)
            loaded_base = copy.deepcopy(loaded.canonical)
            loaded_base["script"]["device"] = next_device.model_dump(mode="json")
            self._commit_loaded_state(loaded.model, loaded_base, loaded)
            self._provisional_device_snapshot = next_device

    # ------------------------------------------------------------------ GUI / 调度

    def gui_args(self, task: str) -> str:
        """
        获取给gui显示的参数
        :return:
        """
        return self.model.gui_args(task=task)

    def update_scheduler(self) -> None:
        """
        更新调度器， 设置pending_task and waiting_task
        :return:
        """
        pending_task = []
        waiting_task = []
        error = []
        self.scheduler_update_dt = datetime.now()
        for key, value in self.model.dict().items():
            func = Function(key, value)
            if not func.enable:
                continue
            if not isinstance(func.next_run, datetime):
                error.append(func)
            elif func.next_run < self.scheduler_update_dt:
                pending_task.append(func)
            else:
                waiting_task.append(func)

        # f = Filter(regex=r"(.*)", attr=["command"])
        # f.load(self.SCHEDULER_PRIORITY)
        if pending_task:
            pending_task = TaskScheduler.schedule(rule=self.model.script.optimization.schedule_rule,
                                                  pending=pending_task)
            # 防止正在运行的任务被新上来的pending队列中的任务给顶替掉
            if self.model.running_task and pending_task:
                for i, obj in enumerate(pending_task):
                    if obj.command == self.model.running_task:
                        pending_task.insert(0, pending_task.pop(i))
                        logger.info(f'{self.model.running_task} is running')
                        break
        if waiting_task:
            # waiting_task = f.apply(waiting_task)
            waiting_task = sorted(waiting_task, key=operator.attrgetter("next_run"))
        if error:
            pending_task = error + pending_task

        self.pending_task = pending_task
        self.waiting_task = waiting_task

    def get_next(self) -> Function:
        """
        获取下一个要执行的任务
        :return:
        """
        self.update_scheduler()

        if self.pending_task:
            logger.info(f"Pending tasks: {[f.command for f in self.pending_task]}")
            task = self.pending_task[0]
            self.task = task
            logger.attr("Task", task)
            return task

        # 哪怕是没有任务，也要返回一个任务，这样才能保证调度器正常运行
        if self.waiting_task:
            logger.info("No task pending")
            task = copy.deepcopy(self.waiting_task[0])
            # task.next_run = (task.next_run + self.hoarding).replace(microsecond=0)
            logger.attr("Task", task)
            return task
        else:
            logger.critical("No task waiting or pending")
            logger.critical("Please enable at least one task")
            raise RequestHumanTakeover

    def get_forbidden_time_end(self, task: str, now: datetime = None) -> datetime:
        """
        判断任务当前是否处于禁止运行时间段内。
        命中则返回该连续禁止区间的结束时刻（供调度层推迟任务），否则返回 None。

        :param task: 任务名，大驼峰，如 'KekkaiUtilize'
        :param now: 当前时间，默认取 datetime.now()
        :return: 区间结束时刻的 datetime 或 None
        """
        submodel_name = FORBIDDEN_TIME_TASKS.get(task)
        if submodel_name is None:
            return None
        task_object = getattr(self.model, convert_to_underscore(task), None)
        if task_object is None:
            return None
        sub = getattr(task_object, submodel_name, None)
        if sub is None:
            return None
        if not getattr(sub, 'forbidden_time_enable', False):
            return None
        time_range = getattr(sub, 'forbidden_time_range', '') or ''
        if not time_range:
            return None
        now = now or datetime.now()
        return forbidden_range_end(now, time_range)

    def get_task_random_delay(self, task: str) -> timedelta | None:
        """
        读取任务的下次上号随机延时配置。
        开启且区间合法时返回 [min, max] 分钟内的随机 timedelta，否则返回 None。

        :param task: 任务名，大驼峰，如 'KekkaiUtilize'（需在 FORBIDDEN_TIME_TASKS 注册）
        :return: 随机延时的 timedelta 或 None（未注册/未开启/区间非法）
        """
        submodel_name = FORBIDDEN_TIME_TASKS.get(task)
        if submodel_name is None:
            return None
        task_object = getattr(self.model, convert_to_underscore(task), None)
        if task_object is None:
            return None
        sub = getattr(task_object, submodel_name, None)
        if sub is None:
            return None
        if not getattr(sub, 'random_delay_enable', False):
            return None
        # min/max 颠倒时自动对调，避免配置笔误导致 randint 抛 ValueError
        delay_min = getattr(sub, 'random_delay_min', 0)
        delay_max = getattr(sub, 'random_delay_max', 0)
        if delay_min > delay_max:
            delay_min, delay_max = delay_max, delay_min
        if delay_max <= 0:
            return None
        # random 模块已被 Config 用于服务器更新抖动，此处复用同一随机源
        return timedelta(minutes=random.randint(delay_min, delay_max))

    def get_schedule_data(self) -> dict[str, dict]:
        """
        获取调度器的数据， 但是你必须使用update_scheduler来更新信息
        :return:
        """
        # 根据调度器更新时间来判断是否有可运行的任务,保证逻辑一致性
        scheduler_update_dt = getattr(self, 'scheduler_update_dt', datetime.now())
        running = {}
        if self.task is not None and self.task.next_run < scheduler_update_dt:
            running = {"name": self.task.command, "next_run": str(self.task.next_run)}

        pending = []
        for p in self.pending_task[1:]:
            item = {"name": p.command, "next_run": str(p.next_run)}
            pending.append(item)

        waiting = []
        for w in self.waiting_task:
            item = {"name": w.command, "next_run": str(w.next_run)}
            waiting.append(item)

        data = {"running": running, "pending": pending, "waiting": waiting}
        return data

    def task_call(self, task: str = None, force_call=True):
        """
        回调任务，这会是在任务结束后调用
        :param task: 调用的任务的大写名称
        :param force_call:
        :return:
        """
        task = convert_to_underscore(task)
        if self.model.deep_get(self.model, keys=f'{task}.scheduler.next_run') is None:
            raise ScriptError(f"Task to call: `{task}` does not exist in user config")

        task_enable = self.model.deep_get(self.model, keys=f'{task}.scheduler.enable')
        if force_call or task_enable:
            logger.info(f"Task call: {task}")
            next_run = datetime.now().replace(
                microsecond=0
            )
            self.model.deep_set(self.model, keys=f'{task}.scheduler.next_run', value=next_run)
            self.save()
            return True
        else:
            logger.info(f"Task call: {task} (skipped because disabled by user)")
            return False

    def task_delay(self, task: str, start_time: datetime = None,
                   success: bool = None, server: bool = True, target: datetime = None,
                   persist: bool = True) -> None:
        """
        设置下次运行时间  当然这个也是可以重写的
        :param persist: 是否立即保存；False 供同一事务继续修改其他配置后统一保存
        :param target: 可以自定义的下次运行时间
        :param server: True
        :param success: 判断是成功的还是失败的时间间隔
        :param task: 任务名称，大驼峰的
        :param finish: 是完成任务后的时间为基准还是开始任务的时间为基准
        :return:
        """
        # 加载配置文件（受 COLD 快照保护，外部 device 修改不进入运行模型）
        self.reload()
        # 任务预处理
        if not task:
            task = self.task.command
        task = convert_to_underscore(task)
        task_object = getattr(self.model, task, None)
        if not task_object:
            logger.warning(f'No task named {task}')
            return
        scheduler = getattr(task_object, 'scheduler', None)
        if not scheduler:
            logger.warning(f'No scheduler in {task}')
            return

        # 任务开始时间
        if not start_time:
            start_time = datetime.now().replace(microsecond=0)

        # 依次判断是否有自定义的下次运行时间
        run = []
        if success is not None:
            interval = (
                scheduler.success_interval
                if success
                else scheduler.failure_interval
            )
            if isinstance(interval, str):
                interval = timedelta(interval)
            run.append(start_time + interval)
        # if server is not None:
        #     if server:
        #         server = scheduler.server_update
        #         run.append(get_server_next_update(server))
        if target is not None:
            target = [target] if not isinstance(target, list) else target
            target = nearest_future(target)
            run.append(target)

        next_run = None
        # 排序
        if not len(run):
            raise ScriptError(
                "Missing argument in delay_next_run, should set at least one"
            )

        run = min(run).replace(microsecond=0)
        next_run = run

        if server and hasattr(scheduler, 'server_update'):
            # 加入随机延迟时间
            float_seconds = (scheduler.float_time.hour * 3600 +
                             scheduler.float_time.minute * 60 +
                             scheduler.float_time.second)
            random_float = random.randint(0, float_seconds)
            # 如果有强制运行时间
            if scheduler.server_update == time(hour=9):
                next_run += timedelta(seconds=random_float)
            else:
                next_run = parse_tomorrow_server(scheduler.server_update, scheduler.delay_date, random_float)

        # 将这些连接起来，方便日志输出
        kv = dict_to_kv(
            {
                "success": success,
                "server_update": server,
                "target": target,
            },
            allow_none=False,
        )
        logger.info(f"Delay task `{task}` to {next_run} ({kv})")

        # 保证线程安全的
        self.lock_config.acquire()
        try:
            scheduler.next_run = next_run
            if persist:
                self.save()
        finally:
            self.lock_config.release()
        # 设置
        logger.attr(f'{task}.scheduler.next_run', next_run)


if __name__ == '__main__':
    config = Config(config_name='oas1')
    config.notifier.push(title="0000", content="dddddddd")

    # print(config.get_next())
