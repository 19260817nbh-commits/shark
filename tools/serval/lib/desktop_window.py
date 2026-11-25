#!/usr/bin/env python3
"""Native QtWebEngine window that hosts the existing Serval web GUI.

This module implements the desktop part of ``docs/gui-design.md``
(sections 2, 3 and 8):

* :func:`webengine_available` detects PyQt5 + QtWebEngine without raising so a
  launcher can print an installation command instead of a traceback.
* :class:`NativeHost` bridges ``Application.native`` calls made from HTTP
  threads onto the Qt GUI thread (file dialogs, offline decode start, open
  directory, downloads) and hands the result back with a ``threading.Event``.
* :func:`run_desktop` starts ``monitor.create_server`` in a background thread,
  embeds ``http://127.0.0.1:<port>/`` in a :class:`QMainWindow` and blocks in
  the Qt event loop until the window is closed.

PyQt5 is imported lazily on purpose: the launchers must be able to run
``--help`` and report a missing ``python3-pyqt5.qtwebengine`` cleanly.
"""
import math
import os
import signal
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

INSTALL_HINT_LINUX = ('sudo apt install -y python3-pyqt5 python3-pyqtgraph '
                      'python3-pyqt5.qtwebengine')
INSTALL_HINT_WINDOWS = 'py -3 -m pip install -r tools/serval/requirements_native.txt'
DEFAULT_TITLE = 'Serval · 串口解算台'
WINDOW_SIZE = (1440, 920)
WINDOW_MIN_SIZE = (1000, 640)
REQUEST_TIMEOUT_S = 1800.0
CONSOLE_LOG_LIMIT = 100
PUSH_HZ_MIN = 20               # explicit --push-hz range
PUSH_HZ_MAX = 240
# QtWebEngine 5.15 (Chromium 83) renders at ~60 rAF/s even on a 165 Hz display
# (measured), so `auto` follows the screen but never pushes more than 60/s.
# High-refresh setups can still pass an explicit 20..240 value.
AUTO_PUSH_HZ_MIN = 30          # auto = screen refresh rate, clamped to engine cap
AUTO_PUSH_HZ_MAX = 60
BELOW_NORMAL_PRIORITY_CLASS = 0x00004000


class DesktopUnavailable(RuntimeError):
    """The QtWebEngine desktop window cannot be created on this machine."""


_QT = None
_APPLICATION = None


def install_hint():
    """Installation command matching the current platform."""
    if os.name == 'nt':
        return INSTALL_HINT_WINDOWS
    return INSTALL_HINT_LINUX


def webengine_available():
    """Return True when PyQt5 and QtWebEngine can be imported.

    No ``QApplication`` and no window is created, so this is safe to call
    before the launchers have validated anything else.
    """
    try:
        import PyQt5.QtCore            # noqa: F401
        import PyQt5.QtWidgets         # noqa: F401
        import PyQt5.QtWebEngineWidgets  # noqa: F401
    except Exception:
        return False
    return True


def prepare_chromium_flags():
    """Linux/ARM: prefer software compositing, but never override the user.

    QtWebEngine on Jetson renders more reliably with ``--disable-gpu``; the
    variable is only extended, an explicit user value always wins.  Windows
    keeps its default GPU path (only screenshots need software compositing and
    the smoke script sets that itself).
    """
    if not sys.platform.startswith('linux'):
        return
    current = os.environ.get('QTWEBENGINE_CHROMIUM_FLAGS', '')
    if '--disable-gpu' in current.split():
        return
    os.environ['QTWEBENGINE_CHROMIUM_FLAGS'] = (current + ' --disable-gpu').strip()


# Set the Linux/ARM Chromium flags at import time: QtWebEngine reads its flags
# when it initializes, and the launchers import this module before creating any
# QApplication or web view.
prepare_chromium_flags()


def screen_refresh_hz(qt_app=None):
    """Primary screen refresh rate as an int, or None when unavailable."""
    try:
        qt = _load_qt()
        application = qt_app or qt.QApplication.instance()
        if application is None:
            return None
        screen = application.primaryScreen()
        if screen is None:
            return None
        rate = float(screen.refreshRate())
    except (AttributeError, TypeError, ValueError, RuntimeError):
        return None
    if not math.isfinite(rate) or rate <= 0:
        return None
    return int(round(rate))


def resolve_push_hz(requested, screen_hz=None):
    """Resolve ``--push-hz auto|N`` to a concrete integer rate.

    ``auto`` follows the screen refresh rate (clamped 30..240, fallback 60);
    an explicit value is validated against 20..240.
    """
    if isinstance(requested, str) and requested.strip().lower() == 'auto':
        base = screen_hz if screen_hz else 60
        return max(AUTO_PUSH_HZ_MIN, min(AUTO_PUSH_HZ_MAX, int(base)))
    try:
        value = int(requested)
    except (TypeError, ValueError):
        raise ValueError(
            f'push_hz 必须是 auto 或 {PUSH_HZ_MIN}..{PUSH_HZ_MAX} 的整数：{requested!r}')
    if not PUSH_HZ_MIN <= value <= PUSH_HZ_MAX:
        raise ValueError(f'push_hz 必须在 {PUSH_HZ_MIN}..{PUSH_HZ_MAX} 之间：{value}')
    return value


def apply_low_priority():
    """Lower this GUI process' scheduling priority and describe the result.

    The Jetson launcher must call this *after* spawning the vision child so the
    child keeps the default priority (it inherits the launcher's priority).
    Failures are reported, never raised: low interference is best effort.
    """
    if os.name == 'nt':
        try:
            import ctypes
            kernel32 = ctypes.windll.kernel32
            kernel32.GetCurrentProcess.restype = ctypes.c_void_p
            kernel32.SetPriorityClass.argtypes = [ctypes.c_void_p, ctypes.c_uint]
            kernel32.SetPriorityClass.restype = ctypes.c_int
            handle = kernel32.GetCurrentProcess()
            if kernel32.SetPriorityClass(handle, BELOW_NORMAL_PRIORITY_CLASS):
                return '进程优先级 BELOW_NORMAL_PRIORITY_CLASS（低于普通）'
            return '设置 BELOW_NORMAL_PRIORITY_CLASS 失败，已忽略'
        except Exception as exc:      # ctypes/windll missing or policy blocked
            return f'设置低优先级失败，已忽略：{exc}'
    if hasattr(os, 'nice'):
        try:
            before = os.nice(0)
            after = os.nice(10)
            return f'进程 nice {before} → {after}（GUI 线程低于默认优先级）'
        except OSError as exc:
            return f'设置 nice 失败，已忽略：{exc}'
    return '当前平台不支持调整进程优先级，已忽略'


def _load_qt():
    """Import and cache every Qt symbol used by this module."""
    global _QT
    if _QT is not None:
        return _QT
    try:
        from PyQt5.QtCore import QObject, QTimer, QUrl, pyqtSignal
        from PyQt5.QtGui import QDesktopServices
        from PyQt5.QtWidgets import QApplication, QFileDialog, QMainWindow
        from PyQt5.QtWebEngineWidgets import (QWebEnginePage, QWebEngineProfile,
                                              QWebEngineScript, QWebEngineView)
    except Exception as exc:  # ImportError or a broken Qt installation
        prefix = ''
        try:
            from PyQt5.QtWidgets import QApplication
            if QApplication.instance() is not None:
                prefix = ('QtWebEngine 必须在创建 QApplication 之前导入；'
                          '请让 run_desktop/run_with_app 创建窗口。\n')
        except Exception:
            pass
        raise DesktopUnavailable(prefix +
            'QtWebEngine 不可用：{}\n安装命令：{}'.format(exc, install_hint())) from exc
    _QT = SimpleNamespace(
        QObject=QObject, QTimer=QTimer, QUrl=QUrl, pyqtSignal=pyqtSignal,
        QDesktopServices=QDesktopServices, QApplication=QApplication,
        QFileDialog=QFileDialog, QMainWindow=QMainWindow,
        QWebEnginePage=QWebEnginePage, QWebEngineProfile=QWebEngineProfile,
        QWebEngineScript=QWebEngineScript, QWebEngineView=QWebEngineView)
    return _QT


def ensure_application(argv=None):
    """Return the existing QApplication or create one (never two).

    A module-level strong reference is kept: if the caller discards the return
    value, Python garbage collection would otherwise destroy the C++
    QApplication and later font/dialog calls would crash.
    """
    global _APPLICATION
    qt = _load_qt()
    application = qt.QApplication.instance()
    if application is None:
        application = _APPLICATION = qt.QApplication(list(argv or sys.argv[:1]))
    elif _APPLICATION is None:
        _APPLICATION = application
    application.setApplicationName('Serval Scope')
    return application


def records_dir():
    """Default directory for saved/recorded/decoded serial files.

    It follows ``monitor.ROOT`` so the dialog defaults and the backend's own
    automatic paths stay in the same place.
    """
    try:
        import monitor  # launchers put tools/serval on sys.path
        root = Path(monitor.ROOT).resolve()
    except Exception:
        root = Path(__file__).resolve().parents[1]
    return root / 'records' / 'serial'


def _timestamp():
    return time.strftime('%Y%m%d_%H%M%S') + '_' + f'{time.time_ns() % 1_000_000:06d}'


def _monitor_module():
    import importlib
    return importlib.import_module('monitor')


def create_http_server(app, host='127.0.0.1', port=0):
    """Create the backend HTTP server (``monitor.create_server`` contract).

    Falls back to the same stdlib handler/server when an older ``monitor.py``
    without ``create_server`` is being used, which keeps ``--demo`` smoke runs
    possible while the backend is still being implemented.
    """
    monitor = _monitor_module()
    factory = getattr(monitor, 'create_server', None)
    if callable(factory):
        return factory(app, host, port)
    from http.server import ThreadingHTTPServer

    class _CompatServer(ThreadingHTTPServer):
        daemon_threads = True
        protocol_version = 'HTTP/1.1'

    return _CompatServer((host, port), monitor.make_handler(app))


class _NativeRequest:
    """A GUI-thread job created by an HTTP thread."""

    __slots__ = ('action', 'payload', 'event', 'result', 'error')

    def __init__(self, action, payload):
        self.action = action
        self.payload = dict(payload or {})
        self.event = threading.Event()
        self.result = None
        self.error = None


_BRIDGE_CLASS = None
_PAGE_CLASS = None
_WINDOW_CLASS = None


def _bridge_class():
    """Lazily define the QObject carrying cross-thread requests."""
    global _BRIDGE_CLASS
    if _BRIDGE_CLASS is not None:
        return _BRIDGE_CLASS
    qt = _load_qt()

    class _NativeBridge(qt.QObject):
        requested = qt.pyqtSignal(object)

        def __init__(self, host):
            super().__init__()
            self.host = host
            # Connection created in the GUI thread: emitting from an HTTP
            # thread gives a queued invocation on the GUI thread.
            self.requested.connect(self._dispatch)

        def _dispatch(self, request):
            self.host._execute(request)

    _BRIDGE_CLASS = _NativeBridge
    return _BRIDGE_CLASS


def _page_class():
    """Lazily define a page that forwards JS console output to stderr."""
    global _PAGE_CLASS
    if _PAGE_CLASS is not None:
        return _PAGE_CLASS
    qt = _load_qt()

    class _ConsolePage(qt.QWebEnginePage):
        def __init__(self, parent=None):
            super().__init__(parent)
            self._last_message = None
            self._repeated = 0
            self._printed = 0

        def javaScriptConsoleMessage(self, level, message, line, source):
            text = f'{source}:{line}: {message}'
            if text == self._last_message:
                self._repeated += 1
                return
            if self._last_message is not None and self._repeated:
                print(f'[页面控制台] 上一条消息重复 {self._repeated} 次',
                      file=sys.stderr, flush=True)
            self._last_message, self._repeated = text, 0
            if self._printed > CONSOLE_LOG_LIMIT:
                return
            if self._printed == CONSOLE_LOG_LIMIT:
                self._printed += 1
                print('[页面控制台] 消息过多，后续输出已静音', file=sys.stderr, flush=True)
                return
            self._printed += 1
            label = {0: '信息', 1: '警告', 2: '错误'}.get(getattr(level, 'value', level), str(level))
            print(f'[页面控制台/{label}] {text}', file=sys.stderr, flush=True)

    _PAGE_CLASS = _ConsolePage
    return _PAGE_CLASS


def _window_class():
    """Lazily define the main window so importing this module needs no Qt."""
    global _WINDOW_CLASS
    if _WINDOW_CLASS is not None:
        return _WINDOW_CLASS
    qt = _load_qt()

    class _DesktopWindow(qt.QMainWindow):
        def __init__(self, session):
            super().__init__()
            self.session = session

        def closeEvent(self, event):
            self.session.request_close()
            event.accept()

    _WINDOW_CLASS = _DesktopWindow
    return _WINDOW_CLASS


class NativeHost:
    """``Application.native`` host for the native quick actions (§8).

    :meth:`handle` is called from HTTP worker threads.  Every action that needs
    widgets is queued onto the Qt GUI thread through a ``pyqtSignal(object)``;
    the HTTP thread blocks on a ``threading.Event`` only until the dialog has
    been confirmed/cancelled.  The offline decode child process itself runs on
    the HTTP thread so the window stays responsive for large logs.
    """

    def __init__(self, app, window):
        _load_qt()
        self.app = app
        self.window = window
        self.last_saved = None          # last saved/recorded/decoded path
        self.downloads = []             # keep QWebEngineDownloadItem alive
        self.profile = None
        self.bridge = _bridge_class()(self)
        self._install_download_handler()

    # -- HTTP thread side ---------------------------------------------------
    def handle(self, action, payload=None):
        action = str(action or '').strip()
        payload = dict(payload or {})
        if action == 'status':
            return self.status()
        if action == 'decode':
            return self._decode(payload)
        request = _NativeRequest(action, payload)
        self.bridge.requested.emit(request)
        if not request.event.wait(REQUEST_TIMEOUT_S):
            raise ValueError('原生对话框等待超时（桌面窗口无响应）')
        if request.error is not None:
            raise ValueError(str(request.error))
        return request.result

    def _decode(self, payload):
        """Pick the file on the GUI thread, then decode on this HTTP thread.

        ``app.run_offline_decode`` waits for a child process (up to 300 s for a
        large log); running it on the GUI thread would freeze the window.  Only
        the ``QFileDialog`` is queued to the GUI thread here.
        """
        path = payload.get('path')
        plot = bool(payload.get('plot', True))
        out_dir = payload.get('out_dir') or None
        if path:
            chosen = Path(str(path)).expanduser()
            if not chosen.is_file():
                raise ValueError(f'离线解算文件不存在：{path}')
            path = str(chosen.resolve())
        else:
            request = _NativeRequest('decode-pick', {})
            self.bridge.requested.emit(request)
            if not request.event.wait(REQUEST_TIMEOUT_S):
                raise ValueError('原生文件对话框等待超时（桌面窗口无响应）')
            if request.error is not None:
                raise ValueError(str(request.error))
            picked = request.result if isinstance(request.result, dict) else {}
            if picked.get('cancelled') or not picked.get('path'):
                return {'cancelled': True}
            path = picked['path']
        result = self.app.run_offline_decode(path, plot=plot, out_dir=out_dir)
        if not isinstance(result, dict):
            result = {'path': path}
        self._remember(result.get('path') or path)
        return result

    def status(self):
        try:
            state = dict(self.app.native_status())
        except AttributeError:
            state = {'recording': None, 'last_decode': None,
                     'mode': getattr(self.app, 'mode', None), 'capabilities': []}
        state['available'] = True
        if not state.get('capabilities'):
            state['capabilities'] = ['save', 'record', 'decode', 'open']
        return state

    # -- GUI thread side ----------------------------------------------------
    def _execute(self, request):
        try:
            method = getattr(self, '_action_' + request.action.replace('-', '_'), None)
            if method is None:
                raise ValueError(f'不支持的原生操作：{request.action}')
            request.result = method(request.payload)
        except Exception as exc:  # delivered to the waiting HTTP thread
            request.error = exc
        finally:
            request.event.set()

    def _ask_save(self, title, default_name, file_filter='串口记录 (*.bin)'):
        qt = _load_qt()
        start = records_dir() / default_name
        start.parent.mkdir(parents=True, exist_ok=True)
        path, _ = qt.QFileDialog.getSaveFileName(self.window, title, str(start), file_filter)
        if not path:
            return None
        path = Path(path).expanduser()
        if not path.suffix:
            path = path.with_name(path.name + '.bin')
        return str(path.resolve())

    def _ask_open(self, title):
        qt = _load_qt()
        last = self.last_saved
        if last is not None and Path(last).is_file():
            initial = str(last)
        else:
            directory = records_dir()
            directory.mkdir(parents=True, exist_ok=True)
            initial = str(directory)
        path, _ = qt.QFileDialog.getOpenFileName(
            self.window, title, initial, '串口记录 (*.bin);;所有文件 (*)')
        return str(Path(path).resolve()) if path else None

    def _remember(self, path):
        try:
            self.last_saved = Path(str(path)).expanduser().resolve()
        except (OSError, ValueError):
            pass

    def _action_save(self, payload):
        requested = payload.get('path')
        if requested:
            path = str(Path(str(requested)).expanduser().resolve())
        else:
            path = self._ask_save('保存原始串口包', 'serial_' + _timestamp() + '.bin')
        if path is None:
            return {'cancelled': True}
        result = self.app.save_cache(path)
        if not isinstance(result, dict):
            result = {'path': path}
        self._remember(result.get('path') or path)
        return result

    def _action_record_start(self, payload):
        requested = payload.get('path')
        if requested:
            path = str(Path(str(requested)).expanduser().resolve())
        else:
            path = self._ask_save('选择录制文件', 'serial_record_' + _timestamp() + '.bin')
        if path is None:
            return {'cancelled': True}
        snapshot = self.app.start_recording(path)
        self._remember(path)
        return {'recording': snapshot}

    def _action_record_stop(self, payload):
        snapshot = self.app.stop_recording()
        if isinstance(snapshot, dict) and snapshot.get('path'):
            self._remember(snapshot['path'])
        return {'recording': snapshot}

    def _action_decode_pick(self, payload):
        """GUI-thread half of the decode action: only choose the .bin file."""
        path = self._ask_open('选择要离线解算的 .bin 日志')
        return {'cancelled': True} if path is None else {'path': path}

    def _action_open(self, payload):
        qt = _load_qt()
        raw = payload.get('path')
        if raw:
            target = Path(str(raw)).expanduser()
        elif self.last_saved is not None:
            target = Path(self.last_saved).parent
        else:
            target = records_dir()
            try:
                target.mkdir(parents=True, exist_ok=True)
            except OSError:
                return {'ok': False}
        if not target.exists():
            return {'ok': False}
        if target.is_file():
            target = target.parent
        opened = qt.QDesktopServices.openUrl(qt.QUrl.fromLocalFile(str(target)))
        return {'ok': bool(opened)}

    # -- downloads ----------------------------------------------------------
    def _install_download_handler(self):
        qt = _load_qt()
        try:
            profile = qt.QWebEngineProfile.defaultProfile()
        except Exception as exc:
            print(f'[下载] 无法访问默认 profile：{exc}', file=sys.stderr, flush=True)
            return
        self.profile = profile
        profile.downloadRequested.connect(self._on_download_requested)

    def _on_download_requested(self, download):
        """Take over page downloads instead of letting them fail silently."""
        qt = _load_qt()
        try:
            # The item is owned by QtWebEngine; keep the wrapper referenced
            # until it finished, otherwise the download callback can crash.
            self.downloads.append(download)
            suggested = download.suggestedFileName() or ('serval_' + _timestamp() + '.bin')
            target = records_dir() / suggested
            try:
                target.parent.mkdir(parents=True, exist_ok=True)
            except OSError:
                pass
            path, _ = qt.QFileDialog.getSaveFileName(
                self.window, '保存下载文件', str(target), '所有文件 (*)')
            if not path:
                download.cancel()
                return
            final = Path(path).expanduser().resolve()
            try:
                download.setPath(str(final))
            except AttributeError:  # older Qt API
                download.setDownloadDirectory(str(final.parent))
                download.setDownloadFileName(final.name)
            download.finished.connect(lambda item=download: self._forget_download(item))
            download.accept()
            print(f'[下载] {final}', flush=True)
        except Exception as exc:
            print(f'[下载] 拦截保存失败：{exc}', file=sys.stderr, flush=True)
            try:
                download.cancel()
            except Exception:
                pass

    def _forget_download(self, download):
        try:
            self.downloads.remove(download)
        except ValueError:
            pass

    def close(self):
        profile = self.profile
        self.profile = None
        if profile is not None:
            try:
                profile.downloadRequested.disconnect(self._on_download_requested)
            except (TypeError, RuntimeError):
                pass
        for download in list(self.downloads):
            try:
                if not download.isFinished():
                    download.cancel()
            except Exception:
                pass
        self.downloads.clear()


class DesktopSession:
    """Owns the HTTP server thread, the window and the embedded page.

    ``run_desktop`` drives one of these from start to shutdown; the smoke
    script uses it through ``run_with_app(..., on_ready=...)``.
    """

    def __init__(self, app, title=DEFAULT_TITLE, preview_fps=60,
                 http_host='127.0.0.1', http_port=0, push_hz='auto'):
        prepare_chromium_flags()
        self.app = app
        self.qt = _load_qt()
        self.qt_app = ensure_application()
        self.title = title
        self.preview_fps = int(preview_fps)
        self.http_host = http_host
        self.http_port = int(http_port)
        self.push_hz_requested = push_hz
        self.push_hz = None
        self.screen_hz = None
        self.server = None
        self.server_thread = None
        self.url = None
        self.window = None
        self.view = None
        self.native = None
        self.page_loaded = False
        self.load_error = None
        self._closed = False
        self._closing = False
        self._app_close_done = False
        self._keepalive = None
        self._old_handlers = {}

    def _resolve_push_hz(self):
        """Resolve push rate and write ``app.push_hz`` before create_server()."""
        self.screen_hz = screen_refresh_hz(self.qt_app)
        self.push_hz = resolve_push_hz(self.push_hz_requested, self.screen_hz)
        explicit = not (isinstance(self.push_hz_requested, str)
                        and self.push_hz_requested.strip().lower() == 'auto')
        if explicit:
            screen = f'屏幕 {self.screen_hz} Hz · 显式指定' if self.screen_hz else '显式指定'
        elif self.screen_hz is None:
            screen = '屏幕刷新率未知，回退 60 Hz'
        else:
            screen = f'屏幕 {self.screen_hz} Hz'
        # The backend reads this attribute in the SSE loop / snapshot; setting it
        # here (before the server starts and the page opens the stream) keeps the
        # timing deterministic even when Application.push_hz does not exist yet.
        try:
            setattr(self.app, 'push_hz', self.push_hz)
        except (AttributeError, TypeError) as exc:
            print(f'[GUI] 无法写入 app.push_hz：{exc}', file=sys.stderr, flush=True)
        print(f'[GUI] 推送刷新率 = {self.push_hz} Hz（{screen}）', flush=True)
        return self.push_hz

    # -- start --------------------------------------------------------------
    def start(self):
        self._resolve_push_hz()
        self.server = create_http_server(self.app, self.http_host, self.http_port)
        port = self.server.server_address[1]
        host = self.http_host
        if host in ('', '0.0.0.0', '::'):
            host = '127.0.0.1'
        self.url = f'http://{host}:{port}/'
        self.server_thread = threading.Thread(
            target=self.server.serve_forever, kwargs={'poll_interval': 0.1},
            name='serval-http', daemon=True)
        self.server_thread.start()

        qt = self.qt
        self.window = _window_class()(self)
        self.window.setWindowTitle(self.title)
        self.window.resize(*WINDOW_SIZE)
        self.window.setMinimumSize(*WINDOW_MIN_SIZE)
        self.view = qt.QWebEngineView(self.window)
        page = _page_class()(self.view)
        self.view.setPage(page)
        self.window.setCentralWidget(self.view)
        self._inject_desktop_hint(page)

        page.loadFinished.connect(self._on_load_finished)
        page.renderProcessTerminated.connect(self._on_render_terminated)

        # The backend serves /api/native/status from this object; it must exist
        # before the page (and web/native.js) is loaded.
        self.native = NativeHost(self.app, self.window)
        self.app.native = self.native
        self.view.load(qt.QUrl(self.url))
        print(f'[GUI] QtWebEngine 窗口 {self.title} · {self.url}', flush=True)
        self.window.show()
        self._install_signal_handlers()
        self._start_keepalive()
        return self

    def _inject_desktop_hint(self, page):
        qt = self.qt
        try:
            script = qt.QWebEngineScript()
            script.setName('serval-desktop-hint')
            script.setInjectionPoint(qt.QWebEngineScript.DocumentCreation)
            script.setWorldId(qt.QWebEngineScript.MainWorld)
            script.setRunsOnSubFrames(False)
            script.setSourceCode(
                'window.ServalDesktop = Object.freeze('
                '{"native": true, "preview_fps": %d, "push_hz": %d, '
                '"host": "qtwebengine"});'
                % (self.preview_fps, self.push_hz or 60))
            page.scripts().insert(script)
        except Exception as exc:
            print(f'[GUI] 无法注入桌面窗口标记：{exc}', file=sys.stderr, flush=True)

    def _on_load_finished(self, ok):
        self.page_loaded = bool(ok)
        if ok:
            print('[GUI] 页面加载完成', flush=True)
            return
        self.load_error = '页面加载失败'
        print('[GUI] 页面加载失败，请检查后端 monitor.create_server 是否正常',
              file=sys.stderr, flush=True)

    def _on_render_terminated(self, status, code):
        self.load_error = f'渲染进程异常退出（status={status}, code={code}）'
        print(f'[GUI] {self.load_error}', file=sys.stderr, flush=True)

    # -- signal handling ----------------------------------------------------
    def _install_signal_handlers(self):
        if threading.current_thread() is not threading.main_thread():
            return
        for name in ('SIGINT', 'SIGTERM'):
            signum = getattr(signal, name, None)
            if signum is None:
                continue
            try:
                previous = signal.getsignal(signum)
                signal.signal(signum, self._on_signal)
            except (ValueError, OSError, RuntimeError):
                continue
            self._old_handlers[signum] = previous

    def _on_signal(self, signum, frame):
        print(f'[GUI] 收到信号 {signum}，关闭窗口并清理', file=sys.stderr, flush=True)
        self.request_close()

    def _restore_signal_handlers(self):
        for signum, previous in list(self._old_handlers.items()):
            try:
                signal.signal(signum, previous)
            except (ValueError, OSError, RuntimeError):
                pass
        self._old_handlers.clear()

    def _start_keepalive(self):
        qt = self.qt
        self._keepalive = qt.QTimer(self.window)
        self._keepalive.timeout.connect(self._keepalive_tick)
        self._keepalive.start(200)

    def _keepalive_tick(self):
        """Empty tick: gives the Python interpreter a chance to run signal handlers."""

    # -- shutdown -----------------------------------------------------------
    def request_close(self):
        """Called from the window closeEvent / a signal handler."""
        if self._closed or self._closing:
            return
        self._closing = True
        self.qt_app.quit()

    def shutdown(self):
        if self._closed:
            return
        self._closed = True
        if self._keepalive is not None:
            try:
                self._keepalive.stop()
            except (RuntimeError, TypeError):
                pass
            self._keepalive = None
        self._restore_signal_handlers()
        if self.native is not None:
            try:
                self.native.close()
            except Exception as exc:
                print(f'[GUI] 关闭原生宿主时出错：{exc}', file=sys.stderr, flush=True)
            self.native = None
        if self.window is not None:
            try:
                self.window.hide()
            except RuntimeError:
                pass
        server, thread = self.server, self.server_thread
        self.server = self.server_thread = None
        if server is not None:
            try:
                server.shutdown()
            except Exception as exc:
                print(f'[GUI] 停止 HTTP 服务时出错：{exc}', file=sys.stderr, flush=True)
            if thread is not None:
                thread.join(timeout=5)
            try:
                server.server_close()
            except Exception:
                pass
        self._close_application()

    def _close_application(self):
        if self._app_close_done:
            return
        self._app_close_done = True
        try:
            self.app.close()
        except Exception as exc:
            print(f'[GUI] 关闭后端时出错：{exc}', file=sys.stderr, flush=True)


def run_desktop(app, title=DEFAULT_TITLE, preview_fps=60,
                http_host='127.0.0.1', http_port=0, push_hz='auto', *, on_ready=None):
    """Open the web GUI in a native window and block until it is closed.

    ``app`` is a ``monitor.Application`` (the launchers pass one built with
    ``udp=...`` / ``camera=...``).  ``push_hz`` is ``'auto'`` (follow the
    screen refresh rate, clamped 30..240) or an explicit 20..240 value; it is
    written to ``app.push_hz`` before the HTTP server starts.
    ``on_ready`` is an optional callable that receives the started
    :class:`DesktopSession`; it exists so ``tests/gui_smoke.py`` and
    ``tests/high_rate_bench.py`` can drive assertions with QTimer.
    """
    _load_qt()
    prepare_chromium_flags()
    application = ensure_application()
    session = DesktopSession(app, title=title, preview_fps=preview_fps,
                             http_host=http_host, http_port=http_port,
                             push_hz=push_hz)
    try:
        session.start()
        if on_ready is not None:
            on_ready(session)
        if hasattr(application, 'exec'):
            return application.exec()
        return application.exec_()
    finally:
        session.shutdown()


def run_with_app(app, **kwargs):
    """Stable entry point for tests/smoke scripts; equivalent to run_desktop."""
    return run_desktop(app, **kwargs)
