# ReturnGift SR 碎片统计实现计划

## 背景

`design.md` 要求在 ReturnGift 任务中新增“获取所有 SR 图像的碎片数量”功能：

1. 从 `temp_path` 下指定日期截图中裁剪 SR 图像模板，生成 `count\image.json` 和 `count\sr_*.png`。
2. 在回礼页面通过模板匹配识别 SR 图像，并用 OCR 读取对应碎片数量。
3. 将统计结果输出为 `sr_count.json`。

新增功能必须与现有送礼/收礼流程分离执行，不能同一次运行同时执行。默认模式保持现有送礼流程，避免影响 `ReturnGift.run()` 与 `MultiDailyAltAcc` 的 `daily_progress.json` 联动。

资源提交约定：

- `tasks\ReturnGift\count\image.json` 和 `tasks\ReturnGift\count\sr_*.png` 是模板资源，需要提交 git。
- `tasks\ReturnGift\sr_count.json` 是运行统计结果，不提交 git。

## 需要修改的文件

- `tasks\ReturnGift\config.py`
- `tasks\ReturnGift\script_task.py`
- `tests\unit\logic\test_return_gift_sr_count.py`
- `.gitignore`：仅在需要时忽略 `tasks/ReturnGift/sr_count.json`，不要忽略 `tasks/ReturnGift/count/`。

## 配置设计

在 `ReturnGiftConfig` 中新增运行模式字段：

- `return_gift_mode: str = "gift"`

建议下拉选项：

- `gift`：送礼模式，默认值，执行现有送礼/收礼流程。
- `build_sr_template`：模板生成模式，只生成 `count\image.json` 和 `count\sr_*.png`。
- `sr_count`：SR 碎片统计模式，只识别 SR 图像并输出 `sr_count.json`。

新增 SR 统计配置，默认值来自 `design.md`：

- `sr_template_date = "2026-06-07"`
- `sr_temp_path = "temp_path"`
- `sr_match_roi = (334, 163, 612, 454)`
- `sr_template_box = (340, 171, 104, 146)`
- `sr_count_ocr_box = (348, 320, 95, 22)`
- `sr_empty_swipe_limit = 3`
- `sr_match_threshold = 0.85`

所有新增配置字段都需要中文 `description`。

## 执行模式拆分

将当前 `ScriptTask.run()` 改为模式分发：

1. 读取 `return_gift_mode`。
2. `gift`：调用 `_run_gift_mode()`，保持现有逻辑不变。
3. `build_sr_template`：调用 `_run_build_sr_template_mode()`。
4. `sr_count`：调用 `_run_sr_count_mode()`。

建议把当前 `run()` 中已有送礼/收礼主体整体迁移到 `_run_gift_mode()`，减少行为变化。

## 关键函数设计

### 路径辅助

- `return_gift_dir(self) -> Path`
- `sr_count_dir(self) -> Path`
- `sr_count_output_path(self) -> Path`

### 纯逻辑函数

- `parse_sr_count(text: str) -> int | None`
  - OCR 文本形态是 `999/40`，实际数量取 `/` 前的 `999`。
  - 如果没有 `/`，提取第一个整数。
  - 空文本或无数字返回 `None`。

- `offset_box_by_match(base_ocr_box, anchor_box, matched_box) -> tuple[int, int, int, int]`
  - 根据 SR 模板命中框相对首个 SR 框的偏移，推导对应 OCR 框。

- `sort_sr_matches(matches) -> list[tuple]`
  - 按 `y` 再按 `x` 排序，保证识别和输出稳定。

## 模板生成流程

新增 `build_sr_templates(self) -> list[dict]`：

1. 在 `sr_temp_path` 中查找 `{sr_template_date} *.png`。
2. 创建 `tasks\ReturnGift\count`。
3. 按 `sr_match_roi`、`sr_template_box` 在截图中裁剪 SR 图像。
4. 只保留完全落在 `sr_match_roi` 内的裁剪框。
5. 按 `sr_1.png`、`sr_2.png` 递增命名输出。
6. 生成 `count\image.json`，记录模板名、文件路径、来源截图、裁剪框、识别范围和创建时间。

## 模板加载流程

新增 `load_sr_templates(self) -> list[RuleImage]`：

1. 读取 `tasks\ReturnGift\count\image.json`。
2. 为每个 `sr_*.png` 动态构造 `RuleImage`。
3. 使用 `RuleImage.match_all_any()` 进行匹配。
4. 不手动修改 `assets.py`，因为它是自动生成文件。

## SR 碎片统计流程

新增 `count_sr_fragments(self) -> dict`：

1. 加载 `count\image.json` 中的 SR 模板。
2. 循环截图并在 `sr_match_roi` 内匹配未识别模板。
3. 每个模板匹配成功后，根据命中框偏移计算 OCR 框。
4. 碎片 OCR 优先使用 `DIGITCOUNTER` 模式，因为文本形态是 `999/40`。
5. 如果 `DIGITCOUNTER` 效果不好，再用自定义后处理提取 `/` 前数字。
6. 成功读取数量后，将该模板加入 `seen`，后续不再匹配该资源。
7. 当前页没有识别到新 SR 时执行下滑。
8. 下滑距离为 `146 + 22 = 168`。
9. 连续 3 次滑动没有新识别后停止。
10. 输出 `tasks\ReturnGift\sr_count.json`。

输出结构建议：

```json
{
  "updated_at": "ISO 时间",
  "total": 1,
  "items": [
    {
      "name": "sr_1",
      "template": "count/sr_1.png",
      "count": 999,
      "match_box": [340, 171, 104, 146],
      "ocr_box": [348, 320, 95, 22],
      "ocr_text": "999/40"
    }
  ]
}
```

## 测试计划

新增 `tests\unit\logic\test_return_gift_sr_count.py`，优先覆盖纯逻辑：

- 默认 `gift` 模式只执行原送礼流程。
- `build_sr_template` 和 `sr_count` 不执行送礼流程。
- `parse_sr_count("999/40") == 999`。
- `parse_sr_count("碎片12/40") == 12`。
- 空文本、无数字文本返回 `None`。
- OCR 框跟随 SR 命中框偏移。
- 模板识别成功后不会重复匹配。
- 连续 3 次空滑后停止。
- `sr_count.json` 输出结构稳定。

## 验证命令

```bash
./toolkit/python.exe -m pytest tests/unit/logic/test_return_gift_sr_count.py
./toolkit/python.exe -m pytest tests/ -m "not e2e"
```

## 注意事项

- 默认模式必须是 `gift`。
- 新增 SR 统计不能影响现有送礼/收礼流程。
- `assets.py` 不手动修改。
- `count\image.json` 和 `count\sr_*.png` 需要提交。
- `sr_count.json` 不提交。
- OCR 实现优先尝试 `DIGITCOUNTER`，不稳定时再增加自定义解析。
