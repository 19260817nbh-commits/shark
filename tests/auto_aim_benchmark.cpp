#include <fmt/core.h>

#include <algorithm>
#include <chrono>
#include <cmath>
#include <filesystem>
#include <fstream>
#include <map>
#include <numeric>
#include <stdexcept>
#include <string>
#include <vector>

#include <nlohmann/json.hpp>
#include <opencv2/opencv.hpp>
#include <yaml-cpp/yaml.h>

#include "tasks/auto_aim/solver.hpp"
#include "tasks/auto_aim/tracker.hpp"
#include "tasks/auto_aim/yolo.hpp"
#include "tools/math_tools.hpp"

namespace
{
const std::string keys =
  "{help h usage ? |                   | 输出命令行参数说明 }"
  "{config-path c  | configs/demo.yaml | yaml配置文件的路径}"
  "{nis-soft-gate  | true              | 是否读取配置中的NIS软门控}"
  "{trace          | false             | 输出逐帧检测和状态信息}"
  "{start-index s  | 0                 | 视频起始帧下标    }"
  "{end-index e    | 0                 | 视频结束帧下标    }"
  "{@input-path    | assets/demo/demo  | avi和txt文件的路径}";

struct Samples
{
  void add(double value)
  {
    if (std::isfinite(value)) values.push_back(value);
  }

  nlohmann::json summary() const
  {
    if (values.empty()) {
      return {{"count", 0}, {"mean", 0.0}, {"p95", 0.0}, {"max", 0.0}};
    }

    auto sorted = values;
    std::sort(sorted.begin(), sorted.end());
    const auto p95_index =
      std::min(sorted.size() - 1, static_cast<size_t>(std::ceil(sorted.size() * 0.95) - 1));
    const auto sum = std::accumulate(values.begin(), values.end(), 0.0);
    return {
      {"count", values.size()},
      {"mean", sum / static_cast<double>(values.size())},
      {"p95", sorted[p95_index]},
      {"max", sorted.back()}};
  }

  std::vector<double> values;
};

double rate(int numerator, int denominator)
{
  return denominator > 0 ? static_cast<double>(numerator) / denominator : 0.0;
}

std::filesystem::path with_extension(
  const std::filesystem::path & input, const std::string & extension)
{
  if (!input.has_extension()) return input.string() + extension;
  auto result = input;
  result.replace_extension(extension);
  return result;
}
}  // namespace

int main(int argc, char * argv[])
{
  cv::CommandLineParser cli(argc, argv, keys);
  if (cli.has("help")) {
    cli.printMessage();
    return 0;
  }

  const std::filesystem::path input_path = cli.get<std::string>(0);
  const auto config_path = cli.get<std::string>("config-path");
  const auto start_index = cli.get<int>("start-index");
  const auto end_index = cli.get<int>("end-index");
  const auto use_nis_soft_gate = cli.get<bool>("nis-soft-gate");
  const auto trace = cli.get<bool>("trace");
  const auto video_path = with_extension(input_path, ".avi");
  const auto text_path = with_extension(input_path, ".txt");

  cv::VideoCapture video(video_path.string());
  std::ifstream text(text_path);
  if (!video.isOpened()) {
    fmt::print(stderr, "Failed to open video: {}\n", video_path.string());
    return 2;
  }
  if (!text.is_open()) {
    fmt::print(stderr, "Failed to open pose data: {}\n", text_path.string());
    return 2;
  }

  auto_aim::YOLO yolo(config_path, false);
  auto_aim::Solver solver(config_path);
  auto_aim::Tracker tracker(config_path, solver, use_nis_soft_gate);
  const auto yaml = YAML::LoadFile(config_path);
  const auto enemy_color =
    yaml["enemy_color"].as<std::string>() == "red" ? auto_aim::Color::red : auto_aim::Color::blue;

  video.set(cv::CAP_PROP_POS_FRAMES, start_index);
  for (int i = 0; i < start_index; i++) {
    double t, w, x, y, z;
    if (!(text >> t >> w >> x >> y >> z)) {
      fmt::print(stderr, "Pose data ended before start-index {}\n", start_index);
      return 2;
    }
  }

  int frames = 0;
  int detection_frames = 0;
  int detection_count = 0;
  int raw_detection_frames = 0;
  int raw_detection_count = 0;
  int color_rejected_count = 0;
  int geometry_rejected_count = 0;
  int current_detection_streak = 0;
  int max_detection_streak = 0;
  int target_frames = 0;
  int tracking_frames = 0;
  int detecting_frames = 0;
  int temp_lost_frames = 0;
  int lost_frames = 0;
  int target_segments = 0;
  int lost_transitions = 0;
  int observed_but_untracked_frames = 0;
  int target_switches = 0;
  int nis_gate_updates = 0;
  bool previous_has_target = false;
  bool previous_state_lost = true;
  bool have_previous_x = false;
  auto_aim::ArmorName previous_name = auto_aim::ArmorName::not_armor;
  Eigen::VectorXd previous_x;
  double previous_t = 0.0;
  double previous_frame_t = 0.0;

  Samples detect_ms;
  Samples tracker_ms;
  Samples raw_confidence;
  Samples nis;
  Samples center_step;
  Samples velocity_step;
  Samples angular_velocity_step;
  Samples radius_step;
  Samples nis_r_scale;

  const auto benchmark_start = std::chrono::steady_clock::now();
  const auto t0 = std::chrono::steady_clock::now();
  for (int frame_index = start_index;; frame_index++) {
    if (end_index > 0 && frame_index > end_index) break;

    cv::Mat image;
    if (!video.read(image) || image.empty()) break;

    double t, w, x, y, z;
    if (!(text >> t >> w >> x >> y >> z)) {
      fmt::print(stderr, "Pose data ended at video frame {}; stopping benchmark\n", frame_index);
      break;
    }
    const auto timestamp = t0 + std::chrono::microseconds(static_cast<int64_t>(t * 1e6));
    solver.set_R_gimbal2world({w, x, y, z});

    const auto detect_start = std::chrono::steady_clock::now();
    auto armors = yolo.detect(image, frame_index);
    const auto raw_armors = armors;
    const auto track_start = std::chrono::steady_clock::now();
    const auto state_before = tracker.state();
    auto targets = tracker.track(armors, timestamp);
    const auto track_end = std::chrono::steady_clock::now();

    frames++;
    raw_detection_count += static_cast<int>(raw_armors.size());
    if (!raw_armors.empty()) raw_detection_frames++;
    for (const auto & armor : raw_armors) {
      raw_confidence.add(armor.confidence);
      if (armor.color != enemy_color) color_rejected_count++;
    }
    const int enemy_color_count = static_cast<int>(std::count_if(
      raw_armors.begin(), raw_armors.end(),
      [enemy_color](const auto_aim::Armor & armor) { return armor.color == enemy_color; }));
    geometry_rejected_count +=
      std::max(0, enemy_color_count - static_cast<int>(armors.size()));

    detection_count += static_cast<int>(armors.size());
    if (!armors.empty()) {
      detection_frames++;
      current_detection_streak++;
      max_detection_streak = std::max(max_detection_streak, current_detection_streak);
    } else {
      current_detection_streak = 0;
    }
    detect_ms.add(tools::delta_time(track_start, detect_start) * 1e3);
    tracker_ms.add(tools::delta_time(track_end, track_start) * 1e3);

    const auto state = tracker.state();
    tracking_frames += state == "tracking";
    detecting_frames += state == "detecting";
    temp_lost_frames += state == "temp_lost";
    lost_frames += state == "lost";
    const bool state_lost = state == "lost";
    if (state_lost && !previous_state_lost) lost_transitions++;
    previous_state_lost = state_lost;

    const bool has_target = !targets.empty();
    if (has_target && !previous_has_target) target_segments++;
    if (!armors.empty() && !has_target) observed_but_untracked_frames++;
    previous_has_target = has_target;
    if (trace) {
      nlohmann::json raw = nlohmann::json::array();
      for (const auto & armor : raw_armors) {
        raw.push_back(
          {{"color", auto_aim::COLORS[armor.color]},
           {"name", auto_aim::ARMOR_NAMES[armor.name]},
           {"type", auto_aim::ARMOR_TYPES[armor.type]},
           {"confidence", armor.confidence},
           {"center_x", armor.center.x},
           {"center_y", armor.center.y}});
      }
      nlohmann::json filtered = nlohmann::json::array();
      for (const auto & armor : armors) {
        filtered.push_back(
          {{"color", auto_aim::COLORS[armor.color]},
           {"name", auto_aim::ARMOR_NAMES[armor.name]},
           {"confidence", armor.confidence}});
      }
      nlohmann::json frame_trace{
        {"frame", frame_index},
        {"time", t},
        {"dt", previous_frame_t > 0.0 ? t - previous_frame_t : 0.0},
        {"state_before", state_before},
        {"state_after", state},
        {"raw", raw},
        {"filtered", filtered},
        {"has_target", has_target}};
      if (has_target) {
        const auto & target = targets.front();
        const auto target_x = target.ekf_x();
        frame_trace["target"] = {
          {"name", auto_aim::ARMOR_NAMES[target.name]},
          {"r", target_x[8]},
          {"l", target_x[9]},
          {"w", target_x[7]}};
      }
      fmt::print("FRAME_TRACE {}\n", frame_trace.dump());
    }
    previous_frame_t = t;
    if (!has_target) {
      have_previous_x = false;
      continue;
    }

    target_frames++;
    const auto & target = targets.front();
    const auto current_x = target.ekf_x();
    const auto & data = target.ekf().data;
    if (target.is_switch()) target_switches++;

    const auto nis_it = data.find("nis");
    if (nis_it != data.end()) nis.add(nis_it->second);
    const auto gated_it = data.find("nis_gated");
    if (gated_it != data.end() && gated_it->second > 0.5) nis_gate_updates++;
    const auto scale_it = data.find("nis_r_scale");
    if (scale_it != data.end()) nis_r_scale.add(scale_it->second);

    const double dt = t - previous_t;
    if (
      have_previous_x && target.name == previous_name && current_x.size() >= 11 &&
      previous_x.size() == current_x.size() && dt > 1e-4 && dt < 0.2) {
      const Eigen::Vector3d current_center{current_x[0], current_x[2], current_x[4]};
      const Eigen::Vector3d previous_center{previous_x[0], previous_x[2], previous_x[4]};
      const Eigen::Vector3d current_velocity{current_x[1], current_x[3], current_x[5]};
      const Eigen::Vector3d previous_velocity{previous_x[1], previous_x[3], previous_x[5]};
      center_step.add((current_center - previous_center).norm());
      velocity_step.add((current_velocity - previous_velocity).norm());
      angular_velocity_step.add(std::abs(current_x[7] - previous_x[7]));
      radius_step.add(std::abs(current_x[8] - previous_x[8]));
    }

    previous_x = current_x;
    previous_name = target.name;
    previous_t = t;
    have_previous_x = true;
  }

  const auto benchmark_end = std::chrono::steady_clock::now();
  nlohmann::json result{
    {"video", video_path.string()},
    {"config", config_path},
    {"frames", frames},
    {"raw_detection_frames", raw_detection_frames},
    {"raw_detection_frame_rate", rate(raw_detection_frames, frames)},
    {"raw_detections", raw_detection_count},
    {"detection_frames", detection_frames},
    {"detection_frame_rate", rate(detection_frames, frames)},
    {"detections", detection_count},
    {"color_rejected_detections", color_rejected_count},
    {"geometry_rejected_detections", geometry_rejected_count},
    {"max_consecutive_detection_frames", max_detection_streak},
    {"target_frames", target_frames},
    {"target_frame_rate", rate(target_frames, frames)},
    {"tracking_frames", tracking_frames},
    {"detecting_frames", detecting_frames},
    {"temp_lost_frames", temp_lost_frames},
    {"lost_frames", lost_frames},
    {"target_segments", target_segments},
    {"lost_transitions", lost_transitions},
    {"observed_but_untracked_frames", observed_but_untracked_frames},
    {"target_switches", target_switches},
    {"nis_gate_updates", nis_gate_updates},
    {"elapsed_ms", tools::delta_time(benchmark_end, benchmark_start) * 1e3},
    {"detect_ms", detect_ms.summary()},
    {"raw_confidence", raw_confidence.summary()},
    {"tracker_ms", tracker_ms.summary()},
    {"nis", nis.summary()},
    {"center_step_m", center_step.summary()},
    {"velocity_step_mps", velocity_step.summary()},
    {"angular_velocity_step_radps", angular_velocity_step.summary()},
    {"radius_step_m", radius_step.summary()},
    {"nis_r_scale", nis_r_scale.summary()}};

  fmt::print("BENCHMARK_RESULT {}\n", result.dump());
  return frames > 0 ? 0 : 2;
}
