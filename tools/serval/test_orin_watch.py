import json
import os
from pathlib import Path
import re
import signal
import socket
import subprocess
import sys
import tempfile
import unittest
from urllib.request import urlopen

from test_serval import packet, wait_until
from test_packets import log_record


@unittest.skipUnless(os.name == 'posix', 'Orin/Linux process lifecycle')
class LauncherTests(unittest.TestCase):
    def test_direct_logging_monitor_exit_and_launcher_cleanup(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = root/'config.yaml'
            original = 'com_port: /dev/test-device\nenemy_color: blue\nauto_fire: false\n'
            config.write_text(original)
            fixture = log_record('tx', packet('tx')) + log_record('rx', packet('rx'), ns=1000)
            binary = root/'fake_vision.py'
            binary.write_text('#!'+sys.executable+'\n'+
                'import os,pathlib,time\n'+
                'assert os.environ["SP_SERIAL_LOG"] == "1"\n'+
                'assert os.environ["SP_CONTROL_DEBUG"] == "1"\n'+
                'p=pathlib.Path(os.environ["SP_SERIAL_LOG_DIR"])/"capture.bin"\n'+
                f'data={fixture!r}\n'+
                'p.write_bytes(data)\n'+
                'print("[AimTrace] rx_mode=AUTO_AIM rx_color=0 active_color=blue detected=1 red=0 blue=1 kept=1 tracker=tracking targets=1",flush=True)\n'+
                'print("[ControlTrace] target=true target_id=3 control=true planner_fire=false yaw=0.5 pitch=0.1 reason=control_ready",flush=True)\n'+
                'while True:\n'
                ' with p.open("ab") as f: f.write(data)\n'
                ' print("YOLO26TRT timing noisy-output",flush=True)\n'
                ' time.sleep(.03)\n')
            binary.chmod(0o755)
            with socket.socket() as probe:
                probe.bind(('127.0.0.1', 0))
                port = probe.getsockname()[1]
            output = root/'launcher.txt'
            launcher = None
            with output.open('w') as log:
                try:
                    launcher = subprocess.Popen([
                        sys.executable, '-B', str(Path(__file__).with_name('orin_watch.py')),
                        '--project', str(root), '--config', 'config.yaml', '--binary', 'fake_vision.py',
                        '--host', '127.0.0.1', '--http-port', str(port),
                    ], stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
                    wait_until(lambda: 'Vision PID=' in output.read_text(), timeout=8)
                    match = re.search(r'Vision PID=(\d+); monitor PID=(\d+)', output.read_text())
                    self.assertIsNotNone(match)
                    vision_pid, monitor_pid = map(int, match.groups())
                    base = f'http://127.0.0.1:{port}'
                    def has_packets():
                        with urlopen(base+'/api/packets', timeout=2) as response:
                            return len(json.load(response)['latest']) == 2
                    wait_until(has_packets)
                    def has_diagnostics():
                        with urlopen(base+'/api/diagnostics', timeout=2) as response:
                            return 'control' in json.load(response)['latest']
                    wait_until(has_diagnostics)
                    self.assertNotIn('noisy-output', output.read_text())
                    self.assertNotIn('[AimTrace]', output.read_text())
                    self.assertEqual(config.read_text(), original)
                    capture = next((root/'records/serial').glob('watch_*'))/'capture.bin'
                    size = capture.stat().st_size
                    os.kill(monitor_pid, signal.SIGINT)
                    wait_until(lambda: 'Monitor stopped; vision' in output.read_text(), timeout=5)
                    wait_until(lambda: capture.stat().st_size > size)
                    os.kill(vision_pid, 0)
                    launcher.send_signal(signal.SIGINT)
                    self.assertEqual(launcher.wait(timeout=10), 0)
                    with self.assertRaises(ProcessLookupError):
                        os.kill(vision_pid, 0)
                finally:
                    if launcher and launcher.poll() is None:
                        launcher.send_signal(signal.SIGINT)
                        launcher.wait(timeout=10)


if __name__ == '__main__':
    unittest.main()
