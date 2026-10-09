"""Offline reconnect tests: real gate and AST-loaded Script/Login methods.

No Device constructor, window, ADB, game process or RPC server is used.
"""
import ast
from datetime import datetime
from pathlib import Path
import sys
from threading import Event
from types import ModuleType, SimpleNamespace as NS
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from module.exception import GameNotRunningError, GameStuckError, GameTooManyClickError, RequestHumanTakeover
from tasks.Restart.reconnect import ReconnectGate, ReconnectInterrupted, TARGET, match_takeover



def methods(path, name, selected, namespace, base=None):
    tree = ast.parse(path.read_text(encoding='utf-8-sig'))
    source = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == name)
    body = [n for n in source.body if isinstance(n, ast.FunctionDef) and n.name in selected]
    assert len(body) == len(selected)
    namespace['FixtureBase'] = base
    cls = ast.ClassDef(name=name, bases=[ast.Name(id='FixtureBase', ctx=ast.Load())] if base else [],
                       keywords=[], body=body, decorator_list=[])
    exec(compile(ast.fix_missing_locations(ast.Module(body=[cls], type_ignores=[])), str(path), 'exec'), namespace)
    return namespace[name]


class Clock:
    def __init__(self):
        self.now = 100.0
        self.on_sleep = lambda: None

    def time(self):
        return self.now

    def sleep(self, seconds):
        self.now = round(self.now + seconds, 6)
        self.on_sleep()


class Image:
    shape = (720, 1280, 3)

    def __init__(self, text):
        self.text = text

    def __getitem__(self, crop):
        return self

    def tobytes(self):
        return self.text.encode('utf-8')


class Device:
    def __init__(self, gate, frames):
        self.gate, self.frames = gate, list(frames)
        self.image = None
        self.captures = []
        self.clicks = []
        self.stuck_record_clear = Mock()
        self.click_record_clear = Mock()

    def screenshot(self):
        if len(self.frames) > 1:
            text = self.frames.pop(0)
        else:
            text = self.frames[0]
        self.image = Image(text)
        self.captures.append(self.gate.clock())
        self.gate.observe(self)
        return self.image

    def click(self):
        self.gate.guard_input(self)
        self.clicks.append(self.gate.clock())


class Fixture:
    def __init__(self, task='RealmRaid', enabled=True, minutes=3, foster=0):
        self.clock = Clock()
        self.config = NS(restart=NS(auto_reconnect_config=NS(auto_reconnect_enable=enabled,
                            auto_reconnect_delay_minutes=minutes)),
                         kekkai_utilize=NS(utilize_config=NS(foster_reconnect_delay_minutes=foster)))
        self.config.get_forbidden_time_end = Mock(return_value=None)
        self.config.pending_task = [NS(command=task)]
        self.config.update_scheduler = Mock()
        self.gate = ReconnectGate(lambda: self.config, 'fake-instance', clock=self.clock.time,
                                  wall_clock=lambda: datetime(2026, 10, 8), sleep=self.clock.sleep,
                                  ocr=lambda crop: (crop.text, 1.0))
        self.gate.task = task
        self.device = Device(self.gate, [TARGET, TARGET])
        self.login = Mock()
        self.login.reconnect_status.return_value = 'page:main'
        self.login.app_reconnect.side_effect = self.login_success
        cls = methods(ROOT / 'script.py', 'Script', {
            '_reconnect_checkpoint', '_reconnect_login_allowed', '_reconnect_probe',
            '_wait_for_reconnect', '_wait_for_desktop_reconnect'},
                      dict(RequestHumanTakeover=RequestHumanTakeover, GameStuckError=GameStuckError,
                           GameTooManyClickError=GameTooManyClickError,
                           GameNotRunningError=GameNotRunningError, ReconnectInterrupted=ReconnectInterrupted, logger=Mock()))
        self.script = cls()
        self.script.reconnect, self.script.config, self.script.device = self.gate, self.config, self.device
        self.script.stop_event = Event()
        self.script._config_checkpoint = Mock()
        self.script._ocr_task_start, self.script._ocr_task_end = Mock(), Mock()
        self.module = ModuleType('tasks.Restart.script_task')
        self.module.ScriptTask = Mock(return_value=self.login)

    def login_success(self, **kwargs):
        self.gate.before_attempt()
        self.device.frames = ['庭院']

    def confirm(self):
        with unittest.TestCase().assertRaises(ReconnectInterrupted):
            self.device.screenshot()

    def recover(self):
        with patch.dict(sys.modules, {'tasks.Restart.script_task': self.module}):
            return self.script._wait_for_reconnect()


class GateTests(unittest.TestCase):
    def test_off_no_ocr_no_gate(self):
        f = Fixture(enabled=False)
        f.gate.ocr = Mock(side_effect=AssertionError('Disabled OCR'))
        f.device.screenshot()
        f.device.click()
        self.assertFalse(f.gate.pending)
        self.assertEqual(len(f.device.clicks), 1)

    def test_multi_daily_and_multi_tasks_excluded_including_subtasks(self):
        for top in ('MultiDailyAltAcc', 'MultiTasks'):
            with self.subTest(top=top):
                f = Fixture(task=top)
                f.config.task = NS(command='KekkaiUtilize')  # adapter changes ordinary task context
                f.gate.ocr = Mock(side_effect=AssertionError('Multi-account OCR'))
                f.device.screenshot()
                f.device.click()
                self.assertFalse(f.gate.pending)

    def test_supported_single_tasks(self):
        for task in ('KekkaiUtilize', 'MemoryScrolls', 'Exploration', 'RealmRaid', 'RyouToppa', 'Hunt'):
            with self.subTest(task=task):
                f = Fixture(task)
                f.confirm()
                self.assertEqual(f.gate.task, task)

    def test_delay_inheritance_and_override(self):
        self.assertEqual(Fixture('KekkaiUtilize', minutes=30).gate.delay_minutes(), 30)
        self.assertEqual(Fixture('KekkaiUtilize', minutes=30, foster=60).gate.delay_minutes(), 60)
        self.assertEqual(Fixture('RealmRaid', minutes=30, foster=60).gate.delay_minutes(), 30)

    def test_single_frame_does_not_trigger_and_uses_second_image(self):
        f = Fixture()
        f.device.frames = [TARGET, '庭院']
        f.device.screenshot()
        f.device.click()
        self.assertFalse(f.gate.pending)
        self.assertEqual(f.device.image.text, '庭院')
        self.assertEqual(len(f.device.clicks), 1)

    def test_throttle_and_stale_input_confirmation(self):
        f = Fixture()
        f.device.frames = ['庭院']
        ocr = f.gate.ocr = Mock(wraps=f.gate.ocr)
        f.device.screenshot()
        for _ in range(10):
            f.clock.sleep(0.1)
            f.device.screenshot()
        self.assertEqual(ocr.call_count, 1)
        f.device.frames = [TARGET, TARGET]
        f.clock.sleep(2)
        with self.assertRaises(ReconnectInterrupted):
            f.device.click()
        self.assertEqual(f.device.clicks, [])

    def test_changed_roi_in_throttled_frame_blocks_click(self):
        f = Fixture()
        f.device.frames = ['庭院']
        f.device.screenshot()
        f.clock.sleep(0.1)
        f.device.frames = [TARGET, TARGET]
        f.device.screenshot()  # normal OCR is throttled, but this frame changed
        self.assertFalse(f.gate.pending)
        with self.assertRaises(ReconnectInterrupted):
            f.device.click()
        self.assertEqual(f.device.clicks, [])

    def test_deadline_from_confirmation_and_no_input_during_wait(self):
        f = Fixture()
        f.confirm()
        self.assertAlmostEqual(f.gate.deadline, 280.3)
        f.clock.sleep(50)  # Scheduler reaches the gate later; no new 180s delay.
        for _ in range(3):
            with self.assertRaises(ReconnectInterrupted):
                f.device.click()
        f.recover()
        self.assertAlmostEqual(f.device.captures[2], 150.3)  # prepare immediately at the late checkpoint
        self.assertGreaterEqual(f.clock.now, f.gate.deadline)
        self.assertEqual(f.device.clicks, [])
        self.assertEqual(f.login.app_reconnect.call_count, 1)

    def test_stop_cancels_before_app_preparation(self):
        f = Fixture()
        f.confirm()
        f.clock.on_sleep = lambda: f.script.stop_event.set()
        with self.assertRaises(RequestHumanTakeover):
            f.recover()
        self.assertEqual(f.device.clicks, [])
        f.login.app_reconnect.assert_not_called()
        f.login.app_handle_login.assert_not_called()

    def test_disable_while_waiting_cancels(self):
        f = Fixture()
        f.confirm()
        f.clock.on_sleep = lambda: setattr(f.config.restart.auto_reconnect_config, 'auto_reconnect_enable', False)
        with self.assertRaises(RequestHumanTakeover):
            f.recover()
        f.login.app_reconnect.assert_not_called()

    def test_android_manual_role_without_verified_entry_stops(self):
        f = Fixture()
        f.confirm()
        f.device.frames = ['庭院']
        with self.assertRaisesRegex(RequestHumanTakeover, 'possible early auto-login'):
            f.recover()
        f.login.app_reconnect.assert_not_called()
        self.assertEqual(f.gate.state, 'FAILED')

    def test_popup_disappears_unknown_is_not_success(self):
        f = Fixture()
        f.confirm()
        f.device.frames = ['未知']
        f.login.reconnect_status.return_value = 'unknown'
        with self.assertRaises(RequestHumanTakeover):
            f.recover()
        f.login.app_reconnect.assert_not_called()

    def test_failure_does_not_resume_task(self):
        f = Fixture()
        f.confirm()
        f.login.app_reconnect.side_effect = RequestHumanTakeover('Login failed')
        with self.assertRaises(RequestHumanTakeover):
            f.recover()
        self.assertNotEqual(f.gate.state, 'RECOVERED')

    def test_budget_shared_across_repeated_disconnect_before_task_completion(self):
        f = Fixture()
        for _ in range(2):
            f.device.frames = [TARGET, TARGET]
            f.clock.sleep(2)
            f.confirm()
            f.recover()
        f.device.frames = [TARGET, TARGET]
        f.clock.sleep(2)
        f.confirm()
        with self.assertRaises(RequestHumanTakeover):
            f.recover()
        self.assertEqual(f.gate.attempts, 2)

    def test_instances_do_not_share_state_or_budget(self):
        first, second = Fixture(), Fixture()
        first.confirm()
        first.gate.before_attempt()
        self.assertFalse(second.gate.pending)
        self.assertEqual(second.gate.attempts, 0)
        second.device.frames = ['庭院']
        second.device.screenshot()
        second.device.click()
        self.assertEqual(len(second.device.clicks), 1)

    def test_postponed_task_is_not_forced_or_plan_overwritten(self):
        f = Fixture()
        f.confirm()
        f.config.pending_task = []
        self.assertIsNone(f.recover())
        self.assertFalse(f.gate.pending)
        self.assertFalse(hasattr(f.config, 'task_delay'))

    def test_existing_forbidden_window_defers_login(self):
        f = Fixture()
        f.confirm()
        f.config.get_forbidden_time_end.side_effect = lambda _: datetime(2026, 10, 8) if f.clock.now < 300 else None
        f.login.app_handle_login.side_effect = lambda: self.assertGreaterEqual(f.clock.now, 300)
        f.recover()
        self.assertLess(f.device.captures[2], f.gate.deadline)  # preparation is permitted before entry
        f.login.app_handle_login.assert_called_once()

    def test_ocr_failure_latches_gate(self):
        f = Fixture()
        f.gate.ocr = Mock(side_effect=RuntimeError('OCR broken'))
        with self.assertRaises(ReconnectInterrupted):
            f.device.screenshot()
        self.assertEqual(f.gate.state, 'FAILED')
        with self.assertRaises(ReconnectInterrupted):
            f.device.click()

    def test_low_confidence_and_invalid_coordinate_size_are_safe(self):
        f = Fixture()
        f.gate.ocr = lambda crop: (TARGET, .94)
        with self.assertRaises(ReconnectInterrupted):
            f.device.screenshot()
        self.assertEqual(f.gate.state, 'FAILED')
        with self.assertRaises(RequestHumanTakeover):
            f.gate.scan(NS(shape=(1080, 1920, 3)))

    def test_text_matching_is_strict(self):
        self.assertTrue(match_takeover('检测到 帐号在其他设备登錄。', .999))
        for text in ('检测到账号未在其他设备登录', TARGET+'失败', '检测到账号在本设备登录'):
            self.assertFalse(match_takeover(text, 1))
        self.assertFalse(match_takeover(TARGET, .94))


class IntegrationTests(unittest.TestCase):
    def test_real_readonly_status_checks_login_page_and_overlays(self):
        cls = methods(ROOT / 'tasks/Restart/script_task.py', 'ScriptTask', {'reconnect_status'}, {})
        obj = cls()
        obj.device = NS(is_desktop=False)
        obj.config = NS()
        obj.appear = Mock(return_value=False)
        for name in ('I_LOGIN_8','I_LOGIN_SPECIFIC_SERVE','I_CANCEL_BATTLE','I_OFF_TICKET',
                     'I_LOGIN_GET_COUPON','I_LOGIN_LOAD_DOWN','I_WATCH_VIDEO_CANCEL',
                     'I_LOGIN_RED_CLOSE','I_LOGIN_YELLOW_CLOSE','I_CLICK_REFUSE','I_LOGIN_LOGIN_GOTO_BIND_PHONE'):
            setattr(obj, name, name)
        login_page, main_page = NS(name='login',check_button='login'), NS(name='main',check_button='main')
        ui = NS(ui_pages=[login_page, main_page], _current_overlay_page=Mock(return_value=None),
                _match_page=Mock(side_effect=lambda page: page == main_page))
        game_module, page_module = ModuleType('tasks.GameUi.game_ui'), ModuleType('tasks.GameUi.page')
        game_module.GameUi, page_module.page_login = Mock(return_value=ui), login_page
        page_module.page_friends = NS(name='friends', check_button='friends')
        with patch.dict(sys.modules, {'tasks.GameUi.game_ui':game_module, 'tasks.GameUi.page':page_module}):
            self.assertEqual(obj.reconnect_status(), 'page:main')
            ui._current_overlay_page.return_value = NS(name='overlay')
            self.assertEqual(obj.reconnect_status(), 'unknown')
            ui._match_page.side_effect = lambda page: page == login_page
            self.assertEqual(obj.reconnect_status(), 'login')
        obj.device.is_desktop = True
        obj.device.find_desktop_login_popup = Mock(return_value=True)
        self.assertEqual(obj.reconnect_status(), 'login')
        # This method exposes no navigation / input APIs to the fixture.

    def test_login_failure_stops_real_loop_before_any_other_task(self):
        cls = methods(ROOT / 'script.py', 'Script', {'loop'},
                      dict(logger=Mock(), ReconnectInterrupted=ReconnectInterrupted))
        f = Fixture()
        f.confirm()
        f.login.app_reconnect.side_effect = RequestHumanTakeover('failed')
        script = cls()
        script.config_name, script.config, script.reconnect = 'fake', f.config, f.gate
        script.config.notifier = NS(push=Mock())
        script._config_checkpoint = Mock()
        script._wait_for_reconnect = f.recover
        script.get_next_task, script._execute_scheduled_task = Mock(), Mock()
        script.loop()
        script.get_next_task.assert_not_called()
        script._execute_scheduled_task.assert_not_called()
        self.assertEqual(f.gate.state, 'FAILED')

    def test_new_switch_can_reconnect_without_enabling_legacy_error_restart(self):
        class Platform:
            def app_start(self):
                self.starts += 1
            def app_stop(self):
                self.stops += 1
        from module.exception import GameNotRunningError
        cls = methods(ROOT / 'module/device/device.py', 'Device', {'app_start', 'app_stop'},
                      dict(logger=Mock(), RequestHumanTakeover=RequestHumanTakeover,
                           GameNotRunningError=GameNotRunningError), base=Platform)
        obj = cls()
        f = Fixture()
        obj._reconnect_gate, obj.is_desktop = f.gate, False
        obj.config = NS(script=NS(error=NS(handle_error=False)))
        obj.starts, obj.stops = 0, 0
        obj.stuck_record_clear = obj.click_record_clear = Mock()
        with self.assertRaises(RequestHumanTakeover):
            obj.app_start()
        f.gate.pending, f.gate.state = True, 'RECONNECTING'
        obj.app_stop()
        obj.app_start()
        self.assertEqual((obj.starts, obj.stops), (1, 1))

    def login_fixture(self, fixture):
        from module.exception import GameNotRunningError, GameTooManyClickError
        ns = dict(logger=Mock(), GameStuckError=GameStuckError,
                  GameNotRunningError=GameNotRunningError, GameTooManyClickError=GameTooManyClickError,
                  RequestHumanTakeover=RequestHumanTakeover, LoginHandoffReady=type('Handoff', (Exception,), {}))
        cls = methods(ROOT / 'tasks/Restart/login.py', 'LoginHandler', {'app_handle_login'}, ns)
        task = cls()
        task._reconnect_mode = True
        task._get_login_handoff_task = Mock(return_value='')
        task._app_handle_login = Mock(return_value=True)
        task.harvest = Mock()
        task.config = NS(restart=NS(harvest_config=NS(enable=True)))
        task.device = NS(is_desktop=False, stuck_record_clear=Mock(), click_record_clear=Mock(),
                         app_start=Mock(), app_stop=Mock(), _reconnect_gate=fixture.gate)
        return NS(task=task, ns=ns)

    def test_root_script_context_survives_normal_subtask(self):
        cls = methods(ROOT / 'script.py', 'Script', {'run'}, {})
        for top in ('MultiDailyAltAcc', 'MultiTasks', 'KekkaiUtilize'):
            with self.subTest(top=top):
                f = Fixture(top)
                script = cls()
                script.reconnect, script.config = f.gate, f.config
                script._ocr_task_depth = 0
                script.config.actual_running_task = top
                script.config.flush_account_login_times = Mock()
                script._ocr_task_start = lambda: setattr(script, '_ocr_task_depth', script._ocr_task_depth+1)
                script._ocr_task_end = lambda: setattr(script, '_ocr_task_depth', script._ocr_task_depth-1)
                contexts = []
                def body(command):
                    contexts.append(script.reconnect.task)
                    if command == top:
                        script.config.task = NS(command='RealmRaid')
                        script.run('RealmRaid')
                    return True
                script._run_task = body
                script.run(top)
                self.assertEqual(contexts, [top, top])

    def test_real_loop_gate_precedes_other_due_tasks_and_idle_actions(self):
        cls = methods(ROOT / 'script.py', 'Script', {'loop'},
                      dict(logger=Mock(), ReconnectInterrupted=ReconnectInterrupted))
        f = Fixture()
        f.confirm()
        script = cls()
        script.config_name, script.config, script.reconnect = 'fake', f.config, f.gate
        script._wait_for_reconnect = f.recover
        script.get_next_task = Mock(side_effect=AssertionError('Other due task / idle action escaped gate'))
        script._page_unknown_recoverable = False
        class Finished(BaseException):
            pass
        script._config_checkpoint = lambda phase: (_ for _ in ()).throw(Finished()) if phase == 'task_end' else None
        executed = []
        def execute(task):
            executed.append((task, f.clock.now))
            f.device.click()
            return True
        script._execute_scheduled_task = execute
        with self.assertRaises(Finished):
            script.loop()
        self.assertEqual(executed[0][0], 'RealmRaid')
        self.assertGreaterEqual(executed[0][1], f.device.captures[2]+2.5)
        script.get_next_task.assert_not_called()
        self.assertFalse(f.gate.pending)

    def test_task_abort_bypasses_subtask_exception_handlers(self):
        f = Fixture()
        consumed = False
        with self.assertRaises(ReconnectInterrupted):
            try:
                f.device.screenshot()
            except Exception:
                consumed = True
        self.assertFalse(consumed)

    def test_real_login_mumu_reconnect_budget_and_no_harvest(self):
        fixture = Fixture()
        f = self.login_fixture(fixture)
        f.task._app_handle_login = Mock(side_effect=f.ns['GameStuckError']('failed'))
        with self.assertRaises(f.ns['RequestHumanTakeover']):
            f.task.app_handle_login(start_app=True)
        self.assertEqual(f.task.device.app_start.call_count, 2)
        self.assertEqual(f.task._app_handle_login.call_count, 2)
        self.assertEqual(fixture.gate.attempts, 2)
        f.task.harvest.assert_not_called()
        f.task._app_handle_login.side_effect = None
        f.task._app_handle_login.return_value = True
        fixture.gate.attempts = 0
        self.assertTrue(f.task.app_handle_login(start_app=True))
        f.task.harvest.assert_not_called()

    def test_explicit_character_failure_stops_reconnect(self):
        fixture = Fixture()
        f = self.login_fixture(fixture)
        f.task._app_handle_login = Mock(return_value=False)
        with self.assertRaises(f.ns['RequestHumanTakeover']):
            f.task.app_handle_login(start_app=True)
        self.assertEqual(f.task._app_handle_login.call_count, 1)

    def test_indeterminate_login_result_is_not_success(self):
        f = self.login_fixture(Fixture())
        f.task._app_handle_login.return_value = None
        with self.assertRaises(RequestHumanTakeover):
            f.task.app_handle_login(start_app=True)
        f.task.harvest.assert_not_called()

    def test_real_task_catch_does_not_schedule_restart_or_report_completion(self):
        import module.exception as errors
        ns = dict(vars(errors), ReconnectInterrupted=ReconnectInterrupted, logger=Mock(), Path=Path,
                  load_module=Mock(), I18n=NS(trans_zh_cn=lambda _: 'task'), datetime=datetime)
        cls = methods(ROOT / 'script.py', 'Script', {'_run_task'}, ns)
        f = Fixture()
        script = cls()
        script.SKIP_APP_CHECK_TASKS = []
        script.device, script.reconnect, script.config = f.device, f.gate, f.config
        script.config.task_call, script.config.notifier = Mock(), NS(push=Mock())
        self.assertFalse(script._run_task('RealmRaid'))
        script.config.task_call.assert_not_called()
        script.config.notifier.push.assert_not_called()
        ns['load_module'].assert_not_called()
        # A task's finally block must not replace cancellation with TaskEnd.
        script.device.app_is_running = Mock(return_value=True)
        ns['load_module'].return_value.ScriptTask.return_value.run.side_effect = errors.TaskEnd('finally')
        self.assertFalse(script._run_task('RealmRaid'))
        script.config.notifier.push.assert_not_called()

    def test_real_desktop_default_three_reconnect_two_and_cleanup(self):
        from module.exception import GameNotRunningError, GameTooManyClickError
        cls = methods(ROOT / 'tasks/Restart/script_task.py', 'ScriptTask',
                      {'_desktop_start_and_login', '_get_login_handoff_task', 'app_reconnect'},
                      dict(logger=Mock(), DESKTOP_RESTART_ATTEMPTS=3,
                           GameNotRunningError=GameNotRunningError, GameStuckError=GameStuckError,
                           GameTooManyClickError=GameTooManyClickError, RequestHumanTakeover=RequestHumanTakeover))
        for reconnect, expected in ((False, 3), (True, 2)):
            with self.subTest(reconnect=reconnect):
                fixture = Fixture()
                obj = cls()
                obj.device = NS(is_desktop=True, app_start=Mock(), desktop_stop_client=Mock(return_value=True),
                                _reconnect_gate=fixture.gate)
                obj.app_handle_login = Mock(side_effect=GameStuckError('failed'))
                with self.assertRaises(RequestHumanTakeover):
                    if reconnect:
                        obj.app_reconnect()
                    else:
                        obj._desktop_start_and_login()
                self.assertEqual(obj.device.app_start.call_count, expected)
                self.assertEqual(obj.device.desktop_stop_client.call_count, expected + int(reconnect))
                self.assertEqual(fixture.gate.attempts, expected if reconnect else 0)

    def test_cancel_during_reconnect_blocks_input_and_next_start(self):
        f = Fixture()
        f.confirm()
        f.gate.state = 'RECONNECTING'
        f.config.restart.auto_reconnect_config.auto_reconnect_enable = False
        with self.assertRaises(ReconnectInterrupted):
            f.device.click()
        with self.assertRaises(ReconnectInterrupted):
            f.gate.before_attempt()
        self.assertEqual(f.gate.attempts, 0)

    def test_reconnect_does_not_use_login_handoff_or_modify_plans(self):
        cls = methods(ROOT / 'tasks/Restart/script_task.py', 'ScriptTask',
                      {'_get_login_handoff_task', 'app_reconnect'}, dict(logger=Mock()))
        f = Fixture()
        obj = cls()
        obj.config = NS(update_scheduler=Mock(side_effect=AssertionError('Handoff queried scheduler')))
        obj.device = NS(is_desktop=False, app_stop=Mock())
        obj.app_handle_login = Mock(return_value=True)
        obj.set_next_run = Mock()
        obj.app_reconnect()
        self.assertEqual(obj._get_login_handoff_task(), '')
        obj.app_handle_login.assert_called_once_with(start_app=True)
        obj.set_next_run.assert_not_called()

    def test_final_input_gate_covers_unchecked_click_long_swipe_drag(self):
        cls = methods(ROOT / 'module/device/control.py', 'Control', {
            '_pace_action_before', 'click', 'long_click', 'swipe', 'drag'},
            dict(logger=Mock(), time=NS(time=lambda: 0), ensure_int=lambda *args: args,
                 ensure_time=lambda x: x, point2str=lambda *args: '',
                 timing_swipe_duration_by_dist=lambda _: 1))
        obj = cls()
        obj._humanizer_enabled = lambda: False
        obj._reconnect_gate = NS(guard_input=Mock(side_effect=ReconnectInterrupted()))
        obj.handle_control_check = obj.handle_swipe_control_check = Mock()
        obj.config = NS(script=NS(device=NS(control_method='ADB'), emulator=NS(control_method='ADB')))
        obj.click_methods = {'ADB': Mock()}
        obj.click_adb = Mock()
        for call in (lambda: obj.click(1, 2, control_check=False, pace=False),
                     lambda: obj.long_click(1, 2, duration=1),
                     lambda: obj.swipe((1, 1), (20, 20), duration=1, distance_check=False),
                     lambda: obj.drag((1, 1), (20, 20))):
            with self.assertRaises(ReconnectInterrupted):
                call()
        obj.click_methods['ADB'].assert_not_called()

    def test_models_defaults_bounds_and_webui(self):
        from pydantic import ValidationError
        from tasks.Restart.config import AutoReconnectConfig
        from tasks.KekkaiUtilize.config import UtilizeConfig
        from module.config.config_model import ConfigModel
        self.assertFalse(AutoReconnectConfig().auto_reconnect_enable)
        self.assertEqual(AutoReconnectConfig().auto_reconnect_delay_minutes, 30)
        self.assertEqual(UtilizeConfig().foster_reconnect_delay_minutes, 0)
        for value in (0, -1, 1441):
            with self.assertRaises(ValidationError):
                AutoReconnectConfig(auto_reconnect_delay_minutes=value)
        for value in (-1, 1441):
            with self.assertRaises(ValidationError):
                UtilizeConfig(foster_reconnect_delay_minutes=value)
        schema = ConfigModel().script_task('Restart')
        self.assertEqual(schema['auto_reconnect_config'][1]['value'], 30)


if __name__ == '__main__':
    unittest.main()
