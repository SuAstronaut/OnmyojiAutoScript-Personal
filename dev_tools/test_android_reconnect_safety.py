"""Android A+ safety: real gate/login/wait/dispatch and complete scheduler loop.

Only device backends, screenshots, clock and external task bodies are fakes.
Config/Store task claims run in a temporary directory, never in live config.
"""
import json
from datetime import datetime, timedelta
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace as NS
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / 'dev_tools'), str(ROOT)]
import test_android_login_stages as stages
from test_android_login_stages import MuMuFixture
from test_takeover_reconnect import Image, methods
import module.exception as exceptions
import tasks.Restart.reconnect as reconnect
from module.exception import RequestHumanTakeover, GamePageUnknownError, GameNotRunningError, GameStuckError, TaskEnd
from tasks.Restart.reconnect import ReconnectInterrupted, TARGET


class CropImage(Image):
    roi = None
    def __getitem__(self, crop):
        result = CropImage(self.text)
        result.roi = (crop[1].start, crop[0].start)
        return result


class SafetyTests(unittest.TestCase):
    def setUp(self):
        self.addCleanup(patch.stopall)
        patch.object(stages, 'Image', CropImage).start()
        self.log = patch.object(reconnect, 'logger', Mock()).start()

    def logs(self):
        return [json.loads(call.args[0].split('] ', 1)[1]) for call in self.log.info.call_args_list
                if call.args and call.args[0].startswith('[Android reconnect] ')]

    def fixture(self):
        f = MuMuFixture(cost=0)
        f.confirm()
        self.log.reset_mock()
        return f

    def test_true_false_true_before_due_recovers_without_early_input(self):
        f = self.fixture()
        start_due = f.gate.deadline
        seen = []
        original = f.task.android_game_login_ready
        def inspect(refresh=False):
            if f.gate.state == 'WAIT_AT_GAME_LOGIN':
                f.phase = 'unknown' if 130 <= f.clock.now < 131 else '进入游戏'
            result = original(refresh=refresh)
            if f.gate.state == 'WAIT_AT_GAME_LOGIN' and f.clock.now < start_due:
                seen.append(result)
                self.assertFalse(f.entries())
            return result
        f.task.android_game_login_ready = inspect
        self.assertEqual(f.recover(), 'RealmRaid')
        self.assertIn(False, seen)
        miss = seen.index(False)
        self.assertTrue(any(seen[:miss]) and any(seen[miss+1:]))
        self.assertEqual(f.gate.deadline, start_due)
        self.assertEqual(f.entries(), [round(start_due + .3, 6)])
        self.assertEqual(f.gate.attempts, 1)

    def test_persistent_false_waits_to_due_then_bounded_stop(self):
        f = self.fixture()
        f.clock.on_sleep = lambda: setattr(f, 'phase', 'unknown') if f.gate.state == 'WAIT_AT_GAME_LOGIN' else None
        with self.assertRaisesRegex(RequestHumanTakeover, 'bounded deadline verification'):
            f.recover()
        self.assertGreaterEqual(f.clock.now, f.gate.deadline + 10)
        self.assertLess(f.clock.now, f.gate.deadline + 10.4)
        self.assertFalse(f.entries())
        self.assertEqual(f.gate.state, 'FAILED')
        self.assertEqual(f.gate.attempts, 1)

    def test_deadline_confirmation_requires_consecutive_new_frames(self):
        f = self.fixture()
        frames, snapshots = iter(['进入游戏', 'unknown', '进入游戏', '进入游戏']), []
        original = f.task.android_game_login_ready
        def inspect(refresh=False):
            if f.gate.state == 'WAIT_AT_GAME_LOGIN' and f.clock.now >= f.gate.deadline:
                f.phase = next(frames)
                snapshots.append((f.clock.now, len(f.device.captures)))
            return original(refresh=refresh)
        f.task.android_game_login_ready = inspect
        f.recover()
        self.assertEqual(len(snapshots), 4)
        self.assertEqual(f.entries(), [round(f.gate.deadline + .9, 6)])
        self.assertEqual(len({count for _, count in snapshots}), 4)

    def test_final_dispatch_change_after_two_frames_blocks_backend(self):
        f = self.fixture()
        original = f.task._app_handle_login
        def changed():
            f.phase = 'announcement'
            return original()
        f.task._app_handle_login = changed
        with self.assertRaises(RequestHumanTakeover):
            f.recover()
        f.device.click_adb.assert_not_called()
        self.assertFalse(f.entries())
        self.assertEqual(f.gate.state, 'FAILED')
        self.assertEqual(self.logs()[-1]['entry_sent'], False)

    def test_template_loss_and_low_confidence_are_unconfirmed(self):
        f = self.fixture()
        original = f.task.appear
        f.task.appear = lambda rule: False if rule == 'I_LOGIN_8' else original(rule)
        f.phase = '进入游戏'
        self.assertFalse(f.task.android_game_login_ready(refresh=True))
        self.assertEqual(f.gate.android_login['ocr_status'], 'not_run')
        self.assertEqual(f.gate.android_login['status'], 'UNCONFIRMED')
        f.task.appear = original
        f.gate.ocr = lambda crop: (crop.text, .94 if crop.roi == (550, 567) else 1)
        self.assertFalse(f.task.android_game_login_ready(refresh=True))
        self.assertEqual(f.gate.android_login['ocr_status'], 'ok')
        self.assertEqual(f.gate.android_login['reason'], 'entry_low_confidence')
        f.gate.ocr = lambda crop: (crop.text, 1)
        self.assertTrue(f.task.android_game_login_ready(refresh=True))

    def test_nonfinite_entry_confidence_never_becomes_ready(self):
        for score in (float('nan'), float('inf'), float('-inf')):
            with self.subTest(score=score):
                f = self.fixture()
                f.phase = '进入游戏'
                f.gate.ocr = lambda crop: (crop.text, score if crop.roi == (550, 567) else 1)
                self.assertFalse(f.task.android_game_login_ready(refresh=True))
                self.assertEqual(f.gate.android_login['status'], 'UNCONFIRMED')
                self.assertIsNone(f.gate.android_login['ocr_confidence'])
                self.assertFalse(f.entries())

    def test_ocr_text_mismatch_is_distinct_from_not_executed(self):
        f = self.fixture()
        f.phase = '进入游戏'
        f.gate.ocr = lambda crop: ('加载中' if crop.roi == (550, 567) else crop.text, 1)
        self.assertFalse(f.task.android_game_login_ready(refresh=True))
        self.assertEqual(f.gate.android_login['reason'], 'entry_text_mismatch')
        self.assertEqual(f.gate.android_login['ocr_text'], '加载中')

    def test_entry_ocr_service_error_remains_exception(self):
        f = self.fixture()
        def ocr(crop):
            if crop.roi == (550, 567):
                raise RuntimeError('offline OCR service failed')
            return crop.text, 1
        f.gate.ocr = ocr
        with self.assertRaisesRegex(RequestHumanTakeover, 'OCR unavailable'):
            f.recover()
        self.assertEqual(f.gate.android_login['ocr_status'], 'error')
        self.assertEqual(f.gate.android_login['status'], 'ERROR')
        self.assertFalse(f.entries())

    def test_wrong_frame_geometry_records_no_ocr_execution(self):
        f = self.fixture()
        f.device.image = CropImage('unknown')
        f.device.image.shape = (1080, 1920, 3)
        f.gate.android_staged = True
        f.gate.ocr = Mock(side_effect=AssertionError('OCR must not run on wrong geometry'))
        with self.assertRaisesRegex(RequestHumanTakeover, 'normalized 1280x720'):
            f.task.android_game_login_ready()
        f.gate.ocr.assert_not_called()
        self.assertEqual(f.gate.android_login['ocr_status'], 'not_run')
        self.assertEqual(f.gate.android_login['takeover_ocr_status'], 'not_run')
        self.assertEqual(f.gate.android_login['status'], 'ERROR')

    def test_takeover_ocr_service_error_stops_without_entry_ocr(self):
        f = self.fixture()
        original = f.task.android_game_login_ready
        def inspect(refresh=False):
            f.gate.ocr = Mock(side_effect=RuntimeError('takeover OCR unavailable'))
            return original(refresh=refresh)
        f.task.android_game_login_ready = inspect
        with self.assertRaisesRegex(RequestHumanTakeover, 'Reconnect OCR unavailable'):
            f.recover()
        self.assertEqual(f.gate.android_login['ocr_status'], 'not_run')
        self.assertEqual(f.gate.android_login['takeover_ocr_status'], 'error')
        self.assertFalse(f.entries())

    def test_known_popup_and_sdk_block_even_when_entry_remains_visible(self):
        for marker in ('I_LOGIN_RED_CLOSE', 'sdk', 'android'):
            with self.subTest(marker=marker):
                f = self.fixture()
                f.phase = '进入游戏'
                original = f.task.appear
                if marker.startswith('I_'):
                    setattr(f.task, marker, marker)
                f.task.appear = lambda rule: rule == marker or original(rule)
                self.assertFalse(f.task.android_game_login_ready(refresh=True))
                self.assertEqual(f.gate.android_login['reason'], 'popup_or_sdk_visible')
                self.assertEqual(f.gate.android_login['ocr_status'], 'not_run')
                self.assertFalse(f.entries())

    def test_role_evidence_stops_immediately_before_due(self):
        f = self.fixture()
        f.clock.on_sleep = lambda: setattr(f, 'phase', '庭院') if f.gate.state == 'WAIT_AT_GAME_LOGIN' else None
        with self.assertRaisesRegex(RequestHumanTakeover, 'possible early auto-login'):
            f.recover()
        self.assertLess(f.clock.now, f.gate.deadline)
        self.assertEqual(f.gate.android_login['status'], 'DANGER')
        self.assertIn('I_CHECK_MAIN', f.gate.android_login['role_markers'])
        self.assertFalse(f.entries())

    def test_reconfirmed_takeover_while_waiting_never_uses_old_due(self):
        f = self.fixture()
        original_due = f.gate.deadline
        f.clock.on_sleep = lambda: setattr(f, 'phase', TARGET) if f.gate.state == 'WAIT_AT_GAME_LOGIN' else None
        with self.assertRaisesRegex(RequestHumanTakeover, 'old deadline unusable'):
            f.recover()
        self.assertLess(f.clock.now, original_due)
        self.assertEqual(f.gate.deadline, original_due)
        self.assertEqual(f.gate.android_login['status'], 'TAKEOVER')
        self.assertEqual(f.gate.state, 'FAILED')
        self.assertFalse(f.entries())

    def test_reconfirmed_takeover_during_login_stops_shared_retry(self):
        f = self.fixture()
        def login():
            f.phase = TARGET
            f.device.screenshot()
            f.device.click(630, 595, pace=False)
        f.task._app_handle_login = login
        with self.assertRaises(ReconnectInterrupted):
            f.recover()
        self.assertEqual(f.gate.state, 'FAILED')
        self.assertEqual(f.gate.attempts, 1)
        self.assertFalse(f.entries())

    def test_stop_at_final_screenshot_leaves_zero_inputs(self):
        f = self.fixture()
        original = f.task.android_game_login_ready
        def inspect(refresh=False):
            result = original(refresh=refresh)
            if f.gate.state == 'RECONNECTING':
                f.script.stop_event.set()
            return result
        f.task.android_game_login_ready = inspect
        with self.assertRaises(RequestHumanTakeover):
            f.recover()
        self.assertFalse(f.entries())
        self.assertEqual(f.gate.state, 'FAILED')

    def test_diagnostics_include_due_and_actual_input_without_frame_spam(self):
        f = self.fixture()
        due = f.gate.due_at
        f.config.restart.auto_reconnect_config.auto_reconnect_delay_minutes = 10
        f.recover()
        logs = self.logs()
        self.assertLess(len(logs), 12)
        for row in logs:
            for key in ('instance', 'task', 'stage', 'due', 'monotonic', 'deadline', 'remaining',
                        'template', 'ocr_text', 'ocr_confidence', 'ocr_status', 'role_markers',
                        'reason', 'entry_sent'):
                self.assertIn(key, row)
            self.assertEqual(row['due'], due.isoformat())
        self.assertTrue(any(row['event'] == 'deadline' for row in logs))
        sent = [row for row in logs if row['event'] == 'entry_sent']
        self.assertEqual(len(sent), 1)
        self.assertTrue(sent[0]['entry_sent'])
        self.assertTrue(all(not row['entry_sent'] for row in logs[:logs.index(sent[0])]))

    def test_low_confidence_then_ready_during_waiting_is_not_terminal(self):
        f = self.fixture()
        f.gate.ocr = lambda crop: (crop.text, .94 if crop.roi == (550, 567) and 130 <= f.clock.now < 131 else 1)
        f.recover()
        reasons = [row['reason'] for row in self.logs()]
        self.assertIn('entry_low_confidence', reasons)
        self.assertEqual(f.gate.state, 'RECOVERED')
        self.assertEqual(f.entries(), [round(f.gate.deadline + .3, 6)])

    def test_sdk_appearing_at_due_exhausts_validation_without_click(self):
        f = self.fixture()
        original = f.task.appear
        f.task.appear = lambda rule: (rule == 'sdk' and f.clock.now >= f.gate.deadline) or original(rule)
        with self.assertRaisesRegex(RequestHumanTakeover, 'bounded deadline verification'):
            f.recover()
        self.assertEqual(f.gate.android_login['reason'], 'popup_or_sdk_visible')
        self.assertFalse(f.entries())
        self.assertEqual(f.gate.attempts, 1)

    def test_cold_process_without_event_has_no_old_deadline_protection(self):
        # Explicit evidence of the documented boundary, not a safety guarantee.
        f = MuMuFixture(cost=0)
        f.phase = '进入游戏'
        self.assertIsNone(f.gate.deadline)
        self.assertFalse(f.gate.pending)
        f.device.click(630, 595, pace=False)
        self.assertEqual(f.entries(), [100.0])

    def test_verified_role_can_be_rechecked_before_task_claim_without_new_entry(self):
        f = self.fixture()
        self.assertEqual(f.recover(), 'RealmRaid')
        due, entries, attempts = f.gate.deadline, f.entries(), f.gate.attempts
        # Scheduler may defer a claim; this role came from this event's verified input.
        self.assertEqual(f.recover(), 'RealmRaid')
        self.assertEqual(f.gate.deadline, due)
        self.assertEqual(f.gate.attempts, attempts)
        self.assertEqual(f.entries(), entries)
        self.assertEqual(sum(kind == 'app_start' for kind, _ in f.events), 1)

    def test_failed_event_cannot_be_reset_by_direct_reentry(self):
        f = self.fixture()
        f.gate.state, f.gate.error = 'FAILED', 'restored task failed'
        with self.assertRaisesRegex(RequestHumanTakeover, 'restored task failed'):
            f.recover()
        self.assertEqual(f.gate.state, 'FAILED')
        self.assertFalse(f.events)
        self.assertEqual(f.gate.attempts, 0)


class SchedulerLoopTests(unittest.TestCase):
    """Real loop -> wait -> Store claim -> run -> error -> release -> next wait."""
    def fixture(self, failure=None):
        from module.config.config import Config
        from module.config.config_model import ConfigModel
        from module.config.config_store import ConfigStore
        from module.config.scheduler_runtime import SchedulerRuntime
        import inflection
        f = MuMuFixture(cost=0)
        temp = tempfile.TemporaryDirectory(prefix='oas_android_scheduler_')
        self.addCleanup(temp.cleanup)
        store = ConfigStore(Path(temp.name))
        raw = ConfigModel(config_name='test').model_dump(mode='json')
        for group in raw.values():
            if isinstance(group, dict) and 'scheduler' in group:
                group['scheduler']['enable'] = False
        raw['realm_raid']['scheduler'].update(enable=True, next_run=str(datetime.now()-timedelta(days=1)))
        raw['restart']['auto_reconnect_config'].update(auto_reconnect_enable=True, auto_reconnect_delay_minutes=1)
        raw['script']['device']['control_method'] = 'adb'
        raw['script']['error']['handle_error'] = False
        store.create_from_template('test', raw)
        f.config = Config('test', store=store)
        f.config.update_scheduler()
        f.config.notifier = NS(push=Mock())
        f.device.config = f.task.config = f.config
        f.config.store.claim_scheduler_task = Mock(wraps=store.claim_scheduler_task)
        f.config.store.release_scheduler_task = Mock(wraps=store.release_scheduler_task)
        calls = []
        class ExternalTask:
            def __init__(self, config, device):
                calls.append(('construct', f.clock.now))
            def run(self):
                calls.append(('run', f.clock.now))
                if failure:
                    raise failure('offline restored task failure')
                raise TaskEnd('done')
        namespace = dict(vars(exceptions), logger=Mock(), Path=Path,
                         load_module=lambda *args: NS(ScriptTask=ExternalTask),
                         ReconnectInterrupted=ReconnectInterrupted,
                         SchedulerRuntime=SchedulerRuntime, inflection=inflection, datetime=datetime)
        cls = methods(ROOT/'script.py', 'Script',
                      {'loop', 'run', '_run_task', '_execute_scheduled_task', '_publish_schedule'},
                      namespace, base=type(f.script))
        script = cls()
        script.__dict__.update(f.script.__dict__)
        f.script = script
        script.config, script.config_name = f.config, 'test'
        script.scheduler_runtime, script.is_first_task, script._needs_recovery = None, False, False
        script._page_unknown_recoverable, script._ocr_task_depth, script.state_queue = False, 0, None
        script.SKIP_APP_CHECK_TASKS = set()
        loop_turns = []
        def bounded_checkpoint(phase):
            if phase == 'before_task':
                loop_turns.append(phase)
                if len(loop_turns) > 4:
                    raise AssertionError('Offline scheduler exceeded four turns; terminal state may have been overwritten')
        script._config_checkpoint = bounded_checkpoint
        script._resolve_task_end_name = lambda command, error: command
        script._should_notify_task_end = lambda task: False
        script.get_next_task = Mock(side_effect=AssertionError('unrelated task escaped gate'))
        f.device.app_is_running = Mock(return_value=True)
        f.confirm()
        return f, calls

    def run_loop(self, f):
        with patch.dict(sys.modules, {'tasks.Restart.script_task': f.module}):
            f.script.loop()

    def test_restored_task_failures_end_complete_loop_without_second_claim(self):
        for failure in (GamePageUnknownError, GameStuckError, GameNotRunningError):
            with self.subTest(failure=failure.__name__):
                f, calls = self.fixture(failure)
                self.run_loop(f)
                self.assertEqual([kind for kind, _ in calls], ['construct', 'run'])
                self.assertEqual(f.gate.state, 'FAILED')
                self.assertTrue(f.gate.pending)
                self.assertEqual(f.gate.attempts, 1)
                self.assertEqual(f.config.store.claim_scheduler_task.call_count, 1)
                self.assertEqual(f.config.store.release_scheduler_task.call_count, 1)
                self.assertEqual(f.config.actual_running_task, '')
                f.script.get_next_task.assert_not_called()
                self.assertEqual(f.config.notifier.push.call_count, 1)
                self.assertLess(f.clock.now, f.gate.deadline + 5)

    def test_success_completes_event_and_releases_claim(self):
        f, calls = self.fixture()
        class Finished(BaseException): pass
        def checkpoint(phase):
            if phase == 'task_end':
                raise Finished()
        f.script._config_checkpoint = checkpoint
        with self.assertRaises(Finished):
            self.run_loop(f)
        self.assertEqual([kind for kind, _ in calls], ['construct', 'run'])
        self.assertFalse(f.gate.pending)
        self.assertEqual(f.gate.state, 'NORMAL')
        self.assertEqual(f.config.store.release_scheduler_task.call_count, 1)
        self.assertEqual(f.config.actual_running_task, '')

    def test_user_stop_prevents_claim_and_automatic_recovery(self):
        f, calls = self.fixture()
        f.clock.on_sleep = lambda: f.script.stop_event.set() if f.gate.state == 'WAIT_AT_GAME_LOGIN' else None
        self.run_loop(f)
        self.assertFalse(calls)
        self.assertFalse(f.entries())
        self.assertEqual(f.gate.state, 'FAILED')
        f.config.store.claim_scheduler_task.assert_not_called()
        self.run_loop(f)
        self.assertFalse(calls)
        f.config.store.claim_scheduler_task.assert_not_called()

    def test_exhausted_login_budget_stops_loop_before_claim(self):
        f, calls = self.fixture()
        f.gate.attempts = f.gate.MAX_ATTEMPTS
        self.run_loop(f)
        self.assertEqual(f.gate.state, 'FAILED')
        self.assertFalse(calls)
        self.assertFalse(f.entries())
        self.assertEqual(f.gate.attempts, f.gate.MAX_ATTEMPTS)


if __name__ == '__main__':
    unittest.main()
