"""RMSE logs compatible with io/gimbal/serial_recorder and save/decode_serial_log.py."""
import math
import os
import queue
import struct
import threading
import time
from pathlib import Path

from protocol import ProtocolError, decode_frame

MAGIC = 0x45534D52
HEADER = struct.Struct('<IBBHQII')


class Recorder:
    """Disk I/O never runs on serial forwarding threads."""
    def __init__(self, path, capacity=8192):
        self.path = str(Path(path).resolve())
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self.file = open(self.path, 'xb')
        self.start = time.monotonic()
        self.queue = queue.Queue(maxsize=capacity)
        self.stop_event = threading.Event()
        self.sequences = {'tx': 0, 'rx': 0}
        self.written = self.dropped = 0
        self.flushed_bytes = 0
        self.error = ''
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def add(self, direction, packet, timestamp, crc_ok):
        if self.stop_event.is_set() or self.error:
            return
        sequence = self.sequences[direction]
        self.sequences[direction] += 1
        stamp = time.monotonic() if timestamp is None else timestamp
        ns = max(0, int((stamp - self.start) * 1e9))
        # TX reached the write call successfully; RX status describes its CRC.
        status = 1 | (2 if direction == 'tx' or crc_ok else 0)
        header = HEADER.pack(MAGIC, 0 if direction == 'tx' else 1,
                             status, len(packet), ns, sequence, 0)
        try:
            self.queue.put_nowait(header + packet)
        except queue.Full:
            self.dropped += 1

    def _run(self):
        last_flush = time.monotonic()
        try:
            while not self.stop_event.is_set() or not self.queue.empty():
                try:
                    data = self.queue.get(timeout=.1)
                except queue.Empty:
                    data = None
                if data is not None:
                    self.file.write(data)
                    self.written += 1
                if time.monotonic() - last_flush >= .2:
                    self.file.flush()
                    self.flushed_bytes = self.file.tell()
                    last_flush = time.monotonic()
            self.file.flush()
            self.flushed_bytes = self.file.tell()
        except OSError as exc:
            self.error = str(exc)
        finally:
            self.file.close()

    def close(self):
        self.stop_event.set()
        self.thread.join(timeout=3)
        if self.thread.is_alive():
            self.error = '日志仍在写入；请检查存储设备'

    def snapshot(self):
        return dict(path=self.path, written=self.written, dropped=self.dropped,
                    flushed_bytes=self.flushed_bytes,
                    error=self.error, active=not self.stop_event.is_set() and not self.error)


class Replay:
    """Read an indexed RMSE log; playback never opens or writes a serial port."""
    def __init__(self, path, on_frame, on_error, base_time, on_raw=None):
        self.path = str(Path(path).resolve())
        self.on_frame, self.on_error, self.base_time = on_frame, on_error, base_time
        self.on_raw = on_raw
        self.entries = []
        self.times = []
        self.truncated_bytes = 0
        self.complete_bytes = 0
        self.condition = threading.Condition(threading.RLock())
        self.stop_event = threading.Event()
        self.position = 0.0
        self.speed = 1.0
        self.playing = False
        self.anchor = time.monotonic()
        self.index = 0
        self.error = ''
        self.file = open(self.path, 'rb')
        try:
            self._index()
        except Exception:
            self.file.close()
            raise
        self.duration = self.times[-1] if self.times else 0.0
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def _index(self):
        size = os.fstat(self.file.fileno()).st_size
        while self.file.tell() < size:
            offset = self.file.tell()
            raw = self.file.read(HEADER.size)
            if len(raw) < HEADER.size:
                self.truncated_bytes = size - offset
                break
            magic, direction, status, length, ns, seq, _ = HEADER.unpack(raw)
            if magic != MAGIC or direction not in (0, 1) or not 1 <= length <= 64:
                raise ValueError(f'不是有效 RMSE 日志，记录偏移 {offset}')
            if self.file.tell() + length > size:
                self.truncated_bytes = size - offset
                break
            self.entries.append((ns, offset + HEADER.size, length, direction, status, seq))
            self.file.seek(length, os.SEEK_CUR)
            self.complete_bytes = self.file.tell()
            if len(self.entries) > 2000000:
                raise ValueError('日志超过 200 万条，请先分段')
        if not self.entries:
            raise ValueError('日志没有完整数据帧')
        self.entries.sort(key=lambda entry: entry[0])
        first = self.entries[0][0]
        self.times = [(entry[0] - first) * 1e-9 for entry in self.entries]

    def _position(self):
        if self.playing:
            return min(self.duration, self.position + (time.monotonic() - self.anchor) * self.speed)
        return self.position

    def snapshot(self):
        with self.condition:
            return dict(path=self.path, playing=self.playing, speed=self.speed,
                        position=self._position(), duration=self.duration,
                        records=len(self.entries), error=self.error,
                        truncated_bytes=self.truncated_bytes)

    def control(self, action, speed=None):
        with self.condition:
            self.position = self._position()
            self.anchor = time.monotonic()
            if action == 'play':
                self.playing = True
            elif action == 'pause':
                self.playing = False
            elif action == 'speed':
                if not isinstance(speed, (int, float)) or not math.isfinite(speed) or not .1 <= speed <= 8:
                    raise ValueError('回放倍率必须在 0.1 到 8 之间')
                self.speed = speed
            elif action == 'restart':
                self.index = 0
                self.position = 0.0
                self.playing = True
            else:
                raise ValueError('未知回放操作')
            self.condition.notify_all()

    def _run(self):
        try:
            while not self.stop_event.is_set():
                with self.condition:
                    if not self.playing:
                        self.condition.wait(timeout=.1)
                        continue
                    now = self._position()
                    if self.index >= len(self.entries):
                        self.position = self.duration
                        self.playing = False
                        continue
                    if self.times[self.index] > now:
                        self.condition.wait(timeout=min(.05, (self.times[self.index]-now)/self.speed))
                        continue
                    ns, offset, length, expected, status, seq = self.entries[self.index]
                    timestamp = self.times[self.index]
                    self.index += 1
                    self.file.seek(offset)
                    packet = self.file.read(length)
                    if self.on_raw:
                        self.on_raw('tx' if expected == 0 else 'rx', packet,
                                    self.base_time + timestamp, bool(status & 2),
                                    status=status, log_seq=seq, log_ns=ns)
                    try:
                        direction, values = decode_frame(packet)
                        if status & 4 or not status & 2:
                            raise ProtocolError('record_status', '记录端报告 I/O 异常或未确认成功；仅显示原始包')
                        if direction != ('tx' if expected == 0 else 'rx'):
                            raise ProtocolError('length', '日志方向与帧长度不符')
                    except ProtocolError as exc:
                        self.on_error(exc)
                    else:
                        self.on_frame(direction, values, self.base_time + timestamp)
        except (OSError, ValueError) as exc:
            self.error = str(exc)
            self.playing = False

    def close(self):
        self.stop_event.set()
        with self.condition:
            self.condition.notify_all()
        self.thread.join(timeout=2)
        self.file.close()
