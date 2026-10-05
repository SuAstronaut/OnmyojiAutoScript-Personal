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
from tasks.RealmRaid.settlement_safe import BASE_SAFE_AREA, get_settlement_safe_point
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
from module.ocr.base_ocr import enlarge_canvas


# ----------------------------------------------------------------------------------------------------------------------
# 结界突破九宫格：勋章槽位刚性网格的实测参数（1280×720 授权空间）
# 实测三张截图确认：行锚点 y=211/346/481、列锚点 x=242/574/906、槽距 39.4px、每格恒 5 槽。
# 攻破印章（I_FINISH_SIGN，x=406/738/1070 宽 48）恰好压在第 5 槽（外推 x=401/733/1065）上，
# 模板漏匹配也会减少命中数；槽数只辅助判断可信度，不能反推完成/失败状态。
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

    def battle_wait(self, random_click_swipt_enable: bool) -> bool:
        """Keep the parent state machine; scope direct continue-click routing."""
        previous = getattr(self, '_realm_settlement_active', False)
        self._realm_settlement_active = True
        try:
            return super().battle_wait(random_click_swipt_enable)
        finally:
            self._realm_settlement_active = previous

    def run_general_battle_back(self, config=None, exit_four=False) -> bool:
        # Retreat / confirm / prepare remain precise. Only the resulting FALSE
        # screen's continue click uses the settlement mask, once per fresh frame.
        previous = getattr(self, '_realm_settlement_active', False)
        self._realm_settlement_active = True
        try:
            return super().run_general_battle_back(config, exit_four=exit_four)
        finally:
            self._realm_settlement_active = previous

    def _get_settlement_safe_point(self):
        # User-confirmed shared breakthrough reward layout, full 1280 x 720.
        if self.device.image.shape[:2] != (720, 1280):
            logger.error('[RealmRaid] settlement_safe_click unsupported frame size')
            raise RuntimeError('RealmRaid settlement safe click requires 1280 x 720')
        try:
            return get_settlement_safe_point()
        except RuntimeError:
            logger.error('[RealmRaid] settlement_safe_click sampling exhausted')
            raise

    def _log_settlement_safe_point(self, state, x, y):
        logger.info(f'[RealmRaid] settlement_safe_click state={state} '
                    f'x={x} y={y} strategy=uniform_mask')

    def _click_settlement_safe_area(self, state, control_name, pace=True):
        x, y = self._get_settlement_safe_point()
        self._log_settlement_safe_point(state, x, y)
        self.device.click(x, y, control_name=control_name, pace=pace)

    def appear_then_click(self, target, action=None, interval=None, threshold=None,
                          duration=None, repeat_exempt=False):
        active = getattr(self, '_realm_settlement_active', False)
        if active and target is self.I_CONFIRM_CLOSE_DIFF_SOUL:
            # The parent's unknown-reward branch may have just clicked blank
            # space. Re-read the screen before its separate precise confirmation.
            self.screenshot()
        if (active and action is None and duration is None
                and (target is self.I_FALSE or target is self.I_DE_WIN)):
            if not self._settlement_cd_ready():
                return False
            if not self.appear(target, interval=interval, threshold=threshold):
                return False
            state = 'false' if target is self.I_FALSE else 'win'
            self._click_settlement_safe_area(state, target.name)
            self._settlement_cd_touch()
            return True
        return super().appear_then_click(
            target, action=action, interval=interval, threshold=threshold,
            duration=duration, repeat_exempt=repeat_exempt)

    def click(self, click=None, interval=None) -> bool:
        if (getattr(self, '_realm_settlement_active', False)
                and click is self.C_RANDOM_CLICK and interval is None):
            # Route only the parent's identified unknown-reward continuation.
            # Battle-process random gestures keep their existing behavior.
            if (self.appear(self.I_STATISTICS) and not self.win_appear()
                    and not self.appear(self.I_REWARD)
                    and not self.reward_grid_appear()):
                return self.settlement_click(self.I_STATISTICS, None, interval=interval)
        return super().click(click, interval=interval)

    def settlement_gesture(self, action, control_name='Reward') -> None:
        previous = getattr(self, '_realm_settlement_state', 'reward')
        self._realm_settlement_state = (
            'win' if control_name in self.win_template_names() else 'reward')
        try:
            super().settlement_gesture(action, control_name=control_name)
        finally:
            self._realm_settlement_state = previous

    def _settlement_point(self, action):
        x, y = self._get_settlement_safe_point()
        self._log_settlement_safe_point(self._realm_settlement_state, x, y)
        left, top, right, bottom = BASE_SAFE_AREA
        roi = (left, top, right - left + 1, bottom - top + 1)
        return x, y, RuleClick(roi, roi, name='realm_settlement_safe')

    def settlement_click_count(self, page_clicks: int) -> int:
        return 1

    def _settlement_extra_clicks(self, action, x, y, control_name,
                                 first_ts=None, page_clicks=0):
        # Parent records first_ts as the last click when this returns None.
        return None

    def run(self):
        self.run_2()

    def screenshot(self):
        # 限时只作用于本任务退四的进入/准备设置；正常 battle 不受此期限影响。
        self._realm_exit_four_checkpoint()
        image = super().screenshot()
        self._realm_exit_four_checkpoint()
        return image

    def _realm_exit_four_identity(self):
        return (id(self.config), getattr(self.config, 'config_name', None),
                getattr(self.config, 'generation', None), getattr(self.config, 'task', None))

    def _realm_exit_four_checkpoint(self):
        context = getattr(self, '_realm_exit_four_context', None)
        if context is None:
            return
        if context['identity'] != self._realm_exit_four_identity():
            raise GameStuckError('RealmRaid exit four ABORT: task/config changed')
        deadline = context.get('deadline')
        if deadline is not None and time.monotonic() >= deadline:
            raise GameStuckError(f"RealmRaid exit four ABORT: timeout at {context['state']}")

    def _realm_retry_text(self, rule):
        """Quiet OCR of one dedicated ROI, returning unique full-frame boxes."""
        if self.device.image.shape[:2] != (720, 1280):
            raise GameStuckError('RealmRaid retry requires 1280 x 720')
        roi_x, roi_y, roi_w, roi_h = rule.roi
        cropped = enlarge_canvas(rule.pre_process(rule.crop(self.device.image, rule.roi)))
        result = {}
        # Use the existing model API without BaseCor's per-frame full-text log.
        for item in rule.model.detect_and_ocr(cropped):
            text = re.sub(r'[\s？?。！!]', '', rule.after_process(item.ocr_text))
            if item.score < 0.90 or text not in (
                    '再次挑战', '再次战斗', '重新战斗', '是否再次挑战', '确定'):
                continue
            xs, ys = [p[0] for p in item.box], [p[1] for p in item.box]
            left, top, right, bottom = int(min(xs)), int(min(ys)), int(max(xs)), int(max(ys))
            if not (0 <= left < right <= roi_w and 0 <= top < bottom <= roi_h):
                continue
            result.setdefault(text, []).append((roi_x+left, roi_y+top, right-left, bottom-top))
        # Ambiguous labels are rejected, rather than choosing the first box.
        return {text: boxes[0] if len(boxes) == 1 else None for text, boxes in result.items()}

    def _realm_retry_click(self, roi, name):
        x, y, w, h = roi
        # Click inside the recognized label, avoiding its OCR edges.
        inset = (x + w//4, y + h//4, max(1, w//2), max(1, h//2))
        self.click(RuleClick(inset, inset, name=name))

    def _realm_prepare_visible(self):
        if self.is_in_real_battle(False):
            raise GameStuckError('RealmRaid exit four ABORT: unexpected real battle')
        return (not self.appear(self.I_FALSE) and not self.appear(self.I_EXIT_ENSURE)
                and self.is_in_prepare(False))

    def _realm_wait(self, state, predicate, timeout=30):
        deadline = time.monotonic() + timeout
        context = getattr(self, '_realm_exit_four_context', None)
        if context is not None:
            context.update(state=state, deadline=deadline)
        while time.monotonic() < deadline:
            self.screenshot()
            if predicate():
                return
        raise GameStuckError(f'RealmRaid exit four ABORT: timeout at {state}')

    def _exit_prepare_once(self):
        """S1 -> S2 -> S3 -> S4. Keep the confirmed defeat screen open."""
        def prepare_exit_available():
            if self.is_in_real_battle(False):
                raise GameStuckError('RealmRaid exit four ABORT: unexpected real battle')
            if self.appear(self.I_FALSE):
                raise GameStuckError('RealmRaid exit four ABORT: unexpected defeat before prepare exit')
            if self.appear(self.I_EXIT_ENSURE):
                raise GameStuckError('RealmRaid exit four ABORT: unexpected exit confirmation before prepare exit')
            # fire/retry can see prepare markers before the exit control loads.
            # Require both on the same fresh frame before sending any exit input.
            return self.is_in_prepare(False) and self.appear(self.I_EXIT)

        try:
            self._realm_wait('prepare', prepare_exit_available, timeout=8)
        except GameStuckError as exc:
            # The scoped screenshot guard may also report this exact deadline.
            # Preserve real-battle/device/config errors without relabeling them.
            if str(exc) != 'RealmRaid exit four ABORT: timeout at prepare':
                raise
            raise GameStuckError('RealmRaid exit four ABORT: missing prepare exit') from exc
        self.click(self.I_EXIT)

        def exit_confirmation():
            if self.is_in_real_battle(False):
                raise GameStuckError('RealmRaid exit four ABORT: unexpected real battle')
            return self.appear(self.I_EXIT_ENSURE) and not self.appear(self.I_FALSE)

        self._realm_wait('exit_confirmation', exit_confirmation)
        self.click(self.I_EXIT_ENSURE)

        def new_defeat():
            if self.is_in_real_battle(False):
                raise GameStuckError('RealmRaid exit four ABORT: unexpected real battle')
            return (self.appear(self.I_FALSE) and not self.appear(self.I_EXIT_ENSURE)
                    and not self.is_in_prepare(False))

        self._realm_wait('new_defeat', new_defeat)

    def _retry_same_target(self):
        """S4 -> optional retry confirmation -> fresh prepare, including round four."""
        def retry_button():
            if self.is_in_real_battle(False):
                raise GameStuckError('RealmRaid exit four ABORT: unexpected real battle')
            if not self.appear(self.I_FALSE) or self.appear(self.I_EXIT_ENSURE):
                return False
            texts = self._realm_retry_text(self.O_REALM_RETRY_BUTTON)
            buttons = [texts[label] for label in ('再次挑战', '再次战斗', '重新战斗')
                       if label in texts]
            if '是否再次挑战' in texts or len(buttons) != 1 or buttons[0] is None:
                return False
            if '是否再次挑战' in self._realm_retry_text(self.O_REALM_RETRY_DIALOG):
                return False
            logger.info('RealmRaid retry button detected confidence>=0.90')
            self._realm_retry_click(buttons[0], 'realm_retry')
            return True

        self._realm_wait('retry_button', retry_button)
        confirmed = False
        dialog_logged = False
        no_remind_clicked_at = None
        no_remind_last_state = None

        def retry_destination():
            nonlocal confirmed, dialog_logged, no_remind_clicked_at, no_remind_last_state
            if self.is_in_real_battle(False):
                raise GameStuckError('RealmRaid exit four ABORT: unexpected real battle')
            # A direct prepare transition needs no OCR at all.
            if self._realm_prepare_visible():
                return True
            texts = self._realm_retry_text(self.O_REALM_RETRY_DIALOG)
            if '是否再次挑战' in texts:
                if not dialog_logged:
                    logger.info('RealmRaid retry dialog detected')
                    dialog_logged = True
                title = texts['是否再次挑战']
                button = self._realm_retry_text(self.O_REALM_RETRY_CONFIRM).get('确定')
                if not confirmed and title and button and button[1] >= title[1] + title[3]:
                    state = self._realm_retry_no_remind_state()
                    if state != no_remind_last_state:
                        logger.info(f'RealmRaid retry no-remind state={state}')
                        no_remind_last_state = state
                    if state == 'off' and no_remind_clicked_at is None:
                        self._realm_retry_click(tuple(self.I_REALM_RETRY_NO_REMIND_OFF.roi_front),
                                                'realm_retry_no_remind')
                        no_remind_clicked_at = time.monotonic()
                        return False  # Confirm ON on a fresh screenshot before confirming.
                    if no_remind_clicked_at is not None and state != 'on':
                        if time.monotonic() - no_remind_clicked_at < 5:
                            return False
                        if state == 'off':
                            raise GameStuckError('RealmRaid retry no-remind ON not confirmed')
                        logger.warning('RealmRaid retry no-remind ON not confirmed; confirm only, no further toggle')
                    elif state == 'unknown':
                        logger.warning('RealmRaid retry no-remind state unknown; confirm only')
                    self._realm_retry_click(button, 'realm_retry_confirm')
                    confirmed = True
                return False
            return False

        self._realm_wait('retry_prepare', retry_destination)

    def _realm_retry_no_remind_state(self):
        off = self.appear(self.I_REALM_RETRY_NO_REMIND_OFF)
        on = self.appear(self.I_REALM_RETRY_NO_REMIND_ON)
        if bool(off) == bool(on):
            return 'unknown'
        return 'off' if off else 'on'

    def _realm_clear_reward(self):
        """Drain recognized personal-raid/normal settlement, never an unknown page."""
        if self.appear(self.I_SOUL_RAID):
            self.appear_then_click(self.I_SOUL_RAID, interval=1.5)
            return True
        for target, threshold in ((self.I_REWARD, 0.6), (self.I_REWARD_GOLD, 0.8),
                                  (self.I_EXTRA_INFO, None), (self.I_STATISTICS, None),
                                  (self.I_WIN, 0.8), (self.I_WIN_2, 0.8),
                                  (self.I_DE_WIN, 0.8), (self.I_FALSE, 0.8)):
            if self.appear(target, threshold=threshold):
                self.settlement_click(target, self.C_RANDOM_CLICK,
                                      interval=1.5, threshold=threshold)
                return True  # Even if click is cooling down, no grid scan yet.
        if self.reward_grid_appear():
            self.settlement_click_grid(self.C_RANDOM_CLICK, interval=1.5)
            return True
        return False

    def _realm_grid_slot_signature(self):
        """Visual grid precheck only: no fallback anchors, cell selection or level OCR."""
        hits = self._slot_hits(self.device.image)
        rows = _cluster([y for _, y, _ in hits], ROW_CLUSTER_GAP)
        cols = _cluster([x for x, _, _ in hits], COL_CLUSTER_GAP)
        if len(rows) != GRID_ROWS or len(cols) != GRID_COLS:
            return None
        xs, ys = [min(col) for col in cols], [min(row) for row in rows]
        counts = tuple(sum(abs(hy-y) < ROW_CLUSTER_GAP
                           and x-20 <= hx < x + SLOTS_PER_CELL*SLOT_GAP
                           for hx, hy, _ in hits) for y in ys for x in xs)
        if any(count == 0 for count in counts):
            return None
        return tuple(xs), tuple(ys), counts

    def _wait_realmraid_grid(self):
        """Positive, two-frame personal-grid gate after every kind of settlement."""
        previous = None
        handled_reward = False

        def stable_grid():
            nonlocal previous, handled_reward
            if self._realm_clear_reward():
                previous = None
                handled_reward = True
                return False
            if not self._realm_safe_grid():
                previous = None
                return False
            signature = self._realm_grid_slot_signature()
            stable = signature is not None and signature == previous
            previous = signature
            return stable

        self._realm_wait('stable_personal_grid', stable_grid)
        return handled_reward

    def _realm_safe_grid(self):
        # Reject overlaid target detail, rewards, settlement and any battle controls.
        if not (self.appear(self.I_CHECK_REALM_RAID) and self.appear(self.I_RR_PERSON)):
            return False
        if (self.is_in_real_battle(False) or self.is_in_prepare(False)
                or any(self.appear(target) for target in
                       (self.I_FIRE, self.I_FALSE, self.I_EXIT_ENSURE,
                        self.I_SOUL_RAID, self.I_FRESH_ENSURE, self.I_REWARD,
                        self.I_UI_CONFIRM, self.I_UI_CONFIRM_SAMLL))):
            return False
        return bool(self.appear(self.I_LOCK)) != bool(self.appear(self.I_UNLOCK))

    def _restore_realmraid_lock(self, expected, timeout=30):
        stable = 0
        clicked = False
        desired, opposite = (self.I_LOCK, self.I_UNLOCK) if expected else (self.I_UNLOCK, self.I_LOCK)

        def confirmed_grid_lock():
            nonlocal stable, clicked
            if not self._realm_safe_grid():
                stable = 0
                return False
            stable += 1
            if stable < 2:
                return False
            if self.appear(desired):
                return True
            if not clicked and self.appear(opposite):
                self.click(opposite)
                clicked = True
                stable = 0
            return False

        self._realm_wait('grid_lock', confirmed_grid_lock, timeout)

    def _finish_exit_four_battle(self, config, exit_count):
        if exit_count != 4:
            raise GameStuckError('RealmRaid exit four ABORT: invalid exit count')
        self._realm_wait('fifth_prepare', self._realm_prepare_visible)
        # The parent is entered after real battle, so preserve its first normal
        # battle's unlocked preset setup here, without changing persistent config.
        if not config.lock_team_enable and self.current_count == 0:
            context = self._realm_exit_four_context
            context.update(state='fifth_preset', deadline=time.monotonic() + 30)
            self.switch_preset_team(config.preset_enable, config.preset_group, config.preset_team)
            self.check_and_open_buff(None)
            self._realm_wait('fifth_prepare_after_preset', self._realm_prepare_visible)
        ready_clicked = False

        def started():
            nonlocal ready_clicked
            if self.is_in_real_battle(False):
                if not ready_clicked:
                    raise GameStuckError('RealmRaid exit four ABORT: battle started before ready')
                return True
            if self.appear(self.I_FALSE):
                raise GameStuckError('RealmRaid exit four ABORT: fifth preparation failed')
            if self.appear_then_click(self.I_DISABLE_7DAYS_DIFF_SOUL, interval=0.6):
                return False
            if self.appear_then_click(self.I_CONFIRM_CLOSE_DIFF_SOUL, interval=0.6):
                return False
            if (not ready_clicked and self._realm_prepare_visible()
                    and self.appear(self.I_PREPARE_HIGHLIGHT)):
                self.click(self.I_PREPARE_HIGHLIGHT)
                ready_clicked = True
            return False

        self._realm_wait('fifth_real_battle', started)
        self._realm_exit_four_context.update(state='normal_battle', deadline=None)
        # Only the original normal entry owns current_count += 1.
        return self.run_general_battle(config)

    def _run_exit_four_with_retry(self, index, config):
        # This context is task-local and always discarded. It is never resumed
        # from a FAILED stamp, an exception or a prior task's partial progress.
        context = dict(identity=self._realm_exit_four_identity(), state='unlock',
                       deadline=None, exit_count=0)
        self._realm_exit_four_context = context
        expected_lock = config.lock_team_enable
        try:
            self._restore_realmraid_lock(False)
            # Unlock confirmation takes fresh frames. If the grid changed in
            # that interval, cancel before entering a different last target.
            cells = self._detect_attack_cells(read_level=False)
            remaining = [cell for cell in cells if cell['state'] != CELL_FINISHED]
            if (len(cells) != GRID_ROWS * GRID_COLS or len(remaining) != 1
                    or remaining[0]['index'] != index):
                self._restore_realmraid_lock(expected_lock)
                return None
            context.update(state='first_fire', deadline=time.monotonic() + 30)
            if self.fire(index, revalidate=True) is False:
                self._restore_realmraid_lock(expected_lock)
                return None
            exit_count = 0
            while exit_count < 4:
                self._exit_prepare_once()
                # The only commit: exit confirmation was clicked and a new
                # defeat screenshot was observed (S3 -> S4), exactly once/round.
                exit_count += 1
                context['exit_count'] = exit_count
                logger.info(f'RealmRaid confirmed prepare exits: {exit_count}/4')
                self._retry_same_target()
            win = self._finish_exit_four_battle(config, exit_count)

            self._wait_realmraid_grid()
            self._restore_realmraid_lock(expected_lock)
            return win
        except Exception:
            logger.warning(f"RealmRaid exit four ABORT at {context['state']}; confirmed exits={context['exit_count']}")
            # Recovery never navigates away from settlement/unknown pages.
            try:
                context.update(state='safe_recovery', deadline=time.monotonic() + 5)
                self.screenshot()
                if self._realm_safe_grid():
                    self._restore_realmraid_lock(expected_lock, timeout=5)
            except Exception:
                logger.warning('RealmRaid lock recovery unavailable; next safe entry will correct it')
            raise
        finally:
            self._realm_exit_four_context = None

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
        self._restore_realmraid_lock(con.general_battle_config.lock_team_enable)

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
            cells = self._detect_attack_cells(read_level=auto_exit_all)
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
            # 安全入口补偿：上一任务被强停时也按当前配置重新校正锁定。
            self._restore_realmraid_lock(con.general_battle_config.lock_team_enable)
            after_exit_four = is_last_target and not exit_all and exit_four_enable
            if is_last_target and not exit_all:
                logger.info(f'Last undefeated realm at position {index}')
            if after_exit_four:
                last_battle = self._run_exit_four_with_retry(index, con.general_battle_config)
                if last_battle is None:  # 首次 fire 复核发现目标变化，尚未退出。
                    continue
            else:
                if self.fire(index, revalidate=True) is False:
                    continue
                # 全退与普通战斗继续使用原入口。
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

        状态优先级：已攻破 > 攻打失败 > 可正常攻打。已攻破格保留槽位命中数供诊断，
        但勋章数不参与普通候选选择（印章和模板漏匹配都可能使统计残缺）。
        网格形状由参数决定：个人突破 3×3、寮突破 4×2（捕获截图实测同一套勋章模板可用）；
        滚动截断时行数可能少于期望，识别到几行就返回几行。
        read_level=False 时跳过逐格等级 OCR（level 置 None）：等级只服务于
        auto_exit_all 的全退/禁退四策略，不消费等级的调用方不该付这笔 OCR 成本。
        slot_hits 传入已跑过的勋章槽命中点可跳过全图匹配（调用方做过画面预检时复用）。
        :return: 格子 dict 列表，含 index/slot/region/state/slot_count/medal_count/no_medal_count/level/click_roi
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
                'slot_count': len(band),
                'medal_count': medal_count,
                'no_medal_count': no_medal_count,
                'level': self._read_level(image, slot_x, slot_y) if read_level else None,
                'click_roi': click_roi,
            })
        return cells

    @staticmethod
    def _is_attack_candidate(cell: dict) -> bool:
        """印章决定状态；槽位仅排除完全无命中的强异常，不要求五槽齐全。"""
        return (cell['state'] == CELL_ATTACKABLE
                and cell['slot_count'] > 0)

    def _detect_attack_cells(self, read_level: bool=False) -> list:
        """有可信目标就继续；只剩零槽或网格不明确时有限重检。"""
        self._wait_realmraid_grid()
        for attempt in range(3):
            cells = self.detect_cells(attempt > 0, read_level=read_level)
            zero_slots = [cell['index'] for cell in cells
                          if cell['state'] == CELL_ATTACKABLE and cell['slot_count'] == 0]
            if len(cells) == GRID_ROWS * GRID_COLS:
                if any(self._is_attack_candidate(cell) for cell in cells) or not zero_slots:
                    return cells
            logger.warning(f'RealmRaid no trusted target or incomplete grid '
                           f'(cells={len(cells)}, zero_slots={zero_slots}), '
                           f'detection attempt {attempt + 1}/3')
            if attempt < 2:
                time.sleep(0.5)
        raise GameStuckError('RealmRaid unresolved zero-slot targets or grid after detection retries')

    def _validated_target_click(self, order: int, screenshot: bool=True,
                                allow_failed: bool=False):
        """仅在详情未出现、准备点击格子时复核；退四允许已选 FAILED 重入。"""
        cells = self.detect_cells(screenshot, read_level=False)
        target = next((cell for cell in cells if cell['index'] == order), None)
        allowed = target is not None and (
            self._is_attack_candidate(target)
            or (allow_failed and target['state'] == CELL_FAILED))
        if len(cells) != GRID_ROWS * GRID_COLS or not allowed:
            logger.info(f'RealmRaid target {order} changed or incomplete, cancel and reselect')
            return None
        return RuleClick(roi_front=target['click_roi'], roi_back=target['click_roi'],
                         name=f'partition_{order}')

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
            elif not self._is_attack_candidate(cell):
                logger.warning(f'Position {cell["index"]} has {cell["slot_count"]} slots, skip')
        candidates = [c for c in cells if self._is_attack_candidate(c)]
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
        """每三胜额外奖励也必须排空，并正向确认九宫格稳定。"""
        # Always read fresh frames; an old ticket OCR result cannot prove that
        # the additional settlement layer has disappeared.
        return self._wait_realmraid_grid()

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

    def fire(self, order: int, revalidate: bool=False):
        """
        挑战；列表标签失配时复核页面，超时交给现有卡死恢复流程。
        :param order: 第几个
        :param revalidate: 普通攻击复核；退四已选目标重入保留原 FAILED 路径
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
                self.settlement_click(self.I_REWARD, None)
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

        # 普通目标在真正点格子前复核；退四首次重入保留原点击路径。
        click = None if revalidate else self.partition[order - 1]
        partition_attempts = 0
        detail_wait_until = 0
        detail_seen = False
        fire_inputs_sent = 0
        list_return_frames = 0
        list_recoveries = 0
        recovery_target_evidence = ()
        while time.monotonic() < deadline:
            self.screenshot()
            if self.appear(self.I_FRESH_ENSURE):
                list_return_frames = 0
                recovery_target_evidence = ()
                self.device.click(x=530, y=460)
                time.sleep(1.0)
                continue

            # 标签消失不能证明进攻成功；由准备页或真实战斗页确认。
            if self.is_in_prepare(False) or self.is_in_real_battle(False):
                logger.info(f'Click fire {order} success')
                return

            if self.appear(self.I_FIRE):
                # 详情是正向证据；不再检测被详情面板遮挡的背景槽位。
                if not detail_seen:
                    detail_wait_until = max(detail_wait_until, time.monotonic() + 2.5)
                detail_seen = True
                list_return_frames = 0
                recovery_target_evidence = ()
                # 返回 True 只代表输入已发送，准备/战斗仍由上面的页面判据确认。
                if fire_inputs_sent < 2 and self.appear_then_click(self.I_FIRE, interval=1):
                    fire_inputs_sent += 1
                    detail_wait_until = time.monotonic() + 2.5
                else:
                    time.sleep(0.3)
                continue

            on_list = self.appear(self.I_RR_PERSON) or self.ui_page_appear(page_realm_raid)
            if detail_seen:
                # 单帧按钮消失/背景列表不触发恢复；等待后连续新帧确认返回列表。
                list_return_frames = list_return_frames + 1 if on_list else 0
                if not on_list:
                    recovery_target_evidence = ()
                if list_return_frames < 2 or time.monotonic() < detail_wait_until:
                    time.sleep(0.3)
                    continue
            elif time.monotonic() < detail_wait_until or partition_attempts >= 2:
                # 点击预算耗尽只禁止继续点格子，不缩短总响应等待期限。
                time.sleep(0.3)
                continue
            if on_list:
                if revalidate or partition_attempts or detail_seen:
                    # 无 RES_FIRE 才复核列表；第二次尝试绝不沿用旧点击区。
                    click = self._validated_target_click(
                        order, screenshot=False, allow_failed=not revalidate)
                    if detail_seen:
                        # 恢复还需连续帧给出相同落点/取消证据，单次漏识别不改阶段。
                        evidence = (click.roi_front if click is not None else None,)
                        if evidence != recovery_target_evidence:
                            recovery_target_evidence = evidence
                            time.sleep(0.3)
                            continue
                    if click is None:
                        if not revalidate:
                            raise GameStuckError(f'RealmRaid retreat target {order}: '
                                                 'state changed before reentry')
                        return False
                if detail_seen and (list_recoveries >= 1 or partition_attempts >= 2
                                    or fire_inputs_sent >= 2):
                    raise GameStuckError(f'RealmRaid target {order}: list recovery budget exhausted')
                if self.click(click, interval=1.8):
                    if detail_seen:
                        list_recoveries += 1
                    partition_attempts += 1
                    detail_seen = False
                    list_return_frames = 0
                    recovery_target_evidence = ()
                    detail_wait_until = time.monotonic() + 2.5
                    logger.info(f'RealmRaid target {order}: partition attempt '
                                f'{partition_attempts}/2, wait for RES_FIRE')
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
