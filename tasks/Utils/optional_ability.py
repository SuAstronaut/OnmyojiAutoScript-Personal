# This Python file uses the following encoding: utf-8
"""可选能力探测入口；具体实现由本机扩展声明。"""
from tasks.Utils.optional_tasks import extend


def run_optional_ability(name: str, task) -> bool:
    """调用本机声明的可选能力，未声明时返回 False。"""
    return bool(extend('run_optional_ability', False, name, task))
