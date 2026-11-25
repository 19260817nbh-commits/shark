import json
from pathlib import Path
import tempfile
import threading
from http.server import ThreadingHTTPServer
from urllib.request import urlopen
import unittest
from unittest.mock import patch

from monitor import Application, Store, make_handler
from lib.passive import read_project_config
from lib.recording import HEADER, MAGIC
from .test_serval import packet, wait_until


def log_record(direction, payload, ns=0, seq=17, status=3):
    return HEADER.pack(MAGIC, 0 if direction == 'tx' else 1, status,
                       len(payload), ns, seq, 0) + payload


class PacketTests(unittest.TestCase):
    def test_follow_keeps_bad_crc_raw_but_excludes_bad_waveform(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/'capture.bin'
            bad = packet('rx')[:-2] + b'\0\0'
            data = log_record('rx', bad, status=1) + log_record('tx', packet('tx'), ns=20_000_000, seq=33)
            path.write_bytes(data)
            app = Application()
            try:
                with patch.object(app.bridge, '_open_port', side_effect=AssertionError('serial opened')):
                    app.start_follow(path)
                    wait_until(lambda: app.store.seq == 1)
                    raw = app.packet_snapshot('both', 30)
                    self.assertEqual(len(raw['packets']), 2)
                    rx = raw['latest']['rx']
                    self.assertEqual(rx['hex'], bad.hex(' ').upper())
                    self.assertFalse(rx['crc_ok'])
                    self.assertIsNone(rx['values'])
                    self.assertEqual(rx['log_status'], 1)
                    self.assertEqual(raw['latest']['tx']['log_seq'], 33)
                    self.assertEqual(app.store.latest['tx']['values']['mode'], 1)
                    self.assertNotIn('rx', app.store.latest)
                    self.assertEqual(path.read_bytes(), data)
            finally:
                app.close()

    def test_replay_keeps_log_metadata_and_error_frames(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/'capture.bin'
            bad = packet('rx')[:-2] + b'\0\0'
            path.write_bytes(log_record('tx', packet('tx'), seq=91, status=5) +
                             log_record('rx', bad, ns=5_000_000, seq=92, status=1))
            app = Application()
            try:
                app.start_replay(path)
                wait_until(lambda: len(app.store.packets) == 2)
                snapshot = app.packet_snapshot('rx', 30)
                self.assertEqual(len(snapshot['packets']), 1)
                self.assertEqual(snapshot['latest']['tx']['log_status'], 5)
                self.assertEqual(snapshot['latest']['rx']['log_seq'], 92)
                self.assertEqual(snapshot['mode'], 'replay')
                app.replay_control('restart')
                wait_until(lambda: app.store.packet_seq == 4)
                self.assertEqual(len(app.store.packets), 2)
            finally:
                app.close()

    def test_packet_history_is_bounded_and_keeps_each_direction_latest(self):
        store = Store('test')
        store.add_packet('tx', packet('tx'))
        for _ in range(1010):
            store.add_packet('rx', packet('rx'))
        data = store.packet_snapshot('rx', 4)
        self.assertEqual(data['retained'], 1000)
        self.assertEqual(len(data['packets']), 4)
        self.assertEqual(data['latest']['tx']['length'], 30)
        self.assertEqual(data['latest']['rx']['values']['enemy_color'], 1)
        self.assertEqual(data['latest']['rx']['hex'].split()[41], '01')

    def test_http_packets_and_script_do_not_mutate_control(self):
        app = Application()
        app.record_raw('tx', packet('tx'), app.store.start, True)
        server = ThreadingHTTPServer(('127.0.0.1', 0), make_handler(app))
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base = 'http://127.0.0.1:' + str(server.server_port)
        try:
            with patch.object(app.bridge, '_open_port', side_effect=AssertionError('serial opened')):
                with urlopen(base+'/api/packets?direction=tx&limit=10') as response:
                    data = json.load(response)
                self.assertEqual(data['latest']['tx']['values']['target_id'], 3)
                with urlopen(base+'/packets.js') as response:
                    self.assertIn(b'crc_calculated', response.read())
                self.assertFalse(app.bridge.running)
                self.assertEqual(app.mode, 'idle')
        finally:
            server.shutdown()
            thread.join()
            server.server_close()
            app.close()

    def test_config_reads_color_source_and_fire_without_editing(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/'config.yaml'
            original = 'enemy_color: blue\nenemy_color_source: config\nauto_fire: false\ncom_port: /dev/ttyACM0\n'
            path.write_text(original)
            settings = read_project_config(path)
            self.assertEqual(settings['enemy_color_source'], 'config')
            self.assertIs(settings['auto_fire'], False)
            self.assertEqual(path.read_text(), original)


if __name__ == '__main__':
    unittest.main()
