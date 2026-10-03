"""Offline tests of real startup methods, isolated from OAS import side effects.

AST-load production definitions; replace only processes, queues, clock, logging
and child Script. No real process, server, config file or game is started.
Run: toolkit/python.exe -B -m unittest discover -s dev_tools -p test_script_start_diagnostics.py -v
"""
import ast
import asyncio
import queue
import sys
import threading
import types
import unittest
import uuid
from contextlib import nullcontext
from enum import Enum
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

from fastapi import HTTPException


ROOT = Path(__file__).resolve().parents[1]
PROCESS_SOURCE = ROOT / 'module/server/script_process.py'
ROUTE_SOURCE = ROOT / 'module/server/script_router.py'
NONCE = 'a' * 32
GENERATION = 'test-generation'


class Clock:
    def __init__(self):
        self.now = 0.0

    def monotonic(self):
        return self.now

    async def sleep(self, duration):
        self.now += duration
        await asyncio.sleep(0)


class Process:
    def __init__(self, alive=True, exitcode=None, pid=12345):
        self.alive, self.exitcode, self.pid = alive, exitcode, pid
        self.start = Mock()
        self.join = Mock()
        self.terminate = Mock(side_effect=self._terminate)
        self.kill = Mock(side_effect=self._terminate)

    def is_alive(self):
        return self.alive

    def _terminate(self):
        self.alive, self.exitcode = False, -15


class WS:
    def __init__(self):
        self.active_connections = []
        self.broadcasts = []
        self.logs = []

    async def broadcast_state(self, data):
        self.broadcasts.append(data)
        await asyncio.sleep(0)

    async def broadcast_log(self, data):
        self.logs.append(data)
        await asyncio.sleep(0)


def load_production(clock, logger):
    """Compile all ScriptProcess methods and helpers, not module imports."""
    namespace = dict(
        asyncio=asyncio, time=types.SimpleNamespace(monotonic=clock.monotonic, sleep=Mock()),
        threading=threading, uuid=uuid, Enum=Enum, BaseProcess=Process,
        ScriptWSManager=WS, logger=logger, queue_module=queue,
        QueueEmpty=asyncio.QueueEmpty, CancelledError=asyncio.CancelledError,
        sleep=clock.sleep, os=types.SimpleNamespace(getpid=lambda: 34567), sys=sys,
        signal=types.SimpleNamespace(signal=Mock(), SIGTERM=15, SIGINT=2),
        SchedulerRuntime=lambda generation: types.SimpleNamespace(generation=generation),
        multiprocessing=types.SimpleNamespace(Queue=queue.Queue, Pipe=lambda _: (Mock(), Mock())),
    )
    for name in ('ConfigGenerationMismatchError', 'RequestHumanTakeover'):
        namespace[name] = type(name, (Exception,), {})
    tree = ast.parse(PROCESS_SOURCE.read_text(encoding='utf-8-sig'))
    definitions = [node for node in tree.body if isinstance(node, (ast.FunctionDef, ast.ClassDef))]
    exec(compile(ast.Module(body=definitions, type_ignores=[]), str(PROCESS_SOURCE), 'exec'), namespace)
    route_tree = ast.parse(ROUTE_SOURCE.read_text(encoding='utf-8-sig'))
    route = next(node for node in route_tree.body
                 if isinstance(node, ast.AsyncFunctionDef) and node.name == 'script_start')
    route.decorator_list = []
    namespace['HTTPException'] = HTTPException
    for name in ('ConfigNotFoundError', 'ConfigIdentityNotFoundError', 'Timeout',
                 'ConfigGenerationError', 'ConfigJsonError', 'StoreConfigJsonError',
                 'ConfigValidationError'):
        namespace[name] = type(name, (Exception,), {})
    exec(compile(ast.Module(body=[route], type_ignores=[]), str(ROUTE_SOURCE), 'exec'), namespace)
    return namespace


class StartupTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.clock = Clock()
        self.tracebacks = []
        self.logger = Mock()
        self.logger.exception.side_effect = lambda *args, **kwargs: self.tracebacks.append(sys.exc_info())
        self.ns = load_production(self.clock, self.logger)
        store = Mock()
        store.load.return_value = types.SimpleNamespace(generation=GENERATION)
        store._load_unlocked.return_value = store.load.return_value
        store.generation.identity_lifecycle_lock.side_effect = lambda _: nullcontext()
        self.p = self.ns['ScriptProcess']('Desktop', store=store, generation=GENERATION)
        self.p._spawn_attempt_nonce = NONCE
        self.p._notify_ocr_instance_stopped = Mock()
        # Scheduler projection is another task's code, and dynamically imports Config.
        self.p.schedule_snapshot = Mock(return_value={})

    def ready(self, *, nonce=NONCE, generation=GENERATION):
        return dict(status='ready', generation=generation, nonce=nonce)

    def messages(self, level='info'):
        return [call.args[0] for call in getattr(self.logger, level).call_args_list]

    async def wait(self, process=None):
        await self.p._wait_for_spawn_handshake(process or Process(), NONCE)

    def spawn_factory(self, *, ready=True):
        def construct(**kwargs):
            child = Process()

            def start():
                self.clock.now += 0.125
                if ready:
                    args = kwargs['args']
                    args[5].put(self.ready(nonce=args[6]))
            child.start.side_effect = start
            return child
        factory = Mock(side_effect=construct)
        self.ns['multiprocessing'].Process = factory
        return factory

    async def test_ready_inside_ten_seconds(self):
        self.p.ready_queue.put(self.ready())
        await self.wait()
        self.assertTrue(any('handshake_ready' in line for line in self.messages()))
        self.logger.exception.assert_not_called()

    async def test_exit_one_fails_immediately_with_context(self):
        self.clock.now = 1.0
        with self.assertRaises(self.ns['ScriptStartupProcessExitedError']) as caught:
            await self.wait(Process(False, 1))
        self.assertEqual(self.clock.now, 1.0)
        for value in ('Desktop', '12345', 'exitcode=1', GENERATION, 'elapsed=0.000s'):
            self.assertIn(value, str(caught.exception))
        self.assertEqual(caught.exception.exitcode, 1)

    async def test_exit_zero_without_ready_is_failure(self):
        with self.assertRaises(self.ns['ScriptStartupProcessExitedError']) as caught:
            await self.wait(Process(False, 0))
        self.assertEqual(caught.exception.exitcode, 0)
        self.assertLess(self.clock.now, 10)

    async def test_child_dies_after_one_second_without_waiting_deadline(self):
        child = Process()
        child.is_alive = lambda: self.clock.now < 1.0
        with self.assertRaises(self.ns['ScriptStartupProcessExitedError']):
            await self.wait(child)
        self.assertGreaterEqual(self.clock.now, 1.0)
        self.assertLess(self.clock.now, 1.02)

    async def test_ready_already_queued_wins_over_exited_child(self):
        self.p.ready_queue.put(self.ready())
        await self.wait(Process(False, 1))
        self.assertFalse(any('handshake_child_exited' in line for line in self.messages('error')))

    async def test_ready_arriving_during_death_probe_is_rechecked(self):
        child = Process(False, 0)
        # First is_alive is diagnostic wait_begin; second occurs after Empty.
        calls = 0

        def alive():
            nonlocal calls
            calls += 1
            if calls == 2:
                self.p.ready_queue.put(self.ready())
            return False
        child.is_alive = alive
        await self.wait(child)
        self.assertTrue(any('handshake_ready' in line for line in self.messages()))

    async def test_stale_generation_is_ignored_then_correct_ready_succeeds(self):
        self.p.ready_queue.put(self.ready(generation='old-generation'))
        self.p.ready_queue.put(self.ready())
        await self.wait()
        self.assertIn('ignored_generation=1', self.messages()[-1])

    async def test_nonce_compares_full_value_not_logged_prefix(self):
        self.p.ready_queue.put(self.ready(nonce=NONCE[:12] + 'b' * 20))
        self.p.ready_queue.put(self.ready())
        await self.wait()
        self.assertIn('ignored_nonce=1', self.messages()[-1])
        self.assertNotIn(NONCE, self.messages()[-1])

    async def test_real_deadline_reports_alive_pid_exitcode_elapsed_and_ignored(self):
        self.p.ready_queue.put(self.ready(nonce='stale'))
        with self.assertRaises(self.ns['ScriptStartupTimeoutError']):
            await self.wait(Process(True, None))
        self.assertGreaterEqual(self.clock.now, 10.0)
        self.assertLess(self.clock.now, 10.02)
        message = self.messages('error')[-1]
        for value in ('handshake_timeout', 'Desktop', 'pid=12345', 'is_alive=True',
                      'exitcode=None', 'elapsed=', GENERATION, 'ignored=1'):
            self.assertIn(value, message)
        self.assertEqual(len(self.messages()), 1)  # No 0.01s poll logs.

    async def test_queue_read_error_preserves_cause(self):
        error = OSError('private-path')
        self.p.ready_queue = Mock()
        self.p.ready_queue.get_nowait.side_effect = error
        with self.assertRaises(self.ns['ScriptStartupError']) as caught:
            await self.wait()
        self.assertIs(caught.exception.__cause__, error)
        self.assertIn('handshake_queue_error', self.messages('error')[-1])

    async def test_invalid_handshake_is_specific_startup_error(self):
        self.p.ready_queue.put('bad-message')
        with self.assertRaises(self.ns['ScriptStartupError']):
            await self.wait()

    async def test_explicit_child_generation_rejection_is_not_ignored(self):
        self.p.ready_queue.put(dict(status='failed', generation='other', nonce=NONCE,
                                    reason='generation_mismatch'))
        with self.assertRaises(self.ns['ConfigGenerationMismatchError']):
            await self.wait()

    async def test_probe_errors_do_not_mask_timeout(self):
        child = Process()
        child.is_alive = Mock(side_effect=OSError('probe failed'))
        with self.assertRaises(self.ns['ScriptStartupTimeoutError']):
            await self.wait(child)
        self.assertIn('status_errors=', self.messages('error')[-1])
        self.assertIn('is_alive=None', self.messages('error')[-1])

    async def test_normal_start_has_four_parent_events_and_no_exception(self):
        factory = self.spawn_factory()
        self.assertTrue(await self.p.start())
        self.assertEqual(factory.call_count, 1)
        events = [line.split()[1] for line in self.messages() if line.startswith('[START_DIAG]')]
        self.assertEqual(events, ['spawn_begin', 'spawn_return', 'handshake_wait_begin', 'handshake_ready'])
        self.assertIn("elapsed='0.125s'", self.messages()[1])
        self.logger.exception.assert_not_called()
        self.logger.error.assert_not_called()

    async def test_two_same_name_starts_create_one_worker(self):
        factory = self.spawn_factory()
        self.assertEqual(await asyncio.gather(self.p.start(), self.p.start()), [True, True])
        self.assertEqual(factory.call_count, 1)

    async def test_timeout_cleanup_confirms_exit_and_clears_handle(self):
        self.spawn_factory(ready=False)
        with self.assertRaises(self.ns['ScriptStartupTimeoutError']):
            await self.p.start()
        self.assertIsNone(self.p._process_snapshot())
        self.assertEqual(self.p.state, self.ns['ScriptState'].INACTIVE)
        self.assertIn('confirmed_exited=True', self.messages()[-1])
        self.assertIn('handle_retained=False', self.messages()[-1])

    async def test_cleanup_failure_retains_handle_and_original_startup_error(self):
        self.spawn_factory(ready=False)

        async def broken_cleanup(**kwargs):
            raise OSError('cleanup failed')
        self.p._stop_locked = broken_cleanup
        with self.assertRaises(self.ns['ScriptStartupTimeoutError']):
            await self.p.start()
        self.assertIsNotNone(self.p._process_snapshot())
        self.assertIn('handle_retained=True', self.messages('error')[-1])
        self.assertIn('confirmed_exited=False', self.messages('error')[-1])

    async def route_error(self, error):
        self.ns['mm'] = types.SimpleNamespace(start_script_process=AsyncMock(side_effect=error))
        with self.assertRaises(HTTPException) as caught:
            await self.ns['script_start']('Desktop')
        self.assertEqual(caught.exception.status_code, 500)
        self.assertIs(caught.exception.__cause__, error)
        self.logger.exception.assert_called_once()
        self.assertIs(self.tracebacks[-1][1], error)
        self.assertNotIn('private-path', caught.exception.detail)
        return caught.exception

    async def test_route_timeout_is_500_and_logs_original_traceback(self):
        response = await self.route_error(self.ns['ScriptStartupTimeoutError']('private-path'))
        self.assertIn('timed out', response.detail)

    async def test_route_child_exit_is_500_and_logs_exitcode(self):
        response = await self.route_error(
            self.ns['ScriptStartupProcessExitedError']('private-path', exitcode=1))
        self.assertIn('exitcode=1', response.detail)

    async def test_route_other_startup_error_has_safe_detail(self):
        response = await self.route_error(self.ns['ScriptStartupError']('private-path'))
        self.assertEqual(response.detail, 'Script startup failed: Desktop startup handshake failed')

    async def test_route_success_unchanged(self):
        self.ns['mm'] = types.SimpleNamespace(start_script_process=AsyncMock(return_value=True))
        self.assertIsNone(await self.ns['script_start']('Desktop'))
        self.logger.exception.assert_not_called()

    async def test_route_expected_lock_and_identity_errors_do_not_log_traceback(self):
        for name, code in (('Timeout', 503), ('ConfigGenerationMismatchError', 409)):
            self.ns['mm'] = types.SimpleNamespace(start_script_process=AsyncMock(side_effect=self.ns[name]('expected')))
            with self.assertRaises(HTTPException) as caught:
                await self.ns['script_start']('Desktop')
            self.assertEqual(caught.exception.status_code, code)
        self.logger.exception.assert_not_called()

    async def test_state_and_log_coroutines_still_broadcast_and_cancel(self):
        self.p._process = Process()
        self.p.state = self.ns['ScriptState'].RUNNING
        self.p.state_queue.put({'state': self.ns['ScriptState'].WARNING})
        self.p.log_pipe_out.poll.side_effect = [True] + [False] * 100
        self.p.log_pipe_out.recv.return_value = 'sample log'
        tasks = [asyncio.create_task(self.p.coroutine_broadcast_state()),
                 asyncio.create_task(self.p.coroutine_broadcast_log())]
        try:
            for _ in range(10):
                await asyncio.sleep(0)
                if self.p.broadcasts and self.p.logs:
                    break
            self.assertEqual(self.p.state, self.ns['ScriptState'].WARNING)
            self.assertIn('sample log', self.p.logs)
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks)
        warnings = self.messages('warning')
        self.assertTrue(any('state coroutine is cancelled' in line for line in warnings))
        self.assertTrue(any('log coroutine is cancelled' in line for line in warnings))
        self.assertFalse(any('[START_DIAG]' in line for line in self.messages()))

    async def test_timeout_default_remains_ten(self):
        self.assertEqual(self.p._spawn_handshake_timeout, 10.0)

    def child(self, Script, *, log_setup_error=None, send_error=None):
        log_module = types.ModuleType('module.logger')
        log_module.set_file_logger = Mock(side_effect=log_setup_error)
        log_module.set_func_logger = Mock()
        script_module = types.ModuleType('script')
        script_module.Script = Script
        ready_queue = Mock() if send_error else queue.Queue()
        if send_error:
            ready_queue.put.side_effect = send_error
        with patch.dict(sys.modules, {'module.logger': log_module, 'script': script_module}):
            self.ns['func']('Desktop', GENERATION, queue.Queue(), Mock(), queue.Queue(), ready_queue, NONCE)
        return ready_queue

    def successful_script(self):
        instance = types.SimpleNamespace(config=types.SimpleNamespace(generation=GENERATION), loop=Mock())
        return Mock(return_value=instance), instance

    async def test_child_stages_are_visible_and_ready_identity_is_unchanged(self):
        Script, instance = self.successful_script()
        ready = self.child(Script).get_nowait()
        self.assertEqual((ready['status'], ready['generation'], ready['nonce']), ('ready', GENERATION, NONCE))
        events = [line.split()[1] for line in self.messages()]
        self.assertEqual(events, ['child_enter', 'script_import_begin', 'script_import_done',
                                  'script_construct_done', 'config_identity_ready', 'handshake_sent'])
        self.assertIn('next_stage=\'config_load_begin\'', self.messages()[3])
        self.assertIn("delivery='queued'", self.messages()[-1])
        instance.loop.assert_called_once()
        self.logger.exception.assert_not_called()

    async def test_child_constructor_error_has_one_traceback(self):
        Script = Mock(side_effect=ValueError('construct failed'))
        with self.assertRaises(ValueError):
            self.child(Script)
        self.logger.exception.assert_called_once()
        self.assertIn('script_construct_begin', self.messages('exception')[0])

    async def test_child_script_import_error_has_one_traceback(self):
        Script, _ = self.successful_script()
        import builtins
        original_import = builtins.__import__

        def import_module(name, *args, **kwargs):
            if name == 'script':
                raise ImportError('Script import failed')
            return original_import(name, *args, **kwargs)
        with patch('builtins.__import__', side_effect=import_module):
            with self.assertRaises(ImportError):
                self.child(Script)
        Script.assert_not_called()
        self.logger.exception.assert_called_once()
        self.assertIn('script_import_begin', self.messages('exception')[0])

    async def test_child_identity_rejection_does_not_enter_loop(self):
        Script, instance = self.successful_script()
        instance.config.generation = 'replacement-generation'
        message = self.child(Script).get_nowait()
        self.assertEqual((message['status'], message['reason']), ('failed', 'generation_mismatch'))
        instance.loop.assert_not_called()

    async def test_child_config_human_takeover_gets_missing_traceback(self):
        takeover = self.ns['RequestHumanTakeover']

        class Script:
            def __init__(self, **kwargs):
                pass

            @property
            def config(self):
                try:
                    raise takeover('identity unavailable')
                except takeover:
                    raise SystemExit(1)
        with self.assertRaises(SystemExit):
            self.child(Script)
        self.logger.exception.assert_called_once()
        self.assertIn('config_load_begin', self.messages('exception')[0])

    async def test_child_log_setup_error_is_reported_once(self):
        Script, _ = self.successful_script()
        with self.assertRaises(OSError):
            self.child(Script, log_setup_error=OSError('cannot open log'))
        Script.assert_not_called()
        self.logger.exception.assert_called_once()
        self.assertIn('log_setup', self.messages('exception')[0])

    async def test_child_config_error_traceback_is_not_duplicated(self):
        logger = self.logger

        class Script:
            def __init__(self, **kwargs):
                pass

            @property
            def config(self):
                try:
                    raise ValueError('config failed')
                except Exception:
                    logger.exception('Config already logged original failure')
                    raise SystemExit(1)
        with self.assertRaises(SystemExit):
            self.child(Script)
        self.logger.exception.assert_called_once()
        self.assertTrue(any('traceback_already_logged=True' in line for line in self.messages('error')))

    async def test_child_handshake_send_failure_has_traceback(self):
        Script, instance = self.successful_script()
        self.child(Script, send_error=OSError('broken queue'))
        self.logger.exception.assert_called_once()
        self.assertIn('handshake_send_failed', self.messages('exception')[0])
        # Keep existing behavior: put failure does not change the task loop contract.
        instance.loop.assert_called_once()


if __name__ == '__main__':
    unittest.main()
