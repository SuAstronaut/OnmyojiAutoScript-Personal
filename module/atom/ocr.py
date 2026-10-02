# This Python file uses the following encoding: utf-8
# @author runhey
# github https://github.com/runhey

import numpy as np
import cv2

from module.ocr.base_ocr import BaseCor, OcrMode, OcrMethod, OcrMethodType
from module.ocr.sub_ocr import Full, Single, Digit, DigitCounter, Duration, Quantity
from module.logger import logger
from module.device.humanize import get_current_humanizer


class RuleOcr(Digit, DigitCounter, Duration, Single, Full, Quantity):

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

    def pre_process(self, image):
        match self.method.get_method_type():
            case OcrMethodType.DEFAULT:
                pass
            case OcrMethodType.CF_RGB:
                _val = self.method.get_val()
                lower, upper = _val.split(',')
                lower = np.array([int(lower[i:i + 2], 16) for i in (0, 2, 4)])
                upper = np.array([int(upper[i:i + 2], 16) for i in (0, 2, 4)])
                mask = cv2.inRange(image, lower, upper)
                res_img = cv2.bitwise_and(image, image, mask=mask)
                return res_img
            case OcrMethodType.CF_HSV:
                _val = self.method.get_val()
                lower, upper = _val.split(',')
                lower = np.array([int(lower[i:i + 2], 16) for i in (0, 2, 4)])
                upper = np.array([int(upper[i:i + 2], 16) for i in (0, 2, 4)])
                res_img = cv2.cvtColor(image, cv2.COLOR_RGB2HSV)
                mask = cv2.inRange(res_img, lower, upper)
                res_img = cv2.bitwise_and(res_img, res_img, mask=mask)
                res_img = cv2.cvtColor(res_img, cv2.COLOR_HSV2RGB)
                return res_img
        return image

    def after_process(self, result):
        match self.mode:
            case OcrMode.FULL:
                return Full.after_process(self, result)
            case OcrMode.SINGLE:
                return Single.after_process(self, result)
            case OcrMode.DIGIT:
                return Digit.after_process(self, result)
            case OcrMode.DIGITCOUNTER:
                return DigitCounter.after_process(self, result)
            case OcrMode.DURATION:
                return Duration.after_process(self, result)
            case OcrMode.QUANTITY:
                return Quantity.after_process(self, result)
            case _:
                return result

    def ocr(self, image, keyword=None):

        match self.mode:
            case OcrMode.FULL:
                return Full.ocr_full(self, image, keyword)
            case OcrMode.SINGLE:
                return Single.ocr_single(self, image)
            case OcrMode.DIGIT:
                return Digit.ocr_digit(self, image)
            case OcrMode.DIGITCOUNTER:
                return DigitCounter.ocr_digit_counter(self, image)
            case OcrMode.DURATION:
                return Duration.ocr_duration(self, image)
            case OcrMode.QUANTITY:
                return Quantity.ocr_quantity(self, image)
            case _:
                return None

    def coord(self) -> tuple:
        """
        获取一个区域，随机返回一个坐标
        :return:
        """
        area = None
        if self.mode == OcrMode.FULL:
            logger.info(f"OCR [{self.name}] is in FULL mode")
            area = self.area
        else:
            area = self.roi

        x, y, w, h = self.area
        logger.info(f"OCR [{self.name}] area: {self.area}")
        # 维度 A 落点采样（Plan Task 13）：有绑定且启用时用拟人 RNG，无 context 或
        # sample_point 返回 None（off/回退）时走原 np.random.randint 均匀采样
        humanizer = get_current_humanizer()
        if humanizer is not None:
            point = humanizer.sample_point((x, y, w, h))
            if point is not None:
                return point
        x = np.random.randint(x, x + w)
        y = np.random.randint(y, y + h)
        return x, y


if __name__ == "__main__":
    # O_MALL_RESOURCE_1 = RuleOcr(roi=(144, 7, 100, 43), area=(144, 7, 100, 43), mode="Quantity", method="Default",
    #                             keyword="", name="mall_resource_1")
    # O_MALL_RESOURCE_2 = RuleOcr(roi=(326, 8, 124, 39), area=(326, 8, 124, 39), mode="Quantity", method="Default",
    #                             keyword="", name="mall_resource_2")
    # O_MALL_RESOURCE_3 = RuleOcr(roi=(533, 9, 107, 38), area=(533, 9, 107, 38), mode="Quantity", method="Default",
    #                             keyword="", name="mall_resource_3")
    # O_MALL_RESOURCE_4 = RuleOcr(roi=(739, 8, 100, 39), area=(739, 8, 100, 39), mode="Quantity", method="Default",
    #                             keyword="", name="mall_resource_4")
    # O_MALL_RESOURCE_5 = RuleOcr(roi=(935, 11, 100, 37), area=(935, 11, 100, 37), mode="Quantity", method="Default",
    #                             keyword="", name="mall_resource_5")
    # O_MALL_RESOURCE_6 = RuleOcr(roi=(1129, 6, 100, 41), area=(1129, 6, 100, 41), mode="Quantity", method="Default",
    #                             keyword="", name="mall_resource_6")
    # image = cv2.imread(r"E:\2025-01-16225353.png")
    # print(O_MALL_RESOURCE_5.ocr_quantity(image))
    from module.atom.image import RuleImage
    image = cv2.imread(r"E:\Debug\MuMu-20260119-114931-173.png")

    I_SIDE_CHECK_SPECIAL = RuleImage(roi_front=(155,8,42,42), roi_back=(155,7,1115,90), threshold=0.7, method="Template matching", file="E:\Debug\\navbar_mall_sccales_check.png")

    #roi = I_SIDE_CHECK_SPECIAL.get_mall_resource_text_roi(image, threshold=0.7)

    rule = I_SIDE_CHECK_SPECIAL.build_mall_resource_ocr(
        image,
        threshold=0.7,
        name="mall_resource_1"
    )

    print(rule.ocr_quantity(image))

    pass
