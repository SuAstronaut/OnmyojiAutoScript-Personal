# This Python file uses the following encoding: utf-8
"""OCR RPC 服务进程入口。

必须作为独立进程入口存在，不能用 multiprocessing.Process 直接把
module.ocr.rpc.run_ocr_server 当 target：

zerorpc 依赖 gevent，gevent 会替换线程与 TLS 原语；onnxruntime 的 DLL
初始化例程需要原生 Win32 线程语义，在被 patch 过的环境里 import 会报
    ImportError: DLL load failed while importing onnxruntime_pybind11_state:
    动态链接库(DLL)初始化例程失败。
实测导入顺序是唯一变量：先 onnxruntime 后 zerorpc 正常，反之必然失败。

而 module/ocr/rpc.py 在模块顶层就 import zerorpc，Windows spawn 子进程
re-import 该模块时顺序已经错了。所以把入口独立成本模块，并保证
「import onnxruntime」是模块里第一条实质语句，rpc 相关 import 全部延后。

用法（由 rpc.ensure_ocr_server_started 自动拉起）：
    ./toolkit/python.exe -m module.ocr.server_boot --host 0.0.0.0 --port 22268
"""
import argparse
import sys


def preload_backend() -> bool:
    """先让 onnxruntime 完成原生 DLL 初始化。必须在任何 gevent/zerorpc 之前。"""
    try:
        import onnxruntime
        print(f'[ocr-server] onnxruntime {onnxruntime.__version__} '
              f'providers={onnxruntime.get_available_providers()}', flush=True)
        return True
    except Exception as e:
        print(f'[ocr-server] failed to load onnxruntime: {e}', flush=True)
        return False


def main() -> int:
    parser = argparse.ArgumentParser(description='OAS OCR RPC server')
    parser.add_argument('--host', default='0.0.0.0')
    parser.add_argument('--port', type=int, default=22268)
    args = parser.parse_args()

    if not preload_backend():
        return 1

    # 此刻 ORT 的 DLL 已初始化完成，再引入 gevent/zerorpc 是安全的
    from module.logger import logger
    from module.ocr.rpc import serve_forever

    logger.info(f'Start OCR server on {args.host}:{args.port}')
    serve_forever(args.host, args.port)
    return 0


if __name__ == '__main__':
    sys.exit(main())
