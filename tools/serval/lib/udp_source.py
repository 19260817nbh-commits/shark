"""Low-latency UDP telemetry source feeding monitor.Store.

The vision program emits two datagram kinds on the same UDP port:

* ``SVL1`` telemetry: a session id (u64) followed by one or more RMSE records
  (``<IBBHQII`` header: magic, direction, status, length, t_ns, seq, reserved)
  concatenated with the original wire bytes.
* ``SVF1`` FPS: a single float64 frames-per-second reading.

This module keeps the sender clock: ``t`` in the store is exactly
``ns * 1e-9`` seconds for every decoded frame (the store is told that exact
relative time so no ``start + x - start`` floating point round trip can lose
precision).  Raw records are cached (byte-for-byte, bad CRC included) so the
GUI can save a decodable ``.bin`` or stream them into ``Recorder``.

Read-only: never opens a serial port and never sends anything back.
"""
import math
import socket
import threading
import time
from collections import deque
from pathlib import Path

from .live_udp import FPS, unpack_datagram
from .protocol import ProtocolError, RX, TX, check_crc16
from .recording import decode_record

FPS_FRESH_S = 3.0
DIRECTIONS = {0: 'tx', 1: 'rx'}
DETAIL_CAPACITY = 512   # recent per-direction interval samples kept for statistics
GAP_LIMIT = 32          # most recent abnormal sender intervals exposed to the page


class UdpSource:
    """Receive SVL1/SVF1 datagrams and decode them into a ``monitor.Store``."""

    def __init__(self, store, host='0.0.0.0', port=9871, source_ip=None, capacity=60000):
        self.store = store
        self.host = host
        self.source_ip = source_ip
        self.capacity = capacity
        self.lock = threading.RLock()
        self.raw = deque(maxlen=capacity)
        self.recorder = None
        self.recording_stopped_reason = ''
        self.error = ''
        self.peer = source_ip
        self.session = None
        self.retired = deque(maxlen=8)
        self.received_at = None
        self.last_t = None          # sender-relative time of the last accepted record
        self.last_ingest_at = None  # local monotonic clock when that record arrived
        self.frame_count = 0
        self.fps = None
        self.fps_at = None
        self.last_seq = {}
        self.missing = self.late = self.invalid = self.foreign = self.records = 0
        # High-rate detail: recent sender sample intervals / network arrival
        # intervals per direction, plus the most recent abnormal sample gaps.
        self.detail_capacity = DETAIL_CAPACITY
        self.interval_samples = {0: deque(maxlen=DETAIL_CAPACITY), 1: deque(maxlen=DETAIL_CAPACITY)}
        self.arrival_samples = {0: deque(maxlen=DETAIL_CAPACITY), 1: deque(maxlen=DETAIL_CAPACITY)}
        # Running sum / sum of squares / count per ring (O(1) jitter at snapshot).
        self.interval_moments = {0: [0.0, 0.0, 0], 1: [0.0, 0.0, 0]}
        self.arrival_moments = {0: [0.0, 0.0, 0], 1: [0.0, 0.0, 0]}
        self.detail_last_ns = {}
        self.detail_last_at = {}
        self.detail_count = {0: 0, 1: 0}
        self.gap_events = deque(maxlen=GAP_LIMIT)
        self.socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4 * 1024 * 1024)
            self.socket.bind((host, port))
            self.socket.settimeout(.2)
        except BaseException:
            self.socket.close()
            raise
        self.port = self.socket.getsockname()[1]
        self.stop_event = threading.Event()
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    # -- receive loop -----------------------------------------------------
    def _run(self):
        try:
            while not self.stop_event.is_set():
                try:
                    data, peer = self.socket.recvfrom(65535)
                except socket.timeout:
                    continue
                except OSError as exc:
                    if self.stop_event.is_set():
                        break
                    self.error = str(exc)
                    break
                if self.source_ip is not None and peer[0] != self.source_ip:
                    with self.lock:
                        self.foreign += 1
                    continue
                try:
                    self.ingest(data, peer[0])
                except ValueError:
                    with self.lock:
                        self.invalid += 1
        except OSError as exc:  # pragma: no cover - defensive
            if not self.stop_event.is_set():
                self.error = str(exc)

    def ingest(self, data, peer=None, now=None):
        """Decode one datagram synchronously (used by the thread and by tests)."""
        # Arrival intervals need sub-ms resolution: time.monotonic() is only
        # ~15.6 ms granular on Windows (GetTickCount64), while perf_counter is a
        # high resolution monotonic clock on every platform. Tests that inject a
        # synthetic ``now`` keep deterministic arrival statistics.
        synthetic = now is not None
        now = time.monotonic() if now is None else now
        arrival_now = now if synthetic else time.perf_counter()
        if data[:4] == b'SVF1':
            if len(data) != FPS.size:
                raise ValueError('Invalid FPS datagram')
            _, fps = FPS.unpack(data)
            if not math.isfinite(fps) or fps < 0:
                raise ValueError('Invalid FPS value')
            with self.lock:
                self.fps, self.fps_at = fps, now
                if peer is not None:
                    self.peer = peer
            return 0
        session, records = unpack_datagram(data)
        with self.lock:
            if peer is not None:
                self.peer = peer
            if session in self.retired:
                # A previous session's late datagram must never reach the chart.
                self.late += len(records)
                return 0
            if self.session is None:
                self.session = session
            elif session != self.session:
                self.retired.append(self.session)
                self.session = session
                self.last_seq.clear()
                self.missing = self.late = 0
                self.fps = self.fps_at = None
                self._reset_detail()
                # One .bin must stay one sender session: a visual restart resets
                # t_ns/seq, so stop recording instead of mixing two sessions.
                self._stop_recording('UDP 会话切换（发送端可能重启），录制已自动结束')
                self.store.reset('UDP 实时 · ' + (self.peer or self.host), start=time.monotonic())
            add_record = self.store.add_record
            reject = self.store.reject
            sample_detail = self._sample_detail
            for direction, status, ns, seq, payload, raw in records:
                name = DIRECTIONS[direction]
                previous = self.last_seq.get(direction)
                delta = (seq - previous) & 0xffffffff if previous is not None else 1
                if previous is not None and (delta == 0 or delta >= 0x80000000):
                    self.late += 1
                    continue
                self.missing += delta - 1
                self.last_seq[direction] = seq
                self.records += 1
                self.received_at = now
                self.last_ingest_at = now
                self.raw.append(raw)
                sample_detail(direction, name, ns, now, arrival_now)
                exact_t = ns * 1e-9
                self.last_t = exact_t
                timestamp = self.store.start + exact_t
                recorder = self.recorder
                if recorder is not None:
                    try:
                        recorder.add_raw(raw)
                    except Exception:  # keep the receive loop alive even if a recorder fails
                        pass
                # One CRC check + one unpack per record. Store.add_record then
                # writes the raw-packet row and the waveform frame under one lock.
                failure = None
                values = None
                crc_ok = False
                expected = TX.size if direction == 0 else RX.size
                if len(payload) != expected:
                    failure = ProtocolError('length', f'日志帧长度 {len(payload)} 与协议 {expected} 不符')
                elif not check_crc16(payload):
                    failure = ProtocolError('crc', 'CRC 校验失败，请检查协议版本、波特率和线路')
                else:
                    crc_ok = True
                    try:
                        values = decode_record(direction, status, payload, verified=True)
                    except ProtocolError as exc:
                        failure = exc
                if failure is not None:
                    reject(failure)
                    values = None
                add_record(name, payload, timestamp=timestamp, exact_t=exact_t,
                           crc_ok=crc_ok, values=values,
                           error='' if failure is None else str(failure),
                           status=status, log_seq=seq, log_ns=ns)
                if values is not None:
                    self.frame_count += 1
        return len(records)

    # -- high-rate detail statistics --------------------------------------
    @staticmethod
    def _ring_append(samples, moments, value):
        if len(samples) == samples.maxlen:
            dropped = samples[0]
            moments[0] -= dropped
            moments[1] -= dropped * dropped
            moments[2] -= 1
        samples.append(value)
        moments[0] += value
        moments[1] += value * value
        moments[2] += 1

    def _sample_detail(self, direction, name, ns, now, arrival_now=None):
        """Record the sender/network intervals used by ``snapshot()['detail']``."""
        previous_ns = self.detail_last_ns.get(direction)
        if previous_ns is not None:
            interval_ms = max(0.0, (ns - previous_ns) * 1e-6)
            self._ring_append(self.interval_samples[direction],
                              self.interval_moments[direction], interval_ms)
            if interval_ms >= 20.0:
                threshold = max(20.0, 3.0 * (self._median(self.interval_samples[direction]) or 0.0))
                if interval_ms > threshold:
                    # Most recent first for the page; t is the sender timestamp of
                    # the record that ends the gap.
                    self.gap_events.append(dict(t=ns * 1e-9, ms=interval_ms, direction=name))
        self.detail_last_ns[direction] = ns
        arrival_now = now if arrival_now is None else arrival_now
        previous_at = self.detail_last_at.get(direction)
        if previous_at is not None:
            self._ring_append(self.arrival_samples[direction],
                              self.arrival_moments[direction],
                              max(0.0, (arrival_now - previous_at) * 1000.0))
        self.detail_last_at[direction] = arrival_now
        self.detail_count[direction] += 1

    @staticmethod
    def _median(samples):
        count = len(samples)
        if not count:
            return None
        ordered = sorted(samples)
        middle = count // 2
        if count % 2:
            return ordered[middle]
        return (ordered[middle - 1] + ordered[middle]) / 2.

    @staticmethod
    def _sample_summary(values, moments):
        """median/p95/max/population-std of an interval ring (``None`` if empty)."""
        count = len(values)
        if not count:
            return dict(median=None, p95=None, max=None, jitter=None)
        ordered = sorted(values)
        middle = count // 2
        median = ordered[middle] if count % 2 else (ordered[middle - 1] + ordered[middle]) / 2.
        p95 = ordered[min(count - 1, max(0, math.ceil(count * .95) - 1))]
        # Running sum/sum-of-squares avoid a second Python pass over <=512 samples.
        mean = moments[0] / count
        variance = max(0.0, moments[1] / count - mean * mean)
        return dict(median=median, p95=p95, max=ordered[-1], jitter=math.sqrt(variance))

    @classmethod
    def _detail_snapshot(cls, intervals, arrivals, interval_moments, arrival_moments,
                         counts, gaps, capacity):
        detail = dict(gaps=[dict(event) for event in reversed(gaps)], capacity=capacity)
        for direction, name in DIRECTIONS.items():
            interval = cls._sample_summary(intervals[direction], interval_moments[direction])
            arrival = cls._sample_summary(arrivals[direction], arrival_moments[direction])
            detail[name] = dict(
                interval_median_ms=interval['median'],
                interval_p95_ms=interval['p95'],
                interval_max_ms=interval['max'],
                interval_jitter_ms=interval['jitter'],
                arrival_median_ms=arrival['median'],
                arrival_p95_ms=arrival['p95'],
                arrival_jitter_ms=arrival['jitter'],
                count=counts[direction],
            )
        return detail

    def _reset_detail(self):
        for samples in self.interval_samples.values():
            samples.clear()
        for samples in self.arrival_samples.values():
            samples.clear()
        for moments in self.interval_moments.values():
            moments[:] = [0.0, 0.0, 0]
        for moments in self.arrival_moments.values():
            moments[:] = [0.0, 0.0, 0]
        self.detail_last_ns.clear()
        self.detail_last_at.clear()
        self.detail_count = {0: 0, 1: 0}
        self.gap_events.clear()

    # -- recording --------------------------------------------------------
    def _stop_recording(self, reason):
        """Stop the active Recorder from the receive thread (never blocks).

        The Recorder's writer thread flushes and closes the file on its own; the
        Application keeps its reference so ``data.recording`` shows the finished
        take (path + frame count) and the page switches back to 开始录制.
        """
        recorder = self.recorder
        if recorder is None:
            return
        self.recorder = None
        self.recording_stopped_reason = reason
        try:
            recorder.stop()
        except Exception:  # a broken recorder must not kill telemetry
            pass

    def set_recorder(self, recorder):
        with self.lock:
            self.recorder = recorder
            if recorder is not None:
                self.recording_stopped_reason = ''
        return recorder

    def save(self, path):
        """Write the cached raw records to a new .bin file; never overwrite."""
        with self.lock:
            records = list(self.raw)
        if not records:
            raise ValueError('尚无 UDP 原始数据可保存')
        target = Path(path).expanduser()
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open('xb') as output:
            for record in records:
                output.write(record)
        return len(records)

    # -- status -----------------------------------------------------------
    def now(self):
        """Current position on the *sender* clock (same axis as frame ``t``).

        Frame ``t`` is the vision process' SerialRecorder t_ns, which may already
        be hours ahead of the local monotonic clock on a remote Windows machine;
        the chart/analysis windows must therefore advance from the last accepted
        record, not from ``time.monotonic() - store.start``.
        """
        with self.lock:
            last_t, received = self.last_t, self.last_ingest_at
        if last_t is None:
            return 0.0
        elapsed = 0. if received is None else max(0., time.monotonic() - received)
        # Cap the extrapolation: a silent visual must not keep scrolling forever.
        return last_t + min(.5, elapsed)

    def running(self):
        return self.thread.is_alive() and not self.stop_event.is_set() and not self.error

    def snapshot(self):
        now = time.monotonic()
        with self.lock:
            fresh = (self.fps is not None and self.fps_at is not None
                     and now - self.fps_at <= FPS_FRESH_S)
            last_t, received = self.last_t, self.last_ingest_at
            data = dict(
                host=self.host, port=self.port, source_ip=self.source_ip,
                peer=self.peer, error=self.error, running=self.running(),
                session=self.session, records=self.records,
                missing=self.missing, late=self.late,
                invalid=self.invalid, foreign=self.foreign,
                fps=self.fps if fresh else None,
                fps_age=None if self.fps_at is None else max(0., now - self.fps_at),
                age=None if self.received_at is None else max(0., now - self.received_at),
                last_t=last_t,
                last_ingest_at=received,
                frame_count=self.frame_count,
                now=(0.0 if last_t is None else
                     last_t + min(.5, 0. if received is None else max(0., now - received))),
                seq={DIRECTIONS[d]: self.last_seq.get(d) for d in (0, 1)},
                retained=len(self.raw), capacity=self.capacity,
                recording_stopped_reason=self.recording_stopped_reason,
                recorder=None,
            )
            recorder = self.recorder
            # Copy the bounded interval rings and compute the statistics after
            # releasing the lock: sorting <=512 samples never delays packet ingest.
            interval_copies = {direction: list(values)
                               for direction, values in self.interval_samples.items()}
            arrival_copies = {direction: list(values)
                              for direction, values in self.arrival_samples.items()}
            interval_moments = {direction: list(moments)
                                for direction, moments in self.interval_moments.items()}
            arrival_moments = {direction: list(moments)
                               for direction, moments in self.arrival_moments.items()}
            detail_counts = dict(self.detail_count)
            gap_events = list(self.gap_events)
            detail_capacity = self.detail_capacity
        data['detail'] = self._detail_snapshot(interval_copies, arrival_copies,
                                               interval_moments, arrival_moments,
                                               detail_counts, gap_events, detail_capacity)
        if recorder is not None:
            data['recorder'] = recorder.snapshot()
        # Record-level CRC/status rejections are kept by Store; expose them here so
        # the page does not need to scrape the generic rejection counters.
        with self.store.lock:
            data['bad'] = self.store.rejected
        return data

    def close(self):
        self.stop_event.set()
        try:
            self.socket.close()
        except OSError:
            pass
        if self.thread is not threading.current_thread():
            self.thread.join(timeout=1)
