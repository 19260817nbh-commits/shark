# Serval 运行指令

本文件只保留安装、启动、停止和开发验证命令；功能原理、数据通路、精度与实测数据见
[功能与原理说明](docs/功能与原理说明.md)。

平台代码已分目录：

| 目录 | 内容 |
| --- | --- |
| `linux/` | Jetson/Linux 端：启动自瞄、二进制 UDP 直发 Windows；**不解算**；保留可选本机 GUI |
| `windows/` | Windows 端：接收二进制流、全部 CRC/协议解算、GUI、录制/离线解码 |
| `lib/`、`web/`、`save/` | 两端共享的协议/录制/界面/离线解码库（只维护一份） |
| `monitor.py`、`packet_dump.py` | 浏览器模式后端与终端工具 |
| `docs/` | 功能与原理说明、截图 |
| `tests/` | Python/网页测试、GUI 冒烟、高频基准 |
| `maintenance/` | 旧版本诊断与配置迁移脚本 |

低算力推荐链路：**Linux 只复制/广播原始二进制帧，Windows 全部解算**；本机 GUI 只作为没有 Windows 时的备用。

## 1. Orin 更新与编译

首次使用原生模式必须编译。

### 1.1 正常更新

```bash
cd ~/Desktop/test/shark-main
git pull --ff-only origin main
cmake --build build --target standard_mpc -j2
```

### 1.2 用远程 main 强制覆盖本地（会丢弃本地修改）

目标仓库：`https://github.com/19260817nbh-commits/shark.git`，分支：`main`。
先停止自瞄和监听。以下命令把 `origin` 指向目标仓库并用 `main` 覆盖本地：丢弃已跟踪文件修改、未推送提交
（含 YAML/标定），删除未跟踪文件；Git 忽略的 `build/`、日志、记录保留。仅在明确不要本地修改时使用。

```bash
cd ~/Desktop/test/shark-main
git remote set-url origin https://github.com/19260817nbh-commits/shark.git
git fetch origin main
git reset --hard origin/main
git clean -fd
cmake --build build --target standard_mpc -j2
```

### 1.3 Windows PowerShell 5.1 覆盖更新

在 Windows 的 `shark` 克隆目录执行；目标仓库/分支同上，影响范围同 1.2。

```powershell
git remote -v
git remote set-url origin https://github.com/19260817nbh-commits/shark.git
git fetch origin main
if ($LASTEXITCODE -eq 0) {
    git reset --hard origin/main
    if ($LASTEXITCODE -eq 0) {
        git clean -fd
    }
}
```

### 1.4 用本地强制覆盖远程 main（危险，force push）

仅当本地版本应完全取代远程 `main` 时使用；会重写远程 main 历史。`--force-with-lease` 比 `--force` 安全。

```bash
git push --force-with-lease origin HEAD:main
```

物理串口 / 不开火测试配置：

```yaml
com_port: "/dev/ttyACM0"
auto_fire: false
```

## 2. 安装依赖

### 2.1 Jetson：低算力直发模式（推荐，无需 Qt）

`linux/aim_bridge.py` 只用 Python 标准库 + 现有视觉二进制，**不需要 PyQt/QtWebEngine**。
确认编译产物存在即可：

```bash
cd ~/Desktop/test/shark-main
cmake --build build --target standard_mpc -j2
```

如果也要在 Jetson 本机看 GUI（可选）：

```bash
sudo apt update
sudo apt install -y python3-pyqt5 python3-pyqtgraph python3-pyqt5.qtwebengine
# 绘制离线波形 PNG 可选
sudo apt install -y python3-matplotlib
```

### 2.2 Windows：接收 + 解算 + GUI

```powershell
py -3 -m pip install -r tools/serval/requirements_native.txt
```

### 2.3 浏览器模式 / 串口桥接（可选）

```powershell
py -3 -m pip install -r tools/serval/requirements.txt
```

```bash
python3 -m pip install -r tools/serval/requirements.txt
```

## 3. 启动

### 3.1 推荐：Jetson 二进制 UDP 直发 Windows（Windows 全解算）

**先启动 Windows 接收端**：

```powershell
py -3 tools/serval/windows/windows_gui.py --port 9871
```

或双击 `tools\serval\windows\start_gui.bat`。

**再在 Jetson 启动自瞄 + 二进制广播**（`192.168.1.100` 换成 Windows IP）：

```bash
cd ~/Desktop/test/shark-main
python3 tools/serval/linux/aim_bridge.py --config configs/balance1.yaml \
    --binary standard_mpc --target 192.168.1.100 --port 9871
```

```bash
# 需要 Jetson 侧完整原始记录时（可选）
python3 tools/serval/linux/aim_bridge.py --config configs/balance1.yaml \
    --binary standard_mpc --target 192.168.1.100 --port 9871 --record
```

- Linux 端只启动视觉程序并设置观测环境变量；C++ 现有钩子复制原始串口帧并发二进制 UDP，**不做任何解算**。
- Windows 实时页只画 Yaw/Pitch 的 TX（下发）与 RX（云台回传）两张对比波形 + 关键读数；**不显示相机**，
  也不绘制 IMU/速度/原始包表/诊断/延迟/双通信时序面板。
- 传输+解析+推送延迟：默认 `--push-hz auto` 上限 240，回环实测 p95 ≈ 5.3–5.4 ms（500/250 记录/秒/方向），
  目标 15 ms 内；屏幕像素刷新另受 QtWebEngine ~60 FPS 限制（详见 [功能与原理](docs/功能与原理说明.md#延迟预算与-15-ms-目标)）。
- Serval 不发送云台控制命令、不改 YAML/标定；Ctrl+C 停止本启动器启动的视觉进程。
- 视觉异常退出时会自动打印 `vision.log` 末尾（路径 `records/serial/aim_<时间>_<pid>/vision.log`）。
- 实时页面精简不影响离线能力：`.bin` 的离线解码仍完整解析全部字段/CRC/四元数并生成 CSV/波形。

Windows 防火墙首次需放行入站 UDP（管理员 PowerShell）：

```powershell
New-NetFirewallRule -DisplayName "Serval UDP 9871" -Direction Inbound -Action Allow -Protocol UDP -LocalPort 9871 -RemoteAddress LocalSubnet
```

### 3.2 可选：Jetson 本机 GUI（占用 Jetson 算力）

没有 Windows 或需要现场本机观察时使用；Orin NX 上建议低负载：

```bash
python3 tools/serval/linux/jetson_gui.py --config configs/balance1.yaml --binary standard_mpc
python3 tools/serval/linux/jetson_gui.py --config configs/balance1.yaml --binary standard_debug_mpc
python3 tools/serval/linux/jetson_gui.py --classic --config configs/balance1.yaml --binary standard_mpc
```

一键桌面图标（仅本机 GUI 模式）：

```bash
bash tools/serval/linux/install_jetson_gui.sh
tools/serval/linux/serval_gui.sh
```

### 3.3 经典原生窗口

Windows：

```powershell
py -3 tools/serval/windows/native_scope.py --port 9871
py -3 tools/serval/windows/windows_gui.py --classic --port 9871
```

Jetson（启动自瞄 + 本机经典窗口/远端 UDP）：

```bash
python3 tools/serval/linux/native_watch.py --config configs/balance1.yaml --binary standard_mpc
python3 tools/serval/linux/native_watch.py --config configs/balance1.yaml --binary standard_mpc --target 192.168.1.100 --port 9871
python3 tools/serval/linux/jetson_scope.py --host 127.0.0.1 --port 9871
```

### 3.4 已经单独启动自瞄，只添加观测

```bash
SP_SERIAL_UDP=192.168.1.100:9871 SP_LOG_LEVEL=info ./build/standard_mpc configs/balance1.yaml
```

```bash
SP_SERIAL_UDP=127.0.0.1:9871 SP_LOG_LEVEL=info ./build/standard_mpc configs/balance1.yaml
```

### 3.5 浏览器网页方式

```bash
cd ~/Desktop/test/shark-main
python3 tools/serval/linux/orin_watch.py --config configs/balance1.yaml --binary build/standard_mpc --host 0.0.0.0
# 浏览器访问 http://Orin的IP:8765 或 http://127.0.0.1:8765
```

只读监听/回放同一后端：

```bash
python tools/serval/monitor.py --udp 9871 --http-port 8765
python tools/serval/monitor.py --replay "你的记录.bin" --open
```

SSH 转发（电脑 PowerShell，保持窗口）：

```powershell
ssh -o ExitOnForwardFailure=yes -L 8766:127.0.0.1:8765 shark@10.239.128.43
```

```bash
python3 tools/serval/linux/orin_watch.py --config configs/balance1.yaml --binary build/standard_mpc --host 127.0.0.1
```

浏览器访问 `http://127.0.0.1:8766`。

## 4. 常用参数

| 参数 | 入口 | 作用 |
| --- | --- | --- |
| `--config PATH` | `linux/aim_bridge.py`、`linux/jetson_gui.py`、`linux/native_watch.py` | 视觉实际使用的 YAML（只读） |
| `--binary NAME\|PATH` | 同上 | `standard_mpc` / `standard_debug_mpc` 或二进制路径 |
| `--target IP` | `linux/aim_bridge.py`、`linux/native_watch.py` | Windows 接收端 IPv4（二进制 UDP 目的地） |
| `--port N` | 所有入口 | UDP 端口，默认 9871 |
| `--record` | `linux/aim_bridge.py`、`linux/jetson_gui.py`、`linux/native_watch.py` | Jetson 侧写完整原始 `.bin` |
| `--preview-fps 1..120` | `linux/jetson_gui.py`、`linux/native_watch.py` | 仅旧经典本地窗口的相机预览；精简实时页面不显示相机 |
| `--push-hz auto\|20..240` | `linux/jetson_gui.py`、`windows/windows_gui.py` | 页面 SSE 推送率，auto 上限 240（传输+解析目标 ≤15 ms） |
| `--capacity N` | 同上 | 历史帧缓存，默认 60000，范围 10000–500000 |
| `--no-low-priority` | 同上 | 不降低 GUI 进程优先级 |
| `--classic` | 两个 GUI 入口 | 回退旧 PyQtGraph 窗口 |
| `--host ADDR` | `windows/windows_gui.py`、scope、monitor | 监听地址 |
| `--source-ip IP` | `windows/windows_gui.py`、scope、monitor | 只接受指定来源 IP |
| `--refresh 10..60` | `windows/native_scope.py`、`linux/jetson_scope.py` | 经典窗口刷新率 |

## 5. 录制、保存与离线解算

实时页面精简（只画 Yaw/Pitch 位置比对），但**离线解码保留完整解析**：全部字段、CRC/status、四元数与角度、
弹速/弹量、纳秒时间、原始坏帧字节都写入 CSV/结果，不做简化。

Windows GUI 顶部「本地工具」：保存 `.bin`、开始/停止录制、离线解码、打开输出目录。
经典窗口按钮：开始录制/停止录制/离线解码/启动解码/打开输出目录。

命令行离线解算（Windows 或 Jetson 均可）：

```powershell
py -3 tools/serval/save/decode_serial_log.py 你的记录.bin --plot
py -3 tools/serval/save/offline_decode.py
```

```bash
python3 tools/serval/save/decode_serial_log.py 你的记录.bin --plot
python3 tools/serval/save/offline_decode.py
```

终端查看已有记录 / 下载 Jetson 记录：

```bash
python3 tools/serval/packet_dump.py --follow --direction both --hz 2
```

```powershell
scp -r shark@10.239.128.43:/home/shark/Desktop/test/shark-main/records/serial .
```

## 6. 后台运行与停止

Jetson 后台直发（替换 Windows IP；关闭终端不停止自瞄）：

```bash
cd ~/Desktop/test/shark-main
nohup python3 tools/serval/linux/aim_bridge.py --config configs/balance1.yaml \
    --binary standard_mpc --target 192.168.1.100 --port 9871 > /tmp/serval-aim.log 2>&1 < /dev/null &
tail -n 20 /tmp/serval-aim.log
```

停止：

```bash
pkill -TERM -f 'tools/serval/linux/(aim_bridge|native_watch|orin_watch|jetson_gui)[.]py'
pkill -TERM -f 'tools/serval/windows/(windows_gui|native_scope)[.]py'
pkill -TERM -f '(^|/)(standard_mpc|standard_debug_mpc)([[:space:]]|$)'
```

## 7. 开发验证

```bash
python3 -B -m unittest discover -s tools/serval -p 'test_*.py' -q
node tools/serval/tests/test_signals.cjs
node tools/serval/tests/test_web_frontend.cjs
```

当前基线：Python 93 项通过、6 项 POSIX 用例在 Windows 跳过；Node 2 组测试通过。

GUI 冒烟/端到端（无 QtWebEngine 的机器自动跳过）：

```bash
cd tools/serval
python tests/gui_smoke.py --demo
python tests/gui_smoke.py
python tests/gui_e2e_live.py
python tests/gui_e2e_live.py --rate 500 --push-hz 240
python tests/high_rate_bench.py --rate 250 --duration 10
```

传输+解析+推送延迟预算（目标 p95 ≤15 ms）：

```bash
python tests/latency_pipeline.py                 # 默认 240 Hz 推送
python tests/latency_pipeline.py --rate 500 --push-hz 240
python tests/latency_pipeline.py --limit-ms 15
```

截图与结果保存在临时目录或 `docs/images/`；Jetson 真机性能请用 `tests/high_rate_bench.py` 实测。
