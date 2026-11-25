#!/usr/bin/env python3
"""Jetson desktop scope for read-only telemetry."""
import argparse
from pathlib import Path
import sys


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--host', default='0.0.0.0')
    parser.add_argument('--port', type=int, default=9871)
    parser.add_argument('--source-ip', help='Only accept this Orin IPv4 address')
    parser.add_argument('--refresh', type=int, default=30, choices=range(10, 61), metavar='10..60')
    parser.add_argument('--camera-preview', type=Path, help='Read the latest local camera JPEG')
    parser.add_argument('--preview-fps', type=int, default=60, choices=range(1, 121), metavar='1..120',
                        help='Camera panel polling rate (default: 60)')
    args = parser.parse_args(argv)
    try:
        from lib.jetson_window import run_scope
    except ImportError as exc:
        parser.exit(1, f'{exc}\nInstall: python -m pip install -r requirements_native.txt\n'
                       'Orin: sudo apt install python3-pyqt5 python3-pyqtgraph\n')
    try:
        return run_scope(args)
    except OSError as exc:
        parser.exit(1, f'Cannot open scope: {exc}\n')


if __name__ == '__main__':
    sys.exit(main())
