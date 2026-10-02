"""跨任务角色登录时间合并；兼容模型列表和持久化的 list_N 字段。"""
from datetime import datetime, timedelta, timezone

from pydantic import BaseModel

from tasks.Component.SwitchAccount.switch_account_config import clean_text


def account_identity(account):
    """账号、角色、区服及平台共同标识角色，别名不参与跨表匹配。"""
    values = account if isinstance(account, dict) else vars(account)
    identity = tuple(clean_text(values.get(key, '')) for key in ('account', 'character', 'svr'))
    if not all(isinstance(value, str) and value for value in identity):
        return None
    platform = values.get('apple_or_android', True)
    if not isinstance(platform, bool):
        return None
    return (*identity, platform)


def login_moment(value):
    """旧配置和带时区的时间统一为北京时间；无效旧值不阻止真实登录更新。"""
    try:
        moment = value if isinstance(value, datetime) else datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None
    if moment.tzinfo is not None:
        moment = moment.astimezone(timezone(timedelta(hours=8))).replace(tzinfo=None)
    # 配置 DateTime 只保存到秒，同一秒不能因微秒不同被误判为实际变更。
    return moment.replace(microsecond=0)


def iter_accounts(node):
    """递归识别具有完整账号字段和登录时间的成员，不硬编码任务名称。"""
    values = vars(node) if isinstance(node, BaseModel) else node
    if isinstance(values, dict):
        if 'last_complete_time' in values and account_identity(values) is not None:
            yield values
        else:
            for value in values.values():
                yield from iter_accounts(value)
    elif isinstance(values, (list, tuple)):
        for value in values:
            yield from iter_accounts(value)


def latest_login_times(node):
    """同一角色出现多次时取最大时间，陈旧会话不能覆盖更新的记录。"""
    result = {}
    for account in iter_accounts(node):
        identity = account_identity(account)
        moment = login_moment(account.get('last_complete_time'))
        if moment is not None and (identity not in result or moment > result[identity]):
            result[identity] = moment
    return result


def changed_login_times(base, local):
    """只传播本次会话推进的时间，不把未变化的旧快照当成新的登录。"""
    def samples(node):
        # 比较每个角色的全部副本，避免某一表追平时被另一表的较新值遮蔽。
        result = {}
        for account in iter_accounts(node):
            moment = login_moment(account.get('last_complete_time'))
            if moment is not None:
                result.setdefault(account_identity(account), []).append(moment)
        return {identity: sorted(times) for identity, times in result.items()}

    previous = samples(base)
    return {identity: max(times) for identity, times in samples(local).items()
            if times != previous.get(identity)}


def apply_login_times(node, updates, *, model=False):
    """原地更新全部匹配成员；只前进，不增删账号或改变列表顺序。"""
    changed = False
    for account in iter_accounts(node):
        moment = updates.get(account_identity(account))
        if moment is None:
            continue
        previous = login_moment(account.get('last_complete_time'))
        if previous is None or previous < moment:
            account['last_complete_time'] = moment if model else moment.isoformat(sep=' ')
            changed = True
    return changed
