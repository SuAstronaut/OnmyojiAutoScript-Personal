# 修行合训（season_boss）玩法

季节性玩法, 与原有 boss 战共存。本版本活动结束后停用, 下次活动可再启用。
入口: 复用现有 boss 页入口 (`I_TO_BATTLE_BOSS`), 进入后即为修行合训主页。

## 启用
1. `config/<实例>.json` 中 `activity_shikigami.general_climb.run_sequence` 加入 `season_boss`
   (如 `"run_sequence": "pass,boss,ap,season_boss"`)
2. `activity_shikigami.general_climb.season_boss_limit` 设置最大搜寻次数
3. `activity_shikigami.season_boss.phase_order` 配置门票阶段顺序 (normal/premium)
4. `activity_shikigami.season_boss.monster_preset_text` 配置怪物预设, 品阶=普通/精英/首领:
   - 4段(只切队伍): 怪物名,品阶,队伍组,队伍队
   - 6段(队伍+御魂): 怪物名,品阶,队伍组,队伍队,御魂组,御魂队
5. `activity_shikigami.season_boss.enable_preset` 开启按怪物切预设
6. (可选) `enable_switch_soul` 开启御魂切换: 收服御灵页点式神录进式神录切御魂再退回。
   御魂预设来源: 命中行6段末两段, 未命中或4段时回落 `default_soul_group_team`;
   仍未配御魂(soul=None)时御魂跟随队伍预设, 进式神录切相同预设。
   以下情况自动跳过式神录切换, 只切队伍:
   - 无御魂目标(队伍预设也未配)
   - 御魂预设与上次已切一致: 同次任务内不重复切换
   其余情况(有明确御魂预设且与上次不同)都会进式神录切换。
7. (可选) `default_soul_group_team` 兜底御魂预设, 组,队; `-1,-1` 表示不切

## 停用 (下次活动前)
只需从 `run_sequence` 移除 `season_boss`。代码与资源保留, 不参与调度, 零影响。

## 资源隔离
- 全部资产为 `SEASON_BOSS_` 前缀 (见 tasks/ActivityShikigami/assets.py)
- 全部逻辑与配置在 `tasks/ActivityShikigami/season_boss/` 子包内
- 唯一共享改动: `GeneralClimb.season_boss_limit` 单字段

## 竖排怪物名 OCR
中间界面怪物名为竖排文字, ROI 高/宽 >= 1.5, `ocr_single_line` 自动旋转识别。
若某期活动怪物名识别率下降, 调整 `season_boss_monster_name` 的 ROI。

## 搜寻按钮锁定状态
搜到御灵但还没击败时, 右下角搜寻按钮变灰并叠加锁图标(`I_SEASON_BOSS_LOCKED`),
此时普通/注灵两个搜寻按钮都不渲染, **无法判断当前门票模式**。
流程对此的处理: 每轮循环先查锁, 锁住就点左上角卡片重开收服页把这只打掉,
战斗结束回主页锁自动解除, 再回到循环重新判断模式。
`_switch_ticket_mode` 因此改为单次判断返回 bool, 不再内部死等 stop(死等是卡死的根因)。
