#!/usr/bin/env bash
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CONFIG_PATH="${1:-configs/balance1.yaml}"

cd "$PROJECT_DIR"

echo "[debug] stopping watchdog and production vision processes..."

for pid in $(pgrep -f "$PROJECT_DIR/watchdog\\.sh" || true); do
  if [ "$pid" != "$$" ]; then
    kill -TERM "$pid" 2>/dev/null || true
  fi
done

for name in standard_mpc standard_debug_mpc auto_aim_debug_mpc; do
  pkill -INT -x "$name" 2>/dev/null || true
done

sleep 1

if [ ! -x ./build/standard_debug_mpc ]; then
  echo "[debug] build/standard_debug_mpc not found; building first..."
  cmake -B build
  cmake --build build -j"$(nproc)" --target standard_debug_mpc
fi

echo "[debug] starting standard_debug_mpc with $CONFIG_PATH"
exec ./build/standard_debug_mpc "$CONFIG_PATH"
