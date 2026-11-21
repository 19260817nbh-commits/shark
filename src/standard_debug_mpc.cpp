#include <fmt/core.h>
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

  io::Gimbal gimbal(config_path);
  io::Camera camera(config_path);

  auto_aim::YOLO yolo(config_path, true);
  auto_aim::Solver solver(config_path);
  auto_aim::Tracker tracker(config_path, solver, true);
  auto_aim::Planner planner(config_path);

  tools::ThreadSafeQueue<std::optional<auto_aim::Target>, true> target_queue(1);
  target_queue.push(std::nullopt);

  auto_buff::Buff_Detector buff_detector(config_path);
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

        gimbal.send(
          plan.control, plan.fire, target_id, plan.yaw, plan.yaw_vel, plan.yaw_acc, plan.pitch, plan.pitch_vel,
          plan.pitch_acc);

        std::this_thread::sleep_for(10ms);
      } else
        std::this_thread::sleep_for(200ms);
    }
  });

  // 延迟统计变量
  double avg_camera_delay = 0.0;
  double avg_detect_delay = 0.0;
  double avg_track_delay = 0.0;
  double avg_record_delay = 0.0;
  double avg_display_delay = 0.0;
  double avg_total_delay = 0.0;
  double display_fps = 0.0;
  int frame_count = 0;
  const double alpha = 0.1; // 指数移动平均的平滑因子

  while (!exiter.exit()) {
    auto loop_start = std::chrono::steady_clock::now();

    mode = gimbal.mode();

    if (last_mode != mode) {
      tools::logger()->info("Switch to {}", gimbal.str(mode));
      if (last_mode == io::GimbalMode::BIG_BUFF && mode != io::GimbalMode::BIG_BUFF) {
        buff_big_selector.reset_cycle();
      }
      last_mode = mode.load();
    }

    // 相机读取计时
    auto camera_start = std::chrono::steady_clock::now();
    camera.read(img, t);
    auto camera_end = std::chrono::steady_clock::now();
    double camera_delay = std::chrono::duration<double, std::milli>(camera_end - camera_start).count();

    ++frame_counter;
    auto now = std::chrono::steady_clock::now();
    auto elapsed = std::chrono::duration_cast<std::chrono::milliseconds>(now - last_fps_log);
    if (elapsed.count() >= 1000) {
      auto fps = static_cast<double>(frame_counter) / (elapsed.count() / 1000.0);
      tools::logger()->info("standard_debug_mpc fps: {:.2f}", fps);
      display_fps = fps;
      frame_counter = 0;
      last_fps_log = now;
    }
    auto q = gimbal.q(t);
    auto gs = gimbal.state();
    if (gs.enemy_color == 0) {
      tracker.set_enemy_color(auto_aim::Color::blue);
      buff_detector.set_enemy_color("blue");
    } else if (gs.enemy_color == 1) {
      tracker.set_enemy_color(auto_aim::Color::red);
      buff_detector.set_enemy_color("red");
    }
    
    auto record_start = std::chrono::steady_clock::now();
    recorder.record(img, q, t);
    auto record_end = std::chrono::steady_clock::now();
    double record_delay = std::chrono::duration<double, std::milli>(record_end - record_start).count();

    solver.set_R_gimbal2world(q);

    double detect_delay = 0.0;
    double track_delay = 0.0;

    /// 自瞄
    if (mode.load() == io::GimbalMode::AUTO_AIM) {
      // 检测计时
      auto detect_start = std::chrono::steady_clock::now();
      auto armors = yolo.detect(img);
      auto detect_end = std::chrono::steady_clock::now();
      detect_delay = std::chrono::duration<double, std::milli>(detect_end - detect_start).count();

      // 跟踪计时
      auto track_start = std::chrono::steady_clock::now();
      auto targets = tracker.track(armors, t);
      auto track_end = std::chrono::steady_clock::now();
      track_delay = std::chrono::duration<double, std::milli>(track_end - track_start).count();

      if (!targets.empty()) {
        target_queue.push(targets.front());
        
        // 重投影绘制
        auto target = targets.front();
        std::vector<Eigen::Vector4d> armor_xyza_list = target.armor_xyza_list();
        for (const Eigen::Vector4d & xyza : armor_xyza_list) {
          auto image_points =
            solver.reproject_armor(xyza.head(3), xyza[3], target.armor_type, target.name);
          tools::draw_points(img, image_points, {0, 255, 0});
        }

        Eigen::Vector4d aim_xyza = planner.debug_xyza;
        auto image_points =
          solver.reproject_armor(aim_xyza.head(3), aim_xyza[3], target.armor_type, target.name);
        tools::draw_points(img, image_points, {0, 0, 255});

      } else {
        target_queue.push(std::nullopt);
      }
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
          if (buff_plan.fire) {
            buff_big_selector.on_fire(std::chrono::steady_clock::now());
          }
        }
      }
      gimbal.send(
        buff_plan.control, buff_plan.fire, 0, buff_plan.yaw, buff_plan.yaw_vel, buff_plan.yaw_acc,
        buff_plan.pitch, buff_plan.pitch_vel, buff_plan.pitch_acc);

    } else
      gimbal.send(false, false, 0, 0, 0, 0, 0, 0, 0);

    // 总延迟计算
    auto loop_end = std::chrono::steady_clock::now();
    double total_delay = std::chrono::duration<double, std::milli>(loop_end - loop_start).count();
    
    // 使用指数移动平均更新延迟统计
    if (frame_count == 0) {
      avg_camera_delay = camera_delay;
      avg_detect_delay = detect_delay;
      avg_track_delay = track_delay;
      avg_record_delay = record_delay;
      avg_total_delay = total_delay;
    } else {
      avg_camera_delay = alpha * camera_delay + (1 - alpha) * avg_camera_delay;
      avg_detect_delay = alpha * detect_delay + (1 - alpha) * avg_detect_delay;
      avg_track_delay = alpha * track_delay + (1 - alpha) * avg_track_delay;
      avg_record_delay = alpha * record_delay + (1 - alpha) * avg_record_delay;
      avg_total_delay = alpha * total_delay + (1 - alpha) * avg_total_delay;
    }
    frame_count++;

    // 显示画面和延迟信息
    cv::resize(img, img, {}, 0.5, 0.5);  // 显示时缩小图片尺寸
    
    int text_y = 30;
    int text_line_height = 30;
    cv::Scalar text_color(0, 255, 255); // 黄色
    int text_thickness = 2;
    double font_scale = 0.7;
    
    cv::putText(img, fmt::format("Camera: {:.2f} ms", avg_camera_delay), 
                cv::Point(10, text_y), cv::FONT_HERSHEY_SIMPLEX, font_scale, text_color, text_thickness);
    text_y += text_line_height;
    
    cv::putText(img, fmt::format("Detect: {:.2f} ms", avg_detect_delay), 
                cv::Point(10, text_y), cv::FONT_HERSHEY_SIMPLEX, font_scale, text_color, text_thickness);
    text_y += text_line_height;
    
    cv::putText(img, fmt::format("Track: {:.2f} ms", avg_track_delay), 
                cv::Point(10, text_y), cv::FONT_HERSHEY_SIMPLEX, font_scale, text_color, text_thickness);
    text_y += text_line_height;

    cv::putText(img, fmt::format("Record: {:.2f} ms", avg_record_delay), 
                cv::Point(10, text_y), cv::FONT_HERSHEY_SIMPLEX, font_scale, text_color, text_thickness);
    text_y += text_line_height;

    cv::putText(img, fmt::format("Display: {:.2f} ms", avg_display_delay), 
                cv::Point(10, text_y), cv::FONT_HERSHEY_SIMPLEX, font_scale, text_color, text_thickness);
    text_y += text_line_height;
    
    cv::putText(img, fmt::format("Total(Alg): {:.2f} ms", avg_total_delay), 
                cv::Point(10, text_y), cv::FONT_HERSHEY_SIMPLEX, font_scale, text_color, text_thickness);
    text_y += text_line_height;
    
    cv::putText(img, fmt::format("FPS: {:.1f}", display_fps), 
                cv::Point(10, text_y), cv::FONT_HERSHEY_SIMPLEX, font_scale, text_color, text_thickness);
    
    auto display_start = std::chrono::steady_clock::now();
    cv::imshow("standard_debug_mpc", img);
    auto key = cv::waitKey(1);
    auto display_end = std::chrono::steady_clock::now();
    double display_delay = std::chrono::duration<double, std::milli>(display_end - display_start).count();

    if (frame_count == 1) {
        avg_display_delay = display_delay;
    } else {
        avg_display_delay = alpha * display_delay + (1 - alpha) * avg_display_delay;
    }

    if (key == 'q') break;
  }

  quit = true;
  if (plan_thread.joinable()) plan_thread.join();
  gimbal.send(false, false, 0, 0, 0, 0, 0, 0, 0);

  return 0;
}
