#!/usr/bin/env python3
"""离线解算 SerialRecorder 录制的串口原始日志（records/serial/*.bin）。

录制端：io/gimbal/serial_recorder.cpp，环境变量 SP_SERIAL_LOG=1 开启。
本脚本只读文件，不碰串口，所以录一次可以反复解算、反复画图。

用法:
    python tools/serval/save/decode_serial_log.py records/serial/serial_2026-11-15_20-30-00.bin
    python tools/serval/save/decode_serial_log.py xxx.bin -o out/ --plot
    python tools/serval/save/decode_serial_log.py --selftest

输出:
    out/tx.csv   下发帧（VisionToGimbal，30 字节）
    out/rx.csv   反馈帧（GimbalToVision，44 字节）
    out/waveform.png   --plot 时生成，四格：
                  ① yaw 下发 vs 实际   ② pitch 下发 vs 实际
                  ③ 跟随误差（指令−实际）④ 收发帧间隔（链路质量）
                  图上的红色 × 是拒收记录

文件格式（小端）:
    头 24 字节: magic u32('RMSE'=0x45534D52), dir u8, status u8, len u16,
                t_ns u64, seq u32, reserved u32
    负载 len 字节: 线上原始帧（含 'SP' 帧头与 CRC16）
"""

from __future__ import annotations

import argparse
import csv
import math
import os
import struct
import sys
from collections import Counter
from pathlib import Path

# Windows 控制台默认不是 UTF-8，避免中文乱码
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        try:
            _stream.reconfigure(encoding="utf-8")
        except Exception:
            pass

# The saved script can be launched directly from any working directory.
SERVAL_DIR = str(Path(__file__).resolve().parents[1])
if SERVAL_DIR not in sys.path:
    sys.path.insert(0, SERVAL_DIR)

from lib.analysis import aligned_errors
from lib.protocol import ProtocolError, RX, TX, check_crc16, crc16
from lib.recording import HEADER as HDR, MAGIC, STATUS_ERR, STATUS_HEAD, STATUS_OK, decode_record

DIR_TX, DIR_RX = 0, 1


def read_records(path):
    """逐条读记录，遇到截断的尾部就停下（录制被 kill 时会出现）。"""
    total = os.path.getsize(path)
    with open(path, "rb") as f:
        while True:
            head = f.read(HDR.size)
            if len(head) < HDR.size:
                if head:
                    print(f"[warn] 已忽略末尾 {len(head)} 字节的不完整记录头", file=sys.stderr)
                break
            magic, dir_, status, length, t_ns, seq, _ = HDR.unpack(head)
            if magic != MAGIC:
                raise ValueError(f"magic 不符 @ offset {f.tell() - HDR.size}: 0x{magic:08x}")
            if dir_ not in (DIR_TX, DIR_RX) or not 1 <= length <= 64:
                raise ValueError(f"记录头损坏 @ offset {f.tell() - HDR.size}，方向 {dir_}，长度 {length}")
            payload = f.read(length)
            if len(payload) < length:
                print(f"[warn] 尾部记录被截断，已忽略最后 {length - len(payload)} 字节", file=sys.stderr)
                break
            yield dir_, status, t_ns, seq, payload
    print(f"[info] 文件 {path} 共 {total} 字节")


def decode(path):
    rows = ([], [])
    bad_len = {"tx": 0, "rx": 0}
    rejected = {"tx": Counter(), "rx": Counter()}
    crc_bad = write_err = 0
    for direction, status, t_ns, seq, payload in read_records(path):
        name = "tx" if direction == DIR_TX else "rx"
        row = dict(t_s=t_ns * 1e-9, time_ns=t_ns, seq=seq, log_status=f"0x{status:02X}",
                   frame_length=len(payload), valid=0, reject_reason="", raw=payload.hex())
        if direction == DIR_TX:
            row["write_ok"] = int(bool(status & STATUS_OK) and not status & STATUS_ERR)
            write_err += 1 - row["write_ok"]
        try:
            values = decode_record(direction, status, payload)
        except ProtocolError as error:
            row["reject_reason"] = error.code
            rejected[name][error.code] += 1
            row["crc_ok"] = int(payload.startswith(b"SP") and check_crc16(payload))
            expected = TX.size if direction == DIR_TX else RX.size
            if len(payload) != expected:
                bad_len[name] += 1
            elif direction == DIR_RX and not row["crc_ok"]:
                crc_bad += 1
        else:
            row["valid"] = row["crc_ok"] = 1
            yaw, pitch = values.pop("yaw"), values.pop("pitch")
            row.update(values, yaw_deg=math.degrees(yaw), pitch_deg=math.degrees(pitch),
                       yaw_rad=yaw, pitch_rad=pitch)
        rows[direction].append(row)

    return rows[0], rows[1], dict(bad_len=bad_len, crc_bad=crc_bad, write_err=write_err, rejected=rejected)


RECORD_FIELDS = ["time_ns", "log_status", "frame_length", "valid", "reject_reason", "yaw_rad", "pitch_rad"]
TX_FIELDS = ["t_s", "seq", "write_ok", "crc_ok", "mode", "target_id", "yaw_deg", "pitch_deg",
             "yaw_vel", "pitch_vel", "yaw_acc", "pitch_acc", "raw", *RECORD_FIELDS]
RX_FIELDS = ["t_s", "seq", "crc_ok", "mode", "qw", "qx", "qy", "qz", "yaw_deg", "pitch_deg",
             "yaw_vel", "pitch_vel", "bullet_speed", "bullet_count", "enemy_color", "raw",
             *RECORD_FIELDS, "q_norm", "q_yaw", "q_pitch", "q_roll"]


def write_csv(path, rows, fields):
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)
    print(f"[info] 写出 {path} ({len(rows)} 行)")


def timing_points(rows):
    previous = None
    for row in rows:
        if row["valid"] and previous is not None:
            elapsed = row["time_ns"] - previous["time_ns"]
            if elapsed > 0 and ((row["seq"] - previous["seq"]) & 0xffffffff) == 1:
                yield row["t_s"], elapsed * 1e-6
        previous = row if row["valid"] else None


def summarize(tx_rows, rx_rows, extra):
    print("\n===== 解码摘要 =====")
    for name, rows in (("TX", tx_rows), ("RX", rx_rows)):
        valid = sum(row["valid"] for row in rows)
        print(f"{name} 记录 {len(rows)} 包，有效 {valid} 包，拒收 {len(rows) - valid} 包")
        gaps = sorted(value for _, value in timing_points(rows))
        if gaps:
            mean = sum(gaps) / len(gaps)
            p99 = gaps[min(len(gaps)-1, int(len(gaps)*.99))]
            print(f"  相邻有效序号平均间隔 {mean:.3f} ms，P99 {p99:.3f} ms")
        reasons = extra["rejected"][name.lower()]
        if reasons:
            print("  拒收原因 " + ", ".join(f"{reason}={count}" for reason, count in sorted(reasons.items())))
    print(f"TX 写入失败 {extra['write_err']} 包，RX CRC 异常 {extra['crc_bad']} 包")
    print("=====================\n")


def plot_samples(rows, field, command):
    times, values = [], []
    previous = None
    for row in rows:
        value = row.get(field, math.nan)
        if previous is not None and row["valid"]:
            duration = row["time_ns"] - previous["time_ns"]
            sequential = ((row["seq"] - previous["seq"]) & 0xffffffff) == 1
            if (not sequential or duration > 100_000_000 or
                    (command and row["mode"] != previous["mode"]) or abs(value - previous[field]) > 180):
                times.append(row["t_s"])
                values.append(math.nan)
        times.append(row["t_s"])
        values.append(value)
        previous = row if row["valid"] else None
    return times, values


def plot(tx_rows, rx_rows, out_path):
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except ImportError:
        print("[warn] 没装 matplotlib，已跳过波形图", file=sys.stderr)
        return

    plt.rcParams["font.sans-serif"] = ["Microsoft YaHei", "SimHei", "Noto Sans CJK SC", "DejaVu Sans"]
    plt.rcParams["axes.unicode_minus"] = False
    directions = [("tx", sorted(tx_rows, key=lambda row: row["time_ns"]), "#377fbd"),
                  ("rx", sorted(rx_rows, key=lambda row: row["time_ns"]), "#248e6b")]
    fig, axes = plt.subplots(4, 1, figsize=(12, 10.2), sharex=True)
    for ax, field in zip(axes, ("yaw", "pitch")):
        for direction, rows, color in directions:
            if any(row["valid"] for row in rows):
                x, y = plot_samples(rows, field + "_deg", direction == "tx")
                ax.plot(x, y, color=color, lw=1.4, label=direction.upper(),
                        drawstyle="steps-post" if direction == "tx" else "default")
        rejected = [row["t_s"] for _, rows, _ in directions for row in rows if not row["valid"]]
        if rejected:
            ax.plot(rejected, [.06] * len(rejected), "x", color="#d35c56",
                    transform=ax.get_xaxis_transform(), label="拒收记录")
        ax.set_ylabel(f"{field} / °")

    frames = []
    for direction, rows, _ in directions:
        for row in rows:
            if row["valid"] or direction == "tx":
                # A rejected TX still marks the end of the prior command's hold.
                values = {"mode": row["mode"], "yaw": row["yaw_rad"], "pitch": row["pitch_rad"]} if row["valid"] else {}
                frames.append(dict(t=row["t_s"], direction=direction, values=values))
    errors = aligned_errors(frames, -math.inf, math.inf)
    for field, color in (("yaw", "#248e6b"), ("pitch", "#9467bd")):
        if errors:
            axes[2].plot([item["t"] for item in errors], [math.degrees(item[field]) for item in errors],
                         ".", markersize=1.8, color=color, label=field)
    axes[2].set_ylabel("对齐差值 / °")
    axes[2].axhline(0, color="0.55", lw=.8)
    if not errors:
        axes[2].text(.5, .5, "没有可配对的有效记录", ha="center", transform=axes[2].transAxes)

    for direction, rows, color in directions:
        points = list(timing_points(rows))
        if points:
            x, y = zip(*points)
            axes[3].plot(x, y, ".", markersize=1.8, color=color, label=direction.upper())
    axes[3].set_ylabel("相邻帧间隔 / ms")
    axes[3].set_xlabel("记录时间 / s")
    axes[3].ticklabel_format(axis="y", useOffset=False, style="plain")
    for ax in axes:
        ax.grid(alpha=.24)
        if ax.get_legend_handles_labels()[0]:
            ax.legend(loc="upper right", fontsize=9)
    fig.suptitle("串口记录离线解码", fontsize=13)
    fig.tight_layout()
    fig.savefig(out_path, dpi=150)
    plt.close(fig)
    print(f"[info] 波形图 {out_path}")


def selftest(out_dir):
    """造一段假日志，验证解码链路（同时也验证 CRC 实现）。

    假数据模拟“云台一阶滞后跟随指令”，这样画出来应该能看到：
    指令是台阶，实际是平滑曲线，误差先大后收敛 —— 也就是真实录播该有的样子。
    """
    os.makedirs(out_dir, exist_ok=True)
    path = os.path.join(out_dir, "selftest.bin")

    def cmd_deg(i):
        t = i * 0.01
        if t < 0.5:
            return 0.0
        if t < 1.5:
            return 10.0
        if t < 2.5:
            return 4.0
        return -6.0

    def build_tx(cmd_deg_value, mode=1):
        body = struct.pack(
            "<2sB6fB", b"SP", mode, cmd_deg_value / 57.29577951308232, 0.2, 0.0,
            -0.05, 0.0, 0.0, 1)
        return body + struct.pack("<H", crc16(body))

    def build_rx(rx_deg):
        q = (1.0, 0.0, 0.0, 0.0)
        body = struct.pack(
            "<2sB4f5fHB", b"SP", 1, *q, rx_deg / 57.29577951308232, 0.1, -0.05, 0.1,
            22.5, 1000, 0)
        return body + struct.pack("<H", crc16(body))

    rx_deg = 0.0
    with open(path, "wb") as f:
        for i in range(300):
            t_ns = int(i * 1e7)
            cmd = cmd_deg(i)
            rx_deg += 0.25 * (cmd - rx_deg)  # 一阶滞后

            tx = build_tx(cmd)
            f.write(HDR.pack(MAGIC, DIR_TX, STATUS_HEAD | STATUS_OK, len(tx), t_ns, i, 0))
            f.write(tx)

            rx_status = STATUS_HEAD | STATUS_OK
            rx = build_rx(rx_deg)
            if i == 120:
                # 注入一帧 CRC 错误：模拟线上被打断/校验失败，验证解算能标出来
                rx = bytearray(rx)
                rx[12] ^= 0xFF
                rx = bytes(rx)
                rx_status = STATUS_HEAD
            f.write(HDR.pack(MAGIC, DIR_RX, rx_status, len(rx), t_ns + 500_000, i, 0))
            f.write(rx)

    tx_rows, rx_rows, extra = decode(path)
    assert len(tx_rows) == 300 and len(rx_rows) == 300, (len(tx_rows), len(rx_rows))
    assert all(r["crc_ok"] for r in tx_rows), "TX CRC 自检失败"
    bad_count = sum(1 for r in rx_rows if not r["crc_ok"])
    assert bad_count == 1, f"应识别出 1 帧 CRC 错误，实际 {bad_count}"
    assert extra["crc_bad"] == 1, extra["crc_bad"]

    write_csv(os.path.join(out_dir, "tx.csv"), tx_rows, TX_FIELDS)
    write_csv(os.path.join(out_dir, "rx.csv"), rx_rows, RX_FIELDS)
    plot(tx_rows, rx_rows, os.path.join(out_dir, "waveform.png"))
    print("[ok] selftest 通过：解码、CRC 识别、CSV、绘图链路都正常")


def main():
    ap = argparse.ArgumentParser(description="离线解算串口原始日志")
    ap.add_argument("log", nargs="?", help="records/serial/*.bin")
    ap.add_argument("-o", "--out-dir", default=None, help="输出目录，默认与日志同名")
    ap.add_argument("--plot", action="store_true", help="生成 waveform.png")
    ap.add_argument("--selftest", action="store_true", help="生成假日志并自检")
    args = ap.parse_args()

    if args.selftest:
        selftest(args.out_dir or "out/selftest")
        return 0

    if not args.log:
        ap.error("需要日志路径，或使用 --selftest")

    out_dir = args.out_dir or os.path.splitext(args.log)[0]
    try:
        tx_rows, rx_rows, extra = decode(args.log)
        if not tx_rows and not rx_rows:
            raise ValueError("记录中没有可解码的 TX 或 RX 帧")
        os.makedirs(out_dir, exist_ok=True)
        write_csv(os.path.join(out_dir, "tx.csv"), tx_rows, TX_FIELDS)
        write_csv(os.path.join(out_dir, "rx.csv"), rx_rows, RX_FIELDS)
        summarize(tx_rows, rx_rows, extra)
        if args.plot:
            plot(tx_rows, rx_rows, os.path.join(out_dir, "waveform.png"))
    except (OSError, ValueError) as error:
        ap.exit(1, f"[error] {error}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
