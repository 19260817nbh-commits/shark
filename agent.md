# JNU Vision Agent Guide

## 项目定位
本仓库是一个基于 `C++17 + CMake` 的 RoboMaster 视觉框架，核心能力是自瞄、打符、哨兵全向感知、标定与离线调试。

代码的主干结构是：
- `src/` 放不同兵种或调试模式的可执行入口。
- `tasks/` 放算法模块，按 `auto_aim`、`auto_buff`、`omniperception` 分层。
- `io/` 放硬件抽象层，包含工业相机、USB 相机、CAN/CBoard、串口 Gimbal、ROS2。
- `tools/` 放通用工具，如日志、录制、PlotJuggler 数据发送、EKF、弹道和线程安全队列。
- `configs/` 放每台机器人或测试场景的 YAML 配置。
- `tests/` 放离线/联调/硬件测试程序。
- `calibration/` 放相机标定和手眼标定程序。

## 构建与依赖
顶层 `CMakeLists.txt` 直接启用 CUDA，并依赖：
- `OpenCV`
- `fmt`
- `Eigen3`
- `spdlog`
- `yaml-cpp`
- `nlohmann_json`
- `OpenVINO`
- `TensorRT` 相关库 `nvinfer`、`nvonnxparser`
- `Ceres`，仅 `auto_buff` 需要
- 可选 `ROS2`，仅哨兵和 ROS 测试会在环境存在时编译

常用构建命令：

```bash
cmake -B build
cmake --build build -j"$(nproc)"
```

如果 ROS2 环境不完整，`sentry`、`publish_test`、`subscribe_test`、`topic_loop_test` 会被自动跳过。

## 入口程序怎么选
常见主程序都在顶层 `CMakeLists.txt` 里直接注册：

- `standard`: 步兵基础版，自瞄主循环，走 `io::CBoard`。
- `mt_standard`: 步兵多线程检测版，检测和决策解耦。
- `standard_mpc`: 串口 `io::Gimbal` 版本，带 MPC/Planner，同时支持自瞄和打符。
- `standard_debug_mpc`: `standard_mpc` 的调试版，带绘图、录制、可视化和 FPS 日志。
- `auto_aim_debug_mpc`: 纯自瞄 MPC 调试入口。
- `mt_auto_aim_debug`: 自瞄多线程调试入口。
- `auto_buff_debug` / `auto_buff_debug_mpc`: 打符调试入口。
- `uav`: 通过 `CBoard` 模式切换自瞄/打符。
- `uav_debug`: `uav` 的调试版。
- `sentry` / `sentry_bp` / `sentry_debug` / `sentry_multithread`: 哨兵入口，依赖 ROS2 和 `omniperception`。

经验上可以这样选：
- 改普通步兵主流程，先看 `src/standard.cpp` 或 `src/standard_mpc.cpp`。
- 改串口云台协议或 MPC，下钻 `src/standard_mpc.cpp`、`src/standard_debug_mpc.cpp`、`io/gimbal/`、`tasks/auto_aim/planner/`。
- 改哨兵逻辑，先看 `src/sentry.cpp` 和 `tasks/omniperception/`。
- 改打符流程，先看 `src/uav.cpp` 或 `src/auto_buff_debug*.cpp`。

## 核心数据流
大多数入口遵循同一条主链路：

`camera.read()` -> 读取姿态 `cboard.imu_at()` 或 `gimbal.q()` -> `solver.set_R_gimbal2world()` -> 检测器 -> `tracker.track()` 或 buff target 更新 -> `aimer` / `planner` / `shooter` -> `cboard.send()` 或 `gimbal.send()`

这里最关键的模块职责是：

- `tasks/auto_aim/detector.*`: 传统方法检测器。
- `tasks/auto_aim/yolo.*` 与 `tasks/auto_aim/yolos/*`: 统一 YOLO 封装，后端由 `yolo_name` 决定。
- `tasks/auto_aim/solver.*`: 装甲板位姿解算、重投影、坐标变换。
- `tasks/auto_aim/tracker.*` 与 `target.*`: 目标状态机 + EKF 跟踪。
- `tasks/auto_aim/aimer.*`: 选瞄点并输出 `io::Command`。
- `tasks/auto_aim/shooter.*`: 开火判定。
- `tasks/auto_aim/planner/*`: MPC 规划器。
- `tasks/auto_buff/*`: 打符检测（YOLO11-pose 9 关键点）、解算（IPPE+精化 PnP）、预测（小符 7 维/大符 10 维正弦 EKF）、多扇叶管理与选板（`buff_big_group.*`）、瞄准。
- `tasks/omniperception/*`: 哨兵全向感知和优先级决策。

## IO 层怎么理解
仓库里有两套控制链路，不要混淆：

- `io::CBoard`
  通过 `SocketCAN` 收发。
  从 CAN 接收四元数、弹速、模式，发送 `io::Command`。
  常见于 `standard`、`uav`、部分调试程序。

- `io::Gimbal`
  通过串口收发自定义协议。
  内部线程持续读包，维护姿态和状态缓存。
  常见于 `standard_mpc`、`standard_debug_mpc`、`auto_aim_debug_mpc`。

其余硬件相关模块：
- `io/camera.*`: 根据 YAML 中 `camera_name` 选择 `HikRobot` 或 `MindVision`。
- `io/usbcamera/*`: USB 摄像头线程化读取，主要给哨兵全向感知用。
- `io/dm_imu/*`: 达妙 IMU。
- `io/ros2/*`: 可选 ROS2 发布/订阅封装。

## 配置规则
配置是仓库的高耦合点，很多类直接调用 `tools::read(yaml, key)` 读取键值，缺键会直接 `exit(1)`。这意味着：

- 新增配置项时，要同步补齐所有会用到该模块的 YAML。
- 如果只是某个入口专用，最好显式限制读取范围，不要让公共类无条件读新键。
- 机器人差异主要通过 `configs/*.yaml` 承担，而不是通过代码分支硬编码。

常见配置文件：
- `configs/standard3.yaml`、`standard4.yaml`、`standard5.yaml`: 步兵/标准机。
- `configs/hero.yaml`: 英雄或特定机型。
- `configs/sentry.yaml`: 哨兵。
- `configs/uav.yaml`: 无人机/双模式入口。
- `configs/camera.yaml`: 单纯相机测试。
- `configs/calibration.yaml`: 标定程序。
- `configs/demo.yaml`: 离线 demo。

常见配置键分组：
- 检测：`yolo_name`、`*_model_path`、`device`、`min_confidence`、`use_traditional`
- 工业相机：`camera_name`、`exposure_ms`、`gain`/`gamma`、`vid_pid`
- USB 相机：`image_width`、`image_height`、`fov_h`、`fov_v`、`usb_*`
- 跟踪：`min_detect_count`、`max_temp_lost_count`、`outpost_max_temp_lost_count`
- 瞄准与开火：`yaw_offset`、`pitch_offset`、`decision_speed`、`*_delay_time`、`first_tolerance`、`second_tolerance`
- CAN/CBoard：`quaternion_canid`、`bullet_speed_canid`、`send_canid`、`can_interface`
- 串口 Gimbal：`com_port`
- MPC：`fire_thresh`、`max_*_acc`、`Q_*`、`R_*`
- Buff：`model`、`ConfidenceThreshold`、`IouThreshold`、`fire_gap_time`、`predict_time`、`pnp_mode`、`buff_*`（几何参数）、`binary_threshold` 等 R 中心精化参数

## 常用运行命令
离线与联调时，优先用这些入口：

```bash
./build/auto_aim_test
./build/standard configs/standard3.yaml
./build/standard_mpc configs/standard5.yaml
./build/standard_debug_mpc configs/standard5.yaml
./build/uav configs/uav.yaml
./build/sentry configs/sentry.yaml
./build/capture configs/calibration.yaml
./build/calibrate_camera -c configs/calibration.yaml
./build/calibrate_handeye -c configs/calibration.yaml
```

脚本相关：
- `autostart.sh` 直接跑 `./build/auto_aim_debug_mpc configs/hero.yaml`
- `watchdog.sh` 监控 `./build/standard_mpc configs/standard5.yaml` 的 fps 输出，超时重启
- `register_autostart.sh` 把 `watchdog.sh` 注册为桌面自启
- `stop_watchdog.sh` 终止 watchdog 和 `standard_mpc`

## 调试与产物
- `tools::logger()` 同时输出终端和 `logs/*.log`
- `tools::Recorder` 会在 `records/` 下落 `.avi + .txt`
- `tools::Plotter` 通过 UDP 把 JSON 发到 `127.0.0.1:9870`，适配 PlotJuggler
- `io::SerialRecorder`（`io/gimbal/serial_recorder.*`）把 `Gimbal` 串口的原始收发帧原样落盘到
  `records/serial/*.bin`，默认关闭，`SP_SERIAL_LOG=1` 开启（`SP_SERIAL_LOG_DIR` 可改目录）。
  离线解算：`python tools/decode_serial_log.py <bin> --plot`，输出 `tx.csv`/`rx.csv` 和波形图。
  先录原始字节再解算，协议改动后旧数据仍可重新解析。
- 很多 debug 入口会在图像上画重投影结果，并且持续发送 `nis`、`nees`、目标状态、开火状态等调试数据

如果你在排查“看起来算法没问题但车上不工作”的问题，优先确认：
- 用的是 `CBoard` 还是 `Gimbal` 链路
- YAML 里的 `camera_name`、`vid_pid`、`com_port`、`can_interface` 是否匹配当前机器
- 模型路径是否存在，`yolo_name` 是否和模型格式匹配
- 当前入口是否真的支持你在用的模式，比如 ROS2 版哨兵不会在无 ROS2 时构建

## 推荐改动落点
- 改识别效果：`tasks/auto_aim/detector.*`、`tasks/auto_aim/yolo.*`、`tasks/auto_aim/yolos/*`
- 改位姿或坐标系：`tasks/auto_aim/solver.*` + 对应 `configs/*.yaml` 标定参数
- 改跟踪稳定性：`tasks/auto_aim/tracker.*`、`tasks/auto_aim/target.*`
- 改瞄准点选择或发弹时机：`tasks/auto_aim/aimer.*`、`tasks/auto_aim/shooter.*`
- 改 MPC：`tasks/auto_aim/planner/*`、`src/standard_mpc.cpp`
- 改打符：`tasks/auto_buff/*`
- 改硬件接入：`io/*`
- 改录制/日志/绘图：`tools/*`

## 已确认的注意事项
- `tests/cboard_test.cpp` 默认配置写的是 `configs/standard.yaml`，仓库里没有这个文件；运行时需要手动改成现有配置。
- `tests/handeye_test.cpp` 默认配置写的是 `configs/handeye.yaml`，仓库里没有这个文件；更接近当前现状的是 `configs/calibration.yaml`。
- `readme.md` 仍以 `sp_vision_25` 为主叙述，但当前仓库名和脚本路径已经是 `JNU_vision_26`。
- `register_autostart.sh`、`watchdog.sh`、`autostart.sh` 里有硬编码绝对路径，迁移机器时必须一起改。
- 很多入口在姿态对齐时使用 `t - 1ms` 或额外 `delay` 补偿，改时序相关逻辑时要连同这一层一起看。

## 最后建议
这个仓库不是“一个通用框架 + 少量业务代码”的结构，而是“多个可执行入口共享同一批算法和硬件模块”。改动前先判断你是在改：

- 通用算法层
- 某个兵种入口
- 某条硬件链路
- 某台机器人的配置

先找对层，再动手，通常能少走很多弯路。
