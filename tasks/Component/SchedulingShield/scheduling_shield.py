# This Python file uses the following encoding: utf-8
"""调度屏蔽工具：复用别的任务的 run() 时，丢弃它写死的调度副作用。

背景：BaseTask.set_next_run() 最终落到 Config.task_delay()，按**任务名**去改
config/<实例>.json 里对应任务的 scheduler.next_run。它不认「是谁在跑」，只认名字。
所以一个任务复用另一个任务的 run() 时，被复用任务里写死的任务名就会去改那个
单账号任务的下次运行时间——多账号场景下等于把小号干的活算到大号头上。

注意：覆写 set_next_run 只能拦住**同一个对象**发出的调度。如果被复用的任务在
内部又实例化了别的任务（XxxTask(self.config, self.device).run()），那个新实例走的是
自己的 BaseTask.set_next_run，本工具拦不到——必须在「创建那一刻」就换成屏蔽子类。
"""
from module.logger import logger


def shield_scheduling(base_cls: type, blocked: tuple[str, ...], owner: str) -> type:
    """返回 base_cls 的子类，按任务名屏蔽调度副作用，其余任务原样转发。

    只有 task 参与过滤判断，其他参数不做解释，因此收 **kwargs 转发：
    基类 set_next_run 新增参数（如 persist）时无需同步修改这里，
    避免调用方传新参数时抛 TypeError。

    @param base_cls: 被复用的任务类
    @param blocked: 需要屏蔽的任务名，命中则直接丢弃该次调度
    @param owner: 复用方任务名，仅用于日志前缀，便于定位是谁屏蔽的
    """

    def set_next_run(self, task: str, **kwargs) -> None:
        if task in blocked:
            logger.info(f'[{owner}] 屏蔽子任务调度: {task}')
            return
        # 直接走 base_cls 的原实现（通常来自 BaseTask），不用 super()：
        # 本方法可能被 type() 注入到旁系子类上，那时零参 super() 没有 __class__
        # 闭包单元会直接 RuntimeError，显式 super() 也会因实例类型不匹配抛 TypeError。
        base_cls.set_next_run(self, task=task, **kwargs)

    return type(f'_Shielded{base_cls.__name__}', (base_cls,), {'set_next_run': set_next_run})
