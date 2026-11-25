#!/usr/bin/env python3
"""High-rate GUI benchmark (not a unittest module).

Second-round acceptance tool: synthesize a real ``SVL1`` UDP stream (one TX and
one RX record per datagram, sender-clock ``t_ns`` with a large offset), feed a
real ``monitor.Application(udp=...)``, open the embedded GUI through
``run_desktop`` and measure what the desktop side can show.

Measured each second from inside the page (``page.runJavaScript``):
``window.ServalDebug()`` fields ``drawFps`` / ``pushEvents`` / ``frames`` /
``transport`` (all optional: the benchmark degrades to an injected
``requestAnimationFrame`` counter when the frontend fields are not available
yet, so it also runs while the second-round backend/frontend tasks are in
flight).  Parent-process CPU comes from ``time.process_time()``; process-tree
CPU is printed when ``psutil`` is installed.

Pass criteria (docs/gui-design.md §10.3/§10.4): no record loss and page drawing
``FPS >= 20``; otherwise exit 1.  Missing QtWebEngine prints a skip and exits 0.

Examples::

    python tools/serval/tests/high_rate_bench.py
    python tools/serval/tests/high_rate_bench.py --rate 500 --duration 10 --push-hz 120
    python tools/serval/tests/high_rate_bench.py --rate 500 --screenshot bench.png
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
ROOT = HERE.parents[1]                      # tools/serval
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

PREFIX = struct.Struct('<4sQ')
NS_PER_S = 1_000_000_000
# A sender clock far away from this computer's monotonic clock: the desktop
# window must display it without any "now" assumption (regression guard).
DEFAULT_NS_BASE = 1_700_000_000 * NS_PER_S
WINDOWS_CHROMIUM_FLAGS = '--disable-gpu --no-sandbox'


# ---------------------------------------------------------------------------
# Synthetic SVL1/SVF1 sender
# ---------------------------------------------------------------------------
def _crc_packet(packet):
    from lib.protocol import crc16
    return packet[:-2] + crc16(packet[:-2]).to_bytes(2, 'little')


def _tx_packet(t):
    from lib.protocol import TX
    yaw = .48 * math.sin(t * .8)
    pitch = .18 * math.sin(t * .55)
    packet = TX.pack(b'SP', 1, yaw, .384 * math.cos(t * .8), -.3072 * math.sin(t * .8),
                     pitch, .099 * math.cos(t * .55), -.05445 * math.sin(t * .55), 3, 0)
    return _crc_packet(packet)


def _rx_packet(t):
    from lib.protocol import RX
    yaw = .47 * math.sin((t - .1) * .8)
    pitch = .175 * math.sin((t - .08) * .55)
    cy, sy = math.cos(yaw / 2), math.sin(yaw / 2)
    cp, sp = math.cos(pitch / 2), math.sin(pitch / 2)
    packet = RX.pack(b'SP', 1, cy * cp, -sy * sp, cy * sp, sy * cp, yaw,
                     .376 * math.cos((t - .1) * .8), pitch,
                     .09625 * math.cos((t - .08) * .55), 26, 0, 1, 0)
    return _crc_packet(packet)


def _free_udp_port():
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
        probe.bind(('127.0.0.1', 0))
        return probe.getsockname()[1]


def _push_hz_value(text):
    value = str(text).strip().lower()
    if value == 'auto':
        return 'auto'
    try:
        hz = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError('--push-hz 需要 auto 或 20..240 的整数')
    if not 20 <= hz <= 240:
        raise argparse.ArgumentTypeError('--push-hz 必须在 20..240 之间（或 auto）')
    return hz


class SyntheticSender(threading.Thread):
    """One datagram per tick carrying one TX + one RX record (and 1 Hz SVF1)."""

    def __init__(self, port, rate=250.0, duration=20.0, ns_base=DEFAULT_NS_BASE):
        super().__init__(name='high-rate-udp', daemon=True)
        self.port = int(port)
        self.rate = max(1.0, float(rate))
        self.duration = max(1.0, float(duration))
        self.ns_base = int(ns_base)
        self.session = int.from_bytes(os.urandom(8), 'little')
        self.sent = {'tx': 0, 'rx': 0}
        self.datagrams = 0
        self.error = None
        self.stop_event = threading.Event()
        self._sequence = {0: 0, 1: 0}

    @property
    def sent_records(self):
        return self.sent['tx'] + self.sent['rx']

    def run(self):
        from lib.live_udp import FPS
        from lib.recording import HEADER, MAGIC, STATUS_HEAD, STATUS_OK
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
                started = time.monotonic()
                interval = 1.0 / self.rate
                index = 0
                next_fps = started + 1.0
                while not self.stop_event.is_set():
                    now = time.monotonic()
                    if now - started >= self.duration:
                        break
                    scheduled = started + index * interval
                    if now < scheduled:
                        self.stop_event.wait(min(scheduled - now, 0.02))
                        continue
                    # Keep the sender-time grid exact (ns = base + index/rate)
                    # and catch up with a burst when the host wakes up late, so
                    # recorded sample intervals never drift.
                    t = index * interval
                    ns = self.ns_base + int(round(t * NS_PER_S))
                    datagram = bytearray(PREFIX.pack(b'SVL1', self.session))
                    for direction, packet in ((0, _tx_packet(t)), (1, _rx_packet(t))):
                        self._sequence[direction] += 1
                        datagram += HEADER.pack(MAGIC, direction, STATUS_HEAD | STATUS_OK,
                                                len(packet), ns,
                                                self._sequence[direction], 0)
                        datagram += packet
                        self.sent['tx' if direction == 0 else 'rx'] += 1
                    sock.sendto(bytes(datagram), ('127.0.0.1', self.port))
                    self.datagrams += 1
                    index += 1
                    if now >= next_fps:
                        sock.sendto(FPS.pack(b'SVF1', float(self.rate)),
                                    ('127.0.0.1', self.port))
                        next_fps = now + 1.0
        except OSError as exc:
            self.error = str(exc)

    def stop(self):
        self.stop_event.set()


# ---------------------------------------------------------------------------
# Page-side sampling
# ---------------------------------------------------------------------------
JS_RAF_INJECT = r"""
(function () {
  if (window.__servalBenchRaf !== undefined) return 'already';
  window.__servalBenchRaf = 0;
  function tick() { window.__servalBenchRaf += 1; window.requestAnimationFrame(tick); }
  window.requestAnimationFrame(tick);
  return 'injected';
})()
"""

JS_SAMPLE = r"""
(function () {
  try {
    var out = {raf: window.__servalBenchRaf || 0, visible: !document.hidden};
    var debug = null;
    try {
      if (typeof window.ServalDebug === 'function') debug = window.ServalDebug();
      else debug = window.__SERVAL_DEBUG__ || null;
    } catch (error) { debug = null; }
    out.hasDebug = !!(debug && typeof debug === 'object');
    if (out.hasDebug) {
      var num = function (value) {
        return (typeof value === 'number' && isFinite(value)) ? value : null;
      };
      out.frames = num(debug.frames);
      out.retained = num(debug.retained);
      out.pushEvents = num(debug.pushEvents);
      out.drawFps = num(debug.drawFps);
      out.transport = debug.transport || null;
      out.mode = debug.mode || null;
      out.lastPushAgeMs = num(debug.lastPushAgeMs);
      out.detailMode = debug.detailMode || null;
      out.gaps = Array.isArray(debug.gaps) ? debug.gaps.length : null;
    }
    if (out.frames === null && out.retained === null) {
      var retained = document.getElementById('retained');
      if (retained) {
        var n = parseInt(String(retained.textContent || '').replace(/[^0-9]/g, ''), 10);
        if (isFinite(n)) out.retained = n;
      }
    }
    out.status = (document.getElementById('status') || {}).textContent || '';
    return JSON.stringify(out);
  } catch (error) {
    return JSON.stringify({error: String((error && error.stack) || error)});
  }
})()
"""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _stdio_utf8():
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, 'reconfigure'):
            stream.reconfigure(encoding='utf-8')


def _configure_webengine_environment():
    """Window screenshots need software compositing on Windows."""
    if os.name != 'nt':
        return
    current = os.environ.get('QTWEBENGINE_CHROMIUM_FLAGS', '')
    flags = current.split()
    for flag in WINDOWS_CHROMIUM_FLAGS.split():
        if flag not in flags:
            flags.append(flag)
    os.environ['QTWEBENGINE_CHROMIUM_FLAGS'] = ' '.join(flags)


def _constructor_params(cls):
    try:
        import inspect
        return set(inspect.signature(cls.__init__).parameters)
    except (TypeError, ValueError):
        return set()


def _fetch_snapshot(base_url):
    try:
        with urllib.request.urlopen(base_url + 'api/data?since=0', timeout=8) as response:
            payload = json.loads(response.read().decode('utf-8'))
        return payload if isinstance(payload, dict) else None
    except (OSError, ValueError):
        return None


def _run_js(page, script, callback):
    """runJavaScript with a safe callback: a closing window must not raise."""
    try:
        page.runJavaScript(script, callback)
    except Exception as exc:          # page destroyed / renderer gone
        print(f'[bench] runJavaScript 失败：{exc}', file=sys.stderr, flush=True)
        try:
            callback('')
        except Exception:
            pass


def _psutil_tree_cpu_time():
    """Total user+system CPU seconds of this process tree, or None."""
    try:
        import psutil
    except ImportError:
        return None
    try:
        process = psutil.Process()
        total = 0.0
        for item in [process] + process.children(recursive=True):
            try:
                times = item.cpu_times()
                total += times.user + times.system
            except (psutil.Error, OSError):
                continue
        return total
    except (psutil.Error, OSError):
        return None


def _mean(values):
    return sum(values) / len(values) if values else None


def _rate(value, suffix=''):
    return '—' if value is None else f'{value:.1f}{suffix}'


class BenchState:
    def __init__(self):
        self.session = None
        self.done = False
        self.busy = False
        self.timer = None
        self.prev = None
        self.backend = None
        self.samples = []
        self.push_rates = []
        self.raf_rates = []
        self.draw_fps = []
        self.cpu_percent = None
        self.psutil_percent = None
        self.screenshot = None
        self.frames = None
        self.transport = None
        self.exit_code = 1

    def consume(self, payload, wall):
        """Update counters from one JS sample; returns the next baseline."""
        raf = payload.get('raf')
        push_events = payload.get('pushEvents')
        if payload.get('drawFps') is not None:
            self.draw_fps.append(float(payload['drawFps']))
        if payload.get('frames') is not None:
            self.frames = payload['frames']
        if payload.get('transport'):
            self.transport = payload['transport']
        current = {'wall': wall,
                   'raf': raf if isinstance(raf, (int, float)) else 0,
                   'push': push_events if isinstance(push_events, (int, float)) else None}
        previous = self.prev
        if previous is not None and wall > previous['wall']:
            delta_wall = wall - previous['wall']
            raf_delta = current['raf'] - previous['raf']
            if raf_delta >= 0:
                self.raf_rates.append(raf_delta / delta_wall)
            if current['push'] is not None and previous['push'] is not None:
                push_delta = current['push'] - previous['push']
                if push_delta >= 0:
                    self.push_rates.append(push_delta / delta_wall)
        self.prev = current
        return current


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main(argv=None):
    _stdio_utf8()
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--rate', type=float, default=250.0, metavar='HZ',
                        help='records per second per direction (default: 250; total = 2×)')
    parser.add_argument('--duration', type=float, default=10.0,
                        help='measurement duration in seconds (default: 10)')
    parser.add_argument('--push-hz', type=_push_hz_value, default='auto',
                        metavar='{auto|20..240}',
                        help='GUI push rate: auto=screen refresh rate, or 20..240 (default: auto)')
    parser.add_argument('--screenshot', type=Path,
                        help='save a window screenshot to this path')
    parser.add_argument('--port', type=int,
                        help='fixed loopback UDP port (default: a free port)')
    parser.add_argument('--settle', type=int, default=1800,
                        help='milliseconds after loadFinished before sampling (default: 1800)')
    parser.add_argument('--timeout', type=int, default=0,
                        help='watchdog in milliseconds (default: settle + duration + 20 s)')
    args = parser.parse_args(argv)
    duration = max(1.0, float(args.duration))

    _configure_webengine_environment()
    from lib.desktop_window import run_desktop, webengine_available
    if not webengine_available():
        print('[bench] SKIP: QtWebEngine/PyQt5 不可用，无法打开原生 GUI 窗口。', flush=True)
        return 0
    if os.name == 'posix' and not (os.environ.get('DISPLAY')
                                   or os.environ.get('WAYLAND_DISPLAY')):
        print('[bench] SKIP: 没有图形桌面（DISPLAY/WAYLAND_DISPLAY 缺失）。', flush=True)
        return 0

    from PyQt5.QtCore import QTimer
    import monitor

    params = _constructor_params(monitor.Application)
    if 'udp' not in params:
        print('[bench] SKIP: monitor.Application 尚未实现契约接口 udp=...（后端未就绪）。',
              flush=True)
        return 2

    screenshot_dir = Path(tempfile.mkdtemp(prefix='serval-bench-'))
    screenshot_path = (args.screenshot.resolve() if args.screenshot
                       else screenshot_dir / f'high-rate-{int(args.rate)}hz.png')
    screenshot_path.parent.mkdir(parents=True, exist_ok=True)
    timeout_ms = args.timeout or int((args.settle / 1000 + duration + 20) * 1000)

    port = args.port or _free_udp_port()
    app_kwargs = dict(udp=dict(host='127.0.0.1', port=port))
    if args.push_hz != 'auto' and 'push_hz' in params:
        app_kwargs['push_hz'] = args.push_hz
    try:
        app = monitor.Application(**app_kwargs)
    except (OSError, ValueError, TypeError) as exc:
        print(f'[bench] ERROR: 无法创建 monitor.Application：{exc}', file=sys.stderr, flush=True)
        return 2

    sender = SyntheticSender(port, rate=args.rate,
                             duration=args.settle / 1000 + duration + 20)
    sender.start()
    state = BenchState()
    print(f'[bench] 合成 UDP 127.0.0.1:{port} · {args.rate:g} 记录/s/方向 '
          f'(合计 {2 * args.rate:g}/s) · 测量 {duration:g} s · push-hz={args.push_hz}',
          flush=True)

    def schedule(session):
        state.session = session
        page = session.view.page()
        clock = {'process': 0.0, 'psutil': None, 'wall': 0.0}

        def finalize():
            if state.done:
                return
            state.done = True
            if state.timer is not None:
                try:
                    state.timer.stop()
                except RuntimeError:
                    pass
                state.timer = None
            elapsed = time.monotonic() - clock['wall'] if clock['wall'] else 0.0
            process_cpu = time.process_time() - clock['process'] if clock['wall'] else 0.0
            state.cpu_percent = 100.0 * process_cpu / elapsed if elapsed > 0 else None
            psutil_end = _psutil_tree_cpu_time()
            if clock['psutil'] is not None and psutil_end is not None and elapsed > 0:
                state.psutil_percent = 100.0 * (psutil_end - clock['psutil']) / elapsed
            # Stop sending, let the backend drain its socket, then read the
            # authoritative receive counters from the backend snapshot.
            sender.stop()
            sender.join(timeout=3)
            time.sleep(0.4)
            state.backend = _fetch_snapshot(session.url)
            try:
                pixmap = session.window.grab()
                if pixmap.save(str(screenshot_path), 'PNG'):
                    state.screenshot = str(screenshot_path)
            except Exception as exc:
                print(f'[bench] 截图失败：{exc}', file=sys.stderr, flush=True)
            print_report(args, sender, state, duration)
            state.exit_code = 0 if _evaluate(args, sender, state) else 1
            session.qt_app.quit()

        def on_sample(value):
            if state.done:
                return
            state.busy = False
            try:
                payload = json.loads(value) if value else {}
            except (TypeError, ValueError):
                payload = {}
            if not isinstance(payload, dict) or payload.get('error'):
                payload = {}
            state.consume(payload, time.monotonic())
            state.samples.append(time.monotonic())
            if len(state.samples) >= int(round(duration)):
                finalize()

        def tick():
            if state.done or state.busy:
                return
            state.busy = True
            _run_js(page, JS_SAMPLE, on_sample)

        def prime(value):
            if state.done:
                return
            try:
                payload = json.loads(value) if value else {}
            except (TypeError, ValueError):
                payload = {}
            state.consume(payload if isinstance(payload, dict) else {}, time.monotonic())
            clock['process'] = time.process_time()
            clock['psutil'] = _psutil_tree_cpu_time()
            clock['wall'] = time.monotonic()
            timer = QTimer()
            timer.timeout.connect(tick)
            timer.start(1000)
            state.timer = timer

        def begin():
            _run_js(page, JS_RAF_INJECT,
                    lambda _value: _run_js(page, JS_SAMPLE, prime))

        session.view.loadFinished.connect(
            lambda _ok=True: QTimer.singleShot(max(0, args.settle), begin))

        def watchdog():
            if state.done:
                return
            print('[bench] WARN: 总超时，使用已采集样本收尾', flush=True)
            finalize()

        QTimer.singleShot(timeout_ms, watchdog)

    try:
        run_desktop(app, title='Serval 高频 GUI 基准', push_hz=args.push_hz,
                    on_ready=schedule)
    except Exception as exc:
        print(f'[bench] ERROR: 桌面窗口运行失败：{exc}', file=sys.stderr, flush=True)
        return 2
    finally:
        sender.stop()
        sender.join(timeout=1)
    return state.exit_code


def print_report(args, sender, state, duration):
    backend = state.backend or {}
    udp = backend.get('udp') if isinstance(backend.get('udp'), dict) else {}
    sent = sender.sent_records
    received = udp.get('records')
    if received is None:
        received = backend.get('retained')
    received = int(received or 0)
    lost = max(0, sent - received)
    push_rate = _mean(state.push_rates)
    raf_rate = _mean(state.raf_rates)
    draw_fps = _mean(state.draw_fps) if state.draw_fps else raf_rate
    session = state.session
    print('', flush=True)
    print('==== Serval 高频 GUI 基准 ====', flush=True)
    print(f"参数: --rate {args.rate:g} 记录/s/方向（合计 {2 * args.rate:g} 记录/s）"
          f" · --duration {duration:g} s · --push-hz {args.push_hz}"
          f" · 采集样本 {len(state.samples)} 个", flush=True)
    print(f"发送: TX {sender.sent['tx']} + RX {sender.sent['rx']} = {sent} 记录 "
          f"({sender.datagrams} 数据报)", flush=True)
    print(f"接收: {received} 记录 · missing={udp.get('missing')} late={udp.get('late')} "
          f"invalid={udp.get('invalid')} · 丢失 {lost} "
          f"({100.0 * lost / sent if sent else 0:.3f}%) "
          f"· 后端 push_hz={backend.get('push_hz')} · mode={backend.get('mode')!r}", flush=True)
    print(f"推送: 平均 {_rate(push_rate, ' Hz')}（pushEvents 差分 n={len(state.push_rates)}）"
          f" · 页面 transport={state.transport or '—'}"
          f" · frames={state.frames if state.frames is not None else '—'}", flush=True)
    print(f"绘制: {'debug ' if state.draw_fps else 'rAF '}{_rate(draw_fps, ' FPS')}"
          f" · rAF {_rate(raf_rate, ' FPS')}", flush=True)
    if session is not None:
        psutil_text = (f" · psutil 进程树 {_rate(state.psutil_percent, '%')}"
                       if state.psutil_percent is not None
                       else ' · psutil 未安装（跳过进程树 CPU）')
        print(f"桌面: push_hz={session.push_hz} Hz · 屏幕 {session.screen_hz} Hz"
              f" · CPU 父进程 {_rate(state.cpu_percent, '%')}{psutil_text}", flush=True)
    if state.screenshot:
        print(f"截图: {state.screenshot}", flush=True)
    if sender.error:
        print(f"发送端错误: {sender.error}", flush=True)


def _evaluate(args, sender, state):
    backend = state.backend or {}
    udp = backend.get('udp') if isinstance(backend.get('udp'), dict) else {}
    failures = []
    if sender.error:
        failures.append(f'发送端错误：{sender.error}')
    if backend.get('mode') != 'live_udp':
        failures.append(f"后端模式不是 live_udp：{backend.get('mode')!r}")
    sent = sender.sent_records
    received = udp.get('records')
    if received is None:
        received = backend.get('retained')
    received = int(received or 0)
    lost = max(0, sent - received)
    if lost:
        failures.append(f'丢失 {lost}/{sent} 记录')
    for name in ('missing', 'late', 'invalid'):
        value = udp.get(name)
        if value is None:
            failures.append(f'后端 udp.{name} 缺失（UDP 快照不完整）')
        elif int(value):
            failures.append(f'udp.{name}={value}')
    draw_values = state.draw_fps or state.raf_rates
    draw_fps = _mean(draw_values) if draw_values else None
    if draw_fps is None or draw_fps < 20:
        failures.append(f'绘制 FPS 过低：{draw_fps}')
    if failures:
        print('结果: FAIL — ' + '；'.join(failures), flush=True)
        return False
    print(f'结果: PASS（无丢失 · 绘制 {draw_fps:.1f} FPS ≥ 20）', flush=True)
    return True


if __name__ == '__main__':
    sys.exit(main())
