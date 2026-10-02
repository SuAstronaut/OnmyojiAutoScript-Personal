# OAS Diagnostic Assistant：日志与诊断体系设计

审查日期：2026-10-02。对象：`D:\Entertainment\Tool\OAS`，OnmyojiAutoScript-Personal。

当前核查 HEAD：`1fc18f6a66cab44120d45291dbc0aa7e8dbacb8e`，分支 `main`。本页以实际工作区源码为依据，不能把入口文档中的初始业务基线 `be4160df…` 当作本轮 HEAD。

已阅读 [README](README.md)、[项目背景](00_Project_Context.md)、[当前架构](01_Current_Architecture.md) 和根 [AGENTS.md](../../AGENTS.md)。开始时已有审计文档修改及未跟踪文件，本轮保留它们，只新增本页。**未修改正式代码、未修复 bug、未运行 OAS 或游戏、未运行测试。历史日志只检查目录、文件名形状、数量和大小，没有读取正文、打开截图、上传、移动、清理或改写。按本次历史日志只读约束，不追加外部 worklog。**

本文中的“当前确认”指源码静态确认或目录元数据确认；“建议”均未实施。历史日志内容、实际触发频率、现场根因、运行性能未确认。

## 1. 结论与设计边界

现有 OAS 已具备可用的诊断基础：统一 logger、实例按日文本日志、调度任务边界、GameUi 导航消息、带资源名的点击记录、部分等待超时、分类异常处理、错误截图包，以及多账号日常的 `[STAT]` 结构化统计。它能帮助人工排查，但无法稳定、完整地回答每次运行的五个问题。

主要缺口是**关联、时效、动作结果和业务语义**，不是缺少另一套日志框架。建议先做本地只读的 OAS Diagnostic Assistant，复用已有证据，再在确有收益的边界补少量可选诊断事件。

助手的职责是回答“发生了什么、证据在哪里、还缺什么、下一步如何核查”。不执行点击、切号、Restart、改配置或自动修复；不根据诊断结果改变原有恢复、重试、结算和任务完成逻辑。不引入全局状态机、统一任务重写、外部日志平台或强制模型服务。

## 2. 当前日志能力

### 2.1 module/logger

证据：[module/logger.py](../../module/logger.py)。

| 当前能力 | 实际行为 | 诊断含义与限制 |
|---|---|---|
| 统一日志入口 | `logging.getLogger('oas')`；`logger_debug=False`，当前默认 INFO | DEBUG 消息默认不可见；不能靠未输出的调试信息诊断历史故障 |
| 磁盘日志 | UTF-8 追加到 `log/YYYY-MM-DD_<name>.txt`；包含完整日期、毫秒、文件名、行号、等级、消息 | 可定位调用点和时间；普通消息可能按 160 列折行，一个物理行不一定是一条事件 |
| 按日滚动 | `RichFileHandler.maybe_rollover()` 切换文件并更新 `logger.log_file`，新文件补 START 三行块 | 已有跨天支持，不应重复设计为待实现功能；跨天 START 不等于新进程启动 |
| 实例文件切换 | `set_file_logger()` 替换并关闭旧文件 handler；调度及子进程入口按配置名设置 | 实例通常由文件名体现，普通行没有稳定 session/run 标识 |
| 展示渠道 | 控制台、Rich 文件、Flutter callback/管道，格式不同 | GUI 只有时分秒且可能继续折行；诊断首选原始文件，不能把展示文本当唯一机器协议 |
| 辅助格式 | `hr/rule` 输出分隔块；`attr` 输出 `[名称] 值` | `hr/rule` 经 console 直接写出，绕过普通 `LogRecord.emit`；只增加 logging handler 无法捕获所有边界 |
| 结构化统计保护 | `[STAT] ` 行走特殊渲染路径，整行写出 JSON | 已有机器协议；新诊断不能破坏它，也不能假定任意新前缀自动拥有此能力 |
| 异常展示 | Rich traceback；文件和 Flutter handler 开启 locals 展示；`logger.error(Exception)` 补异常类型 | 并非所有异常分支都调用 `logger.exception`；文本及 locals 也不是通用脱敏后的安全输出 |
| 可选清理 | `cleanup_logs(keep_days=7)`；`set_file_logger(do_cleanup=False)` 默认关闭 | 所查调用未显式启用清理，不能称“默认自动保留七天”；清理函数处理根目录旧文件和 `error` 子目录，其他子目录不在同一路径中 |

logger 模块导入本身会改变工作目录、初始化文件日志并写 START。因此本轮只读源码，未通过 import 来验证日志，也不建议离线助手导入它。

### 2.2 task 日志与多账号统计

证据：[script.py](../../script.py) 的 `loop/run/_run_task/get_next_task`；[stat_log.py](../../tasks/DailyAltAcc/stat_log.py)；[DailyAltAcc](../../tasks/DailyAltAcc/script_task.py) 的 `_run_with_stat`；[MultiDailyAltAcc](../../tasks/MultiDailyAltAcc/script_task.py)。

- 调度器输出 `Scheduler: Start task`、任务标题块、动态加载模块路径、`Scheduler: End task`，可识别顶层任务的执行片段。配置任务队列与 GUI schedule 状态是辅助信息，不能代替实际进入的任务。
- 多账号日常已有 `run_start/run_end`、`switch_start/switch`、`acc_start/acc_end`、`task_start/task_end/error` 等 `[STAT]` 事件。账号上下文含 `acc/char/svr/sys`，子任务含 `task`，结束含 `ok/dur`，异常含 `etype/emsg`。
- `[STAT]` 覆盖的是已有统计调用点，不能推广为所有任务都有完整结构化生命周期。其 `run_start/run_end` 是事件边界，不是全局唯一 `run_id`；`run_end` 在 finally 中输出也不表示成功。
- 多账号会动态构造 DailyAltAcc 及嵌套任务。`config.task.command` 可能仍是顶层调度任务；只读它不能准确得到当前子任务。单个任务模块自己的阶段、Scene、资源不足等日志语义不统一。
- `TaskEnd` 是控制流信号；顶层 End 日志也会出现在返回 False 的路径。MemoryScrolls 存在报错后抛 `TaskEnd` 的路径。因此助手必须区分“控制流结束”“调度层结果”“子任务结果”“业务目标是否确认”。不修正这些业务行为，也不替它们猜测成功。

### 2.3 GameUi 与当前页面

证据：[game_ui.py](../../tasks/GameUi/game_ui.py)；[page.py](../../tasks/GameUi/page.py)。

已有 PageRegistry、动态页面注册、页面图和覆盖页处理，不能描述成“没有页面状态”。`ui_current` 是对象内最近识别或设置的页面；日志包括：

- `[UI] <page>`：页面判据命中。登录页日志输出后仍可能抛 `GameNotRunningError`，不表示允许继续业务。
- `Current page / Following shortest path`：当时的导航起点和计划路径。
- `Page switch: A -> B`、`Wait appear and operate`：计划进行切换和等待操作，不能作为 B 已到达的证据。
- `Page arrived B`：到达判据命中，是比切换计划更强的证据。
- 未识别页面、按钮识别失败、导航超时、覆盖页变化、皮肤探测/回庭院等处理线索。

限制：任务内局部 Scene 不一定同步到 GameUi；`ui_current` 没有统一的识别帧 ID、观察时间和过期标记。覆盖页检测中的状态变化也不是每次都有独立事件。助手只能说“最后一次观察到的页面”，不能把很久前的日志当作故障时页面。

### 2.4 点击与等待

证据：[control.py](../../module/device/control.py) 的 `click/long_click/swipe/drag`；[device.py](../../module/device/device.py) 的控制检查、stuck 检测；[BaseTask](../../tasks/base_task.py) 的识别、点击和等待接口。

点击通常输出 `Click (坐标) @ 资源名`，长按也使用 Click 文本并附时长。BaseTask 会向 Device 传入目标资源名；裸坐标调用可能只得到默认名 `Click`。滑动、拖动另有日志。

**点击日志位于底层动作执行之前，且 click 的部分节奏等待也在日志之后。** 这条记录证明调用准备执行，不能单独证明动作已发送、后端返回或游戏响应。拟人化后端实际轨迹也不能从该坐标消息完整还原。`click_record` 是最多 15 项的资源名队列，不是带坐标/时间/结果的动作审计；控制检查还能在输出 Click 前暂停或抛异常。

等待已有以下线索，但覆盖不完整：

| 等待类别 | 现有证据 | 缺口 |
|---|---|---|
| 调度等待 | `Wait until <时间> for task`；禁止时段推迟；空闲回庭院/关闭游戏等消息 | 缺统一开始、结束、取消原因及关联标识 |
| 页面导航等待 | `Waiting for page`、等待按钮、导航超时及部分耗时 | 多次等待可能嵌套，缺稳定 wait ID 和最终结果 |
| 目标出现/消失/稳定 | 部分起始消息和超时警告；一些接口主要在超时才输出 | 等待开始时未必可见，成功/提前返回不统一；有的没有局部超时 |
| stuck 保护 | `Wait too long`、`Waiting for {detect_record}`、连续点击历史 | `detect_record` 是集合，不是有序等待清单；异常前会清理记录，事后读取已清理状态会丢失现场 |
| 操作节奏、截图限流、业务循环 | Device 截图前 `pace_view`、动作前等待、截图间隔及任务内 sleep/Timer | 普通日志不能稳定区分主动节奏等待、战斗等待、识别失败和设备阻塞 |

`appear=False` 可能只是 interval 冷却尚未到，不一定执行过图片识别。诊断不得将每次 False 都归类为模板不匹配。`Timer.reached()` 会递增内部确认次数；未来观测代码不能额外调用它来“读取超时状态”，否则会改变运行语义。

### 2.5 screenshot 保存机制

证据：[screenshot.py](../../module/device/screenshot.py)、[script.py](../../script.py) 的 `save_error_log`、[BaseTask.save_image](../../tasks/base_task.py)。

| 路径/机制 | 当前行为 | 诊断限制 |
|---|---|---|
| 内存 `screenshot_deque` | `save_error=True` 时采集 `{time,image}`；容量来自 `screenshot_length` | 不是完整录像；未附任务、页面或 action ID，按采样张数而非固定秒数保留 |
| `log/error/<timestamp_ms>/` | 部分异常触发，保存缓存 PNG 和 `log.txt`；PNG 名为带微秒的时间字符串 | 目录只有毫秒时间戳，没有显式实例归属、执行标识或 manifest |
| 错误文本切片 | 在当前 `logger.log_file` 中找最后一个长 `═` 分隔行，再按现有切片规则复制 | 是分隔块附近的文本片段，不保证全任务、跨天或完整会话；图片与文本未必覆盖相同窗口 |
| `Device.save_screenshot` | 当前写到 `log/screenshots/<timestamp_ms>.png`，按 genre 节流 | genre 未用于目录分类；没有实例/任务关联，不能当作每次截图均落盘 |
| `BaseTask.save_image` | 延时后重新截图，可裁 ROI；写 `screenshots/<调度任务>_Screenshots/<名称>_<时间>.png`，调试时退化 Default | 这是主动业务/通知截图，可能晚于故障；调度任务名不保证就是嵌套子任务名 |

[config_error.py](../../tasks/Script/config_error.py) 的模型默认 `save_error=True`、`screenshot_length=1`。`save_error_log` 注释中的“最近 60 张”不是当前保证。没有读取用户配置，因此实际配置值未确认。历史目录每包一张与默认值相符，但不能据此断言全部运行都使用默认配置。

`save_error_log` 调用了 [sensitive_info.py](../../module/handler/sensitive_info.py)，但当前图像函数直接返回原图，文本只做有限的旧 AzurLane 路径替换。不存在已确认的通用账号、凭据或截图遮罩脱敏。助手优先输出位置引用和白名单元数据；若未来支持导出，必须单独设计脱敏及人工查看，不自动上传原日志、locals 或图片。

### 2.6 exception 处理与已有诊断工具

证据：[exception.py](../../module/exception.py)、[script.py](../../script.py) 的 `_run_task`、[script_process.py](../../module/server/script_process.py)、DailyAltAcc/MultiDailyAltAcc 的异常分支。

| 分类 | 顶层现有处理 | 助手应如何解释 |
|---|---|---|
| `TaskEnd` | 控制流正常返回 True，可通知 | 任务结束信号；业务成功仍要看其他证据 |
| `EmulatorNotRunningError` | 警告、标记恢复、请求 Restart，返回 False | 设备状态/恢复请求；不保证请求已实际执行 |
| `GameNotRunningError` | 警告并请求 Restart，返回 False | 游戏/登录状态判定；这一路没有统一自动错误包 |
| `GameStuckError` / `GameTooManyClickError` | 错误文本、错误包、通知、请求 Restart、等待 10 秒、返回 False | 直接触发条件较明确，但超时/连点不是根因结论 |
| `GameBugError` | 警告、错误包、重启请求和等待 | 错误分类与现有处理；旧 AzurLane 描述不能直接当作本游戏根因 |
| `GamePageUnknownError` | 连续计数、错误包、通知，达到 3 次请求 Restart | 已有失败放行与恢复策略；维护/网络提示只是可能性 |
| `ScriptError` / `RequestHumanTakeover` | critical、通知、退出 | 人工接管/退出原因；该顶层分支未统一保存错误包或完整 traceback |
| 其他 `Exception` | `logger.exception`、错误包、通知、退出 | 有调用栈证据，但不能保证保存现场成功或所有上下文完整 |

DailyAltAcc 会对部分子任务异常输出 `[STAT] error/task_end(ok=False)`，根据既有进度规则标记、保留接续或向上抛出；有些异常被局部处理后继续下一个子任务，不能只看调度层结束判断整个账号成功。多账号设备异常保存现场已有保护方法，但这不代表所有顶层归档都具备同样的保护。

`save_error=False`、未触发归档的分支、局部异常处理、强制退出、磁盘/权限问题都可能造成证据缺失。缺少错误包不能证明没有失败，归档失败也不能覆盖原始故障解释。

[PostDiagnotor](../../tasks/Utils/post_diagnotor.py) 已有御魂溢出图像判据；在所查 `script.py/module/tasks` 中未找到调用接入。它不是已经运行的通用诊断助手，不建议为了本方案把它接到异常恢复逻辑中。

### 2.7 已有日志读取与统计服务

证据：[log_service.py](../../module/server/log_service.py)、[log_router.py](../../module/server/log_router.py)、[log_stats.py](../../module/server/log_stats.py)、[server_logging.py](../../module/server/server_logging.py)。

- LogBrowserService 已支持日志文件索引、按字节/行数限制读取、游标、SSE 增量、错误目录列表、错误详情和图片路径检查。行记录提供 `file_name/line_no/offset/byte_length` 及截断标志，可作为助手证据定位的基础。
- 服务根目录固定在 OAS/log，并已有路径范围校验。不能为方便诊断开放任意文件路径或复用无边界扫描。
- log_stats 已解析任务/战斗分隔块及 `[STAT]`，并兼容历史折行 JSON。建议复用其规则和样例，而非复制整套统计器或改写统计语义。
- 错误读取支持纯时间戳及 `<script>_<timestamp>` 两种名称，但当前 `Script.save_error_log` 仍写纯时间戳。读端支持新格式不等于写端已使用。
- 纯时间戳错误包的 `script_name=None`；带实例过滤时不会命中这些包。助手应单列“归属未确认”，不能把未列出解释为该实例没有错误。
- `normalize_script_name` 在首个下划线处裁剪名称，是既有读取约定。新增诊断关联应保留精确实例身份，展示归一化名另存，不能只用归一化名字关联不同实例。
- server 第三方日志转发最终仍进入 OAS 文本日志；不是所有来源都保留原始 logger/调用点。磁盘文本与 GUI 格式不同，诊断应保存原始证据位置。

## 3. 五个问题：当前能回答到什么程度

| 问题 | 目前结论 | 可用证据 | 必须展示的缺口 |
|---|---|---|---|
| 当前在哪个任务？ | **顶层通常可定位，多账号子任务部分可定位** | Scheduler 开始/结束、模块路径、`[STAT] task_start/task_end` | 无统一执行 ID；嵌套调用/突然退出可能缺边界；待执行任务不是正在执行任务 |
| 当前页面？ | **只能回答最后观察到的页面或局部 Scene** | `[UI]`、Page arrived、局部 Scene、错误 PNG 元信息 | 观察时效、覆盖页、识别帧、未注册场景缺失；计划切页不是已到达 |
| 最近一次点击？ | **通常能找到最近一次点击调用记录** | Click 坐标与资源名、连续点击历史 | 是否发送、后端完成、实际落点、游戏响应、决策截图关联未确认 |
| 为什么等待？ | **部分可回答** | 调度等待时间、导航目标、稳定等待、stuck 集合 | 无统一等待起止、原因分类和预算；节奏/限流/业务循环可能静默 |
| 为什么失败？ | **常能说明直接触发条件，根因不稳定** | 异常类型、消息、traceback、STAT error、截图包 | 任务/页面/动作/等待未完整关联；局部吞异常、归档截断、现场缺失；网络/维护等提示未被证实 |

回答形式应同时给出：值、观察时间、证据位置、证据等级和缺失项。例如“最近点击调用为 X，是否发送未确认”，不能缩写为“最后成功点击 X”。本轮没有读取历史正文，因此没有产出任何具体历史运行的任务/页面/失败结论。

## 4. 历史日志目录结构检查

以下是本轮一次目录快照，运行中的目录可继续增长；数量不是失败次数、任务次数或性能结论。未读取历史文件内容，也未核对图片画面或 log.txt 文本是否完整。

```text
D:\Entertainment\Tool\OAS\
├─ log/
│  ├─ YYYY-MM-DD_<name>.txt
│  ├─ error/
│  │  ├─ <timestamp_ms>/
│  │  │  ├─ log.txt
│  │  │  └─ YYYY-MM-DD_HH-MM-SS-microseconds.png
│  │  └─ 123/                         # 非标准空目录
│  ├─ screenshots/                    # 当前为空
│  └─ annotator/                      # 当前为空
└─ screenshots/
   └─ mshop/                          # 当前历史截图目录

D:\Entertainment\Tool\logs\
├─ task_logs/                         # 当前为空
├─ handoff/OAS/                       # 文档型交接资料，含归档目录
└─ worklog/OAS/                       # AI 工作记录
```

| 检查对象 | 目录元数据确认 | 推断边界 |
|---|---|---|
| OAS/log 根目录 | 49 个文件，均符合按日 `.txt` 命名；文件名日期范围 2026-09-26 至 2026-10-02；合计 62,929,659 字节 | 不代表每个实例连续运行，不证明七天自动清理，也不证明内容覆盖完整日期 |
| OAS/log/error | 183 个子目录；182 个为至少 10 位纯数字时间戳，各有 `log.txt` 和一张 PNG；另一个 `123` 空目录；没有带实例前缀的错误包 | 182 个包不等于 182 个独立根因；无法凭结构归属实例；空目录不推断来源、不删除 |
| OAS/log/screenshots、annotator | 均为空 | 不证明从未使用或已失效 |
| OAS/screenshots/mshop | 99 个 PNG，合计 81,217,509 字节 | 与当前 BaseTask 的 `<任务>_Screenshots` 命名不是同一形状；可能来自其他调用或历史行为，来源未确认 |
| Tool/logs/task_logs | 空目录 | 不是当前 OAS 主文件日志路径 |
| Tool/logs/handoff | 2 个 Markdown 文件、2 个子目录（递归计数） | 是维护资料，不能自动当作游戏运行证据 |
| Tool/logs/worklog | 1 个 Markdown 文件、1 个子目录（递归计数） | 是 AI 工作记录；本轮只核对结构，未读取或追加 |

结构审查揭示三点：错误包缺少实例归属；截图存在多套保存路径；维护记录与运行日志分离。助手应明确证据类别和来源，不移动、统一重命名或迁移这些历史目录。

## 5. 缺失信息与推荐新增字段

### 5.1 第一优先级：少量关联字段

以下是**未来可选诊断 JSONL 的建议字段**，不要求给每一条普通日志、`[STAT]` 或业务配置增加字段。拿不到的值用 `null` 或 `unknown`，不得伪造。

| 字段 | 建议语义 | 如何低侵入取得 |
|---|---|---|
| `v`、`event` | 协议版本与事件类型 | 新诊断写入器定义，独立于 STAT |
| `ts`、`seq` | 带时区 ISO 时间、进程 session 内单调事件序号 | 新写入器生成；时间不替代事件顺序 |
| `instance_id` | 精确实例身份，可用稳定的本地不透明 ID | 实例创建时绑定，不使用被下划线裁剪后的名称作为唯一键 |
| `session_id` | 一次脚本进程生命周期的随机唯一标识 | 进程创建时生成；跨日不变，重启必须变化，PID 可作为辅助元数据 |
| `task_run_id`、`parent_task_run_id` | 一次任务调用及父调用 | 实际执行入口 push，退出 finally pop；不能从 `config.task` 单独推导嵌套任务 |
| `task`、`subtask`、`phase` | 顶层执行任务、当前子任务和业务阶段 | 使用真实参数、STAT 调用点或已有局部状态；没有公共阶段时不强制标准化所有任务 |
| `account_ref` | 当前账号/角色/区服组合的本地匿名关联标识 | 多账号已有身份上下文派生；切号期间区分目标账号与已确认账号，不记录密码或原始配置 |
| `result`、`reason_code` | 结束/失败/等待的观察结果和稳定原因码 | 记录已有分支结果；与业务进度状态分开，不改变原 return/raise |

`seq` 只在同一 `session_id` 内比较；多个进程不建立虚假的全局顺序。`task_run_id` 不能复用为下一次重试，重试关联用额外的父关联或可选 `retry_of`。先覆盖确实经过入口的任务，直接动态构造的子任务须明确覆盖点，不能声称自动捕获所有调用。

### 5.2 第二优先级：五个问题所需的事件字段

| 事件组 | 推荐字段 | 关键语义 |
|---|---|---|
| `page_observed` | `page`、`scene`、`overlay`、`observed_at`、`frame_id`、`recognition_source` | 页面、局部场景和覆盖页分开；来源可为 GameUi、局部 Scene、庭院探测；只记录已有识别结果 |
| `action_attempt/action_end` | `action_id`、`kind`、`resource`、`requested_xy`、`control_method`、`basis_frame_id`、`status`、`duration_ms`、`exception_type` | attempt 是尝试；end 区分后端返回/抛异常；后端返回仍不等于游戏响应；实际坐标未暴露时 `actual_xy=null` |
| `wait_start/wait_end` | `wait_id`、`reason_code`、`target`、`started_at`、`budget_s`、`elapsed_s`、`result` | 原因可为 scheduler_due、page_arrival、target_appear、target_disappear、animation_stable；无局部预算就填 null |
| `failure` | `error_id`、`exception_type`、`message_summary`、`trigger`、`last_action_id`、`active_wait_id`、`last_page_observation`、`recovery_requested` | 先记录触发现场，再由原处理继续；请求 Restart 与实际启动事件分开；根因未证实则 unknown |
| `artifact_index` | `error_id`、`error_dir`、`text_source`、`image_refs`、`frame_times`、`coverage`、`archive_status` | 本地相对路径、帧关联及完整性；区分 disabled、not_attempted、saved、partial、failed、unknown |

失败字段中的 `trigger` 是“哪个条件导致失败”，不是根因。诊断结果需额外保存 `evidence_status=confirmed/inferred/unknown` 和 `source_ref`；旧文本推断与新增直接事件不能混成同一种证据。

不把全量 OCR、模板分数、资源 hash、完整配置快照列为第一阶段必填。RuleImage 普通匹配分数目前主要在 debug_mode 下输出；若以后需要，优先记录已有可用结果，局部按需扩展，不能为了诊断重新匹配或再次调用 OCR。空输出与真实 0、冷却未执行与匹配失败也应分开，但不在本轮修改接口。

## 6. 最小侵入方案

### 6.1 起点：独立本地只读助手

优先实现一个可独立运行的小工具，建议位置为仓库外 `D:\Entertainment\Tool\scripts\utils\OAS_diagnostic_assistant\`（仅设计，未创建）。按项目长期辅助工具规则登记。使用标准文件读取，不 import OAS logger、Script、Device 或配置模型，不启动 GUI/server/OCR/游戏。

输入是用户指定且通过范围检查的实例日志窗口或错误包路径；默认只读，不扫描账号配置、维护 worklog、handoff 或其他项目。历史正文分析需在后续明确授权范围内进行，本轮元数据审查不是读取历史内容的授权。

处理顺序：

1. 枚举文件和元数据，限制文件数量与读取字节；优先从故障附近窗口开始，避免每次读取全部历史。
2. 解析原始文本日期/调用点、START/任务/战斗边界、STAT、UI、Click、等待及异常。合并折行或 Rich traceback 时保留原始行范围，不根据屏幕折行逆推原文。
3. 优先采用结构化事件，普通文本用于补充。旧日志只建立“基于文件边界和时间的推断片段”，不得生成貌似真实的历史 session/run ID。
4. 为五个问题生成摘要和证据引用，列出缺失字段、截断、过期或冲突。未知消息、坏 JSON、半条追加记录不影响读取其他证据。
5. 显示结果；若用户要求保存报告，只写单独输出目录，不向原日志或错误包补写数据。未来可接已有日志读取服务，但不要求先修改 API 或前端。

旧错误包如果没有实例字段，不凭最近时间自动绑定某个实例；时间邻近只能作为候选关联，保留多候选和“未确认”。跨天 START 只作为读取边界，不能一律增加进程 session。没有最新进程状态时，只能输出“日志截至某时刻的状态”，不能声称任务当前仍在运行。

### 6.2 后续可选观测层

在单独获准实施后，新增一个小型诊断上下文/写入器模块，显式调用，默认关闭，故障时只禁用诊断输出。每个实例进程拥有自己的上下文；任务、账号、子任务形成作用域栈，退出时恢复父级，避免多实例、切号或异常退出后串上下文。必要线程边界显式传递，不用一个全局可变“当前任务”字典。

建议事件单独追加到 `OAS/log/diagnostics/<date>_<instance-ref>_<session-id>.jsonl`，每条一行 UTF-8 JSON；不要先把 `[DIAG]` 拼进普通 Rich 日志。新目录不是既有错误目录，不修改旧读端、STAT 或文本格式。跨日只滚动诊断文件，session 不变。该位置不受现有 cleanup 的同一策略覆盖，后续必须为新数据单独设置容量与保留范围，不能擅自扩大原清理函数。

| 可选观测点 | 只记录什么 | 不得改变什么 |
|---|---|---|
| Script 实际任务入口/退出、调度等待边界 | command、调用 ID、返回/异常/退出及调度等待原因 | 动态加载顺序、TaskEnd、任务选择、Restart 排程、OCR 活跃生命周期 |
| GameUi 已有页面识别成功/到达/未知分支 | 原结果、观察时间与对应已有帧 | 页面扫描顺序、overlay、导航、皮肤探测；不额外识别一次 |
| Control 的 click/long_click 边界 | 同一 action ID 的尝试与后端返回/异常 | 原始日志位置、节奏等待、坐标采样、随机数序列、backend/retry、连点和结算手势 |
| 少数公共 wait 接口 | 已存在的开始、返回/超时、目标和预算 | 返回值、计时器、循环、超时和截图频率；不全面拦截 sleep/Timer |
| 异常捕获及错误保存前后 | 保护清理前的现场快照、错误包路径和归档结果 | 原异常类型、消息、传播及恢复策略，不把诊断故障抛入业务 |
| 已有多账号 STAT 调用边界 | 账号匿名引用、子任务调用上下文 | STAT 原 payload/事件、进度标记、切号和登录复用；暂不改通用多账号框架 |

先从必要的任务、页面、点击边界验证收益；等待、失败完整性及多账号观测按下一阶段加入。不是一次性修改上表全部文件。业务局部循环没有观测点的情况继续标 unknown，不为覆盖率重写任务。

诊断层只能读取已取得的标量和快照，不求值会创建 Device/Config 的 cached_property，不调用 `Timer.reached()`、资源 `.coord()`、重新匹配、OCR、截图或新的随机函数。动作日志必须复用原来取到的坐标；可用 `monotonic` 度量诊断耗时，不替换原业务 Timer。

诊断 I/O 使用有界缓冲/有界写入策略，满时按既定策略丢弃并记录缺口，不能阻塞业务等待磁盘。诊断序列化、权限、磁盘满和关闭失败只影响诊断，不改变原 return/raise；告警限频，避免日志递归。关键现场即使丢失也如实报告，不能保证强杀前必然写出 task_end。

### 6.3 错误包关联：先加外部索引，不迁移历史

保留现有 `log/error/<timestamp>/log.txt` 与 PNG 保存方式。将当前错误包 ID、实例、session、task_run、最近动作/等待/帧及归档结果写入独立诊断索引，未来助手据此精确关联。

这样可避免立即改 `save_error_log` 的命名、文本切片、图像脱敏钩子、已有 errors API 或读取响应模型。历史包缺索引就维持旧读取与未知归属；不回填、重命名、移动或自动修复旧目录。若以后考虑 `manifest.json` 或 `<script>_<timestamp>` 命名，作为独立兼容性任务评估，不能借本方案顺手实施。

## 7. 不建议修改的位置与兼容约束

| 位置/行为 | 不建议改动的内容 | 原因 |
|---|---|---|
| module/logger 与 GUI 文本 | formatter、宽度、分隔符、hr/rule、日期滚动、旧文件命名 | 统计解析、错误切片和展示共同依赖；诊断收益不足以支持换框架 |
| `[STAT]`、StatEvent、log_stats | 不替换现有字段/事件，不把诊断事件塞进统计语义 | 已有多账号/战斗统计及历史折行兼容；诊断旁路更易回退 |
| Script 异常分支与恢复 | 不统一改为捕获所有异常，不重排 Restart/退出/恢复 | 诊断要观察当前行为，不改变失败传播和调度策略 |
| Device stuck/click 保护与 humanize | 不改阈值、历史队列、计时器、重试、豁免、节奏与坐标 | 添加一次“只读检查”也可能消费 Timer/RNG 或影响安全操作 |
| GameUi/PageRegistry/主题资源 | 不抽出全新 Navigator/Session，不改识别优先级和素材 | 当前能力已存在；大规模替换与诊断目标无关 |
| GeneralBattle/reward_frame | 不改奖励判据、禁点区、冷却和结算连点 | 安全收尾是生产保护边界；这里只允许复用已确认结果 |
| 多账号核心、ProgressStore、DailyAltAccEx | 不改账号排序、切号、进度状态、接续及屏蔽调度 | 诊断字段不能成为新的进度或恢复依据 |
| ConfigStore/热更新/OCR 底层 | 不改保存、generation、锁、模型生命周期/RPC，不为诊断保存全配置 | 避免诊断引入状态污染、额外识别成本或生产数据变更 |
| 历史日志、截图、外部维护记录 | 不清理、迁移、补写、覆盖或上传 | 原始证据和本轮用户只读约束 |
| OASX、安全更新器、外部参考仓库 | 不修改前端、同步代码或运行更新 | 独立项目/维护边界，与本次文档任务无关 |

兼容验收必须验证：旧日志无需转换即可读取；原普通日志、STAT、分隔块和 errors API 行为保持兼容；关闭或删除新增诊断组件能完全回到原观测能力。新增诊断信息不作为业务成功、进度完成或自动恢复的判断输入。

## 8. 助手输出契约

每份诊断摘要至少包含：

1. **范围与时点：** 分析的实例/候选归属、时间窗口、输入文件、是否只覆盖局部、最新证据时间。
2. **五问回答：** 任务、最后观察页面、最近动作调用/执行状态、已知等待原因、直接失败条件及未确认根因。
3. **证据：** 原始文件相对 OAS 根路径、行号/字节偏移或错误包/图片名；可追溯到原始资料。
4. **完整性：** 开始/结束是否缺失、截断/折行、页面是否过期、截图/索引是否缺失、事件丢弃或归属冲突。
5. **核查建议：** 只建议人工检查或下一轮所需观测，不能自动执行修复。

示例仅为输出语义说明，不是本轮历史日志分析结果：

> 截至 T，顶层任务 A 的开始边界已确认，子任务未确认。最后页面 B 在 T−Δ 被观察到，故障时页面未知。最近 Click 指向资源 X，只有执行前记录，发送及响应未确认。出现目标等待超时，局部预算已记录；根因未知。证据引用到文件与行号；错误包实例归属未确认。下一步人工核对对应时间窗口，或在后续授权中补等待与动作关联。

模型可在以后帮助整理措辞，但规则解析与证据索引应独立工作。无需把日志发往模型服务才能完成 MVP；远程传输不属于本方案默认行为。

## 9. 后续实现路线与验证

各阶段单独授权、小步实施；下面是计划，不是本轮完成项。

| 阶段 | 交付 | 改动范围 | 验收重点 |
|---|---|---|---|
| D0：设计基线（本轮） | 本页、源码能力盘点、历史目录结构快照 | 仅本页 | 五问结论有证据，未知项明确，不触碰业务和历史内容 |
| D1：离线只读 MVP | 有限窗口读取、旧文本/STAT 解析、五问摘要和证据索引 | 仓库外独立辅助工具；优先用人工构造且脱敏的样例 | INFO 与折行、旧 STAT、坏 JSON、截断、无结束、跨天 START、未知实例、TaskEnd 非业务成功都能正确表达 |
| D2：核心可选事件 | session/task 关联、页面观察、click/long_click 尝试与结束 | 新小模块，少量实际任务/GameUi/Control 观测点；默认关闭 | 默认关闭时行为不变；开启后不额外截图/OCR/匹配/采样；嵌套恢复父上下文，多实例不串；动作执行状态不冒充游戏成功 |
| D3：等待与失败现场 | 少数公共等待、多账号作用域、清理前现场、归档外部索引 | 仅证据缺口对应边界，不改变异常/进度规则 | 正常等待、超时、取消、强杀缺结束、无错误包/保存失败均可解释；同一故障重复捕获不被计为独立根因 |
| D4：按需接入展示 | 本地报告入口，必要时复用现有 LogBrowserService 增量接口 | 优先 OAS 侧读取适配；前端作为独立授权任务 | 旧客户端、旧 API、旧统计不受影响；分页/截断/路径边界保留；可完全停用新增功能 |

后续验证应覆盖两类真实风险：

- **行为兼容：** 对同一可控输入，比较诊断开/关的原 return/异常、任务顺序、点击与后端调用、截图/OCR 次数、计时器确认次数、随机序列、STAT 统计结果；磁盘满/无权限/队列满时业务继续原流程。不要靠“诊断函数返回了”证明无侵入。
- **证据正确性：** 将“准备切页但未到达”“点击记录后后端失败”“TaskEnd 但子任务失败”“同名归一化冲突”“跨日但 session 未变”“局部异常已处理”“强制退出缺结束”“单张截图无全过程”做成独立样例，保证助手不报告虚假成功、页面或根因。

先离线验证，再在用户明确安排的受控运行中测量开/关时的延迟、写入量、内存和事件丢弃量；性能预算依据实测设定，不能在本页宣称已达标。已有 60/300 秒 stuck Timer 及业务改写不是可直接套用的诊断超时 SLA。

回退策略是关闭观测开关或停用新增模块与读取入口；原文本、STAT、错误包始终保留。不通过恢复历史日志、迁移进度或更改 ConfigStore 来回退诊断。

## 10. 本轮验证与待确认事项

本轮完成源码与目录元数据静态核查，并检查文档范围与引用；没有运行应用、单元测试、游戏或历史内容解析。目录计数是结构快照，不是运行样本验证。

仍待后续明确授权与验证：实际用户 screenshot_length、现场日志完整性、各任务观测覆盖率、错误包与实例的真实关联、直接构造任务的上下文传播、写入性能、保存失败表现、可用脱敏规则，以及需要接入的具体展示入口。

**最合理的下一步是 D1：用人工构造且脱敏的样例验证离线只读助手，先展示五问摘要的实际价值，再决定是否需要 D2 的少量观测点。** 不以本页作为修改业务代码、修复 bug、读取历史正文或上传日志的授权。
