"""账号下拉列表展开状态的判定。

先由 LOGO 确认账号选择界面，再由下部横条亮度或「添加新账号」确认列表展开。
短列表不会覆盖固定横条，因此需要添加入口补充判定。
"""

from tasks.Component.SwitchAccount.assets import SwitchAccountAssets

# 长账号列表展开时弹窗向下延伸覆盖的横条；短列表与收起时这里可能露出游戏背景
ACCOUNT_DROPDOWN_ROI: tuple = (377, 578, 525, 22)
# 横条整体亮度高于此值判为「弹窗覆盖中」，即列表展开
ACCOUNT_DROPDOWN_BRIGHTNESS: float = 160


def is_account_dropdown_opened(task) -> bool:
    """账号下拉列表是否展开。

    先用网易游戏 LOGO 确认停在账号选择界面：庭院、悬赏等其他页面也会出现偏亮的
    横条，只靠亮度会被误判，LOGO 把范围收窄到账号选择界面。长列表展开时弹窗盖住
    下部横条（列表是白的、滚到底露出「常用」标签是金的，两种都亮），实测 211~254。
    收起或账号较少时横条露出暗色背景，亮度不足则检查列表中的「添加新账号」。

    这里读像素均值而非模板匹配：该区域判别力最强处是纯色，而纯色模板去均值后
    恒为零，TM_CCOEFF_NORMED 的响应图会塌成一个常数——既让 appear 恒为真，
    又会在命中时把 roi_front 回写到搜索区左上角，使后续取色跑到别处。
    """
    if not task.appear(SwitchAccountAssets.I_SA_NETEASE_GAME_LOGO):
        return False
    x, y, w, h = ACCOUNT_DROPDOWN_ROI
    # 短列表的添加入口位置随账号数量上移；资源搜索区覆盖整个列表高度。
    return (float(task.device.image[y:y + h, x:x + w].mean()) > ACCOUNT_DROPDOWN_BRIGHTNESS
            or task.appear(SwitchAccountAssets.I_SA_ACCOUNT_DROP_DOWN_ADD_ACCOUNT))
