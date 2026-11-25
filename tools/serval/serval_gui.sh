#!/usr/bin/env bash
# Serval Jetson one-click launcher: run the vision program and show the web GUI
# inside a native QtWebEngine window (no browser, no SSH).
#
# Usage:
#   tools/serval/serval_gui.sh                          # configs/balance1.yaml + standard_mpc
#   tools/serval/serval_gui.sh --binary standard_debug_mpc   # camera preview + waveforms
#   tools/serval/serval_gui.sh --config configs/hero.yaml --record
#   tools/serval/serval_gui.sh --help                   # all options
set -euo pipefail

HERE="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd -- "$HERE/../.." && pwd)"
cd "$ROOT"

if [[ -z "${DISPLAY:-}" && -z "${WAYLAND_DISPLAY:-}" ]]; then
  echo "错误：未检测到图形桌面（DISPLAY / WAYLAND_DISPLAY 为空）。" >&2
  echo "请从 Orin/Jetson 的桌面终端运行；只向 Windows 发数据时用 native_watch.py --target <电脑IP>。" >&2
  exit 1
fi

echo "Serval GUI · Jetson 无浏览器模式"
echo "工程目录：$ROOT"
echo "启动：python3 tools/serval/jetson_gui.py $*"
exec python3 "$HERE/jetson_gui.py" "$@"
