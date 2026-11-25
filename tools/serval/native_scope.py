#!/usr/bin/env python3
"""Native read-only scope for Orin/Linux and Windows; no browser or SSH."""
import argparse
import math
from pathlib import Path
import sys
import time

from lib.live_udp import LiveStore, Receiver


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--host', default='0.0.0.0')
    parser.add_argument('--port', type=int, default=9871)
    parser.add_argument('--source-ip', help='Only accept this Orin IPv4 address')
    parser.add_argument('--refresh', type=int, default=30, choices=range(10, 61), metavar='10..60')
    args = parser.parse_args(argv)
    try:
        import numpy as np
        import pyqtgraph as pg
        from pyqtgraph.Qt import QtCore, QtWidgets
    except ImportError:
        parser.exit(1, 'Install: python -m pip install numpy pyqtgraph PyQt5\nOrin: sudo apt install python3-pyqt5 python3-pyqtgraph\n')
    pg.setConfigOptions(antialias=False, useOpenGL=False, background='#0b1220', foreground='#c5d6ea')
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication(sys.argv[:1])
    app.setStyleSheet('QWidget {background:#101a29;color:#dbe6f4;font-size:12px;} '
                     'QPushButton,QComboBox {background:#20324a;border:1px solid #425a76;'
                     'padding:6px;border-radius:4px;} QCheckBox {padding:6px;} '
                     'QMessageBox QLabel {min-width:300px;}')
    store = LiveStore()
    try:
        receiver = Receiver(store, args.host, args.port, args.source_ip)
    except OSError as exc:
        parser.exit(1, f'Cannot bind UDP {args.port}: {exc}\n')
    window = QtWidgets.QMainWindow()
    window.setWindowTitle('Serval Native Scope · 只读串口示波器')
    window.resize(1240, 900)
    central = QtWidgets.QWidget()
    layout = QtWidgets.QVBoxLayout(central)
    status = QtWidgets.QLabel('等待 Orin 的 UDP 数据；本窗口不发送云台命令')
    status.setWordWrap(True)
    status.setStyleSheet('font-size:14px; padding:10px; color:#38d9b6; background:#101e2f')
    layout.addWidget(status)
    bar = QtWidgets.QHBoxLayout()
    pause = QtWidgets.QPushButton('暂停显示')
    save = QtWidgets.QPushButton('保存当前缓存 .bin')
    length = QtWidgets.QComboBox()
    length.addItems(['2', '5', '10', '20', '30'])
    length.setCurrentText('10')
    degrees = QtWidgets.QCheckBox('使用度 °（关闭为 rad）')
    degrees.setChecked(True)
    lock_y = QtWidgets.QCheckBox('锁定 Y 轴')
    for widget in (pause, save, QtWidgets.QLabel('窗口秒数'), length, degrees, lock_y):
        bar.addWidget(widget)
    bar.addStretch()
    layout.addLayout(bar)
    graph = pg.GraphicsLayoutWidget()
    layout.addWidget(graph, 1)
    packet_label = QtWidgets.QLabel('TX / RX 原始包：等待数据')
    packet_label.setWordWrap(True)
    packet_label.setTextInteractionFlags(QtCore.Qt.TextInteractionFlag.TextSelectableByMouse)
    packet_label.setStyleSheet('font:11px monospace; padding:6px')
    layout.addWidget(packet_label)
    note = QtWidgets.QLabel('原始采样时间 · 无平滑/无插值 · UDP缺样处断线 · RX协议没有角加速度 · '
                           '网络到达间隔不是单程通信延迟 · 关闭窗口不停止自瞄')
    note.setWordWrap(True)
    layout.addWidget(note)
    window.setCentralWidget(central)
    plots, curves = [], {}
    for row, (suffix, title) in enumerate([('', '角度'), ('_vel', '角速度'), ('_acc', '角加速度')]):
        for col, axis in enumerate(['yaw', 'pitch']):
            key = axis+suffix
            plot = graph.addPlot(row=row, col=col, title=axis.upper()+' '+title)
            plot.showGrid(x=True, y=True, alpha=.18)
            plot.addLegend(offset=(8, 8))
            plot.setLabel('bottom', '记录时间', units='s')
            plot.setClipToView(True)
            if plots:
                plot.setXLink(plots[0])
            plots.append(plot)
            for direction, color, name in [(0,'#61b7ff','TX 实际指令'),(1,'#38d9b6','RX 实际反馈')]:
                if suffix=='_acc' and direction==1:
                    continue
                curves[direction,key] = plot.plot(pen=pg.mkPen(color,width=1),name=name,
                    connect='finite',autoDownsample=True,downsampleMethod='peak')
    paused = False
    last_packet = 0.
    last_unit = None

    def toggle_pause():
        nonlocal paused
        paused = not paused
        pause.setText('继续显示' if paused else '暂停显示')

    def save_cache():
        path, _ = QtWidgets.QFileDialog.getSaveFileName(window, '保存最近缓存（最多60000包，含坏包）',
            'serial_'+time.strftime('%Y%m%d_%H%M%S')+'.bin', 'Serial log (*.bin)')
        if path:
            try:
                count = store.save(path)
                QtWidgets.QMessageBox.information(window, '已保存', f'{count} 个原始包\n{path}')
            except (OSError, ValueError) as exc:
                QtWidgets.QMessageBox.warning(window, '保存失败', str(exc))

    def toggle_y(checked):
        for plot in plots:
            plot.enableAutoRange(axis=pg.ViewBox.YAxis, enable=not checked)

    def update():
        nonlocal last_packet, last_unit
        snapshot = store.snapshot(float(length.currentText()))
        age = snapshot['age']
        fps = '—' if snapshot['fps'] is None else f'{snapshot["fps"]:.2f}'
        state = '等待数据' if age is None else ('已过期' if age>1 else '接收中')
        latest_tx = snapshot['series'][0][-1]['values'] if snapshot['series'][0] else {}
        status.setText(f'{state}  |  视觉 FPS {fps}  |  TX {snapshot["rates"][0]} Hz / RX {snapshot["rates"][1]} Hz'
                       f'  |  缺序号 {snapshot["missing"]} / 迟到 {snapshot["late"]} / 坏包 {snapshot["bad"]}'
                       f'  |  mode {latest_tx.get("mode", "—")} / 目标 {latest_tx.get("target_id", "—")}'
                       f'  |  UDP {receiver.peer or "*"}:{receiver.port}'
                       + (f'  |  距收包 {age*1000:.0f} ms' if age is not None else '')
                       + (f'  |  {receiver.error}' if receiver.error else ''))
        if paused:
            return
        scale = 180/math.pi if degrees.isChecked() else 1.
        for direction, frames in snapshot['series'].items():
            for (d,key), curve in curves.items():
                if d!=direction:
                    continue
                x, y, previous = [], [], None
                for frame in frames:
                    value = frame['values'].get(key)
                    t = frame['t']
                    if value is None:
                        continue
                    mode = frame['values']['mode']
                    gap = frame['gap'] or (previous is not None and
                        (t-previous[0]>.1 or (direction==0 and mode!=previous[1]) or
                         (key in ('yaw','pitch') and abs(value-previous[2])>math.pi)))
                    if gap:
                        x.append(t); y.append(float('nan'))
                    # Draw the command as a zero-order hold, without smoothing or inventing RX samples.
                    if direction==0 and previous is not None and not gap:
                        x.append(t); y.append(previous[2]*scale)
                    x.append(t); y.append(value*scale)
                    previous = t,mode,value
                curve.setData(np.asarray(x), np.asarray(y), connect='finite')
        if snapshot['session'] is not None:
            plots[0].setXRange(max(0,snapshot['end']-float(length.currentText())),
                              max(.1,snapshot['end']),padding=0)
        unit = '°' if degrees.isChecked() else 'rad'
        if unit!=last_unit:
            for i, plot in enumerate(plots):
                plot.setLabel('left', unit+['','/s','/s²'][i//2])
            last_unit = unit
        if time.monotonic()-last_packet>=.25:
            with store.lock:
                latest = {d:payload.hex(' ').upper() for d,payload in store.latest_raw.items()}
            packet_label.setText('TX '+latest.get(0,'—')+'\nRX '+latest.get(1,'—'))
            last_packet = time.monotonic()

    pause.clicked.connect(toggle_pause)
    save.clicked.connect(save_cache)
    lock_y.toggled.connect(toggle_y)
    timer = QtCore.QTimer()
    timer.timeout.connect(update)
    timer.start(round(1000/args.refresh))
    app.aboutToQuit.connect(receiver.close)
    window.show()
    return app.exec() if hasattr(app,'exec') else app.exec_()


if __name__=='__main__':
    sys.exit(main())
