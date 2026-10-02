# This Python file uses the following encoding: utf-8
import hashlib
from enum import Enum
from typing import Any, Dict

from pydantic import Field, ValidationError, model_serializer, model_validator

from tasks.Component.SwitchAccount.switch_account_config import AccountInfo
from tasks.Component.config_base import ConfigBase, TimeDelta
from tasks.Component.config_scheduler import Scheduler


class SubTaskType(str, Enum):
    """MultiTasks 每轮执行的单账号任务，单选。"""
    ACTIVITY_SIGN_IN = 'activity_sign_in'        # 活动签到
    ACTIVITY_SHIKIGAMI = 'activity_shikigami'    # 活动爬塔
    EXPERIENCE_YOUKAI = 'experience_youkai'      # 经验妖怪
    EVO_ZONE = 'evo_zone'                        # 觉醒副本
    ACCOUNT_EXPORT = 'account_export'            # 账号数据导出


class AccountSourceType(str, Enum):
    """账号来源方式，单选；三组来源参数常驻界面，只有选中的那组生效。"""
    OWN_LIST = 'own_list'                    # 本任务账号表
    CONFIG_SELECTION = 'config_selection'    # 勾选配置实例
    CHARACTERS = 'characters'                # 角色名列表


def account_config_field_name(config_name: str) -> str:
    """使用配置名稳定生成动态开关字段，避免规范化名称发生碰撞。"""
    digest = hashlib.sha256(config_name.encode('utf-8')).hexdigest()[:16]
    return f'config_{digest}'


def _count_multi_daily_accounts(data: dict) -> int:
    """统计 canonical 配置中 MultiDailyAltAcc 的有效账号条目数。"""
    task_data = data.get('multi_daily_alt_acc', {})
    return sum(
        1
        for key, value in task_data.items()
        if key.startswith('sup_account_list_')
        and isinstance(value, dict)
        and value.get('character')
    )


def active_account_configs(store) -> dict[str, tuple[str, int]]:
    """通过已初始化 Store 枚举当前 active 且含有效账号的配置。

    用 active_canonical_snapshots 一次拿齐 canonical：枚举后再逐个 load 会让每份配置
    多走一遍身份锁与严格校验（OASX 打开本任务参数页时最坏取 150 次文件锁）。
    锁超时仍由 Store 向上传播，任务不会把「暂时无法读取」误判为「没有选中账号」。
    """
    discovered = {}
    for config_name, canonical in store.active_canonical_snapshots().items():
        account_count = _count_multi_daily_accounts(canonical)
        if account_count == 0:
            continue
        discovered[account_config_field_name(config_name)] = (
            config_name,
            account_count,
        )
    return discovered


class AccountConfigSelection(ConfigBase, extra='allow'):
    # 动态 config_<sha256> 布尔字段由严格持久化边界校验，静态模型只负责保留 canonical 值。
    pass


class MultiTasksScheduler(Scheduler):
    # 多账号任务节奏：每日一次，失败 2 小时后重试
    priority: int = Field(default=5, description='priority_help')
    success_interval: TimeDelta = Field(default=TimeDelta(days=1), description='success_interval_help')
    failure_interval: TimeDelta = Field(default=TimeDelta(hours=2), description='failure_interval_help')


class MultiTasksConfig(ConfigBase):
    # 两个下拉框：跑哪个单账号任务、账号从哪来
    sub_task: SubTaskType = Field(default=SubTaskType.ACTIVITY_SIGN_IN, description='sub_task_help')
    account_source: AccountSourceType = Field(
        default=AccountSourceType.CONFIG_SELECTION, description='account_source_help')
    # 仅 account_source=own_list 生效
    sup_account_count: int = Field(default=1, ge=1, description='sup_account_count_help')
    # 仅 account_source=characters 生效
    account_characters: str = Field(
        default='', description='要执行的角色名，仅用英文逗号 , 分隔，例如 张三,李四')


class MultiTasks(ConfigBase):
    scheduler: MultiTasksScheduler = Field(default_factory=MultiTasksScheduler)
    multi_tasks_config: MultiTasksConfig = Field(default_factory=MultiTasksConfig)
    # 仅 account_source=config_selection 生效
    account_config_selection: AccountConfigSelection = Field(default_factory=AccountConfigSelection)
    # 仅 account_source=own_list 生效；裸 AccountInfo，不带任何子任务级开关
    # （经验妖怪的加成开关等一律沿用单账号任务自身的配置）
    sup_account_list: list[AccountInfo] = None

    @model_validator(mode='before')
    @classmethod
    def validator_all(cls, v: dict) -> Any:
        sup_account_count = v.get('multi_tasks_config', {}).get('sup_account_count', 1)

        def validator_list(list_name, data, item_type=None, list_size=1):
            if list_name not in data:
                data[list_name] = []

            remove_keys = []
            for key, value in data.items():
                if list_name == key or list_name not in key:
                    continue
                try:
                    item = item_type(**value)
                    # 保留空白和未填完的账号，逐字段编辑时不能丢内容或改变成员编号。
                    data[list_name].append(item)
                    remove_keys.append(key)
                except ValidationError:
                    pass
                except TypeError:
                    pass

            for key in remove_keys:
                del data[key]

            if item_type is not None:
                if len(data[list_name]) < list_size:
                    for _ in range(list_size - len(data[list_name])):
                        data[list_name].append(item_type())
        validator_list('sup_account_list', v, AccountInfo, sup_account_count)

        return v

    @model_serializer()
    def serializer_model(self, value: Any) -> Dict[str, Any]:
        properties = self.__dict__
        data = {}

        def v_dump(v):
            try:
                return v.model_dump()
            except AttributeError as e:
                from module.logger import logger
                logger.error(e)
                return v

        for key, value in properties.items():
            if isinstance(value, list):
                for index, v in enumerate(value):
                    data[f'{key}_{index + 1}'] = v_dump(v)
            else:
                data[key] = v_dump(value)
        return data
