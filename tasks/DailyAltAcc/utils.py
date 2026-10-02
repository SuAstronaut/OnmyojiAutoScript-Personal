# This Python file uses the following encoding: utf-8
import functools

from module.config.config import Config
from module.device.device import Device
from module.logger import logger
from tasks.GameUi.game_ui import GameUi
from tasks.DailyAltAcc.assets import DailyAltAccAssets
from tasks.Plotline.assets import PlotlineAssets


def guild_popup_guard(method):
    """寮相关子任务入口装饰器：流程内启用「已收到碎片」等突发弹窗的统一跳过。

    DailyAltAcc 的所有子任务混在同一个 ScriptTask 实例里串行执行，MRO 无法
    按子任务切换 screenshot() 行为，因此用运行时标志划出作用域——只有套了
    本装饰器的寮子任务（祈愿/发布碎片/神社/寮信息）会做弹窗检测，庭院、
    邮箱、商城、试炼战斗等非寮子任务不付出检测成本、也不承担误点风险。
    """
    @functools.wraps(method)
    def wrapper(self, *args, **kwargs):
        with self.guild_popup_scope():
            return method(self, *args, **kwargs)
    return wrapper


class GuildPopupMixin:
    """寮页面突发弹窗统一跳过 mixin。

    「已收到碎片」(I_SUDDEN) 与「感谢」(I_PAGE_TK) 弹窗由寮友捐赠碎片触发，
    属于服务器推送的模态弹窗，随时可能盖在任何寮页面（神社/祈愿/寮信息）
    流程中途，挡住界面导致模板匹配全部落空。挂在 screenshot() 入口统一
    关闭，进入这些页面的子任务无需各自处理。

    默认不激活（_guild_popup_active=False）：寮子任务入口用 @guild_popup_guard
    装饰启用；整个任务都在寮页面的任务（如 ReturnGift）可类级置 True 常开。
    """

    # 需要统一跳过的寮页面突发弹窗：(模板, 弹窗名)，命中后点击同一红键关闭
    GUILD_POPUPS = (
        (DailyAltAccAssets.I_SUDDEN, '已收到碎片'),
        (DailyAltAccAssets.I_PAGE_TK, '感谢'),
    )
    # 顽固弹窗最多点击红键的次数，超过则放弃检测、交回任务流程兜底
    GUILD_POPUP_MAX_CLICKS = 5
    # 是否处于寮相关流程内（由 guild_popup_scope 置位），控制 screenshot() 是否检测
    _guild_popup_active = False

    def screenshot(self):
        """截图入口：寮相关流程内取帧后先关闭突发弹窗，再把画面交给任务流程。"""
        image = super().screenshot()
        if self._guild_popup_active:
            self.dismiss_guild_popup()
        return image

    def guild_popup_scope(self):
        """寮相关流程的作用域：进入时启用弹窗跳过，退出（含异常）时恢复原状。"""
        return _GuildPopupScope(self)

    def dismiss_guild_popup(self):
        """检测并关闭「已收到碎片」/「感谢」突发弹窗，点击红键直到消失。

        循环内改用 device.screenshot() 取帧：若走 self.screenshot() 会再次
        进入本检测，弹窗关闭动画未播完时形成递归（与 BaseTask._burst 的
        好友邀请处理做法一致）。
        """
        for rule, desc in self.GUILD_POPUPS:
            if not self.appear(rule):
                continue
            logger.info(f'检测到「{desc}」突发弹窗，点击红键关闭')
            for _ in range(self.GUILD_POPUP_MAX_CLICKS):
                self.appear_then_click(rule, action=DailyAltAccAssets.C_BACK_RED, interval=1)
                # 取下一帧确认弹窗是否已关闭
                self.device.screenshot()
                if not self.appear(rule):
                    break
            else:
                logger.warning(f'「{desc}」弹窗点击红键 {self.GUILD_POPUP_MAX_CLICKS} 次仍未关闭，跳过并继续任务流程')


class _GuildPopupScope:
    """guild_popup_scope 的上下文管理器实现：置位/复位宿主的检测标志。

    复位写回进入前的值而不是硬编码 False，兼容「常开任务流程内再进
    子作用域」的嵌套场景。
    """

    def __init__(self, host):
        self.host = host
        self.prev = None

    def __enter__(self):
        self.prev = self.host._guild_popup_active
        self.host._guild_popup_active = True
        return self

    def __exit__(self, exc_type, exc, tb):
        self.host._guild_popup_active = self.prev
        return False


class DailyAltAccBase(GuildPopupMixin, GameUi, DailyAltAccAssets):
    """所有子任务的公共基类，提供通用方法和资源"""
    config: Config
    device: Device
    msg: list

    def __init__(self, config: Config, device: Device) -> None:
        super().__init__(config, device)
        self.msg = []

    def get_config(self):
        return self.config.daily_alt_acc

    def _is_exp_extract_dialog(self) -> bool:
        """判断当前弹窗是否为「结界经验提取」弹窗（正文含「提取」）。

        这里刻意不用 ocr_appear：RuleOcr 的 FULL 模式走 base_ocr.filter()，
        整串关键词匹配失败时会降级成「keyword 里任一字符出现即算命中」，
        而「取消」本身就含「取」字，必然误判。改为直接取 OCR 原始文本自行判断
        （与 Restart/login.py 处理区服名的做法一致），语义完全可控。
        """
        try:
            text = self.O_EXP_DAILOG.detect_text(self.device.image)
        except Exception as e:
            # OCR 异常不能影响主流程：识别不出就按原有逻辑点【取消】
            logger.warning(f'经验提取弹窗 OCR 失败，按普通弹窗处理: {e}')
            return False
        if self.O_EXP_DAILOG.keyword in text:
            logger.info(f'检测到结界经验提取弹窗，点击确认提取: [{text}]')
            return True
        return False

    def get_award_daliy(self) -> bool:
        """
        处理日常弹窗奖励（悬赏、协战、签到等）
        """
        self.screenshot()
        if self.appear_then_click(self.I_M_AWARD, action=self.C_MS_REFRESH_ACTION, interval=1):
            return True
        elif self.appear(self.I_M_PICTURE_REFUSE):
            # 【取消】按钮同时出现在「获得插画」与「结界经验提取」两种弹窗上，
            # 且 I_M_PICTURE_REFUSE 的模板就是「取消」二字、ROI 也完全重合，
            # 光靠图片匹配无法区分。因此命中后先用 OCR 读弹窗正文再决定点哪个按钮：
            # 是经验提取弹窗就点【确认】提取（否则点【取消】会让弹窗反复出现，
            # 与庭院事务的【一键完成】形成互点死循环并触发 GameTooManyClickError）。
            if self._is_exp_extract_dialog():
                # O_EXP_DAILOG 的 area 即【确认】按钮区域，click(RuleOcr) 取的是 area
                self.click(self.O_EXP_DAILOG, interval=1)
                return True
            self.appear_then_click(self.I_M_PICTURE_REFUSE, interval=1)
            return True
        elif self.appear_then_click(self.I_M_PICTURE, self.C_MS_REFRESH_ACTION, interval=1):
            return True
        elif self.appear_then_click(self.I_CORD_EXIT, interval=1):
            return True
        elif self.appear_rgb(self.I_CORD_BACK_RED):
            self.appear_then_click(self.I_CORD_BACK_RED, interval=1)
            return True
        elif self.appear_rgb(self.I_M_FRAME_BACK_RED):
            self.appear_then_click(self.I_M_FRAME_BACK_RED, interval=1)
            return True
        elif self.appear_rgb(self.I_T_BACK_RED_SIGN):
            self.appear_then_click(self.I_T_BACK_RED_SIGN, interval=1)
            return True
        elif self.appear_then_click(self.I_T_SIGN_FLAG, action=self.C_T_EXIT_SIGN, interval=1) or \
                self.appear_then_click(self.I_T_SIGN_FLAG2, action=self.C_T_EXIT_SIGN, interval=1):
            return True
        elif self.appear_then_click(PlotlineAssets.I_CLICK_CURSOR, action=self.C_MS_REFRESH_ACTION, interval=1):
            return True
        elif self.appear_then_click(PlotlineAssets.I_PAGE_CLICK_ANY, action=self.C_MS_REFRESH_ACTION, interval=1):
            return True
        else:
            return False


if __name__ == "__main__":
    c = Config('oas3')
    d = Device(c)
    self = DailyAltAccBase(c, d)
