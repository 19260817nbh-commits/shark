import os
from pathlib import Path
import struct
import tempfile
import time
import unittest
from unittest.mock import patch

from monitor import Application
from lib.passive import read_project_config
from lib.recording import HEADER, MAGIC
from .test_serval import packet, wait_until
from packet_dump import Reader, describe


def record(direction, ns, data=None):
    payload = packet(direction) if data is None else data
    return HEADER.pack(MAGIC, 0 if direction == 'tx' else 1, 3, len(payload), ns, 0, 0)+payload


class PassiveTests(unittest.TestCase):
    def test_packet_dump_retains_partial_records(self):
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'serial.bin'
            data=record('rx',0)
            path.write_bytes(data[:20])
            with path.open('rb') as f:
                reader=Reader(f)
                self.assertEqual(list(reader.records()),[])
                with path.open('ab') as output:
                    output.write(data[20:])
                frames=list(reader.records())
                self.assertEqual(len(frames),1)
                text=describe(frames[0])
                self.assertIn('CRC=OK',text)
                self.assertIn('enemy_color_raw=1',text)
                self.assertIn('byte[41]=0x01',text)
                self.assertNotIn('红',text)

    def test_packet_dump_shows_bad_crc_and_full_hex(self):
        bad=packet('tx')[:-2]+b'\x00\x00'
        text=describe((0,3,1,5,bad))
        self.assertIn('CRC=BAD',text)
        self.assertIn(bad.hex(' ').upper(),text)
        self.assertIn('untrusted',text)

    def test_config_reads_active_scalars_without_editing(self):
        with tempfile.TemporaryDirectory() as folder:
            p=Path(folder)/'balance.yaml'
            data='#enemy_color: "red"\nenemy_color: "blue"\ncom_port: "/dev/ttyACM0" # physical\n'
            p.write_text(data)
            config=read_project_config(p)
            self.assertEqual(config['enemy_color'],'blue')
            self.assertEqual(config['com_port'],'/dev/ttyACM0')
            self.assertEqual(p.read_text(),data)

    def test_config_rejects_duplicate_fields(self):
        with tempfile.TemporaryDirectory() as folder:
            p=Path(folder)/'balance.yaml'
            p.write_text('enemy_color: blue\nenemy_color: red\n')
            with self.assertRaises(ValueError):
                read_project_config(p)

    def test_follow_live_partial_records_and_no_serial_access(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder)
            app=Application()
            try:
                with patch.object(app.bridge,'_open_port',side_effect=AssertionError('opened serial')):
                    app.start_follow(root)
                    p=root/'serial_first.bin'
                    p.write_bytes(b'')
                    wait_until(lambda:app.follower.file_path==p)
                    data=record('rx',0)
                    with p.open('ab',buffering=0) as output:
                        output.write(data[:15])
                        time.sleep(.08)
                        self.assertFalse(app.store.latest)
                        output.write(data[15:])
                        wait_until(lambda:'rx' in app.store.latest)
                        output.write(record('tx',10000000))
                        wait_until(lambda:'tx' in app.store.latest)
                    self.assertEqual(app.snapshot(0)['mode'],'follow')
                    self.assertEqual(app.store.latest['rx']['values']['enemy_color'],1)
                    app.stop()
                    self.assertEqual(app.mode,'idle')
                    self.assertEqual(p.read_bytes(),data+record('tx',10000000))
            finally:
                app.close()

    def test_follow_rotates_and_preserves_log_timing(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder)
            p=root/'first.bin'
            p.write_bytes(record('rx',0)+record('tx',2_000_000_000))
            app=Application()
            try:
                app.start_follow(root)
                wait_until(lambda:app.store.seq==2)
                frames=app.store.snapshot(0)['frames']
                self.assertAlmostEqual(frames[-1]['t']-frames[0]['t'],2)
                self.assertLessEqual(app.store.latest['tx']['t'],app.snapshot(0)['now']+.1)
                q=root/'second.bin'
                q.write_bytes(record('rx',0))
                os.utime(q,(time.time()+.5,time.time()+.5))
                wait_until(lambda:app.follower.file_path==q and app.store.seq==3)
                self.assertEqual(len(app.store.frames),1)
            finally:
                app.close()

    def test_follow_bad_crc_does_not_publish_or_rewrite(self):
        with tempfile.TemporaryDirectory() as folder:
            p=Path(folder)/'serial.bin'
            bad=packet('rx')[:-2]+b'\x00\x00'
            data=record('rx',0,bad)+record('rx',10_000_000)
            p.write_bytes(data)
            app=Application()
            try:
                app.start_follow(p)
                wait_until(lambda:app.store.seq==1)
                self.assertEqual(app.store.errors['crc'],1)
                self.assertEqual(p.read_bytes(),data)
            finally:
                app.close()


if __name__=='__main__':
    unittest.main()
