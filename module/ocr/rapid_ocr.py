# This Python file uses the following encoding: utf-8
"""PP-OCRv6 推理适配器（RapidOCR + onnxruntime）。

对上层只暴露两个方法，签名与项目原有 OCR 模型保持一致：
    ocr_single_line(image) -> (text, score)
    detect_and_ocr(image, drop_score=..., unclip_ratio=..., box_thresh=..., vertical=...) -> list[BoxedResult]

设计上有三个必须遵守的约束：

1. 状态复位。RapidOCR 的 update_params() 会把每次调用传入的参数永久写回引擎实例，
   且对 None 值直接跳过（不复位）。这意味着一次 use_det=False 的单行调用之后，
   如果下一次全图调用省略 use_det，引擎仍然停留在单行模式，返回 TextRecOutput，
   全图 OCR 会静默失效。因此每次调用都显式传满全部可变参数。

2. 设备可移植。同一份配置要能在 N 卡、A 卡、Intel 核显和无独显机器上启动，
   所以 auto 档先探测 DirectML，再用一次预热推理验证真的能跑，失败才降级 CPU。

3. 模型落在项目内。Global.model_root_dir 指向 toolkit/ocr_models，
   避免 RapidOCR 把模型下载到用户级缓存目录。

对 rapidocr 的 import 全部延迟到真正构造引擎时，这样在未安装依赖的机器上
也能导入本模块跑单元测试。
"""
import sys
from typing import Any, Dict, List, Optional, Tuple

import numpy as np

from module.logger import logger
from module.ocr.result import BoxedResult

# 支持的推理设备与模型档位
DEVICES = ('auto', 'dml', 'cpu')
MODEL_TYPES = ('small', 'medium')
# medium 只在 GPU 下启用，CPU 上代价过高，自动降级为 small
GPU_ONLY_MODEL_TYPES = ('medium',)

# 默认调优参数，取 RapidOCR 默认值，保持与既有识别行为一致
DEFAULT_DROP_SCORE = 0.5
DEFAULT_BOX_THRESH = 0.5
DEFAULT_UNCLIP_RATIO = 1.6
# 竖排判定阈值：高宽比超过该值才旋转
VERTICAL_RATIO = 1.5
# DirectML 最低 Windows 内部版本号（Windows 10 1903）
DML_MIN_WINDOWS_BUILD = 18362


def ensure_backend_loaded() -> bool:
    """确保 onnxruntime 的原生 DLL 已完成初始化。

    这里解决一个 Windows 上的 DLL 加载顺序冲突：pyzmq 会加载自带的
    libzmq/libsodium，之后再 import onnxruntime 就会失败：

        ImportError: DLL load failed while importing onnxruntime_pybind11_state:
        动态链接库(DLL)初始化例程失败。

    冲突是单向的——ORT 先加载则两者可以共存。而 OAS 的 import 链里
    zerorpc（依赖 pyzmq）会被 script.py / server.py 提前拉进来，
    于是本地 OCR 在任务进程里必然崩溃。

    实测只有 pyzmq 触发，gevent / greenlet 本身无影响。

    调用方必须在任何推理之前调用本函数；由于 sys.modules 会缓存，
    重复调用没有额外开销。

    Returns:
        bool: 后端是否可用。
    """
    if 'onnxruntime' in sys.modules:
        return True
    if 'zmq' in sys.modules:
        # 已经晚了：此时 import 必定失败。给出可操作的错误信息而不是裸异常。
        logger.error('pyzmq was loaded before onnxruntime; OCR backend cannot '
                     'initialize. Call module.ocr.rapid_ocr.ensure_backend_loaded() '
                     'at process entry, before importing zerorpc.')
    try:
        import onnxruntime  # noqa: F401
        return True
    except ImportError as e:
        logger.error(f'Failed to load onnxruntime: {e}')
        return False


def probe_directml() -> bool:
    """探测当前机器能否使用 DirectML(GPU) 推理。

    只做便宜的静态检查：操作系统、Windows 版本、onnxruntime provider 列表。
    真实驱动是否能建 session 由 RapidOcrModel 的预热推理兜底。

    Returns:
        bool: True 表示可以尝试 DirectML。
    """
    import platform

    if platform.system() != 'Windows':
        logger.info('DirectML is only available on Windows, fall back to CPU')
        return False

    build_str = platform.version().split('.')[-1]
    build = int(build_str) if build_str.isdigit() else 0
    if build and build < DML_MIN_WINDOWS_BUILD:
        logger.info(f'Windows build {build} is lower than {DML_MIN_WINDOWS_BUILD}, fall back to CPU')
        return False

    try:
        import onnxruntime
    except ImportError:
        logger.warning('onnxruntime is not installed, cannot probe DirectML')
        return False

    providers = onnxruntime.get_available_providers()
    if 'DmlExecutionProvider' not in providers:
        logger.info(f'DmlExecutionProvider not in {providers}, fall back to CPU')
        return False
    return True


class RapidOcrModel:
    """PP-OCRv6 模型包装，供本地调用与 RPC 服务端共用。"""

    def __init__(self,
                 model_dir: str,
                 device: str = 'auto',
                 model_type: str = 'small',
                 cpu_threads: int = 4,
                 engine: Any = None) -> None:
        """
        Args:
            model_dir: v6 onnx 模型目录，必须在项目内。
            device: auto / dml / cpu。
            model_type: small / medium，medium 仅 GPU 生效。
            cpu_threads: CPU 推理线程数。
            engine: 注入的引擎实例，仅测试使用；为 None 时惰性构造真实引擎。
        """
        if device not in DEVICES:
            raise ValueError(f'Unsupported OcrDevice: {device}, expected one of {DEVICES}')
        if model_type not in MODEL_TYPES:
            raise ValueError(f'Unsupported OcrModelType: {model_type}, expected one of {MODEL_TYPES}')

        self.model_dir = model_dir
        self.device = device
        self.model_type = model_type
        self.cpu_threads = int(cpu_threads)
        self._engine = engine
        # 注入引擎时视为已就绪，不再触发探测/预热
        self._engine_ready = engine is not None

        self.resolved_device = self._resolve_device(device)
        self.resolved_model_type = self._resolve_model_type(model_type, self.resolved_device)

    # ---------------- 设备与档位解析 ----------------

    @staticmethod
    def _resolve_device(device: str) -> str:
        """把 auto 解析成 dml 或 cpu；dml / cpu 原样返回。"""
        if device != 'auto':
            return device
        return 'dml' if probe_directml() else 'cpu'

    @staticmethod
    def _resolve_model_type(model_type: str, resolved_device: str) -> str:
        """CPU 下把 GPU 专属档位降级为 small，保证低配机器也能启动。"""
        if model_type in GPU_ONLY_MODEL_TYPES and resolved_device == 'cpu':
            logger.info(f'OcrModelType {model_type} requires GPU, downgrade to small on CPU')
            return 'small'
        return model_type

    # ---------------- 引擎构造 ----------------

    def build_params(self, use_dml: Optional[bool] = None) -> Dict[str, Any]:
        """生成 RapidOCR 构造参数（枚举字段保持字符串形式）。

        字符串便于在未安装 rapidocr 的环境下断言，真正构造前由
        _to_engine_params 转成 RapidOCR 要求的 Enum。

        Args:
            use_dml: 覆盖 GPU 开关；None 表示按 resolved_device 决定。
        """
        if use_dml is None:
            use_dml = self.resolved_device == 'dml'
        model_type = self.resolved_model_type
        return {
            'Global.model_root_dir': self.model_dir,
            # OAS 截图恒为正向，方向分类只增加开销和误判
            'Global.use_cls': False,
            'Det.engine_type': 'onnxruntime',
            'Det.lang_type': 'ch',
            'Det.model_type': model_type,
            'Det.ocr_version': 'PP-OCRv6',
            'Rec.engine_type': 'onnxruntime',
            'Rec.lang_type': 'ch',
            'Rec.model_type': model_type,
            'Rec.ocr_version': 'PP-OCRv6',
            # 降低 rec 批大小以压低峰值内存/显存
            'Rec.rec_batch_num': 2,
            'EngineConfig.onnxruntime.use_dml': use_dml,
            'EngineConfig.onnxruntime.use_cuda': False,
            'EngineConfig.onnxruntime.intra_op_num_threads': self.cpu_threads,
        }

    @staticmethod
    def _to_engine_params(params: Dict[str, Any]) -> Dict[str, Any]:
        """把字符串枚举字段转成 RapidOCR 要求的 Enum 类型。

        ParseParams.update_batch 对 engine_type / model_type / ocr_version
        强制要求 Enum，传字符串会直接 TypeError。
        """
        from rapidocr.utils.typings import (EngineType, LangDet, LangRec,
                                            ModelType, OCRVersion)

        converted = dict(params)
        for section, lang_enum in (('Det', LangDet), ('Rec', LangRec)):
            converted[f'{section}.engine_type'] = EngineType(params[f'{section}.engine_type'])
            converted[f'{section}.lang_type'] = lang_enum(params[f'{section}.lang_type'])
            converted[f'{section}.model_type'] = ModelType(params[f'{section}.model_type'])
            converted[f'{section}.ocr_version'] = OCRVersion(params[f'{section}.ocr_version'])
        return converted

    def _create_engine(self, use_dml: bool):
        """构造一个 RapidOCR 实例。"""
        from rapidocr import RapidOCR

        params = self._to_engine_params(self.build_params(use_dml=use_dml))
        return RapidOCR(params=params)

    def _warmup(self, engine) -> None:
        """跑一次极小图推理，验证 provider 真的能建 session 并预热内存池。

        DirectML 的驱动问题只会在建 session / 首次推理时暴露，
        provider 列表检查通不过这一关。
        """
        probe = np.zeros((32, 64, 3), dtype=np.uint8)
        engine(probe, **self._call_kwargs(use_det=False))

    @property
    def engine(self):
        """惰性构造引擎；DirectML 失败时自动降级 CPU 重建。"""
        if self._engine_ready:
            return self._engine

        want_dml = self.resolved_device == 'dml'
        if want_dml:
            try:
                engine = self._create_engine(use_dml=True)
                self._warmup(engine)
                logger.info(f'OCR engine ready: PP-OCRv6 {self.resolved_model_type} on DirectML')
                self._engine = engine
                self._engine_ready = True
                return self._engine
            except Exception as e:
                logger.warning(f'DirectML inference unavailable ({e}), fall back to CPU')
                self.resolved_device = 'cpu'
                self.resolved_model_type = self._resolve_model_type(self.model_type, 'cpu')

        engine = self._create_engine(use_dml=False)
        self._warmup(engine)
        logger.info(f'OCR engine ready: PP-OCRv6 {self.resolved_model_type} on CPU '
                    f'({self.cpu_threads} threads)')
        self._engine = engine
        self._engine_ready = True
        return self._engine

    # ---------------- 调用参数 ----------------

    @staticmethod
    def _call_kwargs(use_det: bool,
                     drop_score: Optional[float] = None,
                     box_thresh: Optional[float] = None,
                     unclip_ratio: Optional[float] = None) -> Dict[str, Any]:
        """构造一次调用的完整参数。

        这里必须把全部可变参数显式传满：RapidOCR 会把参数写回实例，
        且对 None 跳过复位，省略任何一项都会继承上一次调用的状态。
        """
        return {
            'use_det': use_det,
            'use_cls': False,
            'use_rec': True,
            'return_word_box': False,
            'return_single_char_box': False,
            'text_score': DEFAULT_DROP_SCORE if drop_score is None else float(drop_score),
            'box_thresh': DEFAULT_BOX_THRESH if box_thresh is None else float(box_thresh),
            'unclip_ratio': DEFAULT_UNCLIP_RATIO if unclip_ratio is None else float(unclip_ratio),
        }

    @staticmethod
    def _is_empty_image(image) -> bool:
        """零尺寸或空图判定。RapidOCR 内部会对这类输入除零。"""
        if image is None:
            return True
        shape = getattr(image, 'shape', None)
        if not shape or len(shape) < 2:
            return True
        return shape[0] <= 0 or shape[1] <= 0

    @staticmethod
    def rotate_vertical(image: np.ndarray) -> np.ndarray:
        """竖排裁剪图旋转 90 度后再识别，否则竖排文字识别率极低。"""
        height, width = image.shape[0:2]
        if width and height / width >= VERTICAL_RATIO:
            return np.rot90(image)
        return image

    # ---------------- 对外接口 ----------------

    def ocr_single_line(self, image) -> Tuple[str, float]:
        """单行识别，跳过检测直接走 rec。

        Returns:
            tuple: (文本, 分数)。识别不到时返回 ('', 0.0)，绝不返回 None，
                上层普遍写 `result, score = ...`，返回 None 会直接 TypeError。
        """
        if self._is_empty_image(image):
            return '', 0.0

        result = self.engine(image, **self._call_kwargs(use_det=False))
        txts = getattr(result, 'txts', None)
        scores = getattr(result, 'scores', None)
        if not txts:
            return '', 0.0
        score = float(scores[0]) if scores else 0.0
        return str(txts[0]), score

    def detect_and_ocr(self,
                       image,
                       drop_score: Optional[float] = None,
                       unclip_ratio: Optional[float] = None,
                       box_thresh: Optional[float] = None,
                       vertical: bool = False) -> List[BoxedResult]:
        """全图检测 + 识别。

        Args:
            image: BGR 图像。
            drop_score: 低于该分数的结果被丢弃，默认 0.5。
            unclip_ratio: 检测框扩张比例。
            box_thresh: 检测框阈值，调低可召回更多小字。
            vertical: 竖排模式，识别前旋转裁剪图。

        Returns:
            list[BoxedResult]: 无结果时返回空列表。
        """
        if self._is_empty_image(image):
            return []

        threshold = DEFAULT_DROP_SCORE if drop_score is None else float(drop_score)
        kwargs = self._call_kwargs(use_det=True,
                                   drop_score=drop_score,
                                   box_thresh=box_thresh,
                                   unclip_ratio=unclip_ratio)

        engine = self.engine
        if vertical:
            result = self._call_vertical(engine, image, kwargs)
        else:
            result = engine(image, **kwargs)

        boxes = getattr(result, 'boxes', None)
        txts = getattr(result, 'txts', None)
        scores = getattr(result, 'scores', None)
        if boxes is None or txts is None or scores is None:
            return []

        results = []
        for box, text, score in zip(boxes, txts, scores):
            if float(score) < threshold:
                continue
            # box 转 list：RuleList 与 Exploration 都按 box[0][0] 取下标，
            # list 与 ndarray 都满足，转 list 是为了 RPC 序列化时形态一致
            results.append(BoxedResult(np.asarray(box).tolist(), None, str(text), score))
        return results

    def _call_vertical(self, engine, image, kwargs):
        """竖排模式：临时包裹引擎的识别步骤，把裁剪图旋转后再送入 rec。

        RapidOCR 没有暴露竖排开关，recognize_txt 是唯一稳定的注入点。
        用完必须还原，否则横排调用也会被旋转。
        """
        origin = getattr(engine, 'recognize_txt', None)
        if origin is None:
            return engine(image, **kwargs)

        def rotated_recognize(img_list):
            return origin([self.rotate_vertical(i) for i in img_list])

        engine.recognize_txt = rotated_recognize
        try:
            return engine(image, **kwargs)
        finally:
            engine.recognize_txt = origin
