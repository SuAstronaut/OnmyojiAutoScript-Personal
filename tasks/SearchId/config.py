from pydantic import BaseModel
from tasks.Component.config_base import ConfigBase
from pydantic import Field
from tasks.Component.config_scheduler import Scheduler

class SearchIdConfig(ConfigBase):
    """
    SearchId任务的配置类
    """
    batch_mode_enabled: bool = True
    csv_file_path: str = "./data.csv"
    target_id: str = "#1234567890"

class SearchId(ConfigBase):
    scheduler: Scheduler = Field(default_factory=Scheduler)
    search_id_config: SearchIdConfig = Field(default_factory=SearchIdConfig)