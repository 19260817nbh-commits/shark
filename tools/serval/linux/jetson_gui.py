#!/usr/bin/env python3
"""Jetson/Linux launcher: run the vision program and show the web GUI in a native window.

The page is served by ``monitor.Application`` on a local loopback port and
embedded through QtWebEngine, so no browser is needed on the Jetson.  The vision
program sends UDP on ``127.0.0.1`` (``SP_SERIAL_UDP``) and the GUI window shows
the same low-latency SSE data path used by the web page.

Closing the window stops the vision process started by this launcher.  Use
``--classic`` to fall back to the old PyQtGraph ``native_watch.py`` window.

High-rate / low-interference options (second round): ``--push-hz`` (auto follows
the screen refresh rate) and ``--capacity`` (history frames) tune the display
path; by default the GUI process lowers its own scheduling priority (Linux
``nice 10``) *after* the vision child was spawned, so auto-aim keeps the default
priority.  ``--no-low-priority`` disables that.
"""
import argparse
import mmap
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time

SERVAL_ROOT = Path(__file__).resolve().parents[1]
if str(SERVAL_ROOT) not in sys.path:
    sys.path.insert(0, str(SERVAL_ROOT))

LOCAL_BINARIES = ('standard_mpc', 'standard_debug_mpc')
CAPACITY_DEFAULT = 60000
CAPACITY_MIN = 10000
CAPACITY_MAX = 500000


def _stdio_utf8():
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, 'reconfigure'):
            stream.reconfigure(encoding='utf-8')


def _push_hz_value(text):
    """argparse type: ``auto`` or an explicit 20..240 Hz push rate."""
    value = str(text).strip().lower()
    if value == 'auto':
        return 'auto'
    try:
        hz = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError('--push-hz 需要 auto 或 20..240 的整数')
    if not 20 <= hz <= 240:
        raise argparse.ArgumentTypeError('--push-hz 必须在 20..240 之间（或 auto）')
    return hz


def _accepts_parameter(cls, name):
    """True when ``cls.__init__`` accepts the keyword ``name`` (backend-variant safe)."""
    try:
        import inspect
        parameters = inspect.signature(cls.__init__).parameters
    except (TypeError, ValueError):
        return False
    if name in parameters:
        return True
    return any(item.kind is inspect.Parameter.VAR_KEYWORD
               for item in parameters.values())



def _tail_log(path, lines=40):
    """Last lines of vision.log, shown when the vision process aborts."""
    try:
        text = Path(path).read_text(encoding='utf-8', errors='replace')
    except OSError:
        return ''
    return '\n'.join(text.splitlines()[-lines:])


def _watch_vision(process, log_path, closing):
    """Report an unexpected vision exit without stopping the GUI window."""
    code = process.wait()
    if not closing.is_set():
        print(f'视觉进程已退出（returncode={code}）；窗口仍可查看已接收数据。'
              f'详情：{log_path}', flush=True)
        if code:
            tail = _tail_log(log_path)
            if tail.strip():
                print(f'--- {Path(log_path).name} 末尾 ---\n{tail}\n--- 日志结束 ---', flush=True)


def _classic_forwarded(args):
    forwarded = ['--config', str(args.config), '--binary', args.binary,
                 '--project', str(args.project), '--port', str(args.port)]
    if args.preview_fps is not None:
        forwarded.extend(['--preview-fps', str(args.preview_fps)])
    if args.record:
        forwarded.append('--record')
    return forwarded


def main(argv=None):
    _stdio_utf8()
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    parser.add_argument('--config', default='configs/balance1.yaml',
                        help='The YAML actually used by auto-aim (read only)')
    parser.add_argument('--binary', default='standard_mpc',
                        help='standard_mpc (waveforms), standard_debug_mpc '
                             '(camera + waveforms), or a binary path')
    parser.add_argument('--preview-fps', type=int, choices=range(1, 121), metavar='1..120',
                        help='Jetson camera preview rate (default: 60, standard_debug_mpc only)')
    parser.add_argument('--project', type=Path, default=Path(__file__).resolve().parents[3],
                        help='Project root that contains configs/ and build/')
    parser.add_argument('--port', type=int, default=9871,
                        help='Local loopback UDP port (vision -> GUI window)')
    parser.add_argument('--record', action='store_true',
                        help='Also record full raw serial logs on the Jetson')
    parser.add_argument('--push-hz', type=_push_hz_value, default='auto', metavar='{auto|20..240}',
                        help='SSE 推送/刷新率：auto=跟随屏幕刷新率但上限 60（QtWebEngine 5.15 '
                             '实测绘制约 60 FPS），或显式 20..240（高刷屏/高采样调试用）')
    parser.add_argument('--capacity', type=int, default=CAPACITY_DEFAULT, metavar='N',
                        help=f'历史帧容量（帧，夹 {CAPACITY_MIN}..{CAPACITY_MAX}，'
                             f'默认 {CAPACITY_DEFAULT}；500 Hz/方向时 {CAPACITY_DEFAULT} 帧≈60 s）')
    parser.add_argument('--no-low-priority', action='store_true',
                        help='不降低 GUI 进程调度优先级（默认 Linux nice 10，自瞄进程优先）')
    parser.add_argument('--classic', action='store_true',
                        help='Use the old PyQtGraph native_watch.py window instead of the embedded page')
    args = parser.parse_args(argv)

    if args.classic:
        from native_watch import main as native_watch_main
        if args.push_hz != 'auto' or args.capacity != CAPACITY_DEFAULT or args.no_low_priority:
            print('Classic 模式：--push-hz/--capacity/--no-low-priority 仅用于内嵌页面模式', flush=True)
        forwarded = _classic_forwarded(args)
        print('Classic 模式：委托 native_watch.py（旧 PyQtGraph 窗口）', flush=True)
        return native_watch_main(forwarded)

    if os.name != 'posix':
        parser.error('Run jetson_gui.py on the Orin/Linux desktop; '
                     'use windows_gui.py on Windows')
    if not 1 <= args.port <= 65535:
        parser.error('--port must be 1..65535')
    capacity = max(CAPACITY_MIN, min(CAPACITY_MAX, args.capacity))
    if capacity != args.capacity:
        print(f'[GUI] --capacity {args.capacity} 已夹到 {capacity}'
              f'（允许 {CAPACITY_MIN}..{CAPACITY_MAX}）', flush=True)

    root = args.project.resolve()
    binary_arg = Path('build') / args.binary if args.binary in LOCAL_BINARIES else Path(args.binary)
    config = (root / args.config).resolve()
    binary = (root / binary_arg).resolve()
    camera_enabled = binary_arg.name == 'standard_debug_mpc'
    if args.preview_fps is not None and not camera_enabled:
        parser.error('--preview-fps requires --binary standard_debug_mpc')
    preview_fps = args.preview_fps or 60

    from lib.passive import read_project_config
    settings = read_project_config(config)
    if not (settings['com_port'] or '').startswith('/dev/'):
        parser.error('Use the real physical /dev/ serial port in YAML, not the old PTY bridge')
    if not binary.is_file() or not os.access(binary, os.X_OK):
        parser.error('Compile the selected vision binary first: ' + str(binary))
    with binary.open('rb') as source:
        with mmap.mmap(source.fileno(), 0, access=mmap.ACCESS_READ) as content:
            if content.find(b'SP_SERIAL_UDP') < 0:
                parser.error('Binary lacks live UDP support. Rebuild: cmake --build build '
                             f'--target {binary_arg.name} -j2')
            if camera_enabled and content.find(b'SP_CAMERA_PREVIEW_FPS') < 0:
                parser.error('Binary lacks configurable camera preview support. Rebuild: '
                             'cmake --build build --target standard_debug_mpc -j2')
    if not (os.environ.get('DISPLAY') or os.environ.get('WAYLAND_DISPLAY')):
        parser.error('No local desktop (DISPLAY/WAYLAND_DISPLAY missing). Run from an '
                     'Orin desktop terminal; for a remote receiver use native_watch.py '
                     '--target <Windows-IP> plus windows_gui.py on that computer.')

    from lib.desktop_window import apply_low_priority, run_desktop, webengine_available
    if not webengine_available():
        print('QtWebEngine（PyQt5 内嵌浏览器）缺失，无法打开无浏览器 GUI 窗口。\n'
              'Jetson/Linux 安装命令：\n'
              '  sudo apt install -y python3-pyqt5 python3-pyqtgraph python3-pyqt5.qtwebengine\n'
              '或 pip install -r tools/serval/requirements_native.txt；'
              '旧 PyQtGraph 窗口可加 --classic。', file=sys.stderr, flush=True)
        return 1

    # Detect an occupied loopback port before starting the vision process.
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
            probe.bind(('127.0.0.1', args.port))
    except OSError as exc:
        parser.error(f'UDP 回环端口 127.0.0.1:{args.port} 已被占用：{exc}')

    log_dir = root / 'records' / 'serial' / (
        'native_' + time.strftime('%Y%m%d_%H%M%S') + '_' + str(os.getpid()))
    log_dir.mkdir(parents=True, exist_ok=False)
    log = log_dir / 'vision.log'
    preview_dir = preview_path = None
    vision = None
    app = None
    closing = threading.Event()
    code = 0
    try:
        if camera_enabled:
            shared_memory = '/dev/shm' if os.access('/dev/shm', os.W_OK | os.X_OK) else None
            preview_dir = tempfile.TemporaryDirectory(prefix='serval-camera-', dir=shared_memory)
            preview_path = str(Path(preview_dir.name) / 'latest.jpg')

        from monitor import Application
        app_kwargs = dict(udp=dict(host='0.0.0.0', port=args.port),
                          config=str(config), diagnostics=str(log),
                          camera=preview_path, preview_fps=preview_fps)
        if _accepts_parameter(Application, 'capacity'):
            app_kwargs['capacity'] = capacity
        else:
            print('[GUI] 后端 Application 暂不支持 capacity=，沿用后端默认容量'
                  '（待后端 task-6 就绪后生效）', flush=True)
        if args.push_hz != 'auto' and _accepts_parameter(Application, 'push_hz'):
            app_kwargs['push_hz'] = args.push_hz
        app = Application(**app_kwargs)

        env = os.environ.copy()
        env.pop('SP_CAMERA_PREVIEW_JPEG', None)
        env.pop('SP_CAMERA_PREVIEW_FPS', None)
        env.update(SP_SERIAL_UDP=f'127.0.0.1:{args.port}',
                   SP_SERIAL_LOG='1' if args.record else '0',
                   SP_SERIAL_LOG_DIR=str(log_dir),
                   SP_LOG_LEVEL='info',
                   SP_CONTROL_DEBUG='1')
        if camera_enabled:
            env['SP_CAMERA_PREVIEW_JPEG'] = preview_path
            env['SP_CAMERA_PREVIEW_FPS'] = str(preview_fps)
        from orin_watch import stop_child
        with log.open('xb') as output:
            vision = subprocess.Popen([str(binary), str(config)], cwd=str(root), env=env,
                                      stdout=output, stderr=subprocess.STDOUT,
                                      start_new_session=True)
        threading.Thread(target=_watch_vision, args=(vision, log, closing),
                         name='vision-watch', daemon=True).start()
        print(f'Vision PID={vision.pid}; UDP loopback 127.0.0.1:{args.port}; '
              f'auto_fire={settings["auto_fire"]}', flush=True)
        print(f'Log: {log}', flush=True)
        print(f'Records: {log_dir}', flush=True)
        if camera_enabled:
            print(f'Camera preview: {preview_path} ({preview_fps} FPS)', flush=True)
        print(f'历史帧容量 = {capacity} 帧（页面“缓存范围”按实际速率显示可回溯秒数；'
              f'默认 {CAPACITY_DEFAULT} 帧 ≈ 60 s @ 1000 记录/s）', flush=True)
        print('无浏览器 GUI 页面模式：QtWebEngine 内嵌 web/ 页面，数据经 SSE 低延迟推送。', flush=True)
        print('关闭窗口会停止本启动器启动的视觉进程；Ctrl+C 同样干净退出。', flush=True)
        # Lower the GUI priority only after the vision child was spawned: the child
        # inherits the launcher's priority, and auto-aim must stay above the GUI.
        if args.no_low_priority:
            print('[GUI] 低干扰：已禁用（--no-low-priority）', flush=True)
        else:
            print(f'[GUI] 低干扰：{apply_low_priority()}', flush=True)
        code = run_desktop(app, title='Serval · Jetson 串口解算台',
                           preview_fps=preview_fps, push_hz=args.push_hz)
        app = None  # run_desktop already closed the backend
    finally:
        vision_code = vision.poll() if vision is not None else None
        closing.set()
        if vision is not None:
            try:
                from orin_watch import stop_child
                stop_child(vision)
            except OSError as exc:
                print(f'停止视觉进程时出错：{exc}', file=sys.stderr, flush=True)
        if app is not None:
            try:
                app.close()
            except Exception:
                pass
        if preview_dir is not None:
            preview_dir.cleanup()
    if vision_code not in (None, 0):
        print(f'视觉进程退出码 {vision_code}；详情：{log}', flush=True)
        return vision_code
    return code


if __name__ == '__main__':
    def _terminate(_signum, _frame):
        raise KeyboardInterrupt
    signal.signal(signal.SIGTERM, _terminate)
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(0)
    except (OSError, ValueError, RuntimeError, TypeError) as exc:
        print(f'ERROR: {exc}', file=sys.stderr)
        sys.exit(1)
