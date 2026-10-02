# This Python file uses the following encoding: utf-8
# @author runhey
# github https://github.com/runhey

import cv2
import re
import cn2an
import itertools

from datetime import timedelta

from module.exception import ScriptError
from module.base.utils import area_pad, crop, float2str
from module.ocr.base_ocr import BaseCor, OcrMode, OcrMethod
from module.ocr.utils import merge_area
from module.logger import logger


class Full(BaseCor):
    """
    这个类适用于大ROI范围的文本识别。可以支持多条文本识别， 默认不支持竖方向的文本识别
    """
    def after_process(self, result):
        return result

    def ocr_full(self, image, keyword: str=None) -> tuple:
        """
        检测整个图片的文本,并对结果进行过滤。返回的是匹配到的keyword的左边。如果没有匹配到返回(0, 0, 0, 0)
        :param image:
        :param keyword:
        :return:
        """
        if keyword is None:
            keyword = self.keyword

        boxed_results = self.detect_and_ocr(image)
        
        if not boxed_results:
            return 0, 0, 0, 0

        index_list = self.filter(boxed_results, keyword)
        logger.info(f"OCR [{self.name}] detected in {index_list}")
        # 如果一个都没有匹配到
        if not index_list:
            return 0, 0, 0, 0

        # 如果匹配到了多个,则合并所有的坐标，返回合并后的坐标
        if len(index_list) > 1:
            area_list = [(
                #boxed_results[index].box[0, 0],  # x
                #boxed_results[index].box[0, 1],  # y
                #boxed_results[index].box[1, 0] - boxed_results[index].box[0, 0],     # width
                #boxed_results[index].box[2, 1] - boxed_results[index].box[0, 1],     # height
                boxed_results[index].box[0][0],  # x
                boxed_results[index].box[0][1],  # y
                boxed_results[index].box[1][0] - boxed_results[index].box[0][0],     # width
                boxed_results[index].box[2][1] - boxed_results[index].box[0][1],     # height
            ) for index in index_list]
            area = merge_area(area_list)
            print(area)
            area = area[0]+self.roi[0], area[1]+self.roi[1], area[2], area[3]
        else:
            box = boxed_results[index_list[0]].box
            #self.area = box[0, 0]+self.roi[0], box[0, 1]+self.roi[1], box[1, 0] - box[0, 0], box[2, 1] - box[0, 1]
            area = box[0][0]+self.roi[0], box[0][1]+self.roi[1], box[1][0] - box[0][0], box[2][1] - box[0][1]
        logger.info(f"OCR [{self.name}] detected in {area} self.area {self.area}")
        if self.area == [0,0,100,100]:
            self.area = area
        return area

class Single(BaseCor):
    """
    这个类使用于单行文本识别（所识别的ROI不会动）
    """
    def after_process(self, result):
        return result

    def ocr_single(self, image) -> str:
        """
        检测某个固定位置的roi的文本。可以是横方向也可以是竖方向
        :param image:
        :return: 返回到识别的文字, 如果没有返回空字符串
        """
        if self.roi:
            result = self.ocr_single_line(image)
            if result != "":
                # 带 keyword 的纯 Single 未严格命中时，可能是多行文字被整图单行
                # 识别按列乱序（两行「进入/游戏」读成「讲游入戏」），值得回退到
                # 检测路径重试；其余情况（无 keyword / 非纯 Single / 已命中）维
                # 持原行为直接返回
                if not self.keyword or self.mode != OcrMode.SINGLE or result == self.keyword:
                    return result
                logger.info(f"[{self.name}] Single result [{result}] mismatch keyword [{self.keyword}], retry with detect")
            else:
                # 如果没有识别到，这个时候考虑到可能是竖方向的文本, 使用detect_and_ocr来进行识别
                logger.info(f"[{self.name}] Try to detect vertically")

            # 回退 detect_and_ocr：先检测文本行再逐框识别，与 annotator 测试路径
            # 一致；多行文字（竖排单列除外）只有这条路能识别正确
            boxed_results = self.detect_and_ocr(image)
            if not boxed_results:
                logger.info(f"[{self.name}]: No text detected in ROI")
                return result
            # 拼接全部识别框文本：多行文字的 keyword 跨框（「进入」+「游戏」），
            # 只取首框会永远差半截
            texts = [boxed_result.ocr_text for boxed_result in boxed_results if boxed_result.ocr_text]
            if texts:
                return "".join(texts)

            # 如果还是没有识别到。那可能就是真的没有识别到了
            return result
        else:
            raise ScriptError("Roi is empty")

class Digit(Single):

    def after_process(self, result):
        result = super().after_process(result)
        replacements = {
            'I': '1', 'D': '0', 'S': '5',
            'B': '8', '？': '2', '?': '2',
            'd': '6', 'o': '0', 'O': '0',
            '→': '1'
        }

        for old, new in replacements.items():
            result = result.replace(old, new)

        result = [char for char in result if char.isdigit()]
        result = ''.join(result)

        prev = result
        result = int(result) if result else 0
        if str(result) != prev:
            logger.warning(f'OCR {self.name}: Result "{prev}" is revised to "{result}"')

        return result

    def ocr_digit(self, image) -> int:
        """
        返回数字
        :param image:
        :return:
        """
        result = self.ocr_single(image)

        if result == "":
            return 0
        else:
            return int(result)

class DigitCounter(Single):
    def after_process(self, result):
        result = super().after_process(result)
        replacements = {
            'I': '1', 'D': '0', 'S': '5',
            'B': '8', '？': '2', '?': '2',
            'd': '6', 'o': '0', 'O': '0',
            '→': '1'
        }

        for old, new in replacements.items():
            result = result.replace(old, new)

        result = [char for char in result if char.isdigit() or char == '/']
        result = ''.join(result)
        return result

    @classmethod
    def ocr_str_digit_counter(cls, result: str) -> tuple[int, int, int]:
        result = re.search(r'(\d+)/(\d+)', result)
        if result:
            result = [int(s) for s in result.groups()]
            current, total = int(result[0]), int(result[1])
            # 不知道为什么加了这一句，妈的
            # current = min(current, total)
            if current > total:
                logger.warning(f'[{cls.name}]: Current {current} is greater than total {total}')
            return current, total - current, total
        else:
            logger.warning(f'Unexpected ocr result: {result}')
            return 0, 0, 0


    def ocr_digit_counter(self, image) -> tuple[int, int, int]:
        """
        获取计数的结果
        :param image:
        :return: 例如 14/15，返回 (14, 1, 15) 。如果没有识别到，返回 (0, 0, 0)
        """
        result = self.ocr_single(image)
        if result == "":
            return 0, 0, 0
        return self.ocr_str_digit_counter(result)

class Duration(Single):
    def after_process(self, result):
        result = result.replace('I', '1').replace('D', '0').replace('S', '5')
        result = result.replace('o', '0').replace('l', '1').replace('O', '0')
        result = result.replace('B', '8').replace('：', ':').replace(' ', '').replace('.', ':')
        # OCR 常见误识别：冒号:被识别为8
        # 时间格式应为 H:MM:SS 或 HH:MM:SS，包含2个冒号
        # 如果冒号数不足2个，尝试将特定位置的8还原为冒号
        colon_count = result.count(':')
        if colon_count < 2:
            fixed = self._fix_colon_as_eight(result)
            if fixed != result:
                logger.info(f'OCR {self.name}: Duration fix "8"->":" in "{result}" -> "{fixed}"')
                result = fixed
        result = super().after_process(result)
        return result

    @staticmethod
    def _fix_colon_as_eight(string):
        """
        修复OCR将冒号:误识别为8的情况
        时间格式 H:MM:SS 或 HH:MM:SS
        """
        # 找出所有可能是误识别冒号的8的位置
        eight_positions = [i for i, c in enumerate(string) if c == '8']

        # 最多替换2个8（时间格式有2个冒号）
        max_replace = min(len(eight_positions), 2)

        for num_replace in range(1, max_replace + 1):
            for positions in itertools.combinations(eight_positions, num_replace):
                candidate = list(string)
                for pos in positions:
                    candidate[pos] = ':'
                candidate = ''.join(candidate)
                # 验证替换后是否包含合法的时间格式
                match = re.search(r'(\d{1,2}):(\d{2}):(\d{2})', candidate)
                if match:
                    h, m, s = int(match.group(1)), int(match.group(2)), int(match.group(3))
                    # 分钟和秒数必须 < 60
                    if m < 60 and s < 60:
                        return match.group(0)
        return string

    @staticmethod
    def parse_time(string):
        """
        Args:
            string (str): `01:30:00`

        Returns:
            datetime.timedelta:
        """
        result = re.search(r'(\d{1,2}):?(\d{2}):?(\d{2})', string)
        if result:
            result = [int(s) for s in result.groups()]
            return timedelta(hours=result[0], minutes=result[1], seconds=result[2])
        else:
            logger.warning(f'Invalid duration: {string}')
            return timedelta(hours=0, minutes=0, seconds=0)

    def ocr_duration(self, image) -> timedelta:
        """

        :param image:
        :return:
        """
        result = self.ocr_single(image)

        if result == "":
            return timedelta(hours=0, minutes=0, seconds=0)

        return self.parse_time(result)

class Quantity(BaseCor):
    """
    专门用于识别超级多的数量，不支持多个区域的识别
    可支持负数
    比如：”6.33亿“ ”1.2万“ “53万/100” -> 530,000
    """
    def after_process(self, result):
        result = super().after_process(result)
        result = result.replace('I', '1').replace('D', '0').replace('S', '5')
        result = result.replace('B', '8').replace('？', '2').replace('?', '2').replace('d', '6')
        result = [char
                  for char in result
                  if char.isdigit() or char == '.' or char == '/' or char == '万' or char == '亿' or char == '千']
        result = ''.join(result)

        if '/' in result:
            result_split = result.split('/')
            result = result_split[0]
        result = cn2an.cn2an(result, 'smart')

        try:
            result = int(result)
        except ValueError:
            logger.warning(f'[{self.name}]: Invalid quantity: {result}')
            result = 0
        return result

    def ocr_quantity(self, image) -> int:
        """
        返回数量
        :param image:
        :return:
        """
        boxed_results = self.detect_and_ocr(image)
        if not boxed_results:
            logger.warning(f'[{self.name}]: No text detected')
            return 0

        box = boxed_results[0].box
        #self.area = box[0, 0] + self.roi[0], box[0, 1] + self.roi[1], box[1, 0] - box[0, 0], box[2, 1] - box[0, 1]
        self.area = box[0][0] + self.roi[0], box[0][1] + self.roi[1], box[1][0] - box[0][0], box[2][1] - box[0][1]
        return boxed_results[0].ocr_text



if __name__ == '__main__':
    import cv2
    #image = cv2.imread(r'E:\Project\OnmyojiAutoScript-assets\jade.png')
    
    image = cv2.imread(r'd:\MuMu12-20250704-215030.png')
