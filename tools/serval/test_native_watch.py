import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import tempfile
import unittest

from live_udp import FPS
from test_serval import wait_until


@unittest.skipUnless(os.name=='posix','Orin/Linux process lifecycle')
class NativeLauncherTests(unittest.TestCase):
    def test_headless_launch_fps_udp_config_preserved_and_shutdown(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            text='com_port: /dev/test\nenemy_color: blue\nauto_fire: false\n'
            (root/'config.yaml').write_text(text)
            binary=root/'fake.py'
            binary.write_text('#!'+sys.executable+'\n'+
                'import os,time,pathlib\n'+
                'assert os.environ["SP_SERIAL_UDP"].startswith("127.0.0.1:")\n'+
                'assert os.environ["SP_SERIAL_LOG"]=="1"\n'+
                'assert os.environ["SP_LOG_LEVEL"]=="info"\n'+
                'pathlib.Path("pid").write_text(str(os.getpid()))\n'+
                'while True:\n print("standard_mpc fps: 88.25",flush=True)\n time.sleep(.2)\n')
            binary.chmod(0o755)
            sock=socket.socket(socket.AF_INET,socket.SOCK_DGRAM)
            sock.bind(('127.0.0.1',0));sock.settimeout(5)
            output=root/'launcher.log'
            with output.open('w') as log:
                process=subprocess.Popen([sys.executable,'-B',str(Path(__file__).with_name('native_watch.py')),
                    '--project',str(root),'--config','config.yaml','--binary','fake.py',
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


if __name__=='__main__':unittest.main()
