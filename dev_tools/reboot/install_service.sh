#!/bin/bash

echo "正在安装OAS守护进程服务..."

# 获取脚本所在目录
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# 获取项目根目录
PROJECT_ROOT="$(dirname "$(dirname "$SCRIPT_DIR")")"

# 创建systemd服务文件
SERVICE_FILE="/etc/systemd/system/oas-daemon.service"
sudo tee $SERVICE_FILE > /dev/null <<EOF
[Unit]
Description=OAS Daemon Service
After=network.target

[Service]
Type=simple
User=$USER
WorkingDirectory=$PROJECT_ROOT
ExecStart=/usr/bin/python3 $SCRIPT_DIR/reboot_daemon.py --config-file $SCRIPT_DIR/daemon_config.json
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
EOF

# 重新加载systemd配置
sudo systemctl daemon-reload

# 启用服务
sudo systemctl enable oas-daemon

# 启动服务
sudo systemctl start oas-daemon

echo ""
echo "OAS守护进程服务已安装并启动。"
echo ""
echo "常用管理命令："
echo "  启动服务: sudo systemctl start oas-daemon"
echo "  停止服务: sudo systemctl stop oas-daemon"
echo "  重启服务: sudo systemctl restart oas-daemon"
echo "  查看状态: sudo systemctl status oas-daemon"
echo "  查看日志: journalctl -u oas-daemon -f"
echo ""