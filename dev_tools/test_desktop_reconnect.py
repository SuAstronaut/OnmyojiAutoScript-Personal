"""Offline real-picture OCR and PID/HWND lifecycle regressions.

Run: toolkit/python.exe -B dev_tools/test_desktop_reconnect.py -v
No Device constructor, live window enumeration, input, process or RPC is used.
"""
import ast
import hashlib
import io
import json
import sys
from contextlib import nullcontext
from pathlib import Path
from types import ModuleType, SimpleNamespace as NS
import unittest
from unittest.mock import Mock, patch

import cv2
import numpy as np
from PIL import Image, ImageDraw, ImageEnhance, ImageFont

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / 'dev_tools')]
from test_takeover_reconnect import Fixture, Clock, methods
import test_desktop_attach as attach
from test_desktop_attach import HANDLE, WINDOW, candidate
from module.exception import GameNotRunningError, RequestHumanTakeover
from module.ocr.rapid_ocr import RapidOcrModel
from tasks.Restart.reconnect import ROI, TARGET, ReconnectInterrupted, match_takeover

FIXTURES = ROOT / 'dev_tools/fixtures/reconnect'
DESKTOP = ROOT / 'screenshots/desktop_capture/desktop_client_20261009_082509_393779.png'
DESKTOP_SMALL = ROOT / 'screenshots/desktop_capture/desktop_client_20261009_082151_599056.png'


def normalized(raw):
    # Explicitly reproduce the existing runtime BitBlt fallback, not a new fix.
    return raw if raw.shape[:2] == (720, 1280) else cv2.resize(raw, (1280, 720))


class PictureTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.model = RapidOcrModel(str(ROOT / 'toolkit/ocr_models'), device='cpu', cpu_threads=4)
        cls.desktop_text = np.asarray(Image.open(FIXTURES / 'desktop_takeover_text.png').convert('RGB'))
        cls.mumu_text = np.asarray(Image.open(FIXTURES / 'mumu_takeover_text.png').convert('RGB'))
        cls.desktop_1280_text = np.asarray(Image.open(FIXTURES / 'desktop_1280_takeover_text.png').convert('RGB'))

    def assert_hit(self, crop):
        text, score = self.model.ocr_single_line(crop)
        self.assertEqual(text, TARGET)
        self.assertGreaterEqual(score, .95)

    def test_lossless_desktop_and_mumu_text_only(self):
        for crop in (self.desktop_text, self.desktop_1280_text, self.mumu_text):
            with self.subTest(shape=crop.shape):
                self.assert_hit(crop)

    def test_runtime_common_roi_without_game_background(self):
        canvas = np.full((640, 1136, 3), 255, dtype=np.uint8)
        canvas[265:315, 410:725] = self.desktop_text
        x, y, w, h = ROI
        self.assert_hit(normalized(canvas)[y:y+h, x:x+w])
        self.assert_hit(self.mumu_text)

    @unittest.skipUnless(DESKTOP.exists(), 'Original private screenshot not present; portable text fixtures still tested')
    def test_original_stacked_picture_runtime_roi_and_raw_dimensions(self):
        raw = np.asarray(Image.open(DESKTOP).convert('RGB'))
        self.assertEqual(raw.shape[:2], (720, 1280))
        metadata = json.loads((FIXTURES / 'provenance.json').read_text(encoding='utf-8'))
        self.assertEqual(hashlib.sha256(DESKTOP.read_bytes()).hexdigest(), metadata['desktop_1280']['source_sha256'])
        x, y, w, h = ROI
        self.assert_hit(raw[y:y+h, x:x+w])
        # Background removal confirmation is outside the takeover text ROI.
        self.assertFalse(match_takeover(*self.model.ocr_single_line(raw[105:175, 430:840])))

    @unittest.skipUnless(DESKTOP_SMALL.exists(), 'Earlier non-target-size diagnostic PNG not present')
    def test_small_original_requires_existing_backend_coordinate_normalization(self):
        raw = np.asarray(Image.open(DESKTOP_SMALL).convert('RGB'))
        self.assertEqual(raw.shape[:2], (640, 1136))
        x, y, w, h = ROI
        self.assertFalse(match_takeover(*self.model.ocr_single_line(raw[y:y+h, x:x+w])))
        self.assert_hit(normalized(raw)[y:y+h, x:x+w])

    def test_brightness_and_jpeg(self):
        for platform, crop in [('desktop', self.desktop_text), ('desktop_1280', self.desktop_1280_text),
                               ('mumu', self.mumu_text)]:
            for factor in (.7, 1.25):
                with self.subTest(platform=platform, brightness=factor):
                    self.assert_hit(np.asarray(ImageEnhance.Brightness(Image.fromarray(crop)).enhance(factor)))
            stream = io.BytesIO()
            Image.fromarray(crop).save(stream, 'JPEG', quality=55)
            stream.seek(0)
            with self.subTest(platform=platform, jpeg=55):
                self.assert_hit(np.asarray(Image.open(stream)))

    def test_similar_text_and_regular_scenes_are_not_takeover(self):
        font = ImageFont.truetype('C:/Windows/Fonts/msyh.ttc', 19)
        for text in ('是否从好友列表中移除以下玩家？', '检测到账号在本设备登录',
                     '检测到账号未在其他设备登录', TARGET + '失败', '网络连接失败', '确定'):
            canvas = Image.new('RGB', (360, 65), (205, 184, 155))
            ImageDraw.Draw(canvas).text((8, 14), text, font=font, fill=(39, 33, 24))
            with self.subTest(text=text):
                self.assertFalse(match_takeover(*self.model.ocr_single_line(np.asarray(canvas))))
        for name in ('summon_rest_real.png', 'summon_map_real.png', 'two_attribute_full_scene_real.png'):
            im = np.asarray(Image.open(ROOT / 'dev_tools/fixtures/sixrealms' / name).convert('RGB'))
            x, y, w, h = ROI
            with self.subTest(scene=name):
                self.assertFalse(match_takeover(*self.model.ocr_single_line(im[y:y+h, x:x+w])))

    def test_real_text_two_frames_and_waiting_blocks_both_dialog_layers(self):
        scene = np.full((720, 1280, 3), 255, dtype=np.uint8)
        x, y, w, h = ROI
        scene[y:y+h, x:x+w] = self.mumu_text
        scenes = [scene]
        if DESKTOP.exists():
            scenes.append(normalized(np.asarray(Image.open(DESKTOP).convert('RGB'))))
        for scene in scenes:
            f = Fixture()
            f.gate.ocr = self.model.ocr_single_line
            f.device.image = scene
            def screenshot():
                f.device.image = scene.copy()
                f.gate.observe(f.device)
            f.device.screenshot = screenshot
            with self.assertRaises(ReconnectInterrupted):
                screenshot()
            self.assertTrue(f.gate.pending)
            self.assertEqual(f.gate.state, 'WAITING')
            for _ in range(3):
                with self.assertRaises(ReconnectInterrupted):
                    f.device.click()
            self.assertEqual(f.device.clicks, [])

    @unittest.skipUnless(DESKTOP.exists(), 'Original private screenshot not present')
    def test_real_friend_marker_matches_through_overlay_but_cannot_report_recovered(self):
        from tasks.GameUi.assets import GameUiAssets as G
        raw = np.asarray(Image.open(DESKTOP).convert('RGB'))
        self.assertTrue(G.I_CHECK_FRIENDS.match(raw))
        cls = methods(ROOT / 'tasks/Restart/script_task.py', 'ScriptTask', {'reconnect_status'}, {})
        task = cls()
        task.config = NS()
        task.device = NS(is_desktop=False)
        task.appear = Mock(return_value=False)  # no login markers in this frame
        for name in ('I_LOGIN_8', 'I_LOGIN_SPECIFIC_SERVE', 'I_CANCEL_BATTLE', 'I_OFF_TICKET',
                     'I_LOGIN_GET_COUPON', 'I_LOGIN_LOAD_DOWN', 'I_WATCH_VIDEO_CANCEL',
                     'I_LOGIN_RED_CLOSE', 'I_LOGIN_YELLOW_CLOSE', 'I_CLICK_REFUSE',
                     'I_LOGIN_LOGIN_GOTO_BIND_PHONE'):
            setattr(task, name, name)
        friends, main, login = NS(name='friends'), NS(name='main'), NS(name='login')
        ui = NS(_match_page=lambda page: page is friends and G.I_CHECK_FRIENDS.match(raw),
                _current_overlay_page=Mock(return_value=None), ui_pages=[friends, main])
        game_module, page_module = ModuleType('tasks.GameUi.game_ui'), ModuleType('tasks.GameUi.page')
        game_module.GameUi = Mock(return_value=ui)
        page_module.page_login, page_module.page_friends = login, friends
        with patch.dict(sys.modules, {'tasks.GameUi.game_ui': game_module, 'tasks.GameUi.page': page_module}):
            self.assertEqual(task.reconnect_status(), 'unknown')


class ClosedClientTests(unittest.TestCase):
    def test_closed_desktop_prepares_before_deadline_and_finishes_at_deadline(self):
        f = Fixture()
        f.confirm()
        f.device.is_desktop = True
        original = f.device.screenshot
        f.device.screenshot = Mock(side_effect=GameNotRunningError('closed during wait'))
        def reconnect(**kwargs):
            self.assertLess(f.clock.now, f.gate.deadline)
            f.login_success()
            f.device.screenshot = original
        f.login.app_reconnect.side_effect = reconnect
        f.login.app_handle_login.side_effect = lambda: self.assertGreaterEqual(f.clock.now, f.gate.deadline)
        self.assertEqual(f.recover(), 'RealmRaid')
        f.login.app_reconnect.assert_called_once()
        self.assertEqual(f.device.clicks, [])

    def test_closed_client_stop_cancels_before_any_launch(self):
        f = Fixture()
        f.confirm()
        f.device.is_desktop = True
        f.device.screenshot = Mock(side_effect=GameNotRunningError())
        f.script.stop_event.set()
        with self.assertRaises(RequestHumanTakeover):
            f.recover()
        f.login.app_reconnect.assert_not_called()

    def test_missing_again_after_login_stops(self):
        f = Fixture()
        f.confirm()
        f.device.is_desktop = True
        f.device.screenshot = Mock(side_effect=GameNotRunningError())
        f.login.app_reconnect.side_effect = lambda **kwargs: f.gate.before_attempt()
        with self.assertRaises(RequestHumanTakeover):
            f.recover()
        self.assertEqual(f.login.app_reconnect.call_count, 2)

    def test_mumu_missing_screenshot_stops_after_two_app_attempts(self):
        f = Fixture()
        f.confirm()
        f.device.is_desktop = False
        f.device.screenshot = Mock(side_effect=GameNotRunningError())
        with self.assertRaises(RequestHumanTakeover):
            f.recover()
        self.assertEqual(f.login.app_reconnect.call_count, 2)
        self.assertEqual(f.gate.attempts, 2)
        self.assertEqual(f.device.clicks, [])

    def test_foreground_disappears_but_background_overlay_does_not_resume(self):
        f = Fixture()
        f.confirm()
        f.device.frames = ['是否从好友列表中移除以下玩家？']
        f.login.reconnect_status.return_value = 'unknown'
        with self.assertRaises(RequestHumanTakeover):
            f.recover()
        f.login.app_reconnect.assert_not_called()
        self.assertEqual(f.device.clicks, [])


class LifecycleTests(unittest.TestCase):
    def add_window(self, *args, **kwargs):
        attach.DesktopAttachTests.add_window(self, *args, **kwargs)
        for process in self.processes.values():
            if not hasattr(process, 'ppid'):
                process.ppid = Mock(return_value=0)
                process.create_time = Mock(return_value=getattr(getattr(self, 'clock', None), 'now', 100))

    def setUp(self):
        attach.DesktopAttachTests.setUp(self)
        # Production exceptions are distinct; the older attach-only fixture
        # aliases both because it never exercises this propagation boundary.
        self.ns['EmulatorNotRunningError'] = type('MissingWindow', (Exception,), {})
        cls = methods(HANDLE, 'Handle', {
            'find_desktop_window_by_pid', 'desktop_window_exists', '_desktop_rebind_window',
            '_desktop_launch_attempt', 'desktop_force_kill', '_desktop_process_can_stop',
            '_desktop_kill_pids'}, self.ns)
        for name in cls.__dict__:
            if not name.startswith('__'):
                setattr(self.d, name, getattr(cls, name).__get__(self.d))
        self.add_window(hwnd=90, pid=9)
        self.clock = Clock()
        self.ns['time'] = NS(time=self.clock.time, sleep=self.clock.sleep)

    def test_same_pid_new_hwnd_refreshes_all_caches_and_frame(self):
        self.d.screenshot_handle_num, self.d.control_handle_list = 90, [90]
        self.d.screenshot_size = (1280, 720)
        self.d.image, self.d._desktop_cursor = 'old frame', (1, 2)
        self.rows.pop(90)
        self.add_window(hwnd=91, pid=9)
        self.assertTrue(self.d.desktop_window_exists())
        self.assertEqual((self.d.root_handle, self.d.root_handle_num), ('9', 91))
        self.assertEqual(self.d.screenshot_handle_num, 91)
        self.assertEqual(self.d.control_handle_list, [91])
        self.assertNotIn('image', self.d.__dict__)
        self.assertNotIn('_desktop_cursor', self.d.__dict__)
        self.assertTrue(self.d._desktop_login_done)
        self.d.config.startup_normalize.assert_not_called()

    def test_new_pid_binding_survives_cold_config_and_new_task_object(self):
        self.d.desktop_bind_pid(10, 100)
        self.add_window(hwnd=100, pid=10)
        self.assertEqual(self.d.config.script.device.handle, '9')
        self.assertTrue(self.d.desktop_window_exists())
        self.assertEqual(self.d.screenshot_handle_num, 100)
        self.assertEqual(self.d.control_handle_list, [100])
        self.assertIs(NS(device=self.d).device, self.d)

    def test_real_bitblt_method_reads_rebound_hwnd_not_cached_old_one(self):
        self.d.screenshot_handle_num = 90
        self.rows.pop(90)
        self.add_window(hwnd=91, pid=9)
        self.assertTrue(self.d.desktop_window_exists())
        bitmap = Mock()
        bitmap.GetBitmapBits.return_value = np.zeros((720, 1280, 4), dtype=np.uint8).tobytes()
        ns = dict(dpi_awareness=nullcontext, GetClientRect=Mock(return_value=(0, 0, 1280, 720)),
                  GetDC=Mock(return_value='new client DC'), CreateDCFromHandle=Mock(),
                  CreateBitmap=Mock(return_value=bitmap), DeleteObject=Mock(),
                  frombuffer=np.frombuffer, cv2=cv2, SRCCOPY=0, RequestHumanTakeover=RequestHumanTakeover)
        cls = methods(WINDOW, 'Window', {'screenshot_desktop_bitblt'}, ns)
        self.d.desktop_window_restore_if_minimized = Mock()
        frame = cls.screenshot_desktop_bitblt(self.d)
        self.assertEqual(frame.shape, (720, 1280, 3))
        ns['GetClientRect'].assert_called_once_with(91)
        ns['GetDC'].assert_called_once_with(91)

    def test_missing_process_is_not_a_running_window(self):
        self.rows.clear()
        self.processes.clear()
        self.assertFalse(self.d.desktop_window_exists())

    def test_manager_hidden_owned_and_wrong_exe_are_excluded(self):
        self.rows.clear()
        self.processes.clear()
        cases = [dict(title='阴阳师管家'), dict(visible=False), dict(owner=1), dict(parent=1)]
        for changes in cases:
            self.add_window(hwnd=100, pid=9, **changes)
            with self.subTest(changes=changes):
                self.assertFalse(self.d.desktop_window_exists())
            self.rows.clear()
        self.add_window(hwnd=100, pid=9)
        self.processes[9].exe.return_value = r'C:\YYS\manager.exe'
        self.assertFalse(self.d.desktop_window_exists())

    def test_ambiguous_same_pid_game_windows_are_not_selected_arbitrarily(self):
        self.add_window(hwnd=91, pid=9)
        with self.assertRaises(self.ns['RequestHumanTakeover']):
            self.d.desktop_window_exists()
        self.assertEqual(self.d.root_handle_num, 90)

    def launch(self, rows):
        self.ns['list_desktop_windows'] = Mock(side_effect=[[], rows, rows, rows, rows])
        self.ns['subprocess'] = NS(Popen=Mock(return_value=NS(pid=20)))
        self.d.desktop_bind_pid = Mock()
        return self.d._desktop_launch_attempt('game.exe', '.', 3, 1)

    def test_launch_only_binds_verified_spawned_descendant(self):
        self.add_window(hwnd=200, pid=20, title='阴阳师管家')
        self.add_window(hwnd=210, pid=21)
        self.processes[21].parents = Mock(return_value=[NS(pid=20)])
        pid, spawned = self.launch([candidate(200, 20), candidate(210, 21)])
        self.assertEqual((pid, spawned), (21, {20, 21}))
        self.d.desktop_bind_pid.assert_called_once_with(21, 210)

    def test_launch_does_not_claim_unrelated_new_game(self):
        self.add_window(hwnd=210, pid=21)
        self.processes[21].parents = Mock(return_value=[NS(pid=999)])
        pid, spawned = self.launch([candidate(210, 21)])
        self.assertEqual((pid, spawned), (0, {20}))
        self.d.desktop_bind_pid.assert_not_called()

    def test_launch_multiple_verified_windows_is_bounded_and_not_bound(self):
        for hwnd, pid in ((210, 21), (220, 22)):
            self.add_window(hwnd, pid)
            self.processes[pid].parents = Mock(return_value=[NS(pid=20)])
        self.assertEqual(self.launch([candidate(210, 21), candidate(220, 22)])[0], 0)
        self.d.desktop_bind_pid.assert_not_called()

    def test_reused_pid_of_other_executable_is_never_killed(self):
        self.processes[9].exe.return_value = r'C:\Other\editor.exe'
        self.ns['ctypes'] = Mock()
        with self.assertRaises(self.ns['RequestHumanTakeover']):
            self.d.desktop_force_kill()
        self.ns['ctypes'].windll.kernel32.OpenProcess.assert_not_called()

    def test_missing_process_stop_does_not_open_termination_handle(self):
        self.psutil.Process.side_effect = self.ns['psutil'].NoSuchProcess()
        self.ns['ctypes'] = Mock()
        self.d.desktop_force_kill()
        self.ns['ctypes'].windll.kernel32.OpenProcess.assert_not_called()

    def test_stale_launcher_pid_of_other_executable_is_never_killed(self):
        self.processes[9].exe.return_value = r'C:\Other\editor.exe'
        self.ns['ctypes'] = Mock()
        self.ns['DESKTOP_KILL_ATTEMPTS'] = 3
        self.d._desktop_close_wait_seconds = lambda: 0
        with self.assertRaises(self.ns['RequestHumanTakeover']):
            self.d._desktop_kill_pids({9})
        self.ns['ctypes'].windll.kernel32.OpenProcess.assert_not_called()


class MessageTests(unittest.TestCase):
    def setUp(self):
        self.ns = dict(RequestHumanTakeover=RequestHumanTakeover,
                       DESKTOP_WINDOW_TITLES=('阴阳师-MuMu模拟器专版',),
                       IsWindow=Mock(return_value=True),
                       GetWindowThreadProcessId=Mock(return_value=(1, 9)),
                       GetWindowText=Mock(return_value='阴阳师-MuMu模拟器专版'),
                       PostMessage=Mock(), SendMessage=Mock(),
                       WM_LBUTTONDOWN=1, WM_CHAR=2, WM_KEYDOWN=3)
        cls = methods(WINDOW, 'Window', {'_desktop_check_message', '_desktop_post_message',
                                        '_desktop_send_message'}, self.ns)
        self.window = cls()
        self.window.root_handle_num = 90
        self.window.desktop_pid = lambda: 9

    def test_new_handle_receives_message(self):
        self.window.root_handle_num = 91
        self.window._desktop_post_message(91, 4, 0, 0)
        self.ns['PostMessage'].assert_called_once_with(91, 4, 0, 0)

    def test_invalid_reused_rebuilt_or_manager_hwnd_never_receives_input(self):
        for fault in ('invalid', 'pid', 'rebound', 'title'):
            self.setUp()
            if fault == 'invalid':
                self.ns['IsWindow'].return_value = False
            elif fault == 'pid':
                self.ns['GetWindowThreadProcessId'].return_value = (1, 999)
            elif fault == 'rebound':
                self.window.root_handle_num = 91
            else:
                self.ns['GetWindowText'].return_value = '阴阳师管家'
            with self.subTest(fault=fault), self.assertRaises(RequestHumanTakeover):
                self.window._desktop_post_message(90, 1, 0, 0)
            self.ns['PostMessage'].assert_not_called()

    def test_window_changes_mid_gesture_no_followup_reaches_new_window(self):
        self.window._desktop_post_message(90, 1, 0, 0)
        self.window.root_handle_num = 91
        with self.assertRaises(RequestHumanTakeover):
            self.window._desktop_post_message(90, 4, 0, 0)
        self.ns['PostMessage'].assert_called_once_with(90, 1, 0, 0)

    def test_waiting_blocks_mouse_keyboard_and_sync_messages(self):
        f = Fixture()
        f.confirm()
        self.window._reconnect_gate = f.gate
        for method in (self.window._desktop_post_message, self.window._desktop_send_message):
            for message in (1, 2, 3, 4):
                with self.assertRaises(ReconnectInterrupted):
                    method(90, message, 0, 0)
        self.ns['PostMessage'].assert_not_called()
        self.ns['SendMessage'].assert_not_called()

    def test_popup_appears_during_dwell_blocked_before_mouse_down(self):
        f = Fixture()
        f.device.frames = ['庭院']
        f.device.screenshot()
        f.device.frames = [TARGET, TARGET]
        f.clock.sleep(.4)
        self.window._reconnect_gate = f.gate
        self.window.image = f.device.image
        self.window.screenshot = lambda: (f.device.screenshot(), setattr(self.window, 'image', f.device.image))
        with self.assertRaises(ReconnectInterrupted):
            self.window._desktop_post_message(90, 1, 0, 0)
        self.ns['PostMessage'].assert_not_called()

    def test_switch_off_preserves_ordinary_message_path(self):
        f = Fixture(enabled=False)
        self.window._reconnect_gate = f.gate
        self.window._desktop_post_message(90, 1, 0, 0)
        self.ns['PostMessage'].assert_called_once()

    def test_all_desktop_dispatches_use_guard_and_mumu_hold_keeps_original(self):
        tree = ast.parse(WINDOW.read_text(encoding='utf-8-sig'))
        window = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == 'Window')
        names = {'click_desktop_window_message', 'long_click_desktop_window_message',
                 'move_desktop_window_message', 'move_desktop_plan',
                 'swipe_desktop_window_message', 'input_text_desktop'}
        for method in window.body:
            if not isinstance(method, ast.FunctionDef) or method.name not in names:
                continue
            with self.subTest(method=method.name):
                calls = [n for n in ast.walk(method) if isinstance(n, ast.Call)]
                self.assertFalse(any(isinstance(n.func, ast.Name) and n.func.id in ('PostMessage', 'SendMessage')
                                     for n in calls))


if __name__ == '__main__':
    unittest.main()
