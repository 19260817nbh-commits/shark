import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

from lib.live_udp import FPS
from native_watch import main
from .test_serval import wait_until


@unittest.skipUnless(os.name=='posix','Orin/Linux process lifecycle')
class NativeLauncherTests(unittest.TestCase):
    def test_headless_launch_fps_udp_config_preserved_and_shutdown(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            text='com_port: /dev/test\nenemy_color: blue\nauto_fire: false\n'
            (root/'config.yaml').write_text(text)
            binary=root/'build'/'standard_mpc'
            binary.parent.mkdir()
            binary.write_text('#!'+sys.executable+'\n'+
                'import os,time,pathlib\n'+
                'assert os.environ["SP_SERIAL_UDP"].startswith("127.0.0.1:")\n'+
                'assert os.environ["SP_SERIAL_LOG"]=="1"\n'+
                'assert os.environ["SP_LOG_LEVEL"]=="info"\n'+
                'assert "SP_CAMERA_PREVIEW_JPEG" not in os.environ\n'+
                'assert "SP_CAMERA_PREVIEW_FPS" not in os.environ\n'+
                'pathlib.Path("pid").write_text(str(os.getpid()))\n'+
                'while True:\n print("standard_mpc fps: 88.25",flush=True)\n time.sleep(.2)\n')
            binary.chmod(0o755)
            sock=socket.socket(socket.AF_INET,socket.SOCK_DGRAM)
            sock.bind(('127.0.0.1',0));sock.settimeout(5)
            output=root/'launcher.log'
            with output.open('w') as log:
                process=subprocess.Popen([sys.executable,'-B',str(Path(__file__).resolve().parents[1]/'native_watch.py'),
                    '--project',str(root),'--config','config.yaml','--binary','standard_mpc',
                    '--target','127.0.0.1','--port',str(sock.getsockname()[1]),'--record'],
                    stdout=log,stderr=subprocess.STDOUT,start_new_session=True)
                try:
                    data,_=sock.recvfrom(100)
                    self.assertEqual(FPS.unpack(data),(b'SVF1',88.25))
                    pid=int((root/'pid').read_text())
                    sock.close()
                    os.kill(pid,0)  # Receiver disconnect must not terminate direct control.
                    process.send_signal(signal.SIGINT)
                    self.assertEqual(process.wait(timeout=10),0)
                    with self.assertRaises(ProcessLookupError):os.kill(pid,0)
                    self.assertEqual((root/'config.yaml').read_text(),text)
                finally:
                    sock.close()
                    if process.poll() is None:
                        process.send_signal(signal.SIGINT);process.wait(timeout=10)

    def test_debug_alias_shares_preview_path_with_local_scope(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = root / 'config.yaml'
            config.write_text('com_port: /dev/test\n')
            binary = root / 'build' / 'standard_debug_mpc'
            binary.parent.mkdir()
            binary.write_bytes(b'SP_SERIAL_UDP\0SP_CAMERA_PREVIEW_JPEG\0SP_CAMERA_PREVIEW_FPS\0')
            binary.chmod(0o755)
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
                probe.bind(('127.0.0.1', 0))
                port = probe.getsockname()[1]
            camera_paths = []

            def launch(command, **options):
                if '--camera-preview' in command:
                    camera_paths.append(Path(command[command.index('--camera-preview') + 1]))
                    self.assertTrue(camera_paths[0].parent.is_dir())
                    self.assertEqual(Path(command[2]).name, 'jetson_scope.py')
                    self.assertEqual(command[-2:], ['--preview-fps', '120'])
                else:
                    self.assertEqual(command, [str(binary), str(config)])
                    self.assertEqual(options['env']['SP_CAMERA_PREVIEW_JPEG'], str(camera_paths[0]))
                    self.assertEqual(options['env']['SP_CAMERA_PREVIEW_FPS'], '120')
                return Mock(pid=123, returncode=0, poll=Mock(return_value=0))

            with patch.dict(os.environ, {'DISPLAY': ':fixture'}), \
                 patch('native_watch.subprocess.run', return_value=Mock(returncode=0)), \
                 patch('native_watch.subprocess.Popen', side_effect=launch) as processes:
                result = main(['--project', str(root), '--config', 'config.yaml',
                               '--binary', 'standard_debug_mpc', '--port', str(port), '--preview-fps', '120'])
            self.assertEqual(result, 0)
            self.assertEqual(processes.call_count, 2)
            self.assertFalse(camera_paths[0].parent.exists())


if __name__=='__main__':unittest.main()
