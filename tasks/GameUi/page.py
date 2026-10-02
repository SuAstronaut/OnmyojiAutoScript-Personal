from itertools import compress

import random

import traceback
from module.atom.click import RuleClick
from tasks.BondlingFairyland.assets import BondlingFairylandAssets
from tasks.GlobalGame.assets import GlobalGameAssets as GGA
from tasks.GameUi.assets import GameUiAssets as G
from tasks.KekkaiUtilize.assets import KekkaiUtilizeAssets
from tasks.Restart.assets import RestartAssets
from tasks.base_task import BaseTask as BT
from tasks.RyouToppa.assets import RyouToppaAssets
from tasks.Component.GeneralInvite.assets import GeneralInviteAssets


class PageRegistry:
    _registry = []

    @classmethod
    def register(cls, page):
        cls._registry.append(page)

    @classmethod
    def all(cls):
        return list(cls._registry)


class Page:
    def __init__(self, check_button, links=None, *, enabled=None, overlay=False):
        if links is None:
            links = {}
        self.check_button = check_button
        self.links = links
        # 作用域按当前任务判断，不修改共享 Page；覆盖页优先于遮罩下的普通页面。
        self.enabled = enabled
        self.overlay = overlay
        self.additional: list = None  # 附加按钮或者是ocr检测按钮
        (filename, line_number, function_name, text) = traceback.extract_stack()[-2]
        self.name = text[:text.find('=')].strip()
        PageRegistry.register(self)

    def __eq__(self, other):
        if other is None:
            return False
        return self.name == other.name

    def __hash__(self):
        return hash(self.name)

    def __str__(self):
        return self.name

    def link(self, button, destination):
        self.links[destination] = button


#登录login
page_login = Page(G.I_CHECK_LOGIN_FORM)
# 探索exploration
page_exploration = Page(G.I_CHECK_EXPLORATION)
# 个性化推荐是独立覆盖页，注册在绑定手机之前以优先识别并关闭。
page_personalized_recommendation = Page(G.I_CLICK_REFUSE, overlay=True)


def _bind_phone_appear(task) -> bool:
    """专属提示启动绑定流程；通用取消按钮只允许延续已经识别出的绑定页面。"""
    return (task.appear(RestartAssets.I_LOGIN_LOGIN_GOTO_BIND_PHONE)
            or (getattr(task, 'ui_current', None) == page_bind_phone
                and task.appear(RestartAssets.I_LOGIN_LOGIN_CANCEL_BIND_PHONE)))


# 绑定手机号是前景覆盖页，优先于庭院及其同心集结横幅；两个阶段都保持该 Page。
# 两步操作：run_additional 点击“前往绑定”，link 点击“取消绑定”关闭，再处理后方集结。
page_bind_phone = Page(_bind_phone_appear, overlay=True)
page_bind_phone.additional = [RestartAssets.I_LOGIN_LOGIN_GOTO_BIND_PHONE]
# Main Home 主页
# check_button 是**列表**：两个锚点都是皮肤无关的固定 UI，任一命中即算在庭院。
#   活动图标（I_CHECK_MAIN）—— 主判据。它在右侧，不被顶部的同心队横幅遮挡，
#       所以二十多处 appear(I_CHECK_MAIN) 的「回庭院」判定也跟着受益。
#   加成按钮（I_MAIN_BUFF）—— 兜底。活动入口没开（无进行中的活动）时右侧没有那张图，
#       而加成按钮是核心 UI、一直挂着；它唯一的缺口是被同心队横幅盖住
#       （实测那批帧上它只有 0.335~0.369），正好由活动图标补上。
# 两者都**不随庭院皮肤变**，所以既不该进 main_costume_model 的替换表，也不能拿皮肤图当判据。
page_main = Page([G.I_CHECK_MAIN, G.I_MAIN_BUFF])
# 卷轴收起时庭院上只剩这四样要处理的。展开/收起卷轴不在 additional 里——
# 那是「前往 page_theme / 回到 page_main」的页面跳转，由下面的 link 承担。
page_main.additional = [G.I_CHECK_YARD, G.I_AD_CLOSE_RED, G.I_BACK_FRIENDS, RestartAssets.I_CANCEL_BATTLE]
page_personalized_recommendation.link(button=G.I_CLICK_REFUSE, destination=page_main)
page_bind_phone.link(button=RestartAssets.I_LOGIN_LOGIN_CANCEL_BIND_PHONE, destination=page_main)

# 卷轴展开态：庭院右下角卷轴打开后，底部露出一排入口（图鉴/珍旅居/组队/阴阳寮/商店/合战/好友/阴阳术/式神录）。
#
# 它**不参与页面识别**：卷轴开合不影响「当前在庭院」这个判断，ui_get_current_page 在庭院
# 一律返回 page_main（展开态也只返回 page_main）。page_theme 只是导航图上的中转节点 ——
# 去底部那排入口时，路径 page_main -> page_theme -> 目标 会顺带把卷轴展开。
# ui_wait_until_appear(page_theme) 直接查 check_button、不经过 ui_get_current_page，
# 所以这条两跳路径照样推进得动（_execute_path 命中它会自己把 ui_current 设成 page_theme）。
# 第一个 check_button 是卷轴展开图（随卷轴皮肤变，theme_costume_model 换的就是它）；
# 第二个是卷轴展开后才会出现的式神录按钮，作为卷轴图失效时的兜底判据（不随任何皮肤变）。
page_theme = Page([RestartAssets.I_LOGIN_SCROOLL_OPEN, G.I_MAIN_GOTO_SHIKIGAMI_RECORDS])
# 展开态同样要处理这四样：弹窗/邀战都是覆盖层，不随卷轴开合变化，少了这套在 page_theme
# 上就没人关弹窗，去式神录那一步会被挡住。
page_theme.additional = [G.I_CHECK_YARD, G.I_AD_CLOSE_RED, G.I_BACK_FRIENDS, RestartAssets.I_CANCEL_BATTLE]
# 展开卷轴用「卷轴收起图」而不是那块点击区域：点击区域是 toggle，卷轴已经展开时点它反而会
# 把卷轴收回去；而收起图标在展开态根本不匹配，appear_then_operate 不会误点。
# 卷轴已经展开时这一步就没有可点的东西，由 _execute_path 的「跳转按钮不在、目标页已可见」
# 判据提前收工，不会再空等满 6 秒。
page_main.link(button=RestartAssets.I_LOGIN_SCROOLL_CLOSE, destination=page_theme)
# 回庭院用同一张图、同一条规则：卷轴状态只朝「展开」方向走（收起态一定要打开，展开态绝不
# 能收起）。收起图只在收起态匹配，展开态一下都点不出去，于是走上面那条「目标页已可见」的
# 判据直接收工；而那块点击区域是 toggle、与状态无关，放这里会在展开态把卷轴收回去。
page_theme.link(button=RestartAssets.I_LOGIN_SCROOLL_CLOSE, destination=page_main)
# 召唤summon
page_summon = Page(G.I_CHECK_SUMMON)
page_summon.additional = [G.O_SUMMON_BACK_Y, G.I_SUMMON_BACK_R,G.I_SUMMON_BACK_TICKET]
page_summon.link(button=G.I_SUMMON_GOTO_MAIN, destination=page_main)
page_main.link(button=G.I_MAIN_GOTO_SUMMON, destination=page_summon)
# 探索exploration
#page_exploration = Page(G.I_CHECK_EXPLORATION)
page_exploration.link(button=G.I_BACK_YOLLOW, destination=page_main)
page_main.link(button=G.I_MAIN_GOTO_EXPLORATION, destination=page_exploration)
# 町中town
page_town = Page(G.I_CHECK_TOWN)
page_town.link(button=G.I_TOWN_GOTO_MAIN, destination=page_main)
page_main.link(button=G.I_MAIN_GOTO_TOWN, destination=page_town)

# ************************************* 探索部分 *****************************************#
# 觉醒 awake zones
page_awake_zones = Page(G.I_CHECK_AWAKE)
page_awake_zones.link(button=G.I_BACK_YOLLOW, destination=page_exploration)
page_exploration.link(button=G.I_EXPLORATION_GOTO_AWAKE_ZONE, destination=page_awake_zones)
# 御魂 soul zones
page_soul_zones = Page(G.I_CHECK_SOUL_ZONES)
page_soul_zones.link(button=G.I_BACK_YOLLOW, destination=page_exploration)
page_exploration.link(button=G.I_EXPLORATION_GOTO_SOUL_ZONE, destination=page_soul_zones)
# 结界突破 realm raid
page_realm_raid = Page(G.I_CHECK_REALM_RAID)
page_realm_raid.link(button=G.I_REALM_RAID_GOTO_EXPLORATION, destination=page_exploration)
page_exploration.link(button=G.I_EXPLORATION_GOTO_REALM_RAID, destination=page_realm_raid)
# 寮结界突破右上角 kekkai toppa
page_kekkai_toppa = Page(G.I_KEKKAI_TOPPA)
page_kekkai_toppa.link(button=G.I_REALM_RAID_GOTO_EXPLORATION, destination=page_exploration)
page_realm_raid.link(button=RyouToppaAssets.I_RYOU_TOPPA, destination=page_kekkai_toppa)
page_kekkai_toppa.link(button=G.I_RYOUTOPPA_GOTO_REALMRAID, destination=page_realm_raid)
# 御灵 goryou realm
page_goryou_realm = Page(G.I_CHECK_GORYOU)
page_goryou_realm.link(button=G.I_BACK_YOLLOW, destination=page_exploration)
page_exploration.link(button=G.I_EXPLORATION_GOTO_GORYOU_REALM, destination=page_goryou_realm)
# 委派 delegation
page_delegation = Page(G.I_CHECK_DELEGATION)
page_delegation.link(button=G.I_BACK_YOLLOW, destination=page_exploration)
page_exploration.link(button=G.I_EXPLORATION_GOTO_DELEGATION, destination=page_delegation)
# 秘闻副本 SECRET zones
page_secret_zones = Page(G.I_CHECK_SECRET_ZONES)
page_secret_zones.link(button=G.I_BACK_YOLLOW, destination=page_exploration)
page_exploration.link(button=G.I_EXPLORATION_GOTO_SECRET_ZONES, destination=page_secret_zones)
# 地域鬼王 area boss
page_area_boss = Page(G.I_CHECK_AREA_BOSS)
page_area_boss.link(button=G.I_BACK_YOLLOW, destination=page_exploration)
page_exploration.link(button=G.I_EXPLORATION_GOTO_AREA_BOSS, destination=page_area_boss)
#妖气探索
page_youki = Page(G.I_CHECK_YOUKI)
page_youki.link(button=G.I_BACK_YOLLOW, destination=page_exploration)
page_exploration.link(button=G.I_EXPLORATION_GOTO_YOUKI, destination=page_youki)
# 平安奇谭 heian kitan
page_heian_kitan = Page(G.I_CHECK_HEIAN_KITAN)
page_heian_kitan.link(button=G.I_CHECK_HEIAN_KITAN, destination=page_exploration)
page_exploration.link(button=G.I_EXPLORATION_GOTO_HEIAN_KITAN, destination=page_heian_kitan)
# 六道之门 six gates
page_six_gates = Page(G.I_CHECK_SIX_GATES)
page_six_gates.link(button=G.I_SIX_GATES_GOTO_EXPLORATION, destination=page_exploration)
page_exploration.link(button=G.I_EXPLORATION_GOTO_SIX_GATES, destination=page_six_gates)
# 契灵之境 bondling fairyland
page_bondling_fairyland = Page(BondlingFairylandAssets.I_BALL_AREA)
page_bondling_fairyland.link(button=G.I_BACK_YOLLOW, destination=page_exploration)
page_exploration.link(button=G.I_EXPLORATION_GOTO_BONDLING_FAIRYLAND, destination=page_bondling_fairyland)
# 英杰试炼 hero test
page_hero_test = Page(G.I_CHECK_HERO_TEST)
page_hero_test.link(button=G.I_BACK_YOLLOW, destination=page_exploration)
page_exploration.link(button=G.I_EXPLORATION_GOTO_HERO_TEST, destination=page_hero_test)

# ************************************* 町中部分 *****************************************#
# 斗技 duel
page_duel = Page(G.I_CHECK_DUEL)
page_duel.link(button=G.I_BACK_YOLLOW, destination=page_town)
page_town.link(button=G.I_TOWN_GOTO_DUEL, destination=page_duel)
# 逢魔之时 demon_encounter
page_demon_encounter = Page(G.I_CHECK_DEMON_ENCOUNTER)
page_demon_encounter.link(button=G.I_BACK_YOLLOW, destination=page_town)
page_town.link(button=G.I_TOWN_GOTO_DEMON_ENCOUNTER, destination=page_demon_encounter)
# 逢魔之时现世逢魔 demon_encounter_realworld
page_demon_encounter_realworld = Page(G.I_CHECK_DEMON_ENCOUNTER_REALWORLD)
page_demon_encounter_realworld.link(button=G.I_BACK_YOLLOW, destination=page_demon_encounter)
page_demon_encounter.link(button=G.I_DEMON_ENCOUNTER_REALWORLD_GOTO, destination=page_demon_encounter_realworld)
# 狩猎战 hunt
page_hunt = Page(G.I_CHECK_HUNT)
page_hunt.link(button=G.I_BACK_YOLLOW, destination=page_town)
page_town.link(button=G.I_TOWN_GOTO_HUNT, destination=page_hunt)
# 狩猎战麒麟 hunt_kirin
page_hunt_kirin = Page(G.I_CHECK_HUNT_KIRIN)
page_hunt_kirin.link(button=G.I_BACK_YOLLOW, destination=page_town)
page_town.link(button=G.I_TOWN_GOTO_HUNT, destination=page_hunt_kirin)
# 协同斗技 draft_duel
page_draft_duel = Page(G.I_CHECK_DRAFT_DUEL)
page_draft_duel.link(button=G.I_BACK_YOLLOW, destination=page_town)
page_town.link(button=G.I_TOWN_GOTO_DRAFT_DUEL, destination=page_draft_duel)
# 百鬼弈 hyakkisen
page_hyakkisen = Page(G.I_CHECK_HYAKKISEN)
page_hyakkisen.link(button=G.I_BACK_YOLLOW, destination=page_town)
page_town.link(button=G.I_TOWN_GOTO_HYAKKISEN, destination=page_hyakkisen)
# 百鬼夜行
page_hyakkiyakou = Page(G.I_CHECK_KYAKKIYAKOU)
page_hyakkiyakou.link(button=G.I_HYAKKIYAKOU_CLOSE, destination=page_town)
page_town.link(button=G.I_TOWN_GOTO_HYAKKIYAKOU, destination=page_hyakkiyakou)

# ************************************* 庭院部分 *****************************************#
# 式神录 shikigami_records
page_shikigami_records = Page(G.I_CHECK_RECORDS)
page_shikigami_records.additional = [[G.I_DLC_EXIT, 1.5],[G.I_AD_DISAPPEAR_2, 1.5],[G.I_DLC_EXIT,1.5]]
page_shikigami_records.link(button=G.I_BACK_Y, destination=page_main)
# ↓ 以下这排页面都挂在 page_theme 下而非 page_main：它们的入口在屏幕底部 y≈656 一带，
#   卷轴收起时被卷轴挡住，必须先展开卷轴（page_main -> page_theme）才点得到。
page_theme.link(button=G.O_PAGE_SHIKIGAMI_RECORDS, destination=page_shikigami_records)
# 阴阳术 onmyodo
page_onmyodo = Page(G.I_CHECK_ONMYODO)
page_onmyodo.link(button=G.I_BACK_Y, destination=page_main)
page_theme.link(button=G.O_PAGE_ONMYODO, destination=page_onmyodo)
# 好友 friends
page_friends = Page(G.I_CHECK_FRIENDS)
page_friends.additional = [[G.I_FAVORABILITY_UP, G.C_FAVORABILITY_UP]]
page_friends.link(button=G.I_BACK_FRIENDS, destination=page_main)
page_theme.link(button=G.O_PAGE_FRIENDS, destination=page_friends)
# 花合战 daily
page_daily = Page(G.I_CHECK_DAILY)
page_daily.additional = [G.O_CLICK_CLOSE_1, G.O_CLICK_CLOSE_2]
page_daily.link(button=G.I_BACK_DAILY, destination=page_main)
page_theme.link(button=G.O_PAGE_DAILY, destination=page_daily)
from tasks.DailyTrifles.assets import DailyTriflesAssets

# 商店 mall
page_mall = Page(check_button=[G.I_CHECK_MALL, DailyTriflesAssets.I_ROOM_GIFT])
page_mall.additional = [[G.I_AD_CLOSE_RED, 1.5],[G.I_DLC_EXIT, 1.5], [G.I_BACK_Y, 1.5], G.I_DLC_CLOSE]
page_mall.link(button=G.I_BACK_MALL, destination=page_main)
page_theme.link(button=G.O_PAGE_MALL, destination=page_mall)
# 阴阳寮 guild
page_guild = Page(G.I_CHECK_GUILD)
# 活动弹窗常驻检测: 种花与种树两组模板全部挂上，以适配不同时期的活动弹窗。
# 限时项共享同一个 1.5s 窗口(命中一项即重置计时)，挂几组模板的日常成本都是 1.5s，
# 不随模板数量线性叠加 —— 这正是原先只能注释掉一组轮换使用的原因。
page_guild.additional = [[KekkaiUtilizeAssets.I_PLANT_FLOWER_ENSURE, 1.5],
                         [KekkaiUtilizeAssets.I_PLANT_FLOWER_ENSURE2, 1.5],
                         [KekkaiUtilizeAssets.I_PLANT_TREE_CLOSE, 1.5],
                         [KekkaiUtilizeAssets.I_PLANT_TREE_CLOSE_2, 1.5]]
page_guild.link(button=G.I_BACK_Y, destination=page_main)
page_theme.link(button=G.O_PAGE_GUILD, destination=page_guild)
# 寮结界 realm（从寮主页的「寮结界」按钮进入）
# check_button 是**列表**，两个分量分工不同：
#   I_REALM_PAGE —— 结界页的站位锚点，**随结界皮肤变**：牌匾位置与界面元素每套皮肤各一份，
#                   所以它同时是运行时皮肤探测的模板。
#   I_REALM_SHIN —— 「结界皮肤」入口图标，**不随结界皮肤变**：它是固定 UI，不是皮肤的画。
#                   KekkaiUtilize 里 back_realm / _exit_to_realm 一直拿它当根页信号。
# 后者是前者的兜底，缺不得：皮肤与配置不符时 I_REALM_PAGE 必然失效，只靠它这一页就完全
# 认不出来，goto_realm 里那条「认出来了但锚点对不上 -> 补一次皮肤探测」的钩子也就无从触发。
# 实测（2026-09-11，妖伞结界）I_REALM_PAGE 8/8 帧命中 0.958~1.000；
# 实测（2026-09-12，14 帧覆盖鬼灵咒符 / 狐梦之乡两套皮肤）I_REALM_SHIN 0.9854~0.9875 全命中。
page_guild_realm = Page([KekkaiUtilizeAssets.I_REALM_PAGE,
                         KekkaiUtilizeAssets.I_REALM_SHIN])
# 返回上一级（黄箭头）回寮主页；一键回庭院按钮直达 page_main
page_guild_realm.link(button=G.I_BACK_YOLLOW, destination=page_guild)
page_guild_realm.link(button=G.I_BACK_MAIN, destination=page_main)
# 寮主页 -> 寮结界
page_guild.link(button=KekkaiUtilizeAssets.I_GUILD_REALM, destination=page_guild_realm)
# 组队 team
page_team = Page(G.I_CHECK_TEAM)
page_team.link(button=G.I_BACK_Y, destination=page_main)
page_theme.link(button=G.O_PAGE_TEAM, destination=page_team)
# 组队房间 room (退出需要两步: additional点击返回触发确认框, link点击确认退出)
page_room = Page(GeneralInviteAssets.I_GI_EMOJI_1)
page_room.additional = [GeneralInviteAssets.I_BACK_YELLOW]
page_room.link(button=GeneralInviteAssets.I_GI_SURE, destination=page_main)
# 收集 collection
page_collection = Page(G.I_CHECK_COLLECTION)
page_collection.additional =[G.I_DLC_TICK,G.I_DLC_EXIT]
page_collection.link(button=G.I_BACK_Y, destination=page_main)
page_theme.link(button=G.O_PAGE_COLLECTION, destination=page_collection)
# 珍旅居
page_travel = Page(G.I_CHECK_TRAVEL)
page_travel.link(button=G.I_BACK_Y, destination=page_main)
page_theme.link(button=G.O_PAGE_TRAVEL, destination=page_travel)

# 道馆
from tasks.Component.GeneralBattle.assets import GeneralBattleAssets
from tasks.Dokan.assets import DokanAssets

page_dokan = Page(DokanAssets.I_RYOU_DOKAN_CHECK)
page_dokan.additional = [GeneralBattleAssets.I_EXIT, DokanAssets.I_RYOU_DOKAN_EXIT_ENSURE, G.I_BACK_BLUE]
page_dokan.link(button=G.I_BACK_Y, destination=page_main)


# ************************************* 战斗部分 *****************************************#
# 战斗界面
# page_battle = Page(GeneralBattleAssets.I_BATTLE_INFO)
#
#
def random_click(low: int = None, high: int = None, ltrb: tuple = (True, False, True, False)) -> RuleClick | list[RuleClick]:
    """
    随机生成RuleClick, 不传入参数则返回1个RuleClick, 传入参数则生成范围内的click数组
    :return: RuleClick或者RuleClick的数组
    """
    from tasks.Component.GeneralBattle.assets import GeneralBattleAssets as GBA
    click_area_list = [GBA.C_REWARD_1, GBA.C_REWARD_2, GBA.C_REWARD_3]
    click = random.choice(list(compress(click_area_list, ltrb)))
    click.name = "SAFE_RANDOM_CLICK"
    if low is None or high is None:
        return click
    return [click for _ in range(random.randint(low, high))]
#
#
# # 奖励界面
# page_reward = Page(check_button=[GeneralBattleAssets.I_REWARD_PURPLE_SNAKE_SKIN, GeneralBattleAssets.I_REWARD,
#                                  GeneralBattleAssets.I_REWARD_EXP_SOUL_4, GeneralBattleAssets.I_WIN,
#                                  GeneralBattleAssets.I_REWARD_GOLD, GeneralBattleAssets.I_REWARD_GOLD_SNAKE_SKIN,
#                                  GeneralBattleAssets.I_REWARD_SOUL_5, GeneralBattleAssets.I_REWARD_SOUL_6,
#                                  GGA.I_UI_REWARD, ])
# page_reward.additional = [random_click()]
# # 失败界面
# page_failed = Page(GeneralBattleAssets.I_FALSE)
# page_failed.additional = [random_click()]
