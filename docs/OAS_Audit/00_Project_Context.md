# OAS Personal：项目背景与边界

更新日期：2026-10-02。来源：S1–S4、W1、W2及本轮本地 Git 检查；编号见 [README](README.md#sources)。

## 定位

OnmyojiAutoScript-Personal 是以当前 OAS 为生产核心的个人维护版本。目标是保留多账号日常、庭院、动态突破、探索、绘卷、安全结算、配置持久化和 OCR 等已有能力，选择性吸收上游修复与设计。

维护原则：稳定优先于智能，可维护优先于炫技，小步迭代优先于大重构。不整体迁移官方 dev 或 yys，不删除个人功能，不以新增代码数量衡量进步。

## 当前代码与历史来源

| 项目 | 已知事实 | 证据与限制 |
|---|---|---|
| 当前本地项目 | `D:\Entertainment\Tool\OAS` | 本轮检查的真实目录 |
| 当前个人仓库 | `https://github.com/SuAstronaut/OnmyojiAutoScript-Personal.git` | 本地 origin；不是本轮新增的远端 |
| 整理开始时的分支与代码基线 | main；`be4160dffb2615e66f97d20163196a420dbdd68a` | 提交说明为 Initial commit: OAS baseline；当时本地可达历史为 1 个提交，后续文档提交不作为业务代码基线变化 |
| 历史直接维护来源 | FLAG12321 的 run_now_2 | W1/W2 记录曾使用其 GitHub/Gitee 来源；S3 也确认旧 OAS 与官方的谱系关系 |
| 更早的框架来源 | runhey/OnmyojiAutoScript；其 README 说明继承 Alas 思想 | 当前项目 README 和 S3 |
| yys | 与 OAS 高度同源的参考项目 | S3 未找到其审计分支与官方的共同 Git 祖先；不能断言平台 fork 关系 |

历史审计使用旧 OAS HEAD `a0db2acf` 及当时未提交的庭院、突破修改。本轮 Personal 基线已变化，起始工作区干净。旧报告中的“当前脏文件”不是本轮状态，也不是恢复旧版本的依据。

**需要验证：** Personal 初始快照与 FLAG 某个提交之间的逐文件差异、是否保留可用共同祖先、哪些增强已经上游化。单个初始提交和当前 origin 不能独立证明每个功能的作者或分叉位置。

## 为什么建立 Personal 分支

1. 将自己的生产版本、改动理由和验证记录留在可维护的仓库中。
2. 避免更新时直接覆盖个人功能、账号配置和运行数据。
3. 将多来源的参考设计转成小范围、可审查、可回滚的改动。
4. 让后续 Codex 任务先读取统一文档，减少重复审计和依赖聊天记忆。

这不是将三个来源合并成一个不断同步的镜像。每项外部变化仍须判断接口、资源、配置和运行行为是否适合当前 OAS。

## 核心资产

| 资产 | 需要保留的能力 |
|---|---|
| MultiDailyAltAcc / DailyAltAcc | 账号排序、总开关与账号开关、阶段计划、逐任务进度、切号和恢复 |
| 庭院与登录 | 页面与遮挡判断、奖励优先、连续状态确认、已有登录适配 |
| RealmRaid | 动态槽位、三态识别、最后未击破目标九退四、正向入场确认 |
| Exploration / MemoryScrolls | 章节容错、目标关联、退出补领、双绘卷和个人进度策略 |
| GeneralBattle | 奖励区域、禁点区、共享点击冷却及已有安全收尾 |
| ConfigStore / 热更新 | 配置身份、校验、合并保存及 HOT/WARM/COLD 边界 |
| OCR | 已有 RapidOCR/PP-OCRv6、本地/RPC 接口及模型生命周期 |
| 安全更新辅助工具 | 候选验证、受保护运行数据和失败恢复设计 |

上述能力“存在”不表示全链路已经实机验证。各模块问题和验证边界见对应审计。

## 仓库与运行环境边界

- OASX 是独立项目，不能因关联而自动修改或同步。
- 个人账号 JSON、任务计划、进度记录、配置 generations 和 toolkit 属运行环境；不能混入外部代码迁移。
- 顶层 assets 与任务内游戏资源职责不同，不能整体覆盖。
- 安全更新辅助工具位于仓库外的 `D:\Entertainment\Tool\scripts\OAS_local_patch`。其 README 仍包含旧 run_now_2/目标基线约束，**需要验证是否适配 Personal main；不能直接执行旧 cutover 流程**。
- 本轮只生成文档，没有添加上游远端、调整更新配置或运行更新器。

## 文档维护方式

文档保存历史审计事实、当前静态证据、建议和 TODO。代码修改后只更新受到影响的结论，注明基线、验证范围和仍未确认事项。

入口：[README](README.md)。实施顺序：[优化路线](06_Optimization_Roadmap.md)。长期跟踪和合并：[维护策略](07_Future_Maintenance_Strategy.md)。
