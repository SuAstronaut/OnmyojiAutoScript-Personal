# This Python file uses the following encoding: utf-8
# @author runhey
# github https://github.com/runhey
import json
import threading
from fastapi import APIRouter, Body
from pathlib import Path

from module.config.utils import write_file
from module.logger import logger
from module.ocr.rpc import shutdown_ocr_server
from module.server.main_manager import MainManager
from module.server.updater import (Updater, _update_progress, Timeout,
                                  update_lock, validate_branch, validate_repository,
                                  UPDATE_LOCK_WAIT)
from module.server.i18n import I18n

home_app = APIRouter(
    prefix="/home",
    tags=["home"],
)


@home_app.get('/test')
async def home_test():
    return {'message': 'test'}


#  gcc -Wall -pedantic -shared -fPIC -o group_work.so group_work.c -lwiringPi
@home_app.get('/home_menu')
async def home_menu():
    return {'Home': [], 'Updater': [], 'Tool': []}


@home_app.post('/notify_test')
async def notify_test(setting: str, title: str, content: str):
    from module.notify.notify import Notifier
    try:
        notifier = Notifier(setting, True)
        if notifier.push(title=title, content=content):
            del notifier
            return True
        else:
            del notifier
            return False
    except Exception as e:
        logger.exception(e)
        return str(e)


@home_app.get('/kill_server')
async def kill_server():
    shutdown_ocr_server()
    MainManager.signal_kill_server = True
    return 'success'


@home_app.get('/update_info')
def update_info():
    # 同步 def：FastAPI 会放到线程池执行，git fetch 耗时不会阻塞事件循环。
    # 即便连不上 GitHub，也不至于拖住 /update_progress 等其他接口。
    try:
        with update_lock():
            updater = Updater()
            result = {'is_update': updater.check_update(),
                      'fetch_ok': updater.fetch_ok,
                      'branch': updater.current_branch(),
                      'repository': updater.Repository,
                      'current_commit': updater.current_commit(),
                      'latest_commit': updater.latest_commit(),
                      'commit': updater.get_commit(n=15),
                      }
            return result
    except Timeout:
        logger.warning('仓库更新操作正在进行，暂不读取更新信息')
        return None
    except Exception as e:
        logger.error(e)
        return None


@home_app.get('/execute_update')
async def execute_update():
    # 原子领取进程内执行权，避免两个请求同时通过状态检查后各启一个线程。
    if not _update_progress.try_start():
        return '更新正在后台运行，请等待本次更新完成或失败后再重试'
    try:
        updater = Updater()

        def _run():
            # 线程级兜底：任何未捕获异常都不让状态卡死在 running，
            # 否则前端永远无法再次触发更新（恢复入口被堵死）。
            try:
                updater.execute_pull()
            except Exception as e:
                logger.error(e)
                _update_progress.finish(False)

        threading.Thread(target=_run, daemon=True).start()
    except Exception as e:
        logger.error(e)
        _update_progress.finish(False)
    return '更新已在后台开始，可通过 /update_progress 查看进度'


@home_app.get('/update_progress')
async def update_progress():
    return _update_progress.snapshot()


@home_app.post('/update_config')
async def update_config(branch: str = None, repository: str = None):
    # 写回 deploy.yaml（DeployConfig.__setattr__ 自动落盘）；Repository 同步 git remote
    try:
        # 写配置同样等非零超时：/update_info 的 fetch 也持这把锁，
        # 立即失败会让用户刚打开页面就改不了 Repository/Branch。
        with update_lock(timeout=UPDATE_LOCK_WAIT):
            updater = Updater()
            # 先完整校验输入，再落盘任何字段，避免 repository 已写入而 branch 校验失败。
            checked_repository = validate_repository(repository) if repository else None
            checked_branch = validate_branch(branch) if branch else None
            old_repository = updater.Repository
            old_branch = updater.Branch
            try:
                if checked_repository:
                    # 先成功更新 remote，再写 deploy.yaml，避免两边指向不同仓库。
                    if not updater.execute_stream(
                            f'"{updater.git}" remote set-url origin {checked_repository}'):
                        return {'error': 'git remote set-url 失败'}
                    if checked_repository != old_repository:
                        updater.Repository = checked_repository
                if checked_branch:
                    updater.Branch = checked_branch
            except Exception:
                # 配置落盘失败时尽力恢复旧配置和旧 remote，避免半更新状态。
                try:
                    updater.Repository = old_repository
                    updater.Branch = old_branch
                    updater.execute_stream(
                        f'"{updater.git}" remote set-url origin {old_repository}')
                except Exception as rollback_error:
                    logger.exception(f'回滚仓库配置失败：{rollback_error}')
                return {'error': '仓库配置写入失败，已尝试回滚'}
            return {'repository': updater.Repository, 'branch': updater.Branch}
    except ValueError as e:
        return {'error': str(e)}
    except Timeout:
        return {'error': '更新正在进行，暂时不能修改仓库配置'}


@home_app.put('/chinese_translate')
async def chinese_translate(data: dict = Body(...)):
    try:
        I18n.save_zh_cn(data)
    except Exception as e:
        logger.error(e)
    return True


@home_app.get('/additional_translate')
async def additional_translate() -> dict:
    try:
        data = I18n.load_additions()
        return data
    except Exception as e:
        logger.error(e)
    # 异常时也保证响应形状稳定（两语言字段齐全），避免前端解析中断
    return {'en-US': {}, 'zh-CN': {}}
