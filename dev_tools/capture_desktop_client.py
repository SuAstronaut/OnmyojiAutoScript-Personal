"""Read-only Onmyoji client capture, in physical pixels (no window changes).

Uses the same GetDC(client)/BitBlt/RGB pipeline as OAS Window's desktop
backend, without its automatic restore and resize. Importing Window/Handle
also initializes module.logger (including old-log cleanup), so this diagnostic
deliberately imports only their Win32/image dependencies. Keep this small
adapter in sync with module/device/method/windows_impl.py and handle.py.
At 1280x720, PNG coordinates equal OAS logged Click coordinates. Otherwise
OAS normalizes its image to 1280x720; this tool preserves the actual pixels.
"""

import argparse
from contextlib import contextmanager, ExitStack
import ctypes
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_TITLE = '阴阳师-网易游戏'
TARGET = (1280, 720)
BACKEND = 'Win32 GetDC(client) + BitBlt(SRCCOPY), OAS-compatible raw RGB'


class CaptureError(RuntimeError):
    pass


class InvalidWindow(CaptureError):
    pass


@contextmanager
def physical_pixels(user32):
    """OAS's thread-local PMv2 context, with checked pointer-sized API types.

    Fail closed if unavailable: silently using logical pixels would invalidate
    coordinate analysis on scaled displays. Restore the caller's DPI context.
    """
    try:
        setter = user32.SetThreadDpiAwarenessContext
        getter = user32.GetThreadDpiAwarenessContext
        awareness = user32.GetAwarenessFromDpiAwarenessContext
        setter.argtypes = [ctypes.c_void_p]
        setter.restype = ctypes.c_void_p
        getter.argtypes = []
        getter.restype = ctypes.c_void_p
        awareness.argtypes = [ctypes.c_void_p]
        awareness.restype = ctypes.c_int
    except AttributeError as exc:
        raise CaptureError('Per-Monitor V2 DPI API unavailable; physical pixels cannot be guaranteed.') from exc
    previous = setter(ctypes.c_void_p(-4))
    if not previous:
        raise CaptureError('Cannot enter Per-Monitor V2 DPI context; capture aborted.')
    try:
        if awareness(getter()) != 2:
            raise CaptureError('Per-monitor DPI awareness not active; capture aborted.')
        yield
    finally:
        if not setter(previous):
            raise CaptureError('Cannot restore the calling thread DPI context.')


@dataclass(frozen=True)
class WindowInfo:
    hwnd: int
    pid: int
    title: str
    window_rect: tuple
    client_rect: tuple
    origin: tuple
    minimized: bool

    @property
    def size(self):
        left, top, right, bottom = self.client_rect
        return right - left, bottom - top


class Win32Backend:
    def __init__(self):
        if sys.platform != 'win32':
            raise CaptureError('This tool requires Windows.')
        try:
            import win32gui
            import win32process
            import win32ui
            import numpy
            from PIL import Image
        except ImportError as exc:
            raise CaptureError(
                f'Missing dependency: {exc}. Use the OAS toolkit Python: '
                f'"{ROOT / "toolkit" / "python.exe"}"') from exc
        self.gui = win32gui
        self.process = win32process
        self.ui = win32ui
        self.np = numpy
        self.user32 = ctypes.WinDLL('user32', use_last_error=True)

    def info(self, hwnd):
        g = self.gui
        if (not g.IsWindow(hwnd) or not g.IsWindowVisible(hwnd)
                or g.GetParent(hwnd) or g.GetWindow(hwnd, 4)
                or g.GetClassName(hwnd) in ('MPAY_LOGIN', 'IME', 'MSCTFIME UI', 'Button')):
            raise InvalidWindow(f'HWND {hwnd} is not a visible, unowned top-level game candidate.')
        with physical_pixels(self.user32):
            return WindowInfo(hwnd, self.process.GetWindowThreadProcessId(hwnd)[1],
                              g.GetWindowText(hwnd), g.GetWindowRect(hwnd),
                              g.GetClientRect(hwnd), g.ClientToScreen(hwnd, (0, 0)),
                              bool(g.IsIconic(hwnd)))

    def candidates(self, title):
        handles = []
        def collect(hwnd, _):
            handles.append(hwnd)
            return True
        try:
            self.gui.EnumWindows(collect, None)
        except Exception as exc:
            raise CaptureError('Cannot enumerate windows on this Windows desktop/session. '
                               f'Run in the interactive desktop session. Details: {exc}') from exc
        result = []
        fragment = '阴阳师' if title == DEFAULT_TITLE else title
        for hwnd in handles:
            try:
                name = self.gui.GetWindowText(hwnd)
                if name == title or fragment in name:
                    result.append(self.info(hwnd))
            except InvalidWindow:
                continue
            except CaptureError:
                raise
            except Exception:
                # A candidate can disappear while the read-only enumeration runs.
                continue
        return sorted(result, key=lambda item: item.hwnd)

    def capture(self, expected):
        """Capture only the client DC, releasing every GDI resource on failure."""
        before = self.info(expected.hwnd)
        if before != expected:
            raise CaptureError('Window identity/geometry changed before capture; run again.')
        check_capturable(before)
        width, height = before.size
        with physical_pixels(self.user32), ExitStack() as cleanup:
            dc_handle = self.gui.GetDC(before.hwnd)
            if not dc_handle:
                raise CaptureError('GetDC failed for the client area.')
            cleanup.callback(self.gui.ReleaseDC, before.hwnd, dc_handle)
            source = self.ui.CreateDCFromHandle(dc_handle)
            # CreateDCFromHandle is a non-owning PyCDC wrapper (no Detach API).
            # The acquired client DC belongs to GetDC/ReleaseDC, not DeleteDC.
            memory = source.CreateCompatibleDC()
            cleanup.callback(memory.DeleteDC)
            bitmap = self.ui.CreateBitmap()
            bitmap.CreateCompatibleBitmap(source, width, height)
            cleanup.callback(lambda: self.gui.DeleteObject(bitmap.GetHandle()))
            previous = memory.SelectObject(bitmap)
            cleanup.callback(memory.SelectObject, previous)
            memory.BitBlt((0, 0), (width, height), source, (0, 0), 0x00CC0020)
            raw = bitmap.GetBitmapBits(True)
            if len(raw) != width * height * 4:
                raise CaptureError('Unexpected BitBlt buffer size.')
            bgra = self.np.frombuffer(raw, dtype=self.np.uint8).reshape(height, width, 4)
            # Same conversion as OAS cv2.COLOR_BGR2RGB: discard GDI's undefined
            # alpha and copy B,G,R into R,G,B. No image resampling.
            rgb = bgra[:, :, [2, 1, 0]].copy()
        if self.info(before.hwnd) != before:
            raise CaptureError('Window identity/geometry changed during capture; run again.')
        return rgb


def check_capturable(info):
    if info.minimized:
        raise CaptureError('Window is minimized and current backend cannot reliably capture it. Restore it manually.')
    if min(info.size) <= 0:
        raise CaptureError(f'Invalid client size: {info.size[0]}x{info.size[1]}.')


def print_candidates(candidates):
    print('HWND       PID       CLIENT       TITLE')
    for item in candidates:
        width, height = item.size
        print(f'{item.hwnd:<10} {item.pid:<9} {width}x{height}       {item.title}')
        print(f'  Window rect: {item.window_rect}; Client rect: {item.client_rect}; '
              f'Client origin: {item.origin}; Minimized: {item.minimized}')


def choose_window(candidates, title):
    exact = [item for item in candidates if item.title == title]
    preferred = exact or candidates
    if not preferred:
        raise CaptureError('Onmyoji desktop client window not found.')
    if len(preferred) > 1:
        print_candidates(preferred)
        raise CaptureError('Multiple desktop client windows found. Specify --hwnd from the list.')
    return preferred[0]


def save_png(rgb, size, output=None, directory=None):
    """Save losslessly and reopen to verify RGB pixels and original dimensions."""
    import numpy as np
    from PIL import Image

    width, height = size
    if (width <= 0 or height <= 0 or rgb.size == 0 or rgb.dtype != np.uint8
            or rgb.shape != (height, width, 3)):
        raise CaptureError('Empty image or unexpected dimensions/RGB channels.')
    black = not bool(np.any(rgb))
    if black:
        print('WARNING: Capture is entirely black; PNG is diagnostic evidence, not a valid game frame.')
    if output is not None:
        path = Path(output).expanduser().resolve()
        path.parent.mkdir(parents=True, exist_ok=True)
        Image.fromarray(rgb, 'RGB').save(path, format='PNG')
    else:
        folder = Path(directory) if directory is not None else ROOT / 'screenshots' / 'desktop_capture'
        folder.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime('%Y%m%d_%H%M%S_%f')
        # Exclusive creation also protects against a same-microsecond collision.
        for suffix in range(1000):
            tail = f'_{suffix}' if suffix else ''
            path = (folder / f'desktop_client_{stamp}{tail}.png').resolve()
            try:
                with path.open('xb') as stream:
                    Image.fromarray(rgb, 'RGB').save(stream, format='PNG')
                break
            except FileExistsError:
                continue
        else:
            raise CaptureError('Cannot allocate a unique screenshot filename.')
    if not path.is_file() or path.stat().st_size == 0:
        raise CaptureError(f'PNG missing or empty: {path}')
    with Image.open(path) as image:
        image.load()
        if (image.format != 'PNG' or image.size != size or image.mode != 'RGB'
                or not np.array_equal(np.asarray(image), rgb)):
            raise CaptureError(f'Saved PNG failed pixel/dimension/channel verification: {path}')
    return path, black


def parser():
    result = argparse.ArgumentParser(
        description='Capture the Onmyoji Windows desktop client area using coordinates compatible with OAS Desktop mode.',
        epilog='Examples:\n  python dev_tools\\capture_desktop_client.py\n'
               '  python dev_tools\\capture_desktop_client.py --list\n'
               '  python dev_tools\\capture_desktop_client.py --output "D:\\Documents\\desktop.png"',
        formatter_class=argparse.RawDescriptionHelpFormatter)
    result.add_argument('--output', type=Path, help='PNG path (an existing file is overwritten); default: unique timestamp in screenshots/desktop_capture/')
    result.add_argument('--title', default=DEFAULT_TITLE, help='Prefer an exact title, then visible title substring matches')
    result.add_argument('--list', action='store_true', help='List candidates only; no capture')
    result.add_argument('--hwnd', type=lambda text: int(text, 16) if text.lower().startswith('0x') else int(text),
                        help='Explicit HWND from --list (decimal or 0x hexadecimal)')
    return result


def main(argv=None, backend=None):
    args = parser().parse_args(argv)
    try:
        if not args.title.strip():
            raise CaptureError('--title must not be empty.')
        backend = backend if backend is not None else Win32Backend()
        if args.hwnd is not None:
            info = backend.info(args.hwnd)
            if args.list:
                print_candidates([info])
                return 0
        else:
            candidates = backend.candidates(args.title)
            if args.list:
                print_candidates(candidates)
                if not candidates:
                    raise CaptureError('Onmyoji desktop client window not found.')
                return 0
            info = choose_window(candidates, args.title)
        check_capturable(info)
        width, height = info.size
        print('Desktop Client Capture\n--------------------------------')
        for label, value in (
                ('Window title', info.title), ('HWND', info.hwnd), ('PID', info.pid),
                ('Window rect', info.window_rect), ('Client rect', info.client_rect),
                ('Client origin', info.origin), ('Client size', f'{width}x{height}'),
                ('Backend', BACKEND), ('Expected', '1280x720'),
                ('Pixel space', 'physical; Per-Monitor V2 DPI'),
                ('Uses OAS Desktop screenshot backend', 'NO (same raw Win32 pipeline)')):
            print(f'{label:<35}: {value}')
        print(f'Fallback backend: {BACKEND}')
        if info.size != TARGET:
            print(f'WARNING:\nCurrent client size: {width}x{height}\nExpected OAS target: 1280x720\n'
                  'Screenshot will be saved without resizing.\n'
                  f'OAS Click (x,y) maps approximately to raw PNG (x*{width}/1280, y*{height}/720); '
                  'DPI message rounding may add a pixel difference.')
        else:
            print('PNG (x,y) = OAS screenshot/logged Click (x,y). Message rounding may affect actual input.')
        rgb = backend.capture(info)
        path, black = save_png(rgb, info.size, args.output)
        print(f'\nSaved screenshot:\n{path}\n\nImage size:\n{width}x{height}')
        if black:
            print('ERROR: Black capture saved for diagnosis; valid capture NOT confirmed.')
            return 1
        print('PNG verified: RGB pixels preserved; no resizing.')
        return 0
    except Exception as exc:
        print(f'ERROR: {exc}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
