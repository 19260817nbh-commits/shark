#!/usr/bin/env python3
"""Run existing auto-aim with raw serial logging and a read-only web monitor.

No YAML edits, serial bridge, camera replacement, or command generation.
Captures verbose vision output to vision.log and enables low-rate control diagnostics.
--attach only starts the monitor for a vision program already recording logs.
"""
import argparse
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import time
from urllib.error import URLError
from urllib.request import urlopen

from passive import read_project_config


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


def wait_for_monitor(process, port):
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError('Monitor failed to start. Check its output and HTTP port.')
        try:
            with urlopen(f'http://127.0.0.1:{port}/api/packets', timeout=.5) as response:
                if response.status == 200:
                    return
        except (URLError, OSError):
            pass
        time.sleep(.1)
    raise RuntimeError('Monitor did not become ready within 10 seconds.')


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', required=True, help='The YAML actually used by auto-aim')
    parser.add_argument('--binary', default='build/standard_debug_mpc', help='Existing compiled auto-aim executable')
    parser.add_argument('--project', type=Path, default=Path(__file__).resolve().parents[2])
    parser.add_argument('--host', default='0.0.0.0', help='Web listen address; default permits LAN access')
    parser.add_argument('--http-port', type=int, default=8765)
    parser.add_argument('--attach', action='store_true', help='Only monitor an existing program that is already writing RMSE logs')
    parser.add_argument('--log-dir', type=Path, help='Attach: existing log directory; launch: dedicated new recording directory')
    parser.add_argument('--diagnostics', type=Path, help='Attach: existing auto-aim text log; default LOG_DIR/vision.log')
    args = parser.parse_args(argv)
    if os.name != 'posix':
        parser.error('Run this launcher on Orin/Linux. Windows can open the web page or replay logs.')
    if not 1 <= args.http_port <= 65535:
        parser.error('--http-port must be 1..65535')
    root = args.project.resolve()
    config = (root / args.config).resolve()
    settings = read_project_config(config)
    binary = (root / args.binary).resolve()
    if not args.attach:
        port = settings['com_port'] or ''
        if not port.startswith('/dev/'):
            parser.error('Direct mode requires com_port: "/dev/ttyACM0" (or your actual /dev device). Stop the old bridge first. Use --attach only to inspect an existing log.')
        if not binary.is_file() or not os.access(binary, os.X_OK):
            parser.error('Compiled executable is missing or not executable: ' + str(binary))
    if args.log_dir:
        log_dir = (root / args.log_dir).resolve()
    elif args.attach:
        log_dir = root / 'records/serial'
    else:
        log_dir = root / 'records/serial' / ('watch_' + time.strftime('%Y%m%d_%H%M%S') + '_' + str(os.getpid()))
    if not args.attach:
        if log_dir.exists() and any(log_dir.iterdir()):
            parser.error('Choose an empty --log-dir to avoid displaying records from another run.')
        log_dir.mkdir(parents=True, exist_ok=True)
    diagnostic_path = (root / args.diagnostics).resolve() if args.diagnostics else log_dir / 'vision.log'
    if not args.attach and args.diagnostics:
        parser.error('--diagnostics is only for --attach; a launched vision gets its own vision.log')
    # Detect an occupied port before starting either child.
    with socket.socket() as probe:
        probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        probe.bind((args.host, args.http_port))
    monitor = vision = None
    try:
        monitor = subprocess.Popen([
            sys.executable, '-B', str(Path(__file__).with_name('monitor.py')),
            '--follow', str(log_dir), '--config', str(config),
            '--diagnostics', str(diagnostic_path),
            '--host', args.host, '--http-port', str(args.http_port),
        ], cwd=root, start_new_session=True)
        wait_for_monitor(monitor, args.http_port)
        print(f'Web: http://<Orin-IP>:{args.http_port} (local: http://127.0.0.1:{args.http_port})', flush=True)
        print('Raw logs: ' + str(log_dir), flush=True)
        print('Vision/diagnostic log: ' + str(diagnostic_path), flush=True)
        if args.attach:
            print('Attach only: no vision process or serial port is opened. Existing logs must be growing.', flush=True)
            return monitor.wait()
        env = os.environ.copy()
        env.update(SP_SERIAL_LOG='1', SP_SERIAL_LOG_DIR=str(log_dir), SP_CONTROL_DEBUG='1')
        with diagnostic_path.open('xb') as vision_output:
            vision = subprocess.Popen([str(binary), str(config)], cwd=root, env=env,
                                      stdout=vision_output, stderr=subprocess.STDOUT, start_new_session=True)
        print(f'Vision PID={vision.pid}; monitor PID={monitor.pid}; YAML auto_fire={settings["auto_fire"]}', flush=True)
        print('YAML/fire settings are passed through unchanged. Closing the page does not stop vision.', flush=True)
        print('High-frequency vision output goes to vision.log. View control diagnostics on the web page.', flush=True)
        print('Ctrl+C in this launcher stops the vision and monitor that it started.', flush=True)
        monitor_reported = False
        while vision.poll() is None:
            if monitor.poll() is not None and not monitor_reported:
                print('Monitor stopped; vision and direct serial communication continue.', flush=True)
                monitor_reported = True
            time.sleep(.2)
        if vision.returncode:
            print('Vision exited with code '+str(vision.returncode)+'; details: '+str(diagnostic_path), flush=True)
        return vision.returncode
    except KeyboardInterrupt:
        return 0
    finally:
        stop_child(vision)
        stop_child(monitor)


if __name__ == '__main__':
    def terminate(_signum, _frame):
        raise KeyboardInterrupt
    signal.signal(signal.SIGTERM, terminate)
    try:
        sys.exit(main())
    except (OSError, ValueError, RuntimeError) as exc:
        print('ERROR: ' + str(exc), file=sys.stderr)
        sys.exit(1)
