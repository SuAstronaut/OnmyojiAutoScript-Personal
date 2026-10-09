"""Offline desktop preparation / deadline / second-layer entry regressions."""
import io
import json
from contextlib import nullcontext
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace as NS
import unittest
from unittest.mock import Mock

import numpy as np
from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / 'dev_tools')]
from test_takeover_reconnect import Fixture, methods
import test_desktop_reconnect as previous
from module.exception import GameNotRunningError, GameStuckError, RequestHumanTakeover
from module.ocr.rapid_ocr import RapidOcrModel
from tasks.Restart.reconnect import ReconnectInterrupted, GAME_LOGIN_ROI

HANDLE = ROOT / 'module/device/handle.py'
WINDOW = ROOT / 'module/device/method/windows_impl.py'
LOGIN = ROOT / 'tasks/Restart/login.py'
PHOTO = ROOT / 'screenshots/desktop_capture/desktop_client_20261009_103413_530562.png'


class StagedFixture(Fixture):
    def __init__(self, cost=15):
        super().__init__(minutes=1)
        self.device.is_desktop = True
        self.device._reconnect_gate = self.gate
        self.device.root_handle_num = 90
        self.device.desktop_pid = lambda: 9
        self.events, self.cost, self.failures = [], cost, 0
        self.login.app_reconnect.side_effect = self.prepare
        self.login.app_handle_login.side_effect = self.finish
        self.login.desktop_game_login_ready.side_effect = self.ready
        self.login.reconnect_status.side_effect = lambda: ('page:main' if self.device.image.text == '庭院' else 'login')
        ns = dict(RequestHumanTakeover=RequestHumanTakeover,
                  DESKTOP_WINDOW_TITLES=('阴阳师-MuMu模拟器专版',),
                  IsWindow=lambda h: h == 90, GetWindowThreadProcessId=lambda h: (1, 9),
                  GetWindowText=lambda h: '阴阳师-MuMu模拟器专版',
                  PostMessage=self.post, WM_LBUTTONDOWN=1, WM_CHAR=2, WM_KEYDOWN=3)
        cls = methods(WINDOW, 'Window', {'_desktop_check_message', '_desktop_post_message'}, ns)
        for name in ('_desktop_check_message', '_desktop_post_message'):
            setattr(self.device, name, getattr(cls, name).__get__(self.device))

    def prepare(self, *, prepare_only):
        assert prepare_only
        self.gate.before_attempt()
        self.events.append(('prepare', self.clock.now))
        self.clock.sleep(self.cost)
        self.device.frames = ['进入游戏']

    def ready(self, *, refresh=False):
        if refresh:
            self.device.screenshot()
        return self.device.frames[0] == '进入游戏'

    def finish(self):
        if self.failures:
            self.failures -= 1
            raise GameStuckError('fake login failure')
        self.gate.guard_game_entry(self.device, (630, 595))
        self.device._desktop_post_message(90, 1, 0, 0)

    def post(self, *args):
        self.events.append(('entry', self.clock.now))
        self.device.frames = ['庭院']


class TimingTests(unittest.TestCase):
    def test_prepare_immediately_and_only_wait_remaining_time(self):
        f = StagedFixture(15)
        f.confirm()
        due = f.gate.deadline
        self.assertEqual(f.recover(), 'RealmRaid')
        self.assertLess(f.events[0][1], due)
        self.assertEqual(next(t for e, t in f.events if e == 'entry'), due)
        self.assertEqual(f.gate.attempts, 1)
        self.assertEqual(f.gate.state, 'RECOVERED')

    def test_slow_preparation_does_not_start_another_delay(self):
        f = StagedFixture(80)
        f.confirm()
        due = f.gate.deadline
        f.recover()
        entry = next(t for e, t in f.events if e == 'entry')
        self.assertGreater(entry, due)
        self.assertLess(entry, due + 30)

    def test_due_gate_blocks_even_if_old_login_handler_is_called_early(self):
        f = StagedFixture()
        f.confirm()
        f.gate.desktop_staged = True
        f.gate.state = 'RECONNECTING'
        with self.assertRaises(ReconnectInterrupted):
            f.finish()
        self.assertFalse(f.events)

    def test_waiting_blocks_all_ordinary_game_input(self):
        f = StagedFixture()
        f.confirm()
        def check():
            if f.gate.state == 'WAIT_AT_GAME_LOGIN':
                with self.assertRaises(ReconnectInterrupted):
                    f.device.click()
        f.clock.on_sleep = check
        f.recover()
        self.assertFalse(f.device.clicks)

    def test_stop_after_preparation_never_enters_game(self):
        f = StagedFixture()
        f.confirm()
        f.clock.on_sleep = lambda: f.script.stop_event.set() if f.gate.state == 'WAIT_AT_GAME_LOGIN' else None
        with self.assertRaises(RequestHumanTakeover):
            f.recover()
        self.assertEqual([e for e, _ in f.events], ['prepare'])

    def test_disable_after_preparation_never_enters_game(self):
        f = StagedFixture()
        f.confirm()
        def disable():
            if f.gate.state == 'WAIT_AT_GAME_LOGIN':
                f.config.restart.auto_reconnect_config.auto_reconnect_enable = False
        f.clock.on_sleep = disable
        with self.assertRaises(RequestHumanTakeover):
            f.recover()
        self.assertEqual([e for e, _ in f.events], ['prepare'])

    def test_second_layer_changes_before_deadline_no_blind_click(self):
        f = StagedFixture()
        f.confirm()
        f.clock.on_sleep = lambda: setattr(f.device, 'frames', ['unknown']) if f.gate.state == 'WAIT_AT_GAME_LOGIN' else None
        with self.assertRaises(RequestHumanTakeover):
            f.recover()
        self.assertEqual([e for e, _ in f.events], ['prepare'])

    def test_final_dispatch_rechecks_second_layer(self):
        f = StagedFixture()
        f.confirm()
        f.gate.desktop_staged, f.gate.state = True, 'RECONNECTING'
        f.clock.now = f.gate.deadline
        f.device.frames = ['进入游戏']
        f.gate.entry_check = lambda: False
        f.gate.guard_game_entry(f.device, (630, 595))
        with self.assertRaises(RequestHumanTakeover):
            f.device._desktop_post_message(90, 1, 0, 0)
        self.assertFalse(f.events)

    def test_first_input_cannot_select_another_account(self):
        f = StagedFixture()
        f.confirm()
        f.gate.desktop_staged, f.gate.state = True, 'RECONNECTING'
        f.clock.now = f.gate.deadline
        f.device.frames = ['进入游戏']
        with self.assertRaises(RequestHumanTakeover):
            f.gate.guard_game_entry(f.device, (1220, 245))

    def test_keyboard_cannot_bypass_verified_second_layer_entry(self):
        f = StagedFixture()
        f.confirm()
        f.gate.desktop_staged, f.gate.state = True, 'RECONNECTING'
        f.clock.now = f.gate.deadline
        f.device.frames = ['进入游戏']
        for message in (2, 3):
            with self.assertRaises(RequestHumanTakeover):
                f.device._desktop_post_message(90, message, 13, 0)
        self.assertFalse(f.events)

    def test_fresh_entry_check_rebinds_hwnd_old_gesture_is_discarded(self):
        f = StagedFixture()
        f.confirm()
        f.gate.desktop_staged, f.gate.state = True, 'RECONNECTING'
        f.clock.now = f.gate.deadline
        f.device.frames = ['进入游戏']
        f.gate.guard_game_entry(f.device, (630, 595))
        def rebind():
            f.device.root_handle_num = 91
            return True
        f.gate.entry_check = rebind
        with self.assertRaises(RequestHumanTakeover):
            f.device._desktop_post_message(90, 1, 0, 0)
        self.assertFalse(f.events)
        self.assertIsNone(f.gate.game_entry_at)

    def test_failure_has_shared_two_attempt_budget_and_no_new_deadline(self):
        f = StagedFixture()
        f.confirm()
        due = f.gate.deadline
        f.failures = 2
        with self.assertRaises(RequestHumanTakeover):
            f.recover()
        self.assertEqual(f.gate.deadline, due)
        self.assertEqual(f.gate.attempts, 2)
        self.assertEqual([e for e, _ in f.events], ['prepare', 'prepare'])

    def test_one_retry_can_recover_without_extra_wait(self):
        f = StagedFixture()
        f.confirm()
        f.failures = 1
        f.recover()
        self.assertEqual(f.gate.attempts, 2)
        self.assertEqual([e for e, _ in f.events], ['prepare', 'prepare', 'entry'])

    def test_other_instance_is_not_gated(self):
        f, other = StagedFixture(), Fixture(enabled=False)
        f.confirm()
        other.device.screenshot()
        other.device.click()
        self.assertEqual(len(other.device.clicks), 1)

    def test_forbidden_time_postpones_entry_not_preparation(self):
        f = StagedFixture()
        f.confirm()
        f.config.get_forbidden_time_end.side_effect = lambda task: 1 if f.clock.now < f.gate.deadline+10 else None
        f.recover()
        self.assertLess(f.events[0][1], f.gate.deadline)
        self.assertEqual(next(t for e, t in f.events if e == 'entry'), f.gate.deadline+10)


class PreparationTests(unittest.TestCase):
    def setUp(self):
        self.f = Fixture(minutes=1)
        self.f.confirm()
        self.gate = self.f.gate
        self.gate.desktop_staged, self.gate.state = True, 'PREPARE_LOGIN'
        self.gate.checkpoint = self.f.script._reconnect_checkpoint
        self.popup, self.messages = True, []
        ns = dict(logger=Mock(), time=NS(time=self.f.clock.time, sleep=self.f.clock.sleep),
                  RequestHumanTakeover=RequestHumanTakeover,
                  IsWindow=lambda h: h == 120 and self.popup,
                  GetClassName=lambda h: 'MPAY_LOGIN', DESKTOP_LOGIN_POPUP_CLASS='MPAY_LOGIN',
                  GetWindowThreadProcessId=lambda h: (1, 9), SendMessage=self.send,
                  WM_KEYDOWN=1, WM_KEYUP=2, VK_RETURN=13)
        cls = methods(HANDLE, 'Handle', {'desktop_confirm_login_popup', '_desktop_send_enter'}, ns)
        self.device = self.f.device
        self.device._reconnect_gate = self.gate
        self.device.desktop_pid = lambda: 9
        self.device.desktop_client_size = lambda: (1280, 720)
        self.device.find_desktop_login_popup = lambda: 120 if self.popup else 0
        for name in ('desktop_confirm_login_popup', '_desktop_send_enter'):
            setattr(self.device, name, getattr(cls, name).__get__(self.device))
        cls = methods(LOGIN, 'LoginHandler', {'desktop_prepare_login', 'desktop_game_login_ready'},
                      dict(logger=Mock(), RequestHumanTakeover=RequestHumanTakeover,
                           GameStuckError=GameStuckError, DESKTOP_MPAY_CLOSE_LIMIT=3))
        self.login = cls()
        self.login.device = self.device
        self.login.screenshot = self.device.screenshot
        self.login.I_LOGIN_8, self.login.I_CHECK_LOGIN_FORM = 'age', 'form'
        self.login.I_CHECK_MAIN, self.login.I_MAIN_BUFF = 'main', 'buff'
        self.login.appear = lambda rule: self.device.frames[0] == '进入游戏' and rule in ('age', 'form')

    def send(self, hwnd, message, key, data):
        self.messages.append((hwnd, key, self.f.clock.now))
        self.popup = False
        self.device.frames = ['进入游戏']

    def test_mpay_confirmed_before_due_and_no_game_click_in_prepare(self):
        self.login.desktop_prepare_login()
        self.assertEqual([(h, key) for h, key, _ in self.messages], [(120, 13)])
        self.assertLess(self.messages[0][2], self.gate.deadline)
        self.assertTrue(self.login.desktop_game_login_ready())
        self.assertFalse(self.device.clicks)

    def test_mpay_present_prevents_game_layer_ready_even_when_main_frame_has_button(self):
        self.device.frames = ['进入游戏']
        self.assertFalse(self.login.desktop_game_login_ready())

    def test_unknown_mpay_does_not_fall_through_to_game_input(self):
        self.device._desktop_send_enter = Mock()
        with self.assertRaises(RequestHumanTakeover):
            self.login.desktop_prepare_login()
        self.assertFalse(self.device.clicks)

    def test_stop_during_mpay_load_sends_no_enter(self):
        self.f.clock.on_sleep = self.f.script.stop_event.set
        with self.assertRaises(RequestHumanTakeover):
            self.login.desktop_prepare_login()
        self.assertFalse(self.messages)

    def test_auto_entered_role_during_preparation_stops(self):
        self.popup = False
        self.device.frames = ['庭院']
        self.login.appear = lambda rule: rule == 'main'
        with self.assertRaises(RequestHumanTakeover):
            self.login.desktop_prepare_login()
        self.assertFalse(self.messages)

    def test_mpay_target_pid_changes_no_message(self):
        self.device.desktop_pid = lambda: 99
        with self.assertRaises(RequestHumanTakeover):
            self.device._desktop_send_enter(120)
        self.assertFalse(self.messages)

    def test_second_layer_physical_client_size_mismatch_stops(self):
        self.popup = False
        self.device.frames = ['进入游戏']
        self.device.desktop_client_size = lambda: (1278, 720)
        with self.assertRaises(RequestHumanTakeover):
            self.login.desktop_game_login_ready(refresh=True)
        self.assertFalse(self.messages)

    def test_mpay_enumeration_is_instance_scoped_and_rejects_ambiguity(self):
        rows = [120, 130]
        ns = dict(EnumWindows=lambda cb, param: [cb(h, param) for h in rows],
                  GetClassName=lambda h: 'MPAY_LOGIN',
                  GetWindowThreadProcessId=lambda h: (1, 10 if h == 130 else 9),
                  DESKTOP_LOGIN_POPUP_CLASS='MPAY_LOGIN', RequestHumanTakeover=RequestHumanTakeover)
        cls = methods(HANDLE, 'Handle', {'find_desktop_login_popup'}, ns)
        obj = cls()
        obj.desktop_pid = lambda: 9
        self.assertEqual(obj.find_desktop_login_popup(), 120)
        rows.append(121)
        with self.assertRaises(RequestHumanTakeover):
            obj.find_desktop_login_popup()
        self.assertFalse(self.messages)


class StaticWaitingTests(unittest.TestCase):
    def test_long_static_second_layer_does_not_trigger_legacy_stuck_restart(self):
        f = Fixture(minutes=30)
        f.confirm()
        f.gate.desktop_staged, f.gate.state = True, 'WAIT_AT_GAME_LOGIN'
        f.clock.sleep(600)
        class Screen:
            def screenshot(self):
                self.image = f.device.image
        cls = methods(ROOT / 'module/device/device.py', 'Device', {'screenshot'},
                      dict(GameNotRunningError=GameNotRunningError,
                           RequestHumanTakeover=RequestHumanTakeover), base=Screen)
        obj = cls()
        obj._reconnect_gate = f.gate
        obj._humanizer_enabled = lambda: False
        obj.is_desktop = True
        obj.desktop_window_exists = lambda: True
        obj.handle_night_commission = lambda: False
        obj.stuck_record_check = Mock(side_effect=GameStuckError('ordinary static-frame timeout'))
        obj.screenshot()
        obj.stuck_record_check.assert_not_called()


class LauncherAndSizeTests(unittest.TestCase):
    def test_launch_exe_is_preferred_and_explicit_exe_path_resolves_root(self):
        cls = methods(HANDLE, 'Handle', {'desktop_game_exe', '_desktop_root_from_path'}, dict(Path=Path))
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'bin').mkdir()
            (root / 'bin/onmyoji.exe').touch()
            (root / 'Launch.exe').touch()
            self.assertEqual(cls.desktop_game_exe(root), str(root / 'Launch.exe'))
            self.assertEqual(cls._desktop_root_from_path(str(root / 'Launch.exe')), root.resolve())

    def test_game_pid_is_bound_when_launcher_already_exited(self):
        fixture = previous.LifecycleTests()
        fixture.setUp()
        fixture.add_window(hwnd=210, pid=21)
        fixture.processes[21].ppid.return_value = 20
        fixture.processes[21].parents = Mock(side_effect=fixture.ns['psutil'].NoSuchProcess())
        self.assertEqual(fixture.launch([previous.candidate(210, 21)])[0], 21)
        fixture.d.desktop_bind_pid.assert_called_once_with(21, 210)
        fixture.processes[21].parents.assert_not_called()

    def test_child_created_before_popen_returns_is_still_bound(self):
        fixture = previous.LifecycleTests()
        fixture.setUp()
        fixture.add_window(hwnd=210, pid=21)
        fixture.processes[21].ppid.return_value = 20
        fixture.ns['list_desktop_windows'] = Mock(side_effect=[[], [previous.candidate(210, 21)],
                                                              [previous.candidate(210, 21)]])
        def popen(*args, **kwargs):
            fixture.clock.sleep(1)
            return NS(pid=20)
        fixture.ns['subprocess'] = NS(Popen=popen)
        fixture.d.desktop_bind_pid = Mock()
        self.assertEqual(fixture.d._desktop_launch_attempt('Launch.exe', '.', 3, 1)[0], 21)
        fixture.d.desktop_bind_pid.assert_called_once_with(21, 210)

    def test_correct_size_never_calls_setwindowpos(self):
        for size in ((1280, 720), (1136, 640)):
            ns = dict(logger=Mock(), IsWindow=lambda h: True, dpi_awareness=nullcontext,
                      RequestHumanTakeover=RequestHumanTakeover, SetWindowPos=Mock())
            cls = methods(HANDLE, 'Handle', {'_desktop_try_set_size'}, ns)
            obj = cls()
            obj.root_handle_num = 90
            obj._desktop_client_size = lambda hwnd: size
            obj._reconnect_gate = NS(pending=True, desktop_staged=True)
            if size == (1280, 720):
                self.assertFalse(obj._desktop_try_set_size(1280, 720))
            else:
                with self.assertRaises(RequestHumanTakeover):
                    obj._desktop_try_set_size(1280, 720)
            ns['SetWindowPos'].assert_not_called()

    def test_other_desktop_instance_prevents_launcher_restart(self):
        cls = methods(ROOT / 'tasks/Restart/script_task.py', 'ScriptTask', {'app_reconnect'},
                      dict(RequestHumanTakeover=RequestHumanTakeover))
        obj = cls()
        obj.device = NS(is_desktop=True, desktop_pid=lambda: 9,
                        desktop_existing_candidates=lambda: [dict(pid=10)], desktop_stop_client=Mock())
        with self.assertRaises(RequestHumanTakeover):
            obj.app_reconnect(prepare_only=True)
        obj.device.desktop_stop_client.assert_not_called()


class RealPictureTests(unittest.TestCase):
    def test_portable_real_button_text_without_game_background(self):
        model = RapidOcrModel(str(ROOT / 'toolkit/ocr_models'), device='cpu', cpu_threads=4)
        crop = np.asarray(Image.open(ROOT / 'dev_tools/fixtures/reconnect/desktop_game_entry_text.png'))
        text, score = model.ocr_single_line(crop)
        self.assertEqual(text, '进入游戏')
        self.assertGreater(score, .95)
        gate = Fixture().gate
        gate.ocr = model.ocr_single_line
        canvas = np.full((720, 1280, 3), 255, dtype=np.uint8)
        x, y, w, h = GAME_LOGIN_ROI
        canvas[y:y+h, x:x+w] = crop
        self.assertTrue(gate.game_login_text(canvas))

    @unittest.skipUnless(PHOTO.exists(), 'Original private login screenshot absent')
    def test_original_latest_login_markers_and_ocr(self):
        from tasks.GameUi.assets import GameUiAssets as G
        from tasks.Restart.assets import RestartAssets as R
        raw = np.asarray(Image.open(PHOTO).convert('RGB'))
        self.assertEqual(raw.shape[:2], (720, 1280))
        self.assertTrue(R.I_LOGIN_8.match(raw))
        self.assertTrue(G.I_CHECK_LOGIN_FORM.match(raw))
        self.assertFalse(G.I_CHECK_MAIN.match(raw))
        model = RapidOcrModel(str(ROOT / 'toolkit/ocr_models'), device='cpu', cpu_threads=4)
        gate = Fixture().gate
        gate.ocr = model.ocr_single_line
        self.assertTrue(gate.game_login_text(raw))


if __name__ == '__main__':
    unittest.main()
