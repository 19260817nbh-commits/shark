#include <chrono>
#include <opencv2/opencv.hpp>
#include <thread>

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

  tools::Exiter exiter;
  tools::Plotter plotter;
  tools::Recorder recorder;

  io::Camera camera(config_path);
  io::CBoard cboard(config_path);

  auto_aim::multithread::MultiThreadDetector detector(config_path);
  auto_aim::Solver solver(config_path);
  auto_aim::Tracker tracker(config_path, solver);
  auto_aim::Aimer aimer(config_path);
  auto_aim::Shooter shooter(config_path);

  auto_buff::Buff_Detector buff_detector(config_path);
  auto_buff::Solver buff_solver(config_path);
  auto buff_target_params = auto_buff::load_target_params(config_path);
  auto_buff::SmallTarget buff_small_target(buff_target_params);
  auto_buff::BigTargetGroup buff_big_group(buff_target_params);
  auto_buff::BigTargetSelector buff_big_selector(config_path);
  auto_buff::Aimer buff_aimer(config_path);

  auto_aim::multithread::CommandGener commandgener(shooter, aimer, cboard, plotter);

  std::atomic<io::Mode> mode{io::Mode::idle};
  auto last_mode{io::Mode::idle};

  auto detect_thread = std::thread([&]() {
    cv::Mat img;
    std::chrono::steady_clock::time_point t;

    while (!exiter.exit()) {
      if (mode.load() == io::Mode::auto_aim) {
        camera.read(img, t);
        detector.push(img, t);
      } else
        continue;
    }
  });

  while (!exiter.exit()) {
    mode = cboard.mode;

    if (last_mode != mode) {
      tools::logger()->info("Switch to {}", io::MODES[mode]);
      if (last_mode == io::Mode::big_buff && mode != io::Mode::big_buff) {
        buff_big_selector.reset_cycle();
      }
      last_mode = mode.load();
    }

    /// 自瞄
    if (mode.load() == io::Mode::auto_aim) {
      auto [img, armors, t] = detector.debug_pop();
      Eigen::Quaterniond q = cboard.imu_at(t - 1ms);

      // recorder.record(img, q, t);

      solver.set_R_gimbal2world(q);

      Eigen::Vector3d ypr = tools::eulers(solver.R_gimbal2world(), 2, 1, 0);

      auto targets = tracker.track(armors, t);

      commandgener.push(targets, t, cboard.bullet_speed, ypr);  // 发送给决策线程

    }

    /// 打符
    else if (mode.load() == io::Mode::small_buff || mode.load() == io::Mode::big_buff) {
      cv::Mat img;
      Eigen::Quaterniond q;
      std::chrono::steady_clock::time_point t;

      camera.read(img, t);
      q = cboard.imu_at(t - 1ms);

      // recorder.record(img, q, t);

      buff_solver.set_R_gimbal2world(q);

      io::Command buff_command = {false, false, 0, 0};
      if (mode.load() == io::Mode::small_buff) {
        auto observations = buff_detector.detect(img, auto_buff::SMALL);
        buff_solver.solve(observations);
        std::optional<auto_buff::PowerRune> power_rune =
          observations.empty() ? std::nullopt
                               : std::optional<auto_buff::PowerRune>(observations.front());

        buff_small_target.get_target(power_rune, t);
        auto target_copy = buff_small_target;
        buff_command = buff_aimer.aim(target_copy, t, cboard.bullet_speed, true);
      } else {
        auto observations = buff_detector.detect(img, auto_buff::BIG);
        buff_solver.solve(observations);
        buff_big_group.update(observations, t);

        Eigen::Vector3d ypr = tools::eulers(buff_solver.R_gimbal2world(), 2, 1, 0);
        auto selected_target = buff_big_selector.select_target(
          buff_big_group, buff_aimer, t, cboard.bullet_speed, ypr[0], ypr[1],
          std::chrono::steady_clock::now());
        if (selected_target.has_value()) {
          auto target_copy = selected_target.value();
          buff_command = buff_aimer.aim(target_copy, t, cboard.bullet_speed, true);
          if (buff_command.shoot) {
            buff_big_selector.on_fire(std::chrono::steady_clock::now());
          }
        }
      }
      cboard.send(buff_command);

    } else
      continue;
  }

  detect_thread.join();

  return 0;
}