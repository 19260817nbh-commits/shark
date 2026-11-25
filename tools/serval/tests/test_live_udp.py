from contextlib import ExitStack
from pathlib import Path
import socket
import tempfile
import time
import unittest

from lib.live_udp import FPS, PREFIX, LiveStore, Receiver, unpack_datagram
from lib.recording import HEADER, MAGIC
from .test_serval import packet, wait_until


def datagram(direction=0, seq=0, ns=1000000, session=42, status=3):
    payload=packet('tx' if direction==0 else 'rx')
    return PREFIX.pack(b'SVL1',session)+HEADER.pack(MAGIC,direction,status,len(payload),ns,seq,0)+payload


class LiveTests(unittest.TestCase):
    def test_exact_payload_and_time_sequence_gaps_reorder_wrap(self):
        store=LiveStore()
        data=datagram(seq=0xfffffffe)
        self.assertEqual(unpack_datagram(data)[1][0][4],packet('tx'))
        store.ingest(data,now=10)
        store.ingest(datagram(seq=0,ns=3000000),now=10.002)
        store.ingest(datagram(seq=0xffffffff,ns=2000000),now=10.003)
        state=store.snapshot(now=10.004)
        self.assertEqual(state['missing'],1)
        self.assertEqual(state['late'],1)
        self.assertTrue(state['series'][0][-1]['gap'])
        self.assertEqual(state['series'][0][-1]['t'],.003)
        self.assertAlmostEqual(state['age'],.002)

    def test_session_restart_ignores_retired_packets_and_clears_fps(self):
        store=LiveStore()
        store.ingest(datagram())
        store.ingest(FPS.pack(b'SVF1',99.2))
        self.assertEqual(store.snapshot()['fps'],99.2)
        store.ingest(datagram(session=43))
        store.ingest(datagram(session=42))
        self.assertEqual(store.session,43)
        self.assertEqual(len(store.raw),1)
        self.assertIsNone(store.snapshot()['fps'])

    def test_bad_crc_and_status_preserved_without_plotting(self):
        store=LiveStore()
        raw=bytearray(datagram())
        raw[-1]^=1
        store.ingest(raw)
        store.ingest(datagram(seq=1,status=7))
        self.assertEqual(store.snapshot()['bad'],2)
        self.assertEqual(len(store.raw),2)
        self.assertEqual(store.snapshot()['series'][0],[])

    def test_invalid_datagram_rejected_atomically_and_fps_expires(self):
        store=LiveStore()
        for bad in [b'',datagram()[:-1],datagram()+b'bad',PREFIX.pack(b'xxxx',42),FPS.pack(b'SVF1',float('nan'))]:
            with self.assertRaises(ValueError):store.ingest(bad)
        self.assertIsNone(store.session)
        store.ingest(FPS.pack(b'SVF1',0),now=10)
        self.assertEqual(store.snapshot(now=12)['fps'],0)
        self.assertIsNone(store.snapshot(now=14)['fps'])

    def test_bad_samples_remain_inspectable_and_break_waveform(self):
        store = LiveStore()
        timestamp = 9007199254740993
        store.ingest(datagram(seq=4, ns=timestamp))
        bad = datagram(seq=5, ns=timestamp + 1, status=7)
        store.ingest(bad)
        store.ingest(datagram(seq=6, ns=timestamp + 2))
        store.ingest(datagram(direction=1))
        self.assertIn(bad[PREFIX.size:], store.recent_packets(direction=0))
        self.assertEqual(len(store.recent_packets(direction=1)), 1)
        self.assertTrue(store.snapshot()['series'][0][-1]['gap'])
        self.assertEqual(store.snapshot(include_series=False)['series'], {0: [], 1: []})

    def test_recording_keeps_original_bytes_and_stops_on_session_change(self):
        store = LiveStore()
        store.ingest(datagram(seq=0))
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'run.bin'
            writer = store.start_recording(path)
            original = datagram(seq=1, ns=9007199254740993)
            rejected = datagram(direction=1, status=7)
            store.ingest(original)
            store.ingest(rejected)
            store.ingest(datagram(session=43))
            self.assertFalse(writer.snapshot()['active'])
            store.ingest(datagram(session=43, seq=1))
            writer.close()
            self.assertEqual(path.read_bytes(), original[PREFIX.size:] + rejected[PREFIX.size:])
            self.assertEqual(writer.snapshot()['written'], 2)

    def test_real_udp_receiver_bounded_cache_and_exact_export(self):
        with ExitStack() as stack, tempfile.TemporaryDirectory() as directory:
            store=LiveStore(capacity=2)
            receiver=Receiver(store,'127.0.0.1',0,'127.0.0.1')
            stack.callback(receiver.close)
            sender=stack.enter_context(socket.socket(socket.AF_INET,socket.SOCK_DGRAM))
            frames=[datagram(seq=i,ns=i*1000000) for i in range(3)]
            for frame in frames:sender.sendto(frame,('127.0.0.1',receiver.port))
            wait_until(lambda:store.last_seq.get(0)==2)
            self.assertEqual(len(store.raw),2)
            path=Path(directory)/'test.bin'
            self.assertEqual(store.save(path),2)
            self.assertEqual(path.read_bytes(),b''.join(frame[PREFIX.size:] for frame in frames[1:]))
            with self.assertRaises(FileExistsError):store.save(path)


if __name__=='__main__':unittest.main()
