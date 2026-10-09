"""Offline MuMu App preparation, original deadline and final input regressions.

Real Script/Gate/Login/Device/Control methods, Fake Clock and Mock AppControl.
No Device constructor, ADB connection, emulator or game is used.
"""
import ast
from pathlib import Path
import sys
from types import SimpleNamespace as NS
import unittest
from unittest.mock import Mock

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / 'dev_tools'), str(ROOT)]
from test_takeover_reconnect import Fixture, Image, methods
from module.exception import GameNotRunningError, GameStuckError, GameTooManyClickError, RequestHumanTakeover, TaskEnd
from tasks.Restart.reconnect import ReconnectInterrupted

LOGIN = ROOT / 'tasks/Restart/login.py'
DEVICE = ROOT / 'module/device/device.py'
CONTROL = ROOT / 'module/device/control.py'
RESTART = ROOT / 'tasks/Restart/script_task.py'


class MuMuFixture(Fixture):
    def __init__(self, cost=20, task='RealmRaid', minutes=1, foster=0):
        super().__init__(task=task, minutes=minutes, foster=foster)
        self.cost, self.phase, self.started_at, self.screen_cost = cost, None, None, 0
        self.events, self.failures, self.presses_required = [], 0, 1
        self.config.script = NS(error=NS(handle_error=False), device=NS(control_method='adb'))
        f = self
        class AppControl:
            def app_stop(self):
                f.events.append(('app_stop', f.clock.now))
            def app_start(self):
                f.events.append(('app_start', f.clock.now))
                f.started_at, f.phase = f.clock.now, 'loading'
            def screenshot(self):
                if f.screen_cost and f.gate.state == 'RECONNECTING':
                    f.clock.sleep(f.screen_cost)
                if f.phase == 'loading' and f.clock.now - f.started_at >= f.cost:
                    f.phase = '进入游戏'
                text = f.phase or self.frames[0]
                if f.phase is None and len(self.frames) > 1:
                    self.frames.pop(0)
                self.image = Image(text)
                self.captures.append(f.clock.now)
        cls = methods(DEVICE, 'Device', {'app_start', 'app_stop', 'screenshot'},
                      dict(logger=Mock(), GameNotRunningError=GameNotRunningError,
                           RequestHumanTakeover=RequestHumanTakeover), base=AppControl)
        original = self.device
        self.device = cls()
        self.device.__dict__.update(original.__dict__)
        self.device._reconnect_gate, self.device.config, self.device.is_desktop = self.gate, self.config, False
        self.script.device = self.device
        self.device._humanizer_enabled = lambda: False
        self.device.handle_night_commission = Mock(return_value=False)
        self.device.stuck_record_check = Mock(return_value=False)
        self.device.get_orientation = Mock()
        self.device.handle_control_check = self.device._maybe_deliver_idle = self.device._pace_action_after = Mock()
        self.device.full_recovery = self.device._emulator_start = Mock(side_effect=AssertionError('Emulator restart'))
        cls = methods(CONTROL, 'Control', {'click', '_pace_action_before'},
                      dict(logger=Mock(), ensure_int=lambda *args: tuple(map(int, args)),
                           point2str=lambda *args: '', time=NS(time=self.clock.time)))
        self.device.click = cls.click.__get__(self.device)
        self.device._pace_action_before = cls._pace_action_before.__get__(self.device)
        self.device.click_adb = Mock(side_effect=self.native_click)
        self.device.click_methods = {'adb': self.device.click_adb}
        ns = dict(logger=Mock(), RequestHumanTakeover=RequestHumanTakeover,
                  GameStuckError=GameStuckError, GameNotRunningError=GameNotRunningError,
                  GameTooManyClickError=GameTooManyClickError,
                  LoginHandoffReady=type('UnusedHandoff', (Exception,), {}),
                  SwitchAccountAssets=NS(I_SA_ACCOUNT_LOGIN_BTN='sdk', I_SA_LOGIN_FORM_ANDROID='android'))
        cls = methods(LOGIN, 'LoginHandler', {'android_prepare_login', 'android_game_login_ready',
                                            'app_handle_login'}, ns)
        self.task = cls()
        for name in ('I_CHECK_MAIN', 'I_MAIN_BUFF', 'I_MAIN_GOTO_SHIKIGAMI_RECORDS',
                     'I_LOGIN_SCROOLL_OPEN', 'I_LOGIN_SCROOLL_CLOSE', 'I_LOGIN_COURTYARD', 'I_LOGIN_8'):
            setattr(self.task, name, name)
        self.task.device, self.task.config = self.device, self.config
        self.task.screenshot = self.device.screenshot
        self.task.appear = self.appear
        self.task._app_handle_login = Mock(side_effect=self.finish)
        self.task.harvest = self.task._get_login_handoff_task = Mock(side_effect=AssertionError('Harvest/handoff'))
        cls = methods(RESTART, 'ScriptTask', {'app_reconnect'}, dict(RequestHumanTakeover=RequestHumanTakeover))
        self.task.app_reconnect = cls.app_reconnect.__get__(self.task)
        self.task.reconnect_status = lambda: 'page:main' if self.device.image.text == '庭院' else 'login'
        self.module.ScriptTask.return_value = self.task

    def appear(self, rule, **kwargs):
        return ((rule == 'I_LOGIN_8' and self.device.image.text == '进入游戏')
                or (rule == 'I_CHECK_MAIN' and self.device.image.text == '庭院'))

    def finish(self):
        if self.failures:
            self.failures -= 1
            raise GameStuckError('Mock login failure')
        self.device.click(630, 595, control_name='LOGIN_ENTER_GAME', pace=False)
        return True

    def native_click(self, x, y):
        self.events.append(('entry', self.clock.now))
        if sum(event == 'entry' for event, _ in self.events) >= self.presses_required:
            self.phase = '庭院'

    def entries(self):
        return [t for event, t in self.events if event == 'entry']


class TimingTests(unittest.TestCase):
    def test_immediate_app_prepare_before_due_and_no_emulator_restart(self):
        f = MuMuFixture()
        f.confirm()
        due = f.gate.deadline
        self.assertEqual(f.recover(), 'RealmRaid')
        self.assertEqual([e for e, _ in f.events], ['app_stop', 'app_start', 'entry'])
        self.assertLess(f.events[1][1], due)
        self.assertEqual(f.gate.attempts, 1)
        f.device.full_recovery.assert_not_called()
        f.device._emulator_start.assert_not_called()

    def test_twenty_second_loading_only_waits_the_remaining_time(self):
        f = MuMuFixture(cost=20)
        f.confirm()
        confirmed, due = f.clock.now, f.gate.deadline
        f.recover()
        self.assertAlmostEqual(due - confirmed, 60)
        self.assertEqual(f.entries(), [round(due + .3, 6)])
        self.assertEqual(f.gate.deadline, due)
        self.assertEqual(sum(e == 'app_start' for e, _ in f.events), 1)

    def test_twenty_second_total_preparation_waits_exactly_forty_more_seconds(self):
        f = MuMuFixture(cost=0)
        f.confirm()
        confirmed, prepared = f.clock.now, []
        def prepare():
            # Initial two-frame read-only probe costs .3s; remaining preparation costs 19.7s.
            f.clock.sleep(19.7)
            f.phase = '进入游戏'
            prepared.append(f.clock.now)
        f.task.android_prepare_login = prepare
        f.recover()
        self.assertAlmostEqual(prepared[0] - confirmed, 20)
        self.assertAlmostEqual(f.entries()[0] - prepared[0], 40.3)
        self.assertEqual(f.entries(), [round(f.gate.deadline + .3, 6)])

    def test_slow_preparation_enters_when_ready_without_a_new_delay(self):
        f = MuMuFixture(cost=80)
        f.confirm()
        due = f.gate.deadline
        f.recover()
        self.assertGreater(f.entries()[0], due)
        self.assertLess(f.entries()[0], due + 22)

    def test_no_game_input_while_preparing_or_waiting(self):
        f = MuMuFixture()
        f.confirm()
        checked = set()
        def check():
            if f.gate.state in {'PREPARE_LOGIN', 'WAIT_AT_GAME_LOGIN'}:
                checked.add(f.gate.state)
                with self.assertRaises(ReconnectInterrupted):
                    f.device.click(630, 595, control_check=False, pace=False)
        f.clock.on_sleep = check
        f.recover()
        self.assertEqual(checked, {'PREPARE_LOGIN', 'WAIT_AT_GAME_LOGIN'})
        self.assertEqual(f.entries(), [round(f.gate.deadline + .3, 6)])

    def test_old_login_handler_cannot_enter_early_even_in_finish_state(self):
        f = MuMuFixture()
        f.confirm()
        f.gate.android_staged, f.gate.state = True, 'RECONNECTING'
        f.phase = '进入游戏'
        with self.assertRaises(ReconnectInterrupted):
            f.finish()
        self.assertFalse(f.entries())

    def test_stop_and_disable_while_waiting_cancel_entry(self):
        for action in ('stop', 'disable'):
            with self.subTest(action=action):
                f = MuMuFixture()
                f.confirm()
                def cancel():
                    if f.gate.state == 'WAIT_AT_GAME_LOGIN':
                        if action == 'stop':
                            f.script.stop_event.set()
                        else:
                            f.config.restart.auto_reconnect_config.auto_reconnect_enable = False
                f.clock.on_sleep = cancel
                with self.assertRaises(RequestHumanTakeover):
                    f.recover()
                self.assertFalse(f.entries())

    def test_stop_during_preparation_no_entry_or_another_start(self):
        f = MuMuFixture()
        f.confirm()
        f.clock.on_sleep = lambda: f.script.stop_event.set() if f.gate.state == 'PREPARE_LOGIN' else None
        with self.assertRaises(RequestHumanTakeover):
            f.recover()
        self.assertFalse(f.entries())
        self.assertEqual(sum(e == 'app_start' for e, _ in f.events), 1)

    def test_login_changed_while_waiting_never_clicks(self):
        f = MuMuFixture()
        f.confirm()
        f.clock.on_sleep = lambda: setattr(f, 'phase', 'unknown') if f.gate.state == 'WAIT_AT_GAME_LOGIN' else None
        with self.assertRaises(RequestHumanTakeover):
            f.recover()
        self.assertFalse(f.entries())
        self.assertGreaterEqual(f.clock.now, f.gate.deadline)
        self.assertLess(f.clock.now, f.gate.deadline + 11)
        self.assertEqual(f.gate.attempts, 1)

    def test_auto_courtyard_during_preparation_reports_early_login(self):
        f = MuMuFixture()
        f.confirm()
        f.clock.on_sleep = lambda: setattr(f, 'phase', '庭院') if f.gate.state == 'PREPARE_LOGIN' else None
        with self.assertRaisesRegex(RequestHumanTakeover, 'possible early auto-login'):
            f.recover()
        self.assertFalse(f.entries())
        self.assertNotEqual(f.gate.state, 'RECOVERED')

    def test_auto_courtyard_while_waiting_is_not_silently_accepted(self):
        f = MuMuFixture()
        f.confirm()
        f.clock.on_sleep = lambda: setattr(f, 'phase', '庭院') if f.gate.state == 'WAIT_AT_GAME_LOGIN' else None
        with self.assertRaisesRegex(RequestHumanTakeover, 'possible early auto-login'):
            f.recover()
        self.assertFalse(f.entries())

    def test_two_failures_share_prepare_and_finish_budget(self):
        f = MuMuFixture()
        f.confirm()
        due = f.gate.deadline
        f.failures = 2
        with self.assertRaises(RequestHumanTakeover):
            f.recover()
        self.assertEqual(f.gate.attempts, 2)
        self.assertEqual(sum(e == 'app_start' for e, _ in f.events), 2)
        self.assertEqual(f.gate.deadline, due)
        self.assertFalse(f.entries())

    def test_one_failed_login_can_retry_without_resetting_due(self):
        f = MuMuFixture()
        f.confirm()
        due = f.gate.deadline
        f.failures = 1
        self.assertEqual(f.recover(), 'RealmRaid')
        self.assertEqual(f.gate.attempts, 2)
        self.assertEqual(f.gate.deadline, due)
        self.assertLess(f.entries()[0], due + 22)
        f.task.harvest.assert_not_called()

    def test_repeated_disconnects_before_task_completion_keep_the_same_budget(self):
        f = MuMuFixture()
        for _ in range(2):
            f.phase = '检测到账号在其他设备登录'
            f.clock.sleep(2)
            f.confirm()
            f.recover()
        f.phase = '检测到账号在其他设备登录'
        f.clock.sleep(2)
        f.confirm()
        with self.assertRaises(RequestHumanTakeover):
            f.recover()
        self.assertEqual(f.gate.attempts, 2)
        self.assertEqual(sum(e == 'app_start' for e, _ in f.events), 2)

    def test_cancel_between_app_stop_and_start_never_starts_or_enters(self):
        f = MuMuFixture()
        f.confirm()
        original = f.device.app_stop
        def stop():
            original()
            f.script.stop_event.set()
        f.device.app_stop = stop
        with self.assertRaises(RequestHumanTakeover):
            f.recover()
        self.assertEqual([e for e, _ in f.events], ['app_stop'])

    def test_disabled_task_plan_is_not_overwritten_after_recovery(self):
        f = MuMuFixture()
        f.confirm()
        f.config.pending_task = []
        self.assertIsNone(f.recover())
        self.assertFalse(f.gate.pending)
        self.assertFalse(f.gate.android_staged)
        self.assertFalse(hasattr(f.config, 'task_delay'))

    def test_preparation_timeout_is_bounded_by_two_app_starts(self):
        f = MuMuFixture(cost=200)
        f.confirm()
        with self.assertRaises(RequestHumanTakeover):
            f.recover()
        self.assertEqual(sum(e == 'app_start' for e, _ in f.events), 2)
        self.assertEqual(f.gate.attempts, 2)
        self.assertFalse(f.entries())

    def test_forbidden_time_delays_entry_without_delaying_preparation(self):
        f = MuMuFixture()
        f.confirm()
        f.config.get_forbidden_time_end.side_effect = lambda task: 1 if f.clock.now < f.gate.deadline + 10 else None
        f.recover()
        self.assertLess(f.events[1][1], f.gate.deadline)
        self.assertEqual(f.entries(), [round(f.gate.deadline + 10.3, 6)])

    def test_instance_isolation(self):
        f, other = MuMuFixture(), Fixture(enabled=False)
        f.confirm()
        f.gate.android_staged, f.gate.state = True, 'WAIT_AT_GAME_LOGIN'
        other.device.screenshot()
        other.device.click()
        self.assertEqual(len(other.device.clicks), 1)
        self.assertFalse(other.gate.pending)

    def test_multi_account_outer_context_excludes_ordinary_subtask(self):
        for name in ('MultiDailyAltAcc', 'MultiTasks'):
            f = MuMuFixture(task=name)
            f.config.task = NS(command='RealmRaid')
            f.gate.ocr = Mock(side_effect=AssertionError('Multi-account OCR'))
            f.device.screenshot()
            f.device.click(630, 595, pace=False)
            self.assertFalse(f.gate.pending)
            self.assertFalse(f.gate.android_staged)

    def test_foster_override_inheritance_and_regular_task_delay(self):
        for task, global_minutes, foster, expected in (
                ('KekkaiUtilize', 1, 0, 1), ('KekkaiUtilize', 1, 2, 2), ('RealmRaid', 1, 2, 1)):
            with self.subTest(task=task, foster=foster):
                f = MuMuFixture(task=task, minutes=global_minutes, foster=foster)
                f.confirm()
                self.assertAlmostEqual(f.gate.deadline - f.clock.now, expected * 60)
                f.recover()
                self.assertEqual(f.entries(), [round(f.gate.deadline + .3, 6)])

    def test_off_preserves_normal_click_without_app_preparation(self):
        f = MuMuFixture()
        f.config.restart.auto_reconnect_config.auto_reconnect_enable = False
        f.gate.ocr = Mock(side_effect=AssertionError('Disabled OCR'))
        f.device.screenshot()
        f.device.click(630, 595, pace=False)
        self.assertFalse(f.gate.pending)
        self.assertEqual([e for e, _ in f.events], ['entry'])

    def test_role_before_preparation_stops_without_restart(self):
        f = MuMuFixture()
        f.confirm()
        f.phase = '庭院'
        with self.assertRaisesRegex(RequestHumanTakeover, 'possible early auto-login'):
            f.recover()
        self.assertFalse(f.events)
        self.assertEqual(f.gate.state, 'FAILED')


class InputAndLoginTests(unittest.TestCase):
    def ready_fixture(self):
        f = MuMuFixture()
        f.confirm()
        f.gate.android_staged, f.gate.state = True, 'RECONNECTING'
        f.gate.checkpoint, f.gate.login_allowed = f.script._reconnect_checkpoint, lambda: True
        f.clock.now = f.gate.deadline
        f.phase = '进入游戏'
        f.gate.entry_check = lambda: f.task.android_game_login_ready(refresh=True)
        return f

    def test_final_click_rechecks_a_fresh_frame(self):
        f = self.ready_fixture()
        calls = len(f.device.captures)
        f.device.click(630, 595, control_check=False, pace=False)
        self.assertGreater(len(f.device.captures), calls)
        self.assertEqual(f.entries(), [f.gate.deadline])

    def test_last_frame_changes_after_candidate_selection_no_backend_call(self):
        f = self.ready_fixture()
        def changed():
            f.phase = 'unknown'
            return f.task.android_game_login_ready(refresh=True)
        f.gate.entry_check = changed
        with self.assertRaises(RequestHumanTakeover):
            f.device.click(630, 595, pace=False)
        f.device.click_adb.assert_not_called()
        self.assertIsNone(f.gate.game_entry_at)

    def test_cancel_during_final_verification_no_backend_call(self):
        f = self.ready_fixture()
        def stop():
            f.script.stop_event.set()
            return True
        f.gate.entry_check = stop
        with self.assertRaises(RequestHumanTakeover):
            f.device.click(630, 595, pace=False)
        f.device.click_adb.assert_not_called()

    def test_first_click_cannot_choose_other_account_or_background_button(self):
        f = self.ready_fixture()
        with self.assertRaises(RequestHumanTakeover):
            f.device.click(1220, 245, pace=False)
        f.device.click_adb.assert_not_called()

    def test_failed_backend_does_not_record_entry_or_resume(self):
        f = self.ready_fixture()
        f.device.click_adb.side_effect = GameNotRunningError('Mock backend failure')
        with self.assertRaises(GameNotRunningError):
            f.device.click(630, 595, pace=False)
        self.assertIsNone(f.gate.game_entry_at)

    def test_unknown_or_low_confidence_login_not_ready(self):
        f = MuMuFixture()
        f.phase = 'unknown'
        self.assertFalse(f.task.android_game_login_ready(refresh=True))
        f.phase = '进入游戏'
        f.gate.ocr = lambda crop: ('进入游戏', .94)
        self.assertFalse(f.task.android_game_login_ready(refresh=True))

    def test_preparation_requires_two_consecutive_ready_frames(self):
        f = MuMuFixture()
        f.confirm()
        f.gate.android_staged, f.gate.state = True, 'PREPARE_LOGIN'
        ready = f.task.android_game_login_ready = Mock(side_effect=[True, False, True, True])
        start = f.clock.now
        f.task.android_prepare_login()
        self.assertEqual(ready.call_count, 4)
        self.assertAlmostEqual(f.clock.now - start, 1.5)
        self.assertFalse(f.entries())

    def test_entry_geometry_is_the_existing_login_ocr_rule(self):
        from tasks.Restart.assets import RestartAssets
        from tasks.Restart.reconnect import GAME_LOGIN_ROI, GAME_LOGIN_AREA
        self.assertEqual(tuple(RestartAssets.O_LOGIN_ENTER_GAME.roi), GAME_LOGIN_ROI)
        self.assertEqual(tuple(RestartAssets.O_LOGIN_ENTER_GAME.area), GAME_LOGIN_AREA)

    def test_static_login_wait_over_ten_minutes_does_not_trigger_stuck(self):
        f = MuMuFixture(minutes=30)
        f.confirm()
        f.gate.android_staged, f.gate.state = True, 'WAIT_AT_GAME_LOGIN'
        f.clock.sleep(600)
        f.phase = '进入游戏'
        f.device.stuck_record_check.side_effect = GameStuckError('Legacy static timeout')
        f.device.screenshot()
        f.device.stuck_record_check.assert_not_called()

    def test_real_original_login_loop_can_click_twice_and_confirm_courtyard(self):
        f = MuMuFixture()
        timer_tree = ast.parse((ROOT / 'module/base/timer.py').read_text(encoding='utf-8'))
        timer_class = next(n for n in timer_tree.body if isinstance(n, ast.ClassDef) and n.name == 'Timer')
        timer_ns = dict(time=NS(time=f.clock.time, sleep=f.clock.sleep))
        exec(compile(ast.Module(body=[timer_class], type_ignores=[]), 'offline_timer', 'exec'), timer_ns)
        cls = methods(LOGIN, 'LoginHandler', {'_app_handle_login', '_wait_login_transition'},
                      dict(Timer=timer_ns['Timer'], logger=Mock(), LOGIN_COURTYARD_CONFIRM_DELAY=2.5,
                           DESKTOP_MPAY_CLOSE_LIMIT=5, release_costume_probe_locks=lambda: None,
                           RequestHumanTakeover=RequestHumanTakeover, GameStuckError=GameStuckError,
                           SwitchAccountAssets=NS(I_SA_ACCOUNT_LOGIN_BTN='sdk', I_SA_LOGIN_FORM_ANDROID='android')))
        for name in ('_app_handle_login', '_wait_login_transition'):
            setattr(f.task, name, getattr(cls, name).__get__(f.task))
        tree = ast.parse(LOGIN.read_text(encoding='utf-8-sig'))
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute) and node.attr.startswith(('I_', 'O_', 'C_')):
                if isinstance(node.value, ast.Name) and node.value.id == 'self':
                    setattr(f.task, node.attr, node.attr)
        f.task._get_login_handoff_task = lambda: ''
        f.task._login_popup_blocking = lambda: False
        f.task.appear_then_click = lambda *args, **kwargs: False
        f.task.ocr_appear_click = lambda *args, **kwargs: (
            f.phase == '进入游戏' and (f.device.click(630, 595, pace=False) or True))
        f.device.stuck_record_add = Mock()
        f.presses_required, f.screen_cost = 2, .1
        f.confirm()
        self.assertEqual(f.recover(), 'RealmRaid')
        self.assertEqual(len(f.entries()), 2)
        self.assertTrue(all(t >= f.gate.deadline for t in f.entries()))
        self.assertEqual(f.gate.attempts, 1)
        self.assertEqual(f.gate.state, 'RECOVERED')

    def test_recovered_original_task_is_constructed_as_a_new_object(self):
        f = MuMuFixture()
        f.confirm()
        command = f.recover()
        original_task = Mock()
        original_task.run.side_effect = TaskEnd('done')
        module = NS(ScriptTask=Mock(return_value=original_task))
        cls = methods(ROOT / 'script.py', 'Script', {'_run_task'},
                      dict(logger=Mock(), Path=Path, load_module=Mock(return_value=module),
                           TaskEnd=TaskEnd, ReconnectInterrupted=ReconnectInterrupted))
        script = cls()
        script.reconnect, script.device, script.config = f.gate, f.device, f.config
        script.SKIP_APP_CHECK_TASKS = set()
        script._resolve_task_end_name = lambda command, error: command
        script._should_notify_task_end = lambda task: False
        f.device.app_is_running = Mock(return_value=True)
        self.assertTrue(script._run_task(command))
        module.ScriptTask.assert_called_once_with(config=f.config, device=f.device)
        original_task.run.assert_called_once_with()
        self.assertIsNot(original_task, f.task)


class AppRoutingTests(unittest.TestCase):
    def test_real_app_control_routes_only_android_package_start_stop(self):
        cls = methods(ROOT / 'module/device/app_control.py', 'AppControl', {'app_start', 'app_stop'},
                      dict(logger=Mock()))
        cls._app_u2_family = ['uiautomator2', 'minitouch', 'scrcpy']
        for method, backend in (('adb', 'adb'), ('nemu_ipc', 'adb'), ('uiautomator2', 'uiautomator2')):
            with self.subTest(method=method):
                obj = cls()
                obj.package = 'mock.game.package'
                obj.config = NS(script=NS(device=NS(screenshot_method=method)))
                for name in ('app_start_adb', 'app_stop_adb', 'app_start_uiautomator2', 'app_stop_uiautomator2'):
                    setattr(obj, name, Mock())
                obj.app_stop()
                obj.app_start()
                getattr(obj, 'app_stop_'+backend).assert_called_once_with()
                getattr(obj, 'app_start_'+backend).assert_called_once_with()


if __name__ == '__main__':
    unittest.main()
