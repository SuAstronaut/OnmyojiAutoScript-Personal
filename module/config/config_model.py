# This Python file uses the following encoding: utf-8
# @author runhey
# github https://github.com/runhey
from tasks.GuildActivityMonitor.config import GuildActivityMonitor
from typing import Dict, Any

import re
import inflection

from pydantic import Field

from module.config.utils import *
from module.logger import logger

# 导入配置的Python文件
from tasks.Component.config_base import ConfigBase
from tasks.Exploration.config import Exploration
from tasks.RyouToppa.config import RyouToppa
from tasks.Dokan.config import Dokan
from tasks.Script.config import Script
from tasks.Restart.config import Restart
from tasks.GlobalGame.config import GlobalGame
# 每日任务-----------------------------------------------------------------------------------------------------
from tasks.AreaBoss.config import AreaBoss
from tasks.ExperienceYoukai.config import ExperienceYoukai
from tasks.GoldYoukai.config import GoldYoukai
from tasks.Nian.config import Nian
from tasks.KekkaiUtilize.config import KekkaiUtilize
from tasks.KekkaiActivation.config import KekkaiActivation
from tasks.DemonEncounter.config import DemonEncounter
from tasks.DailyTrifles.config import DailyTrifles
from tasks.TalismanPass.config import TalismanPass
from tasks.Pets.config import Pets
from tasks.SoulsTidy.config import SoulsTidy
from tasks.Delegation.config import Delegation
from tasks.WantedQuests.config import WantedQuests
from tasks.Tako.config import Tako
from tasks.AutoCheckinBigGod.config import AutoCheckinBigGod
from tasks.DailyAltAcc.config import DailyAltAcc
from tasks.ActivitySignIn.config import ActivitySignIn
from tasks.MasterDisciple.config import MasterDisciple
# ----------------------------------------------------------------------------------------------------------------------
from tasks.Orochi.config import Orochi
from tasks.OrochiMoans.config import OrochiMoans
from tasks.Sougenbi.config import Sougenbi
from tasks.FallenSun.config import FallenSun
from tasks.EternitySea.config import EternitySea
from tasks.SixRealms.config import SixRealms
from tasks.RealmRaid.config import RealmRaid
from tasks.CollectiveMissions.config import CollectiveMissions
from tasks.Hunt.config import Hunt
from tasks.AbyssShadows.config import AbyssShadows
from tasks.GuildBanquet.config import GuildBanquet
from tasks.DemonRetreat.config import DemonRetreat
from tasks.GuildActivityMonitor.config import GuildActivityMonitor

# 这一部分是活动的配置-----------------------------------------------------------------------------------------------------
from tasks.ActivityShikigami.config import ActivityShikigami
from tasks.MetaDemon.config import MetaDemon
from tasks.FrogBoss.config import FrogBoss
from tasks.FloatParade.config import FloatParade
from tasks.Quiz.config import Quiz
from tasks.KittyShop.config import KittyShop
from tasks.DyeTrials.config import DyeTrials
# ----------------------------------------------------------------------------------------------------------------------

# 肝帝专属---------------------------------------------------------------------------------------------------------------
from tasks.BondlingFairyland.config import BondlingFairyland
from tasks.EvoZone.config import EvoZone
from tasks.GoryouRealm.config import GoryouRealm
from tasks.Hyakkiyakou.config import Hyakkiyakou
from tasks.HeroTest.config import HeroTest
from tasks.FindJade.config import FindJade
from tasks.MemoryScrolls.config import MemoryScrolls
from tasks.MultiDailyAltAcc.config import MultiDailyAltAcc
from tasks.MultiTasks.config import MultiTasks
from tasks.AccountExport.config import AccountExport  # 独立账号数据导出。
from tasks.Utils.optional_tasks import extend
from tasks.ReturnGift.config import ReturnGift
from tasks.Plotline.config import Plotline
from tasks.SearchId.config import SearchId
# ----------------------------------------------------------------------------------------------------------------------

# 每周任务---------------------------------------------------------------------------------------------------------------
from tasks.TrueOrochi.config import TrueOrochi
from tasks.RichMan.config import RichMan
from tasks.Secret.config import Secret
from tasks.WeeklyTrifles.config import WeeklyTrifles
from tasks.MysteryShop.config import MysteryShop
from tasks.Duel.config import Duel
# ----------------------------------------------------------------------------------------------------------------------

class ConfigModel(ConfigBase):
    config_name: str = "oas"
    running_task: str = ''
    script: Script = Field(default_factory=Script)
    restart: Restart = Field(default_factory=Restart)
    global_game: GlobalGame = Field(default_factory=GlobalGame)

    # 这些是每日任务的
    area_boss: AreaBoss = Field(default_factory=AreaBoss)
    experience_youkai: ExperienceYoukai = Field(default_factory=ExperienceYoukai)
    gold_youkai: GoldYoukai = Field(default_factory=GoldYoukai)
    nian: Nian = Field(default_factory=Nian)
    realm_raid: RealmRaid = Field(default_factory=RealmRaid)
    ryou_toppa: RyouToppa = Field(default_factory=RyouToppa)
    kekkai_utilize: KekkaiUtilize = Field(default_factory=KekkaiUtilize)
    kekkai_activation: KekkaiActivation = Field(default_factory=KekkaiActivation)
    demon_encounter: DemonEncounter = Field(default_factory=DemonEncounter)
    daily_trifles: DailyTrifles = Field(default_factory=DailyTrifles)
    talisman_pass: TalismanPass = Field(default_factory=TalismanPass)
    pets: Pets = Field(default_factory=Pets)
    souls_tidy: SoulsTidy = Field(default_factory=SoulsTidy)
    delegation: Delegation = Field(default_factory=Delegation)
    exploration: Exploration = Field(default_factory=Exploration)
    wanted_quests: WantedQuests = Field(default_factory=WantedQuests)
    tako: Tako = Field(default_factory=Tako)
    auto_checkin_big_god: AutoCheckinBigGod = Field(default_factory=AutoCheckinBigGod)
    daily_alt_acc: DailyAltAcc = Field(default_factory=DailyAltAcc)
    activity_sign_in: ActivitySignIn = Field(default_factory=ActivitySignIn)
    master_disciple: MasterDisciple = Field(default_factory=MasterDisciple)

    # 这些是刷御魂的
    orochi: Orochi = Field(default_factory=Orochi)
    orochi_moans: OrochiMoans = Field(default_factory=OrochiMoans)
    sougenbi: Sougenbi = Field(default_factory=Sougenbi)
    fallen_sun: FallenSun = Field(default_factory=FallenSun)
    eternity_sea: EternitySea = Field(default_factory=EternitySea)
    six_realms: SixRealms = Field(default_factory=SixRealms)

    # 这些是活动的
    activity_shikigami: ActivityShikigami = Field(default_factory=ActivityShikigami)
    meta_demon: MetaDemon = Field(default_factory=MetaDemon)
    frog_boss: FrogBoss = Field(default_factory=FrogBoss)
    float_parade: FloatParade = Field(default_factory=FloatParade)
    quiz: Quiz = Field(default_factory=Quiz)
    kitty_shop: KittyShop = Field(default_factory=KittyShop)
    dye_trials: DyeTrials = Field(default_factory=DyeTrials)

    # 这些是肝帝专属
    bondling_fairyland: BondlingFairyland = Field(default_factory=BondlingFairyland)
    evo_zone: EvoZone = Field(default_factory=EvoZone)
    goryou_realm: GoryouRealm = Field(default_factory=GoryouRealm)
    hyakkiyakou: Hyakkiyakou = Field(default_factory=Hyakkiyakou)
    hero_test: HeroTest = Field(default_factory=HeroTest)
    find_jade: FindJade = Field(default_factory=FindJade)
    memory_scrolls: MemoryScrolls = Field(default_factory=MemoryScrolls)
    multi_daily_alt_acc: MultiDailyAltAcc = Field(default_factory=MultiDailyAltAcc)
    multi_tasks: MultiTasks = Field(default_factory=MultiTasks)
    account_export: AccountExport = Field(default_factory=AccountExport)  # 默认关闭，由用户启用。
    plotline: Plotline = Field(default_factory=Plotline)
    search_id: SearchId = Field(default_factory=SearchId)
    return_gift: ReturnGift = Field(default_factory=ReturnGift)
    # 这些是每周任务
    true_orochi: TrueOrochi = Field(default_factory=TrueOrochi)
    rich_man: RichMan = Field(default_factory=RichMan)
    secret: Secret = Field(default_factory=Secret)
    weekly_trifles: WeeklyTrifles = Field(default_factory=WeeklyTrifles)
    mystery_shop: MysteryShop = Field(default_factory=MysteryShop)
    duel: Duel = Field(default_factory=Duel)

    # 阴阳寮
    collective_missions: CollectiveMissions = Field(default_factory=CollectiveMissions)
    hunt: Hunt = Field(default_factory=Hunt)
    dokan: Dokan = Field(default_factory=Dokan)
    abyss_shadows: AbyssShadows = Field(default_factory=AbyssShadows)
    guild_banquet: GuildBanquet = Field(default_factory=GuildBanquet)
    demon_retreat: DemonRetreat = Field(default_factory=DemonRetreat)
    guild_activity_monitor: GuildActivityMonitor = Field(default_factory=GuildActivityMonitor)

    # 注意：ConfigModel 只保留纯 Schema 与 UI/读取 helper。文件型 __init__、自动保存
    # __setattr__、read_json/write_json/save/script_set_arg/copy/reset 等 I/O 与业务写操作
    # 已在 Task 3 全部移入 ConfigStore；任何磁盘读写必须经 ConfigStore/公共 locked API。

    def gui_args(self, task: str) -> str:
        """
        返回提供给gui显示的参数
        :param task: 输入的是任务的名称英文 如'Script' 或者是'script'都是可以的
        :return: 返回的是pydantic给我们结构化的输出的信息, 如果不能获取就返回空的str
        """
        task = convert_to_underscore(task)
        task_gui = getattr(self, task, None)
        if task_gui is None:
            logger.warning(f'{task} is no inexistence')
            return ''

        schema2 = task_gui.schema()
        # https://github.com/pydantic/pydantic/discussions/5687
        if 'definitions' in schema2:
            if 'Scheduler' in schema2['definitions']:
                if 'properties' in schema2['definitions']['Scheduler']:
                    properties = schema2['definitions']['Scheduler']['properties']
                    if 'success_interval' in properties:
                        properties['success_interval']['type'] = 'string'
                    if 'failure_interval' in properties:
                        properties['failure_interval']['type'] = 'string'
        return json.dumps(schema2)

    def gui_task(self, task: str) -> str:
        """
        返回提供给gui显示的参数
        :param task:
        :return:
        """
        task_name = convert_to_underscore(task)
        task = getattr(self, task_name, None)
        if task is None:
            logger.warning(f'{task_name} is no inexistence')
            return ''
        return task.json()

    @staticmethod
    def type(key: str) -> str:
        """
        输入模型的键值，获取这个字段对象的类型 比如输入是orochi输出是Orochi
        :param key:
        :return:
        """
        # 扩展模型通过继承追加字段；Pydantic 字段表同时包含公共字段和扩展字段。
        field_type: str = str(ConfigModel.model_fields[key].annotation)
        # return field_type
        if '.' in field_type:
            classname = field_type.split('.')[-1][:-2]
            return classname
        else:
            classname = re.findall(r"'([^']*)'", field_type)[0]
            return classname

    @staticmethod
    def deep_get(obj, keys: str, default=None):
        """
        递归获取模型的值
        :param obj:
        :param keys:
        :param default:
        :return:
        """
        if not isinstance(keys, list):
            keys = keys.split('.')
        value = obj
        try:
            for key in keys:
                value = getattr(value, key)
        except AttributeError:
            return default
        return value

    @staticmethod
    def deep_set(obj, keys: str, value) -> bool:
        if not isinstance(keys, list):
            keys = keys.split('.')
        current_obj = obj
        try:
            for key in keys[:-1]:
                current_obj = getattr(current_obj, key)
            setattr(current_obj, keys[-1], value)
            return True
        except (AttributeError, KeyError):
            return False

    # ----------------------------------- fastapi -----------------------------------
    def script_task(self, task: str) -> dict:
        """

        :param task: 同gui_args函数
        :return:
        """
        task_name = convert_to_underscore(task)
        task = getattr(self, task_name, None)
        if task is None:
            logger.warning(f'{task_name} is no inexistence')
            return {}

        def extract_groups(sch):
            # 从schema 中提取未解析的group的数据
            # properties = properties_groups(sch)
            results = {}
            properties = {}
            for key, value in sch["properties"].items():
                if 'items' in value:
                    properties[key] = re.search(r"/([^/]+)$", value['items']['$ref']).group(1)
                else:
                    properties[key] = re.search(r"/([^/]+)$", value['$ref']).group(1)

            for key, value in properties.items():
                results[key] = sch["$defs"][value]
            return results

        def merge_value(groups, jsons, definitions) -> list[dict]:
            # 将 groups的参数，同导出的json一起合并, 用于前端显示
            result = []
            for key, value in groups["properties"].items():
                # deal with exclude 
                if key in jsons and jsons[key] == 0xABCDEF:
                    continue

                item = {}
                item["name"] = key
                item["title"] = value["title"] if "title" in value else inflection.underscore(key)
                if "description" in value:
                    item["description"] = value["description"]
                item["default"] = value["default"]
                item["value"] = jsons[key] if key in jsons else value["default"]
                item["type"] = value["type"] if "type" in value else "enum"
                if '$ref' in value:  # list
                    enum_key = re.search(r"/([^/]+)$", value['$ref']).group(1)
                    item["enumEnum"] = definitions[enum_key]["enum"]
                # if 'allOf' in value:
                #     enum_key = re.search(r"/([^/]+)$", value['allOf'][0]['$ref']).group(1)
                #     item["enumEnum"] = definitions[enum_key]["enum"]
                result.append(item)
            return result

        schema = task.model_json_schema()
        groups = extract_groups(schema)
        groups_value = groups.copy()

        result: dict[str, list] = {}
        for key, value in task.model_dump(context={'hide': True}).items():
            if key not in groups:
                for group_name in groups.keys():
                    if group_name in key:
                        groups_value[key] = groups[group_name]
            result[key] = merge_value(groups_value[key], value, schema["$defs"])

        self._inject_desktop_handle_options(result)
        self._filter_orochi_team_fields(task_name, result)
        self._filter_activity_shikigami_fields(task_name, result)
        self._filter_master_disciple_fields(task_name, result)
        return result

    def _filter_activity_shikigami_fields(self, task_name: str, result: dict) -> None:
        """爬塔任务当前不存在的玩法模式：整组从界面摘掉。

        纯展示层过滤：只从返回给界面的字典里摘掉条目，不动 pydantic 模型也不改
        任何落盘值。修行合训（season_boss）当前活动无入口，配置组整体隐藏；
        ap20/大富翁的字段级隐藏走 GeneralClimb/SwitchSoulConfig/GeneralBattleConfig
        上的 dynamic_hide（隐藏字段不会出现在 result 里）。活动切换回来后删掉本
        调用与各组 hide_fields 即可恢复显示。
        """
        if task_name != 'activity_shikigami':
            return
        result.pop('season_boss', None)

    def _filter_orochi_team_fields(self, task_name: str, result: dict) -> None:
        """按御魂组队模式与身份裁剪界面字段，只留下该模式真正需要填的项。

        纯展示层过滤：只从返回给界面的字典里摘掉条目，不动 pydantic 模型也不改
        任何落盘值。切模式后重新打开设置页即按新模式显示，隐藏字段的值原样保留。

        隐藏规则：
          - 单人：组队组只留模式下拉；副本设置去掉身份（身份只服务于组队配对）
          - 组队队员：层数、单轮限制、总量、邀请配置全部由队长决定或不参与流程
          - 组队队长：leader_instance 是队员用来指认队长的，队长自己不需要填
        """
        if task_name != 'orochi':
            return
        team_items = result.get('team_config')
        if not team_items:
            return

        def drop(group: str, names: set) -> None:
            items = result.get(group)
            if not items:
                return
            kept = [item for item in items if item.get('name') not in names]
            # 一组字段被摘空时连分组一起去掉，避免界面上留一个空的设置框
            if kept:
                result[group] = kept
            else:
                result.pop(group, None)

        team_mode = getattr(self.orochi.team_config, 'team_mode', None)
        team_mode = getattr(team_mode, 'value', team_mode)
        user_status = getattr(self.orochi.orochi_config, 'user_status', None)
        user_status = getattr(user_status, 'value', user_status)

        if team_mode != 'team':
            # 单人：组队组只保留模式下拉，身份也不再参与流程
            result['team_config'] = [
                item for item in team_items if item.get('name') == 'team_mode'
            ]
            drop('orochi_config', {'user_status'})
            return

        if user_status == 'member':
            # 队员：总量与单轮限制均由队长发布并同步过来，自己填了也不生效
            drop('team_config', {'total_limit_count', 'total_limit_time'})
            # 实际层数由队长选定，队员不进副本选层，因此层数也不需要填
            drop('orochi_config', {'limit_count', 'limit_time', 'layer'})
            # 队员不执行邀请动作；等待时间在组队流程里由长等待逻辑接管，无需再填
            drop('invite_config', {'invite_number', 'friend_1', 'friend_2',
                                   'find_mode', 'default_invite', 'wait_time'})
        else:
            # 队长：由自己发布场次，不需要指认别的实例作队长
            drop('team_config', {'leader_instance'})

    def _filter_master_disciple_fields(self, task_name: str, result: dict) -> None:
        """按师徒模式裁剪界面字段，只显示当前模式真正需要配置的项。

        纯展示层过滤：只从返回给界面的字典里摘掉条目，不动 pydantic 模型
        也不改任何落盘值。切模式后重新打开设置页即按新模式显示，隐藏字段
        的值原样保留。

        战斗行为指令（金币/经验是否准备后退出）已改由徒弟经同步协议下发，
        师父侧不再读本地开关，因此师父模式下退出开关也不显示。
        """
        if task_name != 'master_disciple':
            return
        items = result.get('master_disciple_config')
        if not items:
            return

        def drop(group: str, names: set) -> None:
            group_items = result.get(group)
            if not group_items:
                return
            kept = [item for item in group_items if item.get('name') not in names]
            # 一组字段被摘空时连分组一起去掉，避免界面上留一个空的设置框
            if kept:
                result[group] = kept
            else:
                result.pop(group, None)

        # mode 取枚举原始值（与御魂组队过滤同款写法）
        mode = getattr(self.master_disciple.master_disciple_config, 'mode', None)
        mode = getattr(mode, 'value', mode)

        if mode == 'master':
            # 师父模式：只留模式选择与御魂预设，徒弟专用字段全部隐藏
            drop('master_disciple_config', {
                'master_name', 'master_instance', 'limit_time', 'limit_count',
                'run_exploration', 'run_exp_monster', 'run_stone_ju',
                'run_coin_monster', 'run_guard', 'guard_battle_count',
                'auto_switch_account', 'cycle_all_disciples', 'invite_timeout',
                'master_coin_exit_after_prepare', 'master_exp_exit_after_prepare',
                'buy_ap_when_low', 'ap_threshold'})
            # 徒弟账号列表组（序列化后为 disciple_account_list_1..N）
            for key in [k for k in result if k.startswith('disciple_account_list')]:
                result.pop(key, None)
        else:
            # 徒弟模式：御魂预设只服务于师父模式；退出开关归徒弟配置并经协议下发
            result.pop('master_preset_1', None)
            result.pop('master_preset_2', None)
            result.pop('master_preset_3', None)
            # 轮询模式是纯单人流程（只跑探索/领体力，房间任务开关失效）：
            # 房间任务与同步相关字段一并隐藏，界面只剩单人环节配置
            md_config = self.master_disciple.master_disciple_config
            cycle_mode = bool(getattr(md_config, 'auto_switch_account', False)
                              and getattr(md_config, 'cycle_all_disciples', False))
            if cycle_mode:
                drop('master_disciple_config', {
                    'master_name', 'master_instance', 'run_guard', 'run_stone_ju',
                    'run_coin_monster', 'run_exp_monster', 'guard_battle_count',
                    'invite_timeout', 'master_coin_exit_after_prepare',
                    'master_exp_exit_after_prepare'})

    def _inject_desktop_handle_options(self, result: dict) -> None:
        """桌面模式下把 handle 就地改成"已开客户端窗口"下拉，供界面选择而非手填 PID。

        仅当 script.device.serial == 'desktop' 时注入：handle 是桌面与模拟器共用
        字段，模拟器模式下需要保留 auto/窗口标题/HWND 的自由文本输入，因此那边的
        schema 输出保持原样不动。
        字段类型仍是 str，只是在返回给界面的字典里补上候选项，pydantic 校验不受影响。
        """
        device_items = result.get('device')
        if not device_items:
            return
        if getattr(self.script.device, 'serial', '') != 'desktop':
            return
        try:
            from module.device.handle import desktop_window_option, list_desktop_windows
            windows = list_desktop_windows()
        except Exception as e:
            # 枚举依赖 win32 且只服务于界面展示，失败时退回手填输入框，不能拖垮设置页
            logger.warning(f'list desktop windows failed, handle stays a text input: {e}')
            return

        options = [desktop_window_option(w) for w in windows]
        current = ''
        for item in device_items:
            if item['name'] == 'handle':
                current = str(item.get('value') or '')
                break
        # 落盘的 handle 是纯 PID，而候选项是带标题坐标的展示串。界面靠"值等于某个
        # 候选项"来选中，所以要把当前值换成同款展示串，否则下拉会显示为空白
        display = ''
        for window, option in zip(windows, options):
            if str(window['pid']) == current:
                display = option
                break
        # 已绑定的 PID 此刻不在枚举结果里（客户端未启动，或重启后换了 PID）：
        # 仍把原值作为候选保留，让界面显示出"配置里存的是谁"而不是静默变空
        if current and not display:
            display = current
            options.append(current)
        # 空串候选用于"解除绑定"，同时保证 handle 为空时下拉有项可选中
        options.insert(0, '')

        for item in device_items:
            if item['name'] != 'handle':
                continue
            item['type'] = 'enum'
            item['enumEnum'] = options
            item['value'] = display
            break


# 本机扩展在模型定义完成后追加字段，继承既有 Schema 与读取方法。
ConfigModel = extend('config_model', ConfigModel)


if __name__ == "__main__":
    c = ConfigModel()
    print(c.script_task('GuildBanquet'))
