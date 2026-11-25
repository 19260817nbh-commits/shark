#!/usr/bin/env python3
"""GUI smoke test for the native QtWebEngine window (not a unittest module).

The file name is ``gui_smoke.py`` on purpose: ``python -m unittest discover
-p 'test_*.py'`` must not import it.

What it does
------------
* ``--demo``: builds ``monitor.Application(demo=True)`` (no UDP, works while the
  backend is still being finished) and only verifies the window/page load.
* default: starts a synthetic ``SVL1`` UDP sender (``lib.recording.HEADER`` +
  ``lib.protocol.TX/RX`` packets + CRC16, ~100 Hz for a few seconds), feeds
  ``monitor.Application(udp={'host': '127.0.0.1', 'port': <free>})`` and verifies
  the page really receives data through the backend/SSE path.
* Opens the window with ``lib.desktop_window.run_with_app``; after the page
  finished loading + a settle delay it uses ``page.runJavaScript`` to read the
  DOM, asserts status text / cached frames / chart canvases / native quick
  buttons, grabs the window to a PNG and exits 0.

``--load-only`` keeps only the window/page-load assertions, which is useful
before the backend/frontend tasks are merged.

Run::

    python tools/serval/tests/gui_smoke.py --demo
    python tools/serval/tests/gui_smoke.py --demo --load-only
    python tools/serval/tests/gui_smoke.py
    python tools/serval/tests/gui_smoke.py --camera   # + camera MJPEG panel

``--camera`` writes a synthetic JPEG, passes it as ``Application(camera=...)``
and additionally asserts the ``#camera-panel`` / ``/api/camera.mjpg`` chain and
saves a second screenshot scrolled to the panel.
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
ROOT = HERE.parents[1]              # tools/serval
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

PREFIX = struct.Struct('<4sQ')
WINDOWS_CHROMIUM_FLAGS = '--disable-gpu --no-sandbox'
WAITING_WORDS = ('等待', '未连接', '正在连接', '监听服务未连接', '失败', '已中断', '未收到')
LIVE_WORDS = ('接收', '实时', '演示', '监听', 'UDP', '回放')


# ---------------------------------------------------------------------------
# Synthetic UDP sender
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


class SyntheticUdpSender(threading.Thread):
    """Send real SVL1 datagrams so the backend/UDP/SSE path is exercised."""

    def __init__(self, port, duration=8.0, rate=100.0):
        super().__init__(name='synthetic-udp', daemon=True)
        self.port = port
        self.duration = max(1.0, float(duration))
        self.interval = 1.0 / max(1.0, float(rate))
        self.session = int.from_bytes(os.urandom(8), 'little')
        self.sent = 0
        self.error = None
        self.stop_event = threading.Event()

    def run(self):
        from lib.live_udp import FPS
        from lib.recording import HEADER, MAGIC, STATUS_HEAD, STATUS_OK
        sequence = {0: 0, 1: 0}
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
                started = time.monotonic()
                start_ns = time.perf_counter_ns()
                next_fps = 0.0
                while not self.stop_event.is_set():
                    elapsed = time.monotonic() - started
                    if elapsed >= self.duration:
                        break
                    ns = time.perf_counter_ns() - start_ns
                    datagram = bytearray(PREFIX.pack(b'SVL1', self.session))
                    for direction, packet in ((0, _tx_packet(elapsed)), (1, _rx_packet(elapsed))):
                        sequence[direction] += 1
                        datagram += HEADER.pack(MAGIC, direction, STATUS_HEAD | STATUS_OK,
                                                len(packet), ns, sequence[direction], 0)
                        datagram += packet
                    sock.sendto(bytes(datagram), ('127.0.0.1', self.port))
                    self.sent += 1
                    if elapsed >= next_fps:
                        sock.sendto(FPS.pack(b'SVF1', 60.0), ('127.0.0.1', self.port))
                        next_fps = elapsed + 1.0
                    self.stop_event.wait(max(0.0, self.interval))
        except OSError as exc:
            self.error = str(exc)

    def stop(self):
        self.stop_event.set()


# ---------------------------------------------------------------------------
# DOM snapshot read inside the page (synchronous: runJavaScript cannot await)
# ---------------------------------------------------------------------------
JS_SNAPSHOT = r"""
(function(){
  try {
    var $ = function(id){ return document.getElementById(id); };
    var text = function(el){
      return el ? String(el.textContent || '').replace(/\s+/g, ' ').trim() : '';
    };
    var isVisible = function(el){
      if (!el) return null;
      var style = window.getComputedStyle(el);
      return !el.hidden && style.display !== 'none' && style.visibility !== 'hidden'
        && style.opacity !== '0';
    };
    var retainedText = text($('retained'));
    var retained = parseInt(retainedText.replace(/[^0-9]/g, ''), 10);
    if (!isFinite(retained)) retained = 0;
    var canvases = Array.prototype.slice.call(document.querySelectorAll('canvas'));
    var drawn = 0;
    for (var index = 0; index < canvases.length; index++) {
      var canvas = canvases[index];
      try {
        if (!canvas.width || !canvas.height) continue;
        var data = canvas.getContext('2d').getImageData(0, 0, canvas.width, canvas.height).data;
        var seen = {};
        var colors = 0;
        var step = Math.max(1, Math.floor(Math.sqrt(canvas.width * canvas.height / 500)));
        for (var y = 0; y < canvas.height; y += step) {
          for (var x = 0; x < canvas.width; x += step) {
            var offset = (y * canvas.width + x) * 4;
            if (data[offset + 3] < 8) continue;
            var key = (data[offset] >> 4) + ',' + (data[offset + 1] >> 4) + ','
              + (data[offset + 2] >> 4);
            if (!seen[key]) { seen[key] = true; colors++; }
          }
        }
        if (colors >= 3) drawn++;
      } catch (error) {}
    }
    var udpTexts = [];
    var nodes = document.querySelectorAll('[id^="udp-"]');
    for (var n = 0; n < nodes.length; n++) {
      var value = text(nodes[n]);
      if (value) udpTexts.push(nodes[n].id + '=' + value);
    }
    var debug = null;
    if (typeof window.ServalDebug === 'function') {
      try { debug = window.ServalDebug(); } catch (error) { debug = null; }
    }
    if (!debug || typeof debug !== 'object') debug = window.__SERVAL_DEBUG__ || null;
    var debugFrames = null;
    if (debug && typeof debug === 'object') {
      var candidate = debug.frames;
      if (candidate === undefined) candidate = debug.retained;
      if (typeof candidate === 'number' && isFinite(candidate)) debugFrames = candidate;
    }
    var tools = $('native-tools');
    var cameraFeed = $('camera-feed');
    return JSON.stringify({
      ready: document.readyState,
      title: document.title,
      status: text($('status')),
      statusDot: $('status-dot') ? String($('status-dot').className || '') : '',
      source: text($('source')),
      retainedText: retainedText,
      retained: retained,
      txRate: text($('tx-rate')),
      rxRate: text($('rx-rate')),
      yawValue: text($('yaw-value')),
      unitLabel: text($('unit-label')),
      viewRange: text($('view-range')),
      canvasCount: canvases.length,
      canvasesDrawn: drawn,
      nativeToolsPresent: !!tools,
      nativeToolsVisible: isVisible(tools),
      nativeStatus: text($('native-status')),
      udpTexts: udpTexts,
      debugFrames: debugFrames,
      debugKeys: debug ? Object.keys(debug) : [],
      debugTransport: debug ? (debug.transport || null) : null,
      pageError: text($('error')),
      connectionError: text($('connection-error')),
      desktopHint: window.ServalDesktop || null,
      cameraPanelVisible: isVisible($('camera-panel')),
      cameraNaturalWidth: cameraFeed && cameraFeed.naturalWidth ? cameraFeed.naturalWidth : 0,
      cameraFpsText: text($('camera-fps')),
      cameraSrc: cameraFeed ? String(cameraFeed.currentSrc || cameraFeed.src || '') : ''
    });
  } catch (error) {
    return JSON.stringify({scriptError: String((error && error.stack) || error)});
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
    """Screenshot (window.grab) needs software compositing.

    On Windows the contract keeps the GPU path for the normal window; the smoke
    script forces software rendering here because ``QWebEngineView.grab()``
    returns a black image with the default GPU compositor.  Existing user flags
    are kept and only the missing ones are appended.
    """
    if os.name != 'nt':
        return
    current = os.environ.get('QTWEBENGINE_CHROMIUM_FLAGS', '')
    flags = current.split()
    for flag in WINDOWS_CHROMIUM_FLAGS.split():
        if flag not in flags:
            flags.append(flag)
    os.environ['QTWEBENGINE_CHROMIUM_FLAGS'] = ' '.join(flags)


def _free_udp_port():
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
        probe.bind(('127.0.0.1', 0))
        return probe.getsockname()[1]


def _constructor_params(cls):
    try:
        import inspect
        return set(inspect.signature(cls.__init__).parameters)
    except (TypeError, ValueError):
        return set()


def _digits(value):
    if not value:
        return 0
    digits = ''.join(ch for ch in str(value) if ch.isdigit())
    try:
        return int(digits) if digits else 0
    except ValueError:
        return 0


def _fetch_snapshot(base_url):
    try:
        with urllib.request.urlopen(base_url + 'api/data?since=0', timeout=5) as response:
            payload = json.loads(response.read().decode('utf-8'))
        return payload if isinstance(payload, dict) else None
    except (OSError, ValueError):
        return None


def _write_synthetic_jpeg(path):
    """Write a small JPEG used as the fake vision camera preview."""
    try:
        from PyQt5.QtGui import QColor, QImage, QPainter
    except Exception:
        return False
    image = QImage(480, 270, QImage.Format_RGB32)
    image.fill(QColor('#0b1220'))
    painter = QPainter(image)
    try:
        painter.fillRect(40, 40, 250, 140, QColor('#163e4b'))
        painter.setPen(QColor('#38d9b6'))
        painter.drawRect(40, 40, 250, 140)
        painter.drawLine(40, 40, 290, 180)
        painter.drawLine(290, 40, 40, 180)
        painter.setPen(QColor('#dbe6f4'))
        painter.drawText(42, 222, 'SERVAL CAMERA SMOKE TEST')
        painter.setPen(QColor('#61b7ff'))
        painter.drawText(42, 244, 'synthetic JPEG · /api/camera.mjpg')
    finally:
        painter.end()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        return bool(image.save(str(path), 'JPEG', 85))
    except (OSError, ValueError):
        return False


class SmokeState:
    def __init__(self):
        self.checks = []        # (name, ok, detail, required)
        self.failures = []
        self.warnings = []
        self.js = None
        self.snapshot = None
        self.screenshot = None
        self.screenshot_error = None
        self.session = None
        self.done = False
        self.watchdog = False
        self.camera_loaded = False
        self.camera_screenshot = None

    def check(self, name, ok, detail='', required=True):
        ok = bool(ok)
        self.checks.append((name, ok, str(detail), required))
        if not ok:
            if required:
                self.failures.append(f'{name}: {detail}')
            else:
                self.warnings.append(f'{name}: {detail}')
        return ok


def _run_checks(args, state, screenshot_path):
    """GUI-thread callback: read DOM, assert, screenshot, quit."""
    session = state.session
    if session is None or state.done:
        return
    state.done = True

    def finalize():
        print_report(args, state)
        session.qt_app.quit()

    def camera_shot():
        from PyQt5.QtCore import QTimer
        target = screenshot_path.with_name('gui-smoke-camera.png')

        def shoot():
            try:
                pixmap = session.window.grab()
                saved = bool(pixmap.save(str(target), 'PNG'))
                state.camera_screenshot = str(target) if saved else None
                state.check('相机面板截图', saved and target.stat().st_size > 1000,
                            f'{target} {pixmap.width()}x{pixmap.height()} '
                            f'{target.stat().st_size if target.exists() else 0} B')
            except Exception as exc:
                state.check('相机面板截图', False, f'{target}: {exc}')
            finalize()

        QTimer.singleShot(600, shoot)

    def maybe_camera_shot():
        if not (args.camera and state.camera_loaded):
            return finalize()
        try:
            session.view.page().runJavaScript(
                "var el=document.getElementById('camera-panel');"
                "if(el){el.scrollIntoView({block:'center'});} true",
                lambda _value: camera_shot())
        except Exception:
            finalize()

    def finish(value):
        if value:
            try:
                state.js = json.loads(value)
            except (TypeError, ValueError):
                state.js = {'scriptError': 'runJavaScript 返回非 JSON：' + str(value)[:200]}
        else:
            state.js = {'scriptError': 'runJavaScript 没有返回值'}
        state.snapshot = _fetch_snapshot(session.url)
        _evaluate(args, state)
        _save_screenshot(session, state, screenshot_path)
        maybe_camera_shot()

    try:
        session.view.page().runJavaScript(JS_SNAPSHOT, finish)
    except Exception as exc:
        state.js = {'scriptError': f'runJavaScript 失败：{exc}'}
        state.snapshot = _fetch_snapshot(session.url)
        _evaluate(args, state)
        _save_screenshot(session, state, screenshot_path)
        finalize()


def _evaluate(args, state):
    js = state.js or {}
    snapshot = state.snapshot
    load_only = bool(args.load_only)

    def check(name, ok, detail='', required=True):
        return state.check(name, ok, detail, required)

    if js.get('scriptError'):
        check('页面 DOM 快照', False, js['scriptError'])
        return
    check('页面加载完成', js.get('ready') == 'complete' and state.session.page_loaded,
          f"ready={js.get('ready')} page_loaded={state.session.page_loaded} "
          f"load_error={state.session.load_error}")
    if args.watchdog_reason:
        state.warnings.append(f'总超时先于页面加载完成：{args.watchdog_reason}')

    status = (js.get('status') or '').strip()
    waiting = [word for word in WAITING_WORDS if word in status]
    live = [word for word in LIVE_WORDS if word in status]
    check('页面状态文本', bool(status) and not waiting and bool(live),
          f'status={status!r}' + (f' 含等待词={waiting}' if waiting else ''))

    if load_only:
        check('图表元素存在', (js.get('canvasCount') or 0) > 0,
              f"canvas={js.get('canvasCount')}")
        state.warnings.append('--load-only：跳过数据/速率/原生工具断言')
        return
    if snapshot is None:
        check('后端 /api/data 可用', False, '无法读取 /api/data（HTTP 后端未就绪）')
    else:
        frames = snapshot.get('frames')
        frames = frames if isinstance(frames, list) else []
        mode_name = snapshot.get('mode')
        expected_mode = 'demo' if args.demo else 'live_udp'
        check('后端数据帧', len(frames) >= 2, f'frames={len(frames)} mode={mode_name!r}')
        check('后端模式', mode_name == expected_mode,
              f'mode={mode_name!r}（期望 {expected_mode!r}）')
        if not args.demo:
            udp = snapshot.get('udp')
            check('后端 UDP 快照', isinstance(udp, dict) and bool(udp),
                  f'udp={type(udp).__name__} keys={sorted(udp)[:8] if isinstance(udp, dict) else None}')

    retained = js.get('retained') or 0
    debug_frames = js.get('debugFrames')
    page_frames = retained if retained else (debug_frames or 0)
    check('页面帧缓存', page_frames > 0,
          f"#retained={js.get('retainedText')!r} debugFrames={debug_frames}")
    tx_rate, rx_rate = _digits(js.get('txRate')), _digits(js.get('rxRate'))
    check('页面 TX/RX 速率', tx_rate > 0 and rx_rate > 0,
          f"tx={js.get('txRate')!r} rx={js.get('rxRate')!r}")
    yaw_value = (js.get('yawValue') or '').strip()
    check('页面姿态读数', bool(yaw_value) and yaw_value != '—', f'yaw={yaw_value!r}')
    check('图表画布已绘制', (js.get('canvasCount') or 0) > 0 and (js.get('canvasesDrawn') or 0) > 0,
          f"canvas={js.get('canvasCount')} drawn={js.get('canvasesDrawn')}")
    check('本地工具按钮可见',
          bool(js.get('nativeToolsPresent')) and js.get('nativeToolsVisible') is True,
          f"#native-tools present={js.get('nativeToolsPresent')} "
          f"visible={js.get('nativeToolsVisible')} status={js.get('nativeStatus')!r}")
    if not args.demo:
        udp_texts = js.get('udpTexts') or []
        if udp_texts:
            print(f"[gui-smoke] UDP DOM 指标: {' | '.join(udp_texts[:6])}", flush=True)
        else:
            state.warnings.append('未发现 id 以 "udp-" 开头的 DOM 指标元素（前端命名可能不同）')
    hint = js.get('desktopHint')
    if not (isinstance(hint, dict) and hint.get('native')):
        state.warnings.append(f'window.ServalDesktop 桌面窗口标记缺失：{hint!r}')
    if getattr(args, 'camera', False):
        panel_visible = js.get('cameraPanelVisible')
        natural_width = js.get('cameraNaturalWidth') or 0
        state.camera_loaded = bool(panel_visible) and natural_width > 0
        check('相机 MJPEG 面板', state.camera_loaded,
              f"#camera-panel visible={panel_visible} naturalWidth={natural_width} "
              f"fps={js.get('cameraFpsText')!r} src={js.get('cameraSrc')!r}")


def _save_screenshot(session, state, screenshot_path):
    try:
        pixmap = session.window.grab()
        saved = bool(pixmap.save(str(screenshot_path), 'PNG'))
        size = screenshot_path.stat().st_size if screenshot_path.exists() else 0
        state.screenshot = str(screenshot_path) if saved else None
        state.check('窗口截图', saved and size > 1000,
                    f'{screenshot_path} {pixmap.width()}x{pixmap.height()} {size} B')
    except Exception as exc:
        state.screenshot_error = str(exc)
        state.check('窗口截图', False, f'{screenshot_path}: {exc}')


def print_report(args, state):
    mode = 'demo' if args.demo else 'UDP 合成数据'
    print(f'[gui-smoke] 模式: {mode}' +
          ('（--load-only）' if args.load_only else ''), flush=True)
    if state.js:
        js = state.js
        print(f"[gui-smoke] 页面: title={js.get('title')!r} ready={js.get('ready')} "
              f"status={js.get('status')!r} source={js.get('source')!r}", flush=True)
        print(f"[gui-smoke] DOM: retained={js.get('retainedText')!r} "
              f"tx={js.get('txRate')!r} rx={js.get('rxRate')!r} "
              f"canvas={js.get('canvasCount')}/{js.get('canvasesDrawn')} "
              f"native-tools={js.get('nativeToolsPresent')}/{js.get('nativeToolsVisible')}", flush=True)
        if js.get('debugKeys'):
            print(f"[gui-smoke] window.ServalDebug keys: {js.get('debugKeys')}", flush=True)
        if args.camera:
            print(f"[gui-smoke] 相机: panel={js.get('cameraPanelVisible')} "
                  f"naturalWidth={js.get('cameraNaturalWidth')} "
                  f"fps={js.get('cameraFpsText')!r} src={js.get('cameraSrc')!r}", flush=True)
    if state.snapshot is not None:
        frames = state.snapshot.get('frames')
        print(f"[gui-smoke] 后端: mode={state.snapshot.get('mode')!r} "
              f"frames={len(frames) if isinstance(frames, list) else '?'} "
              f"retained={state.snapshot.get('retained')!r} "
              f"udp={'dict' if isinstance(state.snapshot.get('udp'), dict) else state.snapshot.get('udp')!r}",
              flush=True)
    required = [item for item in state.checks if item[3]]
    passed = sum(1 for item in required if item[1])
    for name, ok, detail, is_required in state.checks:
        tag = 'PASS' if ok else ('FAIL' if is_required else 'WARN')
        print(f'[gui-smoke] {tag} {name}' + (f' · {detail}' if detail else ''), flush=True)
    if state.screenshot:
        print(f'[gui-smoke] 截图: {state.screenshot}', flush=True)
    elif state.screenshot_error:
        print(f'[gui-smoke] 截图失败: {state.screenshot_error}', flush=True)
    if state.camera_screenshot:
        print(f'[gui-smoke] 相机截图: {state.camera_screenshot}', flush=True)
    print(f'[gui-smoke] 检查: {passed}/{len(required)} 通过, {len(state.failures)} 失败, '
          f'{len(state.warnings)} 提示', flush=True)
    if state.warnings:
        for warning in state.warnings:
            print(f'[gui-smoke] WARN {warning}', flush=True)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main(argv=None):
    _stdio_utf8()
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--demo', action='store_true',
                        help='use monitor.Application(demo=True) instead of synthetic UDP')
    parser.add_argument('--load-only', action='store_true',
                        help='only assert window/page load (skip data/native assertions)')
    parser.add_argument('--seconds', type=float, default=8.0,
                        help='synthetic UDP duration in seconds (default: 8)')
    parser.add_argument('--settle', type=int, default=2600,
                        help='milliseconds to wait after loadFinished before asserting (default: 2600)')
    parser.add_argument('--timeout', type=int, default=20000,
                        help='overall watchdog in milliseconds (default: 20000)')
    parser.add_argument('--screenshot', type=Path,
                        help='screenshot path (default: <temp>/gui-smoke.png)')
    parser.add_argument('--camera', action='store_true',
                        help='also test the camera MJPEG panel with a synthetic JPEG')
    parser.add_argument('--port', type=int,
                        help='fixed loopback UDP port (default: a free port)')
    args = parser.parse_args(argv)
    args.watchdog_reason = None

    _configure_webengine_environment()
    from lib.desktop_window import ensure_application, run_with_app, webengine_available
    if not webengine_available():
        print('[gui-smoke] SKIP: QtWebEngine/PyQt5 不可用，无法创建原生 GUI 窗口。\n'
              '  Windows: py -3 -m pip install -r tools/serval/requirements_native.txt\n'
              '  Linux  : sudo apt install -y python3-pyqt5 python3-pyqtgraph '
              'python3-pyqt5.qtwebengine', flush=True)
        return 0
    if os.name == 'posix' and not (os.environ.get('DISPLAY')
                                   or os.environ.get('WAYLAND_DISPLAY')):
        print('[gui-smoke] SKIP: 没有图形桌面（DISPLAY/WAYLAND_DISPLAY 缺失）。', flush=True)
        return 0

    from PyQt5.QtCore import QTimer
    import monitor

    screenshot_dir = Path(tempfile.mkdtemp(prefix='serval-gui-smoke-'))
    screenshot_path = (args.screenshot.resolve() if args.screenshot
                       else (screenshot_dir / 'gui-smoke.png'))
    screenshot_path.parent.mkdir(parents=True, exist_ok=True)

    params = _constructor_params(monitor.Application)
    if args.demo and 'demo' not in params:
        print('[gui-smoke] SKIP: monitor.Application 缺少 demo 参数（后端接口未就绪）。', flush=True)
        return 2
    if not args.demo and 'udp' not in params:
        print('[gui-smoke] SKIP: monitor.Application 尚未实现契约接口 udp=...（backend 任务未完成）。\n'
              '  后端就绪后重跑: python tools/serval/tests/gui_smoke.py\n'
              '  当前可先用:     python tools/serval/tests/gui_smoke.py --demo --load-only',
              flush=True)
        return 2
    camera_kwargs = {}
    if args.camera:
        if 'camera' not in params:
            print('[gui-smoke] SKIP: monitor.Application 缺少 camera 参数（后端接口未就绪）。',
                  flush=True)
            return 2
        ensure_application()
        camera_path = screenshot_dir / 'camera.jpg'
        if not _write_synthetic_jpeg(camera_path):
            print('[gui-smoke] ERROR: 无法生成相机测试 JPEG', file=sys.stderr, flush=True)
            return 2
        camera_kwargs = dict(camera=str(camera_path), preview_fps=60)

    sender = None
    try:
        if args.demo:
            app = monitor.Application(demo=True, **camera_kwargs)
        else:
            port = args.port or _free_udp_port()
            app = monitor.Application(udp=dict(host='127.0.0.1', port=port), **camera_kwargs)
            sender = SyntheticUdpSender(port, duration=args.seconds)
            sender.start()
            print(f'[gui-smoke] 合成 UDP 发送到 127.0.0.1:{port}，{args.seconds:.1f} 秒',
                  flush=True)
    except (OSError, ValueError, TypeError) as exc:
        print(f'[gui-smoke] ERROR: 无法创建 monitor.Application：{exc}', file=sys.stderr,
              flush=True)
        return 2

    state = SmokeState()

    def schedule_checks(session):
        state.session = session

        def watchdog():
            if state.done:
                return
            args.watchdog_reason = '总超时（页面未在限定时间内完成加载）'
            _run_checks(args, state, screenshot_path)

        def loaded(_ok=True):
            QTimer.singleShot(args.settle, lambda: _run_checks(args, state, screenshot_path))

        session.view.loadFinished.connect(loaded)
        QTimer.singleShot(args.timeout, watchdog)

    try:
        run_with_app(app, title='Serval GUI 冒烟测试', preview_fps=60,
                     on_ready=schedule_checks)
    except Exception as exc:
        print(f'[gui-smoke] ERROR: 桌面窗口运行失败：{exc}', file=sys.stderr, flush=True)
        return 2
    finally:
        if sender is not None:
            sender.stop()
            sender.join(timeout=2)

    failed = bool(state.failures) or state.done is False
    if state.done is False:
        print('[gui-smoke] FAIL: 检查从未执行（窗口提前关闭）', flush=True)
    if failed:
        print(f'[gui-smoke] FAIL（{len(state.failures)} 项失败）', flush=True)
        return 1
    print('[gui-smoke] PASS', flush=True)
    return 0


if __name__ == '__main__':
    sys.exit(main())
