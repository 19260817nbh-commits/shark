"""Run: python -m unittest discover -s tools/serval -v"""
import csv
import io
import math
import os
import threading
import time
import tempfile
import unittest
from unittest.mock import patch

from monitor import Store
from protocol import RX, TX, StreamDecoder, crc16, decode_frame
from serial_io import SerialBridge


def packet(direction='rx'):
    if direction == 'rx':
        raw = RX.pack(b'SP', 1, 1, 0, 0, 0, .25, .5, -.1, -.2, 26, 42, 1, 0)
    else:
        raw = TX.pack(b'SP', 1, .3, .6, .7, -.2, -.3, -.4, 3, 0)
    return raw[:-2] + crc16(raw[:-2]).to_bytes(2, 'little')


class MemoryPort:
    def __init__(self):
        self.input = bytearray()
        self.output = bytearray()
        self.closed = False
        self.lock = threading.Lock()

    @property
    def in_waiting(self):
        with self.lock:
            return len(self.input)

    def feed(self, data):
        with self.lock:
            self.input.extend(data)

    def read(self, size):
        with self.lock:
            data = bytes(self.input[:size])
            del self.input[:size]
        if not data:
            time.sleep(.002)
        return data

    def write(self, data):
        # Deliberate short writes exercise forwarding until every byte is sent.
        with self.lock:
            size = min(7, len(data))
            self.output.extend(data[:size])
            return size

    def close(self):
        self.closed = True


def wait_until(predicate, timeout=3):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(.005)
    raise AssertionError('Timed out waiting for serial relay')


class ProtocolTests(unittest.TestCase):
    def test_wire_layout_crc_and_values(self):
        self.assertEqual((RX.size, TX.size), (44, 30))
        self.assertEqual(crc16(b'123456789'), 0x6f91)
        direction, rx = decode_frame(packet())
        self.assertEqual(direction, 'rx')
        self.assertAlmostEqual(rx['yaw'], .25)
        self.assertAlmostEqual(rx['pitch'], -.1)
        self.assertEqual(rx['bullet_count'], 42)
        self.assertEqual(rx['enemy_color'], 1)
        self.assertEqual(rx['q_norm'], 1)
        self.assertEqual(rx['q_yaw'], 0)
        direction, tx = decode_frame(packet('tx'))
        self.assertEqual(direction, 'tx')
        self.assertAlmostEqual(tx['pitch_acc'], -.4)
        self.assertEqual(tx['target_id'], 3)

    def test_fragmentation_noise_and_crc_recovery(self):
        frames, errors = [], []
        parser = StreamDecoder('rx', lambda *args: frames.append(args), errors.append)
        good = packet()
        corrupt = good[:-1] + bytes([good[-1] ^ 0xff])
        stream = b'noiseS' + corrupt + b'noise' + good + good
        for byte in stream:
            parser.feed(bytes([byte]))
        self.assertEqual(len(frames), 2)
        self.assertTrue(errors)

    def test_nonfinite_values_rejected(self):
        raw = TX.pack(b'SP', 1, math.nan, 0, 0, 0, 0, 0, 1, 0)
        raw = raw[:-2] + crc16(raw[:-2]).to_bytes(2, 'little')
        with self.assertRaisesRegex(ValueError, 'NaN'):
            decode_frame(raw)

    def test_store_retention_cursor_and_csv(self):
        store = Store('test', capacity=2)
        for value in range(3):
            store.add('rx', {'yaw': value})
        snap = store.snapshot(0)
        self.assertEqual(snap['dropped'], 1)
        self.assertEqual([f['seq'] for f in snap['frames']], [2, 3])
        self.assertEqual(store.snapshot(3)['frames'], [])
        rows = list(csv.DictReader(io.StringIO(store.export().decode('utf-8-sig'))))
        self.assertEqual([r['yaw'] for r in rows], ['1', '2'])
        store.reset('new')
        self.assertEqual(store.snapshot(3)['epoch'], 1)
        self.assertFalse(store.snapshot(3)['latest'])
        store.add('rx', {'yaw': 4})
        self.assertEqual(store.snapshot(0)['dropped'], 0)


class BridgeTests(unittest.TestCase):
    def setUp(self):
        self.sandbox = tempfile.TemporaryDirectory()
        self.addCleanup(self.sandbox.cleanup)
        self.test_link = os.path.join(self.sandbox.name, 'gimbal')
    def test_both_directions_and_corrupt_bytes_are_forwarded_unchanged(self):
        physical, virtual = MemoryPort(), MemoryPort()
        frames, errors = [], []
        bridge = SerialBridge(lambda *args: frames.append(args), errors.append)
        with patch.object(bridge, '_open_port', side_effect=[physical, virtual]):
            bridge.start('physical', virtual='virtual')
        try:
            tx, rx = packet('tx'), packet('rx')
            raw_tx = tx + b'not-a-frame\x00'
            corrupt_rx = rx[:-2] + b'\x00\x00'
            raw_rx = rx + corrupt_rx
            virtual.feed(raw_tx[:4])
            physical.feed(raw_rx[:3])
            wait_until(lambda: len(physical.output) >= 4 and len(virtual.output) >= 3)
            virtual.feed(raw_tx[4:])
            physical.feed(raw_rx[3:])
            wait_until(lambda: len(physical.output) == len(raw_tx) and len(virtual.output) == len(raw_rx))
            wait_until(lambda: len(frames) == 2 and len(errors) >= 1)
            self.assertEqual(bytes(physical.output), raw_tx)
            self.assertEqual(bytes(virtual.output), raw_rx)
            self.assertEqual({f[0] for f in frames}, {'rx', 'tx'})
        finally:
            bridge.stop()
        self.assertTrue(physical.closed and virtual.closed)
        self.assertFalse(bridge.running)

    def test_preview_failure_does_not_stop_forwarding(self):
        physical, virtual = MemoryPort(), MemoryPort()
        def bad_preview(*_args):
            raise RuntimeError('simulated renderer failure')
        errors = []
        bridge = SerialBridge(bad_preview, errors.append)
        with patch.object(bridge, '_open_port', side_effect=[physical, virtual]):
            bridge.start('physical', virtual='virtual')
        try:
            data = packet('tx') * 3
            virtual.feed(data)
            wait_until(lambda: bytes(physical.output) == data and errors)
            self.assertTrue(bridge.running)
            virtual.feed(data)
            wait_until(lambda: bytes(physical.output) == data * 2)
        finally:
            bridge.stop()

    @unittest.skipIf(os.name == 'nt', 'Linux PTY integration requires POSIX')
    def test_linux_pty_full_duplex(self):
        import serial
        physical = MemoryPort()
        bridge = SerialBridge(lambda *_: None, lambda *_: None)
        with patch.object(bridge, '_open_port', return_value=physical):
            state = bridge.start('physical', link=self.test_link)
        try:
            with serial.Serial(state['app_port'], 921600, timeout=1) as app:
                tx, rx = packet('tx'), packet('rx')
                app.write(tx)
                wait_until(lambda: bytes(physical.output) == tx)
                physical.feed(rx)
                self.assertEqual(app.read(len(rx)), rx)
        finally:
            bridge.stop()

    @unittest.skipIf(os.name == 'nt', 'Linux PTY integration requires POSIX')
    def test_linux_two_real_serial_endpoints(self):
        import select
        import serial
        import tty
        device_master, device_slave = os.openpty()
        tty.setraw(device_slave)
        frames = []
        bridge = SerialBridge(lambda *args: frames.append(args), lambda *_: None)
        try:
            state = bridge.start(os.ttyname(device_slave), link=self.test_link)
            with serial.Serial(state['app_port'], 921600, timeout=1) as app:
                tx, rx = packet('tx'), packet('rx')
                app.write(tx)
                self.assertTrue(select.select([device_master], [], [], 1)[0])
                forwarded = bytearray()
                while len(forwarded) < len(tx):
                    self.assertTrue(select.select([device_master], [], [], 1)[0])
                    forwarded.extend(os.read(device_master, len(tx)-len(forwarded)))
                self.assertEqual(bytes(forwarded), tx)
                os.write(device_master, rx)
                self.assertEqual(app.read(len(rx)), rx)
                wait_until(lambda: len(frames) == 2)
        finally:
            bridge.stop()
            os.close(device_master)
            os.close(device_slave)


if __name__ == '__main__':
    unittest.main()
