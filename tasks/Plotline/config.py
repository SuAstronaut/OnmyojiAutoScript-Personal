from datetime import datetime, timedelta
from typing import Any, Dict

from pydantic import Field, BaseModel, model_validator, model_serializer, ValidationError

from deploy.logger import logger
from tasks.Component.SwitchAccount.switch_account_config import AccountInfo
from tasks.Component.config_base import ConfigBase, DateTime
from tasks.Component.config_scheduler import Scheduler
from tasks.WantedQuests.config import CooperationSelectMaskDescription, CooperationSelectMask, CooperationType
from tasks.Component.GeneralBattle.config_general_battle import GeneralBattleConfig

class PlotlineConfig(ConfigBase):
    switch_system_shikigami: bool = Field(default=True)
    experience_youkai_battle: bool = Field(default=True)
    exploration_battle_lock: bool = Field(default=False)
    pass

class Plotline(ConfigBase):
    scheduler: Scheduler = Field(default_factory=Scheduler)
    plotline_config: PlotlineConfig = Field(default_factory=PlotlineConfig)


    