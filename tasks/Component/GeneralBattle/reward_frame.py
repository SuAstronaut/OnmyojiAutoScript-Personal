# -*- coding: utf-8 -*-
"""战斗结算奖励框检测 + 安全落点计算。

把「识别到奖励就点几个固定区域」改成「全屏候选，挖掉常驻禁点区域与
检测出的奖励行（含行间间隙），剩余区域按面积与人类行为偏好加权取落点」。

检测原理（由 dev_tools/reward_frame_demo.py 在 17 张真实截图 93 个框上验证，
召回 100% / 精确 100% / 禁点行 17/17，真值最低分 0.541、空槽最高分 0.282）：

  1. 奖励格是刚性网格：格 108x106，同行间距 135，行上边框 y ∈ {172, 307, 442}；
     同一张图所有行共享同一 x 相位（GT 实测 7 张多行样本行行一致）。
  2. 用「环形蒙版」把格子内部挖空，只让边框参与匹配 —— 内部图标千变万化，
     只有边框环稳定，所以 6 个模板就能覆盖任意奖励内容。
  3. 环几乎不外扩（RING_OUT=1）：格间只有 27px 空隙，外扩会吃进邻格光晕，
     实测外扩 8px 时真值最低分反而低于空槽最高分，无阈值可分。
  4. 颜色空间用 HSV：边框线饱和度稳定（S=181~227），亮度极不稳（V=75~255）。

性能：全分辨率带蒙版滑窗会关闭 OpenCV 的 FFT 快路径，6 模板 x 3 行实测 2.7s，
在结算循环里不可用。故改为两级级联：
  粗级 —— 缩小 COARSE_SCALE 倍后滑窗，只用来提名候选 x（约 48ms）；
  细级 —— 在每个候选 x 附近 ±REFINE_RADIUS 像素内用「原尺寸、原蒙版、原阈值」复核。
细级特征与上述已验证配置逐字节相同，所以级联只可能在「粗级漏提名」处退化，
而粗级取的是每行 top-K 分离峰（K 已大于一行最多能放的格子数），漏提名风险极低。
"""
import bisect
import math
import random
from typing import NamedTuple

import cv2
import numpy as np

from pathlib import Path

from module.atom.click import RuleClick
from module.logger import logger

# ---------------- 网格几何（720p 授权坐标，实测） ----------------
CELL_W, CELL_H = 108, 106
PITCH = 135
ROW_TOPS = (172, 307, 442)
# 奖励网格整体占据的 y 范围，用于判断候选落点是否天然在网格外
GRID_TOP = ROW_TOPS[0]
GRID_BOTTOM = ROW_TOPS[-1] + CELL_H

# ---------------- 环形蒙版与匹配参数（扫描最优，勿轻改） ----------------
RING_OUT, RING_IN = 1, 8
MATCH_METHOD = cv2.TM_CCOEFF_NORMED
# 可用窗口 (0.282, 0.541]，取 0.45：距真值下限 0.091、距空槽上限 0.168，双向留余量
THRESHOLD = 0.45
# 同行两个检出至少相距这么远才算不同格子（格宽 108，取略小于一格）
MIN_SEP = 90
# 相位锁定容差：同一张图所有格子的 x 应同余于 PITCH
PHASE_TOL = 4

# ---------------- 级联参数 ----------------
COARSE_SCALE = 3
# 一行最多放 (1280-108)//135+1 = 9 个格子，取 12 留足余量
COARSE_TOP_K = 12
# 粗级量化误差最大约 COARSE_SCALE 像素，取两倍作为细级搜索半径
REFINE_RADIUS = COARSE_SCALE * 2
# 粗级门槛只负责「不漏」，精确性交给细级：
# 实测粗级真值最低 0.492、空槽最高 0.374，取 0.36 —— 比真值下限低 0.13（召回余量足），
# 又略低于空槽上限（宁可多放几个候选进细级，细级空槽最高仅 0.282，会被 0.45 挡掉）。
COARSE_GATE = 0.36
# 细级只复核粗级排名前 N 的模板。实测 N=2 的真值最低分（0.541）与全 6 模板完全相同，
# 而调用次数降到 1/3 —— 带蒙版的 matchTemplate 单次固定开销很大，调用数是主要成本。
REFINE_TOP_TPL = 2

TPL_NAMES = ('purple', 'orange', 'white_grey', 'blue', 'orange_glow', 'white_grey2')
TPL_DIR = Path(__file__).parent / 'reward_frame_tpl'

# 授权分辨率：检测几何全部按此标定，非此尺寸的截图先归一化再检测
BASE_W, BASE_H = 1280, 720

# ---------------- 常驻禁止点击区域（按任务类型预设） ----------------
# 格式 (名称, x, y, w, h)，720p 授权坐标。这里放「与奖励框无关但永远不该点」的地方。
# 检测出的奖励行会和这里的区域合并后一起从候选落点中挖掉。
# 任务通过覆盖 GeneralBattle.reward_forbidden() 选择自己的预设。

# 御魂本 / 活动本 / 其他本的默认预设：顶部整条统计带 + 左下角
FORBIDDEN_DEFAULT = (
    ('top_bar', 0, 0, 1280, 171),
    ('bottom_left', 0, 515, 112, 205),
)

# 结界突破 / 寮突破 / 探索的预设：顶左条 + 顶右条 + 左下角
FORBIDDEN_KEKKAI = (
    ('top_left', 0, 0, 130, 171),
    ('top_right', 842, 0, 438, 171),
    ('bottom_left', 0, 515, 112, 205),
)

# 活动爬塔（ActivityShikigami）的预设：默认两块 + 本期活动的奖励卷轴面板
# 2026-09-09 这期活动结算的居中卷轴面板（用户实测框定）：面板本体是物品与
# 按钮区，结算点击只能落在面板四周与下方。活动轮换后面板位置变化时需同步更新。
FORBIDDEN_ACTIVITY = (
    ('top_bar', 0, 0, 1280, 171),
    ('bottom_left', 0, 515, 112, 205),
    ('act_panel', 272, 118, 740, 438),
)

# ---------------- 组队战斗胜利画面的队友战绩框（回避区，不是禁区） ----------------
# 组队时胜利画面会多出队友的战绩框（单人战斗没有），点上去会展开队友详情。
# 人数由玩法决定：同心协力固定三人，契灵/御魂/师徒等组队副本是两人。
#
# **按「回避区」处理而不是并入常驻禁区**：禁区会进 subtract_rects 参与几何切分，
# 战绩框横跨热区中部，挖掉后安全块被切成上下两截、热区锚点被带偏。回避区不改变
# 安全区域几何（热区锚点、分块、密度场都不动），只在取到落点后判定是否落在框内，
# 落在框内就重新取一个点（拒绝采样）。密度场形状完整保留，落点只是从战绩框里
# 挪开，仍分布在原来的热区范围内。
#
# 取舍的前提是「框只占热区的一小块」：三人框占热区概率质量的 6.4%、
# 两人框占 0.1%，拒绝采样的期望重试分别是 0.07 / 0.00 次，代价可忽略。
# （若框几乎铺满热区，拒绝采样会失效——那才需要回到禁区方案重新分块。）
AVOID_WIN_TEAM2 = (
    ('win_ally', 562, 394, 174, 202),
)
AVOID_WIN_TEAM3 = (
    ('win_ally', 449, 398, 410, 201),
)
# 拒绝采样的最大重试次数：超过就用最后一次的落点（兜底避免极端配置下死循环）
SETTLEMENT_AVOID_TRIES = 12

# ---------------- 候选落点区域 ----------------
# 全屏候选：危险位置全部由「常驻禁点预设 + 检测出的奖励行」负责挖掉，
# 剩下的任意空白都是合法落点，分布只由区域几何决定。
CLICK_CANDIDATES = (
    ('full_screen', 0, 0, 1280, 720),
)

# 裁剪后过小的碎片不作为落点：太小容易压边框，也不像人手点击
MIN_SAFE_W, MIN_SAFE_H = 24, 18

# ---------------- 人类落点密度场（以热区中心为峰的各向异性正态） ----------------
# 真人（右手持鼠）的点击密度是以热区中心为峰值的单峰钟形（split-normal）：
# 左/上两侧 σ 小（收手方向衰减快），右/下两侧 σ 大（自然延伸方向衰减慢），
# 峰值处最密，向左上收得急、向右下拖得缓 —— 没有平台，处处按正态衰减。
#
# 密度场同时作用于两级：选块权重（面积×均值密度=场的积分，weighted_choice）
# 与块内落点（FieldRuleClick 两轴加权抽样∝场）—— 两级相乘使整体落点分布
# 严格等于密度场本身。
#
# x 轴用固定像素：屏幕宽度不随奖励排数变化。
# y 轴按百分比分配：奖励排数越多，禁区下方的可用高度越小，峰值位置与两侧
# σ 按可用高度同比例压缩，分布始终贴合剩余空间而不是被屏幕底截断。
# y 锚点 = 奖励禁区底边（无奖励行时用 HOT_Y_BASE 基准），
# 可用高度 = min(HOT_H, 锚点到屏幕底) —— 热区是固定高度的带，禁区很深时才被压缩。
#
# 以下参数由真人实采数据反推（1053 次点击 / 33.8 分钟，见 log/click_monitor）：
# 热区内真人 x 中位 983（p10=900 p90=1104），y 中位 509（p10=466 p90=569），
# x IQR 109 / y IQR 58。原 σ_x 150/200 落点铺得比真人开得多（x IQR 约 1.9 倍）；
# y 的峰值位置 38% 与原 0.35 基本吻合。
#
# σ 档位横扫实测（plot_sigma_grid.py，各档 10000 次采样、独立同种子，
# 指标 = |IQR比-1| 之和 + x/y 的 KS 距离，越小越贴合真人）：
#    95/130 ·.16/.26   xIQR×1.31  偏差 0.674
#    88/120 ·.16/.25   xIQR×1.20  偏差 0.506
#    80/110 ·.16/.24   xIQR×1.10  偏差 0.348   ← 当前取值
#    73/102 ·.16/.23   xIQR×1.06  偏差 0.266
#    69/98  ·.16/.225  xIQR×1.00  偏差 0.172（数值最优）
#    65/95  ·.16/.22   xIQR×0.94  偏差 0.276
# 偏差曲线在 69/98 触底后两侧回升：更散则 IQR 与 KS 同时变差，更紧则 IQR 偏小
# 开始主导。取 80/110 而非数值最优档：真人整体 IQR 是 27 个习惯位置的叠加，
# 脚本只有单层分布，横向留一点余量避免落点收得过紧。
HOT_X = (706, 534)                  # 热区 x 范围 (x, w)，中心 x=973（真人中位 983）
SIGMA_LEFT, SIGMA_RIGHT = 80, 110   # 峰值左/右两侧衰减 σ（px）：左快右慢
HOT_Y_BASE = 427                    # 无奖励行时（胜利画面）的 y 基准锚点
HOT_H = 217                         # 热区高度：锚点向下 217px 为一带，超出屏幕底才压缩
HOT_Y_PEAK_RATIO = 0.38             # 峰值中心位于可用高度的 38% 处（真人 y 中位 509）
HOT_Y_UP_RATIO = 0.16               # 上侧衰减 σ = 可用高度的 16%（0.16×217≈35）
HOT_Y_DOWN_RATIO = 0.24             # 下侧衰减 σ = 可用高度的 24%（0.24×217≈52）


class HotShape(NamedTuple):
    """热区形状参数组：任务级覆盖用（GeneralBattle.reward_hot 返回），缺省取通用校准值。

    只收 4 个可覆盖的形状参数（x 范围 / 两侧 σ / y 峰值比例）；y 锚点
    （hot_y0）由奖励禁区动态决定，HOT_H 与上下 σ 比例保持全局一致。
    通用校准值由真人实采反推，任务覆盖属于主观调整（如活动爬塔
    2026-09-09 的右收下移），不影响其他任务。
    """
    hot_x: tuple = HOT_X
    sigma_left: float = SIGMA_LEFT
    sigma_right: float = SIGMA_RIGHT
    peak_ratio: float = HOT_Y_PEAK_RATIO


# 通用默认形状（四个字段 = 上方通用常量的取值），hot=None 时取它
HOT_DEFAULT = HotShape()
# 块边渐缩：安全块边界上密度强制归零，向内按 smoothstep 恢复全值，
# 渐缩宽度按块尺寸百分比取值。没有它，块边处密度是场在边界的残值，
# 落点会沿块边聚成直线、几条直线拼出块的矩形轮廓 —— 明显的机器特征。
EDGE_TAPER_RATIO = 0.15             # 渐缩宽度 = 块宽/高的 15%
# 矩形内密度均值的采样网格步长（px），越小积分越准、计算越多
FIELD_GRID_STEP = 64
# 块内落点采样的分片步长（px）：密度场按此粒度切片后做两轴加权抽样
FIELD_SAMPLE_STEP = 8

# ---------------- 结算连点（单击/多击）人类行为参数 ----------------
# 真人结算时经常对同一目标快速连点（跳过结算动画），而其余场景多是单击。
# 连点只用于战斗结束（胜利画面）与领取奖励两个场景（GeneralBattle 的
# settlement_click / settlement_gesture），不影响其他点击。
#
# 以下三项由真人实采数据校准（1053 次点击 / 451 个簇 / 33.8 分钟，
# 见 log/click_monitor 的采集与分析脚本）：
# 簇长直方图 1点258 2点18 3点35 4点90 5点33 6点7 8点6 9点2 10点1 11点1。
# 原 (60,35,5) 的单击占比 60% 与真人 57% 吻合，但多点簇最长只到 3 点。
#
# **簇长在 4 点封顶**：首击一旦点掉奖励页，后续追加击就落在新出现的界面上，
# 而安全区域是按奖励页算的——在新界面上那个坐标可能正好是「再来一局」
# 「进入下一关」之类的按钮。真人误触了会自己退回来，脚本不会，且长簇误触的
# 代价随任务链路放大。封顶把最长暴露窗口从 2.20s 压到 0.66s。
#
# **多点簇内部重新配重**（刻意偏离真人分布）：真人多点簇的峰值在 4 点
# （143 个多点簇里 90 个是 4 点），但 4 点簇的时长也最长（0.66s，是 2 点的 3 倍），
# 误触窗口与簇长成正比。故把权重从 4 点挪向 2/3 点：4 点 22.4%→9.5%，
# 2 点 4.5%→11.2%，3 点 8.7%→15.0%。单击占比与连点触发率保持不变
# （64.3% / 35.7%，总权重仍是 401），只是连点发生时更短——期望每次手势
# 点击数 1.89→1.70，平均追加时长 165ms→129ms。
# 保留「3 点多于 2 点」的相对关系，这一点与真人一致。
MULTI_CLICK_SIZES = (1, 2, 3, 4)          # 一次手势的总点击数取值（4 点封顶，见上）
MULTI_CLICK_WEIGHTS = (258, 45, 60, 38)   # 单击权重取真人值，多点簇内部按上述配重
MULTI_CLICK_GAP_S = (0.09, 0.17)     # 相邻两击的**目标间隔**（秒）：2026-09-06 用户
                                     # 实测连击 80~110ms 与真人簇内实测中位 181ms 的
                                     # 折中（用户指定）。注意这是目标值不是 sleep 值
                                     # ——device.click 自身要花约 165ms（按下/移动/
                                     # 抬起 + 按压时长 + 轨迹），直接 sleep 会叠加
                                     # 在它上面。实测实例A/B/C 的相邻击间隔中位
                                     # 334/356/381ms，正是设定值的两倍。故
                                     # _settlement_extra_clicks 改为节拍补偿：只补足
                                     # 距上一击的差额。
# 整次手势里追加击占用的总时长上限（秒），**按 wall clock 计**。与簇长封顶各管
# 一头：簇长管「点几下」，这里管「拖多久」。不能用「累加自己 sleep 了多久」——
# click 本身的耗时不进 sleep 的账，那样 4 点手势预算内 0.66s、实测却是 1.28s。
# 取 0.7s：4 点在节拍补偿下最长 3×0.22=0.66s 正好在预算内，预算是防止日后调大
# 簇长/间隔时误触窗口悄悄变长的硬保险。
MULTI_CLICK_MAX_S = 0.7
# 追加击是否偏移的概率：真人簇内相邻位移 86% 完全为 0（落在同一像素），
# 且簇越长越不动（8 点以上簇 100% 零位移）。原实现每次必抖 0~3px，恰好落在
# 真人最罕见的档位（0.5~3px 仅占 1.5%，而脚本实测 68% 都在这一档）——
# 「从不完全重复」本身就是最容易被识别的机器特征，故默认复用坐标。
MULTI_CLICK_JITTER_PROB = 0.14
MULTI_CLICK_JITTER_RANGE = (1.0, 12.0)  # 发生偏移时的距离范围（px）：真人非零
                                        # 位移中位 10.9px；方向随机，偏移后钳回安全矩形

# ---------------- 结算过程内的连点衰减 ----------------
# 误触根因（2026-09-06 复盘，共享 CD 之后仍复现）：截图间隔约 0.3s，首击把
# 结算页点掉后画面开始切换，而最近一帧仍是结算界面——判据继续命中，一次
# 手势**内部**的追加击继续执行，落在已切换完成的新画面上造成误触。结算
# 过程拖得越久（已发起的点击事件越多），页面越可能在下一次点击后被点掉，
# 追加击跨越画面切换点的风险越高。
#
# 故按**本结算过程已发起的点击事件数**衰减多击权重（multi_click_weights 查表）：
# - 单击档位永不衰减：页面必须总能被点掉，否则结算循环空转；
# - 档位越高出局越早——长连点的误触窗口与簇长成正比：4 连击第 3 次事件后
#   出局、3 连击第 4 次后出局，只剩 2 连击随触发率衰减到第 8 次归零；
# - 计数是事件级的：一次手势（无论内部连点几下）只计 1 次；
# - 第 1 次点击保持原始触发率（35.7%，单击权重恒 258），拟真度不损失——
#   衰减只发生在判据反复命中的顽固页面上，那里恰是误触风险累积的地方。
#
# 2026-09-06 用户指定簇长分布（非线性坍缩，线性衰减率表达不了，改为查表）：
#   第 1 次 35/50/15、第 2 次 45/45/10、第 3 次 85/15/0、第 4 次起仅 2 连击。
# 触发率序列保持真人实采校准：35.7% → 28.7% → 20.4% → 13.4% → 6.5%
# → 4.2% → 1.7% → 0%（第 8 次起永远单击）。
# 计数跨「胜利画面→奖励页」累计（两次画面切换有同样的误触窗口），场次边界
# 与 _settlement_last 同一 TTL 判定（见 settlement_gesture），无需显式重置。
# 索引越界取末档（第 8 次起等价于永远单击）。
MULTI_CLICK_WEIGHTS_BY_EVENT = (
    (258, 50, 72, 21),   # 第 1 次点击事件：触发 35.7%，簇长 35/50/15
    (258, 47, 47, 10),   # 第 2 次：触发 28.7%，簇长 45/45/10
    (258, 56, 10, 0),    # 第 3 次：触发 20.4%，簇长 85/15/0
    (258, 40, 0, 0),     # 第 4 次：触发 13.4%，仅 2 连击
    (258, 18, 0, 0),     # 第 5 次：触发 6.5%
    (258, 11, 0, 0),     # 第 6 次：触发 4.2%
    (258, 5, 0, 0),      # 第 7 次：触发 1.7%
    (258, 0, 0, 0),      # 第 8 次起：永远单击
)

# ---------------- 结算点击的共享节流 ----------------
# 结算阶段的各个判据（I_WIN / I_WIN_2 / I_REWARD / I_REWARD_GOLD /
# I_REWARD_STATISTICS / 各种皮肤碎片与御魂模板 / 奖励框兜底 / 贪吃鬼…）指向的
# 是**同一个意图**：「把结算页面点掉」。但它们各有一个以模板名为 key 的
# interval timer，彼此不互斥——FallenSun 的奖励循环挂了 9 个模板，理论上
# 1.5s 内能点 9 次。
#
# 实测事故（2026-09-06 08:44 日志）：
#   32.013 Click @GB_REWARD        ← 各自的 interval 都刚满
#   32.753 Click @GB_REWARD_GOLD   ← 仅 +0.74s，另一个 timer 没被前一次点击阻塞
#   33.503 Click @REWARD_GRID      ← 又 +0.75s，第三个 timer
#   34.609 break reward loop       ← 奖励页其实早已关闭
#   45:34  WARNING Wait too long   ← 多余点击落到准备页面上，页面识别错乱卡死 60s
#
# 故所有结算点击共享一个 CD：任一结算点击发生后，其余判据在 CD 内一律不点，
# 由 GeneralBattle._settlement_cd_ready 在 settlement_click /
# settlement_click_grid / settlement_gesture 三个入口统一把关，新增判据自动生效。
#
# 取值 1.0s（2026-09-06 用户指定，原 1.5）：仍完整覆盖事故里 0.74/0.75s 的
# 并发间隔，同时顽固结算页面每次点击的节奏加快 0.5s。CD 只管「多判据并发」
# 这一个意图级误触，单击节奏仍由各判据自己的 interval 参数决定。
SETTLEMENT_CLICK_CD_S = 1.0

# ---------------- 同一场战斗内的落点复用 ----------------
# 真人按战斗场次（>10s 间隔切分）三层切开后的实测（analyze_battle_structure.py）：
#   连点内（≤300ms）      86.0% 完全同坐标  —— 由 MULTI_CLICK_JITTER_PROB 负责
#   场次内事件间（中位 837ms） 31.4% 完全同坐标、≤30px 占 43.8%、中位 38.6px
#   跨场次（>10s）         0% 同坐标，距离中位 97.1px，与「热区内自由取点」的
#                         期望距离 96.2px 之比 1.01 —— 等同于重新取点
# 故「奖励页的落点参考胜利画面那一次」只在场次内成立，跨场次必须自由取点。
# 用时限判定场次边界：上次落点超过 TTL 即失效，不需要在战斗流程里显式重置，
# 对所有调用方（FallenSun / Orochi / MasterDisciple 等自带结算循环）一致生效。
#
# 2026-09-06 应用户要求上调复用强度（刻意高于真人 31.4%/12.4%）：设计目标
# 完全同坐标 40% + 3~30px 微调 20%（合计 ≤30px 约 62%）。自由取点也会偶入
# 30px 内（实测约 2.2%），所以复用本身只设计到 57.8%：0.58×0.67 = 0.389
# 同坐标 + 微调落进 ≤3px 的约 1.3%（uniform(1,30) 中 d≤3 占 2/29）≈ 实测
# 口径 40%；微调 3~30px 0.58×0.33×0.93 ≈ 18% + 自由取点 2% ≈ 20%。
SETTLEMENT_REUSE_PROB = 0.58     # 参考上次落点的概率（原 0.44 对齐真人 ≤30px 43.8%）
SETTLEMENT_REUSE_EXACT = 0.67    # 参考时用完全相同坐标的比例（原 0.72 对齐真人 31.4%）
SETTLEMENT_REUSE_RADIUS = 30     # 非完全复用时的偏移半径上限（px）
SETTLEMENT_REUSE_TTL_S = 10      # 上次落点的有效期（秒），从**末击**发起时刻起算
                                 # （settlement_gesture 在整次手势结束后写入，连点
                                 # 期间不消耗 TTL）：超过即认为换了一场战斗

# 禁点行在格子上下各外扩这么多像素。奖励框有向外的光晕（正因如此环形蒙版才只能外扩 1px），
# 紧贴边框外侧仍可能点中奖励。外扩 6px 后，行间 27px 的缝隙只剩 15px，
# 低于 MIN_SAFE_H 会被自动丢弃，不会把落点放到两行奖励之间的窄缝里。
ROW_MARGIN = 6


def _build_ring_mask(th, tw):
    """环形蒙版：外扩 RING_OUT、内缩 RING_IN，中间挖空只留边框环。"""
    mask = np.full((th, tw), 255, dtype=np.uint8)
    inset = RING_OUT + RING_IN
    mask[inset:th - inset, inset:tw - inset] = 0
    return mask


class RewardFrameDetector:
    """奖励框检测器。模板与蒙版只在首次使用时加载一次，之后常驻复用。"""

    def __init__(self, tpl_dir: Path = TPL_DIR):
        self.tpl_dir = Path(tpl_dir)
        self._fine = None
        self._coarse = None

    def _ensure_loaded(self):
        """懒加载：细级用原尺寸模板，粗级用缩小后的模板与重新二值化的蒙版。"""
        if self._fine is not None:
            return
        fine, coarse = [], []
        for name in TPL_NAMES:
            path = self.tpl_dir / ('%s.png' % name)
            raw = cv2.imread(str(path), cv2.IMREAD_COLOR)
            if raw is None:
                raise FileNotFoundError('奖励框模板缺失: %s' % path)
            tpl = cv2.cvtColor(raw, cv2.COLOR_BGR2HSV)
            th, tw = tpl.shape[:2]
            fine.append((name, tpl, _build_ring_mask(th, tw)))

            small = cv2.resize(tpl, None, fx=1 / COARSE_SCALE, fy=1 / COARSE_SCALE,
                              interpolation=cv2.INTER_AREA)
            sh, sw = small.shape[:2]
            # 蒙版缩小会插值出灰边，必须重新二值化，否则半透明像素会污染相关系数
            smask = cv2.resize(_build_ring_mask(th, tw), (sw, sh),
                               interpolation=cv2.INTER_AREA)
            smask = np.where(smask > 200, 255, 0).astype(np.uint8)
            coarse.append((name, small, smask))
        self._fine, self._coarse = fine, coarse

    # ---------------- 级联两级 ----------------

    def _coarse_peaks(self, small, row_top):
        """粗级：缩小图上滑窗，提名候选位置。

        :return: [(粗尺度 x, 该位置分数最高的 REFINE_TOP_TPL 个模板下标)...]
        """
        th = self._coarse[0][1].shape[0]
        top = int(round((row_top - RING_OUT) / COARSE_SCALE))
        if top < 0 or top + th > small.shape[0]:
            return []
        band = small[top:top + th, :]
        # 保留每个模板各自的分数线，用来决定细级复核哪几个模板
        mat = []
        for _, tpl, mask in self._coarse:
            line = cv2.matchTemplate(band, tpl, MATCH_METHOD, mask=mask)
            mat.append(np.nan_to_num(line, nan=-1.0, posinf=-1.0, neginf=-1.0)[0])
        mat = np.stack(mat)
        best = mat.max(axis=0)

        sep = max(1, MIN_SEP // COARSE_SCALE)
        picked = []
        for idx in np.argsort(-best):
            if len(picked) >= COARSE_TOP_K:
                break
            if best[idx] < COARSE_GATE:
                break
            if any(abs(int(idx) - p[0]) < sep for p in picked):
                continue
            order = np.argsort(-mat[:, idx])[:REFINE_TOP_TPL]
            picked.append((int(idx), [int(i) for i in order]))
        return picked

    def _refine(self, converted, row_top, candidates):
        """细级：在每个粗候选附近用原尺寸/原蒙版复核，返回超阈值的峰。"""
        th = CELL_H + 2 * RING_OUT
        tw = CELL_W + 2 * RING_OUT
        top = row_top - RING_OUT
        if top < 0 or top + th > converted.shape[0]:
            return []
        band = converted[top:top + th, :]
        width = band.shape[1]

        found = []
        for cx, tpl_idx in candidates:
            # 粗候选还原到原分辨率，向左右各留 REFINE_RADIUS 的搜索余量
            center = cx * COARSE_SCALE
            x0 = max(0, center - REFINE_RADIUS)
            x1 = min(width, center + tw + REFINE_RADIUS)
            if x1 - x0 < tw:
                continue
            patch = band[:, x0:x1]
            best_score, best_x = -1.0, None
            for i in tpl_idx:
                _, tpl, mask = self._fine[i]
                line = cv2.matchTemplate(patch, tpl, MATCH_METHOD, mask=mask)
                line = np.nan_to_num(line, nan=-1.0, posinf=-1.0, neginf=-1.0)[0]
                j = int(np.argmax(line))
                if float(line[j]) > best_score:
                    best_score, best_x = float(line[j]), x0 + j
            if best_score >= THRESHOLD:
                found.append({'x': best_x + RING_OUT, 'y': row_top, 'score': best_score})

        # 粗候选彼此已分离，但细级微调后可能靠拢，这里按分数高优先再去重一次
        found.sort(key=lambda p: -p['score'])
        kept = []
        for peak in found:
            if any(abs(peak['x'] - k['x']) < MIN_SEP for k in kept):
                continue
            kept.append(peak)
        return kept

    @staticmethod
    def _phase_lock(peaks):
        """跨行相位锁定：整张图所有奖励格的 x 必须同余于 PITCH。

        以「支持该相位的峰的总分」而非个数为准，避免多个低分误报凑成一个相位
        压过少量高分真框。
        """
        if len(peaks) <= 1:
            return peaks
        best_keep, best_weight = peaks, -1.0
        for anchor in peaks:
            keep = []
            for peak in peaks:
                diff = (peak['x'] - anchor['x']) % PITCH
                if min(diff, PITCH - diff) <= PHASE_TOL:
                    keep.append(peak)
            weight = sum(p['score'] for p in keep)
            if weight > best_weight:
                best_weight, best_keep = weight, keep
        return best_keep

    # ---------------- 对外接口 ----------------

    def detect(self, image) -> list:
        """检测奖励框，返回禁点行列表。

        :param image: BGR 截图（device.image）
        :return: [{'y0','y1','boxes':[{'x','y','w','h','score'}...]}...]，按行 y 升序
        """
        if image is None:
            return []
        # 几何按 720p 标定；其他分辨率先归一化，720p 走原路不做任何改动
        h, w = image.shape[:2]
        if (w, h) != (BASE_W, BASE_H):
            image = cv2.resize(image, (BASE_W, BASE_H), interpolation=cv2.INTER_LINEAR)

        self._ensure_loaded()
        converted = cv2.cvtColor(image, cv2.COLOR_BGR2HSV)
        small = cv2.resize(converted, None, fx=1 / COARSE_SCALE, fy=1 / COARSE_SCALE,
                           interpolation=cv2.INTER_AREA)

        peaks = []
        for row_top in ROW_TOPS:
            candidates = self._coarse_peaks(small, row_top)
            peaks.extend(self._refine(converted, row_top, candidates))
        peaks = self._phase_lock(peaks)

        rows = []
        for row_top in ROW_TOPS:
            in_row = sorted((p for p in peaks if p['y'] == row_top), key=lambda p: p['x'])
            if not in_row:
                continue
            rows.append({
                'y0': row_top,
                'y1': row_top + CELL_H,
                'boxes': [{'x': p['x'], 'y': row_top, 'w': CELL_W, 'h': CELL_H,
                           'score': round(p['score'], 4)} for p in in_row],
            })
        return rows


# 进程内共享一个检测器，避免每次结算都重新读模板
_DETECTOR = RewardFrameDetector()


def get_detector() -> RewardFrameDetector:
    """取进程内共享的检测器实例。"""
    return _DETECTOR


class FrozenRowsDetector:
    """把已检测好的奖励行包成检测器接口，让同一帧只检测一次。

    forbidden_rects / safe_click_rules 都通过 detector.detect(image) 取奖励行。
    调用方若已经自己 detect 过一次（例如还要用同一批行判断「是否仍在奖励页」），
    就用本类把那批行原样传下去，避免同一帧重复跑一次 60~150ms 的检测。
    """

    def __init__(self, rows: list):
        self.rows = rows

    def detect(self, image) -> list:
        return self.rows


# ---------------- 禁止区域与安全落点 ----------------

def forbidden_rects(image, forbidden_preset=(), detector: RewardFrameDetector = None) -> list:
    """汇总本帧所有禁止点击矩形 = 常驻禁止区域 + 检测出的奖励行。

    奖励行不再逐行返回窄带：从第一个奖励行到最后一行的整个竖向范围连成
    一整块满宽禁区 —— 行与行之间 27px 的间隙同样不该点到（间隙里还有
    下一行奖励的向上光晕），覆盖掉可以彻底排除「点到间隙里」的可能。

    :param forbidden_preset: 任务自选的常驻禁点预设，见 FORBIDDEN_DEFAULT / FORBIDDEN_KEKKAI
    :return: [(name, x, y, w, h)...]
    """
    rects = [(name, x, y, w, h) for name, x, y, w, h in forbidden_preset]
    rows = (detector or _DETECTOR).detect(image)
    if rows:
        # 首行顶（含外扩）到末行底（含外扩）整块满宽禁止
        y0 = max(0, rows[0]['y0'] - ROW_MARGIN)
        y1 = min(BASE_H, rows[-1]['y1'] + ROW_MARGIN)
        rects.append(('reward_grid', 0, y0, BASE_W, y1 - y0))
    return rects


def subtract_rects(rect, blockers, min_w=MIN_SAFE_W, min_h=MIN_SAFE_H) -> list:
    """从 rect 中挖掉所有 blocker，返回互不相交的安全子矩形（右下优先贪心分块）。

    分块从右下角开始：每次取「剩余区域最右下的格子」为锚，先沿底行向左扩满，
    再整体向上扩到不能再扩，得到一个以锚为右下角的极大矩形；依次向左上推进。

    为什么不按边界网格切条再合并：切条会产生横贯全宽的条带，其几何中心远离
    人类热区，拟人化层「矩形内正态采样」的中心被带偏，实际落点分布与密度场
    不符。右下优先的极大矩形让最大一块锚在右下（热区所在一侧），正态中心
    自然贴近热区，也保住了最大块的连续点击面积。

    :param rect: (x, y, w, h)
    :param blockers: [(x, y, w, h)...]
    :return: [(x, y, w, h)...] 按生成顺序（先右下大块，后左上小块）
    """
    rx, ry, rw, rh = rect
    rx1, ry1 = rx + rw, ry + rh

    # 只保留与 rect 真正相交的 blocker，并裁到 rect 内
    clipped = []
    for bx, by, bw, bh in blockers:
        cx0, cy0 = max(rx, bx), max(ry, by)
        cx1, cy1 = min(rx1, bx + bw), min(ry1, by + bh)
        if cx1 > cx0 and cy1 > cy0:
            clipped.append((cx0, cy0, cx1, cy1))
    if not clipped:
        return [(rx, ry, rw, rh)] if rw >= min_w and rh >= min_h else []

    xs = sorted({rx, rx1} | {v for c in clipped for v in (c[0], c[2])})
    ys = sorted({ry, ry1} | {v for c in clipped for v in (c[1], c[3])})
    nx, ny = len(xs) - 1, len(ys) - 1

    # 原子格覆盖位图：blocker 边界对齐切点，整格包含即整格被覆盖
    covered = [[any(cx0 <= xs[c] and xs[c + 1] <= cx1
                    and cy0 <= ys[r] and ys[r + 1] <= cy1
                    for cx0, cy0, cx1, cy1 in clipped)
                for c in range(nx)] for r in range(ny)]

    out = []
    # 从最下行最右列开始扫，遇到的第一个未覆盖格即当前最右下锚点
    for start_r in range(ny - 1, -1, -1):
        for start_c in range(nx - 1, -1, -1):
            if covered[start_r][start_c]:
                continue
            # 沿底行向左扩到底
            c0 = start_c
            while c0 > 0 and not covered[start_r][c0 - 1]:
                c0 -= 1
            # 保持列宽不变整体向上扩到顶
            r0 = start_r
            while r0 > 0 and all(not covered[r0 - 1][k]
                                 for k in range(c0, start_c + 1)):
                r0 -= 1
            # 消耗掉这块区域（过小的块也消耗掉，保证循环推进）
            for r in range(r0, start_r + 1):
                for k in range(c0, start_c + 1):
                    covered[r][k] = True
            bw = xs[start_c + 1] - xs[c0]
            bh = ys[start_r + 1] - ys[r0]
            if bw >= min_w and bh >= min_h:
                out.append((xs[c0], ys[r0], bw, bh))
    return out


def safe_click_rules(image, forbidden_preset=(), detector: RewardFrameDetector = None,
                     candidates=CLICK_CANDIDATES, hot: HotShape = None) -> list:
    """算出本帧可用的安全落点区域，包成 RuleClick 返回（面积大的在前）。

    落点在区域内的具体采样仍交给 RuleClick.coord()，以保留拟人化层的行为。

    :param hot: 任务级热区形状覆盖（GeneralBattle.reward_hot）；None 用通用校准值
    :return: list[RuleClick]，全部为空时返回空列表，由调用方决定兜底
    """
    blockers = forbidden_rects(image, forbidden_preset, detector)
    naked = [(x, y, w, h) for _, x, y, w, h in blockers]
    # 热区 y 锚点：**禁区没碰到热区时热区不动**，碰到了才被往下压。
    # 热区是 HOT_Y_BASE 向下 HOT_H 的一条带，奖励禁区是从屏幕上方压下来的满宽块，
    # 所以「碰到」等价于禁区底边越过热区上沿 HOT_Y_BASE，取两者较大值即可：
    #   1 排（禁区底 284）、2 排（419）都够不到热区上沿 427 → 保持基准锚点；
    #   3 排（554）压进热区 → 锚点下移到禁区底，热区整体让位。
    # 早先直接取禁区底会让浅禁区把热区反向**上提**到屏幕上半部，与真人
    # 「结算点集中在屏幕中下部」相反（实测一排奖励时热区命中率 98%→17%）。
    grid_bottom = next((by + bh for name, _, by, bw, bh in blockers
                        if name == 'reward_grid'), None)
    hot_y0 = max(grid_bottom, HOT_Y_BASE) if grid_bottom is not None else HOT_Y_BASE

    rules = []
    for name, cx, cy, cw, ch in candidates:
        pieces = subtract_rects((cx, cy, cw, ch), naked)
        for i, (x, y, w, h) in enumerate(pieces):
            roi = (x, y, w, h)
            # 候选区域一个都没被裁剪（原样返回）时沿用原名称；
            # 被裁开成多个块时加 _safeN 后缀区分
            if len(pieces) == 1 and (x, y, w, h) == (cx, cy, cw, ch):
                rule_name = name
            else:
                rule_name = '%s_safe%d' % (name, i + 1)
            rule = FieldRuleClick(roi, rule_name, hot_y0, hot)
            # 预计算本矩形的选择权重（面积×密度），weighted_choice 直接取用，
            # 不必在每次点击时重算密度场，也保证热区锚点与检测画面一致
            rule.human_weight = w * h * human_rect_weight(x, y, w, h, hot_y0, hot)
            # 本帧生效的热区锚点：采样只用到闭包里的它，挂出来供日志与单测观察
            rule.hot_y0 = hot_y0
            rules.append(rule)
    if not rules:
        logger.warning('Reward safe click: 所有候选区域都被禁止区域覆盖')
    else:
        # 每帧结算循环都会重算一次，INFO 级别刷屏——降 debug，排查时再开
        logger.debug('Reward safe click: %d 个安全区域 热区y0=%s %s' % (
            len(rules), hot_y0,
            ' '.join('%s%s(d%.2f)' % (r.name, r.roi_front,
                                      r.human_weight / (r.roi_front[2] * r.roi_front[3]))
                     for r in rules)))
    return rules


def _gauss_x(x: float, hot: HotShape = None) -> float:
    """密度场 x 轴分量：以热区中心 x 为峰值，左侧 SIGMA_LEFT（快）、右侧 SIGMA_RIGHT（慢）。

    :param hot: 任务级热区形状覆盖；None 用通用校准值（HOT_DEFAULT）
    """
    hot = hot or HOT_DEFAULT
    cx = hot.hot_x[0] + hot.hot_x[1] / 2.0
    dx = x - cx
    sigma = hot.sigma_left if dx < 0 else hot.sigma_right
    return math.exp(-dx * dx / (2.0 * sigma * sigma))


def _gauss_y(y: float, hot_y0: float, hot: HotShape = None) -> float:
    """密度场 y 轴分量：峰值与两侧 σ 按「可用高度」百分比分配。

    可用高度 = min(HOT_H, 锚点到屏幕底)：热区是锚点向下 HOT_H 的一条带，
    奖励排数多到禁区压过来时才被屏幕底压缩，峰值位置与衰减宽度同比例收窄，
    分布始终贴合剩余空间而不被截断。两侧 σ 依然上快下慢。

    :param hot_y0: 热区 y 锚点（奖励禁区底边）
    :param hot: 任务级热区形状覆盖；None 用通用校准值（HOT_DEFAULT）
    """
    hot = hot or HOT_DEFAULT
    strip = min(HOT_H, BASE_H - hot_y0)
    cy = hot_y0 + hot.peak_ratio * strip
    dy = y - cy
    sigma = (HOT_Y_UP_RATIO if dy < 0 else HOT_Y_DOWN_RATIO) * strip
    return math.exp(-dy * dy / (2.0 * sigma * sigma))


def field_density(x: float, y: float, hot_y0: float = None,
                  hot: HotShape = None) -> float:
    """人类落点密度场在 (x, y) 处的取值：以热区中心为峰值（1.0）的各向异性正态。

    每个轴是以峰值为中心、两侧 σ 不同的单峰正态（split-normal）：左边/上边
    σ 小（衰减快），右边/下边 σ 大（衰减慢），单峰钟形无平台。密度可分离
    （x/y 两轴独立相乘），可分离性被 FieldRuleClick 用于两轴独立加权采样。

    :param hot_y0: 热区 y 锚点（奖励禁区底边）；None 时用 HOT_Y_BASE 基准锚点
    :param hot: 任务级热区形状覆盖；None 用通用校准值（HOT_DEFAULT）
    """
    if hot_y0 is None:
        hot_y0 = HOT_Y_BASE
    return _gauss_x(x, hot) * _gauss_y(y, hot_y0, hot)


def _edge_taper(dist: float, margin: float) -> float:
    """块边渐缩：距块边 dist（px）在渐缩宽度 margin 内按 smoothstep 从 0 升到 1。

    保证块边界上密度为 0、渐缩宽度处恢复全值；margin<=0 时恒为 1（不渐缩）。
    """
    if margin <= 0:
        return 1.0
    u = dist / margin
    if u >= 1.0:
        return 1.0
    if u <= 0.0:
        return 0.0
    return u * u * (3.0 - 2.0 * u)


def human_rect_weight(x, y, w, h, hot_y0: float = None,
                      hot: HotShape = None) -> float:
    """矩形内人类落点密度均值（密度场 × 块边渐缩，网格采样近似面积分）。

    加权选择时再乘矩形面积，等价于「该矩形覆盖的期望点击量份额」。
    纯几何函数（无随机性），方便单测与调参。

    :param hot_y0: 热区 y 锚点（奖励禁区底边）；None 时用 HOT_Y_BASE 基准锚点
    :param hot: 任务级热区形状覆盖；None 用通用校准值（HOT_DEFAULT）
    """
    nx = max(1, min(12, round(w / FIELD_GRID_STEP)))
    ny = max(1, min(8, round(h / FIELD_GRID_STEP)))
    # 两轴各自的渐缩宽度（块尺寸百分比），与 FieldRuleClick 的块内采样保持一致
    mx, my = EDGE_TAPER_RATIO * w, EDGE_TAPER_RATIO * h
    total = 0.0
    for i in range(nx):
        px = x + (i + 0.5) * w / nx
        tx = _edge_taper(min(px - x, x + w - px), mx)
        for j in range(ny):
            py = y + (j + 0.5) * h / ny
            ty = _edge_taper(min(py - y, y + h - py), my)
            total += field_density(px, py, hot_y0, hot) * tx * ty
    return total / (nx * ny)


def _axis_sampler(start: int, length: int, weight_fn):
    """把 [start, start+length) 切成 FIELD_SAMPLE_STEP 宽的分片，构建加权抽样表。

    :param weight_fn: 一维密度函数（x 轴或 y 轴分量）
    :return: (切片起点列表, 切片终点列表, 累计权重列表)
    """
    n = max(1, (length + FIELD_SAMPLE_STEP - 1) // FIELD_SAMPLE_STEP)
    starts, ends, cum = [], [], []
    acc = 0.0
    for i in range(n):
        s = start + i * FIELD_SAMPLE_STEP
        e = min(s + FIELD_SAMPLE_STEP, start + length)
        acc += (e - s) * weight_fn((s + e) / 2.0)   # 片宽 × 片中心密度
        starts.append(s)
        ends.append(e)
        cum.append(acc)
    return starts, ends, cum


class FieldRuleClick(RuleClick):
    """结算落点专用 RuleClick：块内采样按「密度场 × 块边渐缩」，不走拟人化维度 A。

    为什么不复用拟人化层的 center/offset_gauss：
      1. 拟人化以「矩形几何中心」为正态均值，表达不了热区偏置 —— 实测大块内
         落点重心被拉到矩形中心左侧，与密度场「右边>左边」的要求相反；
      2. 正态尾巴超出矩形时被钳到边界，产生贴边堆点（实测占 5%，其中左边界
         一条竖线就压了近 2.5%），是非自然的机器痕迹。
    本类按可分离密度场做两轴独立加权抽样：联合分布恰好 ∝ 密度场本身，
    落点永远在矩形内部且不会堆在边上。配合选块权重（面积×均值密度=场的积分），
    两级相乘后整体落点分布严格等于「密度场×块边渐缩」。

    块边渐缩（_edge_taper）保证任何块边界上密度为 0 —— 否则落点沿块边聚成
    直线，几条直线拼出块的矩形轮廓。

    代价：结算落点不再消费人格的 aim_bias（握姿偏心已内建在密度场的各向异性里）；
    按压时长、轨迹形状、点击间隔等其余拟人化维度不受影响，仍走原链路。
    """

    def __init__(self, roi, name, hot_y0: float, hot: HotShape = None):
        super().__init__(roi_front=roi, roi_back=roi, name=name)
        x, y, w, h = roi
        # 两轴抽样表在构造期一次算好；权重 = 密度场分量 × 块边渐缩
        # （渐缩保证块边界上密度为 0，落点不会沿块边聚成直线）
        mx, my = EDGE_TAPER_RATIO * w, EDGE_TAPER_RATIO * h

        def x_weight(px):
            return _gauss_x(px, hot) * _edge_taper(min(px - x, x + w - px), mx)

        def y_weight(py):
            return _gauss_y(py, hot_y0, hot) * _edge_taper(min(py - y, y + h - py), my)

        self._x_table = _axis_sampler(x, w, x_weight)
        self._y_table = _axis_sampler(y, h, y_weight)

    @staticmethod
    def _draw(table):
        """按累计权重抽一个切片，再在切片内均匀取一点。"""
        starts, ends, cum = table
        u = random.random() * cum[-1]
        i = min(bisect.bisect_right(cum, u), len(cum) - 1)
        return random.uniform(starts[i], ends[i])

    def coord(self) -> tuple:
        """块内落点：x/y 独立按密度场边缘分布采样，乘积即联合密度。"""
        return int(self._draw(self._x_table)), int(self._draw(self._y_table))

    def coord_more(self) -> tuple:
        """roi_back 与 roi_front 相同，直接复用同一采样。"""
        return self.coord()


def multi_click_weights(page_clicks: int, sizes=MULTI_CLICK_SIZES,
                        base=MULTI_CLICK_WEIGHTS) -> tuple:
    """按本结算过程已发起的点击事件数查表返回多击档位权重（单击档位恒定）。

    2026-09-06 起为查表实现（用户指定的非线性簇长分布，线性衰减率表达不了，
    MULTI_CLICK_DECAY 已删除）：按 page_clicks 索引 MULTI_CLICK_WEIGHTS_BY_EVENT，
    越界（第 8 次起）取末档（等价于永远单击）。

    :param page_clicks: 已发起的结算点击事件数（一次手势计 1 次，连点内部不计数）
    :param sizes: 档位元组，与权重一一对应（保留便于单测替换；查表行按
        (单击, 2, 3, 4) 顺序展开，与 sizes 顺序一致）
    :param base: 保留参数兼容既有单测调用签名；查表生效时忽略
    :return: 当前点击事件序号下应使用的权重组，长度与 sizes 一致
    """
    row = MULTI_CLICK_WEIGHTS_BY_EVENT[min(page_clicks, len(MULTI_CLICK_WEIGHTS_BY_EVENT) - 1)]
    return tuple(row)


def in_avoid(avoid: tuple, x: int, y: int) -> str:
    """落点是否踩在回避区里，是则返回该区域名，否则返回 None。

    半开区间 [x, x+w) × [y, y+h)，与 subtract_rects 的切分语义一致。
    """
    for name, ax, ay, aw, ah in avoid:
        if ax <= x < ax + aw and ay <= y < ay + ah:
            return name
    return None


def avoid_coord(rule, avoid: tuple, tries: int = SETTLEMENT_AVOID_TRIES) -> tuple:
    """在块内按密度场取点，落进回避区就重取（拒绝采样），返回 (x, y)。

    与「把回避区并入禁区」的区别：禁区会改变分块几何，把热区挤到别处；
    拒绝采样保持密度场形状不变，只是把落点从回避区里挪开——落点仍分布在
    原来的热区范围内，只是回避区那几块的概率被重新摊到周围。

    :param avoid: [(名称, x, y, w, h)...]，空则等价于直接 rule.coord()
    """
    x, y = rule.coord()
    if not avoid:
        return x, y
    for _ in range(tries):
        hit = in_avoid(avoid, x, y)
        if hit is None:
            return x, y
        x, y = rule.coord()
    # 兜底：重试用尽仍在回避区（该块几乎被回避区铺满），返回最后一次落点
    logger.warning('Settlement avoid: %d 次重取仍落在 %s，用最后一次落点 (%d, %d)'
                   % (tries, in_avoid(avoid, x, y), x, y))
    return x, y


def locate_rule(rules: list, x: int, y: int):
    """返回包含点 (x, y) 的安全区域，没有则 None。

    用于判断「上一次结算落点在本帧是否仍然安全」：胜利画面无奖励框、奖励页有，
    同一个坐标在下一页可能正好压在新出现的奖励行上。
    """
    for r in rules or ():
        rx, ry, rw, rh = r.roi_front
        if rx <= x < rx + rw and ry <= y < ry + rh:
            return r
    return None


def shift_down_to_safe(rules: list, x: int, y: int):
    """保持 x 不变、沿 y 向下找最近的安全区域，返回 (y, rule)；找不到返回 None。

    上次落点被新出现的奖励行覆盖时用它「把落点往下挪」而不是重新自由取点：
    热区本就锚在奖励禁区下方，向下平移与真人「奖励框弹出后手往下挪一点继续点」
    一致，也保住了「奖励点击参考胜利点击位置」的横向对齐。
    落点取块内偏上的位置（贴着禁区下沿），避免一路滑到屏幕最底。
    """
    best = None
    for r in rules or ():
        rx, ry, rw, rh = r.roi_front
        if not (rx <= x < rx + rw):
            continue                    # x 不在该块的横向范围内，平移到不了
        if ry + rh <= y:
            continue                    # 整块都在上方
        ny = max(ry, y)                 # 落在块内，或从块顶开始
        if ny >= ry + rh:
            continue
        if best is None or ny < best[0]:
            best = (ny, r)
    if best is None:
        return None
    ny, rule = best
    rx, ry, rw, rh = rule.roi_front
    # 贴块顶会压在禁区外扩边缘上，向下留出一点余量（不超过块高的 1/4）
    ny = min(ny + int(min(rh * 0.25, 24)), ry + rh - 1)
    return int(ny), rule


def weighted_choice(rules: list) -> object:
    """按「面积 × 人类落点密度」加权随机挑一个安全区域。

    random.choice 是等概率的：小块与大块同等机会被选中，大区域的样本密度
    被稀释。面积 × 密度均值等价于「矩形覆盖的期望点击量份额」，既保住大区域
    的份额，又让真人的分布习惯生效 —— 热区（中下偏右）更容易被选中，
    贴屏幕边、靠左靠上的区域更少被选中。

    safe_click_rules 产出的 RuleClick 自带预计算的 human_weight（含当帧
    热区锚点）；其他来源的 RuleClick 没有该属性，退回静态密度场计算。

    空列表时返回 None，由调用方决定兜底。
    """
    if not rules:
        return None
    if len(rules) == 1:
        return rules[0]
    weights = []
    for r in rules:
        w = getattr(r, 'human_weight', None)
        if w is None:
            w = r.roi_front[2] * r.roi_front[3] * human_rect_weight(*r.roi_front)
        weights.append(w)
    # random.choices 返回单元素列表，取第一个
    return random.choices(rules, weights=weights, k=1)[0]
