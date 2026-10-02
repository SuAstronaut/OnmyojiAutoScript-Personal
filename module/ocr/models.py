# This Python file uses the following encoding: utf-8
"""OCR 模型工厂。

本地推理与 RPC 服务端共用同一个 PP-OCRv6 模型实现（RapidOcrModel），
避免两条路径识别行为漂移。对外只暴露三个函数：

    get_ocr_model(lang)       上层统一入口，按部署配置决定本地还是 RPC
    get_local_ocr_model(lang) 强制本地模型，RPC 服务端自己也用它
    clear_ocr_model_cache()   清空缓存，配置变更或测试时使用

这里不 import 任何推理后端：RapidOcrModel 内部把 rapidocr 的 import
推迟到真正构造引擎时，因此 gui / server / script 的 import 链不会
提前把 onnxruntime 加载进内存。
"""
from typing import Any, Dict

from module.logger import logger
from module.ocr.rapid_ocr import RapidOcrModel

# 当前只支持中文模型；保留 lang 参数是为了兼容既有调用签名
SUPPORTED_LANGS = ('ch',)

# 本地模型按语言缓存：模型加载昂贵，且每个 Rule 都会取一次
_LOCAL_MODEL_CACHE: Dict[str, RapidOcrModel] = {}
# RPC 代理按地址缓存
_OCR_PROXY_CACHE: Dict[str, Any] = {}


def clear_ocr_model_cache() -> None:
    """清空本地模型与 RPC 代理缓存。"""
    _LOCAL_MODEL_CACHE.clear()
    _OCR_PROXY_CACHE.clear()


def _create_model_proxy(address: str):
    """构造 RPC 代理。

    独立成函数是为了给测试提供稳定注入点，同时把 zerorpc 的 import
    限制在真正使用 RPC 时。
    """
    from module.ocr.rpc import ModelProxy
    return ModelProxy(address)


def get_local_ocr_model(lang: str = 'ch') -> RapidOcrModel:
    """获取本地 PP-OCRv6 模型实例（按语言缓存）。

    Args:
        lang: 目前只支持 'ch'。

    Raises:
        ValueError: 传入不支持的语言。
    """
    if lang not in SUPPORTED_LANGS:
        raise ValueError(f'Unsupported OCR lang: {lang}, expected one of {SUPPORTED_LANGS}')

    if lang not in _LOCAL_MODEL_CACHE:
        from module.server.setting import State

        deploy_config = State.deploy_config
        _LOCAL_MODEL_CACHE[lang] = RapidOcrModel(
            model_dir=deploy_config.filepath('OcrModelDir'),
            device=str(deploy_config.OcrDevice),
            model_type=str(deploy_config.OcrModelType),
            cpu_threads=int(deploy_config.OcrCpuThreads),
        )
    return _LOCAL_MODEL_CACHE[lang]


def get_ocr_model(lang: str = 'ch'):
    """获取 OCR 模型，按部署配置在 RPC 服务与本地模型之间分流。

    RPC 连接失败时降级为本地模型：多开时 RPC 能显著省内存，但连不上
    不该让整个任务崩掉，代价只是这个进程自己加载一份模型。
    """
    from module.server.setting import State

    deploy_config = State.deploy_config
    if not deploy_config.UseOcrServer:
        return get_local_ocr_model(lang)

    address = deploy_config.OcrClientAddress or '127.0.0.1:22268'
    if address in _OCR_PROXY_CACHE:
        return _OCR_PROXY_CACHE[address]

    try:
        proxy = _create_model_proxy(address)
    except Exception as e:
        logger.warning(f'OCR server unavailable at {address} ({e}), fall back to local model')
        return get_local_ocr_model(lang)

    _OCR_PROXY_CACHE[address] = proxy
    return proxy
