"""解析 YYX 位置数组及 OAS 寄售券、传记归属扩展，拒绝缺项、未知尾项和类型错位。"""
import json
import math


class SnapshotParseError(ValueError):
    """原始数据不能完整匹配 yyx-bridge Snapshot 协议。"""


# 字段顺序对应 bridge-types/src；字典表示位置记录，列表表示 Vec，元组表示枚举。
# 已声明字段没有默认值，不能用空集合或零填补缺项；旧协议未采集寄售券时保留缺省。
# 新版 YYX 导出器已追加 UR=6；保留旧序号，未知序号仍由枚举校验拒绝。
_HERO_RARITY = ('Unknown', 'N', 'R', 'SR', 'SSR', 'SP', 'UR')
_EQUIP_ATTR_TYPE = (
    'Hp', 'Defense', 'Attack', 'HpRate', 'DefenseRate', 'AttackRate',
    'Speed', 'CritRate', 'CritPower', 'EffectHitRate', 'EffectResistRate',
)
_HERO_ATTR = {'base': float, 'add_value': float, 'add_rate': float, 'value': float}
_HERO_ATTRS = {
    'max_hp': _HERO_ATTR,
    'speed': _HERO_ATTR,
    'crit_power': _HERO_ATTR,
    'crit_rate': _HERO_ATTR,
    'defense': _HERO_ATTR,
    'attack': _HERO_ATTR,
    'effect_hit_rate': float,
    'effect_resist_rate': float,
}
_HERO = {
    'id': str,
    'hero_id': int,
    'equips': [str],
    'level': int,
    'exp': float,
    'nick_name': str,
    'born': int,
    'lock': bool,
    'rarity': _HERO_RARITY,
    'skills': [{'id': int, 'level': int}],
    'awake': int,
    'star': int,
    'attrs': _HERO_ATTRS,
}
# Rust 的 type_ 字段带 serde(rename = "type")，导出对象使用重命名后的键。
_EQUIP_ATTR = {'type': _EQUIP_ATTR_TYPE, 'value': float}
_HERO_EQUIP = {
    'id': str,
    'suit_id': int,
    'quality': int,
    'pos': int,
    'equip_id': int,
    'level': int,
    'born': int,
    'lock': bool,
    'garbage': bool,
    'attrs': [_EQUIP_ATTR],
    'base_attr': _EQUIP_ATTR,
    'random_attrs': [_EQUIP_ATTR],
    'random_attr_rates': [_EQUIP_ATTR],
    'single_attrs': [_EQUIP_ATTR],
}
_CURRENCY = {name: int for name in (
    'coin', 'jade', 'action_point', 'auto_point', 'honor', 'medal', 'contrib',
    'totem_pass', 's_jade', 'skin_token', 'realm_raid_pass', 'broken_amulet',
    'mystery_amulet', 'ar_amulet', 'ofuda', 'gold_ofuda', 'scale', 'reverse_scale',
    'demon_soul', 'foolery_pass', 'sp_skin_token',
)}
# 新传记记录保留原两项，尾部携带游戏配置中的式神编号及章节，旧记录不猜归属。
_STORY_TASK = {'id': int, 'progress': {'value': int, 'max_value': int}}
_STORY_TASK_WITH_HERO = {**_STORY_TASK, 'hero_id': int, 'chapter': int}
_SNAPSHOT = {
    'player': {'id': int, 'server_id': int, 'name': str, 'level': int},
    'currency': _CURRENCY,
    'heroes': [_HERO],
    'hero_equips': [_HERO_EQUIP],
    'hero_equip_presets': [{'name': str, 'items': [str]}],
    'hero_book_shards': [{
        'hero_id': int, 'shards': int, 'books': int, 'book_max_shards': int,
    }],
    'realm_cards': [{
        'id': str, 'item_id': int, 'total_time': int,
        'attrs': {'exp': int, 'bonus': int},
    }],
    'story_tasks': [_STORY_TASK],
}


def _parse_value(value, schema, path):
    """递归校验数组位置和基础类型，错误只包含字段路径以免泄露账号数据。"""
    if isinstance(schema, dict):
        if not isinstance(value, list):
            raise SnapshotParseError(f'{path}: 应为位置数组')
        # 同一快照允许新旧记录混合；只接收完整的四项扩展，其余长度仍严格拒绝。
        if schema is _STORY_TASK and len(value) == 4:
            schema = _STORY_TASK_WITH_HERO
        if len(value) != len(schema):
            # 原 Rust 忽略多余尾项；此处拒绝版本变化，防止字段错位仍然被当作成功。
            raise SnapshotParseError(
                f'{path}: 应有 {len(schema)} 个字段，实际为 {len(value)} 个'
            )
        result = {
            name: _parse_value(item, field, f'{path}.{name}')
            for (name, field), item in zip(schema.items(), value)
        }
        if schema is _STORY_TASK_WITH_HERO:
            for name in ('hero_id', 'chapter'):
                # 类型和 i64 范围已由递归验证，归属字段额外要求正整数。
                if result[name] <= 0:
                    raise SnapshotParseError(f'{path}.{name}: 应为正整数')
        return result
    if isinstance(schema, list):
        if not isinstance(value, list):
            raise SnapshotParseError(f'{path}: 应为数组')
        return [
            _parse_value(item, schema[0], f'{path}[{index}]')
            for index, item in enumerate(value)
        ]
    if isinstance(schema, tuple):
        # bool 是 Python int 的子类，但 Rust as_u64 不接受 JSON 布尔值。
        if type(value) is not int or not 0 <= value < len(schema):
            raise SnapshotParseError(f'{path}: 应为 0..{len(schema) - 1} 的枚举整数')
        return schema[value]
    if schema is float:
        # Rust f64 接受整数和浮点数，但 JSON 不支持 NaN、Infinity 或溢出的浮点数。
        if type(value) in (int, float):
            try:
                number = float(value)
                if math.isfinite(number):
                    return number
            except OverflowError:
                pass
        raise SnapshotParseError(f'{path}: 应为有限数值')
    if schema is int:
        if type(value) is not int or not -(2 ** 63) <= value < 2 ** 63:
            raise SnapshotParseError(f'{path}: 应为 i64 范围内的整数')
    elif type(value) is not schema:
        raise SnapshotParseError(f'{path}: 应为 {schema.__name__}')
    if schema is str:
        try:
            # Python 会保留 JSON 中孤立的代理码点；Rust String 只接受有效 Unicode。
            value.encode('utf-8')
        except UnicodeError as exc:
            raise SnapshotParseError(f'{path}: 字符串不是有效的 Unicode') from exc
    return value


def _reject_constant(value):
    """禁用 Python json 默认接受的非标准数值。"""
    raise SnapshotParseError('原始 JSON 含非标准数值')


def parse_snapshot(raw: str | bytes) -> dict:
    """返回 Rust Serialize 同名对象；不添加 timestamp、version 或外层 data。"""
    if not isinstance(raw, (str, bytes)):
        raise SnapshotParseError('原始数据必须为 JSON 字符串或 UTF-8 字节')
    try:
        # Rust from_slice 使用 UTF-8，不接受 json.loads 自动探测的 UTF-16/UTF-32。
        if isinstance(raw, bytes):
            raw = raw.decode('utf-8')
        payload = json.loads(raw, parse_constant=_reject_constant)
    except SnapshotParseError:
        raise
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise SnapshotParseError('原始数据不是有效的 UTF-8 JSON') from exc
    if isinstance(payload, dict) and 'error' in payload:
        raise SnapshotParseError('客户端返回 error，账号数据未成功导出')
    schema = _SNAPSHOT
    # 兼容原版 21 项货币；OAS 只识别末尾的寄售券扩展，其他长度仍严格拒绝。
    # 旧快照不添加寄售券字段，避免把「未采集」误报成持有数量为零。
    if (isinstance(payload, list) and len(payload) > 1
            and isinstance(payload[1], list) and len(payload[1]) == len(_CURRENCY) + 1):
        schema = {**_SNAPSHOT, 'currency': {**_CURRENCY, 'consignment_ticket': int}}
    return _parse_value(payload, schema, 'snapshot')
