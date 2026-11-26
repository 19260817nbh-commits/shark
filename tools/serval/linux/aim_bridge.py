#!/usr/bin/env python3
"""Jetson/Linux low-latency entry: launch auto-aim and broadcast raw binary UDP to Windows.

Design (Orin NX compute is limited):

* Linux does **no** protocol decode, no CRC check, no plotting, no HTTP/SSE server.
  It only starts the existing vision binary; the C++ SerialRecorder hook (unchanged)
  copies raw serial TX/RX frames and broadcasts binary ``SVL1`` datagrams to Windows.
* Windows receives the binary UDP stream and performs *all* decoding, timestamp
  alignment, plotting, recording and offline decoding.
* The aim/control path is untouched: this launcher adds no serial writes and no
  control commands; it only sets the observation environment variables.

Usage::

    # Windows receiver must be started first:
    #   py -3 tools/serval/windows/windows_gui.py --port 9871
    python3 tools/serval/linux/aim_bridge.py --config configs/balance1.yaml \\
        --binary standard_mpc --target 192.168.1.100 --port 9871

    python3 tools/serval/linux/aim_bridge.py --config configs/balance1.yaml \\
        --binary standard_debug_mpc --target 192.168.1.100 --record

Ctrl+C stops the vision process started by this launcher (equivalent to the old
``native_watch.py`` Ctrl+C).  For a local, on-Jetson window use ``jetson_gui.py``
(optional, costs CPU/GPU) or ``native_watch.py`` (classic PyQtGraph).
"""
import argparse
import ipaddress
import mmap
import os
from pathlib import Path
import signal
import subprocess
import sys
import time

SERVAL_ROOT = Path(__file__).resolve().parents[1]
if str(SERVAL_ROOT) not in sys.path:
    sys.path.insert(0, str(SERVAL_ROOT))

LOCAL_BINARIES = ('standard_mpc', 'standard_debug_mpc')


def log_tail(path, lines=40):
    """Last lines of vision.log, printed when the vision process aborts."""
    try:
        text = Path(path).read_text(encoding='utf-8', errors='replace')
    except OSError:
        return ''
    return '\n'.join(text.splitlines()[-lines:])


def stop_child(process):
    if process is None or process.poll() is not None:
        return
    process.send_signal(signal.SIGINT)
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.terminate()
        try:
            process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()


def main(argv=None):
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    parser.add_argument('--config', default='configs/balance1.yaml',
                        help='The YAML actually used by auto-aim (read only)')
    parser.add_argument('--binary', default='standard_mpc',
                        help='standard_mpc / standard_debug_mpc, or a binary path')
    parser.add_argument('--project', type=Path, default=Path(__file__).resolve().parents[3],
                        help='Project root that contains configs/ and build/')
    parser.add_argument('--target', required=True,
                        help='Windows receiver IPv4 address (binary UDP destination)')
    parser.add_argument('--port', type=int, default=9871,
                        help='UDP port listened by the Windows receiver')
    parser.add_argument('--record', action='store_true',
                        help='Also write the complete raw serial log on Jetson (.bin)')
    args = parser.parse_args(argv)

    if os.name != 'posix':
        parser.error('Run aim_bridge.py on Orin/Linux; use windows/windows_gui.py on Windows')
    try:
        target = str(ipaddress.IPv4Address(args.target))
    except ipaddress.AddressValueError:
        parser.error('--target must be the Windows receiver IPv4 address')
    if not 1 <= args.port <= 65535:
        parser.error('--port must be 1..65535')

    from lib.passive import read_project_config
    root = args.project.resolve()
    binary_arg = Path('build') / args.binary if args.binary in LOCAL_BINARIES else Path(args.binary)
    config = (root / args.config).resolve()
    binary = (root / binary_arg).resolve()
    settings = read_project_config(config)
    port = settings['com_port'] or ''
    if not port.startswith('/dev/'):
        parser.error('Use the real physical /dev/ serial port in YAML, not the old PTY bridge')
    if not binary.is_file() or not os.access(binary, os.X_OK):
        parser.error('Compile the selected vision binary first: ' + str(binary))
    with binary.open('rb') as source:
        with mmap.mmap(source.fileno(), 0, access=mmap.ACCESS_READ) as content:
            if content.find(b'SP_SERIAL_UDP') < 0:
                parser.error('Binary lacks live UDP support. Rebuild: cmake --build build '
                             f'--target {binary_arg.name} -j2')

    log_dir = root / 'records' / 'serial' / (
        'aim_' + time.strftime('%Y%m%d_%H%M%S') + '_' + str(os.getpid()))
    log_dir.mkdir(parents=True, exist_ok=False)
    log = log_dir / 'vision.log'
    vision = None
    try:
        env = os.environ.copy()
        for name in ('SP_CAMERA_PREVIEW_JPEG', 'SP_CAMERA_PREVIEW_FPS'):
            env.pop(name, None)
        env.update(SP_SERIAL_UDP=f'{target}:{args.port}',
                   SP_SERIAL_LOG='1' if args.record else '0',
                   SP_SERIAL_LOG_DIR=str(log_dir),
                   SP_LOG_LEVEL='info',
                   SP_CONTROL_DEBUG='0')
        with log.open('xb') as output:
            vision = subprocess.Popen([str(binary), str(config)], cwd=str(root), env=env,
                                      stdout=output, stderr=subprocess.STDOUT,
                                      start_new_session=True)
        print(f'Vision PID={vision.pid}; binary UDP -> {target}:{args.port}', flush=True)
        print(f'Log: {log}', flush=True)
        print('Linux side: no decode/GUI/HTTP; raw serial frames are copied by the unchanged '
              'C++ recorder and broadcast as binary UDP.', flush=True)
        print(f'auto_fire={settings["auto_fire"]}; Serval adds no serial writes or control '
              f'commands. Ctrl+C stops this vision process.', flush=True)
        while vision.poll() is None:
            time.sleep(.2)
        code = vision.returncode
        if code:
            print(f'Vision exited ({code}). Last lines of {log}:', flush=True)
            tail = log_tail(log)
            print(tail if tail.strip() else '(vision.log is empty)', flush=True)
            print(f'Full log: {log}', flush=True)
        else:
            print(f'Vision exited (0); see {log}', flush=True)
        return code
    except KeyboardInterrupt:
        return 0
    finally:
        stop_child(vision)


if __name__ == '__main__':
    def _terminate(_signum, _frame):
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, _terminate)
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(0)
    except (OSError, ValueError) as exc:
        print(f'ERROR: {exc}', file=sys.stderr)
        sys.exit(1)
