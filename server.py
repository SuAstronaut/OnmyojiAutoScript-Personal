# This Python file uses the following encoding: utf-8
# Copy from https://github.com/LmeSzinc/AzurLaneAutoScript/gui.py


"""
在任何平台把当前 Python 进程（含子线程、子进程）切到北京时间。
• Linux/macOS/WSL 及 Win-Py 3.11+ → TZ='Asia/Shanghai' + time.tzset()
• Win-Py ≤ 3.10            → TZ='CST-8'       + _tzset()（POSIX 语法）
"""
import os, sys, time

if hasattr(time, "tzset"):
    # Unix 全系  /  Windows 3.11+ 走这条
    os.environ["TZ"] = "Asia/Shanghai"     # IANA 名称，glibc/Apple libc 都认识
    time.tzset()
else:
    # 只有旧 Windows 才会落到这里
    import ctypes
    os.environ["TZ"] = "CST-8"             # POSIX 字符串：UTC+8 且无 DST
    for dll in ("ucrtbase", "msvcrt"):     # 新旧 CRT 都试一遍
        try:
            ctypes.CDLL(dll)._tzset()
            break
        except (OSError, AttributeError):
            continue
        
import threading

# 必须早于任何 zerorpc / pyzmq 的 import：pyzmq 会与 onnxruntime 抢 DLL
# 加载顺序，先加载 pyzmq 会让后续 OCR 后端初始化失败。详见 module/ocr/preload.py
from module.ocr.preload import preload_ocr_backend

preload_ocr_backend()

from module.logger import logger
from module.server.setting import State
from module.server.server_logging import setup_server_logging
from module.ocr.rpc import ensure_ocr_server_started, shutdown_ocr_server


def fun(ev: threading.Event):
    import argparse
    import asyncio
    import sys

    import uvicorn

    # 不知道干啥的照着抄就行了
    if sys.platform.startswith("win"):
        asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())

    State.restart_event = ev

    parser = argparse.ArgumentParser(description="Alas web service")
    parser.add_argument(
        "--host",
        type=str,
        help="Host to listen. Default to WebuiHost in deploy setting",
    )
    parser.add_argument(
        "-p",
        "--port",
        type=int,
        help="Port to listen. Default to WebuiPort in deploy setting",
    )
    parser.add_argument(
        "-k", "--key", type=str, help="Password of alas. No password by default"
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
        help="Run alas by config names on startup",
    )
    args, _ = parser.parse_known_args()

    host = args.host or State.deploy_config.WebuiHost or "0.0.0.0"
    port = args.port or int(State.deploy_config.WebuiPort) or 22270
    os.environ["OAS_WEBUI_PORT"] = str(port)  # 子脚本进程通过该端口主动请求 server 级重启。

    setup_server_logging()

    logger.hr("Launcher config")
    logger.attr("Host", host)
    logger.attr("Port", port)
    logger.attr("Reload", ev is not None)
    logger.attr("Log file", logger.log_file)

    ensure_ocr_server_started()

    try:
        # 保留 Server 实例引用：/home/kill_server 通过 State.server.should_exit 让 uvicorn
        # 优雅退出整个服务（跑 lifespan 关闭 + finally 清理 OCR），而不是只停脚本进程。
        config = uvicorn.Config("module.server.app:fastapi_app",
                                host=host,
                                port=port,
                                factory=True,
                                log_config=None)
        server = uvicorn.Server(config)
        State.server = server
        server.run()
    finally:
        shutdown_ocr_server()


if __name__ == "__main__":
    fun(None)
