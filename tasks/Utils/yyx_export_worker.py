"""MuMu 12 的独立采集进程：root + 回环 Frida 服务 + 已选定设备的 ADB 转发。"""
import hashlib
import json
import os
import re
import shlex
import uuid

from adbutils import AdbClient
from filelock import FileLock

from tasks.Utils.yyx_export import FRIDA_VERSION, ROOT, SERVER_PATH, YyxExportError


# 使用独立名称和端口，避免影响大神签到或用户自己的 Frida 服务。
REMOTE_SERVER = '/data/local/tmp/oas_yyx_server_17_6_2'
REMOTE_PORT = 27443
RPC_SOURCE = '''
// 采集仅在持有 Python GIL 时执行，退出时成对释放；不改游戏主模块的变量。
rpc.exports.collect = function(code) {
    if (Process.arch !== 'x64') throw new Error('仅支持 x86_64 游戏进程');
    const module = Process.getModuleByName('libclient.so');
    const ensure = new NativeFunction(module.getExportByName('PyGILState_Ensure'), 'int', []);
    const release = new NativeFunction(module.getExportByName('PyGILState_Release'), 'void', ['int']);
    const run = new NativeFunction(module.getExportByName('PyRun_SimpleStringFlags'), 'int', ['pointer', 'pointer']);
    const text = Memory.allocUtf8String(code);
    const gil = ensure();
    try { return run(text, ptr(0)); }
    finally { release(gil); }
};
'''


def _root_shell(device, args, timeout=10):
    """su 只接收已引用的参数列表，游戏包名和临时路径不能变成 shell 指令。"""
    return device.shell(['su', '-c', shlex.join(args)], timeout=timeout)


def _game_process(device, package):
    """auto 仅接受唯一运行中的阴阳师主进程，不误选推送服务或其他游戏。"""
    if package == 'auto':
        packages = [line.removeprefix('package:').strip()
                    for line in device.shell(['pm', 'list', 'packages'], timeout=10).splitlines()]
        packages = [name for name in packages
                    if name.startswith(('com.netease.onmyoji', 'com.netease.yys'))]
    else:
        packages = [package]
    running = []
    for name in packages:
        if not re.fullmatch(r'[A-Za-z0-9_]+(?:\.[A-Za-z0-9_]+)+', name):
            raise YyxExportError('游戏包名格式无效')
        pid = device.shell(['pidof', name], timeout=5).strip()
        if pid:
            if not pid.isdigit():
                raise YyxExportError('发现多个游戏进程，无法确定导出目标')
            running.append((name, int(pid)))
    if len(running) != 1:
        raise YyxExportError('需要唯一运行中的阴阳师主进程，请先登录角色')
    return running[0]


def _python_program(output):
    """临时结果写游戏自身缓存目录；采集脚本在独立命名空间运行。"""
    source = (ROOT / 'tasks' / 'Utils' / 'yyx_client.py').read_text(encoding='utf-8')
    program = source + '\n' + f'''
import json
import traceback
try:
    result = collect_snapshot()
    encoded = json.dumps(result, ensure_ascii=False, allow_nan=False)
except Exception:
    encoded = json.dumps({{"error": traceback.format_exc()}}, ensure_ascii=False)
with open({output!r}, 'w', encoding='utf-8') as stream:
    stream.write(encoded)
'''
    return f'exec(compile({program!r}, "oas_yyx_client.py", "exec"), {{"__name__": "__oas_yyx_export__"}})'


def _remove_forward(client, serial, port):
    """当前 adbutils 没有公开 remove 方法，按 ADB 协议只移除本次端口。"""
    with client._connect(timeout=5) as connection:
        connection.send_command(f'host-serial:{serial}:killforward:tcp:{port}')
        # ADB 分别确认服务选择与转发操作，两次状态都需检查。
        connection.check_okay()
        connection.check_okay()


def _create_forward(client, serial):
    """由 ADB 原子分配本次端口，不复用或删除其他工具已有的转发。"""
    with client._connect(timeout=5) as connection:
        connection.send_command(f'host-serial:{serial}:forward:tcp:0;tcp:{REMOTE_PORT}')
        # 首个 OKAY 是服务选择，第二个才是转发成功，之后才有长度前缀端口号。
        connection.check_okay()
        connection.check_okay()
        port = int(connection.read_string_block())
        if not 0 < port < 65536:
            raise RuntimeError('ADB 返回了无效的转发端口')
        return port


def _collect_locked(device, client, serial, package):
    """建立连接、读取快照并释放本次资源；全程不 kill ADB 或游戏进程。"""
    import frida

    if frida.__version__ != FRIDA_VERSION:
        raise YyxExportError(f'账号导出要求 Frida {FRIDA_VERSION}，请使用项目内置 Python')
    if not SERVER_PATH.is_file():
        raise YyxExportError('缺少账号导出组件，请运行 toolkit/python.exe -m dev_tools.yyx_export --install')
    if 'uid=0(' not in _root_shell(device, ['id']):
        raise YyxExportError('当前 MuMu 未开启 root，无法读取账号数据')
    package, pid = _game_process(device, package)
    output = f'/data/data/{package}/cache/oas_yyx_{uuid.uuid4().hex}.json'
    manager = frida.get_device_manager()
    endpoint = None
    session = None
    server_pid = None
    port = None
    try:
        # 已有服务可能属于仍在运行的工具或上次超时现场，拒绝复用或终止它。
        existing = _root_shell(device, ['pidof', REMOTE_SERVER.rsplit('/', 1)[1]]).strip()
        if existing:
            # 前次采集可能仍在游戏内执行，此时必须接管，不能继续切号。
            raise RuntimeError('账号导出服务仍在运行，请检查前次导出是否已结束')
        # 保留设备端可执行文件缓存，多账号循环不重复传输百兆工具。
        version = device.shell([REMOTE_SERVER, '--version'], timeout=5).strip()
        if version != FRIDA_VERSION:
            device.sync.push(str(SERVER_PATH), REMOTE_SERVER, mode=0o755)
        status = _root_shell(device, [REMOTE_SERVER, '--daemonize', f'--listen=127.0.0.1:{REMOTE_PORT}'])
        server_pid = _root_shell(device, ['pidof', REMOTE_SERVER.rsplit('/', 1)[1]]).strip()
        if not server_pid.isdigit():
            server_pid = None
            raise YyxExportError('无法启动账号导出服务：' + status[:200])
        port = _create_forward(client, serial)
        endpoint = f'127.0.0.1:{port}'
        remote = manager.add_remote_device(endpoint)
        session = remote.attach(pid)
        script = session.create_script(RPC_SOURCE)
        script.load()
        if script.exports_sync.collect(_python_program(output)) != 0:
            raise YyxExportError('游戏 Python 拒绝执行账号采集脚本')
        raw = _root_shell(device, ['cat', output], timeout=30)
        try:
            value = json.loads(raw)
        except ValueError as exc:
            raise YyxExportError('游戏未生成有效的账号快照') from exc
        if isinstance(value, dict) and 'error' in value:
            raise YyxExportError('游戏数据接口不兼容：' + str(value['error'])[-800:])
        return raw
    finally:
        # 每项独立清理；断连等失败意味着状态不确定，不能返回成功后继续切号。
        actions = []
        if session is not None:
            actions.append(session.detach)
        if endpoint is not None:
            actions.append(lambda: manager.remove_remote_device(endpoint))
        if port is not None:
            actions.append(lambda: _remove_forward(client, serial, port))
        actions.append(lambda: _root_shell(device, ['rm', '-f', output], timeout=5))
        if server_pid is not None:
            actions.append(lambda: _root_shell(device, ['kill', '-TERM', server_pid], timeout=5))
        cleanup_error = None
        for action in actions:
            try:
                action()
            except Exception as exc:
                cleanup_error = cleanup_error or exc
        if cleanup_error is not None:
            raise RuntimeError(f'账号导出资源清理失败：{cleanup_error}') from cleanup_error


def collect_from_device(serial, package):
    """按设备加文件锁，防止多个 OAS 实例同时使用同一设备导出。"""
    lock_dir = ROOT / 'config' / 'tasks_config' / 'yyx_locks'
    lock_dir.mkdir(parents=True, exist_ok=True)
    key = hashlib.sha256(serial.encode('utf-8')).hexdigest()
    client = AdbClient(host='127.0.0.1', port=int(os.environ.get('ANDROID_ADB_SERVER_PORT', '5037')))
    device = client.device(serial=serial)
    with FileLock(str(lock_dir / f'{key}.lock'), timeout=0):
        return _collect_locked(device, client, serial, package)
