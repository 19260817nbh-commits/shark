#!/usr/bin/env python3
"""Launch direct-serial vision with a native local scope or a remote UDP observer."""
import argparse
import ipaddress
import mmap
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import tempfile
import time

SERVAL_ROOT = Path(__file__).resolve().parents[1]
if str(SERVAL_ROOT) not in sys.path:
    sys.path.insert(0, str(SERVAL_ROOT))

from lib.diagnostics import DiagnosticFollower, DiagnosticStore
from lib.live_udp import FPS
from orin_watch import stop_child
from lib.passive import read_project_config

LOCAL_BINARIES = ('standard_mpc', 'standard_debug_mpc')


def log_tail(path, lines=40):
    """Last lines of the vision log, printed automatically when vision aborts."""
    try:
        text = Path(path).read_text(encoding='utf-8', errors='replace')
    except OSError:
        return ''
    return '\n'.join(text.splitlines()[-lines:])


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', default='configs/balance1.yaml')
    parser.add_argument('--binary', default='standard_mpc',
                        help='standard_mpc (waveforms) or standard_debug_mpc (camera + waveforms), or a binary path')
    parser.add_argument('--preview-fps', type=int, choices=range(1, 121), metavar='1..120',
                        help='Jetson camera preview rate (default: 60, local standard_debug_mpc only)')
    parser.add_argument('--project', type=Path, default=Path(__file__).resolve().parents[3])
    parser.add_argument('--target', help='Receiver computer IPv4; omitted: open scope locally on Orin')
    parser.add_argument('--port', type=int, default=9871)
    parser.add_argument('--record', action='store_true', help='Also record full raw serial logs on Orin')
    args = parser.parse_args(argv)
    if os.name != 'posix':
        parser.error('Run native_watch.py on Orin/Linux; use native_scope.py on Windows')
    try:
        target = str(ipaddress.IPv4Address(args.target or '127.0.0.1'))
    except ipaddress.AddressValueError:
        parser.error('--target must be the receiver IPv4 address')
    if not 1<=args.port<=65535:
        parser.error('--port must be 1..65535')
    root = args.project.resolve()
    binary_arg = Path('build') / args.binary if args.binary in LOCAL_BINARIES else Path(args.binary)
    config, binary = (root/args.config).resolve(), (root/binary_arg).resolve()
    camera_enabled = not args.target and binary_arg.name == 'standard_debug_mpc'
    if args.preview_fps is not None and not camera_enabled:
        parser.error('--preview-fps requires local --binary standard_debug_mpc')
    preview_fps = args.preview_fps or 60
    settings = read_project_config(config)
    if not (settings['com_port'] or '').startswith('/dev/'):
        parser.error('Use the real physical /dev/ serial port in YAML, not the old PTY bridge')
    if not binary.is_file() or not os.access(binary, os.X_OK):
        parser.error('Compile the selected vision binary first')
    with binary.open('rb') as source:
        with mmap.mmap(source.fileno(), 0, access=mmap.ACCESS_READ) as content:
            if content.find(b'SP_SERIAL_UDP')<0:
                parser.error(f'Binary lacks live UDP support. Rebuild: cmake --build build --target {binary_arg.name} -j2')
            if camera_enabled and content.find(b'SP_CAMERA_PREVIEW_FPS') < 0:
                parser.error('Binary lacks configurable preview support. Rebuild: cmake --build build --target standard_debug_mpc -j2')
    if not args.target and not (os.environ.get('DISPLAY') or os.environ.get('WAYLAND_DISPLAY')):
        parser.error('No local desktop. Use --target <Windows-IP>, or run from an Orin desktop terminal')
    if not args.target:
        dependency = subprocess.run([sys.executable,'-c','import pyqtgraph; from pyqtgraph.Qt import QtWidgets'],
            capture_output=True, text=True)
        if dependency.returncode:
            parser.error('Native GUI dependencies missing. Install python3-pyqt5 and python3-pyqtgraph first.')
        with socket.socket(socket.AF_INET,socket.SOCK_DGRAM) as probe:
            probe.bind(('127.0.0.1',args.port))
    log_dir = root/'records/serial'/('native_'+time.strftime('%Y%m%d_%H%M%S')+'_'+str(os.getpid()))
    log_dir.mkdir(parents=True, exist_ok=False)
    log = log_dir/'vision.log'
    scope = vision = follower = None
    preview_dir = None
    sock = socket.socket(socket.AF_INET,socket.SOCK_DGRAM)
    sock.setblocking(False)
    try:
        env = os.environ.copy()
        env.pop('SP_CAMERA_PREVIEW_JPEG', None)
        env.pop('SP_CAMERA_PREVIEW_FPS', None)
        if not args.target:
            command = [sys.executable, '-B', str(Path(__file__).with_name('jetson_scope.py')),
                       '--host', '127.0.0.1', '--port', str(args.port)]
            if camera_enabled:
                shared_memory = '/dev/shm' if os.access('/dev/shm', os.W_OK | os.X_OK) else None
                preview_dir = tempfile.TemporaryDirectory(prefix='serval-camera-', dir=shared_memory)
                preview_path = str(Path(preview_dir.name) / 'latest.jpg')
                command.extend(['--camera-preview', preview_path, '--preview-fps', str(preview_fps)])
                env['SP_CAMERA_PREVIEW_JPEG'] = preview_path
                env['SP_CAMERA_PREVIEW_FPS'] = str(preview_fps)
            scope = subprocess.Popen(command, cwd=root, start_new_session=True)
        env.update(SP_SERIAL_UDP=f'{target}:{args.port}',SP_SERIAL_LOG='1' if args.record else '0',
                   SP_SERIAL_LOG_DIR=str(log_dir),SP_LOG_LEVEL='info',SP_CONTROL_DEBUG='0')
        with log.open('xb') as output:
            vision = subprocess.Popen([str(binary),str(config)],cwd=root,env=env,
                stdout=output,stderr=subprocess.STDOUT,start_new_session=True)
        store = DiagnosticStore()
        follower = DiagnosticFollower(log,store)
        print(f'Vision PID={vision.pid}; UDP observer={target}:{args.port}; auto_fire={settings["auto_fire"]}',flush=True)
        print(f'Log: {log}\nDirect serial; no browser/SSH dependency. Ctrl+C stops this launcher and its vision.',flush=True)
        last_fps = None
        scope_reported = False
        while vision.poll() is None:
            if scope is not None and scope.poll() is not None and not scope_reported:
                print('Native window closed; vision continues. Ctrl+C here stops vision.',flush=True)
                scope_reported = True
            with store.lock:
                item = store.latest.get('fps')
                if item and item['observed']!=last_fps and time.monotonic()-item['observed']<=3:
                    try:
                        sock.sendto(FPS.pack(b'SVF1',item['fields']['fps']),(target,args.port))
                    except OSError:
                        pass
                    last_fps = item['observed']
            time.sleep(.05)
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
        stop_child(scope)
        if follower is not None:
            follower.close()
        sock.close()
        if preview_dir is not None:
            preview_dir.cleanup()


if __name__=='__main__':
    signal.signal(signal.SIGTERM,lambda *_: (_ for _ in ()).throw(KeyboardInterrupt()))
    try:
        sys.exit(main())
    except (OSError, ValueError) as exc:
        print(f'ERROR: {exc}',file=sys.stderr)
        sys.exit(1)
