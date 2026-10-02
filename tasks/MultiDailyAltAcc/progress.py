# This Python file uses the following encoding: utf-8
"""MultiDailyAltAcc 子任务进度持久化（薄封装）。

通用实现位于 tasks/Component/MultiAccountRunner/progress.py，本模块只做两件事：
1. 固定 task_name='multi_daily'，保证进度文件名仍为 multi_daily_progress_<config>.json；
2. 提供 MultiDailyAltAcc 专属的阶段判定辅助（phase_flags_of / phase_id_of）。

本模块只做文件读写，不依赖 device / Config，便于单元测试。
"""
import json
from datetime import datetime
from pathlib import Path

from filelock import FileLock

from module.logger import logger

from tasks.Component.MultiAccountRunner.progress import (
    # 重新导出，保持既有 import 面不变：
    # tasks/MultiDailyAltAcc/script_task.py 与 tasks/DailyAltAcc/script_task.py 均从此处取
    FALSE_LIMIT,
    STALE_HOURS,
    STATUS_DONE,
    STATUS_FAILED,
    STATUS_PENDING,
    STATUS_SKIPPED,
    ProgressStore as _BaseProgressStore,
    _write_json_atomic,
    acc_key,
    phase_flags_match,
)

__all__ = [
    'FALSE_LIMIT',
    'STALE_HOURS',
    'STATUS_DONE',
    'STATUS_FAILED',
    'STATUS_PENDING',
    'STATUS_SKIPPED',
    'COOP_ARCHIVE_LIMIT',
    'ProgressStore',
    'acc_key',
    'phase_flags_of',
    'phase_id_of',
]

# 协作归档保留份数：只保留最近 N 份，避免无限积累（轻量容量策略）
COOP_ARCHIVE_LIMIT = 5


class ProgressStore(_BaseProgressStore):
    """MultiDailyAltAcc 的进度存储，固定文件名 multi_daily_progress_<config>.json。

    协作汇总记录挂在本进度文件的顶层 ``coop`` 列表，跟随现有 ensure_phase / clear
    生命周期：与账号进度共用同一轮次边界——接续则继续累计，新阶段/成功收尾则重建或清除，
    天然满足「重启/十分钟接续恢复 + 下一轮不污染」。
    """

    def __init__(self, config_name: str, base_dir='config/tasks_config'):
        super().__init__('multi_daily', config_name, base_dir)

    def _load(self) -> dict:
        """忽略已取消的导出阶段键，移除功能后仍能接续已完成的日常进度。"""
        data = super()._load()
        flags = data.get('phase_flags')
        if isinstance(flags, dict):
            flags.pop('account_export_enable', None)
        return data

    # -------------------------------------------------- 本轮协作汇总

    def append_coop(self, record: dict) -> None:
        """立即持久化一条协作记录到进度文件顶层 ``coop`` 列表。

        每发现一条协作就调用（沿用现有原子 JSON 写入 _save），即使 OAS/Python 中途
        退出，已保存的协作也不丢；下一轮/新阶段由 ensure_phase / clear 统一清理。
        """
        coops = self._data.setdefault('coop', [])
        if not isinstance(coops, list):
            coops = []
            self._data['coop'] = coops
        coops.append(dict(record))
        self._save()

    def load_coops(self) -> list:
        """读取本轮已保存的协作记录。"""
        coops = self._data.get('coop', [])
        return list(coops) if isinstance(coops, list) else []

    # -------------------------------------------------- 本轮神秘商店汇总

    def append_mshop(self, record: dict) -> None:
        """立即持久化一条神秘商店记录到进度文件顶层 ``mshop`` 列表。

        与 append_coop 同构，但用独立键：协作汇总有固定 7 类格式化逻辑，
        商店记录混进 coop 会被当协作解析。生命周期同样跟随 ensure_phase / clear。
        """
        items = self._data.setdefault('mshop', [])
        if not isinstance(items, list):
            items = []
            self._data['mshop'] = items
        items.append(dict(record))
        self._save()

    def load_mshops(self) -> list:
        """读取本轮已保存的神秘商店记录。"""
        items = self._data.get('mshop', [])
        return list(items) if isinstance(items, list) else []

    # ------------------------------------------------ 本轮已通知标记

    def is_coop_notified(self) -> bool:
        """本轮是否已成功发送最终协作汇总（coop_notified=true）。"""
        return bool(self._data.get('coop_notified', False))

    def mark_coop_notified(self) -> None:
        """持久化「本轮协作已完成通知」。

        仅在 PushPlus 发送成功后调用；随后即使 next_run/clear 前崩溃，重启接续
        后 _notify_daily_completion 也会因标记存在而跳过，消除重复通知窗口。
        clear() 删除整个进度文件时该标记自然消失，不会带入下一轮。
        """
        self._data['coop_notified'] = True
        self._save()

    def archive_pending_coops(self) -> int:
        """重建前兜底：把尚未完成通知的协作与神秘商店记录归档到独立 JSON，返回归档条数。

        触发场景：phase_flags 已变 / 进度过期导致 ensure_phase 准备重建覆盖，而旧
        coop / mshop 尚未发送最终汇总。为避免数据永久丢失，先把它们复制到
        ``multi_daily_coop_archive_<config>.json``（按 config 隔离，保留最近
        COOP_ARCHIVE_LIMIT 份）。归档失败只记日志，绝不阻断主任务。

        两类记录写同一份归档文件的同一条目：它们属于同一轮，放一起才好回溯。
        返回值是两类合计条数（调用方只用它判断「有没有东西被归档」）。
        """
        coops = self.load_coops()
        mshops = self.load_mshops()
        if not coops and not mshops:
            return 0
        archive_path = self.path.parent / f'{self.task_name}_coop_archive_{self.config_name}.json'
        try:
            # 与管家历史清理共用锁，读取、追加与原子替换必须处在同一临界区。
            archive_path.parent.mkdir(parents=True, exist_ok=True)
            with FileLock(str(archive_path) + '.cleanup.lock'):
                existing = []
                if archive_path.exists():
                    try:
                        with open(archive_path, 'r', encoding='utf-8') as f:
                            existing = json.load(f)
                        if not isinstance(existing, list):
                            existing = []
                    except (json.JSONDecodeError, OSError, ValueError):
                        existing = []
                existing.append({
                    'archived_at': datetime.now().isoformat(),
                    'coops': coops,
                    'mshops': mshops,
                })
                existing = existing[-COOP_ARCHIVE_LIMIT:]
                _write_json_atomic(archive_path, existing)
            logger.info(f'已归档 {len(coops)} 条未通知协作、'
                        f'{len(mshops)} 条神秘商店记录 -> {archive_path}')
            return len(coops) + len(mshops)
        except Exception as e:
            logger.warning(f'协作归档失败（不影响主任务）: {e}')
            return 0

    def ensure_phase(self, phase_flags: dict, phase_id: str) -> bool:
        """在基类判定前做「重建前归档」兜底：本轮即将被重建且还有未通知记录时归档。

        保持基类 ensure_phase 的接续/重建语义完全不变：归档只是旁路留痕，绝不改变
        现有账号进度的接续规则，也不影响新建/重建结果。这里的判定必须与基类完全
        一致，否则会出现「归档了却仍然接续」的错位。
        """
        data = self._load()
        resumable = (
            bool(data)
            and phase_flags_match(data.get('phase_flags'), phase_flags)
            and not self._is_stale(data)
        )
        pending = any(isinstance(data.get(key), list) and data[key]
                      for key in ('coop', 'mshop'))
        if not resumable and pending:
            self._data = dict(data)
            self.archive_pending_coops()
        return super().ensure_phase(phase_flags, phase_id)


# 参与阶段判定的开关白名单：收 _schedule_* 会改写的键、轮转模式，以及会改变本轮执行内容的手动开关。
# 切换这些开关等于改变执行内容，必须让进度重建而不是接续。
# 显式白名单而非 startswith('total_')，因为 total_KekkaiUtilize_enable 会在
# 运行期被 MSGType.Utilize（未找到寄养卡）改写并落盘——若纳入快照，另一账号
# 失败重调度时会被误判成新阶段，重建进度导致已完成账号全部重跑、重复领奖。
# total_donatejade_enable / total_kekkaiActivation_enable 是静态用户配置，
# 不随阶段变化，同样排除。
PHASE_FLAG_KEYS = (
    # 模式开关：切换即换一套执行内容，纳入快照保证重建而非接续
    'disable_task_rotation',
    'total_alliedteam_battle_enable',
    'total_alliedteam_ap_enable',
    'total_returngift_enable',
    'total_courtyard_enable',
    'total_mail_enable',
    'total_cooperation_enable',
    'total_weekaward_enable',
    'total_mysteryshop_enable',
    # 单次运行标志：由 _reset_one_shot_flags 在成功收尾时清零，属于阶段语义
    'total_tree_planting_enable',
    'total_trialbattle_enable',
    'total_summon_up_enable',
    'total_publish_sr_enable',
)


def phase_flags_of(base_config, task_plan_phase: str | None = None) -> dict:
    """提取决定「本轮做什么」的全局开关快照，用于判断调度阶段是否延续。

    这些键由 _schedule_normal_day / _schedule_evening / _schedule_after_midnight /
    _schedule_alliedteam_after_returngift 在成功收尾时改写，因此快照变化恰好
    等价于「任务已分配下一次要做什么」，正是进度失效的边界。
    disable_task_rotation 不在自动轮转的写入面上，
    但用户手动切换后同样改变「本轮做什么」，需要重建进度。
    need_login / need_login_time 已删除，账号完成与否完全由进度文件判定。
    """
    flags = {name: getattr(base_config, name, None) for name in PHASE_FLAG_KEYS}
    # task_plan 只影响普通早/下午轮；将阶段名纳入快照，避免二者恰好拥有相同
    # total_* 值时错误接续上一轮账号进度。
    if task_plan_phase is not None:
        flags['task_plan_phase'] = task_plan_phase
    return flags


def phase_id_of(start_time) -> str:
    """生成人类可读的阶段标签（仅用于日志排查，不参与阶段判定）。"""
    return start_time.strftime('%Y%m%d-%H%M')
