# This Python file uses the following encoding: utf-8
"""MultiTasks 的三种账号来源。

三个 load_* 函数同签名 `(config) -> (执行项, 提醒文本, 是否有配置加载失败)`，
只读配置、不碰 device，因此可脱离模拟器单测。有效性校验与去重收敛在
_is_usable / _dedup 两个 helper 里，三种来源共用。
"""
from typing import Callable

from module.logger import logger
from tasks.Component.SwitchAccount.switch_account_config import AccountInfo
from tasks.MultiTasks.config import AccountSourceType, active_account_configs

# (来源配置名, 账号)。来源配置名只用于日志与通知，不参与去重键。
ExecutionItem = tuple[str, AccountInfo]

# 三元组返回值：(执行项, 提醒文本, 是否有配置加载失败)
SourceResult = tuple[list[ExecutionItem], list[str], bool]


def parse_account_characters(raw: str) -> list[str]:
    """解析角色名字符串为有序去重列表。

    规则：仅按英文逗号分割 -> 去除首尾空白 -> 忽略空项 -> 重复名按首次出现去重。
    空字符串、纯空白或仅含逗号时返回空列表。
    """
    if not raw:
        return []
    result: list[str] = []
    seen: set[str] = set()
    for part in raw.split(','):
        name = part.strip()
        if not name or name in seen:
            continue
        seen.add(name)
        result.append(name)
    return result


def _is_usable(account) -> bool:
    """只有具备完整切号资料的账号才能进入执行队列。账号别名允许为空。"""
    return bool(
        account is not None
        and account.character
        and account.svr
        and account.account
        and account.apple_or_android is not None
    )


def _dedup(items: list[ExecutionItem]) -> list[ExecutionItem]:
    """按 (角色名, 账号, 服务器) 保序去重。

    同一物理账号可能同时登记在多个配置实例的小号表里，不去重会导致重复执行
    （重复领奖、重复消耗体力）。
    """
    result: list[ExecutionItem] = []
    seen: set[tuple[str, str, str]] = set()
    for source_name, account in items:
        key = (account.character, account.account, str(account.svr))
        if key in seen:
            logger.info(
                f'[MultiTasks] 跳过重复账号: config={source_name}, '
                f'character={account.character}, server={account.svr}'
            )
            continue
        seen.add(key)
        result.append((source_name, account))
    return result


def _load_source_accounts(store, source_name: str) -> list:
    """读取指定配置实例的 MultiDailyAltAcc 小号表。

    独立成函数便于单测打桩；调用方负责 try 住本函数的异常并置 load_failure。
    """
    from module.config.config import Config

    source_config = Config(source_name, store=store)
    return source_config.multi_daily_alt_acc.sup_account_list or []


def load_own_list(config) -> SourceResult:
    """方式一：读本任务自己的账号表。

    本实例配置已在内存中，不存在加载失败，load_failure 恒为 False。
    """
    accounts = config.multi_tasks.sup_account_list or []
    items = [(config.config_name, account) for account in accounts if _is_usable(account)]
    return _dedup(items), [], False


def load_config_selection(config) -> SourceResult:
    """方式二：读用户勾选的配置实例的 MultiDailyAltAcc 小号表。

    每次执行都通过 Store 完成恢复后枚举，避免 create/rename/delete 后沿用陈旧身份。
    每个实例的加载单独 try：失败只记日志并置 load_failure，继续扫描其余实例。
    """
    selection = config.multi_tasks.account_config_selection
    source_names = [
        source_name
        for field_name, (source_name, _count) in active_account_configs(config.store).items()
        if getattr(selection, field_name, False)
    ]

    items: list[ExecutionItem] = []
    load_failure = False
    for source_name in source_names:
        try:
            accounts = _load_source_accounts(config.store, source_name)
        except Exception as e:
            # 单个配置加载失败：记录不含敏感数据的错误日志并继续扫描其他配置
            load_failure = True
            logger.error(f'[MultiTasks] 加载配置失败: config={source_name}, error={type(e).__name__}')
            continue
        items.extend((source_name, account) for account in accounts if _is_usable(account))
    return _dedup(items), [], load_failure


def load_characters(config) -> SourceResult:
    """方式三：按角色名精确匹配，扫描全部 active 配置的 MultiDailyAltAcc 小号表。

    通过注入 ConfigStore 的 active 配置列表扫描，不 glob 配置文件；配置按名称稳定
    排序，账号按原始列表位置保序，保证同名项内部顺序稳定。输出顺序遵循用户输入
    的角色名顺序。未匹配的角色名进提醒文本，本身不算失败。
    """
    characters = parse_account_characters(
        config.multi_tasks.multi_tasks_config.account_characters
    )
    if not characters:
        return [], [], False

    index: dict[str, list[ExecutionItem]] = {}
    load_failure = False
    for source_name in config.store.active_config_names():
        try:
            accounts = _load_source_accounts(config.store, source_name)
        except Exception as e:
            load_failure = True
            logger.error(f'[MultiTasks] 加载配置失败: config={source_name}, error={type(e).__name__}')
            continue
        for account in accounts:
            if not _is_usable(account):
                continue
            index.setdefault(account.character, []).append((source_name, account))

    items: list[ExecutionItem] = []
    unmatched: list[str] = []
    for character in characters:
        matches = index.get(character)
        if not matches:
            unmatched.append(character)
            continue
        items.extend(matches)
    return _dedup(items), unmatched, load_failure


ACCOUNT_SOURCES: dict[AccountSourceType, Callable[[object], SourceResult]] = {
    AccountSourceType.OWN_LIST: load_own_list,
    AccountSourceType.CONFIG_SELECTION: load_config_selection,
    AccountSourceType.CHARACTERS: load_characters,
}
