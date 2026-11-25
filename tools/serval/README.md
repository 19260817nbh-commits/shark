# Serval 运行指令

运行命令见下文。界面截图和功能介绍见 [功能与原理说明](docs/功能与原理说明.md)。

| 位置 | 内容 |
| --- | --- |
| 顶层 | Jetson、Windows、网页和终端工具的启动入口 |
| `save/` | 离线解码、图形窗口和示意图脚本 |
| `lib/` | 共享协议、录制、UDP 接收和界面实现 |
| `web/` | 浏览器页面、样式和脚本 |
| `docs/` | 功能说明与截图 |
| `tests/` | Python 和网页测试 |
| `maintenance/` | 旧版本的诊断和配置迁移脚本 |

## 1. Orin 更新和编译（原生模式首次必须编译）

### 1.1 正常更新

```bash
cd ~/Desktop/test/shark-main
git pull --ff-only origin main
cmake --build build --target standard_mpc -j2
```

### 1.2 放弃本地修改，用 GitHub 最新版覆盖

先停止自瞄和监听。以下命令会丢弃已跟踪文件的本地修改及当前分支上未推送的提交，
包括 YAML 配置与标定参数；同时删除未跟踪文件/目录。Git 忽略的 `build/`、日志和记录目录保留。

```bash
cd ~/Desktop/test/shark-main
git fetch origin && git reset --hard origin/main && git clean -fd
cmake --build build --target standard_mpc -j2
```

仅在明确不要本地修改时使用 1.2；更新后检查实际使用的 YAML 串口和标定参数。

### 1.3 Windows PowerShell 5.1 覆盖更新

在 Windows 的 `shark` 克隆目录执行。先用 `git remote -v` 确认 `origin` 对应 `19260817nbh-commits/shark`。覆盖会清除本地修改和未跟踪文件，影响范围见第 1.2 节。

```powershell
git remote -v
```

```powershell
git fetch origin
if ($LASTEXITCODE -eq 0) {
    git reset --hard origin/main
    if ($LASTEXITCODE -eq 0) {
        git clean -fd
    }
}
```

配置使用物理串口；不开火测试设置：

```yaml
com_port: "/dev/ttyACM0"
auto_fire: false
```

## 2. 原生窗口直接显示在 Orin

安装一次：

```bash
sudo apt update
sudo apt install -y python3-pyqt5 python3-pyqtgraph
```

在 Orin 桌面终端运行。仅显示波形和数据包：

```bash
cd ~/Desktop/test/shark-main
python3 tools/serval/native_watch.py --config configs/balance1.yaml --binary standard_mpc
```

同屏显示相机原始预览、波形和数据包：

```bash
cd ~/Desktop/test/shark-main
cmake --build build --target standard_debug_mpc -j2
python3 tools/serval/native_watch.py --config configs/balance1.yaml --binary standard_debug_mpc
```

相机预览默认目标为 60 FPS。设为 120 FPS：

```bash
python3 tools/serval/native_watch.py --config configs/balance1.yaml --binary standard_debug_mpc --preview-fps 120
```

同时保存完整串口日志：

```bash
python3 tools/serval/native_watch.py --config configs/balance1.yaml --binary standard_mpc --record
```

## 3. 原生窗口显示在 Windows，Orin 直接通过局域网发送

若在 Windows 单独使用客户端，请复制完整的 `tools/serval` 目录。在复制后的 `serval` 目录打开 PowerShell，安装一次：

```powershell
py -3 -m pip install -r requirements_native.txt
ipconfig
```

记下电脑当前联网适配器的 IPv4 地址。先在电脑启动接收窗口：

```powershell
py -3 native_scope.py --port 9871
```

若使用完整 Git 仓库，在仓库根目录运行：

```powershell
py -3 -m pip install -r tools/serval/requirements_native.txt
py -3 tools/serval/native_scope.py --port 9871
```

在 Orin 本机终端启动发送；把 `192.168.1.100` 换成 Windows 电脑 IP：

```bash
cd ~/Desktop/test/shark-main
python3 tools/serval/native_watch.py --config configs/balance1.yaml --target 192.168.1.100 --port 9871
```

需要完整原始记录时加 `--record`：

```bash
python3 tools/serval/native_watch.py --config configs/balance1.yaml --target 192.168.1.100 --port 9871 --record
```

仅在 Windows 防火墙拦截时，以管理员 PowerShell 放行本地子网 UDP 9871：

```powershell
New-NetFirewallRule -DisplayName "Serval UDP 9871" -Direction Inbound -Action Allow -Protocol UDP -LocalPort 9871 -RemoteAddress LocalSubnet
```


## 4. 已经单独启动自瞄，只添加原生观测

先启动对应的接收窗口。自瞄需要重新启动一次以启用 UDP。

Orin 桌面

```bash
python3 tools/serval/jetson_scope.py --host 127.0.0.1 --port 9871
```

Windows PowerShell

```powershell
py -3 tools/serval/native_scope.py --port 9871
```

Orin 本机显示：

```bash
cd ~/Desktop/test/shark-main
SP_SERIAL_UDP=127.0.0.1:9871 SP_LOG_LEVEL=info ./build/standard_mpc configs/balance1.yaml
```

Windows 显示（替换电脑 IP）：

```bash
SP_SERIAL_UDP=192.168.1.100:9871 SP_LOG_LEVEL=info ./build/standard_mpc configs/balance1.yaml
```

需要视觉 FPS 和完整录制时使用第 2/3 节。

## 5. 后台运行 Orin 发送程序

在 Orin 本机执行；替换电脑 IP。关闭终端不停止自瞄：

```bash
cd ~/Desktop/test/shark-main
nohup python3 tools/serval/native_watch.py --config configs/balance1.yaml --target 192.168.1.100 --port 9871 > /tmp/serval-native.log 2>&1 < /dev/null &
```

查看启动信息：

```bash
tail -n 20 /tmp/serval-native.log
```

## 6. 停止程序

```bash
pkill -TERM -f 'tools/serval/native_watch[.]py|tools/serval/orin_watch[.]py'
pkill -TERM -f 'tools/serval/(jetson_scope|native_scope|monitor)[.]py'
pkill -TERM -f '(^|/)(standard_mpc|standard_debug_mpc)([[:space:]]|$)'
```

## 7. 保留的网页方式

Orin 启动：

```bash
cd ~/Desktop/test/shark-main
python3 tools/serval/orin_watch.py --config configs/balance1.yaml --binary build/standard_mpc --host 0.0.0.0
```

电脑浏览器访问 `http://Orin的IP:8765`；Orin 本机访问 `http://127.0.0.1:8765`。

## 8. 可选 SSH 网页转发

电脑 PowerShell 执行（替换 Orin IP），保持此窗口：

```powershell
ssh -o ExitOnForwardFailure=yes -L 8766:127.0.0.1:8765 shark@10.239.128.43
```

登录后运行：

```bash
cd ~/Desktop/test/shark-main
python3 tools/serval/orin_watch.py --config configs/balance1.yaml --binary build/standard_mpc --host 127.0.0.1
```

电脑浏览器访问 `http://127.0.0.1:8766`。

## 9. GUI 录制与离线解码

在 Jetson 桌面运行：

```bash
cd ~/Desktop/test/shark-main
python3 tools/serval/native_watch.py --config configs/balance1.yaml --binary standard_mpc
```

1. 点「开始录制」，选择一个新 `.bin` 文件。窗口会持续写入之后收到的原始包。
2. 点「停止录制」，等状态显示「录制结束」。已有的「保存 .bin」用于一次性导出当前缓存。
3. 点「离线解码」。刚录完的文件会自动填入，也可以另选 `.bin`。
4. 点「启动解码」。完成后点「打开输出目录」，查看 `tx.csv`、`rx.csv` 和可选的 `waveform.png`。

GUI 录制包含该窗口收到的包。需要在发送端全程写入原始记录时，按第 2 节使用 `--record`。

Jetson 需要绘制 PNG 时安装一次：

```bash
sudo apt install -y python3-matplotlib
```

本机回放（使用网页，无需 Orin 在线）：

```bash
python tools/serval/monitor.py --replay "你的记录.bin" --open
```

Orin 终端实时查看已有串口记录：

```bash
python3 tools/serval/packet_dump.py --follow --direction both --hz 2
```

Orin 单独打开离线窗口：

```bash
python3 tools/serval/save/offline_decode.py
```

Windows 离线解码从工程根目录的 PowerShell 单独打开。实时接收仍使用第 3 节的 `native_scope.py`：

```powershell
py -3 tools/serval/save/offline_decode.py
```

命令行离线解码：

```bash
python3 tools/serval/save/decode_serial_log.py 你的记录.bin --plot
```

电脑 PowerShell 下载 Orin 记录：

```powershell
scp -r shark@10.239.128.43:/home/shark/Desktop/test/shark-main/records/serial .
```

## 10. 开发验证

```bash
python3 -B -m unittest discover -s tools/serval -p 'test_*.py' -q
node tools/serval/tests/test_signals.cjs
node tools/serval/tests/test_diagnostics.cjs
```
