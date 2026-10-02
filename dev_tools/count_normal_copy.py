# This Python file uses the following encoding: utf-8
"""
批量 OCR 识别协战界面截图中「普通副本 x/15」的次数。

截图由 tasks/DailyAltAcc/alliedteam.py 保存到
screenshots/Battle_Screenshots_<年_月_日>/<角色名>.png

昵称取自卡片右上角带勾选标记的那张亲友卡，即当前设成协战式神的那位好友；
文件名用的是 OAS 配置里的角色名（如 角色A），与游戏里显示的昵称（如
玩家甲）对不上，所以要把昵称单独列出来才看得出是谁的号。

输出按昵称分组校验，分「已完成 / 未完成 / 异常」三段：

* 同一昵称下的所有截图算同一个角色，次数合计 >= GROUP_COPY_PASS(50) 即为已完成；
* 单张次数 >= SINGLE_COPY_PASS(13) 才算这张达标。合计达标但个别账号没打够时，
  把没打够的那几张列在该角色下面；合计不达标则把该角色全部截图都列出来，
  便于区分是缺账号还是次数不够；
* 昵称没识别出来的截图无法归组，统一放到最后单独列出。

每行格式为「昵称  次数/状态  总次数或图片名称」。

用法：
    # 不带参数：识别当天的截图目录
    ./toolkit/python.exe -m dev_tools.count_normal_copy

    # 带日期：识别该日期的截图目录
    ./toolkit/python.exe -m dev_tools.count_normal_copy 2026_07_20

    # 带路径：识别任意文件夹，可用于旧版保存在根目录下的日期文件夹
    ./toolkit/python.exe -m dev_tools.count_normal_copy D:\\some\\folder
"""
import argparse
import logging
import re
import sys
import unicodedata
from datetime import datetime
from pathlib import Path
from typing import NamedTuple

import cv2
import numpy as np

from module.atom.ocr import RuleOcr
from module.logger import logger
from module.ocr import models
from module.ocr.rapid_ocr import ensure_backend_loaded

# 项目根目录，本文件位于 dev_tools/ 下，向上一级即根目录
PROJECT_ROOT = Path(__file__).resolve().parent.parent

# 支持的图片后缀
IMAGE_SUFFIXES = {'.png', '.jpg', '.jpeg'}

# 截图保存目录与按日期命名的子目录前缀，与 tasks/DailyAltAcc/alliedteam.py 保持一致
SCREENSHOT_DIR = 'screenshots'
SCREENSHOT_PREFIX = 'Battle_Screenshots_'

# 形如 2026_08_02 的日期参数
DATE_PATTERN = re.compile(r'\d{4}_\d{2}_\d{2}')

# 校验线：单张截图的「普通副本」次数不低于此值才算这张达标
SINGLE_COPY_PASS = 13
# 同一昵称下所有截图的次数合计不低于此值，才算该角色已完成
GROUP_COPY_PASS = 50

# 输出排版：明细行整体缩进 INDENT，组行在昵称后补等宽空格，使两行的第二列对齐
INDENT = '  '
SEP = '  '
# 分节标题行右侧的横线个数。制表横线 U+2500 的 East Asian Width 是「Ambiguous」，
# 中文终端普遍按 2 列渲染，所以数量按 2 列/个折算——只影响观感，不影响列对齐
SECTION_DASHES = 17
# 标题补到该显示宽度，三段的分隔线才会一样长
SECTION_TITLE_WIDTH = 6


class Shot(NamedTuple):
    """单张截图的识别结果。"""
    filename: str
    count: int | None   # 「普通副本」次数，未识别时为 None（按 0 计入合计）
    text: str           # 展示用文本，如「13/15」或「未识别」

    @property
    def passed(self) -> bool:
        """单张是否达标：识别到次数且不低于 SINGLE_COPY_PASS。"""
        return self.count is not None and self.count >= SINGLE_COPY_PASS


def display_width(text: str) -> int:
    """
    计算字符串在终端里占用的列数。中文等东亚宽字符占 2 列，直接用 len() 补空格会错位。
    :param text: 待计算的文本
    :return: 显示宽度
    """
    return sum(2 if unicodedata.east_asian_width(ch) in ('W', 'F') else 1 for ch in text)


def pad(text: str, width: int) -> str:
    """
    按显示宽度右侧补空格。
    :param text: 待补齐的文本
    :param width: 目标显示宽度
    :return: 补齐后的文本；文本本身已超宽时原样返回
    """
    return text + ' ' * max(0, width - display_width(text))


def section(title: str) -> str:
    """
    生成分节标题行，右侧用横线补足便于扫读。
    :param title: 分节名，如「已完成」
    :return: 标题行文本
    """
    return f'─── {pad(title, SECTION_TITLE_WIDTH)} ' + '─' * SECTION_DASHES


def group_order(item: tuple[str, list[Shot], int]) -> tuple[int, str]:
    """
    分组排序键：总次数降序（余量大的角色排前面），同次数再按昵称升序，保证多次运行输出一致。
    :param item: (昵称, 截图列表, 总次数)
    :return: 排序键
    """
    return -item[2], item[0]


# 「每日协战次数」区块中「普通副本13/15」所在的固定 ROI（基于 1280x720 截图）
# DigitCounter 模式的后处理会剔除中文，只保留数字和斜杠，因此 ROI 可以把「普通副本」一起框进来
O_NORMAL_COPY = RuleOcr(
    roi=(765, 152, 195, 46),
    area=(765, 152, 195, 46),
    mode="DigitCounter",
    method="Default",
    keyword="",
    name="normal_copy",
)

# --------------------------------------------------------- 被勾选亲友卡的昵称
#
# 截图文件名是 OAS 的角色名（如 角色A），游戏里显示的是玩家昵称（如 玩家甲），
# 两者对不上。这里额外把「被勾选的那张亲友卡」上的昵称读出来，方便人工对账。
#
# 版面（基于 1280x720 截图，实测各账号各日期完全一致）：
#   * 勾选标记是贴在卡片右上角的青色菱形徽章，填充色实测 BGR≈(165,130,0)~(181,146,24)，
#     即 R 极低、B>G 的强青色。式神立绘里也有青色，但饱和度低得多（R 普遍 >100），
#     用「R 极低」这一个条件就能把立绘排除干净。
#   * 昵称写在卡片底部的黑底名条上，名条是深色背景 + 浅色文字，整条 OCR 即可，
#     且该行带只覆盖黑底，名条下方红色的服务器名（如 砂狐乐园）会被切掉、不会混进来。
#
# 只有一张卡被勾选（当前设置成协战式神的那位好友），所以不必判断「第几张」：
# 找出勾选徽章的横向位置，再取横向离它最近的那条昵称就是同一张卡。

# 搜索勾选徽章的区域（x, y, w, h）：好友/亲友式神列表整体，含第一行卡片及其
# 右上角徽章位置；再往下的卡片行会被列表底边裁掉，不必扩到全屏
CHECK_BADGE_SEARCH = (480, 340, 620, 260)
# 勾选徽章填充色的判定范围
CHECK_BADGE_MAX_RED = 60            # R 通道上限，立绘的青色 R 普遍 >100，靠这一条就能排除
CHECK_BADGE_GREEN_RANGE = (100, 180)
CHECK_BADGE_BLUE_RANGE = (140, 215)  # 并要求 B > G，把偏绿的颜色一并排除
CHECK_BADGE_MIN_AREA = 40           # 徽章被白色对勾切成两块，单块面积远大于此

# 被勾选卡片底部黑底名条所在的行带（基于 1280x720 截图）
O_NICKNAME_BAR = RuleOcr(
    roi=(480, 495, 620, 36),
    area=(480, 495, 620, 36),
    mode="Full",
    method="Default",
    keyword="",
    name="nickname_bar",
)


def find_check_badge(image: np.ndarray) -> tuple[int, int] | None:
    """
    定位「被勾选的那张亲友卡」右上角的勾选徽章。
    :param image: 截图
    :return: 徽章质心的全图坐标 (x, y)，没有勾选标记时返回 None
    """
    x, y, w, h = CHECK_BADGE_SEARCH
    sub = image[y:y + h, x:x + w]
    b = sub[:, :, 0].astype(int)
    g = sub[:, :, 1].astype(int)
    r = sub[:, :, 2].astype(int)
    mask = ((r < CHECK_BADGE_MAX_RED)
            & (g >= CHECK_BADGE_GREEN_RANGE[0]) & (g <= CHECK_BADGE_GREEN_RANGE[1])
            & (b >= CHECK_BADGE_BLUE_RANGE[0]) & (b <= CHECK_BADGE_BLUE_RANGE[1])
            & (b > g)).astype(np.uint8)

    count, _, stats, _ = cv2.connectedComponentsWithStats(mask, 8)
    # 白色对勾会把菱形徽章切成两块，两块一起取并集才能还原徽章整体
    hit = [i for i in range(1, count) if stats[i, cv2.CC_STAT_AREA] >= CHECK_BADGE_MIN_AREA]
    if not hit:
        return None
    x0 = min(stats[i, cv2.CC_STAT_LEFT] for i in hit)
    x1 = max(stats[i, cv2.CC_STAT_LEFT] + stats[i, cv2.CC_STAT_WIDTH] for i in hit)
    y0 = min(stats[i, cv2.CC_STAT_TOP] for i in hit)
    y1 = max(stats[i, cv2.CC_STAT_TOP] + stats[i, cv2.CC_STAT_HEIGHT] for i in hit)
    # 统计量都相对搜索区裁剪图，加回搜索区偏移才是全图坐标
    return (x0 + x1) // 2 + x, (y0 + y1) // 2 + y


def ocr_checked_nickname(image: np.ndarray) -> str:
    """
    读取被勾选亲友卡上的玩家昵称。
    :param image: 截图
    :return: 昵称；没有勾选标记或名条上没识别出文字时返回空串
    """
    badge = find_check_badge(image)
    if badge is None:
        return ''
    # detect_and_ocr 的 box 坐标相对名条 ROI 裁剪图，换算成同一坐标系再比
    badge_x = badge[0] - O_NICKNAME_BAR.roi[0]
    # 昵称在黑底名条上居中，勾选徽章却贴在卡片右上角，两者横向差约 40px；
    # 而卡片列间距约 220px，所以「横向中心离徽章最近」能唯一锁定被勾选的那张卡，
    # 不需要知道它是第几张，也不依赖卡片固定的横向像素位置。
    results = O_NICKNAME_BAR.detect_and_ocr(image)
    if not results:
        return ''
    nearest = min(results, key=lambda r: abs((r.box[0][0] + r.box[1][0]) / 2 - badge_x))
    return nearest.ocr_text


def resolve_folder(name: str | None) -> Path:
    """
    解析要识别的文件夹。
    :param name: 命令行传入的日期、文件夹名或路径，为 None 时使用当天日期
    :return: 文件夹的绝对路径
    """
    if name is None:
        name = datetime.now().strftime('%Y_%m_%d')

    # 参数不是 2026_08_02 这种日期时，按普通文件夹名或路径处理
    if not DATE_PATTERN.fullmatch(name):
        folder = Path(name)
        return folder if folder.is_absolute() else PROJECT_ROOT / folder

    # 日期优先解析成新的截图目录，仅当它不存在而根目录下的旧文件夹存在时才回退
    dated = PROJECT_ROOT / SCREENSHOT_DIR / f'{SCREENSHOT_PREFIX}{name}'
    legacy = PROJECT_ROOT / name
    if not dated.is_dir() and legacy.is_dir():
        return legacy
    return dated


def read_image(path: Path) -> np.ndarray | None:
    """
    读取图片。使用 imdecode 而非 imread，避免路径含中文时读取失败。
    :param path: 图片路径
    :return: 图片数组，读取失败返回 None
    """
    try:
        return cv2.imdecode(np.fromfile(str(path), dtype=np.uint8), cv2.IMREAD_COLOR)
    except OSError:
        return None


def main() -> int:
    # Windows 下 stdout 默认是 gbk，在 Git Bash 等 UTF-8 终端里中文会乱码，统一改成 UTF-8
    if sys.platform.startswith('win'):
        try:
            sys.stdout.reconfigure(encoding='utf-8', errors='replace')
        except (AttributeError, OSError):
            pass

    # 纯本地识别，不依赖 OCR RPC 服务，本工具可以在没开 server/脚本时直接跑。
    #
    # 两步的顺序不能颠倒，原因各不相同：
    #
    # 1. 先加载 onnxruntime。pyzmq 一旦先进入 sys.modules，onnxruntime 的原生
    #    DLL 就必然初始化失败（ImportError: DLL load failed，详见 rapid_ocr
    #    .ensure_backend_loaded 的说明）。工具的 import 链本身是干净的，这里
    #    只是把「ORT 先就位」变成显式保证。
    # 2. 再把模型入口钉死到本地版本。默认的 get_ocr_model 会按部署配置的
    #    UseOcrServer 去连 127.0.0.1:22268，服务没开时要连等 3 次
    #    CONNECT_RETRY(每次 10 秒心跳超时) 才降级，最后仍然回落到本地。
    #    直接从源头改，省掉这段空等，也顺带把 import zerorpc(pyzmq) 这条
    #    会污染 ORT 的路径整个绕开。
    #
    # 注意不要用 State.deploy_config.UseOcrServer = False 代替第 2 步：
    # DeployConfig.__setattr__ 对配置段内的大写键会直接 write() 落盘
    # （module/server/config.py:50），那样等于永久改了用户的 deploy.yaml。
    if not ensure_backend_loaded():
        print('onnxruntime 后端不可用，无法进行本地 OCR')
        return 1
    models.get_ocr_model = models.get_local_ocr_model

    parser = argparse.ArgumentParser(description='识别协战截图中「普通副本 x/15」的次数')
    parser.add_argument('folder', nargs='?', default=None,
                        help='日期（形如 2026_08_02，对应 screenshots/Battle_Screenshots_2026_08_02），'
                             '或任意文件夹名/路径；缺省为当天日期')
    args = parser.parse_args()

    folder = resolve_folder(args.folder)
    if not folder.is_dir():
        print(f'文件夹不存在：{folder}')
        return 1

    images = sorted(p for p in folder.iterdir()
                    if p.is_file() and p.suffix.lower() in IMAGE_SUFFIXES)
    if not images:
        print(f'文件夹中没有图片：{folder}')
        return 1

    # 压掉 RuleOcr 内部逐张打印的 logger.attr 日志，只保留本脚本的输出
    logger.setLevel(logging.ERROR)

    # 先全部识别完再分组：同一昵称下的截图对应同一个协战式神，
    # 逐角色核对「今天这个角色被协战够了没有」时不用在整份名单里来回找。
    groups: dict[str, list[Shot]] = {}
    # 昵称没识别出来的截图无法归组，单独攒起来放到最后
    anomalies: list[tuple[str, str, str]] = []
    for path in images:
        image = read_image(path)
        if image is None:
            anomalies.append(('读取失败', '读取失败', path.name))
            continue

        current, _, total = O_NORMAL_COPY.ocr(image)
        # total 为 0 说明没有匹配到 x/y 形式的文本，视为识别失败，按 0 计入合计
        count = current if total else None
        text = f'{current}/{total}' if total else '未识别'
        nickname = ocr_checked_nickname(image)
        # 昵称取不到就无法判断属于哪个角色，把这张连同次数一起当异常输出
        if not nickname:
            anomalies.append(('未勾选', text, path.name))
            continue
        # 昵称按字符串精确分组：简繁写法不同的两个昵称算两个人，不合并；
        # images 本身按文件名排过序，所以每组的截图顺序也是稳定的
        groups.setdefault(nickname, []).append(Shot(path.name, count, text))

    finished: list[tuple[str, list[Shot], int]] = []
    unfinished: list[tuple[str, list[Shot], int]] = []
    for nickname, shots in groups.items():
        # 未识别的截图按 0 计入合计：读不到就当成没打，宁可让人工回头翻截图
        total_count = sum(shot.count or 0 for shot in shots)
        (finished if total_count >= GROUP_COPY_PASS else unfinished).append(
            (nickname, shots, total_count))
    finished.sort(key=group_order)
    unfinished.sort(key=group_order)

    # 先算出两列的显示宽度再输出，三段之间才能列对齐
    nick_w = max((display_width(nickname)
                  for nickname, _, _ in finished + unfinished), default=0)
    nick_w = max(nick_w, max((display_width(label) for label, _, _ in anomalies), default=0))
    col_w = max((display_width(text) for text in
                 ['已完成', '未完成']
                 + [shot.text for _, shots, _ in finished + unfinished for shot in shots]
                 + [text for _, text, _ in anomalies]), default=0)

    def row(nickname: str, second: str, tail: str, indent: bool = False) -> str:
        """拼一行输出。明细行整体缩进 INDENT，因此未缩进的行要在昵称后补等宽空格才对齐。"""
        if indent:
            return f'{INDENT}{pad(nickname, nick_w)}{SEP}{pad(second, col_w)}{SEP}{tail}'
        return (f'{pad(nickname, nick_w + display_width(INDENT))}'
                f'{SEP}{pad(second, col_w)}{SEP}{tail}')

    print(f'{folder}  共 {len(images)} 张图片')
    print(f'已完成 {len(finished)} 组 / 未完成 {len(unfinished)} 组 / 异常 {len(anomalies)} 张')

    print(section('已完成'))
    if not finished:
        print('（无）')
    for nickname, shots, total_count in finished:
        print(row(nickname, '已完成', str(total_count)))
        # 合计达标但个别账号没打够时，把这几张列在该角色下面，便于回头补次数
        for shot in shots:
            if not shot.passed:
                print(row(nickname, shot.text, shot.filename, indent=True))

    print(section('未完成'))
    if not unfinished:
        print('（无）')
    for nickname, shots, total_count in unfinished:
        print(row(nickname, '未完成', str(total_count)))
        # 没达标就列出该角色全部截图，便于区分是缺账号还是次数不够
        for shot in shots:
            print(row(nickname, shot.text, shot.filename, indent=True))

    print(section('异常'))
    if not anomalies:
        print('（无）')
    for label, text, filename in anomalies:
        print(row(label, text, filename))

    return 0


if __name__ == '__main__':
    sys.exit(main())
