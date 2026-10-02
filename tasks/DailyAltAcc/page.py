"""任务导航遇到同心集结时退出旧队伍，正在进行同心战斗时保留集结。"""

import re

from module.atom.ocr import RuleOcr
from module.exception import GamePageUnknownError
from module.logger import logger
from tasks.DailyAltAcc.assets import DailyAltAccAssets as A
from tasks.GameUi.assets import GameUiAssets as G
from tasks.GameUi.page import Page, page_main


# 顶部横幅文字独立读取，不以房间中也会出现的退出 X 单独判定集结状态。
O_ALLIEDTEAM_ASSEMBLY_TEXT = RuleOcr(
    roi=(400, 12, 390, 78), area=(400, 12, 390, 78),
    mode='Full', method='Default', keyword='', name='alliedteam_assembly_text',
)


def _exit_enabled(task) -> bool:
    """所有任务都能导航退出集结，只有当前实例的同心组队战斗过程暂时禁用。"""
    return not getattr(task, '_alliedteam_battle_active', False)


def _read_text(ocr, image) -> str:
    """严格读取正文，OCR 服务异常交给外层恢复，不能凭通用按钮猜确认内容。"""
    try:
        return re.sub(r'\s+', '', ocr.detect_text(image))
    except Exception as exc:
        logger.exception('同心退队页面 OCR 失败')
        raise GamePageUnknownError('无法识别同心退队页面') from exc


def _exit_confirm_appear(task) -> bool:
    """通用确定按钮和退出同心集结的正文必须同时命中。"""
    if not task.appear(A.I_EXIT3):
        return False
    text = _read_text(A.O_EXP_DAILOG, task.device.image)
    if '同心队' in text and '是否退出集结' in text:
        return True
    # 已知正在退出同心或横幅仍在时不能走通用确定兜底；其他任务的普通弹窗照旧处理。
    if (getattr(task, 'ui_current', None) in (page_alliedteam_assembly, page_alliedteam_exit_confirm)
            or _assembly_banner_appear(task)):
        raise GamePageUnknownError('同心集结期间遇到未识别的确认框，停止导航以免误确认')
    return False


def _assembly_banner_appear(task) -> bool:
    """用庭院锚点、横幅退出 X 和完整文字确认集结，允许检查弹窗后方的横幅。"""
    if not task.appear(A.I_FORM_OVER):
        return False
    if not (task.appear(G.I_CHECK_MAIN) or task.appear(G.I_MAIN_BUFF)):
        return False
    text = _read_text(O_ALLIEDTEAM_ASSEMBLY_TEXT, task.device.image)
    return '同心队集结状态中' in text


def _assembly_appear(task) -> bool:
    """只认庭院上的集结横幅；任何确认框盖住横幅时都不点击底层退出 X。"""
    if task.appear(A.I_EXIT3):
        return False
    return _assembly_banner_appear(task)


# 确认框必须先于背景横幅匹配；所有任务共用退出方向，同心战斗期间按实例禁用。
page_alliedteam_exit_confirm = Page(
    _exit_confirm_appear, enabled=_exit_enabled, overlay=True,
)
page_alliedteam_assembly = Page(
    _assembly_appear, enabled=_exit_enabled, overlay=True,
)
page_alliedteam_assembly.link(A.I_FORM_OVER, page_alliedteam_exit_confirm)
page_alliedteam_exit_confirm.link(A.I_EXIT3, page_main)
