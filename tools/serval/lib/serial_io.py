"""Full-duplex SP serial bridge. Forwarding and observation use separate queues."""
import os
import queue
import select
import tempfile
import threading
import time
from pathlib import Path

from .protocol import ProtocolError, StreamDecoder


class SerialBridge:
    def __init__(self, on_frame, on_reject, on_raw=None):
        self.on_frame, self.on_reject, self.on_raw = on_frame, on_reject, on_raw
        self.lock = threading.RLock()
        self.stop_event = threading.Event()
        self.workers = []
        self.physical = self.virtual = None
        self.master = self.slave = None
        self.link = None
        self.link_target = None
        self.physical_name = self.app_port = self.error = ''
        self.running = False
        self.baud = 921600
        self.bytes = {'rx': 0, 'tx': 0}
        self.forward_ms = {'rx': 0.0, 'tx': 0.0}
        self.max_forward_ms = {'rx': 0.0, 'tx': 0.0}
        self.observations = queue.Queue(maxsize=512)
        self.preview_drops = 0

    @staticmethod
    def list_ports():
        try:
            from serial.tools import list_ports
            return [dict(device=p.device, description=p.description) for p in list_ports.comports()]
        except ImportError:
            return []

    @staticmethod
    def _open_port(name, baud):
        import serial
        port = serial.Serial(baudrate=baud, timeout=.02, write_timeout=.5,
                             bytesize=serial.EIGHTBITS, parity=serial.PARITY_NONE,
                             stopbits=serial.STOPBITS_ONE, xonxoff=False,
                             rtscts=False, dsrdtr=False)
        port.dtr = port.rts = False
        if os.name != 'nt':
            port.exclusive = True
        port.port = name
        port.open()
        return port

    @staticmethod
    def _canonical(name):
        return os.path.normcase(os.path.realpath(name)).replace('\\\\.\\', '')

    def snapshot(self):
        with self.lock:
            return dict(running=self.running, physical=self.physical_name, app_port=self.app_port,
                        baud=self.baud, error=self.error, bytes=dict(self.bytes),
                        forward_ms=dict(self.forward_ms), max_forward_ms=dict(self.max_forward_ms),
                        preview_drops=self.preview_drops, windows=os.name == 'nt')

    def start(self, physical, baud=921600, virtual='', link=''):
        with self.lock:
            if self.running:
                raise ValueError('桥接已运行；先停止再修改端口')
            self.stop()
            if not physical:
                raise ValueError('请选择连接云台的物理串口')
            if not isinstance(baud, int) or not 1 <= baud <= 4000000:
                raise ValueError('波特率无效')
            if os.name == 'nt' and not virtual:
                raise ValueError('Windows 需要虚拟串口对：填写工具端 COM，上位机打开另一端')
            if virtual and self._canonical(physical) == self._canonical(virtual):
                raise ValueError('物理串口与虚拟串口不能相同')
            self.error = ''
            self.physical_name, self.baud = physical, baud
            self.bytes = {'rx': 0, 'tx': 0}
            self.forward_ms = {'rx': 0.0, 'tx': 0.0}
            self.max_forward_ms = {'rx': 0.0, 'tx': 0.0}
            self.observations = queue.Queue(maxsize=512)
            self.preview_drops = 0
            try:
                if not virtual:
                    link_path = Path(link) if link else Path(tempfile.gettempdir()) / 'serval-gimbal'
                    if not link_path.is_absolute():
                        raise ValueError('虚拟串口路径必须是绝对路径')
                    if os.path.lexists(link_path):
                        raise ValueError(f'虚拟串口路径已存在：{link_path}；请停止原实例或使用另一个 --link')
                self.physical = self._open_port(physical, baud)
                if virtual:
                    self.virtual = self._open_port(virtual, baud)
                    self.app_port = '虚拟串口对的另一端（由驱动配置决定）'
                else:
                    import tty
                    self.master, self.slave = os.openpty()
                    tty.setraw(self.slave)
                    os.set_blocking(self.master, False)
                    target = os.ttyname(self.slave)
                    link_path.symlink_to(target)
                    self.link, self.link_target = link_path, target
                    self.app_port = str(link_path)
                self.stop_event = threading.Event()
                self.running = True
                for direction in ('rx', 'tx'):
                    worker = threading.Thread(target=self._relay,
                        args=(direction, self.physical, self.virtual, self.master,
                              self.stop_event, self.observations), daemon=True)
                    self.workers.append(worker)
                self.workers.append(threading.Thread(target=self._observe,
                    args=(self.stop_event, self.observations), daemon=True))
                for worker in self.workers:
                    worker.start()
            except Exception:
                self.stop()
                raise
            return self.snapshot()

    @staticmethod
    def _write_master(master, data, stop):
        view = memoryview(data)
        while view and not stop.is_set():
            _, ready, _ = select.select([], [master], [], .1)
            if ready:
                try:
                    written = os.write(master, view)
                except BlockingIOError:
                    continue
                if not written:
                    raise OSError('虚拟串口写入返回 0 字节')
                view = view[written:]
        return not view

    @staticmethod
    def _write_serial(port, data, stop):
        view = memoryview(data)
        while view and not stop.is_set():
            written = port.write(view)
            if not written:
                raise OSError('串口写入返回 0 字节')
            view = view[written:]
        return not view

    def _relay(self, direction, physical, virtual, master, stop, observations):
        chunk_seq = 0
        try:
            while not stop.is_set():
                if direction == 'rx':
                    data = physical.read(max(1, min(physical.in_waiting, 8192)))
                elif virtual:
                    data = virtual.read(max(1, min(virtual.in_waiting, 8192)))
                else:
                    ready, _, _ = select.select([master], [], [], .1)
                    if not ready:
                        continue
                    try:
                        data = os.read(master, 8192)
                    except BlockingIOError:
                        continue
                    if not data:
                        raise OSError('上位机虚拟串口已关闭')
                if not data:
                    continue
                stamp = time.monotonic()
                if direction == 'tx':
                    complete = self._write_serial(physical, data, stop)
                elif virtual:
                    complete = self._write_serial(virtual, data, stop)
                else:
                    complete = self._write_master(master, data, stop)
                if not complete:
                    break
                elapsed = (time.monotonic() - stamp) * 1000
                self.forward_ms[direction] = elapsed
                self.max_forward_ms[direction] = max(self.max_forward_ms[direction], elapsed)
                self.bytes[direction] += len(data)
                chunk_seq += 1
                try:
                    observations.put_nowait((direction, data, stamp, chunk_seq))
                except queue.Full:
                    self.preview_drops += 1
        except Exception as exc:
            if not stop.is_set():
                self.error = f'{direction.upper()} 转发停止：{exc}'
                self.running = False
                stop.set()

    def _observe(self, stop, observations):
        decoders = {d: StreamDecoder(d, self.on_frame, self.on_reject, self.on_raw)
                    for d in ('rx', 'tx')}
        sequences = {'rx': 0, 'tx': 0}
        while not stop.is_set() or not observations.empty():
            try:
                direction, data, stamp, seq = observations.get(timeout=.1)
            except queue.Empty:
                continue
            try:
                if seq != sequences[direction] + 1:
                    decoders[direction].reset()
                    self.on_reject(ProtocolError('preview_gap', '预览队列丢块，已重新同步帧头'))
                sequences[direction] = seq
                decoders[direction].feed(data, stamp)
            except Exception as exc:
                # Neither a UI exception nor a logging error may stop forwarding.
                try:
                    self.on_reject(ProtocolError('observer', f'预览解析失败：{exc}'))
                except Exception:
                    pass

    def stop(self):
        with self.lock:
            self.stop_event.set()
            # Closing ports also wakes blocking reads/writes during error recovery.
            for port in (self.physical, self.virtual):
                if port:
                    try:
                        port.close()
                    except OSError:
                        pass
            for worker in self.workers:
                if worker.ident is not None:
                    worker.join(timeout=2)
            for fd in (self.master, self.slave):
                if fd is not None:
                    try:
                        os.close(fd)
                    except OSError:
                        pass
            for worker in self.workers:
                if worker.ident is not None and worker.is_alive():
                    worker.join(timeout=1)
            if any(worker.is_alive() for worker in self.workers):
                raise RuntimeError('旧串口线程尚未退出，拒绝重新连接')
            if self.link and self.link.is_symlink() and os.readlink(self.link) == self.link_target:
                self.link.unlink()
            self.workers = []
            self.physical = self.virtual = self.master = self.slave = None
            self.link = self.link_target = None
            self.running = False
