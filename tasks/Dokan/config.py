# This Python file uses the following encoding: utf-8
# @brief    Configurations for Ryou Dokan Toppa (阴阳竂道馆突破配置)
# @author   jackyhwei
# @note     draft version without full test
# github    https://github.com/roarhill/oas

from pydantic import BaseModel, Field
from tasks.Component.GeneralBattle.config_general_battle import GeneralBattleConfig
from tasks.Component.SwitchSoul.switch_soul_config import SwitchSoulConfig
from tasks.Component.config_base import ConfigBase
from tasks.Component.config_scheduler import Scheduler


class WelfareConfig(BaseModel):
    # 是否只开福利寮
    welfare_enable: bool = Field(default=False, description='是否优先开启福利寮')
    # 福利寮标签关键字，多个用逗号分隔，OCR 识别到的标签匹配其中任意一个即视为福利寮
    welfare_tags: str = Field(default='鑫', description='福利寮标签关键字，多个用逗号分隔如鑫,星')
    # 福利寮刷新次数
    fresh_num: int = Field(default=5, description='福利寮刷新次数')
    # 福利寮最少人数限制
    min_people_num: int = Field(default=-1, description='福利寮最少人数')
    # 刷新多少次才开始减少人数
    fresh_num_less_people: int = Field(default=20, description='刷新多少次才开始减少人数')
    # 是否发送请求检查福利寮开启
    enable_get_requests: bool = Field(default=False, description='是否发送请求检查福利寮开启')
    get_requests_url: str = Field(default='', description='获取请求的URL')


class DokanConfig(BaseModel):
    # # 寮管理开启道馆
    dokan_enable: bool = Field(default=False, description='寮管理开启道馆')
    # 刷新次数
    fresh_num: int = Field(default=5, description='刷新次数')
    # 道馆系数,赏金/人数 根据喜好配置
    find_dokan_score: float = Field(default=4.6, description='dokan_score_help')
    # 道馆最小人数限制
    min_people_num: int = Field(default=-1, description='min_people_num_help')
    # 最少赏金设置
    min_bounty: int = Field(default=0, description='min_bounty_help')

    # # 选择哪一个竂
    # dokan_declear_war_priority: int = Field(default=0, description='dokan_declear_war_priority_help')

    # 攻击优先顺序: 见习=0,初级=1...
    # dokan_attack_priority: int = Field(default=0, description='dokan_attack_priority_help')

    # 失败CD后自动加油
    # dokan_auto_cheering_while_cd: bool = Field(default=False, description='dokan_auto_cheering_while_cd_help')

    # 正式进攻会设定 2s - 10s 的随机延迟，避免攻击间隔及其相近被检测为脚本。
    # random_delay: bool = Field(default=False, description='random_delay_help')

    # 防封：使用固定的随机区域进行随机点击，若为False将自动识别当前画面中的最大纯色区域作为随机点击区域
    # anti_detect_click_fixed_random_area: bool = Field(default=False, description='anti_detect_click_fixed_random_area_help')

    enable_weekend_run: bool = Field(default=False, description='启用周末运行')
    run_on_friday: bool = Field(default=False, description='周五运行')
    run_on_saturday: bool = Field(default=False, description='周六运行')
    run_on_sunday: bool = Field(default=False, description='周日运行')


class QQGroupTriggerConfig(BaseModel):
    # 是否启用QQ群消息触发道馆
    enable: bool = Field(default=False, description='是否启用QQ群消息触发道馆')
    # OneBot HTTP API地址 (go-cqhttp / NapCat / Lagrange 等)
    endpoint: str = Field(default='http://127.0.0.1:3000', description='OneBot HTTP API地址')
    # access_token (可选)
    access_token: str = Field(default='', description='OneBot access_token')
    # 监听的QQ群号
    group_id: int = Field(default=0, description='监听的QQ群号')
    # 触发道馆创建关键词(由特定成员发送)
    create_keyword: str = Field(default='道馆已经创建', description='道馆创建关键词，由指定成员发送')
    # 发送道馆创建关键词的QQ号，为0则不限制发送者
    create_sender_id: int = Field(default=0, description='发送道馆创建关键词的QQ号，0表示不限制发送者')
    # 发送@全体成员的QQ号，为0则与create_sender_id一致
    at_all_sender_id: int = Field(default=0, description='发送@全体成员的QQ号，0表示与create_sender_id一致')
    # 是否要求同时检测到@全体成员消息才触发
    require_at_all: bool = Field(default=True, description='是否要求同时检测到@全体成员消息才触发')
    # 未检测到触发消息时，重试间隔(分钟)
    retry_interval: int = Field(default=5, description='未检测到触发消息时重试间隔(分钟)')


class Dokan(ConfigBase):
    scheduler: Scheduler = Field(default_factory=Scheduler)
    welfare_config: WelfareConfig = Field(default_factory=WelfareConfig)
    dokan_config: DokanConfig = Field(default_factory=DokanConfig)
    qq_group_trigger: QQGroupTriggerConfig = Field(default_factory=QQGroupTriggerConfig)
    general_battle_config: GeneralBattleConfig = Field(default_factory=GeneralBattleConfig,description='正常寮预设')
    general_battle_config2: GeneralBattleConfig = Field(default_factory=GeneralBattleConfig,description='福利寮预设')
    general_battle_config_master: GeneralBattleConfig = Field(default_factory=GeneralBattleConfig,description='馆主战阵容预设')
    switch_soul_config: SwitchSoulConfig = Field(default_factory=SwitchSoulConfig,description='正常寮切换御魂')
    switch_soul_config2: SwitchSoulConfig = Field(default_factory=SwitchSoulConfig,description='福利寮切换御魂')
    switch_soul_config_master: SwitchSoulConfig = Field(default_factory=SwitchSoulConfig,description='馆主战切换御魂')
