#!/usr/bin/env bash
# Install a desktop shortcut for the Serval Jetson GUI page.
#
#   bash tools/serval/linux/install_jetson_gui.sh
#
# Checks the QtWebEngine dependency (offers the apt command), makes the scripts
# executable, installs an application-menu entry and a ~/Desktop launcher.
set -euo pipefail

HERE="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
PROJECT="$(cd -- "$HERE/../../.." && pwd)"
DOCS="$(cd -- "$HERE/../docs" && pwd)"
ICON="$DOCS/images/serval-logo.svg"
LAUNCHER="$HERE/serval_gui.sh"
TEMPLATE="$HERE/serval-jetson-gui.desktop.in"
APP_ID="serval-jetson-gui.desktop"

if [[ ! -f "$LAUNCHER" || ! -f "$TEMPLATE" ]]; then
  echo "错误：找不到 serval_gui.sh 或 serval-jetson-gui.desktop.in（目录：$HERE）" >&2
  exit 1
fi
chmod +x "$LAUNCHER" 2>/dev/null || true

webengine_ok() {
  python3 - <<'PY' >/dev/null 2>&1
import importlib.util
import sys
sys.exit(0 if importlib.util.find_spec('PyQt5.QtWebEngineWidgets') else 1)
PY
}

if ! webengine_ok; then
  echo "检测到 QtWebEngine（PyQt5 内嵌浏览器）未安装。"
  echo "Jetson/Ubuntu 安装命令："
  echo "  sudo apt update && sudo apt install -y python3-pyqt5 python3-pyqtgraph python3-pyqt5.qtwebengine"
  read -r -p "现在安装？[y/N] " answer || answer=""
  if [[ "$answer" =~ ^[Yy]$ ]]; then
    sudo apt update
    sudo apt install -y python3-pyqt5 python3-pyqtgraph python3-pyqt5.qtwebengine
  else
    echo "已跳过依赖安装；缺失 QtWebEngine 时 serval_gui.sh 会提示安装命令，可加 --classic 使用旧窗口。"
  fi
fi

mkdir -p "$HOME/.local/share/applications"
APP_FILE="$HOME/.local/share/applications/$APP_ID"
sed -e "s|@SERVAL_DIR@|$HERE|g" -e "s|@DOCS_DIR@|$DOCS|g" -e "s|@PROJECT_DIR@|$PROJECT|g" "$TEMPLATE" > "$APP_FILE"
chmod +x "$APP_FILE"
echo "已安装应用菜单入口：$APP_FILE"

DESKTOP_DIR="$(xdg-user-dir DESKTOP 2>/dev/null || true)"
if [[ -z "$DESKTOP_DIR" || "$DESKTOP_DIR" == "$HOME" ]]; then
  DESKTOP_DIR="$HOME/Desktop"
fi
if [[ -d "$DESKTOP_DIR" ]]; then
  cp "$APP_FILE" "$DESKTOP_DIR/$APP_ID"
  chmod +x "$DESKTOP_DIR/$APP_ID"
  # GNOME/Nautilus requires the trusted flag before a .desktop file may launch.
  gio set "$DESKTOP_DIR/$APP_ID" metadata::trusted true 2>/dev/null || true
  echo "已复制桌面图标：$DESKTOP_DIR/$APP_ID"
  echo "若双击提示“不受信任”，右键选择“允许启动”/Allow Launching 一次即可。"
fi

if command -v update-desktop-database >/dev/null 2>&1; then
  update-desktop-database "$HOME/.local/share/applications" >/dev/null 2>&1 || true
fi

cat <<EOF

安装完成。使用方法：
  1) 双击桌面「Serval 串口解算台」，或在应用菜单搜索 Serval
  2) 命令行直接运行：
       $LAUNCHER
       $LAUNCHER --config configs/balance1.yaml --binary standard_mpc
       $LAUNCHER --binary standard_debug_mpc          # 带相机预览
       $LAUNCHER --classic                            # 旧 PyQtGraph 窗口
  图标：$ICON
  工程：$PROJECT
EOF
