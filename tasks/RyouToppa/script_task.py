# This Python file uses the following encoding: utf-8
# @author runhey
# github https://github.com/runhey
import time
from datetime import datetime, timedelta, time as dt_time
import random

from tasks.Component.SwitchSoul.switch_soul import SwitchSoul
from tasks.RyouToppa.assets import RyouToppaAssets
from tasks.Component.GeneralBattle.general_battle import GeneralBattle
from tasks.Component.GeneralBattle.reward_frame import FORBIDDEN_KEKKAI
from tasks.Component.config_base import ConfigBase, Time
from tasks.GameUi.game_ui import GameUi
from tasks.GameUi.page import page_realm_raid, page_main, page_kekkai_toppa, page_shikigami_records
from tasks.RealmRaid.assets import RealmRaidAssets
from tasks.RealmRaid.script_task import ScriptTask as RealmRaidScriptTask

from module.logger import logger
from module.exception import TaskEnd
from module.atom.click import RuleClick
from module.base.utils import point2str
from module.base.timer import Timer
from module.exception import GamePageUnknownError

# 寮突破复用个人突破的四态识别：同一套勋章模板（I_MEDAL / I_NO_MEDAL）在两个界面都可命中，
# 网格形状 4 排 2 列（个人突破是 3×3），由 RealmRaidScriptTask.detect_cells 的参数区分。
RYOU_ROWS_EXPECTED = 4
RYOU_COLS_EXPECTED = 2
# 屏幕可见失败结界达到该数量就上划刷新，去找没失败的区域
RYOU_FLUSH_FAILED_COUNT = 6
# 寮突破网格的回退锚点（首槽左上角），取自实测截图 capture_1788805078387：
# 行 y=199/334/459/594（行距 135）、列 x=518/855（列距 337）
RYOU_FALLBACK_SLOT_X = (518, 855)
RYOU_FALLBACK_SLOT_Y = (199, 334, 459, 594)


def random_delay(min_value: float = 1.0, max_value: float = 2.0, decimal: int = 1):
    """
    生成一个指定范围内的随机小数
    """
    random_float_in_range = random.uniform(min_value, max_value)
    return (round(random_float_in_range, decimal))

class ScriptTask(RealmRaidScriptTask, GeneralBattle, GameUi, SwitchSoul, RyouToppaAssets):
    _current_cells: list = None  # 本轮识别的格子结果，attack_area 取点击区用

    # MRO 资产遮蔽修正：RealmRaidScriptTask 的 RealmRaidAssets 排在 RyouToppaAssets 之前，
    # 两者唯一的同名资产 O_NUMBER 会解析到个人突破版（右上角 1143,13 突破卷数量），
    # 而寮突的进攻机会 OCR 在左下角 (271,560)——被遮蔽后 has_ticket 永远读空、
    # 误判 no ticket 直接结束任务。显式重绑回寮突自己的实例。
    O_NUMBER = RyouToppaAssets.O_NUMBER

    def reward_forbidden(self) -> tuple:
        """寮突破结算界面的常驻禁点区域（顶左条 + 顶右条 + 左下角）。"""
        return FORBIDDEN_KEKKAI

    def detect_ryou_cells(self, screenshot: bool=True) -> list:
        """寮突破 4×2 网格的四态识别，复用个人突破的勋章反推识别链。

        列锚点/行距与个人突破略有差异（列距 337 vs 332），按实测值传参。
        滚动截断后可能只返回 3 行甚至更少——识别到几行处理几行，不补齐。
        寮突不消费等级，read_level=False 跳过逐格等级 OCR。
        画面有效性预检：一个勋章槽都识别不到说明不是列表界面（结算/过渡帧），
        返回 [] 交给调用方重试——绝不回退写死锚点在错误画面上点击。
        """
        if screenshot:
            self.screenshot()
        hits = self._slot_hits(self.device.image)
        if not hits:
            logger.warning('No medal slot on screen, realm list is not ready, retry later')
            return []
        # 预检已跑过全图匹配，传给 detect_cells 复用，避免重复匹配
        return self.detect_cells(screenshot=False,
                                 rows_expected=RYOU_ROWS_EXPECTED,
                                 cols_expected=RYOU_COLS_EXPECTED,
                                 fallback_x=RYOU_FALLBACK_SLOT_X,
                                 fallback_y=RYOU_FALLBACK_SLOT_Y,
                                 read_level=False,
                                 slot_hits=hits)

    def _infer_hidden_failed(self, cells: list) -> list:
        """按列表有序性补判被遮挡的失败结界。

        寮突列表从上到下固定为 失败区 → 正常区 → 攻破区：失败结界只会在正常结界上面，
        正常结界打胜沉到攻破区尾部、打败浮到失败区尾部。因此任何一个可见的失败格，
        其上方的所有格必然也是失败——包括被上划截断、失败标志识别不到的格子。
        攻破格不受此规则影响（它在失败区下面），跳过不覆盖。
        """
        result = [dict(c) for c in cells]
        for i, cell in enumerate(result):
            if cell['state'] != 'FAILED':
                continue
            for j in range(i):
                if result[j]['state'] == 'ATTACKABLE':
                    logger.info(f'Cell {result[j]["index"]} inferred FAILED '
                                f'(above failed cell {cell["index"]})')
                    result[j]['state'] = 'FAILED'
                    result[j]['inferred'] = True
        return result

    def decide_ryou_flush(self, cells: list) -> bool:
        """可见失败结界（含反推补判）达到阈值就上划，找没失败的区域。"""
        failed = [c for c in cells if c['state'] == 'FAILED']
        if len(failed) >= RYOU_FLUSH_FAILED_COUNT:
            logger.info(f'{len(failed)} failed realms on screen >= {RYOU_FLUSH_FAILED_COUNT}, flush area cache')
            return True
        return False

    def find_ryou_attack_start(self, cells: list) -> int:
        """定位进攻起点：最后一个失败格的下一格。

        列表有序保证失败区是连续块，最后一个失败格之后必然全是正常结界。
        :return: 进攻起点 index（1-based）；None 表示没有可打的正常结界（失败区
                 直接衔接攻破区或全屏攻破），任务应结束
        """
        last_failed = None
        for cell in cells:
            if cell['state'] == 'FAILED':
                last_failed = cell
        if last_failed is None:
            # 屏幕上没有失败格：要么全是正常结界（从头打），要么全是攻破
            if any(c['state'] == 'ATTACKABLE' for c in cells):
                return cells[0]['index']
            logger.info('No attackable realm on screen, ryou toppa is done')
            return None
        # 最后一个失败格的下一格（按屏幕线性序）：是攻破 → 失败区已衔接攻破区
        next_cell = next((c for c in cells if c['index'] > last_failed['index']), None)
        if next_cell is None:
            logger.info('Failed realm is the last visible cell, flush to check the next row')
            return None
        if next_cell['state'] == 'FINISHED':
            logger.info(f'Cell {next_cell["index"]} after last failed cell {last_failed["index"]} '
                        f'is FINISHED, no normal realm left')
            return None
        return next_cell['index']

    def run(self):
        """
        执行
        :return:
        """
        ryou_config = self.config.ryou_toppa
        time_limit: Time = ryou_config.raid_config.limit_time
        time_delta = timedelta(hours=time_limit.hour, minutes=time_limit.minute, seconds=time_limit.second)

        if ryou_config.switch_soul_config.enable:
            self.ui_get_current_page()
            self.ui_goto(page_shikigami_records)
            self.run_switch_soul(ryou_config.switch_soul_config.switch_group_team)

        if ryou_config.switch_soul_config.enable_switch_by_name:
            self.ui_get_current_page()
            self.ui_goto(page_shikigami_records)
            self.run_switch_soul_by_name(ryou_config.switch_soul_config.group_name, ryou_config.switch_soul_config.team_name)

        self.ui_get_current_page()
        self.ui_goto(page_kekkai_toppa)
        ryou_toppa_start_flag = True
        ryou_toppa_success_penetration = False
        ryou_toppa_admin_flag = False
        # 点击突破
        while 1:
            self.screenshot()
            if self.appear_then_click(RealmRaidAssets.I_REALM_RAID, interval=1):
                continue
            if self.appear(self.I_REAL_RAID_REFRESH, threshold=0.8):
                if self.appear_then_click(self.I_RYOU_TOPPA, interval=1):
                    continue
            # 攻破阴阳寮，说明寮突已开，则退出
            elif self.appear(self.I_SUCCESS_PENETRATION, threshold=0.8):
                ryou_toppa_start_flag = True
                ryou_toppa_success_penetration = True
                break
            # 出现选择寮突说明寮突未开
            elif self.appear(self.I_SELECT_RYOU_BUTTON, threshold=0.8):
                ryou_toppa_start_flag = False
                ryou_toppa_admin_flag = True
                break
            # 出现晴明说明寮突未开
            elif self.appear(self.I_NO_SELECT_RYOU, threshold=0.8):
                ryou_toppa_start_flag = False
                break
            # 出现寮奖励， 说明寮突已开
            elif self.appear(self.I_RYOU_REWARD, threshold=0.8) or self.appear(self.I_RYOU_REWARD_90, threshold=0.8):
                ryou_toppa_start_flag = True
                break

        logger.attr('ryou_toppa_start_flag', ryou_toppa_start_flag)
        logger.attr('ryou_toppa_success_penetration', ryou_toppa_success_penetration)
        # 寮突未开 并且有权限， 开开寮突，没有权限则标记失败
        if not ryou_toppa_start_flag:
            if ryou_config.raid_config.ryou_access and ryou_toppa_admin_flag:
                # 作为寮管理，开启今天的寮突
                logger.info("As the manager of the ryou, try to start ryou toppa.")
                self.start_ryou_toppa()
            else:
                logger.info("The ryou toppa is not open and you are a ryou member.")
                self.set_next_run(task='RyouToppa', finish=True, server=True, success=False)
                raise TaskEnd('RyouToppa')

        # 100% 攻破, 第二天再执行
        if ryou_toppa_success_penetration:
            logger.info('RyouToppa is 100%')
            self.plan_tomorrow_ryoutoppa()
            raise TaskEnd('RyouToppa')
        if self.config.ryou_toppa.general_battle_config.lock_team_enable:
            logger.info("Lock team.")
            self.ui_click(self.I_TOPPA_UNLOCK_TEAM, self.I_TOPPA_LOCK_TEAM)
        else:
            logger.info("Unlock team.")
            self.ui_click(self.I_TOPPA_LOCK_TEAM, self.I_TOPPA_UNLOCK_TEAM)
        # --------------------------------------------------------------------------------------------------------------
        # 开始突破：识别 → 上划判定 → 定位起点 → 按序进攻
        # --------------------------------------------------------------------------------------------------------------
        success = True
        while 1:
            # 设置长任务标志,用来寻找寮突可进攻的目标
            self.device.stuck_record_add('PREPARE_BEFORE_BATTLE')
            if not self.has_ticket():
                logger.info("We have no chance to attack. Try again after 1 hour.")
                success = False
                break
            if self.current_count >= ryou_config.raid_config.limit_count:
                logger.warning("We have attacked the limit count.")
                break
            if datetime.now() >= self.start_time + time_delta:
                logger.warning("We have attacked the limit time.")
                break

            # 21 点后免票时段 has_ticket 直接返回（不等界面），结算→列表的过渡帧上
            # 识别会 rows=0 回退写死锚点，第一击落空后还要吃 attack_area 里 5s 的
            # 点击去重。这里显式等寮突界面就绪再识别；白天 has_ticket 已等过，
            # 第二次等待界面已就绪、立即返回
            self.wait_until_appear(self.I_TOPPA_RECORD)
            # 每次战斗前重新识别整屏：列表会重排，上一轮的格子位置不可复用
            cells = self._infer_hidden_failed(self.detect_ryou_cells())
            if not cells:
                # 画面不是列表（结算/过渡帧，勋章槽一个都没有）：回循环顶部重试，
                # 不能把空结果当「没有可打目标」结束任务
                continue
            self._current_cells = cells  # attack_area 取本轮回退落的点击区
            # 失败结界过多：上划刷新，把没失败的区域滚上来
            if self.decide_ryou_flush(cells):
                self.flush_area_cache()
                continue
            # 定位进攻起点：最后一个失败格的下一格；没有正常结界则整个寮突结束
            start = self.find_ryou_attack_start(cells)
            if start is None:
                logger.info('No normal realm left to attack, finish ryou toppa')
                break
            # 从起点开始按屏幕顺序进攻，打完一个重新识别（列表重排会把后面的正常结界顶上来）
            res = self.attack_area(start)
            if not res:
                # 区域不可用或战斗失败：回到循环顶部重新识别定位，而不是盲目 +1 跳格
                continue


        # 回 page_main 失败
        # self.ui_current = page_ryou_toppa
        # self.ui_goto(page_main)
        if success:
            self.plan_tomorrow_ryoutoppa()
            #self.set_next_run(task='RyouToppa', finish=True, server=True, success=True)
        else:
            self.set_next_run(task='RyouToppa', finish=True, server=True, success=False)
        raise TaskEnd('RyouToppa')

    def plan_tomorrow_ryoutoppa(self):
        # 安排下次寮突破，便于复用
        now = datetime.now()
        # 如果时间在00:00-5:00之间则设定时间为当天的自定义时间
        if now.hour < 5:  # 不确定 time 的使用范围，重命名 datetime 中的 time
            self.custom_next_run(task='RyouToppa', custom_time=self.config.ryou_toppa.raid_config.next_ryoutoppa_time, time_delta=0)
        # 如果时间在05:00-23:59之间则设定时间为明天的自定义时间
        else:
            self.custom_next_run(task='RyouToppa', custom_time=self.config.ryou_toppa.raid_config.next_ryoutoppa_time, time_delta=1)

    def start_ryou_toppa(self):
        """
        开启寮突破
        :return:
        """
        # 点击寮突
        while 1:
            self.screenshot()
            if self.appear_then_click(self.I_SELECT_RYOU_BUTTON, interval=1):
                break
        logger.info(f'Click {self.I_SELECT_RYOU_BUTTON.name}')

        # 选择第一个寮
        while 1:
            self.screenshot()
            if self.appear_then_click(self.I_GUILD_ORDERS_REWARDS, action=self.C_SELECT_FIRST_RYOU, interval=1):
                break
        logger.info(f'Click {self.C_SELECT_FIRST_RYOU.name}')

        # 点击开始突入
        while 1:
            self.screenshot()
            if self.appear_then_click(self.I_START_TOPPA_BUTTON, interval=1):
                continue
            # 出现寮奖励， 说明寮突已开
            if self.appear(self.I_RYOU_REWARD, threshold=0.8):
                break
        logger.info(f'Click {self.I_START_TOPPA_BUTTON.name}')

    def has_ticket(self) -> bool:
        """
        如果没有票了，那么就返回False
        :return:
        """
        # 21点后、次日5点前无限进攻机会
        if datetime.now().hour >= 21 or datetime.now().hour <= 5:
            return True
        self.wait_until_appear(self.I_TOPPA_RECORD)
        self.screenshot()
        cu, res, total = self.O_NUMBER.ocr(self.device.image)
        if cu == 0 and cu + res == total:
            logger.warning(f'Execute round failed, no ticket')
            return False
        return True

    def flush_area_cache(self):
        time.sleep(2)
        duration = 0.352
        count = random.randint(1, 3)
        for i in range(count):
            # 测试过很多次 win32api, win32gui 的 MOUSEEVENTF_WHEEL, WM_MOUSEWHEEL
            # 都出现过很多次离奇的事件，索性放弃了使用以下方法，参数是精心调试的
            # 每次执行刚好刷新一组（2个）设定随机刷新 1 - 3 次
            safe_pos_x = random.randint(540, 1000)
            safe_pos_y = random.randint(320, 540)
            p1 = (safe_pos_x, safe_pos_y)
            p2 = (safe_pos_x, safe_pos_y - 101)
            logger.info('Swipe %s -> %s, %s ' % (point2str(*p1), point2str(*p2), duration))
            self.device.swipe_adb(p1, p2, duration=duration)
            time.sleep(2)

    def attack_area(self, index: int):
        """
        进攻指定屏幕位置（1-based，来自本轮 detect_ryou_cells 的识别结果）。
        区域状态已在主循环里判定过，这里只负责点击与战斗流程。
        :return: 战斗成功(True) or 战斗失败(False) or 区域不可用（False） or 没有进攻机会（设定下次运行并退出）
        """
        # 正式进攻会设定 2s - 10s 的随机延迟，避免攻击间隔及其相近被检测为脚本。
        if self.config.ryou_toppa.raid_config.random_delay:
            delay = random_delay()
            time.sleep(delay)


        # 点击区复用识别结果里勋章反推出的落点（避开头像与首槽），不再用写死的 C_AREA_x
        cells = self._current_cells
        cell = next((c for c in cells if c['index'] == index), None)
        if cell is None:
            logger.warning(f'Attack area {index} not in current cells, skip')
            return False
        rcl = RuleClick(roi_front=cell['click_roi'], roi_back=cell['click_roi'], name=f'area_{index}')
        # 塔塔开！
        click_failure_count = 0
        exit_count = self.config.ryou_toppa.raid_config.exit_count
        while True:
            self.screenshot()
            if click_failure_count >= 5:
                logger.warning("Click failure, check your click position")
                return False
            if self.appear_then_click(RealmRaidAssets.I_FIRE, interval=2, threshold=0.8):
                click_failure_count += 1
                time.sleep(1)
                self.screenshot()
                if self.appear(self.I_TOPPA_RECORD, threshold=0.85):
                    continue
                if self.config.ryou_toppa.raid_config.exit_count > 0 and self.wait_until_appear(self.I_EXIT, wait_time=5):
                    if exit_count > 0:
                        logger.info('Exit four enable')
                        self.run_general_battle_back(config=self.config.ryou_toppa.general_battle_config, exit_four=True)
                        exit_count -= 1
                        click_failure_count = 0
                        if exit_count > 0:
                            continue
                        else:
                            return False
                else:
                    return self.run_general_battle(config=self.config.ryou_toppa.general_battle_config)
            if self.click(rcl, interval=5):
                click_failure_count += 1
                continue
                

if __name__ == "__main__":
    from module.config.config import Config
    from module.device.device import Device

    config = Config('oas1')
    device = Device(config)
    t = ScriptTask(config, device)
    t.run()
