#!/usr/bin/env python3
"""Serval: OrinNX 30/44-byte serial monitoring, RMSE recording and offline replay."""
import argparse
import csv
import io
import json
import math
import sys
import tempfile
import threading
import time
import webbrowser
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, unquote, urlparse

from protocol import FIELDS, RX, TX, ProtocolError, crc16, decode_frame
from recording import HEADER, MAGIC, Recorder, Replay
from serial_io import SerialBridge
from passive import LogFollower, read_project_config
from latency import estimate_follow_delay
from diagnostics import DiagnosticFollower, DiagnosticStore, explain_control
from analysis import response_summary, step_response

ROOT = Path(__file__).resolve().parent


class Store:
    def __init__(self, source, capacity=60000):
        self.source = source
        self.frames = deque(maxlen=capacity)
        self.lock = threading.RLock()
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

    def add(self, direction, values, timestamp=None):
        with self.lock:
            stamp = time.monotonic() if timestamp is None else timestamp
            t = max(0, stamp - self.start)
            self.seq += 1
            previous = self.latest.get(direction)
            if previous:
                self.max_gap_ms[direction] = max(self.max_gap_ms[direction], (t-previous['t'])*1000)
            frame = dict(seq=self.seq, t=t, direction=direction, values=values)
            packet = self.latest_packets.get(direction)
            if packet and abs(packet['t']-t) < 1e-9:
                frame.update(log_seq=packet['log_seq'], log_time_ns=packet['log_time_ns'])
            self.frames.append(frame)
            self.latest[direction] = frame
            self.rates[direction].append(t)
            self._trim_rates(t)

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
    def __init__(self, demo=False, config=None, diagnostics=None):
        self.store = Store('演示数据' if demo else '未连接串口')
        self.bridge = SerialBridge(self.store.add, self.store.reject, self.record_raw)
        self.lock = threading.RLock()
        self.mode = 'demo' if demo else 'idle'
        self.recorder = self.replay = None
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

    def record_raw(self, direction, packet, timestamp, crc_ok, **metadata):
        self.store.add_packet(direction, packet, timestamp, crc_ok, **metadata)
        recorder = self.recorder
        if recorder:
            recorder.add(direction, packet, timestamp, crc_ok)

    def start_bridge(self, args):
        with self.lock:
            if self.demo or self.mode in ('replay', 'follow'):
                raise ValueError('请先退出演示或离线回放，再连接设备')
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
            if self.demo or self.bridge.snapshot()['running']:
                raise ValueError('请先停止实时桥接；演示模式请重新启动后加载文件')
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
            if self.demo or self.bridge.snapshot()['running']:
                raise ValueError('请先停止桥接或退出演示')
            self.stop()
            self.store.reset('只读监听 · 等待日志')
            self.mode = 'follow'
            self.recorder = None
            self.follower = LogFollower(path, self.store.add, self.store.reject,
                lambda name, start: self.store.reset('只读监听 · '+Path(name).name, start),
                self.record_raw)
            return self.follower.snapshot()

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
            if self.follower:
                self.follower.close()
                self.follower = None
            if self.recorder:
                self.recorder.close()
            if self.replay:
                self.replay.close()
                self.replay = None
            if self.upload_dir:
                self.upload_dir.cleanup()
                self.upload_dir = None
            if not self.demo:
                self.mode = 'idle'

    def snapshot(self, since):
        with self.lock:
            replay = self.replay.snapshot() if self.replay else None
            data = self.store.snapshot(since, replay['position'] if replay else None)
            data.update(bridge=self.bridge.snapshot(), demo=self.demo, mode=self.mode,
                        replay=replay, recording=self.recorder.snapshot() if self.recorder else None,
                        follow=self.follower.snapshot() if self.follower else None,
                        config=self.config,
                        protocol=dict(tx_bytes=TX.size, rx_bytes=RX.size))
            return data

    def packet_snapshot(self, direction, limit):
        with self.lock:
            result = self.store.packet_snapshot(direction, limit)
            result.update(mode=self.mode, now=self.replay.snapshot()['position']
                          if self.replay else time.monotonic()-self.store.start)
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

    def latency_snapshot(self):
        clock = time.monotonic()
        with self.lock:
            mode = self.mode
            now = self.replay.snapshot()['position'] if self.replay else clock-self.store.start
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
            now = self.replay.snapshot()['position'] if self.replay else time.monotonic()-self.store.start
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
        with self.store.lock:
            now = time.monotonic()-self.store.start
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
        return state


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

        def do_GET(self):
            parsed = urlparse(self.path)
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
            if parsed.path in ('/','/index.html','/app.js','/signals.js','/packets.js','/latency.js','/diagnostics.js','/style.css'):
                name = 'index.html' if parsed.path == '/' else parsed.path[1:]
                mime = {'html':'text/html','js':'text/javascript','css':'text/css'}[name.rsplit('.',1)[1]]
                return self.respond((ROOT/name).read_bytes(),mime+'; charset=utf-8')
            self.send_error(404)

        def do_POST(self):
            origin = self.headers.get('Origin')
            if origin and origin != 'http://'+self.headers.get('Host',''):
                return self.respond({'error':'拒绝跨站串口操作'},status=403)
            if app.demo:
                return self.respond({'error':'演示模式不连接串口或载入其他数据'},status=409)
            upload = None
            try:
                length = int(self.headers.get('Content-Length','0'))
                if self.path == '/api/replay/file':
                    if not 1 <= length <= 128*1024*1024:
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
                    return self.respond({'error':'需要 application/json'},status=415)
                if not 0 <= length <= 4096:
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
    parser.add_argument('--config',metavar='YAML',help='只读加载配置颜色和端口供对照，不改变控制程序')
    parser.add_argument('--diagnostics',metavar='LOG',help='只读解析自瞄日志中的 AimTrace/ControlTrace')
    parser.add_argument('--virtual-port',default='',help='Windows 虚拟串口对的工具端')
    parser.add_argument('--link',default='',help='Linux 固定虚拟串口路径，默认 /tmp/serval-gimbal')
    parser.add_argument('--record',nargs='?',const='auto',default=False,metavar='FILE',
                        help='记录原始帧；省略文件名则保存到 serval/records/')
    parser.add_argument('--baud',type=int,default=921600)
    parser.add_argument('--http-port',type=int,default=8765)
    parser.add_argument('--host',default='127.0.0.1',help='监听地址；局域网访问使用 0.0.0.0')
    parser.add_argument('--open',action='store_true')
    args = parser.parse_args()
    if args.record and not args.bridge:
        parser.error('--record 需要同时指定 --bridge')
    try:
        app = Application(args.demo, args.config, args.diagnostics)
    except (ValueError, OSError) as exc:
        parser.exit(1, f'无法读取配置：{exc}\n')
    httpd = None
    try:
        httpd = ThreadingHTTPServer((args.host,args.http_port),make_handler(app))
        if args.bridge:
            state = app.start_bridge(dict(port=args.bridge,baud=args.baud,virtual=args.virtual_port,
                                          link=args.link,record=args.record))
            print(f"Serial bridge: {args.bridge} <-> {state['app_port']}",flush=True)
        elif args.replay:
            app.start_replay(args.replay)
        elif args.follow:
            app.start_follow(args.follow)
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
