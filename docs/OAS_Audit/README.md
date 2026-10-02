# OAS Personal 审计与维护入口

整理日期：2026-10-02。适用目录：D:\Entertainment\Tool\OAS。代码核查基线：be4160dffb2615e66f97d20163196a420dbdd68a（Personal main初始快照）；未来任务仍须检查当时真实HEAD与diff。

本目录把聊天中的多账号、探索、绘卷和外部项目比较整合为长期文档，不将计划冒充已实现能力。**以后任何OAS修改任务，先读取本README与根AGENTS.md，再读相关模块页、真实代码和Git状态。**

稳定优先于智能，可维护优先于炫技，小步迭代优先于大重构。当前OAS继续作为生产核心，不整体迁移官方dev或yys。

## 文档索引

| 文档 | 内容 | 什么时候读 |
|---|---|---|
| [00_Project_Context](00_Project_Context.md) | Personal定位、历史来源、核心资产和边界 | 每次接手 |
| [01_Current_Architecture](01_Current_Architecture.md) | 调度、任务、配置、OCR、图片和资源关系 | 理解调用链、共享模块修改 |
| [02_MultiDaily_Audit](02_MultiDaily_Audit.md) | 多账号状态、开关、接续、完成和重复操作 | 日常、切号、进度、恢复 |
| [03_Exploration_Audit](03_Exploration_Audit.md) | 页面、UP、入场、计数、资源不足和退出 | 探索修改 |
| [04_Event_Painting_Audit](04_Event_Painting_Audit.md) | 绘卷联动、捐献、达摩标记和安全结算 | 绘卷、活动、奖励判断 |
| [05_ThirdParty_Comparison](05_ThirdParty_Comparison.md) | 官方/FLAG/yys事实、适配价值和证据缺口 | 上游修复或资源吸收 |
| [06_Optimization_Roadmap](06_Optimization_Roadmap.md) | Phase0–3、优先级、范围、风险和Codex适用性 | 选择下一项实施任务 |
| [07_Future_Maintenance_Strategy](07_Future_Maintenance_Strategy.md) | 每周检查、选择性合并、验证与防覆盖 | 长期维护 |

<a id="sources"></a>
## 来源与证据

用户确认原报告主要在本聊天中，没有提供独立文件。整理时通过对话记录读取正文，而非声称找到四个原始Markdown文件。

源对话：**审计 OAS 多账号日常流程**；线程ID：01a0f69e-4b89-7542-b460-77c901ae1294。现有三份独立审计覆盖四类主题，另有路线图；没有定位到独立FLAG专项报告。

| 编号 | 实际读取来源 | 适用边界 |
|---|---|---|
| S1 | 本对话：多账号日常/Exploration绘卷自身审计；轮次01a0f861-214a-7633-b205-3f9e257a8bb3 | 旧OAS a0db2acf工作区；85项历史离线验证及隔离复现，不等于当前实机通过 |
| S2 | 本对话：《OAS vs yys 深度审计报告》；轮次01a0f86d-957c-7703-8f14-284567e0ca78 | yys固定快照；48项历史突破验证，包含对S1准备态结论的修正 |
| S3 | 本对话：《OAS三方架构进化审计报告》；轮次01a0f88b-849a-7260-b289-49bbbf67e22f | 旧OAS、yys、官方master/dev；不是独立FLAG新版本审计 |
| S4 | 本对话：《OAS Evolution Roadmap》；轮次01a0f8a5-94ab-7fe3-8cf3-5dc79cf2c559 | 规划来源；任务列入不表示已经实现 |
| W1 | D:\Entertainment\Tool\logs\worklog\OAS\AI_WORKLOG.md | 历史来源、上游化、庭院/突破修改与验证记录；时间和基线不得混用 |
| W2 | D:\Entertainment\Tool\logs\handoff\OAS\archive\OAS_WORK_HANDOFF_20260817.md | FLAG/Gitee run_now_2历史来源；不是当前部署状态 |
| C0 | 当前真实代码、Git状态、任务README/plan等 | 本轮只读核对关键事实；没有运行测试或游戏 |
| C1–C3 | 既有官方master/dev和yys源码快照 | 固定源码证据；没有执行外部程序/编译模块 |

### 固定源码快照

| 对象 | 版本 | 本机缓存 |
|---|---|---|
| Personal代码基线 | be4160dffb2615e66f97d20163196a420dbdd68a | D:\Entertainment\Tool\OAS |
| 官方master（原审计分支） | 967a0120b03497eae7ec707557d121606aeb3329 | D:\Entertainment\Tool\cache\codex\official-oas-audit-20261002 |
| 官方dev（原审计分支） | 9ec6101e50bda369cbca13c9ee85f6501ca1533f | D:\Entertainment\Tool\cache\codex\official-oas-dev-audit-20261002 |
| yys run_now | 5b145606a11eb84df89d845b2c007c14de99c0bb | D:\Entertainment\Tool\cache\codex\yys-audit-20261002 |
| FLAG独立对照 | TODO / 需要验证目标SHA及差异 | 本轮未找到独立可确认快照 |

缓存与外部日志不随本目录保证分发；换机后需要补充这些材料。外部来源按仓库URL与SHA确认，不根据本地分支标签推断。

<a id="evidence"></a>
### 证据分级

- **当前静态确认：** 本轮在实际代码/元数据看到对应路径，不保证实机触发频率。
- **历史验证：** 原报告记录的测试或隔离复现，注明来源和基线；不能当本轮再次通过。
- **建议/计划：** 尚未实施的行为或架构方向。
- **TODO / 需要验证：** 功能归属、现场表现、性能、样本或完整报告证据不足。

事实优先级遵循AGENTS.md：真实文件与Git状态、实际验证、工作记录、推测。历史材料与代码冲突时保留历史背景并说明当前差异。

<a id="corrections"></a>
## 整合时的重要修正

1. GeneralBattle正确路径是tasks/Component/GeneralBattle/general_battle.py；旧路线中的tasks/GeneralBattle/GeneralBattle.py不存在。
2. S1对准备态接管的绝对判断被S2修正：部分准备页能由综合战斗判据识别，剩余边界需要验证。
3. 当前已有PageRegistry、页面图、分级热更新、原子ProgressStore、紧邻重试登录复用和回礼等待补差；不能重复列为待新增功能。
4. ProgressStore原子保存与daily_progress共享记录是不同对象；后者仍需跨进程及原子写改进。
5. 双绘卷是选择其中一个活动；达摩UP标记不是白/红/黑目标分类。
6. S1/S2的85/48项可能重叠，不能相加；本轮没有运行这些测试。
7. 旧run_now_2工作区两处修改描述不代表Personal起始状态；当前整理开始时工作区干净。
8. 旧优先级与阶段号统一映射到本路线P0/P1/P2、Phase0–3，事实不随编号变化。
9. 安全更新器存在不证明已适配Personal main；先核查旧分支/manifest/基线约束。

## 当前重点与保护边界

确定问题优先：严格UP配对、主动退战分类、全局九格门槛、金币奖励、最佳尺度尺寸、绘卷最终排程、多账号完成与恢复。

保护ConfigStore、多账号核心调度、OCR底层、安全结算和安全更新。修复边界可以实施，但须明确收益、兼容和验证；不顺带整体重构。

现有[Upstream策略](../Upstream/README.md)将FLAG作为Primary、yys和runhey作为Reference。定位与实测性能分开，不按来源名判断实现质量。

## 后续Codex任务最小流程

1. 读取根[AGENTS.md](../../AGENTS.md)、本入口和相关模块审计。
2. 检查真实文件、Git状态、测试与日志，确认结论仍适用。
3. 提出具体范围、行为变化、风险、验证与回滚。
4. 在已授权范围内小步修改，验证后更新对应文档和worklog。
5. commit/push/PR/生产更新按各自授权执行；文档本身不是授权。

本轮只生成Markdown并按项目规则追加工作记录，没有修改业务代码、账号配置、测试或更新器。已对不确定内容保留TODO。下一项建议是EX-01；上游生产更新前先完成UPD-01。
