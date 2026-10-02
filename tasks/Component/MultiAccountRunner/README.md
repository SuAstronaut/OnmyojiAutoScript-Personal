# MultiAccountRunner - 多账号轮转执行器

## 概述

`MultiAccountRunner` 是一个通用的多账号任务执行框架，封装了多账号轮转场景下的公共逻辑，供所有需要多账号切换执行的任务复用。

## 解决的问题

在 MultiDailyAltAcc、MultiTasks、FindJade 等多账号任务中，以下逻辑被反复实现：
- 账号过滤（根据 `need_login` 和 `login_time` 判断是否需要处理）
- 账号排序（按邮箱分组、按完成时间排序）
- 账号切换（调用 `SwitchAccount`）
- 重试机制（失败后重试）
- 进度追踪（基于文件的多进程协调）
- 错误处理（异常时恢复 `need_login_time`）

这些逻辑原本分散在各任务的 `script_task.py` 中，代码高度重复且容易因修改不同步而产生 bug（如排序逻辑漏洞）。

## 核心设计

### 账号排序逻辑

**先剔除，再排序**，避免同一邮箱下已完成账号影响未完成账号的排序：

1. **过滤**：当 `need_login=False` 时，剔除 `last_complete_time >= login_time` 的账号
2. **分组**：按邮箱（account）分组，使同一邮箱下的角色连续排列（减少切换邮箱的次数）
3. **组间排序**：按每个邮箱分组的最晚完成时间排序（最晚完成的邮箱在前）
4. **组内排序**：同一邮箱内，按角色的完成时间排序（最晚完成的在前）

### login_time 参数

`login_time` 必须在任务开始时从配置中读取并传入 `MultiAccountRunner`，而不是在函数内部实时读取。因为 `need_login_time` 的值会在运行过程中变化，排序和过滤需要基于任务开始时的快照值。

### 进度追踪

基于 JSON 文件的多进程进度追踪，每个任务有独立的进度文件（`./config/tasks_config/{task_name}_progress.json`），用于协调多个配置实例的并行执行。

### 账号级续做进度（progress）

传入 `progress`（通用 `ProgressStore`，见 [progress.py](./progress.py)）后，Runner 的账号完成判定与落盘改由进度文件驱动：

- `should_process_account` 改查 `is_account_done`，中断重跑时已完成账号整个跳过；`need_login` / `login_time` 不再生效。
- 每个账号处理成功后即时 `mark_account_done` 落盘，崩溃后据此接续。
- 进度在任务全部成功收尾、安排下一阶段后由调用方 `clear()`（先调度后清，顺序不可颠倒，否则有「调度已改、进度已删」的窗口导致整轮重跑）。
- 设备级异常（卡死/弹错/模拟器退出等）会原样穿透到 `script.py` 的 Restart / 人工接管逻辑，不会被 Runner 吞掉重试。

## 使用方式

### 基本用法

```python
from tasks.Component.MultiAccountRunner.multi_account_runner import MultiAccountRunner

# 在 script_task.py 的 run() 方法中
def run(self):
    self.daily_conf = self.config.multi_daily_alt_acc
    login_time = self.daily_conf.daily_config.need_login_time

    runner = MultiAccountRunner(
        task_name="MultiDailyAltAcc",
        config=self.config,
        device=self.device,
        account_list=self.daily_conf.sup_account_list,
        need_login=self.daily_conf.daily_config.need_login,
        login_time=login_time,
        update_login_history_func=self.daily_conf.update_account_login_history,
        save_config_func=self.save_config,
    )

    # 传入自定义的账号处理函数
    runner.run(process_func=self._process_single_account)
```

### 自定义处理函数

`process_func` 接收一个 `AccountInfo` 参数，返回 `True`（成功）或 `False`（失败）：

```python
def _process_single_account(self, account_info):
    """处理单个账号"""
    # 1. 切换账号
    if not self.runner.switch_to_account(account_info):
        return False

    # 2. 执行任务逻辑
    try:
        # ... 你的任务逻辑 ...
        return True
    except Exception as e:
        logger.error(f"Error: {e}")
        return False
```

### 错误回调

可通过 `on_account_error` 参数注册错误回调，用于在账号处理异常时执行特定逻辑（如更新配置、发送通知等）：

```python
def _on_account_error(self, account_info, error):
    # 错误回调只负责通知与留痕；账号级续接交给 progress 判定，
    # 不再需要改写 need_login / need_login_time（会破坏过滤，见下）。
    self.config.notifier.push(
        content=f"{account_info.character}-{account_info.svr} 任务执行错误\nError: {error}",
        title="ERROR"
    )
    Script.save_error_log(self)

runner = MultiAccountRunner(
    ...,
    on_account_error=self._on_account_error,
)
```

### 单独使用排序/过滤功能

如果只需要排序功能而不需要完整的执行流程：

```python
runner = MultiAccountRunner(
    task_name="MyTask",
    config=self.config,
    device=self.device,
    account_list=my_account_list,
    need_login=False,
    login_time=my_login_time,
    update_login_history_func=my_update_func,
    save_config_func=my_save_func,
)

sorted_accounts = runner.get_sorted_accounts()
for account in sorted_accounts:
    runner.switch_to_account(account)
    # ... 自定义逻辑 ...
```

### 自定义过滤逻辑

如果某个任务的过滤逻辑不同于默认实现，可以继承 `MultiAccountRunner` 并重写 `should_process_account`：

```python
class FindJadeRunner(MultiAccountRunner):
    def should_process_account(self, account_info: AccountInfo) -> bool:
        """FindJade 使用基于时间跨度的判断逻辑"""
        now = datetime.now()
        last_time = account_info.last_complete_time
        if now - last_time > timedelta(hours=13):
            return True
        if (last_time.hour >= 18 or last_time.hour < 5) and (18 > now.hour >= 5):
            return True
        if (5 <= last_time.hour < 18) and now.hour >= 18:
            return True
        return False
```

### 自定义排序逻辑

`MultiTasks` 覆写 `get_sorted_accounts` 改为「按邮箱分组使同邮箱角色连续，组间与
组内保持首次出现顺序」，不按 `last_complete_time` 排序：该任务不写这个字段
（账号完成状态由 `progress` 驱动），字段由各来源配置实例自行维护，拿它排序会让
执行顺序随其他实例的运行状态漂移。需要「最久未跑优先」语义的任务仍用默认实现。

## API 参考

### MultiAccountRunner 构造参数

| 参数 | 类型 | 说明 |
|------|------|------|
| `task_name` | `str` | 任务名称，用于日志和进度文件 |
| `config` | `Config` | 配置实例 |
| `device` | `Device` | 设备实例 |
| `account_list` | `List[AccountInfo]` | 账号列表 |
| `need_login` | `bool` | 是否强制登录 |
| `login_time` | `datetime` | 登录时间基准（任务开始时的快照） |
| `update_login_history_func` | `Callable` | 更新账号登录历史的回调 |
| `save_config_func` | `Callable` | 保存配置的回调 |
| `max_retries` | `int` | 最大重试次数，默认 3 |
| `on_account_error` | `Optional[Callable]` | 账号异常回调，可选 |
| `progress` | `Optional[ProgressStore]` | 账号级进度存储（可选）。传入后账号完成判定改由进度文件驱动，`need_login` / `login_time` 不再生效 |

### 主要方法

| 方法 | 说明 |
|------|------|
| `run(process_func)` | 执行多账号轮转任务的主入口 |
| `get_sorted_accounts()` | 获取排序后的账号列表（已过滤） |
| `should_process_account(account_info)` | 判断是否需要处理该账号 |
| `switch_to_account(account_info)` | 切换到指定账号 |
