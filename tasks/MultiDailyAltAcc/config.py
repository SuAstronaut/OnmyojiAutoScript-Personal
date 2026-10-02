from datetime import datetime, timedelta
from typing import Any, Dict

from pydantic import Field, BaseModel, model_validator, model_serializer, ValidationError

from deploy.logger import logger
from tasks.Component.SwitchAccount.switch_account_config import AccountInfo
from tasks.Component.config_base import ConfigBase
from tasks.Component.config_scheduler import Scheduler
from tasks.WantedQuests.config import CooperationSelectMaskDescription, CooperationSelectMask, CooperationType

class ExtendedAccountInfo(AccountInfo):
    # 继承所有AccountInfo的属性，并添加新属性
    alliedteam_battle_enable: bool = Field(default=False, description='是否开启同心队战斗')
    alliedteam_limit_count: int = Field(default=30, description='limit_count_help')
    # 邀请人数阈值：队友标记满该数量即进入下一流程，仅支持 1 或 2（默认2）
    alliedteam_invite_count: int = Field(default=2, ge=1, le=2, description='同心队需要邀请的队友人数(1或2)')
    alliedteam_ap_enable: bool = Field(default=True, description='是否开启补充体力')
    donatejade_enable: bool = Field(default=True, description='是否开启捐勾')
    courtyard_enable: bool = Field(default=True, description='是否开启庭院事务')
    mail_enable: bool = Field(default=True, description='是否开启领取邮件')
    cooperation_enable: bool = Field(default=True, description='是否开启寻找协作')
    # ================================================================
    # 以下任务建议单独开启（蹭卡和挂卡可以同时开启）
    # ================================================================
    returngift_enable: bool = Field(default=True, description='是否开启回礼')
    weekaward_enable: bool = Field(default=True, description='是否领取每周奖励')
    mysteryshop_enable: bool = Field(default=True, description='是否开启神秘商店')
    isflower: int = Field(default=0, ge=0, le=3, description='几花账号：0零花 1一花 2二花 3三花，决定神秘商店解锁哪些货')
    kekkaiActivation_enable: bool = Field(default=True, description='是否挂卡')
    KekkaiUtilize_enable: bool = Field(default=True, description='是否蹭卡')
    tree_planting_enable: int = Field(default=2, description='种树:0不运行 1买花 2买花捐树')
    trialbattle_enable: bool = Field(default=True, description='是否开启试炼战斗')
    summon_up_enable: bool = Field(default=True, description='是否开启UP召唤领取礼包')
    publish_sr_enable: bool = Field(default=True, description='是否发布SR碎片')
class MultiDailyAltAccConfig(ConfigBase):
    # 小号数
    sup_account_count: int = Field(default=1, ge=1, description='sup_account_count_help')
    # 关闭任务自动轮转：开启后不再做时间轮转（早晚轮 plan 物化、回礼轮/同心战斗轮
    # 阶段切换全部停用），下面的 total_* 开关只按用户手动勾选执行，下次运行时间
    # 沿用通用调度器的成功间隔。默认关闭＝维持原有的时间轮转行为。
    disable_task_rotation: bool = Field(default=False, description='关闭任务自动轮转：勾选后不随时间轮转任务，总开关只按手动勾选执行，运行时间沿用调度器设置')
    total_alliedteam_battle_enable: bool = Field(default=False, description='同心寮三十,建议单独开启（单独开启）')
    total_alliedteam_ap_enable: bool = Field(default=True, description='补充同心体力')
    total_donatejade_enable: bool = Field(default=True, description='捐勾')
    total_courtyard_enable: bool = Field(default=True, description='庭院事务')
    total_mail_enable: bool = Field(default=True, description='邮件')
    total_cooperation_enable: bool = Field(default=True, description='协作')
    total_returngift_enable: bool = Field(default=True, description='回礼')
    total_weekaward_enable: bool = Field(default=True, description='寄售券,蓝票,黑蛋领取')
    total_mysteryshop_enable: bool = Field(default=False, description='金蛇皮,逢魔皮,二花黑碎提醒')
    total_kekkaiActivation_enable: bool = Field(default=False, description='是否挂卡（只能和蹭卡/挂卡开启）')
    total_KekkaiUtilize_enable: bool = Field(default=False, description='是否蹭卡（只能和蹭卡/挂卡开启）')
    total_tree_planting_enable: int = Field(default=0, description='种树:0不运行 1买花 2买花捐树（单独开启）')
    total_trialbattle_enable: bool = Field(default=False, description='集结六张蓝票领取（单独开启）')
    total_summon_up_enable: bool = Field(default=False, description='活动UP召唤伴生礼包领取（需要等三天礼包解锁，单独开启）')
    total_publish_sr_enable: bool = Field(default=False, description='是否发布SR碎片（单独开启）')
    # ================================================================
    # 以下为配置字段并非任务
    # ================================================================
    shutdown_after_finish: bool = Field(default=False, description='0点-8点期间同心战斗完成后检测是否关机')
    # 协作整轮汇总：是否显示系统（安卓/iOS），默认开
    coop_notify_show_system: bool = Field(default=True, title='推送显示系统', description='推送显示系统')
    # 协作整轮汇总：是否显示账号/邮箱（默认关；开启后直接显示 account 原值）
    coop_notify_show_account: bool = Field(default=False, title='推送显示账号', description='推送显示账号')

class MultiDailyAltAcc(ConfigBase):
    scheduler: Scheduler = Field(default_factory=Scheduler)
    multi_daily_alt_acc_config: MultiDailyAltAccConfig = Field(default_factory=MultiDailyAltAccConfig)
    # 小号信息
    sup_account_list: list[ExtendedAccountInfo] = None
    def update_account_login_history(self, account: ExtendedAccountInfo):
        accountInfoList = self.sup_account_list
        for info in accountInfoList:
            # 同角色名可能属于不同邮箱或平台，必须按完整切号身份匹配。
            if (info.account, info.character, info.svr, info.apple_or_android) != (
                    account.account, account.character, account.svr, account.apple_or_android):
                continue
            info.last_complete_time = datetime.now()
            logger.info(f"update login history name:{info.character}  time :{info.last_complete_time}")
            return  info.last_complete_time

    @model_validator(mode='before')
    @classmethod
    def validator_all(cls, v: dict) -> Any:
        sup_account_count = v.get('multi_daily_alt_acc_config', {}).get('sup_account_count', 1)

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
                except ValidationError as e:
                    pass
                except TypeError as e:
                    pass

            for key in remove_keys:
                del data[key]

            if item_type is not None:
                if len(data[list_name]) < list_size:
                    for i in range(list_size - len(data[list_name])):
                        data[list_name].append(item_type())
        validator_list('sup_account_list', v, ExtendedAccountInfo, sup_account_count)

        return v

    @model_serializer()
    def serializer_model(self, value: Any) -> Dict[str, Any]:
        properties = self.__dict__
        data = {}

        def v_dump(v):
            try:
                return v.model_dump()
            except AttributeError as e:
                logger.error(e)
                return v

        for key, value in properties.items():
            if isinstance(value, list):
                for index, v in enumerate(value):
                    data[f'{key}_{index + 1}'] = v_dump(v)
            else:
                data[key] = v_dump(value)
        return data
