# This Python file uses the following encoding: utf-8
# @author runhey
# 主进程的管理
# github https://github.com/runhey
import asyncio
import concurrent.futures
import time
from threading import Event, RLock, Thread

from module.logger import logger
from module.config.config import Config
from module.config.config_generation import (
    ConfigGenerationError,
    ConfigIdentityConflictError,
)
from module.server.script_process import ScriptProcess, ScriptState
from module.server.config_manager import ConfigManager
from module.server.setting import State


# 缺失属性与合法的 inactive/None 终态必须严格区分。
_MISSING = object()


class MainManager(ConfigManager):
    # config_cache: Config = None  # 缓存当前切换的配置
    script_process: dict[str: ScriptProcess] = None  # 脚本进程
    push_data_thread: Thread = None  # 数据推送线程
    signal_kill_server: bool = False

    def __init__(self, store=None) -> None:
        # 模块导入必须无 I/O：只创建 store、空 script_process registry 和未启动线程，
        # 不调用 Store initialize/list/load，也不创建 ScriptProcess。
        super().__init__(store=store)
        self.script_process: dict[str: ScriptProcess] = {}  # 脚本进程
        self.push_data_thread: Thread = None
        # manager 级身份锁同时保护 wrapper 替换、启动和 rename/delete 全事务，
        # 禁止 start 插入“进程已停止、Store 身份尚未提交”的广播窗口。
        self._ensure_script_lock = asyncio.Lock()
        self._registry_lock = RLock()
        self._push_shutdown_event = Event()
        self._push_interval = 3.0
        # ScriptProcess lifecycle lock 归属主事件循环，推送线程只能线程安全投递 stop。
        self._main_loop = None
        self._stop_request_timeout = 10.0
        self._stop_cancel_timeout = 0.2
        # 生命周期对账超时后仍保留可追踪 task，避免产生未消费异常的孤儿任务。
        self._reconcile_timeout = 5.0
        self._reconcile_cancel_timeout = 0.5
        self._managed_reconcile_tasks: set[asyncio.Task] = set()
        # 超时批次仍可能在主 loop 收尾，重试前必须确认旧批次已结束。
        self._managed_stop_tasks: set[asyncio.Task] = set()
        self._last_stop_all_errors: list[tuple[str, BaseException]] = []

    def config_cache(self, name: str) -> Config:
        return Config(name, store=self.store)

    async def _ensure_script_process_locked(self, name: str) -> ScriptProcess:
        """调用方持有 manager 身份锁时获取 active generation 对应的 wrapper。"""
        while True:
            loaded = self.store.load(name)
            with self._registry_lock:
                process = self.script_process.get(name)
            if process is not None and process.generation == loaded.generation:
                return process
            if process is not None:
                # stop 是异步操作，必须先等待旧对象收敛，再把 registry 切换到新身份。
                logger.warning(
                    f'[{name}] replace stale ScriptProcess generation '
                    f'{process.generation} -> {loaded.generation}'
                )
                await process.stop()
                # 对象 CAS 防止陈旧清理覆盖其他路径已安装的新 wrapper。
                with self._registry_lock:
                    if self.script_process.get(name) is process:
                        self.script_process.pop(name, None)
                    else:
                        # registry 已换对象时重新 load/generation 核验，禁止直接采用未知 wrapper。
                        continue

            candidate = ScriptProcess(name, store=self.store, generation=loaded.generation)
            with self._registry_lock:
                current = self.script_process.get(name)
                if current is None:
                    self.script_process[name] = candidate
                    return candidate
                if current.generation == loaded.generation:
                    return current
            # 仅可能由异步对账 CAS 安装不同 generation；回到循环统一 stop/复核。

    async def ensure_script_process(self, name: str) -> ScriptProcess:
        """按 active identity 获取/创建 ScriptProcess，不隐式启动。"""
        async with self._ensure_script_lock:
            return await self._ensure_script_process_locked(name)

    async def _start_process_locked(self, process: ScriptProcess) -> bool:
        """调用方持有 manager 身份锁时启动已核验 wrapper，内部恢复禁止重入锁。"""
        return await process.start()

    async def start_script_process(self, name: str) -> bool:
        """在 manager 身份锁内完成 load/ensure/start，串行化身份提交与启动。"""
        async with self._ensure_script_lock:
            process = await self._ensure_script_process_locked(name)
            return await self._start_process_locked(process)

    async def restart_script_process(self, name: str) -> bool:
        """在同一身份锁内完成 stop/start，避免 restart 绕过 rename/delete 协议。"""
        async with self._ensure_script_lock:
            process = await self._ensure_script_process_locked(name)
            await process.stop()
            # stop 广播后再次核验 active identity，拒绝并发外部 ABA。
            current = await self._ensure_script_process_locked(name)
            if current is not process:
                process = current
            return await self._start_process_locked(process)

    def _registry_snapshot(self) -> list[tuple[str, ScriptProcess]]:
        """在线程锁内复制 registry，禁止推送线程直接迭代可变字典。"""
        with self._registry_lock:
            return list(self.script_process.items())

    def notify_config_changed(self, config_name: str, result) -> None:
        """OASX PUT 成功后锁外投递 config_changed 事件给运行实例。"""
        with self._registry_lock:
            process = self.script_process.get(config_name)
        if process is None or process.state == ScriptState.INACTIVE:
            return
        process.deliver_config_changed(
            getattr(result, "generation", ""),
            getattr(result, "mtime_ns", 0),
            list(getattr(result, "changed_paths", []) or []),
        )

    async def initialize(self) -> None:
        """migration → lifecycle 恢复 → active 身份校验 → 枚举 → 创建进程 → 启动推送线程。"""
        self._main_loop = asyncio.get_running_loop()
        try:
            self.store.initialize()
        except Exception as e:
            # 结构级身份损坏必须 fail closed（规格 §7 / §10.3），但要给出可定位信息，
            # 否则用户只看到服务起不来。内容级校验失败已在 survey 阶段隔离，不会走到这里。
            logger.error(
                f'config identity state is unrecoverable: {type(e).__name__}: {e}; '
                f'原字节备份位于 {self.store.generation.backups_dir}'
            )
            raise
        for name, error in sorted(self.store.quarantined_identities.items()):
            # 单份配置内容非法只隔离该实例，不阻断其余健康实例与整个服务启动。
            logger.error(
                f'[{name}] config quarantined and will not be listed or started: '
                f'{type(error).__name__}: {error}; '
                f'原字节备份位于 {self.store.generation.backups_dir}'
            )
        names = self.store.active_config_names()
        processes = {
            name: ScriptProcess(name, store=self.store)
            for name in names
        }
        with self._registry_lock:
            self.script_process = processes
        self.start_push_data_thread()

    async def add_script_file(self, file_name: str):
        # 当你添加了新的脚本文件后，需要添加缓存的列表。
        try:
            await self.ensure_script_process(file_name)
        except Exception as e:
            logger.error(f'[{file_name}] add script file failed: {e}')

    def start_push_data_thread(self):
        if self.push_data_thread is not None and self.push_data_thread.is_alive():
            return
        self._push_shutdown_event.clear()
        self.push_data_thread = Thread(target=self.start_push_data_handle, daemon=True)
        self.push_data_thread.start()

    def start_push_data_handle(self):
        """运行推送事件循环；单轮异常只记录并重试，不让长期线程退出。"""
        while not self._push_shutdown_event.is_set():
            try:
                asyncio.run(self.push_data_handle())
                return
            except SystemExit:
                logger.info('Kill the main process')
                # kill_server 已确认全部实例停止：通知 uvicorn 优雅退出整个服务。
                # 测试环境下 State.server 为 None，保持只结束推送线程的既有行为。
                server = State.server
                if server is not None:
                    server.should_exit = True
                return
            except Exception as e:
                logger.exception(f'push data loop failed, retrying: {e}')
                time.sleep(0.1)

    async def _cancel_push_entry(self, entry) -> None:
        """取消一组旧 process 的 state/log 协程，并消费其异常以避免 task 泄漏。"""
        state_task, log_task = entry[1], entry[2]
        for task in (state_task, log_task):
            if task is not None and not task.done():
                task.cancel()
        await asyncio.gather(state_task, log_task, return_exceptions=True)

    async def _sync_push_tasks(self, tasks: dict[str, tuple]) -> None:
        """按 process 对象身份同步推送协程，generation 替换时清理旧任务。"""
        snapshot = dict(self._registry_snapshot())
        for name, entry in list(tasks.items()):
            current = snapshot.get(name)
            state_task, log_task = entry[1], entry[2]
            stale = (
                current is not entry[0]
                or current is None
                or current.state == ScriptState.INACTIVE
                or state_task.done()
                or log_task.done()
            )
            if stale:
                await self._cancel_push_entry(entry)
                tasks.pop(name, None)

        for name, process in snapshot.items():
            if process.state == ScriptState.INACTIVE or name in tasks:
                continue
            state_task = asyncio.create_task(
                process.coroutine_broadcast_state(),
                name=f'coroutine_state_{name}_{id(process)}',
            )
            log_task = asyncio.create_task(
                process.coroutine_broadcast_log(),
                name=f'coroutine_log_{name}_{id(process)}',
            )
            tasks[name] = (process, state_task, log_task)

    async def stop_all_processes(self) -> bool:
        """逐实例停止并聚合失败；取消会传播，registry 变化会触发后续重试。"""
        errors: list[tuple[str, BaseException]] = []
        seen: set[tuple[str, int]] = set()
        registry_changed = False
        while True:
            snapshot = self._registry_snapshot()
            pending = [
                (name, process)
                for name, process in snapshot
                if (name, id(process)) not in seen
            ]
            if not pending:
                break
            for name, process in pending:
                seen.add((name, id(process)))
                try:
                    await process.stop()
                    # 缺失 state/句柄不是“看起来没问题”，而是无法确认真实终态。
                    state = getattr(process, "state", _MISSING)
                    if state is _MISSING:
                        raise RuntimeError(f"Script {name} state is unavailable after stop")
                    if state != ScriptState.INACTIVE:
                        raise RuntimeError(
                            f"Script {name} stop returned while state is still active"
                        )
                    handle = getattr(process, "_process", _MISSING)
                    if handle is _MISSING:
                        raise RuntimeError(
                            f"Script {name} process handle is unavailable after stop"
                        )
                    if handle is not None:
                        try:
                            alive = handle.is_alive()
                            # 只有明确 False 才能确认已退出，None/string 等均 fail-closed。
                            if alive is not False:
                                raise RuntimeError(
                                    f"Script {name} subprocess state is not confirmed"
                                )
                        except BaseException as check_error:
                            if isinstance(check_error, RuntimeError):
                                raise
                            raise RuntimeError(
                                f"Script {name} subprocess state could not be confirmed"
                            ) from check_error
                except asyncio.CancelledError:
                    # 批次取消不能被单实例容错吞掉，否则 timeout 后旧 task 会与重试重叠。
                    self._last_stop_all_errors = errors
                    raise
                except BaseException as stop_error:
                    errors.append((name, stop_error))
                    logger.error(f'[{name}] stop during kill-server failed: {stop_error}')
            if self._registry_snapshot() != snapshot:
                registry_changed = True

        if registry_changed:
            errors.append(
                (
                    "__registry__",
                    RuntimeError("script registry changed while stopping processes"),
                )
            )
        self._last_stop_all_errors = errors
        return not errors

    def _remember_reconcile_task(self, task: asyncio.Task) -> None:
        """超时后托管仍运行的对账 task，并消费最终异常，避免任务泄漏。"""
        self._managed_reconcile_tasks.add(task)

        def consume(done_task):
            self._managed_reconcile_tasks.discard(done_task)
            try:
                done_task.result()
            except BaseException as error:
                logger.error(
                    f'lifecycle reconcile task completed after timeout: '
                    f'{type(error).__name__}: {error}'
                )

        task.add_done_callback(consume)

    async def _retire_stale_registry_process(
        self, source: str, process: ScriptProcess, reason: str
    ) -> bool:
        """安全回收身份过期 wrapper；无法确认退出时保留句柄供后续重试。"""
        with self._registry_lock:
            # 新 wrapper 已安装时，陈旧对账不得停止或删除它。
            if self.script_process.get(source) is not process:
                return False
        try:
            await process.stop()
        except BaseException as stop_error:
            # CancelledError/stop 异常都按 fail-closed 处理，不能丢弃可能仍存活的句柄。
            logger.error(
                f'[{source}] stale wrapper retire stop failed; retain registry: '
                f'{type(stop_error).__name__}: {stop_error}'
            )
            return False
        try:
            state = getattr(process, "state", _MISSING)
            if state != ScriptState.INACTIVE:
                raise RuntimeError(f"stale wrapper state is not inactive: {state!r}")
            handle = getattr(process, "_process", _MISSING)
            if handle is _MISSING:
                raise RuntimeError("stale wrapper process handle is unavailable")
            if handle is not None:
                alive = handle.is_alive()
                # 只有内建 False 能证明句柄已退出；None/非 bool/True 均保留。
                if alive is not False:
                    raise RuntimeError(
                        f"stale wrapper subprocess state is not confirmed: {alive!r}"
                    )
        except BaseException as probe_error:
            logger.error(
                f'[{source}] stale wrapper retire probe failed; retain registry '
                f'({reason}): {type(probe_error).__name__}: {probe_error}'
            )
            return False
        with self._registry_lock:
            # stop/探针期间若发生替换，只能 CAS 移除原对象。
            if self.script_process.get(source) is not process:
                return False
            updated = dict(self.script_process)
            updated.pop(source, None)
            self.script_process = updated
        return True

    async def _preserve_reconcile_registry(self, args, kwargs) -> None:
        """对账超时后按 generation CAS 保留可重试的 source/句柄，不覆盖新 wrapper。"""
        source = kwargs.get("source", args[0] if args else None)
        process = kwargs.get("process")
        if process is None and len(args) >= 3:
            process = args[2]
        if not isinstance(source, str) or process is None:
            return
        try:
            record = await asyncio.to_thread(
                self.store.generation.read_active_generation, source
            )
            source_active = record is not None and record.state == "active"
            process_generation = getattr(process, "generation", _MISSING)
            generation_matches = (
                source_active
                and process_generation is not _MISSING
                and process_generation == record.generation
            )
        except BaseException as error:
            logger.error(f'[{source}] reconcile timeout identity check failed: {error}')
            return
        if generation_matches:
            return
        # generation 不匹配时先安全停止旧句柄，确认退出后再对象 CAS 移除。
        await self._retire_stale_registry_process(
            source, process, "reconcile timeout generation mismatch"
        )

    def _request_stop_all_from_push_thread(self) -> bool:
        """向主 loop 投递批量 stop；任务所有权由 loop callback 明确管理。"""
        loop = self._main_loop
        try:
            # 两次检查都放在提交前，但提交本身仍需捕获 loop 关闭的 TOCTOU。
            if loop is None or loop.is_closed() or not loop.is_running():
                logger.error('kill-server stop failed: main event loop unavailable')
                return False
        except BaseException as loop_error:
            logger.error(f'kill-server stop failed while checking main loop: {loop_error}')
            return False
        try:
            current_loop = asyncio.get_running_loop()
        except RuntimeError:
            current_loop = None
        if current_loop is loop:
            logger.error('kill-server stop failed: synchronous request from main event loop')
            return False

        request_future = concurrent.futures.Future()
        completion_future = concurrent.futures.Future()
        owner = {"task": None}

        def finish(task):
            with self._registry_lock:
                self._managed_stop_tasks.discard(task)
            try:
                result = task.result()
            except BaseException as task_error:
                if not completion_future.done():
                    completion_future.set_exception(task_error)
                if not request_future.done():
                    request_future.set_exception(task_error)
            else:
                if not completion_future.done():
                    completion_future.set_result(result)
                if not request_future.done():
                    request_future.set_result(result)

        def submit() -> None:
            # callback 尚未执行时，request_future 已取消即可安全放弃，未创建 coroutine。
            if request_future.cancelled():
                return
            with self._registry_lock:
                self._managed_stop_tasks = {
                    old_task
                    for old_task in self._managed_stop_tasks
                    if not old_task.done()
                }
                if self._managed_stop_tasks:
                    # 旧批次仍由主 loop 托管，禁止重试与其争抢实例 lifecycle lock。
                    request_future.set_result(False)
                    return
            coroutine = self.stop_all_processes()
            try:
                task = loop.create_task(coroutine)
            except BaseException as submit_error:
                # create_task 失败时 callback 已拥有 coroutine，必须显式 close。
                coroutine.close()
                if not completion_future.done():
                    completion_future.set_exception(submit_error)
                if not request_future.done():
                    request_future.set_exception(submit_error)
                return
            owner["task"] = task
            with self._registry_lock:
                self._managed_stop_tasks.add(task)
            task.add_done_callback(finish)

        try:
            # 只投递 callback；coroutine 在 callback 执行时才创建，避免 stopped loop 的 never-awaited。
            loop.call_soon_threadsafe(submit)
        except BaseException as submit_error:
            request_future.cancel()
            logger.error(f'kill-server stop submit failed: {submit_error}')
            return False

        try:
            # 只接受 stop_all_processes 的明确 True，避免任意 truthy 返回值造成假成功。
            return request_future.result(timeout=self._stop_request_timeout) is True
        except concurrent.futures.TimeoutError:
            request_future.cancel()

            def cancel_task() -> None:
                task = owner["task"]
                if task is not None and not task.done():
                    task.cancel()

            try:
                # task 已创建时由其所属 loop 取消；callback 未执行时这里只是空操作。
                loop.call_soon_threadsafe(cancel_task)
            except BaseException as cancel_error:
                logger.error(f'kill-server stop cancellation submit failed: {cancel_error}')
            try:
                # 给已创建 task 一个很短的 drain 窗口，不能让 push thread 长时间阻塞。
                completion_future.result(timeout=self._stop_cancel_timeout)
            except BaseException:
                pass
            logger.error('kill-server stop request timed out and was cancelled for retry')
            return False
        except BaseException as stop_error:
            logger.error(f'kill-server stop request failed: {stop_error}')
            return False

    async def push_data_handle(self):
        tasks: dict[str, tuple] = {}
        try:
            while not self._push_shutdown_event.is_set():
                await asyncio.sleep(self._push_interval)
                if self._push_shutdown_event.is_set():
                    break
                if MainManager.signal_kill_server:
                    logger.info('Kill all server')
                    # 只有主 loop 明确确认全部 stop 成功，推送线程才进入退出路径。
                    if self._request_stop_all_from_push_thread():
                        raise SystemExit
                    logger.error('Kill all server incomplete; keep push thread alive for retry')
                    continue
                try:
                    await self._sync_push_tasks(tasks)
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    # registry 变化或单个协程异常只影响当前轮，下一轮继续服务其他实例。
                    logger.exception(f'push data iteration failed: {e}')
        finally:
            for entry in list(tasks.values()):
                await self._cancel_push_entry(entry)
            tasks.clear()

    async def restart_processes(self, script_instances: list[str]):
        for instance in script_instances:
            logger.info(f'Restart script {instance}')
            try:
                await self.start_script_process(instance)
            except FileNotFoundError:
                logger.error(f'{instance} file not found')
                continue

    def _disk_identity_active(self, name: str) -> bool:
        """按 sidecar 的 active 状态判断 registry 应保留的磁盘身份。"""
        record = self.store.generation.read_active_generation(name)
        return record is not None and record.state == "active"

    async def _reconcile_after_lifecycle_failure(self, *args, **kwargs) -> None:
        """取消期间有界等待对账；超时后托管 task 并保留可重试 registry。"""
        reconcile_task = asyncio.create_task(
            self._reconcile_lifecycle_registry(*args, **kwargs)
        )
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self._reconcile_timeout
        timed_out = False
        while not reconcile_task.done():
            remaining = deadline - loop.time()
            if remaining <= 0:
                timed_out = True
                break
            try:
                await asyncio.wait_for(asyncio.shield(reconcile_task), remaining)
            except asyncio.CancelledError:
                # 记录并继续到截止时间；外层仍会按原始异常优先级重抛。
                continue
            except asyncio.TimeoutError:
                timed_out = True
                break

        if timed_out and not reconcile_task.done():
            logger.error('lifecycle reconcile timed out; cancelling recovery task')
            reconcile_task.cancel()
            cancel_deadline = loop.time() + self._reconcile_cancel_timeout
            while not reconcile_task.done():
                remaining = cancel_deadline - loop.time()
                if remaining <= 0:
                    break
                try:
                    await asyncio.wait_for(asyncio.shield(reconcile_task), remaining)
                except asyncio.CancelledError:
                    continue
                except asyncio.TimeoutError:
                    break
            if not reconcile_task.done():
                # 不丢弃仍在运行的 task；由回调消费结果，避免未观察异常/孤儿任务。
                logger.error('lifecycle reconcile cancellation did not converge; task retained')
                self._remember_reconcile_task(reconcile_task)
                await self._preserve_reconcile_registry(args, kwargs)
                return

        # 正常完成或已取消完成时消费 result；异常由调用方保留原始生命周期异常。
        reconcile_task.result()

    async def _reconcile_lifecycle_registry(
        self,
        source: str,
        destination: str | None = None,
        process: ScriptProcess | None = None,
        was_running: bool = False,
    ) -> None:
        """恢复 journal 并按稳定磁盘身份修正 registry；任何对账失败都向上报告。"""
        # Store/FileLock 是同步 I/O，必须移出主 loop 才能让 reconcile timeout 真正生效。
        await asyncio.to_thread(self.store.reconcile_lifecycle_transactions)
        source_active = await asyncio.to_thread(self._disk_identity_active, source)
        source_generation = None
        if source_active:
            # 对象恢复前再次读取 generation，避免 ABA 期间把旧 wrapper 启回去。
            source_record = await asyncio.to_thread(
                self.store.generation.read_active_generation, source
            )
            source_active = source_record is not None and source_record.state == "active"
            source_generation = source_record.generation if source_active else None
        destination_record = None
        destination_generation = None
        if destination is not None:
            # 目标 generation 必须在事务恢复后重新读取，避免只凭 key 存在判断身份。
            destination_record = await asyncio.to_thread(
                self.store.generation.read_active_generation, destination
            )
        destination_active = (
            destination_record is not None and destination_record.state == "active"
        )
        if destination_active:
            destination_generation = destination_record.generation

        destination_process = None
        if destination is not None and destination_active and not source_active:
            with self._registry_lock:
                current_destination = self.script_process.get(destination)

            if (
                current_destination is not None
                and getattr(current_destination, "generation", _MISSING)
                != destination_generation
            ):
                # committed 后不能留下旧 generation wrapper；退役失败必须保留旧对象并报错。
                retired = await self._retire_stale_registry_process(
                    destination,
                    current_destination,
                    "lifecycle reconcile destination generation mismatch",
                )
                if not retired:
                    with self._registry_lock:
                        observed_destination = self.script_process.get(destination)
                    if observed_destination is current_destination:
                        raise ConfigGenerationError(
                            f"{destination}: stale registry wrapper generation "
                            f"{getattr(current_destination, 'generation', _MISSING)!r} "
                            f"could not be retired"
                        )
                    if (
                        observed_destination is None
                        or getattr(observed_destination, "generation", _MISSING)
                        != destination_generation
                    ):
                        # 旧句柄已不再可由 registry 确认管理时也不能伪装成成功。
                        raise ConfigGenerationError(
                            f"{destination}: registry changed while retiring stale wrapper"
                        )
                    current_destination = observed_destination
                else:
                    current_destination = None

            if current_destination is None:
                try:
                    destination_process = await asyncio.to_thread(
                        ScriptProcess,
                        destination,
                        store=self.store,
                        generation=destination_generation,
                    )
                except Exception as e:
                    # 磁盘 rename 已提交、source registry 已移除，但 destination wrapper 构造失败：
                    # 保留已提交磁盘状态不回滚，统一包装 ConfigGenerationError 向上传播，
                    # 避免 postcommit 被误映射为 503/200 假成功（语义应为 500 一致性失败）。
                    logger.error(
                        f'Config {source} renamed to {destination}, '
                        f'but destination process cache creation failed: {e}'
                    )
                    raise ConfigGenerationError(
                        f'{destination}: destination registry reconciliation/cache '
                        f'construction failed after rename committed'
                    ) from e
                else:
                    with self._registry_lock:
                        # 只在槽位仍为空时安装，不能覆盖并发路径先装入的新对象。
                        current_destination = self.script_process.get(destination)
                        if current_destination is None:
                            self.script_process[destination] = destination_process
                        elif (
                            getattr(current_destination, "generation", _MISSING)
                            != destination_generation
                        ):
                            raise ConfigGenerationError(
                                f"{destination}: concurrent registry wrapper has "
                                f"unexpected generation "
                                f"{getattr(current_destination, 'generation', _MISSING)!r}"
                            )

        if source_active and process is not None:
            process_generation = getattr(process, "generation", _MISSING)
            if process_generation != source_generation:
                # active source 已进入新 generation 时，旧 wrapper 必须先安全 retire，
                # 否则可能留下错误身份的 live handle；新 wrapper 则由 helper 的 CAS 保护。
                await self._retire_stale_registry_process(
                    source, process, "lifecycle reconcile generation mismatch"
                )

        with self._registry_lock:
            # 仅删除本事务选择的旧对象；新 generation/wrapper 一律不做盲删。
            if not source_active and self.script_process.get(source) is process:
                self.script_process.pop(source, None)
            if (
                destination_process is not None
                and destination not in self.script_process
            ):
                self.script_process[destination] = destination_process

        if source_active and process is not None and was_running:
            # 仅当 registry 对象与磁盘 generation 均未变化时恢复旧运行态。
            with self._registry_lock:
                current = self.script_process.get(source)
            process_generation = getattr(process, "generation", _MISSING)
            if current is not process or process_generation != source_generation:
                return
            if process.state == ScriptState.INACTIVE:
                try:
                    await self._start_process_locked(process)
                except BaseException as restore_error:
                    # 实际 ScriptProcess 可能仅在成功 spawn 后的广播阶段被取消；句柄仍存活即恢复成功。
                    restored_process = getattr(process, "_process", None)
                    if process.state != ScriptState.INACTIVE and restored_process is not None:
                        logger.warning(
                            f'[{source}] restore broadcast failed after process started: {restore_error}'
                        )
                        return
                    try:
                        await process.stop()
                    except BaseException as cleanup_error:
                        logger.error(
                            f'[{source}] process cleanup after restore start failure failed: '
                            f'{type(cleanup_error).__name__}: {cleanup_error}'
                        )
                    if getattr(process, "_process", None) is None:
                        process.state = ScriptState.INACTIVE
                    raise

    async def _ensure_destination_slot_reusable(self, destination: str) -> None:
        """预检 rename 目标槽位：tombstone/缺失目标只允许安全退役的陈旧 wrapper 复用。"""
        # validate_rename_names 已按 Store 冲突拒绝 active/creating 目标；这里再读一次
        # 磁盘身份，防止预检后由并发请求把目标改为 active/creating 时误退役正常目标。
        destination_record = self.store.generation.read_active_generation(destination)
        if destination_record is not None and destination_record.state != "tombstone":
            raise ConfigIdentityConflictError(f"{destination} already exists")
        with self._registry_lock:
            destination_process = self.script_process.get(destination)
        if destination_process is None:
            return
        # 磁盘 tombstone/不存在但 registry 仍持有旧 wrapper（删除时 fail-closed 保留）：
        # 必须先安全退役；无法确认退出（live/unknown/stop error/CancelledError/探针非 bool）
        # 时保留 wrapper/句柄，并在停止 source 与 Store 提交之前拒绝 rename。
        retired = await self._retire_stale_registry_process(
            destination,
            destination_process,
            "rename precommit destination reuse",
        )
        if retired:
            return
        raise ConfigIdentityConflictError(
            f"{destination}: stale registry wrapper generation "
            f"{getattr(destination_process, 'generation', _MISSING)!r} "
            f"could not be retired"
        )

    async def _rename_config_locked(self, source: str, destination: str) -> None:
        """调用方持有 manager 身份锁时执行完整 rename 与失败恢复。"""
        # 纯名称、源身份与确定的目标冲突必须早于 stop，避免无效请求中断运行实例。
        self.store.validate_rename_names(source, destination)
        # 目标磁盘 tombstone/不存在时 Store 允许复用名称，但 registry 可能仍持有删除时
        # 未确认退出的旧 wrapper；预检目标槽位，占用无法退役时必须在 stop source 与
        # Store 提交前拒绝，避免提交后才发现旧句柄与新 generation 交错。
        await self._ensure_destination_slot_reusable(destination)
        with self._registry_lock:
            process = self.script_process.get(source)
        was_running = process is not None and process.state != ScriptState.INACTIVE
        try:
            if process is not None:
                await process.stop()
            self.store.rename_config(source, destination)
        except BaseException as lifecycle_error:
            # stop 后事务未提交时恢复 source；若恢复本身失败，记录次生错误但保留原异常类型。
            try:
                await self._reconcile_after_lifecycle_failure(
                    source, destination, process=process, was_running=was_running
                )
            except BaseException as reconcile_error:
                logger.error(
                    f'Rename {source} to {destination} failed with '
                    f'{type(lifecycle_error).__name__}; registry reconciliation also failed: '
                    f'{type(reconcile_error).__name__}: {reconcile_error}'
                )
            raise

        # 仅对象 CAS 移除本事务选择的旧 wrapper，不能覆盖并发安装的新对象。
        with self._registry_lock:
            if self.script_process.get(source) is process:
                self.script_process.pop(source, None)
        await self._reconcile_lifecycle_registry(
            source, destination, process=process, was_running=False
        )

    async def rename_config(self, source: str, destination: str) -> None:
        """以 manager 身份锁覆盖 wrapper 选择、stop、Store 提交和 registry 对账。"""
        async with self._ensure_script_lock:
            await self._rename_config_locked(source, destination)

    async def _delete_config_locked(self, name: str) -> None:
        """调用方持有 manager 身份锁时执行完整 delete 与失败恢复。"""
        with self._registry_lock:
            process = self.script_process.get(name)
        was_running = process is not None and process.state != ScriptState.INACTIVE
        try:
            if process is not None:
                await process.stop()
            self.store.delete_config(name)
        except BaseException as lifecycle_error:
            # tombstone 尚未提交时恢复 source；恢复失败只记录，必须继续抛原生命周期异常。
            try:
                await self._reconcile_after_lifecycle_failure(
                    name, process=process, was_running=was_running
                )
            except BaseException as reconcile_error:
                logger.error(
                    f'Delete {name} failed with {type(lifecycle_error).__name__}; '
                    f'registry reconciliation also failed: '
                    f'{type(reconcile_error).__name__}: {reconcile_error}'
                )
            raise

        # 仅对象 CAS 移除本事务选择的旧 wrapper，不能误删等待路径安装的新对象。
        with self._registry_lock:
            if self.script_process.get(name) is process:
                self.script_process.pop(name, None)
        await self._reconcile_lifecycle_registry(name, process=process)

    async def delete_config(self, name: str) -> None:
        """以 manager 身份锁覆盖 wrapper 选择、stop、Store 提交和 registry 对账。"""
        async with self._ensure_script_lock:
            await self._delete_config_locked(name)


mm = MainManager()
