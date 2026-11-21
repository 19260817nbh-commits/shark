#include "serial_recorder.hpp"

#include <cstdlib>
#include <cstring>
#include <ctime>
#include <filesystem>
#include <string>

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

  enabled_ = enable || env_on("SP_SERIAL_LOG");
  if (!enabled_) return;

  std::error_code ec;
  std::filesystem::create_directories(dir, ec);
  if (ec) {
    tools::logger()->warn("[SerialRecorder] cannot create {}: {}", dir, ec.message());
    enabled_ = false;
    return;
  }

  path_ = dir + "/serial_" + now_string() + ".bin";
  t0_ = std::chrono::steady_clock::now();
  thread_ = std::thread(&SerialRecorder::writer_loop, this);

  tools::logger()->info("[SerialRecorder] recording raw serial frames -> {}", path_);
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

  tools::logger()->info(
    "[SerialRecorder] closed {}: {} frames / {} bytes, dropped {}", path_, written_, written_bytes_,
    dropped_);
}

void SerialRecorder::record(
  uint8_t dir, const uint8_t * data, size_t len, std::chrono::steady_clock::time_point t,
  uint8_t status)
{
  if (!enabled_ || data == nullptr) return;

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

void SerialRecorder::writer_loop()
{
  std::ofstream out(path_, std::ios::binary | std::ios::trunc);
  if (!out) {
    tools::logger()->error("[SerialRecorder] failed to open {}", path_);
    return;
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

    if (!write_batch(out, batch, written_, written_bytes_)) {
      tools::logger()->warn("[SerialRecorder] write failed at frame {}", written_);
      break;
    }

    const auto now = std::chrono::steady_clock::now();
    if (quit_ || now - last_flush > std::chrono::milliseconds(200)) {
      out.flush();
      last_flush = now;
    }
  }

  out.flush();
  out.close();
}

}  // namespace io
