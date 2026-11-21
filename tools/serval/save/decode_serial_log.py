#!/usr/bin/env python3
"""离线解算 SerialRecorder 录制的串口原始日志（records/serial/*.bin）。

录制端：io/gimbal/serial_recorder.cpp，环境变量 SP_SERIAL_LOG=1 开启。
本脚本只读文件，不碰串口，所以录一次可以反复解算、反复画图。

用法:
    python tools/decode_serial_log.py records/serial/serial_2026-11-15_20-30-00.bin
    python tools/decode_serial_log.py xxx.bin -o out/ --plot
    python tools/decode_serial_log.py xxx.bin --selftest     # 生成假日志验证解码链路

输出:
    out/tx.csv   下发帧（VisionToGimbal，30 字节）
    out/rx.csv   反馈帧（GimbalToVision，44 字节）
    out/waveform.png   --plot 时生成，四格：
                  ① yaw 下发 vs 实际   ② pitch 下发 vs 实际
                  ③ 跟随误差（指令−实际）④ 收发帧间隔（链路质量）
                  图上的红色 × 是 CRC 错误帧

文件格式（小端）:
    头 24 字节: magic u32('RMSE'=0x45534D52), dir u8, status u8, len u16,
                t_ns u64, seq u32, reserved u32
    负载 len 字节: 线上原始帧（含 'SP' 帧头与 CRC16）
"""

from __future__ import annotations

import argparse
import csv
import os
import struct
import sys

# Windows 控制台默认不是 UTF-8，避免中文乱码
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        try:
            _stream.reconfigure(encoding="utf-8")
        except Exception:
            pass

MAGIC = 0x45534D52  # 'RMSE'
HDR = struct.Struct("<IBBHQII")  # 24 bytes

# 与 io/gimbal/gimbal.hpp 的 packed 结构体一一对应
TX = struct.Struct("<2sB6fBH")
RX = struct.Struct("<2sB4f5fHBH")

DIR_TX, DIR_RX = 0, 1
STATUS_HEAD, STATUS_OK, STATUS_ERR = 1 << 0, 1 << 1, 1 << 2


def _crc16_table():
    table = []
    for i in range(256):
        crc = i
        for _ in range(8):
            crc = (crc >> 1) ^ 0x8408 if crc & 1 else crc >> 1
        table.append(crc)
    return table


_CRC16_TABLE = _crc16_table()


def crc16(data: bytes) -> int:
    crc = 0xFFFF
    for b in data:
        crc = (crc >> 8) ^ _CRC16_TABLE[(crc ^ b) & 0xFF]
    return crc


def check_crc16(frame: bytes) -> bool:
    """与 tools::check_crc16 一致：CRC 小端存在最后两字节。"""
    if len(frame) < 3:
        return False
    want = frame[-1] << 8 | frame[-2]
    return crc16(frame[:-2]) == want


def read_records(path):
    """逐条读记录，遇到截断的尾部就停下（录制被 kill 时会出现）。"""
    total = os.path.getsize(path)
    with open(path, "rb") as f:
        while True:
            head = f.read(HDR.size)
            if len(head) < HDR.size:
                break
            magic, dir_, status, length, t_ns, seq, _ = HDR.unpack(head)
            if magic != MAGIC:
                raise ValueError(f"magic 不符 @ offset {f.tell() - HDR.size}: 0x{magic:08x}")
            payload = f.read(length)
            if len(payload) < length:
                print(f"[warn] 尾部记录被截断，已忽略最后 {length - len(payload)} 字节", file=sys.stderr)
                break
            yield dir_, status, t_ns, seq, payload
    print(f"[info] 文件 {path} 共 {total} 字节")


def decode(path):
    tx_rows, rx_rows = [], []
    bad_len = {"tx": 0, "rx": 0}
    crc_bad = 0
    write_err = 0

    for dir_, status, t_ns, seq, payload in read_records(path):
        t_s = t_ns * 1e-9
        if dir_ == DIR_TX:
            if len(payload) != TX.size:
                bad_len["tx"] += 1
                continue
            head, mode, yaw, yaw_vel, yaw_acc, pitch, pitch_vel, pitch_acc, tid, crc = TX.unpack(
                payload
            )
            ok = head == b"SP" and check_crc16(payload)
            write_err += 0 if (status & STATUS_OK) else 1
            tx_rows.append(
                dict(
                    t_s=t_s,
                    seq=seq,
                    write_ok=int(bool(status & STATUS_OK)),
                    crc_ok=int(ok),
                    mode=mode,
                    target_id=tid,
                    yaw_deg=yaw * 57.29577951308232,
                    pitch_deg=pitch * 57.29577951308232,
                    yaw_vel=yaw_vel,
                    pitch_vel=pitch_vel,
                    yaw_acc=yaw_acc,
                    pitch_acc=pitch_acc,
                    raw=payload.hex(),
                )
            )
        elif dir_ == DIR_RX:
            if len(payload) != RX.size:
                bad_len["rx"] += 1
                continue
            (head, mode, qw, qx, qy, qz, yaw, yaw_vel, pitch, pitch_vel, bullet_speed,
             bullet_count, enemy_color, crc) = RX.unpack(payload)
            ok = head == b"SP" and check_crc16(payload)
            crc_bad += 0 if ok else 1
            rx_rows.append(
                dict(
                    t_s=t_s,
                    seq=seq,
                    crc_ok=int(ok),
                    mode=mode,
                    qw=qw, qx=qx, qy=qy, qz=qz,
                    yaw_deg=yaw * 57.29577951308232,
                    pitch_deg=pitch * 57.29577951308232,
                    yaw_vel=yaw_vel,
                    pitch_vel=pitch_vel,
                    bullet_speed=bullet_speed,
                    bullet_count=bullet_count,
                    enemy_color=enemy_color,
                    raw=payload.hex(),
                )
            )
        else:
            raise ValueError(f"未知方向 {dir_}")

    return tx_rows, rx_rows, dict(bad_len=bad_len, crc_bad=crc_bad, write_err=write_err)


TX_FIELDS = ["t_s", "seq", "write_ok", "crc_ok", "mode", "target_id", "yaw_deg", "pitch_deg",
             "yaw_vel", "pitch_vel", "yaw_acc", "pitch_acc", "raw"]
RX_FIELDS = ["t_s", "seq", "crc_ok", "mode", "qw", "qx", "qy", "qz", "yaw_deg", "pitch_deg",
             "yaw_vel", "pitch_vel", "bullet_speed", "bullet_count", "enemy_color", "raw"]


def write_csv(path, rows, fields):
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)
    print(f"[info] 写出 {path} ({len(rows)} 行)")


def intervals(rows):
    ts = [r["t_s"] for r in rows]
    if len(ts) < 3:
        return 0.0, 0.0
    d = [b - a for a, b in zip(ts, ts[1:])]
    d.sort()
    mean = sum(d) / len(d)
    p99 = d[int(len(d) * 0.99)]
    return mean, p99


def summarize(tx_rows, rx_rows, extra):
    print("\n===== 解算摘要 =====")
    if tx_rows:
        span = tx_rows[-1]["t_s"] - tx_rows[0]["t_s"]
        mean, p99 = intervals(tx_rows)
        print(f"TX 帧数 {len(tx_rows):6d}  时长 {span:.3f}s  平均间隔 {mean*1e3:.3f}ms "
              f"p99 {p99*1e3:.3f}ms  写失败 {extra['write_err']}")
        modes = {}
        for r in tx_rows:
            modes[r["mode"]] = modes.get(r["mode"], 0) + 1
        print(f"TX mode 分布 {modes}  (0=不控制 1=控制 2=开火)")
    else:
        print("TX 无有效帧")
    if rx_rows:
        span = rx_rows[-1]["t_s"] - rx_rows[0]["t_s"]
        mean, p99 = intervals(rx_rows)
        print(f"RX 帧数 {len(rx_rows):6d}  时长 {span:.3f}s  平均间隔 {mean*1e3:.3f}ms "
              f"p99 {p99*1e3:.3f}ms  CRC 错 {extra['crc_bad']}")
        last = rx_rows[-1]
        print(f"RX 末态 mode={last['mode']} bullet_count={last['bullet_count']} "
              f"bullet_speed={last['bullet_speed']:.2f} yaw={last['yaw_deg']:.2f}° "
              f"pitch={last['pitch_deg']:.2f}°")
    if extra["bad_len"]["tx"] or extra["bad_len"]["rx"]:
        print(f"长度异常的记录: {extra['bad_len']}")
    print("=====================\n")


def plot(tx_rows, rx_rows, out_path):
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except ImportError:
        print("[warn] 没装 matplotlib，跳过 --plot", file=sys.stderr)
        return

    plt.rcParams["font.sans-serif"] = ["Microsoft YaHei", "SimHei", "Noto Sans CJK SC",
                                       "DejaVu Sans"]
    plt.rcParams["axes.unicode_minus"] = False

    tx_t = [r["t_s"] for r in tx_rows]
    tx_yaw = [r["yaw_deg"] for r in tx_rows]
    tx_pitch = [r["pitch_deg"] for r in tx_rows]
    rx_t = [r["t_s"] for r in rx_rows]
    rx_yaw = [r["yaw_deg"] for r in rx_rows]
    rx_pitch = [r["pitch_deg"] for r in rx_rows]
    bad = [r for r in rx_rows if not r["crc_ok"]]

    fig, axes = plt.subplots(
        4, 1, figsize=(12, 10.2), sharex=True,
        gridspec_kw={"height_ratios": [3.0, 2.2, 1.8, 1.5]},
    )

    # ---- ① yaw：下发 vs 实际 ----
    ax = axes[0]
    ax.step(tx_t, tx_yaw, where="post", color="#d62728", lw=1.6, label="下发 yaw（TX）")
    ax.plot(rx_t, rx_yaw, color="#1f77b4", lw=1.6, label="实际 yaw（RX）")
    if bad:
        ax.plot([r["t_s"] for r in bad], [r["yaw_deg"] for r in bad], "x",
                color="#ff1f1f", ms=8, mew=1.8, label=f"CRC 错误帧 ×{len(bad)}")
    ax.set_ylabel("yaw / °")
    ax.grid(alpha=0.25)
    ax.legend(loc="upper right", fontsize=9)

    # ---- ② pitch：下发 vs 实际 ----
    ax = axes[1]
    ax.step(tx_t, tx_pitch, where="post", color="#d62728", lw=1.6, label="下发 pitch（TX）")
    ax.plot(rx_t, rx_pitch, color="#1f77b4", lw=1.6, label="实际 pitch（RX）")
    ax.set_ylabel("pitch / °")
    ax.grid(alpha=0.25)
    ax.legend(loc="upper right", fontsize=9)

    # ---- ③ 误差：RX 时刻上取最近的 TX 指令（TX 是阶梯，取前值） ----
    ax = axes[2]
    if tx_rows and rx_rows:
        import bisect

        def tx_value_at(t, key):
            i = bisect.bisect_right(tx_t, t) - 1
            return tx_rows[max(i, 0)][key]

        err_yaw = [tx_value_at(t, "yaw_deg") - y for t, y in zip(rx_t, rx_yaw)]
        err_pitch = [tx_value_at(t, "pitch_deg") - y for t, y in zip(rx_t, rx_pitch)]
        ax.plot(rx_t, err_yaw, color="#2ca02c", lw=1.2, label="误差 yaw")
        ax.plot(rx_t, err_pitch, color="#9467bd", lw=1.2, label="误差 pitch")
        ax.axhline(0, color="0.4", lw=0.8)
        ax.set_ylabel("指令 − 实际 / °")
        ax.grid(alpha=0.25)
        ax.legend(loc="upper right", fontsize=9)

    # ---- ④ 链路质量：帧间隔 ----
    ax = axes[3]
    dt_all = []
    if len(tx_t) > 1:
        dt_tx = [(b - a) * 1e3 for a, b in zip(tx_t, tx_t[1:])]
        dt_all += dt_tx
        m = sum(dt_tx) / len(dt_tx)
        ax.plot(tx_t[1:], dt_tx, color="#d62728", lw=1.0, label=f"TX 间隔（均值 {m:.2f} ms）")
    if len(rx_t) > 1:
        dt_rx = [(b - a) * 1e3 for a, b in zip(rx_t, rx_t[1:])]
        dt_all += dt_rx
        m = sum(dt_rx) / len(dt_rx)
        ax.plot(rx_t[1:], dt_rx, color="#1f77b4", lw=1.0, label=f"RX 间隔（均值 {m:.2f} ms）")
    ax.set_ylabel("帧间隔 / ms")
    ax.set_xlabel("时间 / s")
    ax.grid(alpha=0.25)
    ax.legend(loc="upper right", fontsize=8.5)
    # 间隔是固定值，禁用科学计数法的 offset，避免坐标轴显示成 "1e+01"
    ax.ticklabel_format(axis="y", useOffset=False, style="plain")
    if dt_all:
        ax.set_ylim(0, max(max(dt_all) * 1.3, 1.0))

    fig.suptitle("串口链路：下发 vs 实际（离线解算）", fontsize=13)
    fig.tight_layout()
    fig.savefig(out_path, dpi=150)
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
    os.makedirs(out_dir, exist_ok=True)

    tx_rows, rx_rows, extra = decode(args.log)
    write_csv(os.path.join(out_dir, "tx.csv"), tx_rows, TX_FIELDS)
    write_csv(os.path.join(out_dir, "rx.csv"), rx_rows, RX_FIELDS)
    summarize(tx_rows, rx_rows, extra)
    if args.plot:
        plot(tx_rows, rx_rows, os.path.join(out_dir, "waveform.png"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
