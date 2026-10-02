# MultiDailyAltAcc - 多账号小号每日任务

## 概述

`MultiDailyAltAcc` 是多账号小号每日任务的调度器，负责在多个小号之间轮转切换并执行每日例行操作。每个小号的具体日常逻辑由 `DailyAltAcc` 任务完成。

## 功能

对配置的小号列表依次执行以下可选子任务（通过 `DailyAltAcc` 实现）：

| 子任务 | 配置字段 | 说明 |
|--------|----------|------|
| 庭院事务 | `courtyard_enable` | 签到、领取庭院奖励 |
| 邮件 | `mail_enable` | 领取邮件附件 |
| 协作 | `cooperation_enable` | 寻找协作任务 |
| 捐勾玉 | `donatejade_enable` | 寮捐勾玉 |
| 回礼 | `returngift_enable` | 好友回礼 |
| 周奖励 | `weekaward_enable` | 领取寮周奖励、商店 |
| 神秘商店 | `mysteryshop_enable` | 刷新并购买神秘商店 |
| 种树 | `tree_planting_enable` | 0=不运行 1=买花 2=买花捐树 |
| 试炼战斗 | `trialbattle_enable` | 每日试炼 |
| UP召唤 | `summon_up_enable` | 领取UP召唤礼包 |
| 挂卡 | `kekkaiActivation_enable` | 结界挂卡 |
| 蹭卡 | `KekkaiUtilize_enable` | 结界蹭卡 |
| 同心队 | `alliedteam_battle_enable` | 同心队战斗 |
| 补体力 | `alliedteam_ap_enable` | 同心队补充体力 |

## 架构

```
MultiDailyAltAcc/          # 多账号调度层
├── script_task.py         # 主调度器：账号排序、切换、重试、进度追踪
├── config.py              # 配置模型（MultiDailyAltAcc, DailyConfig, ExtendedAccountInfo）
├── assets.py              # 资源定义
├── DailyAltAccEx.py       # 桥接模块：为 DailyAltAcc 注入账号级配置
└── README.md

DailyAltAcc/               # 单账号执行层
├── script_task.py         # 单账号日常任务入口
├── config.py              # 单账号配置（DailyAltAccConfig）
├── courtyard.py           # 庭院事务
├── mail.py                # 邮件
├── cooperation.py         # 协作
├── donatejade.py          # 捐勾玉
├── returngift.py          # 回礼
├── mshop.py               # 神秘商店
├── tree.py                # 种树
├── trialbattle.py         # 试炼战斗
├── summon_up.py           # UP召唤
├── alliedteam.py          # 同心队
└── utils.py               # 公共基类 DailyAltAccBase
```

## 配置

### 全局配置（DailyConfig）

在 GUI 中配置，控制所有小号的全局开关：

- `sup_account_count`: 小号数量
- `disable_task_rotation`: 关闭任务自动轮转（见下节）
- `total_*_enable`: 各子任务的全局开关
- `shutdown_after_finish`: 0点-8点期间同心战斗完成后检测是否关机

### 任务轮转与「关闭任务自动轮转」

默认（不勾选 `disable_task_rotation`）按时间在四个阶段间轮转，每轮的执行内容
由 `task_plan.json` 物化进 `total_*` 开关：

| 阶段 | 时间 | 做什么 |
|---|---|---|
| 早轮 | `schedule.morning_time` | `plan.morning` 勾选的普通任务 |
| 晚轮 | `schedule.afternoon_time` | `plan.afternoon` 勾选的普通任务 |
| 回礼轮 | 次日 00:20 | 回礼 + `plan.returngift` 勾选的勾协/神秘商店 |
| 同心战斗轮 | 回礼轮完成 3 分钟后 | 仅同心战斗 |

勾选 `disable_task_rotation` 后：

- **不轮转**：完全不再读取 `task_plan.json`，`total_*` 开关只按手动勾选执行。
  回礼与同心战斗降级为普通任务（勾了就随每趟一起做，不再单独成轮）。
- **运行时间沿用通用调度器**：下次运行 = 本次开始时间 + `Scheduler.success_interval`
  （默认 1 天），在 OASX 的调度器面板调整。
- **失败逻辑不变**：仍是 3 分钟后重试，并保留进度接续未完成的账号。
- 试炼战斗 / 种树 / UP召唤 / 发布SR 仍是「跑一次自动关闭」的一次性开关。

### 账号配置（ExtendedAccountInfo）

每个小号可单独覆盖全局开关，支持精细化控制。

## 执行流程

1. 读取配置，获取小号列表
2. 按邮箱分组排序（减少邮箱切换次数），按完成时间排序
3. 过滤已完成的账号（基于进度文件 `logs/multi_daily_progress_<配置名>.json`）
4. 依次切换到每个小号，调用 `DailyAltAcc` 执行日常任务
5. 每个账号最多重试 3 次
6. 全部完成后在0点-8点闲时期间检测是否关机

## 注意事项

1. 需要额外开启一个模拟器用于切换登录小号
2. 小号配置相关信息见 [SwitchAccount](../Component/SwitchAccount/README.md)
3. 要求被邀请对象处于登录状态，与小号是好友关系
4. 多开实例的运行状态追踪基于文件（`./logs/multidailyaltacc_progress.json`），支持多进程并行

## 子任务进度持久化与异常恢复

进度文件：`logs/multi_daily_progress_<配置名>.json`，每个实例一份。

- 账号与子任务的完成状态都记在这里，任务中断（崩溃、断电、失败重调度）后
  自动接续：已完成的账号整个跳过，已完成的子任务跳过，同心战斗从已记场次
  继续打剩余次数。
- 子任务抛业务异常时标记为 `failed`，推送一封「子任务异常已跳过」通知，
  本轮后续接续不再重试该子任务；**该跳过只对当前角色生效**，其他角色的
  同名子任务照常执行。
- 子任务连续 2 次显式返回未完成（如庭院当轮无奖可领、邮箱为空）会被标记
  为 `skipped` 放行，只记日志不发通知，避免账号因「永远无法完成」的子任务
  无限重试。
- 每条 `failed` / `skipped` 迁移会同步追加到异常归档文件
  `logs/multi_daily_errors_<配置名>.json`（按天分组，写入时自动只保留今天和昨天
  两组），进度文件被清空或重建后仍可在此回查当天所有没有正常结束的子任务；
  `failed` 记录含异常类型与消息，`skipped` 记录含累计 False 次数，同心战斗
  附带已打场次。归档写入失败只记日志，不影响任务执行。
- 进度在任务成功收尾、安排下一调度阶段时自动清除（主文件与快照一起删）。
  若想强制全量重跑，删除该进度文件即可（异常通知里附了文件路径）；同名
  `.bak` 快照会随后被新进度覆盖，不必手动处理。
- 每次落盘都会同步写一份 `.bak` 快照。主进度文件**存在但损坏**（停电/断电
  可能让最后一次落盘留下截断或全零的 JSON）时自动从快照接续，避免按「无进度」
  把已完成的账号与同心战斗场次全部重做一遍；主文件**不存在**时不看快照，
  保证「删除进度文件＝强制全量重跑」这条用法不被陈旧快照复活。
- 进度超过 18 小时视为过期：某阶段连续失败重调度超过该时长后会全量重建，
  已完成的账号与同心战斗场次会重新执行（宁多跑不漏跑的兜底）。
- 原 `need_login` / `need_login_time` 字段已删除，账号完成判定完全由进度文件驱动。
