# This Python file uses the following encoding: utf-8
# @author runhey
# github https://github.com/runhey
from contextlib import asynccontextmanager

import argparse
import time
from pathlib import Path
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from module.logger import logger

from module.server.home_router import home_app
from module.server.script_router import script_app
from module.server.log_router import log_app
from module.server.stats_router import stats_app
from module.server.tool_router import tool_app
from module.server.setting import State
from module.server.main_manager import mm
from starlette import status
from starlette.responses import JSONResponse


@asynccontextmanager
async def lifespan(app: FastAPI):
    await on_startup()
    yield
    await on_shutdown()

app = FastAPI(
    title='OAS',
    description='OAS web service',
    version='0.0.0',
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"]
)

app.include_router(home_app)
app.include_router(script_app)
app.include_router(log_app)
app.include_router(stats_app)
app.include_router(tool_app)

annotator_static_dir = Path(__file__).resolve().parent / "web" / "annotator" / "static"
if annotator_static_dir.exists():
    app.mount("/tool/annotator/static", StaticFiles(directory=str(annotator_static_dir)), name="annotator_static")


@app.middleware("http")
async def log_request(request: Request, call_next):
    """记录 server 端 HTTP 请求状态和耗时。"""
    start_time = time.perf_counter()
    response = None
    try:
        response = await call_next(request)
        return response
    finally:
        elapsed_ms = (time.perf_counter() - start_time) * 1000
        status_code = response.status_code if response else 500
        client_host = request.client.host if request.client else "unknown"
        # 中间件统一记录请求结果，便于排查前端 API 调用问题。
        logger.info(
            f"{request.method} {request.url.path} {status_code} "
            f"{elapsed_ms:.2f}ms from {client_host}"
        )


async def on_startup():
    """
    app.state 的生命周期在定义app的时候就有了
    :return:
    """
    # 第一条生产动作：migration → lifecycle 恢复 → active 身份校验 → 枚举并创建 ScriptProcess。
    # 必须在任何 config 读取/枚举（restart_processes）之前完成。
    await mm.initialize()
    logger.info('OAS web service startup done')
    if app.state.script_instances:
        await mm.restart_processes(app.state.script_instances)


async def on_shutdown():
    logger.info('OAS web service shutdown start')
    # 停止所有脚本实例并清空注册/cache，避免服务退出后残留子进程
    for _name, script_p in list(mm.script_process.items()):
        try:
            await script_p.stop()
        except Exception as e:
            logger.error(f'stop script {_name} failed during shutdown: {e}')
    mm.script_process = {}
    logger.info('OAS web service shutdown done')


@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    logger.error(f"Internal Server Error: ", exc_info=True)

    message = ', '.join(str(arg) for arg in exc.args) if exc.args else str(exc)

    return JSONResponse(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        content={
            'message': message
        },
    )
 
def fastapi_app():
    parser = argparse.ArgumentParser(description="OAS web service")
    parser.add_argument(
        "-k", "--key", type=str, help="Password of OAS. No password by default"
    )
    parser.add_argument(
        "--cdn",
        action="store_true",
        help="Use jsdelivr cdn for pywebio static files (css, js). Self host cdn by default.",
    )
    parser.add_argument(
        "--run",
        nargs="+",
        type=str,
        help="Run OAS by config names on startup",
    )
    args, _ = parser.parse_known_args()
    # ------------------------------------------------------------------------------------------------------------------

    runs = None
    if args.run:
        runs = args.run
    elif State.deploy_config.Run:
        # TODO: refactor poor_yaml_read() to support list
        tmp = State.deploy_config.Run.split(",")
        runs = [l.strip(" ['\"]") for l in tmp if len(l)]
    # ------------------------------------------------------------------------------------------------------------------
    app.state.script_instances = runs

    return app
