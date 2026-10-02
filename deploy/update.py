# This Python file uses the following encoding: utf-8
"""独立更新器入口：在干净进程里完成 git 拉取 + pip 依赖 + OCR 依赖对齐。

为什么必须独立于 server / gui / script 三个入口：

Windows 会锁定已加载的 onnxruntime_providers_shared.dll，任何进程都无法删除或
替换它；而 server.py / gui.py / script.py 都在入口最前面调用
preload_ocr_backend()（见 module/ocr/preload.py，为了抢在 pyzmq 之前占住 DLL
加载顺序），Python 又没有卸载已加载扩展 DLL 的手段。因此从 web 更新器发起的
OCR 换包必然撞 WinError 5 拒绝访问——它跑在 server 进程内，而 server 进程自己
就是锁的持有者，杀掉 OCR RPC 子进程也救不回来。

本模块进程不 import onnxruntime，也不 import zerorpc，是唯一能安全换 ORT 发行版
的更新路径。git 编排逻辑全部复用 module.server.updater.Updater.execute_pull()，
不在这里重复实现。

三种调用方式（供 OASX 更新器页直接 spawn，无需 server 存活）：

    python -m deploy.update                 执行更新，阶段进度打到 stdout
    python -m deploy.update --info          输出仓库信息 JSON（同 /home/update_info）
    python -m deploy.update --set-config     从 stdin 读 JSON 写 deploy.yaml

--info 的 JSON 字段与 /home/update_info 逐字段一致，因此前端的解析模型可直接复用。
"""
import argparse
import importlib
import json
import os
import subprocess
import sys

from deploy.logger import logger
from deploy.process import ProcessManager

# 首次安装自举阶段，最小 toolkit 只带 pip/requests/pywin32 等引导依赖，还没有 rich/filelock。
# module.server.updater 会连带拉起 module.logger(rich)（见 module/server/__init__.py、updater.py、
# module/base/retry.py）与 filelock，缺依赖时 import 直接裸崩 ModuleNotFoundError。
# 这里提前探测并给出可执行的提示，而不是让用户看到一条 traceback。
def _check_bootstrap_deps() -> None:
    missing = []
    for name in ('rich', 'filelock'):
        try:
            importlib.import_module(name)
        except ImportError:
            missing.append(name)
    if missing:
        print(
            '缺少更新器依赖：' + '、'.join(missing)
            + '\n如果是首次安装，请先运行 oas-backend.bat（或 python -m deploy.installer）完成依赖安装；'
              '已安装可手动补齐：toolkit\\python.exe -m pip install ' + ' '.join(missing),
            file=sys.stderr,
        )
        sys.exit(1)


_check_bootstrap_deps()

from module.server.updater import (Updater, Timeout, _update_progress,
                                  update_lock, validate_branch, validate_repository,
                                  UPDATE_LOCK_WAIT)

# JSON 结果行前缀。执行更新时 stdout 混着 git/pip 的原始输出，
# 前端靠这个前缀在噪声里定位机器可读的那一行。
JSON_PREFIX = 'OAS_JSON:'


def emit_json(payload) -> None:
    """把 JSON 打到 stdout，带前缀便于前端在混合输出里定位。"""
    print(f'{JSON_PREFIX}{json.dumps(payload, ensure_ascii=True)}', flush=True)


def stop_oas_processes(updater: Updater) -> None:
    """停止安装目录下所有 OAS 进程，释放它们持有的 onnxruntime DLL。

    换 ORT 发行版的前提。只影响安装目录内的 python/pythonw/oas 进程，
    且 ProcessManager 会排除本进程，不会自杀。
    """
    logger.hr('Stop OAS processes', 1)
    if not ProcessManager(file=updater.file).process_kill():
        raise RuntimeError('无法确认所有 OAS 进程已退出，已中止更新')


def align_pip(updater: Updater, prog) -> bool:
    """代码更新后对齐 pip 依赖。作为 execute_pull 的 before_ocr 钩子调用。

    Args:
        updater: 更新器实例，已继承 PipManager 提供 pip_install。
        prog: 更新进度对象，用于把阶段与失败原因透出。

    Returns:
        bool: 是否对齐成功。
    """
    prog.set_step('对齐 pip 依赖')
    try:
        # pip_install 自己会检查 InstallDependencies 开关；
        # 走 DeployConfig.execute，失败时抛 ExecutionError
        updater.pip_install()
    except Exception as e:
        prog.append(f'阶段「对齐 pip 依赖」失败：{e}')
        return False
    return True


def run_info() -> int:
    """输出仓库信息 JSON，字段与 /home/update_info 一致。

    供 OASX 更新器页在 server 未启动时填充分支表单与 commit 对比表。
    与 home_router.update_info 保持同一组字段；任何一方新增字段都要同步。
    """
    try:
        # --info 也会 fetch，必须和更新/写配置共用跨进程锁。
        with update_lock():
            updater = Updater()
            emit_json({
                'is_update': updater.check_update(),
                'fetch_ok': updater.fetch_ok,
                'branch': updater.current_branch(),
                'repository': updater.Repository,
                'current_commit': updater.current_commit(),
                'latest_commit': updater.latest_commit(),
                'commit': updater.get_commit(n=15),
            })
        return 0
    except Timeout:
        emit_json({'error': '已有其它 OAS 仓库操作正在运行'})
        return 1
    except Exception as e:
        logger.info(f'读取仓库信息失败：{e}')
        emit_json({'error': str(e)})
        return 1


def run_set_config() -> int:
    """从 stdin 读 JSON 写入 deploy.yaml，并与其它仓库操作互斥。"""
    try:
        payload = json.loads(sys.stdin.read() or '{}')
    except ValueError as e:
        emit_json({'error': f'stdin 不是合法 JSON：{e}'})
        return 1

    repository = str(payload.get('repository') or '').strip()
    branch = str(payload.get('branch') or '').strip()
    try:
        checked_repository = validate_repository(repository) if repository else None
        checked_branch = validate_branch(branch) if branch else None
        # 写配置等非零超时：--info 的 fetch 也持这把锁，立即失败会让用户改不了配置。
        with update_lock(timeout=UPDATE_LOCK_WAIT):
            updater = Updater()
            # 先完整校验，再落盘任何字段，避免部分配置更新。
            if checked_repository:
                # 先更新 remote，成功后才写 deploy.yaml，避免两者不一致。
                result = subprocess.run(
                    [updater.git, 'remote', 'set-url', 'origin', checked_repository],
                    cwd=updater.root_filepath,
                    capture_output=True,
                    encoding='utf-8',
                    errors='replace',
                    creationflags=subprocess.CREATE_NO_WINDOW if sys.platform.startswith('win') else 0,
                )
                if result.stdout:
                    logger.info(result.stdout.rstrip())
                if result.stderr:
                    logger.warning(result.stderr.rstrip())
                if result.returncode != 0:
                    emit_json({'error': 'git remote set-url 失败'})
                    return 1
                if checked_repository != updater.Repository:
                    updater.Repository = checked_repository
            if checked_branch:
                updater.Branch = checked_branch
            emit_json({'repository': updater.Repository, 'branch': updater.Branch})
            return 0
    except Timeout:
        emit_json({'error': '已有其它 OAS 仓库操作正在运行'})
        return 1
    except ValueError as e:
        emit_json({'error': str(e)})
        return 1
    except Exception as e:
        emit_json({'error': str(e)})
        return 1


def run_update() -> int:
    """执行完整更新流程，阶段进度实时打到 stdout。"""
    ok = False
    try:
        updater = Updater()
        # 等非零超时而非立即失败：--info / web 的 /update_info 也持这把锁且内含
        # git fetch，立即失败会让「刚看完分支信息就点更新」必被拒。
        with update_lock(updater.file, timeout=UPDATE_LOCK_WAIT):
            # 监听器必须在停止其它 OAS 进程前注册，保证用户能看到完整阶段。
            _update_progress.set_listener(logger.info)
            try:
                logger.hr('OAS Updater', 0)
                stop_oas_processes(updater)
                ok = updater._execute_pull_locked(
                    before_ocr=lambda prog: align_pip(updater, prog))
            finally:
                _update_progress.set_listener(None)
    except Timeout:
        _update_progress.reject('已有其它 OAS 仓库操作正在运行，拒绝并发更新')
        emit_json({'ok': False, 'status': 'rejected', 'step': '', 'branch': ''})
        return 1
    except Exception as e:
        logger.exception(e)
        _update_progress.finish(False)

    snapshot = _update_progress.snapshot()
    logger.hr('Result', 1)
    if ok:
        logger.info(f'更新完成，当前分支 {snapshot["branch"]}')
    else:
        logger.info(f'更新未完成（status={snapshot["status"]}），中断在阶段：'
                    f'{snapshot["step"] or "未开始"}')
    emit_json({
        'ok': ok,
        'status': snapshot['status'],
        'step': snapshot['step'],
        'branch': snapshot['branch'],
    })
    return 0 if ok else 1


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description='OAS 独立更新器')
    group = parser.add_mutually_exclusive_group()
    group.add_argument('--info', action='store_true',
                       help='输出仓库信息 JSON，不做任何改动')
    group.add_argument('--set-config', action='store_true',
                       help='从 stdin 读 JSON 写入 deploy.yaml 的 Repository/Branch')
    args = parser.parse_args(argv)

    if args.info:
        return run_info()
    if args.set_config:
        return run_set_config()
    return run_update()


if __name__ == '__main__':
    sys.exit(main())
