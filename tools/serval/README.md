# Serval 串口示波器使用说明

Serval 用来查看本工程上位机实际下发的指令、云台实际反馈和原始数据包，支持在线监听、保存及离线回放。页面不依赖互联网或外部 CDN，Python 3.9+ 可运行；真实串口桥接额外需要 `pyserial`。

## 1. 推荐启动方式：自瞄直接连接云台

在已确认可正常跟踪的配置上增加监听，不要为了看曲线更换标定或控制配置。YAML 使用真实串口：

```yaml
com_port: "/dev/ttyACM0"
enemy_color: "blue"
enemy_color_source: "config"
auto_fire: false
```

`enemy_color_source: config` 固定使用配置颜色；`serial` 或省略此项时，串口颜色会覆盖初始配置。`auto_fire: false` 在本仓库更新后的 `standard_mpc`、`standard_debug_mpc` 中禁止视觉开火请求，仍允许有效目标跟踪。它不会自动改变下位机的手动发射逻辑。改动 C++ 后需要重新编译，修改 YAML 后需要重启自瞄。

第一次更新 C++ 后，在 Orin 工程根目录编译所用入口：

```bash
cd ~/Desktop/test/shark-main
cmake --build build --target standard_debug_mpc -j2
```

推荐两个终端分别管理。终端 1 保留已经验证可用的自瞄启动命令，仅增加原始日志和低频诊断：

```bash
cd ~/Desktop/test/shark-main
SP_SERIAL_LOG=1 SP_CONTROL_DEBUG=1 ./build/standard_debug_mpc configs/balance1.yaml
```

终端 2 启动只读监视器：

```bash
cd ~/Desktop/test/shark-main
python3 tools/serval/monitor.py --follow records/serial --config configs/balance1.yaml --host 0.0.0.0
```

关闭终端 2 或网页不会停止终端 1 的自瞄。只更新 Python/网页时无需重新编译 C++。

也可以一条命令同时启动自瞄和监听：

```bash
python3 tools/serval/orin_watch.py --config configs/balance1.yaml
```

默认入口是 `build/standard_debug_mpc`。无相机调试窗口的入口可显式指定：

```bash
python3 tools/serval/orin_watch.py --config configs/balance1.yaml --binary build/standard_mpc
```

启动器不会重写 YAML；自动设置 `SP_SERIAL_LOG=1`、`SP_CONTROL_DEBUG=1`，将本次串口日志和 `vision.log` 放进独立的 `records/serial/watch_时间_进程号/`。终端会打印准确路径。对启动器按 Ctrl+C 会停止它启动的自瞄和监视器；仅关闭浏览器不会。

若自瞄已开启录制，可只挂接现有日志：

```bash
python3 tools/serval/orin_watch.py --attach --config configs/balance1.yaml --log-dir records/serial/实际的watch目录
```

在另一个终端设置环境变量不能给已经运行的进程补开日志。没有 `[SerialRecorder] recording raw serial frames` 提示、没有增长的 `.bin` 时，先确认实际二进制包含记录功能且启动时启用了录制。

## 2. 在电脑或 Orin 上打开页面

Orin 本机：`http://127.0.0.1:8765`。电脑与 Orin 网络互通后，在电脑浏览器打开 `http://Orin的IP:8765`，例如此前的 `http://10.239.128.11:8765`。

```bash
hostname -I
```

上面命令用于查询 Orin 当前 IP。远程访问需要监视器监听 `0.0.0.0`；联合启动器默认允许远程访问。页面不需要登录账号，不需要互联网。更新页面后可按 Ctrl+F5 强制刷新。

也可使用 SSH 端口转发，在电脑终端运行：

```bash
ssh -L 8765:127.0.0.1:8765 shark@10.239.128.11
```

然后电脑访问 `http://127.0.0.1:8765`，这种方式不要求服务监听外部地址。Orin 无需安装浏览器，也不需要 `--open`；`--open` 仅尝试在运行脚本的机器打开浏览器。

## 3. 网页模块

| 模块 | 作用 |
| --- | --- |
| 链路概览 | 收发频率、最新角度、控制模式、CRC 拒收、缓存情况 |
| 波形：角度 | TX 下发 yaw/pitch 与 RX 反馈叠加；TX 为阶梯线 |
| 波形：角速度 | 直接显示协议中的角速度，不由角度差分替代 |
| 波形：角加速度 | 显示 TX 原生加速度；RX 协议没有加速度字段 |
| 跟随误差 | 每个 RX 时刻取此前最近、间隔不超过 50 ms 的有效控制 TX，计算 TX−RX |
| 游标与 A/B | 共享记录时间轴，显示采样值和样本年龄，测量两标记时间差 |
| 缩放与 Y 轴锁定 | 滚轮缩放、拖动平移、双击回到最新；锁定 Y 轴方便比较调参结果 |
| 时间滑条 | 暂停后查看有限缓存内的历史，不是无限长日志索引 |
| 统计与阶跃 | 样本 RMSE、绝对误差 P95、峰值、偏差；符合条件时计算阶跃响应 |
| 延迟与响应 | 估计跟随滞后，并独立显示网页请求耗时、采样间隔和数据新鲜度 |
| 原始数据包 | TX/RX 的 HEX、CRC、偏移、字段值、日志序号和原始纳秒时间 |
| 控制诊断 | 显示检测、颜色过滤、跟踪和规划状态，过滤高频计时输出 |
| 连接与回放 | 载入离线日志，或选择可选的串口桥接模式 |

暂停显示仅冻结页面，不停止串口或自瞄。历史最多缓存 60,000 个有效帧，长时间实验应保存原始 `.bin`。

## 4. 解码精度与调参边界

协议与 `io/gimbal/gimbal.hpp` 对应：小端、packed、IEEE 754 float32，不添加对齐填充。角度原始单位 rad，角速度 rad/s，角加速度 rad/s²。转为度只影响显示，CSV 保留原始单位。

| 方向 | 格式 | 长度 | 字段与起始字节偏移（从 0 计数） |
| --- | --- | --- | --- |
| TX 上位机→云台 | `<2sB6fBH` | 30 | `SP:0, mode:2, yaw:3, yaw_vel:7, yaw_acc:11, pitch:15, pitch_vel:19, pitch_acc:23, target_id:27, crc16:28` |
| RX 云台→上位机 | `<2sB9fHBH` | 44 | `SP:0, mode:2, q[wxyz]:3/7/11/15, yaw:19, yaw_vel:23, pitch:27, pitch_vel:31, bullet_speed:35, bullet_count:39, enemy_color:41, crc16:42` |

TX mode：0 不控制；1 控制但不开火；2 控制且允许开火。RX mode：0 空闲；1 自瞄；2 小符；3 大符。两种 mode 含义不同。工程颜色定义为 0 蓝、1 红；页面保留原始值，颜色映射选择只影响显示。

CRC16 初值 `0xffff`，反射多项式 `0x8408`，无最终异或，结果低字节在前。旧工程 29/43 字节协议不自动猜测。

- 先检查帧头、长度、CRC。CRC 错误、非有限值、日志报告 IO 异常的数据不进入有效波形，原始包仍可检查。
- Python double 可精确容纳有限 float32，JSON 不主动截断。字段表使用 9 位有效数字，足以往返还原 float32；多显示小数不会提高传感器精度。
- 原始纳秒时间以字符串传给浏览器，避免 JavaScript 大整数失真。
- 不平滑、不自动平移反馈以对齐指令。显示密集时保留首、尾、最小、最大点，统计使用完整缓存样本。
- Yaw 可选择连续展开或原始 ±π；原始模式跨边界断线。Yaw 跟随误差使用最短角差。
- TX mode=0 的真实数值仍可查看，但控制切换处断线，mode=0 不参与有效控制误差。
- 游标取该时刻以前的真实采样并显示年龄，不拿未来值填充。
- 误差和统计要求上下位机角度坐标、方向、零点一致。IMU 四元数属于独立坐标系，不能直接当作云台关节角误差。

**延迟：**跟随滞后估计根据实际采样间隔使用 1–5 ms 搜索网格，在 0–500 ms 内粗搜再细搜。它包含通信、控制器和机械响应，不是串口 RTT，也不是阻尼比。搜索步长不代表硬件精度。静止、未控制、数据过期、相关性不足或无法区分匀速偏差与滞后时不给有效结果。

**阶跃：**要求单次明确跳变至少 0.3°、之前至少 100 ms 稳定、之后至少 300 ms 指令平台。容差取步幅 2% 和 0.05° 中较大者。缺样超过 100 ms、连续运动或初始反馈未接近指令时拒绝计算。反馈进入误差带并保持至少 200 ms 才报告观察窗口内的稳定时间；不编造阻尼比。

C++ 的 TX 时间在 `serial.write` 调用前采样，RX 在读到帧头后采样，是上位机记录时间，受 USB 和操作系统缓冲影响。当前协议没有回显指令序号及设备同步时间，不能将相邻 TX/RX 时间差当作 RTT。921600 波特、8N1 下的理论线上发送时间：30 字节约 0.326 ms，44 字节约 0.477 ms，均不含排队和执行。

## 5. 保存、回放和终端解码

网页“保存当前日志（.bin）”下载到**打开网页的那台电脑**。有日志源时保存点击时完整记录的快照，包括 CRC 错包和两个方向，不受最近包列表或 60,000 帧缓存限制。演示或无日志源时只保存最近最多 1,000 个缓存包，按钮会明确标注。CSV 导出有限缓存中的有效解析数据，不能替代完整日志。

在电脑离线回放：

```bash
python tools/serval/monitor.py --replay "你的记录.bin" --open
```

也可从未连接设备的网页加载 `.bin`：上传上限 128 MB，较大文件用命令行；索引上限 200 万条。支持暂停、继续、调速、从头播放，不打开物理串口。未写完的尾记录会跳过，内部损坏会报错。

只在终端看原始包（不启动网页、不打开串口）：

```bash
python3 tools/serval/packet_dump.py --follow --direction both --hz 2
python3 tools/serval/packet_dump.py records/serial/实际日志.bin --direction rx --hz 0
```

不指定日志时从默认记录目录选择最新日志；显式指定可避免混入另一场测试。`--hz 0` 输出全部包，可能大量刷屏。原版离线工具保存在 `save/`，内容保留：

```bash
python3 tools/serval/save/decode_serial_log.py 你的记录.bin --plot
```

`save/plot_serial_waveform.py` 生成模拟示意图，不采集真实数据。

## 6. 有反馈但云台不控制时看什么

没有一个通用 YAML 开关能把无效目标强行变成有效控制。发送函数依据 `plan.control` 决定 TX mode；`auto_fire` 只控制开火请求。此发送路径不读取 YAML 的 `yaw_kp/yaw_kd/pitch_kp/pitch_kd`。

| 观察结果 | 排查位置 |
| --- | --- |
| TX mode=0 | 当前没有控制指令，先查检测、跟踪、规划，不能据此分析云台超调 |
| `detected=0` | 检测器、相机和目标，不是串口发送开关 |
| `detected>0, kept=0` | 实际敌方颜色、颜色/前哨过滤 |
| `kept>0, targets=0` | 跟踪状态、目标确认或丢失 |
| `reason=planner_rejected` | 位姿、距离、弹道与 `Unsolvable target` 异常 |
| `reason=control_ready` 但 TX mode=0 | 核对同一时段、同一进程日志及实际发送参数 |
| TX mode=1/2 且指令变化但云台不动 | 核对下位机协议、控制模式、单位、方向及执行逻辑 |

CRC 正确只表示校验通过；写入无异常不等于下位机执行成功。不要绕过无目标判断强制发送全零控制量。

`SP_CONTROL_DEBUG=1` 开启低频 `AimTrace/ControlTrace`。两线程各最多一行/秒，不代表同一帧同步数据；网页状态过期时会标注。缺少诊断功能的旧源码可用 `install_control_trace.py` 预检、备份并安装，再编译对应入口；当前仓库已集成，无需重复安装。

`setup_tracking.py` 是可选配置迁移工具，会备份并修改支持的旧入口及指定 YAML，使其固定识别蓝色、禁止视觉开火；不是启动自瞄所必需的步骤。它不编译，也不启动程序。

## 7. 对自瞄的影响

推荐模式的链路：

```text
自瞄 ⇄ 真实串口 ⇄ 云台
  └─ 异步原始日志 → .bin → 只读监视器 → 浏览器
```

监听不打开物理串口，不修改控制包、不增加转发环节。关闭网页不停止自瞄。录制会复制数据并入队、后台写盘，监视器解析和分析会消耗 CPU/内存；队列满时丢弃日志记录，不主动丢弃控制包。不能承诺对 Orin 调度和帧率完全零影响，尚未在你的真实设备测量新增开销。

日志约每 200 ms 批量落盘，波形约每 100 ms 请求、包面板约每 500 ms 请求。显示可能落后几百毫秒，但横轴保留原始记录时间。TX 100 Hz、RX 1000 Hz 时，含 24 字节记录头约 73.4 kB/s、264 MB/小时。下载大日志会增加磁盘与网络流量。

## 8. 可选模式：串口桥接、演示

只读监听优先。桥接需要先停止占用物理口的自瞄，安装依赖：

```bash
python3 -m pip install -r tools/serval/requirements.txt
python3 tools/serval/monitor.py --bridge /dev/ttyACM0 --link /tmp/serval-gimbal --baud 921600 --record
```

然后单独启动自瞄，其 YAML 使用 `com_port: "/tmp/serval-gimbal"`。此模式下 Serval 参与双向透明转发，包括错误包和未知字节；关闭桥接进程会中断通信。切回直连必须恢复真实物理口。不能让两个程序同时读同一物理串口。

Windows 桥接需要自行准备一对虚拟串口（例如 COM11↔COM12）：工具打开 COM3 和 COM11，自瞄打开 COM12。

```bash
python tools/serval/monitor.py --bridge COM3 --virtual-port COM11 --record --open
```

演示不打开设备，数据是模拟的：

```bash
python tools/serval/monitor.py --demo --open
```

端口占用时用 `--http-port 8766`。Linux 的 `--open` 可能触发系统浏览器/GPU 报错，可去掉该参数并从电脑访问网页。

## 9. 文件分工与验证

| 文件 | 作用 |
| --- | --- |
| `monitor.py` | 会话、HTTP、缓存、导出、回放管理 |
| `protocol.py` | 字节协议、CRC、float32 解析和重同步 |
| `serial_io.py` | 可选物理/虚拟串口桥接 |
| `recording.py / passive.py` | 原始日志写入、回放、只读追踪 |
| `orin_watch.py / packet_dump.py` | 联合启动 / 终端数据包查看 |
| `analysis.py / latency.py / diagnostics.py` | 误差与阶跃、滞后估计、控制诊断 |
| `index.html / style.css / app.js / signals.js` | 页面布局、绘图、信号采样 |
| `packets.js / latency.js / diagnostics.js` | 原始包、延迟和诊断面板 |
| `save/` | 两份原版 Python 离线工具 |

在工程根目录运行测试：

```bash
python3 -B -m unittest discover -s tools/serval -p 'test_*.py' -q
node tools/serval/test_signals.cjs
```

Linux 测试包括真实 PTY 双向转发和进程清理；Windows 跳过 Linux 专用测试。测试不驱动真实云台，不代表已经完成实车调参验证。

设计参考：[PlotJuggler](https://github.com/PlotJuggler/PlotJuggler)、[Foxglove Plot](https://docs.foxglove.dev/docs/visualization/panels/plot)、[Saleae Timing Markers](https://new.saleae.com/support/logic-software/viewing-and-analyzing-data/measurements-timing-markers)。页面独立实现。
