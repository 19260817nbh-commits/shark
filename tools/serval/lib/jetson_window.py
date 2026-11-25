"""Jetson desktop panel for live, read-only serial telemetry."""
from functools import partial
import math
import os
from pathlib import Path
import sys
import time

import numpy as np
import pyqtgraph as pg
from pyqtgraph.Qt import QtCore, QtGui, QtWidgets as Qt

from .live_udp import LiveStore, Receiver
from .protocol import ProtocolError, check_crc16
from .recording import HEADER, decode_record

AXES = ('yaw', 'pitch')
CHANNELS = (('角度', '', ''), ('角速度', '_vel', '/s'), ('角加速度', '_acc', '/s²'))
DIRECTIONS = ('TX', 'RX')
COLORS = ('#77b6ff', '#66e2c4')
STYLE = '''
QWidget { background: #0d1521; color: #dbe7f4; font-size: 13px; }
QFrame#card { background: #152237; border: 1px solid #293e57; border-radius: 8px; }
QFrame#card QLabel { background: transparent; }
QLabel[muted="true"] { color: #9aadbf; }
QLabel#title { font-size: 23px; font-weight: 700; }
QLabel#metric { font-size: 19px; font-weight: 600; }
QPushButton, QComboBox { background: #192a3f; border: 1px solid #354f6d;
    border-radius: 6px; padding: 6px 10px; }
QPushButton:hover, QComboBox:hover { border-color: #77b6ff; }
QPushButton:checked, QPushButton#primary { background: #193f52; border-color: #4586a0; }
QPushButton:disabled { color: #788fa6; }
QTableWidget, QPlainTextEdit { border: 1px solid #273a51; }
QTableWidget, QPlainTextEdit { background: #101b2a; alternate-background-color: #152136;
    selection-background-color: #234764; }
QHeaderView::section { background: #192a3f; padding: 4px; border: none; }
QScrollBar:vertical { background: #101b2a; width: 10px; margin: 0; }
QScrollBar::handle:vertical { background: #37506a; border-radius: 4px; min-height: 24px; }
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }
'''


def combo(entries, index=0):
    widget = Qt.QComboBox()
    for label, value in entries:
        widget.addItem(label, value)
    widget.setCurrentIndex(index)
    return widget


def curve_samples(times, values, modes, gaps, *, command, angle):
    """Draw the TX hold and discontinuities while keeping the source samples intact."""
    breaks = gaps.copy()
    breaks[1:] |= np.diff(times) > .1
    if command:
        breaks[1:] |= modes[1:] != modes[:-1]
    if angle:
        breaks[1:] |= np.abs(np.diff(values)) > math.pi
    if not command and not breaks.any():
        return times, values
    preceding = np.full_like(values, np.nan)
    if command:
        preceding[1:] = np.where(breaks[1:], np.nan, values[:-1])
    x = np.repeat(times, 2)
    y = np.column_stack((preceding, values)).ravel()
    if command:
        return x, y
    keep = np.ones(len(x), dtype=bool)
    keep[::2] = breaks
    return x[keep], y[keep]


def packet_fields(raw):
    _, direction, status, length, ns, seq, _ = HEADER.unpack_from(raw)
    payload = raw[HEADER.size:]
    return direction, status, length, ns, seq, payload, check_crc16(payload)


class CameraPanel(Qt.QWidget):
    def __init__(self, path, target_fps, heading):
        super().__init__()
        self.path = path
        self.image = QtGui.QImage()
        self.signature = None
        self.last_frame = None
        self.target_fps = target_fps
        self.heading = heading
        self.frames = 0
        self.rate_start = time.monotonic()
        self.setMinimumHeight(120)
        self.timer = QtCore.QTimer(self)
        self.timer.setTimerType(QtCore.Qt.PreciseTimer)
        self.timer.timeout.connect(self.refresh_image)
        self.timer.start(1000 // target_fps)

    def refresh_image(self):
        now = time.monotonic()
        try:
            with self.path.open('rb') as frame:
                stat = os.fstat(frame.fileno())
                signature = (stat.st_ino, stat.st_mtime_ns)
                if stat.st_size and signature != self.signature:
                    image = QtGui.QImage.fromData(frame.read(), b'JPEG')
                    self.signature = signature
                    if not image.isNull():
                        self.image = image
                        self.last_frame = now
                        self.frames += 1
                        self.update()
        except OSError:
            pass
        if not self.image.isNull() and now - self.last_frame > 1.0:
            self.image = QtGui.QImage()
            self.update()
        elapsed = now - self.rate_start
        if elapsed >= 1.0:
            rate = '—' if self.image.isNull() else f'{self.frames / elapsed:.0f}'
            self.heading.setText(f'相机预览 · {rate} / {self.target_fps} FPS')
            self.frames = 0
            self.rate_start = now

    def paintEvent(self, event):
        painter = QtGui.QPainter(self)
        painter.fillRect(self.rect(), QtGui.QColor('#09121d'))
        if self.image.isNull():
            painter.setPen(QtGui.QColor('#9aadbf'))
            message = '等待相机画面' if self.last_frame is None else '相机画面已停止更新'
            painter.drawText(self.rect(), QtCore.Qt.AlignCenter, message)
            return
        size = self.image.size().scaled(self.size(), QtCore.Qt.KeepAspectRatio)
        target = QtCore.QRect(QtCore.QPoint(), size)
        target.moveCenter(self.rect().center())
        painter.drawImage(target, self.image)


class ScopeWindow(Qt.QMainWindow):
    def __init__(self, args):
        super().__init__()
        self.store = LiveStore()
        self.receiver = Receiver(self.store, args.host, args.port, args.source_ip)
        self.drawn = None
        self.redraw = True
        self.session = None
        self.timestamps = {0: np.empty(0), 1: np.empty(0)}
        self.cursor = None
        self.cursor_channel = 0
        self.markers = {}
        self.last_status = 0.
        self.packets = []
        self.last_saved = None
        self.recorder = None
        self.setWindowTitle('Serval · 串口示波器')
        self.resize(1320, 820)
        self.setMinimumSize(900, 570)

        central = Qt.QWidget()
        self.setCentralWidget(central)
        layout = Qt.QVBoxLayout(central)
        layout.setContentsMargins(14, 12, 14, 10)
        layout.setSpacing(10)
        layout.addLayout(self.build_header())
        layout.addLayout(self.build_metrics())
        panels = Qt.QSplitter(QtCore.Qt.Horizontal)
        panels.addWidget(self.build_scope())
        packets = self.build_packets()
        if args.camera_preview is None:
            panels.addWidget(packets)
        else:
            side = Qt.QSplitter(QtCore.Qt.Vertical)
            camera_view = Qt.QWidget()
            camera_layout = Qt.QVBoxLayout(camera_view)
            heading = Qt.QLabel('相机预览')
            heading.setStyleSheet('font-size:16px; font-weight:600;')
            camera_layout.addWidget(heading)
            camera_layout.addWidget(CameraPanel(args.camera_preview, args.preview_fps, heading), 1)
            side.addWidget(camera_view)
            side.addWidget(packets)
            side.setChildrenCollapsible(False)
            side.setSizes([230, 400])
            panels.addWidget(side)
        panels.setChildrenCollapsible(False)
        panels.setSizes([890, 380])
        panels.setStretchFactor(0, 3)
        panels.setStretchFactor(1, 1)
        layout.addWidget(panels, 1)
        self.connection = Qt.QLabel()
        self.connection.setProperty('muted', True)
        layout.addWidget(self.connection)
        self.record_status = Qt.QLabel()
        self.record_status.setWordWrap(True)
        self.record_status.setTextInteractionFlags(QtCore.Qt.TextSelectableByMouse)
        self.record_status.hide()
        layout.addWidget(self.record_status)

        self.timer = QtCore.QTimer(self)
        self.timer.timeout.connect(self.tick)
        self.timer.start(round(1000 / args.refresh))

    def build_header(self):
        row = Qt.QHBoxLayout()
        title = Qt.QLabel('Serval 串口示波器')
        title.setObjectName('title')
        row.addWidget(title)
        row.addStretch()
        self.record_button = Qt.QPushButton('开始录制')
        self.record_button.setToolTip('将之后收到的原始串口包持续保存为 .bin')
        self.record_button.clicked.connect(self.toggle_recording)
        row.addWidget(self.record_button)
        offline = Qt.QPushButton('离线解码')
        offline.setToolTip('打开保存的 .bin，启动解码并导出 CSV')
        offline.clicked.connect(self.open_offline_decoder)
        row.addWidget(offline)
        save = Qt.QPushButton('保存 .bin')
        save.setObjectName('primary')
        save.setToolTip('保存当前缓存，最多 60000 包')
        save.clicked.connect(self.save_cache)
        row.addWidget(save)
        return row

    def build_metrics(self):
        row = Qt.QHBoxLayout()
        self.metrics = {}
        for key, title in [('source', '数据源'), ('fps', '视觉 FPS'), ('tx', 'TX 包率'),
                           ('rx', 'RX 包率'), ('packets', '缺序号 / 迟到 / 坏包')]:
            card = Qt.QFrame()
            card.setObjectName('card')
            body = Qt.QVBoxLayout(card)
            body.setContentsMargins(10, 6, 10, 6)
            body.setSpacing(2)
            label = Qt.QLabel(title)
            label.setProperty('muted', True)
            number = Qt.QLabel('—')
            number.setObjectName('metric')
            body.addWidget(label)
            body.addWidget(number)
            self.metrics[key] = number
            row.addWidget(card, 2 if key == 'packets' else 1)
        return row

    def build_scope(self):
        view = Qt.QWidget()
        layout = Qt.QVBoxLayout(view)
        row = Qt.QHBoxLayout()
        self.length = combo([(f'{value} s', value) for value in (2, 5, 10, 20, 30)], 1)
        self.units = combo([('度 °', True), ('弧度 rad', False)])
        self.pause = Qt.QPushButton('暂停滚动')
        self.pause.setCheckable(True)
        self.lock_y = Qt.QCheckBox('锁定 Y 轴')
        for widget in (self.length, self.units, self.pause, self.lock_y):
            row.addWidget(widget)
        row.addStretch()
        layout.addLayout(row)

        self.graph = pg.GraphicsLayoutWidget()
        self.plots, self.curves, self.lines = [], {}, {}
        for channel, (title, suffix, _) in enumerate(CHANNELS):
            for col, axis in enumerate(AXES):
                field = axis + suffix
                plot = self.graph.addPlot(row=channel, col=col)
                plot.setTitle(f'{axis.upper()} · {title}', size='11pt')
                plot.showGrid(x=True, y=True, alpha=.16)
                if channel == len(CHANNELS) - 1:
                    plot.setLabel('bottom', '记录时间', units='s')
                else:
                    plot.getAxis('bottom').setStyle(showValues=False)
                if self.plots:
                    plot.setXLink(self.plots[0])
                self.plots.append(plot)
                plot.getViewBox().sigRangeChangedManually.connect(self.freeze_scroll)
                for direction in range(1 if channel == 2 else 2):
                    self.curves[field, direction] = plot.plot(pen=pg.mkPen(COLORS[direction]))
                for name, color in [('cursor', '#94a8bc'), ('A', '#ffce7b'), ('B', '#d4aaff')]:
                    line = pg.InfiniteLine(angle=90, pen=pg.mkPen(color), label=None if name == 'cursor' else name)
                    line.setZValue(2)
                    plot.addItem(line, ignoreBounds=True)
                    line.hide()
                    self.lines[field, name] = line
        self.mouse = pg.SignalProxy(self.graph.scene().sigMouseMoved, rateLimit=30, slot=self.move_cursor)
        layout.addWidget(self.graph, 1)

        row = Qt.QHBoxLayout()
        self.marker_delta = Qt.QLabel('Δt —')
        for name in ('A', 'B'):
            button = Qt.QPushButton(f'标记 {name}')
            button.clicked.connect(partial(self.set_marker, name))
            row.addWidget(button)
        clear = Qt.QPushButton('清除标记')
        clear.clicked.connect(self.clear_markers)
        row.addWidget(clear)
        row.addWidget(self.marker_delta)
        row.addStretch()
        hint = Qt.QLabel('<span style="color:#77b6ff">■ TX 指令</span> &nbsp; <span style="color:#66e2c4">■ RX 反馈</span>')
        row.addWidget(hint)
        layout.addLayout(row)
        self.readout = Qt.QLabel()
        self.readout.setWordWrap(True)
        layout.addWidget(self.readout)
        self.units.currentIndexChanged.connect(self.change_units)
        self.length.currentIndexChanged.connect(self.change_window)
        self.pause.toggled.connect(self.change_pause)
        self.lock_y.toggled.connect(self.change_y_lock)
        self.change_units()
        return view

    def build_packets(self):
        view = Qt.QWidget()
        layout = Qt.QVBoxLayout(view)
        row = Qt.QHBoxLayout()
        self.direction = combo([('全部方向', None), ('仅 TX', 0), ('仅 RX', 1)])
        self.packet_pause = Qt.QCheckBox('冻结列表')
        row.addWidget(self.direction)
        row.addWidget(self.packet_pause)
        row.addStretch()
        title = Qt.QLabel('原始数据包')
        title.setStyleSheet('font-size: 16px; font-weight: 600;')
        layout.addWidget(title)
        layout.addLayout(row)

        split = Qt.QSplitter(QtCore.Qt.Vertical)
        self.packet_table = Qt.QTableWidget(0, 5)
        self.packet_table.verticalHeader().setMinimumSectionSize(22)
        self.packet_table.verticalHeader().setDefaultSectionSize(23)
        self.packet_table.setHorizontalHeaderLabels(['方向', '序号', '时间 (s)', '字节', 'CRC'])
        self.packet_table.setAlternatingRowColors(True)
        self.packet_table.setEditTriggers(Qt.QAbstractItemView.NoEditTriggers)
        self.packet_table.setSelectionBehavior(Qt.QAbstractItemView.SelectRows)
        self.packet_table.setSelectionMode(Qt.QAbstractItemView.SingleSelection)
        self.packet_table.verticalHeader().hide()
        header = self.packet_table.horizontalHeader()
        header.setSectionResizeMode(Qt.QHeaderView.ResizeToContents)
        header.setSectionResizeMode(2, Qt.QHeaderView.Stretch)
        split.addWidget(self.packet_table)
        self.packet_detail = Qt.QPlainTextEdit()
        self.packet_detail.setReadOnly(True)
        self.packet_detail.setPlaceholderText('选择一行查看原始 HEX 和解析字段')
        split.addWidget(self.packet_detail)
        split.setSizes([260, 380])
        layout.addWidget(split)
        self.packet_table.currentCellChanged.connect(self.inspect_packet)
        self.direction.currentIndexChanged.connect(self.refresh_packets)
        self.packet_pause.toggled.connect(self.refresh_packets)
        return view

    def change_units(self):
        self.scale = 180 / math.pi if self.units.currentData() else 1.
        self.unit = '°' if self.units.currentData() else 'rad'
        for index, plot in enumerate(self.plots):
            plot.setLabel('left', self.unit + CHANNELS[index // len(AXES)][2])
        if self.lock_y.isChecked():
            self.lock_y.setChecked(False)
        self.redraw = True

    def change_window(self):
        self.pause.setChecked(False)
        self.redraw = True

    def change_pause(self, paused):
        self.pause.setText('跟随最新' if paused else '暂停滚动')
        self.redraw = True

    def freeze_scroll(self, *_):
        self.pause.setChecked(True)

    def change_y_lock(self, locked):
        for plot in self.plots:
            plot.enableAutoRange(axis=pg.ViewBox.YAxis, enable=not locked)

    def move_cursor(self, event):
        pos = event[0]
        for index, plot in enumerate(self.plots):
            view = plot.getViewBox()
            if view.sceneBoundingRect().contains(pos):
                self.cursor = view.mapSceneToView(pos).x()
                self.cursor_channel = index // len(AXES)
                for (_, name), line in self.lines.items():
                    if name == 'cursor':
                        line.setValue(self.cursor)
                        line.show()
                self.update_readout()
                return

    def set_marker(self, name, _checked=False):
        if self.cursor is None or self.drawn is None:
            return
        self.markers[name] = self.cursor
        for (_, marker), line in self.lines.items():
            if marker == name:
                line.setValue(self.cursor)
                line.show()
        if len(self.markers) == 2:
            delta = self.markers['B'] - self.markers['A']
            self.marker_delta.setText(f'Δt B−A {delta * 1000:+.2f} ms')

    def clear_markers(self):
        self.markers.clear()
        self.marker_delta.setText('Δt —')
        for (_, name), line in self.lines.items():
            if name != 'cursor':
                line.hide()

    def update_readout(self):
        title, suffix, rate_unit = CHANNELS[self.cursor_channel]
        text = f'最新{title}' if self.cursor is None else f'{self.cursor:.6f} s · {title} · 游标左侧样本'
        for direction, label in enumerate(DIRECTIONS):
            times = self.timestamps[direction]
            index = len(times) - 1 if self.cursor is None else np.searchsorted(times, self.cursor, side='right') - 1
            values = self.drawn['series'][direction][index]['values'] if index >= 0 else {}
            readings = [f'{axis.upper()} {values[axis + suffix] * self.scale:.4f} {self.unit}{rate_unit}'
                        if axis + suffix in values else f'{axis.upper()} —' for axis in AXES]
            age = f' ({(self.cursor - times[index]) * 1000:.1f} ms 前)' if self.cursor is not None and index >= 0 else ''
            text += f'    {label}{age}  ' + ' / '.join(readings)
        self.readout.setText(text)

    def draw_curves(self):
        for direction, frames in self.drawn['series'].items():
            times = np.fromiter((frame['t'] for frame in frames), float, count=len(frames))
            modes = np.fromiter((frame['values']['mode'] for frame in frames), np.uint8, count=len(frames))
            gaps = np.fromiter((frame['gap'] for frame in frames), bool, count=len(frames))
            self.timestamps[direction] = times
            for (field, curve_direction), curve in self.curves.items():
                if curve_direction != direction:
                    continue
                values = np.fromiter((frame['values'][field] for frame in frames), float, count=len(frames))
                x, y = curve_samples(times, values, modes, gaps, command=direction == 0, angle=field in AXES)
                curve.setData(x, y * self.scale, connect='finite')
        if not self.pause.isChecked() and self.drawn['latest']:
            end = self.drawn['end']
            self.plots[0].setXRange(end - self.length.currentData(), end, padding=0)
        self.update_readout()
        self.redraw = False

    def refresh_packets(self, *_):
        if self.packet_pause.isChecked():
            return
        records = self.store.recent_packets(self.direction.currentData())
        if records == self.packets:
            return
        row = self.packet_table.currentRow()
        selected = self.packets[row] if 0 <= row < len(self.packets) else None
        self.packets = records
        self.packet_table.blockSignals(True)
        self.packet_table.setRowCount(len(records))
        for row, raw in enumerate(records):
            direction, _, length, ns, seq, _, crc_ok = packet_fields(raw)
            values = (DIRECTIONS[direction], str(seq), f'{ns * 1e-9:.3f}', str(length), 'OK' if crc_ok else '错误')
            for col, value in enumerate(values):
                self.packet_table.setItem(row, col, Qt.QTableWidgetItem(value))
        row = records.index(selected) if selected in records else (0 if records else -1)
        if row >= 0:
            self.packet_table.setCurrentCell(row, 0)
        self.packet_table.blockSignals(False)
        self.inspect_packet(row)

    def inspect_packet(self, row, *_):
        if not 0 <= row < len(self.packets):
            self.packet_detail.clear()
            return
        direction, status, length, ns, seq, payload, crc_ok = packet_fields(self.packets[row])
        lines = [f'{DIRECTIONS[direction]} #{seq}  ·  {length} 字节',
                 f'{ns} ns  ·  状态 0x{status:02X}  ·  CRC {"OK" if crc_ok else "错误"}',
                 '', payload.hex(' ').upper(), '']
        try:
            values = decode_record(direction, status, payload)
        except ProtocolError as exc:
            lines.append(str(exc))
        else:
            lines.append('角度 rad · 角速度 rad/s · 角加速度 rad/s²')
            lines.extend(f'{name:<14} {value:.9g}' for name, value in values.items())
        self.packet_detail.setPlainText('\n'.join(lines))

    def save_cache(self):
        default = f'serial_{time.strftime("%Y%m%d_%H%M%S")}.bin'
        path, _ = Qt.QFileDialog.getSaveFileName(self, '保存当前缓存', default, '串口记录 (*.bin)')
        if not path:
            return
        try:
            count = self.store.save(path)
        except (OSError, ValueError) as exc:
            Qt.QMessageBox.warning(self, '保存失败', str(exc))
        else:
            self.last_saved = Path(path).resolve()
            Qt.QMessageBox.information(self, '已保存', f'{count} 个数据包\n{path}')

    def open_offline_decoder(self):
        script = Path(__file__).resolve().parents[1] / 'save' / 'offline_decode.py'
        if not script.is_file():
            Qt.QMessageBox.warning(self, '启动失败', '找不到 save/offline_decode.py。')
            return
        arguments = ['-B', str(script.resolve())]
        if self.last_saved is not None and self.last_saved.is_file():
            arguments.append(str(self.last_saved))
        started, _ = QtCore.QProcess.startDetached(sys.executable, arguments)
        if not started:
            Qt.QMessageBox.warning(self, '启动失败', '无法打开离线解码窗口。')

    def toggle_recording(self):
        if self.recorder is not None:
            self.store.stop_recording()
            self.update_recording()
            return
        default = f'serial_record_{time.strftime("%Y%m%d_%H%M%S")}.bin'
        path, _ = Qt.QFileDialog.getSaveFileName(self, '选择录制文件', default, '串口记录 (*.bin)')
        if not path:
            return
        if Path(path).suffix.lower() != '.bin':
            path += '.bin'
        try:
            self.recorder = self.store.start_recording(path)
        except (OSError, ValueError) as error:
            Qt.QMessageBox.warning(self, '录制失败', str(error))
            return
        self.record_status.show()
        self.update_recording()

    def update_recording(self):
        if self.recorder is None:
            return
        state = self.recorder.snapshot()
        path = Path(state['path'])
        counts = f'{state["written"]} 包 · 写入队列丢弃 {state["dropped"]} 包'
        if state['error']:
            self.store.stop_recording()
        if state['finished']:
            self.recorder = None
            self.record_button.setText('开始录制')
            self.record_button.setEnabled(True)
            if state['error']:
                self.record_status.setText(f'录制失败 · {state["error"]} · {path}')
            else:
                if state['written']:
                    self.last_saved = path
                self.record_status.setText(f'录制结束 · {counts} · {path}')
        elif state['active']:
            self.record_button.setText('停止录制')
            self.record_status.setText(f'录制中 · {counts} · {path.name}')
        else:
            self.record_button.setEnabled(False)
            self.record_status.setText(f'正在收尾 · {counts} · {path.name}')

    def reset_display(self, session):
        self.session = session
        self.drawn = None
        self.redraw = True
        self.timestamps = {0: np.empty(0), 1: np.empty(0)}
        self.cursor = None
        self.cursor_channel = 0
        for line in self.lines.values():
            line.hide()
        self.clear_markers()
        self.pause.setChecked(False)
        self.packet_pause.setChecked(False)

    def update_status(self, state):
        age = state['age']
        fps = '—' if state['fps'] is None else f'{state["fps"]:.2f}'
        label = '等待数据' if age is None else ('已过期' if age > 1 else '实时接收')
        detail = f'UDP {self.receiver.port} · 来源 {self.receiver.peer or "等待"}'
        if self.receiver.error:
            detail += f' · {self.receiver.error}'
        if age is not None:
            detail += f' · 距最近收包 {age * 1000:.0f} ms'
        if 0 in state['latest']:
            tx = state['latest'][0]['values']
            detail += f' · TX 模式 {tx["mode"]} / 目标 {tx["target_id"]}'
        self.metrics['source'].setText(label)
        self.metrics['fps'].setText(fps)
        for direction, key in enumerate(('tx', 'rx')):
            self.metrics[key].setText(f'{state["rates"][direction]} Hz')
        self.metrics['packets'].setText(f'{state["missing"]} / {state["late"]} / {state["bad"]}')
        self.connection.setText(detail)

    def tick(self):
        now = time.monotonic()
        state = self.store.snapshot(now=now, include_series=False)
        if state['session'] != self.session:
            self.reset_display(state['session'])
        # Status and packet history do not need to repaint at the graph refresh rate.
        if now - self.last_status >= .25:
            self.last_status = now
            self.update_status(state)
            self.refresh_packets()
            self.update_recording()
        frozen = self.pause.isChecked() and self.drawn is not None
        if not self.redraw and (frozen or state['latest'] == self.drawn['latest']):
            return
        if not frozen:
            snapshot = self.store.snapshot(self.length.currentData(), now)
            if snapshot['session'] != self.session:
                self.reset_display(snapshot['session'])
            self.drawn = snapshot
        self.draw_curves()

    def closeEvent(self, event):
        self.timer.stop()
        self.receiver.close()
        self.store.stop_recording()
        if self.recorder is not None:
            self.recorder.close()
        super().closeEvent(event)


def run_scope(args):
    pg.setConfigOptions(antialias=False, useOpenGL=False, background='#0e1928', foreground='#bfd0e3')
    app = Qt.QApplication.instance() or Qt.QApplication(sys.argv[:1])
    app.setStyleSheet(STYLE)
    window = ScopeWindow(args)
    window.show()
    return app.exec() if hasattr(app, 'exec') else app.exec_()
