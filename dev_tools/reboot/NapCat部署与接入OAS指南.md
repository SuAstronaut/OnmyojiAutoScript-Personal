# NapCat 部署、使用与接入 OAS 指南

## 一、NapCat 简介

NapCat 是基于 NTQQ 的 OneBot 11 协议实现，提供 HTTP API 供外部程序调用 QQ 功能（如获取群消息），是 go-cqhttp 的现代替代方案。

- 官方文档：https://napneko.github.io
- 支持 OneBot 11 标准接口（`get_group_msg_history`、`get_login_info` 等）

---

## 二、安装 NapCat

### 2.1 Shell 版（推荐）

1. 从 [NapCat Release](https://github.com/NapNeko/NapCatQQ/releases) 下载最新版
2. 解压到目标目录，例如 `C:\NapCat`
3. 解压后目录结构应包含：
   - `launcher.bat` — Win11 启动脚本
   - `launcher-win10.bat` — Win10 启动脚本

### 2.2 一键版

1. 下载一键安装包
2. 安装后主程序为 `NapCatWinBootMain.exe`

---

## 三、启动 NapCat

### 3.1 命令行启动（带 QQ 号自动登录）

NapCat 启动时必须带 QQ 号参数才能实现自动登录：

| 系统 | 启动命令 |
|------|---------|
| Win11 | `launcher.bat 123456789` |
| Win10 | `launcher-win10.bat 123456789` |
| 一键版 | `NapCatWinBootMain.exe 123456789` |

### 3.2 首次登录

首次启动会弹出 QQ 登录二维码，用手机 QQ 扫码登录。登录成功后，后续启动会自动登录。

---

## 四、配置 NapCat 网络服务

NapCat 有两套配置文件：

| 文件 | 用途 |
|------|------|
| `napcat.json` / `napcat_<QQ号>.json` | 日志、Hook 等基础设置 |
| `onebot11_<QQ号>.json` | **HTTP 服务器、WebSocket 等网络配置** |

### 4.1 本机运行（OAS 和 NapCat 在同一台电脑）

通过 WebUI 或直接编辑 `onebot11_<QQ号>.json`：

1. 启动 NapCat，查看日志找到 WebUI 地址：
   ```
   [NapCat] [WebUi] WebUi User Panel Url: http://127.0.0.1:6099/webui?token=xxxxx
   ```
2. 浏览器打开该地址
3. 进入 **网络配置** → 添加/编辑 HTTP 服务器，配置如下：

| 配置项 | 值 | 说明 |
|--------|-----|------|
| name | `OAS` | 自定义名称 |
| enable | ✅ | 启用 |
| host | `127.0.0.1` | 本机监听 |
| port | `3000` | 端口号 |
| token | （留空或自定义） | 鉴权密钥，局域网建议设置 |

### 4.2 远程运行（OAS 和 NapCat 在不同电脑）

**NapCat 端（例如 192.168.1.101）**：

`onebot11_<QQ号>.json` 中 `host` 必须改为 `0.0.0.0`，否则拒绝远程连接：

```json
{
  "network": {
    "httpServers": [
      {
        "name": "OAS",
        "enable": true,
        "host": "0.0.0.0",
        "port": 3000,
        "enableCors": true,
        "enableWebsocket": true,
        "messagePostFormat": "array",
        "token": "你的密码",
        "debug": false
      }
    ],
    "httpClients": [],
    "websocketServers": [],
    "websocketClients": []
  },
  "musicSignUrl": "",
  "enableLocalFile2Url": false,
  "parseMultMsg": false
}
```

**Windows 防火墙放行**（在 NapCat 主机上以管理员 PowerShell 执行）：

```powershell
netsh advfirewall firewall add rule name="NapCat API" dir=in action=allow protocol=tcp localport=3000
```

> ⚠️ `host` 设为 `0.0.0.0` 意味着所有网络接口均可访问，**务必设置 token** 防止未授权调用。

---

## 五、接入 OAS

OAS 通过 NapCat 的 OneBot HTTP API 获取 QQ 群消息，用于道馆突破的群消息触发功能。

### 5.1 网络拓扑

```
┌──────────────────────┐       HTTP API        ┌──────────────────────┐
│   OAS 主机            │ ───────────────────→  │   NapCat 主机        │
│   (192.168.1.200)     │   http://IP:3000      │   (192.168.1.101)    │
│                       │                       │   QQ 机器人运行中     │
│   Dokan 任务运行      │ ←───────────────────  │   OneBot 11 服务     │
│   道馆突破触发        │   群消息历史数据       │   监听QQ群消息        │
└──────────────────────┘                       └──────────────────────┘
```

如果 OAS 和 NapCat 在同一台电脑，IP 均为 `127.0.0.1`。

### 5.2 配置 OAS 道馆任务（Dokan）

在 OAS 界面中，找到 **道馆突破 (Dokan)** 任务的 `QQGroupTriggerConfig` 配置：

| 字段 | 本机部署 | 远程部署 |
|------|---------|---------|
| `enable` | ✅ | ✅ |
| `endpoint` | `http://127.0.0.1:3000` | `http://192.168.1.101:3000` |
| `access_token` | 留空或与 NapCat token 一致 | 与 NapCat token 一致 |
| `group_id` | 监听的 QQ 群号 | 同左 |
| `create_keyword` | `道馆已经创建` | 同左 |
| `create_sender_id` | 触发消息发送者 QQ 号，0 不限制 | 同左 |
| `at_all_sender_id` | @全体成员的 QQ 号，0 与上面一致 | 同左 |
| `require_at_all` | 是否要求同时检测到@全体成员 | 同左 |
| `retry_interval` | 重试间隔（分钟） | 同左 |

### 5.3 配置守护进程（可选）

如果 NapCat 和 OAS 在同一台电脑，可通过守护进程自动管理 NapCat 的启停和崩溃重启。

编辑 `dev_tools/reboot/daemon_config.json`：

```json
{
  "napcat": {
    "enable": true,
    "qq_account": "123456789",
    "endpoint": "http://127.0.0.1:3000",
    "access_token": "你的密码",
    "napcat_dir": "C:\\NapCat",
    "start_cmd": "launcher-win10.bat {qq}",
    "check_interval": 30,
    "fail_count": 3,
    "max_restart_attempts": 5,
    "restart_cooldown": 60
  }
}
```

各字段说明：

| 字段 | 说明 |
|------|------|
| `enable` | 是否启用 NapCat 守护 |
| `qq_account` | 自动登录的 QQ 号 |
| `endpoint` | NapCat HTTP API 地址（用于健康检查） |
| `access_token` | 与 NapCat 的 token 一致 |
| `napcat_dir` | NapCat 安装目录 |
| `start_cmd` | 启动命令，`{qq}` 会被替换为 `qq_account` 的值 |
| `check_interval` | 健康检查间隔（秒） |
| `fail_count` | 连续失败多少次后重启 |
| `max_restart_attempts` | 最大重启尝试次数 |
| `restart_cooldown` | 重启冷却时间（秒） |

**`start_cmd` 支持的格式：**

| 安装方式 | `start_cmd` 值 |
|---------|---------------|
| Shell 版 Win11 | `launcher.bat {qq}` |
| Shell 版 Win10 | `launcher-win10.bat {qq}` |
| 一键版 | `NapCatWinBootMain.exe {qq}` |
| 自定义脚本 | `napcat.bat {qq}` |

> ⚠️ 如果 NapCat 运行在远程主机，守护进程无法远程管理进程，应设 `"enable": false`。需要在 NapCat 主机上单独运行 `reboot_daemon.py`。

---

## 六、验证连通性

### 6.1 检查 NapCat 是否运行

浏览器访问：`http://<NapCat-IP>:3000/get_login_info`

应返回类似：
```json
{"status":"ok","data":{"user_id":123456789,"nickname":"你的昵称"}}
```

### 6.2 检查群消息接口

用 curl 或 Postman 发送 POST 请求：

```bash
curl -X POST http://<NapCat-IP>:3000/get_group_msg_history \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer 你的token" \
  -d '{"group_id": 你的群号, "count": 5}'
```

应返回群消息历史数据。

### 6.3 OAS 端验证

启动 OAS 的道馆突破任务，观察日志是否出现：
- `QQ群暂无消息` — 连接正常，群内暂无新消息
- `NapCat无法连接` — 检查 endpoint、防火墙、NapCat 是否运行
- `QQ群消息API返回错误` — 检查 token 是否一致

---

## 七、常见问题

### Q1: 启动 NapCat 后没有自动登录？
启动命令必须带 QQ 号参数，例如 `launcher-win10.bat 123456789`。不带参数只会弹出登录窗口。

### Q2: 远程连接 NapCat 报 ConnectionError？
1. 检查 `onebot11_<QQ号>.json` 中 `host` 是否为 `0.0.0.0`
2. 检查 Windows 防火墙是否放行了端口
3. 确认 NapCat 主机 IP 可达：`ping 192.168.1.101`

### Q3: API 返回 401 或 token 错误？
OAS 的 `access_token` 必须与 NapCat `onebot11_<QQ号>.json` 中的 `token` 完全一致。都不设 token 也可以，但局域网环境不建议。

### Q4: 道馆任务不触发？
1. 确认 `group_id` 正确
2. 确认 `create_keyword` 与群内消息一致
3. 确认 `create_sender_id` 设置正确（0 表示不限制发送者）
4. 道馆触发仅监控 21:00 之后的消息

### Q5: 守护进程无法启动 NapCat？
1. 确认 `napcat_dir` 路径正确
2. 确认 `start_cmd` 与实际启动脚本文件名一致
3. 确认 `qq_account` 已填写
