#!/usr/bin/env bash
# Serval Jetson local GUI launcher (optional local mode; all-in-one offload
# mode uses aim_bridge.py instead and does no decoding on the Jetson).
#
# Usage:
#   tools/serval/linux/serval_gui.sh                          # configs/balance1.yaml + standard_mpc
#   tools/serval/linux/serval_gui.sh --binary standard_debug_mpc   # camera preview + waveforms
#   tools/serval/linux/serval_gui.sh --config configs/hero.yaml --record
#   tools/serval/linux/serval_gui.sh --help                   # all options
set -euo pipefail

HERE="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd -- "$HERE/../../.." && pwd)"
cd "$ROOT"

if [[ -z "${DISPLAY:-}" && -z "${WAYLAND_DISPLAY:-}" ]]; then
  echo "错误：未检测到图形桌面（DISPLAY / WAYLAND_DISPLAY 为空）。" >&2
  echo "本机无桌面时用 linux/aim_bridge.py 把二进制 UDP 发到 Windows。" >&2
  exit 1
fi

echo "Serval GUI · Jetson 本地模式（会占用 Jetson 算力）"
echo "工程目录：$ROOT"
echo "启动：python3 tools/serval/linux/jetson_gui.py $*"
exec python3 "$HERE/jetson_gui.py" "$@"
