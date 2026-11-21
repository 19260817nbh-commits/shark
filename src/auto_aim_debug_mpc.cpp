#include <fmt/core.h>

#include <atomic>
#include <chrono>
#include <nlohmann/json.hpp>
#include <opencv2/opencv.hpp>
#include <thread>

#include "io/camera.hpp"
#include "io/gimbal/gimbal.hpp"
#include "tasks/auto_aim/planner/planner.hpp"
#include "tasks/auto_aim/solver.hpp"
#include "tasks/auto_aim/tracker.hpp"
#include "tasks/auto_aim/yolo.hpp"
#include "tasks/auto_aim/detector.hpp"
#include "tools/exiter.hpp"
#include "tools/img_tools.hpp"
#include "tools/logger.hpp"
#include "tools/math_tools.hpp"
#include "tools/plotter.hpp"
#include "tools/thread_safe_queue.hpp"

using namespace std::chrono_literals;

const std::string keys =
  "{help h usage ? |                        | 输出命令行参数说明}"
  "{@config-path   | configs/sentry.yaml | 位置参数，yaml配置文件路径 }";

int main(int argc, char * argv[])
{
  tools::Exiter exiter;
  tools::Plotter plotter;

  cv::CommandLineParser cli(argc, argv, keys);
  auto config_path = cli.get<std::string>(0);
  if (cli.has("help") || config_path.empty()) {
    cli.printMessage();
    return 0;
  }

  io::Gimbal gimbal(config_path);
  io::Camera camera(config_path);

  auto_aim::YOLO yolo(config_path, true);
//   auto_aim::Detector detector(config_path, true);

  auto_aim::Solver solver(config_path);
  auto_aim::Tracker tracker(config_path, solver);
  auto_aim::Planner planner(config_path);

  tools::ThreadSafeQueue<std::optional<auto_aim::Target>, true> target_queue(1);
  target_queue.push(std::nullopt);

  std::atomic<bool> quit = false;
  auto plan_thread = std::thread([&]() {
    auto t0 = std::chrono::steady_clock::now();
    uint16_t last_bullet_count = 0;

    while (!quit) {
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

      auto fired = gs.bullet_count > last_bullet_count;
      last_bullet_count = gs.bullet_count;

      nlohmann::json data;
      data["t"] = tools::delta_time(std::chrono::steady_clock::now(), t0);

      data["gimbal_yaw"] = gs.yaw;
      data["gimbal_yaw_vel"] = gs.yaw_vel;
      data["gimbal_pitch"] = gs.pitch;
      data["gimbal_pitch_vel"] = gs.pitch_vel;

      data["target_yaw"] = plan.target_yaw;
      data["target_pitch"] = plan.target_pitch;

      data["plan_yaw"] = plan.yaw;
      data["plan_yaw_vel"] = plan.yaw_vel;
      data["plan_yaw_acc"] = plan.yaw_acc;

      data["plan_pitch"] = plan.pitch;
      data["plan_pitch_vel"] = plan.pitch_vel;
      data["plan_pitch_acc"] = plan.pitch_acc;

      data["fire"] = plan.fire ? 1 : 0;
      data["fired"] = fired ? 1 : 0;

      if (target.has_value()) {
        data["target_z"] = target->ekf_x()[4];   //z
        data["target_vz"] = target->ekf_x()[5];  //vz
      }

      if (target.has_value()) {
        data["w"] = target->ekf_x()[7];
      } else {
        data["w"] = 0.0;
      }

      plotter.plot(data);

      std::this_thread::sleep_for(10ms);
    }
  });

  cv::Mat img;
  std::chrono::steady_clock::time_point t;

  // 延迟统计变量
  double avg_camera_delay = 0.0;
  double avg_detect_delay = 0.0;
  double avg_track_delay = 0.0;
  double avg_total_delay = 0.0;
  int frame_count = 0;
  const double alpha = 0.1; // 指数移动平均的平滑因子

  while (!exiter.exit()) {
    auto loop_start = std::chrono::steady_clock::now();
    
    // 相机读取计时
    auto camera_start = std::chrono::steady_clock::now();
    camera.read(img, t);
    auto camera_end = std::chrono::steady_clock::now();
    double camera_delay = std::chrono::duration<double, std::milli>(camera_end - camera_start).count();
    
    auto q = gimbal.q(t);
    solver.set_R_gimbal2world(q);

    auto gs = gimbal.state();
    if (gs.enemy_color == 0)
      tracker.set_enemy_color(auto_aim::Color::blue);
    else if (gs.enemy_color == 1)
      tracker.set_enemy_color(auto_aim::Color::red);
    
    // 检测计时
    auto detect_start = std::chrono::steady_clock::now();
    auto armors = yolo.detect(img);
    // auto armors = detector.detect(img);
    auto detect_end = std::chrono::steady_clock::now();
    double detect_delay = std::chrono::duration<double, std::milli>(detect_end - detect_start).count();
    
    // 跟踪计时
    auto track_start = std::chrono::steady_clock::now();
    auto targets = tracker.track(armors, t);
    auto track_end = std::chrono::steady_clock::now();
    double track_delay = std::chrono::duration<double, std::milli>(track_end - track_start).count();
    
    if (!targets.empty())
      target_queue.push(targets.front());
    else
      target_queue.push(std::nullopt);

    // 总延迟计算
    auto loop_end = std::chrono::steady_clock::now();
    double total_delay = std::chrono::duration<double, std::milli>(loop_end - loop_start).count();
    
    // 使用指数移动平均更新延迟统计
    if (frame_count == 0) {
      avg_camera_delay = camera_delay;
      avg_detect_delay = detect_delay;
      avg_track_delay = track_delay;
      avg_total_delay = total_delay;
    } else {
      avg_camera_delay = alpha * camera_delay + (1 - alpha) * avg_camera_delay;
      avg_detect_delay = alpha * detect_delay + (1 - alpha) * avg_detect_delay;
      avg_track_delay = alpha * track_delay + (1 - alpha) * avg_track_delay;
      avg_total_delay = alpha * total_delay + (1 - alpha) * avg_total_delay;
    }
    frame_count++;

    if (!targets.empty()) {
      auto target = targets.front();

      // 当前帧target更新后
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
    }

    cv::resize(img, img, {}, 0.5, 0.5);  // 显示时缩小图片尺寸
    
    // 在图像上显示延迟统计信息
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
    
    cv::putText(img, fmt::format("Total: {:.2f} ms", avg_total_delay), 
                cv::Point(10, text_y), cv::FONT_HERSHEY_SIMPLEX, font_scale, text_color, text_thickness);
    text_y += text_line_height;
    
    cv::putText(img, fmt::format("FPS: {:.1f}", 1000.0 / avg_total_delay), 
                cv::Point(10, text_y), cv::FONT_HERSHEY_SIMPLEX, font_scale, text_color, text_thickness);
    
    cv::imshow("reprojection", img);
    auto key = cv::waitKey(1);
    if (key == 'q') break;
  }

  quit = true;
  if (plan_thread.joinable()) plan_thread.join();
  gimbal.send(false, false, 0, 0, 0, 0, 0, 0, 0);

  return 0;
}