# OAS 新版弹窗定向适配与离线回归报告

日期：2026-10-10（Asia/Shanghai）。执行者：Codex。项目：D:/Entertainment/Tool/OAS。

## 结果与范围

完成 10 张真实截图的只读审计、证据充分的资源修复和离线回归。最终 124 项相关测试全部通过（新增 6 项 + 既有九退四/实图回归 118 项），无跳过。没有连接游戏、实例化 Device、启动/停止模拟器或游戏，没有执行购买、刷新或勾玉消耗，也没有启动 OAS 日常、重启 OAS、修改 config/、暂存、提交、推送或创建 PR。

基线为 main/77c8c1f5f1ceaac7249c84dec1a970bf80dde1f0。开工已有 16 个修改文件和 10 个未跟踪文件，本轮结束全部 26 个文件 SHA256 保持一致。AGENTS.md 两阶段规则已阅读；本次用户明确授权审计后直接执行有证据的最小修复，所以范围内没有再次等待确认。

## 1. 截图核验

七张 MuMu 图片均为 RGBA，三张桌面客户端图片为 RGB；全部能完整解码且为 1280×720。识别输入统一转换为 RGB，符合 RuleImage.load_image 的通道约定。来源按用户给出的文件及实图内容确认，没有操作截图来源设备。

| 编号 | 来源/状态 | 原始截图 |
|---|---|---|
| A | 模拟器/邮件全部领取确认 | [MuMu-20261010-111335-040.png](D:/Documents/MuMu共享文件夹/Screenshots/MuMu-20261010-111335-040.png) |
| B | 模拟器/邮件获得奖励汇总 | [MuMu-20261010-111342-717.png](D:/Documents/MuMu共享文件夹/Screenshots/MuMu-20261010-111342-717.png) |
| C | 模拟器/退出战斗 | [MuMu-20261010-111359-085.png](D:/Documents/MuMu共享文件夹/Screenshots/MuMu-20261010-111359-085.png) |
| D_off | 模拟器/再次挑战未勾选 | [MuMu-20261010-111403-197.png](D:/Documents/MuMu共享文件夹/Screenshots/MuMu-20261010-111403-197.png) |
| D_on | 模拟器/再次挑战已勾选 | [MuMu-20261010-111406-133.png](D:/Documents/MuMu共享文件夹/Screenshots/MuMu-20261010-111406-133.png) |
| E | 模拟器/退出组队面板 | [MuMu-20261010-111421-376.png](D:/Documents/MuMu共享文件夹/Screenshots/MuMu-20261010-111421-376.png) |
| F | 模拟器/神秘商店刷新 | [MuMu-20261010-111612-409.png](D:/Documents/MuMu共享文件夹/Screenshots/MuMu-20261010-111612-409.png) |
| C_pc | 桌面/退出战斗 | [desktop_client_20261010_111749_364249.png](D:/Entertainment/Tool/OAS/screenshots/desktop_capture/desktop_client_20261010_111749_364249.png) |
| D_pc | 桌面/再次挑战未勾选 | [desktop_client_20261010_112003_194511.png](D:/Entertainment/Tool/OAS/screenshots/desktop_capture/desktop_client_20261010_112003_194511.png) |
| E_pc | 桌面/退出组队面板 | [desktop_client_20261010_111831_853928.png](D:/Entertainment/Tool/OAS/screenshots/desktop_capture/desktop_client_20261010_111831_853928.png) |

## 2. 五组 UI 审计及处理结论

### 邮件（A 与 B 分别处理）

源码：[tasks/DailyAltAcc/mail.py](D:/Entertainment/Tool/OAS/tasks/DailyAltAcc/mail.py)；共用奖励处理：[tasks/DailyAltAcc/utils.py](D:/Entertainment/Tool/OAS/tasks/DailyAltAcc/utils.py)；资源声明：[tasks/DailyAltAcc/assets.py](D:/Entertainment/Tool/OAS/tasks/DailyAltAcc/assets.py) 和 [tasks/DailyAltAcc/mail/image.json](D:/Entertainment/Tool/OAS/tasks/DailyAltAcc/mail/image.json)。

- A：原 `mail_m_ensure_get.png` 仍能匹配，得分 0.962961（阈值 0.8），实际定位 `(728,553,76,40)` 位于确定按钮内，保留图片。旧 `mail_m_page_get.png` 仅 0.371730（阈值 0.8），确认失效；用 A 的固定“全部领取”文字替换，前置区域从 `(554,48,195,51)` 改为 `(544,58,167,46)`，搜索区域 `(539,33,225,88)` 保持不变。将标题作为领取确定资源的 guard，避免只凭确定文字确认。
- B：原 `I_M_AWARD` 对 B 仅 0.233511（阈值 0.7），旧资源实际尺寸为 325×100，声明与真实图片尺寸原本就不同。该资源被 `get_award_daliy()` 广泛使用，不能因邮件截图失配覆盖所有奖励场景。因此保留原图片，新增 `mail_m_batch_award.png`，只在邮件业务里处理新版批量汇总。
- 邮件收取流程在共用奖励处理和返回按钮之前识别 B，再点击空白区域 `(940,560,120,40)` 关闭；区域在奖励面板右下方、邮件底部操作按钮上方，不覆盖奖励物品和返回 X。截图中背景返回 X 仍可匹配（0.9925），必须优先处理覆盖弹窗，避免把 B 当作正常邮件页返回。
- 原始一键领取 `RestartAssets.I_HARVEST_MAIL_ALL` 在 A 为 0.9947，邮件返回 X 在 A/B 为 0.9928/0.9925，均保留。A 与 B 不相互命中新加入的状态识别。
- 没有邮件桌面截图；桌面兼容性仅通过 RGB 输入、固定 720p 区域及现有架构分析，未做桌面邮件实图验证。清洁邮件页及庭院也未提供。

### 战斗退出（C、C_pc）

源码及消费方：[tasks/Component/GeneralBattle/general_battle.py](D:/Entertainment/Tool/OAS/tasks/Component/GeneralBattle/general_battle.py)、[tasks/RealmRaid/script_task.py](D:/Entertainment/Tool/OAS/tasks/RealmRaid/script_task.py)，并审计 ActivityShikigami、Duel、EvoZone、MasterDisciple、AbyssShadows 等 `I_EXIT_ENSURE` 引用。资源：[tasks/Component/GeneralBattle/gb/image.json](D:/Entertainment/Tool/OAS/tasks/Component/GeneralBattle/gb/image.json) 和 [tasks/Component/GeneralBattle/assets.py](D:/Entertainment/Tool/OAS/tasks/Component/GeneralBattle/assets.py)。

- 旧 `exit_ensure.png` 在模拟器/桌面只有 0.579820/0.616590（原阈值 0.8），确认失效。直接替换为 C 截图的“确定”固定文字 `(720,418,70,39)`，原搜索区域 `(661,371,155,100)` 不变。
- 新增 `exit_dialog.png`，只取“确认退出战斗吗？”正文 `(537,302,209,31)`，独立搜索 `(533,298,217,39)`，阈值 0.9。按钮阈值仍为原值 0.8。两个条件都匹配才返回 True，标题坐标不参与点击。
- 两端均复用同一套资源。桌面按钮得分 0.975235，专属正文 0.941263；没有降低全局或旧按钮阈值。
- `I_EXIT` 在两端依旧有效（0.9458/0.9117）；失败模板在再次挑战弹窗后方仍能识别（0.8918/0.8898），这种背景命中不能当成弹窗已消失，既有九退四专属标题及等待逻辑保留。
- 未改 GeneralBattle 战斗退出/失败结算方法、RealmRaid 业务规则、锁定恢复、退四计数和第五次正常战斗规则。

### 再次挑战（D_off、D_on、D_pc）

源码：[tasks/RealmRaid/script_task.py](D:/Entertainment/Tool/OAS/tasks/RealmRaid/script_task.py)；资源：[tasks/RealmRaid/assets.py](D:/Entertainment/Tool/OAS/tasks/RealmRaid/assets.py)、[tasks/RealmRaid/res/ocr.json](D:/Entertainment/Tool/OAS/tasks/RealmRaid/res/ocr.json)。

- 现有 OCR 区域仍有效：重试按钮 `(805,540,125,45)`、弹窗标题 `(548,265,190,37)`、确定文字 `(708,403,102,60)`；现有过滤要求置信度 ≥0.90、明确文字、唯一局部框、框在 ROI 内、确定位于标题之下。全部保留。
- 现有复选框 off/on 图片继续使用 `(537,341,38,38)` 前置区域、`(533,337,46,46)` 搜索区域及 0.9 阈值；本轮实际匹配位于 `(537,342,38,38)`，无需修改。未勾选/已勾选分别为 0.998553/0.995915，桌面未勾选为 0.997305，反向状态不匹配。
- 标题 OCR 在两张模拟器截图为“是否再次挑战?”，0.939780；桌面为相同文字，0.959150。代码去标点后接受“是否再次挑战”。确定 OCR 模拟器/桌面为“确定”，0.999960/0.999980。局部框及下方关系有效。
- 实际 OCR 确定框为模拟器 `(717,417,72,41)`，桌面 `(717,418,72,40)`；既有 `_realm_retry_click` 点击文字内部的中间一半，分别是 `(735,427,36,20)` / `(735,428,36,20)`，不会误点复选框。
- 复选框点击本来就是既有业务意图：off 时先点复选框、等新截图确认 on，再确认；on 时只确认；unknown 按既有保守分支处理。本轮没有主动改变这项行为。像素接入九退四离线状态机验证 off 仅切换一次、on 零切换。
- 三张图都会匹配身后的失败页与“再次挑战”字样；专属弹窗标题和状态机优先级用于区分这些重叠状态，未将按钮文字单独当成重试确认。
- 未提供桌面已勾选截图，不能声称桌面 on 已通过真实截图验证。

### 组队退出（E、E_pc）

源码：[tasks/Component/GeneralInvite/general_invite.py](D:/Entertainment/Tool/OAS/tasks/Component/GeneralInvite/general_invite.py)、[tasks/Component/GeneralRoom/general_room.py](D:/Entertainment/Tool/OAS/tasks/Component/GeneralRoom/general_room.py)、[tasks/BondlingFairyland/general_invite.py](D:/Entertainment/Tool/OAS/tasks/BondlingFairyland/general_invite.py)、[tasks/BondlingFairyland/script_task.py](D:/Entertainment/Tool/OAS/tasks/BondlingFairyland/script_task.py)、[tasks/GameUi/page.py](D:/Entertainment/Tool/OAS/tasks/GameUi/page.py)、[tasks/GameUi/game_ui.py](D:/Entertainment/Tool/OAS/tasks/GameUi/game_ui.py)。

- 旧 GeneralInvite `gi_gi_sure.png` 在 E/E_pc 仅约 0.0451/0.0442；GeneralBattle 的通用 `gb_confirm.png` 在这两图仅约 0.4788/0.4902，均不能处理该新版组队退出弹窗。但 `I_GI_SURE` 还用于接受邀请等其他确认，不覆盖它，也不改变通用 `I_CONFIRM`。
- 新增 `gi_exit_dialog.png`，固定正文 `(510,302,260,31)`、搜索 `(506,298,268,39)`、阈值 0.9；新 `I_GI_EXIT_ENSURE` 复用战斗“确定”图片和按钮搜索区，使用标题 guard。没有复制桌面/模拟器两套按钮。
- `exit_room()` 只在退出流程使用新专属资源，邀请流程保留原 `I_GI_SURE`；可以从已经弹出的专属确认框继续。`exit_team()` 在读取背景页面是否离开前处理专属确认框，避免遮罩背景模板失配导致提前返回。
- `page_room` 导航确认出边与未知页面关闭列表采用专属资源，只有“确认退出组队面板吗？”及按钮同时命中才确认；战斗退出、再次挑战、商店弹窗不会触发这项退出操作。
- 两端按钮位置相同；专属正文模拟器 1.000000、桌面 0.978399，按钮桌面 0.975235；不区分设备搜索区域。
- 共用同心队资源 `I_EXIT3` 仍有效且会命中多种“确定”。进一步检查发现它还被 `tasks/DailyAltAcc/page.py` 的同心集结退出页面使用，该页面已有“同心队/是否退出集结”OCR 安全约束。本轮撤回对 `I_EXIT3` 加组队标题的尝试，最终该图片、声明和同心队代码均原样保留，不能用 E 的标题破坏另一场景。

### 神秘商店刷新（F）

源码：[tasks/DailyAltAcc/mshop.py](D:/Entertainment/Tool/OAS/tasks/DailyAltAcc/mshop.py)；资源：[tasks/DailyAltAcc/mshop/image1.json](D:/Entertainment/Tool/OAS/tasks/DailyAltAcc/mshop/image1.json) 和 [tasks/DailyAltAcc/assets.py](D:/Entertainment/Tool/OAS/tasks/DailyAltAcc/assets.py)。

- 原 `mshop_ms_ensure.png` 仍有效，0.898610（阈值 0.8），位置 `(713,415,81,43)`；保留图片及区域。
- 但旧按钮还命中 C、D、E（模拟器 0.898610、桌面约 0.934453），确认存在按钮单独判断的假阳性。新增 `mshop_ms_refresh_dialog.png`，从 F 取完整固定刷新正文与 x5 勾玉标志 `(450,295,380,45)`，搜索 `(446,291,388,53)`、阈值 0.9；用 guard 限制 `I_MS_ENSURE` 只接受 F。
- 现有刷新策略、配置开关、重试次数、货物扫描/购买业务没有改动。`MysteryShop/script_task.py` 的常规好友商店购买走另外的 Mall 链，不在本轮替换范围。
- 仅在内存假设备记录预期点击坐标，未运行 `run_mysteryshop()`，没有实际确认、购买或消耗。
- 无桌面 F 截图，桌面商店实图识别及真实刷新效果均未验证。

## 3. 实际修改文件

### 替换图片（2 张）

- [tasks/Component/GeneralBattle/gb/exit_ensure.png](D:/Entertainment/Tool/OAS/tasks/Component/GeneralBattle/gb/exit_ensure.png)
- [tasks/DailyAltAcc/mail/mail_m_page_get.png](D:/Entertainment/Tool/OAS/tasks/DailyAltAcc/mail/mail_m_page_get.png)

### 新增图片（4 张）

- [tasks/Component/GeneralBattle/gb/exit_dialog.png](D:/Entertainment/Tool/OAS/tasks/Component/GeneralBattle/gb/exit_dialog.png)
- [tasks/Component/GeneralInvite/gi/gi_exit_dialog.png](D:/Entertainment/Tool/OAS/tasks/Component/GeneralInvite/gi/gi_exit_dialog.png)
- [tasks/DailyAltAcc/mail/mail_m_batch_award.png](D:/Entertainment/Tool/OAS/tasks/DailyAltAcc/mail/mail_m_batch_award.png)
- [tasks/DailyAltAcc/mshop/mshop_ms_refresh_dialog.png](D:/Entertainment/Tool/OAS/tasks/DailyAltAcc/mshop/mshop_ms_refresh_dialog.png)

全部来自本次提供的原始截图，未合成/重绘；裁切不包括奖励物品、人物、聊天或游戏背景。原图来源及 SHA256 位于 before.json/after.json。

### 资源声明 / 匹配区域

- [tasks/Component/GeneralBattle/gb/image.json](D:/Entertainment/Tool/OAS/tasks/Component/GeneralBattle/gb/image.json)
- [tasks/Component/GeneralBattle/assets.py](D:/Entertainment/Tool/OAS/tasks/Component/GeneralBattle/assets.py)
- [tasks/Component/GeneralInvite/gi/image.json](D:/Entertainment/Tool/OAS/tasks/Component/GeneralInvite/gi/image.json)
- [tasks/Component/GeneralInvite/assets.py](D:/Entertainment/Tool/OAS/tasks/Component/GeneralInvite/assets.py)
- [tasks/DailyAltAcc/mail/image.json](D:/Entertainment/Tool/OAS/tasks/DailyAltAcc/mail/image.json)
- [tasks/DailyAltAcc/mshop/image1.json](D:/Entertainment/Tool/OAS/tasks/DailyAltAcc/mshop/image1.json)
- [tasks/DailyAltAcc/assets.py](D:/Entertainment/Tool/OAS/tasks/DailyAltAcc/assets.py)

没有修改 OCR 区域、OCR 置信度要求或全局匹配阈值。原搜索区域保留，新增标题使用小范围局部区域。

### 必要识别支持与业务判断

- [module/atom/image.py](D:/Entertainment/Tool/OAS/module/atom/image.py)
- [dev_tools/assets_extract.py](D:/Entertainment/Tool/OAS/dev_tools/assets_extract.py)
- [tasks/DailyAltAcc/mail.py](D:/Entertainment/Tool/OAS/tasks/DailyAltAcc/mail.py)
- [tasks/Component/GeneralInvite/general_invite.py](D:/Entertainment/Tool/OAS/tasks/Component/GeneralInvite/general_invite.py)
- [tasks/Component/GeneralRoom/general_room.py](D:/Entertainment/Tool/OAS/tasks/Component/GeneralRoom/general_room.py)
- [tasks/BondlingFairyland/general_invite.py](D:/Entertainment/Tool/OAS/tasks/BondlingFairyland/general_invite.py)
- [tasks/BondlingFairyland/script_task.py](D:/Entertainment/Tool/OAS/tasks/BondlingFairyland/script_task.py)
- [tasks/GameUi/page.py](D:/Entertainment/Tool/OAS/tasks/GameUi/page.py)
- [tasks/GameUi/game_ui.py](D:/Entertainment/Tool/OAS/tasks/GameUi/game_ui.py)

`RuleImage` 仅新增可选 `guard` 参数：`match()` 先识别专属正文，再匹配按钮；未配置 guard 的资源继续原算法。guard 使用独立的 RuleImage，保持按钮坐标及按钮阈值覆盖与正文阈值独立。资源提取器增加两行可选参数生成支持，避免再生成 assets.py 后丢失保护。此次消费者均调用单尺度 `match()`；未对 `match_all` / `match_multi_scale` 另建判断系统，也未声称这些未使用路径已具备 guard 约束。

### 新增测试及报告

- [dev_tools/test_popup_ui_20261010.py](D:/Entertainment/Tool/OAS/dev_tools/test_popup_ui_20261010.py)
- [docs/ui_popup_adaptation_20261010.md](D:/Entertainment/Tool/OAS/docs/ui_popup_adaptation_20261010.md)

保留未修改：原 `mail_m_ensure_get.png`、原 `mail_m_award.png`、邮件领取/返回图片、通用 `gb_confirm.png`、`gi_gi_sure.png`、`mshop_ms_ensure.png`、`I_EXIT3` 及所有 RealmRaid 资源和业务代码。DailyAltAcc/utils.py 的共用奖励处理也未修改。

## 4. 逐张离线识别结果

表内模板分数为 cv2.TM_CCOEFF_NORMED 最大匹配值，不是概率；判定为实际 `RuleImage.match()` 的结果。目标匹配通过后点击采样区域已验证整体包含于真实按钮边界；每次再抽样 50 个原 `coord()` 落点，均位于按钮内。标题 guard 独立匹配，失败时调用者即使传 `threshold=-1` 也无法绕过正文限制。

| 截图 | 目标 | 模板分数 / 正文分数 | 实际定位 x,y,w,h | 结论 |
|---|---|---|---|---|
| A | `I_M_ENSURE_GET` | 0.962961 / 1.000000 | `728,553,76,40` | 通过；专属正文保护 |
| B | `I_M_BATCH_AWARD` | 1.000000 | `519,116,239,65` | 通过；先关闭奖励再返回邮件 |
| C | `I_EXIT_ENSURE` | 1.000000 / 1.000000 | `720,418,70,39` | 通过；专属正文保护 |
| D_off | `I_REALM_RETRY_NO_REMIND_OFF` | 0.998553 | `537,342,38,38` | 通过；原有 OCR 标题/确定也通过 |
| D_on | `I_REALM_RETRY_NO_REMIND_ON` | 0.995915 | `537,342,38,38` | 通过；原有 OCR 标题/确定也通过 |
| E | `I_GI_EXIT_ENSURE` | 1.000000 / 0.999999 | `720,418,70,39` | 通过；专属正文保护 |
| F | `I_MS_ENSURE` | 0.898610 / 1.000000 | `713,415,81,43` | 通过；专属正文保护 |
| C_pc | `I_EXIT_ENSURE` | 0.975235 / 0.941263 | `720,418,70,39` | 通过；专属正文保护 |
| D_pc | `I_REALM_RETRY_NO_REMIND_OFF` | 0.997305 | `537,342,38,38` | 通过；原有 OCR 标题/确定也通过 |
| E_pc | `I_GI_EXIT_ENSURE` | 0.975235 / 0.978399 | `720,418,70,39` | 通过；专属正文保护 |

十图交叉测试：`I_M_ENSURE_GET` 仅 A；`I_M_BATCH_AWARD` 仅 B；`I_EXIT_ENSURE` 仅 C/C_pc；`I_GI_EXIT_ENSURE` 仅 E/E_pc；`I_MS_ENSURE` 仅 F。非 D 截图没有被 `_realm_retry_text` 接受为“是否再次挑战”。退出战斗与退出组队互斥；邮件确认与奖励分离；商店刷新与普通确定分离。原有共用按钮仍会出现单模板假阳性，不能把这些按钮当成新的业务判据。

### 实际调用链及流程测试

新增 6 项测试：

1. 十图完整匹配矩阵及真实局部 OCR（含原后处理和置信度筛选）。
2. 读取生产 BaseTask.appear / appear_then_click / RuleImage.match / coord 实现，内存假设备记录点击，验证确认只在对应弹窗触发且点击框安全。
3. 模拟器和桌面 E 实图接入四个退出方法（通用组队、结契组队、通用房间、结契房间），遮罩背景不匹配时仍处理专属弹窗。
4. A→B→邮件→庭院的 Mail.harvest_mail 顺序：A/B 使用真实像素；返回状态使用明确测试桩，验证 B 优先于背景返回 X，未声称这是实机转场。
5. guard 元数据与生产 ImageExtractor 生成声明一致。
6. C、D_off、D_on 真实像素接入既有九退四状态机，退出/重试边界使用实际模板和 CPU OCR，四次退出确认与四次重试确认，第五次正常战斗一次；off 按原逻辑切换一次，on 不切换。

既有 `tests/unit/logic/test_realmraid_retry.py` 和 `test_realmraid_live_fixes.py` 118 项同步运行，包括第五次正常准备、迟到帧、失败返回、锁定恢复、异常中止、账户切换及较早真实截图识别；最终联合 124 passed in 16.48s。没有通过删用例、skip 或降低断言达成结果。Python 语法、JSON 解析、git diff --check 均通过。

运行环境使用 OAS toolkit 的 Python/CV2/PP-OCRv6 模型；为防导入 Device/配置产生外部动作，通过 AST 加载相关生产类/方法，替换日志及设备连接边界。OCR 使用现有 RapidOcrModel、enlarge_canvas、_realm_retry_text 和本机已有 onnx 文件。CPU/small/4 线程明确指定，未测试常驻 RPC 服务或 DirectML 分支。pytest 原本未安装，依赖仅放入 Tool/cache/codex/ui_20261010_test_deps，不修改 toolkit 依赖或配置。

复现：

```powershell
# 在 D:/Entertainment/Tool/OAS 执行；只做离线识别及测试。
$env:PYTHONPATH='D:\Entertainment\Tool\cache\codex\ui_20261010_test_deps'
$env:PYTEST_DISABLE_PLUGIN_AUTOLOAD='1'
toolkit/python.exe -m pytest dev_tools/test_popup_ui_20261010.py tests/unit/logic/test_realmraid_retry.py tests/unit/logic/test_realmraid_live_fixes.py -q --disable-warnings
toolkit/python.exe dev_tools/test_popup_ui_20261010.py --output ../logs/ui_adaptation/20261010/after.json
```

测试需要本机提供的截图及本地 tests/ 目录；tests/ 在本仓库忽略规则中，不随克隆分发。单独识别 CLI 不需要 pytest。

## 5. 已知边界与人工验收

离线通过代表所提供图片和测试模型下的识别正确，不代表真实游戏任务已端到端验收。来源截图本身作为模板裁切源，自匹配接近 1 并不是独立样本泛化证明；C/E 的桌面截图提供跨客户端验证，邮件与商店没有这种独立桌面证据。

- 邮件：人工确认领取→汇总奖励→空白点击关闭→清洁邮件页→庭院的真实转场、动画和冷却；检查奖励数量变动时固定标题仍稳定。桌面邮件未确认。
- 战斗：准备态与实际战斗态分别退出，确认失败页及关闭策略；跨副本/活动的退出标题是否始终一致未确认，尤其不同房间类型、特殊副本和过场状态。
- 再次挑战：两种状态、勾选后当日后续不弹提示及自然再次挑战路径需要实机复核；桌面已勾选未确认。保持既有用户行为，不能为验收主动更改“不再提示”策略。
- 九退四：人工确认第 1–4 次退出计数、失败页转场、同目标重试、第五次正常战斗与锁队恢复。干净失败页、准备页、网格页、第五次战斗及其结算在新十图中没有提供；离线测试中这些节点为已有模拟，不可声称全部节点使用新版实机帧。
- 组队：组队页、组队房间导航、结契自定义退出，以及遮罩后背景仍可匹配/暂时失配的情况；其他旧型退出正文未确认。本轮只替换退出流程，原邀请确认仍需维持。
- 神秘商店：人工先检查弹窗识别与坐标，不应为了验证资源自动执行消耗。桌面 F 未确认；本轮没有验证实际刷新或购买效果。
- 仍保留的共用 `I_EXIT3`、`I_M_PICTURE_REFUSE` 等在相似弹窗上可匹配；它们的其他业务语义没有对应实图，不能强行用本轮 E/F 的正文限定所有消费者。同心集结 page.py 已有 OCR 保护，`alliedteam.return_to_main()` 的原通用兜底则仍依赖业务上下文，这一已有风险本轮未重构。不得据本轮结论扩展为“所有通用确定按钮都绝无误匹配”。

## 6. Git 状态与审计证据

本轮新增/修改 24 个项目文件：18 个已跟踪修改、6 个未跟踪新增（含本报告和离线测试）。最终总体 34 个已跟踪修改、16 个未跟踪文件，包含开工原有 16M/10??。暂存区为空，HEAD 未改变，未 commit、push、创建 PR。所有原有 26 个文件 SHA256 完全一致，不包含任何本轮覆盖。没有触及 OASX。

本轮文件列表：

- ` M` [dev_tools/assets_extract.py](D:/Entertainment/Tool/OAS/dev_tools/assets_extract.py)
- ` M` [module/atom/image.py](D:/Entertainment/Tool/OAS/module/atom/image.py)
- ` M` [tasks/BondlingFairyland/general_invite.py](D:/Entertainment/Tool/OAS/tasks/BondlingFairyland/general_invite.py)
- ` M` [tasks/BondlingFairyland/script_task.py](D:/Entertainment/Tool/OAS/tasks/BondlingFairyland/script_task.py)
- ` M` [tasks/Component/GeneralBattle/assets.py](D:/Entertainment/Tool/OAS/tasks/Component/GeneralBattle/assets.py)
- ` M` [tasks/Component/GeneralBattle/gb/exit_ensure.png](D:/Entertainment/Tool/OAS/tasks/Component/GeneralBattle/gb/exit_ensure.png)
- ` M` [tasks/Component/GeneralBattle/gb/image.json](D:/Entertainment/Tool/OAS/tasks/Component/GeneralBattle/gb/image.json)
- ` M` [tasks/Component/GeneralInvite/assets.py](D:/Entertainment/Tool/OAS/tasks/Component/GeneralInvite/assets.py)
- ` M` [tasks/Component/GeneralInvite/general_invite.py](D:/Entertainment/Tool/OAS/tasks/Component/GeneralInvite/general_invite.py)
- ` M` [tasks/Component/GeneralInvite/gi/image.json](D:/Entertainment/Tool/OAS/tasks/Component/GeneralInvite/gi/image.json)
- ` M` [tasks/Component/GeneralRoom/general_room.py](D:/Entertainment/Tool/OAS/tasks/Component/GeneralRoom/general_room.py)
- ` M` [tasks/DailyAltAcc/assets.py](D:/Entertainment/Tool/OAS/tasks/DailyAltAcc/assets.py)
- ` M` [tasks/DailyAltAcc/mail.py](D:/Entertainment/Tool/OAS/tasks/DailyAltAcc/mail.py)
- ` M` [tasks/DailyAltAcc/mail/image.json](D:/Entertainment/Tool/OAS/tasks/DailyAltAcc/mail/image.json)
- ` M` [tasks/DailyAltAcc/mail/mail_m_page_get.png](D:/Entertainment/Tool/OAS/tasks/DailyAltAcc/mail/mail_m_page_get.png)
- ` M` [tasks/DailyAltAcc/mshop/image1.json](D:/Entertainment/Tool/OAS/tasks/DailyAltAcc/mshop/image1.json)
- ` M` [tasks/GameUi/game_ui.py](D:/Entertainment/Tool/OAS/tasks/GameUi/game_ui.py)
- ` M` [tasks/GameUi/page.py](D:/Entertainment/Tool/OAS/tasks/GameUi/page.py)
- `??` [dev_tools/test_popup_ui_20261010.py](D:/Entertainment/Tool/OAS/dev_tools/test_popup_ui_20261010.py)
- `??` [tasks/Component/GeneralBattle/gb/exit_dialog.png](D:/Entertainment/Tool/OAS/tasks/Component/GeneralBattle/gb/exit_dialog.png)
- `??` [tasks/Component/GeneralInvite/gi/gi_exit_dialog.png](D:/Entertainment/Tool/OAS/tasks/Component/GeneralInvite/gi/gi_exit_dialog.png)
- `??` [tasks/DailyAltAcc/mail/mail_m_batch_award.png](D:/Entertainment/Tool/OAS/tasks/DailyAltAcc/mail/mail_m_batch_award.png)
- `??` [tasks/DailyAltAcc/mshop/mshop_ms_refresh_dialog.png](D:/Entertainment/Tool/OAS/tasks/DailyAltAcc/mshop/mshop_ms_refresh_dialog.png)
- `??` [docs/ui_popup_adaptation_20261010.md](D:/Entertainment/Tool/OAS/docs/ui_popup_adaptation_20261010.md)

开工原有文件（全部保留，非本轮改动）：

- [dev_tools/click_collector.py](D:/Entertainment/Tool/OAS/dev_tools/click_collector.py)
- [module/device/method/minitouch.py](D:/Entertainment/Tool/OAS/module/device/method/minitouch.py)
- [tasks/Component/SwitchAccount/assets.py](D:/Entertainment/Tool/OAS/tasks/Component/SwitchAccount/assets.py)
- [tasks/Component/SwitchAccount/dropdown_state.py](D:/Entertainment/Tool/OAS/tasks/Component/SwitchAccount/dropdown_state.py)
- [tasks/Component/SwitchAccount/res/image.json](D:/Entertainment/Tool/OAS/tasks/Component/SwitchAccount/res/image.json)
- [tasks/Component/SwitchHelpShikigami/assets.py](D:/Entertainment/Tool/OAS/tasks/Component/SwitchHelpShikigami/assets.py)
- [tasks/Component/SwitchHelpShikigami/res/ocr.json](D:/Entertainment/Tool/OAS/tasks/Component/SwitchHelpShikigami/res/ocr.json)
- [tasks/Component/SwitchHelpShikigami/switch_help_shikigami.py](D:/Entertainment/Tool/OAS/tasks/Component/SwitchHelpShikigami/switch_help_shikigami.py)
- [tasks/DailyAltAcc/alliedteam.py](D:/Entertainment/Tool/OAS/tasks/DailyAltAcc/alliedteam.py)
- [tasks/EvoZone/script_task.py](D:/Entertainment/Tool/OAS/tasks/EvoZone/script_task.py)
- [tasks/KekkaiUtilize/config.py](D:/Entertainment/Tool/OAS/tasks/KekkaiUtilize/config.py)
- [tasks/MasterDisciple/script_task.py](D:/Entertainment/Tool/OAS/tasks/MasterDisciple/script_task.py)
- [tasks/Plotline/assets.py](D:/Entertainment/Tool/OAS/tasks/Plotline/assets.py)
- [tasks/Plotline/res/image.json](D:/Entertainment/Tool/OAS/tasks/Plotline/res/image.json)
- [tasks/Plotline/script_task.py](D:/Entertainment/Tool/OAS/tasks/Plotline/script_task.py)
- [tasks/WantedQuests/explore.py](D:/Entertainment/Tool/OAS/tasks/WantedQuests/explore.py)
- [dev_tools/capture_desktop_client.py](D:/Entertainment/Tool/OAS/dev_tools/capture_desktop_client.py)
- [dev_tools/test_android_package_resolution.py](D:/Entertainment/Tool/OAS/dev_tools/test_android_package_resolution.py)
- [dev_tools/test_capture_desktop_client.py](D:/Entertainment/Tool/OAS/dev_tools/test_capture_desktop_client.py)
- [dev_tools/test_upstream_evo_zone.py](D:/Entertainment/Tool/OAS/dev_tools/test_upstream_evo_zone.py)
- [dev_tools/test_upstream_help_ocr.py](D:/Entertainment/Tool/OAS/dev_tools/test_upstream_help_ocr.py)
- [dev_tools/test_upstream_minitouch.py](D:/Entertainment/Tool/OAS/dev_tools/test_upstream_minitouch.py)
- [dev_tools/test_upstream_plotline.py](D:/Entertainment/Tool/OAS/dev_tools/test_upstream_plotline.py)
- [dev_tools/test_upstream_switch_account.py](D:/Entertainment/Tool/OAS/dev_tools/test_upstream_switch_account.py)
- [dev_tools/test_upstream_wanted_quests.py](D:/Entertainment/Tool/OAS/dev_tools/test_upstream_wanted_quests.py)
- [tasks/Plotline/res/page_summon_draw.png](D:/Entertainment/Tool/OAS/tasks/Plotline/res/page_summon_draw.png)

证据目录：D:/Entertainment/Tool/logs/ui_adaptation/20261010。

- [before.json](D:/Entertainment/Tool/logs/ui_adaptation/20261010/before.json)
- [after.json](D:/Entertainment/Tool/logs/ui_adaptation/20261010/after.json)
- [existing_hashes.json](D:/Entertainment/Tool/logs/ui_adaptation/20261010/existing_hashes.json)
- [initial_git_status.txt](D:/Entertainment/Tool/logs/ui_adaptation/20261010/initial_git_status.txt)
- [final_git_status.txt](D:/Entertainment/Tool/logs/ui_adaptation/20261010/final_git_status.txt)
- [task_files.json](D:/Entertainment/Tool/logs/ui_adaptation/20261010/task_files.json)
- [pytest_final.txt](D:/Entertainment/Tool/logs/ui_adaptation/20261010/pytest_final.txt)
- [recognition_final.txt](D:/Entertainment/Tool/logs/ui_adaptation/20261010/recognition_final.txt)

initial_git_status.txt 在创建本轮测试后保存，包含该测试条目；原有 26 个条目以 existing_hashes.json 和首次只读 Git 输出为准。报告生成后的最终完整状态以 final_git_status.txt 为准。


## 7. 用户实机验收与独立本地提交（2026-10-10 更新）

前文第 5、6 节记录资源适配完成时的离线阶段状态；以下为用户随后提供的验收证据及本次提交范围，不能将先前“未实机验收”状态当作当前结论。

用户人工实机验收已通过：

- 邮件领取及奖励结算。
- 退出战斗确认。
- 再次挑战流程。
- 个人突破九退四：退出四次，第五次正常完成。

以上实机结论来自用户直接反馈；用户没有分别指定此次验收使用的客户端或设备，不据此扩大成两端所有场景全部实机通过。

退出组队、神秘商店刷新等功能尚未完成全部实机验收；它们只有本报告描述的截图识别及离线回归证据，不能宣称实机已通过。后续由用户安排验收，不自动操作游戏。

本次仅授权创建一个独立本地 commit，使用本报告第 6 节的 24 文件白名单：18 个已有文件修改和 6 个新文件（包括报告与测试）。提交信息为 `fix: adapt updated game UI popup recognition`。不提交账号配置、原始截图、日志或白名单之外文件；不 push，不创建 PR。之前的适配阶段未暂存/未提交记录保留为历史事实。

当前额外的探索模块改动及其他历史未提交工作均排除。全部白名单代码差异逐项核对，未发现与无关工作的混合修改；提交前保存其余文件及 config/ 的哈希，并审查完整暂存差异与格式检查。实际 commit SHA、所含文件、最终状态和保护验证记录保存在本机 `D:/Entertainment/Tool/logs/ui_adaptation/20261010/local_commit/`，不把这些本机日志加入提交。
