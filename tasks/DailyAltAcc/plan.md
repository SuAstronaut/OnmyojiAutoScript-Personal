# DailyAltAcc · 发布碎片子功能 实现计划

## Context（背景）

`tasks/DailyAltAcc/design.md` 提出在 `DailyAltAcc` 任务下新增"发布碎片"子功能。已有的 `tasks/ReturnGift/script_task.py::count_sr_fragments` 会扫描寮回礼页面、统计每种 SR 碎片的持有数量，并将结果写入 `logs/sr_count.json`（精简后字段为 `[{"name": "I_SR_X", "count": N}, ...]`）。

本计划在此基础上引入子功能 `publish_sr`：

1. 把 `logs/sr_count.json` 转换为按 *可发布次数*（`count // 99`）排序的待办队列 `tasks/DailyAltAcc/sr_cnt.json`；
2. 按队列顺序在游戏内查找对应 SR 卡片，模板命中即点击 → 调用预留的"碎片发布流程"接口；
3. 每发一次将对应 `count` 减 1，归零移除；
4. 每次发布后回写 `sr_cnt.json`，支持断点续做；
5. 当队列全部项都未命中模板时退出循环。

页面入口（导航到"发布碎片"页面）以及真正的发布动作均由用户后续补，本次只做队列、匹配点击与回写。

---

## 关键依赖

- **DailyAltAcc 任务架构** — `tasks/DailyAltAcc/script_task.py:35-38` 用多继承组装各子模块，`run()` 根据 `con.daily_alt_acc_config.<xxx>_enable` 依次调用 `run_<xxx>`。
- **子模块样板** — `tasks/DailyAltAcc/donatejade.py:7-19`：继承 `DailyAltAccBase`，写 `run_<name>` 入口，内部用 `appear / appear_then_click` 操作。
- **基类** — `tasks/DailyAltAcc/utils.py:9-20` 的 `DailyAltAccBase = GameUi + DailyAltAccAssets`，提供 `get_config()` 与 `screenshot()`。
- **配置入口** — `tasks/DailyAltAcc/config.py:15-32` 的 `DailyAltAccConfig`，新增字段不需要动 `module/config/config_model.py`。
- **SR 数量数据源** — `tasks/ReturnGift/script_task.py:247-302` 输出 `logs/sr_count.json`，结构 `[{"name": "I_SR_N", "count": int}, ...]`。
- **SR 模板资源** — 模板文件在 `tasks/ReturnGift/count/sr_X.png`，清单 `tasks/ReturnGift/count/image.json` 已是 image rule 标准格式；运行 `dev_tools/assets_extract.py` 后会在 `tasks/ReturnGift/assets.py::ReturnGiftAssets` 中生成 `I_SR_1 … I_SR_N`（`dev_tools/assets_extract.py::name_transform` + `ImageExtractor.extract_item` 已验证 `itemName="sr_1"` → `I_SR_1`）。
- **匹配与点击** — `module/atom/image.py:140-170` 的 `RuleImage.match()`；`tasks/base_task.py:208` 的 `appear_then_click(target)` 命中后自动点击 `roi_front` 中心，是"匹配后点击命中位置"的现成实现。

---

## 实现方案

### 1. 配置字段

文件：`tasks/DailyAltAcc/config.py`

在 `DailyAltAccConfig` 末尾追加：

```python
publish_sr_enable: bool = Field(default=False, description='是否发布SR碎片')
```

### 2. 新建子模块 `publish_sr.py`

文件：`tasks/DailyAltAcc/publish_sr.py`

**职责**：实现 design.md 的 1–4 步；不负责导航到"发布碎片"页面，也不实现真正的发布动作。

```python
# This Python file uses the following encoding: utf-8
import json
from pathlib import Path

from module.logger import logger
from tasks.DailyAltAcc.utils import DailyAltAccBase
from tasks.ReturnGift.assets import ReturnGiftAssets


class PublishSr(DailyAltAccBase, ReturnGiftAssets):
    """发布SR碎片子功能：依据 sr_count.json 生成可发布队列，按序点击发布"""

    # 输入：碎片数量统计；输出/续做：可发布次数队列
    SR_COUNT_FILE = Path('logs/sr_count.json')
    SR_CNT_FILE = Path(__file__).resolve().parent / 'sr_cnt.json'
    PER_PUBLISH = 99  # 一次发布需要消耗的碎片数

    def run_publish_sr(self):
        # 步骤1：sr_cnt.json 已存在则直接续做，跳过步骤2/3
        if self.SR_CNT_FILE.exists():
            queue = self._read_queue()
        else:
            queue = self._build_queue_from_sr_count()
            self._write_queue(queue)

        # 步骤4：按队列循环匹配+点击+回写
        self._publish_loop(queue)

    # ---- 步骤2/3：构建队列 ----
    def _build_queue_from_sr_count(self) -> list[dict]:
        # 读 logs/sr_count.json，count 改写为 count // 99，过滤 0 项后排序
        if not self.SR_COUNT_FILE.exists():
            logger.warning(f'{self.SR_COUNT_FILE} 不存在，发布队列为空')
            return []
        with open(self.SR_COUNT_FILE, 'r', encoding='utf-8') as f:
            raw = json.load(f)
        items = []
        for entry in raw:
            publish_times = int(entry['count']) // self.PER_PUBLISH
            if publish_times <= 0:
                continue
            items.append({'name': entry['name'], 'count': publish_times})
        return self._sort_queue(items)

    def _sort_queue(self, queue: list[dict]) -> list[dict]:
        # count 降序；count 相同时『后来者排前面』——用稳定排序+反转原序实现
        indexed = list(enumerate(queue))
        indexed.sort(key=lambda iv: (-iv[1]['count'], -iv[0]))
        return [v for _, v in indexed]

    # ---- 步骤4：发布主循环 ----
    def _publish_loop(self, queue: list[dict]):
        while queue:
            matched_index = self._find_first_match(queue)
            if matched_index is None:
                # 全队遍历皆 miss → 结束流程（用户确认的语义）
                logger.info('队列中所有 SR 模板均未命中，结束发布流程')
                break

            top = queue[matched_index]
            self._do_publish_sr(top['name'])  # 预留接口，目前仅日志占位

            top['count'] -= 1
            if top['count'] <= 0:
                queue.pop(matched_index)
            queue = self._sort_queue(queue)
            self._write_queue(queue)

    def _find_first_match(self, queue: list[dict]) -> int | None:
        # 遍历整个队列，返回首个 match 命中的索引；全部 miss 返回 None
        self.screenshot()
        for idx, entry in enumerate(queue):
            rule = getattr(ReturnGiftAssets, entry['name'], None)
            if rule is None:
                logger.warning(f'ReturnGiftAssets 中找不到 {entry["name"]}，跳过')
                continue
            # appear_then_click：命中后自动点击 roi_front，并返回 True
            if self.appear_then_click(rule):
                return idx
        return None

    # ---- 预留接口 ----
    def _do_publish_sr(self, name: str):
        # 占位：真正的发布操作由后续补充
        logger.info(f'TODO publish {name}')

    # ---- IO ----
    def _read_queue(self) -> list[dict]:
        with open(self.SR_CNT_FILE, 'r', encoding='utf-8') as f:
            return json.load(f)

    def _write_queue(self, queue: list[dict]):
        self.SR_CNT_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(self.SR_CNT_FILE, 'w', encoding='utf-8') as f:
            json.dump(queue, f, ensure_ascii=False, indent=2)
```

#### 设计要点（要点对应 design.md 步骤）

| design.md 步骤 | 实现位置 | 备注 |
|---|---|---|
| 1. 文件已存在则跳过 2、3 | `run_publish_sr` 起始的 `if SR_CNT_FILE.exists()` | 续做支持 |
| 2. 读 sr_count.json 取 name、count | `_build_queue_from_sr_count` 第一段 | name 已是 `I_SR_X` 形式 |
| 3. count // 99，过滤 0，排序后写文件 | `_build_queue_from_sr_count` + `_sort_queue` | "后来居前" 用 `(-count, -原索引)` 实现 |
| 4. 匹配 → 点击 → 发布 → 减 1 → 排序 → 回写 | `_publish_loop` + `_find_first_match` | 全队 miss 退出 |

`_sort_queue` 的稳定性测试：原序 `[A:2, B:2, C:2]` → 索引 `[(0,A),(1,B),(2,C)]` → 按 `(-2, -idx)` 排序得 `[(2,C),(1,B),(0,A)]`，符合"相同 count 后来者在前"。

### 3. 接入 ScriptTask

文件：`tasks/DailyAltAcc/script_task.py`

- 顶部 import 区域追加：

  ```python
  from tasks.DailyAltAcc.publish_sr import PublishSr
  ```

- `ScriptTask` 继承列表追加 `PublishSr`：

  ```python
  class ScriptTask(Courtyard, Mail, Donatejade, Cooperation,
                   Returngift, Alliedteam, Mshop, Tree,
                   SummonUp, Trialbattle, PublishSr,
                   Guild, WeeklyTrifles):
  ```

- `run()` 中调度：插在 `alliedteam` 之前（避免被早期网络/弹窗循环和 `alliedteam` 的长流程夹击），紧邻 `summon_up`：

  ```python
  if con.daily_alt_acc_config.publish_sr_enable:
      self.run_publish_sr()
  ```

### 4. 生成 SR 模板资源

实施期由用户运行一次：

```bash
./toolkit/python.exe -m dev_tools.assets_extract
```

预期：`tasks/ReturnGift/assets.py::ReturnGiftAssets` 新增 `I_SR_1 … I_SR_N`，供 `getattr(ReturnGiftAssets, name)` 动态访问。

---

## 验证

### 单元验证（无设备）

写一个临时脚本调用 `PublishSr._build_queue_from_sr_count` 与 `_sort_queue`：

- 用例 1：`sr_count.json = [{name:I_SR_1,count:1000},{name:I_SR_2,count:100},{name:I_SR_3,count:98}]`  
  期望队列：`[{I_SR_1,10},{I_SR_2,1}]`，`I_SR_3` 被过滤。
- 用例 2：相同 count 的稳定性 — 输入 `[A:200,B:200,C:200]`（每个 200//99=2）→ 队列 `[C:2, B:2, A:2]`。
- 用例 3：`sr_cnt.json` 已存在时，`run_publish_sr` 走 `_read_queue` 分支，不重新构建。

### 集成验证（真机）

在 `config/oas.json`（或测试实例）打开 `daily_alt_acc.daily_alt_acc_config.publish_sr_enable = true`，运行：

```bash
./toolkit/python.exe -m tasks.DailyAltAcc.script_task --config oas
```

观察：
- 日志按顺序出现 `TODO publish I_SR_X`；
- `tasks/DailyAltAcc/sr_cnt.json` 每次发布后 `count` 同步减 1；
- 全队 miss 时日志出现 `队列中所有 SR 模板均未命中，结束发布流程` 并退出。

### 续做验证

手工保留 `tasks/DailyAltAcc/sr_cnt.json`（例如截断成只剩一项）→ 再次运行任务 → 确认走 `_read_queue` 分支、不重建。

---

## 关键文件清单

| 文件 | 修改类型 |
|---|---|
| `tasks/DailyAltAcc/config.py` | 编辑：`DailyAltAccConfig` 追加 `publish_sr_enable` |
| `tasks/DailyAltAcc/publish_sr.py` | 新建 |
| `tasks/DailyAltAcc/script_task.py` | 编辑：import + 继承列表 + `run()` 调用 |
| `tasks/ReturnGift/assets.py` | 由 `dev_tools.assets_extract` 重新生成（含 `I_SR_X`） |
| `tasks/DailyAltAcc/sr_cnt.json` | 运行期由代码创建/更新，不预先建 |
