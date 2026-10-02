from tasks.DailyAltAcc.config import  DailyAltAccConfig,GeneralBattleConfig,DailyAltAcc
from tasks.Component.SchedulingShield import shield_scheduling
def get_config(self):
    config=DailyAltAcc()
    config.daily_alt_acc_config.alliedteam_battle_enable = self.account_info.alliedteam_battle_enable
    config.daily_alt_acc_config.alliedteam_ap_enable = self.account_info.alliedteam_ap_enable
    # 同心战斗自动模式与纸人设置：不加 Multi 侧配置，直接镜像大号配置文件里
    # DailyAltAcc 的当前值——大号是什么，所有小号就是什么
    config.daily_alt_acc_config.alliedteam_auto_battle_enable = \
        self.config.daily_alt_acc.daily_alt_acc_config.alliedteam_auto_battle_enable
    config.daily_alt_acc_config.alliedteam_paper_settings_enable = \
        self.config.daily_alt_acc.daily_alt_acc_config.alliedteam_paper_settings_enable
    config.daily_alt_acc_config.mail_enable = self.account_info.mail_enable
    config.daily_alt_acc_config.donatejade_enable = self.account_info.donatejade_enable
    config.daily_alt_acc_config.courtyard_enable = self.account_info.courtyard_enable
    config.daily_alt_acc_config.alliedteam_limit_count = self.account_info.alliedteam_limit_count
    config.daily_alt_acc_config.alliedteam_invite_count = self.account_info.alliedteam_invite_count
    config.daily_alt_acc_config.cooperation_enable = self.account_info.cooperation_enable
    config.daily_alt_acc_config.returngift_enable = self.account_info.returngift_enable
    config.daily_alt_acc_config.weekaward_enable = self.account_info.weekaward_enable
    config.daily_alt_acc_config.mysteryshop_enable = self.account_info.mysteryshop_enable
    config.daily_alt_acc_config.isflower = self.account_info.isflower
    config.daily_alt_acc_config.kekkaiActivation_enable = self.account_info.kekkaiActivation_enable
    config.daily_alt_acc_config.KekkaiUtilize_enable = self.account_info.KekkaiUtilize_enable
    config.daily_alt_acc_config.tree_planting_enable = self.account_info.tree_planting_enable
    config.daily_alt_acc_config.trialbattle_enable = self.account_info.trialbattle_enable
    config.daily_alt_acc_config.summon_up_enable = self.account_info.summon_up_enable
    config.daily_alt_acc_config.publish_sr_enable = self.account_info.publish_sr_enable
    return config
def run_success(self):
    self.daily_conf.update_account_login_history
    self.config.save()


# 多账号批量执行时必须屏蔽的调度任务名。
# 这些任务在 MultiDailyAltAcc 里都是「被小号借用」的：
# - DailyAltAcc：本任务就是它的多账号版本，每个小号都改一次大号的下次运行时间纯属污染；
# - KekkaiActivation / KekkaiUtilize：DailyAltAcc 内部用写死的 DAILY 配置跑挂卡与寄养，
#   与大号自己的挂卡/寄养设置无关，却会把大号的下次运行时间拖到几分钟后甚至当下。
# 其余任务（如流程中处理悬赏邀请产生的 WantedQuests）一律原样转发，与
# MultiTasks 的 Adapter 策略保持一致。
BLOCKED_SELF: tuple[str, ...] = ('DailyAltAcc',)
BLOCKED_NESTED: tuple[str, ...] = ('KekkaiActivation', 'KekkaiUtilize')
OWNER = 'MultiDailyAltAcc'


def shield_self(base_cls: type) -> type:
    """屏蔽 DailyAltAcc 自身调度的子类。"""
    return shield_scheduling(base_cls, BLOCKED_SELF, OWNER)


def create_nested_task(self, task_cls: type):
    """覆写 DailyAltAcc._create_nested_task：嵌套子任务换成屏蔽调度的子类。

    挂卡/寄养是 DailyAltAcc 内部另行实例化的独立对象，走的是自己的
    BaseTask.set_next_run，注入在 DailyAltAcc 上的 set_next_run 覆写拦不住它们，
    必须在创建这一步换成屏蔽后的子类。
    """
    return shield_scheduling(task_cls, BLOCKED_NESTED, OWNER)(self.config, self.device)
