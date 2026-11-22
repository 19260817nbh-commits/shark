"""Observe growing RMSE logs without opening any serial port or writing to the log."""
import re
import threading
import time
from pathlib import Path

from protocol import ProtocolError, RX, TX, decode_frame
from recording import HEADER, MAGIC


def read_project_config(path):
    """Read top-level display/launch settings without rewriting YAML."""
    result = {'path': str(Path(path).resolve()), 'enemy_color': None, 'com_port': None,
              'enemy_color_source': None, 'auto_fire': None}
    # These project fields are single-line scalars. Reject unfamiliar YAML constructs.
    pattern = re.compile(r"""^(enemy_color|com_port|enemy_color_source|auto_fire):\s*(?:"([^"]*)"|'([^']*)'|([^#\s]+))\s*(?:#.*)?$""")
    seen = set()
    for line in Path(path).read_text(encoding='utf-8-sig').splitlines():
        if not re.match(r'^(enemy_color|com_port|enemy_color_source|auto_fire):', line):
            continue
        match = pattern.fullmatch(line)
        if not match or match[1] in seen:
            raise ValueError('颜色、串口、auto_fire 配置需为唯一的顶层单行标量')
        seen.add(match[1])
        result[match[1]] = next(v for v in match.groups()[1:] if v is not None)
    if result['enemy_color'] not in (None, 'red', 'blue'):
        raise ValueError('enemy_color 仅支持 red 或 blue')
    if result['enemy_color_source'] not in (None, 'serial', 'config'):
        raise ValueError('enemy_color_source 仅支持 serial 或 config')
    if result['auto_fire'] is not None:
        if result['auto_fire'].lower() not in ('true', 'false'):
            raise ValueError('auto_fire 仅支持 true 或 false')
        result['auto_fire'] = result['auto_fire'].lower() == 'true'
    return result


class LogFollower:
    def __init__(self, path, on_frame, on_error, on_session, on_raw=None):
        self.path = Path(path).resolve()
        self.on_frame, self.on_error, self.on_session = on_frame, on_error, on_session
        self.on_raw = on_raw
        self.stop_event = threading.Event()
        self.lock = threading.RLock()
        self.file = None
        self.file_path = None
        self.identity = None
        self.buffer = bytearray()
        self.anchor = None
        self.first_ns = None
        self.offset = self.records = self.skipped = 0
        self.error = ''
        self.started = self.last_scan = 0.0
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def snapshot(self):
        with self.lock:
            return dict(path=str(self.path), current=str(self.file_path) if self.file_path else None,
                        records=self.records, skipped=self.skipped, error=self.error,
                        complete_bytes=self.offset-len(self.buffer),
                        running=self.thread.is_alive() and not self.stop_event.is_set())

    def _find(self):
        if self.path.is_dir():
            files = list(self.path.glob('*.bin'))
            return max(files, key=lambda p: (p.stat().st_mtime_ns, p.name)) if files else None
        return self.path if self.path.is_file() else None

    def _open(self, candidate):
        stat = candidate.stat()
        identity = (stat.st_dev, stat.st_ino)
        changed = candidate != self.file_path or identity != self.identity
        truncated = self.file is not None and not changed and stat.st_size < self.offset
        if not changed and not truncated:
            return
        if self.file:
            self.file.close()
        self.file = candidate.open('rb')
        self.file_path, self.identity = candidate, identity
        self.buffer.clear()
        self.first_ns = self.anchor = None
        self.offset = self.records = self.skipped = 0
        self.error = ''
        last_ns = None
        while not self.stop_event.is_set():
            header = self.file.read(HEADER.size)
            if len(header) < HEADER.size:
                break
            magic, direction, status, length, ns, seq, reserved = HEADER.unpack(header)
            if magic != MAGIC or direction not in (0, 1) or not 1 <= length <= 64:
                raise ValueError('不是有效 RMSE 日志')
            if self.file.tell()+length > stat.st_size:
                break
            if self.first_ns is None:
                self.first_ns = ns
            last_ns = ns
            self.file.seek(length, 1)
        span = max(0, (last_ns-self.first_ns)*1e-9) if last_ns is not None else 0
        age = max(0, time.time()-stat.st_mtime) if last_ns is not None else 0
        self.anchor = time.monotonic()-span-age
        self.started = self.anchor
        self.file.seek(0)
        self.on_session(str(candidate), self.anchor)

    def _consume(self):
        while len(self.buffer) >= HEADER.size:
            magic, direction, status, length, ns, seq, reserved = HEADER.unpack_from(self.buffer)
            if magic != MAGIC or direction not in (0, 1) or not 1 <= length <= 64:
                raise ValueError('日志记录头损坏或不是 RMSE 格式')
            size = HEADER.size + length
            if len(self.buffer) < size:
                return
            payload = bytes(self.buffer[HEADER.size:size])
            del self.buffer[:size]
            self.records += 1
            if self.first_ns is None:
                self.first_ns = ns
            stamp = self.anchor + (ns-self.first_ns)*1e-9
            if self.on_raw:
                self.on_raw('tx' if direction == 0 else 'rx', payload, stamp, bool(status & 2),
                            status=status, log_seq=seq, log_ns=ns)
            try:
                expected = TX.size if direction == 0 else RX.size
                if length != expected:
                    raise ProtocolError('length', f'日志帧长度 {length} 与协议 {expected} 不符')
                decoded_direction, values = decode_frame(payload)
                if status & 4 or not status & 2:
                    raise ProtocolError('record_status', '记录端报告 I/O 异常或未确认成功；仅显示原始包')
            except ProtocolError as exc:
                self.skipped += 1
                self.on_error(exc)
                continue
            self.on_frame(decoded_direction, values, stamp)

    def _run(self):
        try:
            while not self.stop_event.is_set():
                with self.lock:
                    now = time.monotonic()
                    if now-self.last_scan >= .25:
                        candidate = self._find()
                        if candidate is not None:
                            self._open(candidate)
                        self.last_scan = now
                    if self.file:
                        # Bound each pass so stopping remains responsive.
                        data = self.file.read(256*1024)
                        if data:
                            self.offset += len(data)
                            self.buffer.extend(data)
                            self._consume()
                            continue
                self.stop_event.wait(.02)
        except (OSError, ValueError) as exc:
            with self.lock:
                self.error = str(exc)
        finally:
            if self.file:
                self.file.close()

    def close(self):
        self.stop_event.set()
        self.thread.join(timeout=3)
        if self.thread.is_alive():
            raise RuntimeError('日志监听线程尚未退出')
