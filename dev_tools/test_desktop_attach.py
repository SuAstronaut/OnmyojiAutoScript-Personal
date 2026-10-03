"""Desktop 接管离线回归：执行真实方法，Win32/进程/启动/写配置均隔离。

Run: toolkit/python.exe -B dev_tools/test_desktop_attach.py -v
"""
import ast
from functools import cached_property
from pathlib import Path
import tempfile
from types import SimpleNamespace as NS
import unittest
from unittest.mock import Mock, patch


ROOT = Path(__file__).resolve().parents[1]
HANDLE = ROOT / 'module/device/handle.py'
DEVICE = ROOT / 'module/device/device.py'
WINDOW = ROOT / 'module/device/method/windows_impl.py'


class Takeover(Exception):
    pass


class NoSuchProcess(Exception):
    pass


class AccessDenied(Exception):
    pass


class Bootstrap:
    def __init__(self, config):
        self.config = config
        if getattr(config, 'bootstrap_error', False):
            raise Takeover('saved PID disappeared during Handle init')


def load_class(path, name, methods, base, namespace):
    tree = ast.parse(path.read_text(encoding='utf-8-sig'))
    original = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == name)
    selected = [n for n in original.body if isinstance(n, ast.FunctionDef) and n.name in methods]
    assert len(selected) == len(methods), (name, methods)
    node = ast.ClassDef(name=name, bases=[ast.Name(id=base, ctx=ast.Load())],
                        keywords=[], body=selected, decorator_list=[])
    exec(compile(ast.fix_missing_locations(ast.Module(body=[node], type_ignores=[])),
                 str(path), 'exec'), namespace)
    return namespace[name]


def candidate(hwnd=100, pid=10, **kwargs):
    return dict(hwnd=hwnd, pid=pid, title='阴阳师-MuMu模拟器专版', x=311, y=126, **kwargs)


class DesktopAttachTests(unittest.TestCase):
    def setUp(self):
        import os
        self.rows = {}
        self.processes = {}
        self.log = NS(info=Mock(), warning=Mock(), error=Mock(), critical=Mock())
        self.psutil = NS(Process=Mock(side_effect=lambda pid: self.processes[pid]),
                         pid_exists=Mock(side_effect=lambda pid: pid in self.processes),
                         NoSuchProcess=NoSuchProcess, AccessDenied=AccessDenied)
        self.ns = dict(Bootstrap=Bootstrap, Path=Path, os=os, psutil=self.psutil,
                       cached_property=cached_property, logger=self.log,
                       RequestHumanTakeover=Takeover, EmulatorNotRunningError=Takeover,
                       EmulatorState=NS(COLD='cold', HEALTHY='healthy'),
                       DESKTOP_WINDOW_TITLES=('阴阳师-网易游戏', '阴阳师-MuMu模拟器专版'),
                       DESKTOP_LOGIN_POPUP_CLASS='MPAY_LOGIN', GW_OWNER=4,
                       del_cached_property=lambda obj, prop: obj.__dict__.pop(prop, None))
        functions = {
            'IsWindow': lambda h: h in self.rows,
            'IsWindowVisible': lambda h: self.rows[h].get('visible', True),
            'GetParent': lambda h: self.rows[h].get('parent', 0),
            'GetWindow': lambda h, flag: self.rows[h].get('owner', 0),
            'GetWindowText': lambda h: self.rows[h]['title'],
            'GetClassName': lambda h: self.rows[h].get('class', 'Win32Window'),
            'GetWindowThreadProcessId': lambda h: (1, self.rows[h]['pid']),
            'GetWindowRect': lambda h: (311, 126, 1609, 893),
            'EnumWindows': lambda cb, target: [cb(h, target) for h in list(self.rows)],
        }
        self.ns.update({name: Mock(side_effect=fn) for name, fn in functions.items()})
        tree = ast.parse(HANDLE.read_text(encoding='utf-8-sig'))
        node = next(n for n in tree.body if isinstance(n, ast.FunctionDef)
                    and n.name == 'list_desktop_windows')
        exec(compile(ast.Module(body=[node], type_ignores=[]), str(HANDLE), 'exec'), self.ns)
        load_class(HANDLE, 'Handle', {
            'desktop_pid', '_desktop_existing_window_valid', 'desktop_existing_candidates',
            'desktop_attach_existing', '_desktop_clear_handle_cache', 'desktop_bind_pid',
            '_desktop_root_from_path', 'screenshot_handle_num', 'screenshot_size',
        }, 'Bootstrap', self.ns)
        load_class(WINDOW, 'Window', {'control_handle_list'}, 'Handle', self.ns)
        self.cls = load_class(DEVICE, 'Device', {
            '__init__', '_desktop_ensure_launched', '_init_desktop', 'app_start',
        }, 'Window', self.ns)
        self.d = self.cls.__new__(self.cls)
        self.d.config = NS(script=NS(device=NS(serial='desktop', handle='9',
                                               desktop_game_path='',
                                               screenshot_method='window_background',
                                               control_method='window_message')),
                           startup_normalize=Mock(), save=Mock())
        self.d.root_handle = '9'
        self.d.root_handle_num = 90
        self.d.is_desktop_window = True
        self.d._desktop_login_done = True
        self.d.is_desktop = True
        self.d.desktop_window_exists = Mock(return_value=False)
        self.d.launch_desktop_client = Mock(return_value=True)
        self.d.desktop_window_set_size = Mock()
        self.d.screenshot_interval_set = Mock()
        self.d._transition_to = Mock()
        self.d._ensure_humanizer_context = Mock()

    def add_window(self, hwnd=100, pid=10, **changes):
        self.rows[hwnd] = dict(title='阴阳师-MuMu模拟器专版', pid=pid)
        self.rows[hwnd].update(changes)
        self.processes[pid] = NS(is_running=Mock(return_value=True),
                                 exe=Mock(return_value=r'C:\YYS\bin\onmyoji.exe'))

    def ensure(self):
        return self.d._desktop_ensure_launched()

    def test_valid_saved_pid_skips_discovery_attach_and_launch(self):
        self.add_window()
        self.add_window(hwnd=101, pid=11)
        self.d.desktop_window_exists.return_value = True
        self.d.desktop_existing_candidates = Mock(side_effect=AssertionError('must not discover'))
        self.d.desktop_attach_existing = Mock()
        self.assertTrue(self.ensure())
        self.assertEqual(self.d.root_handle, '9')
        self.d.desktop_existing_candidates.assert_not_called()
        self.d.desktop_attach_existing.assert_not_called()
        self.d.launch_desktop_client.assert_not_called()

    def test_unique_existing_window_attaches_and_persists(self):
        self.add_window()
        self.assertTrue(self.ensure())
        self.assertEqual((self.d.root_handle, self.d.root_handle_num), ('10', 100))
        self.d.config.startup_normalize.assert_called_once_with({('script', 'device', 'handle'): '10'})
        self.d.launch_desktop_client.assert_not_called()

    def test_empty_saved_pid_also_attaches_unique_existing_window(self):
        self.d.config.script.device.handle = ''
        del self.d.root_handle
        del self.d.root_handle_num
        self.d.is_desktop_window = False
        self.add_window()
        self.assertTrue(self.ensure())
        self.d.desktop_window_exists.assert_not_called()
        self.assertEqual(self.d.root_handle, '10')

    def test_no_candidates_uses_original_auto_launch(self):
        self.assertTrue(self.ensure())
        self.d.launch_desktop_client.assert_called_once_with()
        self.d.config.startup_normalize.assert_not_called()

    def test_no_candidates_preserves_launch_failure(self):
        self.d.launch_desktop_client.return_value = False
        self.assertFalse(self.ensure())

    def test_multiple_candidates_require_manual_selection_without_launch(self):
        self.add_window()
        self.add_window(hwnd=101, pid=11)
        with self.assertRaisesRegex(Takeover, 'WebUI.*handle'):
            self.ensure()
        self.assertEqual((self.d.root_handle, self.d.root_handle_num), ('9', 90))
        self.d.launch_desktop_client.assert_not_called()
        self.d.config.startup_normalize.assert_not_called()
        self.assertIn('PID=10', self.log.error.call_args[0][0])
        self.assertIn('PID=11', self.log.error.call_args[0][0])

    def test_attach_clears_old_handles_frames_and_dimensions(self):
        self.add_window()
        self.d.__dict__.update(screenshot_handle_num=90, screenshot_size=(1136, 640),
                               control_handle_list=[90], image='old frame', _desktop_cursor=(9, 9),
                               _screen_size_checked=True, _screen_black_checked=True)
        self.assertTrue(self.ensure())
        self.assertEqual(self.d.screenshot_handle_num, 100)
        self.assertEqual(self.d.screenshot_size, (1280, 720))
        self.assertEqual(self.d.control_handle_list, [100])
        self.assertNotIn('image', self.d.__dict__)
        self.assertNotIn('_desktop_cursor', self.d.__dict__)
        self.assertFalse(self.d._screen_size_checked)
        self.assertFalse(self.d._screen_black_checked)

    def test_attach_preserves_both_logged_in_and_logged_out_states(self):
        self.add_window()
        for state in (True, False):
            with self.subTest(state=state):
                self.d._desktop_login_done = state
                self.assertTrue(self.d.desktop_attach_existing(candidate()))
                self.assertEqual(self.d._desktop_login_done, state)

    def test_new_launch_binding_still_marks_logged_out(self):
        self.d.desktop_bind_pid(10, 100)
        self.assertFalse(self.d._desktop_login_done)
        self.assertEqual((self.d.root_handle, self.d.root_handle_num), ('10', 100))

    def test_runtime_attach_persists_after_startup_snapshot_frozen(self):
        self.add_window()
        self.d.config.startup_normalize.side_effect = RuntimeError('frozen')
        self.assertTrue(self.ensure())
        self.assertEqual(self.d.config.script.device.handle, '10')
        self.d.config.save.assert_called_once_with()

    def test_candidate_filters_auxiliary_invisible_owned_child_and_dead_process(self):
        self.add_window()
        for h, changes in enumerate([
            {'visible': False}, {'parent': 100}, {'owner': 100}, {'class': 'Button'},
            {'class': 'IME'}, {'class': 'MSCTFIME UI'}, {'class': 'GDI+ Hook Window Class'},
            {'class': 'MPAY_LOGIN'}, {'title': 'other app'},
        ], start=101):
            self.add_window(hwnd=h, pid=h, **changes)
        self.add_window(hwnd=120, pid=120)
        self.processes[120].is_running.return_value = False
        self.add_window(hwnd=121, pid=121)
        self.processes[121].exe.side_effect = NoSuchProcess()
        self.add_window(hwnd=122, pid=self.ns['os'].getpid())
        self.assertEqual([w['hwnd'] for w in self.d.desktop_existing_candidates()], [100])

    def test_wrong_exe_name_is_rejected(self):
        self.add_window()
        self.processes[10].exe.return_value = r'C:\YYS\Launch.exe'
        self.assertEqual(self.d.desktop_existing_candidates(), [])

    def test_exe_access_denied_falls_back_to_window_pid_checks(self):
        self.add_window()
        self.processes[10].exe.side_effect = AccessDenied('denied')
        self.assertTrue(self.ensure())
        self.d.launch_desktop_client.assert_not_called()
        self.assertTrue(self.log.warning.called)

    def test_process_open_access_denied_also_falls_back_if_pid_exists(self):
        self.add_window()
        self.psutil.Process.side_effect = AccessDenied('denied')
        self.assertTrue(self.ensure())
        self.d.launch_desktop_client.assert_not_called()

    def test_missing_process_is_not_a_candidate(self):
        self.add_window()
        self.processes.clear()
        self.assertEqual(self.d.desktop_existing_candidates(), [])

    def test_invalid_path_error_does_not_hide_existing_window(self):
        self.add_window()
        self.d.config.script.device.desktop_game_path = 'invalid-path'
        self.d._desktop_root_from_path = Mock(side_effect=OSError('invalid path'))
        self.assertTrue(self.ensure())
        self.d.launch_desktop_client.assert_not_called()

    def test_valid_config_root_filters_other_installation(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / 'bin').mkdir()
            exe = root / 'bin' / 'onmyoji.exe'
            exe.touch()
            self.d.config.script.device.desktop_game_path = str(root)
            self.add_window()
            self.add_window(hwnd=101, pid=11)
            self.processes[10].exe.return_value = str(exe)
            self.assertEqual([w['pid'] for w in self.d.desktop_existing_candidates()], [10])

    def test_invalid_config_root_does_not_block_existing_client(self):
        self.d.config.script.device.desktop_game_path = 'missing-install-root'
        self.add_window()
        self.assertTrue(self.ensure())

    def test_candidate_disappears_before_attach_then_launches(self):
        self.add_window()
        original = self.d.desktop_attach_existing
        def disappear(window):
            self.rows.clear()
            return original(window)
        self.d.desktop_attach_existing = Mock(side_effect=disappear)
        self.assertTrue(self.ensure())
        self.d.desktop_attach_existing.assert_called_once()
        self.d.launch_desktop_client.assert_called_once()
        self.d.config.startup_normalize.assert_not_called()

    def test_candidate_disappears_then_multiple_candidates_stop(self):
        self.add_window()
        original = self.d.desktop_attach_existing
        def change(window):
            self.rows.clear()
            self.add_window(hwnd=101, pid=11)
            self.add_window(hwnd=102, pid=12)
            return original(window)
        self.d.desktop_attach_existing = Mock(side_effect=change)
        with self.assertRaisesRegex(Takeover, '多个'):
            self.ensure()
        self.d.launch_desktop_client.assert_not_called()

    def test_repeated_race_is_bounded_without_guessing_or_launching(self):
        self.add_window()
        self.d.desktop_attach_existing = Mock(return_value=False)
        with self.assertRaisesRegex(Takeover, '持续变化'):
            self.ensure()
        self.assertEqual(self.d.desktop_attach_existing.call_count, 2)
        self.d.launch_desktop_client.assert_not_called()

    def test_discovery_error_never_means_no_client(self):
        self.ns['EnumWindows'].side_effect = OSError('desktop access denied')
        with self.assertRaisesRegex(Takeover, '无法检查'):
            self.ensure()
        self.d.launch_desktop_client.assert_not_called()

    def test_window_property_race_does_not_crash_enumeration(self):
        self.add_window()
        self.add_window(hwnd=101, pid=11)
        read_title = self.ns['GetWindowText'].side_effect
        def raced_title(hwnd):
            if hwnd == 101:
                raise OSError('gone')
            return read_title(hwnd)
        self.ns['GetWindowText'].side_effect = raced_title
        self.assertEqual([w['pid'] for w in self.d.desktop_existing_candidates()], [10])

    def test_pid_changed_before_attach_is_rejected(self):
        self.add_window(pid=11)
        self.assertFalse(self.d.desktop_attach_existing(candidate(pid=10)))
        self.d.config.startup_normalize.assert_not_called()

    def test_path_quotes_only_strip_one_matching_outer_pair(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / 'bin').mkdir()
            exe = root / 'bin' / 'onmyoji.exe'
            exe.touch()
            for value in (str(root), f'"{root}"', f"'{root}'", f'  "{root}"  ',
                          str(exe), f'"{exe}"'):
                with self.subTest(value=value):
                    self.assertEqual(self.d._desktop_root_from_path(value), root.resolve())
            for value in (f'"{root}', f"'{root}\"", f'""{root}""', str(root / 'bin')):
                with self.subTest(value=value):
                    self.assertIsNone(self.d._desktop_root_from_path(value))

    def test_path_quote_normalization_preserves_internal_apostrophe(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder) / "Y'YS"
            (root / 'bin').mkdir(parents=True)
            (root / 'bin' / 'onmyoji.exe').touch()
            self.assertEqual(self.d._desktop_root_from_path(f'"{root}"'), root.resolve())

    def test_launch_binding_also_discards_cached_control_handle(self):
        self.d.__dict__.update(control_handle_list=[90], screenshot_handle_num=90)
        self.d.desktop_bind_pid(10, 100)
        self.assertEqual(self.d.control_handle_list, [100])
        self.assertEqual(self.d.screenshot_handle_num, 100)

    def test_desktop_initialization_attach_uses_existing_resize_flow(self):
        self.add_window()
        health = NS(EmulatorHealth=Mock())
        reset = NS(FullReset=Mock())
        with patch.dict('sys.modules', {'module.device.emulator_health': health,
                                        'module.device.emulator_reset': reset}):
            self.d.__init__(config=self.d.config)
        self.d.desktop_window_set_size.assert_called_once_with()
        self.d.screenshot_interval_set.assert_called_once_with()
        self.d.launch_desktop_client.assert_not_called()
        self.assertTrue(self.d._desktop_login_done)

    def test_invalid_saved_pid_handle_init_exception_still_attaches(self):
        self.add_window()
        self.d.config.bootstrap_error = True
        with patch.dict('sys.modules', {
            'module.device.emulator_health': NS(EmulatorHealth=Mock()),
            'module.device.emulator_reset': NS(FullReset=Mock()),
        }):
            self.d.__init__(config=self.d.config)
        self.assertEqual((self.d.root_handle, self.d.root_handle_num), ('10', 100))
        self.d.desktop_window_set_size.assert_called_once()
        self.d.launch_desktop_client.assert_not_called()

    def test_runtime_app_start_attach_also_uses_existing_resize_flow(self):
        self.add_window()
        self.d.app_start()
        self.d.desktop_window_set_size.assert_called_once_with()
        self.d.launch_desktop_client.assert_not_called()

    def test_emulator_mode_never_uses_desktop_discovery(self):
        self.d.config.script.device.serial = 'emulator-5554'
        self.assertEqual(self.d.desktop_existing_candidates(), [])
        self.ns['EnumWindows'].assert_not_called()


if __name__ == '__main__':
    unittest.main()
