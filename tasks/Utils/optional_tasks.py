# This Python file uses the following encoding: utf-8
"""从本机文件加载可选扩展；公开源码不保存本机任务清单。"""
from functools import lru_cache
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
import sys


@lru_cache(maxsize=1)
def _load_extensions():
    """扩展只加载一次；缺失时使用公开默认值，加载错误则原样上抛。"""
    path = Path(__file__).resolve().parents[2] / '.local' / 'task_extensions.py'
    if not path.is_file():
        return None
    spec = spec_from_file_location('_oas_local_task_extensions', path)
    module = module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def extend(section: str, value, *args):
    """仅调用本机已声明的扩展点，未声明时保持调用方原值。"""
    callback = getattr(_load_extensions(), section, None)
    return value if callback is None else callback(value, *args)
