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
from urllib.parse import parse_qs, unquote, urlparse

from protocol import FIELDS, RX, TX, crc16, decode_frame
from recording import Recorder, Replay
from serial_io import SerialBridge

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

    def add(self, direction, values, timestamp=None):
        with self.lock:
            stamp = time.monotonic() if timestamp is None else timestamp
            t = max(0, stamp - self.start)
            self.seq += 1
            previous = self.latest.get(direction)
            if previous:
                self.max_gap_ms[direction] = max(self.max_gap_ms[direction], (t-previous['t'])*1000)
            frame = dict(seq=self.seq, t=t, direction=direction, values=values)
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
            dropped = max(0, selected[0]['seq']-max(since,self.epoch_start_seq)-1) if selected else 0
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

    def export(self):
        with self.lock:
            frames = list(self.frames)
        output = io.StringIO(newline='')
        writer = csv.writer(output)
        writer.writerow(('time_s', 'direction') + FIELDS)
        for frame in frames:
            writer.writerow((f"{frame['t']:.6f}", frame['direction']) +
                            tuple(frame['values'].get(k, '') for k in FIELDS))
        return output.getvalue().encode('utf-8-sig')

    def reset(self, source):
        with self.lock:
            self.start = time.monotonic()
            self.epoch += 1
            self.epoch_start_seq = self.seq
            self.frames.clear()
            self.latest.clear()
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
            direction, values = decode_frame(packet[:-2]+crc16(packet[:-2]).to_bytes(2, 'little'))
            store.add(direction, values)
        stop.wait(.01)


class Application:
    def __init__(self, demo=False):
        self.store = Store('演示数据' if demo else '未连接串口')
        self.bridge = SerialBridge(self.store.add, self.store.reject, self.record_raw)
        self.lock = threading.RLock()
        self.mode = 'demo' if demo else 'idle'
        self.recorder = self.replay = None
        self.upload_dir = None
        self.demo = demo
        self.demo_stop = threading.Event()
        if demo:
            threading.Thread(target=demo_loop, args=(self.store,self.demo_stop), daemon=True).start()

    def record_raw(self, *args):
        recorder = self.recorder
        if recorder:
            recorder.add(*args)

    def start_bridge(self, args):
        with self.lock:
            if self.demo or self.mode == 'replay':
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
            candidate = Replay(path, self.store.add, self.store.reject, 0)
            self.stop()
            self.store.reset('离线回放 · '+(name or Path(path).name))
            candidate.base_time = self.store.start
            self.replay, self.upload_dir = candidate, upload_dir
            self.mode = 'replay'
            candidate.control('play')
            return candidate.snapshot()

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
                        protocol=dict(tx_bytes=TX.size, rx_bytes=RX.size))
            return data

    def close(self):
        self.demo_stop.set()
        self.stop()


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
            if parsed.path == '/api/export.csv':
                return self.respond(app.store.export(), 'text/csv; charset=utf-8',
                    extra={'Content-Disposition':'attachment; filename="serval-telemetry.csv"'})
            if parsed.path in ('/','/index.html','/app.js','/style.css'):
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
    parser.add_argument('--virtual-port',default='',help='Windows 虚拟串口对的工具端')
    parser.add_argument('--link',default='',help='Linux 固定虚拟串口路径，默认 /tmp/serval-gimbal')
    parser.add_argument('--record',nargs='?',const='auto',default=False,metavar='FILE',
                        help='记录原始帧；省略文件名则保存到 serval/records/')
    parser.add_argument('--baud',type=int,default=921600)
    parser.add_argument('--http-port',type=int,default=8765)
    parser.add_argument('--open',action='store_true')
    args = parser.parse_args()
    if args.record and not args.bridge:
        parser.error('--record 需要同时指定 --bridge')
    app = Application(args.demo)
    httpd = None
    try:
        httpd = ThreadingHTTPServer(('127.0.0.1',args.http_port),make_handler(app))
        if args.bridge:
            state = app.start_bridge(dict(port=args.bridge,baud=args.baud,virtual=args.virtual_port,
                                          link=args.link,record=args.record))
            print(f"Serial bridge: {args.bridge} <-> {state['app_port']}",flush=True)
        elif args.replay:
            app.start_replay(args.replay)
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
