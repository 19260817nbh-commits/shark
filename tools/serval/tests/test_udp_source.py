"""UdpSource: exact sender time, dedup/session handling, raw save and recording."""
import socket
import tempfile
from pathlib import Path

import unittest

from lib.live_udp import FPS, PREFIX
from lib.recording import HEADER, MAGIC, Recorder
from lib.udp_source import UdpSource
from monitor import Store
from save.decode_serial_log import decode
from .test_serval import packet, wait_until


def datagram(direction=0, seq=0, ns=1000000, session=42, status=3, payload=None):
    """Build an SVL1 datagram with a real 30/44-byte SP frame (valid CRC)."""
    if payload is None:
        payload = packet('tx' if direction == 0 else 'rx')
    return (PREFIX.pack(b'SVL1', session) +
            HEADER.pack(MAGIC, direction, status, len(payload), ns, seq, 0) + payload)


class UdpSourceTests(unittest.TestCase):
    def setUp(self):
        self.store = Store('test')
        self.source = UdpSource(self.store, '127.0.0.1', 0, '127.0.0.1')
        self.addCleanup(self.source.close)
        self.sender = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.addCleanup(self.sender.close)
        self.address = ('127.0.0.1', self.source.port)

    def send(self, data):
        self.sender.sendto(data, self.address)

    def test_two_directions_keep_exact_sender_time_and_log_metadata(self):
        tx_ns, rx_ns = 1000000, 2500000123
        self.send(datagram(0, seq=7, ns=tx_ns))
        self.send(datagram(1, seq=8, ns=rx_ns))
        wait_until(lambda: len(self.store.frames) == 2)

        tx, rx = self.store.latest['tx'], self.store.latest['rx']
        # High precision: bit-exact sender time, not merely almost equal.
        self.assertEqual(tx['t'], tx_ns * 1e-9)
        self.assertEqual(rx['t'], rx_ns * 1e-9)
        self.assertAlmostEqual(tx['values']['yaw'], .3)
        self.assertAlmostEqual(rx['values']['yaw'], .25)
        self.assertEqual(tx['log_seq'], 7)
        self.assertEqual(tx['log_time_ns'], '1000000')
        self.assertEqual(rx['log_seq'], 8)
        self.assertEqual(rx['log_time_ns'], str(rx_ns))

        state = self.source.snapshot()
        self.assertEqual(state['session'], 42)
        self.assertEqual(state['peer'], '127.0.0.1')
        self.assertEqual(state['records'], 2)
        self.assertEqual(state['missing'], 0)
        self.assertEqual(state['late'], 0)
        self.assertTrue(state['running'])
        self.assertIsNotNone(state['age'])
        self.assertEqual(self.store.rejected, 0)

    def test_bad_crc_and_record_status_are_rejected_but_kept_raw(self):
        good = packet('rx')
        corrupt = good[:-1] + bytes([good[-1] ^ 0xff])
        good_frame = datagram(1, seq=0, ns=1000000, payload=good)
        bad_crc = datagram(1, seq=1, ns=2000000, payload=corrupt)
        bad_status = datagram(1, seq=2, ns=3000000, status=7)
        for frame in (good_frame, bad_crc, bad_status):
            self.send(frame)
        wait_until(lambda: self.store.rejected == 2)

        self.assertEqual(len(self.store.frames), 1)  # rejected records never plot
        self.assertEqual(self.store.errors.get('crc'), 1)
        self.assertEqual(self.store.errors.get('record_status'), 1)
        by_seq = {p['log_seq']: p for p in self.store.packet_snapshot('rx', 10)['packets']}
        self.assertFalse(by_seq[1]['crc_ok'])
        self.assertIsNone(by_seq[1]['values'])
        self.assertTrue(by_seq[2]['crc_ok'])
        self.assertEqual(by_seq[2]['log_status'], 7)
        state = self.source.snapshot()
        self.assertEqual(state['records'], 3)
        self.assertEqual(state['bad'], 2)

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'capture.bin'
            self.assertEqual(self.source.save(path), 3)
            self.assertEqual(path.read_bytes(), b''.join(
                frame[PREFIX.size:] for frame in (good_frame, bad_crc, bad_status)))

    def test_missing_late_duplicates_and_session_switch(self):
        self.send(datagram(0, seq=0, ns=1_000_000))
        wait_until(lambda: self.source.snapshot()['records'] == 1)
        self.send(datagram(0, seq=2, ns=2_000_000))
        wait_until(lambda: self.source.snapshot()['missing'] == 1)
        self.send(datagram(0, seq=1, ns=1_500_000))   # late: below last accepted seq
        self.send(datagram(0, seq=2, ns=2_000_000))   # duplicate of last accepted seq
        wait_until(lambda: self.source.snapshot()['late'] == 2)
        self.assertEqual(len(self.store.frames), 2)
        self.assertEqual(self.source.snapshot()['seq']['tx'], 2)

        # A new sender session forgets sequence state and resets the store chart.
        self.send(datagram(0, seq=5, ns=500_000, session=43))
        wait_until(lambda: self.store.epoch == 1)
        wait_until(lambda: len(self.store.frames) == 1)
        state = self.source.snapshot()
        self.assertEqual(state['session'], 43)
        self.assertEqual(state['missing'], 0)
        self.assertEqual(state['late'], 0)
        self.assertEqual(state['seq']['tx'], 5)
        self.assertTrue(self.store.source.startswith('UDP 实时 · 127.0.0.1'))

        # A late datagram from the retired session only counts as late.
        self.send(datagram(0, seq=3, ns=3_000_000, session=42))
        wait_until(lambda: self.source.snapshot()['late'] == 1)
        self.assertEqual(len(self.store.frames), 1)
        self.assertEqual(self.source.snapshot()['records'], 3)

    def test_fps_datagram_and_invalid_datagrams_do_not_kill_receiver(self):
        self.send(FPS.pack(b'SVF1', 59.5))
        wait_until(lambda: self.source.snapshot()['fps'] == 59.5)
        state = self.source.snapshot()
        self.assertLess(state['fps_age'], 1.)
        self.assertEqual(state['invalid'], 0)

        for bad in (b'', b'xxxx' + b'\0' * 10, PREFIX.pack(b'SVL1', 1) + b'\x04',
                    FPS.pack(b'SVF1', float('nan')), FPS.pack(b'SVF1', -1.)):
            self.send(bad)
        wait_until(lambda: self.source.snapshot()['invalid'] == 5)
        self.assertFalse(self.source.error)
        self.assertEqual(self.source.snapshot()['fps'], 59.5)

        with self.source.lock:  # simulate 3 s of silence without sleeping
            self.source.fps_at -= 4
        self.assertIsNone(self.source.snapshot()['fps'])

    def test_source_ip_filter_ignores_foreign_packets(self):
        foreign_store = Store('filtered')
        foreign = UdpSource(foreign_store, '127.0.0.1', 0, '192.0.2.7')
        self.addCleanup(foreign.close)
        sender = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.addCleanup(sender.close)
        sender.sendto(datagram(0, seq=0), ('127.0.0.1', foreign.port))
        wait_until(lambda: foreign.snapshot()['foreign'] == 1)
        self.assertEqual(foreign.snapshot()['records'], 0)
        self.assertEqual(len(foreign_store.frames), 0)
        self.assertEqual(foreign_store.epoch, 0)

    def test_save_writes_decodable_rmse_records_and_refuses_overwrite(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'capture.bin'
            with self.assertRaisesRegex(ValueError, '尚无'):
                self.source.save(path)
            tx_ns, rx_ns = 1_000_000, 4_500_000_123
            self.send(datagram(0, seq=11, ns=tx_ns))
            self.send(datagram(1, seq=12, ns=rx_ns))
            wait_until(lambda: self.source.snapshot()['records'] == 2)
            self.assertEqual(self.source.save(path), 2)

            # The saved file must be readable by the offline decoder unchanged.
            tx_rows, rx_rows, extra = decode(path)
            self.assertEqual((len(tx_rows), len(rx_rows)), (1, 1))
            self.assertEqual(tx_rows[0]['time_ns'], tx_ns)
            self.assertEqual(rx_rows[0]['time_ns'], rx_ns)
            self.assertEqual(tx_rows[0]['t_s'], tx_ns * 1e-9)
            self.assertEqual(rx_rows[0]['t_s'], rx_ns * 1e-9)
            self.assertEqual((tx_rows[0]['seq'], rx_rows[0]['seq']), (11, 12))
            self.assertEqual((tx_rows[0]['valid'], rx_rows[0]['valid']), (1, 1))
            self.assertAlmostEqual(tx_rows[0]['yaw_rad'], .3)
            self.assertAlmostEqual(rx_rows[0]['yaw_rad'], .25)
            self.assertEqual(extra['crc_bad'], 0)
            with self.assertRaises(FileExistsError):
                self.source.save(path)

    def test_recorder_streams_original_bytes_including_rejected_records(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'record.bin'
            recorder = Recorder(path)
            try:
                self.source.set_recorder(recorder)
                good = datagram(1, seq=0, ns=1_000_000)
                bad_status = datagram(1, seq=1, ns=2_000_000, status=1)
                self.send(good)
                self.send(bad_status)
                wait_until(lambda: self.source.snapshot()['records'] == 2)
                wait_until(lambda: recorder.snapshot()['written'] == 2)
            finally:
                self.source.set_recorder(None)
                recorder.close()
            self.assertEqual(path.read_bytes(), good[PREFIX.size:] + bad_status[PREFIX.size:])

    def test_session_switch_stops_recording_and_drops_new_session(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'record.bin'
            recorder = Recorder(path)
            self.source.set_recorder(recorder)
            first_session = datagram(0, seq=0, ns=1_000_000, session=42)
            try:
                self.send(first_session)
                wait_until(lambda: recorder.snapshot()['written'] == 1)
                # The vision process restarted: a new session id arrives mid-recording.
                self.send(datagram(0, seq=0, ns=1_000_000, session=43))
                wait_until(lambda: self.source.snapshot()['session'] == 43)
                state = self.source.snapshot()
                self.assertIsNone(state['recorder'])   # detached, not queued further
                self.assertIn('会话切换', state['recording_stopped_reason'])
                self.assertFalse(recorder.snapshot()['active'])
            finally:
                recorder.close()
            wait_until(lambda: recorder.snapshot()['finished'])
            # One .bin keeps one sender session: only the first record reached disk.
            self.assertEqual(path.read_bytes(), first_session[PREFIX.size:])

    def test_close_stops_thread_and_is_idempotent(self):
        self.source.close()
        self.assertFalse(self.source.running())
        self.source.close()
        self.assertTrue(self.source.snapshot()['error'] == '')


if __name__ == '__main__':
    unittest.main()
