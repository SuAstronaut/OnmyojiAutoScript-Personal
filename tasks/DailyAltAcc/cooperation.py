# This Python file uses the following encoding: utf-8
import re
import time
from datetime import datetime
from typing import List

from module.atom.image import RuleImage
from module.atom.ocr import RuleOcr
from module.logger import logger
from tasks.GameUi.page import page_main, page_guild
from tasks.DailyAltAcc.assets import DailyAltAccAssets
from tasks.DailyAltAcc.utils import DailyAltAccBase
from tasks.WantedQuests.assets import WantedQuestsAssets
from tasks.WantedQuests.config import CooperationType
from tasks.DailyAltAcc.config import MSGType
from tasks.DailyAltAcc.stat_log import StatEvent
from tasks.Utils.coop_records import record_cooperation


# 协作时间戳格式。刻意与另一处协作记录的时间字段逐字一致，让两类来源里的时间长得
# 一样、下游按同一套规则读；真正不能复制的刷新窗口规则（05:00 / 18:00）没有被复制。
COOP_TS_FORMAT = '%Y-%m-%d %H:%M:%S'


def _parse_cooperation_monster(raw_text: str, prefix: str) -> str:
    """从一行协作目标 OCR 文本中提取怪物名称。"""
    text = re.sub(r"\s+", "", str(raw_text or ""))
    text = re.sub(r"\d+[/／]\d+$", "", text)
    if text.startswith(prefix):
        text = text[len(prefix):]
    return text.strip()


def _parse_real_cooperation_monster(raw_text: str) -> str:
    """从现世协作的“击败N个XXX”目标中提取怪物名称。"""
    text = re.sub(r"\s+", "", str(raw_text or ""))
    text = re.sub(r"\d+[/／]\d+$", "", text)
    match = re.fullmatch(r"击败\d+个(.+)", text)
    return match.group(1).strip() if match else ""


# ---- 协作板单区域批量识别常量 ----
# 三张卡片所在的整块区域，与 I_NET_NORMAL_FLAG 的 roi_back 一致。8 个模板共用这
# 一个搜索区域：实测该区域、更小的卡片区、乃至全屏，三者结果逐条完全一致。
COOP_CARD_BAND = (130, 110, 1022, 508)
# 锚点（协/享/邀请）的槽位基准 x。协/享 是同一槽位的互斥标记，实测标称左上角为
# 槽1=154 / 槽2=453 / 槽3=752，槽间距精确 299（协字 bbox 158→457→752 验证）。
# 邀请按钮的标称 x 是 137/462/754，与标记不同位，偏差由归位容差吸收。
COOP_ANCHOR_SLOT_X = (154, 453, 752)
# 锚点标称 y：协 288 / 享 293，同一条水平带
COOP_ANCHOR_SLOT_Y = 288
# 类型图标的槽位基准 x = 奖励区中心（奖励格起点 195/490/790 + 约 90）。
# 必须与锚点基准分开：类型图标与锚点标记的物理位置相差 130~140px，共用一套基准
# 会把 gold@290 / sushi@590 这类命中挤出容差，导致整个槽位识别不出类型。
COOP_TYPE_SLOT_X = (285, 580, 880)
# 槽位归位容差：命中点与基准的 x 距离超过该值即判为不属于任何槽位
COOP_SLOT_TOLERANCE = 120
# 锚点优先级：协/享 不随邀请消失，比邀请按钮可靠，故优先于邀请按钮
COOP_ANCHOR_PRIORITY = {'协': 0, '享': 1, '邀请': 2}
# 类型优先级：食物协作里也有金币奖励，故金币排在最后（沿用改造前的判定顺序）
COOP_TYPE_PRIORITY = {'jade': 0, 'dog_food': 1, 'cat_food': 2, 'sushi': 3, 'gold': 4}
# 协作目标文本的标称 ROI（槽1 位置）。槽间横向间距 300（180→480→780），实际位置
# 由「槽位偏移 + 标记抖动」算出，不按槽位写死。
COOP_DISCOVERER_BASE = (180, 403, 200, 30)
COOP_FRIEND_BASE = (180, 448, 200, 30)
# 现世体协只有一行目标文本，位置落在普通协作两行之间
COOP_REAL_MONSTER_BASE = (180, 425, 200, 32)
# 目标文本 ROI 的槽间横向间距（奖励格起点 195/490/790 → 180/480/780）
COOP_TARGET_SLOT_PITCH = 300
# 作位置基准的锚点模板在槽1 的标称左上角（实测）。只有 协/享 入表：它们固定在
# 卡片同一横向位置，槽间距精确 299；邀请按钮的横向位置随卡片文案长短浮动
# （137/462/754，间距 325/292），拿它当基准会引入最多 26px 的固定偏移。
COOP_ANCHOR_ORIGIN = {'协': (154, 288), '享': (159, 293)}
# 协/享 标记的槽间横向间距（协字 bbox 158→457→752 精确等差）
COOP_ANCHOR_PITCH = 299


class Cooperation(DailyAltAccBase):
    # 多账号任务注入同一持久表、账号身份和轮次；单任务无上下文时只产出日志/消息。
    _coop_store = None
    _account_identity = None
    _coop_round_id = None
    # 本次「找协作」取帧的时刻，由 get_cooperation_info 在 screenshot() 之后立刻盖下。
    # 协作的归属窗口是 05:00 / 18:00 两档刷新，窗口由**识别帧**决定而不是落盘时刻：
    # 从识别到落盘隔着点返回与 ui_goto，有 1~5 秒，恰好跨过刷新点时按落盘时刻取
    # 会把一条版面已经刷掉的协作归进下一个窗口，它于是在下游待办里活满 11 小时。
    # None 表示没有取帧来源（单测、手工构造），_coop_emit 退化为当下时刻。
    _coop_scan_at: datetime = None

    # 单区域批量识别使用的 8 个模板。这 8 个条目只被本文件引用，所以
    # match_all_any 用 roi 参数改写 roi_back 时不会波及别的任务——WantedQuests
    # 读的是带 _1/_2/_3 后缀、roi_back 为固定区域的另一组条目，两者是不同的对象。
    COOP_ANCHOR_RULES = {
        '协': DailyAltAccAssets.I_NORMAL_FLAG,
        '享': DailyAltAccAssets.I_REAL_FLAG,
        '邀请': WantedQuestsAssets.I_WQ_INVITE,
    }
    COOP_TYPE_RULES = {
        'jade': WantedQuestsAssets.I_WQ_COOPERATION_TYPE_JADE,
        'dog_food': WantedQuestsAssets.I_WQ_COOPERATION_TYPE_DOG_FOOD,
        'cat_food': WantedQuestsAssets.I_WQ_COOPERATION_TYPE_CAT_FOOD,
        'sushi': WantedQuestsAssets.I_WQ_COOPERATION_TYPE_SUSHI,
        'gold': WantedQuestsAssets.I_WQ_COOPERATION_TYPE_GOLD,
    }

    # 类型信号的输出规格。日志文案与标签沿用改造前的逐字表述（含 "find  jade
    # cooperation " 里的双空格），前端展示与推送依赖这些字符串，不得改动。
    # 狗粮/猫粮/金币改造前硬编码 real=False、标签只有一份；改为忠实反映锚点后
    # 「享」也会出现在这三类上（实机样本里撞到 5 次现世金币），故补上「现世」
    # 标签，汇总侧的分类表同步扩到 10 类（见 tasks/Utils/coop_summary.coop_category_order）。
    COOP_TYPE_SPEC = {
        'jade': {
            'type': CooperationType.Jade, 'event_type': 'jade', 'notify': True,
            'log': 'find  jade cooperation ', 'log_real': 'find real jade cooperation ',
            'label': '普通勾协', 'label_real': '现世勾协',
        },
        'dog_food': {
            'type': CooperationType.Food, 'event_type': 'food', 'notify': False,
            'log': 'find dog food cooperation ', 'log_real': None,
            'label': '狗粮协作', 'label_real': '现世狗粮协作', 'food_kind': 'dog',
        },
        'cat_food': {
            'type': CooperationType.Food, 'event_type': 'food', 'notify': False,
            'log': 'find cat food cooperation ', 'log_real': None,
            'label': '猫粮协作', 'label_real': '现世猫粮协作', 'food_kind': 'cat',
        },
        'sushi': {
            'type': CooperationType.Sushi, 'event_type': 'sushi', 'notify': True,
            'log': 'find  sushi cooperation ', 'log_real': 'find real sushi cooperation ',
            'label': '普通体协', 'label_real': '现世体协',
        },
        'gold': {
            'type': CooperationType.Gold, 'event_type': 'gold', 'notify': False,
            'log': 'find gold cooperation ', 'log_real': None,
            'label': '金币协作', 'label_real': '现世金币协作',
        },
    }

    # 目标文本读法：'normal' 读双行（自己击败X / 好友击败Y），'real' 读单行
    # （击败N个X）。键为 (类型信号, 是否现世)，缺省即不读该组合的目标文本。
    # 沿用改造前行为：勾协只在普通时读、体协现世读单行普通读双行、其余类型不读
    # （读目标要走 OCR，非必要不付出这个开销）。
    COOP_TARGET_READ = {
        ('jade', False): 'normal',
        ('sushi', True): 'real',
        ('sushi', False): 'normal',
    }

    @staticmethod
    def _shift_roi(roi: tuple | list, delta: tuple[int, int]) -> tuple[int, int, int, int]:
        x, y, width, height = roi
        dx, dy = delta
        return int(x + dx), int(y + dy), int(width), int(height)

    @staticmethod
    def _coop_target_shift(anchor, slot: int) -> tuple[int, int]:
        """从锚点实测位置反推该槽协作目标文本 ROI 的平移量。

        @param anchor: 该槽胜出的锚点 (信号名, 得分, x, y)
        @return: (dx, dy)，用于 _shift_roi 平移 COOP_*_BASE

        拆成「槽位偏移 + 标记抖动」两段：槽位偏移是标称几何（每槽 300），抖动是
        实测位置相对标称的偏差（实测样本上恒为 0，保留作为版面轻微偏移的兜底）。
        位置基准只用 协/享 —— 走到邀请说明两个标记都没匹配上，此时退回标称位置。
        """
        anchor_name, _score, anchor_x, anchor_y = anchor
        dx, dy = COOP_TARGET_SLOT_PITCH * slot, 0
        origin = COOP_ANCHOR_ORIGIN.get(anchor_name)
        if origin is not None:
            dx += anchor_x - origin[0] - COOP_ANCHOR_PITCH * slot
            dy = anchor_y - origin[1]
        return int(dx), int(dy)

    @staticmethod
    def _coop_slot_of(x: int, basis: tuple) -> int | None:
        """按 |x - 基准| 把命中归位到最近的槽位；超出容差返回 None。"""
        slot, best = None, None
        for index, base_x in enumerate(basis):
            distance = abs(x - base_x)
            if best is None or distance < best:
                slot, best = index, distance
        return slot if best is not None and best <= COOP_SLOT_TOLERANCE else None

    @classmethod
    def _coop_collect(cls, image, rules: dict, basis: tuple, priority: dict) -> tuple[dict, dict]:
        """在同一区域内批量匹配一组模板，并把命中按 x 归位到卡片槽位。

        改造前每个模板要 match 三次（每槽一次），这里每个模板只匹配一次、在整块
        区域里一次找出全部命中再做归位，匹配次数从 9+15 降到 8，且完全没有循环。

        @param rules: {信号名: RuleImage}
        @param basis: 槽位基准 x 三元组
        @param priority: 同槽位多命中时的优先级，数值小者胜出
        @return: ({槽位: (信号名, 得分, x, y)}, {信号名: [(得分, x, y, 槽位), ...]})
                 槽位为 None 表示该命中离所有基准都超过容差、已丢弃。
        """
        winners, hits = {}, {}
        for name, rule in rules.items():
            # roi 参数会改写 rule.roi_back（module/atom/image.py），这 8 个条目专供
            # 本处批量识别、不被别的任务引用，故该副作用无害。
            # 必须用 match_all_any：match_all 返回所有 ≥阈值的像素点，一个图标就是
            # 几百个候选框；match_all_any 内部走 cv2.dnn.NMSBoxes 去重。
            matches = rule.match_all_any(image, roi=COOP_CARD_BAND, nms_threshold=0.3)
            hits[name] = [
                (score, x, y, cls._coop_slot_of(x, basis))
                for score, x, y, _width, _height in matches
            ]
            for score, x, y, slot in hits[name]:
                if slot is None:
                    continue
                current = winners.get(slot)
                if current is None or priority[name] < priority[current[0]]:
                    winners[slot] = (name, score, x, y)
        return winners, hits

    def _coop_read_targets(self, type_name: str, real: bool, slot: int, anchor) -> dict:
        """按类型与现世标记读取该槽的协作目标文本；不需要读的组合直接返回空。"""
        read_mode = self.COOP_TARGET_READ.get((type_name, real))
        if read_mode is None:
            return {}
        if read_mode == 'real':
            return self._read_real_sushi_target(slot, anchor)
        return self._read_normal_cooperation_targets(slot, anchor)

    def _coop_invite_button(self, slot: int, invite_hits: list):
        """按该槽实测到的邀请按钮位置构造返回用的 RuleImage。

        改造前直接返回 I_WQ_INVITE_{n} 实例（appear() 已把实测位置写回 roi_front）；
        改成单实例批量识别后不再有「每槽一个固定条目」，故按命中位置就地构造。
        本任务不消费该字段，仅为保持 @return 契约。卡片已邀请时按钮不存在，
        退回该槽的锚点位置。
        """
        source = self.COOP_ANCHOR_RULES['邀请']
        hit = next((item for item in invite_hits if item[3] == slot), None)
        x, y = (hit[1], hit[2]) if hit else (COOP_ANCHOR_SLOT_X[slot], COOP_ANCHOR_SLOT_Y)
        return RuleImage(
            roi_front=(x, y, source.roi_front[2], source.roi_front[3]),
            roi_back=COOP_CARD_BAND,
            method=source.method,
            threshold=source.threshold,
            file=source.file,
        )

    @staticmethod
    def _ocr_single(image, roi: tuple[int, int, int, int], name: str) -> str:
        return RuleOcr(
            mode="Single", roi=roi, area=roi, method="Default", keyword="", name=name,
        ).ocr(image)

    @staticmethod
    def _normal_target_result(discoverer_raw: str, friend_raw: str) -> dict:
        discoverer = _parse_cooperation_monster(discoverer_raw, "自己击败")
        friend = _parse_cooperation_monster(friend_raw, "好友击败")
        result = {}
        if discoverer:
            result["discoverer_monster"] = discoverer
        if friend:
            result["friend_monster"] = friend
        if discoverer or friend:
            result["monster_text"] = "&".join(
                monster for monster in (discoverer, friend) if monster
            )
        return result

    def _read_normal_cooperation_targets(self, slot: int, anchor) -> dict:
        """读取普通协作的双目标（自己击败X / 好友击败Y）。

        区域由「槽位偏移 + 标记抖动」一次算出。改造前是先试固定 ROI、失败再按
        邀请锚点平移重试一次；锚点位置本身已是实测值，固定 ROI 那一步是多余的，
        故这里只保留一次带偏移的读取，不再需要二次 fallback。
        """
        image = getattr(getattr(self, "device", None), "image", None)
        if image is None:
            return {}
        shift = self._coop_target_shift(anchor, slot)
        try:
            result = self._normal_target_result(
                self._ocr_single(
                    image,
                    self._shift_roi(COOP_DISCOVERER_BASE, shift),
                    f"wq_cooperation_discoverer_slot_{slot + 1}",
                ),
                self._ocr_single(
                    image,
                    self._shift_roi(COOP_FRIEND_BASE, shift),
                    f"wq_cooperation_friend_slot_{slot + 1}",
                ),
            )
        except Exception as exc:
            logger.warning(f"普通协作目标 OCR 失败(slot={slot + 1}): {exc}")
            result = {}
        logger.info(
            f"normal cooperation slot={slot + 1} shift={shift} "
            f"discoverer={result.get('discoverer_monster')!r} "
            f"friend={result.get('friend_monster')!r}"
        )
        return result

    def _read_real_sushi_target(self, slot: int, anchor) -> dict:
        """读取现世体协的单目标（击败N个X）。

        改造前只读槽1/槽2 —— O_WQ_REAL_COOPERATION_MONSTER_3 这个资产根本不存在，
        调用处因此带着 index < 2 的绕行判断。改按锚点位置整体平移后，区域是算出来
        的，槽3 同样能读，那个绕行判断随之取消。
        """
        image = getattr(getattr(self, "device", None), "image", None)
        if image is None:
            return {}
        shift = self._coop_target_shift(anchor, slot)
        try:
            monster = _parse_real_cooperation_monster(self._ocr_single(
                image,
                self._shift_roi(COOP_REAL_MONSTER_BASE, shift),
                f"wq_real_cooperation_monster_slot_{slot + 1}",
            ))
        except Exception as exc:
            logger.warning(f"现世体协目标 OCR 失败(slot={slot + 1}): {exc}")
            monster = ""
        logger.info(
            f"real sushi cooperation slot={slot + 1} shift={shift} monster={monster!r}"
        )
        return {"monster_text": monster} if monster else {}

    def run_cooperation(self):   
        #self.account_info =[] #self.get_account_info()
        # 打开悬赏封印 界面
        self.screenshot()
        if self.ui_get_current_page() != page_main:
            self.ui_goto(page_main)
        self.screenshot()
        retry_count = 0
        while 1:    
            self.screenshot()
            if  retry_count >3:
                self.screenshot()
                self.ui_goto(page_guild)
                time.sleep(1)
                self.screenshot()
                if self.ui_get_current_page() != page_main:
                    self.ui_goto(page_main)
            if self.appear(WantedQuestsAssets.I_WQ_SEAL,interval=1) or self.appear(WantedQuestsAssets.I_WQ_DONE,interval=1):
                break
            retry_count += 1
            time.sleep(1)
        while 1:
            self.screenshot()
            if self.appear(WantedQuestsAssets.I_TRACE_ENABLE) or self.appear(WantedQuestsAssets.I_TRACE_DISABLE) or self.appear(self.I_UI_BACK_RED):
                break
            if self.appear_then_click(WantedQuestsAssets.I_WQ_SEAL, interval=1):
                continue
            if self.appear_then_click(WantedQuestsAssets.I_WQ_DONE, interval=1):
                continue
        self.get_cooperation_info()
        self.screenshot()
        if self.appear(self.I_UI_BACK_RED):
            self.click(self.I_UI_BACK_RED)
            time.sleep(1)
        self.screenshot()
        if self.ui_get_current_page() != page_main:
            self.ui_goto(page_main)

    def get_account_info(self):
        self.screenshot()
        if self.ui_get_current_page() != page_main:
            self.ui_goto(page_main)
        retry_count = 0
        while 1:
            self.screenshot()
            if retry_count > 5:
                logger.info("get account info failed")
                return None
            if self.appear(self.I_PAGE_ACCOUNT):
                account_info = self.O_ACC_NAME.ocr(self.device.image)
                logger.info(f"get account info : {account_info}")
                break
            else:
                self.click(self.C_TO_ACCOUNT)
                retry_count += 1
                time.sleep(1.5)
                continue
        retry_count = 0
        while 1:
            self.screenshot()
            if retry_count > 5:
                self.ui_goto(page_main)
                break
            if self.appear_then_click(self.I_UI_BACK_RED, interval=2):
                retry_count += 1
                continue
            if self.ui_get_current_page()==page_main:
                break
            if not self.appear(self.I_UI_BACK_RED):
                self.screenshot()
                if self.ui_get_current_page()==page_main:
                    self.ui_goto(page_main)
                break
        return account_info

    def _coop_found_at(self) -> str:
        """本次识别帧的时刻字符串，见 ``_coop_scan_at`` 的说明。

        退化分支只在没走 get_cooperation_info 的调用里会走到（单测、手工构造）：
        拿不出取帧时刻时，当下是最接近的估计——比「不写时间」好得多，下游把没有
        采集时间的协作判为不可判定、排除出供需，等于白识别一场。
        """
        return (self._coop_scan_at or datetime.now()).strftime(COOP_TS_FORMAT)

    def _coop_emit(self, type_name: str, real: bool, cooperation: dict, total=None) -> None:
        """产出该槽的日志、推送与归档事件。

        日志文案与推送文案沿用改造前的逐字表述，前端展示与推送逻辑依赖它们；
        推送范围也保持原样——只有勾协与体协推送，其余类型只归档。

        事件里带 ``found_at``：识别帧时刻随事件一起走，持久表与 STAT 不再
        各自记时。表内同时保存轮次和卡槽快照，窗口归属只有一个来源。
        """
        spec = self.COOP_TYPE_SPEC[type_name]
        label = spec['label_real'] if real else spec['label']
        logger.info(spec['log_real'] if (real and spec['log_real']) else spec['log'])
        event = {"type": spec['event_type'], "real": real, "label": label,
                 "found_at": self._coop_found_at()}
        if 'slot' in cooperation:
            # 卡槽只用于保存本轮逐卡推送快照，不改变管家已有的协作去重 ID。
            event['slot'] = cooperation['slot']
        if 'food_kind' in spec:
            event["food_kind"] = spec['food_kind']
        event.update({
            key: cooperation[key]
            for key in ("discoverer_monster", "friend_monster", "monster_text")
            if cooperation.get(key)
        })
        self.msg.append([MSGType.cooperation, event])
        emit_stat = getattr(self, 'emit_stat', None)
        if emit_stat and total is not None:
            # 日志、持久表和推送用同一事件；先写日志，持久化失败时仍有原始证据。
            emit_stat(
                StatEvent.COOP, ctype=event['type'], real=event['real'], total=total,
                found_at=event['found_at'], food_kind=event.get('food_kind'),
                discoverer_monster=event.get('discoverer_monster', ''),
                friend_monster=event.get('friend_monster', ''),
                slot=event.get('slot'),
            )
        record_cooperation(event, self._coop_store, self._account_identity,
                           round_id=self._coop_round_id)
        if spec['notify']:
            self.push_notify(content=f"    发现{label}", title="协作任务提醒")

    def get_cooperation_info(self) -> List:
        """
            获取协作任务详情
        @return: 协作任务类型与邀请按钮
        """
        self.screenshot()
        # 取帧即记时刻：下面是纯图像处理（模板匹配 + OCR），不再截图，所以这一屏
        # 识别出的协作都属于这一帧。落到 _coop_scan_at 供 _coop_emit 与 STAT 共用，
        # 而不是各自取 datetime.now()——那会让「落盘的时间」和「日志里的时间」差出
        # 几毫秒，恰好跨过 05:00 / 18:00 时同一张协作会被算进两个窗口。
        self._coop_scan_at = datetime.now()
        image = getattr(getattr(self, "device", None), "image", None)
        retList = []
        if image is None:
            logger.warning("协作识别取帧失败，本屏按没有协作任务处理")
            logger.info(f"get cooperation size {len(retList)}")
            return retList

        # ① 8 个模板各在整块协作板区域内匹配一次，一次性拿到全部命中（坐标为绝对值）
        anchor_slots, anchor_hits = self._coop_collect(
            image, self.COOP_ANCHOR_RULES, COOP_ANCHOR_SLOT_X, COOP_ANCHOR_PRIORITY)
        logger.info(f"[协作锚点] 协={len(anchor_hits['协'])} 享={len(anchor_hits['享'])}"
                    f" 邀请={len(anchor_hits['邀请'])}")
        # 整板无锚点 = 本屏没有协作任务。实测 103 张样本里 58 张如此，是多数情况
        # 而不是异常，故用 info 记录（设计文档原写 warning，按实测数据下调）。
        if not anchor_slots:
            logger.info("本屏无协/享/邀请标记，判定为没有协作任务")
            logger.info(f"get cooperation size {len(retList)}")
            return retList
        type_slots, _ = self._coop_collect(
            image, self.COOP_TYPE_RULES, COOP_TYPE_SLOT_X, COOP_TYPE_PRIORITY)
        # 有锚点且有类型的槽位才会产出事件；每条 STAT 沿用本屏总数口径。
        total = len(anchor_slots.keys() & type_slots.keys())

        # ② 逐槽产出。类型结果只在同时存在锚点的槽位被采纳 —— 类型模板本身不能
        # 证明「这是协作卡」：实测 sushi 在非协作卡的橙色鱼籽奖励上能得 0.9735，
        # 越过 0.8 阈值；锚点先行是唯一可靠的过滤（103 张样本上拦下 252 次误命中）。
        # 另：改造前的判据是「邀请按钮 或 享字」，一张已邀请的普通协作卡会让循环
        # break、同屏其后的卡片被一起跳过；现在只跳过没有锚点的槽位，不再提前终止。
        for slot in sorted(anchor_slots):
            anchor_name, anchor_score, anchor_x, anchor_y = anchor_slots[slot]
            logger.info(f"[协作锚点] slot={slot} {anchor_name} "
                        f"score={anchor_score:.4f} @({anchor_x},{anchor_y})")
            typed = type_slots.get(slot)
            if typed is None:
                logger.warning(f"槽{slot + 1} 有协作标记({anchor_name})但类型模板全部失配，跳过该槽")
                continue
            type_name, type_score, type_x, type_y = typed
            logger.info(f"[协作类型] slot={slot} {type_name} "
                        f"score={type_score:.4f} @({type_x},{type_y})")
            # 现世标记直接由胜出的锚点决定：协→普通、享→现世；走到邀请说明
            # 协/享 都没匹配上，按普通处理
            real = anchor_name == '享'
            cooperation = {
                'type': self.COOP_TYPE_SPEC[type_name]['type'],
                'inviteBtn': self._coop_invite_button(slot, anchor_hits['邀请']),
                'real': real,
                'slot': slot,
            }
            cooperation.update(
                self._coop_read_targets(type_name, real, slot, anchor_slots[slot]))
            retList.append(cooperation)
            self._coop_emit(type_name, real, cooperation, total=total)

        logger.info(f"get cooperation size {len(retList)}")
        return retList


if __name__ == "__main__":
    from module.config.config import Config
    from module.device.device import Device
    c = Config('oas2')
    d = Device(c)
    self = Cooperation(c, d)
    self.screenshot()
    self.run_cooperation()
