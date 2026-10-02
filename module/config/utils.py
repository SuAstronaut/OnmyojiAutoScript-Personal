# This Python file uses the following encoding: utf-8
# @author runhey
# github https://github.com/runhey
import os
import json
import yaml

from filelock import FileLock
from datetime import datetime, timedelta, timezone, time

from module.config.atomicwrites import atomic_write
from module.logger import logger

DEFAULT_TIME = datetime(2023, 1, 1, 0, 0)

def filepath_config(filename, mod_name='script') -> str:
    """
    返回配置文件的路径
    """
    if mod_name == 'script':
        return os.path.join('./config', f'{filename}.json')
    else:
        return os.path.join('./config', f'{filename}.{mod_name}.json')

def filepath_args(filename='args', mod_name='alas'):
    return f'./module/config/argument/{filename}.json'


def filepath_argument(filename):
    return f'./module/config/argument/{filename}.yaml'


def _read_content_by_extension(file: str):
    """
    按扩展名读取文件内容。调用方必须已持有对应锁（或确认目录已存在）；
    与 read_file 的差异是不做目录创建和文件存在性检查。
    """
    _, ext = os.path.splitext(file)
    if ext == '.yaml':
        with open(file, mode='r', encoding='utf-8') as f:
            s = f.read()
            data = list(yaml.safe_load_all(s))
            if len(data) == 1:
                data = data[0]
            if not data:
                data = {}
            return data
    elif ext == '.json':
        with open(file, mode='r', encoding='utf-8') as f:
            s = f.read()
            return json.loads(s)
    else:
        logger.warning(f'Unsupported config file extension: {ext}')
        return {}

def _atomic_write_content(file: str, data) -> None:
    """
    按扩展名原子写文件内容。调用方必须已持有对应锁（或确认目录已存在）；
    与 write_file 的差异是不做目录创建。
    """
    _, ext = os.path.splitext(file)
    if ext == '.yaml':
        with atomic_write(file, overwrite=True, encoding='utf-8', newline='') as f:
            if isinstance(data, list):
                yaml.safe_dump_all(data, f, default_flow_style=False, encoding='utf-8', allow_unicode=True,
                                   sort_keys=False)
            else:
                yaml.safe_dump(data, f, default_flow_style=False, encoding='utf-8', allow_unicode=True,
                               sort_keys=False)
    elif ext == '.json':
        with atomic_write(file, overwrite=True, encoding='utf-8', newline='') as f:
            s = json.dumps(data, indent=2, ensure_ascii=False, sort_keys=False, default=str)
            f.write(s)
    else:
        logger.warning(f'Unsupported config file extension: {ext}')

def _read_file_unlocked(file: str):
    """
    仅供配置事务内部使用的无锁读取原语；调用方必须已持有对应 FileLock。
    """
    return _read_content_by_extension(file)

def _write_file_unlocked(file: str, data) -> None:
    """
    仅供配置事务内部使用的无锁写入原语；调用方必须已持有对应 FileLock。
    """
    _atomic_write_content(file, data)

def read_file(file: str):
    """
    Read a file, support both .yaml and .json format.
    Return empty dict if file not exists.

    Args:
        file (str):

    Returns:
        dict, list:
    """
    folder = os.path.dirname(file)
    if not os.path.exists(folder):
        os.mkdir(folder)

    if not os.path.exists(file):
        return {}

    lock = FileLock(f"{file}.lock")
    with lock:
        logger.debug(f'read: {file}')
        return _read_file_unlocked(file)

def write_file(file: str, data):
    """
    Write data into a file, supports both .yaml and .json format.

    Args:
        file (str):
        data (dict, list):
    """
    folder = os.path.dirname(file)
    if not os.path.exists(folder):
        os.mkdir(folder)

    lock = FileLock(f"{file}.lock")
    with lock:
        logger.debug(f'write: {file}')
        _write_file_unlocked(file, data)


def deep_iter(data, depth=0, current_depth=1):
    """
    Iter a dictionary safely.

    Args:
        data (dict):
        depth (int): Maximum depth to iter
        current_depth (int):

    Returns:
        list: Key path
        Any:
    """
    if isinstance(data, dict) \
            and (depth and current_depth <= depth):
        for key, value in data.items():
            for child_path, child_value in deep_iter(value, depth=depth, current_depth=current_depth + 1):
                yield [key] + child_path, child_value
    else:
        yield [], data

def server_timezone() -> timedelta:
    return timedelta(hours=8)

def server_time_offset() -> timedelta:
    """
    To convert local time to server time:
        server_time = local_time + server_time_offset()
    To convert server time to local time:
        local_time = server_time - server_time_offset()
    """
    return datetime.now(timezone.utc).astimezone().utcoffset() - server_timezone()

def get_server_next_update(daily_trigger):
    """
    Args:
        daily_trigger (list[str], str): [ "00:00", "12:00", "18:00",]

    Returns:
        datetime.datetime
    """
    if isinstance(daily_trigger, str):
        daily_trigger = daily_trigger.replace(' ', '').split(',')

    diff = server_time_offset()
    local_now = datetime.now()
    trigger = []
    for t in daily_trigger:
        h, m = [int(x) for x in t.split(':')]
        future = local_now.replace(hour=h, minute=m, second=0, microsecond=0) + diff
        s = (future - local_now).total_seconds() % 86400
        future = local_now + timedelta(seconds=s)
        trigger.append(future)
    update = sorted(trigger)[0]
    return update


def convert_to_underscore(text: str) -> str:
    """
    大驼峰形式的字符串转换为下划线形式的字符串，并在数字前插入下划线。如果字符串中已经包含下划线，则会直接返回原始字符串。
    :param text:
    :return:
    """
    if '_' in text:
        # If text already contains underscore, assume it's in the correct format
        return text
    text = text.replace(' ', '')

    result = ''
    for i, char in enumerate(text):
        if char.isupper():
            if i > 0 and (text[i-1].islower() or (i < len(text) - 1 and text[i+1].islower())):
                # Insert underscore before uppercase letter, except at the beginning
                result += '_'
            result += char.lower()
        elif char.isdigit():
            if i > 0 and (text[i-1].isalpha() or (i < len(text) - 1 and text[i+1].isalpha())):
                # Insert underscore before digit, except at the beginning
                result += '_'
            result += char
        else:
            result += char

    return result

def get_server_last_update(daily_trigger):
    """
    Args:
        daily_trigger (list[str], str): [ "00:00", "12:00", "18:00",]

    Returns:
        datetime.datetime
    """
    if isinstance(daily_trigger, str):
        daily_trigger = daily_trigger.replace(' ', '').split(',')

    diff = server_time_offset()
    local_now = datetime.now()
    trigger = []
    for t in daily_trigger:
        h, m = [int(x) for x in t.split(':')]
        future = local_now.replace(hour=h, minute=m, second=0, microsecond=0) + diff
        s = (future - local_now).total_seconds() % 86400 - 86400
        future = local_now + timedelta(seconds=s)
        trigger.append(future)
    update = sorted(trigger)[-1]
    return update


def nearest_future(future, interval=120):
    """
    Get the neatest future time.
    Return the last one if two things will finish within `interval`.

    Args:
        future (list[datetime.datetime]):
        interval (int): Seconds

    Returns:
        datetime.datetime:
    """
    future = [datetime.fromisoformat(f) if isinstance(f, str) else f for f in future]
    future = sorted(future)
    next_run = future[0]
    for finish in future:
        if finish - next_run < timedelta(seconds=interval):
            next_run = finish

    return next_run


def parse_forbidden_ranges(text: str) -> list:
    """
    解析禁止运行时间段字符串，返回 (start_time, end_time) 元组列表。
    格式为 24 小时制、精度到分钟，多个区间用英文逗号分隔，例如 "01:00-02:00,02:30-04:00"。
    跨天区间（如 "23:00-01:00"）会被原样保留（start > end），由调用方处理跨天逻辑。

    :param text: 时间段配置字符串
    :return: [(time(1,0), time(2,0)), ...]，非法项会被跳过
    """
    ranges = []
    if not text:
        return ranges
    for segment in text.split(','):
        segment = segment.strip()
        if not segment:
            continue
        parts = segment.split('-')
        if len(parts) != 2:
            logger.warning(f'非法的禁止时间段配置，已跳过: {segment}')
            continue
        try:
            start = time.fromisoformat(parts[0].strip())
            end = time.fromisoformat(parts[1].strip())
        except ValueError:
            logger.warning(f'非法的禁止时间段配置，已跳过: {segment}')
            continue
        ranges.append((start, end))
    return ranges


def _build_forbidden_intervals(now: datetime, ranges: list) -> list:
    """
    把每个 (start_time, end_time) 段展开为围绕 now 的具体 datetime 区间。
    为覆盖跨天与合并场景，每个段都在 昨天/今天/明天 三个基准日各生成一份。
    普通段（start < end）区间在同一天内；跨天段（start > end）结束时间落到基准日的次日。

    :param now: 当前时间
    :param ranges: parse_forbidden_ranges 的输出
    :return: [(start_dt, end_dt), ...]
    """
    intervals = []
    base_days = [now.date() + timedelta(days=d) for d in (-1, 0, 1)]
    for start_t, end_t in ranges:
        if start_t == end_t:
            # 空区间，忽略
            continue
        for base in base_days:
            start_dt = datetime.combine(base, start_t)
            if start_t < end_t:
                end_dt = datetime.combine(base, end_t)
            else:
                # 跨天段：结束时间在次日
                end_dt = datetime.combine(base + timedelta(days=1), end_t)
            intervals.append((start_dt, end_dt))
    return intervals


def _merge_intervals(intervals: list) -> list:
    """
    合并重叠或首尾相接的 datetime 区间。相接（next.start == prev.end）也会合并，
    从而把 "01:00-02:00,02:00-03:00" 这类相邻段视为一个连续禁止区。

    :param intervals: [(start_dt, end_dt), ...]
    :return: 合并后的有序区间列表
    """
    intervals = sorted(intervals)
    merged = []
    for start_dt, end_dt in intervals:
        if merged and start_dt <= merged[-1][1]:
            # 与上一个区间重叠或相接，扩展其结束时间
            merged[-1] = (merged[-1][0], max(merged[-1][1], end_dt))
        else:
            merged.append((start_dt, end_dt))
    return merged


def forbidden_range_end(now: datetime, text: str):
    """
    判断 now 是否落在任一禁止运行时间段内，命中则返回该（合并后）区间结束对应的 datetime。
    区间为左闭右开 [start, end)：等于 end 视为不在区间内，避免跳到区间末尾后又被判定命中。
    相邻/重叠的段会先被合并成连续禁止区，跨天段（如 23:00-01:00）也在合并范围内，
    因此命中后返回的是整段连续禁止区的最终结束时刻（可能落在次日）。

    :param now: 当前时间
    :param text: 禁止时间段配置字符串
    :return: 命中则返回结束时刻的 datetime（含日期），否则返回 None
    """
    ranges = parse_forbidden_ranges(text)
    if not ranges:
        return None
    for start_dt, end_dt in _merge_intervals(_build_forbidden_intervals(now, ranges)):
        if start_dt <= now < end_dt:
            return end_dt.replace(second=0, microsecond=0)
    return None


def dict_to_kv(dictionary, allow_none=True):
    """
    Args:
        dictionary: Such as `{'path': 'Scheduler.ServerUpdate', 'value': True}`
        allow_none (bool):

    Returns:
        str: Such as `path='Scheduler.ServerUpdate', value=True`
    """
    return ', '.join([f'{k}={repr(v)}' for k, v in dictionary.items() if allow_none or v is not None])


def parse_tomorrow_server(server_update: time, delay_date: int = 1, float_seconds: int = 0) -> datetime:
    """
    获取明天的日期，给这个日期加上server_update的时间，返回datetime
    :param server_update:
    :param float_seconds: 浮动的秒数，可为正或负值
    :return:
    """
    if isinstance(server_update, str):
        server_update = time.fromisoformat(server_update)
    now = datetime.now()
    tomorrow = now + timedelta(days=delay_date)
    next_run = datetime.combine(tomorrow, server_update)
    
    # 应用浮动时间
    if float_seconds !=0:
        next_run += timedelta(seconds=float_seconds)

        # 确保时间在第二天内，不回退到前一天，不跨越到第三天（考虑任务时间，最早 00:00，最晚 23:50）
        start_of_tomorrow = datetime.combine(tomorrow, time.min)
        end_of_tomorrow = datetime.combine(tomorrow, time(hour=23, minute=50))
    
        if next_run < start_of_tomorrow:
            next_run = start_of_tomorrow
        elif next_run > end_of_tomorrow:
            next_run = end_of_tomorrow
    
    return next_run

def deep_get(d, keys, default=None):
    """
    Get values in dictionary safely.
    https://stackoverflow.com/questions/25833613/safe-method-to-get-value-of-nested-dictionary

    Args:
        d (dict):
        keys (str, list): Such as `Scheduler.NextRun.value`
        default: Default return if key not found.

    Returns:

    """
    if isinstance(keys, str):
        keys = keys.split('.')
    assert type(keys) is list
    if d is None:
        return default
    if not keys:
        return d
    return deep_get(d.get(keys[0]), keys[1:], default)


def deep_set(d, keys, value):
    """
    Set value into dictionary safely, imitating deep_get().
    """
    if isinstance(keys, str):
        keys = keys.split('.')
    assert type(keys) is list
    if not keys:
        return value
    if not isinstance(d, dict):
        d = {}
    d[keys[0]] = deep_set(d.get(keys[0], {}), keys[1:], value)
    return d


def deep_pop(d, keys, default=None):
    """
    Pop value from dictionary safely, imitating deep_get().
    """
    if isinstance(keys, str):
        keys = keys.split('.')
    assert type(keys) is list
    if not isinstance(d, dict):
        return default
    if not keys:
        return default
    elif len(keys) == 1:
        return d.pop(keys[0], default)
    return deep_pop(d.get(keys[0]), keys[1:], default)






if __name__ == '__main__':
    print(parse_tomorrow_server("09:01:00"))
