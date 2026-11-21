#!/bin/bash

# 切换到项目根目录
cd /home/shark/JNU_vision_26 || exit

# 给予执行权限 (防止 build 后权限丢失)
chmod +x build/standard_mpc
sleep 5
# 定义超时时间（秒）
TIMEOUT=5
# 定义心跳文件
HEARTBEAT_FILE="/tmp/vision_fps_heartbeat"

while true; do
    echo "[$(date)] Starting standard_mpc with standard5.yaml..."
    
    # 初始化心跳时间
    touch "$HEARTBEAT_FILE"

    # 启动程序
    # 使用进程替换 >(...) 来实时分析输出
    # 1. 打印原始输出 (echo "$line")
    # 2. 检测 "FPS" 关键字并更新心跳文件
    ./build/standard_mpc configs/balance1.yaml > >(while read -r line; do 
        echo "$line"
        if [[ "$line" == *"fps"* ]]; then
            touch "$HEARTBEAT_FILE"
        fi
    done) 2>&1 &
    
    PID=$!
    
    # 监控循环
    while kill -0 $PID 2>/dev/null; do
        NOW=$(date +%s)
        LAST_HEARTBEAT=$(stat -c %Y "$HEARTBEAT_FILE")
        DIFF=$((NOW - LAST_HEARTBEAT))
        
        if [ $DIFF -gt $TIMEOUT ]; then
            echo "[$(date)] Error: No FPS output for $DIFF seconds. Killing process $PID..."
            kill -9 $PID
            break
        fi
        sleep 1
    done
    
    wait $PID 2>/dev/null
    EXIT_CODE=$?
    echo "[$(date)] standard_mpc exited with code $EXIT_CODE"
    
    # 防止程序疯狂重启，等待1秒
    sleep 1
done
