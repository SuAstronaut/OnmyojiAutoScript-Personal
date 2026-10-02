# This Python file uses the following encoding: utf-8
# @author runhey
# github https://github.com/runhey
import time

import importlib
import sys
from pathlib import Path

from datetime import datetime
from time import sleep

import random
from collections import deque
from module.atom.click import RuleClick
from module.atom.gif import RuleGif
from module.atom.image import RuleImage
from module.atom.list import RuleList
from module.atom.ocr import RuleOcr
from module.base.decorator import run_once
from module.base.timer import Timer
from module.exception import (GameNotRunningError, GamePageUnknownError)
from module.logger import logger
from tasks.Component.GeneralBattle.assets import GeneralBattleAssets
from tasks.GameUi.assets import GameUiAssets
from tasks.GameUi.page import Page, PageRegistry, page_login, page_main, random_click,page_mall,page_shikigami_records,page_onmyodo,page_friends,page_guild,page_team,page_collection,page_travel,page_daily,page_theme
from tasks.Restart.assets import RestartAssets
from tasks.SixRealms.assets import SixRealmsAssets
from tasks.base_task import BaseTask
from tasks.ActivityShikigami.assets import ActivityShikigamiAssets


# 判「卷轴是否展开」用的入口：卷轴展开后它们会一起露出来，命中任一即算展开。
# 从 page_theme 的 9 个 OCR 出边里挑 4 个就够 —— 每多一个就多一次 OCR 推理，而判别力
# 几乎不增。用 OCR 而不是式神录按钮的图：展开后的排版随卷轴皮肤变，图判据在没采集过的
# 皮肤上照样认不出，而这些入口的文字不随皮肤变。
_SCROLL_ENTRIES = (
    GameUiAssets.O_PAGE_SHIKIGAMI_RECORDS,   # 式神录
    GameUiAssets.O_PAGE_GUILD,               # 阴阳寮
    GameUiAssets.O_PAGE_TEAM,                # 组队
    GameUiAssets.O_PAGE_COLLECTION,          # 图鉴
)


class _NavigationPageChanged(Exception):
    """导航中的覆盖页已变化，放弃旧页面操作并从当前 Page 重新规划。"""


class GameUi(BaseTask, GameUiAssets):
    ui_current: Page = None
    ui_close = [GameUiAssets.I_BACK_MALL, GeneralBattleAssets.I_CONFIRM,
                BaseTask.I_UI_BACK_RED, BaseTask.I_UI_BACK_YELLOW,
                GameUiAssets.I_BACK_FRIENDS, GameUiAssets.I_BACK_DAILY,
                GameUiAssets.I_REALM_RAID_GOTO_EXPLORATION,
                GameUiAssets.I_SIX_GATES_GOTO_EXPLORATION, SixRealmsAssets.I_EXIT_SIXREALMS,
                ActivityShikigamiAssets.I_SKIP_BUTTON, ActivityShikigamiAssets.I_RED_EXIT, BaseTask.I_UI_BACK_BLUE]

    def __init__(self, config, device):
        super().__init__(config, device)
        # 初始化时动态导入所有 page 模块
        self._import_all_pages()
        # 卷轴皮肤未采集时的兜底点击本轮导航是否已用过（见 _execute_path）。
        # 每次 ui_goto 重置，保证一轮导航最多兜底一次。
        self._scroll_fallback_used = False

    @staticmethod
    def _import_all_pages():
        """动态加载 tasks/**/page.py"""
        base_dir = Path(__file__).resolve().parent.parent  # tasks 目录
        for task_dir in base_dir.iterdir():
            if not task_dir.is_dir():
                continue
            page_file = task_dir / "page.py"
            if not page_file.exists():
                continue
            module_name = f"tasks.{task_dir.name}.page"
            # 已加载过则直接复用缓存：重复 exec 会重新执行模块级 Page() 构造，
            # 把同名页面反复注册进 PageRegistry（每轮任务 +1），
            # 页面识别时注册表越遍历越慢（实测事故中单轮从 14s 拖到 53s）
            if module_name in sys.modules:
                continue
            spec = importlib.util.spec_from_file_location(module_name, page_file)
            if spec and spec.loader:
                module = importlib.util.module_from_spec(spec)
                # 先写入 sys.modules 再 exec，保证本次加载后后续调用能命中上面的缓存检查
                sys.modules[module_name] = module
                spec.loader.exec_module(module)

    @property
    def ui_pages(self) -> list[Page]:
        # 作用域按任务实例筛选；稳定排序保留旧页面次序，并让覆盖页优先于底层庭院。
        pages = [page for page in PageRegistry.all()
                 if page.enabled is None or page.enabled(self)]
        return sorted(pages, key=lambda page: not page.overlay)

    def _match_page(self, page: Page, interval=None) -> bool:
        """仅匹配当前帧；复合页面判据可读取多项素材与正文，不在这里截图或点击。"""
        if callable(page.check_button):
            return page.check_button(self)
        if isinstance(page.check_button, list):
            return any(self.appear(button, interval) for button in page.check_button)
        return self.appear(page.check_button, interval)

    def _current_overlay_page(self):
        """返回当前作用域内最上层的覆盖页，避免遮罩下页面通过到达判断。"""
        for page in self.ui_pages:
            if page.overlay and self._match_page(page):
                return page
        return None

    def maybe_screenshot(self, soft_skip: bool = False):
        """保留完整截图链；导航取帧后只识别覆盖页，具体点击仍由 Page.links 执行。"""
        image = super().maybe_screenshot(soft_skip)
        if getattr(self, '_overlay_navigation_active', False):
            overlay = self._current_overlay_page()
            if overlay != self.ui_current and (
                    overlay is not None or getattr(self.ui_current, 'overlay', False)):
                # 覆盖页出现、变化或消失都重算，不能继续点旧页面的通用确定按钮。
                self.ui_current = overlay
                raise _NavigationPageChanged()
        return image

    def ui_page_appear(self, page: Page, skip_first_screenshot: bool = True, interval: float = None):
        """
        判断当前页面是否为page
        """
        self.maybe_screenshot(skip_first_screenshot)
        # 直接等待页面也要遵守作用域和遮挡关系，不能绕过全页识别的优先级。
        if page.enabled is not None and not page.enabled(self):
            return False
        overlay = self._current_overlay_page()
        return page == overlay if overlay is not None else self._match_page(page, interval)

    def ui_wait_until_appear(self, page: Page, timeout: float = 5, interval: float = 0.5,
                             skip_first_screenshot: bool = True) -> bool:
        """
        等待页面出现
        :param page: 等待的页面
        :param timeout: 超时时间
        :param interval: 检查间隔时间
        :param skip_first_screenshot:
        :return: 页面出现返回True, 否则返回False
        """
        logger.info(f'Waiting for {page}')
        timeout_timer = Timer(timeout).start()
        while not timeout_timer.reached():
            if self.ui_page_appear(page, skip_first_screenshot, interval=interval):
                return True
            skip_first_screenshot = False
        return False

    def _costume_scroll_visible(self) -> bool:
        """卷轴两个状态（展开 / 收起）的图标至少认得出一个。

        两个都认不出，就说明卷轴皮肤与当前配置不符 —— 换皮后 theme_costume_model 替换的
        正是这两张图，它们会一起失效。两张图都走资产自带阈值（收起态 0.7），与 login.py
        的 courtyard_mark 保持一致。
        """
        return bool(self.appear(RestartAssets.I_LOGIN_SCROOLL_OPEN)
                    or self.appear(RestartAssets.I_LOGIN_SCROOLL_CLOSE))

    def _scroll_entries_visible(self) -> bool:
        """卷轴展开后底部那几个入口是否可见（_SCROLL_ENTRIES 任一命中即算）。

        成本说明：一次调用最多 4 次 OCR 推理，所以只在导航走到 page_main -> page_theme
        这条边时才调。不要挂进 page_theme.check_button —— 那个会随 ui_get_current_page
        的全页遍历反复执行。
        """
        return any(self.ocr_appear(entry) for entry in _SCROLL_ENTRIES)

    def _page_theme_appear(self) -> bool:
        """page_theme（卷轴展开态）是否可见。

        先走 check_button 的图判据（快，已采集的皮肤下够用），不命中再补一路底部入口的
        OCR —— 展开后的排版随卷轴皮肤变，没采集过的皮肤上图判据认不出，而入口的文字不随
        皮肤变。图判据放在前面是为了让正常路径零额外成本：OCR 只在它失败后才跑。
        """
        visible = self.ui_page_appear(page_theme, skip_first_screenshot=False)
        # 底栏 OCR 也能透过遮罩命中，不能绕过覆盖页的排他判断。
        return visible or (self._current_overlay_page() is None and self._scroll_entries_visible())

    def _target_page_appear(self, page: Page) -> bool:
        """跳转目标页是否已经可见。page_theme 多一路 OCR 判据，其余页仍走 check_button。"""
        if page == page_theme:
            return self._page_theme_appear()
        return self.ui_page_appear(page, skip_first_screenshot=False)

    def ui_get_current_page(self, skip_first_screenshot=True, accept_login: bool = False) -> Page:
        """
        获取当前页面
        :param skip_first_screenshot:
        :param accept_login: True 时把登录页当作合法当前页返回（切号场景需要停在登录页操作），
                             False 时检测到登录页抛 GameNotRunningError 交由 Restart 重新登录
        :return:
        """
        #logger.info("UI get current page")

        @run_once
        def app_check():
            if not self.device.app_is_running():
                raise GameNotRunningError("Game not running")

        @run_once
        def minicap_check():
            if self.config.script.device.control_method == "uiautomator2":
                self.device.uninstall_minicap()

        @run_once
        def rotation_check():
            self.device.get_orientation()

        def desktop_login_popup_check():
            """MPay 存活时游戏画面上的 Page 标志全部无效，交给 Restart 处理。"""
            if not self.device.is_desktop:
                return
            if self.device.find_desktop_login_popup():
                logger.warning('Desktop MPay login popup present, client is not logged in')
                # 客户端已掉回未登录态，复位登录标记，让 app_is_running 的任务前置检查
                # 也能直接拦下，不必等下一个任务跑起来再发现
                self.device.desktop_mark_logged_out()
                raise GameNotRunningError('Desktop MPay login popup present')

        # MPay 是独立顶层窗口，不能通过游戏截图中的 Page 素材可靠识别。
        desktop_login_popup_check()
        timeout = Timer(10, count=20).start()
        while 1:
            self.maybe_screenshot(skip_first_screenshot)
            skip_first_screenshot = False
            # 弹窗可能在页面轮询期间出现，每轮截图后重新检查 HWND 是否存活。
            desktop_login_popup_check()
            # 如果10S还没有到底，那么就抛出异常
            if timeout.reached():
                break
            # Known pages
            redetect = False
            for page in self.ui_pages:
                if not page.check_button:
                    continue
                if self.ui_page_appear(page=page, interval=None):
                    logger.attr("UI", page.name)
                    if page == page_login:
                        # 默认语义：登录页=掉线信号，抛异常让调度器走 Restart 重新登录；
                        # 仅切号流程（accept_login=True）把登录页当合法当前页，用于选账号/角色
                        if accept_login:
                            self.ui_current = page
                            return page
                        raise GameNotRunningError("Login page detected")
                    # 在庭院却认不出卷轴本身：卷轴换皮后两张 I_LOGIN_SCROOLL_* 一起失效，
                    # 而 I_CHECK_MAIN 不随卷轴变、庭院照样判得出来。不在这里补一次探测，
                    # 卷轴皮肤与配置不符就永远发现不了 —— 登录期已经不探（见 login.py 的
                    # courtyard_mark 段），而庭院判据皮肤无关，导航失败那条路也不会因为
                    # 卷轴不符而走到（卷轴不参与页面识别，认不出它照样在 page_main）。
                    # 探到就重截一帧重判，让修正后的卷轴资产立刻生效（探针自带节流与锁）。
                    # 只挂 page_main：卷轴开合不参与页面识别，庭院一律判成 page_main，
                    # page_theme 只做导航中转、永远不会由这里返回。
                    if page == page_main and not self._costume_scroll_visible():
                        if self.try_detect_costume():
                            logger.info('Costume fixed by probe, re-check current page')
                            redetect = True
                            break
                    self.ui_current = page
                    return page
            if redetect:
                continue
            # ── 全页扫描全落空 ──
            # 庭院判据已皮肤无关（活动图标 / 加成按钮），「认不出庭院」不再由皮肤引起，
            # 所以这一支**不再是皮肤修复的主路径**（那条挪到了 _execute_path 的导航失败
            # 分支）。留着的是它的另一层作用：把画面当作庭院，让 page_main 的 additional
            # 去关掉盖住整屏的弹窗。
            # 位置要在下面 _try_back_main_shortcut / try_close_unknown_page 之前——
            # 那两支会重置 timeout 并可能把画面导离庭院。也不能再往前挪到页面循环之前，
            # 那会让每一轮都白跑一次探测。
            detected = self.try_detect_costume_main()
            if detected is not None:
                logger.info(f'Courtyard recognised as {detected}, recover page_main')
                self.ui_current = page_main
                return page_main
            # Try to close unknown page: 优先尝试 I_BACK_MAIN 回主页
            if self._try_back_main_shortcut(skip_first_screenshot=False):
                timeout = Timer(10, count=20).start()
            elif self.try_close_unknown_page():
                timeout = Timer(10, count=20).start()
            else:
                # entirely unknown page, click safe random area
                #self.click(random_click(), interval=4)
                pass
            # wait to ui
            sleep(0.3)
            app_check()
            minicap_check()
            rotation_check()
        # Unknown page, need manual switching
        logger.warning("Unknown ui page")
        logger.attr("EMULATOR__SCREENSHOT_METHOD", self.config.script.device.screenshot_method)
        logger.attr("EMULATOR__CONTROL_METHOD", self.config.script.device.control_method)
        logger.warning("Starting from current page is not supported")
        logger.warning(f"Supported page: {[str(page) for page in self.ui_pages]}")
        logger.warning('Supported page: Any page with a "HOME" button on the upper-right')
        logger.critical("Please switch to a supported page before starting oas")
        raise GamePageUnknownError

    def ui_button_interval_reset(self, button):
        """
        Reset interval of some button to avoid mistaken clicks

        Args:
            button (Button):
        """
        if getattr(button, 'name', None) and button.name in self.interval_timer:
            self.interval_timer[button.name].reset()

    def build_reverse_path_dict(self, destination: Page) -> dict[Page, list[Page]]:
        """
        构建从每个页面到目标页面的最短路径（反向 BFS）

        Returns:
            dict[Page, list[Page]] -> {start_page: [page1, ...destinationPage], ...}
        """
        paths = {destination: [destination]}
        queue = deque([destination])
        while queue:
            cur = queue.popleft()
            for page in self.ui_pages:
                if page not in paths and cur in page.links:
                    # page -> cur
                    paths[page] = [page] + paths[cur]
                    queue.append(page)
        return paths

    def build_reverse_paths(self, destination: Page) -> list[tuple[Page, list[Page]]]:
        """
        构建从每个页面到目标页面的最短路径（反向 BFS）
        路径从短到长排序

        Returns:
            [(start_page, [page1, ...destinationPage]), ...]
        """
        paths = self.build_reverse_path_dict(destination)
        # 转换成列表并按路径长度排序, 短到长
        sorted_paths = sorted(paths.items(), key=lambda kv: len(kv[1]))
        return sorted_paths

    def ui_goto_page(self, dest_page: Page, confirm_wait=0, skip_first_screenshot=True, timeout: int = 60) -> bool:
        """前往指定page, 自动调用获取当前页面方法, 其他参数同ui_goto
        """
        self.ui_get_current_page()
        return self.ui_goto(dest_page, confirm_wait, skip_first_screenshot, timeout)

    def _try_back_main_shortcut(self, skip_first_screenshot=True) -> bool:
        """
        尝试使用一键回主页按钮(I_BACK_MAIN)直接回到 page_main。
        当页面未识别或目标是 page_main 时调用。
        如果检测到并成功点击 I_BACK_MAIN，返回 True；否则返回 False。
        """
        self.maybe_screenshot(skip_first_screenshot)
        if self.appear(self.I_BACK_MAIN):
            logger.info(f"Found I_BACK_MAIN shortcut, clicking to go to page_main")
            if self.appear_then_click(self.I_BACK_MAIN, interval=1.0):
                # 等待 page_main 出现
                if self.ui_wait_until_appear(page_main, timeout=5, skip_first_screenshot=False):
                    logger.info("I_BACK_MAIN shortcut success, arrived at page_main")
                    self.ui_current = page_main
                    return True
        return False

    def ui_goto(self, destination: Page, confirm_wait=0, skip_first_screenshot=True, timeout: int = 60) -> bool:
        """按 Page 出边导航；作用域内新出现的覆盖页会中断旧路径并重新规划。"""
        if not any(page.overlay for page in self.ui_pages):
            return self._ui_goto(destination, confirm_wait, skip_first_screenshot, timeout)
        previous_active = getattr(self, '_overlay_navigation_active', False)
        self._overlay_navigation_active = True
        total_timer = Timer(timeout).start()
        try:
            while not total_timer.reached():
                try:
                    # 已缓存目标页时也先核对画面，不能因路径只有一页就跳过新弹窗。
                    self.maybe_screenshot(skip_first_screenshot)
                    return self._ui_goto(
                        destination, confirm_wait, True, max(0, timeout - total_timer.current()))
                except _NavigationPageChanged:
                    # 仅重算 Page 路径，不走未知页关闭，更不把总超时预算重新置满。
                    logger.info(f'导航覆盖页变化，重新规划路径: {self.ui_current}')
                    skip_first_screenshot = False
            logger.error(f'Cannot goto page[{destination}], timeout[{timeout}s] reached')
            return False
        finally:
            self._overlay_navigation_active = previous_active

    def _ui_goto(self, destination: Page, confirm_wait=0, skip_first_screenshot=True, timeout: int = 60) -> bool:
        """
        Args:
            destination (Page):
            confirm_wait:
            skip_first_screenshot:
        :return: find destination page or timeout reached
        """
        logger.hr(f"UI goto {destination}")
        # 初始化
        timeout_timer = Timer(timeout).start()
        confirm_timer = Timer(confirm_wait, count=int(confirm_wait // 0.5)).start()
        close_unknown_timer = Timer(3).start()
        # 卷轴兜底点击一轮导航最多用一次：展开区域是 toggle，反复点会来回切换
        self._scroll_fallback_used = False
        # 构建路径映射
        path_dict = self.build_reverse_path_dict(destination)

        found = False
        while not timeout_timer.reached():
            if found:
                confirm_timer.wait()
                return True
            confirm_timer.reset()
            # 如果目标是 page_main，先尝试一键回主页快捷方式
            if destination == page_main and self.ui_current != page_main:
                if self._try_back_main_shortcut(skip_first_screenshot=skip_first_screenshot):
                    found = True
                    continue
                skip_first_screenshot = False
            path = path_dict.get(self.ui_current, None)
            # 找不到路径时，先尝试 I_BACK_MAIN 回到 page_main，再从 page_main 出发
            if not path:
                if self.ui_current != page_main and self._try_back_main_shortcut(skip_first_screenshot=skip_first_screenshot):
                    # 成功回到 page_main，重新构建路径继续导航
                    skip_first_screenshot = False
                    continue
                self.ui_get_current_page(skip_first_screenshot)
                continue
            skip_first_screenshot = False
            logger.info(f"Current page: {self.ui_current}. Following shortest path:")
            show_paths: str = ' -> '.join([p.name for p in path])
            logger.info(f"{show_paths}")
            # 遍历路径
            found = self._execute_path(path, timeout_timer)
            if not found:
                if close_unknown_timer.reached_and_reset():
                    self.try_close_unknown_page(skip_screenshot=False)
                    self.ui_current = None
        else:
            logger.error(f'Cannot goto page[{destination}], timeout[{timeout}s] reached')
        return False

    def try_close_unknown_page(self, skip_screenshot: bool = True):
        """
        尝试关闭未知界面
        :return: 执行了关闭返回True, 否则False
        """
        self.maybe_screenshot(skip_screenshot)
        timer = Timer(None).start()
        for close in self.ui_close:
            if self.appear_then_click(close, interval=1.5):
                logger.warning('Trying to switch to supported page')
                logger.info(f'[{timer.current():.1f}s]Click {close} on {self.ui_current} success')
                return True
        return False

    def _execute_path(self, path: list, timeout_timer):
        """
        执行路径
        :param path: currentPage,page1,page2,...,destinationPage
        :param timeout_timer: 超时定时器
        :return: currentPage==destinationPage
        """
        for i, current_page in enumerate(path):
            if timeout_timer.reached():
                return False
            # 当前页不等于路径中对应页, 尝试下一页
            if self.ui_current != current_page:
                continue
            # 无 additional 的页面也要检查覆盖状态，防止沿用遮罩出现前缓存的路径。
            if getattr(self, '_overlay_navigation_active', False):
                self.maybe_screenshot(False)
            self.run_additional(current_page, interval=0.6, skip_first_screenshot=False)
            # 如果已经是最后一页，不再跳转
            if i == len(path) - 1:
                if len(path) == 1:
                    logger.info(f'Page arrived {current_page}')
                break
            next_page = path[i + 1]
            logger.info(f'Page switch: {current_page} -> {next_page}')
            # 获取页面跳转操作
            button = current_page.links.get(next_page)
            if not button:
                logger.warning(f"No link from {current_page} to {next_page}")
                continue
            # 跳转页面
            max_wait_timer = Timer(6).start()
            logger.info(f'Wait appear and operate {button} on {current_page}')
            # 提前收工判据用的按钮：列表取第一个，与上面 exec_operates[0] 的语义一致
            probe = button[0] if isinstance(button, list) else button
            while not max_wait_timer.reached():
                if timeout_timer.reached():
                    return False
                if isinstance(button, list):
                    exec_operates = [self.appear_then_operate(btn, interval=0.8, skip_first_screenshot=False)
                                     for btn in button]
                    if exec_operates[0]:  # 只要第一个成功就跳出
                        break
                if self.appear_then_operate(button, interval=0.8, skip_first_screenshot=False):
                    break
                # 跳转按钮不出现、而目标页已经可见：这一步就没有可操作的对象了，
                # 直接进入到达判定，省掉最多 6 秒空等。典型是 page_main -> page_theme：
                # 卷轴已经展开时「收起卷轴」图不匹配，但目标页判据已经命中（page_theme
                # 走 _target_page_appear，图之外还有一路 OCR）。
                # 两个条件缺一不可：目标页判据可能与当前页重叠（卷轴展开态下
                # I_CHECK_MAIN 照样匹配），只看目标页会把该点的「收起卷轴」跳掉，
                # 卷轴就一直挂着。RuleClick 类跳转（固定点击区域）不参与本判据——
                # 它必然可点，该点就得点
                if isinstance(probe, (RuleImage, RuleGif, RuleOcr)) \
                        and not self.appear(probe) \
                        and self._target_page_appear(next_page):
                    logger.info(f'{next_page} already appear, skip operating {probe}')
                    break
            else:
                logger.warning(f'Failed recognize {button} on {current_page}')
                # 人在庭院却点不动跳转按钮：最可能的原因是这几张图与配置的皮肤不符
                # （庭院入口的 I_MAIN_GOTO_* / I_PET_HOUSE 都随皮肤变），6 秒里一个
                # 点击都发不出。这是解耦之后**唯一**的皮肤纠正时机——登录期不再探，
                # 而庭院判据已皮肤无关，「全页扫描落空」那条路不会再因为皮肤不符而走到。
                # 探到就直接放弃这一轮：check_costume_main 刚把这几张图换掉，
                # 必须重新截图重走一遍路径才用得上新模板，在同一帧上继续没有意义。
                # 交回给 ui_goto 的重试循环即可，它下一轮就会带新资产重进 _execute_path。
                if current_page == page_main and self.try_detect_costume():
                    logger.info('Costume main assets fixed by probe, retry navigation')
                    return False
                # 卷轴皮肤未采集时两张图都认不出 → 卷轴展不开 → 底部那排入口不出现 →
                # 目标页进不去，形成死循环。展开区域是固定坐标、不依赖任何一张图，
                # 是「未知皮肤」这种情况下唯一的突破口。
                #
                # 「卷轴是不是已经展开」用底部那排入口的 OCR 判，不用 check_button 的
                # 图判据：展开后的排版随卷轴皮肤变，式神录按钮图在没采集过的皮肤上照样
                # 认不出，而入口的文字不随皮肤变。OCR 说没展开才点 —— 展开区域是 toggle，
                # 判错方向就会把它点回收起。
                # 限次：一轮导航最多兜底一次，防展开后到达判定没跟上、下一轮又点回收起。
                if (not self._scroll_fallback_used
                        and current_page == page_main and next_page == page_theme
                        and not self._scroll_entries_visible()):
                    self._scroll_fallback_used = True
                    logger.info('Scroll images unrecognized, click the fixed expand area')
                    self.click(RestartAssets.C_LOGIN_SCROLL_CLOSE_AREA)
                self.ui_get_current_page(skip_first_screenshot=False)
                # 当前页面不是对应路径的页面, 则尝试下一个页面
                if self.ui_current != current_page:
                    continue
            max_wait_timer.reset()
            while not max_wait_timer.reached():
                if timeout_timer.reached():
                    return False
                # page_theme 的可见性要带上 OCR 判据（见 _page_theme_appear），而
                # ui_wait_until_appear 内部只认 check_button，所以对它特判；其余页保持
                # 原来的等待逻辑。特判分支每轮都要重截一帧，OCR 才有新画面可判。
                if next_page == page_theme:
                    # 注意参数名是 soft_skip 而不是 skip_first_screenshot：本文件另几处
                    # 都是位置传参、变量名与形参名对不上，这里显式写关键字，别跟着抄错
                    self.maybe_screenshot(soft_skip=False)
                    arrived = self._page_theme_appear()
                else:
                    arrived = self.ui_wait_until_appear(next_page, timeout=2.5,
                                                        skip_first_screenshot=False)
                if arrived:
                    logger.info(f'[{max_wait_timer.current():.1f}s]Page arrived {next_page}')
                    self.ui_current = next_page
                    break
            else:
                # 重新获取当前页
                self.ui_get_current_page(skip_first_screenshot=False)
        return self.ui_current == path[-1]

    def run_additional(self, page: Page, interval: float = None, skip_first_screenshot: bool = True):
        """执行页面附加操作"""
        if not page.additional:
            return

        # 先统一解析出所有附加项: condition, action, detect_seconds
        # 格式: btn                          -> condition=btn, action=btn, detect=0 (普通点击)
        #       [target, detect]             -> condition=target, action=target, detect=detect (限时检测点击)
        #       [condition, action]          -> condition=condition, action=action, detect=0 (复合条件)
        #       [condition, action, detect]  -> condition=condition, action=action, detect=detect (限时复合条件)
        items: list = []
        for btn in page.additional:
            condition = btn
            action = btn
            detect_seconds = 0
            if isinstance(btn, (list, tuple)):
                if len(btn) == 3 and isinstance(btn[2], (int, float)):
                    condition, action, detect_seconds = btn
                elif len(btn) == 2 and isinstance(btn[1], (int, float)):
                    condition, action, detect_seconds = btn[0], btn[0], btn[1]
                elif len(btn) == 2:
                    condition, action = btn
            items.append((condition, action, detect_seconds))

        # 限时检测: 所有限时项共享同一个超时窗口(取各项峰值时长)，而不是每项各自跑满自己的时长。
        # 总耗时由 sum(detect) 降为 max(detect)；且 appear() 读的是缓存帧, 一轮只截一次图就能同时
        # 匹配全部模板（原先每项独立截图, 一帧只查一个模板）。
        # 命中一项后立刻重置计时, 剩余项重新获得一个完整窗口, 所以「关掉一个又弹出下一个」仍能依次处理。
        timed = [item for item in items if item[2] > 0]
        timed_seconds = max((item[2] for item in timed), default=0)
        timed_done = False

        # 列表顺序即优先级: 从前往后处理, 靠前的先识别, 同一帧里多个都命中时也靠前的先操作。
        # 限时项整批只在「首个限时项」的位置执行一次 —— 排在它前面的非限时项先就地处理,
        # 从而保持配置里写下的先后顺序不被改写。
        for condition, action, detect_seconds in items:
            if detect_seconds > 0:
                if not timed_done:
                    self._run_timed_additional(page, timed, timed_seconds, interval)
                    timed_done = True
                    skip_first_screenshot = True  # 批次已截过图, 后续非限时项直接复用
                continue

            # 无延时时: 原有逻辑
            if condition is not action:
                # 复合条件操作: 出现条件图片时点击另一个区域
                self.maybe_screenshot(skip_first_screenshot)
                if self.appear(condition):
                    if self.appear_then_operate(action, interval=interval, skip_first_screenshot=False):
                        logger.info(f'Page {page} additional conditional {condition} -> {action} executed')
                        skip_first_screenshot = False
            elif self.appear_then_operate(condition, interval=interval, skip_first_screenshot=skip_first_screenshot):
                logger.info(f'Page {page} additional {condition} clicked')
                skip_first_screenshot = False

    def _run_timed_additional(self, page: Page, timed: list, timeout: float, interval: float) -> None:
        """共享超时窗口处理限时项。

        所有项共用一个计时器：每轮只截一次图供全部模板复用，并按列表顺序从前往后匹配。
        命中一项立即操作并从待检列表移除，随后重置计时器，让剩余项重新获得完整窗口。
        于是总耗时是 max(各项 detect) 而非 sum，且同帧多命中时靠前的项先被操作。
        """
        detect_timer = Timer(timeout).start()
        while timed and not detect_timer.reached():
            # 与普通附加操作共用导航取帧检查，发现新覆盖页后立即停止旧页面点击。
            self.maybe_screenshot(False)
            for i, (condition, action, _) in enumerate(timed):
                if not self.appear(condition):
                    continue
                timed.pop(i)
                # 复用刚匹配到的那一帧执行动作, 不再重复截图
                if self.appear_then_operate(action, interval=interval, skip_first_screenshot=True):
                    logger.info(f'Page {page} additional {condition} -> {action} detected within {timeout}s')
                detect_timer.reset()  # 找到一个就重置时间
                break  # 画面已变化, 跳出重新截图后再查剩余项
            else:
                sleep(0.1)
    def appear_then_operate(self, target: RuleList | RuleImage | RuleGif | RuleOcr | RuleClick,
                            interval: float = None, skip_first_screenshot: bool = True):
        """
        出现对应目标执行操作(点击图像, 滑动列表至array第一个元素并点击, 点击OCR, 点击)
        :param target: 目标
        :param interval: 间隔
        :param skip_first_screenshot: 是否跳过首次截图
        :return: 是否成功操作
        """
        self.maybe_screenshot(skip_first_screenshot)
        operated = False
        if isinstance(target, RuleList):
            operated = self.list_appear_click(target, interval=interval)
        elif isinstance(target, (RuleImage, RuleGif)):
            operated = self.appear_then_click(target, interval=interval)
        elif isinstance(target, RuleOcr):
            #logger.info(f'Trying to OCR target.area{target.area} target.roi{target.roi} for {target.name}')
            operated = self.ocr_appear_click(target, interval=interval)
        elif isinstance(target, RuleClick):
            operated = self.click(target, interval=interval)
        return operated


if __name__ == '__main__':
    from module.config.config import Config
    from module.device.device import Device
    from tasks.GameUi.page import page_guild

    c = Config('OAS1')
    d = Device(c)
    game = GameUi(config=c, device=d)
    game.screenshot()
    #game.appear_then_click(game.I_DLC_EXIT)
    #logger.info(game.ui_get_current_page())
    game.screenshot()
    game.ui_goto(page_main)
    game.ui_goto(page_shikigami_records)
    game.ui_goto(page_onmyodo)
    game.ui_goto(page_friends)
    game.ui_goto(page_guild)
    game.ui_goto(page_team)
    game.ui_goto(page_collection)
    game.ui_goto(page_travel)
    game.ui_goto(page_daily)
    game.ui_goto(page_mall)
    game.ui_goto(page_main)
