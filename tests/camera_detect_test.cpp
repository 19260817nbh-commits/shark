#include <Eigen/Dense>
#include <algorithm>
#include <chrono>
#include <fmt/format.h>
#include <list>
#include <map>
#include <memory>
#include <opencv2/opencv.hpp>
#include <string>
#include <vector>

#include "io/camera.hpp"
#include "tasks/auto_aim/armor.hpp"
#include "tasks/auto_aim/detector.hpp"
#include "tasks/auto_aim/solver.hpp"
#include "tasks/auto_aim/tracker.hpp"
#include "tasks/auto_aim/yolo.hpp"
#include "tools/exiter.hpp"
#include "tools/logger.hpp"

// 这三个表在 auto_aim 命名空间里
using auto_aim::ARMOR_NAMES;
using auto_aim::ARMOR_TYPES;
using auto_aim::COLORS;

const std::string keys =
  "{help h usage ? |                     | 显示本帮助}"
  "{@config-path   | configs/sentry.yaml | yaml 配置文件路径}"
  "{tradition t    | false               | 用传统方法识别，而不是 YOLO}"
  "{debug d        | false               | 打开 imshow（绘制开销会算进 detect）}"
  "{no-detect n    | false               | 只测相机，不做检测}"
  "{track k        | false               | 追加 Solver+Tracker，统计 target 帧数}"
  "{quiet q        | false               | 关掉每秒状态行，只在退出时报表}"
  "{warmup w       | 30                  | 预热帧数，不计入统计}";

namespace
{
double ms_between(
  const std::chrono::steady_clock::time_point & a,
  const std::chrono::steady_clock::time_point & b)
{
  return std::chrono::duration<double, std::milli>(a - b).count();
}

double mean_of(const std::vector<double> & v)
{
  if (v.empty()) return 0.0;
  double sum = 0.0;
  for (auto x : v) sum += x;
  return sum / static_cast<double>(v.size());
}

// 一行统计：avg / p50 / p95 / max + 等效帧率
std::string stats_line(
  const std::string & key, const std::string & note, const std::vector<double> & samples)
{
  if (samples.empty()) return fmt::format("  {:<7}没有采集到有效帧\n", key);

  auto v = samples;
  std::sort(v.begin(), v.end());
  const auto at = [&v](double p) {
    return v[static_cast<size_t>(p * static_cast<double>(v.size() - 1))];
  };
  const double avg = mean_of(v);

  return fmt::format(
    "  {:<7}avg {:>6.2f}  p50 {:>6.2f}  p95 {:>6.2f}  max {:>6.2f} ms"
    "   = {:>6.1f} fps   {}\n",
    key, avg, at(0.50), at(0.95), v.back(), 1000.0 / avg, note);
}
}  // namespace

int main(int argc, char * argv[])
{
  cv::CommandLineParser cli(argc, argv, keys);
  if (cli.has("help")) {
    cli.printMessage();
    return 0;
  }

  const auto config_path = cli.get<std::string>(0);
  const auto use_tradition = cli.get<bool>("tradition");
  const auto debug = cli.get<bool>("debug");
  const auto no_detect = cli.get<bool>("no-detect");
  const auto want_track = cli.get<bool>("track");
  const auto quiet = cli.get<bool>("quiet");
  const auto warmup = cli.get<int>("warmup");

  tools::Exiter exiter;

  io::Camera camera(config_path);
  auto_aim::YOLO yolo(config_path, debug);

  std::unique_ptr<auto_aim::Detector> detector;
  if (use_tradition) detector = std::make_unique<auto_aim::Detector>(config_path, false);

  std::unique_ptr<auto_aim::Solver> solver;
  std::unique_ptr<auto_aim::Tracker> tracker;
  const bool track = want_track && !no_detect;
  if (track) {
    solver = std::make_unique<auto_aim::Solver>(config_path);
    // 没有云台/CBoard：用单位四元数，世界系 == gimbal 系，只验证链路不验证瞄准
    solver->set_R_gimbal2world(Eigen::Quaterniond(1.0, 0.0, 0.0, 0.0));
    tracker = std::make_unique<auto_aim::Tracker>(config_path, *solver);
  }

  tools::logger()->info(
    "开始: config={} 方式={} imshow={} 跟踪={} 预热={} 帧", config_path,
    use_tradition ? "传统方法" : "YOLO", debug ? "开" : "关", track ? "开" : "关", warmup);

  std::vector<double> detect_ms;
  std::vector<double> loop_ms;
  std::vector<double> track_ms;
  detect_ms.reserve(8192);
  loop_ms.reserve(8192);
  track_ms.reserve(8192);

  // 检测结果统计
  long armor_frames = 0;  // 有装甲板的帧数
  long armor_total = 0;   // 累计装甲板块数
  long max_armors = 0;    // 单帧最多几块
  long hist[4] = {0, 0, 0, 0};
  std::map<int, long> color_count;
  std::map<int, long> name_count;
  double max_conf = 0.0;

  // tracker 统计
  long target_frames = 0;
  std::map<std::string, long> state_count;

  // 每秒状态行
  long sec_frames = 0;
  long sec_armor_frames = 0;
  long sec_target_frames = 0;
  double sec_detect_sum = 0.0;
  std::string last_armor = "无";

  cv::Mat img;
  std::chrono::steady_clock::time_point timestamp;

  int frame = 0;
  const auto run_begin = std::chrono::steady_clock::now();
  auto sec_begin = run_begin;

  // 循环里不打印、不 waitKey；Ctrl+C 之后出一次报表
  while (!exiter.exit()) {
    const auto loop_begin = std::chrono::steady_clock::now();

    camera.read(img, timestamp);
    if (img.empty()) break;

    auto detect_begin = loop_begin;
    auto detect_end = loop_begin;
    double detect_ms_now = 0.0;
    int armor_count = 0;
    std::list<auto_aim::Armor> armors;

    if (!no_detect) {
      detect_begin = std::chrono::steady_clock::now();
      armors = use_tradition ? detector->detect(img) : yolo.detect(img);
      detect_end = std::chrono::steady_clock::now();
      detect_ms_now = ms_between(detect_end, detect_begin);

      armor_count = static_cast<int>(armors.size());

      if (!armors.empty()) {
        const auto & a = armors.front();
        last_armor = fmt::format(
          "{} {} {} conf={:.2f}", COLORS[static_cast<size_t>(a.color)],
          ARMOR_TYPES[static_cast<size_t>(a.type)], ARMOR_NAMES[static_cast<size_t>(a.name)],
          a.confidence);
      } else {
        last_armor = "无";
      }

      if (frame >= warmup) {
        for (const auto & armor : armors) {
          const int ci = static_cast<int>(armor.color);
          if (ci >= 0 && ci < static_cast<int>(COLORS.size())) color_count[ci]++;
          const int ni = static_cast<int>(armor.name);
          if (ni >= 0 && ni < static_cast<int>(ARMOR_NAMES.size())) name_count[ni]++;
          max_conf = std::max(max_conf, armor.confidence);
        }
      }
    }

    size_t target_count = 0;
    double track_ms_now = 0.0;
    if (tracker) {
      const auto track_begin = std::chrono::steady_clock::now();
      auto targets = tracker->track(armors, timestamp);  // armors 会被排序/过滤
      target_count = targets.size();
      track_ms_now = ms_between(std::chrono::steady_clock::now(), track_begin);
    }

    const auto loop_end = std::chrono::steady_clock::now();
    ++frame;

    if (frame > warmup) {
      loop_ms.push_back(ms_between(loop_end, loop_begin));

      if (!no_detect) {
        detect_ms.push_back(detect_ms_now);
        if (armor_count > 0) armor_frames++;
        armor_total += armor_count;
        max_armors = std::max<long>(max_armors, armor_count);
        hist[std::min(3, armor_count)]++;
      }

      if (tracker) {
        track_ms.push_back(track_ms_now);
        if (target_count > 0) target_frames++;
        state_count[tracker->state()]++;
      }

      sec_frames++;
      sec_detect_sum += detect_ms_now;
      if (armor_count > 0) sec_armor_frames++;
      if (target_count > 0) sec_target_frames++;
    }

    if (!quiet) {
      const double sec_elapsed = std::chrono::duration<double>(loop_end - sec_begin).count();
      if (sec_elapsed >= 1.0) {
        const double t_now = std::chrono::duration<double>(loop_end - run_begin).count();
        const std::string extra =
          tracker ? fmt::format(" | target {} 帧", sec_target_frames) : std::string();

        tools::logger()->info(
          "[{:6.1f}s] 帧率 {:5.0f} | 检测 {:5.2f} ms | 本秒有板 {} 帧{} | 最近一帧: {}", t_now,
          static_cast<double>(sec_frames) / sec_elapsed,
          sec_frames > 0 ? sec_detect_sum / static_cast<double>(sec_frames) : 0.0,
          sec_armor_frames, extra, last_armor);

        sec_frames = 0;
        sec_armor_frames = 0;
        sec_target_frames = 0;
        sec_detect_sum = 0.0;
        sec_begin = loop_end;
      }
    }
  }

  // ==================== 退出时输出一次报表 ====================
  const double elapsed =
    std::chrono::duration<double>(std::chrono::steady_clock::now() - run_begin).count();
  const long counted = static_cast<long>(loop_ms.size());

  std::string report;
  report += "====================== 本次运行汇总 ======================\n";
  report += fmt::format(
    "  运行 {:.1f} s，共 {} 帧，整环 {:.1f} fps（以下统计 {} 帧，跳过预热 {} 帧）\n", elapsed,
    frame, elapsed > 0 ? static_cast<double>(frame) / elapsed : 0.0, counted, warmup);

  report += "  -- 耗时（单位 ms，越小越好）--\n";
  if (!no_detect) report += stats_line("detect", "只算检测本身", detect_ms);
  report += stats_line("loop", "整环：camera.read + 检测", loop_ms);
  if (tracker) report += stats_line("track", "Solver 解算 + Tracker", track_ms);

  if (!no_detect) {
    const double pct = counted > 0 ? 100.0 * static_cast<double>(armor_frames) / counted : 0.0;
    const double per_frame = counted > 0 ? static_cast<double>(armor_total) / counted : 0.0;

    std::string colors;
    for (const auto & kv : color_count) colors += COLORS[kv.first] + "=" + std::to_string(kv.second) + " ";
    std::string names;
    for (const auto & kv : name_count) names += ARMOR_NAMES[kv.first] + "=" + std::to_string(kv.second) + " ";

    report += "  -- 检测结果 --\n";
    report += fmt::format(
      "  有板帧  {} / {} 帧（{:.1f}%），平均 {:.2f} 块/帧，单帧最多 {} 块\n", armor_frames,
      counted, pct, per_frame, max_armors);
    report += fmt::format(
      "  分布    0块 {} 帧 | 1块 {} 帧 | 2块 {} 帧 | 3块及以上 {} 帧\n", hist[0], hist[1], hist[2],
      hist[3]);
    report += fmt::format("  颜色    {}\n", colors.empty() ? std::string("无") : colors);
    report += fmt::format("  名称    {}\n", names.empty() ? std::string("无") : names);
    report += fmt::format("  最高分  {:.2f}\n", max_conf);
  }

  if (tracker) {
    const double pct = counted > 0 ? 100.0 * static_cast<double>(target_frames) / counted : 0.0;
    std::string states;
    for (const auto & kv : state_count) states += kv.first + "=" + std::to_string(kv.second) + " ";

    report += "  -- 跟踪（target）--\n";
    report += fmt::format("  target 帧 {} / {} 帧（{:.1f}%）\n", target_frames, counted, pct);
    report += fmt::format("  tracker 状态 {}", states.empty() ? std::string("无") : states);
    report += "\n";
  }

  report += "==========================================================";
  tools::logger()->info("\n{}", report);

  return 0;
}
