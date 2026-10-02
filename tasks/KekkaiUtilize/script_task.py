# This Python file uses the following encoding: utf-8
# @author runhey
# github https://github.com/runhey
import re
import time
from cached_property import cached_property
from datetime import timedelta, datetime

from module.base.timer import Timer
from module.atom.image_grid import ImageGrid
from module.atom.click import RuleClick
from module.logger import logger
from module.exception import TaskEnd

from tasks.GameUi.game_ui import GameUi
from tasks.Utils.config_enum import ShikigamiClass
from tasks.KekkaiUtilize.assets import KekkaiUtilizeAssets
from tasks.KekkaiUtilize.config import UtilizeRule, SelectFriendList
from tasks.KekkaiUtilize.utils import CardClass
from tasks.Component.ReplaceShikigami.replace_shikigami import ReplaceShikigami
from tasks.GameUi.page import page_main, page_guild, page_guild_realm
import random
from tasks.KekkaiUtilize.fail_streak import EntryFailStreak
from tasks.Pets.script_task import ScriptTask as Pets
""" 结界蹭卡 """


class ScriptTask(GameUi, ReplaceShikigami, KekkaiUtilizeAssets):
    # 搜索输入框未获得焦点时显示的占位文字
    PRIORITY_NAME_PLACEHOLDER = '请输入好友昵称或备注'
    # 好友列表右侧边界，动态选中标记识别区以此坐标收口
    PRIORITY_LIST_RIGHT = 640
    # 实测当前卡片中心与选中标记中心相差不超过7像素，相邻行至少相差100像素
    CARD_SELECTED_Y_THRESHOLD = 40
    # 未选中目标卡片时最多等待两秒，识别到选中标记后立即继续
    CARD_SELECTION_TIMEOUT = 2
    # O_CHECK_OPEN 命中这些状态说明该好友结界无法寄养，不必再识别收益
    PRIORITY_CARD_UNAVAILABLE_KEYWORDS = ('未公开', '已占用')
    last_best_index = 99
    utilize_add_count = 0
    ap_max_num = 0
    jade_max_num = 0
    first_utilize = True
    # 优先搜索中未达标好友的结界卡数值记录：{好友名: {'zone': 区服, '斗鱼': 值, '太鼓': 值}}
    priority_friend_records: dict = {}
    # 选卡阶段内部是否已完成寄养（搜索优先好友路径会自行进结界上式神）
    utilized_in_select = False
    # 本次「进寄养」尝试中，育成界面显示已在寄养中（不需要再进寄养界面）
    utilize_already_active = False
    # 本次运行是否已计入一次进入失败：失败收尾有的 raise TaskEnd 直接结束，有的
    #（没合适卡）return 后还会走完正常收尾，run() 末尾要靠它区分该不该清计数
    entry_failed_this_run = False
    msg: list = []
    # 进入结界入口/子界面失败后的延后重试间隔（寄养与挂卡共用）
    REALM_ENTRY_FAIL_DELAY = timedelta(minutes=5)
    # 进入结界子界面（寄养/挂卡）的重试节奏
    REALM_ENTRY_CLICK_MAX = 4    # 每步最多点几次区域
    REALM_ENTRY_ROUND_MAX = 3    # 「回寮 -> 重进结界」最多几轮
    # 点击区域后等待目标页面出现的上限（秒）：同一坐标在目标页面上往往是关闭
    # 按钮，抢在页面打开之前重复点击会把刚打开的页面关掉。页面一出现立即继续，
    # 不必等满整个上限
    REALM_ENTRY_CLICK_INTERVAL = 3.0
    # 从寮进结界的转场动画时长（秒）：页面判据会在动画中途就成立，等它放完再判皮肤
    REALM_ANIMATION_WAIT = 2.0
    # 「放置好友寄养」加号按钮的三种状态。育成界面上它是原版亮度；寄养界面打开后
    # 覆盖层的半透明遮罩会把它压暗（实测 RGB 均值 174,163,145 → 72,68,61，约 41%
    # 亮度），而两种状态下模板相关度都很高（变暗后仍有 0.857）—— 形状判定分不出来，
    # 只有亮度能区分。这正是「加号仍能识别、但比原版暗」的由来。
    UTILIZE_ADD_BRIGHT = 'bright'   # 育成界面原版亮度，可以点
    UTILIZE_ADD_DIM = 'dim'         # 寄养界面已打开（被遮罩压暗），不能再点
    UTILIZE_ADD_ABSENT = 'absent'   # 加号不存在，已经在寄养中
    # 变暗判定阈值：现场区域亮度 / 模板亮度 低于该比例即认为被遮罩压暗
    UTILIZE_ADD_DIM_RATIO = 0.6
    # 加号状态连续多少帧保持一致才算稳定（单帧会被转场动画、飘字特效干扰）
    UTILIZE_ADD_STABLE_FRAMES = 3

    def run(self):
        con = self.config.kekkai_utilize.utilize_config
        # 检查是否处于禁止运行时间段，命中则跳过本次运行
        self.check_forbidden_time('KekkaiUtilize', con.forbidden_time_enable, con.forbidden_time_range)
        self.msg = []
        self.ui_get_current_page()
        if not self.ui_goto(page_guild):
            # 连寮主页都到不了，后面所有步骤都会落在错误页面上
            self.realm_entry_failed('寮')
        logger.info(f'开始蹭卡{self.config.kekkai_utilize.utilize_config.utilize_rule}')
        # 进入寮结界
        if not self.goto_realm():
            self.realm_entry_failed('寮结界')
        # 育成界面去蹭卡
        if con.utilize_enable:
            self.check_utilize_add()

        # 查看育成满级
        if con.exchange_before:
            self.check_max_lv(con.shikigami_class)
        # 检查蹭卡收获
        if con.guild_ap_enable:
            self.check_utilize_harvest()
        # 收体力盒子或者是经验盒子
        self.check_box_ap_or_exp(con.box_ap_enable, con.box_exp_enable, con.box_exp_waste)
        # 收取寮资金和体力
        if con.guild_assets_enable:
            self.recive_guild_ap_or_assets(con.harvest_guild_max_times)
        if not con.utilize_enable:
            self.set_next_run(task='KekkaiUtilize', finish=True, success=True)
        if con.pets_enable:
            pets = Pets(self.config, self.device)
            pets.run()
        # 走到这里说明本次运行没有走过失败收尾，连续失败计数归零
        self.reset_entry_failure('KekkaiUtilize')
        logger.info(self.msg)
        raise TaskEnd(self.msg)

    def recive_guild_ap_or_assets(self, max_tries: int = 3):
        for i in range(1, max_tries+1):
            self.ui_get_current_page()
            self.ui_goto(page_guild)
            # 在寮的主界面 检查是否有收取体力或者是收取寮资金
            if self.check_guild_ap_or_assets():
                logger.warning(f'第[{i}]次检查寮收获,成功')
                self.ui_goto(page_main)
                break
            else:
                logger.warning(f'第[{i}]次检查寮收获寮收获,失败')
            self.ui_goto(page_main)

    def check_utilize_add(self):
        con = self.config.kekkai_utilize.utilize_config
        while 1:
            # 本循环只负责「换一张卡再试」；界面进不去由 goto_sub_page + realm_entry_failed
            # 处理并直接结束任务，不再消耗 utilize_add_count 的剩余次数。
            self.utilize_add_count += 1
            if self.utilize_add_count >= 5:
                # 连续失败达上限就不再 5 分钟一次地重复，直接推到明天同一时刻。
                # 这里只记失败、不抛 TaskEnd：后面还有收怪、收盒等步骤要走完。
                if self.record_entry_failure('KekkaiUtilize', con.max_consecutive_failures):
                    fail_msg = '没有合适可以蹭的卡, 连续失败已达上限, 明天再试'
                    target = datetime.now() + timedelta(days=1)
                else:
                    fail_msg = '没有合适可以蹭的卡, 5分钟后再次执行蹭卡'
                    target = datetime.now() + timedelta(minutes=5)
                logger.warning(fail_msg)
                # 添加消息到列表，以便在TaskEnd时返回
                from tasks.DailyAltAcc.config import MSGType
                self.msg.append([MSGType.Utilize, "未找到寄养卡"])
                self.push_notify(content=fail_msg)
                
                if not con.utilize_rule == UtilizeRule.DAILY:
                    self.config.notifier.push(content=fail_msg, title='寄养')
                self.set_next_run(task='KekkaiUtilize', target=target)

                return

            # 无论收不收到菜，都会进入看看至少看一眼时间还剩多少
            time.sleep(0.5)
            # 进入寄养界面（育成 + 寄养两步）。失败时回寮重进结界重试，仍失败就收尾结束
            # 本次任务 —— 界面点不动与是哪张卡无关，继续换卡只会掩盖真因。
            if not self.goto_sub_page(self.enter_utilize_once, '寄养'):
                # 通知 + 5 分钟延后 + TaskEnd 都在这里完成；文案由 realm_entry_failed 统一生成
                self.realm_entry_failed('寄养')

            if self.utilize_already_active:
                remaining_time = self.O_UTILIZE_RES_TIME.ocr_duration(self.device.image)
                if not isinstance(remaining_time, timedelta):
                    logger.warning('Ocr remaining time error')
                logger.info(f'Utilize remaining time: {remaining_time}')
                # 已经蹭上卡了，设置下次蹭卡时间  # 减少30秒
                # remaining_time = remaining_time - timedelta(seconds=30)
                next_time = datetime.now() + remaining_time
                # 正常蹭上卡后的下次上号时间叠加随机延时，避免准点上线
                next_time = self.apply_random_delay('KekkaiUtilize', next_time)
                if not self.config.kekkai_utilize.utilize_config.utilize_rule == UtilizeRule.DAILY:
                    self.config.notifier.push(content=f'下次寄养时间: {next_time}', title='寄养')

                self.set_next_run(task='KekkaiUtilize', target=next_time)
                return
            # 开始执行寄养
            if self.run_utilize(con.select_friend_list, con.shikigami_class, con.shikigami_order):
                # 退出寮结界
                self.back_guild()
                # 进入寮结界（back_guild 不更新 ui_current，goto_realm 内部会先重新识别）
                if not self.goto_realm():
                    self.realm_entry_failed('寮结界')
            else:
                self.back_realm()

    def check_max_lv(self, shikigami_class: ShikigamiClass = ShikigamiClass.N):
        """
        在结界界面，进入式神育成，检查是否有满级的，如果有就换下一个
        退出的时候还是结界界面
        :return:
        """
        if not self.realm_goto_grown():
            logger.warning('进入式神育成界面失败，跳过满级检查')
            return
        if self.appear(self.I_RS_LEVEL_MAX):
            # 存在满级的式神
            logger.info('Exist max level shikigami and replace it')
            self.unset_shikigami_max_lv()
            self.switch_shikigami_class(shikigami_class)
            self.set_shikigami(shikigami_order=7, stop_image=self.I_RS_NO_ADD)
        else:
            logger.info('No max level shikigami')
        if self.detect_no_shikigami():
            logger.warning('There are no any shikigami grow room')
            self.switch_shikigami_class(shikigami_class)
            self.set_shikigami(shikigami_order=7, stop_image=self.I_RS_NO_ADD)

        # 回到结界界面
        while 1:
            self.screenshot()

            if self.appear(self.I_REALM_SHIN) and self.appear_multi_scale(self.I_SHI_GROWN):
                self.screenshot()
                if not self.appear(self.I_REALM_SHIN):
                    continue
                break
            if self.appear_then_click(self.I_UI_BACK_BLUE, interval=2.5):
                continue

    def check_guild_ap_or_assets(self, ap_enable: bool = True, assets_enable: bool = True) -> bool:
        """
        在寮的主界面 检查是否有收取体力或者是收取寮资金
        如果有就顺带收取
        :return:
        """
        timer_check = Timer(2)
        timer_check.start()
        click_ap = False
        while 1:
            self.screenshot()

            # 获得奖励
            if self.ui_reward_appear_click():
                timer_check.reset()
                continue

            if timer_check.reached():
                return False

            if click_ap and not self.appear(self.I_GUILD_AP) and not self.appear(self.I_UI_REWARD):
                return True

            # 关闭展开的寮活动横幅
            if self.appear_then_click(self.I_GUILD_EXPAND):
                timer_check.reset()
                continue

            # 资金收取确认
            if self.appear_then_click(self.I_GUILD_ASSETS_RECEIVE, interval=1):
                time.sleep(1)
                timer_check.reset()
                continue

            # 收资金
            if self.appear_then_click(self.I_GUILD_ASSETS, interval=1.5, threshold=0.6):
                timer_check.reset()
                continue

            # 收体力
            if self.appear_then_click(self.I_GUILD_AP, interval=1):
                # 等待1秒，看到获得奖励
                time.sleep(1)
                logger.info('appear_click guild_ap success')
                if self.ui_reward_appear_click(True):
                    logger.info('appear_click reward success')
                    click_ap = True
                    time.sleep(1)
                    timer_check.reset()
                continue

    def goto_realm(self) -> bool:
        """前往寮结界根页。

        结构：先在**当前画面**上把弹窗清掉并判根页 → 不是就走导航图 → 到达后再核对一次皮肤。
        **ui_goto_page 必须放在最后**：导航的第一次点击是从当前页出发的，画面被弹窗
        盖住时那一下会被弹窗吃掉，所以要先清干净再导航。

        判据用「当前页是不是 page_guild_realm」：这一页的 check_button 里有 I_REALM_SHIN，
        它是固定 UI、不随结界皮肤变，所以换皮后照样认得出来。

        :return: 已进入结界返回 True；导航失败返回 False（由调用方 realm_entry_failed 收尾）
        """
        while 1:
            self.screenshot()
            if self.ui_page_appear(page_guild_realm, skip_first_screenshot=True, interval=None):
                # 页面认出来了才校正皮肤：下面两个入口都是**按坐标**点的，用错皮肤
                # 就是点空。校正放在页面判据之后，判据本身不重复判（见 _check_realm_skin）。
                self._check_realm_skin()
                # 根页判据命中：同步页面缓存，否则下一次导航会按旧缓存走同节点空路径
                self.ui_current = page_guild_realm
                return True
            # 种树 / 种花活动弹窗。与 page_guild.additional 用的是同一组图——那边只在
            # page_guild 上关，这里补上根页这一侧，否则弹窗盖住时导航点不动。
            if self.appear_then_click(self.I_PLANT_TREE_CLOSE):
                continue
            if self.appear_then_click(self.I_PLANT_TREE_CLOSE_2):
                continue
            if self.appear_then_click(self.I_PLANT_FLOWER_ENSURE):
                continue
            if self.appear_then_click(self.I_PLANT_FLOWER_ENSURE2):
                continue
            break
        # 必须走 ui_goto_page 而不是 ui_goto：back_guild() 手动退回寮后不更新 ui_current，
        # 缓存若仍停在结界，直接导航会把同节点路径当成已到达、根本不点链接按钮
        if not self.ui_goto_page(page_guild_realm):
            return False
        # 到达后再校正一次，**不改判到达**（ui_goto_page 已由 ui_wait_until_appear 确认过）：
        # 从庭院/寮进来的那一跳里结界背景根本不在画面上，循环里那次看不到，
        # 真正该校正的是这一帧。
        self._check_realm_skin()
        return True

    def _check_realm_skin(self) -> None:
        """在结界帧上补一次皮肤探测（探到即就地改写资产并回写配置）。

        **调用点必须已经确认画面就是 page_guild_realm**（见 goto_realm 的两处调用）。
        这里刻意**不设自己的判据**：判据交给页面本身 —— page_guild_realm 的 check_button
        已经同时含了随皮肤变的站位锚点与不随皮肤变的 I_REALM_SHIN，在它之后再用其中
        某一个单独卡一道，只是把同一件事用更严的阈值重判一遍。

        实测（2026-09-12）这样卡会**静默**跳过纠正：00:34 那次 signal=False
        （I_REALM_SHIN 单帧未命中）直接 return，接着拿错皮肤的坐标点了 4 次；
        00:36 / 00:37 同一条路径 signal=True，探测触发并成功纠正。
        **同一个信号时灵时不灵，就不能当闸门用** —— 它在 back_realm 那种每轮重判的
        循环里没问题，在只跑一两次的一次性判据里就是单点故障。

        两个判据仍然照打日志：它们是「皮肤到底相不相符」唯一的佐证，
        signal=False anchor=True 这种组合本身就是有价值的现场信息。

        探测自身带独立的节流与锁，且是「配置优先」顺序扫候选 —— 配置那套相符时
        第一个就命中、幂等套用并上锁，代价只有一次扫描。
        """
        # **先等转场动画放完再判**。从寮进结界的动画很长（实测页面判据会在
        # [0.9s]~[2.0s] 就成立，即动画中途），此时截图做模板匹配等于拿半张画面去比：
        # 三张探测候选会全部零命中、I_REALM_SHIN 也会掉到阈值下、连锚点都可能误判。
        # 实测（2026-09-12）两次翻车都是这个原因：00:34 那次 signal=False、
        # 00:51 第 1 轮探针零命中；而等到画面稳定后判的几次（00:36 / 00:37 / 00:50 /
        # 00:51 第 2 轮）次次命中。**探针不是认不出皮肤，是根本没看清。**
        time.sleep(self.REALM_ANIMATION_WAIT)
        self.screenshot()
        signal = self.appear(self.I_REALM_SHIN)
        anchor = self.appear(self.I_REALM_PAGE)
        logger.info(f'Realm skin check: signal={signal} anchor={anchor}')
        self.try_detect_costume_realm()

    @staticmethod
    def _realm_region(asset, name: str) -> RuleClick:
        """把资产的当前 roi_front 变成点击区域。

        realm_grown / realm_card 用 C_REALM_* （RuleClick 资产，roi_front 本身就是
        点击区域）；realm_utilize 用 I_UTILIZE_ADD（1:1 固定位资产）。
        坐标现取是为了跟随成本管线的皮肤改写：结界皮肤将来会覆盖 C_REALM_*，
        鲤鱼旗皮肤会就地改写 I_SHI_* 的 roi_front（costume_base.py:383）。
        :param asset: 资产（C_REALM_GROWN / I_UTILIZE_ADD / C_REALM_CARD）
        :param name: 日志用的区域名
        """
        # 两个 list 分开构造，避免 roi_front 与 roi_back 共享同一个对象
        return RuleClick(list(asset.roi_front), list(asset.roi_front), name=name)

    def realm_entry_failed(self, name: str) -> None:
        """进入结界入口或子界面失败的统一收尾：通知、延后、结束本次任务。

        文案不写「已重试 N 轮」：goto_sub_page 在回寮/重进结界失败时会提前放弃，
        实际轮数不一定跑满，写死轮数会说谎。
        :param name: 失败位置（'寮' / '寮结界' / '寄养'）
        """
        con = self.config.kekkai_utilize.utilize_config
        # 连续失败达上限就不再 5 分钟一次地重复，直接推到明天同一时刻
        if self.record_entry_failure('KekkaiUtilize', con.max_consecutive_failures):
            fail_msg = f'进入{name}失败, 连续失败已达上限, 明天再试'
            target = datetime.now() + timedelta(days=1)
        else:
            fail_msg = (f'进入{name}失败, '
                        f'{int(self.REALM_ENTRY_FAIL_DELAY.total_seconds() // 60)}分钟后再次执行')
            target = datetime.now() + self.REALM_ENTRY_FAIL_DELAY
        logger.warning(fail_msg)
        from tasks.DailyAltAcc.config import MSGType
        self.msg.append([MSGType.Utilize, fail_msg])
        self.push_notify(content=fail_msg)
        if not con.utilize_rule == UtilizeRule.DAILY:
            self.config.notifier.push(content=fail_msg, title='寄养')
        self.set_next_run(task='KekkaiUtilize', target=target)
        raise TaskEnd(self.msg)

    @cached_property
    def entry_fail_streak(self) -> EntryFailStreak:
        """连续进入失败计数（按 config 实例分文件落盘）"""
        return EntryFailStreak(self.config.config_name)

    def record_entry_failure(self, task_name: str, limit: int) -> bool:
        """记录一次「进入结界/子界面失败」，返回是否已达连续失败上限。

        达上限时由调用方把下次运行时间推到明天；未达上限维持原有的 5 分钟重试。
        计数必须落盘：失败会结束本次任务，下次跑的是全新实例，内存计数必丢。
        :param task_name: 任务名（KekkaiUtilize / KekkaiActivation），也是计数文件的键
        :param limit: 连续失败上限，<=0 表示不启用
        :return: True 表示已达上限（计数同时归零、重新累计）
        """
        self.entry_failed_this_run = True
        count = self.entry_fail_streak.increase(task_name)
        if limit <= 0:
            return False
        if count >= limit:
            logger.warning(f'[{task_name}] 连续失败 {count} 次，已达上限 {limit}')
            self.entry_fail_streak.reset(task_name)
            return True
        logger.warning(f'[{task_name}] 连续失败 {count}/{limit} 次')
        return False

    def reset_entry_failure(self, task_name: str) -> None:
        """本次运行正常收尾时把连续失败计数归零。

        只在「本次运行没走过失败收尾」时调用：没合适卡那条路径失败后是 return
        回去继续走完正常收尾的，不能在这里把它的计数清掉。
        """
        if self.entry_failed_this_run:
            return
        self.entry_fail_streak.reset(task_name)

    @cached_property
    def utilize_add_brightness(self) -> float:
        """加号模板图自身的平均亮度，作为「原版亮度」的基准。

        读不到模板图时返回 0，调用处会跳过变暗判定（退化成只看模板匹配）。
        """
        import cv2
        img = cv2.imread(self.I_UTILIZE_ADD.file)
        if img is None:
            logger.warning(f'读取加号模板失败: {self.I_UTILIZE_ADD.file}')
            return 0.0
        return float(cv2.cvtColor(img, cv2.COLOR_BGR2RGB).mean())

    def utilize_add_state(self) -> str:
        """判定「放置好友寄养」加号按钮当前的状态。

        加号整个消失是 ABSENT（说明已经在寄养中）；仍在、但亮度掉到原版六成
        以下是 DIM —— 那是寄养界面已经打开、覆盖层把加号压暗了。此时再点同一
        坐标会点到覆盖层上、把刚打开的页面关掉，所以调用方必须把 DIM 当作
        「页面已打开」，而不是「加号还在，继续点」。
        """
        if not self.appear(self.I_UTILIZE_ADD):
            return self.UTILIZE_ADD_ABSENT
        # appear 命中后 roi_front 已回写为匹配到的位置，按它取区域亮度
        x, y, w, h = (int(v) for v in self.I_UTILIZE_ADD.roi_front)
        cur = float(self.device.image[y:y + h, x:x + w].mean())
        ref = self.utilize_add_brightness
        if ref > 0 and cur / ref < self.UTILIZE_ADD_DIM_RATIO:
            return self.UTILIZE_ADD_DIM
        return self.UTILIZE_ADD_BRIGHT

    def utilize_add_state_stable(self, frames: int = None) -> str | None:
        """连续 frames 帧状态一致才返回该状态；帧间变化返回 None（状态还在变）。

        device.screenshot() 自带 interval 节流（会阻塞到下一帧），所以循环调用
        拿到的确实是不同的帧，不是同一张图判多次。
        """
        if frames is None:
            frames = self.UTILIZE_ADD_STABLE_FRAMES
        state = None
        for i in range(frames):
            self.screenshot()
            cur = self.utilize_add_state()
            if i == 0:
                state = cur
            elif cur != state:
                logger.warning(f'加号状态帧间不一致（{state} -> {cur}），本次判定不稳定')
                return None
        return state

    def realm_entry_click(self, region, arrived, max_clicks: int = None) -> bool:
        """在结界界面点 region 区域，直到 arrived() 为真；最多点 max_clicks 次。

        每次点击前先判 arrived()：已经进去了就不再多点一下，避免误触界面里的其它控件。

        **前置条件：结界皮肤必须已识别**（`realm_skin_confirmed()`，由 goto_realm 里的探测置位）。
        不满足时一次都不点、直接返回 False —— region 是按皮肤定的坐标，认不出皮肤就是盲点。
        这是「所有按坐标点结界页」的唯一收口，realm_grown / realm_utilize / realm_card 都走这里。

        :param region: 点击区域（RuleClick）
        :param arrived: 成功判据，无参可调用、返回 bool
        :param max_clicks: 本步点击预算，默认 REALM_ENTRY_CLICK_MAX
        :return: 已进入目标界面返回 True；进不去、或皮肤未识别而拒点时返回 False
        """
        if max_clicks is None:
            max_clicks = self.REALM_ENTRY_CLICK_MAX
        for _ in range(max_clicks):
            self.screenshot()
            if arrived():
                return True
            # 皮肤没识别出来就**不许按坐标点**：region 的坐标是按某套皮肤定的（C_REALM_GROWN /
            # C_REALM_CARD 都随结界皮肤变，由 costume_base 就地改写），认不出是哪套皮肤，
            # 就没有任何依据判断这些坐标能不能用 —— 点了就是盲点，既浪费整个点击预算，
            # 还可能误触界面里的其它控件。返回 False 交给 goto_sub_page 走「回寮重进」，
            # 那条路会重新 goto_realm、重新探一次皮肤（探针零命中只节流 2 秒、不锁死）。
            #
            # 放在 arrived() 之后：人已经在目标界面时直接成功，不需要皮肤作依据。
            if not self.realm_skin_confirmed():
                logger.warning('结界皮肤未识别，跳过按坐标点击，交给外层重进结界后重探')
                return False
            # 同一区域是**刻意连点**（每步 4 次 × 3 轮 = 12 次），正好会撞上 device 的防连点
            # 门禁：同名计数 >= 12 先清记录 + sleep(10)，连续触发还会抛
            # GameTooManyClickError，从 click 内部抛出、绕过 realm_entry_failed 的干净收尾
            # （module/device/device.py:446）。在调用处显式声明「预期内连点」，
            # 与 DemonEncounter 同一范式（tasks/DemonEncounter/script_task.py:100）。
            # 注意：这里只豁免 device 的点击记录门禁；拟人化的同一资源退避走 repeat_exempt，
            # 而 BaseTask.click 未暴露该参数，故连点退避仍会生效（拟人化默认 off，影响面小）。
            self.device.click_record_remove(region)
            self.click(region)
            # 点击后给页面留出响应时间再判：同一坐标在目标页面上往往是关闭按钮，
            # 抢在页面打开之前重复点击会把刚打开的页面关掉。页面一出现立即返回。
            timer = Timer(self.REALM_ENTRY_CLICK_INTERVAL).start()
            while not timer.reached():
                self.screenshot()
                if arrived():
                    return True
        # 最后一次点击后界面可能已经切过去，再判一次
        self.screenshot()
        return arrived()

    def goto_sub_page(self, enter_once, name: str) -> bool:
        """进入结界子界面的统一重试外壳。

        每轮先尝试一次完整进入；失败则退回寮并重新进入结界再试。全部轮次失败即返回
        False —— 本函数**不**自行通知，通知与延后由调用方按任务各自的渠道处理。

        调用方拿到 False 后应当直接收尾（`realm_entry_failed`），不要再回到「换卡」之类
        的上层循环里重试：界面点不动是 UI 层面的问题、与是哪张卡无关，换卡重试只会把
        真因掩盖成「没有合适的卡」。

        :param enter_once: 无参可调用，执行一次完整进入尝试，返回是否成功
        :param name: 界面名（'寄养' / '挂卡'），用于日志文案
        :return: 进入成功返回 True；判定失败返回 False
        """
        for r in range(1, self.REALM_ENTRY_ROUND_MAX + 1):
            if enter_once():
                return True
            # 最后一轮失败后不再回寮重进 —— 后面已经没有下一轮，多跑一次导航只是白等
            if r < self.REALM_ENTRY_ROUND_MAX:
                logger.warning(f'第[{r}]轮进入{name}界面失败，退回寮并重新进入结界')
                # 走 ui_goto_page：连续失败后 ui_current 未必可信，直接 ui_goto 可能拿旧缓存走空路径
                if not self.ui_goto_page(page_guild):
                    logger.warning(f'退回寮失败，放弃进入{name}界面')
                    return False
                if not self.goto_realm():
                    logger.warning(f'重新进入结界失败，放弃进入{name}界面')
                    return False
        logger.warning(f'进入{name}界面失败（已重试{self.REALM_ENTRY_ROUND_MAX}轮）')
        return False

    def check_box_ap_or_exp(self, ap_enable: bool = True, exp_enable: bool = True, exp_waste: bool = True) -> bool:
        """
        顺路检查盒子
        :param ap_enable:
        :param exp_enable:
        :return:
        """

        # 退出到寮结界
        def _exit_to_realm():
            # 右上方关闭红色
            while 1:
                self.screenshot()
                if self.appear(self.I_REALM_SHIN):
                    break
                if self.appear_then_click(self.I_UI_BACK_RED, interval=1):
                    continue

        # 先是体力盒子
        def _check_ap_box(appear: bool = False):
            if not appear:
                return False
            # 点击盒子
            timer_ap = Timer(6)
            timer_ap.start()
            while 1:
                self.screenshot()

                if self.appear(self.I_UI_REWARD):
                    while 1:
                        self.screenshot()
                        if not self.appear(self.I_UI_REWARD):
                            break
                        if self.appear_then_click(self.I_UI_REWARD, self.C_UI_REWARD, interval=1, threshold=0.6):
                            continue
                    logger.info('Reward box')
                    break

                if self.appear_then_click(self.I_BOX_AP, interval=1):
                    continue
                if self.appear_then_click(self.I_AP_EXTRACT, interval=2):
                    continue
                if timer_ap.reached():
                    logger.warning('Extract ap box timeout')
                    break
            logger.info('Extract AP box finished')
            _exit_to_realm()

        # 经验盒子
        def _check_exp_box(appear: bool = False):
            if not appear:
                logger.info('No exp box')
                return False

            time_exp = Timer(12)
            time_exp.start()
            while 1:
                self.screenshot()
                # 如果出现结界皮肤， 表示收取好了
                if self.appear(self.I_REALM_SHIN) and not self.appear(self.I_BOX_EXP, threshold=0.6):
                    break
                # 如果出现收取确认，表明进入到了有满级的
                if self.appear(self.I_UI_CONFIRM):
                    self.screenshot()
                    if not self.appear(self.I_UI_CANCEL):
                        logger.info('No cancel button')
                        continue
                    if exp_waste:
                        check_button = self.I_UI_CONFIRM
                    else:
                        check_button = self.I_UI_CANCEL
                    while 1:
                        self.screenshot()
                        if not self.appear(check_button):
                            break
                        if self.appear_then_click(check_button, interval=1):
                            continue
                    break

                if self.appear(self.I_EXP_EXTRACT):
                    # 如果达到今日领取的最大，就不领取了
                    cur, res, totol = self.O_BOX_EXP.ocr(self.device.image)
                    if cur == res == totol == 0:
                        continue
                    if cur == totol and cur + res == totol:
                        logger.info('Exp box reach max do not collect')
                        break
                if self.appear_then_click(self.I_BOX_EXP, threshold=0.6, interval=1):
                    continue
                if self.appear_then_click(self.I_EXP_EXTRACT, interval=1):
                    continue

                if time_exp.reached():
                    logger.warning('Extract exp box timeout')
                    break
            _exit_to_realm()

        self.screenshot()
        box_ap = self.appear(self.I_BOX_AP)
        box_exp = self.appear(self.I_BOX_EXP, threshold=0.6) or self.appear(self.I_BOX_EXP_MAX, threshold=0.6)
        if ap_enable:
            _check_ap_box(box_ap)
        if exp_enable:
            _check_exp_box(box_exp)

    def check_utilize_harvest(self) -> bool:
        """
        在寮结界界面检查是否有收获
        :return: 如果没有返回False, 如果有就收菜返回True
        """
        self.screenshot()
        appear = self.appear(self.I_UTILIZE_EXP)
        if not appear:
            logger.info('No utilize harvest')
            return False

        # 收获
        self.ui_get_reward(self.I_UTILIZE_EXP)
        return True

    def realm_goto_grown(self) -> bool:
        """结界界面 -> 式神育成界面。

        保持「单步原语」语义：只点固定次数，自身不做回寮重进 —— check_max_lv() 也调它，
        那里不该触发整套重试。
        :return: 已进入式神育成界面返回 True
        """
        if not self.realm_entry_click(self._realm_region(self.C_REALM_GROWN, 'realm_grown'),
                                      self.in_shikigami_growth):
            logger.warning('进入式神育成界面失败')
            return False
        logger.info('Enter shikigami grown')
        return True

    def grown_goto_utilize(self) -> bool:
        """式神育成界面 -> 寄养界面。

        I_UTILIZE_ADD 的存在性是业务判据（有可寄养的槽位），不是导航判据，
        所以保留原有守卫，不随入口区域一起改成盲点。
        :return: 已进入寄养界面返回 True
        """
        state = self.utilize_add_state_stable()
        if state == self.UTILIZE_ADD_ABSENT:
            logger.warning('No utilize add')
            return False
        if state == self.UTILIZE_ADD_DIM:
            # 寄养界面已经开着（加号被覆盖层压暗），直接算到达：再点一次同一坐标
            # 会点到覆盖层上，把刚打开的页面关掉
            logger.info('Utilize page already open (add is dimmed), skip clicking')
            return True
        # BRIGHT，或状态不稳（None，页面正在变化）：走正常的点击流程
        if not self.realm_entry_click(self._realm_region(self.I_UTILIZE_ADD, 'realm_utilize'),
                                      lambda: self.appear(self.I_U_ENTER_REALM)):
            logger.warning('进入寄养界面失败')
            return False
        logger.info('Enter utilize')
        return True

    def enter_utilize_once(self) -> bool:
        """结界界面 -> 寄养界面的一次完整尝试（育成、寄养两步）。

        育成界面若看不到「放置好友寄养」，说明已经有寄养在进行，本次不需要再进寄养
        界面 —— 置 utilize_already_active 并返回 True，调用方据此走「已寄养」分支
        （读剩余时间、设置下次运行）。这个判定必须夹在两步之间，因为判据只存在于
        育成界面，没法被 goto_sub_page 的重试外壳吞掉。
        :return: 本次尝试成功（含「不需要进入」）返回 True
        """
        self.utilize_already_active = False
        if not self.realm_goto_grown():
            return False
        # 加号连续多帧都看不到才算「已经在寄养中」：单帧漏判会把已寄养误判成
        # 未寄养，进而去点一个不存在的加号
        if self.utilize_add_state_stable() == self.UTILIZE_ADD_ABSENT:
            self.utilize_already_active = True
            return True
        return self.grown_goto_utilize()

    @staticmethod
    def parse_priority_search_names(raw_names: str) -> list[tuple[SelectFriendList, str]]:
        """解析带区服标记的优先搜索名称配置。"""
        if not raw_names or not raw_names.strip():
            return []

        zone_map = {
            '同区': SelectFriendList.SAME_SERVER,
            '同服': SelectFriendList.SAME_SERVER,
            'same_server': SelectFriendList.SAME_SERVER,
            '跨区': SelectFriendList.DIFFERENT_SERVER,
            '跨服': SelectFriendList.DIFFERENT_SERVER,
            'different_server': SelectFriendList.DIFFERENT_SERVER,
        }
        result = []
        for raw_item in re.split(r'[\n,，;；]+', raw_names):
            item = raw_item.strip()
            if not item:
                continue
            matched = re.match(
                r'^(同区|同服|跨区|跨服|same_server|different_server)\s*[:：|]\s*(.+)$',
                item,
                flags=re.IGNORECASE,
            )
            if not matched:
                logger.warning('忽略未标注同区或跨区的优先搜索名称: %s', item)
                continue
            zone_text = matched.group(1).lower()
            name = matched.group(2).strip()
            if not name:
                logger.warning('忽略角色名为空的优先搜索配置: %s', item)
                continue
            result.append((zone_map[zone_text], name))
        return result

    @staticmethod
    def _normalize_priority_name(text: str) -> str:
        """统一空白和常见异体字，降低好友名称 OCR 的比较误差。"""
        return re.sub(r'\s+', '', str(text or '')).replace('瑤', '瑶').replace('別', '别')

    @classmethod
    def _priority_input_text(cls, texts: list[str]) -> str:
        """把输入框 OCR 结果拼成用于状态判断的文本，并剔除光标噪声。

        输入框聚焦后会显示闪烁的竖线光标，OCR 常把它误识别成「一」「1」「|」等
        单字符；这类噪声会让状态机把空输入框误判成「残留其它内容」而多删一次。
        因此只保留长度大于 1 的片段，单字符片段一律视为噪声丢弃。
        """
        merged = ''
        for text in texts:
            piece = cls._normalize_priority_name(text)
            if len(piece) <= 1:
                continue
            merged += piece
        return merged

    @staticmethod
    def _opposite_friend_list(friend: SelectFriendList) -> SelectFriendList:
        """返回另一个好友区服，用于清空当前搜索状态。"""
        if friend == SelectFriendList.SAME_SERVER:
            return SelectFriendList.DIFFERENT_SERVER
        return SelectFriendList.SAME_SERVER

    def _reset_priority_search(self, current_friend: SelectFriendList,
                               target_friend: SelectFriendList) -> SelectFriendList:
        """切区服重建好友列表，用于翻列表之后需要确定地解除搜索过滤的场景。

        区服不同时直接切过去即可：切换本身就是一次真实的 tab 点击，列表会重建。
        区服相同时必须先绕对面一趟——switch_friend_list 的循环条件是"目标 tab 已
        高亮就退出"，已在该区服时调用会立即返回、不做任何点击，达不到重置效果。
        """
        if current_friend != target_friend:
            self.switch_friend_list(target_friend)
            return target_friend
        self.switch_friend_list(self._opposite_friend_list(current_friend))
        self.switch_friend_list(target_friend)
        return target_friend

    def _detect_friend_list_zone(self) -> SelectFriendList:
        """截图判断好友列表当前处于同区还是跨区。

        同区/跨区两个分组标签本身就是 switch_friend_list 的判定依据，可直接复用：
        识别到跨区标签即跨区，否则按游戏行为回退为同区——刚进入蹭卡列表必定是同区。
        """
        self.screenshot()
        if self.appear(self.I_UTILIZE_ZONES_GROUP):
            logger.info('当前好友列表区服: 跨区')
            return SelectFriendList.DIFFERENT_SERVER
        if self.appear(self.I_UTILIZE_FRIEND_GROUP):
            logger.info('当前好友列表区服: 同区')
            return SelectFriendList.SAME_SERVER
        # 两个标签都没识别到时按游戏默认行为处理：刚进入列表必定是同区
        logger.info('未识别到好友列表分组标签，按刚进入列表的默认区服处理: 同区')
        return SelectFriendList.SAME_SERVER

    def _goto_priority_zone(self, current_friend: SelectFriendList,
                            target_friend: SelectFriendList) -> SelectFriendList:
        """遍历优先名称时切到目标区服，仅在区服不同时才切换。

        与 _reset_priority_search 不同，这里不需要重建列表：输入框由
        I_NAME_DELETE 负责清空，同区服时无须多切一趟。
        """
        if current_friend != target_friend:
            self.switch_friend_list(target_friend)
        return target_friend

    def _open_priority_name_search(self) -> bool:
        """打开好友名称搜索栏，搜索按钮出现后停止点击放大镜。"""
        timeout = Timer(8).start()
        while not timeout.reached():
            self.screenshot()
            if self.appear(self.I_SERACH_ON):
                return True
            if self.appear_then_click(self.I_SERACH_NAME, interval=0.6):
                continue
        logger.warning('打开好友名称搜索栏超时')
        return False

    def _priority_name_check_texts(self) -> list[str]:
        """读取搜索输入框中的全部 OCR 文本。"""
        results = self.O_NAME_CHECK.detect_and_ocr(self.device.image)
        return [item.ocr_text for item in results] if results else []

    def _focus_priority_name_input(self) -> bool:
        """持续点击名称输入框，直到占位文字消失。"""
        timeout = Timer(6).start()
        roi_x, roi_y, roi_w, roi_h = self.O_NAME_CHECK.roi
        click_x = int(roi_x + roi_w / 2)
        click_y = int(roi_y + roi_h / 2)
        placeholder = self._normalize_priority_name(self.PRIORITY_NAME_PLACEHOLDER)

        while not timeout.reached():
            # 每轮先点击再校验，避免首次 OCR 漏检时误判输入框已经聚焦
            self.device.click(click_x, click_y, control_name=self.O_NAME_CHECK.name)
            time.sleep(0.3)
            self.screenshot()
            current_text = self._priority_input_text(self._priority_name_check_texts())
            if placeholder not in current_text:
                return True
        logger.warning('好友名称输入框未能获得焦点')
        return False

    def _clear_priority_name_input(self) -> bool:
        """点击删除按钮清空搜索输入框，占位文字重新出现即确认已清空。

        游戏侧点击删除后输入框会失去焦点、重新显示占位文字，所以调用方随后
        需要再走一次 _focus_priority_name_input 才能继续输入。
        """
        timeout = Timer(6).start()
        placeholder = self._normalize_priority_name(self.PRIORITY_NAME_PLACEHOLDER)
        while not timeout.reached():
            self.screenshot()
            current_text = self._priority_input_text(self._priority_name_check_texts())
            if placeholder in current_text:
                logger.info('搜索输入框已清空')
                return True
            if not self.appear_then_click(self.I_NAME_DELETE, interval=0.6):
                continue
        logger.warning('清空搜索输入框超时')
        return False

    def _input_priority_name(self, name: str) -> bool:
        """把角色名写入搜索输入框，并等待输入框 OCR 确认。

        先按输入框当前 OCR 状态分支，避免盲目重复输入：
        - 已经是目标名：上一次输入的残留可直接复用，跳过输入
        - 占位文字（空）：聚焦后输入
        - 其它残留名：先点 I_NAME_DELETE 清空，删除会失去焦点故需重新聚焦再输入

        输入本身一次性 send_keys 整串角色名（内部走 fastinput IME 广播，中文不会
        乱码），不逐字符输入；结束后收起 fastinput 浮动条，防止遮挡结界卡识别。
        """
        expected = self._normalize_priority_name(name)
        placeholder = self._normalize_priority_name(self.PRIORITY_NAME_PLACEHOLDER)

        self.screenshot()
        current_text = self._priority_input_text(self._priority_name_check_texts())
        if expected in current_text:
            logger.info('搜索输入框已是目标角色名，跳过输入: %s', name)
            return True
        if placeholder not in current_text and current_text:
            # 残留了别的角色名，先删掉再重新聚焦
            logger.info('搜索输入框残留其它内容[%s]，先清空', current_text)
            if not self._clear_priority_name_input():
                return False
        if not self._focus_priority_name_input():
            return False

        is_desktop = getattr(self.device, 'is_desktop', False)
        for attempt in range(3):
            try:
                if is_desktop:
                    # 桌面端没有 uiautomator2，清空与输入统一走 Windows 消息注入
                    self.device.input_text_desktop('', clear=True)
                    self.device.input_text_desktop(name)
                else:
                    # 兜底再清一次，避免 OCR 漏检残留把角色名重复追加
                    self.device.u2.send_keys('', clear=True)
                    # 一次性输入整串角色名，避免逐字符消息抖动导致漏字
                    self.device.u2.send_keys(name)
            except Exception as error:
                logger.warning('好友名称输入失败: %s', error)
                continue

            check_timer = Timer(3).start()
            while not check_timer.reached():
                time.sleep(0.3)
                self.screenshot()
                current_text = self._priority_input_text(self._priority_name_check_texts())
                if expected in current_text:
                    logger.info('优先搜索名称已输入: %s', name)
                    self._restore_priority_name_ime()
                    return True
            logger.warning('第%d次输入后未识别到角色名: %s', attempt + 1, name)
        self._restore_priority_name_ime()
        return False

    def _restore_priority_name_ime(self) -> None:
        """收起 uiautomator2 fastinput 输入法浮动条，避免遮挡结界卡素材识别。

        一次性 send_keys 会把系统 IME 切到 com.github.uiautomator/.FastInputIME，
        输入框聚焦时屏幕底部会显示 ∞ 浮动工具条（clear Text / switch IME）；
        禁用后系统自动回退到默认输入法，下一次 send_keys 会再次自动启用。
        桌面客户端模式没有 Android IME，直接跳过。
        """
        if getattr(self.device, 'is_desktop', False):
            return
        try:
            self.device.u2.set_fastinput_ime(False)
        except Exception as error:
            logger.warning('收起好友搜索输入法失败: %s', error)

    def _click_priority_search(self) -> bool:
        """连续点击两到三次搜索按钮，降低单次点击丢失的概率。"""
        self.screenshot()
        if not self.appear(self.I_SERACH_ON):
            logger.warning('未识别到好友名称搜索按钮')
            return False
        click_x, click_y = self.I_SERACH_ON.front_center()
        for _ in range(random.randint(2, 3)):
            self.device.click(click_x, click_y, control_name=self.I_SERACH_ON.name)
            time.sleep(0.25)
        return True

    def _matching_priority_name_areas(self, name: str) -> list[tuple[int, int, int, int]]:
        """返回搜索结果中所有同名角色的绝对坐标，并按从上到下排序。"""
        expected = self._normalize_priority_name(name)
        results = self.O_NAME_LIST.detect_and_ocr(self.device.image)
        if not results:
            return []

        roi_x, roi_y, _, _ = self.O_NAME_LIST.roi
        areas = []
        for result in results:
            current = self._normalize_priority_name(result.ocr_text)
            if expected not in current:
                continue
            box = result.box
            # OCR 检测框可能轻微倾斜，按四点外接矩形换算为截图绝对坐标
            box_x = [point[0] for point in box]
            box_y = [point[1] for point in box]
            x = int(roi_x + min(box_x))
            y = int(roi_y + min(box_y))
            width = max(1, int(max(box_x) - min(box_x)))
            height = max(1, int(max(box_y) - min(box_y)))
            areas.append((x, y, width, height))
        return sorted(areas, key=lambda area: (area[1], area[0]))

    def _wait_priority_name_areas(self, name: str) -> list[tuple[int, int, int, int]]:
        """等待名称搜索结果加载完成。"""
        timeout = Timer(6).start()
        while not timeout.reached():
            self.screenshot()
            areas = self._matching_priority_name_areas(name)
            if areas:
                logger.info('搜索到%d个同名角色: %s', len(areas), name)
                return areas
            time.sleep(0.3)
        logger.info('未搜索到角色名: %s', name)
        return []

    @classmethod
    def _priority_selection_roi(cls, name_area: tuple[int, int, int, int]) -> tuple[int, int, int, int]:
        """按角色名纵坐标构造选中标记识别区，右边界固定为640。"""
        name_x, name_y, _, _ = name_area
        left = max(0, int(name_x))
        top = max(0, int(name_y) - 15)
        return left, top, max(1, cls.PRIORITY_LIST_RIGHT - left), 70

    def _select_priority_name_area(self, name: str,
                                   name_area: tuple[int, int, int, int]) -> bool:
        """点击指定同名角色，直到该行出现选中标记。"""
        original_roi = self.I_SELECT_REALM_ON.roi_back
        self.I_SELECT_REALM_ON.roi_back = self._priority_selection_roi(name_area)
        name_x, name_y, name_w, name_h = name_area
        click_x = int(name_x + name_w / 2)
        click_y = int(name_y + name_h / 2)
        timeout = Timer(6).start()
        try:
            while not timeout.reached():
                self.screenshot()
                if self.appear(self.I_SELECT_REALM_ON):
                    logger.info('已选中同名角色: %s @ %s', name, name_area)
                    return True
                self.device.click(click_x, click_y, control_name='priority_search_name')
                time.sleep(0.5)
        finally:
            self.I_SELECT_REALM_ON.roi_back = original_roi
        logger.warning('点击后未识别到角色选中标记: %s @ %s', name, name_area)
        return False

    @classmethod
    def _card_matches_selected_row(
            cls,
            card_area: tuple[int, int, int, int],
            selected_area: tuple[int, int, int, int],
    ) -> bool:
        """根据中心纵坐标判断结界卡是否属于当前选中好友。"""
        _, card_y, _, card_height = card_area
        _, selected_y, _, selected_height = selected_area
        card_center_y = card_y + card_height / 2
        selected_center_y = selected_y + selected_height / 2
        return abs(card_center_y - selected_center_y) <= cls.CARD_SELECTED_Y_THRESHOLD

    def _ensure_card_selected(self, card_area: tuple[int, int, int, int]) -> bool:
        """确保目标结界卡已选中，已对齐时跳过点击和固定等待。"""
        self.C_SELECT_CARD.roi_front = card_area
        if self.appear(self.I_SELECT_REALM_ON) and self._card_matches_selected_row(
                card_area, tuple(self.I_SELECT_REALM_ON.roi_front)):
            logger.info('结界卡已处于选中行，跳过重复点击: %s', card_area)
            return True

        self.click(self.C_SELECT_CARD)
        timeout = Timer(self.CARD_SELECTION_TIMEOUT).start()
        while not timeout.reached():
            self.screenshot()
            if self.appear(self.I_SELECT_REALM_ON) and self._card_matches_selected_row(
                    card_area, tuple(self.I_SELECT_REALM_ON.roi_front)):
                logger.info('结界卡点击后已选中: %s', card_area)
                return True
        logger.warning('等待结界卡选中超时: %s', card_area)
        return False

    @staticmethod
    def _meets_min_value(card_value: int, min_value: int) -> bool:
        """数值达标判断：未配置门槛（0）时保持大于 0，配置后需达到门槛值。"""
        if min_value > 0:
            return card_value >= min_value
        return card_value > 0

    def _priority_search_min_for(self, card_type: str) -> int:
        """返回指定结界卡类型配置的最低寄养门槛；斗鱼/太鼓分别取值，0 表示不限制。"""
        con = self.config.kekkai_utilize.utilize_config
        if card_type == '斗鱼':
            return con.priority_search_min_fish
        if card_type == '太鼓':
            return con.priority_search_min_taiko
        return 0

    def _select_priority_name_card(self, name: str,
                                   name_area: tuple[int, int, int, int],
                                   check_min: bool = True) -> bool:
        """校验选中标记在目标角色行后，直接 OCR 结界卡收益判断是否达标。

        点击角色名时游戏已同时选中该行的结界卡，收益可直接由 O_CARD_NUM 读出，
        无需再用 I_U_FISH_6 / I_U_FISH_5 等模板定位卡片图标：模板会因卡面差异匹配
        不到（实测 151 满值斗鱼卡就匹配不上），导致达标的卡被静默跳过。

        但 OCR 前必须确认选中标记确实落在目标角色那一行，否则会把上一次残留或
        其它行的收益当成本角色的数值。

        check_min=True 时需达到对应类型的最低门槛；未达标的数值会被记录，供后续
        最佳值兜底时直接搜索该好友。check_min=False 时只要收益有效就选中。
        """
        if not self._selected_row_matches_name(name, name_area):
            return False
        # 未公开/已占用的结界不可寄养，视为识别失败且不记录数值，避免占用最佳值兜底名额
        if self._priority_card_unavailable(name):
            return False

        card_type, card_value = self.check_card_num()
        if card_type == 'unknown' or card_value <= 0:
            logger.warning('优先角色%s的结界卡收益识别失败: %s@%s @ %s',
                           name, card_type, card_value, name_area)
            return False

        min_value = self._priority_search_min_for(card_type)
        if check_min and not self._meets_min_value(card_value, min_value):
            # 未达标：记录该好友结界卡数值，供最佳值匹配后直接搜索该好友
            self._record_priority_friend_value(name, card_type, card_value)
            logger.info('优先角色%s的结界卡不满足要求: %s@%s (最低值 %s)', name, card_type, card_value, min_value)
            return False
        logger.info('优先角色%s的结界卡满足要求: %s@%s (最低值 %s)', name, card_type, card_value, min_value)
        return True

    def _selected_row_matches_name(self, name: str,
                                   name_area: tuple[int, int, int, int]) -> bool:
        """确认当前选中标记落在目标角色所在行，避免 OCR 读到其它行的收益。

        复用 _card_matches_selected_row 的中心纵坐标比对：选中标记与角色名同属
        一行时两者中心 Y 接近，跨行则至少相差 100 像素以上。
        """
        self.screenshot()
        if not self.appear(self.I_SELECT_REALM_ON):
            logger.warning('优先角色%s未识别到选中标记，跳过收益识别 @ %s', name, name_area)
            return False
        selected_area = tuple(self.I_SELECT_REALM_ON.roi_front)
        if not self._card_matches_selected_row(selected_area, name_area):
            logger.warning('优先角色%s的选中标记不在该行，跳过收益识别: 标记%s 角色行%s',
                           name, selected_area, name_area)
            return False
        return True

    def _priority_card_unavailable(self, name: str) -> bool:
        """OCR 结界状态栏，判断该好友结界是否未公开或已占用。

        未公开/已占用时结界卡区域不显示可寄养的收益，继续 OCR 只会读到脏数据；
        这里提前短路，让调用方按「收益识别失败」处理，同时不记录数值——被占用的
        结界即使收益很高也寄养不进去，不能参与最佳值兜底。

        复用 _selected_row_matches_name 刚截的图，不额外截屏。
        """
        status_text = self._normalize_priority_name(self.O_CHECK_OPEN.ocr(self.device.image))
        for keyword in self.PRIORITY_CARD_UNAVAILABLE_KEYWORDS:
            if keyword in status_text:
                logger.info('优先角色%s的结界%s，跳过收益识别', name, keyword)
                return True
        return False

    def _record_priority_friend_value(self, name: str, card_type: str, card_value: int) -> None:
        """记录优先好友未达标结界卡的数值，供最佳值匹配后直接搜索该好友。"""
        record = self.priority_friend_records.setdefault(name, {})
        record[card_type] = max(record.get(card_type, 0), card_value)

    def _priority_friend_matching(self, target_value: int, card_type: str) -> str | None:
        """返回结界卡数值不低于最佳值且最接近的优先好友名，没有则返回 None。"""
        best_name = None
        best_value = -1
        for name, record in self.priority_friend_records.items():
            value = record.get(card_type, 0)
            if value >= target_value and value > best_value:
                best_name = name
                best_value = value
        return best_name

    def _select_from_priority_names(
            self,
            priority_names: list[tuple[SelectFriendList, str]],
            current_friend: SelectFriendList,
            shikigami_class: ShikigamiClass = ShikigamiClass.N,
            shikigami_order: int = 7,
            check_min: bool = True,
    ) -> tuple[bool, SelectFriendList]:
        """依次搜索优先角色，逐个检查同名结果；寄养成功后返回，坑位被占用则跳过当前角色。"""
        # 确保实例属性存在，避免误改类属性造成跨轮污染
        if 'priority_friend_records' not in self.__dict__:
            self.priority_friend_records = {}
        for target_friend, name in priority_names:
            # 记录该好友所在区服，供后续最佳值匹配后直接搜索该好友
            self.priority_friend_records.setdefault(name, {})['zone'] = target_friend
            # 输入框由 I_NAME_DELETE 清空，这里只在区服不同时切换
            current_friend = self._goto_priority_zone(current_friend, target_friend)
            logger.info('开始优先搜索好友: %s [%s]', name, target_friend.value)

            if not self._open_priority_name_search():
                continue
            # 聚焦已收进 _input_priority_name，避免两处都点输入框导致状态打架
            if not self._input_priority_name(name):
                continue
            if not self._click_priority_search():
                continue

            for name_area in self._wait_priority_name_areas(name):
                if not self._select_priority_name_area(name, name_area):
                    continue
                if not self._select_priority_name_card(name, name_area, check_min=check_min):
                    continue
                # 选中好友后进入结界寄养；坑位被占用时退回蹭卡界面并跳过当前角色
                result = self._enter_realm_and_utilize(shikigami_class, shikigami_order)
                if result == 'occupied':
                    logger.info('好友%s的结界坑位被占用，跳过该角色', name)
                    # 坑位被占用时该好友不可用，移除其结界卡数值，避免被计入最佳值
                    self.priority_friend_records.pop(name, None)
                    self._exit_friend_realm_to_utilize()
                    continue
                if result == 'ok':
                    return True, current_friend
                logger.warning('进入好友%s的结界失败，跳过该角色', name)
                self._exit_friend_realm_to_utilize()
                continue
        return False, current_friend

    def _search_priority_friend_and_utilize(self, name: str, friend: SelectFriendList,
                                            shikigami_class: ShikigamiClass,
                                            shikigami_order: int,
                                            list_friend: SelectFriendList = None) -> bool:
        """直接搜索指定优先好友并寄养（不检查最低值），用于最佳值匹配好友时免翻列表。

        进来之前刚翻过列表，搜索框状态不可知，所以先 _reset_priority_search 切区服
        重建列表：好友与翻列表处于同一区服时会绕对面一趟，不同区服则直接切过去。
        :param list_friend: 翻列表时所处的区服，缺省按好友区服处理
        """
        if list_friend is None:
            list_friend = friend
        current_friend = self._reset_priority_search(list_friend, friend)
        selected, _ = self._select_from_priority_names(
            [(friend, name)], current_friend, shikigami_class, shikigami_order, check_min=False)
        return selected

    def _enter_realm_and_utilize(self, shikigami_class: ShikigamiClass,
                                 shikigami_order: int) -> str:
        """进入已选好友的结界并上式神寄养。

        返回字符串状态供调用方决定后续：
        - 'ok': 寄养成功
        - 'occupied': 结界坑位被占用，未上式神
        - 'failed': 进入好友结界失败
        """
        # 找到卡后清理重试次数和原选卡流程留下的最优值
        self.utilize_add_count = 0
        self.ap_max_num, self.jade_max_num = 0, 0
        logger.info('开始执行进入结界蹭卡流程')
        self.screenshot()
        # 进入结界
        if not self.appear(self.I_U_ENTER_REALM):
            logger.warning('Cannot find enter realm button')
            # 可能是滑动的时候出错
            logger.warning('The best reason is that the swipe is wrong')
            return 'failed'
        wait_timer = Timer(20)
        wait_timer.start()
        while 1:
            self.screenshot()
            if self.appear(self.I_U_ADD_1) or self.appear(self.I_U_ADD_2):
                logger.info('Appear enter friend realm button')
                break
            if self.appear(self.I_CHECK_FRIEND_REALM_1):
                self.wait_until_stable(self.I_CHECK_FRIEND_REALM_1)
                logger.info('Appear enter friend realm button')
                break
            if self.appear(self.I_CHECK_FRIEND_REALM_3):
                self.wait_until_stable(self.I_CHECK_FRIEND_REALM_3)
                logger.info('Appear enter friend realm button')
                break
            if wait_timer.reached():
                self.save_image(wait_time=0, push_flag=False, content='进入好友结界超时', image_type='png')
                logger.warning('Appear friend realm timeout')
                return 'failed'
            if self.appear_then_click(self.I_CHECK_FRIEND_REALM_2, interval=1.5):
                logger.info('Click too fast to enter the friend\'s realm pool')
                continue
            if self.appear_then_click(self.I_U_ENTER_REALM, interval=2.5):
                time.sleep(0.5)
                continue
        logger.info('Enter friend realm')

        # 判断好友的有两个位置还是一个坑位
        stop_image = None
        self.screenshot()
        if self.appear(self.I_U_ADD_1):  # 右侧第一个有（无论左侧有没有）
            logger.info('Right side has one')
            stop_image = self.I_U_ADD_1
        elif self.appear(self.I_U_ADD_2) and not self.appear(self.I_U_ADD_1):  # 右侧第二个有 但是最左边的没有，这表示只留有一个坑位
            logger.info('Right side has two')
            stop_image = self.I_U_ADD_2
        if not stop_image:
            # 没有坑位可能是其他人的手速太快了抢占了
            self.save_image(content='没有坑位了', wait_time=0, push_flag=False, image_type='png')
            logger.warning('没有坑位可能是其他人的手速太快了抢占了')
            return 'occupied'
        # 切换式神的类型
        self.switch_shikigami_class(shikigami_class)
        # 上式神
        self.set_shikigami(shikigami_order, stop_image)
        return 'ok'

    def _exit_friend_realm_to_utilize(self) -> None:
        """从好友结界退回到蹭卡界面（好友列表），供继续搜索下一个优先角色。"""
        timeout = Timer(10).start()
        while not timeout.reached():
            self.screenshot()
            if self.appear(self.I_UTILIZE_FRIEND_GROUP) or self.appear(self.I_UTILIZE_ZONES_GROUP):
                return
            if self.appear_then_click(self.I_UI_BACK_RED, interval=1):
                continue
            if self.appear_then_click(self.I_UI_BACK_BLUE, interval=1):
                continue
        logger.warning('退出好友结界返回蹭卡界面超时')

    def switch_friend_list(self, friend: SelectFriendList = SelectFriendList.SAME_SERVER) -> bool:
        """
        切换不同的服务区
        :param friend:
        :return:
        """
        logger.info('Switch friend list to %s', friend)
        if friend == SelectFriendList.SAME_SERVER:
            check_image = self.I_UTILIZE_FRIEND_GROUP
        else:
            check_image = self.I_UTILIZE_ZONES_GROUP

        timer_click = Timer(1)
        timer_click.start()
        while 1:
            self.screenshot()
            if self.appear(check_image):
                break
            if timer_click.reached():
                timer_click.reset()
                x, y = check_image.coord()
                self.device.click(x=x, y=y, control_name=check_image.name)
        if friend == SelectFriendList.DIFFERENT_SERVER:
            time.sleep(1)
        time.sleep(0.5)
        return True

    @cached_property
    def order_targets(self) -> ImageGrid:
        rule = self.config.kekkai_utilize.utilize_config.utilize_rule
        if rule == UtilizeRule.DEFAULT:
            return ImageGrid([self.I_U_FISH_6, self.I_U_TAIKO_6, self.I_U_FISH_5, self.I_U_TAIKO_5])
        elif rule == UtilizeRule.FISH:
            return ImageGrid([self.I_U_FISH_6, self.I_U_FISH_5])
        elif rule == UtilizeRule.TAIKO:
            return ImageGrid([self.I_U_TAIKO_6, self.I_U_TAIKO_5])
        elif rule == UtilizeRule.DAILY:
            return ImageGrid([self.I_U_TAIKO_6, self.I_U_TAIKO_5, self.I_U_TAIKO_4, self.I_U_TAIKO_3])
        else:
            logger.error('Unknown utilize rule')
            raise ValueError('Unknown utilize rule')

    @cached_property
    def order_cards(self) -> list[CardClass]:
        rule = self.config.kekkai_utilize.utilize_config.utilize_rule
        result = []
        if rule == UtilizeRule.DEFAULT:
            result = [CardClass.FISH6, CardClass.TAIKO6, CardClass.FISH5, CardClass.TAIKO5,
                      CardClass.TAIKO4, CardClass.FISH4, CardClass.TAIKO3, CardClass.FISH3]
        elif rule == UtilizeRule.FISH:
            result = [CardClass.FISH6, CardClass.FISH5,
                      CardClass.TAIKO6, CardClass.TAIKO5, CardClass.FISH4, CardClass.TAIKO4, CardClass.FISH3,
                      CardClass.TAIKO3]
        elif rule == UtilizeRule.TAIKO:
            result = [CardClass.TAIKO6, CardClass.TAIKO5,
                      CardClass.TAIKO4, CardClass.TAIKO3, CardClass.FISH6, CardClass.FISH5, CardClass.FISH4,
                      CardClass.FISH3]
        elif rule == UtilizeRule.DAILY:
            result = [CardClass.TAIKO6, CardClass.TAIKO5,
                      CardClass.TAIKO4, CardClass.TAIKO3]
        else:
            logger.error('Unknown utilize rule')
            raise ValueError('Unknown utilize rule')
        return result

    def run_utilize(self, friend: SelectFriendList = SelectFriendList.SAME_SERVER,
                    shikigami_class: ShikigamiClass = ShikigamiClass.N,
                    shikigami_order: int = 7):
        """
        执行寄养
        :param shikigami_class:
        :param friend:
        :param rule:
        :return:
        """
        logger.hr('Start utilize')
        # 每轮寄养前清空优先好友记录，避免跨轮残留
        self.priority_friend_records = {}
        first_round = self.first_utilize
        if self.first_utilize:
            self.first_utilize = False
        if not first_round:
            # 非首次：搜索前切回配置的优先好友区服
            self.switch_friend_list(friend)
        # 首次搜索前不切换区服，避免干扰搜索框定位

        # --------------- 结界卡选择 ---------------
        priority_names = self.parse_priority_search_names(
            self.config.kekkai_utilize.utilize_config.priority_search_names
        )
        selected = False
        # 首轮没有实际切换过区服，靠截图判断屏幕真实区服（识别不到则按刚进列表的同区处理），
        # 否则配置为跨区且首个名称也标跨区时会误判为已在跨区而不切换，在同区列表里搜跨区好友
        current_friend = friend if not first_round else self._detect_friend_list_zone()
        if priority_names:
            # 优先搜索成功时已在内部进入结界并寄养
            selected, current_friend = self._select_from_priority_names(
                priority_names, current_friend, shikigami_class, shikigami_order)
        if not selected:
            if priority_names:
                # 优先名称全部失败后清空搜索，并回到配置的优先好友区服执行原流程
                self._reset_priority_search(current_friend, friend)
            # 优先搜索完成后再滑动到列表最下方，拉到底后切换一遍同区跨区刷新列表
            if first_round:
                self.swipe(self.S_U_END, interval=3)
                if friend == SelectFriendList.SAME_SERVER:
                    self.switch_friend_list(SelectFriendList.DIFFERENT_SERVER)
                    self.switch_friend_list(SelectFriendList.SAME_SERVER)
                else:
                    self.switch_friend_list(SelectFriendList.SAME_SERVER)
                    self.switch_friend_list(SelectFriendList.DIFFERENT_SERVER)
            # 回退到原流程后始终在配置的优先好友区服翻列表
            # 选卡阶段可能内部就完成了寄养（搜索优先好友路径），用标志区分
            self.utilized_in_select = False
            if not self._select_optimal_resource_card(
                    shikigami_class, shikigami_order, list_friend=friend):
                return False
            if self.utilized_in_select:
                # 选卡阶段已进结界并上式神，不能再进一次
                logger.info('选卡阶段已完成寄养，跳过重复进入结界')
                return True
            # 原选卡流程选中的卡，进入结界寄养；占用时保持原行为直接返回
            result = self._enter_realm_and_utilize(shikigami_class, shikigami_order)
            if result == 'occupied':
                logger.info('原选卡流程的结界坑位被占用')
                return True
            if result == 'failed':
                return False
        return True

    def _select_optimal_resource_card(self,
                                      shikigami_class: ShikigamiClass = ShikigamiClass.N,
                                      shikigami_order: int = 7,
                                      list_friend: SelectFriendList = SelectFriendList.SAME_SERVER):
        """整合后的智能选卡主逻辑（无嵌套函数版）

        :param list_friend: 翻列表时所处的区服，回头搜优先好友时决定怎么切区服重置
        """
        # 类常量声明（需在类中定义）
        RESOURCE_PRESETS = {
            '斗鱼': [151, 143, 134, 126, 101, 84],
            '太鼓': [76,  76,  67,  67,  59,  50]
        }
        MAX_INDEX = 99

        def get_resource_index(resource_name, current_value, preset_values):
            """获取资源匹配的档位索引"""
            for idx, val in enumerate(preset_values):
                if current_value >= val:
                    logger.info(f'📊 {resource_name}区间匹配: {current_value} ≥ {val} (档位{idx})')
                    return idx
            logger.warning(f'⚠️ {resource_name}值[{current_value}]低于所有预设')
            return MAX_INDEX
        if self.config.kekkai_utilize.utilize_config.utilize_rule == UtilizeRule.DAILY:
            logger.info('DAILY规则：寻找任意太鼓卡')
            if self._find_any_taiko_card():
                logger.info('✅ 找到太鼓卡，立即寄养')
                return True
            else:
                logger.info('❌ 未找到合适的太鼓卡')
                return False
        while True:
            self.screenshot()

            # 第一阶段：初始记录获取
            if self.ap_max_num == 0 and self.jade_max_num == 0:
                logger.hr('第一阶段：初始记录获取', 2)
                if self._current_select_best():
                    logger.info(f'✅ 完美结界卡确认成功，重置状态')
                    self.ap_max_num, self.jade_max_num = 0, 0
                    return True
                logger.info(f'📝 记录最佳值 | 斗鱼:{self.ap_max_num} 太鼓:{self.jade_max_num}')
                if self.ap_max_num == 0 and self.jade_max_num == 0:
                    # 一整轮都没有可用结界卡，放弃
                    return False
                # 未找到达标卡，进入第二阶段用当前最佳值兜底

            logger.hr('第二阶段：资源优先级判断', 2)
            # 获取双资源档位
            ap_index = get_resource_index('斗鱼', self.ap_max_num, RESOURCE_PRESETS['斗鱼'])
            jade_index = get_resource_index('太鼓', self.jade_max_num, RESOURCE_PRESETS['太鼓'])

            # 双资源超限处理
            if ap_index == MAX_INDEX and jade_index == MAX_INDEX:
                if self.ap_max_num <= 0 and self.jade_max_num <= 0:
                    logger.warning('🔄 未记录到任何可用结界卡，重置初始记录')
                    self.ap_max_num, self.jade_max_num = 0, 0
                    return False
                # 探索未达到最低值，第二轮直接选择当前数值较高的最佳值兜底
                res_type, target = ('斗鱼', self.ap_max_num) if self.ap_max_num >= self.jade_max_num else ('太鼓', self.jade_max_num)
            else:
                # 决策优先级
                res_type, target = ('斗鱼', self.ap_max_num) if ap_index <= jade_index else ('太鼓', self.jade_max_num)
            logger.info(f'⚖️ 选择{res_type}卡 | 目标: {target}')

            # 第三阶段：执行选卡操作
            logger.hr('第三阶段：执行选卡操作', 2)
            # 若最佳值恰好来自某个优先好友的结界卡（好友值不低于最佳值），
            # 直接搜索该好友寄养，避免再次翻列表；好友值更低时仍翻列表确认
            friend_name = self._priority_friend_matching(target, res_type)
            if friend_name:
                friend_zone = self.priority_friend_records.get(friend_name, {}).get(
                    'zone', SelectFriendList.SAME_SERVER)
                if self._search_priority_friend_and_utilize(
                        friend_name, friend_zone, shikigami_class, shikigami_order,
                        list_friend=list_friend):
                    logger.info(f'✅ 搜索优先好友{friend_name}寄养成功')
                    self.ap_max_num, self.jade_max_num = 0, 0
                    # 搜索好友路径内部已完成进结界与上式神，通知调用方不要再进一次
                    self.utilized_in_select = True
                    return True
                logger.warning(f'❌ 搜索优先好友{friend_name}寄养失败，回落翻列表确认')
                # 搜索好友时留下了过滤状态、且可能停在好友所在区服，
                # 回落翻列表前按重置原则切回翻列表区服并重建完整列表
                self._reset_priority_search(friend_zone, list_friend)
            if self._current_select_best(res_type, target, selected_card=True):
                logger.info(f'✅ {res_type}卡确认成功，重置状态')
                self.ap_max_num, self.jade_max_num = 0, 0
                return True
            else:
                logger.warning(f'❌ {res_type}卡确认失败，重置状态')
                self.ap_max_num, self.jade_max_num = 0, 0
                return False
# ... existing code ...
    def _find_any_taiko_card(self):
        """在DAILY规则下寻找任意太鼓卡并立即使用"""
        logger.info('开始寻找任意太鼓卡...')
        RESOURCE_CONFIG = {
            '斗鱼': {'max': 151, 'record_attr': 'ap_max_num'},
            '太鼓': {'max': 76, 'record_attr': 'jade_max_num'}
        }
        MAX_SWIPES = 20  # 最大滑动次数
        CONSEC_MISS = 3  # 允许连续无卡次数
        TIMEOUT = 120  # 操作超时(秒)

        # ============== 初始化阶段 ==============#
        timer = Timer(TIMEOUT).start()
        miss_count = 0  # 连续无卡计数器

        # ============== 主滑动循环 ==============#
        for swipe_count in range(MAX_SWIPES + 1):
            # 超时检测
            if timer.reached():
                logger.warning('⏰ 操作超时，终止流程')
                return False

            # ------ 步骤1: 截图识别结界卡 ------#
            self.screenshot()
            cards = self.order_targets.find_everyone(self.device.image)

            # 处理无卡情况
            if not cards:
                miss_count += 1
                logger.info(f'第{swipe_count}次滑动 | 未检测到结界卡' if swipe_count > 0 else '初始界面 | 未检测到结界卡')
                # 连续无卡超过阈值则终止
                if miss_count > CONSEC_MISS:
                    logger.warning(f'⚠️ 连续{miss_count}次 | 未检测到结界卡, 终止流程')
                    return False
                # 执行滑动操作
                self.perform_swipe_action()
                continue

            miss_count = 0  # 重置无卡计数器

            # ------ 步骤2: 处理识别到的结界卡 ------
            cards_list = [target for target, _, _ in cards]
            logger.info((f'第{swipe_count}次滑动' if swipe_count > 0 else '初始界面') + f' | 检测到结界卡：{cards_list}')

            # 遍历所有结界卡（已按位置排序）
            for _, _, area in cards:
                # 选中目标卡片后再读取详情，已选中时无需重复点击
                if not self._ensure_card_selected(area):
                    continue

                # 解析结界卡类型和数值
                card_type, card_value = self.check_card_num()

                # 跳过无效结界卡（类型未知或数值异常）
                if card_type == 'unknown' or card_value <= 0 or card_type not in RESOURCE_CONFIG:
                    logger.info(f'⏭️ 跳过无效卡: {card_type}@{card_value}')
                    continue

                # 太鼓卡达到配置最低值后立即确认寄养，低于门槛则跳过
                if card_type == '太鼓' and self._meets_min_value(
                        card_value, self._priority_search_min_for(card_type)):
                    logger.info(f'🎉 发现太鼓卡: {card_type}@{card_value}，返回成功')
                    self.save_image(push_flag=False, wait_time=0, content=f'🎉 发现太鼓卡（{card_type}: {card_value}）')
                    return True

            # ------ 步骤3: 滑动到下一屏 ------#
            self.perform_swipe_action()

        # ============== 终止处理 ==============#
        logger.warning(f'⚠️ 已达到最大滑动次数{MAX_SWIPES}, 未找到可用太鼓卡')
        return False
# ... existing code ...
    def _current_select_best(self, best_card_type=None, best_card_num=0, selected_card=False):
        """结界卡选择核心逻辑（集成版）
        功能：滑动屏幕寻找最优资源卡，支持两种模式：
        - 探索模式：记录当前遇到的最佳结界卡数值
        - 确认模式：根据给定条件选择指定类型结界卡

        :param best_card_type: 目标卡类型('太鼓'/'斗鱼')
        :param best_card_num:  要求的最低数值
        :param selected_card:  是否处于确认选择模式
        :return: 找到符合条件返回True，否则None
        """
        # ============== 配置常量 ==============#
        RESOURCE_CONFIG = {
            '斗鱼': {'max': 151, 'record_attr': 'ap_max_num'},
            '太鼓': {'max': 76, 'record_attr': 'jade_max_num'}
        }
        MAX_SWIPES = 50  # 最大滑动次数
        CONSEC_MISS = 8  # 允许连续无卡次数
        TIMEOUT = 240  # 操作超时(秒)

        # ============== 初始化阶段 ==============#
        logger.info(f'启动{"探索模式" if not selected_card else f"确认模式 | 目标: {best_card_type} @ {best_card_num}"}')
        timer = Timer(TIMEOUT).start()
        miss_count = 0  # 连续无卡计数器

        # ============== 主滑动循环 ==============#
        for swipe_count in range(MAX_SWIPES + 1):
            # 超时检测
            if timer.reached():
                logger.warning('⏰ 操作超时，终止流程')
                return None

            # ------ 步骤1: 截图识别结界卡 ------#
            self.screenshot()
            cards = self.order_targets.find_everyone(self.device.image)

            # 处理无卡情况
            if not cards:
                miss_count += 1
                logger.info(f'第{swipe_count}次滑动 | 未检测到结界卡' if swipe_count > 0 else '初始界面 | 未检测到结界卡')
                # 连续无卡超过阈值则终止
                if miss_count > CONSEC_MISS:
                    logger.warning(f'⚠️ 连续{miss_count}次 | 未检测到结界卡, 终止流程')
                    return None
                # 执行滑动操作
                self.perform_swipe_action()
                continue

            miss_count = 0  # 重置无卡计数器

            # ------ 步骤2: 处理识别到的结界卡 ------
            cards_list = [target for target, _, _ in cards]
            logger.info((f'第{swipe_count}次滑动' if swipe_count > 0 else '初始界面') + f' | 检测到结界卡：{cards_list}')

            # 遍历所有结界卡（已按位置排序）
            for _, _, area in cards:
                # 选中目标卡片后再读取详情，已选中时无需重复点击
                if not self._ensure_card_selected(area):
                    continue

                # 解析结界卡类型和数值
                card_type, card_value = self.check_card_num()

                # 跳过无效结界卡（类型未知或数值异常）
                if card_type == 'unknown' or card_value <= 0 or card_type not in RESOURCE_CONFIG:
                    logger.info(f'⏭️ 跳过无效卡: {card_type}@{card_value}')
                    continue

                # ====== 模式分支处理 ======#
                current_max = RESOURCE_CONFIG[card_type]['max']
                record_attr = RESOURCE_CONFIG[card_type]['record_attr']
                current_record = getattr(self, record_attr, 0)
                logger.info(f'🔍 识别卡片: {card_type} | 当前值: {card_value}, 最优值: {current_record}')

                # 更新最佳记录
                if card_value > current_record:
                    logger.info(f'📈 更新记录: {card_type} | {current_record} → {card_value}')
                    setattr(self, record_attr, card_value)

                if selected_card:  # 确认选择模式
                    # 第二轮确认选择当前最佳值，不再强制达到最低值门槛
                    if (card_type == best_card_type) and (card_value >= best_card_num):
                        logger.info(f'🎉 确认蹭卡: {card_type} | 当前值: {card_value} ≥ 目标值: {best_card_num}')
                        self.save_image(push_flag=False, wait_time=0, content=f'🎉 确认蹭卡（{card_type}: {card_value}）')
                        return True
                else:  # 探索记录模式
                    # 达到配置最低值的卡直接寄养，不用继续翻找
                    min_value = self._priority_search_min_for(card_type)
                    if card_value >= current_max or (min_value > 0 and card_value >= min_value):
                        message = f'🎉 完美蹭卡 | {card_type}: {card_value}'
                        logger.info(message)
                        self.save_image(push_flag=False, wait_time=0, content=message)
                        return True

            # ------ 步骤3: 滑动到下一屏 ------#
            self.perform_swipe_action()

        # ============== 终止处理 ==============#
        logger.warning(f'⚠️ 已达到最大滑动次数{MAX_SWIPES}, 终止流程')
        return None

    def perform_swipe_action(self):
        """统一滑动操作"""
        # 保留约半行重叠，兼顾速度与列表覆盖完整性
        safe_pos_x = random.randint(340, 600)
        safe_pos_y = random.randint(500, 565)
        p1 = (safe_pos_x, safe_pos_y)
        p2 = (safe_pos_x, safe_pos_y - 300)
        # 不传 duration，走 Control.swipe 的距离推导默认时长
        # （100~300px→200~350ms，300px 列表上滑落在该档）
        self.device.swipe(p1, p2)

        # self.swipe(self.S_U_UP, duration=1, wait_up_time=1)
        time.sleep(2)

    def check_card_num(self) -> tuple[str, int]:
        """优化版数值提取方法，返回结界卡类型及对应数值"""
        self.screenshot()
        # OCR识别
        raw_text = self.O_CARD_NUM.ocr(self.device.image)
        # logger.info(f'OCR原始结果: {raw_text}')

        # 判断结界卡类型
        if any(c in raw_text for c in ['体', 'カ', '力']):
            card_type = '斗鱼'
        elif any(c in raw_text for c in ['勾', '玉']):
            card_type = '太鼓'
        else:
            logger.warning(f'结界卡类型识别失败，原始内容: {raw_text}')
            # self.push_notify(content=f'结界卡类型识别失败: {raw_text}')
            return 'unknown', 0  # 未知类型返回0

        # 提取纯数字部分（兼容带+号的情况，如+100）
        cleaned = re.sub(r'[^\d+]', '', raw_text)  # 保留数字和加号
        match = re.search(r'\d+', cleaned)  # 匹配连续数字

        try:
            value = int(match.group()) if match else 0
        except ValueError:
            logger.warning(f'数值转换异常，清理后文本: {cleaned}')
            value = 0

        if value <= 0:
            self.push_notify(content=f'数值异常: {raw_text} -> 解析值: {value}')
            return card_type, 0

        # logger.info(f'识别成功: 卡类型: {card_type}, 数值: {value}')
        return card_type, value

    def back_guild(self):
        """
        回到寮的界面
        :return:
        """
        while 1:
            self.screenshot()

            if self.appear(self.I_GUILD_INFO):
                break
            if self.appear(self.I_GUILD_REALM):
                break
            if self.appear_then_click(self.I_PLANT_TREE_CLOSE):
                continue

            if self.appear_then_click(self.I_UI_BACK_RED, interval=1):
                continue
            if self.appear_then_click(self.I_UI_BACK_BLUE, interval=1):
                continue
            if self.appear_then_click(self.I_UI_BACK_YELLOW, interval=1):
                continue

    def back_realm(self):
        # 回到寮结界
        while 1:
            self.screenshot()
            if self.appear(self.I_REALM_SHIN):
                break
            if self.appear_multi_scale(self.I_SHI_DEFENSE):
                break
            if self.appear_then_click(self.I_UI_BACK_RED, interval=1):
                continue
            if self.appear_then_click(self.I_UI_BACK_BLUE, interval=1):
                continue


if __name__ == "__main__":
    from module.config.config import Config
    from module.device.device import Device

    c = Config('oas2')
    d = Device(c)
    t = ScriptTask(c, d)
    t.run()
    for i in range(10):
        t.perform_swipe_action()
    t.recive_guild_ap_or_assets()
    # t.check_utilize_add()
    # t.check_card_num('勾玉', 67)
    # t.screenshot()
    # print(t.appear(t.I_BOX_EXP, threshold=0.6))
    # print(t.appear(t.I_BOX_EXP_MAX, threshold=0.6))
