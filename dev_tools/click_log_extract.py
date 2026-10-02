# -*- coding: utf-8 -*-
"""脚本点击提取器：从 OAS 日志提取 Click 行，产出与采集器同格式的 jsonl。

对照校准工具链（参考 SmartOnmyoji issue #45）的脚本侧数据源。OAS 的每次
点击都由 module/device/control.py 打日志（point2str 4 位右对齐坐标）：

    2026-08-20 00:10:02.628 | control.py:0075 | INFO | Click ( 646,  600) @ LOGIN_ENTER_GAME

提取后归一化到 720p 授权空间，与手动采集数据（click_collector.py）同坐标系，
供 click_analysis.html 叠加对比。

只提取 Click 行（左键点击）；long_click / Swipe 行不在本组件对照范围内。

用法：
    toolkit/python.exe -m dev_tools.click_log_extract log/2026-08-20_oas1.txt
    toolkit/python.exe -m dev_tools.click_log_extract log/2026-08-20_oas*.txt --resolution 540p

输出：log/click_monitor/<输入文件名去扩展名>_script.jsonl
"""
import argparse
import json
import re
import sys
from datetime import datetime
from pathlib import Path

sys.stdout.reconfigure(encoding='utf-8', errors='replace')

# 日志行首时间戳：2026-08-20 00:10:02.628（毫秒 3 位）
RE_TIMESTAMP = re.compile(r'^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\.\d{3})')
# Click 行：坐标为 point2str 4 位右对齐，空格数可变（个位数 3 空格、千位数 0 空格）。
# 行尾锚定 \s*$：long_click 行为 "Click (x, y) @ name <duration>"（名后跟时长数字），
# 锚定后无法匹配从而被自然排除；普通 Click 行名后到行尾只剩填充空白
RE_CLICK = re.compile(r'Click \(\s*(\d+),\s*(\d+)\) @ (\S+)\s*$')
# 任务块边界行：Scheduler: Start/End task `TaskName`
RE_TASK_START = re.compile(r'Scheduler: Start task `(\w+)`')
RE_TASK_END = re.compile(r'Scheduler: End task `(\w+)`')
# 日志里的时间无时区信息，本地时间解析（与 logger 写入侧一致）
DATETIME_FMT = '%Y-%m-%d %H:%M:%S.%f'

# 授权空间常宽（与 module/base/canvas.py 的 BASE_W/BASE_H 一致）
BASE_W, BASE_H = 1280, 720

# 输出目录：与采集器同目录，分析页按此约定找文件
OUTPUT_DIR = Path('./log/click_monitor')

# 540p 画布时 log 坐标的归一化倍率（540p 授权空间 960×540 → 720p 需 ×4/3）
SCALE_540P = 4 / 3


def parse_line(line):
    """解析一行日志，是 Click 行则返回 (ts_ms, x, y, name)，否则 None。"""
    m_ts = RE_TIMESTAMP.search(line)
    m_click = RE_CLICK.search(line)
    if not m_ts or not m_click:
        return None
    dt = datetime.strptime(m_ts.group(1), DATETIME_FMT)
    ts_ms = int(dt.timestamp() * 1000)
    return ts_ms, int(m_click.group(1)), int(m_click.group(2)), m_click.group(3)


def extract_file(log_path, resolution='720p', task=None):
    """提取单个日志文件的 Click 行，返回事件列表（已归一化到 720p）。

    task 不为 None 时只提取该任务块内的 Click：任务块由
    "Scheduler: Start task `X`" 到 "Scheduler: End task `X`" 划界（script.py
    的调度日志）。Start 后若无对应 End（进程被杀等），块延续到文件末尾。
    """
    scale = SCALE_540P if resolution == '540p' else 1.0
    events = []
    in_task = False
    with open(log_path, 'r', encoding='utf-8', errors='replace') as f:
        for line in f:
            m_start = RE_TASK_START.search(line)
            if m_start:
                # 嵌套任务（如 RyouToppa 内 call restart）以最后 Start 为准，
                # End 只在与当前任务同名时闭合块
                in_task = (task is None) or (m_start.group(1) == task)
                continue
            m_end = RE_TASK_END.search(line)
            if m_end and m_end.group(1) == task:
                in_task = False
                continue
            if task is not None and not in_task:
                continue
            parsed = parse_line(line)
            if parsed is None:
                continue
            ts, x, y, name = parsed
            events.append({
                'ts': ts,
                'x': round(x * scale),
                'y': round(y * scale),
                'source': 'script',
                # window 字段对 script 侧存控件名：分析页能看到点了哪些按钮
                'window': name,
            })
    return events


def main():
    parser = argparse.ArgumentParser(description='脚本点击提取器（OAS 日志 → jsonl）')
    parser.add_argument('logs', nargs='+', help='OAS 日志文件路径（支持通配符）')
    parser.add_argument('--resolution', choices=['720p', '540p'], default='720p',
                        help='日志产生时的画布档位（日志不记录，需人工指定），默认 720p')
    parser.add_argument('--task', help='只提取该任务块内的点击（如 RyouToppa），'
                        '按 "Scheduler: Start/End task" 边界过滤')
    args = parser.parse_args()

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    for log_path in map(Path, args.logs):
        if not log_path.exists():
            print('跳过不存在的文件: %s' % log_path)
            continue
        events = extract_file(log_path, args.resolution, task=args.task)
        # task 过滤时输出名带任务名，与全量提取区分开
        suffix = '_script' if not args.task else '_%s_script' % args.task
        out_path = OUTPUT_DIR / ('%s%s.jsonl' % (log_path.stem, suffix))
        with open(out_path, 'w', encoding='utf-8') as f:
            for event in events:
                f.write(json.dumps(event, ensure_ascii=False) + '\n')
        print('%s: 提取 %d 次点击 -> %s' % (log_path, len(events), out_path))


if __name__ == '__main__':
    main()
