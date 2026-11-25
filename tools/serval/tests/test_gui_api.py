"""GUI HTTP API: SSE live stream, native quick actions, camera preview, UDP mode guards."""
from datetime import datetime, timedelta
import http.client
import json
import socket
import tempfile
import threading
import time
from pathlib import Path
import unittest
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from unittest.mock import patch

from lib.live_udp import FPS
from monitor import Application, create_server
from .test_serval import wait_until
from .test_udp_source import datagram


class FakeNative:
    """Stand-in for lib/desktop_window.NativeHost (runs on the HTTP thread)."""

    def __init__(self):
        self.calls = []
        self.responses = {}

    def handle(self, action, payload):
        self.calls.append((action, dict(payload)))
        return self.responses.get(action)


class ApiTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.app = Application(udp=dict(host='127.0.0.1', port=0, source_ip='127.0.0.1'))
        self.udp_port = self.app.udp.port
        self.server = create_server(self.app, '127.0.0.1', 0)
        self.thread = threading.Thread(target=self.server.serve_forever,
                                       kwargs={'poll_interval': .02}, daemon=True)
        self.thread.start()
        self.addCleanup(self._shutdown)
        self.base = 'http://127.0.0.1:' + str(self.server.server_port)
        self.sender = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.addCleanup(self.sender.close)

    def _shutdown(self):
        self.server.shutdown()
        self.thread.join(timeout=5)
        self.server.server_close()
        self.app.close()

    # -- helpers ----------------------------------------------------------
    def send(self, data):
        self.sender.sendto(data, ('127.0.0.1', self.udp_port))

    def request(self, method, path, payload=None, headers=None):
        request_headers = dict(headers or {})
        body = None
        if payload is not None:
            body = json.dumps(payload).encode()
            request_headers['Content-Type'] = 'application/json'
        request = Request(self.base + path, data=body, headers=request_headers, method=method)
        try:
            with urlopen(request, timeout=15) as response:
                return response.status, response.headers, response.read()
        except HTTPError as error:
            return error.code, error.headers, error.read()

    def get_json(self, path):
        status, _headers, body = self.request('GET', path)
        return status, (json.loads(body) if body else None)

    def post_json(self, path, payload):
        status, _headers, body = self.request('POST', path, payload)
        return status, (json.loads(body) if body else None)

    @staticmethod
    def read_event(response):
        """Read one SSE event (key/value lines) or return None at EOF."""
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
            if line.startswith(':'):
                return {'comment': line}
            key, _, value = line.partition(':')
            fields[key] = value.lstrip(' ')

    def read_data_event(self, response, timeout=3.):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            event = self.read_event(response)
            if event is None:
                raise AssertionError('SSE connection closed before a data event')
            if 'data' in event:
                return event
        raise AssertionError('Timed out waiting for an SSE data event')

    def open_stream(self, since=0):
        connection = http.client.HTTPConnection('127.0.0.1', self.server.server_port, timeout=5)
        self.addCleanup(connection.close)
        connection.request('GET', f'/api/stream?since={since}',
                           headers={'Accept': 'text/event-stream'})
        response = connection.getresponse()
        return connection, response

    # -- snapshot shape ---------------------------------------------------
    def test_live_udp_snapshot_shape_and_http11_content_length(self):
        self.send(datagram(0, seq=0, ns=1_000_000))
        self.send(datagram(1, seq=0, ns=2_000_000))
        wait_until(lambda: len(self.app.store.frames) == 2)
        self.assertEqual(self.app.mode, 'live_udp')
        self.assertEqual(self.server.protocol_version, 'HTTP/1.1')
        self.assertTrue(self.server.daemon_threads)

        status, headers, body = self.request('GET', '/api/data?since=0')
        self.assertEqual(status, 200)
        self.assertIsNotNone(headers.get('Content-Length'))
        self.assertEqual(int(headers['Content-Length']), len(body))
        data = json.loads(body)
        self.assertEqual(data['mode'], 'live_udp')
        self.assertEqual(sorted(frame['t'] for frame in data['frames']), [1e-3, 2e-3])
        udp = data['udp']
        for key in ('host', 'port', 'peer', 'error', 'running', 'session', 'records',
                    'missing', 'late', 'invalid', 'bad', 'foreign', 'fps', 'fps_age',
                    'age', 'retained', 'capacity', 'seq'):
            self.assertIn(key, udp)
        self.assertEqual(udp['session'], 42)
        self.assertEqual(udp['peer'], '127.0.0.1')
        self.assertEqual(udp['records'], 2)
        self.assertEqual(udp['missing'], 0)
        self.assertEqual(udp['late'], 0)
        self.assertTrue(udp['running'])
        self.assertEqual((udp['seq']['tx'], udp['seq']['rx']), (0, 0))
        self.assertEqual(data['camera'], {'available': False, 'fps': 60})

        status, native = self.get_json('/api/native/status')
        self.assertEqual(status, 200)
        self.assertEqual(native, dict(available=False, recording=None, last_decode=None,
                                      mode='live_udp', capabilities=[]))

        # An existing download endpoint must still complete under HTTP/1.1.
        status, headers, body = self.request('GET', '/api/export.bin')
        self.assertEqual(status, 200)
        self.assertEqual(headers['Content-Type'], 'application/octet-stream')
        self.assertEqual(int(headers['Content-Length']), len(body))
        self.assertGreater(len(body), 0)

    def test_live_udp_now_shares_the_sender_time_axis(self):
        # A remote vision process may have started long before this monitor, so
        # sender t_ns starts at an arbitrary multi-minute value.
        base_ns = 1234567890123
        self.send(datagram(0, seq=0, ns=base_ns))
        self.send(datagram(1, seq=1, ns=base_ns + 1_000_000))
        wait_until(lambda: len(self.app.store.frames) == 2)

        status, data = self.get_json('/api/data?since=0')
        self.assertEqual(status, 200)
        end = max(frame['t'] for frame in data['frames'])
        self.assertGreater(end, 1234.)
        self.assertLessEqual(abs(data['now'] - end), 1.)
        self.assertGreaterEqual(data['now'], end)
        # Freshness/tracking must be evaluated on the sender axis, not local time.
        self.assertIsNotNone(data['tracking'])
        self.assertLess(data['tracking']['sample_offset_ms'], 50.)
        self.assertEqual(data['udp']['last_t'], (base_ns + 1_000_000) * 1e-9)
        self.assertIsNotNone(data['udp']['last_ingest_at'])
        self.assertEqual(data['udp']['frame_count'], 2)

        status, packets = self.get_json('/api/packets?direction=tx&limit=5')
        self.assertEqual(status, 200)
        self.assertLessEqual(abs(packets['now'] - end), 1.)

        status, analysis = self.get_json('/api/analysis')
        self.assertEqual(status, 200)
        self.assertEqual(analysis['epoch'], self.app.store.epoch)

    def test_diagnostic_fps_falls_back_to_udp_only_when_log_is_stale(self):
        status, before = self.get_json('/api/diagnostics')
        self.assertEqual(status, 200)
        self.assertNotIn('fps', before['latest'])

        self.send(FPS.pack(b'SVF1', 59.5))
        wait_until(lambda: self.app.udp.snapshot()['fps'] == 59.5)
        status, after = self.get_json('/api/diagnostics')
        fps = after['latest']['fps']
        self.assertEqual(fps['fields'], {'program': 'UDP', 'fps': 59.5})
        self.assertLess(fps['age_s'], 3)

        # A fresh diagnostic log line wins over the UDP fallback...
        self.app.diagnostic_store.add_line('standard_mpc fps: 120.0')
        status, fresh = self.get_json('/api/diagnostics')
        self.assertEqual(fresh['latest']['fps']['fields']['program'], 'standard_mpc')
        # ...but a stale one is replaced by the UDP reading.
        stamp = (datetime.now() - timedelta(seconds=10)).strftime('%Y-%m-%d %H:%M:%S')
        self.app.diagnostic_store.add_line(f'[{stamp}] standard_mpc fps: 120.0')
        status, stale = self.get_json('/api/diagnostics')
        self.assertEqual(stale['latest']['fps']['fields']['program'], 'UDP')
        self.assertEqual(stale['latest']['fps']['fields']['fps'], 59.5)

    # -- SSE --------------------------------------------------------------
    def test_sse_pushes_new_frames_within_half_second(self):
        connection, response = self.open_stream()
        self.assertEqual(response.status, 200)
        self.assertEqual(response.getheader('Content-Type'), 'text/event-stream; charset=utf-8')
        self.assertTrue(response.chunked)
        self.assertIsNotNone(response.getheader('Transfer-Encoding'))
        self.assertIsNone(response.getheader('Content-Length'))
        retry = self.read_event(response)
        self.assertEqual(retry.get('retry'), '500')

        sent = time.monotonic()
        self.send(datagram(0, seq=0, ns=1_000_000))
        self.send(datagram(1, seq=0, ns=2_000_000))
        frames, events = {}, []
        while sum(len(event.get('frames', [])) for event in events) < 2:
            event = self.read_data_event(response)
            events.append(json.loads(event['data']))
        elapsed = time.monotonic() - sent
        self.assertLess(elapsed, .5, 'SSE did not push the new frames within 0.5 s')
        for data in events:
            self.assertIn('cursor', data)
            self.assertIn('udp', data)
            self.assertEqual(data['mode'], 'live_udp')
            self.assertEqual(data['id'] if 'id' in data else data['cursor'], data['cursor'])
            for frame in data['frames']:
                frames[frame['t']] = frame
        self.assertEqual(sorted(frames), [1e-3, 2e-3])
        cursors = [event['cursor'] for event in events]
        self.assertEqual(cursors, sorted(cursors))
        self.assertEqual(cursors[-1], 2)
        connection.close()

    def test_sse_sends_backlog_immediately_then_keepalive(self):
        self.send(datagram(0, seq=0, ns=1_000_000))
        wait_until(lambda: len(self.app.store.frames) == 1)
        connection, response = self.open_stream(0)
        retry = self.read_event(response)
        self.assertEqual(retry.get('retry'), '500')
        started = time.monotonic()
        event = self.read_data_event(response, timeout=2)
        self.assertLess(time.monotonic() - started, .5)
        data = json.loads(event['data'])
        self.assertEqual([frame['t'] for frame in data['frames']], [1e-3])
        self.assertEqual(data['cursor'], 1)

        # With nothing new to push, a comment heartbeat keeps the stream alive.
        started = time.monotonic()
        heartbeat = self.read_event(response)
        self.assertEqual(heartbeat, {'comment': ': keepalive'})
        self.assertGreaterEqual(time.monotonic() - started, .5)
        connection.close()

        # A page closing the SSE stream must not break the server.
        status, after = self.get_json('/api/data?since=0')
        self.assertEqual(status, 200)
        self.assertEqual(after['mode'], 'live_udp')

    # -- native quick actions without a desktop host ----------------------
    def test_native_auto_save_record_decode_open(self):
        self.send(datagram(0, seq=0, ns=1_000_000))
        wait_until(lambda: self.app.udp.snapshot()['records'] == 1)
        with patch('monitor.ROOT', Path(self.directory.name)):
            status, saved = self.post_json('/api/native/save', {})
            self.assertEqual(status, 200)
            self.assertEqual(saved['count'], 1)
            self.assertTrue(Path(saved['path']).is_file())
            self.assertTrue(saved['path'].startswith(str(Path(self.directory.name))))
            self.assertEqual(self.request('POST', '/api/native/save',
                                          {'path': saved['path']})[0], 409)

            status, started = self.post_json('/api/native/record/start', {})
            self.assertEqual(status, 200)
            self.assertTrue(started['recording']['active'])
            record_path = Path(started['recording']['path'])
        self.send(datagram(0, seq=1, ns=2_000_000))
        wait_until(lambda: self.app.udp.snapshot()['records'] == 2)
        status, stopped = self.post_json('/api/native/record/stop', {})
        self.assertEqual(status, 200)
        self.assertFalse(stopped['recording']['active'])
        self.assertTrue(stopped['recording']['finished'])
        self.assertGreater(record_path.stat().st_size, 0)

        status, decoded = self.post_json('/api/native/decode',
                                         {'path': saved['path'], 'plot': False})
        self.assertEqual(status, 200)
        self.assertEqual(decoded['returncode'], 0, decoded)
        output_dir = Path(decoded['output_dir'])
        self.assertTrue(output_dir.is_dir())
        self.assertTrue((output_dir / 'tx.csv').is_file())
        self.assertTrue((output_dir / 'rx.csv').is_file())
        self.assertIn('tx.csv', decoded['output'])
        self.assertEqual(self.app.native_status()['last_decode'], decoded)

        bad_file = Path(self.directory.name) / 'capture.txt'
        bad_file.write_text('not a log')
        for path in (str(Path(self.directory.name) / 'missing.bin'), str(bad_file)):
            status, error = self.post_json('/api/native/decode', {'path': path, 'plot': False})
            self.assertEqual(status, 400)
            self.assertIn('error', error)

        opened = []
        with patch.object(self.app, 'open_directory',
                          side_effect=lambda path: opened.append(path) or {'ok': True}):
            status, result = self.post_json('/api/native/open', {'path': self.directory.name})
        self.assertEqual((status, result), (200, {'ok': True}))
        self.assertEqual(opened, [self.directory.name])

    def test_native_host_delegation_and_cancel_branches(self):
        native = FakeNative()
        native.responses.update({'save': {'cancelled': True},
                                 'record-start': {'cancelled': True},
                                 'record-stop': None,
                                 'decode': {'cancelled': True},
                                 'open': {'ok': False}})
        self.app.native = native
        status, native_status = self.get_json('/api/native/status')
        self.assertEqual(status, 200)
        self.assertTrue(native_status['available'])
        self.assertEqual(native_status['capabilities'], ['save', 'record', 'decode', 'open'])

        self.assertEqual(self.post_json('/api/native/save', {}),
                         (200, {'cancelled': True}))
        self.assertEqual(self.post_json('/api/native/record/start', {}),
                         (200, {'cancelled': True}))
        self.assertEqual(self.post_json('/api/native/record/stop', {}),
                         (200, {'recording': None}))
        payload = {'path': None, 'plot': True, 'out_dir': None}
        self.assertEqual(self.post_json('/api/native/decode', payload),
                         (200, {'cancelled': True}))
        self.assertEqual(self.post_json('/api/native/open', {'path': '/tmp/x'}),
                         (200, {'ok': False}))
        self.assertEqual([call[0] for call in native.calls],
                         ['save', 'record-start', 'record-stop', 'decode', 'open'])
        self.assertEqual(native.calls[3][1], payload)

        # A host that returns a recorder snapshot is wrapped by the HTTP layer.
        native.responses['record-start'] = dict(path='/tmp/cap.bin', written=0, active=True)
        status, body = self.post_json('/api/native/record/start', {})
        self.assertEqual(status, 200)
        self.assertEqual(body, {'recording': native.responses['record-start']})

    # -- camera preview ---------------------------------------------------
    def test_camera_jpeg_and_mjpeg_endpoints(self):
        status, data = self.get_json('/api/camera.jpg')
        self.assertEqual(status, 404)
        self.assertIn('error', data)

        jpeg = b'\xff\xd8fake-jpeg-body\xff\xd9'
        path = Path(self.directory.name) / 'camera.jpg'
        path.write_bytes(jpeg)
        self.app.camera = str(path)
        status, headers, body = self.request('GET', '/api/camera.jpg')
        self.assertEqual(status, 200)
        self.assertEqual(headers['Content-Type'], 'image/jpeg')
        self.assertEqual(body, jpeg)
        status, data = self.get_json('/api/data?since=0')
        self.assertEqual(data['camera'], {'available': True, 'fps': 60})

        connection = http.client.HTTPConnection('127.0.0.1', self.server.server_port, timeout=5)
        self.addCleanup(connection.close)
        connection.request('GET', '/api/camera.mjpg')
        response = connection.getresponse()
        self.assertEqual(response.status, 200)
        self.assertEqual(response.getheader('Content-Type'),
                         'multipart/x-mixed-replace; boundary=frame')
        head = (b'--frame\r\nContent-Type: image/jpeg\r\nContent-Length: ' +
                str(len(jpeg)).encode() + b'\r\n\r\n')
        part = response.read(len(head) + len(jpeg) + 2)
        self.assertEqual(part, head + jpeg + b'\r\n')
        connection.close()

        self.app.camera = None
        status, data = self.get_json('/api/data?since=0')
        self.assertEqual(data['camera'], {'available': False, 'fps': 60})

    def test_stop_flushes_an_active_recording(self):
        path = Path(self.directory.name) / 'recording.bin'
        self.app.start_recording(str(path))
        self.send(datagram(0, seq=0, ns=1_000_000))
        wait_until(lambda: self.app.recording_snapshot()['written'] >= 1)
        recorder = self.app.recorder
        self.assertIsNotNone(recorder)
        self.app.stop()  # equivalent to closing the desktop window mid-recording
        self.assertIsNone(self.app.recorder)
        self.assertIsNone(self.app.udp)
        self.assertEqual(self.app.mode, 'idle')
        self.assertTrue(recorder.snapshot()['finished'])
        self.assertFalse(recorder.snapshot()['active'])
        self.assertGreater(path.stat().st_size, 0)

    def test_session_switch_auto_stops_recording_and_page_sees_finished(self):
        first = Path(self.directory.name) / 'first.bin'
        self.app.start_recording(str(first))
        self.send(datagram(0, seq=0, ns=1_000_000, session=42))
        wait_until(lambda: self.app.recording_snapshot()['written'] >= 1)
        recorder = self.app.recorder
        # Visual restart: a new session id must end the take instead of mixing two
        # sender clocks/sequence spaces into one .bin.
        self.send(datagram(0, seq=0, ns=500_000, session=43))
        wait_until(lambda: self.app.udp.snapshot()['session'] == 43)
        wait_until(lambda: recorder.snapshot()['finished'])
        self.assertFalse(recorder.snapshot()['active'])
        status, data = self.get_json('/api/data?since=0')
        self.assertEqual(status, 200)
        self.assertFalse(data['recording']['active'])
        self.assertEqual(data['recording']['path'], str(first.resolve()))
        self.assertIn('会话切换', data['udp']['recording_stopped_reason'])
        self.assertEqual(data['udp']['session'], 43)
        # The page's record button switches back: a new take can start immediately.
        second = Path(self.directory.name) / 'second.bin'
        started = self.app.start_recording(str(second))
        self.assertTrue(started['active'])
        self.assertFalse(started['finished'])
        self.assertFalse(self.app.stop_recording()['active'])

    def test_large_backlog_snapshot_is_sent_as_one_complete_chunk(self):
        for index in range(5000):
            self.app.store.add('rx', {'yaw': .1, 'pitch': .2, 'mode': 1},
                               exact_t=(index + 1) * .001,
                               log_seq=index, log_ns=(index + 1) * 1_000_000)
        status, _headers, body = self.request('GET', '/api/data?since=0')
        self.assertEqual(status, 200)
        data = json.loads(body)
        self.assertEqual(len(data['frames']), 5000)
        self.assertGreater(len(body), 64 * 1024)  # >64 KB in one manual chunk

        connection, response = self.open_stream(0)
        self.assertTrue(response.chunked)
        retry = self.read_event(response)
        self.assertEqual(retry.get('retry'), '500')
        event = self.read_data_event(response, timeout=5)
        self.assertGreater(len(event['data'].encode()), 64 * 1024)
        parsed = json.loads(event['data'])
        self.assertEqual(len(parsed['frames']), 5000)
        self.assertEqual([frame['t'] for frame in parsed['frames'][:2]], [.001, .002])
        connection.close()

    # -- live_udp mode guards ---------------------------------------------
    def test_live_udp_rejects_other_sources_and_stop_closes_udp(self):
        self.assertEqual(self.app.mode, 'live_udp')
        for action in (lambda: self.app.start_bridge({}),
                       lambda: self.app.start_replay(Path(self.directory.name) / 'x.bin'),
                       lambda: self.app.start_follow(str(Path(self.directory.name)))):
            with self.assertRaises(ValueError):
                action()
        status, error = self.post_json('/api/start', {})
        self.assertEqual(status, 400)
        self.assertIn('error', error)

        status, _body = self.post_json('/api/stop', {})
        self.assertEqual(status, 200)
        self.assertEqual(self.app.mode, 'idle')
        self.assertIsNone(self.app.udp)
        status, data = self.get_json('/api/data?since=0')
        self.assertEqual(status, 200)
        self.assertIsNone(data['udp'])

        # UDP can be restarted after stop; a plain Application has no cache to save.
        state = self.app.start_udp('127.0.0.1', 0, '127.0.0.1')
        self.assertEqual(self.app.mode, 'live_udp')
        self.assertEqual(state['records'], 0)
        plain = Application()
        try:
            with self.assertRaises(ValueError):
                plain.save_cache()
            with self.assertRaises(ValueError):
                plain.handle_native('save', {})
            with self.assertRaises(ValueError):
                plain.handle_native('unknown-action', {})
        finally:
            plain.close()


if __name__ == '__main__':
    unittest.main()
