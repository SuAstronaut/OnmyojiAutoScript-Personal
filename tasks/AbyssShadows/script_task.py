# This Python file uses the following encoding: utf-8
# @brief    AbyssShadows(阴阳竂狭间暗域功能)
# @author   jackyhwei
# @note     draft version without full test
# github    https://github.com/roarhill/oas
import time
from time import sleep

from datetime import datetime

from future.backports.datetime import timedelta
from module.exception import TaskEnd, RequestHumanTakeover
from module.base.timer import Timer
from module.logger import logger
from module.config.config import Config
from module.device.device import Device
from tasks.AbyssShadows.assets import AbyssShadowsAssets
from tasks.AbyssShadows.config import AbyssShadows, EnemyType, AreaType, Code, AbyssShadowsDifficulty, \
    CodeList, IndexMap
from tasks.Component.GeneralBattle.general_battle import GeneralBattle
from tasks.Component.GeneralBattle.reward_frame import weighted_choice
from tasks.Component.SwitchAccount.switch_account import SwitchAccountOnStart
from tasks.Component.SwitchSoul.switch_soul import SwitchSoul
from tasks.GameUi.game_ui import GameUi
from tasks.GameUi.page import page_main, page_guild

# 单个首领/副将/精英 一次无法完成目标（一般是一次没打掉） 的情况下，最大战斗次数
MAX_BATTLE_COUNT = 2

# 小蛇单轮走位的最长时间（秒）：超时视为本轮走位失败，重来整套定位流程
SNAKE_LOCATE_TIMEOUT = 40
# 小蛇定位「整套流程」（开导航→点6号怪→点前往→走位）的最大轮次
MAX_SNAKE_LOCATE_ROUND = 2


class AbyssShadowsFinished(Exception):
    pass


class ScriptTask(GeneralBattle, GameUi, SwitchSoul, SwitchAccountOnStart, AbyssShadowsAssets):
    #
    min_count = {
        EnemyType.BOSS: 2,  # 最少首领战斗次数
        EnemyType.GENERAL: 4,  # 最少副将战斗次数
        EnemyType.ELITE: 6  # 最少精英战斗次数
    }

    def __init__(self, config: Config, device: Device):
        super().__init__(config, device)
        # 当前所用队伍预设
        self.cur_preset = None
        # 当前所用队伍对应的敌人类型（用于判断类型变化时才切换队伍）
        self.cur_enemy_type = None
        # 当前已切换的御魂预设字符串（如 '6,3'），用于御魂懒切换：与本场预设相同则跳过
        self.cur_soul_preset = None
        # process list：attack_order 展开的普通怪序列
        self.ps_list: CodeList = CodeList('')
        # 进度游标：线性序列中最后完成的项（'SNAKE-<k>' 或 '<code>'），空表示尚未开始
        self.progress_cursor: str = ''
        # 补全奖励用的真实战斗计数（仅真打+1，跳过不计）
        self.done_boss: int = 0
        self.done_general: int = 0
        self.done_elite: int = 0

    def run(self):
        """ 狭间暗域主函数

        :return:
        """
        cfg: AbyssShadows = self.config.abyss_shadows

        today = datetime.now().weekday()
        if today not in [4, 5, 6]:
            # 非周五六日，直接退出
            logger.info(f"Today is not abyss shadows day, exit")
            self.set_next_run(task='AbyssShadows', finish=False, server=True, success=True)
            raise TaskEnd('AbyssShadows')

        # 任务开始前切号：启用时切换到目标账号，失败则中止任务稍后重试
        if not self.switch_account_on_start(cfg.switch_account_config, cfg.switch_account_list):
            self.set_next_run(task='AbyssShadows', finish=False, server=True, success=False)
            raise TaskEnd('AbyssShadows')

        # 进入狭间
        self.goto_abyss_shadows()

        # 尝试开启狭间
        if cfg.abyss_shadows_time.try_start_abyss_shadows:
            self.start_abyss_shadows()

        try:
            self.init_list_from_cfg()

            # 根据游标决定首个要进入的区域（用于检测狭间是否开启）
            area_enter = self.get_first_area_to_enter()
            if area_enter is None:
                raise AbyssShadowsFinished

            # 通过能否进入，检测狭间是否开启
            if not self.select_boss(area_enter):
                logger.warning("Failed to enter abyss shadows")
                self.goto_main()
                self.set_next_run(task='AbyssShadows', finish=False, server=False, success=False)
                raise TaskEnd('AbyssShadows')

            # 集结中图片
            self.wait_until_appear(self.I_WAIT_TO_START, wait_time=2)

            # 检查活动是否结束
            if self.appear(self.I_CHECK_FINISH):
                logger.info(f"{self.I_CHECK_FINISH} appear,abyss shadows finished")
                raise AbyssShadowsFinished

            # 集结期（还没到开战）先把小蛇的御魂换好并走位到小蛇跟前待命，
            # 把这段本来干等的时间用掉；预备失败则返回 False，开战后走原完整流程兜底。
            # 预备期间有换御魂、走位等长时间少点击的动作，先放宽卡死检测阈值（60s -> 300s），
            # 否则 goto_snake 的 sleep(3) 空档累计超过 60 秒就会被误判为 GameStuckError
            self.device.stuck_record_add('PREPARE_BEFORE_BATTLE')
            try:
                snake_prepared = self.prepare_snake_before_start()
            finally:
                self.device.stuck_record_clear()

            #
            self.device.stuck_record_add('BATTLE_STATUS_S')
            # 等待战斗开始（顶部状态条从「集结中」变为「进攻中」，站在小蛇跟前也能识别到）
            # 集结期约 180 秒；预备已消耗掉大部分，这里等的是剩余时间
            self.wait_until_appear(self.I_IS_ATTACK, wait_time=180)
            self.device.stuck_record_clear()
            #
            # 小蛇战斗永远在最开始执行，与普通怪物的攻打顺序无关
            # 单独捕获 AbyssShadowsFinished：小蛇阶段结束不应跳过后续普通怪流程；
            # 其它框架异常（如 GameStuckError/TaskEnd）继续向上抛，交由调度器处理
            try:
                self.run_snake_battles(prepared=snake_prepared)
            except AbyssShadowsFinished:
                logger.info("Snake battle stage finished with AbyssShadowsFinished")
            #
            self.process()
        except AbyssShadowsFinished:
            logger.info("Abyss shadows finished with Exception AbyssShadowsFinished")
            pass
        logger.info("Abyss shadows process done")

        # 保持好习惯，一个任务结束了就返回到庭院，方便下一任务的开始
        self.goto_main()

        # 设置下次运行时间
        self.set_next_run(task='AbyssShadows', finish=True, server=True, success=True)

        self.clear_saved_params()

        raise TaskEnd('AbyssShadows')

    def init_list_from_cfg(self):
        if datetime.today().strftime('%Y-%m-%d') != self.config.model.abyss_shadows.saved_params.save_date:
            logger.info("Today is not saved date, clear saved params")
            self.clear_saved_params()
        #
        self.ps_list = CodeList(self.config.model.abyss_shadows.process_manage.attack_order)
        # 从缓存读取游标与补全计数
        sp = self.config.model.abyss_shadows.saved_params
        self.progress_cursor = sp.progress_cursor
        self.done_boss = sp.done_boss
        self.done_general = sp.done_general
        self.done_elite = sp.done_elite
        logger.info(f"init from cfg done! cursor={self.progress_cursor!r} "
                    f"boss={self.done_boss} general={self.done_general} elite={self.done_elite}")

    def clear_saved_params(self):
        sp = self.config.model.abyss_shadows.saved_params
        sp.progress_cursor = ''
        sp.done_boss = 0
        sp.done_general = 0
        sp.done_elite = 0
        self.config.save()
        logger.info("Clear saved params done")

    def build_linear_sequence(self) -> list:
        """ 构造整轮战斗的线性序列

        序列 = 小蛇段（若启用，N 个 'SNAKE' 标记）+ attack_order 展开的普通怪 Code 序列。
        小蛇用字符串 'SNAKE' 占位，普通怪用 Code 对象。
        :return list，元素为 'SNAKE'(str) 或 Code
        """
        pm = self.config.model.abyss_shadows.process_manage
        seq = []
        if pm.enable_snake:
            seq.extend(['SNAKE'] * pm.snake_battle_count)
        seq.extend(list(CodeList(pm.attack_order)))
        return seq

    def get_resume_index(self, seq: list) -> int:
        """ 根据游标定位续跑起点在 seq 中的下标

        游标语义：游标是序列里“最后完成的项”，返回其下一项的下标。
        - 空游标 -> 0（从头开始）
        - 'SNAKE-<k>' -> 已完成 k 次小蛇，返回下标 k
        - '<code>'（如 'A-5'）-> 返回该 code 在序列中的下标 +1；找不到则视为普通怪已全部完成，返回 len(seq)
        :return int 续跑起点下标
        """
        cursor = self.progress_cursor
        if not cursor:
            return 0
        if cursor.startswith('SNAKE-'):
            try:
                k = int(cursor.split('-', 1)[1])
            except ValueError:
                return 0
            # 已完成 k 次小蛇，下一项就是下标 k
            return min(k, len(seq))
        # 普通怪游标：在序列里找到该 code，返回其后一位
        for i, item in enumerate(seq):
            # 注意 Code 是 str 子类，不能用 isinstance(item, str) 判别；SNAKE 标记是字面量 'SNAKE'
            if item == 'SNAKE':
                continue
            if item.value == cursor:
                return i + 1
        # 游标对应的怪不在当前序列（如 attack_order 被改小/改动过）。
        # 宁可从头重跑也不漏打：已打死的怪 execute 会因 goto 失败而跳过，代价可控。
        logger.warning(f"cursor {cursor!r} not found in sequence, restart from 0 to avoid missing enemies")
        return 0

    def advance_cursor(self, item, real_battle: bool):
        """ 前进游标并落盘；真打的普通怪累加对应补全计数

        :param item: 'SNAKE'(此时需传 SNAKE 完成次数) 或 Code
        :param real_battle: 是否真实进行了战斗（跳过的怪为 False，不计入补全计数）
        """
        sp = self.config.model.abyss_shadows.saved_params
        sp.save_date = datetime.today().strftime('%Y-%m-%d')
        sp.progress_cursor = self.progress_cursor
        # 真打的普通怪累加补全计数（Code 是 str 子类，用 != 'SNAKE' 判别普通怪）
        if real_battle and item != 'SNAKE':
            enemy_type = item.get_enemy_type()
            if enemy_type == EnemyType.BOSS:
                self.done_boss += 1
            elif enemy_type == EnemyType.GENERAL:
                self.done_general += 1
            elif enemy_type == EnemyType.ELITE:
                self.done_elite += 1
        sp.done_boss = self.done_boss
        sp.done_general = self.done_general
        sp.done_elite = self.done_elite
        self.config.save()
        logger.info(f"Advance cursor to {self.progress_cursor!r} "
                    f"(real={real_battle}) boss={self.done_boss} general={self.done_general} elite={self.done_elite}")

    def get_first_area_to_enter(self):
        """ 根据游标决定首个要进入的区域（用于进入狭间时检测是否开启）

        取线性序列续跑位置起第一个普通怪的区域；若续跑位置仍在 SNAKE 段或序列无普通怪，
        回退到 attack_order 的第一个区域。
        :return AreaType 或 None（序列为空且无 attack_order）
        """
        seq = self.build_linear_sequence()
        idx = self.get_resume_index(seq)
        for i in range(idx, len(seq)):
            item = seq[i]
            # Code 是 str 子类，用 != 'SNAKE' 判别普通怪
            if item != 'SNAKE':
                return item.get_areatype()
        # 续跑位置之后没有普通怪，回退到 attack_order 首个区域
        for ps in CodeList(self.config.model.abyss_shadows.process_manage.attack_order):
            return ps.get_areatype()
        return None

    def check_current_area(self) -> AreaType:
        """ 获取当前区域
        :return AreaType
        """
        logger.info("Checking current area")
        while 1:
            self.screenshot()
            # 关闭战报界面
            if self.appear(self.I_ABYSS_MAP_EXIT):
                self.click(self.I_ABYSS_MAP_EXIT, interval=2)
                continue
            if self.appear(self.I_ABYSS_ENEMY_INFO_EXIT):
                self.click(self.I_ABYSS_ENEMY_INFO_EXIT, interval=2)
                continue
            if not self.appear(self.I_ABYSS_NAVIGATION):
                # 确定不在战报界面后依旧没有在某一区域，则返回None
                return None
            if self.appear(self.I_PEACOCK_AREA):
                return AreaType.PEACOCK
            elif self.appear(self.I_DRAGON_AREA):
                return AreaType.DRAGON
            elif self.appear(self.I_FOX_AREA):
                return AreaType.FOX
            elif self.appear(self.I_LEOPARD_AREA):
                return AreaType.LEOPARD

    def change_area(self, area_name: AreaType) -> bool:
        """ 切换到下个区域,不管成功与否,只要存在可用区域,就进入,不会停留在选择区域页面
        :return
        """
        # 确保进入区域,有 切换区域 按钮
        logger.info(f"Change area to {area_name}")
        while 1:
            self.screenshot()
            # 如果出现挑战完成，直接退出
            if self.appear(self.I_CHECK_FINISH):
                raise AbyssShadowsFinished

            if self.appear(self.I_ABYSS_NAVIGATION) or self.appear(self.I_CHANGE_AREA):
                break
            #
            if self.appear(self.I_ABYSS_MAP_EXIT):
                self.click(self.I_ABYSS_MAP_EXIT, interval=2)
                continue
            #
            if self.appear(self.I_ABYSS_ENEMY_INFO_EXIT):
                self.click(self.I_ABYSS_ENEMY_INFO_EXIT, interval=2)
                continue

        # 判断当前区域是否正确
        current_area = self.check_current_area()
        if current_area == area_name:
            logger.info(f"Current area is {current_area.name}, no need to change")
            return True

        # 切换到选择区域界面
        while 1:
            self.screenshot()
            # 如果出现挑战完成，直接退出
            if self.appear(self.I_CHECK_FINISH):
                raise AbyssShadowsFinished

            # 出现切换区域界面
            if self.appear(self.I_ABYSS_DRAGON_OVER) or self.appear(self.I_ABYSS_DRAGON):
                break
            # 点击切换区域按钮
            if self.appear_then_click(self.I_CHANGE_AREA, interval=4):
                logger.info(f"Click {self.I_CHANGE_AREA.name}")
                continue

        logger.info(f"enter change area page")
        # 判断区域是否可用，并进入一个区域
        available_areas, unavailable_areas = self.detect_area_status()

        if available_areas is None or available_areas == []:
            # 所有区域均不可用
            raise AbyssShadowsFinished

        success = area_name in available_areas
        if not success:
            # 目标区不可用（已封印/已打完）：直接返回 False，由调用方（execute）按游标跳过该怪；
            # 不再自动改去其它区，避免打乱 attack_order 顺序
            logger.info(f"Target area {area_name.name} unavailable, skip")
            # 但不能停在切换区域界面：该界面既没有导航按钮（下一次 change_area 的首个循环
            # 会无超时死等），顶部也不显示集结中/进攻中状态条。故先进入任一可用区回到区域
            # 导航界面，让 change_area 无论成功失败都以「已在某个区域内」的状态返回。
            self.select_boss(available_areas[0])
            return False

        self.select_boss(area_name)
        logger.info(f"Switch to {area_name.name}")

        return success

    def goto_main(self):
        """ 保持好习惯，一个任务结束了就返回庭院，方便下一任务的开始或者是出错重启
        """
        # 可能在狭间，也可能在其他界面
        timer_quit_abyss_shadows = Timer(16)
        timer_quit_abyss_shadows.start()
        while 1:
            self.screenshot()
            if timer_quit_abyss_shadows.reached():
                logger.info("timer_quit_abyss_shadows reached,")
                break

            if self.appear(self.I_ABYSS_DRAGON) or self.appear(self.I_ABYSS_DRAGON_OVER):
                # 在切换区域界面
                self.device.click(x=600, y=600)
                self.wait_until_appear(self.I_ABYSS_NAVIGATION, wait_time=2)
                continue
            if self.appear_then_click(self.I_ABYSS_MAP_EXIT, interval=2):
                self.wait_until_appear(self.I_ABYSS_NAVIGATION, wait_time=2)
                continue
            if self.appear_then_click(self.I_ABYSS_ENEMY_INFO_EXIT, interval=2):
                self.wait_until_appear(self.I_ABYSS_MAP_EXIT, wait_time=2)
                continue
            if self.appear_then_click(self.I_UI_BACK_BLUE, interval=2):
                self.wait_until_appear(self.I_ABYSS_NAVIGATION, wait_time=1)
                continue
            if self.appear_then_click(self.I_UI_BACK_YELLOW, interval=2):
                continue
            if self.appear(self.I_ABYSS_NAVIGATION, threshold=0.85) or self.appear(self.I_CHECK_FINISH, threshold=0.85):
                break
            if self.appear(self.I_CHECK_SUMMON):
                break

        #
        logger.info("Exiting abyss_shadows")
        self.ui_get_current_page()
        self.ui_goto(page_main)

    def goto_abyss_shadows(self) -> bool:
        """ 进入狭间
        :return bool
        """
        self.ui_get_current_page()
        logger.info("Entering abyss_shadows")
        self.ui_goto(page_guild)

        while 1:
            self.screenshot()
            # 进入神社
            if self.appear_then_click(self.I_RYOU_SHENSHE, interval=1):
                logger.info("Enter Shenshe")
                continue
            # 查找狭间
            if not self.appear(self.I_ABYSS_SHADOWS, threshold=0.8):
                self.swipe(self.S_TO_ABBSY_SHADOWS, interval=3)
                continue
            # 进入狭间
            if self.appear_then_click(self.I_ABYSS_SHADOWS):
                logger.info("Enter abyss_shadows")
                break
        return True

    def select_boss(self, area_name: AreaType) -> bool:
        """ 选择暗域类型
        :return
        """
        logger.info(f"Select boss: {area_name.name} start")
        click_times = 0
        while 1:
            self.screenshot()
            # 区域图片与入口图片不一致，使用点击进去
            if self.appear(self.I_ABYSS_DRAGON_OVER) or self.appear(self.I_ABYSS_DRAGON):
                is_click = False
                match area_name:
                    case AreaType.DRAGON:
                        is_click = self.click(self.C_ABYSS_DRAGON, interval=2)
                    case AreaType.PEACOCK:
                        is_click = self.click(self.C_ABYSS_PEACOCK, interval=2)
                    case AreaType.FOX:
                        is_click = self.click(self.C_ABYSS_FOX, interval=2)
                    case AreaType.LEOPARD:
                        is_click = self.click(self.C_ABYSS_LEOPARD, interval=2)
                if is_click:
                    click_times += 1
                    logger.info(f"Click {area_name.name} {click_times} times")
                if click_times >= 3:
                    logger.info(f"select boss: {area_name.name} failed")
                    return False
                continue
            if self.appear(self.I_ABYSS_NAVIGATION):
                break
        logger.info(f"select boss: {area_name.name} done")
        return True

    def goto_enemy(self, item_code: Code) -> bool:
        # 前往当前区域 的某个 敌人
        logger.info(f"Goto enemy: {item_code}")
        click_area = item_code.get_enemy_click()
        logger.info(f"Click emeny area: {click_area.name}")
        # 点击前往按钮的次数，阴阳师BUG:点击后不动，
        # 所以如果失败了，在点击前，尝试使用左下方的摇杆移动一点点
        count_click_goto_enemy = 0
        # 点击战报
        while 1:
            self.screenshot()
            if self.appear(self.I_ABYSS_FIRE):
                break
            # 尝试使用左下方摇杆移动
            if count_click_goto_enemy > 0 and self.appear(self.I_ABYSS_NAVIGATION):
                self.move_a_little()
            # 打开导航页面
            self.open_navigation()

            click_times = 0
            # 点击攻打区域,直到出现"前往"字样
            while 1:
                self.screenshot()
                # 如果点3次还没进去就表示目标已死亡,跳过
                if click_times >= 3:
                    logger.warning(f"Failed to click {click_area}")
                    return False
                # 出现前往按钮就退出
                if self.appear(self.I_ABYSS_GOTO_ENEMY):
                    logger.info(f"{self.I_ABYSS_GOTO_ENEMY} appear")
                    break
                if self.click(click_area, interval=1.5):
                    click_times += 1
                    continue
                if self.appear_then_click(self.I_ENSURE_BUTTON, interval=1):
                    continue

            # 点击前往按钮,知道该按钮消失或出现"挑战"字样

            while 1:
                self.screenshot()
                if self.appear(self.I_CHECK_FINISH):
                    raise AbyssShadowsFinished
                if self.appear(self.I_ABYSS_FIRE):
                    logger.info(f"{self.I_ABYSS_FIRE} appear")
                    break
                if self.appear(self.I_ENSURE_BUTTON):
                    self.click(self.I_ENSURE_BUTTON, interval=1)
                    continue
                if self.appear(self.I_ABYSS_GOTO_ENEMY):
                    self.click(self.I_ABYSS_GOTO_ENEMY, interval=1)
                    count_click_goto_enemy += 1
                    continue
                if not self.wait_until_appear(self.I_ABYSS_FIRE, wait_time=10):
                    break
        return True

    def attack_enemy(self):
        logger.info("Attack enemy")
        # 点击战斗按钮
        # NOTE: 以下暂时为猜测，待验证
        # 同一敌人,需要第二次攻击时,此时刚刚退出战斗,先出现大地图的帧,然后才会出现战斗按钮，故延迟几秒检测
        timer_animation = Timer(2)
        timer_animation.start()
        while 1:
            self.screenshot()
            if self.appear(self.I_CHECK_FINISH):
                raise AbyssShadowsFinished
            # 挑战敌人后，如果是奖励次数上限，会出现确认框
            if self.appear(self.I_ENSURE_BUTTON):
                self.click(self.I_ENSURE_BUTTON, interval=2)
                continue
            #
            if self.appear(self.I_ABYSS_ENEMY_FIRE):
                self.click(self.I_ABYSS_ENEMY_FIRE, interval=0.4)
                self.wait_until_appear(self.I_ABYSS_FIRE, wait_time=1)
                continue
            #
            if self.appear(self.I_ABYSS_FIRE):
                self.click(self.I_ABYSS_FIRE, interval=0.4)
                self.wait_until_appear(self.I_PREPARE_HIGHLIGHT, wait_time=2)
                continue
            #
            if self.appear(self.I_PREPARE_HIGHLIGHT):
                return True
            if not timer_animation.reached():
                continue
            if self.appear(self.I_ABYSS_NAVIGATION, threshold=0.85):
                # 已返回主界面
                logger.info("Return to main page while try to attack enemy")
                return False
            if self.appear(self.I_ABYSS_GOTO_ENEMY):
                # 为了修复问题:开始从一个怪物跑到另一个怪物时，还是可以打的，等小人到了之后，发现已经打死了
                # 就会出现这个前往按钮
                logger.info("Found goto enemy button while try to attack enemy")
                return False
        return True

    def start_abyss_shadows(self):
        # 尝试开启狭间暗域
        self.wait_until_appear(self.I_SELECT_DIFFICULTY, wait_time=2)
        if not self.appear(self.I_SELECT_DIFFICULTY):
            logger.info("Failed to Open abyss_shadows ,cause not found I_SELECT_DIFFICULTY")
            return
        if not self.appear(self.I_BTN_START):
            logger.info("Failed to Open abyss_shadows ,cause not found I_BTN_START")
            return
        # 选择难度
        self.ui_click(self.I_SELECT_DIFFICULTY, stop=self.I_DIFFICULTY_EASY, interval=2)

        difficulty_btn = None
        match self.config.model.abyss_shadows.abyss_shadows_time.difficulty:
            case AbyssShadowsDifficulty.EASY:
                difficulty_btn = self.I_DIFFICULTY_EASY
            case AbyssShadowsDifficulty.HARD:
                difficulty_btn = self.I_DIFFICULTY_HARD
            case AbyssShadowsDifficulty.NORMAL:
                difficulty_btn = self.I_DIFFICULTY_NORMAL
        self.ui_click_until_disappear(difficulty_btn, interval=2)
        # 开始
        self.ui_click(self.I_BTN_START, stop=self.I_START_ENSURE, interval=2)
        self.ui_click_until_disappear(self.I_START_ENSURE, interval=2)

    def process(self):
        # 阶段一：按线性序列 + 游标续跑，逐项攻打普通怪
        # （小蛇段已由 run_snake_battles 在本方法之前打完，游标通常已越过 SNAKE 段）
        self.init_list_from_cfg()
        seq = self.build_linear_sequence()
        idx = self.get_resume_index(seq)
        logger.info(f"process start at index {idx}/{len(seq)}")
        while idx < len(seq):
            item = seq[idx]
            # Code 是 str 子类，用 == 'SNAKE' 判别小蛇占位项
            if item == 'SNAKE':
                # SNAKE 占位项：小蛇已在 process 前处理，这里仅推进游标越过
                self.progress_cursor = f'SNAKE-{idx + 1}'
                self.advance_cursor(item, real_battle=False)
                idx += 1
                continue
            # 普通怪：真打或跳过都前进游标；real_battle 决定是否计入补全计数
            real_battle = self.execute(item)
            self.progress_cursor = item.value
            self.advance_cursor(item, real_battle=real_battle)
            idx += 1

        # 阶段二：补全奖励（可选）
        self.complete_rewards()

    def complete_rewards(self):
        """ 补全 2/4/6 奖励：主序列打完后，若开启补全且计数未满，按 A→B→C→D 找怪补打

        仅在 try_complete_enemy_count 开启时生效。补全阶段靠“goto 失败自动跳过 + 真实计数”
        驱动，不再推进游标（游标已到序列末尾）。用 tried 集合记住本轮已尝试过的节点，
        每个节点最多打一次，避免“首候选不可达就停全部补全”或“在可复战节点上反复刷”。
        """
        if not self.config.model.abyss_shadows.abyss_shadows_time.try_complete_enemy_count:
            logger.info("try_complete_enemy_count disabled, skip completion")
            return
        tried: set[str] = set()
        while True:
            target = self.get_completion_target(tried)
            if target is None:
                logger.info("Completion done or no more candidates")
                break
            # 无论真打还是不可达，都记为已尝试，下次找下一个候选
            tried.add(target.value)
            # 补全阶段的战斗，真打才累加计数
            real_battle = self.execute(target)
            if real_battle:
                enemy_type = target.get_enemy_type()
                if enemy_type == EnemyType.BOSS:
                    self.done_boss += 1
                elif enemy_type == EnemyType.GENERAL:
                    self.done_general += 1
                elif enemy_type == EnemyType.ELITE:
                    self.done_elite += 1
                self._save_completion_counts()
            else:
                # 该节点已被打完/不可达，跳过它继续找下一个候选
                logger.info(f"Completion target {target} unreachable, skip and try next")

    def _save_completion_counts(self):
        sp = self.config.model.abyss_shadows.saved_params
        sp.done_boss = self.done_boss
        sp.done_general = self.done_general
        sp.done_elite = self.done_elite
        self.config.save()

    def get_completion_target(self, tried: set = None):
        """ 补全阶段：返回下一个需要补打的怪，没有则返回 None

        用真实计数 done_boss/general/elite 判断还差哪类，
        按 A→B→C→D 固定区域顺序、区内 6 只怪的顺序找第一个匹配类型、且不在 tried 里的怪。
        :param tried: 本轮补全已尝试过的节点 value 集合，跳过它们继续找下一个
        """
        if tried is None:
            tried = set()
        need_boss = self.done_boss < self.min_count[EnemyType.BOSS]
        need_general = self.done_general < self.min_count[EnemyType.GENERAL]
        need_elite = self.done_elite < self.min_count[EnemyType.ELITE]
        logger.info(f"Completion need boss={need_boss} general={need_general} elite={need_elite}")
        if not (need_boss or need_general or need_elite):
            return None

        for area in AreaType:
            area_code = IndexMap[area.name].value  # 如 DRAGON -> 'A'
            for num in ['1', '2', '3', '4', '5', '6']:
                code = Code(f"{area_code}-{num}")
                if code.value in tried:
                    continue
                enemy_type = code.get_enemy_type()
                if enemy_type == EnemyType.BOSS and need_boss:
                    return code
                elif enemy_type == EnemyType.GENERAL and need_general:
                    return code
                elif enemy_type == EnemyType.ELITE and need_elite:
                    return code
        return None

    def open_navigation(self):
        logger.info("Open navigation")
        while True:
            self.screenshot()
            if self.appear(self.I_CHECK_FINISH):
                raise AbyssShadowsFinished
            if self.appear(self.I_ABYSS_MAP):
                break
            if self.appear(self.I_ABYSS_NAVIGATION):
                self.click(self.I_ABYSS_NAVIGATION, interval=1)
                continue
            if self.appear(self.I_ABYSS_FIRE) or self.appear(self.I_ABYSS_GOTO_ENEMY):
                self.click(self.I_ABYSS_ENEMY_INFO_EXIT, interval=2)
                continue

    def execute(self, item_code: Code) -> bool:
        """ 攻打单个普通怪。游标式下每个怪只处理一次、不重试。

        :return bool 是否真实进行了战斗（True=真打，用于补全计数；False=被跳过/已死/无法前往）
        """
        logger.info(f"Start to execute code {item_code}")
        area = item_code.get_areatype()

        if not self.change_area(area):
            # 区域不可用，视为跳过
            return False
        # 当前应当在正确的区域（区域导航界面），此时式神录入口可用
        # 按本场敌人类型的御魂预设懒切换：与上次相同则跳过，前往打怪前完成御魂切换
        # 若退出式神录时退过头（已重进狭间），需重新定位到目标区域
        if self.switch_soul_lazy(self.get_soul_preset(item_code.get_enemy_type())):
            if not self.change_area(area):
                # 重新定位时区域不可用，视为跳过
                return False

        if not self.goto_enemy(item_code):
            # 前往失败（已被打完/无法到达），视为跳过
            logger.info(f"{item_code} unreachable, skip")
            return False

        real_battle = False
        battle_count = MAX_BATTLE_COUNT
        while battle_count > 0:
            self.screenshot()

            if not self.attack_enemy():
                # 首次就没有战斗按钮 -> 该怪已死，视为跳过（不计数）
                if battle_count == MAX_BATTLE_COUNT:
                    logger.info(f"{item_code} already dead, skip")
                    return False
                # 曾经战斗过，则认为该怪已完成
                logger.info(f"{item_code} has been killed")
                break
            # 真实战斗
            suc = self.run_battle(item_code)
            real_battle = True
            self.device.stuck_record_clear()
            if suc:
                break
            battle_count -= 1
        logger.info(f"{item_code} done, real_battle={real_battle}")
        return real_battle

    def settlement_click_count(self, page_clicks: int) -> int:
        """狭间结算固定单击：不做任何追加击（2026-09-11 用户指示）。

        狭间结算画面切换快，追加击容易跨越画面切换点落到新界面上误触按钮，
        固定单击彻底消除追加击误触窗口。与 ActivityShikigami（爬塔）同一策略；
        事件衰减查表（page_clicks）对固定簇长无意义，忽略。
        """
        return 1

    def run_battle(self, item_code: Code):
        success = False
        enemy_type = item_code.get_enemy_type()

        # 判断是否需要更换预设
        def get_preset(_enemy_type: EnemyType):
            match _enemy_type:
                case EnemyType.BOSS:
                    return self.config.model.abyss_shadows.process_manage.preset_boss
                case EnemyType.GENERAL:
                    return self.config.model.abyss_shadows.process_manage.preset_general
                case EnemyType.ELITE:
                    return self.config.model.abyss_shadows.process_manage.preset_elite

        preset = get_preset(enemy_type)
        # 按敌人类型是否变化来判断：只要本场类型与上一场不同，就执行队伍切换；
        # 同一类型连续多场则不重复切换（即使各类型预设值相同，类型变化也会切换）
        if enemy_type != self.cur_enemy_type:
            logger.info(f"enemyType{enemy_type}--Switch preset to {preset} and {self.cur_enemy_type=}")
            self.switch_preset_team_with_str(preset)
            self.cur_preset = preset
            self.cur_enemy_type = enemy_type

        # 点击准备
        _timer_battle = Timer(180)
        self.wait_until_appear(self.I_PREPARE_HIGHLIGHT, wait_time=3)
        self.ui_click_until_disappear(self.I_PREPARE_HIGHLIGHT, interval=0.6)
        _timer_battle.start()

        # 生成退出条件
        # 因为条件中可能是时间相关,所以在点击准备按钮后直接生成,尽量减小误差
        condition = self.config.model.abyss_shadows.process_manage.generate_quit_condition(enemy_type)
        logger.info(f"enemyType{enemy_type}--{condition}")

        # 标记主怪
        is_need_mark_main = self.config.model.abyss_shadows.process_manage.is_need_mark_main(enemy_type)
        if is_need_mark_main:
            logger.info(f"enemyType{enemy_type}--Mark main")
            # 需要处理主怪没了的情况,增加最大次数
            count_click_mark_main = 0
            while count_click_mark_main < 5:
                if self.appear(self.I_MARK_MAIN):
                    break
                if self.click(self.C_MARK_MAIN, interval=1):
                    count_click_mark_main += 1
                    self.wait_until_appear(self.I_MARK_MAIN, wait_time=1)
                    continue

        # 绿标
        # self.green_mark(True,GreenMarkType.GREEN_LEFT1)

        _cur_damage = 0
        need_check_damage = condition.is_need_damage_value()
        self.device.screenshot_interval_set(1)
        self.device.stuck_record_add('BATTLE_STATUS_S')
        while True:
            self.screenshot()
            if need_check_damage:
                _cur_damage = self.O_DAMAGE.ocr_digit(self.device.image)
            if condition.is_valid(_cur_damage):
                logger.info(f"Condition Validated,try to quit battle")
                self.device.screenshot_interval_set()
                self.quit_battle()
                break
            if self.appear_then_click(self.I_PREPARE_HIGHLIGHT, interval=3):
                # 正常来讲，此处不应该出现准备按钮，以防万一
                self.device.stuck_record_add("BATTLE_STATUS_S")
                _timer_battle.reset()
                continue
            # 结算点击：安全落点 + 固定单击（settlement_click_count 覆盖为 1）
            action_click = weighted_choice(self.reward_click_actions())
            # 战斗胜利标志
            if self.settlement_click(self.I_WIN, action_click, interval=1):
                self.device.screenshot_interval_set()
                need_check_damage = False
                continue
            # 战斗奖励标志
            if self.settlement_click(self.I_REWARD, action_click, interval=1):
                self.device.screenshot_interval_set()
                need_check_damage = False
                continue
            if self.appear(self.I_ABYSS_NAVIGATION):
                self.device.screenshot_interval_set()
                break
        if condition.is_passed() or (not _timer_battle.reached()):
            # 通过条件结束的,视其为完成
            # 条件未通过且战斗时间不足3分钟的,极大可能是打死了,视之为完成
            logger.info(f"{enemy_type.name} battle result SUCCESS")
            success = True

        logger.info(f"{enemy_type.name} DONE")
        return success

    def prepare_snake_before_start(self) -> bool:
        """ 集结期预备小蛇：切到小蛇区域 -> 切御魂 -> 走位定位到小蛇处待命

        狭间集结期（等待 I_IS_ATTACK 出现的这段时间）原本只是干等，把御魂切换与小蛇走位
        提前放到这段时间完成，正式开战后即可直接进入战斗循环，省掉开战后的准备耗时。
        任何一步失败都只返回 False，由调用方在开战后走原来的完整前置流程兜底。

        :return bool 是否已完成小蛇预备（True=开战后可直接进入战斗循环）
        """
        pm = self.config.model.abyss_shadows.process_manage
        if not pm.enable_snake:
            logger.info("Snake battle disabled, skip snake prepare")
            return False

        done_count = self.get_snake_done_from_cursor()
        if done_count >= pm.snake_battle_count:
            logger.info(f"Snake battle already done ({done_count}/{pm.snake_battle_count}), skip snake prepare")
            return False

        logger.info("Prepare snake during gathering phase")
        # 小蛇固定在 C 区（白藏主暗域，AreaType.FOX）
        snake_area = AreaType.FOX
        if not self.change_area(snake_area):
            # C 区被封印/未开启，小蛇整段跳过（change_area 已把画面放回某个可用区的导航界面）
            logger.warning(f"Snake area {snake_area.name} unavailable, skip snake prepare")
            return False

        # 按小蛇御魂预设懒切换（此时在区域导航界面，式神录入口可用）
        # 若退出式神录时退过头（已重进狭间），需重新定位到小蛇所在区域
        if self.switch_soul_lazy(self.get_soul_preset('SNAKE')):
            if not self.change_area(snake_area):
                logger.warning(f"Snake area {snake_area.name} unavailable after re-enter, skip snake prepare")
                return False

        # 走位定位到小蛇处；集结期不限切区重置次数，直到定位成功或狭间开战为止，
        # 把这 180 秒等待时间全部用于定位，保证开战后能直接开打
        if not self.locate_snake_with_retry(snake_area, until_start=True):
            logger.warning("Failed to locate snake during gathering phase, fallback to prepare after start")
            return False

        logger.info("Snake prepared, waiting for abyss shadows to start")
        return True

    def locate_snake_with_retry(self, snake_area: AreaType, max_retry: int = 1,
                                until_start: bool = False) -> bool:
        """ 定位小蛇，失败则切出区域再切回以重置人物位置后重试

        goto_snake 内部已把「整套定位流程」重来了 MAX_SNAKE_LOCATE_ROUND 轮仍失败才返回 False，
        说明人物卡在地图某处、引导图一直不出现。原地再重来整套无法脱困——站位没变，
        同一死角会反复复现；切到别的区域再切回来会让人物重新进区回到出生点，走位路径随之复位。

        :param snake_area: 小蛇所在区域（C 区/白藏主）
        :param max_retry: 定位失败后的最大重试次数（每次重试前做一次切区重置）；
                          until_start=True 时忽略该值
        :param until_start: True=集结期模式，不限重试次数，直到定位成功或狭间开战
                            （I_IS_ATTACK 出现）为止，最大化利用集结期的等待时间
        :return bool 是否成功定位到小蛇
        """
        if self.goto_snake():
            return True
        attempt = 0
        while 1:
            attempt += 1
            if until_start:
                # 集结期模式：以「是否已开战」作为循环边界，而不是固定次数。
                # 开战后必须立刻停止重试，把控制权交回主流程去打小蛇
                self.screenshot()
                if self.appear(self.I_IS_ATTACK):
                    logger.info("Abyss shadows started while locating snake, stop retry")
                    return False
                logger.warning(f"Locate snake failed, reset position by area switch (attempt {attempt}, "
                               f"unlimited until start)")
            else:
                if attempt > max_retry:
                    logger.warning(f"Locate snake still failed after {max_retry} area-switch retries")
                    return False
                logger.warning(f"Locate snake failed, reset position by area switch ({attempt}/{max_retry})")
            if not self.reset_position_by_area_switch(snake_area):
                return False
            if self.goto_snake():
                return True

    def back_to_area_navigation(self, timeout: int = 20) -> bool:
        """ 带超时地退回区域导航界面（切区重置前先把当前画面收拾干净）

        小蛇走位超时时画面可能停在敌人信息面板/战报/6号怪信息窗上，
        而 change_area 首个循环等的是导航按钮且没有超时保护，直接调它有卡死风险；
        这里先把这些浮层关掉，关不掉就超时返回 False 让调用方放弃重置。

        :param timeout: 最长收尾时间（秒）
        :return bool 是否已回到区域导航界面
        """
        timer = Timer(timeout)
        timer.start()
        while 1:
            self.screenshot()
            if self.appear(self.I_CHECK_FINISH):
                raise AbyssShadowsFinished
            # 导航按钮可见即已在区域导航界面，change_area 的首个循环随后可立即通过
            if self.appear(self.I_ABYSS_NAVIGATION):
                return True
            if timer.reached():
                logger.warning("Back to area navigation timeout")
                return False
            if self.appear_then_click(self.I_ABYSS_MAP_EXIT, interval=2):
                continue
            if self.appear_then_click(self.I_ABYSS_ENEMY_INFO_EXIT, interval=2):
                continue
            if self.appear_then_click(self.I_A_BACK_RED, interval=2):
                continue

    def reset_position_by_area_switch(self, snake_area: AreaType) -> bool:
        """ 切到别的区域再切回小蛇区，用于重置人物在地图上的位置

        优先切 D 区（黑豹暗域）；D 区已被封印/不可用时依次退回其它非小蛇区域，
        避免 D 区不可用就完全没法重置。
        :param snake_area: 小蛇所在区域，切走后要切回来
        :return bool 是否成功切走并切回到小蛇所在区域
        """
        # 先退回区域导航界面，避免带着浮层进 change_area 卡死
        if not self.back_to_area_navigation():
            return False
        # D 区优先，其余区域作为 D 区不可用时的备选
        candidates = [AreaType.LEOPARD] + [a for a in AreaType if a is not AreaType.LEOPARD]
        for area in candidates:
            if area is snake_area:
                continue
            if not self.change_area(area):
                logger.info(f"Area {area.name} unavailable for position reset, try next")
                continue
            logger.info(f"Switched to {area.name} for reset, switching back to {snake_area.name}")
            if not self.change_area(snake_area):
                logger.warning(f"Snake area {snake_area.name} unavailable after switching back")
                return False
            return True
        logger.warning("No area available for snake position reset")
        return False

    def run_snake_battles(self, prepared: bool = False):
        """ 小蛇战斗主控制流程

        小蛇战斗特点：
        - 固定在 C 区（白藏主暗域）进行，与普通怪物的攻打顺序无关，永远在狭间最开始执行；
        - 同一位置（6号怪处的小蛇）可无限次战斗，靠计数控制次数；
        - 计数与区域无关（打满 snake_battle_count 次即停）；
        - 进度并入 progress_cursor（'SNAKE-<k>'），支持中断后续跑。

        :param prepared: 是否已在集结期由 prepare_snake_before_start 完成
                         「切区域 + 换御魂 + 走位定位」，True 则直接进入战斗循环
        """
        pm = self.config.model.abyss_shadows.process_manage
        if not pm.enable_snake:
            logger.info("Snake battle disabled, skip")
            return

        target_count = pm.snake_battle_count
        done_count = self.get_snake_done_from_cursor()
        if done_count >= target_count:
            logger.info(f"Snake battle already done ({done_count}/{target_count}), skip")
            return

        # 小蛇固定在 C 区（白藏主暗域，AreaType.FOX）进行，与 attack_order 无关
        snake_area = AreaType.FOX
        logger.info(f"Snake battle area: {snake_area.name}, progress {done_count}/{target_count}, "
                    f"prepared={prepared}")

        if not prepared:
            # 集结期未完成预备（未启用预备/预备失败/中途续跑已开打），走完整前置流程

            # 进入小蛇所在区域；C 区不可用（封印/未开启）则跳过小蛇
            if not self.change_area(snake_area):
                logger.warning(f"Snake area {snake_area.name} unavailable, skip snake battle")
                return

            # 定位小蛇前先按小蛇御魂预设懒切换（此时在区域导航界面，式神录入口可用）
            # 若退出式神录时退过头（已重进狭间），需重新定位到小蛇所在区域
            if self.switch_soul_lazy(self.get_soul_preset('SNAKE')):
                if not self.change_area(snake_area):
                    logger.warning(f"Snake area {snake_area.name} unavailable after re-enter, skip snake battle")
                    return

            # 定位小蛇：点击6号怪 -> 点固定坐标 -> 等待 I_ABYSS_ENEMY_FIRE 出现
            # 已开战，只允许 1 次切区重置，避免定位耗时挤占战斗时间
            if not self.locate_snake_with_retry(snake_area, max_retry=1):
                logger.warning("Failed to locate snake, skip snake battle")
                return

        # 循环战斗直到达到目标次数
        # relocate_fail 记录“挑战按钮缺失并重新定位”的连续次数，超过阈值则放弃，避免无进展空转
        relocate_fail = 0
        while done_count < target_count:
            self.screenshot()
            # 战斗入口标志：小蛇挑战按钮
            if not self.appear(self.I_ABYSS_ENEMY_FIRE):
                # 挑战按钮未出现，尝试重新定位
                logger.info("I_ABYSS_ENEMY_FIRE not found, try to relocate snake")
                relocate_fail += 1
                # 单次重定位只做 1 次切区重置（本循环累计最多重定位 3 次），
                # 避免最坏情况下 3×3 次 60 秒走位把战斗时间全耗在定位上
                if relocate_fail >= 3 or not self.locate_snake_with_retry(snake_area, max_retry=1):
                    logger.warning("Relocate snake failed, stop snake battle")
                    break
                continue
            # 成功出现挑战按钮，重置重定位计数
            relocate_fail = 0
            suc = self.run_snake_single()
            # 无论本场成功与否，都清理战斗卡死记录，避免影响后续流程
            self.device.stuck_record_clear()
            if not suc:
                logger.warning("Snake single battle not success, stop snake battle")
                break

            # 战斗完成，游标前进到 SNAKE-<k> 并落盘（保证中断可续跑；小蛇不计入补全三计数）
            done_count += 1
            self.progress_cursor = f'SNAKE-{done_count}'
            self.advance_cursor('SNAKE', real_battle=False)
            logger.info(f"Snake battle progress {done_count}/{target_count}")

        logger.info(f"Snake battle finished, total {done_count}/{target_count}")

    def get_snake_done_from_cursor(self) -> int:
        """ 从游标推断小蛇已完成次数

        - 空游标 -> 0
        - 'SNAKE-<k>' -> k
        - 普通怪游标（如 'A-5'）-> 小蛇段早已越过，视为已全部完成，返回 snake_battle_count
        """
        cursor = self.progress_cursor
        target_count = self.config.model.abyss_shadows.process_manage.snake_battle_count
        if not cursor:
            return 0
        if cursor.startswith('SNAKE-'):
            try:
                return int(cursor.split('-', 1)[1])
            except ValueError:
                return 0
        # 游标已到普通怪，小蛇段必然已完成
        return target_count

    def goto_snake(self) -> bool:
        """ 定位小蛇：点击6号怪 -> 点击固定坐标 -> 等待挑战按钮出现

        整套流程（开导航 -> 点6号怪 -> 点前往 -> 走位）最多重来 MAX_SNAKE_LOCATE_ROUND 轮：
        单轮走位超时（SNAKE_LOCATE_TIMEOUT）不直接失败，而是重来整套；轮次用尽才返回 False，
        由调用方 locate_snake_with_retry 切区重置人物位置后再整体重试。
        :return bool 是否成功出现 I_ABYSS_ENEMY_FIRE
        """
        logger.info("Goto snake")
        count_click_goto_enemy = 0
        # 整套定位流程的轮次计数（走位超时会 +1 并重来整套）
        locate_round = 0
        # 点击战报
        while 1:
            self.screenshot()
            # 仅首轮允许「已在敌人面板」的快速路径：走位超时重来时画面往往仍有 I_ABYSS_FIRE，
            # 此时若直接 break 会把未完成的定位误判为成功
            if locate_round == 0 and self.appear(self.I_ABYSS_FIRE):
                break
            # 尝试使用左下方摇杆移动
            if count_click_goto_enemy > 0 and self.appear(self.I_ABYSS_NAVIGATION):
                self.move_a_little()
            # 打开导航页面
            self.open_navigation()
            # 点击攻打区域,直到出现"前往"字样
            click_times = 0
            while 1:
                self.screenshot()
                # 如果点3次还没进去就表示目标已死亡,跳过
                if click_times >= 3:
                    logger.warning(f"Failed to click {self.C_ELITE_3_CLICK_AREA}")
                    return False
                # 出现前往按钮就退出
                if self.appear(self.I_ABYSS_GOTO_ENEMY):
                    logger.info(f"{self.I_ABYSS_GOTO_ENEMY} appear")
                    break
                if self.click(self.C_ELITE_3_CLICK_AREA, interval=1.5):
                    click_times += 1
                    continue
                if self.appear_then_click(self.I_ENSURE_BUTTON, interval=1):
                    continue

            # 点击前往按钮,知道该按钮消失或出现"挑战"字样

            while 1:
                self.screenshot()
                if self.appear(self.I_CHECK_FINISH):
                    raise AbyssShadowsFinished
                if self.appear(self.I_ABYSS_FIRE):
                    logger.info(f"{self.I_ABYSS_FIRE} appear")
                    break
                if self.appear(self.I_ENSURE_BUTTON):
                    self.click(self.I_ENSURE_BUTTON, interval=1)
                    continue
                if self.appear(self.I_ABYSS_GOTO_ENEMY):
                    self.click(self.I_ABYSS_GOTO_ENEMY, interval=1)
                    count_click_goto_enemy += 1
                    continue
                if not self.wait_until_appear(self.I_ABYSS_FIRE, wait_time=10):
                    break
            # 单轮走位最多等待 SNAKE_LOCATE_TIMEOUT 秒，避免引导图异常时卡住
            snake_flow_timer = Timer(SNAKE_LOCATE_TIMEOUT)
            snake_flow_timer.start()
            to_snake_flow_timer = Timer(5)
            to_snake_flow_timer.start()
            to_snake_flow_timer._current =time.time()-5
            # 小蛇二阶段最多点击10次“中心下方148px”位置
            while 1:
                self.screenshot()
                if snake_flow_timer.reached():
                    # 本轮走位超时：不直接判失败，重来整套定位流程；轮次用尽才失败
                    locate_round += 1
                    if locate_round >= MAX_SNAKE_LOCATE_ROUND:
                        logger.warning(f"Locate snake flow timeout, {locate_round} rounds used up")
                        return False
                    logger.warning(f"Locate snake flow timeout, retry whole flow "
                                   f"({locate_round}/{MAX_SNAKE_LOCATE_ROUND})")
                    break
                if self.appear(self.I_MONSTER_6)and self.appear_then_click(self.I_A_BACK_RED, interval=1):
                    logger.info('close monster6 info window')
                    continue
                # 发现二阶段引导图即表示已完成定位
                if self.appear(self.I_TO_SNAKE2) and self.appear(self.I_ABYSS_ENEMY_FIRE):
                    logger.info('finish locate snake')
                    return True
                if to_snake_flow_timer.reached() and self.appear(self.I_TO_SNAKE):
                    # 发现引导图时点击正中心，等待5秒确认其是否消失
                    while 1:
                        self.screenshot()
                        if not self.appear(self.I_TO_SNAKE):
                            break
                        x, y = self.I_TO_SNAKE.front_center()
                        self.device.click(x=x, y=y)
                        logger.info(f"Click to snake flow at {x=}, {y=}")
                        sleep(3)
                    self.screenshot()
                    # 点击绝对坐标前往两个小蛇之间
                    self.device.click(x=731, y=622)
                    sleep(3)
                    to_snake_flow_timer.reset()
                    continue

    def run_snake_single(self):
        """ 小蛇单场战斗：切队伍 -> 点挑战 -> 准备 -> 按策略退出 -> 等待胜利

        战斗中段逻辑与精英一致，仅队伍预设与退出策略使用小蛇专属配置。
        :return bool 本场战斗是否成功
        """
        success = False
        pm = self.config.model.abyss_shadows.process_manage

        # 切换小蛇队伍：用字符串 'SNAKE' 作为 cur_enemy_type 标记，
        # 以便后续从小蛇切回普通怪（精英等）时能重新触发切队
        preset = pm.preset_snake
        

        # 点击小蛇挑战按钮，进入战斗准备界面
        # 加超时保护，避免既无挑战按钮也无准备按钮时死循环空转
        _timer_enter = Timer(30)
        _timer_enter.start()
        while 1:
            self.screenshot()
            
            if self.appear(self.I_CHECK_FINISH):
                raise AbyssShadowsFinished
            if self.appear(self.I_PREPARE_HIGHLIGHT):
                break
            if _timer_enter.reached():
                # 超时仍未进入准备界面，放弃本场，交由调用方停止
                logger.warning("Enter snake prepare page timeout")
                return False
            # 奖励次数上限等确认框
            if self.appear_then_click(self.I_ENSURE_BUTTON, interval=1):
                continue
            if self.appear(self.I_ABYSS_ENEMY_FIRE):
                self.click(self.I_ABYSS_ENEMY_FIRE, interval=0.4)
                self.wait_until_appear(self.I_PREPARE_HIGHLIGHT, wait_time=2)
                continue
            if self.appear(self.I_ABYSS_FIRE):
                self.click(self.I_ABYSS_FIRE, interval=0.4)
                self.wait_until_appear(self.I_PREPARE_HIGHLIGHT, wait_time=2)
                continue
        if self.cur_enemy_type != 'SNAKE':
                logger.info(f"Snake--Switch preset to {preset} and {self.cur_enemy_type=}")
                self.switch_preset_team_with_str(preset)
                self.cur_preset = preset
                self.cur_enemy_type = 'SNAKE'
        # 点击准备
        _timer_battle = Timer(180)
        self.wait_until_appear(self.I_PREPARE_HIGHLIGHT, wait_time=3)
        self.ui_click_until_disappear(self.I_PREPARE_HIGHLIGHT, interval=0.6)
        _timer_battle.start()

        # 生成退出条件（小蛇专属策略）
        condition = pm.generate_snake_quit_condition()
        logger.info(f"Snake--{condition}")

        _cur_damage = 0
        need_check_damage = condition.is_need_damage_value()
        self.device.screenshot_interval_set(1)
        self.device.stuck_record_add('BATTLE_STATUS_S')
        while True:
            self.screenshot()
            if need_check_damage:
                _cur_damage = self.O_DAMAGE.ocr_digit(self.device.image)
            if condition.is_valid(_cur_damage):
                logger.info("Condition Validated,try to quit battle")
                self.device.screenshot_interval_set()
                self.quit_battle()
                break
            if self.appear_then_click(self.I_PREPARE_HIGHLIGHT, interval=3):
                # 正常来讲，此处不应该出现准备按钮，以防万一
                self.device.stuck_record_add("BATTLE_STATUS_S")
                _timer_battle.reset()
                continue
            # 结算点击：安全落点 + 固定单击（settlement_click_count 覆盖为 1）
            action_click = weighted_choice(self.reward_click_actions())
            # 战斗胜利标志
            if self.settlement_click(self.I_WIN, action_click, interval=1):
                self.device.screenshot_interval_set()
                need_check_damage = False
                continue
            # 战斗奖励标志
            if self.settlement_click(self.I_REWARD, action_click, interval=1):
                self.device.screenshot_interval_set()
                need_check_damage = False
                continue
            if self.appear(self.I_ABYSS_NAVIGATION):
                self.device.screenshot_interval_set()
                break
        if condition.is_passed() or (not _timer_battle.reached()):
            logger.info("Snake battle result SUCCESS")
            success = True

        logger.info("Snake battle DONE")
        return success

    def quit_battle(self):
        logger.info("Quitting battle")
        while True:
            self.screenshot()
            if self.appear(self.I_EXIT_ENSURE):
                if self.click(self.I_EXIT_ENSURE, interval=1):
                    self.wait_until_appear(self.I_ABYSS_NAVIGATION, wait_time=1)
                continue
            if self.appear(self.I_ABYSS_NAVIGATION):
                break
            if self.appear(self.I_WIN):
                self.click(self.I_WIN, interval=1)
                continue
            if self.appear(self.I_REWARD):
                self.click(self.I_REWARD, interval=1)
                continue
            if self.appear(self.I_EXIT):
                if self.click(self.I_EXIT, interval=2):
                    self.wait_until_appear(self.I_EXIT_ENSURE, wait_time=1)
                continue
        return

    def switch_preset_team_with_str(self, v: str):
        tmp = v.split(',')
        if not tmp or len(tmp) != 2:
            logger.error(f"Due to a configuration error (value: {v}), an error occurred while switch preset team.")
            return
        self.switch_preset_team(True, int(tmp[0]), int(tmp[1]))

    def get_soul_preset(self, enemy_type) -> str:
        """ 按敌人类型返回对应的御魂预设字符串（如 '6,3'）

        小蛇用字符串 'SNAKE' 标记；御魂预设与队伍预设共用同一组配置值。
        :param enemy_type: EnemyType 或字符串 'SNAKE'
        :return str 御魂预设 'group,team'
        """
        pm = self.config.model.abyss_shadows.process_manage
        if enemy_type == 'SNAKE':
            return pm.preset_snake
        match enemy_type:
            case EnemyType.BOSS:
                return pm.preset_boss
            case EnemyType.GENERAL:
                return pm.preset_general
            case EnemyType.ELITE:
                return pm.preset_elite

    def switch_soul_lazy(self, preset: str) -> bool:
        """ 御魂懒切换：与队伍切换同样按需切换，仅当本场御魂预设与上次不同才切

        - enable_switch_soul_in_as=False：不启用御魂切换，直接返回（此时仍会正常切队伍）
        - preset 与 cur_soul_preset 相同：跳过，避免重复进出式神录
        - 否则：进式神录 -> run_switch_soul -> 退出式神录 -> 记录 cur_soul_preset

        调用前须处于区域导航界面（式神录入口 I_ABYSS_SHIKI 可见）。
        :param preset: 御魂预设字符串 'group,team'
        :return bool 退出式神录时是否退过头离开了区域（True=调用方需重新 change_area 定位区域）
        """
        if not self.config.model.abyss_shadows.process_manage.enable_switch_soul_in_as:
            # 未启用御魂切换，只切队伍不切御魂
            return False
        if preset == self.cur_soul_preset:
            logger.info(f"Soul preset {preset} same as current, skip switch")
            return False

        l = preset.split(',')
        if len(l) != 2:
            logger.error(f"Due to a configuration error (value: {preset}), an error occurred while switch soul.")
            raise RequestHumanTakeover

        logger.info(f"Switch soul preset to {preset} (from {self.cur_soul_preset})")
        # 进入式神录
        self.ui_click_until_disappear(self.I_ABYSS_SHIKI, interval=2)
        # 切换御魂
        self.run_switch_soul((int(l[0]), int(l[1])))
        self.cur_soul_preset = preset
        # 退出式神录，回到区域导航界面；退过头则已重进狭间，返回 True 让调用方重新定位区域
        return self.exit_shikigami_in_as()

    def exit_shikigami_in_as(self) -> bool:
        """ 退出狭间内的式神录界面，回到区域导航界面

        正常情况点返回按钮即可回到区域导航界面（I_ABYSS_SHIKI/I_ABYSS_NAVIGATION 可见）。
        若不小心退过头到大地图/寮/庭院，则调用 goto_abyss_shadows 重新进入狭间。

        :return bool 是否退过头离开了区域（True=已退出到狭间外并重进，调用方需重新 change_area 定位区域）
        """
        from tasks.GameUi.assets import GameUiAssets as gua
        while 1:
            self.screenshot()
            # 优先判断是否已回到区域导航界面（正常退出路径），此时无需任何额外动作
            if self.appear(self.I_ABYSS_SHIKI) or self.appear(self.I_ABYSS_NAVIGATION):
                return False
            # 退过头到寮/庭院：重新进入狭间，返回 True 让调用方重新定位区域
            # 用无副作用的 appear 检测寮/庭院的 check_button，不能用 ui_get_current_page —
            # 后者在遇到过渡帧识别不到已知页时会主动点 I_BACK_MAIN 一键回主页，把还在式神录的正常状态强行拽出
            if self.appear(gua.I_CHECK_GUILD) or self.appear(gua.I_CHECK_MAIN):
                logger.warning("Exited too far to guild/main page, re-entering abyss shadows")
                self.goto_abyss_shadows()
                # 重进后停在集结界面，需先 select_boss 进入一个区域，恢复到有导航按钮的状态，
                # 调用方随后的 change_area 才能正常从导航界面定位到真正的目标区域
                area = self.get_first_area_to_enter()
                if area is not None:
                    self.select_boss(area)
                return True
            if self.appear_then_click(gua.I_BACK_Y, interval=2):
                continue

    def check_available(self, item_code: Code):
        # 判断该怪物是否可用
        # TODO 设想使用平均亮度分辨 是否可用
        self.change_area(item_code.get_areatype())

        while True:
            if self.appear(self.I_ABYSS_NAVIGATION):
                self.click(self.I_ABYSS_NAVIGATION, interval=2)
                continue
            if self.appear(self.I_ABYSS_MAP):
                break

        return True

    def detect_area_status(self):
        # 在切换区域界面检查各个区域是否可用
        #
        available_areas = []
        unavailable_areas = []
        self.screenshot()
        for area in AreaType:
            if self.is_area_done(area):
                unavailable_areas.append(area)
                # self.unavailable_list += CodeList(IndexMap[area.name].value)
                logger.info(f"{area.name} unavailable")
                continue
            available_areas.append(area)
            logger.info(f"{area.name} available")
        return available_areas, unavailable_areas

    def is_area_done(self, area_type: AreaType):
        # 不再切换区域界面直接返回
        if not self.appear(self.I_ABYSS_DRAGON) and not self.appear(self.I_ABYSS_DRAGON_OVER):
            return False
        #
        res_img = self.device.image

        match area_type:
            case AreaType.DRAGON:
                ocr_res = self.O_DRAGON_DONE.ocr(res_img)
                return ocr_res.find('封印') != -1
            case AreaType.FOX:
                ocr_res = self.O_FOX_DONE.ocr(res_img)
                return ocr_res.find('封印') != -1
            case AreaType.LEOPARD:
                ocr_res = self.O_LEOPARD_DONE.ocr(res_img)
                return ocr_res.find('封印') != -1
            case AreaType.PEACOCK:
                ocr_res = self.O_PEACOCK_DONE.ocr(res_img)
                return ocr_res.find('封印') != -1

        return False

    def move_a_little(self):
        radius = 150
        # 寮里面摇杆的中心点
        p1 = (197, 568)
        import random
        dx, dy = random.randint(-radius, radius), random.randint(-radius, radius)
        self.device.swipe_adb(p1, (p1[0] + dx, p1[1] + dy), duration=0.5)
        logger.info(f"Swipe {p1} to {(p1[0] + dx, p1[1] + dy)}")


if __name__ == "__main__":
    import cv2, numpy as np
    from module.config.config import Config
    from module.device.device import Device

    config = Config('oas2')
    device = Device(config)

    # image = cv2.imread('E:/f.png')
    # image = cv2.cvtColor(image, cv2.COLOR_BGR2RGB)
    #
    # hsv_image = cv2.cvtColor(image, cv2.COLOR_RGB2HSV)
    #
    # lower_green = np.array([9, 128, 180])
    # upper_green = np.array([30, 210, 255])
    # mask = cv2.inRange(hsv_image, lower_green, upper_green)
    # res_img = cv2.bitwise_and(image, image, mask=mask)
    # res_img = cv2.cvtColor(res_img, cv2.COLOR_RGB2BGR)
    # cv2.imshow('res', res_img)
    # cv2.waitKey()
    t = ScriptTask(config, device)
    t.run()
    #radius = 150
    """ p1 = (197, 568)
    import random

    while True:
        dx, dy = random.randint(-radius, radius), random.randint(-radius, radius)
        t.device.swipe_adb(p1, (p1[0] + dx, p1[1] + dy), duration=0.5)
        logger.info(f"Swipe {p1} to {(p1[0] + dx, p1[1] + dy)}")
        sleep(5) """

    # area_type = AreaType.DRAGON
    # t.unavailable_list += CodeList(IndexMap[area_type.name].value)
    # print(f"{t.unavailable_list=}")
    # t.screenshot()

    # cv2.imshow("origin", t.device.image)
    # cv2.waitKey()

    # res = t.O_TEST_PRE.ocr(image)
    # print(res)
    # damage = t.O_DAMAGE.ocr(res_img)
    # print(damage)

    # t.done_list = CodeList('A-4')
    # t.unavailable_list  = CodeList('D-3')
    # t.flash_list()

    # code = Code('D-1')
    # a = code.get_enemy_type()
    # b = code.get_enemy_click()
    # c = code.get_areatype()
    # print(a, b, c)
    #
    # t.is_area_done(AreaType.DRAGON)
    # t.screenshot()
    # t.start_abyss_shadows()
    # hsv_image = cv2.cvtColor(image, cv2.COLOR_RGB2HSV)
    #
    # lower_green = np.array([9, 128, 180])
    # upper_green = np.array([30, 210, 255])
    # mask = cv2.inRange(hsv_image, lower_green, upper_green)
    # res_img = cv2.bitwise_and(image, image, mask=mask)
    # res_img = cv2.cvtColor(res_img, cv2.COLOR_RGB2BGR)
