#!/usr/bin/env python3
"""Lead end-to-end check for the desktop GUI page (not a unittest module).

Verifies the *live UDP* path with a sender clock that is far away from the
receiver's monotonic clock (the Windows remote scenario), the camera MJPEG
panel, the embedded QtWebEngine page, and the native "offline decode" quick
action through the real HTTP API.

Run from ``tools/serval``::

    python tests/gui_e2e_live.py
    python tests/gui_e2e_live.py --screenshot docs/images/gui-page-live.png
    python tests/gui_e2e_live.py --keep-temp
"""
import argparse
import json
import math
import os
from pathlib import Path
import socket
import struct
import sys
import tempfile
import threading
import time
import urllib.request

HERE = Path(__file__).resolve()
ROOT = HERE.parents[1]                 # tools/serval
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

PREFIX = struct.Struct('<4sQ')
SENDER_NS_BASE = 1_234_567_890_123      # ~1234 s after the sender's t0
CAMERA_SIZE = (320, 180)


def free_udp_port():
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.bind(('127.0.0.1', 0))
        return sock.getsockname()[1]


def camera_jpeg(phase):
    """A cheap synthetic JPEG for the camera panel (created with Qt)."""
    from PyQt5.QtGui import QColor, QImage, QPainter
    image = QImage(CAMERA_SIZE[0], CAMERA_SIZE[1], QImage.Format_RGB32)
    image.fill(QColor(12, 22, 34))
    painter = QPainter(image)
    try:
        painter.fillRect(0, 0, CAMERA_SIZE[0], CAMERA_SIZE[1], QColor(12, 22, 34))
        painter.setPen(QColor(97, 183, 255))
        painter.drawRect(8, 8, CAMERA_SIZE[0] - 16, CAMERA_SIZE[1] - 16)
        painter.setPen(QColor(56, 217, 182))
        x = int(CAMERA_SIZE[0] * (.5 + .35 * math.sin(phase)))
        painter.drawLine(CAMERA_SIZE[0] // 2, CAMERA_SIZE[1] // 2, x, CAMERA_SIZE[1] // 3)
    finally:
        painter.end()
    path = Path(tempfile.gettempdir()) / ('serval-e2e-camera-%d.jpg' % os.getpid())
    image.save(str(path), 'JPEG', 70)
    return path


class SyntheticSender(threading.Thread):
    """Send SVL1 records with a large sender-relative ns offset + camera frames."""

    def __init__(self, port, camera_path, duration=8.0, rate=100.0):
        super().__init__(name='e2e-udp-sender', daemon=True)
        self.port = port
        self.camera_path = camera_path
        self.duration = duration
        self.interval = 1.0 / rate
        self.session = 0x5EEA10E2
        self.sent = 0
        self.error = None
        # Real sender-side sample intervals (ms); the Windows Event/sleep timer
        # granularity is ~16 ms, so the loop deadline-schedules and busy-waits the
        # last millisecond to actually reach the requested rate.
        self.actual_intervals = []
        self.stop_event = threading.Event()

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
        from lib.live_udp import FPS
        from lib.recording import HEADER, MAGIC, STATUS_HEAD, STATUS_OK
        sequence = {0: 0, 1: 0}
        # Camera writes must not stall the 4 ms sender pacing.
        threading.Thread(target=self._camera_writer, name='e2e-camera', daemon=True).start()
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
                started = time.perf_counter()
                next_fps = next_send = 0.0
                previous_ns = None
                while not self.stop_event.is_set():
                    now = time.perf_counter() - started
                    if now < next_send:
                        remaining = next_send - now
                        if remaining > .0012:
                            # Python 3.11 on Windows uses a high-resolution timer
                            # for sleep(), so most of the wait costs no CPU.
                            time.sleep(remaining - .0008)
                        # Spin only the last ~1 ms to avoid the ~16 ms Event.wait /
                        # coarse-timer quantization that produces bursty senders.
                        while (not self.stop_event.is_set()
                               and time.perf_counter() - started < next_send):
                            pass
                        now = time.perf_counter() - started
                    if now >= self.duration:
                        break
                    elapsed = now
                    tx, rx = self._pack(elapsed)
                    ns = SENDER_NS_BASE + int(elapsed * 1e9)
                    if previous_ns is not None:
                        self.actual_intervals.append((ns - previous_ns) * 1e-6)
                    previous_ns = ns
                    datagram = bytearray(PREFIX.pack(b'SVL1', self.session))
                    for direction, packet in ((0, tx), (1, rx)):
                        sequence[direction] += 1
                        datagram += HEADER.pack(MAGIC, direction, STATUS_HEAD | STATUS_OK,
                                                len(packet), ns, sequence[direction], 0)
                        datagram += packet
                    sock.sendto(bytes(datagram), ('127.0.0.1', self.port))
                    self.sent += 1
                    if elapsed >= next_fps:
                        sock.sendto(FPS.pack(b'SVF1', 60.0), ('127.0.0.1', self.port))
                        next_fps = elapsed + 1.0
                    # Never "catch up" after a stall: bursts would create 0 ms
                    # sample intervals that confuse the interval statistics.
                    next_send = max(next_send + self.interval, now + self.interval)
        except OSError as exc:
            self.error = str(exc)

    def _camera_writer(self):
        started = time.monotonic()
        while not self.stop_event.wait(.1):
            try:
                camera_jpeg((time.monotonic() - started) * 6).replace(self.camera_path)
            except Exception as exc:  # pragma: no cover - diagnostics
                self.error = str(exc)
                return

    def stop(self):
        self.stop_event.set()


class Results:
    def __init__(self):
        self.items = []

    def check(self, ok, name, detail=''):
        self.items.append((bool(ok), name, detail))
        print(('PASS' if ok else 'FAIL'), name, detail, flush=True)
        return ok

    def failures(self):
        return [item for item in self.items if not item[0]]


def http_json(base, path, payload=None):
    data = None if payload is None else json.dumps(payload).encode()
    request = urllib.request.Request(base + path, data=data,
                                     headers={'Content-Type': 'application/json'})
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.load(response)


def run_native_checks(app, base, results, temp_dir):
    """Exercise /api/native/status and /api/native/decode without file dialogs."""
    try:
        status = http_json(base, '/api/native/status')
        results.check(status.get('available') is True, 'native status available',
                      json.dumps({k: status.get(k) for k in ('mode', 'capabilities')}))
    except Exception as exc:
        results.check(False, 'native status available', repr(exc))
        return
    # Save the live UDP cache directly (the button only adds a file dialog on top).
    bin_path = Path(temp_dir) / 'e2e-cache.bin'
    try:
        saved = app.save_cache(str(bin_path))
        results.check(saved.get('count', 0) > 0 and bin_path.is_file(),
                      'save cache .bin', json.dumps(saved))
    except Exception as exc:
        results.check(False, 'save cache .bin', repr(exc))
        return
    try:
        decoded = http_json(base, '/api/native/decode',
                            {'path': str(bin_path), 'plot': True})
        output = Path(decoded.get('output_dir', ''))
        results.check(decoded.get('returncode') == 0
                      and (output / 'tx.csv').is_file() and (output / 'rx.csv').is_file(),
                      'offline decode quick action',
                      json.dumps({k: decoded.get(k) for k in ('returncode', 'output_dir')}))
    except Exception as exc:
        results.check(False, 'offline decode quick action', repr(exc))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--screenshot', default=str(ROOT / 'docs' / 'images' / 'gui-page-live.png'))
    parser.add_argument('--detail-screenshot',
                        default=str(ROOT / 'docs' / 'images' / 'gui-page-detail.png'))
    parser.add_argument('--rate', type=float, default=100.0,
                        help='records per second per direction (default: 100)')
    parser.add_argument('--duration', type=float, default=8.0, help='synthetic sender duration')
    parser.add_argument('--push-hz', default='auto',
                        help='SSE push rate passed to run_desktop (auto or 20..240)')
    parser.add_argument('--no-detail', action='store_true',
                        help='skip the detail-mode / duplex screenshot checks')
    parser.add_argument('--keep-temp', action='store_true')
    parser.add_argument('--wait', type=float, default=4.0)
    args = parser.parse_args(argv)

    from lib.desktop_window import webengine_available
    if not webengine_available():
        print('SKIP: QtWebEngine/PyQt5 not available on this machine')
        return 0

    results = Results()
    temp_dir = tempfile.mkdtemp(prefix='serval-e2e-')
    camera_path = camera_jpeg(0)
    app = None
    sender = None
    screenshot = Path(args.screenshot).resolve()

    def on_ready(session):
        nonlocal app, sender
        from PyQt5.QtCore import QTimer
        base = session.url.rstrip('/')
        port = app.udp.port
        sender = SyntheticSender(port, Path(str(camera_path)),
                                 duration=args.duration, rate=args.rate)
        sender.start()
        deadline = args.wait

        def check_page():
            session.view.page().runJavaScript(
                'JSON.stringify(window.ServalDebug ? window.ServalDebug() : {})',
                lambda raw: inspect_page(session, raw))

        def inspect_page(session, raw):
            try:
                debug = json.loads(raw or '{}')
            except ValueError:
                debug = {}
            print('[e2e] ServalDebug =', json.dumps(debug, ensure_ascii=False), flush=True)
            results.check(debug.get('mode') == 'live_udp', 'page mode live_udp',
                          str(debug.get('mode')))
            results.check(debug.get('transport') == 'sse', 'page transport is SSE',
                          str(debug.get('transport')))
            results.check((debug.get('frames') or 0) > 100, 'page received frames',
                          str(debug.get('frames')))
            results.check(bool(debug.get('nativeToolsVisible')), 'native quick buttons visible')
            results.check(bool(debug.get('cameraVisible')), 'camera panel visible')
            latest = debug.get('latestT')
            shown = debug.get('displayNow')
            if isinstance(latest, (int, float)) and isinstance(shown, (int, float)):
                results.check(latest > 1200.0, 'sender ns offset preserved', str(latest))
                results.check(abs(shown - latest) <= 1.0, 'display now follows sender clock',
                              'now=%.3f latest=%.3f' % (shown, latest))
            else:
                results.check(False, 'display now follows sender clock', repr((latest, shown)))
            udp = debug.get('udp') or {}
            results.check(udp.get('peer') == '127.0.0.1' and (udp.get('missing') or 0) >= 0,
                          'UDP counters exposed', json.dumps(udp))
            shot_written = False
            try:
                screenshot.parent.mkdir(parents=True, exist_ok=True)
                image = session.window.grab()
                shot_written = bool(image.save(str(screenshot)))
                print('[e2e] screenshot:', screenshot, image.width(), image.height(), flush=True)
            except Exception as exc:
                print('[e2e] screenshot failed:', exc, flush=True)
            results.check(shot_written, 'window screenshot saved', str(screenshot))
            results.check(sender is not None and sender.sent > 0, 'synthetic UDP sender active',
                          str(sender.sent if sender else 0))
            check_throughput()
            if args.no_detail:
                start_native()
            else:
                enable_detail()

        def check_throughput():
            """No sample loss and sane high-rate detail statistics on the server side."""
            snap = app.snapshot(0)
            expected = max(1, sender.sent * 2)              # TX + RX per datagram
            retained = snap['retained']
            detail = (snap.get('udp') or {}).get('detail') or {}
            results.check(retained >= expected * .98, 'no frame loss at high rate',
                          'retained=%d expected=%d' % (retained, expected))
            udp = snap.get('udp') or {}
            results.check((udp.get('missing') or 0) == 0 and (udp.get('late') or 0) == 0
                          and (udp.get('invalid') or 0) == 0,
                          'UDP counters clean at high rate',
                          json.dumps({k: udp.get(k) for k in ('missing', 'late', 'invalid')}))
            tx = detail.get('tx') or {}
            median = tx.get('interval_median_ms')
            nominal = 1000.0 / args.rate
            actual = sorted(sender.actual_intervals)[len(sender.actual_intervals) // 2] \
                if sender.actual_intervals else None
            expected_median = actual if isinstance(actual, (int, float)) and actual > 0 else nominal
            results.check(isinstance(median, (int, float)) and
                          expected_median * .6 <= median <= expected_median * 1.6,
                          'detail interval median follows sender interval',
                          'median=%r sender_median=%r nominal=%.2fms'
                          % (median, actual, nominal))
            results.check(isinstance(tx.get('interval_jitter_ms'), (int, float))
                          and isinstance(tx.get('arrival_median_ms'), (int, float)),
                          'detail jitter/arrival stats exposed',
                          json.dumps({k: tx.get(k) for k in ('interval_median_ms',
                                                            'interval_jitter_ms',
                                                            'arrival_median_ms')}))
            results.check(isinstance(detail.get('gaps'), list), 'detail gap list exposed',
                          'gaps=%d' % len(detail.get('gaps') or []))

        def enable_detail():
            session.view.page().runJavaScript(
                "(function(){const b=document.getElementById('detail-mode');"
                "if(!b)return 'missing';if(!b.classList.contains('active'))b.click();"
                "return b.classList.contains('active')?'ok':'not-active';})()",
                lambda _result: QTimer.singleShot(500, read_detail))

        def read_detail():
            session.view.page().runJavaScript(
                "JSON.stringify({dbg:window.ServalDebug?window.ServalDebug():{},"
                "duplex:(function(){const c=document.getElementById('duplex-canvas');"
                "if(!c)return null;const r=c.getBoundingClientRect();"
                "return {w:r.width,h:r.height,visible:r.width>0&&r.height>0};})(),"
                "detailElement:!!document.getElementById('detail-mode')})",
                lambda raw: inspect_detail(raw))

        def inspect_detail(raw):
            try:
                payload = json.loads(raw or '{}')
            except ValueError:
                payload = {}
            debug = payload.get('dbg') or {}
            duplex = payload.get('duplex') or {}
            print('[e2e] detail debug =', json.dumps(
                {k: debug.get(k) for k in ('detailMode', 'drawFps', 'pushEvents',
                                           'pushHz', 'gaps', 'duplexWindow')},
                ensure_ascii=False), flush=True)
            results.check(payload.get('detailElement') is True, 'detail mode control exists')
            results.check(debug.get('detailMode') is True, 'detail mode enabled',
                          str(debug.get('detailMode')))
            results.check((debug.get('drawFps') or 0) > 20, 'page draw FPS at high rate',
                          str(debug.get('drawFps')))
            results.check((debug.get('pushEvents') or 0) > 0, 'SSE push events counted',
                          str(debug.get('pushEvents')))
            results.check(bool(duplex.get('visible')) and (duplex.get('w') or 0) > 100,
                          'duplex timing canvas visible',
                          json.dumps(duplex))
            results.check(isinstance(debug.get('gaps'), list), 'page gap list exposed')
            # Scroll the duplex panel into view so the documentation screenshot shows
            # the timing detail instead of the page top.
            session.view.page().runJavaScript(
                "(function(){const c=document.getElementById('duplex-canvas');"
                "if(c&&c.scrollIntoView)c.scrollIntoView({block:'center'});return true;})()",
                lambda _ok: QTimer.singleShot(350, save_detail_shot))

        def save_detail_shot():
            detail_shot = Path(args.detail_screenshot).resolve()
            written = False
            try:
                detail_shot.parent.mkdir(parents=True, exist_ok=True)
                image = session.window.grab()
                written = bool(image.save(str(detail_shot)))
                print('[e2e] detail screenshot:', detail_shot, image.width(), image.height(),
                      flush=True)
            except Exception as exc:
                print('[e2e] detail screenshot failed:', exc, flush=True)
            results.check(written, 'detail-mode screenshot saved', str(detail_shot))
            start_native()

        def start_native():
            # Native quick actions must run off the GUI thread: NativeHost marshals
            # its dialogs/decode through the GUI event loop.
            threading.Thread(target=native_then_close,
                             args=(session, base), name='e2e-native', daemon=True).start()

        def native_then_close(session, base):
            try:
                run_native_checks(app, base, results, temp_dir)
            finally:
                # Post window.close() to the GUI thread (a bare QTimer from a
                # worker thread would be owned by that thread and never fire).
                from PyQt5.QtCore import QMetaObject, Qt
                QMetaObject.invokeMethod(session.window, 'close', Qt.QueuedConnection)

        from PyQt5.QtCore import QTimer
        QTimer.singleShot(int(deadline * 1000), check_page)
        # Safety net: never hang a headless run.
        QTimer.singleShot(int((deadline + 90) * 1000), session.request_close)

    from monitor import Application
    from lib.desktop_window import run_desktop
    import inspect
    run_kwargs = dict(title='Serval · Lead E2E 实时 UDP + 相机', preview_fps=20,
                      on_ready=on_ready)
    if 'push_hz' in inspect.signature(run_desktop).parameters:
        run_kwargs['push_hz'] = args.push_hz
    app = Application(udp=dict(host='127.0.0.1', port=free_udp_port()),
                      camera=str(camera_path), preview_fps=20)
    try:
        run_desktop(app, **run_kwargs)
    finally:
        if sender is not None:
            sender.stop()
            sender.join(timeout=2)
        try:
            app.close()
        except Exception:
            pass
        if not args.keep_temp:
            import shutil
            shutil.rmtree(temp_dir, ignore_errors=True)
            try:
                Path(str(camera_path)).unlink()
            except OSError:
                pass

    print('[e2e] %d checks, %d failed' % (len(results.items), len(results.failures())), flush=True)
    if results.failures():
        for ok, name, detail in results.failures():
            print('[e2e] FAILED:', name, detail, flush=True)
        return 1
    if screenshot.is_file():
        print('[e2e] PASS screenshot:', screenshot, screenshot.stat().st_size, 'bytes', flush=True)
    return 0


if __name__ == '__main__':
    sys.exit(main())
