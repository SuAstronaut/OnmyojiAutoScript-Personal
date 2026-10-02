from module.logger import logger
from module.device.device import Device
from module.device.connection import Connection
from typing import Union  # 添加Union类型导入

class RuleInput(Connection):
    """
    输入规则类，提供多种方式在模拟器/设备上输入字符和数字
    """
    
    def __init__(self, device: Device):
        self.device = device

    def input_text(self, text: str):
        """
        在设备上输入文本（包括字符和数字）
        注意：这要求设备上有一个可以接收输入的文本框处于焦点状态
        
        Args:
            text (str): 要输入的文本
        """
        # 桌面客户端模式没有 adb/uiautomator2，改用 Windows 消息注入
        if self.device.is_desktop:
            self.device.input_text_desktop(text)
            logger.info(f"成功输入文本: {text}")
            return
        # 使用设备的输入方法直接输入
        try:
            # 直接使用u2的send_keys方法输入
            self.device.u2.send_keys(text)
            
            logger.info(f"成功输入文本: {text}")
        except Exception as e:
            logger.error(f"输入文本时发生错误: {e}")

    def input_text_alternative(self, text: str):
        """
        使用替代方法输入文本（逐字符输入）
        对于某些特殊字符或语言，这种方法可能更有效
        
        Args:
            text (str): 蟊要输入的文本
        """
        # 桌面端逐字符与一次性输入走同一条 Windows 消息注入路径
        if self.device.is_desktop:
            self.device.input_text_desktop(text)
            logger.info(f"逐字符输入文本: {text}")
            return
        try:
            for char in text:
                if char == ' ':
                    # 输入空格
                    self.device.u2.shell('input keyevent KEYCODE_SPACE')
                elif char.isdigit():
                    # 数字
                    self.device.u2.shell(f'input keyevent KEYCODE_{char}')
                elif char.isalpha():
                    # 字母
                    self.device.u2.send_keys(char)
                else:
                    # 特殊字符
                    self.device.u2.send_keys(char)
                    
            logger.info(f"逐字符输入文本: {text}")
        except Exception as e:
            logger.error(f"逐字符输入文本时发生错误: {e}")

    def input_number(self, number: Union[int, float, str]):
        """
        输入数字
        
        Args:
            number (int/float/str): 要输入的数字
        """
        # 桌面端数字输入同样走 Windows 消息注入
        if self.device.is_desktop:
            self.device.input_text_desktop(str(number))
            logger.info(f"成功输入数字: {number}")
            return
        try:
            number_str = str(number)
            for digit in number_str:
                if digit == '.':
                    self.device.u2.shell('input keyevent KEYCODE_PERIOD')
                elif digit == '-':
                    self.device.u2.shell('input keyevent KEYCODE_MINUS')
                elif digit.isdigit():
                    self.device.u2.send_keys(digit)
                    
            logger.info(f"成功输入数字: {number_str}")
        except Exception as e:
            logger.error(f"输入数字时发生错误: {e}")