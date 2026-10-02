# MultiDailyAltAcc / DailyAltAcc：多账号日常审计

更新日期：2026-10-02。来源：S1/S4、W1及当前 be4160d 基线静态核查；[证据说明](README.md#evidence)。

## 1. 目标与“一遍完成”定义

账号进入流程后，每项启用任务都有可解释的结果，已完成任务不再重复进入或领取，临时失败有有限恢复，中断只补未完成部分。整轮终结与全部成功必须分开。

“没有异常日志”“没有 pending”或“账号标为 done”单独都不能证明业务全部完成。

## 2. 当前执行链

```text
通用调度器
  → MultiDailyAltAcc.run
  → 阶段/单用途轮判断、建立或接续进度
  → 账号过滤与排序
  → 合成 total AND account 有效配置
  → 切号或受限复用紧邻重试登录
  → DailyAltAcc 固定顺序执行启用且未终结的子任务
  → 逐任务保存结果
  → 账号结果汇总
  → 失败接续或下一阶段排程
  → 收尾
```

账号循环位于 [MultiDailyAltAcc/script_task.py](../../tasks/MultiDailyAltAcc/script_task.py)。本地 [progress.py](../../tasks/MultiDailyAltAcc/progress.py)包装和复用 [共享 ProgressStore](../../tasks/Component/MultiAccountRunner/progress.py)，不是另一套完整持久化系统。

## 3. 开关与任务计划语义

| 场景 | 当前条件 | 不能误解的地方 |
|---|---|---|
| 普通早晚轮 | 排程时计划物化为 total；执行时 total AND account | 不是每次启动重新读取普通 plan 并三重门控 |
| 回礼/同心单用途轮 | total、single_purpose、账号条件共同作用 | total 同时可能承担轮次身份 |
| disable_task_rotation | 不加载任务计划，按手动 total 与账号开关执行 | 计划值不决定此模式的实际执行 |
| 周奖励/神秘商店 | 还受星期门控 | true 不是无条件执行承诺 |
| 一次性任务 | 成功收尾后关闭对应总开关 | 应尊重已有一次性语义 |

依据：_create_account_config、_schedule_plan_phase、next_run、_reset_one_shot_flags，以及 [task_plan.py](../../tasks/MultiDailyAltAcc/task_plan.py)的解析和 enabled。

MD-01：下午计划物化后，_schedule_normal_day 仍将周奖励和神秘商店 total 强制置 false。启用这两个下午选项时存在不执行问题。当前默认计划未打开这两项，不能描述为所有默认运行都漏跑。

最小方案是核实并处理重复覆盖，保留星期门控及现有 total 手动单轮语义，不重写任务计划系统。

## 4. 当前已有稳定增强

| 能力 | 当前依据 | 保留要求 |
|---|---|---|
| 账号/子任务跳过 | is_account_done、is_task_finished | 已完成进度不全量重建 |
| pending 防遗漏 | _finalize_account_progress | 未完成项继续保留 |
| 进度原子保存 | _write_json_atomic：临时文件、flush、fsync、replace | 不重复实现另一套 ProgressStore |
| 损坏回退 | 主文件损坏可用 bak；主文件缺失不自动复活备份 | 保留删除和损坏的语义区分 |
| 阶段快照兼容 | ensure_phase、phase_flags_match | 旧快照接续不简单失效 |
| 同心场次接续 | 场次落盘，有新增场次时保留续做 | 不丢失累计次数 |
| 设备异常穿透 | Daily 和父任务上抛设备级异常 | 不被业务包装吞掉 |
| 嵌套调度屏蔽 | DailyAltAccEx.shield_self、create_nested_task | 子任务不改变主排程 |
| 紧邻重试登录复用 | 四项身份一致且新截图确认庭院 | 新账号、异常和重新调度清除复用 |
| 回礼等待补差 | 实际前置耗时抵扣 10 秒间隔 | 不是再固定等 10 秒 |
| 庭院安全收尾 | 奖励优先、三帧且至少一秒确认、主页确认、120 秒预算 | 不为加速删除确认 |

逐任务 ProgressStore 已原子化，与另一个共享 daily_progress.json 的直接写入问题不同。

## 5. 状态模型与完成判断

当前状态：pending、done、failed、skipped。后三种都表示本轮终结，并在接续时跳过，这是有限收敛策略。

_finalize_account_progress 在无 pending 时可以标账号 done，即使含 failed/skipped。父 run 的 finally 又调用 _mark_task_completed 写 completed=True，关机协调读取该字段。

建议先增加结束原因和只读结果汇总：成功、正常无事可做、部分失败、中断。保留现有终态跳过，不把所有 failed 改回 pending。只对明确可恢复的原因设置有限重试。

Daily 的 _run_with_stat 将正常非 False 返回（含 None）和 TaskEnd 视为 done。不能一次改变所有历史返回值；先适配具体误报出口。

## 6. 问题清单与最小方向

优先级按本次统一定义；历史复现属于 S1 当时环境，不是本轮再次执行的结果。

| ID | 优先级 | 事实/风险及依据 | 最小方向 |
|---|---|---|---|
| MD-01 | P0（启用时） | _schedule_normal_day 覆盖下午周奖励/商店 plan | 消除或明确覆盖语义，覆盖早晚/星期组合 |
| MD-02 | P0 | _current_normal_plan_phase 与新实例启动时间相关；S1记录跨时段重试改变阶段的风险 | 接续保留原轮次身份，新轮才换阶段 |
| MD-03 | P0 | TaskPlan 生成当日目标；结束较晚时下一时刻可能在过去 | 保证目标晚于当前时刻，按目标执行日做星期门控 |
| MD-04 | P0 | failed/skipped 与成功混入账号/整轮完成汇总 | 分开 settled 与 success，保留失败原因 |
| MD-05 | P0 | 非 False/TaskEnd 被记 done；邮件入口、试炼 None、挂卡入口失败等出口不可靠 | 逐任务显式区分完成、no_work、retryable、failed |
| MD-06 | P1 | 庭院虽已有稳定确认，初始已灰或自行返回主页仍可能受本轮点击标志限制；S1有隔离复现 | 接受经确认的已完成和主页证据，保留遮挡门禁 |
| MD-07 | P1 | Restart.harvest 与 Daily 分别负责庭院/邮件 | 传入本轮收获策略或交接已确认结果，不永久改 Restart 配置 |
| MD-08 | P0 | 临时挂卡/寄养参数共享 Config；中途或父收尾保存可能写回 | 隔离借用子树；只在末尾恢复内存不足以防中途写盘 |
| MD-09 | P2/需要验证 | 阶段快照未涵盖全部开关，账号进度键未包含全部登录身份 | 定义新增任务恢复边界；实际需要时兼容旧身份键 |
| MD-10 | P0 | mshop 退出 while time.time() > start_time - 5 恒真 | 固定总截止；到期明确未完成或恢复 |
| MD-11 | P1/需要验证 | 旧分享开关测试与当前 config/入口不一致 | 确认业务是否保留，再修测试或功能，不能按测试存在宣称已实现 |
| MD-12 | P0 | finally 无条件写 completed，可能进入协调关机判断 | 分开运行结束与满足成功条件；异常/中断不得触发全部完成 |
| MD-13 | P1 | daily_progress 使用 threading.Lock 与直接覆盖写，不覆盖跨进程 | 单独文件锁和原子写，损坏不默认为完成 |
| FW-01 | P0 | force_call 不绕过 enable=False；Restart 可能不执行 | 一次性恢复入口，请求清除后接续原任务 |

相关具体文件：[Daily 入口](../../tasks/DailyAltAcc/script_task.py)、[mail.py](../../tasks/DailyAltAcc/mail.py)、[trialbattle.py](../../tasks/DailyAltAcc/trialbattle.py)、[courtyard.py](../../tasks/DailyAltAcc/courtyard.py)、[mshop.py](../../tasks/DailyAltAcc/mshop.py)、[DailyAltAccEx.py](../../tasks/MultiDailyAltAcc/DailyAltAccEx.py)、[配置调度](../../module/config/config.py)。

## 7. 等待、重复操作与效率

- 网络前置、庭院补救、邮件退出等具体循环应有局部总预算；不能只依赖点击重置后的设备卡死计时。
- 没有奖励的正常结束与暂时识别失败不应共用同一 False 重试策略。
- 已确认登录收获不要再次进入相同业务，但必须传递完成证据。
- 同一帧多个判据可复用；任何点击、页面变化、账号切换后失效。
- 按状态变化记录日志，最终记录结果、原因、尝试次数和下一时刻。
- 保留紧邻登录复用、回礼补差和庭院连续确认；它们已经存在。

当前神秘商店主要扫描、筛选、提醒并保留货架，实际购买仍有 TODO；不能沿用 README 的“购买”描述推断完整购买能力。

## 8. 验收与待验证

至少覆盖：已完成页面、空邮箱、入口失败、False/None/TaskEnd、部分失败、禁用 Restart、跨 18 点/午夜、任务计划变化、嵌套任务中途保存、多进程完成记录和关机门控。仅模拟关机条件，不在离线验证中执行真实关机。

模块 README 的进度路径仍写 logs，当前代码默认是 config/tasks_config，文档以代码为准。账号跨平台身份冲突取决于真实使用，未确认。

S1 记录 85 项既有离线测试通过及若干隔离复现；S2 的 48 项突破测试可能重叠，不能相加。它们不证明当前 Personal 全链路一遍完成。本轮未运行测试、未启动游戏、未测量性能。
