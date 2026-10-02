# This Python file uses the following encoding: utf-8
"""神秘商店货架的格位化定位（纯逻辑层）。

只做坐标算术与数据结构，不依赖 device / config，便于单测穷举边界。
设备交互与编排在 tasks/DailyAltAcc/mshop.py。
"""
from dataclasses import dataclass

from module.atom.ocr import RuleOcr
from tasks.DailyAltAcc.config import CoinType, GoodsType

# 货架总区域，沿用 assets.py 里 I_MS_ALL_* 的 roi_back（已验证坐标）
SHELF_ROI = (139, 107, 887, 454)

# 8 格位网格：4 列 × 2 行的连续铺砖。
# 列边界由 8 个价格 OCR ROI 的中心（x = 248/474/697/923，列间距约 225px）取相邻中点；
# 行边界取两行「商品 + 价格」整段之间的中点。
# 不变量：网格 x∈[135,1036)、y∈[107,561) 完全包含 SHELF_ROI，
# 因此任何命中都不可能落在网格外 —— 越界只可能是代码 bug，由单测守护。
GRID_X_EDGES = (135, 361, 586, 810, 1036)
GRID_Y_EDGES = (107, 330, 561)
GRID_COLS = 4

# 币种阈值：神秘商店金币价 > 10000，勾玉价 <= 10000。
# 魂玉不会给这些货标价，所以纯数值判定是完备的，不需要再匹配币种图标。
COIN_THRESHOLD = 10000

# 货物清单：加货物只加一行，扫描逻辑不用改。
# 值是 tasks/DailyAltAcc/assets.py 里的资产变量名，由 mshop/image1.json 生成，
# 搜索区统一是货架总区域 SHELF_ROI。调用方用 getattr(self, 名字) 取实例。
#
# 硬约束：取到的实例只允许调 match_all_any()，禁止调 match()。
# assets.py 里的 RuleImage 是类属性，全进程共享同一个实例；match() 会把命中坐标
# 写回 roi_front（module/atom/image.py:166），那就是跨任务、跨多开实例的污染
# —— 与 tasks/RichMan/mall/special.py:156 就地改 O_SP_RES_NUMBER.roi 同一类问题。
# match_all_any() 不传 roi 参数时对实例零副作用（module/atom/image.py:278
# 仅在传了 roi 时才改 roi_back）。
GOODS_ASSETS = {
    GoodsType.orochi_scale: 'I_MS_OROCHI_SCALE',
    GoodsType.demon_soul: 'I_MS_DEMON_SOUL',
    GoodsType.skill_shard: 'I_MS_SKILL_SHARD',
    GoodsType.mystery_amulet: 'I_MS_MYSTERY_AMULET',
    GoodsType.black_daruma: 'I_MS_BLACK_DARUMA',
}

# 价格 OCR 的 ROI 点阵。原来是 assets.py 里 8 个手绘的 O_MS_PRICENUM_*，
# x 有 ±4px、y 有 ±5px 抖动；现规整为严格均匀点阵，由 price_rule() 现造 RuleOcr，
# 不再占 8 个资产条目（加减格位只改这里的常量）：
#   x = 168 + col * 225   ->  168 / 393 / 618 / 843
#   y = 253 + row * 262   ->  253 / 515
#   w = 159, h = 45
# 取值保证每个框完整覆盖对应的原手绘框（只放大不裁切），单测守护这一不变量。
PRICE_X0 = 168
PRICE_PITCH_X = 225
PRICE_Y0 = 253
PRICE_PITCH_Y = 262
PRICE_W = 159
PRICE_H = 45

# 货物中文名。既用于播报文案，也作为 [STAT] 日志 goods 字段的值
# （不透传枚举英文名，直接给中文，面板上不用再做一层翻译）。
GOODS_NAMES = {
    GoodsType.orochi_scale: '大蛇的逆鳞',
    GoodsType.demon_soul: '逢魔之魂',
    GoodsType.skill_shard: '御行达摩碎片',
    GoodsType.mystery_amulet: '神秘符咒',
    GoodsType.black_daruma: '御行达摩',
}

# 各货物需要的最低花数（账号 isflower 字段：0零花 1一花 2二花 3三花）。
# 大蛇的逆鳞与逢魔之魂不限花数；一花解放神秘符咒，二花解放御行达摩碎片，
# 三花解锁御行达摩（黑蛋）。
FLOWER_UNLOCK = {
    GoodsType.orochi_scale: 0,
    GoodsType.demon_soul: 0,
    GoodsType.mystery_amulet: 1,
    GoodsType.skill_shard: 2,
    GoodsType.black_daruma: 3,
}

# 御行达摩碎片与御行达摩（黑蛋）图标相近，模板可能互相命中；两者价格区间不重叠，
# 碎片最高 300、黑蛋最低 960，中间有安全间隔，所以用价格做二次校正。
SHARD_MAX_PRICE = 300
BLACK_DARUMA_MIN_PRICE = 960

COIN_NAMES = {
    CoinType.jade: '勾玉',
    CoinType.gold: '金币',
    CoinType.unknow: '未知币种',
}


@dataclass
class SlotItem:
    """货架上某个格位识别出的商品。"""

    slot: int          # 格位号 1..8，行优先
    goods: GoodsType
    price: int
    coin: CoinType
    score: float       # 模板匹配得分，同格冲突时取优


def _bucket(value: int, edges: tuple) -> int | None:
    """左闭右开分桶：edges[i] <= value < edges[i+1] 时返回 i，越界返回 None。"""
    for index in range(len(edges) - 1):
        if edges[index] <= value < edges[index + 1]:
            return index
    return None


def locate_slot(cx: int, cy: int) -> int | None:
    """把模板命中框的中心点映射到格位号。

    :param cx: 命中框中心 x
    :param cy: 命中框中心 y
    :return: 格位号 1..8；落在网格外返回 None（调用方应记 warning 并跳过）
    """
    col = _bucket(cx, GRID_X_EDGES)
    row = _bucket(cy, GRID_Y_EDGES)
    if col is None or row is None:
        return None
    return row * GRID_COLS + col + 1


def coin_of(price: int) -> CoinType:
    """按价格数值判定币种，price <= 0 视为 OCR 失败。"""
    if price <= 0:
        return CoinType.unknow
    if price > COIN_THRESHOLD:
        return CoinType.gold
    return CoinType.jade


def enabled_goods(flower: int) -> list[GoodsType]:
    """按账号花数返回要扫描的货物类型。

    :param flower: 账号 isflower，0零花 1一花 2二花 3三花
    :return: 花数已解锁的货物类型，顺序与 FLOWER_UNLOCK 声明一致
    """
    return [goods for goods, need in FLOWER_UNLOCK.items() if flower >= need]


def refine_goods(goods: GoodsType, price: int) -> GoodsType:
    """用价格校正御行达摩系的碎片 / 黑蛋误判。

    两者图标相近，模板可能交叉命中；价格区间不重叠（碎片 <=300，黑蛋 >=960），
    所以以价格为准。非御行达摩系的货物原样返回。

    :param goods: 模板命中得到的货物类型
    :param price: 该格位 OCR 出的价格
    :return: 校正后的货物类型
    """
    if goods not in (GoodsType.skill_shard, GoodsType.black_daruma):
        return goods
    if price >= BLACK_DARUMA_MIN_PRICE:
        return GoodsType.black_daruma
    if price <= SHARD_MAX_PRICE:
        return GoodsType.skill_shard
    return goods


def is_unlocked(goods: GoodsType, flower: int) -> bool:
    """该货物在当前花数下是否已解锁。"""
    return flower >= FLOWER_UNLOCK[goods]


def price_roi(slot: int) -> tuple[int, int, int, int]:
    """按点阵算出某格位的价格 ROI。

    :param slot: 格位号 1..8
    :return: (x, y, w, h)
    """
    col = (slot - 1) % GRID_COLS
    row = (slot - 1) // GRID_COLS
    return (PRICE_X0 + col * PRICE_PITCH_X, PRICE_Y0 + row * PRICE_PITCH_Y,
            PRICE_W, PRICE_H)


# 按格位号缓存价格 RuleOcr，避免每次扫描重复构造。
# RuleOcr 只读不写（ocr()/detect_and_ocr() 都不改自身 roi），所以缓存共享是安全的
# —— 与 RuleImage.match() 会写回 roi_front 的情况不同。
_PRICE_RULE_CACHE: dict[int, RuleOcr] = {}


def price_rule(slot: int) -> RuleOcr:
    """按点阵现造某格位的价格 RuleOcr，取代原 assets.py 里的 O_MS_PRICENUM_1..8。

    不进 assets.py 的理由：8 个 ROI 是同一点阵的机械展开，写进 ocr.json 等于
    把「加减一列格位」变成手改 8 条 json + 重生成资产；现在只改点阵常量。
    name 带格位号，日志里仍能分辨是哪一格（形如 [MS_PRICE_3 0.01s] [72]）。

    :param slot: 格位号 1..8
    """
    if slot not in _PRICE_RULE_CACHE:
        _PRICE_RULE_CACHE[slot] = RuleOcr(
            roi=price_roi(slot), area=(0, 0, 100, 100), mode='Digit',
            method='Default', keyword='', name=f'ms_price_{slot}')
    return _PRICE_RULE_CACHE[slot]
