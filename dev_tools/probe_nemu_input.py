# -*- coding: utf-8 -*-
"""nemu_ipc 输入通道探针：验证 MuMu 官方 IPC 的 down/up 注入与「连续 down 即滑动」的轨迹质量。

背景：OAS 六个模拟器实例控制走 minitouch（设备侧常驻进程，是最易被扫描的驻留项）。
nemu_ipc.py 里已有 nemu_input_event_touch_down/up 接口但未注册为控制通道。本探针在
切换前回答三个问题：
  1) down/up 是否在 Android 内核输入层产生真实事件（getevent 可见 = 内核级注入）；
  2) 连续 down 不同坐标是否被解释为滑动（MOVE），轨迹坐标是否连续平滑；
  3) 每次 down 调用的 IPC 往返延迟是多少（决定滑动能否做到 ~10ms 级步进）。

用法（目标实例已开机且 adb 可连）：
    ./toolkit/python.exe -m dev_tools.probe_nemu_input --serial 127.0.0.1:5555 \
        --nemu-folder "I:/Program Files/Netease/MuMu"

产物：log/nemu_probe/<时间戳>/ 下的截图与 getevent 解析报告。
"""
import argparse
import re
import subprocess
import threading
import time
from pathlib import Path

import cv2
import numpy as np

from module.device.method.minitouch import insert_swipe
from module.device.method.nemu_ipc import NemuIpcImpl
from module.logger import logger

# getevent 行示例：[   1234.567890] /dev/input/event5: EV_ABS ABS_MT_POSITION_X 000001f4
GETEVENT_RE = re.compile(r'^\[\s*(\d+\.\d+)\]\s+(\S+):\s+([A-Z_]+)\s+([A-Z_0-9]+)\s+(\S+)')


class GeteventRecorder:
    """后台抓取 adb getevent -lt 全设备事件流（内核 evdev 层，注入可见即内核级）。"""

    def __init__(self, adb_path, serial):
        self.proc = subprocess.Popen(
            [str(adb_path), '-s', serial, 'shell', 'getevent -lt'],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, encoding='utf-8', errors='ignore')
        self.lines = []
        self._lock = threading.Lock()
        self._thread = threading.Thread(target=self._reader, daemon=True)
        self._thread.start()

    def _reader(self):
        for line in self.proc.stdout:
            with self._lock:
                self.lines.append(line.rstrip('\n'))

    def mark(self, tag):
        """插入标记行，用于在事件流里切分各个手势窗口。"""
        with self._lock:
            self.lines.append(f'### MARK {tag} {time.time():.6f}')

    def window(self, tag):
        """取最近一个 MARK tag 到下一个 MARK 之间的事件行。"""
        with self._lock:
            lines = list(self.lines)
        idx = [i for i, l in enumerate(lines) if l.startswith(f'### MARK {tag} ')]
        if not idx:
            return []
        start = idx[-1] + 1
        end = next((i for i, l in enumerate(lines[start:], start)
                    if l.startswith('### MARK')), len(lines))
        return lines[start:end]

    def stop(self):
        self.proc.kill()


def find_running_instance(nemu_folder, adb_path, serial):
    """扫描 instance_id 0..8：能成功截图的即运行中实例；再与 adb 截图比对确认身份。"""
    # adb 基准截图（resize 到 1280x720 与 nemu 帧对齐后比对）
    import adbutils
    from adbutils import adb
    adb.connect(serial, timeout=8)
    raw = subprocess.run([str(adb_path), '-s', serial, 'exec-out', 'screencap -p'],
                         capture_output=True, timeout=15).stdout
    ref_img = cv2.imdecode(np.frombuffer(raw, np.uint8), cv2.IMREAD_COLOR)
    ref = cv2.resize(ref_img, (1280, 720))

    running = []
    for iid in range(9):
        try:
            impl = NemuIpcImpl(nemu_folder=nemu_folder, instance_id=iid, display_id=0).__enter__()
            img = impl.screenshot()
            running.append((iid, impl, img))
            logger.info(f'instance {iid}: 截图 OK')
        except Exception as e:
            logger.info(f'instance {iid}: 不可用（{type(e).__name__}）')

    if not running:
        raise RuntimeError('没有任何运行中的 nemu 实例')
    if len(running) == 1:
        return running[0][0], running[0][1], ref

    # 多实例运行中：与 adb 基准逐像素比对，找同一块屏幕
    best, best_diff = None, None
    for iid, impl, img in running:
        diff = float(np.mean(cv2.absdiff(
            cv2.resize(img, (1280, 720)), ref)))
        logger.info(f'instance {iid}: 与 adb 截图平均像素差 {diff:.1f}')
        if best_diff is None or diff < best_diff:
            best, best_diff = (iid, impl), diff
    # 关掉未选中的连接，避免句柄泄漏
    for iid, impl, _ in running:
        if iid != best[0]:
            try:
                impl.disconnect()
            except Exception:
                pass
    return best[0], best[1], ref


def bezier_points(p0, p2, n=30):
    """水平方向弓形的贝塞尔轨迹（模拟真实滑动的弧线），返回 [(x, y), ...]。"""
    p1 = ((p0[0] + p2[0]) / 2, min(p0[1], p2[1]) - 120)  # 控制点向上拱
    pts = []
    for i in range(n):
        t = i / (n - 1)
        x = (1 - t) ** 2 * p0[0] + 2 * (1 - t) * t * p1[0] + t ** 2 * p2[0]
        y = (1 - t) ** 2 * p0[1] + 2 * (1 - t) * t * p1[1] + t ** 2 * p2[1]
        pts.append((int(x), int(y)))
    return pts


def analyze(window_lines, title, report):
    """解析一个手势窗口的事件流：设备名、事件类型序列、坐标轨迹、时间跨度。"""
    report.append(f'\n===== {title} =====')
    parsed = []
    for line in window_lines:
        m = GETEVENT_RE.match(line)
        if m:
            parsed.append((float(m.group(1)), m.group(2), m.group(3), m.group(4), m.group(5)))
    if not parsed:
        report.append('  (getevent 无事件 —— 注入未到达内核 evdev 层)')
        return
    devices = sorted({p[1] for p in parsed})
    report.append(f'  设备: {devices}  事件数: {len(parsed)}  时间跨度: '
                  f'{parsed[-1][0] - parsed[0][0]:.3f}s')
    codes = {}
    for _, _, etype, code, _ in parsed:
        codes.setdefault(etype, {}).setdefault(code, 0)
        codes[etype][code] += 1
    for etype, cc in codes.items():
        report.append(f'  {etype}: ' + ', '.join(f'{k}×{v}' for k, v in sorted(cc.items())))
    xs = [int(p[4], 16) for p in parsed if p[3] == 'ABS_MT_POSITION_X']
    ys = [int(p[4], 16) for p in parsed if p[3] == 'ABS_MT_POSITION_Y']
    if xs:
        report.append(f'  轨迹: {len(xs)} 个位置点, x∈[{min(xs)},{max(xs)}], y∈[{min(ys)},{max(ys)}]')
    keys = [p[3] for p in parsed if p[2] == 'EV_KEY']
    if keys:
        report.append(f'  按键序列: {keys}')


def main():
    parser = argparse.ArgumentParser(description='nemu_ipc 输入通道探针')
    parser.add_argument('--serial', default='127.0.0.1:5555', help='目标实例的 adb serial')
    parser.add_argument('--nemu-folder', default=r'I:\Program Files\Netease\MuMu', help='MuMu 安装目录')
    args = parser.parse_args()

    out_dir = Path('log/nemu_probe') / time.strftime('%Y%m%d_%H%M%S')
    out_dir.mkdir(parents=True, exist_ok=True)

    from adbutils import adb
    # adb_path 是函数；项目自带 adb 二进制在 adbutils/binaries 下
    import adbutils
    adb_path = Path(adbutils.__file__).parent / 'binaries' / 'adb.exe'
    adb.connect(args.serial, timeout=8)

    # [1] 定位运行中的 nemu 实例
    iid, impl, ref = find_running_instance(args.nemu_folder, adb_path, args.serial)
    logger.info(f'目标实例: instance_id={iid}')
    cv2.imwrite(str(out_dir / '00_ref_adb.png'), ref)
    cv2.imwrite(str(out_dir / '01_nemu_before.png'), impl.screenshot())

    # [2] 启动 getevent 事件流抓取
    rec = GeteventRecorder(adb_path, args.serial)
    time.sleep(1.0)  # 等流稳定

    report = [f'nemu_ipc 输入探针  serial={args.serial}  instance_id={iid}']

    # [3] 手势 A：单点 click（down→短暂→up）
    rec.mark('click')
    lat = []
    t0 = time.perf_counter()
    impl.down(640, 360)
    lat.append((time.perf_counter() - t0) * 1000)
    time.sleep(0.05)
    t0 = time.perf_counter()
    impl.up()
    lat.append((time.perf_counter() - t0) * 1000)
    time.sleep(0.8)

    # [4] 手势 B：30 点贝塞尔滑动（逐点 down，间隔 10ms，与上游 swipe_nemu_ipc 同语义）
    pts = bezier_points((200, 500), (1080, 300), n=30)
    rec.mark('swipe')
    for p in pts:
        t0 = time.perf_counter()
        impl.down(*p)
        lat.append((time.perf_counter() - t0) * 1000)
        time.sleep(0.010)
    impl.up()
    time.sleep(0.8)

    # [5] 手势 C：长按（down→1s→up）
    rec.mark('longpress')
    impl.down(400, 400)
    time.sleep(1.0)
    impl.up()
    time.sleep(0.8)

    # [6] 对照组：系统 input swipe（Instrumentation 注入基线）
    rec.mark('input_swipe')
    subprocess.run([str(adb_path), '-s', args.serial, 'shell',
                    'input swipe 700 500 700 200 300'], capture_output=True)
    time.sleep(0.8)

    time.sleep(0.5)
    rec.stop()
    cv2.imwrite(str(out_dir / '02_nemu_after.png'), impl.screenshot())

    # [7] 解析报告
    analyze(rec.window('click'), 'A. nemu 单点 click（期望: 一组 DOWN/UP）', report)
    analyze(rec.window('swipe'), 'B. nemu 30 点贝塞尔滑动（期望: 连续 MOVE + 单 DOWN/UP 配对）', report)
    analyze(rec.window('longpress'), 'C. nemu 长按 1s', report)
    analyze(rec.window('input_swipe'), 'D. 对照: 系统 input swipe', report)

    arr = np.array(lat)
    report.append(f'\n===== IPC 延迟（down/up 单次调用，n={len(lat)}） =====')
    report.append(f'  mean={arr.mean():.2f}ms  p50={np.percentile(arr, 50):.2f}ms  '
                  f'max={arr.max():.2f}ms')

    text = '\n'.join(report)
    (out_dir / 'report.txt').write_text(text, encoding='utf-8')
    print(text)
    print(f'\n产物目录: {out_dir}')
    impl.disconnect()


if __name__ == '__main__':
    main()
