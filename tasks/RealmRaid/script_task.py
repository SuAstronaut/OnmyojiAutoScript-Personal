# This Python file uses the following encoding: utf-8
# @author runhey
# github https://github.com/runhey
import time
from datetime import  datetime, timedelta
import re
from cached_property import cached_property

from tasks.base_task import BaseTask
from tasks.Component.GeneralBattle.general_battle import GeneralBattle
from tasks.Component.GeneralBattle.reward_frame import FORBIDDEN_KEKKAI
from tasks.GameUi.game_ui import GameUi
from tasks.GameUi.page import page_realm_raid, page_main, page_shikigami_records
from tasks.RealmRaid.assets import RealmRaidAssets
from tasks.RealmRaid.config import RealmRaid, WhenAttackFail
from tasks.Component.SwitchSoul.switch_soul import SwitchSoul
from module.logger import logger
from module.exception import TaskEnd, GameStuckError
from module.atom.image import RuleImage
from module.atom.click import RuleClick
from module.atom.ocr import RuleOcr


# ----------------------------------------------------------------------------------------------------------------------
# 结界突破九宫格：勋章槽位刚性网格的实测参数（1280×720 授权空间）
# 实测三张截图确认：行锚点 y=211/346/481、列锚点 x=242/574/906、槽距 39.4px、每格恒 5 槽。
# 攻破印章（I_FINISH_SIGN，x=406/738/1070 宽 48）恰好压在第 5 槽（外推 x=401/733/1065）上，
# 因此「有印章 ⟺ 槽数 4、无印章 ⟺ 槽数 5」，槽数可作为攻破态的第二证据。
# ----------------------------------------------------------------------------------------------------------------------
GRID_COLS = 3                 # 个人突破：3 列
GRID_ROWS = 3                 # 个人突破：3 行
# 寮突破：4 排 2 列，实测截图 capture_1788805078387 行距 135、列距 337，
# 与个人突破同一套勋章模板（I_MEDAL / I_NO_MEDAL）可命中，仅网格形状不同
RYOU_COLS = 2
RYOU_ROWS = 4
CELL_W = 330                 # 单格识别区标称宽
CELL_H = 133                 # 单格识别区标称高
SLOT_GAP = 39.4              # 相邻勋章槽间距，实测 38~40
SLOTS_PER_CELL = 5           # 每格固定 5 个星位
SLOT_TO_CELL_DX = 97         # 首槽左上角 → 格左边界的偏移
SLOT_TO_CELL_DY = 67         # 首槽左上角 → 格上边界的偏移
CLICK_INSET = 10             # 点击区上/下/右三边内缩（左边界另用第 2 槽起点避开式神头像）
ROW_CLUSTER_GAP = 40         # y 聚类阈值：行距 135，槽高 ~47，40 足以分行且容忍抖动
COL_CLUSTER_GAP = 60         # x 聚类阈值：槽距 39.4 < 60 < 列内跨度到下一列的间隔
# 反推失败时的回退锚点（首槽左上角坐标），取自实测基线
FALLBACK_SLOT_X = (242, 574, 906)
FALLBACK_SLOT_Y = (211, 346, 481)
# 单格状态
CELL_FINISHED = 'FINISHED'       # 已攻破（印章命中）
CELL_FAILED = 'FAILED'           # 攻打失败（自己退的或退四退的）
CELL_ATTACKABLE = 'ATTACKABLE'   # 可正常攻打
# I_FAILURE_SIGN 的真/假间隙最窄（真 1.000 / 假 0.597），单独提阈值留余量，不改资产
FAILURE_SIGN_THRESHOLD = 0.85
# order_attack 填这个值时改为按格号顺序进攻，而不是按勋章数（星级）
ORDER_BY_POSITION = 'position'
# 退四针对的格号：右下角第九格是难度最高的结界
EXIT_FOUR_INDEX = 9
# 结界等级 OCR：相对「勋章槽位锚点」（detect_grid 反推的首槽左上角）的偏移与尺寸，
# 覆盖式神头像左上角的等级数字。不锚定二次推导的格边界——那会引入常数漂移，
# 实测 2px 偏移就能把 60 误读成 160。网格搜索两张基线截图 18/18 全对的稳定窗口。
LEVEL_ROI_DX = -86
LEVEL_ROI_DY = -50
LEVEL_ROI_W = 40
LEVEL_ROI_H = 30
# 等级有效区间：结界等级带宽不超过 5 级，出现 60 级时不可能同时有 55 级以下，
# 所以低于 55 的读数一定是 OCR 误读，不作为有效数据
LEVEL_VALID_MIN = 55
LEVEL_VALID_MAX = 60
# 全退（降低结界等级）与关闭退四（提升结界等级）的判定门槛
LEVEL_FORCE_EXIT_ALL = 59.0    # 均值高于此值直接全退，不看勋章数
LEVEL_EXIT_ALL = 58.3          # 均值高于此值且勋章不足时全退
LEVEL_NO_EXIT_FOUR = 58.3      # 均值不高于此值且勋章不足时禁止退四，让等级涨回来
MEDAL_TOTAL_ENOUGH = 27        # 九格总勋章下限，九格平均 3 个


def _cluster(values: list, gap: float) -> list:
    """把一维坐标按间隔聚类，返回每簇的坐标列表（升序）。"""
    if not values:
        return []
    values = sorted(values)
    groups = [[values[0]]]
    for v in values[1:]:
        if v - groups[-1][-1] <= gap:
            groups[-1].append(v)
        else:
            groups.append([v])
    return groups


class ScriptTask(GeneralBattle, GameUi, SwitchSoul, RealmRaidAssets):

    def reward_forbidden(self) -> tuple:
        """结界突破结算界面的常驻禁点区域（顶左条 + 顶右条 + 左下角）。"""
        return FORBIDDEN_KEKKAI

    def run(self):
        self.run_2()

    def run_2(self):
        con = self.config.realm_raid
        if con.switch_soul_config.enable:
            self.ui_get_current_page()
            self.ui_goto(page_shikigami_records)
            self.run_switch_soul(con.switch_soul_config.switch_group_team)
        if con.switch_soul_config.enable_switch_by_name:
            self.ui_get_current_page()
            self.ui_goto(page_shikigami_records)
            self.run_switch_soul_by_name(con.switch_soul_config.group_name, con.switch_soul_config.team_name)

        self.ui_get_current_page()
        self.ui_goto(page_realm_raid)

        # 判断是不是锁定阵容
        self.ensure_lock(con.general_battle_config.lock_team_enable)

        # 开始循环
        success = True
        last_battle = True  # 记录上一次战斗的结果
        exit_all = False
        exit_four_enable = con.raid_config.exit_four  # 可被等级策略临时关掉，不写回配置
        # 等级策略（全退降级 / 禁退四升级）只服务于 auto_exit_all 选项：
        # 没开就完全不识别等级、不做策略判定，退四保持 exit_four 配置的打九退四原语义
        auto_exit_all = con.raid_config.auto_exit_all
        # 更改循环顺序
        while 1:
            self.screenshot()
            #看到弹窗点掉，不然会卡死
            if self.appear(self.I_FRESH_ENSURE):
                logger.info("Pop-up detected: Refresh Confirmation. Clicking Confirm.")
                self.appear_then_click(self.I_FRESH_ENSURE, interval=1.5)
                continue
            # 检查票数
            if not self.check_ticket(con.raid_config.number_base):
                break
            # 挑战次数
            if self.current_count >= con.raid_config.number_attack:
                logger.info(f'Current count {self.current_count}, max count {con.raid_config.number_attack}')
                break
            # ----------------------------------------开始进攻
            # 一次识别喂给策略判定与目标选择，避免同一帧重复跑模板匹配；
            # 没开 auto_exit_all 时连逐格等级 OCR 一起跳过
            cells = self.detect_cells(False, read_level=auto_exit_all)
            # 按九格等级与总勋章数决定是否全退降级 / 是否禁用退四提升等级（仅 auto_exit_all 开启时）
            if auto_exit_all:
                new_exit_all, allow_exit_four = self.decide_exit_strategy(cells)
                if new_exit_all is not None:
                    exit_all = new_exit_all
                if allow_exit_four is not None:
                    exit_four_enable = allow_exit_four and con.raid_config.exit_four
            index = self.find_one(False, cells=cells)
            if not index:
                # 已经没有可以挑战的了，只能刷新
                if con.raid_config.when_attack_fail == WhenAttackFail.CONTINUE:
                    logger.info('No one can attack and then refresh')
                    if self.check_refresh():
                        exit_all = False
                        continue
                    else :
                        self.set_next_run(task='RealmRaid', target=datetime.now() + timedelta(minutes=5))
                        raise TaskEnd('RealmRaid')
                else:
                    logger.info('No one can attack, break')
                    # 检查是否有“刷新确认”弹窗挡路
                    if self.appear(self.I_FRESH_ENSURE):
                        logger.info("Closing obstructing refresh dialog (Click Ensure)...")
                        # 点击“确定”来完成刷新（或者你可以改成点取消）
                        self.appear_then_click(self.I_FRESH_ENSURE, interval=2)
                    success = False
                    break
            # 完整九格中只剩当前目标未击破时退四，与目标位置编号无关。
            remaining = [cell for cell in cells if cell['state'] != CELL_FINISHED]
            is_last_target = (
                len(cells) == GRID_ROWS * GRID_COLS
                and len(remaining) == 1
                and remaining[0]['index'] == index
            )
            # exit_all 只在 auto_exit_all 开启时才可能为 True，此处无需再判一次
            if is_last_target and not exit_all:
                logger.info(f'Last undefeated realm at position {index}')
                if exit_four_enable:
                    logger.info('Exit four enable')
                    self.fire(index)
                    self.run_general_battle_back(con.general_battle_config, exit_four=True)
                    self.fire(index)
                    self.run_general_battle_back(con.general_battle_config, exit_four=True)
                    self.fire(index)
                    self.run_general_battle_back(con.general_battle_config, exit_four=True)
                    self.fire(index)
                    self.run_general_battle_back(con.general_battle_config, exit_four=True)
            self.fire(index)
            # 全退模式（auto_exit_all 判定等级过高）：进战斗立即退出，逐格清场降级
            if exit_all:
                logger.info('Exit all')
                self.run_general_battle_back(con.general_battle_config, exit_four=True)
                last_battle = False
            else:
                last_battle = self.run_general_battle(con.general_battle_config)
            # 检查是否每三次领一个奖励
            if self.reward_detect_click(False):
                logger.info('Rewards of three wins')
                continue
            # 刷新 >> 如果勾选了三次刷新并且到达了三次，就刷新
            if con.raid_config.three_refresh and self.appear(self.I_RR_THREE, threshold=0.8):
                logger.info('Three refresh')
                if self.check_refresh():
                    continue
                else:
                    success = False
                    break
            # 刷新 >> 如果上一轮的失败并且勾选了失败刷新，就刷新
            if not last_battle and con.raid_config.when_attack_fail == WhenAttackFail.REFRESH:
                logger.info('Battle lost and then refresh')
                if self.check_refresh():
                    continue
                else:
                    success = False
                    break
            # 如果上一轮失败 -> 退出
            if not last_battle and con.raid_config.when_attack_fail == WhenAttackFail.EXIT:
                logger.info('Battle lost and exit')
                break


        self.ui_click(self.I_BACK_RED, self.I_CHECK_EXPLORATION)
        self.ui_get_current_page()
        self.ui_goto(page_main)
        
        # 设置RealmRaid下次运行时间
        self.set_next_run(task='RealmRaid', success=success, finish=True)
        
    
        raise TaskEnd('RealmRaid')








    # ----------------------------------------------------------------------------------------------------------------------
    # 2023.7.21 改版个人突破

    def ensure_lock(self, lock_team_enable: bool):
        """
        确保锁定阵容
        :param lock_team_enable:
        :return:
        """
        if lock_team_enable:
            while 1:
                logger.info('Check lock: %s', lock_team_enable)
                self.screenshot()
                if self.appear_then_click(self.I_UNLOCK, interval=1):
                    continue
                if self.appear(self.I_LOCK):
                    break
            logger.info(f'Click {self.I_UNLOCK.name}')
        else:
            while 1:
                self.screenshot()
                if self.appear_then_click(self.I_LOCK, interval=1):
                    continue
                if self.appear(self.I_UNLOCK):
                    break
            logger.info(f'Click {self.I_LOCK.name}')

    def check_ticket(self, base: int=0) -> bool:
        """
        检查是不是有票， 检查这个票是否大于等于基准
        :param base:
        :return:
        """
        if base < 0 or base > 30:
            logger.warning(f'It is not a valid base {base}')
            base = 0
        self.wait_until_appear(self.I_BACK_RED)
        self.screenshot()
        cu, res, total = self.O_NUMBER.ocr(self.device.image)

        if total == 0:
            self.reward_detect_click(True)
            # 增加出现聊天框遮挡，处理奖励之后，重新识别票数
            cu, res, total = self.O_NUMBER.ocr(self.device.image)
        if cu == 0 and cu + res == total:
            logger.warning(f'Execute raid failed, no ticket')
            return False
        elif cu + res == total and cu < base:
            logger.warning(f'Execute raid failed, ticket is not enough')
            return False
        return True

    @cached_property
    def attack_by_position(self) -> bool:
        """order_attack 填 position 时按格号顺序进攻（格 1 → 格 9，从简单打到难）。"""
        return self.config.realm_raid.raid_config.order_attack.strip().lower() == ORDER_BY_POSITION

    @cached_property
    def attack_order(self) -> list:
        """解析 order_attack 配置，返回进攻优先的勋章数（星级）列表。

        实心勋章数等于结界星级，按勋章数排序即星级优先。
        """
        order_attack = self.config.realm_raid.raid_config.order_attack
        support_number = [0, 1, 2, 3, 4, 5]
        order = order_attack.replace(' ', '').replace('\n', '')
        order = [int(i) for i in re.split(r'>', order) if i.strip().isdigit()]
        order = [i for i in order if i in support_number]
        if not order:
            logger.warning(f'Invalid order_attack [{order_attack}], fallback to 5>4>3>2>1>0')
            order = [5, 4, 3, 2, 1, 0]
        return order

    @staticmethod
    def _match_in_region(rule: RuleImage, image, region: tuple, threshold: float=None) -> bool:
        """在指定区域内做一次模板匹配。

        base_task.appear() 不支持指定区域，这里直接临时改写 roi_back；资产是跨任务
        共享的类属性，所以必须在 finally 里还原，否则第一次分格匹配就把资产污染成那一格。
        """
        saved = rule.roi_back
        try:
            rule.roi_back = list(region)
            return rule.match(image, threshold=threshold)
        finally:
            rule.roi_back = saved

    def _slot_hits(self, image) -> list:
        """全图各跑一次 I_MEDAL / I_NO_MEDAL，返回全部勋章槽位命中点。

        :return: [(x, y, kind)]，kind 为 'M'（实心）或 'N'（空槽）
        """
        full_roi = [0, 0, 1280, 720]
        hits = []
        for rule, kind in ((self.I_MEDAL, 'M'), (self.I_NO_MEDAL, 'N')):
            # match_all_any 会改写 roi_back，而资产是跨任务共享的类属性，用完必须还原
            saved = rule.roi_back
            try:
                matches = rule.match_all_any(image, roi=list(full_roi))
            finally:
                rule.roi_back = saved
            hits += [(int(x), int(y), kind) for (_, x, y, _, _) in matches]
        return hits

    def detect_grid(self, hits: list, rows_expected: int=GRID_ROWS, cols_expected: int=GRID_COLS,
                    fallback_x: tuple=FALLBACK_SLOT_X, fallback_y: tuple=FALLBACK_SLOT_Y) -> list:
        """从勋章槽位命中点反推网格锚点。

        槽位是刚性网格，用命中点聚类出的行/列锚点比硬编码坐标更贴合实际画面。
        行列数与期望一致时用实测锚点；列表类界面（寮突破）滚动截断后行数不足，
        不做补齐——识别到几行就处理几行，截断行没有可信信息；仅当列数不符
        （连列都定不了）时才回退到写死的基线锚点。
        :return: [(slot_x, slot_y)] 首槽左上角坐标，次序为从左到右、从上到下
        """
        rows = _cluster([y for _, y, _ in hits], ROW_CLUSTER_GAP)
        cols = _cluster([x for x, _, _ in hits], COL_CLUSTER_GAP)
        if len(cols) == cols_expected and len(rows) <= rows_expected:
            slot_y = [min(r) for r in rows]
            slot_x = [min(c) for c in cols]
            return [(x, y) for y in slot_y for x in slot_x]
        logger.warning(f'Grid detect failed (rows={len(rows)}, cols={len(cols)}), fallback to preset anchors')
        slot_y = list(fallback_y)
        slot_x = list(fallback_x)
        return [(x, y) for y in slot_y for x in slot_x]

    def detect_cells(self, screenshot: bool=True, rows_expected: int=GRID_ROWS,
                     cols_expected: int=GRID_COLS,
                     fallback_x: tuple=FALLBACK_SLOT_X, fallback_y: tuple=FALLBACK_SLOT_Y,
                     read_level: bool=True, slot_hits: list=None) -> list:
        """逐格判定网格状态、统计勋章数并读取结界等级。

        状态优先级：已攻破 > 攻打失败 > 可正常攻打。已攻破的格子勋章数没有意义，
        不做统计（印章还会遮掉第 5 槽，统计出来也是残缺的）。
        网格形状由参数决定：个人突破 3×3、寮突破 4×2（捕获截图实测同一套勋章模板可用）；
        滚动截断时行数可能少于期望，识别到几行就返回几行。
        read_level=False 时跳过逐格等级 OCR（level 置 None）：等级只服务于
        auto_exit_all 的全退/禁退四策略，不消费等级的调用方不该付这笔 OCR 成本。
        slot_hits 传入已跑过的勋章槽命中点可跳过全图匹配（调用方做过画面预检时复用）。
        :return: 格子 dict 列表，含 index/slot/region/state/medal_count/no_medal_count/level/click_roi
        """
        if screenshot:
            self.screenshot()
        image = self.device.image
        hits = slot_hits if slot_hits is not None else self._slot_hits(image)
        cells = []
        for index, (slot_x, slot_y) in enumerate(self.detect_grid(hits, rows_expected, cols_expected,
                                                                  fallback_x, fallback_y), start=1):
            gx, gy = slot_x - SLOT_TO_CELL_DX, slot_y - SLOT_TO_CELL_DY
            region = (gx, gy, CELL_W, CELL_H)
            finished = self._match_in_region(self.I_FINISH_SIGN, image, region)
            failed = self._match_in_region(self.I_FAILURE_SIGN, image, region,
                                           threshold=FAILURE_SIGN_THRESHOLD)
            # 落在本格槽位带内的命中点：y 同行、x 从首槽起覆盖 5 槽
            band = [h for h in hits
                    if abs(h[1] - slot_y) < ROW_CLUSTER_GAP
                    and slot_x - 20 <= h[0] < slot_x + SLOTS_PER_CELL * SLOT_GAP]
            medal_count = sum(1 for h in band if h[2] == 'M')
            no_medal_count = len(band) - medal_count
            if finished:
                state = CELL_FINISHED
            elif failed:
                state = CELL_FAILED
            else:
                state = CELL_ATTACKABLE
            # 一致性校验：有印章应 4 槽、无印章应 5 槽。只告警不改判定，
            # 印章是直接信号，槽数是间接信号，矛盾时以印章为准。
            expect = SLOTS_PER_CELL - 1 if finished else SLOTS_PER_CELL
            if len(band) != expect:
                logger.warning(f'Cell {index} slot count {len(band)} != expect {expect} (state={state})')
            # 点击区：左边界取第 2 槽起点以避开式神头像与首槽，其余三边内缩
            click_x = int(slot_x + SLOT_GAP)
            click_roi = (click_x, gy + CLICK_INSET,
                         gx + CELL_W - CLICK_INSET - click_x, CELL_H - 2 * CLICK_INSET)
            cells.append({
                'index': index,
                'slot': (slot_x, slot_y),
                'region': region,
                'state': state,
                'medal_count': medal_count,
                'no_medal_count': no_medal_count,
                'level': self._read_level(image, slot_x, slot_y) if read_level else None,
                'click_roi': click_roi,
            })
        return cells

    @staticmethod
    def _read_level(image, slot_x: int, slot_y: int) -> int:
        """读取格内式神头像左上角的结界等级。

        ROI 锚定勋章反推的槽位锚点（与 detect_grid 同源），OCR 失败或异常返回 0（视为无效）。
        """
        roi = (slot_x + LEVEL_ROI_DX, slot_y + LEVEL_ROI_DY, LEVEL_ROI_W, LEVEL_ROI_H)
        rule = RuleOcr(roi=roi, area=(0, 0, 100, 100), mode='Digit',
                       method='Default', keyword='', name='realm_level')
        try:
            return int(rule.ocr(image))
        except Exception as e:
            logger.warning(f'Read realm level failed at {roi}: {e}')
            return 0

    def decide_exit_strategy(self, cells: list) -> tuple:
        """按九格结界等级与总勋章数决定是否全退 / 是否禁用退四。

        只在九格全为可攻打态时判定：有攻破或失败格时勋章统计不完整（印章会遮掉第 5 槽），
        等级也可能因为界面变化读不准，此时不做任何策略调整。
        等级有效性用两重校验：9 格全部读到数值，且全部落在 [55, 60]——结界等级带宽
        不超过 5 级，出现 60 级时不可能同时存在 55 级以下，低于 55 的一定是 OCR 误读。
        :return: (exit_all, allow_exit_four)，None 表示该项维持调用方的现值
        """
        if any(cell['state'] != CELL_ATTACKABLE for cell in cells):
            return None, None
        levels = [cell['level'] for cell in cells]
        invalid = [lv for lv in levels if not LEVEL_VALID_MIN <= lv <= LEVEL_VALID_MAX]
        if invalid:
            logger.warning(f'Realm levels {levels} contain invalid values, skip exit strategy')
            return None, None
        average = sum(levels) / len(levels)
        total_medal = sum(cell['medal_count'] for cell in cells)
        enough_medal = total_medal >= MEDAL_TOTAL_ENOUGH
        logger.info(f'Realm levels {levels}, average {average:.2f}, total medal {total_medal}')
        # 等级过高：直接全退降级，不看勋章数
        if average > LEVEL_FORCE_EXIT_ALL:
            logger.info(f'Average level {average:.2f} > {LEVEL_FORCE_EXIT_ALL}, exit all to lower level')
            return True, True
        # 等级偏高且勋章不足：全退降级
        if average > LEVEL_EXIT_ALL and not enough_medal:
            logger.info(f'Average level {average:.2f} > {LEVEL_EXIT_ALL} and medal {total_medal} '
                        f'< {MEDAL_TOTAL_ENOUGH}, exit all to lower level')
            return True, True
        # 等级不高且勋章不足：禁用退四，靠正常挑战把结界等级提上去
        if average <= LEVEL_NO_EXIT_FOUR and not enough_medal:
            logger.info(f'Average level {average:.2f} <= {LEVEL_NO_EXIT_FOUR} and medal {total_medal} '
                        f'< {MEDAL_TOTAL_ENOUGH}, disable exit four to raise level')
            return False, False
        return False, True

    @property
    def partition(self) -> list[RuleClick]:
        """九宫格点击落点，按当前画面反推的锚点动态生成。

        每次取用都重新反推：fire 里清弹窗可能改变画面，用旧帧的锚点会点到错格。
        代价只有两次全图模板匹配，比点错目标便宜得多。
        name 沿用 partition_N，保持 device 层连点判重与日志 key 与改造前一致。
        """
        # 只消费 click_roi，跳过逐格等级 OCR
        return [RuleClick(roi_front=cell['click_roi'], roi_back=cell['click_roi'],
                          name=f'partition_{cell["index"]}')
                for cell in self.detect_cells(read_level=False)]

    def find_one(self, screenshot: bool=True, cells: list=None) -> int:
        """
        找到一个可以打的格子，返回格号
        我们约定次序是：从左到右 上到下
        1 2 3
        4 5 6
        7 8 9
        已攻破与攻打失败的格子都跳过（是否重打失败格不再按 when_attack_fail 分支区分，
        统一跳过；该配置仍决定「没有可打目标之后」是刷新还是退出）。
        :param cells: 已识别好的九宫格结果，传入可避免同一帧重复识别
        :return: 可攻打格子的格号（1~9），没有找到返回 None
        """
        if cells is None:
            cells = self.detect_cells(screenshot)
        for cell in cells:
            if cell['state'] != CELL_ATTACKABLE:
                logger.info(f'Position {cell["index"]} is {cell["state"].lower()}, skip')
        candidates = [c for c in cells if c['state'] == CELL_ATTACKABLE]
        if not candidates:
            return None
        if self.attack_by_position:
            # 按格号顺序进攻：格 1 → 格 9，从简单打到难
            target_cell = candidates[0]
        else:
            # 按 order_attack 的星级优先级挑目标，同星级时取靠前的格子
            target_cell = None
            for medal_count in self.attack_order:
                for cell in candidates:
                    if cell['medal_count'] == medal_count:
                        target_cell = cell
                        break
                if target_cell:
                    break
            if not target_cell:
                return None
        total = target_cell['medal_count'] + target_cell['no_medal_count']
        logger.info(f'Find one medal {target_cell["medal_count"]}/{total}, '
                    f'order is {target_cell["index"]}')
        return target_cell['index']

    def reward_detect_click(self, screenshot: bool=True) -> bool:
        """
        检测是否出现 每三次就有奖励的界面, 有就领取
        :return:
        """
        if screenshot:
            self.screenshot()
        # 由于更改识别顺序，退出战斗之后，需要先等待回到个人突破界面，即识别到红色退出按钮，再进行奖励判断
        self.wait_until_appear(self.I_BACK_RED)
        text = self.O_TEXT.ocr(self.device.image)
        # 识别突破卷区域，如果识别到了且其中含有文字，即有聊天框遮挡则进入循环，等待三胜奖励出现并点击，循环退出条件为识别到票（即*/*的形式）
        if text != "":
            if re.search(r'[\u4e00-\u9fff]', text):
                while 1:
                    self.screenshot()
                    result = self.O_TEXT.ocr(self.device.image)
                    if not re.search(r'[\u4e00-\u9fff]', result) and re.search(r'(\d+)/(\d+)', result):
                        return True
                    if self.appear_then_click(self.I_SOUL_RAID, interval=1.5):
                        continue

        # if self.appear(self.I_SOUL_RAID):
        #     self.screenshot()
        #     # 稳定一次的截图时间
        #     # 再次判断是否出现的
        #     if not self.appear(self.I_SOUL_RAID):
        #         return False
        #     while 1:
        #         self.screenshot()
        #         if not self.appear(self.I_SOUL_RAID, threshold=0.7):
        #             return True
        #         if self.appear_then_click(self.I_SOUL_RAID, interval=1.5):
        #             continue

    def check_refresh(self, screenshot: bool=True) -> bool:
        """
        检查是否出现了刷新的按钮
        如果可以刷新就刷新，返回True
        如果在CD中，就返回False
        :return:
        """
        if screenshot:
            self.screenshot()
        if not self.appear(self.I_FRESH):
            logger.info(f'No find refresh button and it is in CD')
            return False
        while 1:
            self.screenshot()
            if self.appear(self.I_FRESH_ENSURE):
                break
            if self.appear_then_click(self.I_FRESH, interval=1):
                continue
        while 1:
            self.screenshot()
            if not self.appear(self.I_FRESH_ENSURE):
                return True
            if self.appear_then_click(self.I_FRESH_ENSURE, interval=1):
                continue

    def fire(self, order: int):
        """
        挑战；列表标签失配时复核页面，超时交给现有卡死恢复流程。
        :param order: 第几个
        """
        deadline = time.monotonic() + 30
        list_confirmations = 0
        while time.monotonic() < deadline:
            self.screenshot()
            person_found = self.appear(self.I_RR_PERSON)
            on_list = not person_found and self.ui_page_appear(page_realm_raid)

            # 仅处理明确识别的弹窗或奖励，缺失标签不作为清理依据。
            if self.appear(self.I_FRESH_ENSURE):
                self.device.click(x=530, y=460)
                list_confirmations = 0
                time.sleep(1.5)
                continue
            if self.appear(self.I_SOUL_RAID):
                self.appear_then_click(self.I_SOUL_RAID, interval=1.5)
                list_confirmations = 0
                continue
            if not on_list and self.appear(self.I_REWARD):
                self.device.click(x=640, y=50)
                list_confirmations = 0
                time.sleep(1.5)
                continue

            if person_found:
                break
            if on_list:
                # 连续两次重新取帧确认列表，保留原阈值并继续正常攻击。
                list_confirmations += 1
                if list_confirmations >= 2:
                    break
            else:
                list_confirmations = 0
            time.sleep(0.5)
        else:
            raise GameStuckError(f'RealmRaid target {order}: list confirmation timed out')

        # 弹窗清理后重新反推目标点击区，保持原目标选择与点击间隔。
        click = self.partition[order - 1]
        while time.monotonic() < deadline:
            self.screenshot()
            if self.appear(self.I_FRESH_ENSURE):
                self.device.click(x=530, y=460)
                time.sleep(1.0)
                continue

            # 标签消失不能证明进攻成功；由准备页或真实战斗页确认。
            if self.is_in_prepare(False) or self.is_in_real_battle(False):
                logger.info(f'Click fire {order} success')
                return

            if self.appear(self.I_FIRE):
                self.appear_then_click(self.I_FIRE, interval=1)
                continue
            if self.appear(self.I_RR_PERSON) or self.ui_page_appear(page_realm_raid):
                if self.click(click, interval=1.8):
                    continue
            time.sleep(0.3)

        raise GameStuckError(f'RealmRaid target {order}: attack timed out')

if __name__ == "__main__":
    from module.config.config import Config
    from module.device.device import Device
    config = Config('oas1')
    device = Device(config)
    t = ScriptTask(config, device)

    t.run()
