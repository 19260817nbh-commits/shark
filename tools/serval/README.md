# Serval 运行指令

本文件只保留通用安装、启动、停止和开发验证命令。功能原理、参数含义、精度、实测数据与窗口字段说明见
[功能与原理说明](docs/功能与原理说明.md)。

| 位置 | 内容 |
| --- | --- |
| 顶层 | Jetson、Windows、网页和终端工具的启动入口 |
| `save/` | 离线解码、图形窗口和示意图脚本 |
| `lib/` | 共享协议、录制、UDP 接收和界面实现 |
| `web/` | 浏览器页面、样式和脚本 |
| `docs/` | 功能与原理说明、截图 |
| `tests/` | Python/网页测试、GUI 冒烟、高频基准 |
| `maintenance/` | 旧版本的诊断和配置迁移脚本 |

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

先停止自瞄和监听。以下命令把 `origin` 指向目标仓库，并用 `main` 覆盖本地：丢弃已跟踪文件的本地修改、
未推送提交（包括 YAML 与标定参数），并删除未跟踪文件；Git 忽略的 `build/`、日志和记录目录保留。
仅在明确不要本地修改时使用。

```bash
cd ~/Desktop/test/shark-main
git remote set-url origin https://github.com/19260817nbh-commits/shark.git
git fetch origin main
git reset --hard origin/main
git clean -fd
cmake --build build --target standard_mpc -j2
```

### 1.3 Windows PowerShell 5.1 覆盖更新

在 Windows 的 `shark` 克隆目录执行。目标仓库/分支同上，影响范围同 1.2。

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

只有在确认本地版本应当完全取代远程 `main` 时执行；会重写远程 main 历史，其他协作者需要重新同步。
`--force-with-lease` 会在远程 main 已被他人更新时拒绝覆盖，比 `--force` 安全。

```bash
git push --force-with-lease origin HEAD:main
```

物理串口 / 不开火测试配置：

```yaml
com_port: "/dev/ttyACM0"
auto_fire: false
```

## 2. 安装依赖

### 2.1 Jetson 桌面 GUI（无浏览器）

```bash
sudo apt update
sudo apt install -y python3-pyqt5 python3-pyqtgraph python3-pyqt5.qtwebengine
```

发行版没有 `python3-pyqt5.qtwebengine` 时：

```bash
python3 -m pip install -r tools/serval/requirements_native.txt
```

### 2.2 Windows 桌面 GUI / 经典窗口

```powershell
py -3 -m pip install -r tools/serval/requirements_native.txt
```

### 2.3 浏览器网页 / 串口桥接

```powershell
py -3 -m pip install -r tools/serval/requirements.txt
```

```bash
python3 -m pip install -r tools/serval/requirements.txt
```

## 3. 启动

### 3.1 Jetson 桌面 GUI（QtWebEngine，无浏览器）

只显示波形和数据包：

```bash
cd ~/Desktop/test/shark-main
python3 tools/serval/jetson_gui.py --config configs/balance1.yaml --binary standard_mpc
```

带相机原始预览（调试版，`--preview-fps` 可选 1–120）：

```bash
cmake --build build --target standard_debug_mpc -j2
python3 tools/serval/jetson_gui.py --config configs/balance1.yaml --binary standard_debug_mpc
```

一键桌面入口（首次安装；之后双击桌面「Serval 串口解算台」，或命令行 `serval_gui.sh`）：

```bash
bash tools/serval/install_jetson_gui.sh
tools/serval/serval_gui.sh
```

其他常用项：`--record` 写完整原始日志；`--classic` 用旧 PyQtGraph 窗口；`--push-hz/--capacity/--no-low-priority` 见第 4 节。

### 3.2 Windows 桌面 GUI（远程接收 UDP）

```powershell
py -3 tools/serval/windows_gui.py --port 9871
```

或直接双击 `tools\serval\start_gui.bat`。

Orin 端启动视觉并发送到电脑 IP（替换 `192.168.1.100`）：

```bash
cd ~/Desktop/test/shark-main
python3 tools/serval/native_watch.py --config configs/balance1.yaml --target 192.168.1.100 --port 9871
```

防火墙拦截时（管理员 PowerShell）：

```powershell
New-NetFirewallRule -DisplayName "Serval UDP 9871" -Direction Inbound -Action Allow -Protocol UDP -LocalPort 9871 -RemoteAddress LocalSubnet
```

### 3.3 经典原生窗口（PyQtGraph）

Orin：

```bash
python3 tools/serval/native_watch.py --config configs/balance1.yaml --binary standard_mpc
python3 tools/serval/native_watch.py --config configs/balance1.yaml --binary standard_debug_mpc --preview-fps 120
```

Windows：

```powershell
py -3 tools/serval/native_scope.py --port 9871
```

也可从两个新入口回退：

```bash
python3 tools/serval/jetson_gui.py --classic --config configs/balance1.yaml --binary standard_mpc
py -3 tools/serval/windows_gui.py --classic --port 9871
```

### 3.4 已经单独启动自瞄，只添加观测

先启动接收窗口（`jetson_scope.py` / `native_scope.py` 或新 GUI），再用环境变量启动视觉：

```bash
cd ~/Desktop/test/shark-main
SP_SERIAL_UDP=127.0.0.1:9871 SP_LOG_LEVEL=info ./build/standard_mpc configs/balance1.yaml
```

```bash
SP_SERIAL_UDP=192.168.1.100:9871 SP_LOG_LEVEL=info ./build/standard_mpc configs/balance1.yaml
```

### 3.5 浏览器网页方式

```bash
cd ~/Desktop/test/shark-main
python3 tools/serval/orin_watch.py --config configs/balance1.yaml --binary build/standard_mpc --host 0.0.0.0
```

浏览器访问 `http://Orin的IP:8765`（本机 `http://127.0.0.1:8765`）。

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
python3 tools/serval/orin_watch.py --config configs/balance1.yaml --binary build/standard_mpc --host 127.0.0.1
```

浏览器访问 `http://127.0.0.1:8766`。

## 4. 常用参数

| 参数 | 适用入口 | 作用 |
| --- | --- | --- |
| `--config PATH` | `jetson_gui.py`、`native_watch.py` | 视觉实际使用的 YAML（只读） |
| `--binary NAME\|PATH` | `jetson_gui.py`、`native_watch.py` | `standard_mpc` / `standard_debug_mpc` 或二进制路径 |
| `--preview-fps 1..120` | 调试版 | 相机预览目标帧率 |
| `--record` | `jetson_gui.py`、`native_watch.py` | 发送端写完整原始 `.bin` |
| `--push-hz auto\|20..240` | `jetson_gui.py`、`windows_gui.py` | SSE 推送/刷新率，auto 上限 60 |
| `--capacity N` | 同上 | 历史帧缓存，默认 60000，范围 10000–500000 |
| `--no-low-priority` | 同上 | 不降低 GUI 进程优先级 |
| `--classic` | `jetson_gui.py`、`windows_gui.py` | 回退旧 PyQtGraph 窗口 |
| `--port N` | 同上 | UDP 端口（Jetson 本机回环 / Windows 监听） |
| `--host ADDR` | `windows_gui.py`、`scope`/`monitor` | UDP/HTTP 监听地址 |
| `--source-ip IP` | `windows_gui.py`、scope、monitor | 只接受指定来源 IP |
| `--refresh 10..60` | `jetson_scope.py`、`native_scope.py` | 经典窗口绘制刷新率 |

## 5. 录制、保存与离线解算

桌面 GUI 顶部「本地工具」按钮：保存 `.bin`、开始/停止录制、离线解码、打开输出目录。

经典窗口按钮：「开始录制」「停止录制」「离线解码」「启动解码」「打开输出目录」。

命令行离线解算：

```bash
python3 tools/serval/save/decode_serial_log.py 你的记录.bin --plot
python3 tools/serval/save/offline_decode.py
py -3 tools/serval/save/offline_decode.py
```

终端查看已有记录 / 下载 Orin 记录：

```bash
python3 tools/serval/packet_dump.py --follow --direction both --hz 2
```

```powershell
scp -r shark@10.239.128.43:/home/shark/Desktop/test/shark-main/records/serial .
```

绘制 PNG 需要 Matplotlib：

```bash
sudo apt install -y python3-matplotlib
```

## 6. 后台运行与停止

Orin 后台发送（替换电脑 IP；关闭终端不停止自瞄）：

```bash
cd ~/Desktop/test/shark-main
nohup python3 tools/serval/native_watch.py --config configs/balance1.yaml --target 192.168.1.100 --port 9871 > /tmp/serval-native.log 2>&1 < /dev/null &
tail -n 20 /tmp/serval-native.log
```

停止（按需取用）：

```bash
pkill -TERM -f 'tools/serval/(native_watch|orin_watch|jetson_gui)[.]py'
pkill -TERM -f 'tools/serval/(jetson_scope|native_scope|monitor|windows_gui)[.]py'
pkill -TERM -f '(^|/)(standard_mpc|standard_debug_mpc)([[:space:]]|$)'
```

## 7. 开发验证

```bash
python3 -B -m unittest discover -s tools/serval -p 'test_*.py' -q
node tools/serval/tests/test_signals.cjs
node tools/serval/tests/test_diagnostics.cjs
node tools/serval/tests/test_web_frontend.cjs
```

当前基线：Python 93 项通过、6 项 POSIX 用例在 Windows 跳过；Node 3 组测试通过。

桌面 GUI 冒烟/端到端（无 QtWebEngine 的机器自动跳过）：

```bash
cd tools/serval
python tests/gui_smoke.py --demo
python tests/gui_smoke.py
python tests/gui_smoke.py --camera
python tests/gui_e2e_live.py
python tests/gui_e2e_live.py --rate 500 --push-hz 60
python tests/high_rate_bench.py --rate 250 --duration 10
```

截图与结果保存在临时目录或 `docs/images/`；Jetson 真机性能请用 `tests/high_rate_bench.py` 实测。
