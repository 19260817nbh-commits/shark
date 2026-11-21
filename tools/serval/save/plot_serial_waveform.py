"""串口监测"波形"示意图生成脚本（纯示意数据，不读取真实串口）。

作用：把「自瞄下发的角度」和「云台反馈的实际角度」画在同一时间轴上，
说明所谓"发送 vs 实际执行"的波形图到底画的是什么。

x 轴：时间（秒）
y 轴：角度（度）

真实使用时，把下面 gen_demo() 换成读 CSV 即可：
    t, yaw_cmd, yaw_actual = np.loadtxt("gimbal_link.csv", delimiter=",",
                                        skiprows=1, usecols=(0, 1, 2), unpack=True)
CSV 列建议：t_host_s, yaw_cmd_deg, yaw_actual_deg, pitch_cmd_deg, pitch_actual_deg
"""

import numpy as np
import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.patches import FancyArrowPatch

plt.rcParams["font.sans-serif"] = ["Microsoft YaHei", "SimHei", "DejaVu Sans"]
plt.rcParams["axes.unicode_minus"] = False

FS = 100.0          # 自瞄发送频率 100 Hz -> 每 10 ms 一帧
DURATION = 2.0
FPS = DURATION * FS


def command(t: np.ndarray) -> np.ndarray:
    """假设的瞄准指令：几个阶跃，模拟目标切换。"""
    y = np.zeros_like(t)
    y[t >= 0.20] = 10.0
    y[t >= 0.90] = 12.5
    y[t >= 1.45] = 5.0
    return y


def gen_demo():
    t = np.arange(0.0, DURATION, 1.0 / FS)
    u = command(t)

    # 二阶系统模拟云台响应：自然频率 26 rad/s，阻尼 0.72（略有超调）
    wn, zeta, dt = 26.0, 0.72, 0.0005
    ts = np.arange(0.0, DURATION, dt)
    us = command(ts)
    x = np.zeros_like(ts)
    v = np.zeros_like(ts)
    for i in range(1, len(ts)):
        a = wn * wn * (us[i - 1] - x[i - 1]) - 2 * zeta * wn * v[i - 1]
        v[i] = v[i - 1] + a * dt
        x[i] = x[i - 1] + v[i] * dt

    rng = np.random.default_rng(0)
    y = np.interp(t, ts, x) + rng.normal(0.0, 0.012, t.size)  # 反馈测量噪声
    return t, u, y


def main(out_path: str = "tools/serial_waveform_demo.png"):
    t, u, y = gen_demo()
    err = u - y

    fig, (ax1, ax2) = plt.subplots(
        2, 1, figsize=(11.5, 6.8), sharex=True,
        gridspec_kw={"height_ratios": [3.2, 1.3], "hspace": 0.12},
    )

    # ---- 上图：两条曲线 = 波形 ----
    ax1.step(t, u, where="post", color="#d62728", lw=2.2,
             label="自瞄下发角度  yaw_cmd（每 10 ms 一帧）")
    ax1.plot(t, y, color="#1f77b4", lw=2.2, label="云台反馈实际角度  yaw_actual")
    ax1.plot(t, y, "o", color="#1f77b4", ms=2.4, alpha=0.55,
             label="每帧一个采样点，连起来就是曲线")

    for tx in (0.20, 0.90, 1.45):
        ax1.axvline(tx, color="0.6", ls="--", lw=0.9, zorder=0)

    ax1.set_ylabel("yaw 角度 / (°)")
    ax1.set_ylim(-2.0, 15.0)
    ax1.grid(alpha=0.25)
    ax1.legend(loc="upper left", fontsize=9, framealpha=0.9)

    # ① 指令跳变
    ax1.annotate("① 指令在这里跳变\n（自瞄算出新的瞄准角）",
                 xy=(0.205, 10.0), xytext=(0.34, 14.0),
                 fontsize=10, color="#d62728",
                 arrowprops=dict(arrowstyle="->", color="#d62728", lw=1.2))

    # ② 竖直差 = 跟随误差
    te = 0.30
    i = np.searchsorted(t, te)
    ax1.add_patch(FancyArrowPatch((te, y[i]), (te, u[i]),
                                  arrowstyle="<->", mutation_scale=12,
                                  color="#2ca02c", lw=1.4))
    ax1.annotate("② 竖直差 = 跟随误差\n(这一刻差了约 3°)",
                 xy=(te, 0.5 * (u[i] + y[i])), xytext=(te + 0.06, 5.0),
                 fontsize=10, color="#2ca02c",
                 arrowprops=dict(arrowstyle="->", color="#2ca02c", lw=1.2))

    # ③ 水平错开 = 响应滞后
    ax1.add_patch(FancyArrowPatch((0.205, 12.9), (0.32, 12.9),
                                  arrowstyle="<->", mutation_scale=12,
                                  color="#9467bd", lw=1.4))
    ax1.annotate("③ 水平错开 = 响应滞后\n(指令发出到实际跟上，约几十 ms)",
                 xy=(0.26, 12.9), xytext=(0.55, 14.3),
                 fontsize=10, color="#9467bd",
                 arrowprops=dict(arrowstyle="->", color="#9467bd", lw=1.2))

    # ④ 稳态重合
    ax1.annotate("④ 稳定后两条线重合\n= 云台跟上了",
                 xy=(1.25, 12.6), xytext=(1.30, 4.2),
                 fontsize=10, color="#555555",
                 arrowprops=dict(arrowstyle="->", color="#555555", lw=1.0))

    # ---- 下图：误差 = 两条曲线相减 ----
    ax2.fill_between(t, 0, err, color="#2ca02c", alpha=0.28, step="post")
    ax2.step(t, err, where="post", color="#2ca02c", lw=1.4)
    ax2.axhline(0, color="0.4", lw=0.9)
    ax2.set_xlabel("时间 / s")
    ax2.set_ylabel("误差 / (°)")
    ax2.set_ylim(-4.5, 6.5)
    ax2.grid(alpha=0.25)
    ax2.text(0.01, 0.92, "误差 = 下发 − 实际；>0 表示云台还落后，<0 表示冲过头",
             transform=ax2.transAxes, fontsize=9.5, va="top")

    fig.suptitle("串口监测里的“波形”：把两路数据放到同一条时间轴上（示意图，非真实录播数据）",
                 fontsize=13, y=0.975)
    fig.subplots_adjust(left=0.075, right=0.985, top=0.90, bottom=0.10)
    fig.savefig(out_path, dpi=160)
    print(f"saved -> {out_path}")


if __name__ == "__main__":
    main()
