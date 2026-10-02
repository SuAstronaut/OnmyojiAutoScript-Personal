# This Python file uses the following encoding: utf-8
# @author runhey
# github https://github.com/runhey
import re
from copy import deepcopy

from cached_property import cached_property

from deploy.utils import DEPLOY_TEMPLATE, poor_yaml_read, poor_yaml_write
from module.base.timer import timer
from module.config.utils import *

class ConfigUpdater:

    def __init__(self, store=None) -> None:
        from pathlib import Path
        from module.config.config_store import ConfigStore
        # 实例配置的读取统一走注入 ConfigStore 的 canonical snapshot，避免裸读旁路
        self.store = store or ConfigStore(config_root=Path.cwd() / 'config')

    @cached_property
    def args(self):
        return read_file(filepath_args(filename='args'))

    @timer
    def update_template(self, template_name: str = "template") -> None:
        """
        更新模板 。从args.json更新
        :param template_name:
        :return:
        """
        pass

    @timer
    def update_config(self, config_name: str) -> None:
        """
        更新配置文件.从template更新
        :param config_name:
        :return:
        """
        pass

    def read_file(self, config_name, is_template=False):
        """
        Read config file via ConfigStore canonical snapshot.

        Args:
            config_name (str): ./config/{file}.json
            is_template (bool):

        Returns:
            dict:
        """
        try:
            return self.store.load_canonical_snapshot(config_name)
        except TimeoutError:
            # 锁超时是「暂时读不到」，不是「配置为空」：静默返回 {} 会让调用方据此
            # 做出错误决策（规格 §7 fail closed），必须向上传播。
            raise
        except (FileNotFoundError, ValueError):
            # 配置缺失、JSON 损坏或严格校验失败：保持旧 read_file 的缺失语义返回空 dict
            return {}
