# 官方 / FLAG12321 / Gitee yys：第三方对比

更新日期：2026-10-02。来源：S2/S3、W1/W2及既有外部源码快照；[完整来源登记](README.md#sources)。对比的是固定版本和已读证据，不代表远端未来状态。

## 1. 来源与证据边界

| 来源 | 地址/版本 | 已确认范围 |
|---|---|---|
| runhey 官方 master | [仓库](https://github.com/runhey/OnmyojiAutoScript)；967a0120b03497eae7ec707557d121606aeb3329 | S3审计主分支与本地固定快照 |
| runhey 官方 dev | [固定树](https://github.com/runhey/OnmyojiAutoScript/tree/9ec6101e50bda369cbca13c9ee85f6501ca1533f) | GameUi分层和对应任务适配 |
| FLAG12321 | [GitHub](https://github.com/FLAG12321/OnmyojiAutoScript)、[Gitee](https://gitee.com/flag12321/OnmyojiAutoScript)；历史 run_now_2 | W1/W2历史生产来源，S3谱系结论；本轮无独立新快照 |
| Gitee yys | [固定树](https://gitee.com/pattering-rain/yys/tree/5b145606a11eb84df89d845b2c007c14de99c0bb)；run_now | S2/S3及可读Python源码；部分执行/设备层为pyd |

S3 当时核实官方生产分支叫 master，没有 main。每次维护重新核实；Personal 的 main 与官方历史分支名不是一回事。

旧审计确认旧 OAS 与官方有共同祖先 d98f6af6，并记录 FLAG 为官方 fork。该结论属于旧仓库；Personal 的单次初始快照不能继承这些 Git 关系证明。yys 高度同源，但精确 Git 分叉和平台 fork 关系未确认。

**FLAG 缺口：** 用户确认报告在本聊天中。已取得的独立报告是自身审计、yys对比、官方master/dev对比和Roadmap；没有定位到独立 FLAG 专项正文及对应新 SHA。因此只整理有证据的历史来源和上游化记录，不虚构其专属新功能、性能或当前合并难度。

## 2. 各项目角色、优势与不足

| 来源 | 架构特点 | 有证据的优势/价值 | 局限与合并难度 |
|---|---|---|---|
| 官方 master/dev | 通用任务、Rule资源、Pydantic；dev主要拆分导航 | 任务与活动参考，图片尺寸保护，动画ROI，生命周期与导航职责思想 | 个人配置/OCR/安全点击接口已分叉；完整合并风险高 |
| FLAG12321 | 历史 run_now_2 作为直接维护基础；同一 OAS 风格 | W1记录 task-plan 等公共实现已上游化；与历史生产分支关系近 | 当前独立版本差异需要验证；不能把 Personal 全部增强归为 FLAG |
| Gitee yys | 相近task/module结构，模板组和mask；部分核心编译封装 | 排名/积分捐献、UP严格配对、容量校验、活动上下文思路 | 内部pyd不透明；OCR/超额/期限/错图等风险；局部中等，整体高风险 |
| 当前 Personal | 保留多账号、安全配置、OCR升级、动态突破、安全结算 | 生产业务与个人环境适配已有资产 | 仍有确定正确性缺陷；缺统一性能基准和部分结果契约 |

FLAG 角色还可参照现有 [Upstream 策略](../Upstream/README.md)。其中 yys“低资源识别”等是参考方向，不是本轮实测结论。

## 3. 模块对照

| 模块 | 当前 Personal | 官方审计快照 | yys审计快照 | FLAG证据状态 |
|---|---|---|---|---|
| 多账号日常 | MultiDailyAltAcc / DailyAltAcc /共享进度 | 未见等价完整日常体系 | 多种切号模式，部分内部不可读 | task-plan历史上游化有W1记录；当前逐项归属TODO |
| 配置 | ConfigStore、generation、三方合并、分级热更新 | 相对简单整模型方式 | 旧式序列化/重载 | 当前对应版本需要验证 |
| OCR | RapidOCR/PP-OCRv6、本地/RPC、延迟加载/释放 | 旧TextSystem等方式 | ONNXPaddleOcr | 独立当前版本需要验证 |
| GameUi | 现有注册/页面图/overlay/个人登录 | dev拆Registry、Matcher、Action、Session、Navigator | 相近旧导航 | Personal历史基础不能替代当前FLAG差异 |
| Exploration | 章节容错、窄ROI、横向关联；UP仍可错误降级 | 相似UP问题，不能整体替换 | 无关联返回None；total容量校验 | 当前上游是否已有修复TODO |
| MemoryScrolls | 双活动选择、进度策略；排程与通知耦合 | 普通排程、独立关闭；仍有边界 | 排名、积分、定额和增量反馈 | “FLAG绘卷优势”无独立证据，不列为成熟能力 |
| RealmRaid | 动态格子/三态、最后目标九退四、正向入场 | 固定区域等旧方式，新BattleWait接入 | 固定区域、退战模式和活动门控 | 具体个人增强是否上游化TODO |
| 结算 | 安全框/禁点区/冷却；金币门控缺口 | 新旧战斗并存 | 模板组，含胜败错图 | 需以目标SHA逐函数比较 |
| 资源 | 任务内图片/JSON生成、主题局部替换 | 相同基础；dev主要导航资源调整 | Group/mask及部分新构造参数 | 新主题/活动资源按真实文件验证 |

## 4. 官方架构思想

[dev GameUi](https://github.com/runhey/OnmyojiAutoScript/tree/9ec6101e50bda369cbca13c9ee85f6501ca1533f/tasks/GameUi)拆分页面定义、登记、条件组合、动作、导航和会话。可借鉴当前页优先、限制候选、关键两帧确认、失败路径诊断和状态局部化。

边界：timeout可被进展重置，不是硬总期限；会话副本可能仍共享识别器/动作；同帧缓存接口不等于完整缓存实现；Personal的overlay、庭院纠正和独立登录窗口没有直接等价替代。

新 BattleWait 在 master/dev 均存在，不能描述为 dev 独占或全项目已迁移。可借鉴 setup/prepare/success/failure/completion 等阶段与任务终态判据；默认完成条件、内层超时和类级状态仍需适配，不能覆盖现有安全点击。

## 5. yys 功能思想

| 设计 | 具体位置 | 借鉴方式与难度 |
|---|---|---|
| 排名/积分门控 | tasks/MemoryScrolls/config.py、script_task.py | 默认关闭的新配置；有效OCR和增量验证，中等 |
| UP关联失败拒绝 | tasks/Exploration/base.py::search_up_fight | 只吸收空结果约束，低 |
| 突破券容量校验 | 同文件票数检查 | 借业务约束，真实容量和旧配置需验证，低 |
| 挑战/胜利统计、再次挑战 | tasks/RealmRaid/script_task.py | 区分退战结果，短路径须有期限和回列表，中等 |
| 活动目标上下文 | RealmRaid活动模板/等级/位置门控 | 仅活动成立后启用；等级20不等于可靠身份，中等 |
| RuleImageGroup/mask | module/atom/image.py | 少量候选与缓存mask；构造、分派、点击均要适配，中等 |

不迁移：编译执行/设备层、旧OCR后端、整套RealmRaid run_2/run_3、无期限循环、固定启用依赖任务和全部资源组。

## 6. 图片与资源风险

- IMG-01：Personal最佳位置与最后尺度尺寸错配；官方也有类似问题，不能靠同步整文件修复。
- IMG-02：Personal动画首帧全图，官方首帧ROI；适合局部吸收。
- 官方的普通/每尺度尺寸保护值得参考，但多尺度不能被过早尺寸检查挡住有效下采样。
- yys I_WIN_4/I_FALSE_4 同指向失败图片；增加模板前验证胜败互斥。
- 外部 RuleImage 新 path、Group 类型与 Personal 点击分派不同，识别成功不等于动作可用。
- Main15/Main17 等主题编号已冲突，必须按名称和稳定身份映射。
- Personal部分默认主题分支没有还原共享资源；复用现有快照思路，验证非默认→默认及账号切换。
- 三方未发现可直接采用的完整活动/主题/账号资源分层解析器，属于后续建设。

S1还记录过I_M_PICTURE、I_SUMMON_UP_TO_SHARE重复定义以及旧模板缺失；当时未发现缺图的有效消费路径，不能据此认定正在卡死。S2所选核心资源检查也不能代替全仓扫描。**TODO：** 当前生成资产的重复名、废弃资源和有效消费路径校验；不为补齐历史列表而无条件补图。

## 7. 性能对比的正确用法

审计未做同设备CPU、内存和耗时基准。默认截图节奏相近；新排名/积分会增加OCR，dev关键双帧会增加截图，候选限制则可能减少匹配。

Personal空闲释放可降低闲置内存，但连续短任务可能重复冷加载。旧后端、更多线程或常驻模型不能直接证明更快。

先测截图/OCR/匹配次数、冷/热加载、等待/重试、阶段耗时、闲置/峰值内存。按局部ROI、按需OCR、同帧缓存和变化日志优化，不以库名或模板数量排名。

## 8. 吸收选择与TODO

| 类别 | 建议 |
|---|---|
| 思想优先 | 严格筛选、集中收尾、有效数字校验、阶段/终态、有限恢复 |
| 有限适配 | ROI/尺寸保护、排名/积分、少量模板、活动资源 |
| 暂不迁移 | 整体导航/战斗、配置/OCR底层、编译组件、整树主题资源 |

TODO：补FLAG独立目标SHA/报告证据；核实Personal精确基线；验证主题和活动样本；建立统一性能数据；检查外部新版本是否已修历史问题。

继承关系、功能归属和合并难度不确定时保留“需要验证”，不能根据相似类名、本地分支标签或聊天意向作事实判断。
