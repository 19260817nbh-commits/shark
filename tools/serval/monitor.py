#!/usr/bin/env python3
"""Serval: OrinNX 30/44-byte serial monitoring, RMSE recording and offline replay."""
import argparse
import csv
import io
import json
import math
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
import webbrowser
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, unquote, urlparse

from lib.protocol import FIELDS, RX, TX, ProtocolError, crc16, decode_frame
from lib.recording import HEADER, MAGIC, Recorder, Replay
from lib.serial_io import SerialBridge
from lib.passive import LogFollower, read_project_config
from lib.latency import estimate_follow_delay
from lib.diagnostics import DiagnosticFollower, DiagnosticStore, explain_control
from lib.analysis import response_summary, step_response
from lib.udp_source import UdpSource

ROOT = Path(__file__).resolve().parent
WEB = ROOT / 'web'


class Store:
    def __init__(self, source, capacity=60000):
        self.source = source
        self.frames = deque(maxlen=capacity)
        self.lock = threading.RLock()
        # The SSE endpoint waits on this; add/add_packet/reset notify it while
        # holding the same lock, so a snapshot can never miss a change.
        self.updated = threading.Condition(self.lock)
        self.start = time.monotonic()
        self.seq = self.epoch = 0
        self.epoch_start_seq = 0
        self.rejected = 0
        self.errors = {}
        self.last_error = ''
        self.latest = {}
        self.rates = {'rx': deque(), 'tx': deque()}
        self.max_gap_ms = {'rx': 0.0, 'tx': 0.0}
        self.packets = deque(maxlen=1000)
        self.packet_seq = 0
        self.latest_packets = {}
        self.packet_observed = {}

    def add_packet(self, direction, packet, timestamp=None, crc_ok=None, *, status=None, log_seq=None, log_ns=None):
        # Keep wire bytes even when CRC/semantic checks reject a waveform sample.
        stamp = time.monotonic() if timestamp is None else timestamp
        calculated = crc16(packet[:-2]) if len(packet) >= 2 else None
        received = int.from_bytes(packet[-2:], 'little') if len(packet) >= 2 else None
        valid_crc = calculated == received and calculated is not None
        values, error = None, ''
        try:
            decoded_direction, values = decode_frame(packet)
            if decoded_direction != direction:
                values, error = None, '日志方向与协议长度不符'
        except ProtocolError as exc:
            error = str(exc)
        with self.lock:
            self.packet_seq += 1
            item = dict(seq=self.packet_seq, t=max(0, stamp-self.start), direction=direction,
                        length=len(packet), hex=packet.hex(' ').upper(), crc_ok=valid_crc,
                        crc_received=received, crc_calculated=calculated, values=values,
                        error=error, log_status=status, log_seq=log_seq,
                        log_time_ns=str(log_ns) if log_ns is not None else None)
            self.packets.append(item)
            self.latest_packets[direction] = item
            self.packet_observed[direction] = time.monotonic()
            self.updated.notify_all()

    def packet_snapshot(self, direction='both', limit=30):
        with self.lock:
            items = [p for p in reversed(self.packets)
                     if direction == 'both' or p['direction'] == direction][:limit]
            return dict(packets=items, latest=dict(self.latest_packets),
                        retained=len(self.packets), capacity=self.packets.maxlen,
                        cursor=self.packet_seq, epoch=self.epoch)

    def export_packets(self):
        with self.lock:
            packets = list(self.packets)
        result = bytearray()
        seqs = {'tx': 0, 'rx': 0}
        for packet in packets:
            direction = packet['direction']
            raw = bytes.fromhex(packet['hex'])
            status = packet['log_status']
            if status is None:
                status = 1 | (2 if packet['crc_ok'] else 0)
            seq = packet['log_seq']
            if seq is None:
                seq = seqs[direction]
            seqs[direction] += 1
            result.extend(HEADER.pack(MAGIC, 0 if direction == 'tx' else 1, status, len(raw),
                                      round(packet['t']*1e9), seq, 0))
            result.extend(raw)
        return bytes(result)

    def add(self, direction, values, timestamp=None, *, exact_t=None, log_seq=None, log_ns=None):
        with self.lock:
            if exact_t is not None:
                # Sender-relative time (e.g. t_ns*1e-9 from UDP telemetry) is kept
                # bit-exact: t == exact_t, without the start+x-start float round
                # trip that a large time.monotonic() base would quantize.
                t = max(0.0, float(exact_t))
            else:
                stamp = time.monotonic() if timestamp is None else timestamp
                t = max(0, stamp - self.start)
            self.seq += 1
            previous = self.latest.get(direction)
            if previous:
                self.max_gap_ms[direction] = max(self.max_gap_ms[direction], (t-previous['t'])*1000)
            frame = dict(seq=self.seq, t=t, direction=direction, values=values)
            if log_seq is not None or log_ns is not None:
                # Exact sender metadata from a telemetry record: no float matching.
                frame.update(log_seq=log_seq,
                             log_time_ns=str(log_ns) if log_ns is not None else None)
            else:
                packet = self.latest_packets.get(direction)
                if packet and abs(packet['t']-t) < 1e-9:
                    frame.update(log_seq=packet['log_seq'], log_time_ns=packet['log_time_ns'])
            self.frames.append(frame)
            self.latest[direction] = frame
            self.rates[direction].append(t)
            self._trim_rates(t)
            self.updated.notify_all()

    def add_record(self, direction, payload, *, timestamp=None, exact_t=None, crc_ok=True,
                   values=None, error='', status=None, log_seq=None, log_ns=None):
        """UDP high-rate fast path: one lock writes the raw packet row and the frame.

        The caller already decoded ``payload`` exactly once (CRC check included),
        so nothing is recomputed here. ``values is None`` keeps the raw packet
        inspectable (bad CRC / recorder status / decode error) but never feeds
        the waveform. Only one ``updated`` notify is issued per record, keeping
        the 500 Hz/direction stream cheap; ``add_packet``/``add`` stay unchanged.
        """
        with self.lock:
            stamp = time.monotonic() if timestamp is None else timestamp
            packet_t = max(0, stamp - self.start)
            received = int.from_bytes(payload[-2:], 'little') if len(payload) >= 2 else None
            log_time_ns = None if log_ns is None else str(log_ns)
            self.packet_seq += 1
            item = {'seq': self.packet_seq, 't': packet_t, 'direction': direction,
                    'length': len(payload), 'hex': payload.hex(' ').upper(),
                    'crc_ok': crc_ok, 'crc_received': received,
                    'crc_calculated': received if crc_ok else None,
                    'values': values, 'error': error, 'log_status': status,
                    'log_seq': log_seq, 'log_time_ns': log_time_ns}
            self.packets.append(item)
            self.latest_packets[direction] = item
            self.packet_observed[direction] = time.monotonic()
            if values is not None:
                t = max(0.0, float(exact_t)) if exact_t is not None else packet_t
                self.seq += 1
                previous = self.latest.get(direction)
                if previous:
                    self.max_gap_ms[direction] = max(self.max_gap_ms[direction], (t-previous['t'])*1000)
                frame = {'seq': self.seq, 't': t, 'direction': direction, 'values': values}
                if log_seq is not None or log_ns is not None:
                    frame['log_seq'] = log_seq
                    frame['log_time_ns'] = log_time_ns
                self.frames.append(frame)
                self.latest[direction] = frame
                self.rates[direction].append(t)
                self._trim_rates(t)
            self.updated.notify_all()

    def _trim_rates(self, now):
        for stamps in self.rates.values():
            while stamps and stamps[0] < now-2:
                stamps.popleft()

    def reject(self, reason):
        with self.lock:
            self.rejected += 1
            code = getattr(reason, 'code', 'other')
            self.errors[code] = self.errors.get(code, 0) + 1
            self.last_error = str(reason)

    def snapshot(self, since, now=None):
        with self.lock:
            now = time.monotonic()-self.start if now is None else now
            self._trim_rates(now)
            selected = []
            for frame in reversed(self.frames):
                if frame['seq'] <= since:
                    break
                selected.append(frame)
                if len(selected) == 5000:
                    break
            selected.reverse()
            selected.sort(key=lambda frame: (frame['t'], frame['seq']))
            dropped = max(0, min(f['seq'] for f in selected)-max(since,self.epoch_start_seq)-1) if selected else 0
            tracking = None
            rx, tx = self.latest.get('rx'), self.latest.get('tx')
            if rx and tx and tx['values'].get('mode', 0) and (
                    0 <= now-rx['t'] <= .25 and 0 <= now-tx['t'] <= .25 and abs(rx['t']-tx['t']) <= .05):
                tracking = dict(yaw=math.remainder(tx['values']['yaw']-rx['values']['yaw'], 2*math.pi),
                                pitch=tx['values']['pitch']-rx['values']['pitch'],
                                sample_offset_ms=abs(rx['t']-tx['t'])*1000)
            return dict(frames=selected, cursor=self.seq, now=now, source=self.source, epoch=self.epoch,
                        rejected=self.rejected, errors=dict(self.errors), last_error=self.last_error,
                        latest=dict(self.latest), max_gap_ms=dict(self.max_gap_ms), tracking=tracking,
                        rate={k: len(v)/min(2, max(now, .1)) for k,v in self.rates.items()},
                        retained=len(self.frames), capacity=self.frames.maxlen, dropped=dropped)

    def measurement_frames(self, now, window=10):
        with self.lock:
            return sorted((f for f in self.frames if now-window-.1 <= f['t'] <= now+.001),
                          key=lambda frame: (frame['t'], frame['seq']))

    def export(self):
        with self.lock:
            frames = list(self.frames)
        output = io.StringIO(newline='')
        writer = csv.writer(output)
        writer.writerow(('time_s', 'direction') + FIELDS + ('log_sequence', 'original_log_time_ns'))
        for frame in frames:
            writer.writerow((f"{frame['t']:.9f}", frame['direction']) +
                            tuple(frame['values'].get(k, '') for k in FIELDS) +
                            (frame.get('log_seq', ''), frame.get('log_time_ns', '')))
        return output.getvalue().encode('utf-8-sig')

    def reset(self, source, start=None):
        with self.lock:
            self.start = time.monotonic() if start is None else start
            self.epoch += 1
            self.epoch_start_seq = self.seq
            self.frames.clear()
            self.latest.clear()
            self.packets.clear()
            self.latest_packets.clear()
            self.packet_observed.clear()
            self.source = source
            self.rejected = 0
            self.errors.clear()
            self.last_error = ''
            self.max_gap_ms = {'rx': 0.0, 'tx': 0.0}
            for stamps in self.rates.values():
                stamps.clear()
            self.updated.notify_all()


def demo_loop(store, stop):
    """Exercise the real encoder/decoder without opening any serial port."""
    started = time.monotonic()
    while not stop.is_set():
        t = time.monotonic()-started
        yaw, pitch = .48*math.sin(t*.8), .18*math.sin(t*.55)
        actual_yaw, actual_pitch = .47*math.sin((t-.1)*.8), .175*math.sin((t-.08)*.55)
        cy, sy = math.cos(actual_yaw/2), math.sin(actual_yaw/2)
        cp, sp = math.cos(actual_pitch/2), math.sin(actual_pitch/2)
        tx = TX.pack(b'SP', 1, yaw, .384*math.cos(t*.8), -.3072*math.sin(t*.8),
                     pitch, .099*math.cos(t*.55), -.05445*math.sin(t*.55), 3, 0)
        rx = RX.pack(b'SP', 1, cy*cp, -sy*sp, cy*sp, sy*cp, actual_yaw,
                     .376*math.cos((t-.1)*.8), actual_pitch,
                     .09625*math.cos((t-.08)*.55), 26, 0, 1, 0)
        for packet in (tx, rx):
            packet = packet[:-2]+crc16(packet[:-2]).to_bytes(2, 'little')
            direction, values = decode_frame(packet)
            store.add_packet(direction, packet)
            store.add(direction, values)
        stop.wait(.01)


class Application:
    def __init__(self, demo=False, config=None, diagnostics=None, udp=None,
                 native=None, camera=None, preview_fps=60, push_hz=60, capacity=60000):
        # Push refresh rate: SSE interval only, never the data sampling rate.
        try:
            push_hz = float(push_hz)
            if not math.isfinite(push_hz):
                raise ValueError
        except (TypeError, ValueError):
            push_hz = 60.
        self.push_hz = min(240., max(20., push_hz))
        try:
            capacity = int(capacity)
        except (TypeError, ValueError):
            capacity = 60000
        self.store = Store('演示数据' if demo else '未连接串口', capacity=max(2, capacity))
        self.bridge = SerialBridge(self.store.add, self.store.reject, self.record_raw)
        self.lock = threading.RLock()
        self.mode = 'demo' if demo else 'idle'
        self.recorder = self.replay = None
        self.udp = None
        self.native = native
        self.camera = str(Path(camera).expanduser().resolve()) if camera else None
        try:
            self.preview_fps = max(1, int(preview_fps))
        except (TypeError, ValueError):
            self.preview_fps = 60
        self.last_decode = None
        self.follower = None
        self.config = read_project_config(config) if config else None
        self.upload_dir = None
        self.demo = demo
        self.demo_stop = threading.Event()
        self.latency_lock = threading.Lock()
        self.latency_cache = None
        self.latency_at = 0.
        self.latency_epoch = -1
        self.diagnostic_store = DiagnosticStore()
        self.diagnostic_follower = DiagnosticFollower(diagnostics, self.diagnostic_store) if diagnostics else None
        if demo:
            threading.Thread(target=demo_loop, args=(self.store,self.demo_stop), daemon=True).start()
        if udp:
            # Convenience for the desktop launchers: Application(udp=dict(host=..., port=...)).
            self.start_udp(**udp)

    def record_raw(self, direction, packet, timestamp, crc_ok, **metadata):
        self.store.add_packet(direction, packet, timestamp, crc_ok, **metadata)
        recorder = self.recorder
        if recorder:
            recorder.add(direction, packet, timestamp, crc_ok)

    def start_bridge(self, args):
        with self.lock:
            if self.demo or self.mode in ('replay', 'follow', 'live_udp'):
                raise ValueError('请先停止 UDP 实时接收/离线回放/演示，再连接设备')
            if self.bridge.snapshot()['running']:
                raise ValueError('桥接已运行')
            self.stop()
            self.store.reset('串口 ' + args.get('port', ''))
            record = args.get('record', False)
            if record:
                path = record if isinstance(record, str) and record != 'auto' else (
                    ROOT/'records'/('serial_'+time.strftime('%Y%m%d_%H%M%S')+f'_{time.time_ns()%1000000:06d}.bin'))
                self.recorder = Recorder(path)
            try:
                result = self.bridge.start(args.get('port', ''), int(args.get('baud',921600)),
                                           args.get('virtual',''), args.get('link',''))
                self.mode = 'live'
                return result
            except Exception:
                if self.recorder:
                    self.recorder.close()
                raise

    def start_replay(self, path, upload_dir=None, name=None):
        with self.lock:
            if self.demo or self.mode == 'live_udp' or self.bridge.snapshot()['running']:
                raise ValueError('请先停止 UDP 实时接收/实时桥接；演示模式请重新启动后加载文件')
            # Validate the new file before discarding the current replay.
            candidate = Replay(path, self.store.add, self.store.reject, 0, self.record_raw)
            self.stop()
            self.store.reset('离线回放 · '+(name or Path(path).name))
            candidate.base_time = self.store.start
            self.replay, self.upload_dir = candidate, upload_dir
            self.mode = 'replay'
            candidate.control('play')
            return candidate.snapshot()

    def start_follow(self, path):
        with self.lock:
            if self.demo or self.mode == 'live_udp' or self.bridge.snapshot()['running']:
                raise ValueError('请先停止 UDP 实时接收/桥接或退出演示')
            self.stop()
            self.store.reset('只读监听 · 等待日志')
            self.mode = 'follow'
            self.recorder = None
            self.follower = LogFollower(path, self.store.add, self.store.reject,
                lambda name, start: self.store.reset('只读监听 · '+Path(name).name, start),
                self.record_raw)
            return self.follower.snapshot()

    def start_udp(self, host='0.0.0.0', port=9871, source_ip=None):
        """Low-latency live source: decode SVL1 datagrams straight into the store."""
        with self.lock:
            if self.demo or self.mode in ('replay', 'follow') or self.bridge.snapshot()['running']:
                raise ValueError('请先退出演示/桥接/回放/只读监听，再启动 UDP 实时接收')
            port = int(port)
            self.stop()
            self.recorder = None
            self.store.reset(f'UDP 实时 · {host}:{port}')
            self.mode = 'live_udp'
            try:
                self.udp = UdpSource(self.store, host=host, port=port, source_ip=source_ip)
            except Exception:
                self.mode = 'idle'
                raise
            return self.udp.snapshot()

    def replay_control(self, action, speed=None):
        with self.lock:
            if not self.replay:
                raise ValueError('尚未加载日志')
            if action == 'restart':
                with self.replay.condition:
                    self.replay.control('pause')
                    self.store.reset(self.store.source)
                    self.replay.base_time = self.store.start
                    self.replay.control('restart')
            else:
                self.replay.control(action, speed)
            return self.replay.snapshot()

    def stop(self):
        with self.lock:
            self.bridge.stop()
            recorder = self.recorder
            if self.udp:
                # Detach the recorder before the receive thread stops, then flush it
                # explicitly: closing the window during a recording must not leak a
                # Recorder thread or an unflushed .bin file.
                self.udp.set_recorder(None)
                self.udp.close()
                self.udp = None
            self.recorder = None
            if self.follower:
                self.follower.close()
                self.follower = None
            if recorder:
                recorder.close()
            if self.replay:
                self.replay.close()
                self.replay = None
            if self.upload_dir:
                self.upload_dir.cleanup()
                self.upload_dir = None
            if not self.demo:
                self.mode = 'idle'

    def _timeline_now(self):
        """Current position on the same time axis as the stored frame ``t`` values.

        Replay uses its playback position; UDP uses the sender's SerialRecorder
        clock (a remote vision process may be hours ahead of local monotonic
        time); every other mode keeps the historical monotonic behaviour.
        """
        if self.replay:
            return self.replay.snapshot()['position']
        if self.udp is not None:
            return self.udp.now()
        return time.monotonic() - self.store.start

    def snapshot(self, since):
        with self.lock:
            replay = self.replay.snapshot() if self.replay else None
            data = self.store.snapshot(since, replay['position'] if replay else self._timeline_now())
            data.update(bridge=self.bridge.snapshot(), demo=self.demo, mode=self.mode,
                        replay=replay, recording=self.recorder.snapshot() if self.recorder else None,
                        follow=self.follower.snapshot() if self.follower else None,
                        config=self.config,
                        protocol=dict(tx_bytes=TX.size, rx_bytes=RX.size),
                        udp=self.udp.snapshot() if self.udp else None,
                        camera=self.camera_snapshot(), push_hz=self.push_hz)
            return data

    def stream_event(self, since, timeout=0.05):
        """Wait up to ``timeout`` for a store change, then return the current snapshot."""
        timeout = max(0.0, min(1.0, float(timeout)))
        with self.store.updated:
            self.store.updated.wait(timeout)
        return self.snapshot(since)

    def packet_snapshot(self, direction, limit):
        with self.lock:
            result = self.store.packet_snapshot(direction, limit)
            result.update(mode=self.mode, now=self._timeline_now())
            source = self._raw_log_source()
            result['download'] = dict(scope='log' if source else 'cache',
                                      available=bool(source or result['retained']),
                                      bytes=source[1] if source else None)
            return result

    def _raw_log_source(self):
        if self.follower:
            state = self.follower.snapshot()
            if state['current'] and state['complete_bytes']:
                return state['current'], state['complete_bytes']
        elif self.replay:
            return self.replay.path, self.replay.complete_bytes
        elif self.recorder:
            state = self.recorder.snapshot()
            if state['flushed_bytes']:
                return state['path'], state['flushed_bytes']
        return None

    def open_packet_download(self):
        with self.lock:
            source = self._raw_log_source()
            if source:
                path, length = source
                stream = open(path, 'rb')
                if stream.seek(0, 2) < length:
                    stream.close()
                    raise ValueError('日志已截断，请刷新页面后重试')
                stream.seek(0)
                return stream, length, Path(path).name, 'log'
            data = self.store.export_packets()
            if not data:
                raise ValueError('尚无数据包可保存')
            return io.BytesIO(data), len(data), 'serval-cached-packets.bin', 'cache'

    # -- camera preview (latest JPEG written by the vision process) ---------
    def camera_available(self):
        if not self.camera:
            return False
        try:
            return Path(self.camera).stat().st_size > 0
        except OSError:
            return False

    def camera_snapshot(self):
        return dict(available=self.camera_available(), fps=self.preview_fps)

    def camera_frame(self):
        """Return (JPEG bytes, change signature) or None when the preview is absent."""
        if not self.camera:
            return None
        path = Path(self.camera)
        try:
            stat = path.stat()
            if stat.st_size <= 0:
                return None
            return path.read_bytes(), (stat.st_mtime_ns, stat.st_size)
        except OSError:
            return None

    # -- native quick actions (save / record / offline decode / open) ------
    def _auto_path(self, prefix):
        stamp = time.strftime('%Y%m%d_%H%M%S')
        unique = time.time_ns() % 1000000
        return ROOT / 'records' / 'serial' / f'{prefix}_{stamp}_{unique:06d}.bin'

    def save_cache(self, path=None):
        """Write the cached raw UDP records to a new .bin (never overwrite)."""
        if not self.udp:
            raise ValueError('当前模式没有 UDP 原始缓存可保存')
        target = (Path(path).expanduser() if path else self._auto_path('serial')).resolve()
        count = self.udp.save(target)
        return {'path': str(target), 'count': count}

    def start_recording(self, path=None):
        with self.lock:
            if self.recorder is not None and self.recorder.snapshot()['active']:
                raise ValueError('已经在录制')
            target = (Path(path).expanduser() if path else self._auto_path('serial_record')).resolve()
            recorder = Recorder(target)
            previous = self.recorder
            self.recorder = recorder
            if self.udp:
                self.udp.set_recorder(recorder)
            if previous is not None:
                previous.close()
            return recorder.snapshot()

    def stop_recording(self):
        with self.lock:
            recorder = self.recorder
            if recorder is None:
                return None
            if self.udp:
                self.udp.set_recorder(None)
            recorder.stop()
            # close() joins the writer thread and closes the file, so the returned
            # snapshot already reports finished=True with the final flushed size.
            recorder.close()
            return recorder.snapshot()

    def recording_snapshot(self):
        return self.recorder.snapshot() if self.recorder else None

    def run_offline_decode(self, path, plot=True, out_dir=None):
        """Run save/decode_serial_log.py in a child process and keep the result."""
        if not isinstance(path, str) or not path.strip():
            raise ValueError('请提供 .bin 原始日志路径')
        source = Path(path).expanduser()
        if not source.is_file():
            raise ValueError(f'日志文件不存在：{source}')
        if source.suffix.lower() != '.bin':
            raise ValueError('离线解码只接受 .bin 原始记录文件')
        source = source.resolve()
        output = Path(out_dir).expanduser().resolve() if out_dir else source.with_suffix('')
        command = [sys.executable, '-B', '-u', str(ROOT/'save'/'decode_serial_log.py'),
                   str(source), '--out-dir', str(output)]
        if plot:
            command.append('--plot')
        try:
            completed = subprocess.run(command, capture_output=True, text=True,
                                       encoding='utf-8', errors='replace', timeout=300,
                                       cwd=str(ROOT))
        except (OSError, subprocess.SubprocessError) as exc:
            result = dict(path=str(source), output_dir=str(output), plot=bool(plot),
                          returncode=-1, output='', error=str(exc))
            self.last_decode = result
            return result
        lines = [line for line in ((completed.stdout or '') + (completed.stderr or '')).splitlines()
                 if line.strip()]
        result = dict(path=str(source), output_dir=str(output), plot=bool(plot),
                      returncode=completed.returncode,
                      output='\n'.join(lines[-20:]),
                      error='' if completed.returncode == 0 else ('\n'.join(lines[-12:]) or '离线解码失败'))
        self.last_decode = result
        return result

    def open_directory(self, path):
        """Fallback for browser/no-Qt mode: let the OS file manager open a path."""
        if not isinstance(path, (str, os.PathLike)) or not str(path).strip():
            raise ValueError('请提供要打开的目录路径')
        target = Path(path).expanduser()
        if not target.exists():
            raise ValueError(f'路径不存在：{target}')
        target = target.resolve()
        try:
            if os.name == 'nt':
                os.startfile(str(target))
            elif sys.platform == 'darwin':
                subprocess.Popen(['open', str(target)])
            else:
                subprocess.Popen(['xdg-open', str(target)])
        except OSError as exc:
            raise ValueError(f'无法打开目录：{exc}')
        return {'ok': True}

    def native_status(self):
        return dict(available=self.native is not None,
                    recording=self.recording_snapshot(),
                    last_decode=self.last_decode,
                    mode=self.mode,
                    capabilities=['save', 'record', 'decode', 'open'] if self.native is not None else [])

    def handle_native(self, action, payload):
        """Dispatch a local shortcut action; a desktop NativeHost wins when present."""
        payload = payload if isinstance(payload, dict) else {}
        if self.native is not None:
            return self.native.handle(action, payload)
        if action == 'save':
            return self.save_cache(payload.get('path'))
        if action == 'record-start':
            return self.start_recording(payload.get('path'))
        if action == 'record-stop':
            return self.stop_recording()
        if action == 'decode':
            return self.run_offline_decode(payload.get('path'), bool(payload.get('plot', True)),
                                           payload.get('out_dir'))
        if action == 'open':
            return self.open_directory(payload.get('path'))
        if action == 'status':
            return self.native_status()
        raise ValueError(f'未知的本地快捷操作：{action}')

    def latency_snapshot(self):
        clock = time.monotonic()
        with self.lock:
            mode = self.mode
            now = self._timeline_now()
        with self.latency_lock:
            with self.store.lock:
                epoch = self.store.epoch
                ages = {d: max(0., (clock-self.store.packet_observed[d])*1000)
                        if d in self.store.packet_observed else None for d in ('tx', 'rx')}
                if self.latency_cache is None or epoch != self.latency_epoch or clock-self.latency_at >= 1:
                    frames = [f for f in self.store.frames if now-6.5 <= f['t'] <= now+.1]
                else:
                    frames = None
            if frames is not None:
                frames.sort(key=lambda f:f['t'])
                self.latency_cache = estimate_follow_delay(frames, now)
                self.latency_at, self.latency_epoch = clock, epoch
            baud = self.bridge.baud if mode == 'live' else 921600
            return dict(mode=mode, following=self.latency_cache, observed_age_ms=ages,
                        wire_time_ms=dict(tx=TX.size*10*1000/baud, rx=RX.size*10*1000/baud),
                        baud=baud, note='跟随滞后为估计；当前协议无法测量准确串口 RTT')

    def analysis_snapshot(self, start=None, end=None):
        with self.lock:
            now = self._timeline_now()
        end = now if end is None else end
        start = max(0., end-10) if start is None else start
        if not all(math.isfinite(t) for t in (start,end)) or start < 0 or end <= start or end-start > 120:
            raise ValueError('分析窗口必须是有效的 0–120 秒区间')
        frames = self.store.measurement_frames(end, end-start)
        result = response_summary(frames, start, end)
        result['step'] = {axis:step_response(frames,start,end,axis) for axis in ('yaw','pitch')}
        result.update(epoch=self.store.epoch, source=self.store.source,
                      oldest_available=min((f['t'] for f in frames), default=None))
        return result

    def close(self):
        self.demo_stop.set()
        self.stop()
        if self.diagnostic_follower:
            self.diagnostic_follower.close()

    def diagnostic_snapshot(self):
        state = self.diagnostic_store.snapshot()
        # Freshness must follow the sender clock in live_udp mode (remote vision).
        now = self.udp.now() if self.udp is not None else time.monotonic()-self.store.start
        with self.store.lock:
            wire = {d:dict(values=f['values'], age_s=max(0., now-f['t']))
                    for d,f in self.store.latest.items()}
        follower = self.diagnostic_follower
        state.update(path=str(follower.path) if follower else None,
                     error=follower.error if follower else '',
                     mode=self.mode, wire=wire)
        if self.mode in ('demo', 'replay'):
            state['explanation'] = dict(code='offline', title='当前是演示或回放',
                                       detail='诊断日志与串口回放独立，不能据此判断实际云台控制。')
        else:
            state['explanation'] = explain_control(state, wire)
        # A fresh SVF1 FPS datagram is the authoritative visual rate in UDP mode;
        # only fill it in when the followed log has no fresh FPS line.
        udp = self.udp.snapshot() if self.udp else None
        if udp and udp.get('fps') is not None:
            current = state['latest'].get('fps')
            if not current or current.get('age_s', math.inf) > 3:
                state['latest']['fps'] = dict(kind='fps', fields={'program': 'UDP', 'fps': udp['fps']},
                                              text='', age_s=float(udp.get('fps_age') or 0.))
        return state


class ServalHTTPServer(ThreadingHTTPServer):
    """HTTP/1.1 server with keep-alive; non-SSE responses always carry Content-Length."""
    daemon_threads = True
    protocol_version = 'HTTP/1.1'

    def handle_error(self, request, client_address):
        # A page closing an SSE/MJPEG stream mid-write is normal, not an error.
        if isinstance(sys.exc_info()[1], (BrokenPipeError, ConnectionResetError, ConnectionAbortedError)):
            return
        super().handle_error(request, client_address)


NATIVE_ACTIONS = {
    '/api/native/save': 'save',
    '/api/native/record/start': 'record-start',
    '/api/native/record/stop': 'record-stop',
    '/api/native/decode': 'decode',
    '/api/native/open': 'open',
}


def create_server(app, host='127.0.0.1', port=8765):
    return ServalHTTPServer((host, port), make_handler(app))


def make_handler(app):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def respond(self, body, mime='application/json; charset=utf-8', status=200, extra=None):
            if not isinstance(body, bytes):
                body = json.dumps(body, allow_nan=False).encode()
            self.send_response(status)
            self.send_header('Content-Type', mime)
            self.send_header('Content-Length', str(len(body)))
            self.send_header('Cache-Control', 'no-store')
            self.send_header('X-Content-Type-Options', 'nosniff')
            for key, value in (extra or {}).items():
                self.send_header(key,value)
            self.end_headers()
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass

        # -- streaming responses (SSE / MJPEG) --------------------------------
        def write_chunk(self, data):
            self.wfile.write(f'{len(data):X}\r\n'.encode() + data + b'\r\n')

        def stream_events(self, query):
            try:
                since = max(0, int(parse_qs(query).get('since', ['0'])[0]))
            except ValueError:
                return self.respond({'error': '无效游标'}, status=400)
            # SSE has no Content-Length; chunked framing carries every event whole
            # and the stream ends only when the client disconnects.
            self.send_response(200)
            self.send_header('Content-Type', 'text/event-stream; charset=utf-8')
            self.send_header('Cache-Control', 'no-cache')
            self.send_header('X-Accel-Buffering', 'no')
            self.send_header('Transfer-Encoding', 'chunked')
            self.end_headers()
            self.close_connection = True
            # Push rate only (20-240 Hz); data sampling is untouched. The desktop
            # launcher may set app.push_hz from the screen refresh rate.
            interval = 1. / max(20., min(240., float(getattr(app, 'push_hz', 60.))))
            with app.store.lock:
                epoch = app.store.epoch
                backlog = app.store.seq != since
            sent_at = 0.
            try:
                self.write_chunk(b'retry: 500\n\n')
                state = app.snapshot(since) if backlog else None
                while True:
                    if state is None:
                        # Wait only until the next allowed push time. A change that
                        # arrives earlier wakes the wait immediately and is merged
                        # below instead of sleeping a full interval first.
                        now = time.monotonic()
                        wait = interval if not sent_at else max(
                            .001, min(interval, sent_at + interval - now))
                        state = app.stream_event(since, wait)
                    changed = state['cursor'] != since or state.get('epoch') != epoch
                    if changed:
                        # Cap merged pushes at ~60/s; sleeping here (rather than
                        # after every send) removes one fixed interval of latency.
                        delay = sent_at + interval - time.monotonic() if sent_at else 0.
                        if delay > 0:
                            time.sleep(delay)
                            state = app.snapshot(since)
                            changed = state['cursor'] != since or state.get('epoch') != epoch
                        if changed:
                            since, epoch = state['cursor'], state.get('epoch')
                            payload = json.dumps(state, allow_nan=False).encode()
                            self.write_chunk(b'id: ' + str(since).encode() +
                                             b'\nevent: data\ndata: ' + payload + b'\n\n')
                            sent_at = time.monotonic()
                        state = None
                        continue
                    if time.monotonic() - sent_at >= 1.:
                        self.write_chunk(b': keepalive\n\n')
                        sent_at = time.monotonic()
                    state = None
            except (BrokenPipeError, ConnectionResetError, OSError):
                return

        def camera_stream(self):
            if app.camera_frame() is None:
                return self.respond({'error': '没有可用的相机预览'}, status=404)
            self.send_response(200)
            self.send_header('Content-Type', 'multipart/x-mixed-replace; boundary=frame')
            self.send_header('Cache-Control', 'no-store')
            self.end_headers()
            self.close_connection = True
            interval = 1. / max(1, app.preview_fps)
            try:
                while True:
                    started = time.monotonic()
                    frame = app.camera_frame()
                    if frame is None:
                        return  # preview disappeared: end the multipart stream
                    data = frame[0]
                    self.wfile.write(b'--frame\r\nContent-Type: image/jpeg\r\nContent-Length: ' +
                                     str(len(data)).encode() + b'\r\n\r\n' + data + b'\r\n')
                    self.wfile.flush()
                    delay = interval - (time.monotonic() - started)
                    if delay > 0:
                        time.sleep(delay)
            except (BrokenPipeError, ConnectionResetError, OSError):
                return

        def native_action(self, path):
            action = NATIVE_ACTIONS.get(path)
            if action is None:
                return self.send_error(404)
            try:
                length = int(self.headers.get('Content-Length', '0'))
                if not 0 <= length <= 1024 * 1024:
                    raise ValueError('请求过长')
                payload = {}
                if length:
                    if not self.headers.get('Content-Type', '').startswith('application/json'):
                        self.close_connection = True
                        return self.respond({'error': '需要 application/json'}, status=415)
                    payload = json.loads(self.rfile.read(length))
                    if not isinstance(payload, dict):
                        raise ValueError('请求必须是对象')
                result = app.handle_native(action, payload)
            except FileExistsError as exc:
                return self.respond({'error': str(exc)}, status=409)
            except (ValueError, OSError, ImportError, RuntimeError, TypeError) as exc:
                return self.respond({'error': str(exc)}, status=400)
            if isinstance(result, dict) and result.get('cancelled'):
                return self.respond(result)
            if action in ('record-start', 'record-stop'):
                if isinstance(result, dict) and 'recording' in result:
                    return self.respond(result)
                return self.respond({'recording': result})
            if result is None:
                return self.respond({})
            return self.respond(result)

        def do_GET(self):
            parsed = urlparse(self.path)
            if parsed.path == '/api/stream':
                return self.stream_events(parsed.query)
            if parsed.path == '/api/native/status':
                return self.respond(app.native_status())
            if parsed.path == '/api/camera.mjpg':
                return self.camera_stream()
            if parsed.path == '/api/camera.jpg':
                frame = app.camera_frame()
                if frame is None:
                    return self.respond({'error': '没有可用的相机预览'}, status=404)
                return self.respond(frame[0], 'image/jpeg')
            if parsed.path == '/api/data':
                try:
                    since = max(0, int(parse_qs(parsed.query).get('since',['0'])[0]))
                except ValueError:
                    return self.respond({'error':'无效游标'},status=400)
                return self.respond(app.snapshot(since))
            if parsed.path == '/api/ports':
                return self.respond(app.bridge.list_ports())
            if parsed.path == '/api/latency':
                return self.respond(app.latency_snapshot())
            if parsed.path == '/api/analysis':
                query = parse_qs(parsed.query)
                try:
                    start = float(query['start'][0]) if 'start' in query else None
                    end = float(query['end'][0]) if 'end' in query else None
                    return self.respond(app.analysis_snapshot(start,end))
                except ValueError as exc:
                    return self.respond({'error':str(exc)},status=400)
            if parsed.path == '/api/diagnostics':
                return self.respond(app.diagnostic_snapshot())
            if parsed.path == '/api/diagnostics.txt':
                return self.respond(app.diagnostic_store.export(), 'text/plain; charset=utf-8',
                                    extra={'Content-Disposition':'attachment; filename="aim-diagnostics.txt"'})
            if parsed.path == '/api/export.bin':
                try:
                    stream, length, name, scope = app.open_packet_download()
                except (OSError, ValueError) as exc:
                    return self.respond({'error':str(exc)}, status=409)
                with stream:
                    self.send_response(200)
                    self.send_header('Content-Type', 'application/octet-stream')
                    self.send_header('Content-Length', str(length))
                    self.send_header('Content-Disposition', "attachment; filename=serval-packets.bin; filename*=UTF-8''"+quote(name))
                    self.send_header('Cache-Control', 'no-store')
                    self.send_header('X-Serval-Export-Scope', scope)
                    self.end_headers()
                    try:
                        remaining = length
                        while remaining:
                            chunk = stream.read(min(65536, remaining))
                            if not chunk:
                                break
                            self.wfile.write(chunk)
                            remaining -= len(chunk)
                    except (BrokenPipeError, ConnectionResetError):
                        pass
                return
            if parsed.path == '/api/packets':
                query = parse_qs(parsed.query)
                direction = query.get('direction', ['both'])[0]
                try:
                    limit = int(query.get('limit', ['30'])[0])
                    if direction not in ('both', 'tx', 'rx') or not 1 <= limit <= 100:
                        raise ValueError()
                except ValueError:
                    return self.respond({'error':'无效数据包筛选参数'}, status=400)
                return self.respond(app.packet_snapshot(direction, limit))
            if parsed.path == '/api/export.csv':
                return self.respond(app.store.export(), 'text/csv; charset=utf-8',
                    extra={'Content-Disposition':'attachment; filename="serval-telemetry.csv"'})
            if parsed.path in ('/','/index.html','/app.js','/signals.js','/native.js','/style.css'):
                name = 'index.html' if parsed.path == '/' else parsed.path[1:]
                mime = {'html':'text/html','js':'text/javascript','css':'text/css'}[name.rsplit('.',1)[1]]
                return self.respond((WEB/name).read_bytes(),mime+'; charset=utf-8')
            # Extra page assets: the frontend owns web/, so serve plain filenames
            # from it without requiring a backend change (and without traversal).
            name = parsed.path[1:]
            if (re.fullmatch(r'[A-Za-z0-9_-]+\.(?:js|css|html|png|svg|ico|jpg|jpeg|woff2?|map)', name)
                    and (WEB/name).is_file()):
                mime = {'html':'text/html','js':'text/javascript','css':'text/css',
                        'png':'image/png','svg':'image/svg+xml','ico':'image/x-icon',
                        'jpg':'image/jpeg','jpeg':'image/jpeg','woff':'font/woff',
                        'woff2':'font/woff2','map':'application/json'}[name.rsplit('.',1)[1]]
                return self.respond((WEB/name).read_bytes(), mime+'; charset=utf-8')
            self.send_error(404)

        def do_POST(self):
            origin = self.headers.get('Origin')
            if origin and origin != 'http://'+self.headers.get('Host',''):
                self.close_connection = True
                return self.respond({'error':'拒绝跨站串口操作'},status=403)
            path = urlparse(self.path).path
            if path in NATIVE_ACTIONS:
                return self.native_action(path)
            if app.demo:
                self.close_connection = True
                return self.respond({'error':'演示模式不连接串口或载入其他数据'},status=409)
            upload = None
            try:
                length = int(self.headers.get('Content-Length','0'))
                if self.path == '/api/replay/file':
                    if not 1 <= length <= 128*1024*1024:
                        self.close_connection = True
                        raise ValueError('请选择 128 MB 以内的原版 RMSE .bin 日志；大文件可用 --replay')
                    if app.bridge.snapshot()['running']:
                        raise ValueError('请先停止实时桥接')
                    upload = tempfile.TemporaryDirectory(prefix='serval-replay-')
                    path = Path(upload.name)/'capture.bin'
                    remaining = length
                    with path.open('wb') as output:
                        while remaining:
                            chunk = self.rfile.read(min(65536,remaining))
                            if not chunk:
                                raise ValueError('日志上传不完整')
                            output.write(chunk)
                            remaining -= len(chunk)
                    result = app.start_replay(path,upload,unquote(self.headers.get('X-File-Name','capture.bin')))
                    upload = None
                    return self.respond(result)
                if not self.headers.get('Content-Type','').startswith('application/json'):
                    self.close_connection = True
                    return self.respond({'error':'需要 application/json'},status=415)
                if not 0 <= length <= 4096:
                    self.close_connection = True
                    raise ValueError('请求过长')
                args = json.loads(self.rfile.read(length))
                if not isinstance(args,dict):
                    raise ValueError('请求必须是对象')
                if self.path == '/api/start':
                    return self.respond(app.start_bridge(args))
                if self.path == '/api/follow':
                    if not isinstance(args.get('path'), str) or not args['path'].strip():
                        raise ValueError('请填写串口日志目录或文件')
                    return self.respond(app.start_follow(args['path']))
                if self.path == '/api/stop':
                    app.stop()
                    return self.respond(app.bridge.snapshot())
                if self.path == '/api/replay/control':
                    return self.respond(app.replay_control(args.get('action'),args.get('speed')))
                self.send_error(404)
            except FileExistsError as exc:
                self.respond({'error':str(exc)},status=409)
            except (ValueError,OSError,ImportError,RuntimeError,TypeError) as exc:
                self.respond({'error':str(exc)},status=400)
            finally:
                if upload:
                    upload.cleanup()
    return Handler


def main():
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, 'reconfigure'):
            stream.reconfigure(encoding='utf-8')
    parser = argparse.ArgumentParser(description=__doc__)
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument('--demo',action='store_true',help='仅显示模拟数据')
    modes.add_argument('--bridge',metavar='PORT',help='桥接云台物理串口')
    modes.add_argument('--replay',metavar='FILE',help='离线播放原工程 RMSE .bin 日志，不打开串口')
    modes.add_argument('--follow',metavar='PATH',help='只读监听原工程正在写入的串口日志目录或 .bin，不打开串口')
    modes.add_argument('--udp',metavar='PORT',type=int,help='低延迟接收 SVL1/SVF1 UDP 遥测（浏览器调试用）')
    parser.add_argument('--source-ip',metavar='IP',default=None,help='只接受该 IPv4 地址的 UDP 数据')
    parser.add_argument('--camera',metavar='PATH',default=None,help='视觉进程写出的最新相机 JPEG，MJPEG 预览用')
    parser.add_argument('--config',metavar='YAML',help='只读加载配置颜色和端口供对照，不改变控制程序')
    parser.add_argument('--diagnostics',metavar='LOG',help='只读解析自瞄日志中的 AimTrace/ControlTrace')
    parser.add_argument('--virtual-port',default='',help='Windows 虚拟串口对的工具端')
    parser.add_argument('--link',default='',help='Linux 固定虚拟串口路径，默认 /tmp/serval-gimbal')
    parser.add_argument('--record',nargs='?',const='auto',default=False,metavar='FILE',
                        help='记录原始帧；省略文件名则保存到 serval/records/')
    parser.add_argument('--baud',type=int,default=921600)
    parser.add_argument('--push-hz',type=float,default=60.,metavar='HZ',
                        help='SSE 推送刷新率，夹在 20–240 Hz（默认 60；数据采样率不变）')
    parser.add_argument('--capacity',type=int,default=60000,metavar='N',
                        help='波形缓存帧数（默认 60000；原始包缓存固定 1000）')
    parser.add_argument('--http-port',type=int,default=8765)
    parser.add_argument('--host',default='127.0.0.1',help='监听地址；局域网访问使用 0.0.0.0')
    parser.add_argument('--open',action='store_true')
    args = parser.parse_args()
    if args.record and not args.bridge:
        parser.error('--record 需要同时指定 --bridge')
    if args.source_ip and not args.udp:
        parser.error('--source-ip 需要同时指定 --udp')
    try:
        app = Application(args.demo, args.config, args.diagnostics, camera=args.camera,
                          push_hz=args.push_hz, capacity=args.capacity)
    except (ValueError, OSError) as exc:
        parser.exit(1, f'无法读取配置：{exc}\n')
    httpd = None
    try:
        httpd = create_server(app, args.host, args.http_port)
        if args.bridge:
            state = app.start_bridge(dict(port=args.bridge,baud=args.baud,virtual=args.virtual_port,
                                          link=args.link,record=args.record))
            print(f"Serial bridge: {args.bridge} <-> {state['app_port']}",flush=True)
        elif args.replay:
            app.start_replay(args.replay)
        elif args.follow:
            app.start_follow(args.follow)
        elif args.udp:
            state = app.start_udp('0.0.0.0', args.udp, args.source_ip)
            print(f"UDP telemetry: listening on 0.0.0.0:{state['port']}", flush=True)
        url = f'http://127.0.0.1:{args.http_port}'
        print(f'Serval | {app.store.source}\nOpen {url}\nCtrl+C to exit.',flush=True)
        if args.open:
            webbrowser.open(url)
        httpd.serve_forever(poll_interval=.2)
    except (OSError,ValueError,ImportError,RuntimeError) as exc:
        parser.exit(1,f'无法启动：{exc}\n')
    except KeyboardInterrupt:
        pass
    finally:
        app.close()
        if httpd:
            httpd.server_close()


if __name__ == '__main__':
    main()
