#ifndef TOOLS__CAMERA_PREVIEW_HPP
#define TOOLS__CAMERA_PREVIEW_HPP

#include <algorithm>
#include <chrono>
#include <condition_variable>
#include <cstdio>
#include <cstdlib>
#include <exception>
#include <filesystem>
#include <mutex>
#include <opencv2/imgcodecs.hpp>
#include <opencv2/imgproc.hpp>
#include <thread>
#include <utility>

namespace tools
{
// Optional local camera display with a configurable cadence and one pending frame.
class CameraPreview
{
public:
  CameraPreview(const char * path, const char * preview_fps)
  : path_(path ? path : ""), scratch_(path_.string() + ".tmp.jpg")
  {
    if (!enabled()) return;
    constexpr long default_fps = 60;
    char * end = nullptr;
    const long fps = preview_fps ? std::strtol(preview_fps, &end, 10) : default_fps;
    const bool valid = fps >= 1 && fps <= 120 && (!end || *end == '\0');
    interval_ = std::chrono::nanoseconds(1'000'000'000 / (valid ? fps : default_fps));
    worker_ = std::thread(&CameraPreview::publish, this);
  }

  ~CameraPreview()
  {
    if (!worker_.joinable()) return;
    {
      std::lock_guard<std::mutex> lock(mutex_);
      stopping_ = true;
    }
    ready_.notify_one();
    worker_.join();
  }

  bool enabled() const { return !path_.empty(); }

  void sample(const cv::Mat & image)
  {
    if (!enabled() || image.empty()) return;
    const auto now = std::chrono::steady_clock::now();
    if (now < next_sample_) return;
    // Preserve the sampling phase when a frame arrives slightly after its deadline.
    if (now >= next_sample_ + interval_) next_sample_ = now;
    next_sample_ += interval_;

    cv::Mat copy;
    try {
      copy = image.clone();
    } catch (const cv::Exception &) {
      return;
    }
    {
      std::lock_guard<std::mutex> lock(mutex_);
      pending_ = std::move(copy);
    }
    ready_.notify_one();
  }

private:
  void publish()
  {
    bool reported_error = false;
    while (true) {
      cv::Mat frame;
      {
        std::unique_lock<std::mutex> lock(mutex_);
        ready_.wait(lock, [this] { return stopping_ || !pending_.empty(); });
        if (stopping_) return;
        frame = std::move(pending_);
      }
      try {
        const double scale = std::min({1.0, 640.0 / frame.cols, 480.0 / frame.rows});
        if (scale < 1.0) cv::resize(frame, frame, {}, scale, scale, cv::INTER_AREA);
        if (cv::imwrite(scratch_.string(), frame, {cv::IMWRITE_JPEG_QUALITY, 75})) {
          std::filesystem::rename(scratch_, path_);
          reported_error = false;
          continue;
        }
      } catch (const std::exception &) {
        // A preview failure stays on this thread. The next sample can recover.
      }
      if (!reported_error) {
        std::fputs("Camera preview could not update the local image.\n", stderr);
        reported_error = true;
      }
    }
  }

  const std::filesystem::path path_;
  const std::filesystem::path scratch_;
  std::chrono::nanoseconds interval_{};
  std::chrono::steady_clock::time_point next_sample_{};
  std::mutex mutex_;
  std::condition_variable ready_;
  cv::Mat pending_;
  bool stopping_ = false;
  std::thread worker_;
};
}  // namespace tools

#endif
