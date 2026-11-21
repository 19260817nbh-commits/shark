# Serval：OrinNX 串口监视与离线回放

面向本工程 `io::Gimbal` 的 Python 工具。默认协议是 **TX 30 字节、RX 44 字节**，包含
`target_id` 和 `enemy_color`。不依赖 ROS、PlotJuggler 或 C++ 编译；Python 3.9+，
串口模式只需 `pyserial`。所有新增代码放在本目录，`save/` 仍只有原来的两个 Python 脚本。

## 功能与接入原则

- 双向字节透明转发；CRC 错误和未知字节也原样转发，不改写控制命令。
- 串口转发、预览解析、磁盘日志分别执行。浏览器约每 100 ms 刷新，关闭浏览器不停止桥接。
- 显示 yaw/pitch 指令与反馈、角速度、角加速度、四元数、目标 ID、敌方颜色和状态。
- 提供 CRC 计数、最大采样间隔和转发调用峰值；这些不是电控确认或单程延迟。
- 可选录制与原工程兼容的 RMSE `.bin` 日志；可在同一界面离线回放。
- 回放能暂停、继续、从头播放和调速；全程不打开物理串口。

链路：

```text
上位机程序 ⇄ 虚拟串口 ⇄ Serval ⇄ 物理串口 ⇄ 云台
                         └─ 预览队列 → 协议解析 → 浏览器
                                      └─ 日志队列 → .bin
```

同一物理串口不能同时被原上位机和工具各自读取。首次接入需要停止上位机，
让工具打开物理串口，再把上位机 `com_port` 改到虚拟串口，重新启动上位机。
停止工具会中断桥接；“暂停曲线”只冻结显示，不停止通信。
本工具不生成运动或开火指令，转发的是原上位机发出的字节。

## 安装与演示

在 `tools/serval` 目录执行：

```bash
python -m pip install -r requirements.txt
python monitor.py --demo --open
```

Linux 使用 `python3` 也可以。演示用模拟数据，不打开设备。
浏览器地址是 `http://127.0.0.1:8765`，端口占用时可加 `--http-port 8766`。
Windows 可双击 `start.bat`，默认打开空白的设备选择界面。

## Orin / Linux 真实串口

以下假设原配置的物理口是 `/dev/ttyACM0`，实际以使用中的 YAML 为准：

```bash
python3 monitor.py --bridge /dev/ttyACM0 --baud 921600 --record --open
```

工具默认创建固定虚拟串口：

```yaml
com_port: "/tmp/serval-gimbal"
```

把上位机配置改成这个路径后重新启动上位机。后续正常重启工具仍使用同一路径；
退出工具会移除自己创建的符号链接。已有路径不会被覆盖，以免干扰其他实例。
多台设备可显式指定不同路径：

```bash
python3 monitor.py --bridge /dev/ttyACM1 --link /tmp/serval-gimbal-2 --record --open
```

如果物理口被占用，先停止使用它的原程序。若工具异常终止后留下符号链接，
先确认没有运行的旧实例，再检查和清理该链接；工具不会擅自覆盖它。

也可执行 `python3 monitor.py --open`，在界面中填写设备、波特率、固定虚拟路径，
勾选“保存原始日志”后启动。HTTP 界面仅监听本机，可在 Orin 的远程桌面中打开。

## Windows 真实串口

先准备一对虚拟串口，例如 COM11 ↔ COM12。工具不安装虚拟串口驱动。

假设物理云台为 COM3，工具打开 COM3 与 COM11，上位机打开 COM12：

```bash
python monitor.py --bridge COM3 --virtual-port COM11 --record --open
```

界面里“虚拟串口 · 工具端”填写 COM11，而不是物理口 COM3。
物理口与虚拟口不能指向同一个设备。

## 日志录制与离线回放

`--record` 自动将日志保存到本目录 `records/`；也可以显式指定文件：

```bash
python3 monitor.py --bridge /dev/ttyACM0 --record /home/robot/logs/gimbal-test.bin
```

不覆盖已有同名日志。界面显示日志路径、写入帧数和失败信息。
日志格式与本工程 `io/gimbal/serial_recorder.*` 一致，可用原脚本解码：

```bash
python save/decode_serial_log.py records/serial_xxx.bin --plot
```

离线回放直接读取该日志，或者读取原工程 `SP_SERIAL_LOG=1` 录制的日志：

```bash
python monitor.py --replay records/serial_xxx.bin --open
```

也可以在未连接设备的界面中选择 `.bin` 文件，点“加载日志”。
网页上传上限为 128 MB；更大文件使用命令行。索引上限为 200 万条记录。
“暂停回放”暂停时间推进；“暂停曲线”仅冻结图形。CRC 错误仍计入统计，
不会作为有效姿态绘图。日志结束时自动暂停，“从头播放”清空旧视图并重新开始。
文件末尾未写完整的最后一条记录会跳过并提示；错误文件头或内部损坏会报错。

日志记录的是识别到的完整帧候选，包括 CRC 错误帧，不是逻辑分析仪的全字节采集。
预览队列或日志队列过载时会统计丢弃，不阻塞控制链路。程序退出时排空日志队列；
存储写入失败会在界面提示，串口仍继续转发。

## 协议

小端、紧凑排列，IEEE 754 float32，无对齐填充。角度原始单位为 rad，
角速度 rad/s，角加速度 rad/s²。CSV 保留原始单位，界面可切换为角度。

| 方向 | 格式 | 长度 |
| --- | --- | --- |
| TX 上位机→云台 | `<2sB6fBH` | 30 字节 |
| RX 云台→上位机 | `<2sB9fHBH` | 44 字节 |

TX：`SP, mode, yaw, yaw_vel, yaw_acc, pitch, pitch_vel, pitch_acc, target_id, crc16`。

RX：`SP, mode, q[wxyz], yaw, yaw_vel, pitch, pitch_vel, bullet_speed, bullet_count, enemy_color, crc16`。

TX mode：0 不控制；1 控制但不开火；2 控制且允许开火。
RX mode：0 空闲；1 自瞄；2 小符；3 大符。敌方颜色：0 蓝、1 红。
二者的 mode 含义不同。CRC16 初值 0xffff、反射多项式 0x8408，校验字节低位在前。
旧工程的 29/43 字节格式不在本版支持范围内，不会自动猜测协议。

解析器处理拆包、粘包、噪声与 CRC 错帧。预览队列丢块后会丢弃未完成的残包重新同步，
防止把缺字节的数据拼成一个假帧。接收时间在转发线程读取数据时记录，
后续解析排队不会改变该时间；同一读块中的帧可能有相同采样时间。

## 怎样理解统计

- **近邻采样角差**：仅在 TX 处于控制模式、两端采样都在 250 ms 内且时间相差不超过
  50 ms 时显示。Yaw 按 ±π 环绕计算，Pitch 直接相减。它是近邻采样差，不是严格同步误差。
- **最大采样间隔**：单方向有效帧的最大时间间隔；不能直接证明物理线路丢包。
- **转发调用峰值**：从读取到写入调用完成的最大耗时，包含驱动等待；
  不代表电机执行时间，也不代表数据已到达电控。
- **CRC 错误**：监视器识别到的候选帧校验错误数；不是准确的线上丢包率。
- **四元数**：保留原始模长，转换欧拉角时归一化。IMU 坐标系与云台关节角可能不同，
  所以独立显示，不直接做跟随误差。

历史缓存为最近 60,000 个有效帧，浏览器最多显示最近 120 秒。
CSV 导出该缓存；长时间实验应同时录制 `.bin`，不要只依赖 CSV。
设备断开或写入失败时停止桥接并提示，手动重新启动；不重发旧指令。
这是软实时调试工具，还需要在真实云台上验证额外调度与桥接耗时。

## 源码与检查

| 文件 | 作用 |
| --- | --- |
| `monitor.py` | 启动、会话管理、HTTP、缓存、统计和 CSV |
| `serial_io.py` | 端口、固定虚拟串口、双向转发与预览队列 |
| `protocol.py` | 30/44 字节解码、CRC、姿态与重同步 |
| `recording.py` | RMSE 原始日志异步写入及离线回放 |
| `index.html / app.js / style.css` | 无外部依赖的网页界面 |
| `test_serval.py / test_engineering.py` | 协议、桥接、日志兼容及回放测试 |

```bash
python -m unittest discover -s . -v
```

Linux 上还会验证真实 PTY 两端的双向字节转发、固定路径占用与清理。
Windows 跳过 Linux 特有测试。测试不驱动真实云台。

## 原版脚本保留

`save/` 只保留原来的两个 Python 脚本，内容不修改：

- `decode_serial_log.py`：原版日志离线解码、CSV 和绘图。
- `plot_serial_waveform.py`：模拟波形示意图，不读取真实串口。
