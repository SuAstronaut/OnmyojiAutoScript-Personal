# This Python file uses the following encoding: utf-8
# @author runhey
# github https://github.com/runhey
from module.base.timer import Timer
from module.exception import RequestHumanTakeover, GameNotRunningError, GameTooManyClickError, GameStuckError
from module.logger import logger
from module.atom.click import RuleClick
from tasks.Component.Costume.costume_base import release_costume_probe_locks
from tasks.Component.SwitchAccount.character_match import (
    CHARACTER_LIST_EMPTY_OCR_DELAY, CHARACTER_LIST_EMPTY_OCR_LIMIT, find_character_index,
)
from tasks.Component.SwitchAccount.assets import SwitchAccountAssets
from tasks.Component.SwitchAccount.dropdown_state import is_account_dropdown_opened
from tasks.Restart.assets import RestartAssets
from tasks.GameUi.assets import GameUiAssets
from tasks.base_task import BaseTask
import cv2
import time

# 单次登录流程里容许处理的 MPay 弹窗次数上限。弹窗关得掉却反复重弹，说明客户端登录态
# 没有真正推进；而弹窗分支是 continue 回循环顶部、绕过了 self.screenshot()，卡死检测
# （stuck_record_check）只在截图里跑，因此这条路径没有任何超时兜底，原地重试会把登录
# 循环永久挂住。超过上限就交给 Restart 重建客户端，而不是继续等一个不会消失的弹窗。
DESKTOP_MPAY_CLOSE_LIMIT = 5

# 庭院稳定确认的延迟。登录弹窗是随机弹出的，点掉一个到下一个冒出来之间会露出一段真实的
# 干净庭院画面，只看一瞬间的截图无法区分「真的进庭院了」和「两个弹窗之间的中间态」。
# 因此首次看到庭院只启动计时、不作数，等这段时间过完再判一次，两次都是干净庭院才认定
# 登录完成；期间弹窗照常处理，中途识别到弹窗就停止计时，等下次看到庭院重新开始。
#
# 这个值必须大于 _login_popup_blocking 里所有弹窗分支的 interval（当前最大 1.6s，红/黄
# 关闭）。弹窗分支的 interval 是点击节流，静默期内 appear 直接返回 False，那些帧会放行
# 庭院判定；只要确认延迟比最大 interval 长，弹窗真还在的话计时必然在走满之前被下一次点击
# 清掉，所以不会误判。调小这个值或调大某个弹窗的 interval 都会破坏该保证，
# test_courtyard_confirm_delay_exceeds_popup_intervals 会拦住。
#
# LOGIN_CHECK 在 Device.stuck_long_wait_list 里，登录的卡死预算是 stuck_timer_long 的
# 300s，这点延迟占不到 1%，不会把正常登录推成误报卡死。
LOGIN_COURTYARD_CONFIRM_DELAY = 2.5

# 勾选标记与目标文本的 y 容差。必须小于半个【条目】间距（实测条目间距约 123px，半间距
# 61px），而不是半个文本行间距——每个条目是「角色名 / 区服名」两行结构，行内间距只有约
# 37px，而勾选标记落在这两行之间，与角色名差约 20px、与区服名差约 17px。所以容差需要
# 大于 20（否则匹到区服名那一行时判不成立）且小于 61（否则勾选在上一条目、目标在下一
# 条目时也会判定成立，登进邻号且日志毫无异常，是静默失效）。
SELECT_CHARACTER_Y_TOLERANCE = 30

# 点击目标后验证选中态的重试次数。每轮先验证再点击，所以「进来时本就已选中」（默认高亮
# 恰好是目标）会零点击直接通过。
SELECT_CHARACTER_CLICK_RETRY = 3

# 半透明条目会透出登录插画，彩图模板失配时改比对亮色勾选的形状；阈值仍须严格，
# 不能仅凭金色边框或高亮背景认定选中。所有转换只在内存中进行，不修改模板资源。
SELECT_CHARACTER_BRIGHTNESS = 220
SELECT_CHARACTER_SHAPE_THRESHOLD = 0.85

# 角色列表滑动后的等待时间。列表有惯性动画，滑完立刻截图会拍到运动残影导致 OCR 拉花，
# 与 login_account.py 的 switch_character 同口径。
CHARACTER_LIST_SWIPE_DELAY = 1.5


class LoginHandoffReady(Exception):
    """账号选择入口已就绪，结束重启登录并跳过庭院领取。"""


class LoginHandler(BaseTask, RestartAssets, GameUiAssets):
    character: str
    svr: str
    # 旧配置单槽允许填写角色或区服，显式调用则分别使用传入的两个字段。
    _legacy_character_or_svr = False

    def __init__(self, *wargs, **kwargs):
        super().__init__(*wargs, **kwargs)
        self.character = self.config.restart.login_character_config.character.strip()
        # 独立 Restart 的旧配置仅有「角色名/服务器名」单槽，显式标记其二选一语义。
        self.svr = ''
        self._legacy_character_or_svr = True
        self.O_LOGIN_SPECIFIC_SERVE.keyword = self.character
        # self.specific_usr = kwargs['config'].

    def _login_popup_blocking(self) -> bool:
        """处理登录期间的弹窗：识别到一个就点掉并立即返回，让调用方重新截图再判。

        登录循环的判定顺序是「先处理弹窗，一个都没有才判庭院」。弹窗背后就是庭院，庭院标识
        照样能匹配到，所以只要还有弹窗在处理，这一轮就不能碰庭院判定。

        注意各分支的 interval 是点击节流：静默期内 appear 会在模板匹配之前直接返回 False，
        于是「弹窗还在、但本轮不点」的帧会让本函数返回 False、放行庭院判定。这不会造成误判，
        因为庭院二次确认的延迟比所有弹窗 interval 都长（见 LOGIN_COURTYARD_CONFIRM_DELAY），
        弹窗真在的话计时必然在走满之前被下一次点击清掉。
        :return: 处理了弹窗返回 True
        """
        # 网络异常
        # if self.ocr_appear(self.O_LOGIN_NETWORK):
        #     logger.error('Network error')
        #     raise RequestHumanTakeover('Network error')

        # 跳过观看视频
        # if self.ocr_appear_click(self.O_LOGIN_SKIP_1, interval=1):
        #     return False
        # 领取抵扣券
        if self.appear_then_click(self.I_OFF_TICKET, interval=1):
            return True
        #领取抵扣券
        if self.appear_then_click(self.I_LOGIN_GET_COUPON, interval=1):
            return True
        # 下载插画
        if self.appear_then_click(self.I_LOGIN_LOAD_DOWN, interval=1):
            logger.info('Download inbetweening')
            return True
        # 不观看视频
        if self.appear_then_click(self.I_WATCH_VIDEO_CANCEL, interval=0.6):
            logger.info('Close video')
            return True
        # 右上角的红色的关闭
        if self.appear_then_click(self.I_LOGIN_RED_CLOSE, interval=1.6):
            logger.info('Close red close')
            return True
        # 左上角的黄色关闭
        if self.appear_then_click(self.I_LOGIN_YELLOW_CLOSE, interval=1.6):
            logger.info('Close yellow close')
            return True
        # 个性化推荐提示与绑定手机号弹窗是两个独立弹窗，识别顺序排在绑定之前；
        # interval 需小于 LOGIN_COURTYARD_CONFIRM_DELAY
        if self.appear_then_click(self.I_CLICK_REFUSE, interval=1):
            logger.info('Close personalized recommendation')
            return True
        # 绑定手机号弹窗
        if self.appear_then_click(self.I_LOGIN_LOGIN_GOTO_BIND_PHONE):
            while 1:
                self.screenshot()
                if self.appear_then_click(self.I_LOGIN_LOGIN_CANCEL_BIND_PHONE):
                    logger.info("Close bind phone")
                    break
            return True
        # 拒绝邀请弹窗已移除：匹配帧与点击帧之间存在竞态——邀请在点击落地前
        # 消失会让点击穿透到庭院左缘入口上，误开灯笼菜单导致页面识别死循环
        # （2026-08-31 远程事故）；登录期间的邀请放任自然过期即可。
        # from tasks.Component.GeneralInvite.assets import GeneralInviteAssets as gia
        # if self.appear_then_click(gia.I_I_REJECT, interval=0.8):
        #     logger.info("reject invites")
        #     return True
        # 阴阳师精灵提示分支已移除（用户决策），skip_onmyoji_genie 参数一并清理
        # if not self.skip_onmyoji_genie and self.appear_then_click(self.I_LOGIN_LOGIN_ONMYOJI_GENIE):
        #     logger.info("click onmyoji genie")
        #     return True
        return False
    

    def _get_login_handoff_task(self) -> str:
        """普通登录和切号后的登录必须进庭院，只有 Restart 可以指定接手任务。"""
        return ''

    def _prepare_login_handoff(self) -> bool:
        """只打开账号选择入口并验证可操作，不提交当前账号或进入游戏。"""
        assets = SwitchAccountAssets
        # is_account_dropdown_opened 内部已确认账号选择界面（网易游戏 LOGO）可见
        if is_account_dropdown_opened(self):
            # 列表可能已经展开；先收起，再验证完整表单，避免卡在等待状态。
            self.click(assets.C_SA_ACCOUNT_DROPDOWN_BLANK, interval=1)
            return False
        if self.appear(assets.I_SA_NETEASE_GAME_LOGO) and self.appear(assets.I_SA_ACCOUNT_LOGIN_BTN):
            # 平台选择会覆盖账号表单，必须先退回真正可选择账号的界面。
            if self.appear(assets.I_SA_LOGIN_FORM_APPLE):
                self.appear_then_click(assets.I_SA_APPLE_BACK, interval=1)
                return False
            return self.appear(assets.I_SA_ACCOUNT_DROP_DOWN_CLOSED)
        if self.appear_then_click(assets.I_SA_SWITCH_ACCOUNT_BTN, interval=1):
            return False
        # 误入选服/选角时只退回登录页，不确认任何角色。
        if self.appear(self.I_LOGIN_SPECIFIC_SERVE):
            self.appear_then_click(self.I_BACK_BLUE, interval=1)
        elif self.appear(assets.I_SA_CHECK_SELECT_SVR_1) or self.appear(assets.I_SA_CHECK_SELECT_SVR_2):
            self.click(assets.C_SA_LOGIN_FORM_CANCEL_SVR_SELECT, interval=1)
        elif self.appear(assets.I_SA_LOGIN_FORM_APPLE):
            self.appear_then_click(assets.I_SA_APPLE_BACK, interval=1)
        elif self.appear(self.I_CHECK_LOGIN_FORM) or self.appear(self.I_LOGIN_8):
            # 登录页入口只有坐标资产；点击后还要等切换按钮和账号下拉框实际出现。
            self.click(assets.C_SA_LOGIN_FORM_USER_CENTER, interval=1)
        return False

    def _app_handle_login(self, stop_at_login: bool = False) -> bool:
        """
        普通登录最终到庭院；指定交接时在可操作的账号选择页结束。
        :return:
        """
        logger.hr('App login')
        # 这次登录可能落到另一个号上，之前锁定的庭院/卷轴皮肤对它不一定成立，先解锁。
        # 本次登录里已经不再探测（见下方 courtyard_mark 那段），解锁是为了让**导航失败**
        # 时的探测能对新号重新生效。
        # 放在这个私有函数开头而不是 app_handle_login()：后者的重试循环在函数体内，
        # 放外面只解锁一次，第二轮重试会带着上一轮的锁状态进来。
        release_costume_probe_locks()
        self.device.stuck_record_add('LOGIN_CHECK')

        # 庭院二次确认计时器。首次看到干净庭院时才 start，到点后再判一次才认定登录完成；
        # 中途画面不是干净庭院就 clear，等下次重新看到庭院从头计时。
        courtyard_timer = Timer(LOGIN_COURTYARD_CONFIRM_DELAY)
        orientation_timer = Timer(10)
        login_success = False
        # 本次登录已处理过的 MPay 弹窗次数，用于给「关掉又重弹」封顶
        popup_handled = 0

        def handle_desktop_login_popup() -> bool:
            """登录期间持续处理 MPay 弹窗，返回是否刚刚关闭了弹窗。"""
            nonlocal popup_handled
            if not self.device.is_desktop or not self.device.find_desktop_login_popup():
                return False
            popup_handled += 1
            if popup_handled > DESKTOP_MPAY_CLOSE_LIMIT:
                # 前几次都成功关掉了却又弹出来，说明登录靠回车已经走不出去，
                # 交给 Restart 清掉客户端重建，而不是在这里无限关弹窗
                raise GameStuckError(
                    f'Desktop MPay login popup reappeared more than {DESKTOP_MPAY_CLOSE_LIMIT} times')
            if not self.device.desktop_confirm_login_popup():
                # 弹窗可能恰好在确认函数最后一次枚举后自行关闭，重新确认避免误判为卡死。
                if not self.device.find_desktop_login_popup():
                    return True
                # 弹窗仍存活时不能继续识别游戏页面，否则会把 MPay 误报成未知页面。
                raise GameStuckError('Desktop MPay login popup cannot be closed')
            return True

        while 1:
            # 启动和加载期间任务可能到期或跨入禁止时段，每轮点击前刷新实际接手对象。
            # 一旦决定保护登录页就不再回退到默认账号登录；队列变化只能重新交接或停止。
            handoff_task = self._get_login_handoff_task()
            stop_at_login = (stop_at_login or bool(handoff_task)
                             or self.__dict__.get('_login_handoff_protected', False))
            self._login_handoff_protected = stop_at_login
            # MPay 可能在启动后、重登时或登录流程中途反复出现，每一轮都必须处理。
            if handle_desktop_login_popup():
                continue
            # Watch device rotation
            if not login_success and orientation_timer.reached():
                # Screen may rotate after starting an app
                self.device.get_orientation()
                orientation_timer.reset()

            self.screenshot()
            # 截图与窗口枚举存在竞态，截图后再检查一次，避免刚出现的 MPay 进入图像识别。
            if handle_desktop_login_popup():
                continue
            # 取消继续战斗
            if self.appear_then_click(self.I_CANCEL_BATTLE, interval=0.8):
                logger.info('Cancel continue battle')
                continue
            # ── 先处理弹窗，识别到一个就点掉并重来，本轮不碰庭院判定 ──
            # 弹窗背后就是庭院，庭院标识照样能匹配到，所以还有弹窗要处理时判庭院不可信。
            if self._login_popup_blocking():
                # 本轮处理了弹窗，已经开始的二次确认作废，等下次看到庭院重新计时
                courtyard_timer.clear()
                continue
            # ── 所有弹窗都不在，才判断是不是庭院 ──
            # 这些标识出现都意味着已经在庭院里：式神录按钮和展开的卷轴要卷轴展开后才有，
            # 卷轴收起图标与闲庭图标则是卷轴没展开时唯一的证据，少算哪个都会让庭院里的
            # 正常画面被判成「不是庭院」，计时被自己的卷轴动作反复清掉，确认永远走不完。
            #
            # I_CHECK_MAIN 是 page_main 的 check_button，本身就是「在庭院」的权威判据，
            # 且既不随卷轴皮肤变也不随庭院皮肤变（活动图标 / 加成按钮都是固定 UI）——
            # 卷轴换皮后上面两个 I_LOGIN_SCROOLL_* 会一起失效，只靠闲庭与式神录两路
            # 缺口太大，加上它才补得住。
            #
            # 一律不传 interval：判定的是画面状态而非点击节流，带上会让静默期内的每一帧
            # 都误判成非庭院；桌面端截图间隔可低到 0.05s，这种漏判会让确认永远走不完。
            courtyard_mark = (self.appear(self.I_CHECK_MAIN)
                              or self.appear(self.I_MAIN_BUFF)
                              or self.appear(self.I_MAIN_GOTO_SHIKIGAMI_RECORDS)
                              or self.appear(self.I_LOGIN_SCROOLL_OPEN)
                              or self.appear(self.I_LOGIN_SCROOLL_CLOSE)
                              or self.appear(self.I_LOGIN_COURTYARD))
            # 这里**不再跑皮肤探针**。原先写的是「一个证据都没命中 → 试试探测皮肤」，但
            # 「还没走到庭院」和「庭院判据坏了」在这一屏上长得一模一样，而这个循环从切
            # 角色的那一帧就开始跑，于是探针必被调用、还会在「进入游戏」过渡帧上盲扫
            # 16 张月亮模板而误判（2026-09-15 两次事故的起点就是它）。
            # 判据换成皮肤无关的活动图标与加成按钮之后，「认不出庭院」只剩「没到庭院」
            # 这一种成因，探针在这里收益为零、风险照旧，故整块删掉。
            # 皮肤纠正改由导航失败时触发（game_ui.py 的 _execute_path）。
            courtyard = courtyard_mark
            # 已看到庭院即可停掉屏幕旋转检测，不必等二次确认通过
            if courtyard:
                login_success = True
            if not courtyard:
                # 还没进庭院，之前的观察作废重新等
                courtyard_timer.clear()
            elif not courtyard_timer.started():
                # 首次看到庭院不能立刻认定：可能只是点掉一个弹窗、下一个还没弹出来的中间态
                courtyard_timer.start()
                logger.info(f'Courtyard appears, confirm again after {LOGIN_COURTYARD_CONFIRM_DELAY}s')
            elif courtyard_timer.reached():
                # 隔了一段时间后再次看到干净庭院，才认定这是稳定的庭院画面
                logger.info('Login to main confirm (courtyard stable)')
                break

            # ── 庭院里该点的东西。触发条件不能换成 courtyard：式神录按钮已经露出来时
            # 再点卷轴区域会把卷轴收回去。这些都是庭院内的动作，不影响已开始的计时。──
            # 确认进入庭院
            if self.appear_then_click(self.I_LOGIN_SCROOLL_CLOSE, interval=2):
                logger.info('Open scroll')
                continue
            # 确认进入庭院(优化：当出现闲庭图片时，点击卷轴关闭区域，然后判断式神录按钮出现就代表登录成功)
            if self.appear(self.I_LOGIN_COURTYARD, interval=0.2):
                if self.click(self.C_LOGIN_SCROLL_CLOSE_AREA, interval=2):
                    logger.info('Click scroll close area because courtyard appears')
                    self.screenshot()  # 点击后立即获取最新截图，确保后续状态检查准确
                    continue

            # 名单命中后，入口未就绪只能继续等待/退回登录页，不能兜底登录未知账号。
            if stop_at_login:
                if self._prepare_login_handoff():
                    if not handoff_task:
                        raise RequestHumanTakeover('切号入口已就绪，但后续任务已变化，请检查重启交接名单和调度')
                    raise LoginHandoffReady(handoff_task)
                continue

            # 当账号未登录时点击登录
            if self.appear_then_click(SwitchAccountAssets.I_SA_ACCOUNT_LOGIN_BTN, interval=0.8):
                logger.info("click login")
                continue
            if self.appear_then_click(SwitchAccountAssets.I_SA_LOGIN_FORM_ANDROID, interval=0.8):
                logger.info("click ANDROID")
                continue
            # 点击屏幕进入游戏
            if self.appear(self.I_LOGIN_SPECIFIC_SERVE, interval=0.6):
                if not self._select_login_character():
                    # 未识别只结束本次尝试；先退回登录页，现有账号级重试再重新进入。
                    self.ui_click(self.I_BACK_BLUE, self.I_LOGIN_8, interval=1)
                    logger.warning('Character selection failed, returned to login page')
                    return False
                logger.info('login specific user')
                continue
            
            # 创建角色, 误入新区直接重启
            if self.appear(self.I_CREATE_ACCOUNT):
                logger.warning('Appear create account')
                raise GameStuckError('Appear create account')

            # 点击“进入游戏”速度过快会进入区服设置，同时需在检测I_LOGIN_8之前检测，因为新服图标会让I_LOGIN_8向右偏移导致永远无法检测成功
            # 同时修复了点击位置（之前是点击I_CHARACTARS而不是左边的区域）
            if self.appear(self.I_CHARACTARS, interval=1):
                logger.info('误入区服设置')
                # https://github.com/runhey/OnmyojiAutoScript/issues/585
                self.device.click(x=106, y=535)
                
            # 点击’进入游戏‘
            if not self.appear(self.I_LOGIN_8):
                continue
            
            # 登录体验服时，点击“进入游戏”速度过快，可能会出现体验服的弹窗
            if self.appear(self.I_EARLY_SERVER):
                if self.appear_then_click(self.I_EARLY_SERVER_CANCEL):
                    logger.info('Cancel switch from early server to normal server')
                    continue
            if self.ocr_appear_click(self.O_LOGIN_ENTER_GAME, interval=3):
                self.wait_until_appear(self.I_LOGIN_SPECIFIC_SERVE, True, wait_time=5)
                continue

        return login_success

    def app_handle_login(self, start_app: bool = False, *, account_retry: bool = False) -> bool:
        # Restart 可要求在重试内启动游戏；其他调用方仍可直接处理已经打开的登录界面。
        # 桌面客户端的启动、清理和三轮重建统一由 _desktop_start_and_login 管理。
        # 这里若再 stop/start，会在内部重试耗尽后留下一个从未验证的新进程。
        # 每次启动重新选择交接对象；普通切号调用方始终完整登录。
        handoff_task = '' if account_retry else self._get_login_handoff_task()
        # 当前任务实例的应用重试共享保护状态；切号会新建普通 LoginHandler。
        self._login_handoff_protected = (bool(handoff_task)
                                        or self.__dict__.get('_login_handoff_protected', False))
        attempts = 1 if self.device.is_desktop else 2
        for attempt in range(attempts):
            self.device.stuck_record_clear()
            self.device.click_record_clear()
            try:
                # 只有真正开始下一轮登录才启动，最后一次失败后不再额外拉起游戏。
                if start_app or attempt > 0:
                    self.device.app_start()
                # 保留普通调用签名，切号组件使用的 LoginHandler 不会启用提前结束。
                result = self._app_handle_login(stop_at_login=True) if handoff_task else self._app_handle_login()
                if result is False:
                    # 切号调用方已有三次账号级预算，普通未识别不叠加应用重启重试。
                    if account_retry:
                        return False
                    # 独立 Restart 没有账号队列接管失败，仍沿用原有登录恢复。
                    raise GameStuckError('Login character selection failed')
                # 桌面分支：登录成功标记登录态，使 app_is_running 判定为已在游戏中
                if self.device.is_desktop:
                    self.device.desktop_mark_logged_in()
                if self.config.restart.harvest_config.enable:
                    self.harvest()
                return True
            except LoginHandoffReady as ready:
                # 模拟器已到账号选择页，不领取庭院奖励，后续任务直接从切号表单接手。
                handoff_task = ready.args[0]
                self.device.stuck_record_clear()
                self.device.click_record_clear()
                logger.info(f'账号选择入口已就绪，结束重启并交给 {handoff_task} 切号')
                return True
            except (GameNotRunningError, GameTooManyClickError, GameStuckError) as e:
                logger.warning(e)
                if self.device.is_desktop:
                    raise
                # 最后一轮也清理失败的游戏进程，但启动留给下一轮 try 内执行。
                self.device.app_stop()

        logger.critical(f'Login failed after {attempts} attempts')
        logger.critical('Onmyoji server may be under maintenance, or you may lost network connection')
        raise RequestHumanTakeover

    def harvest(self):
        """
        获得奖励
        :return: 如果没有发现任何奖励后退出
        """
        logger.hr('Harvest')
        # 各领取项独立遵守配置开关，避免关闭某项后仍被登录收获流程点击。
        harvest = self.config.restart.harvest_config
        timer_harvest = Timer(5)  # 如果连续5秒没有发现任何奖励，退出
        skip_default = False
        courtyard_affairs_done = False  # 庭院事务只执行一次
        while 1:
            self.screenshot()

            # 点击'获得奖励'
            if self.ui_reward_appear_click():
                timer_harvest.reset()
                continue
            # 获得奖励
            if self.appear_then_click(self.I_UI_AWARD, interval=0.2):
                timer_harvest.reset()
                continue
            # 偶尔会打开到聊天频道
            if self.appear_then_click(self.I_HARVEST_CHAT_CLOSE, interval=1):
                timer_harvest.reset()
                continue
            # 偶尔会进入其他页面
            # 左上角的黄色关闭
            if self.appear_then_click(self.I_LOGIN_YELLOW_CLOSE, interval=0.6):
                timer_harvest.reset()
                logger.info('Close yellow close')
                continue
            # 关闭宠物小屋
            if self.appear_then_click(self.I_HARVEST_BACK_PET_HOUSE, interval=0.6):
                timer_harvest.reset()
                logger.info('Close yellow close')
                continue
            # 御魂溢确认
            if self.appear_then_click(self.I_UI_CONFIRM_SAMLL, interval=2.5):
                timer_harvest.reset()
                skip_default = True
                logger.info('Soul overflow')
                continue
            # 关闭姿度出现的蒙版
            if self.appear(self.I_HARVEST_ZIDU, interval=1):
                timer_harvest.reset()
                self.I_HARVEST_ZIDU.roi_front[0] -= 200
                self.I_HARVEST_ZIDU.roi_front[1] -= 200
                if self.click(self.I_HARVEST_ZIDU, interval=2):
                    logger.info('Close zidu')
                continue

            # 庭院事务
            if harvest.enable_courtyard_affairs and not courtyard_affairs_done:
                self.harvest_courtyard_affairs()
                timer_harvest.reset()
                courtyard_affairs_done = True
                continue
            # 勾玉
            if harvest.enable_jade and self.appear_then_click(self.I_HARVEST_JADE, interval=1.5):
                timer_harvest.reset()
                continue
            # 签到
            if harvest.enable_sign and self.appear_then_click(self.I_HARVEST_SIGN, interval=1.5):
                self.wait_until_appear(self.I_HARVEST_SIGN_2, wait_time=2)
                timer_harvest.reset()
                continue
            # 某些活动的特殊签到，有空看到就删掉
            if harvest.enable_sign and self.appear_then_click(self.I_HARVEST_SIGN_3, interval=0.7):
                timer_harvest.reset()
                continue
            if harvest.enable_sign and self.appear_then_click(self.I_HARVEST_SIGN_4, interval=1):
                timer_harvest.reset()
                continue
            if harvest.enable_sign and self.appear_then_click(self.I_HARVEST_SIGN_2, interval=1.5):
                self.wait_until_appear(self.I_LOGIN_RED_CLOSE, wait_time=2)
                timer_harvest.reset()
                continue
            # 999天的签到福袋
            if harvest.enable_sign_999 and self.appear_then_click(self.I_HARVEST_SIGN_999, interval=1.5):
                timer_harvest.reset()
                continue
            # 判断是否勾选了收取邮件（不收取邮件可以查看每日收获）
            if not skip_default and harvest.enable_mail and self.harvest_mail():
                timer_harvest.reset()
                continue
            if harvest.enable_ap and self.appear_then_click(self.I_HARVEST_AP, interval=1, threshold=0.7):
                timer_harvest.reset()
                continue
            # 御魂觉醒加成
            if harvest.enable_soul and self.appear_then_click(self.I_HARVEST_SOUL, interval=1):
                timer_harvest.reset()
                continue
            # 寮包
            if self.appear_then_click(self.I_HARVEST_GUILD_REWARD, interval=2):
                timer_harvest.reset()
                continue
            # 自选御魂
            if not skip_default and self.appear(self.I_HARVEST_SOUL_1):
                logger.info('Select soul 2')
                self.ui_click(self.I_HARVEST_SOUL_1, stop=self.I_HARVEST_SOUL_2)
                self.ui_click(self.I_HARVEST_SOUL_2, stop=self.I_HARVEST_SOUL_3, interval=3)
                self.ui_click_until_disappear(click=self.I_HARVEST_SOUL_3)
                timer_harvest.reset()

            # 红色的关闭
            if self.appear(self.I_LOGIN_RED_CLOSE):
                self.click(self.I_LOGIN_RED_CLOSE, interval=2)
                timer_harvest.reset()
                continue

            # 五秒内没有发现任何奖励，退出
            if not timer_harvest.started():
                timer_harvest.start()
            else:
                if timer_harvest.reached():
                    logger.info('No more reward')
                    return

    def set_specific_usr(self, character: str, svr: str = None):
        """设置角色与区服；同一帧中任意一个字段命中即可。

        @param character: 角色名
        @param svr: 区服名，不传则只匹配角色；仅填区服时角色传空字符串
        """
        self.character = character.strip()
        self.svr = svr.strip() if svr else ''
        # 显式字段分别比对；旧单槽的区服回退仅在配置初始化时启用。
        self._legacy_character_or_svr = False
        self.O_LOGIN_SPECIFIC_SERVE.keyword = self.character

    # ------------------------------------------------------------------
    # 选角：同帧角色或区服匹配 + 向上滑动 + 勾选态验证
    # 判据全部抽成不依赖 device 的静态方法，查找循环只负责截图与调用，便于单测。
    # ------------------------------------------------------------------

    @staticmethod
    def _match_character_index(ocr_results: list, character: str, svr: str, *,
                               legacy_either: bool = False) -> int:
        """共用一帧 OCR，区服优先、角色兜底；缺失返回 -1。"""
        index = find_character_index(ocr_results, character, svr,
                                     legacy_either=legacy_either)
        return index if index is not None else -1

    @staticmethod
    def _ocr_box_to_roi(ocr_roi: tuple, box) -> list:
        """把 detect_and_ocr 返回的 box（相对 OCR ROI 的四点坐标）换算成设备空间的 roi。

        @return: [x, y, w, h]
        """
        return [
            ocr_roi[0] + box[0][0],
            ocr_roi[1] + box[0][1],
            box[1][0] - box[0][0],
            box[2][1] - box[1][1],
        ]

    @staticmethod
    def _is_select_mark_aligned(mark_y: int, target_y: int) -> bool:
        """勾选标记的中心 y 与目标文本的中心 y 是否落在同一个条目内。"""
        return abs(mark_y - target_y) <= SELECT_CHARACTER_Y_TOLERANCE

    def _select_login_character(self) -> bool:
        """在角色列表里选中目标角色并确认登录。

        三个阶段：查找（一轮一次 OCR，未命中则向上滑动重试）→ 选中（点击并用勾选标记验证）
        → 确认（点确认按钮直到列表界面消失）。

        查找或选中验证失败返回 False，调用方返回登录页后复用账号级重试。
        """
        target_click, target_y = self._find_login_character()

        if target_click is None:
            if self.character or self.svr:
                logger.warning('Login character not found: %s / %s', self.character, self.svr)
                return False
        elif not self._ensure_character_selected(target_click, target_y):
            logger.warning('Login character not selected: %s / %s', self.character, self.svr)
            return False

        # 只有未配置角色时才允许默认登录；已配置角色须通过上面的查找和选中验证。
        while True:
            self.screenshot()
            if self.appear(self.I_LOGIN_SPECIFIC_SERVE):
                self.click(self.C_LOGIN_ENSURE_LOGIN_CHARACTER_IN_SAME_SVR, interval=2)
                continue
            break
        return True

    def _find_login_character(self):
        """查找目标角色，返回 (点击区域, 目标文本中心 y)；无目标可点时返回 (None, None)。

        同一帧比对角色名与区服名，任一命中即可；连续三帧空 OCR 才交账号级重试。
        有文本但未命中才滑动，两轮结果相同即到底，不另设扫描次数上限。
        """
        # 未指定角色和区服：不 OCR、不滑动，直接登默认第一个角色（保持原有行为）
        if not self.character and not self.svr:
            logger.info('No specific character or svr configured, login default one')
            return None, None

        ocr_roi = self.O_LOGIN_SPECIFIC_SERVE.roi
        last_texts = None
        empty_ocr_count = 0
        while True:
            self.screenshot()
            ocr_res = self.O_LOGIN_SPECIFIC_SERVE.detect_and_ocr(self.device.image)
            # 空帧不滑动、不判断到底，先原地等下一帧，避免短暂加载消耗账号重试。
            if not any(item.ocr_text.strip() for item in ocr_res):
                empty_ocr_count += 1
                if empty_ocr_count >= CHARACTER_LIST_EMPTY_OCR_LIMIT:
                    logger.warning('Character list OCR empty for %d consecutive frames', empty_ocr_count)
                    return None, None
                logger.info('Character list OCR empty (%d/%d), wait and retry in place',
                            empty_ocr_count, CHARACTER_LIST_EMPTY_OCR_LIMIT)
                time.sleep(CHARACTER_LIST_EMPTY_OCR_DELAY)
                continue
            # 有效帧清零计数，下一页短暂空白仍可原地等待。
            empty_ocr_count = 0

            # OCR 返回顺序变化不代表列表发生移动，按视觉位置统一顺序再判定到底。
            texts = tuple(item.ocr_text for item in sorted(
                ocr_res, key=lambda item: (min(point[1] for point in item.box),
                                          min(point[0] for point in item.box))))
            index = self._match_character_index(ocr_res, self.character, self.svr,
                                                legacy_either=self._legacy_character_or_svr)
            if index < 0:
                if texts == last_texts:
                    # 两轮结果一致说明已滑到底；返回查找失败，禁止兜底选择别的角色。
                    logger.error('Character %s / svr %s not found after scrolling to end, '
                                 'stop character selection', self.character, self.svr)
                    return None, None
                else:
                    last_texts = texts
                    self.swipe(self.S_LOGIN_CHARACTER_LIST_UP)
                    time.sleep(CHARACTER_LIST_SWIPE_DELAY)
                    continue

            box = ocr_res[index].box
            roi = self._ocr_box_to_roi(ocr_roi, box)
            # 文本框中心 y（设备空间），用于和勾选标记的中心 y 比对
            target_y = ocr_roi[1] + (box[0][1] + box[2][1]) // 2
            return RuleClick(roi, roi, 'character select'), target_y

    def _find_select_mark_y(self):
        """返回本帧勾选中心 y；插画干扰彩图时，用亮色轮廓保留勾选形状证据。"""
        rule = self.I_SELECT_CHARACTER
        # 原模板命中后立即读取回写坐标，不能复用上一帧的 roi_front。
        if self.appear(rule):
            mark = rule.roi_front
            return mark[1] + mark[3] // 2

        # 背景透过条目后颜色会变化，而勾选主体仍为亮色；连同轮廓外的暗区一起
        # 做二值模板匹配，避免把单纯明亮的插画误当成勾选。
        template = cv2.inRange(cv2.cvtColor(rule.image, cv2.COLOR_RGB2GRAY),
                               SELECT_CHARACTER_BRIGHTNESS, 255)
        source = cv2.inRange(cv2.cvtColor(rule.corp(self.device.image), cv2.COLOR_RGB2GRAY),
                             SELECT_CHARACTER_BRIGHTNESS, 255)
        result = cv2.matchTemplate(source, template, cv2.TM_CCOEFF_NORMED)
        _, score, _, position = cv2.minMaxLoc(result)
        if score < SELECT_CHARACTER_SHAPE_THRESHOLD:
            return None
        logger.info('Select mark matched by brightness shape, score=%.3f', score)
        return rule.roi_back[1] + position[1] + template.shape[0] // 2

    def _ensure_character_selected(self, target_click: RuleClick, target_y: int) -> bool:
        """点击目标条目并用勾选标记验证选中态，最多重试 SELECT_CHARACTER_CLICK_RETRY 次。

        每轮先验证再点击，所以默认高亮恰好就是目标时会零点击直接通过。
        最后一次点击后也要重新验证；失败返回 False，由调用方进入有界登录重试。
        """
        for attempt in range(SELECT_CHARACTER_CLICK_RETRY + 1):
            self.screenshot()
            # 两种识别都必须命中真实勾选，并与目标同条目对齐后才能确认登录。
            mark_y = self._find_select_mark_y()
            if mark_y is not None:
                logger.info('Select mark y=%d, target y=%d, delta=%d (tolerance=%d)',
                            mark_y, target_y, abs(mark_y - target_y), SELECT_CHARACTER_Y_TOLERANCE)
                if self._is_select_mark_aligned(mark_y, target_y):
                    logger.info('Target character selected')
                    return True
            # 多一次验证，但点击次数仍限制为 SELECT_CHARACTER_CLICK_RETRY。
            if attempt == SELECT_CHARACTER_CLICK_RETRY:
                break
            self.click(target_click)
            time.sleep(1)

        logger.error('Select mark not aligned with target after %d clicks',
                     SELECT_CHARACTER_CLICK_RETRY)
        return False

    def harvest_mail(self) -> bool:
        if not self.appear_multi_scale(self.I_HARVEST_MAIL,scale_range=(0.8, 1.1)) and \
                not self.appear(self.I_HARVEST_MAIL_COPY):
            if not self.appear(self.I_READ_ALL_MAIL):
                return False
        logger.info('Harvest mail')
        while 1:
            self.screenshot()
            if self.appear(self.I_READ_ALL_MAIL):
                break
            if self.appear_then_click_multi_scale(self.I_HARVEST_MAIL, interval=1.5, scale_range=(0.8, 1.1)):
                continue
            if self.appear_then_click(self.I_HARVEST_MAIL_COPY, interval=1.5):
                continue
        timeout_timer = Timer(3).start()
        logger.info('Exec harvest mail')
        while 1:
            self.screenshot()
            if timeout_timer.reached():
                break
            if self.appear_then_click(self.I_HARVEST_MAIL_CONFIRM, interval=0.8):
                break

            if self.appear_then_click(self.I_READ_ALL_MAIL, interval=1.5):
                continue
            if self.appear_then_click(self.I_HARVEST_MAIL_ALL, interval=1.5):
                continue
            if self.appear_then_click(self.I_MAIL_RED_POINT, interval=4):
                continue
        self.ui_click_until_disappear(self.I_LOGIN_RED_CLOSE)
        return True
    
    def harvest_courtyard_affairs(self) -> bool:
        if not self.ui_click_multi_scale(self.I_NOTE, self.I_PAGE, timeout=3, scale_range=(0.8, 1.2)):
            logger.warning('courtyard affairs timeout!')
            return False
        count_success = 0
        while 1:
            self.screenshot()
            if self.appear(self.I_NO_TASKS):
                logger.info('courtyard affairs completed！')
                break
            # 每日六星御魂
            if self.appear_then_click(self.I_HARVEST_SOUL_2, interval=1) \
                    or self.appear_then_click(self.I_HARVEST_SOUL_3, interval=1):
                continue
            # 点击'获得奖励'
            if self.ui_reward_appear_click():
                continue
            # 获得奖励
            if self.appear_then_click(self.I_UI_AWARD, interval=0.2):
                continue
            # 式神满级，是否提取物经验？确定
            if self.appear_then_click(self.I_CONFIRM, interval=1):
                continue

            if self.appear_then_click(self.I_DAILY, interval=1):
                continue
            # 领取成功： 太傻逼了收取结界奖励游戏里面居然没有加上限制
            if self.appear_then_click(self.I_SUCCESS_CLAIMED, interval=1):
                continue
            if self.appear_then_click(self.I_SKIP):# 万花牌跳过
                continue
            if self.appear_then_click(self.I_LOGIN_RED_CLOSE, interval=1):# 万花牌X
                continue
            # 一键完成
            if count_success >= 3:
                logger.info(f'Click complete tasks {count_success} times')
                break
            if self.appear_then_click(self.I_COMPLETE_TASKS, interval=2.3):
                count_success += 1
                continue
        return True
