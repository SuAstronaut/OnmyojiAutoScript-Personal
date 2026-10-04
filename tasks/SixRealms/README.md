# SixRealms / 月之海

运行控制流以 `efc0b9b057c9ec095fbe8482c8262e7af59d6073` 为基线。

- `one()` 每次扫描当前页，处理技能或属性后立即回外层。连续 THREE/TWO 不限次数；不等待奖励、地图或转场证明，不建立属性事务。
- THREE：力量可选就优先力量。点击复用 `appear_then_click(..., interval=1)` 的既有节流；下一页重新判断，不能在同一按钮的内层循环里跳过等级变化。
- TWO：左技巧、右魅力。复用现有单行 OCR，只读技巧左上角数字 `(431,236,19,24)`；Lv1/Lv2选技巧，Lv3及以上选魅力。失败后重新截图读一次；仍未知的安全退路是不点击、保存诊断并明确停止本次接管，避免未知Lv继续加技巧或盲猜魅力；不创建pending。
- 属性页须有独立属性标题身份和完整固定按钮结构。保留真实标题渲染变体；不降低阈值或扩ROI。
- `cnt_skillpower` 恢复基线从1开始的路线/刷新进度代理。它不是精确游戏力量等级，不持久化；地图和普通刷新保持旧配置语义，默认目标4。
- 路线两档：尚无skill101且力量代理未达目标时优先战斗→神秘→宁息→绽放→混沌；其它时候神秘→宁息→战斗→绽放→混沌。两档都先处理取消及已有余额不足进入分支。
- 普通技能自然出现skill101（六道·轰雷）就选；旧刷新条件为未有101、力量代理达标、允许刷新、当前fallback第4列、金币>=50、剩刷新次数>0。不使用remaining<=10、绽放进度或每页一次预算。
- 普通技能已知101点击、商店购买及可观察战斗图标均可正向记录拥有。奖励弹窗由外层按标题+关闭提示识别，使用原 `C_UI_REWARD` 关闭；不识别奖励内容，不做7秒ownership确认。
- 战斗使用原 `I_BATTLE_SKILL101` 本体模板，front `(86,90,30,41)`、search `(5,83,235,51)`；真实Lv1/Lv3分数相同，数字主体未包含在模板内。自动按钮与图标同时可见才补证。不可观察/单帧未匹配不会清除已有拥有证据。
- 最终保底：仅干净地图读取真实remaining，原数字ROI `(1072,235,36,24)`、原HSV过滤、严格1..19且置信度>=0.9。新局remaining19；不维护整局节点历史。两次外层读到同一remaining1或2、无可靠轰雷证据且未尝试，才发一次唤息。复用正常300确认、余额不足进入及原 `run_l101()/buy_skill_101()`。失败警告后回外层，不伪造拥有，不再次唤息。
- 只持久化 `honglei_owned/charm_selected/honglei_guarantee_attempted` 三个布尔标记到独立 `config/tasks_config/sixrealms_flags/`。Start清标记，Continue/局内入口读取；旧实验 `config/tasks_config/sixrealms/*.json` 不读、不迁移、不删除，不能阻塞运行。没有力量/技巧计数、节点计数或pending字段。
- 启动若已在可靠地图、属性、奖励、普通技能、商店或战斗页，SixRealms入口直接接管，不先经过通用页面导航；Start/Continue照常进入。普通运行战斗以AUTO+轰雷图标确认为SixRealms，最终BOSS以名称+AUTO确认，直接复用现有战斗handler，不再等待已经消失的NPC开战入口。
- 魅力点击后记录本把魅力；仅最终BOSS执行绿标。真实可操作BOSS+觉模型+目标专属绿标ROI保持原校验；已绿标不点击，否则按人工成功点P1 `(625,544)`、P2 `(624,526)`、P3 `(631,569)`，每点后最多8帧且连续3帧确认，最多3次。失败警告后继续BOSS。
- 统计独立保留：执行次数、segment内最大完成N后跨segment累加的实际完成把数、任务execution边界耗时。OASX当前尚未显示完成把数，本次不修改。

离线回归：`dev_tools/test_sixrealms_simplified.py`、`dev_tools/test_sixrealms_stats.py`、`dev_tools/test_log_stats_execution_time.py`。真实TWO Lv1/Lv2已有fixture；真实TWO Lv3与战斗Lv2尚未采样，单元逻辑/数字不变性测试不能冒充真实采样。三标记不构成完整局身份：OAS未观察到Start而用户已在停机期间手动结束旧局并新开，再从新局地图接管，无法自动区分；应让OAS从Start接管。正常同局Continue保留标记；不可见技能栏不清除拥有证据。

旧 `run_state.py` 及5个实验测试已逐字节备份后移出工作树，位于 `D:/Entertainment/Tool/cache/codex/sixrealms-simplify-20261005/files/`；完整patch、哈希及退出列表同目录。保留22个验证模板和有价值的真实fixture。

坐标工具 `dev_tools.click_collector`：用 `--list` 获取当前窗口index；index并非永久稳定。
