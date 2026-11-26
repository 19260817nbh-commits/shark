#!/usr/bin/env python3
"""Install optional once-per-second diagnostics for why auto-aim does not control the gimbal.

Run from the project root, then rebuild and launch with SP_CONTROL_DEBUG=1.
Does not change gimbal.send arguments, YAML, detection thresholds or controller decisions.
"""
import argparse
import datetime
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

PATCH = '--- a/src/standard_debug_mpc.cpp\n+++ b/src/standard_debug_mpc.cpp\n@@ -2,2 +2,3 @@\n #include <chrono>\n+#include <cstdlib>\n #include <opencv2/opencv.hpp>\n@@ -64,2 +65,6 @@\n \n+  const char * trace_setting = std::getenv("SP_CONTROL_DEBUG");\n+  const bool control_trace = trace_setting != nullptr && std::string(trace_setting) == "1";\n+  if (control_trace) tools::logger()->info("[ControlTrace] enabled; reports at most once per second");\n+\n   tools::Exiter exiter;\n@@ -105,2 +110,3 @@\n     auto t0 = std::chrono::steady_clock::now();\n+    auto last_control_trace = t0 - 1s;\n     uint16_t last_bullet_count = 0;\n@@ -128,2 +134,12 @@\n \n+        if (control_trace) {\n+          const auto trace_now = std::chrono::steady_clock::now();\n+          if (trace_now - last_control_trace >= 1s) {\n+            tools::logger()->info(\n+              "[ControlTrace] target={} target_id={} control={} planner_fire={} yaw={:.5f} pitch={:.5f} reason={}",\n+              target.has_value(), target_id, plan.control, plan.fire, plan.yaw, plan.pitch,\n+              !target.has_value() ? "no_target" : (plan.control ? "control_ready" : "planner_rejected"));\n+            last_control_trace = trace_now;\n+          }\n+        }\n         gimbal.send(\n@@ -149,2 +165,3 @@\n \n+  auto last_aim_trace = std::chrono::steady_clock::now() - 1s;\n   while (!exiter.exit()) {\n@@ -203,2 +220,10 @@\n       auto armors = yolo.detect(img);\n+      const auto detected_count = armors.size();\n+      int red_count = 0, blue_count = 0;\n+      if (control_trace) {\n+        for (const auto & armor : armors) {\n+          if (armor.color == auto_aim::Color::red) ++red_count;\n+          if (armor.color == auto_aim::Color::blue) ++blue_count;\n+        }\n+      }\n       auto detect_end = std::chrono::steady_clock::now();\n@@ -209,2 +234,13 @@\n       auto targets = tracker.track(armors, t);\n+      if (control_trace) {\n+        const auto trace_now = std::chrono::steady_clock::now();\n+        if (trace_now - last_aim_trace >= 1s) {\n+          tools::logger()->info(\n+            "[AimTrace] rx_mode={} rx_color={} active_color={} detected={} red={} blue={} kept={} tracker={} targets={}",\n+            gimbal.str(mode.load()), static_cast<int>(gs.enemy_color),\n+            auto_aim::COLORS[tracker.enemy_color()], detected_count, red_count, blue_count,\n+            armors.size(), tracker.state(), targets.size());\n+          last_aim_trace = trace_now;\n+        }\n+      }\n       auto track_end = std::chrono::steady_clock::now();\n--- a/src/standard_mpc.cpp\n+++ b/src/standard_mpc.cpp\n@@ -1,2 +1,3 @@\n #include <chrono>\n+#include <cstdlib>\n #include <opencv2/opencv.hpp>\n@@ -63,2 +64,6 @@\n \n+  const char * trace_setting = std::getenv("SP_CONTROL_DEBUG");\n+  const bool control_trace = trace_setting != nullptr && std::string(trace_setting) == "1";\n+  if (control_trace) tools::logger()->info("[ControlTrace] enabled; reports at most once per second");\n+\n   tools::Exiter exiter;\n@@ -104,2 +109,3 @@\n     auto t0 = std::chrono::steady_clock::now();\n+    auto last_control_trace = t0 - 1s;\n     uint16_t last_bullet_count = 0;\n@@ -127,2 +133,12 @@\n \n+        if (control_trace) {\n+          const auto trace_now = std::chrono::steady_clock::now();\n+          if (trace_now - last_control_trace >= 1s) {\n+            tools::logger()->info(\n+              "[ControlTrace] target={} target_id={} control={} planner_fire={} yaw={:.5f} pitch={:.5f} reason={}",\n+              target.has_value(), target_id, plan.control, plan.fire, plan.yaw, plan.pitch,\n+              !target.has_value() ? "no_target" : (plan.control ? "control_ready" : "planner_rejected"));\n+            last_control_trace = trace_now;\n+          }\n+        }\n         gimbal.send(\n@@ -137,2 +153,3 @@\n \n+  auto last_aim_trace = std::chrono::steady_clock::now() - 1s;\n   while (!exiter.exit()) {\n@@ -174,3 +191,22 @@\n       auto armors = yolo.detect(img);\n+      const auto detected_count = armors.size();\n+      int red_count = 0, blue_count = 0;\n+      if (control_trace) {\n+        for (const auto & armor : armors) {\n+          if (armor.color == auto_aim::Color::red) ++red_count;\n+          if (armor.color == auto_aim::Color::blue) ++blue_count;\n+        }\n+      }\n       auto targets = tracker.track(armors, t);\n+      if (control_trace) {\n+        const auto trace_now = std::chrono::steady_clock::now();\n+        if (trace_now - last_aim_trace >= 1s) {\n+          tools::logger()->info(\n+            "[AimTrace] rx_mode={} rx_color={} active_color={} detected={} red={} blue={} kept={} tracker={} targets={}",\n+            gimbal.str(mode.load()), static_cast<int>(gs.enemy_color),\n+            auto_aim::COLORS[tracker.enemy_color()], detected_count, red_count, blue_count,\n+            armors.size(), tracker.state(), targets.size());\n+          last_aim_trace = trace_now;\n+        }\n+      }\n       if (!targets.empty()) {\n--- a/tasks/auto_aim/tracker.hpp\n+++ b/tasks/auto_aim/tracker.hpp\n@@ -33,2 +33,3 @@\n   void set_enemy_color(Color color) { enemy_color_ = color; }\n+  Color enemy_color() const { return enemy_color_; }\n \n--- a/tasks/auto_aim/planner/planner.cpp\n+++ b/tasks/auto_aim/planner/planner.cpp\n@@ -56,3 +56,3 @@\n   } catch (const std::exception & e) {\n-    tools::logger()->warn("Unsolvable target {:.2f}", bullet_speed);\n+    tools::logger()->warn("Unsolvable target {:.2f}: {}", bullet_speed, e.what());\n     return {false};\n'
FILES = ('src/standard_debug_mpc.cpp', 'src/standard_mpc.cpp',
         'tasks/auto_aim/tracker.hpp', 'tasks/auto_aim/planner/planner.cpp')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--project', default='.', type=Path)
    parser.add_argument('--check', action='store_true', help='Check compatibility without editing')
    args = parser.parse_args()
    root = args.project.resolve()
    for name in FILES:
        if not (root/name).is_file():
            raise ValueError('Missing source file; run from the project root: ' + name)
    with tempfile.TemporaryDirectory(prefix='control-trace-') as directory:
        stage = Path(directory)
        subprocess.run(['git', 'init', '-q', str(stage)], check=True)
        for name in FILES:
            dest = stage/name
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(root/name, dest)
        command = ['git', '-C', str(stage), 'apply', '--check', '-']
        check = subprocess.run(command, input=PATCH.encode(), capture_output=True)
        if check.returncode:
            reverse = subprocess.run(['git','-C',str(stage),'apply','--reverse','--check','-'], input=PATCH.encode(),capture_output=True)
            if reverse.returncode == 0:
                print('ALREADY INSTALLED. Rebuild if needed; launch with SP_CONTROL_DEBUG=1.')
                return 0
            raise ValueError('Source version differs; no files changed. '+check.stderr.decode(errors='replace'))
        if args.check:
            print('COMPATIBLE. No files changed.')
            return 0
        subprocess.run(['git','-C',str(stage),'apply','-'], input=PATCH.encode(),check=True)
        changes = {root/name:(stage/name).read_bytes() for name in FILES}
    suffix = '.control-trace-backup-' + datetime.datetime.now().strftime('%Y%m%d-%H%M%S-%f')
    for path in changes:
        shutil.copy2(path,path.with_name(path.name+suffix))
    written=[]
    try:
        for path,data in changes.items():
            written.append(path)
            path.write_bytes(data)
            print('UPDATED:',path)
    except OSError:
        for path in written:
            shutil.copy2(path.with_name(path.name+suffix),path)
        raise
    print('READY: diagnostics installed. Existing control and fire arguments are unchanged.')
    print('Rebuild: cmake --build build --target standard_debug_mpc -j2')
    print('Run: SP_CONTROL_DEBUG=1 python3 tools/serval/linux/orin_watch.py --config configs/balance1_serval.yaml')
    return 0


if __name__ == '__main__':
    try:
        sys.exit(main())
    except (OSError,ValueError,subprocess.CalledProcessError) as exc:
        print('ERROR:',exc,file=sys.stderr)
        sys.exit(1)
