#!/usr/bin/env python3
"""Inspect RMSE serial packets in a terminal; stdlib only, never opens a serial port."""
import argparse
from collections import deque
import math
from pathlib import Path
import struct
import sys
import time

HEADER = struct.Struct('<IBBHQII')
MAGIC = 0x45534D52
TX = struct.Struct('<2sB6fBH')
RX = struct.Struct('<2sB9fHBH')


def crc16(data):
    value = 0xffff
    for byte in data:
        value ^= byte
        for _ in range(8):
            value = (value >> 1) ^ (0x8408 if value & 1 else 0)
    return value


def latest_log(path=None):
    if path:
        selected = Path(path).expanduser()
        if selected.is_file():
            return selected
        roots = [selected]
    else:
        roots = [Path('records/serial'), Path('tools/serval/records')]
    candidates = [p for root in roots if root.is_dir() for p in root.glob('*.bin')]
    return max(candidates, key=lambda p: (p.stat().st_mtime_ns, str(p))) if candidates else None


class Reader:
    def __init__(self, file):
        self.file = file

    def records(self):
        while True:
            offset = self.file.tell()
            raw = self.file.read(HEADER.size)
            if len(raw) < HEADER.size:
                self.file.seek(offset)
                return
            magic, direction, status, length, ns, seq, reserved = HEADER.unpack(raw)
            if magic != MAGIC or direction not in (0, 1) or not 1 <= length <= 64:
                raise ValueError(f'Invalid RMSE header at offset {offset}; not a supported serial log')
            packet = self.file.read(length)
            if len(packet) < length:
                self.file.seek(offset)
                return
            yield direction, status, ns, seq, packet


def describe(record):
    direction, status, ns, seq, packet = record
    label = 'TX' if direction == 0 else 'RX'
    valid = len(packet) >= 2 and crc16(packet[:-2]) == int.from_bytes(packet[-2:], 'little')
    lines = [f'{label} seq={seq} t={ns/1e9:.6f}s len={len(packet)} CRC={"OK" if valid else "BAD"} log_status=0x{status:02X}',
             '  HEX: ' + packet.hex(' ').upper()]
    expected = TX.size if direction == 0 else RX.size
    if len(packet) != expected or packet[:2] != b'SP':
        lines.append(f'  Unsupported layout: expected {expected} bytes with SP header')
        return '\n'.join(lines)
    mode = packet[2]
    if direction == 0:
        _, _, yaw, vyaw, ayaw, pitch, vpitch, apitch, target, _ = TX.unpack(packet)
        lines.append(f'  mode={mode} target_id={target} yaw={yaw:.6f} pitch={pitch:.6f} rad')
        lines.append(f'  yaw_vel={vyaw:.6f} pitch_vel={vpitch:.6f} yaw_acc={ayaw:.6f} pitch_acc={apitch:.6f}')
    else:
        _, _, w, x, y, z, yaw, vyaw, pitch, vpitch, speed, bullets, color, _ = RX.unpack(packet)
        lines.append(f'  mode={mode} enemy_color_raw={color} (byte[41]=0x{packet[41]:02X})')
        lines.append(f'  yaw={yaw:.6f} pitch={pitch:.6f} rad yaw_vel={vyaw:.6f} pitch_vel={vpitch:.6f}')
        lines.append(f'  q[wxyz]=[{w:.6f}, {x:.6f}, {y:.6f}, {z:.6f}] speed={speed:.3f} bullets={bullets}')
    if not valid:
        lines.append('  CRC BAD: decoded fields above are untrusted')
    return '\n'.join(lines)


def main():
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, 'reconfigure'):
            stream.reconfigure(encoding='utf-8')
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('path', nargs='?', help='Log file or directory; defaults to newest of records/serial and tools/serval/records')
    parser.add_argument('--follow', action='store_true', help='Follow appended records and rotate to newer logs')
    parser.add_argument('--direction', choices=('both', 'tx', 'rx'), default='both')
    parser.add_argument('--count', type=int, default=6, help='Number of recent records printed on open')
    parser.add_argument('--hz', type=float, default=2, help='Maximum printed packets per second per direction while following; 0=all')
    args = parser.parse_args()
    if args.count < 0 or not math.isfinite(args.hz) or args.hz < 0:
        parser.error('--count and --hz must be nonnegative')
    print('Read-only log inspection. No serial port is opened. Angles are radians.', flush=True)
    file = None
    current = identity = None
    reader = None
    last_print = {0: 0., 1: 0.}
    last_scan = last_activity = last_notice = 0.
    interval = 1/args.hz if args.hz else 0

    def wanted(record):
        return args.direction == 'both' or record[0] == (0 if args.direction == 'tx' else 1)

    try:
        while True:
            now = time.monotonic()
            if now-last_scan >= .25 or current is None:
                candidate = latest_log(args.path)
                last_scan = now
                if candidate:
                    stat = candidate.stat()
                    new_identity = (stat.st_dev, stat.st_ino)
                    if current != candidate or identity != new_identity or (file and stat.st_size < file.tell()):
                        if file:
                            file.close()
                        file = candidate.open('rb')
                        current, identity = candidate, new_identity
                        reader = Reader(file)
                        recent = deque(maxlen=args.count)
                        for record in reader.records():
                            if wanted(record):
                                recent.append(record)
                        print(f'LOG: {current} bytes={stat.st_size} file_age={max(0,time.time()-stat.st_mtime):.1f}s', flush=True)
                        for record in recent:
                            print(describe(record), flush=True)
                        last_activity = now
                        if not args.follow:
                            return 0
            if reader:
                for record in reader.records():
                    now = time.monotonic()
                    last_activity = now
                    if wanted(record) and now-last_print[record[0]] >= interval:
                        print(describe(record), flush=True)
                        last_print[record[0]] = now
            if not args.follow:
                print('No log found. Enable SP_SERIAL_LOG=1 in the vision program, or specify an existing .bin.', file=sys.stderr)
                return 1
            now = time.monotonic()
            if now-max(last_activity,last_notice) >= 3:
                print('WAIT: no new records; cached output is not current feedback.', flush=True)
                last_notice = now
            time.sleep(.05)
    except KeyboardInterrupt:
        return 0
    except (OSError, ValueError) as exc:
        print(f'ERROR: {exc}', file=sys.stderr)
        return 1
    finally:
        if file:
            file.close()


if __name__ == '__main__':
    raise SystemExit(main())
