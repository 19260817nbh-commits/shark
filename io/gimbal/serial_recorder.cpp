#include "serial_recorder.hpp"

#include <cstdlib>
#include <cstring>
#include <ctime>
#include <filesystem>
#include <string>
#include <array>
#include <stdexcept>
#include <arpa/inet.h>
#include <sys/socket.h>
#include <unistd.h>

#include "tools/logger.hpp"

namespace io
{
namespace
{
bool env_on(const char * name)
{
  const char * v = std::getenv(name);
  if (v == nullptr || v[0] == '\0') return false;
  const std::string s(v);
  return s != "0" && s != "false" && s != "off";
}

std::string now_string()
{
  std::time_t now = std::time(nullptr);
  std::tm tm{};
  localtime_r(&now, &tm);
  char buf[32] = {};
  std::strftime(buf, sizeof(buf), "%Y-%m-%d_%H-%M-%S", &tm);
  return buf;
}
}  // namespace

SerialRecorder::SerialRecorder(bool enable)
{
  std::string dir = "records/serial";
  if (const char * env_dir = std::getenv("SP_SERIAL_LOG_DIR"); env_dir != nullptr && env_dir[0]) {
    dir = env_dir;
  }

  disk_enabled_ = enable || env_on("SP_SERIAL_LOG");
  // Optional observer only. The control threads never perform socket or disk I/O here.
  if (const char * endpoint = std::getenv("SP_SERIAL_UDP"); endpoint && endpoint[0]) {
    const std::string value(endpoint);
    const auto colon = value.rfind(':');
    try {
      size_t consumed = 0;
      const auto port_text = colon == std::string::npos ? "" : value.substr(colon + 1);
      const int port = std::stoi(port_text, &consumed);
      udp_address_.sin_family = AF_INET;
      if (consumed != port_text.size() || port < 1 || port > 65535 ||
          inet_pton(AF_INET, value.substr(0, colon).c_str(), &udp_address_.sin_addr) != 1) {
        throw std::invalid_argument("expected IPv4:port");
      }
      udp_address_.sin_port = htons(static_cast<uint16_t>(port));
      udp_socket_ = socket(AF_INET, SOCK_DGRAM | SOCK_NONBLOCK | SOCK_CLOEXEC, 0);
      if (udp_socket_ < 0) throw std::runtime_error("socket failed");
      tools::logger()->info("[SerialRecorder] live UDP -> {}", value);
    } catch (const std::exception & e) {
      tools::logger()->warn("[SerialRecorder] invalid SP_SERIAL_UDP {}: {}", value, e.what());
    }
  }
  enabled_ = disk_enabled_ || udp_socket_ >= 0;
  if (!enabled_) return;

  std::error_code ec;
  if (disk_enabled_) std::filesystem::create_directories(dir, ec);
  if (ec) {
    tools::logger()->warn("[SerialRecorder] cannot create {}: {}", dir, ec.message());
    disk_enabled_ = false;
    enabled_ = udp_socket_ >= 0;
    if (!enabled_) return;
  }

  if (disk_enabled_) path_ = dir + "/serial_" + now_string() + ".bin";
  t0_ = std::chrono::steady_clock::now();
  session_ = static_cast<uint64_t>(t0_.time_since_epoch().count()) ^
             (static_cast<uint64_t>(getpid()) << 32);
  thread_ = std::thread(&SerialRecorder::writer_loop, this);

  if (disk_enabled_) tools::logger()->info("[SerialRecorder] recording raw serial frames -> {}", path_);
}

SerialRecorder::~SerialRecorder()
{
  if (!enabled_) return;

  {
    std::lock_guard<std::mutex> lock(mutex_);
    quit_ = true;
  }
  cv_.notify_all();
  if (thread_.joinable()) thread_.join();
  if (udp_socket_ >= 0) close(udp_socket_);

  tools::logger()->info(
    "[SerialRecorder] closed {}: {} frames / {} bytes, dropped {}", path_, written_, written_bytes_,
    dropped_);
  if (udp_dropped_) tools::logger()->warn("[SerialRecorder] live UDP dropped {} datagrams", udp_dropped_);
}

void SerialRecorder::record(
  uint8_t dir, const uint8_t * data, size_t len, std::chrono::steady_clock::time_point t,
  uint8_t status)
{
  if (!enabled_ || data == nullptr || dir > 1 || len == 0 || len > MAX_PAYLOAD) return;

  Record r;
  r.dir = dir;
  r.status = status;
  r.len = static_cast<uint16_t>(len > MAX_PAYLOAD ? MAX_PAYLOAD : len);
  r.t_ns =
    static_cast<uint64_t>(std::chrono::duration_cast<std::chrono::nanoseconds>(t - t0_).count());
  std::memcpy(r.data, data, r.len);

  std::lock_guard<std::mutex> lock(mutex_);
  r.seq = seq_[dir & 1u]++;
  if (queue_.size() >= MAX_QUEUE) {
    dropped_++;
    return;
  }
  queue_.push_back(r);
  cv_.notify_one();
}

bool SerialRecorder::write_batch(
  std::ofstream & out, const std::deque<Record> & batch, uint64_t & frames, uint64_t & bytes)
{
  for (const auto & r : batch) {
    // 与小端 packed 的 Header 严格对应，格式见 serial_recorder.hpp
    const uint32_t magic = MAGIC;
    const uint32_t reserved = 0;

    out.write(reinterpret_cast<const char *>(&magic), 4);
    out.write(reinterpret_cast<const char *>(&r.dir), 1);
    out.write(reinterpret_cast<const char *>(&r.status), 1);
    out.write(reinterpret_cast<const char *>(&r.len), 2);
    out.write(reinterpret_cast<const char *>(&r.t_ns), 8);
    out.write(reinterpret_cast<const char *>(&r.seq), 4);
    out.write(reinterpret_cast<const char *>(&reserved), 4);
    out.write(reinterpret_cast<const char *>(r.data), r.len);

    if (!out) return false;

    frames++;
    bytes += sizeof(Header) + r.len;
  }
  return true;
}

void SerialRecorder::send_live(const std::deque<Record> & batch)
{
  if (udp_socket_ < 0) return;
  // Stay below a normal LAN MTU. Every datagram is independently decodable.
  std::array<uint8_t, 1200> packet{};
  std::memcpy(packet.data(), "SVL1", 4);
  std::memcpy(packet.data() + 4, &session_, sizeof(session_));
  size_t used = 12;
  const auto flush = [&]() {
    if (used == 12) return;
    const auto sent = sendto(udp_socket_, packet.data(), used, MSG_DONTWAIT,
      reinterpret_cast<const sockaddr *>(&udp_address_), sizeof(udp_address_));
    if (sent != static_cast<ssize_t>(used)) ++udp_dropped_;
    used = 12;
  };
  for (const auto & r : batch) {
    if (used + sizeof(Header) + r.len > packet.size()) flush();
    const Header header{MAGIC, r.dir, r.status, r.len, r.t_ns, r.seq, 0};
    std::memcpy(packet.data() + used, &header, sizeof(header));
    used += sizeof(header);
    std::memcpy(packet.data() + used, r.data, r.len);
    used += r.len;
  }
  flush();
}

void SerialRecorder::writer_loop()
{
  std::ofstream out;
  if (disk_enabled_) {
    out.open(path_, std::ios::binary | std::ios::trunc);
    if (!out) tools::logger()->error("[SerialRecorder] failed to open {}", path_);
  }

  // 不要每包都 flush，按时间批量落盘；即使进程被杀，最多丢几百毫秒
  auto last_flush = std::chrono::steady_clock::now();

  while (true) {
    std::deque<Record> batch;
    {
      std::unique_lock<std::mutex> lock(mutex_);
      cv_.wait(lock, [this] { return quit_ || !queue_.empty(); });
      batch.swap(queue_);
      if (batch.empty() && quit_) break;
    }

    send_live(batch);
    if (out.is_open() && !write_batch(out, batch, written_, written_bytes_)) {
      tools::logger()->warn("[SerialRecorder] write failed at frame {}", written_);
      out.close(); // Disk errors must not terminate the live observer.
    }

    const auto now = std::chrono::steady_clock::now();
    if (quit_ || now - last_flush > std::chrono::milliseconds(200)) {
      if (out.is_open()) out.flush();
      last_flush = now;
    }
  }

  if (out.is_open()) { out.flush(); out.close(); }
}

}  // namespace io
