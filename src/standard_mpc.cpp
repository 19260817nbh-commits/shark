#include <chrono>
#include <cstdlib>
#include <opencv2/opencv.hpp>
#include <thread>
#include <yaml-cpp/yaml.h>

#include "io/camera.hpp"
#include "io/dm_imu/dm_imu.hpp"
#include "tasks/auto_aim/aimer.hpp"
#include "tasks/auto_aim/multithread/commandgener.hpp"
#include "tasks/auto_aim/multithread/mt_detector.hpp"
#include "tasks/auto_aim/shooter.hpp"
#include "tasks/auto_aim/solver.hpp"
#include "tasks/auto_aim/tracker.hpp"
#include "tasks/auto_buff/buff_aimer.hpp"
#include "tasks/auto_buff/buff_big_group.hpp"
#include "tasks/auto_buff/buff_detector.hpp"
#include "tasks/auto_buff/buff_solver.hpp"
#include "tasks/auto_buff/buff_target.hpp"
#include "tasks/auto_buff/buff_type.hpp"
#include "tools/exiter.hpp"
#include "tools/img_tools.hpp"
#include "tools/logger.hpp"
#include "tools/math_tools.hpp"
#include "tools/plotter.hpp"
#include "tools/recorder.hpp"
#include "tools/yaml.hpp"

const std::string keys =
  "{help h usage ? | | 输出命令行参数说明}"
  "{@config-path   | | yaml配置文件路径 }";

using namespace std::chrono_literals;

int main(int argc, char * argv[])
{
  cv::CommandLineParser cli(argc, argv, keys);
  auto config_path = cli.get<std::string>("@config-path");
  if (cli.has("help") || !cli.has("@config-path")) {
    cli.printMessage();
    return 0;
  }

  auto yaml = tools::load(config_path);
  const auto enemy_color_source = yaml["enemy_color_source"]
                                    ? yaml["enemy_color_source"].as<std::string>()
                                    : std::string("serial");
  const auto configured_enemy_color = tools::read<std::string>(yaml, "enemy_color");
  if (enemy_color_source != "serial" && enemy_color_source != "config") {
    tools::logger()->error("[YAML] enemy_color_source must be serial or config");
    return 1;
  }
  if (enemy_color_source == "config" &&
      configured_enemy_color != "red" && configured_enemy_color != "blue") {
    tools::logger()->error("[YAML] enemy_color must be red or blue");
    return 1;
  }
  tools::logger()->info(
    "[EnemyColor] source={} configured={}", enemy_color_source, configured_enemy_color);

  const auto fire_config = YAML::LoadFile(config_path);
  const bool auto_fire = fire_config["auto_fire"] && fire_config["auto_fire"].as<bool>();
  tools::logger()->info("[Fire] auto_fire={}", auto_fire);

  const char * trace_setting = std::getenv("SP_CONTROL_DEBUG");
  const bool control_trace = trace_setting != nullptr && std::string(trace_setting) == "1";
  if (control_trace) tools::logger()->info("[ControlTrace] enabled; reports at most once per second");

  tools::Exiter exiter;
  tools::Plotter plotter;
  // tools::Recorder recorder;

  io::Gimbal gimbal(config_path);
  io::Camera camera(config_path);

  auto_aim::YOLO yolo(config_path, false);
  auto_aim::Solver solver(config_path);
  auto_aim::Tracker tracker(config_path, solver, true);
  auto_aim::Planner planner(config_path);

  tools::ThreadSafeQueue<std::optional<auto_aim::Target>, true> target_queue(1);
  target_queue.push(std::nullopt);

  auto_buff::Buff_Detector buff_detector(config_path);
  if (enemy_color_source == "config") {
    tracker.set_enemy_color(
      configured_enemy_color == "blue" ? auto_aim::Color::blue : auto_aim::Color::red);
    buff_detector.set_enemy_color(configured_enemy_color);
  }
  auto_buff::Solver buff_solver(config_path);
  auto buff_target_params = auto_buff::load_target_params(config_path);
  auto_buff::SmallTarget buff_small_target(buff_target_params);
  auto_buff::BigTargetGroup buff_big_group(buff_target_params);
  auto_buff::BigTargetSelector buff_big_selector(config_path);
  auto_buff::Aimer buff_aimer(config_path);

  cv::Mat img;
  Eigen::Quaterniond q;
  std::chrono::steady_clock::time_point t;

  std::atomic<bool> quit = false;

  std::atomic<io::GimbalMode> mode{io::GimbalMode::IDLE};
  auto last_mode{io::GimbalMode::IDLE};
  auto last_fps_log = std::chrono::steady_clock::now();
  size_t frame_counter = 0;

  auto plan_thread = std::thread([&]() {
    auto t0 = std::chrono::steady_clock::now();
    auto last_control_trace = t0 - 1s;
    uint16_t last_bullet_count = 0;

    while (!quit) {
      if (!target_queue.empty() && mode == io::GimbalMode::AUTO_AIM) {
        auto target = target_queue.front();
        auto gs = gimbal.state();
        auto plan = planner.plan(target, gs.bullet_speed);

        int target_id = 0;
        if (target.has_value()) {
          switch (target->name) {
            case auto_aim::ArmorName::one: target_id = 1; break;
            case auto_aim::ArmorName::two: target_id = 2; break;
            case auto_aim::ArmorName::three: target_id = 3; break;
            case auto_aim::ArmorName::four: target_id = 4; break;
            case auto_aim::ArmorName::five: target_id = 5; break;
            case auto_aim::ArmorName::sentry: target_id = 7; break;
            case auto_aim::ArmorName::outpost: target_id = 8; break;
            case auto_aim::ArmorName::base: target_id = 9; break;
            default: target_id = 0; break;
          }
        }

        if (control_trace) {
          const auto trace_now = std::chrono::steady_clock::now();
          if (trace_now - last_control_trace >= 1s) {
            tools::logger()->info(
              "[ControlTrace] target={} target_id={} control={} planner_fire={} yaw={:.5f} pitch={:.5f} reason={}",
              target.has_value(), target_id, plan.control, plan.fire, plan.yaw, plan.pitch,
              !target.has_value() ? "no_target" : (plan.control ? "control_ready" : "planner_rejected"));
            last_control_trace = trace_now;
          }
        }
        gimbal.send(
          plan.control, auto_fire && plan.fire, target_id, plan.yaw, plan.yaw_vel, plan.yaw_acc, plan.pitch, plan.pitch_vel,
          plan.pitch_acc);

        std::this_thread::sleep_for(10ms);
      } else
        std::this_thread::sleep_for(200ms);
    }
  });

  auto last_aim_trace = std::chrono::steady_clock::now() - 1s;
  while (!exiter.exit()) {
    mode = gimbal.mode();

    if (last_mode != mode) {
      tools::logger()->info("Switch to {}", gimbal.str(mode));
      if (last_mode == io::GimbalMode::BIG_BUFF && mode != io::GimbalMode::BIG_BUFF) {
        buff_big_selector.reset_cycle();
      }
      last_mode = mode.load();
    }

    camera.read(img, t);
    ++frame_counter;
    auto now = std::chrono::steady_clock::now();
    auto elapsed = std::chrono::duration_cast<std::chrono::milliseconds>(now - last_fps_log);
    if (elapsed.count() >= 1000) {
      auto fps = static_cast<double>(frame_counter) / (elapsed.count() / 1000.0);
      tools::logger()->info("standard_mpc fps: {:.2f}", fps);
      frame_counter = 0;
      last_fps_log = now;
    }
    auto q = gimbal.q(t);
    auto gs = gimbal.state();
    if (enemy_color_source == "serial" && gs.enemy_color == 0) {
      tracker.set_enemy_color(auto_aim::Color::blue);
      buff_detector.set_enemy_color("blue");
    } else if (enemy_color_source == "serial" && gs.enemy_color == 1) {
      tracker.set_enemy_color(auto_aim::Color::red);
      buff_detector.set_enemy_color("red");
    }

    // recorder.record(img, q, t);
    solver.set_R_gimbal2world(q);

    /// 自瞄
    if (mode.load() == io::GimbalMode::AUTO_AIM) {
      auto armors = yolo.detect(img);
      const auto detected_count = armors.size();
      int red_count = 0, blue_count = 0;
      if (control_trace) {
        for (const auto & armor : armors) {
          if (armor.color == auto_aim::Color::red) ++red_count;
          if (armor.color == auto_aim::Color::blue) ++blue_count;
        }
      }
      auto targets = tracker.track(armors, t);
      if (control_trace) {
        const auto trace_now = std::chrono::steady_clock::now();
        if (trace_now - last_aim_trace >= 1s) {
          tools::logger()->info(
            "[AimTrace] rx_mode={} rx_color={} active_color={} detected={} red={} blue={} kept={} tracker={} targets={}",
            gimbal.str(mode.load()), static_cast<int>(gs.enemy_color),
            auto_aim::COLORS[tracker.enemy_color()], detected_count, red_count, blue_count,
            armors.size(), tracker.state(), targets.size());
          last_aim_trace = trace_now;
        }
      }
      if (!targets.empty()) {
        target_queue.push(targets.front());
        auto & target = targets.front();
        // if (target.ekf().data.find("nis") != target.ekf().data.end()) {
        //   tools::logger()->info(
        //     "NIS: {:.2f} Failures: {:.2f}", target.ekf().data.at("nis"),
        //     target.ekf().data.at("recent_nis_failures"));
        // }
      } else
        target_queue.push(std::nullopt);
    }

    /// 打符
    else if (mode.load() == io::GimbalMode::SMALL_BUFF || mode.load() == io::GimbalMode::BIG_BUFF) {
      buff_solver.set_R_gimbal2world(q);

      auto_aim::Plan buff_plan = {false, false, 0, 0, 0, 0, 0, 0, 0, 0};
      if (mode.load() == io::GimbalMode::SMALL_BUFF) {
        auto observations = buff_detector.detect(img, auto_buff::SMALL);
        buff_solver.solve(observations);
        std::optional<auto_buff::PowerRune> power_rune =
          observations.empty() ? std::nullopt
                               : std::optional<auto_buff::PowerRune>(observations.front());

        buff_small_target.get_target(power_rune, t);
        auto target_copy = buff_small_target;
        buff_plan = buff_aimer.mpc_aim(target_copy, t, gs, true);
      } else {
        auto observations = buff_detector.detect(img, auto_buff::BIG);
        buff_solver.solve(observations);
        buff_big_group.update(observations, t);

        auto selected_target = buff_big_selector.select_target(
          buff_big_group, buff_aimer, t, gs.bullet_speed, gs.yaw, gs.pitch,
          std::chrono::steady_clock::now());
        if (selected_target.has_value()) {
          auto target_copy = selected_target.value();
          buff_plan = buff_aimer.mpc_aim(target_copy, t, gs, true);
          if (auto_fire && buff_plan.control && buff_plan.fire) {
            buff_big_selector.on_fire(std::chrono::steady_clock::now());
          }
        }
      }
      gimbal.send(
        buff_plan.control, auto_fire && buff_plan.fire, 0, buff_plan.yaw, buff_plan.yaw_vel, buff_plan.yaw_acc,
        buff_plan.pitch, buff_plan.pitch_vel, buff_plan.pitch_acc);

    } else
      gimbal.send(false, false, 0, 0, 0, 0, 0, 0, 0);
  }

  quit = true;
  if (plan_thread.joinable()) plan_thread.join();
  gimbal.send(false, false, 0, 0, 0, 0, 0, 0, 0);

  return 0;
}
