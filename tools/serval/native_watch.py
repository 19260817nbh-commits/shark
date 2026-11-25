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
import time

from diagnostics import DiagnosticFollower, DiagnosticStore
from live_udp import FPS
from orin_watch import stop_child
from passive import read_project_config


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', default='configs/balance1.yaml')
    parser.add_argument('--binary', default='build/standard_mpc')
    parser.add_argument('--project', type=Path, default=Path(__file__).resolve().parents[2])
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
    config, binary = (root/args.config).resolve(), (root/args.binary).resolve()
    settings = read_project_config(config)
    if not (settings['com_port'] or '').startswith('/dev/'):
        parser.error('Use the real physical /dev/ serial port in YAML, not the old PTY bridge')
    if not binary.is_file() or not os.access(binary, os.X_OK):
        parser.error('Compile the selected vision binary first')
    with binary.open('rb') as source:
        with mmap.mmap(source.fileno(), 0, access=mmap.ACCESS_READ) as content:
            if content.find(b'SP_SERIAL_UDP')<0:
                parser.error('Binary lacks live UDP support. Rebuild: cmake --build build --target standard_mpc -j2')
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
    sock = socket.socket(socket.AF_INET,socket.SOCK_DGRAM)
    sock.setblocking(False)
    try:
        if not args.target:
            scope = subprocess.Popen([sys.executable,'-B',str(Path(__file__).with_name('native_scope.py')),
                '--host','127.0.0.1','--port',str(args.port)], cwd=root, start_new_session=True)
        env = os.environ.copy()
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
        print(f'Vision exited ({vision.returncode}); see {log}',flush=True)
        return vision.returncode
    except KeyboardInterrupt:
        return 0
    finally:
        stop_child(vision)
        stop_child(scope)
        if follower is not None:
            follower.close()
        sock.close()


if __name__=='__main__':
    signal.signal(signal.SIGTERM,lambda *_: (_ for _ in ()).throw(KeyboardInterrupt()))
    try:
        sys.exit(main())
    except (OSError, ValueError) as exc:
        print(f'ERROR: {exc}',file=sys.stderr)
        sys.exit(1)
