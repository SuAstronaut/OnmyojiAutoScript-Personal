# This Python file uses the following encoding: utf-8
"""MultiTasks 可选子任务的注册表与 Adapter。

MultiTasks 不复制任何子任务逻辑：直接复用单账号 ScriptTask 的 run()，
只用 Component 层的 shield_scheduling 屏蔽不适用于批量执行的调度副作用
（详见 tasks/Component/SchedulingShield/scheduling_shield.py 的说明）。
"""
from dataclasses import dataclass

from tasks.AccountExport.script_task import ScriptTask as AccountExportTask  # 独立导出流程。
from tasks.ActivityShikigami.script_task import ScriptTask as ActivityShikigamiTask
from tasks.ActivitySignIn.script_task import ScriptTask as ActivitySignInTask
from tasks.Component.SchedulingShield import shield_scheduling
from tasks.EvoZone.script_task import ScriptTask as EvoZoneTask
from tasks.ExperienceYoukai.script_task import ScriptTask as ExperienceYoukaiTask
from tasks.MultiTasks.config import SubTaskType

# 屏蔽日志的前缀，标明是谁屏蔽的
OWNER = 'MultiTasks'


@dataclass(frozen=True)
class SubTaskSpec:
    """一个可选子任务的规格。

    :param base_cls: 单账号 ScriptTask 类，其 run() 被原样复用
    :param task_end_name: 正常结束时 TaskEnd 携带的任务名，用于判定当前账号完成
    :param suppressed: 要屏蔽的 set_next_run 任务名，直接喂给 shield_scheduling
    """
    base_cls: type
    task_end_name: str
    suppressed: tuple[str, ...]


SUB_TASKS: dict[SubTaskType, SubTaskSpec] = {
    # 屏蔽自身调度：否则每个账号都会改动对应单账号任务的下次运行时间
    SubTaskType.ACTIVITY_SIGN_IN: SubTaskSpec(
        ActivitySignInTask, 'ActivitySignIn', ('ActivitySignIn',)),
    # 额外屏蔽 SoulsTidy：多账号爬塔不清理御魂（沿用旧 MultiActivityShikigami 行为）
    SubTaskType.ACTIVITY_SHIKIGAMI: SubTaskSpec(
        ActivityShikigamiTask, 'ActivityShikigami',
        ('ActivityShikigami', 'SoulsTidy')),
    SubTaskType.EXPERIENCE_YOUKAI: SubTaskSpec(
        ExperienceYoukaiTask, 'ExperienceYoukai', ('ExperienceYoukai',)),
    # 觉醒副本：配置全部继承该实例 EvoZone 段（麒麟/层数/次数/换御魂/邀请设置），
    # 本任务不另开一份参数。run() 结尾的 set_next_run('EvoZone') 必须屏蔽，
    # 否则每个小号都会去改大号觉醒副本的下次运行时间。
    SubTaskType.EVO_ZONE: SubTaskSpec(
        EvoZoneTask, 'EvoZone', ('EvoZone',)),
    # 批量导出复用单账号任务，成功和失败都屏蔽单账号调度。
    SubTaskType.ACCOUNT_EXPORT: SubTaskSpec(
        AccountExportTask, 'AccountExport', ('AccountExport',)),
}

# Adapter 类在模块导入时建一次；实例每账号新建，保证可变状态不跨账号共享。
# 必须在「创建那一刻」就用屏蔽子类：覆写 set_next_run 只能拦住同一个对象发出的
# 调度，事后打补丁拦不住子任务内部另行实例化的任务。
ADAPTERS: dict[SubTaskType, type] = {
    key: shield_scheduling(spec.base_cls, spec.suppressed, OWNER)
    for key, spec in SUB_TASKS.items()
}
