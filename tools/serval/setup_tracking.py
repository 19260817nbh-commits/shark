#!/usr/bin/env python3
"""Set up config-selected enemy color and tracking without vision fire requests.

Run from the project root: python3 tools/serval/setup_tracking.py --config configs/balance1_serval.yaml
Changes only the two standard MPC entrypoints and three keys in the selected YAML.
Creates timestamped backups. Does not compile, run the robot, or open serial ports.
"""
import argparse
import datetime
import pathlib
import re
import shutil
import subprocess
import sys
import tempfile

COMBINED_PATCH = 'diff --git a/src/standard_debug_mpc.cpp b/src/standard_debug_mpc.cpp\nindex 1668aa9..df6c253 100644\n--- a/src/standard_debug_mpc.cpp\n+++ b/src/standard_debug_mpc.cpp\n@@ -2,6 +2,7 @@\n #include <chrono>\n #include <opencv2/opencv.hpp>\n #include <thread>\n+#include <yaml-cpp/yaml.h>\n \n #include "io/camera.hpp"\n #include "io/dm_imu/dm_imu.hpp"\n@@ -23,6 +24,7 @@\n #include "tools/math_tools.hpp"\n #include "tools/plotter.hpp"\n #include "tools/recorder.hpp"\n+#include "tools/yaml.hpp"\n \n const std::string keys =\n   "{help h usage ? | | 输出命令行参数说明}"\n@@ -39,6 +41,27 @@ int main(int argc, char * argv[])\n     return 0;\n   }\n \n+  auto yaml = tools::load(config_path);\n+  const auto enemy_color_source = yaml["enemy_color_source"]\n+                                    ? yaml["enemy_color_source"].as<std::string>()\n+                                    : std::string("serial");\n+  const auto configured_enemy_color = tools::read<std::string>(yaml, "enemy_color");\n+  if (enemy_color_source != "serial" && enemy_color_source != "config") {\n+    tools::logger()->error("[YAML] enemy_color_source must be serial or config");\n+    return 1;\n+  }\n+  if (enemy_color_source == "config" &&\n+      configured_enemy_color != "red" && configured_enemy_color != "blue") {\n+    tools::logger()->error("[YAML] enemy_color must be red or blue");\n+    return 1;\n+  }\n+  tools::logger()->info(\n+    "[EnemyColor] source={} configured={}", enemy_color_source, configured_enemy_color);\n+\n+  const auto fire_config = YAML::LoadFile(config_path);\n+  const bool auto_fire = fire_config["auto_fire"] && fire_config["auto_fire"].as<bool>();\n+  tools::logger()->info("[Fire] auto_fire={}", auto_fire);\n+\n   tools::Exiter exiter;\n   tools::Plotter plotter;\n   tools::Recorder recorder;\n@@ -55,6 +78,11 @@ int main(int argc, char * argv[])\n   target_queue.push(std::nullopt);\n \n   auto_buff::Buff_Detector buff_detector(config_path);\n+  if (enemy_color_source == "config") {\n+    tracker.set_enemy_color(\n+      configured_enemy_color == "blue" ? auto_aim::Color::blue : auto_aim::Color::red);\n+    buff_detector.set_enemy_color(configured_enemy_color);\n+  }\n   auto_buff::Solver buff_solver(config_path);\n   auto buff_target_params = auto_buff::load_target_params(config_path);\n   auto_buff::SmallTarget buff_small_target(buff_target_params);\n@@ -99,7 +127,7 @@ int main(int argc, char * argv[])\n         }\n \n         gimbal.send(\n-          plan.control, plan.fire, target_id, plan.yaw, plan.yaw_vel, plan.yaw_acc, plan.pitch, plan.pitch_vel,\n+          plan.control, auto_fire && plan.fire, target_id, plan.yaw, plan.yaw_vel, plan.yaw_acc, plan.pitch, plan.pitch_vel,\n           plan.pitch_acc);\n \n         std::this_thread::sleep_for(10ms);\n@@ -150,10 +178,10 @@ int main(int argc, char * argv[])\n     }\n     auto q = gimbal.q(t);\n     auto gs = gimbal.state();\n-    if (gs.enemy_color == 0) {\n+    if (enemy_color_source == "serial" && gs.enemy_color == 0) {\n       tracker.set_enemy_color(auto_aim::Color::blue);\n       buff_detector.set_enemy_color("blue");\n-    } else if (gs.enemy_color == 1) {\n+    } else if (enemy_color_source == "serial" && gs.enemy_color == 1) {\n       tracker.set_enemy_color(auto_aim::Color::red);\n       buff_detector.set_enemy_color("red");\n     }\n@@ -230,13 +258,13 @@ int main(int argc, char * argv[])\n         if (selected_target.has_value()) {\n           auto target_copy = selected_target.value();\n           buff_plan = buff_aimer.mpc_aim(target_copy, t, gs, true);\n-          if (buff_plan.fire) {\n+          if (auto_fire && buff_plan.control && buff_plan.fire) {\n             buff_big_selector.on_fire(std::chrono::steady_clock::now());\n           }\n         }\n       }\n       gimbal.send(\n-        buff_plan.control, buff_plan.fire, 0, buff_plan.yaw, buff_plan.yaw_vel, buff_plan.yaw_acc,\n+        buff_plan.control, auto_fire && buff_plan.fire, 0, buff_plan.yaw, buff_plan.yaw_vel, buff_plan.yaw_acc,\n         buff_plan.pitch, buff_plan.pitch_vel, buff_plan.pitch_acc);\n \n     } else\ndiff --git a/src/standard_mpc.cpp b/src/standard_mpc.cpp\nindex 0e3d94c..cf1b6ed 100644\n--- a/src/standard_mpc.cpp\n+++ b/src/standard_mpc.cpp\n@@ -1,6 +1,7 @@\n #include <chrono>\n #include <opencv2/opencv.hpp>\n #include <thread>\n+#include <yaml-cpp/yaml.h>\n \n #include "io/camera.hpp"\n #include "io/dm_imu/dm_imu.hpp"\n@@ -22,6 +23,7 @@\n #include "tools/math_tools.hpp"\n #include "tools/plotter.hpp"\n #include "tools/recorder.hpp"\n+#include "tools/yaml.hpp"\n \n const std::string keys =\n   "{help h usage ? | | 输出命令行参数说明}"\n@@ -38,6 +40,27 @@ int main(int argc, char * argv[])\n     return 0;\n   }\n \n+  auto yaml = tools::load(config_path);\n+  const auto enemy_color_source = yaml["enemy_color_source"]\n+                                    ? yaml["enemy_color_source"].as<std::string>()\n+                                    : std::string("serial");\n+  const auto configured_enemy_color = tools::read<std::string>(yaml, "enemy_color");\n+  if (enemy_color_source != "serial" && enemy_color_source != "config") {\n+    tools::logger()->error("[YAML] enemy_color_source must be serial or config");\n+    return 1;\n+  }\n+  if (enemy_color_source == "config" &&\n+      configured_enemy_color != "red" && configured_enemy_color != "blue") {\n+    tools::logger()->error("[YAML] enemy_color must be red or blue");\n+    return 1;\n+  }\n+  tools::logger()->info(\n+    "[EnemyColor] source={} configured={}", enemy_color_source, configured_enemy_color);\n+\n+  const auto fire_config = YAML::LoadFile(config_path);\n+  const bool auto_fire = fire_config["auto_fire"] && fire_config["auto_fire"].as<bool>();\n+  tools::logger()->info("[Fire] auto_fire={}", auto_fire);\n+\n   tools::Exiter exiter;\n   tools::Plotter plotter;\n   // tools::Recorder recorder;\n@@ -54,6 +77,11 @@ int main(int argc, char * argv[])\n   target_queue.push(std::nullopt);\n \n   auto_buff::Buff_Detector buff_detector(config_path);\n+  if (enemy_color_source == "config") {\n+    tracker.set_enemy_color(\n+      configured_enemy_color == "blue" ? auto_aim::Color::blue : auto_aim::Color::red);\n+    buff_detector.set_enemy_color(configured_enemy_color);\n+  }\n   auto_buff::Solver buff_solver(config_path);\n   auto buff_target_params = auto_buff::load_target_params(config_path);\n   auto_buff::SmallTarget buff_small_target(buff_target_params);\n@@ -98,7 +126,7 @@ int main(int argc, char * argv[])\n         }\n \n         gimbal.send(\n-          plan.control, plan.fire, target_id, plan.yaw, plan.yaw_vel, plan.yaw_acc, plan.pitch, plan.pitch_vel,\n+          plan.control, auto_fire && plan.fire, target_id, plan.yaw, plan.yaw_vel, plan.yaw_acc, plan.pitch, plan.pitch_vel,\n           plan.pitch_acc);\n \n         std::this_thread::sleep_for(10ms);\n@@ -130,10 +158,10 @@ int main(int argc, char * argv[])\n     }\n     auto q = gimbal.q(t);\n     auto gs = gimbal.state();\n-    if (gs.enemy_color == 0) {\n+    if (enemy_color_source == "serial" && gs.enemy_color == 0) {\n       tracker.set_enemy_color(auto_aim::Color::blue);\n       buff_detector.set_enemy_color("blue");\n-    } else if (gs.enemy_color == 1) {\n+    } else if (enemy_color_source == "serial" && gs.enemy_color == 1) {\n       tracker.set_enemy_color(auto_aim::Color::red);\n       buff_detector.set_enemy_color("red");\n     }\n@@ -183,13 +211,13 @@ int main(int argc, char * argv[])\n         if (selected_target.has_value()) {\n           auto target_copy = selected_target.value();\n           buff_plan = buff_aimer.mpc_aim(target_copy, t, gs, true);\n-          if (buff_plan.fire) {\n+          if (auto_fire && buff_plan.control && buff_plan.fire) {\n             buff_big_selector.on_fire(std::chrono::steady_clock::now());\n           }\n         }\n       }\n       gimbal.send(\n-        buff_plan.control, buff_plan.fire, 0, buff_plan.yaw, buff_plan.yaw_vel, buff_plan.yaw_acc,\n+        buff_plan.control, auto_fire && buff_plan.fire, 0, buff_plan.yaw, buff_plan.yaw_vel, buff_plan.yaw_acc,\n         buff_plan.pitch, buff_plan.pitch_vel, buff_plan.pitch_acc);\n \n     } else\n'
FIRE_PATCH = '--- a/src/standard_mpc.cpp\n+++ b/src/standard_mpc.cpp\n@@ -3,2 +3,3 @@\n #include <thread>\n+#include <yaml-cpp/yaml.h>\n \n@@ -58,2 +59,6 @@\n \n+  const auto fire_config = YAML::LoadFile(config_path);\n+  const bool auto_fire = fire_config["auto_fire"] && fire_config["auto_fire"].as<bool>();\n+  tools::logger()->info("[Fire] auto_fire={}", auto_fire);\n+\n   tools::Exiter exiter;\n@@ -123,3 +128,3 @@\n         gimbal.send(\n-          plan.control, plan.fire, target_id, plan.yaw, plan.yaw_vel, plan.yaw_acc, plan.pitch, plan.pitch_vel,\n+          plan.control, auto_fire && plan.fire, target_id, plan.yaw, plan.yaw_vel, plan.yaw_acc, plan.pitch, plan.pitch_vel,\n           plan.pitch_acc);\n@@ -208,3 +213,3 @@\n           buff_plan = buff_aimer.mpc_aim(target_copy, t, gs, true);\n-          if (buff_plan.fire) {\n+          if (auto_fire && buff_plan.control && buff_plan.fire) {\n             buff_big_selector.on_fire(std::chrono::steady_clock::now());\n@@ -214,3 +219,3 @@\n       gimbal.send(\n-        buff_plan.control, buff_plan.fire, 0, buff_plan.yaw, buff_plan.yaw_vel, buff_plan.yaw_acc,\n+        buff_plan.control, auto_fire && buff_plan.fire, 0, buff_plan.yaw, buff_plan.yaw_vel, buff_plan.yaw_acc,\n         buff_plan.pitch, buff_plan.pitch_vel, buff_plan.pitch_acc);\n--- a/src/standard_debug_mpc.cpp\n+++ b/src/standard_debug_mpc.cpp\n@@ -4,2 +4,3 @@\n #include <thread>\n+#include <yaml-cpp/yaml.h>\n \n@@ -59,2 +60,6 @@\n \n+  const auto fire_config = YAML::LoadFile(config_path);\n+  const bool auto_fire = fire_config["auto_fire"] && fire_config["auto_fire"].as<bool>();\n+  tools::logger()->info("[Fire] auto_fire={}", auto_fire);\n+\n   tools::Exiter exiter;\n@@ -124,3 +129,3 @@\n         gimbal.send(\n-          plan.control, plan.fire, target_id, plan.yaw, plan.yaw_vel, plan.yaw_acc, plan.pitch, plan.pitch_vel,\n+          plan.control, auto_fire && plan.fire, target_id, plan.yaw, plan.yaw_vel, plan.yaw_acc, plan.pitch, plan.pitch_vel,\n           plan.pitch_acc);\n@@ -255,3 +260,3 @@\n           buff_plan = buff_aimer.mpc_aim(target_copy, t, gs, true);\n-          if (buff_plan.fire) {\n+          if (auto_fire && buff_plan.control && buff_plan.fire) {\n             buff_big_selector.on_fire(std::chrono::steady_clock::now());\n@@ -261,3 +266,3 @@\n       gimbal.send(\n-        buff_plan.control, buff_plan.fire, 0, buff_plan.yaw, buff_plan.yaw_vel, buff_plan.yaw_acc,\n+        buff_plan.control, auto_fire && buff_plan.fire, 0, buff_plan.yaw, buff_plan.yaw_vel, buff_plan.yaw_acc,\n         buff_plan.pitch, buff_plan.pitch_vel, buff_plan.pitch_acc);\n'
SOURCES = ('src/standard_mpc.cpp', 'src/standard_debug_mpc.cpp')


def configure_yaml(text):
    for key, value in (('enemy_color', '"blue"'), ('enemy_color_source', '"config"'), ('auto_fire', 'false')):
        pattern = re.compile(r'^' + key + r'[ \t]*:[^\r\n]*', re.MULTILINE)
        matches = list(pattern.finditer(text))
        if len(matches) > 1:
            raise ValueError('Duplicate YAML key: ' + key)
        replacement = key + ': ' + value
        if matches:
            text = pattern.sub(lambda match: replacement, text)
        else:
            text = text.rstrip('\r\n') + '\n' + replacement + '\n'
    return text


def git_apply(folder, patch, reverse=False):
    command = ['git', '-C', str(folder), 'apply', '--check']
    if reverse:
        command.append('--reverse')
    result = subprocess.run(command + ['-'], input=patch.encode(), stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if result.returncode == 0 and not reverse:
        subprocess.run(['git', '-C', str(folder), 'apply', '-'], input=patch.encode(), check=True)
    return result.returncode == 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', required=True, help='The exact YAML used to start the vision program')
    args = parser.parse_args()
    root = pathlib.Path.cwd()
    config = (root / args.config).resolve()
    paths = [root / name for name in SOURCES]
    if not config.is_file() or not all(path.is_file() for path in paths):
        raise ValueError('Run from the project root and specify an existing config YAML.')
    updated_config = configure_yaml(config.read_text(encoding='utf-8'))
    with tempfile.TemporaryDirectory(prefix='tracking-setup-') as directory:
        stage = pathlib.Path(directory)
        subprocess.run(['git', 'init', '-q', str(stage)], check=True)
        for name, path in zip(SOURCES, paths):
            dest = stage / name
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, dest)
        if not git_apply(stage, COMBINED_PATCH) and not git_apply(stage, FIRE_PATCH):
            if not git_apply(stage, COMBINED_PATCH, reverse=True):
                raise ValueError('Source version differs from the expected version. No project files changed; share this error for adaptation.')
        updated = {path: (stage / name).read_bytes() for name, path in zip(SOURCES, paths)}
    updated[config] = updated_config.encode('utf-8')
    changed = {path: data for path, data in updated.items() if path.read_bytes() != data}
    suffix = '.tracking-backup-' + datetime.datetime.now().strftime('%Y%m%d-%H%M%S-%f')
    # Back up every changed file before modifying any project file.
    for path in changed:
        backup = path.with_name(path.name + suffix)
        shutil.copy2(path, backup)
        print('BACKUP:', backup)
    written = []
    try:
        for path, data in changed.items():
            written.append(path)
            path.write_bytes(data)
            print('UPDATED:', path)
    except OSError:
        for path in written:
            shutil.copy2(path.with_name(path.name + suffix), path)
        raise
    print('READY: enemy_color=blue, enemy_color_source=config, auto_fire=false')
    print('Source/config setup only. Rebuild both executables and restart vision to activate.')
    return 0


if __name__ == '__main__':
    try:
        sys.exit(main())
    except (OSError, ValueError, subprocess.CalledProcessError) as exc:
        print('ERROR:', exc, file=sys.stderr)
        sys.exit(1)
