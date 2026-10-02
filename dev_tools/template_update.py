# This Python file uses the following encoding: utf-8
# @author runhey
# github https://github.com/runhey
import json

from pathlib import Path

from module.config.config_model import ConfigModel
from module.config.config import Config
from module.logger import logger





if __name__ == "__main__":
    # ConfigModel 默认对象 → ConfigStore.replace_template 严格校验并原子替换 template
    from module.config.config_store import ConfigStore
    config = ConfigModel()
    store = ConfigStore(config_root=Path("./config"))
    store.replace_template(config.model_dump(mode="json"))
    logger.info("template updated")
