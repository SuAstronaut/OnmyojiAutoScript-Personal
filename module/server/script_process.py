# This Python file uses the following encoding: utf-8
# @author runhey
# 脚本进程
# github https://github.com/runhey
import sys, os
import signal
import threading
import asyncio
import multiprocessing
import queue as queue_module
import uuid
import time
from multiprocessing.process import BaseProcess
from asyncio import QueueEmpty, CancelledError, sleep
from enum import Enum

from module.logger import logger
from module.config.config_store import ConfigGenerationMismatchError
from module.config.scheduler_runtime import SchedulerRuntime
from module.server.script_websocket import ScriptWSManager
from module.exception import RequestHumanTakeover


class ScriptStartupError(RuntimeError):
    """子进程启动握手失败；HTTP 层记录原始异常后返回安全的 500。"""


class ScriptStartupProcessExitedError(ScriptStartupError):
    """子进程退出且未收到本次启动握手。"""

    def __init__(self, message, *, exitcode=None):
        super().__init__(message)
        self.exitcode = exitcode


class ScriptStartupTimeoutError(ScriptStartupError):
    """子进程 generation 握手超时，属于启动失败而非 filelock 锁超时。

    与 filelock.Timeout（继承 builtin TimeoutError）严格区分：路由据此映射
    HTTP 500（startup/consistency 失败），而不是把子进程启动失败误标为 503 配置锁超时。
    """


def _start_diag(stage, config, generation, nonce, pid, *, level="info", **fields):
    """固定阶段日志；不记录配置内容，也不在每次 poll 时输出。"""
    context = dict(config=config, generation=generation,
                   nonce=str(nonce or "")[:12], pid=pid, **fields)
    getattr(logger, level)(
        f"[START_DIAG] {stage} " + " ".join(f"{key}={value!r}" for key, value in context.items())
    )


def _startup_process_status(process):
    """诊断探针失败只返回 unknown，不能覆盖原始启动/清理异常。"""
    status = dict(pid=None, is_alive=None, exitcode=None)
    errors = []
    if process is not None:
        for key in status:
            try:
                value = process.is_alive() if key == "is_alive" else getattr(process, key)
                if key == "is_alive" and value is not True and value is not False:
                    raise TypeError("non-boolean process alive result")
                status[key] = value
            except Exception as error:
                errors.append(f"{key}:{type(error).__name__}")
    if errors:
        status["status_errors"] = ",".join(errors)
    return status


class ScriptState(int, Enum):
    INACTIVE = 0
    RUNNING = 1
    WARNING = 2
    UPDATING = 3


class ScriptProcess(ScriptWSManager):

    def __init__(self, config_name: str, store=None, generation: str = None) -> None:
        super().__init__()
        # 通过注入 Store 的 load 验证 active identity 并缓存 generation；不再直接枚举配置文件。
        if store is None:
            from module.config.config_store import ConfigStore
            from pathlib import Path
            store = ConfigStore(config_root=Path.cwd() / 'config')
        self.store = store
        loaded = self.store.load(config_name)
        self.config_name = config_name  # config_name
        self.generation = generation or loaded.generation
        self.log_pipe_out, self.log_pipe_in = multiprocessing.Pipe(False)
        self.state_queue = multiprocessing.Queue()  # 子进程→主进程状态上报
        self.config_event_queue = multiprocessing.Queue()  # 主进程→子进程配置变更提示
        # 子进程完成 generation 校验后向父进程发送一次性 ready/failed 握手。
        self.ready_queue = multiprocessing.Queue()
        # 握手超时窗口（秒）：子进程要完成全量 import（script.py 依赖链）+
        # Script 构造后才上报 ready，冷启动实测 3s 上下、叠加 Windows spawn
        # bootstrap 后压在 5s 线附近抖动（2026-09-07 远端实测两次超时一次
        # 过线），放宽到 10s 消除边缘失败
        self._spawn_handshake_timeout = 10.0
        self._spawn_attempt_nonce = None
        self.scheduler_runtime = None
        self._schedule_projection_lock = threading.Lock()
        self._schedule_revision = 0
        self._schedule_projection_id = uuid.uuid4().hex
        # 独立线程锁只串行化进程句柄及其耦合状态的短提交，不覆盖任何阻塞操作。
        self._process_state_lock = threading.Lock()
        with self._process_state_lock:
            self.state: ScriptState = ScriptState.INACTIVE
            self._config_state_cache: dict | None = None
            self._process = None
        # start/stop 共用实例级锁，完整串行化状态广播、spawn、引用提交与回收。
        self._lifecycle_lock = asyncio.Lock()

    def deliver_config_changed(self, generation: str, mtime_ns: int, changed_paths) -> None:
        """主进程在 successful patch 后锁外投递固定 config_changed 事件。

        队列满时合并为最新 generation/mtime 与路径并集；丢事件也不丢配置，
        子进程侧仍会以 mtime_ns 兜底检测（规格 §4.4）。
        """
        # str 路径视为单段，避免被 tuple() 拆成字符（防御）
        paths = sorted({tuple(p) if isinstance(p, (tuple, list)) else (p,) for p in changed_paths})
        event = {
            "type": "config_changed",
            "generation": generation,
            "mtime_ns": mtime_ns,
            "changed_paths": paths,
        }
        try:
            self.config_event_queue.put_nowait(event)
            return
        except Exception:
            # 队列满合并分支（防御性）：multiprocessing.Queue() 默认 maxsize=0 在 CPython
            # 映射为无界队列，put_nowait 生产上不抛 Full，本分支为 future-proof 保留；
            # 若未来改为有界队列，排空既有事件合并 latest generation/mtime 与路径并集，
            # 保证低延迟提示不丢失（丢事件仍可被子进程 mtime_ns 兜底，规格 §4.4）。
            merged_paths: set = set(paths)
            latest_mtime = mtime_ns
            latest_gen = generation
            while True:
                try:
                    old = self.config_event_queue.get_nowait()
                except Exception:
                    break
                if isinstance(old, dict) and old.get("type") == "config_changed":
                    merged_paths.update(
                        tuple(p) if isinstance(p, (tuple, list)) else (p,)
                        for p in old.get("changed_paths") or []
                    )
                    if (old.get("mtime_ns") or 0) > latest_mtime:
                        latest_mtime = old["mtime_ns"]
                        latest_gen = old.get("generation") or latest_gen
            merged = {
                "type": "config_changed",
                "generation": latest_gen,
                "mtime_ns": latest_mtime,
                "changed_paths": sorted(merged_paths),
            }
            try:
                self.config_event_queue.put_nowait(merged)
            except Exception:
                # 仍失败则丢弃（mtime 兜底仍可检出变化），不阻塞主进程
                logger.warning(f'[{self.config_name}] config event queue full, event dropped')

    def _live_scheduler_runtime(self):
        process = self._process_snapshot()
        if process is None:
            return None
        alive, error = self._process_alive_status(process, 'scheduler')
        if alive is None:
            raise RuntimeError('Cannot verify scheduler process state') from error
        if alive and self.scheduler_runtime is None:
            raise RuntimeError('Live scheduler has no execution marker')
        return self.scheduler_runtime if alive else None

    async def scheduler_action(self, task: str, action: str, expected_generation=None):
        # Start/stop cannot swap the spawn buffer while a worker is committing.
        async with self._lifecycle_lock:
            runtime = self._live_scheduler_runtime()
            operation = asyncio.create_task(asyncio.to_thread(
                self.store.scheduler_action, self.config_name, task, action,
                expected_generation or self.generation, runtime))
            try:
                return await asyncio.shield(operation)
            except asyncio.CancelledError:
                await operation
                raise

    def schedule_snapshot(self) -> dict:
        """Reproject every HTTP/WS schedule from disk + actual spawn state.

        Child queue entries are refresh hints, never forwarded as stale data.
        Monotonic per-wrapper revisions let clients drop delayed older frames.
        """
        from module.config.config import Config
        with self._schedule_projection_lock:
            runtime = self._live_scheduler_runtime()
            with self.store.generation.identity_lifecycle_lock(self.config_name):
                loaded = self.store._load_unlocked(self.config_name)
                if loaded.generation != self.generation:
                    raise ConfigGenerationMismatchError('Scheduler configuration changed')
                actual = runtime.read_locked() if runtime is not None else ''
                config = Config(self.config_name, store=self.store, loaded=loaded)
                config.update_scheduler()
                schedule = config.get_schedule_data(running_task=actual)
                self._schedule_revision += 1
                schedule.update(generation=loaded.generation, revision=self._schedule_revision,
                                projection_id=self._schedule_projection_id)
                return schedule

    async def broadcast_state(self, data: dict):
        if 'schedule' in data:
            data = {**data, 'schedule': await asyncio.to_thread(self.schedule_snapshot)}
        await super().broadcast_state(data)

    async def send_state(self, websocket, data: dict):
        if 'schedule' in data:
            data = {**data, 'schedule': await asyncio.to_thread(self.schedule_snapshot)}
        await super().send_state(websocket, data)

    def _drain_config_event_queue(self) -> None:
        """清空 config_event_queue 中尚未消费的事件（stop/异常退出时调用）。"""
        queue = self.config_event_queue
        if queue is None:
            return
        while True:
            try:
                queue.get_nowait()
            except Exception:
                break

    def _drain_state_queue(self) -> None:
        """清空 state_queue 中残留的上报（stop/restart 时调用，避免消费上一进程状态）。"""
        queue = self.state_queue
        if queue is None:
            return
        while True:
            try:
                queue.get_nowait()
            except Exception:
                break

    def _drain_ready_queue(self) -> None:
        """启动前清理旧握手消息，避免把上一子进程的 ready 当成当前结果。"""
        while True:
            try:
                self.ready_queue.get_nowait()
            except Exception:
                break

    async def _wait_for_spawn_handshake(self, process, attempt_nonce=None) -> None:
        """等待真实子进程完成 generation/nonce 校验；测试替身不伪造握手时保持兼容。"""
        if not isinstance(process, BaseProcess):
            return
        expected_nonce = attempt_nonce or self._spawn_attempt_nonce
        started_at = time.monotonic()
        deadline = started_at + self._spawn_handshake_timeout
        ignored_nonce = ignored_generation = 0

        def diagnose(stage, *, level="info", status=None, **fields):
            details = status if status is not None else _startup_process_status(process)
            _start_diag(stage, self.config_name, self.generation, expected_nonce,
                        details["pid"], level=level,
                        elapsed=f"{time.monotonic() - started_at:.3f}s",
                        timeout=self._spawn_handshake_timeout,
                        ignored=ignored_nonce + ignored_generation,
                        ignored_nonce=ignored_nonce, ignored_generation=ignored_generation,
                        **{key: value for key, value in details.items() if key != "pid"}, **fields)

        def read_message():
            try:
                return self.ready_queue.get_nowait()
            except queue_module.Empty:
                raise
            except Exception as error:
                diagnose("handshake_queue_error", level="error", error_type=type(error).__name__)
                raise ScriptStartupError(
                    f"[{self.config_name}] child generation handshake queue failed"
                ) from error

        diagnose("handshake_wait_begin")
        while True:
            try:
                message = read_message()
            except queue_module.Empty:
                status = _startup_process_status(process)
                exited = status["is_alive"] is False or status["exitcode"] is not None
                expired = time.monotonic() >= deadline
                if not exited and not expired:
                    await sleep(0.01)
                    continue
                # 查询进程状态期间 feeder 可能刚把 ready 送到；先再读一次再宣告失败。
                try:
                    message = read_message()
                except queue_module.Empty:
                    if exited:
                        diagnose("handshake_child_exited", level="error", status=status)
                        raise ScriptStartupProcessExitedError(
                            f"Script process exited before startup handshake: "
                            f"config={self.config_name} pid={status['pid']} "
                            f"exitcode={status['exitcode']} generation={self.generation} "
                            f"elapsed={time.monotonic() - started_at:.3f}s",
                            exitcode=status["exitcode"],
                        )
                    diagnose("handshake_timeout", level="error", status=status)
                    raise ScriptStartupTimeoutError(
                        f"[{self.config_name}] child generation handshake timed out "
                        f"after {self._spawn_handshake_timeout:.1f}s"
                    )

            if not isinstance(message, dict):
                diagnose("handshake_invalid", level="error", message_type=type(message).__name__)
                raise ScriptStartupError(
                    f"[{self.config_name}] invalid child generation handshake"
                )
            status = message.get("status")
            generation = message.get("generation")
            if message.get("nonce") != expected_nonce:
                # 队列 feeder 延迟投递的旧消息不能污染本次 spawn。
                ignored_nonce += 1
                if ignored_nonce == 1:
                    diagnose("handshake_ignored_nonce", level="debug")
                continue
            if status == "ready" and generation == self.generation:
                diagnose("handshake_ready")
                return
            if status == "ready" and generation != self.generation:
                ignored_generation += 1
                if ignored_generation == 1:
                    diagnose("handshake_ignored_generation", level="debug")
                continue
            if status == "failed" and message.get("reason") == "generation_mismatch":
                diagnose("handshake_generation_mismatch", level="warning")
                raise ConfigGenerationMismatchError(
                    f"{self.config_name} child generation mismatch: {generation}"
                )
            reason = message.get("reason") or "unknown child startup failure"
            diagnose("handshake_failed", level="error")
            raise ScriptStartupError(f"[{self.config_name}] child startup failed: {reason}")

    def cached_config_state(self) -> dict:
        """返回缓存的 config_state；inactive 或尚无上报时固定返回空 pending/current。"""
        with self._process_state_lock:
            inactive = self.state == ScriptState.INACTIVE
            cached = self._config_state_cache
        if inactive:
            return {
                "pending_restart_paths": [],
                "pending_warm_paths": [],
                "observed_mtime_ns": 0,
                "status": "current",
            }
        if cached is not None:
            return cached
        return {
            "pending_restart_paths": [],
            "pending_warm_paths": [],
            "observed_mtime_ns": 0,
            "status": "current",
        }

    def _clear_config_state(self) -> None:
        """在进程状态锁内清空 config_state cache 与两条非阻塞队列。"""
        self._config_state_cache = None
        # Detach, rather than reuse, a stopped/failed child's shared buffer.
        self.scheduler_runtime = None
        self._drain_config_event_queue()
        self._drain_state_queue()

    def _process_snapshot(self):
        """在线程短锁内读取当前进程句柄。"""
        with self._process_state_lock:
            return self._process

    def _commit_process_if_current(
        self, expected, replacement, state: ScriptState, *, clear_state: bool = False
    ) -> bool:
        """仅当句柄身份未变时，原子提交句柄及其耦合状态。"""
        with self._process_state_lock:
            if self._process is not expected:
                return False
            if clear_state:
                self._clear_config_state()
            self._process = replacement
            self.state = state
            return True

    def _process_alive_status(self, process, operation: str) -> tuple[bool | None, Exception | None]:
        """查询进程存活状态；只有明确 bool 才能确认退出，其他值均为 unknown。"""
        try:
            alive = process.is_alive()
            if alive is True or alive is False:
                return alive, None
            raise TypeError(f'non-boolean alive result: {alive!r}')
        except Exception as error:
            logger.error(f'[{self.config_name}] {operation} alive check failed: {error}')
            return None, error

    async def _broadcast_lifecycle_state(self) -> asyncio.CancelledError | None:
        """事务完成后广播实际状态；普通广播错误只记录，取消由调用方决定是否重抛。"""
        try:
            with self._process_state_lock:
                state = self.state
            await self.broadcast_state({"state": state})
            await self.broadcast_state({'schedule': {}})
        except asyncio.CancelledError as cancel_error:
            return cancel_error
        except Exception as broadcast_error:
            logger.error(f'[{self.config_name}] lifecycle state broadcast failed: {broadcast_error}')
        return None

    def _cleanup_spawn_failure(self, process) -> None:
        """同步清理局部 spawn 进程；任何可取消 await 前先让句柄可达。"""
        with self._process_state_lock:
            owns_handle = self._process is None or self._process is process
            if owns_handle:
                self._process = process

        final_status = False
        if process is not None:
            def alive_status() -> bool | None:
                try:
                    alive = process.is_alive()
                    if alive is True or alive is False:
                        return alive
                    raise TypeError(f'non-boolean alive result: {alive!r}')
                except Exception as check_error:
                    logger.error(f'[{self.config_name}] spawn cleanup alive check failed: {check_error}')
                    return None

            status = alive_status()
            if status is not False:
                try:
                    process.terminate()
                except Exception as cleanup_error:
                    logger.error(f'[{self.config_name}] spawn cleanup terminate failed: {cleanup_error}')
                try:
                    process.join(timeout=0.7)
                except Exception as cleanup_error:
                    logger.error(f'[{self.config_name}] spawn cleanup join failed: {cleanup_error}')
                status = alive_status()
                if status is not False:
                    try:
                        process.kill()
                    except Exception as cleanup_error:
                        logger.error(f'[{self.config_name}] spawn cleanup kill failed: {cleanup_error}')
                    try:
                        process.join(timeout=2.0)
                    except Exception as cleanup_error:
                        logger.error(f'[{self.config_name}] spawn cleanup final join failed: {cleanup_error}')
                    status = alive_status()
            final_status = status is not False

        retained = False
        if owns_handle:
            if process is None or not final_status:
                self._commit_process_if_current(
                    process, None, ScriptState.INACTIVE, clear_state=True
                )
            else:
                retained = self._commit_process_if_current(
                    process, process, ScriptState.RUNNING
                )
        if retained:
            # 未确认退出的句柄必须继续可达，并保持运行态供后续 stop 重试。
            logger.error(f'[{self.config_name}] spawn cleanup incomplete; process handle retained')

    async def start(self) -> bool:
        """串行提交启动事务；生命周期广播移到锁外，避免广播卡死阻塞 stop。"""
        async with self._lifecycle_lock:
            started = await self._start_locked()
        if not started:
            return started
        cancel_error = await self._broadcast_lifecycle_state()
        if cancel_error is not None:
            raise cancel_error
        return True

    async def _start_locked(self) -> bool:
        # start 前先快速复核缓存 generation；磁盘身份变化则拒绝启动。
        try:
            loaded = self.store.load(self.config_name)
        except Exception as start_error:
            logger.error(f'[{self.config_name}] refuse to start, config load failed: {start_error}')
            try:
                await self._stop_locked(broadcast=False)
            except BaseException as cleanup_error:
                logger.error(f'[{self.config_name}] start failure cleanup failed: {cleanup_error}')
            # 清理广播异常或取消不能覆盖原始 load 异常。
            raise
        if loaded.generation != self.generation:
            logger.error(f'[{self.config_name}] generation changed since process created, refuse to start')
            await self._stop_locked(broadcast=False)
            return False

        # start 幂等：已有存活进程直接返回，不再先停后起——否则 daemon 重启或
        # API/GUI 重复 start 会把正常运行的脚本进程杀掉重拉（原实现等于 restart）。
        existing = self._process_snapshot()
        if existing is not None:
            alive, _alive_error = self._process_alive_status(existing, "start")
            if alive is True:
                logger.info(f'[{self.config_name}] already running, start is a no-op')
                return True
            # 句柄残留但进程已死或状态未知：走 stop 清理，再重新 spawn
            await self._stop_locked(broadcast=False)
        spawn_attempt_nonce = uuid.uuid4().hex
        with self._process_state_lock:
            self._config_state_cache = None
            self._drain_state_queue()
            self._drain_ready_queue()
            self._spawn_attempt_nonce = spawn_attempt_nonce

        new_process = None
        try:
            # lifecycle 锁覆盖最终 load、Process 构造与 spawn，阻止 delete/create 插入 TOCTOU 窗口。
            with self.store.generation.identity_lifecycle_lock(self.config_name):
                final_loaded = self.store._load_unlocked(self.config_name)
                if final_loaded.generation != self.generation:
                    raise ConfigGenerationMismatchError(
                        f'{self.config_name} generation changed before spawn'
                    )
                self.scheduler_runtime = SchedulerRuntime(self.generation)
                new_process = multiprocessing.Process(
                    target=func,
                    args=(
                        self.config_name,
                        self.generation,
                        self.state_queue,
                        self.log_pipe_in,
                        self.config_event_queue,
                        self.ready_queue,
                        spawn_attempt_nonce,
                        self.scheduler_runtime,
                    ),
                    name=self.config_name,
                    daemon=True,
                )
                _start_diag("spawn_begin", self.config_name, self.generation,
                            spawn_attempt_nonce, None)
                spawn_started_at = time.monotonic()
                new_process.start()
                spawn_elapsed = time.monotonic() - spawn_started_at
                _start_diag("spawn_return", self.config_name, self.generation,
                            spawn_attempt_nonce, _startup_process_status(new_process)["pid"],
                            elapsed=f"{spawn_elapsed:.3f}s")
        except ConfigGenerationMismatchError:
            await self._stop_locked(broadcast=False)
            return False
        except BaseException as spawn_error:
            # 先同步建立可达句柄并清理，再重抛原始 spawn/取消异常。
            _start_diag("spawn_failed", self.config_name, self.generation,
                        spawn_attempt_nonce, _startup_process_status(new_process)["pid"],
                        level="exception" if isinstance(spawn_error, Exception) else "error",
                        error_type=type(spawn_error).__name__)
            self._cleanup_spawn_failure(new_process)
            self._diagnose_startup_cleanup(new_process, spawn_attempt_nonce)
            raise

        if not self._commit_process_if_current(
            None, new_process, ScriptState.RUNNING
        ):
            # lifecycle 锁外仍可能有独立线程提交句柄；拒绝覆盖并回收本次局部进程。
            self._cleanup_spawn_failure(new_process)
            self._diagnose_startup_cleanup(new_process, spawn_attempt_nonce)
            raise RuntimeError(
                f'[{self.config_name}] process handle changed before spawn commit'
            )
        try:
            # 父端只有收到 child generation ready 后才向 API 报告启动成功。
            await self._wait_for_spawn_handshake(new_process, spawn_attempt_nonce)
        except ConfigGenerationMismatchError:
            try:
                await self._stop_locked(broadcast=False)
            except BaseException as cleanup_error:
                logger.error(
                    f'[{self.config_name}] generation handshake cleanup failed: '
                    f'{cleanup_error}'
                )
            self._diagnose_startup_cleanup(new_process, spawn_attempt_nonce)
            return False
        except BaseException:
            try:
                await self._stop_locked(broadcast=False)
            except BaseException as cleanup_error:
                logger.error(
                    f'[{self.config_name}] child handshake cleanup failed: '
                    f'{cleanup_error}'
                )
            self._diagnose_startup_cleanup(new_process, spawn_attempt_nonce)
            raise
        return True

    def _diagnose_startup_cleanup(self, process, attempt_nonce):
        status = _startup_process_status(process)
        retained = process is not None and self._process_snapshot() is process
        confirmed_exited = process is None or status["is_alive"] is False or status["exitcode"] is not None
        _start_diag("startup_cleanup", self.config_name, self.generation,
                    attempt_nonce, status["pid"],
                    level="info" if confirmed_exited and not retained else "error",
                    confirmed_exited=confirmed_exited, handle_retained=retained,
                    **{key: value for key, value in status.items() if key != "pid"})

    async def stop(self):
        """提交停止状态后在锁外广播，广播卡死也不阻塞后续 stop/retry。"""
        async with self._lifecycle_lock:
            await self._stop_locked(broadcast=False)
        cancel_error = await self._broadcast_lifecycle_state()
        if cancel_error is not None:
            raise cancel_error

    def _notify_ocr_instance_stopped(self) -> None:
        """实例被强制停止前注销 OCR 活跃状态，避免等待超时回收模型。"""
        try:
            from module.ocr.rpc import notify_ocr_instance_state
            notify_ocr_instance_state(self.config_name, False)
        except Exception as error:
            logger.debug(f'[{self.config_name}] OCR stop notification failed: {error}')

    async def _stop_locked(self, broadcast: bool = False):
        """先回收进程并收敛真实状态，再广播；查询异常仍继续 terminate/kill。"""
        process = self._process_snapshot()
        if process is not None:
            self._notify_ocr_instance_stopped()
        process_error = None
        status = False
        if process is not None:
            status, process_error = self._process_alive_status(process, "stop")
            if status is not False:
                try:
                    process.terminate()
                except BaseException as error:
                    if process_error is None:
                        process_error = error
                    logger.error(f'[{self.config_name}] stop terminate failed: {error}')
                try:
                    process.join(timeout=0.7)
                except BaseException as error:
                    if process_error is None:
                        process_error = error
                    logger.error(f'[{self.config_name}] stop join failed: {error}')
                status, _check_error = self._process_alive_status(process, "stop")
                if status is not False:
                    try:
                        process.kill()
                    except BaseException as error:
                        if process_error is None:
                            process_error = error
                        logger.error(f'[{self.config_name}] stop kill failed: {error}')
                    try:
                        process.join(timeout=2.0)
                    except BaseException as error:
                        if process_error is None:
                            process_error = error
                        logger.error(f'[{self.config_name}] stop final join failed: {error}')
                    status, _check_error = self._process_alive_status(process, "stop")
                    if process_error is None and status is not False:
                        process_error = RuntimeError(
                            f'Script {self.config_name} subprocess kill failed'
                        )

        if process is None or status is False:
            committed = self._commit_process_if_current(
                process, None, ScriptState.INACTIVE, clear_state=True
            )
        else:
            # 未确认退出的句柄必须继续可达；后续 stop 可重试，不能虚报已停止。
            committed = self._commit_process_if_current(
                process, process, ScriptState.RUNNING
            )

        cancel_error = None
        if broadcast and committed:
            cancel_error = await self._broadcast_lifecycle_state()
        if process_error is not None:
            raise process_error
        if cancel_error is not None:
            raise cancel_error

    async def coroutine_broadcast_state(self):
        try:
            while 1:
                with self._process_state_lock:
                    inactive = self.state == ScriptState.INACTIVE
                if inactive:
                    await sleep(1)
                    continue
                await sleep(0.1)
                # 子进程异常退出/mismatch 退出时清理；存活查询异常只记录并继续管理。
                checked_process = self._process_snapshot()
                if checked_process is None:
                    continue
                alive, _error = self._process_alive_status(
                    checked_process, "state broadcaster"
                )
                if alive is None:
                    await sleep(0.1)
                    continue
                if not alive:
                    # 独立 loop 只以对象身份 CAS 清理；身份复核与关联状态提交同锁完成。
                    cleared = self._commit_process_if_current(
                        checked_process,
                        None,
                        ScriptState.INACTIVE,
                        clear_state=True,
                    )
                    if not cleared:
                        continue
                    logger.warning(f'[{self.config_name}] script process exited, clear config state')
                    # 锁外仅广播本次 CAS 已确定的 INACTIVE，避免读取后续 replacement 状态。
                    await self.broadcast_state({"state": ScriptState.INACTIVE})
                    try:
                        await self.broadcast_state({'schedule': {}})
                    except Exception as error:
                        logger.warning(f'[{self.config_name}] exit schedule refresh failed: {error}')
                    continue
                try:
                    with self._process_state_lock:
                        if self._process is not checked_process:
                            continue
                    queue_empty = self.state_queue.empty()
                    data = None if queue_empty else self.state_queue.get_nowait()
                    if queue_empty:
                        await sleep(1)
                        continue
                    if not data:
                        await sleep(0.5)
                        continue
                    with self._process_state_lock:
                        if self._process is not checked_process:
                            continue
                        if 'state' in data and data['state'] == ScriptState.WARNING:
                            self.state = ScriptState.WARNING
                        if 'config_state' in data:
                            # 缓存子进程上报的完整 config_state，供新连接定向首帧使用
                            self._config_state_cache = data['config_state']
                    await self.broadcast_state(data)
                except QueueEmpty as e:
                    logger.warning(f'QueueEmpty: {e}')
                    await sleep(0.5)
                    continue
                except Exception as e:
                    logger.error(f'Error: {e}')
                    continue
        except CancelledError as e:
            logger.warning(f'{self.config_name} state coroutine is cancelled')
            return

    async def coroutine_broadcast_log(self):
        try:
            while 1:
                if self.state == ScriptState.INACTIVE:
                    await sleep(1)
                    continue
                await sleep(0.05)
                try:
                    if not self.log_pipe_out.poll():
                        await sleep(0.3)
                        continue
                    log = self.log_pipe_out.recv()
                    if not log:
                        await sleep(0.5)
                        continue
                    await self.broadcast_log(log)
                except EOFError as e:
                    await sleep(0.5)
                    logger.warning(f'EOFError: {e}')
                    continue
                except Exception as e:
                    logger.error(f'Log Error: {e}')
                    continue
        except CancelledError as e:
            logger.warning(f'{self.config_name} log coroutine is cancelled')
            return


def func(
    config: str,
    expected_generation: str,
    state_queue: multiprocessing.Queue,
    log_pipe_in,
    config_event_queue=None,
    ready_queue=None,
    attempt_nonce=None,
    scheduler_runtime=None,
) -> None:
    child_started_at = time.monotonic()
    startup_stage = "child_enter"
    handshake_ready = False

    def diagnose_child(stage, *, level="info", **fields):
        _start_diag(stage, config, expected_generation, attempt_nonce, os.getpid(),
                    level=level, elapsed=f"{time.monotonic() - child_started_at:.3f}s", **fields)

    # func 入口表示 spawn bootstrap 已到达 target；入口前崩溃由父端 exitcode 诊断。
    diagnose_child("child_enter", next_stage="log_setup")

    def signal_handler(signum, frame):
        logger.info(f'Script {config} received signal {signum}, exiting gracefully')
        log_pipe_in.close()
        state_queue.close()
        if config_event_queue is not None:
            config_event_queue.close()
        if ready_queue is not None:
            ready_queue.close()
        sys.exit(0)

    def start_log() -> None:
        # 外层统一记录/上报启动异常，避免 log setup 失败时重复两份 traceback。
        from module.logger import set_file_logger, set_func_logger
        set_file_logger(name=config)
        set_func_logger(log_pipe_in.send)

    def report_handshake(status: str, generation=None, reason: str = "") -> None:
        """把 child generation 校验结果送回父进程；队列失败只记录并继续退出。"""
        if ready_queue is None:
            return
        try:
            diagnose_child("handshake_send_begin", level="debug", status=status)
            ready_queue.put({
                "status": status,
                "generation": generation,
                "nonce": attempt_nonce,
                "reason": reason,
            })
            # Queue.put 仅表示进入 feeder 缓冲；父端 handshake_ready 才证明消息被读到。
            diagnose_child("handshake_sent", level="info" if status == "ready" else "warning",
                           status=status, actual_generation=generation, delivery="queued")
        except Exception as error:
            diagnose_child("handshake_send_failed", level="exception",
                           status=status, error_type=type(error).__name__)

    try:
        startup_stage = "log_setup"
        signal.signal(signal.SIGTERM, signal_handler)
        signal.signal(signal.SIGINT, signal_handler)
        start_log()
        startup_stage = "script_import_begin"
        phase_started_at = time.monotonic()
        diagnose_child("script_import_begin")
        from script import Script
        diagnose_child("script_import_done", next_stage="script_construct_begin",
                       phase_elapsed=f"{time.monotonic() - phase_started_at:.3f}s")
        startup_stage = "script_construct_begin"
        phase_started_at = time.monotonic()
        script = Script(config_name=config)
        diagnose_child("script_construct_done", next_stage="config_load_begin",
                       phase_elapsed=f"{time.monotonic() - phase_started_at:.3f}s")
        startup_stage = "config_load_begin"
        phase_started_at = time.monotonic()
        actual_generation = script.config.generation
        if expected_generation and actual_generation != expected_generation:
            # 子进程再次核对身份，防止 spawn 后同名配置已发生 ABA 时以新身份运行。
            logger.error(f'[{config}] child generation changed before loop, refuse to run')
            report_handshake(
                "failed",
                generation=actual_generation,
                reason="generation_mismatch",
            )
            return
        diagnose_child("config_identity_ready", next_stage="handshake_send_begin",
                       actual_generation=actual_generation,
                       phase_elapsed=f"{time.monotonic() - phase_started_at:.3f}s")
        script.state_queue = state_queue
        script.config_event_queue = config_event_queue
        script.scheduler_runtime = scheduler_runtime
        startup_stage = "handshake_send_begin"
        report_handshake("ready", generation=actual_generation)
        handshake_ready = True
        script.loop()
    except RequestHumanTakeover as e:
        if not handshake_ready:
            diagnose_child("child_startup_failed", level="exception", child_stage=startup_stage)
        report_handshake("failed", reason=f"human takeover: {e}")
        logger.critical(f'Script {config} requires human takeover: {e}')
        state_queue.put({"state": ScriptState.WARNING})
        time.sleep(0.1)
        exit(-1)
    except SystemExit as e:
        if not handshake_ready:
            # Script.config 的普通异常已 logger.exception 后 exit；不要重复其 traceback。
            # RequestHumanTakeover 分支只有 critical，仍需在这里补异常链。
            config_logged = (startup_stage == "config_load_begin" and e.__context__ is not None
                             and not isinstance(e.__context__, RequestHumanTakeover))
            diagnose_child("child_startup_failed", level="error" if config_logged else "exception",
                           child_stage=startup_stage, exitcode=e.code,
                           traceback_already_logged=config_logged)
        report_handshake("failed", reason=f"system exit: {e}")
        logger.info(f'Script {config} process exit')
        logger.error(f'Error: {e}')
        state_queue.put({"state": ScriptState.WARNING})
        time.sleep(0.1)
        exit(-1)
    except Exception as e:
        report_handshake("failed", reason=f"{type(e).__name__}: {e}")
        if not handshake_ready:
            diagnose_child("child_startup_failed", level="exception", child_stage=startup_stage)
        else:
            logger.exception(f'Run script {config} error')
        logger.error(f'Error: {e}')
        raise


if __name__ == '__main__':
    p = ScriptProcess('oas1')
    p.start()
    from time import sleep
    sleep(10)
    logger.info(p._process.exitcode)
