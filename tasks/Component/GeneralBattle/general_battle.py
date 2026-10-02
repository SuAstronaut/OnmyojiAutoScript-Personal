# This Python file uses the following encoding: utf-8
# @author runhey
# github https://github.com/runhey
import time
import math
import random
import re
from time import sleep

import cv2
from module.base.timer import Timer

from module.base.utils import get_color, color_similar
from tasks.base_task import BaseTask
from tasks.Component.GeneralInvite.assets import GeneralInviteAssets
from tasks.Component.GeneralBattle.config_general_battle import (
    GreenMarkType, GeneralBattleConfig, parse_segment_range)
from tasks.Component.GeneralBattle.assets import GeneralBattleAssets
from tasks.Component.GeneralBattle.reward_frame import (
    safe_click_rules, weighted_choice, FORBIDDEN_DEFAULT,
    get_detector, FrozenRowsDetector, locate_rule, shift_down_to_safe,
    in_avoid, avoid_coord, multi_click_weights,
    MULTI_CLICK_SIZES, MULTI_CLICK_GAP_S,
    MULTI_CLICK_MAX_S, MULTI_CLICK_JITTER_PROB, MULTI_CLICK_JITTER_RANGE,
    SETTLEMENT_CLICK_CD_S,
    SETTLEMENT_REUSE_PROB, SETTLEMENT_REUSE_EXACT,
    SETTLEMENT_REUSE_RADIUS, SETTLEMENT_REUSE_TTL_S)
from tasks.Component.GeneralBattle.config_general_battle import GreenMarkType, GeneralBattleConfig
from tasks.Component.GeneralBuff.config_buff import BuffClass
from tasks.Component.GeneralBuff.general_buff import GeneralBuff

from module.logger import logger


def auto_ocr_running(text) -> bool:
    """O_POINT_OR_SPEED 的 OCR 文本是否表示自动战斗运行中。

    资产注释约定语义：'×2'/'X2'/'x2' 等倍速字样视为未运行，
    纯数字 0-200 视为正在运行；空白/误读同样视为未运行。
    """
    if not text:
        return False
    t = str(text).strip()
    if not re.fullmatch(r'\d{1,3}', t):
        return False
    return 0 <= int(t) <= 200


class GeneralBattle(GeneralBuff, GeneralBattleAssets):
    """
    使用这个通用的战斗必须要求这个任务的config有config_general_battle
    """

    def run_general_battle(self, config: GeneralBattleConfig = None,
                           buff: BuffClass or list[BuffClass] = None,
                           remaining_count: int = None) -> bool:
        """
        运行脚本
        :param remaining_count: 任务层传入的剩余战斗次数（含本场），自动段规划用；
            不传则自动段功能静默跳过（存量调用方零改动）
        :return:
        """
        logger.hr("General battle start", 2)
        if config is None:
            config = GeneralBattleConfig()
        # 本人选择的策略是只要进来了就算一次，不管是不是打完了
        # 战斗统计
        self.current_count += 1
        logger.info(f"Current count: {self.current_count}")
        # 自动段规划（spec 4.2）：配置开启且任务层传入剩余场次时生效
        self.auto_battle_plan(config, remaining_count)
        # 战前设置
        self.battle_before(buff, config)
        # 绿标
        if self.is_in_battle(False):
            self.green_mark(config.green_enable, config.green_mark)
        # 到达自动段起点：整段（开自动→M 场→取消）在函数内完成后，battle_wait 接管第 M+1 场手动流程。
        # 时序：battle_before/绿标完成后、battle_wait 之前执行段，段返回时页面处于
        # 第 M+1 场的战斗过程界面，battle_wait 正常接管。
        # 注意必须放在 run_general_battle 而非 battle_wait：Orochi 等任务重写了 battle_wait，
        # 插在基类 battle_wait 开头的分支对重写方不可达（审查发现的静默失效缺陷）
        if self._auto_seg_reached():
            # 段内零输入且此处尚未进入 battle_wait（stuck 未挂），先挂长战斗计时作段内兜底
            self.device.stuck_record_add('BATTLE_STATUS_S')
            # 段执行（含段内截断/中断处理）返回后推进计划索引，指向下一段
            self.auto_battle_run()
            self._auto_seg_consume()
        # 战中设置
        win = self.battle_wait(config.random_click_swipt_enable)
        if win:
            return True
        else:
            return False

    def battle_before(self, buff: BuffClass | list[BuffClass], config: GeneralBattleConfig, timeout: float = 5) -> bool:
        """战斗前设置
        :return: True:进入战斗或点击了准备按钮且识别不到准备按钮了 False:超过timeout s还没有进入战斗且没有点击过准备
        """
        timeout_timer = Timer(timeout).start()
        confed = False
        while not timeout_timer.reached():
            self.screenshot()
            if self.is_in_real_battle(False):  # 战斗阶段
                return True
            if self.appear_then_click(self.I_DISABLE_7DAYS_DIFF_SOUL, interval=0.6):  # 关闭御魂不一致提示
                continue
            if self.appear_then_click(self.I_CONFIRM_CLOSE_DIFF_SOUL, interval=0.6):  # 确认关闭御魂不一致提示
                continue
            if self.is_in_prepare(False):  # 战斗准备阶段
                if not getattr(config, 'lock_team_enable', False):  # 没有锁定阵容
                    if self.current_count == 1 and not confed:  # 第一次战斗且是本次第一次配置
                        self.switch_preset_team(config.preset_enable, config.preset_group, config.preset_team)
                        self.check_and_open_buff(buff)
                        confed = True
                # 点击准备(仅非锁定阵容：锁定状态下游戏会自动准备，按钮出现
                # 的窗口期点击属于多余操作，容易暴露脚本特征，故不点)
                if not getattr(config, 'lock_team_enable', False):
                    if self.appear_then_click(self.I_PREPARE_HIGHLIGHT, interval=0.8):
                        continue
                continue
            # 未知界面, 既不是准备界面也不是战斗界面
            # logger.info('Wait for preparation page')  # 这玩意刷屏
            sleep(random.uniform(0.4, 0.8))
        return False

    def run_general_battle_back(self, config: GeneralBattleConfig = None, exit_four: bool = False) -> bool:
        """
        进入挑战然后直接返回
        :param config:
        :return:
        """
        # 如果没有锁定队伍那么在点击准备后才退出的,退四的话就直接退出
        if not config.lock_team_enable and not exit_four:
            # 点击准备按钮
            self.wait_until_appear(self.I_PREPARE_HIGHLIGHT)
            while 1:
                self.screenshot()
                if self.appear_then_click(self.I_PREPARE_HIGHLIGHT, interval=1.5):
                    continue
                if not (self.appear(self.I_PRESET) or self.appear(self.I_PRESET_WIT_NUMBER)):
                    break
            logger.info(f"Click {self.I_PREPARE_HIGHLIGHT.name}")

        # 点击返回
        while 1:
            self.screenshot()
            if self.appear(self.I_EXIT_ENSURE):
                break
            if self.appear_then_click(self.I_EXIT, interval=1.5):
                continue
            
        logger.info(f"Click {self.I_EXIT.name}")

        # 点击返回确认
        while 1:
            self.screenshot()
            if self.appear(self.I_FALSE):
                break
            if self.appear_then_click(self.I_EXIT_ENSURE, interval=1.5):
                continue
            
        logger.info(f"Click {self.I_EXIT_ENSURE.name}")

        # 点击失败确认
        self.wait_until_appear(self.I_FALSE)
        while 1:
            self.screenshot()
            if not self.appear(self.I_FALSE):
                break
            if self.appear_then_click(self.I_FALSE, interval=1.5):
                continue
            
        logger.info(f"Click {self.I_FALSE.name}")

        return True

    def exit_battle(self, skip_first: bool = False) -> bool:
        """
        在战斗的时候强制退出战斗
        :return:
        """
        if skip_first:
            self.screenshot()

        if not self.appear(self.I_EXIT):
            return False

        # 点击返回
        logger.info(f"Click {self.I_EXIT.name}")
        # 先查退出确认弹窗再点返回：弹窗弹出后 I_EXIT 在背后仍可匹配，
        # 单轮耗时>=interval 时旧顺序会每轮 continue 饿死 break（慢节奏死循环）
        while 1:
            self.screenshot()
            if self.appear(self.I_EXIT_ENSURE):
                break
            if self.appear_then_click(self.I_EXIT, interval=1.5):
                continue

        # 点击返回确认
        while 1:
            self.screenshot()
            if self.appear_then_click(self.I_EXIT_ENSURE, interval=1.5):
                continue
            if self.appear_then_click(self.I_FALSE, interval=1.5):
                continue
            if not self.appear(self.I_EXIT):
                break

        return True

    # 结算奖励框检测开关（类属性，任务可覆盖）：True（默认）启用检测，检测出的
    # 奖励行挖出禁区并作为「仍在奖励页」的第二判据。结算页没有标准三行奖励
    # 网格的任务（如本期活动的卷轴面板结算）应覆盖为 False——检测恒为空，
    # 白白消耗每帧 60~150ms，且兜底判据恒 False 无意义；此时禁区只剩常驻预设。
    # 同一任务内不同结算画面形态不同的，覆盖 reward_grid_detect_enabled 按画面分派。
    REWARD_GRID_DETECT = True

    def reward_grid_detect_enabled(self) -> bool:
        """本帧是否做奖励框检测（禁区的动态部分）。

        默认直接取类属性 REWARD_GRID_DETECT。需要按**结算画面**分派的任务覆盖
        本方法——同一个活动里 boss 结算是标准三行奖励网格、普通结算走卷轴面板，
        而 REWARD_GRID_DETECT 只是任务级开关，粒度不够。类属性仍作为默认值保留。
        """
        return self.REWARD_GRID_DETECT

    def reward_hot(self):
        """本任务的热区形状覆盖（None = 通用校准值，真人实采反推，见 reward_frame）。

        结算落点密度场的 x 范围 / 两侧 σ / y 峰值比例可按任务界面特点整体
        覆盖（返回 reward_frame.HotShape）；y 锚点仍由奖励禁区动态决定，
        不在覆盖范围内。需要专属热区的任务覆盖本方法。
        """
        return None

    def reward_forbidden(self) -> tuple:
        """本任务的常驻禁止区域预设（720p），与奖励检测无关、永远不点的地方。

        基类返回御魂本/活动本/其他本的默认预设；结界突破、寮突破、探索
        这三类任务的界面布局不同，覆盖本方法换成 FORBIDDEN_KEKKAI。
        """
        return FORBIDDEN_DEFAULT

    def reward_avoid(self) -> tuple:
        """本任务的落点回避区（720p）：不改变安全区域几何，只是落点不往里放。

        与 reward_forbidden 的区别：禁区参与几何切分（会把热区挤走），回避区
        只做落点的拒绝采样。组队时胜利画面的队友战绩框用这个——它们横跨整个
        热区宽度，并入禁区会把落点全压到屏幕最底一条（实测核心区命中 19%）。

        基类返回空（单人战斗没有队友战绩框）；组队任务覆盖成
        AVOID_WIN_TEAM2（两人：契灵/御魂/师徒）或 AVOID_WIN_TEAM3（三人：同心协力）。
        """
        return ()

    def screenshot(self):
        """截图入口：每取到新的一帧就作废奖励检测缓存。

        奖励框是结算动画里逐行出现的——上一帧算出的安全区域，到这一帧可能
        已经压在新出现的那一行上。缓存只活一帧，保证「点击落点按当前页面算」；
        同一帧内的多次调用（算落点 + 奖励框判据 + 退出条件）仍只检测一次。
        """
        image = super().screenshot()
        self._reward_safe_rules = None
        return image

    def reward_click_actions(self):
        """结算奖励与战斗胜利画面的落点：全屏候选挖掉常驻禁点区域与检测出的奖励行。

        战斗胜利画面（I_WIN / I_WIN_2 共判）没有奖励框，检测出的禁点行自然为空，
        所以两个画面共用同一套安全区域即可；画面切换到奖励页后奖励行会被
        检测出来并从落点里挖掉。

        检测一次约 60~150ms。缓存粒度是「一帧」（截图入口作废，见 screenshot）：
        同一帧内重复调用复用结果，换帧必重新检测——奖励框逐行出现，
        用上一帧的禁区去点当前帧就可能正好点在刚出现的那一行上。
        检测异常或安全区域被挖空时回退到 C_REWARD_1：它在奖励网格下方（y 623 > 网格底 554），
        不依赖检测就一定安全。

        同一帧只检测一次：检测出的奖励行既用来挖禁区，也作为「仍在奖励页」的
        第二判据缓存下来（见 reward_grid_appear）。reward_grid_detect_enabled
        为 False 时跳过检测（无标准奖励网格的结算页），本方法退化为纯静态分区。
        """
        if getattr(self, '_reward_safe_rules', None) is not None:
            return self._reward_safe_rules

        try:
            # 奖励框检测开关（reward_grid_detect_enabled）：关闭时跳过检测、
            # 禁区只剩常驻预设——无标准奖励网格的结算页用
            rows = (get_detector().detect(self.device.image)
                    if self.reward_grid_detect_enabled() else [])
            rules = safe_click_rules(self.device.image,
                                     forbidden_preset=self.reward_forbidden(),
                                     detector=FrozenRowsDetector(rows),
                                     hot=self.reward_hot())
        except Exception as e:
            # 模板缺失、截图异常等都不该让整个战斗任务挂掉，回退到恒安全的底部区域
            logger.warning(f'Reward frame detect failed, fallback to C_REWARD_1: {e}')
            rows, rules = [], []
        if not rules:
            rules = [self.C_REWARD_1]

        self._reward_safe_rules = rules
        self._reward_grid_found = bool(rows)
        return rules

    def reward_grid_appear(self, interval: float = None) -> bool:
        """奖励框检测作为「仍在奖励页」的第二判据，与 I_REWARD 系模板并列。

        I_REWARD / I_REWARD_GOLD 认的是奖励页上具体某个图案，遇到没收录过的
        奖励底色或结算动画中间帧会失配——此时页面明明还停在奖励页，却既没人
        点击也会被判成结算结束。奖励框检测认的是网格本身（6 种边框模板 × 3 行
        相位锁定），只要页面上还有奖励框就成立，与奖励内容无关。

        复用 reward_click_actions 的同一份检测结果（同帧只检测一次、换帧必重检，
        见 screenshot），interval 语义与 appear() 一致。
        """
        name = 'REWARD_GRID'
        if interval:
            if name in self.interval_timer:
                if self.interval_timer[name].limit != interval:
                    self.interval_timer[name] = Timer(interval)
            else:
                self.interval_timer[name] = Timer(interval)
            if not self.interval_timer[name].reached():
                return False
        # 顺带保证落点区域与本次判据出自同一份检测结果
        self.reward_click_actions()
        appear = bool(getattr(self, '_reward_grid_found', False))
        if appear and interval:
            self.interval_timer[name].reset()
        return appear

    def win_appear(self, interval: float = None, threshold: float = None) -> bool:
        """胜利画面判据：I_WIN / I_WIN_2 / I_DE_WIN 任一命中即视为胜利画面到来。

        I_WIN_2 是增补的第二个胜利标记模板（2026-09-06），覆盖 I_WIN 在部分
        画面状态下失配的情形；I_DE_WIN 是封魔战斗的胜利标记。三模板共判：
        「胜利画面还在」的判定集合必须与「点掉它」的模板集合一致，否则单一
        模板失配时会误判画面已消失、提前跳出结算等待（原实现只认 I_WIN，
        I_WIN 失配的胜利画面会被当成已消失）。
        """
        return (self.appear(self.I_WIN, interval=interval, threshold=threshold) or
                self.appear(self.I_WIN_2, interval=interval, threshold=threshold) or
                self.appear(self.I_DE_WIN, interval=interval, threshold=threshold))

    def win_template_names(self) -> tuple:
        """胜利画面判据的资源名集合（与 win_appear 的模板集一致）。

        settlement_gesture 用它区分两个结算阶段的点击：
        - 胜利画面的点击（control_name 命中本集合）：追加击落在随后出现的
          奖励页空白处，安全区域依然有效，不计入连点衰减计数（并把计数清零，
          标记新一场结算的开始）；
        - 奖励页阶段的点击（I_REWARD 系 / 奖励框兜底 / 贪吃鬼等）：
          get reward 之后才开始计数——它们的追加击可能落在切换后的
          准备页/主界面上，那里没有安全区域可言，才是衰减要管的对象。
        """
        return (self.I_WIN.name, self.I_WIN_2.name, self.I_DE_WIN.name)

    def _settlement_cd_ready(self) -> bool:
        """结算点击的共享 CD：距上一次结算点击不足 SETTLEMENT_CLICK_CD_S 时 False。

        结算阶段的各个判据（I_WIN / I_WIN_2 / I_REWARD / I_REWARD_GOLD / 各种皮肤
        与御魂模板 / 奖励框兜底 / 贪吃鬼）指向同一个意图「把结算页面点掉」，但它们
        各有以模板名为 key 的 interval timer，彼此不互斥——多判据并挂时
        1.5s 内能连点多次，每次点击都会落到「点击后才关闭的页面」上，多余
        的那次可能正好点进准备页面造成误触（2026-09-06 08:44 事故：奖励页
        关闭后 0.74s 又点了一次，落到准备页面，页面识别错乱卡死 60s）。
        三个结算入口（settlement_click / settlement_click_grid /
        settlement_gesture）统一在这里把关，新增判据自动被覆盖。
        """
        last = getattr(self, '_settlement_click_ts', None)
        if last is not None and time.time() - last < SETTLEMENT_CLICK_CD_S:
            return False
        return True

    def _settlement_cd_touch(self) -> None:
        """记一次结算点击时刻（共享 CD 的打点）。"""
        self._settlement_click_ts = time.time()

    def settlement_click_grid(self, action, interval: float = None) -> bool:
        """检测到奖励框就点击：I_REWARD 系模板全部失配时的兜底触发。

        落点仍是安全区域（已挖掉奖励行与常驻禁区），不点奖励框本身；
        控件名单列 REWARD_GRID，与 I_REWARD 的连点计数/退避互不干扰。
        与其他结算判据共享点击 CD（见 _settlement_cd_ready）。
        """
        if not self.reward_grid_appear(interval=interval):
            return False
        if not self._settlement_cd_ready():
            return False
        self.settlement_gesture(action, control_name='REWARD_GRID')
        return True

    def settlement_click(self, target, action, interval=None, threshold=None) -> bool:
        """结算专用「出现即点击」：目标出现就在安全区域落点点击，并按概率连点。

        appear_then_click 的结算限定版，两者语义一致（interval 计时器照常管理），
        差别只在点击动作换成 settlement_gesture——按真人簇长直方图连点。
        **连点只允许用在战斗结束（胜利画面）与领取奖励两个场景**，其余点击
        一律继续走 appear_then_click，保持单击语义。
        与其他结算判据共享点击 CD（见 _settlement_cd_ready）。
        """
        if not self.appear(target, interval=interval, threshold=threshold):
            return False
        if not self._settlement_cd_ready():
            return False
        self.settlement_gesture(action, control_name=target.name)
        return True

    def settlement_gesture(self, action, control_name='Reward') -> None:
        """执行一次结算点击手势：首击走正常节奏，其后按概率追加快速连击。

        首击与普通点击完全一致（节奏已在截图入口等满、按压时长/轨迹等拟人化
        维度照常），追加击由 _settlement_extra_clicks 负责。结算循环里不方便
        appear_then_click 的场景（如贪吃鬼连点）可直接调本方法。

        首击落点由 _settlement_point 决定：同一场战斗内会参考上一次结算落点
        （奖励页参考胜利画面那一次），跨场次则回到自由取点。

        连点衰减只统计**奖励页阶段**（get reward 之后）的点击事件：胜利画面
        的追加击落在随后的奖励页空白处仍安全，奖励页的追加击才可能落在切换
        后的准备页/主界面上（见 _settlement_extra_clicks）。胜利画面的点击
        同时把计数清零——新一场结算的奖励页从第 0 次计起。

        直接调用（不经 settlement_click 的判据过滤）同样守共享 CD——贪吃鬼
        这类显式 appear 判定后调用的场景与模板判据用的是同一份节流。
        """
        if not self._settlement_cd_ready():
            return
        # 连点衰减计数（奖励页阶段已发起的点击事件数，事件级）：
        # - 胜利画面的点击：清零计数（新一场结算开始），本击自身不计数；
        # - 跨场兜底：胜利画面失配直接出奖励页的流程不经过胜利画面点击，
        #   用与 _settlement_last 同一的 TTL 判定场次边界，清掉上一场残留
        is_win_click = control_name in self.win_template_names()
        if is_win_click:
            self._settlement_page_clicks = 0
        else:
            prev_ts = getattr(self, '_settlement_click_ts', None)
            if prev_ts is None or time.time() - prev_ts > SETTLEMENT_REUSE_TTL_S:
                self._settlement_page_clicks = 0
        self._settlement_cd_touch()
        # 本次点击的序号：连点抽样的衰减档位按它取（第 0 次保持原始权重）
        clicks_before = getattr(self, '_settlement_page_clicks', 0)
        x, y, rule = self._settlement_point(action)
        first_ts = time.time()          # 首击发起时刻，作为连点节拍的起点
        self.device.click(x, y, control_name=control_name)
        last_ts = self._settlement_extra_clicks(rule, x, y, control_name, first_ts,
                                                page_clicks=clicks_before)
        # 奖励页阶段的点击：整次手势（含内部连点）计 1 个事件，
        # 作为下一次手势的衰减档位；胜利画面点击不计（保持 0）
        if not is_win_click:
            self._settlement_page_clicks = clicks_before + 1
        # TTL 基准取**末击**的发起时刻：连点最长 0.66s，若从首击起算，
        # 追加击这段时间会被 TTL 白白吃掉——下次取点时距首击已 >TTL 的
        # 场景（结算动画慢、贪吃鬼连点）会误判成换了场。场次的语义是
        # 「距这只手最后一次动作多久」，天然以末击为准。
        self._settlement_last = (x, y, last_ts if last_ts is not None else first_ts)

    def _settlement_point(self, action):
        """算本次结算首击的落点，返回 (x, y, 该落点所在的安全区域)。

        同一场战斗内的落点互相参考：真人按场次切分后，场次内相邻点击事件有
        31.4% 落在完全相同的坐标、43.8% 在 30px 内（中位 38.6px），而跨场次
        （>10s）与重新自由取点不可区分（距离比 1.01）。故复用带 TTL，超时自动
        失效，无需在战斗流程里显式重置。

        胜利画面无奖励框、奖励页有，复用的坐标可能正好被新出现的奖励行覆盖：
        此时保持 x 不变沿 y 向下挪到最近的安全区域（热区本就锚在禁区下方），
        挪不动才回退自由取点。

        只使用本帧已有的安全区域缓存，不额外触发奖励框检测——调用方在
        weighted_choice 时已经算过，这里复用同一份结果。

        注意：这里**不写** _settlement_last。时间戳的基准是末击发起时刻
        （连点可能持续 0.66s），由 settlement_gesture 在整次手势结束后统一
        写入；在取点阶段提前写会把首击时刻当基准，连点时长被 TTL 吃掉。
        """
        rules = getattr(self, '_reward_safe_rules', None)
        last = getattr(self, '_settlement_last', None)
        if (rules and last is not None
                and time.time() - last[2] <= SETTLEMENT_REUSE_TTL_S
                and random.random() < SETTLEMENT_REUSE_PROB):
            point = self._settlement_reuse(rules, last[0], last[1])
            if point is not None:
                return point
        # 自由取点：落进队友战绩框等回避区就重取，不改变安全区域几何
        return *avoid_coord(action, self.reward_avoid()), action

    def _settlement_reuse(self, rules, x, y):
        """把上次落点适配到本帧，返回 (x, y, rule)；无法适配返回 None。

        除了「被新出现的奖励行盖住」，还要处理「落在本任务的回避区里」——
        上一次可能是在胜利画面之前取的点，此时队友战绩框还没出现。
        """
        avoid = self.reward_avoid()
        rule = locate_rule(rules, x, y)
        if rule is None:
            # 被新出现的奖励行盖住了：保持 x，沿 y 往下挪到最近的安全区域
            shifted = shift_down_to_safe(rules, x, y)
            if shifted is None:
                return None
            y, rule = shifted
            logger.info(f'Settlement point shifted down to ({x}, {y}) by forbidden area')
            if in_avoid(avoid, x, y) is None:
                return x, y, rule
            # 下移后正好落在战绩框上：放弃复用，回到自由取点
            return None
        hit = in_avoid(avoid, x, y)
        if hit is not None:
            # 上次落点现在压在战绩框上（例如胜利画面才出现的队友框）：不复用
            logger.info(f'Settlement reuse dropped: last point ({x}, {y}) in {hit}')
            return None
        # 仍然安全：多数情况用完全相同的坐标，其余在小半径内微调
        if random.random() < SETTLEMENT_REUSE_EXACT:
            return x, y, rule
        d = random.uniform(1.0, SETTLEMENT_REUSE_RADIUS)
        a = random.uniform(0, 2 * math.pi)
        nx = x + int(round(d * math.cos(a)))
        ny = y + int(round(d * math.sin(a)))
        moved = locate_rule(rules, nx, ny)
        # 微调后越界到禁区或踩进回避区就放弃微调，退回原坐标（原坐标已确认可点）
        if moved is None or in_avoid(avoid, nx, ny) is not None:
            return x, y, rule
        return nx, ny, moved

    def settlement_click_count(self, page_clicks: int) -> int:
        """本次结算手势的总点击数（含首击）：默认按真人簇长直方图查表抽样。

        任务级覆盖点：固定返回值后连击不再抽样——结算画面切换快的任务
        （如活动爬塔）用固定双击把追加击的误触窗口压到最小。
        :param page_clicks: 本奖励页已发起的点击事件数（衰减查表索引，
            覆盖方不需要衰减时忽略该参数）
        """
        return random.choices(MULTI_CLICK_SIZES,
                              weights=multi_click_weights(page_clicks))[0]

    def _settlement_extra_clicks(self, action, x, y, control_name,
                                 first_ts: float = None, page_clicks: int = 0):
        """按真人簇长分布在首击后追加快速连击，对齐真人结算行为。

        :return: 末次追加击的发起时刻；没有追加击（单击）返回 None。
            调用方用它做 TTL 基准（见 settlement_gesture）。

        追加击的特征（MULTI_CLICK_* 常量在 reward_frame.py，取值由真人实采校准）：
        - 次数由 settlement_click_count 给出（默认按真人簇长直方图抽样，任务
          可覆盖为固定值，如活动爬塔的固定双击），**在 4 点封顶**：首击点掉奖励页后，
          剩余追加击会落到新出现的界面上（安全区域是按奖励页算的，在新界面
          上那个坐标可能是「再来一局」之类的按钮），所以真人尾部 5~11 点的
          长簇不采用，把最长暴露窗口从 2.20s 压到 0.66s。多点簇内部还把权重
          从 4 点挪向 2/3 点——误触窗口与簇长成正比，而单击占比与连点触发率
          保持真人值不变；
        - 权重随**本奖励页已发起的点击事件数**线性衰减（page_clicks，
          multi_click_weights）：截图间隔约 0.3s，首击点掉结算页后画面正在
          切换而最近一帧仍是旧画面——判据继续命中、追加击继续执行，就会
          落到已切换完成的新画面上（共享 CD 只管事件之间，管不到手势内部）。
          奖励页点得越多越可能在下一次被点掉，多击档位按次数衰减、档位越高
          越快（4 连击已点 3 次后归零）；第 0 次保持原始权重，拟真度不损失；
        - 间隔按**节拍补偿**对齐到目标值：device.click 自身要花约 165ms
          （按下-移动-抬起 + 拟人化按压时长 + 轨迹），直接 sleep(gap) 会叠加
          在它上面，实测相邻击间隔 334~381ms，是设定值 150~220ms 的两倍
          （实例A/B/C 日志实测）。这里改为「距上一击已过多久，只补足差额」，
          实测间隔才真正等于真人的 150~220ms；
        - device.click 传 pace=False 绕过操作节奏 CD——否则节奏模型的兜底
          等待会把连点拖成秒级间隔；节奏与同资源退避只在首击记账，整次手势
          视作一个意图；
        - 追加击总时长受 MULTI_CLICK_MAX_S 预算约束，**按 wall clock 计**而非
          累加自己 sleep 了多久——click 本身的耗时不进 sleep 的账，只记 sleep
          会让预算形同虚设（4 点手势预算内 0.66s、实测 1.28s）。超预算立即
          收尾、不补完剩余次数；
        - **落点默认复用首击坐标**（真人簇内 86% 的相邻点击落在同一像素），
          仅 MULTI_CLICK_JITTER_PROB 概率偏移，且偏移量取真人非零位移的量级
          （中位约 11px）而非 0~3px 的持续微抖——「每次都抖一点点」正是真人
          最罕见、脚本最典型的模式；偏移后钳回首击所在的安全矩形，
          绝不因微动越界点进禁点区域；
        - 按压时长、按压轨迹等拟人化维度不受影响，追加击走正常 backend 链路。

        :param first_ts: 首击的发起时刻。节拍以「点击发起」为基准而非「点击返回」，
            否则补偿不掉 click 自身的耗时——那正是实测间隔翻倍的原因。
            缺省取当前时刻（首个间隔会偏长约一次 click 的耗时）。
        :param page_clicks: 本奖励页已发起的点击事件数（get reward 之后起算，
            一次手势计 1 次、连点内部不重复计），作为多击权重的查表索引
            （MULTI_CLICK_WEIGHTS_BY_EVENT，2026-09-06 起查表替代线性衰减）。
        """
        # 簇长从 settlement_click_count 取（任务可覆盖为固定值），不再就地抽样
        n = self.settlement_click_count(page_clicks)
        if n == 1:
            return None
        rx, ry, rw, rh = action.roi_front
        px, py = x, y
        avoid = self.reward_avoid()          # 连点偏移同样不许踩进战绩框
        last = first_ts if first_ts is not None else time.time()   # 上一击的发起时刻
        start = last                                               # 整次手势的起点
        for _ in range(n - 1):
            gap = random.uniform(*MULTI_CLICK_GAP_S)
            now = time.time()
            # 下一击最早能发出的时刻：理想节拍点；若上一击本身就耗时超过 gap，
            # 已经追不上节拍，就立刻发出（max 保证不往回等）
            nxt = max(last + gap, now)
            if nxt - start > MULTI_CLICK_MAX_S:
                # 预算按 wall clock 判定，不够再点一击就立即收尾，缩短误触窗口
                break
            if nxt > now:
                sleep(nxt - now)
            last = time.time()          # 本击的发起时刻，作为下一次补偿的基准
            # 默认复用上一击坐标（真人手按住不动）；小概率发生一次真实移动，
            # 移动后作为新的落点继续连点，与真人「点着点着挪了一下」一致
            if random.random() < MULTI_CLICK_JITTER_PROB:
                d = random.uniform(*MULTI_CLICK_JITTER_RANGE)
                a = random.uniform(0, 2 * math.pi)
                nx = px + int(round(d * math.cos(a)))
                ny = py + int(round(d * math.sin(a)))
                # 贴块边时钳回安全矩形，保证偏移不会越界点进禁点区域
                nx = min(max(nx, rx), rx + rw - 1)
                ny = min(max(ny, ry), ry + rh - 1)
                # 偏移后落在队友战绩框等回避区就放弃这次偏移（保持原坐标继续连点）
                if in_avoid(avoid, nx, ny) is None:
                    px, py = nx, ny
            self.device.click(px, py, control_name=control_name, pace=False)
        return last                      # 末击发起时刻（至少含首击基准）

    def battle_wait(self, random_click_swipt_enable: bool) -> bool:
        """
        等待战斗结束 ！！！
        很重要 这个函数是原先写的， 优化版本在tasks/Secret/script_task下。本着不改动原先的代码的原则，所以就不改了
        :param random_click_swipt_enable:
        :return:
        """
        # 有的时候是长战斗，需要在设置stuck检测为长战斗
        # 但是无需取消设置，因为如果有点击或者滑动的话 handle_control_check会自行取消掉
        self.device.stuck_record_add('BATTLE_STATUS_S')
        self.device.click_record_clear()
        # 战斗过程 随机点击和滑动 防封
        logger.info("Start battle process")
        win: bool = False
        while 1:
            self.screenshot()
            # 如果出现赢 就点击：I_WIN/I_WIN_2/I_DE_WIN 三模板共判（封魔走 I_DE_WIN）
            if self.win_appear(threshold=0.8):
                logger.info("Battle result is win")
                if self.appear(self.I_DE_WIN):
                    self.ui_click_until_disappear(self.I_DE_WIN)
                win = True
                break

            # 如果出现失败 就点击，返回False
            if self.appear(self.I_FALSE, threshold=0.8):
                logger.info("Battle result is false")
                win = False
                break

            # 如果领奖励
            if self.appear(self.I_REWARD, threshold=0.6):
                win = True
                break

            # 如果领奖励出现金币
            if self.appear(self.I_REWARD_GOLD, threshold=0.8):
                win = True
                break
            # 如果开启战斗过程随机滑动
            if random_click_swipt_enable:
                self.random_click_swipt()

        # 再次确认战斗结果
        logger.info("Reconfirm the results of the battle")
        while 1:
            self.screenshot()
            if win:
                # 点击赢了：全屏减去常驻禁点区域（胜利画面无奖励框，与奖励页共用安全区域），
                # 落点按「面积×人类落点密度」加权挑选、区域内采样由拟人化层完成；
                # 结算场景按概率连点（双击/三击），见 settlement_click。
                # 点击链与 win_appear 的模板集一致（I_WIN/I_WIN_2/I_DE_WIN），
                # 保证「判定还在的画面」永远有对应模板可点，不会空转
                action_click = weighted_choice(self.reward_click_actions())
                if (self.settlement_click(self.I_WIN, action_click, interval=0.5) or
                        self.settlement_click(self.I_WIN_2, action_click, interval=0.5) or
                        self.settlement_click(self.I_DE_WIN, action_click, interval=0.5)):
                    continue
                if not self.win_appear():
                    break
            else:
                # 如果失败且 点击失败后
                if self.appear_then_click(self.I_FALSE, threshold=0.6):
                    continue
                if not self.appear(self.I_FALSE, threshold=0.6):
                    return False
        # 最后保证能点击 获得奖励
        # 结算面板就绪信号二选一：奖励框 I_REWARD 或左上角详情图标 I_EXTRA_INFO。
        # 原来串行等待（EXTRA_INFO 3 秒超时后才等 I_REWARD）在没有 EXTRA_INFO 的
        # 战斗（结界突破/寮突破）上每场白等 3 秒——两个信号并行等，任一出现即领奖
        self.screenshot()
        panel_timer = Timer(3)
        panel_timer.start()
        while not (self.appear(self.I_REWARD) or self.appear(self.I_EXTRA_INFO)):
            if panel_timer.reached():
                break
            self.screenshot()
        if not self.appear(self.I_REWARD) and not self.appear(self.I_EXTRA_INFO):
            # 并行等待超时：面板可能还没刷出来，限时再等 I_REWARD 一段时间。
            # 原来这里不传 wait_time 是无限等待，「没有奖励」分支永远不可达，
            # 真没奖励的战斗会卡死——限时等待让 no reward 路径可达
            if not self.wait_until_appear(self.I_REWARD, wait_time=5):
                # 有些的战斗没有下面的奖励，所以直接返回
                logger.info("There is no reward, Exit battle")
                return win
        logger.info("Get reward")
        while 1:
            self.screenshot()
            # 战斗胜利后队长弹出「是否邀请队友继续进行战斗」确认框：
            # 奖励阶段已结束，退出循环交给任务层 check_and_invite 处理，
            # 避免结算左上角 EXTRA_INFO 在弹窗上仍命中导致持续误点（Too many click）
            if self.appear(GeneralInviteAssets.I_GI_SURE):
                logger.info("Invite teammate dialog detected, exit reward loop")
                break
            # 如果出现领奖励；落点按「面积×人类落点密度」加权挑选（热区更容易被选中），
            # 结算场景按概率连点（双击/三击），见 settlement_click
            action_click = weighted_choice(self.reward_click_actions())
            if (self.settlement_click(self.I_REWARD, action_click, interval=1.5) or
                self.settlement_click(self.I_REWARD_GOLD, action_click, interval=1.5) or
                # I_REWARD 系模板失配时的兜底：只要还检测到奖励框就照样点安全区域
                self.settlement_click_grid(action_click, interval=1.5)  #  or
                # self.settlement_click(self.I_REWARD_STATISTICS, action_click, interval=1.5) or
                # self.settlement_click(self.I_REWARD_PURPLE_SNAKE_SKIN, action_click, interval=1.5) or
                # self.settlement_click(self.I_REWARD_GOLD_SNAKE_SKIN, action_click, interval=1.5) or
                # self.settlement_click(self.I_REWARD_EXP_SOUL_4, action_click, interval=1.5) or
                # self.settlement_click(self.I_REWARD_SOUL_5, action_click, interval=1.5) or
                # self.settlement_click(self.I_REWARD_SOUL_6, action_click, interval=1.5)
                ):
                continue
            if self.settlement_click(self.I_EXTRA_INFO, action_click, interval=1.5):
                logger.info(f"Click self.I_EXTRA_INFO.name")
                sleep(1.5)
                continue
            # 未知结算弹窗（皮肤碎片等）：点一下空白区域尝试跳过。
            # 检测到奖励框说明还在奖励页（I_REWARD 只是失配），不能走这条盲点分支
            if self.appear(self.I_STATISTICS) and not self.appear(self.I_REWARD)and not self.win_appear() and not self.appear(GeneralInviteAssets.I_GI_SURE) and not self.reward_grid_appear():
                self.click(self.C_RANDOM_CLICK)  #碎片
                self.appear_then_click(self.I_CONFIRM_CLOSE_DIFF_SOUL) #整个皮肤
                continue
            if (not self.appear(self.I_REWARD) and
                not self.appear(self.I_REWARD_GOLD) and
                not self.appear(self.I_EXTRA_INFO) and
                # 奖励框还在就不算结算结束（与上面的兜底点击同一判据）
                not self.reward_grid_appear()#  and
                # not self.appear(self.I_REWARD_STATISTICS) and
                # not self.appear(self.I_REWARD_PURPLE_SNAKE_SKIN) and
                # not self.appear(self.I_REWARD_GOLD_SNAKE_SKIN) and
                # not self.appear(self.I_REWARD_EXP_SOUL_4) and
                # not self.appear(self.I_REWARD_SOUL_5) and
                # not self.appear(self.I_REWARD_SOUL_6)
                ):
                logger.info(f"break reward loop")
                break

        return win

    def _hook_special_reward(self) -> bool:
        """
        For overwrite https://github.com/runhey/OnmyojiAutoScript/issues/1580
        """
        return False

    def green_mark(self, enable: bool = False, mark_mode: GreenMarkType = GreenMarkType.GREEN_MAIN):
        """
        绿标， 如果不使能就直接返回
        :param enable:
        :param mark_mode:
        :return:
        """
        if enable:
            logger.info("Green is enable")
            x, y = None, None
            match mark_mode:
                case GreenMarkType.GREEN_LEFT1:
                    x, y = self.C_GREEN_LEFT_1.coord()
                    logger.info("Green left 1")
                case GreenMarkType.GREEN_LEFT2:
                    x, y = self.C_GREEN_LEFT_2.coord()
                    logger.info("Green left 2")
                case GreenMarkType.GREEN_LEFT3:
                    x, y = self.C_GREEN_LEFT_3.coord()
                    logger.info("Green left 3")
                case GreenMarkType.GREEN_LEFT4:
                    x, y = self.C_GREEN_LEFT_4.coord()
                    logger.info("Green left 4")
                case GreenMarkType.GREEN_LEFT5:
                    x, y = self.C_GREEN_LEFT_5.coord()
                    logger.info("Green left 5")
                case GreenMarkType.GREEN_MAIN:
                    x, y = self.C_GREEN_MAIN.coord()
                    logger.info("Green main")

            # 等待那个准备的消失
            while 1:
                self.screenshot()
                if not self.appear(self.I_PREPARE_HIGHLIGHT):
                    break

            # 判断有无坐标的偏移
            self.appear_then_click(self.I_LOCAL)
            time.sleep(0.3)
            # 点击绿标
            self.device.click(x, y)

    def switch_preset_team(self, enable: bool = False, preset_group: int = 1, preset_team: int = 1):
        """
        切换预设的队伍， 要求是在不锁定队伍时的情况下
        :param enable:
        :param preset_group:
        :param preset_team:
        :return:
        """
        if not enable:
            logger.info("Preset is disable")
            return None

        logger.info("Preset is enable")
        # 点击预设按钮
        while 1:
            self.screenshot()

            if self.appear(self.I_PRESET_ENSURE):
                break
            # 首个队伍没有满足5个式神，未出现预设按钮的情况下跳出循环
            if self.appear(self.I_PRESENT_LESS_THAN_5):
                break
            if self.appear_then_click(self.I_PRESET, threshold=0.8, interval=1):
                continue
            if self.appear_then_click(self.I_PRESET_WIT_NUMBER, threshold=0.8, interval=1):
                continue
            if self.ocr_appear(self.O_PRESET):
                self.click(self.O_PRESET, interval=1)
                continue
            if self.ocr_appear(self.O_PRESET_FULL):
                self.click(self.O_PRESET_FULL, interval=1)
                continue
        logger.info("Click preset button")

        def get_unselect_color(tmp1, tmp2, tmp3, size):
            # 获取未选择分组的颜色，3组之中必定存在两个颜色相似
            # area 参数格式是（x1,y1,x2,y2）
            color_1 = get_color(self.device.image,
                                (tmp1.roi_back[0], tmp1.roi_back[1],
                                 tmp1.roi_back[0] + size[0], tmp1.roi_back[1] + size[1]))
            color_2 = get_color(self.device.image,
                                (tmp2.roi_back[0], tmp2.roi_back[1],
                                 tmp2.roi_back[0] + size[0], tmp2.roi_back[1] + size[1]))
            color_3 = get_color(self.device.image,
                                (tmp3.roi_back[0], tmp3.roi_back[1],
                                 tmp3.roi_back[0] + size[0], tmp3.roi_back[1] + size[1]))

            if color_similar(color_1, color_2):
                return color_1
            if color_similar(color_2, color_3):
                return color_2
            return color_3

        # 选择预设组
        tmp = self.__getattribute__("C_PRESET_GROUP_" + str(preset_group))
        if tmp is None:
            tmp = self.C_PRESET_GROUP_1
        color_size = [self.C_PRESET_GROUP_1.roi_back[2],
                      self.C_PRESET_GROUP_1.roi_back[3]]
        # unselected_color = get_unselect_color(self.C_PRESET_GROUP_1, self.C_PRESET_GROUP_2, self.C_PRESET_GROUP_3, size=color_size)
        # 考虑到有些预设组没有预设，所以这里取一个比较固定的颜色
        unselected_color = (224.9, 208.3, 187.4)
        while True:
            self.screenshot()
            color_tmp = get_color(self.device.image,
                                  (tmp.roi_back[0], tmp.roi_back[1], tmp.roi_back[0] + color_size[0],
                                   tmp.roi_back[1] + color_size[1]))
            if color_similar(color_tmp, unselected_color):
                self.click(tmp, interval=0.2)
                continue
            break

        logger.info("Select preset group")

        # 选择预设的队伍
        time.sleep(0.5)
        tmp = self.__getattribute__("C_PRESET_TEAM_" + str(preset_team))
        if tmp is None:
            tmp = self.C_PRESET_TEAM_1
        color_size = [5, 5]
        # unselected_color = get_unselect_color(self.C_PRESET_TEAM_1, self.C_PRESET_TEAM_2, self.C_PRESET_TEAM_3, size=color_size )
        unselected_color = (216.8, 185.0, 146.8)
        while True:
            self.screenshot()
            color_tmp = get_color(self.device.image,
                                  (tmp.roi_back[0], tmp.roi_back[1], tmp.roi_back[0] + color_size[0],
                                   tmp.roi_back[1] + color_size[1]))
            if color_similar(color_tmp, unselected_color):
                self.click(tmp, interval=0.2)
                continue
            break

        self.click(tmp)
        logger.info("Select preset team")

        # 点击预设确认
        self.wait_until_appear(self.I_PRESET_ENSURE, wait_time=1)
        while 1:
            self.screenshot()
            if not self.appear(self.I_PRESET_ENSURE):
                break
            if self.appear_then_click(self.I_PRESET_ENSURE, threshold=0.8, interval=0.2):
                continue
        logger.info("Click preset ensure")

    def random_click_swipt(self):
        if 0 <= random.randint(0, 500) <= 3:  # 百分之4的概率
            rand_type = random.randint(0, 2)
            match rand_type:
                case 0:
                    self.click(self.C_RANDOM_CLICK, interval=20)
                case 1:
                    self.swipe(self.S_BATTLE_RANDOM_LEFT, interval=20)
                case 2:
                    self.swipe(self.S_BATTLE_RANDOM_RIGHT, interval=20)
            # 重新设置为长战斗
            # self.device.stuck_record_add('BATTLE_STATUS_S')
        else:
            time.sleep(0.4)  # 这样的好像不对

    # 判断是否在战斗中
    def is_in_battle(self, is_screenshot: bool = True) -> bool:
        """
        判断是否在战斗中
        tip: 因为有friends判别, 所以即使在准备界面也会识别在战斗中
        :return:
        """
        if is_screenshot:
            self.screenshot()
        if self.appear(self.I_BATTLE_INFO) or \
                self.appear(self.I_FRIENDS) or \
                self.win_appear() or \
                self.appear(self.I_FALSE) or \
                self.appear(self.I_REWARD):
            return True
        else:
            return False

    def is_in_real_battle(self, is_screenshot: bool = True):
        """
        判断是否在真正的战斗中(不是战斗准备界面也不是战斗结束界面)

        判据只能用 I_BATTLE_INFO：I_FRIENDS / I_EXIT 在准备界面同样存在
        （见 is_in_battle 的 tip，2026-09-14 真机复核），只有这个双箭头标识
        只在战斗过程中出现。
        2026-09-14 起每套战斗主题都在 battle_theme_model 里登记了自己的
        I_BATTLE_INFO_N，由 check_costume_battle 就地替换——此前它不随主题
        替换，换到雅乐之邦后仍匹配默认主题的图（实测 0.68 < 0.8），自动战斗段
        全程判不出战斗界面、_auto_start 六轮空转后回退手动（oas1 11:34/12:09）。
        :param is_screenshot:
        :return:
        """
        if is_screenshot:
            self.screenshot()
        return self.appear(self.I_BATTLE_INFO) and self.appear(self.I_EXIT)

    def is_in_prepare(self, is_screenshot: bool = True) -> bool:
        """
        判断是否在准备中
        :return:
        """
        if is_screenshot:
            self.screenshot()
        if self.appear(self.I_BUFF):
            return True
        elif self.appear(self.I_PREPARE_HIGHLIGHT):
            return True
        elif self.appear(self.I_PREPARE_DARK):
            return True
        elif self.appear(self.I_PRESET) or self.appear(self.I_PRESET_WIT_NUMBER):
            return True
        else:
            return False

    def is_auto_battle_page(self) -> bool:
        """自动战斗页判定（spec 4.4 真机修订）：OR 语义——按钮消失（模板失配）
        或 OCR 运行数字，任一成立即视为自动中。

        真机验证发现：战斗打完进结算页的渐入瞬间，OCR 会短暂读空
        （No text detected），原 AND 语义（OCR 数字且按钮消失）在此时
        连续 3 帧失配即误判中断，导致段退出后脚本在游戏仍自动的状态下
        开始点结算。按钮消失是"开启后即消失"的强模板证据，作为主判据；
        OCR 只在按钮仍在时复核（按钮在 + OCR 非运行数字 = 确认手动）。
        """
        if self.appear(self.I_PAPER_TOSTART):
            # 按钮还在：OCR 读到运行数字仍视为自动（OCR 直接证据优先），
            # 否则是手动页
            return auto_ocr_running(self.O_POINT_OR_SPEED.ocr(self.device.image))
        # 按钮不在 = 自动中（主判据，无需 OCR——段内监测也不再有 OCR 刷屏）
        return True

    def _seg_state(self) -> dict:
        """自动段状态载体（spec 4.1），惰性初始化。

        直接调 battle_wait 的路径（如 DemonRetreat）不会先跑规划，
        getattr 兜底保证其行为不变。

        2026-09-09 一次性分段规划改版：新增 plan（全段计划列表）、
        plan_idx（当前执行索引）、base（规划时的场次基准：start 为从
        规划场起数的序号，实际场次 = base + start）；
        start_offset/seg_len 保留为兼容字段（不再由规划写入）。
        """
        seg = getattr(self, '_auto_seg', None)
        if seg is None:
            seg = {'enabled': False, 'total_left': 0, 'planned': False,
                   'start_offset': -1, 'seg_len': 0,
                   'plan': [], 'plan_idx': 0, 'base': 0}
            self._auto_seg = seg
        return seg

    @staticmethod
    def _auto_plan_generate(r: int, t: int, seg_lo: int, seg_hi: int) -> list:
        """开局一次性生成全部自动段计划（均等槽位 + 区间随机段长）。

        算法（用户需求：数据随机、流程固定——只在开局随机一次，之后按序执行）：
        1. 段数 K 按预算自适应：K = max(1, round(T / 段长中值))；
        2. 把剩余场次轴 R 均分成 K 个槽（槽宽 W = R/K），每槽保证放一段——
           无论段长/偏移怎么随机，相邻段起点间隔有硬下界，杜绝旧实现
           "全段随机起点易挤到后半程"的分布不均；
        3. 每段段长在 [lo,hi] 随机抽取，受剩余预算截断（最后一段吸收差额，
           允许低于 lo）；
        4. 每段起点在槽内随机偏移（随机空档）。

        :param r: 剩余场次（含本场，首次收到的口径）
        :param t: 自动战斗总场数上限
        :param seg_lo/seg_hi: 段长区间
        :return: [(start, len), ...] 按 start 升序
        """
        # 段数 K：按预算与段长中值推导，保证期望段长落在区间内
        k = max(1, round(t / ((seg_lo + seg_hi) / 2)))
        # 槽宽不足容纳下限段长时收缩段数：每槽至少要装得下一段
        while k > 1 and r / k < seg_lo + 1:
            k -= 1
        w = r / k  # 槽宽（float：R 不被 K 整除时槽边界不取整对齐）
        plan = []
        budget = t
        prev_start = -1
        for i in range(k):
            # 段长：区间随机，吸收剩余预算差额（最后一段允许低于 lo）
            seg_len = random.randint(seg_lo, seg_hi)
            if seg_len > budget:
                seg_len = budget
            # 槽内偏移：起点在 [槽首, 槽首 + 槽宽 - 段长] 随机（随机空档）；
            # 段长超过槽宽时区间倒挂，钳到槽首；再钳严格递增，防止相邻段
            # 起点重合导致后段被跳过（current 单调递增追不上重合的 start）
            slot_lo = i * w
            slot_hi = (i + 1) * w - seg_len
            start = int(round(slot_lo + max(0.0, slot_hi - slot_lo) * random.random()))
            start = max(start, prev_start + 1)
            plan.append((start, seg_len))
            prev_start = start
            budget -= seg_len
            if budget <= 0:
                break
        return plan

    def auto_battle_plan(self, config: GeneralBattleConfig, remaining_count: int) -> None:
        """自动段规划（2026-09-09 一次性分段规划改版）：开局生成全计划，每场序言调用一次。

        旧版为逐段规划（打完一段再随机下一段起点，全段均匀随机易挤到后半程，
        前半程纯手动、后半程长时间挂自动）。新版开局一次性生成全部段的
        (起点, 段长)，起点经均等槽位铺开、段长在区间内随机，之后按序消费，
        不再逐段随机。

        双保险之一：配置未开或任务层未传剩余场次，整段功能静默跳过。
        """
        seg = self._seg_state()
        # 配置字段已拍平在 GeneralBattleConfig 上（无嵌套子模型）
        if not config.auto_battle_enable or remaining_count is None:
            # 配置未开或任务层未传剩余场次：整段功能静默跳过（双保险之一）
            return
        if not seg['enabled']:
            seg['enabled'] = True
            seg['total_left'] = config.auto_total_count
        if not seg['planned']:
            # 开局一次性规划：解析段长区间，生成全段计划
            lo, hi = parse_segment_range(config.auto_segment_count)
            # 规划要求：剩余场数容纳得下 [下限段长 + 1 场取消]，与旧版 M+1 对齐
            if seg['total_left'] > 0 and remaining_count >= lo + 1:
                seg['base'] = self.current_count  # start 从下一场（序言计数前）起数
                seg['plan'] = self._auto_plan_generate(
                    remaining_count, seg['total_left'], lo, hi)
                seg['plan_idx'] = 0
                seg['planned'] = True
                logger.info(f'Auto battle plan: {seg["plan"]}')

    def _auto_seg_current(self):
        """当前待执行段 (start, len)，无计划或已消费完返回 None。"""
        seg = getattr(self, '_auto_seg', None)
        if seg is None or not seg['enabled'] or not seg['planned']:
            return None
        if seg['plan_idx'] >= len(seg['plan']):
            return None
        return seg['plan'][seg['plan_idx']]

    def _auto_seg_reached(self) -> bool:
        """本场是否为自动段起点（run_general_battle 段分支查询一次）。

        2026-09-09 改版：判定基于规划基准 base——实际场次轴上的第
        base + start 场是起点（start 从 0 起数：0 = 规划后第一场即起点；
        本场序言计数后的 current_count == base + start 即到达）。
        """
        cur = self._auto_seg_current()
        if cur is None:
            return False
        start, seg_len = cur
        seg = getattr(self, '_auto_seg', None)
        if seg['total_left'] <= 0:
            return False
        return self.current_count == seg['base'] + start

    def _auto_seg_consume(self) -> None:
        """段执行完（auto_battle_run 返回）后推进计划索引。

        total_left 在段内由 auto_battle_run/ auto_battle_count_step 递减；
        此处只推进 plan_idx，不动计数——段未打满被中断时预算照扣（保守口径，
        与旧版"领取段即消耗"一致）。
        """
        seg = getattr(self, '_auto_seg', None)
        if seg is None:
            return
        seg['plan_idx'] += 1
        seg['planned'] = seg['plan_idx'] < len(seg['plan'])

    def auto_battle_count_step(self) -> None:
        """段内跨一次场次边界的单点计数同步（spec 4.5.2）。

        第 1 场的计数由 run_general_battle 序言完成，这里只补第 2~M 场；
        任务层口径（五倍券/组队进度）经钩子同步，保证三处计数一致。
        """
        self.current_count += 1
        # 与 run_general_battle 序言同格式记次：自动段内零输入翻场，
        # 日志上看不到 GENERAL BATTLE START 分隔线，靠此行核对场次进度
        logger.info(f"Current count: {self.current_count}")
        self._auto_seg['total_left'] -= 1
        # 段内零输入：stuck 检测靠页面翻转续命，每跨一场重挂长战斗计时。
        # 必须 clear 之后再 add，只 add 无效——stuck_record_add 只往
        # detect_record 塞名字、不碰 stuck_timer/stuck_timer_long，而
        # detect_record 是 set，重复 add 同一个标记是 no-op；BATTLE_STATUS_S
        # 在 stuck_long_wait_list 里只把上限从 60s 放宽到 300s，不是无限豁免，
        # 计时器照跑满 300s 就抛 GameStuckError。段内没有点击/滑动，
        # handle_control_check 这条唯一的自动清零路径也不会走到，于是唯一一次
        # clear 停在段起点 _auto_start 的那次点击上（2026-09-11 oas2 爬塔事故：
        # 每 ~16.5s 稳定翻一场，仍在段起点点击后精确 300s 被误判卡死重启两次）
        self.device.stuck_record_clear()
        self.device.stuck_record_add('BATTLE_STATUS_S')
        # 任务层钩子：Orochi 在此同步五倍券补次/组队心跳
        self.auto_battle_count_hook()

    def auto_battle_count_hook(self) -> None:
        """段内每完成一场的计数钩子：基类空实现，任务层覆盖以同步自己的口径。"""

    def auto_battle_remaining_now(self):
        """段内截断用的剩余次数口径：基类返回 None（无任务限制信息则不截断）。"""
        return None

    def _auto_start(self, retry: int = 2) -> bool:
        """开启自动战斗（spec 4.5.1）：识别开启按钮→点击→确认进入自动页。

        已处于自动页（上次残留）直接视为成功，不重复点击。
        """
        for _ in range(retry + 1):
            self.screenshot()
            if self.is_auto_battle_page():
                return True
            if self.appear(self.I_PAPER_TOSTART):
                self.click(self.C_PAPER_TOSTART)
                # 等页面切换，节奏拟人
                sleep(random.uniform(0.5, 1.0))
            else:
                # 按钮还没出现（还在进场动画），稍等再试
                sleep(0.3)
        logger.warning('Auto battle start failed, fallback to manual battle')
        return False

    def _auto_cancel(self, retry: int = 2) -> bool:
        """取消自动战斗（spec 4.5.3）：确认自动页→点击→确认按钮回归。"""
        for _ in range(retry + 1):
            self.screenshot()
            if not self.is_auto_battle_page():
                logger.info('Auto battle canceled (already manual)')
                return True
            self.click(self.C_PAPER_TOSTART)
            sleep(random.uniform(0.5, 1.0))
            # appear 基于 device.image（上一次截图的旧帧）：点击后必须重新截图刷新，
            # 否则查的是点击前的旧帧，按钮明明已回归却查不到，白白多试一轮
            self.screenshot()
            if self.appear(self.I_PAPER_TOSTART):
                logger.info('Auto battle canceled')
                return True
        logger.warning('Auto battle cancel failed, continue manual flow')
        return False

    def _auto_wait_battle_ui(self, timeout: float = 10.0) -> bool:
        """等待回到战斗过程界面：取消自动必须在战斗界面上点按钮，准备页/结算页上
        按钮不可见，直接判定会假成功。超时返回 False 由调用方告警兜底。"""
        timer = Timer(timeout).start()
        while not timer.reached():
            self.screenshot()
            if self.is_in_real_battle(False):
                return True
            sleep(0.3)
        return False

    def auto_battle_run(self) -> None:
        """自动段主体（spec 4.5）：点开自动→游戏连打 M 场（脚本零输入）→点回手动。

        返回时页面处于第 M+1 场的战斗过程界面（或准备页），battle_wait 继续
        手动流程。游戏自动战斗会自己完成结算与下一场挑战（fire），段内脚本
        除开/关自动两次点击外零输入，只截图识别、记次、计数同步。
        """
        seg = self._seg_state()
        # 旧版此处置 planned=False（领取段即消耗、逐段重新规划）；一次性计划版
        # 由 _auto_seg_consume 统一推进 plan_idx 与 planned，此处不再动状态
        # 段长取当前计划段（进入本函数前 _auto_seg_reached 已确认段有效）
        cur = self._auto_seg_current()
        m = cur[1] if cur is not None else seg['seg_len']

        def _settle_appear() -> bool:
            """结算页系模板共判 + 奖励框检测兜底（两处边界判定复用）。

            模板认的是具体图案（胜利鼓/失败/领奖图标），活动副本的奖励底色
            或结算动画中间帧可能全部失配——失配时段内看不到"结算页出现"，
            游戏自动翻进下一场后不会记次，段会卡死在等待结算。奖励框检测认
            网格本身（reward_grid_appear，与 settlement_click_grid 同一判据），
            与奖励内容无关，作为第二判据兜底。
            """
            return (self.win_appear(threshold=0.8)
                    or self.appear(self.I_FALSE, threshold=0.8)
                    or self.appear(self.I_REWARD, threshold=0.6)
                    or self.appear(self.I_REWARD_GOLD, threshold=0.8)
                    or self.reward_grid_appear())

        # ---- 1. 开启自动 ----
        if not self._auto_start():
            # 开启失败回退手动，本场照常打，不消耗 T
            return
        # 第 1 场的 T 消耗（其 current_count 已由 run_general_battle 序言计入）
        seg['total_left'] -= 1
        seg_done = 1
        waiting_settle = False   # 是否已见到本场的结算页
        fail_frames = 0          # 自动状态失配的连续帧数
        fail_since = None        # 失配起始时刻（累计时长防抖用）
        unknown_frames = 0       # 结算后连续未知页面帧数（既非战斗/准备也非结算）
        # ---- 2. 段内循环：零输入，只识别记次 ----
        while seg_done < m:
            self.screenshot()
            if not waiting_settle:
                # 中断监测只在战斗过程界面帧做：结算页帧 OCR 天然读不到数字，
                # 等结算期间不判定（实现修正，避免结算动画被误判为中断）
                if self.is_auto_battle_page():
                    fail_frames = 0
                    fail_since = None
                else:
                    fail_frames += 1
                    if fail_since is None:
                        fail_since = time.time()
                    if fail_frames >= 3 or time.time() - fail_since >= 1.5:
                        logger.warning(f'Auto battle interrupted at {seg_done}/{m}')
                        return
                # 场次边界第一步：结算页出现
                if _settle_appear():
                    waiting_settle = True
                    # 结算页帧不累计战斗界面失配：结算动画动辄数秒，fail_since 若
                    # 残留到结算后，回到战斗界面第 1 帧失配即满足时长分支，
                    # 1~2 帧失配就误判中断——进入结算时同步清零防抖
                    fail_frames = 0
                    fail_since = None
                    # 页面翻转即"未卡死"的证据，重挂一次长战斗计时。
                    # 同 auto_battle_count_step：必须 clear 再 add，只 add 不重置
                    # 计时器（stuck_record_add 只写 detect_record 集合）
                    self.device.stuck_record_clear()
                    self.device.stuck_record_add('BATTLE_STATUS_S')
            else:
                # 场次边界第二步：结算页过后回到战斗/准备界面 = 跨过一场
                if self.is_in_real_battle(False) or self.is_in_prepare(False):
                    unknown_frames = 0
                    self.auto_battle_count_step()
                    seg_done += 1
                    waiting_settle = False
                    # 计数偏差截断（spec 4.5.2）：剩余场数不够"段剩余+1 场取消"
                    remaining_now = self.auto_battle_remaining_now()
                    if remaining_now is not None and remaining_now <= m - seg_done:
                        logger.warning(f'Auto battle segment truncated at {seg_done}/{m}')
                        break
                elif _settle_appear():
                    # 结算页系模板还在（动画/翻页中）属于正常等待，不算未知界面
                    unknown_frames = 0
                else:
                    # 页面流失去未知界面（异常弹窗/跳转）：没有退出条件会段内死循环，
                    # 连续超过 30 帧告警退出，交 battle_wait/stuck 兜底
                    unknown_frames += 1
                    if unknown_frames > 30:
                        logger.warning(f'Auto battle lost in unknown ui at {seg_done}/{m}')
                        return
        # ---- 3. 段尾取消（按钮只在战斗过程界面出现，先等界面再取消） ----
        if self._auto_wait_battle_ui():
            self._auto_cancel()
        else:
            logger.warning('Auto battle cancel skipped: not in battle ui')

    def auto_battle_finish_sweep(self) -> None:
        """任务收尾兜底（spec 6.3 真机修订）：仅在战斗过程界面上判定与取消。

        is_auto_battle_page 是 OR 语义（按钮不在即自动中），主界面/准备页等
        非战斗页按钮天然不在，会被误判"自动中"并误点——真机验证发现任务
        收尾时页面几乎总是已回到主界面，在庭院坐标上连点三次 paper_tostart。
        必须先确认战斗界面（is_in_real_battle，判据见其 docstring）再做判定与取消。
        """
        self.screenshot()
        if not self.is_in_real_battle(False):
            # 非战斗界面：OR 判定不可信，也不存在可点的自动按钮
            return
        if self.is_auto_battle_page():
            logger.warning('Auto battle still on at task end, try cancel once')
            self._auto_cancel()

    def check_take_over_battle(self, is_screenshot: bool, config: GeneralBattleConfig) -> bool or None:
        """
        中途接入战斗，并且接管
        :return:  赢了返回True， 输了返回False, 不是在战斗中返回None
        """
        if is_screenshot:
            self.screenshot()
        if not self.is_in_battle():
            return None

        return self.run_general_battle(config=config)

    def check_lock(self, enable: bool, lock_image, unlock_image):
        """
        检测是否锁定队伍，
        :param enable:
        :param lock_image:
        :param unlock_image:
        :return:
        """
        if enable:
            logger.info("Lock team")
            while 1:
                self.screenshot()
                if self.appear(lock_image):
                    logger.info("Lock team")
                    break
                if self.appear_then_click(unlock_image, interval=1):
                    continue
        else:
            logger.info("Unlock team")
            while 1:
                self.screenshot()
                if self.appear(unlock_image):
                    break
                if self.appear_then_click(lock_image, interval=1):
                    continue

    def check_and_open_buff(self, buff: BuffClass or list[BuffClass] = None):
        """
        检测是否开启buff
        :param buff:
        :return:
        """
        if not buff:
            return
        logger.info(f'Open buff {buff}')
        self.ui_click(self.I_BUFF, self.I_CLOUD, interval=2)
        if isinstance(buff, BuffClass):
            buff = [buff]
        match_method = {
            BuffClass.AWAKE: (self.awake, True),
            BuffClass.SOUL: (self.soul, True),
            BuffClass.GOLD_50: (self.gold_50, True),
            BuffClass.GOLD_100: (self.gold_100, True),
            BuffClass.EXP_50: (self.exp_50, True),
            BuffClass.EXP_100: (self.exp_100, True),
            BuffClass.AWAKE_CLOSE: (self.awake, False),
            BuffClass.SOUL_CLOSE: (self.soul, False),
            BuffClass.GOLD_50_CLOSE: (self.gold_50, False),
            BuffClass.GOLD_100_CLOSE: (self.gold_100, False),
            BuffClass.EXP_50_CLOSE: (self.exp_50, False),
            BuffClass.EXP_100_CLOSE: (self.exp_100, False),
        }
        for b in buff:
            func, is_open = match_method[b]
            func(is_open)
            time.sleep(0.1)
        logger.info(f'Open buff success')
        while 1:
            self.screenshot()
            if not self.appear(self.I_CLOUD):
                break
            if self.appear_then_click(self.I_BUFF, interval=1):
                continue

    def boss_mark(self, enable=True) -> bool:
        if not enable or self._boss_mark_flag:
            return False
        if self.ocr_appear(self.O_BOSS_MARK):
            self.screenshot()
            if self.ocr_appear(self.O_BOSS_MARK):
                self._boss_mark_flag = True
                logger.info('Boss marked')
                self.device.stuck_record_add('BATTLE_STATUS_S')
                return True
        if self.device.click_record.count(str(self.O_BOSS_MARK)) >= 3:
            self._boss_mark_flag = True
            logger.info('Boss mark skipped due to maybe no boss')
            self.device.stuck_record_add('BATTLE_STATUS_S')
            return False
        if self.click(self.O_BOSS_MARK, interval=1.8):
            return False
        return False

    def boss_mark_reset(self):
        self._boss_mark_flag = False


if __name__ == '__main__':
    from module.config.config import Config
    from module.device.device import Device

    c = Config('oas1')
    d = Device(c)
    t = GeneralBattle(c, d)
    self = t
    # t.check_buff([BuffClass.EXP_50, BuffClass.GOLD_50])

    img = cv2.imread(r"E:\preset3.png")
    img = cv2.cvtColor(img, cv2.COLOR_BGR2RGB)
    self.device.image = img


    def get_unselect_color(tmp1, tmp2, tmp3, size):
        # 获取未选择分组的颜色，3组之中必定存在两个颜色相似
        # area 参数格式是（x1,y1,x2,y2）
        color_1 = get_color(self.device.image,
                            (tmp1.roi_back[0], tmp1.roi_back[1],
                             tmp1.roi_back[0] + size[0], tmp1.roi_back[1] + size[1]))
        color_2 = get_color(self.device.image,
                            (tmp2.roi_back[0], tmp2.roi_back[1],
                             tmp2.roi_back[0] + size[0], tmp2.roi_back[1] + size[1]))
        color_3 = get_color(self.device.image,
                            (tmp3.roi_back[0], tmp3.roi_back[1],
                             tmp3.roi_back[0] + size[0], tmp3.roi_back[1] + size[1]))

        if color_similar(color_1, color_2):
            return color_1
        if color_similar(color_2, color_3):
            return color_2
        return color_3


    color_size = [self.C_PRESET_GROUP_1.roi_back[2],
                  self.C_PRESET_GROUP_1.roi_back[3]]
    unselected_color = get_unselect_color(self.C_PRESET_GROUP_1, self.C_PRESET_GROUP_2, self.C_PRESET_GROUP_3,
                                          size=color_size)
    print("")
    color_size = [5, 5]
    unselected_color = get_unselect_color(self.C_PRESET_TEAM_1, self.C_PRESET_TEAM_2, self.C_PRESET_TEAM_3,
                                          size=color_size
                                          )
    print("")
