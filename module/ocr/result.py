# This Python file uses the following encoding: utf-8
"""项目自有 OCR 结果类型。

原先业务层直接使用 ppocronnx.predict_system.BoxedResult，导致 module/atom、
tasks 等上层代码与第三方 OCR 包强耦合，换引擎时必须改动大量文件。
这里定义等价的项目内类型，字段顺序与旧类保持一致，方便平滑替换。
"""
from typing import Any, Optional


class BoxedResult:
    """一条带检测框的 OCR 结果。

    Attributes:
        box: 四点检测框。RuleList 会按 box[0][0] / box[0][1] 取坐标，
            因此实现方必须保证其支持二级下标（list 或 ndarray 均可）。
        text_img: 该框裁剪出的图像。当前全仓无读取点，实现方可传 None。
        ocr_text: 识别文本。BaseCor.detect_and_ocr 会就地改写为后处理结果。
        score: 识别置信度，统一为内置 float，便于与阈值直接比较。
    """

    def __init__(self, box: Any, text_img: Optional[Any], ocr_text: str, score: float) -> None:
        self.box = box
        self.text_img = text_img
        self.ocr_text = ocr_text
        self.score = float(score)

    def __repr__(self) -> str:
        return 'BoxedResult[%s, %s]' % (self.ocr_text, self.score)

    __str__ = __repr__
