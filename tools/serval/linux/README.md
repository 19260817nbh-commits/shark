# Serval Linux/Jetson 端（只转发，不解算）

本目录只放 **Jetson/Linux** 的启动入口。设计前提：Orin NX 算力有限，Linux 端不运行任何协议解算、
不跑绘图/GUI 服务；所有解析（CRC、字段、四元数、时间轴、缓存、保存、离线解码）由
[`../windows/README.md`](../windows/README.md) 端完成。

## 数据链路（二进制 UDP）

```text
自瞄 standard_mpc / standard_debug_mpc
  └ 现有 C++ SerialRecorder 钩子（本次未改动）
       ├ 串口线程：原始 TX/RX 字节 memcpy 入队
       └ 后台线程：打包二进制数据报 → UDP 直发 Windows
                        ↓
              Windows（windows_gui.py）：收包 + 全部解算 + 显示/录制/离线解码
```

- 线格式：`SVL1` + `uint64` 会话 ID + 若干 RMSE 记录（24 字节头：方向/status/长度/
  发送端纳秒 `t_ns`/每方向序号），记录负载就是**线上原始串口帧**（TX 30 / RX 44 字节，含 `SP` 帧头与 CRC16）。
- 校验与精度：每帧保留原始帧内 CRC16，Windows 端逐帧复算；会话 ID + 序号用于去重/缺号统计；
  `t_ns` 是 C++ 在串口读写处用 `steady_clock` 记录的时间（帧级时间戳），Windows 用它作为波形时间轴。
  不修改底层控制/串口代码，因此不引入额外发送调用。
- Linux 端没有解码、没有 GUI、没有 HTTP 服务；唯一额外开销是原有的后台复制/发送线程。

## 推荐入口：`aim_bridge.py`（最低延迟，Windows 全解算）

先在 **Windows** 启动接收端（见 [`../windows/README.md`](../windows/README.md)）：

```powershell
py -3 tools/serval/windows/windows_gui.py --port 9871
```

再在 Jetson 上启动自瞄 + 二进制直发（`192.168.1.100` 换成 Windows IP）：

```bash
cd ~/Desktop/test/shark-main
python3 tools/serval/linux/aim_bridge.py --config configs/balance1.yaml \
    --binary standard_mpc --target 192.168.1.100 --port 9871
```

- `--binary standard_debug_mpc`：同上，调试版视觉程序（本模式不显示相机预览，预览只属于本地 GUI 模式）。
- `--record`：在 Jetson 额外写完整原始 `.bin`（可选，会占磁盘 I/O）。
- 关闭/ Ctrl+C 会停止本启动器启动的视觉进程；Serval 不发送任何云台控制命令，也不改 YAML/标定。
- 视觉程序异常退出（如 SIGABRT/-6）时会自动打印 `vision.log` 末尾，日志在
  `records/serial/aim_<时间>_<pid>/vision.log`。

最低延迟选择：本方案就是 aim 进程的 C++ 钩子直接 UDP 到 Windows，没有中间 Python 中继进程，
也没有 Jetson 侧解码；共享内存/Unix socket 方案需要改底层或加中继，反而多一跳，因此不采用。

## 可选：本机 GUI 模式（会占用 Jetson 算力）

只有在没有 Windows 或需要现场本机看波形时使用；Orin NX 上建议低负载运行：

```bash
# 新 GUI（QtWebEngine，无浏览器）
python3 tools/serval/linux/jetson_gui.py --config configs/balance1.yaml --binary standard_mpc

# 带相机预览
python3 tools/serval/linux/jetson_gui.py --config configs/balance1.yaml --binary standard_debug_mpc

# 旧 PyQtGraph 窗口
python3 tools/serval/linux/jetson_gui.py --classic --config configs/balance1.yaml --binary standard_mpc
```

一键桌面图标（仅用于本地 GUI 模式）：

```bash
bash tools/serval/linux/install_jetson_gui.sh
# 之后双击桌面「Serval 串口解算台」，或：
tools/serval/linux/serval_gui.sh
```

## 其他入口

| 文件 | 用途 |
| --- | --- |
| `aim_bridge.py` | 启动自瞄，二进制 UDP 直发 Windows；Linux 不解算（推荐） |
| `jetson_gui.py` | 本机新 GUI；`--classic` 回退经典窗口 |
| `native_watch.py` | 旧启动器：启动自瞄 + 本地经典窗口/远端 UDP |
| `jetson_scope.py` | 只接收 UDP 的本机经典窗口 |
| `orin_watch.py` | 浏览器网页模式（本机 HTTP + 日志跟随） |
| `start.bat` | 浏览器模式快速启动（Windows 批处理，路径已适配） |
| `serval_gui.sh` / `install_jetson_gui.sh` / `serval-jetson-gui.desktop.in` | 本机 GUI 一键入口与桌面图标 |

安装依赖与常用参数见 [`../README.md`](../README.md)。
