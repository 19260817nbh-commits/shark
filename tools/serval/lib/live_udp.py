"""Read-only live telemetry. No serial port access and no control commands."""
from collections import deque
from pathlib import Path
import math
import socket
import struct
import threading
import time

from .protocol import ProtocolError
from .recording import HEADER, MAGIC, Recorder, decode_record

PREFIX = struct.Struct('<4sQ')
FPS = struct.Struct('<4sd')


def unpack_datagram(data):
    if len(data) < PREFIX.size:
        raise ValueError('Short telemetry datagram')
    magic, session = PREFIX.unpack_from(data)
    if magic != b'SVL1':
        raise ValueError('Unknown telemetry format')
    result, offset = [], PREFIX.size
    while offset < len(data):
        if len(data) - offset < HEADER.size:
            raise ValueError('Incomplete telemetry record')
        fields = HEADER.unpack_from(data, offset)
        marker, direction, status, length, ns, seq, _ = fields
        if marker != MAGIC or direction not in (0, 1) or not 1 <= length <= 64:
            raise ValueError('Invalid telemetry header')
        end = offset + HEADER.size + length
        if end > len(data):
            raise ValueError('Incomplete telemetry payload')
        result.append((direction, status, ns, seq, data[offset + HEADER.size:end], data[offset:end]))
        offset = end
    if not result:
        raise ValueError('Empty telemetry datagram')
    return session, result


class LiveStore:
    def __init__(self, capacity=60000):
        self.lock = threading.RLock()
        self.capacity = capacity
        self.session = None
        self.retired = deque(maxlen=32)
        self.frames = {0:deque(maxlen=capacity), 1:deque(maxlen=capacity)}
        self.raw = deque(maxlen=capacity)
        self.latest_raw = {}
        self.last_seq = {}
        self.last_valid_seq = {}
        self.missing = self.late = self.bad = 0
        self.received_at = None
        self.fps = None
        self.fps_at = None
        self.arrivals = {0:deque(), 1:deque()}
        self.recorder = None

    def start_recording(self, path):
        with self.lock:
            if self.recorder is not None:
                raise ValueError('已经在录制')
            self.recorder = Recorder(path)
            return self.recorder

    def stop_recording(self):
        with self.lock:
            if self.recorder is not None:
                self.recorder.stop()
                self.recorder = None

    def ingest(self, data, now=None):
        now = time.monotonic() if now is None else now
        if data[:4] == b'SVF1':
            if len(data) != FPS.size:
                raise ValueError('Invalid FPS datagram')
            _, fps = FPS.unpack(data)
            if not math.isfinite(fps) or fps < 0:
                raise ValueError('Invalid FPS value')
            with self.lock:
                self.fps, self.fps_at = fps, now
            return
        session, records = unpack_datagram(data)
        with self.lock:
            if session in self.retired:
                self.late += len(records)
                return
            if session != self.session:
                if self.session is not None:
                    # Each file has a single sender clock and sequence space.
                    self.stop_recording()
                    self.retired.append(self.session)
                self.session = session
                for values in self.frames.values():
                    values.clear()
                for values in self.arrivals.values():
                    values.clear()
                self.raw.clear()
                self.latest_raw.clear()
                self.last_seq.clear()
                self.last_valid_seq.clear()
                self.missing = self.late = self.bad = 0
                self.fps = self.fps_at = None
            for direction, status, ns, seq, payload, raw in records:
                previous = self.last_seq.get(direction)
                delta = (seq - previous) & 0xffffffff if previous is not None else 1
                if previous is not None and (delta == 0 or delta >= 0x80000000):
                    self.late += 1
                    continue
                self.missing += delta - 1
                self.last_seq[direction] = seq
                self.raw.append(raw)
                if self.recorder is not None:
                    self.recorder.add_raw(raw)
                self.latest_raw[direction] = payload
                self.received_at = now
                arrivals = self.arrivals[direction]
                arrivals.append(now)
                while arrivals and now - arrivals[0] > 1:
                    arrivals.popleft()
                try:
                    values = decode_record(direction, status, payload)
                except ProtocolError:
                    self.bad += 1
                    continue
                previous_valid = self.last_valid_seq.get(direction, (seq - 1) & 0xffffffff)
                gap = ((seq - previous_valid) & 0xffffffff) != 1
                self.last_valid_seq[direction] = seq
                self.frames[direction].append(dict(t=ns * 1e-9, values=values, gap=gap))

    def snapshot(self, window=10., now=None, *, include_series=True):
        now = time.monotonic() if now is None else now
        with self.lock:
            latest = {direction: frames[-1] for direction, frames in self.frames.items() if frames}
            end = max((frame['t'] for frame in latest.values()), default=0.)
            series = {0: [], 1: []}
            if include_series:
                for direction, frames in self.frames.items():
                    recent = series[direction]
                    for frame in reversed(frames):
                        recent.append(frame)
                        if frame['t'] < end-window:
                            break
                    recent.reverse()
            return dict(session=self.session, end=end, series=series, latest=latest,
                        missing=self.missing, late=self.late, bad=self.bad,
                        age=None if self.received_at is None else max(0., now-self.received_at),
                        fps=self.fps if self.fps_at is not None and now-self.fps_at<=3 else None,
                        rates={d:sum(now-t<=1 for t in a) for d,a in self.arrivals.items()})

    def recent_packets(self, direction=None, limit=60):
        with self.lock:
            records = []
            for raw in reversed(self.raw):
                if direction is None or raw[4] == direction:
                    records.append(raw)
                    if len(records) == limit:
                        break
            return records

    def save(self, path):
        with self.lock:
            records = list(self.raw)
        if not records:
            raise ValueError('No packets received')
        # Exclusive creation avoids accidentally overwriting a previous measurement.
        with Path(path).open('xb') as output:
            for record in records:
                output.write(record)
        return len(records)


class Receiver:
    def __init__(self, store, host='0.0.0.0', port=9871, source_ip=None):
        self.store, self.source_ip = store, source_ip
        self.peer = source_ip
        self.error = ''
        self.invalid = self.foreign = 0
        self.socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4*1024*1024)
            self.socket.bind((host, port))
            self.socket.settimeout(.2)
        except BaseException:
            self.socket.close()
            raise
        self.port = self.socket.getsockname()[1]
        self.stop = threading.Event()
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def _run(self):
        try:
            while not self.stop.is_set():
                try:
                    data, peer = self.socket.recvfrom(65535)
                except socket.timeout:
                    continue
                if self.peer is not None and peer[0] != self.peer:
                    self.foreign += 1
                    continue
                try:
                    self.store.ingest(data)
                    self.peer = peer[0]
                except ValueError:
                    self.invalid += 1
        except OSError as exc:
            if not self.stop.is_set():
                self.error = str(exc)

    def close(self):
        self.stop.set()
        self.socket.close()
        self.thread.join(timeout=1)
