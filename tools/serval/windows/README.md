# Serval Windows 端（全部解算在这里）

本目录只放 **Windows** 的接收/解算/显示入口。Windows 端负责全部工作：

- 接收 Linux/Jetson 发来的**二进制 UDP**（`SVL1` + RMSE 原始串口帧）；
- 会话/序号管理、CRC16 校验、协议字段解析、四元数姿态、时间轴对齐；
- 精简波形显示（Yaw/Pitch 的 TX vs RX）、逐点模式、缺号/迟到/坏包统计；不显示相机和诊断面板；
- 保存 `.bin`、录制、离线解码、打开输出目录。

Linux 端只复制原始串口字节并广播，不解算（见 [`../linux/README.md`](../linux/README.md)）。

## 安装（一次）

```powershell
py -3 -m pip install -r tools/serval/requirements_native.txt
```

Jetson 端需要先启动（见 [`../linux/README.md`](../linux/README.md)），
或使用旧链路由 `native_watch.py --target <Windows-IP>` 直接发送。

## 启动

新桌面 GUI（QtWebEngine 内嵌页面，不需要浏览器）：

```powershell
py -3 tools/serval/windows/windows_gui.py --port 9871
```

或双击 `tools\serval\windows\start_gui.bat`。

经典 PyQtGraph 接收窗口：

```powershell
py -3 tools/serval/windows/native_scope.py --port 9871
# 或
py -3 tools/serval/windows/windows_gui.py --classic --port 9871
```

常用参数：

| 参数 | 作用 |
| --- | --- |
| `--port N` | 监听 UDP 端口（默认 9871，需与 Jetson `--port` 一致） |
| `--host ADDR` | 监听地址，默认 `0.0.0.0` 接受局域网 |
| `--source-ip IP` | 只接受指定 Jetson IPv4，避免同网段其他发送端混入 |
| `--push-hz auto\|20..240` | 页面 SSE 推送率（auto 跟随屏幕但上限 60） |
| `--capacity N` | 历史帧缓存（默认 60000） |
| `--no-low-priority` | 不降低接收端进程优先级 |
| `--classic` | 使用旧 PyQtGraph 窗口 |

Windows 防火墙首次需放行入站 UDP（管理员 PowerShell）：

```powershell
New-NetFirewallRule -DisplayName "Serval UDP 9871" -Direction Inbound -Action Allow -Protocol UDP -LocalPort 9871 -RemoteAddress LocalSubnet
```

## 数据格式（只读参考）

```text
UDP datagram = 'SVL1' + uint64 session
               + N × [ 24B RMSE header (magic 'RMSE', dir, status, len, t_ns, seq, reserved)
                       + raw serial frame (TX 30B / RX 44B, includes 'SP' + CRC16) ]
UDP datagram = 'SVF1' + float64 视觉 FPS
```

时间轴使用 `t_ns`（发送端串口读写处记录），不是 Windows 接收时刻；到达间隔单独用于网络抖动统计。
UDP 不重传：缺号/迟到计数在页面/状态栏显示，长期证据请用 `.bin` 录制。

## 本地工具与离线解算

GUI 顶部「本地工具」：保存 `.bin`、开始/停止录制、离线解码、打开输出目录；解算在后台进程运行。
命令行：

```powershell
py -3 tools/serval/save/decode_serial_log.py 你的记录.bin --plot
py -3 tools/serval/save/offline_decode.py
```

原理、精度边界、15 ms 延迟目标与完整离线解码说明见 [`../docs/功能与原理说明.md`](../docs/功能与原理说明.md)。
