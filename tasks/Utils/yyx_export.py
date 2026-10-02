"""通过当前 OAS 的 ADB 设备导出 YYX 快照；原生采集放在有超时的独立进程。"""
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
from datetime import datetime

from tasks.Utils.yyx_snapshot import parse_snapshot


ROOT = Path(__file__).resolve().parents[2]
FRIDA_VERSION = '17.6.2'  # 与项目 requirements.txt 保持一致。
SERVER_NAME = f'frida-server-{FRIDA_VERSION}-android-x86_64'
SERVER_PATH = ROOT / 'toolkit' / 'yyx_export' / SERVER_NAME
EXPORT_ROOT = ROOT / 'config' / 'tasks_config' / 'yyx_exports'
WORKER_TIMEOUT = 90


class YyxExportError(RuntimeError):
    """导出没有生成可用文件，可以保留进度并在下一次重试。"""


def _snapshot_identity(account_ctx):
    """只导出角色名、区服和登录账号，避免临时配置名影响下游角色关联。

    三项身份必须齐全才写入：缺项时返回 None，不用半截身份冒充完整来源。
    单账号直跑没有账号上下文（或只填了角色名），返回 None，导出格式与旧版一致。
    """
    context = account_ctx or {}
    character = context.get('char')
    server = context.get('svr')
    account = context.get('acc')
    if not all(isinstance(value, str) and value.strip()
               for value in (character, server, account)):
        return None
    return {
        'character': character,
        'svr': server,
        'account': account,
    }


def _write_snapshot(data, output_root, identity=None):
    """同一角色保存最新快照，原子替换保证失败时上一份仍然可用。"""
    # 所有 OAS 配置共用导出目录，同一角色换配置后仍覆盖原文件。
    directory = Path(output_root)
    directory.mkdir(parents=True, exist_ok=True)
    player = data['player']
    # 文件名只用稳定的区服与角色 ID，角色改名后仍覆盖同一份快照。
    path = directory / f'yyx_{player["server_id"]}_{player["id"]}.json'
    # 来源身份附加在数据层：采集协议是按位置校验的数组，不能被这个键撑开。
    snapshot = {
        'timestamp': datetime.now().astimezone().isoformat(),
        'version': '2.0.0',
        'data': data if identity is None else {**data, 'oas_identity': identity},
    }
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
            mode='w', encoding='utf-8', dir=directory, suffix='.tmp', delete=False,
        ) as stream:
            temporary = Path(stream.name)
            json.dump(snapshot, stream, ensure_ascii=False, allow_nan=False)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    return path


def export_account_snapshot(device, config_name, account_ctx=None, *, output_root=EXPORT_ROOT):
    """复用已选设备导出当前登录角色；account_ctx 决定写入的来源身份。

    :param config_name: 保留现有调用接口；配置实例名不参与来源身份。
    :param account_ctx: 多账号运行注入的账号上下文（acc/char/svr）；
                        单账号直跑时为空，导出文件不写来源身份。

    这里不再按角色名二次核对切号目标：切号阶段的 find_character_index 已用统一的
    异体字归一化（见 character_match.normalize_name）确认过目标，而本函数若再按
    字面比较，就会把「瑶/瑤」这类游戏内异体字判成切错号，让整份数据落不了盘。
    文件名与内容都取自采集结果本身，导出错号也不会覆盖目标角色的文件。
    """
    serial = str(device.serial)
    if serial in ('auto', 'desktop', '') or not re.fullmatch(r'[\w.\-:\[\]]+', serial):
        raise YyxExportError('账号导出需要已连接的 Android ADB 设备')
    package = str(getattr(device, 'package', 'auto'))
    command = [sys.executable, '-m', 'tasks.Utils.yyx_export', '--worker', serial, package]
    try:
        # 独立进程隔离 Frida 与调度器的 gevent 环境；不启动其他 ADB server。
        result = subprocess.run(
            command, cwd=ROOT, capture_output=True, timeout=WORKER_TIMEOUT,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0,
        )
    except subprocess.TimeoutExpired as exc:
        # 原生调用超时后游戏状态不确定，禁止继续切号，交给现有人工接管机制。
        from module.exception import RequestHumanTakeover
        raise RequestHumanTakeover('账号导出超时，请检查游戏状态后再继续') from exc
    except OSError as exc:
        raise YyxExportError(f'无法启动账号导出进程：{exc}') from exc
    if result.returncode:
        message = result.stderr.decode('utf-8', errors='replace').strip()
        if result.returncode != 1:
            # ADB/Frida 中断与原生进程崩溃意味着状态不确定，不能当普通业务失败继续切号。
            from module.exception import RequestHumanTakeover
            raise RequestHumanTakeover('账号采集连接异常，请检查游戏状态：' + message[:1000])
        raise YyxExportError(message[:1000] or '账号导出进程异常结束')
    try:
        data = parse_snapshot(result.stdout)
        player = data['player']
        if not player['name'] or player['id'] <= 0 or player['server_id'] <= 0:
            raise YyxExportError('导出结果没有有效角色身份')
        path = _write_snapshot(data, output_root,
                               _snapshot_identity(account_ctx))
    except (ValueError, OSError) as exc:
        raise YyxExportError(f'账号快照校验或保存失败：{exc}') from exc
    return {
        'path': str(path), 'player': player,
        'counts': {key: len(value) for key, value in data.items() if isinstance(value, list)},
    }


def _main():
    """工作进程只在 stdout 输出原始快照，诊断写 stderr，避免混入导出数据。"""
    try:
        if len(sys.argv) != 4 or sys.argv[1] != '--worker':
            raise YyxExportError('请使用 python -m dev_tools.yyx_export --config 配置名')
        from tasks.Utils.yyx_export_worker import collect_from_device
        raw = collect_from_device(sys.argv[2], sys.argv[3])
        sys.stdout.buffer.write(raw.encode('utf-8'))
    except Exception as exc:
        # 不回显原始账号数据，只保留异常类别与定位信息。
        message = f'{type(exc).__name__}: {exc}'
        sys.stderr.buffer.write(message[:1000].encode('utf-8', errors='replace'))
        # -m 启动时本模块名为 __main__；worker 导入的是规范模块名，要识别同一异常的两份类。
        from tasks.Utils.yyx_export import YyxExportError as WorkerExportError
        return 1 if isinstance(exc, (YyxExportError, WorkerExportError)) else 2
    return 0


if __name__ == '__main__':
    raise SystemExit(_main())
