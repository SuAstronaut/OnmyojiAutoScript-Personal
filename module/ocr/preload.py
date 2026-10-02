# This Python file uses the following encoding: utf-8
"""OCR 后端预加载。

必须在进程入口最前面调用，早于任何 zerorpc / pyzmq 的 import。

Windows 上 pyzmq 自带的 libzmq/libsodium 与 onnxruntime 的依赖 DLL 冲突：
先加载 pyzmq，之后 import onnxruntime 会失败：

    ImportError: DLL load failed while importing onnxruntime_pybind11_state:
    动态链接库(DLL)初始化例程失败。

冲突是单向的——onnxruntime 先加载则两者可以正常共存。实测只有 pyzmq 触发，
gevent / greenlet 本身无影响。

而 script.py / server.py / gui.py 都会在很早的位置 import zerorpc（依赖 pyzmq），
OCR 模块在 import 链里排得更靠后，所以修复不能放在 OCR 模块内部，
必须由入口在第一行主动触发。

这个模块刻意保持零项目内依赖（不 import module.logger 等），
以免间接把别的东西先拉进来。
"""
import sys

# 预加载结果，供诊断使用：True 成功，False 失败，None 未尝试
BACKEND_LOADED = None


def preload_ocr_backend(verbose: bool = False) -> bool:
    """加载 onnxruntime，占住 DLL 加载顺序。

    Args:
        verbose: 为 True 时打印后端信息（安装/诊断脚本用）。

    Returns:
        bool: 后端是否可用。未安装 onnxruntime 时返回 False 而不抛异常，
            让不需要 OCR 的流程（如纯配置操作）仍能启动。
    """
    global BACKEND_LOADED

    if BACKEND_LOADED is not None:
        return BACKEND_LOADED

    if 'zmq' in sys.modules:
        # 已经晚了，这次 import 必定失败。打印可操作的提示而不是静默失败。
        print('[ocr] WARNING: pyzmq loaded before onnxruntime, '
              'OCR backend will fail to initialize. '
              'Call preload_ocr_backend() earlier in the entry point.',
              file=sys.stderr)

    try:
        import onnxruntime
        BACKEND_LOADED = True
        if verbose:
            print(f'[ocr] onnxruntime {onnxruntime.__version__} '
                  f'providers={onnxruntime.get_available_providers()}')
    except ImportError as e:
        BACKEND_LOADED = False
        if verbose:
            print(f'[ocr] onnxruntime unavailable: {e}', file=sys.stderr)
    return BACKEND_LOADED
