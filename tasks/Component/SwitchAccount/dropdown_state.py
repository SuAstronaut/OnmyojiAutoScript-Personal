"""账号下拉列表展开状态的判定。

该判据由两个「单独都不够、合起来才闭环」的检查组成：LOGO 负责回答
「在不在账号选择界面」，横条亮度负责回答「弹窗有没有延伸到这一行」。
"""

from tasks.Component.SwitchAccount.assets import SwitchAccountAssets

# 账号下拉列表展开时弹窗向下延伸覆盖的横条；收起时这里露出游戏背景
ACCOUNT_DROPDOWN_ROI: tuple = (377, 578, 525, 22)
# 横条整体亮度高于此值判为「弹窗覆盖中」，即列表展开
ACCOUNT_DROPDOWN_BRIGHTNESS: float = 160


def is_account_dropdown_opened(task) -> bool:
    """账号下拉列表是否展开。

    先用网易游戏 LOGO 确认停在账号选择界面：庭院、悬赏等其他页面也会出现偏亮的
    横条，只靠亮度会被误判，LOGO 把范围收窄到账号选择界面。再看弹窗下部横条的
    亮度——展开时弹窗盖住这一条（列表是白的、滚到底露出「常用」标签是金的，两种
    都亮），收起时弹窗停在更靠上的位置，这里露出暗色游戏背景。实测展开 211~254、
    收起 43，阈值 160 时展开侧余量 51、收起侧余量 116。

    这里读像素均值而非模板匹配：该区域判别力最强处是纯色，而纯色模板去均值后
    恒为零，TM_CCOEFF_NORMED 的响应图会塌成一个常数——既让 appear 恒为真，
    又会在命中时把 roi_front 回写到搜索区左上角，使后续取色跑到别处。
    """
    if not task.appear(SwitchAccountAssets.I_SA_NETEASE_GAME_LOGO):
        return False
    x, y, w, h = ACCOUNT_DROPDOWN_ROI
    return float(task.device.image[y:y + h, x:x + w].mean()) > ACCOUNT_DROPDOWN_BRIGHTNESS
