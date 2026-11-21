#!/bin/bash

echo "正在停止看门狗和主程序..."

# 1. 先杀掉看门狗脚本，防止它重启程序
# 使用 -f 匹配命令行参数，确保匹配到脚本名
if pgrep -f "watchdog.sh" > /dev/null; then
    pkill -f "watchdog.sh"
    echo "已终止 watchdog.sh"
else
    echo "未发现正在运行的 watchdog.sh"
fi

# 等待一小会儿，确保看门狗进程已退出
sleep 0.5

# 2. 再杀掉主程序
if pgrep -f "standard_mpc" > /dev/null; then
    pkill -f "standard_mpc"
    echo "已终止 standard_mpc"
else
    echo "未发现正在运行的 standard_mpc"
fi

echo "停止操作完成。"
