# OAS 守护进程

OAS（Onmyoji AutoScript）守护进程，通过 REST API + WebSocket 与 OAS Server 通信（与 OASX 客户端方式一致），实现以下功能：

1. **开机自启动**：系统启动时自动拉起 OAS Server 并运行指定实例
2. **多实例状态监控**：通过 WebSocket 长连接实时接收实例状态推送，自动重启挂掉的实例
3. **灵活的多实例配置**：每个实例可独立设置启停、自动重启策略、冷却时间等
4. **定时重启系统**：支持按时间和星期几定时重启电脑

## 架构说明

```
┌─────────────────────┐      REST API       ┌─────────────────────┐
│                     │  GET /test           │                     │
│   RebootDaemon      │  GET /{name}/start   │   OAS Server        │
│   (守护进程)         │  GET /{name}/stop    │   (FastAPI)         │
│                     │  GET /home/kill      │                     │
│                     ├──────────────────────┤                     │
│                     │   WebSocket          │                     │
│                     │  ws://host:port/ws/  │                     │
│                     │  {instance_name}     │                     │
└─────────────────────┘                      └─────────────────────┘
```

**通信方式**（与 OASX 客户端一致）：

| 方式 | 端点 | 用途 |
|------|------|------|
| REST | `GET /test` | 检查 Server 是否在线 |
| REST | `GET /{name}/start` | 启动实例（WebSocket 失败时回退） |
| REST | `GET /{name}/stop` | 停止实例（WebSocket 失败时回退） |
| REST | `GET /home/kill_server` | 关闭 OAS Server |
| WebSocket | `ws://{host}:{port}/ws/{name}` | 实例长连接，接收状态/调度推送 |

**WebSocket 消息协议**：

| 方向 | 消息 | 说明 |
|------|------|------|
| 发送 | `start` | 启动实例 |
| 发送 | `stop` | 停止实例 |
| 发送 | `get_state` | 请求当前状态 |
| 接收 | `{"state": 0-3}` | 状态推送（0=INACTIVE, 1=RUNNING, 2=WARNING, 3=UPDATING） |
| 接收 | `{"schedule": {...}}` | 调度信息推送 |

## 文件说明

| 文件 | 说明 |
|------|------|
| `reboot_daemon.py` | 守护进程主程序 |
| `daemon_config.json` | 守护进程配置文件 |
| `install_startup.bat` | Windows 开机自启安装脚本 |
| `uninstall_startup.bat` | Windows 开机自启卸载脚本 |
| `install_service.sh` | Linux systemd 服务安装脚本 |
| `daemon.log` | 运行日志 |

## 快速开始

### 1. 安装依赖

```bash
pip install websockets schedule nest-asyncio
```

### 2. 编辑配置文件

编辑 `daemon_config.json`，添加需要监控的实例：

```json
{
  "api_host": "127.0.0.1",
  "api_port": 22288,
  "instances": [
    {"name": "oas1", "enabled": true, "auto_restart": true},
    {"name": "oas2", "enabled": true, "auto_restart": true}
  ]
}
```

### 3. 手动测试

```bash
cd dev_tools\reboot
python reboot_daemon.py
```

### 4. 设置开机自启

```cmd
install_startup.bat
```

## 命令行参数

```
python reboot_daemon.py [OPTIONS]

选项:
  --config-file PATH     配置文件路径 (默认: 同目录下 daemon_config.json)
  --api-host HOST        OAS Server 地址 (默认: 127.0.0.1)
  --api-port PORT        OAS Server 端口 (默认: 22288)
  --reboot-time HH:MM   自动重启时间，覆盖配置文件
  --reboot-weekday N     重启星期几 0=周一 6=周日，覆盖配置文件
```

## 故障排除

| 问题 | 排查方向 |
|------|----------|
| 守护进程未启动 | 检查 `daemon.log`、Python 依赖 |
| 实例无法启动 | 检查配置文件名是否存在、模拟器/ADB 连接 |
| WebSocket 连接失败 | 确认 OAS Server 已启动、端口正确 |
| 自动重启不生效 | 检查 `auto_restart` 是否为 true、是否达到 `max_restart_attempts` 上限 |
| 定时重启异常 | 检查时间格式 HH:MM、系统时间、执行权限 |
