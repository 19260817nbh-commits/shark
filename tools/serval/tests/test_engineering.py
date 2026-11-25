import importlib.util
import math
import os
from pathlib import Path
import struct
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from monitor import Application, Store
from lib.protocol import ProtocolError, StreamDecoder, decode_frame, crc16
from lib.recording import HEADER, MAGIC, Recorder, Replay
from lib.serial_io import SerialBridge
from .test_serval import MemoryPort, packet, wait_until


class EngineeringTests(unittest.TestCase):
    def test_independent_wire_offsets(self):
        raw = bytearray(30)
        raw[:2], raw[2], raw[27] = b'SP', 2, 7
        for offset, value in zip((3,7,11,15,19,23),(1.,2.,3.,4.,5.,6.)):
            struct.pack_into('<f',raw,offset,value)
        struct.pack_into('<H',raw,28,crc16(raw[:28]))
        direction, data = decode_frame(raw)
        self.assertEqual(direction,'tx')
        self.assertEqual(data['target_id'],7)
        self.assertEqual(data['pitch_acc'],6)

    def test_old_layout_rejected_with_explanation(self):
        with self.assertRaisesRegex(ProtocolError,'30'):
            decode_frame(packet('tx')[:-1])

    def test_timestamp_and_raw_capture_survive_partial_reads(self):
        frames, raw = [], []
        parser = StreamDecoder('rx',lambda *args: frames.append(args),lambda _:None,
                               lambda *args: raw.append(args))
        data = packet('rx')
        parser.feed(data[:12],100)
        parser.feed(data[12:],101)
        self.assertEqual(frames[0][2],101)
        self.assertEqual(raw[0],('rx',data,101,True))

    def test_recording_readable_by_original_decoder(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder)/'test.bin'
            recorder = Recorder(path)
            stamp = recorder.start
            recorder.add('tx',packet('tx'),stamp,True)
            recorder.add('rx',packet('rx'),stamp+.01,True)
            bad = packet('rx')[:-2]+b'\x00\x00'
            recorder.add('rx',bad,stamp+.02,False)
            recorder.close()
            legacy_path = Path(__file__).resolve().parents[1]/'save'/'decode_serial_log.py'
            spec = importlib.util.spec_from_file_location('legacy_decoder',legacy_path)
            legacy = importlib.util.module_from_spec(spec)
            previous = sys.dont_write_bytecode
            try:
                sys.dont_write_bytecode = True
                spec.loader.exec_module(legacy)
            finally:
                sys.dont_write_bytecode = previous
            tx, rx, info = legacy.decode(str(path))
            self.assertEqual((len(tx),len(rx)),(1,2))
            self.assertEqual(tx[0]['target_id'],3)
            self.assertEqual(rx[0]['enemy_color'],1)
            self.assertEqual(tx[0]['yaw_rad'], decode_frame(packet('tx'))[1]['yaw'])
            self.assertEqual(rx[1]['valid'], 0)
            self.assertEqual(rx[1]['reject_reason'], 'crc')
            self.assertNotIn('yaw_deg', rx[1])
            self.assertEqual(rx[1]['raw'], bad.hex())
            self.assertEqual(info['crc_bad'],1)
            self.assertEqual(recorder.written,3)

    def test_replay_pause_speed_restart_and_truncated_tail(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder)/'test.bin'
            with path.open('wb') as f:
                for i in range(3):
                    p = packet('rx')
                    f.write(HEADER.pack(MAGIC,1,3,len(p),i*40000000,i,0)+p)
                f.write(b'RM')
            frames=[]
            replay = Replay(path,lambda *x:frames.append(x),lambda _:None,100)
            try:
                self.assertEqual(replay.truncated_bytes,2)
                replay.control('speed',4)
                replay.control('play')
                wait_until(lambda:len(frames)==3)
                self.assertAlmostEqual(frames[-1][2],100.08)
                replay.control('pause')
                self.assertFalse(replay.snapshot()['playing'])
                replay.control('restart')
                wait_until(lambda:len(frames)==6)
                with self.assertRaises(ValueError):
                    replay.control('speed',math.nan)
            finally:
                replay.close()

    def test_replay_never_opens_serial(self):
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'test.bin'
            p=packet('tx')
            path.write_bytes(HEADER.pack(MAGIC,0,3,len(p),0,0,0)+p)
            app=Application()
            try:
                with patch.object(app.bridge,'_open_port',side_effect=AssertionError('Opened serial')):
                    app.start_replay(path)
                    wait_until(lambda:app.store.seq==1)
                    self.assertEqual(app.snapshot(0)['mode'],'replay')
                    app.replay_control('restart')
                    wait_until(lambda:app.store.seq==2)
            finally:
                app.close()

    def test_1000_frame_burst_is_forwarded_and_decoded(self):
        physical,virtual=MemoryPort(),MemoryPort()
        frames=[]
        bridge=SerialBridge(lambda *x:frames.append(x),lambda _:None)
        with patch.object(bridge,'_open_port',side_effect=[physical,virtual]):
            bridge.start('physical',virtual='virtual')
        try:
            data=packet('rx')*1000
            physical.feed(data)
            wait_until(lambda:len(virtual.output)==len(data),timeout=5)
            wait_until(lambda:len(frames)==1000,timeout=5)
            self.assertEqual(bytes(virtual.output),data)
            self.assertEqual(bridge.preview_drops,0)
        finally:
            bridge.stop()

    def test_zero_write_stops_without_automatic_retry(self):
        physical,virtual=MemoryPort(),MemoryPort()
        bridge=SerialBridge(lambda *_:None,lambda _:None)
        with patch.object(bridge,'_open_port',side_effect=[physical,virtual]):
            bridge.start('physical',virtual='virtual')
        try:
            with patch.object(physical,'write',return_value=0) as write:
                virtual.feed(packet('tx'))
                wait_until(lambda:not bridge.running)
                self.assertEqual(write.call_count,1)
                self.assertIn('0',bridge.error)
        finally:
            bridge.stop()

    def test_invalid_virtual_port_closes_physical_port(self):
        physical=MemoryPort()
        bridge=SerialBridge(lambda *_:None,lambda _:None)
        with patch.object(bridge,'_open_port',side_effect=[physical,OSError('busy')]):
            with self.assertRaises(OSError):
                bridge.start('physical',virtual='virtual')
        self.assertTrue(physical.closed)
        self.assertFalse(bridge.running)

    @unittest.skipIf(os.name=='nt','Linux symlink integration')
    def test_fixed_link_reusable_and_foreign_path_protected(self):
        with tempfile.TemporaryDirectory() as folder:
            link=Path(folder)/'gimbal'
            link.write_text('owned by someone else')
            bridge=SerialBridge(lambda *_:None,lambda _:None)
            with patch.object(bridge,'_open_port') as open_port:
                with self.assertRaises(ValueError):
                    bridge.start('physical',link=str(link))
                open_port.assert_not_called()
            self.assertEqual(link.read_text(),'owned by someone else')
            link.unlink()
            for _ in range(2):
                with patch.object(bridge,'_open_port',return_value=MemoryPort()):
                    bridge.start('physical',link=str(link))
                self.assertTrue(link.is_symlink())
                bridge.stop()
                self.assertFalse(link.exists())

    def test_stale_feedback_does_not_make_fresh_error(self):
        store=Store('test')
        store.add('tx',{'mode':1,'yaw':.2,'pitch':0},store.start)
        store.add('rx',{'mode':1,'yaw':.1,'pitch':0},store.start+.01)
        self.assertAlmostEqual(store.snapshot(0,now=.02)['tracking']['yaw'],.1)
        self.assertIsNone(store.snapshot(0,now=2)['tracking'])


if __name__=='__main__':
    unittest.main()
