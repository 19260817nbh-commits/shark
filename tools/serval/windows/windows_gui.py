#!/usr/bin/env python3
"""Windows (or Linux) receiver: show the web GUI in a native window, no browser.

The Jetson vision program sends UDP (``SP_SERIAL_UDP=<windows-ip>:<port>``);
this launcher listens for it and embeds the Serval ``web/`` page through
QtWebEngine.  The old PyQtGraph scope stays available via ``--classic``.

Closing the window stops only this receiver; the Jetson vision program keeps
running.

High-rate / low-interference options (second round): ``--push-hz`` (auto follows
the screen refresh rate) and ``--capacity`` (history frames).  By default the
receiver process lowers its own scheduling priority (Windows
``BELOW_NORMAL_PRIORITY_CLASS``); ``--no-low-priority`` disables that.
"""
import argparse
import ipaddress
import os
from pathlib import Path
import signal
import sys

SERVAL_ROOT = Path(__file__).resolve().parents[1]
if str(SERVAL_ROOT) not in sys.path:
    sys.path.insert(0, str(SERVAL_ROOT))

INSTALL_HINT_WINDOWS = 'py -3 -m pip install -r tools/serval/requirements_native.txt'
INSTALL_HINT_LINUX = ('sudo apt install -y python3-pyqt5 python3-pyqtgraph '
                      'python3-pyqt5.qtwebengine')
CAPACITY_DEFAULT = 60000
CAPACITY_MIN = 10000
CAPACITY_MAX = 500000


def _stdio_utf8():
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, 'reconfigure'):
            stream.reconfigure(encoding='utf-8')


def _install_hint():
    return INSTALL_HINT_WINDOWS if os.name == 'nt' else INSTALL_HINT_LINUX


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



def _classic_forwarded(args):
    forwarded = ['--host', args.host, '--port', str(args.port)]
    if args.source_ip:
        forwarded.extend(['--source-ip', args.source_ip])
    return forwarded


def main(argv=None):
    _stdio_utf8()
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    parser.add_argument('--host', default='0.0.0.0',
                        help='UDP listen address; 0.0.0.0 accepts the LAN')
    parser.add_argument('--port', type=int, default=9871,
                        help='UDP port the Jetson vision is sending to')
    parser.add_argument('--source-ip', help='Only accept this Jetson IPv4 address')
    parser.add_argument('--config', help='Optional YAML actually used by the vision '
                                         '(read only; shown on the page for reference)')
    parser.add_argument('--push-hz', type=_push_hz_value, default='auto', metavar='{auto|20..240}',
                        help='SSE 推送/刷新率：auto=屏幕刷新但上限 240（传输+解析目标 ≤15 ms；'
                             '屏幕绘制仍约 60 FPS）。显式 20..240 可覆盖')
    parser.add_argument('--capacity', type=int, default=CAPACITY_DEFAULT, metavar='N',
                        help=f'历史帧容量（帧，夹 {CAPACITY_MIN}..{CAPACITY_MAX}，'
                             f'默认 {CAPACITY_DEFAULT}；500 Hz/方向时 {CAPACITY_DEFAULT} 帧≈60 s）')
    parser.add_argument('--no-low-priority', action='store_true',
                        help='不降低接收端进程调度优先级（默认 Windows 低于普通，Linux nice 10）')
    parser.add_argument('--classic', action='store_true',
                        help='Use the old PyQtGraph native_scope.py window instead of the embedded page')
    args = parser.parse_args(argv)

    if args.classic:
        from native_scope import main as native_scope_main
        if args.config:
            print('Classic 模式：旧 native_scope.py 不读取 --config，已忽略', flush=True)
        if args.push_hz != 'auto' or args.capacity != CAPACITY_DEFAULT or args.no_low_priority:
            print('Classic 模式：--push-hz/--capacity/--no-low-priority 仅用于内嵌页面模式', flush=True)
        forwarding = _classic_forwarded(args)
        print('Classic 模式：委托 native_scope.py（旧 PyQtGraph 窗口）', flush=True)
        return native_scope_main(forwarding)
    if not 1 <= args.port <= 65535:
        parser.error('--port must be 1..65535')
    capacity = max(CAPACITY_MIN, min(CAPACITY_MAX, args.capacity))
    if capacity != args.capacity:
        print(f'[GUI] --capacity {args.capacity} 已夹到 {capacity}'
              f'（允许 {CAPACITY_MIN}..{CAPACITY_MAX}）', flush=True)
    source_ip = None
    if args.source_ip:
        try:
            source_ip = str(ipaddress.IPv4Address(args.source_ip))
        except ipaddress.AddressValueError:
            parser.error('--source-ip must be a valid IPv4 address')

    from lib.desktop_window import apply_low_priority, run_desktop, webengine_available
    if not webengine_available():
        print('QtWebEngine（PyQt5 内嵌浏览器）缺失，无法打开内嵌 GUI 页面窗口。\n'
              f'安装：{_install_hint()}\n'
              '旧 PyQtGraph 接收窗口可加 --classic。', file=sys.stderr, flush=True)
        return 1

    # The receiver has no child process, so lowering the priority before starting
    # the backend/HTTP threads is safe on both platforms.
    if args.no_low_priority:
        print('[GUI] 低干扰：已禁用（--no-low-priority）', flush=True)
    else:
        print(f'[GUI] 低干扰：{apply_low_priority()}', flush=True)

    from monitor import Application
    udp = dict(host=args.host, port=args.port)
    if source_ip:
        udp['source_ip'] = source_ip
    config_path = None
    if args.config:
        config_path = str(Path(args.config).expanduser().resolve())
    app_kwargs = dict(udp=udp, config=config_path)
    if _accepts_parameter(Application, 'capacity'):
        app_kwargs['capacity'] = capacity
    else:
        print('[GUI] 后端 Application 暂不支持 capacity=，沿用后端默认容量'
              '（待后端 task-6 就绪后生效）', flush=True)
    if args.push_hz != 'auto' and _accepts_parameter(Application, 'push_hz'):
        app_kwargs['push_hz'] = args.push_hz
    try:
        app = Application(**app_kwargs)
    except (OSError, ValueError) as exc:
        print(f'无法监听 UDP {args.host}:{args.port}：{exc}\n'
              '请确认端口未被其他程序占用，且没有另一个 GUI 实例在运行。',
              file=sys.stderr, flush=True)
        return 1
    title = ('Serval · Windows 远程串口解算台' if os.name == 'nt'
             else 'Serval · 远程串口解算台')
    try:
        print(f'Serval GUI · 远程 UDP 接收 {args.host}:{args.port}'
              + (f' · 仅接受来源 {source_ip}' if source_ip else ''), flush=True)
        print(f'历史帧容量 = {capacity} 帧（页面“缓存范围”按实际速率显示可回溯秒数）', flush=True)
        print('窗口内嵌 web/ 页面，数据经 SSE 低延迟推送；'
              '关闭窗口只停止本接收端，不影响 Jetson 视觉程序。', flush=True)
        return run_desktop(app, title=title, push_hz=args.push_hz)
    finally:
        try:
            app.close()
        except Exception:
            pass


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
