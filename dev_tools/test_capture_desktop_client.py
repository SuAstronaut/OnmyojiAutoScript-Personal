"""Offline tests; never enumerate or operate real game windows.

Run: toolkit\python.exe -m unittest discover -s dev_tools -p test_capture_desktop_client.py
"""

from contextlib import redirect_stdout, redirect_stderr
import ctypes
from dataclasses import replace
import io
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

import numpy as np
from PIL import Image

import capture_desktop_client as capture


def window(hwnd=123, title=capture.DEFAULT_TITLE, size=(1280, 720), minimized=False):
    return capture.WindowInfo(hwnd, 456, title, (10, 20, 1300, 780),
                              (0, 0, *size), (15, 50), minimized)


class FakeBackend:
    def __init__(self, windows):
        self.windows = windows
        self.captures = 0

    def candidates(self, title):
        return self.windows

    def info(self, hwnd):
        return next(item for item in self.windows if item.hwnd == hwnd)

    def capture(self, info):
        self.captures += 1
        width, height = info.size
        rgb = np.empty((height, width, 3), np.uint8)
        rgb[:] = [31, 127, 241]
        return rgb


class Tests(unittest.TestCase):
    def run_main(self, args, backend):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = capture.main(args, backend)
        return code, out.getvalue() + err.getvalue()

    def test_selection_exact_fallback_missing_and_multiple(self):
        exact = window()
        fuzzy = window(124, '阴阳师-其他客户端')
        self.assertEqual(capture.choose_window([fuzzy, exact], capture.DEFAULT_TITLE), exact)
        self.assertEqual(capture.choose_window([fuzzy], capture.DEFAULT_TITLE), fuzzy)
        with self.assertRaisesRegex(capture.CaptureError, 'not found'):
            capture.choose_window([], capture.DEFAULT_TITLE)
        with redirect_stdout(io.StringIO()), self.assertRaisesRegex(capture.CaptureError, 'Multiple'):
            capture.choose_window([exact, window(125)], capture.DEFAULT_TITLE)

    def test_cli_list_no_capture_and_missing_error(self):
        backend = FakeBackend([window()])
        code, text = self.run_main(['--list'], backend)
        self.assertEqual(code, 0)
        self.assertIn('456', text)
        self.assertIn('Client rect', text)
        self.assertEqual(backend.captures, 0)
        code, text = self.run_main([], FakeBackend([]))
        self.assertEqual(code, 1)
        self.assertIn('ERROR: Onmyoji desktop client window not found.', text)

    def test_cli_minimized_zero_and_ambiguous_do_not_capture(self):
        for windows in ([window(minimized=True)], [window(size=(0, 0))],
                        [window(), window(124)]):
            backend = FakeBackend(windows)
            code, _ = self.run_main([], backend)
            self.assertEqual(code, 1)
            self.assertEqual(backend.captures, 0)

    def test_explicit_hwnd_output_and_non_target_dimensions(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / '中文' / 'capture.png'
            backend = FakeBackend([window(), window(124, size=(1278, 720))])
            code, text = self.run_main(['--hwnd', '0x7c', '--output', str(path)], backend)
            self.assertEqual(code, 0, text)
            self.assertIn('without resizing', text)
            with Image.open(path) as image:
                self.assertEqual(image.size, (1278, 720))
                self.assertEqual(image.mode, 'RGB')
                self.assertEqual(image.getpixel((1014, 664)), (31, 127, 241))

    def test_default_filename_collision_cannot_overwrite(self):
        rgb = np.full((2, 3, 3), 23, np.uint8)
        with tempfile.TemporaryDirectory() as folder, patch.object(capture, 'datetime') as clock:
            clock.now.return_value.strftime.return_value = '20261007_021530_000000'
            first, _ = capture.save_png(rgb, (3, 2), directory=folder)
            second, _ = capture.save_png(rgb + 1, (3, 2), directory=folder)
            self.assertNotEqual(first, second)
            with Image.open(first) as image:
                self.assertEqual(image.getpixel((0, 0)), (23, 23, 23))

    def test_black_warns_and_invalid_arrays_fail(self):
        with tempfile.TemporaryDirectory() as folder, redirect_stdout(io.StringIO()) as out:
            path, black = capture.save_png(np.zeros((2, 3, 3), np.uint8), (3, 2), directory=folder)
            self.assertTrue(path.is_file())
            self.assertTrue(black)
            self.assertIn('entirely black', out.getvalue())
            for array in (np.zeros((0, 0, 3), np.uint8), np.zeros((2, 3, 4), np.uint8),
                          np.zeros((2, 3, 3), np.float32), np.zeros((3, 2, 3), np.uint8)):
                with self.assertRaises(capture.CaptureError):
                    capture.save_png(array, (3, 2), directory=folder)

    def test_dpi_context_is_restored_even_on_error(self):
        user32 = Mock()
        user32.SetThreadDpiAwarenessContext.return_value = 987
        user32.GetAwarenessFromDpiAwarenessContext.return_value = 2
        with self.assertRaisesRegex(RuntimeError, 'test'):
            with capture.physical_pixels(user32):
                raise RuntimeError('test')
        self.assertEqual(user32.SetThreadDpiAwarenessContext.call_args_list[-1].args, (987,))
        self.assertIs(user32.SetThreadDpiAwarenessContext.restype, ctypes.c_void_p)
        user32.SetThreadDpiAwarenessContext.return_value = 0
        with self.assertRaisesRegex(capture.CaptureError, 'Cannot enter'):
            with capture.physical_pixels(user32):
                self.fail('Failed DPI context should not capture')

    def test_raw_bitblt_rgb_origin_and_resource_release(self):
        backend = object.__new__(capture.Win32Backend)
        backend.gui, backend.ui = Mock(), Mock()
        backend.np = np
        backend.user32 = Mock()
        backend.user32.SetThreadDpiAwarenessContext.return_value = 987
        backend.user32.GetAwarenessFromDpiAwarenessContext.return_value = 2
        info = window(size=(2, 1))
        backend.info = Mock(return_value=info)
        bitmap = backend.ui.CreateBitmap.return_value
        bitmap.GetBitmapBits.return_value = bytes([3, 2, 1, 0, 6, 5, 4, 255])
        image = backend.capture(info)
        np.testing.assert_array_equal(image, [[[1, 2, 3], [4, 5, 6]]])
        source = backend.ui.CreateDCFromHandle.return_value
        memory = source.CreateCompatibleDC.return_value
        memory.BitBlt.assert_called_once_with((0, 0), (2, 1), source, (0, 0), 0x00CC0020)
        source.DeleteDC.assert_not_called()
        memory.DeleteDC.assert_called_once()
        backend.gui.ReleaseDC.assert_called_once()
        backend.gui.DeleteObject.assert_called_once()
        memory.BitBlt.side_effect = RuntimeError('GDI failure')
        with self.assertRaisesRegex(RuntimeError, 'GDI failure'):
            backend.capture(info)
        self.assertEqual(backend.gui.ReleaseDC.call_count, 2)
        self.assertEqual(backend.gui.DeleteObject.call_count, 2)

    def test_capture_rejects_geometry_change(self):
        backend = object.__new__(capture.Win32Backend)
        info = window()
        backend.info = Mock(return_value=replace(info, origin=(20, 50)))
        with self.assertRaisesRegex(capture.CaptureError, 'changed'):
            backend.capture(info)

    def test_real_enumerator_contract_and_invalid_candidate_filter(self):
        backend = object.__new__(capture.Win32Backend)
        backend.gui = Mock()
        def enumerate_windows(callback, param):
            for hwnd in (123, 124, 125):
                self.assertIs(callback(hwnd, param), True)
        backend.gui.EnumWindows.side_effect = enumerate_windows
        backend.gui.GetWindowText.side_effect = lambda hwnd: {
            123: capture.DEFAULT_TITLE, 124: '阴阳师 登录', 125: 'Unrelated'}[hwnd]
        def info(hwnd):
            if hwnd == 124:
                raise capture.InvalidWindow('Owned login dialog')
            return window(hwnd)
        backend.info = info
        self.assertEqual([item.hwnd for item in backend.candidates(capture.DEFAULT_TITLE)], [123])

    def test_physical_geometry_at_display_scalings(self):
        for scale in (1.0, 1.25, 1.5):
            backend = object.__new__(capture.Win32Backend)
            backend.gui, backend.process = Mock(), Mock()
            backend.gui.IsWindow.return_value = True
            backend.gui.IsWindowVisible.return_value = True
            backend.gui.GetParent.return_value = 0
            backend.gui.GetWindow.return_value = 0
            backend.gui.GetClassName.return_value = 'Game'
            backend.gui.GetWindowText.return_value = capture.DEFAULT_TITLE
            backend.gui.IsIconic.return_value = False
            backend.process.GetWindowThreadProcessId.return_value = (111, 456)
            backend.user32 = Mock()
            active = [False]
            def setter(context):
                active[0] = isinstance(context, ctypes.c_void_p)
                return 987
            backend.user32.SetThreadDpiAwarenessContext.side_effect = setter
            backend.user32.GetAwarenessFromDpiAwarenessContext.return_value = 2
            backend.gui.GetClientRect.side_effect = lambda _: (
                (0, 0, 1280, 720) if active[0] else (0, 0, int(1280/scale), int(720/scale)))
            backend.gui.GetWindowRect.side_effect = lambda _: (10, 20, 1300, 780) if active[0] else self.fail('logical rect')
            backend.gui.ClientToScreen.side_effect = lambda *_: (15, 50) if active[0] else self.fail('logical origin')
            self.assertEqual(backend.info(123).size, (1280, 720))
            self.assertFalse(active[0])

    def test_black_cli_is_not_reported_as_success(self):
        backend = FakeBackend([window(size=(3, 2))])
        backend.capture = lambda _: np.zeros((2, 3, 3), np.uint8)
        with tempfile.TemporaryDirectory() as folder:
            code, text = self.run_main(['--output', str(Path(folder) / 'black.png')], backend)
            self.assertEqual(code, 1)
            self.assertIn('valid capture NOT confirmed', text)

    def test_read_only_import_and_call_boundary(self):
        import ast
        tree = ast.parse(Path(capture.__file__).read_text(encoding='utf-8'))
        forbidden = {'click', 'mouse_event', 'SendInput', 'SetCursorPos', 'keybd_event',
                     'MoveWindow', 'SetWindowPos', 'ShowWindow', 'SetForegroundWindow',
                     'PostMessage', 'SendMessage', 'resize', 'task', 'scheduler'}
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                name = node.func.attr if isinstance(node.func, ast.Attribute) else getattr(node.func, 'id', '')
                self.assertNotIn(name, forbidden)
            if isinstance(node, ast.ImportFrom):
                self.assertFalse((node.module or '').startswith(('module.', 'tasks.')))


if __name__ == '__main__':
    unittest.main()
