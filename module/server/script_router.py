# This Python file uses the following encoding: utf-8
# @author runhey
# github https://github.com/runhey
import asyncio
import copy
import json
from urllib.parse import quote

from fastapi import APIRouter, File, Form, HTTPException, UploadFile
from fastapi.responses import Response, StreamingResponse
from fastapi import WebSocket, WebSocketDisconnect
from datetime import datetime
from filelock import Timeout
from module.config.config_generation import (
    ConfigGenerationError,
    ConfigIdentityConflictError,
    ConfigIdentityNameError,
    ConfigIdentityNotFoundError,
)
from module.config.config_menu import ConfigMenu
from module.config.config_operations import MISSING
from module.config.config_store import (
    ConfigGenerationMismatchError,
    ConfigJsonError as StoreConfigJsonError,
    ConfigNotFoundError,
)
from module.config.config_validation import ConfigValidationError
from module.config.utils import convert_to_underscore
from module.server.config_manager import (
    ConfigAlreadyExistsError,
    ConfigJsonError,
    ConfigNameError,
    ConfigNotFoundError as ManagerConfigNotFoundError,
    ConfigTaskError,
    ConfigValidationError as ManagerConfigValidationError,
)

from module.logger import logger
from module.server.main_manager import mm
from module.server.script_process import (
    ScriptProcess,
    ScriptStartupTimeoutError,
    ScriptState,
)

from tasks.Component.config_base import TimeDelta


script_app = APIRouter()


@script_app.get('/test')
async def script_test():
    return 'success'

@script_app.get('/script_menu')
async def script_menu():
    # 菜单是静态结构，不应因 template 配置校验失败而让前端任务列表整体不可用
    return ConfigMenu().gui_menu_list
# ----------------------------------   配置文件管理   ----------------------------------
@script_app.get('/config_list')
async def config_list():
    return mm.all_script_files()

@script_app.post('/config_copy')
async def config_copy(file: str, template: str = 'template'):
    """复制配置并把生命周期失败映射为稳定 HTTP 状态，禁止失败返回 200。"""
    try:
        mm.copy(file, template)
        return mm.all_script_files()
    except (ConfigNameError, ConfigIdentityNameError) as e:
        raise HTTPException(status_code=400, detail=str(e))
    except (
        ManagerConfigNotFoundError,
        ConfigIdentityNotFoundError,
        ConfigNotFoundError,
    ) as e:
        raise HTTPException(status_code=404, detail=str(e))
    except (ConfigAlreadyExistsError, ConfigIdentityConflictError) as e:
        raise HTTPException(status_code=409, detail=str(e))
    except (Timeout, TimeoutError) as e:
        raise HTTPException(status_code=503, detail=f'Config lock timeout: {e}')
    except (ConfigGenerationError, ConfigValidationError, OSError) as e:
        # 身份损坏、源配置损坏或文件系统失败均属于服务端错误。
        raise HTTPException(status_code=500, detail=f'Config copy failed: {e}')

@script_app.get('/config_new_name')
async def config_new_name():
    return mm.generate_script_name()

@script_app.get('/config_all')
async def config_all():
    return mm.all_json_file()


@script_app.post('/config/import')
async def config_import(name: str = Form(...), file: UploadFile = File(...)):
    """导入脚本配置文件，上传文件名不会作为落盘名称。"""
    try:
        raw = await file.read()
        data = json.loads(raw.decode('utf-8'))
        config_name = mm.import_config(name, data)
        await mm.add_script_file(config_name)
        return {'name': config_name, 'file': f'{config_name}.json'}
    except ConfigAlreadyExistsError as e:
        raise HTTPException(status_code=409, detail=str(e))
    except ManagerConfigValidationError as e:
        raise HTTPException(status_code=400, detail={'message': str(e), 'fields': e.fields})
    except (UnicodeDecodeError, json.JSONDecodeError) as e:
        raise HTTPException(status_code=400, detail=f'Invalid JSON file: {e}')
    except (ConfigNameError, ConfigJsonError) as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Timeout as e:
        raise HTTPException(status_code=503, detail=f'Config lock timeout: {e}')
    except (ConfigGenerationError, OSError) as e:
        # 身份损坏、migration/recovery 失败或文件系统错误保留为服务端错误。
        raise HTTPException(status_code=500, detail=f'Config import failed: {e}')


@script_app.get('/config/export')
async def config_export(name: str):
    """导出脱敏后的脚本配置文件。"""
    try:
        config_name, data = mm.load_config_for_export(name)
        redacted = mm.redact_config(data)
        content = json.dumps(redacted, indent=2, ensure_ascii=False, sort_keys=False, default=str)
        filename = f'{config_name}.json'
        return Response(
            content=content.encode('utf-8'),
            media_type='application/json; charset=utf-8',
            headers={
                'Content-Disposition': f'attachment; filename="{filename}"; filename*=UTF-8\'\'{quote(filename)}',
                'Cache-Control': 'no-store',
            },
        )
    except ConfigIdentityNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except ConfigNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except (ConfigNameError, ConfigJsonError) as e:
        raise HTTPException(status_code=400, detail=str(e))


@script_app.post('/config/task/import')
async def config_task_import(
    config_name: str = Form(...),
    task_name: str = Form(...),
    json_text: str | None = Form(None),
    file: UploadFile | None = File(None),
):
    """导入单个任务配置 JSON，内容必须是 {"task_key": {...}}。"""
    try:
        file_content = await file.read() if file is not None else None
        data = mm.parse_task_json_source(json_text=json_text, file_content=file_content)
        normalized_config_name, task_key = mm.import_task_config(config_name, task_name, data)
        return {
            'config_name': normalized_config_name,
            'task_name': task_key,
            'file': f'{normalized_config_name}.json',
            'updated': True,
        }
    except (
        ManagerConfigNotFoundError,
        ConfigIdentityNotFoundError,
        ConfigNotFoundError,
    ) as e:
        raise HTTPException(status_code=404, detail=str(e))
    except ManagerConfigValidationError as e:
        raise HTTPException(status_code=400, detail={'message': str(e), 'fields': e.fields})
    except ConfigGenerationMismatchError as e:
        raise HTTPException(status_code=409, detail=str(e))
    except Timeout as e:
        raise HTTPException(status_code=503, detail=f'Config lock timeout: {e}')
    except OSError as e:
        raise HTTPException(status_code=500, detail=f'Config write failed: {e}')
    except (ConfigNameError, ConfigJsonError, ConfigTaskError) as e:
        raise HTTPException(status_code=400, detail=str(e))


@script_app.get('/config/task/export')
async def config_task_export(config_name: str, task_name: str):
    """导出脱敏后的单个任务配置 JSON 文件。"""
    try:
        normalized_name, task_key, data = mm.load_task_for_export(config_name, task_name)
        content = json.dumps(data, indent=2, ensure_ascii=False, sort_keys=False, default=str)
        filename = f'{normalized_name}-{task_key}.json'
        return Response(
            content=content.encode('utf-8'),
            media_type='application/json; charset=utf-8',
            headers={
                'Content-Disposition': f'attachment; filename="{filename}"; filename*=UTF-8\'\'{quote(filename)}',
                'Cache-Control': 'no-store',
            },
        )
    except ConfigIdentityNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except ConfigNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except (ConfigNameError, ConfigJsonError, ConfigTaskError) as e:
        raise HTTPException(status_code=400, detail=str(e))


@script_app.get('/config/task/copy-json')
async def config_task_copy_json(config_name: str, task_name: str):
    """复制单个任务配置 JSON，返回未脱敏普通 JSON。"""
    try:
        _, _, data = mm.load_task_for_transfer(config_name, task_name)
        return data
    except ConfigIdentityNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except ConfigNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except (ConfigNameError, ConfigJsonError, ConfigTaskError) as e:
        raise HTTPException(status_code=400, detail=str(e))


@script_app.put('/config')
async def config_rename(old_name: str = '', new_name: str = ''):
    """
    update config name
    :param old_name: old config name
    :param new_name: new config name
    :return: True or False
    """
    if old_name == new_name or new_name == '':
        return False
    try:
        # stop 源实例 → Store 提交 → 重建注册；禁止直接调用 coroutine 而不 await
        await mm.rename_config(old_name, new_name)
    except ConfigIdentityNameError as e:
        raise HTTPException(status_code=400, detail=f'Rename failed: {e}')
    except ConfigIdentityNotFoundError as e:
        raise HTTPException(status_code=404, detail=f'Rename failed: {e}')
    except ConfigIdentityConflictError as e:
        raise HTTPException(status_code=409, detail=f'Rename failed: {e}')
    except Timeout as e:
        raise HTTPException(status_code=503, detail=f'Config lock timeout: {e}')
    except (ConfigGenerationError, OSError) as e:
        raise HTTPException(status_code=500, detail=f'Rename failed: {e}')
    return True


@script_app.delete('/config')
async def config_delete(name: str = ''):
    """
    delete config file
    :param name: config name
    :return: True or False
    """
    if name == '' or name == 'template':
        raise HTTPException(status_code=400, detail='Delete failed')
    try:
        await mm.delete_config(name)
    except ConfigIdentityNameError as e:
        raise HTTPException(status_code=400, detail=f'Delete failed: {e}')
    except ConfigIdentityNotFoundError as e:
        raise HTTPException(status_code=404, detail=f'Delete failed: {e}')
    except Timeout as e:
        raise HTTPException(status_code=503, detail=f'Config lock timeout: {e}')
    except (ConfigGenerationError, OSError) as e:
        raise HTTPException(status_code=500, detail=f'Delete failed: {e}')
    return True


@script_app.put('/config/task/copy')
async def task_copy(task_name: str, dest_config_name: str, source_config_name: str):
    if dest_config_name not in mm.script_process or source_config_name not in mm.script_process:
        return False
    task_key = convert_to_underscore(task_name)
    try:
        source_canonical = mm.store.load_canonical_snapshot(source_config_name)
        dest_loaded = mm.store.load(dest_config_name)
    except ConfigIdentityNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except ConfigNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    if task_key not in source_canonical:
        return False
    expected = dest_loaded.canonical.get(task_key, MISSING)
    try:
        result = mm.store.replace_subtree(
            dest_config_name,
            (task_key,),
            expected,
            copy.deepcopy(source_canonical[task_key]),
            dest_loaded.generation,
        )
    except ConfigValidationError as e:
        raise HTTPException(status_code=400, detail=f'Task copy invalid: {e}')
    except ConfigIdentityNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except ConfigNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except ConfigGenerationMismatchError as e:
        raise HTTPException(status_code=409, detail=str(e))
    except Timeout as e:
        raise HTTPException(status_code=503, detail=f'Config lock timeout: {e}')

    # 成功后锁外投递 config_changed 事件，与 PUT value/reset 保持同一模式。
    mm.notify_config_changed(dest_config_name, result)
    return True


@script_app.put('/config/task/group/copy')
async def task_group_copy(task_name: str, group_name: str, dest_config_name: str, source_config_name: str):
    if dest_config_name not in mm.script_process or source_config_name not in mm.script_process:
        return False
    task_key = convert_to_underscore(task_name)
    group_key = convert_to_underscore(group_name)
    try:
        source_canonical = mm.store.load_canonical_snapshot(source_config_name)
        dest_loaded = mm.store.load(dest_config_name)
    except ConfigIdentityNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except ConfigNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    dest_canonical = dest_loaded.canonical
    if task_key not in source_canonical or not isinstance(source_canonical[task_key], dict):
        return False
    if group_key not in source_canonical[task_key]:
        return False
    expected = dest_canonical.get(task_key, {}).get(group_key, MISSING) if isinstance(
        dest_canonical.get(task_key), dict) else MISSING
    try:
        result = mm.store.replace_subtree(
            dest_config_name,
            (task_key, group_key),
            expected,
            copy.deepcopy(source_canonical[task_key][group_key]),
            dest_loaded.generation,
        )
    except ConfigValidationError as e:
        raise HTTPException(status_code=400, detail=f'Task group copy invalid: {e}')
    except ConfigIdentityNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except ConfigNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except ConfigGenerationMismatchError as e:
        raise HTTPException(status_code=409, detail=str(e))
    except Timeout as e:
        raise HTTPException(status_code=503, detail=f'Config lock timeout: {e}')

    # 成功后锁外投递 config_changed 事件，与 PUT value/reset 保持同一模式。
    mm.notify_config_changed(dest_config_name, result)
    return True


# ---------------------------------   脚本实例管理   ----------------------------------
@script_app.get('/{script_name}/start')
async def script_start(script_name: str):
    try:
        started = await mm.start_script_process(script_name)
    except (ConfigNotFoundError, ConfigIdentityNotFoundError) as e:
        raise HTTPException(status_code=404, detail=f'Config not found: {script_name}') from e
    except Timeout as e:
        raise HTTPException(status_code=503, detail=f'Config lock timeout: {e}') from e
    except ScriptStartupTimeoutError as e:
        # 子进程 generation 握手超时属于启动失败，必须返回 500 而不是 503 锁超时。
        raise HTTPException(status_code=500, detail=f'Script startup failed: {e}') from e
    except ConfigGenerationMismatchError as e:
        raise HTTPException(status_code=409, detail=str(e)) from e
    except (ConfigGenerationError, ConfigJsonError, StoreConfigJsonError, ConfigValidationError, OSError) as e:
        # 配置损坏、校验失败或文件系统错误必须保留为服务端失败，不能伪装成冲突。
        raise HTTPException(status_code=500, detail=f'Config start failed: {e}') from e
    if started is not True:
        # 只有明确 True 才算启动成功，None/False/其他返回值均禁止 HTTP 200。
        raise HTTPException(status_code=409, detail=f'Config refused to start: {script_name}')
    return

@script_app.get('/{script_name}/stop')
async def script_stop(script_name: str):
    if script_name not in mm.script_process:
        logger.warning(f'[{script_name}] script process does not exist')
        return
    await mm.script_process[script_name].stop()
    return

async def _restart_from_instance(script_name: str):
    # 实例主动请求重启也走 manager 身份协议，不能把 start 插入 rename/delete 提交窗口。
    await mm.restart_script_process(script_name)

@script_app.get('/{script_name}/restart_from_instance')
async def script_restart_from_instance(script_name: str):
    if script_name not in mm.script_process:
        logger.warning(f'[{script_name}] script process does not exist, skip restart_from_instance')
        return {'restarting': False}
    asyncio.create_task(_restart_from_instance(script_name))
    return {'restarting': True}

@script_app.get('/{script_name}/{task}/args')
async def script_task(script_name: str, task: str):
    # Config 会话持有注入 Store，可在模型纯 Schema 结果上补充运行时 active 身份。
    return mm.config_cache(script_name).script_task(task)

@script_app.put('/{script_name}/{task}/{group}/{argument}/value')
async def script_task(script_name: str, task: str, group: str, argument: str, types: str, value):
    try:
        match types:
            case 'integer':
                value = int(value)
            case 'number':
                value = float(value)
            case 'boolean':
                if isinstance(value, str):
                    logger.warning(f'[{script_name}] script argument {argument} value is string, try to convert to bool')
                    normalized = value.lower()
                    if normalized in ['true', '1']:
                        value = True
                    elif normalized in ['false', '0']:
                        value = False
                    else:
                        raise ValueError(f'Invalid boolean value: {value!r}')
                elif type(value) is not bool:
                    raise ValueError(f'Invalid boolean value: {value!r}')
            case 'string':
                pass
            case 'date_time':
                value = datetime.strptime(value, '%Y-%m-%d %H:%M:%S')
            case 'time_delta':
                # strptime 是个好东西，但是不能解析00的天数
                day = int(value[1])
                date_time = datetime.strptime(value[3:], '%H:%M:%S')
                value = TimeDelta(days=day, hours=date_time.hour, minutes=date_time.minute, seconds=date_time.second)
            case 'time':
                value = datetime.strptime(value, '%H:%M:%S').time()
            case _: pass
    except Exception as e:
        # 类型不正确
        raise HTTPException(status_code=400, detail=f'Argument type error: {e}')

    try:
        is_global_reset = (
            convert_to_underscore(task) == 'restart'
            and convert_to_underscore(group) == 'tasks_config_reset'
            and convert_to_underscore(argument) == 'reset_task_datetime_enable'
            and value is True
        )
        # 全局重置是一个用户动作，必须在单个 Store 事务内更新标志与全部 next_run。
        if is_global_reset:
            result = mm.store.reset_enabled_next_runs(script_name)
        else:
            result = mm.store.patch_user_argument(
                script_name,
                task,
                group,
                argument,
                value,
            )
    except ConfigValidationError as e:
        raise HTTPException(status_code=400, detail=f'Argument invalid: {e}')
    except ConfigIdentityNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except ConfigNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except ConfigGenerationMismatchError as e:
        raise HTTPException(status_code=409, detail=str(e))
    except Timeout as e:
        raise HTTPException(status_code=503, detail=f'Config lock timeout: {e}')
    except OSError as e:
        # atomic writer/文件系统失败必须明确返回服务端错误，不得报告配置已保存。
        raise HTTPException(status_code=500, detail=f'Config write failed: {e}')

    # 成功后锁外投递 config_changed 事件给运行实例，结果包含本次原子事务的全部路径。
    mm.notify_config_changed(script_name, result)
    return result.success


@script_app.put('/{script_name}/{task}/sync_next_run')
async def sync_next_run(script_name: str, task: str, target_dt: str):
    if script_name not in mm.script_process:
        return False
    config = mm.config_cache(script_name)
    target = datetime.strptime(target_dt, '%Y-%m-%d %H:%M:%S') if target_dt else None
    config.task_delay(task=task, success=True, target=target)
    script_process = mm.script_process[script_name]
    config.get_next()
    await script_process.broadcast_state({"schedule": config.get_schedule_data()})
    # task_delay 已写盘，投递 config_changed 事件提示子进程在下一边界刷新；
    # INACTIVE 实例不投递，避免 restart 后新进程消费陈旧事件（与 notify_config_changed 一致）。
    # 事件仅作低延迟提示：极端竞态下 generation/mtime 即使略陈旧，子进程仍会以
    # mtime_ns 兜底检测（规格 §4.4），不依赖事件携带的身份做一致性判断。
    if script_process.state != ScriptState.INACTIVE:
        script_process.deliver_config_changed(
            config.generation, config.mtime_ns,
            [(convert_to_underscore(task), "scheduler", "next_run")],
        )
    return True


# --------------------------------------  SSE  --------------------------------------
@script_app.get('/{script_name}/state')
async def script_task_state(script_name: str):
    async def state_generate_events():
        while True:
            # 生成 SSE 事件数据
            event_data = "data: Hello, SSE!\n\n"
            yield event_data

            # 模拟异步操作，可以替换为您的实际处理逻辑
            await asyncio.sleep(1)

    response = StreamingResponse(state_generate_events(), media_type="text/event-stream")
    response.headers["Cache-Control"] = "no-cache"
    return response

@script_app.get('/{script_name}/log')
async def script_task_log(script_name: str):
    async def log_generate_events():
        while True:
            # 生成 SSE 事件数据
            event_data = "data: log\n"
            yield event_data

            # 模拟异步操作，可以替换为您的实际处理逻辑
            await asyncio.sleep(1)

    response = StreamingResponse(log_generate_events(), media_type="text/event-stream")
    response.headers["Cache-Control"] = "no-cache"
    return response

# -------------------------------------- websocket --------------------------------------

@script_app.websocket("/ws/{script_name}")
async def websocket_endpoint(websocket: WebSocket, script_name: str):
    try:
        script_process = await mm.ensure_script_process(script_name)
    except ConfigNotFoundError:
        await websocket.close()
        return
    await script_process.connect(websocket)
    # 新连接定向首帧：顺序发送 state、schedule、缓存 config_state（不广播模拟首帧）
    await script_process.send_state(websocket, {"state": script_process.state})
    config = mm.config_cache(script_name)
    config.get_next()
    await script_process.send_state(websocket, {"schedule": config.get_schedule_data()})
    await script_process.send_state(websocket, {"config_state": script_process.cached_config_state()})

    try:
        while True:
            data = await websocket.receive_text()
            if data == 'get_state':
                await script_process.send_state(websocket, {"state": script_process.state})
            elif data == 'get_schedule':
                config = mm.config_cache(script_name)
                config.get_next()
                await script_process.send_state(websocket, {"schedule": config.get_schedule_data()})
            elif data == 'get_config_state':
                await script_process.send_state(websocket, {"config_state": script_process.cached_config_state()})
            elif data == 'start':
                # 连接期间缓存的 wrapper 可能已失效，按名称重新进入 manager 身份协议。
                # 启动失败（generation 不匹配、握手超时、配置损坏、锁超时）不得穿透
                # while True：外层 try 只捕 WebSocketDisconnect，异常逃逸会直接打死
                # 这条 WS 连接，前端只看到重连而拿不到任何失败提示。
                # 捕获后回推一帧当前 state，让客户端退出「启动中」并与真实状态对齐。
                try:
                    await mm.start_script_process(script_name)
                    script_process = await mm.ensure_script_process(script_name)
                except Exception as e:
                    logger.error(f'[{script_name}] websocket start failed: {e}')
                await script_process.send_state(websocket, {"state": script_process.state})
            elif data == 'stop':
                # 停止同样不得让异常打死连接，理由同上
                try:
                    await script_process.stop()
                except Exception as e:
                    logger.error(f'[{script_name}] websocket stop failed: {e}')
                await script_process.send_state(websocket, {"state": script_process.state})

    except WebSocketDisconnect:
        logger.warning(f'[{script_name}] websocket disconnect')
        await script_process.disconnect(websocket)
