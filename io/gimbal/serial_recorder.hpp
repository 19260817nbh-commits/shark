#ifndef IO__GIMBAL__SERIAL_RECORDER_HPP
#define IO__GIMBAL__SERIAL_RECORDER_HPP

#include <atomic>
#include <chrono>
#include <condition_variable>
#include <cstddef>
#include <cstdint>
#include <deque>
#include <fstream>
#include <mutex>
#include <string>
#include <thread>
#include <netinet/in.h>

namespace io
{
// 串口原始收发记录器：把线上字节原样落盘，供离线解算 / 画波形。
//
// 设计原则：
//   - 默认关闭，只有环境变量 SP_SERIAL_LOG=1 时才写文件（生产运行零影响）；
//     可用 SP_SERIAL_LOG_DIR 指定输出目录，默认 records/serial。
//   - 只做 memcpy + 入队，不在串口线程里碰磁盘；写盘由后台线程完成。
//   - 队列满时丢弃并计数，绝不阻塞收发线程（避免影响控制周期）。
//
// 文件格式（小端，逐条记录，头 24 字节 + 负载）：
//   offset  size  field
//   0       4     magic   'RMSE' = 0x45534D52
//   4       1     dir     0=TX(上位机->云台) 1=RX(云台->上位机)
//   5       1     status  bit0 帧头正确 / bit1 TX:写入成功 RX:CRC通过 / bit2 IO异常
//   6       2     len     负载字节数
//   8       8     t_ns    相对本文件创建时刻的纳秒数（steady_clock）
//   16      4     seq     该方向内递增序号
//   20      4     reserved
//   24      len   payload 原始帧字节（含帧头与 CRC，与线上完全一致）
class SerialRecorder
{
public:
  static constexpr uint8_t DIR_TX = 0;
  static constexpr uint8_t DIR_RX = 1;

  static constexpr uint8_t STATUS_HEAD = 1 << 0;  // 帧头正确
  static constexpr uint8_t STATUS_OK = 1 << 1;    // TX: 写入成功；RX: CRC 通过
  static constexpr uint8_t STATUS_ERR = 1 << 2;   // IO 异常

  static constexpr size_t MAX_PAYLOAD = 64;
  static constexpr size_t MAX_QUEUE = 16384;

  explicit SerialRecorder(bool enable = false);
  ~SerialRecorder();

  SerialRecorder(const SerialRecorder &) = delete;
  SerialRecorder & operator=(const SerialRecorder &) = delete;

  bool enabled() const { return enabled_; }
  const std::string & path() const { return path_; }

  void record(
    uint8_t dir, const uint8_t * data, size_t len, std::chrono::steady_clock::time_point t,
    uint8_t status);

private:
  struct Record
  {
    uint8_t dir = 0;
    uint8_t status = 0;
    uint16_t len = 0;
    uint64_t t_ns = 0;
    uint32_t seq = 0;
    uint8_t data[MAX_PAYLOAD] = {};
  };

  struct __attribute__((packed)) Header
  {
    uint32_t magic;
    uint8_t dir;
    uint8_t status;
    uint16_t len;
    uint64_t t_ns;
    uint32_t seq;
    uint32_t reserved;
  };

  static constexpr uint32_t MAGIC = 0x45534D52;  // 'RMSE'
  static_assert(sizeof(Header) == 24, "serial log header must be 24 bytes");

  void writer_loop();
  void send_live(const std::deque<Record> & batch);
  static bool write_batch(
    std::ofstream & out, const std::deque<Record> & batch, uint64_t & frames, uint64_t & bytes);

  bool enabled_ = false;
  bool disk_enabled_ = false;
  int udp_socket_ = -1;
  sockaddr_in udp_address_{};
  uint64_t session_ = 0;
  uint64_t udp_dropped_ = 0;
  std::string path_;
  std::chrono::steady_clock::time_point t0_;

  std::mutex mutex_;
  std::condition_variable cv_;
  std::deque<Record> queue_;
  std::atomic<bool> quit_{false};
  std::thread thread_;

  uint64_t dropped_ = 0;
  uint32_t seq_[2] = {0, 0};
  uint64_t written_ = 0;
  uint64_t written_bytes_ = 0;
};
}  // namespace io

#endif  // IO__GIMBAL__SERIAL_RECORDER_HPP
