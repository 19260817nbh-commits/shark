#!/bin/bash

PROJECT_DIR="/home/shark/JNU_vision_26"
WATCHDOG_SCRIPT="$PROJECT_DIR/watchdog.sh"
AUTOSTART_DIR="$HOME/.config/autostart"
DESKTOP_FILE="$AUTOSTART_DIR/jnu_vision.desktop"

# 确保看门狗脚本有执行权限
chmod +x "$WATCHDOG_SCRIPT"

# 创建 autostart 目录
mkdir -p "$AUTOSTART_DIR"

# 创建 .desktop 文件
# 使用 gnome-terminal 启动以便可以看到输出日志
cat > "$DESKTOP_FILE" <<EOF
[Desktop Entry]
Type=Application
Exec=gnome-terminal --working-directory="$PROJECT_DIR" -- bash -c "$WATCHDOG_SCRIPT; exec bash"
Hidden=false
NoDisplay=false
X-GNOME-Autostart-enabled=true
Name=JNU Vision Watchdog
Comment=Auto-start JNU Vision system with Watchdog
EOF

echo "自启动配置已创建: $DESKTOP_FILE"
echo "看门狗脚本位于: $WATCHDOG_SCRIPT"
