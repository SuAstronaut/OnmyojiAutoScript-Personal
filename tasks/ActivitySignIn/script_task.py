# This Python file uses the following encoding: utf-8
import time
from dataclasses import dataclass

from module.base.timer import Timer
from module.exception import GameTooManyClickError, TaskEnd
from module.logger import logger
from tasks.ActivitySignIn.assets import ActivitySignInAssets
from tasks.GameUi.game_ui import GameUi
from tasks.GameUi.page import page_main


@dataclass(frozen=True)
class FlowSpec:
    """一个活动签到流程的声明。

    :param key: 配置字段名，与 config.py 的 flow_* 字段一一对应
    :param name: 日志显示名
    :param goto: 导航方法名；None 表示资源尚未采集，运行时直接跳过
    :param claim: 领取方法名；None 表示导航到活动页即结束（无需额外领取动作）
    """
    key: str
    name: str
    goto: str | None
    claim: str | None = None


# 流程执行顺序 = 本表声明顺序，与 config.py 的字段声明顺序保持一致。
# goto/claim 存方法名而非函数对象，避免本模块反向依赖 ScriptTask。
FLOWS: tuple[FlowSpec, ...] = (
    FlowSpec('flow_shikigami',   '五个活动式神', '_goto_reward_page',   '_claim_leftmost'),
    FlowSpec('flow_iwanaga',     '石长姬',      '_goto_reward_page_a', '_claim_iwanaga'),
    FlowSpec('flow_summon_ten',  '十连十金',    '_goto_reward_page_b', '_claim_summon_ten'),
    # 典藏 / 唤新礼 / 晴明皮：动作简单，导航阶段内即把该做的事做完，没有独立的领取阶段
    FlowSpec('flow_collection',  '典藏',        '_goto_reward_page_c', None),
    FlowSpec('flow_huanxin_gift', '唤新礼',     '_goto_reward_page_d', None),
    FlowSpec('flow_seimei_skin',  '晴明皮',     '_goto_reward_page_e', None),
)


class ScriptTask(GameUi, ActivitySignInAssets):
    """单账号活动签到：按配置勾选的流程依次进入对应活动领取奖励。

    从旧 MultiAccountSignIn 剥离而来；
    多账号轮转由 MultiTasks 负责，本任务不感知账号切换。
    """

    # 活动栏「切换列表」在一次导航内的点击上限。翻满仍未找到目标活动，就说明该活动
    # 当前不在列表里——再翻下去只是空转到导航超时，不如直接结束本次导航。
    MAX_LIST_SCROLL = 11
    # 本次导航内已翻次数，由 _run_flow 在每次调用导航方法前重置
    _list_scroll_count = 0

    def _scroll_list_limit_reached(self) -> bool:
        """「切换列表」又翻了一次之后是否已达上限。

        调用点固定在 `appear_then_click(I_CHANGE_ITEM)` 命中的分支里，因此这里
        每被调用一次就代表实际翻了一格。

        @return: True 表示已达上限，调用方应 return False 结束本次导航
        """
        self._list_scroll_count += 1
        if self._list_scroll_count >= self.MAX_LIST_SCROLL:
            logger.warning(
                f'[ActivitySignIn] 切换活动列表超过 {self.MAX_LIST_SCROLL} 次仍未找到目标活动，放弃本次导航')
            return True
        return False

    def _click_leftmost_reward(self) -> bool:
        """识别所有可领取奖励，并点击横坐标最小的一个。"""
        matches = self.I_GET_SHI.match_all_any(self.device.image)
        if not matches:
            logger.warning('[ActivitySignIn] 未识别到可领取奖励')
            return False

        _, x, y, width, height = min(matches, key=lambda match: match[1])
        self.device.click(
            x=x + width // 2,
            y=y + height // 2,
            control_name='I_GET_SHI_LEFTMOST',
        )
        return True

    def _wait_reward_result(self, timeout: float = 8) -> bool:
        """等待领取结果；识别到成功页面时返回 True。"""
        timeout_timer = Timer(timeout).start()
        while not timeout_timer.reached():
            self.screenshot()
            if self.appear(self.I_GET_SHI_SUCCESS):
                return True
            # 转场期间可能出现通用确认或返回按钮，优先关闭后继续检测。
            if self.appear(self.I_GET_SHI_OVER) and self.appear_then_click(self.I_UI_BACK_RED, interval=1):
                return False
        return False

    def _return_to_reward_page(self, timeout: float = 15) -> bool:
        """关闭领取结果页及附加弹窗，直到重新回到奖励主页。"""
        timeout_timer = Timer(timeout).start()
        wait_timer = Timer(3).start()
        while not timeout_timer.reached():
            self.screenshot()
            if self.appear(self.I_PAGE_GET_SHI):
                return True
            if self.appear_then_click(self.I_UI_BACK_RED, interval=1):
                timeout_timer.reset()
                wait_timer.reset()
                continue
            if self.appear_then_click(self.I_GET_SHI_SUCCESS, interval=1):
                timeout_timer.reset()
                wait_timer.reset()
                continue
            if self.appear_then_click(self.I_ANIMATION_JUMP, interval=1):
                timeout_timer.reset()
                wait_timer.reset()
                continue
            if self.appear_then_click(self.I_DLC_EXIT, interval=1):
                timeout_timer.reset()
                wait_timer.reset()
                continue
            if wait_timer.reached():
                self.click(self.I_ANIMATION_JUMP, interval=1)
                wait_timer.reset()
                continue
        logger.warning('[ActivitySignIn] 返回奖励主页超时')
        return False

    def _goto_reward_page(self, timeout: float = 30) -> bool:
        """从庭院进入式神奖励主页。"""
        timeout_timer = Timer(timeout).start()
        while not timeout_timer.reached():
            self.screenshot()
            if self.appear(self.I_PAGE_GET_SHI):
                return True
            if self.appear(self.I_TO_PAGE_SHI):
                self.click(self.I_TO_PAGE_SHI, interval=1.5)
                continue
            if self.appear_then_click(self.I_CHANGE_ITEM, interval=1, repeat_exempt=True):
                if self._scroll_list_limit_reached():
                    return False
                continue
        logger.warning('[ActivitySignIn] 进入式神奖励主页超时')
        return False
    def _goto_reward_page_a(self, timeout: float = 30) -> bool:
            """从庭院进入式神奖励主页。"""
            timeout_timer = Timer(timeout).start()
            while not timeout_timer.reached():
                self.screenshot()
                if self.appear(self.I_A_MAIN):
                    return True
                if self.appear(self.I_A_FINISH):
                    return False
                if self.appear(self.I_A_SKIP):
                    self.click(self.I_A_SKIP, interval=1.5)
                    continue
                if self.appear(self.I_A_TO_MAIN):
                    self.click(self.I_A_TO_MAIN, interval=1.5)
                    continue
                if self.appear_then_click(self.I_CHANGE_ITEM, interval=1, repeat_exempt=True):
                    if self._scroll_list_limit_reached():
                        return False
                    continue
            logger.warning('[ActivitySignIn] 进入式神奖励主页超时')
            return False
    def _goto_reward_page_b(self, timeout: float = 30) -> bool:
            """从庭院进入式神奖励主页。"""
            timeout_timer = Timer(timeout).start()
            while not timeout_timer.reached():
                self.screenshot()
                if self.appear(self.I_B_MAIN):
                    return True
                if self.appear(self.I_B_FINISH):
                    return False
                if self.appear(self.I_B_TO_MAIN):
                    self.click(self.I_B_TO_MAIN, interval=1.5)
                    continue
                if self.appear(self.I_B_TO_MAIN_2):
                    self.click(self.I_B_TO_MAIN_2, interval=1.5)
                    continue
                if self.appear(self.I_B_SELECT_POOL_2) and self.appear(self.I_B_ENSURE):
                    self.click(self.I_B_ENSURE, interval=1.5)
                    continue
                if self.appear(self.I_B_SELECT_POOL):
                    self.click(self.C_B_SELECT_POOL, interval=1.5)
                    continue
                if self.appear_then_click(self.I_CHANGE_ITEM, interval=1, repeat_exempt=True):
                    if self._scroll_list_limit_reached():
                        return False
                    continue
            logger.warning('[ActivitySignIn] 进入式神奖励主页超时')
            return False
    def _goto_reward_page_c(self, timeout: float = 60) -> bool:
                """从庭院进入式神奖励主页。"""
                timeout_timer = Timer(timeout).start()
                while not timeout_timer.reached():
                    self.screenshot()
                    if self.appear(self.I_C_MAIN):
                        return True
                    if self.appear_then_click(self.I_C_GET,interval=1.5):
                        continue
                    if self.appear_then_click(self.I_C_SKIP,interval=1.5):
                        continue
                    if self.appear_then_click(self.I_C_TO_MAIN,interval=1.5):
                        continue
                    if self.appear_then_click(self.I_C_TO_MAIN_2,interval=1.5):
                        continue
                logger.warning('[ActivitySignIn] 进入式神奖励主页超时')
                return False

    def _goto_reward_page_d(self, timeout: float = 60) -> bool:
        """从庭院进入唤新礼活动页，并在页内把奖励领掉。

        I_D_MAIN（「已领取」）既表示「已进入唤新礼页面」，也同时意味着本期已领完，
        两个含义合一：导航命中即算完成。
        入口 I_D_TO_MAIN 是活动栏里点进页面的那一个，槽位与 a 线的 I_A_TO_MAIN 同族。
        I_B_BACK_RED 必须等 I_D_GET 点完才能点——否则会在领取流程还没走完时
        就把页面退出去。
        """
        timeout_timer = Timer(timeout).start()
        # I_D_GET 是否已点到消失；点完之前一律不点返回
        claimed = False
        while not timeout_timer.reached():
            self.screenshot()
            if self.appear(self.I_D_MAIN):
                time.sleep(1.5)
                self.screenshot()
                if self.appear_then_click(self.I_D_MAIN,interval=1.5):
                    return True
                continue
            if self.appear(self.I_D_TO_MAIN):
                self.click(self.I_D_TO_MAIN, interval=1.5)
                continue
            if claimed and self.appear_then_click(self.I_B_BACK_RED, interval=1.5, repeat_exempt=True):
                continue
            if self.appear_then_click(self.I_D_TO_MAIN_2, interval=1.5):
                continue
            if self.appear(self.I_D_GET):
                # 阻塞式点到消失为止；它返回即代表领取已点完，此后才放开返回
                self.ui_click_until_disappear(self.I_D_GET, interval=1.5)
                claimed = True
                continue
            if self.appear_then_click(self.I_D_TO_GET, interval=1.5):
                continue
            if self.appear_then_click(self.I_CHANGE_ITEM, interval=1, repeat_exempt=True):
                if self._scroll_list_limit_reached():
                    return False
                continue
        logger.warning('[ActivitySignIn] 进入唤新礼活动页超时')
        return False

    def _goto_reward_page_e(self, timeout: float = 60) -> bool:
        """从庭院进入晴明皮活动页，并在页内走完微信分享流程。

        I_E_MAIN（「查看」）是活动页标识，I_E_TO_MAIN 是活动栏里的角色头像入口，
        槽位与 a/d 线的入口同族（roiBack 一致）。
        I_E_WECHAT_SUCCESS（「点击「扫一扫」扫码分享」）是分享已走通的标志，不是终态：
        必须等它出现才能开始点返回，否则会把还没走完的分享弹窗直接关掉。分享本身要
        OAS 之外扫码才能完成，所以连点返回到重新看见活动页就算本流程结束。
        """
        timeout_timer = Timer(timeout).start()
        # 分享是否已走通（I_E_WECHAT_SUCCESS 出现过）——出现前一律不点返回
        shared = False
        while not timeout_timer.reached():
            self.screenshot()

            # 回到活动页即完成；否则连点返回逐层退出分享流程
            if self.appear_rgb(self.I_E_MAIN):
                time.sleep(1.5)
                self.screenshot()
                if self.appear_rgb(self.I_E_MAIN):
                    return True
                continue
            if shared:
                if self.appear_then_click(self.I_B_BACK_RED, interval=1.5):
                    continue

            if self.appear(self.I_E_WECHAT_SUCCESS):
                shared = True
                continue
            if self.appear(self.I_E_TO_MAIN):
                self.click(self.I_E_TO_MAIN, interval=1.5)
                continue
            
            if self.appear_then_click(self.I_E_WECHAT_SHARE, interval=8):
                continue
            if self.appear_rgb(self.I_E_TO_SHARE):
                time.sleep(1.5)
                self.screenshot()
                self.appear_then_click(self.I_E_TO_SHARE, interval=1.5)
                continue
            if self.appear_then_click(self.I_CHANGE_ITEM, interval=1, repeat_exempt=True):
                if self._scroll_list_limit_reached():
                    return False
                continue
        logger.warning('[ActivitySignIn] 晴明皮活动页导航超时')
        return False

    def _run_page_summon(self) -> None:
        """十连召唤页推进：反复点金按钮出抽，直到回到本页的收尾入口。

        原为 `_run_sign_in_b` 内的嵌套函数，提取为方法以便 `_claim_summon_ten` 独立可读。
        """
        from tasks.Plotline.assets import PlotlineAssets
        start_time=time.time()
        while time.time()-start_time<20:
            self.screenshot()
            if self.appear(self.I_B_MAIN) or  self.appear(self.I_B_ENSURE_2) or self.appear(self.I_B_SUMMON) :
                break
            if not self.appear(self.I_B_ENSURE_2) and self.appear(self.I_B_FINISH):
                break
            if self.appear_then_click(self.I_B_BACK_RED, interval=1.5):
                start_time=time.time()
                continue
            # 十连召唤金按钮每出一抽结果重现一次，连点约 10 次是流程的正常
            # 推进方式；repeat_exempt 豁免拟人化同一资源连点退避，否则第 6 次
            # 起退避爬到 10/16s，十连会被拖成分钟级
            if self.appear_then_click(self.I_B_SUMMON_GOLD, interval=1.5, repeat_exempt=True):
                start_time=time.time()
                continue
            if self.appear(self.I_B_CANCEL):
                self.click(self.I_B_CANCEL, interval=1.5)
                start_time=time.time()
                continue
            if self.appear_then_click(self.I_B_SUMMON_CHIP_GET, interval=1.5):
                start_time=time.time()
                continue
            if self.appear_then_click(self.I_B_SUMMON_FLAG,action=self.C_B_SUMMON_FLAG, interval=1.5):
                start_time=time.time()
                continue
            if self.appear_then_click(self.I_B_SKIP, interval=1.5):
                start_time=time.time()
                continue
            if self.appear(self.I_B_PAGE_SUMMON):
                self.swipe(PlotlineAssets.S_SWIPE_SUMMON, interval=1.5)
                start_time=time.time()
                continue

    def _claim_leftmost(self) -> None:
        """领取式神奖励主页最左侧的可领奖励，最多尝试 3 次。"""
        for attempt in range(1, 4):
            self.screenshot()
            if not self._click_leftmost_reward():
                # 无可领奖励（通常为已领），视为已处理：不会重复领取
                logger.info('[ActivitySignIn] 无可领奖励，视为已签到')
                return
            logger.info(f'[ActivitySignIn] 第 {attempt}/3 次点击最左侧奖励')
            if not self._wait_reward_result():
                logger.warning(f'[ActivitySignIn] 第 {attempt}/3 次未进入领取成功页面')
                continue
            logger.info('[ActivitySignIn] 奖励领取成功')
            self._return_to_reward_page()
            return

        logger.info('[ActivitySignIn] 三次尝试后未领取成功，本次签到结束')
        return

    def _claim_iwanaga(self) -> None:
        """石长姬：跳过剧情对话与召唤动画，直至命中完成态。"""
        from tasks.Plotline.assets import PlotlineAssets
        start_time=time.time()
        exit_flag=False
        while time.time()-start_time<20:
            self.screenshot()
            if not self.appear(self.I_A_SKIP_3) and exit_flag==True:
                break
            if self.appear(self.I_A_FINISH):
                return
            if self.appear(self.I_A_SKIP_2):
                self.click(self.I_A_SKIP_2, interval=1.5)
                start_time=time.time()
                continue
            if self.appear(self.I_A_SKIP_3):
                self.click(self.I_A_SKIP_2, interval=1.5)
                exit_flag=True
                start_time=time.time()
                continue
            if self.appear(self.I_A_MAIN):
                self.swipe(PlotlineAssets.S_SWIPE_SUMMON, interval=1.5)
                start_time=time.time()
                continue
            if not self.appear(self.I_A_MAIN):
                self.click(self.I_A_SKIP_2, interval=1.5)
                continue

    def _claim_summon_ten(self) -> None:
        """十连十金：推进到召唤页跑完十连，直到命中完成态。"""
        start_time=time.time()
        exit_flag=False
        while time.time()-start_time<20:
            self.screenshot()
            if exit_flag==True and self.appear(self.I_B_MAIN) and not self.appear(self.I_B_ENSURE_2):
                break
            if self.appear(self.I_B_BACK_RED):
                self.click(self.I_B_BACK_RED, interval=1.5)
                start_time=time.time()
                continue
            if not self.appear(self.I_B_ENSURE_2) and self.appear_rgb(self.I_B_FINISH):
                time.sleep(2)
                self.screenshot()
                if self.appear(self.I_B_ENSURE_2):
                    self.click(self.I_B_ENSURE_2, interval=1.5)
                if self.appear_rgb(self.I_B_FINISH):
                    break
                start_time=time.time()
            if self.appear(self.I_B_PAGE_SUMMON):
                self._run_page_summon()
                start_time=time.time()
                continue
            if self.appear(self.I_B_ENSURE_2):
                time.sleep(2)
                self.screenshot()
                if self.appear(self.I_B_ENSURE_2):
                    self.click(self.I_B_ENSURE_2, interval=1.5)
                    exit_flag=True
                start_time=time.time()
                continue
            if self.appear(self.I_B_SUMMON):
                self.click(self.I_B_SUMMON, interval=1.5)
                start_time=time.time()
                continue

    def _selected_flows(self) -> list[FlowSpec]:
        """按 FLOWS 声明顺序返回配置中勾选的流程。"""
        conf = self.config.activity_sign_in.activity_sign_in_config
        return [spec for spec in FLOWS if getattr(conf, spec.key)]

    def _run_flow(self, spec: FlowSpec) -> bool:
        """统一骨架：确保在庭院 → 导航到活动页 → 可选领取。

        @return: True 表示本次流程已处理完；False 表示跳过或导航失败
        """
        if spec.goto is None:
            logger.warning(f'[ActivitySignIn] {spec.name}：资源尚未采集，跳过')
            return False
        # 「切换列表」上限是「本次导航内」的约束，每次导航前归零
        self._list_scroll_count = 0
        # 设备层防连点记录（device.py 的 click_record：最近 15 次点击里同一按钮达 12 次
        # 即抛 GameTooManyClickError）是**跨流程累计**的。翻活动栏本来就是连点同一个
        # 按钮，多个流程各翻几下一凑就到 12 次——但每个流程是一次独立的导航，这个
        # 判定不该跨流程继承。每次导航前清一次，从源头避免误判。
        self.device.click_record_clear()
        try:
            self.screenshot()
            if self.ui_get_current_page() != page_main and not self.ui_goto(page_main):
                logger.warning(f'[ActivitySignIn] {spec.name}：无法返回庭院主页面，跳过')
                return False
            try:
                if not getattr(self, spec.goto)():
                    logger.warning(f'[ActivitySignIn] {spec.name}：导航失败，跳过')
                    return False
                if spec.claim is not None:
                    getattr(self, spec.claim)()
            except GameTooManyClickError:
                # 兜底：防连点仍被触发时只放弃这一个流程。放它穿到调度器会被判定
                # Game stuck 并重启游戏，把 MultiTasks 整轮打断——这代价远大于跳过。
                logger.warning(f'[ActivitySignIn] {spec.name}：触发设备防连点保护，放弃本流程')
                return False
            return True
        finally:
            # 无论结果如何都返回主页面：MultiTasks 复用本任务时，
            # 下一账号/下一流程的切号流程要求从稳定页面开始。
            self.ui_get_current_page(skip_first_screenshot=False)
            if not self.ui_goto(page_main, skip_first_screenshot=False):
                logger.warning(f'[ActivitySignIn] {spec.name}：结束后返回主页面失败')

    def run(self):
        """按配置勾选的顺序依次执行各活动签到流程。"""
        flows = self._selected_flows()
        if not flows:
            logger.warning('[ActivitySignIn] 未勾选任何签到流程，本次不执行')
        for spec in flows:
            try:
                self._run_flow(spec)
            except GameTooManyClickError:
                # 最后一道网：_run_flow 内部已拦了导航/领取，这里兜住收尾阶段（回庭院）
                # 逃出来的同类异常。单个流程的任何问题都不该中断整轮流程，更不该
                # 穿到调度器触发重启——MultiTasks 复用本任务时那会打断所有后续账号。
                logger.warning(f'[ActivitySignIn] {spec.name}：触发设备防连点保护，跳过本流程')
        self.set_next_run('ActivitySignIn', finish=True, success=True, server=False)
        raise TaskEnd('ActivitySignIn')


if __name__ == '__main__':
    from module.config.config import Config
    from module.device.device import Device

    config = Config('oas2')
    device = Device(config)
    task = ScriptTask(config, device)
    task.run()
