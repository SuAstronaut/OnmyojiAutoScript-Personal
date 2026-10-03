"""Isolated scheduler regression: real Store/locks/HTTP/process projection.

Never starts a game or imports Script's device/OCR stack. The executor methods
are compiled directly from script.py, with only the device and task body mocked.
Run: OAS_test_venv/Scripts/python.exe -B dev_tools/test_scheduler_shortcuts.py -v
"""
import ast
import asyncio
import multiprocessing
import sys
import tempfile
import types
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
# Importing the production logger otherwise creates a real log file.
logger_module = types.ModuleType('module.logger')
logger_module.logger = Mock()
sys.modules['module.logger'] = logger_module

import inflection
import httpx
from fastapi import FastAPI
from module.config.config import Config
from module.config.config_model import ConfigModel
from module.config.config_store import ConfigStore, SchedulerActionError, ConfigGenerationMismatchError
from module.config.scheduler_runtime import SchedulerRuntime
from module.exception import GameStuckError, TaskEnd
from module.server.main_manager import MainManager
from module.server.script_process import ScriptProcess, ScriptState
from module.server import script_router


def subprocess_postpone(root, generation, result_queue):
    try:
        result = ConfigStore(Path(root)).scheduler_action('test', 'Orochi', 'postpone_day', generation)
        result_queue.put(result.next_run)
    except Exception as error:
        result_queue.put(repr(error))


def subprocess_claim(root, generation, runtime, ready):
    store = ConfigStore(Path(root))
    ready.put(store.claim_scheduler_task('test', 'Orochi', generation, runtime))
    # Parent intentionally terminates us, exercising equivalent death cleanup.
    import time
    time.sleep(30)


def executor_class():
    tree = ast.parse((ROOT / 'script.py').read_text(encoding='utf-8'))
    script = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == 'Script')
    methods = [node for node in script.body if isinstance(node, ast.FunctionDef)
               and node.name in ('_execute_scheduled_task', '_publish_schedule')]
    cls = ast.ClassDef(name='Executor', bases=[], keywords=[], body=methods, decorator_list=[])
    namespace = {'SchedulerRuntime': SchedulerRuntime, 'logger': logger_module.logger, 'inflection': inflection}
    exec(compile(ast.fix_missing_locations(ast.Module(body=[cls], type_ignores=[])), str(ROOT / 'script.py'), 'exec'), namespace)
    return namespace['Executor']


class ASGIClient:
    """Use HTTPX's ASGI transport without Starlette's old Client(app=...) API."""
    def __init__(self, app):
        self.app = app

    def post(self, path):
        async def request():
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app), base_url='http://test') as client:
                return await client.post(path)
        return asyncio.run(request())


class SchedulerTests(unittest.TestCase):
    NOW = datetime(2026, 10, 3, 13, 0, 12, 999999)

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='oas_scheduler_')
        self.addCleanup(self.temp.cleanup)
        self.store = ConfigStore(Path(self.temp.name))
        raw = ConfigModel(config_name='test').model_dump(mode='json')
        for value in raw.values():
            if isinstance(value, dict) and 'scheduler' in value:
                value['scheduler']['enable'] = False
        raw['orochi']['scheduler'].update(enable=True, next_run='2026-10-03 20:01:23')
        raw['hunt']['scheduler'].update(enable=True, next_run='2026-10-04 08:00:00')
        loaded = self.store.create_from_template('test', raw)
        self.generation = loaded.generation
        self.runtime = SchedulerRuntime(self.generation)

    def action(self, action, task='Orochi', runtime=None):
        return self.store.scheduler_action('test', task, action, self.generation, runtime, now=self.NOW)

    def set_value(self, value, field='next_run', task='orochi'):
        self.store.patch_user_field('test', (task, 'scheduler', field), value)

    def next_run(self):
        return self.store.load('test').canonical['orochi']['scheduler']['next_run']

    def marker(self, task='Orochi'):
        with self.store.generation.identity_lifecycle_lock('test'):
            self.runtime.claim_locked(inflection.underscore(task))

    def process(self):
        process = ScriptProcess('test', store=self.store)
        self.addCleanup(process.log_pipe_in.close)
        self.addCleanup(process.log_pipe_out.close)
        for queue in (process.state_queue, process.config_event_queue, process.ready_queue):
            self.addCleanup(queue.close)
        return process

    def test_future_run_now_persists_seconds_only(self):
        result = self.action('run_now')
        self.assertEqual((result.status, result.next_run), ('scheduled', '2026-10-03 13:00:12'))
        self.assertEqual(ConfigStore(Path(self.temp.name)).load('test').canonical['orochi']['scheduler']['next_run'], result.next_run)

    def test_overdue_preserves_order_and_notifies_path_without_write(self):
        self.set_value('2026-10-01 08:01:23')
        with patch.object(self.store, '_write_config', wraps=self.store._write_config) as writer:
            result = self.action('run_now')
        writer.assert_not_called()
        self.assertEqual(result.status, 'already_due')
        self.assertEqual(result.next_run, '2026-10-01 08:01:23')
        self.assertEqual(result.changed_paths, [('orochi', 'scheduler', 'next_run')])

    def test_disabled_run_now_rejects_without_enabling(self):
        self.set_value(False, 'enable')
        with self.assertRaises(SchedulerActionError) as raised:
            self.action('run_now')
        self.assertEqual(raised.exception.status, 'task_disabled')
        self.assertFalse(self.store.load('test').model.orochi.scheduler.enable)

    def test_disabled_postpone_is_allowed(self):
        self.set_value(False, 'enable')
        self.assertEqual(self.action('postpone_day').next_run, '2026-10-04 20:01:23')
        self.assertFalse(self.store.load('test').model.orochi.scheduler.enable)

    def test_running_rejects_both_without_writing(self):
        self.marker()
        for action in ('run_now', 'postpone_day'):
            with self.subTest(action=action), self.assertRaises(SchedulerActionError) as raised:
                self.action(action, runtime=self.runtime)
            self.assertEqual(raised.exception.status, 'task_running')
        self.assertEqual(self.next_run(), '2026-10-03 20:01:23')

    def test_other_running_task_is_not_preempted(self):
        self.marker('Hunt')
        self.action('run_now', runtime=self.runtime)
        self.assertEqual(self.runtime.read_locked(), 'hunt')
        self.assertFalse(self.store.claim_scheduler_task('test', 'Orochi', self.generation, self.runtime, self.NOW))

    def test_repeated_run_now_does_not_add_instance_or_claim_twice(self):
        self.action('run_now', runtime=self.runtime)
        self.assertEqual(self.action('run_now', runtime=self.runtime).status, 'already_due')
        self.assertTrue(self.store.claim_scheduler_task('test', 'Orochi', self.generation, self.runtime, self.NOW))
        self.assertFalse(self.store.claim_scheduler_task('test', 'Orochi', self.generation, self.runtime, self.NOW))

    def test_postpone_literal_day_and_second_click_accumulates(self):
        self.assertEqual(self.action('postpone_day').next_run, '2026-10-04 20:01:23')
        self.assertEqual(self.action('postpone_day').next_run, '2026-10-05 20:01:23')

    def test_postpone_date_boundaries_and_overdue(self):
        for old, new in [('2026-10-31 20:01:23', '2026-11-01 20:01:23'),
                         ('2026-12-31 20:01:23', '2027-01-01 20:01:23'),
                         ('2028-02-28 20:01:23', '2028-02-29 20:01:23'),
                         ('2026-10-01 08:01:23', '2026-10-02 08:01:23')]:
            with self.subTest(old=old):
                self.set_value(old)
                self.assertEqual(self.action('postpone_day').next_run, new)

    def test_weekly_fields_not_reinterpreted(self):
        self.set_value('2026-10-05 08:01:23', task='weekly_trifles')  # Monday
        before = self.store.load('test').canonical
        self.assertEqual(self.action('postpone_day', task='WeeklyTrifles').next_run, '2026-10-06 08:01:23')
        after = self.store.load('test').canonical
        before['weekly_trifles']['scheduler']['next_run'] = after['weekly_trifles']['scheduler']['next_run']
        self.assertEqual(before, after)  # Includes the real seven-day interval.

    def test_two_threads_read_latest_and_add_two_days(self):
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: self.action('postpone_day'), range(2)))
        self.assertEqual({r.next_run for r in results}, {'2026-10-04 20:01:23', '2026-10-05 20:01:23'})

    def test_two_spawned_processes_add_two_days(self):
        ctx = multiprocessing.get_context('spawn')
        queue = ctx.Queue()
        self.addCleanup(queue.close)
        workers = [ctx.Process(target=subprocess_postpone, args=(self.temp.name, self.generation, queue)) for _ in range(2)]
        try:
            for worker in workers:
                worker.start()
            results = [queue.get(timeout=25) for _ in workers]
            for worker in workers:
                worker.join(timeout=10)
                self.assertEqual(worker.exitcode, 0)
            self.assertEqual(set(results), {'2026-10-04 20:01:23', '2026-10-05 20:01:23'})
            self.assertEqual(self.next_run(), '2026-10-05 20:01:23')
        finally:
            for worker in workers:
                if worker.is_alive():
                    worker.terminate()
                    worker.join(timeout=5)

    def test_missing_task(self):
        with self.assertRaises(SchedulerActionError) as raised:
            self.action('postpone_day', task='DoesNotExist')
        self.assertEqual(raised.exception.status, 'task_not_found')

    def test_generation_mismatch_no_write(self):
        with self.assertRaises(ConfigGenerationMismatchError):
            self.store.scheduler_action('test', 'Orochi', 'postpone_day', 'old-generation')
        self.assertEqual(self.next_run(), '2026-10-03 20:01:23')

    def test_runtime_generation_mismatch(self):
        with self.assertRaises(ConfigGenerationMismatchError):
            self.action('run_now', runtime=SchedulerRuntime('old'))

    def test_write_failure_preserves_disk(self):
        with patch.object(self.store, '_write_config', side_effect=OSError('disk unavailable')):
            with self.assertRaises(OSError):
                self.action('postpone_day')
        self.assertEqual(self.next_run(), '2026-10-03 20:01:23')

    def test_claim_rejects_disabled_and_future(self):
        self.assertFalse(self.store.claim_scheduler_task('test', 'Orochi', self.generation, self.runtime, self.NOW))
        self.set_value('2026-10-01 08:00:00')
        self.set_value(False, 'enable')
        self.assertFalse(self.store.claim_scheduler_task('test', 'Orochi', self.generation, self.runtime, self.NOW))

    def executor(self):
        executor = executor_class()()
        executor.config_name = 'test'
        executor.config = Config('test', store=self.store)
        executor.scheduler_runtime = self.runtime
        executor._config_checkpoint = Mock()
        executor.is_first_task = False
        executor._needs_recovery = False
        executor.device = Mock()
        executor.state_queue = None
        executor.run = Mock(return_value=True)
        return executor

    def test_selected_then_postponed_never_starts_device_or_task(self):
        self.set_value(str(datetime.now().replace(microsecond=0) - timedelta(minutes=5)))
        executor = self.executor()
        selected = executor.config.get_next().command
        self.assertEqual(selected, 'Orochi')
        self.action('postpone_day')
        self.assertIsNone(executor._execute_scheduled_task(selected))
        executor.run.assert_not_called()
        executor.device.stuck_record_clear.assert_not_called()
        self.assertEqual(self.runtime.read_locked(), '')

    def test_claim_wins_then_postpone_rejects(self):
        self.set_value('2026-10-01 08:00:00')
        self.assertTrue(self.store.claim_scheduler_task('test', 'Orochi', self.generation, self.runtime, self.NOW))
        with self.assertRaises(SchedulerActionError) as raised:
            self.action('postpone_day', runtime=self.runtime)
        self.assertEqual(raised.exception.status, 'task_running')

    def test_executor_cleans_marker_after_normal_and_failure_results(self):
        self.set_value('2000-01-01 00:00:00')
        for outcome in (True, False):
            with self.subTest(outcome=outcome):
                executor = self.executor()
                def run(command):
                    self.assertEqual(self.runtime.read_locked(), 'orochi')
                    self.assertEqual(executor.config.actual_running_task, 'Orochi')
                    return outcome
                executor.run.side_effect = run
                self.assertEqual(executor._execute_scheduled_task('Orochi'), outcome)
                self.assertEqual(self.runtime.read_locked(), '')
                self.assertEqual(executor.config.actual_running_task, '')

    def test_executor_cleans_marker_after_exceptions_and_stop(self):
        self.set_value('2000-01-01 00:00:00')
        for error in (TaskEnd(), GameStuckError(), RuntimeError('task failed'), SystemExit(0)):
            with self.subTest(error=type(error).__name__):
                executor = self.executor()
                executor.run.side_effect = error
                with self.assertRaises(type(error)):
                    executor._execute_scheduled_task('Orochi')
                self.assertEqual(self.runtime.read_locked(), '')

    def test_closed_state_queue_does_not_mask_task_result_or_cleanup(self):
        self.set_value('2000-01-01 00:00:00')
        executor = self.executor()
        executor.state_queue = Mock(put=Mock(side_effect=ValueError('queue closed')))
        self.assertTrue(executor._execute_scheduled_task('Orochi'))
        self.assertEqual(self.runtime.read_locked(), '')

    def test_due_head_is_pending_until_claimed(self):
        self.set_value('2000-01-01 00:00:00')
        process = self.process()
        snapshot = process.schedule_snapshot()
        self.assertEqual(snapshot['running'], {})
        self.assertIn('Orochi', [task['name'] for task in snapshot['pending']])

    def test_ws_reprojects_late_child_hint_and_direct_initial_frame(self):
        process = self.process()
        socket = Mock(send_json=AsyncMock())
        process.active_connections.append(socket)
        self.action('postpone_day')
        asyncio.run(process.broadcast_state({'schedule': {'running': {'name': 'stale'}}}))
        frame = socket.send_json.call_args.args[0]['schedule']
        self.assertEqual(frame['running'], {})
        self.assertIn({'name': 'Orochi', 'next_run': '2026-10-04 20:01:23'}, frame['waiting'])
        revision = frame['revision']
        asyncio.run(process.send_state(socket, {'schedule': {}}))
        self.assertGreater(socket.send_json.call_args.args[0]['schedule']['revision'], revision)

    def test_real_marker_projects_running_excluding_pending(self):
        self.set_value('2000-01-01 00:00:00')
        self.marker()
        process = self.process()
        process._process = Mock(is_alive=Mock(return_value=True))
        process.scheduler_runtime = self.runtime
        snapshot = process.schedule_snapshot()
        self.assertEqual(snapshot['running']['name'], 'Orochi')
        self.assertNotIn('Orochi', [task['name'] for task in snapshot['pending']])
        self.assertEqual(snapshot['generation'], self.generation)
        self.assertGreater(process.schedule_snapshot()['revision'], snapshot['revision'])

    def test_dead_child_marker_ignored_and_next_spawn_buffer_independent(self):
        self.set_value('2000-01-01 00:00:00')
        ctx = multiprocessing.get_context('spawn')
        ready = ctx.Queue()
        self.addCleanup(ready.close)
        child = ctx.Process(target=subprocess_claim, args=(self.temp.name, self.generation, self.runtime, ready))
        child.start()
        try:
            self.assertTrue(ready.get(timeout=25))
            process = self.process()
            process._process = child
            process.scheduler_runtime = self.runtime
            self.assertEqual(process.schedule_snapshot()['running']['name'], 'Orochi')
            child.terminate()
            child.join(timeout=5)
            self.assertEqual(process.schedule_snapshot()['running'], {})
            process._commit_process_if_current(child, None, ScriptState.INACTIVE, clear_state=True)
            self.assertIsNone(process.scheduler_runtime)
            new_runtime = SchedulerRuntime(self.generation)
            self.assertTrue(self.store.claim_scheduler_task('test', 'Orochi', self.generation, new_runtime))
            self.store.release_scheduler_task('test', 'Orochi', self.runtime)
            self.assertEqual(new_runtime.read_locked(), 'orochi')
        finally:
            if child.is_alive():
                child.terminate()
                child.join(timeout=5)

    def api(self):
        manager = MainManager(store=self.store)
        # Reuse a resource-owned wrapper; never start it.
        process = self.process()
        manager.script_process['test'] = process
        manager.notify_config_changed = Mock(wraps=manager.notify_config_changed)
        app = FastAPI()
        app.include_router(script_router.script_app)
        patcher = patch.object(script_router, 'mm', manager)
        patcher.start()
        self.addCleanup(patcher.stop)
        return ASGIClient(app), manager, process

    def test_api_stopped_saves_notifies_and_never_starts(self):
        client, manager, process = self.api()
        with patch.object(process, 'start') as start:
            response = client.post('/test/Orochi/scheduler/run_now')
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertTrue(body['success'])
        self.assertEqual(body['task'], 'Orochi')
        self.assertIn(body['status'], ('scheduled', 'already_due'))
        self.assertEqual(body['next_run'], self.next_run())
        self.assertEqual(body['schedule']['running'], {})
        self.assertIsNone(process._process)
        start.assert_not_called()
        manager.notify_config_changed.assert_called_once()

    def test_api_two_postpones_return_final_values(self):
        client, _, _ = self.api()
        self.assertEqual(client.post('/test/Orochi/scheduler/postpone_day').json()['next_run'], '2026-10-04 20:01:23')
        self.assertEqual(client.post('/test/Orochi/scheduler/postpone_day').json()['next_run'], '2026-10-05 20:01:23')

    def test_api_overdue_notifies_active_scheduler_without_preemption(self):
        self.set_value('2000-01-01 00:00:00')
        client, _, process = self.api()
        self.marker('Hunt')
        process.scheduler_runtime = self.runtime
        process._process = Mock(is_alive=Mock(return_value=True))
        process.state = ScriptState.RUNNING
        response = client.post('/test/Orochi/scheduler/run_now')
        self.assertEqual(response.json()['status'], 'already_due')
        event = process.config_event_queue.get(timeout=3)
        self.assertEqual(event['type'], 'config_changed')
        self.assertEqual(event['generation'], self.generation)
        self.assertIn(('orochi', 'scheduler', 'next_run'), event['changed_paths'])
        self.assertEqual(self.runtime.read_locked(), 'hunt')

    def test_api_concurrent_postpones_add_two_days(self):
        client, _, _ = self.api()
        async def requests():
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=client.app), base_url='http://test') as connection:
                return await asyncio.gather(*[connection.post('/test/Orochi/scheduler/postpone_day') for _ in range(2)])
        responses = asyncio.run(requests())
        self.assertTrue(all(response.status_code == 200 for response in responses))
        self.assertEqual({r.json()['next_run'] for r in responses}, {'2026-10-04 20:01:23', '2026-10-05 20:01:23'})
        self.assertEqual(self.next_run(), '2026-10-05 20:01:23')

    def test_api_unknown_process_state_and_missing_marker_fail_closed(self):
        client, _, process = self.api()
        for is_alive in (Mock(side_effect=OSError('unverifiable process')), Mock(return_value=True)):
            with self.subTest(is_alive=is_alive):
                process._process = Mock(is_alive=is_alive)
                response = client.post('/test/Orochi/scheduler/postpone_day')
                self.assertEqual(response.status_code, 409)
                self.assertEqual(self.next_run(), '2026-10-03 20:01:23')

    def test_api_error_codes(self):
        client, _, process = self.api()
        cases = [('/test/Missing/scheduler/run_now', 404, 'task_not_found'),
                 ('/missing/Orochi/scheduler/run_now', 404, 'configuration_changed'),
                 ('/test/Orochi/scheduler/run_now?generation=old', 409, 'configuration_changed')]
        self.set_value(False, 'enable')
        cases.append(('/test/Orochi/scheduler/run_now', 409, 'task_disabled'))
        for url, code, status in cases:
            with self.subTest(status=status):
                response = client.post(url)
                self.assertEqual(response.status_code, code)
                self.assertEqual(response.json()['detail']['status'], status)
        self.marker()
        process.scheduler_runtime = self.runtime
        process._process = Mock(is_alive=Mock(return_value=True))
        self.assertEqual(client.post('/test/Orochi/scheduler/postpone_day').json()['detail']['status'], 'task_running')

    def test_api_write_failure(self):
        client, _, _ = self.api()
        with patch.object(self.store, '_write_config', side_effect=OSError('disk unavailable')):
            response = client.post('/test/Orochi/scheduler/postpone_day')
        self.assertEqual(response.status_code, 500)
        self.assertEqual(response.json()['detail']['status'], 'write_failed')

    def test_api_refresh_failure_after_commit_still_reports_success(self):
        client, _, process = self.api()
        with patch.object(process, 'schedule_snapshot', side_effect=RuntimeError('refresh unavailable')):
            body = client.post('/test/Orochi/scheduler/postpone_day').json()
        self.assertTrue(body['success'])
        self.assertEqual(body['next_run'], self.next_run())
        self.assertEqual(body['next_run'], '2026-10-04 20:01:23')


if __name__ == '__main__':
    unittest.main()
