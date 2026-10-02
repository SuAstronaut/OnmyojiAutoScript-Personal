# This Python file uses the following encoding: utf-8
# @author runhey
# github https://github.com/runhey

import json

from module.config.config import Config
from module.config.utils import convert_to_underscore
from module.config.config_model import ConfigModel

from module.logger import logger
from pydantic import BaseModel, ValidationError



class ConfigModify(Config):
    """
    这个类的出现是为了修补一个架构问题：
    不同于Alas,我默认用户在GUI界面点击的时候就启动了脚本进程，初始化会直接同时初始化一个config和一个device
    如果用户配置不对这个进程直接挂掉了，用户甚至没有修改config的机会，即时重启了也会由于没有修改config而再次挂掉

    因此这个类的出现是为了在脚本进程挂掉的时候，用户可以修改config，然后再次启动脚本进程


    """

    def __init__(self, config: str, store=None) -> None:
        super().__init__(config, store=store)


    def gui_args(self, task: str) -> str:
        """
        获取给gui显示的参数
        :return:
        """
        return super().gui_args(task=task)

    def gui_task(self, task: str) -> str:
        """
        获取给gui显示的任务 的参数的具体值
        :return:
        """
        return self.model.gui_task(task=task)

    def gui_set_task(self, task: str, group: str, argument: str, value) -> bool:
        """
        设置给gui显示的任务 的参数的具体值
        统一走 ConfigStore 的 patch_user_argument，脚本是否存活不改变持久化路径
        :return:
        """
        try:
            result = self.store.patch_user_argument(self.config_name, task, group, argument, value)
            if result.success:
                logger.info(f'gui_set_task {task}.{group}.{argument}.{value}')
            return result.success
        except Exception as e:
            logger.error(f'gui_set_task {task}.{group}.{argument}.{value} failed: {e}')
            return False

    def gui_task_list(self) -> str:
        """
        获取给gui显示的任务列表
        :return:
        """
        result = {}
        for key, value in self.model.dict().items():
            if isinstance(value, str):
                continue
            if key == "restart":
                continue
            if "scheduler" not in value:
                continue

            scheduler = value["scheduler"]
            item = {"enable": scheduler["enable"],
                    "next_run": str(scheduler["next_run"])}
            key = self.config.model.type(key)
            result[key] = item
        return json.dumps(result)
