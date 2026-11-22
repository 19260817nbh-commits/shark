import json
import math
from pathlib import Path
import tempfile
import threading
from http.server import ThreadingHTTPServer
from urllib.error import HTTPError
from urllib.request import urlopen
import unittest
from contextlib import ExitStack

from latency import estimate_follow_delay
from monitor import Application, make_handler
from packet_dump import Reader
from test_packets import log_record
from test_serval import packet, wait_until


def motion_frames(yaw_lag=.12, pitch_lag=.08, wrap=False):
    def wave(t):
        return .35*math.sin(4*t)+.12*math.sin(9*t)+.06*math.sin(17*t)
    frames = []
    for i in range(651):
        t = i*.01
        yaw = 3.1+wave(t) if wrap else wave(t)
        actual = 3.1+wave(t+.001-yaw_lag) if wrap else .9*wave(t+.001-yaw_lag)+.03
        if wrap:
            yaw, actual = math.remainder(yaw, 2*math.pi), math.remainder(actual, 2*math.pi)
        frames.extend([
            dict(t=t, direction='tx', values=dict(mode=1, yaw=yaw, pitch=wave(t))),
            dict(t=t+.001, direction='rx', values=dict(mode=1, yaw=actual, pitch=wave(t+.001-pitch_lag))),
        ])
    return sorted(frames, key=lambda f:f['t'])


class FollowDelayTests(unittest.TestCase):
    def test_recovers_known_lag_with_independent_sampling_gain_and_offset(self):
        result = estimate_follow_delay(motion_frames(), 6.51)
        self.assertEqual(result['yaw']['delay_ms'], 120)
        self.assertEqual(result['pitch']['delay_ms'], 80)
        self.assertGreater(result['yaw']['correlation'], .999)

    def test_yaw_wrapping_does_not_create_false_lag(self):
        result = estimate_follow_delay(motion_frames(wrap=True), 6.51)
        self.assertEqual(result['yaw']['delay_ms'], 120)

    def test_no_control_stale_and_stationary_are_not_reported_as_zero_delay(self):
        frames = motion_frames()
        self.assertEqual(estimate_follow_delay(frames, 9)['yaw']['status'], 'stale')
        frames[-2]['values']['mode'] = 0
        self.assertEqual(estimate_follow_delay(frames, 6.51)['yaw']['status'], 'not_controlled')
        frames = motion_frames()
        for frame in frames:
            frame['values']['yaw'] = .1
        result = estimate_follow_delay(frames, 6.51)
        self.assertIsNone(result['yaw']['delay_ms'])
        self.assertEqual(result['yaw']['status'], 'no_motion')

    def test_constant_speed_is_ambiguous_without_an_identifiable_time_feature(self):
        frames = motion_frames()
        for frame in frames:
            frame['values']['yaw'] = frame['t']*.1-(.02 if frame['direction']=='rx' else 0)
        result = estimate_follow_delay(frames, 6.51)
        self.assertIsNone(result['yaw']['delay_ms'])
        self.assertEqual(result['yaw']['status'], 'ambiguous')


class DownloadTests(unittest.TestCase):
    def setUp(self):
        self.app = Application()
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), make_handler(self.app))
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = 'http://127.0.0.1:' + str(self.server.server_port)

    def tearDown(self):
        self.server.shutdown()
        self.thread.join()
        self.server.server_close()
        self.app.close()

    def test_download_entire_log_including_bad_crc_not_just_packet_cache(self):
        with tempfile.TemporaryDirectory() as directory, ExitStack() as cleanup:
            cleanup.callback(self.app.stop)
            path = Path(directory)/'capture.bin'
            bad = packet('rx')[:-2]+b'\0\0'
            data = b''.join(log_record('rx', packet('rx'), i*1_000_000, i) for i in range(1200))
            data += log_record('rx', bad, 1_201_000_000, 1200, status=1)
            partial = log_record('tx', packet('tx'))[:35]
            path.write_bytes(data+partial)
            self.app.start_follow(path)
            wait_until(lambda:self.app.follower.records == 1201)
            self.assertEqual(len(self.app.store.packets), 1000)
            with urlopen(self.base+'/api/export.bin') as response:
                downloaded = response.read()
                self.assertEqual(response.headers['X-Serval-Export-Scope'], 'log')
                self.assertIn('attachment', response.headers['Content-Disposition'])
                self.assertEqual(int(response.headers['Content-Length']), len(data))
            self.assertEqual(downloaded, data)
            self.assertEqual(path.read_bytes(), data+partial)

    def test_download_is_fixed_size_while_recording_grows(self):
        with tempfile.TemporaryDirectory() as directory, ExitStack() as cleanup:
            cleanup.callback(self.app.stop)
            path = Path(directory)/'capture.bin'
            first = log_record('tx', packet('tx'))
            path.write_bytes(first)
            self.app.start_follow(path)
            wait_until(lambda:self.app.follower.records == 1)
            stream, length, _, scope = self.app.open_packet_download()
            try:
                with path.open('ab') as output:
                    output.write(log_record('rx', packet('rx')))
                self.assertEqual(stream.read(length), first)
                self.assertEqual(scope, 'log')
            finally:
                stream.close()

    def test_cache_download_is_valid_rmse_and_contains_both_directions(self):
        self.app.record_raw('tx', packet('tx'), self.app.store.start, True)
        self.app.record_raw('rx', packet('rx'), self.app.store.start+.01, True)
        stream, length, name, scope = self.app.open_packet_download()
        with stream:
            records = list(Reader(stream).records())
        self.assertEqual(scope, 'cache')
        self.assertEqual(name, 'serval-cached-packets.bin')
        self.assertEqual([r[0] for r in records], [0, 1])
        self.assertEqual(records[0][-1], packet('tx'))

    def test_empty_download_and_latency_endpoint(self):
        with self.assertRaises(HTTPError) as error:
            urlopen(self.base+'/api/export.bin')
        self.assertEqual(error.exception.code, 409)
        with urlopen(self.base+'/api/latency') as response:
            data = json.load(response)
        self.assertIsNone(data['following']['yaw']['delay_ms'])
        self.assertAlmostEqual(data['wire_time_ms']['tx'], 30*10*1000/921600)


if __name__ == '__main__':
    unittest.main()
