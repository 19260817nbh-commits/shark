#!/usr/bin/env python3
"""Run the saved serial-log decoder from a separate Qt window."""
import argparse
import codecs
from importlib.util import find_spec
from pathlib import Path
import sys
import tempfile


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('log', nargs='?', type=Path, help='Optional .bin file to select on startup')
    args = parser.parse_args(argv)
    try:
        from pyqtgraph.Qt import QtCore, QtGui, QtWidgets as Qt
    except ImportError as error:
        parser.exit(1, f'{error}\nOrin: sudo apt install python3-pyqt5 python3-pyqtgraph\n')

    class PlotPreview(Qt.QLabel):
        def __init__(self):
            super().__init__('解码完成后显示波形')
            self.setAlignment(QtCore.Qt.AlignCenter)
            self.setMinimumSize(270, 190)
            self.setSizePolicy(Qt.QSizePolicy.Ignored, Qt.QSizePolicy.Ignored)
            self.source = QtGui.QPixmap()

        def load(self, path=None):
            self.source = QtGui.QPixmap(str(path)) if path else QtGui.QPixmap()
            self.refresh()

        def refresh(self):
            if self.source.isNull():
                self.setText('解码完成后显示波形')
            else:
                self.setPixmap(self.source.scaled(self.size(), QtCore.Qt.KeepAspectRatio,
                                                  QtCore.Qt.SmoothTransformation))

        def resizeEvent(self, event):
            super().resizeEvent(event)
            self.refresh()

    class DecodeDialog(Qt.QDialog):
        def __init__(self, log):
            super().__init__()
            self.setWindowTitle('Serval · 离线解码')
            self.resize(990, 590)
            self.output_dir = None
            self.process = QtCore.QProcess(self)
            self.process.setProcessChannelMode(QtCore.QProcess.MergedChannels)
            self.process.readyReadStandardOutput.connect(self.read_output)
            self.process.finished.connect(self.finished)
            self.process.errorOccurred.connect(self.process_error)
            self.text_decoder = None

            layout = Qt.QVBoxLayout(self)
            layout.setContentsMargins(16, 14, 16, 14)
            layout.addWidget(Qt.QLabel('串口记录 .bin'))
            inputs = Qt.QHBoxLayout()
            self.log_path = Qt.QLineEdit(str(log.resolve()) if log else '')
            self.log_path.setPlaceholderText('选择一份已保存的串口记录')
            self.log_path.setReadOnly(True)
            self.browse = Qt.QPushButton('选择文件')
            self.browse.clicked.connect(self.choose_log)
            inputs.addWidget(self.log_path, 1)
            inputs.addWidget(self.browse)
            layout.addLayout(inputs)

            actions = Qt.QHBoxLayout()
            self.plot = Qt.QCheckBox('生成波形 PNG')
            self.plot_available = find_spec('matplotlib') is not None
            self.plot.setChecked(self.plot_available)
            self.plot.setEnabled(self.plot_available)
            self.plot.setToolTip('绘图需要 Matplotlib')
            actions.addWidget(self.plot)
            actions.addStretch()
            self.open_folder = Qt.QPushButton('打开输出目录')
            self.open_folder.setEnabled(False)
            self.open_folder.clicked.connect(self.show_output_dir)
            actions.addWidget(self.open_folder)
            self.start_button = Qt.QPushButton('启动解码')
            self.start_button.setObjectName('primary')
            self.start_button.setEnabled(bool(log and log.is_file()))
            self.start_button.clicked.connect(self.start_decode)
            actions.addWidget(self.start_button)
            layout.addLayout(actions)

            self.status = Qt.QLabel('结果写入记录旁的新目录，每次运行单独保存。')
            self.status.setWordWrap(True)
            self.status.setTextInteractionFlags(QtCore.Qt.TextSelectableByMouse)
            layout.addWidget(self.status)
            content = Qt.QSplitter(QtCore.Qt.Horizontal)
            self.details = Qt.QPlainTextEdit()
            self.details.setReadOnly(True)
            self.details.setMaximumBlockCount(300)
            self.details.setPlaceholderText('解码状态与结果将显示在这里')
            content.addWidget(self.details)
            self.preview = PlotPreview()
            content.addWidget(self.preview)
            content.setSizes([380, 570])
            layout.addWidget(content, 1)

        def choose_log(self):
            path, _ = Qt.QFileDialog.getOpenFileName(
                self, '选择串口记录', self.log_path.text(), '串口记录 (*.bin)'
            )
            if path:
                self.log_path.setText(path)
                self.start_button.setEnabled(True)

        def start_decode(self):
            path = Path(self.log_path.text())
            if not path.is_file():
                self.status.setText('找不到记录文件，请重新选择。')
                return
            try:
                self.output_dir = Path(tempfile.mkdtemp(prefix=f'{path.stem[:64]}_decode_', dir=path.parent))
            except OSError as error:
                self.status.setText(f'无法在记录旁创建输出目录。{error}')
                return
            self.plot_requested = self.plot.isChecked()
            self.set_busy(True)
            self.details.clear()
            self.preview.load()
            self.text_decoder = codecs.getincrementaldecoder('utf-8')(errors='replace')
            self.status.setText('正在解码…')
            script = Path(__file__).with_name('decode_serial_log.py').resolve()
            arguments = ['-B', '-u', str(script), str(path.resolve()), '--out-dir', str(self.output_dir)]
            if self.plot_requested:
                arguments.append('--plot')
            self.process.start(sys.executable, arguments)

        def set_busy(self, busy):
            self.start_button.setEnabled(not busy)
            self.browse.setEnabled(not busy)
            self.plot.setEnabled(not busy and self.plot_available)
            self.open_folder.setEnabled(not busy and self.output_dir is not None)

        def read_output(self, final=False):
            data = bytes(self.process.readAllStandardOutput())
            text = self.text_decoder.decode(data, final=final)
            if text:
                self.details.moveCursor(QtGui.QTextCursor.End)
                self.details.insertPlainText(text)

        def finished(self, exit_code, exit_status):
            self.read_output(final=True)
            self.set_busy(False)
            if exit_status != QtCore.QProcess.NormalExit or exit_code:
                self.status.setText(f'解码失败，退出代码 {exit_code}。详情见下方。')
                return
            result = '已生成 tx.csv 和 rx.csv'
            if self.plot_requested:
                waveform = self.output_dir / 'waveform.png'
                if waveform.is_file():
                    self.preview.load(waveform)
                    result += ' 和 waveform.png'
                else:
                    result += '。波形图未生成，详情见下方。'
            self.status.setText(f'{result}\n{self.output_dir}')

        def process_error(self, error):
            if error == QtCore.QProcess.FailedToStart:
                self.set_busy(False)
                self.status.setText(f'无法启动解码脚本。{self.process.errorString()}')

        def show_output_dir(self):
            if not QtGui.QDesktopServices.openUrl(QtCore.QUrl.fromLocalFile(str(self.output_dir))):
                self.status.setText(f'请在文件管理器中打开\n{self.output_dir}')

        def reject(self):
            if self.process.state() != QtCore.QProcess.NotRunning:
                self.status.setText('正在解码，完成后可关闭此窗口。')
                return
            super().reject()

        def closeEvent(self, event):
            if self.process.state() != QtCore.QProcess.NotRunning:
                self.reject()
                event.ignore()
                return
            super().closeEvent(event)

    app = Qt.QApplication.instance() or Qt.QApplication(sys.argv[:1])
    dialog = DecodeDialog(args.log)
    dialog.setStyleSheet('''
        QDialog { background: #0d1521; color: #dbe7f4; }
        QLabel, QCheckBox { color: #dbe7f4; }
        QLineEdit, QPlainTextEdit { background: #111e2e; color: #dbe7f4; border: 1px solid #304763; padding: 6px; }
        QPushButton { background: #192a3f; color: #dbe7f4; border: 1px solid #354f6d; border-radius: 5px; padding: 6px 10px; }
        QPushButton#primary { background: #193f52; border-color: #4586a0; }
        QPushButton:disabled { color: #70859b; }
    ''')
    dialog.exec() if hasattr(dialog, 'exec') else dialog.exec_()
    return 0


if __name__ == '__main__':
    sys.exit(main())
