#!/usr/bin/env python3
"""Measure the live UDP -> decode -> SSE pipeline latency (not a unittest).

The Windows receiver does all decoding: UDP recv thread decodes each RMSE
record, writes it to the Store, and the SSE thread pushes it to the page.
This benchmark binds a real loopback UDP receiver + HTTP/SSE server, sends
``SVL1`` datagrams at a fixed rate while stamping every record with
``perf_counter``, then measures **send -> JSON event received** on the SSE
client for each decoded frame.

Target from the project requirements: the transmission + parse + push pipeline
should stay within **15 ms** (p95) on the Windows side. Screen paint adds one
QtWebEngine rAF frame (~16.7 ms at 60 FPS) on top; it is not part of this
number.

Run from ``tools/serval``::

    python tests/latency_pipeline.py                  # 240 Hz push (GUI auto default)
    python tests/latency_pipeline.py --push-hz 240    # stricter pipeline budget
    python tests/latency_pipeline.py --rate 500 --duration 6
"""
import argparse
import json
import math
import os
import socket
import struct
import sys
import threading
import time
from pathlib import Path

HERE = Path(__file__).resolve()
ROOT = HERE.parents[1]                 # tools/serval
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

PREFIX = struct.Struct('<4sQ')
SENDER_NS_BASE = 1_234_567_890_123


def free_udp_port():
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.bind(('127.0.0.1', 0))
        return sock.getsockname()[1]


def percentile(values, fraction):
    if not values:
        return None
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, int(math.ceil(len(ordered) * fraction) - 1)))
    return ordered[index]


class Sender(threading.Thread):
    """Send SVL1 TX+RX records and remember perf_counter send times by seq."""

    def __init__(self, port, duration=6.0, rate=500.0):
        super().__init__(name='latency-sender', daemon=True)
        self.port = port
        self.duration = duration
        self.interval = 1.0 / rate
        self.session = 0x5EEA10E3
        self.sent = 0
        self.stop_event = threading.Event()
        self.send_times = {}
        self.lock = threading.Lock()
        self.error = None

    def _pack(self, t):
        from lib.protocol import RX, TX, crc16
        yaw = .48 * math.sin(t * .8)
        pitch = .18 * math.sin(t * .55)
        tx = TX.pack(b'SP', 1, yaw, .384 * math.cos(t * .8), -.3072 * math.sin(t * .8),
                     pitch, .099 * math.cos(t * .55), -.05445 * math.sin(t * .55), 3, 0)
        tx = tx[:-2] + crc16(tx[:-2]).to_bytes(2, 'little')
        ryaw = .47 * math.sin((t - .1) * .8)
        rpitch = .175 * math.sin((t - .08) * .55)
        cy, sy = math.cos(ryaw / 2), math.sin(ryaw / 2)
        cp, sp = math.cos(rpitch / 2), math.sin(rpitch / 2)
        rx = RX.pack(b'SP', 1, cy * cp, -sy * sp, cy * sp, sy * cp, ryaw,
                     .376 * math.cos((t - .1) * .8), rpitch,
                     .09625 * math.cos((t - .08) * .55), 26, 0, 1, 0)
        rx = rx[:-2] + crc16(rx[:-2]).to_bytes(2, 'little')
        return tx, rx

    def run(self):
        from lib.recording import HEADER, MAGIC, STATUS_HEAD, STATUS_OK
        sequence = {'tx': 0, 'rx': 0}
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
                started = time.perf_counter()
                next_send = 0.0
                while not self.stop_event.is_set():
                    now = time.perf_counter() - started
                    if now < next_send:
                        remaining = next_send - now
                        if remaining > .0012:
                            time.sleep(remaining - .0008)
                        while (not self.stop_event.is_set()
                               and time.perf_counter() - started < next_send):
                            pass
                        now = time.perf_counter() - started
                    if now >= self.duration:
                        break
                    tx, rx = self._pack(now)
                    ns = SENDER_NS_BASE + int(now * 1e9)
                    datagram = bytearray(PREFIX.pack(b'SVL1', self.session))
                    for direction, packet in (('tx', tx), ('rx', rx)):
                        sequence[direction] += 1
                        # Stamp right before sendto so the measured latency is send -> SSE.
                        send_at = time.perf_counter()
                        with self.lock:
                            self.send_times[(direction, sequence[direction])] = send_at
                        datagram += HEADER.pack(MAGIC, 0 if direction == 'tx' else 1,
                                                STATUS_HEAD | STATUS_OK, len(packet),
                                                ns, sequence[direction], 0)
                        datagram += packet
                    sock.sendto(bytes(datagram), ('127.0.0.1', self.port))
                    self.sent += 1
                    next_send = max(next_send + self.interval, now + self.interval)
        except OSError as exc:
            self.error = str(exc)


class SseReader:
    """Minimal raw-socket HTTP/1.1 chunked ``text/event-stream`` reader.

    urllib's chunked decoder buffers/decodes the body, which is unsuitable for
    measuring per-event arrival time; raw sockets keep the framing visible.
    """

    def __init__(self, url):
        from urllib.parse import urlparse
        parsed = urlparse(url)
        self.sock = socket.create_connection((parsed.hostname, parsed.port), timeout=1.5)
        path = parsed.path + (('?' + parsed.query) if parsed.query else '')
        self.sock.sendall((
            'GET %s HTTP/1.1\r\nHost: %s:%s\r\nAccept: text/event-stream\r\n'
            'Connection: close\r\n\r\n' % (path, parsed.hostname, parsed.port)).encode())
        self.buffer = b''
        while b'\r\n\r\n' not in self.buffer:
            chunk = self.sock.recv(4096)
            if not chunk:
                raise EOFError('stream closed before headers')
            self.buffer += chunk
        headers, self.buffer = self.buffer.split(b'\r\n\r\n', 1)
        if b' 200 ' not in headers.split(b'\r\n', 1)[0]:
            raise RuntimeError('unexpected HTTP status: %r' % headers.split(b'\r\n', 1)[0])

    def _read_exact(self, count):
        while len(self.buffer) < count:
            chunk = self.sock.recv(4096)
            if not chunk:
                raise EOFError('stream closed')
            self.buffer += chunk
        data, self.buffer = self.buffer[:count], self.buffer[count:]
        return data

    def _readline(self):
        while b'\r\n' not in self.buffer:
            chunk = self.sock.recv(4096)
            if not chunk:
                raise EOFError('stream closed')
            self.buffer += chunk
        line, self.buffer = self.buffer.split(b'\r\n', 1)
        return line

    def next_event(self):
        while True:
            size_line = self._readline()
            if not size_line:
                continue
            size = int(size_line.split(b';')[0], 16)
            if size == 0:
                raise EOFError('stream ended')
            payload = self._read_exact(size)
            self._read_exact(2)  # CRLF after chunk
            for line in payload.split(b'\n'):
                if line.startswith(b'data: '):
                    return line[6:]
            # keep-alive / retry / comment chunks contain no data line

    def close(self):
        try:
            self.sock.close()
        except Exception:
            pass


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--push-hz', type=float, default=240.0,
                        help='SSE push rate to measure (GUI auto default is 240)')
    parser.add_argument('--rate', type=float, default=500.0,
                        help='records per second per direction (default 500)')
    parser.add_argument('--duration', type=float, default=6.0,
                        help='measurement duration in seconds (default 6)')
    parser.add_argument('--limit-ms', type=float, default=15.0,
                        help='p95 pipeline budget in ms (default 15)')
    args = parser.parse_args(argv)

    from lib.desktop_window import ensure_application
    import monitor
    ensure_application()
    app = monitor.Application(udp=dict(host='127.0.0.1', port=free_udp_port()),
                              push_hz=max(20.0, min(240.0, args.push_hz)))
    server = monitor.create_server(app, '127.0.0.1', 0)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, name='latency-http', daemon=True)
    thread.start()
    base = 'http://127.0.0.1:%d' % server.server_port
    sender = Sender(app.udp.port, duration=args.duration, rate=args.rate)
    watchdog = threading.Timer(args.duration + 20.0, lambda: os._exit(3))
    watchdog.daemon = True
    watchdog.start()
    reader = None
    latencies = []
    parse_ms = []
    first_event = None
    try:
        # Subscribe first (empty backlog), then start sending, so every measured
        # frame was sent after the SSE stream was ready.
        reader = SseReader(base + '/api/stream?since=0')
        sender.start()
        started = time.perf_counter()
        while time.perf_counter() - started < args.duration + 3.0:
            try:
                raw = reader.next_event()
            except (TimeoutError, socket.timeout):
                if not sender.is_alive() and latencies:
                    break
                if time.perf_counter() - started > args.duration + 1.0:
                    break
                continue
            received = time.perf_counter()
            parse_start = time.perf_counter()
            data = json.loads(raw)
            parse_ms.append((time.perf_counter() - parse_start) * 1000)
            for frame in data.get('frames') or []:
                direction = frame.get('direction')
                # ``seq`` is the store-wide ordering cursor; ``log_seq`` is the
                # per-direction protocol sequence used on the wire.
                seq = frame.get('log_seq', frame.get('seq'))
                with sender.lock:
                    sent_at = sender.send_times.pop((direction, seq), None)
                if sent_at is None:
                    continue
                latencies.append((received - sent_at) * 1000)
                if first_event is None:
                    first_event = latencies[-1]
            if not sender.is_alive() and latencies and time.perf_counter() - started > args.duration:
                break
        missing = app.udp.missing
        late = app.udp.late
        invalid = app.udp.invalid
        sender.stop_event.set()
        sender.join(timeout=2)
    finally:
        # The HTTP server runs in a daemon thread; force-exit at the end, so never
        # block a measurement on a lingering SSE handler thread.
        if reader is not None:
            reader.close()
        missing = locals().get('missing', getattr(app.udp, 'missing', '?'))
        late = locals().get('late', getattr(app.udp, 'late', '?'))
        invalid = locals().get('invalid', getattr(app.udp, 'invalid', '?'))
        try:
            app.close()
        except Exception:
            pass

    if sender.error:
        print('[latency] sender error:', sender.error, flush=True)
        os._exit(1)
    if len(latencies) < 20:
        print('[latency] FAIL: too few matched frames (%d); server/push not working'
              % len(latencies), flush=True)
        os._exit(1)
    p50 = percentile(latencies, .50)
    p95 = percentile(latencies, .95)
    p99 = percentile(latencies, .99)
    parse_p95 = percentile(parse_ms, .95)
    print('[latency] push_hz=%.0f rate=%.0f/direction frames=%d'
          % (args.push_hz, args.rate, len(latencies)), flush=True)
    print('[latency] send -> SSE JSON received: '
          'p50=%.2f ms p95=%.2f ms p99=%.2f ms max=%.2f ms first=%.2f ms'
          % (p50, p95, p99, max(latencies), first_event if first_event is not None else -1),
          flush=True)
    print('[latency] client JSON parse: p95=%.3f ms' % parse_p95, flush=True)
    print('[latency] UDP counters: missing=%s late=%s invalid=%s'
          % (missing, late, invalid), flush=True)
    ok = p95 <= args.limit_ms and missing == 0 and invalid == 0
    print('[latency] %s: p95 %.2f ms %s %.2f ms budget'
          % ('PASS' if ok else 'FAIL', p95, '<=' if ok else '>', args.limit_ms), flush=True)
    if watchdog is not None:
        watchdog.cancel()
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(0 if ok else 1)


if __name__ == '__main__':
    sys.exit(main())
