# Serval 运行指令

运行分类与命令如下；功能、精度和影响见 [功能与原理说明](功能与原理说明.md)。

## 1. Orin 更新和编译（原生模式首次必须编译）

```bash
cd ~/Desktop/test/shark-main
git pull --ff-only origin main
cmake --build build --target standard_mpc -j2
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

在 Orin 桌面终端运行（需连接显示器或已有图形桌面）：

```bash
cd ~/Desktop/test/shark-main
python3 tools/serval/native_watch.py --config configs/balance1.yaml
```

同时保存完整串口日志：

```bash
python3 tools/serval/native_watch.py --config configs/balance1.yaml --record
```

## 3. 原生窗口显示在 Windows，Orin 直接通过局域网发送

Windows 解压客户端压缩包后，在客户端目录打开 PowerShell，安装一次：

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

先在 Windows 或 Orin 启动 `native_scope.py`。自瞄需要重新启动一次以启用 UDP。

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
pkill -TERM -f 'tools/serval/native_scope[.]py|tools/serval/monitor[.]py'
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

## 9. 离线回放、终端包解析和记录下载

本机回放（使用网页，无需 Orin 在线）：

```bash
python tools/serval/monitor.py --replay "你的记录.bin" --open
```

Orin 终端实时查看已有串口记录：

```bash
python3 tools/serval/packet_dump.py --follow --direction both --hz 2
```

离线解码：

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
node tools/serval/test_signals.cjs
node tools/serval/test_diagnostics.cjs
```
