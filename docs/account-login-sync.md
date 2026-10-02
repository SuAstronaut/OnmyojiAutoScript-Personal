# 多账号登录时间同步与管家配置锁

所有通过通用切号组件成功进入庭院的角色都会登记登录时间。任务保存配置时，将本批角色时间同步到当前配置的全部账号表；任务结束时补写尚未保存的记录。已完成的任务仍沿用各自的续跑进度，不把登录时间当作任务完成状态。

角色按 `account + character + svr + apple_or_android` 匹配，只更新已有成员，保留列表顺序。相同角色在磁盘上已有较新时间时取较新值，不倒退。多账号通用任务选用其他配置的账号时，同时回写显式传入的来源配置。

## 备份协议

以 `config/qmumu1.json` 为例，备份固定为 `config/.butler_backups/qmumu1.json`。备份是首次实际写入前的完整原文件字节，保留全部字段、编码、BOM、缩进和换行，不添加包装、时间戳或来源字段。

每次保存为一批：多个角色和多个账号表先合并，再备份一次、写入一次。后续批次覆盖同名备份；预览、无变化及无匹配角色不更新备份。在备份目录创建临时文件，依次执行 `write → flush → os.fsync → os.replace`，最后清理临时文件。备份失败即停止本次配置改写。

## 两端共享锁协议

OAS 使用已有的 `filelock.FileLock`，固定路径为：

```text
<OAS>/config/.generations/locks/<配置名>.lock
```

例如向 `qmumu1.json` 回写，锁文件必须是 `config/.generations/locks/qmumu1.lock`，不能使用副本名称。锁文件是独立的稳定文件，禁止以配置 JSON 或备份文件作为锁对象，也不要自行删除锁文件。

OAS 普通配置保存（`ConfigStore.save_background`）保持原有整段持锁（身份锁 + 单配置锁）不变；登录时间来源回写（`ConfigStore.sync_account_login_times`）则是读取在锁外、只有提交校验、备份及覆盖写入使用单配置锁。为了避免读取后被另一方改写，提交时在锁内核对原字节摘要；摘要变化则释放锁、重新读取并合并，不能直接覆盖。OAS 最多重试三次，持续冲突时报错并保留待回写记录。

管家侧直接操作文件时采用相同边界：

```python
from pathlib import Path
from filelock import FileLock

root = Path(oas_root) / 'config'
lock_path = root / '.generations' / 'locks' / f'{config_name}.lock'
lock_path.parent.mkdir(parents=True, exist_ok=True)
# 在锁外读取配置快照、记录摘要，并计算需要保存的修改。
with FileLock(str(lock_path), timeout=10):
    # 提交前核对摘要；若已变化，释放锁后重读重算，不继续覆盖。
    # 快照仍有效时，先备份完整原字节，再原子覆盖配置。
    # 无变化不备份；备份失败必须立即退出，不继续写入。
    ...
```

Windows 使用 `msvcrt.locking` 字节锁，Unix 使用 `flock`；使用同平台的 `filelock.FileLock` 即可互通。仅凭锁文件存在判断占用、或使用 `SoftFileLock`，均不能与 OAS 互斥。

登录时间回写只获取上述单配置锁，不获取全局身份锁。OAS 原有创建、删除、重命名等身份管理操作保留自身的锁规则。持有单配置锁时不要再调用会重新取锁的 `ConfigStore` 方法。如果直接调用 `ConfigStore.sync_account_login_times()`，它已自行处理读取、提交校验、备份和覆盖，无需外层加锁。不同配置逐份处理。普通保存走原有身份事务，不会因本协议改变行为。

本次仅实现 OAS 侧。管家所有配置回写入口采用上述同一协议后，两端才能互斥；文件原子替换本身不能避免两个独立读改写事务互相覆盖。
