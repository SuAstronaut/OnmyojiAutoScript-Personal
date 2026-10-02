# This Python file uses the following encoding: utf-8
# @author runhey
# github https://github.com/runhey

from module.atom.click import RuleClick
from module.atom.image import RuleImage
from module.base.decorator import del_cached_property
from module.base.timer import Timer
from module.logger import logger

from tasks.Component.Costume.config import (MainType, CostumeConfig, RealmType,
                                            ThemeType, ShikigamiType, SignType, BattleType, CarpBannerType)
from tasks.Component.Costume.assets import CostumeAssets
from tasks.Component.CostumeTheme.assets import CostumeThemeAssets
from tasks.Component.CostumeBattle.assets import CostumeBattleAssets
from tasks.Component.CostumeShikigami.assets import CostumeShikigamiAssets
from tasks.Component.CostumeCarpBanner.assets import CostumeCarpBannerAssets
from tasks.Component.CostumeRealm.assets import CostumeRealmAssets
from tasks.GameUi.assets import GameUiAssets
from tasks.KekkaiUtilize.assets import KekkaiUtilizeAssets
from tasks.Pets.assets import PetsAssets
from tasks.Restart.assets import RestartAssets

# 庭院皮肤
# 主界面皮肤（使用字典推导式动态生成）
# key 用 (资产类, 属性名) 而不是裸字符串：替换直接落到类属性对象上，不依赖调用方任务的
# MRO，也不会因为「碰巧没有别的任务持有该属性」而静默跳过。
# I_PET_HOUSE 要显式指向 PetsAssets——GameUiAssets 上没有这个属性，旧代码是靠
# hasattr(self, ...) 作用在 Pets 任务实例上才换到的（见本任务「关键设计选择」）。
main_costume_model = {
    getattr(MainType, f"COSTUME_MAIN_{i}"): {
        # 这里**刻意没有** (GameUiAssets, 'I_CHECK_MAIN')：它已经换成皮肤无关的共用判据
        # （庭院右侧的活动图标），不随皮肤变，也不该被 replace_img 改写——否则一次误判
        # 就会打掉 page_main 的判据，2026-09-15 的 GamePageUnknownError 就是这么来的。
        # 各套皮肤那张月亮图还在（I_CHECK_MAIN_{i}），但只当皮肤探测的模板用，
        # 模板来源见 _MAIN_PROBE_DEFAULT。
        (GameUiAssets, 'I_MAIN_GOTO_EXPLORATION'): f'I_MAIN_GOTO_EXPLORATION_{i}',
        (GameUiAssets, 'I_MAIN_GOTO_SUMMON'): f'I_MAIN_GOTO_SUMMON_{i}',
        (GameUiAssets, 'I_MAIN_GOTO_TOWN'): f'I_MAIN_GOTO_TOWN_{i}',
        (PetsAssets, 'I_PET_HOUSE'): f'I_PET_HOUSE_{i}',
    } for i in range(1, 18)
}


# 鲤鱼旗皮肤
carpbanner_costume_model = {
    getattr(CarpBannerType, f"COSTUME_CARPBANNER_{i}"): {
        'I_SHI_CARD': f'I_SHI_CARD_{i}',
        'I_SHI_DEFENSE': f'I_SHI_DEFENSE_{i}',
        'I_SHI_GROWN': f'I_SHI_GROWN_{i}',
    } for i in range(1, 4)
}


# 战斗主题（使用循环处理常规情况 + 特例处理）
battle_theme_model = {}
for i in range(1, 14):
    entry = {
        'I_LOCAL': f'I_LOCAL_{i}',
        'I_EXIT': f'I_EXIT_{i}',
        'I_FRIENDS': f'I_FRIENDS_{i}',
        # 战斗信息标识：is_in_real_battle 的唯一判据。不能用 I_FRIENDS/I_EXIT
        # 代替——那两个在准备界面同样存在，判不出「真正的战斗中」。而这张图
        # 只在战斗过程中出现，图案却随主题变，所以必须逐套登记，否则换主题后
        # 仍是默认主题的图而失配（雅乐之邦实测 0.68 < 0.8）。
        'I_BATTLE_INFO': f'I_BATTLE_INFO_{i}',
    }
    if i == 8:  # 特殊处理第8项
        entry.update({
            'I_WIN': 'I_WIN_8',
            'I_DE_WIN': 'I_DE_WIN_8',
            'I_FALSE': 'I_FALSE_8'
        })
    if i == 12:  # 特殊处理第12项
        entry.update({
            'I_WIN': 'I_WIN_12',
            'I_DE_WIN': 'I_DE_WIN_12',
            'I_FALSE': 'I_FALSE_12'
        })
    if i == 13:  # 13项特殊处理
        entry.update({
            'I_WIN': 'I_WIN_13',
            'I_DE_WIN': 'I_DE_WIN_13',
            'I_FALSE': 'I_FALSE_13'
        })
    battle_theme_model[getattr(BattleType, f"COSTUME_BATTLE_{i}")] = entry

# 幕间主题
shikigami_costume_model = {
    getattr(ShikigamiType, f"COSTUME_SHIKIGAMI_{i}"): {
        # GameUi 进出式神录
        'I_CHECK_RECORDS': f'I_CHECK_RECORDS_{i}',
        'I_RECORD_SOUL_BACK': f'I_RECORD_SOUL_BACK_{i}',
        # SwitchSoul 相关
        'I_SOUL_PRESET': f'I_SOUL_PRESET_{i}',
        'I_SOU_CHECK_IN': f'I_SOU_CHECK_IN_{i}',
        'I_SOU_TEAM_PRESENT': f'I_SOU_TEAM_PRESENT_{i}',
        'I_SOU_CLICK_PRESENT': f'I_SOU_CLICK_PRESENT_{i}',
        'I_SOU_SWITCH_SURE': f'I_SOU_SWITCH_SURE_{i}',
        # SwitchSoul 分组相关 (1-7组)
        **{f'I_SOU_CHECK_GROUP_{g}': f'I_SOU_CHECK_GROUP_{g}_{i}' for g in range(1, 8)},
        # SwitchSoul 队伍相关 (1-4队)
        **{f'I_SOU_SWITCH_{t}': f'I_SOU_SWITCH_{t}_{i}' for t in range(1, 5)},
        # SoulsTidy 相关
        'I_ST_SOULS': f'I_ST_SOULS_{i}',
        'I_ST_REPLACE': f'I_ST_REPLACE_{i}',
    }
    for i in range(1, 11)  # 目前支持 COSTUME_SHIKIGAMI_1 到 COSTUME_SHIKIGAMI_10
}

def _clear_rule_image_cache(obj: RuleImage) -> None:
    """清掉 RuleImage 的懒加载缓存。

    结界链的 target 里混着 RuleClick（C_REALM_GROWN / C_REALM_CARD），所以开头必须先挡一道：
    RuleClick 不做模板匹配，既没有 _image / _kp / _des，name 也不是 cached_property 而是
    __init__ 里赋的普通实例属性。del_cached_property 只吞 KeyError、删得到就真删——删掉
    name 之后 RuleClick.__repr__ 直接 AttributeError，而 name 还担着 device 防连点记录的
    键与日志展示。往 RuleClick 上塞 _image 之类的空壳属性同样没有意义。

    replace_img 只改 .file，而 load_image() 开头 `if self._image is not None: return`
    会直接早退，不清就是换了白换；name / kp / des 都是 cached_property（结果塞在实例
    __dict__ 里），不清 name 会继续按旧文件名登记 interval_timer，不清 kp/des 会让
    Sift Flann 方法沿用旧模板的特征点（当前映射的资产都是 Template matching，属潜伏
    问题，但这是个通用清理器）。
    """
    if isinstance(obj, RuleClick):
        return
    obj._image = None
    obj._kp = None
    obj._des = None
    del_cached_property(obj, 'name')
    del_cached_property(obj, 'kp')
    del_cached_property(obj, 'des')


# 庭院皮肤涉及的资产 key。直接从 model 的第一项推导，避免「映射表」与「默认套要做哪些
# key 的快照」两处各维护一份清单而脱节。
_MAIN_TARGETS = tuple(next(iter(main_costume_model.values())).keys())

# 出厂资产快照：{ (资产类, 属性名): {'file','roi_front','roi_back','threshold'} }
#
# 为什么需要：replace_img 就地改写类属性对象，默认套一旦被换成别的皮肤，它的出厂图就
# 再没有引用，探测时永远匹配不到「默认庭院」，回切也没东西可换回去。所以在首次真替换
# 之前冻结一份。此时 RuleImage 还没被任何地方加载过图，roi_front 就是 assets.py 里的
# 出厂值。
_default_main_snapshot: dict | None = None

# 当前已套用的皮肤，None 表示出厂状态。用于幂等（每个任务 __init__ 都会调
# check_costume）与默认套回切。
_applied_main: MainType | None = None


def _snapshot(obj: RuleImage) -> dict:
    """记下资产的出厂取值，供回切默认套时重建。

    method 必须一起记：不记就只能在 _rebuild 里硬编码，回切默认套会把 method 改掉
    （当前涉及的资产都是 Template matching，属潜伏问题）。

    结界链的 target 混着 RuleClick：它不做模板匹配，没有 file / threshold / method，
    照 RuleImage 那套读会直接 AttributeError。所以按类型分开记——**没有 file 键就是
    RuleClick**，_rebuild 用同一个判据分流，不必额外加类型标记位。
    """
    if isinstance(obj, RuleClick):
        return {
            'name': obj.name,
            'roi_front': tuple(obj.roi_front),
            'roi_back': tuple(obj.roi_back),
        }
    return {
        'file': obj.file,
        'roi_front': tuple(obj.roi_front),
        'roi_back': obj.roi_back,
        'threshold': obj.threshold,
        'method': obj.method,
    }


def _rebuild(snapshot: dict) -> RuleImage:
    """按快照重建一个资产对象。

    不能直接复用快照里的原对象——那个对象已经被 replace_img 就地改写成别的皮肤了。
    """
    if 'file' not in snapshot:
        # 没有 file 键即为 RuleClick（见 _snapshot），按区域重建
        return RuleClick(roi_front=snapshot['roi_front'], roi_back=snapshot['roi_back'],
                         name=snapshot['name'])
    return RuleImage(roi_front=snapshot['roi_front'], roi_back=snapshot['roi_back'],
                     method=snapshot['method'], threshold=snapshot['threshold'],
                     file=snapshot['file'])


def _ensure_default_main_snapshot() -> None:
    """冻结默认庭院的出厂资产值。必须在首次真替换之前调用，且只做一次。"""
    global _default_main_snapshot
    if _default_main_snapshot is not None:
        return
    snap: dict = {}
    for target in _MAIN_TARGETS:
        asset_cls, attr = target
        # 目标类没有该属性时静默跳过，与 replace_img 的处理保持一致
        # （正常情况下 4 个 key 都齐：3 个在 GameUiAssets，I_PET_HOUSE 在 PetsAssets）
        if not hasattr(asset_cls, attr):
            continue
        snap[target] = _snapshot(getattr(asset_cls, attr))
    _default_main_snapshot = snap


# 卷轴（主题）皮肤。默认套的资产在 RestartAssets，靠出厂快照回切，不登记映射。
# 键是 (资产类, 属性名)：卷轴的收起/展开态挂在 RestartAssets 上，而 Plotline /
# ExperienceYoukai 的任务 MRO 里没有 RestartAssets，用 getattr(self, ...) 会静默跳过。
#
# 用推导式而非逐套登记：各套的键集合完全一致、value 名只差一个序号，属**同构**。
# 同构就该让结构去保证一致性——逐套手写的第 N 项和第 1 项一样容易写错 value 名或漏键，
# 推导式在结构上不可能出现这两种错。代价是「有几套」这个事实同时存在于枚举与 range
# 两处，加新皮肤**记得把上界 +1**：漏改会静默少一套，但 verify.py 会以「映射表里没有
# 这一项」报 [NG] 兜住。若将来某套换的资产与其余不一致（异构），再照 battle_theme_model
# 那样按序号补特例块。每套的中文名见 assets/i18n/zh-CN.json，不在这里重复。
theme_costume_model = {
    getattr(ThemeType, f'COSTUME_THEME_{i}'): {
        (RestartAssets, 'I_LOGIN_SCROOLL_CLOSE'): f'I_THEME_{i}_SCROLL_CLOSE',
        (RestartAssets, 'I_LOGIN_SCROOLL_OPEN'): f'I_THEME_{i}_SCROLL_OPEN',
    } for i in range(1, 3)
}

# 卷轴皮肤涉及的资产 key（收起态 + 展开态），默认套也要，回切时得知道还原哪几个。
_THEME_TARGETS = (
    (RestartAssets, 'I_LOGIN_SCROOLL_CLOSE'),
    (RestartAssets, 'I_LOGIN_SCROOLL_OPEN'),
)

_default_theme_snapshot: dict | None = None
_applied_theme: ThemeType | None = None


def _ensure_default_theme_snapshot() -> None:
    """冻结默认卷轴的出厂资产值。必须在首次真替换之前调用，且只做一次。"""
    global _default_theme_snapshot
    if _default_theme_snapshot is not None:
        return
    snap: dict = {}
    for target in _THEME_TARGETS:
        asset_cls, attr = target
        if not hasattr(asset_cls, attr):
            continue
        snap[target] = _snapshot(getattr(asset_cls, attr))
    _default_theme_snapshot = snap


# 结界皮肤。默认套（妖伞结界）的资产挂在 KekkaiUtilizeAssets 上，靠出厂快照回切，不登记映射。
# 键用 (资产类, 属性名)：活资产在 KekkaiUtilizeAssets 上，而换肤发生在 KekkaiUtilize /
# KekkaiActivation 及其派生任务里，写元组直接落到类属性对象上，不依赖调用方任务的 MRO。
#
# 一套结界皮肤要换三样东西（用户实测确认过，其余资源与皮肤无关）：
#   1. I_REALM_PAGE   结界页的站位锚点，同时是 page_guild_realm 的 check_button
#   2. C_REALM_GROWN  进式神育成的点击区域
#   3. C_REALM_CARD   进结界卡的点击区域
# 注意 I_UTILIZE_ADD（育成界面里「放置好友寄养」那个按钮）**不随皮肤变**，是固定资产，
# 所以刻意不在这张表里。
# 用推导式而非逐套登记，理由同 theme_costume_model：三样资产的键在每套里完全相同、
# 只有 value 名带序号，属同构。加新皮肤**记得把 range 上界 +1**。
realm_costume_model = {
    getattr(RealmType, f'COSTUME_REALM_{i}'): {
        (KekkaiUtilizeAssets, 'I_REALM_PAGE'): f'I_REALM_{i}_PAGE',
        (KekkaiUtilizeAssets, 'C_REALM_GROWN'): f'C_REALM_{i}_GROWN',
        (KekkaiUtilizeAssets, 'C_REALM_CARD'): f'C_REALM_{i}_CARD',
    } for i in range(1, 9)
}

# 结界皮肤涉及的资产 key。同 _MAIN_TARGETS，从 model 的第一项推导，避免映射表与
# 「默认套要快照哪些 key」两处各维护一份清单而脱节。
_REALM_TARGETS = tuple(next(iter(realm_costume_model.values())).keys())

_default_realm_snapshot: dict | None = None
_applied_realm: RealmType | None = None


def _ensure_default_realm_snapshot() -> None:
    """冻结默认结界的出厂资产值。必须在首次真替换之前调用，且只做一次。

    快照里有 RuleClick（两个点击区域），_snapshot 已按类型分流处理。
    """
    global _default_realm_snapshot
    if _default_realm_snapshot is not None:
        return
    snap: dict = {}
    for target in _REALM_TARGETS:
        asset_cls, attr = target
        if not hasattr(asset_cls, attr):
            continue
        snap[target] = _snapshot(getattr(asset_cls, attr))
    _default_realm_snapshot = snap


# 探测候选表：(皮肤类型, 用于判定的资产列表)。惰性构建一次并缓存。
#
# 缓存的理由不是「CostumeAssets() 很贵」——那 70 个 RuleImage 是类属性、import 期建
# 一次，new 一个空实例很廉价且不丢 _image 缓存（实测 CostumeAssets().I_CHECK_MAIN_5
# is CostumeAssets().I_CHECK_MAIN_5 为 True）。真正的理由是**卷轴与结界**的默认套候选
# 必须靠 _rebuild(快照) 新建对象（活对象已被就地改写），不缓存就每轮探测都要重新
# imdecode 一次默认模板；顺带也省掉每轮对映射表的 hasattr/getattr 扫描。
#
# 庭院那张表没有这层顾虑：它的模板来源已独立（见 _MAIN_PROBE_DEFAULT），是永久出厂态、
# 不会被 replace_img 动的独立资产，直接引用即可。
_probe_table_main: dict | None = None
_probe_table_theme: dict | None = None
_probe_table_realm: dict | None = None
# 庭院复核表：{ 皮肤类型: 该套 4 张 goto 资产的**副本** }，与 _probe_table_main 同键。
# 必须是副本：这 4 张对应的活资产是要被点击的目标，而 RuleImage.match() 命中会写
# roi_front（module/atom/image.py:166-168），探针直接拿活对象去匹配就会把它写脏，
# 随后 replace_img 又把脏 roi_front 拷进 GameUiAssets，点击落点跟着探测那次匹配跑偏。
_probe_confirm_main: dict | None = None

# 探测节流。Timer 未 start 时 _current == 0，reached() 恒为 True，所以新建即「已到点」，
# 首次探测不被拦；零命中后 reset() 才真正开始计时。
_probe_timer = Timer(2)

# 结界探测**单独**一个节流计时器，不与 main/theme 共用：两者不在同一时机跑
# （main/theme 在登录的庭院帧上，realm 在结界页帧上），共用会让 goto_realm 里紧挨着
# 的两次探测互相挡住——第一次零命中 reset 之后，第二次（刚导航进结界、人已经站在
# 结界页上）会被窗口拦掉，而那正是唯一能探到结界背景的一次。
_realm_probe_timer = Timer(2)

# 探针锁：探测成功即锁定，避免页面识别每轮都全量轮询。
# 解锁在 _app_handle_login() 开头（重试循环内，见 Task 6）。
_main_probe_locked = False
_theme_probe_locked = False
_realm_probe_locked = False

# 零命中告警去重：探测受 2s 节流，登录卡住时会反复零命中，不加限制会刷屏。
_probe_warned = False
# 结界探测的告警去重单独一个标志，不与上面的共用：两者生命周期不同，共用会让先探的
# 那个把后探的那个的告警吞掉，而且文案分不出是哪个探针零命中、该去补哪套皮肤的图。
_realm_probe_warned = False

# 庭院探测的模板名。**不再从 main_costume_model 取**：那张表里的 (GameUiAssets,'I_CHECK_MAIN')
# 已经换成皮肤无关的「活动」图标，拿它当模板会让默认套变成「匹配所有庭院」的通配符——
# 配置说 5 号、实际也是 5 号时会被默认套抢先命中，把 goto 资产换回出厂值，导航全废。
#
# 默认套用 CostumeAssets.I_CHECK_MAIN_DEFAULT（出厂态的月亮+天空），第 N 套用
# CostumeAssets.I_CHECK_MAIN_{N}。两类都是**永久出厂态**：main_costume_model 里已经没有它们，
# replace_img 再也改不到，所以不必像卷轴/结界那样靠出厂快照 _rebuild，直接引用资产对象即可。
#
# 沿用旧约束：模板必须选「从不被点击」的资产。RuleImage.match() 命中时会写 roi_front
# （module/atom/image.py:166-168），被探测写脏的资产若同时是要点击的目标
# （I_MAIN_GOTO_* / I_PET_HOUSE），就会点错位置；这几张月亮图只作判据与模板，从不被点击。
_MAIN_PROBE_DEFAULT = 'I_CHECK_MAIN_DEFAULT'
_MAIN_PROBE_NAME = 'I_CHECK_MAIN_{i}'
# 与 main_costume_model 的 range(1, 18) 是同一份「有几套」的事实的第二处副本：漏改会静默
# 少一套候选（那套皮肤永远探不到），取舍同 theme_costume_model 上方那段注释。
_MAIN_PROBE_MAX = 17

# 月亮命中之后，再用该套的 4 张 goto 资产（探索 / 召唤 / 町中 / 宠物屋）复核一遍：
# 至少要有 _MAIN_CONFIRM_MIN 张认得出，才认这次命中。
#
# 为什么要复核：月亮是**皮肤锚点**（区分度最好，实测真命中那套 0.92~0.99、其余套 ≤0.73），
# 但这 4 张才是真正要被点击、真正会因为皮肤不符而点不动的图。月亮孤零零命中、四张 goto
# 图集体不认，正是 2026-09-15 那次误判的形态。
#
# 门槛取 2 的依据（离线复现：7 张已知皮肤真值帧，对比「配置=真值」与「配置错时能否
# 纠回真值」两个指标）：
#   真命中那套的过阈值张数**最低是 2**——默认套在 08:53 那帧只有 2（探索 0.922 / 町中
#   0.863 过，召唤 0.797 差 0.003、宠物屋 0.378 不过）。取 3 会把默认套整批挡掉
#   （配置=真值 从 7/7 掉到 6/7）；取 0/1/2 在现有语料上结果完全一致（7/7、纠错 18/21），
#   所以取 2：最严，又不伤真命中。
_MAIN_CONFIRM_MIN = 2

# 结界探测同理只取页锚点这一个 key。映射表里另外两项是 RuleClick（两个点击区域），
# 它们没有模板图、进不了候选表——而且 C_REALM_* 也不该被 match() 写脏。
_REALM_PROBE_KEY = (KekkaiUtilizeAssets, 'I_REALM_PAGE')


def _ensure_probe_tables() -> None:
    """惰性构建庭院/卷轴/结界的探测候选表与庭院复核表，只做一次。"""
    global _probe_table_main, _probe_table_theme, _probe_table_realm, _probe_confirm_main
    if _probe_table_main is not None:
        return
    _ensure_default_main_snapshot()
    _ensure_default_theme_snapshot()
    _ensure_default_realm_snapshot()
    costume_assets = CostumeAssets()
    # 卷轴图放独立组件 tasks/Component/CostumeTheme/（与 CostumeBattle / CostumeShikigami
    # / CostumeCarpBanner 同构），不混进庭院那套 CostumeAssets
    theme_assets = CostumeThemeAssets()
    # 结界同理：皮肤版资源在 CostumeRealmAssets 上，活资产挂在 KekkaiUtilizeAssets
    realm_assets = CostumeRealmAssets()

    main_table: dict = {}
    # 默认套：模板是独立资产（_MAIN_PROBE_DEFAULT），直接用，不必再从出厂快照重建
    if hasattr(costume_assets, _MAIN_PROBE_DEFAULT):
        main_table[MainType.COSTUME_MAIN] = [getattr(costume_assets, _MAIN_PROBE_DEFAULT)]
    for i in range(1, _MAIN_PROBE_MAX + 1):
        value = _MAIN_PROBE_NAME.format(i=i)
        # 皮肤资产还没采集的套直接不进表，探测不会为它花时间
        if hasattr(costume_assets, value):
            main_table[getattr(MainType, f'COSTUME_MAIN_{i}')] = [getattr(costume_assets, value)]

    # 复核表：每套的 4 张 goto 资产，用 main_costume_model 的 value 名去 CostumeAssets 上取副本。
    # 默认套没有映射条目，它的 4 张从出厂快照重建（活对象已被就地改写）。
    confirm_table: dict = {}
    default_confirm = [_rebuild(_default_main_snapshot[t])
                       for t in _MAIN_TARGETS if t in _default_main_snapshot]
    if default_confirm:
        confirm_table[MainType.COSTUME_MAIN] = default_confirm
    for main_type, model in main_costume_model.items():
        rules = [_rebuild(_snapshot(getattr(costume_assets, v)))
                 for v in model.values() if hasattr(costume_assets, v)]
        # 一张都没采到就不进表 —— 探测时按「无法复核」放行，退回纯月亮判断
        if rules:
            confirm_table[main_type] = rules

    theme_table: dict = {}
    # 卷轴一套两张（收起态 + 展开态），任一命中即算该套命中
    default_theme = [_rebuild(_default_theme_snapshot[t])
                     for t in _THEME_TARGETS if t in _default_theme_snapshot]
    if default_theme:
        theme_table[ThemeType.COSTUME_THEME_DEFAULT] = default_theme
    for theme_type, model in theme_costume_model.items():
        assets = [getattr(theme_assets, v) for v in model.values() if hasattr(theme_assets, v)]
        if assets:
            theme_table[theme_type] = assets

    realm_table: dict = {}
    # 结界候选一套只取页锚点（_REALM_PROBE_KEY）。映射表里另外两项是 RuleClick，
    # 没有模板图可匹配，也不该被 match() 写脏 roi_front
    if _REALM_PROBE_KEY in _default_realm_snapshot:
        realm_table[RealmType.COSTUME_REALM_DEFAULT] = [
            _rebuild(_default_realm_snapshot[_REALM_PROBE_KEY])]
    for realm_type, model in realm_costume_model.items():
        value = model.get(_REALM_PROBE_KEY)
        if value and hasattr(realm_assets, value):
            realm_table[realm_type] = [getattr(realm_assets, value)]

    _probe_table_main = main_table
    _probe_table_theme = theme_table
    _probe_table_realm = realm_table
    _probe_confirm_main = confirm_table


def _ordered_candidates(table: dict, configured):
    """把配置的那一套提到最前，其余保持插入顺序。

    先试配置的原因：配置对的时候一次匹配就结束，探测零额外开销；配置错时只多花一次匹配。
    顺序固定是为了消歧义——「命中就回写」不设门槛，多个候选同时命中时取第一个。
    """
    if configured in table:
        yield configured, table[configured]
    for key, assets in table.items():
        if key != configured:
            yield key, assets


def _warn_probe_missed_once(what: str) -> None:
    """零命中告警，每次解锁周期只打一条。"""
    global _probe_warned
    if _probe_warned:
        return
    _probe_warned = True
    logger.warning(f'Costume {what} probe found no match; '
                   f'the current skin may not be collected yet')


def _warn_realm_probe_missed_once() -> None:
    """结界探测零命中告警，每次解锁周期只打一条。

    不复用 _warn_probe_missed_once：两者跑在不同时机（main/theme 在登录的庭院帧上，
    结界在结界页帧上），共用一个标志会让先探的那个把后探的那个的告警吞掉；
    文案也要分开，否则日志里看不出该去补哪套皮肤的图。
    """
    global _realm_probe_warned
    if _realm_probe_warned:
        return
    _realm_probe_warned = True
    logger.warning('Costume realm probe found no match; '
                   'the current realm skin may not be collected yet')


def reset_costume_module_state() -> None:
    """把模块级状态复位到出厂。仅供测试隔离使用。

    必须有这个函数：模块级快照会跨用例泄漏。fixture 把 _default_main_snapshot 复位为
    None，之后 _ensure_default_main_snapshot() 就会拿**被上一个用例改过的**资产值去冻结
    「出厂快照」，后续所有断言都建立在一个假的出厂值上。

    tests/device/ 的庭院用例调的是私有 _app_handle_login()，不经过解锁入口，
    也必须靠 autouse fixture 调这个函数，否则探针锁会跨用例污染。
    """
    global _default_main_snapshot, _default_theme_snapshot, _default_realm_snapshot
    global _applied_main, _applied_theme, _applied_realm
    global _probe_table_main, _probe_table_theme, _probe_table_realm, _probe_confirm_main
    global _main_probe_locked, _theme_probe_locked, _realm_probe_locked
    global _probe_warned, _realm_probe_warned
    _default_main_snapshot = None
    _default_theme_snapshot = None
    _default_realm_snapshot = None
    _applied_main = None
    _applied_theme = None
    _applied_realm = None
    _probe_table_main = None
    _probe_table_theme = None
    _probe_table_realm = None
    _probe_confirm_main = None
    _main_probe_locked = False
    _theme_probe_locked = False
    _realm_probe_locked = False
    _probe_warned = False
    _realm_probe_warned = False
    _probe_timer.clear()
    _realm_probe_timer.clear()


def release_costume_probe_locks() -> None:
    """解锁探测。在 _app_handle_login() 开头调用。

    为什么是私有的 _app_handle_login 而不是公开的 app_handle_login：后者的
    `for _ in range(attempts)` 重试循环在函数体内（login.py:288-293），放循环外面
    只会解锁一次，第二轮重试就带着上一轮的锁状态进来。

    这次登录可能落到另一个号上，之前锁定的皮肤对它不一定成立；解锁后同一次登录里的
    探测会重新锁定。连节流一起 clear：切号后应该立刻能探，不该白等一个节流窗口。

    结界锁也在这里放掉：换号后结界皮肤同样可能不同，而结界探测要等到任务真正走进
    结界页才跑，锁留着会让新号第一次进结界时探不了。
    """
    global _main_probe_locked, _theme_probe_locked, _realm_probe_locked
    global _probe_warned, _realm_probe_warned
    _main_probe_locked = False
    _theme_probe_locked = False
    _realm_probe_locked = False
    _probe_warned = False
    _realm_probe_warned = False
    _probe_timer.clear()
    _realm_probe_timer.clear()


class CostumeBase:
    def check_costume(self, config: CostumeConfig=None):
        if config is None:
            config: CostumeConfig = self.config.model.global_game.costume_config
        self.check_costume_main(config.costume_main_type)
        self.check_costume_theme(config.costume_theme_type)
        self.check_costume_carpbanner(config.costume_carpbanner_type)
        self.check_costume_battle(config.costume_battle_type)
        self.check_costume_shikigami(config.costume_shikigami_type)
        self.check_costume_realm(config.costume_realm_type)

    def replace_img(self,
                    target,
                    asset_after: RuleImage,
                    rp_roi_back: bool = True):
        """把 target 指向的资产对象就地改写为 asset_after。

        就地改写意味着影响是全进程的——这正是本机制生效的前提：所有任务读到的都是
        同一个对象，改一次处处生效。

        target 支持两种形态：
        - ``(资产类, 属性名)``：直接落到类属性上，不依赖调用方任务的 MRO。本次新迁的
          main / theme 两条链用这种。目标类没有该属性时静默跳过（跨类的正常情况）。
        - ``str``：旧的 ``getattr(self, 属性名)`` 路径，行为逐字节保持现状。
          carpbanner / battle / shikigami 三条链仍用它——它们的调用方任务都通过 MRO
          持有对应资产，替换本来就在正常工作，没有迁移的必要（那 30 多个 key 横跨
          GeneralBattleAssets / SwitchSoulAssets / 各 Costume*Assets 共 5 个类）。

        roi_front 走拷贝而不是共享引用：RuleImage.match() 命中时会写 roi_front[0]/[1]，
        共享引用会让一处匹配串改另一处资产。
        """
        if isinstance(target, tuple):
            asset_cls, attr = target
            if not hasattr(asset_cls, attr):
                return
            obj: RuleImage = getattr(asset_cls, attr)
        else:
            if not hasattr(self, target):
                return
            obj: RuleImage = getattr(self, target)
        obj.roi_front = list(asset_after.roi_front)
        if rp_roi_back:
            obj.roi_back = asset_after.roi_back
        # RuleClick 没有 threshold / file（它不做模板匹配），只同步区域
        if not isinstance(obj, RuleClick):
            obj.threshold = asset_after.threshold
            obj.file = asset_after.file
        _clear_rule_image_cache(obj)

    def check_costume_main(self, main_type: MainType):
        """套用指定庭院皮肤。幂等：同一套重复调用直接返回。

        默认套不是「什么都不做」而是「按出厂快照还原」——运行时探测会出现
        main5 → 默认 的回切，早退式实现会让默认套永远回不去。
        """
        global _applied_main
        _ensure_default_main_snapshot()
        if main_type == _applied_main:
            return
        logger.info(f'Switch main costume to {main_type}')
        targets: dict = {}
        if main_type == MainType.COSTUME_MAIN:
            # 默认套的资产对象已被就地改写，出厂值只能从快照重建
            targets = {t: _rebuild(s) for t, s in _default_main_snapshot.items()}
        else:
            costume_assets = CostumeAssets()
            for target, value in main_costume_model[main_type].items():
                if not hasattr(costume_assets, value):
                    # 皮肤资产还没采集（例如只登记了枚举项），跳过而不是抛异常
                    logger.warning(f'Main costume asset {value} not found, skip')
                    continue
                targets[target] = getattr(costume_assets, value)
        for target, asset in targets.items():
            self.replace_img(target, asset)
        if not targets:
            # 一个都没换成（资产未采集）：不能记成已套用，否则幂等守卫让它永不重试
            logger.warning(f'Main costume {main_type} applied nothing, keep previous state')
            return
        _applied_main = main_type

    def check_costume_theme(self, theme_type: ThemeType):
        """套用指定卷轴（主题）皮肤。幂等；默认套按出厂快照还原。

        卷轴图在独立组件 CostumeThemeAssets 上（与 CostumeBattle / CostumeShikigami 同构）。
        新皮肤的图还没采集时（映射里的资产在该类上不存在）打警告后跳过，
        与 check_costume_carpbanner / check_costume_shikigami 的既有处理一致。
        """
        global _applied_theme
        _ensure_default_theme_snapshot()
        if theme_type == _applied_theme:
            return
        logger.info(f'Switch theme costume to {theme_type}')
        targets: dict = {}
        if theme_type == ThemeType.COSTUME_THEME_DEFAULT:
            targets = {t: _rebuild(s) for t, s in _default_theme_snapshot.items()}
        else:
            costume_assets = CostumeThemeAssets()
            for target, value in theme_costume_model.get(theme_type, {}).items():
                if not hasattr(costume_assets, value):
                    logger.warning(f'Theme costume asset {value} not found, skip')
                    continue
                targets[target] = getattr(costume_assets, value)
        for target, asset in targets.items():
            self.replace_img(target, asset)
        if not targets:
            # 一个都没换成（本批 COSTUME_THEME_1 就是这种情况：图还没采集）：
            # 不能记成已套用，否则幂等守卫让它永不重试
            logger.warning(f'Theme costume {theme_type} applied nothing, keep previous state')
            return
        _applied_theme = theme_type

    def probe_costume_main(self) -> MainType | None:
        """在当前截图上轮询庭院候选，返回第一个**通过复核**的皮肤类型，都不中返回 None。

        两级判断：先看月亮模板（皮肤锚点，区分度最好），命中后再用该套的 4 张 goto 资产复核
        （门槛见 _MAIN_CONFIRM_MIN）。复核不过是**继续往下扫**，不是就此放弃——月亮在某套上
        偶然过线、而那一套的 goto 图集体不认时，下一套才是真命中的机会。

        只读 self.device.image，不重新截图——调用方刚取的那一帧就是判定依据。
        """
        _ensure_probe_tables()
        configured = self.config.model.global_game.costume_config.costume_main_type
        for main_type, assets in _ordered_candidates(_probe_table_main, configured):
            if not any(self.appear(asset) for asset in assets):
                continue
            if not self._confirm_main_candidate(main_type):
                continue
            return main_type
        return None

    def _confirm_main_candidate(self, main_type: MainType) -> bool:
        """用该套的 4 张 goto 资产复核月亮模板的这次命中。

        用的必须是**副本**（见 _probe_confirm_main 的说明）：这几张对应的活资产是要被点击
        的目标，拿活对象来匹配会把它的 roi_front 写脏，随后 replace_img 又把这个脏值拷进
        GameUiAssets，点击落点就跟着探测那一次匹配跑偏了。

        该套一张 goto 图都没采集到（复核表里没有它）时**放行**：没有复核依据就不能据此否决，
        退回纯月亮判断，与引入复核之前的行为一致。
        """
        rules = (_probe_confirm_main or {}).get(main_type) or []
        if not rules:
            return True
        return sum(1 for rule in rules if self.appear(rule)) >= _MAIN_CONFIRM_MIN

    def probe_costume_theme(self) -> ThemeType | None:
        """在当前截图上轮询卷轴候选。一套两张（收起态 / 展开态），任一张命中即算命中。"""
        _ensure_probe_tables()
        configured = self.config.model.global_game.costume_config.costume_theme_type
        for theme_type, assets in _ordered_candidates(_probe_table_theme, configured):
            for asset in assets:
                if self.appear(asset):
                    return theme_type
        return None

    def probe_costume_realm(self) -> RealmType | None:
        """在当前截图上轮询结界皮肤候选，返回第一个命中的，都不中返回 None。

        只读 self.device.image，不重新截图——调用方刚取的那一帧就是判定依据。

        候选一套只有一张：页锚点（`I_REALM_PAGE`）。映射表里另外两项是 RuleClick，
        没有模板图可匹配。

        **不能挂到登录的庭院帧上**（try_detect_costume 那条路）：结界换的是结界页的
        背景，庭院那帧里根本看不到，探了必然零命中。调用方是结界任务自己，只在真正
        站在结界页上时探。
        """
        _ensure_probe_tables()
        configured = self.config.model.global_game.costume_config.costume_realm_type
        for realm_type, assets in _ordered_candidates(_probe_table_realm, configured):
            for asset in assets:
                if self.appear(asset):
                    return realm_type
        return None

    def _apply_detected_costume_main(self, detected: MainType) -> None:
        """套用探测到的庭院皮肤并回写配置（配置自我修正）。

        只是一层薄包装：资产替换仍走 check_costume_main（幂等、支持回切默认套），
        这里只多做「回写配置 + 打日志」。

        命中就回写是既定语义：配置只是快路径，写错了下一次探测会纠正。回写必须留日志，
        那是这个「不设可信度门槛」的选择唯一的护栏。
        """
        config = self.config.model.global_game.costume_config
        old = config.costume_main_type
        self.check_costume_main(detected)
        if old == detected:
            logger.info(f'Costume main detected {detected} (matches config)')
            return
        config.costume_main_type = detected
        self.config.save()
        logger.info(f'Costume main detected {old} -> {detected}, config updated')

    def _apply_detected_costume_theme(self, detected: ThemeType) -> None:
        """套用探测到的卷轴皮肤并回写配置。语义同 _apply_detected_costume_main。"""
        config = self.config.model.global_game.costume_config
        old = config.costume_theme_type
        self.check_costume_theme(detected)
        if old == detected:
            logger.info(f'Costume theme detected {detected} (matches config)')
            return
        config.costume_theme_type = detected
        self.config.save()
        logger.info(f'Costume theme detected {old} -> {detected}, config updated')

    def _apply_detected_costume_realm(self, detected: RealmType) -> None:
        """套用探测到的结界皮肤并回写配置。语义同 _apply_detected_costume_main。"""
        config = self.config.model.global_game.costume_config
        old = config.costume_realm_type
        self.check_costume_realm(detected)
        if old == detected:
            logger.info(f'Costume realm detected {detected} (matches config)')
            return
        config.costume_realm_type = detected
        self.config.save()
        logger.info(f'Costume realm detected {old} -> {detected}, config updated')

    def try_detect_costume_main(self) -> MainType | None:
        """带节流与锁的庭院探测入口。命中即套用 + 回写并锁定。

        零命中打一条 warning（当前皮肤可能尚未采集），每次解锁周期只打一条——
        探测受 2s 节流，登录卡住时会反复零命中，不加限制会刷屏。
        """
        global _main_probe_locked, _probe_warned
        if _main_probe_locked or not _probe_timer.reached():
            return None
        _probe_timer.reset()
        detected = self.probe_costume_main()
        if detected is None:
            _warn_probe_missed_once('main')
            return None
        _main_probe_locked = True
        _probe_warned = False
        self._apply_detected_costume_main(detected)
        return detected

    def try_detect_costume_realm(self) -> bool:
        """带节流与锁的结界探测入口。命中即套用 + 回写并锁定。**返回「是否刚修好了皮肤资产」。**

        返回值语义与 try_detect_costume 一致，调用方拿到 True 应该重截一帧重判，
        让修正后的 I_REALM_PAGE（page_guild_realm 的 check_button）在下一帧生效；
        **不要把它直接当成「在结界里」的证据**——它只在命中那一帧为真，而判「到了结界」
        仍要靠页面判据本身。

        零命中打一条 warning（当前皮肤可能尚未采集），每次解锁周期只打一条。
        """
        global _realm_probe_locked, _realm_probe_warned
        if _realm_probe_locked or not _realm_probe_timer.reached():
            return False
        _realm_probe_timer.reset()
        detected = self.probe_costume_realm()
        if detected is None:
            _warn_realm_probe_missed_once()
            return False
        _realm_probe_locked = True
        _realm_probe_warned = False
        self._apply_detected_costume_realm(detected)
        return True

    def realm_skin_confirmed(self) -> bool:
        """结界皮肤是否已确认（探测命中过至少一次）。

        结界页上的点击区域都是**按皮肤**的坐标（C_REALM_GROWN / C_REALM_CARD），皮肤没认出来
        就没有任何依据判断这些坐标能不能用——此时按坐标点就是盲点。探测命中即上锁，
        所以那把锁就是「已确认」的判据；release_costume_probe_locks()（登录开头）会清掉它。

        注意它**不是**「皮肤与配置相符」的判据：探测是配置优先扫候选，配置那套相符时
        第一个就命中并上锁，所以正常路径同样是 True。
        """
        return _realm_probe_locked

    def try_detect_costume(self) -> bool:
        """庭院与卷轴各探一次，共用一次节流窗口。**返回「是否刚修好了资产」，不是「是否在庭院」。**

        不能简单串两个 try_detect_costume_*：它们各自会 reset 节流，先跑的那个命中后
        会把后跑的那个挡在窗口外。这里只判一次节流，两个都探到。

        调用方拿到 True 的正确用法是重新截图重判（`continue`），让刚换上的资产在下一帧
        生效 —— **不要把返回值直接当成「在庭院」的证据**：探针受 2s 节流 + 成功即锁，
        只在命中那一帧为真。

        触发点在**导航失败**那一支（game_ui.py 的 `_execute_path`）：庭院入口那几张图
        随皮肤变，与配置不符就点不动，那正是它唯一还能帮上忙的场合。登录期**刻意不探**
        ——庭院判据已皮肤无关（活动图标 / 加成按钮），那里探不出有价值的东西，反而会在
        「进入游戏」过渡帧上盲扫 16 张月亮模板而误判（15/16 号阈值 0.7 且搜索窗近全屏，
        2026-09-15 事故的起点就是它）。

        已知残留：命中即锁，而锁只在下次登录的 release_costume_probe_locks() 里解。
        所以一次误判之后，后续导航失败也修不回来，得等下一次登录。
        """
        global _main_probe_locked, _theme_probe_locked, _probe_warned
        if not _probe_timer.reached():
            return False
        _probe_timer.reset()
        hit = False
        if not _main_probe_locked:
            detected = self.probe_costume_main()
            if detected is not None:
                _main_probe_locked = True
                self._apply_detected_costume_main(detected)
                hit = True
        if not _theme_probe_locked:
            detected = self.probe_costume_theme()
            if detected is not None:
                _theme_probe_locked = True
                self._apply_detected_costume_theme(detected)
                hit = True
        if not hit:
            _warn_probe_missed_once('main/theme')
            return False
        _probe_warned = False
        return True

    def check_costume_carpbanner(self, carpbanner_type: CarpBannerType):
        if carpbanner_type == CarpBannerType.COSTUME_CARPBANNER_DEFAULT:
            return
        logger.info(f'Switch carp banner theme {carpbanner_type} (override realm assets)')
        carpbanner_assets = CostumeCarpBannerAssets()
        model = carpbanner_costume_model.get(carpbanner_type, {})
        for key, value in model.items():
            if not hasattr(carpbanner_assets, value):
                logger.warning(f'Carp banner asset {value} not found, skip')
                continue
            assert_value: RuleImage = getattr(carpbanner_assets, value)
            # 执行替换（覆盖结界皮肤的同名key）
            self.replace_img(key, assert_value)

    def check_costume_battle(self, battle_type: BattleType):
        if battle_type == BattleType.COSTUME_BATTLE_DEFAULT:
            return
        logger.info(f'Switch battle theme {battle_type}')
        costume_battle_assets = CostumeBattleAssets()
        for key, value in battle_theme_model[battle_type].items():
            if not hasattr(costume_battle_assets, value):
                # 尚未采集完成的资产，跳过（沿用 shikigami 链的写法）。
                # I_BATTLE_INFO 要逐套截图，未采的主题在 assets.py 里还没有
                # 对应属性，这里跳过可让用户分批采而不必一次采齐 13 套。
                continue
            assert_value: RuleImage = getattr(costume_battle_assets, value)
            # 绿标的坐标点范围不变
            if key == 'I_LOCAL':
                self.replace_img(key, assert_value, rp_roi_back=False)
            else:
                self.replace_img(key, assert_value)

    def check_costume_shikigami(self, shikigami_type: ShikigamiType):
        if shikigami_type == ShikigamiType.COSTUME_SHIKIGAMI_DEFAULT:
            return
        logger.info(f'Switch shikigami theme {shikigami_type}')
        shikigami_assets = CostumeShikigamiAssets()
        model = shikigami_costume_model.get(shikigami_type, {})
        for key, value in model.items():
            if not hasattr(shikigami_assets, value):
                # 尚未采集完成的资产，跳过
                continue
            assert_value: RuleImage = getattr(shikigami_assets, value)
            # 一般不需要固定 back ROI，如确有需要可在此为特例设置 rp_roi_back=False
            self.replace_img(key, assert_value)

    def check_costume_realm(self, realm_type: RealmType):
        """套用指定结界皮肤。幂等；默认套按出厂快照还原。

        结构与 check_costume_theme 同构，理由也一样：结界皮肤换的那三样资产
        （页面锚点 / 育成区域 / 挂卡区域）都是 KekkaiUtilizeAssets 上的类属性活对象，
        replace_img 就地改写一次，KekkaiUtilize 与 KekkaiActivation 后续读到的就是新皮肤。
        CostumeRealm/realm<N>/ 下的规则文件只是这些「皮肤版」资产的来源。

        默认套（妖伞结界）不登记映射：它的资产就是 KekkaiUtilizeAssets 上的出厂值，
        被就地改写后原值再没有别的引用，只能靠 _default_realm_snapshot 重建回切。

        图还没采集时（映射里的属性名在 CostumeRealmAssets 上不存在）打警告后跳过；
        一个都没换成就不记 _applied_realm，否则幂等守卫会让它永不重试。
        """
        global _applied_realm
        _ensure_default_realm_snapshot()
        if realm_type == _applied_realm:
            return
        logger.info(f'Switch realm costume to {realm_type}')
        targets: dict = {}
        if realm_type == RealmType.COSTUME_REALM_DEFAULT:
            targets = {t: _rebuild(s) for t, s in _default_realm_snapshot.items()}
        else:
            realm_assets = CostumeRealmAssets()
            for target, value in realm_costume_model.get(realm_type, {}).items():
                if not hasattr(realm_assets, value):
                    logger.warning(f'Realm costume asset {value} not found, skip')
                    continue
                targets[target] = getattr(realm_assets, value)
        for target, asset in targets.items():
            self.replace_img(target, asset)
        if not targets:
            logger.warning(f'Realm costume {realm_type} applied nothing, keep previous state')
            return
        _applied_realm = realm_type


if __name__ == '__main__':
    c = CostumeBase()
    c.check_costume_main(MainType.COSTUME_MAIN_2)
