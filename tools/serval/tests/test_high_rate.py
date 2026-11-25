"""High-rate backend: single decode per record, detail statistics, push_hz/capacity."""
import http.client
import json
import socket
import threading
import time
import unittest
from unittest.mock import patch

import lib.protocol as protocol_module
import lib.udp_source as udp_source_module
from lib.live_udp import PREFIX
from lib.recording import HEADER, MAGIC
from monitor import Application, Store, create_server
from .test_serval import packet
from .test_udp_source import datagram


def multi_datagram(records, session=42):
    """One SVL1 datagram carrying several (direction, payload) records."""
    body = b''.join(HEADER.pack(MAGIC, direction, 3, len(payload), ns, seq, 0) + payload
                    for direction, ns, seq, payload in records)
    return PREFIX.pack(b'SVL1', session) + body


def read_event(response):
    """Read one SSE event from an http.client response (chunked handled by stdlib)."""
    fields = {}
    while True:
        line = response.readline()
        if not line:
            return None
        line = line.decode('utf-8').rstrip('\r\n')
        if not line:
            if fields:
                return fields
            continue
        key, _, value = line.partition(':')
        fields[key] = value.lstrip(' ')


class SingleDecodeTests(unittest.TestCase):
    def setUp(self):
        self.store = Store('test')
        self.source = udp_source_module.UdpSource(self.store, '127.0.0.1', 0, '127.0.0.1')
        self.addCleanup(self.source.close)
        self.sender = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.addCleanup(self.sender.close)
        self.address = ('127.0.0.1', self.source.port)

    def test_each_record_is_crc_checked_and_decoded_exactly_once(self):
        multi = multi_datagram([(0, 3_000_000, 1, packet('tx')),
                                (1, 4_000_000, 1, packet('rx'))])
        frames = [datagram(0, seq=0, ns=1_000_000),
                  datagram(1, seq=0, ns=2_000_000),
                  multi]
        calls = {'crc': 0, 'decode': 0, 'crc16': 0}
        original_crc = udp_source_module.check_crc16
        original_decode = udp_source_module.decode_record
        original_crc16 = protocol_module.crc16

        def counting_crc(payload):
            calls['crc'] += 1
            return original_crc(payload)

        def counting_decode(direction, status, payload, *, verified=False):
            calls['decode'] += 1
            self.assertTrue(verified, 'UDP fast path must skip the extra CRC recompute')
            return original_decode(direction, status, payload, verified=verified)

        def counting_crc16(data):
            calls['crc16'] += 1
            return original_crc16(data)

        with patch.object(udp_source_module, 'check_crc16', counting_crc), \
                patch.object(udp_source_module, 'decode_record', counting_decode), \
                patch.object(protocol_module, 'crc16', counting_crc16):
            for frame in frames:
                self.sender.sendto(frame, self.address)
            timeout = time.monotonic() + 3
            while self.source.snapshot()['records'] != 4 and time.monotonic() < timeout:
                time.sleep(.005)
        self.assertEqual(self.source.snapshot()['records'], 4)
        self.assertEqual(calls, {'crc': 4, 'decode': 4, 'crc16': 4})

        # The fast path still produces exactly the same store content.
        self.assertEqual(len(self.store.frames), 4)
        self.assertEqual(self.store.latest['tx']['t'], 3_000_000 * 1e-9)
        self.assertEqual(self.store.latest['rx']['t'], 4_000_000 * 1e-9)
        self.assertEqual(self.store.latest['tx']['log_seq'], 1)
        self.assertEqual(self.store.latest['tx']['log_time_ns'], '3000000')
        self.assertEqual(self.store.rejected, 0)

    def test_add_record_writes_packet_and_frame_in_one_call(self):
        store = Store('record')
        payload = packet('rx')
        store.add_record('rx', payload, timestamp=store.start + .5, exact_t=.5, crc_ok=True,
                         values={'yaw': .25}, error='', status=3, log_seq=9, log_ns=500_000_000)
        packet_row = store.latest_packets['rx']
        frame = store.latest['rx']
        self.assertEqual(packet_row['hex'], payload.hex(' ').upper())
        self.assertEqual(packet_row['length'], len(payload))
        self.assertTrue(packet_row['crc_ok'])
        self.assertEqual(packet_row['crc_received'], packet_row['crc_calculated'])
        self.assertEqual((packet_row['log_seq'], packet_row['log_time_ns']), (9, '500000000'))
        self.assertEqual((frame['t'], frame['log_seq'], frame['log_time_ns']), (.5, 9, '500000000'))

        # values None: raw packet stays inspectable, waveforem untouched, one notify path.
        before = len(store.frames)
        bad = bytearray(packet('rx'))
        bad[-1] ^= 0xff
        bad = bytes(bad)
        store.add_record('rx', bad, timestamp=store.start + .6, exact_t=.6, crc_ok=False,
                         values=None, error='CRC 校验失败', status=3, log_seq=10, log_ns=600_000_000)
        self.assertEqual(len(store.frames), before)
        self.assertIsNone(store.latest_packets['rx']['values'])
        self.assertEqual(store.latest_packets['rx']['error'], 'CRC 校验失败')
        self.assertFalse(store.latest_packets['rx']['crc_ok'])


class DetailStatisticsTests(unittest.TestCase):
    def setUp(self):
        self.store = Store('detail')
        self.source = udp_source_module.UdpSource(self.store, '127.0.0.1', 0, '127.0.0.1')
        self.addCleanup(self.source.close)

    def ingest(self, direction, seq, ns, now, session=42):
        self.source.ingest(datagram(direction, seq=seq, ns=ns, session=session),
                           peer='127.0.0.1', now=now)

    def test_known_intervals_produce_correct_statistics_and_gaps(self):
        base_ns = 1_000_000
        for index in range(10):                      # ten 5 ms samples, 4 ms arrival
            self.ingest(0, index, base_ns + index * 5_000_000, 100.0 + index * .004)
        gap_ns = base_ns + 9 * 5_000_000 + 50_000_000
        self.ingest(0, 10, gap_ns, 100.0 + 10 * .004)

        detail = self.source.snapshot()['detail']
        self.assertEqual(detail['capacity'], 512)
        tx = detail['tx']
        self.assertEqual(tx['count'], 11)
        self.assertAlmostEqual(tx['interval_median_ms'], 5.0)
        self.assertEqual(tx['interval_p95_ms'], 50.0)
        self.assertEqual(tx['interval_max_ms'], 50.0)
        self.assertAlmostEqual(tx['interval_jitter_ms'], 13.5, places=6)
        self.assertAlmostEqual(tx['arrival_median_ms'], 4.0)
        self.assertAlmostEqual(tx['arrival_p95_ms'], 4.0)
        self.assertAlmostEqual(tx['arrival_jitter_ms'], 0.0)
        self.assertEqual(detail['gaps'], [dict(t=gap_ns * 1e-9, ms=50.0, direction='tx')])

        # Empty direction: explicit None (never NaN/negative) and JSON-safe output.
        self.assertEqual(detail['rx']['count'], 0)
        self.assertIsNone(detail['rx']['interval_median_ms'])
        self.assertIsNone(detail['rx']['arrival_p95_ms'])
        json.dumps(detail, allow_nan=False)

    def test_session_switch_resets_detail_without_negative_intervals(self):
        self.ingest(0, 0, 5_000_000_000, 1.0)
        self.ingest(0, 1, 5_005_000_000, 1.005)
        # A restarted sender: session changes and t_ns restarts from 1 ms.
        self.ingest(0, 0, 1_000_000, 2.0, session=43)
        self.ingest(0, 1, 6_000_000, 2.005, session=43)
        detail = self.source.snapshot()['detail']
        self.assertEqual(detail['tx']['count'], 2)
        self.assertEqual(detail['tx']['interval_median_ms'], 5.0)
        self.assertEqual(detail['tx']['interval_max_ms'], 5.0)
        self.assertEqual(detail['gaps'], [])
        self.assertEqual(detail['rx']['count'], 0)
        json.dumps(detail, allow_nan=False)

    def test_abnormal_gap_needs_to_exceed_three_times_the_median(self):
        # 100 ms stable samples => threshold max(20, 3*100) = 300 ms.
        for index in range(60):
            self.ingest(0, index, 1_000_000 + index * 100_000_000, 1.0 + index * .1)
        self.ingest(0, 60, 1_000_000 + 59 * 100_000_000 + 150_000_000, 1.0 + 6.0)  # 150 ms
        self.assertEqual(self.source.snapshot()['detail']['gaps'], [])
        self.ingest(0, 61, 1_000_000 + 59 * 100_000_000 + 150_000_000 + 400_000_000, 1.0 + 6.4)
        gaps = self.source.snapshot()['detail']['gaps']
        self.assertEqual(len(gaps), 1)
        self.assertEqual(gaps[0]['ms'], 400.0)
        self.assertEqual(gaps[0]['direction'], 'tx')

    def test_real_udp_detail_measures_sampling_and_arrival_intervals(self):
        source = udp_source_module.UdpSource(Store('detail-live'), '127.0.0.1', 0, '127.0.0.1')
        sender = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            payload = packet('tx')
            address = ('127.0.0.1', source.port)
            for seq in range(30):
                ns = 1_000_000 + seq * 4_000_000
                sender.sendto(PREFIX.pack(b'SVL1', 42) +
                              HEADER.pack(MAGIC, 0, 3, len(payload), ns, seq, 0) + payload,
                              address)
                time.sleep(.004)
            deadline = time.monotonic() + 3
            while source.snapshot()['records'] < 30 and time.monotonic() < deadline:
                time.sleep(.005)
            time.sleep(.1)
            detail = source.snapshot()['detail']
        finally:
            sender.close()
            source.close()
        tx = detail['tx']
        self.assertEqual(tx['count'], 30)
        self.assertEqual(tx['interval_median_ms'], 4.0)   # sender t_ns spacing
        self.assertEqual(tx['interval_max_ms'], 4.0)
        # Arrival intervals must use a high-resolution monotonic clock: Windows
        # time.monotonic() is 15.6 ms granular and would report 0 ms here.
        self.assertIsNotNone(tx['arrival_median_ms'])
        self.assertGreater(tx['arrival_median_ms'], .5)
        self.assertLess(tx['arrival_median_ms'], 25.)
        self.assertEqual(detail['gaps'], [])
        json.dumps(detail, allow_nan=False)


class PushAndCapacityTests(unittest.TestCase):
    def test_push_hz_clamp_snapshot_field_and_capacity(self):
        default = Application()
        try:
            self.assertEqual(default.push_hz, 60.0)
            self.assertEqual(default.store.frames.maxlen, 60000)
            self.assertEqual(default.snapshot(0)['push_hz'], 60.0)
        finally:
            default.close()

        app = Application(push_hz=120, capacity=1234)
        try:
            self.assertEqual(app.push_hz, 120.0)
            self.assertEqual(app.snapshot(0)['push_hz'], 120.0)
            self.assertEqual(app.store.frames.maxlen, 1234)
        finally:
            app.close()

        for raw, expected in ((5, 20.0), (400, 240.0), (90.5, 90.5), ('bad', 60.0), (float('nan'), 60.0)):
            app = Application(push_hz=raw)
            try:
                self.assertEqual(app.push_hz, expected)
            finally:
                app.close()

        app = Application(capacity=3)
        try:
            for index in range(5):
                app.store.add('rx', {'yaw': index, 'mode': 1}, exact_t=index * .001)
            self.assertEqual(app.store.frames.maxlen, 3)
            self.assertEqual(app.snapshot(0)['retained'], 3)
        finally:
            app.close()

    def test_real_sse_120hz_event_rate(self):
        app = Application(udp=dict(host='127.0.0.1', port=0, source_ip='127.0.0.1'),
                          push_hz=120)
        server = create_server(app, '127.0.0.1', 0)
        thread = threading.Thread(target=server.serve_forever, kwargs={'poll_interval': .02},
                                  daemon=True)
        thread.start()
        sender = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        connection = http.client.HTTPConnection('127.0.0.1', server.server_port, timeout=8)
        stop = threading.Event()

        payload = packet('tx')
        address = ('127.0.0.1', app.udp.port)

        def pump():
            seq = 0
            ns = 1_000_000
            while not stop.is_set():
                frame = (PREFIX.pack(b'SVL1', 42) +
                         HEADER.pack(MAGIC, 0, 3, len(payload), ns, seq, 0) + payload)
                sender.sendto(frame, address)
                seq += 1
                ns += 4_000_000      # 250 records/s: always data for the 120 Hz push
                time.sleep(.004)

        try:
            connection.request('GET', '/api/stream?since=0',
                               headers={'Accept': 'text/event-stream'})
            response = connection.getresponse()
            self.assertEqual(response.status, 200)
            self.assertTrue(response.chunked)
            retry = read_event(response)
            self.assertEqual(retry.get('retry'), '500')

            worker = threading.Thread(target=pump, daemon=True)
            worker.start()
            started = time.monotonic()
            events = 0
            first_data = None
            while time.monotonic() - started < 1.2:
                event = read_event(response)
                if event is None:
                    self.fail('SSE stream closed unexpectedly')
                if 'data' in event:
                    events += 1
                    if first_data is None:
                        first_data = json.loads(event['data'])
            elapsed = time.monotonic() - started
        finally:
            stop.set()
            if 'worker' in locals():
                worker.join(timeout=2)
            connection.close()
            sender.close()
            server.shutdown()
            thread.join(timeout=5)
            server.server_close()
            app.close()
        self.assertGreaterEqual(events, 90, 'SSE push rate too low: %d events in %.2fs' % (events, elapsed))
        self.assertLessEqual(events, 160, 'SSE push rate too high: %d events in %.2fs' % (events, elapsed))
        # The page contract: push_hz and udp.detail ride in the same snapshot.
        self.assertEqual(first_data['push_hz'], 120.0)
        detail = first_data['udp']['detail']
        self.assertEqual(detail['capacity'], 512)
        self.assertIn('interval_median_ms', detail['tx'])
        self.assertIn('arrival_jitter_ms', detail['rx'])
        self.assertIsInstance(detail['gaps'], list)


if __name__ == '__main__':
    unittest.main()
